# SPDX-License-Identifier: MPL-2.0
"""btrfs-snapshot-core's use cases: one Run per invocation (clock guards,
state lists, creation, sealing of writable snapshots, deletions under the
circuit breaker and the re-checks before each one), the scan of a snapshot
folder, and the bodies of snapshot, prune, finish, list, caches and latest.
They reach the machine through the ports (btrfs/snapshot/ports.py) that
wiring.py builds; cli.py parses the command line and presenter.py prints
the answers of list, caches and latest.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Iterable
from collections.abc import Sequence
from typing import Literal
import dataclasses
import datetime as dt
import os
import re
from btrfs.snapshot.domain import (
    CLOCK_SLACK,
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    NTP_GRACE,
    SAFETY_NET,
    STATE_FILE,
    UNFINISHED_FILE,
    UUID_RE,
    WRITABLE_FILE,
    CacheWalk,
    Decision,
    LastRun,
    LatestQuery,
    Listing,
    ParseError,
    RunOptions,
    Scan,
    Snap,
    Source,
    SourceCaches,
    SourceListing,
    SubvolInfo,
    SubvolPath,
    apply_safeguards,
    epoch_to_wall,
    err_text,
    fmt_epoch,
    fmt_span,
    format_policy,
    is_mass_delete,
    mass_delete_limit,
    name_regex,
    newest_of,
    other_parent,
    parse_snapshot_name,
    parse_state,
    plan,
    snapshot_name,
    unexplained_deletions,
    wall_to_epoch,
)
from btrfs.snapshot.ports import BtrfsPort, LogFn, PinsPort, RunLockPort, SnapshotHostPort, StateStorePort
from cst_core.domain.errors import Usage
from cst_core.domain.values import format_bytes_human


@dataclasses.dataclass(frozen=True, slots=True)
class Machine:
    """The ports one invocation works through (wiring.py builds them)."""

    host: SnapshotHostPort
    btrfs: BtrfsPort
    state: StateStorePort
    pins: PinsPort
    lock: RunLockPort
    log: LogFn


class Run:
    """State of one snapshot/prune/finish/list/caches invocation."""

    def __init__(self, options: RunOptions, machine: Machine, tz: dt.tzinfo) -> None:
        self.options = options
        self.host = machine.host
        self.btrfs = machine.btrfs
        self.state = machine.state
        self.pin_file = machine.pins
        self.log = machine.log
        self.tz = tz
        self.now = self.host.time()
        self.boot_id, self.boottime = self.host.boot()
        self.snapdir = os.path.normpath(options.snapdir)
        self._ntp: bool | None = None
        self._pins: tuple[set[str], str | None] | None = None
        self._last_run: tuple[LastRun | None, str | None] | None = None
        self.writable: set[str] = set()  # writable entries named like ours, seen by this run's scans
        self.scanned_all = True  # False: a source could not be scanned (keep the old record)

    def mark_unscanned(self) -> None:
        """A source could not be scanned: the record of writable entries stays as it is."""
        self.scanned_all = False

    def note_writable(self, scan: Scan) -> None:
        """Add the writable entries SCAN saw to this run's record."""
        self.writable.update(os.path.basename(p) for p in scan.writable)

    # Lists in the state directory ----------------------------------------------
    def state_file(self, name: str) -> str:
        return os.path.join(self.options.state_dir, name)

    def read_list(self, name: str) -> list[str]:
        """The entries (one per line) of STATE/name; [] if it does not exist.
        OSError if it cannot be read."""
        return self.state.read_list(name)

    def write_list(self, name: str, entries: Sequence[str], *, sync_dir: bool = True) -> None:
        """Replace STATE/name with ENTRIES, durably; removed when ENTRIES is empty."""
        self.state.write_list(name, entries, sync_dir=sync_dir)

    def list_add(self, name: str, entry: str) -> None:
        entries = self.read_list(name)
        if entry not in entries:
            self.write_list(name, entries + [entry])

    def list_remove(self, name: str, entry: str) -> None:
        entries = self.read_list(name)
        if entry in entries:
            self.write_list(name, [e for e in entries if e != entry])

    def record_writable(self) -> None:
        """STATE/core-writable: the writable entries the scans saw (cst doctor
        cannot open the root-only snapshot folder); removed when none."""
        if not self.scanned_all:
            return
        entries = sorted(self.writable)
        try:
            if self.read_list(WRITABLE_FILE) != entries:  # unchanged: no rewrite, no fsync
                self.write_list(WRITABLE_FILE, entries)
        except OSError as e:
            self.log(4, f"cannot write {self.state_file(WRITABLE_FILE)}: {e.strerror}")

    # Clock guards -----------------------------------------------------------
    def ntp_problem(self) -> str | None:
        if self.options.assume_clock_ok:
            return None
        if self._ntp is None:
            rc, out, _ = self.host.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
            self._ntp = rc == 0 and out.strip() == "yes"
        return None if self._ntp else "the system clock is not NTP-synchronized (timedatectl NTPSynchronized != yes)"

    def last_run(self) -> tuple[LastRun | None, str | None]:
        """(LastRun or None, problem or None)."""
        if self._last_run is None:
            try:
                text = self.state.read_record(STATE_FILE)
            except (OSError, UnicodeDecodeError) as e:
                self._last_run = (None, f"cannot read {self.state_file(STATE_FILE)}: {e}")
            else:
                self._last_run = (None, None) if text is None else self.parse_last_run(text)
        return self._last_run

    def parse_last_run(self, text: str) -> tuple[LastRun | None, str | None]:
        record = parse_state(text)
        if record:
            return record, None
        return None, f"unreadable state {self.state_file(STATE_FILE)} ({text[:60]!r}); remove it if the clock is right"

    def behind_last_run(self) -> str | None:
        """Why the clock is not to be trusted against the state file (None = fine)."""
        last, problem = self.last_run()
        if problem:
            return problem
        if last is not None and self.now < last.epoch - CLOCK_SLACK:
            return (
                f"the clock ({fmt_epoch(self.now, self.tz)}) is behind the last successful run "
                f"({fmt_epoch(last.epoch, self.tz)}); if the clock is right, remove {self.state_file(STATE_FILE)}"
            )
        return None

    def backwards_problem(self, newest: Snap | None) -> str | None:
        """Deletions: the clock runs behind what this tool already recorded."""
        problem = self.behind_last_run()
        if problem:
            return problem
        if newest is not None:
            newest_epoch = wall_to_epoch(newest.wall, self.tz)
            if self.now < newest_epoch - CLOCK_SLACK:
                return (
                    f"the clock ({fmt_epoch(self.now, self.tz)}) is behind the newest snapshot "
                    f"{newest.entry}; if the clock is right, that name came from a clock that ran ahead: "
                    f"check it with '{self.options.list_hint}' and delete it with "
                    f"'btrfs subvolume delete {newest.path or newest.entry}'"
                )
        return None

    def clock_jump(self) -> float:
        """Seconds the wall clock advanced beyond the real time elapsed since the
        last successful run, if that run was in this boot (else 0: unknown)."""
        last, problem = self.last_run()
        if (
            problem
            or last is None
            or last.boot_id is None
            or last.boottime is None
            or self.boot_id is None
            or self.boottime is None
        ):
            return 0
        if last.boot_id != self.boot_id or self.boottime < last.boottime:
            return 0
        return max(0, (self.now - last.epoch) - (self.boottime - last.boottime))

    def creation_check(self, newest: Snap | None) -> tuple[str | None, list[tuple[int, str]]]:
        """(problem, notes) for naming a new snapshot after the clock: problem =
        why not to create it now (a refusal), notes = (level, text) to log if it
        is created."""
        problem = self.behind_last_run()
        if problem:
            return problem, []
        synced = self.ntp_problem() is None
        last, _ = self.last_run()
        newest_epoch = wall_to_epoch(newest.wall, self.tz) if newest is not None else None
        notes: list[tuple[int, str]] = []
        if newest is not None and newest_epoch is not None and self.now < newest_epoch - CLOCK_SLACK:
            if not synced:
                return (
                    f"the clock ({fmt_epoch(self.now, self.tz)}) is behind the newest snapshot "
                    f"{newest.entry} and not NTP-synchronized; if the clock is right, that name came "
                    f"from a clock that ran ahead: check it with '{self.options.list_hint}' and delete it "
                    f"with 'btrfs subvolume delete {newest.path}'"
                ), []
            notes.append(
                (
                    4,
                    f"the newest snapshot {newest.entry} is dated in the future (named while the clock "
                    f"ran ahead?); it is kept until its date passes - if the name is wrong, delete it "
                    f"with 'btrfs subvolume delete {newest.path}'",
                )
            )
        if not synced:
            jump = self.clock_jump()
            if jump > CLOCK_SLACK:
                assert last is not None
                return (
                    f"the clock is not NTP-synchronized and moved {fmt_span(jump)} further than the time "
                    f"elapsed since the last successful run ({fmt_epoch(last.epoch, self.tz)}, same boot)"
                ), []
            refs = [e for e in (newest_epoch, last.epoch if last else None) if e is not None]
            if refs and self.now > max(refs) + SAFETY_NET and self.boottime is not None and self.boottime < NTP_GRACE:
                return (
                    f"the clock ({fmt_epoch(self.now, self.tz)}) is not NTP-synchronized yet, the system "
                    f"booted {fmt_span(self.boottime)} ago, and the clock is more than 24 h ahead of the "
                    f"newest snapshot and the last successful run; not freezing a possibly wrong clock "
                    f"into a name - the next run catches up"
                ), []
            notes.append((5, "the clock is not NTP-synchronized; the name uses the unverified time"))
        return None, notes

    def forget_pins(self) -> None:
        """The next pins() reads the pin file again (pins are re-read before every deletion)."""
        self._pins = None

    def pins(self) -> tuple[set[str], str | None]:
        """(set of pinned paths, problem or None)."""
        if self._pins is None:
            self._pins = self.pin_file.read()
        return self._pins

    def is_pinned(self, path: str) -> bool:
        """A pin covers PATH (pins as last read)."""
        return self.pin_file.covers(path, self.pins()[0])

    def pinned_among(self, snaps: Iterable[Snap]) -> set[str]:
        """The paths of the SNAPS a pin covers."""
        return {s.path for s in snaps if self.is_pinned(s.path)}

    def record_success(self) -> None:
        """Store this run as the last successful one - only with a trusted clock,
        and never moving the record backwards (it guards against a clock that
        later runs behind)."""
        if self.ntp_problem():
            self.log(6, "the clock is not NTP-synchronized; this run is not recorded as the last successful one")
            return
        last, problem = self.last_run()
        if problem:
            self.log(4, f"not recording this run: {problem}")
            return
        if last is not None and self.now < last.epoch:
            if self.now < last.epoch - CLOCK_SLACK:
                self.log(
                    4,
                    f"the clock ({fmt_epoch(self.now, self.tz)}) is behind the last successful run "
                    f"({fmt_epoch(last.epoch, self.tz)}); keeping that record - if the clock is right, "
                    f"remove {self.state_file(STATE_FILE)}",
                )
            return
        line = str(int(self.now))
        if self.boot_id and UUID_RE.fullmatch(self.boot_id) and self.boottime is not None:
            line += f" {self.boot_id} {self.boottime:.3f}"
        # The file is fsynced, its folder is not (a crash at worst keeps the
        # previous record): one btrfs log sync less per run.
        self.write_list(STATE_FILE, [line], sync_dir=False)
        self._last_run = (parse_state(line), None)

    # Per source ---------------------------------------------------------------
    def source_info(self, src: Source) -> SubvolInfo:
        kind = self.host.kind(src.path)
        if kind != "subvol":
            raise ParseError(f"{src.path} is not a subvolume ({kind})")
        return self.btrfs.show(SubvolPath(src.path))

    def decide(
        self, src: Source, scan: Scan, extra: Sequence[Snap] = ()
    ) -> tuple[list[Decision], list[Decision], str | None]:
        """Decisions after policy and safeguards, the deletion candidates, and
        why deleting is refused (None = allowed)."""
        snaps = list(scan.owned) + list(extra)
        pin_problem = self.pins()[1]
        pinned = self.pinned_among(snaps)
        decisions = apply_safeguards(plan(snaps, self.now, src.policy, self.tz), self.now, self.tz, pinned)
        doomed = [d for d in decisions if d.action == "delete" and not d.snap.virtual]
        refusal = None
        if doomed:
            # A dry run counts the snapshot it would create, like the real run will.
            newest = newest_of(snaps)
            if scan.unverifiable:
                path, why = scan.unverifiable[0]
                refusal = (
                    f"{len(scan.unverifiable)} entr{'y' if len(scan.unverifiable) == 1 else 'ies'} "
                    f"could not be verified ({path}: {why})"
                )
            elif pin_problem:
                refusal = pin_problem
            else:
                refusal = self.backwards_problem(newest) or self.ntp_problem()
            owned = len(snaps)
            if refusal is None and not self.options.allow_mass_delete:
                refusal = self.breaker(src, decisions, snaps, pinned, doomed, owned)
        return decisions, doomed, refusal

    def breaker(
        self,
        src: Source,
        decisions: Sequence[Decision],
        snaps: Sequence[Snap],
        pinned: set[str],
        doomed: Sequence[Decision],
        owned: int,
    ) -> str | None:
        last, _ = self.last_run()
        since = last.epoch if last is not None else None
        jump = self.clock_jump()
        ref = self.now - jump if jump > CLOCK_SLACK else None
        unexplained = unexplained_deletions(decisions, snaps, src.policy, self.tz, pinned, since, ref)
        if not is_mass_delete(len(unexplained), owned):
            return None
        if since is None:
            why = "no successful run is recorded, so none of them is explained by the time passed"
        else:
            why = (
                f"{len(unexplained)} of them not explained by the time passed since the last successful "
                f"run ({fmt_epoch(since, self.tz)})"
            )
            if ref is not None:
                why += f"; the clock moved {fmt_span(jump)} further than the time elapsed since then (same boot)"
            else:
                why += "; was the policy shortened?"
        return (
            f"circuit breaker: {len(doomed)} of {owned} snapshots would be deleted, {why} "
            f"(limit {mass_delete_limit(owned)}); check with '{self.options.list_hint}', "
            f"then delete them with '{self.options.confirm_hint}'"
        )


@dataclasses.dataclass
class Result:
    name: str
    status: int = EXIT_OK
    created: str = ""
    deleted: int = 0
    kept: int = 0
    note: str = ""

    def worse(self, status: int) -> None:
        # error beats refusal beats ok
        rank = {EXIT_OK: 0, EXIT_REFUSED: 1, EXIT_ERROR: 2}
        if rank[status] > rank[self.status]:
            self.status = status


def process_source(run: Run, src: Source, create: bool) -> Result:
    args = run.options
    res = Result(src.name)
    dry = args.dry_run
    would = "would " if dry else ""
    try:
        info = run.source_info(src)
    except ParseError as e:
        run.log(3, f"{src.name}: cannot read source {src.path}: {e}; nothing done")
        res.worse(EXIT_ERROR)
        res.note = "source unreadable"
        run.mark_unscanned()
        return res
    try:
        scan = scan_snapshots(run.host, run.btrfs, run.snapdir, src.name, info["uuid"])
    except ParseError as e:
        run.log(3, f"{src.name}: {e}; nothing done")
        res.worse(EXIT_ERROR)
        run.mark_unscanned()
        return res
    run.note_writable(scan)
    for path, why in scan.unverifiable:
        run.log(4, f"{src.name}: cannot verify {path}: {why}")
    newest = newest_of(scan.owned)
    extra: list[Snap] = []

    if create:
        reason = None
        if args.force:
            reason = "forced"
        elif newest is None:
            reason = "no snapshot yet"
        elif info["generation"] > newest.cgen:
            reason = f"changed (generation {info['generation']} > {newest.cgen} of {newest.entry})"
        elif run.now - wall_to_epoch(newest.wall, run.tz) > SAFETY_NET:
            reason = f"newest snapshot {newest.entry} is older than 24 h"
        elif run.now < wall_to_epoch(newest.wall, run.tz) - CLOCK_SLACK and run.ntp_problem() is None:
            # Named while the clock ran ahead. A correctly named snapshot becomes
            # the newest by generation, which lets deletions resume.
            reason = f"newest snapshot {newest.entry} is dated in the future"
        if reason is None:
            assert newest is not None
            run.log(6, f"{src.name}: unchanged since {newest.entry}; no new snapshot")
        else:
            problem, notes = run.creation_check(newest)
            if problem:
                run.log(4, f"{src.name}: not creating a snapshot: {problem}")
                res.worse(EXIT_REFUSED)
                res.note = "clock"
            else:
                for level, note in notes:
                    run.log(level, f"{src.name}: {note}")
                wall = epoch_to_wall(run.now, run.tz).replace(second=0, microsecond=0)
                entry = free_name(run.host, run.snapdir, src.name, wall)
                dest = os.path.join(run.snapdir, entry)
                if dry:
                    run.log(5, f"{src.name}: would create {dest} ({reason})")
                    if args.exclude_caches:
                        preview_drop(run, src)
                    # The real snapshot's "Gen at creation" is the running transaction:
                    # above the source's generation and every existing snapshot's.
                    cgen = max([info["generation"]] + [s.cgen for s in scan.owned]) + 1
                    parsed_entry = parse_snapshot_name(src.name, entry)
                    assert parsed_entry is not None
                    extra.append(
                        Snap(
                            entry=entry,
                            wall=wall,
                            seq=parsed_entry[1],
                            path=dest,
                            cgen=cgen,
                            virtual=True,
                        )
                    )
                    res.created = entry
                elif create_snapshot(run, src, info, dest, entry, reason):
                    res.created = entry
                    try:
                        scan = scan_snapshots(run.host, run.btrfs, run.snapdir, src.name, info["uuid"])
                    except ParseError as e:
                        run.log(3, f"{src.name}: {e}; no deletions")
                        res.worse(EXIT_ERROR)
                        run.mark_unscanned()
                        return res
                    run.note_writable(scan)
                    if not any(s.entry == entry for s in scan.owned):
                        run.log(3, f"{src.name}: created {dest} but it does not pass the ownership check")
                        res.worse(EXIT_ERROR)
                else:
                    res.worse(EXIT_ERROR)

    decisions, doomed, refusal = run.decide(src, scan, extra)
    res.kept = sum(1 for d in decisions if d.action == "keep" and not d.snap.virtual)
    if not doomed:
        return res
    if refusal:
        run.log(4, f"{src.name}: {len(doomed)} deletion(s) blocked: {refusal}")
        for d in doomed:
            run.log(6, f"{src.name}: blocked: {d.snap.entry} ({d.reason})")
        res.worse(EXIT_REFUSED)
        res.kept += len(doomed)
        return res
    if dry:
        for d in doomed:
            run.log(5, f"{src.name}: {would}delete {d.snap.path} ({d.reason})")
        res.deleted = len(doomed)
        return res

    # Right before deleting: list again, decide again, and keep only what both
    # passes agree on (same UUID); then re-check each one just before its delete.
    run.forget_pins()
    try:
        fresh = scan_snapshots(run.host, run.btrfs, run.snapdir, src.name, info["uuid"])
    except ParseError as e:
        run.log(3, f"{src.name}: {e}; no deletions")
        res.worse(EXIT_ERROR)
        return res
    _, fresh_doomed, refusal = run.decide(src, fresh)
    if refusal:
        run.log(4, f"{src.name}: deletions blocked on re-check: {refusal}")
        res.worse(EXIT_REFUSED)
        res.kept += len(doomed)
        return res
    fresh_uuids = {d.snap.uuid for d in fresh_doomed}
    for d in doomed:
        s = d.snap
        if s.uuid not in fresh_uuids:
            run.log(5, f"{src.name}: {s.entry} is no longer due after re-listing; kept")
            res.kept += 1
            continue
        # Pins are re-read before every deletion; a pin is protection, not a fault.
        run.forget_pins()
        if run.is_pinned(s.path):
            run.log(5, f"{src.name}: {s.entry} was pinned meanwhile; kept")
            res.kept += 1
            continue
        problem = recheck(run, s, info["uuid"])
        if problem:
            run.log(4, f"{src.name}: not deleting {s.path}: {problem}")
            res.worse(EXIT_REFUSED)
            res.kept += 1
            continue
        rc, _, err = run.btrfs.delete(SubvolPath(s.path))
        if rc != 0:
            run.log(3, f"{src.name}: deleting {s.path} failed (rc={rc}): {err.strip()}; stopping deletions")
            res.worse(EXIT_ERROR)
            break
        run.log(5, f"{src.name}: deleted {s.path} ({d.reason})")
        res.deleted += 1
    return res


def recheck(run: Run, snap: Snap, source_uuid: str) -> str | None:
    """Why SNAP must not be deleted after all (None = go ahead)."""
    problem = run.pins()[1]
    if problem:
        return problem
    if run.host.kind(snap.path) != "subvol":
        return "no longer a subvolume"
    try:
        info = run.btrfs.show(SubvolPath(snap.path))
        ro = run.btrfs.readonly(SubvolPath(snap.path))
    except ParseError as e:
        return str(e)
    if info["uuid"] != snap.uuid:
        return f"UUID changed ({snap.uuid} -> {info['uuid']})"
    if info["parent_uuid"] != source_uuid:
        return "Parent UUID changed"
    if not ro:
        return "no longer read-only"
    return None


def free_name(host: SnapshotHostPort, snapdir: str, name: str, wall: dt.datetime) -> str:
    """NAME.TS, or NAME.TS_N with N = highest present + 1 (btrbk)."""
    base = snapshot_name(name, wall)
    pattern = re.compile(re.escape(base) + r"(?:_([0-9]+))?")
    try:
        entries = host.listdir(snapdir)
    except FileNotFoundError:
        entries = []
    taken = [int(m.group(1) or 0) for m in map(pattern.fullmatch, entries) if m]
    return base if not taken else f"{base}_{max(taken) + 1}"


def create_snapshot(run: Run, src: Source, info: SubvolInfo, dest: str, entry: str, reason: str) -> bool:
    if run.host.kind(run.snapdir) == "missing":
        try:
            run.host.mkdir(run.snapdir, 0o700)
            run.log(5, f"created the snapshot directory {run.snapdir}")
        except OSError as e:
            run.log(3, f"cannot create {run.snapdir}: {e.strerror}")
            return False
    # With --exclude-caches the snapshot is taken writable (to drop its cache
    # folders), but only once it is recorded: if the run dies before it is
    # read-only, the next run finds it in the list and finishes it (a writable
    # snapshot is never "ours" otherwise, so it would stay forever).
    tracked = False
    if run.options.exclude_caches:
        try:
            run.list_add(UNFINISHED_FILE, entry)
            tracked = True
        except OSError as e:
            run.log(
                4,
                f"{src.name}: cannot record {entry} in {run.state_file(UNFINISHED_FILE)} "
                f"({err_text(e)}); this snapshot keeps its cache folders",
            )
    rc, _, err = run.btrfs.snapshot(SubvolPath(src.path), dest, readonly=not tracked)
    if rc != 0:
        run.log(3, f"{src.name}: creating {dest} failed (rc={rc}): {err.strip()}")
        if tracked and run.host.kind(dest) == "missing":
            try:
                run.list_remove(UNFINISHED_FILE, entry)
            except OSError:
                pass  # the next run sees it missing and forgets it
        return False
    run.log(5, f"{src.name}: created {entry} ({reason})")
    return seal(run, src, dest, entry, info["uuid"], created=True) == "sealed" if tracked else True


def ours_writable(run: Run, src: Source, dest: str, entry: str, source_uuid: str) -> tuple[str | None, bool | None]:
    """(problem, readonly): problem is why DEST is not a snapshot of SRC that
    this tool may change (None = it is: directly in the snapshot folder,
    named like SRC's snapshots, a subvolume with SRC's Parent UUID)."""
    if os.path.dirname(dest) != run.snapdir or not name_regex(src.name).fullmatch(entry):
        return f"not a snapshot name of {src.name} in {run.snapdir}", None
    kind = run.host.kind(dest)
    if kind != "subvol":
        return ("missing" if kind == "missing" else f"not a subvolume ({kind})"), None
    try:
        info = run.btrfs.show(SubvolPath(dest))  # host.kind said so
        ro = run.btrfs.readonly(SubvolPath(dest))
    except ParseError as e:
        return str(e), None
    other = other_parent(info, source_uuid)
    return (f"a {other}" if other else None), ro


def seal(
    run: Run, src: Source, dest: str, entry: str, source_uuid: str, created: bool
) -> Literal["sealed", "left", "error"]:
    """The second half of a snapshot taken writable: drop its cache folders
    (with --exclude-caches), make it read-only, check that it is, and take
    it off the unfinished list. CREATED: this run has just made it, so any
    failure still ends with it read-only (the snapshot is kept, its caches
    perhaps too). Otherwise it is a listed leftover of an interrupted run,
    changed only once ours_writable() proves it is ours. Returns "sealed",
    "left" (not ours: untouched, still listed) or "error"."""
    problem, ro = ours_writable(run, src, dest, entry, source_uuid)
    if problem:
        if not created:
            run.log(
                4,
                f"{src.name}: {entry} is listed in {run.state_file(UNFINISHED_FILE)} but is {problem}; "
                f"left alone - check it, then remove its line from that file",
            )
            return "left"
        run.log(
            3,
            f"{src.name}: cannot check {dest} after creating it: {problem}; its cache folders stay, "
            f"making it read-only",
        )
    elif ro is False and run.options.exclude_caches:
        drop_caches(run, src, dest, entry)
    if ro is not True:
        rc, _, err = run.btrfs.set_readonly(SubvolPath(dest))  # the snapshot this run checked or made
        if rc != 0:
            run.log(
                3,
                f"{src.name}: making {dest} read-only failed (rc={rc}): {err.strip()}; it stays listed in "
                f"{run.state_file(UNFINISHED_FILE)} and the next run tries again",
            )
            run.writable.add(entry)
            return "error"
        ro_state: bool | str
        try:
            ro_state = run.btrfs.readonly(SubvolPath(dest))
        except ParseError as e:
            ro_state = f"cannot check: {e}"
        if ro_state is not True:
            run.log(
                3,
                f"{src.name}: {dest} is not read-only after setting it ({ro_state}); it stays listed in "
                f"{run.state_file(UNFINISHED_FILE)} and the next run tries again",
            )
            run.writable.add(entry)
            return "error"
    try:
        run.list_remove(UNFINISHED_FILE, entry)
    except OSError as e:
        run.log(
            4,
            f"{src.name}: cannot update {run.state_file(UNFINISHED_FILE)} "
            f"({err_text(e)}); the next run finds {entry} read-only and forgets it",
        )
    # Read-only now: off the doctor's record too. Snapshot and prune runs
    # rewrite it from run.writable at the end; a `finish` run (as in purge)
    # writes no new record, so a stale line would read as a foreign entry.
    run.writable.discard(entry)
    if run.options.cmd == "finish":
        try:
            run.list_remove(WRITABLE_FILE, entry)
        except OSError as e:
            run.log(4, f"cannot update {run.state_file(WRITABLE_FILE)}: {err_text(e)}")
    return "sealed"


def report_walk(log: LogFn, src: Source, where: str, walk: CacheWalk, verb: str) -> None:
    """Log lines for a cache walk of SRC (WHERE: the snapshot or source)."""
    level = 3 if verb == "dropped" else 4
    for c in walk.folders:
        log(6, f"{src.name}: {verb} {c.rel} ({c.files} files, {format_bytes_human(c.size)})")
    for rel in walk.kept:
        log(6, f"{src.name}: kept {rel}: a cache folder that holds a skipped path")
    for rel, why in walk.problems[:20]:
        log(level, f"{src.name}: {where}: {rel}: {why}")
    if len(walk.problems) > 20:
        log(level, f"{src.name}: {where}: and {len(walk.problems) - 20} more problem(s)")


def drop_caches(run: Run, src: Source, dest: str, entry: str) -> None:
    """Delete the cache folders in the fresh writable snapshot DEST. Never
    fails the run: a folder that cannot be deleted (or a walk that breaks)
    is logged as an error and simply stays in the snapshot."""
    try:
        walk = run.host.walk_caches(dest, src.skips, "drop")
    except Exception as e:  # noqa: BLE001 - whatever breaks here, the snapshot must still end read-only
        run.log(
            3,
            f"{src.name}: dropping the cache folders from {entry} failed ({type(e).__name__}: {e}); "
            f"the snapshot keeps them",
        )
        return
    report_walk(run.log, src, entry, walk, "dropped")
    tail = (
        f"; {len(walk.problems)} problem(s): what could not be deleted stays in the snapshot" if walk.problems else ""
    )
    run.log(
        3 if walk.problems else 5,
        f"{src.name}: dropped {len(walk.folders)} cache folder(s) from {entry}: {walk.files} files, "
        f"{format_bytes_human(walk.size)} (apparent size), {walk.seconds:.1f} s{tail}",
    )


def preview_drop(run: Run, src: Source) -> None:
    """Dry run: what the new snapshot would lose, read from the source."""
    try:
        walk = run.host.walk_caches(src.path, src.skips, "count")
    except OSError as e:
        run.log(4, f"{src.name}: cannot look for cache folders in {src.path}: {e.strerror}")
        return
    report_walk(run.log, src, src.path, walk, "would drop")
    run.log(
        5,
        f"{src.name}: would drop {len(walk.folders)} cache folder(s) from the new snapshot: "
        f"{walk.files} files, {format_bytes_human(walk.size)} (apparent size)",
    )


def finish_unfinished(run: Run) -> int:
    """Before anything else (snapshot, prune, finish): the snapshots listed in
    the unfinished file are leftovers of a run that stopped between creating
    a writable snapshot and making it read-only. Only a listed entry that is
    proven ours (seal) is finished; a listed name that was never created is
    forgotten; anything else is left alone with a warning. Returns an exit
    status (EXIT_ERROR if finishing one failed)."""
    path = run.state_file(UNFINISHED_FILE)
    try:
        entries = run.read_list(UNFINISHED_FILE)
    except OSError as e:
        run.log(4, f"cannot read {path} ({err_text(e)}); interrupted snapshots are not finished")
        return EXIT_OK
    status = EXIT_OK
    infos: dict[str, str | None] = {}
    for entry in entries:
        src = next((s for s in run.options.sources if "/" not in entry and name_regex(s.name).fullmatch(entry)), None)
        if src is None:
            run.log(
                4,
                f"{entry!r} is listed in {path} but is no snapshot name of "
                f"{', '.join(s.name for s in run.options.sources)}; left alone",
            )
            continue
        dest = os.path.join(run.snapdir, entry)
        if run.host.kind(dest) == "missing":
            if run.options.dry_run:
                run.log(6, f"{src.name}: would forget {entry} (listed in {path}, never created)")
                continue
            run.log(6, f"{src.name}: {entry} (listed in {path}) was never created; forgotten")
            try:
                run.list_remove(UNFINISHED_FILE, entry)
            except OSError as e:
                run.log(4, f"cannot update {path}: {err_text(e)}")
            continue
        if src.name not in infos:
            try:
                infos[src.name] = run.source_info(src)["uuid"]
            except ParseError as e:
                infos[src.name] = None
                run.log(4, f"{src.name}: cannot read source {src.path}: {e}; its interrupted snapshots wait")
        source_uuid = infos[src.name]
        if source_uuid is None:
            continue
        if run.options.dry_run:
            run.log(5, f"{src.name}: would finish {entry}, left writable by an interrupted run")
            continue
        run.log(4, f"{src.name}: finishing {entry}, left writable by an interrupted run")
        if seal(run, src, dest, entry, source_uuid, created=False) == "error":
            status = EXIT_ERROR
    return status


def check_snapdir(host: SnapshotHostPort, snapdir: str) -> None:
    # A missing directory just holds no snapshots yet (snapshot creates it).
    if host.kind(snapdir) == "symlink":
        raise Usage(f"--snapdir {snapdir} is a symlink")


def locked(args: RunOptions, machine: Machine, body: Callable[[], int]) -> int:
    """BODY() under the run lock (EXIT_BUSY, BODY not run, when another run holds it)."""
    check_snapdir(machine.host, args.snapdir)
    if args.no_lock or args.dry_run:  # the caller holds it, or a dry run
        return body()
    release = machine.lock.acquire(args.lock)
    if release is None:
        machine.log(5, f"another run holds {args.lock}; skipped")
        return EXIT_BUSY
    try:
        return body()
    finally:
        release()


def cmd_snapshot_or_prune(args: RunOptions, machine: Machine, tz: dt.tzinfo, create: bool) -> int:
    return locked(args, machine, lambda: snapshot_or_prune(args, machine, tz, create))


def snapshot_or_prune(args: RunOptions, machine: Machine, tz: dt.tzinfo, create: bool) -> int:
    run = Run(args, machine, tz)
    log = machine.log
    status = finish_unfinished(run)
    results = [process_source(run, src, create) for src in args.sources]
    for r in results:
        if r.status == EXIT_ERROR or (r.status == EXIT_REFUSED and status == EXIT_OK):
            status = r.status
    parts: list[str] = []
    for r in results:
        what = f"created {r.created}" if r.created else ("no new snapshot" if create else "")
        bits = [b for b in (what, f"deleted {r.deleted}", f"kept {r.kept}", r.note) if b]
        parts.append(f"{r.name}: " + ", ".join(bits))
    word = {EXIT_OK: "ok", EXIT_REFUSED: "blocked by a safety check", EXIT_ERROR: "error"}[status]
    log(
        5 if status == EXIT_OK else 4 if status == EXIT_REFUSED else 3,
        f"{'dry run ' if args.dry_run else ''}{'snapshot' if create else 'prune'} summary: "
        + "; ".join(parts)
        + f"; result: {word}",
    )
    if not args.dry_run:
        run.record_writable()
    if status == EXIT_OK and not args.dry_run:
        try:
            run.record_success()
        except OSError as e:
            log(4, f"cannot record the run in {args.state_dir}: {e.strerror}")
    return status


def cmd_list(args: RunOptions, machine: Machine, tz: dt.tzinfo) -> tuple[int, Listing]:
    """Every snapshot of each source with its decision (changes nothing)."""
    run = Run(args, machine, tz)
    log = machine.log
    out: list[SourceListing] = []
    status = EXIT_OK
    try:
        unfinished = set(run.read_list(UNFINISHED_FILE))
    except OSError:
        unfinished = set()
    for src in args.sources:
        policy = format_policy(src.policy)
        try:
            info = run.source_info(src)
        except ParseError as e:
            log(3, f"{src.name}: cannot read source {src.path}: {e}")
            out.append(SourceListing(src.name, src.path, policy, error=str(e)))
            status = EXIT_ERROR
            continue
        try:
            scan = scan_snapshots(machine.host, run.btrfs, run.snapdir, src.name, info["uuid"])
        except ParseError as e:
            log(3, f"{src.name}: {e}")
            out.append(SourceListing(src.name, src.path, policy, error=str(e)))
            status = EXIT_ERROR
            continue
        decisions, doomed, refusal = run.decide(src, scan)
        newest = newest_of(scan.owned)
        ignored: list[tuple[str, str]] = []
        writable = set(scan.writable)
        for p, r in scan.ignored:
            entry_name = os.path.basename(p)
            if p in writable and entry_name in unfinished:
                problem, _ = ours_writable(run, src, p, entry_name, info["uuid"])
                r += (
                    ", left by an interrupted run: the next run finishes it"
                    if problem is None
                    else f", listed as left by an interrupted run but {problem}: left alone"
                )
            ignored.append((p, r))
        out.append(
            SourceListing(
                src.name,
                src.path,
                policy,
                uuid=info["uuid"],
                generation=info["generation"],
                changed=newest is None or info["generation"] > newest.cgen,
                decisions=tuple(decisions),
                ignored=tuple(ignored),
                unverifiable=tuple(scan.unverifiable),
                deletions_blocked=refusal,
            )
        )
    return status, Listing(run.snapdir, int(run.now), tuple(out))


def cmd_finish(args: RunOptions, machine: Machine, tz: dt.tzinfo) -> int:
    """Only the recovery of interrupted snapshots (btrfs-snapshot purge runs it
    first, so they become ours and are deleted too)."""
    status = locked(args, machine, lambda: finish_unfinished(Run(args, machine, tz)))
    if status == EXIT_BUSY:
        return status
    machine.log(
        5 if status == EXIT_OK else 3,
        f"{'dry run ' if args.dry_run else ''}finish summary: result: {'ok' if status == EXIT_OK else 'error'}",
    )
    return status


def cmd_caches(args: RunOptions, machine: Machine, tz: dt.tzinfo) -> tuple[int, list[SourceCaches]]:
    """What --exclude-caches drops from each source's next snapshot (read from
    the sources; changes nothing)."""
    run = Run(args, machine, tz)
    out: list[SourceCaches] = []
    status = EXIT_OK
    for src in args.sources:
        skips = tuple(src.skips)
        try:
            run.source_info(src)
            walk = machine.host.walk_caches(src.path, src.skips, "count")
        except (ParseError, OSError) as e:
            error = err_text(e)
            machine.log(3, f"{src.name}: cannot read source {src.path}: {error}")
            out.append(SourceCaches(src.name, src.path, skips, error=error))
            status = EXIT_ERROR
            continue
        out.append(SourceCaches(src.name, src.path, skips, walk=walk))
    return status, out


def cmd_latest(args: LatestQuery, machine: Machine) -> tuple[int, str | None]:
    """The path of the newest snapshot of NAME (None, with EXIT_ERROR, when there is none)."""
    check_snapdir(machine.host, args.snapdir)
    log = machine.log
    source_uuid = None
    if args.source_path:
        try:
            source_uuid = machine.btrfs.show(SubvolPath(os.path.normpath(args.source_path)))["uuid"]
        except ParseError as e:
            log(3, f"cannot read {args.source_path}: {e}")
            return EXIT_ERROR, None
    try:
        scan = scan_snapshots(machine.host, machine.btrfs, os.path.normpath(args.snapdir), args.name, source_uuid)
    except ParseError as e:
        log(3, str(e))
        return EXIT_ERROR, None
    for path, why in scan.unverifiable:
        log(4, f"cannot verify {path}: {why}")
    newest = newest_of(scan.owned)
    if newest is None:
        log(4, f"no snapshot of {args.name} in {args.snapdir}")
        return EXIT_ERROR, None
    return EXIT_OK, newest.path


def scan_snapshots(host: SnapshotHostPort, btrfs: BtrfsPort, snapdir: str, name: str, source_uuid: str | None) -> Scan:
    """Owned snapshots of one source. source_uuid None = skip the parent check (latest)."""
    owned: list[Snap] = []
    ignored: list[tuple[str, str]] = []
    unverifiable: list[tuple[str, str]] = []
    writable: list[str] = []
    try:
        entries = host.listdir(snapdir)
    except FileNotFoundError:
        return Scan(owned, ignored, unverifiable)
    except OSError as e:
        raise ParseError(f"cannot list {snapdir}: {e.strerror}") from None
    pattern = name_regex(name)
    for entry in entries:
        if not pattern.fullmatch(entry):
            continue
        path = os.path.join(snapdir, entry)
        parsed = parse_snapshot_name(name, entry)
        if parsed is None:
            ignored.append((path, "invalid date in the name"))
            continue
        kind = host.kind(path)
        if kind != "subvol":
            ignored.append((path, "not a subvolume" if kind != "symlink" else "symlink"))
            continue
        try:
            info = btrfs.show(SubvolPath(path))  # host.kind said so
            ro = btrfs.readonly(SubvolPath(path))
        except ParseError as e:
            unverifiable.append((path, str(e)))
            continue
        if not ro:
            ignored.append((path, "not read-only"))
            writable.append(path)
            continue
        other = other_parent(info, source_uuid) if source_uuid is not None else None
        if other:
            ignored.append((path, other))
            continue
        owned.append(Snap(entry=entry, wall=parsed[0], seq=parsed[1], path=path, uuid=info["uuid"], cgen=info["cgen"]))
    return Scan(owned, ignored, unverifiable, writable)

"""btrfs-snapshot-core's commands: one Run per invocation (clock guards,
state file, creation, sealing of writable snapshots, deletions under the
circuit breaker), the run lock, the argument parser and main().
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from typing import Any
from typing import Literal
import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import zoneinfo
from btrfs.snapshot.adapters import (
    Btrfs,
    CacheWalk,
    Host,
    Scan,
    apply_safeguards,
    is_pinned,
    local_tz,
    scan_snapshots,
    unexplained_deletions,
)
from btrfs.snapshot.domain import (
    CLOCK_SLACK,
    DEFAULT_CONFIRM_HINT,
    DEFAULT_LIST_HINT,
    DEFAULT_LOCK,
    DEFAULT_PIN_FILE,
    DEFAULT_STATE_DIR,
    Decision,
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    LastRun,
    NAME_RE,
    NTP_GRACE,
    ParseError,
    SAFETY_NET,
    STATE_FILE,
    Snap,
    Source,
    UNFINISHED_FILE,
    UUID_RE,
    WRITABLE_FILE,
    SubvolInfo,
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
    parse_skip,
    parse_snapshot_name,
    parse_source,
    parse_state,
    plan,
    require_absolute,
    snapshot_name,
    wall_to_epoch,
)
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.locks import take_lock
from cst_core.domain.errors import Usage
from cst_core.domain.files import checked_path
from cst_core.domain.values import format_bytes_human

LOG_STREAM = sys.stdout


def log(level: int, msg: str) -> None:
    journal_log(level, msg, tag="btrfs-snapshot", stream=LOG_STREAM)


class Run:
    """State of one snapshot/prune/list invocation."""

    def __init__(self, args: argparse.Namespace, host: Host) -> None:
        self.args = args
        self.host = host
        self.btrfs = Btrfs(host)
        self.tz = local_tz(args.tz)
        self.now = host.time()
        self.boot_id, self.boottime = host.boot()
        self.snapdir = os.path.normpath(args.snapdir)
        self._ntp: bool | None = None
        self._pins: tuple[set[str], str | None] | None = None
        self._last_run: tuple[LastRun | None, str | None] | None = None
        self.writable: set[str] = set()  # writable entries named like ours, seen by this run's scans
        self.scanned_all = True  # False: a source could not be scanned (keep the old record)

    # Lists in the state directory ----------------------------------------------
    def state_file(self, name: str) -> str:
        return os.path.join(self.args.state_dir, name)

    def read_list(self, name: str) -> list[str]:
        """The entries (one per line) of STATE/name; [] if it does not exist.
        OSError if it cannot be read. Bytes that are not UTF-8 become U+FFFD
        (such a line names no snapshot, so it is left alone with a warning)."""
        try:
            with open(self.state_file(name), encoding="utf-8", errors="replace") as f:
                return [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def write_list(self, name: str, entries: Sequence[str], sync_dir: bool = True) -> None:
        """Replace STATE/name with ENTRIES, durably (fsync of the file and,
        with SYNC_DIR, the folder); removed when ENTRIES is empty.
        World-readable: cst doctor runs as the user."""
        path = self.state_file(name)
        if not entries:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            return
        os.makedirs(self.args.state_dir, mode=0o755, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW, 0o644)
        os.fchmod(fd, 0o644)  # whatever the umask (sudo keeps a user's 077)
        with os.fdopen(fd, "w") as f:
            f.write("".join(f"{e}\n" for e in entries))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if not sync_dir:
            return
        dfd = os.open(self.args.state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

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
            log(4, f"cannot write {self.state_file(WRITABLE_FILE)}: {e.strerror}")

    # Clock guards -----------------------------------------------------------
    def ntp_problem(self) -> str | None:
        if self.args.assume_clock_ok:
            return None
        if self._ntp is None:
            rc, out, _ = self.host.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
            self._ntp = rc == 0 and out.strip() == "yes"
        return None if self._ntp else "the system clock is not NTP-synchronized (timedatectl NTPSynchronized != yes)"

    def last_run(self) -> tuple[LastRun | None, str | None]:
        """(LastRun or None, problem or None)."""
        if self._last_run is None:
            try:
                with open(self.state_file(STATE_FILE)) as f:
                    text = f.read().strip()
                record = parse_state(text)
                self._last_run = (
                    (record, None)
                    if record
                    else (
                        None,
                        f"unreadable state {self.state_file(STATE_FILE)} ({text[:60]!r}); "
                        f"remove it if the clock is right",
                    )
                )
            except FileNotFoundError:
                self._last_run = (None, None)
            except (OSError, UnicodeDecodeError) as e:
                self._last_run = (None, f"cannot read {self.state_file(STATE_FILE)}: {e}")
        return self._last_run

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
                    f"check it with '{self.args.list_hint}' and delete it with "
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
                    f"from a clock that ran ahead: check it with '{self.args.list_hint}' and delete it "
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

    def pins(self) -> tuple[set[str], str | None]:
        """(set of pinned paths, problem or None)."""
        if self._pins is None:
            pins: set[str] = set()
            try:
                with open(self.args.pin_file) as f:
                    for raw in f:
                        line = raw.strip()
                        if line:
                            pins.add(os.path.normpath(line))
                            pins.add(os.path.realpath(line))
                self._pins = (pins, None)
            except FileNotFoundError:
                self._pins = (pins, None)
            except (OSError, UnicodeDecodeError) as e:
                self._pins = (pins, f"cannot read the pin file {self.args.pin_file}: {e}")
        return self._pins

    def record_success(self) -> None:
        """Store this run as the last successful one - only with a trusted clock,
        and never moving the record backwards (it guards against a clock that
        later runs behind)."""
        if self.ntp_problem():
            log(6, "the clock is not NTP-synchronized; this run is not recorded as the last successful one")
            return
        last, problem = self.last_run()
        if problem:
            log(4, f"not recording this run: {problem}")
            return
        if last is not None and self.now < last.epoch:
            if self.now < last.epoch - CLOCK_SLACK:
                log(
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
        return self.btrfs.show(src.path)

    def decide(
        self, src: Source, scan: Scan, extra: Sequence[Snap] = ()
    ) -> tuple[list[Decision], list[Decision], str | None]:
        """Decisions after policy and safeguards, the deletion candidates, and
        why deleting is refused (None = allowed)."""
        snaps = list(scan.owned) + list(extra)
        pins, pin_problem = self.pins()
        decisions = apply_safeguards(plan(snaps, self.now, src.policy, self.tz), self.now, self.tz, pins)
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
            if refusal is None and not self.args.allow_mass_delete:
                refusal = self.breaker(src, decisions, snaps, pins, doomed, owned)
        return decisions, doomed, refusal

    def breaker(
        self,
        src: Source,
        decisions: Sequence[Decision],
        snaps: Sequence[Snap],
        pins: set[str],
        doomed: Sequence[Decision],
        owned: int,
    ) -> str | None:
        last, _ = self.last_run()
        since = last.epoch if last is not None else None
        jump = self.clock_jump()
        ref = self.now - jump if jump > CLOCK_SLACK else None
        unexplained = unexplained_deletions(decisions, snaps, src.policy, self.tz, pins, since, ref)
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
            f"(limit {mass_delete_limit(owned)}); check with '{self.args.list_hint}', "
            f"then delete them with '{self.args.confirm_hint}'"
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
    args = run.args
    res = Result(src.name)
    dry = args.dry_run
    would = "would " if dry else ""
    try:
        info = run.source_info(src)
    except ParseError as e:
        log(3, f"{src.name}: cannot read source {src.path}: {e}; nothing done")
        res.worse(EXIT_ERROR)
        res.note = "source unreadable"
        run.scanned_all = False
        return res
    try:
        scan = scan_snapshots(run.host, run.btrfs, run.snapdir, src.name, info["uuid"])
    except ParseError as e:
        log(3, f"{src.name}: {e}; nothing done")
        res.worse(EXIT_ERROR)
        run.scanned_all = False
        return res
    note_writable(run, scan)
    for path, why in scan.unverifiable:
        log(4, f"{src.name}: cannot verify {path}: {why}")
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
            log(6, f"{src.name}: unchanged since {newest.entry}; no new snapshot")
        else:
            problem, notes = run.creation_check(newest)
            if problem:
                log(4, f"{src.name}: not creating a snapshot: {problem}")
                res.worse(EXIT_REFUSED)
                res.note = "clock"
            else:
                for level, note in notes:
                    log(level, f"{src.name}: {note}")
                wall = epoch_to_wall(run.now, run.tz).replace(second=0, microsecond=0)
                entry = free_name(run.host, run.snapdir, src.name, wall)
                dest = os.path.join(run.snapdir, entry)
                if dry:
                    log(5, f"{src.name}: would create {dest} ({reason})")
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
                        log(3, f"{src.name}: {e}; no deletions")
                        res.worse(EXIT_ERROR)
                        run.scanned_all = False
                        return res
                    note_writable(run, scan)
                    if not any(s.entry == entry for s in scan.owned):
                        log(3, f"{src.name}: created {dest} but it does not pass the ownership check")
                        res.worse(EXIT_ERROR)
                else:
                    res.worse(EXIT_ERROR)

    decisions, doomed, refusal = run.decide(src, scan, extra)
    res.kept = sum(1 for d in decisions if d.action == "keep" and not d.snap.virtual)
    if not doomed:
        return res
    if refusal:
        log(4, f"{src.name}: {len(doomed)} deletion(s) blocked: {refusal}")
        for d in doomed:
            log(6, f"{src.name}: blocked: {d.snap.entry} ({d.reason})")
        res.worse(EXIT_REFUSED)
        res.kept += len(doomed)
        return res
    if dry:
        for d in doomed:
            log(5, f"{src.name}: {would}delete {d.snap.path} ({d.reason})")
        res.deleted = len(doomed)
        return res

    # Right before deleting: list again, decide again, and keep only what both
    # passes agree on (same UUID); then re-check each one just before its delete.
    run._pins = None
    try:
        fresh = scan_snapshots(run.host, run.btrfs, run.snapdir, src.name, info["uuid"])
    except ParseError as e:
        log(3, f"{src.name}: {e}; no deletions")
        res.worse(EXIT_ERROR)
        return res
    _, fresh_doomed, refusal = run.decide(src, fresh)
    if refusal:
        log(4, f"{src.name}: deletions blocked on re-check: {refusal}")
        res.worse(EXIT_REFUSED)
        res.kept += len(doomed)
        return res
    fresh_uuids = {d.snap.uuid for d in fresh_doomed}
    for d in doomed:
        s = d.snap
        if s.uuid not in fresh_uuids:
            log(5, f"{src.name}: {s.entry} is no longer due after re-listing; kept")
            res.kept += 1
            continue
        # Pins are re-read before every deletion; a pin is protection, not a fault.
        run._pins = None
        if is_pinned(s.path, run.pins()[0]):
            log(5, f"{src.name}: {s.entry} was pinned meanwhile; kept")
            res.kept += 1
            continue
        problem = recheck(run, s, info["uuid"])
        if problem:
            log(4, f"{src.name}: not deleting {s.path}: {problem}")
            res.worse(EXIT_REFUSED)
            res.kept += 1
            continue
        rc, _, err = run.btrfs.delete(s.path)
        if rc != 0:
            log(3, f"{src.name}: deleting {s.path} failed (rc={rc}): {err.strip()}; stopping deletions")
            res.worse(EXIT_ERROR)
            break
        log(5, f"{src.name}: deleted {s.path} ({d.reason})")
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
        info = run.btrfs.show(snap.path)
        ro = run.btrfs.readonly(snap.path)
    except ParseError as e:
        return str(e)
    if info["uuid"] != snap.uuid:
        return f"UUID changed ({snap.uuid} -> {info['uuid']})"
    if info["parent_uuid"] != source_uuid:
        return "Parent UUID changed"
    if not ro:
        return "no longer read-only"
    return None


def free_name(host: Host, snapdir: str, name: str, wall: dt.datetime) -> str:
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
            log(5, f"created the snapshot directory {run.snapdir}")
        except OSError as e:
            log(3, f"cannot create {run.snapdir}: {e.strerror}")
            return False
    # With --exclude-caches the snapshot is taken writable (to drop its cache
    # folders), but only once it is recorded: if the run dies before it is
    # read-only, the next run finds it in the list and finishes it (a writable
    # snapshot is never "ours" otherwise, so it would stay forever).
    tracked = False
    if run.args.exclude_caches:
        try:
            run.list_add(UNFINISHED_FILE, entry)
            tracked = True
        except OSError as e:
            log(
                4,
                f"{src.name}: cannot record {entry} in {run.state_file(UNFINISHED_FILE)} "
                f"({err_text(e)}); this snapshot keeps its cache folders",
            )
    rc, _, err = run.btrfs.snapshot(src.path, dest, readonly=not tracked)
    if rc != 0:
        log(3, f"{src.name}: creating {dest} failed (rc={rc}): {err.strip()}")
        if tracked and run.host.kind(dest) == "missing":
            try:
                run.list_remove(UNFINISHED_FILE, entry)
            except OSError:
                pass  # the next run sees it missing and forgets it
        return False
    log(5, f"{src.name}: created {entry} ({reason})")
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
        info = run.btrfs.show(dest)
        ro = run.btrfs.readonly(dest)
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
            log(
                4,
                f"{src.name}: {entry} is listed in {run.state_file(UNFINISHED_FILE)} but is {problem}; "
                f"left alone - check it, then remove its line from that file",
            )
            return "left"
        log(
            3,
            f"{src.name}: cannot check {dest} after creating it: {problem}; its cache folders stay, "
            f"making it read-only",
        )
    elif ro is False and run.args.exclude_caches:
        drop_caches(run, src, dest, entry)
    if ro is not True:
        rc, _, err = run.btrfs.set_readonly(dest)
        if rc != 0:
            log(
                3,
                f"{src.name}: making {dest} read-only failed (rc={rc}): {err.strip()}; it stays listed in "
                f"{run.state_file(UNFINISHED_FILE)} and the next run tries again",
            )
            run.writable.add(entry)
            return "error"
        ro_state: bool | str
        try:
            ro_state = run.btrfs.readonly(dest)
        except ParseError as e:
            ro_state = f"cannot check: {e}"
        if ro_state is not True:
            log(
                3,
                f"{src.name}: {dest} is not read-only after setting it ({ro_state}); it stays listed in "
                f"{run.state_file(UNFINISHED_FILE)} and the next run tries again",
            )
            run.writable.add(entry)
            return "error"
    try:
        run.list_remove(UNFINISHED_FILE, entry)
    except OSError as e:
        log(
            4,
            f"{src.name}: cannot update {run.state_file(UNFINISHED_FILE)} "
            f"({err_text(e)}); the next run finds {entry} read-only and forgets it",
        )
    # Read-only now: off the doctor's record too. Snapshot and prune runs
    # rewrite it from run.writable at the end; a `finish` run (as in purge)
    # writes no new record, so a stale line would read as a foreign entry.
    run.writable.discard(entry)
    if run.args.cmd == "finish":
        try:
            run.list_remove(WRITABLE_FILE, entry)
        except OSError as e:
            log(4, f"cannot update {run.state_file(WRITABLE_FILE)}: {err_text(e)}")
    return "sealed"


def report_walk(src: Source, where: str, walk: CacheWalk, verb: str) -> None:
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
        log(
            3,
            f"{src.name}: dropping the cache folders from {entry} failed ({type(e).__name__}: {e}); "
            f"the snapshot keeps them",
        )
        return
    report_walk(src, entry, walk, "dropped")
    tail = (
        f"; {len(walk.problems)} problem(s): what could not be deleted stays in the snapshot" if walk.problems else ""
    )
    log(
        3 if walk.problems else 5,
        f"{src.name}: dropped {len(walk.folders)} cache folder(s) from {entry}: {walk.files} files, "
        f"{format_bytes_human(walk.size)} (apparent size), {walk.seconds:.1f} s{tail}",
    )


def preview_drop(run: Run, src: Source) -> None:
    """Dry run: what the new snapshot would lose, read from the source."""
    try:
        walk = run.host.walk_caches(src.path, src.skips, "count")
    except OSError as e:
        log(4, f"{src.name}: cannot look for cache folders in {src.path}: {e.strerror}")
        return
    report_walk(src, src.path, walk, "would drop")
    log(
        5,
        f"{src.name}: would drop {len(walk.folders)} cache folder(s) from the new snapshot: "
        f"{walk.files} files, {format_bytes_human(walk.size)} (apparent size)",
    )


def note_writable(run: Run, scan: Scan) -> None:
    run.writable.update(os.path.basename(p) for p in scan.writable)


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
        log(4, f"cannot read {path} ({err_text(e)}); interrupted snapshots are not finished")
        return EXIT_OK
    status = EXIT_OK
    infos: dict[str, str | None] = {}
    for entry in entries:
        src = next((s for s in run.args.sources if "/" not in entry and name_regex(s.name).fullmatch(entry)), None)
        if src is None:
            log(
                4,
                f"{entry!r} is listed in {path} but is no snapshot name of "
                f"{', '.join(s.name for s in run.args.sources)}; left alone",
            )
            continue
        dest = os.path.join(run.snapdir, entry)
        if run.host.kind(dest) == "missing":
            if run.args.dry_run:
                log(6, f"{src.name}: would forget {entry} (listed in {path}, never created)")
                continue
            log(6, f"{src.name}: {entry} (listed in {path}) was never created; forgotten")
            try:
                run.list_remove(UNFINISHED_FILE, entry)
            except OSError as e:
                log(4, f"cannot update {path}: {err_text(e)}")
            continue
        if src.name not in infos:
            try:
                infos[src.name] = run.source_info(src)["uuid"]
            except ParseError as e:
                infos[src.name] = None
                log(4, f"{src.name}: cannot read source {src.path}: {e}; its interrupted snapshots wait")
        source_uuid = infos[src.name]
        if source_uuid is None:
            continue
        if run.args.dry_run:
            log(5, f"{src.name}: would finish {entry}, left writable by an interrupted run")
            continue
        log(4, f"{src.name}: finishing {entry}, left writable by an interrupted run")
        if seal(run, src, dest, entry, source_uuid, created=False) == "error":
            status = EXIT_ERROR
    return status


def check_snapdir(host: Host, snapdir: str) -> None:
    # A missing directory just holds no snapshots yet (snapshot creates it).
    if host.kind(snapdir) == "symlink":
        raise Usage(f"--snapdir {snapdir} is a symlink")


def locked(args: argparse.Namespace, host: Host, body: Callable[[], int]) -> int:
    """BODY() under the run lock (EXIT_BUSY, BODY not run, when another run holds it)."""
    check_snapdir(host, args.snapdir)
    # -1: no lock wanted (the caller holds it, or a dry run); None: busy.
    fd = -1 if args.no_lock or args.dry_run else take_lock(args.lock, wait=False)
    if fd is None:
        log(5, f"another run holds {args.lock}; skipped")
        return EXIT_BUSY
    try:
        return body()
    finally:
        if fd >= 0:
            os.close(fd)


def cmd_snapshot_or_prune(args: argparse.Namespace, host: Host, create: bool) -> int:
    return locked(args, host, lambda: snapshot_or_prune(args, host, create))


def snapshot_or_prune(args: argparse.Namespace, host: Host, create: bool) -> int:
    run = Run(args, host)
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


def cmd_list(args: argparse.Namespace, host: Host) -> int:
    run = Run(args, host)
    out: list[dict[str, Any]] = []
    status = EXIT_OK
    try:
        unfinished = set(run.read_list(UNFINISHED_FILE))
    except OSError:
        unfinished = set()
    for src in args.sources:
        entry: dict[str, Any] = {"name": src.name, "source": src.path, "policy": format_policy(src.policy)}
        try:
            info = run.source_info(src)
        except ParseError as e:
            log(3, f"{src.name}: cannot read source {src.path}: {e}")
            entry["error"] = str(e)
            out.append(entry)
            status = EXIT_ERROR
            continue
        try:
            scan = scan_snapshots(host, run.btrfs, run.snapdir, src.name, info["uuid"])
        except ParseError as e:
            log(3, f"{src.name}: {e}")
            entry["error"] = str(e)
            out.append(entry)
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
        entry.update(
            {
                "uuid": info["uuid"],
                "generation": info["generation"],
                "changed": newest is None or info["generation"] > newest.cgen,
                "snapshots": [
                    {
                        "path": d.snap.path,
                        "entry": d.snap.entry,
                        "time": d.snap.wall.strftime("%Y-%m-%dT%H:%M"),
                        "uuid": d.snap.uuid,
                        "gen_at_creation": d.snap.cgen,
                        "action": d.action,
                        "reason": d.reason,
                    }
                    for d in decisions
                ],
                "ignored": [{"path": p, "reason": r} for p, r in ignored],
                "unverifiable": [{"path": p, "reason": r} for p, r in scan.unverifiable],
                "deletions_blocked": refusal,
            }
        )
        out.append(entry)
    if args.json:
        json.dump({"snapdir": run.snapdir, "now": int(run.now), "sources": out}, sys.stdout, indent=2)
        print()
        return status
    for e in out:
        if "error" in e:
            print(f"{e['name']}: {e['source']}: ERROR {e['error']}")
            continue
        print(
            f"{e['name']}: {e['source']} (generation {e['generation']}, "
            f"{'changed' if e['changed'] else 'unchanged'} since the newest snapshot), policy '{e['policy']}'"
        )
        for s in e["snapshots"]:
            print(f"  {s['action'].upper():7} {s['entry']:<32} {s['reason']}")
        for s in e["ignored"]:
            print(f"  {'IGNORED':7} {os.path.basename(s['path']):<32} {s['reason']}")
        for s in e["unverifiable"]:
            print(f"  {'UNKNOWN':7} {os.path.basename(s['path']):<32} {s['reason']}")
        n_del = sum(1 for s in e["snapshots"] if s["action"] == "delete")
        print(
            f"  {len(e['snapshots'])} snapshot(s), {n_del} due for deletion"
            + (f"; deletions would be blocked: {e['deletions_blocked']}" if e["deletions_blocked"] else "")
        )
    return status


def cmd_finish(args: argparse.Namespace, host: Host) -> int:
    """Only the recovery of interrupted snapshots (btrfs-snapshot purge runs it
    first, so they become ours and are deleted too)."""
    status = locked(args, host, lambda: finish_unfinished(Run(args, host)))
    if status == EXIT_BUSY:
        return status
    log(
        5 if status == EXIT_OK else 3,
        f"{'dry run ' if args.dry_run else ''}finish summary: result: {'ok' if status == EXIT_OK else 'error'}",
    )
    return status


def cmd_caches(args: argparse.Namespace, host: Host) -> int:
    """What --exclude-caches drops from each source's next snapshot (read from
    the sources; changes nothing)."""
    run = Run(args, host)
    out: list[dict[str, Any]] = []
    status = EXIT_OK
    for src in args.sources:
        entry: dict[str, Any] = {"name": src.name, "source": src.path, "skips": list(src.skips)}
        try:
            run.source_info(src)
            walk = host.walk_caches(src.path, src.skips, "count")
        except (ParseError, OSError) as e:
            entry["error"] = err_text(e)
            log(3, f"{src.name}: cannot read source {src.path}: {entry['error']}")
            out.append(entry)
            status = EXIT_ERROR
            continue
        entry.update(
            {
                "folders": [{"path": c.rel, "files": c.files, "bytes": c.size} for c in walk.folders],
                "files": walk.files,
                "bytes": walk.size,
                "kept": walk.kept,
                "skipped": walk.skipped,
                "problems": [{"path": p, "reason": r} for p, r in walk.problems],
                "seconds": round(walk.seconds, 2),
            }
        )
        out.append(entry)
    if args.json:
        json.dump({"sources": out}, sys.stdout, indent=2)
        print()
        return status
    for e in out:
        if "error" in e:
            print(f"{e['name']}: {e['source']}: ERROR {e['error']}")
            continue
        print(
            f"{e['name']}: {e['source']}: {len(e['folders'])} cache folder(s), {e['files']} files, "
            f"{format_bytes_human(e['bytes'])} (apparent size); walk {e['seconds']:.1f} s"
        )
        for c in e["folders"]:
            print(f"  DROP   {c['path']:<56} {c['files']:>9} files {format_bytes_human(c['bytes']):>10}")
        for rel in e["kept"]:
            print(f"  KEEP   {rel}  (a cache folder that holds a skipped path)")
        for rel in e["skipped"]:
            print(f"  SKIP   {rel}  (skip list: kept whole)")
        for p in e["problems"]:
            print(f"  ERROR  {p['path']}: {p['reason']}")
    return status


def cmd_latest(args: argparse.Namespace, host: Host) -> int:
    check_snapdir(host, args.snapdir)
    btrfs = Btrfs(host)
    source_uuid = None
    if args.source_path:
        try:
            source_uuid = btrfs.show(os.path.normpath(args.source_path))["uuid"]
        except ParseError as e:
            log(3, f"cannot read {args.source_path}: {e}")
            return EXIT_ERROR
    try:
        scan = scan_snapshots(host, btrfs, os.path.normpath(args.snapdir), args.name, source_uuid)
    except ParseError as e:
        log(3, str(e))
        return EXIT_ERROR
    for path, why in scan.unverifiable:
        log(4, f"cannot verify {path}: {why}")
    newest = newest_of(scan.owned)
    if newest is None:
        log(4, f"no snapshot of {args.name} in {args.snapdir}")
        return EXIT_ERROR
    print(newest.path)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="btrfs-snapshot-core", description="Local btrfs snapshots with btrbk-compatible retention."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser, policy: bool = True) -> None:
        sp.add_argument("--snapdir", required=True, help="directory holding the snapshots")
        sp.add_argument(
            "--source",
            action="append",
            required=True,
            dest="sources",
            metavar="NAME=PATH:POLICY",
            help="snapshot name, source subvolume and retention, e.g. home=/mnt/top/home:'72h 7d'",
        )
        sp.add_argument("--tz", help="time zone of the names and the retention (default: local; for tests)")
        sp.add_argument("--pin-file", default=DEFAULT_PIN_FILE, help=f"pinned paths (default {DEFAULT_PIN_FILE})")
        sp.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help=f"default {DEFAULT_STATE_DIR}")
        sp.add_argument(
            "--assume-clock-ok",
            action="store_true",
            help="skip the NTP-synchronized check (tests); the other clock guards stay",
        )
        # A caller that builds the --source arguments itself (btrfs-snapshot)
        # names its own runnable commands in the messages instead of ours.
        sp.add_argument(
            "--list-hint",
            default=DEFAULT_LIST_HINT,
            metavar="COMMAND",
            help="command named in messages to review the decisions",
        )
        sp.add_argument(
            "--confirm-hint",
            default=DEFAULT_CONFIRM_HINT,
            metavar="COMMAND",
            help="command named in messages to confirm a mass deletion",
        )

    def caches(sp: argparse.ArgumentParser, exclude: bool = True) -> None:
        if exclude:
            sp.add_argument(
                "--exclude-caches",
                action="store_true",
                help="take new snapshots writable, drop their CACHEDIR.TAG folders, then make them read-only",
            )
        sp.add_argument(
            "--caches-skip",
            action="append",
            default=[],
            metavar="NAME=REL",
            help="a path in source NAME (relative to its root, '.' = all of it) kept whole: "
            "never entered by the cache walk",
        )

    for name, helptext in (
        ("snapshot", "create changed snapshots, then prune"),
        ("prune", "prune only"),
        ("finish", "only finish snapshots an interrupted run left writable"),
    ):
        sp = sub.add_parser(name, help=helptext)
        common(sp)
        sp.add_argument("--dry-run", action="store_true", help="show what would be created/deleted")
        if name != "finish":
            sp.add_argument("--allow-mass-delete", action="store_true", help="disable the circuit breaker")
        sp.add_argument("--lock", default=DEFAULT_LOCK, type=checked_path, help=f"lock file (default {DEFAULT_LOCK})")
        sp.add_argument("--no-lock", action="store_true", help="the caller already holds the lock")
        caches(sp)
        if name == "snapshot":
            sp.add_argument("--force", action="store_true", help="create even if unchanged")
    sp = sub.add_parser("list", help="list snapshots with the retention decision")
    common(sp)
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("caches", help="the cache folders --exclude-caches would drop from the next snapshots")
    common(sp)
    caches(sp, exclude=False)
    sp.add_argument("--json", action="store_true")
    sp = sub.add_parser("latest", help="print the newest snapshot of NAME")
    sp.add_argument("--snapdir", required=True)
    sp.add_argument("--name", required=True)
    sp.add_argument("--source-path", help="also require the snapshot's Parent UUID to be this subvolume's UUID")
    return p


def main(argv: Sequence[str] | None = None, host: Host | None = None) -> int:
    global LOG_STREAM
    host = host or Host()
    parser = build_parser()
    args = parser.parse_args(argv)
    LOG_STREAM = sys.stderr if args.cmd in ("latest", "caches") or getattr(args, "json", False) else sys.stdout
    for attr in ("force", "allow_mass_delete", "dry_run", "no_lock", "exclude_caches"):
        if not hasattr(args, attr):
            setattr(args, attr, False)
    for attr, default in (("list_hint", DEFAULT_LIST_HINT), ("confirm_hint", DEFAULT_CONFIRM_HINT)):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    try:
        require_absolute("--snapdir", args.snapdir)
        if args.cmd == "latest":
            require_absolute("--source-path", args.source_path)
            if not NAME_RE.fullmatch(args.name):
                raise Usage("--name must match [A-Za-z0-9._-]+")
            return cmd_latest(args, host)
        if args.tz:
            try:
                zoneinfo.ZoneInfo(args.tz)
            except zoneinfo.ZoneInfoNotFoundError, ValueError:
                raise Usage(f"unknown --tz {args.tz!r}") from None
        args.sources = [parse_source(s) for s in args.sources]
        names = [s.name for s in args.sources]
        paths = [s.path for s in args.sources]
        if len(set(names)) != len(names) or len(set(paths)) != len(paths):
            raise Usage("each --source needs its own NAME and PATH")
        for text in getattr(args, "caches_skip", []):
            name, rel = parse_skip(text, names)
            args.sources[names.index(name)].skips.append(rel)
        if args.cmd == "list":
            return cmd_list(args, host)
        if args.cmd == "caches":
            return cmd_caches(args, host)
        if args.cmd == "finish":
            return cmd_finish(args, host)
        return cmd_snapshot_or_prune(args, host, create=(args.cmd == "snapshot"))
    except Usage as e:
        parser.print_usage(sys.stderr)
        print(f"btrfs-snapshot-core: {e}", file=sys.stderr)
        return EXIT_USAGE

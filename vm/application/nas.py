# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup [VM] --nas`: the NAS backup with restic, through a live snapshot that is
merged back (blockcommit) whatever happens, and its retention. Moved from vm/cli/nas.py; it
reaches the machine through the ports of vm/ports.py (NasBackup)."""

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import os.path

from cst_core.domain.errors import ExitStatus, Refused, ResticExit
from cst_core.domain.text import first_line
from cst_core.domain.values import HOUR, MINUTE, format_bytes_gib_mib
from cst_core.ports import ClockPort
from vm.application import domain_state
from vm.application.backup import (
    SourceWalk,
    backup_time,
    check_backup_start,
    check_shut_off,
    fingerprint_or_none,
    read_backup_definition,
)
from vm.application.nas_torn import forget_torn, nas_snapshot_ids, retention_after_torn
from vm.domain import (
    LIVE_STATES,
    NAS_DEFAULTS,
    OVERLAY_MARK,
    RESTIC_CHECK_TIMEOUT,
    RETRY_LOCK,
    SHUT_OFF,
    STOPPED_STATES,
    VIRSH_COMMIT_MARGIN,
    DomainParts,
    NasRepo,
    VmOverlayKind,
    VmSettings,
    agent_state,
    disk_label,
    live_disks,
    overlay_kind,
    overlay_recovery,
    quiesce_skipped,
)
from vm.domain.backup import (
    IndexEntry,
    IndexRefused,
    Manifest,
    NasMergeBack,
    NasNumbers,
    NasRead,
    NasRun,
    CopiedFile,
    ResticSummary,
    Stop,
    StopAfterBackup,
    StopAfterSnapshot,
    Uncertain,
    kept_unindexed_text,
    manifest_text,
    nas_done_lines,
    nas_index,
    nas_index_entry,
    nas_manifest,
    nas_tags,
    not_indexed_text,
    restic_failure_why,
    snapshot_args,
    torn_entry,
    unindexed_backup_text,
    unreadable_keep_keys,
    unreadable_nas_number,
)
from vm.ports import (
    BackupStorePort,
    LockPort,
    ProgressPort,
    ResticPort,
    SettingsPort,
    SignalsPort,
    VmCommandsPort,
)


@dataclass(frozen=True, slots=True)
class NasBackup:
    """What a NAS backup reaches: libvirt and the host's packages, the files, the settings, restic,
    the stop signals, the per-VM lock, the desktop alert (critical: title, body), the clock, the
    terminal; root: this process runs as root."""

    commands: VmCommandsPort
    store: BackupStorePort
    settings: SettingsPort
    restic: ResticPort
    signals: SignalsPort
    lock: LockPort
    alert: Callable[[str, str], None]
    clock: ClockPort
    out: ProgressPort
    root: bool


def blklist(commands: VmCommandsPort, vm: str, inactive: bool = False) -> tuple[dict[str, str] | None, str]:
    """({target: source}, problem) from virsh domblklist --details."""
    rc, so, err = commands.virsh(
        ("domblklist", vm, "--details", *(["--inactive"] if inactive else [])), 60, detach=None
    )
    if rc != 0:
        return None, first_line(err) or f"exit {rc}"
    out: dict[str, str] = {}
    for ln in so.splitlines():
        parts = ln.split(None, 3)
        if len(parts) == 4 and parts[0] != "Type" and not parts[0].startswith("-"):
            out[parts[2]] = parts[3].strip()
    return out, ""


def domain_state_or_none(commands: VmCommandsPort, vm: str) -> str | None:
    rc, so, _ = commands.virsh(("domstate", vm), 60, detach=None)
    return first_line(so) if rc == 0 else None


def restic_ready(cfg: VmSettings, restic: ResticPort, nas: NasRepo) -> None:
    """Refused unless the repository opens (NAS reachable, password right):
    checked before any snapshot, so an unreachable NAS never leaves the VM on an overlay."""
    # --no-lock: it only proves the repository opens, also while the /home
    # NAS job holds the lock (its prune), which the backup waits for.
    rc, _, err = restic.run(nas["env"], ["cat", "config", "--no-lock"], RESTIC_CHECK_TIMEOUT)
    if rc == 0:
        return
    why = {
        ExitStatus.NOT_FOUND: "restic is not installed (it comes with the NAS setup; after rpm-ostree, active at the next reboot)",
        ExitStatus.TIMED_OUT: f"the NAS did not answer within {RESTIC_CHECK_TIMEOUT // MINUTE} minutes",
        ResticExit.NO_REPOSITORY: f"no restic repository at {nas['repo']}",
        ResticExit.LOCKED: "the repository is locked (try again later)",
        ResticExit.WRONG_PASSWORD: f"{cfg.paths.restic_password} does not open the repository",
    }.get(rc, first_line(err) or f"restic exit {rc}")
    raise Refused(f"NAS repository not usable: {why}. Nothing was changed (no snapshot taken)")


def commit_back(
    cfg: VmSettings, p: NasBackup, vm: str, overlaid: Mapping[str, tuple[str, str]]
) -> tuple[list[str], dict[str, VmOverlayKind], set[str]]:
    """virsh blockcommit --active --pivot for each overlay (also on a paused
    VM), then a check of the live and the persistent definition, then the
    overlay file removed. Returns (problems, {target: how to recover}, the
    targets put back on their image); no problems: every disk is back on its image."""
    problems: list[str] = []
    how: dict[str, VmOverlayKind] = {}
    back: set[str] = set()
    state = domain_state_or_none(p.commands, vm)
    if state not in LIVE_STATES:
        problems.append(
            f"{vm} stopped during the backup: its disks still point to the overlay files"
            if state in STOPPED_STATES
            else f"the state of {vm} could not be read ({state or 'virsh domstate failed'}): nothing merged"
        )
        for t, (base, _overlay) in overlaid.items():
            how[t] = overlay_kind(state, None, base)
        return problems, how, back
    if state != "running":
        p.out.say(f"  {vm} is {state}: merging back anyway (a block job works on it)", flush=True)
    deadline = p.clock.monotonic() + cfg.commit_timeout
    for t, (base, _overlay) in overlaid.items():
        left = max(MINUTE, int(deadline - p.clock.monotonic()))
        p.out.say(f"  merging the overlay of {t} back into {base} (virsh blockcommit --active --pivot)", flush=True)
        # --timeout: virsh aborts the job itself (the disk stays on the overlay, no job
        # left half done) before the timer's TimeoutStopSec could kill it.
        rc, so, err = p.commands.virsh(
            ("blockcommit", vm, t, "--active", "--pivot", "--wait", "--verbose", "--timeout", str(left)),
            left + VIRSH_COMMIT_MARGIN,
            detach=None,
        )
        if rc != 0:
            problems.append(f"{t}: virsh blockcommit: {first_line(err) or first_line(so) or f'exit {rc}'}")
    live, lerr = blklist(p.commands, vm)
    persistent, perr = blklist(p.commands, vm, inactive=True)
    for t, (base, overlay) in overlaid.items():
        live_src = live.get(t) if live is not None else None
        if live is None:
            problems.append(f"{t}: virsh domblklist: {lerr}")
        elif live_src != base:
            problems.append(f"{t}: the running VM still uses {live_src or '?'} (expected {base})")
        elif persistent is not None and persistent.get(t) != base:
            problems.append(
                f"{t}: the definition still names {persistent.get(t) or '?'} (expected {base}): "
                "the VM would start on that stale overlay"
            )
        elif persistent is None:
            problems.append(
                f"{t}: the persistent definition was not readable ({perr}): the overlay is kept "
                "until both listings show the image"
            )
        else:
            back.add(t)
            why = p.store.remove_overlay(overlay)
            if why is not None:
                p.out.say(f"  note: {overlay}: not removed ({why}); {t} is back on {base}: remove it by hand")
            continue
        how[t] = overlay_kind(state, live_src, base)
    return problems, how, back


def find_overlays(p: NasBackup, vm: str, specs: Mapping[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """{target: (image, overlay)} of this run's overlays that the running VM
    or its definition names, or whose file exists when neither is readable."""
    live, _ = blklist(p.commands, vm)
    persistent, _ = blklist(p.commands, vm, inactive=True)
    out: dict[str, tuple[str, str]] = {}
    for t, (base, overlay) in specs.items():
        named = [m.get(t) == overlay for m in (live, persistent) if m is not None]
        if any(named) or (not named and p.store.exists(overlay)):
            out[t] = (base, overlay)
    return out


def nas_sources(
    cfg: VmSettings, store: BackupStorePort, vm: str, parts: DomainParts, notes: list[str]
) -> list[tuple[str, str]]:
    """[(path, kind)] restic reads: the disks, the UEFI variables, the TPM folder (SourceWalk)."""
    found = SourceWalk(cfg, store, vm, notes, nas=True).sources(parts)
    state = [(f, k) for f, k in ((found.nvram, "nvram"), (found.tpm, "tpm")) if f is not None]
    return [(d, "disk") for d in found.disks] + state


def backup_nas(cfg: VmSettings, vm: str, keep: str | None, p: NasBackup, *, quiesce: bool) -> None:
    """Back vm up to the NAS (keep: --keep-tag; quiesce: try the guest agent first)."""
    check_backup_start(cfg, p.store, "backup --nas", root=p.root)
    if "," in vm or not vm.strip():
        raise Refused(f"VM name {vm!r}: a comma cannot be part of a restic tag")
    p.store.read_torn()  # a record this cst vm cannot use refuses before anything is done (TornRefused)
    nas = p.settings.nas_repo()
    state = domain_state(p.commands, vm)
    if state not in ("running", SHUT_OFF):
        raise Refused(
            f"{vm} is {state or 'in an unknown state'}: the NAS backup reads a running VM (through a "
            f"snapshot) or a shut-off one. Resume it or shut it down first"
        )
    if state == SHUT_OFF:
        check_shut_off(p.commands, vm)
    p.signals.catch_stops()
    with p.lock(vm):
        nas_run(cfg, p, vm, keep, nas, quiesce=quiesce, live=state == "running")


def nas_run(
    cfg: VmSettings, p: NasBackup, vm: str, keep: str | None, nas: NasRepo, *, quiesce: bool, live: bool
) -> None:
    run = _nas_preflight(cfg, p, vm, live=live, quiesce=quiesce)
    restic_ready(cfg, p.restic, nas)
    p.out.say(
        f"{vm}: backup to the NAS ({nas['repo']}), {'running: live snapshot' if live else 'shut off'}", flush=True
    )
    failure: BaseException | None = None
    try:
        run.read.append(NasRead(*_nas_read(cfg, p, run, nas, keep)))
    # Any failure, a stop included: the overlays are merged back first, then it is raised again.
    except BaseException as e:  # noqa: BLE001
        failure = e
    # From here on no signal may cut the cleanup short (the merge back, the alert and the refusal with
    # the recovery lines), and no child may get one.
    with p.signals.ignored():
        merge = _nas_merge_back(cfg, p, run)
        # The snapshot's unclear outcome, also when a stop held through its call came first or the merge
        # back of another disk failed: libvirt may still switch a disk to an overlay file, so the alert names it.
        if run.unclear:
            merge.problems.extend(run.unclear)
            _add_overlay_files(p, run)
        refused = _nas_not_merged_back(cfg, p, run, merge, failure) if merge.problems else None
        # A stop after a read whose check passed, all merged back: the snapshot is a backup, indexed
        # before the stop ends the run (no listing, no retention: those wait for the next backup).
        if refused is None and isinstance(failure, StopAfterBackup):
            _say_notes(p, run.notes)
            failure = _index_stopped_backup(
                p, vm, failure, nas_index_entry(run, nas, failure.snap, failure.consistency, keep)
            )
    if refused is not None:
        raise refused
    if failure is not None:
        raise failure
    read = run.read[0]
    _nas_after_read(cfg, p, run, nas, nas_index_entry(run, nas, read.snap, read.consistency, keep))


def _nas_after_read(cfg: VmSettings, p: NasBackup, run: NasRun, nas: NasRepo, entry: IndexEntry) -> None:
    """After a good read, all merged back (entry: its index entry): what restic read, the retention (retention_after_torn),
    the index, the notes and how to restore it. A stop before the index is written (in the retention, a
    listing or the write) ends the run only once the snapshot is indexed, with no listing
    (_index_stopped_backup): else the next run names that backup as a snapshot no record names."""
    snap, vm = entry["snapshot"], run.vm
    try:
        # Here, not before: a stop that comes while it is printed still indexes the snapshot.
        summary = run.read[0].summary
        p.out.say(
            f"  restic snapshot {snap[:8]}: {format_bytes_gib_mib(summary.get('total_bytes_processed') or 0, mib_decimals=1)} read, "
            f"{format_bytes_gib_mib(summary.get('data_added') or 0, mib_decimals=1)} new on the NAS ({entry['consistency']})"
        )
        retention_after_torn(cfg, p, vm, nas, run.numbers, made=snap)
        refused = _index_nas_backup(p, vm, nas, entry)
    except Stop as e:
        with p.signals.ignored():
            _say_notes(p, run.notes)
            raise _index_stopped_backup(p, vm, e, entry) from None
    for line in nas_done_lines(run.notes, snap, vm, entry["consistency"], entry["keep"]):
        p.out.say(line)
    # A keep number that does not read: the backup is done, its retention skipped (retention), exit 1.
    unread = [
        f"{unreadable_nas_number(k, cfg.paths.vm_conf)}: the backup is done; its retention is skipped until it is fixed"
        for k in unreadable_keep_keys(run.numbers)
    ]
    if refused is not None or unread:
        raise Refused("\n".join([*([str(refused)] if refused is not None else []), *unread]))


def _nas_numbers(cfg: VmSettings, settings: SettingsPort) -> NasNumbers:
    """The numbers of vm.conf the run reads, once, before anything changes: Refused, with nothing backed up,
    when the time limit does not read (a keep number that does not read is named at the end of the run)."""
    timeout = settings.number("VM_NAS_TIMEOUT_HOURS")
    if timeout is None:
        raise Refused(f"{unreadable_nas_number('VM_NAS_TIMEOUT_HOURS', cfg.paths.vm_conf)}: nothing backed up; fix it")
    return NasNumbers(timeout, settings.number("VM_NAS_KEEP_WEEKLY"), settings.number("VM_NAS_KEEP_MONTHLY"))


def _nas_preflight(cfg: VmSettings, p: NasBackup, vm: str, *, live: bool, quiesce: bool) -> NasRun:
    """What a NAS backup reads and checks before restic is asked anything: Refused, with nothing
    changed, for a definition, a disk or a running VM it cannot back up, or a time limit of vm.conf that
    does not read (read first: _nas_numbers)."""
    numbers = _nas_numbers(cfg, p.settings)
    xml_text, parts = read_backup_definition(p.commands, vm, "nothing backed up")
    notes = list(parts.skipped)
    sources = nas_sources(cfg, p.store, vm, parts, notes)
    epoch, stamp = backup_time(cfg, p.clock)
    specs, others, agent = _nas_live_specs(cfg, p, vm, stamp, sources) if live else ({}, [], "none")
    return NasRun(
        vm=vm,
        live=live,
        quiesce=quiesce,
        xml_text=xml_text,
        parts=parts,
        numbers=numbers,
        versions=p.commands.package_versions(),
        deployment=p.commands.booted_deployment(),
        notes=notes,
        sources=sources,
        epoch=epoch,
        stamp=stamp,
        specs=specs,
        others=others,
        agent=agent,
    )


def _nas_live_specs(
    cfg: VmSettings, p: NasBackup, vm: str, stamp: str, sources: Sequence[tuple[str, str]]
) -> tuple[dict[str, tuple[str, str]], list[str], str]:
    """(specs, others, the guest agent's state) of a running VM's snapshot; Refused when its running
    definition cannot be read, runs on a backing chain, differs from the inactive one, or an overlay
    of this stamp is there already."""
    rc, live_xml, err = p.commands.virsh(("dumpxml", vm), 60, detach=None)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    specs: dict[str, tuple[str, str]] = {}
    others: list[str] = []
    for d in live_disks(live_xml):
        if d.device != "disk" or d.kind != "file":
            others.append(d.target)
            continue
        if OVERLAY_MARK in d.source or d.backing:
            raise Refused(
                f"{vm}: {disk_label(d.target, word='disk ')} runs on {d.source} with a backing chain (an overlay "
                f"left by an earlier backup?). Put it back first; `{cfg.cst} doctor` shows the commands"
            )
        base = p.store.realpath(d.source)
        specs[d.target] = (
            base,
            os.path.join(os.path.dirname(base), f"{os.path.basename(base)}{OVERLAY_MARK}{stamp}"),
        )
    bases = sorted(b for b, _ in specs.values())
    if len(set(bases)) != len(bases):  # two targets on one file would share one overlay
        raise Refused(
            f"{vm}: two disks of the running VM are one file ({', '.join(sorted({b for b in bases if bases.count(b) > 1}))}): "
            "a live snapshot cannot give each its own overlay; shut the VM down and back it up then"
        )
    if bases != sorted(src for src, k in sources if k == "disk"):
        raise Refused(
            f"{vm}: the running VM's disks differ from its definition (changed since it started): "
            "restart it, then back up"
        )
    for _, overlay in specs.values():
        if p.store.lexists(overlay):
            raise Refused(f"{overlay} already exists: wait a second and run it again")
    return specs, others, agent_state(live_xml)


def _nas_read(
    cfg: VmSettings, p: NasBackup, run: NasRun, nas: NasRepo, keep: str | None
) -> tuple[str, str, ResticSummary]:
    """The snapshot of a running VM, restic's read and the checks after it: (consistency, restic's
    snapshot, its summary). Refused, the overlays left to the merge back, when the read is no backup."""
    store = p.store
    consistency = "shut-off"
    if run.live:
        # The UEFI variables and the TPM state have no snapshot: read live, checked after.
        run.before.update({src: store.fingerprint(src) for src, k in run.sources if k != "disk"})
        consistency = take_snapshot(cfg, p, run)
    else:
        run.before.update({src: store.fingerprint(src) for src, _ in run.sources})
    files: list[CopiedFile] = [
        {"source": src, "kind": k, "allocated": store.sizes(src)[0], "apparent": store.sizes(src)[1]}
        for src, k in run.sources
    ]
    answer = _nas_restic_read(cfg, p, run, nas, nas_manifest(run, files, consistency, keep))
    return consistency, answer.snap, answer.summary


def _nas_restic_read(cfg: VmSettings, p: NasBackup, run: NasRun, nas: NasRepo, manifest: Manifest) -> _ResticAnswer:
    """restic's read of the sources (with manifest staged) and the check after it: restic's answer. Once
    restic answered with a snapshot, a stop waits (the stops held: the stage's removal, the check after
    the read) until the snapshot is recorded as torn (forget_torn, before its forget) or, when it is a
    backup, the check is done; so does a stop that ended restic once it had written its snapshot
    (StopAfterSnapshot). Such a stop after a check that passed is raised as StopAfterBackup: nas_run
    indexes the snapshot. After a stop that ended restic, a snapshot whose check could not be done (an
    error that is neither the check's refusal nor a stop) is recorded as torn, then the stop raised."""
    answer = _ResticAnswer(p.signals)
    try:
        _nas_restic_backup(cfg, p, run, nas, manifest, answer=answer)
        try:
            _nas_check_after_read(cfg, p, run, nas, answer)
        except Refused, Stop:
            raise
        except Exception:
            if answer.carried is None or not answer.snap:
                raise
            # The stop came while restic ran: the snapshot was not checked, so it is no backup. Its record,
            # then the carried stop (forget_torn's release), which replaces the error (its __context__).
            entry = torn_entry(nas, answer.snap, run.vm, run.stamp, "check failed")
            forget_torn(cfg, p, nas, entry, release=answer.release)
            raise
    except BaseException as e:
        answer.end_hold(e)
        raise
    try:
        answer.release()
    except Stop as e:
        raise StopAfterBackup(str(e), answer.snap, manifest.get("consistency") or "shut-off") from None
    return answer


@dataclass(slots=True)
class _ResticAnswer:
    """restic's answer to the backup: its snapshot ("" none) and summary; from the answer on, the stops
    held (hold) until release, so that a stop never lands between restic's answer and the record of a
    torn snapshot (or the end of the check after the read); and a stop that ended restic after it had
    written the snapshot (carried), raised by release after the one that waited, if any."""

    signals: SignalsPort
    snap: str = ""
    summary: ResticSummary = field(default_factory=ResticSummary)
    carried: Stop | None = None
    hold: AbstractContextManager[object] | None = None

    def answered(self) -> None:
        """restic answered with a snapshot: the stops wait from now on (restic.backup's answered). A stop
        whose handler runs between the mask and the keeping of the hold leaves the stops blocked:
        harmless, as on_signal ignores them after a stop, and the merge back runs with them ignored."""
        hold = self.signals.held()
        hold.__enter__()
        self.hold = hold

    def end_hold(self, failure: BaseException | None = None) -> None:
        """The stops let in again, as a with block ends: a stop that waited is raised here, also when failure
        (the exception that ends the hold) is given; the stop then replaces failure, which stays its
        __context__."""
        hold, self.hold = self.hold, None
        if hold is None:
            return
        if failure is None:
            hold.__exit__(None, None, None)
        else:
            hold.__exit__(type(failure), failure, failure.__traceback__)

    def release(self) -> None:
        """The stops let in again (a stop that waited is raised here), then a carried stop raised."""
        self.end_hold()
        if self.carried is not None:
            stop, self.carried = self.carried, None
            raise stop


def _nas_restic_backup(
    cfg: VmSettings, p: NasBackup, run: NasRun, nas: NasRepo, manifest: Manifest, *, answer: _ResticAnswer
) -> None:
    """restic backup of the sources and the staged definition and manifest, tagged with the manifest's
    tags: its snapshot and summary into answer; Refused (an incomplete snapshot forgotten) when it is no
    backup. A stop that ended restic after its snapshot is carried in answer, and the snapshot goes on as
    restic's answer: a torn one (restic did not end on its own) is recorded, then the stop raised."""
    store, out = p.store, p.out
    stage = store.stage(run.vm, run.xml_text, manifest_text(manifest))
    paths = [src for src, _ in run.sources] + [stage]
    timeout = run.numbers.timeout_hours * HOUR or NAS_DEFAULTS["VM_NAS_TIMEOUT_HOURS"] * HOUR
    for src, k in run.sources:
        a, s = store.sizes(src)
        out.say(
            f"  reading {src} ({k}, {format_bytes_gib_mib(a, mib_decimals=1)} allocated, {format_bytes_gib_mib(s, mib_decimals=1)} apparent)"
        )
    tags = ",".join(nas_tags(run.vm, manifest.get("keep")))
    try:
        rc, answer.summary = p.restic.backup(
            nas["env"],
            ["backup", "--json", "--host", nas["host"], "--tag", tags, "--retry-lock", RETRY_LOCK, *paths],
            timeout,
            out,
            answered=answer.answered,
        )
    except StopAfterSnapshot as e:
        rc, answer.summary, answer.carried = e.rc, e.summary, e
    finally:
        store.unstage(stage)
    answer.snap = snap = str(answer.summary.get("snapshot_id") or "")
    if rc != 0 or not snap:
        # incomplete (rc 3), or restic stopped after its snapshot: not a backup
        why = "incomplete" if answer.carried is None else "stopped"
        entry = torn_entry(nas, snap, run.vm, run.stamp, why)
        left = forget_torn(cfg, p, nas, entry, release=answer.release) if snap else None
        raise Refused(
            f"restic backup failed: {restic_failure_why(rc)}; no backup recorded{'' if left is None else f'; {left}'}"
        )


def _nas_check_after_read(cfg: VmSettings, p: NasBackup, run: NasRun, nas: NasRepo, answer: _ResticAnswer) -> None:
    """After restic's read: a note for each file read live that changed meanwhile; for a shut-off VM,
    Refused (its snapshot forgotten) when the VM was started or a source changed meanwhile."""
    if run.live:
        _nas_note_changed(p.store, run)
        return
    vm, snap = run.vm, answer.snap
    try:
        check_shut_off(p.commands, vm)
    except Refused as e:
        entry = torn_entry(nas, snap, vm, run.stamp, "state changed")
        left = forget_torn(cfg, p, nas, entry, release=answer.release)
        raise Refused(
            f"the VM's state changed during the backup, the copy is not consistent "
            f"({left or f'restic snapshot {snap[:8]} forgotten'}): {e}"
        ) from None
    changed = [src for src, _ in run.sources if fingerprint_or_none(p.store, src) != run.before[src]]
    if changed:
        entry = torn_entry(nas, snap, vm, run.stamp, "changed during the read")
        left = forget_torn(cfg, p, nas, entry, release=answer.release)
        raise Refused(
            "changed during the backup (the VM was started?), the copy is not consistent "
            f"({left or f'restic snapshot {snap[:8]} forgotten'}): {', '.join(changed)}"
        )


def _nas_note_changed(store: BackupStorePort, run: NasRun) -> None:
    """A note for each file read live (the UEFI variables, the TPM state) that changed meanwhile."""
    for src in run.before:
        if fingerprint_or_none(store, src) != run.before[src]:
            run.notes.append(
                f"{src} changed while restic read it (the VM wrote it): that copy may be torn; "
                "the next backup reads it again"
            )


def _nas_merge_back(cfg: VmSettings, p: NasBackup, run: NasRun) -> NasMergeBack:
    """The merge back of every overlay this run made, whatever happened before (with the signals
    ignored by the caller): what it could not do."""
    problems: list[str] = []
    how: dict[str, VmOverlayKind] = {}
    back: set[str] = set()
    with p.commands.detached():
        if run.live:
            # What the VM really runs on, not only what take_snapshot saw.
            try:
                run.overlaid.update(find_overlays(p, run.vm, run.specs))
            # The merge back still runs for what is known.
            except Exception as e:  # noqa: BLE001
                problems.append(f"the disks could not be listed: {e!r}")
        if run.overlaid:
            try:
                more, how, back = commit_back(cfg, p, run.vm, run.overlaid)
                problems += more
            except Exception as e:  # noqa: BLE001 - reported loudly below
                problems.append(f"the merge back failed: {e!r}")
    return NasMergeBack(problems, how, frozenset(back))


def _nas_not_merged_back(
    cfg: VmSettings, p: NasBackup, run: NasRun, merge: NasMergeBack, failure: BaseException | None
) -> Refused:
    """A disk left on an overlay: the alert, and the refusal with the commands that put each disk
    back (and the backup's own failure, unless it was the snapshot's unclear outcome)."""
    body = (
        f"{run.vm}: a disk was not merged back after the NAS backup, or may not be: "
        + "; ".join(merge.problems)
        + f". Check what the VM runs on; see `{cfg.cst} doctor`."
    )
    p.alert("VM backup: disk left on a snapshot overlay", body)
    if not run.overlaid:  # nothing known: the files this run could have made
        _add_overlay_files(p, run)
    # A disk the merge back put back (both listings show its image) needs no command. When none is left
    # to name (a disk that never showed its overlay, a listing that could not be read), the look-first
    # commands of the disks the run knows (no recovery kind) still show where the VM stands.
    left = [(t, spec) for t, spec in run.overlaid.items() if t not in merge.back] or list(run.overlaid.items())
    lines = overlay_recovery(run.vm, left, merge.how)
    msg = (
        "DISK NOT MERGED BACK:\n  "
        + "\n  ".join(merge.problems)
        + "\nNothing was undone or removed. Put each disk back by hand (read each line first):\n  "
        + "\n  ".join(lines)
    )
    # A good read (a stop after it, or none: run.read has it): its snapshot named, with what keeps it.
    if isinstance(failure, StopAfterBackup):
        msg += f"\n(the backup itself: {failure}; {unindexed_backup_text(run.vm, failure.snap)})"
    elif failure is None and run.read:
        msg += f"\n(the backup itself: {unindexed_backup_text(run.vm, run.read[0].snap)})"
    elif failure is not None and not isinstance(failure, Uncertain):
        msg += f"\n(the backup itself: {failure})"
    return Refused(msg)


def _add_overlay_files(p: NasBackup, run: NasRun) -> None:
    """run.overlaid gets each overlay of run.specs whose file is there."""
    run.overlaid.update({t: spec for t, spec in run.specs.items() if p.store.lexists(spec[1])})


def _say_notes(p: NasBackup, notes: Sequence[str]) -> None:
    """The run's notes (a TPM copy that may be torn, ...), as nas_done_lines prints them: for a stop that
    ends a run whose snapshot is a backup, before its words."""
    for n in notes:
        p.out.say(f"  note: {n}")


def _index_stopped_backup(p: NasBackup, vm: str, stop: Stop, entry: IndexEntry) -> Stop:
    """The index with the backup a stop came after (a stop held through its checked read, or one after
    the merge back; no listing: its other entries stay as they are); the stop to raise, which says so,
    or that the index is not this cst vm's."""
    snap = entry["snapshot"]
    try:
        p.store.update_index(lambda entries: nas_index(entries, entry, None))
    except IndexRefused as e:
        return Stop(f"{stop}; {_not_indexed(snap, vm, e)}")
    except OSError as e:
        return Stop(f"{stop}; {_index_write_failed(snap, vm, e)}")
    return Stop(
        f"{stop}; restic snapshot {snap[:8]} of {vm} is a backup: indexed (the retention runs at the next backup)"
    )


def _not_indexed(snap: str, vm: str, e: IndexRefused) -> str:
    """What a NAS backup the index could not take says of its snapshot."""
    return not_indexed_text(f"the restic snapshot {snap[:8]} of {vm}", kept_unindexed_text(snap), e)


def _index_write_failed(snap: str, vm: str, e: OSError) -> str:
    """What a NAS backup whose index write failed says of its snapshot: true whether the index was replaced
    or not (write_atomic can fail at the folder's fsync, after the rename)."""
    return (
        f"the restic snapshot {snap[:8]} of {vm} is a backup, but writing the index failed ({e}): the index may "
        f"not name it; {kept_unindexed_text(snap)}"
    )


def _index_nas_backup(p: NasBackup, vm: str, nas: NasRepo, entry: IndexEntry) -> Refused | None:
    """The index with the new NAS backup (and without the VM's NAS entries whose snapshot restic no
    longer lists); the refusal to raise after the notes when the index is not this cst vm's or cannot
    be written."""
    # The index was read before the snapshot too, but a newer cst vm (or a damage) can have changed it
    # since: the snapshot stays, and its notes and restore line are printed before the refusal.
    snap = entry["snapshot"]
    try:
        # An index this cst vm cannot write refuses before restic is asked. The listing comes before the
        # index's lock: a restic call never holds another VM's backup.
        p.store.read_index()
        listing = nas_snapshot_ids(p.restic, vm, nas)
        alive, unkept = (None, frozenset[str]()) if listing is None else listing
        p.store.update_index(lambda entries: nas_index(entries, entry, alive, unkept=unkept))
    except IndexRefused as e:
        return Refused(_not_indexed(snap, vm, e))
    except OSError as e:
        return Refused(_index_write_failed(snap, vm, e))
    return None


def take_snapshot(cfg: VmSettings, p: NasBackup, run: NasRun) -> str:
    """The external disk-only snapshot; fills run.overlaid {target: (image,
    overlay)} from what domblklist shows, also after a failure. Returns the
    consistency: quiesced or crash-consistent. Raises Uncertain when virsh
    did not answer and libvirt's result stayed unclear."""
    why = quiesce_skipped(run.agent, quiesce=run.quiesce)
    if not why:
        rc, err, unsure = _snapshot_attempt(cfg, p, run, quiesce=True)
        _refuse_partial(run)
        if run.overlaid:
            if rc != 0 or unsure:
                if rc != 0:  # _snapshot_attempt thawed the guest
                    run.notes.append(f"virsh reported an error but the snapshot is in place: {err}")
                return "crash-consistent"
            p.out.say("  snapshot taken with the guest's file systems frozen (quiesced, QEMU guest agent)")
            return "quiesced"
        why = f"quiesce failed: {err}"
    p.out.say(f"  crash-consistent snapshot ({why})", flush=True)
    rc, err, _ = _snapshot_attempt(cfg, p, run, quiesce=False)
    _refuse_partial(run)
    if not run.overlaid:
        raise Refused(f"virsh snapshot-create-as {run.vm}: {err} (nothing changed)")
    if rc != 0:
        run.notes.append(f"virsh reported an error but the snapshot is in place: {err}")
    run.notes.append(f"crash-consistent: {why}")
    return "crash-consistent"


def _refuse_partial(run: NasRun) -> None:
    """A settled answer that switched only some disks (which --atomic should never let happen) is no
    backup: Refused leaves the switched ones to the merge back, with no second attempt (the files of the
    disks not switched were dropped: _snapshot_attempt)."""
    if not run.overlaid or len(run.overlaid) == len(run.specs):
        return
    switched = ", ".join(t for t in run.specs if t in run.overlaid)
    not_switched = ", ".join(t for t in run.specs if t not in run.overlaid)
    raise Refused(
        f"virsh snapshot-create-as {run.vm} switched only some disks (switched: {switched}; not: {not_switched}): "
        "no backup, the switched disks are merged back"
    )


def _snapshot_attempt(cfg: VmSettings, p: NasBackup, run: NasRun, *, quiesce: bool) -> tuple[int, str, bool]:
    """One virsh snapshot-create-as: (its exit status, its error, True when it did not answer in
    time but the snapshot is in place). Raises Uncertain when the VM's disks still do not all show
    their overlay once snapshot_settle is over."""
    vm, specs = run.vm, run.specs
    args = snapshot_args(vm, run.stamp, specs, run.others, quiesce=quiesce)
    # Signals wait until the outcome is known (virsh is never killed by a stop);
    # its own session keeps a Ctrl-C or a closed terminal away from it.
    with p.signals.held():
        rc, so, err = p.commands.virsh(args, cfg.snapshot_timeout, detach=True)
        settled = rc != ExitStatus.TIMED_OUT and rc >= 0  # not a timeout, and virsh not killed by a signal
        deadline = p.clock.monotonic() + (0 if settled else cfg.snapshot_settle)
        _poll_overlays(p, run, deadline, settled=settled)
        if not settled and len(run.overlaid) != len(specs):
            # In run before the block ends: a stop held through the call is raised there, and nas_run
            # still alerts on this.
            kept = ", ".join(o for t, (_, o) in specs.items() if t not in run.overlaid and p.store.lexists(o))
            run.unclear.append(
                f"virsh snapshot-create-as {vm} did not answer within {cfg.snapshot_timeout} s and, "
                f"{cfg.snapshot_settle} s later, the VM's disks still do not all show the overlay: libvirt "
                f"may still switch to it. Nothing was removed{f' (kept: {kept})' if kept else ''}"
            )
        # In the block too: a stop held through the call is raised at its end, and the guest must not
        # stay frozen. A quiesced call that did not answer, or that failed with a disk in place, leaves
        # the freeze unknown.
        if quiesce and (not settled or (rc != 0 and run.overlaid)):
            p.commands.virsh(("domfsthaw", vm), 60, detach=None)  # best effort: never leave the guest frozen
        # In the block too: the files of the disks a settled answer did not switch (not in use), also when
        # a stop held through the call is raised at the block's end, and no stop cuts the drop short.
        if settled and len(run.overlaid) != len(specs):
            _drop_stray_overlays(p, run)
    if run.unclear:
        raise Uncertain(run.unclear[0])
    if not settled and quiesce:
        run.notes.append(
            "virsh did not answer in time; the snapshot is in place, but whether the guest was frozen is unknown"
        )
        return 0, "", True
    return rc, first_line(err) or first_line(so) or f"exit {rc}", False


def _poll_overlays(p: NasBackup, run: NasRun, deadline: float, *, settled: bool) -> None:
    """run.overlaid from what the VM runs on, also when virsh failed or timed out (the truth); again
    every 5 s until every disk shows its overlay or deadline passes, unless virsh settled it."""
    while True:
        cur, _ = blklist(p.commands, run.vm)
        run.overlaid.update(
            {
                t: (base, overlay)
                for t, (base, overlay) in run.specs.items()
                if ((cur.get(t) == overlay) if cur is not None else p.store.exists(overlay))
            }
        )
        if settled or len(run.overlaid) == len(run.specs) or p.clock.monotonic() >= deadline:
            break
        p.clock.sleep(5)


def _drop_stray_overlays(p: NasBackup, run: NasRun) -> None:
    """The overlay files of the disks that did not switch to them: only after a definite error from
    libvirt, so not in use."""
    for t, (_, overlay) in run.specs.items():
        if t not in run.overlaid:
            p.store.drop(overlay)

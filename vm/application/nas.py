# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup [VM] --nas`: the NAS backup with restic, through a live snapshot that is
merged back (blockcommit) whatever happens, and its retention. Moved from vm/cli/nas.py as it was;
it reaches the machine through the ports of vm/ports.py (NasBackup)."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os.path
import re

from cst_core.domain.errors import ExitStatus, Refused, ResticExit
from cst_core.domain.shapes import list_of, parse_json
from cst_core.domain.text import first_line
from cst_core.domain.values import HOUR, MINUTE, format_bytes_gib_mib
from cst_core.ports import ClockPort
from vm.application import domain_state
from vm.application.backup import (
    backup_time,
    check_backup_start,
    check_shut_off,
    fingerprint_or_none,
    read_backup_definition,
)
from vm.domain import (
    KEEP_TAG,
    LIVE_STATES,
    NAS_DEFAULTS,
    OVERLAY_MARK,
    RESTIC_CHECK_TIMEOUT,
    RESTIC_FORGET_TIMEOUT,
    RETRY_LOCK,
    SHUT_OFF,
    TAG,
    VIRSH_COMMIT_MARGIN,
    DomainParts,
    NasRepo,
    VmOverlayKind,
    VmSettings,
    agent_state,
    disk_label,
    forget_args,
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
    NasRun,
    CopiedFile,
    ResticSnapshot,
    ResticSummary,
    Stop,
    TornEntry,
    Uncertain,
    is_restic_snapshot,
    manifest_text,
    nas_done_lines,
    nas_index,
    nas_index_entry,
    nas_manifest,
    not_indexed_text,
    restic_failure_why,
    snapshot_args,
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
    checked before any snapshot, so an unreachable NAS never leaves the VM
    on an overlay."""
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
) -> tuple[list[str], dict[str, VmOverlayKind]]:
    """virsh blockcommit --active --pivot for each overlay (also on a paused
    VM), then a check of the live and the persistent definition, then the
    overlay file removed. Returns (problems, {target: how to recover}); no
    problems: every disk is back on its image."""
    problems: list[str] = []
    how: dict[str, VmOverlayKind] = {}
    state = domain_state_or_none(p.commands, vm)
    if state not in LIVE_STATES:
        problems.append(
            f"{vm} stopped during the backup: its disks still point to the overlay files"
            if state in (SHUT_OFF, "crashed")
            else f"the state of {vm} could not be read ({state or 'virsh domstate failed'}): nothing merged"
        )
        for t, (base, _overlay) in overlaid.items():
            how[t] = overlay_kind(state, None, base)
        return problems, how
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
            why = p.store.remove_overlay(overlay)
            if why is not None:
                p.out.say(f"  note: {overlay}: not removed ({why}); {t} is back on {base}: remove it by hand")
            continue
        how[t] = overlay_kind(state, live_src, base)
    return problems, how


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
    """[(path, kind)] restic reads: the disks, the UEFI variables, the TPM folder."""
    out: list[tuple[str, str]] = [(_nas_disk_source(cfg, store, vm, p, notes), "disk") for p in parts.disks]
    if parts.nvram:
        if store.is_file(parts.nvram):
            out.append((store.realpath(parts.nvram), "nvram"))
        else:
            notes.append(f"UEFI variables {parts.nvram} not there yet: not backed up")
    if parts.tpm:
        tpm_dir = os.path.join(cfg.paths.swtpm_dir, parts.uuid)
        if parts.uuid and store.is_dir(tpm_dir):
            out.append((store.realpath(tpm_dir), "tpm"))
        else:
            notes.append(
                f"TPM state {tpm_dir} not found: not backed up (BitLocker may then ask for its "
                "recovery key after a restore)"
            )
    return out


def _nas_disk_source(cfg: VmSettings, store: BackupStorePort, vm: str, p: str, notes: list[str]) -> str:
    """The file restic reads for the disk p (through a symbolic link, noted); Refused when it is not
    there, is an overlay a NAS backup left behind, or has a backing file."""
    if not store.is_file(p):
        raise Refused(f"{vm}: disk {p} not found")
    if OVERLAY_MARK in p:
        raise Refused(
            f"{vm}: disk {p} is an overlay a NAS backup left behind (its blockcommit did not "
            f"finish). Put the disk back first; `{cfg.cst} doctor` shows the commands"
        )
    r = store.realpath(p)
    if r != p:
        notes.append(f"disk {p} is a symbolic link: read through it: {r}")
    if store.has_backing_file(r):
        raise Refused(
            f"{vm}: {r} is a qcow2 overlay with a backing file: back up the chain by hand "
            "(qemu-img info --backing-chain)"
        )
    return r


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
    consistency, snap, summary = "shut-off", "", ResticSummary()
    try:
        consistency, snap, summary = _nas_read(cfg, p, run, nas, keep)
    # Any failure, a stop included: the overlays are merged back first, then it is raised again.
    except BaseException as e:  # noqa: BLE001
        failure = e
    # From here on no signal may cut the cleanup short, and no child may get one.
    with p.signals.ignored():
        merge = _nas_merge_back(cfg, p, run)
    if isinstance(failure, Uncertain) and not merge.problems:
        merge.problems.append(str(failure))
        run.overlaid.update({t: spec for t, spec in run.specs.items() if p.store.lexists(spec[1])})
    if merge.problems:
        raise _nas_not_merged_back(cfg, p, run, merge, failure)
    if failure is not None:
        raise failure
    p.out.say(
        f"  restic snapshot {snap[:8]}: {format_bytes_gib_mib(summary.get('total_bytes_processed') or 0, mib_decimals=1)} read, "
        f"{format_bytes_gib_mib(summary.get('data_added') or 0, mib_decimals=1)} new on the NAS ({consistency})"
    )
    retention_after_torn(cfg, p, vm, nas)
    refused = _index_nas_backup(p, vm, nas, nas_index_entry(run, nas, snap, consistency, keep))
    for line in nas_done_lines(run.notes, snap, vm, consistency, keep):
        p.out.say(line)
    if refused is not None:
        raise refused


def _nas_preflight(cfg: VmSettings, p: NasBackup, vm: str, *, live: bool, quiesce: bool) -> NasRun:
    """What a NAS backup reads and checks before restic is asked anything: Refused, with nothing
    changed, for a definition, a disk or a running VM it cannot back up."""
    xml_text, parts = read_backup_definition(p.commands, vm, "nothing backed up")
    notes = list(parts.skipped)
    sources = nas_sources(cfg, p.store, vm, parts, notes)
    epoch, stamp = backup_time(cfg, p.clock)
    specs: dict[str, tuple[str, str]] = {}
    others: list[str] = []  # target -> (image, overlay); targets left out of the snapshot
    agent = "none"
    if live:
        specs, others, agent = _nas_live_specs(cfg, p, vm, stamp, sources)
    versions, deployment = p.commands.package_versions(), p.commands.booted_deployment()
    return NasRun(
        vm, live, quiesce, xml_text, parts, notes, sources, epoch, stamp, specs, others, agent, versions, deployment
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
    if sorted(b for b, _ in specs.values()) != sorted(src for src, k in sources if k == "disk"):
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
    snap, summary = _nas_restic_backup(cfg, p, run, nas, nas_manifest(run, files, consistency, keep))
    _nas_check_after_read(cfg, p, run, nas, snap)
    return consistency, snap, summary


def _nas_restic_backup(
    cfg: VmSettings, p: NasBackup, run: NasRun, nas: NasRepo, manifest: Manifest
) -> tuple[str, ResticSummary]:
    """restic backup of the sources and the staged definition and manifest, with the manifest's
    tags: (its snapshot, its summary); Refused (an incomplete snapshot forgotten) when it is no backup."""
    store, out = p.store, p.out
    stage = store.stage(run.vm, run.xml_text, manifest_text(manifest))
    paths = [src for src, _ in run.sources] + [stage]
    timeout = p.settings.number("VM_NAS_TIMEOUT_HOURS") * HOUR or NAS_DEFAULTS["VM_NAS_TIMEOUT_HOURS"] * HOUR
    for src, k in run.sources:
        a, s = store.sizes(src)
        out.say(
            f"  reading {src} ({k}, {format_bytes_gib_mib(a, mib_decimals=1)} allocated, {format_bytes_gib_mib(s, mib_decimals=1)} apparent)"
        )
    tags = ",".join(manifest.get("tags", []))
    try:
        rc, summary = p.restic.backup(
            nas["env"],
            ["backup", "--json", "--host", nas["host"], "--tag", tags, "--retry-lock", RETRY_LOCK, *paths],
            timeout,
            out,
        )
    finally:
        store.unstage(stage)
    snap = str(summary.get("snapshot_id") or "")
    if rc != 0 or not snap:
        # incomplete (rc 3): not a backup
        left = forget_torn(cfg, p, nas, torn_entry(nas, snap, run.vm, run.stamp, "incomplete")) if snap else None
        raise Refused(
            f"restic backup failed: {restic_failure_why(rc)}; no backup recorded{'' if left is None else f'; {left}'}"
        )
    return snap, summary


def _nas_check_after_read(cfg: VmSettings, p: NasBackup, run: NasRun, nas: NasRepo, snap: str) -> None:
    """After restic's read: a note for each file read live that changed meanwhile; for a shut-off VM,
    Refused (its snapshot forgotten) when the VM was started or a source changed meanwhile."""
    if run.live:
        _nas_note_changed(p.store, run)
        return
    vm = run.vm
    try:
        check_shut_off(p.commands, vm)
    except Refused as e:
        left = forget_torn(cfg, p, nas, torn_entry(nas, snap, vm, run.stamp, "state changed"))
        raise Refused(
            f"the VM's state changed during the backup, the copy is not consistent "
            f"({left or f'restic snapshot {snap[:8]} forgotten'}): {e}"
        ) from None
    changed = [src for src, _ in run.sources if fingerprint_or_none(p.store, src) != run.before[src]]
    if changed:
        left = forget_torn(cfg, p, nas, torn_entry(nas, snap, vm, run.stamp, "changed during the read"))
        raise Refused(
            "changed during the backup (the VM was started?), the copy is not consistent "
            f"({left or f'restic snapshot {snap[:8]} forgotten'}): {', '.join(changed)}"
        )


def _nas_note_changed(store: BackupStorePort, run: NasRun) -> None:
    """A note for each file read live (the UEFI variables, the TPM state) that changed meanwhile."""
    for src in run.before:
        try:
            same = store.fingerprint(src) == run.before[src]
        except OSError:
            same = False
        if not same:
            run.notes.append(
                f"{src} changed while restic read it (the VM wrote it): that copy may be torn; "
                "the next backup reads it again"
            )


def _nas_merge_back(cfg: VmSettings, p: NasBackup, run: NasRun) -> NasMergeBack:
    """The merge back of every overlay this run made, whatever happened before (with the signals
    ignored by the caller): what it could not do."""
    problems: list[str] = []
    how: dict[str, VmOverlayKind] = {}
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
                more, how = commit_back(cfg, p, run.vm, run.overlaid)
                problems += more
            except Exception as e:  # noqa: BLE001 - reported loudly below
                problems.append(f"the merge back failed: {e!r}")
    return NasMergeBack(problems, how)


def _nas_not_merged_back(
    cfg: VmSettings, p: NasBackup, run: NasRun, merge: NasMergeBack, failure: BaseException | None
) -> Refused:
    """A disk left on an overlay: the alert, and the refusal with the commands that put each disk
    back (and the backup's own failure, unless it was the snapshot's unclear outcome)."""
    vm = run.vm
    body = (
        f"{vm}: a disk was not merged back after the NAS backup, or may not be: "
        + "; ".join(merge.problems)
        + f". Check what the VM runs on; see `{cfg.cst} doctor`."
    )
    p.alert("VM backup: disk left on a snapshot overlay", body)
    if not run.overlaid:  # nothing known: the files this run could have made
        run.overlaid.update({t: spec for t, spec in run.specs.items() if p.store.lexists(spec[1])})
    lines = overlay_recovery(vm, run.overlaid.items(), merge.how)
    msg = (
        "DISK NOT MERGED BACK:\n  "
        + "\n  ".join(merge.problems)
        + "\nNothing was undone or removed. Put each disk back by hand (read each line first):\n  "
        + "\n  ".join(lines)
    )
    if failure is not None and not isinstance(failure, Uncertain):
        msg += f"\n(the backup itself: {failure})"
    return Refused(msg)


def _index_nas_backup(p: NasBackup, vm: str, nas: NasRepo, entry: IndexEntry) -> Refused | None:
    """The index with the new NAS backup (and without the VM's NAS entries whose snapshot restic no
    longer lists); the refusal to raise after the notes when the index is not this cst vm's."""
    # The index was read before the snapshot too, but a newer cst vm (or a damage) can have changed it
    # since: the snapshot stays, and its notes and restore line are printed before the refusal.
    snap = entry["snapshot"]
    try:
        entries = p.store.read_index()
        p.store.write_index(nas_index(entries, entry, nas_snapshot_ids(p.restic, vm, nas)))
    except IndexRefused as e:
        kept = (
            "the snapshot stays in the repository, where the retention sees it as any VM backup of its tags "
            '(restore it by its ID: vm/README.md, section "Restore from the NAS")'
        )
        return Refused(not_indexed_text(f"the restic snapshot {snap[:8]} of {vm}", kept, e))
    return None


def take_snapshot(cfg: VmSettings, p: NasBackup, run: NasRun) -> str:
    """The external disk-only snapshot; fills run.overlaid {target: (image,
    overlay)} from what domblklist shows, also after a failure. Returns the
    consistency: quiesced or crash-consistent. Raises Uncertain when virsh
    did not answer and libvirt's result stayed unclear."""
    why = quiesce_skipped(run.agent, quiesce=run.quiesce)
    if not why:
        rc, err, unsure = _snapshot_attempt(cfg, p, run, quiesce=True)
        if run.overlaid:
            if rc != 0 or unsure:
                if rc != 0:
                    run.notes.append(f"virsh reported an error but the snapshot is in place: {err}")
                    p.commands.virsh(
                        ("domfsthaw", run.vm), 60, detach=None
                    )  # best effort: never leave the guest frozen
                return "crash-consistent"
            p.out.say("  snapshot taken with the guest's file systems frozen (quiesced, QEMU guest agent)")
            return "quiesced"
        _drop_stray_overlays(p, run)
        why = f"quiesce failed: {err}"
    p.out.say(f"  crash-consistent snapshot ({why})", flush=True)
    rc, err, _ = _snapshot_attempt(cfg, p, run, quiesce=False)
    if not run.overlaid:
        _drop_stray_overlays(p, run)
        raise Refused(f"virsh snapshot-create-as {run.vm}: {err} (nothing changed)")
    if rc != 0:
        run.notes.append(f"virsh reported an error but the snapshot is in place: {err}")
    run.notes.append(f"crash-consistent: {why}")
    return "crash-consistent"


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
        if quiesce:
            p.commands.virsh(("domfsthaw", vm), 60, detach=None)  # best effort: never leave the guest frozen
        kept = ", ".join(o for t, (_, o) in specs.items() if t not in run.overlaid and p.store.lexists(o))
        raise Uncertain(
            f"virsh snapshot-create-as {vm} did not answer within {cfg.snapshot_timeout} s and, "
            f"{cfg.snapshot_settle} s later, the VM's disks still do not all show the overlay: libvirt "
            f"may still switch to it. Nothing was removed{f' (kept: {kept})' if kept else ''}"
        )
    if not settled and quiesce:
        p.commands.virsh(("domfsthaw", vm), 60, detach=None)
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


def nas_snapshot_ids(restic: ResticPort, vm: str, nas: NasRepo) -> set[str] | None:
    """The ids of the VM's restic snapshots, or None when not readable."""
    snaps = nas_snapshots(restic, vm, nas)
    return None if snaps is None else {s["id"] for s in snaps}


def nas_snapshots(restic: ResticPort, vm: str, nas: NasRepo, host: str | None = None) -> list[ResticSnapshot] | None:
    """The VM's restic snapshots (restic snapshots --json of its tags, made on host: nas's by default),
    or None when not readable."""
    rc, so, _ = restic.run(
        nas["env"],
        [
            "snapshots",
            "--json",
            "--no-lock",
            "--host",
            nas["host"] if host is None else host,
            "--tag",
            f"{TAG},vm:{vm}",
        ],
        RESTIC_CHECK_TIMEOUT,
    )
    if rc != 0:
        return None
    try:
        data = parse_json(so)
    except ValueError, RecursionError:
        return None
    # All or nothing: an answer of another shape is no list of the live snapshots, and reading it
    # as one would drop the index entries of every snapshot it does not name.
    return data if list_of(data, is_restic_snapshot) else None


def torn_entry(nas: NasRepo, snap: str, vm: str, stamp: str, why: str) -> TornEntry:
    """The record of snap, a torn snapshot of vm's run at stamp (why: what tore it)."""
    return {"snapshot": snap, "vm": vm, "repository": nas["repo"], "host": nas["host"], "timestamp": stamp, "why": why}


def forget_torn(cfg: VmSettings, p: NasBackup, nas: NasRepo, entry: TornEntry) -> str | None:
    """Forget a restic snapshot that is no backup (incomplete, or not consistent). None when restic
    forgot it; else the words that name the snapshot left in the repository and how to forget it.

    The snapshot goes into the torn record first, and out again once restic forgot it: a stop waits
    until the record is written (its lock: TORN_LOCK_TIMEOUT at most), so a stop then or while forget
    waits for the repository's lock leaves it recorded, and the next NAS backup of the VM forgets it
    before its retention could count it as a backup. A stop after a record that failed says which
    snapshot is left, neither recorded nor forgotten."""
    snap, vm = entry["snapshot"], entry["vm"]
    unrecorded: str | None = None
    try:
        with p.signals.held():
            try:
                p.store.record_torn(entry)
            except (OSError, Refused) as e:
                unrecorded = str(e)
        failed = forget_snapshot(p, nas, snap)
    except Stop as e:
        if unrecorded is None:
            raise
        raise Stop(
            f"{e}; restic snapshot {snap[:8]} is no backup and could NOT be recorded ({unrecorded}) or forgotten: "
            f"forget it by hand before the next backup of {vm}, whose retention would count it: restic forget "
            f'{snap}, with the repository\'s variables of vm/README.md, section "Restore from the NAS"'
        ) from None
    if failed is None:
        if unrecorded is None:
            try:
                p.store.drop_torn([entry])
            except (OSError, Refused) as e:
                p.out.say(
                    f"  note: restic snapshot {snap[:8]} is forgotten but still in {cfg.paths.torn} ({e}): "
                    f"the next NAS backup of {vm} drops it"
                )
        return None
    left = (
        f"restic snapshot {snap[:8]} could NOT be forgotten ({failed}): it is no "
        f"backup; forget it by hand, restic forget {snap}, with the repository's variables of vm/README.md, "
        'section "Restore from the NAS"'
    )
    if unrecorded is None:
        return f"{left}; recorded in {cfg.paths.torn}: the next NAS backup of {vm} forgets it before its retention"
    return (
        f"{left}; it could NOT be recorded ({unrecorded}): forget it by hand before the next backup of {vm}, "
        "whose retention would count it"
    )


def retry_torn(p: NasBackup, vm: str, nas: NasRepo) -> str | None:
    """Forget the torn snapshots of vm in this repository that an earlier run recorded and could not
    forget, before the retention. A recorded id alone is never enough: restic must still list exactly
    one snapshot of vm's tags that it names (prefix either way), made on the host the entry recorded
    (restic lists a snapshot only under its own host, and NAS_RESTIC_HOST can change), and that one is
    forgotten by its listed id. One that restic no longer lists that way is left alone and dropped from
    the record. Returns why the retention must wait (an entry kept, the listing or the record not
    usable), else None. With no such entry it runs nothing."""
    try:
        mine = [e for e in p.store.read_torn() if e["vm"] == vm and e["repository"] == nas["repo"]]
    except (OSError, Refused) as e:
        return f"the record of torn snapshots cannot be read ({e})"
    if not mine:
        return None
    kept: list[str] = []
    done: list[TornEntry] = []
    for host in sorted({e["host"] for e in mine}):
        listed = nas_snapshots(p.restic, vm, nas, host)
        if listed is None:
            on = "" if host == nas["host"] else f" made on host {host}"
            kept.append(f"restic snapshots did not list the snapshots of {vm}{on}, so the torn ones were not forgotten")
            continue
        for e in mine:
            if e["host"] != host:
                continue
            why = forget_listed(p, vm, nas, e, listed)
            if why is None:
                done.append(e)
            else:
                kept.append(why)
    if done:
        try:
            p.store.drop_torn(done)
        except (OSError, Refused) as e:
            kept.append(f"the record of torn snapshots was not updated ({e})")
    return "; ".join(kept) or None


def forget_listed(p: NasBackup, vm: str, nas: NasRepo, e: TornEntry, listed: Sequence[ResticSnapshot]) -> str | None:
    """Forget the listed snapshot the torn entry e names (retry_torn); None when the entry is done with
    (forgotten now, or restic no longer lists it: forgotten already), else why it stays. A listed one
    without both tags is never forgotten: it stays, and the retention waits."""
    short = e["snapshot"][:8]
    found = [s for s in listed if s["id"].startswith(e["snapshot"]) or e["snapshot"].startswith(s["id"])]
    if len(found) > 1:
        return f"the torn snapshot {short} names {len(found)} listed snapshots"
    if not found:
        p.out.say(
            f"  torn restic snapshot {short}: restic no longer lists it among the {TAG} snapshots of {vm} "
            "(forgotten already): dropped from the record"
        )
        return None
    tags = found[0].get("tags")
    if not isinstance(tags, list) or TAG not in tags or f"vm:{vm}" not in tags:
        return f"restic lists the torn snapshot {short} without both tags {TAG} and vm:{vm}, so it was not forgotten"
    snap = found[0]["id"]
    if (failed := forget_snapshot(p, nas, snap)) is not None:
        return f"restic forget {snap[:8]} (torn, of an earlier run): {failed}"
    p.out.say(f"  torn restic snapshot {snap[:8]} of an earlier run forgotten")
    return None


def forget_snapshot(p: NasBackup, nas: NasRepo, snap: str) -> str | None:
    """restic forget of the one snapshot snap (waiting up to RETRY_LOCK for the repository's lock):
    None when restic forgot it, else why not (restic's first error line, or its exit status)."""
    rc, _, err = p.restic.run(nas["env"], ["forget", "--retry-lock", RETRY_LOCK, snap], RESTIC_FORGET_TIMEOUT)
    return None if rc == 0 else first_line(err) or f"exit {rc}"


def retention_after_torn(cfg: VmSettings, p: NasBackup, vm: str, nas: NasRepo) -> None:
    """The retention, once the torn snapshots of earlier runs are forgotten (retry_torn); skipped when
    one is not: a torn snapshot would take a good backup's place among those kept."""
    why = retry_torn(p, vm, nas)
    if why is None:
        retention(cfg, p, vm, nas)
    else:
        p.out.say(f"  retention skipped: {why}; the repository keeps more until the next backup")


def retention(cfg: VmSettings, p: NasBackup, vm: str, nas: NasRepo) -> None:
    weekly, monthly = p.settings.number("VM_NAS_KEEP_WEEKLY"), p.settings.number("VM_NAS_KEEP_MONTHLY")
    if weekly + monthly == 0:
        p.out.say(
            f"  retention: VM_NAS_KEEP_WEEKLY and VM_NAS_KEEP_MONTHLY are 0 in {cfg.paths.vm_conf}: nothing forgotten"
        )
        return
    rc, so, err = p.restic.run(nas["env"], forget_args(vm, nas["host"], weekly, monthly), RESTIC_FORGET_TIMEOUT)
    if rc != 0:
        p.out.say(
            f"  note: restic forget failed ({first_line(err) or f'exit {rc}'}): the backup is fine, the "
            "retention runs again next time"
        )
        return
    gone = len(re.findall(r"^remove \d+ snapshots?:", so, re.MULTILINE))
    p.out.say(
        f"  retention: {weekly} weekly + {monthly} monthly kept, and those tagged {KEEP_TAG}"
        f"{'; older ones forgotten' if gone else ''} (space is freed by the /home NAS job's weekly prune)"
    )

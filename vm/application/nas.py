# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup [VM] --nas`: the NAS backup with restic, through a live snapshot that is
merged back (blockcommit) whatever happens, and its retention. Moved from vm/cli/nas.py as it was;
it reaches the machine through the ports of vm/ports.py (NasBackup)."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os.path
import re

from cst_core.domain.errors import Refused
from cst_core.domain.text import first_line
from cst_core.domain.values import format_bytes_gib_mib
from cst_core.ports import ClockPort
from vm.application import domain_state
from vm.application.backup import check_backup_start, check_shut_off, backup_time
from vm.domain import (
    KEEP_TAG,
    LIVE_STATES,
    OVERLAY_MARK,
    RETRY_LOCK,
    TAG,
    DomainParts,
    NasRepo,
    VmSettings,
    agent_state,
    disk_label,
    domain_parts,
    forget_args,
    live_disks,
    overlay_kind,
    overlay_recovery,
)
from vm.domain.backup import Fingerprint, Manifest, ResticSummary, Uncertain, manifest_text
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
    rc, _, err = restic.run(nas["env"], ["cat", "config", "--no-lock"], 300)
    if rc == 0:
        return
    why = {
        127: "restic is not installed (it comes with the NAS setup; after rpm-ostree, active at the next reboot)",
        124: "the NAS did not answer within 5 minutes",
        10: f"no restic repository at {nas['repo']}",
        11: "the repository is locked (try again later)",
        12: f"{cfg.paths.restic_password} does not open the repository",
    }.get(rc, first_line(err) or f"restic exit {rc}")
    raise Refused(f"NAS repository not usable: {why}. Nothing was changed (no snapshot taken)")


def commit_back(
    cfg: VmSettings, p: NasBackup, vm: str, overlaid: Mapping[str, tuple[str, str]]
) -> tuple[list[str], dict[str, str]]:
    """virsh blockcommit --active --pivot for each overlay (also on a paused
    VM), then a check of the live and the persistent definition, then the
    overlay file removed. Returns (problems, {target: how to recover}); no
    problems: every disk is back on its image."""
    problems: list[str] = []
    how: dict[str, str] = {}
    state = domain_state_or_none(p.commands, vm)
    if state not in LIVE_STATES:
        problems.append(
            f"{vm} stopped during the backup: its disks still point to the overlay files"
            if state in ("shut off", "crashed")
            else f"the state of {vm} could not be read ({state or 'virsh domstate failed'}): nothing merged"
        )
        for t, (base, _overlay) in overlaid.items():
            how[t] = overlay_kind(state, None, base)
        return problems, how
    if state != "running":
        p.out.say(f"  {vm} is {state}: merging back anyway (a block job works on it)", flush=True)
    deadline = p.clock.monotonic() + cfg.commit_timeout
    for t, (base, _overlay) in overlaid.items():
        left = max(60, int(deadline - p.clock.monotonic()))
        p.out.say(f"  merging the overlay of {t} back into {base} (virsh blockcommit --active --pivot)", flush=True)
        # --timeout: virsh aborts the job itself (the disk stays on the overlay, no job
        # left half done) before the timer's TimeoutStopSec could kill it.
        rc, so, err = p.commands.virsh(
            ("blockcommit", vm, t, "--active", "--pivot", "--wait", "--verbose", "--timeout", str(left)),
            left + 300,
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
        if any(named) or not named and p.store.exists(overlay):
            out[t] = (base, overlay)
    return out


def nas_sources(
    cfg: VmSettings, store: BackupStorePort, vm: str, parts: DomainParts, notes: list[str]
) -> list[tuple[str, str]]:
    """[(path, kind)] restic reads: the disks, the UEFI variables, the TPM folder."""
    out: list[tuple[str, str]] = []
    for p in parts.disks:
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
        out.append((r, "disk"))
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


def backup_nas(cfg: VmSettings, vm: str, keep: str | None, p: NasBackup, *, quiesce: bool) -> None:
    """Back vm up to the NAS (keep: --keep-tag; quiesce: try the guest agent first)."""
    check_backup_start(cfg, p.store, "backup --nas", root=p.root)
    if "," in vm or not vm.strip():
        raise Refused(f"VM name {vm!r}: a comma cannot be part of a restic tag")
    nas = p.settings.nas_repo()
    state = domain_state(p.commands, vm)
    if state not in ("running", "shut off"):
        raise Refused(
            f"{vm} is {state or 'in an unknown state'}: the NAS backup reads a running VM (through a "
            f"snapshot) or a shut-off one. Resume it or shut it down first"
        )
    if state == "shut off":
        check_shut_off(p.commands, vm)
    p.signals.catch_stops()
    with p.lock(vm):
        nas_run(cfg, p, vm, keep, nas, quiesce=quiesce, live=state == "running")


def nas_run(
    cfg: VmSettings, p: NasBackup, vm: str, keep: str | None, nas: NasRepo, *, quiesce: bool, live: bool
) -> None:
    store, out = p.store, p.out
    rc, xml_text, err = p.commands.virsh(("dumpxml", "--inactive", "--security-info", vm), 60, detach=None)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    parts = domain_parts(xml_text)
    if parts.problems:
        raise Refused(f"{vm}: nothing backed up:\n  " + "\n  ".join(parts.problems))
    if not parts.disks:
        raise Refused(f"{vm}: no file disk in its definition: nothing to back up")
    notes = list(parts.skipped)
    sources = nas_sources(cfg, store, vm, parts, notes)
    epoch, stamp = backup_time(cfg, p.clock)
    specs: dict[str, tuple[str, str]] = {}
    others: list[str] = []  # target -> (image, overlay); targets left out of the snapshot
    agent = "none"
    if live:
        rc, live_xml, err = p.commands.virsh(("dumpxml", vm), 60, detach=None)
        if rc != 0:
            raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
        disks = live_disks(live_xml)
        for d in disks:
            if d.device != "disk" or d.kind != "file":
                others.append(d.target)
                continue
            if OVERLAY_MARK in d.source or d.backing:
                raise Refused(
                    f"{vm}: {disk_label(d.target, word='disk ')} runs on {d.source} with a backing chain (an overlay "
                    f"left by an earlier backup?). Put it back first; `{cfg.cst} doctor` shows the commands"
                )
            base = store.realpath(d.source)
            specs[d.target] = (
                base,
                os.path.join(os.path.dirname(base), f"{os.path.basename(base)}{OVERLAY_MARK}{stamp}"),
            )
        if sorted(b for b, _ in specs.values()) != sorted(src for src, k in sources if k == "disk"):
            raise Refused(
                f"{vm}: the running VM's disks differ from its definition (changed since it started): "
                "restart it, then back up"
            )
        for _t, (_, overlay) in specs.items():
            if store.lexists(overlay):
                raise Refused(f"{overlay} already exists: wait a second and run it again")
        agent = agent_state(live_xml)
    versions, deployment = p.commands.package_versions(), p.commands.booted_deployment()
    restic_ready(cfg, p.restic, nas)
    out.say(f"{vm}: backup to the NAS ({nas['repo']}), {'running: live snapshot' if live else 'shut off'}", flush=True)
    overlaid: dict[str, tuple[str, str]] = {}
    consistency = "shut-off"
    failure: BaseException | None = None
    before: dict[str, Fingerprint] = {}
    snap = ""
    summary: ResticSummary = {}
    try:
        if live:
            # The UEFI variables and the TPM state have no snapshot: read live, checked after.
            before = {src: store.fingerprint(src) for src, k in sources if k != "disk"}
            consistency = take_snapshot(cfg, p, vm, stamp, specs, others, quiesce, agent, overlaid, notes)
        else:
            before = {src: store.fingerprint(src) for src, _ in sources}
        tags = [TAG, f"vm:{vm}"] + ([KEEP_TAG, keep] if keep else [])
        manifest: Manifest = {
            "vm": vm,
            "uuid": parts.uuid,
            "timestamp": stamp,
            "epoch": int(epoch),
            "mode": "nas",
            "consistency": consistency,
            "keep": keep,
            "tags": tags,
            "files": [
                {"source": src, "kind": k, "allocated": store.sizes(src)[0], "apparent": store.sizes(src)[1]}
                for src, k in sources
            ],
            "versions": versions,
            "deployment": deployment,
            "notes": notes,
        }
        stage = store.stage(vm, xml_text, manifest_text(manifest))
        paths = [src for src, _ in sources] + [stage]
        timeout = p.settings.number("VM_NAS_TIMEOUT_HOURS") * 3600 or 6 * 3600
        for src, k in sources:
            a, s = store.sizes(src)
            out.say(
                f"  reading {src} ({k}, {format_bytes_gib_mib(a, mib_decimals=1)} allocated, {format_bytes_gib_mib(s, mib_decimals=1)} apparent)"
            )
        try:
            rc, summary = p.restic.backup(
                nas["env"],
                [
                    "backup",
                    "--json",
                    "--host",
                    nas["host"],
                    "--tag",
                    ",".join(tags),
                    "--retry-lock",
                    RETRY_LOCK,
                    *paths,
                ],
                timeout,
                out,
            )
        finally:
            store.unstage(stage)
        snap = str(summary.get("snapshot_id") or "")
        if rc != 0 or not snap:
            if snap:
                p.restic.run(
                    nas["env"], ["forget", "--retry-lock", RETRY_LOCK, snap], 1800
                )  # incomplete (rc 3): not a backup
            why = {
                3: "some files could not be read",
                124: "stopped at the time limit (VM_NAS_TIMEOUT_HOURS)",
                127: "restic not found",
                11: "the repository stayed locked",
            }.get(rc, f"restic exit {rc}")
            raise Refused(f"restic backup failed: {why}; no backup recorded")
        if live:
            for src in before:
                try:
                    same = store.fingerprint(src) == before[src]
                except OSError:
                    same = False
                if not same:
                    notes.append(
                        f"{src} changed while restic read it (the VM wrote it): that copy may be torn; "
                        "the next backup reads it again"
                    )
        if not live:
            try:
                check_shut_off(p.commands, vm)
            except Refused as e:
                p.restic.run(nas["env"], ["forget", "--retry-lock", RETRY_LOCK, snap], 1800)
                raise Refused(
                    f"the VM's state changed during the backup, the copy is not consistent "
                    f"(restic snapshot {snap[:8]} forgotten): {e}"
                ) from None

            def now_fp(path: str) -> Fingerprint | None:
                try:
                    return store.fingerprint(path)
                except OSError:
                    return None

            changed = [src for src, _ in sources if now_fp(src) != before[src]]
            if changed:
                p.restic.run(nas["env"], ["forget", "--retry-lock", RETRY_LOCK, snap], 1800)
                raise Refused(
                    "changed during the backup (the VM was started?), the copy is not consistent "
                    f"(restic snapshot {snap[:8]} forgotten): {', '.join(changed)}"
                )
    except BaseException as e:  # noqa: BLE001 - the overlays are merged back first, then it is raised again
        failure = e
    # From here on no signal may cut the cleanup short, and no child may get one.
    problems: list[str] = []
    how: dict[str, str] = {}
    with p.signals.ignored(), p.commands.detached():
        if live:
            # What the VM really runs on, not only what take_snapshot saw.
            try:
                overlaid.update(find_overlays(p, vm, specs))
            except Exception as e:  # noqa: BLE001 - the merge back still runs for what is known
                problems.append(f"the disks could not be listed: {e!r}")
        if overlaid:
            try:
                more, how = commit_back(cfg, p, vm, overlaid)
                problems += more
            except Exception as e:  # noqa: BLE001 - reported loudly below
                problems.append(f"the merge back failed: {e!r}")
    if isinstance(failure, Uncertain) and not problems:
        problems.append(str(failure))
        overlaid.update({t: spec for t, spec in specs.items() if store.lexists(spec[1])})
    if problems:
        body = (
            f"{vm}: a disk was not merged back after the NAS backup, or may not be: "
            + "; ".join(problems)
            + f". Check what the VM runs on; see `{cfg.cst} doctor`."
        )
        p.alert("VM backup: disk left on a snapshot overlay", body)
        if not overlaid:  # nothing known: the files this run could have made
            overlaid.update({t: spec for t, spec in specs.items() if store.lexists(spec[1])})
        lines = overlay_recovery(vm, overlaid.items(), how)
        msg = (
            "DISK NOT MERGED BACK:\n  "
            + "\n  ".join(problems)
            + "\nNothing was undone or removed. Put each disk back by hand (read each line first):\n  "
            + "\n  ".join(lines)
        )
        if failure is not None and not isinstance(failure, Uncertain):
            msg += f"\n(the backup itself: {failure})"
        raise Refused(msg)
    if failure is not None:
        raise failure
    out.say(
        f"  restic snapshot {snap[:8]}: {format_bytes_gib_mib(summary.get('total_bytes_processed') or 0, mib_decimals=1)} read, "
        f"{format_bytes_gib_mib(summary.get('data_added') or 0, mib_decimals=1)} new on the NAS ({consistency})"
    )
    retention(cfg, p, vm, nas)
    entries = [e for e in store.read_index() if not (e.get("mode") == "nas" and e.get("snapshot") == snap)]
    entries.append(
        {
            "vm": vm,
            "timestamp": stamp,
            "epoch": int(epoch),
            "mode": "nas",
            "snapshot": snap,
            "repository": nas["repo"],
            "host": nas["host"],
            "consistency": consistency,
            "keep": keep,
            "versions": versions,
            "deployment": deployment,
        }
    )
    alive = nas_snapshot_ids(p.restic, vm, nas)
    if alive is not None:
        alive.add(snap)
        entries = [
            e
            for e in entries
            if not (
                e["vm"] == vm
                and e.get("mode") == "nas"
                and e.get("repository") == nas["repo"]
                and not any(a.startswith(e["snapshot"]) or e["snapshot"].startswith(a) for a in alive)
            )
        ]
    entries.sort(key=lambda e: (e["vm"], e.get("epoch") or 0))
    store.write_index(entries)
    for n in notes:
        out.say(f"  note: {n}")
    out.say(
        f"Done: restic snapshot {snap[:8]} of {vm} ({consistency}"
        f"{f'; kept until removed by hand: tags {KEEP_TAG},{keep}' if keep else ''})."
    )
    out.say(
        'Restore: vm/README.md in CST, section "Restore from the NAS" (restic restore '
        f"{snap[:8]} --sparse --target <a NOCOW folder>)."
    )


def take_snapshot(
    cfg: VmSettings,
    p: NasBackup,
    vm: str,
    stamp: str,
    specs: Mapping[str, tuple[str, str]],
    others: Sequence[str],
    quiesce: bool,
    agent: str,
    overlaid: dict[str, tuple[str, str]],
    notes: list[str],
) -> str:
    """The external disk-only snapshot; fills overlaid {target: (image,
    overlay)} from what domblklist shows, also after a failure. Returns the
    consistency: quiesced or crash-consistent. Raises Uncertain when virsh
    did not answer and libvirt's result stayed unclear."""

    def attempt(with_quiesce: bool) -> tuple[int, str, bool]:
        args = ["snapshot-create-as", vm, "--name", f"cst-nas-{stamp}", "--disk-only", "--atomic", "--no-metadata"]
        if with_quiesce:
            args.append("--quiesce")
        for t, (_, overlay) in specs.items():
            args += ["--diskspec", f"{t},snapshot=external,file={overlay.replace(',', ',,')}"]
        for t in others:
            args += ["--diskspec", f"{t},snapshot=no"]
        # Signals wait until the outcome is known (virsh is never killed by a stop);
        # its own session keeps a Ctrl-C or a closed terminal away from it.
        with p.signals.held():
            rc, so, err = p.commands.virsh(args, cfg.snapshot_timeout, detach=True)
            settled = rc != 124 and rc >= 0  # not a timeout, and virsh not killed by a signal
            deadline = p.clock.monotonic() + (0 if settled else cfg.snapshot_settle)
            while True:
                # The truth is what the VM runs on, also when virsh failed or timed out.
                cur, _ = blklist(p.commands, vm)
                for t, (base, overlay) in specs.items():
                    if (cur.get(t) == overlay) if cur is not None else p.store.exists(overlay):
                        overlaid[t] = (base, overlay)
                if settled or len(overlaid) == len(specs) or p.clock.monotonic() >= deadline:
                    break
                p.clock.sleep(5)
        if not settled and len(overlaid) != len(specs):
            if with_quiesce:
                p.commands.virsh(("domfsthaw", vm), 60, detach=None)  # best effort: never leave the guest frozen
            kept = ", ".join(o for t, (_, o) in specs.items() if t not in overlaid and p.store.lexists(o))
            raise Uncertain(
                f"virsh snapshot-create-as {vm} did not answer within {cfg.snapshot_timeout} s and, "
                f"{cfg.snapshot_settle} s later, the VM's disks still do not all show the overlay: libvirt "
                f"may still switch to it. Nothing was removed{f' (kept: {kept})' if kept else ''}"
            )
        if not settled and with_quiesce:
            p.commands.virsh(("domfsthaw", vm), 60, detach=None)
            notes.append(
                "virsh did not answer in time; the snapshot is in place, but whether the guest was frozen is unknown"
            )
            return 0, "", True
        return rc, first_line(err) or first_line(so) or f"exit {rc}", False

    def drop_strays() -> None:
        # Only after a definite error from libvirt: those files are not in use.
        for t, (_, overlay) in specs.items():
            if t not in overlaid:
                p.store.drop(overlay)

    why = (
        "--no-quiesce"
        if not quiesce
        else "no guest agent channel (org.qemu.guest_agent.0) in the definition"
        if agent == "none"
        else "the guest agent is not connected"
        if agent != "connected"
        else ""
    )
    if not why:
        rc, err, unsure = attempt(True)
        if overlaid:
            if rc != 0 or unsure:
                if rc != 0:
                    notes.append(f"virsh reported an error but the snapshot is in place: {err}")
                    p.commands.virsh(("domfsthaw", vm), 60, detach=None)  # best effort: never leave the guest frozen
                return "crash-consistent"
            p.out.say("  snapshot taken with the guest's file systems frozen (quiesced, QEMU guest agent)")
            return "quiesced"
        drop_strays()
        why = f"quiesce failed: {err}"
    p.out.say(f"  crash-consistent snapshot ({why})", flush=True)
    rc, err, _ = attempt(False)
    if not overlaid:
        drop_strays()
        raise Refused(f"virsh snapshot-create-as {vm}: {err} (nothing changed)")
    if rc != 0:
        notes.append(f"virsh reported an error but the snapshot is in place: {err}")
    notes.append(f"crash-consistent: {why}")
    return "crash-consistent"


def nas_snapshot_ids(restic: ResticPort, vm: str, nas: NasRepo) -> set[str] | None:
    """The ids of the VM's restic snapshots, or None when not readable."""
    rc, so, _ = restic.run(
        nas["env"], ["snapshots", "--json", "--no-lock", "--host", nas["host"], "--tag", f"{TAG},vm:{vm}"], 300
    )
    try:
        return {s["id"] for s in json.loads(so) if isinstance(s, dict) and s.get("id")} if rc == 0 else None
    except ValueError, TypeError:
        return None


def retention(cfg: VmSettings, p: NasBackup, vm: str, nas: NasRepo) -> None:
    weekly, monthly = p.settings.number("VM_NAS_KEEP_WEEKLY"), p.settings.number("VM_NAS_KEEP_MONTHLY")
    if weekly + monthly == 0:
        p.out.say(
            f"  retention: VM_NAS_KEEP_WEEKLY and VM_NAS_KEEP_MONTHLY are 0 in {cfg.paths.vm_conf}: nothing forgotten"
        )
        return
    rc, so, err = p.restic.run(nas["env"], forget_args(vm, nas["host"], weekly, monthly), 1800)
    if rc != 0:
        p.out.say(
            f"  note: restic forget failed ({first_line(err) or f'exit {rc}'}): the backup is fine, the "
            "retention runs again next time"
        )
        return
    gone = len(re.findall(r"^remove \d+ snapshots?:", so, re.M))
    p.out.say(
        f"  retention: {weekly} weekly + {monthly} monthly kept, and those tagged {KEEP_TAG}"
        f"{'; older ones forgotten' if gone else ''} (space is freed by the /home NAS job's weekly prune)"
    )

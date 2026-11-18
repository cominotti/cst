# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup [VM] --nas`: the NAS backup with restic, through a live snapshot that is
merged back (blockcommit) whatever happens, and its retention."""

from collections.abc import Mapping, Sequence
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Any

from cst_core.domain.errors import Refused
from cst_core.domain.text import first_line
from cst_core.domain.values import format_bytes_gib_mib
from vm import application, wiring
from vm.adapters import (
    COMMANDS_OF_VM,
    blklist,
    booted_deployment,
    conf_number,
    domain_state_or_none,
    nas_settings,
    package_versions,
    restic,
    restic_env,
    virsh,
)
from vm.adapters.backup import (
    STOP_SIGNALS,
    VmLock,
    fingerprint,
    notify,
    on_signal,
    qcow2_backing_file,
    check_backup_start,
    read_index,
    signals_held,
    tree_sizes,
    utc_now,
    write_index,
)
from vm.cli.backup import check_shut_off
from vm.domain import (
    VmSettings,
    DomainParts,
    KEEP_TAG,
    LIVE_STATES,
    OVERLAY_MARK,
    RETRY_LOCK,
    TAG,
    agent_state,
    disk_label,
    domain_parts,
    forget_args,
    live_disks,
    overlay_kind,
    overlay_recovery,
)


class Uncertain(Refused):
    """virsh snapshot-create-as did not answer in time and libvirt's result
    stayed unclear: the overlay files are kept, an alert is sent."""


def restic_ready(cfg: VmSettings, nas: Mapping[str, Any]) -> None:
    """Refused unless the repository opens (NAS reachable, password right):
    checked before any snapshot, so an unreachable NAS never leaves the VM
    on an overlay."""
    # --no-lock: it only proves the repository opens, also while the /home
    # NAS job holds the lock (its prune), which the backup waits for.
    rc, _, err = restic(nas, "cat", "config", "--no-lock", timeout=300)
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


def restic_backup(nas: Mapping[str, Any], args: Sequence[str], timeout: int) -> tuple[int, dict[str, Any]]:
    """restic backup --json: stderr goes through, the status lines become a
    progress line every minute, the summary is kept. (rc, summary or {})."""
    env = restic_env(nas["env"])
    try:
        p = subprocess.Popen(["restic", *args], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, env=env)
    except FileNotFoundError:
        return 127, {}
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    summary: dict[str, Any] = {}
    shown = 0.0
    try:
        assert p.stdout is not None
        for line in p.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                print(f"  restic: {line.rstrip()}", flush=True)
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("message_type")
            if kind == "summary":
                summary = msg
            elif kind == "status" and time.monotonic() - shown >= 60:
                shown = time.monotonic()
                done, total = msg.get("bytes_done") or 0, msg.get("total_bytes") or 0
                print(
                    f"  restic: {100 * float(msg.get('percent_done') or 0):.0f} % ({format_bytes_gib_mib(done, mib_decimals=1)} of "
                    f"{format_bytes_gib_mib(total, mib_decimals=1)} read)",
                    flush=True,
                )
            elif kind == "error":
                err = msg.get("error") or {}
                print(f"  restic error: {msg.get('item') or ''} {err.get('message') or err}", file=sys.stderr)
        rc = p.wait()
    except BaseException:
        p.terminate()
        try:
            p.wait(60)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
        raise
    finally:
        timer.cancel()
    if rc == -signal.SIGKILL:
        rc = 124
    return rc, summary


def commit_back(cfg: VmSettings, vm: str, overlaid: Mapping[str, tuple[str, str]]) -> tuple[list[str], dict[str, str]]:
    """virsh blockcommit --active --pivot for each overlay (also on a paused
    VM), then a check of the live and the persistent definition, then the
    overlay file removed. Returns (problems, {target: how to recover}); no
    problems: every disk is back on its image."""
    problems: list[str] = []
    how: dict[str, str] = {}
    state = domain_state_or_none(vm)
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
        print(f"  {vm} is {state}: merging back anyway (a block job works on it)", flush=True)
    deadline = time.monotonic() + cfg.commit_timeout
    for t, (base, _overlay) in overlaid.items():
        left = max(60, int(deadline - time.monotonic()))
        print(f"  merging the overlay of {t} back into {base} (virsh blockcommit --active --pivot)", flush=True)
        # --timeout: virsh aborts the job itself (the disk stays on the overlay, no job
        # left half done) before the timer's TimeoutStopSec could kill it.
        rc, so, err = virsh(
            "blockcommit",
            vm,
            t,
            "--active",
            "--pivot",
            "--wait",
            "--verbose",
            "--timeout",
            str(left),
            timeout=left + 300,
        )
        if rc != 0:
            problems.append(f"{t}: virsh blockcommit: {first_line(err) or first_line(so) or f'exit {rc}'}")
    live, lerr = blklist(vm)
    persistent, perr = blklist(vm, inactive=True)
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
            try:
                os.unlink(overlay)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"  note: {overlay}: not removed ({e.strerror}); {t} is back on {base}: remove it by hand")
            continue
        how[t] = overlay_kind(state, live_src, base)
    return problems, how


def find_overlays(vm: str, specs: Mapping[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    """{target: (image, overlay)} of this run's overlays that the running VM
    or its definition names, or whose file exists when neither is readable."""
    live, _ = blklist(vm)
    persistent, _ = blklist(vm, inactive=True)
    out: dict[str, tuple[str, str]] = {}
    for t, (base, overlay) in specs.items():
        named = [m.get(t) == overlay for m in (live, persistent) if m is not None]
        if any(named) or not named and os.path.exists(overlay):
            out[t] = (base, overlay)
    return out


def stage_files(cfg: VmSettings, vm: str, xml_text: str, manifest: Mapping[str, Any]) -> str:
    """domain.xml (with --security-info: graphics passwords; restic stores it
    encrypted) and manifest.json, 0600 in a 0700 folder. Returns the folder."""
    d = os.path.join(cfg.paths.nas_stage, vm)
    for x in (cfg.paths.nas_stage, d):
        os.makedirs(x, mode=0o700, exist_ok=True)
        os.chmod(x, 0o700)
    for name, text in (
        ("domain.xml", xml_text),
        ("manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n"),
    ):
        path = os.path.join(d, name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(text)
    return d


def unstage(d: str) -> None:
    for name in ("domain.xml", "manifest.json"):
        try:
            os.unlink(os.path.join(d, name))
        except OSError:
            pass


def nas_sources(cfg: VmSettings, vm: str, parts: DomainParts, notes: list[str]) -> list[tuple[str, str]]:
    """[(path, kind)] restic reads: the disks, the UEFI variables, the TPM folder."""
    out: list[tuple[str, str]] = []
    for p in parts.disks:
        if not os.path.isfile(p):
            raise Refused(f"{vm}: disk {p} not found")
        if OVERLAY_MARK in p:
            raise Refused(
                f"{vm}: disk {p} is an overlay a NAS backup left behind (its blockcommit did not "
                f"finish). Put the disk back first; `{cfg.cst} doctor` shows the commands"
            )
        r = os.path.realpath(p)
        if r != p:
            notes.append(f"disk {p} is a symbolic link: read through it: {r}")
        if qcow2_backing_file(r):
            raise Refused(
                f"{vm}: {r} is a qcow2 overlay with a backing file: back up the chain by hand "
                "(qemu-img info --backing-chain)"
            )
        out.append((r, "disk"))
    if parts.nvram:
        if os.path.isfile(parts.nvram):
            out.append((os.path.realpath(parts.nvram), "nvram"))
        else:
            notes.append(f"UEFI variables {parts.nvram} not there yet: not backed up")
    if parts.tpm:
        tpm_dir = os.path.join(cfg.paths.swtpm_dir, parts.uuid)
        if parts.uuid and os.path.isdir(tpm_dir):
            out.append((os.path.realpath(tpm_dir), "tpm"))
        else:
            notes.append(
                f"TPM state {tpm_dir} not found: not backed up (BitLocker may then ask for its "
                "recovery key after a restore)"
            )
    return out


def cmd_backup_nas(cfg: VmSettings, vm: str, keep: str | None, quiesce: bool) -> int:
    check_backup_start(cfg, "backup --nas")
    if "," in vm or not vm.strip():
        raise Refused(f"VM name {vm!r}: a comma cannot be part of a restic tag")
    nas = nas_settings(cfg)
    state = application.domain_state(wiring.vm_commands(), vm)
    if state not in ("running", "shut off"):
        raise Refused(
            f"{vm} is {state or 'in an unknown state'}: the NAS backup reads a running VM (through a "
            f"snapshot) or a shut-off one. Resume it or shut it down first"
        )
    if state == "shut off":
        check_shut_off(vm)
    for sig in STOP_SIGNALS:
        signal.signal(sig, on_signal)
    with VmLock(vm, cfg.paths.run_dir):
        return nas_run(cfg, vm, keep, quiesce, nas, state == "running")


def nas_run(cfg: VmSettings, vm: str, keep: str | None, quiesce: bool, nas: Mapping[str, Any], live: bool) -> int:
    rc, xml_text, err = virsh("dumpxml", "--inactive", "--security-info", vm)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    parts = domain_parts(xml_text)
    if parts.problems:
        raise Refused(f"{vm}: nothing backed up:\n  " + "\n  ".join(parts.problems))
    if not parts.disks:
        raise Refused(f"{vm}: no file disk in its definition: nothing to back up")
    notes = list(parts.skipped)
    sources = nas_sources(cfg, vm, parts, notes)
    epoch, stamp = utc_now(cfg.now)
    specs: dict[str, tuple[str, str]] = {}
    others: list[str] = []  # target -> (image, overlay); targets left out of the snapshot
    agent = "none"
    if live:
        rc, live_xml, err = virsh("dumpxml", vm)
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
            base = os.path.realpath(d.source)
            specs[d.target] = (
                base,
                os.path.join(os.path.dirname(base), f"{os.path.basename(base)}{OVERLAY_MARK}{stamp}"),
            )
        if sorted(b for b, _ in specs.values()) != sorted(p for p, k in sources if k == "disk"):
            raise Refused(
                f"{vm}: the running VM's disks differ from its definition (changed since it started): "
                "restart it, then back up"
            )
        for _t, (_, overlay) in specs.items():
            if os.path.lexists(overlay):
                raise Refused(f"{overlay} already exists: wait a second and run it again")
        agent = agent_state(live_xml)
    versions, deployment = package_versions(), booted_deployment()
    restic_ready(cfg, nas)
    print(f"{vm}: backup to the NAS ({nas['repo']}), {'running: live snapshot' if live else 'shut off'}", flush=True)
    overlaid: dict[str, tuple[str, str]] = {}
    consistency = "shut-off"
    failure: BaseException | None = None
    before: dict[str, tuple[Any, ...]] = {}
    snap = ""
    summary: dict[str, Any] = {}
    try:
        if live:
            # The UEFI variables and the TPM state have no snapshot: read live, checked after.
            before = {p: fingerprint(p) for p, k in sources if k != "disk"}
            consistency = take_snapshot(cfg, vm, stamp, specs, others, quiesce, agent, overlaid, notes)
        else:
            before = {p: fingerprint(p) for p, _ in sources}
        tags = [TAG, f"vm:{vm}"] + ([KEEP_TAG, keep] if keep else [])
        manifest = {
            "vm": vm,
            "uuid": parts.uuid,
            "timestamp": stamp,
            "epoch": int(epoch),
            "mode": "nas",
            "consistency": consistency,
            "keep": keep,
            "tags": tags,
            "files": [
                {"source": p, "kind": k, "allocated": tree_sizes(p)[0], "apparent": tree_sizes(p)[1]}
                for p, k in sources
            ],
            "versions": versions,
            "deployment": deployment,
            "notes": notes,
        }
        stage = stage_files(cfg, vm, xml_text, manifest)
        paths = [p for p, _ in sources] + [stage]
        timeout = conf_number(cfg.paths, "VM_NAS_TIMEOUT_HOURS") * 3600 or 6 * 3600
        for p, k in sources:
            a, s = tree_sizes(p)
            print(
                f"  reading {p} ({k}, {format_bytes_gib_mib(a, mib_decimals=1)} allocated, {format_bytes_gib_mib(s, mib_decimals=1)} apparent)"
            )
        try:
            rc, summary = restic_backup(
                nas,
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
            )
        finally:
            unstage(stage)
        snap = str(summary.get("snapshot_id") or "")
        if rc != 0 or not snap:
            if snap:
                restic(nas, "forget", "--retry-lock", RETRY_LOCK, snap)  # incomplete (rc 3): not a backup
            why = {
                3: "some files could not be read",
                124: "stopped at the time limit (VM_NAS_TIMEOUT_HOURS)",
                127: "restic not found",
                11: "the repository stayed locked",
            }.get(rc, f"restic exit {rc}")
            raise Refused(f"restic backup failed: {why}; no backup recorded")
        if live:
            for p in before:
                try:
                    same = fingerprint(p) == before[p]
                except OSError:
                    same = False
                if not same:
                    notes.append(
                        f"{p} changed while restic read it (the VM wrote it): that copy may be torn; "
                        "the next backup reads it again"
                    )
        if not live:
            try:
                check_shut_off(vm)
            except Refused as e:
                restic(nas, "forget", "--retry-lock", RETRY_LOCK, snap)
                raise Refused(
                    f"the VM's state changed during the backup, the copy is not consistent "
                    f"(restic snapshot {snap[:8]} forgotten): {e}"
                ) from None

            def now_fp(p: str) -> tuple[Any, ...] | None:
                try:
                    return fingerprint(p)
                except OSError:
                    return None

            changed = [p for p, _ in sources if now_fp(p) != before[p]]
            if changed:
                restic(nas, "forget", "--retry-lock", RETRY_LOCK, snap)
                raise Refused(
                    "changed during the backup (the VM was started?), the copy is not consistent "
                    f"(restic snapshot {snap[:8]} forgotten): {', '.join(changed)}"
                )
    except BaseException as e:  # noqa: BLE001 - the overlays are merged back first, then it is raised again
        failure = e
    # From here on no signal may cut the cleanup short, and no child may get one.
    old = {s: signal.signal(s, signal.SIG_IGN) for s in STOP_SIGNALS}
    problems: list[str] = []
    how: dict[str, str] = {}
    try:
        with COMMANDS_OF_VM.detached():
            if live:
                # What the VM really runs on, not only what take_snapshot saw.
                try:
                    overlaid.update(find_overlays(vm, specs))
                except Exception as e:  # noqa: BLE001 - the merge back still runs for what is known
                    problems.append(f"the disks could not be listed: {e!r}")
            if overlaid:
                try:
                    more, how = commit_back(cfg, vm, overlaid)
                    problems += more
                except Exception as e:  # noqa: BLE001 - reported loudly below
                    problems.append(f"the merge back failed: {e!r}")
    finally:
        for s, h in old.items():
            if h is not None:
                signal.signal(s, h)
    if isinstance(failure, Uncertain) and not problems:
        problems.append(str(failure))
        overlaid.update({t: spec for t, spec in specs.items() if os.path.lexists(spec[1])})
    if problems:
        body = (
            f"{vm}: a disk was not merged back after the NAS backup, or may not be: "
            + "; ".join(problems)
            + f". Check what the VM runs on; see `{cfg.cst} doctor`."
        )
        notify(cfg, "critical", "VM backup: disk left on a snapshot overlay", body)
        if not overlaid:  # nothing known: the files this run could have made
            overlaid.update({t: spec for t, spec in specs.items() if os.path.lexists(spec[1])})
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
    print(
        f"  restic snapshot {snap[:8]}: {format_bytes_gib_mib(summary.get('total_bytes_processed') or 0, mib_decimals=1)} read, "
        f"{format_bytes_gib_mib(summary.get('data_added') or 0, mib_decimals=1)} new on the NAS ({consistency})"
    )
    retention(cfg, vm, nas)
    entries = [e for e in read_index(cfg) if not (e.get("mode") == "nas" and e.get("snapshot") == snap)]
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
    alive = nas_snapshot_ids(vm, nas)
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
    write_index(entries, cfg.paths.index)
    for n in notes:
        print(f"  note: {n}")
    print(
        f"Done: restic snapshot {snap[:8]} of {vm} ({consistency}"
        f"{f'; kept until removed by hand: tags {KEEP_TAG},{keep}' if keep else ''})."
    )
    print(
        'Restore: vm/README.md in CST, section "Restore from the NAS" (restic restore '
        f"{snap[:8]} --sparse --target <a NOCOW folder>)."
    )
    return 0


def take_snapshot(
    cfg: VmSettings,
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
        with signals_held():
            rc, so, err = virsh(*args, timeout=cfg.snapshot_timeout, detach=True)
            settled = rc != 124 and rc >= 0  # not a timeout, and virsh not killed by a signal
            deadline = time.monotonic() + (0 if settled else cfg.snapshot_settle)
            while True:
                # The truth is what the VM runs on, also when virsh failed or timed out.
                cur, _ = blklist(vm)
                for t, (base, overlay) in specs.items():
                    if (cur.get(t) == overlay) if cur is not None else os.path.exists(overlay):
                        overlaid[t] = (base, overlay)
                if settled or len(overlaid) == len(specs) or time.monotonic() >= deadline:
                    break
                time.sleep(5)
        if not settled and len(overlaid) != len(specs):
            if with_quiesce:
                virsh("domfsthaw", vm)  # best effort: never leave the guest frozen
            kept = ", ".join(o for t, (_, o) in specs.items() if t not in overlaid and os.path.lexists(o))
            raise Uncertain(
                f"virsh snapshot-create-as {vm} did not answer within {cfg.snapshot_timeout} s and, "
                f"{cfg.snapshot_settle} s later, the VM's disks still do not all show the overlay: libvirt "
                f"may still switch to it. Nothing was removed{f' (kept: {kept})' if kept else ''}"
            )
        if not settled and with_quiesce:
            virsh("domfsthaw", vm)
            notes.append(
                "virsh did not answer in time; the snapshot is in place, but whether the guest was frozen is unknown"
            )
            return 0, "", True
        return rc, first_line(err) or first_line(so) or f"exit {rc}", False

    def drop_strays() -> None:
        # Only after a definite error from libvirt: those files are not in use.
        for t, (_, overlay) in specs.items():
            if t not in overlaid:
                try:
                    os.unlink(overlay)
                except OSError:
                    pass

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
                    virsh("domfsthaw", vm)  # best effort: never leave the guest frozen
                return "crash-consistent"
            print("  snapshot taken with the guest's file systems frozen (quiesced, QEMU guest agent)")
            return "quiesced"
        drop_strays()
        why = f"quiesce failed: {err}"
    print(f"  crash-consistent snapshot ({why})", flush=True)
    rc, err, _ = attempt(False)
    if not overlaid:
        drop_strays()
        raise Refused(f"virsh snapshot-create-as {vm}: {err} (nothing changed)")
    if rc != 0:
        notes.append(f"virsh reported an error but the snapshot is in place: {err}")
    notes.append(f"crash-consistent: {why}")
    return "crash-consistent"


def nas_snapshot_ids(vm: str, nas: Mapping[str, Any]) -> set[str] | None:
    """The ids of the VM's restic snapshots, or None when not readable."""
    rc, so, _ = restic(
        nas, "snapshots", "--json", "--no-lock", "--host", nas["host"], "--tag", f"{TAG},vm:{vm}", timeout=300
    )
    try:
        return {s["id"] for s in json.loads(so) if isinstance(s, dict) and s.get("id")} if rc == 0 else None
    except ValueError, TypeError:
        return None


def retention(cfg: VmSettings, vm: str, nas: Mapping[str, Any]) -> None:
    weekly, monthly = conf_number(cfg.paths, "VM_NAS_KEEP_WEEKLY"), conf_number(cfg.paths, "VM_NAS_KEEP_MONTHLY")
    if weekly + monthly == 0:
        print(
            f"  retention: VM_NAS_KEEP_WEEKLY and VM_NAS_KEEP_MONTHLY are 0 in {cfg.paths.vm_conf}: nothing forgotten"
        )
        return
    rc, so, err = restic(nas, *forget_args(vm, nas["host"], weekly, monthly))
    if rc != 0:
        print(
            f"  note: restic forget failed ({first_line(err) or f'exit {rc}'}): the backup is fine, the "
            "retention runs again next time"
        )
        return
    gone = len(re.findall(r"^remove \d+ snapshots?:", so, re.M))
    print(
        f"  retention: {weekly} weekly + {monthly} monthly kept, and those tagged {KEEP_TAG}"
        f"{'; older ones forgotten' if gone else ''} (space is freed by the /home NAS job's weekly prune)"
    )

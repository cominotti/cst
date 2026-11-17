"""The `cst vm` command line (the program behind vm/cst-vm): backup, the NAS
backup and its weekly timer, list, status, tune, nocow.

A driving adapter: argv in, the rules of vm.domain applied to what
vm.adapters reads from the host, the answer printed.
"""

from collections.abc import Mapping, Sequence
import difflib
import functools
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import xml.etree.ElementTree as ET

from cst_core.adapters.files import owned_file_state_at, write_bytes_atomic
from cst_core.adapters.runtime import install_payload
from cst_core.domain.errors import Refused, Usage
from cst_core.domain.libvirt_xml import keep_prefixes
from cst_core.domain.text import first_line
from cst_core.domain.values import format_bytes_gib_mib
from vm.adapters import (
    BACKUP_DIR,
    COMMIT_TIMEOUT,
    CST,
    DETACH,
    INDEX,
    NAS_CONF,
    NAS_STAGE,
    PROG,
    RESTIC_CACHE,
    RESTIC_PASSWORD,
    SBIN_COPY,
    SNAPSHOT_SETTLE,
    SNAPSHOT_TIMEOUT,
    STOP_SIGNALS,
    SWTPM_DIR,
    Stop,
    UNIT_DIR,
    VM_CONF,
    VmLock,
    blklist,
    booted_deployment,
    conf_number,
    confirm,
    cpu_disable_list,
    default_vm,
    describe_entry,
    domain_state,
    domain_state_or_none,
    is_ours,
    fingerprint,
    flag_state,
    has_managed_save,
    image_dirs,
    readable_inode_flags,
    knobs,
    needs_mark,
    nocow,
    notify,
    on_signal,
    package_versions,
    prepare_command,
    precreate_nocow,
    probe_dir,
    qcow2_backing_file,
    read_conf,
    read_index,
    restic,
    run,
    safe_output,
    mark_folder_nocow,
    signals_held,
    systemctl,
    tree_sizes,
    utc_now,
    virsh,
    wants_links,
    write_index,
)
from vm.domain import (
    FULL_MARGIN,
    KEEP_TAG,
    KNOBS,
    LIVE_STATES,
    NAS_SSH_ALIAS,
    ON_CALENDAR,
    OVERLAY_MARK,
    RETRY_LOCK,
    SERVICE,
    TAG,
    TIMER,
    URI,
    WINDOWS_STEPS,
    agent_state,
    convert_lines,
    domain_parts,
    forget_args,
    guest_kind,
    index_entry,
    instance,
    live_disks,
    normalized,
    overlay_kind,
    overlay_recovery,
    parse_args,
    render_service,
    render_timer,
    restore_lines,
    stored,
    timer_file_words,
    tune_domain,
    version_changes,
)

USAGE = (
    f"usage: {PROG} backup [VM] [--full] | backup [VM] --nas [--keep-tag NAME] [--no-quiesce] | "
    f"backup-timer on|off|status [VM] | list [VM] | status [VM] | "
    f"tune plan|apply [VM] [--guest-has-virtio] | nocow plan [--root] | nocow apply | help"
)


def copy(src: str, dest: str, mode: str, big: bool) -> None:
    """Copy src (a file or a folder) to dest with cp -a. Disks (big) follow
    the mode; the small parts (UEFI variables, TPM state) are plain copies."""
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    if not big:
        flags = ["--reflink=never"] if mode == "full" else []
    elif mode == "reflink":
        flags = ["--reflink=always"]  # cp allows --reflink only with --sparse=auto
        if os.path.isfile(src) and nocow(src):
            precreate_nocow(dest)
    else:
        # --sparse=auto keeps the source's holes only. --sparse=always also turns
        # every run of zero blocks inside the data into a hole: a 92 GiB image
        # became 1.05 million extents (the source had 0.6 million), and the guest
        # writing into those holes allocates new extents even on a NOCOW file.
        flags = ["--reflink=never", "--sparse=auto"]
    rc, _, err = run(["cp", *flags, "-a", "--", src, dest], timeout=6 * 3600)
    if rc != 0:
        raise Refused(
            f"cp {' '.join(flags)} {src}: {first_line(err) or f'exit {rc}'}"
            + (
                f"\nReflink copies need the backup folder on the same btrfs filesystem as the "
                f"disks. Use real copies instead: {PROG} backup VM --full"
                if big and mode == "reflink"
                else ""
            )
        )


def check_shut_off(vm: str) -> None:
    """Refused unless the VM is shut off AND has no managed-save image: after
    `virsh managedsave` (virt-manager's Save) domstate also says "shut off",
    but the guest's disks are only consistent with the saved RAM."""
    rc, so, err = virsh("domstate", vm)
    if rc != 0:
        raise Refused(f"virsh domstate {vm}: {first_line(err) or f'exit {rc}'}")
    state = first_line(so)
    if state != "shut off":
        raise Refused(
            f"{vm} is {state or 'in an unknown state'}: shut it down first (a copy of a running "
            f"VM is not consistent). From Windows, or: virsh -c {URI} shutdown {vm}"
        )
    if has_managed_save(vm):
        raise Refused(
            f"{vm} was saved (managed save), not shut down: its disks are only consistent with "
            f"the saved memory. Start it and shut Windows down, then run the backup again "
            f"(or drop the saved memory: virsh -c {URI} managedsave-remove {vm})"
        )


def cmd_backup(vm: str, full: bool) -> int:
    if os.geteuid() != 0 and not os.environ.get("CST_VM_NONROOT_OK"):
        raise Refused("backup needs root: run it as `cst vm backup` (it calls sudo by itself)")
    check_shut_off(vm)
    rc, xml_text, err = virsh("dumpxml", "--inactive", "--security-info", vm)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    parts = domain_parts(xml_text)
    if parts["problems"]:
        raise Refused(f"{vm}: nothing copied:\n  " + "\n  ".join(parts["problems"]))
    if not parts["disks"]:
        raise Refused(f"{vm}: no file disk in its definition: nothing to back up")
    notes = list(parts["skipped"])

    def real(path: str, what: str) -> str:
        # cp -a copies a symbolic link as a link (no data): the file it points
        # to is what gets copied, and restored, instead.
        r = os.path.realpath(path)
        if r != path:
            notes.append(f"{what} {path} is a symbolic link: copied (and restored) through it: {r}")
        return r

    sources: list[tuple[str, bool]] = []
    for p in parts["disks"]:
        if not os.path.isfile(p):
            raise Refused(f"{vm}: disk {p} not found")
        p = real(p, "disk")
        if qcow2_backing_file(p):
            raise Refused(
                f"{vm}: {p} is a qcow2 overlay with a backing file: back up the chain by hand "
                "(qemu-img info --backing-chain)"
            )
        if (p, True) not in sources:
            sources.append((p, True))
    if parts["nvram"]:
        if os.path.isfile(parts["nvram"]):
            sources.append((real(parts["nvram"], "UEFI variables"), False))
        else:
            notes.append(f"UEFI variables {parts['nvram']} not there yet (the VM never started?): not copied")
    tpm_dir = ""
    if parts["tpm"]:
        tpm_dir = os.path.join(SWTPM_DIR, parts["uuid"])
        if parts["uuid"] and os.path.isdir(tpm_dir):
            tpm_dir = real(tpm_dir, "TPM state")
            sources.append((tpm_dir, False))
        else:
            notes.append(
                f"TPM state {tpm_dir} not found: not copied (BitLocker may then ask for its "
                "recovery key after a restore)"
            )
    mode = "full" if full else "reflink"
    epoch, stamp = utc_now()
    vm_dir = os.path.join(BACKUP_DIR, vm)
    dest_dir = os.path.join(vm_dir, stamp)
    if os.path.exists(dest_dir):
        raise Refused(f"{dest_dir} already exists: wait a second and run it again")
    sizes = {p: tree_sizes(p) for p, _ in sources}
    if full:
        need = sum(a for a, _ in sizes.values())
        margin = max(FULL_MARGIN[0], int(need * FULL_MARGIN[1]))
        probe = BACKUP_DIR
        while not os.path.exists(probe):
            probe = os.path.dirname(probe)
        st = os.statvfs(probe)
        free = st.f_bavail * st.f_frsize
        if free < need + margin:
            raise Refused(
                f"--full needs {format_bytes_gib_mib(need, mib_decimals=1)} (allocated size) + {format_bytes_gib_mib(margin, mib_decimals=1)} margin, but "
                f"{probe} has {format_bytes_gib_mib(free, mib_decimals=1)} free: free space first, or use the default reflink copy"
            )
    for d in (BACKUP_DIR, vm_dir):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    os.mkdir(dest_dir, 0o700)
    print(f"{vm}: {mode} backup to {dest_dir}")
    copied: list[tuple[str, bool]] = []
    try:
        before = {p: fingerprint(p) for p, _ in sources}
        for src, big in sources:
            a, s = sizes[src]
            print(
                f"  copying {src} ({format_bytes_gib_mib(a, mib_decimals=1)} allocated, {format_bytes_gib_mib(s, mib_decimals=1)} apparent)",
                flush=True,
            )
            copy(src, stored(dest_dir, src), mode, big)
            copied.append((src, big))
        # Nothing stops the VM from starting during a long copy: check again,
        # and that no source changed while it was read.
        try:
            check_shut_off(vm)
        except Refused as e:
            raise Refused(f"the VM's state changed during the copy, the copy is not consistent: {e}") from e

        def now_fp(p: str) -> tuple[Any, ...] | None:
            try:
                return fingerprint(p)
            except OSError:
                return None  # removed during the copy: changed as well

        changed = [p for p, _ in sources if now_fp(p) != before[p]]
        if changed:
            raise Refused(
                "changed during the copy (the VM was started?), the copy is not consistent: "
                + ", ".join(changed)
                + ". Shut the VM off and run the backup again"
            )
        # The definition holds --security-info secrets (graphics passwords): 0600 from the start.
        with os.fdopen(
            os.open(os.path.join(dest_dir, "domain.xml"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as f:
            f.write(xml_text)
        manifest = {
            "vm": vm,
            "uuid": parts["uuid"],
            "timestamp": stamp,
            "epoch": int(epoch),
            "mode": mode,
            "files": [
                {
                    "source": src,
                    "stored": os.path.relpath(stored(dest_dir, src), dest_dir),
                    "kind": "disk" if big else ("tpm" if src == tpm_dir else "nvram"),
                    "allocated": sizes[src][0],
                    "apparent": sizes[src][1],
                }
                for src, big in copied
            ],
            "versions": package_versions(),
            "deployment": booted_deployment(),
            "notes": notes,
        }
        with os.fdopen(
            os.open(os.path.join(dest_dir, "manifest.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
        ) as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
            f.write("\n")
    except BaseException:
        shutil.rmtree(dest_dir, ignore_errors=True)
        print(f"  incomplete backup removed: {dest_dir}", file=sys.stderr)
        raise
    # Entries whose folder was removed by hand leave the index here.
    entries = [e for e in read_index() if e.get("mode") == "nas" or os.path.isdir(e["path"]) and e["path"] != dest_dir]
    entries.append(index_entry(manifest, dest_dir))
    entries.sort(key=lambda e: (e["vm"], e.get("epoch") or 0))
    write_index(entries)
    for n in notes:
        print(f"  note: {n}")
    print(f"Done: {len(copied)} item(s), definition and manifest in {dest_dir}")
    if mode == "reflink":
        print(
            "Reflink copies share disk blocks with the VM: they protect against changes to the image "
            "(a repair tool, an upgrade), not against losing the disks. For that: --full, or a copy "
            "on another machine."
        )
    print("\nTo restore (never done automatically; read each line first):")
    for line in restore_lines(vm, dest_dir, copied, tpm_dir):
        print(f"  {line}")
    print(
        f"\nRemove an old backup by hand: sudo rm -r {shlex.quote(vm_dir)}/TIMESTAMP "
        f"(the next backup drops it from {CST} vm list)"
    )
    return 0


class Uncertain(Refused):
    """virsh snapshot-create-as did not answer in time and libvirt's result
    stayed unclear: the overlay files are kept, an alert is sent."""


def nas_settings() -> dict[str, Any]:
    """{host, env, repo} for restic, from the /home NAS backup's settings.
    Refused when that backup is not set up: its setup creates the
    repository, the SSH key and the password file this one reuses."""
    conf = read_conf(NAS_CONF, bash_sourced=True)
    setup = f"{CST} btrfs snapshots nas setup"
    if conf.get("NAS_ENABLED") != "1" or not conf.get("NAS_PATH"):
        raise Refused(
            f"the NAS backup is not set up ({NAS_CONF}: NAS_ENABLED=1 and NAS_PATH are needed). "
            f"Set it up first: {setup} (the VM backup shares its restic repository)"
        )
    try:
        if os.path.getsize(RESTIC_PASSWORD) == 0:
            raise OSError
    except OSError:
        raise Refused(
            f"{RESTIC_PASSWORD} is missing or empty: run {setup} again (or put the saved repository password there)"
        ) from None
    host = conf.get("NAS_RESTIC_HOST") or socket.gethostname().split(".")[0]
    repo = f"sftp:{NAS_SSH_ALIAS}:{conf['NAS_PATH']}"
    return {
        "host": host,
        "repo": repo,
        "env": {"RESTIC_REPOSITORY": repo, "RESTIC_PASSWORD_FILE": RESTIC_PASSWORD, "RESTIC_CACHE_DIR": RESTIC_CACHE},
    }


def restic_ready(nas: Mapping[str, Any]) -> None:
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
        12: f"{RESTIC_PASSWORD} does not open the repository",
    }.get(rc, first_line(err) or f"restic exit {rc}")
    raise Refused(f"NAS repository not usable: {why}. Nothing was changed (no snapshot taken)")


def restic_backup(nas: Mapping[str, Any], args: Sequence[str], timeout: int) -> tuple[int, dict[str, Any]]:
    """restic backup --json: stderr goes through, the status lines become a
    progress line every minute, the summary is kept. (rc, summary or {})."""
    env = {**os.environ, "LC_ALL": "C", **nas["env"]}
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


def commit_back(vm: str, overlaid: Mapping[str, tuple[str, str]]) -> tuple[list[str], dict[str, str]]:
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
    deadline = time.monotonic() + COMMIT_TIMEOUT
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


def stage_files(vm: str, xml_text: str, manifest: Mapping[str, Any]) -> str:
    """domain.xml (with --security-info: graphics passwords; restic stores it
    encrypted) and manifest.json, 0600 in a 0700 folder. Returns the folder."""
    d = os.path.join(NAS_STAGE, vm)
    for x in (NAS_STAGE, d):
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


def nas_sources(vm: str, parts: Mapping[str, Any], notes: list[str]) -> list[tuple[str, str]]:
    """[(path, kind)] restic reads: the disks, the UEFI variables, the TPM folder."""
    out: list[tuple[str, str]] = []
    for p in parts["disks"]:
        if not os.path.isfile(p):
            raise Refused(f"{vm}: disk {p} not found")
        if OVERLAY_MARK in p:
            raise Refused(
                f"{vm}: disk {p} is an overlay a NAS backup left behind (its blockcommit did not "
                f"finish). Put the disk back first; `{CST} doctor` shows the commands"
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
    if parts["nvram"]:
        if os.path.isfile(parts["nvram"]):
            out.append((os.path.realpath(parts["nvram"]), "nvram"))
        else:
            notes.append(f"UEFI variables {parts['nvram']} not there yet: not backed up")
    if parts["tpm"]:
        tpm_dir = os.path.join(SWTPM_DIR, parts["uuid"])
        if parts["uuid"] and os.path.isdir(tpm_dir):
            out.append((os.path.realpath(tpm_dir), "tpm"))
        else:
            notes.append(
                f"TPM state {tpm_dir} not found: not backed up (BitLocker may then ask for its "
                "recovery key after a restore)"
            )
    return out


def cmd_backup_nas(vm: str, keep: str | None, quiesce: bool) -> int:
    if os.geteuid() != 0 and not os.environ.get("CST_VM_NONROOT_OK"):
        raise Refused("backup --nas needs root: run it as `cst vm backup --nas` (it calls sudo by itself)")
    if "," in vm or not vm.strip():
        raise Refused(f"VM name {vm!r}: a comma cannot be part of a restic tag")
    nas = nas_settings()
    state = domain_state(vm)
    if state not in ("running", "shut off"):
        raise Refused(
            f"{vm} is {state or 'in an unknown state'}: the NAS backup reads a running VM (through a "
            f"snapshot) or a shut-off one. Resume it or shut it down first"
        )
    if state == "shut off":
        check_shut_off(vm)
    for sig in STOP_SIGNALS:
        signal.signal(sig, on_signal)
    with VmLock(vm):
        return nas_run(vm, keep, quiesce, nas, state == "running")


def nas_run(vm: str, keep: str | None, quiesce: bool, nas: Mapping[str, Any], live: bool) -> int:
    rc, xml_text, err = virsh("dumpxml", "--inactive", "--security-info", vm)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    parts = domain_parts(xml_text)
    if parts["problems"]:
        raise Refused(f"{vm}: nothing backed up:\n  " + "\n  ".join(parts["problems"]))
    if not parts["disks"]:
        raise Refused(f"{vm}: no file disk in its definition: nothing to back up")
    notes = list(parts["skipped"])
    sources = nas_sources(vm, parts, notes)
    epoch, stamp = utc_now()
    specs: dict[str, tuple[str, str]] = {}
    others: list[str] = []  # target -> (image, overlay); targets left out of the snapshot
    agent = "none"
    if live:
        rc, live_xml, err = virsh("dumpxml", vm)
        if rc != 0:
            raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
        disks = live_disks(live_xml)
        for d in disks:
            if d["device"] != "disk" or d["type"] != "file":
                others.append(d["target"])
                continue
            if OVERLAY_MARK in d["source"] or d["backing"]:
                raise Refused(
                    f"{vm}: disk {d['target']} runs on {d['source']} with a backing chain (an overlay "
                    f"left by an earlier backup?). Put it back first; `{CST} doctor` shows the commands"
                )
            base = os.path.realpath(d["source"])
            specs[d["target"]] = (
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
    restic_ready(nas)
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
            consistency = take_snapshot(vm, stamp, specs, others, quiesce, agent, overlaid, notes)
        else:
            before = {p: fingerprint(p) for p, _ in sources}
        tags = [TAG, f"vm:{vm}"] + ([KEEP_TAG, keep] if keep else [])
        manifest = {
            "vm": vm,
            "uuid": parts["uuid"],
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
        stage = stage_files(vm, xml_text, manifest)
        paths = [p for p, _ in sources] + [stage]
        timeout = conf_number("VM_NAS_TIMEOUT_HOURS") * 3600 or 6 * 3600
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
    DETACH["on"] = True
    problems: list[str] = []
    how: dict[str, str] = {}
    try:
        if live:
            # What the VM really runs on, not only what take_snapshot saw.
            try:
                overlaid.update(find_overlays(vm, specs))
            except Exception as e:  # noqa: BLE001 - the merge back still runs for what is known
                problems.append(f"the disks could not be listed: {e!r}")
        if overlaid:
            try:
                more, how = commit_back(vm, overlaid)
                problems += more
            except Exception as e:  # noqa: BLE001 - reported loudly below
                problems.append(f"the merge back failed: {e!r}")
    finally:
        DETACH["on"] = False
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
            + f". Check what the VM runs on; see `{CST} doctor`."
        )
        notify("critical", "VM backup: disk left on a snapshot overlay", body)
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
    retention(vm, nas)
    entries = [e for e in read_index() if not (e.get("mode") == "nas" and e.get("snapshot") == snap)]
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
    write_index(entries)
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
            rc, so, err = virsh(*args, timeout=SNAPSHOT_TIMEOUT, detach=True)
            settled = rc != 124 and rc >= 0  # not a timeout, and virsh not killed by a signal
            deadline = time.monotonic() + (0 if settled else SNAPSHOT_SETTLE)
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
                f"virsh snapshot-create-as {vm} did not answer within {SNAPSHOT_TIMEOUT} s and, "
                f"{SNAPSHOT_SETTLE} s later, the VM's disks still do not all show the overlay: libvirt "
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


def retention(vm: str, nas: Mapping[str, Any]) -> None:
    weekly, monthly = conf_number("VM_NAS_KEEP_WEEKLY"), conf_number("VM_NAS_KEEP_MONTHLY")
    if weekly + monthly == 0:
        print(f"  retention: VM_NAS_KEEP_WEEKLY and VM_NAS_KEEP_MONTHLY are 0 in {VM_CONF}: nothing forgotten")
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


def stop_timeout() -> str:
    """A stop may arrive while the snapshot call is held (SNAPSHOT_TIMEOUT +
    SNAPSHOT_SETTLE), then the merge runs (COMMIT_TIMEOUT, plus virsh's own
    300 s), then the checks and the alert: 30 minutes of margin."""
    return f"{(SNAPSHOT_TIMEOUT + SNAPSHOT_SETTLE + COMMIT_TIMEOUT + 300 + 1800 + 59) // 60}min"


def timer_files(entry_file: str) -> dict[str, tuple[bytes, int]]:
    """{path: (wanted bytes, mode)}: the program as `install` writes it (a
    zipapp; entry_file is the running cst-vm), the service and the timer."""
    return {
        SBIN_COPY: (install_payload("vm/cst-vm", entry_file), 0o755),
        os.path.join(UNIT_DIR, SERVICE): (render_service(SBIN_COPY, stop_timeout()).encode(), 0o644),
        os.path.join(UNIT_DIR, TIMER): (render_timer().encode(), 0o644),
    }


def cmd_timer(sub: str, vm: str, entry_file: str) -> int:
    unit = instance(vm)
    if sub != "status" and os.geteuid() != 0 and not os.environ.get("CST_VM_NONROOT_OK"):
        raise Refused(f"backup-timer {sub} needs root: run it as `{PROG} backup-timer {sub}` (it calls sudo)")
    files = timer_files(entry_file)
    states = {p: owned_file_state_at(p, t, functools.partial(is_ours, p), owner="root") for p, (t, _) in files.items()}
    if sub == "status":
        on = unit in wants_links()
        print(f"weekly NAS backup of {vm}: {'on' if on else 'off'} ({unit}; {ON_CALENDAR}, up to 30 min later)")
        words = timer_file_words(PROG, vm)
        for p, st in states.items():
            print(f"  {p}: {words[st]}")
        if on:
            rc, nxt, _ = systemctl("show", "-p", "NextElapseUSecRealtime", "--value", unit)
            rc2, last, _ = systemctl("show", "-p", "Result", "--value", unit.replace(".timer", ".service"))
            if rc == 0 and nxt:
                print(f"  next run: {nxt}")
            if rc2 == 0 and last:
                print(f"  last run of the service: {last}")
        nas = [e for e in read_index() if e["vm"] == vm and e.get("mode") == "nas"]
        print(f"  last NAS backup: {describe_entry(max(nas, key=lambda e: e.get('epoch') or 0)) if nas else 'none'}")
        conf = read_conf(NAS_CONF, bash_sourced=True)
        if conf.get("NAS_ENABLED") != "1" or not conf.get("NAS_PATH"):
            print(f"  the NAS backup is not set up: {CST} btrfs snapshots nas setup")
        return 0
    foreign = [p for p, st in states.items() if st == "conflict"]
    if foreign:
        raise Refused("not CST's files, left as they are: " + ", ".join(foreign))
    if sub == "on":
        nas_settings()
        for p, (text, mode) in files.items():
            if states[p] != "ok":
                write_bytes_atomic(p, text, mode=mode)
                print(f"  {'updated' if states[p] == 'change' else 'installed'}: {p}")
        for args in (("daemon-reload",), ("enable", "--now", unit)):
            rc, _, err = systemctl(*args)
            if rc != 0:
                raise Refused(f"systemctl {' '.join(args)}: {err or f'exit {rc}'}")
        print(
            f"On: {unit} ({ON_CALENDAR}, then {CST} vm backup {vm} --nas). Run one now with: "
            f"sudo systemctl start {unit.replace('.timer', '.service')}"
        )
        return 0
    rc, _, err = systemctl("disable", "--now", unit)
    if rc != 0 and unit in wants_links():
        raise Refused(f"systemctl disable --now {unit}: {err or f'exit {rc}'}")
    # Forget the timer's last run (Persistent=: /var/lib/systemd/timers/stamp-UNIT): needs the unit loaded
    # and stopped, so before its files go. A failure only leaves that small file behind.
    systemctl("clean", "--what=state", unit)
    print(f"Off: {unit}")
    if not wants_links():
        for p, st in states.items():
            if st != "missing":
                os.unlink(p)
                print(f"  removed: {p}")
        systemctl("daemon-reload")
    else:
        print(f"  kept the units and {SBIN_COPY}: still used by {', '.join(wants_links())}")
    print(f"The backups on the NAS stay (list: {CST} vm list).")
    return 0


def cmd_list(vm: str | None) -> int:
    entries = [e for e in read_index() if vm is None or e["vm"] == vm]
    if not entries:
        print(f"no VM backups{f' of {vm}' if vm else ''} in {INDEX} (make one: {CST} vm backup)")
        return 0
    for e in sorted(entries, key=lambda e: (e["vm"], e.get("epoch") or 0)):
        print(describe_entry(e))
    return 0


def cmd_status(vm: str) -> int:
    rc, so, err = virsh("domstate", vm)
    if rc == 127:
        print(f"{vm}: virsh not found (libvirt not installed here)")
    elif rc != 0:
        print(f"{vm}: state not readable ({first_line(err) or f'exit {rc}'})")
    else:
        print(f"{vm}: {first_line(so)}")
        rc, xml_text, err = virsh("dumpxml", "--inactive", vm)
        try:
            cpu = ET.fromstring(xml_text).find("cpu") if rc == 0 else None
        except ET.ParseError:
            cpu = None
        names = cpu_disable_list()
        if cpu is None:
            print("  cpu: no <cpu> element (or not readable)")
        else:
            mode = cpu.get("mode", "custom")
            off = {f.get("name") for f in cpu.findall("feature") if f.get("policy") == "disable"}
            missing = [n for n in names if n not in off]
            if mode != "host-passthrough":
                print(f"  cpu mode {mode}: the CET/MBEC disables are only for host-passthrough")
            elif not names:
                print(f"  cpu mode {mode}: CPU features not managed (VM_CPU_DISABLE is empty)")
            elif missing:
                print(
                    f"  cpu mode {mode}: NOT disabled: {' '.join(missing)} (Windows may bootloop under "
                    f"QEMU 11; fix: {CST} cpu vm-pin plan)"
                )
            else:
                print(f"  cpu mode {mode}: disabled {' '.join(names)}")
        if rc == 0:
            print(f"  {tune_line(xml_text)}")
    entries = sorted((e for e in read_index() if e["vm"] == vm), key=lambda e: e.get("epoch") or 0)
    if not entries:
        print(
            f"  last backup: none (to the NAS, also while it runs: {CST} vm backup --nas; "
            f"a local copy with the VM shut off: {CST} vm backup)"
        )
    else:
        last = entries[-1]
        print(f"  last backup: {describe_entry(last)}")
        changes = version_changes(last.get("versions"), package_versions())
        if changes:
            print("  virtualization packages changed since then (back up again once the VM works on them):")
            for c in changes:
                print(f"    {c}")
        else:
            print("  virtualization packages: unchanged since the last backup")
    nas = [e for e in entries if e.get("mode") == "nas"]
    if nas and nas[-1] is not entries[-1]:
        print(f"  last NAS backup: {describe_entry(nas[-1])}")
    try:
        print(
            f"  weekly NAS backup timer: {'on' if instance(vm) in wants_links() else 'off'}"
            f" ({CST} vm backup-timer status {vm})"
        )
    except Refused:
        pass
    if knobs()["VM_NOCOW"]:
        print("VM image folders (NOCOW):")
        for d in image_dirs():
            for ln in nocow_lines(probe_dir(d), verbose=False):
                print(f"  {ln}")
    return 0


def nocow_lines(d: Mapping[str, Any], verbose: bool = True) -> list[str]:
    """Lines describing one probed folder (plan and status)."""
    head = f"{d['path']}  [{d['what']}]"
    if d.get("link") is not None:
        return [
            f"{head}: a symbolic link to {d['link']}: not managed by CST (set the mark on the real "
            f"folder by hand if it is on btrfs: chattr +C <folder>)"
        ]
    if not d["present"]:
        return [f"{head}: not present"]
    if d["btrfs"] is False:
        return [f"{head}: not on btrfs (NOCOW does not apply)"]
    out = [head]
    root_hint = f"{PROG} nocow plan --root"
    if d["nocow"] is None:
        out.append(f"  NOCOW mark (+C): not readable as your user (root-only folder): {root_hint}")
    else:
        out.append(
            "  NOCOW mark (+C): "
            + ("yes" if d["nocow"] else f"no: {PROG} nocow apply sets it (only files created later get it)")
        )
    if not d["listed"]:
        if d["nocow"] is not None or d["scope"] != "system":
            out.append("  images: folder not listable as your user")
        return out
    cow = [i for i in d["images"] if i["nocow"] is False]
    unknown = [i for i in d["images"] if i["nocow"] is None]
    ok = [i for i in d["images"] if i["nocow"]]
    if not d["images"]:
        out.append("  images: none")
    if ok:
        out.append(f"  NOCOW images: {', '.join(i['name'] for i in ok)}")
    if unknown:
        out.append(
            f"  images whose mark is not readable as your user: {', '.join(i['name'] for i in unknown)} ({root_hint})"
        )
    for i in cow:
        out.append(
            f"  still copy-on-write: {i['name']}  {format_bytes_gib_mib(i['alloc'], mib_decimals=1)} allocated, {format_bytes_gib_mib(i['size'], mib_decimals=1)} apparent"
        )
        if verbose:
            out.append(
                f"    to convert (every VM using it shut off; needs about {format_bytes_gib_mib(i['alloc'], mib_decimals=1)} free):"
            )
            out += [f"      {ln}" for ln in convert_lines(i, d["scope"] == "system")]
    for n in d["leftovers"]:
        out.append(f"  leftover of a conversion: {n} (remove it once the VM booted well from the new file)")
    return out


def cmd_nocow_plan(as_root: bool = False) -> int:
    if as_root and os.geteuid() != 0 and not os.environ.get("CST_VM_NONROOT_OK"):
        raise Refused(
            f"--root reads the system folder as root: run it as `{PROG} nocow plan --root` (it calls sudo by itself)"
        )
    if not knobs()["VM_NOCOW"]:
        print(f"VM_NOCOW=no in {VM_CONF}: the VM image folders are not managed.")
        return 0
    dirs = [probe_dir(d) for d in image_dirs()]
    for d in dirs:
        for ln in nocow_lines(d):
            print(ln)
    todo = [d for d in dirs if needs_mark(d)]
    print()
    if todo:
        print(
            f"Set the mark with: {PROG} nocow apply   (folders: "
            + ", ".join(
                d["path"]
                + (
                    " (not created yet)"
                    if not d["present"]
                    else " (mark not readable here: set again, harmless)"
                    if d["nocow"] is None
                    else ""
                )
                for d in todo
            )
            + ")"
        )
    else:
        print("No folder to mark.")
    print("Existing images are never converted by CST: the commands above are for you to run.")
    return 0


def mark_system_dir(d: Mapping[str, Any]) -> str:
    """Create (root:root 0711, as libvirt does) and mark the system folder:
    directly as root, else with sudo. Returns an error text or ""."""
    path, q = d["path"], shlex.quote(d["path"])
    if os.path.islink(path) or not d["present"] and os.path.lexists(path):
        return f"{path}: appeared or is a symbolic link: not touched"
    if os.geteuid() == 0:
        try:
            if not d["present"]:
                os.mkdir(path, 0o711)
                os.chmod(path, 0o711)
                run(["restorecon", path])
            mark_folder_nocow(path)
        except OSError as e:
            return f"{path}: {e.strerror}"
        return "" if flag_state(readable_inode_flags(path)) else f"{path}: the mark did not stick"
    steps: list[list[str]] = []
    if not d["present"]:
        steps += [["install", "-d", "-o", "root", "-g", "root", "-m", "0711", path], ["restorecon", path]]
    steps.append(["chattr", "+C", path])
    for cmd in steps:
        print(f"  sudo {' '.join(shlex.quote(c) for c in cmd)}", flush=True)
        p = subprocess.run(["sudo", "--", *cmd], check=False, env={**os.environ, "LC_ALL": "C"})
        if p.returncode != 0:
            return f"sudo {cmd[0]} {q}: exit {p.returncode}"
    rc, so, _ = run(["sudo", "--", "lsattr", "-d", path])
    attrs = so.split()[0] if rc == 0 and so.split() else ""
    return "" if "C" in attrs else f"{path}: the mark could not be confirmed (sudo lsattr -d {q})"


def cmd_nocow_apply() -> int:
    if not knobs()["VM_NOCOW"]:
        print(f"VM_NOCOW=no in {VM_CONF}: nothing to do.")
        return 0
    dirs = [probe_dir(d) for d in image_dirs()]
    todo = [d for d in dirs if needs_mark(d)]
    if not todo:
        print(
            "Every VM image folder present on btrfs already has the NOCOW mark (+C)"
            + (" (symbolic links are not managed)." if any(d.get("link") for d in dirs) else ".")
        )
        return 0
    for d in todo:
        what = "create (root:root 0711) and mark" if not d["present"] else "mark"
        unknown = " (its mark is not readable as your user: set again, harmless)" if d["nocow"] is None else ""
        print(f"  {what} +C: {d['path']}{' (sudo)' if d['scope'] == 'system' and os.geteuid() else ''}{unknown}")
    print(
        "Only files created there later get the mark; existing images stay as they are "
        f"({PROG} nocow plan shows how to convert them)."
    )
    if not confirm("Set the NOCOW mark on these folders?"):
        print("Nothing changed.")
        return 0
    errors: list[str] = []
    for d in todo:
        if d["scope"] == "system":
            err = mark_system_dir(d)
        else:
            try:
                mark_folder_nocow(d["path"])
                err = "" if flag_state(readable_inode_flags(d["path"])) else f"{d['path']}: the mark did not stick"
            except OSError as e:
                err = f"{d['path']}: {e.strerror}"
        if err:
            errors.append(err)
        else:
            print(f"  done: {d['path']}")
    if errors:
        raise Refused("not done:\n  " + "\n  ".join(errors))
    return 0


def cpu_owner_line(root: ET.Element) -> str:
    """How cst cpu vm-pin's part of the definition stands (reported, never changed here)."""
    cpu = root.find("cpu")
    mode = cpu.get("mode", "custom") if cpu is not None else "not set"
    names = cpu_disable_list()
    off = {f.get("name") for f in cpu.findall("feature") if f.get("policy") == "disable"} if cpu is not None else set()
    missing = [n for n in names if n not in off]
    pins = {c for p in root.findall("cputune/vcpupin") if (c := p.get("cpuset")) is not None}
    pin = f"vCPUs pinned to {', '.join(sorted(pins))}" if pins else "vCPUs not pinned"
    feat = (
        f"NOT disabled: {' '.join(missing)}"
        if mode == "host-passthrough" and missing
        else "CET/MBEC disabled"
        if mode == "host-passthrough" and names
        else f"cpu mode {mode}"
    )
    return f"CPU (owned by {CST} cpu vm-pin, not by tune): {feat}; {pin}; check: {CST} cpu vm-pin plan"


def cmd_tune(vm: str, apply: bool, guest_virtio: bool) -> int:
    if apply and os.geteuid() != 0 and not os.environ.get("CST_VM_NONROOT_OK"):
        raise Refused(f"tune apply needs root: run it as `{PROG} tune apply` (it calls sudo by itself)")
    state = domain_state(vm)
    # Without --security-info: what is printed holds no graphics passwords.
    rc, plain, err = virsh("dumpxml", "--inactive", vm)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    keep_prefixes(plain)
    root = ET.fromstring(plain)
    kind, why = guest_kind(root)
    allow_bus = kind == "linux" or guest_virtio
    kn = knobs()
    after, changes, held, notes = tune_domain(plain, kn, allow_bus)
    if after is None:
        raise Refused(f"{vm}: {notes[0]}")
    label = {"windows": "Windows", "linux": "Linux", "unknown": "not known"}[kind]
    print(f"{vm}: {state}; guest: {label} ({why})")
    off = [k for k in KNOBS[1:] if not kn[k]]
    if off:
        print(f"  skipped by {VM_CONF}: {', '.join(k + '=no' for k in off)}")
    for c in changes:
        print(f"  change: {c}")
    for h in held:
        print(f"  held back: {h}")
    for n in notes:
        print(f"  note: {n}")
    print(f"  {cpu_owner_line(root)}")
    if kind == "linux" and changes and any("bus" in c or "network card" in c for c in changes):
        print(
            "  note: Linux has the virtio drivers in its kernel; an initramfs built with only the needed "
            "drivers (hostonly) may lack virtio_scsi: check `lsinitrd | grep virtio` in the guest first."
        )
    if held:
        print(f"\nHeld back: the guest is {label}, and its virtio drivers are not known to be installed.")
        print(WINDOWS_STEPS.format(cst=CST, vm=vm))
    diff = list(difflib.unified_diff(normalized(plain).splitlines(True), after.splitlines(True), "current", "tuned"))
    if not diff:
        print(
            f"\n{vm}: disks and network card already as {CST} vm tune recommends"
            + (" (except what is held back above)." if held else ".")
        )
        return 0
    print()
    sys.stdout.writelines(diff)
    flag = " --guest-has-virtio" if guest_virtio else ""
    bus_change = any(" bus " in c or "network card" in c for c in changes)
    msaved = kind != "linux" and bus_change and state == "shut off" and has_managed_save(vm)
    if kind != "linux" and bus_change and (state != "shut off" or msaved):
        msg = (
            f"{vm} is {state}: for a guest that is not known to be Linux, the bus and network card "
            f"changes are applied only with the VM shut off (shut Windows down first)"
        )
        if msaved:
            msg = (
                f"{vm} was saved (managed save), not shut down: start it and shut Windows down fully "
                f"(`shutdown /s /t 0`), then apply; or drop the saved memory: "
                f"virsh -c {URI} managedsave-remove {vm}"
            )
        if not apply:
            print(f"\nPlan only. {msg}; then: {CST} vm tune apply {vm}{flag}")
            return 0
        raise Refused(msg)
    if not apply:
        print(f"\nPlan only. Apply with: {CST} vm tune apply {vm}{flag}   (effective at the VM's next start)")
        return 0
    # Define from the definition WITH its secrets (else graphics passwords are lost).
    rc, secret, err = virsh("dumpxml", "--inactive", "--security-info", vm)
    if rc != 0:
        raise Refused(f"virsh dumpxml --security-info {vm}: {first_line(err) or f'exit {rc}'}")
    s_after, s_changes, _, _ = tune_domain(secret, kn, allow_bus)
    if s_after is None or s_changes != changes:
        raise Refused(f"{vm}: the definition changed while planning: run it again")
    if not confirm(f"\nDefine {vm} with these changes (effective at its next start)?"):
        print("Nothing changed.")
        return 0
    _, stamp = utc_now()
    keep = os.path.join(BACKUP_DIR, vm, "definitions")
    for d in (BACKUP_DIR, os.path.join(BACKUP_DIR, vm), keep):
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    saved = os.path.join(keep, f"{stamp}.before-tune.xml")
    # Holds --security-info secrets (graphics passwords): 0600 from the start.
    with os.fdopen(os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        f.write(secret)
    fd, tmp = tempfile.mkstemp(prefix="cst-vm-tune.", suffix=".xml")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(s_after)
        rc, _, err = virsh("define", tmp)
    finally:
        os.unlink(tmp)
    if rc != 0:
        raise Refused(
            f"virsh define: {first_line(err) or f'exit {rc}'} (nothing changed; "
            f"the definition is as before, also saved in {saved})"
        )
    print(f"Defined. Effective at the next start of {vm}.")
    print(f"Previous definition saved in {saved} (restore: sudo virsh -c {URI} define {shlex.quote(saved)}).")
    rc, now, _ = virsh("dumpxml", "--inactive", vm)
    if rc == 0:
        _, left, _, _ = tune_domain(now, kn, allow_bus)
        if left:
            print("Note: libvirt changed the definition again: still different: " + "; ".join(left))
    return 0


def tune_line(xml_text: str) -> str:
    """One status line: whether the hardware matches tune (all changes, as if
    the drivers were there; Windows gets the hint to read the plan)."""
    try:
        _, changes, _, _ = tune_domain(xml_text, knobs(), True)
        kind, _ = guest_kind(ET.fromstring(xml_text))
    except ET.ParseError:
        return "hardware: definition not readable"
    if not changes:
        return "hardware: disks and network card as tune recommends"
    extra = " (Windows: virtio drivers first, the plan shows how)" if kind != "linux" else ""
    return f"hardware: {len(changes)} difference(s) from tune{extra}: {CST} vm tune plan"


def main(argv: Sequence[str], doc: str, entry_file: str) -> int:
    """`cst vm ...` (argv without the program name). doc is the program's help;
    entry_file the running cst-vm (backup-timer installs it)."""
    try:
        a = parse_args(argv)
    except Usage as e:
        print(f"{PROG}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    cmd, vm = a["cmd"], a["vm"]
    try:
        prepare_command(cmd, a["sub"], a["nas"], vm)
        if cmd == "help":
            print(doc)
            return 0
        if cmd == "list":
            return cmd_list(vm)
        if cmd == "nocow":
            return cmd_nocow_plan(a["root"]) if a["sub"] == "plan" else cmd_nocow_apply()
        vm = vm or default_vm()
        if cmd == "status":
            return cmd_status(vm)
        if cmd == "tune":
            return cmd_tune(vm, a["sub"] == "apply", a["guest_virtio"])
        if cmd == "backup-timer":
            return cmd_timer(a["sub"], vm, entry_file)
        safe_output()  # a closed terminal or pipe never cuts a backup or its cleanup short
        if a["nas"]:
            return cmd_backup_nas(vm, a["keep"], not a["no_quiesce"])
        return cmd_backup(vm, a["full"])
    except (Refused, Stop) as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        if cmd == "backup" and a["nas"] and os.environ.get("INVOCATION_ID") and "DISK NOT MERGED" not in str(e):
            # Started by systemd (the weekly timer): nobody reads the terminal.
            notify(
                "normal",
                f"Weekly VM backup of {vm} to the NAS failed",
                f"{first_line(str(e))[:300]} See: journalctl -u cst-vm-backup@{vm}.service",
            )
        return 1

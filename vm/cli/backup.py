# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup [VM] [--full]`: the local copy of a shut-off VM (reflink or full)."""

import json
import os
import shlex
import shutil
import sys
from typing import Any

from cst_core.domain.errors import Refused
from cst_core.domain.text import first_line
from cst_core.domain.values import format_bytes_gib_mib
from vm.adapters import (
    BACKUP_DIR,
    CST,
    PROG,
    SWTPM_DIR,
    booted_deployment,
    fingerprint,
    has_managed_save,
    nocow,
    package_versions,
    precreate_nocow,
    qcow2_backing_file,
    check_backup_start,
    read_index,
    run,
    tree_sizes,
    utc_now,
    virsh,
    write_index,
)
from vm.domain import FULL_MARGIN, URI, domain_parts, index_entry, restore_lines, stored


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
    check_backup_start("backup")
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

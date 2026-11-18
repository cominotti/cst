# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup [VM] [--full]`: the local copy of a shut-off VM (reflink or full), and the checks
every backup makes before it copies anything (also the NAS backup's, vm.application.nas)."""

from dataclasses import dataclass
import os.path
import posixpath
import shlex

from cst_core.domain.errors import Refused
from cst_core.domain.text import first_line
from cst_core.domain.values import format_bytes_gib_mib
from cst_core.ports import ClockPort
from vm.application import domain_state, has_managed_save
from vm.domain import FULL_MARGIN, URI, VmSettings, domain_parts, restore_lines, stored
from vm.domain.backup import Fingerprint, Manifest, index_entry, manifest_text, utc_stamp
from vm.ports import BackupStorePort, ProgressPort, VmCommandsPort


@dataclass(frozen=True, slots=True)
class LocalBackup:
    """What a local backup reaches: libvirt and the host's packages, the files, the clock, the
    terminal; root: this process runs as root."""

    commands: VmCommandsPort
    store: BackupStorePort
    clock: ClockPort
    out: ProgressPort
    root: bool


def backup_time(cfg: VmSettings, clock: ClockPort) -> tuple[float, str]:
    """(seconds, its UTC stamp) of now, or of cfg.now (CST_VM_NOW, tests)."""
    seconds = float(cfg.now or clock.now())
    return seconds, utc_stamp(seconds)


def check_backup_start(cfg: VmSettings, store: BackupStorePort, what: str, *, root: bool) -> None:
    """Before a backup copies anything: root (or the tests' override), and an index this cst vm can
    record it in (read_index refuses one of a newer cst vm)."""
    if not root and not cfg.nonroot_ok:
        raise Refused(f"{what} needs root: run it as `cst vm {what}` (it calls sudo by itself)")
    store.read_index()


def check_shut_off(commands: VmCommandsPort, vm: str) -> None:
    """Refused unless the VM is shut off AND has no managed-save image: after
    `virsh managedsave` (virt-manager's Save) domstate also says "shut off",
    but the guest's disks are only consistent with the saved RAM."""
    state = domain_state(commands, vm)
    if state != "shut off":
        raise Refused(
            f"{vm} is {state or 'in an unknown state'}: shut it down first (a copy of a running "
            f"VM is not consistent). From Windows, or: virsh -c {URI} shutdown {vm}"
        )
    if has_managed_save(commands, vm):
        raise Refused(
            f"{vm} was saved (managed save), not shut down: its disks are only consistent with "
            f"the saved memory. Start it and shut Windows down, then run the backup again "
            f"(or drop the saved memory: virsh -c {URI} managedsave-remove {vm})"
        )


def backup_local(cfg: VmSettings, vm: str, p: LocalBackup, *, full: bool) -> None:
    """Copy the shut-off vm into BACKUP_DIR/VM/STAMP (reflink, or real copies with full), check that
    nothing changed meanwhile, record it in the index and print how to restore it. Refused, with
    nothing left behind, when it cannot."""
    store, out = p.store, p.out
    check_backup_start(cfg, store, "backup", root=p.root)
    check_shut_off(p.commands, vm)
    rc, xml_text, err = p.commands.virsh(("dumpxml", "--inactive", "--security-info", vm), 60, detach=None)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    parts = domain_parts(xml_text)
    if parts.problems:
        raise Refused(f"{vm}: nothing copied:\n  " + "\n  ".join(parts.problems))
    if not parts.disks:
        raise Refused(f"{vm}: no file disk in its definition: nothing to back up")
    notes = list(parts.skipped)

    def real(path: str, what: str) -> str:
        # cp -a copies a symbolic link as a link (no data): the file it points
        # to is what gets copied, and restored, instead.
        r = store.realpath(path)
        if r != path:
            notes.append(f"{what} {path} is a symbolic link: copied (and restored) through it: {r}")
        return r

    sources: list[tuple[str, bool]] = []
    for path in parts.disks:
        if not store.is_file(path):
            raise Refused(f"{vm}: disk {path} not found")
        path = real(path, "disk")
        if store.has_backing_file(path):
            raise Refused(
                f"{vm}: {path} is a qcow2 overlay with a backing file: back up the chain by hand "
                "(qemu-img info --backing-chain)"
            )
        if (path, True) not in sources:
            sources.append((path, True))
    if parts.nvram:
        if store.is_file(parts.nvram):
            sources.append((real(parts.nvram, "UEFI variables"), False))
        else:
            notes.append(f"UEFI variables {parts.nvram} not there yet (the VM never started?): not copied")
    tpm_dir = ""
    if parts.tpm:
        tpm_dir = os.path.join(cfg.paths.swtpm_dir, parts.uuid)
        if parts.uuid and store.is_dir(tpm_dir):
            tpm_dir = real(tpm_dir, "TPM state")
            sources.append((tpm_dir, False))
        else:
            notes.append(
                f"TPM state {tpm_dir} not found: not copied (BitLocker may then ask for its "
                "recovery key after a restore)"
            )
    mode = "full" if full else "reflink"
    epoch, stamp = backup_time(cfg, p.clock)
    vm_dir = os.path.join(cfg.paths.backup_dir, vm)
    dest_dir = os.path.join(vm_dir, stamp)
    if store.exists(dest_dir):
        raise Refused(f"{dest_dir} already exists: wait a second and run it again")
    sizes = {src: store.sizes(src) for src, _ in sources}
    if full:
        need = sum(a for a, _ in sizes.values())
        margin = max(FULL_MARGIN[0], int(need * FULL_MARGIN[1]))
        probe, free = store.free_space(cfg.paths.backup_dir)
        if free < need + margin:
            raise Refused(
                f"--full needs {format_bytes_gib_mib(need, mib_decimals=1)} (allocated size) + {format_bytes_gib_mib(margin, mib_decimals=1)} margin, but "
                f"{probe} has {format_bytes_gib_mib(free, mib_decimals=1)} free: free space first, or use the default reflink copy"
            )
    store.make_backup_folder(cfg.paths.backup_dir, vm_dir, dest_dir)
    out.say(f"{vm}: {mode} backup to {dest_dir}")
    copied: list[tuple[str, bool]] = []
    try:
        before = {src: store.fingerprint(src) for src, _ in sources}
        for src, big in sources:
            a, s = sizes[src]
            out.say(
                f"  copying {src} ({format_bytes_gib_mib(a, mib_decimals=1)} allocated, {format_bytes_gib_mib(s, mib_decimals=1)} apparent)",
                flush=True,
            )
            store.copy(src, stored(dest_dir, src), mode, big=big)
            copied.append((src, big))
        # Nothing stops the VM from starting during a long copy: check again,
        # and that no source changed while it was read.
        try:
            check_shut_off(p.commands, vm)
        except Refused as e:
            raise Refused(f"the VM's state changed during the copy, the copy is not consistent: {e}") from e

        def now_fp(path: str) -> Fingerprint | None:
            try:
                return store.fingerprint(path)
            except OSError:
                return None  # removed during the copy: changed as well

        changed = [src for src, _ in sources if now_fp(src) != before[src]]
        if changed:
            raise Refused(
                "changed during the copy (the VM was started?), the copy is not consistent: "
                + ", ".join(changed)
                + ". Shut the VM off and run the backup again"
            )
        # The definition holds --security-info secrets (graphics passwords): 0600 from the start.
        store.write_private(os.path.join(dest_dir, "domain.xml"), xml_text)
        manifest: Manifest = {
            "vm": vm,
            "uuid": parts.uuid,
            "timestamp": stamp,
            "epoch": int(epoch),
            "mode": mode,
            "files": [
                {
                    "source": src,
                    "stored": posixpath.relpath(stored(dest_dir, src), dest_dir),
                    "kind": "disk" if big else ("tpm" if src == tpm_dir else "nvram"),
                    "allocated": sizes[src][0],
                    "apparent": sizes[src][1],
                }
                for src, big in copied
            ],
            "versions": p.commands.package_versions(),
            "deployment": p.commands.booted_deployment(),
            "notes": notes,
        }
        store.write_private(os.path.join(dest_dir, "manifest.json"), manifest_text(manifest))
    except BaseException:
        store.remove_tree(dest_dir)
        out.warn(f"  incomplete backup removed: {dest_dir}")
        raise
    # Entries whose folder was removed by hand leave the index here.
    entries = [
        e for e in store.read_index() if e.get("mode") == "nas" or store.is_dir(e["path"]) and e["path"] != dest_dir
    ]
    entries.append(index_entry(manifest, dest_dir))
    entries.sort(key=lambda e: (e["vm"], e.get("epoch") or 0))
    store.write_index(entries)
    for n in notes:
        out.say(f"  note: {n}")
    out.say(f"Done: {len(copied)} item(s), definition and manifest in {dest_dir}")
    if mode == "reflink":
        out.say(
            "Reflink copies share disk blocks with the VM: they protect against changes to the image "
            "(a repair tool, an upgrade), not against losing the disks. For that: --full, or a copy "
            "on another machine."
        )
    out.say("\nTo restore (never done automatically; read each line first):")
    for line in restore_lines(vm, dest_dir, copied, tpm_dir):
        out.say(f"  {line}")
    out.say(
        f"\nRemove an old backup by hand: sudo rm -r {shlex.quote(vm_dir)}/TIMESTAMP "
        f"(the next backup drops it from {cfg.cst} vm list)"
    )

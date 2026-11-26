# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup [VM] [--full]`: the local copy of a shut-off VM (reflink or full), and the checks
every backup makes before it copies anything (also the NAS backup's, vm.application.nas)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os.path
import shlex

from cst_core.domain.errors import Refused
from cst_core.domain.text import first_line
from cst_core.domain.values import format_bytes_gib_mib
from cst_core.ports import ClockPort
from vm.application import domain_state, has_managed_save
from vm.domain import (
    FULL_MARGIN,
    OVERLAY_MARK,
    SHUT_OFF,
    URI,
    DomainParts,
    VmSettings,
    domain_parts,
    restore_lines,
    stored,
    stored_name,
)
from vm.domain.backup import (
    IndexEntry,
    DOMAIN_XML,
    MANIFEST_JSON,
    BackupMode,
    Fingerprint,
    IndexRefused,
    Manifest,
    copied_kind,
    index_entry,
    manifest_text,
    not_indexed_text,
    utc_stamp,
)
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
    if state != SHUT_OFF:
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


def read_backup_definition(commands: VmCommandsPort, vm: str, nothing: str) -> tuple[str, DomainParts]:
    """(the inactive definition with its secrets, what it is made of) of a backup; Refused when virsh
    cannot give it, when a disk cannot be backed up (nothing: "nothing copied", "nothing backed up"),
    or when it has no file disk."""
    rc, xml_text, err = commands.virsh(("dumpxml", "--inactive", "--security-info", vm), 60, detach=None)
    if rc != 0:
        raise Refused(f"virsh dumpxml {vm}: {first_line(err) or f'exit {rc}'}")
    parts = domain_parts(xml_text)
    if parts.problems:
        raise Refused(f"{vm}: {nothing}:\n  " + "\n  ".join(parts.problems))
    if not parts.disks:
        raise Refused(f"{vm}: no file disk in its definition: nothing to back up")
    return xml_text, parts


def fingerprint_or_none(store: BackupStorePort, path: str) -> Fingerprint | None:
    """The fingerprint of path, None when it is gone (removed meanwhile: changed as well)."""
    try:
        return store.fingerprint(path)
    except OSError:
        return None


def backup_local(cfg: VmSettings, vm: str, p: LocalBackup, *, full: bool) -> None:
    """Copy the shut-off vm into BACKUP_DIR/VM/STAMP (reflink, or real copies with full), check that
    nothing changed meanwhile, record it in the index and print how to restore it. Refused, with
    nothing left behind, when it cannot."""
    store, out = p.store, p.out
    check_backup_start(cfg, store, "backup", root=p.root)
    check_shut_off(p.commands, vm)
    xml_text, parts = read_backup_definition(p.commands, vm, "nothing copied")
    notes = list(parts.skipped)
    found = SourceWalk(cfg, store, vm, notes, nas=False).sources(parts)
    sources = [(d, True) for d in found.disks] + [(f, False) for f in (found.nvram, found.tpm) if f is not None]
    tpm_dir = found.tpm_dir
    mode = BackupMode.FULL if full else BackupMode.REFLINK
    epoch, stamp = backup_time(cfg, p.clock)
    vm_dir = os.path.join(cfg.paths.backup_dir, vm)
    dest_dir = os.path.join(vm_dir, stamp)
    if store.exists(dest_dir):
        raise Refused(f"{dest_dir} already exists: wait a second and run it again")
    sizes = {src: store.sizes(src) for src, _ in sources}
    if full:
        _check_full_space(cfg, store, sizes)
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
        _check_copy_unchanged(p, vm, sources, before)
        # The definition holds --security-info secrets (graphics passwords): 0600 from the start.
        store.write_private(os.path.join(dest_dir, DOMAIN_XML), xml_text)
        manifest: Manifest = {
            "vm": vm,
            "uuid": parts.uuid,
            "timestamp": stamp,
            "epoch": int(epoch),
            "mode": mode,
            "files": [
                {
                    "source": src,
                    "stored": stored_name(src),
                    "kind": copied_kind(src, tpm_dir, big=big),
                    "allocated": sizes[src][0],
                    "apparent": sizes[src][1],
                }
                for src, big in copied
            ],
            "versions": p.commands.package_versions(),
            "deployment": p.commands.booted_deployment(),
            "notes": notes,
        }
        store.write_private(os.path.join(dest_dir, MANIFEST_JSON), manifest_text(manifest))
    except BaseException:
        store.remove_tree(dest_dir)
        out.warn(f"  incomplete backup removed: {dest_dir}")
        raise
    refused = index_local_backup(store, manifest, dest_dir)
    _say_local_done(out, notes, dest_dir, len(copied), reflink=mode == BackupMode.REFLINK)
    out.say("\nTo restore (never done automatically; read each line first):")
    for line in restore_lines(vm, dest_dir, copied, tpm_dir):
        out.say(f"  {line}")
    if refused is not None:
        raise refused
    out.say(
        f"\nRemove an old backup by hand: sudo rm -r {shlex.quote(vm_dir)}/TIMESTAMP "
        f"(the next backup drops it from {cfg.cst} vm list)"
    )


@dataclass(frozen=True, slots=True)
class BackupSources:
    """What a backup of a VM reads (SourceWalk.sources): its disks, in the order of its definition (each file
    once), the files its UEFI variables and TPM state are read from (None: not read), and the
    TPM folder a local copy records (its real path when read; the folder named for its uuid when not
    there; "" without a TPM)."""

    disks: list[str]
    nvram: str | None
    tpm: str | None
    tpm_dir: str


@dataclass(frozen=True, slots=True)
class SourceWalk:
    """The walk over a VM's definition that finds what its backup reads (sources): for a local copy
    (nas False) or a NAS backup of vm, through store, its notes added to notes."""

    cfg: VmSettings
    store: BackupStorePort
    vm: str
    notes: list[str]
    nas: bool

    def through(self, path: str, what: str) -> str:
        """The file path is or points to: the backup reads (cp -a would copy a link as a link, no data)
        and restores the file it points to instead, which a note says for each file."""
        r = self.store.realpath(path)
        if r != path:
            verb = "read" if self.nas else "copied"
            self.notes.append(f"{what} {path} is a symbolic link: {verb} (and restored) through it: {r}")
        return r

    def disk(self, disk: str) -> str:
        """The file a backup reads for a disk of the definition; Refused when it is not there, is an
        overlay a NAS backup left behind (NAS only), or has a backing file."""
        if not self.store.is_file(disk):
            raise Refused(f"{self.vm}: disk {disk} not found")
        if self.nas and OVERLAY_MARK in disk:
            raise Refused(
                f"{self.vm}: disk {disk} is an overlay a NAS backup left behind (its blockcommit did not "
                f"finish). Put the disk back first; `{self.cfg.cst} doctor` shows the commands"
            )
        path = self.through(disk, "disk")
        if self.store.has_backing_file(path):
            raise Refused(
                f"{self.vm}: {path} is a qcow2 overlay with a backing file: back up the chain by hand "
                "(qemu-img info --backing-chain)"
            )
        return path

    def sources(self, parts: DomainParts) -> BackupSources:
        """What the backup reads of the VM: its disks (Refused for one it cannot back up), its UEFI
        variables and its TPM folder; notes say which is not there, and which is read through a
        symbolic link."""
        disks: list[str] = []
        for disk in parts.disks:
            path = self.disk(disk)
            if path not in disks:  # a file the definition names twice (or two links to it) is read once
                disks.append(path)
        not_read = "backed up" if self.nas else "copied"
        nvram = None
        if parts.nvram and self.store.is_file(parts.nvram):
            nvram = self.through(parts.nvram, "UEFI variables")
        elif parts.nvram:
            self.notes.append(f"UEFI variables {parts.nvram} not there yet (the VM never started?): not {not_read}")
        tpm, tpm_dir = None, ""
        if parts.tpm:
            tpm_dir = os.path.join(self.cfg.paths.swtpm_dir, parts.uuid)
            if parts.uuid and self.store.is_dir(tpm_dir):
                tpm = tpm_dir = self.through(tpm_dir, "TPM state")
            else:
                self.notes.append(
                    f"TPM state {tpm_dir} not found: not {not_read} (BitLocker may then ask for its recovery "
                    "key after a restore)"
                )
        return BackupSources(disks, nvram, tpm, tpm_dir)


def _check_full_space(cfg: VmSettings, store: BackupStorePort, sizes: Mapping[str, tuple[int, int]]) -> None:
    """Refused when the backup folder's file system has not the allocated size of the sources free,
    plus FULL_MARGIN (--full)."""
    need = sum(a for a, _ in sizes.values())
    margin = max(FULL_MARGIN[0], int(need * FULL_MARGIN[1]))
    probe, free = store.free_space(cfg.paths.backup_dir)
    if free < need + margin:
        raise Refused(
            f"--full needs {format_bytes_gib_mib(need, mib_decimals=1)} (allocated size) + {format_bytes_gib_mib(margin, mib_decimals=1)} margin, but "
            f"{probe} has {format_bytes_gib_mib(free, mib_decimals=1)} free: free space first, or use the default reflink copy"
        )


def _check_copy_unchanged(
    p: LocalBackup, vm: str, sources: Sequence[tuple[str, bool]], before: Mapping[str, Fingerprint]
) -> None:
    """Nothing stops the VM from starting during a long copy: Refused unless it is still shut off and
    no source changed while it was read (before: their fingerprints)."""
    try:
        check_shut_off(p.commands, vm)
    except Refused as e:
        raise Refused(f"the VM's state changed during the copy, the copy is not consistent: {e}") from e
    changed = [src for src, _ in sources if fingerprint_or_none(p.store, src) != before[src]]
    if changed:
        raise Refused(
            "changed during the copy (the VM was started?), the copy is not consistent: "
            + ", ".join(changed)
            + ". Shut the VM off and run the backup again"
        )


def index_local_backup(store: BackupStorePort, manifest: Manifest, dest_dir: str) -> Refused | None:
    """The index with the new backup (entries whose folder was removed by hand leave it); the refusal
    to raise once the notes and restore lines are printed when the index is not this cst vm's."""

    # The index was read before the copy too, but a newer cst vm (or a damage) can have changed it
    # since: the backup stays, and its notes and restore lines are printed before the refusal.
    def change(stored: list[IndexEntry]) -> list[IndexEntry]:
        entries = [
            e for e in stored if e.get("mode") == BackupMode.NAS or (store.is_dir(e["path"]) and e["path"] != dest_dir)
        ]
        entries.append(index_entry(manifest, dest_dir))
        entries.sort(key=lambda e: (e["vm"], e.get("epoch") or 0))
        return entries

    try:
        store.update_index(change)  # under the index's lock: another VM's backup may end at the same time
    except IndexRefused as e:
        kept = (
            f"the folder stays until removed by hand, and its {MANIFEST_JSON} says what it holds "
            "(restore by hand: vm/README.md, section 5)"
        )
        return Refused(not_indexed_text(f"the backup {dest_dir}", kept, e))
    return None


def _say_local_done(out: ProgressPort, notes: Sequence[str], dest_dir: str, items: int, *, reflink: bool) -> None:
    """The notes of a local copy, its Done line, and what a reflink copy protects against."""
    for n in notes:
        out.say(f"  note: {n}")
    out.say(f"Done: {items} item(s), definition and manifest in {dest_dir}")
    if reflink:
        out.say(
            "Reflink copies share disk blocks with the VM: they protect against changes to the image "
            "(a repair tool, an upgrade), not against losing the disks. For that: --full, or a copy "
            "on another machine."
        )

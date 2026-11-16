"""Domain model and formatting for the top-level `cst` module dispatcher."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """Descriptor for one top-level CST module entrypoint."""

    name: str
    script: str
    about: str
    status_needs_root: bool


MODULES: Final[tuple[ModuleSpec, ...]] = (
    ModuleSpec(
        name="btrfs",
        script="btrfs-setup.sh",
        about="storage: disk health, stalls, snapshots, ~/Bulk, Docker volumes and cleanup",
        status_needs_root=True,
    ),
    ModuleSpec(
        name="cpu",
        script="cpu-setup.sh",
        about="CPU: game profile, VM/toolbox/Docker priority and E-cores, measurements",
        status_needs_root=True,
    ),
    ModuleSpec(
        name="swap",
        script="swap-setup.sh",
        about="swap and memory: zswap, encrypted NVMe swap, zram off, swap sysctls, VM memory floor",
        status_needs_root=False,
    ),
    ModuleSpec(
        name="bluetooth",
        script="bluetooth-setup.sh",
        about="Bluetooth audio: LE Audio (BAP/LC3) in BlueZ, adaptive LDAC for the headset",
        status_needs_root=False,
    ),
    ModuleSpec(
        name="toolchains",
        script="toolchains-setup.sh",
        about="toolchain caches and settings: pruning of cargo build folders and uv's cache, cargo/uv settings",
        status_needs_root=False,
    ),
    ModuleSpec(
        name="vm",
        script="vm-setup.sh",
        about=(
            "the libvirt VM: backups (disks, UEFI variables, TPM) to the NAS weekly and before upgrades, "
            "virtio hardware (tune), NOCOW image folders, status"
        ),
        status_needs_root=False,
    ),
)

MODULE_BY_NAME: Final[Mapping[str, ModuleSpec]] = {spec.name: spec for spec in MODULES}

USAGE_HEADER: Final[str] = """
cst

CST (Cominotti Setup Tools): one command for the setup tools of this
workstation. Each module is a script next to this one, and still works on
its own; through cst its help and hints say `cst MODULE ...`.

  cst btrfs [ARGS...]    storage: disk health, stalls, snapshots, ~/Bulk,
                         Docker volumes and cleanup (btrfs-setup.sh)
  cst cpu [ARGS...]      CPU: game profile, VM/toolbox/Docker priority and
                         E-cores, measurements (cpu-setup.sh)
  cst swap [ARGS...]     swap and memory: zswap, encrypted NVMe swap, zram
                         off, swap sysctls, VM memory floor (swap-setup.sh)
  cst bluetooth [ARGS...]
                         Bluetooth audio: LE Audio (BAP/LC3) in BlueZ,
                         adaptive LDAC for the headset (bluetooth-setup.sh)
  cst toolchains [ARGS...]
                         toolchain caches and settings: hourly pruning of
                         cargo's build folders, uv cache prune, cargo
                         build-dir and uv link-mode (toolchains-setup.sh;
                         as your user)
  cst vm [ARGS...]       the libvirt VM: backups of its disks, UEFI
                         variables and TPM state (weekly to the NAS with
                         the VM running, or a local copy before risky upgrades),
                         the recommended virtual hardware (tune), NOCOW
                         for the VM image folders (nocow), and its status
                         (vm-setup.sh; backup and tune apply as root)
  cst doctor [--brief]   read-only check-up across modules: ok/info/WARN/
                         ERROR lines, most WARNs with the command that fixes
                         it (layered packages pinned to a version, failed
                         units, unseen alerts, btrfs chunk allocation).
                         Exit 1 if any WARN/ERROR
                         (doctor/cst-doctor; as your user, also in a toolbox)
  cst status             the read-only status of every module; asks sudo
                         once when one of them needs root (inside a toolbox
                         those are skipped, with a note); ends with the
                         short form of cst doctor
  cst help               this overview; `cst MODULE help` lists the
                         commands of one module

Root commands of a module call sudo by themselves: type `cst btrfs install`,
not `sudo cst ...` (sudo would not find cst in its PATH). cst may be called
through a symlink, e.g. ~/.local/bin/cst -> the cst of this folder.

Modules here:"""


def format_cst_usage(present_modules: Mapping[str, bool]) -> str:
    """Format `cst help` output with availability annotations per module."""
    lines = [USAGE_HEADER]
    for spec in MODULES:
        state = "" if present_modules.get(spec.name, False) else " (not in this copy)"
        lines.append(f"  {spec.name:<10} {spec.about}{state}")
    return "\n".join(lines)


__all__ = [
    "MODULES",
    "MODULE_BY_NAME",
    "ModuleSpec",
    "USAGE_HEADER",
    "format_cst_usage",
]

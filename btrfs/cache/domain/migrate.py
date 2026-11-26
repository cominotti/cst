# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for migrating existing `~/.cache` files into `/var/mnt/cache/<user>` during `attach`."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from btrfs.cache.domain import BindingProbe, BusyProcess, CacheMountKind, MigrationProbe

MIGRATE_BACKUP_SUFFIX: Final = ".pre-cst-cache"
BTRFS_SUBVOL_ROOT_INO: Final = 256
BUSY_PROCS_SHOWN: Final = 10


def backup_path_for(target: str) -> str:
    """Return sibling path where original `~/.cache` is moved during `--migrate`."""
    return f"{target.rstrip('/')}{MIGRATE_BACKUP_SUFFIX}"


def format_busy_blocker(procs: Sequence[BusyProcess], target: str) -> str:
    """Format clear error listing busy processes using `target`."""
    shown = ", ".join(f"{p.comm} (pid {p.pid})" for p in procs[:BUSY_PROCS_SHOWN])
    more = f", and {len(procs) - BUSY_PROCS_SHOWN} more" if len(procs) > BUSY_PROCS_SHOWN else ""
    return (
        f"{target} is in use by {len(procs)} running process(es): {shown}{more}; "
        "close them first before migrating (or pass --allow-busy)"
    )


def cleanup_hint(backup_path: str, *, is_subvol: bool) -> str:
    """Exact command the user runs to reclaim space after verifying migrated `~/.cache`."""
    if is_subvol:
        return f"sudo btrfs subvolume delete {backup_path}"
    return f"rm -rf {backup_path}"


def needs_migration(probe: BindingProbe) -> bool:
    """True when `probe` is the cache binding and currently holds non-empty unattached data."""
    return (
        probe.binding.kind == CacheMountKind.CACHE
        and not probe.mounted_from_source
        and probe.target_exists
        and probe.target_nonempty
        and not probe.target_is_symlink
        and not probe.target_not_dir
        and not probe.mounted_other
        and not probe.source_unchecked
        and not probe.fstab_unmanaged
    )


def validate_migration(probe: BindingProbe, mig: MigrationProbe, *, allow_busy: bool) -> list[str]:
    """Return blocking reasons preventing safe `--migrate`, or `[]` when safe."""
    if not needs_migration(probe):
        return []
    errors: list[str] = []
    if mig.backup_exists:
        errors.append(f"backup path {mig.backup_path} already exists; inspect or remove it before running --migrate")
    if mig.source_nonempty:
        errors.append(
            f"destination {probe.binding.source} is already non-empty; move/empty it before running --migrate"
        )
    if not allow_busy:
        if mig.busy_error is not None:
            errors.append(f"cannot inspect open handles in {probe.binding.target}: {mig.busy_error}")
        elif mig.busy_procs:
            errors.append(format_busy_blocker(mig.busy_procs, probe.binding.target))
    return errors

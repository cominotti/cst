# SPDX-License-Identifier: MPL-2.0
"""CLI wrapper for `btrfs-cache` / `cst btrfs scratch`."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.cache import adapters as cache_mount_adapters, domain as cache_mount_domain
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli, undo_python_start

_CACHE_MOUNT_SPEC = ManagedCliSpec(
    default_prog="cst btrfs scratch",
    parse=cache_mount_domain.parse_cache_mount_args,
    format_usage=cache_mount_domain.format_cache_mount_usage,
    is_help=cache_mount_domain.is_cache_mount_help,
    parse_error=cache_mount_domain.cache_mount_parse_error,
    write_action=cache_mount_domain.cache_mount_write_action,
    elevation_reason="configures per-user cache/scratch directories, /etc/fstab bind mounts, and live mounts",
    execute=cache_mount_adapters.dispatch_cache_mount_cli_action,
    is_err_line=cache_mount_domain.is_cache_mount_err,
)


def run_cache_mount_cli(argv: Sequence[str] | None = None, *, deps: ManagedCliEnv | None = None) -> int:
    """Parse CLI arguments, enforce host-root on writes, and invoke `btrfs-cache`."""
    resolved = argv if argv is not None else sys.argv[1:]
    return run_managed_cli(resolved, _CACHE_MOUNT_SPEC, deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point invoked by `btrfs/btrfs-cache`."""
    undo_python_start()
    cfg = ManagedCliEnv(entry_file=entry_file)
    return run_managed_cli(sys.argv[1:] if argv is None else argv, _CACHE_MOUNT_SPEC, cfg)

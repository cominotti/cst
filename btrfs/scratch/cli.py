# SPDX-License-Identifier: MPL-2.0
"""CLI wrapper for `btrfs-scratch` / `cst btrfs scratch`."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.scratch import adapters as scratch_adapters, domain as scratch_domain
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli, undo_python_start

_SCRATCH_SPEC = ManagedCliSpec(
    default_prog="cst btrfs scratch",
    parse=scratch_domain.parse_scratch_args,
    format_usage=scratch_domain.format_scratch_usage,
    is_help=scratch_domain.is_scratch_help,
    parse_error=scratch_domain.scratch_parse_error,
    write_action=scratch_domain.scratch_write_action,
    elevation_reason="configures per-user cache/scratch directories, /etc/fstab bind mounts, and live mounts",
    execute=scratch_adapters.dispatch_scratch_cli_action,
    is_err_line=scratch_domain.is_scratch_err,
)


def run_scratch_cli(argv: Sequence[str] | None = None, *, deps: ManagedCliEnv | None = None) -> int:
    """Parse CLI arguments, enforce host-root on writes, and invoke `btrfs-scratch`."""
    resolved = argv if argv is not None else sys.argv[1:]
    return run_managed_cli(resolved, _SCRATCH_SPEC, deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point invoked by `btrfs/btrfs-scratch`."""
    undo_python_start()
    cfg = ManagedCliEnv(entry_file=entry_file)
    return run_managed_cli(sys.argv[1:] if argv is None else argv, _SCRATCH_SPEC, cfg)

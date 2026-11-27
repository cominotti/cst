# SPDX-License-Identifier: MPL-2.0
"""CLI wrapper for `btrfs-flushoncommit` / `cst btrfs flushoncommit`."""

from __future__ import annotations

from collections.abc import Sequence

from btrfs.flushoncommit.adapters import FLUSH_SPEC
from cst_core.adapters.runtime import ManagedCliEnv, run_managed_cli, run_managed_entry

FlushCliDeps = ManagedCliEnv


def run_flush_cli(argv: Sequence[str] | None = None, *, deps: FlushCliDeps | None = None) -> int:
    """Parse arguments, enforce host/root on writes, and invoke `btrfs-flushoncommit` adapters."""
    args: Sequence[str] = () if argv is None else argv
    return run_managed_cli(args, FLUSH_SPEC, deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point invoked by `btrfs/btrfs-flushoncommit`."""
    return run_managed_entry(FLUSH_SPEC, argv, entry_file)

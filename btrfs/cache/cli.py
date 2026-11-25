# SPDX-License-Identifier: MPL-2.0
"""CLI wrapper for `btrfs-cache` / `cst btrfs cache`: argv to wiring.py, which runs the command."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.cache import wiring


def run_cache_mount_cli(argv: Sequence[str] | None = None, *, deps: wiring.CacheMountCliDeps | None = None) -> int:
    """Parse CLI arguments, enforce host-root on writes, and invoke `btrfs-cache`."""
    return wiring.run_cache_mount(sys.argv[1:] if argv is None else argv, deps=deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point invoked by `btrfs/btrfs-cache`."""
    wiring.start_cache_mount_process()
    return run_cache_mount_cli(argv, deps=wiring.CacheMountCliDeps(entry_file=entry_file))

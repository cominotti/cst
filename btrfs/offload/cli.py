# SPDX-License-Identifier: MPL-2.0
"""CLI wrapper for `btrfs-cache-offload` / `cst btrfs cache offload`: argv to wiring.py."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.offload import wiring


def run_offload_cli(argv: Sequence[str] | None = None, *, deps: wiring.OffloadCliDeps | None = None) -> int:
    """Parse CLI arguments, enforce host user execution on writes, and invoke offload service."""
    return wiring.run_offload(sys.argv[1:] if argv is None else argv, deps=deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point invoked by `btrfs/btrfs-cache-offload`."""
    wiring.start_offload_process()
    return run_offload_cli(argv, deps=wiring.OffloadCliDeps(entry_file=entry_file))

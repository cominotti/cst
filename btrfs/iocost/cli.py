# SPDX-License-Identifier: MPL-2.0
"""Command-line frontend for `btrfs-iocost` / `cst btrfs iocost`: argv to wiring.py, which runs the command."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.iocost import wiring

IocostCliDeps = wiring.IocostCliDeps


def run_iocost_cli(argv: Sequence[str] | None = None, *, deps: IocostCliDeps | None = None) -> int:
    """Parse arguments, enforce host/root on writes, and run the chosen action."""
    return wiring.run_iocost(sys.argv[1:] if argv is None else argv, deps=deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point invoked by `btrfs/btrfs-iocost`."""
    wiring.start_iocost_process()
    return run_iocost_cli(argv, deps=IocostCliDeps(entry_file=entry_file))

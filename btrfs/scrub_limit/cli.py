# SPDX-License-Identifier: MPL-2.0
"""Command-line frontend for `cst btrfs scrub-limit`: argv to wiring.py, which runs the command."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.scrub_limit import wiring

ScrubCliDeps = wiring.ScrubCliDeps


def run_scrub_limit_cli(
    argv: Sequence[str] | None = None,
    *,
    deps: ScrubCliDeps | None = None,
    build: wiring.ScrubBuild | None = None,
) -> int:
    """Parse arguments, enforce host/root preconditions for writes, and run the chosen action (build: a test's
    use case for read_only; None: the real one)."""
    return wiring.run_scrub_limit(sys.argv[1:] if argv is None else argv, deps=deps, build=build)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point delegating to `run_scrub_limit_cli`."""
    wiring.start_scrub_process()
    return run_scrub_limit_cli(argv, deps=ScrubCliDeps(entry_file=entry_file))

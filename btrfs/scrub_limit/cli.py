# SPDX-License-Identifier: MPL-2.0
"""Command-line frontend for `cst btrfs scrub-limit`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import sys

from btrfs.scrub_limit import adapters as scrub_adapters, domain as scrub_domain
from btrfs.scrub_limit.domain import ScrubLimitAction
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli

ScrubCliDeps = ManagedCliEnv

_SCRUB_SPEC = ManagedCliSpec(
    default_prog="cst btrfs scrub-limit",
    parse=scrub_domain.parse_scrub_limit_args,
    format_usage=scrub_domain.format_scrub_usage,
    is_help=lambda p: p.action == ScrubLimitAction.HELP,
    parse_error=lambda p: None if p.error is None else (p.code, p.error),
    write_action=lambda p: p.action.value if p.action in (ScrubLimitAction.SET, ScrubLimitAction.OFF) else None,
    elevation_reason="writes /etc/btrfs-setup/snapshots.conf and systemd drop-in",
    execute=scrub_adapters.ScrubCliExecutor(),
)


def run_scrub_limit_cli(
    argv: Sequence[str] | None = None,
    *,
    deps: ScrubCliDeps | None = None,
    build: scrub_adapters.ScrubBuild | None = None,
) -> int:
    """Parse arguments, enforce host/root preconditions for writes, and run the chosen action (build: a test's
    use case for read_only; None: the real one)."""
    spec = _SCRUB_SPEC if build is None else replace(_SCRUB_SPEC, execute=scrub_adapters.ScrubCliExecutor(build))
    return run_managed_cli(sys.argv[1:] if argv is None else argv, spec, deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point delegating to `run_scrub_limit_cli`."""
    scrub_adapters.start_scrub_process()
    return run_scrub_limit_cli(argv, deps=ScrubCliDeps(entry_file=entry_file))

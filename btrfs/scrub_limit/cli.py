# SPDX-License-Identifier: MPL-2.0
"""Command-line frontend for `cst btrfs scrub-limit`."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.scrub_limit import adapters as scrub_adapters, domain as scrub_domain
from btrfs.scrub_limit.domain import ScrubLimitAction, ScrubParsedArgs
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli

ScrubCliDeps = ManagedCliEnv


def _run_scrub_action(parsed: ScrubParsedArgs, env: ManagedCliEnv, *, read_only: bool) -> tuple[int, list[str]]:
    paths = scrub_adapters.scrub_paths_for(
        env.entry_file, ctx=env.ctx, read_only=read_only, fallback_root=env.fallback_root
    )
    if parsed.action == ScrubLimitAction.STATUS:
        return scrub_adapters.scrub_limit_status(paths, runner=env.runner)
    if parsed.action == ScrubLimitAction.PLAN:
        return scrub_adapters.scrub_limit_plan(paths, parsed.limit, runner=env.runner)
    return scrub_adapters.scrub_limit_apply(paths, parsed.limit, runner=env.runner)


_SCRUB_SPEC = ManagedCliSpec(
    default_prog="cst btrfs scrub-limit",
    parse=scrub_domain.parse_scrub_limit_args,
    format_usage=scrub_domain.format_scrub_usage,
    is_help=lambda p: p.action == ScrubLimitAction.HELP,
    parse_error=lambda p: None if p.error is None else (p.code, p.error),
    write_action=lambda p: p.action.value if p.action in (ScrubLimitAction.SET, ScrubLimitAction.OFF) else None,
    elevation_reason="writes /etc/btrfs-setup/snapshots.conf and systemd drop-in",
    execute=_run_scrub_action,
)


def run_scrub_limit_cli(argv: Sequence[str] | None = None, *, deps: ScrubCliDeps | None = None) -> int:
    """Parse arguments, enforce host/root preconditions for writes, and run the chosen action."""
    return run_managed_cli(sys.argv[1:] if argv is None else argv, _SCRUB_SPEC, deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point delegating to `run_scrub_limit_cli`."""
    scrub_adapters.start_scrub_process()
    return run_scrub_limit_cli(argv, deps=ScrubCliDeps(entry_file=entry_file))

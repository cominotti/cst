# SPDX-License-Identifier: MPL-2.0
"""Command-line frontend for `cst btrfs scrub-limit`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
import sys
from typing import TextIO

from btrfs.scrub_limit import adapters as scrub_adapters, domain as scrub_domain
from btrfs.scrub_limit.domain import ScrubLimitAction
from cst_core.ports import CommandRunnerPort, ExecutionContextPort


@dataclass(frozen=True, slots=True)
class ScrubCliDeps:
    """Injected runtime boundaries and metadata for `run_scrub_limit_cli`."""

    entry_file: str = ""
    env: Mapping[str, str] | None = None
    out: TextIO | None = None
    err: TextIO | None = None
    ctx: ExecutionContextPort | None = None
    runner: CommandRunnerPort | None = None
    fallback_root: str = ""


def run_scrub_limit_cli(argv: Sequence[str] | None = None, *, deps: ScrubCliDeps | None = None) -> int:
    """Parse arguments, enforce host/root preconditions for writes, and run the chosen action."""
    cfg = ScrubCliDeps() if deps is None else deps
    args = list(sys.argv[1:] if argv is None else argv)
    env_map = os.environ if cfg.env is None else cfg.env
    prog = env_map.get("CST_PROG") or "cst btrfs scrub-limit"
    stdout = sys.stdout if cfg.out is None else cfg.out
    stderr = sys.stderr if cfg.err is None else cfg.err

    parsed = scrub_domain.parse_scrub_limit_args(args)
    if parsed.error is not None:
        print(f"{prog}: {parsed.error}", file=stderr)
        stderr.write("\n" + scrub_domain.format_scrub_usage(prog))
        return parsed.code
    if parsed.action == ScrubLimitAction.HELP:
        stdout.write(scrub_domain.format_scrub_usage(prog))
        return 0

    invocation = scrub_adapters.ScrubInvocation(prog=prog, args=tuple(args), entry_file=cfg.entry_file)
    refusal = scrub_adapters.enforce_scrub_host_root(parsed.action, invocation, ctx=cfg.ctx)
    if refusal is not None:
        print(refusal, file=stderr)
        return 1

    read_only = parsed.action in (ScrubLimitAction.STATUS, ScrubLimitAction.PLAN)
    paths = scrub_adapters.scrub_paths_for(
        cfg.entry_file, ctx=cfg.ctx, read_only=read_only, fallback_root=cfg.fallback_root
    )
    match parsed.action:
        case ScrubLimitAction.STATUS:
            rc, lines = scrub_adapters.scrub_limit_status(paths)
        case ScrubLimitAction.PLAN:
            rc, lines = scrub_adapters.scrub_limit_plan(paths, parsed.limit)
        case _:
            rc, lines = scrub_adapters.scrub_limit_apply(paths, parsed.limit, runner=cfg.runner)
    for line in lines:
        print(line, file=stderr if rc != 0 and line.startswith("ERROR:") else stdout)
    return rc


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point delegating to `run_scrub_limit_cli`."""
    scrub_adapters.start_scrub_process()
    return run_scrub_limit_cli(argv, deps=ScrubCliDeps(entry_file=entry_file))

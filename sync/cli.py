# SPDX-License-Identifier: MPL-2.0
"""CLI entry module for `cst sync`."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from cst_core.domain.errors import Refused
from sync import wiring
from sync.application import plan_host_sync, run_apply
from sync.domain import SyncAction, format_sync_help, parse_sync_args
from sync.ports import SyncExecPort, SyncInspectPort
from sync.presenter import format_apply_report, format_sync_plan


def run_sync_cli(
    argv: Sequence[str],
    *,
    entry_file: str,
    inspect: SyncInspectPort | None = None,
    executor: SyncExecPort | None = None,
) -> int:
    """Execute `cst sync` CLI and return exit status."""
    args = parse_sync_args(argv)
    prog = wiring.sync_prog()
    if args.error is not None:
        print(f"{prog}: {args.error}", file=sys.stderr)
        print(format_sync_help(prog), file=sys.stderr)
        return args.code
    if inspect is None or executor is None:
        wiring.prepare_process(args)
        real_inspect, real_exec = wiring.build_adapters(entry_file, prog)
        inspect = real_inspect if inspect is None else inspect
        executor = real_exec if executor is None else executor
    if args.action == SyncAction.HELP:
        print(format_sync_help(prog), end="")
        return 0
    try:
        if args.action == SyncAction.PLAN:
            print(format_sync_plan(plan_host_sync(inspect, args), prog))
            return 0
        report = run_apply(inspect, executor, args, prog=prog)
        print(format_apply_report(report, prog))
        return 0 if report.ok else 1
    except Refused as exc:
        print(f"{prog}: ERROR: {exc}", file=sys.stderr)
        return 1


def main(argv: Sequence[str], *, entry_file: str) -> int:
    """CLI entrypoint called by `sync/cst-sync`."""
    return run_sync_cli(argv, entry_file=entry_file)

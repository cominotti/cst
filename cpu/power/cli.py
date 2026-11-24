# SPDX-License-Identifier: MPL-2.0
"""Command-line frontend for `cpu-power` / `cst cpu power`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
import sys
from typing import TextIO

from cst_core.adapters.runtime import RootElevation, enforce_host_root
from cst_core.ports import ExecutionContextPort
from cpu.power import adapters as power_adapters, domain as power_domain
from cpu.power.domain import PowerAction


@dataclass(frozen=True, slots=True)
class PowerCliDeps:
    """Injected runtime boundaries and metadata for `run_power_cli`."""

    entry_file: str = ""
    env: Mapping[str, str] | None = None
    out: TextIO | None = None
    err: TextIO | None = None
    ctx: ExecutionContextPort | None = None
    paths: power_adapters.PowerPaths | None = None


def run_power_cli(argv: Sequence[str] | None = None, *, deps: PowerCliDeps | None = None) -> int:
    """Parse arguments, enforce host/root preconditions on writes, and execute the action."""
    cfg = PowerCliDeps() if deps is None else deps
    args = list(sys.argv[1:] if argv is None else argv)
    raw_prog = (os.environ if cfg.env is None else cfg.env).get("CST_PROG") or ""
    prog = f"{raw_prog} power" if raw_prog == "cst cpu" else (raw_prog or "cst cpu power")
    stdout = sys.stdout if cfg.out is None else cfg.out
    stderr = sys.stderr if cfg.err is None else cfg.err

    parsed = power_domain.parse_power_args(args)
    if parsed.error is not None:
        print(f"{prog}: {parsed.error}", file=stderr)
        stderr.write("\n" + power_domain.format_power_usage(prog))
        return parsed.code

    match parsed.action:
        case PowerAction.HELP:
            stdout.write(power_domain.format_power_usage(prog))
            return 0
        case PowerAction.APPLY | PowerAction.REVERT:
            req = RootElevation(
                prog, parsed.action.value, tuple(args), cfg.entry_file, "writes /sys and /run/cpu-setup"
            )
            if (refusal := enforce_host_root(req, ctx=cfg.ctx)) is not None:
                print(refusal, file=stderr)
                return 1
            ro = False
        case _:
            ro = True
    paths = power_adapters.power_paths_for(ctx=cfg.ctx, read_only=ro) if cfg.paths is None else cfg.paths
    rc, lines = power_adapters.execute_power_action(parsed, paths)
    for line in lines:
        print(line, file=stderr if line.startswith("WARN:") else stdout)
    return rc


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point delegating to `run_power_cli`."""
    power_adapters.start_power_process()
    return run_power_cli(argv, deps=PowerCliDeps(entry_file=entry_file))

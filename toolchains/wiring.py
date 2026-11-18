# SPDX-License-Identifier: MPL-2.0
"""The composition root of cst-toolchains: what it reads of its environment (the
program's name in hints, the journal's stream, the fixed "now" of tests, the stop
of the re-run on the host), its re-run on the host from a toolbox, and the session
every command runs with: the home, the adapters, and the targets built over them.

toolchains/cli/ parses argv, asks this module for the session and runs one command.
"""

from __future__ import annotations

import dataclasses
import os
import sys
from typing import TYPE_CHECKING

from cst_core.adapters import SystemClockAdapter
from cst_core.adapters.runner import on_host_argv
from cst_core.adapters.runtime import program_argv
from toolchains.adapters.build_tree import CargoBuildTree
from toolchains.adapters.home_files import RunSummaries, load_settings
from toolchains.adapters.processes import cargo_procs
from toolchains.adapters.program import RunOutput, home_folder, in_container, journal_stdout
from toolchains.adapters.units import UserUnits
from toolchains.adapters.uv import UvTool
from toolchains.application.prune import TargetPorts
from toolchains.application.python_uv import PythonUv
from toolchains.application.rust_build import RustBuild
from toolchains.domain.common import NAME
from toolchains.domain.prune import PYTHON_UV, RUST_BUILD, Limits

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from cst_core.ports import ClockPort
    from toolchains.application.prune import Target
    from toolchains.domain.build import CargoProc
    from toolchains.domain.prune import Settings
    from toolchains.ports import BuildTreePort, RunOutputPort, UvPort


@dataclasses.dataclass(frozen=True)
class Session:
    """What every command runs with: the home it works on, the run's "now", how
    hints name the program, the running program's entry point (its files are next
    to it), the run's bounds, and the adapters."""

    home: str
    now: float
    prog: str
    entry_file: str
    limits: Limits
    out: RunOutput
    units: UserUnits
    clock: ClockPort
    machine: TargetPorts


@dataclasses.dataclass(frozen=True)
class Parts:
    """What a test gives instead of the real thing (None: the real one)."""

    limits: Limits | None = None
    units: Callable[[RunOutputPort], UserUnits] | None = None
    procs: Callable[[], list[CargoProc]] | None = None
    tree: Callable[[str, str, Limits, ClockPort], BuildTreePort] | None = None
    uv: UvPort | None = None
    journal: bool | None = None


def prog_name() -> str:
    """How hints name the frontend ("cst toolchains" through cst)."""
    return os.environ.get("CST_PROG") or "cst toolchains"


def reexec_on_host(argv: Sequence[str], entry_file: str) -> int | None:
    """From a toolbox the processes of other containers cannot be inspected:
    the same program runs on the host (same home path), replacing this one.
    None when it runs here (on the host, or CST_TOOLCHAINS_HERE set); 1 when
    the host cannot be reached (said on stderr)."""
    if not in_container() or os.environ.get("CST_TOOLCHAINS_HERE"):
        return None
    try:
        spawn = on_host_argv([*program_argv(entry_file), *argv], {"CST_PROG": prog_name()})
        os.execvp(spawn[0], spawn)
    except OSError as e:
        print(f"{NAME}: cannot run on the host (flatpak-spawn: {e.strerror}); run it on the host", file=sys.stderr)
    return 1


def session(home_arg: str | None, entry_file: str, parts: Parts | None = None) -> Session:
    """The session of one command: --home (None: the user's), the adapters (PARTS: a test's)."""
    p = Parts() if parts is None else parts
    clock = SystemClockAdapter()
    home = home_folder(home_arg)
    now = float(os.environ.get("CST_TOOLCHAINS_NOW") or clock.now())
    prog = prog_name()
    journal = journal_stdout(os.environ.get("JOURNAL_STREAM", "")) if p.journal is None else p.journal
    out = RunOutput(home, journal=journal)
    limits = Limits() if p.limits is None else p.limits
    if p.tree is not None:
        tree = p.tree(home, prog, limits, clock)
    else:
        tree = CargoBuildTree(home, prog, limits, clock, procs=cargo_procs if p.procs is None else p.procs)
    machine = TargetPorts(tree, UvTool(home) if p.uv is None else p.uv, RunSummaries(home, out), out, clock)
    units = (UserUnits if p.units is None else p.units)(out)
    return Session(home, now, prog, entry_file, limits, out, units, clock, machine)


def settings(s: Session) -> Settings:
    """The checked settings (ConfigError on any bad line or value)."""
    return load_settings(s.home, s.entry_file)


def target(s: Session, checked: Settings | None, tid: str) -> Target:
    """A new target TID (its clock starts now) with its policy from CHECKED
    (None: bad settings; only location, check and status_lines work then).
    KeyError for an id of no target here (a target in SPECS that nothing builds)."""
    if tid == RUST_BUILD.id:
        return RustBuild(s.home, s.now, checked.rust if checked else None, s.machine, s.limits)
    if tid == PYTHON_UV.id:
        return PythonUv(s.home, s.now, checked.uv if checked else None, s.machine, s.limits)
    raise KeyError(tid)

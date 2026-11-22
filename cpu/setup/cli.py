# SPDX-License-Identifier: MPL-2.0
"""The command line of `cst cpu` (cpu-setup.sh): help, cpu-watch's commands, and the root commands.

What it does with each command is cpu-setup.sh's: the help from that script's header; watch, report and
quiet exec cpu-watch (on the host from a toolbox); the root commands refuse in a toolbox, run the script
again through sudo, and as root run on the host (cpu/setup/wiring.py builds them).
"""

from collections.abc import Callable, Sequence
import sys

from cst_core.domain.dispatch import script_help
from cst_core.ports import ModuleRoutePort
from cpu.setup import wiring
from cpu.setup.domain import CpuRoute, route


def main(
    argv: Sequence[str],
    *,
    handed: wiring.Handed | None = None,
    host: ModuleRoutePort | None = None,
    root_command: Callable[[str, Sequence[str]], int] | None = None,
) -> int:
    """Run one command (argv without the program name); its exit status when it does not exec."""
    h = wiring.start() if handed is None else handed
    if not h.script:
        print("usage: cpu/cpu-setup is run by cpu-setup.sh (which hands over CST_SELF)", file=sys.stderr)
        return 2
    machine = wiring.this_machine() if host is None else host
    prog = h.cst_prog or h.arg0
    cmd = argv[0] if argv and argv[0] else "help"  # ${1:-help}
    rest = list(argv[1:])
    where = route(cmd, rest)
    if where == CpuRoute.HELP:
        machine.say("out", script_help(machine.read_text(h.script), prog))
        return 0
    if where == CpuRoute.WATCH:
        return machine.exec_replace(wiring.watch_argv(h, cmd, rest, in_container=machine.is_container()))
    if where == CpuRoute.UNKNOWN:
        machine.say("err", f"Unknown command: {cmd}\n{script_help(machine.read_text(h.script), prog)}")
        return 2
    if machine.is_container():
        machine.say("err", f"'{cmd}' must run on the host, not inside a toolbox/container.\n")
        return 1
    if machine.euid() != 0:
        return machine.exec_replace(wiring.sudo_argv(h, cmd, rest))
    run = wiring.root_command(h, prog) if root_command is None else root_command
    return run(cmd, rest)

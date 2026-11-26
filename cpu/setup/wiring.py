# SPDX-License-Identifier: MPL-2.0
"""The composition root of `cst cpu`: what cpu-setup.sh hands over, the environment, the command lines it
execs (cpu-watch, sudo) and the root commands over the real adapters.

cpu/setup/cli.py parses argv and asks this module for those; the work is in
cpu/setup/adapters.py, the rules in cpu/setup/domain.py.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os

from cst_core.adapters.dispatch import LocalCstDispatchAdapter, default_ctrl_c
from cst_core.adapters.runner import SubprocessCommandRunner, on_host_argv
from cst_core.adapters.runtime import undo_python_start
from cst_core.ports import ModuleRoutePort
from cpu.game.adapters.host import collate_as_bash
from cpu.setup.adapters.setup import CpuSetup, target_user
from cpu.setup.domain import QUIET_ENV, CpuRootCommand, SetupPaths


@dataclass(frozen=True, slots=True)
class Handed:
    """What the shim hands over: script (cpu-setup.sh, its real path; "" when run without it), arg0 (the
    name it was started with), CST_PROG and SUDO_USER as the caller set them ("" when unset), and the
    QUIET_* limits that are set (a toolbox hands them to the host)."""

    script: str
    arg0: str
    cst_prog: str
    sudo_user: str
    quiet: Mapping[str, str]


def start() -> Handed:
    """SIGPIPE at its default and the caller's LC_CTYPE back (as the bash script had them), Ctrl-C ends it
    as it ended bash; CST_SELF and CST_ARG0 taken out of the environment the commands get."""
    undo_python_start()
    default_ctrl_c()
    script = os.environ.pop("CST_SELF", "")
    arg0 = os.environ.pop("CST_ARG0", "") or script
    quiet = {v: os.environ[v] for v in QUIET_ENV if v in os.environ}
    return Handed(script, arg0, os.environ.get("CST_PROG", ""), os.environ.get("SUDO_USER", ""), quiet)


def this_machine() -> ModuleRoutePort:
    """This machine: host or toolbox, the euid, the shim's text, the terminal and exec."""
    return LocalCstDispatchAdapter()


def checkout(handed: Handed) -> str:
    """The CST folder the shim is in."""
    return os.path.dirname(handed.script)


def watch_argv(handed: Handed, cmd: str, rest: Sequence[str], *, in_container: bool) -> list[str]:
    """cpu-watch, cpu-power, or cpu-irq CMD ARGS from the checkout; cpu-watch runs on the host from a toolbox."""
    direct = {CpuRootCommand.POWER: "cpu-power", CpuRootCommand.IRQ: "cpu-irq"}.get(cmd)
    if direct is not None:
        return ["python3", "-I", "-B", os.path.join(checkout(handed), "cpu", direct), *rest]
    argv = ["python3", "-I", "-B", os.path.join(checkout(handed), "cpu", "cpu-watch"), cmd, *rest]
    return on_host_argv(argv, handed.quiet) if in_container else argv


def sudo_argv(handed: Handed, cmd: str, rest: Sequence[str]) -> list[str]:
    """cpu-setup.sh again as root. sudo resets the environment: CST_PROG is handed over (when the caller set it)."""
    handover = ["env", f"CST_PROG={handed.cst_prog}"] if handed.cst_prog else []
    return ["sudo", "--", *handover, handed.script, cmd, *rest]


def root_command(handed: Handed, prog: str) -> Callable[[str, Sequence[str]], int]:
    """The root commands over the real adapters (as root, on the host); names sort as bash's globs
    sorted them (the files uninstall lists)."""
    collate_as_bash()
    paths = SetupPaths()
    setup = CpuSetup(
        paths,
        SubprocessCommandRunner(),
        root=checkout(handed),
        prog=prog,
        user=target_user(handed.sudo_user, paths.conf),
        passthrough=LocalCstDispatchAdapter().run_passthrough,
    )
    return setup.command

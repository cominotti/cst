# SPDX-License-Identifier: MPL-2.0
"""The composition root of `cst swap`: the environment, the sudo re-run and the adapters.

swap/cli.py parses argv, asks this module for what it needs from the
process (the environment, the execution context, the command that runs it
again as root) and for the use case built over the real adapters, and
renders the result.
"""

from collections.abc import Mapping, Sequence
import os
import sys
from typing import NoReturn

from cst_core.adapters import HostExecutionContext, SubprocessCommandRunner, SystemctlAdapter
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.adapters.runtime import checkout_root, lc_ctype_handover, sibling_program, undo_python_start
from cst_core.ports import CommandRunnerPort, ExecutionContextPort, FileSystemPort
from swap.adapters import LinuxSwapSystemAdapter
from swap.application import SwapApplicationService, SwapEnvironment


def start_swap() -> None:
    """SIGPIPE at its default (a closed pipe stops it quietly), the caller's LC_CTYPE back."""
    undo_python_start()


def swap_env() -> Mapping[str, str]:
    """This process's environment, read when asked (tests patch it)."""
    return os.environ


def execution_context() -> ExecutionContextPort:
    """Host or toolbox, root or not, and where the host's /etc and /run are seen from here."""
    return HostExecutionContext()


def sudo_rerun_argv(prog: str, args: Sequence[str]) -> list[str]:
    """The command that runs swap/cli.py again as root, with the same arguments.

    sudo resets the environment: CST_PROG and the caller's LC_CTYPE are
    handed over through env.
    """
    python = [sys.executable, "-I", "-B", sibling_program("swap/cli.py", __file__)]
    return ["sudo", "--", "env", f"CST_PROG={prog}", lc_ctype_handover(), *python, *args]


def sudo_reexec(argv: list[str]) -> NoReturn:
    """Replace this process with argv (a sudo command line)."""
    os.execvp("sudo", argv)


def build_service(
    prog: str,
    ctx: ExecutionContextPort,
    runner: CommandRunnerPort | None,
    fs: FileSystemPort | None,
    *,
    fallback_root: str,
) -> SwapApplicationService:
    """The swap use cases over the real adapters (runner and fs: a test's fakes, or None)."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    file_sys = LocalFileSystemAdapter() if fs is None else fs
    systemd = SystemctlAdapter(cmd_runner)
    swap_system = LinuxSwapSystemAdapter(cmd_runner, file_sys)
    paths = desired_paths(prog, ctx, fallback_root=fallback_root)
    return SwapApplicationService(paths, file_sys, systemd, swap_system)


def desired_paths(prog: str, ctx: ExecutionContextPort, *, fallback_root: str) -> SwapEnvironment:
    """Where swap's desired files are (this checkout, else fallback_root: the bootstrap's root) and the host's /etc and /run."""
    return SwapEnvironment(
        src_dir=os.path.join(checkout_root(__file__) or fallback_root, "swap"),
        etc_dir=ctx.etc_dir(),
        cpu_run_dir=ctx.host_run_dir("cpu-setup"),
        prog=prog,
    )

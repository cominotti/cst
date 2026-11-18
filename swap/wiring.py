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

from cst_core.adapters import SystemctlAdapter
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.adapters.install import program_installer
from cst_core.adapters.runtime import (
    checkout_root,
    lc_ctype_handover,
    sibling_program,
    undo_python_start,
)
from cst_core.domain.record import ROOT_RECORD
from cst_core.ports import CommandRunnerPort, ExecutionContextPort, FileSystemPort
from swap.adapters import LinuxSwapSystemAdapter
from swap.application.apply import SwapApplicationService
from swap.application.reports import SwapEnvironment


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
    env: Mapping[str, str] | None = None,
) -> SwapApplicationService:
    """The swap use cases over the real adapters (runner and fs: a test's fakes, or None; env: the
    environment, for kernel_dirs)."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    file_sys = LocalFileSystemAdapter() if fs is None else fs
    systemd = SystemctlAdapter(cmd_runner)
    swap_system = LinuxSwapSystemAdapter(cmd_runner, file_sys)
    paths = desired_paths(prog, ctx, fallback_root=fallback_root, env=env)
    # swap's files are written through the installer and listed in the install record.
    return SwapApplicationService(
        paths,
        file_sys,
        systemd,
        swap_system,
        installer=program_installer(sibling_program("swap/cli.py", __file__), fallback_root),
    )


def kernel_dirs(env: Mapping[str, str]) -> dict[str, str]:
    """Where /proc and /sys are read and written: CST_SWAP_PROC and CST_SWAP_SYS when set (a test's
    scratch tree: the tier 1 E2E container's own /proc/sys and /sys are read-only and the host's
    kernel), else the defaults. The cgroup tree stays /sys/fs/cgroup either way."""
    out: dict[str, str] = {}
    proc, sys_dir = env.get("CST_SWAP_PROC", ""), env.get("CST_SWAP_SYS", "")
    if proc:
        out["proc_dir"] = proc
    if sys_dir:
        out["sys_dir"] = sys_dir
        out["zswap_params_dir"] = os.path.join(sys_dir, "module", "zswap", "parameters")
        out["zswap_debug_dir"] = os.path.join(sys_dir, "kernel", "debug", "zswap")
    return out


def desired_paths(
    prog: str, ctx: ExecutionContextPort, *, fallback_root: str, env: Mapping[str, str] | None = None
) -> SwapEnvironment:
    """Where swap's desired files are (this checkout, else fallback_root: the bootstrap's root), the
    host's /etc and /run, and the kernel's files (kernel_dirs)."""
    etc = ctx.etc_dir()
    return SwapEnvironment(
        **kernel_dirs(env or {}),
        src_dir=os.path.join(checkout_root(__file__) or fallback_root, "swap"),
        etc_dir=etc,
        cpu_run_dir=ctx.host_run_dir("cpu-setup"),
        prog=prog,
        # Next to the /etc it writes (the host's /var/lib/cst; under a test's folder, that folder's).
        install_record=os.path.join(os.path.dirname(etc.rstrip("/")), ROOT_RECORD.lstrip("/")),
        as_root=ctx.is_root(),
    )

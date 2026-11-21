# SPDX-License-Identifier: MPL-2.0
"""Composition root of `cst btrfs compress-force`."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
import sys
from typing import Final, NoReturn

from btrfs.compress.adapters import CompressAdapterConfig, LinuxCompressHostAdapter
from btrfs.compress.application import CompressService
from btrfs.compress.domain import CompressCommand
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import lc_ctype_handover, program_path, undo_python_start
from cst_core.ports import CommandRunnerPort, ExecutionContextPort, FileSystemPort

SEAM_PROC: Final = "CST_BTRFS_COMPRESS_PROC"


@dataclass(frozen=True, slots=True)
class CompressWiringOverrides:
    """Optional fakes injected by tests when assembling `CompressService`."""

    ctx: ExecutionContextPort | None = None
    runner: CommandRunnerPort | None = None
    fs: FileSystemPort | None = None
    elevate: Callable[[list[str]], NoReturn] | None = None
    host_root: str = ""
    fallback_root: str = ""


def build_sudo_command(prog: str, args: Sequence[str], entry_file: str) -> list[str]:
    """Command line re-executing the checkout entrypoint via sudo."""
    python_argv = [sys.executable, "-I", "-B", program_path(entry_file)]
    return ["sudo", "--", "env", f"CST_PROG={prog}", lc_ctype_handover(), *python_argv, *args]


def exec_sudo_process(argv: list[str]) -> NoReturn:
    """Replace current process with `sudo`."""
    os.execvp("sudo", argv)


def compress_execution_context() -> ExecutionContextPort:
    """Host/container and root/non-root execution probe."""
    return HostExecutionContext()


def read_compress_env() -> Mapping[str, str]:
    """Current process environment (tests pass an explicit mapping)."""
    return os.environ


def start_compress_process() -> None:
    """Restore default SIGPIPE/SIGXFSZ and caller LC_CTYPE."""
    undo_python_start()


def ensure_host_root(
    cmd: CompressCommand,
    prog: str,
    args: Sequence[str],
    entry_file: str,
    overrides: CompressWiringOverrides,
) -> str | None:
    """Enforce host execution and root privilege for mutating commands; return refusal text or None."""
    if cmd not in (CompressCommand.APPLY, CompressCommand.REVERT):
        return None
    exec_ctx = compress_execution_context() if overrides.ctx is None else overrides.ctx
    if exec_ctx.is_container():
        return (
            f"'{prog} {cmd.value}' must run on the host, not inside a toolbox/container "
            "(needs the host's /etc/fstab, rpm-ostree, mount, and systemd)."
        )
    if not exec_ctx.is_root():
        reexec = exec_sudo_process if overrides.elevate is None else overrides.elevate
        reexec(build_sudo_command(prog, args, entry_file))
    return None


def assemble_compress_service(
    entry_file: str,
    env: Mapping[str, str],
    overrides: CompressWiringOverrides,
) -> CompressService:
    """Build `CompressService` over real or injected adapters without globals."""
    seam_proc = env.get(SEAM_PROC, "")
    cfg = CompressAdapterConfig(
        entry_file=entry_file,
        fallback_root=overrides.fallback_root,
        proc_dir=seam_proc or "/proc",
        host_root=overrides.host_root,
    )
    adapter = LinuxCompressHostAdapter(
        cfg,
        ctx=overrides.ctx or compress_execution_context(),
        runner=overrides.runner or SubprocessCommandRunner(),
        fs=overrides.fs or LocalFileSystemAdapter(),
    )
    return CompressService(adapter, seam_proc=seam_proc)

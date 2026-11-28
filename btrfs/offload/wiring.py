# SPDX-License-Identifier: MPL-2.0
"""Composition root for `cst btrfs cache offload` (`btrfs/btrfs-cache-offload`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from typing import Final, TextIO

from btrfs.offload.adapters import (
    LocalOffloadHost,
    OffloadInstallerAdapter,
    OffloadUserSystemd,
    is_effective_root,
    resolve_home_and_proc,
)
from btrfs.offload.application import OffloadService
from btrfs.offload.domain import (
    OFFLOAD_PROG_DEFAULT,
    OffloadAction,
    OffloadParsedArgs,
    format_offload_usage,
    is_offload_help,
    offload_parse_error,
    parse_offload_args,
)
from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import undo_python_start
from cst_core.adapters.terminal import stderr, stdout
from cst_core.domain.values import MINUTE
from cst_core.ports import CommandRunnerPort, ExecutionContextPort

OFFLOAD_SEAM_ROOT_ENV: Final = "CST_BTRFS_OFFLOAD_ROOT"
OFFLOAD_HERE_ENV: Final = "CST_BTRFS_OFFLOAD_HERE"
OFFLOAD_SYSTEMCTL_ENV: Final = "CST_BTRFS_OFFLOAD_SYSTEMCTL"


@dataclass(frozen=True, slots=True)
class OffloadCliDeps:
    """Injectable environment and fake ports for tests."""

    entry_file: str = ""
    env: Mapping[str, str] | None = None
    out: TextIO | None = None
    err: TextIO | None = None
    ctx: ExecutionContextPort | None = None
    runner: CommandRunnerPort | None = None


def _user_manager_factory(
    env_map: Mapping[str, str],
    runner: CommandRunnerPort,
) -> SystemctlAdapter:
    stub = env_map.get(OFFLOAD_SYSTEMCTL_ENV) or None
    user_env = {
        k: v
        for k, v in env_map.items()
        if k in ("HOME", "USER", "LOGNAME", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "PATH")
    }
    return SystemctlAdapter(
        runner,
        timeout=MINUTE,
        user=True,
        program=stub,
        on_host=SystemctlHost.EVERY,
        base_env=user_env,
        stdin="inherit",
    )


def build_offload_service(
    env_map: Mapping[str, str],
    entry_file: str,
    runner: CommandRunnerPort,
) -> OffloadService:
    """Wire `OffloadService` over real host adapters."""
    seam_root = env_map.get(OFFLOAD_SEAM_ROOT_ENV) or ""
    home, proc_root = resolve_home_and_proc(env_map, seam_root)
    prog = env_map.get("CST_PROG") or OFFLOAD_PROG_DEFAULT
    host = LocalOffloadHost(home=home, prog=prog, runner=runner, proc_root=proc_root)
    units = OffloadUserSystemd(lambda: _user_manager_factory(env_map, runner), runner=runner)
    installer = OffloadInstallerAdapter(entry_file)
    return OffloadService(host=host, units=units, installer=installer)


def _write_refusal(
    parsed: OffloadParsedArgs,
    env_map: Mapping[str, str],
    ctx: ExecutionContextPort,
    prog: str,
) -> str | None:
    if parsed.action in (OffloadAction.STATUS, OffloadAction.PLAN, OffloadAction.HELP):
        return None
    if bool(env_map.get(OFFLOAD_SEAM_ROOT_ENV)):
        return None
    if is_effective_root():
        return f"{prog}: refusing to run `{parsed.action.value}` as root; run it as your normal user on the host"
    if ctx.is_container() and not bool(env_map.get(OFFLOAD_HERE_ENV)):
        return f"{prog}: refusing to run `{parsed.action.value}` inside a container; run it directly on the host"
    return None


def start_offload_process() -> None:
    """Restore default SIGPIPE/SIGXFSZ and caller LC_CTYPE."""
    undo_python_start()


def run_offload(argv: Sequence[str], *, deps: OffloadCliDeps | None = None) -> int:
    """Execute `cst btrfs cache offload` from CLI args."""
    cfg = OffloadCliDeps() if deps is None else deps
    env_map = os.environ if cfg.env is None else cfg.env
    out_fh = stdout() if cfg.out is None else cfg.out
    err_fh = stderr() if cfg.err is None else cfg.err
    prog = env_map.get("CST_PROG") or OFFLOAD_PROG_DEFAULT

    parsed = parse_offload_args(argv)
    if (err_info := offload_parse_error(parsed)) is not None:
        code, msg = err_info
        err_fh.write(f"{prog}: {msg}\n\n{format_offload_usage(prog)}")
        return code
    if is_offload_help(parsed):
        out_fh.write(format_offload_usage(prog))
        return 0

    ctx = HostExecutionContext() if cfg.ctx is None else cfg.ctx
    if (refusal := _write_refusal(parsed, env_map, ctx, prog)) is not None:
        err_fh.write(f"{refusal}\n")
        return 1

    runner = SubprocessCommandRunner() if cfg.runner is None else cfg.runner
    svc = build_offload_service(env_map, cfg.entry_file, runner)
    rc, lines = svc.run(parsed)
    for line in lines:
        target = err_fh if rc != 0 and line.startswith("ERROR:") else out_fh
        target.write(f"{line}\n")
    return rc

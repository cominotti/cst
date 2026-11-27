# SPDX-License-Identifier: MPL-2.0
"""The composition root of `cst btrfs cache` (`btrfs/btrfs-cache`): the use cases of application.py built
over the real adapters, the command line's action, and the managed command line (help, refusals, sudo
through run_managed_cli).

btrfs/cache/cli.py hands argv to this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import os

from btrfs.cache import domain as cache_mount_domain
from btrfs.cache.adapters.migrate import CacheMigrationAdapter
from btrfs.cache.adapters.mounts import (
    CacheAccounts,
    CacheFolders,
    CacheHostView,
    CacheMountTable,
    CacheSystemd,
    FstabFile,
    cache_runtime,
)
from btrfs.cache.adapters.provision import CacheProvisionAdapter
from btrfs.cache.adapters.system import CacheSystemBindAdapter
from btrfs.cache.application.mounts import CacheMounts
from btrfs.cache.application.system import SystemEnv, run_system_action
from btrfs.cache.domain import CacheMountAction, CacheMountParsedArgs
from btrfs.cache.domain.format import format_cache_mount_usage
from btrfs.cache.domain.system_spec import SystemAction, SystemArgsResult, parse_system_args
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli, undo_python_start
from cst_core.domain.values import ExitAndLines
from cst_core.ports import CommandRunnerPort, ExecutionContextPort

CacheMountCliDeps = ManagedCliEnv


@dataclass(frozen=True, slots=True)
class _CacheCliArgs:
    base: CacheMountParsedArgs
    system: SystemArgsResult | None = None


def start_cache_mount_process() -> None:
    """Restore default SIGPIPE/SIGXFSZ and caller LC_CTYPE."""
    undo_python_start()


def build_cache_mounts(
    env_map: Mapping[str, str], ctx: ExecutionContextPort | None, runner: CommandRunnerPort | None
) -> CacheMounts:
    """The use cases over the real files and commands: the test root from env_map (CST_BTRFS_CACHE_ROOT), ctx
    and runner (None: the host's, a subprocess)."""
    rt = cache_runtime(env_map, ctx, runner)
    mounts = CacheMounts(
        FstabFile(rt), CacheMountTable(rt), CacheSystemd(rt), CacheFolders(rt), CacheHostView(rt), CacheAccounts(rt)
    )
    return replace(
        mounts,
        provision=CacheProvisionAdapter(rt),
        migration=CacheMigrationAdapter(rt),
        system_binds=CacheSystemBindAdapter(rt),
        seam_root=rt.seam_root,
    )


def _parse_cli_args(argv: Sequence[str]) -> _CacheCliArgs:
    base = cache_mount_domain.parse_cache_mount_args(argv)
    if base.action == CacheMountAction.SYSTEM:
        return _CacheCliArgs(base=base, system=parse_system_args(base.system_argv))
    return _CacheCliArgs(base=base)


def _is_cli_help(parsed: _CacheCliArgs) -> bool:
    if parsed.system is not None and parsed.system.action == SystemAction.HELP:
        return True
    return cache_mount_domain.is_cache_mount_help(parsed.base)


def _cli_parse_error(parsed: _CacheCliArgs) -> tuple[int, str] | None:
    if parsed.system is not None and parsed.system.error is not None:
        return (parsed.system.code, parsed.system.error)
    return cache_mount_domain.cache_mount_parse_error(parsed.base)


def _cli_write_action(parsed: _CacheCliArgs) -> str | None:
    if parsed.system is not None:
        if parsed.system.action in (SystemAction.APPLY, SystemAction.REVERT):
            return f"system {parsed.system.action.value}"
        return None
    return cache_mount_domain.cache_mount_write_action(parsed.base)


def dispatch_cache_mount_cli_action(parsed: _CacheCliArgs, env: ManagedCliEnv, *, read_only: bool) -> ExitAndLines:
    """Resolve runtime dependencies and execute the requested `cache` subcommand."""
    del read_only
    env_map = os.environ if env.env is None else env.env
    prog = env_map.get("CST_PROG") or "cst btrfs cache"
    mounts = build_cache_mounts(env_map, env.ctx, env.runner)
    if parsed.system is not None:
        sys_env = SystemEnv(
            binds=mounts.system_binds,
            fstab=mounts.fstab,
            mounts=mounts.mounts,
            systemd=mounts.systemd,
            seam_root=mounts.seam_root,
        )
        return run_system_action(parsed.system, sys_env, prog)
    return mounts.run(parsed.base, prog)


_CACHE_MOUNT_SPEC = ManagedCliSpec(
    default_prog="cst btrfs cache",
    parse=_parse_cli_args,
    format_usage=format_cache_mount_usage,
    is_help=_is_cli_help,
    parse_error=_cli_parse_error,
    write_action=_cli_write_action,
    elevation_reason="configures the NVMe cache drive, per-user directories, /etc/fstab bind mounts, and live mounts",
    execute=dispatch_cache_mount_cli_action,
    is_err_line=cache_mount_domain.is_cache_mount_err,
)


def run_cache_mount(args: Sequence[str], *, deps: CacheMountCliDeps | None = None) -> int:
    """One run of the command line with args: help, usage errors, host and root refusals for writes (sudo
    re-runs it as root), then the action."""
    return run_managed_cli(args, _CACHE_MOUNT_SPEC, deps)

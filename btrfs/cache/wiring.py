# SPDX-License-Identifier: MPL-2.0
"""The composition root of `cst btrfs cache` (`btrfs/btrfs-cache`): the use cases of application.py built
over the real adapters, the command line's action, and the managed command line (help, refusals, sudo
through run_managed_cli).

btrfs/cache/cli.py hands argv to this module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

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
from btrfs.cache.application.mounts import CacheMounts
from btrfs.cache.domain import CacheMountParsedArgs
from btrfs.cache.domain.format import format_cache_mount_usage
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli, undo_python_start
from cst_core.domain.values import ExitAndLines
from cst_core.ports import CommandRunnerPort, ExecutionContextPort

CacheMountCliDeps = ManagedCliEnv


def start_cache_mount_process() -> None:
    """Restore default SIGPIPE/SIGXFSZ and caller LC_CTYPE."""
    undo_python_start()


def build_cache_mounts(
    env_map: Mapping[str, str], ctx: ExecutionContextPort | None, runner: CommandRunnerPort | None
) -> CacheMounts:
    """The use cases over the real files and commands: the test root from env_map (CST_BTRFS_CACHE_ROOT), ctx
    and runner (None: the host's, a subprocess)."""
    rt = cache_runtime(env_map, ctx, runner)
    return CacheMounts(
        fstab=FstabFile(rt),
        mounts=CacheMountTable(rt),
        systemd=CacheSystemd(rt),
        folders=CacheFolders(rt),
        host=CacheHostView(rt),
        accounts=CacheAccounts(rt),
        provision=CacheProvisionAdapter(rt),
        migration=CacheMigrationAdapter(rt),
        seam_root=rt.seam_root,
    )


def dispatch_cache_mount_cli_action(
    parsed: CacheMountParsedArgs, env: ManagedCliEnv, *, read_only: bool
) -> ExitAndLines:
    """Resolve runtime dependencies and execute the requested `cache` subcommand."""
    del read_only
    env_map = os.environ if env.env is None else env.env
    prog = env_map.get("CST_PROG") or "cst btrfs cache"
    return build_cache_mounts(env_map, env.ctx, env.runner).run(parsed, prog)


_CACHE_MOUNT_SPEC = ManagedCliSpec(
    default_prog="cst btrfs cache",
    parse=cache_mount_domain.parse_cache_mount_args,
    format_usage=format_cache_mount_usage,
    is_help=cache_mount_domain.is_cache_mount_help,
    parse_error=cache_mount_domain.cache_mount_parse_error,
    write_action=cache_mount_domain.cache_mount_write_action,
    elevation_reason="configures the NVMe cache drive, per-user directories, /etc/fstab bind mounts, and live mounts",
    execute=dispatch_cache_mount_cli_action,
    is_err_line=cache_mount_domain.is_cache_mount_err,
)


def run_cache_mount(args: Sequence[str], *, deps: CacheMountCliDeps | None = None) -> int:
    """One run of the command line with args: help, usage errors, host and root refusals for writes (sudo
    re-runs it as root), then the action."""
    return run_managed_cli(args, _CACHE_MOUNT_SPEC, deps)

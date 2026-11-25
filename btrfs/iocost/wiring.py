# SPDX-License-Identifier: MPL-2.0
"""The composition root of `cst btrfs iocost` (`btrfs/btrfs-iocost`): the use cases of application.py built
over the real adapters, the fronts that run them, the command line's action, and the managed command line
(help, refusals, sudo through run_managed_cli).

btrfs/iocost/cli.py hands argv to this module.
"""

from __future__ import annotations

from collections.abc import Sequence

from btrfs.iocost import domain as iocost_domain
from btrfs.iocost.adapters import (
    IocostInstaller,
    IocostLockFile,
    IocostPaths,
    IocostSysfs,
    IocostSystemd,
    IocostUdev,
    IocostUninstall,
    ManagedDevicesFile,
    QosControlFile,
    iocost_paths_for,
    udev_properties,
)
from btrfs.iocost.application import Iocost, with_udev_answer
from btrfs.iocost.domain import BlockDisk, IocostAction, IocostParsedArgs, IocostSnapshot
from cst_core.adapters.install import program_installer
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli, undo_python_start
from cst_core.application.install import InstallService
from cst_core.ports import CommandRunnerPort

IocostCliDeps = ManagedCliEnv


def start_iocost_process() -> None:
    """Restore default SIGPIPE/SIGXFSZ and caller LC_CTYPE."""
    undo_python_start()


def build_iocost(
    paths: IocostPaths, runner: CommandRunnerPort | None, *, uninstall: InstallService | None = None
) -> Iocost:
    """The use cases over the real files, commands and locks at paths, through runner (None: a
    SubprocessCommandRunner); uninstall: the InstallService a full revert removes btrfs:iocost with."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    return Iocost(
        sysfs=IocostSysfs(paths.sys_block, paths.controllers_path, paths.model_path),
        qos=QosControlFile(paths.qos_path),
        managed=ManagedDevicesFile(paths.managed_file),
        udev=IocostUdev(cmd_runner, paths.udev_control, paths.rule_dest),
        systemd=IocostSystemd(cmd_runner),
        installer=IocostInstaller(cmd_runner, paths.installer_script, paths.install_root),
        qos_lock=IocostLockFile(paths.lock_path, paths.lock_timeout),
        component_lock=IocostLockFile(paths.component_lock_path, paths.lock_timeout),
        uninstall=None if uninstall is None else IocostUninstall(uninstall, paths.install_root, paths.record_path),
    )


def with_udev_properties(runner: CommandRunnerPort, disk: BlockDisk) -> BlockDisk:
    """disk with what udev knows of it: its hwdb solution, whether udev has processed it, or why
    `udevadm info` could not tell (missing, failed: the disk is judged as before)."""
    return with_udev_answer(disk, udev_properties(runner, disk.name))


def collect_iocost_snapshot(
    paths: IocostPaths, *, runner: CommandRunnerPort | None = None, udev: bool = True
) -> IocostSnapshot:
    """Collect kernel `io.cost.qos`, block devices, runtime state, and systemd unit state.

    udev: ask udev about each disk the scan of every disk would write (its hwdb solution); a --device
    run and revert do not need it."""
    return build_iocost(paths, runner).snapshot(udev=udev)


def apply_iocost(
    paths: IocostPaths,
    *,
    runner: CommandRunnerPort | None = None,
    live_only: bool = False,
    target_device: str | None = None,
) -> tuple[int, list[str]]:
    """Enable `blk-iocost` in `io.cost.qos` and optionally install unit + udev persistence (Iocost.apply)."""
    return build_iocost(paths, runner).apply(live_only=live_only, target_device=target_device)


def revert_iocost(
    paths: IocostPaths,
    *,
    live_only: bool = False,
    runner: CommandRunnerPort | None = None,
    installer: InstallService | None = None,
) -> tuple[int, list[str]]:
    """Disable CST-managed `blk-iocost` entries in `io.cost.qos` and optionally uninstall unit/rule
    (Iocost.revert). A full revert removes btrfs:iocost through installer (None: program_installer, built
    here, before any lock is taken)."""
    if live_only:
        return build_iocost(paths, runner).revert(live_only=True)
    svc = program_installer(paths.entry_file, paths.fallback_root) if installer is None else installer
    return build_iocost(paths, runner, uninstall=svc).revert(live_only=False)


def component_changes(paths: IocostPaths, runner: CommandRunnerPort) -> tuple[str, ...] | None:
    """The btrfs:iocost files `apply` would write or record again (IocostInstaller.changes)."""
    return IocostInstaller(runner, paths.installer_script, paths.install_root).changes()


def plan_iocost(
    paths: IocostPaths, runner: CommandRunnerPort, *, live_only: bool, target_device: str | None
) -> tuple[int, list[str]]:
    """`cst btrfs iocost plan`: what apply would write, changing nothing."""
    return build_iocost(paths, runner).plan(live_only=live_only, target_device=target_device)


def dispatch_iocost_cli_action(
    parsed: IocostParsedArgs, env: ManagedCliEnv, *, read_only: bool
) -> tuple[int, list[str]]:
    """Resolve `IocostPaths` and invoke the requested `btrfs-iocost` subcommand."""
    paths = iocost_paths_for(env.entry_file, ctx=env.ctx, read_only=read_only, fallback_root=env.fallback_root)
    match parsed.action:
        case IocostAction.STATUS:
            return build_iocost(paths, env.runner).status()
        case IocostAction.PLAN:
            return build_iocost(paths, env.runner).plan(live_only=parsed.live_only, target_device=parsed.device)
        case IocostAction.APPLY:
            return apply_iocost(paths, live_only=parsed.live_only, target_device=parsed.device, runner=env.runner)
        case _:
            return revert_iocost(paths, live_only=parsed.live_only, runner=env.runner)


_IOCOST_SPEC = ManagedCliSpec(
    default_prog="cst btrfs iocost",
    parse=iocost_domain.parse_iocost_args,
    format_usage=iocost_domain.format_iocost_usage,
    is_help=lambda p: p.action == IocostAction.HELP,
    parse_error=lambda p: None if p.error is None else (p.code, p.error),
    write_action=lambda p: p.action.value if iocost_domain.is_iocost_write(p.action) else None,
    elevation_reason="configures /sys/fs/cgroup/io.cost.qos, systemd unit, and udev rule",
    execute=dispatch_iocost_cli_action,
)


def run_iocost(args: Sequence[str], *, deps: IocostCliDeps | None = None) -> int:
    """One run of the command line with args: help, usage errors, host and root refusals for writes (sudo
    re-runs it as root), then the action."""
    return run_managed_cli(args, _IOCOST_SPEC, deps)

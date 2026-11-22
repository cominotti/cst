# SPDX-License-Identifier: MPL-2.0
"""CLI wrapper for `btrfs-iocost` / `cst btrfs iocost`."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.iocost import adapters as iocost_adapters, domain as iocost_domain
from btrfs.iocost.domain import IocostAction
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli, undo_python_start

IocostCliDeps = ManagedCliEnv

_IOCOST_SPEC = ManagedCliSpec(
    default_prog="cst btrfs iocost",
    parse=iocost_domain.parse_iocost_args,
    format_usage=iocost_domain.format_iocost_usage,
    is_help=lambda p: p.action == IocostAction.HELP,
    parse_error=lambda p: None if p.error is None else (p.code, p.error),
    write_action=lambda p: p.action.value if iocost_domain.is_iocost_write(p.action) else None,
    elevation_reason="configures /sys/fs/cgroup/io.cost.qos, systemd unit, and udev rule",
    execute=iocost_adapters.dispatch_iocost_cli_action,
)


def run_iocost_cli(argv: Sequence[str] | None = None, *, deps: IocostCliDeps | None = None) -> int:
    """Parse arguments, enforce host/root on writes, and invoke `btrfs-iocost` adapters."""
    return run_managed_cli(sys.argv[1:] if argv is None else argv, _IOCOST_SPEC, deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point invoked by `btrfs/btrfs-iocost`."""
    undo_python_start()
    return run_iocost_cli(argv, deps=IocostCliDeps(entry_file=entry_file))

# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup [VM] [--full]`: the local copy of a shut-off VM (reflink or full)."""

from vm import wiring
from vm.application.backup import backup_local
from vm.domain import VmSettings


def cmd_backup(cfg: VmSettings, vm: str, full: bool) -> int:
    backup_local(cfg, vm, wiring.local_backup(cfg), full=full)
    return 0

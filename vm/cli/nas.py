# SPDX-License-Identifier: MPL-2.0
"""`cst vm backup [VM] --nas`: the NAS backup with restic (vm.application.nas)."""

from vm import wiring
from vm.application.nas import backup_nas
from vm.domain import VmSettings


def cmd_backup_nas(cfg: VmSettings, vm: str, keep: str | None, quiesce: bool) -> int:
    backup_nas(cfg, vm, keep, wiring.nas_backup(cfg), quiesce=quiesce)
    return 0

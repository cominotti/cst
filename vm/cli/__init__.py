# SPDX-License-Identifier: MPL-2.0
"""The `cst vm` command line (the program behind vm/cst-vm): backup, the NAS
backup and its weekly timer, list, status, tune, nocow.

A driving adapter: argv in, the ports vm.wiring builds handed to the use
cases of vm.application (or read directly, for list and status), the
answer printed. One module per
command group (vm.cli.backup, nas, timer, status, tune, nocow); main is here.
"""

from collections.abc import Sequence
import sys
from typing import assert_never

from cst_core.domain.errors import Refused, Usage
from cst_core.domain.text import first_line
from cst_core.ports import WriteFailed
from vm import wiring
from vm.cli import backup, nas, nocow, status, timer, tune
from vm.domain import VmCommand, VmSettings, VmSub, parse_args
from vm.domain.backup import Stop


# The usage line, after "usage: PROG ".
USAGE = (
    "backup [VM] [--full] | backup [VM] --nas [--keep-tag NAME] [--no-quiesce] | "
    "backup-timer on|off|status [VM] | list [VM] | status [VM] | "
    "tune plan|apply [VM] [--guest-has-virtio] | nocow plan [--root] | nocow apply | help"
)


def main(argv: Sequence[str], doc: str, entry_file: str, settings: VmSettings | None = None) -> int:
    """`cst vm ...` (argv without the program name). doc is the program's help;
    entry_file the running cst-vm (backup-timer installs it); settings: a test's."""
    cfg = wiring.vm_settings() if settings is None else settings
    try:
        a = parse_args(argv)
    except Usage as e:
        print(f"{cfg.prog}: {e}\nusage: {cfg.prog} {USAGE}", file=sys.stderr)
        return 2
    cmd, vm = a.cmd, a.vm
    try:
        wiring.prepare(cfg, a)
        if cmd == VmCommand.HELP:
            print(doc)
            return 0
        if cmd == VmCommand.LIST:
            status.cmd_list(cfg, vm)
            return 0
        if cmd == VmCommand.NOCOW:
            return nocow.cmd_nocow_plan(cfg, a.root) if a.sub == VmSub.PLAN else nocow.cmd_nocow_apply(cfg)
        vm = vm or wiring.settings_port(cfg).default_vm()
        match cmd:
            case VmCommand.STATUS:
                status.cmd_status(cfg, vm, entry_file)
            case VmCommand.TUNE:
                return tune.cmd_tune(cfg, vm, a.sub == VmSub.APPLY, a.guest_virtio)
            case VmCommand.BACKUP_TIMER:
                timer.cmd_timer(cfg, a.sub or VmSub.STATUS, vm, entry_file)
            case VmCommand.BACKUP:
                wiring.safe_terminal()  # a closed terminal or pipe never cuts a backup or its cleanup short
                if a.nas:
                    nas.cmd_backup_nas(cfg, vm, a.keep, not a.no_quiesce)
                else:
                    backup.cmd_backup(cfg, vm, a.full)
            case _:
                assert_never(cmd)  # every command is handled above
        return 0
    except (Refused, Stop, WriteFailed) as e:
        print(f"{cfg.prog}: {e}", file=sys.stderr)
        if cmd == VmCommand.BACKUP and a.nas and cfg.under_systemd and "DISK NOT MERGED" not in str(e):
            # Started by systemd (the weekly timer): nobody reads the terminal.
            wiring.failure_alert(
                cfg,
                f"Weekly VM backup of {vm} to the NAS failed",
                f"{first_line(str(e))[:300]} See: journalctl -u cst-vm-backup@{vm}.service",
            )
        return 1

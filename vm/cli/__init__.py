# SPDX-License-Identifier: MPL-2.0
"""The `cst vm` command line (the program behind vm/cst-vm): backup, the NAS
backup and its weekly timer, list, status, tune, nocow.

A driving adapter: argv in, the rules of vm.domain applied to what
vm.adapters reads from the host, the answer printed. One module per
command group (vm.cli.backup, nas, timer, status, tune, nocow); main is here.
"""

from collections.abc import Sequence
import os
import sys
from typing import assert_never

from cst_core.domain.errors import Refused, Usage
from cst_core.domain.text import first_line
from cst_core.ports import WriteFailed
from vm.adapters import PROG, Stop, default_vm, notify, prepare_command, safe_output
from vm.cli import backup, nas, nocow, status, timer, tune
from vm.domain import parse_args

USAGE = (
    f"usage: {PROG} backup [VM] [--full] | backup [VM] --nas [--keep-tag NAME] [--no-quiesce] | "
    f"backup-timer on|off|status [VM] | list [VM] | status [VM] | "
    f"tune plan|apply [VM] [--guest-has-virtio] | nocow plan [--root] | nocow apply | help"
)


def main(argv: Sequence[str], doc: str, entry_file: str) -> int:
    """`cst vm ...` (argv without the program name). doc is the program's help;
    entry_file the running cst-vm (backup-timer installs it)."""
    try:
        a = parse_args(argv)
    except Usage as e:
        print(f"{PROG}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    cmd, vm = a.cmd, a.vm
    try:
        prepare_command(cmd, a.sub, a.nas, vm)
        if cmd == "help":
            print(doc)
            return 0
        if cmd == "list":
            return status.cmd_list(vm)
        if cmd == "nocow":
            return nocow.cmd_nocow_plan(a.root) if a.sub == "plan" else nocow.cmd_nocow_apply()
        vm = vm or default_vm()
        if cmd == "status":
            return status.cmd_status(vm)
        if cmd == "tune":
            return tune.cmd_tune(vm, a.sub == "apply", a.guest_virtio)
        if cmd == "backup-timer":
            return timer.cmd_timer(a.sub or "status", vm, entry_file)
        if cmd != "backup":
            assert_never(cmd)  # every command is handled above
        safe_output()  # a closed terminal or pipe never cuts a backup or its cleanup short
        return nas.cmd_backup_nas(vm, a.keep, not a.no_quiesce) if a.nas else backup.cmd_backup(vm, a.full)
    except (Refused, Stop, WriteFailed) as e:
        print(f"{PROG}: {e}", file=sys.stderr)
        if cmd == "backup" and a.nas and os.environ.get("INVOCATION_ID") and "DISK NOT MERGED" not in str(e):
            # Started by systemd (the weekly timer): nobody reads the terminal.
            notify(
                "normal",
                f"Weekly VM backup of {vm} to the NAS failed",
                f"{first_line(str(e))[:300]} See: journalctl -u cst-vm-backup@{vm}.service",
            )
        return 1

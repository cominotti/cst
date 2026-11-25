# SPDX-License-Identifier: MPL-2.0
"""btrfs-snapshot-runtime's command line: the commands of btrfs-snapshot that the bash front
(btrfs/btrfs-snapshot) hands over, as it ran them."""

from collections.abc import Sequence
import os

from btrfs.snapshot_runtime.adapters.commands import COMMANDS, INTERNAL_COMMANDS, unknown_command
from btrfs.snapshot_runtime.adapters.machine import SnapshotRun, keep_bytes_on_output, sigpipe_handed_over
from cst_core.adapters.dispatch import default_ctrl_c
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.runtime import restore_lc_ctype


def main(argv: Sequence[str]) -> int:
    """argv: the program and the command line btrfs-snapshot got."""
    default_ctrl_c()
    restore_lc_ctype()
    keep_bytes_on_output()
    cmd = argv[1] if len(argv) > 1 else ""
    if (command := COMMANDS.get(cmd)) is None:
        # An internal command runs before the start; any other is a usage error.
        return INTERNAL_COMMANDS.get(cmd, unknown_command)(argv[2:])
    m = SnapshotRun(os.environ, SubprocessCommandRunner(keep_signals=sigpipe_handed_over()), argv[0])
    m.start()
    return command(m, argv[2:])

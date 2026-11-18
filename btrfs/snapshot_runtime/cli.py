# SPDX-License-Identifier: MPL-2.0
"""btrfs-snapshot-runtime's command line: the commands of btrfs-snapshot that the bash front
(btrfs/btrfs-snapshot) hands over, as it ran them."""

from collections.abc import Sequence
import io
import os
import sys

from btrfs.snapshot_runtime.adapters.commands import COMMANDS
from btrfs.snapshot_runtime.adapters.machine import SnapshotRun
from btrfs.snapshot_runtime.domain import EXIT_USAGE, USAGE
from cst_core.adapters.dispatch import default_ctrl_c
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.runtime import restore_lc_ctype


def main(argv: Sequence[str]) -> int:
    """argv: the program and the command line btrfs-snapshot got."""
    default_ctrl_c()
    restore_lc_ctype()
    # A setting or path that is not UTF-8 goes out as the bytes it was, as bash wrote it.
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="surrogateescape")
    cmd = argv[1] if len(argv) > 1 else ""
    command = COMMANDS.get(cmd)
    if command is None:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    m = SnapshotRun(os.environ, SubprocessCommandRunner(), argv[0])
    m.start()
    return command(m, argv[2:])

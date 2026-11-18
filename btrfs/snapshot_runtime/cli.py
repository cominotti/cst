# SPDX-License-Identifier: MPL-2.0
"""btrfs-snapshot-runtime's command line: the commands of btrfs-snapshot that the bash front
(btrfs/btrfs-snapshot) hands over, as it ran them."""

from collections.abc import Sequence
import os
import signal
import sys

from btrfs.snapshot_runtime.adapters.commands import COMMANDS
from btrfs.snapshot_runtime.adapters.machine import SnapshotRun, keep_bytes_on_output
from btrfs.snapshot_runtime.domain import EXIT_USAGE, USAGE
from cst_core.adapters.dispatch import default_ctrl_c
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.runtime import restore_lc_ctype


def sigpipe_handed_over() -> bool:
    """CST_SIGPIPE_IGNORED=1 (which goes): the front started with SIGPIPE ignored, as systemd runs
    its services. Python ignores SIGPIPE whatever it got, so only the front can tell. Then the
    commands this run starts get SIGPIPE ignored too, as bash's children did (a closed journal
    stream is EPIPE for them, not a kill), and SIGXFSZ at its default, as bash had it; otherwise
    both at their default (subprocess restores them)."""
    if os.environ.pop("CST_SIGPIPE_IGNORED", "") != "1":
        return False
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    return True


def main(argv: Sequence[str]) -> int:
    """argv: the program and the command line btrfs-snapshot got."""
    default_ctrl_c()
    restore_lc_ctype()
    keep_bytes_on_output()
    cmd = argv[1] if len(argv) > 1 else ""
    command = COMMANDS.get(cmd)
    if command is None:
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    m = SnapshotRun(os.environ, SubprocessCommandRunner(keep_signals=sigpipe_handed_over()), argv[0])
    m.start()
    return command(m, argv[2:])

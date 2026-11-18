# SPDX-License-Identifier: MPL-2.0
"""btrfs-health-check's run and main(): every check, then the OK line or the alert."""

from collections.abc import Sequence
import os
import sys

from btrfs.health_check.adapters import (
    Probe,
    Members,
    alert,
    check_filesystem,
    degraded_boot_problems,
    housekeeping,
    kernel_log,
    make_state_dir,
    md_problems,
    mounts,
    smart,
    snapshot_problems,
    swap_disks,
)
from btrfs.health_check.domain import TAG, ok_line
from cst_core.adapters.dispatch import default_ctrl_c
from cst_core.adapters.journal import log
from cst_core.adapters.runner import SubprocessCommandRunner


def run_check(m: Probe) -> int:
    """The whole check: 0 healthy, 1 at least one problem (alerted)."""
    make_state_dir(m)
    housekeeping(m)
    problems: list[str] = []
    members = Members()
    found = mounts(m)
    for mnt in found:
        problems += check_filesystem(m, mnt, members)
    problems += degraded_boot_problems(m)
    problems += kernel_log(m, [*members.dms, *members.disks])
    problems += snapshot_problems(m)
    problems += md_problems(m)
    disks = sorted({d for d in members.disks if d})
    for disk in disks:
        problems += smart(m, disk)
    # Swap has no second copy: a failing swap disk kills or hangs the programs whose memory was
    # swapped out, so its health log is read as well.
    swap = swap_disks(m, disks)
    for disk in swap:
        problems += smart(m, disk, "swap")
    if not problems:
        log(6, ok_line(found, disks, swap), tag=TAG)
        return 0
    for p in problems:
        log(2, p, tag=TAG, stream=sys.stderr)
    alert(m, problems)
    return 1


def main(argv: Sequence[str]) -> int:
    del argv  # it takes no arguments (and ignores any)
    default_ctrl_c()
    return run_check(Probe(os.environ.get("CST_HEALTH_ROOT", ""), SubprocessCommandRunner()))

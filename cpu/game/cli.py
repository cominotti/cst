# SPDX-License-Identifier: MPL-2.0
"""The command line of the game profile (cpu-game-profile on|off|tick|state).

Light profile: main is the composition root. on, off and tick run under the
run lock; state only reads (a closed pipe ends it quietly).
"""

from collections.abc import Sequence
import sys

from cst_core.adapters.dispatch import default_ctrl_c
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.runtime import restore_default_signals
from cpu.game.adapters.host import collate_as_bash
from cpu.game.adapters.profile import load_profile
from cpu.game.domain import USAGE, GamePaths


def main(argv: Sequence[str]) -> int:
    """Run one action (argv without the program name); 2 with a usage line for anything else."""
    action = argv[0] if argv else ""
    if action not in ("on", "off", "tick", "state"):
        print(USAGE, file=sys.stderr)
        return 2
    default_ctrl_c()
    collate_as_bash()
    if action == "state":
        restore_default_signals()
    paths = GamePaths()
    profile = load_profile(paths, SubprocessCommandRunner())
    return profile.state() if action == "state" else profile.locked(action)

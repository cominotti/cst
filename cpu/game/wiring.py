# SPDX-License-Identifier: MPL-2.0
"""The composition root of the game profile (cpu-game-profile on|off|tick|state): how the process starts
(Ctrl-C, the locale's collation, SIGPIPE for 'state'), its paths, the command runner and the profile over the
real adapters (cpu.game.adapters.profile.load_profile).

cpu/game/cli.py checks argv and hands the action here.
"""

from cst_core.adapters.dispatch import default_ctrl_c
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.runtime import restore_default_signals
from cpu.game.adapters.host import collate_as_bash
from cpu.game.adapters.profile import load_profile
from cpu.game.domain import GamePaths


def run_action(action: str) -> int:
    """Run one action (on, off, tick or state) on the machine: on, off and tick under the run lock, state with
    no lock (a closed pipe ends it quietly); its exit status."""
    default_ctrl_c()
    collate_as_bash()
    if action == "state":
        restore_default_signals()
    paths = GamePaths()
    profile = load_profile(paths, SubprocessCommandRunner())
    return profile.state() if action == "state" else profile.locked(action)

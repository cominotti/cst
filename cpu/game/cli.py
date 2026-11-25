# SPDX-License-Identifier: MPL-2.0
"""The command line of the game profile (cpu-game-profile on|off|tick|state).

Full profile: main checks argv and hands the action to cpu.game.wiring, the composition root.
"""

from collections.abc import Sequence
import sys

from cpu.game import wiring
from cpu.game.domain import USAGE


def main(argv: Sequence[str]) -> int:
    """Run one action (argv without the program name); 2 with a usage line for anything else."""
    action = argv[0] if argv else ""
    if action not in ("on", "off", "tick", "state"):
        print(USAGE, file=sys.stderr)
        return 2
    return wiring.run_action(action)

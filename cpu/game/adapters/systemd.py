# SPDX-License-Identifier: MPL-2.0
"""systemctl for the game profile's two managers, through the command runner (cpu.game.ports.GameSystemdPort)."""

from collections.abc import Sequence

from cst_core.domain.units import user_manager_head
from cst_core.ports import CommandResult, CommandRunnerPort, OutputMode
from cpu.game.domain import Manager


class GameSystemctl:
    """systemctl through the command runner: `systemctl ARGS`, or `user_manager_head(USER) ARGS`."""

    def __init__(self, runner: CommandRunnerPort) -> None:
        self._runner = runner

    def run(self, mgr: Manager, user: str, args: Sequence[str], *, output: OutputMode) -> CommandResult:
        """systemctl ARGS in that manager, its output as output says."""
        argv = [*user_manager_head(user), *args] if mgr == "usr" else ["systemctl", *args]
        return self._runner.run(argv, output=output)

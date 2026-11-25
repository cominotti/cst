# SPDX-License-Identifier: MPL-2.0
"""The game profile's front: GameProfile, cpu.game.application.profile.GameProfileSteps (on, off, tick and
state) over the real adapters (machine_ports), and load_profile, which reads its settings first.

The adapters are laid out by boundary: systemd (systemctl for both managers), files (the state files, the marks,
game.leftovers, the runtime unit files and the run lock), kernel (the cgroup tree and /proc), power (sysfs and
the DRM discovery) and output. The lookups and reads the game profile shares with cst cpu (the settings, the
accounts, a file's text) are in cpu.game.adapters.host.
"""

from collections.abc import Callable, Sequence
from typing import TextIO

from cst_core.domain.text import SettingsProblem
from cst_core.ports import CommandRunnerPort
from cpu.game.adapters.files import GameLeftoversFile, GameRunLock, GameStateFiles, GameUnitFiles
from cpu.game.adapters.host import read_settings, user_id, user_name
from cpu.game.adapters.kernel import GameKernelFiles
from cpu.game.adapters.output import GameStreamOutput
from cpu.game.adapters.power import GameSysfsFiles
from cpu.game.adapters.systemd import GameSystemctl
from cpu.game.application.profile import GameProfileSteps
from cpu.game.domain import RUN_LOCK_WAIT_S, GamePaths, GameSettings
from cpu.game.ports import GamePorts


def machine_ports(
    paths: GamePaths,
    runner: CommandRunnerPort,
    *,
    uid_of: Callable[[str], str] = user_id,
    name_of: Callable[[str], str] = user_name,
    out: TextIO | None = None,
) -> GamePorts:
    """The real adapters, over paths and runner."""
    return GamePorts(
        systemd=GameSystemctl(runner),
        state=GameStateFiles(),
        leftovers=GameLeftoversFile(paths.leftovers),
        units=GameUnitFiles(),
        kernel=GameKernelFiles(),
        power=GameSysfsFiles(),
        lock=GameRunLock(paths.run, paths.lock, timeout=RUN_LOCK_WAIT_S),
        out=GameStreamOutput(out),
        uid_of=uid_of,
        name_of=name_of,
    )


def load_profile(paths: GamePaths, runner: CommandRunnerPort) -> GameProfile:
    """The game profile over paths.conf's settings (and its problem lines)."""
    settings, problems = read_settings(paths.conf)
    return GameProfile(paths, settings, runner, problems=problems)


class GameProfile(GameProfileSteps):
    """on, off, tick and state of the game profile, over one runner and one set of paths (the real adapters)."""

    def __init__(
        self,
        paths: GamePaths,
        settings: GameSettings,
        runner: CommandRunnerPort,
        *,
        uid_of: Callable[[str], str] = user_id,
        name_of: Callable[[str], str] = user_name,
        problems: Sequence[SettingsProblem] = (),
        out: TextIO | None = None,
    ) -> None:
        ports = machine_ports(paths, runner, uid_of=uid_of, name_of=name_of, out=out)
        super().__init__(paths, settings, ports, problems=problems)

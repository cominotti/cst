"""Ports (interfaces) for the `toolchains` bounded context."""

from collections.abc import Sequence
from typing import Protocol


class SystemctlUserPort(Protocol):
    """Interface to `systemctl --user` and its manager environment."""

    def run(self, *args: str, check: bool = False, ok: tuple[int, ...] = (0,)) -> str:
        """Run `systemctl --user` with `args`, returning stripped stdout."""
        ...

    def manager_path(self) -> str | None:
        """Return the PATH of the user systemd manager, or None if unavailable."""
        ...


class ToolchainsRuntimePort(Protocol):
    """Execution port delegating commands to the toolchains host runtime."""

    def main(self, argv: Sequence[str]) -> int:
        """Run full CLI entrypoint dispatch and return exit status."""
        ...

    def status(self, home: str, now: float, brief: bool) -> int:
        """Execute `status` for `home` at timestamp `now`."""
        ...

    def prune(self, home: str, now: float, action: str, names: Sequence[str]) -> int:
        """Execute `prune plan|apply` for target names."""
        ...

    def auto_on(self, home: str, now: float, unless_off: bool = False) -> int:
        """Install and enable the user prune timer."""
        ...

    def auto_off(self, home: str, now: float) -> int:
        """Disable and remove the user prune timer and program copies."""
        ...

    def config(self, home: str, action: str, lean: bool | None = None, sccache: bool | None = None) -> int:
        """Inspect or apply Cargo, uv, and sccache settings."""
        ...

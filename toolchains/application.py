"""Application use cases for the `toolchains` bounded context."""

from collections.abc import Mapping, Sequence

from toolchains.domain import (
    FolderDecision,
    FolderSnapshot,
    ParsedCommand,
    RustBuildPolicy,
    decide,
    parse_command,
)
from toolchains.ports import ToolchainsRuntimePort


class ToolchainsService:
    """Coordinates toolchain pruning, status reporting, timer setup, and user config."""

    def __init__(self, runtime: ToolchainsRuntimePort) -> None:
        self._runtime = runtime

    def validate_command(self, argv: Sequence[str]) -> ParsedCommand:
        """Parse and validate CLI arguments into a `ParsedCommand`."""
        return parse_command(argv)

    def evaluate_rust_build_decisions(
        self,
        folders: Sequence[FolderSnapshot],
        policy: RustBuildPolicy,
        now: float,
        in_use: Mapping[str, Sequence[tuple[int | str, str]]],
        claimed: Mapping[str, Sequence[str]] | None = None,
    ) -> list[FolderDecision]:
        """Compute `FolderDecision` entries from immutable folder snapshots and policy."""
        return [
            FolderDecision(folder=folder, action=action, reason=reason)
            for folder, action, reason in decide(folders, policy.to_dict(), now, in_use, claimed)
        ]

    def status(self, home: str, now: float, brief: bool = False) -> int:
        """Run toolchain cache and configuration status report."""
        return self._runtime.status(home, now, brief)

    def prune(self, home: str, now: float, action: str, names: Sequence[str] = ()) -> int:
        """Plan or apply pruning for enabled or explicitly named targets."""
        return self._runtime.prune(home, now, action, names)

    def auto_on(self, home: str, now: float, unless_off: bool = False) -> int:
        """Install and enable the hourly user pruning timer."""
        return self._runtime.auto_on(home, now, unless_off)

    def auto_off(self, home: str, now: float) -> int:
        """Disable and uninstall the user pruning timer."""
        return self._runtime.auto_off(home, now)

    def config(self, home: str, action: str, lean: bool | None = None, sccache: bool | None = None) -> int:
        """Plan or apply Cargo, uv, and sccache settings."""
        return self._runtime.config(home, action, lean, sccache)

    def dispatch(self, argv: Sequence[str]) -> int:
        """Validate `argv` via domain parsing and dispatch through `runtime`."""
        self.validate_command(argv)
        return self._runtime.main(argv)

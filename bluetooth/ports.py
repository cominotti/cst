"""Ports (interfaces) for the `bluetooth` bounded context."""

from collections.abc import Sequence
from typing import Any, Protocol


class BluetoothHostPort(Protocol):
    """Read-only view of files, WirePlumber fragments, libwireplumber config, and stdin."""

    def read(self, path: str) -> str:
        """Read a UTF-8 file, raising FileNotFoundError/OSError on failure."""
        ...

    def read_or_none(self, path: str) -> str | None:
        """Read a UTF-8 file, returning None if it does not exist."""
        ...

    def other_fragments(self, ours: str, dirs: Sequence[str]) -> list[tuple[str, str]]:
        """List ('before'|'after', path) of other fragments defining monitor.bluez.properties."""
        ...

    def wp_effective_roles(self) -> tuple[bool, str | None]:
        """Resolve WirePlumber's effective bluez5.roles as (ok, value_or_error)."""
        ...

    def read_stdin(self) -> str:
        """Read all text from standard input."""
        ...


class LiveAudioPort(Protocol):
    """Interface for live PipeWire/PulseAudio inspection and profile switching."""

    dev: str

    def snap(self) -> dict[str, Any] | None:
        """Return the current PipeWire snapshot of the headset, or None."""
        ...

    def cards(self) -> list[dict[str, Any]] | None:
        """Return PulseAudio/PipeWire card descriptors, or None."""
        ...

    def set_profile(self, prof: str) -> bool:
        """Switch the headset card to `prof`, returning True on success."""
        ...

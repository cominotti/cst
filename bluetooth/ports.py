# SPDX-License-Identifier: MPL-2.0
"""Ports (interfaces) for the `bluetooth` bounded context."""

from collections.abc import Sequence
from typing import Protocol

from bluetooth.domain import PactlCard
from bluetooth.domain.watch import Snapshot


class BluetoothHostPort(Protocol):
    """Files, WirePlumber fragments, libwireplumber config, and stdin, read only; one file of its
    own written: the restarts still owed (restart_pending)."""

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

    def restart_pending(self, path: str, ids: tuple[str, str], skipped: Sequence[str], *, save: bool) -> str:
        """The units whose restart is still owed, one per line (domain.still_pending of the file at
        path, the current invocation IDs of RESTART_UNITS, "" for one that does not run, and the
        units just skipped); with save, the file is replaced atomically with them (removed when
        none is owed)."""
        ...


class LiveAudioPort(Protocol):
    """Interface for live PipeWire/PulseAudio inspection and profile switching."""

    dev: str

    def snap(self) -> Snapshot | None:
        """Return the current PipeWire snapshot of the headset, or None."""
        ...

    def cards(self) -> list[PactlCard] | None:
        """Return PulseAudio/PipeWire card descriptors, or None."""
        ...

    def set_profile(self, prof: str) -> bool:
        """Switch the headset card to `prof`, returning True on success."""
        ...

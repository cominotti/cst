# SPDX-License-Identifier: MPL-2.0
"""The boundaries of the game profile's protocol: systemd (both managers), its state files, game.leftovers,
systemd's runtime unit files, the cgroup tree and /proc, sysfs (EPP, cpuidle, the GPU), the run lock and
the output.

Each port is one thing on the machine, not one command: cpu.game.application runs on, off, tick and state
through them, and the adapters implement them over the files as the host sees them, systemctl through the
command runner, the journal's line format and the standard streams. Every mutating method is one call that
can fail on its own (a write, a removal, a set-property), so a test injects a fault at one of them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
import dataclasses
from typing import Protocol

from cst_core.ports import CommandResult, OutputMode
from cpu.game.domain import Manager


class GameSystemdPort(Protocol):
    """systemctl for the system manager (sys) or a user's (usr)."""

    def run(self, mgr: Manager, user: str, args: Sequence[str], *, output: OutputMode) -> CommandResult:
        """systemctl ARGS in that manager (user: the name for -M USER@, ignored for sys), its output as output
        says; a command that fails answers with its exit status."""
        ...


class GameStateFilesPort(Protocol):
    """game.state, game.intended and the profile's marks in the run folder, by path."""

    def exists(self, path: str) -> bool:
        """The file is there."""
        ...

    def read_text(self, path: str) -> str | None:
        """Its text, bad bytes replaced; None when it cannot be read."""
        ...

    def read_bytes(self, path: str) -> bytes | None:
        """Its bytes; None when it cannot be read."""
        ...

    def snapshot(self, path: str) -> str | None:
        """Its text in one look: None when it is not there, "" when it is there but cannot be read."""
        ...

    def create(self, path: str, text: str) -> None:
        """The file holds text, from its start (OSError when it cannot be written)."""
        ...

    def append(self, path: str, text: str) -> None:
        """text at the end of the file, a field's undecodable bytes as they were (OSError when it fails)."""
        ...

    def replace(self, path: str, text: str) -> None:
        """The file replaced whole by one holding text, mode 0644 (OSError when it fails)."""
        ...

    def truncate(self, path: str, size: int) -> None:
        """The file cut to its first size bytes (OSError when it fails)."""
        ...

    def remove(self, path: str) -> None:
        """The file removed (FileNotFoundError when it is not there, another OSError when it stays)."""
        ...


class GameLeftoversPort(Protocol):
    """game.leftovers: what 'off' could not undo, kept for the next 'off' and tick."""

    def read(self) -> str | None:
        """Its text byte for byte; None when it is not there, the OSError of a file there that cannot be read."""
        ...

    def write(self, text: str) -> None:
        """The file replaced whole by one holding text, mode 0644 (OSError when it fails)."""
        ...

    def remove(self) -> None:
        """The file removed (FileNotFoundError when it is not there, another OSError when it stays)."""
        ...


class GameUnitFilesPort(Protocol):
    """systemd's runtime unit files: the overrides set-property --runtime writes and the MemoryLow drop-ins."""

    def exists(self, path: str) -> bool:
        """The file is there."""
        ...

    def is_dir(self, path: str) -> bool:
        """The folder is there (a user manager's runtime folder)."""
        ...

    def dropins(self, folder: str, name: str) -> list[str]:
        """Every FOLDER/*.d/NAME there is, sorted."""
        ...

    def make_folder(self, path: str) -> None:
        """The folder made, with its parents; one there already is fine (OSError when it fails)."""
        ...

    def write_dropin(self, path: str, text: str) -> None:
        """The drop-in replaced whole by one holding text, mode as the umask gives (OSError when it fails)."""
        ...

    def remove(self, path: str) -> None:
        """The file removed (FileNotFoundError when it is not there, another OSError when it stays)."""
        ...

    def remove_folder(self, path: str) -> None:
        """The empty folder removed (OSError when it is not there, not empty or stays)."""
        ...


class GameSysValuesPort(Protocol):
    """The kernel's files that hold one value (the cgroup tree, /proc and sysfs), as the profile reads and
    writes them: what its kernel and power ports share."""

    def read_text(self, path: str) -> str | None:
        """The file's text; None when it cannot be read."""
        ...

    def read_stripped(self, path: str) -> str | None:
        """The file's text without its last newlines; None when it cannot be read."""
        ...

    def is_dir(self, path: str) -> bool:
        """The folder is there."""
        ...

    def writable(self, path: str) -> bool:
        """The file can be written."""
        ...

    def write(self, path: str, value: str) -> bool:
        """value and a newline written to the file; False when the write failed."""
        ...


class GameKernelPort(GameSysValuesPort, Protocol):
    """The cgroup tree and /proc as the profile reads and writes them, and the files its undo writes back."""

    def exists(self, path: str) -> bool:
        """The file is there."""
        ...

    def lexists(self, path: str) -> bool:
        """The path is there (a link counts, wherever it points)."""
        ...

    def holds(self, path: str, needle: str) -> bool:
        """The file holds needle; False when it cannot be read."""
        ...

    def process_maps(self, proc: str) -> list[str]:
        """Every PROC/PID/maps there is, sorted."""
        ...

    def boxes(self, folder: str, pattern: str) -> list[str]:
        """The folders FOLDER/PATTERN matches, in the order of the locale's collation."""
        ...

    def procs_texts(self, folder: str, depth: int) -> Iterator[str]:
        """The text of each cgroup.procs in the folder and below it (depth levels below at most), as it is
        read: a caller that stops early reads no more."""
        ...


class GamePowerPort(GameSysValuesPort, Protocol):
    """sysfs as the profile's power steps read and write it: EPP, cpuidle and the GPU's power files."""

    def list_dir(self, path: str) -> list[str]:
        """The names in the folder; none when it cannot be listed, whatever the error (gone meanwhile: a CPU taken
        offline)."""
        ...

    def drm_cards(self, sys_drm: str) -> tuple[dict[str, str], list[str]]:
        """Each DRM card's device (its real path) to its cardN name, and every cardN, by number; none when the
        folder cannot be listed (gone meanwhile)."""
        ...

    def pid_cards(self, proc: str, sys_drm: str, pid: str, dev_to_card: dict[str, str]) -> set[str]:
        """The DRM cards whose render or card node the process holds open (none when its fds cannot be listed)."""
        ...

    def connected_cards(self, sys_drm: str, cards: list[str]) -> list[str]:
        """The cards with a connected display, in the order of cards; a card whose folder cannot be listed (a
        GPU removed meanwhile) is not connected."""
        ...


class GameRunLockPort(Protocol):
    """The run lock that keeps on, off and tick apart (/run/cpu-setup/lock)."""

    def take(self) -> bool:
        """Held now; False when it stays busy (past its wait) or cannot be taken."""
        ...

    def release(self) -> None:
        """Given back."""
        ...


class GameOutputPort(Protocol):
    """What the profile says: journal lines and 'state''s lines on its stream, errors on stderr."""

    def log(self, prio: int, msg: str) -> None:
        """A journal line (<prio>game-profile: msg)."""
        ...

    def say(self, text: str) -> None:
        """One line."""
        ...

    def flush(self) -> None:
        """What was said is out (before a command writes on the same stream)."""
        ...

    def relay(self, res: CommandResult) -> None:
        """What a command printed, on our streams."""
        ...

    def error(self, text: str) -> None:
        """One line on stderr."""
        ...

    def stderr_text(self, text: str) -> None:
        """text on stderr as it is."""
        ...


@dataclasses.dataclass(frozen=True)
class GamePorts:
    """Every boundary of the protocol, and the account lookups (uid_of: a name's uid, "" for none; name_of:
    a uid's name, "" for none)."""

    systemd: GameSystemdPort
    state: GameStateFilesPort
    leftovers: GameLeftoversPort
    units: GameUnitFilesPort
    kernel: GameKernelPort
    power: GamePowerPort
    lock: GameRunLockPort
    out: GameOutputPort
    uid_of: Callable[[str], str]
    name_of: Callable[[str], str]

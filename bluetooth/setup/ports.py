# SPDX-License-Identifier: MPL-2.0
"""The boundaries `cst bluetooth`'s use cases (bluetooth/setup/application/) are typed
against, besides cst_core's CommandRunnerPort (every command it runs, on the
host through flatpak-spawn from a toolbox) and ClockPort (its waits), and
bluetooth.ports.BluetoothHostPort (bt-helper's reads, the record of restarts
still owed).
"""

from typing import Protocol

from cst_core.ports import FileSystemPort


class SetupConsolePort(Protocol):
    """The terminal: what the program prints, and the one question it asks."""

    def out(self, text: str) -> None:
        """Write text to stdout, at once (the commands it runs write there too)."""
        ...

    def err(self, text: str) -> None:
        """Write text to stderr, at once."""
        ...

    def out_closed(self) -> None:
        """stdout's reader went away: what is still written there goes nowhere."""
        ...

    def ask(self, prompt: str) -> str | None:
        """A line typed on the terminal after the prompt (shown on stderr); None when stdin is no terminal."""
        ...


class SetupFilesPort(FileSystemPort, Protocol):
    """The files the program judges and writes as the user: the WirePlumber files, the
    settings, the watcher's copies, the backups of main.conf (root writes go through sudo
    commands instead). cst_core's FileSystemPort, and what the shell's tools did besides."""

    def contains(self, path: str, text: str, *, quiet: bool) -> bool:
        """The file holds text (grep -qF; quiet: -s, no message when it cannot be read)."""
        ...

    def which(self, name: str) -> bool:
        """A command of that name is on PATH (command -v)."""
        ...

    def write_user_file(self, path: str, text: str, mode: int) -> None:
        """Replace the file at path atomically, as this user, no fsync (a link there is replaced, never followed)."""
        ...

    def write_through(self, path: str, text: str) -> None:
        """Replace what path points to atomically, keeping its mode (0644 for a new file)."""
        ...

    def copy(self, src: str, dest: str) -> None:
        """Copy src to dest with its mode and times (cp -p)."""
        ...

    def remove_empty_dir(self, path: str) -> None:
        """Remove the folder if it is empty; nothing otherwise."""
        ...

    def make_dirs(self, path: str) -> None:
        """The folder and its missing parents (mkdir -p)."""
        ...

    def backups(self, path: str) -> list[tuple[float, str]]:
        """(modification time, path) of each path.bak-* there is."""
        ...

    def realpath(self, path: str) -> str:
        """path with every link resolved (readlink -f)."""
        ...

# SPDX-License-Identifier: MPL-2.0
"""The game profile's files in /run: game.state, game.intended and the marks, game.leftovers, systemd's runtime
unit files (the overrides and the MemoryLow drop-ins) and the run lock (cpu.game.ports: GameStateFilesPort,
GameLeftoversPort, GameUnitFilesPort, GameRunLockPort). Their formats are cpu.game.domain's."""

import glob
import os

from cst_core.adapters.files import LocalFileSystemAdapter, ensure_dir, read_bytes, read_text, write_atomic
from cst_core.adapters.locks import take_lock
from cpu.game.adapters.host import read_snapshot


class GameStateFiles:
    """game.state, game.intended and the marks as files."""

    def exists(self, path: str) -> bool:
        """The file is there."""
        return os.path.exists(path)

    def read_text(self, path: str) -> str | None:
        """Its text, bad bytes replaced; None when it cannot be read."""
        return read_text(path)

    def read_bytes(self, path: str) -> bytes | None:
        """Its bytes; None when it cannot be read."""
        return read_bytes(path)

    def snapshot(self, path: str) -> str | None:
        """Its text in one look (read_snapshot)."""
        return read_snapshot(path)

    def create(self, path: str, text: str) -> None:
        """The file holds text, from its start."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def append(self, path: str, text: str) -> None:
        """text at the end of the file (a field's undecodable bytes written back as they were)."""
        with open(path, "a", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(text)

    def replace(self, path: str, text: str) -> None:
        """The file replaced whole, mode 0644 (in /run: no fsync, no relabel)."""
        write_atomic(path, text, mode=0o644, durable=False, relabel=False)

    def truncate(self, path: str, size: int) -> None:
        """The file cut to its first size bytes."""
        os.truncate(path, size)

    def remove(self, path: str) -> None:
        """The file removed."""
        os.unlink(path)


class GameLeftoversFile:
    """game.leftovers at path, read byte for byte (as it is written back)."""

    def __init__(self, path: str) -> None:
        self._path = path

    def read(self) -> str | None:
        """Its text; None when it is not there, the OSError of a file there that cannot be read."""
        return LocalFileSystemAdapter().read_existing_text(self._path)

    def write(self, text: str) -> None:
        """The file replaced whole, mode 0644 (in /run: no fsync, no relabel)."""
        write_atomic(self._path, text, mode=0o644, durable=False, relabel=False)

    def remove(self) -> None:
        """The file removed."""
        os.unlink(self._path)


class GameUnitFiles:
    """systemd's runtime unit files as files."""

    def exists(self, path: str) -> bool:
        """The file is there."""
        return os.path.exists(path)

    def is_dir(self, path: str) -> bool:
        """The folder is there."""
        return os.path.isdir(path)

    def dropins(self, folder: str, name: str) -> list[str]:
        """Every FOLDER/*.d/NAME there is, sorted."""
        found = glob.glob(os.path.join(glob.escape(folder), "*.d", name))
        return [f for f in sorted(found) if os.path.exists(f)]

    def make_folder(self, path: str) -> None:
        """The folder made, with its parents (one there already is fine)."""
        os.makedirs(path, exist_ok=True)

    def write_dropin(self, path: str, text: str) -> None:
        """The drop-in replaced whole, mode as the umask gives (in /run: no fsync, no relabel)."""
        write_atomic(path, text, mode="umask", durable=False, relabel=False)

    def remove(self, path: str) -> None:
        """The file removed."""
        os.unlink(path)

    def remove_folder(self, path: str) -> None:
        """The empty folder removed."""
        os.rmdir(path)


class GameRunLock:
    """The run lock: the run folder made (0755, also when it is there), the lock taken within its wait."""

    def __init__(self, run: str, lock: str, *, timeout: float) -> None:
        self._run, self._lock = run, lock
        self._timeout = timeout  # how long take() waits for a lock held elsewhere, in seconds
        self._fd: int | None = None

    def take(self) -> bool:
        """Held now; False when it stays busy or cannot be taken."""
        try:
            ensure_dir(self._run, 0o755)
            os.chmod(self._run, 0o755)  # install -d -m 0755 sets the mode of a folder already there too
            self._fd = take_lock(self._lock, wait=True, mode=0o644, timeout=self._timeout)
        except OSError:
            self._fd = None
        return self._fd is not None

    def release(self) -> None:
        """Given back."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

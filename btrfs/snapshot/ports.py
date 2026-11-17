# SPDX-License-Identifier: MPL-2.0
"""The boundaries of btrfs-snapshot-core's use cases (typing.Protocol): the
host (commands, clock, boot, directory entries, the cache walk), the btrfs
commands, the state directory, the pin file, the run lock and the journal.
adapters.py implements them; tests replace the host.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Collection
from collections.abc import Sequence
from typing import Literal, Protocol

from btrfs.snapshot.domain import CacheWalk, SubvolInfo, SubvolPath

# A journal line: (syslog priority, text).
type LogFn = Callable[[int, str], None]


class SnapshotHostPort(Protocol):
    """Everything a run reads or changes on the machine, but btrfs's own state files."""

    def run(self, argv: Sequence[str]) -> tuple[int, str, str]:
        """(rc, stdout, stderr) of argv in the C locale (127: not found, 124: timed out)."""
        ...

    def time(self) -> float: ...

    def boot(self) -> tuple[str | None, float | None]:
        """(boot id, seconds since boot including suspend), or (None, None) if unknown."""
        ...

    def listdir(self, path: str) -> list[str]:
        """The sorted entries of PATH; FileNotFoundError, OSError as os.listdir."""
        ...

    def kind(self, path: str) -> str:
        """'subvol' (a real subvolume root), 'symlink', 'missing' or 'other'."""
        ...

    def mkdir(self, path: str, mode: int) -> None: ...

    def walk_caches(self, root: str, skips: Sequence[str], mode: Literal["count", "drop"]) -> CacheWalk: ...


class BtrfsPort(Protocol):
    """The btrfs commands of a run (over the host's run)."""

    def show(self, path: SubvolPath) -> SubvolInfo:
        """`btrfs subvolume show`; ParseError when it fails or reads oddly."""
        ...

    def readonly(self, path: SubvolPath) -> bool:
        """The ro property; ParseError when it cannot be read."""
        ...

    def snapshot(self, source: SubvolPath, dest: str, *, readonly: bool = True) -> tuple[int, str, str]: ...

    def set_readonly(self, path: SubvolPath) -> tuple[int, str, str]: ...

    def delete(self, path: SubvolPath) -> tuple[int, str, str]: ...


class StateStorePort(Protocol):
    """The files of --state-dir, by name (STATE_FILE, UNFINISHED_FILE, WRITABLE_FILE)."""

    def read_list(self, name: str) -> list[str]:
        """The entries (one per line); [] if the file does not exist; OSError if it cannot be read."""
        ...

    def write_list(self, name: str, entries: Sequence[str], *, sync_dir: bool = True) -> None:
        """Replace the file with ENTRIES, durably; removed when ENTRIES is empty."""
        ...

    def read_record(self, name: str) -> str | None:
        """The file's text, stripped; None if it does not exist; OSError, UnicodeDecodeError."""
        ...


class PinsPort(Protocol):
    """The pin file: paths whose snapshots are never deleted."""

    def read(self) -> tuple[set[str], str | None]:
        """(the pinned paths, each normalised and resolved; why the file could not be read, or None)."""
        ...

    def covers(self, path: str, pins: Collection[str]) -> bool:
        """PATH (normalised, or resolved on the filesystem) is one of PINS."""
        ...


class RunLockPort(Protocol):
    """The lock one snapshot, prune or finish run holds."""

    def acquire(self, path: str) -> Callable[[], None] | None:
        """Take the lock at PATH without waiting: the function that releases it, or None when another run holds it."""
        ...

# SPDX-License-Identifier: MPL-2.0
"""The boundaries of `cst btrfs cache`'s use cases: /etc/fstab, the live mount table, systemd, the private
folders and the scratchpad's tag, the host's read-only facts and the accounts.

Each port is one thing on the machine, not one command: application.py runs status, plan, attach and
detach through them, and adapters.py implements them over the files and commands as the host sees them
(the test root of CST_BTRFS_CACHE_ROOT, /run/host from a toolbox). Paths go in as the user sees them
(/etc/fstab, /var/mnt/cache/USER, ~/.cache); the adapters map them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from btrfs.cache.domain import CacheMountBinding, CacheMountUser, LiveMount


@dataclass(frozen=True, slots=True)
class BindingPaths:
    """What the file system shows of one binding's two folders, read without following a link at the target."""

    target_is_link: bool
    target_exists: bool  # anything there, a dangling link too
    target_nonempty: bool  # a folder with an entry, or anything else that is not a folder; False for a link
    source_is_dir: bool  # a folder, through a link
    source_is_link: bool
    target_is_dir: bool  # asked only when the target exists and is not a link; False otherwise


class FstabPort(Protocol):
    """/etc/fstab, read and written byte-transparent (the lines CST does not manage keep their bytes)."""

    def read(self) -> str | None:
        """Its text; None when there is no file. OSError when a file is there and cannot be read (never
        taken for a missing one: a /etc/fstab rebuilt from "" would drop every line)."""
        ...

    def write(self, text: str) -> None:
        """The whole file replaced by text, atomically, mode 0644. OSError when it fails."""
        ...

    def remove(self) -> None:
        """No /etc/fstab (a file already gone is no error). OSError when it fails."""
        ...


class MountTablePort(Protocol):
    """The live mount table: findmnt, mount --bind and umount."""

    def read(self) -> tuple[tuple[LiveMount, ...], str | None]:
        """Every live mount; (), and why, when findmnt cannot answer (nothing is guessed from another table)."""
        ...

    def bind(self, binding: CacheMountBinding) -> str | None:
        """mount --bind of the binding's source at its target; None, or why it failed."""
        ...

    def unbind(self, binding: CacheMountBinding) -> str | None:
        """umount of the binding's target; None, or why it failed."""
        ...


class CacheSystemdPort(Protocol):
    """What systemd has loaded of /etc/fstab, and its reload."""

    def daemon_reload(self) -> str | None:
        """systemctl daemon-reload; None, or why it failed."""
        ...

    def mount_units(self) -> list[dict[str, str]] | None:
        """The mount units systemd has loaded, active or not (MOUNT_UNIT_PROPS of each); None when systemctl
        gives no answer."""
        ...


class CacheFoldersPort(Protocol):
    """The user's private folders and the scratchpad's CACHEDIR.TAG. Both calls raise OSError."""

    def ensure_private(self, path: str, user: CacheMountUser) -> None:
        """The folder at path (made when missing), then made 0700 and the user's through a descriptor opened
        without following a link: a link put there fails (ENOTDIR, as O_NOFOLLOW with O_DIRECTORY gives) instead
        of having its target changed."""
        ...

    def ensure_tag(self, source: str, user: CacheMountUser) -> None:
        """CACHEDIR.TAG in the source folder, the user's, unless something is already there."""
        ...


class CacheHostViewPort(Protocol):
    """The host's read-only facts that the probe reads besides /etc/fstab and the mounts."""

    def crypttab(self) -> str | None:
        """/etc/crypttab's text; None when it is not there."""
        ...

    def binding_paths(self, binding: CacheMountBinding) -> BindingPaths:
        """What the file system shows of the binding's source and target."""
        ...

    def home_exists(self, home: str) -> bool:
        """Whether the home is a folder."""
        ...


class CacheAccountsPort(Protocol):
    """The account a run is for."""

    def resolve(self, requested: str | None) -> tuple[CacheMountUser | None, str | None]:
        """The requested user (None: the caller's), its home as the kernel names it; or None and why not
        (no such user, a system user)."""
        ...

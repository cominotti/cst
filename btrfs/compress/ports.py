# SPDX-License-Identifier: MPL-2.0
"""Ports for `cst btrfs compress-force` use cases."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, Protocol

from btrfs.compress.domain import TopMountInfo
from cst_core.domain.errors import Refused
from cst_core.ports import CommandFailed, WriteFailed

# What update_top_mount can raise: installing the unit, marking it and reloading systemd.
TOP_MOUNT_ERRORS: Final = (OSError, Refused, CommandFailed, WriteFailed, ValueError)


class CompressHostPort(Protocol):
    """Host interactions needed by `btrfs.compress.application`."""

    def is_ostree(self) -> bool:
        """Return True on an rpm-ostree booted host."""
        ...

    def root_uuid(self) -> str:
        """Return the root btrfs filesystem UUID, or "" when unknown."""
        ...

    def read_fstab(self) -> str | None:
        """Read `/etc/fstab`, or return None when absent."""
        ...

    def read_proc_mounts(self) -> str | None:
        """Read `/proc/mounts`, or return None when unreadable."""
        ...

    def read_proc_cmdline(self) -> str | None:
        """Read `/proc/cmdline`, or return None when unreadable."""
        ...

    def read_kargs(self) -> tuple[bool, str, str]:
        """Return `(ok, stdout, err)` for `rpm-ostree kargs`."""
        ...

    def read_top_mount(self) -> TopMountInfo | None:
        """Read installed `var-mnt-btrfs\\x2dtop.mount`, or return None if not installed."""
        ...

    def write_fstab(self, text: str) -> None:
        """Atomically write `/etc/fstab` and reload systemd units."""
        ...

    def update_kargs(self, edits: Sequence[tuple[str, str]]) -> tuple[bool, str]:
        """Apply `(old_rootflags, new_rootflags)` via `rpm-ostree kargs`."""
        ...

    def remount(self, target: str, option: str) -> tuple[bool, str]:
        """Run `mount -o remount,<option> <target>`."""
        ...

    def update_top_mount(self, uuid: str, options: str) -> None:
        """Re-render and verify `btrfs:top-mount`, reload systemd, and update install record."""
        ...

    def confirm(self, prompt: str) -> bool:
        """Ask interactive confirmation; raises `Refused` when stdin is not a terminal."""
        ...

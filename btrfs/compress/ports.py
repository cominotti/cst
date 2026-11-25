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

    def read_settled_kargs(self) -> tuple[bool, str, str]:
        """`read_kargs` once rpm-ostree runs no transaction (a change that answered a failure may still be
        staging), waiting a bounded time; ok False when one still runs or rpm-ostree cannot say."""
        ...

    def read_top_mount(self) -> TopMountInfo | None:
        """Read installed `var-mnt-btrfs\\x2dtop.mount`, or return None if not installed."""
        ...

    def write_fstab(self, text: str, *, put_back: bool = True) -> None:
        """Atomically write `/etc/fstab` and reload systemd units; on a reload error the previous text is put
        back, unless put_back is False (an undo: the previous text is the one it undoes)."""
        ...

    def mount_units(self) -> list[dict[str, str]] | None:
        """The mount units systemd has loaded, active or not (`systemctl show --state=loaded -p
        MOUNT_UNIT_PROPS '*.mount'`); None when systemctl gives no answer."""
        ...

    def daemon_reload(self) -> str | None:
        """The catch-up `systemctl daemon-reload`, at most a minute: None, or why it failed (a reload that did
        not end in time is a failed one)."""
        ...

    def update_kargs(self, edits: Sequence[tuple[str, str]]) -> tuple[bool, str]:
        """Apply `(old_rootflags, new_rootflags)` via `rpm-ostree kargs`."""
        ...

    def remount(self, target: str, option: str) -> tuple[bool, str]:
        """Run `mount -o remount,<option> <target>`."""
        ...

    def update_top_mount(self, uuid: str, options: str, *, put_back: bool = True) -> None:
        """Re-render and verify `btrfs:top-mount`, reload systemd, and update install record; on an error the
        previous unit is put back, unless put_back is False (an undo, as write_fstab's)."""
        ...

    def confirm(self, prompt: str) -> bool:
        """Ask interactive confirmation; raises `Refused` when stdin is not a terminal."""
        ...

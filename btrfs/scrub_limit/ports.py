# SPDX-License-Identifier: MPL-2.0
"""The boundaries of `cst btrfs scrub-limit`'s use cases: snapshots.conf, the drop-in, systemd and sysfs.

Each port is one thing on the machine, not one command: application.py runs status, plan and set|off
(and their roll back) through them, and adapters.py implements them over the files as the host sees
them, the installer and systemctl.
"""

from __future__ import annotations

from typing import Protocol

from cst_core.ports import CommandResult


class ScrubSettingsPort(Protocol):
    """snapshots.conf, the file scrub-limit writes itself."""

    @property
    def path(self) -> str:
        """Its path, for messages."""
        ...

    def read(self) -> tuple[str | None, bool]:
        """(its text, None when it is not there; True when it is there and cannot be read)."""
        ...

    def folder_exists(self) -> bool:
        """Whether its folder is there."""
        ...

    def write(self, text: str) -> None:
        """Its folder made when it is missing, then the file replaced with text. OSError when it fails,
        which can come after the new text is in place (the folder's sync after the rename)."""
        ...

    def remove(self) -> None:
        """The file removed (one that is not there: nothing). OSError when it fails."""
        ...

    def remove_folder_if_empty(self) -> None:
        """Its folder removed when it is empty; never raises."""
        ...


class ScrubDropinPort(Protocol):
    """btrfs-scrub-weekly.service's drop-in, written only through the installer (btrfs:core keeps its
    state in the install record)."""

    @property
    def path(self) -> str:
        """Its path, for messages."""
        ...

    def read(self) -> tuple[str | None, bool]:
        """(its text, None when it is not there; True when it is there and cannot be read)."""
        ...

    def write(self, text: str) -> tuple[int, list[str], str]:
        """The drop-in written with text: (exit status, the installer's lines, its stderr)."""
        ...

    def remove(self) -> tuple[int, list[str], str]:
        """The drop-in removed: (exit status, the installer's lines, its stderr)."""
        ...

    def remove_folder_if_empty(self) -> None:
        """Its folder removed when it is empty; never raises."""
        ...


class ScrubSystemdPort(Protocol):
    """What systemd has loaded of btrfs-scrub-weekly.service."""

    def need_daemon_reload(self) -> bool | None:
        """True when systemd has not loaded the unit's files as they are on disk; None when systemctl
        cannot say."""
        ...

    def daemon_reload(self, *, timeout: float | None) -> CommandResult:
        """systemctl daemon-reload, at most timeout seconds (None: no limit)."""
        ...


class ScrubSysfsPort(Protocol):
    """The scrub speed limit each btrfs device has now."""

    def live_limits(self) -> tuple[str, ...]:
        """One line per device: the filesystem's UUID (its first 8 characters), `device`, its ID and the limit."""
        ...

# SPDX-License-Identifier: MPL-2.0
"""docker-volumes-nocow's ports: the volumes on disk, the helper's own state, the Docker daemons, its output."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from btrfs.docker_volumes_nocow.domain import TreeScan
from cst_core.ports import CommandResult


class VolumeTreePort(Protocol):
    """The volumes folder and the trees below it: NOCOW flags, renames, copies, labels.
    Errors are OSError (a data root that is not on btrfs: the flag ioctls fail)."""

    def is_dir(self, path: str) -> bool: ...

    def volumes(self) -> list[tuple[str, str]]:
        """(name, path of _data) for every volume of the local driver."""
        ...

    def scan(self, top: str, *, fix: bool = False) -> TreeScan:
        """Walk top (another filesystem is not entered). fix: also set +C in place where
        that works: on folders (what is created later) and on empty files."""
        ...

    def dirs_cow(self, top: str) -> bool:
        """top or a folder below it lacks +C (new files there: copy-on-write)."""
        ...

    def is_nocow(self, path: str) -> bool: ...

    def set_nocow(self, path: str, *, on: bool) -> None: ...

    def restore_interrupted(self) -> list[str]:
        """Undo what an interrupted migration left (kill, timeout, power loss); the
        volumes whose original was put back."""
        ...

    def make_twin(self, data: str, new: str) -> None:
        """new: an empty folder (a leftover removed first) with data's owner and mode."""
        ...

    def reference_label(self, data: str, new: str) -> None:
        """Give new the SELinux label of data; RuntimeError (new removed) when labels are kept and that fails."""
        ...

    def copy(self, data: str, new: str) -> CommandResult:
        """A real copy of data's contents into new (cp -a, no reflinks)."""
        ...

    def labels(self, top: str) -> Mapping[str, bytes | None]: ...

    def remove_tree(self, path: str, *, ignore_errors: bool = False) -> None: ...

    def rename(self, src: str, dst: str) -> None: ...

    def sync(self) -> None: ...

    def free_bytes(self) -> int:
        """btrfs's estimated free space where the volumes are (0: unknown)."""
        ...


class NocowStatePort(Protocol):
    """The helper's files: the migration flag, the attempts counter, the record of postponed boots."""

    def flag_exists(self) -> bool: ...

    def request_migration(self) -> None:
        """Write the flag durably (file and folders fsynced); OSError when that fails."""
        ...

    def remove_flag(self) -> None: ...

    def attempts(self) -> int:
        """Boots that tried the migration (0: none, or a counter that cannot be read)."""
        ...

    def save_attempts(self, count: int) -> None:
        """Durably, before the long rewrite starts."""
        ...

    def remove_attempts(self) -> None: ...

    def forget_attempts(self) -> None:
        """Remove the counter if there is one (a new request gets its two boots again)."""
        ...

    def boot_id(self) -> str:
        """The kernel's boot id ("" when unknown)."""
        ...

    def read_postponed(self) -> Mapping[str, object] | None: ...

    def write_postponed(self, record: Mapping[str, object]) -> None: ...

    def clear_postponed(self) -> None: ...


class DockerDaemonPort(Protocol):
    """Whether a Docker daemon may use the volumes now."""

    def service_busy(self) -> str | None:
        """None when systemd says docker.service is stopped, else why the migration waits."""
        ...

    def dockerd_busy(self) -> str | None:
        """None when no dockerd outside docker.service uses the volumes, else why the migration waits."""
        ...


class NocowOutputPort(Protocol):
    """What the helper says: status lines, journal lines, a desktop alert."""

    def say(self, line: str) -> None: ...

    def log(self, prio: int, msg: str) -> None:
        """One journal line, "<prio>docker-volumes-nocow: msg"."""
        ...

    def notify(self, title: str, body: str) -> None:
        """A desktop alert through btrfs-notify (kept until the next graphical login)."""
        ...

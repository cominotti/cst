# SPDX-License-Identifier: MPL-2.0
"""The ports vm/application.py is typed against: how a use case reaches the machine."""

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from cst_core.domain.files import UnitFileState
from cst_core.domain.values import Json
from vm.domain import TimerStep
from vm.domain.backup import Fingerprint, IndexEntry


class TimerFilesPort(Protocol):
    """The weekly NAS backup timer's files on this machine: what backup-timer on writes (the program
    copy and the two template units, through the installer, listed in the install record) and the
    links that say which VM's timer is on. systemd itself is cst_core's SystemdPort. Errors that stop
    a use case are Refused."""

    def file_states(self, unit: str) -> dict[str, UnitFileState]:
        """{path: state} of each file backup-timer on writes; the VM's own timer unit (which
        `systemctl mask` of that VM creates) is added only when it is masked."""
        ...

    def install(self, states: Mapping[str, UnitFileState], report: Callable[[TimerStep], None]) -> None:
        """Write each file that is not current, reporting each one as it is written."""
        ...

    def mark_complete(self) -> None:
        """The install record says the component is complete."""
        ...

    def remove(self, path: str) -> None: ...

    def forget(self, removed: Sequence[str]) -> None:
        """The install record no longer lists these files."""
        ...

    def timers_on(self) -> list[str]:
        """The VM timer units that are on (their timers.target.wants links)."""
        ...


class VmCommandsPort(Protocol):
    """virsh on this machine (libvirt's qemu:///system), as vm.adapters.VmCommands runs it: in the C
    locale, with vm's answers for a virsh that is missing (127) or does not answer (124)."""

    def virsh(self, args: Sequence[str], timeout: int, *, detach: bool | None) -> tuple[int, str, str]:
        """(exit status, stdout, stderr) of virsh ARGS."""
        ...

    def define(self, text: str) -> tuple[int, str, str]:
        """virsh define of a definition given as its text: (exit status, stdout, stderr)."""
        ...

    def package_versions(self) -> dict[str, str]:
        """{package: name-version-release.arch or "not installed"} of the virtualization packages."""
        ...

    def booted_deployment(self) -> dict[str, Json] | None:
        """{version, checksum, timestamp} of the booted rpm-ostree deployment, or None."""
        ...


class DefinitionStorePort(Protocol):
    """The copies of a VM definition kept before tune changes it (files; libvirt itself is
    VmCommandsPort)."""

    def save(self, vm: str, stamp: str, text: str) -> str:
        """Keep text (it holds --security-info secrets) as VM's definition before tune; its path."""
        ...


class ProgressPort(Protocol):
    """What a backup tells the person running it, as it goes."""

    def say(self, text: str, *, flush: bool = False) -> None:
        """A line on stdout (flushed at once with flush)."""
        ...

    def warn(self, text: str) -> None:
        """A line on stderr."""
        ...


class BackupStorePort(Protocol):
    """The files of the backups: what a backup reads (the VM's disks, UEFI variables and TPM
    folder), the folders it writes (BACKUP_DIR/VM/STAMP), and the index. A failure to write raises
    (OSError, or Refused for a copy that failed)."""

    def is_file(self, path: str) -> bool: ...

    def is_dir(self, path: str) -> bool: ...

    def exists(self, path: str) -> bool: ...

    def realpath(self, path: str) -> str: ...

    def has_backing_file(self, path: str) -> bool:
        """A qcow2 image with a backing file (an overlay: useless without its base)."""
        ...

    def sizes(self, path: str) -> tuple[int, int]:
        """(allocated, apparent) bytes of a file or a folder tree."""
        ...

    def fingerprint(self, path: str) -> Fingerprint:
        """What changes when something writes to path (OSError when it is gone)."""
        ...

    def free_space(self, path: str) -> tuple[str, int]:
        """(the nearest existing folder of path, its free bytes)."""
        ...

    def make_backup_folder(self, backup_dir: str, vm_dir: str, dest_dir: str) -> None:
        """backup_dir and vm_dir (0700, also when they exist), then dest_dir (new, 0700)."""
        ...

    def copy(self, src: str, dest: str, mode: str, *, big: bool) -> None:
        """cp -a src to dest: a disk (big) by mode (reflink or full), the small parts as plain copies.
        Refused when cp fails."""
        ...

    def write_private(self, path: str, text: str) -> None:
        """A new file, 0600 from the start (it may hold secrets)."""
        ...

    def remove_tree(self, path: str) -> None:
        """Remove path and what is in it, as far as it goes."""
        ...

    def read_index(self) -> list[IndexEntry]:
        """The backups in the index ([] when there is none); Refused for one of a newer cst vm."""
        ...

    def write_index(self, entries: Sequence[IndexEntry]) -> None:
        """Replace the index, durably."""
        ...

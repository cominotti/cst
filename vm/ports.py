# SPDX-License-Identifier: MPL-2.0
"""The ports vm/application.py is typed against: how a use case reaches the machine."""

from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from typing import Protocol

from cst_core.domain.files import UnitFileState
from vm.domain import NasRepo, TimerStep
from vm.domain.backup import BootedDeployment, Fingerprint, IndexEntry, ResticSummary

# One NAS backup per VM at a time: the lock of the VM named, held while the block runs (Refused
# when another backup holds it).
type LockPort = Callable[[str], AbstractContextManager[object]]


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
    """The commands cst vm runs on this machine, as vm.adapters.VmCommands runs them: virsh
    (libvirt's qemu:///system), rpm (the virtualization packages) and rpm-ostree (the booted
    deployment), in the C locale, with vm's answers for a command that is missing (127) or does not
    answer (124)."""

    def virsh(self, args: Sequence[str], timeout: int, *, detach: bool | None) -> tuple[int, str, str]:
        """(exit status, stdout, stderr) of virsh ARGS."""
        ...

    def define(self, text: str) -> tuple[int, str, str]:
        """virsh define of a definition given as its text: (exit status, stdout, stderr)."""
        ...

    def package_versions(self) -> dict[str, str]:
        """{package: name-version-release.arch or "not installed"} of the virtualization packages."""
        ...

    def detached(self) -> AbstractContextManager[None]:
        """While it lasts, every command runs in a session of its own (a Ctrl-C or a closed
        terminal cannot reach it); a call's own detach= wins."""
        ...

    def booted_deployment(self) -> BootedDeployment | None:
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

    def lexists(self, path: str) -> bool: ...

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

    def stage(self, vm: str, xml_text: str, manifest: str) -> str:
        """domain.xml and manifest.json of a NAS backup, 0600 in the 0700 staging folder of vm (for
        restic to read); the folder."""
        ...

    def unstage(self, folder: str) -> None:
        """The staged files removed, as far as it goes."""
        ...

    def remove_overlay(self, path: str) -> str | None:
        """Remove a merged overlay file (gone already: fine); why not, when it could not be."""
        ...

    def drop(self, path: str) -> None:
        """Remove a file this run made and libvirt never used, as far as it goes."""
        ...

    def describe(self, entry: IndexEntry) -> str:
        """One line for a backup of the index (list, status): a local copy whose folder is gone
        says so."""
        ...

    def read_index(self) -> list[IndexEntry]:
        """The backups in the index ([] when there is none); Refused for one of a newer cst vm."""
        ...

    def write_index(self, entries: Sequence[IndexEntry]) -> None:
        """Replace the index, durably."""
        ...


class SettingsPort(Protocol):
    """The settings files cst vm reads (/etc/cst/vm.conf, the /home NAS backup's snapshots.conf),
    read again at each call."""

    def number(self, key: str) -> int:
        """A whole number of vm.conf (its default when missing or not a number >= 0)."""
        ...

    def nas_repo(self) -> NasRepo:
        """The restic repository of the /home NAS backup; Refused when that backup is not set up."""
        ...

    def nas_set_up(self) -> bool:
        """snapshots.conf says the /home NAS backup is set up (NAS_ENABLED=1 and NAS_PATH)."""
        ...

    def knobs(self) -> dict[str, bool]:
        """{knob: on} of vm.conf (a missing file or knob is on)."""
        ...

    def cpu_disable(self) -> list[str]:
        """The CPU features vm-pin keeps disabled (cpu-setup.conf's VM_CPU_DISABLE, else its default)."""
        ...

    def default_vm(self) -> str:
        """The VM a command names when none is given (cpu-setup.conf's VM_NAME, else the default)."""
        ...


class ResticPort(Protocol):
    """restic against the repository its RESTIC_* variables (env) name."""

    def run(self, env: Mapping[str, str], args: Sequence[str], timeout: int) -> tuple[int, str, str]:
        """(exit status, stdout, stderr) of restic ARGS."""
        ...

    def backup(
        self, env: Mapping[str, str], args: Sequence[str], timeout: int, out: ProgressPort
    ) -> tuple[int, ResticSummary]:
        """restic ARGS (a backup --json): its progress to out as it runs; (exit status, its summary)."""
        ...


class SignalsPort(Protocol):
    """The stop signals of this process (SIGTERM, SIGHUP, SIGINT) during a NAS backup."""

    def catch_stops(self) -> None:
        """From now on a stop raises Stop (vm.domain.backup), so the overlays are merged back."""
        ...

    def held(self) -> AbstractContextManager[object]:
        """While it lasts, the stops wait (blocked): a stop never kills the snapshot call."""
        ...

    def ignored(self) -> AbstractContextManager[None]:
        """While it lasts, the stops are ignored (the merge back); the handlers before it after."""
        ...

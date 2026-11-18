# SPDX-License-Identifier: MPL-2.0
"""The ports vm/application.py is typed against: how a use case reaches the machine."""

from collections.abc import Callable, Mapping, Sequence
from typing import Protocol

from cst_core.domain.files import UnitFileState
from vm.domain import TimerStep


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


class DefinitionStorePort(Protocol):
    """VM definitions as files: the copy kept before tune changes a definition, and a definition
    given to libvirt from its text."""

    def save(self, vm: str, stamp: str, text: str) -> str:
        """Keep text (it holds --security-info secrets) as VM's definition before tune; its path."""
        ...

    def define(self, text: str) -> tuple[int, str, str]:
        """virsh define of text: (exit status, stdout, stderr)."""
        ...

# SPDX-License-Identifier: MPL-2.0
"""The boundaries of `cst btrfs iocost`'s use cases: io.cost.qos, the managed-devices file, sysfs, udev,
systemd, the installer, the in-process removal of btrfs:iocost and the two locks.

Each port is one thing on the machine, not one command: application.py runs status, plan, apply and
revert through them, and adapters.py implements them over the files as the host sees them, the
installer, InstallService, systemctl and udevadm.
"""

from __future__ import annotations

from typing import Protocol

from btrfs.iocost.domain import BlockDisk, UdevProperties
from cst_core.domain.record import ComponentState
from cst_core.ports import CommandResult


class QosControlPort(Protocol):
    """The kernel's io.cost.qos: one line per device."""

    @property
    def path(self) -> str:
        """Its path, for messages."""
        ...

    def read(self) -> str | None:
        """Its text; None when it is not there (no blk-iocost)."""
        ...

    def write(self, line: str) -> None:
        """One device's line, which the kernel merges by MAJ:MIN (that device's line is replaced, the
        others stay). OSError when the kernel refuses it."""
        ...


class ManagedDevicesPort(Protocol):
    """The devices CST enabled this boot (/run/btrfs-setup/iocost-managed): one MAJ:MIN per line."""

    @property
    def path(self) -> str:
        """Its path, for messages."""
        ...

    def read(self) -> frozenset[str]:
        """The MAJ:MIN lines it holds; empty when it is not there or cannot be read."""
        ...

    def save(self, devnos: frozenset[str]) -> None:
        """The file holding devnos (an empty set removes it; none there: nothing). OSError when it fails."""
        ...


class IocostSysfsPort(Protocol):
    """The kernel's other answers: the block disks, the cgroup controllers and io.cost.model."""

    def disks(self) -> tuple[BlockDisk, ...]:
        """Every whole block disk with a MAJ:MIN, by name (partitions left out)."""
        ...

    def io_controller(self) -> bool | None:
        """Whether the io controller is listed in cgroup.controllers; None when it cannot be read."""
        ...

    def model(self) -> str:
        """io.cost.model's text, stripped ("" when it cannot be read)."""
        ...


class UdevPort(Protocol):
    """udevd, the udev rule CST installs and what udev knows of a disk."""

    def running(self) -> bool:
        """Whether udevd runs (its control socket is there)."""
        ...

    def rule_installed(self) -> bool:
        """Whether CST's rule (91-cst-iocost.rules) is in place."""
        ...

    def properties(self, name: str) -> UdevProperties:
        """What `udevadm info` says of /dev/NAME, or why it could not say."""
        ...

    def reload_rules(self) -> CommandResult:
        """`udevadm control --reload-rules`: its exit status and output."""
        ...


class IocostSystemdPort(Protocol):
    """cst-btrfs-iocost.service in systemd."""

    def probe(self) -> tuple[bool, str, str]:
        """(the unit is loaded, its ActiveState, its UnitFileState); "unknown" when systemctl cannot say."""
        ...

    def daemon_reload(self) -> CommandResult:
        """`systemctl daemon-reload`."""
        ...

    def enable_now(self) -> CommandResult:
        """`systemctl enable --now` of the service (it starts the service, which takes the io.cost.qos lock)."""
        ...

    def disable_now(self) -> CommandResult:
        """`systemctl disable --now` of the service."""
        ...


class IocostInstallerPort(Protocol):
    """btrfs:iocost through the installer of a CST checkout."""

    def available(self) -> bool:
        """Whether a checkout's installer is there to install from (the installed zipapp has none)."""
        ...

    def install(self) -> tuple[int, list[str]]:
        """`install --component btrfs:iocost`: (exit status, its lines and its error line)."""
        ...

    def mark_done(self) -> tuple[int, list[str]]:
        """`mark --component btrfs:iocost done`: (exit status, its lines and its error line)."""
        ...

    def changes(self) -> tuple[str, ...] | None:
        """The files an install would write or record again; None when the installer cannot tell (no
        checkout, or its plan failed)."""
        ...


class IocostUninstallPort(Protocol):
    """btrfs:iocost removed in this process (from the installed zipapp too), and its state in root's
    install record. Both calls raise InstallRefused, WriteFailed or OSError when they fail."""

    @property
    def record_path(self) -> str:
        """The install record that lists btrfs:iocost's files."""
        ...

    def mark(self, state: ComponentState) -> ComponentState | None:
        """btrfs:iocost marked state; the state it had (None: none)."""
        ...

    def remove(self) -> list[str]:
        """Every file of btrfs:iocost and its line removed; the lines that say what went."""
        ...


class IocostLockPort(Protocol):
    """One lock file a run holds while it works."""

    @property
    def path(self) -> str:
        """Its path, for messages."""
        ...

    def take(self) -> bool:
        """Wait for the lock (its folder made first); False when another run still holds it after the
        time limit. OSError when the folder or the lock file cannot be made.

        A take while this run already holds that lock is a programming error: both implementations
        raise AssertionError (which _locked does not catch), never wait or answer False. The real
        adapter (IocostLockFile) checks its own lock object, the fake (FakeIocostLock) the lock's name
        in its world; wiring builds one IocostLockFile per path, so the two agree in production."""
        ...

    def release(self) -> None:
        """Let the lock go."""
        ...

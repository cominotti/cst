# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapters for `cst btrfs iocost` (`btrfs/btrfs-iocost`): the ports of ports.py over
io.cost.qos, the managed-devices file, sysfs, udevadm, systemctl, the installer, InstallService and the
lock files, and where they are (IocostPaths). wiring.py builds the use cases over them."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Final

from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import ensure_dir, read_text, write_atomic
from cst_core.adapters.locks import take_lock
from cst_core.adapters.runner import HostExecutionContext
from cst_core.adapters.runtime import (
    host_view_path,
    installer_script_for,
    probe_systemd_unit,
    run_installer,
)
from cst_core.application.install import InstallRequest, InstallService, record_file
from cst_core.domain.install import INSTALL_MANIFEST, Component, Owner, select_entries
from cst_core.domain.record import ComponentState
from cst_core.domain.text import first_line, group
from cst_core.ports import CommandResult, CommandRunnerPort, ExecutionContextPort, OutputMode
from btrfs.iocost.domain import (
    CGROUP_CONTROLLERS_PATH,
    IOCOST_COMPONENT_LOCK_NAME,
    IOCOST_LOCK,
    IOCOST_LOCK_TIMEOUT_S,
    IOCOST_MANAGED_FILE,
    IOCOST_MODEL_PATH,
    IOCOST_QOS_PATH,
    IOCOST_RULE_DEST,
    IOCOST_SERVICE,
    UDEV_CONTROL,
    UDEVADM_TIMEOUT_S,
    BlockDisk,
    UdevProperties,
)

_DEVNO_RE: Final = re.compile(r"^[0-9]+:[0-9]+$")
_ACTIVE_SCHED_RE: Final = re.compile(r"\[([^\]]+)\]")
_INSTALL_PLAN_CHANGES: Final = ("would write:", "would record:")  # the installer plan's lines for a change


@dataclass(frozen=True, slots=True)
class IocostPaths:
    """Resolved host sysfs, cgroup, unit, and runtime paths for `btrfs-iocost`."""

    qos_path: str = IOCOST_QOS_PATH
    model_path: str = IOCOST_MODEL_PATH
    controllers_path: str = CGROUP_CONTROLLERS_PATH
    sys_block: str = "/sys/block"
    managed_file: str = IOCOST_MANAGED_FILE
    rule_dest: str = IOCOST_RULE_DEST
    installer_script: str = ""
    udev_control: str = UDEV_CONTROL
    lock_path: str = IOCOST_LOCK
    lock_timeout: float = IOCOST_LOCK_TIMEOUT_S
    # revert removes btrfs:iocost in this process (it works from the installed zipapp, which has no
    # installer.py): the files under install_root ("" = the real root), their lines in root's install
    # record under the same root (record_path).
    install_root: str = ""
    entry_file: str = ""
    fallback_root: str = ""

    @property
    def record_path(self) -> str:
        """The install record that lists btrfs:iocost's files: root's, under install_root."""
        return record_file(Owner.ROOT, None, self.install_root)

    @property
    def component_lock_path(self) -> str:
        """The lock a full apply and a full revert hold from their first step to their last, so that one
        cannot mark btrfs:iocost done between the other's steps (lock_path, the io.cost.qos lock, is the
        boot service's and the udev rule's too: a full apply cannot hold it while enable --now starts the
        service)."""
        return os.path.join(os.path.dirname(self.lock_path), IOCOST_COMPONENT_LOCK_NAME)


def iocost_paths_for(
    entry_file: str, *, ctx: ExecutionContextPort | None = None, read_only: bool = True, fallback_root: str = ""
) -> IocostPaths:
    """Resolve `IocostPaths`, mapping host views through `/run/host` for read-only containers."""
    exec_ctx = ctx or HostExecutionContext()
    installer = installer_script_for(entry_file, fallback_root)
    if not read_only:
        return IocostPaths(installer_script=installer, entry_file=entry_file, fallback_root=fallback_root)
    raw = (
        IOCOST_QOS_PATH,
        IOCOST_MODEL_PATH,
        CGROUP_CONTROLLERS_PATH,
        "/sys/block",
        IOCOST_MANAGED_FILE,
        IOCOST_RULE_DEST,
    )
    return IocostPaths(
        *[host_view_path(exec_ctx, item) for item in raw],
        installer_script=installer,
        install_root=host_view_path(exec_ctx, "/").rstrip("/"),  # the host's files, for the installer's plan
    )


def _active_scheduler(raw: str | None) -> str:
    if raw is None:
        return "unknown"
    text = raw.strip()
    m = _ACTIVE_SCHED_RE.search(text)
    return group(m, 1) if m is not None else (text or "none")


def _scan_sys_block(sys_block: str) -> tuple[BlockDisk, ...]:
    if not os.path.isdir(sys_block):
        return ()
    disks: list[BlockDisk] = []
    for name in sorted(os.listdir(sys_block)):
        base = os.path.join(sys_block, name)
        if os.path.exists(os.path.join(base, "partition")):
            continue
        dev_raw = (read_text(os.path.join(base, "dev")) or "").strip()
        if _DEVNO_RE.fullmatch(dev_raw) is None:
            continue
        rot_raw = (read_text(os.path.join(base, "queue", "rotational")) or "").strip()
        sched = _active_scheduler(read_text(os.path.join(base, "queue", "scheduler")))
        model = (read_text(os.path.join(base, "device", "model")) or "").strip()
        disks.append(BlockDisk(name=name, devno=dev_raw, rotational=(rot_raw != "0"), scheduler=sched, model=model))
    return tuple(disks)


def udev_properties(runner: CommandRunnerPort, name: str) -> UdevProperties:
    """`udevadm info -q property -n /dev/NAME` on the host, in the C locale, at most UDEVADM_TIMEOUT_S: its
    KEY=VALUE lines, or why it could not say (its first stderr line stripped, or "exit N")."""
    res = runner.run(
        ["udevadm", "info", "-q", "property", "-n", f"/dev/{name}"],
        on_host=True,
        locale_c=True,
        timeout=UDEVADM_TIMEOUT_S,
        output=OutputMode.CAPTURE,
    )
    if res.fault is not None or res.returncode != 0:
        return UdevProperties(None, first_line(res.stderr) or f"exit {res.returncode}")
    return UdevProperties(
        {key: value for key, sep, value in (line.partition("=") for line in res.stdout.splitlines()) if sep}
    )


def _installer_step(runner: CommandRunnerPort, script: str, subargs: list[str]) -> tuple[int, list[str]]:
    rc, lines, err = run_installer(runner, script, subargs)
    return rc, [*lines, *([err] if err else [])]


@dataclass(frozen=True, slots=True)
class QosControlFile:
    """QosControlPort over io.cost.qos at path: one open and one write of the line per call."""

    path: str

    def read(self) -> str | None:
        """Its text; None when it is not there."""
        return read_text(self.path)

    def write(self, line: str) -> None:
        """line and a newline, in one write; the kernel's refusal raised."""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(line + "\n")


@dataclass(frozen=True, slots=True)
class ManagedDevicesFile:
    """ManagedDevicesPort over the file at path: 0644 in a folder 0755, replaced atomically (not durably:
    /run does not outlive a boot), never relabelled."""

    path: str

    def read(self) -> frozenset[str]:
        """The MAJ:MIN lines it holds."""
        text = read_text(self.path)
        if text is None:
            return frozenset()
        return frozenset(line.strip() for line in text.splitlines() if _DEVNO_RE.fullmatch(line.strip()))

    def save(self, devnos: frozenset[str]) -> None:
        """The file holding devnos, one per line in order; removed for an empty set."""
        ordered = sorted(devnos)
        if not ordered:
            if os.path.lexists(self.path):
                os.remove(self.path)
            return
        ensure_dir(os.path.dirname(self.path), 0o755)
        write_atomic(self.path, "".join(f"{d}\n" for d in ordered), mode=0o644, durable=False, relabel=False)


@dataclass(frozen=True, slots=True)
class IocostSysfs:
    """IocostSysfsPort over the block folder, cgroup.controllers and io.cost.model at these paths."""

    sys_block: str
    controllers_path: str
    model_path: str

    def disks(self) -> tuple[BlockDisk, ...]:
        """The scan of sys_block."""
        return _scan_sys_block(self.sys_block)

    def io_controller(self) -> bool | None:
        """ "io" among cgroup.controllers' words."""
        ctrl_raw = read_text(self.controllers_path)
        return ("io" in ctrl_raw.split()) if ctrl_raw is not None else None

    def model(self) -> str:
        """io.cost.model, stripped."""
        return (read_text(self.model_path) or "").strip()


@dataclass(frozen=True, slots=True)
class IocostUdev:
    """UdevPort through runner: udevadm, udevd's control socket at udev_control, the rule at rule_dest."""

    runner: CommandRunnerPort
    udev_control: str
    rule_dest: str

    def running(self) -> bool:
        """Whether udevd's control socket is there."""
        return os.path.exists(self.udev_control)

    def rule_installed(self) -> bool:
        """Whether the rule is there."""
        return os.path.exists(self.rule_dest)

    def properties(self, name: str) -> UdevProperties:
        """udev_properties of /dev/NAME."""
        return udev_properties(self.runner, name)

    def reload_rules(self) -> CommandResult:
        """`udevadm control --reload-rules`, here (no flatpak-spawn), its output captured."""
        return self.runner.run(["udevadm", "control", "--reload-rules"], output=OutputMode.CAPTURE)


@dataclass(frozen=True, slots=True)
class IocostSystemd:
    """IocostSystemdPort through SystemctlAdapter over runner: the unit's state asked on the host from a
    toolbox (status and plan), every change made here."""

    runner: CommandRunnerPort

    def probe(self) -> tuple[bool, str, str]:
        """probe_systemd_unit of the service."""
        return probe_systemd_unit(self.runner, IOCOST_SERVICE)

    def _systemctl(self) -> SystemctlAdapter:
        """systemctl through runner, here (no flatpak-spawn): every change of the unit."""
        return SystemctlAdapter(self.runner, on_host=SystemctlHost.LOCAL)

    def daemon_reload(self) -> CommandResult:
        """`systemctl daemon-reload`."""
        return self._systemctl().daemon_reload_result()

    def enable_now(self) -> CommandResult:
        """`systemctl enable --now cst-btrfs-iocost.service`."""
        return self._systemctl().enable(IOCOST_SERVICE, now=True)

    def disable_now(self) -> CommandResult:
        """`systemctl disable --now cst-btrfs-iocost.service`."""
        return self._systemctl().disable(IOCOST_SERVICE, now=True)


@dataclass(frozen=True, slots=True)
class IocostInstaller:
    """IocostInstallerPort: the installer at installer_script ("" for none: the installed zipapp) run by
    runner; its plan reads the files under install_root ("" for the real root)."""

    runner: CommandRunnerPort
    installer_script: str
    install_root: str

    def available(self) -> bool:
        """Whether a checkout's installer is known."""
        return bool(self.installer_script)

    def install(self) -> tuple[int, list[str]]:
        """installer.py install --component btrfs:iocost."""
        return _installer_step(
            self.runner, self.installer_script, ["install", "--component", Component.BTRFS_IOCOST.value]
        )

    def mark_done(self) -> tuple[int, list[str]]:
        """installer.py mark --component btrfs:iocost done."""
        mark = ["mark", "--component", Component.BTRFS_IOCOST.value, "done"]
        return _installer_step(self.runner, self.installer_script, mark)

    def changes(self) -> tuple[str, ...] | None:
        """The btrfs:iocost files `apply` would write or record again, by the installer's plan (under
        install_root: the host's files from a toolbox); None when it cannot tell (no checkout to install
        from, or the plan failed)."""
        if not self.installer_script:
            return None
        root = ["--root", self.install_root] if self.install_root else []
        rc, lines, _err = run_installer(
            self.runner, self.installer_script, ["plan", "--component", Component.BTRFS_IOCOST.value, *root]
        )
        if rc != 0:
            return None
        return tuple(
            line.partition(": ")[2].strip().removeprefix(self.install_root)
            for line in lines
            if line.startswith(_INSTALL_PLAN_CHANGES)
        )


@dataclass(frozen=True, slots=True)
class IocostUninstall:
    """IocostUninstallPort over svc: btrfs:iocost's files under install_root ("" for the real root), their
    lines and its state in root's install record under the same root (record_path)."""

    svc: InstallService
    install_root: str
    record_path: str

    def mark(self, state: ComponentState) -> ComponentState | None:
        """btrfs:iocost marked state in the record; the state it had."""
        return self.svc.mark(self.record_path, Component.BTRFS_IOCOST.value, state)

    def remove(self) -> list[str]:
        """The manifest's btrfs:iocost entries removed with their lines."""
        req = InstallRequest(root=self.install_root, as_root=True)
        places = [
            self.svc.placement(e, req) for e in select_entries(INSTALL_MANIFEST, component=Component.BTRFS_IOCOST)
        ]
        return list(self.svc.remove(places))


class IocostLockFile:
    """IocostLockPort over the lock file at path (0644, its folder 0755), waited for at most timeout
    seconds; release closes it (a killed run's lock goes with its process)."""

    __slots__ = ("_fd", "path", "timeout")

    def __init__(self, path: str, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self._fd: int | None = None

    def take(self) -> bool:
        """The lock, once its folder is made; False when another run holds it after timeout seconds."""
        ensure_dir(os.path.dirname(self.path), 0o755)
        fd = take_lock(self.path, wait=True, mode=0o644, timeout=self.timeout)
        if fd is None:
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        """The lock let go."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

# SPDX-License-Identifier: MPL-2.0
"""iocost's ports: one contract per port, run against its in-memory fake (btrfs/tests/fakes.py,
IocostWorld, which the fault tests and the iocost machine use) and against its real adapter
(btrfs/iocost/adapters.py, in a temporary folder, with a runner fake for systemctl, udevadm and the
installer that keeps what systemd and udev have loaded in the same SystemdState the fakes use), so the
fakes cannot drift from what the adapters do. Then the real adapters' own commands and files, pinned:
what no fake can show (the command lines and their options, the modes of what is written, the sysfs
scan's rules, the lock's time limit)."""

from __future__ import annotations

from collections.abc import Generator, Sequence
from contextlib import AbstractContextManager, contextmanager
import errno
import os
import shutil
import stat
import tempfile
import time
from typing import Protocol, override
import unittest

from btrfs.iocost.adapters import (
    IocostInstaller,
    IocostLockFile,
    IocostSysfs,
    IocostSystemd,
    IocostUdev,
    IocostUninstall,
    ManagedDevicesFile,
    QosControlFile,
)
from btrfs.iocost.domain import IOCOST_SERVICE, BlockDisk, UdevProperties
from btrfs.iocost.ports import (
    IocostInstallerPort,
    IocostLockPort,
    IocostSysfsPort,
    IocostSystemdPort,
    IocostUninstallPort,
    ManagedDevicesPort,
    QosControlPort,
    UdevPort,
)
from btrfs.tests.fakes import (
    IOCOST_ENTRIES,
    FakeIocostInstaller,
    FakeIocostLock,
    FakeIocostSysfs,
    FakeIocostSystemd,
    FakeIocostUninstall,
    FakeManagedDevices,
    FakeQosControl,
    FakeUdev,
    IocostWorld,
    SystemdState,
)
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget
from cst_core.adapters.locks import take_lock
from cst_core.adapters.runtime import lc_ctype_handover
from cst_core.application.install import InstallRefused, InstallRequest, InstallService, record_file, staged
from cst_core.domain.install import Component, Owner
from cst_core.domain.record import ComponentState
from cst_core.installer import NAME as INSTALLER_NAME
from cst_core.ports import CommandFault, CommandResult, OutputMode, WriteFailed
from tests.fault_injection import no_file_may_grow

LINE_ON = "259:1 enable=1 ctrl=auto"
LINE_OFF = "259:1 enable=0 ctrl=auto"
INSTALLER = "/opt/cst/cst_core/installer.py"
UNIT = "/etc/systemd/system/cst-btrfs-iocost.service"
UNINSTALL_ERRORS = (InstallRefused, WriteFailed, OSError)  # what revert reports as an ERROR line


def _access_denied() -> OSError:
    return OSError(errno.EACCES, "Access denied")


class Side(Protocol):
    """One implementation of the ports under one contract: the ports, and how a test sets their state."""

    root: str

    def qos(self) -> QosControlPort: ...
    def managed(self) -> ManagedDevicesPort: ...
    def sysfs(self) -> IocostSysfsPort: ...
    def udev(self) -> UdevPort: ...
    def systemd(self) -> IocostSystemdPort: ...
    def installer(self) -> IocostInstallerPort: ...
    def uninstall(self) -> IocostUninstallPort: ...
    def lock(self, name: str) -> IocostLockPort: ...
    def hold(self, *, qos: bool) -> None: ...
    def qos_present(self) -> None: ...
    def qos_refuses(self) -> None: ...
    def managed_folder(self) -> None: ...
    def managed_folder_cannot_be_made(self) -> None: ...
    def managed_disk_full(self) -> AbstractContextManager[None]: ...
    def put_disk(self, disk: BlockDisk) -> None: ...
    def put_controllers(self, text: str | None) -> None: ...
    def put_model(self, text: str) -> None: ...
    def udevd(self) -> None: ...
    def udev_answer(self, name: str, answer: UdevProperties) -> None: ...
    def udev_loaded(self) -> str | None: ...
    def fails(self, what: str) -> None: ...
    def show_fails(self) -> None: ...
    def loaded(self) -> SystemdState: ...
    def installer_gone(self) -> None: ...
    def installer_plan(self, changes: list[str] | None) -> None: ...
    def in_place(self) -> list[str]: ...
    def state(self) -> ComponentState | None: ...
    def removal_fails(self, dest: str) -> None: ...
    def record_unwritable(self) -> None: ...
    def lock_busy(self, name: str) -> None: ...
    def lock_cannot_be_made(self, name: str) -> None: ...


# ---- the fake side -------------------------------------------------------------------------------

_FAKE_POINTS = {
    "daemon-reload": "systemd daemon-reload",
    "enable": "systemd enable --now",
    "disable": "systemd disable --now",
    "udev reload": "udev reload-rules",
    "install": "installer install",
    "mark": "installer mark done",
}


class FakeSide:
    """The in-memory fakes over one IocostWorld."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.world = IocostWorld(install_root=root)
        self.world.qos = None
        self.world.udevd = False
        self.world.controllers = None
        self.world.held.update(qos=True, component=True)

    def qos(self) -> QosControlPort:
        return FakeQosControl(self.world)

    def managed(self) -> ManagedDevicesPort:
        return FakeManagedDevices(self.world)

    def sysfs(self) -> IocostSysfsPort:
        return FakeIocostSysfs(self.world)

    def udev(self) -> UdevPort:
        return FakeUdev(self.world)

    def systemd(self) -> IocostSystemdPort:
        return FakeIocostSystemd(self.world)

    def installer(self) -> IocostInstallerPort:
        return FakeIocostInstaller(self.world)

    def uninstall(self) -> IocostUninstallPort:
        return FakeIocostUninstall(self.world)

    def lock(self, name: str) -> IocostLockPort:
        self.world.held[name] = False
        return FakeIocostLock(self.world, name)

    def hold(self, *, qos: bool) -> None:
        self.world.held["qos"] = qos

    def qos_present(self) -> None:
        self.world.qos = {}

    def qos_refuses(self) -> None:
        self.world.qos_refusal = OSError(errno.EINVAL, os.strerror(errno.EINVAL))

    def managed_folder(self) -> None:
        self.world.managed_folder = True

    def managed_folder_cannot_be_made(self) -> None:
        self.world.fail("managed save", OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR)))

    @contextmanager
    def managed_disk_full(self) -> Generator[None]:
        self.world.fail("managed save", OSError(errno.EFBIG, os.strerror(errno.EFBIG)))
        try:
            yield
        finally:
            self.world.failing.clear()

    def put_disk(self, disk: BlockDisk) -> None:
        self.world.disks.append(disk)

    def put_controllers(self, text: str | None) -> None:
        self.world.controllers = text

    def put_model(self, text: str) -> None:
        self.world.model = text

    def udevd(self) -> None:
        self.world.udevd = True

    def udev_answer(self, name: str, answer: UdevProperties) -> None:
        self.world.udev_answers[name] = answer

    def udev_loaded(self) -> str | None:
        return self.world.udev_loaded

    def fails(self, what: str) -> None:
        err = _access_denied()
        if what in ("install", "mark"):
            err = OSError(errno.EACCES, f"{INSTALLER_NAME}: cannot write {self.world.record}: Access denied")
        self.world.fail(_FAKE_POINTS[what], err)

    def show_fails(self) -> None:
        self.world.show_fails = True

    def loaded(self) -> SystemdState:
        return self.world.systemd

    def installer_gone(self) -> None:
        self.world.installer_available = False

    def installer_plan(self, changes: list[str] | None) -> None:
        self.world.plan_answer = changes

    def in_place(self) -> list[str]:
        return self.world.in_place()

    def state(self) -> ComponentState | None:
        return self.world.state()

    def removal_fails(self, dest: str) -> None:
        self.world.target.fail_on = self.world.dest_path(dest)

    def record_unwritable(self) -> None:
        self.world.fail("uninstall mark pending", OSError(errno.ENOSPC, os.strerror(errno.ENOSPC)))

    def lock_busy(self, name: str) -> None:
        self.world.busy.add(name)

    def lock_cannot_be_made(self, name: str) -> None:
        self.world.lock_errors[name] = FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))


# ---- the real side -------------------------------------------------------------------------------


def _answer(cmd: list[str], rc: int = 0, out: str = "", err: str = "") -> CommandResult:
    return CommandResult(rc, out, err, argv=tuple(cmd))


class MachineRunner:
    """systemctl, udevadm and installer.py as the host would answer them, recorded with their options:
    systemd's view of the unit in a SystemdState (the unit is loaded while its file is under root, as
    IocostWorld has it), the rule udev loaded, and the installer's install and mark through a real
    InstallService over the files under root; fail_on: the commands (a part of the command line) that
    answer exit 1."""

    def __init__(self, root: str, unit: str, rule: str) -> None:
        self.root, self.unit, self.rule = root, unit, rule
        self.state = SystemdState()
        self.udev_loaded: str | None = None
        self.udev_answers: dict[str, UdevProperties] = {}
        self.fail_on: dict[str, str] = {}
        self.show_fails = False
        self.plan: list[str] | None = []
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.svc = InstallService(LocalInstallTarget(), lambda e: f"{e.dest} 0\n".encode(), record=JsonInstallRecord())

    def _text(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        cmd = list(argv)
        self.calls.append((cmd, kw))
        line = " ".join(cmd)
        err = next((e for part, e in self.fail_on.items() if part in line), None)
        if err is not None:
            return _answer(cmd, 1, "", err)
        if len(cmd) > 4 and cmd[3].endswith("installer.py"):
            return self._installer(cmd)
        handler = {
            ("udevadm", "info"): self._udev_info,
            ("udevadm", "control"): self._udev_reload,
            ("systemctl", "show"): self._show,
            ("systemctl", "daemon-reload"): self._daemon_reload,
            ("systemctl", "enable"): self._enable,
            ("systemctl", "disable"): self._disable,
        }.get((cmd[0], cmd[1]) if len(cmd) > 1 else ("", ""))
        if handler is None:
            raise AssertionError(f"command not modelled: {cmd}")
        return handler(cmd)

    def _udev_info(self, cmd: list[str]) -> CommandResult:
        answer = self.udev_answers.get(cmd[-1].removeprefix("/dev/"), UdevProperties({"USEC_INITIALIZED": "1234"}))
        if answer.props is None:
            return _answer(cmd, 4, "", f"\n  {answer.error} \t\nsecond line\n")
        return _answer(cmd, 0, "".join(f"{k}={v}\n" for k, v in answer.props.items()) + "a line without a sign\n")

    def _udev_reload(self, cmd: list[str]) -> CommandResult:
        if cmd != ["udevadm", "control", "--reload-rules"]:
            raise AssertionError(f"command not modelled: {cmd}")
        self.udev_loaded = self._text(self.rule)
        return _answer(cmd)

    def _show(self, cmd: list[str]) -> CommandResult:
        if self.show_fails:
            return _answer(cmd, 1, "", "Failed to connect to bus")
        load = "loaded" if os.path.exists(self.unit) else "not-found"
        s = self.state
        act, en = ("active" if s.active else "inactive"), ("enabled" if s.enabled else "disabled")
        return _answer(cmd, 0, f"LoadState={load}\nActiveState={act}\nUnitFileState={en}\n")

    def _daemon_reload(self, cmd: list[str]) -> CommandResult:
        self.state.reloaded(self._text(self.unit))
        return _answer(cmd)

    def _enable(self, cmd: list[str]) -> CommandResult:
        if cmd != ["systemctl", "enable", "--now", IOCOST_SERVICE]:
            raise AssertionError(f"command not modelled: {cmd}")
        if not os.path.exists(self.unit):
            return _answer(cmd, 1, "", f"Failed to enable unit: Unit file {IOCOST_SERVICE} does not exist\n")
        self.state.enabled = self.state.active = True
        return _answer(cmd)

    def _disable(self, cmd: list[str]) -> CommandResult:
        if cmd != ["systemctl", "disable", "--now", IOCOST_SERVICE]:
            raise AssertionError(f"command not modelled: {cmd}")
        self.state.enabled = self.state.active = False
        return _answer(cmd)

    def _installer(self, cmd: list[str]) -> CommandResult:
        req = InstallRequest(root=self.root, as_root=False)
        record = record_file(Owner.ROOT, None, self.root)
        match cmd[4:]:
            case ["install", "--component", "btrfs:iocost"]:
                lines = list(self.svc.install(IOCOST_ENTRIES, req))
                if any(not x.startswith("unchanged:") for x in lines):
                    self.state.changed()
                return _answer(cmd, 0, "".join(f"{x}\n" for x in lines))
            case ["mark", "--component", "btrfs:iocost", "done"]:
                self.svc.mark(record, Component.BTRFS_IOCOST.value)
                return _answer(cmd)
            case ["plan", "--component", "btrfs:iocost", "--root", root] if root == self.root:
                if self.plan is None:
                    return _answer(cmd, 1, "", f"{INSTALLER_NAME}: cannot build the zipapp")
                return _answer(
                    cmd,
                    0,
                    f"unchanged: {root}/usr/local/sbin/btrfs-iocost\n"
                    + "".join(f"would write: {root}{p}\n" for p in self.plan),
                )
            case _:
                raise AssertionError(f"installer command not modelled: {cmd}")


class FailingRemoveTarget(LocalInstallTarget):
    """This machine's files, except that the removal of one path fails, as MemoryTarget's fail_on does."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_on: str | None = None

    @override
    def remove(self, path: str) -> bool:
        if path == self.fail_on:
            raise WriteFailed(path, "Permission denied")
        return super().remove(path)


class RealSide:
    """The real adapters in a temporary folder (root: the install root, the host's files under it)."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.qos_path = os.path.join(root, "sys", "fs", "cgroup", "io.cost.qos")
        self.controllers = os.path.join(root, "sys", "fs", "cgroup", "cgroup.controllers")
        self.model_path = os.path.join(root, "sys", "fs", "cgroup", "io.cost.model")
        self.sys_block = os.path.join(root, "sys", "block")
        self.managed_path = os.path.join(root, "run", "btrfs-setup", "iocost-managed")
        self.control = os.path.join(root, "run", "udev", "control")
        self.rule = staged(root, "/etc/udev/rules.d/91-cst-iocost.rules")
        self.unit = staged(root, UNIT)
        self.locks = {
            "qos": os.path.join(root, "run", "btrfs-setup", "iocost-managed.lock"),
            "component": os.path.join(root, "run", "btrfs-setup", "iocost-component.lock"),
        }
        self.runner = MachineRunner(root, self.unit, self.rule)
        self.script = INSTALLER
        self.target = FailingRemoveTarget()
        self.held: list[int] = []

    def close(self) -> None:
        for fd in self.held:
            os.close(fd)

    def qos(self) -> QosControlPort:
        return QosControlFile(self.qos_path)

    def managed(self) -> ManagedDevicesPort:
        return ManagedDevicesFile(self.managed_path)

    def sysfs(self) -> IocostSysfsPort:
        return IocostSysfs(self.sys_block, self.controllers, self.model_path)

    def udev(self) -> UdevPort:
        return IocostUdev(self.runner, self.control, self.rule)

    def systemd(self) -> IocostSystemdPort:
        return IocostSystemd(self.runner)

    def installer(self) -> IocostInstallerPort:
        return IocostInstaller(self.runner, self.script, self.root)

    def uninstall(self) -> IocostUninstallPort:
        svc = InstallService(self.target, lambda _e: b"", record=JsonInstallRecord())
        return IocostUninstall(svc, self.root, record_file(Owner.ROOT, None, self.root))

    def lock(self, name: str) -> IocostLockPort:
        return IocostLockFile(self.locks[name], 0.05)

    def hold(self, *, qos: bool) -> None:
        pass

    def _write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def qos_present(self) -> None:
        self._write(self.qos_path, "")

    def qos_refuses(self) -> None:
        os.remove(self.qos_path)
        os.mkdir(self.qos_path)  # a folder: its open for writing fails (EISDIR), also as root

    def managed_folder(self) -> None:
        os.makedirs(os.path.join(self.managed_path, "x"))

    def managed_folder_cannot_be_made(self) -> None:
        self._write(os.path.dirname(self.managed_path), "a file where the folder belongs\n")

    def managed_disk_full(self) -> AbstractContextManager[None]:
        return no_file_may_grow()

    def put_disk(self, disk: BlockDisk) -> None:
        base = os.path.join(self.sys_block, disk.name)
        self._write(os.path.join(base, "dev"), f"{disk.devno}\n")
        self._write(os.path.join(base, "queue", "rotational"), "1\n" if disk.rotational else "0\n")
        self._write(os.path.join(base, "queue", "scheduler"), f"mq-deadline [{disk.scheduler}] kyber\n")
        if disk.model:
            self._write(os.path.join(base, "device", "model"), f"  {disk.model} \n")

    def put_controllers(self, text: str | None) -> None:
        if text is not None:
            self._write(self.controllers, f"{text}\n")

    def put_model(self, text: str) -> None:
        self._write(self.model_path, text)

    def udevd(self) -> None:
        self._write(self.control, "")

    def udev_answer(self, name: str, answer: UdevProperties) -> None:
        self.runner.udev_answers[name] = answer

    def udev_loaded(self) -> str | None:
        return self.runner.udev_loaded

    def fails(self, what: str) -> None:
        if what in ("install", "mark"):
            record = record_file(Owner.ROOT, None, self.root)
            self.runner.fail_on[f"installer.py {what}"] = f"{INSTALLER_NAME}: cannot write {record}: Access denied\n"
            return
        part = {
            "daemon-reload": "systemctl daemon-reload",
            "enable": "systemctl enable",
            "disable": "systemctl disable",
            "udev reload": "udevadm control",
        }[what]
        self.runner.fail_on[part] = "Access denied\n"

    def show_fails(self) -> None:
        self.runner.show_fails = True

    def loaded(self) -> SystemdState:
        return self.runner.state

    def installer_gone(self) -> None:
        self.script = ""

    def installer_plan(self, changes: list[str] | None) -> None:
        self.runner.plan = changes

    def in_place(self) -> list[str]:
        return [e.dest for e in IOCOST_ENTRIES if os.path.lexists(staged(self.root, e.dest))]

    def state(self) -> ComponentState | None:
        rec = JsonInstallRecord().read(record_file(Owner.ROOT, None, self.root))
        return None if rec is None else rec.components.get(Component.BTRFS_IOCOST.value)

    def removal_fails(self, dest: str) -> None:
        self.target.fail_on = staged(self.root, dest)

    def record_unwritable(self) -> None:
        self._write(record_file(Owner.ROOT, None, self.root), '{"format": 9, "files": {}}')

    def lock_busy(self, name: str) -> None:
        os.makedirs(os.path.dirname(self.locks[name]), exist_ok=True)
        fd = take_lock(self.locks[name], wait=False, mode=0o644)
        assert fd is not None
        self.held.append(fd)

    def lock_cannot_be_made(self, name: str) -> None:
        self._write(os.path.dirname(self.locks[name]), "a file where the folder belongs\n")


# ---- the contract --------------------------------------------------------------------------------


class _PortsContract(unittest.TestCase):
    """The contract of each port; FakePortsContractTest and RealPortsContractTest give the side."""

    def make_side(self, root: str) -> Side:
        raise NotImplementedError

    @override
    def setUp(self) -> None:
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cst-iocost-ports-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.side = self.make_side(self.root)

    # io.cost.qos
    def test_io_cost_qos_reads_and_takes_one_device_s_line_per_write(self) -> None:
        q = self.side.qos()
        self.assertIsNone(q.read())  # no blk-iocost
        self.side.qos_present()
        self.assertEqual(q.read(), "")
        q.write(LINE_ON)
        self.assertEqual(q.read(), f"{LINE_ON}\n")
        q.write(LINE_OFF)  # the same device: its line is replaced
        self.assertEqual(q.read(), f"{LINE_OFF}\n")
        self.assertIsNot(q.read(), q.read())

    def test_a_write_the_kernel_refuses_raises(self) -> None:
        self.side.qos_present()
        self.side.qos_refuses()
        with self.assertRaises(OSError):
            self.side.qos().write(LINE_ON)

    # the managed-devices file
    def test_the_managed_devices_are_saved_read_and_removed(self) -> None:
        m = self.side.managed()
        self.assertEqual(m.read(), frozenset())
        m.save(frozenset())  # none there, none to save: nothing
        m.save(frozenset({"259:1", "8:0"}))
        self.assertEqual(m.read(), frozenset({"259:1", "8:0"}))
        self.assertIsNot(m.read(), m.read())
        m.save(frozenset({"259:1"}))
        self.assertEqual(m.read(), frozenset({"259:1"}))
        m.save(frozenset())
        self.assertEqual(m.read(), frozenset())

    def test_a_folder_where_the_managed_file_belongs_reads_nothing_and_cannot_be_saved(self) -> None:
        self.side.managed_folder()
        m = self.side.managed()
        self.assertEqual(m.read(), frozenset())
        for devnos in (frozenset({"259:1"}), frozenset()):
            with self.subTest(devnos=devnos), self.assertRaises(OSError) as raised:
                m.save(devnos)
            self.assertEqual(raised.exception.errno, errno.EISDIR)

    def test_a_save_on_a_full_disk_raises_and_keeps_the_devices_saved_before(self) -> None:
        m = self.side.managed()
        m.save(frozenset({"259:1"}))
        with self.side.managed_disk_full(), self.assertRaises(OSError):
            m.save(frozenset({"259:1", "8:0"}))
        self.assertEqual(m.read(), frozenset({"259:1"}))

    def test_a_save_that_cannot_make_its_folder_raises(self) -> None:
        self.side.managed_folder_cannot_be_made()
        with self.assertRaises(OSError):
            self.side.managed().save(frozenset({"259:1"}))
        self.assertEqual(self.side.managed().read(), frozenset())

    # sysfs
    def test_sysfs_lists_the_disks_by_name_with_their_fields(self) -> None:
        self.assertEqual(self.side.sysfs().disks(), ())
        disks = (
            BlockDisk("loop0", "7:0", rotational=False, scheduler="none"),
            BlockDisk("nvme0n1", "259:1", rotational=False, scheduler="none", model="Samsung SSD"),
            BlockDisk("sda", "8:0", rotational=True, scheduler="bfq", model="HDD"),
        )
        for d in disks:
            self.side.put_disk(d)
        self.assertEqual(self.side.sysfs().disks(), disks)

    def test_sysfs_reads_the_io_controller_and_the_model(self) -> None:
        s = self.side.sysfs()
        self.assertIsNone(s.io_controller())
        self.assertEqual(s.model(), "")
        self.side.put_controllers("cpuset cpu memory")
        self.assertFalse(s.io_controller())
        self.side.put_controllers("cpuset cpu io memory pids")
        self.assertTrue(s.io_controller())
        self.side.put_model(" 259:1 ctrl=auto model=linear \n")
        self.assertEqual(s.model(), "259:1 ctrl=auto model=linear")

    # udev
    def test_udev_answers_what_it_knows_of_a_disk_or_why_it_cannot(self) -> None:
        u = self.side.udev()
        self.assertEqual(u.properties("nvme0n1"), UdevProperties({"USEC_INITIALIZED": "1234"}))
        answer = UdevProperties({"IOCOST_SOLUTIONS": " isolation ", "USEC_INITIALIZED": "1"})
        self.side.udev_answer("nvme0n1", answer)
        self.assertEqual(u.properties("nvme0n1"), answer)
        self.assertIsNot(u.properties("nvme0n1").props, u.properties("nvme0n1").props)
        self.side.udev_answer("sda", UdevProperties(None, "Unknown device"))
        self.assertEqual(u.properties("sda"), UdevProperties(None, "Unknown device"))

    def test_udev_runs_and_reloads_the_rule(self) -> None:
        u = self.side.udev()
        self.assertFalse(u.running())
        self.assertFalse(u.rule_installed())
        self.side.udevd()
        self.assertTrue(u.running())
        self.side.installer().install()
        self.assertTrue(u.rule_installed())
        self.assertEqual(u.reload_rules().returncode, 0)
        self.assertEqual(self.side.udev_loaded(), "/etc/udev/rules.d/91-cst-iocost.rules 0\n")

    def test_a_failed_udev_reload_says_why_and_loads_nothing(self) -> None:
        self.side.fails("udev reload")
        r = self.side.udev().reload_rules()
        self.assertEqual(
            (r.returncode, r.failure("udevadm control --reload-rules")),
            (1, "udevadm control --reload-rules failed: Access denied"),
        )
        self.assertIsNone(self.side.udev_loaded())

    # systemd
    def test_systemd_loads_enables_and_disables_the_service(self) -> None:
        self.side.hold(qos=False)
        s = self.side.systemd()
        self.assertEqual(s.probe(), (False, "inactive", "disabled"))
        r = s.enable_now()
        self.assertEqual(
            (r.returncode, r.stderr), (1, f"Failed to enable unit: Unit file {IOCOST_SERVICE} does not exist\n")
        )
        self.side.installer().install()
        self.assertTrue(self.side.loaded().dirty)
        self.assertEqual(s.daemon_reload().returncode, 0)
        self.assertEqual((self.side.loaded().loaded, self.side.loaded().dirty), (f"{UNIT} 0\n", False))
        self.assertEqual(s.probe(), (True, "inactive", "disabled"))
        self.assertEqual(s.enable_now().returncode, 0)
        self.assertEqual(s.probe(), (True, "active", "enabled"))
        self.assertEqual(s.disable_now().returncode, 0)
        self.assertEqual(s.probe(), (True, "inactive", "disabled"))

    def test_a_failed_systemctl_says_why_and_changes_nothing(self) -> None:
        self.side.hold(qos=False)
        self.side.installer().install()
        s = self.side.systemd()
        for what, call, text in (
            ("daemon-reload", s.daemon_reload, "systemctl daemon-reload"),
            ("enable", s.enable_now, f"systemctl enable --now {IOCOST_SERVICE}"),
            ("disable", s.disable_now, f"systemctl disable --now {IOCOST_SERVICE}"),
        ):
            with self.subTest(what):
                self.side.fails(what)
                r = call()
                self.assertEqual((r.returncode, r.failure(text)), (1, f"{text} failed: Access denied"))
        self.assertEqual(s.probe(), (True, "inactive", "disabled"))
        self.assertTrue(self.side.loaded().dirty)

    def test_systemctl_that_cannot_answer_says_unknown(self) -> None:
        self.side.show_fails()
        self.assertEqual(self.side.systemd().probe(), (False, "unknown", "unknown"))

    # the installer
    def test_the_installer_installs_and_marks_btrfs_iocost(self) -> None:
        i = self.side.installer()
        self.assertTrue(i.available())
        rc, lines = i.install()
        self.assertEqual(rc, 0, lines)
        self.assertEqual(len(lines), 3, lines)
        self.assertEqual(self.side.in_place(), [e.dest for e in IOCOST_ENTRIES])
        self.assertEqual(self.side.state(), "pending")
        self.assertEqual(i.mark_done(), (0, []))
        self.assertEqual(self.side.state(), "done")
        rc, lines = i.install()
        self.assertEqual((rc, [x.split()[0] for x in lines]), (0, ["unchanged:"] * 3))

    def test_a_failed_installer_answers_exit_1_and_its_error_line(self) -> None:
        record = record_file(Owner.ROOT, None, self.root)
        i = self.side.installer()
        for what, call in (("install", i.install), ("mark", i.mark_done)):
            with self.subTest(what):
                self.side.fails(what)
                self.assertEqual(call(), (1, [f"{INSTALLER_NAME}: cannot write {record}: Access denied"]))
        self.assertEqual((self.side.in_place(), self.side.state()), ([], None))

    def test_the_installer_s_plan_names_the_files_an_install_would_change(self) -> None:
        self.assertEqual(self.side.installer().changes(), ())
        self.side.installer_plan([UNIT])
        self.assertEqual(self.side.installer().changes(), (UNIT,))
        self.side.installer_plan(None)
        self.assertIsNone(self.side.installer().changes())
        self.side.installer_gone()
        self.assertFalse(self.side.installer().available())

    # the in-process removal
    def test_the_removal_marks_and_removes_btrfs_iocost(self) -> None:
        u = self.side.uninstall()
        self.assertEqual(u.record_path, record_file(Owner.ROOT, None, self.root))
        self.assertIsNone(u.mark(ComponentState.PENDING))
        self.assertEqual(u.remove(), [])  # nothing installed: nothing to remove
        self.side.installer().install()
        self.side.installer().mark_done()
        self.assertEqual(u.mark(ComponentState.PENDING), "done")
        self.assertEqual(self.side.state(), "pending")
        lines = u.remove()
        self.assertEqual([x.split()[0] for x in lines], ["removed:"] * 3, lines)
        self.assertEqual((self.side.in_place(), self.side.state()), ([], None))

    def test_a_removal_that_fails_raises_and_keeps_the_files_after_it(self) -> None:
        self.side.installer().install()
        rule = IOCOST_ENTRIES[-1].dest
        self.side.removal_fails(rule)
        with self.assertRaises(WriteFailed) as raised:
            self.side.uninstall().remove()
        self.assertEqual(str(raised.exception), f"cannot write {staged(self.root, rule)}: Permission denied")
        self.assertIn(rule, self.side.in_place())

    def test_a_record_it_cannot_mark_raises_an_error_revert_reports(self) -> None:
        self.side.record_unwritable()
        with self.assertRaises(UNINSTALL_ERRORS):
            self.side.uninstall().mark(ComponentState.PENDING)

    # the locks
    def test_a_lock_is_taken_released_and_taken_again(self) -> None:
        for name in ("qos", "component"):
            with self.subTest(name):
                lock = self.side.lock(name)
                self.assertTrue(lock.take())
                lock.release()
                self.assertTrue(lock.take())
                lock.release()

    def test_a_lock_this_run_holds_cannot_be_taken_again(self) -> None:
        lock = self.side.lock("qos")
        self.assertTrue(lock.take())
        with self.assertRaises(AssertionError):
            lock.take()
        lock.release()
        self.assertTrue(lock.take())  # the first take's hold was kept and let go
        lock.release()

    def test_a_lock_another_run_holds_is_not_taken(self) -> None:
        self.side.lock_busy("qos")
        self.assertFalse(self.side.lock("qos").take())
        self.assertTrue(self.side.lock("component").take())

    def test_a_lock_whose_folder_cannot_be_made_raises(self) -> None:
        self.side.lock_cannot_be_made("component")
        with self.assertRaises(OSError):
            self.side.lock("component").take()


class FakePortsContractTest(_PortsContract):
    @override
    def make_side(self, root: str) -> Side:
        return FakeSide(root)

    def test_io_cost_qos_keeps_one_line_per_device(self) -> None:
        # Only the fake models the kernel's merge by MAJ:MIN (a regular file is truncated by each open).
        q = self.side.qos()
        self.side.qos_present()
        q.write("259:2 enable=1 ctrl=user")
        q.write(LINE_ON)
        q.write(LINE_OFF)
        self.assertEqual(q.read(), f"259:2 enable=1 ctrl=user\n{LINE_OFF}\n")


class RealPortsContractTest(_PortsContract):
    @override
    def make_side(self, root: str) -> Side:
        side = RealSide(root)
        self.addCleanup(side.close)
        return side


del _PortsContract  # a base only: unittest runs its subclasses


class IocostAdapterPinsTest(unittest.TestCase):
    """The commands, files and locks of the real adapters, as they are run and written."""

    @override
    def setUp(self) -> None:
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="cst-iocost-pins-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.side = RealSide(self.root)
        self.addCleanup(self.side.close)
        self.runner = self.side.runner

    def test_the_commands_and_their_options(self) -> None:
        self.side.installer().install()
        self.side.installer().mark_done()
        self.side.installer().changes()
        s = self.side.systemd()
        s.probe()
        s.daemon_reload()
        s.enable_now()
        s.disable_now()
        u = self.side.udev()
        u.properties("nvme0n1")
        u.reload_rules()
        key, _, value = lc_ctype_handover().partition("=")
        inst = {"env": {key: value}, "input_text": None, "output": OutputMode.CAPTURE}
        local = {"check": False, "timeout": None, "locale_c": False}
        py = ["/usr/bin/python3", "-I", "-B", INSTALLER]
        self.assertEqual(
            self.runner.calls,
            [
                ([*py, "install", "--component", "btrfs:iocost"], inst),
                ([*py, "mark", "--component", "btrfs:iocost", "done"], inst),
                ([*py, "plan", "--component", "btrfs:iocost", "--root", self.root], inst),
                (
                    [
                        "systemctl",
                        "show",
                        "-p",
                        "LoadState",
                        "-p",
                        "ActiveState",
                        "-p",
                        "UnitFileState",
                        IOCOST_SERVICE,
                    ],
                    {"check": False, "timeout": 20, "locale_c": True, "on_host": True},
                ),
                (["systemctl", "daemon-reload"], local),
                (["systemctl", "enable", "--now", IOCOST_SERVICE], local),
                (["systemctl", "disable", "--now", IOCOST_SERVICE], local),
                (
                    ["udevadm", "info", "-q", "property", "-n", "/dev/nvme0n1"],
                    {"on_host": True, "locale_c": True, "timeout": 10.0, "output": OutputMode.CAPTURE},
                ),
                (["udevadm", "control", "--reload-rules"], {"output": OutputMode.CAPTURE}),
            ],
        )

    def test_the_plan_without_an_install_root_names_none(self) -> None:
        self.runner.plan = [UNIT]
        self.runner.calls.clear()
        runner = self.runner
        rc_root = IocostInstaller(runner, INSTALLER, self.root).changes()
        self.assertEqual(rc_root, (UNIT,))
        self.assertIsNone(IocostInstaller(runner, "", self.root).changes())
        self.assertEqual(len(runner.calls), 1)  # no checkout: the installer is not asked

    def test_udevadm_missing_or_failing_says_why(self) -> None:
        class Missing:
            def run(self, argv: Sequence[str], **_kw: object) -> CommandResult:
                err = FileNotFoundError(errno.ENOENT, "No such file or directory", "udevadm")
                return CommandResult(127, "", str(err), argv=tuple(argv), fault=CommandFault.MISSING, cause=err)

        got = IocostUdev(Missing(), "", "").properties("nvme0n1")
        self.assertEqual(got, UdevProperties(None, "[Errno 2] No such file or directory: 'udevadm'"))

        class Silent:
            def run(self, argv: Sequence[str], **_kw: object) -> CommandResult:
                return CommandResult(4, "", " \n", argv=tuple(argv))

        self.assertEqual(IocostUdev(Silent(), "", "").properties("sda"), UdevProperties(None, "exit 4"))

    def test_io_cost_qos_gets_one_open_and_one_write_per_line(self) -> None:
        self.side.qos_present()
        q = self.side.qos()
        q.write("259:2 enable=1 ctrl=user")
        q.write(LINE_ON)
        with open(self.side.qos_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), f"{LINE_ON}\n")  # a regular file: each write's open truncates it

    def test_the_managed_file_is_0644_in_a_new_folder_0755_one_device_per_line(self) -> None:
        self.side.managed().save(frozenset({"8:0", "259:1", "259:10"}))
        path = self.side.managed_path
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "259:1\n259:10\n8:0\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o755)
        self.assertEqual(os.listdir(os.path.dirname(path)), ["iocost-managed"])  # no temporary file left
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("not a device\n 7:0 \n")
        self.assertEqual(self.side.managed().read(), frozenset({"8:0", "259:1", "259:10", "7:0"}))
        self.side.managed().save(frozenset())
        self.assertFalse(os.path.lexists(path))

    def test_the_sysfs_scan_s_rules(self) -> None:
        sb = self.side.sys_block
        for rel, text in (
            ("nvme0n1p1/partition", "1\n"),
            ("nvme0n1p1/dev", "259:3\n"),
            ("vda/dev", "not a devno\n"),
            ("sdb/dev", "8:16\n"),
            ("sdc/dev", "8:32\n"),
            ("sdc/queue/rotational", "0\n"),
            ("sdc/queue/scheduler", "none\n"),
            ("sdd/dev", "8:48\n"),
            ("sdd/queue/rotational", "0\n"),
            ("sdd/queue/scheduler", "\n"),
        ):
            self.side._write(os.path.join(sb, rel), text)
        self.assertEqual(
            self.side.sysfs().disks(),
            (
                BlockDisk("sdb", "8:16", rotational=True, scheduler="unknown"),  # no rotational, no scheduler
                BlockDisk("sdc", "8:32", rotational=False, scheduler="none"),  # no brackets: the text
                BlockDisk("sdd", "8:48", rotational=False, scheduler="none"),  # empty: none
            ),
        )
        self.assertEqual(IocostSysfs(os.path.join(self.root, "nowhere"), "", "").disks(), ())

    def test_the_lock_files_and_their_time_limit(self) -> None:
        lock = IocostLockFile(self.side.locks["qos"], 0.2)
        self.assertTrue(lock.take())
        self.assertEqual(stat.S_IMODE(os.stat(lock.path).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(lock.path)).st_mode), 0o755)
        other = IocostLockFile(lock.path, 0.2)
        started = time.monotonic()
        self.assertFalse(other.take())
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        lock.release()
        lock.release()  # twice: nothing
        self.assertTrue(other.take())
        other.release()


if __name__ == "__main__":
    unittest.main()

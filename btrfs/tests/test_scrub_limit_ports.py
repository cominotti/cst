# SPDX-License-Identifier: MPL-2.0
"""scrub-limit's ports: one contract per port, run against its in-memory fake (btrfs/tests/fakes.py,
ScrubWorld, which the fault tests and the scrub machine use) and against its real adapter
(btrfs/scrub_limit/adapters.py, in a temporary folder, with the unit tests' runner fakes for the
installer and systemctl), so the fake cannot drift from what the adapter does. Then the real adapters'
own commands and files, pinned: what no fake can show (the installer's command line and stdin,
systemctl's, the time limits, the modes of what is written)."""

from __future__ import annotations

from collections.abc import Sequence
import errno
import os
import shutil
import stat
import tempfile
from typing import Protocol, override
import unittest

from btrfs.scrub_limit.adapters import ScrubDropinInstaller, ScrubSettingsFile, ScrubSysfs, ScrubSystemd
from btrfs.scrub_limit.ports import ScrubDropinPort, ScrubSettingsPort, ScrubSysfsPort, ScrubSystemdPort
from btrfs.tests.fakes import (
    FakeScrubDropin,
    FakeScrubSettings,
    FakeScrubSysfs,
    FakeScrubSystemd,
    ScrubFakeRunner,
    ScrubWorld,
    SystemdModel,
    core_text,
)
from cst_core.adapters import SystemctlHost
from cst_core.adapters.install import JsonInstallRecord
from cst_core.adapters.runtime import lc_ctype_handover
from cst_core.domain.install import Component
from cst_core.domain.record import ComponentState
from cst_core.ports import CommandResult, OutputMode

TEXT_A = "[Service]\nExecStartPre=a\n"
TEXT_B = "[Service]\nExecStartPre=b\n"
INSTALLER = "/opt/cst/cst_core/installer.py"


def _access_denied() -> OSError:
    return OSError(errno.EACCES, "Access denied")


class Side(Protocol):
    """One implementation of the ports under one contract: the port, and how a test sets its state."""

    def settings(self) -> ScrubSettingsPort: ...
    def dropin(self) -> ScrubDropinPort: ...
    def systemd(self) -> ScrubSystemdPort: ...
    def sysfs(self) -> ScrubSysfsPort: ...
    def conf_unreadable(self) -> None: ...
    def conf_folder_unwritable(self) -> None: ...
    def dropin_unreadable(self) -> None: ...
    def dropin_folder_exists(self) -> bool: ...
    def installer_fails(self) -> None: ...
    def reload_fails(self) -> None: ...
    def show_fails(self) -> None: ...
    def loaded(self) -> str | None: ...
    def install_core(self, *, done: bool) -> None: ...
    def core_state(self) -> ComponentState | None: ...
    def add_device(self, uuid: str, devid: str, value: str) -> None: ...


class FakeSide:
    """The in-memory fakes over one ScrubWorld."""

    def __init__(self, conf: str, dropin: str, record: str) -> None:
        self.world = ScrubWorld(conf_path=conf, dropin_path=dropin, record=record)

    def settings(self) -> ScrubSettingsPort:
        return FakeScrubSettings(self.world)

    def dropin(self) -> ScrubDropinPort:
        return FakeScrubDropin(self.world)

    def systemd(self) -> ScrubSystemdPort:
        return FakeScrubSystemd(self.world)

    def sysfs(self) -> ScrubSysfsPort:
        return FakeScrubSysfs(self.world)

    def conf_unreadable(self) -> None:
        self.world.conf_unreadable = True

    def conf_folder_unwritable(self) -> None:
        self.world.fail("settings folder", OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR)))

    def dropin_unreadable(self) -> None:
        self.world.dropin_unreadable = True

    def dropin_folder_exists(self) -> bool:
        return self.world.dropin_folder

    def installer_fails(self) -> None:
        self.world.fail("drop-in text", _access_denied())
        self.world.fail("drop-in remove-text", _access_denied())

    def reload_fails(self) -> None:
        self.world.fail("systemctl daemon-reload", _access_denied())

    def show_fails(self) -> None:
        self.world.show_fails = True

    def loaded(self) -> str | None:
        return self.world.loaded

    def install_core(self, *, done: bool) -> None:
        self.world.install_core(done=done)

    def core_state(self) -> ComponentState | None:
        return self.world.core_state()

    def add_device(self, uuid: str, devid: str, value: str) -> None:
        line = f"{uuid[:8]} device {devid} {value.strip()}"
        self.world.live = tuple(sorted((*self.world.live, line)))


class RealSide:
    """The real adapters in a temporary folder; the installer and systemctl through SystemdModel."""

    def __init__(self, conf: str, dropin: str, record: str) -> None:
        self.conf, self.dropin_path, self.record = conf, dropin, record
        self.sys = os.path.join(os.path.dirname(record), "sys")
        self.model = SystemdModel(dropin, record)

    def settings(self) -> ScrubSettingsPort:
        return ScrubSettingsFile(self.conf)

    def dropin(self) -> ScrubDropinPort:
        return ScrubDropinInstaller(self.dropin_path, INSTALLER, self.model)

    def systemd(self) -> ScrubSystemdPort:
        return ScrubSystemd(self.model, SystemctlHost.LOCAL)

    def sysfs(self) -> ScrubSysfsPort:
        return ScrubSysfs(self.sys)

    def conf_unreadable(self) -> None:
        os.makedirs(self.conf)  # a folder where the file should be

    def conf_folder_unwritable(self) -> None:
        folder = os.path.dirname(self.conf)
        os.makedirs(os.path.dirname(folder), exist_ok=True)
        with open(folder, "w", encoding="utf-8") as fh:  # a file where its folder should be
            fh.write("x\n")

    def dropin_unreadable(self) -> None:
        os.makedirs(self.dropin_path)

    def dropin_folder_exists(self) -> bool:
        return os.path.isdir(os.path.dirname(self.dropin_path))

    def installer_fails(self) -> None:
        self.model.fail_on = {"installer.py"}

    def reload_fails(self) -> None:
        self.model.fail_on = {"systemctl daemon-reload"}

    def show_fails(self) -> None:
        self.model.fail_on = {"systemctl show"}

    def loaded(self) -> str | None:
        return self.model.loaded

    def install_core(self, *, done: bool) -> None:
        seed = os.path.join(os.path.dirname(self.record), "core-seed")
        self.model.svc.put(core_text(seed, self.record), b"seed\n", as_root=False)
        if done:
            self.model.svc.mark(self.record, Component.BTRFS_CORE.value)

    def core_state(self) -> ComponentState | None:
        rec = JsonInstallRecord().read(self.record)
        return None if rec is None else rec.components.get(Component.BTRFS_CORE.value)

    def add_device(self, uuid: str, devid: str, value: str) -> None:
        path = os.path.join(self.sys, uuid, "devinfo", devid, "scrub_speed_max")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)


class _PortsContract(unittest.TestCase):
    """The contract of each port; FakePortsContractTest and RealPortsContractTest give the side."""

    def make_side(self, conf: str, dropin: str, record: str) -> Side:
        raise NotImplementedError

    @override
    def setUp(self) -> None:
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-scrub-ports-"))
        self.addCleanup(shutil.rmtree, tmp)
        self.conf = os.path.join(tmp, "etc", "btrfs-setup", "snapshots.conf")
        self.dropin = os.path.join(tmp, "etc", "systemd", "system", "btrfs-scrub-weekly.service.d", "10-cst-limit.conf")
        self.side = self.make_side(self.conf, self.dropin, os.path.join(tmp, "installed.json"))

    def test_settings_reads_writes_and_removes_snapshots_conf(self) -> None:
        p = self.side.settings()
        self.assertEqual(p.path, self.conf)
        self.assertEqual(p.read(), (None, False))
        self.assertFalse(p.folder_exists())
        p.remove()  # not there: nothing
        p.write("A=1\r\nB=2\n")
        self.assertTrue(p.folder_exists())
        self.assertEqual(p.read(), ("A=1\r\nB=2\n", False))  # line ends as they are
        p.write("C=3\n")
        self.assertEqual(p.read(), ("C=3\n", False))
        p.remove_folder_if_empty()
        self.assertTrue(p.folder_exists())  # it holds the file
        p.remove()
        p.remove()
        self.assertEqual(p.read(), (None, False))
        self.assertTrue(p.folder_exists())
        p.remove_folder_if_empty()
        self.assertFalse(p.folder_exists())
        p.remove_folder_if_empty()  # not there: no error

    def test_settings_tells_an_unreadable_file_from_a_missing_one(self) -> None:
        self.side.conf_unreadable()
        self.assertEqual(self.side.settings().read(), (None, True))

    def test_a_settings_write_that_cannot_make_its_folder_raises(self) -> None:
        self.side.conf_folder_unwritable()
        p = self.side.settings()
        with self.assertRaises(OSError):
            p.write("A=1\n")
        self.assertFalse(p.folder_exists())

    def test_the_drop_in_through_the_installer_answers_its_report_lines(self) -> None:
        d = self.side.dropin()
        self.assertEqual(d.path, self.dropin)
        self.assertEqual(d.read(), (None, False))
        self.assertEqual(d.write(TEXT_A), (0, [f"written:   {self.dropin}"], ""))
        self.assertEqual(d.read(), (TEXT_A, False))
        self.assertEqual(d.write(TEXT_A), (0, [f"unchanged: {self.dropin}"], ""))
        self.assertEqual(d.write(TEXT_B), (0, [f"written:   {self.dropin}"], ""))
        d.remove_folder_if_empty()
        self.assertTrue(self.side.dropin_folder_exists())  # it holds the drop-in
        self.assertEqual(d.remove(), (0, [f"removed:   {self.dropin}"], ""))
        self.assertEqual(d.remove(), (0, [], ""))
        self.assertEqual(d.read(), (None, False))
        d.remove_folder_if_empty()
        self.assertFalse(self.side.dropin_folder_exists())
        d.remove_folder_if_empty()  # not there: no error

    def test_a_failed_installer_answers_exit_1_and_its_error(self) -> None:
        self.side.installer_fails()
        d = self.side.dropin()
        self.assertEqual(d.write(TEXT_A), (1, [], "Access denied"))
        self.assertEqual(d.remove(), (1, [], "Access denied"))
        self.assertEqual(d.read(), (None, False))

    def test_the_drop_in_tells_an_unreadable_file_from_a_missing_one(self) -> None:
        self.side.dropin_unreadable()
        self.assertEqual(self.side.dropin().read(), (None, True))

    def test_the_drop_in_never_marks_btrfs_core_done(self) -> None:
        self.side.dropin().write(TEXT_A)  # never installed: pending at most, never done
        self.assertIn(self.side.core_state(), (None, ComponentState.PENDING))

    def keeps_core(self, *, done: bool) -> None:
        self.side.install_core(done=done)
        want = ComponentState.DONE if done else ComponentState.PENDING
        d = self.side.dropin()
        for step in (lambda: d.write(TEXT_A), lambda: d.write(TEXT_B), d.remove, lambda: d.write(TEXT_A)):
            step()
            self.assertEqual(self.side.core_state(), want)  # off's remove: the seed still names btrfs:core

    def test_the_drop_in_keeps_btrfs_core_done(self) -> None:
        self.keeps_core(done=True)

    def test_the_drop_in_keeps_btrfs_core_pending(self) -> None:
        self.keeps_core(done=False)

    def test_systemd_follows_the_drop_in_s_writes_and_reloads(self) -> None:
        s, d = self.side.systemd(), self.side.dropin()
        behind = [s.need_daemon_reload()]
        d.write(TEXT_A)
        behind.append(s.need_daemon_reload())
        self.assertEqual(s.daemon_reload(timeout=None).returncode, 0)
        behind.append(s.need_daemon_reload())
        self.assertEqual(self.side.loaded(), TEXT_A)
        d.write(TEXT_A)  # unchanged: nothing for systemd to load
        behind.append(s.need_daemon_reload())
        d.remove()
        behind.append(s.need_daemon_reload())
        self.assertEqual(s.daemon_reload(timeout=60).returncode, 0)
        self.assertIsNone(self.side.loaded())
        self.assertEqual(behind, [False, True, False, False, True])

    def test_a_failed_reload_says_why_and_loads_nothing(self) -> None:
        s, d = self.side.systemd(), self.side.dropin()
        d.write(TEXT_A)
        self.side.reload_fails()
        r = s.daemon_reload(timeout=None)
        self.assertEqual(
            (r.returncode, r.failure("systemctl daemon-reload")), (1, "systemctl daemon-reload failed: Access denied")
        )
        self.assertIsNone(self.side.loaded())

    def test_systemctl_that_cannot_answer_says_none(self) -> None:
        self.side.show_fails()
        self.assertIsNone(self.side.systemd().need_daemon_reload())

    def test_sysfs_lists_each_device_s_limit(self) -> None:
        self.assertEqual(self.side.sysfs().live_limits(), ())
        self.side.add_device("bbbbbbbb-2222-2222-2222-222222222222", "1", "0\n")
        self.side.add_device("aaaaaaaa-1111-1111-1111-111111111111", "2", "524288000\n")
        self.side.add_device("aaaaaaaa-1111-1111-1111-111111111111", "1", "0\n")
        self.assertEqual(
            self.side.sysfs().live_limits(),
            ("aaaaaaaa device 1 0", "aaaaaaaa device 2 524288000", "bbbbbbbb device 1 0"),
        )


class FakePortsContractTest(_PortsContract):
    @override
    def make_side(self, conf: str, dropin: str, record: str) -> Side:
        return FakeSide(conf, dropin, record)


class RealPortsContractTest(_PortsContract):
    @override
    def make_side(self, conf: str, dropin: str, record: str) -> Side:
        return RealSide(conf, dropin, record)


del _PortsContract  # a base only: unittest runs its subclasses


class _Recording(ScrubFakeRunner):
    """ScrubFakeRunner that keeps every call's options too."""

    def __init__(self) -> None:
        super().__init__()
        self.options: list[dict[str, object]] = []

    @override
    def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
        self.options.append(kw)
        return super().run(argv, input_text=input_text, **kw)


class ScrubAdapterPinsTest(unittest.TestCase):
    """The commands and files of the real adapters, as they are run and written."""

    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-scrub-pins-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_the_drop_in_s_installer_command_lines(self) -> None:
        runner = _Recording()
        path = os.path.join(self.tmp, "d", "10-cst-limit.conf")
        d = ScrubDropinInstaller(path, INSTALLER, runner)
        d.write(TEXT_A)
        d.remove()
        key, _, value = lc_ctype_handover().partition("=")
        self.assertEqual(
            runner.calls,
            [
                [
                    "/usr/bin/python3",
                    "-I",
                    "-B",
                    INSTALLER,
                    "text",
                    path,
                    "0644",
                    "--component",
                    "btrfs:core",
                    "--keep-state",
                ],
                ["/usr/bin/python3", "-I", "-B", INSTALLER, "remove-text", path],
            ],
        )
        self.assertEqual(runner.inputs, [TEXT_A, None])
        self.assertEqual(runner.options, [{"env": {key: value}, "output": OutputMode.CAPTURE}] * 2)

    def test_systemctl_s_question_and_reloads(self) -> None:
        for on_host in (SystemctlHost.SHOW_ONLY, SystemctlHost.LOCAL):
            with self.subTest(on_host=on_host):
                runner = _Recording()
                runner.need_daemon_reload = "yes\n"
                s = ScrubSystemd(runner, on_host)
                self.assertEqual([s.need_daemon_reload()], [True])
                s.daemon_reload(timeout=None)
                s.daemon_reload(timeout=60)
                self.assertEqual(
                    runner.calls,
                    [
                        ["systemctl", "show", "-p", "NeedDaemonReload", "btrfs-scrub-weekly.service"],
                        ["systemctl", "daemon-reload"],
                        ["systemctl", "daemon-reload"],
                    ],
                )
                self.assertEqual(
                    runner.options,
                    [
                        {
                            "check": False,
                            "timeout": 20,
                            "locale_c": True,
                            "on_host": on_host == SystemctlHost.SHOW_ONLY,
                        },
                        {"check": False, "timeout": None, "locale_c": False},
                        {"check": False, "timeout": 60, "locale_c": False},
                    ],
                )

    def test_snapshots_conf_is_written_0644_in_a_new_folder_0755(self) -> None:
        conf = os.path.join(self.tmp, "etc", "btrfs-setup", "snapshots.conf")
        ScrubSettingsFile(conf).write("A=1\n")
        self.assertEqual(stat.S_IMODE(os.stat(conf).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(conf)).st_mode), 0o755)
        self.assertEqual(os.listdir(os.path.dirname(conf)), ["snapshots.conf"])  # no temporary file left


if __name__ == "__main__":
    unittest.main()

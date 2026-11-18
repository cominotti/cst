# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the cst_core Shared Kernel and base adapters."""

from collections.abc import Callable, Mapping, Sequence
import contextlib
import fcntl
import io
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from typing import Final, Literal
import unittest
from unittest import mock

from cst_core.adapters import SystemClockAdapter, SystemctlAdapter
from cst_core.adapters.runner import HostExecutionContext, can_spawn_on_host, on_host_argv, SubprocessCommandRunner
from cst_core.adapters.virsh import SubprocessVirshAdapter
from cst_core.adapters.dispatch import LocalCstDispatchAdapter
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.adapters.runtime import lc_ctype_handover, undo_python_start
from cst_core import wiring as core_wiring
from cst_core.domain.contracts import state_format, version_of
from cst_core.domain.install import INSTALL_MANIFEST
from cst_core.domain.ostree import OstreeStatus, query_status
from cst_core.application.dispatch import CstDispatcher, ModuleRouter, RouteSettings
from cst_core.domain.dispatch import MODULES, ROUTING, Routed, format_cst_usage, route_command, script_help
from cst_core.domain.files import MarkedLineSpec, effective_lines, inspect_managed_file
from cst_core.domain.plan import PlanItem
from cst_core.domain.values import (
    ByteSize,
    CpuSet,
    DomainValidationError,
    Duration,
    OctalMode,
    format_bytes_decimal,
    format_bytes_gib_mib,
    format_bytes_human,
)
from cst_core.ports import CommandFailed, CommandResult

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEM_PYTHON = "/usr/bin/python3"
_CLEAN_ENV: Mapping[str, str] = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}


class FakeRunner:
    """In-memory CommandRunnerPort recording invocations for SystemctlAdapter tests."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        **_options: object,
    ) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        res = self.responses.get(tuple(cmd), CommandResult(0, "", ""))
        if check and res.returncode != 0:
            raise RuntimeError(f"command failed: {cmd}")
        return res


class ValueObjectsTest(unittest.TestCase):
    def test_byte_size_parse_and_human(self) -> None:
        self.assertEqual(ByteSize.parse("48G"), ByteSize(48 * 2**30))
        self.assertEqual(ByteSize.parse(" 512M "), ByteSize(512 * 2**20))
        self.assertEqual(ByteSize.parse("4K"), ByteSize(4096))
        self.assertEqual(ByteSize.parse("2T"), ByteSize(2 * 2**40))
        self.assertEqual(ByteSize.parse("1024"), ByteSize(1024))
        self.assertIsNone(ByteSize.parse("infinity"))
        self.assertIsNone(ByteSize.parse(None))
        self.assertIsNone(ByteSize.parse(""))

        self.assertEqual(ByteSize(2 * 2**30).human(), "2.0 GiB")
        self.assertEqual(ByteSize.parse_iec("150G"), ByteSize(150 * 2**30))
        self.assertEqual(ByteSize.parse_iec("off"), ByteSize(0))
        self.assertEqual(format_bytes_human(None), "-")
        self.assertEqual(format_bytes_human(512), "512 B")
        self.assertEqual(format_bytes_human(-2048), "-2.0 KiB")
        self.assertEqual(format_bytes_gib_mib(2**30), "1.0 GiB")
        self.assertEqual(format_bytes_gib_mib(512 * 2**20), "512 MiB")
        self.assertEqual(format_bytes_decimal(1500), "1.5 KB")

        with self.assertRaises(DomainValidationError):
            ByteSize(-1)
        with self.assertRaises(DomainValidationError):
            ByteSize.parse_iec("150")

    def test_duration_parse(self) -> None:
        self.assertEqual(Duration.parse("30s"), Duration(30))
        self.assertEqual(Duration.parse("15m"), Duration(900))
        self.assertEqual(Duration.parse("48h"), Duration(48 * 3600))
        self.assertEqual(Duration.parse("7d"), Duration(7 * 86400))
        self.assertEqual(Duration.parse("2w"), Duration(2 * 604800))
        self.assertEqual(Duration.parse_float("120", allow_unitless=True, positive_only=True), 120.0)
        self.assertEqual(Duration.parse_float("1.5h"), 5400.0)
        for bad in ("150", "", "5x", "-1s"):
            with self.subTest(bad=bad), self.assertRaises(DomainValidationError):
                Duration.parse(bad)
        with self.assertRaises(DomainValidationError):
            Duration(-5)

    def test_cpu_set_parse(self) -> None:
        cs = CpuSet.parse("0-3,6,12-13")
        self.assertEqual(cs.cpus, frozenset({0, 1, 2, 3, 6, 12, 13}))
        self.assertEqual(cs.sorted_cpus(), (0, 1, 2, 3, 6, 12, 13))
        for bad in ("", " , ", "3-1", "a-b", "0-1-2", "cpu0"):
            with self.subTest(bad=bad), self.assertRaises(DomainValidationError):
                CpuSet.parse(bad)
        with self.assertRaises(DomainValidationError):
            CpuSet(frozenset())
        with self.assertRaises(DomainValidationError):
            CpuSet(frozenset({-1}))

    def test_octal_mode_parse(self) -> None:
        self.assertEqual(OctalMode.parse("0600"), OctalMode(0o600))
        self.assertEqual(OctalMode.parse("644"), OctalMode(0o644))
        self.assertEqual(OctalMode.parse(0o755), OctalMode(0o755))
        for bad in ("999", "064444", "abc", -1, 0o10000, True):
            with self.subTest(bad=bad), self.assertRaises(DomainValidationError):
                OctalMode.parse(bad)


class ManagedFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = MarkedLineSpec(
            desired_line="/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 # managed-by: swap-setup.sh (CST)",
            key="/dev/mapper/swap0",
            current_marker="# managed-by: swap-setup.sh (CST)",
            legacy_markers=("# managed-by: apply-swap-config.sh", "# managed-by: apply-zram-config.sh"),
        )

    def test_marked_line_states(self) -> None:
        old = "/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 # managed-by: apply-swap-config.sh"
        older = "/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 # managed-by: apply-zram-config.sh"
        changed = "/dev/mapper/swap0 none swap defaults,nofail,pri=5 0 0 # managed-by: apply-swap-config.sh"

        self.assertEqual(self.spec.inspect(None, exists=False, readable=True), "missing")
        self.assertEqual(self.spec.inspect(None, exists=True, readable=False), "unreadable")
        self.assertEqual(self.spec.inspect("UUID=x / btrfs defaults 0 0\n", exists=True, readable=True), "missing")
        self.assertEqual(self.spec.inspect(self.spec.desired_line + "\n", exists=True, readable=True), "ok")
        self.assertEqual(self.spec.inspect(old + "\n", exists=True, readable=True), "adopt")
        self.assertEqual(self.spec.inspect(older + "\n", exists=True, readable=True), "adopt")
        self.assertEqual(
            self.spec.inspect(f"{self.spec.desired_line}\n{old}\n", exists=True, readable=True), "duplicate"
        )
        self.assertEqual(self.spec.inspect(f"{old}\n{changed}\n", exists=True, readable=True), "change")
        self.assertEqual(self.spec.inspect(changed + "\n", exists=True, readable=True), "change")
        self.assertEqual(
            self.spec.inspect("/dev/mapper/swap0 none swap defaults 0 0\n", exists=True, readable=True), "conflict"
        )

    def test_render_updated_replaces_in_place_and_dedupes(self) -> None:
        old = "/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 # managed-by: apply-swap-config.sh"
        existing = f"# head\n{old}\nUUID=x / btrfs defaults 0 0\n{old}\n# tail\n"
        updated = self.spec.render_updated(existing)
        self.assertEqual(
            updated,
            f"# head\n{self.spec.desired_line}\nUUID=x / btrfs defaults 0 0\n# tail\n",
        )
        appended = self.spec.render_updated("UUID=x / btrfs defaults 0 0\n")
        self.assertEqual(appended, f"UUID=x / btrfs defaults 0 0\n{self.spec.desired_line}\n")

    def test_inspect_managed_file(self) -> None:
        desired = "# new comment\nvm.swappiness = 100\n\nvm.page-cluster = 0\n"
        adopted = "# old comment\n\nvm.swappiness = 100\nvm.page-cluster = 0\n"
        changed = "# old comment\nvm.swappiness = 60\nvm.page-cluster = 0\n"

        self.assertEqual(effective_lines(desired), ["vm.swappiness = 100", "vm.page-cluster = 0"])
        self.assertEqual(
            inspect_managed_file(desired, desired, exists=True, is_symlink=False, readable=True),
            "ok",
        )
        self.assertEqual(
            inspect_managed_file(desired, adopted, exists=True, is_symlink=False, readable=True),
            "adopt",
        )
        self.assertEqual(
            inspect_managed_file(desired, changed, exists=True, is_symlink=False, readable=True),
            "change",
        )
        self.assertEqual(
            inspect_managed_file(desired, None, exists=False, is_symlink=False, readable=True),
            "missing",
        )
        self.assertEqual(
            inspect_managed_file(desired, None, exists=True, is_symlink=False, readable=False),
            "unreadable",
        )
        self.assertEqual(
            inspect_managed_file(desired, desired, exists=True, is_symlink=True, readable=True),
            "change",
        )


class PlanItemTest(unittest.TestCase):
    def test_plan_item(self) -> None:
        row = PlanItem("ok", "crypttab: swap0 line", "up to date")
        self.assertEqual(row.format_line(), "  ok        crypttab: swap0 line                   up to date")
        self.assertEqual(PlanItem("ok", "zram0").format_line(), "  ok        zram0")


class AdaptersTest(unittest.TestCase):
    def test_virsh_in_a_new_session_replaces_bytes_that_are_not_utf8(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-core-virsh-")
        self.addCleanup(shutil.rmtree, tmp)
        stub = os.path.join(tmp, "virsh")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\nprintf "\\377abc LC_ALL=%s" "$LC_ALL"\n')
        os.chmod(stub, 0o755)
        virsh = SubprocessVirshAdapter(ctx=HostExecutionContext(exists=lambda _path: False))
        with mock.patch.dict(os.environ, {"PATH": f"{tmp}:/usr/bin:/bin"}):
            res = virsh.call("domstate", "vm1", start_new_session=True)
        self.assertEqual((res.returncode, res.stdout), (0, "\ufffdabc LC_ALL=C"))
        self.assertEqual(res.argv, ("virsh", "-c", "qemu:///system", "domstate", "vm1"))

    def test_local_filesystem_adapter(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-core-fs-")
        self.addCleanup(shutil.rmtree, tmp)
        fs = LocalFileSystemAdapter()

        target = os.path.join(tmp, "sub", "file.conf")
        fs.write_atomic(target, "line1\nline2\n", mode=0o600)
        self.assertTrue(fs.exists(target))
        self.assertTrue(fs.is_file(target))
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(fs.read_text(target), "line1\nline2\n")
        self.assertEqual(fs.read_first_line(target), "line1\nline2")
        # Byte-transparent, as bash's read -r was on fstab/crypttab: no decode error, no newline translation.
        raw = os.path.join(tmp, "raw")
        with open(raw, "wb") as fh:
            fh.write(b"caf\xe9\r\nx\n")
        text = fs.read_text(raw)
        assert text is not None
        self.assertTrue(text.endswith("\r\nx\n"))
        fs.write_atomic(raw, text, mode=0o644)
        with open(raw, "rb") as fh:
            self.assertEqual(fh.read(), b"caf\xe9\r\nx\n")

        bak = target + ".bak"
        fs.copy_if_absent(target, bak)
        fs.write_atomic(target, "changed\n", mode=0o644)
        fs.copy_if_absent(target, bak)  # keeps original backup
        self.assertEqual(fs.read_text(bak), "line1\nline2\n")

        link = os.path.join(tmp, "link")
        fs.force_symlink("/dev/null", link)
        self.assertTrue(fs.is_symlink(link))
        self.assertEqual(fs.readlink(link), "/dev/null")
        fs.write_atomic(link, "replaced\n", mode=0o644)
        self.assertFalse(fs.is_symlink(link))
        self.assertEqual(fs.read_text(link), "replaced\n")

        fs.remove_file(link)
        fs.remove_file(link)  # idempotent
        self.assertFalse(fs.lexists(link))

    def test_subprocess_runner_and_systemctl_adapter(self) -> None:
        runner = SubprocessCommandRunner()
        res = runner.run([sys.executable, "-c", "import sys; print('hi:' + sys.stdin.read())"], check=True)
        self.assertEqual(res.stdout.strip(), "hi:")
        res_in = runner.run(
            [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
            check=True,
            input_text="abc",
        )
        self.assertEqual(res_in.stdout.strip(), "abc")
        with self.assertRaises(CommandFailed) as ctx:
            runner.run([sys.executable, "-c", "import sys; sys.exit(3)"], check=True)
        self.assertEqual(ctx.exception.returncode, 3)
        missing = runner.run(["/nonexistent/cst-binary-check"], check=False)
        self.assertEqual(missing.returncode, 127)

        fake = FakeRunner(
            {
                ("systemd-escape", "-p", "--suffix=swap", "/dev/mapper/swap0"): CommandResult(
                    0, "dev-mapper-swap0.swap\n", ""
                ),
                ("systemctl", "stop", "dev-zram0.swap"): CommandResult(1, "", "failed"),
            }
        )
        sysd = SystemctlAdapter(fake)
        sysd.daemon_reload()
        self.assertEqual(sysd.start("systemd-cryptsetup@swap0.service").returncode, 0)
        self.assertEqual(sysd.restart("systemd-cryptsetup@swap0.service").returncode, 0)
        # systemctl's message comes back with its exit status: swap apply shows both.
        self.assertEqual(sysd.stop("dev-zram0.swap"), CommandResult(1, "", "failed"))
        self.assertEqual(sysd.escape_path("/dev/mapper/swap0", suffix="swap"), "dev-mapper-swap0.swap")

    def test_host_execution_context_and_clock(self) -> None:
        host = HostExecutionContext(exists=lambda _p: False, euid=lambda: 1000)
        self.assertFalse(host.is_container())
        self.assertFalse(host.is_root())
        self.assertEqual(host.etc_dir(), "/etc")
        self.assertEqual(host.host_run_dir("cpu-setup"), "/run/cpu-setup")
        self.assertEqual(host.path("/proc/meminfo"), "/proc/meminfo")

        box = HostExecutionContext(exists=lambda p: p == "/.dockerenv", euid=lambda: 0)
        self.assertTrue(box.is_container())
        self.assertTrue(box.is_root())
        self.assertEqual(box.etc_dir(), "/run/host/etc")
        self.assertEqual(box.host_run_dir("cpu-setup"), "/run/host/run/cpu-setup")
        self.assertEqual(box.path("/proc/meminfo"), "/run/host/proc/meminfo")

        clock = SystemClockAdapter()
        self.assertGreater(clock.now(), 0.0)
        self.assertGreater(clock.monotonic(), 0.0)


class _FakeDispatchPort:
    """In-memory CstDispatchPort double recording executed subprocesses."""

    def __init__(
        self,
        *,
        files: set[str] | None = None,
        container: bool = False,
        uid: int = 1000,
        env: Mapping[str, str] | None = None,
        passwd: Mapping[str, tuple[int, str]] | None = None,
        handler: Callable[[list[str], Mapping[str, str] | None], int] | None = None,
        on_path: Mapping[str, str] | None = None,
        links: Mapping[str, str] | None = None,
    ) -> None:
        self.on_path = {} if on_path is None else dict(on_path)
        self.links = {} if links is None else dict(links)
        self.files = set() if files is None else files
        self.container = container
        self.uid = uid
        self.env = {} if env is None else dict(env)
        self.passwd = {} if passwd is None else dict(passwd)
        self.handler = handler
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []
        self.execs: list[list[str]] = []

    def is_file(self, path: str) -> bool:
        return path in self.files

    def is_container(self) -> bool:
        return self.container

    def euid(self) -> int:
        return self.uid

    def environ(self) -> Mapping[str, str]:
        return dict(self.env)

    def user_passwd(self, user: str) -> tuple[int, str]:
        return self.passwd[user]

    def which(self, name: str, path: str | None) -> str | None:
        return self.on_path.get(name) if path else None

    def realpath(self, path: str) -> str:
        return self.links.get(path, path)

    def run_passthrough(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        cmd = list(argv)
        copied_env = None if env is None else dict(env)
        self.calls.append((cmd, copied_env))
        if self.handler is not None:
            return self.handler(cmd, copied_env)
        return 0

    def exec_replace(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        self.execs.append(list(argv))
        return self.run_passthrough(argv, env=env)


class CstDispatcherTest(unittest.TestCase):
    def _dispatcher(
        self, port: _FakeDispatchPort, *, repo_dir: str = "/repo", arg0: str = "./cst"
    ) -> tuple[CstDispatcher, io.StringIO, io.StringIO]:
        out = io.StringIO()
        err = io.StringIO()
        dispatcher = CstDispatcher(
            port=port,
            repo_dir=repo_dir,
            self_path=f"{repo_dir}/cst",
            arg0=arg0,
            stdout=out,
            stderr=err,
        )
        return dispatcher, out, err

    def test_format_cst_usage_marks_missing_modules(self) -> None:
        text = format_cst_usage({"btrfs": True, "cpu": False})
        self.assertIn("btrfs      storage:", text)
        self.assertNotIn("btrfs      storage:.*not in this copy", text)
        self.assertIn("cpu-setup.sh", text)
        self.assertIn("(not in this copy)", text)

    def test_help_and_unknown_command(self) -> None:
        files = {f"/repo/{m.script}" for m in MODULES if m.name != "vm"}
        port = _FakeDispatchPort(files=files, env={"CST_NAME": "cst"})
        dispatcher, out, err = self._dispatcher(port)

        self.assertEqual(dispatcher.dispatch([]), 0)
        self.assertIn("Modules here:", out.getvalue())
        self.assertIn("  vm         ", out.getvalue())
        self.assertIn("(not in this copy)", out.getvalue())

        # bash's `cmd="${1:-help}"`: one empty word is help too.
        out.truncate(0)
        self.assertEqual(dispatcher.dispatch([""]), 0)
        self.assertIn("Modules here:", out.getvalue())
        self.assertEqual(err.getvalue(), "")

        self.assertEqual(dispatcher.dispatch(["nope"]), 2)
        self.assertIn("cst: unknown command or module 'nope'", err.getvalue())
        self.assertIn("Modules here:", err.getvalue())

    def test_the_name_in_messages_comes_from_the_port(self) -> None:
        # "cst" when the cst on PATH is this copy (through a link), else how it was called.
        missing = ["nope"]
        for on_path, links, want in (
            ({"cst": "/home/u/.local/bin/cst"}, {"/home/u/.local/bin/cst": "/repo/cst"}, "cst"),
            ({"cst": "/usr/bin/cst"}, {}, "./cst"),
            ({}, {}, "./cst"),
        ):
            port = _FakeDispatchPort(env={"PATH": "/usr/bin"}, on_path=on_path, links=links)
            dispatcher, _, err = self._dispatcher(port)
            with self.subTest(on_path=on_path):
                self.assertEqual(dispatcher.dispatch(missing), 2)
                self.assertTrue(
                    err.getvalue().startswith(f"{want}: unknown command or module 'nope'\n"), err.getvalue()
                )

    def test_module_dispatch_and_missing_module(self) -> None:
        port = _FakeDispatchPort(files={"/repo/swap-setup.sh"}, env={"CST_NAME": "cst"})
        dispatcher, _, err = self._dispatcher(port)

        self.assertEqual(dispatcher.dispatch(["swap", "plan", "--root"]), 0)
        cmd, env = port.calls[0]
        self.assertEqual(cmd, ["bash", "/repo/swap-setup.sh", "plan", "--root"])
        assert env is not None
        self.assertEqual(env["CST_PROG"], "cst swap")
        self.assertEqual(port.execs, [cmd], "the module replaces cst (bash's exec)")

        self.assertEqual(dispatcher.dispatch(["vm", "status"]), 1)
        self.assertIn("cst: the 'vm' module is not in this copy (/repo/vm-setup.sh is missing)", err.getvalue())

    def test_doctor_as_user_and_via_sudo_user(self) -> None:
        doc = "/repo/doctor/cst-doctor"
        user_port = _FakeDispatchPort(files={doc}, uid=1000, env={"CST_NAME": "cst"})
        user_disp, _, _ = self._dispatcher(user_port)
        self.assertEqual(user_disp.dispatch(["doctor", "--brief"]), 0)
        self.assertEqual(user_port.calls[0][0], ["python3", "-I", "-B", doc, "--brief"])
        self.assertEqual(user_port.execs, [], "doctor runs in the foreground, not exec'd")
        assert user_port.calls[0][1] is not None
        self.assertEqual(user_port.calls[0][1]["CST_PROG"], "cst doctor")

        root_port = _FakeDispatchPort(
            files={doc},
            uid=0,
            env={"CST_NAME": "cst", "SUDO_USER": "alice"},
            passwd={"alice": (1001, "/home/alice")},
        )
        root_disp, _, _ = self._dispatcher(root_port)
        self.assertEqual(root_disp.dispatch(["doctor"]), 0)
        self.assertEqual(
            root_port.calls[0][0],
            [
                "runuser",
                "-u",
                "alice",
                "--",
                "env",
                "-u",
                "XDG_STATE_HOME",
                "-u",
                "SUDO_USER",
                "HOME=/home/alice",
                "USER=alice",
                "LOGNAME=alice",
                "XDG_RUNTIME_DIR=/run/user/1001",
                "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus",
                "CST_PROG=cst doctor",
                "python3",
                "-I",
                "-B",
                doc,
            ],
        )

    def test_status_elevation_and_container_skip(self) -> None:
        all_files = {f"/repo/{m.script}" for m in MODULES} | {"/repo/doctor/cst-doctor"}
        host_nonroot = _FakeDispatchPort(files=all_files, container=False, uid=1000, env={"CST_NAME": "cst"})
        disp_elevate, _, err = self._dispatcher(host_nonroot)
        self.assertEqual(disp_elevate.dispatch(["status", "extra"]), 2)
        self.assertIn("cst status: no arguments", err.getvalue())

        self.assertEqual(disp_elevate.dispatch(["status"]), 0)
        self.assertIn("asking sudo once", err.getvalue())
        self.assertEqual(host_nonroot.calls[0][0], ["sudo", "--", "env", "CST_NAME=cst", "/repo/cst", "status"])
        self.assertEqual(host_nonroot.execs, [host_nonroot.calls[0][0]], "sudo replaces cst (bash's exec)")

        box_port = _FakeDispatchPort(files=all_files, container=True, uid=1000, env={"CST_NAME": "cst"})
        disp_box, out, _ = self._dispatcher(box_port)
        self.assertEqual(disp_box.dispatch(["status"]), 0)
        self.assertIn("skipped: its status needs root on the host", out.getvalue())
        self.assertEqual(
            [cmd for cmd, _ in box_port.calls],
            [
                ["bash", "/repo/swap-setup.sh", "status"],
                ["bash", "/repo/bluetooth-setup.sh", "status"],
                ["bash", "/repo/toolchains-setup.sh", "status"],
                ["bash", "/repo/vm-setup.sh", "status"],
                ["python3", "-I", "-B", "/repo/doctor/cst-doctor", "--brief"],
            ],
        )
        self.assertEqual(box_port.execs, [], "the status of each module runs in the foreground")


class LocalCstDispatchAdapterTest(unittest.TestCase):
    """The real adapter: exit statuses as bash shows them."""

    def test_a_foreground_command_killed_by_a_signal_gives_128_plus_n(self) -> None:
        port = LocalCstDispatchAdapter()
        # run_passthrough hands its own stdin on (a terminal, as bash does); SHLVL keeps a socket
        # stdin from making bash read ~/.bashrc (AGENTS.md, "Test gotchas").
        env = {**_CLEAN_ENV, "SHLVL": "1"}
        self.assertEqual(port.run_passthrough(["bash", "-c", "kill -TERM $$"], env=env), 143)
        self.assertEqual(port.run_passthrough(["bash", "-c", "exit 3"], env=env), 3)

    def test_restore_lc_ctype(self) -> None:
        for handed_over, before, after in (
            (None, {"LC_CTYPE": "C.UTF-8"}, "C.UTF-8"),  # cli.py run without the wrapper: left alone
            ("", {"LC_CTYPE": "C.UTF-8"}, None),
            ("=C", {"LC_CTYPE": "C.UTF-8"}, "C"),
            ("=", {}, ""),
        ):
            env = before if handed_over is None else {**before, "CST_LC_CTYPE": handed_over}
            with (
                self.subTest(handed_over=handed_over),
                mock.patch.dict(os.environ, env, clear=True),
                mock.patch("cst_core.adapters.runtime.restore_default_signals"),
            ):
                undo_python_start()
                self.assertEqual(os.environ.get("LC_CTYPE"), after)
                self.assertNotIn("CST_LC_CTYPE", os.environ)

    def test_undo_python_start_gives_a_child_the_caller_s_lc_ctype(self) -> None:
        # A real Python under a C locale (no LC_ALL, LANG or LC_CTYPE: PEP 538 sets LC_CTYPE=C.UTF-8), and
        # the command it then runs.
        probe = (
            f"import subprocess, sys; sys.path.insert(0, {REPO_ROOT!r})\n"
            "from cst_core.adapters.runtime import undo_python_start\n"
            "undo_python_start()\n"
            "subprocess.run(['sh', '-c', 'echo \"${LC_CTYPE-unset} ${CST_LC_CTYPE-gone}\"'])\n"
        )
        for caller, seen in (
            ({}, "C.UTF-8 gone"),  # no wrapper: Python's own change stays (the case this undoes)
            ({"CST_LC_CTYPE": ""}, "unset gone"),
            ({"LC_CTYPE": "C", "CST_LC_CTYPE": "=C"}, "C gone"),
        ):
            with self.subTest(caller=caller):
                proc = subprocess.run(
                    [SYSTEM_PYTHON, "-I", "-B", "-c", probe],
                    env={"PATH": "/usr/bin:/bin", **caller},
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(proc.stdout, seen + "\n", proc.stderr)

    def test_lc_ctype_handover(self) -> None:
        # What `cst swap` puts in sudo's `env` when it starts itself again as root.
        for env, setting in (
            ({}, "CST_LC_CTYPE="),
            ({"LC_CTYPE": "C"}, "CST_LC_CTYPE==C"),
            ({"LC_CTYPE": ""}, "CST_LC_CTYPE=="),
        ):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(lc_ctype_handover(), setting)

    def test_a_command_that_cannot_start(self) -> None:
        port = LocalCstDispatchAdapter()
        missing = os.path.join(tempfile.gettempdir(), "cst-no-such-program")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(port.run_passthrough([missing]), 127)
            self.assertEqual(port.exec_replace([missing]), 127)
        self.assertEqual(err.getvalue(), f"{missing}: No such file or directory\n" * 2)


def _cst_copy(tmp: str, swap_setup: str) -> str:
    """A copy of the cst command (the wrapper and cst_core) in tmp, with swap-setup.sh as the only module."""
    shutil.copy2(os.path.join(REPO_ROOT, "cst"), os.path.join(tmp, "cst"))
    shutil.copytree(
        os.path.join(REPO_ROOT, "cst_core"),
        os.path.join(tmp, "cst_core"),
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    with open(os.path.join(tmp, "swap-setup.sh"), "w", encoding="utf-8") as fh:
        fh.write(swap_setup)
    return os.path.join(tmp, "cst")


class CstCommandTest(unittest.TestCase):
    """The real cst command, run from a scratch copy: exec, signals, pipes and the environment of a module."""

    tmp: str

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-cmd-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _run(
        self, swap_setup: str, *args: str, env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        cst = _cst_copy(self.tmp, swap_setup)
        return subprocess.run(
            [cst, *args],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env=dict(_CLEAN_ENV if env is None else env),
            timeout=30,
        )

    def test_a_module_killed_by_a_signal_kills_cst_with_it(self) -> None:
        # main ran `exec bash MODULE`: the module was the cst process (bash showed 143).
        proc = self._run("kill -TERM $$\n", "swap")
        self.assertEqual(proc.returncode, -signal.SIGTERM, proc.stderr)

    def test_the_module_replaces_cst(self) -> None:
        proc = self._run('echo "pid=$$ ppid=$PPID"\n', "swap")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The module runs as the process cst started as: its parent is this test.
        self.assertIn(f"ppid={os.getpid()}", proc.stdout)

    def test_ctrl_c_lets_the_module_finish_its_trap(self) -> None:
        cst = _cst_copy(
            self.tmp,
            # The sleep runs in the background (where Ctrl-C does not reach it) so the
            # trap runs at once wherever the signal lands; the trap ends it.
            "trap 'echo cleanup start; kill $s; sleep 0.3; echo cleanup done; exit 130' INT\n"
            "sleep 10 & s=$!\necho started\nwait $s\n",
        )
        proc = subprocess.Popen(
            [cst, "swap"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=dict(_CLEAN_ENV),
            start_new_session=True,
        )
        assert proc.stdout is not None
        self.assertEqual(proc.stdout.readline(), "started\n")
        os.killpg(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        self.assertIn("cleanup done", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(proc.returncode, 130)

    def test_status_waits_for_a_module_that_handles_ctrl_c(self) -> None:
        # status runs each module in the foreground, as bash did: Ctrl-C reaches the module, which may
        # handle it; cst neither prints a traceback nor kills the module.
        cst = _cst_copy(
            self.tmp,
            "trap 'echo cleanup start; kill $s; sleep 0.3; echo cleanup done' INT\n"
            "sleep 10 & s=$!\necho started\nwait $s\necho after\n",
        )
        proc = subprocess.Popen(
            [cst, "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=dict(_CLEAN_ENV),
            start_new_session=True,
        )
        assert proc.stdout is not None
        while proc.stdout.readline() not in ("started\n", ""):
            pass
        os.killpg(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        self.assertIn("cleanup done", out)
        self.assertIn("==== doctor", out)
        self.assertNotIn("Traceback", err)

    def _status_signalled(self, module: str, sig: int) -> tuple[str, str, int]:
        """cst status, with sig sent to its whole process group (as the terminal sends Ctrl-C) once the module started."""
        proc = subprocess.Popen(
            [_cst_copy(self.tmp, module), "status"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=dict(_CLEAN_ENV),
            start_new_session=True,
        )
        assert proc.stdout is not None
        while proc.stdout.readline() not in ("started\n", ""):
            pass
        os.killpg(proc.pid, sig)
        out, err = proc.communicate(timeout=30)
        return out, err, proc.returncode

    def test_status_stops_when_a_module_dies_of_ctrl_c(self) -> None:
        out, err, rc = self._status_signalled("echo started\nsleep 10\n", signal.SIGINT)
        self.assertNotIn("==== doctor", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(rc, -signal.SIGINT)

    def test_status_goes_on_after_ctrl_backslash(self) -> None:
        # bash ignores SIGQUIT itself: a module killed by Ctrl-\ failed, and main's status went on.
        # (bash ignores it too until the exec: the module says "started" from the program it became.)
        module = "exec /usr/bin/python3 -I -c 'import time; print(\"started\", flush=True); time.sleep(10)'\n"
        out, err, rc = self._status_signalled(module, signal.SIGQUIT)
        self.assertIn("==== doctor", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(rc, 1)

    def test_status_ignores_ctrl_backslash_between_two_modules(self) -> None:
        # bash ignored SIGQUIT the whole time, also between two modules. A small stdout pipe that the
        # test fills while the module runs holds cst in the next header, after the module ended.
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        fcntl.fcntl(w, fcntl.F_SETPIPE_SZ, 4096)
        proc = subprocess.Popen(
            # No core file when it dies of the signal; SIGQUIT at its default, whatever this test got.
            [
                "bash",
                "-c",
                'ulimit -c 0; exec env --default-signal=QUIT "$0" status',
                _cst_copy(self.tmp, 'echo "module $$"\nsleep 0.5\n'),
            ],
            stdout=w,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=dict(_CLEAN_ENV),
            start_new_session=True,
        )
        assert proc.stderr is not None
        self.addCleanup(proc.stderr.close)
        self.addCleanup(proc.wait, 30)
        self.addCleanup(proc.kill)  # cleanups run last first: kill, wait, close
        out = b""
        while not (started := re.search(rb"^module (\d+)$", out, re.M)):
            self.assertTrue(select.select([r], [], [], 10)[0], f"no module line: {out!r}")
            out += os.read(r, 4096)
        # The module sleeps, cst waits for it: neither writes while the pipe is non-blocking.
        os.set_blocking(w, False)
        with contextlib.suppress(BlockingIOError):
            while True:
                os.write(w, b"x")
        os.set_blocking(w, True)
        os.close(w)
        module_pid = int(started.group(1))
        deadline = time.monotonic() + 10
        while os.path.exists(f"/proc/{module_pid}") and time.monotonic() < deadline:  # until cst reaped it
            time.sleep(0.01)
        time.sleep(0.2)
        os.kill(proc.pid, signal.SIGQUIT)
        time.sleep(0.2)
        while chunk := os.read(r, 65536):
            out += chunk
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 1, err)  # 1: this copy has no doctor
        self.assertIn(b"==== doctor", out)
        self.assertNotIn(b"Traceback", err)

    def test_programs_cst_starts_get_sigquit_at_its_default(self) -> None:
        # cst ignores SIGQUIT with a handler, not SIG_IGN, which a module would inherit across exec.
        module = "grep '^SigIgn:' /proc/self/status\n"
        for args in (["status"], ["swap"]):
            with self.subTest(args=args):
                proc = subprocess.run(
                    ["env", "--default-signal=INT,QUIT", _cst_copy(self.tmp, module), *args],
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    env=dict(_CLEAN_ENV),
                    check=False,
                    timeout=30,
                )
                lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("SigIgn:")]
                self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
                ignored = int(lines[0].split()[1], 16)
                for sig in (signal.SIGQUIT, signal.SIGINT):
                    self.assertFalse(ignored & (1 << (sig - 1)), f"{sig.name} ignored in a module: {lines[0]}")

    def test_status_goes_on_when_only_the_module_got_ctrl_c(self) -> None:
        # bash dies with a foreground command's SIGINT only when it got the signal too (Ctrl-C reaches
        # both); a SIGINT sent to the module alone is that module's failure, and main's status went on.
        proc = self._run("echo started\nkill -INT $$\necho not reached\n", "status")
        self.assertIn("started", proc.stdout)
        self.assertNotIn("not reached", proc.stdout)
        self.assertIn("==== doctor", proc.stdout)
        self.assertEqual(proc.returncode, 1, proc.stderr)

    def test_ctrl_c_ignored_by_cst_s_caller_stays_ignored(self) -> None:
        # bash keeps a signal ignored at its start ignored for the commands it runs (`nohup`-style callers).
        cst = _cst_copy(self.tmp, "echo started\nsleep 0.5\necho after\n")
        proc = subprocess.Popen(
            ["bash", "-c", "trap '' INT; exec \"$0\" status", cst],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env=dict(_CLEAN_ENV),
            start_new_session=True,
        )
        assert proc.stdout is not None
        while proc.stdout.readline() not in ("started\n", ""):
            pass
        os.killpg(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=30)
        self.assertIn("after", out)
        self.assertIn("==== doctor", out)
        self.assertNotIn("Traceback", err)

    def test_help_into_a_closed_pipe_stops_quietly(self) -> None:
        cst = _cst_copy(self.tmp, "")
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            proc = subprocess.run(
                [cst, "help"],
                stdout=write_end,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                check=False,
                env=dict(_CLEAN_ENV),
                timeout=30,
            )
        finally:
            os.close(write_end)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(proc.returncode, -signal.SIGPIPE)

    def test_a_module_starts_with_sigpipe_at_its_default(self) -> None:
        # Python ignores SIGPIPE; an exec'd module must not inherit that (bash cannot undo an ignored signal).
        proc = self._run('yes | head -n 1\necho "pipe=${PIPESTATUS[*]}"\n', "swap")
        self.assertIn("pipe=141 0", proc.stdout)

    def test_a_module_sees_only_cst_prog_from_cst(self) -> None:
        script = (
            'echo "PROG=$CST_PROG NAME=${CST_NAME-unset} SELF=${CST_SELF-unset} DIR=${CST_DIR-unset}'
            ' ARG0=${CST_ARG0-unset} LC=${CST_LC_CTYPE-unset}"\n'
        )
        proc = self._run(script, "swap")
        cst = os.path.join(self.tmp, "cst")
        unset = "SELF=unset DIR=unset ARG0=unset LC=unset"
        self.assertEqual(proc.stdout, f"PROG={cst} swap NAME=unset {unset}\n", proc.stderr)
        # A name the caller set (sudo's `env CST_NAME=...`) is kept, as before.
        proc = self._run(script, "swap", env={**_CLEAN_ENV, "CST_NAME": "cst"})
        self.assertEqual(proc.stdout, f"PROG=cst swap NAME=cst {unset}\n", proc.stderr)

    def test_a_module_gets_the_caller_s_lc_ctype(self) -> None:
        # Under a C locale Python sets LC_CTYPE=C.UTF-8 in its own environment (PEP 538); the bash cst
        # handed the caller's LC_CTYPE on as it was, unset included.
        script = 'echo "LC_CTYPE=${LC_CTYPE-unset}"\n'
        path = {"PATH": "/usr/bin:/bin"}
        for caller, seen in (
            ({}, "unset"),
            ({"LANG": "C"}, "unset"),
            ({"LC_CTYPE": "C"}, "C"),
            ({"LC_CTYPE": ""}, ""),
            ({"LC_CTYPE": "C.UTF-8"}, "C.UTF-8"),
        ):
            with self.subTest(caller=caller):
                proc = self._run(script, "swap", env={**path, **caller})
                self.assertEqual(proc.stdout, f"LC_CTYPE={seen}\n", proc.stderr)


class OstreeQueryTest(unittest.TestCase):
    """cst_core.domain.ostree: the one `rpm-ostree status --json` query, in the words doctor always used."""

    @staticmethod
    def _answer(rc: int, out: str, err: str = "") -> tuple[list[list[str]], OstreeStatus | str]:
        calls: list[list[str]] = []

        def run(argv: list[str]) -> tuple[int, str, str]:
            calls.append(argv)
            return rc, out, err

        return calls, query_status(run)

    def test_the_query_and_its_failures(self) -> None:
        calls, got = self._answer(1, "", "error: no daemon\nmore\n")
        self.assertEqual(calls, [["rpm-ostree", "status", "--json"]])
        self.assertEqual(got, "rpm-ostree status --json failed: error: no daemon")
        self.assertEqual(self._answer(3, "")[1], "rpm-ostree status --json failed: exit 3")
        prefix = "rpm-ostree status --json: unreadable output "
        for out, why in (
            ("{", "(Expecting property name enclosed in double quotes: line 1 column 2 (char 1))"),
            ("{}", "('deployments')"),
            ("[]", "(list indices must be integers or slices, not str)"),
            ('"x"', "(string indices must be integers, not 'str')"),
            ('{"deployments": null}', "('NoneType' object is not iterable)"),
            ('{"deployments": 5}', "('int' object is not iterable)"),
        ):
            with self.subTest(out=out):
                self.assertEqual(self._answer(0, out)[1], prefix + why)

    def test_booted_and_pending(self) -> None:
        booted = {"booted": True, "osname": "fedora", "checksum": "b"}
        staged = {"osname": "fedora", "checksum": "s", "staged": True}
        other = {"osname": "other", "checksum": "o"}
        rollback = {"osname": "fedora", "checksum": "r"}
        cases = (
            ([staged, booted], staged),  # staged: listed first
            ([rollback, booted], rollback),  # after rpm-ostree rollback: first, not staged
            ([other, booted, rollback], None),  # another osname first: the booted one is the next boot
            ([booted, rollback], None),
        )
        for deps, pending in cases:
            with self.subTest(deps=deps):
                got = self._answer(0, json.dumps({"deployments": ["junk", *deps, 3]}))[1]
                assert isinstance(got, OstreeStatus), got
                self.assertEqual(got.deployments, deps, "only the objects, in order")
                self.assertEqual(got.booted, booted)
                self.assertEqual(got.pending, pending)
        got = self._answer(0, json.dumps({"deployments": [staged, rollback]}))[1]
        assert isinstance(got, OstreeStatus), got
        self.assertEqual((got.booted, got.pending), (None, staged), "no booted one: the first of any osname")
        got = self._answer(0, '{"deployments": []}')[1]
        self.assertEqual(got, OstreeStatus([], None, None))


class HostDetectionTest(unittest.TestCase):
    """One host-or-toolbox detection (HostExecutionContext), injected where a module asks."""

    class _Probe:
        def __init__(self, *, container: bool) -> None:
            self.container = container

        def is_container(self) -> bool:
            return self.container

    def test_the_dispatcher_asks_the_probe(self) -> None:
        for container in (True, False):
            self.assertIs(LocalCstDispatchAdapter(self._Probe(container=container)).is_container(), container)
        markers = {"/run/.containerenv"}
        ctx = HostExecutionContext(exists=lambda p: p in markers)
        self.assertTrue(LocalCstDispatchAdapter(ctx).is_container())
        markers.clear()
        self.assertFalse(LocalCstDispatchAdapter(ctx).is_container())


class _FakeRoutePort:
    """ModuleRoutePort over plain values: what the router says and execs is recorded."""

    def __init__(
        self,
        *,
        euid: int = 1000,
        container: bool = False,
        spawn: bool = True,
        env: Mapping[str, str] | None = None,
        script_text: str = "#!/bin/bash\n# SPDX-License-Identifier: MPL-2.0\n#\n# x-setup.sh  help\n\nset -e\n",
    ) -> None:
        self._euid, self._container, self._spawn, self._text = euid, container, spawn, script_text
        self._env = {"PATH": "/usr/bin"} if env is None else dict(env)
        self.said: list[tuple[str, str]] = []
        self.execs: list[tuple[list[str], dict[str, str] | None]] = []

    def environ(self) -> Mapping[str, str]:
        return self._env

    def euid(self) -> int:
        return self._euid

    def is_container(self) -> bool:
        return self._container

    def user_passwd(self, user: str) -> tuple[int, str]:
        if user != "alice":
            raise KeyError(user)
        return 1234, "/home/alice"

    def exec_on_host(self, argv: Sequence[str], *, settings: Mapping[str, str]) -> int | None:
        if not self._spawn:
            return None
        self.execs.append((["flatpak-spawn", *argv], dict(settings)))
        return 0

    def read_text(self, path: str) -> str:
        return self._text

    def say(self, stream: Literal["out", "err"], text: str) -> None:
        self.said.append((stream, text))

    def exec_replace(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        self.execs.append((list(argv), None if env is None else dict(env)))
        return 0


class ModuleRouterTest(unittest.TestCase):
    """cst_core's routing table and ModuleRouter (what vm-setup.sh and toolchains-setup.sh run)."""

    @staticmethod
    def _router(
        module: str, port: _FakeRoutePort, *, cst_prog: str = "cst x", container: str = "", sudo_user: str = ""
    ) -> ModuleRouter:
        settings = RouteSettings(
            script="/c/x-setup.sh", arg0="./x-setup.sh", cst_prog=cst_prog, container=container, sudo_user=sudo_user
        )
        return ModuleRouter(port=port, routing=ROUTING[module], settings=settings)

    def test_route_command(self) -> None:
        vm = ROUTING["vm"]
        self.assertEqual(route_command(vm, "help", [], "p").action, "help")
        self.assertEqual(route_command(vm, "nope", [], "p"), Routed("unknown", message="Unknown command: nope"))
        self.assertEqual(
            route_command(vm, "tune", ["x"], "cst vm"),
            Routed("usage", message="Usage: cst vm tune plan|apply [VM] [--guest-has-virtio]"),
        )
        for args, privilege in (
            (["list"], "read"),
            (["tune", "plan"], "read"),
            (["tune", "apply"], "root"),
            (["nocow", "plan"], "read"),
            (["nocow", "plan", "w", "--root"], "root"),
            (["nocow", "apply", "--root"], "user"),
            (["backup-timer", "status"], "read"),
            (["backup-timer", "off"], "root"),
            (["backup"], "root"),
        ):
            with self.subTest(args=args):
                self.assertEqual(route_command(vm, args[0], args[1:], "p"), Routed("run", privilege))
        for cmd in ("status", "prune", "auto", "config"):
            self.assertEqual(route_command(ROUTING["toolchains"], cmd, ["x"], "p"), Routed("run", "as-user"))
        self.assertEqual(route_command(ROUTING["toolchains"], "list", [], "p").action, "unknown")

    def test_script_help_is_what_the_header_sed_printed(self) -> None:
        text = "#!/x\n# SPDX-License-Identifier: MPL-2.0\n#\n# a  b\n#c\n#   d\nnot a comment\n\nlater\n"
        self.assertEqual(script_help(text, "p"), "\na  b\nc\n  d\nnot a comment\n\nUsage: p <command> [args]\n")
        self.assertEqual(
            script_help("#!/x\n# one", "p"), "one\nUsage: p <command> [args]\n", "no empty line: to the end"
        )
        # The header runs to the end of a file that ends with a newline: no blank line before Usage (as sed).
        self.assertEqual(script_help("#!/x\n# one\n# two\n", "p"), "one\ntwo\nUsage: p <command> [args]\n")
        self.assertEqual(script_help("#!/x\n# one\n\n", "p"), "one\n\nUsage: p <command> [args]\n")
        for module in ROUTING:
            script = os.path.join(REPO_ROOT, f"{module}-setup.sh")
            sed = subprocess.run(
                [
                    "bash",
                    "-c",
                    "sed -n '2,/^$/{/SPDX-License-Identifier:/!p;}' \"$1\" | sed 's/^# \\{0,1\\}//'",
                    "_",
                    script,
                ],
                capture_output=True,
                text=True,
                check=True,
                stdin=subprocess.DEVNULL,
            )
            with open(script, encoding="utf-8") as fh:
                self.assertEqual(script_help(fh.read(), "q"), sed.stdout + "Usage: q <command> [args]\n")

    def test_help_unknown_and_usage(self) -> None:
        port = _FakeRoutePort()
        self.assertEqual(self._router("vm", port).route([]), 0)
        self.assertEqual(port.said, [("out", "\nx-setup.sh  help\n\nUsage: cst x <command> [args]\n")])
        port = _FakeRoutePort()
        self.assertEqual(self._router("vm", port, cst_prog="").route(["nope"]), 2)
        self.assertEqual(port.said[0][1].split("\n", 1)[0], "Unknown command: nope")
        self.assertTrue(port.said[0][1].endswith("Usage: ./x-setup.sh <command> [args]\n"))
        port = _FakeRoutePort(container=True)
        self.assertEqual(self._router("vm", port).route(["backup-timer", ""]), 2)
        self.assertEqual(port.said, [("err", "Usage: cst x backup-timer on|off|status [VM]\n")])
        self.assertEqual(port.execs, [])

    def test_read_runs_the_program_or_spawns_it_on_the_host(self) -> None:
        program = ["python3", "-I", "-B", "/c/vm/cst-vm", "status", "w"]
        port = _FakeRoutePort(env={"PATH": "/p", "A": "1"})
        self._router("vm", port).route(["status", "w"])
        self.assertEqual(port.execs, [(program, {"PATH": "/p", "A": "1", "CST_PROG": "cst x"})])
        for container, detected in (("1", False), ("", True)):
            port = _FakeRoutePort(container=detected)
            self._router("vm", port, container=container).route(["status", "w"])
            self.assertEqual(port.execs, [(["flatpak-spawn", *program], {"CST_PROG": "cst x"})])
        port = _FakeRoutePort(container=True, spawn=False)
        self._router("vm", port).route(["status", "w"])
        self.assertEqual(
            port.said, [("out", "(cannot reach the host from this container: showing what is visible here)\n")]
        )
        self.assertEqual(port.execs, [(program, {"PATH": "/usr/bin", "CST_PROG": "cst x"})])
        port = _FakeRoutePort(container=True)
        self._router("vm", port, container="0").route(["status", "w"])
        self.assertEqual(port.execs[0][0], program, "the override says host")

    def test_changing_commands(self) -> None:
        port = _FakeRoutePort(container=True)
        self.assertEqual(self._router("vm", port).route(["nocow", "apply"]), 1)
        self.assertEqual(
            port.said,
            [
                (
                    "err",
                    "'cst x nocow apply' must run on the host, not inside a toolbox/container.\n"
                    "In a host terminal: cst x nocow apply\n",
                )
            ],
        )
        port = _FakeRoutePort()
        self._router("vm", port).route(["backup", "w"])
        self.assertEqual(port.execs, [(["sudo", "--", "env", "CST_PROG=cst x", "/c/x-setup.sh", "backup", "w"], None)])
        port = _FakeRoutePort()
        self._router("vm", port, cst_prog="").route(["backup"])
        self.assertEqual(port.execs, [(["sudo", "--", "/c/x-setup.sh", "backup"], None)])
        port = _FakeRoutePort(euid=0)
        self._router("vm", port, cst_prog="").route(["backup"])
        self.assertEqual(
            port.execs,
            [(["python3", "-I", "-B", "/c/vm/cst-vm", "backup"], {"PATH": "/usr/bin", "CST_PROG": "./x-setup.sh"})],
        )
        port = _FakeRoutePort()
        self._router("vm", port).route(["nocow", "apply"])
        self.assertEqual(port.execs[0][0][:4], ["python3", "-I", "-B", "/c/vm/cst-vm"], "user: no sudo")

    def test_as_user(self) -> None:
        port = _FakeRoutePort()
        self._router("toolchains", port).route(["status"])
        self.assertEqual(
            port.execs,
            [
                (
                    ["python3", "-I", "-B", "/c/toolchains/cst-toolchains", "status"],
                    {"PATH": "/usr/bin", "CST_PROG": "cst x"},
                )
            ],
        )
        port = _FakeRoutePort(euid=0)
        self._router("toolchains", port, sudo_user="alice").route(["prune", "plan"])
        self.assertEqual(
            port.execs,
            [
                (
                    [
                        "runuser", "-u", "alice", "--", "env", "-u", "XDG_CONFIG_HOME", "-u", "SUDO_USER",
                        "HOME=/home/alice", "USER=alice", "LOGNAME=alice", "XDG_RUNTIME_DIR=/run/user/1234",
                        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1234/bus", "CST_PROG=cst x",
                        "/c/x-setup.sh", "prune", "plan",
                    ],
                    None,
                )
            ],
        )  # fmt: skip
        for user in ("", "root"):
            port = _FakeRoutePort(euid=0)
            self.assertEqual(self._router("toolchains", port, sudo_user=user).route(["status"]), 1)
            self.assertEqual(
                port.said,
                [
                    (
                        "err",
                        "cst x: ERROR: run it from your user account (needs SUDO_USER): "
                        "toolchain caches and settings belong to your user\n",
                    )
                ],
            )
        port = _FakeRoutePort(euid=0)
        self.assertEqual(self._router("toolchains", port, sudo_user="bob").route(["status"]), 1)
        self.assertEqual(port.said, [("err", "cst x: ERROR: SUDO_USER=bob: no such user\n")])
        self.assertEqual(port.execs, [])

    def test_the_router_starts_with_ctrl_c_as_bash_had_it(self) -> None:
        for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGPIPE, signal.SIGXFSZ):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        for before, after in ((signal.default_int_handler, signal.SIG_DFL), (signal.SIG_IGN, signal.SIG_IGN)):
            signal.signal(signal.SIGINT, before)
            with mock.patch.dict(os.environ, {"CST_LC_CTYPE": ""}):
                core_wiring.start_router()
            self.assertIs(signal.getsignal(signal.SIGINT), after)
        self.assertIs(signal.getsignal(signal.SIGPIPE), signal.SIG_DFL)

    def test_the_adapter_keeps_every_byte_and_spawns_only_with_flatpak_spawn(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-route-adapter-")
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "s")
        with open(path, "wb") as fh:
            fh.write(b"caf\xe9 \xc3\xa9\n")
        port = LocalCstDispatchAdapter()
        text = port.read_text(path)
        self.assertEqual(os.fsencode(text), b"caf\xe9 \xc3\xa9\n")
        self.assertEqual(on_host_argv(["a", "b"], {"K": "v w"}), ["flatpak-spawn", "--host", "--env=K=v w", "a", "b"])
        self.assertFalse(can_spawn_on_host(tmp))
        os.symlink("/bin/true", os.path.join(tmp, "flatpak-spawn"))
        self.assertTrue(can_spawn_on_host(tmp))
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertIsNone(port.exec_on_host(["x"], settings={}))


class CoreWiringTest(unittest.TestCase):
    """cst_core/wiring.py: what cst and the installer take from the process."""

    def test_the_router_needs_the_shim_it_routes_for(self) -> None:
        # Without CST_SELF (route.py run by hand, not by vm-setup.sh or toolchains-setup.sh) there is
        # no script to print the help of or to run again: a usage error, not a traceback or ./vm/cst-vm.
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}, clear=True):
            self.assertIsNone(core_wiring.module_router("vm"))
        env = {"PATH": "/usr/bin:/bin"}
        for args in (["vm", "help"], ["toolchains", "status"], ["nope"], []):
            with self.subTest(args=args):
                p = subprocess.run(
                    [sys.executable, "-I", "-B", os.path.join(REPO_ROOT, "cst_core", "route.py"), *args],
                    env=env,
                    cwd=tempfile.gettempdir(),
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    check=False,
                )
                self.assertEqual((p.returncode, p.stdout), (2, ""), p.stderr)
                self.assertTrue(p.stderr.startswith("usage: route.py MODULE [ARGS...]"), p.stderr)

    def test_the_installer_builds_payloads_from_this_checkout(self) -> None:
        self.assertIsNotNone(core_wiring.install_service())

    def test_the_dispatcher_takes_and_removes_what_the_wrapper_hands_over(self) -> None:
        handed = {"CST_DIR": "/nonexistent-dir", "CST_SELF": "/nonexistent-dir/cst", "CST_ARG0": "./cst"}
        err = io.StringIO()
        with mock.patch.dict(os.environ, {**handed, "PATH": "/nonexistent"}, clear=True):
            dispatcher = core_wiring.cst_dispatcher(
                fallback_root="/fallback",
                stdout=io.StringIO(),
                stderr=err,
            )
            self.assertEqual(dict(os.environ), {"PATH": "/nonexistent"}, "the modules do not inherit them")
        self.assertEqual(dispatcher.dispatch(["nope"]), 2)
        self.assertTrue(err.getvalue().startswith("./cst: unknown command or module 'nope'\n"), err.getvalue())
        self.assertIn("vm         the libvirt VM", err.getvalue())
        self.assertIn("(not in this copy)", err.getvalue(), "CST_DIR names the folder of the modules")

    def test_without_the_wrapper_the_checkout_of_cli_py_and_argv0(self) -> None:
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"PATH": "/nonexistent"}, clear=True),
            mock.patch.object(sys, "argv", ["/some/cst-link"]),
        ):
            dispatcher = core_wiring.cst_dispatcher(
                fallback_root="/fallback",
                stdout=out,
                stderr=io.StringIO(),
            )
        self.assertEqual(dispatcher.dispatch(["help"]), 0)
        self.assertNotIn("(not in this copy)", out.getvalue(), "this checkout holds every module")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}, clear=True), mock.patch.object(sys, "argv", []):
            core_wiring.cst_dispatcher(fallback_root="/fallback", stdout=io.StringIO(), stderr=err).dispatch(["nope"])
        self.assertTrue(err.getvalue().startswith("cst: unknown command or module 'nope'\n"), err.getvalue())


class StateContractsTest(unittest.TestCase):
    """cst_core/domain/contracts.py: every shared state format, and the programs that use its paths."""

    KEYS: Final = ("alert-spool", "stall-records", "stall-queue", "bulk-journal", "docker-gc-state", "docker-gc-last-run",
            "vm-index", "install-record", "game-state", "game-intended")  # fmt: skip

    def test_every_format_names_real_writers_and_readers(self) -> None:
        for key in self.KEYS:
            f = state_format(key)
            with self.subTest(key):
                self.assertEqual(f.key, key)
                self.assertTrue(f.path.startswith("/") and f.folder and f.file_name, f.path)
                self.assertGreaterEqual(f.version, 0)
                self.assertTrue(f.writers and f.readers)
                for rel in f.writers + f.readers:
                    self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, rel)), rel)

    def test_a_version_is_a_whole_number_and_none_is_version_0(self) -> None:
        self.assertEqual(
            [version_of({}, "v"), version_of({"v": 3}, "v"), version_of({"version": 1}, "version")], [0, 3, 1]
        )
        for bad in ({"v": True}, {"v": "1"}, {"v": -1}, {"v": 1.0}, [], "text", None):
            with self.subTest(bad=bad):
                self.assertIsNone(version_of(bad, "v"))
        self.assertEqual(
            state_format("vm-index").newer_text("/x.json", 2),
            "/x.json is format 2 of the vm-index (this CST reads format 1 and older): written by a newer CST",
        )

    def test_the_programs_use_the_registered_paths(self) -> None:
        from btrfs.bulk.domain import BulkPaths
        from btrfs.docker_gc.domain import GcPaths
        from btrfs.stall_monitord.domain import VERSION
        from btrfs.stall_report.adapters import LOG_DIR
        from cpu.domain import GAME_STATE
        from cst_core.domain.record import ROOT_RECORD
        from doctor.domain import ALERT_PENDING, GAME_INTENDED, VM_INDEX

        self.assertEqual(GcPaths().last_used, state_format("docker-gc-state").path)
        self.assertEqual(GcPaths().last_run, state_format("docker-gc-last-run").path)
        self.assertEqual(BulkPaths().state, state_format("bulk-journal").folder)
        self.assertEqual(
            (VERSION, LOG_DIR), (state_format("stall-records").version, state_format("stall-records").folder)
        )
        self.assertEqual(GAME_STATE, state_format("game-state").path)
        self.assertEqual(GAME_INTENDED, state_format("game-intended").path)
        self.assertEqual(ALERT_PENDING, state_format("alert-spool").folder)
        self.assertEqual(VM_INDEX, state_format("vm-index").path)
        self.assertEqual(ROOT_RECORD, state_format("install-record").path)

    def test_paths_of_installed_programs_come_from_the_manifest(self) -> None:
        from btrfs.bulk.domain import BulkPaths
        from btrfs.docker_volumes_nocow.domain import NocowPaths
        from doctor.domain import GAME_PROFILE_HOST, HEALTH_CHECK_HOST, NOTIFY_HOST
        from toolchains.domain.common import DEFAULTS_REL, PROG_REL
        from toolchains.domain.config import WRAPPER_REL

        dest = {e.src: e.dest for e in INSTALL_MANIFEST}
        notify = dest["btrfs/btrfs-notify"]
        self.assertEqual(notify, "/usr/local/sbin/btrfs-notify")
        self.assertEqual((BulkPaths().notify, NocowPaths().notify, NOTIFY_HOST), (notify, notify, notify))
        self.assertEqual(HEALTH_CHECK_HOST, dest["btrfs/btrfs-health-check"])
        self.assertEqual(GAME_PROFILE_HOST, dest["cpu/game-profile"])
        self.assertEqual(
            ("~/" + PROG_REL, "~/" + DEFAULTS_REL, "~/" + WRAPPER_REL),
            (dest["toolchains/cst-toolchains"], dest["toolchains/toolchains.conf"], dest["toolchains/rustc-wrapper"]),
        )


if __name__ == "__main__":
    unittest.main()

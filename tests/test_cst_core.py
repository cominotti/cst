"""Unit tests for the cst_core Shared Kernel and base adapters."""

from collections.abc import Callable, Mapping, Sequence
import io
import os
import shutil
import stat
import sys
import tempfile
import unittest

from cst_core.adapters import (
    HostExecutionContext,
    LocalFileSystemAdapter,
    SubprocessCommandRunner,
    SystemClockAdapter,
    SystemctlAdapter,
)
from cst_core.cli import CstDispatcher
from cst_core.domain import (
    ByteSize,
    CpuSet,
    DomainValidationError,
    Duration,
    Err,
    MODULES,
    MarkedLineSpec,
    OctalMode,
    Ok,
    Outcome,
    PlanItem,
    effective_lines,
    format_bytes_decimal,
    format_bytes_gib_mib,
    format_bytes_human,
    format_cst_usage,
    inspect_managed_file,
)
from cst_core.ports import CommandFailed, CommandResult


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


class PlanAndOutcomeTest(unittest.TestCase):
    def test_plan_item_and_outcome(self) -> None:
        row = PlanItem("ok", "crypttab: swap0 line", "up to date")
        self.assertEqual(row.format_line(), "  ok        crypttab: swap0 line                   up to date")
        self.assertEqual(PlanItem("ok", "zram0").format_line(), "  ok        zram0")

        ok: Outcome[int, str] = Ok(42)
        err: Outcome[int, str] = Err("boom")
        self.assertEqual(ok.value, 42)
        self.assertEqual(err.error, "boom")


class AdaptersTest(unittest.TestCase):
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
        self.assertTrue(sysd.start("systemd-cryptsetup@swap0.service"))
        self.assertTrue(sysd.restart("systemd-cryptsetup@swap0.service"))
        self.assertFalse(sysd.stop("dev-zram0.swap"))
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
    ) -> None:
        self.files = set() if files is None else files
        self.container = container
        self.uid = uid
        self.env = {} if env is None else dict(env)
        self.passwd = {} if passwd is None else dict(passwd)
        self.handler = handler
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

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

    def run_passthrough(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        cmd = list(argv)
        copied_env = None if env is None else dict(env)
        self.calls.append((cmd, copied_env))
        if self.handler is not None:
            return self.handler(cmd, copied_env)
        return 0


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

        self.assertEqual(dispatcher.dispatch(["nope"]), 2)
        self.assertIn("cst: unknown command or module 'nope'", err.getvalue())
        self.assertIn("Modules here:", err.getvalue())

    def test_module_dispatch_and_missing_module(self) -> None:
        port = _FakeDispatchPort(files={"/repo/swap-setup.sh"}, env={"CST_NAME": "cst"})
        dispatcher, _, err = self._dispatcher(port)

        self.assertEqual(dispatcher.dispatch(["swap", "plan", "--root"]), 0)
        cmd, env = port.calls[0]
        self.assertEqual(cmd, ["bash", "/repo/swap-setup.sh", "plan", "--root"])
        assert env is not None
        self.assertEqual(env["CST_PROG"], "cst swap")

        self.assertEqual(dispatcher.dispatch(["vm", "status"]), 1)
        self.assertIn("cst: the 'vm' module is not in this copy (/repo/vm-setup.sh is missing)", err.getvalue())

    def test_doctor_as_user_and_via_sudo_user(self) -> None:
        doc = "/repo/doctor/cst-doctor"
        user_port = _FakeDispatchPort(files={doc}, uid=1000, env={"CST_NAME": "cst"})
        user_disp, _, _ = self._dispatcher(user_port)
        self.assertEqual(user_disp.dispatch(["doctor", "--brief"]), 0)
        self.assertEqual(user_port.calls[0][0], ["python3", "-I", doc, "--brief"])
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
                ["python3", "-I", "/repo/doctor/cst-doctor", "--brief"],
            ],
        )


if __name__ == "__main__":
    unittest.main()

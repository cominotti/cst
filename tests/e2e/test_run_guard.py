# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the tier 2 opt-in guard in tests/e2e/run.py.

These never start Docker: the tier runners are replaced by stubs that fail
the test if the guard lets a run through when it should not.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
from typing import Protocol, cast, override
import unittest
from unittest import mock

RUN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")


class _E2ERunModule(Protocol):
    run_tier1: Callable[..., bool]
    run_tier2: Callable[..., bool]
    main: Callable[[Sequence[str] | None], int]
    checkout_preflight: Callable[..., str | None]
    host_lsm_security_opts: Callable[..., list[str]]
    selinux_type: Callable[[Path], str | None]
    host_selinux: Callable[..., bool]
    run: Callable[..., object]
    docker_build: Callable[..., None]
    wait_systemd_ready: Callable[..., None]
    load_suite: Callable[[str], unittest.TestSuite]
    dump_logs: Callable[..., None]

    def checkout_label_problem(
        self, *, enforcing: bool, types: Mapping[str, str | None], repo: Path, label_disabled: bool = False
    ) -> str | None: ...


def load_run() -> _E2ERunModule:
    spec = importlib.util.spec_from_file_location("cst_e2e_run", RUN_PY)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return cast("_E2ERunModule", cast("object", mod))


class PrivilegedGuardTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.mod = load_run()
        self.calls: list[int] = []
        self.mod.run_tier1 = lambda *a, **k: self.calls.append(1) or True
        self.mod.run_tier2 = lambda *a, **k: self.calls.append(2) or True
        self.mod.checkout_preflight = lambda *a, **k: None  # the machine's labels are tested below
        env = {k: v for k, v in os.environ.items() if k != "CST_E2E_ALLOW_PRIVILEGED"}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *argv: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = self.mod.main(list(argv))
        return rc, err.getvalue()

    def test_tier2_refused_without_opt_in(self) -> None:
        for argv in (("--tier", "2"), ("--tier", "1", "--tier", "2"), ("--tier", "2", "--check-only")):
            rc, err = self.main(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("--privileged", err)
            self.assertIn("block", err)
            self.assertIn("CST_E2E_ALLOW_PRIVILEGED=1", err)
        self.assertEqual(self.calls, [])

    def test_env_values_other_than_1_do_not_opt_in(self) -> None:
        for value in ("", "0", "true", "yes"):
            os.environ["CST_E2E_ALLOW_PRIVILEGED"] = value
            rc, _ = self.main("--tier", "2")
            self.assertEqual(rc, 2, value)
        self.assertEqual(self.calls, [])

    def test_tier2_allowed_by_env_or_flag(self) -> None:
        os.environ["CST_E2E_ALLOW_PRIVILEGED"] = "1"
        self.assertEqual(self.main("--tier", "2")[0], 0)
        del os.environ["CST_E2E_ALLOW_PRIVILEGED"]
        self.assertEqual(self.main("--tier", "2", "--allow-privileged")[0], 0)
        self.assertEqual(self.calls, [2, 2])

    def test_tier1_needs_no_opt_in(self) -> None:
        self.assertEqual(self.main("--tier", "1")[0], 0)
        self.assertEqual(self.calls, [1])

    def test_check_only_runs_nothing(self) -> None:
        self.assertEqual(self.main("--tier", "1", "--tier", "2", "--allow-privileged", "--check-only")[0], 0)
        self.assertEqual(self.main("--tier", "1", "--check-only")[0], 0)
        self.assertEqual(self.calls, [])


class CheckoutPreflightTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.mod = load_run()

    def test_labels(self) -> None:
        repo = Path("/w/cst-wt")
        check = self.mod.checkout_label_problem
        self.assertIsNone(
            check(enforcing=False, types={"/w/cst-wt": "user_home_t"}, repo=repo), "SELinux not enforcing"
        )
        self.assertIsNone(
            check(
                enforcing=True, types={"/w/cst-wt": "container_file_t", "/w/cst-wt/cst": "container_file_t"}, repo=repo
            )
        )
        self.assertIsNone(check(enforcing=True, types={"/w/cst-wt": None}, repo=repo), "no label at all")
        msg = check(enforcing=True, types={"/w/cst-wt": "user_home_t", "/w/cst-wt/cst": "container_file_t"}, repo=repo)
        assert msg is not None
        self.assertIn("/w/cst-wt (user_home_t)", msg)
        self.assertIn("chcon -R -t container_file_t /w/cst-wt", msg)
        # Containers started with label=disable (host_selinux) read any label.
        self.assertIsNone(check(enforcing=True, types={"/w/cst-wt": "user_home_t"}, repo=repo, label_disabled=True))
        # container_ro_file_t is a type containers may read too.
        self.assertIsNone(check(enforcing=True, types={"/w/cst-wt": "container_ro_file_t"}, repo=repo))

    def test_host_selinux_is_seen_from_a_toolbox(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cst-e2e-lsm-")))
        enforce, marker, host_enforce = tmp / "sys-enforce", tmp / "containerenv", tmp / "host-enforce"

        def seen() -> bool:
            return self.mod.host_selinux(enforce, marker, host_enforce)

        self.assertFalse(seen(), "no SELinux anywhere")
        host_enforce.write_text("1\n")
        self.assertFalse(seen(), "a /run/host tree outside a container is not this machine's host")
        marker.write_text("")
        self.assertTrue(seen(), "inside a toolbox: the host's selinuxfs under /run/host")
        host_enforce.unlink()
        marker.unlink()
        enforce.write_text("0\n")
        self.assertTrue(seen(), "SELinux in the kernel, even permissive")

    def test_label_disable_from_a_toolbox(self) -> None:
        # The host's SELinux seen only under /run/host: label=disable is added
        # (the toolbox's docker is the host's daemon), so the preflight passes.
        self.mod.host_selinux = lambda *a, **k: True
        self.assertIn("label=disable", self.mod.host_lsm_security_opts())
        self.mod.host_selinux = lambda *a, **k: False
        self.assertNotIn("label=disable", self.mod.host_lsm_security_opts())

    def test_apparmor_unconfined_only_when_the_kernel_has_it_on(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cst-e2e-apparmor-")))
        enabled = tmp / "enabled"
        unconfined = ["--security-opt", "apparmor=unconfined"]
        self.mod.host_selinux = lambda *a, **k: False
        enabled.write_text("Y\n")
        self.assertEqual(self.mod.host_lsm_security_opts(enabled), unconfined)
        enabled.write_text("N\n")
        self.assertEqual(self.mod.host_lsm_security_opts(enabled), [])
        self.assertEqual(self.mod.host_lsm_security_opts(tmp / "missing"), [], "no AppArmor in the kernel")
        self.assertEqual(self.mod.host_lsm_security_opts(tmp), [], "unreadable (a folder: IsADirectoryError)")
        # With SELinux too: label=disable first, then apparmor=unconfined.
        self.mod.host_selinux = lambda *a, **k: True
        enabled.write_text("Y\n")
        self.assertEqual(self.mod.host_lsm_security_opts(enabled), ["--security-opt", "label=disable", *unconfined])

    def test_tier1_suite_gets_the_security_options(self) -> None:
        # test_01 starts containers of its own: they need label=disable too.
        cmds: list[list[str]] = []
        self.mod.run = lambda cmd, **k: cmds.append(list(cmd))
        self.mod.docker_build = lambda *a, **k: None
        self.mod.wait_systemd_ready = lambda *a, **k: None
        self.mod.dump_logs = lambda *a, **k: None
        self.mod.load_suite = lambda name: unittest.TestSuite()
        self.mod.host_lsm_security_opts = lambda: ["--security-opt", "label=disable"]
        self.enterContext(mock.patch.dict(os.environ, {}, clear=False))
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cst-e2e-tier1-")))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(self.mod.run_tier1("t", tmp))
        self.assertEqual(os.environ.get("CST_E2E_SECURITY_OPTS"), "--security-opt label=disable")
        systemd = next(c for c in cmds if "SYS_ADMIN" in c)
        self.assertIn("label=disable", systemd)

    def test_selinux_type_of_a_label(self) -> None:
        path = Path("/w/cst-wt/cst")
        with mock.patch("os.getxattr", return_value=b"unconfined_u:object_r:user_home_t:s0:c1,c2\0"):
            self.assertEqual(self.mod.selinux_type(path), "user_home_t")
        with mock.patch("os.getxattr", side_effect=OSError(61, "No data available")):
            self.assertIsNone(self.mod.selinux_type(path))
        with mock.patch("os.getxattr", return_value=b"garbage"):
            self.assertIsNone(self.mod.selinux_type(path))

    def test_a_bad_label_stops_the_run_before_any_container(self) -> None:
        calls: list[int] = []
        self.mod.run_tier1 = lambda *a, **k: calls.append(1) or True
        self.mod.checkout_preflight = lambda *a, **k: "[e2e] SELinux ... chcon -R -t container_file_t /w\n"
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = self.mod.main(["--tier", "1"])
        self.assertEqual((rc, calls), (2, []))
        self.assertIn("chcon -R -t container_file_t", err.getvalue())


class DumpLogsTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.mod = load_run()

    def test_dump_logs_collects_full_boot_journal_and_all_four_logs(self) -> None:
        cmds: list[list[str]] = []

        def fake_run(cmd: Sequence[str], **_k: object) -> object:
            cmds.append(list(cmd))

            class _Done:
                returncode = 0
                stdout = f"out for {' '.join(cmd)}"
                stderr = ""

            return _Done()

        self.mod.run = fake_run
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cst-e2e-logs-")))
        self.mod.dump_logs(["c1"], tmp)
        files = sorted(p.name for p in tmp.iterdir())
        self.assertEqual(
            files,
            ["c1_docker_logs.log", "c1_journal_boot.log", "c1_system_failed.log", "c1_user_failed.log"],
        )
        journal_cmd = next(c for c in cmds if "journalctl" in c)
        self.assertEqual(journal_cmd, ["docker", "exec", "c1", "journalctl", "-b", "--no-pager"])
        self.assertNotIn("-n", journal_cmd)
        self.assertIn("journalctl -b --no-pager", (tmp / "c1_journal_boot.log").read_text())


if __name__ == "__main__":
    unittest.main()

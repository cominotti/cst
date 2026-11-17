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
from typing import Protocol, cast
import unittest
from unittest import mock

RUN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")


class _E2ERunModule(Protocol):
    run_tier1: Callable[..., bool]
    run_tier2: Callable[..., bool]
    main: Callable[[Sequence[str] | None], int]
    checkout_preflight: Callable[..., str | None]

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
        # Containers started with label=disable (SELinux seen at /sys/fs/selinux) read any label.
        self.assertIsNone(check(enforcing=True, types={"/w/cst-wt": "user_home_t"}, repo=repo, label_disabled=True))

    def test_a_bad_label_stops_the_run_before_any_container(self) -> None:
        calls: list[int] = []
        self.mod.run_tier1 = lambda *a, **k: calls.append(1) or True
        self.mod.checkout_preflight = lambda *a, **k: "[e2e] SELinux ... chcon -R -t container_file_t /w\n"
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = self.mod.main(["--tier", "1"])
        self.assertEqual((rc, calls), (2, []))
        self.assertIn("chcon -R -t container_file_t", err.getvalue())


if __name__ == "__main__":
    unittest.main()

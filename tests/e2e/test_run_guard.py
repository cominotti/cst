"""Unit tests for the tier 2 opt-in guard in tests/e2e/run.py.

These never start Docker: the tier runners are replaced by stubs that fail
the test if the guard lets a run through when it should not.
"""

import contextlib
import importlib.util
import io
import os
import unittest
from unittest import mock

RUN_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run.py")


def load_run():
    spec = importlib.util.spec_from_file_location("cst_e2e_run", RUN_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PrivilegedGuardTest(unittest.TestCase):
    def setUp(self):
        self.run = load_run()
        self.calls = []
        self.run.run_tier1 = lambda *a, **k: self.calls.append(1) or True
        self.run.run_tier2 = lambda *a, **k: self.calls.append(2) or True
        env = {k: v for k, v in os.environ.items() if k != "CST_E2E_ALLOW_PRIVILEGED"}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = self.run.main(list(argv))
        return rc, err.getvalue()

    def test_tier2_refused_without_opt_in(self):
        for argv in (("--tier", "2"), ("--tier", "1", "--tier", "2"), ("--tier", "2", "--check-only")):
            rc, err = self.main(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("--privileged", err)
            self.assertIn("block", err)
            self.assertIn("CST_E2E_ALLOW_PRIVILEGED=1", err)
        self.assertEqual(self.calls, [])

    def test_env_values_other_than_1_do_not_opt_in(self):
        for value in ("", "0", "true", "yes"):
            os.environ["CST_E2E_ALLOW_PRIVILEGED"] = value
            rc, _ = self.main("--tier", "2")
            self.assertEqual(rc, 2, value)
        self.assertEqual(self.calls, [])

    def test_tier2_allowed_by_env_or_flag(self):
        os.environ["CST_E2E_ALLOW_PRIVILEGED"] = "1"
        self.assertEqual(self.main("--tier", "2")[0], 0)
        del os.environ["CST_E2E_ALLOW_PRIVILEGED"]
        self.assertEqual(self.main("--tier", "2", "--allow-privileged")[0], 0)
        self.assertEqual(self.calls, [2, 2])

    def test_tier1_needs_no_opt_in(self):
        self.assertEqual(self.main("--tier", "1")[0], 0)
        self.assertEqual(self.calls, [1])

    def test_check_only_runs_nothing(self):
        self.assertEqual(self.main("--tier", "1", "--tier", "2", "--allow-privileged", "--check-only")[0], 0)
        self.assertEqual(self.main("--tier", "1", "--check-only")[0], 0)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

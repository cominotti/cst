# SPDX-License-Identifier: MPL-2.0
"""Unit tests for tests/coverage_floors.py, the coverage floors of the nightly.

A coverage JSON report is made up here; no coverage.py runs.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
from typing import override
import unittest
from unittest import mock

from tests import coverage_floors
from tests.coverage_floors import COVERAGE_FLOORS, floor_problems, layer_counts, percentages
from tests.repo_files import REPO
from tests.test_workflow_audit import INVOWKFILE, task_script


def summary(lines: int, covered_lines: int, branches: int = 0, covered_branches: int = 0) -> dict[str, object]:
    return {
        "summary": {
            "num_statements": lines,
            "covered_lines": covered_lines,
            "num_branches": branches,
            "covered_branches": covered_branches,
        }
    }


REPORT: dict[str, object] = {
    "files": {
        "swap/domain.py": summary(80, 76, 20, 18),  # 94 of 100
        "swap/application/apply.py": summary(60, 45, 40, 25),  # 70 of 100
        "swap/application/reports.py": summary(100, 100),  # 100 of 100
        "swap/adapters.py": summary(10, 1),
        "swap/tests/test_swap.py": summary(500, 500),
        "tests/run_unit.py": summary(50, 10),
        ".github/scripts/pin_invowk.py": summary(40, 0),
        "swap/cst-swap-helper": summary(5, 0),
    }
}


class CoverageFloorsTest(unittest.TestCase):
    def test_counts_per_package_and_layer_leave_tests_out(self) -> None:
        self.assertEqual(
            layer_counts(REPORT),
            {"swap domain": (94, 100), "swap application": (170, 200), "swap adapters": (1, 10), "swap entry": (0, 5)},
        )
        self.assertEqual(percentages({"a domain": (1, 4), "b domain": (0, 0)}), {"a domain": 25.0, "b domain": 100.0})

    def test_at_or_above_the_floor_passes(self) -> None:
        self.assertEqual(floor_problems({"swap domain": 94.0, "swap application": 85.0}, {"swap domain": 94}), [])

    def test_below_the_floor_fails(self) -> None:
        self.assertEqual(
            floor_problems({"swap domain": 93.96}, {"swap domain": 94}),
            ["swap domain: 94.0 % covered, floor 94 %: add unit tests for the code the change added"],
        )

    def test_a_floor_with_nothing_measured_fails(self) -> None:
        self.assertEqual(
            floor_problems({}, {"gone domain": 50}),
            [
                (
                    "gone domain: has a floor (50 %) but no such package layer was measured; "
                    "rename the entry with its package, or delete it"
                )
            ],
        )

    def test_a_layer_without_a_floor_passes(self) -> None:
        self.assertEqual(floor_problems({"new domain": 1.0}, {}), [])

    def test_the_program_fails_below_a_floor_and_prints_the_table(self) -> None:
        with tempfile.TemporaryDirectory(prefix="cst-coverage-") as tmp:
            path = os.path.join(tmp, "coverage.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(REPORT, fh)
            floors = {"swap domain": 95, "swap application": 80}
            out, err = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(COVERAGE_FLOORS, floors, clear=True),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                rc = coverage_floors.main([path])
            self.assertEqual(rc, 1)
            self.assertEqual(
                err.getvalue(),
                "error: swap domain: 94.0 % covered, floor 95 %: add unit tests for the code the change added\n",
            )
            self.assertIn("swap application", out.getvalue())
            self.assertIn("85.0 %  floor 80 %", out.getvalue())
            self.assertIn("swap adapters", out.getvalue())
            with (
                mock.patch.dict(COVERAGE_FLOORS, {"swap domain": 94}, clear=True),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(coverage_floors.main([path]), 0)

    def test_not_a_report_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            layer_counts({"totals": {}})


class CoverageTaskOutTest(unittest.TestCase):
    """The --out check of `invowk cmd test coverage`, run from invowkfile.cue under bash.

    Each refusal ends the script before it runs any tool, so the script runs
    as it is, in a scratch "checkout" that has no tests/uv-dev.
    """

    @override
    def setUp(self) -> None:
        with open(os.path.join(REPO, INVOWKFILE), encoding="utf-8") as fh:
            script = task_script(fh.read(), "test coverage")
        self.assertIsNotNone(script)
        self.script = script or ""
        tmp = tempfile.TemporaryDirectory(prefix="cst-coverage-out-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.repo = os.path.join(self.tmp, "checkout")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.repo)
        os.makedirs(self.outside)

    def run_task(self, out: str) -> subprocess.CompletedProcess[str]:
        env = {"PATH": "/usr/bin:/bin", "HOME": self.tmp, "INVOWK_FLAG_OUT": out}
        return subprocess.run(
            ["bash", "-c", self.script],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )

    def test_a_relative_folder_is_refused(self) -> None:
        r = self.run_task("cov")
        self.assertEqual(r.returncode, 2)
        self.assertIn("test coverage: --out must be an absolute path (outside the checkout)", r.stderr)
        self.assertEqual(os.listdir(self.repo), [])

    def test_a_folder_inside_the_checkout_is_refused_and_not_created(self) -> None:
        os.symlink(self.repo, os.path.join(self.outside, "link"))
        for out, shown in (
            (os.path.join(self.repo, "cov"), os.path.join(self.repo, "cov")),
            (self.repo, self.repo),
            # A path through a symlink is judged where it leads.
            (os.path.join(self.outside, "link", "cov"), os.path.join(self.repo, "cov")),
            (os.path.join(self.outside, "..", "checkout", "cov"), os.path.join(self.repo, "cov")),
        ):
            with self.subTest(out=out):
                r = self.run_task(out)
                self.assertEqual(r.returncode, 2)
                self.assertIn(f"test coverage: {shown} is inside the checkout; give a folder outside it", r.stderr)
                self.assertEqual(os.listdir(self.repo), [])

    def test_a_folder_outside_the_checkout_is_created(self) -> None:
        # A folder whose name only starts like the checkout's is outside it.
        out = self.repo + "-coverage"
        r = self.run_task(out)
        self.assertTrue(os.path.isdir(out))
        self.assertNotIn("inside the checkout", r.stderr)
        # It went on to the tools, which this scratch checkout does not have.
        self.assertIn("tests/uv-dev", r.stderr)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()

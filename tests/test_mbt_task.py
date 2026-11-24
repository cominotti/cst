# SPDX-License-Identifier: MPL-2.0
"""The seed of `invowk cmd test mbt`, run from invowkfile.cue under bash.

The script runs as it is, in a scratch "checkout" whose tests/uv-dev is a
stub that prints the variables the task hands on, so no uv and no
Hypothesis run here.
"""

import os
import re
import subprocess
import tempfile
from typing import override
import unittest

from tests.repo_files import REPO
from tests.test_workflow_audit import INVOWKFILE, task_script

STUB = """#!/bin/bash
printf 'seed=%s profile=%s\\n' "${CST_MBT_SEED-unset}" "${CST_MBT_PROFILE-unset}"
"""
CHOSEN = re.compile(
    r"^mbt: profile nightly, seed (\d+) \(replay: invowk cmd test mbt --profile nightly --seed \1\)$", re.MULTILINE
)


class MbtTaskSeedTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        with open(os.path.join(REPO, INVOWKFILE), encoding="utf-8") as fh:
            script = task_script(fh.read(), "test mbt")
        self.assertIsNotNone(script)
        self.script = script or ""
        tmp = tempfile.TemporaryDirectory(prefix="cst-mbt-task-")
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.realpath(tmp.name)
        os.makedirs(os.path.join(self.repo, "tests", "mbt"))
        with open(os.path.join(self.repo, "tests", "mbt", "mbt_x.py"), "w", encoding="utf-8") as fh:
            fh.write("")
        stub = os.path.join(self.repo, "tests", "uv-dev")
        with open(stub, "w", encoding="utf-8") as fh:
            fh.write(STUB)
        os.chmod(stub, 0o755)

    def run_task(self, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", "-c", self.script],
            cwd=self.repo,
            env={"PATH": "/usr/bin:/bin", "HOME": self.repo, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )

    def test_nightly_without_a_seed_picks_one_prints_it_and_hands_it_on(self) -> None:
        r = self.run_task(INVOWK_FLAG_PROFILE="nightly")
        self.assertEqual(r.returncode, 0, r.stderr)
        chosen = CHOSEN.search(r.stdout)
        self.assertIsNotNone(chosen, r.stdout)
        self.assertIn(f"seed={chosen.group(1) if chosen else ''} profile=nightly", r.stdout)

    def test_a_seed_in_cst_mbt_seed_is_kept_under_every_profile(self) -> None:
        for profile in ("dev", "ci", "nightly"):
            with self.subTest(profile=profile):
                r = self.run_task(INVOWK_FLAG_PROFILE=profile, CST_MBT_SEED="2584909277")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIsNone(CHOSEN.search(r.stdout), r.stdout)
                self.assertIn(f"seed=2584909277 profile={profile}", r.stdout)

    def test_the_seed_flag_wins_over_cst_mbt_seed(self) -> None:
        r = self.run_task(INVOWK_FLAG_PROFILE="nightly", INVOWK_FLAG_SEED="7", CST_MBT_SEED="2584909277")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("seed=7 profile=nightly", r.stdout)


if __name__ == "__main__":
    unittest.main()

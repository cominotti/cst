# SPDX-License-Identifier: MPL-2.0
"""The script of `invowk cmd test quint`, run from invowkfile.cue under bash.

The script runs as it is, in a scratch "checkout" whose tests/quint/cst-quint is a stub that prints
the arguments it was given, so no node, java or quint run here.
"""

import json
import os
import subprocess
import tempfile
from typing import override
import unittest

from tests.repo_files import REPO
from tests.test_workflow_audit import INVOWKFILE, task_script

STUB = """import json, sys
print(json.dumps(sys.argv[1:]))
"""


class QuintTaskTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        with open(os.path.join(REPO, INVOWKFILE), encoding="utf-8") as fh:
            script = task_script(fh.read(), "test quint")
        self.assertIsNotNone(script)
        self.script = script or ""
        tmp = tempfile.TemporaryDirectory(prefix="cst-quint-task-")
        self.addCleanup(tmp.cleanup)
        self.repo = os.path.realpath(tmp.name)
        os.makedirs(os.path.join(self.repo, "tests", "quint"))
        with open(os.path.join(self.repo, "tests", "quint", "cst-quint"), "w", encoding="utf-8") as fh:
            fh.write(STUB)

    def run_task(self, **env: str) -> list[str]:
        r = subprocess.run(
            ["bash", "-c", self.script],
            cwd=self.repo,
            env={"PATH": "/usr/bin:/bin", "HOME": self.repo, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        args = json.loads(r.stdout)
        self.assertIsInstance(args, list)
        return [str(a) for a in args]

    def test_no_flag_checks_every_instance(self) -> None:
        self.assertEqual(self.run_task(), [])

    def test_each_flag_becomes_its_option(self) -> None:
        got = self.run_task(
            INVOWK_FLAG_INSTANCE="nocow_ab",
            INVOWK_FLAG_SPEC="/tmp/planted dir/spec.qnt",
            INVOWK_FLAG_KEEP="true",
            INVOWK_FLAG_TRACES="false",
        )
        self.assertEqual(got, ["--instance", "nocow_ab", "--spec", "/tmp/planted dir/spec.qnt", "--keep"])
        self.assertEqual(self.run_task(INVOWK_FLAG_TRACES="true"), ["--traces"])


if __name__ == "__main__":
    unittest.main()

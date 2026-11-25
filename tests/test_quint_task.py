# SPDX-License-Identifier: MPL-2.0
"""The script of `invowk cmd test quint`, run from invowkfile.cue under bash, and the paths whose
change makes CI's quint job run it.

The script runs as it is, in a scratch "checkout" whose tests/quint/cst-quint is a stub that prints
the arguments it was given, so no node, java or quint run here.
"""

import json
import os
import re
import subprocess
import textwrap
import tempfile
from typing import override
import unittest

from tests.repo_files import REPO
from tests.test_workflow_audit import CI, INVOWKFILE, task_script

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


class QuintCiPathsTest(unittest.TestCase):
    """ci.yml's quint_path, the shell function its changes job runs on each changed path."""

    def quint_path(self, path: str) -> bool:
        with open(os.path.join(REPO, CI), encoding="utf-8") as fh:
            found = re.search(r"^( *)quint_path\(\) \{\n.*?^\1\}\n", fh.read(), re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(found, f"no quint_path() in {CI}")
        func = textwrap.dedent(found.group(0)) if found else ""
        r = subprocess.run(
            ["bash", "-c", func + 'quint_path "$1"', "bash", path],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertIn(r.returncode, (0, 1), r.stderr)
        return r.returncode == 0

    def test_a_change_to_what_the_job_runs_runs_it(self) -> None:
        # The code each spec models, the specs and their traces, the replays, the tool, and what runs
        # the tool: the job itself, the invowk task, the action that installs invowk.
        for path in (
            "btrfs/docker_volumes_nocow/application.py",
            "btrfs/tests/quint/docker_volumes_nocow.qnt",
            "btrfs/tests/quint/traces/walk_ab.itf.json",
            "btrfs/tests/test_docker_volumes_nocow_replay.py",
            "vm/application/nas.py",
            "vm/application/nas_torn.py",
            "vm/adapters/restic.py",
            "vm/adapters/backup.py",
            "vm/domain/backup.py",
            "vm/domain/__init__.py",
            "vm/tests/quint/nas.qnt",
            "vm/tests/quint/traces/walk_live1.itf.json",
            "vm/tests/test_vm_nas_replay.py",
            "tests/quint/cst-quint",
            ".github/workflows/ci.yml",
            "cst.invowkmod/invowkfile.cue",
            ".github/actions/setup-invowk/action.yml",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.quint_path(path))

    def test_other_changes_skip_it(self) -> None:
        # The local backup and the docs do not run it.
        for path in (
            "README.md",
            "btrfs/cache/adapters.py",
            ".github/workflows/nightly.yml",
            "tests/mbt/profiles.py",
            "vm/application/backup.py",
            "vm/README.md",
            "vm/tests/test_vm_nas.py",
        ):
            with self.subTest(path=path):
                self.assertFalse(self.quint_path(path))


if __name__ == "__main__":
    unittest.main()

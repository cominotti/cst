# SPDX-License-Identifier: MPL-2.0
"""Unit tests for dev/CI invowk tasks in cst.invowkmod/invowkfile.cue."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import tempfile
from typing import TypedDict, override
import unittest

from tests.repo_files import REPO
from tests.test_workflow_audit import CI, INVOWKFILE, task_script

_STUB_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys

log_path = os.environ["CST_TASK_STUB_LOG"]
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"exe": os.path.basename(sys.argv[0]), "argv": sys.argv[1:]}) + "\\n")

if os.path.basename(sys.argv[0]) == "docker" and sys.argv[1:3] == ["image", "inspect"]:
    sys.exit(int(os.environ.get("CST_DOCKER_INSPECT_RC", "0")))
sys.exit(0)
"""


class _StubCall(TypedDict):
    exe: str
    argv: list[str]


def _load_task(name: str) -> str:
    cue = (Path(REPO) / INVOWKFILE).read_text(encoding="utf-8")
    script = task_script(cue, name)
    assert script is not None, f"missing task {name!r} in {INVOWKFILE}"
    return script


class FastGatesTaskTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.script = _load_task("ci fast-gates")
        tmp = tempfile.TemporaryDirectory(prefix="cst-fast-gates-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "repo"
        self.bin_dir = self.root / "bin"
        self.repo.mkdir()
        self.bin_dir.mkdir()
        self.log_path = self.root / "calls.jsonl"
        for tool in ("docker", "invowk"):
            path = self.bin_dir / tool
            path.write_text(_STUB_SCRIPT, encoding="utf-8")
            path.chmod(0o755)

    def _run_task(self, *, inspect_rc: str = "0", **env: str) -> list[_StubCall]:
        if self.log_path.exists():
            self.log_path.unlink()
        proc_env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "HOME": str(self.root),
            "CST_TASK_STUB_LOG": str(self.log_path),
            "CST_DOCKER_INSPECT_RC": inspect_rc,
            **env,
        }
        proc = subprocess.run(
            ["bash", "-c", self.script],
            cwd=self.repo,
            env=proc_env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out: list[_StubCall] = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            raw = json.loads(line)
            assert isinstance(raw, dict)
            exe = raw["exe"]
            argv = raw["argv"]
            assert isinstance(exe, str)
            assert isinstance(argv, list) and all(isinstance(item, str) for item in argv)
            out.append({"exe": exe, "argv": argv})
        return out

    def test_default_skips_build_mounts_ro_and_runs_check_only(self) -> None:
        calls = self._run_task(inspect_rc="0")
        self.assertEqual([c["argv"][:2] for c in calls], [["image", "inspect"], ["run", "--rm"]])
        run_argv = calls[1]["argv"]
        self.assertIn(f"{self.repo}:/repo:ro", run_argv)
        self.assertIn(f"{self.bin_dir / 'invowk'}:/usr/local/bin/invowk:ro", run_argv)
        self.assertNotIn(f"{self.repo}:/repo:z", run_argv)
        if Path("/sys/fs/selinux/enforce").exists() or Path("/run/host/sys/fs/selinux/enforce").exists():
            idx = run_argv.index("--security-opt")
            self.assertEqual(run_argv[idx + 1], "label=disable")
        self.assertEqual(run_argv[-1], "false")
        self.assertIn("invowk cmd check", run_argv[-3])

    def test_mbt_and_jobs_flags(self) -> None:
        calls = self._run_task(INVOWK_FLAG_MBT="true", INVOWK_FLAG_JOBS="4")
        run_argv = calls[1]["argv"]
        self.assertIn("CST_TEST_JOBS=4", run_argv)
        self.assertEqual(run_argv[-1], "true")
        self.assertIn("invowk cmd test mbt --profile ci", run_argv[-3])

    def test_rebuild_forces_no_cache_build(self) -> None:
        calls = self._run_task(INVOWK_FLAG_REBUILD="true")
        self.assertEqual(
            calls[0]["argv"],
            ["build", "--no-cache", "-t", "cst-ci-fast-gates:local", "-f", "tests/ci/fast-gates.Dockerfile", "."],
        )

    def test_missing_image_builds_automatically(self) -> None:
        calls = self._run_task(inspect_rc="1")
        self.assertEqual([c["argv"][0] for c in calls], ["image", "build", "run"])
        self.assertEqual(
            calls[1]["argv"],
            ["build", "-t", "cst-ci-fast-gates:local", "-f", "tests/ci/fast-gates.Dockerfile", "."],
        )

    def test_dockerfile_uv_pin_matches_ci_workflow(self) -> None:
        ci_text = (Path(REPO) / CI).read_text(encoding="utf-8")
        ci_ver = re.search(r"astral-sh/setup-uv@[^\n]+\n\s+with:\n\s+version:\s*'([^']+)'", ci_text)
        ci_sha = re.search(r"checksum:\s*'([0-9a-f]{64})'", ci_text)
        self.assertIsNotNone(ci_ver)
        self.assertIsNotNone(ci_sha)
        assert ci_ver is not None and ci_sha is not None

        df_text = (Path(REPO) / "tests" / "ci" / "fast-gates.Dockerfile").read_text(encoding="utf-8")
        df_ver = re.search(r"^ARG UV_VERSION=([^\s]+)$", df_text, re.MULTILINE)
        df_sha = re.search(r"^ARG UV_SHA256=([0-9a-f]{64})$", df_text, re.MULTILINE)
        self.assertIsNotNone(df_ver)
        self.assertIsNotNone(df_sha)
        assert df_ver is not None and df_sha is not None

        self.assertEqual(df_ver.group(1), ci_ver.group(1))
        self.assertEqual(df_sha.group(1), ci_sha.group(1))


if __name__ == "__main__":
    unittest.main()

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

exe = os.path.basename(sys.argv[0])
stdin_data = sys.stdin.read() if (exe == "invowk" and sys.argv[1:3] == ["tui", "filter"]) else None
log_path = os.environ["CST_TASK_STUB_LOG"]
with open(log_path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"exe": exe, "argv": sys.argv[1:], "stdin": stdin_data}) + "\\n")

if exe == "docker" and sys.argv[1:3] == ["image", "inspect"]:
    sys.exit(int(os.environ.get("CST_DOCKER_INSPECT_RC", "0")))
if exe == "git" and sys.argv[1:2] == ["ls-files"]:
    sys.stdout.write(os.environ.get("CST_GIT_FILES", ""))
    sys.exit(0)
if exe == "invowk" and sys.argv[1:2] == ["tui"]:
    rc = int(os.environ.get("CST_TUI_RC", "0"))
    if rc == 0:
        sys.stdout.write(os.environ.get("CST_TUI_OUT", ""))
    sys.exit(rc)
sys.exit(0)
"""


class _StubCall(TypedDict):
    exe: str
    argv: list[str]
    stdin: str | None


def _load_task(name: str) -> str:
    cue = (Path(REPO) / INVOWKFILE).read_text(encoding="utf-8")
    script = task_script(cue, name)
    assert script is not None, f"missing task {name!r} in {INVOWKFILE}"
    return script


def _read_calls(log_path: Path) -> list[_StubCall]:
    out: list[_StubCall] = []
    if not log_path.exists():
        return out
    for line in log_path.read_text(encoding="utf-8").splitlines():
        raw = json.loads(line)
        assert isinstance(raw, dict)
        exe = raw["exe"]
        argv = raw["argv"]
        stdin = raw.get("stdin")
        assert isinstance(exe, str)
        assert isinstance(argv, list) and all(isinstance(item, str) for item in argv)
        assert stdin is None or isinstance(stdin, str)
        out.append({"exe": exe, "argv": argv, "stdin": stdin})
    return out


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
        return _read_calls(self.log_path)

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


class CstWrapperAndPickerTasksTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cst-picker-tasks-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.repo = self.root / "repo"
        self.bin_dir = self.root / "bin"
        self.repo.mkdir()
        self.bin_dir.mkdir()
        self.log_path = self.root / "calls.jsonl"
        for tool in ("git", "invowk"):
            path = self.bin_dir / tool
            path.write_text(_STUB_SCRIPT, encoding="utf-8")
            path.chmod(0o755)
        cst_stub = self.repo / "cst"
        cst_stub.write_text(_STUB_SCRIPT, encoding="utf-8")
        cst_stub.chmod(0o755)
        e2e_dir = self.repo / "tests" / "e2e"
        e2e_dir.mkdir(parents=True)
        (e2e_dir / "run.py").write_text(
            "print('Tier 1 (2 tests):')\nprint('  test_a [cpu, doctor]')\nprint('  test_b [btrfs, cpu]')\n",
            encoding="utf-8",
        )

    def _run_named(self, name: str, **env: str) -> list[_StubCall]:
        if self.log_path.exists():
            self.log_path.unlink()
        proc_env = {
            "PATH": f"{self.bin_dir}:/usr/bin:/bin",
            "HOME": str(self.root),
            "CST_TASK_STUB_LOG": str(self.log_path),
            **env,
        }
        proc = subprocess.run(
            ["bash", "-c", _load_task(name)],
            cwd=self.repo,
            env=proc_env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return _read_calls(self.log_path)

    def test_cst_perf_flags(self) -> None:
        self.assertEqual([c["argv"] for c in self._run_named("cst perf")], [["perf"]])
        calls = self._run_named(
            "cst perf",
            INVOWK_FLAG_AXIS="memory,io",
            INVOWK_FLAG_COMPACT="true",
            INVOWK_FLAG_JSON="true",
        )
        self.assertEqual([c["argv"] for c in calls], [["perf", "--axis", "memory,io", "--compact", "--json"]])

    def test_cst_sync_plan_flags(self) -> None:
        self.assertEqual([c["argv"] for c in self._run_named("cst sync plan")], [["sync", "plan"]])
        calls = self._run_named(
            "cst sync plan",
            INVOWK_FLAG_INCLUDE_RISKY="true",
            INVOWK_FLAG_OVERWRITE_MODIFIED="true",
        )
        self.assertEqual([c["argv"] for c in calls], [["sync", "plan", "--include-risky", "--overwrite-modified"]])

    def test_test_pick_runs_selected_module_and_exits_zero_on_cancel(self) -> None:
        git_files = "vm/tests/test_vm.py\nbtrfs/tests/test_cache.py\n"
        calls = self._run_named(
            "test pick",
            CST_GIT_FILES=git_files,
            CST_TUI_OUT="btrfs.tests.test_cache\n",
        )
        self.assertEqual(calls[0]["argv"], ["ls-files", "*test_*.py"])
        self.assertEqual(calls[1]["argv"][:2], ["tui", "filter"])
        self.assertEqual(calls[1]["stdin"], "btrfs.tests.test_cache\nvm.tests.test_vm\n")
        self.assertEqual(calls[2]["argv"], ["cmd", "test", "module", "btrfs.tests.test_cache"])

        cancelled = self._run_named("test pick", CST_GIT_FILES=git_files, CST_TUI_RC="1")
        self.assertEqual(len(cancelled), 2)

    def test_test_e2e_pick_joins_tags_forwards_flags_and_handles_cancel(self) -> None:
        calls = self._run_named(
            "test e2e pick",
            CST_TUI_OUT="btrfs\ncpu\n",
            INVOWK_FLAG_KEEP_LOGS="true",
            INVOWK_FLAG_REBUILD="true",
            INVOWK_FLAG_JOBS="2",
        )
        self.assertEqual(
            calls[0]["argv"],
            ["tui", "choose", "--no-limit", "--title", "Pick Tier 1 E2E tag(s)", "btrfs", "cpu", "doctor"],
        )
        self.assertEqual(
            calls[1]["argv"],
            ["cmd", "test", "e2e", "tier1", "--tag", "btrfs,cpu", "--keep_logs", "--rebuild", "--jobs", "2"],
        )

        cancelled = self._run_named("test e2e pick", CST_TUI_RC="1")
        self.assertEqual(len(cancelled), 1)


if __name__ == "__main__":
    unittest.main()

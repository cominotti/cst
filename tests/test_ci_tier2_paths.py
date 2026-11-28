# SPDX-License-Identifier: MPL-2.0
"""Static gate: fail-closed scope of CI's `e2e-tier2` job.

Verifies `tier2_irrelevant_path()` in `.github/workflows/ci.yml` against:
- the full transitive Python import closure (`tests.code_graph.module_closure`,
  handling relative imports, `from pkg import mod`, and parent `__init__.py`)
  of every package, entry point, and E2E script that Tier 2 exercises (`btrfs`,
  `cpu`, `vm`, `doctor`, `cst_core`, `cst`, `tests/e2e`);
- runtime fixtures (`btrfs/tests/fixtures/steam/*`) and CI/E2E infrastructure
  (`.github/`, `cst.invowkmod/`, `pyproject.toml`, `uv.lock`, `tests/e2e/*`);
- fail-closed defaults on unknown/new files or paths outside the skip list.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from typing import override
import unittest

from tests.code_graph import is_python, module_closure, production_python
from tests.e2e_host_python import host_closure
from tests.repo_files import REPO, repo_files
from tests.test_workflow_audit import CI

_TIER2_SEED_DIRS = ("btrfs", "cpu", "vm", "doctor", "cst_core")
_TIER2_RUNTIME_PREFIXES = ("tests/e2e/", "btrfs/tests/fixtures/steam/")
_TIER2_STATIC_ENTRIES = (
    "cst",
    "btrfs-setup.sh",
    "cpu-setup.sh",
    "vm-setup.sh",
    ".github/workflows/ci.yml",
    ".github/actions/prep-e2e-runner/action.yml",
    ".github/actions/setup-invowk/action.yml",
    "cst.invowkmod/invowkfile.cue",
    "pyproject.toml",
    "uv.lock",
)


def tier2_transitive_closure() -> list[str]:
    """All repository files reachable from Tier 2 packages, fixtures, and E2E scripts."""
    files = list(repo_files())
    file_set = set(files)
    non_python_assets = {
        rel
        for rel in files
        if rel.startswith(_TIER2_RUNTIME_PREFIXES)
        or (rel.split("/", 1)[0] in _TIER2_SEED_DIRS and not rel.endswith(".md") and "/tests/" not in f"/{rel}")
    }
    py_seeds = {rel for rel in production_python(files) if rel.split("/", 1)[0] in _TIER2_SEED_DIRS or rel == "cst"}
    py_seeds.update(rel for rel in files if rel.startswith("tests/e2e/") and is_python(rel))
    e2e_host, e2e_problems = host_closure(REPO, ("tests/e2e/run.py", "tests/e2e/image_digest.py"))
    if e2e_problems:
        raise AssertionError(f"host_closure failed: {e2e_problems}")
    py_seeds.update(e2e_host)

    closure = {rel for rel in _TIER2_STATIC_ENTRIES if rel in file_set}
    closure.update(non_python_assets)
    closure.update(module_closure(py_seeds))
    return sorted(closure)


class Tier2CiPathsTest(unittest.TestCase):
    """ci.yml's tier2_irrelevant_path(), extracted and executed as bash."""

    _func: str = ""

    @classmethod
    @override
    def setUpClass(cls) -> None:
        super().setUpClass()
        with open(os.path.join(REPO, CI), encoding="utf-8") as fh:
            ci_text = fh.read()
        found = re.search(r"^( *)tier2_irrelevant_path\(\) \{\n.*?^\1\}\n", ci_text, re.MULTILINE | re.DOTALL)
        if found is None:
            raise AssertionError(f"no tier2_irrelevant_path() in {CI}")
        cls._func = textwrap.dedent(found.group(0))

    def is_irrelevant(self, path: str) -> bool:
        r = subprocess.run(
            ["bash", "-c", self._func + 'tier2_irrelevant_path "$1"', "bash", path],
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        self.assertIn(r.returncode, (0, 1), r.stderr)
        return r.returncode == 0

    def test_every_file_in_tier2_closure_triggers_tier2(self) -> None:
        closure = tier2_transitive_closure()
        self.assertTrue(closure)
        self.assertIn("desktop/__init__.py", closure)
        false_skips = [rel for rel in closure if self.is_irrelevant(rel)]
        self.assertEqual(false_skips, [], f"Tier 2 closure files matched skip list: {false_skips}")

    def test_unknown_and_shared_paths_fail_closed_to_tier2(self) -> None:
        for path in (
            "new_module/foo.py",
            "root_script.py",
            "cst",
            "tests/__init__.py",
            "tests/xdg_cache.py",
            "tests/e2e/tests/extra.py",
            "tests/e2e/README.md",
            "tests/e2e/suites/suite_tier2_storage_cpu.py",
            "btrfs/tests/fixtures/steam/libraryfolders.vdf",
            "toolchains/domain/common.py",
            "sync/domain.py",
            "desktop/domain/psd.py",
            "perf/domain/parsers.py",
            "swap/domain.py",
            ".github/workflows/nightly.yml",
        ):
            with self.subTest(path=path):
                self.assertFalse(self.is_irrelevant(path), f"{path} must trigger Tier 2")

    def test_proven_disjoint_paths_may_skip_tier2(self) -> None:
        for path in (
            "README.md",
            ".agents/rules/fast-tests-and-ci.md",
            "bluetooth/setup/domain.py",
            "bluetooth-setup.sh",
            "btrfs/tests/test_btrfs_setup.py",
            "doctor/tests/test_doctor.py",
            "toolchains/tests/test_config_init.py",
            "tests/mbt/mbt_record.py",
            "tests/quint/cst-quint",
            "tests/test_e2e_host_python.py",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.is_irrelevant(path), f"{path} should be skip-safe for Tier 2")

    def test_ci_status_requires_tier2_output_when_skipped(self) -> None:
        with open(os.path.join(REPO, CI), encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("if: needs.changes.outputs.code == 'true' && needs.changes.outputs.tier2 == 'true'", text)
        self.assertIn("TIER2_NEEDED: ${{ needs.changes.outputs.tier2 }}", text)
        self.assertIn(
            '[[ "${TIER2_RESULT}" == "skipped" && ( "${CODE_CHANGED}" == "false" || "${TIER2_NEEDED}" == "false" ) ]]',
            text,
        )


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: MPL-2.0
"""Unit tests for static `--affected` test selection (`tests/affected.py`)."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tests.affected import (
    DOCS_GATES,
    E2E_UNIT_TESTS,
    INSTALL_TESTS,
    format_plan,
    git_changed_files,
    resolve_base,
    reverse_import_map,
    select_affected,
)
from tests.run_unit import GATE_MODULES

_SAMPLE_INVENTORY = (
    "AGENTS.md",
    "README.md",
    "docs/dev-tools.md",
    "pyproject.toml",
    ".github/workflows/ci.yml",
    "cst_core/__init__.py",
    "cst_core/domain/__init__.py",
    "cst_core/domain/files.py",
    "swap/__init__.py",
    "swap/domain.py",
    "swap/tests/__init__.py",
    "swap/tests/test_swap.py",
    "swap/units/10-swap-memory-low.conf",
    "vm/__init__.py",
    "vm/cli.py",
    "vm/tests/__init__.py",
    "vm/tests/test_vm.py",
    "doctor/__init__.py",
    "doctor/tests/__init__.py",
    "doctor/tests/test_doctor.py",
    "btrfs/__init__.py",
    "btrfs/units/btrfs-scrub-weekly.service",
    "btrfs/tests/__init__.py",
    "btrfs/tests/test_btrfs_setup.py",
    "tests/__init__.py",
    "tests/run_unit.py",
    "tests/test_architecture.py",
    "tests/test_docs.py",
    "tests/test_install.py",
    "tests/test_install_record.py",
    "tests/test_license_headers.py",
    "tests/test_module_routing.py",
    "tests/test_repo_paths.py",
    "tests/e2e/run.py",
    "tests/e2e/test_run_guard.py",
    "tests/e2e/test_selection.py",
    "tests/e2e/test_image_digest.py",
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=True,
    )


class SelectAffectedRulesTest(unittest.TestCase):
    def _write_synth(self, root: Path, rel: str, text: str) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_empty_and_docs_only_changes(self) -> None:
        empty = select_affected([], all_files=_SAMPLE_INVENTORY)
        self.assertFalse(empty.full_suite)
        self.assertEqual(empty.modules, GATE_MODULES)

        docs = select_affected(["README.md", "docs/dev-tools.md", ".agents/rules/foo.md"], all_files=_SAMPLE_INVENTORY)
        self.assertFalse(docs.full_suite)
        self.assertEqual(docs.modules, DOCS_GATES)
        self.assertIn("selected (3):", format_plan(docs))

    def test_module_change_selects_own_tests_doctor_and_gates_only(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cst-aff-mod-")))
        plan = select_affected(["swap/domain.py"], all_files=_SAMPLE_INVENTORY, repo=str(tmp))
        self.assertFalse(plan.full_suite)
        self.assertIn("swap.tests.test_swap", plan.modules)
        self.assertIn("doctor.tests.test_doctor", plan.modules)
        self.assertIn("tests.test_architecture", plan.modules)
        self.assertNotIn("vm.tests.test_vm", plan.modules)
        for inst_mod in INSTALL_TESTS:
            self.assertNotIn(inst_mod, plan.modules)

    def test_manifest_or_unit_change_includes_install_tests(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cst-aff-unit-")))
        plan = select_affected(
            ["btrfs/units/btrfs-scrub-weekly.service"],
            all_files=_SAMPLE_INVENTORY,
            repo=str(tmp),
        )
        self.assertFalse(plan.full_suite)
        self.assertIn("btrfs.tests.test_btrfs_setup", plan.modules)
        self.assertIn("doctor.tests.test_doctor", plan.modules)
        for inst_mod in INSTALL_TESTS:
            self.assertIn(inst_mod, plan.modules)

    def test_shared_and_infrastructure_changes_fall_back_to_full_suite(self) -> None:
        for path in (
            "cst_core/domain/files.py",
            "pyproject.toml",
            ".github/workflows/ci.yml",
            "cst.invowkmod/invowkfile.cue",
            "tests/run_unit.py",
        ):
            with self.subTest(path=path):
                plan = select_affected([path], all_files=_SAMPLE_INVENTORY)
                self.assertTrue(plan.full_suite)
                self.assertEqual(plan.modules, ())
                self.assertIn("selected: FULL SUITE", format_plan(plan))

    def test_direct_test_and_e2e_edits_stay_scoped(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cst-aff-direct-")))
        direct = select_affected(["tests/test_install.py"], all_files=_SAMPLE_INVENTORY, repo=str(tmp))
        self.assertFalse(direct.full_suite)
        self.assertIn("tests.test_install", direct.modules)
        self.assertNotIn("vm.tests.test_vm", direct.modules)

        e2e = select_affected(["tests/e2e/run.py"], all_files=_SAMPLE_INVENTORY, repo=str(tmp))
        self.assertFalse(e2e.full_suite)
        for mod in E2E_UNIT_TESTS:
            self.assertIn(mod, e2e.modules)

        deleted = select_affected(["vm/tests/test_deleted.py"], all_files=_SAMPLE_INVENTORY, repo=str(tmp))
        self.assertFalse(deleted.full_suite)
        self.assertNotIn("vm.tests.test_deleted", deleted.modules)

    def test_reverse_ast_import_closure_across_modules(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cst-aff-ast-")))
        files = (
            "swap/__init__.py",
            "swap/domain.py",
            "swap/helper.py",
            "swap/tests/__init__.py",
            "swap/tests/test_swap.py",
            "doctor/__init__.py",
            "doctor/tests/__init__.py",
            "doctor/tests/test_doctor.py",
            "vm/__init__.py",
            "vm/tests/__init__.py",
            "vm/tests/test_vm.py",
        )
        self._write_synth(tmp, "swap/__init__.py", "")
        self._write_synth(tmp, "swap/helper.py", "X = 1\n")
        self._write_synth(tmp, "swap/domain.py", "from swap import helper\n")
        self._write_synth(tmp, "swap/tests/__init__.py", "")
        self._write_synth(tmp, "swap/tests/test_swap.py", "import swap.domain\n")
        self._write_synth(tmp, "doctor/__init__.py", "")
        self._write_synth(tmp, "doctor/tests/__init__.py", "")
        self._write_synth(tmp, "doctor/tests/test_doctor.py", "")
        self._write_synth(tmp, "vm/__init__.py", "")
        self._write_synth(tmp, "vm/tests/__init__.py", "")
        # Cross-package test importing swap.helper transitive dependency
        self._write_synth(tmp, "vm/tests/test_vm.py", "from swap.domain import helper\n")

        rev = reverse_import_map(
            ["swap/tests/test_swap.py", "vm/tests/test_vm.py"],
            repo=str(tmp),
            existing=files,
        )
        self.assertEqual(rev["swap/helper.py"], {"swap.tests.test_swap", "vm.tests.test_vm"})

        plan = select_affected(["swap/helper.py"], all_files=files, repo=str(tmp))
        self.assertFalse(plan.full_suite)
        self.assertIn("swap.tests.test_swap", plan.modules)
        self.assertIn("vm.tests.test_vm", plan.modules)


class GitAffectedIntegrationTest(unittest.TestCase):
    def test_resolve_base_and_git_changed_files_on_temp_repo(self) -> None:
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cst-aff-git-")))
        _git(tmp, "init", "-b", "main")
        _git(tmp, "config", "user.email", "t@example.com")
        _git(tmp, "config", "user.name", "Test")
        (tmp / "swap").mkdir()
        (tmp / "swap" / "domain.py").write_text("X = 1\n", encoding="utf-8")
        _git(tmp, "add", "swap/domain.py")
        _git(tmp, "commit", "-q", "-m", "init")
        main_sha = _git(tmp, "rev-parse", "HEAD").stdout.strip()

        _git(tmp, "checkout", "-q", "-b", "feat/x")
        (tmp / "swap" / "domain.py").write_text("X = 2\n", encoding="utf-8")
        _git(tmp, "commit", "-q", "-am", "edit swap")
        (tmp / "docs").mkdir()
        (tmp / "docs" / "new.md").write_text("# Doc\n", encoding="utf-8")

        self.assertEqual(resolve_base(str(tmp)), main_sha)
        base, changed = git_changed_files(str(tmp))
        self.assertEqual(base, main_sha)
        self.assertEqual(changed, ("docs/new.md", "swap/domain.py"))

        head_base, head_changed = git_changed_files(str(tmp), "HEAD")
        self.assertEqual(head_base, "HEAD")
        self.assertEqual(head_changed, ("docs/new.md",))

        with self.assertRaisesRegex(ValueError, "unknown git revision"):
            git_changed_files(str(tmp), "no-such-branch-xyz")
        self.assertTrue(os.path.isdir(tmp))


if __name__ == "__main__":
    unittest.main()

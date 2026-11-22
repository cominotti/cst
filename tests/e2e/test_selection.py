# SPDX-License-Identifier: MPL-2.0
"""Unit tests for E2E suite selection (--match, --tag, --list-tests)."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import io
import os
from pathlib import Path
import tempfile
from typing import override
import unittest
from unittest import mock

import tests.e2e.selection as e2e_selection
from tests.e2e.selection import (
    E2ETag,
    SuiteSelection,
    e2e_tags,
    iter_cases,
    parse_patterns,
    parse_tags,
    select_cases,
    test_method_name,
    test_tags,
    untagged_methods,
)
from tests.e2e.test_run_guard import load_run


class SelectionHelpersTest(unittest.TestCase):
    def test_e2e_tags_rejects_empty_or_non_enum(self) -> None:
        with self.assertRaises(ValueError):
            e2e_tags()
        raw_decorator = vars(e2e_selection)["e2e_tags"]
        self.assertIsInstance(raw_decorator, Callable)
        with self.assertRaises(TypeError):
            raw_decorator("btrfs")

    def test_parse_tags_and_patterns(self) -> None:
        self.assertEqual(parse_tags(["btrfs, swap", "CPU"]), frozenset({E2ETag.BTRFS, E2ETag.SWAP, E2ETag.CPU}))
        self.assertEqual(parse_patterns(["test_02, *bluetooth*", "vm"]), ("test_02", "*bluetooth*", "vm"))
        with self.assertRaisesRegex(ValueError, "unknown E2E tag"):
            parse_tags(["not_a_tag"])
        with self.assertRaisesRegex(ValueError, "empty E2E tag"):
            parse_tags(["btrfs,"])
        with self.assertRaisesRegex(ValueError, "empty test match pattern"):
            parse_patterns(["test_02,"])

    def test_select_cases_filters_by_tag_and_pattern(self) -> None:
        class _SampleSuite(unittest.TestCase):
            @e2e_tags(E2ETag.SMOKE, E2ETag.BTRFS)
            def test_01_btrfs_smoke(self) -> None:
                pass

            @e2e_tags(E2ETag.BLUETOOTH)
            def test_05_bluetooth_roles(self) -> None:
                pass

            @e2e_tags(E2ETag.BLUETOOTH, E2ETag.SMOKE)
            def test_80_bluetooth_le_audio(self) -> None:
                pass

            def test_99_untagged(self) -> None:
                pass

        cases = iter_cases(unittest.defaultTestLoader.loadTestsFromTestCase(_SampleSuite))
        by_tag = select_cases(cases, tags=frozenset({E2ETag.BLUETOOTH}))
        self.assertEqual(
            [test_method_name(c) for c in by_tag], ["test_05_bluetooth_roles", "test_80_bluetooth_le_audio"]
        )

        by_glob = select_cases(cases, patterns=("*le_audio",))
        self.assertEqual([test_method_name(c) for c in by_glob], ["test_80_bluetooth_le_audio"])

        by_both = select_cases(cases, patterns=("test_*",), tags=frozenset({E2ETag.SMOKE}))
        self.assertEqual([test_method_name(c) for c in by_both], ["test_01_btrfs_smoke", "test_80_bluetooth_le_audio"])

        intersection = select_cases(cases, patterns=("roles",), tags=frozenset({E2ETag.SMOKE}))
        self.assertEqual(intersection, [])


class TierSuitesMetadataTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.run_mod = load_run()

    def test_all_tier1_tests_have_valid_tags_without_env_vars(self) -> None:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("CST_E2E_")}
        with mock.patch.dict(os.environ, clean, clear=True):
            suite = self.run_mod.load_suite("suite_tier1_systemd.py")
            tier2 = self.run_mod.load_suite("suite_tier2_storage_cpu.py")
        cases = iter_cases(suite)
        self.assertGreaterEqual(len(cases), 75)
        self.assertEqual(untagged_methods(suite), [])
        for case in cases:
            self.assertTrue(test_tags(case), test_method_name(case))
        self.assertGreaterEqual(len(iter_cases(tier2)), 20)


class RunCliFilterTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.mod = load_run()
        self.calls: list[dict[str, object]] = []
        self.mod.checkout_preflight = lambda *a, **k: None
        self.mod.run_tier1 = lambda *a, **k: self.calls.append({"tier": 1, **k}) or True
        self.mod.run_tier2 = lambda *a, **k: self.calls.append({"tier": 2, **k}) or True

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.mod.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_list_tests_runs_no_container(self) -> None:
        rc, out, err = self._run_cli("--tier", "1", "--tag", "bluetooth", "--list-tests")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls, [])
        self.assertIn("Tier 1 (6 tests):", out)
        self.assertIn("test_80_bluetooth_second_apply_and_le_audio_cycle", out)

    def test_zero_match_and_invalid_tag_fail_fast(self) -> None:
        rc, _, err = self._run_cli("--tier", "1", "--match", "no_such_test_name")
        self.assertEqual(rc, 2)
        self.assertIn("no tests matched selection", err)
        self.assertEqual(self.calls, [])

        rc_tag, _, err_tag = self._run_cli("--tier", "1", "--tag", "nonexistent")
        self.assertEqual(rc_tag, 2)
        self.assertIn("unknown E2E tag", err_tag)
        self.assertEqual(self.calls, [])

        rc_t2, _, err_t2 = self._run_cli("--tier", "2", "--allow-privileged", "--tag", "btrfs")
        self.assertEqual(rc_t2, 2)
        self.assertIn("only supported for tier 1", err_t2)
        self.assertEqual(self.calls, [])

    def test_run_tier1_zero_match_stops_before_docker_build(self) -> None:
        mod = load_run()
        builds: list[str] = []
        mod.docker_build = lambda tag, *a, **k: builds.append(tag)
        with tempfile.TemporaryDirectory(prefix="cst-e2e-sel-") as tmp, contextlib.redirect_stderr(io.StringIO()):
            ok = mod.run_tier1("t", Path(tmp), selection=SuiteSelection(patterns=("definitely_absent_pattern",)))
        self.assertFalse(ok)
        self.assertEqual(builds, [])


if __name__ == "__main__":
    unittest.main()

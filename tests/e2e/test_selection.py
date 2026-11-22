# SPDX-License-Identifier: MPL-2.0
"""Unit tests for E2E suite selection (--match, --tag, --list-tests)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import contextlib
import io
import os
from pathlib import Path
import subprocess
import tempfile
from typing import override
import unittest
from unittest import mock

import tests.e2e.selection as e2e_selection
from tests.e2e.selection import (
    HOST_EXCLUSIVE_TESTS,
    E2ETag,
    ShardSpec,
    SuiteSelection,
    assign_shards,
    default_tier1_jobs,
    e2e_tags,
    iter_cases,
    parse_patterns,
    parse_shard,
    parse_tags,
    select_cases,
    select_shard,
    split_host_exclusive,
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


class ShardingTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.mod = load_run()
        self.mod.checkout_preflight = lambda *a, **k: None

    def _run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.mod.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_parse_shard_and_jobs_validation(self) -> None:
        self.assertEqual(parse_shard("2/3"), ShardSpec(index=2, total=3))
        for bad in ("0/3", "4/3", "1/0", "2", "1/2/3", "a/b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_shard(bad)

        self.assertEqual(default_tier1_jobs(4, cpu_count=24), 1)
        self.assertEqual(default_tier1_jobs(75, cpu_count=24), 4)
        self.assertEqual(default_tier1_jobs(75, cpu_count=4), 2)

        for argv in (
            ("--tier", "1", "--shard", "1/3", "--jobs", "2"),
            ("--tier", "1", "--jobs", "-1"),
            ("--tier", "2", "--allow-privileged", "--shard", "1/2"),
            ("--tier", "2", "--allow-privileged", "--jobs", "2"),
        ):
            rc, _, _ = self._run_cli(*argv)
            self.assertEqual(rc, 2, argv)

    def test_assign_shards_partitions_all_tier1_tests_weighted_lpt(self) -> None:
        cases = iter_cases(self.mod.load_suite("suite_tier1_systemd.py"))
        all_names = [test_method_name(c) for c in cases]
        for total in (1, 2, 3, 4, 80):
            bins = assign_shards(cases, total)
            self.assertEqual(len(bins), total)
            flattened = [test_method_name(c) for b in bins for c in b]
            self.assertEqual(len(flattened), len(all_names))
            self.assertEqual(set(flattened), set(all_names))
            for idx, bucket in enumerate(bins, 1):
                names = [test_method_name(c) for c in bucket]
                self.assertEqual(names, [n for n in all_names if n in set(names)])
                self.assertEqual([test_method_name(c) for c in select_shard(cases, ShardSpec(idx, total))], names)

        four = [{test_method_name(c) for c in bucket} for bucket in assign_shards(cases, 4)]
        heavy = (
            "test_80_bluetooth_second_apply_and_le_audio_cycle",
            "test_26_btrfs_watch_saves_the_report_of_a_run",
            "test_78_docker_gc_recorder_stays_failed_on_a_state_of_a_newer_docker_gc",
            "test_95_bluetooth_python_program_with_the_bash_one_runuser_and_le_audio",
        )
        for name in heavy:
            self.assertEqual(sum(name in b for b in four), 1, name)

        parallel, exclusive = split_host_exclusive(cases)
        self.assertEqual({test_method_name(c) for c in exclusive}, HOST_EXCLUSIVE_TESTS)
        self.assertEqual(len(parallel) + len(exclusive), len(cases))

    def test_list_tests_shard_and_parallel_preview(self) -> None:
        cases = iter_cases(self.mod.load_suite("suite_tier1_systemd.py"))
        seen: set[str] = set()
        for idx in (1, 2, 3):
            rc, out, err = self._run_cli("--tier", "1", "--shard", f"{idx}/3", "--list-tests")
            self.assertEqual(rc, 0, err)
            self.assertIn(f"shard {idx}/3", out)
            for line in out.splitlines()[1:]:
                seen.add(line.strip().split(" ", 1)[0])
        self.assertEqual(seen, {test_method_name(c) for c in cases})

        rc, out, err = self._run_cli("--tier", "1", "--jobs", "4", "--list-tests")
        self.assertEqual(rc, 0, err)
        self.assertIn("across 4 parallel shards", out)
        self.assertIn("Host-exclusive solo phase (1 tests):", out)

        rc_solo, out_solo, err_solo = self._run_cli(
            "--tier", "1", "--jobs", "4", "--match", "test_10_cpu_quiet", "--list-tests"
        )
        self.assertEqual(rc_solo, 0, err_solo)
        self.assertNotIn("Shard 1/0", out_solo)
        self.assertIn("Host-exclusive solo phase (1 tests):", out_solo)

    def test_empty_shard_skips_docker_cleanly(self) -> None:
        builds: list[str] = []
        self.mod.docker_build = lambda tag, *a, **k: builds.append(tag)
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="cst-e2e-empty-shard-")))
        with contextlib.redirect_stdout(io.StringIO()):
            ok = self.mod.run_tier1(
                "t",
                tmp,
                selection=SuiteSelection(patterns=("test_02_systemd",), shard=ShardSpec(5, 5)),
            )
        self.assertTrue(ok)
        self.assertEqual(builds, [])

    def test_parallel_runner_builds_once_and_runs_solo_exclusive_phase(self) -> None:
        builds: list[tuple[str, bool]] = []
        child_cmds: list[list[str]] = []
        solo_scopes: list[object] = []

        self.mod.docker_build = lambda tag, *_a, force=False, **_k: builds.append((tag, bool(force)))

        def fake_subprocess_run(cmd: Sequence[str], **_k: object) -> subprocess.CompletedProcess[str]:
            child_cmds.append(list(cmd))
            return subprocess.CompletedProcess(list(cmd), 0, "Ran tests OK\n", "")

        def fake_container(cfg: object) -> bool:
            sel = getattr(cfg, "selection", None)
            solo_scopes.append(getattr(sel, "host_scope", None))
            return True

        vars(self.mod)["_run_tier1_container"] = fake_container
        with mock.patch("subprocess.run", side_effect=fake_subprocess_run):
            rc, out, err = self._run_cli("--tier", "1", "--jobs", "2", "--rebuild")

        self.assertEqual(rc, 0, err + out)
        self.assertEqual(len(builds), 2)
        self.assertTrue(all(force for _, force in builds))
        self.assertEqual(len(child_cmds), 2)
        for cmd in child_cmds:
            self.assertIn("--no-build", cmd)
            self.assertNotIn("--rebuild", cmd)
            self.assertIn("--host-scope", cmd)
            self.assertIn("parallel", cmd)
        self.assertEqual([str(s) for s in solo_scopes], ["exclusive"])

        # One failing child shard fails the parallel run.
        def one_fail(cmd: Sequence[str], **_k: object) -> subprocess.CompletedProcess[str]:
            code = 1 if "1/2" in cmd else 0
            return subprocess.CompletedProcess(list(cmd), code, "shard output\n", "")

        with mock.patch("subprocess.run", side_effect=one_fail):
            rc_fail, _, _ = self._run_cli("--tier", "1", "--jobs", "2")
        self.assertEqual(rc_fail, 1)


if __name__ == "__main__":
    unittest.main()

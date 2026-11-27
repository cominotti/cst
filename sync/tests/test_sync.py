# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst sync` (`sync/domain.py`, `sync/application.py`, `sync/cli.py`, `sync/presenter.py`)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from unittest import mock

from cst_core.domain.errors import Refused
from cst_core.domain.install import ArtifactKind, Component, GeneratedKind, Owner
from cst_core.domain.record import ComponentState, InstallRecord, RecordedFile
from cst_core.ports import CommandResult
from sync.adapters import HostSyncExecAdapter, HostSyncInspectAdapter
from sync.application import plan_host_sync, run_apply
from sync.cli import run_sync_cli
from sync.domain import (
    FileDrift,
    SyncAction,
    SyncArgs,
    SyncHostObservation,
    desired_generated_bytes,
    parse_sync_args,
    plan_sync,
)
from sync.presenter import format_apply_report, format_sync_plan


@dataclass
class _FakeInspect:
    container: bool = False
    _euid: int = 1000
    user: tuple[str, int, str] = ("tester", 1000, "/home/tester")
    root_record: tuple[InstallRecord | None, str | None] = (None, None)
    user_record: tuple[InstallRecord | None, str | None] = (None, None)
    disk: dict[str, bytes | None] = field(default_factory=dict)
    desired_manifest: dict[tuple[str, tuple[tuple[str, str], ...]], str | None] = field(default_factory=dict)
    desired_generated: dict[str, str | None] = field(default_factory=dict)

    def in_container(self) -> bool:
        return self.container

    def euid(self) -> int:
        return self._euid

    def target_user(self) -> tuple[str, int, str]:
        return self.user

    def read_record(self, owner: Owner, home: str) -> tuple[InstallRecord | None, str | None]:
        del home
        return self.root_record if owner == Owner.ROOT else self.user_record

    def stat_and_hash(self, dest: str) -> tuple[bool, str | None]:
        if dest not in self.disk:
            return False, None
        raw = self.disk[dest]
        if raw is None:
            return True, None
        return True, hashlib.sha256(raw).hexdigest()

    def desired_manifest_sha256(self, src: str, values: dict[str, str] | None) -> str | None:
        key = (src, tuple(sorted((values or {}).items())))
        return self.desired_manifest.get(key)

    def desired_generated_sha256(self, dest: str, f: RecordedFile) -> str | None:
        del f
        return self.desired_generated.get(dest)


class _FakeExec:
    def __init__(self, *, results: dict[tuple[str, ...], int] | None = None, subpass_rc: int = 0) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.subpasses: list[SyncArgs] = []
        self._results: dict[tuple[str, ...], int] = dict(results) if results is not None else {}
        self._subpass_rc = subpass_rc

    def run_cst_subcommand(self, argv: Sequence[str], *, as_user: str | None = None) -> int:
        cmd = tuple(argv)
        self.calls.append((cmd, as_user))
        return self._results.get(cmd, 0)

    def run_root_subpass(self, args: SyncArgs) -> int:
        self.subpasses.append(args)
        return self._subpass_rc


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ParseArgsTest(unittest.TestCase):
    def test_parses_default_plan_and_flags(self) -> None:
        self.assertEqual(parse_sync_args([]), SyncArgs(SyncAction.PLAN))
        self.assertEqual(parse_sync_args(["plan", "--include-risky"]), SyncArgs(SyncAction.PLAN, include_risky=True))
        self.assertEqual(
            parse_sync_args(["apply", "--include-risky", "--overwrite-modified", "--root-only"]),
            SyncArgs(SyncAction.APPLY, include_risky=True, overwrite_modified=True, root_only=True),
        )
        self.assertEqual(parse_sync_args(["--help"]).action, SyncAction.HELP)
        self.assertEqual(parse_sync_args(["plan", "--help"]).action, SyncAction.HELP)
        self.assertEqual(parse_sync_args(["apply", "-h"]).action, SyncAction.HELP)
        self.assertIsNotNone(parse_sync_args(["plan", "--root-only"]).error)
        self.assertIsNotNone(parse_sync_args(["bogus"]).error)
        self.assertIsNotNone(parse_sync_args(["apply", "--bad"]).error)


class PlanAndApplyTest(unittest.TestCase):
    def _sample_inspect(self) -> _FakeInspect:
        gc_bytes = b"#!/usr/bin/python3\n# old gc\n"
        gc_dest = "/usr/local/sbin/docker-gc"
        swap_bytes = b"vm.swappiness = 60\n"
        swap_dest = "/etc/sysctl.d/99-swap-tuning.conf"
        tc_bytes = b"#!/usr/bin/python3\n# old tc\n"
        tc_dest = "/home/tester/.local/lib/cst/cst-toolchains"
        bt_bytes = b"#!/usr/bin/python3\n# up to date\n"
        bt_dest = "/home/tester/.local/lib/cst/bt-ldac-watch"

        root_rec = InstallRecord(
            files={
                gc_dest: RecordedFile(
                    "btrfs/docker-gc", "btrfs:docker-gc", ArtifactKind.PYTHON_ENTRY, _sha(gc_bytes), 0o755, Owner.ROOT
                ),
                swap_dest: RecordedFile(
                    "swap/sysctl/99-swap-tuning.conf",
                    "swap:core",
                    ArtifactKind.DATA,
                    _sha(swap_bytes),
                    0o644,
                    Owner.ROOT,
                ),
            },
            components={"btrfs:docker-gc": ComponentState.DONE, "swap:core": ComponentState.DONE},
        )
        user_rec = InstallRecord(
            files={
                tc_dest: RecordedFile(
                    "toolchains/cst-toolchains",
                    "toolchains:auto",
                    ArtifactKind.PYTHON_ENTRY,
                    _sha(tc_bytes),
                    0o755,
                    Owner.USER,
                ),
                bt_dest: RecordedFile(
                    "bluetooth/bt-ldac-watch",
                    "bluetooth:ldac",
                    ArtifactKind.PYTHON_ENTRY,
                    _sha(bt_bytes),
                    0o755,
                    Owner.USER,
                ),
            },
            components={"toolchains:auto": ComponentState.DONE, "bluetooth:ldac": ComponentState.DONE},
        )
        return _FakeInspect(
            root_record=(root_rec, None),
            user_record=(user_rec, None),
            disk={gc_dest: gc_bytes, swap_dest: swap_bytes, tc_dest: tc_bytes, bt_dest: bt_bytes},
            desired_manifest={
                ("btrfs/docker-gc", ()): _sha(b"new gc"),
                ("swap/sysctl/99-swap-tuning.conf", ()): _sha(b"new swap"),
                ("toolchains/cst-toolchains", ()): _sha(b"new tc"),
                ("bluetooth/bt-ldac-watch", ()): _sha(bt_bytes),
            },
        )

    def test_plan_splits_safe_and_risky_and_counts_up_to_date(self) -> None:
        plan = plan_host_sync(self._sample_inspect(), SyncArgs(SyncAction.PLAN))
        self.assertEqual(
            [s.argv for s in plan.steps_to_run], [("btrfs", "docker-gc", "enable"), ("toolchains", "auto", "on")]
        )
        self.assertEqual([s.argv for s in plan.skipped_risky], [("swap", "apply")])
        self.assertEqual(plan.skipped_modified, ())
        self.assertEqual(plan.up_to_date_components, 1)
        rendered = format_sync_plan(plan)
        self.assertIn("cst btrfs docker-gc enable [root]", rendered)
        self.assertIn("cst toolchains auto on [user]", rendered)
        self.assertIn("--include-risky", rendered)
        self.assertIn("Dry run: nothing is changed.", rendered)

    def test_apply_as_user_runs_user_steps_directly_and_single_sudo_subpass(self) -> None:
        insp = self._sample_inspect()
        exe = _FakeExec()
        report = run_apply(insp, exe, SyncArgs(SyncAction.APPLY))
        self.assertTrue(report.ok)
        self.assertEqual(exe.calls, [(("toolchains", "auto", "on"), None)])
        self.assertEqual(len(exe.subpasses), 1)
        self.assertEqual(len(report.plan.skipped_risky), 1)
        out = format_apply_report(report)
        self.assertIn("2 updated, 0 failed, 1 skipped (risky)", out)
        self.assertIn("cst sync apply --include-risky", out)

    def test_apply_user_only_never_calls_sudo_and_noop_is_silent_on_sudo(self) -> None:
        insp = self._sample_inspect()
        exe = _FakeExec()
        # Simulate up-to-date root record:
        insp.root_record = (
            InstallRecord(
                files={
                    "/usr/local/sbin/docker-gc": RecordedFile(
                        "btrfs/docker-gc",
                        "btrfs:docker-gc",
                        ArtifactKind.PYTHON_ENTRY,
                        _sha(b"new gc"),
                        0o755,
                        Owner.ROOT,
                    )
                },
                components={"btrfs:docker-gc": ComponentState.DONE},
            ),
            None,
        )
        insp.disk["/usr/local/sbin/docker-gc"] = b"new gc"
        report = run_apply(insp, exe, SyncArgs(SyncAction.APPLY))
        self.assertTrue(report.ok)
        self.assertEqual(exe.subpasses, [])
        self.assertEqual(exe.calls, [(("toolchains", "auto", "on"), None)])

        # Now user also up to date -> nothing runs, no sudo.
        exe2 = _FakeExec()
        insp.user_record = (None, None)
        insp.disk.pop("/home/tester/.local/lib/cst/cst-toolchains", None)
        report2 = run_apply(insp, exe2, SyncArgs(SyncAction.APPLY))
        self.assertTrue(report2.ok)
        self.assertEqual(exe2.calls, [])
        self.assertEqual(exe2.subpasses, [])

    def test_apply_as_root_runs_root_then_steps_down_for_user(self) -> None:
        insp = self._sample_inspect()
        insp._euid = 0
        exe = _FakeExec()
        report = run_apply(insp, exe, SyncArgs(SyncAction.APPLY, include_risky=True))
        self.assertTrue(report.ok)
        self.assertEqual(exe.subpasses, [])
        self.assertEqual(
            exe.calls,
            [
                (("btrfs", "docker-gc", "enable"), None),
                (("swap", "apply"), None),
                (("toolchains", "auto", "on"), "tester"),
            ],
        )

    def test_root_only_subpass_executes_only_root_steps(self) -> None:
        insp = self._sample_inspect()
        insp._euid = 0
        exe = _FakeExec()
        report = run_apply(insp, exe, SyncArgs(SyncAction.APPLY, root_only=True))
        self.assertTrue(report.ok)
        self.assertTrue(report.root_only)
        self.assertEqual(exe.calls, [(("btrfs", "docker-gc", "enable"), None)])
        out = format_apply_report(report)
        self.assertIn("cst sync apply (root): 1 updated, 0 failed.", out)
        self.assertNotIn("Skipped by precaution", out)

    def test_root_subpass_failure_reports_subpass_without_fabricating_step_results(self) -> None:
        insp = self._sample_inspect()
        exe = _FakeExec(subpass_rc=2)
        report = run_apply(insp, exe, SyncArgs(SyncAction.APPLY, include_risky=True))
        self.assertFalse(report.ok)
        self.assertEqual(report.root_subpass_rc, 2)
        # Only user steps have individual StepResults; root steps are covered by root_subpass_rc.
        self.assertEqual(len(report.results), 1)
        out = format_apply_report(report)
        self.assertIn("1 updated, 1 failed", out)
        self.assertIn("FAILED (2): root subpass (cst btrfs docker-gc enable, cst swap apply)", out)

    def test_modified_files_skipped_unless_overwrite_modified(self) -> None:
        gc_dest = "/usr/local/sbin/docker-gc"
        rec_sha = _sha(b"installed")
        insp = _FakeInspect(
            root_record=(
                InstallRecord(
                    files={
                        gc_dest: RecordedFile(
                            "btrfs/docker-gc", "btrfs:docker-gc", ArtifactKind.PYTHON_ENTRY, rec_sha, 0o755, Owner.ROOT
                        )
                    },
                    components={"btrfs:docker-gc": ComponentState.DONE},
                ),
                None,
            ),
            disk={gc_dest: b"edited by hand"},
            desired_manifest={("btrfs/docker-gc", ()): rec_sha},
        )
        plan1 = plan_host_sync(insp, SyncArgs(SyncAction.PLAN))
        self.assertEqual(len(plan1.skipped_modified), 1)
        self.assertEqual(plan1.steps_to_run, ())
        plan2 = plan_host_sync(insp, SyncArgs(SyncAction.PLAN, overwrite_modified=True))
        self.assertEqual(len(plan2.steps_to_run), 1)

    def test_deduplicates_shared_commands_and_keeps_deterministic_order(self) -> None:
        # btrfs:alerts and btrfs:core both share `cst btrfs install`, and must run before btrfs:snapshots
        notify = "/usr/local/sbin/btrfs-notify"
        snap = "/usr/local/sbin/btrfs-snapshot"
        health = "/usr/local/sbin/btrfs-health-check"
        old_sha = _sha(b"old")
        new_sha = _sha(b"new")
        obs = SyncHostObservation(
            root_record=InstallRecord(
                files={
                    snap: RecordedFile(
                        "btrfs/btrfs-snapshot", "btrfs:snapshots", ArtifactKind.SHELL, old_sha, 0o755, Owner.ROOT
                    ),
                    notify: RecordedFile(
                        "btrfs/btrfs-notify", "btrfs:alerts", ArtifactKind.SHELL, old_sha, 0o755, Owner.ROOT
                    ),
                    health: RecordedFile(
                        "btrfs/btrfs-health-check", "btrfs:core", ArtifactKind.PYTHON_ENTRY, old_sha, 0o755, Owner.ROOT
                    ),
                },
                components={
                    "btrfs:snapshots": ComponentState.DONE,
                    "btrfs:alerts": ComponentState.DONE,
                    "btrfs:core": ComponentState.DONE,
                },
            ),
            user_record=None,
            user_home="/home/tester",
            disk_sha256={snap: old_sha, notify: old_sha, health: old_sha},
            missing_dests=frozenset(),
            desired_manifest_sha256={
                ("btrfs/btrfs-snapshot", ()): new_sha,
                ("btrfs/btrfs-notify", ()): new_sha,
                ("btrfs/btrfs-health-check", ()): new_sha,
            },
            desired_generated_sha256={},
        )
        plan = plan_sync(obs, SyncArgs(SyncAction.PLAN))
        self.assertEqual([s.argv for s in plan.steps_to_run], [("btrfs", "install"), ("btrfs", "snapshots", "refresh")])
        self.assertEqual(
            [c.component for c in plan.steps_to_run[0].components], [Component.BTRFS_CORE, Component.BTRFS_ALERTS]
        )

    def test_missing_pending_and_pre_record_files_trigger_sync(self) -> None:
        obs = SyncHostObservation(
            root_record=InstallRecord(
                files={
                    "/etc/systemd/system/docker-gc.timer": RecordedFile(
                        "btrfs/units/docker-gc.timer",
                        "btrfs:docker-gc",
                        ArtifactKind.UNIT,
                        _sha(b"x"),
                        0o644,
                        Owner.ROOT,
                    )
                },
                components={"btrfs:docker-gc": ComponentState.DONE, "btrfs:iocost": ComponentState.PENDING},
            ),
            user_record=None,
            user_home="/home/tester",
            disk_sha256={"/etc/sysctl.d/99-swap-tuning.conf": _sha(b"old")},
            missing_dests=frozenset({"/etc/systemd/system/docker-gc.timer"}),
            desired_manifest_sha256={
                ("btrfs/units/docker-gc.timer", ()): _sha(b"x"),
                ("swap/sysctl/99-swap-tuning.conf", ()): _sha(b"new"),
            },
            desired_generated_sha256={},
        )
        plan = plan_sync(obs, SyncArgs(SyncAction.PLAN, include_risky=True))
        comps = {c.component: c for s in plan.steps_to_run for c in s.components}
        self.assertEqual(comps[Component.BTRFS_DOCKER_GC].files[0].drift, FileDrift.MISSING)
        self.assertTrue(comps[Component.BTRFS_IOCOST].pending)
        self.assertEqual(comps[Component.SWAP_CORE].files[0].drift, FileDrift.PRE_RECORD)

    def test_refuses_in_container_on_apply_and_on_bad_record(self) -> None:
        insp_box = _FakeInspect(container=True)
        exe = _FakeExec()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = run_sync_cli(["apply"], entry_file=__file__, inspect=insp_box, executor=exe)
        self.assertEqual(rc, 1)
        self.assertIn("must run on the host", err.getvalue())

        insp_bad_rec = _FakeInspect(root_record=(None, "/var/lib/cst/installed.json: written by a newer CST"))
        out2, err2 = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
            rc2 = run_sync_cli(["plan"], entry_file=__file__, inspect=insp_bad_rec, executor=exe)
        self.assertEqual(rc2, 1)
        self.assertIn("written by a newer CST", err2.getvalue())

    def test_cli_failure_in_step_continues_and_returns_1(self) -> None:
        insp = self._sample_inspect()
        exe = _FakeExec(results={("toolchains", "auto", "on"): 1}, subpass_rc=0)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = run_sync_cli(["apply"], entry_file=__file__, inspect=insp, executor=exe)
        self.assertEqual(rc, 1)
        self.assertIn("FAILED (1): cst toolchains auto on", out.getvalue())

    def test_desired_generated_bytes_legacy_none_and_invalid_values(self) -> None:
        # Legacy record with missing values does not falsely flag drift.
        legacy_vm = RecordedFile(None, "vm:backup-timer", GeneratedKind.TEXT, "0" * 64, 0o644, Owner.ROOT, None)
        self.assertIsNone(desired_generated_bytes("/etc/systemd/system/cst-vm-backup@.service", legacy_vm))
        # Valid parameterized generated record renders deterministically.
        ok_vm = replace(legacy_vm, values={"program": "/usr/local/sbin/cst-vm", "stop_timeout": "30min"})
        rendered = desired_generated_bytes("/etc/systemd/system/cst-vm-backup@.service", ok_vm)
        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertIn(b"/usr/local/sbin/cst-vm backup %i --nas", rendered)
        # HostSyncInspectAdapter builds cleanly from checkout
        repo_entry = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "sync", "cst-sync")
        adapter = HostSyncInspectAdapter(repo_entry)
        self.assertIsNotNone(adapter.desired_manifest_sha256("btrfs/docker-gc", None))

    def test_alerts_modified_holds_back_snapshots_refresh(self) -> None:
        snap = "/usr/local/sbin/btrfs-snapshot"
        notify = "/usr/local/sbin/btrfs-notify"
        rec_sha = _sha(b"installed")
        insp = _FakeInspect(
            root_record=(
                InstallRecord(
                    files={
                        snap: RecordedFile(
                            "btrfs/btrfs-snapshot",
                            "btrfs:snapshots",
                            ArtifactKind.SHELL,
                            _sha(b"old"),
                            0o755,
                            Owner.ROOT,
                        ),
                        notify: RecordedFile(
                            "btrfs/btrfs-notify", "btrfs:alerts", ArtifactKind.SHELL, rec_sha, 0o755, Owner.ROOT
                        ),
                    },
                    components={"btrfs:snapshots": ComponentState.DONE, "btrfs:alerts": ComponentState.DONE},
                ),
                None,
            ),
            disk={snap: b"old", notify: b"hand edited notify"},
            desired_manifest={("btrfs/btrfs-snapshot", ()): _sha(b"new"), ("btrfs/btrfs-notify", ()): rec_sha},
        )
        plan = plan_host_sync(insp, SyncArgs(SyncAction.PLAN))
        self.assertEqual(plan.steps_to_run, ())
        self.assertEqual(
            {s.argv for s in plan.skipped_modified}, {("btrfs", "install"), ("btrfs", "snapshots", "refresh")}
        )
        plan_ov = plan_host_sync(insp, SyncArgs(SyncAction.PLAN, overwrite_modified=True))
        self.assertEqual(
            [s.argv for s in plan_ov.steps_to_run], [("btrfs", "install"), ("btrfs", "snapshots", "refresh")]
        )

    def test_root_only_refuses_non_root_even_when_up_to_date_and_root_without_sudo_user_notes(self) -> None:
        insp = _FakeInspect()
        exe = _FakeExec()
        with self.assertRaises(Refused):
            run_apply(insp, exe, SyncArgs(SyncAction.APPLY, root_only=True))

        insp_root = _FakeInspect(_euid=0, user=("", 0, ""))
        plan = plan_host_sync(insp_root, SyncArgs(SyncAction.PLAN))
        self.assertTrue(any("SUDO_USER is unset" in n for n in plan.notes))
        self.assertIn("NOTE: SUDO_USER is unset", format_sync_plan(plan))
        rep_outer = run_apply(insp_root, exe, SyncArgs(SyncAction.APPLY))
        self.assertIn("NOTE: SUDO_USER is unset", format_apply_report(rep_outer))
        rep_root_only = run_apply(insp_root, exe, SyncArgs(SyncAction.APPLY, root_only=True))
        self.assertNotIn("NOTE: SUDO_USER is unset", format_apply_report(rep_root_only))


class _FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str] | None]] = []

    def run(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None, **kw: object) -> CommandResult:
        del kw
        self.calls.append((tuple(argv), env))
        return CommandResult(returncode=0, stdout="", stderr="", argv=tuple(argv))


class AdapterTest(unittest.TestCase):
    def test_stat_and_hash_distinguishes_missing_symlink_unreadable_and_regular(self) -> None:
        repo_entry = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "sync", "cst-sync")
        adapter = HostSyncInspectAdapter(repo_entry, env={"HOME": "/home/tester"})
        with tempfile.TemporaryDirectory(prefix="cst-sync-adapt-") as tmp:
            reg = os.path.join(tmp, "regular.txt")
            with open(reg, "wb") as fh:
                fh.write(b"hello")
            dangling = os.path.join(tmp, "dangling")
            os.symlink(os.path.join(tmp, "no-such-target"), dangling)
            missing = os.path.join(tmp, "missing.txt")

            with mock.patch("sync.adapters.host_view_path", side_effect=lambda _ctx, p: p):
                self.assertEqual(adapter.stat_and_hash(reg), (True, _sha(b"hello")))
                self.assertEqual(adapter.stat_and_hash(missing), (False, None))
                self.assertEqual(adapter.stat_and_hash(dangling), (True, None))
                with mock.patch("sync.adapters.os.lstat", side_effect=PermissionError("denied")):
                    self.assertEqual(
                        adapter.stat_and_hash("/etc/polkit-1/rules.d/50-cpu-setup-game.rules"), (True, None)
                    )

    def test_exec_adapter_builds_step_down_and_root_subpass_commands(self) -> None:
        repo_entry = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "sync", "cst-sync")
        runner = _FakeRunner()
        exe = HostSyncExecAdapter(
            repo_entry, prog="cst sync", env={"CST_NAME": "cst", "LC_CTYPE": "C.UTF-8"}, runner=runner
        )

        self.assertEqual(exe.run_cst_subcommand(("btrfs", "docker-gc", "enable")), 0)
        direct_argv, direct_env = runner.calls[-1]
        self.assertEqual(direct_argv[1:], ("btrfs", "docker-gc", "enable"))
        self.assertEqual(direct_env, {"CST_NAME": "cst"})

        with mock.patch("sync.adapters.pwd.getpwnam", return_value=mock.Mock(pw_uid=1000, pw_dir="/home/tester")):
            self.assertEqual(exe.run_cst_subcommand(("toolchains", "auto", "on"), as_user="tester"), 0)
        step_argv, _ = runner.calls[-1]
        self.assertEqual(step_argv[:4], ("runuser", "-u", "tester", "--"))
        self.assertIn("XDG_RUNTIME_DIR=/run/user/1000", step_argv)
        self.assertEqual(step_argv[-3:], ("toolchains", "auto", "on"))

        with mock.patch.dict(os.environ, {"LC_CTYPE": "pt_BR.UTF-8"}, clear=False):
            self.assertEqual(
                exe.run_root_subpass(SyncArgs(SyncAction.APPLY, include_risky=True, overwrite_modified=True)), 0
            )
        sub_argv, _ = runner.calls[-1]
        self.assertEqual(sub_argv[:3], ("sudo", "--", "env"))
        self.assertIn("CST_LC_CTYPE==pt_BR.UTF-8", sub_argv)
        self.assertIn("--root-only", sub_argv)
        self.assertIn("--include-risky", sub_argv)
        self.assertIn("--overwrite-modified", sub_argv)


if __name__ == "__main__":
    unittest.main()

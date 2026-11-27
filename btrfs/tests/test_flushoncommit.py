# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs flushoncommit` (`btrfs/flushoncommit/`)."""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import os
import shutil
import tempfile
from typing import override
import unittest
from unittest import mock

from btrfs.compress import adapters as compress_adapters
from btrfs.compress.adapters import read_root_option_snapshot
from btrfs.compress.application import FSTAB_RELOAD_PENDING, FSTAB_RELOAD_UNKNOWN
from btrfs.compress.domain import (
    CompressSnapshot,
    LayerStatus,
    TopMountInfo,
    parse_proc_mounts,
    parse_top_mount_unit,
    pick_root_mounts,
)
from btrfs.flushoncommit.adapters import FLUSH_TOOL
from btrfs.flushoncommit.cli import FlushCliDeps, run_flush_cli
from btrfs.flushoncommit.domain import (
    FlushAction,
    FlushParsedArgs,
    assess_flush_coherence,
    booted_flush_diff,
    build_flush_plan,
    effective_flushoncommit,
    format_flushoncommit_label,
    has_flushoncommit_token,
    parse_flush_args,
    rewrite_flushoncommit_options,
)
from btrfs.tests.fakes import CacheTestCtx, LayerHost, LayerHostState
from cst_core.domain.errors import Refused
from tests.repo_files import REPO

SAMPLE_UUID = "afed93e2-f4ac-43de-8a48-751b552435c7"

SAMPLE_FSTAB = (
    f"UUID={SAMPLE_UUID} /home btrfs subvol=home,degraded,compress-force=zstd:1,noatime,flushoncommit,commit=60 0 0\n"
    f"UUID={SAMPLE_UUID} /var  btrfs subvol=var,degraded,compress-force=zstd:1,noatime,flushoncommit,commit=60 0 0\n"
    "UUID=1111-2222 /boot/efi vfat umask=0077 0 2\n"
)

SAMPLE_KARGS = (
    f"ostree=/ostree/boot.1/fedora/abc/0 rhgb quiet root=UUID={SAMPLE_UUID} rw "
    "rootflags=subvol=root,degraded,compress-force=zstd:1,noatime,flushoncommit,commit=60\n"
)

SAMPLE_PROC_MOUNTS = (
    "/dev/dm-0 /sysroot btrfs ro,seclabel,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit,commit=60,subvol=/root 0 0\n"
    "/dev/dm-0 /var btrfs rw,seclabel,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit,commit=60,subvol=/var 0 0\n"
    "/dev/dm-0 /var/home btrfs rw,seclabel,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit,commit=60,subvol=/home 0 0\n"
)

SAMPLE_TOP_MOUNT = (
    "[Mount]\n"
    f"What=/dev/disk/by-uuid/{SAMPLE_UUID}\n"
    "Where=/var/mnt/btrfs-top\n"
    "Type=btrfs\n"
    "Options=subvolid=5,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit,commit=60\n"
)


@dataclass
class _FakeState(LayerHostState):
    root_uuid: str = SAMPLE_UUID
    fstab: str | None = SAMPLE_FSTAB
    proc_mounts: str | None = SAMPLE_PROC_MOUNTS
    proc_cmdline: str | None = SAMPLE_KARGS
    kargs_text: str = SAMPLE_KARGS
    top_mount: TopMountInfo | None = field(default_factory=lambda: parse_top_mount_unit(SAMPLE_TOP_MOUNT))


class _FakeHost(LayerHost):
    def __init__(self, state: _FakeState | None = None, *, torn: str | None = None) -> None:
        super().__init__(_FakeState() if state is None else state, torn=torn)

    @override
    def remounted(self, mounts: str, target: str, option: str) -> str:
        enable = option == "flushoncommit"
        lines = [line.split() for line in mounts.splitlines()]
        device = next((f[0] for f in lines if f[1] == target), None)
        updated = [
            f"{f[0]} {f[1]} btrfs {rewrite_flushoncommit_options(f[3], enable=enable) if f[0] == device else f[3]} 0 0"
            for f in lines
        ]
        return "\n".join(updated) + "\n"

    @override
    def top_mount_call(self, uuid: str, options: str) -> str:
        del options
        return f"update_top_mount:{uuid}"


class FlushDomainTests(unittest.TestCase):
    def test_parse_args_and_validation(self) -> None:
        self.assertEqual(parse_flush_args([]), FlushParsedArgs(FlushAction.STATUS))
        self.assertEqual(parse_flush_args(["plan"]), FlushParsedArgs(FlushAction.PLAN))
        self.assertEqual(parse_flush_args(["plan", "--revert"]), FlushParsedArgs(FlushAction.PLAN, revert=True))
        self.assertEqual(parse_flush_args(["apply", "--yes"]), FlushParsedArgs(FlushAction.APPLY, yes=True))
        self.assertEqual(
            parse_flush_args(["revert", "--yes"]), FlushParsedArgs(FlushAction.REVERT, revert=True, yes=True)
        )
        for argv in (["--help"], ["plan", "--help"], ["status", "-h"]):
            with self.subTest(argv=argv):
                self.assertEqual(parse_flush_args(argv), FlushParsedArgs(FlushAction.HELP))
        for bad in (["bogus"], ["status", "extra"], ["plan", "--yes"], ["apply", "--revert"], ["apply", "--bad"]):
            with self.subTest(bad=bad):
                self.assertEqual(parse_flush_args(bad).code, 2)

    def test_option_parsing_and_rewriting(self) -> None:
        self.assertFalse(effective_flushoncommit("noatime,ssd"))
        self.assertFalse(has_flushoncommit_token("noatime,ssd"))
        self.assertTrue(effective_flushoncommit("noatime,\tflushoncommit "))
        self.assertFalse(effective_flushoncommit("flushoncommit,noatime,noflushoncommit"))
        self.assertTrue(effective_flushoncommit("noflushoncommit,flushoncommit"))
        self.assertEqual(format_flushoncommit_label(enabled=None), "(none)")
        self.assertEqual(format_flushoncommit_label(enabled=False), "no flushoncommit (default)")
        self.assertEqual(format_flushoncommit_label(enabled=False, explicit=True), "noflushoncommit")
        self.assertEqual(format_flushoncommit_label(enabled=True), "flushoncommit")

        self.assertEqual(
            rewrite_flushoncommit_options("subvol=var,flushoncommit,noatime,commit=60", enable=False),
            "subvol=var,noatime,commit=60",
        )
        self.assertEqual(
            rewrite_flushoncommit_options("subvol=var,noflushoncommit,noatime", enable=False),
            "subvol=var,noatime",
        )
        self.assertEqual(
            rewrite_flushoncommit_options("subvol=var,noatime,commit=60", enable=True),
            "subvol=var,noatime,commit=60,flushoncommit",
        )
        self.assertEqual(
            rewrite_flushoncommit_options("subvol=var,noflushoncommit,noatime,flushoncommit", enable=True),
            "subvol=var,flushoncommit,noatime",
        )
        self.assertEqual(rewrite_flushoncommit_options("flushoncommit", enable=False), "defaults")
        self.assertEqual(rewrite_flushoncommit_options("", enable=False), "")

    def test_coherence_booted_diff_and_plan(self) -> None:
        empty_coh = assess_flush_coherence(
            CompressSnapshot(
                is_ostree=False,
                root_uuid="",
                fstab_text=None,
                kargs_text=None,
                proc_mounts_text=None,
                top_mount=None,
            )
        )
        self.assertTrue(empty_coh.coherent)
        self.assertIsNone(empty_coh.enabled)

        snap = CompressSnapshot(
            is_ostree=True,
            root_uuid=SAMPLE_UUID,
            fstab_text=SAMPLE_FSTAB,
            kargs_text=SAMPLE_KARGS,
            proc_mounts_text=SAMPLE_PROC_MOUNTS,
            top_mount=parse_top_mount_unit(SAMPLE_TOP_MOUNT),
        )
        coh = assess_flush_coherence(snap)
        self.assertTrue(coh.coherent)
        self.assertTrue(coh.enabled)

        drift_coh = assess_flush_coherence(
            CompressSnapshot(
                is_ostree=True,
                root_uuid=SAMPLE_UUID,
                fstab_text=SAMPLE_FSTAB.replace(",flushoncommit", ""),
                kargs_text=SAMPLE_KARGS,
                proc_mounts_text=SAMPLE_PROC_MOUNTS,
                top_mount=None,
            )
        )
        self.assertFalse(drift_coh.coherent)
        self.assertIsNone(drift_coh.enabled)
        self.assertIn("drift across layers:", drift_coh.summary)

        # Explicit `noflushoncommit` on layers is cleaned on apply; contradictory `noflushoncommit` + `flushoncommit` on top-mount is cleaned on revert.
        noflush_snap = CompressSnapshot(
            is_ostree=True,
            root_uuid=SAMPLE_UUID,
            fstab_text=SAMPLE_FSTAB.replace("flushoncommit", "noflushoncommit"),
            kargs_text=SAMPLE_KARGS.replace("flushoncommit", "noflushoncommit"),
            proc_mounts_text=SAMPLE_PROC_MOUNTS.replace(",flushoncommit", ""),
            top_mount=parse_top_mount_unit(SAMPLE_TOP_MOUNT.replace("flushoncommit", "noflushoncommit,flushoncommit")),
        )
        noflush_apply = build_flush_plan(noflush_snap, revert=False)
        self.assertEqual(
            [lp.status for lp in noflush_apply.layers],
            [LayerStatus.CHANGE, LayerStatus.CHANGE, LayerStatus.OK, LayerStatus.CHANGE],
        )
        self.assertNotIn("noflushoncommit", noflush_apply.new_fstab_text or "")
        contradict_revert = build_flush_plan(noflush_snap, revert=True)
        self.assertEqual(contradict_revert.layers[3].status, LayerStatus.CHANGE)
        self.assertNotIn("noflushoncommit", contradict_revert.new_top_mount_options or "")
        self.assertIn("flushoncommit", contradict_revert.new_top_mount_options or "")

        plan = build_flush_plan(snap, revert=False)
        self.assertTrue(plan.has_changes)
        self.assertEqual([lp.status for lp in plan.layers], [LayerStatus.CHANGE] * 4)
        self.assertNotIn("flushoncommit", plan.new_fstab_text or "")
        self.assertIn("commit=60", plan.new_fstab_text or "")
        self.assertNotIn("flushoncommit", plan.new_top_mount_options or "")

        nxt_kargs = SAMPLE_KARGS.replace(",flushoncommit", "")
        nxt_snap = CompressSnapshot(
            is_ostree=True,
            root_uuid=SAMPLE_UUID,
            fstab_text=SAMPLE_FSTAB,
            kargs_text=nxt_kargs,
            proc_mounts_text=SAMPLE_PROC_MOUNTS,
            top_mount=None,
        )
        self.assertIsNotNone(booted_flush_diff(nxt_snap, SAMPLE_KARGS, kargs_ok=True))
        self.assertIsNone(booted_flush_diff(nxt_snap, nxt_kargs, kargs_ok=True))

        # Separate /var filesystem left untouched by apply.
        other_uuid = "0b5e6a1d-0000-4000-8000-00000000beef"
        var_line = f"UUID={other_uuid} /var btrfs subvol=var,noatime,flushoncommit 0 0\n"
        sep_snap = CompressSnapshot(
            is_ostree=False,
            root_uuid=SAMPLE_UUID,
            fstab_text=SAMPLE_FSTAB.splitlines(keepends=True)[0] + var_line,
            kargs_text=None,
            proc_mounts_text=SAMPLE_PROC_MOUNTS,
            top_mount=None,
        )
        sep_plan = build_flush_plan(sep_snap, revert=False)
        self.assertIn(var_line, sep_plan.new_fstab_text or "")
        self.assertTrue(sep_plan.left_alone_notes)


class FlushExecutionTests(unittest.TestCase):
    def test_apply_idempotent_revert_and_rollback(self) -> None:
        host = _FakeHost()
        rc, out = FLUSH_TOOL.execute(host, FlushParsedArgs(FlushAction.APPLY, yes=True))
        self.assertEqual(rc, 0, out)
        self.assertNotIn("flushoncommit", host.s.fstab or "")
        self.assertNotIn("flushoncommit", host.s.kargs_text)
        self.assertNotIn("flushoncommit", host.s.proc_mounts or "")
        self.assertIsNotNone(host.s.top_mount)
        assert host.s.top_mount is not None
        self.assertNotIn("flushoncommit", host.s.top_mount.options)

        # Second apply is a clean no-op.
        rc2, out2 = FLUSH_TOOL.execute(host, FlushParsedArgs(FlushAction.APPLY, yes=True))
        self.assertEqual(rc2, 0, out2)
        self.assertTrue(any("nothing to do" in line for line in out2), out2)

        # Revert re-enables flushoncommit everywhere.
        rc_rev, out_rev = FLUSH_TOOL.execute(host, FlushParsedArgs(FlushAction.REVERT, revert=True, yes=True))
        self.assertEqual(rc_rev, 0, out_rev)
        self.assertIn("flushoncommit", host.s.fstab or "")
        self.assertIn("flushoncommit", host.s.kargs_text)
        self.assertIn("flushoncommit", host.s.proc_mounts or "")
        self.assertIn("flushoncommit", host.s.top_mount.options)

        # Rollback on kargs failure puts fstab back.
        failing_kargs = _FakeHost(_FakeState(fail_kargs="busy"))
        rc_fail, out_fail = FLUSH_TOOL.execute(failing_kargs, FlushParsedArgs(FlushAction.APPLY, yes=True))
        self.assertEqual(rc_fail, 1)
        self.assertIn("previous configuration restored.", "\n".join(out_fail))
        self.assertEqual(failing_kargs.s.fstab, SAMPLE_FSTAB)

    def test_rollback_failures_torn_writes_non_ostree_and_confirm(self) -> None:
        # Remount failure, post-remount verification mismatch, and top-mount failure all roll back earlier layers.
        for bad_state in (
            _FakeState(fail_remount="EIO"),
            _FakeState(fake_remount_effect=False),
            _FakeState(fail_top_mount=OSError("unit invalid")),
        ):
            with self.subTest(bad_state=bad_state):
                bad_host = _FakeHost(bad_state)
                rc_b, out_b = FLUSH_TOOL.execute(bad_host, FlushParsedArgs(FlushAction.APPLY, yes=True))
                self.assertEqual(rc_b, 1)
                self.assertIn("previous configuration restored.", "\n".join(out_b))
                self.assertEqual(bad_host.s.fstab, SAMPLE_FSTAB)
                self.assertEqual(bad_host.s.kargs_text, SAMPLE_KARGS)
                self.assertTrue(
                    all(
                        effective_flushoncommit(m.options)
                        for m in pick_root_mounts(parse_proc_mounts(bad_host.s.proc_mounts or ""))
                    )
                )

        # Torn writes on fstab, kargs, and top-mount roll back cleanly.
        for torn in ("fstab", "kargs", "top"):
            with self.subTest(torn=torn):
                torn_host = _FakeHost(_FakeState(), torn=torn)
                rc_t, out_t = FLUSH_TOOL.execute(torn_host, FlushParsedArgs(FlushAction.APPLY, yes=True))
                self.assertEqual(rc_t, 1)
                self.assertIn("previous configuration restored.", "\n".join(out_t))
                self.assertEqual(torn_host.s.fstab, SAMPLE_FSTAB)

        # Non-ostree host skips kargs cleanly on status, plan, apply, and revert.
        non_ostree = _FakeHost(_FakeState(is_ostree=False, proc_cmdline="", kargs_text="", top_mount=None))
        rc_a, _ = FLUSH_TOOL.execute(non_ostree, FlushParsedArgs(FlushAction.APPLY, yes=True))
        self.assertEqual(rc_a, 0)
        self.assertNotIn("flushoncommit", non_ostree.s.fstab or "")
        self.assertNotIn("flushoncommit", non_ostree.s.proc_mounts or "")
        non_ostree_snap, _, _ = read_root_option_snapshot(non_ostree)
        self.assertEqual(build_flush_plan(non_ostree_snap).layers[1].status, LayerStatus.NOT_APPLICABLE)
        rc_r, _ = FLUSH_TOOL.execute(non_ostree, FlushParsedArgs(FlushAction.REVERT, revert=True, yes=True))
        self.assertEqual(rc_r, 0)
        self.assertIn("flushoncommit", non_ostree.s.fstab or "")

        # Interactive confirmation refusal & abort without --yes.
        refused_host = _FakeHost(_FakeState())
        with mock.patch.object(refused_host, "confirm", side_effect=Refused("no tty")):
            rc_ref, out_ref = FLUSH_TOOL.execute(refused_host, FlushParsedArgs(FlushAction.APPLY, yes=False))
        self.assertEqual(rc_ref, 1)
        self.assertTrue(any("pass --yes" in line for line in out_ref))
        self.assertEqual(refused_host.s.fstab, SAMPLE_FSTAB)

        abort_host = _FakeHost(_FakeState(confirm_answer=False))
        rc_ab, out_ab = FLUSH_TOOL.execute(abort_host, FlushParsedArgs(FlushAction.APPLY, yes=False))
        self.assertEqual(rc_ab, 1)
        self.assertTrue(any("Aborted; nothing changed." in line for line in out_ab))
        self.assertEqual(abort_host.s.fstab, SAMPLE_FSTAB)

    def test_cli_seam_and_catch_up_dispatch(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-flush-test-")
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "etc", "systemd", "system"), exist_ok=True)
        os.makedirs(os.path.join(tmp, "proc"), exist_ok=True)
        with open(os.path.join(tmp, "etc", "fstab"), "w", encoding="utf-8") as fh:
            fh.write(SAMPLE_FSTAB)
        with open(os.path.join(tmp, "proc", "mounts"), "w", encoding="utf-8") as fh:
            fh.write(SAMPLE_PROC_MOUNTS)
        with open(os.path.join(tmp, "proc", "cmdline"), "w", encoding="utf-8") as fh:
            fh.write(SAMPLE_KARGS)

        out, err = io.StringIO(), io.StringIO()
        deps = FlushCliDeps(
            entry_file=os.path.join(REPO, "btrfs", "btrfs-flushoncommit"),
            env={"CST_BTRFS_FLUSH_ROOT": tmp, "CST_BTRFS_FLUSH_PROC": os.path.join(tmp, "proc")},
            out=out,
            err=err,
            ctx=CacheTestCtx(container=True, root=False),
        )
        self.assertEqual(run_flush_cli(["status"], deps=deps), 0, err.getvalue())
        self.assertIn("coherent (flushoncommit)", out.getvalue())

        out_p, err_p = io.StringIO(), io.StringIO()
        self.assertEqual(
            run_flush_cli(
                ["plan"],
                deps=FlushCliDeps(entry_file=deps.entry_file, env=deps.env, out=out_p, err=err_p, ctx=deps.ctx),
            ),
            0,
        )
        self.assertIn("Dry run", out_p.getvalue())

        out_r, err_r = io.StringIO(), io.StringIO()
        with mock.patch("sys.exit"):
            rc_r = run_flush_cli(
                ["apply", "--yes"],
                deps=FlushCliDeps(entry_file=deps.entry_file, env=deps.env, out=out_r, err=err_r, ctx=deps.ctx),
            )
        self.assertEqual(rc_r, 1)
        self.assertIn("toolbox/container", err_r.getvalue())

        host = _FakeHost(_FakeState(fstab_behind=True))
        with mock.patch.object(compress_adapters, "make_root_option_host", return_value=(host, "", {})):
            rc_s, lines_s = FLUSH_TOOL.dispatch(FlushParsedArgs(FlushAction.STATUS), FlushCliDeps())
        self.assertEqual(rc_s, 0)
        self.assertIn(f"  {FSTAB_RELOAD_PENDING}", lines_s)

        # Already off (default) format check:
        off_snap = CompressSnapshot(
            is_ostree=False,
            root_uuid=SAMPLE_UUID,
            fstab_text=SAMPLE_FSTAB.replace(",flushoncommit", ""),
            kargs_text=None,
            proc_mounts_text=SAMPLE_PROC_MOUNTS.replace(",flushoncommit", ""),
            top_mount=None,
        )
        off_status = "\n".join(FLUSH_TOOL.status_lines(off_snap, None, kargs=(True, "")))
        self.assertIn("coherent (no flushoncommit (default))", off_status)
        off_plan = "\n".join(
            FLUSH_TOOL.plan_lines(build_flush_plan(off_snap), "c", reload_pending=FSTAB_RELOAD_UNKNOWN)
        )
        self.assertIn(FSTAB_RELOAD_UNKNOWN, off_plan)
        self.assertNotIn("nothing to do", off_plan)
        self.assertIsNotNone(read_root_option_snapshot(host)[0])


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs commit-interval` (`btrfs/commit/`)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import io
import os
import shutil
import tempfile
import unittest
from unittest import mock

from btrfs.commit.adapters import execute_commit_change
from btrfs.commit.cli import CommitCliDeps, run_commit_cli
from btrfs.commit.domain import (
    CommitAction,
    CommitParsedArgs,
    assess_commit_coherence,
    build_commit_plan,
    effective_commit_seconds,
    has_explicit_commit,
    parse_commit_args,
    rewrite_commit_options,
)
from btrfs.compress.domain import CompressSnapshot, LayerStatus, TopMountInfo, parse_top_mount_unit
from cst_core.domain.errors import Refused
from tests.repo_files import REPO

SAMPLE_UUID = "afed93e2-f4ac-43de-8a48-751b552435c7"

SAMPLE_FSTAB = (
    f"UUID={SAMPLE_UUID} /home btrfs subvol=home,degraded,compress-force=zstd:1,noatime,flushoncommit 0 0\n"
    f"UUID={SAMPLE_UUID} /var  btrfs subvol=var,degraded,compress-force=zstd:1,noatime,flushoncommit 0 0\n"
    "UUID=1111-2222 /boot/efi vfat umask=0077 0 2\n"
)

SAMPLE_KARGS = (
    f"ostree=/ostree/boot.1/fedora/abc/0 rhgb quiet root=UUID={SAMPLE_UUID} rw "
    "rootflags=subvol=root,degraded,compress-force=zstd:1,noatime,flushoncommit\n"
)

SAMPLE_PROC_MOUNTS = (
    "/dev/dm-0 /sysroot btrfs ro,seclabel,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit,subvol=/root 0 0\n"
    "/dev/dm-0 /var btrfs rw,seclabel,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit,subvol=/var 0 0\n"
    "/dev/dm-0 /var/home btrfs rw,seclabel,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit,subvol=/home 0 0\n"
)

SAMPLE_TOP_MOUNT = (
    "[Mount]\n"
    f"What=/dev/disk/by-uuid/{SAMPLE_UUID}\n"
    "Where=/var/mnt/btrfs-top\n"
    "Type=btrfs\n"
    "Options=subvolid=5,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit\n"
)


class _FakeCtx:
    def __init__(self, *, container: bool = False, root: bool = True) -> None:
        self._container = container
        self._root = root

    def is_container(self) -> bool:
        return self._container

    def is_root(self) -> bool:
        return self._root

    def path(self, host_path: str) -> str:
        return f"/run/host/{host_path.lstrip('/')}" if self._container and host_path.startswith("/") else host_path

    def etc_dir(self) -> str:
        return "/run/host/etc" if self._container else "/etc"

    def host_run_dir(self, subpath: str) -> str:
        return f"/run/host/{subpath}" if self._container else f"/run/{subpath}"


@dataclass
class _FakeState:
    is_ostree: bool = True
    root_uuid: str = SAMPLE_UUID
    fstab: str | None = SAMPLE_FSTAB
    proc_mounts: str | None = SAMPLE_PROC_MOUNTS
    proc_cmdline: str | None = SAMPLE_KARGS
    kargs_ok: bool = True
    kargs_text: str = SAMPLE_KARGS
    kargs_err: str = ""
    top_mount: TopMountInfo | None = field(default_factory=lambda: parse_top_mount_unit(SAMPLE_TOP_MOUNT))
    confirm_answer: bool | None = True
    fail_kargs: str | None = None
    fail_remount: str | None = None
    fake_remount_effect: bool = True
    fail_top_mount: Exception | None = None


class _FakeHost:
    def __init__(self, state: _FakeState | None = None) -> None:
        self.s = _FakeState() if state is None else state
        self.calls: list[str] = []

    def is_ostree(self) -> bool:
        return self.s.is_ostree

    def root_uuid(self) -> str:
        return self.s.root_uuid

    def read_fstab(self) -> str | None:
        return self.s.fstab

    def read_proc_mounts(self) -> str | None:
        return self.s.proc_mounts

    def read_proc_cmdline(self) -> str | None:
        return self.s.proc_cmdline

    def read_kargs(self) -> tuple[bool, str, str]:
        return (self.s.kargs_ok, self.s.kargs_text, self.s.kargs_err)

    def read_top_mount(self) -> TopMountInfo | None:
        return self.s.top_mount

    def write_fstab(self, text: str) -> None:
        self.calls.append("write_fstab")
        self.s.fstab = text

    def update_kargs(self, edits: Sequence[tuple[str, str]]) -> tuple[bool, str]:
        self.calls.append(f"update_kargs:{edits}")
        if self.s.fail_kargs is not None:
            return False, self.s.fail_kargs
        for old_tok, new_tok in edits:
            self.s.kargs_text = self.s.kargs_text.replace(old_tok, new_tok, 1)
        return True, ""

    def remount(self, target: str, option: str) -> tuple[bool, str]:
        self.calls.append(f"remount:{target}:{option}")
        if self.s.fail_remount is not None:
            return False, self.s.fail_remount
        if self.s.fake_remount_effect and self.s.proc_mounts is not None:
            val = int(option.removeprefix("commit="))
            target_opt = None if val == 30 else val
            # The kernel changes every mount of the remounted filesystem (its device), and only those.
            lines = [line.split() for line in self.s.proc_mounts.splitlines()]
            device = next((f[0] for f in lines if f[1] == target), None)
            updated = [
                f"{f[0]} {f[1]} btrfs {rewrite_commit_options(f[3], target_opt) if f[0] == device else f[3]} 0 0"
                for f in lines
            ]
            self.s.proc_mounts = "\n".join(updated) + "\n"
        return True, ""

    def update_top_mount(self, uuid: str, options: str) -> None:
        self.calls.append(f"update_top_mount:{uuid}")
        if self.s.fail_top_mount is not None:
            raise self.s.fail_top_mount
        self.s.top_mount = TopMountInfo(installed=True, uuid=uuid, options=options)

    def confirm(self, prompt: str) -> bool:
        self.calls.append(f"confirm:{prompt}")
        if self.s.confirm_answer is None:
            raise Refused("apply asks for confirmation: run it in a terminal")
        return self.s.confirm_answer


class CommitDomainTests(unittest.TestCase):
    def test_parse_args_and_validation(self) -> None:
        self.assertEqual(parse_commit_args([]), CommitParsedArgs(CommitAction.STATUS))
        self.assertEqual(parse_commit_args(["plan"]), CommitParsedArgs(CommitAction.PLAN, seconds=60))
        self.assertEqual(
            parse_commit_args(["plan", "--revert"]),
            CommitParsedArgs(CommitAction.PLAN, seconds=30, revert=True),
        )
        self.assertEqual(
            parse_commit_args(["apply", "90", "--yes"]),
            CommitParsedArgs(CommitAction.APPLY, seconds=90, yes=True),
        )
        self.assertEqual(
            parse_commit_args(["revert", "--yes"]),
            CommitParsedArgs(CommitAction.REVERT, seconds=30, revert=True, yes=True),
        )
        self.assertEqual(parse_commit_args(["apply", "5"]).code, 2)
        self.assertEqual(parse_commit_args(["apply", "abc"]).code, 2)
        self.assertEqual(parse_commit_args(["plan", "--yes"]).code, 2)
        self.assertEqual(parse_commit_args(["status", "extra"]).code, 2)
        self.assertEqual(parse_commit_args(["bogus"]).code, 2)

    def test_option_parsing_and_rewriting_keeps_flushoncommit(self) -> None:
        base = "subvol=var,compress-force=zstd:1,noatime,flushoncommit"
        self.assertEqual(effective_commit_seconds(base), 30)
        self.assertFalse(has_explicit_commit(base))
        rewritten = rewrite_commit_options(base, 60)
        self.assertEqual(rewritten, "subvol=var,compress-force=zstd:1,noatime,flushoncommit,commit=60")
        self.assertEqual(effective_commit_seconds(rewritten), 60)
        self.assertTrue(has_explicit_commit(rewritten))
        self.assertEqual(rewrite_commit_options(rewritten, 90), base + ",commit=90")
        self.assertEqual(rewrite_commit_options(rewritten, 30), base)
        self.assertEqual(rewrite_commit_options(rewritten, None), base)

    def test_coherence_and_plan(self) -> None:
        snap = CompressSnapshot(
            is_ostree=True,
            root_uuid=SAMPLE_UUID,
            fstab_text=SAMPLE_FSTAB,
            kargs_text=SAMPLE_KARGS,
            proc_mounts_text=SAMPLE_PROC_MOUNTS,
            top_mount=parse_top_mount_unit(SAMPLE_TOP_MOUNT),
        )
        coh = assess_commit_coherence(snap)
        self.assertTrue(coh.coherent)
        self.assertEqual(coh.seconds, 30)
        self.assertFalse(coh.is_custom)

        plan = build_commit_plan(snap, target_seconds=60)
        self.assertTrue(plan.has_changes)
        self.assertEqual([lp.status for lp in plan.layers], [LayerStatus.CHANGE] * 4)
        self.assertIn("commit=60", plan.new_fstab_text or "")
        self.assertIn("flushoncommit", plan.new_fstab_text or "")

        drifted = CompressSnapshot(
            is_ostree=True,
            root_uuid=SAMPLE_UUID,
            fstab_text=plan.new_fstab_text,
            kargs_text=SAMPLE_KARGS,
            proc_mounts_text=SAMPLE_PROC_MOUNTS,
            top_mount=snap.top_mount,
        )
        rep = assess_commit_coherence(drifted)
        self.assertFalse(rep.coherent)
        self.assertIn("fstab /home=commit=60", rep.summary)


class CommitAdapterAndCliTests(unittest.TestCase):
    def test_apply_idempotent_revert_and_rollback(self) -> None:
        host = _FakeHost()
        rc, out = execute_commit_change(host, CommitParsedArgs(CommitAction.APPLY, seconds=60, yes=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(
            [c.split(":", 1)[0] for c in host.calls], ["write_fstab", "update_kargs", "remount", "update_top_mount"]
        )
        self.assertIn("commit=60", host.s.fstab or "")
        self.assertIn("commit=60", host.s.kargs_text)
        self.assertIn("commit=60", host.s.proc_mounts or "")
        self.assertIn("commit=60", host.s.top_mount.options if host.s.top_mount else "")

        # Idempotent second run changes nothing and doesn't prompt.
        host.calls.clear()
        host.s.confirm_answer = None
        rc2, out2 = execute_commit_change(host, CommitParsedArgs(CommitAction.APPLY, seconds=60, yes=False))
        self.assertEqual(rc2, 0)
        self.assertEqual(host.calls, [])
        self.assertIn("nothing to do", "\n".join(out2))

        # Revert strips commit=60 from fstab/kargs/top-mount and remounts commit=30.
        rc3, _ = execute_commit_change(host, CommitParsedArgs(CommitAction.REVERT, seconds=30, revert=True, yes=True))
        self.assertEqual(rc3, 0)
        self.assertNotIn("commit=", host.s.fstab or "")
        self.assertNotIn("commit=", host.s.kargs_text)
        self.assertNotIn("commit=", host.s.top_mount.options if host.s.top_mount else "")
        self.assertNotIn("commit=", host.s.proc_mounts or "")

        # Failure mid-way rolls back earlier layers completely.
        for bad in (
            _FakeState(fail_kargs="busy"),
            _FakeState(fail_remount="mount failed"),
            _FakeState(fake_remount_effect=False),
            _FakeState(fail_top_mount=OSError("disk full")),
        ):
            bad_host = _FakeHost(bad)
            rc_b, out_b = execute_commit_change(bad_host, CommitParsedArgs(CommitAction.APPLY, seconds=60, yes=True))
            self.assertEqual(rc_b, 1)
            self.assertEqual(bad_host.s.fstab, SAMPLE_FSTAB)
            self.assertEqual(bad_host.s.kargs_text, SAMPLE_KARGS)
            self.assertNotIn("commit=60", bad_host.s.proc_mounts or "")
            self.assertIn("previous configuration restored", "\n".join(out_b))

    def test_a_separate_btrfs_home_is_left_alone(self) -> None:
        # /var/home is another btrfs filesystem (dm-1, its own UUID): apply and revert change only the root's.
        home_line = "UUID=0b5e6a1d-0000-4000-8000-00000000cafe /home btrfs subvol=home,compress=zstd:3,noatime 0 0\n"
        fstab = home_line + "".join(line for line in SAMPLE_FSTAB.splitlines(keepends=True) if " /home " not in line)
        mounts = SAMPLE_PROC_MOUNTS.replace(
            "/dev/dm-0 /var/home btrfs rw,seclabel,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit,subvol=/home",
            "/dev/dm-1 /var/home btrfs rw,seclabel,noatime,compress=zstd:3,subvol=/home",
        )
        host = _FakeHost(_FakeState(fstab=fstab, proc_mounts=mounts))
        rc, out = execute_commit_change(host, CommitParsedArgs(CommitAction.APPLY, seconds=60, yes=True))
        self.assertEqual(rc, 0, out)
        self.assertTrue((host.s.fstab or "").startswith(home_line), host.s.fstab)
        self.assertIn("commit=60", host.s.fstab or "")
        self.assertIn("/dev/dm-1 /var/home btrfs rw,seclabel,noatime,compress=zstd:3,subvol=/home 0 0", host.s.proc_mounts or "")
        self.assertFalse(any(c.startswith("remount:/var/home") for c in host.calls), host.calls)
        rc_r, out_r = execute_commit_change(
            host, CommitParsedArgs(CommitAction.REVERT, seconds=30, revert=True, yes=True)
        )
        self.assertEqual(rc_r, 0, out_r)
        self.assertEqual(host.s.fstab, fstab)

    def test_the_top_mount_keeps_its_own_options(self) -> None:
        # The unit's compress-force=zstd:3 is its own: commit apply changes only its commit=, and does not
        # copy the live /var's compress-force=zstd:1 into it.
        unit_opts = "subvolid=5,noatime,degraded,compress-force=zstd:3,ssd,flushoncommit"
        host = _FakeHost(_FakeState(top_mount=TopMountInfo(installed=True, uuid=SAMPLE_UUID, options=unit_opts)))
        rc, out = execute_commit_change(host, CommitParsedArgs(CommitAction.APPLY, seconds=60, yes=True))
        self.assertEqual(rc, 0, out)
        self.assertEqual(host.s.top_mount.options if host.s.top_mount else "", unit_opts + ",commit=60")
        rc_r, out_r = execute_commit_change(
            host, CommitParsedArgs(CommitAction.REVERT, seconds=30, revert=True, yes=True)
        )
        self.assertEqual(rc_r, 0, out_r)
        self.assertEqual(host.s.top_mount.options if host.s.top_mount else "", unit_opts)

    def test_cli_seam_and_container_refusal(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-commit-test-")
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
        deps = CommitCliDeps(
            entry_file=os.path.join(REPO, "btrfs", "btrfs-commit-interval"),
            env={"CST_BTRFS_COMMIT_ROOT": tmp, "CST_BTRFS_COMMIT_PROC": os.path.join(tmp, "proc")},
            out=out,
            err=err,
            ctx=_FakeCtx(container=True, root=False),
        )
        self.assertEqual(run_commit_cli(["status"], deps=deps), 0, err.getvalue())
        self.assertIn("coherent (commit=30 (default))", out.getvalue())

        out_p, err_p = io.StringIO(), io.StringIO()
        self.assertEqual(
            run_commit_cli(
                ["plan"],
                deps=CommitCliDeps(entry_file=deps.entry_file, env=deps.env, out=out_p, err=err_p, ctx=deps.ctx),
            ),
            0,
        )
        self.assertIn("fstab", out_p.getvalue())
        self.assertIn("Dry run", out_p.getvalue())

        out_r, err_r = io.StringIO(), io.StringIO()
        with mock.patch("sys.exit"):
            rc_r = run_commit_cli(
                ["apply", "--yes"],
                deps=CommitCliDeps(entry_file=deps.entry_file, env=deps.env, out=out_r, err=err_r, ctx=deps.ctx),
            )
        self.assertEqual(rc_r, 1)
        self.assertIn("toolbox/container", err_r.getvalue())

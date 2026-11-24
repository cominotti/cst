# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst btrfs compress-force` (`btrfs/compress/`) and Anaconda compress script."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
import io
import json
import os
import shutil
import subprocess
import tempfile
from typing import NoReturn, override
import unittest
from unittest import mock

from btrfs.compress.adapters import CompressAdapterConfig, LinuxCompressHostAdapter
from btrfs.compress.application import CompressService
from btrfs.compress.cli import CompressCliStreams, compress_cli
from btrfs.compress.domain import (
    CompressMode,
    CompressSnapshot,
    CompressSpec,
    LayerName,
    LayerStatus,
    TopMountInfo,
    assess_coherence,
    build_plan,
    effective_compress_spec,
    matches_target_spec,
    parse_compress_spec,
    parse_fstab_root_entries,
    parse_proc_mounts,
    parse_top_mount_unit,
    pick_root_mounts,
    rewrite_fstab,
    rewrite_kargs_rootflags,
    rewrite_mount_options,
    top_mount_options_from_var,
)
from btrfs.compress.domain.args import ArgsError, CompressArgs, CompressCommand, parse_compress_args
from btrfs.compress.presenter import render_compress_change, render_compress_plan, render_compress_status
from btrfs.compress.wiring import CompressWiringOverrides
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.application.install import InstallRefused, InstallRequest, InstallService
from cst_core.domain.errors import Refused
from cst_core.domain.install import InstallEntry
from cst_core.domain.record import ROOT_RECORD
from cst_core.ports import CommandFailed, CommandResult, WriteFailed
from tests.repo_files import REPO

SAMPLE_UUID = "afed93e2-f4ac-43de-8a48-751b552435c7"

SAMPLE_FSTAB = (
    "# Static header comment\n"
    f"UUID={SAMPLE_UUID} /\t\tbtrfs   subvol=root,degraded,compress=zstd:1,noatime,flushoncommit 0 0\n"
    f"UUID={SAMPLE_UUID} /var            btrfs   subvol=var,degraded,compress=zstd:1,noatime,flushoncommit 0 0\n"
    f"UUID={SAMPLE_UUID} /var/home       btrfs   subvol=home,degraded,compress=zstd:1,noatime,flushoncommit 0 0\n"
    "UUID=1111-2222        /boot/efi       vfat    umask=0077,shortname=winnt 0 2\n"
    "UUID=dead-beef        /mnt/backup     btrfs   defaults,compress=zstd:3,noauto 0 0\n"
)

SAMPLE_KARGS = (
    "ostree=/ostree/boot.1/fedora/abc/0 rhgb quiet "
    f"root=UUID={SAMPLE_UUID} rw "
    "rootflags=subvol=root,degraded,compress=zstd:1,noatime,flushoncommit "
    "log_buf_len=4M\n"
)

SAMPLE_PROC_MOUNTS = (
    "/dev/dm-0 /sysroot btrfs ro,seclabel,noatime,degraded,compress=zstd:1,ssd,flushoncommit,subvol=/root 0 0\n"
    "/dev/dm-0 /var btrfs rw,seclabel,noatime,degraded,compress=zstd:1,ssd,flushoncommit,subvol=/var 0 0\n"
    "/dev/dm-0 /var/home btrfs rw,seclabel,noatime,degraded,compress=zstd:1,ssd,flushoncommit,subvol=/home 0 0\n"
)

SAMPLE_TOP_MOUNT = (
    "# SPDX-License-Identifier: MPL-2.0\n"
    "[Unit]\n"
    "Description=btrfs top level for snapshots\n"
    "\n"
    "[Mount]\n"
    f"What=/dev/disk/by-uuid/{SAMPLE_UUID}\n"
    "Where=/var/mnt/btrfs-top\n"
    "Type=btrfs\n"
    "Options=subvolid=5,noatime,degraded,compress=zstd:1,ssd,flushoncommit\n"
)

MOCK_MANUAL_PARTITIONING = '''"""Mock manual partitioning."""
__all__ = ["ManualPartitioningTask"]

class ManualPartitioningTask:
    def run(self):
        device = object()
        mount_data = object()
        device.format.create_options = mount_data.format_options
        device.format.options = mount_data.mount_options
        return device
'''


class _FakeCtx:
    def __init__(self, *, container: bool = False, root: bool = True, host_root: str = "") -> None:
        self._container = container
        self._root = root
        self._host_root = host_root

    def is_container(self) -> bool:
        return self._container

    def is_root(self) -> bool:
        return self._root

    def path(self, host_path: str) -> str:
        return os.path.join(self._host_root, host_path.lstrip("/")) if self._host_root else host_path

    def etc_dir(self) -> str:
        return os.path.join(self._host_root, "etc") if self._host_root else "/etc"

    def host_run_dir(self, subpath: str) -> str:
        cleaned = subpath.strip("/")
        base = os.path.join(self._host_root, "run") if self._host_root else "/run"
        return os.path.join(base, cleaned) if cleaned else base


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
            self.s.proc_mounts = _rewrite_fake_mounts(self.s.proc_mounts, option, target)
        return True, ""

    def update_top_mount(self, uuid: str, options: str) -> None:
        self.calls.append(f"update_top_mount:{uuid}:{options}")
        if self.s.fail_top_mount is not None:
            raise self.s.fail_top_mount
        self.s.top_mount = TopMountInfo(installed=True, uuid=uuid, options=options)

    def confirm(self, prompt: str) -> bool:
        self.calls.append(f"confirm:{prompt}")
        if self.s.confirm_answer is None:
            raise Refused("apply asks for confirmation: run it in a terminal")
        return self.s.confirm_answer


def _rewrite_fake_mounts(cur: str, option: str, target: str = "/var") -> str:
    """A remount of target, as the kernel applies it: every mount of that filesystem (its device) changes."""
    device = next((ln.split()[0] for ln in cur.splitlines() if ln.split()[1:2] == [target]), None)
    updated: list[str] = []
    for ln in cur.splitlines(keepends=True):
        match ln.split():
            case [src, _, "btrfs", opts, *_] if src == device:
                updated.append(ln.replace(opts, rewrite_mount_options(opts, option), 1))
            case _:
                updated.append(ln)
    return "".join(updated)


class _RecordingRunner:
    def __init__(self, tmp_root: str, *, fail_daemon_reload: bool = False) -> None:
        self.tmp_root = tmp_root
        self.fail_daemon_reload = fail_daemon_reload
        self.cmds: list[list[str]] = []

    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        cmd = list(argv)
        self.cmds.append(cmd)
        if self.fail_daemon_reload and cmd[:2] == ["systemctl", "daemon-reload"]:
            if kw.get("check"):
                raise CommandFailed(cmd, 1, "", "reload failed")
            return CommandResult(1, "", "reload failed", tuple(cmd))
        if cmd == ["rpm-ostree", "kargs"]:
            return CommandResult(0, SAMPLE_KARGS, "", tuple(cmd))
        if cmd[:2] == ["findmnt", "-rn"]:
            return CommandResult(0, SAMPLE_UUID + "\n", "", tuple(cmd))
        if cmd[:2] == ["mount", "-o"]:
            opt = cmd[2].removeprefix("remount,")
            mounts_path = os.path.join(self.tmp_root, "proc", "mounts")
            with open(mounts_path, encoding="utf-8") as fh:
                cur = fh.read()
            with open(mounts_path, "w", encoding="utf-8") as fh:
                fh.write(_rewrite_fake_mounts(cur, opt, cmd[-1]))
            return CommandResult(0, "", "", tuple(cmd))
        return CommandResult(0, "", "", tuple(cmd))


class _PerTargetRunner(_RecordingRunner):
    """findmnt answers `--target T` from a mount table, as the real one does: the UUID of T's btrfs mount,
    or exit 1 with no output when T is not mounted or not btrfs."""

    def __init__(self, tmp_root: str, uuids: dict[str, str]) -> None:
        super().__init__(tmp_root)
        self.uuids = uuids

    @override
    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        cmd = list(argv)
        if cmd[:2] == ["findmnt", "-rn"] and "--target" in cmd:
            self.cmds.append(cmd)
            uuid = self.uuids.get(cmd[cmd.index("--target") + 1], "")
            return CommandResult(0 if uuid else 1, uuid + "\n" if uuid else "", "", tuple(cmd))
        return super().run(argv, **kw)


class DomainTests(unittest.TestCase):
    def test_parse_args(self) -> None:
        self.assertEqual(parse_compress_args([]), CompressArgs(command=CompressCommand.STATUS))
        self.assertEqual(parse_compress_args(["--help"]), CompressArgs(command=CompressCommand.HELP))
        self.assertEqual(
            parse_compress_args(["plan", "--level", "3", "--revert"]),
            CompressArgs(command=CompressCommand.PLAN, level=3, revert=True),
        )
        self.assertEqual(
            parse_compress_args(["apply", "--level=6", "--yes"]),
            CompressArgs(command=CompressCommand.APPLY, level=6, yes=True),
        )
        for bad in (
            ["unknown"],
            ["apply", "--level", "0"],
            ["apply", "--level", "16"],
            ["apply", "--level=abc"],
            ["apply", "--revert"],
            ["plan", "--yes"],
            ["status", "--level", "1"],
            ["apply", "--level"],
        ):
            self.assertIsInstance(parse_compress_args(bad), ArgsError, bad)

    def test_spec_and_rewrite_mount_options(self) -> None:
        self.assertEqual(
            parse_compress_spec("compress-force=zstd:3"),
            CompressSpec(mode=CompressMode.COMPRESS_FORCE, algo="zstd", level=3, raw="compress-force=zstd:3"),
        )
        self.assertTrue(matches_target_spec(parse_compress_spec("compress=zstd"), CompressMode.COMPRESS, 3))
        self.assertFalse(matches_target_spec(parse_compress_spec("compress=zstd"), CompressMode.COMPRESS, 1))
        self.assertEqual(
            rewrite_mount_options("subvol=root,degraded,compress=zstd:1,noatime", "compress-force=zstd:1"),
            "subvol=root,degraded,compress-force=zstd:1,noatime",
        )
        self.assertEqual(
            rewrite_mount_options("subvol=root,compress=lzo,noatime,compress=zstd:1", "compress-force=zstd:1"),
            "subvol=root,compress-force=zstd:1,noatime",
        )
        self.assertEqual(
            rewrite_mount_options("subvol=root,noatime", "compress-force=zstd:1"),
            "subvol=root,noatime,compress-force=zstd:1",
        )
        self.assertEqual(
            effective_compress_spec("compress=lzo,compress-force=zstd:1"),
            parse_compress_spec("compress-force=zstd:1"),
        )

    def test_rewrite_fstab_preserves_formatting_and_unrelated_entries(self) -> None:
        out = rewrite_fstab(SAMPLE_FSTAB, "compress-force=zstd:1", SAMPLE_UUID)
        lines = out.splitlines()
        self.assertEqual(lines[0], "# Static header comment")
        self.assertIn(
            f"UUID={SAMPLE_UUID} /\t\tbtrfs   subvol=root,degraded,compress-force=zstd:1,noatime,flushoncommit 0 0",
            lines[1],
        )
        self.assertIn("subvol=var,degraded,compress-force=zstd:1,noatime,flushoncommit", lines[2])
        self.assertIn("subvol=home,degraded,compress-force=zstd:1,noatime,flushoncommit", lines[3])
        self.assertEqual(lines[4], "UUID=1111-2222        /boot/efi       vfat    umask=0077,shortname=winnt 0 2")
        self.assertEqual(lines[5], "UUID=dead-beef        /mnt/backup     btrfs   defaults,compress=zstd:3,noauto 0 0")
        self.assertTrue(out.endswith("\n"))

    def test_rewrite_kargs_and_top_mount_options(self) -> None:
        edits = rewrite_kargs_rootflags(SAMPLE_KARGS, "compress-force=zstd:1")
        self.assertEqual(
            edits,
            (
                (
                    "rootflags=subvol=root,degraded,compress=zstd:1,noatime,flushoncommit",
                    "rootflags=subvol=root,degraded,compress-force=zstd:1,noatime,flushoncommit",
                ),
            ),
        )
        self.assertEqual(
            rewrite_kargs_rootflags(
                SAMPLE_KARGS.replace("compress=zstd:1", "compress-force=zstd:1"),
                "compress-force=zstd:1",
            ),
            (),
        )
        self.assertEqual(
            top_mount_options_from_var("rw,seclabel,subvolid=257,subvol=/var,compress-force=zstd:1,noatime"),
            "subvolid=5,compress-force=zstd:1,noatime",
        )

    def test_coherence_and_plan(self) -> None:
        top = parse_top_mount_unit(SAMPLE_TOP_MOUNT)
        rep_ok = assess_coherence(
            CompressSnapshot(
                is_ostree=True,
                root_uuid=SAMPLE_UUID,
                fstab_text=SAMPLE_FSTAB,
                kargs_text=SAMPLE_KARGS,
                proc_mounts_text=SAMPLE_PROC_MOUNTS,
                top_mount=top,
            )
        )
        self.assertTrue(rep_ok.coherent)
        self.assertEqual(rep_ok.mode, CompressMode.COMPRESS)
        self.assertEqual(rep_ok.level, 1)

        bare_proc = SAMPLE_PROC_MOUNTS.replace("compress=zstd:1", "compress=zstd")
        rep_zstd3 = assess_coherence(
            CompressSnapshot(
                is_ostree=False,
                root_uuid=SAMPLE_UUID,
                fstab_text=SAMPLE_FSTAB.replace("compress=zstd:1", "compress=zstd:3"),
                kargs_text=None,
                proc_mounts_text=bare_proc,
                top_mount=None,
            )
        )
        self.assertTrue(rep_zstd3.coherent)

        drifted = assess_coherence(
            CompressSnapshot(
                is_ostree=True,
                root_uuid=SAMPLE_UUID,
                fstab_text=SAMPLE_FSTAB.replace("compress=zstd:1", "compress-force=zstd:1"),
                kargs_text=SAMPLE_KARGS.replace("compress=zstd:1", "compress-force=zstd:1"),
                proc_mounts_text=SAMPLE_PROC_MOUNTS.replace("compress=zstd:1", "compress-force=zstd:1"),
                top_mount=top,
            )
        )
        self.assertFalse(drifted.coherent)
        self.assertIn("top-mount=compress=zstd:1", drifted.summary)

        plan = build_plan(
            CompressSnapshot(
                is_ostree=True,
                root_uuid=SAMPLE_UUID,
                fstab_text=SAMPLE_FSTAB,
                kargs_text=SAMPLE_KARGS,
                proc_mounts_text=SAMPLE_PROC_MOUNTS,
                top_mount=top,
            ),
            target_mode=CompressMode.COMPRESS_FORCE,
            level_override=None,
        )
        self.assertTrue(plan.has_changes)
        self.assertEqual(plan.target_option, "compress-force=zstd:1")
        self.assertEqual([lp.status for lp in plan.layers], [LayerStatus.CHANGE] * 4)
        self.assertEqual(plan.layers[0].current, "compress=zstd:1")


OTHER_UUID = "0b5e6a1d-0000-4000-8000-00000000cafe"
# A separate btrfs filesystem mounted at /var/home (dm-1), next to the root filesystem (dm-0).
SEPARATE_HOME_LINE = f"UUID={OTHER_UUID} /var/home       btrfs   subvol=home,compress=zstd:3,noatime 0 0\n"
SEPARATE_HOME_FSTAB = "".join(
    SEPARATE_HOME_LINE if "/var/home" in line else line for line in SAMPLE_FSTAB.splitlines(keepends=True)
)
SEPARATE_HOME_MOUNTS = SAMPLE_PROC_MOUNTS.replace(
    "/dev/dm-0 /var/home btrfs rw,seclabel,noatime,degraded,compress=zstd:1,ssd,flushoncommit,subvol=/home",
    "/dev/dm-1 /var/home btrfs rw,seclabel,noatime,compress=zstd:3,ssd,subvol=/home",
)


class SeparateHomeTests(unittest.TestCase):
    """A btrfs /home or /var/home that is another filesystem is never touched (only the root's lines and
    mounts are); /home as a subvolume of the root filesystem (Fedora's default) still is."""

    def test_its_fstab_line_is_kept_byte_for_byte(self) -> None:
        for fstab, line in (
            (SEPARATE_HOME_FSTAB, SEPARATE_HOME_LINE),
            (
                SEPARATE_HOME_FSTAB.replace(" /var/home       ", " /home           "),
                SEPARATE_HOME_LINE.replace(" /var/home       ", " /home           "),
            ),
        ):
            for root_uuid in (SAMPLE_UUID, ""):
                with self.subTest(line=line, root_uuid=root_uuid):
                    out = rewrite_fstab(fstab, "compress-force=zstd:1", root_uuid)
                    self.assertIn(line, out)
                    self.assertEqual(out.count("compress-force=zstd:1"), 2)  # / and /var
                    self.assertEqual([e.target for e in parse_fstab_root_entries(fstab, root_uuid)], ["/", "/var"])

    def test_live_mounts_are_the_root_filesystem_s(self) -> None:
        self.assertEqual(
            [m.target for m in pick_root_mounts(parse_proc_mounts(SEPARATE_HOME_MOUNTS))], ["/sysroot", "/var"]
        )
        findmnt = (
            "/dev/mapper/luks-a[/root] /sysroot btrfs ro,compress=zstd:1,subvol=/root 0 0\n"
            "/dev/mapper/luks-a[/var] /var btrfs rw,compress=zstd:1,subvol=/var 0 0\n"
            "/dev/mapper/luks-a[/home] /var/home btrfs rw,compress=zstd:1,subvol=/home 0 0\n"
            "/dev/mapper/luks-b[/home] /home btrfs rw,compress=zstd:3,subvol=/home 0 0\n"
        )
        self.assertEqual(
            [m.target for m in pick_root_mounts(parse_proc_mounts(findmnt))], ["/sysroot", "/var", "/var/home"]
        )

    def test_fedora_s_default_home_subvolume_is_still_the_root_s(self) -> None:
        self.assertEqual(
            [m.target for m in pick_root_mounts(parse_proc_mounts(SAMPLE_PROC_MOUNTS))],
            ["/sysroot", "/var", "/var/home"],
        )
        self.assertEqual(
            [e.target for e in parse_fstab_root_entries(SAMPLE_FSTAB, SAMPLE_UUID)], ["/", "/var", "/var/home"]
        )

    def test_label_sources_match_by_their_text(self) -> None:
        fstab = (
            "LABEL=fedora / btrfs subvol=root,compress=zstd:1 0 0\n"
            "LABEL=fedora /home btrfs subvol=home,compress=zstd:1 0 0\n"
            "LABEL=data /var/home btrfs subvol=home,compress=zstd:1 0 0\n"
        )
        out = rewrite_fstab(fstab, "compress-force=zstd:1", SAMPLE_UUID).splitlines()
        self.assertEqual(
            out,
            [
                "LABEL=fedora / btrfs subvol=root,compress-force=zstd:1 0 0",
                "LABEL=fedora /home btrfs subvol=home,compress-force=zstd:1 0 0",
                "LABEL=data /var/home btrfs subvol=home,compress=zstd:1 0 0",
            ],
        )

    def test_a_host_whose_only_btrfs_is_home_is_left_alone(self) -> None:
        mounts = "/dev/dm-1 /home btrfs rw,compress=zstd:3,subvol=/home 0 0\n"
        fstab = "UUID=1234 / ext4 defaults 0 1\n" + f"UUID={OTHER_UUID} /home btrfs subvol=home,compress=zstd:3 0 0\n"
        self.assertEqual(pick_root_mounts(parse_proc_mounts(mounts)), ())
        self.assertEqual(parse_fstab_root_entries(fstab, ""), ())
        self.assertEqual(rewrite_fstab(fstab, "compress-force=zstd:1", ""), fstab)

    def test_apply_and_revert_leave_the_separate_home_alone(self) -> None:
        host = _FakeHost(_FakeState(fstab=SEPARATE_HOME_FSTAB, proc_mounts=SEPARATE_HOME_MOUNTS))
        svc = CompressService(host)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.APPLY, yes=True)), out, err),
            0,
            err.getvalue(),
        )
        self.assertIn(SEPARATE_HOME_LINE, host.s.fstab or "")
        self.assertIn(
            "/dev/dm-1 /var/home btrfs rw,seclabel,noatime,compress=zstd:3,ssd,subvol=/home", host.s.proc_mounts or ""
        )
        self.assertFalse(any(c.startswith("remount:/var/home") for c in host.calls), host.calls)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.REVERT, yes=True)), out, err),
            0,
            err.getvalue(),
        )
        self.assertEqual(host.s.fstab, SEPARATE_HOME_FSTAB)


class BootedKargsTests(unittest.TestCase):
    def test_status_compares_the_last_rootflags(self) -> None:
        # The kernel uses the last rootflags=: a difference in an earlier one is no difference.
        head = f"ostree=/ostree/boot.1/fedora/abc/0 root=UUID={SAMPLE_UUID} rw "
        last = "rootflags=subvol=root,degraded,compress=zstd:1,noatime,flushoncommit"
        nxt = head + f"rootflags=subvol=root,compress=lzo {last}\n"
        for booted, want in (
            (head + f"rootflags=subvol=root {last}\n", None),
            (
                head + "rootflags=subvol=root,compress=lzo rootflags=subvol=root,compress-force=zstd:1\n",
                ("rootflags=subvol=root,compress-force=zstd:1", last),
            ),
        ):
            with self.subTest(booted=booted):
                host = _FakeHost(_FakeState(kargs_text=nxt, proc_cmdline=booted))
                self.assertEqual(CompressService(host).gather_status().booted_diff, want)


class TopMountInPlaceTests(unittest.TestCase):
    """apply rewrites only the compress option of the top-mount unit's own Options=: what only the unit has
    stays, and what the live /var has (another tool's commit=) is not copied in."""

    def test_apply_and_revert_keep_the_unit_s_other_options(self) -> None:
        unit_opts = "subvolid=5,noatime,degraded,compress=zstd:1,ssd,flushoncommit,commit=120"
        state = _FakeState(
            top_mount=TopMountInfo(installed=True, uuid=SAMPLE_UUID, options=unit_opts),
            proc_mounts=SAMPLE_PROC_MOUNTS.replace("subvol=/var 0 0", "commit=45,subvol=/var 0 0"),
        )
        host = _FakeHost(state)
        svc = CompressService(host)
        plan = svc.build_change_plan(CompressArgs(CompressCommand.PLAN)).plan
        assert plan is not None
        want = "subvolid=5,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit,commit=120"
        self.assertEqual(plan.new_top_mount_options, want)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.APPLY, yes=True)), out, err),
            0,
            err.getvalue(),
        )
        self.assertIn(f"update_top_mount:{SAMPLE_UUID}:{want}", host.calls)
        self.assertEqual(host.s.top_mount.options if host.s.top_mount else "", want)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.REVERT, yes=True)), out, err),
            0,
            err.getvalue(),
        )
        self.assertEqual(host.s.top_mount.options if host.s.top_mount else "", unit_opts)


class ApplicationTests(unittest.TestCase):
    def test_plan_apply_idempotent_revert_cycle(self) -> None:
        host = _FakeHost()
        svc = CompressService(host)

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_plan(svc.build_change_plan(CompressArgs(CompressCommand.PLAN)), "cst", out, err), 0
        )
        self.assertEqual(host.calls, [])
        self.assertIn("compress-force=zstd:1", out.getvalue())

        host.s.confirm_answer = None
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.APPLY, yes=False)), out, err), 1
        )
        self.assertIn("--yes", err.getvalue())
        self.assertEqual(host.calls, ["confirm:Apply compress-force=zstd:1 across active layers?"])

        host.calls.clear()
        host.s.confirm_answer = False
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.APPLY, yes=False)), out, err), 1
        )
        self.assertIn("Aborted", out.getvalue())

        host.calls.clear()
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.APPLY, yes=True)), out, err), 0
        )
        self.assertEqual(
            [c.split(":", 1)[0] for c in host.calls],
            ["write_fstab", "update_kargs", "remount", "update_top_mount"],
        )
        self.assertIn("compress-force=zstd:1", host.s.fstab or "")
        self.assertIn("compress-force=zstd:1", host.s.kargs_text)
        self.assertIn("compress-force=zstd:1", host.s.top_mount.options if host.s.top_mount else "")

        out = io.StringIO()
        self.assertEqual(render_compress_status(svc.gather_status(), out), 0)
        self.assertIn("overall         coherent (compress-force=zstd:1)", out.getvalue())

        host.calls.clear()
        host.s.confirm_answer = None
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.APPLY, yes=False)), out, err), 0
        )
        self.assertEqual(host.calls, [])
        self.assertIn("nothing to do", out.getvalue())

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.REVERT, yes=True)), out, err), 0
        )
        self.assertNotIn("compress-force", host.s.fstab or "")
        self.assertNotIn("compress-force", host.s.kargs_text)
        self.assertNotIn("compress-force", host.s.top_mount.options if host.s.top_mount else "")
        reverted_plan = build_plan(
            CompressSnapshot(
                is_ostree=True,
                root_uuid=SAMPLE_UUID,
                fstab_text=host.s.fstab,
                kargs_text=host.s.kargs_text,
                proc_mounts_text=host.s.proc_mounts,
                top_mount=host.s.top_mount,
            ),
            target_mode=CompressMode.COMPRESS_FORCE,
            level_override=None,
        )
        self.assertEqual(
            [lp.layer for lp in reverted_plan.layers],
            [LayerName.FSTAB, LayerName.KARGS, LayerName.LIVE, LayerName.TOP_MOUNT],
        )

    def test_rollbacks_on_failure(self) -> None:
        for state in (
            _FakeState(fail_kargs="busy transaction"),
            _FakeState(fail_remount="mount failed"),
            _FakeState(fake_remount_effect=False),
            _FakeState(fail_top_mount=OSError("disk full")),
        ):
            host = _FakeHost(state)
            svc = CompressService(host)
            out, err = io.StringIO(), io.StringIO()
            self.assertEqual(
                render_compress_change(svc.execute_change(CompressArgs(CompressCommand.APPLY, yes=True)), out, err), 1
            )
            self.assertEqual(host.s.fstab, SAMPLE_FSTAB)
            self.assertEqual(host.s.kargs_text, SAMPLE_KARGS)
            self.assertNotIn("compress-force", host.s.proc_mounts or "")
            self.assertIn("previous configuration restored", err.getvalue())

    def test_non_ostree_status_and_plan(self) -> None:
        host = _FakeHost(_FakeState(is_ostree=False, top_mount=None))
        svc = CompressService(host)

        out = io.StringIO()
        self.assertEqual(render_compress_status(svc.gather_status(), out), 0)
        status_text = out.getvalue()
        self.assertIn("(not ostree)", status_text)
        self.assertIn("overall         coherent (compress=zstd:1)", status_text)

        out_p, err_p = io.StringIO(), io.StringIO()
        rc = render_compress_plan(
            svc.build_change_plan(CompressArgs(CompressCommand.PLAN)),
            "cst",
            out_p,
            err_p,
        )
        self.assertEqual(rc, 0)
        plan_text = out_p.getvalue()
        self.assertIn("kargs      [skip  ]", plan_text)
        self.assertIn("top-mount  [skip  ]", plan_text)
        self.assertIn("fstab      [CHANGE]", plan_text)
        self.assertIn("live       [CHANGE]", plan_text)

    def test_kargs_failure_and_booted_diff_in_status(self) -> None:
        host = _FakeHost(_FakeState(kargs_ok=False, kargs_err="dbus timeout"))
        svc = CompressService(host)

        out = io.StringIO()
        self.assertEqual(render_compress_status(svc.gather_status(), out), 0)
        status_text = out.getvalue()
        self.assertIn("(error: dbus timeout)", status_text)
        self.assertIn("overall         incomplete (rpm-ostree kargs error: dbus timeout)", status_text)
        self.assertNotIn("coherent (compress", status_text)

        out_p, err_p = io.StringIO(), io.StringIO()
        rc = render_compress_plan(
            svc.build_change_plan(CompressArgs(CompressCommand.PLAN)),
            "cst",
            out_p,
            err_p,
        )
        self.assertEqual(rc, 1)
        self.assertIn("cannot inspect rpm-ostree kargs: dbus timeout", err_p.getvalue())

        host2 = _FakeHost(
            _FakeState(
                proc_cmdline=SAMPLE_KARGS,
                kargs_text=SAMPLE_KARGS.replace("compress=zstd:1", "compress-force=zstd:1"),
            )
        )
        svc2 = CompressService(host2)
        out2 = io.StringIO()
        self.assertEqual(render_compress_status(svc2.gather_status(), out2), 0)
        self.assertIn("note            booted /proc/cmdline", out2.getvalue())
        self.assertIn("differs from next-boot kargs", out2.getvalue())

    def test_incomplete_rollback_and_no_orig_compress_spec(self) -> None:
        no_comp_mounts = SAMPLE_PROC_MOUNTS.replace("compress=zstd:1,", "")
        host = _FakeHost(_FakeState(proc_mounts=no_comp_mounts, fail_top_mount=OSError("disk full")))
        svc = CompressService(host)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.APPLY, yes=True)), out, err),
            1,
        )
        self.assertIn("remount:/var:compress=no", host.calls)
        self.assertIn("previous configuration restored.", err.getvalue())

        class _PartialRollbackHost(_FakeHost):
            def __init__(self, state: _FakeState) -> None:
                super().__init__(state)
                self._remounts = 0
                self._writes = 0

            @override
            def write_fstab(self, text: str) -> None:
                self._writes += 1
                if self._writes > 1:
                    raise OSError("ro")
                super().write_fstab(text)

            @override
            def update_kargs(self, edits: Sequence[tuple[str, str]]) -> tuple[bool, str]:
                if self.s.kargs_text != SAMPLE_KARGS:
                    return False, "locked"
                return super().update_kargs(edits)

            @override
            def remount(self, target: str, option: str) -> tuple[bool, str]:
                self._remounts += 1
                if self._remounts > 1:
                    return False, "busy"
                return super().remount(target, option)

        partial_host = _PartialRollbackHost(_FakeState(fail_top_mount=OSError("disk full")))
        partial_svc = CompressService(partial_host)
        out2, err2 = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(
                partial_svc.execute_change(CompressArgs(CompressCommand.APPLY, yes=True)),
                out2,
                err2,
            ),
            1,
        )
        self.assertIn("ROLLBACK INCOMPLETE (remount: busy; kargs: locked; fstab: ro)", err2.getvalue())


class AdapterAndCliTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-compress-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        for sub in ("etc/systemd/system", "proc", "run", "var/lib/cst"):
            os.makedirs(os.path.join(self.tmp, sub), exist_ok=True)
        with open(os.path.join(self.tmp, "run", "ostree-booted"), "w", encoding="utf-8") as fh:
            fh.write("")
        with open(os.path.join(self.tmp, "etc", "fstab"), "w", encoding="utf-8") as fh:
            fh.write(SAMPLE_FSTAB)
        with open(os.path.join(self.tmp, "proc", "mounts"), "w", encoding="utf-8") as fh:
            fh.write(SAMPLE_PROC_MOUNTS)
        with open(os.path.join(self.tmp, "proc", "cmdline"), "w", encoding="utf-8") as fh:
            fh.write(SAMPLE_KARGS)
        with open(
            os.path.join(self.tmp, "etc", "systemd", "system", r"var-mnt-btrfs\x2dtop.mount"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(SAMPLE_TOP_MOUNT)

    def test_adapter_writes_fstab_and_installs_marked_top_mount(self) -> None:
        runner = _RecordingRunner(self.tmp)
        cfg = CompressAdapterConfig(
            entry_file=os.path.join(REPO, "btrfs", "btrfs-compress"),
            fallback_root=REPO,
            proc_dir=os.path.join(self.tmp, "proc"),
            host_root=self.tmp,
        )
        adapter = LinuxCompressHostAdapter(
            cfg, ctx=_FakeCtx(root=True, host_root=self.tmp), runner=runner, fs=LocalFileSystemAdapter()
        )
        svc = CompressService(adapter)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            render_compress_change(svc.execute_change(CompressArgs(CompressCommand.APPLY, yes=True)), out, err),
            0,
            err.getvalue(),
        )

        with open(os.path.join(self.tmp, "etc", "fstab"), encoding="utf-8") as fh:
            self.assertIn("compress-force=zstd:1", fh.read())
        with open(
            os.path.join(self.tmp, "etc", "systemd", "system", r"var-mnt-btrfs\x2dtop.mount"), encoding="utf-8"
        ) as fh:
            self.assertIn("Options=subvolid=5,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit", fh.read())
        with open(os.path.join(self.tmp, ROOT_RECORD.lstrip("/")), encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["components"].get("btrfs:top-mount"), "done")
        self.assertTrue(any(cmd[:2] == ["systemctl", "daemon-reload"] for cmd in runner.cmds))

    def test_cli_container_refusal_and_sudo_escalation(self) -> None:
        runner = _RecordingRunner(self.tmp)
        entry = os.path.join(REPO, "btrfs", "btrfs-compress")
        out, err = io.StringIO(), io.StringIO()
        rc = compress_cli(
            ["plan"],
            entry_file=entry,
            streams=CompressCliStreams(
                env={"CST_BTRFS_COMPRESS_PROC": os.path.join(self.tmp, "proc")}, out=out, err=err
            ),
            overrides=CompressWiringOverrides(
                ctx=_FakeCtx(container=True, root=False, host_root=self.tmp),
                runner=runner,
                host_root=self.tmp,
                fallback_root=REPO,
            ),
        )
        self.assertEqual(rc, 0)
        self.assertIn("test seam (CST_BTRFS_COMPRESS_PROC):", out.getvalue())

        out, err = io.StringIO(), io.StringIO()
        rc = compress_cli(
            ["apply", "--yes"],
            entry_file=entry,
            streams=CompressCliStreams(env={}, out=out, err=err),
            overrides=CompressWiringOverrides(ctx=_FakeCtx(container=True, root=False)),
        )
        self.assertEqual(rc, 1)
        self.assertIn("toolbox/container", err.getvalue())

        captured: list[list[str]] = []

        def _fake_elevate(argv: list[str]) -> NoReturn:
            captured.append(argv)
            raise SystemExit(42)

        streams = CompressCliStreams(env={"CST_PROG": "cst btrfs compress-force"})
        overrides = CompressWiringOverrides(ctx=_FakeCtx(container=False, root=False), elevate=_fake_elevate)
        with self.assertRaises(SystemExit) as cm:
            compress_cli(
                ["apply", "--level", "2", "--yes"],
                entry_file=entry,
                streams=streams,
                overrides=overrides,
            )
        self.assertEqual(cm.exception.code, 42)
        self.assertEqual(captured[0][0:3], ["sudo", "--", "env"])
        self.assertIn("apply", captured[0])
        self.assertIn("--yes", captured[0])

    def test_adapter_root_uuid_uses_btrfs_filter_and_reverts_on_reload_failure(self) -> None:
        runner = _RecordingRunner(self.tmp, fail_daemon_reload=True)
        cfg = CompressAdapterConfig(
            entry_file=os.path.join(REPO, "btrfs", "btrfs-compress"),
            fallback_root=REPO,
            proc_dir=os.path.join(self.tmp, "proc"),
            host_root=self.tmp,
        )
        adapter = LinuxCompressHostAdapter(
            cfg, ctx=_FakeCtx(root=True, host_root=self.tmp), runner=runner, fs=LocalFileSystemAdapter()
        )
        self.assertEqual(adapter.root_uuid(), SAMPLE_UUID)
        self.assertIn(["findmnt", "-rn", "-t", "btrfs", "-o", "UUID", "--target", "/sysroot"], runner.cmds)

        with self.assertRaises(CommandFailed):
            adapter.write_fstab("# modified\n")
        with open(os.path.join(self.tmp, "etc", "fstab"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), SAMPLE_FSTAB)

        with self.assertRaises(CommandFailed):
            adapter.update_top_mount(
                SAMPLE_UUID,
                "subvolid=5,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit",
            )
        with open(
            os.path.join(self.tmp, "etc", "systemd", "system", r"var-mnt-btrfs\x2dtop.mount"),
            encoding="utf-8",
        ) as fh:
            top_text = fh.read()
        self.assertIn("compress=zstd:1", top_text)
        self.assertNotIn("compress-force", top_text)

    def _per_target_adapter(self, uuids: dict[str, str]) -> LinuxCompressHostAdapter:
        cfg = CompressAdapterConfig(
            entry_file=os.path.join(REPO, "btrfs", "btrfs-compress"),
            fallback_root=REPO,
            proc_dir=os.path.join(self.tmp, "proc"),
            host_root=self.tmp,
        )
        return LinuxCompressHostAdapter(
            cfg,
            ctx=_FakeCtx(root=True, host_root=self.tmp),
            runner=_PerTargetRunner(self.tmp, uuids),
            fs=LocalFileSystemAdapter(),
        )

    def test_the_root_uuid_is_sysroot_s_else_root_s_else_var_s(self) -> None:
        # The order of cst doctor and btrfs-setup: a separate btrfs /var is not the root.
        var_uuid = "0b5e6a1d-0000-4000-8000-00000000beef"
        cases = (
            ({"/sysroot": SAMPLE_UUID, "/": SAMPLE_UUID, "/var": var_uuid}, SAMPLE_UUID),
            ({"/": "uuid-root", "/var": var_uuid}, "uuid-root"),
            ({"/var": var_uuid}, var_uuid),
            ({}, SAMPLE_UUID),  # no btrfs at any of them: the top mount unit's What=
        )
        for uuids, expected in cases:
            with self.subTest(uuids=uuids):
                self.assertEqual(self._per_target_adapter(uuids).root_uuid(), expected)

    def test_apply_with_a_separate_btrfs_var_changes_only_the_root_s_lines(self) -> None:
        var_uuid = "0b5e6a1d-0000-4000-8000-00000000beef"
        var_line = f"UUID={var_uuid} /var btrfs subvol=var,compress=zstd:3,noatime 0 0\n"
        fstab = "".join(var_line if " /var " in line else line for line in SAMPLE_FSTAB.splitlines(keepends=True))
        with open(os.path.join(self.tmp, "etc", "fstab"), "w", encoding="utf-8") as fh:
            fh.write(fstab)
        with open(os.path.join(self.tmp, "proc", "mounts"), "w", encoding="utf-8") as fh:
            fh.write(
                SAMPLE_PROC_MOUNTS.replace(
                    "/dev/dm-0 /var btrfs rw,seclabel,noatime,degraded,compress=zstd:1,ssd,flushoncommit,subvol=/var",
                    "/dev/dm-2 /var btrfs rw,seclabel,noatime,compress=zstd:3,ssd,subvol=/var",
                )
            )
        adapter = self._per_target_adapter({"/sysroot": SAMPLE_UUID, "/": SAMPLE_UUID, "/var": var_uuid})
        out, err = io.StringIO(), io.StringIO()
        rc = render_compress_change(
            CompressService(adapter).execute_change(CompressArgs(CompressCommand.APPLY, yes=True)), out, err
        )
        self.assertEqual(rc, 0, err.getvalue())
        with open(os.path.join(self.tmp, "etc", "fstab"), encoding="utf-8") as fh:
            lines = fh.read().splitlines(keepends=True)
        self.assertIn(var_line, lines)
        root_line = next(line for line in lines if "\t\tbtrfs" in line)
        self.assertIn("compress-force=zstd:1", root_line)

    # update_top_mount: a failure while installing puts the previous unit back only when the unit on
    # disk changed, and is raised either way.
    def _adapter(self) -> LinuxCompressHostAdapter:
        cfg = CompressAdapterConfig(
            entry_file=os.path.join(REPO, "btrfs", "btrfs-compress"),
            fallback_root=REPO,
            proc_dir=os.path.join(self.tmp, "proc"),
            host_root=self.tmp,
        )
        return LinuxCompressHostAdapter(
            cfg,
            ctx=_FakeCtx(root=True, host_root=self.tmp),
            runner=_RecordingRunner(self.tmp),
            fs=LocalFileSystemAdapter(),
        )

    def _unit(self) -> str:
        with open(
            os.path.join(self.tmp, "etc", "systemd", "system", r"var-mnt-btrfs\x2dtop.mount"), encoding="utf-8"
        ) as fh:
            return fh.read()

    def test_a_failure_after_the_unit_was_written_restores_it(self) -> None:
        # The record's lock cannot be taken: install has written the units when it fails.
        os.makedirs(os.path.join(self.tmp, ROOT_RECORD.lstrip("/") + ".lock"))
        adapter = self._adapter()
        written: list[str] = []
        real_install = InstallService.install

        def spy(svc: InstallService, entries: Sequence[InstallEntry], req: InstallRequest) -> Iterator[str]:
            for line in real_install(svc, entries, req):
                written.append(line)
                yield line

        with mock.patch.object(InstallService, "install", spy), self.assertRaises(WriteFailed):
            adapter.update_top_mount(SAMPLE_UUID, "subvolid=5,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit")
        self.assertTrue(any(line.startswith("written:") and "top.mount" in line for line in written), written)
        self.assertIn("Options=subvolid=5,noatime,degraded,compress=zstd:1,ssd,flushoncommit\n", self._unit())

    def test_a_failure_before_any_write_restores_nothing(self) -> None:
        adapter = self._adapter()
        calls: list[str] = []
        real_install = InstallService.install

        def refuse_first(svc: InstallService, entries: Sequence[InstallEntry], req: InstallRequest) -> Iterator[str]:
            calls.append(req.values.get("OPTIONS", "") if req.values is not None else "")
            if len(calls) == 1:
                raise InstallRefused("var-mnt-btrfs\\x2dtop.mount: refused by the unit verifier")
            return real_install(svc, entries, req)

        with mock.patch.object(InstallService, "install", refuse_first), self.assertRaises(InstallRefused):
            adapter.update_top_mount(SAMPLE_UUID, "subvolid=5,compress-force=zstd:1")
        self.assertEqual(calls, ["subvolid=5,compress-force=zstd:1"])  # no second install: nothing to restore
        self.assertEqual(self._unit(), SAMPLE_TOP_MOUNT)


class AnacondaScriptTest(unittest.TestCase):
    def test_patches_fresh_and_upgrades_old_compress_patch(self) -> None:
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-anaconda-test-"))
        self.addCleanup(shutil.rmtree, tmp)
        target = os.path.join(tmp, "manual_partitioning.py")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("from blivet.errors import StorageError\n" + MOCK_MANUAL_PARTITIONING)

        script = os.path.join(REPO, "fix-anaconda-webui-btrfs-compress.sh")
        env = {"PATH": os.environ["PATH"], "ANACONDA_BTRFS_FIX_TARGET": target}
        r = subprocess.run(
            ["bash", script],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(target, encoding="utf-8") as fh:
            patched = fh.read()
        self.assertIn("compress-force=%s", patched)

        r2 = subprocess.run(
            ["bash", script],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(r2.returncode, 0)
        self.assertIn("Already patched:", r2.stdout)

        with open(target, "w", encoding="utf-8") as fh:
            fh.write(patched.replace("compress-force=%s", "compress=%s"))
        r3 = subprocess.run(
            ["bash", script],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(r3.returncode, 0, r3.stderr)
        with open(target, encoding="utf-8") as fh:
            upgraded = fh.read()
        self.assertIn("compress-force=%s", upgraded)
        self.assertNotIn('"compress=%s"', upgraded)

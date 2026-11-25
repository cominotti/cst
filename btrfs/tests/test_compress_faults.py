# SPDX-License-Identifier: MPL-2.0
"""Fault injection on `cst btrfs compress-force` and `cst btrfs commit-interval` apply and revert.

AdapterFailureTest pins what the real adapter (btrfs/compress/adapters.py, LinuxCompressHostAdapter) gives
when a step fails after it changed its layer: those outcomes are what CompressWorld's torn points
(btrfs/tests/fakes.py) stand for. CompressWorldTest pins the world's own rules.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from enum import StrEnum
import errno
import os
import shutil
import tempfile
from typing import override
import unittest
from unittest import mock

from btrfs.compress.adapters import CompressAdapterConfig, LinuxCompressHostAdapter
from btrfs.compress.domain import TopMountInfo, parse_top_mount_unit
from btrfs.tests.fakes import CompressWorld, kernel_remount_options
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.application.install import InstallRequest, InstallService
from cst_core.domain.install import InstallEntry
from cst_core.ports import CommandFailed, CommandResult
from tests.fault_injection import FaultMode, FaultPlan, FaultyRunner, no_space
from tests.repo_files import REPO

UUID = "afed93e2-f4ac-43de-8a48-751b552435c7"
TOP_UNIT = r"var-mnt-btrfs\x2dtop.mount"
OLD_FSTAB = f"UUID={UUID} / btrfs subvol=root,compress=zstd:1,noatime 0 0\n"
NEW_FSTAB = f"UUID={UUID} / btrfs subvol=root,compress-force=zstd:1,noatime 0 0\n"
OLD_TOP_OPTIONS = "subvolid=5,noatime,compress=zstd:1"
NEW_TOP_OPTIONS = "subvolid=5,noatime,compress-force=zstd:1"


class _Tear(StrEnum):
    """Where a staged write_atomic fails."""

    BEFORE = "before"  # nothing written (the temporary file could not be made)
    AFTER = "after"  # the new text renamed into place, then the folder's sync failed


class _TearingFs(LocalFileSystemAdapter):
    """The real file system, with write_atomic's failures staged in order (one per write; None: it works)."""

    def __init__(self) -> None:
        super().__init__()
        self.tears: list[_Tear | None] = []

    @override
    def write_atomic(self, dest: str, content: str, mode: int = 0o644) -> None:
        tear = self.tears.pop(0) if self.tears else None
        if tear == _Tear.BEFORE:
            raise no_space()
        super().write_atomic(dest, content, mode)
        if tear == _Tear.AFTER:
            raise OSError(errno.EIO, os.strerror(errno.EIO))


class _Answers:
    """Every command answers exit 0 with no output (the reads the adapter makes need no more here)."""

    def __init__(self) -> None:
        self.cmds: list[list[str]] = []

    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        del kw
        self.cmds.append(list(argv))
        return CommandResult(0, "", "", tuple(argv))


def _only_reads(argv: Sequence[str]) -> bool:
    return list(argv[:2]) != ["systemctl", "daemon-reload"]


class _CheckedRunner:
    """FaultyRunner's answers, raising CommandFailed for a failed command run with check=True, as the real
    runner does."""

    def __init__(self, faulty: FaultyRunner) -> None:
        self.faulty = faulty

    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        r = self.faulty.run(argv, **kw)
        if kw.get("check") and r.returncode != 0:
            raise CommandFailed(list(argv), r.returncode, r.stdout, r.stderr)
        return r


class _Ctx:
    def __init__(self, root: str) -> None:
        self.root = root

    def is_container(self) -> bool:
        return False

    def is_root(self) -> bool:
        return True

    def path(self, host_path: str) -> str:
        return os.path.join(self.root, host_path.lstrip("/"))

    def etc_dir(self) -> str:
        return os.path.join(self.root, "etc")

    def host_run_dir(self, subpath: str) -> str:
        return os.path.join(self.root, "run", subpath.strip("/"))


class AdapterFailureTest(unittest.TestCase):
    """The real adapter over a temporary root: a reload that fails (FaultyRunner over a runner that answers
    every command), a write_atomic that fails before or after its rename (_TearingFs)."""

    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-compress-faults-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        for sub in ("etc/systemd/system", "proc", "run", "var/lib/cst"):
            os.makedirs(os.path.join(self.tmp, sub))
        self.put("etc/fstab", OLD_FSTAB)
        self.put(
            f"etc/systemd/system/{TOP_UNIT}",
            "[Unit]\nDescription=btrfs top level\n\n[Mount]\n"
            f"What=/dev/disk/by-uuid/{UUID}\nWhere=/var/mnt/btrfs-top\nType=btrfs\nOptions={OLD_TOP_OPTIONS}\n",
        )
        self.plan = FaultPlan()
        self.inner = _Answers()
        self.fs = _TearingFs()
        self.adapter = LinuxCompressHostAdapter(
            CompressAdapterConfig(
                entry_file=os.path.join(REPO, "btrfs", "btrfs-compress"),
                fallback_root=REPO,
                proc_dir=os.path.join(self.tmp, "proc"),
                host_root=self.tmp,
            ),
            ctx=_Ctx(self.tmp),
            runner=_CheckedRunner(FaultyRunner(self.plan, self.inner, reads=_only_reads)),
            fs=self.fs,
        )

    def put(self, rel: str, text: str) -> None:
        with open(os.path.join(self.tmp, rel), "w", encoding="utf-8") as fh:
            fh.write(text)

    def text(self, rel: str) -> str:
        with open(os.path.join(self.tmp, rel), encoding="utf-8") as fh:
            return fh.read()

    def reloads(self) -> int:
        return sum(1 for c in self.inner.cmds if not _only_reads(c))

    def test_a_failed_reload_puts_the_old_fstab_back(self) -> None:
        # (a): the reload's error comes out, the old text is back.
        self.plan.arm((FaultMode.FAIL, 1))
        with self.assertRaises(CommandFailed):
            self.adapter.write_fstab(NEW_FSTAB)
        self.assertEqual(self.text("etc/fstab"), OLD_FSTAB)
        self.assertEqual(self.plan.calls, ["systemctl daemon-reload"])

    def test_a_failed_reload_whose_put_back_fails_leaves_the_new_fstab(self) -> None:
        # (b): the put-back's error comes out, the new text stays.
        self.plan.arm((FaultMode.FAIL, 1))
        self.fs.tears = [None, _Tear.BEFORE]
        with self.assertRaises(OSError) as caught:
            self.adapter.write_fstab(NEW_FSTAB)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(self.text("etc/fstab"), NEW_FSTAB)

    def test_a_sync_that_fails_after_the_rename_leaves_the_new_fstab(self) -> None:
        # (c): write_atomic raises with the new text in place; no reload runs.
        self.fs.tears = [_Tear.AFTER]
        with self.assertRaises(OSError) as caught:
            self.adapter.write_fstab(NEW_FSTAB)
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertEqual(self.text("etc/fstab"), NEW_FSTAB)
        self.assertEqual(self.reloads(), 0)

    def test_a_write_that_fails_before_the_rename_changes_nothing(self) -> None:
        self.fs.tears = [_Tear.BEFORE]
        with self.assertRaises(OSError):
            self.adapter.write_fstab(NEW_FSTAB)
        self.assertEqual(self.text("etc/fstab"), OLD_FSTAB)
        self.assertEqual(self.reloads(), 0)

    def test_a_top_mount_whose_put_back_fails_keeps_the_new_unit(self) -> None:
        # (d): the reload after the new unit fails, then the put-back's install fails: its error comes out.
        self.plan.arm((FaultMode.FAIL, 1))
        installs: list[str] = []
        real_install = InstallService.install

        def put_back_fails(svc: InstallService, entries: Sequence[InstallEntry], req: InstallRequest) -> Iterator[str]:
            installs.append(req.values.get("OPTIONS", "") if req.values is not None else "")
            if len(installs) > 1:
                raise no_space()
            return real_install(svc, entries, req)

        with mock.patch.object(InstallService, "install", put_back_fails), self.assertRaises(OSError) as caught:
            self.adapter.update_top_mount(UUID, NEW_TOP_OPTIONS)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertIsInstance(caught.exception.__context__, CommandFailed)
        self.assertEqual(installs, [NEW_TOP_OPTIONS, OLD_TOP_OPTIONS])
        unit = parse_top_mount_unit(self.text(f"etc/systemd/system/{TOP_UNIT}"))
        self.assertEqual(unit.options, NEW_TOP_OPTIONS)

    def test_a_top_mount_whose_reload_fails_is_put_back(self) -> None:
        # The contrast of (d): the put-back works, the reload's error comes out.
        self.plan.arm((FaultMode.FAIL, 1))
        with self.assertRaises(CommandFailed):
            self.adapter.update_top_mount(UUID, NEW_TOP_OPTIONS)
        unit = parse_top_mount_unit(self.text(f"etc/systemd/system/{TOP_UNIT}"))
        self.assertEqual(unit.options, OLD_TOP_OPTIONS)


class CompressWorldTest(unittest.TestCase):
    """The world's own rules: rpm-ostree's edits, the kernel's view of a remount, the torn points."""

    def world(self) -> CompressWorld:
        w = CompressWorld(root_uuid=UUID)
        w.kargs = "root=UUID=x rootflags=subvol=root,compress=zstd:1 quiet\n"
        w.mounts = (
            "/dev/dm-0 /sysroot btrfs ro,noatime,compress=zstd:1,subvol=/root 0 0\n"
            "/dev/dm-0 /var btrfs rw,noatime,compress=zstd:1,subvol=/var 0 0\n"
            "/dev/dm-1 /var/home btrfs rw,noatime,compress=zstd:3,subvol=/home 0 0\n"
        )
        return w

    def test_kargs_edits_delete_the_old_and_append_the_new(self) -> None:
        w = self.world()
        self.assertEqual(w.update_kargs([("rootflags=subvol=root,compress=zstd:1", "rootflags=a")]), (True, ""))
        self.assertEqual(w.kargs, "root=UUID=x quiet rootflags=a\n")

    def test_kargs_edits_of_an_argument_not_there_change_nothing(self) -> None:
        w = self.world()
        before = w.kargs
        ok, err = w.update_kargs([("rootflags=subvol=root,compress=zstd:1", "rootflags=a"), ("nothere", "b")])
        self.assertFalse(ok)
        self.assertIn("nothere", err)
        self.assertEqual(w.kargs, before)

    def test_a_remount_changes_every_mount_of_its_device_and_only_those(self) -> None:
        w = self.world()
        self.assertEqual(w.remount("/var", "compress-force=zstd:2"), (True, ""))
        self.assertEqual(
            w.mounts,
            "/dev/dm-0 /sysroot btrfs ro,noatime,compress-force=zstd:2,subvol=/root 0 0\n"
            "/dev/dm-0 /var btrfs rw,noatime,compress-force=zstd:2,subvol=/var 0 0\n"
            "/dev/dm-1 /var/home btrfs rw,noatime,compress=zstd:3,subvol=/home 0 0\n",
        )
        self.assertEqual(w.remounts, [("/var", "compress-force=zstd:2")])
        self.assertFalse(w.remount("/nothere", "commit=60")[0])

    def test_the_kernel_hides_commit_30_and_compress_no(self) -> None:
        self.assertEqual(kernel_remount_options("rw,commit=60,ssd", "commit=30"), "rw,ssd")
        self.assertEqual(kernel_remount_options("rw,ssd", "commit=60"), "rw,ssd,commit=60")
        self.assertEqual(kernel_remount_options("rw,compress=zstd:1,ssd", "compress=no"), "rw,ssd")
        self.assertEqual(kernel_remount_options("rw,ssd", "compress=zstd:1"), "rw,ssd,compress=zstd:1")

    def test_each_mutating_call_is_a_point_and_a_failure_has_no_effect(self) -> None:
        w = self.world()
        w.fstab = OLD_FSTAB
        w.plan.arm(*((FaultMode.FAIL, n) for n in range(1, 5)))
        with self.assertRaises(OSError):
            w.write_fstab(NEW_FSTAB)
        self.assertEqual(w.update_kargs([("quiet", "loud")]), (False, "No space left on device"))
        self.assertEqual(w.remount("/var", "commit=60"), (False, "No space left on device"))
        with self.assertRaises(OSError):
            w.update_top_mount(UUID, NEW_TOP_OPTIONS)
        self.assertEqual(w.calls, ["fstab write", "kargs", "remount", "top-mount"])
        self.assertEqual((w.fstab, w.top), (OLD_FSTAB, None))
        self.assertNotIn("loud", w.kargs)
        self.assertNotIn("commit=60", w.mounts or "")

    def test_a_torn_point_takes_effect_and_then_fails(self) -> None:
        w = self.world()
        w.fstab = OLD_FSTAB
        w.torn = frozenset({"fstab write", "kargs", "top-mount"})
        w.plan.arm(*((FaultMode.FAIL, n) for n in range(1, 4)))
        with self.assertRaises(OSError):
            w.write_fstab(NEW_FSTAB)
        self.assertEqual(w.update_kargs([("quiet", "loud")]), (False, "No space left on device"))
        with self.assertRaises(OSError):
            w.update_top_mount(UUID, NEW_TOP_OPTIONS)
        self.assertEqual(w.fstab, NEW_FSTAB)
        self.assertIn("loud", w.kargs)
        self.assertEqual(w.top, TopMountInfo(installed=True, uuid=UUID, options=NEW_TOP_OPTIONS))

# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on `cst btrfs compress-force` and `cst btrfs commit-interval` apply and revert
(tests/fault_injection.py), over CompressWorld.

compress-force (btrfs/compress/application.py, CompressService.execute_change) and commit-interval
(btrfs/commit/adapters.py, execute_commit_change) share one port, CompressHostPort, and run here over
CompressWorld (btrfs/tests/fakes.py), its in-memory fake: /etc/fstab, the staged kernel arguments, the live
mounts and the top-mount unit. Each mutating call is a point of a FaultPlan: "fstab write", "kargs",
"remount" and "top-mount" (the forward steps and the rollback's undos alike).

Start states (STARTS): compress-force apply from Fedora's default, after an apply killed after its fstab
write, on a host that is not rpm-ostree booted with no top mount, and with live mounts without compress;
revert from fully applied and after a revert killed after its kargs; commit-interval apply 60 from no
commit=, from commit=90, after an apply killed after its kargs; revert from 60 and from live at 60 with the
files reverted. For each, every mode at every point, and every FAIL followed by a KILL or a second FAIL at
each later point of that run (the rollback's calls included), checked:
- I1: a run that returns 1 without "ROLLBACK INCOMPLETE" left every layer as at the start; one that says
  it names each layer that differs from the start; a run that returns 0 left every layer at the target;
- I2: after any fault, each layer holds its start value or the target, never a third; the /etc/fstab lines
  CST does not manage keep their bytes; a separate btrfs /var/home is never remounted;
- I3: the same command again, with no fault, returns 0 with every layer at the target and status coherent,
  and a third run says "nothing to do" and makes no mutating call;
- I3b: the opposite command from the state the fault left converges too;
- I4 (the question whether a formal spec is worth it): a fault that leaves the next boot's two layers
  disagreeing (/etc/fstab's root lines and the last rootflags= of the staged kernel arguments) must be
  followed by a run again that converges. CST_FAULT_REPORT=1 prints every such case.

AdapterFailureTest pins what the real adapter (btrfs/compress/adapters.py, LinuxCompressHostAdapter) gives
when a step fails after it changed its layer: those outcomes are what CompressWorld's torn points stand for.
CompressWorldTest pins the world's own rules.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
import copy
from dataclasses import dataclass
from enum import StrEnum
import errno
import functools
import os
import shutil
import sys
import tempfile
from typing import ClassVar, override
import unittest
from unittest import mock

from btrfs.commit.adapters import execute_commit_change, read_commit_snapshot
from btrfs.commit.domain import (
    CommitAction,
    CommitParsedArgs,
    assess_commit_coherence,
    effective_commit_seconds,
    format_commit_label,
    has_explicit_commit,
)
from btrfs.compress.adapters import CompressAdapterConfig, LinuxCompressHostAdapter
from btrfs.compress.application import CompressService
from btrfs.compress.domain import (
    FSTAB_UNREAD,
    KARGS_SETTLE_POLL_S,
    KARGS_SETTLE_TIMEOUT_S,
    KARGS_STILL_RUNNING,
    REMOUNT_UNREAD,
    ROOTFLAGS_PREFIX,
    TOP_MOUNT_UNREAD,
    TopMountInfo,
    effective_compress_spec,
    extract_rootflags_tokens,
    parse_fstab_root_entries,
    parse_proc_mounts,
    parse_top_mount_unit,
    pick_root_mounts,
    transaction_running,
)
from btrfs.compress.domain.args import CompressArgs, CompressCommand
from btrfs.tests.fakes import CompressWorld, kernel_remount_options
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.application.install import InstallRequest, InstallService
from cst_core.domain.install import InstallEntry
from cst_core.ports import CommandFailed, CommandResult
from tests.fault_injection import Fault, FaultMode, FaultPlan, FaultyRunner, Killed, cut_points, every_fault, no_space
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
        self.adapter = self.build()

    def build(self) -> LinuxCompressHostAdapter:
        """The adapter over the temporary root, its reloads through self.plan."""
        return LinuxCompressHostAdapter(
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

    def test_an_undo_whose_reload_fails_keeps_the_old_fstab(self) -> None:
        # put_back=False (an undo): the text it writes stays after a failed reload; the error comes out.
        self.put("etc/fstab", NEW_FSTAB)
        self.plan.arm((FaultMode.FAIL, 1))
        with self.assertRaises(CommandFailed):
            self.adapter.write_fstab(OLD_FSTAB, put_back=False)
        self.assertEqual(self.text("etc/fstab"), OLD_FSTAB)

    def test_an_undo_of_the_top_mount_whose_reload_fails_keeps_its_unit(self) -> None:
        self.plan.arm((FaultMode.FAIL, 1))
        with self.assertRaises(CommandFailed):
            self.adapter.update_top_mount(UUID, NEW_TOP_OPTIONS, put_back=False)
        unit = parse_top_mount_unit(self.text(f"etc/systemd/system/{TOP_UNIT}"))
        self.assertEqual(unit.options, NEW_TOP_OPTIONS)

    def run_tool_here(self, tool: Tool) -> tuple[int, str]:
        """One apply of the tool over the real adapter and the temporary root: (exit, its last line)."""
        if tool == Tool.COMPRESS:
            res = CompressService(self.adapter).execute_change(CompressArgs(CompressCommand.APPLY, yes=True))
            return res.exit_code, res.error
        rc, lines = execute_commit_change(self.adapter, CommitParsedArgs(CommitAction.APPLY, seconds=60, yes=True))
        return rc, lines[-1]

    def test_the_undo_of_a_torn_fstab_keeps_the_old_text_when_its_reload_fails(self) -> None:
        # The forward write's reload fails and its put-back fails (the new text stays: the run undoes it); the
        # undo writes the old text and its reload fails too: the old text stays, the undo's error is named.
        for tool in Tool:
            with self.subTest(tool=tool):
                self.put("etc/fstab", OLD_FSTAB)
                self.plan = FaultPlan()
                self.adapter = self.build()
                self.plan.arm((FaultMode.FAIL, 1), (FaultMode.FAIL, 2))
                self.fs.tears = [None, _Tear.BEFORE, None]
                rc, last = self.run_tool_here(tool)
                self.assertEqual(rc, 1, last)
                self.assertIn(f"{INCOMPLETE} (fstab: ", last)
                self.assertEqual(self.text("etc/fstab"), OLD_FSTAB)

    def test_the_undo_of_a_torn_top_mount_keeps_the_old_unit_when_its_reload_fails(self) -> None:
        # /etc/fstab is written; the unit's reload fails and its put-back fails (the new unit stays: the run
        # undoes it first); the undo installs the old unit and its reload fails too: the old unit stays, the
        # undo's error is named, and /etc/fstab is put back.
        real_install = InstallService.install
        for tool in Tool:
            with self.subTest(tool=tool):
                self.put("etc/fstab", OLD_FSTAB)
                self.put(
                    f"etc/systemd/system/{TOP_UNIT}",
                    "[Unit]\nDescription=btrfs top level\n\n[Mount]\n"
                    f"What=/dev/disk/by-uuid/{UUID}\nWhere=/var/mnt/btrfs-top\nType=btrfs\nOptions={OLD_TOP_OPTIONS}\n",
                )
                self.plan = FaultPlan()
                self.adapter = self.build()
                self.plan.arm((FaultMode.FAIL, 2), (FaultMode.FAIL, 3))
                installs: list[str] = []

                def put_back_fails(
                    svc: InstallService,
                    entries: Sequence[InstallEntry],
                    req: InstallRequest,
                    installs: list[str] = installs,
                ) -> Iterator[str]:
                    installs.append(req.values.get("OPTIONS", "") if req.values is not None else "")
                    if len(installs) == 2:
                        raise no_space()
                    return real_install(svc, entries, req)

                with mock.patch.object(InstallService, "install", put_back_fails):
                    rc, last = self.run_tool_here(tool)
                self.assertEqual(rc, 1, last)
                self.assertIn(f"{INCOMPLETE} (top-mount: ", last)
                self.assertEqual(len(installs), 3, installs)  # the new unit, its failed put-back, the undo
                unit = parse_top_mount_unit(self.text(f"etc/systemd/system/{TOP_UNIT}"))
                self.assertEqual(unit.options, OLD_TOP_OPTIONS)
                self.assertEqual(self.text("etc/fstab"), OLD_FSTAB)

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
        w.torn = frozenset({"fstab write", "kargs", "remount", "top-mount"})
        w.plan.arm(*((FaultMode.FAIL, n) for n in range(1, 5)))
        with self.assertRaises(OSError):
            w.write_fstab(NEW_FSTAB)
        self.assertEqual(w.update_kargs([("quiet", "loud")]), (False, "No space left on device"))
        self.assertEqual(w.remount("/var", "commit=60"), (False, "No space left on device"))
        with self.assertRaises(OSError):
            w.update_top_mount(UUID, NEW_TOP_OPTIONS)
        self.assertEqual(w.fstab, NEW_FSTAB)
        self.assertIn("loud", w.kargs)
        self.assertIn("commit=60", w.mounts or "")
        self.assertEqual(w.top, TopMountInfo(installed=True, uuid=UUID, options=NEW_TOP_OPTIONS))


# ---- the fault matrix ----------------------------------------------------------------------------------

HOME_UUID = "0b5e6a1d-0000-4000-8000-00000000cafe"
INCOMPLETE = "ROLLBACK INCOMPLETE"
# The points whose failure can take effect first (AdapterFailureTest; a remount that mount(8) answers a failure
# for after the kernel made it).
TORN_POINTS = frozenset({"fstab write", "kargs", "remount", "top-mount"})
# The words an incomplete rollback names each layer with.
LAYER_WORDS = ("fstab:", "kargs:", "remount:", "top-mount:")


class Tool(StrEnum):
    """Which use case runs."""

    COMPRESS = "compress-force"
    COMMIT = "commit-interval"


class Command(StrEnum):
    APPLY = "apply"
    REVERT = "revert"


@dataclass(frozen=True)
class Start:
    """A state before the run: the tool and command, the layers' options (None: the layer is not there), and
    the pinned numbers of points and of FAIL then a later fault cases (the same count for a KILL and for a
    FAIL as the second fault)."""

    name: str
    tool: Tool
    command: Command
    root: str  # the options of the root's fstab lines and of the live root mounts
    kargs: str | None  # the rootflags= value; None: not rpm-ostree booted
    live: str
    top: str | None
    points: int
    fail_then_later: int
    torn_then_kill: int
    separate_home: bool = False


def _compress_opts(spec: str) -> str:
    return f"subvol=root,degraded,{spec},noatime,flushoncommit".replace(",,", ",")


def _commit_opts(commit: str) -> str:
    return f"subvol=root,degraded,compress-force=zstd:1,noatime,flushoncommit{commit}"


def _top(spec: str) -> str:
    return f"subvolid=5,noatime,degraded,{spec},ssd,flushoncommit".replace(",,", ",")


ZSTD, FORCED = "compress=zstd:1", "compress-force=zstd:1"
STARTS: tuple[Start, ...] = (
    # P1: Fedora's default, every layer compress=zstd:1.
    Start("P1", Tool.COMPRESS, Command.APPLY, ZSTD, ZSTD, ZSTD, ZSTD, points=4, fail_then_later=6, torn_then_kill=10),
    # P2: an apply killed after its fstab write.
    Start("P2", Tool.COMPRESS, Command.APPLY, FORCED, ZSTD, ZSTD, ZSTD, points=3, fail_then_later=3, torn_then_kill=6),
    # P3: not rpm-ostree booted, no top mount, a separate btrfs /var/home: two layers.
    Start(
        "P3",
        Tool.COMPRESS,
        Command.APPLY,
        ZSTD,
        None,
        ZSTD,
        None,
        points=2,
        fail_then_later=1,
        torn_then_kill=3,
        separate_home=True,
    ),
    # P4: the live mounts without compress (the rollback remounts with compress=no).
    Start("P4", Tool.COMPRESS, Command.APPLY, ZSTD, ZSTD, "", ZSTD, points=4, fail_then_later=6, torn_then_kill=10),
    # P5: revert from fully applied.
    Start(
        "P5",
        Tool.COMPRESS,
        Command.REVERT,
        FORCED,
        FORCED,
        FORCED,
        FORCED,
        points=4,
        fail_then_later=6,
        torn_then_kill=10,
    ),
    # P6: a revert killed after its kargs (fstab and kargs back), the rest still forced.
    Start(
        "P6", Tool.COMPRESS, Command.REVERT, ZSTD, ZSTD, FORCED, FORCED, points=2, fail_then_later=1, torn_then_kill=3
    ),
    # C1: no commit= anywhere (30 s), a separate btrfs /var/home.
    Start(
        "C1",
        Tool.COMMIT,
        Command.APPLY,
        "",
        "",
        "",
        "",
        points=4,
        fail_then_later=6,
        torn_then_kill=10,
        separate_home=True,
    ),
    # C2: commit=90 everywhere (the undo remounts with commit=90).
    Start(
        "C2",
        Tool.COMMIT,
        Command.APPLY,
        ",commit=90",
        ",commit=90",
        ",commit=90",
        ",commit=90",
        points=4,
        fail_then_later=6,
        torn_then_kill=10,
    ),
    # C3: an apply killed after its kargs (fstab and kargs at 60).
    Start(
        "C3",
        Tool.COMMIT,
        Command.APPLY,
        ",commit=60",
        ",commit=60",
        "",
        "",
        points=2,
        fail_then_later=1,
        torn_then_kill=3,
    ),
    # C4: revert from 60 everywhere.
    Start(
        "C4",
        Tool.COMMIT,
        Command.REVERT,
        ",commit=60",
        ",commit=60",
        ",commit=60",
        ",commit=60",
        points=4,
        fail_then_later=6,
        torn_then_kill=10,
    ),
    # C5: the live mounts at 60, the files reverted.
    Start("C5", Tool.COMMIT, Command.REVERT, "", "", ",commit=60", "", points=1, fail_then_later=0, torn_then_kill=1),
)
BY_NAME = {s.name: s for s in STARTS}

type Layers = tuple[tuple[str, ...], str | None, tuple[str, ...], str | None]


def start_world(s: Start) -> CompressWorld:
    """The machine a start state describes."""
    opts = _compress_opts if s.tool == Tool.COMPRESS else _commit_opts
    w = CompressWorld(is_ostree=s.kargs is not None, root_uuid=UUID)
    home_fstab = (
        f"UUID={HOME_UUID} /var/home btrfs subvol=home,compress=zstd:3,noatime 0 0\n"
        if s.separate_home
        else f"UUID={UUID} /var/home btrfs {opts(s.root).replace('subvol=root', 'subvol=home')} 0 0\n"
    )
    w.fstab = (
        "# Static header comment\n"
        f"UUID={UUID} /\t\tbtrfs   {opts(s.root)} 0 0\n"
        f"UUID={UUID} /var btrfs {opts(s.root).replace('subvol=root', 'subvol=var')} 0 0\n"
        + home_fstab
        + "UUID=1111-2222        /boot/efi       vfat    umask=0077,shortname=winnt 0 2\n"
        + "UUID=dead-beef        /mnt/backup     btrfs   defaults,compress=zstd:3,noauto 0 0\n"
    )
    if s.kargs is not None:
        w.kargs = f"ostree=/ostree/boot.1/fedora/abc/0 rhgb quiet root=UUID={UUID} rw rootflags={opts(s.kargs)} log_buf_len=4M\n"
        w.cmdline = w.kargs
    live_opts = (
        opts(s.live)
        .replace("subvol=root,degraded,", "rw,seclabel,noatime,degraded,")
        .replace(",noatime,flushoncommit", ",ssd,flushoncommit")
    )
    home_mount = (
        "/dev/dm-1 /var/home btrfs rw,seclabel,noatime,compress=zstd:3,ssd,subvol=/home 0 0\n"
        if s.separate_home
        else f"/dev/dm-0 /var/home btrfs {live_opts},subvol=/home 0 0\n"
    )
    w.mounts = (
        f"/dev/dm-0 /sysroot btrfs {live_opts.replace('rw,', 'ro,', 1)},subvol=/root 0 0\n"
        f"/dev/dm-0 /var btrfs {live_opts},subvol=/var 0 0\n" + home_mount
    )
    if s.top is not None:
        top = (
            _top(s.top)
            if s.tool == Tool.COMPRESS
            else f"subvolid=5,noatime,degraded,compress-force=zstd:1,ssd,flushoncommit{s.top}"
        )
        w.top = TopMountInfo(installed=True, uuid=UUID, options=top)
    return w


def _value(tool: Tool, options: str) -> str:
    """What one layer's options say, in the tool's terms."""
    if tool == Tool.COMPRESS:
        spec = effective_compress_spec(options)
        return spec.raw if spec is not None else "(none)"
    return format_commit_label(effective_commit_seconds(options), explicit=has_explicit_commit(options))


def layers(tool: Tool, w: CompressWorld) -> Layers:
    """(fstab's root lines, the last rootflags= of the staged kernel arguments, the live root mounts, the
    top-mount unit), each as _value reads it (None: the layer is not there)."""
    fstab = tuple(_value(tool, e.options) for e in parse_fstab_root_entries(w.fstab or "", UUID))
    flags = extract_rootflags_tokens(w.kargs) if w.ostree else ()
    kargs = _value(tool, flags[-1].removeprefix(ROOTFLAGS_PREFIX)) if flags else None
    live = tuple(_value(tool, m.options) for m in pick_root_mounts(parse_proc_mounts(w.mounts or "")))
    top = _value(tool, w.top.options) if w.top is not None else None
    return fstab, kargs, live, top


def next_boot_disagrees(tool: Tool, w: CompressWorld) -> bool:
    """The next boot's /etc/fstab root lines and its rootflags= say different things."""
    fstab, kargs, _live, _top = layers(tool, w)
    return kargs is not None and any(v != kargs for v in fstab)


def run_tool(s: Start, w: CompressWorld, command: Command) -> tuple[int, str, str]:
    """One run of the tool's command over the world: (exit, every line, the error)."""
    if s.tool == Tool.COMPRESS:
        cmd = CompressCommand.REVERT if command == Command.REVERT else CompressCommand.APPLY
        res = CompressService(w).execute_change(CompressArgs(cmd, yes=True))
        return res.exit_code, "\n".join((*res.messages, res.error)), res.error
    args = (
        CommitParsedArgs(CommitAction.REVERT, seconds=30, revert=True, yes=True)
        if command == Command.REVERT
        else CommitParsedArgs(CommitAction.APPLY, seconds=60, yes=True)
    )
    rc, lines = execute_commit_change(w, args)
    return rc, "\n".join(lines), lines[-1] if rc else ""


def coherent(s: Start, w: CompressWorld) -> bool:
    """What status says of the layers."""
    if s.tool == Tool.COMPRESS:
        return CompressService(w).gather_status().coherence.coherent
    return assess_commit_coherence(read_commit_snapshot(w)[0]).coherent


def unmanaged_lines(w: CompressWorld) -> list[str]:
    """The /etc/fstab lines that are not the root filesystem's."""
    roots = {(e.source, e.target) for e in parse_fstab_root_entries(w.fstab or "", UUID)}
    return [ln for ln in (w.fstab or "").splitlines(keepends=True) if tuple(ln.split()[:2]) not in roots]


class CompressFaultBase(unittest.TestCase):
    """The fixture and the checks; each subclass names its start state."""

    start: ClassVar[Start]
    torn: ClassVar[frozenset[str]] = frozenset()
    disagreeing: ClassVar[list[str]]

    @override
    @classmethod
    def setUpClass(cls) -> None:
        cls.disagreeing = []

    @override
    @classmethod
    def tearDownClass(cls) -> None:
        if os.environ.get("CST_FAULT_REPORT") == "1":
            for line in cls.disagreeing:
                sys.stderr.write(f"{cls.start.name}: next boot disagrees: {line}\n")

    @override
    def setUp(self) -> None:
        self.world = self.fresh()

    def fresh(self, *, torn: frozenset[str] = frozenset()) -> CompressWorld:
        self.world = start_world(self.start)
        self.world.torn = torn
        return self.world

    def run_once(self, *faults: Fault, command: Command | None = None) -> tuple[int, str, str] | None:
        """One run with these faults: (exit, its lines, its error); None when killed."""
        self.world.plan.arm(*faults)
        try:
            return run_tool(self.start, self.world, command or self.start.command)
        except Killed:
            return None

    def target(self) -> Layers:
        w = self.fresh()
        rc, out, _err = run_tool(self.start, w, self.start.command)
        self.assertEqual(rc, 0, out)
        return layers(self.start.tool, w)

    def check(self, *faults: Fault, torn: frozenset[str] = frozenset()) -> None:
        when = f"{self.start.name} {faults} torn={sorted(torn)}"
        tool = self.start.tool
        target = self.target()
        w = self.fresh(torn=torn)
        start = layers(tool, w)
        kept = unmanaged_lines(w)
        result = self.run_once(*faults)
        now = layers(tool, w)
        self.check_undo_order(faults, torn, killed=result is None)
        # I1
        if result is not None:
            rc, out, err = result
            if rc == 0:
                self.assertEqual(now, target, f"{when}: exit 0: {out}")
            else:
                self.assertEqual(rc, 1, f"{when}: {out}")
                if INCOMPLETE not in err:
                    self.assertEqual(now, start, f"{when}: exit 1 without {INCOMPLETE}: {err}")
                else:
                    for layer, word in zip(range(4), LAYER_WORDS, strict=True):
                        if now[layer] != start[layer]:
                            self.assertIn(word, err, f"{when}: a layer that differs is not named: {err}")
        # I2
        for layer in range(4):
            self.assertIn(now[layer], (start[layer], target[layer]), f"{when}: layer {layer} holds a third value")
        self.assertEqual(unmanaged_lines(w), kept, f"{when}: a line CST does not manage changed")
        if self.start.separate_home:
            self.assertNotIn("/var/home", [t for t, _o in w.remounts], when)
        disagrees = next_boot_disagrees(tool, w)
        left = copy.deepcopy(w)
        # I3
        w.torn = frozenset()
        again = self.run_once()
        converged = again is not None and again[0] == 0 and layers(tool, w) == target
        if disagrees:
            self.disagreeing.append(f"{when}: {layers(tool, left)}, the run again converges: {converged}")
        self.assertTrue(converged, f"{when}: the run again: {again}")  # I4: no disagreement that stays
        self.assertTrue(coherent(self.start, w), f"{when}: status after the run again")
        w.calls.clear()
        third = self.run_once() or (None, "", "")
        self.assertEqual(third[0], 0, f"{when}: the third run: {third}")
        self.assertIn("nothing to do", third[1], f"{when}: the third run")
        self.assertEqual(w.calls, [], f"{when}: the third run changed something")
        # I3b
        self.world = left
        left.torn = frozenset()
        other = Command.REVERT if self.start.command == Command.APPLY else Command.APPLY
        back = self.run_once(command=other)
        self.assertEqual((back or (None,))[0], 0, f"{when}: {other} from the state left: {back}")
        self.assertTrue(coherent(self.start, left), f"{when}: status after {other}")
        self.assertIn("nothing to do", (self.run_once(command=other) or (None, "", ""))[1], when)

    def check_undo_order(self, faults: tuple[Fault, ...], torn: frozenset[str], *, killed: bool) -> None:
        """I5: after the first failure (at call k), the rollback's calls undo the calls before it that took
        effect (and the failed one when it took effect anyway, a torn point), in reverse; a run killed while it
        rolls back made the first ones of them."""
        mode, k = faults[0]
        if mode == FaultMode.KILL:
            return
        calls = self.world.plan.calls
        done = calls[:k] if calls[k - 1] in torn else calls[: k - 1]
        expected = list(reversed(done))
        undone = calls[k:]
        when = f"{self.start.name} {faults} torn={sorted(torn)}: the rollback's calls"
        if killed:
            self.assertEqual(undone, expected[: len(undone)], when)
        else:
            self.assertEqual(undone, expected, when)

    def points(self, *faults: Fault, torn: frozenset[str] = frozenset()) -> list[str]:
        """The mutating calls of one run from the start state with these faults."""
        self.fresh(torn=torn)
        if not faults:
            return cut_points(self.world.plan, self.run_once)
        self.run_once(*faults)
        return list(self.world.plan.calls)

    def every(self, mode: FaultMode) -> None:
        calls = self.points()
        self.assertEqual(len(calls), self.start.points, calls)
        for fault in every_fault(len(calls), (mode,)):
            with self.subTest(fault=fault, call=calls[fault[1] - 1]):
                self.check(fault)

    def torn_points(self) -> int:
        """The points of a clean run other than the fstab write that can fail after they took effect."""
        return sum(1 for c in self.points() if c in TORN_POINTS - {"fstab write"})

    def every_torn_failure(self, then: FaultMode | None) -> int:
        """Every FAIL at a point that can fail after it took effect (TORN_POINTS), alone (then None) or followed
        by a fault of mode then at each later point of that run; the number of cases."""
        cases = 0
        for k, call in enumerate(self.points(), 1):
            if call not in TORN_POINTS:
                continue
            torn = frozenset({call})
            if then is None:
                with self.subTest(torn=call, at=k):
                    cases += 1
                    self.check((FaultMode.FAIL, k), torn=torn)
                continue
            for later in every_fault(len(self.points((FaultMode.FAIL, k), torn=torn)), (then,), after=k):
                with self.subTest(torn=call, at=k, then=later):
                    cases += 1
                    self.check((FaultMode.FAIL, k), later, torn=torn)
        return cases

    def every_failure_then(self, mode: FaultMode) -> None:
        """Every FAIL, then a second fault of MODE at each later point of that run (a kill or a failure
        while it rolls back)."""
        cases = 0
        for fail in every_fault(self.start.points, (FaultMode.FAIL,)):
            for then in every_fault(len(self.points(fail)), (mode,), after=fail[1]):
                with self.subTest(fault=fail, then=then):
                    cases += 1
                    self.check(fail, then)
        self.assertEqual(cases, self.start.fail_then_later)


class P1FaultTest(CompressFaultBase):
    """compress-force apply from Fedora's default (compress=zstd:1 everywhere)."""

    start = BY_NAME["P1"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class P2FaultTest(CompressFaultBase):
    """compress-force apply after one killed after its fstab write."""

    start = BY_NAME["P2"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class P3FaultTest(CompressFaultBase):
    """compress-force apply on a host that is not rpm-ostree booted, with no top mount and a separate /var/home."""

    start = BY_NAME["P3"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class P4FaultTest(CompressFaultBase):
    """compress-force apply with live mounts without compress."""

    start = BY_NAME["P4"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class P5FaultTest(CompressFaultBase):
    """compress-force revert from fully applied."""

    start = BY_NAME["P5"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class P6FaultTest(CompressFaultBase):
    """compress-force revert after one killed after its kargs."""

    start = BY_NAME["P6"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class C1FaultTest(CompressFaultBase):
    """commit-interval apply 60 from no commit= anywhere, with a separate /var/home."""

    start = BY_NAME["C1"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class C2FaultTest(CompressFaultBase):
    """commit-interval apply 60 from commit=90 everywhere."""

    start = BY_NAME["C2"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class C3FaultTest(CompressFaultBase):
    """commit-interval apply 60 after one killed after its kargs."""

    start = BY_NAME["C3"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class C4FaultTest(CompressFaultBase):
    """commit-interval revert from 60 everywhere."""

    start = BY_NAME["C4"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class C5FaultTest(CompressFaultBase):
    """commit-interval revert from live mounts at 60 and the files reverted."""

    start = BY_NAME["C5"]

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.KILL)

    def test_every_failure_then_a_failure_at_each_later_point(self) -> None:
        self.every_failure_then(FaultMode.FAIL)

    def test_every_failure_that_took_effect(self) -> None:
        self.assertEqual(self.every_torn_failure(None), self.points().count("fstab write") + self.torn_points())

    def test_every_failure_that_took_effect_then_a_kill_at_each_later_point(self) -> None:
        self.assertEqual(self.every_torn_failure(FaultMode.KILL), self.start.torn_then_kill)


class VerificationFailureTest(unittest.TestCase):
    """A remount that answers it worked while the mounts do not show the option: the verification after it
    fails, the remount is undone with the others in reverse, the layers are as at the start and a run
    again converges."""

    def test_a_remount_the_mounts_do_not_show_is_undone_too(self) -> None:
        cases = 0
        for s in STARTS:
            w = start_world(s)
            forward = cut_points(w.plan, functools.partial(run_tool, s, w, s.command))
            if "remount" not in forward:
                continue
            with self.subTest(start=s.name):
                cases += 1
                w = start_world(s)
                start = layers(s.tool, w)
                w.ignored_remounts = 1
                rc, out, err = run_tool(s, w, s.command)
                self.assertEqual(rc, 1, out)
                self.assertIn("Post-remount verification failed", err)
                self.assertTrue(err.endswith("; previous configuration restored."), err)
                done = forward[: forward.index("remount") + 1]
                self.assertEqual(w.calls, [*done, *reversed(done)])
                self.assertEqual(layers(s.tool, w), start)
                self.assertEqual(run_tool(s, w, s.command)[0], 0)
        self.assertEqual(cases, len(STARTS))


class UnreadableAfterFailureTest(unittest.TestCase):
    """A step that fails and whose layer cannot be read back after it (read_fstab, read_top_mount: None),
    whether the failure took effect or not: the run cannot tell, so ROLLBACK INCOMPLETE names that layer as
    unknown, and only the layers before it are undone, in reverse; once the layer reads again, a run again
    converges."""

    def test_a_layer_that_cannot_be_read_back_is_named_unknown(self) -> None:
        cases = 0
        for s in STARTS:
            w = start_world(s)
            forward = cut_points(w.plan, functools.partial(run_tool, s, w, s.command))
            for point, words in (
                ("fstab write", FSTAB_UNREAD),
                ("remount", REMOUNT_UNREAD),
                ("top-mount", TOP_MOUNT_UNREAD),
            ):
                if point not in forward:
                    continue
                done = forward[: forward.index(point)]
                for torn in (frozenset(), frozenset({point})):
                    with self.subTest(start=s.name, point=point, torn=sorted(torn)):
                        cases += 1
                        w = start_world(s)
                        w.torn = torn
                        w.unreadable_after = frozenset({point})
                        w.fail(point, no_space(), times=1)
                        rc, out, err = run_tool(s, w, s.command)
                        self.assertEqual(rc, 1, out)
                        self.assertTrue(err.endswith(f"; {INCOMPLETE} ({words})"), err)
                        self.assertEqual(w.calls, [*done, point, *reversed(done)])
                        w.unreadable.clear()
                        w.torn = frozenset()
                        self.assertEqual(run_tool(s, w, s.command)[0], 0)
                        self.assertTrue(coherent(s, w), s.name)
        self.assertEqual(cases, 54)


class KargsStillStagingTest(unittest.TestCase):
    """A kargs change that answers a failure while its transaction goes on (CompressWorld.lagging_kargs): the
    run reads the kernel arguments once no transaction runs, so it sees the change staged and undoes it; when
    the transaction still runs after the wait (stuck_kargs), or the kernel arguments cannot be read back, the
    run names them as unknown and undoes the layers before them."""

    def cases(self) -> Iterator[tuple[Start, list[str]]]:
        for s in STARTS:
            w = start_world(s)
            forward = cut_points(w.plan, functools.partial(run_tool, s, w, s.command))
            if "kargs" in forward:
                yield s, forward[: forward.index("kargs")]

    def world(self, s: Start) -> CompressWorld:
        w = start_world(s)
        w.torn = frozenset({"kargs"})
        w.lagging_kargs = True
        w.fail("kargs", no_space(), times=1)
        return w

    def test_a_change_staged_after_its_failure_is_undone(self) -> None:
        cases = 0
        for s, done in self.cases():
            with self.subTest(start=s.name):
                cases += 1
                w = self.world(s)
                start = layers(s.tool, w)
                rc, out, err = run_tool(s, w, s.command)
                w.settle()  # a transaction still running after the run ends
                self.assertEqual(rc, 1, out)
                self.assertTrue(err.endswith("; previous configuration restored."), err)
                self.assertEqual(w.calls, [*done, "kargs", "kargs", *reversed(done)])
                self.assertEqual(layers(s.tool, w), start)
        self.assertEqual(cases, 7)

    def test_a_transaction_still_running_or_kargs_unread_are_named_unknown(self) -> None:
        cases = 0
        for s, done in self.cases():
            for why in ("stuck", "unreadable"):
                with self.subTest(start=s.name, why=why):
                    cases += 1
                    w = self.world(s)
                    if why == "stuck":
                        w.stuck_kargs = True
                        said = KARGS_STILL_RUNNING
                    else:
                        w.unreadable_after = frozenset({"kargs"})
                        said = "error: cannot read the kernel arguments"
                    rc, out, err = run_tool(s, w, s.command)
                    self.assertEqual(rc, 1, out)
                    self.assertTrue(
                        err.endswith(
                            f"; {INCOMPLETE} (kargs: cannot read rpm-ostree kargs back ({said}), its state is unknown)"
                        ),
                        err,
                    )
                    self.assertEqual(w.calls, [*done, "kargs", *reversed(done)])
                    w.stuck_kargs = False
                    w.unreadable.clear()
                    w.settle()
                    w.torn = frozenset()
                    self.assertEqual(run_tool(s, w, s.command)[0], 0)
                    self.assertTrue(coherent(s, w), s.name)
        self.assertEqual(cases, 14)


class _Clock:
    """A clock whose sleep moves time on without sleeping."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class _StatusRunner:
    """`rpm-ostree status --json` answers in order (the last one again when they run out), `rpm-ostree kargs`
    the given kernel arguments."""

    def __init__(self, *status: CommandResult) -> None:
        self.status = list(status)
        self.cmds: list[list[str]] = []

    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        del kw
        self.cmds.append(list(argv))
        if list(argv[:2]) == ["rpm-ostree", "status"]:
            return self.status.pop(0) if len(self.status) > 1 else self.status[0]
        return CommandResult(0, "rootflags=a\n", "", tuple(argv))


def _status(transaction: str, rc: int = 0, stderr: str = "") -> CommandResult:
    return CommandResult(rc, f'{{"deployments": [], "transaction": {transaction}}}', stderr, ("rpm-ostree",))


BUSY = '["KernelArgs", ":1.92", "/org/projectatomic/rpmostree1/fedora/Transaction"]'


class SettledKargsAdapterTest(unittest.TestCase):
    """LinuxCompressHostAdapter.read_settled_kargs: it asks `rpm-ostree status --json` until it names no
    transaction, then reads the kernel arguments; it gives up after KARGS_SETTLE_TIMEOUT_S."""

    def adapter(self, runner: _StatusRunner, clock: _Clock) -> LinuxCompressHostAdapter:
        return LinuxCompressHostAdapter(
            CompressAdapterConfig(entry_file=os.path.join(REPO, "btrfs", "btrfs-compress"), fallback_root=REPO),
            ctx=_Ctx(tempfile.gettempdir()),
            runner=runner,
            clock=clock,
        )

    def test_it_waits_for_the_transaction_to_end_then_reads(self) -> None:
        runner, clock = _StatusRunner(_status(BUSY), _status(BUSY), _status("null")), _Clock()
        self.assertEqual(self.adapter(runner, clock).read_settled_kargs()[:2], (True, "rootflags=a\n"))
        self.assertEqual(clock.slept, [KARGS_SETTLE_POLL_S, KARGS_SETTLE_POLL_S])
        self.assertEqual(runner.cmds[-1], ["rpm-ostree", "kargs"])
        self.assertEqual(runner.cmds.count(["rpm-ostree", "status", "--json"]), 3)

    def test_it_reads_at_once_when_none_runs(self) -> None:
        runner, clock = _StatusRunner(_status("null")), _Clock()
        self.assertTrue(self.adapter(runner, clock).read_settled_kargs()[0])
        self.assertEqual(clock.slept, [])

    def test_a_transaction_that_does_not_end_is_still_running(self) -> None:
        runner, clock = _StatusRunner(_status(BUSY)), _Clock()
        self.assertEqual(self.adapter(runner, clock).read_settled_kargs(), (False, "", KARGS_STILL_RUNNING))
        self.assertGreaterEqual(sum(clock.slept), KARGS_SETTLE_TIMEOUT_S)
        self.assertLess(sum(clock.slept), KARGS_SETTLE_TIMEOUT_S + KARGS_SETTLE_POLL_S)
        self.assertNotIn(["rpm-ostree", "kargs"], runner.cmds)

    def test_a_status_that_cannot_answer_is_no_reading(self) -> None:
        failed = CommandResult(1, "", "error: daemon gone\n", ("rpm-ostree",))
        garbage = CommandResult(0, "not json", "", ("rpm-ostree",))
        for res, why in ((failed, "rpm-ostree status failed: error: daemon gone"), (garbage, "no transaction state")):
            with self.subTest(why=why):
                runner, clock = _StatusRunner(res), _Clock()
                ok, text, err = self.adapter(runner, clock).read_settled_kargs()
                self.assertEqual((ok, text), (False, ""))
                self.assertIn(why, err)
                self.assertNotIn(["rpm-ostree", "kargs"], runner.cmds)

    def test_transaction_running_reads_the_status_answer(self) -> None:
        answers = [transaction_running('{"transaction": null}'), transaction_running(f'{{"transaction": {BUSY}}}')]
        self.assertEqual(answers, [False, True])
        for text in ("", "[]", '{"deployments": []}', "{"):
            with self.subTest(text=text):
                self.assertIsNone(transaction_running(text))


class AlreadyAtTheTargetTest(unittest.TestCase):
    """N: a run from the target has no point and makes no mutating call."""

    def test_no_point_and_no_call(self) -> None:
        for s in STARTS:
            with self.subTest(start=s.name):
                w = start_world(s)
                self.assertEqual(run_tool(s, w, s.command)[0], 0)
                w.calls.clear()
                self.assertEqual(cut_points(w.plan, functools.partial(run_tool, s, w, s.command)), [])
                self.assertEqual(w.calls, [])

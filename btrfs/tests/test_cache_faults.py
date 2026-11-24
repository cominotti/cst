# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on `cst btrfs cache attach` and `detach` (tests/fault_injection.py).

The command runs in process (run_cache_mount_cli, under a test root as test_cache runs it). Its
mutating calls go through a FaultPlan: the folder and file calls by patching the names
btrfs/cache/adapters.py calls (ensure_dir and write_atomic: the folders, the CACHEDIR.TAG, the
/etc/fstab write and its put-back) and LocalFileSystemAdapter.remove_file (the put-back when there
was no /etc/fstab), the commands through FaultyRunner over FakeMountRunner (mount --bind, umount,
systemctl daemon-reload; findmnt only reads). FakeMountRunner keeps the mount table. os.fsync is
stubbed.

Start states: an attach from nothing, from no /etc/fstab at all, and after an attach killed after
its first mount; a detach of both binds, and after a detach killed after its first umount (the
lines already gone). For each, every mode at every point, and every FAIL followed by a KILL or a second FAIL at
each later point of that run (a run that dies, or fails again, while it rolls back), checked:
- I1: every managed line of the user names a bind that is mounted from its source, unless an
  attach returned non-zero and its last line says ROLLBACK INCOMPLETE (a line that would mount at
  the next boot for a bind that is not mounted is what PR 14 and the detach fix ruled out). A
  detach keeps I1 even when its rollback is incomplete: a bind it cannot mount again gets no line
  back, and an /etc/fstab it cannot put back keeps fewer lines;
- I2: exit 0 means the target: attach, both binds mounted from source and /etc/fstab as
  plan_attach_fstab gives it (the other lines kept); detach, no bind of CST and no line of the user;
- I3: a non-zero exit leaves the mounts and /etc/fstab as at the start, or its last line says
  ROLLBACK INCOMPLETE (the folders and the tag an attach made may stay: btrfs/README.md 9.4);
- I4: no traceback, and the same command again with no fault returns 0 and reaches the target; a
  third run says "nothing to do" and runs no mount, umount or reload.

Not checked: that systemd has loaded /etc/fstab after a killed run. An attach killed after its
fstab write and before its daemon-reload says "nothing to do" when run again and runs no reload
(F-C2 in the R5 out-of-scope notes; the generator reads /etc/fstab at the next boot anyway).

One class per start state (a work unit of tests/run_unit.py), one method per mode family, the case
counts pinned.
"""

from __future__ import annotations

from collections.abc import Generator, Sequence
import contextlib
from dataclasses import dataclass
import functools
import io
import os
import shutil
import tempfile
from typing import ClassVar, override
import unittest
from unittest import mock

from btrfs.cache import adapters as cache_adapters
from btrfs.cache.cli import run_cache_mount_cli
from btrfs.cache.domain import (
    CACHE_BASE,
    SCRATCHPAD_BASE,
    CacheMountKind,
    CacheMountUser,
    build_user_bindings,
    parse_managed_fstab_entries,
    plan_attach_fstab,
    plan_detach_fstab,
)
from btrfs.tests.fakes import CacheTestCtx, FakeMountRunner
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.adapters.runtime import ManagedCliEnv
from tests.fault_injection import Fault, FaultMode, FaultPlan, FaultyRunner, Killed, cut_points, every_fault, stub_fsync
from tests.repo_files import REPO

USER = CacheMountUser("danilo", 1000, 1000, "/var/home/danilo")
BINDINGS = build_user_bindings(USER)
BASE_FSTAB = "/dev/mapper/cache /var/mnt/cache btrfs subvol=cache,nofail 0 0\n"
INCOMPLETE = "ROLLBACK INCOMPLETE"
ATTACH, DETACH = "attach", "detach"


@dataclass(frozen=True)
class Start:
    """A state before the run: the command, /etc/fstab (None: no file), the binds mounted from their
    source (targets), and the pinned numbers of points and of FAIL then a later fault cases (the same
    count for a KILL and for a FAIL as the second fault)."""

    action: str
    fstab: str | None
    mounted: tuple[str, ...]
    points: int
    fail_then_later: int


ATTACHED_FSTAB = plan_attach_fstab(BASE_FSTAB, BINDINGS)
BOTH = tuple(b.target for b in BINDINGS)
ATTACH_FRESH = Start(ATTACH, BASE_FSTAB, (), points=9, fail_then_later=6)
ATTACH_NO_FSTAB = Start(ATTACH, None, (), points=9, fail_then_later=6)
# An attach killed after its first mount: the cache bind mounted, no line yet.
ATTACH_HALF_DONE = Start(ATTACH, BASE_FSTAB, (BOTH[0],), points=7, fail_then_later=3)
DETACH_FULL = Start(DETACH, ATTACHED_FSTAB, BOTH, points=4, fail_then_later=6)
# A detach killed after its first umount: the lines gone and the reload done, the scratchpad still mounted.
DETACH_HALF_DONE = Start(DETACH, BASE_FSTAB, (BOTH[1],), points=1, fail_then_later=0)


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def reads(argv: Sequence[str]) -> bool:
    return list(argv[:1]) == ["findmnt"]


class CacheFaultBase(unittest.TestCase):
    """The fixture and the checks; each subclass names its start state."""

    start: ClassVar[Start]

    @override
    def setUp(self) -> None:
        stub_fsync(self)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-cache-faults-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.fstab = os.path.join(self.tmp, "etc", "fstab")
        self.entry = os.path.join(REPO, "btrfs", "btrfs-cache")
        self.plan = FaultPlan()

    # ---- the fixture ------------------------------------------------------

    def fresh(self) -> FakeMountRunner:
        """The start state on disk and in the mount table."""
        s = self.start
        for top in ("etc", "var"):
            shutil.rmtree(os.path.join(self.tmp, top), ignore_errors=True)
        for d in ("etc", "var/mnt/cache/danilo", "var/mnt/scratchpad/danilo", "var/home/danilo"):
            os.makedirs(os.path.join(self.tmp, d))
        with open(os.path.join(self.tmp, "etc", "passwd"), "w", encoding="utf-8") as fh:
            fh.write("root:x:0:0:root:/root:/bin/bash\ndanilo:x:1000:1000:Danilo:/var/home/danilo:/bin/bash\n")
        if s.fstab is not None:
            with open(self.fstab, "w", encoding="utf-8") as fh:
                fh.write(s.fstab)
        model = FakeMountRunner(self.tmp)
        model.add_mount("/dev/mapper/cache", CACHE_BASE, fsroot="/cache")
        model.add_mount("/dev/mapper/cache", SCRATCHPAD_BASE, fsroot="/scratchpad")
        for b in BINDINGS:
            if b.target in s.mounted:
                os.makedirs(os.path.join(self.tmp, b.target.lstrip("/")))
                model.run(["mount", "--bind", self.tmp + b.source, self.tmp + b.target])
        model.calls.clear()
        return model

    @contextlib.contextmanager
    def faulty_files(self) -> Generator[None]:
        """The adapter's own file calls through the plan while the block runs."""
        plan = self.plan
        real_ensure, real_write = cache_adapters.ensure_dir, cache_adapters.write_atomic
        real_remove = LocalFileSystemAdapter.remove_file

        def ensure_dir(path: str, mode: int, *, relabel: bool = False) -> None:
            plan.step(
                f"ensure_dir {path.removeprefix(self.tmp)}", functools.partial(real_ensure, path, mode, relabel=relabel)
            )

        def write_atomic(path: str, data: str | bytes, **kw: object) -> None:
            plan.step(f"write_atomic {os.path.basename(path)}", functools.partial(real_write, path, data, **kw))

        def remove_file(fs: LocalFileSystemAdapter, path: str) -> bool:
            return plan.step(f"remove_file {os.path.basename(path)}", functools.partial(real_remove, fs, path))

        with (
            mock.patch.object(cache_adapters, "ensure_dir", ensure_dir),
            mock.patch.object(cache_adapters, "write_atomic", write_atomic),
            mock.patch.object(LocalFileSystemAdapter, "remove_file", remove_file),
        ):
            yield

    def name(self, argv: Sequence[str]) -> str:
        return " ".join(a.removeprefix(self.tmp) for a in argv)

    def run_once(self, model: FakeMountRunner, *faults: Fault) -> tuple[int, list[str]] | None:
        """One run of the start's command with these faults: (exit, its stderr lines); None when killed."""
        self.plan.arm(*faults)
        out, err = io.StringIO(), io.StringIO()
        runner = FaultyRunner(self.plan, model, reads=reads, name=self.name)
        deps = ManagedCliEnv(
            entry_file=self.entry,
            env={"CST_BTRFS_CACHE_ROOT": self.tmp, "USER": USER.name},
            out=out,
            err=err,
            ctx=CacheTestCtx(),
            runner=runner,
        )
        with self.faulty_files():
            try:
                rc = run_cache_mount_cli([self.start.action], deps=deps)
            except Killed:
                return None
        return rc, (out.getvalue() + err.getvalue()).splitlines()

    # ---- what the state is --------------------------------------------------

    def binds(self, model: FakeMountRunner) -> set[str]:
        """The targets whose top mount is the user's bind from its source."""
        tops = {str(m["target"]): str(m["fsroot"]) for m in model.mounts}
        return {b.target for b in BINDINGS if tops.get(b.target) == f"/{b.kind}/{USER.name}"}

    def mounts(self, model: FakeMountRunner) -> list[tuple[str, str]]:
        return sorted((str(m["target"]), str(m["source"])) for m in model.mounts)

    def target_fstab(self) -> str:
        cur = self.start.fstab or ""
        if self.start.action == ATTACH:
            return plan_attach_fstab(cur, BINDINGS)
        return plan_detach_fstab(cur, USER.name, {CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD})

    def assert_target(self, model: FakeMountRunner, when: str) -> None:
        want = set(BOTH) if self.start.action == ATTACH else set()
        self.assertEqual(self.binds(model), want, f"{when}: the binds")
        self.assertEqual(read(self.fstab), self.target_fstab(), f"{when}: /etc/fstab")

    def assert_no_line_without_its_bind(self, model: FakeMountRunner, when: str) -> None:
        managed = parse_managed_fstab_entries(read(self.fstab) or "")
        lines = {e.target for e in managed if e.user == USER.name}
        self.assertLessEqual(lines, self.binds(model), f"{when}: a line in /etc/fstab for a bind not mounted")

    # ---- the checks ---------------------------------------------------------

    def check(self, *faults: Fault) -> None:
        when = f"{faults}"
        model = self.fresh()
        start_mounts, start_fstab = self.mounts(model), read(self.fstab)
        result = self.run_once(model, *faults)
        incomplete = result is not None and result[0] != 0 and bool(result[1]) and INCOMPLETE in result[1][-1]
        if not incomplete or self.start.action == DETACH:
            self.assert_no_line_without_its_bind(model, when)
        if result is not None:
            rc, lines = result
            if rc == 0:
                self.assert_target(model, f"{when}: exit 0")
            elif not incomplete:
                self.assertEqual(self.mounts(model), start_mounts, f"{when}: exit {rc}: the mounts: {lines}")
                self.assertEqual(read(self.fstab), start_fstab, f"{when}: exit {rc}: /etc/fstab: {lines}")
        again = self.run_once(model)
        self.assertEqual((again or (None, []))[0], 0, f"{when}: the run again: {again}")
        self.assert_target(model, f"{when}: the run again")
        model.calls.clear()
        third = self.run_once(model) or (None, [])
        self.assertEqual(third[0], 0, f"{when}: the third run: {third}")
        self.assertIn("nothing to do", "\n".join(third[1]), f"{when}: the third run")
        changing = [c for c in model.calls if not reads(c)]
        self.assertEqual(changing, [], f"{when}: the third run changed something")

    def points(self, *faults: Fault) -> list[str]:
        """The mutating calls of one run from the start state with these faults."""
        model = self.fresh()
        if not faults:
            calls = cut_points(self.plan, functools.partial(self.run_once, model))
            self.assert_target(model, "no fault")
            return calls
        self.run_once(model, *faults)
        return list(self.plan.calls)

    def every(self, mode: FaultMode) -> None:
        calls = self.points()
        self.assertEqual(len(calls), self.start.points, calls)
        for fault in every_fault(len(calls), (mode,)):
            with self.subTest(fault=fault, call=calls[fault[1] - 1]):
                self.check(fault)

    def every_failure_then(self, mode: FaultMode) -> None:
        """Every FAIL, then a second fault of MODE at each later point of that run (a kill or a
        failure while it rolls back)."""
        cases = 0
        for fail in every_fault(self.start.points, (FaultMode.FAIL,)):
            for then in every_fault(len(self.points(fail)), (mode,), after=fail[1]):
                with self.subTest(fault=fail, then=then):
                    cases += 1
                    self.check(fail, then)
        self.assertEqual(cases, self.start.fail_then_later)


class AttachFreshFaultTest(CacheFaultBase):
    """attach: no bind, /etc/fstab with the base line only."""

    start = ATTACH_FRESH

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


class AttachNoFstabFaultTest(CacheFaultBase):
    """attach: no bind and no /etc/fstab (the put-back removes the file)."""

    start = ATTACH_NO_FSTAB

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


class AttachHalfDoneFaultTest(CacheFaultBase):
    """attach again after one killed after its first mount (the cache bind mounted, no line)."""

    start = ATTACH_HALF_DONE

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


class DetachFullFaultTest(CacheFaultBase):
    """detach: both binds mounted, both lines."""

    start = DETACH_FULL

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


class DetachHalfDoneFaultTest(CacheFaultBase):
    """detach again after one killed after its first umount (no line, the scratchpad still mounted)."""

    start = DETACH_HALF_DONE

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

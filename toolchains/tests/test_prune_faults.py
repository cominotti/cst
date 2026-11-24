# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on `cst toolchains prune apply rust:build` (tests/fault_injection.py).

The protocol under test: toolchains/application/prune.py only orders the targets; the rename away,
the rm and the clean-up of leftovers are CargoBuildTree.delete and .trim
(toolchains/adapters/build_tree.py) and RustBuild._remove_leftovers
(toolchains/application/rust_build.py).

The command runs in process (toolchains' cli.main, as test_prune_rust_build runs it), with a build
tree of its own (Parts.tree): CargoBuildTree with no /proc walk (open_by and cargo_runs find no
user) and its remove (rm -rf of a folder renamed away) a point of a FaultPlan, whose failure is the
port's answer (False, why). The renames, the rmdir of an emptied XX folder and the utime that puts
an incremental folder's parent back are points through FaultyOs over toolchains/adapters/build_tree.py's
os, and the run summary's write (write_atomic in toolchains/adapters/home_files.py) is one too, whose
failure the program says and goes on. os.fsync is stubbed.

Start states: a mixed tree (two idle folders to delete, one the last of its XX folder; one idle
folder whose incremental folders are trimmed; an active folder; a folder of another program under
a name like CST's trash), and a tree with a leftover of an interrupted run beside one folder to
delete (the clean-up itself under fault). Each run has its own "now" (CST_TOOLCHAINS_NOW, one second
apart), so the trash names of two runs differ as they would on a real machine. For each start, every
mode at every point, and every FAIL followed by a KILL at each later point of that run, checked:
- N1, no data lost, after the faulted run: a folder kept keeps its digest (names, sizes, contents,
  mtimes); a folder chosen for deletion, and each incremental folder of the trimmed one, is at its
  path with its digest, or whole under one of CST's trash names, or gone; the rest of the trimmed
  folder keeps its digest; nothing else in the root changes;
- N2, recovery: the next run with no fault exits 0, leaves none of CST's trash, and saves a run
  summary; the folders chosen are then gone and the kept ones keep their digests;
- N3, a second fault: for each case that left trash, a fault at each point of the recovery run,
  then a clean third run, which meets N2;
- N4: a run that returns exits 0 only when every folder chosen is gone and none of CST's trash is
  left; a failed rename or rm gives exit 1 and a "could not delete" line; a failed summary write
  is said, and the run still exits 0; no exception gets out.

Not checked: the trimmed folder's parents' mtimes after a kill between an incremental folder's
rename and the utime that puts its parent back. The folder then looks active, which delays its
next trim or deletion; no data is lost. rm itself: the remove here deletes in process (shutil.rmtree,
one call, never torn), as the real rm -rf would; test_prune_rust_build covers the real rm and one
stopped at the time budget.

One class per start state and mode family (a work unit of tests/run_unit.py), the case counts pinned.
"""

from __future__ import annotations

import ast
from collections.abc import Generator
import contextlib
from dataclasses import dataclass
import functools
import hashlib
import io
import os
import shutil
import stat
import tempfile
import time
from typing import ClassVar, override
import unittest
from unittest import mock

from cst_core.adapters import SystemClockAdapter
from toolchains import cli as tc_cli, wiring as tc_wiring
from toolchains.adapters import build_tree as tc_build_tree
from toolchains.adapters import home_files as tc_home_files
from toolchains.adapters import program as tc_program
from toolchains.domain import build as tcd_build
from toolchains.domain import prune as tcd_prune
from toolchains.domain.build import CargoCache
from toolchains.ports import Users
from tests.fault_injection import (
    Fault,
    FaultMode,
    FaultPlan,
    FaultyOs,
    Killed,
    cut_points,
    every_fault,
    stub_fsync,
)
from tests.repo_files import REPO

TOOL = os.path.join(REPO, "toolchains", "cst-toolchains")
with open(TOOL, encoding="utf-8") as _fh:
    HELP = ast.get_docstring(ast.parse(_fh.read()), clean=False) or ""
HOUR = 3600
SETTINGS = "RUST_BUILD_ORPHANS=no\nRUST_BUILD_INCREMENTAL_IDLE=12h\nRUST_BUILD_MAX_TOTAL=off\n"
FOREIGN = tcd_build.TRASH_PREFIX + "mine"  # another program's folder, not of CST's trash format
INCS = ("debug/incremental", "x86_64-unknown-linux-gnu/debug/incremental")

type Digest = dict[str, tuple[object, ...]]


def digest(top: str, *, skip: tuple[str, ...] = (), dir_times: bool = True) -> Digest:
    """Every entry below top (relative path -> kind, size or content hash, mtime), not entering skip."""
    out: Digest = {}
    for d, dirs, files in os.walk(top):
        rel_d = os.path.relpath(d, top)
        dirs[:] = sorted(x for x in dirs if os.path.normpath(os.path.join(rel_d, x)) not in skip)
        for name in sorted(dirs + files):
            p = os.path.join(d, name)
            rel = os.path.normpath(os.path.join(rel_d, name))
            st = os.lstat(p)
            if stat.S_ISDIR(st.st_mode):
                out[rel] = ("dir", st.st_mtime_ns if dir_times else 0)
            else:
                with open(p, "rb") as fh:
                    out[rel] = ("file", hashlib.sha256(fh.read()).hexdigest(), st.st_mtime_ns)
    return out


@dataclass(frozen=True)
class Start:
    """A start state: the folders deleted, the one trimmed (or ""), the kept ones, a leftover, and the
    pinned numbers of points and of FAIL then KILL cases."""

    delete: tuple[str, ...]
    trim: str
    keep: tuple[str, ...]
    leftover: bool
    points: int
    fail_then_kill: int
    kill_then_fault: int  # the cases of N3: a kill that left trash, then a fault in the recovery run


PRUNE_MIXED = Start(
    delete=("aa/0000000000000a", "bb/0000000000000b"),
    trim="cc/0000000000000d",
    keep=("bb/0000000000000c",),
    leftover=False,
    points=13,
    fail_then_kill=67,
    kill_then_fault=135,
)
PRUNE_LEFTOVER = Start(
    delete=("dd/0000000000000e",), trim="", keep=(), leftover=True, points=5, fail_then_kill=8, kill_then_fault=12
)
LEFTOVER = tcd_build.TRASH_PREFIX + "ee0000000000000f-1700000000"


def rm_tree(path: str) -> tuple[bool, str]:
    """rm -rf in process: the port's (ok, message); a path already gone is ok, as rm -f says."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        return False, str(e.strerror)
    return True, ""


class FaultyTree(tc_build_tree.CargoBuildTree):
    """The real build tree with no /proc walk; its remove, in process (no rm fork per case), a point
    of plan whose failure is the port's (False, why)."""

    plan: FaultPlan

    @override
    def remove(self, path: str, timeout: float | None) -> tuple[bool, str]:
        def failed(err: OSError) -> tuple[bool, str]:
            return False, str(err.strerror)

        return self.plan.step(f"rm {os.path.basename(path)}", functools.partial(rm_tree, path), failed)

    @override
    def open_by(self, root: str) -> tuple[Users, int]:
        return {}, 0

    @override
    def cargo_runs(self, root: str, cache: CargoCache) -> tuple[Users, list[tuple[str, str, str | None, str]]]:
        return {}, []


class PruneFaultBase(unittest.TestCase):
    """The fixture and the checks; each subclass names its start state and its mode."""

    start: ClassVar[Start]

    @override
    def setUp(self) -> None:
        stub_fsync(self)
        self.enterContext(mock.patch.object(tc_program, "restore_default_signals", return_value=None))
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-prune-faults-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.home, ".cache", "cargo-build")
        self.now = float(int(time.time()))
        self.runs = 0
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "CST_TOOLCHAINS_HERE": "1",
                    "CST_TOOLCHAINS_SYSTEMCTL": "/nonexistent/systemctl",
                    "XDG_RUNTIME_DIR": os.path.join(self.tmp, "no-runtime-dir"),
                },
            )
        )
        if "CARGO_HOME" in os.environ:
            self.enterContext(mock.patch.dict(os.environ))
            del os.environ["CARGO_HOME"]
        self.plan = FaultPlan()

    # ---- the fixture ------------------------------------------------------

    def put(self, path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)

    def folder(self, key: str, age_h: float, *, incs: tuple[str, ...] = ()) -> None:
        """A cargo-like build folder, every mtime AGE_H hours before now, with these incremental folders."""
        path = os.path.join(self.root, key)
        files = ["CACHEDIR.TAG", "debug/.cargo-build-lock", "debug/deps/libfoo-0123.rlib", "debug/.fingerprint/f"]
        files += [f"{inc}/foo-1/s-abc/query-cache.bin" for inc in incs]
        for i, rel in enumerate(files):
            self.put(os.path.join(path, rel), f"{key} {rel} {i}".encode() * 64)
        self.age(path, age_h)

    def age(self, path: str, age_h: float) -> None:
        t = self.now - age_h * HOUR
        for d, dirs, files in os.walk(path):
            for n in files + dirs:
                os.utime(os.path.join(d, n), (t, t), follow_symlinks=False)
            os.utime(d, (t, t))

    def fresh(self) -> None:
        """The start state on disk, and the digests the checks compare with."""
        s = self.start
        shutil.rmtree(self.home, ignore_errors=True)
        os.makedirs(self.root)
        cfg = os.path.join(self.home, ".cargo", "config.toml")
        self.put(cfg, f'[build]\nbuild-dir = "{self.root}/{{workspace-path-hash}}"\n'.encode())
        self.put(os.path.join(self.home, ".config", "cst", "toolchains.conf"), SETTINGS.encode())
        for key in s.delete:
            self.folder(key, 100)
        for key in s.keep:
            self.folder(key, 1)
        if s.trim:
            self.folder(s.trim, 24, incs=INCS)
        os.makedirs(os.path.join(self.root, FOREIGN, "data"))
        self.put(os.path.join(self.root, FOREIGN, "data", "f"), b"not CST's")
        if s.leftover:
            self.put(os.path.join(self.root, LEFTOVER, "debug", "deps", "x.rlib"), b"left" * 256)
        self.runs = 0
        self.before = {key: digest(os.path.join(self.root, key)) for key in (*s.delete, *s.keep, FOREIGN)}
        if s.trim:
            path = os.path.join(self.root, s.trim)
            self.trim_rest = self.rest_of_trim()
            self.inc_before = {inc: digest(os.path.join(path, inc)) for inc in INCS}

    def rest_of_trim(self) -> Digest:
        """The trimmed folder without its incremental folders (and their parents' mtimes)."""
        path = os.path.join(self.root, self.start.trim)
        rest = digest(path, skip=INCS)
        for inc in INCS:
            parent = os.path.dirname(inc)
            rest[parent] = ("dir", 0)
        return rest

    @contextlib.contextmanager
    def faulty(self) -> Generator[None]:
        plan = self.plan
        real_write = tc_home_files.write_atomic

        def write_atomic(path: str, data: str | bytes, **kw: object) -> None:
            plan.step(f"write_atomic {os.path.basename(path)}", functools.partial(real_write, path, data, **kw))

        with (
            mock.patch.object(tc_build_tree, "os", FaultyOs(plan, ("rename", "rmdir", "utime"))),
            mock.patch.object(tc_home_files, "write_atomic", write_atomic),
        ):
            yield

    def run_once(self, *faults: Fault) -> tuple[int, str] | None:
        """One `prune apply rust:build` with these faults; None when it was killed."""
        self.plan.arm(*faults)
        self.runs += 1
        limits = tcd_prune.Limits()
        tree = FaultyTree(self.home, "cst toolchains", limits, SystemClockAdapter(), procs=list)
        tree.plan = self.plan
        parts = tc_wiring.Parts(limits=limits, procs=list, tree=tree, journal=False)
        out = io.StringIO()
        argv = ["prune", "apply", "rust:build", "--home", self.home]
        with (
            mock.patch.dict(os.environ, {"CST_TOOLCHAINS_NOW": str(self.now + self.runs)}),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(out),
            self.faulty(),
        ):
            try:
                rc = tc_cli.main(argv, HELP, TOOL, parts=parts)
            except Killed:
                return None
        return rc, out.getvalue()

    # ---- what the state is --------------------------------------------------

    def trash(self) -> list[str]:
        return sorted(n for n in os.listdir(self.root) if tcd_build.TRASH_NAME.match(n))

    def chosen(self) -> dict[str, Digest]:
        """Each piece of data the run may delete (a folder, an incremental folder, the leftover): its digest."""
        s = self.start
        out = {key: self.before[key] for key in s.delete}
        if s.trim:
            out |= {f"{s.trim}/{inc}": d for inc, d in self.inc_before.items()}
        return out

    def assert_no_data_lost(self, when: str) -> None:
        s = self.start
        for key in (*s.keep, FOREIGN):
            self.assertEqual(digest(os.path.join(self.root, key)), self.before[key], f"{when}: {key} kept")
        trash = {n: digest(os.path.join(self.root, n)) for n in self.trash() if n != LEFTOVER}
        for rel, want in self.chosen().items():
            path = os.path.join(self.root, rel)
            if os.path.lexists(path):
                self.assertEqual(digest(path), want, f"{when}: {rel} changed in place")
        unknown = [n for n, d in trash.items() if d not in self.chosen().values()]
        self.assertEqual(unknown, [], f"{when}: trash that holds none of the chosen folders whole")
        if s.trim:
            self.assertEqual(self.rest_of_trim(), self.trim_rest, f"{when}: the rest of {s.trim}")
        expected_top = {k.split("/")[0] for k in (*s.delete, *s.keep, s.trim) if k} | {FOREIGN}
        extra = set(os.listdir(self.root)) - expected_top - set(self.trash())
        self.assertEqual(extra, set(), f"{when}: something new in the root")

    def assert_target(self, when: str) -> None:
        """N2: no trash, the chosen folders gone (an incremental folder may stay: see the docstring)."""
        s = self.start
        self.assertEqual(self.trash(), [], f"{when}: trash left")
        for key in s.delete:
            self.assertFalse(os.path.lexists(os.path.join(self.root, key)), f"{when}: {key} not deleted")
        self.assert_no_data_lost(when)
        state = os.path.join(self.home, ".local", "state", "cst")
        self.assertTrue(os.listdir(state) if os.path.isdir(state) else [], f"{when}: no run summary saved")

    def all_done(self) -> bool:
        gone = [not os.path.lexists(os.path.join(self.root, rel)) for rel in self.chosen()]
        return all(gone) and not self.trash()

    # ---- the checks ---------------------------------------------------------

    def check(self, *faults: Fault, recovery: tuple[Fault, ...] = ()) -> None:
        when = f"{faults}"
        self.fresh()
        result = self.run_once(*faults)
        self.assert_no_data_lost(when)
        if result is not None:
            rc, out = result
            if rc == 0:
                self.assertTrue(self.all_done(), f"{when}: exit 0 with work left: {out}")
            else:
                self.assertIn("could not delete", out, f"{when}: exit {rc}")
        if recovery:
            self.run_once(*recovery)
            when = f"{when} then {recovery}"
            self.assert_no_data_lost(when)
        again = self.run_once()
        self.assertEqual((again or (None, ""))[0], 0, f"{when}: the run again: {again}")
        self.assert_target(f"{when}: the run again")

    def points(self, *faults: Fault) -> list[str]:
        self.fresh()
        if not faults:
            calls = cut_points(self.plan, self.run_once)
            self.assert_target("no fault")
            return calls
        self.run_once(*faults)
        return list(self.plan.calls)

    def every(self, mode: FaultMode) -> None:
        calls = self.points()
        self.assertEqual(len(calls), self.start.points, calls)
        for fault in every_fault(len(calls), (mode,)):
            with self.subTest(fault=fault, call=calls[fault[1] - 1]):
                self.check(fault)

    def every_failure_then_a_kill(self) -> None:
        cases = 0
        for fail in every_fault(self.start.points, (FaultMode.FAIL,)):
            for kill in every_fault(len(self.points(fail)), (FaultMode.KILL,), after=fail[1]):
                with self.subTest(fault=fail, then=kill):
                    cases += 1
                    self.check(fail, kill)
        self.assertEqual(cases, self.start.fail_then_kill)

    def every_kill_then_a_fault_in_the_recovery(self) -> None:
        """N3: each kill that left trash, then each mode at each point of the recovery run."""
        cases = 0
        for kill in every_fault(self.start.points, (FaultMode.KILL,)):
            self.fresh()
            self.run_once(kill)
            if not self.trash():
                continue
            self.run_once()
            recovery_points = len(self.plan.calls)
            for fault in every_fault(recovery_points):
                with self.subTest(kill=kill, recovery=fault):
                    cases += 1
                    self.check(kill, recovery=(fault,))
        self.assertEqual(cases, self.start.kill_then_fault)


class PruneMixedKillFaultTest(PruneFaultBase):
    """two folders to delete, one to trim, one kept, another program's folder."""

    start = PRUNE_MIXED

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)


class PruneMixedRecoveryFaultTest(PruneFaultBase):
    """two folders to delete, one to trim, one kept, another program's folder. A kill, then a fault in the recovery run."""

    start = PRUNE_MIXED

    def test_every_kill_then_a_fault_in_the_recovery(self) -> None:
        self.every_kill_then_a_fault_in_the_recovery()


class PruneMixedFailFaultTest(PruneFaultBase):
    """two folders to delete, one to trim, one kept, another program's folder."""

    start = PRUNE_MIXED

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class PruneLeftoverKillFaultTest(PruneFaultBase):
    """a leftover of an interrupted run, and one folder to delete."""

    start = PRUNE_LEFTOVER

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)


class PruneLeftoverRecoveryFaultTest(PruneFaultBase):
    """a leftover of an interrupted run, and one folder to delete. A kill, then a fault in the recovery run."""

    start = PRUNE_LEFTOVER

    def test_every_kill_then_a_fault_in_the_recovery(self) -> None:
        self.every_kill_then_a_fault_in_the_recovery()


class PruneLeftoverFailFaultTest(PruneFaultBase):
    """a leftover of an interrupted run, and one folder to delete."""

    start = PRUNE_LEFTOVER

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()

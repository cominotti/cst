# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on `cst btrfs cache attach` and `detach` (tests/fault_injection.py), over CacheWorld.

attach and detach (btrfs/cache/application.py) run here over CacheWorld (btrfs/tests/fakes.py), the
in-memory fakes of their ports, through CacheMounts.run: argv parsed as the command line parses it, the
returned lines read instead of stdout and stderr (every error path returns its ERROR line last). Every
mutating call is a point of a FaultPlan: each private folder made, the CACHEDIR.TAG written, each mount
--bind and umount, the /etc/fstab write and put-back (or its removal when there was none) and each
systemctl daemon-reload (not findmnt, systemctl show or the reads of the probe). The world keeps the
mount table and what systemd has loaded of /etc/fstab. btrfs/tests/test_cache_ports.py keeps the fakes
equal to the real adapters; the command line's own part (help, refusals, sudo, ERROR lines to stderr)
is test_cache.py's.

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
  third run says "nothing to do" and runs no mount, umount or reload;
- I5: exit 0 means systemd has loaded /etc/fstab as it is on disk (CacheWorld's view of it).
  A run that left systemd behind (killed between its /etc/fstab write and its daemon-reload, or a
  reload that failed) is followed by a run again that runs exactly one daemon-reload; any other run
  again runs at most one.

One class per start state (a work unit of tests/run_unit.py), one method per mode family, the case
counts pinned. ProtocolFaultTest stages single faults that once needed a patch of the adapters' names.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import os
from typing import ClassVar, override
import unittest

from btrfs.cache.domain import (
    CACHE_BASE,
    SCRATCHPAD_BASE,
    CacheMountKind,
    CacheMountUser,
    build_user_bindings,
    parse_cache_mount_args,
    parse_managed_fstab_entries,
    plan_attach_fstab,
    plan_detach_fstab,
)
from btrfs.tests.fakes import CacheWorld
from tests.fault_injection import Fault, FaultMode, Killed, cut_points, every_fault

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


def cache_world(fstab: str | None, mounted: tuple[str, ...] = ()) -> CacheWorld:
    """The machine the matrix starts from: danilo's account, the two bases mounted, both sources made,
    /etc/fstab as given and the binds of mounted in place."""
    w = CacheWorld()
    w.add_user(USER.name, USER.uid, USER.home)
    w.caller = USER.name
    for b in BINDINGS:
        w.mkdirs(b.source)
    w.put_fstab(fstab)
    w.table.add_mount("/dev/mapper/cache", CACHE_BASE, fsroot="/cache")
    w.table.add_mount("/dev/mapper/cache", SCRATCHPAD_BASE, fsroot="/scratchpad")
    for b in BINDINGS:
        if b.target in mounted:
            w.mount_bind(b.source, b.target)
    return w


def run_cache(world: CacheWorld, argv: list[str]) -> tuple[int, list[str]]:
    """One command over the world's ports: (exit, its lines)."""
    return world.service().run(parse_cache_mount_args(argv), "cst btrfs cache")


def binds(world: CacheWorld) -> set[str]:
    """The targets whose top mount is the user's bind from its source."""
    tops = {str(m["target"]): str(m["fsroot"]) for m in world.table.mounts}
    return {b.target for b in BINDINGS if tops.get(b.target) == f"/{b.kind}/{USER.name}"}


def mounts(world: CacheWorld) -> list[tuple[str, str]]:
    return sorted((str(m["target"]), str(m["source"])) for m in world.table.mounts)


class CacheFaultBase(unittest.TestCase):
    """The fixture and the checks; each subclass names its start state."""

    start: ClassVar[Start]

    @override
    def setUp(self) -> None:
        self.world = self.fresh()

    # ---- the fixture ------------------------------------------------------

    def fresh(self) -> CacheWorld:
        """The start state."""
        self.world = cache_world(self.start.fstab, self.start.mounted)
        return self.world

    def run_once(self, *faults: Fault) -> tuple[int, list[str]] | None:
        """One run of the start's command with these faults: (exit, its lines); None when killed."""
        self.world.plan.arm(*faults)
        try:
            return run_cache(self.world, [self.start.action])
        except Killed:
            return None

    # ---- what the state is --------------------------------------------------

    def target_fstab(self) -> str:
        cur = self.start.fstab or ""
        if self.start.action == ATTACH:
            return plan_attach_fstab(cur, BINDINGS)
        return plan_detach_fstab(cur, USER.name, {CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD})

    def assert_target(self, when: str) -> None:
        want = set(BOTH) if self.start.action == ATTACH else set()
        self.assertEqual(binds(self.world), want, f"{when}: the binds")
        self.assertEqual(self.world.fstab, self.target_fstab(), f"{when}: /etc/fstab")

    def assert_no_line_without_its_bind(self, when: str) -> None:
        managed = parse_managed_fstab_entries(self.world.fstab or "")
        lines = {e.target for e in managed if e.user == USER.name}
        self.assertLessEqual(lines, binds(self.world), f"{when}: a line in /etc/fstab for a bind not mounted")

    # ---- the checks ---------------------------------------------------------

    def check(self, *faults: Fault) -> None:
        when = f"{faults}"
        w = self.fresh()
        start_mounts, start_fstab = mounts(w), w.fstab
        result = self.run_once(*faults)
        incomplete = result is not None and result[0] != 0 and bool(result[1]) and INCOMPLETE in result[1][-1]
        if not incomplete or self.start.action == DETACH:
            self.assert_no_line_without_its_bind(when)
        if result is not None:
            rc, lines = result
            if rc == 0:
                self.assert_target(f"{when}: exit 0")
                self.assertTrue(w.view_current(), f"{when}: exit 0, systemd has not loaded /etc/fstab: {lines}")
            elif not incomplete:
                self.assertEqual(mounts(w), start_mounts, f"{when}: exit {rc}: the mounts: {lines}")
                self.assertEqual(w.fstab, start_fstab, f"{when}: exit {rc}: /etc/fstab: {lines}")
        behind, reloads = not w.view_current(), w.reloads
        again = self.run_once()
        self.assertEqual((again or (None, []))[0], 0, f"{when}: the run again: {again}")
        self.assert_target(f"{when}: the run again")
        self.assertTrue(w.view_current(), f"{when}: the run again, systemd has not loaded /etc/fstab: {again}")
        ran = w.reloads - reloads
        if behind:
            self.assertEqual(ran, 1, f"{when}: the run again after systemd was left behind")
        self.assertLessEqual(ran, 1, f"{when}: the run again reloaded {ran} times")
        w.calls.clear()
        third = self.run_once() or (None, [])
        self.assertEqual(third[0], 0, f"{when}: the third run: {third}")
        self.assertIn("nothing to do", "\n".join(third[1]), f"{when}: the third run")
        self.assertEqual(w.commands(), [], f"{when}: the third run changed something")

    def points(self, *faults: Fault) -> list[str]:
        """The mutating calls of one run from the start state with these faults."""
        self.fresh()
        if not faults:
            calls = cut_points(self.world.plan, self.run_once)
            self.assert_target("no fault")
            return calls
        self.run_once(*faults)
        return list(self.world.plan.calls)

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


def _no_space() -> OSError:
    return OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


BUSY = OSError(errno.EBUSY, "umount: target is busy")  # what FakeMountRunner's busy umount answers
DENIED = OSError(errno.EPERM, "mount: permission denied")
RELOAD_FAILED = OSError(errno.EIO, "Failed to reload daemon")


class ProtocolFaultTest(unittest.TestCase):
    """Single faults staged in the world (fail() or the plan) that test_cache.py once staged by patching
    the adapters' names; the assertions as they were."""

    @override
    def setUp(self) -> None:
        self.world = cache_world(BASE_FSTAB)

    def _run(self, argv: list[str]) -> tuple[int, list[str]]:
        return run_cache(self.world, argv)

    def _mounted(self) -> list[str]:
        return [str(m["target"]) for m in self.world.table.mounts if str(m["target"]).startswith("/var/home/")]

    def test_a_link_put_in_place_of_a_folder_after_the_probe_fails_cleanly(self) -> None:
        # The private folder's O_NOFOLLOW open refuses a link put there after the probe (ENOTDIR: the
        # adapter's half, that the link's target keeps its mode, is in test_cache_ports.py).
        before = self.world.fstab
        self.world.fail("folder /var/mnt/cache/danilo", OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR)))
        rc, lines = self._run(["attach"])
        self.assertEqual(rc, 1, lines)
        self.assertIn("ERROR: cannot prepare /var/mnt/cache/danilo and /var/home/danilo/.cache:", lines[-1])
        self.assertEqual(self.world.commands(), [])
        self.assertEqual(self.world.fstab, before)

    def test_a_folder_that_cannot_be_made_is_an_error_not_a_traceback(self) -> None:
        self.world.fail("folder /var/mnt/cache/danilo", PermissionError(13, "Permission denied"))
        rc, lines = self._run(["attach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: cannot prepare /var/mnt/cache/danilo and /var/home/danilo/.cache: Permission denied", lines
        )
        self.assertFalse(any(c.startswith("mount ") for c in self.world.calls))

    def test_a_bind_and_an_fstab_not_undone_are_both_named(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self.world.fail("umount /var/home/danilo/Scratchpad", BUSY)  # after .cache's umount
        self.world.fail("mount --bind /var/mnt/cache/danilo /var/home/danilo/.cache", DENIED)  # mounting it again
        self.world.fail_at("fstab write", 2, _no_space())  # the put-back, detach's second write there
        rc, lines = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: umount /var/home/danilo/Scratchpad failed: umount: target is busy; ROLLBACK INCOMPLETE "
            "(mount --bind /var/mnt/cache/danilo /var/home/danilo/.cache failed: mount: permission denied; "
            "cannot put /etc/fstab back: No space left on device)",
            lines,
        )

    def test_a_fstab_that_cannot_be_put_back_after_a_busy_umount_is_rollback_incomplete(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        self.world.fail("umount /var/home/danilo/.cache", BUSY)
        self.world.fail_at("fstab write", 2, _no_space())  # the put-back, detach's second write there
        rc, lines = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: umount /var/home/danilo/.cache failed: umount: target is busy; "
            "ROLLBACK INCOMPLETE (cannot put /etc/fstab back: No space left on device)",
            lines,
        )
        self.assertFalse(any("restored." in line for line in lines))
        self.assertEqual(sorted(self._mounted()), ["/var/home/danilo/.cache", "/var/home/danilo/Scratchpad"])

    def test_a_failed_reload_after_putting_the_lines_back_is_rollback_incomplete(self) -> None:
        self.assertEqual(self._run(["attach"])[0], 0)
        attached = self.world.fstab
        self.world.fail("umount /var/home/danilo/.cache", BUSY)
        self.world.fail_at("systemctl daemon-reload", 2, RELOAD_FAILED)  # the one after the put-back
        rc, lines = self._run(["detach"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "ERROR: umount /var/home/danilo/.cache failed: umount: target is busy; "
            "ROLLBACK INCOMPLETE (systemctl daemon-reload failed: Failed to reload daemon)",
            lines,
        )
        self.assertEqual(self.world.fstab, attached)

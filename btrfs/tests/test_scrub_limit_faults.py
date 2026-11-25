# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on `cst btrfs scrub-limit set|off` (tests/fault_injection.py).

ScrubLimit.apply (btrfs/scrub_limit/application.py) runs over the in-memory fakes of its ports
(ScrubWorld, btrfs/tests/fakes.py): snapshots.conf and its folder, the drop-in written and removed
through the real InstallService and an in-memory install record (as installer.py text --keep-state
and remove-text do), and what systemd has loaded of it: the drop-in's text at the last daemon-reload
that worked, and whether a write or a removal of the drop-in came since (systemd's NeedDaemonReload,
which follows the files' times and the list of drop-ins, not their text). Each mutating call of a
port is a point of the FaultPlan: "settings folder", "settings replace" and "settings sync" (the
three parts of a write of snapshots.conf: a failure at the sync leaves the new text in place),
"settings remove", "settings rmdir", "drop-in text", "drop-in remove-text", "drop-in rmdir" and
"systemctl daemon-reload" (systemctl's NeedDaemonReload only reads). What the real adapters run for
each port is pinned in btrfs/tests/test_scrub_limit_ports.py and btrfs/tests/test_scrub_limit.py.

Start states: four with systemd in step, and two where it is behind (an earlier run stopped after
its installer, before its daemon-reload), so the repair run itself is under fault; each of the six
with no install record and with btrfs:core done in it, and set 1G over 500M with btrfs:core pending.
For each, every
mode at every point, and every FAIL followed by a KILL at each later point of that run (a run that
dies while it rolls back), checked:
- no OSError gets out (it would be a traceback);
- a run that returns 0 left the target: snapshots.conf and the drop-in as asked, and systemd has
  loaded that drop-in;
- a run that returns non-zero left both files and systemd's view as they were, or its last line
  says ROLLBACK INCOMPLETE; it says systemd has not loaded the files only from a start where
  systemd was behind;
- converges: the same command again, with no fault, returns 0 and leaves the target, loaded;
- btrfs:core's state in the install record: no run, whatever its end (exit 0, killed, failed, rolled
  back), and not the run again, changes the state btrfs:core had before the first run. A start with
  no state for it (no record: btrfs:core never installed) may end with btrfs:core pending, never
  done, and never with a state no recorded file names.

The start states with btrfs:core in the record hold a second file of it besides the drop-in (a seed
text, as an install leaves btrfs:core): off's remove-text then leaves btrfs:core a file, so its state.

One class per start state (a work unit of tests/run_unit.py), one method per mode family, the
case counts pinned. ScrubLimitWriteFailureTest: the failed writes of snapshots.conf one by one, with
their lines.
"""

from __future__ import annotations

import errno
import functools
import os
import unittest
from dataclasses import dataclass, replace
from typing import ClassVar, override

from btrfs.scrub_limit.domain import render_scrub_dropin, update_snapshots_conf_scrub_limit
from btrfs.tests.fakes import FakeScrubDropin, FakeScrubSystemd, ScrubWorld
from cst_core.domain.record import ComponentState
from cst_core.domain.values import ExitAndLines
from tests.fault_injection import Fault, FaultMode, FaultPlan, Killed, cut_points, every_fault, no_space

DROPIN_500M = render_scrub_dropin("500M")
NOT_LOADED = "systemd has not loaded them"
CONF = "/etc/btrfs-setup/snapshots.conf"


def as_asked(conf: str | None, limit: str | None) -> str | None:
    """snapshots.conf as set LIMIT (off: None) leaves it."""
    new = update_snapshots_conf_scrub_limit(conf, limit)
    return conf if new is None else new


@dataclass(frozen=True)
class Start:
    """A state before the run: the two files, the limit asked (None: off), what systemd has loaded
    (behind: not what is on disk), btrfs:core's state in the install record (None: no record), and the
    pinned numbers of points and of FAIL then KILL cases."""

    conf: str | None
    dropin: str | None
    limit: str | None
    points: int
    fail_then_kill: int
    loaded: str | None = None
    behind: bool = False
    core: ComponentState | None = None


SET_BOTH_NEW = Start(None, None, "500M", points=5, fail_then_kill=10)
SET_KEEPS_A_KEY = Start("NAS_ENABLED=1\n", None, "500M", points=5, fail_then_kill=11)
SET_500M_TO_1G = Start("SCRUB_LIMIT='500M'\n", DROPIN_500M, "1G", points=5, fail_then_kill=10, loaded=DROPIN_500M)
OFF = Start("SCRUB_LIMIT='500M'\n", DROPIN_500M, None, points=6, fail_then_kill=11, loaded=DROPIN_500M)
# A first-ever set 500M killed after its installer: the files as asked, systemd loaded no drop-in.
SET_BEHIND = Start(as_asked(None, "500M"), DROPIN_500M, "500M", points=2, fail_then_kill=0, behind=True)
# An off killed after its installer: no drop-in, no SCRUB_LIMIT, systemd still has the 500M drop-in.
OFF_BEHIND = Start(
    as_asked("SCRUB_LIMIT='500M'\n", None), None, None, points=3, fail_then_kill=1, loaded=DROPIN_500M, behind=True
)


class ScrubFaultBase(unittest.TestCase):
    """The fixture and the checks; each subclass names its start state."""

    start: ClassVar[Start]

    @override
    def setUp(self) -> None:
        self.world = ScrubWorld()

    def fresh(self) -> ScrubWorld:
        """The start state, and systemd's view of it; one FaultPlan for every run, as one fixture had."""
        s = self.start
        plan = self.world.plan
        world = ScrubWorld()
        world.plan = plan
        world.put_conf(s.conf)
        world.put_dropin(s.dropin)
        world.loaded, world.dirty = s.loaded, s.behind
        if s.core is not None:
            world.install_core(done=s.core == ComponentState.DONE)
        self.world = world
        return world

    def assert_core(self, world: ScrubWorld, when: str) -> None:
        """btrfs:core's state is the start's (with no state at the start: none, or pending with a file)."""
        state = world.core_state()
        if self.start.core is not None:
            self.assertEqual(state, self.start.core, f"{when}: btrfs:core's state")
            return
        self.assertIn(state, (None, ComponentState.PENDING), f"{when}: btrfs:core's state")
        if state is not None:
            self.assertTrue(world.core_has_a_file(), f"{when}: {world.records.texts}")

    def run_once(self, world: ScrubWorld, *faults: Fault) -> ExitAndLines | None:
        """One run of the start's command with these faults; None when it was killed."""
        world.plan.arm(*faults)
        try:
            return world.service().apply(self.start.limit)
        except Killed:
            return None
        except OSError as e:
            self.fail(f"{faults}: an OSError got out (a traceback) at {world.plan.calls[-1]}: {e}")

    def assert_target(self, world: ScrubWorld, when: str) -> None:
        s = self.start
        self.assertEqual(world.conf, as_asked(s.conf, s.limit), f"{when}: snapshots.conf")
        self.assertEqual(world.dropin, None if s.limit is None else render_scrub_dropin(s.limit), f"{when}: drop-in")
        self.assertEqual(world.loaded, world.dropin, f"{when}: systemd did not load the drop-in")

    def check(self, *faults: Fault) -> None:
        s = self.start
        when = f"{faults}"
        world = self.fresh()
        result = self.run_once(world, *faults)
        self.assert_core(world, f"{when}: the run")
        if result is not None:
            rc, lines = result
            if rc == 0:
                self.assert_target(world, f"{when}: exit 0")
            elif "ROLLBACK INCOMPLETE" not in lines[-1]:
                self.assertEqual((world.conf, world.dropin), (s.conf, s.dropin), f"{when}: exit {rc}: {lines}")
                self.assertEqual(world.loaded, s.loaded, f"{when}: exit {rc}: systemd's view: {lines}")
                if NOT_LOADED in lines[-1]:
                    self.assertTrue(s.behind, f"{when}: exit {rc}: systemd's view was in step: {lines}")
        rc_again, lines_again = self.run_once(world) or (None, [])
        self.assertEqual(rc_again, 0, f"{when}: the run again: {lines_again}")
        self.assert_target(world, f"{when}: the run again")
        self.assert_core(world, f"{when}: the run again")

    def points(self, *faults: Fault) -> list[str]:
        """The mutating calls of one run from the start state with these faults."""
        world = self.fresh()
        if not faults:
            calls = cut_points(world.plan, functools.partial(self.run_once, world))
            self.assert_target(world, "no fault")
            self.assert_core(world, "no fault")
            return calls
        self.run_once(world, *faults)
        return list(world.plan.calls)

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


class SetBothNewFaultTest(ScrubFaultBase):
    """set 500M: no snapshots.conf, no drop-in."""

    start = SET_BOTH_NEW

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class SetKeepsAKeyFaultTest(ScrubFaultBase):
    """set 500M: snapshots.conf with another key, no drop-in."""

    start = SET_KEEPS_A_KEY

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class Set500MTo1GFaultTest(ScrubFaultBase):
    """set 1G over 500M, systemd in step."""

    start = SET_500M_TO_1G

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class OffFaultTest(ScrubFaultBase):
    """off from 500M, systemd in step."""

    start = OFF

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class SetBehindFaultTest(ScrubFaultBase):
    """set 500M again after a first-ever set killed after its installer (systemd loaded no drop-in)."""

    start = SET_BEHIND

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


class OffBehindFaultTest(ScrubFaultBase):
    """off again after an off killed after its installer (systemd still has the 500M drop-in)."""

    start = OFF_BEHIND

    def test_every_kill(self) -> None:
        self.every(FaultMode.KILL)

    def test_every_failure(self) -> None:
        self.every(FaultMode.FAIL)

    def test_every_full_disk(self) -> None:
        self.every(FaultMode.FAIL_FROM)

    def test_every_failure_then_a_kill_at_each_later_point(self) -> None:
        self.every_failure_then_a_kill()


# The same starts with btrfs:core in the install record: done for each, pending for one. The points
# and the FAIL then KILL counts are the same as without a record (scrub-limit never marks btrfs:core).
DONE = ComponentState.DONE


class SetBothNewCoreDoneFaultTest(SetBothNewFaultTest):
    """set 500M: no snapshots.conf, no drop-in; btrfs:core done."""

    start = replace(SET_BOTH_NEW, core=DONE)


class SetKeepsAKeyCoreDoneFaultTest(SetKeepsAKeyFaultTest):
    """set 500M: snapshots.conf with another key, no drop-in; btrfs:core done."""

    start = replace(SET_KEEPS_A_KEY, core=DONE)


class Set500MTo1GCoreDoneFaultTest(Set500MTo1GFaultTest):
    """set 1G over 500M, systemd in step; btrfs:core done."""

    start = replace(SET_500M_TO_1G, core=DONE)


class Set500MTo1GCorePendingFaultTest(Set500MTo1GFaultTest):
    """set 1G over 500M, systemd in step; btrfs:core pending (an install that did not finish)."""

    start = replace(SET_500M_TO_1G, core=ComponentState.PENDING)


class OffCoreDoneFaultTest(OffFaultTest):
    """off from 500M, systemd in step; btrfs:core done."""

    start = replace(OFF, core=DONE)


class SetBehindCoreDoneFaultTest(SetBehindFaultTest):
    """set 500M again after a first-ever set killed after its installer; btrfs:core done."""

    start = replace(SET_BEHIND, core=DONE)


class OffBehindCoreDoneFaultTest(OffBehindFaultTest):
    """off again after an off killed after its installer; btrfs:core done."""

    start = replace(OFF_BEHIND, core=DONE)


def _eio() -> OSError:
    return OSError(errno.EIO, os.strerror(errno.EIO))


class ScrubLimitWriteFailureTest(unittest.TestCase):
    """A write of snapshots.conf that fails, one way at a time, and the time limit of each reload."""

    def assert_no_drop_in_and_no_reload(self, world: ScrubWorld) -> None:
        self.assertIsNone(world.dropin)
        self.assertEqual([c for c in world.calls if not c.startswith("settings ")], [])  # no installer, no reload

    def test_a_full_disk_at_a_new_snapshots_conf_is_an_error_line_with_nothing_written(self) -> None:
        for broken in ("settings replace", "settings folder"):
            with self.subTest(broken=broken):
                world = ScrubWorld()
                world.fail(broken, no_space())
                rc, lines = world.service().apply("500M")
                self.assertEqual((rc, lines), (1, [f"ERROR: cannot write {CONF}: No space left on device"]))
                self.assertFalse(world.conf_folder)  # the folder this run made: gone
                self.assert_no_drop_in_and_no_reload(world)

    def test_a_full_disk_at_an_existing_snapshots_conf_leaves_it_as_it_was(self) -> None:
        world = ScrubWorld()
        world.put_conf("NAS_ENABLED=1\n")
        world.fail("settings replace", no_space())
        rc, lines = world.service().apply("500M")
        self.assertEqual((rc, lines), (1, [f"ERROR: cannot write {CONF}: No space left on device"]))
        self.assertEqual(world.conf, "NAS_ENABLED=1\n")
        self.assert_no_drop_in_and_no_reload(world)

    def test_a_write_that_fails_after_its_rename_puts_snapshots_conf_back(self) -> None:
        # The folder's sync runs after the rename: the new text is in place when it fails.
        for before in ("NAS_ENABLED=1\n", None):
            with self.subTest(before=before):
                world = ScrubWorld()
                world.put_conf(before)
                world.fail("settings sync", _eio(), times=1)
                rc, lines = world.service().apply("500M")
                self.assertEqual(rc, 1)
                self.assertEqual(
                    lines, [f"ERROR: cannot write {CONF}: Input/output error; previous configuration restored."]
                )
                self.assertEqual(world.conf, before)
                self.assertEqual(world.conf_folder, before is not None)  # the folder this run made: gone
                self.assert_no_drop_in_and_no_reload(world)

    def test_a_write_that_fails_after_its_rename_and_cannot_go_back_says_so(self) -> None:
        world = ScrubWorld()
        world.put_conf("NAS_ENABLED=1\n")
        world.fail("settings sync", _eio(), times=2)  # the write and the rollback's
        rc, lines = world.service().apply("500M")
        self.assertEqual(rc, 1)
        self.assertEqual(
            lines, [f"ERROR: cannot write {CONF}: Input/output error; ROLLBACK INCOMPLETE ({CONF}: Input/output error)"]
        )
        self.assert_no_drop_in_and_no_reload(world)

    def test_a_reload_this_run_did_not_ask_for_has_a_time_limit(self) -> None:
        # (start conf, drop-in, systemd behind, limit) -> the time limit of each daemon-reload
        cases = [
            (None, None, False, "500M", [None]),  # this run wrote the drop-in: no limit
            (as_asked(None, "500M"), DROPIN_500M, True, "500M", [60]),  # nothing to change, systemd behind
            ("NAS_ENABLED=1\n", DROPIN_500M, True, "500M", [60]),  # only snapshots.conf changes
            (as_asked(None, "500M"), DROPIN_500M, False, "500M", []),  # in step: no reload
        ]
        for conf, dropin, behind, limit, want in cases:
            with self.subTest(conf=conf, behind=behind):
                world = ScrubWorld()
                world.put_conf(conf)
                world.put_dropin(dropin)
                world.loaded, world.dirty = (None, True) if behind else (dropin, False)
                rc, lines = world.service().apply(limit)
                self.assertEqual(rc, 0, lines)
                self.assertEqual(world.reload_timeouts, want)
                world.show_fails = True  # systemctl cannot say: a reload with the same limit
                world.reload_timeouts.clear()
                rc, lines = world.service().apply(limit)
                self.assertEqual((rc, world.reload_timeouts), (0, [60]), lines)


class ScrubWorldFailTest(unittest.TestCase):
    """A call failed through ScrubWorld.fail() goes through its FaultPlan too: the plan records it, and
    after a KILL a dead process runs it no more."""

    def test_a_call_failed_by_fail_is_a_point_of_the_plan(self) -> None:
        world = ScrubWorld()
        world.fail("systemctl daemon-reload", OSError(errno.EACCES, "Access denied"))
        r = FakeScrubSystemd(world).daemon_reload(timeout=60)
        self.assertEqual((r.returncode, r.stderr), (1, "Access denied\n"))
        self.assertEqual(world.plan.calls, ["systemctl daemon-reload"])
        self.assertEqual(world.reload_timeouts, [60])  # asked for, and it failed

    def test_after_a_kill_a_call_failed_by_fail_never_runs(self) -> None:
        world = ScrubWorld()
        world.plan = FaultPlan(dead_after_kill=True).arm((FaultMode.KILL, 1))
        world.fail("systemctl daemon-reload", OSError(errno.EACCES, "Access denied"), times=1)
        with self.assertRaises(Killed):
            FakeScrubDropin(world).write(render_scrub_dropin("500M"))
        with self.assertRaises(Killed):
            FakeScrubSystemd(world).daemon_reload(timeout=60)
        self.assertEqual(world.plan.calls, ["drop-in text"])
        self.assertEqual(world.reload_timeouts, [])  # a dead process asks for no reload
        self.assertEqual(world.failing["systemctl daemon-reload"][1], 1)  # still armed: it never ran

    def test_a_kill_armed_at_a_call_failed_by_fail_without_a_callback_fires(self) -> None:
        # "settings replace" reports a failure by raising (no `failed` callback): the failure is recorded,
        # its effect never runs, and the kill armed at that call still fires.
        world = ScrubWorld()
        world.plan.arm((FaultMode.KILL, 1))
        world.fail("settings replace", OSError(errno.EIO, "Input/output error"), times=1)
        ran: list[str] = []
        with self.assertRaises(Killed):
            world.step("settings replace", lambda: ran.append("replaced"))
        self.assertEqual(ran, [])
        self.assertEqual(world.answered, ["settings replace"])
        self.assertEqual(world.plan.calls, ["settings replace"])
        self.assertEqual(world.failing["settings replace"][1], 0)  # used up

    def test_a_call_failed_by_fail_without_a_callback_raises_its_error(self) -> None:
        world = ScrubWorld()
        world.fail("settings replace", OSError(errno.EIO, "Input/output error"), times=1)
        with self.assertRaises(OSError) as caught:
            world.step("settings replace", lambda: None)
        self.assertEqual(caught.exception.errno, errno.EIO)
        self.assertEqual(world.answered, ["settings replace"])
        world.step("settings replace", lambda: None)  # used up: the next call runs
        self.assertEqual(world.answered, ["settings replace"])

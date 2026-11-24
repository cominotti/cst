# SPDX-License-Identifier: MPL-2.0
"""Exhaustive fault injection on docker-volumes-nocow's boot migration (tests/fault_injection.py).

The use case runs over its real adapters on a temporary folder, with the fakes of
test_docker_volumes_nocow (Base: the NOCOW ioctls, the commands, os.sync) and os.fsync stubbed
(what a kill leaves is checked here, not durability). Each mutating call of
the volume tree and of the state files goes through a FaultPlan. One migration of two volumes
from a volumes folder without +C (Docker installed after btrfs-setup, no flag yet) has N such
calls; for every mode and every k in 1..N the migration fails at the k-th call. Then the next
boot's recover() runs, also with a second fault at each of its own steps (tree.rename and
tree.remove_tree, the steps recover decides in the application), and then a whole migrate,
with no fault.

Checked after each fault, after the recovery and at the end:
- no data is lost: each volume's _data, if it is there, holds the original content; if it is
  not, its newest old copy (what recover puts back) does;
- the request is not lost: when the volumes folder has +C and a volume still holds
  copy-on-write data, the flag exists (the flag is written before +C);
- the end converges: every volume holds its original content, all NOCOW, no leftovers, no flag,
  no attempts counter, and status says every volume is fully NOCOW.

A boot that gives up (the third of a scheduled migration) is checked the same way: each fault
there, then two more boots, leave the volumes as they were and no flag and no counter.

A migration from a count of boots an older helper left without its flag (the volumes folder
without +C again, as after Docker is reinstalled) is checked as the first one: its new request
resets that count before it writes the flag, and every fault there still converges in one boot.

One class per start state and fault mode (each a work unit of tests/run_unit.py of its own), each
pinning its number of cases.
"""

from __future__ import annotations

from collections.abc import Callable
import contextlib
import functools
import io
import os
import stat
from typing import override

from btrfs.docker_volumes_nocow.adapters import DockerDaemons, LocalVolumeTree, NocowOutput, NocowStateFiles
from btrfs.docker_volumes_nocow.application import DockerVolumesNocow
from btrfs.docker_volumes_nocow.domain import OLD_MARK, NocowCommand, NocowPaths, PostponedRecord, TreeScan
from btrfs.tests.fakes import entries, tree
from btrfs.tests.test_docker_volumes_nocow import Base, fresh
from cst_core.adapters import SystemClockAdapter
from cst_core.ports import CommandResult, CommandRunnerPort
from tests.fault_injection import (
    ALL_MODES,
    Fault,
    FaultMode,
    FaultPlan,
    Killed,
    cut_points,
    every_fault,
    ignore_error,
    stub_fsync,
)

# Small volumes: the protocol is the same with 4 KiB as with 2 MiB, and each case builds them anew.
SMALL = {"db/base/1": b"\x01" * 4096, "db/base/empty": b"", "README": b"hello volume\n"}
VOLUMES = ("a", "b")
# The cases each class runs (a change of the protocol's calls moves them: pinned so it is seen).
CASES_MIGRATION_KILL = 67  # 25 first faults, 42 with a second fault in recover()
CASES_MIGRATION_FAIL = 55  # 25 and 30
CASES_MIGRATION_FAIL_FROM = 67  # 25 and 42
CASES_GIVE_UP = 4  # per mode: each of the boot's points, and the boot with no fault
# The migration's cases, and one more first fault: the reset of the old count (recover has no step then).
CASES_STALE_KILL = 68
CASES_STALE_FAIL = 56
CASES_STALE_FAIL_FROM = 68


class FaultyTree(LocalVolumeTree):
    """LocalVolumeTree whose mutating calls go through a FaultPlan."""

    def __init__(self, paths: NocowPaths, runner: CommandRunnerPort, plan: FaultPlan) -> None:
        super().__init__(paths, runner)
        self.plan = plan

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.paths.volumes)

    @override
    def scan(self, top: str, *, fix: bool = False) -> TreeScan:
        if not fix:
            return super().scan(top)
        return self.plan.step(f"scan-fix {self.rel(top)}", functools.partial(super().scan, top, fix=True))

    @override
    def set_nocow(self, path: str, *, on: bool) -> None:
        self.plan.step(f"set_nocow {self.rel(path)} {on}", functools.partial(super().set_nocow, path, on=on))

    @override
    def make_twin(self, data: str, new: str) -> None:
        self.plan.step(f"make_twin {self.rel(new)}", functools.partial(super().make_twin, data, new))

    @override
    def reference_label(self, data: str, new: str) -> None:
        self.plan.step(f"reference_label {self.rel(new)}", functools.partial(super().reference_label, data, new))

    @override
    def copy(self, data: str, new: str) -> CommandResult:
        def full(err: OSError) -> CommandResult:  # cp's answer on a full disk: an exit status, no exception
            return CommandResult(1, "", f"cp: {err.strerror}\n", argv=("cp",))

        return self.plan.step(f"copy {self.rel(new)}", functools.partial(super().copy, data, new), full)

    @override
    def remove_tree(self, path: str, *, ignore_errors: bool = False) -> None:
        effect = functools.partial(super().remove_tree, path, ignore_errors=ignore_errors)
        self.plan.step(f"remove_tree {self.rel(path)}", effect, ignore_error if ignore_errors else None)

    @override
    def rename(self, src: str, dst: str) -> None:
        self.plan.step(f"rename {self.rel(src)} {self.rel(dst)}", functools.partial(super().rename, src, dst))


class FaultyState(NocowStateFiles):
    """NocowStateFiles whose writes go through a FaultPlan."""

    def __init__(self, paths: NocowPaths, plan: FaultPlan) -> None:
        super().__init__(paths)
        self.plan = plan

    @override
    def request_migration(self) -> None:
        self.plan.step("request_migration", super().request_migration)

    @override
    def remove_flag(self) -> None:
        self.plan.step("remove_flag", super().remove_flag)

    @override
    def save_attempts(self, count: int) -> None:
        self.plan.step(f"save_attempts {count}", functools.partial(super().save_attempts, count))

    @override
    def remove_attempts(self) -> None:
        self.plan.step("remove_attempts", super().remove_attempts)

    @override
    def forget_attempts(self) -> None:
        self.plan.step("forget_attempts", super().forget_attempts)

    @override
    def write_postponed(self, record: PostponedRecord) -> None:
        self.plan.step("write_postponed", functools.partial(super().write_postponed, record))

    @override
    def clear_postponed(self) -> None:
        self.plan.step("clear_postponed", super().clear_postponed)


class NocowFaultBase(Base):
    """The fixture: a volumes folder without +C holding two copy-on-write volumes, no flag."""

    @override
    def setUp(self) -> None:
        super().setUp()
        stub_fsync(self)
        self.plan = FaultPlan()
        self.service = DockerVolumesNocow(
            paths=self.paths,
            tree=FaultyTree(self.paths, self.fs, self.plan),
            state=FaultyState(self.paths, self.plan),
            daemon=DockerDaemons(self.paths, self.fs),
            out=NocowOutput(self.paths, self.fs),
            clock=SystemClockAdapter(),
        )
        self.before = {name: tree(self.volume(name, data=SMALL)) for name in VOLUMES}
        self.arrange()

    def arrange(self) -> None:
        """The state before the boot: here none of the helper's (Docker installed after btrfs-setup)."""

    def quiet(self, fn: Callable[[], object]) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn()
        return out.getvalue()

    def boot(self, *faults: Fault) -> str:
        """One boot's migrate as the program runs it (an OSError is a journal line); Killed goes through."""
        self.plan.arm(*faults)
        return self.quiet(functools.partial(self.service.execute, NocowCommand.MIGRATE))

    def recover(self, *faults: Fault) -> list[str]:
        """The next boot's recover(), its tree.rename and tree.remove_tree through the plan: those steps.
        An OSError there ends the boot, as the program logs it; Killed goes through."""
        self.plan.arm(*faults)
        with contextlib.suppress(OSError):
            self.quiet(self.service.recover)
        return list(self.plan.calls)

    # ---- the invariants -------------------------------------------------

    def cow_data(self, name: str) -> bool:
        """The volume's _data holds a regular file with data and without +C."""
        data = os.path.join(self.vols, name, "_data")
        return os.path.isdir(data) and any(
            stat.S_ISREG(st.st_mode) and st.st_size and not self.fs.nocow(p) for p, st in entries(data)
        )

    def assert_safe(self, when: str, *, request_kept: bool = True) -> None:
        """No data lost; request_kept: nor the migration's request (not so for a boot that gives up)."""
        for name in VOLUMES:
            vol = os.path.join(self.vols, name)
            data = os.path.join(vol, "_data")
            olds = sorted(x for x in os.listdir(vol) if x.startswith("_data" + OLD_MARK))
            if os.path.lexists(data):
                self.assertEqual(tree(data), self.before[name], f"{when}: {name}/_data is not the original")
            else:
                self.assertTrue(olds, f"{when}: {name} lost its data: {sorted(os.listdir(vol))}")
                self.assertEqual(tree(os.path.join(vol, olds[-1])), self.before[name], f"{when}: {name} old copy")
        if request_kept and self.fs.nocow(self.vols) and any(self.cow_data(n) for n in VOLUMES):
            self.assertTrue(os.path.exists(self.flag), f"{when}: +C on the volumes folder, CoW data left, no flag")

    def assert_converged(self, when: str) -> None:
        """Every volume migrated, nothing of the helper's left."""
        for name in VOLUMES:
            data = os.path.join(self.vols, name, "_data")
            self.assertEqual(tree(data), self.before[name], f"{when}: {name}")
            self.assertEqual(os.listdir(os.path.join(self.vols, name)), ["_data"], f"{when}: {name} leftovers")
            for p, st in entries(data):
                if stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode):
                    self.assertTrue(self.fs.nocow(p), f"{when}: {p} without +C")
        self.assertTrue(self.fs.nocow(self.vols), when)
        self.assertFalse(os.path.exists(self.flag), f"{when}: flag left")
        self.assertFalse(os.path.exists(self.attempts), f"{when}: attempts left")
        status = self.quiet(self.service.status)
        self.assertIn(f"{len(VOLUMES)} of {len(VOLUMES)} volume(s) fully NOCOW", status, when)


class MigrationCutPointsTest(NocowFaultBase):
    """The fail-fast step: the mutating calls of one migration, and a plan that stops after the k-th."""

    def test_the_mutating_calls_of_one_migration(self) -> None:
        calls = cut_points(self.plan, self.boot)
        self.assert_converged("no fault")
        per_volume = [
            "make_twin {v}/_data.nocow-new",
            "reference_label {v}/_data.nocow-new",
            "set_nocow {v}/_data.nocow-new True",
            "copy {v}/_data.nocow-new",
            "rename {v}/_data {v}/_data.cow-old-",
            "rename {v}/_data.nocow-new {v}/_data",
            "remove_tree {v}/_data.cow-old-",
        ]
        want = [
            "clear_postponed",
            "request_migration",
            "set_nocow . True",
            "scan-fix a/_data",
            "scan-fix b/_data",
            "save_attempts 1",
            "set_nocow . True",
            "scan-fix a/_data",
            "scan-fix b/_data",
            *(c.format(v=v) for v in VOLUMES for c in per_volume),
            "remove_attempts",
            "remove_flag",
        ]
        # The old copy's name ends with the time: compared up to its mark.
        self.assertEqual([c.split(OLD_MARK)[0] + (OLD_MARK if OLD_MARK in c else "") for c in calls], want)

    def test_a_kill_stops_after_the_kth_call(self) -> None:
        n = len(cut_points(self.plan, self.boot))
        for k in (1, n // 2, n):
            with self.subTest(k=k):
                fresh(self)
                with self.assertRaises(Killed):
                    self.boot((FaultMode.KILL, k))
                self.assertEqual(len(self.plan.calls), k)


class MigrationFaultBase(NocowFaultBase):
    """Every fault point of one migration, then recovery (with a second fault at each of its points)."""

    def first_fault(self, mode: FaultMode, k: int) -> None:
        fresh(self)
        with contextlib.suppress(Killed):
            self.boot((mode, k))
        self.assert_safe(f"{mode} at {k}")

    def every_fault_then_recover(self, mode: FaultMode) -> int:
        """Each point of one migration failing in mode, then recover() (alone, and with a second fault
        of every mode at each of its steps), then a whole migrate; the number of cases run."""
        n = len(cut_points(self.plan, self.boot))
        cases = 0
        for _mode, k in every_fault(n, (mode,)):
            steps: list[str] = []
            with self.subTest(mode=mode.value, k=k):
                cases += 1
                self.first_fault(mode, k)
                steps = self.recover()  # no second fault: how many steps recover has here
                self.assert_safe(f"{mode} at {k}, recovered")
                self.boot()
                self.assert_converged(f"{mode} at {k}")
            for mode2, j in every_fault(len(steps), ALL_MODES):
                with self.subTest(mode=mode.value, k=k, then=mode2.value, recover_step=j):
                    cases += 1
                    self.first_fault(mode, k)
                    with contextlib.suppress(Killed):
                        self.recover((mode2, j))
                    self.assert_safe(f"{mode} at {k}, then {mode2} at recover step {j}")
                    self.boot()
                    self.assert_converged(f"{mode} at {k}, then {mode2} at recover step {j}")
        return cases


class MigrationKillTest(MigrationFaultBase):
    def test_every_kill_then_recover_then_migrate(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.KILL), CASES_MIGRATION_KILL)


class MigrationFailTest(MigrationFaultBase):
    def test_every_failure_then_recover_then_migrate(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL), CASES_MIGRATION_FAIL)


class MigrationFailFromTest(MigrationFaultBase):
    def test_every_full_disk_then_recover_then_migrate(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL_FROM), CASES_MIGRATION_FAIL_FROM)


class GiveUpFaultBase(NocowFaultBase):
    """The third boot of a scheduled migration: it gives up. Every fault there, then the next boots."""

    @override
    def arrange(self) -> None:
        self.fs.set(self.vols, nocow=True)
        with open(self.flag, "w"):
            pass
        self.set_attempts("2\n")

    def assert_settled(self, when: str) -> None:
        """Given up (the volumes as they were) or migrated after all; never a counter left behind."""
        for name in VOLUMES:
            data = os.path.join(self.vols, name, "_data")
            self.assertEqual(tree(data), self.before[name], f"{when}: {name}")
            self.assertEqual(os.listdir(os.path.join(self.vols, name)), ["_data"], f"{when}: {name} leftovers")
        self.assertFalse(os.path.exists(self.flag), f"{when}: flag left")
        self.assertFalse(os.path.exists(self.attempts), f"{when}: attempts left without a flag")

    def every_fault_while_giving_up(self, mode: FaultMode) -> int:
        """A boot with no fault gives up once; then each point of it failing in mode, and two more
        boots; the number of cases (the one with no fault included)."""
        n = len(cut_points(self.plan, self.boot))
        self.assert_settled("no fault")
        self.assertEqual(len(self.fs.commands(self.notify)), 1)
        cases = 1
        for _mode, k in every_fault(n, (mode,)):
            with self.subTest(mode=mode.value, k=k):
                cases += 1
                fresh(self)
                with contextlib.suppress(Killed):
                    self.boot((mode, k))
                self.assert_safe(f"{mode} at {k}", request_kept=False)
                self.boot()
                self.boot()
                self.assert_settled(f"{mode} at {k}")
        return cases


class GiveUpKillTest(GiveUpFaultBase):
    def test_every_kill_while_giving_up(self) -> None:
        self.assertEqual(self.every_fault_while_giving_up(FaultMode.KILL), CASES_GIVE_UP)


class GiveUpFailTest(GiveUpFaultBase):
    def test_every_failure_while_giving_up(self) -> None:
        self.assertEqual(self.every_fault_while_giving_up(FaultMode.FAIL), CASES_GIVE_UP)


class GiveUpFailFromTest(GiveUpFaultBase):
    def test_every_full_disk_while_giving_up(self) -> None:
        self.assertEqual(self.every_fault_while_giving_up(FaultMode.FAIL_FROM), CASES_GIVE_UP)


class StaleCountFaultBase(MigrationFaultBase):
    """A count of boots an older helper left without its flag (it removed the flag, then the count, and
    could stop between the two), and the volumes folder without +C (Docker reinstalled): the new request
    resets the count, durably, before it writes its flag. Every fault of that boot, then recovery and one
    whole boot, must converge: a request that went on with the old count would give up instead."""

    @override
    def arrange(self) -> None:
        self.set_attempts("2\n")


class StaleCountCutPointsTest(StaleCountFaultBase):
    def test_the_count_is_reset_before_the_flag_is_written(self) -> None:
        calls = cut_points(self.plan, self.boot)
        self.assert_converged("no fault")
        self.assertEqual(calls[:3], ["clear_postponed", "save_attempts 0", "request_migration"])
        self.assertEqual(calls.count("save_attempts 1"), 1)  # this request's first boot


class StaleCountKillTest(StaleCountFaultBase):
    def test_every_kill_then_recover_then_migrate(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.KILL), CASES_STALE_KILL)


class StaleCountFailTest(StaleCountFaultBase):
    def test_every_failure_then_recover_then_migrate(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL), CASES_STALE_FAIL)


class StaleCountFailFromTest(StaleCountFaultBase):
    def test_every_full_disk_then_recover_then_migrate(self) -> None:
        self.assertEqual(self.every_fault_then_recover(FaultMode.FAIL_FROM), CASES_STALE_FAIL_FROM)

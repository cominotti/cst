# SPDX-License-Identifier: MPL-2.0
"""Unit tests for tests/fault_injection.py, the fault injection harness the crash and rollback tests use."""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
import errno
import os
import shutil
import tempfile
from typing import override
import unittest
from unittest import mock

from cst_core.ports import CommandResult
from tests.fault_injection import (
    ALL_MODES,
    FaultMode,
    FaultPlan,
    FaultyOs,
    FaultyRunner,
    Killed,
    cut_points,
    every_fault,
    ignore_error,
    no_space,
    stub_fsync,
)


class Effects:
    """Numbered calls of a run: each one, when it takes effect, notes its number."""

    def __init__(self, plan: FaultPlan) -> None:
        self.plan = plan
        self.done: list[int] = []

    def call(self, n: int) -> int:
        return self.plan.step(f"call {n}", lambda: self.note(n))

    def note(self, n: int) -> int:
        self.done.append(n)
        return n

    def run(self, count: int) -> list[int | str]:
        """count calls, each in turn; a failed one (OSError) is noted as "failed N", and the run goes on."""
        out: list[int | str] = []
        for n in range(1, count + 1):
            try:
                out.append(self.call(n))
            except OSError as e:
                self.assert_no_space(e)
                out.append(f"failed {n}")
        return out

    @staticmethod
    def assert_no_space(e: OSError) -> None:
        if e.errno != errno.ENOSPC:
            raise AssertionError(f"not ENOSPC: {e!r}")


class FaultPlanTest(unittest.TestCase):
    def test_a_run_with_no_fault_runs_every_call_and_records_it(self) -> None:
        plan = FaultPlan()
        fx = Effects(plan)
        self.assertEqual(fx.run(3), [1, 2, 3])
        self.assertEqual(plan.calls, ["call 1", "call 2", "call 3"])

    def test_kill_at_k_keeps_calls_up_to_k_and_runs_nothing_after(self) -> None:
        for k in (1, 3, 5):
            with self.subTest(k=k):
                plan = FaultPlan().arm((FaultMode.KILL, k))
                fx = Effects(plan)
                with self.assertRaises(Killed):
                    fx.run(5)
                self.assertEqual(fx.done, list(range(1, k + 1)))
                self.assertEqual(len(plan.calls), k)

    def test_kill_is_not_caught_by_except_exception(self) -> None:
        plan = FaultPlan().arm((FaultMode.KILL, 1))
        fx = Effects(plan)
        # What the code under test catches (an Exception) does not stop a kill.
        with self.assertRaises(Killed), contextlib.suppress(Exception):
            fx.call(1)
        self.assertEqual(fx.done, [1])

    def test_fail_at_k_has_no_effect_and_the_next_call_runs(self) -> None:
        plan = FaultPlan().arm((FaultMode.FAIL, 2))
        fx = Effects(plan)
        self.assertEqual(fx.run(4), [1, "failed 2", 3, 4])
        self.assertEqual(fx.done, [1, 3, 4])

    def test_fail_with_failed_returns_its_answer_instead_of_raising(self) -> None:
        plan = FaultPlan().arm((FaultMode.FAIL, 1))
        seen: list[OSError] = []
        effects: list[str] = []

        def failed(err: OSError) -> str:
            seen.append(err)
            return "exit 1"

        self.assertEqual(plan.step("cmd", lambda: effects.append("ran") or "exit 0", failed), "exit 1")
        self.assertEqual(effects, [])
        self.assertEqual([e.errno for e in seen], [errno.ENOSPC])
        self.assertEqual(plan.step("cmd", lambda: effects.append("ran") or "exit 0", failed), "exit 0")
        self.assertEqual(effects, ["ran"])

    def test_ignore_error_is_a_failure_with_no_effect_and_no_exception(self) -> None:
        plan = FaultPlan().arm((FaultMode.FAIL, 1))
        effects: list[str] = []
        self.assertIsNone(plan.step("rmtree", lambda: effects.append("ran"), ignore_error))
        self.assertEqual(effects, [])

    def test_fail_from_k_fails_k_and_every_later_call(self) -> None:
        plan = FaultPlan().arm((FaultMode.FAIL_FROM, 3))
        fx = Effects(plan)
        self.assertEqual(fx.run(5), [1, 2, "failed 3", "failed 4", "failed 5"])
        self.assertEqual(fx.done, [1, 2])

    def test_two_faults_in_one_plan_both_apply(self) -> None:
        plan = FaultPlan().arm((FaultMode.FAIL, 2), (FaultMode.KILL, 4))
        fx = Effects(plan)
        with self.assertRaises(Killed):
            fx.run(6)
        self.assertEqual(fx.done, [1, 3, 4])
        self.assertEqual(len(plan.calls), 4)

    def test_arm_starts_a_new_run(self) -> None:
        plan = FaultPlan().arm((FaultMode.FAIL, 1))
        fx = Effects(plan)
        fx.run(2)
        plan.arm()
        self.assertEqual(plan.calls, [])
        self.assertEqual(plan.faults, ())
        self.assertEqual(fx.run(2), [1, 2])

    def test_cut_points_are_the_calls_of_a_run_with_no_fault(self) -> None:
        plan = FaultPlan().arm((FaultMode.FAIL_FROM, 1))  # armed before: cut_points runs with none
        fx = Effects(plan)
        self.assertEqual(cut_points(plan, lambda: fx.run(3)), ["call 1", "call 2", "call 3"])
        self.assertEqual(fx.done, [1, 2, 3])

    def test_every_fault_lists_each_mode_at_each_point_mode_by_mode(self) -> None:
        self.assertEqual(
            every_fault(2),
            [
                (FaultMode.KILL, 1),
                (FaultMode.KILL, 2),
                (FaultMode.FAIL, 1),
                (FaultMode.FAIL, 2),
                (FaultMode.FAIL_FROM, 1),
                (FaultMode.FAIL_FROM, 2),
            ],
        )
        self.assertEqual(every_fault(4, (FaultMode.KILL,), after=2), [(FaultMode.KILL, 3), (FaultMode.KILL, 4)])
        self.assertEqual(every_fault(2, after=2), [])
        self.assertEqual([str(m) for m in ALL_MODES], ["kill", "fail", "fail-from"])

    def test_no_space_is_enospc(self) -> None:
        err = no_space()
        self.assertEqual(err.errno, errno.ENOSPC)
        self.assertEqual(err.strerror, os.strerror(errno.ENOSPC))


class StubFsyncTest(unittest.TestCase):
    def test_fsync_reaches_nothing_while_the_test_runs_and_is_back_after_its_cleanups(self) -> None:
        recorder = mock.Mock(name="fsync")
        with mock.patch.object(os, "fsync", recorder):
            inner = unittest.TestCase()
            stub_fsync(inner)
            with tempfile.TemporaryFile() as f:
                f.write(b"data")
                os.fsync(f.fileno())
                fd = os.dup(f.fileno())
            os.close(fd)
            with self.assertRaises(OSError) as raised:
                os.fsync(fd)  # a closed descriptor: EBADF, as the real fsync says
            self.assertEqual(raised.exception.errno, errno.EBADF)
            recorder.assert_not_called()
            inner.doCleanups()
            self.assertIs(os.fsync, recorder)  # nothing left behind for the worker's later tests


class FaultyOsTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-faulty-os-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.plan = FaultPlan()
        self.fos = FaultyOs(self.plan, ("mkdir", "rmdir"))

    def test_a_named_call_is_a_point_with_its_effect(self) -> None:
        d = os.path.join(self.tmp, "d")
        mkdir = self.fos.mkdir
        assert callable(mkdir)
        mkdir(d, 0o700)
        self.assertTrue(os.path.isdir(d))
        rmdir = self.fos.rmdir
        assert callable(rmdir)
        rmdir(d)
        self.assertFalse(os.path.lexists(d))
        self.assertEqual(self.plan.calls, ["os.mkdir", "os.rmdir"])

    def test_every_other_name_is_os_own_and_no_point(self) -> None:
        self.assertIs(self.fos.listdir, os.listdir)
        self.assertIs(self.fos.path, os.path)
        self.assertEqual(self.fos.sep, os.sep)
        listdir = self.fos.listdir
        assert callable(listdir)
        self.assertEqual(listdir(self.tmp), [])
        self.assertEqual(self.plan.calls, [])

    def test_a_fault_on_a_named_call_fails_it_with_no_effect(self) -> None:
        d = os.path.join(self.tmp, "d")
        self.plan.arm((FaultMode.FAIL, 1))
        mkdir = self.fos.mkdir
        assert callable(mkdir)
        with self.assertRaises(OSError) as raised:
            mkdir(d)
        self.assertEqual(raised.exception.errno, errno.ENOSPC)
        self.assertFalse(os.path.lexists(d))

    def test_a_kill_on_a_named_call_keeps_its_effect(self) -> None:
        d = os.path.join(self.tmp, "d")
        self.plan.arm((FaultMode.KILL, 1))
        mkdir = self.fos.mkdir
        assert callable(mkdir)
        with self.assertRaises(Killed):
            mkdir(d)
        self.assertTrue(os.path.isdir(d))
        os.rmdir(d)

    def test_a_name_os_has_no_function_for_is_refused(self) -> None:
        with self.assertRaises(AttributeError):
            FaultyOs(self.plan, ("no_such_call",))
        with self.assertRaises(AttributeError):
            FaultyOs(self.plan, ("sep",))  # a value, not a function
        with self.assertRaises(AttributeError):
            FaultyOs(self.plan, ("no_such_call",), kill_only=True)

    def test_kill_only_runs_a_call_a_failure_is_armed_at(self) -> None:
        fos = FaultyOs(self.plan, ("mkdir",), kill_only=True)
        d = os.path.join(self.tmp, "d")
        for mode in (FaultMode.FAIL, FaultMode.FAIL_FROM):
            with self.subTest(mode=mode):
                self.plan.arm((mode, 1))
                mkdir = fos.mkdir
                assert callable(mkdir)
                mkdir(d)  # no OSError: the call ran
                self.assertTrue(os.path.isdir(d))
                self.assertEqual(self.plan.calls, ["os.mkdir"])
                os.rmdir(d)

    def test_kill_only_still_kills_after_the_call(self) -> None:
        fos = FaultyOs(self.plan, ("mkdir",), kill_only=True)
        d = os.path.join(self.tmp, "d")
        self.plan.arm((FaultMode.KILL, 1))
        mkdir = fos.mkdir
        assert callable(mkdir)
        with self.assertRaises(Killed):
            mkdir(d)
        self.assertTrue(os.path.isdir(d))
        os.rmdir(d)


class _Inner:
    """A fake command runner that notes what reaches it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str], *, input_text: str | None = None, **_kw: object) -> CommandResult:
        self.calls.append([*argv, *([] if input_text is None else [f"<{input_text}"])])
        return CommandResult(0, "ok\n", "", argv=tuple(argv))


def _reads(argv: Sequence[str]) -> bool:
    return list(argv[:2]) == ["systemctl", "show"]


class FaultyRunnerTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.plan = FaultPlan()
        self.inner = _Inner()
        self.runner = FaultyRunner(self.plan, self.inner, reads=_reads)

    def test_a_reading_command_reaches_the_inner_runner_and_is_no_point(self) -> None:
        self.plan.arm((FaultMode.FAIL_FROM, 1))
        r = self.runner.run(["systemctl", "show", "-p", "X", "u"])
        self.assertEqual((r.returncode, r.stdout), (0, "ok\n"))
        self.assertEqual(self.plan.calls, [])
        self.assertEqual(self.inner.calls, [["systemctl", "show", "-p", "X", "u"]])

    def test_a_changing_command_is_a_point_named_by_its_argv_and_keeps_its_options(self) -> None:
        r = self.runner.run(["systemctl", "daemon-reload"], input_text="in")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.plan.calls, ["systemctl daemon-reload"])
        self.assertEqual(self.inner.calls, [["systemctl", "daemon-reload", "<in"]])

    def test_name_gives_the_point_its_name(self) -> None:
        runner = FaultyRunner(self.plan, self.inner, reads=_reads, name=lambda argv: argv[0])
        runner.run(["installer.py", "text", "/x"])
        self.assertEqual(self.plan.calls, ["installer.py"])

    def test_fail_answers_exit_1_with_the_error_and_the_inner_runner_never_sees_it(self) -> None:
        self.plan.arm((FaultMode.FAIL, 1))
        r = self.runner.run(["systemctl", "daemon-reload"])
        self.assertEqual((r.returncode, r.stdout, r.stderr), (1, "", os.strerror(errno.ENOSPC) + "\n"))
        self.assertEqual(r.argv, ("systemctl", "daemon-reload"))
        self.assertEqual(self.inner.calls, [])

    def test_kill_runs_the_command_then_dies(self) -> None:
        self.plan.arm((FaultMode.KILL, 1))
        with self.assertRaises(Killed):
            self.runner.run(["systemctl", "daemon-reload"])
        self.assertEqual(self.inner.calls, [["systemctl", "daemon-reload"]])

    def test_fail_from_fails_every_later_changing_command_but_not_the_reads(self) -> None:
        self.plan.arm((FaultMode.FAIL_FROM, 2))
        rcs = [
            self.runner.run(argv).returncode
            for argv in (
                ["systemctl", "enable", "u"],
                ["systemctl", "show", "u"],
                ["systemctl", "daemon-reload"],
                ["systemctl", "show", "u"],
                ["udevadm", "control", "--reload-rules"],
            )
        ]
        self.assertEqual(rcs, [0, 0, 1, 0, 1])
        self.assertEqual(
            self.plan.calls, ["systemctl enable u", "systemctl daemon-reload", "udevadm control --reload-rules"]
        )
        self.assertEqual(
            self.inner.calls, [["systemctl", "enable", "u"], ["systemctl", "show", "u"], ["systemctl", "show", "u"]]
        )


if __name__ == "__main__":
    unittest.main()

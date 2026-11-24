# SPDX-License-Identifier: MPL-2.0
"""tests/run_unit.py: what it loads (all, --gates, names), and its run-time gates."""

import os
import shutil
import subprocess
import sys
import tempfile
import types
from typing import override
import unittest
from unittest import mock

from tests.repo_files import REPO, repo_files
import tests.run_unit as run_unit_module
from tests.run_unit import (
    BYTECODE_HINT,
    DEFAULT_CLASS_CHUNK,
    EXPECTED_SKIPS,
    GATE_MODULES,
    HOST_COMMANDS,
    HOST_GUARD_ENV,
    HOST_GUARD_EXIT,
    LEAK_HINT,
    LOST_EXCEPTION_HINT,
    RunOptions,
    bytecode_files,
    default_jobs,
    expected_skip,
    load,
    new_bytecode,
    parse_args,
    plan_work_units,
    skip_problems,
)


def ids(suite: unittest.TestSuite) -> set[str]:
    out: set[str] = set()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            out |= ids(test)
        else:
            out.add(test.id())
    return out


# A child that sets the flag the way the runner does, then reports it and,
# when sudo is there and the run is not root, what sudo answers (it must refuse to run).
_NO_PRIVS_CHILD = """
import os, shutil, subprocess, sys
sys.path.insert(0, sys.argv[1])
from tests.run_unit import forbid_privilege_gain
forbid_privilege_gain()
with open("/proc/self/status") as f:
    print(next(line for line in f if line.startswith("NoNewPrivs:")).split()[1])
sudo = shutil.which("sudo")
if sudo and os.geteuid() != 0:  # root needs no setuid: sudo runs whatever the flag says
    done = subprocess.run([sudo, "-n", "true"], capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
    print(done.returncode)
"""


class NoPrivilegeGainTest(unittest.TestCase):
    def test_the_runner_keeps_every_test_from_becoming_root(self) -> None:
        # A test that ran sudo passed in a toolbox (sudo without a password), as root on the real machine.
        done = subprocess.run(
            [sys.executable, "-I", "-B", "-c", _NO_PRIVS_CHILD, REPO],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        lines = done.stdout.split()
        self.assertEqual(lines[:1], ["1"], done.stdout + done.stderr)
        if shutil.which("sudo") is not None and os.geteuid() != 0:
            self.assertNotEqual(lines[1:], ["0"], "sudo ran as root despite no_new_privs")


# A child that puts the stubs in place the way the runner does, then runs
# each command with the environment it inherited: "name rc refused" lines.
_HOST_GUARD_CHILD = """
import os, subprocess, sys, tempfile
sys.path.insert(0, sys.argv[1])
from tests.run_unit import HOST_COMMANDS, guard_host_commands, guard_hits
with tempfile.TemporaryDirectory() as folder:
    guard_host_commands(folder)
    for name in HOST_COMMANDS:
        done = subprocess.run([name, "--version"], capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False)
        print(name, done.returncode, f"unit test reached the real {name}" in done.stderr)
    print("hits:", guard_hits(folder))
"""


class HostCommandGuardTest(unittest.TestCase):
    def test_a_child_that_runs_a_host_command_is_refused(self) -> None:
        # A helper thread that outlived a patch of subprocess.Popen ran the real systemctl,
        # which from a toolbox starts a unit on the host.
        done = subprocess.run(
            [sys.executable, "-I", "-B", "-c", _HOST_GUARD_CHILD, REPO],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        self.assertEqual(
            done.stdout.splitlines(),
            [f"{name} {HOST_GUARD_EXIT} True" for name in HOST_COMMANDS]
            + [f"hits: {[f'{name} --version' for name in HOST_COMMANDS]}"],
            done.stderr,
        )

    def test_a_run_whose_test_ignored_the_refusal_fails(self) -> None:
        # The helper thread's call: nobody read its exit status, the run must fail anyway.
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="cst-guard-run-"))
        probe = os.path.join(tmp, "probe_guard.py")
        with open(probe, "w", encoding="utf-8") as f:
            f.write(
                "import subprocess, unittest\n"
                "class T(unittest.TestCase):\n"
                "    def test_ignores(self):\n"
                "        subprocess.run(['systemctl', 'start', 'x.service'], capture_output=True,"
                " stdin=subprocess.DEVNULL, check=False)\n"
            )
        env = {**os.environ, "PYTHONPATH": tmp}
        done = subprocess.run(
            [sys.executable, "-B", os.path.join(REPO, "tests", "run_unit.py"), "probe_guard"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
            env=env,
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("error: a unit test reached the real systemctl start x.service", done.stderr)

    def test_the_runner_puts_the_stubs_first_on_path(self) -> None:
        main = sys.modules["__main__"]
        if os.path.basename(getattr(main, "__file__", "") or "") != "run_unit.py":
            return  # a bare python3 -m unittest: no runner, no stubs (AGENTS.md says to use the runner)
        folder = os.environ.get(HOST_GUARD_ENV, "")
        self.assertTrue(folder, f"tests/run_unit.py did not set {HOST_GUARD_ENV}")
        for name in HOST_COMMANDS:
            with self.subTest(name=name):
                self.assertEqual(shutil.which(name), os.path.join(folder, name))


# Test modules run by the runner in a child: each leaks a resource in its own way, or does not.
_LEAKS = """
import gc, os, unittest, warnings
class T(unittest.TestCase):
    def test_leaks_a_file(self):
        f = open(os.devnull)
    def test_closes_its_file(self):
        with open(os.devnull):
            pass
    def test_leaks_a_file_in_a_cycle(self):
        gc.disable()  # only the runner's last collection frees it
        cycle = [open(os.devnull)]
        cycle.append(cycle)
    def test_records_its_own_warning(self):
        with warnings.catch_warnings(record=True) as seen:
            warnings.simplefilter("always")
            f = open(os.devnull)
            del f
        self.assertEqual([w.category for w in seen], [ResourceWarning])
class S(unittest.TestCase):
    def test_replaces_showwarning_then_leaks(self):
        warnings.showwarning = lambda *args, **kwargs: None  # as logging.captureWarnings(True) does
        f = open(os.devnull)
        del f
class U(unittest.TestCase):
    def test_a_weakref_callback_raises(self):
        import weakref
        class Freed:
            pass
        thing = Freed()
        def boom_callback(ref):
            raise ZeroDivisionError("boom")
        self.ref = weakref.ref(thing, boom_callback)
        del thing
class C(unittest.TestCase):
    def test_a_leaks_a_file_in_a_cycle(self):
        gc.disable()  # the next test's collection frees it
        cycle = [open(os.devnull)]
        cycle.append(cycle)
    def test_b_collects(self):
        gc.collect()
        gc.enable()
class P(unittest.TestCase):
    def test_replaces_showwarning_with_one_that_calls_the_original_then_leaks(self):
        warnings.showwarning = lambda *args, **kwargs: warnings._showwarning_orig(*args, **kwargs)
        f = open(os.devnull)
        del f
class H(unittest.TestCase):
    def test_a_replaces_the_unraisable_hook(self):
        import sys
        sys.unraisablehook = lambda unraisable: None  # and does not restore it
    def test_b_a_weakref_callback_raises(self):
        U.test_a_weakref_callback_raises(self)
class R(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        cls.saved = sys.unraisablehook
        sys.unraisablehook = cls.own_hook  # restored by tearDownClass
    @classmethod
    def tearDownClass(cls):
        import sys
        sys.unraisablehook = cls.saved
    @staticmethod
    def own_hook(unraisable):
        pass
    def test_a_sees_its_own_hook(self):
        import sys
        self.assertIs(sys.unraisablehook, R.own_hook)
    def test_b_sees_its_own_hook(self):
        import sys
        self.assertIs(sys.unraisablehook, R.own_hook)
class Q(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import sys
        sys.unraisablehook = lambda unraisable: None  # and no tearDownClass restores it
    def test_runs(self):
        pass
class N(unittest.TestCase):
    def test_a_showwarning_that_warns_once_then_calls_the_original(self):
        shown = []
        def show(*args, **kwargs):
            if not shown:
                shown.append(1)
                warnings.warn("a second warning", ResourceWarning)
            warnings._showwarning_orig(*args, **kwargs)
        warnings.showwarning = show
        f = open(os.devnull)
        del f
class K(unittest.TestCase):
    def test_ignores_every_warning_and_keeps_a_file(self):
        warnings.simplefilter("ignore")  # outside a catch_warnings block
        self.f = open(os.devnull)  # freed when the suite drops the test case, after stopTest
class F(unittest.TestCase):
    def test_a_ignores_every_warning(self):
        warnings.simplefilter("ignore")  # outside a catch_warnings block: for the rest of the run
    def test_b_leaks_a_file(self):
        f = open(os.devnull)
        del f
"""


# A module whose set-up replaces sys.unraisablehook and whose tear-down does not restore it.
_HOOK_LEFT_BY_A_MODULE = """
import sys, unittest
def setUpModule():
    sys.unraisablehook = lambda unraisable: None
class T(unittest.TestCase):
    def test_a(self):
        pass
    def test_b(self):
        pass
"""

# A module whose test loses an exception in a weakref callback.
_LOSES_AN_EXCEPTION = """
import unittest, weakref
class T(unittest.TestCase):
    def test_a_weakref_callback_raises(self):
        class Freed:
            pass
        thing = Freed()
        def boom_callback(ref):
            raise ZeroDivisionError("boom")
        self.ref = weakref.ref(thing, boom_callback)
        del thing
"""


_SIGNALS = """
import signal
import unittest


class Leaks(unittest.TestCase):
    def test_a_sets_sigpipe_to_its_default(self):
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)

    def test_b_sees_the_disposition_the_run_started_with(self):
        self.assertEqual(signal.getsignal(signal.SIGPIPE), signal.SIG_IGN)


class Restores(unittest.TestCase):
    def test_puts_it_back_in_a_cleanup(self):
        self.addCleanup(signal.signal, signal.SIGPIPE, signal.getsignal(signal.SIGPIPE))
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        self.addCleanup(signal.pthread_sigmask, signal.SIG_UNBLOCK, {signal.SIGUSR1})
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})


class Masks(unittest.TestCase):
    def test_blocks_sigusr1(self):
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGUSR1})


class FixtureRestores(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old = signal.signal(signal.SIGUSR2, signal.SIG_IGN)

    @classmethod
    def tearDownClass(cls):
        signal.signal(signal.SIGUSR2, cls.old)

    def test_a_sees_the_class_s_disposition(self):
        self.assertEqual(signal.getsignal(signal.SIGUSR2), signal.SIG_IGN)

    def test_b_sees_it_too(self):
        self.assertEqual(signal.getsignal(signal.SIGUSR2), signal.SIG_IGN)


class FixtureLeaks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        signal.signal(signal.SIGUSR2, signal.SIG_IGN)

    def test_runs(self):
        pass
"""


class SignalGateTest(unittest.TestCase):
    """A test that leaves a signal disposition or the signal mask changed changes it for every later test
    of its worker: it fails the run, even though unittest says OK, and the runner puts it back."""

    def run_probe(self, *names: str, options: tuple[str, ...] = ()) -> subprocess.CompletedProcess[str]:
        """The runner (or with options ("-m", "unittest"), unittest alone) in a child on classes of
        _SIGNALS."""
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="cst-signal-run-"))
        with open(os.path.join(tmp, "probe_signals.py"), "w", encoding="utf-8") as f:
            f.write(_SIGNALS)
        argv = (
            list(options)
            if options[:2] == ("-m", "unittest")
            else [os.path.join(REPO, "tests", "run_unit.py"), *options]
        )
        return subprocess.run(
            [sys.executable, "-B", *argv, *(f"probe_signals.{name}" for name in names)],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
            env={**os.environ, "PYTHONPATH": tmp},
        )

    def errors(self, done: subprocess.CompletedProcess[str]) -> list[str]:
        return [line for line in done.stderr.splitlines() if line.startswith("error: ")]

    def test_a_changed_disposition_fails_the_run_and_is_put_back(self) -> None:
        # unittest alone: OK for the test itself, and the next test then sees SIG_DFL (fails).
        self.assertEqual(
            self.run_probe("Leaks.test_a_sets_sigpipe_to_its_default", options=("-m", "unittest")).returncode, 0
        )
        self.assertIn("FAIL: test_b", self.run_probe("Leaks", options=("-m", "unittest")).stderr)
        done = self.run_probe("Leaks")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(
            self.errors(done),
            [
                (
                    "error: signal SIGPIPE changed and not restored by "
                    "probe_signals.Leaks.test_a_sets_sigpipe_to_its_default (SIG_IGN -> SIG_DFL)"
                )
            ],
        )
        self.assertNotIn("FAIL:", done.stderr)  # the next test saw SIG_IGN again

    def test_a_test_that_puts_them_back_in_a_cleanup_passes(self) -> None:
        done = self.run_probe("Restores")
        self.assertEqual((done.returncode, self.errors(done)), (0, []), done.stderr)

    def test_a_blocked_signal_fails_the_run(self) -> None:
        done = self.run_probe("Masks")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(
            self.errors(done),
            ["error: the signal mask changed and not restored by probe_signals.Masks.test_blocks_sigusr1 (+SIGUSR1)"],
        )

    def test_a_class_set_up_may_change_one_until_its_tear_down(self) -> None:
        done = self.run_probe("FixtureRestores")
        self.assertEqual((done.returncode, self.errors(done)), (0, []), done.stderr)

    def test_a_class_set_up_that_never_puts_it_back_fails_the_run(self) -> None:
        done = self.run_probe("FixtureLeaks")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertEqual(
            self.errors(done),
            [
                (
                    "error: signal SIGUSR2 changed and not restored by a class or module set-up or tear-down "
                    "before probe_signals.FixtureLeaks.test_runs (SIG_DFL -> SIG_IGN)"
                )
            ],
        )

    def test_workers_report_them_as_a_serial_run_does(self) -> None:
        for jobs in ("1", "2"):
            with self.subTest(jobs=jobs):
                done = self.run_probe("Leaks", "Masks", "Restores", options=("--jobs", jobs))
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertEqual(
                    sorted(self.errors(done)),
                    [
                        (
                            "error: signal SIGPIPE changed and not restored by "
                            "probe_signals.Leaks.test_a_sets_sigpipe_to_its_default (SIG_IGN -> SIG_DFL)"
                        ),
                        (
                            "error: the signal mask changed and not restored by "
                            "probe_signals.Masks.test_blocks_sigusr1 (+SIGUSR1)"
                        ),
                    ],
                    done.stderr,
                )


class ResourceWarningGateTest(unittest.TestCase):
    """A test that leaves a file or socket open fails the run, even though unittest says OK."""

    def run_probe(self, name: str, *options: str, cls: str = "T") -> subprocess.CompletedProcess[str]:
        """The runner in a child (with these interpreter options) on one test of _LEAKS (or the
        whole class, for an empty name)."""
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="cst-leak-run-"))
        with open(os.path.join(tmp, "probe_leaks.py"), "w", encoding="utf-8") as f:
            f.write(_LEAKS)
        return subprocess.run(
            [
                sys.executable,
                "-B",
                *options,
                os.path.join(REPO, "tests", "run_unit.py"),
                ".".join(part for part in ("probe_leaks", cls, name) if part),
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
            env={**os.environ, "PYTHONPATH": tmp},
        )

    def test_a_leaked_file_fails_the_run_and_names_the_test(self) -> None:
        done = self.run_probe("test_leaks_a_file")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("OK", done.stderr)  # unittest alone lets it pass
        self.assertIn(
            "error: ResourceWarning in probe_leaks.T.test_leaks_a_file: unclosed file <_io.TextIOWrapper "
            "name='/dev/null'",
            done.stderr,
        )
        self.assertIn(LEAK_HINT, done.stderr)

    def test_with_tracemalloc_the_output_says_where_it_was_opened(self) -> None:
        done = self.run_probe("test_leaks_a_file", "-X", "tracemalloc=8")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("Object allocated at (most recent call last):", done.stderr)
        self.assertIn("f = open(os.devnull)", done.stderr)

    def test_a_closed_file_passes(self) -> None:
        done = self.run_probe("test_closes_its_file")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("error:", done.stderr)

    def test_a_leak_only_the_last_collection_finds_fails_the_run(self) -> None:
        done = self.run_probe("test_leaks_a_file_in_a_cycle")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(
            "error: ResourceWarning in probe_leaks.T.test_leaks_a_file_in_a_cycle (or one before it: found by the "
            "last garbage collection): unclosed file",
            done.stderr,
        )

    def test_a_leak_a_collection_finds_names_the_running_test_or_one_before_it(self) -> None:
        done = self.run_probe("", cls="C")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(
            "error: ResourceWarning in probe_leaks.C.test_b_collects (or one before it: found by a garbage "
            "collection): unclosed file",
            done.stderr,
        )

    def test_a_leak_after_showwarning_was_replaced_fails_the_run(self) -> None:
        done = self.run_probe("test_replaces_showwarning_then_leaks", cls="S")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(
            "error: ResourceWarning in probe_leaks.S.test_replaces_showwarning_then_leaks: unclosed file", done.stderr
        )

    def test_a_leak_seen_by_a_showwarning_that_calls_the_original_is_one_line(self) -> None:
        done = self.run_probe("", cls="P")
        self.assertEqual(done.returncode, 1, done.stderr)
        error = [line for line in done.stderr.splitlines() if line.startswith("error: ")]
        self.assertEqual(len(error), 1, done.stderr)
        self.assertTrue(
            error[0].startswith(
                "error: ResourceWarning in "
                "probe_leaks.P.test_replaces_showwarning_with_one_that_calls_the_original_then_leaks: unclosed file"
            ),
            error,
        )

    def test_a_leak_after_a_test_that_ignored_every_warning_fails_the_run(self) -> None:
        done = self.run_probe("", cls="F")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("error: ResourceWarning in probe_leaks.F.test_b_leaks_a_file: unclosed file", done.stderr)

    def test_an_exception_python_could_only_print_fails_the_run_and_names_the_callback(self) -> None:
        done = self.run_probe("test_a_weakref_callback_raises", cls="U")
        self.assertEqual(done.returncode, 1, done.stderr)
        error = [line for line in done.stderr.splitlines() if line.startswith("error: ")]
        self.assertEqual(len(error), 1, done.stderr)
        self.assertTrue(
            error[0].startswith("error: exception in probe_leaks.U.test_a_weakref_callback_raises: "), error
        )
        self.assertIn("boom_callback", error[0])  # what Python was calling, whatever its words for it
        self.assertTrue(error[0].endswith(": ZeroDivisionError('boom')"), error)
        self.assertIn(LOST_EXCEPTION_HINT, done.stderr)
        self.assertNotIn(LEAK_HINT, done.stderr)

    def test_a_replaced_unraisable_hook_fails_the_run_and_later_exceptions_still_count(self) -> None:
        done = self.run_probe("", cls="H")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(
            "error: sys.unraisablehook replaced and not restored by probe_leaks.H.test_a_replaces_the_unraisable_hook",
            done.stderr,
        )
        self.assertIn("error: exception in probe_leaks.H.test_b_a_weakref_callback_raises: ", done.stderr)

    def test_a_class_set_up_may_replace_the_unraisable_hook_until_its_tear_down(self) -> None:
        done = self.run_probe("", cls="R")
        self.assertEqual(done.returncode, 0, done.stderr)  # its tests see their own hook
        self.assertNotIn("error:", done.stderr)

    def test_a_class_set_up_that_never_restores_the_unraisable_hook_fails_the_run(self) -> None:
        done = self.run_probe("", cls="Q")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(
            "error: sys.unraisablehook replaced and not restored by a class or module set-up or tear-down "
            "before probe_leaks.Q.test_runs",
            done.stderr,
        )

    def test_a_module_set_up_hook_left_in_place_is_found_at_the_next_module(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="cst-leak-run-"))
        for name, text in (
            ("probe_first.py", _HOOK_LEFT_BY_A_MODULE),
            ("probe_second.py", _LOSES_AN_EXCEPTION),
        ):
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write(text)
        done = subprocess.run(
            [sys.executable, "-B", os.path.join(REPO, "tests", "run_unit.py"), "probe_first", "probe_second"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
            env={**os.environ, "PYTHONPATH": tmp},
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        error = [line for line in done.stderr.splitlines() if line.startswith("error: ")]
        self.assertEqual(len(error), 2, done.stderr)
        self.assertIn(
            "error: sys.unraisablehook replaced and not restored by a class or module set-up or tear-down "
            "before probe_first.T.test_a",
            error,
        )
        # The runner's hook is back for the next module: its lost exception is recorded.
        self.assertTrue(
            any(
                line.startswith("error: exception in probe_second.T.test_a_weakref_callback_raises: ") for line in error
            ),
            error,
        )

    def test_a_warning_raised_inside_a_replaced_showwarning_is_one_line_each(self) -> None:
        done = self.run_probe("", cls="N")
        self.assertEqual(done.returncode, 1, done.stderr)
        error = [line for line in done.stderr.splitlines() if line.startswith("error: ")]
        self.assertEqual(len(error), 2, done.stderr)  # each warning once
        self.assertEqual(len([line for line in error if ": a second warning (" in line]), 1, error)
        self.assertEqual(len([line for line in error if ": unclosed file " in line]), 1, error)

    def test_a_leak_freed_between_two_tests_fails_the_run(self) -> None:
        done = self.run_probe("", cls="K")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(
            "error: ResourceWarning in probe_leaks.K.test_ignores_every_warning_and_keeps_a_file: unclosed file",
            done.stderr,
        )

    def test_a_warning_the_test_records_itself_is_its_own(self) -> None:
        done = self.run_probe("test_records_its_own_warning")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("error:", done.stderr)


# A test that starts a child Python without -B on a module that imports a sibling, and one that
# imports a sibling in its own process (the runner's flag keeps that one from writing bytecode).
_BYTECODE = """
import os, subprocess, sys, unittest
class T(unittest.TestCase):
    def test_a_child_without_b(self):
        here = os.path.dirname(os.path.abspath(__file__))
        subprocess.run([sys.executable, "-I", os.path.join(here, "child.py")], check=True, stdin=subprocess.DEVNULL)
    def test_imports_a_sibling(self):
        import sibling
        self.assertEqual(sibling.X, 1)
"""


class BytecodeGateTest(unittest.TestCase):
    def test_new_or_rewritten_bytecode_is_reported(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="cst-pyc-gate-"))

        def put(rel: str) -> None:
            os.makedirs(os.path.dirname(os.path.join(tmp, rel)), exist_ok=True)
            with open(os.path.join(tmp, rel), "wb"):
                pass

        put("a/__pycache__/old.cpython-314.pyc")
        put("a/__pycache__/rewritten.cpython-314.pyc")
        put(".git/objects/x.pyc")
        put("venv/pyvenv.cfg")
        put("venv/lib/__pycache__/v.cpython-314.pyc")
        # A checkout nested in this one (a linked worktree: its .git is a file; a clone: a folder).
        put(".claude/worktrees/w/.git")
        put(".claude/worktrees/w/__pycache__/w.cpython-314.pyc")
        put("clone/.git/HEAD")
        put("clone/a/__pycache__/c.cpython-314.pyc")
        before = bytecode_files(tmp)
        self.assertEqual(
            sorted(before), ["a/__pycache__/old.cpython-314.pyc", "a/__pycache__/rewritten.cpython-314.pyc"]
        )
        put("b/__pycache__/new.cpython-314.pyc")
        put("venv/lib/__pycache__/w.cpython-314.pyc")
        put(".git/y.pyc")
        put(".claude/worktrees/w/b/__pycache__/x.cpython-314.pyc")  # another checkout's run wrote it
        put("clone/b/__pycache__/d.cpython-314.pyc")
        rewritten = os.path.join(tmp, "a/__pycache__/rewritten.cpython-314.pyc")
        later = before["a/__pycache__/rewritten.cpython-314.pyc"] + 10**9
        os.utime(rewritten, ns=(later, later))
        self.assertEqual(
            new_bytecode(before, bytecode_files(tmp)),
            ["a/__pycache__/rewritten.cpython-314.pyc", "b/__pycache__/new.cpython-314.pyc"],
        )

    def run_copy(self, name: str) -> tuple[subprocess.CompletedProcess[str], str]:
        """A copy of the runner in a temporary checkout of its own, run by its shebang on one test of _BYTECODE."""
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="cst-pyc-run-"))
        os.makedirs(os.path.join(tmp, "tests"))
        shutil.copy2(os.path.join(REPO, "tests", "run_unit.py"), os.path.join(tmp, "tests", "run_unit.py"))
        for rel, text in (
            ("probe_bytecode.py", _BYTECODE),
            (
                "child.py",
                "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\nimport sibling\n",
            ),
            ("sibling.py", "X = 1\n"),
        ):
            with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                f.write(text)
        done = subprocess.run(
            [os.path.join(tmp, "tests", "run_unit.py"), f"probe_bytecode.T.{name}"],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
        return done, tmp

    @unittest.skipUnless(os.access("/usr/bin/python3", os.X_OK), "needs /usr/bin/python3")
    def test_a_child_that_writes_bytecode_fails_the_run(self) -> None:
        done, _ = self.run_copy("test_a_child_without_b")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("OK", done.stderr)  # unittest alone lets it pass
        self.assertRegex(
            done.stderr, r"error: a test wrote bytecode into the checkout: __pycache__/sibling\.cpython-\d+\.pyc"
        )
        self.assertIn(BYTECODE_HINT, done.stderr)

    @unittest.skipUnless(os.access("/usr/bin/python3", os.X_OK), "needs /usr/bin/python3")
    def test_the_runner_run_by_its_shebang_writes_no_bytecode_itself(self) -> None:
        done, tmp = self.run_copy("test_imports_a_sibling")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(bytecode_files(tmp), {})


class LoadTest(unittest.TestCase):
    def test_gate_modules_exist_and_load(self) -> None:
        for name in GATE_MODULES:
            with self.subTest(name=name):
                self.assertTrue(os.path.isfile(os.path.join(REPO, *name.split(".")) + ".py"))
        got = {i.rsplit(".", 2)[0] for i in ids(load(REPO, ["--gates"]))}
        self.assertEqual(got, set(GATE_MODULES))

    def test_names_load_only_those(self) -> None:
        got = ids(load(REPO, ["-v", "tests.test_run_unit.LoadTest.test_names_load_only_those"]))
        self.assertEqual(got, {"tests.test_run_unit.LoadTest.test_names_load_only_those"})

    def test_every_expected_skip_names_a_test(self) -> None:
        every = ids(load(REPO, []))
        stale = sorted(key for key in EXPECTED_SKIPS if not any(i == key or i.startswith(key + ".") for i in every))
        self.assertEqual(stale, [], "no such test (renamed or gone): update EXPECTED_SKIPS")

    def test_unexpected_skips_fail(self) -> None:
        self.assertTrue(expected_skip("vm.tests.test_vm.BtrfsReflinkTest.test_reflink_of_a_nocow_image"))
        self.assertFalse(expected_skip("vm.tests.test_vm.BtrfsReflinkTestOther.test_x"))
        self.assertEqual(
            skip_problems(
                [("vm.tests.test_vm.BtrfsReflinkTest.setUp", "x"), ("vm.tests.test_vm.PartsTest.test_a", "y")]
            ),
            ["vm.tests.test_vm.PartsTest.test_a: skipped (y), and not in EXPECTED_SKIPS of tests/run_unit.py"],
        )

    def test_gates_skip_outside_a_git_checkout(self) -> None:
        # A source tarball: repo_files() skips the gates, and the run accepts
        # exactly that skip there, never in a checkout that has a .git.
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="cst-no-git-"))
        with self.assertRaises(unittest.SkipTest) as cm:
            repo_files(tmp)
        skipped = [("tests.test_docs.RepositoryDocsTest.test_links", str(cm.exception))]
        self.assertEqual(skip_problems(skipped, git_checkout=False), [])
        self.assertEqual(len(skip_problems(skipped, git_checkout=True)), 1)
        self.assertEqual(len(skip_problems([("vm.tests.test_vm.PartsTest.test_a", "y")], git_checkout=False)), 1)

    def test_unknown_option_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            load(REPO, ["--gate"])


class ParseArgsAndJobsTest(unittest.TestCase):
    def test_parse_args_valid_flags(self) -> None:
        self.assertEqual(parse_args([]), RunOptions())
        self.assertEqual(
            parse_args(["-v", "--jobs", "4", "a.b"]),
            RunOptions(verbosity=2, jobs=4, gates=False, names=("a.b",)),
        )
        self.assertEqual(
            parse_args(["-j", "2", "--gates"]),
            RunOptions(verbosity=1, jobs=2, gates=True, names=()),
        )
        self.assertEqual(
            parse_args(["--jobs=0", "mod"]),
            RunOptions(verbosity=1, jobs=0, gates=False, names=("mod",)),
        )
        self.assertEqual(
            parse_args(["-v", "--worker-fds=3,4"]),
            RunOptions(verbosity=2, jobs=None, gates=False, names=(), worker_fds=(3, 4)),
        )
        self.assertEqual(
            parse_args(["--affected", "--base", "main"]),
            RunOptions(affected=True, base="main"),
        )
        self.assertEqual(
            parse_args(["--list-affected", "--base=HEAD~1"]),
            RunOptions(affected=True, list_affected=True, base="HEAD~1"),
        )

    def test_parse_args_rejects_bad_flags(self) -> None:
        for bad in (
            ["--gate"],
            ["--gates", "mod"],
            ["--jobs"],
            ["--jobs", "-1"],
            ["--jobs", "abc"],
            ["--worker-fds", "3"],
            ["--affected", "--gates"],
            ["--affected", "mod"],
            ["--list-affected", "mod"],
            ["--base", "main"],
            ["--affected", "--base"],
        ):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                parse_args(bad)

    def test_list_affected_and_affected_cli(self) -> None:
        list_done = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                os.path.join(REPO, "tests", "run_unit.py"),
                "--list-affected",
                "--base",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        self.assertEqual(list_done.returncode, 0, list_done.stderr)
        self.assertIn("Affected unit-test plan (base=HEAD):", list_done.stdout)

        bad_base = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                os.path.join(REPO, "tests", "run_unit.py"),
                "--affected",
                "--base",
                "nope-xyz",
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        self.assertEqual(bad_base.returncode, 2)
        self.assertIn("unknown git revision", bad_base.stderr)

    def test_default_jobs_falls_back_to_one_under_coverage(self) -> None:
        with (
            mock.patch("sys.gettrace", return_value=lambda *_a: None),
            mock.patch.object(sys.monitoring, "get_tool", return_value=None),
            mock.patch.dict(sys.modules, {"coverage": None}),
        ):
            self.assertEqual(default_jobs(), 1)
        with (
            mock.patch("sys.gettrace", return_value=None),
            mock.patch.object(sys.monitoring, "get_tool", return_value="coverage"),
            mock.patch.dict(sys.modules, {"coverage": None}),
        ):
            self.assertEqual(default_jobs(), 1)
        with (
            mock.patch("sys.gettrace", return_value=None),
            mock.patch.object(sys.monitoring, "get_tool", return_value=None),
            mock.patch.dict(sys.modules, {"coverage": types.ModuleType("coverage")}),
        ):
            self.assertEqual(default_jobs(), 1)
        with (
            mock.patch("sys.gettrace", return_value=None),
            mock.patch.object(sys.monitoring, "get_tool", return_value=None),
            mock.patch.dict(sys.modules, {"coverage": None}),
            mock.patch("os.process_cpu_count", return_value=8),
        ):
            self.assertEqual(default_jobs(), 8)

    def test_default_jobs_counts_the_cpus_of_the_affinity_not_of_the_machine(self) -> None:
        # A toolbox pinned to 16 of 24 CPUs: 24 workers would oversubscribe it.
        with (
            mock.patch("sys.gettrace", return_value=None),
            mock.patch.object(sys.monitoring, "get_tool", return_value=None),
            mock.patch.dict(sys.modules, {"coverage": None}),
        ):
            with mock.patch("os.cpu_count", return_value=24), mock.patch("os.process_cpu_count", return_value=16):
                self.assertEqual(default_jobs(), 16)
            with mock.patch("os.process_cpu_count", return_value=None):
                self.assertEqual(default_jobs(), 1)


class TestJobsEnvTest(unittest.TestCase):
    """CST_TEST_JOBS sets the default worker count; -j N wins over it.

    The runner's new names are reached through the module object, not imported at the top: on a
    runner without them only the tests that use them fail, and the wiring and CLI tests fail by
    assertion.
    """

    @override
    def setUp(self) -> None:
        # Coverage off and an affinity of 16 CPUs, as the default tests above patch them.
        self.enterContext(mock.patch("sys.gettrace", return_value=None))
        self.enterContext(mock.patch.object(sys.monitoring, "get_tool", return_value=None))
        self.enterContext(mock.patch.dict(sys.modules, {"coverage": None}))
        self.enterContext(mock.patch("os.process_cpu_count", return_value=16))

    def test_the_variable_sets_the_default_and_a_jobs_flag_wins(self) -> None:
        cases: list[tuple[int | None, dict[str, str], int]] = [
            (None, {}, 16),
            (None, {"CST_TEST_JOBS": ""}, 16),
            (None, {"CST_TEST_JOBS": "0"}, 16),
            (None, {"CST_TEST_JOBS": "6"}, 6),
            (None, {"CST_TEST_JOBS": "1"}, 1),
            (None, {"CST_TEST_JOBS": "06"}, 6),
            (None, {"CST_TEST_JOBS": "40"}, 40),
            (4, {"CST_TEST_JOBS": "6"}, 4),
            (0, {"CST_TEST_JOBS": "6"}, 6),
            (0, {}, 16),
            (4, {"CST_TEST_JOBS": "abc"}, 4),
        ]
        for cli, environ, want in cases:
            with self.subTest(cli=cli, environ=environ):
                self.assertEqual(run_unit_module.requested_jobs(cli, environ), want)

    def test_a_value_that_is_not_a_whole_number_is_a_usage_error(self) -> None:
        for raw in ("abc", "-1", "+3", "1.5", " 6", "6 ", "\N{SUPERSCRIPT TWO}"):
            for cli in (None, 0):
                with self.subTest(raw=raw, cli=cli), self.assertRaises(SystemExit) as caught:
                    run_unit_module.requested_jobs(cli, {"CST_TEST_JOBS": raw})
                text = str(caught.exception)
                self.assertIn("CST_TEST_JOBS", text)
                self.assertIn(repr(raw), text)

    def test_a_number_past_the_digits_int_converts_is_the_same_usage_error(self) -> None:
        raw = "9" * 5000  # int() refuses more than 4300 digits with a ValueError
        with self.assertRaises(SystemExit) as caught:
            run_unit_module.requested_jobs(None, {"CST_TEST_JOBS": raw})
        self.assertTrue(str(caught.exception).startswith("run_unit: invalid CST_TEST_JOBS value '999"))

    def test_coverage_wins_over_the_variable_and_a_jobs_flag_over_coverage(self) -> None:
        with mock.patch("sys.gettrace", return_value=lambda *_a: None):
            self.assertEqual(run_unit_module.requested_jobs(None, {"CST_TEST_JOBS": "6"}), 1)
            self.assertEqual(run_unit_module.requested_jobs(3, {"CST_TEST_JOBS": "6"}), 3)

    def test_under_coverage_the_variable_is_not_read(self) -> None:
        # The documented order puts coverage.py before CST_TEST_JOBS: a bad value there is no error.
        with mock.patch("sys.gettrace", return_value=lambda *_a: None):
            for cli in (None, 0):
                with self.subTest(cli=cli):
                    self.assertEqual(run_unit_module.requested_jobs(cli, {"CST_TEST_JOBS": "abc"}), 1)

    def test_a_run_with_no_jobs_flag_uses_the_variable(self) -> None:
        units = [(f"dummy.mod{i}",) for i in range(5)]
        with (
            mock.patch.dict(os.environ, {"CST_TEST_JOBS": "3"}),
            mock.patch.object(run_unit_module, "_load_options", return_value=unittest.TestSuite()),
            mock.patch.object(run_unit_module, "plan_work_units", return_value=(units, False)),
            mock.patch.object(run_unit_module, "_run_parallel", return_value=0) as parallel,
            mock.patch.object(run_unit_module, "_run_in_process", return_value=0) as in_process,
        ):
            self.assertEqual(run_unit_module._run(REPO, RunOptions()), 0)
        in_process.assert_not_called()
        parallel.assert_called_once()
        self.assertEqual(parallel.call_args.args[3], 3)

    def test_the_runner_stops_on_a_bad_value_before_any_test(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                os.path.join(REPO, "tests", "run_unit.py"),
                "tests.test_run_unit.ParseArgsAndJobsTest.test_parse_args_valid_flags",
            ],
            env={**os.environ, "CST_TEST_JOBS": "x"},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("invalid CST_TEST_JOBS value 'x'", proc.stderr)
        self.assertNotIn("Ran ", proc.stderr)


class PlanWorkUnitsTest(unittest.TestCase):
    def _register_dummy_module(self, mod_name: str, *, with_module_fixture: bool = False) -> types.ModuleType:
        mod = types.ModuleType(mod_name)
        if with_module_fixture:
            mod.__dict__["setUpModule"] = lambda: None
        sys.modules[mod_name] = mod
        self.addCleanup(sys.modules.pop, mod_name, None)
        return mod

    def test_respects_module_and_class_fixtures_and_chunks_plain_classes(self) -> None:
        mod_plain = "_cst_dummy_plain"
        mod_fix = "_cst_dummy_mod_fix"
        self._register_dummy_module(mod_plain, with_module_fixture=False)
        self._register_dummy_module(mod_fix, with_module_fixture=True)

        big_methods: dict[str, object] = {"__module__": mod_plain}
        for idx in range(25):
            big_methods[f"test_{idx:02d}"] = lambda _self: None
        big_cls = type("BigPlain", (unittest.TestCase,), big_methods)

        base_with_cls = type(
            "BaseFix",
            (unittest.TestCase,),
            {"__module__": mod_plain, "setUpClass": classmethod(lambda _cls: None)},
        )
        sub_methods: dict[str, object] = {"__module__": mod_plain}
        for idx in range(15):
            sub_methods[f"test_sub_{idx:02d}"] = lambda _self: None
        sub_cls = type("SubFix", (base_with_cls,), sub_methods)

        mod_fix_methods_a: dict[str, object] = {"__module__": mod_fix, "test_a": lambda _self: None}
        mod_fix_methods_b: dict[str, object] = {"__module__": mod_fix, "test_b": lambda _self: None}
        cls_a = type("ModFixA", (unittest.TestCase,), mod_fix_methods_a)
        cls_b = type("ModFixB", (unittest.TestCase,), mod_fix_methods_b)

        suite = unittest.TestSuite(
            [
                unittest.defaultTestLoader.loadTestsFromTestCase(big_cls),
                unittest.defaultTestLoader.loadTestsFromTestCase(sub_cls),
                unittest.defaultTestLoader.loadTestsFromTestCase(cls_a),
                unittest.defaultTestLoader.loadTestsFromTestCase(cls_b),
            ]
        )
        units, has_load_err = plan_work_units(suite, max_chunk=DEFAULT_CLASS_CHUNK)
        self.assertFalse(has_load_err)
        self.assertEqual([len(u) for u in units], [15, 9, 8, 8, 2])
        self.assertTrue(any(u[0].startswith(f"{mod_fix}.") for u in units if len(u) == 2))

    def test_detects_loader_errors(self) -> None:
        suite = unittest.defaultTestLoader.loadTestsFromNames(["_cst_no_such_module_xyz"])
        units, has_load_err = plan_work_units(suite)
        self.assertTrue(has_load_err)
        self.assertEqual(units, [])


class ParallelRunnerTest(unittest.TestCase):
    def _run_modules(self, files: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="cst-par-run-"))
        for name, text in files.items():
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        return subprocess.run(
            [sys.executable, "-B", os.path.join(REPO, "tests", "run_unit.py"), *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
            env={**os.environ, "PYTHONPATH": tmp},
        )

    def test_parallel_enforces_runtime_gates_and_aggregates_passes(self) -> None:
        probe_a = (
            "import os, shutil, stat, unittest\n"
            "class GatesA(unittest.TestCase):\n"
            "    def test_socket_stdin_no_new_privs_and_host_guard(self):\n"
            "        self.assertTrue(stat.S_ISSOCK(os.fstat(0).st_mode))\n"
            "        with open('/proc/self/status') as f:\n"
            "            self.assertEqual(next(l for l in f if l.startswith('NoNewPrivs:')).split()[1], '1')\n"
            f"        guard = os.environ.get({HOST_GUARD_ENV!r}, '')\n"
            "        self.assertTrue(guard)\n"
            "        self.assertEqual(shutil.which('systemctl'), os.path.join(guard, 'systemctl'))\n"
            "    def test_ok_two(self):\n"
            "        pass\n"
        )
        probe_b = (
            "import subprocess, unittest\n"
            "class GatesB(unittest.TestCase):\n"
            "    def test_bash_devnull(self):\n"
            "        r = subprocess.run(['bash', '-c', 'echo hi'], capture_output=True, text=True,"
            " stdin=subprocess.DEVNULL, check=False)\n"
            "        self.assertEqual(r.returncode, 0)\n"
        )
        done = self._run_modules(
            {"probe_pa.py": probe_a, "probe_pb.py": probe_b},
            "-v",
            "--jobs",
            "2",
            "probe_pa",
            "probe_pb",
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Ran 3 tests in", done.stderr)
        self.assertIn("(2 workers)", done.stderr)
        self.assertNotIn("error:", done.stderr)

    def test_parallel_reports_test_failures_and_unexpected_skips(self) -> None:
        probe_fail = (
            "import unittest\nclass F(unittest.TestCase):\n    def test_fail(self):\n        self.fail('boom')\n"
        )
        probe_skip = (
            "import unittest\n"
            "class S(unittest.TestCase):\n"
            "    @unittest.skip('bad skip')\n"
            "    def test_skip(self):\n"
            "        pass\n"
        )
        done = self._run_modules(
            {"probe_fail.py": probe_fail, "probe_skip.py": probe_skip},
            "--jobs",
            "2",
            "probe_fail",
            "probe_skip",
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("FAIL: test_fail (probe_fail.F.test_fail)", done.stderr)
        self.assertIn("FAILED (failures=1, skipped=1)", done.stderr)
        self.assertIn("not in EXPECTED_SKIPS of tests/run_unit.py", done.stderr)

    def test_parallel_catches_leaks_hooks_and_host_command_hits(self) -> None:
        probe_guard = (
            "import subprocess, unittest\n"
            "class G(unittest.TestCase):\n"
            "    def test_hits(self):\n"
            "        subprocess.run(['systemctl', 'start', 'x.service'], capture_output=True,"
            " stdin=subprocess.DEVNULL, check=False)\n"
        )
        done = self._run_modules(
            {"probe_leaks.py": _LEAKS, "probe_guard.py": probe_guard},
            "--jobs",
            "2",
            "probe_leaks.T.test_leaks_a_file",
            "probe_leaks.U.test_a_weakref_callback_raises",
            "probe_guard",
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("error: ResourceWarning in probe_leaks.T.test_leaks_a_file", done.stderr)
        self.assertIn(LEAK_HINT, done.stderr)
        self.assertIn("error: exception in probe_leaks.U.test_a_weakref_callback_raises", done.stderr)
        self.assertIn(LOST_EXCEPTION_HINT, done.stderr)
        self.assertIn("error: a unit test reached the real systemctl start x.service", done.stderr)

        hook_done = self._run_modules(
            {"probe_first.py": _HOOK_LEFT_BY_A_MODULE, "probe_second.py": _LOSES_AN_EXCEPTION},
            "--jobs",
            "2",
            "probe_first",
            "probe_second",
        )
        self.assertEqual(hook_done.returncode, 1, hook_done.stderr)
        self.assertIn(
            "error: sys.unraisablehook replaced and not restored by a class or module set-up or tear-down "
            "before probe_first.T.test_a",
            hook_done.stderr,
        )
        self.assertIn("error: exception in probe_second.T.test_a_weakref_callback_raises: ", hook_done.stderr)

    def test_parallel_detects_bytecode_written_into_copy(self) -> None:
        tmp = self.enterContext(tempfile.TemporaryDirectory(prefix="cst-par-pyc-"))
        os.makedirs(os.path.join(tmp, "tests"))
        shutil.copy2(os.path.join(REPO, "tests", "run_unit.py"), os.path.join(tmp, "tests", "run_unit.py"))
        for rel, text in (
            ("probe_bytecode.py", _BYTECODE),
            (
                "child.py",
                "import os, sys\nsys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\nimport sibling\n",
            ),
            ("sibling.py", "X = 1\n"),
        ):
            with open(os.path.join(tmp, rel), "w", encoding="utf-8") as fh:
                fh.write(text)
        done = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                os.path.join(tmp, "tests", "run_unit.py"),
                "--jobs",
                "2",
                "probe_bytecode.T.test_a_child_without_b",
                "probe_bytecode.T.test_imports_a_sibling",
            ],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertRegex(
            done.stderr, r"error: a test wrote bytecode into the checkout: __pycache__/sibling\.cpython-\d+\.pyc"
        )
        self.assertIn(BYTECODE_HINT, done.stderr)


if __name__ == "__main__":
    unittest.main()

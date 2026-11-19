# SPDX-License-Identifier: MPL-2.0
"""tests/run_unit.py: what it loads (all, --gates, names), and its run-time gates."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.repo_files import REPO, repo_files
from tests.run_unit import (
    EXPECTED_SKIPS,
    GATE_MODULES,
    HOST_COMMANDS,
    HOST_GUARD_ENV,
    HOST_GUARD_EXIT,
    LEAK_HINT,
    LOST_EXCEPTION_HINT,
    BYTECODE_HINT,
    bytecode_files,
    expected_skip,
    load,
    new_bytecode,
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
        before = bytecode_files(tmp)
        self.assertEqual(
            sorted(before), ["a/__pycache__/old.cpython-314.pyc", "a/__pycache__/rewritten.cpython-314.pyc"]
        )
        put("b/__pycache__/new.cpython-314.pyc")
        put("venv/lib/__pycache__/w.cpython-314.pyc")
        put(".git/y.pyc")
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


if __name__ == "__main__":
    unittest.main()

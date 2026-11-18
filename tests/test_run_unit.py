# SPDX-License-Identifier: MPL-2.0
"""tests/run_unit.py: what it loads (all, --gates, names)."""

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
    expected_skip,
    load,
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
        if shutil.which("sudo") and os.geteuid() != 0:
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

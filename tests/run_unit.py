#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""Run the unit tests with a socket as stdin (a run-time gate).

Use: python3 -I -B tests/run_unit.py [-v] [--gates | NAME...]   (from anywhere)

With no NAME it runs every unit test of the checkout. A NAME is a test
module, class or method as unittest names it (vm.tests.test_vm,
vm.tests.test_vm.PartsTest.test_disks_nvram_tpm): `invowk cmd test module`
runs one that way, so the socket gate below still applies. `--gates` runs
the static gate modules only (GATE_MODULES: `invowk cmd test arch`).

Skipped tests: the run lists every skipped test with its reason, and fails
on a skip that EXPECTED_SKIPS does not name. A skip there is one that
depends on the machine (a tool, root, the file system); any other skip
would turn a test off without anyone noticing. In a copy with no .git (a
source tarball) the gates that read the repository skip themselves
(tests/repo_files.py, NOT_A_CHECKOUT below), and those skips are expected.

bash reads ~/.bashrc even for `bash -c` when SHLVL is unset and stdin is a
socket: it takes that for a start by sshd/rshd. A test that runs bash with
a clean environment and an inherited stdin then passes in a terminal and
fails when an agent or a task runner starts it with a socket as stdin.

This runner makes that case happen on every run: it puts one end of a
socket pair on file descriptor 0 before the tests start, so every child
process inherits a socket. A test that forgets stdin=subprocess.DEVNULL
fails here every time, not only in some environments. The static side of
the same rule is tests/test_subprocess_hygiene.py; the reason is written
down in AGENTS.md.

No test may reach the real machine's daemons either. From a toolbox,
systemctl, busctl, loginctl and systemd-run talk D-Bus to the host's
systemd, flatpak-spawn runs a command on the host and virsh talks to the
host's libvirt; no_new_privs (below) stops none of them. A test once
reached the host's systemd that way: a helper thread outlived the patch of
subprocess.Popen that was meant to catch its call. Before the tests start
this runner writes a refusing stub for each of HOST_COMMANDS into a
temporary folder and puts that folder first on PATH, for the runner and
every child that inherits its environment. Such a call then prints "unit
test reached the real systemctl" and exits 97, and the test fails instead
of changing the machine. Each stub also writes its command line down, and
the run fails when one ran, even if the test ignored the answer (a helper
thread whose result nobody reads). A test that sets a PATH of its own (a
folder of fakes, or one without the command) is not affected.

No test may reach sudo (or any setuid program that would make it root):
where sudo needs no password (a toolbox) such a test would change the real
machine and still pass. Before the tests start this runner sets the
kernel's no_new_privs flag (prctl PR_SET_NO_NEW_PRIVS) on itself, which
every child inherits and no child can clear: a setuid program then runs
without its privileges, and sudo refuses to start. bwrap (the mutation
test's sandbox) works under it; it sets the same flag itself.

No test may leave a file, a socket or a child process open. Python finds
such a leak when the object is freed and only prints a ResourceWarning
(an exception raised in a __del__, a weakref callback or a generator
closed when it is freed is only printed too), so the test still passes; a
daemon that leaks a descriptor per event fails slowly. This runner
records every ResourceWarning (one that goes to a warnings.showwarning a
test replaced too, and one after a test that changed the warning filters
for good: each test starts with them recording again) and every exception
Python could only print (sys.unraisablehook) with the test that was
running (LeakRecordingResult; a leak the garbage collector frees, which
an earlier test may have made, is named with "or one before it"), runs
the garbage collector once more after the last test, and fails the run
on each one, with a hint: for a
ResourceWarning, run the test again with -X tracemalloc=8 to see where the
resource was opened; for an exception, its traceback is printed above the
test results. A test that records warnings itself
(warnings.catch_warnings(record=True)) keeps them. Code that runs in a
child process (a helper started with python3 -I) is not seen.

No test may write bytecode into the checkout. `python3 -I` ignores
PYTHONDONTWRITEBYTECODE, so a child Python started on the checkout's code
without -B writes __pycache__ folders there (root's, when the code runs as
root). This runner sets sys.dont_write_bytecode for itself first (run by its
shebang it has no -B), notes every .pyc of the checkout before the tests
(.git and virtual environments left out), and fails the run on each one that
is new or rewritten after them, with a hint (BYTECODE_HINT).
"""

from collections.abc import Callable, Sequence
import ctypes
import gc
import os
import shlex
import shutil
import socket
import sys
import tempfile
from typing import Final, override
import unittest
from unittest import mock
import warnings

# The static gates: tests that read the repository (layers, budgets,
# entry points, the manifest, the Python and shell lists, docs, license
# headers, the workflow audit's settings), not the behaviour of a module.
# Seconds instead of minutes.
GATE_MODULES: Final = (
    "tests.test_architecture",
    "tests.test_bytecode",
    "tests.test_distribution",
    "tests.test_docs",
    "tests.test_duplication",
    "tests.test_entrypoints",
    "tests.test_fix_budgets",
    "tests.test_license_headers",
    "tests.test_magic_values",
    "tests.test_module_size",
    "tests.test_python_files",
    "tests.test_repo_paths",
    "tests.test_ruff_budget",
    "tests.test_run_unit",
    "tests.test_subprocess_hygiene",
    "tests.test_typing_budget",
    "tests.test_unit_contract",
    "tests.test_workflow_audit",
)


# The root routes of the setup scripts run in a user namespace (unshare -r).
_USERNS: Final = (
    "needs an unprivileged user namespace (unshare -r: off in CI's container; tier 1 test_73 runs it as root)"
)

# Skips that depend on the machine the tests run on: test id (or the id of
# its class) -> why. tests/test_run_unit.py checks that each one still names
# a test.
EXPECTED_SKIPS: Final[dict[str, str]] = {
    "bluetooth.tests.test_bluetooth.WirePlumberResolveTest": "needs libwireplumber-0.5 (not in CI, not in a toolbox)",
    "bluetooth.tests.test_setup.StepDownTest.test_the_test_overrides_do_not_reach_the_run_as_the_user": _USERNS,
    "btrfs.tests.test_btrfs_bulk.BulkTest.test_root_reads_bulk_user_with_the_one_settings_reader": "not as root",
    "btrfs.tests.test_btrfs_snapshot_core.BtrbkOracleTests": "needs btrbk (perl) to compare with",
    "btrfs.tests.test_snapshot_hourly.CommandLineTest.test_as_root_the_test_seam_is_ignored": (
        "needs an unprivileged user namespace (unshare -r: off in CI's container; tier 1 test_108 runs it as root)"
    ),
    "btrfs.tests.test_btrfs_snapshot_core.CacheWalkTests.test_drop_failure_keeps_the_tag_and_goes_on": "not as root",
    "btrfs.tests.test_btrfs_steam.RealBtrfsSteamTest": "opt-in, needs a real btrfs scratch folder",
    "tests.mutation.tests.test_cst_mutation.RealBwrapTest": "needs bwrap and a Python outside the hidden folders",
    "tests.test_cst_core_shared.ShellAssignmentsTest.test_every_value_is_what_bash_sets": "needs /usr/bin/bash",
    "tests.test_cst_core_shared.ShellAssignmentsTest.test_the_files_cst_writes_and_ships_have_no_problem": (
        "needs /usr/bin/bash"
    ),
    "tests.test_distribution.DistributionGateTest.test_python_entries_run_their_help_from_root_without_the_checkout": (
        "needs /usr/bin/python3"
    ),
    "tests.test_distribution.DistributionGateTest.test_an_installed_entry_finds_its_siblings_at_their_installed_dests": (
        "needs /usr/bin/python3"
    ),
    "tests.test_install.InstallerCliTest": "needs /usr/bin/python3; one test not as root",
    "tests.test_install.BashLibraryTest": "needs /usr/bin/python3",
    "tests.test_install.LocalTargetTest.test_a_write_that_fails_names_the_destination": "not as root",
    "tests.test_module_routing.ToolchainsRoutingTest.test_as_root_it_runs_again_as_the_sudo_user": _USERNS,
    "tests.test_module_routing.ToolchainsRoutingTest.test_as_root_without_a_sudo_user_it_refuses": _USERNS,
    "tests.test_module_routing.VmRoutingTest.test_as_root_the_program_runs_directly": _USERNS,
    "toolchains.tests.test_prune_rust_build.ScanRobustTest.test_unreadable_top_folder_is_reported": "not as root",
    "toolchains.tests.test_prune_rust_build.CargoTieTest.test_real_cargo_metadata": "needs cargo (not in CI)",
    "toolchains.tests.test_rules.OrphanTest.test_real_cargo_names_the_same_folder": "needs cargo and rustup (not in CI)",
    "toolchains.tests.test_rules.SccacheConfigTest.test_status_asks_each_running_server": (
        "the temporary folder's path is too long for a unix socket"
    ),
    "toolchains.tests.test_toolchains.ConfigTest.test_write_error_is_reported_and_the_other_setting_still_written": (
        "not as root"
    ),
    "vm.tests.test_vm.BackupTest.test_needs_root": "depends on whether the run is root",
    "vm.tests.test_vm.BtrfsReflinkTest": "needs ~/.cache on btrfs, with NOCOW settable",
}


def expected_skip(test_id: str) -> bool:
    """The test, or its class, is in EXPECTED_SKIPS."""
    return any(test_id == key or test_id.startswith(key + ".") for key in EXPECTED_SKIPS)


# The reason tests/repo_files.py gives when the checkout has no .git (a
# source tarball): the gates that read the repository have nothing to read.
NOT_A_CHECKOUT: Final = "not a git checkout: "


def skip_problems(skipped: Sequence[tuple[str, str]], *, git_checkout: bool = True) -> list[str]:
    """The skips (test id, reason) EXPECTED_SKIPS does not name, as text lines.

    git_checkout: the checkout has a .git. Without one, the gates'
    NOT_A_CHECKOUT skips are expected too; with one, git can only fail
    (repo_files raises), so such a skip there is not let through.
    """
    return [
        f"{test_id}: skipped ({reason}), and not in EXPECTED_SKIPS of tests/run_unit.py"
        for test_id, reason in skipped
        if not expected_skip(test_id) and (git_checkout or not reason.startswith(NOT_A_CHECKOUT))
    ]


# prctl(2): the option that sets the no_new_privs flag (linux/prctl.h).
PR_SET_NO_NEW_PRIVS: Final = 38


def forbid_privilege_gain() -> None:
    """Set no_new_privs on this process (and so on every child): sudo and the
    other setuid programs can no longer make a test root. Raises OSError
    when the kernel refuses: a run that could reach sudo does not start."""
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"prctl(PR_SET_NO_NEW_PRIVS): {os.strerror(err)}")


# The commands that reach a daemon of the real machine (systemd, logind,
# the host through flatpak-spawn, libvirt): a unit test only ever runs a fake of them.
HOST_COMMANDS: Final = ("busctl", "flatpak-spawn", "loginctl", "systemctl", "systemd-run", "virsh")
# The exit status of a stub (no command uses it for an answer of its own).
HOST_GUARD_EXIT: Final = 97
# Names the stubs' folder in the environment of the run (tests/test_run_unit.py checks it).
HOST_GUARD_ENV: Final = "CST_UNIT_HOST_GUARD"
# The file each stub adds its command line to; the runner fails a run that left one.
HOST_GUARD_HITS: Final = "hits"
_STUB: Final = """#!/bin/sh
echo "unit test reached the real {name} (refused by tests/run_unit.py): give the test a fake, or a PATH of its own" >&2
printf '%s\\n' "{name} $*" >> {hits}
exit {code}
"""


def guard_host_commands(folder: str) -> None:
    """Write a refusing stub for each of HOST_COMMANDS into folder, then put
    folder first on this process's PATH (so on every child's that inherits
    it) and name it in HOST_GUARD_ENV. A stub that runs adds its command
    line to folder/HOST_GUARD_HITS."""
    for name in HOST_COMMANDS:
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                _STUB.format(name=name, code=HOST_GUARD_EXIT, hits=shlex.quote(os.path.join(folder, HOST_GUARD_HITS)))
            )
        os.chmod(path, 0o755)
    os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", os.defpath)
    os.environ[HOST_GUARD_ENV] = folder


# What the run prints after the leaks it found: how to see where each one was opened.
LEAK_HINT: Final = (
    "a file, socket or other resource was left open; "
    "run the test again with python3 -I -B -X tracemalloc=8 tests/run_unit.py NAME to see where it was opened"
)
# What the run prints after the exceptions Python could only print (sys.unraisablehook).
LOST_EXCEPTION_HINT: Final = (
    "an exception was raised where Python can only print it (in a __del__, a weakref callback, "
    "a generator closed when it was freed, ...): its traceback is printed above the test results"
)


# What the run prints after the bytecode a test wrote into the checkout.
BYTECODE_HINT: Final = (
    "a test started Python on the checkout's code without -B (python3 -I ignores PYTHONDONTWRITEBYTECODE), "
    "or a program without the bootstrap of tests/test_entrypoints.py: pass -B"
)


def bytecode_files(root: str) -> dict[str, int]:
    """The .pyc files under root (path relative to root -> mtime in ns), .git and virtual
    environments (a folder that holds pyvenv.cfg) left out."""
    found: dict[str, int] = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d != ".git" and not os.path.exists(os.path.join(folder, d, "pyvenv.cfg"))]
        for name in names:
            if name.endswith(".pyc"):
                path = os.path.join(folder, name)
                try:
                    found[os.path.relpath(path, root)] = os.stat(path).st_mtime_ns
                except FileNotFoundError:  # removed while we walked
                    continue
    return found


def new_bytecode(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """The .pyc files of after that were not in before, or were rewritten since."""
    return sorted(path for path, mtime in after.items() if before.get(path) != mtime)


# The warnings module's functions that write one warning (WarningMessage) out: _showwarnmsg is the
# one Python calls; it hands the warning to warnings.showwarning when that was replaced (and its
# original is _showwarning_orig), and to _showwarnmsg_impl otherwise.
_SHOW_HOOK: Final = "_showwarnmsg_impl"
_SHOW_ENTRY: Final = "_showwarnmsg"
_SHOWWARNING_ORIG: Final = "_showwarning_orig"


class LeakRecordingResult(unittest.TextTestResult):
    """A TextTestResult that also records every ResourceWarning (a file, socket or child process
    left open, found when it is freed; in leaks) and every exception Python could only print
    (sys.unraisablehook: raised in a __del__, a weakref callback, ...; in lost_exceptions), with
    the test that was running. unittest only prints them, and such a test still passes; the
    runner fails the run on each one. A test that records warnings itself
    (warnings.catch_warnings with record=True) keeps them: the hook does not see those."""

    leaks: list[str]
    lost_exceptions: list[str]

    @override
    def startTestRun(self) -> None:
        super().startTestRun()
        self.leaks = []
        self.lost_exceptions = []
        self._running = "before the first test"
        # A leak the garbage collector frees (an object in a reference cycle) may come from any
        # test before the one running: _where says so (gc.callbacks tell when it collects).
        self._collecting = False
        self._found_by = "a garbage collection"
        gc.callbacks.append(self._gc_phase)
        # TextTestRunner.run has set its own filter ("default") inside a catch_warnings block,
        # which also restores showwarning when the run ends.
        warnings.simplefilter("always", ResourceWarning)
        self._always = warnings.filters[0]
        # The hook below warnings.showwarning: it still gets the warning's source, so the
        # original prints where the object was allocated (with -X tracemalloc), and a test's
        # warnings.catch_warnings(record=True) replaces it for the test's own warnings.
        self._show: Callable[[warnings.WarningMessage], None] = getattr(warnings, _SHOW_HOOK)
        # A replaced warnings.showwarning (mock.patch, logging.captureWarnings) gets the warning
        # instead, and _showwarnmsg_impl never sees it: the hook on _showwarnmsg records those.
        self._show_entry: Callable[[warnings.WarningMessage], None] = getattr(warnings, _SHOW_ENTRY)
        self._showwarning_orig: object = getattr(warnings, _SHOWWARNING_ORIG)
        self._hooks = (
            mock.patch.object(warnings, _SHOW_HOOK, self._warned),
            mock.patch.object(warnings, _SHOW_ENTRY, self._entered),
        )
        for hook in self._hooks:
            hook.start()
        self._unraisablehook = sys.unraisablehook
        sys.unraisablehook = self._unraisable

    @override
    def startTest(self, test: unittest.TestCase) -> None:
        self._running = test.id()
        self._keep_recording()
        super().startTest(test)

    @override
    def stopTestRun(self) -> None:
        # An object in a reference cycle is freed only by the collector: run it now, while the
        # hooks still record, instead of at exit.
        self._found_by = "the last garbage collection"
        self._keep_recording()
        gc.collect()
        gc.callbacks.remove(self._gc_phase)
        for hook in reversed(self._hooks):
            hook.stop()
        sys.unraisablehook = self._unraisablehook
        super().stopTestRun()

    def _keep_recording(self) -> None:
        """Put the "always" filter for ResourceWarning first again. A test that called
        warnings.simplefilter("ignore") or warnings.resetwarnings() outside a catch_warnings block
        would otherwise hide the leaks of every test after it."""
        if warnings.filters[:1] != [self._always]:
            warnings.simplefilter("always", ResourceWarning)

    def _gc_phase(self, phase: str, _info: dict[str, int]) -> None:
        self._collecting = phase == "start"

    def _where(self) -> str:
        """The test that was running, and when the garbage collector freed the object, that it
        may have been opened by an earlier one."""
        if self._collecting:
            return f"{self._running} (or one before it: found by {self._found_by})"
        return self._running

    def _entered(self, msg: warnings.WarningMessage) -> None:
        if warnings.showwarning is not self._showwarning_orig:
            self._record(msg)  # the replacement gets it, not _warned
        self._show_entry(msg)

    def _warned(self, msg: warnings.WarningMessage) -> None:
        self._record(msg)
        self._show(msg)

    def _record(self, msg: warnings.WarningMessage) -> None:
        if issubclass(msg.category, ResourceWarning):
            self.leaks.append(f"ResourceWarning in {self._where()}: {msg.message} ({msg.filename}:{msg.lineno})")

    def _unraisable(self, unraisable: sys.UnraisableHookArgs) -> None:
        # err_msg says what Python was doing (since 3.14 the object is often None and only err_msg
        # names the __del__ or callback); None means the default "Exception ignored in" the object.
        what = unraisable.err_msg or f"Exception ignored in {unraisable.object!r}"
        self.lost_exceptions.append(f"exception in {self._where()}: {what}: {unraisable.exc_value!r}")
        self._unraisablehook(unraisable)


class LeakRecordingRunner(unittest.TextTestRunner):
    """unittest's TextTestRunner with a LeakRecordingResult."""

    @override
    def _makeResult(self) -> LeakRecordingResult:
        return LeakRecordingResult(self.stream, self.descriptions, self.verbosity, durations=self.durations)


def load(repo: str, args: Sequence[str]) -> unittest.TestSuite:
    """The tests to run: every test (no name), the static gates (--gates) or the named ones."""
    names = [a for a in args if a != "-v"]
    if names == ["--gates"]:
        return unittest.defaultTestLoader.loadTestsFromNames(GATE_MODULES)
    if any(n.startswith("-") for n in names):
        raise SystemExit(f"run_unit: unknown option in {' '.join(names)} (use -v, --gates or test names)")
    if names:
        return unittest.defaultTestLoader.loadTestsFromNames(names)
    return unittest.defaultTestLoader.discover(repo)


def main(argv: Sequence[str]) -> int:
    sys.dont_write_bytecode = True  # run by its shebang (no -B): the test modules it imports write none
    verbosity = 2 if "-v" in argv else 1
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    forbid_privilege_gain()
    guard = tempfile.mkdtemp(prefix="cst-unit-host-guard-")
    try:
        guard_host_commands(guard)
        rc = _run(repo, argv, verbosity)
        hits = guard_hits(guard)
        for line in hits:
            print(f"error: a unit test reached the real {line}", file=sys.stderr)
        return 1 if hits else rc
    finally:
        shutil.rmtree(guard, ignore_errors=True)


def guard_hits(folder: str) -> list[str]:
    """The command lines the stubs in folder refused (a test that ignored the refusal still fails the run)."""
    try:
        with open(os.path.join(folder, HOST_GUARD_HITS), encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def _run(repo: str, argv: Sequence[str], verbosity: int) -> int:
    ours, theirs = socket.socketpair()
    os.dup2(theirs.fileno(), 0)  # fd 0 is now a socket, inherited by every child
    theirs.close()
    sys.stdin = open(0, closefd=False)
    bytecode_before = bytecode_files(repo)
    suite = load(repo, argv)
    result = LeakRecordingRunner(verbosity=verbosity).run(suite)
    ours.close()
    bytecode = [
        f"a test wrote bytecode into the checkout: {path}"
        for path in new_bytecode(bytecode_before, bytecode_files(repo))
    ]
    if not isinstance(result, LeakRecordingResult):  # its _makeResult makes one: never silently no leaks
        raise TypeError(f"the runner made a {type(result).__name__}, not a LeakRecordingResult")
    skipped = [(test.id(), reason) for test, reason in result.skipped]
    for test_id, reason in skipped:
        print(f"skipped: {test_id}: {reason}", file=sys.stderr)
    problems = skip_problems(skipped, git_checkout=os.path.exists(os.path.join(repo, ".git")))
    leaks, lost = result.leaks, result.lost_exceptions
    for line in [*problems, *leaks, *lost, *bytecode]:
        print(f"error: {line}", file=sys.stderr)
    if leaks:
        print(LEAK_HINT, file=sys.stderr)
    if lost:
        print(LOST_EXCEPTION_HINT, file=sys.stderr)
    if bytecode:
        print(BYTECODE_HINT, file=sys.stderr)
    return 0 if result.wasSuccessful() and not problems and not leaks and not lost and not bytecode else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

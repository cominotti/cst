#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""Run the unit tests in parallel across CPU cores with a socket as stdin (run-time gates).

Use: python3 -I -B tests/run_unit.py [-v] [-j N | --jobs N] [--gates | --affected | --list-affected] [--base REF] [NAME...]   (from anywhere)

With no NAME it runs every unit test of the checkout across parallel worker
processes (`default_jobs()`, 1 per CPU; automatically 1 under `coverage.py`).
A NAME is a test module, class or method as unittest names it (vm.tests.test_vm,
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
child process (a helper started with python3 -I) is not seen. The filters
are put back at the end of each test too, so an object freed after a test
that changed them for good, before the next one starts, is still recorded.
A test that replaces sys.unraisablehook and does not restore it fails the
run, and the runner's hook is put back. A class or module set-up may
replace it until its tear-down; one its tear-down does not restore fails
the run, found when the next module starts (the runner's hook is put back
then) or after the last tear-down.

A test runs in the same process as the tests after it (a worker runs many),
so a signal disposition it changes (signal.signal: SIGPIPE back to SIG_DFL,
a handler of its own) or a signal it blocks (signal.pthread_sigmask) changes
every later test of that process, and one of them fails only in some orders.
The runner notes every disposition and the main thread's mask when each test
starts and compares them when it ends (after its cleanups): each difference
fails the run, naming the test, the signal and the change ("signal SIGPIPE
changed and not restored by NAME (SIG_IGN -> SIG_DFL)"), and the runner puts
it back. A class or module set-up may change one until its tear-down; one
left changed when the run (or a worker's batch) ends fails it, named by the
first test that started with it. Put it back in the test: self.addCleanup.

No test may write bytecode into the checkout. `python3 -I` ignores
PYTHONDONTWRITEBYTECODE, so a child Python started on the checkout's code
without -B writes __pycache__ folders there (root's, when the code runs as
root). This runner sets sys.dont_write_bytecode for itself first (run by its
shebang it has no -B), notes every .pyc of the checkout before the tests
(.git, virtual environments and other checkouts nested in it, such as an
agent's worktree under .claude/worktrees, left out), and fails the run on
each one that is new or rewritten after them, with a hint (BYTECODE_HINT).
"""

from collections.abc import Callable, Sequence
import concurrent.futures
import contextlib
import ctypes
from dataclasses import dataclass, field
import gc
import io
import json
import os
import queue
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import FrameType
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
    "doctor.tests.test_doctor.SwapSettingsDoctorTest.test_an_older_cst_set_it_up": "not as root",
    "doctor.tests.test_doctor.SwapSettingsDoctorTest.test_a_swap_conf_this_user_cannot_read": "not as root",
    "doctor.tests.test_doctor.SwapSettingsDoctorTest.test_a_swap_conf_in_a_folder_this_user_may_not_enter": "not as root",
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


def _not_ours(folder: str) -> bool:
    """A folder under the checkout whose bytecode is not this run's: .git, a virtual environment
    (it holds pyvenv.cfg), or another checkout nested in it (it holds a .git: a folder for a clone,
    a file for a linked worktree, such as an agent's under .claude/worktrees)."""
    return os.path.basename(folder) == ".git" or any(
        os.path.lexists(os.path.join(folder, name)) for name in ("pyvenv.cfg", ".git")
    )


def bytecode_files(root: str) -> dict[str, int]:
    """The .pyc files under root (path relative to root -> mtime in ns), the folders _not_ours
    names left out."""
    found: dict[str, int] = {}
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not _not_ours(os.path.join(folder, d))]
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


type _UnraisableHook = Callable[[sys.UnraisableHookArgs], object]

# A signal's disposition as signal.getsignal gives it: SIG_DFL or SIG_IGN, a Python handler, an int,
# or None for a handler set outside Python.
type _Disposition = Callable[[int, FrameType | None], object] | int | signal.Handlers | None


@dataclass(frozen=True, slots=True)
class SignalState:
    """What a test can change for every later test of its process: each signal's disposition, and the
    main thread's mask of blocked signals."""

    dispositions: dict[int, _Disposition]
    mask: frozenset[int]


def signal_state() -> SignalState:
    """The dispositions and the mask now (a signal getsignal refuses is left out)."""
    dispositions: dict[int, _Disposition] = {}
    for signum in signal.valid_signals():
        with contextlib.suppress(ValueError, OSError):
            dispositions[signum] = signal.getsignal(signum)
    return SignalState(dispositions, frozenset(signal.pthread_sigmask(signal.SIG_BLOCK, ())))


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:  # a real-time signal other than SIGRTMIN and SIGRTMAX
        return f"SIGRTMIN+{signum - signal.SIGRTMIN}"


def _disposition_text(disposition: _Disposition) -> str:
    match disposition:
        case signal.Handlers():
            return disposition.name
        case None:
            return "a handler set outside Python"
        case int():
            return str(disposition)
        case _:
            return str(getattr(disposition, "__qualname__", repr(disposition)))


def signal_changes(before: SignalState, after: SignalState) -> list[tuple[str, str]]:
    """(what, how) for each change from before to after: ("signal SIGPIPE", "SIG_IGN -> SIG_DFL"), and
    ("the signal mask", "+SIGUSR1") for the mask."""
    changes = [
        (f"signal {_signal_name(n)}", f"{_disposition_text(old)} -> {_disposition_text(after.dispositions.get(n))}")
        for n, old in before.dispositions.items()
        if after.dispositions.get(n) != old
    ]
    if after.mask != before.mask:
        added = [f"+{_signal_name(n)}" for n in sorted(after.mask - before.mask)]
        removed = [f"-{_signal_name(n)}" for n in sorted(before.mask - after.mask)]
        changes.append(("the signal mask", " ".join(added + removed)))
    return changes


def restore_signal_state(state: SignalState) -> None:
    """Put the dispositions (those set from Python) and the mask of state back."""
    current = signal_state()
    for signum, disposition in state.dispositions.items():
        if disposition is not None and current.dispositions.get(signum) != disposition:
            signal.signal(signum, disposition)
    if current.mask != state.mask:
        signal.pthread_sigmask(signal.SIG_SETMASK, state.mask)


class LeakRecordingResult(unittest.TextTestResult):
    """A TextTestResult that also records every ResourceWarning (a file, socket or child process
    left open, found when it is freed; in leaks) and every exception Python could only print
    (sys.unraisablehook: raised in a __del__, a weakref callback, ...; in lost_exceptions), with
    the test that was running. unittest only prints them, and such a test still passes; the
    runner fails the run on each one. A test that records warnings itself
    (warnings.catch_warnings with record=True) keeps them: the hook does not see those. A signal
    disposition or the signal mask that a test, or a class or module fixture, leaves changed is
    one error in signal_problems, and the runner puts it back."""

    leaks: list[str]
    lost_exceptions: list[str]
    hook_problems: list[str]
    signal_problems: list[str]

    @override
    def startTestRun(self) -> None:
        super().startTestRun()
        self.leaks = []
        self.lost_exceptions = []
        self.hook_problems = []
        self.signal_problems = []
        # Each test must end with the signals it started with; a class or module set-up may change
        # them until its tear-down, so the run's own are compared once more when it ends.
        self._signals_at_run = signal_state()
        self._signals_at_test = self._signals_at_run
        self._signal_fixture_where = ""
        self._in_replaced = False
        # A class or module set-up may replace sys.unraisablehook until its tear-down: the one
        # found in place at a startTest (its first test's), where, and in which module.
        self._fixture_hook: _UnraisableHook | None = None
        self._fixture_where = ""
        self._fixture_module = ""
        self._hook_at_start: _UnraisableHook = self._unraisable
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
        self._note_fixture_hook(test)
        self._signals_at_test = signal_state()
        if self._signals_at_test == self._signals_at_run:
            self._signal_fixture_where = ""
        elif not self._signal_fixture_where:
            self._signal_fixture_where = f"a class or module set-up or tear-down before {test.id()}"
        self._hook_at_start = sys.unraisablehook
        self._running = test.id()
        self._keep_recording()
        super().startTest(test)

    @override
    def stopTest(self, test: unittest.TestCase) -> None:
        # The suite drops the test case after this: what it held is freed before the next startTest.
        if sys.unraisablehook != self._hook_at_start:  # the test itself replaced it
            self.hook_problems.append(f"sys.unraisablehook replaced and not restored by {test.id()}")
            sys.unraisablehook = self._hook_at_start
        self._check_signals(test.id(), self._signals_at_test)
        self._keep_recording()
        super().stopTest(test)

    @override
    def stopTestRun(self) -> None:
        # An object in a reference cycle is freed only by the collector: run it now, while the
        # hooks still record, instead of at exit.
        self._found_by = "the last garbage collection"
        if sys.unraisablehook != self._unraisable:  # every tear-down has run
            where = self._fixture_where or "a class or module tear-down after the last test"
            self.hook_problems.append(f"sys.unraisablehook replaced and not restored by {where}")
            sys.unraisablehook = self._unraisable
        where = self._signal_fixture_where or "a class or module set-up or tear-down after the last test"
        self._check_signals(where, self._signals_at_run)
        self._keep_recording()
        gc.collect()
        gc.callbacks.remove(self._gc_phase)
        for hook in reversed(self._hooks):
            hook.stop()
        sys.unraisablehook = self._unraisablehook
        super().stopTestRun()

    def _check_signals(self, where: str, expected: SignalState) -> None:
        """One error per signal (and for the mask) that differs from expected, and put it back, so the
        rest of the process runs as before."""
        changes = signal_changes(expected, signal_state())
        for what, how in changes:
            self.signal_problems.append(f"{what} changed and not restored by {where} ({how})")
        if changes:
            restore_signal_state(expected)

    def _keep_recording(self) -> None:
        """Put the "always" filter for ResourceWarning first again. A test that called
        warnings.simplefilter("ignore") or warnings.resetwarnings() outside a catch_warnings block
        would otherwise hide the leaks of every test after it."""
        if warnings.filters[:1] != [self._always]:
            warnings.simplefilter("always", ResourceWarning)

    def _note_fixture_hook(self, test: unittest.TestCase) -> None:
        """Before a test: note a sys.unraisablehook that a class or module set-up put in place
        (its tear-down may restore it, so it is no error yet). The same hook still in place when
        the next module starts is one a tear-down did not restore: an error, and the runner's
        hook is put back. stopTestRun reports one still in place after the last tear-down."""
        hook, module = sys.unraisablehook, type(test).__module__
        if hook == self._unraisable:  # a bound method: equal, not the same object
            self._fixture_hook = None
            self._fixture_where = ""
            return
        if hook == self._fixture_hook and module != self._fixture_module:
            self.hook_problems.append(f"sys.unraisablehook replaced and not restored by {self._fixture_where}")
            sys.unraisablehook = self._unraisable
            self._fixture_hook = None
            self._fixture_where = ""
            return
        if hook != self._fixture_hook:
            self._fixture_hook = hook
            self._fixture_where = f"a class or module set-up or tear-down before {test.id()}"
            self._fixture_module = module

    def _gc_phase(self, phase: str, _info: dict[str, int]) -> None:
        self._collecting = phase == "start"

    def _where(self) -> str:
        """The test that was running, and when the garbage collector freed the object, that it
        may have been opened by an earlier one."""
        if self._collecting:
            return f"{self._running} (or one before it: found by {self._found_by})"
        return self._running

    def _entered(self, msg: warnings.WarningMessage) -> None:
        if warnings.showwarning is self._showwarning_orig:
            self._show_entry(msg)
            return
        self._record(msg)  # the replacement gets it, not _warned
        # A replacement that hands the warning on to the original (warnings._showwarning_orig)
        # reaches _warned: that is the same warning, already recorded.
        outer, self._in_replaced = self._in_replaced, True
        try:
            self._show_entry(msg)
        finally:
            self._in_replaced = outer  # a warning raised inside the replacement must not clear it

    def _warned(self, msg: warnings.WarningMessage) -> None:
        if not self._in_replaced:
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

    def make_result(self) -> LeakRecordingResult:
        """Create the runner's `LeakRecordingResult` bound to its decorated stream."""
        return self._makeResult()


DEFAULT_CLASS_CHUNK: Final = 10
_JOBS_FLAG: Final = "--jobs"
_AFFECTED_FLAG: Final = "--affected"
_LIST_AFFECTED_FLAG: Final = "--list-affected"
_BASE_FLAG: Final = "--base"
_WORKER_FDS_FLAG: Final = "--worker-fds"
_ERR_LOAD_MODULE: Final = "unittest.loader"
_SEP1: Final = "=" * 70
_SEP2: Final = "-" * 70
_FAIL_LABEL: Final = "FAIL"
_ERROR_LABEL: Final = "ERROR"


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Parsed command-line options for `tests/run_unit.py`."""

    verbosity: int = 1
    jobs: int | None = None
    gates: bool = False
    affected: bool = False
    list_affected: bool = False
    base: str | None = None
    names: tuple[str, ...] = ()
    worker_fds: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class _WorkerReport:
    ran: int
    stream: str
    extra_stderr: str
    failures: tuple[tuple[str, str], ...]
    errors: tuple[tuple[str, str], ...]
    expected_failures: int
    unexpected_successes: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    leaks: tuple[str, ...]
    lost_exceptions: tuple[str, ...]
    hook_problems: tuple[str, ...]
    signal_problems: tuple[str, ...]


@dataclass(slots=True)
class _WorkerHandle:
    proc: subprocess.Popen[bytes]
    cmd_fh: io.TextIOWrapper
    res_fh: io.TextIOWrapper


@dataclass(slots=True)
class _ParallelTotals:
    lock: threading.Lock
    ran: int = 0
    expected_failures: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    unexpected_successes: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    leaks: list[str] = field(default_factory=list)
    lost_exceptions: list[str] = field(default_factory=list)
    hook_problems: list[str] = field(default_factory=list)
    signal_problems: list[str] = field(default_factory=list)
    worker_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _GateFindings:
    skipped: Sequence[tuple[str, str]]
    leaks: Sequence[str]
    lost: Sequence[str]
    extra_errors: Sequence[str]


def _decode_json(line: str) -> object:
    return json.loads(line)


def _str_field(obj: dict[object, object], key: str) -> str:
    val = obj.get(key)
    if not isinstance(val, str):
        raise ValueError(f"expected string for {key!r}")
    return val


def _int_field(obj: dict[object, object], key: str) -> int:
    val = obj.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"expected int for {key!r}")
    return val


def _str_list_field(obj: dict[object, object], key: str) -> tuple[str, ...]:
    raw = obj.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"expected list for {key!r}")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"expected string entry in {key!r}")
        out.append(item)
    return tuple(out)


def _pair_list_field(obj: dict[object, object], key: str) -> tuple[tuple[str, str], ...]:
    raw = obj.get(key)
    if not isinstance(raw, list):
        raise ValueError(f"expected list for {key!r}")
    out: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str) or not isinstance(item[1], str):
            raise ValueError(f"expected 2-string pair in {key!r}")
        out.append((item[0], item[1]))
    return tuple(out)


def _decode_worker_report(line: str) -> _WorkerReport:
    raw = _decode_json(line)
    if not isinstance(raw, dict):
        raise ValueError("worker reply is not a JSON object")
    return _WorkerReport(
        ran=_int_field(raw, "ran"),
        stream=_str_field(raw, "stream"),
        extra_stderr=_str_field(raw, "extra_stderr"),
        failures=_pair_list_field(raw, "failures"),
        errors=_pair_list_field(raw, "errors"),
        expected_failures=_int_field(raw, "expected_failures"),
        unexpected_successes=_str_list_field(raw, "unexpected_successes"),
        skipped=_pair_list_field(raw, "skipped"),
        leaks=_str_list_field(raw, "leaks"),
        lost_exceptions=_str_list_field(raw, "lost_exceptions"),
        hook_problems=_str_list_field(raw, "hook_problems"),
        signal_problems=_str_list_field(raw, "signal_problems"),
    )


def _coverage_active() -> bool:
    if sys.gettrace() is not None or sys.modules.get("coverage") is not None:
        return True
    return sys.monitoring.get_tool(sys.monitoring.COVERAGE_ID) is not None


def default_jobs() -> int:
    """Default worker count when `-j`/`--jobs` is unset or 0 (1 under coverage.py)."""
    if _coverage_active():
        return 1
    return max(1, os.cpu_count() or 1)


def _consume_value(args: Sequence[str], index: int, flag: str) -> tuple[str, int]:
    tok = args[index]
    prefix = f"{flag}="
    if tok.startswith(prefix):
        return tok[len(prefix) :], 1
    if index + 1 >= len(args):
        raise SystemExit(f"run_unit: {flag} requires a value")
    return args[index + 1], 2


@dataclass(slots=True)
class _ParseAcc:
    verbosity: int = 1
    jobs: int | None = None
    gates: bool = False
    affected: bool = False
    list_affected: bool = False
    base: str | None = None
    names: list[str] = field(default_factory=list)
    worker_fds: tuple[int, int] | None = None


def _parse_jobs_value(raw: str) -> int:
    if not raw.isdigit():
        raise SystemExit(f"run_unit: invalid {_JOBS_FLAG} value {raw!r} (expected non-negative integer)")
    return int(raw)


def _parse_worker_fds(raw: str) -> tuple[int, int]:
    parts = raw.split(",")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise SystemExit(f"run_unit: invalid {_WORKER_FDS_FLAG} value {raw!r}")
    return int(parts[0]), int(parts[1])


def _consume_simple_flag(tok: str, acc: _ParseAcc) -> bool:
    if tok == "-v":
        acc.verbosity = 2
        return True
    if tok == "--gates":
        acc.gates = True
        return True
    if tok == _AFFECTED_FLAG:
        acc.affected = True
        return True
    if tok == _LIST_AFFECTED_FLAG:
        acc.affected = True
        acc.list_affected = True
        return True
    return False


def _consume_value_flag(args: Sequence[str], index: int, acc: _ParseAcc) -> int | None:
    tok = args[index]
    if tok in ("-j", _JOBS_FLAG) or tok.startswith(f"{_JOBS_FLAG}="):
        flag = _JOBS_FLAG if tok.startswith(_JOBS_FLAG) else "-j"
        val, step = _consume_value(args, index, flag)
        acc.jobs = _parse_jobs_value(val)
        return step
    if tok == _BASE_FLAG or tok.startswith(f"{_BASE_FLAG}="):
        val, step = _consume_value(args, index, _BASE_FLAG)
        acc.base = val
        return step
    if tok == _WORKER_FDS_FLAG or tok.startswith(f"{_WORKER_FDS_FLAG}="):
        val, step = _consume_value(args, index, _WORKER_FDS_FLAG)
        acc.worker_fds = _parse_worker_fds(val)
        return step
    return None


def _consume_arg(args: Sequence[str], index: int, acc: _ParseAcc) -> int:
    tok = args[index]
    if _consume_simple_flag(tok, acc):
        return 1
    value_step = _consume_value_flag(args, index, acc)
    if value_step is not None:
        return value_step
    if tok.startswith("-"):
        raise SystemExit(
            f"run_unit: unknown option in {' '.join(args)} "
            f"(use -v, -j/{_JOBS_FLAG} N, --gates, {_AFFECTED_FLAG}, {_LIST_AFFECTED_FLAG}, {_BASE_FLAG} or test names)"
        )
    acc.names.append(tok)
    return 1


def parse_args(args: Sequence[str]) -> RunOptions:
    """Parse CLI options for `tests/run_unit.py`."""
    acc = _ParseAcc()
    i = 0
    while i < len(args):
        i += _consume_arg(args, i, acc)
    if acc.gates and acc.names:
        raise SystemExit(f"run_unit: --gates takes no test names ({' '.join(acc.names)})")
    if acc.affected and (acc.gates or acc.names):
        raise SystemExit(f"run_unit: {_AFFECTED_FLAG} takes neither --gates nor test names")
    if acc.base is not None and not acc.affected:
        raise SystemExit(f"run_unit: {_BASE_FLAG} requires {_AFFECTED_FLAG} or {_LIST_AFFECTED_FLAG}")
    return RunOptions(
        verbosity=acc.verbosity,
        jobs=acc.jobs,
        gates=acc.gates,
        affected=acc.affected,
        list_affected=acc.list_affected,
        base=acc.base,
        names=tuple(acc.names),
        worker_fds=acc.worker_fds,
    )


def _load_options(repo: str, opts: RunOptions) -> unittest.TestSuite:
    if opts.gates:
        return unittest.defaultTestLoader.loadTestsFromNames(GATE_MODULES)
    if opts.names:
        return unittest.defaultTestLoader.loadTestsFromNames(opts.names)
    return unittest.defaultTestLoader.discover(repo)


def load(repo: str, args: Sequence[str]) -> unittest.TestSuite:
    """The tests to run: every test (no name), the static gates (--gates) or the named ones."""
    return _load_options(repo, parse_args(args))


def _iter_cases(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    cases: list[unittest.TestCase] = []
    stack: list[unittest.TestSuite | unittest.TestCase] = [suite]
    while stack:
        item = stack.pop()
        if isinstance(item, unittest.TestSuite):
            stack.extend(reversed(list(item)))
        else:
            cases.append(item)
    return cases


def _has_module_fixture(module_name: str) -> bool:
    mod = sys.modules.get(module_name)
    return mod is not None and (hasattr(mod, "setUpModule") or hasattr(mod, "tearDownModule"))


def _has_class_fixture(cls: type[unittest.TestCase]) -> bool:
    return any(
        "setUpClass" in base.__dict__ or "tearDownClass" in base.__dict__
        for base in cls.__mro__
        if base is not unittest.TestCase and issubclass(base, unittest.TestCase)
    )


def _split_balanced(items: Sequence[str], max_chunk: int) -> list[tuple[str, ...]]:
    chunk_count = (len(items) + max_chunk - 1) // max_chunk
    base, rem = divmod(len(items), chunk_count)
    out: list[tuple[str, ...]] = []
    offset = 0
    for idx in range(chunk_count):
        size = base + (1 if idx < rem else 0)
        out.append(tuple(items[offset : offset + size]))
        offset += size
    return out


def _shard_module_cases(
    module_name: str, mod_cases: Sequence[unittest.TestCase], max_chunk: int
) -> list[tuple[str, ...]]:
    if _has_module_fixture(module_name):
        return [tuple(c.id() for c in mod_cases)]
    by_cls: dict[type[unittest.TestCase], list[str]] = {}
    for case in mod_cases:
        by_cls.setdefault(type(case), []).append(case.id())
    units: list[tuple[str, ...]] = []
    for cls, cls_ids in by_cls.items():
        if _has_class_fixture(cls) or len(cls_ids) <= max_chunk:
            units.append(tuple(cls_ids))
        else:
            units.extend(_split_balanced(cls_ids, max_chunk))
    return units


def plan_work_units(
    suite: unittest.TestSuite, *, max_chunk: int = DEFAULT_CLASS_CHUNK
) -> tuple[list[tuple[str, ...]], bool]:
    """Partition `suite` into isolated work units, falling back on loader errors."""
    cases = _iter_cases(suite)
    if any(type(c).__module__ == _ERR_LOAD_MODULE for c in cases):
        return [], True
    by_mod: dict[str, list[unittest.TestCase]] = {}
    for case in cases:
        by_mod.setdefault(type(case).__module__, []).append(case)
    units: list[tuple[str, ...]] = []
    for module_name, mod_cases in by_mod.items():
        units.extend(_shard_module_cases(module_name, mod_cases, max_chunk))
    units.sort(key=lambda u: (-len(u), u[0]))
    return units, False


def _open_socket_stdin() -> socket.socket:
    ours, theirs = socket.socketpair()
    os.dup2(theirs.fileno(), 0)  # fd 0 is now a socket, inherited by every child
    theirs.close()
    sys.stdin = open(0, closefd=False)
    return ours


def _run_unit_payload(names: Sequence[str], verbosity: int) -> dict[str, object]:
    suite = unittest.defaultTestLoader.loadTestsFromNames(names)
    stream_buf = io.StringIO()
    err_buf = io.StringIO()
    result = LeakRecordingRunner(stream=stream_buf, descriptions=True, verbosity=verbosity).make_result()
    gc_was_enabled = gc.isenabled()
    with warnings.catch_warnings(), contextlib.redirect_stderr(err_buf):
        result.startTestRun()
        try:
            suite.run(result)
        finally:
            result.stopTestRun()
            if gc_was_enabled and not gc.isenabled():
                gc.enable()
    return {
        "ran": result.testsRun,
        "stream": stream_buf.getvalue(),
        "extra_stderr": err_buf.getvalue(),
        "failures": [[result.getDescription(t), tb] for t, tb in result.failures],
        "errors": [[result.getDescription(t), tb] for t, tb in result.errors],
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": [result.getDescription(t) for t in result.unexpectedSuccesses],
        "skipped": [[t.id(), reason] for t, reason in result.skipped],
        "leaks": list(result.leaks),
        "lost_exceptions": list(result.lost_exceptions),
        "hook_problems": list(result.hook_problems),
        "signal_problems": list(result.signal_problems),
    }


def _handle_worker_line(line: str, verbosity: int) -> dict[str, object]:
    raw = _decode_json(line)
    if not isinstance(raw, dict):
        raise ValueError("worker command is not a JSON object")
    return _run_unit_payload(_str_list_field(raw, "names"), verbosity)


def _worker_main(repo: str, opts: RunOptions) -> int:
    if opts.worker_fds is None:
        raise SystemExit("run_unit: missing worker file descriptors")
    cmd_fd, res_fd = opts.worker_fds
    inheritable = False
    for fd in (cmd_fd, res_fd):
        os.set_inheritable(fd, inheritable)
    os.chdir(repo)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    ours = _open_socket_stdin()
    try:
        with (
            os.fdopen(cmd_fd, "r", encoding="utf-8") as cmd_fh,
            os.fdopen(res_fd, "w", encoding="utf-8") as res_fh,
        ):
            for raw_line in cmd_fh:
                line = raw_line.strip()
                if not line:
                    continue
                payload = _handle_worker_line(line, opts.verbosity)
                res_fh.write(json.dumps(payload) + "\n")
                res_fh.flush()
        return 0
    finally:
        ours.close()


def _xoptions_argv() -> list[str]:
    out: list[str] = []
    for key, val in sys._xoptions.items():
        opt = f"{key}={val}" if isinstance(val, str) else key
        out.extend(("-X", opt))
    return out


def _worker_argv(verbosity: int, cmd_r: int, res_w: int) -> list[str]:
    argv = [sys.executable]
    if sys.flags.isolated:
        argv.append("-I")
    argv.append("-B")
    argv.extend(_xoptions_argv())
    argv.append(os.path.abspath(__file__))
    if verbosity >= 2:
        argv.append("-v")
    argv.extend((_WORKER_FDS_FLAG, f"{cmd_r},{res_w}"))
    return argv


def _spawn_worker(repo: str, verbosity: int) -> _WorkerHandle:
    cmd_r, cmd_w = os.pipe()
    res_r, res_w = os.pipe()
    try:
        proc = subprocess.Popen(
            _worker_argv(verbosity, cmd_r, res_w),
            pass_fds=(cmd_r, res_w),
            stdin=subprocess.DEVNULL,
            cwd=repo,
        )
    except OSError:
        for fd in (cmd_r, cmd_w, res_r, res_w):
            os.close(fd)
        raise
    os.close(cmd_r)
    os.close(res_w)
    return _WorkerHandle(
        proc=proc,
        cmd_fh=os.fdopen(cmd_w, "w", encoding="utf-8"),
        res_fh=os.fdopen(res_r, "r", encoding="utf-8"),
    )


def _close_worker(worker: _WorkerHandle, totals: _ParallelTotals) -> None:
    with contextlib.suppress(OSError):
        worker.cmd_fh.close()
    with contextlib.suppress(OSError):
        worker.res_fh.close()
    if worker.proc.poll() is None:
        worker.proc.terminate()
        try:
            worker.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.proc.kill()
            worker.proc.wait(timeout=10)
        with totals.lock:
            totals.worker_errors.append("a worker process was still alive at shutdown")


def _spawn_workers(repo: str, verbosity: int, count: int) -> list[_WorkerHandle]:
    workers: list[_WorkerHandle] = []
    try:
        workers.extend(_spawn_worker(repo, verbosity) for _ in range(count))
    except OSError:
        for worker in workers:
            with contextlib.suppress(OSError):
                worker.cmd_fh.close()
            with contextlib.suppress(OSError):
                worker.res_fh.close()
            worker.proc.terminate()
            worker.proc.wait(timeout=10)
        raise
    return workers


def _record_report(report: _WorkerReport, totals: _ParallelTotals) -> None:
    with totals.lock:
        if report.stream:
            sys.stderr.write(report.stream)
        if report.extra_stderr:
            sys.stderr.write(report.extra_stderr)
        sys.stderr.flush()
        totals.ran += report.ran
        totals.expected_failures += report.expected_failures
        totals.failures.extend(report.failures)
        totals.errors.extend(report.errors)
        totals.unexpected_successes.extend(report.unexpected_successes)
        totals.skipped.extend(report.skipped)
        totals.leaks.extend(report.leaks)
        totals.lost_exceptions.extend(report.lost_exceptions)
        totals.hook_problems.extend(report.hook_problems)
        totals.signal_problems.extend(report.signal_problems)


def _drain_worker(
    worker: _WorkerHandle,
    work_q: queue.SimpleQueue[tuple[str, ...]],
    totals: _ParallelTotals,
) -> None:
    while True:
        try:
            unit = work_q.get_nowait()
        except queue.Empty:
            break
        try:
            worker.cmd_fh.write(json.dumps({"names": list(unit)}) + "\n")
            worker.cmd_fh.flush()
            reply = worker.res_fh.readline()
        except OSError as exc:
            with totals.lock:
                totals.worker_errors.append(f"worker I/O failed running {unit[0]}: {exc}")
            return
        if not reply:
            with totals.lock:
                totals.worker_errors.append(f"worker exited unexpectedly running {unit[0]}")
            return
        try:
            report = _decode_worker_report(reply)
        except (ValueError, json.JSONDecodeError) as exc:
            with totals.lock:
                totals.worker_errors.append(f"malformed worker reply running {unit[0]}: {exc}")
            return
        _record_report(report, totals)
    worker.cmd_fh.close()
    worker.res_fh.close()
    rc = worker.proc.wait(timeout=30)
    if rc != 0:
        with totals.lock:
            totals.worker_errors.append(f"worker exited with status {rc}")


def _execute_workers(workers: list[_WorkerHandle], units: Sequence[tuple[str, ...]]) -> _ParallelTotals:
    work_q: queue.SimpleQueue[tuple[str, ...]] = queue.SimpleQueue()
    for unit in units:
        work_q.put(unit)
    totals = _ParallelTotals(lock=threading.Lock())
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(workers)) as pool:
            futures = [pool.submit(_drain_worker, worker, work_q, totals) for worker in workers]
            for fut in futures:
                fut.result()
    finally:
        for worker in workers:
            _close_worker(worker, totals)
    return totals


def _format_error_list(flavour: str, pairs: Sequence[tuple[str, str]]) -> str:
    parts: list[str] = []
    for label, err in pairs:
        parts.extend((_SEP1, f"{flavour}: {label}", _SEP2, err.rstrip("\n"), ""))
    return "\n".join(parts)


def _summary_status(totals: _ParallelTotals) -> str:
    failed = bool(totals.failures or totals.errors or totals.unexpected_successes)
    infos: list[str] = []
    if totals.failures:
        infos.append(f"failures={len(totals.failures)}")
    if totals.errors:
        infos.append(f"errors={len(totals.errors)}")
    if totals.unexpected_successes:
        infos.append(f"unexpected successes={len(totals.unexpected_successes)}")
    if totals.skipped:
        infos.append(f"skipped={len(totals.skipped)}")
    if totals.expected_failures:
        infos.append(f"expected failures={totals.expected_failures}")
    head = "FAILED" if failed else "OK"
    return f"{head} ({', '.join(infos)})" if infos else head


def _emit_parallel_report(totals: _ParallelTotals, verbosity: int, workers_used: int, elapsed: float) -> None:
    if verbosity == 1 and totals.ran:
        print(file=sys.stderr)
    err_block = _format_error_list(_ERROR_LABEL, totals.errors)
    fail_block = _format_error_list(_FAIL_LABEL, totals.failures)
    if err_block:
        print(err_block, file=sys.stderr)
    if fail_block:
        print(fail_block, file=sys.stderr)
    for label in totals.unexpected_successes:
        print(f"{_SEP1}\nUNEXPECTED SUCCESS: {label}", file=sys.stderr)
    print(_SEP2, file=sys.stderr)
    plural = "s" if totals.ran != 1 else ""
    print(f"Ran {totals.ran} test{plural} in {elapsed:.3f}s ({workers_used} workers)\n", file=sys.stderr)
    print(_summary_status(totals), file=sys.stderr)


def _emit_gate_checks(repo: str, bytecode_before: dict[str, int], findings: _GateFindings) -> list[str]:
    bytecode = [
        f"a test wrote bytecode into the checkout: {path}"
        for path in new_bytecode(bytecode_before, bytecode_files(repo))
    ]
    for test_id, reason in findings.skipped:
        print(f"skipped: {test_id}: {reason}", file=sys.stderr)
    problems = skip_problems(findings.skipped, git_checkout=os.path.exists(os.path.join(repo, ".git")))
    errors = [*problems, *findings.leaks, *findings.lost, *findings.extra_errors, *bytecode]
    for line in errors:
        print(f"error: {line}", file=sys.stderr)
    if findings.leaks:
        print(LEAK_HINT, file=sys.stderr)
    if findings.lost:
        print(LOST_EXCEPTION_HINT, file=sys.stderr)
    if bytecode:
        print(BYTECODE_HINT, file=sys.stderr)
    return errors


def _run_in_process(repo: str, opts: RunOptions) -> int:
    ours = _open_socket_stdin()
    bytecode_before = bytecode_files(repo)
    try:
        suite = _load_options(repo, opts)
        result = LeakRecordingRunner(verbosity=opts.verbosity).run(suite)
    finally:
        ours.close()
    if not isinstance(result, LeakRecordingResult):  # its _makeResult makes one: never silently no leaks
        raise TypeError(f"the runner made a {type(result).__name__}, not a LeakRecordingResult")
    findings = _GateFindings(
        skipped=[(test.id(), reason) for test, reason in result.skipped],
        leaks=result.leaks,
        lost=result.lost_exceptions,
        extra_errors=[*result.hook_problems, *result.signal_problems],
    )
    errors = _emit_gate_checks(repo, bytecode_before, findings)
    return 0 if result.wasSuccessful() and not errors else 1


def _run_parallel(repo: str, opts: RunOptions, units: Sequence[tuple[str, ...]], jobs: int) -> int:
    effective_jobs = min(jobs, len(units))
    bytecode_before = bytecode_files(repo)
    started = time.monotonic()
    workers = _spawn_workers(repo, opts.verbosity, effective_jobs)
    totals = _execute_workers(workers, units)
    elapsed = time.monotonic() - started
    _emit_parallel_report(totals, opts.verbosity, effective_jobs, elapsed)
    findings = _GateFindings(
        skipped=sorted(totals.skipped),
        leaks=totals.leaks,
        lost=totals.lost_exceptions,
        extra_errors=[*totals.hook_problems, *totals.signal_problems, *totals.worker_errors],
    )
    errors = _emit_gate_checks(repo, bytecode_before, findings)
    has_test_fail = bool(totals.failures or totals.errors or totals.unexpected_successes)
    return 0 if not has_test_fail and not errors else 1


def _print_affected_plan(repo: str, base: str | None) -> int:
    from tests.affected import format_plan, git_changed_files, select_affected

    try:
        resolved_base, changed = git_changed_files(repo, base)
    except ValueError as exc:
        print(f"run_unit: {exc}", file=sys.stderr)
        return 2
    plan = select_affected(changed, repo=repo, base=resolved_base)
    print(format_plan(plan))
    return 0


def _resolve_affected_opts(repo: str, opts: RunOptions) -> RunOptions:
    from tests.affected import git_changed_files, select_affected

    resolved_base, changed = git_changed_files(repo, opts.base)
    plan = select_affected(changed, repo=repo, base=resolved_base)
    print(f"[affected] {plan.reason} (base={plan.base[:12]})", file=sys.stderr)
    return RunOptions(
        verbosity=opts.verbosity,
        jobs=opts.jobs,
        names=() if plan.full_suite else plan.modules,
    )


def _run(repo: str, opts: RunOptions) -> int:
    active_opts = opts
    if opts.affected:
        try:
            active_opts = _resolve_affected_opts(repo, opts)
        except ValueError as exc:
            print(f"run_unit: {exc}", file=sys.stderr)
            return 2
    requested = default_jobs() if active_opts.jobs is None or active_opts.jobs == 0 else active_opts.jobs
    if requested <= 1:
        return _run_in_process(repo, active_opts)
    suite = _load_options(repo, active_opts)
    units, has_load_error = plan_work_units(suite)
    if has_load_error or min(requested, len(units)) <= 1:
        return _run_in_process(repo, active_opts)
    return _run_parallel(repo, active_opts, units, requested)


def guard_hits(folder: str) -> list[str]:
    """The command lines the stubs in folder refused (a test that ignored the refusal still fails the run)."""
    try:
        with open(os.path.join(folder, HOST_GUARD_HITS), encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()
    except FileNotFoundError:
        return []


def main(argv: Sequence[str]) -> int:
    sys.dont_write_bytecode = True  # run by its shebang (no -B): the test modules it imports write none
    opts = parse_args(argv)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if opts.worker_fds is not None:
        return _worker_main(repo, opts)
    os.chdir(repo)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    if opts.list_affected:
        return _print_affected_plan(repo, opts.base)
    forbid_privilege_gain()
    guard = tempfile.mkdtemp(prefix="cst-unit-host-guard-")
    try:
        guard_host_commands(guard)
        rc = _run(repo, opts)
        hits = guard_hits(guard)
        for line in hits:
            print(f"error: a unit test reached the real {line}", file=sys.stderr)
        return 1 if hits else rc
    finally:
        shutil.rmtree(guard, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

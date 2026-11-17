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
"""

from collections.abc import Sequence
import os
import socket
import sys
from typing import Final
import unittest

# The static gates: tests that read the repository (layers, budgets,
# entry points, the manifest, the Python and shell lists, docs), not the
# behaviour of a module. Seconds instead of minutes.
GATE_MODULES: Final = (
    "tests.test_architecture",
    "tests.test_bytecode",
    "tests.test_distribution",
    "tests.test_docs",
    "tests.test_duplication",
    "tests.test_entrypoints",
    "tests.test_python_files",
    "tests.test_repo_paths",
    "tests.test_ruff_budget",
    "tests.test_run_unit",
    "tests.test_subprocess_hygiene",
    "tests.test_typing_budget",
    "tests.test_unit_contract",
)


# Skips that depend on the machine the tests run on: test id (or the id of
# its class) -> why. tests/test_run_unit.py checks that each one still names
# a test.
EXPECTED_SKIPS: Final[dict[str, str]] = {
    "bluetooth.tests.test_bluetooth.WirePlumberResolveTest": "needs libwireplumber-0.5 (not in CI, not in a toolbox)",
    "btrfs.tests.test_btrfs_bulk.BulkTest.test_root_reads_bulk_user_with_the_one_settings_reader": "not as root",
    "btrfs.tests.test_btrfs_snapshot_core.BtrbkOracleTests": "needs btrbk (perl) to compare with",
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
    verbosity = 2 if "-v" in argv else 1
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    ours, theirs = socket.socketpair()
    os.dup2(theirs.fileno(), 0)  # fd 0 is now a socket, inherited by every child
    theirs.close()
    sys.stdin = open(0, closefd=False)
    suite = load(repo, argv)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    ours.close()
    skipped = [(test.id(), reason) for test, reason in result.skipped]
    for test_id, reason in skipped:
        print(f"skipped: {test_id}: {reason}", file=sys.stderr)
    problems = skip_problems(skipped, git_checkout=os.path.exists(os.path.join(repo, ".git")))
    for line in problems:
        print(f"error: {line}", file=sys.stderr)
    return 0 if result.wasSuccessful() and not problems else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

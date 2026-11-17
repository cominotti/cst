#!/usr/bin/python3 -I
"""Run the unit tests with a socket as stdin (a run-time gate).

Use: python3 -I -B tests/run_unit.py [-v] [--gates | NAME...]   (from anywhere)

With no NAME it runs every unit test of the checkout. A NAME is a test
module, class or method as unittest names it (vm.tests.test_vm,
vm.tests.test_vm.PartsTest.test_disks_nvram_tpm): `invowk cmd test module`
runs one that way, so the socket gate below still applies. `--gates` runs
the static gate modules only (GATE_MODULES: `invowk cmd test arch`).

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
    "tests.test_subprocess_hygiene",
    "tests.test_typing_budget",
)


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
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

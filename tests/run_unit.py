#!/usr/bin/python3 -I
"""Run the unit tests with a socket as stdin (a run-time gate).

Use: python3 -I -B tests/run_unit.py [-v]   (from the repository root)

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

import os
import socket
import sys
import unittest


def main(argv):
    verbosity = 2 if "-v" in argv else 1
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    ours, theirs = socket.socketpair()
    os.dup2(theirs.fileno(), 0)        # fd 0 is now a socket, inherited by every child
    theirs.close()
    sys.stdin = open(0, closefd=False)
    suite = unittest.defaultTestLoader.discover(repo)
    result = unittest.TextTestRunner(verbosity=verbosity).run(suite)
    ours.close()
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

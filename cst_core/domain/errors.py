# SPDX-License-Identifier: MPL-2.0
"""The errors every CST program reports the same way.

- Usage: the command line is wrong (exit status 2, usage text shown).
- Refused: the program will not do what was asked, for a reason it states
  (a precondition failed; nothing was changed).

ExitStatus names the exit statuses a shell gives that CST programs return or
read; shell_exit_status turns a child's return code into the exit status a
shell gives.
"""

from enum import IntEnum


class Usage(Exception):
    """A mistake on the command line."""


class Refused(Exception):
    """A request the program declines, with the reason as the message."""


class ExitStatus(IntEnum):
    """Exit statuses with a meaning of their own, as bash and timeout(1) give them."""

    TIMED_OUT = 124  # timeout(1): the command ran out of time
    CANNOT_RUN = 126  # found, but it cannot be executed
    NOT_FOUND = 127  # no such command
    SIGNALLED = 128  # 128 + N: killed by signal N
    KILLED = 137  # 128 + 9: killed by SIGKILL


def shell_exit_status(returncode: int) -> int:
    """A child's return code as a shell exit status: 128+N for a child killed by signal N (-N)."""
    return ExitStatus.SIGNALLED - returncode if returncode < 0 else returncode

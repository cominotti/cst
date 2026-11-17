# SPDX-License-Identifier: MPL-2.0
"""The errors every CST program reports the same way.

- Usage: the command line is wrong (exit status 2, usage text shown).
- Refused: the program will not do what was asked, for a reason it states
  (a precondition failed; nothing was changed).

shell_exit_status turns a child's return code into the exit status a shell gives.
"""


class Usage(Exception):
    """A mistake on the command line."""


class Refused(Exception):
    """A request the program declines, with the reason as the message."""


def shell_exit_status(returncode: int) -> int:
    """A child's return code as a shell exit status: 128+N for a child killed by signal N (-N)."""
    return 128 - returncode if returncode < 0 else returncode

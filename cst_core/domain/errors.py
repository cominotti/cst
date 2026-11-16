"""The errors every CST program reports the same way.

- Usage: the command line is wrong (exit status 2, usage text shown).
- Refused: the program will not do what was asked, for a reason it states
  (a precondition failed; nothing was changed).
"""


class Usage(Exception):
    """A mistake on the command line."""


class Refused(Exception):
    """A request the program declines, with the reason as the message."""

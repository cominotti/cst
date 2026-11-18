# SPDX-License-Identifier: MPL-2.0
"""Questions on the terminal."""

import sys

from cst_core.domain.errors import Refused
from cst_core.domain.text import is_yes


def confirm(question: str) -> bool:
    """Ask QUESTION with " [y/N] " on the terminal: True for a yes (is_yes).

    Refused when stdin is not a terminal: nobody is there to answer, and a
    change that asks first never goes ahead without an answer.
    """
    if not sys.stdin.isatty():
        raise Refused("apply asks for confirmation: run it in a terminal")
    return is_yes(input(f"{question} [y/N] "))

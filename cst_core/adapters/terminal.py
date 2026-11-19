# SPDX-License-Identifier: MPL-2.0
"""The terminal: the standard streams, and questions on it."""

import sys
from typing import TextIO, cast

from cst_core.domain.errors import Refused
from cst_core.domain.text import is_yes


def _std(stream: TextIO | None) -> TextIO:
    """The stream as a TextIO, unchanged. typeshed types sys.stdin, sys.stdout and sys.stderr as
    `TextIO | MaybeNone` (None for a program started without them); CST's programs always have them."""
    return cast("TextIO", stream)


def stdin() -> TextIO:
    """sys.stdin, read when called (a test that swaps sys.stdin is followed)."""
    return _std(sys.stdin)


def stdout() -> TextIO:
    """sys.stdout, read when called (contextlib.redirect_stdout is followed)."""
    return _std(sys.stdout)


def stderr() -> TextIO:
    """sys.stderr, read when called (contextlib.redirect_stderr is followed)."""
    return _std(sys.stderr)


def confirm(question: str) -> bool:
    """Ask QUESTION with " [y/N] " on the terminal: True for a yes (is_yes).

    Refused when stdin is not a terminal: nobody is there to answer, and a
    change that asks first never goes ahead without an answer.
    """
    if not sys.stdin.isatty():
        raise Refused("apply asks for confirmation: run it in a terminal")
    return is_yes(input(f"{question} [y/N] "))

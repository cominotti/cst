# SPDX-License-Identifier: MPL-2.0
"""The command line of `cst swap`, read as swap-setup.sh read it: the command word, --root, and
where it stops (help, an argument a command does not take, an unknown command) with its message
and exit status. Pure: swap/cli.py prints the message and the help text and runs the command."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from swap.domain import SwapCommand, parse_swap_command


@dataclass(frozen=True, slots=True)
class SwapArgs:
    """A command to run: the command, the words to run it again with (words: its word as given,
    then the rest), and whether it needs root (on the host, through sudo)."""

    command: Literal[SwapCommand.PLAN, SwapCommand.STATUS, SwapCommand.APPLY]
    words: tuple[str, ...]
    want_root: bool


@dataclass(frozen=True, slots=True)
class SwapArgsStop:
    """The command line ends here with exit status code: its message line (None: none), and
    whether the help text follows it (usage)."""

    code: int
    line: str | None
    usage: bool = False


def parse_swap_args(args: Sequence[str]) -> SwapArgs | SwapArgsStop:
    """What `cst swap ARGS` does."""
    cmd = args[0] if args and args[0] else "help"  # bash's ${1:-help}: an empty word is help too
    rest = tuple(args[1:])
    match parse_swap_command(cmd):
        case SwapCommand.HELP:
            return SwapArgsStop(0, line=None, usage=True)
        case (SwapCommand.PLAN | SwapCommand.STATUS) as command:
            first = rest[0] if rest else ""  # only the first word counts, as in swap-setup.sh
            want_root = first == "--root"
            if first and not want_root:
                return SwapArgsStop(2, line=f"{cmd}: unknown argument '{first}' (only --root)")
            return SwapArgs(command, (cmd, *rest), want_root=want_root)
        case SwapCommand.APPLY as command:
            if rest:
                return SwapArgsStop(2, line="apply: no arguments")
            return SwapArgs(command, (cmd,), want_root=True)
        case None:
            return SwapArgsStop(2, line=f"Unknown command: {cmd}", usage=True)

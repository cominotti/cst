# SPDX-License-Identifier: MPL-2.0
"""The command line of `cst btrfs compress-force` (`btrfs/btrfs-compress`): its words, flags and parser."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from btrfs.compress.domain import MAX_ZSTD_LEVEL, MIN_ZSTD_LEVEL
from cst_core.domain.text import HELP_WORDS, enum_member


class CompressCommand(StrEnum):
    """Supported subcommands of `btrfs-compress`."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"
    HELP = "help"


class CompressFlag(StrEnum):
    """Supported long flags of `btrfs-compress`."""

    REVERT = "--revert"
    YES = "--yes"
    LEVEL = "--level"


@dataclass(frozen=True, slots=True)
class CompressArgs:
    """Validated CLI arguments."""

    command: CompressCommand
    level: int | None = None
    revert: bool = False
    yes: bool = False


@dataclass(frozen=True, slots=True)
class ArgsError:
    """Invalid CLI invocation."""

    message: str
    code: int = 2


def _parse_level_value(raw: str) -> int | ArgsError:
    if not raw.isdigit():
        return ArgsError(f"invalid --level value {raw!r}: expected an integer {MIN_ZSTD_LEVEL}..{MAX_ZSTD_LEVEL}")
    val = int(raw)
    if val < MIN_ZSTD_LEVEL or val > MAX_ZSTD_LEVEL:
        return ArgsError(f"invalid --level {val}: must be between {MIN_ZSTD_LEVEL} and {MAX_ZSTD_LEVEL}")
    return val


def _parse_flag_token(tok: str) -> tuple[CompressFlag | None, str | None]:
    if tok.startswith(f"{CompressFlag.LEVEL}="):
        flag_str, inline = tok.split("=", 1)
        return enum_member(CompressFlag, flag_str), inline
    return enum_member(CompressFlag, tok), None


def _consume_level(cmd: CompressCommand, inline: str | None, next_arg: str | None) -> tuple[int | ArgsError, int]:
    if cmd == CompressCommand.STATUS:
        return ArgsError("--level is not supported with `status`"), 0
    if inline is not None:
        return _parse_level_value(inline), 0
    if next_arg is None:
        return ArgsError("--level requires a value"), 0
    return _parse_level_value(next_arg), 1


def _validate_flag(
    cmd: CompressCommand, tok: str, next_arg: str | None
) -> tuple[CompressFlag, int | bool, int] | ArgsError:
    flag, inline = _parse_flag_token(tok)
    res: tuple[CompressFlag, int | bool, int] | ArgsError
    match flag:
        case CompressFlag.REVERT:
            res = (
                (CompressFlag.REVERT, True, 0)
                if cmd == CompressCommand.PLAN
                else ArgsError("--revert is only supported with `plan`")
            )
        case CompressFlag.YES:
            res = (
                (CompressFlag.YES, True, 0)
                if cmd in (CompressCommand.APPLY, CompressCommand.REVERT)
                else ArgsError(f"--yes is not supported with `{cmd}`")
            )
        case CompressFlag.LEVEL:
            parsed, consumed = _consume_level(cmd, inline, next_arg)
            res = parsed if isinstance(parsed, ArgsError) else (CompressFlag.LEVEL, parsed, consumed)
        case None:
            res = ArgsError(f"unknown or unexpected argument: {tok}")
    return res


def _parse_compress_flags(cmd: CompressCommand, rest: Sequence[str]) -> CompressArgs | ArgsError:
    level: int | None = None
    revert = False
    yes = False
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in HELP_WORDS:
            return CompressArgs(command=CompressCommand.HELP)
        next_arg = rest[i + 1] if i + 1 < len(rest) else None
        step = _validate_flag(cmd, tok, next_arg)
        if isinstance(step, ArgsError):
            return step
        flag, val, extra = step
        match flag:
            case CompressFlag.REVERT:
                revert = bool(val)
            case CompressFlag.YES:
                yes = bool(val)
            case CompressFlag.LEVEL:
                level = int(val)
        i += 1 + extra
    return CompressArgs(command=cmd, level=level, revert=revert, yes=yes)


def parse_compress_args(argv: Sequence[str]) -> CompressArgs | ArgsError:
    """Parse argv (without program name) into `CompressArgs` or `ArgsError`."""
    if not argv:
        return CompressArgs(command=CompressCommand.STATUS)
    head = argv[0]
    if head in HELP_WORDS or head == CompressCommand.HELP:
        return (
            ArgsError(f"unexpected argument after {head}: {argv[1]}")
            if len(argv) > 1
            else CompressArgs(command=CompressCommand.HELP)
        )
    cmd = enum_member(CompressCommand, head)
    if cmd is None:
        return ArgsError(f"unknown command: {head}")
    return _parse_compress_flags(cmd, argv[1:])

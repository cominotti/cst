# SPDX-License-Identifier: MPL-2.0
"""Pure CLI parsing, configuration parsing, and shared constants for `cst desktop`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeIs

from cst_core.domain.text import HELP_WORDS, enum_member, parse_shell_assignments, parse_subcommand_head
from cst_core.domain.values import GIB, MIB, ByteSize, DomainValidationError

DESKTOP_NAME: Final = "cst-desktop"
DESKTOP_PROG: Final = "cst desktop"
DESKTOP_CONF_REL: Final = ".config/cst/desktop.conf"
LOCALSEARCH_STATE_REL: Final = ".config/cst/desktop-gnome-localsearch.json"
PSD_RECOVERY_REL: Final = ".local/share/cst/psd/recovery"
PSD_RUN_ROOT: Final = "/run/cst-psd"

DEFAULT_TMPFS_SIZE: Final = "4G"
DEFAULT_TMPFS_BYTES: Final = 4 * GIB
MIN_TMPFS_BYTES: Final = 256 * MIB
MAX_TMPFS_BYTES: Final = 32 * GIB
MAX_RAM_FRACTION_PCT: Final = 25
EXIT_DESKTOP_USAGE: Final = 2

_CONFIG_KEYS: Final[frozenset[str]] = frozenset({"PSD_PROFILES", "PSD_TMPFS_SIZE"})
_HELP_FLAG_PREFIX: Final = "-"
_STATUS_USER_ARGC: Final = 2


def is_str_val(val: object) -> TypeIs[str]:
    """Return True when `val` is a `str` (shared type guard for JSON `list_of`)."""
    return isinstance(val, str)


class _PsdFlag(StrEnum):
    SKIP_BUSY = "--skip-busy"
    DISCARD = "--discard"
    USER = "--user"
    PROFILES = "--profiles"
    SIZE = "--size"


class DesktopCommand(StrEnum):
    """Top-level commands of `cst desktop`."""

    STATUS = "status"
    GNOME = "gnome"
    PSD = "psd"
    HELP = "help"


class GnomeTopic(StrEnum):
    """Topics under `cst desktop gnome`."""

    LOCALSEARCH = "localsearch"
    HELP = "help"


class LocalsearchAction(StrEnum):
    """Subcommands under `cst desktop gnome localsearch`."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"
    HELP = "help"


class LocalsearchMode(StrEnum):
    """Target indexing policy for GNOME `localsearch-3`."""

    RESTRICT = "restrict"
    DISABLE = "disable"


class PsdAction(StrEnum):
    """Subcommands under `cst desktop psd`."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"
    START = "start"
    STOP = "stop"
    SYNC = "sync"
    RECOVER = "recover"
    HELP = "help"


@dataclass(frozen=True, slots=True)
class DesktopConfig:
    """Validated user configuration from `~/.config/cst/desktop.conf`."""

    profiles_raw: str = "auto"
    tmpfs_bytes: int = DEFAULT_TMPFS_BYTES


@dataclass(frozen=True, slots=True)
class DesktopParsedArgs:
    """Validated CLI arguments for `cst desktop`."""

    command: DesktopCommand
    ls_action: LocalsearchAction = LocalsearchAction.STATUS
    psd_action: PsdAction = PsdAction.STATUS
    mode: LocalsearchMode = LocalsearchMode.RESTRICT
    user: str | None = None
    profiles: str | None = None
    size_text: str | None = None
    discard: bool = False
    skip_busy: bool = False
    error: str | None = None
    code: int = 0


def parse_tmpfs_size_bytes(raw: str) -> int:
    """Parse a tmpfs capacity spec (`256M`..`32G`) into exact bytes."""
    parsed = ByteSize.parse(raw)
    if parsed is None:
        raise DomainValidationError(f"invalid PSD_TMPFS_SIZE {raw!r} (expected e.g. 2G or 4G)")
    count = parsed.bytes_count
    if count < MIN_TMPFS_BYTES or count > MAX_TMPFS_BYTES:
        raise DomainValidationError(f"PSD_TMPFS_SIZE {raw!r} out of allowed range (256M..32G)")
    return count


def parse_desktop_config(text: str, *, source: str = DESKTOP_CONF_REL) -> DesktopConfig:
    """Parse and validate `~/.config/cst/desktop.conf` `KEY=VALUE` lines."""
    assignments = parse_shell_assignments(text, bash_sourced=False)
    if assignments.problems:
        first = assignments.problems[0]
        raise DomainValidationError(f"{source}:{first.line}: {first.reason}")
    for key in assignments.values:
        if key not in _CONFIG_KEYS:
            raise DomainValidationError(f"{source}: unknown setting {key!r}")
    profiles_raw = assignments.values.get("PSD_PROFILES", "auto").strip() or "auto"
    size_raw = assignments.values.get("PSD_TMPFS_SIZE", DEFAULT_TMPFS_SIZE).strip() or DEFAULT_TMPFS_SIZE
    return DesktopConfig(profiles_raw=profiles_raw, tmpfs_bytes=parse_tmpfs_size_bytes(size_raw))


def _usage_err(cmd: DesktopCommand, msg: str) -> DesktopParsedArgs:
    return DesktopParsedArgs(command=cmd, error=msg, code=EXIT_DESKTOP_USAGE)


def _next_value(rest: Sequence[str], idx: int) -> str | None:
    if idx + 1 >= len(rest):
        return None
    val = rest[idx + 1]
    return None if val.startswith(_HELP_FLAG_PREFIX) else val


def _parse_gnome_args(rest: Sequence[str]) -> DesktopParsedArgs:
    if not rest:
        return _usage_err(DesktopCommand.GNOME, "missing topic under `gnome` (expected `localsearch`)")
    if rest[0] in HELP_WORDS:
        return DesktopParsedArgs(command=DesktopCommand.HELP)
    topic, tail, topic_err = parse_subcommand_head(
        rest, GnomeTopic, default=GnomeTopic.HELP, help_action=GnomeTopic.HELP
    )
    if topic_err is not None or topic != GnomeTopic.LOCALSEARCH:
        return _usage_err(DesktopCommand.GNOME, topic_err or "expected `gnome localsearch`")
    action, flags, act_err = parse_subcommand_head(
        tail, LocalsearchAction, default=LocalsearchAction.STATUS, help_action=LocalsearchAction.HELP
    )
    if act_err is not None or action == LocalsearchAction.HELP or any(tok in HELP_WORDS for tok in flags):
        return (
            _usage_err(DesktopCommand.GNOME, act_err)
            if act_err is not None
            else DesktopParsedArgs(command=DesktopCommand.HELP)
        )
    mode = LocalsearchMode.RESTRICT
    idx = 0
    err: str | None = None
    while idx < len(flags) and err is None:
        tok = flags[idx]
        if tok == "--mode" and action in (LocalsearchAction.PLAN, LocalsearchAction.APPLY):
            raw_mode = _next_value(flags, idx)
            parsed_mode = enum_member(LocalsearchMode, raw_mode) if raw_mode is not None else None
            if parsed_mode is None:
                err = "invalid or missing --mode (expected restrict or disable)"
            else:
                mode = parsed_mode
                idx += 2
        else:
            err = f"unexpected argument for localsearch {action.value}: {tok}"
    return (
        _usage_err(DesktopCommand.GNOME, err)
        if err is not None
        else DesktopParsedArgs(command=DesktopCommand.GNOME, ls_action=action, mode=mode)
    )


def _parse_psd_flags(action: PsdAction, flags: Sequence[str]) -> DesktopParsedArgs:
    values: dict[_PsdFlag, str] = {}
    discard = False
    skip_busy = False
    idx = 0
    value_flags = {
        _PsdFlag.USER: {a for a in PsdAction if a != PsdAction.HELP},
        _PsdFlag.PROFILES: {PsdAction.PLAN, PsdAction.APPLY, PsdAction.START},
        _PsdFlag.SIZE: {PsdAction.PLAN, PsdAction.APPLY, PsdAction.START},
    }
    while idx < len(flags):
        flag = enum_member(_PsdFlag, flags[idx])
        if flag == _PsdFlag.SKIP_BUSY and action == PsdAction.START:
            skip_busy = True
            idx += 1
        elif flag == _PsdFlag.DISCARD and action == PsdAction.RECOVER:
            discard = True
            idx += 1
        elif flag is not None and action in value_flags.get(flag, ()):
            val = _next_value(flags, idx)
            if val is None:
                return _usage_err(DesktopCommand.PSD, f"missing value for {flag.value}")
            values[flag] = val
            idx += 2
        else:
            return _usage_err(DesktopCommand.PSD, f"unexpected argument for psd {action.value}: {flags[idx]}")
    return DesktopParsedArgs(
        command=DesktopCommand.PSD,
        psd_action=action,
        user=values.get(_PsdFlag.USER),
        profiles=values.get(_PsdFlag.PROFILES),
        size_text=values.get(_PsdFlag.SIZE),
        discard=discard,
        skip_busy=skip_busy,
    )


def _parse_psd_args(rest: Sequence[str]) -> DesktopParsedArgs:
    action, flags, act_err = parse_subcommand_head(
        rest, PsdAction, default=PsdAction.STATUS, help_action=PsdAction.HELP
    )
    if act_err is not None:
        return _usage_err(DesktopCommand.PSD, act_err)
    if action == PsdAction.HELP or any(tok in HELP_WORDS for tok in flags):
        return DesktopParsedArgs(command=DesktopCommand.HELP)
    return _parse_psd_flags(action, flags)


def _parse_status_args(rest: Sequence[str]) -> DesktopParsedArgs:
    if any(tok in HELP_WORDS for tok in rest):
        return DesktopParsedArgs(command=DesktopCommand.HELP)
    if len(rest) == _STATUS_USER_ARGC and rest[0] == _PsdFlag.USER and not rest[1].startswith(_HELP_FLAG_PREFIX):
        return DesktopParsedArgs(command=DesktopCommand.STATUS, user=rest[1])
    if rest:
        return _usage_err(DesktopCommand.STATUS, f"unexpected argument for status: {rest[0]}")
    return DesktopParsedArgs(command=DesktopCommand.STATUS)


def parse_desktop_args(argv: Sequence[str]) -> DesktopParsedArgs:
    """Parse `cst desktop ...` CLI arguments without touching I/O."""
    cmd, rest, head_err = parse_subcommand_head(
        argv, DesktopCommand, default=DesktopCommand.STATUS, help_action=DesktopCommand.HELP
    )
    if head_err is not None:
        return _usage_err(DesktopCommand.HELP, head_err)
    if cmd == DesktopCommand.HELP:
        return DesktopParsedArgs(command=DesktopCommand.HELP)
    if cmd == DesktopCommand.STATUS:
        return _parse_status_args(rest)
    if cmd == DesktopCommand.GNOME:
        return _parse_gnome_args(rest)
    return _parse_psd_args(rest)

# SPDX-License-Identifier: MPL-2.0
"""Pure specifications and CLI parser for `cst btrfs cache system` (`/var/mnt/cache/system/*`)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from cst_core.domain.text import HELP_WORDS, enum_member, parse_subcommand_head, strip_ascii_blanks

SYSTEM_CACHE_ROOT: Final = "/var/mnt/cache/system"
_EXIT_USAGE: Final = 2


class SystemBindId(StrEnum):
    """Supported system bind targets hosted on `/var/mnt/cache/system/` (never toolboxes)."""

    BELOW = "below"
    STALL_MONITOR = "stall-monitor"
    VAR_TMP = "var-tmp"


class SystemAction(StrEnum):
    """Subcommands under `cst btrfs cache system`."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"
    HELP = "help"


class SystemFlag(StrEnum):
    """Supported CLI options under `cst btrfs cache system`."""

    ONLY = "--only"
    ALLOW_NONEMPTY = "--allow-nonempty"
    MOUNT_VAR_TMP_NOW = "--mount-var-tmp-now"


@dataclass(frozen=True, slots=True)
class SystemBindSpec:
    """Static definition of one system-level bind mount onto `/var/mnt/cache/system/`."""

    bind_id: SystemBindId
    rel_dir: str
    target: str
    mode: int
    service: str = ""
    mount_live_by_default: bool = True

    @property
    def source(self) -> str:
        """Backing directory path on the NVMe cache filesystem."""
        return f"{SYSTEM_CACHE_ROOT}/{self.rel_dir}"


_SPEC_ROWS: Final[tuple[tuple[SystemBindId, str, str, int, str, bool], ...]] = (
    (SystemBindId.BELOW, "below", "/var/log/below", 0o755, "below.service", True),
    (
        SystemBindId.STALL_MONITOR,
        "btrfs-stall-monitor",
        "/var/log/btrfs-stall-monitor",
        0o750,
        "btrfs-stall-monitor.service",
        True,
    ),
    (SystemBindId.VAR_TMP, "var-tmp", "/var/tmp", 0o1777, "", False),
)
SYSTEM_BIND_SPECS: Final[tuple[SystemBindSpec, ...]] = tuple(SystemBindSpec(*row) for row in _SPEC_ROWS)
SYSTEM_SPEC_BY_ID: Final[dict[SystemBindId, SystemBindSpec]] = {s.bind_id: s for s in SYSTEM_BIND_SPECS}


@dataclass(frozen=True, slots=True)
class SystemArgsResult:
    """Parsed arguments for `cst btrfs cache system ...`."""

    action: SystemAction
    only: tuple[SystemBindId, ...] = ()
    allow_nonempty: bool = False
    mount_var_tmp_now: bool = False
    error: str | None = None
    code: int = 0


def _parse_only_list(raw: str) -> tuple[SystemBindId, ...] | None:
    cleaned = strip_ascii_blanks(raw)
    if not cleaned:
        return None
    out: list[SystemBindId] = []
    for tok in cleaned.split(","):
        item = enum_member(SystemBindId, strip_ascii_blanks(tok))
        if item is None:
            return None
        if item not in out:
            out.append(item)
    return tuple(out) if out else None


def _consume_only_flag(rest: Sequence[str], idx: int) -> tuple[tuple[SystemBindId, ...] | None, int]:
    tok = rest[idx]
    if tok.startswith(f"{SystemFlag.ONLY.value}="):
        return _parse_only_list(tok.split("=", 1)[1]), 1
    if idx + 1 >= len(rest):
        return None, 0
    return _parse_only_list(rest[idx + 1]), 2


def _system_usage_err(action: SystemAction, msg: str) -> SystemArgsResult:
    return SystemArgsResult(action=action, error=msg, code=_EXIT_USAGE)


def parse_system_args(argv: Sequence[str]) -> SystemArgsResult:
    """Parse arguments after `cst btrfs cache system` without performing I/O."""
    sub, rest, head_err = parse_subcommand_head(
        argv, SystemAction, default=SystemAction.STATUS, help_action=SystemAction.HELP
    )
    if head_err is not None:
        return _system_usage_err(SystemAction.HELP, head_err)
    if sub == SystemAction.HELP or any(tok in HELP_WORDS for tok in rest):
        return SystemArgsResult(action=SystemAction.HELP)

    only: tuple[SystemBindId, ...] = ()
    allow_nonempty = False
    mount_var_tmp_now = False
    idx = 0
    while idx < len(rest):
        tok = rest[idx]
        flag = enum_member(SystemFlag, tok.split("=", 1)[0])
        if flag == SystemFlag.ONLY and sub in (SystemAction.PLAN, SystemAction.APPLY, SystemAction.REVERT):
            parsed_only, step = _consume_only_flag(rest, idx)
            if parsed_only is None:
                valid = ", ".join(b.value for b in SYSTEM_SPEC_BY_ID)
                return _system_usage_err(sub, f"--only expects comma-separated IDs ({valid})")
            only = parsed_only
            idx += step
        elif flag == SystemFlag.ALLOW_NONEMPTY and sub in (SystemAction.PLAN, SystemAction.APPLY):
            allow_nonempty = True
            idx += 1
        elif flag == SystemFlag.MOUNT_VAR_TMP_NOW and sub in (SystemAction.PLAN, SystemAction.APPLY):
            mount_var_tmp_now = True
            idx += 1
        else:
            return _system_usage_err(sub, f"unknown or unsupported option {tok!r} for `system {sub.value}`")
    return SystemArgsResult(action=sub, only=only, allow_nonempty=allow_nonempty, mount_var_tmp_now=mount_var_tmp_now)

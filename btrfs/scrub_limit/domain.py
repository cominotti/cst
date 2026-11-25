# SPDX-License-Identifier: MPL-2.0
"""Pure domain rules for `cst btrfs scrub-limit`: parsing, validation, config rewriting, and reporting."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Final

from cst_core.domain.contracts import SNAPSHOTS_CONF
from cst_core.domain.text import DRY_RUN_LINE, HELP_WORDS, ShellAssignments, group, parse_shell_assignments

DEFAULT_SCRUB_LIMIT: Final = "500M"
SCRUB_CONF_PATH: Final = SNAPSHOTS_CONF
SCRUB_DROPIN_PATH: Final = "/etc/systemd/system/btrfs-scrub-weekly.service.d/10-cst-limit.conf"
SCRUB_SERVICE: Final = "btrfs-scrub-weekly.service"  # the unit the drop-in extends

_SCRUB_SIZE_RE: Final = re.compile(r"^\d+[KkMmGgTt]?$", re.ASCII)
_SCRUB_ASSIGN_RE: Final = re.compile(r"^\s*(?:export\s+)?SCRUB_LIMIT\s*(?:=|$)")
_SCRUB_DROPIN_RE: Final = re.compile(r"^ExecStartPre=/usr/sbin/btrfs scrub limit -a -l (\S+) /var$", re.MULTILINE)
_CONF_HEADER: Final = (
    "# Managed by btrfs-setup.sh (CST); read by /usr/local/sbin/btrfs-snapshot.\n"
    "# Manual edits of values are kept when btrfs-setup.sh updates other keys.\n"
)


class ScrubLimitAction(StrEnum):
    """Supported subcommands of `cst btrfs scrub-limit`."""

    STATUS = "status"
    PLAN = "plan"
    SET = "set"
    OFF = "off"
    HELP = "help"


@dataclass(frozen=True, slots=True)
class ScrubParsedArgs:
    """Validated command arguments for `cst btrfs scrub-limit`."""

    action: ScrubLimitAction
    limit: str | None = None
    error: str | None = None
    code: int = 0


@dataclass(frozen=True, slots=True)
class ScrubStatusSnapshot:
    """Inputs needed to format `cst btrfs scrub-limit status` output."""

    conf_path: str
    conf_text: str | None
    conf_unreadable: bool
    dropin_path: str
    dropin_text: str | None
    live_sysfs: tuple[str, ...]
    # NeedDaemonReload of SCRUB_SERVICE: True when systemd has not loaded its files as they are on disk;
    # None when systemctl cannot say (nothing is shown then)
    need_reload: bool | None = None


def format_scrub_usage(prog: str) -> str:
    """Return help and usage text for `prog`."""
    return (
        f"{prog}: manage the weekly btrfs scrub per-device speed limit (SCRUB_LIMIT).\n\n"
        "Commands:\n"
        "  status            read-only: show setting in snapshots.conf, drop-in, and sysfs\n"
        f"  plan [SIZE|off]   dry run showing what `set` (default {DEFAULT_SCRUB_LIMIT}) or `off` would change\n"
        f"  set [SIZE]        (root) set SCRUB_LIMIT (e.g. 500M, 1G; default {DEFAULT_SCRUB_LIMIT}) and update drop-in\n"
        "  off               (root) remove SCRUB_LIMIT and the scrub limit drop-in (full speed)\n"
        "  help              show this help\n\n"
        f"Usage: {prog} <status|plan|set|off|help> [SIZE]\n"
    )


def validate_scrub_size(raw: str) -> tuple[str | None, str | None]:
    """Validate a positive btrfs scrub limit size (`500M`, `1G`, ...); return `(size, error)`."""
    if not raw or raw in ("0", "0K", "0k", "0M", "0m", "0G", "0g", "0T", "0t"):
        return None, "limit must be greater than 0 (use 'off' to disable the scrub limit)"
    if _SCRUB_SIZE_RE.fullmatch(raw) is None:
        return None, f"{raw!r} is not a valid size such as 500M or 1G"
    return raw, None


def _parse_limit_arg(args: Sequence[str], *, allow_off: bool) -> tuple[str | None, str | None]:
    if not args:
        return DEFAULT_SCRUB_LIMIT, None
    if len(args) > 1:
        return None, f"unexpected argument {args[1]!r}"
    raw = args[0]
    if allow_off and raw in (ScrubLimitAction.OFF.value, "0"):
        return None, None
    size, err = validate_scrub_size(raw)
    if err is not None:
        return None, err
    return size, None


def _parse_no_arg(action: ScrubLimitAction, rest: Sequence[str]) -> ScrubParsedArgs:
    if rest:
        return ScrubParsedArgs(action, error=f"{action.value} takes no arguments (got {rest[0]!r})", code=2)
    return ScrubParsedArgs(action, limit=None)


def _parse_size_command(action: ScrubLimitAction, rest: Sequence[str], *, allow_off: bool) -> ScrubParsedArgs:
    if allow_off and rest and rest[0] in (ScrubLimitAction.OFF.value, "0"):
        if len(rest) > 1:
            return ScrubParsedArgs(action, error=f"unexpected argument {rest[1]!r}", code=2)
        return ScrubParsedArgs(action, limit=None)
    limit, err = _parse_limit_arg(rest, allow_off=allow_off)
    if err is not None:
        return ScrubParsedArgs(action, error=err, code=2)
    return ScrubParsedArgs(action, limit=limit)


def parse_scrub_limit_args(argv: Sequence[str]) -> ScrubParsedArgs:
    """Parse CLI arguments after `btrfs-scrub-limit`."""
    if not argv:
        return ScrubParsedArgs(ScrubLimitAction.STATUS)
    cmd = argv[0]
    rest = argv[1:]
    if cmd in HELP_WORDS or any(tok in HELP_WORDS for tok in rest):
        return ScrubParsedArgs(ScrubLimitAction.HELP)
    result: ScrubParsedArgs
    match cmd:
        case ScrubLimitAction.STATUS | ScrubLimitAction.OFF:
            result = _parse_no_arg(ScrubLimitAction(cmd), rest)
        case ScrubLimitAction.PLAN:
            result = _parse_size_command(ScrubLimitAction.PLAN, rest, allow_off=True)
        case ScrubLimitAction.SET:
            result = _parse_size_command(ScrubLimitAction.SET, rest, allow_off=False)
        case _:
            result = ScrubParsedArgs(ScrubLimitAction.HELP, error=f"unknown command {cmd!r}", code=2)
    return result


def render_scrub_dropin(limit: str, conf_path: str = SCRUB_CONF_PATH) -> str:
    """Render `btrfs-scrub-weekly.service.d/10-cst-limit.conf` matching `btrfs-setup.sh` byte-for-byte."""
    return (
        f"# Managed by btrfs-setup.sh (CST), from SCRUB_LIMIT in {conf_path}.\n"
        "# btrfs's own limit works with any IO scheduler (IOSchedulingClass=idle\n"
        "# needs BFQ or Kyber). sysfs keeps it only until the next mount, and the\n"
        "# reset runs after every run, also a failed or stopped one.\n"
        "[Service]\n"
        f"ExecStartPre=/usr/sbin/btrfs scrub limit -a -l {limit} /var\n"
        "ExecStopPost=-/usr/sbin/btrfs scrub limit -a -l 0 /var\n"
    )


def parse_scrub_dropin_limit(text: str | None) -> str:
    """Return the size configured in `10-cst-limit.conf`, or '' when absent."""
    if text is None or text == "":
        return ""
    m = _SCRUB_DROPIN_RE.search(text)
    return group(m, 1) if m is not None else ""


def read_scrub_conf_limit(conf_text: str | None) -> tuple[str, str]:
    """Read `(limit, problem)` from `snapshots.conf` (`limit=''` means unset/no limit)."""
    if conf_text is None:
        return "", ""
    assignments: ShellAssignments = parse_shell_assignments(conf_text)
    for prob in assignments.problems:
        if _SCRUB_ASSIGN_RE.match(prob.text):
            return "", f"{prob} is not a plain SCRUB_LIMIT=SIZE line"
    raw = assignments.values.get("SCRUB_LIMIT", "")
    if raw in ("", "0"):
        return "", ""
    if _SCRUB_SIZE_RE.fullmatch(raw) is None:
        return "", f"SCRUB_LIMIT={raw!r} is not a size such as 500M or 1G"
    return raw, ""


def update_snapshots_conf_scrub_limit(existing: str | None, limit: str | None) -> str | None:
    """Return updated `snapshots.conf` text with `SCRUB_LIMIT` set or removed, preserving other keys/comments."""
    if existing is None:
        return f"{_CONF_HEADER}SCRUB_LIMIT='{limit}'\n" if limit is not None else None
    lines = existing.splitlines()
    match_indices = [
        idx
        for idx, line in enumerate(lines)
        if not line.lstrip().startswith("#") and _SCRUB_ASSIGN_RE.match(line) is not None
    ]
    if limit is None:
        if not match_indices:
            return existing
        kept = [line for idx, line in enumerate(lines) if idx not in match_indices]
        return ("\n".join(kept) + "\n") if kept else ""
    desired_line = f"SCRUB_LIMIT='{limit}'"
    if not match_indices:
        updated = [*lines, desired_line]
    else:
        last_idx = match_indices[-1]
        drop = set(match_indices[:-1])
        updated = [desired_line if idx == last_idx else line for idx, line in enumerate(lines) if idx not in drop]
    return "\n".join(updated) + "\n"


def scrub_limit_status_lines(snapshot: ScrubStatusSnapshot) -> list[str]:
    """Format `cst btrfs scrub-limit status` output."""
    if snapshot.conf_unreadable:
        want_line = f"  setting: UNREADABLE ({snapshot.conf_path})"
        want: str | None = None
    else:
        setting, problem = read_scrub_conf_limit(snapshot.conf_text)
        if problem:
            want_line = f"  setting: INVALID: {problem} ({snapshot.conf_path})"
            want = None
        else:
            want_line = f"  setting: {setting or 'none'} (SCRUB_LIMIT in {snapshot.conf_path}; none = full speed)"
            want = setting
    have = parse_scrub_dropin_limit(snapshot.dropin_text)
    if want is None:
        svc_line = f"  service: {have or 'no limit'} ({snapshot.dropin_path})"
    elif want == have:
        svc_line = f"  service: {have or 'no limit'}, as set ({snapshot.dropin_path})"
    else:
        fix = f"set {want}" if want else "off"
        svc_line = f"  service: {have or 'no limit'}, DIFFERS from setting (run: cst btrfs scrub-limit {fix})"
    lines = [want_line, svc_line]
    if snapshot.need_reload is True:
        lines.append(f"  systemd: has not loaded {SCRUB_SERVICE} as it is on disk (run: sudo systemctl daemon-reload)")
    if snapshot.live_sysfs:
        lines.append(f"  now in sysfs (bytes/s, 0 = no limit): {', '.join(snapshot.live_sysfs)}")
    return lines


def scrub_limit_plan_reload_lines(
    *, dropin_text: str | None, target_limit: str | None, need_reload: bool | None
) -> list[str]:
    """The plan's line for the daemon-reload set or off runs although the drop-in stays as it is (the
    drop-in's own line names the reload when it changes): need_reload is NeedDaemonReload of SCRUB_SERVICE,
    True when systemd has not loaded its files as they are on disk, None when systemctl cannot say (set|off
    then asks again and reloads only if it cannot say either, or systemd answers yes: plan from a toolbox
    asks the host through flatpak-spawn, which can fail where set|off on the host does not)."""
    dropin_stays = dropin_text == (None if target_limit is None else render_scrub_dropin(target_limit))
    if need_reload is False or not dropin_stays:
        return []
    if need_reload:
        return [f"  reload:    {SCRUB_SERVICE} (systemd has not loaded it as it is on disk) + systemctl daemon-reload"]
    unknown = (
        f"  reload:    {SCRUB_SERVICE} (systemctl cannot say whether it is loaded) + systemctl daemon-reload,"
        " unless set|off then gets an answer that it is"
    )
    return [unknown]


def scrub_limit_plan_lines(
    *,
    conf_path: str,
    conf_text: str | None,
    dropin_path: str,
    dropin_text: str | None,
    target_limit: str | None,
) -> list[str]:
    """Format dry-run `cst btrfs scrub-limit plan` output."""
    lines = [DRY_RUN_LINE]
    new_conf = update_snapshots_conf_scrub_limit(conf_text, target_limit)
    if target_limit is None:
        if conf_text is None:
            lines.append(f"  unchanged: {conf_path} (absent)")
        elif new_conf == conf_text:
            lines.append(f"  unchanged: {conf_path} (SCRUB_LIMIT already unset)")
        else:
            lines.append(f"  update:    {conf_path} (remove SCRUB_LIMIT)")
        if dropin_text is None:
            lines.append(f"  unchanged: {dropin_path} (absent)")
        else:
            lines.append(f"  remove:    {dropin_path} + systemctl daemon-reload")
        return lines

    if conf_text is None:
        lines.append(f"  write:     {conf_path} (SCRUB_LIMIT='{target_limit}')")
    elif new_conf == conf_text:
        lines.append(f"  unchanged: {conf_path} (SCRUB_LIMIT='{target_limit}')")
    else:
        lines.append(f"  update:    {conf_path} -> SCRUB_LIMIT='{target_limit}'")
    want_dropin = render_scrub_dropin(target_limit)
    if dropin_text == want_dropin:
        lines.append(f"  unchanged: {dropin_path}")
    else:
        verb = "write:" if dropin_text is None else "update:"
        lines.append(f"  {verb:<10} {dropin_path} ({target_limit} per device) + systemctl daemon-reload")
    return lines

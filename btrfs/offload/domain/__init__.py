# SPDX-License-Identifier: MPL-2.0
"""Pure configuration parsing, path validation, presets, and CLI arg parsing for `cst btrfs cache offload`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import posixpath
import re
from typing import Final, TypedDict

from cst_core.domain.shapes import parse_toml

OFFLOAD_PROG_DEFAULT: Final = "cst btrfs cache offload"
OFFLOAD_PROG_REL: Final = ".local/lib/cst/btrfs-cache-offload"
OFFLOAD_CONF_REL: Final = ".config/cst/scratchpad-offload.toml"
OFFLOAD_STATE_REL: Final = ".local/state/cst/scratchpad-offload-backups.json"
OFFLOAD_LOCK_REL: Final = ".local/state/cst/scratchpad-offload.lock"
OFFLOAD_SCRATCHPAD_REL: Final = "Scratchpad"
SCRATCHPAD_MIRROR_ROOT_REL: Final = ".local/share/cst/scratchpad-mirrors"
OFFLOAD_DEFAULT_CALENDAR: Final = "daily"
OFFLOAD_ENTRY_MAX: Final = 64
OFFLOAD_STALE_BACKUP_DAYS: Final = 7

_NAME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_ALLOWED_TOP_KEYS: Final = frozenset({"version", "backup_on_calendar", "entries"})
_ALLOWED_ENTRY_KEYS: Final = frozenset(
    {"source", "scratchpad_subpath", "mode", "backup", "nocow_paths", "sqlite_globs"}
)
FORBIDDEN_HOME_PREFIXES: Final[tuple[tuple[str, str], ...]] = (
    (".local/share/containers", "podman toolboxes and containers must remain on RAID1"),
    (".local/share/cst", "holds CST per-user backups and internal state on /home"),
    ("Scratchpad", "already inside ~/Scratchpad"),
    (".cache", "already managed by `cst btrfs cache attach`"),
    ("Bulk", "already managed by `cst btrfs bulk`"),
    (".config/cst", "holds CST configuration files including scratchpad-offload.toml"),
    (".local/state/cst", "holds CST install and backup state on /home"),
    (".local/lib/cst", "holds CST installed helpers on /home"),
    (".config/systemd", "holds systemd unit files"),
)
DEFAULT_SQLITE_GLOBS: Final[tuple[str, ...]] = ("*.db", "*.sqlite", "*.sqlite3")


class OffloadAction(StrEnum):
    """Subcommands accepted by `cst btrfs cache offload`."""

    STATUS = "status"
    INIT = "init"
    PLAN = "plan"
    APPLY = "apply"
    BACKUP = "backup"
    REVERT = "revert"
    REFRESH = "refresh"
    HELP = "help"


class OffloadMode(StrEnum):
    """Storage policy for an offloaded tree inside `~/Scratchpad`."""

    ZSTD = "zstd"
    NOCOW = "nocow"


class OffloadEntryState(StrEnum):
    """On-disk state of one configured offload entry."""

    ACTIVE = "active"
    PENDING = "pending"
    UNLINKED = "unlinked"
    BROKEN_LINK = "broken_link"
    WRONG_TARGET = "wrong_target"
    CONFLICT = "conflict"


class OffloadHealthState(StrEnum):
    """Health severity for offload status and `cst doctor`."""

    OK = "ok"
    INFO = "info"
    WARN = "warn"


class OffloadBackupEntryRecord(TypedDict):
    """Last cold-mirror backup record for one entry."""

    last_backup_epoch: float
    sqlite_dbs: int
    ok: bool
    detail: str


class OffloadBackupStateRecord(TypedDict):
    """JSON schema persisted in `OFFLOAD_STATE_REL`."""

    version: int
    entries: dict[str, OffloadBackupEntryRecord]


@dataclass(frozen=True, slots=True)
class OffloadEntry:
    """One validated user path to offload onto `~/Scratchpad`."""

    name: str
    source_rel: str
    scratchpad_rel: str
    mirror_rel: str
    mode: OffloadMode
    backup: bool
    nocow_paths: tuple[str, ...]
    sqlite_globs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class OffloadConfig:
    """Parsed `scratchpad-offload.toml` content and any validation errors."""

    config_path: str
    entries: tuple[OffloadEntry, ...]
    backup_on_calendar: str = OFFLOAD_DEFAULT_CALENDAR
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class OffloadParsedArgs:
    """Parsed CLI options for `cst btrfs cache offload`."""

    action: OffloadAction = OffloadAction.STATUS
    config_path: str | None = None
    only: tuple[str, ...] = ()
    force: bool = False
    allow_busy: bool = False
    no_backup_timer: bool = False
    keep_mirror: bool = False
    error: str | None = None
    code: int = 2


def format_offload_usage(prog: str) -> str:
    """Usage and help text for `cst btrfs cache offload`."""
    return (
        "Offload hot user folders onto ~/Scratchpad via symlinks, per-path zstd or NOCOW, and daily /home mirrors.\n"
        "\n"
        "Commands:\n"
        "  status [--config PATH]               read-only: show ~/Scratchpad, entries, links, NOCOW, and backup timer\n"
        "  init [--config PATH] [--force]       write starter ~/.config/cst/scratchpad-offload.toml (nothing enabled)\n"
        "  plan [--config PATH] [OPTIONS]       dry run showing copy/symlink/NOCOW steps, backups, and blockers\n"
        "  apply [--config PATH] [OPTIONS]      move/copy configured dirs to ~/Scratchpad, swap symlinks, install timer\n"
        "  backup [--config PATH] [--only ...]  run online sqlite3 .backup() + rsync cold mirror to /home now\n"
        "  revert [--config PATH] [OPTIONS]     restore offloaded dirs back onto /home and remove symlinks\n"
        "  refresh                              (for `cst sync`) refresh helper and backup timer if configured\n"
        "  help                                 show this help\n"
        "\n"
        "Options:\n"
        "  --config PATH                        custom config path (default: ~/.config/cst/scratchpad-offload.toml)\n"
        "  --only NAME[,NAME...]                restrict plan, apply, backup, or revert to named entries\n"
        "  --force                              (init) overwrite an existing scratchpad-offload.toml\n"
        "  --allow-busy                         (plan/apply/revert) proceed even when processes hold open files there\n"
        "  --no-backup-timer                    (apply) skip enabling cst-scratchpad-backup.timer\n"
        "  --keep-mirror                        (revert) keep the cold mirror in ~/.local/share/cst/scratchpad-mirrors\n"
        "\n"
        "Notes:\n"
        "  - Runs as your normal user on the host (never inside a toolbox or as root).\n"
        "  - Toolboxes (~/.local/share/containers) and caches/Bulk/Scratchpad roots are rejected on purpose.\n"
        "\n"
        f"Usage: {prog} <status|init|plan|apply|backup|revert|refresh|help> [options]\n"
    )


def _split_only(raw: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in raw.split(",") if part.strip())


_ACTION_FLAGS: Final[Mapping[str, frozenset[OffloadAction]]] = {
    "--force": frozenset({OffloadAction.INIT}),
    "--only": frozenset({OffloadAction.PLAN, OffloadAction.APPLY, OffloadAction.BACKUP, OffloadAction.REVERT}),
    "--allow-busy": frozenset({OffloadAction.PLAN, OffloadAction.APPLY, OffloadAction.REVERT}),
    "--no-backup-timer": frozenset({OffloadAction.APPLY}),
    "--keep-mirror": frozenset({OffloadAction.REVERT}),
}


def _flag_allowed(action: OffloadAction, arg: str) -> bool:
    if arg in ("-h", "--help", "--config"):
        return True
    allowed = _ACTION_FLAGS.get(arg)
    return allowed is not None and action in allowed


def _parse_arg_step(
    action: OffloadAction,
    argv: Sequence[str],
    idx: int,
    flags: dict[str, bool],
    only: list[str],
) -> tuple[int, str | None, str | None]:
    arg = argv[idx]
    if not arg.startswith("-"):
        return idx, None, f"unexpected positional argument: {arg}"
    if not _flag_allowed(action, arg):
        return idx, None, f"{arg} is not valid with `{action.value}`"
    if arg in flags:
        flags[arg] = True
        return idx + 1, None, None
    if idx + 1 >= len(argv):
        return idx, None, f"{arg} requires a value"
    if arg == "--config":
        return idx + 2, argv[idx + 1], None
    only.extend(_split_only(argv[idx + 1]))
    return idx + 2, None, None


def parse_offload_args(argv: Sequence[str]) -> OffloadParsedArgs:
    """Parse CLI arguments for `cst btrfs cache offload` cleanly without sys.exit."""
    if not argv:
        return OffloadParsedArgs(action=OffloadAction.STATUS)
    first = argv[0]
    if first in ("-h", "--help"):
        return OffloadParsedArgs(action=OffloadAction.HELP)
    try:
        action = OffloadAction(first)
    except ValueError:
        return OffloadParsedArgs(error=f"unknown command: {first}")
    config_path: str | None = None
    only: list[str] = []
    flags = {"--force": False, "--allow-busy": False, "--no-backup-timer": False, "--keep-mirror": False}
    idx = 1
    while idx < len(argv):
        if argv[idx] in ("-h", "--help"):
            return OffloadParsedArgs(action=OffloadAction.HELP)
        idx, cfg_val, err = _parse_arg_step(action, argv, idx, flags, only)
        if err is not None:
            return OffloadParsedArgs(error=err)
        if cfg_val is not None:
            config_path = cfg_val
    return OffloadParsedArgs(
        action=action,
        config_path=config_path,
        only=tuple(only),
        force=flags["--force"],
        allow_busy=flags["--allow-busy"],
        no_backup_timer=flags["--no-backup-timer"],
        keep_mirror=flags["--keep-mirror"],
    )


def is_offload_help(parsed: OffloadParsedArgs) -> bool:
    """True when CLI requested help output."""
    return parsed.action == OffloadAction.HELP


def offload_parse_error(parsed: OffloadParsedArgs) -> tuple[int, str] | None:
    """Return `(exit_code, message)` when CLI parsing failed."""
    return None if parsed.error is None else (parsed.code, parsed.error)


def render_starter_config() -> str:
    """Render `~/.config/cst/scratchpad-offload.toml` with nothing active by default."""
    return (
        "# CST `cst btrfs cache offload` configuration.\n"
        "# Uncomment or add `[entries.<name>]` blocks to offload high-write user folders onto ~/Scratchpad.\n"
        "# NOTE: Podman toolboxes (`~/.local/share/containers`) are rejected by design and remain on RAID1.\n"
        "version = 1\n"
        'backup_on_calendar = "daily"\n'
        "\n"
        "# [entries.gemini_antigravity]\n"
        '# source = ".gemini/antigravity-cli"\n'
        '# scratchpad_subpath = "offload/gemini-antigravity"\n'
        '# mode = "zstd"\n'
        "# backup = true\n"
        '# nocow_paths = ["*.db", "*.sqlite", "*.sqlite3"]\n'
        '# sqlite_globs = ["*.db", "*.sqlite", "*.sqlite3"]\n'
        "\n"
        "# [entries.claude]\n"
        '# source = ".claude"\n'
        '# scratchpad_subpath = "offload/claude"\n'
        '# mode = "zstd"\n'
        "# backup = true\n"
        '# sqlite_globs = ["*.db", "*.sqlite", "*.sqlite3"]\n'
        "\n"
        "# [entries.codex]\n"
        '# source = ".codex"\n'
        '# scratchpad_subpath = "offload/codex"\n'
        '# mode = "zstd"\n'
        "# backup = true\n"
        '# sqlite_globs = ["*.db", "*.sqlite", "*.sqlite3"]\n'
        "\n"
        "# [entries.jetbrains]\n"
        '# source = ".local/share/JetBrains"\n'
        '# scratchpad_subpath = "offload/jetbrains"\n'
        '# mode = "zstd"\n'
        "# backup = true\n"
    )


def _normalize_home_rel(raw: str, field_name: str) -> tuple[str | None, str | None]:
    stripped = raw.strip()
    if stripped == "~":
        return None, f"{field_name} cannot be $HOME itself"
    stripped = stripped.removeprefix("~/")
    if not stripped or stripped.startswith("/") or "\x00" in stripped:
        return None, f"{field_name} must be a non-empty path relative to $HOME: {raw!r}"
    norm = posixpath.normpath(stripped)
    if norm in (".", "..") or norm.startswith("../"):
        return None, f"{field_name} escapes $HOME: {raw!r}"
    return norm, None


def _normalize_subpath(raw: str, field_name: str) -> tuple[str | None, str | None]:
    stripped = raw.strip()
    if not stripped or stripped.startswith("/") or "\x00" in stripped:
        return None, f"{field_name} must be a relative path: {raw!r}"
    norm = posixpath.normpath(stripped)
    if norm in (".", "..") or norm.startswith("../"):
        return None, f"{field_name} must stay inside its root directory: {raw!r}"
    return norm, None


def _forbidden_reason(source_rel: str) -> str | None:
    for prefix, reason in FORBIDDEN_HOME_PREFIXES:
        if _paths_overlap(source_rel, prefix):
            return f"source `{source_rel}` conflicts with `{prefix}`: {reason}"
    return None


def _parse_str_tuple(raw: object, field_name: str) -> tuple[tuple[str, ...], str | None]:
    if raw is None:
        return (), None
    if not isinstance(raw, list) or not all(
        isinstance(x, str) and x.strip() and not x.strip().startswith("/") for x in raw
    ):
        return (), f"{field_name} must be a list of non-empty relative path patterns"
    out: list[str] = []
    for item in raw:
        norm = posixpath.normpath(item.strip())
        if norm in (".", "..") or norm.startswith("../") or "\x00" in norm:
            return (), f"{field_name} contains an unsafe pattern: {item!r}"
        out.append(norm)
    return tuple(out), None


def _parse_mode(raw: object, name: str) -> tuple[OffloadMode | None, str | None]:
    if raw is None:
        return OffloadMode.ZSTD, None
    if not isinstance(raw, str):
        return None, f"entries.{name}.mode must be `zstd` or `nocow`"
    try:
        return OffloadMode(raw), None
    except ValueError:
        return None, f"entries.{name}.mode must be `zstd` or `nocow` (got {raw!r})"


def _parse_entry_paths(name: str, body: object) -> tuple[str | None, str | None, list[str]]:
    if not isinstance(body, dict):
        return None, None, [f"entries.{name} must be a TOML table"]
    problems = [f"entries.{name}: unknown key {k!r}" for k in body if k not in _ALLOWED_ENTRY_KEYS]
    src_raw = body.get("source")
    src_rel: str | None = None
    if not isinstance(src_raw, str):
        problems.append(f"entries.{name}.source must be a string path relative to $HOME")
    else:
        src_rel, src_err = _normalize_home_rel(src_raw, f"entries.{name}.source")
        if src_err is not None:
            problems.append(src_err)
        elif src_rel is not None and (forb := _forbidden_reason(src_rel)) is not None:
            problems.append(f"entries.{name}: {forb}")

    sp_raw = body.get("scratchpad_subpath", f"offload/{name}")
    sp_sub: str | None = None
    if not isinstance(sp_raw, str):
        problems.append(f"entries.{name}.scratchpad_subpath must be a string")
    else:
        sp_sub, sp_err = _normalize_subpath(sp_raw, f"entries.{name}.scratchpad_subpath")
        if sp_err is not None:
            problems.append(sp_err)
    return src_rel, sp_sub, problems


def _parse_one_entry(name: str, body: object) -> tuple[OffloadEntry | None, list[str]]:
    if not _NAME_RE.fullmatch(name):
        return None, [f"entry name {name!r} must match {_NAME_RE.pattern}"]
    if not isinstance(body, dict):
        return None, [f"entries.{name} must be a TOML table"]
    src_rel, sp_sub, problems = _parse_entry_paths(name, body)
    mode, mode_err = _parse_mode(body.get("mode"), name)
    if mode_err is not None:
        problems.append(mode_err)

    backup_raw = body.get("backup", True)
    if not isinstance(backup_raw, bool):
        problems.append(f"entries.{name}.backup must be a boolean")

    nocow_paths, nocow_err = _parse_str_tuple(body.get("nocow_paths"), f"entries.{name}.nocow_paths")
    if nocow_err is not None:
        problems.append(nocow_err)

    globs_raw = body.get("sqlite_globs")
    sqlite_globs, globs_err = _parse_str_tuple(globs_raw, f"entries.{name}.sqlite_globs")
    if globs_err is not None:
        problems.append(globs_err)
    elif globs_raw is None and backup_raw is True:
        sqlite_globs = DEFAULT_SQLITE_GLOBS

    if problems or src_rel is None or sp_sub is None or mode is None or not isinstance(backup_raw, bool):
        return None, problems
    return (
        OffloadEntry(
            name=name,
            source_rel=src_rel,
            scratchpad_rel=posixpath.join(OFFLOAD_SCRATCHPAD_REL, sp_sub),
            mirror_rel=posixpath.join(SCRATCHPAD_MIRROR_ROOT_REL, name),
            mode=mode,
            backup=backup_raw,
            nocow_paths=nocow_paths,
            sqlite_globs=sqlite_globs,
        ),
        [],
    )


def _paths_overlap(a: str, b: str) -> bool:
    return a == b or a.startswith(f"{b}/") or b.startswith(f"{a}/")


def _check_overlaps(entries: Sequence[OffloadEntry]) -> list[str]:
    problems: list[str] = []
    for i, first in enumerate(entries):
        for second in entries[i + 1 :]:
            if _paths_overlap(first.source_rel, second.source_rel):
                problems.append(
                    f"entries {first.name} (`{first.source_rel}`) and {second.name} (`{second.source_rel}`) overlap"
                )
            if _paths_overlap(first.scratchpad_rel, second.scratchpad_rel):
                problems.append(
                    f"entries {first.name} and {second.name} overlap inside ~/Scratchpad "
                    f"(`{first.scratchpad_rel}` vs `{second.scratchpad_rel}`)"
                )
    return problems


def parse_offload_config(raw: str, config_path: str) -> OffloadConfig:
    """Parse and validate `scratchpad-offload.toml` without touching the filesystem."""
    try:
        doc = parse_toml(raw)
    except ValueError as err:
        return OffloadConfig(config_path=config_path, entries=(), problems=(f"TOML syntax error: {err}",))
    if not isinstance(doc, dict):
        return OffloadConfig(config_path=config_path, entries=(), problems=("root TOML document must be a table",))

    problems = [f"unknown top-level key: {k!r}" for k in doc if k not in _ALLOWED_TOP_KEYS]
    version = doc.get("version", 1)
    if version != 1 or isinstance(version, bool):
        problems.append(f"unsupported version {version!r} (expected 1)")

    cal_raw = doc.get("backup_on_calendar", OFFLOAD_DEFAULT_CALENDAR)
    if not isinstance(cal_raw, str) or not cal_raw.strip() or any(ch in cal_raw for ch in "\r\n\x00"):
        problems.append("backup_on_calendar must be a single-line systemd OnCalendar expression")
        calendar = OFFLOAD_DEFAULT_CALENDAR
    else:
        calendar = cal_raw.strip()

    raw_entries = doc.get("entries", {})
    if not isinstance(raw_entries, dict):
        problems.append("`entries` must be a TOML table")
        return OffloadConfig(config_path=config_path, entries=(), backup_on_calendar=calendar, problems=tuple(problems))
    if len(raw_entries) > OFFLOAD_ENTRY_MAX:
        problems.append(f"too many entries ({len(raw_entries)} > {OFFLOAD_ENTRY_MAX})")

    parsed_entries: list[OffloadEntry] = []
    for raw_name, raw_body in raw_entries.items():
        if not isinstance(raw_name, str):
            problems.append("entry keys must be strings")
            continue
        entry, entry_problems = _parse_one_entry(raw_name, raw_body)
        problems.extend(entry_problems)
        if entry is not None:
            parsed_entries.append(entry)

    problems.extend(_check_overlaps(parsed_entries))
    return OffloadConfig(
        config_path=config_path,
        entries=tuple(parsed_entries),
        backup_on_calendar=calendar,
        problems=tuple(problems),
    )


def select_offload_entries(
    entries: Sequence[OffloadEntry], only: Sequence[str]
) -> tuple[tuple[OffloadEntry, ...], list[str]]:
    """Filter entries by `--only`, returning `(selected, unknown_names)`."""
    if not only:
        return tuple(entries), []
    by_name = {entry.name: entry for entry in entries}
    missing = [name for name in only if name not in by_name]
    seen: set[str] = set()
    selected: list[OffloadEntry] = []
    for name in only:
        if name in by_name and name not in seen:
            seen.add(name)
            selected.append(by_name[name])
    return tuple(selected), missing

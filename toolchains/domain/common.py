# SPDX-License-Identifier: MPL-2.0
"""The names and paths of cst-toolchains, the formats of its lines (sizes, ages,
~ paths), the settings syntax, JSON values, and the command line. No I/O.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from enum import StrEnum
from typing import TYPE_CHECKING, Final, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence, Set as AbstractSet

from cst_core.domain.errors import Usage
from cst_core.domain.install import manifest_by_src
from cst_core.domain.units import USER_UNIT_DIR_REL
from cst_core.domain.shapes import json_object, parse_toml
from cst_core.domain.text import (
    HELP_WORDS,
    NO_WORDS,
    YES_WORDS,
    enum_member,
    parse_shell_assignments,
    strip_ascii_blanks,
)
from cst_core.domain.values import (
    DAY,
    HOUR,
    MINUTE,
    WEEK,
    ByteSize,
    DomainValidationError,
    Duration,
    Json,
    format_bytes_gib_mib,
)


MARKER = "# managed-by: toolchains-setup.sh (CST)"
NAME = "cst-toolchains"
CONF_REL = ".config/cst/toolchains.conf"
# Left by `auto off` (and removed by `auto on`): `snapshots setup` then does
# not turn the timer back on (auto on --unless-off).
OFF_REL = ".config/cst/toolchains-auto.off"
# Where `auto on` installs the helper and its defaults (their manifest rows), relative to the home.
PROG_REL = manifest_by_src()["toolchains/cst-toolchains"].dest.removeprefix("~/")
LIB_REL = os.path.dirname(PROG_REL)
DEFAULTS_NAME = "toolchains.conf"
DEFAULTS_REL = manifest_by_src()["toolchains/toolchains.conf"].dest.removeprefix("~/")
UNIT_DIR_REL = USER_UNIT_DIR_REL
STATE_DIR_REL = ".local/state/cst"
SERVICE = "cst-toolchains-prune.service"
TIMER = "cst-toolchains-prune.timer"
# What the first version (`cst btrfs cargo-gc`, never deployed) installed:
# auto on and auto off remove it when its marker shows it is CST's.
OLD_MARKER = "# managed-by: btrfs-setup.sh (CST)"
OLD_TIMER = "cst-cargo-build-gc.timer"
OLD_UNITS = ("cst-cargo-build-gc.timer", "cst-cargo-build-gc.service")
OLD_FILES = (".local/lib/cst/cargo-build-gc", ".config/cst/cargo-gc.disabled")
OLD_DATA = (".local/state/cst/cargo-build-gc.json", ".local/state/cst/cargo-build-gc.lock")
OLD_CONF_REL = ".config/cst/cargo-gc.conf"
# Where systemd keeps a Persistent= user timer's last run (when the user manager has no
# XDG_DATA_HOME): `clean --what=state` removes it, but only while the timer is loaded.
TIMER_STAMP_DIR_REL = ".local/share/systemd/timers"


def timer_stamp_rel(name: str) -> str:
    """The stamp of user timer NAME, relative to the home."""
    return f"{TIMER_STAMP_DIR_REL}/stamp-{name}"


class ConfigError(Exception):
    pass


def gib(n: float) -> str:
    """GiB from 1 GiB on, else rounded MiB."""
    return format_bytes_gib_mib(n, mib_floor=False)


def ago(seconds: float) -> str:
    secs = max(0, int(seconds))
    if secs < HOUR:
        return f"{secs // MINUTE}m"
    if secs < 4 * DAY:
        return f"{secs / HOUR:.1f}h"
    return f"{secs / DAY:.1f}d"


def span(seconds: int) -> str:
    """A setting's duration, short: 48h, 2h, 90m, 14d."""
    if seconds % HOUR == 0 and seconds < WEEK:
        return f"{seconds // HOUR}h"
    if seconds % DAY == 0:
        return f"{seconds // DAY}d"
    if seconds % MINUTE == 0:
        return f"{seconds // MINUTE}m"
    return f"{seconds}s"


def when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def tilde(path: str, home: str) -> str:
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


SIZE_UNITS = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


def parse_duration(text: str) -> int:
    """Whole seconds of '<number><s|m|h|d|w>' (cst_core Duration.parse_float; the unit is required)."""
    try:
        return int(Duration.parse_float(text))
    except DomainValidationError:
        raise ConfigError(f"'{text}' is not a duration (a number with s, m, h, d or w, e.g. 48h)") from None


def parse_size(text: str) -> int:
    """Bytes (cst_core ByteSize.parse_iec). The unit is required (only 0 may
    go without): MAX_TOTAL=150 meant as 150G would otherwise be 150 bytes, a
    cap that deletes everything older than MIN_KEEP."""
    try:
        return ByteSize.parse_iec(text).bytes_count
    except DomainValidationError as e:
        raise ConfigError(str(e)) from None


# The words a yes/no setting takes (any case, blanks around ignored).
# The words that turn a rule with a duration off (parse_off_or_duration).
OFF_WORDS: Final = frozenset({"0", "off", "no", "none"})


def parse_bool(text: str) -> bool:
    """True for a YES_WORDS word, False for a NO_WORDS word (any case, ASCII blanks around it dropped: a
    no-break space is part of the word); ConfigError for anything else."""
    t = strip_ascii_blanks(text).lower()
    if t in YES_WORDS:
        return True
    if t in NO_WORDS:
        return False
    raise ConfigError(f"'{text}' is not yes or no")


def parse_conf(text: str, known: AbstractSet[str]) -> dict[str, str]:
    """{KEY: raw value} through cst_core's one settings reader (a file only
    Python reads: blanks around = allowed; '#' starts a comment, also after a
    value behind a blank; quotes around the value dropped). The first line
    that is not an assignment, or that sets a key not in KNOWN, is an error:
    a typo must not silently fall back to a default."""
    parsed = parse_shell_assignments(text, bash_sourced=False)
    issues = [(p.line, str(p)) for p in parsed.problems]
    issues += [
        (a.line, f"line {a.line}: unknown setting '{a.key}' (known: {', '.join(sorted(known))})")
        for a in parsed.entries
        if a.key not in known
    ]
    if issues:
        raise ConfigError(min(issues)[1])
    return dict(parsed.values)


def parse_sccache_size(text: str) -> str:
    """A size for SCCACHE_CACHE_SIZE, in sccache's own spelling: a whole
    number with K, M, G or T (sccache counts them in 1024s and knows no
    fractions); 0 is refused (sccache needs a cap)."""
    n = parse_size(text)
    if n <= 0:
        raise ConfigError(f"'{text}' is no cache size (sccache needs one, e.g. 25G)")
    for unit in "TGM":
        if n % SIZE_UNITS[unit.lower()] == 0:
            return f"{n // SIZE_UNITS[unit.lower()]}{unit}"
    return f"{-(-n // SIZE_UNITS['k'])}K"


class RustIncremental(StrEnum):
    """Policy for [build] incremental in ~/.cargo/config.toml (RUST_INCREMENTAL)."""

    AUTO = "auto"
    OFF = "off"


class RustBuildTmpfs(StrEnum):
    """Policy for using CST's dedicated tmpfs mount for cargo build-dir (RUST_BUILD_TMPFS)."""

    AUTO = "auto"
    YES = "yes"
    NO = "no"


def parse_rust_incremental(text: str) -> RustIncremental:
    """'auto' or 'off' (any case, ASCII blanks around dropped); ConfigError otherwise."""
    val = enum_member(RustIncremental, strip_ascii_blanks(text).lower())
    if val is None:
        raise ConfigError(f"'{text}' is not auto or off")
    return val


def parse_rust_build_tmpfs(text: str) -> RustBuildTmpfs:
    """'auto', 'yes' or 'no' (any case, ASCII blanks around dropped); ConfigError otherwise."""
    val = enum_member(RustBuildTmpfs, strip_ascii_blanks(text).lower())
    if val is None:
        raise ConfigError(f"'{text}' is not auto, yes or no")
    return val


# Settings that belong to no prune target: {KEY: parser}.
OPTIONS: dict[str, Callable[[str], bool | str]] = {
    "RUST_BUILD_TMPFS": parse_rust_build_tmpfs,
    "RUST_INCREMENTAL": parse_rust_incremental,
    "RUST_LEAN_DEBUGINFO": parse_bool,
    "RUST_SCCACHE": parse_bool,
    "RUST_SCCACHE_CACHE_SIZE": parse_sccache_size,
    "RUST_SCCACHE_WORKSPACE": parse_bool,
}


def parse_off_or_duration(text: str) -> int:
    """Seconds, or 0 for 0/off/no/none (the rule is off; any case, ASCII blanks around the word dropped)."""
    if strip_ascii_blanks(text).lower() in OFF_WORDS:
        return 0
    return parse_duration(text)


def json_key(key: object) -> str:
    """A key as json.dumps writes it; TypeError for one it refuses."""
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, bool):
        return json.dumps(key)
    if isinstance(key, int | float):
        return json.dumps(key) if isinstance(key, int) else float.__repr__(key)
    raise TypeError(f"keys must be str, int, float, bool or None, not {type(key).__name__}")


def to_json(value: object) -> Json:
    """VALUE as json.loads(json.dumps(VALUE)) gives it back (a tuple becomes a list, a key
    a string); TypeError for a value JSON cannot hold (a TOML date, ...), as json.dumps
    raises."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, dict):
        return {json_key(k): to_json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_json(v) for v in value]
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def json_items(value: Json) -> list[Json]:
    """The items `for x in VALUE or []` visits that can be objects: a list's items; a
    string or an object (its characters, its keys) gives none; a number that is not 0
    is not iterable (TypeError, as the loop raised)."""
    if value is None or not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str | dict):
        return []
    raise TypeError(f"'{type(value).__name__}' object is not iterable")


def plain(data: Mapping[str, object]) -> dict[str, Json]:
    """DATA as json.loads(json.dumps(DATA)) gives it back (see to_json)."""
    return {json_key(k): to_json(v) for k, v in data.items()}


def parses_as(text: str, expect: Mapping[str, object]) -> bool:
    try:
        data = parse_toml(text)
        return json_object(data) and plain(data) == expect
    except tomllib.TOMLDecodeError:
        return False


USAGE = """\
usage: cst-toolchains status [--brief]
       cst-toolchains prune plan|apply [TARGET...]
       cst-toolchains auto on [--unless-off] | auto off
       cst-toolchains config plan|apply [--lean-debuginfo | --no-lean-debuginfo] [--sccache | --no-sccache] | config init [--plan] [--force]
       cst-toolchains tmpfs status|plan|apply|revert [--size SIZE] [--user USER]
       (commands except tmpfs also take --home DIR; help: cst-toolchains help)"""


class ToolchainsCommand(StrEnum):
    """The commands of cst-toolchains (the first word of its command line)."""

    HELP = "help"
    STATUS = "status"
    PRUNE = "prune"
    AUTO = "auto"
    CONFIG = "config"
    TMPFS = "tmpfs"


class ToolchainsAction(StrEnum):
    """The word after a command: plan/apply/init/status/revert or on/off."""

    PLAN = "plan"
    APPLY = "apply"
    INIT = "init"
    ON = "on"
    OFF = "off"
    STATUS = "status"
    REVERT = "revert"


class ParsedArgs(TypedDict):
    cmd: ToolchainsCommand
    action: ToolchainsAction | None
    targets: list[str]
    brief: bool
    unless_off: bool
    home: str | None
    lean: bool | None
    sccache: bool | None
    plan: bool
    force: bool
    user: str | None
    size: str | None


def _split_home_option(argv: Sequence[str]) -> tuple[list[str], str | None]:
    """(ARGV without --home and its folder, that folder or None); raises Usage."""
    args: list[str] = []
    home: str | None = None
    it = iter(argv)
    for a in it:
        if a == "--home":
            home = next(it, None)
            if home is None or home == "":
                raise Usage("--home needs a folder")
        elif a.startswith("--home="):
            home = a.split("=", 1)[1]
            if not home:  # an empty value must never mean "your real home"
                raise Usage("--home needs a folder")
        else:
            args.append(a)
    return args, home


def _status_argv(rest: Sequence[str], out: ParsedArgs) -> None:
    """The arguments after `status`, into OUT; raises Usage."""
    for a in rest:
        if a != "--brief":
            raise Usage(f"status: unknown argument '{a}' (only --brief)")
        out["brief"] = True


def _prune_argv(rest: Sequence[str], out: ParsedArgs) -> None:
    """The arguments after `prune`, into OUT; raises Usage."""
    out["action"] = _action(rest, ToolchainsAction.PLAN, ToolchainsAction.APPLY)
    if out["action"] is None:
        raise Usage("prune: plan or apply, then the targets (none: every enabled one)")
    for a in rest[1:]:
        if a.startswith("-"):
            raise Usage(f"prune: unknown option '{a}'")
        out["targets"].append(a)


def _auto_argv(rest: Sequence[str], out: ParsedArgs) -> None:
    """The arguments after `auto`, into OUT; raises Usage."""
    out["action"] = _action(rest, ToolchainsAction.ON, ToolchainsAction.OFF)
    if out["action"] is None:
        raise Usage("auto: on or off")
    for a in rest[1:]:
        if a == "--unless-off" and out["action"] == ToolchainsAction.ON:
            out["unless_off"] = True
        else:
            raise Usage(f"auto {rest[0]}: unknown argument '{a}'")


def _config_argv(rest: Sequence[str], out: ParsedArgs) -> None:
    """The arguments after `config`, into OUT; raises Usage."""
    act = _action(rest, ToolchainsAction.PLAN, ToolchainsAction.APPLY, ToolchainsAction.INIT)
    if act is None:
        raise Usage("config: plan, apply or init [--plan] [--force]")
    out["action"] = act
    if act == ToolchainsAction.INIT:
        for a in rest[1:]:
            if a == "--plan" and not out["plan"]:
                out["plan"] = True
            elif a == "--force" and not out["force"]:
                out["force"] = True
            else:
                raise Usage(f"config init: unknown argument '{a}'")
        return
    for a in rest[1:]:
        if a in ("--lean-debuginfo", "--no-lean-debuginfo") and out["lean"] is None:
            out["lean"] = a == "--lean-debuginfo"
        elif a in ("--sccache", "--no-sccache") and out["sccache"] is None:
            out["sccache"] = a == "--sccache"
        else:
            raise Usage(f"config {rest[0]}: unknown argument '{a}'")


def _value_option(it: Iterator[str], flag: str, a: str) -> str:
    """Extract value from `--flag VALUE` or `--flag=VALUE`; raises Usage."""
    val = a.split("=", 1)[1] if "=" in a else next(it, None)
    if val is None or not val:
        raise Usage(f"{flag} needs a value")
    return val


def _tmpfs_argv(rest: Sequence[str], out: ParsedArgs) -> None:
    """Arguments after `tmpfs`, into OUT; raises Usage."""
    act = _action(rest, ToolchainsAction.STATUS, ToolchainsAction.PLAN, ToolchainsAction.APPLY, ToolchainsAction.REVERT)
    if act is None:
        raise Usage("tmpfs: status, plan, apply or revert [--size SIZE] [--user USER]")
    out["action"] = act
    it = iter(rest[1:])
    for a in it:
        if a == "--user" or a.startswith("--user="):
            if out["user"] is not None:
                raise Usage(f"tmpfs {act}: --user given twice")
            out["user"] = _value_option(it, "--user", a)
        elif a == "--size" or a.startswith("--size="):
            if act in (ToolchainsAction.STATUS, ToolchainsAction.REVERT):
                raise Usage(f"tmpfs {act}: --size only applies to plan or apply")
            if out["size"] is not None:
                raise Usage(f"tmpfs {act}: --size given twice")
            out["size"] = _value_option(it, "--size", a)
        else:
            raise Usage(f"tmpfs {act}: unknown argument '{a}'")


def parse_args(argv: Sequence[str]) -> ParsedArgs:
    """{cmd, action, targets, brief, unless_off, home}; raises Usage."""
    args, home = _split_home_option(argv)
    out: ParsedArgs = {
        "cmd": ToolchainsCommand.HELP,
        "action": None,
        "targets": [],
        "brief": False,
        "unless_off": False,
        "home": home,
        "lean": None,
        "sccache": None,
        "plan": False,
        "force": False,
        "user": None,
        "size": None,
    }
    if not args or args[0] in HELP_WORDS:
        return out
    cmd, rest = args[0], args[1:]
    command = enum_member(ToolchainsCommand, cmd)
    if command == ToolchainsCommand.TMPFS and home is not None:
        raise Usage("tmpfs manages a system mount unit and takes --user NAME, not --home DIR")
    match command:
        case ToolchainsCommand.STATUS:
            _status_argv(rest, out)
        case ToolchainsCommand.PRUNE:
            _prune_argv(rest, out)
        case ToolchainsCommand.AUTO:
            _auto_argv(rest, out)
        case ToolchainsCommand.CONFIG:
            _config_argv(rest, out)
        case ToolchainsCommand.TMPFS:
            _tmpfs_argv(rest, out)
        case _:
            raise Usage(f"unknown command '{cmd}'")
    out["cmd"] = command
    return out


def _action(rest: Sequence[str], *allowed: ToolchainsAction) -> ToolchainsAction | None:
    """The member of ALLOWED that REST's first word (argv's text) is, else None."""
    return next((a for a in allowed if rest and rest[0] == a), None)

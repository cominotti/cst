# SPDX-License-Identifier: MPL-2.0
"""The names and paths of cst-toolchains, the formats of its lines (sizes, ages,
~ paths), the settings syntax, JSON values, and the command line. No I/O.
"""

from __future__ import annotations

import json
import os
import time
import tomllib
from typing import Any, TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence, Set as AbstractSet

from cst_core.domain.errors import Usage
from cst_core.domain.install import manifest_by_src
from cst_core.domain.text import parse_shell_assignments
from cst_core.domain.values import ByteSize, DomainValidationError, Duration, format_bytes_gib_mib


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
UNIT_DIR_REL = ".config/systemd/user"
STATE_DIR_REL = ".local/state/cst"
SERVICE = "cst-toolchains-prune.service"
TIMER = "cst-toolchains-prune.timer"
# The link `enable` makes for the timer's WantedBy=timers.target.
WANTS_REL = f"{UNIT_DIR_REL}/timers.target.wants/{TIMER}"
# What the first version (`cst btrfs cargo-gc`, never deployed) installed:
# auto on and auto off remove it when its marker shows it is CST's.
OLD_MARKER = "# managed-by: btrfs-setup.sh (CST)"
OLD_TIMER = "cst-cargo-build-gc.timer"
OLD_UNITS = ("cst-cargo-build-gc.timer", "cst-cargo-build-gc.service")
OLD_FILES = (".local/lib/cst/cargo-build-gc", ".config/cst/cargo-gc.disabled")
OLD_DATA = (".local/state/cst/cargo-build-gc.json", ".local/state/cst/cargo-build-gc.lock")
OLD_CONF_REL = ".config/cst/cargo-gc.conf"


class ConfigError(Exception):
    pass


def gib(n: int | float) -> str:
    """GiB from 1 GiB on, else rounded MiB."""
    return format_bytes_gib_mib(n, mib_floor=False)


def ago(seconds: int | float) -> str:
    secs = max(0, int(seconds))
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 4 * 86400:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def span(seconds: int) -> str:
    """A setting's duration, short: 48h, 2h, 90m, 14d."""
    if seconds % 3600 == 0 and seconds < 7 * 86400:
        return f"{seconds // 3600}h"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
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


def parse_bool(text: str) -> bool:
    t = text.strip().lower()
    if t in ("yes", "true", "on", "1"):
        return True
    if t in ("no", "false", "off", "0"):
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


# Settings that belong to no prune target: {KEY: parser}.
OPTIONS: dict[str, Callable[[str], bool | str]] = {
    "RUST_LEAN_DEBUGINFO": parse_bool,
    "RUST_SCCACHE": parse_bool,
    "RUST_SCCACHE_CACHE_SIZE": parse_sccache_size,
    "RUST_SCCACHE_WORKSPACE": parse_bool,
}


def parse_off_or_duration(text: str) -> int:
    """Seconds, or 0 for 0/off/no (the rule is off)."""
    if text.strip().lower() in ("0", "off", "no", "none"):
        return 0
    return parse_duration(text)


def plain(data: Mapping[str, object]) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(json.dumps(data)))


def parses_as(text: str, expect: Mapping[str, object]) -> bool:
    try:
        return plain(tomllib.loads(text)) == expect
    except tomllib.TOMLDecodeError:
        return False


USAGE = """\
usage: cst-toolchains status [--brief]
       cst-toolchains prune plan|apply [TARGET...]
       cst-toolchains auto on [--unless-off] | auto off
       cst-toolchains config plan|apply [--lean-debuginfo | --no-lean-debuginfo] [--sccache | --no-sccache]
       (each also takes --home DIR; help: cst-toolchains help)"""


class ParsedArgs(TypedDict):
    cmd: str
    action: str | None
    targets: list[str]
    brief: bool
    unless_off: bool
    home: str | None
    lean: bool | None
    sccache: bool | None


def parse_args(argv: Sequence[str]) -> ParsedArgs:
    """{cmd, action, targets, brief, unless_off, home}; raises Usage."""
    args: list[str] = []
    home: str | None = None
    it = iter(argv)
    for a in it:
        if a == "--home":
            home = next(it, None)
            if not home:
                raise Usage("--home needs a folder")
        elif a.startswith("--home="):
            home = a.split("=", 1)[1]
            if not home:  # an empty value must never mean "your real home"
                raise Usage("--home needs a folder")
        else:
            args.append(a)
    out: ParsedArgs = {
        "cmd": "help",
        "action": None,
        "targets": [],
        "brief": False,
        "unless_off": False,
        "home": home,
        "lean": None,
        "sccache": None,
    }
    if not args or args[0] in ("help", "-h", "--help"):
        return out
    cmd, rest = args[0], args[1:]
    out["cmd"] = cmd
    if cmd == "status":
        for a in rest:
            if a != "--brief":
                raise Usage(f"status: unknown argument '{a}' (only --brief)")
            out["brief"] = True
    elif cmd == "prune":
        if not rest or rest[0] not in ("plan", "apply"):
            raise Usage("prune: plan or apply, then the targets (none: every enabled one)")
        out["action"] = rest[0]
        for a in rest[1:]:
            if a.startswith("-"):
                raise Usage(f"prune: unknown option '{a}'")
            out["targets"].append(a)
    elif cmd == "auto":
        if not rest or rest[0] not in ("on", "off"):
            raise Usage("auto: on or off")
        out["action"] = rest[0]
        for a in rest[1:]:
            if a == "--unless-off" and rest[0] == "on":
                out["unless_off"] = True
            else:
                raise Usage(f"auto {rest[0]}: unknown argument '{a}'")
    elif cmd == "config":
        if not rest or rest[0] not in ("plan", "apply"):
            raise Usage("config: plan or apply [--lean-debuginfo | --no-lean-debuginfo] [--sccache | --no-sccache]")
        out["action"] = rest[0]
        for a in rest[1:]:
            if a in ("--lean-debuginfo", "--no-lean-debuginfo") and out["lean"] is None:
                out["lean"] = a == "--lean-debuginfo"
            elif a in ("--sccache", "--no-sccache") and out["sccache"] is None:
                out["sccache"] = a == "--sccache"
            else:
                raise Usage(f"config {rest[0]}: unknown argument '{a}'")
    else:
        raise Usage(f"unknown command '{cmd}'")
    return out

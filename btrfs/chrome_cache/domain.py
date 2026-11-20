# SPDX-License-Identifier: MPL-2.0
"""chrome-cache's rules, no I/O: the flag lines, reading chrome-flags.conf's text and deciding what to write."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, TypedDict
import shlex

from cst_core.domain.values import GIB, MIB, format_bytes_gib_mib
from cst_core.domain.text import enum_member

APP = "com.google.Chrome"
CACHE_DIR = "/tmp/chrome-cache"
CACHE_SIZE = 512 * MIB  # 536870912: mandatory cap (shared tmpfs)
FLAG_DIR = f"--disk-cache-dir={CACHE_DIR}"
FLAG_SIZE = f"--disk-cache-size={CACHE_SIZE}"
APP_DIR = f".var/app/{APP}"
FLAGS_REL = f"{APP_DIR}/config/chrome-flags.conf"
OLD_CACHE_REL = f"{APP_DIR}/cache/google-chrome"
CACHE_SUBVOL_REL = f"{APP_DIR}/cache"
# status's line for absent flags when Chrome is installed.
ABSENT_ADDS = "absent; 'snapshots setup' (or 'snapshots chrome-cache apply') adds them"
MARK = "# btrfs-setup.sh (CST) chrome-cache"
# The marker as versions named f44-tools wrote it: recognised like the current
# one (a file it is in is not rewritten for the new name), never written.
MARKS = (MARK, "# btrfs-setup.sh (f44-tools) chrome-cache")
# No quotes or apostrophes: cobalt parses lines as shell words.
ON_LINE = (
    MARK + ": HTTP cache in the sandbox /tmp (RAM, emptied at reboot), at most 512 MiB."
    " Undo: delete the 2 lines below and keep this one (so setup does not add them again)."
)
OFF_LINE = (
    MARK + " off: setup adds no cache flags while this line is here"
    " (delete it, then run cst btrfs snapshots chrome-cache apply, to turn them on)."
)
# Where a system or user install of the app has its deployed files.
SYSTEM_INSTALLS = ["/var/lib/flatpak", "/run/host/var/lib/flatpak"]
USER_INSTALL_REL = ".local/share/flatpak"


class ChromeCacheCommand(StrEnum):
    """chrome-cache's commands (undo is another name for off)."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    OFF = "off"
    UNDO = "undo"


def chrome_command(word: str) -> ChromeCacheCommand | None:
    """The command the word names; None for any other word."""
    return enum_member(ChromeCacheCommand, word)


class ChromeFlagsState(StrEnum):
    """What decide finds in chrome-flags.conf (and apply's no-app)."""

    OFF = "off"  # off by the user's choice, no flags of theirs
    USER = "user"  # flags set by the user, or off with flags of theirs
    ABSENT = "absent"  # no flags: to be added
    MANAGED = "managed"  # our flags under our marker
    USER_SAME = "user-same"  # our values, written by the user
    CAP_MISSING = "cap-missing"  # our marker and folder flag, the size cap line gone
    NO_APP = "no-app"  # Chrome Flatpak not installed (apply only)


class _InspectResult(TypedDict):
    dir: str | None
    size: str | None
    on: bool
    off: bool


# ---- reading the flags file ---------------------------------------------------------------
def marked(s: str, rest: str) -> bool:
    """Whether the stripped line S starts with one of the MARKS followed by REST."""
    return any(s.startswith(m + rest) for m in MARKS)


def words(line: str) -> list[str]:
    """The shell words of one non-comment line (cobalt parses each line so)."""
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return line.split()


def flag_value(tokens: Sequence[str], name: str) -> str | None:
    """Value of --name=VALUE (the last one wins, as in Chrome); "" for a bare
    --name; None if absent."""
    val = None
    for t in tokens:
        if t == name:
            val = ""
        elif t.startswith(name + "="):
            val = t[len(name) + 1 :]
    return val


def inspect(text: str) -> _InspectResult:
    lines = text.splitlines()
    tokens, on, off = [], False, False
    for ln in lines:
        s = ln.strip()
        if marked(s, " off"):
            off = True
        elif marked(s, ":"):
            on = True
        if not s or s.startswith("#"):
            continue
        tokens += words(s)
    return {
        "dir": flag_value(tokens, "--disk-cache-dir"),
        "size": flag_value(tokens, "--disk-cache-size"),
        "on": on,
        "off": off,
    }


# What decide finds (NO_APP is apply's own answer, never decide's).
type DecidedState = Literal[
    ChromeFlagsState.OFF,
    ChromeFlagsState.USER,
    ChromeFlagsState.ABSENT,
    ChromeFlagsState.MANAGED,
    ChromeFlagsState.USER_SAME,
    ChromeFlagsState.CAP_MISSING,
]


def decide(text: str) -> tuple[DecidedState, str | None, str]:
    """(state, new_text or None, message). new_text None: nothing to write."""
    i = inspect(text)
    d, s = i["dir"], i["size"]
    ours_d, ours_s = d == CACHE_DIR, s == str(CACHE_SIZE)
    if i["off"] or (i["on"] and d is None):
        if d is None and s is None:
            return ChromeFlagsState.OFF, None, "off by your choice (marker without the flags): nothing added"
        return ChromeFlagsState.USER, None, f"off by your choice; your own cache flags there: {fmt(d, s)}"
    if d is None and s is None:
        return ChromeFlagsState.ABSENT, add_block(text), "add the cache flags (sandbox /tmp, at most 512 MiB)"
    if ours_d and ours_s:
        if i["on"]:
            return ChromeFlagsState.MANAGED, None, "flags present (managed by CST)"
        return ChromeFlagsState.USER_SAME, None, "flags present (written by you, same values): left as they are"
    if i["on"] and ours_d and s is None and has_line(text, FLAG_DIR):
        return (
            ChromeFlagsState.CAP_MISSING,
            add_after(text, FLAG_DIR, FLAG_SIZE),
            "the 512 MiB cap line is missing under the marker: add it back",
        )
    return ChromeFlagsState.USER, None, f"set by you ({fmt(d, s)}): left as it is{_ram_note(d, s)}"


def _ram_note(d: str | None, s: str | None) -> str:
    """The warning decide adds to flags of the user's own that put the cache in our RAM folder D
    with a size S above the cap or no cap at all; "" for any other flags."""
    if d != CACHE_DIR:
        return ""
    if s not in (None, "") and s.isdigit() and int(s) > CACHE_SIZE:
        return "; WARNING: that cache lives in RAM, keep it small"
    if s is None or s in ("", "0"):
        return "; WARNING: no size cap on a cache that lives in RAM (add --disk-cache-size=536870912)"
    return ""


def fmt(d: str | None, s: str | None) -> str:
    parts = []
    if d is not None:
        parts.append(f"--disk-cache-dir={d}")
    if s is not None:
        parts.append(f"--disk-cache-size={s}")
    return " ".join(parts) or "none"


def nl_end(text: str) -> str:
    return text if not text or text.endswith("\n") else text + "\n"


def add_block(text: str) -> str:
    return nl_end(text) + f"{ON_LINE}\n{FLAG_DIR}\n{FLAG_SIZE}\n"


def has_line(text: str, line: str) -> bool:
    return any(ln.strip() == line for ln in text.splitlines())


def add_after(text: str, anchor: str, new: str) -> str:
    """Insert line NEW right after the last line equal to ANCHOR."""
    lines = nl_end(text).splitlines(keepends=True)
    idx = max(n for n, ln in enumerate(lines) if ln.strip() == anchor)
    lines.insert(idx + 1, new + "\n")
    return "".join(lines)


def turn_off(text: str) -> str | None:
    """Our flag lines out (only if our marker is there), marker -> OFF_LINE;
    no marker at all: OFF_LINE appended. None if already off."""
    i = inspect(text)
    if i["off"]:
        return None
    lines = nl_end(text).splitlines(keepends=True)
    if not i["on"]:
        return nl_end(text) + OFF_LINE + "\n"
    out = []
    for ln in lines:
        s = ln.strip()
        if s in (FLAG_DIR, FLAG_SIZE):
            continue
        if marked(s, ":"):
            out.append(OFF_LINE + "\n")
            continue
        out.append(ln)
    return "".join(out)


def size_text(n: int) -> str:
    return format_bytes_gib_mib(n, gib_threshold=GIB // 10)


def off_message(text: str, new: str) -> str:
    """What turn_off changed (TEXT -> NEW), in words."""
    i, left = inspect(text), inspect(new)
    if left["dir"] is not None or left["size"] is not None:
        what = "our flag lines removed" if i["on"] else "off marker added"
        return (
            f"{what}, but cache flags not written by CST are still there"
            f" ({fmt(left['dir'], left['size'])}): left as they are; to turn them off,"
            " delete those lines by hand"
        )
    if not i["on"]:
        return "off marker added (there were no cache flags); setup will not add them"
    return "off: our flag lines removed (your other lines kept); setup will not add them again"

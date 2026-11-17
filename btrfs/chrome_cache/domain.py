# SPDX-License-Identifier: MPL-2.0
"""chrome-cache's rules, no I/O: the flag lines, reading chrome-flags.conf's text and deciding what to write."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict
import shlex

APP = "com.google.Chrome"
CACHE_DIR = "/tmp/chrome-cache"
CACHE_SIZE = 512 * 1024 * 1024  # 536870912: mandatory cap (shared tmpfs)
FLAG_DIR = f"--disk-cache-dir={CACHE_DIR}"
FLAG_SIZE = f"--disk-cache-size={CACHE_SIZE}"
APP_DIR = f".var/app/{APP}"
FLAGS_REL = f"{APP_DIR}/config/chrome-flags.conf"
OLD_CACHE_REL = f"{APP_DIR}/cache/google-chrome"
CACHE_SUBVOL_REL = f"{APP_DIR}/cache"
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


def decide(text: str) -> tuple[str, str | None, str]:
    """(state, new_text or None, message). new_text None: nothing to write."""
    i = inspect(text)
    d, s = i["dir"], i["size"]
    ours_d, ours_s = d == CACHE_DIR, s == str(CACHE_SIZE)
    if i["off"] or (i["on"] and d is None):
        if d is None and s is None:
            return "off", None, "off by your choice (marker without the flags): nothing added"
        return "user", None, f"off by your choice; your own cache flags there: {fmt(d, s)}"
    if d is None and s is None:
        return "absent", add_block(text), "add the cache flags (sandbox /tmp, at most 512 MiB)"
    if ours_d and ours_s:
        if i["on"]:
            return "managed", None, "flags present (managed by CST)"
        return "user-same", None, "flags present (written by you, same values): left as they are"
    if i["on"] and ours_d and s is None and has_line(text, FLAG_DIR):
        return (
            "cap-missing",
            add_after(text, FLAG_DIR, FLAG_SIZE),
            "the 512 MiB cap line is missing under the marker: add it back",
        )
    note = ""
    if ours_d and s not in (None, "") and s.isdigit() and int(s) > CACHE_SIZE:
        note = "; WARNING: that cache lives in RAM, keep it small"
    elif ours_d and (s is None or s in ("", "0")):
        note = "; WARNING: no size cap on a cache that lives in RAM (add --disk-cache-size=536870912)"
    return "user", None, f"set by you ({fmt(d, s)}): left as it is{note}"


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
    return nl_end(text) + "\n".join([ON_LINE, FLAG_DIR, FLAG_SIZE]) + "\n"


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
    return f"{n / 1024**3:.1f} GiB" if n >= 1024**3 // 10 else f"{n >> 20} MiB"


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

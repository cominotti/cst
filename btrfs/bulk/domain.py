# SPDX-License-Identifier: MPL-2.0
"""The rules of btrfs-bulk, with no I/O: the items that move into ~/Bulk
(Item, ITEMS), the CACHEDIR.TAG text, the size thresholds and texts, and
Steam's file formats (VDF text, libraryfolders.vdf and appmanifest rules).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import NoReturn
import os
import re

from cst_core.domain.values import format_bytes_gib_mib

NOTIFY = "/usr/local/sbin/btrfs-notify"
SUBVOL_CREATE = 0x5000940E  # BTRFS_IOC_SUBVOL_CREATE, struct btrfs_ioctl_vol_args
BTRFS_MAGIC = 0x9123683E
TAG_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"
TAG_TEXT = (
    TAG_SIGNATURE + "\n"
    "# This file is a cache directory tag (https://bford.info/cachedir/).\n"
    "# ~/Bulk is the CST bulk folder: everything in it can be downloaded\n"
    "# again. It is its own btrfs subvolume, left out of /home snapshots and of\n"
    "# the NAS backup (cst btrfs bulk status).\n"
)
STEAM_ROOT = ".var/app/com.valvesoftware.Steam/.local/share/Steam"  # the Flatpak's main library
STEAMAPPS = STEAM_ROOT + "/steamapps"
BIG = 1024**3  # old data from this size on is pointed out
MARGIN = 20 * BIG  # free space kept besides a real (NOCOW) copy


class Item:
    def __init__(
        self,
        ident: str,
        target: str,
        link: str | None = None,
        nocow: bool = False,
        after: str | None = None,
        flatpak: str | None = None,
        disposable: bool = False,
    ) -> None:
        self.id, self.target, self.link = ident, target, link
        self.nocow, self.after, self.flatpak = nocow, after, flatpak
        # A pure cache whose tool's own "clean" deletes the link (not what it
        # points to) and then fills a new real folder: the copy left in
        # ~/Bulk is stale, and apply deletes it instead of reporting
        # "data in both".
        self.disposable = disposable


# Order matters: an item with `after` waits until that one is in place
# (~/Bulk/apps must receive ~/Programs before anything is put inside it).
ITEMS = [
    Item("apps", "apps", "Programs"),
    # Only the IDE builds: Toolbox's accounts and settings stay in /home.
    Item("jetbrains", "apps/jetbrains", ".local/share/JetBrains/Toolbox/apps", after="apps"),
    # LM Studio (Flatpak) models; its conversations stay in /home.
    Item("models", "models", ".var/app/ai.lmstudio.lm-studio/.lmstudio/models", flatpak="ai.lmstudio.lm-studio"),
    # Emulator disk images, rewritten in place: the only NOCOW (+C) data.
    # Grouped by how the data is treated, not by product: everything below
    # emulators/ is +C (NOCOW_TOP), everything else in ~/Bulk is copy-on-write.
    Item("avd", "emulators/android/avd", ".android/avd", nocow=True),
    Item("iso", "iso", "Downloads/iso"),
    # A Steam library you add in Steam's settings; no link (saves stay in /home).
    Item("steam", "steam", flatpak="com.valvesoftware.Steam"),
    # Toolchains: only the re-downloadable caches; config and credentials stay.
    # Grouped as toolchains/<language>/<tool>/<the folder's own name>, so each
    # path reads like the one it replaces (~/.m2/repository -> jvm/m2/repository).
    # "jvm", not "java": Maven and Gradle also serve Kotlin, Scala and Groovy.
    # Parent folders are made on the way (u_mkdirs), as the user.
    # The Android SDK is the tool itself: toolchains/android/sdk.
    Item("android-sdk", "toolchains/android/sdk", "Android/Sdk"),
    Item("m2", "toolchains/jvm/m2/repository", ".m2/repository"),
    Item("gradle-caches", "toolchains/jvm/gradle/caches", ".gradle/caches"),
    Item("gradle-wrapper", "toolchains/jvm/gradle/wrapper", ".gradle/wrapper"),
    # `go clean -modcache` and `npm cache clean --force` remove the link itself.
    Item("go-mod", "toolchains/go/pkg/mod", "go/pkg/mod", disposable=True),
    Item("rustup", "toolchains/rust/rustup/toolchains", ".rustup/toolchains"),
    # rustup unpacks into ~/.rustup/tmp and rename(2)s the files into the
    # toolchain: a rename cannot cross subvolumes (EXDEV), so its scratch
    # folders live next to the toolchains (both empty between runs).
    Item("rustup-tmp", "toolchains/rust/rustup/tmp", ".rustup/tmp"),
    Item("rustup-downloads", "toolchains/rust/rustup/downloads", ".rustup/downloads"),
    Item("cargo-registry", "toolchains/rust/cargo/registry", ".cargo/registry"),
    Item("cargo-git", "toolchains/rust/cargo/git", ".cargo/git"),
    # Only npm's download cache: ~/.npm's logs, _npx and settings stay.
    Item("npm-cache", "toolchains/node/npm/_cacache", ".npm/_cacache", disposable=True),
]
BY_ID = {i.id: i for i in ITEMS}
# The NOCOW (+C) part of ~/Bulk: this folder and every folder below it get +C,
# so any emulator disk folder added later inherits it (+C on a folder only
# affects files created in it afterwards). Exactly the nocow items live here.
NOCOW_TOP = "emulators"


class Unexpected(Exception):
    """A state this tool does not change by itself."""


KEEP_HEADER = (
    "# Managed by btrfs-setup.sh bulk keep/unkeep (CST): ~/Bulk items kept in /home\n"
    "# by choice (one item id per line; '#' starts a comment). bulk apply leaves them alone.\n"
)


def gib(n: int) -> str:
    """GiB from 0.1 GiB on, else whole MiB (floored)."""
    return format_bytes_gib_mib(n, gib_threshold=BIG / 10)


@dataclass(frozen=True, slots=True)
class TreeSize:
    """What du counted under a path. complete is False when du could not
    read all of it: total is then only a lower bound."""

    total: int
    complete: bool


def du_text(size: TreeSize) -> str:
    """gib(total), as "at least ..." when part of the tree could not be read."""
    return gib(size.total) if size.complete else f"at least {gib(size.total)}"


def kept_text(item: Item, detail: str) -> str:
    where = "kept in /home (by choice)" if item.link else "left out (by choice)"
    if detail == "linked":
        return (
            f"{where}, but still a link into ~/Bulk: move the data back "
            "(btrfs/SNAPSHOTS.md, section 12), or 'bulk unkeep " + item.id + "'"
        )
    return where


# Ids whose problem in the last apply() lies inside ~/Bulk only: the in-place
# +C rewrite of a folder already linked into ~/Bulk failed or was refused
# before it wrote a journal (no free space, a copy check, foreign entries).
BULK_ONLY: set[str] = set()
STATE_TEXT = {
    "ok": "ok",
    "mkdir": "create the folder",
    "link": "create the folder and the link",
    "move-subvol": "a subvolume (earlier setup): renamed into ~/Bulk (instant), then the link",
    "wait": "waits",
}
# ---- Steam: move installed games into the ~/Bulk/steam library ---------------------------------
# `btrfs-bulk steam plan|apply [--app ID]...`. Steam must be closed. The game's
# folders and its manifest move from the Flatpak's main library into
# ~/Bulk/steam (a reflink copy, checked, then renames: the two are different
# subvolumes, so a plain rename fails with EXDEV; NOCOW data, which btrfs
# cannot clone into copy-on-write folders, is copied byte by byte and
# compared instead, only with --copy), then both copies of
# libraryfolders.vdf list the game under the new library. Anything unexpected
# (a VDF file in a form Steam does not write, a game mid-update, a Proton
# prefix with data, leftovers) is reported and nothing is touched for it.
# config/config.vdf (the default library, but also the login) is never read.
STEAM_APP = "com.valvesoftware.Steam"
STEAM_MARGIN = 5 * BIG  # free space kept in ~/Bulk (a reflink writes only metadata)
# A real copy (a NOCOW source, see steam_nocow_scan): its full size must be
# free, plus the snapshot emergency limit and MARGIN (steam_copy_floor). Progress lines: every PROGRESS_EVERY seconds or
# PROGRESS_BYTES copied/compared, whichever comes first (checked every
# PROGRESS_POLL seconds).
PROGRESS_EVERY, PROGRESS_BYTES, PROGRESS_POLL = 30, 5 * BIG, 5
CHUNK = 4 << 20  # read size of the content comparison
STEAM_BACKUPS = 5  # .cst-bak-* copies kept per libraryfolders.vdf
STEAM_READ_MAX = 64 << 20  # larger "VDF" files are refused unread
PROC = "/proc"
RUN_USER = "/run/user"
# Apps that are tools, not games: they stay in the main library unless named
# with --app. Checked: the Steam Linux Runtime ids are in Valve's own list
# (steam-runtime, doc/steamlinuxruntime-known-issues.md); 228980 is the
# Steamworks Common Redistributables (Valve's Steamworks docs, SteamDB).
# Proton (one app per version, plus the anti-cheat runtimes) is recognised by
# name and folder below instead of by id: those ids could not be checked.
# Could later read the authoritative "type" from appcache/appinfo.vdf (a
# binary format whose version changes).
KNOWN_TOOLS = {
    "228980": "Steamworks Common Redistributables",
    "1070560": "Steam Linux Runtime 1.0 (scout)",
    "1391110": "Steam Linux Runtime 2.0 (soldier)",
    "1628350": "Steam Linux Runtime 3.0 (sniper)",
    "4183110": "Steam Linux Runtime 4.0",
}
TOOL_DIR = re.compile(r"(SteamLinuxRuntime|Proton|Steamworks Shared)")
TOOL_NAME = re.compile(r"(Steam Linux Runtime|Proton\b|Steamworks Common Redistributables)")
# The keys a libraryfolders.vdf entry may have besides "path" and "apps".
LF_KEYS = ("label", "contentid", "totalsize", "update_clean_bytes_tally", "time_last_update_verified")
DECIMAL = re.compile(r"[0-9]+")
MAKE_DEFAULT = (
    "Once, by hand, so new games go there too: Steam > Settings > Storage > pick ~/Bulk/steam\n"
    "  in the drive list > '...' > Make default. (This tool never touches config/config.vdf,\n"
    "  which holds that setting but also your login.)"
)


class SteamRunning(Exception):
    """Steam (or something of it) runs: nothing is changed."""


def dec(s: object) -> bool:
    return isinstance(s, str) and DECIMAL.fullmatch(s) is not None


# ---- VDF (Valve's text KeyValues): pure functions, no I/O ----
def vdf_parse(data: bytes) -> dict[str, Any]:
    """Strict parser: quoted strings, braces, whitespace, the escapes \\\\ and
    \\". Anything else (unquoted text, comments, #include/#base, [$COND],
    other escapes, duplicate keys, unbalanced braces, trailing data, NUL,
    not UTF-8) raises Unexpected with the line and column. One root key."""
    if b"\0" in data:
        raise Unexpected(f"a NUL byte at offset {data.index(b'\0')}")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise Unexpected(f"not UTF-8 (byte {e.start})") from None
    n = len(text)

    def bad(p: int, what: str) -> NoReturn:
        line = text.count("\n", 0, p) + 1
        col = p - (text.rfind("\n", 0, p) + 1) + 1
        raise Unexpected(f"{what} at line {line}, column {col}")

    def ws(p: int) -> int:
        while p < n and text[p] in " \t\r\n":
            p += 1
        return p

    def token_error(p: int) -> NoReturn:
        if text.startswith("//", p):
            bad(p, "a comment")
        if text[p] == "#":
            bad(p, "a #include/#base directive")
        if text[p] == "[":
            bad(p, "a [$condition]")
        if text[p] == "{":
            bad(p, "a '{' without a key")
        bad(p, f"unquoted text {text[p]!r}")

    def string(p: int) -> tuple[str, int]:
        start, out = p, []
        p += 1
        while True:
            if p >= n or text[p] == "\n":
                bad(start, "an unterminated string")
            c = text[p]
            if c == '"':
                return "".join(out), p + 1
            if c == "\\":
                if p + 1 < n and text[p + 1] in '\\"':
                    out.append(text[p + 1])
                    p += 2
                    continue
                bad(p, "an unsupported escape")
            out.append(c)
            p += 1

    def pair(p: int, depth: int) -> tuple[int, str, str | dict[str, Any], int]:
        kp = p
        key, p = string(p)
        p = ws(p)
        if p >= n:
            bad(kp, "a key without a value")
        if text[p] == '"':
            val, p = string(p)
        elif text[p] == "{":
            val, p = obj(p + 1, depth + 1, p)
        else:
            token_error(p)
        return kp, key, val, p

    def obj(p: int, depth: int, open_at: int) -> tuple[dict[str, Any], int]:
        if depth > 32:
            bad(open_at, "nesting too deep")
        d: dict[str, Any] = {}
        while True:
            p = ws(p)
            if p >= n:
                bad(open_at, "an unbalanced '{'")
            if text[p] == "}":
                return d, p + 1
            if text[p] != '"':
                token_error(p)
            kp, key, val, p = pair(p, depth)
            if key in d:
                bad(kp, f"a duplicate key {key!r}")
            d[key] = val

    p = ws(0)
    if p >= n:
        raise Unexpected("empty")
    if text[p] == "}":
        bad(p, "an unbalanced '}'")
    if text[p] != '"':
        token_error(p)
    _, key, val, p = pair(p, 0)
    if not isinstance(val, dict):
        raise Unexpected("the root key has no '{ }' block")
    p = ws(p)
    if p < n:
        if text[p] == "}":
            bad(p, "an unbalanced '}'")
        bad(p, "trailing data")
    return {key: val}


def vdf_dump(tree: Mapping[str, Any]) -> bytes:
    """Steam's own layout: tab indent, "k"<tab><tab>"v", braces on their own
    lines, a newline at the end."""
    out: list[str] = []

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    def walk(d: Mapping[str, Any], ind: str) -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                out.append(f'{ind}"{esc(k)}"\n{ind}{{\n')
                walk(v, ind + "\t")
                out.append(f"{ind}}}\n")
            else:
                out.append(f'{ind}"{esc(k)}"\t\t"{esc(v)}"\n')

    walk(tree, "")
    return "".join(out).encode()


def vdf_load(data: bytes) -> dict[str, Any]:
    """vdf_parse, plus the round-trip guard: the file must be exactly what
    vdf_dump would write, so writing it back changes only what we change."""
    tree = vdf_parse(data)
    if vdf_dump(tree) != data:
        raise Unexpected("format not as expected (not laid out the way Steam writes it)")
    return tree


def acf_app(tree: Mapping[str, Any], appid: str) -> dict[str, str]:
    """Checks of appmanifest_<appid>.acf (parsed, never written)."""
    if list(tree) != ["AppState"] or not isinstance(tree["AppState"], dict):
        raise Unexpected("not an 'AppState' manifest")
    a = tree["AppState"]
    for k in ("appid", "name", "StateFlags", "installdir", "SizeOnDisk"):
        if not isinstance(a.get(k), str):
            raise Unexpected(f"the manifest lacks {k!r}")
    if a["appid"] != appid:
        raise Unexpected(f"the manifest says appid {a['appid']!r}")
    d = a["installdir"]
    if not d or any(x in d for x in ("/", "\\", "..", "\0")) or d == ".":
        raise Unexpected(f"odd installdir {d!r}")
    busy = []
    if a["StateFlags"] != "4":
        busy.append(f"StateFlags {a['StateFlags']}, not 4 (fully installed)")
    if a.get("StagingSize", "0") != "0":
        busy.append(f"StagingSize {a['StagingSize']}")
    for x, y in (("BytesDownloaded", "BytesToDownload"), ("BytesStaged", "BytesToStage")):
        if a.get(x, "") != a.get(y, ""):
            busy.append(f"{x} {a.get(x)} of {a.get(y)}")
    if a.get("ScheduledAutoUpdate", "0") != "0":
        busy.append("an update is scheduled")
    return {"name": a["name"], "installdir": d, "mid": "; ".join(busy)}


def is_tool(appid: str, name: str, installdir: str) -> str:
    """Why APPID is a tool (runtime, Proton, redistributables), or ''."""
    if appid in KNOWN_TOOLS:
        return KNOWN_TOOLS[appid]
    if TOOL_DIR.match(installdir) or TOOL_NAME.match(name):
        return name
    return ""


def lf_files(src: str) -> list[str]:
    """Both copies: config/ (the one current clients read) first."""
    return [os.path.join(src, "config", "libraryfolders.vdf"), os.path.join(src, "steamapps", "libraryfolders.vdf")]


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}h{s % 3600 // 60:02d}m{s % 60:02d}s" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


STEAM_USAGE = "usage: btrfs-bulk steam plan|apply [--app ID]... [--copy] [--user NAME]"

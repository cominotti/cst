# SPDX-License-Identifier: MPL-2.0
"""The rules of btrfs-bulk, with no I/O: the items that move into ~/Bulk
(Item, ITEMS), the home being handled (Ctx) and the program's paths
(BulkPaths), the CACHEDIR.TAG text, the size thresholds and texts, the
records of the journals, and Steam's file formats (VDF text, the
libraryfolders.vdf edits and the appmanifest rules).
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn
from typing import NotRequired
from typing import TypedDict
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


class BadSettings(Exception):
    """bulk.conf has a line that is not a plain KEY=VALUE: main() stops the
    run with exit 1 and the message on stderr. At boot, main() also sends it
    as an alert (nobody reads the boot console); lines names each bad line
    (file and line number)."""

    def __init__(self, lines: str) -> None:
        super().__init__(f"btrfs-bulk: {lines}\nFix the line (cst btrfs install writes the file) or pass --user NAME.")
        self.lines = lines


class NoUser(Exception):
    """As root, neither --user NAME nor BULK_USER names whose home it is (or
    it names root): main() stops the run with exit 1 and the message on
    stderr. Root must never guess whose home it moves."""


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
DECIMAL = re.compile(r"\d+", re.ASCII)
MAKE_DEFAULT = (
    "Once, by hand, so new games go there too: Steam > Settings > Storage > pick ~/Bulk/steam\n"
    "  in the drive list > '...' > Make default. (This tool never touches config/config.vdf,\n"
    "  which holds that setting but also your login.)"
)


class SteamRunning(Exception):
    """Steam (or something of it) runs: nothing is changed."""


# A parsed VDF file: every value is a string or a block of more.
type Vdf = dict[str, VdfValue]
type VdfValue = str | Vdf


def dec(s: object) -> bool:
    return isinstance(s, str) and DECIMAL.fullmatch(s) is not None


# ---- VDF (Valve's text KeyValues): pure functions, no I/O ----
class VdfText:
    """The strict parser of vdf_parse over one decoded text; p is an offset in it."""

    def __init__(self, text: str) -> None:
        self.text, self.n = text, len(text)

    def bad(self, p: int, what: str) -> NoReturn:
        line = self.text.count("\n", 0, p) + 1
        col = p - (self.text.rfind("\n", 0, p) + 1) + 1
        raise Unexpected(f"{what} at line {line}, column {col}")

    def ws(self, p: int) -> int:
        while p < self.n and self.text[p] in " \t\r\n":
            p += 1
        return p

    def token_error(self, p: int) -> NoReturn:
        text = self.text
        if text.startswith("//", p):
            self.bad(p, "a comment")
        if text[p] == "#":
            self.bad(p, "a #include/#base directive")
        if text[p] == "[":
            self.bad(p, "a [$condition]")
        if text[p] == "{":
            self.bad(p, "a '{' without a key")
        self.bad(p, f"unquoted text {text[p]!r}")

    def string(self, p: int) -> tuple[str, int]:
        text, n = self.text, self.n
        start, out = p, []
        p += 1
        while True:
            if p >= n or text[p] == "\n":
                self.bad(start, "an unterminated string")
            c = text[p]
            if c == '"':
                return "".join(out), p + 1
            if c == "\\":
                if p + 1 < n and text[p + 1] in '\\"':
                    out.append(text[p + 1])
                    p += 2
                    continue
                self.bad(p, "an unsupported escape")
            out.append(c)
            p += 1

    def pair(self, p: int, depth: int) -> tuple[int, str, VdfValue, int]:
        kp = p
        key, p = self.string(p)
        p = self.ws(p)
        if p >= self.n:
            self.bad(kp, "a key without a value")
        if self.text[p] == '"':
            val, p = self.string(p)
        elif self.text[p] == "{":
            val, p = self.obj(p + 1, depth + 1, p)
        else:
            self.token_error(p)
        return kp, key, val, p

    def obj(self, p: int, depth: int, open_at: int) -> tuple[Vdf, int]:
        if depth > 32:
            self.bad(open_at, "nesting too deep")
        d: Vdf = {}
        while True:
            p = self.ws(p)
            if p >= self.n:
                self.bad(open_at, "an unbalanced '{'")
            if self.text[p] == "}":
                return d, p + 1
            if self.text[p] != '"':
                self.token_error(p)
            kp, key, val, p = self.pair(p, depth)
            if key in d:
                self.bad(kp, f"a duplicate key {key!r}")
            d[key] = val

    def document(self) -> Vdf:
        """The one root key and its block, nothing after it."""
        text, n = self.text, self.n
        p = self.ws(0)
        if p >= n:
            raise Unexpected("empty")
        if text[p] == "}":
            self.bad(p, "an unbalanced '}'")
        if text[p] != '"':
            self.token_error(p)
        _, key, val, p = self.pair(p, 0)
        if not isinstance(val, dict):
            raise Unexpected("the root key has no '{ }' block")
        p = self.ws(p)
        if p < n:
            if text[p] == "}":
                self.bad(p, "an unbalanced '}'")
            self.bad(p, "trailing data")
        return {key: val}


def vdf_parse(data: bytes) -> Vdf:
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
    return VdfText(text).document()


def vdf_dump(tree: Mapping[str, VdfValue]) -> bytes:
    """Steam's own layout: tab indent, "k"<tab><tab>"v", braces on their own
    lines, a newline at the end."""
    out: list[str] = []

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    def walk(d: Mapping[str, VdfValue], ind: str) -> None:
        for k, v in d.items():
            if isinstance(v, dict):
                out.append(f'{ind}"{esc(k)}"\n{ind}{{\n')
                walk(v, ind + "\t")
                out.append(f"{ind}}}\n")
            else:
                out.append(f'{ind}"{esc(k)}"\t\t"{esc(v)}"\n')

    walk(tree, "")
    return "".join(out).encode()


def vdf_load(data: bytes) -> Vdf:
    """vdf_parse, plus the round-trip guard: the file must be exactly what
    vdf_dump would write, so writing it back changes only what we change."""
    tree = vdf_parse(data)
    if vdf_dump(tree) != data:
        raise Unexpected("format not as expected (not laid out the way Steam writes it)")
    return tree


def acf_app(tree: Vdf, appid: str) -> dict[str, str]:
    """Checks of appmanifest_<appid>.acf (parsed, never written)."""
    a = tree.get("AppState")
    if list(tree) != ["AppState"] or not isinstance(a, dict):
        raise Unexpected("not an 'AppState' manifest")
    checked: dict[str, str] = {}
    for k in ("appid", "name", "StateFlags", "installdir", "SizeOnDisk"):
        v = a.get(k)
        if not isinstance(v, str):
            raise Unexpected(f"the manifest lacks {k!r}")
        checked[k] = v
    if checked["appid"] != appid:
        raise Unexpected(f"the manifest says appid {checked['appid']!r}")
    d = checked["installdir"]
    if not d or any(x in d for x in ("/", "\\", "..", "\0")) or d == ".":
        raise Unexpected(f"odd installdir {d!r}")
    busy = []
    if checked["StateFlags"] != "4":
        busy.append(f"StateFlags {checked['StateFlags']}, not 4 (fully installed)")
    if a.get("StagingSize", "0") != "0":
        busy.append(f"StagingSize {a['StagingSize']}")
    for x, y in (("BytesDownloaded", "BytesToDownload"), ("BytesStaged", "BytesToStage")):
        if a.get(x, "") != a.get(y, ""):
            busy.append(f"{x} {a.get(x)} of {a.get(y)}")
    if a.get("ScheduledAutoUpdate", "0") != "0":
        busy.append("an update is scheduled")
    return {"name": checked["name"], "installdir": d, "mid": "; ".join(busy)}


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


@dataclass(frozen=True, slots=True)
class SteamArgs:
    """btrfs-bulk steam plan|apply [--app ID]... [--copy] [--user NAME], parsed."""

    sub: str
    ids: tuple[str, ...]
    user_args: tuple[str, ...]
    allow_copy: bool


def parse_steam_args(args: Sequence[str]) -> SteamArgs | tuple[str, ...]:
    """The steam command's arguments, or the lines for stderr (exit 2)."""
    sub = args[0] if args else ""
    ids: list[str] = []
    user_args: tuple[str, ...] = ()
    i = 1
    allow_copy = False
    while i < len(args):
        a = args[i]
        if a == "--copy":
            allow_copy = True
            i += 1
            continue
        if a == "--app" and i + 1 < len(args) and DECIMAL.fullmatch(args[i + 1]) and len(args[i + 1]) <= 10:
            ids.append(str(int(args[i + 1])))
        elif a == "--user" and i + 1 < len(args):
            user_args = (a, args[i + 1])
        else:
            value = f" {args[i + 1]!r}" if a == "--app" and i + 1 < len(args) else ""
            return (f"btrfs-bulk steam: bad argument {a!r}{value}", STEAM_USAGE)
        i += 2
    if sub not in ("plan", "apply"):
        return (STEAM_USAGE,)
    return SteamArgs(sub, tuple(ids), user_args, allow_copy)


def split_user_args(args: Sequence[str]) -> tuple[list[str], list[str]]:
    """(the other arguments, ["--user", NAME] or [])."""
    if "--user" in args:
        i = args.index("--user")
        return list(args[:i]) + list(args[i + 2 :]), list(args[i : i + 2])
    return list(args), []


# ---- the home being handled, the program's paths ----
@dataclass(frozen=True, slots=True)
class Ctx:
    """Whose home: home is its real path (symlinks resolved), uid the
    owner's id (None: the user running this program, asked when needed);
    separate: every change in it runs in a child process of its own that
    dropped to the user (a run as root)."""

    home: str
    uid: int | None = None
    separate: bool = False

    @property
    def bulk(self) -> str:
        return os.path.join(self.home, "Bulk")

    def target(self, item: Item) -> str:
        return os.path.join(self.bulk, item.target)

    def link(self, item: Item) -> str | None:
        return os.path.join(self.home, item.link) if item.link else None

    def short(self, path: str) -> str:
        return "~" + path[len(self.home) :] if path.startswith(self.home + "/") else path


@dataclass(frozen=True, slots=True, kw_only=True)
class BulkPaths:
    """Where btrfs-bulk keeps its settings and state; under() puts them into
    other folders (tests and dry runs; never as root)."""

    etc: str = "/etc/btrfs-setup"
    conf: str = "/etc/btrfs-setup/bulk.conf"
    flag: str = "/etc/btrfs-setup/bulk-at-boot"
    keep: str = "/etc/btrfs-setup/bulk-keep"
    convert_plan: str = "/etc/btrfs-setup/convert-plan"
    snapshots_conf: str = "/etc/btrfs-setup/snapshots.conf"
    notify: str = NOTIFY
    state: str = "/var/lib/btrfs-bulk"
    attempts: str = "/var/lib/btrfs-bulk/boot-attempts"
    lock: str = "/run/btrfs-snapshot.lock"  # shared with btrfs-snapshot

    @classmethod
    def under(cls, *, etc: str, state: str, lock: str, notify: str = NOTIFY) -> BulkPaths:
        """The same files in the folders etc and state, the lock at lock."""
        return cls(
            etc=etc,
            conf=os.path.join(etc, "bulk.conf"),
            flag=os.path.join(etc, "bulk-at-boot"),
            keep=os.path.join(etc, "bulk-keep"),
            convert_plan=os.path.join(etc, "convert-plan"),
            snapshots_conf=os.path.join(etc, "snapshots.conf"),
            notify=notify,
            state=state,
            attempts=os.path.join(state, "boot-attempts"),
            lock=lock,
        )


def present[T](value: T | None, what: str) -> T:
    """value, which the steps before have set; a TypeError if they have not
    (what the old code raised one step later, using the None)."""
    if value is None:
        raise TypeError(f"{what} is not set")
    return value


# ---- the journals (root-owned JSON in BulkPaths.state; read back by any later version) ----
# The field order of these TypedDicts was chosen so that no eight lines repeat
# another record (tests/test_duplication.py, type 2): reordering them can fail
# that gate. JSON keys do not depend on it.
class ItemJournal(TypedDict):
    """journal-<id>.json of one item's move. kind: copy | subvol."""

    kind: str
    src: str
    target: str
    new: str
    stamp2: NotRequired[int]
    bulk_only: NotRequired[bool]
    old: str
    stamp: int
    conflict: NotRequired[bool]
    undo: NotRequired[bool]
    link: str | None
    finishing: NotRequired[bool]


class SteamPart(TypedDict):
    """One part of a Steam game that moves (the game folder, its workshop
    content and file, the manifest). mode: reflink | copy."""

    name: str
    kind: str
    src: str
    mode: NotRequired[str]
    dst: str
    bytes: NotRequired[int]
    new: str
    sample: NotRequired[str]
    old: str


class SteamJournal(TypedDict):
    """journal-steam-<appid>.json. phase: prepared | switching | switched | finishing."""

    kind: str
    appid: str
    src_lib: str
    dst_lib: str
    size: str
    stamp2: NotRequired[int]
    contentid: str
    stamp: int
    phase: str
    parts: list[SteamPart]
    conflict: bool
    undo: bool


class SteamApp(TypedDict):
    """One app of the plan: state move | ok | skip | update | look."""

    id: str
    name: str
    state: str
    parts: list[SteamPart]
    why: str
    size: str
    bytes: int
    copy_bytes: int
    reflink_bytes: int
    nocow_files: int
    nocow_sample: str


class VdfPair(TypedDict):
    """Both libraryfolders.vdf as read (raw, mtime) and the tree they hold."""

    files: list[str]
    tree: Vdf
    repair: bool
    raw: list[bytes]
    ts: str
    backed_up: bool
    mtime: list[int]


class FileRead(TypedDict):
    """A regular file read as the user: its bytes (also as base64), mtime (ns), mode and owner."""

    b64: str
    data: bytes
    mtime: int
    mode: int
    uid: int


class SteamSurvey(TypedDict):
    """Everything steam plan prints and steam apply needs."""

    fatal: str
    running: list[str]
    nothing: bool
    space: str
    copy: bool
    S: VdfPair | None
    apps: list[SteamApp]
    idx: str | None
    copy_bytes: int
    cid: str | None
    free: int


# ---- libraryfolders.vdf: the checks and edits (pure; resolve is os.path.realpath) ----
def vdf_block(value: VdfValue | None, what: str) -> Vdf:
    """value as a block; Unexpected when it is a string (lf_validate has
    ruled that out for every tree the edits below get)."""
    if not isinstance(value, dict):
        raise Unexpected(f"{what} is not a block")
    return value


def vdf_text(value: VdfValue | None, what: str) -> str:
    """value as a string; Unexpected when it is a block."""
    if not isinstance(value, str):
        raise Unexpected(f"{what} is not a value")
    return value


def vdf_copy(tree: Vdf) -> Vdf:
    """A deep copy (blocks copied, strings shared)."""
    return {k: v if isinstance(v, str) else vdf_copy(v) for k, v in tree.items()}


def lf_libraries(tree: Vdf) -> Vdf:
    """The "libraryfolders" block of a checked tree."""
    return vdf_block(tree.get("libraryfolders"), "libraryfolders")


def lf_check_apps(idx: str, v: VdfValue) -> None:
    """The apps block of library IDX: decimal app ids and sizes."""
    if not isinstance(v, dict):
        raise Unexpected(f"library {idx}: 'apps' is not a block")
    for a, s in v.items():
        if not (dec(a) and dec(s)):
            raise Unexpected(f"library {idx}: app {a!r} {s!r} is not decimal")


def lf_check_key(idx: str, k: str, v: VdfValue) -> None:
    """One key of library IDX's entry."""
    if k == "apps":
        lf_check_apps(idx, v)
    elif k == "path":
        if not isinstance(v, str) or not os.path.isabs(v):
            raise Unexpected(f"library {idx}: 'path' is not an absolute path")
    elif k in LF_KEYS:
        if not isinstance(v, str):
            raise Unexpected(f"library {idx}: {k!r} is a block, not a value")
    else:
        raise Unexpected(f"library {idx}: unknown key {k!r}")


def lf_check_entry(idx: str, e: VdfValue) -> str:
    """Library IDX's entry, checked; its path."""
    if not isinstance(e, dict):
        raise Unexpected(f"library {idx} is not a block")
    for k, v in e.items():
        lf_check_key(idx, k, v)
    if "path" not in e or "apps" not in e:
        raise Unexpected(f"library {idx} lacks 'path' or 'apps'")
    return vdf_text(e["path"], f"library {idx}: 'path'")


def lf_check_apart(real: Sequence[tuple[str, str]]) -> None:
    """No two libraries (number, resolved path) are one folder, or one inside the other."""
    for i, (ia, a) in enumerate(real):
        for ib, b in real[i + 1 :]:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                raise Unexpected(f"libraries {ia} and {ib} are the same folder, or one is inside the other")


def lf_validate(tree: Vdf, resolve: Callable[[str], str]) -> Vdf:
    """Schema of libraryfolders.vdf; returns its "libraryfolders" dict."""
    lf = tree.get("libraryfolders")
    if list(tree) != ["libraryfolders"] or not isinstance(lf, dict):
        raise Unexpected("the top level is not exactly one 'libraryfolders' block")
    if not lf or list(lf) != [str(i) for i in range(len(lf))]:
        raise Unexpected(f"library numbers are not 0, 1, 2... in order: {', '.join(lf) or 'none'}")
    lf_check_apart([(idx, resolve(lf_check_entry(idx, e))) for idx, e in lf.items()])
    return lf


def lf_index(tree: Vdf, path: str, resolve: Callable[[str], str]) -> str | None:
    """The number of the entry whose path is the same folder as PATH, or
    None. Compared after resolving symlinks, like lf_validate does, so that
    /home/u/Bulk/steam and /var/home/u/Bulk/steam (Silverblue: /home is a
    symlink to /var/home) count as one library, not two."""
    want = resolve(path)
    for idx, e in lf_libraries(tree).items():
        if resolve(vdf_text(vdf_block(e, f"library {idx}").get("path"), f"library {idx}: 'path'")) == want:
            return idx
    return None


def lf_with_library(tree: Vdf, path: str, contentid: str, resolve: Callable[[str], str]) -> Vdf:
    """TREE with an entry for PATH appended (a new tree); unchanged if listed."""
    new = vdf_copy(tree)
    if lf_index(tree, path, resolve) is None:
        lf = lf_libraries(new)
        lf[str(max(int(i) for i in lf) + 1)] = {
            "path": path,
            "label": "",
            "contentid": contentid,
            "totalsize": "0",
            "update_clean_bytes_tally": "0",
            "time_last_update_verified": "0",
            "apps": {},
        }
    return new


def lf_place_app(tree: Vdf, appid: str, lib_path: str, size: str, resolve: Callable[[str], str]) -> Vdf:
    """A new tree where APPID is listed only under LIB_PATH's entry (with
    SIZE, the string as it was); idempotent."""
    new = vdf_copy(tree)
    idx = lf_index(new, lib_path, resolve)
    if idx is None:
        raise Unexpected(f"{lib_path} is not a library in libraryfolders.vdf")
    for i, e in lf_libraries(new).items():
        apps = vdf_block(vdf_block(e, f"library {i}").get("apps"), f"library {i}: 'apps'")
        if i == idx:
            if apps.get(appid) != size:
                apps[appid] = size
        else:
            apps.pop(appid, None)
    return new


def lf_apps(tree: Vdf, idx: str) -> Vdf:
    """The apps block of library IDX of a checked tree."""
    return vdf_block(vdf_block(lf_libraries(tree).get(idx), f"library {idx}").get("apps"), f"library {idx}: 'apps'")


# ---- texts ----
def row_text(name: str, text: str) -> str:
    """One line of plan/status; a long name gets its own line."""
    if len(name) > 56:
        return f"  {name}\n  {'':<56} {text}"
    return f"  {name:<56} {text}"


def describe_paths(ctx: Ctx, item: Item) -> str:
    t = ctx.short(ctx.target(item))
    return f"~/{item.link} -> {t}" if item.link else t


def conflict_text(ctx: Ctx, j: ItemJournal) -> str:
    return (
        f"written to on both sides during the move: compare {ctx.short(j['old'])} (the old "
        f"tree) with {ctx.short(j['target'])} (in use now), copy what you need into the "
        f"second, delete the first, then run bulk apply again"
        + ("; both inside ~/Bulk, no data of it in /home: snapshots do not wait for it" if j.get("bulk_only") else "")
    )


def steam_nocow_files(ctx: Ctx, a: SteamApp) -> str:
    """'K file(s) ... have +C (NOCOW), for example PATH' for a game with a
    real copy."""
    k = a["nocow_files"]
    return (
        f"{k} file{'s' if k != 1 else ''} in it ha{'ve' if k != 1 else 's'} +C (NOCOW), for example "
        f"{ctx.short(a['nocow_sample'])}"
    )


def steam_move_lines(ctx: Ctx, a: SteamApp, allow_copy: bool) -> list[str]:
    """What plan says about a game that moves: reflink, real copy, or both."""
    c, r = a["copy_bytes"], a["reflink_bytes"]
    out: list[str] = []
    if r or not c:
        out.append(f"move {gib(r)} (reflink, no data written)")
    if c:
        out.extend(
            [
                f"copy {gib(c)} byte by byte: {steam_nocow_files(ctx, a)};",
                "btrfs cannot reflink those, so the folders holding them are rewritten into COW ~/Bulk/steam",
                "(gaining checksums and compression)",
            ]
        )
        if not allow_copy:
            out.append("add --copy to allow it")
    if len(out) > 1 and r:
        out[0] += " and"
    return out

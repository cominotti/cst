# SPDX-License-Identifier: MPL-2.0
"""The rules of btrfs-bulk, with no I/O: the items that move into ~/Bulk
(Item, ITEMS), the home being handled (Ctx) and the program's paths
(BulkPaths), the CACHEDIR.TAG text, the size thresholds and texts, and the
records of an item's journal. Steam's file formats and the records of a
game's move are in steam.py.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from typing import Literal
from typing import NotRequired
from typing import TypedDict
import os

from cst_core.domain.contracts import NOTIFY_PROGRAM, StateFormatKey, state_format
from cst_core.domain.errors import ExitStatus
from cst_core.domain.values import HOUR, MINUTE, format_bytes_gib_mib
from cst_core.domain.text import enum_member

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


class BulkCommand(StrEnum):
    """btrfs-bulk's commands (argv[1]); plan and apply are also steam's, plan and status report's modes."""

    PLAN = "plan"
    APPLY = "apply"
    STATUS = "status"
    BOOT = "boot"
    KEEP = "keep"
    UNKEEP = "unkeep"
    STEAM = "steam"


def bulk_command(word: str) -> BulkCommand | None:
    """The command argv names; None for any other word (the usage, exit 2)."""
    return enum_member(BulkCommand, word)


class LstatKind(StrEnum):
    """What lstat finds at a path (HomeTreePort.kind), no link followed."""

    ABSENT = "absent"
    LINK = "link"
    DIR = "dir"
    OTHER = "other"


class PathChain(StrEnum):
    """HomeTreePort.chain over every component of base/rel; its other answers
    are 'symlink:<path>' and 'file:<path>' (the first that is not a folder)."""

    OK = "ok"
    ABSENT = "absent"


class BulkTargetState(StrEnum):
    """HomeTreePort.target_state of ~/Bulk/<target>; its other answer is 'bad:<reason>'."""

    ABSENT = "absent"
    EMPTY = "empty"
    DATA = "data"


class BulkDirState(StrEnum):
    """HomeTreePort.bulk_state: ~/Bulk itself."""

    OK = "ok"
    CREATE = "create"
    PLAIN = "plain"
    BAD = "bad"


class CacheTagState(StrEnum):
    """HomeTreePort.tag_state: ~/Bulk/CACHEDIR.TAG."""

    OK = "ok"
    CREATE = "create"
    BAD = "bad"


class FlatpakAccess(StrEnum):
    """May the app use a path: UserFilesPort.flatpak (absent, ok, added) and flatpak_state (absent, ok, missing)."""

    ABSENT = "absent"
    OK = "ok"
    ADDED = "added"
    MISSING = "missing"


class BulkItemState(StrEnum):
    """classify's state of an item."""

    KEPT = "kept"
    OK = "ok"
    MKDIR = "mkdir"
    LINK = "link"
    MOVE = "move"
    MOVE_SUBVOL = "move-subvol"
    NOCOW = "nocow"
    WAIT = "wait"
    UNEXPECTED = "unexpected"


class BulkItemDetail(StrEnum):
    """The details of classify that the code tells apart (the others are text: why it waits or needs a look)."""

    NOLINK = "nolink"
    LINKED = "linked"
    EMPTY = "empty"
    STALE = "stale"
    SUBVOLUME = "subvolume"


class BulkItemResult(StrEnum):
    """What act did with an item; its other answers are 'unexpected:<why>' and 'failed:<why>'."""

    OK = "ok"
    BUSY = "busy"
    CHANGED = "changed"
    KEPT = "kept"
    WAIT = "wait"


class Unexpected(Exception):
    """A state this tool does not change by itself."""


class BadSettings(Exception):
    """bulk.conf has a line that is not a plain KEY=VALUE, or its BULK_USER
    names no account: main() stops the run with exit 1 and the message on
    stderr. At boot, boot_run also sends it as an alert (nobody reads the boot
    console) and exits BOOT_REFUSED_EXIT; lines names each bad line (file and
    line number) or the BULK_USER (file)."""

    def __init__(self, lines: str) -> None:
        super().__init__(f"btrfs-bulk: {lines}\nFix the line (cst btrfs install writes the file) or pass --user NAME.")
        self.lines = lines


class NoUser(Exception):
    """As root, neither --user NAME nor BULK_USER names whose home it is (or
    it names root), or --user NAME names no account: main() stops the run with
    exit 1 (BOOT_REFUSED_EXIT at boot) and the message on stderr. Root must
    never guess whose home it moves."""


# The exit status of btrfs-bulk boot when bulk.conf cannot tell whose home it is (a bad line, no
# user): EX_CONFIG of sysexits.h, as docker-gc's. It is not in the SuccessExitStatus= of
# btrfs-bulk-boot.service (1 3 4), so the unit fails and cst doctor lists it; 1, 3 and 4 keep
# their meaning.
BOOT_REFUSED_EXIT: Final = ExitStatus.BAD_SETTINGS
# The exit status of btrfs-bulk boot after an error it did not expect (its traceback goes to the
# unit's journal): EX_SOFTWARE of sysexits.h, not in SuccessExitStatus= either. Python's own exit
# status for an uncaught exception, 1, counted as success there.
BOOT_CRASHED_EXIT: Final = 70


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
    where = "kept in /home (by choice)" if item.link is not None else "left out (by choice)"
    if detail == BulkItemDetail.LINKED:
        return (
            f"{where}, but still a link into ~/Bulk: move the data back "
            "(btrfs/SNAPSHOTS.md, section 12), or 'bulk unkeep " + item.id + "'"
        )
    return where


STATE_TEXT = {
    BulkItemState.OK: "ok",
    BulkItemState.MKDIR: "create the folder",
    BulkItemState.LINK: "create the folder and the link",
    BulkItemState.MOVE_SUBVOL: "a subvolume (earlier setup): renamed into ~/Bulk (instant), then the link",
    BulkItemState.WAIT: "waits",
}
# A real copy (a NOCOW source, see steam_nocow_scan): its full size must be
# free, plus the snapshot emergency limit and MARGIN (steam_copy_floor). Progress lines: every PROGRESS_EVERY seconds or
# PROGRESS_BYTES copied/compared, whichever comes first (checked every
# PROGRESS_POLL seconds). Here, not in steam.py: adapters/machine.py's copy loop reads
# them, and hms.
PROGRESS_EVERY, PROGRESS_BYTES, PROGRESS_POLL = 30, 5 * BIG, 5


def hms(seconds: float) -> str:
    s = int(seconds)
    return (
        f"{s // HOUR}h{s % HOUR // MINUTE:02d}m{s % MINUTE:02d}s" if s >= HOUR else f"{s // MINUTE}m{s % MINUTE:02d}s"
    )


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
    owner's id (None: the user running this program, asked when needed)."""

    home: str
    uid: int | None = None

    @property
    def bulk(self) -> str:
        return os.path.join(self.home, "Bulk")

    def target(self, item: Item) -> str:
        return os.path.join(self.bulk, item.target)

    def link(self, item: Item) -> str | None:
        return os.path.join(self.home, item.link) if item.link is not None else None

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
    notify: str = NOTIFY_PROGRAM
    state: str = state_format(StateFormatKey.BULK_JOURNAL).folder
    attempts: str = "/var/lib/btrfs-bulk/boot-attempts"
    lock: str = "/run/btrfs-snapshot.lock"  # shared with btrfs-snapshot

    @classmethod
    def under(cls, *, etc: str, state: str, lock: str, notify: str = NOTIFY_PROGRAM) -> BulkPaths:
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
class BulkJournalKind(StrEnum):
    """The kind of a journal: an item's copy or subvolume move, a Steam game's move."""

    COPY = "copy"
    SUBVOL = "subvol"
    STEAM = "steam"


class ItemJournal(TypedDict):
    """journal-<id>.json of one item's move (is_item_journal checks kind)."""

    kind: Literal[BulkJournalKind.COPY, BulkJournalKind.SUBVOL]
    src: str
    target: str
    new: str
    old: str
    link: str | None
    stamp: int
    bulk_only: NotRequired[bool]
    stamp2: NotRequired[int]
    conflict: NotRequired[bool]
    undo: NotRequired[bool]
    finishing: NotRequired[bool]


# How a damaged journal is reported (recovery and report, of items and Steam games alike).
DAMAGED = "unreadable, or not as btrfs-bulk writes it: left in place, nothing it names was touched"


def damaged_text(path: str) -> str:
    """What NEEDS A LOOK says of the journal at path, which is not as btrfs-bulk writes it."""
    return f"damaged journal {path} ({DAMAGED})"


# ---- texts ----
def row_text(name: str, text: str) -> str:
    """One line of plan/status; a long name gets its own line."""
    if len(name) > 56:
        return f"  {name}\n  {'':<56} {text}"
    return f"  {name:<56} {text}"


def describe_paths(ctx: Ctx, item: Item) -> str:
    t = ctx.short(ctx.target(item))
    return f"~/{item.link} -> {t}" if item.link is not None else t


def conflict_text(ctx: Ctx, j: ItemJournal) -> str:
    return (
        f"written to on both sides during the move: compare {ctx.short(j['old'])} (the old "
        f"tree) with {ctx.short(j['target'])} (in use now), copy what you need into the "
        f"second, delete the first, then run bulk apply again"
        + (
            "; both inside ~/Bulk, no data of it in /home: snapshots do not wait for it"
            if j.get("bulk_only", False)
            else ""
        )
    )

# SPDX-License-Identifier: MPL-2.0
"""btrfs-snapshot-runtime's rules, as btrfs/btrfs-snapshot (bash) had them: the settings and their
defaults, btrfs-snapshot-core's command lines, and the texts of the journal lines and alerts. No I/O.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
import json
import re
from typing import Final

from btrfs.health_check.domain import bash_int

TAG: Final = "btrfs-snapshot"
GIB: Final = 1024**3
USAGE: Final = (
    "usage: btrfs-snapshot hourly|nas|convert|convert-boot|list [--json]|caches [--json]|prune [--dry-run] "
    "[--allow-mass-delete]|purge|planned-left|busy [--home DIR] PATH...|convert-one PATH"
)
NOT_SET_UP: Final = "not set up; run 'cst btrfs snapshots setup'"
EXIT_USAGE: Final = 2
# stat -c %i of a btrfs subvolume's root folder.
SUBVOL_ROOT_INODE: Final = "256"


DAY_S: Final = 86400
PURGE_BATCH: Final = 10
# How many paths an alert names (head -n 3).
SHOWN: Final = 3


class CoreExit(IntEnum):
    """btrfs-snapshot-core's exit status."""

    OK = 0
    ERROR = 1
    USAGE = 2
    LOCK_BUSY = 3
    BLOCKED = 4  # a safety check blocked a creation or deletion


class GuardError(StrEnum):
    """A space-guard setting that is no integer, as bash arithmetic under set -u treats it: a word
    that starts like a name (abc: an unset variable, the run stops), or anything else that is no
    number (08, 2.5, 250GiB: an error line, and the comparison is false)."""

    BAD_NUMBER = "bad-number"
    NOT_A_NUMBER = "not-a-number"


def guard_number(text: str) -> int | GuardError:
    """A space-guard setting (GiB) as (( free <= SETTING * GIB )) reads it: blanks around it, empty
    as 0, a sign, a decimal, a leading 0 octal, 0x hexadecimal."""
    t = text.strip(" \t\n")
    if not t:
        return 0
    m = re.fullmatch(r"([+-]?)(0[xX][0-9a-fA-F]+|[0-9]+)", t)
    if m is None:
        return GuardError.NOT_A_NUMBER if re.match(r"[A-Za-z_]", t) else GuardError.BAD_NUMBER
    digits = m.group(2)
    if digits[:2] in ("0x", "0X"):
        n = int(digits, 16)
    else:
        found = bash_int(digits)
        if found is None:
            return GuardError.BAD_NUMBER
        n = found
    return -n if m.group(1) == "-" else n


class Urgency(StrEnum):
    """btrfs-notify's -u."""

    NORMAL = "normal"
    CRITICAL = "critical"


class AlertKind(StrEnum):
    """The kinds of alert, each at most once a day (STATE/alerted-KIND)."""

    BLOCKED = "blocked"
    PAUSED = "paused"
    EMERGENCY = "emergency"
    PRUNE_FAILED = "prune-failed"
    PENDING = "pending"
    BOOT_WAIT = "boot-wait"
    BULK_JOURNAL = "bulk-journal"
    BULK_WAIT = "bulk-wait"
    NAS = "nas"
    NAS_CREATED = "nas-created"
    NAS_PASSWORD = "nas-password"
    NAS_CHECK = "nas-check"


class CoreCommand(StrEnum):
    """btrfs-snapshot-core's commands that this program runs."""

    SNAPSHOT = "snapshot"
    PRUNE = "prune"
    FINISH = "finish"
    LIST = "list"
    CACHES = "caches"
    LATEST = "latest"


class Stderr(StrEnum):
    """What a captured command's stderr does: shown on ours, or dropped (2>/dev/null)."""

    SHOW = "show"
    DROP = "drop"


# The settings snapshots.conf may set, with their defaults (strings, as bash holds them). The
# three space-guard numbers are also assignments at the top of btrfs/btrfs-snapshot, which
# btrfs-bulk reads from that file's text (a test checks the two agree).
DEFAULTS: Final[Mapping[str, str]] = {
    "HOME_DIR": "",
    "HOME_MOUNT": "/var/home",
    "HOME_SUBVOL": "home",
    "TOP": "/var/mnt/btrfs-top",
    "SNAP_DIR_NAME": "snapshots",
    "HOME_PRESERVE": "72h 7d 4w 3m",
    "WS_PRESERVE": "48h 7d",
    "EMERGENCY_PRESERVE": "24h 3d",
    "SNAPSHOT_EXCLUDE_CACHES": "yes",
    "SNAPSHOT_CACHES_SKIP": ".local/share/containers",
    "SKIP_AT_OR_BELOW_GIB": "250",
    "RESUME_ABOVE_GIB": "270",
    "EMERGENCY_AT_OR_BELOW_GIB": "150",
    "NAS_ENABLED": "0",
    "NAS_HOST": "",
    "NAS_PORT": "2204",
    "NAS_USER": "",
    "NAS_PATH": "",
    "NAS_RESTIC_HOST": "",
    "NAS_STALE_DAYS": "7",
    "NAS_TIMEOUT": "6h",
    "NAS_KEEP_DAILY": "14",
    "NAS_KEEP_WEEKLY": "8",
    "NAS_KEEP_MONTHLY": "12",
    "NAS_PRUNE_EVERY_DAYS": "7",
    "NAS_CHECK_EVERY_DAYS": "30",
    "NAS_CHECK_SUBSET": "5%",
}
GUARD_KEYS: Final = ("SKIP_AT_OR_BELOW_GIB", "RESUME_ABOVE_GIB", "EMERGENCY_AT_OR_BELOW_GIB")

# What the core names in its messages to review or confirm deletions: the commands of cst.
CORE_HINTS: Final = (
    "--list-hint",
    "cst btrfs snapshots list",
    "--confirm-hint",
    "cst btrfs snapshots prune --confirm",
)


def settings_problem_line(conf: str, number: int, text: str, reason: str) -> str:
    """The warning of a line of snapshots.conf that is not a setting (as the bash front writes it)."""
    return f"<4>{TAG}: {conf} line {number}: {text.strip()}: {reason}; ignored"


@dataclass(frozen=True, slots=True)
class Sources:
    """The snapshot sources: names (home, and Workspace once it is a subvolume), their paths below
    the top level, and HOME_DIR inside the home subvolume ("" when it is the subvolume's root)."""

    names: tuple[str, ...]
    paths: tuple[str, ...]
    home_rel: str


def sources(settings: Mapping[str, str], *, workspace_is_subvol: bool) -> Sources:
    """core_sources of the bash: ~/Workspace has snapshots of its own only once it is a subvolume."""
    rel = settings["HOME_DIR"].removeprefix(settings["HOME_MOUNT"]).removeprefix("/")
    home = f"{settings['TOP']}/{settings['HOME_SUBVOL']}"
    if not workspace_is_subvol:
        return Sources(("home",), (home,), rel)
    return Sources(("home", "Workspace"), (home, f"{home}{'/' + rel if rel else ''}/Workspace"), rel)


def source_args(src: Sources, home_policy: str, ws_policy: str) -> list[str]:
    """The core's --source arguments, NAME=PATH:POLICY."""
    out: list[str] = []
    for name, path in zip(src.names, src.paths, strict=True):
        out += ["--source", f"{name}={path}:{ws_policy if name == 'Workspace' else home_policy}"]
    return out


@dataclass(frozen=True, slots=True)
class CacheArgs:
    """The core's cache options: args (--exclude-caches when on, then the skips), skips (the
    --caches-skip pairs alone, for the listing) and the warnings on the way."""

    args: tuple[str, ...]
    skips: tuple[str, ...]
    warnings: tuple[str, ...]


def cache_args(src: Sources, exclude: str, skip_text: str) -> CacheArgs:
    """core_cache_args of the bash: SNAPSHOT_EXCLUDE_CACHES (yes, no, anything else: a warning and
    yes) and the SNAPSHOT_CACHES_SKIP paths, each given to the source that holds it."""
    args: list[str] = []
    warnings: list[str] = []
    if exclude == "yes":
        args.append("--exclude-caches")
    elif exclude != "no":
        warnings.append(f"SNAPSHOT_EXCLUDE_CACHES='{exclude}' is not yes or no; using yes")
        args.append("--exclude-caches")
    skips: list[str] = []
    # read -r -a: the first line only, words at blanks (space, tab).
    first = skip_text.split("\n", 1)[0]
    for word in [w for w in re.split(r"[ \t]+", first) if w]:
        rel = word.removeprefix("./").removesuffix("/")
        if not rel or rel.startswith("/") or "/../" in f"/{rel}/" or "/./" in f"/{rel}/":
            warnings.append(f"SNAPSHOT_CACHES_SKIP: '{rel}' is not a path inside your home; ignored")
            continue
        if "Workspace" in src.names and (rel == "Workspace" or rel.startswith("Workspace/")):
            rel = "Workspace/." if rel == "Workspace" else rel
            skips += ["--caches-skip", f"Workspace={rel.removeprefix('Workspace/')}"]
        else:
            skips += ["--caches-skip", f"home={src.home_rel + '/' if src.home_rel else ''}{rel}"]
    return CacheArgs((*args, *skips), tuple(skips), tuple(warnings))


def core_reason(lines: Sequence[str]) -> str:
    """CORE_REASON: the core's line that names a block if there is one, else its first warning or
    error (its <3>/<4> lines, without the prefix, summary lines left out)."""
    found: list[str] = []
    for line in lines:
        for level in ("<3>", "<4>"):
            prefix = f"{level}{TAG}: "
            if line.startswith(prefix):
                found.append(line[len(prefix) :])
                break
    found = [ln for ln in found if " summary: " not in ln]
    block = next(
        (ln for ln in found if re.search(r"blocked|not creating a snapshot|not deleting|circuit breaker", ln)), None
    )
    if block is not None:
        return block
    # awk's first == "" {first = $0}: the first line that is not empty.
    return next((ln for ln in found if ln), "")


def plan_entries(text: str) -> list[tuple[str, str]]:
    """(strategy, path) of each line of the convert plan, as `while read -r strategy path` reads it:
    only lines ended by a newline, blanks (space, tab) around the words, the path the rest of the
    line; empty paths and comments (# in the strategy) left out."""
    out: list[tuple[str, str]] = []
    for line in text.split("\n")[:-1]:
        words = re.split(r"[ \t]+", line.strip(" \t"), maxsplit=1)
        strategy, path = words[0], (words[1] if len(words) > 1 else "")
        if not path or strategy.startswith("#"):
            continue
        out.append((strategy, path))
    return out


def read_lines(text: str) -> list[str]:
    """The lines `while IFS= read -r line` gives: those ended by a newline."""
    return text.split("\n")[:-1]


def nonempty_count(text: str) -> int:
    """grep -c . : the lines with at least one character (a last line without a newline too)."""
    return sum(1 for ln in text.split("\n") if ln)


def head_spaced(text: str, n: int) -> str:
    """head -n N | tr '\\n' ' ': the first N lines, each newline a blank."""
    lines = text.split("\n")
    ends = len(lines) - 1  # the lines ended by a newline
    shown = lines[: min(n, ends)]
    out = "".join(f"{ln} " for ln in shown)
    if ends < n and lines[-1]:
        out += lines[-1]
    return out


def blocked_alert(reason: str, conf: str) -> tuple[str, str]:
    """(title, body) of the alert for the core's exit 4 (a safety check blocked something)."""
    title = "Snapshots: safety check blocked deletions"
    if "not creating a snapshot" in reason:
        title = "Snapshots: safety check blocked a new snapshot"
    hint = "review: cst btrfs snapshots list"
    # Only the circuit breaker can be overridden; a clock problem must be fixed.
    if "circuit breaker" in reason:
        hint += (
            f"; if those deletions are right (e.g. a shorter retention in {conf}): cst btrfs snapshots prune --confirm"
        )
    cut = reason.split("; check with ", 1)[0]
    return title, f"{cut[:300]} - {hint}; details: journalctl -u btrfs-snapshot."


# ---- the NAS backup --------------------------------------------------------------------------
NAS_SSH_ALIAS: Final = "btrfs-setup-nas"  # the managed Host block in /root/.ssh/config
# Seconds of NAS_TIMEOUT when it cannot be read (6 h).
DEFAULT_NAS_TIMEOUT_S: Final = 21600
# What a step needs at least of the time left.
MIN_LEFT_S: Final = 60
RESTIC_SHORT_S: Final = 300  # cat config, unlock, snapshots
RESTIC_INIT_S: Final = 600
SFTP_TIMEOUT_S: Final = 60
NAS_LOCK_WAIT_S: Final = 1800
CHECK_FAILURES_ALERTED: Final = 3
SUMMARY_CHARS: Final = 300
EXIT_STOPPED: Final = 143  # bash's trap 'exit 143' TERM INT HUP


class ResticExit(IntEnum):
    """The exit statuses of restic (and of timeout around it) that the NAS backup tells apart."""

    OK = 0
    INCOMPLETE = 3
    NO_REPOSITORY = 10
    LOCKED = 11
    WRONG_PASSWORD = 12
    TIMED_OUT = 124
    INTERRUPTED = 130
    KILLED = 137


# Messages of restic check (0.19) that name damaged data, not a transport problem.
RESTIC_DAMAGE_RE: Final = re.compile(
    r"error for tree|not found in index|ciphertext verification failed|does not match|index is damaged|"
    r"without subtree|decompression failed|invalid compression|contains [0-9]+ errors|no such file|does not exist",
    re.IGNORECASE,
)
_UNITS: Final[Mapping[str, int]] = {"": 1, "s": 1, "m": 60, "h": 3600, "d": DAY_S}


def duration_s(text: str) -> int | None:
    """Seconds of a timeout(1) duration (N, Ns, Nm, Nh, Nd); 6 h if it cannot be read. None for a
    number bash arithmetic refused (08: a leading 0 is octal), which stopped bash's run."""
    m = re.fullmatch(r"([0-9]+)([smhd]?)", text)
    if m is None:
        return DEFAULT_NAS_TIMEOUT_S
    n = bash_int(m.group(1))
    return None if n is None else n * _UNITS[m.group(2)]


def restic_line(line: str) -> str:
    """restic's output in the journal format: errors and warnings at level 4, the rest at 6."""
    if re.match(r"Fatal|[Ee]rror|[Ww]arning", line):
        return f"<4>{TAG}: restic: {line}"
    return f"<6>{TAG}: restic: {line}"


def repo_id(out: str) -> str:
    """The repository id in restic cat config's JSON ("" when there is none)."""
    m = re.search(r'"id": *"([0-9a-f]{64})"', out)
    return "" if m is None else m.group(1)


def backup_time(name: str, entry: str) -> str:
    """restic --time of a snapshot NAME.YYYYMMDDTHHMM[_N]: its time from the name."""
    ts = entry.removeprefix(f"{name}.").split("_", 1)[0]
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:00"


def features_type(out: str) -> str:
    """The type of device-id-for-hardlinks in restic features' table (awk $1 == ... {print $2})."""
    for line in out.split("\n"):
        fields = re.split(r"[ \t]+", line.strip(" \t"))
        if fields[0] == "device-id-for-hardlinks":
            return [*fields, ""][1]
    return ""


def summary_text(out: str) -> str:
    """One line per source about its latest restic snapshot (restic snapshots --json), for
    snapshots status; raises on output that is not restic's JSON."""
    lines: list[str] = []
    for snap in sorted(json.loads(out), key=lambda s: (s.get("paths") or [""])[0]):
        summary = snap.get("summary") or {}
        name = (snap.get("paths") or ["?"])[0].rsplit("/", 1)[-1]
        when = snap.get("time", "")[:16].replace("T", " ")
        size = summary.get("total_bytes_processed", 0) / 2**30
        files = summary.get("total_files_processed", 0)
        added = summary.get("data_added_packed", 0) / 2**20
        lines.append(f"{name}: {when}, {size:.1f} GiB in {files} files, {added:.0f} MiB new on the NAS\n")
    return "".join(lines)

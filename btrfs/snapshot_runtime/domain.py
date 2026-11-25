# SPDX-License-Identifier: MPL-2.0
"""btrfs-snapshot-runtime's rules, as btrfs/btrfs-snapshot (bash) had them: the settings and their
defaults, btrfs-snapshot-core's command lines, and the texts of the journal lines and alerts. No I/O.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import IntEnum, StrEnum
import re
from typing import Final

from btrfs.health_check.domain import awk_field, bash_div, bash_int, bash_wrap
from cst_core.domain.shapes import ShapeError, first_item, items_of, object_of, object_or_empty, parse_json
from cst_core.domain.text import group, read_fields
from cst_core.domain.values import DAY, HOUR, MINUTE

TAG: Final = "btrfs-snapshot"
USAGE: Final = (
    "usage: btrfs-snapshot hourly|nas|convert|convert-boot|list [--json]|caches [--json]|prune [--dry-run] "
    "[--allow-mass-delete]|purge|planned-left|busy [--home DIR] PATH...|convert-one PATH"
)
NOT_SET_UP: Final = "not set up; run 'cst btrfs snapshots setup'"
EXIT_USAGE: Final = 2
EXIT_STOPPED: Final = 143  # bash's trap 'exit 143' TERM INT HUP (nas)
# stat -c %i of a btrfs subvolume's root folder.
SUBVOL_ROOT_INODE: Final = "256"


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
    """A numeric setting bash arithmetic could not read under set -u: a name (abc: an unset
    variable, the run stops), or anything else (08, 2.5, 250GiB, 1/0: an error line, and the
    comparison is false)."""

    BAD_NUMBER = "bad-number"
    NOT_A_NUMBER = "not-a-number"


# The bases bash takes in a BASE#DIGITS number, and its digits 0-9 and a-z (A-Z read as a-z up to that base).
_MIN_BASE: Final = 2
_MAX_BASE: Final = 64
_ONE_CASE_DIGITS: Final = 36


def _based(base: int, digits: str) -> int:
    """BASE#DIGITS as bash reads it: base 2 to 64; digits 0-9, then a-z, A-Z, @ and _ (a-z and A-Z
    alike up to base 36); a digit not below the base is an error."""
    if not _MIN_BASE <= base <= _MAX_BASE:
        raise ValueError("invalid arithmetic base")
    n = 0
    for c in digits:
        if c.isdigit():
            d = int(c)
        elif c.islower():
            d = ord(c) - ord("a") + 10
        elif c.isupper():
            d = ord(c) - ord("A") + (10 if base <= _ONE_CASE_DIGITS else _ONE_CASE_DIGITS)
        else:
            d = 62 if c == "@" else 63
        if d >= base:
            raise ValueError("value too great for base")
        n = n * base + d
    return bash_wrap(n)


# The operators bash's arithmetic reads: after a name, one of these (or another name) is read later, as
# the expression goes on; any other token after a name is an error bash meets before the name's value.
_ARITH_OPERATORS: Final = frozenset("+-*/%<>=!&|^~?:,()[]") | {"**"}
_ARITH_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A token after bash's blanks (space, tab and newline only: a no-break space, \r, \f, \v or another
# blank is a token of its own, which no rule takes).
_ARITH_TOKEN: Final = re.compile(r"[ \t\n]*(\*\*|[0-9A-Za-z_#@]+|[^ \t\n])")


class _Arith:
    """The part of bash arithmetic a setting can hold: integer constants (decimal, a leading 0
    octal, 0x hexadecimal, BASE#DIGITS with no leading 0 in BASE), unary + and -, ** (right to left,
    below unary -: -2**2 is 4), * / %, + -, parentheses, blanks (space, tab and newline, as bash's:
    any other character, a no-break space or a carriage return among them, is an error). Every constant and every
    result wraps around as bash's 64-bit integers do. A name is an unset variable (NOT_A_NUMBER): the
    file is read as data, a name is no value; but bash reads the token after a name before the name's
    value, so a name followed by a token that is neither an operator nor a name (x;, x 08, x
    <no-break space>) is bash's error of that token. Anything else, a division by 0 or a negative
    exponent is an error (BAD_NUMBER)."""

    def __init__(self, text: str) -> None:
        self.tokens = [group(m, 1) for m in _ARITH_TOKEN.finditer(text)]
        self.pos = 0

    def peek(self) -> str:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ""

    def take(self) -> str:
        tok = self.peek()
        self.pos += 1
        return tok

    def expr(self) -> int:
        n = self.term()
        while self.peek() in ("+", "-"):
            op = self.take()
            m = self.term()
            n = bash_wrap(n + m if op == "+" else n - m)
        return n

    def term(self) -> int:
        n = self.power()
        while self.peek() in ("*", "/", "%"):
            op = self.take()
            m = self.power()
            if op == "*":
                n = bash_wrap(n * m)
            elif m == 0:
                raise ValueError("division by 0")
            else:
                q = bash_div(n, m)
                n = bash_wrap(q if op == "/" else n - q * m)
        return n

    def power(self) -> int:
        n = self.unary()
        if self.peek() != "**":
            return n
        self.take()
        e = self.power()
        if e < 0:
            raise ValueError("exponent less than 0")
        return bash_wrap(pow(n, e, 1 << 64))

    def unary(self) -> int:
        if self.peek() in ("+", "-"):
            return bash_wrap(-self.unary()) if self.take() == "-" else self.unary()
        return self.atom()

    def atom(self) -> int:
        tok = self.take()
        if tok == "(":
            n = self.expr()
            if self.take() != ")":
                raise ValueError("missing )")
            return n
        if _ARITH_NAME.fullmatch(tok):
            nxt = self.peek()
            if nxt and nxt not in _ARITH_OPERATORS and not _ARITH_NAME.fullmatch(nxt):
                self._constant(nxt)  # bash reads the token after a name first: a bad one is its error
            raise LookupError(tok)
        return self._constant(tok)

    @staticmethod
    def _constant(tok: str) -> int:
        """An integer constant as bash reads it (0x hexadecimal, BASE#DIGITS, decimal, a leading 0
        octal), wrapped to 64 bits; ValueError for a token bash cannot read as one."""
        if re.fullmatch(r"0[xX][0-9a-fA-F]*", tok):
            return bash_wrap(int(tok[2:] or "0", 16))
        if m := re.fullmatch(r"([0-9]+)#([0-9A-Za-z@_]+)", tok):
            if m.group(1).startswith("0"):
                raise ValueError("invalid number")  # bash reads no base with a leading 0
            return _based(int(m.group(1)), m.group(2))
        n = bash_int(tok)
        if n is None:
            raise ValueError(tok)
        return n


def guard_number(text: str) -> int | GuardError:
    """A numeric setting as bash arithmetic (( ... SETTING ... )) read it under set -u (_Arith):
    empty is 0."""
    if not text.strip(" \t\n"):
        return 0
    parser = _Arith(text)
    try:
        n = parser.expr()
        if parser.peek():
            raise ValueError(parser.peek())
    except LookupError:
        return GuardError.NOT_A_NUMBER
    except ValueError:
        return GuardError.BAD_NUMBER
    return n


def guard_limit(text: str) -> int:
    """A space guard setting (GiB) as a limit the hourly run can hold at: guard_number's value; 0 (no
    limit) for one it cannot compare (BAD_NUMBER: its test is false; NOT_A_NUMBER: the run stops
    first) and for a negative one, which never holds."""
    n = guard_number(text)
    return n if isinstance(n, int) and n > 0 else 0


def snapshots_resume_above(guard: Mapping[str, int]) -> int:
    """Where btrfs-snapshot's paused /home snapshots come back, in the unit of GUARD's numbers
    (snapshot_guard()'s bytes, guard_limit's GiB): its hourly run stays paused while free <=
    SKIP_AT_OR_BELOW_GIB or (paused and free <= RESUME_ABOVE_GIB), so they resume only above the
    larger of the two (a limit read as 0 never holds them)."""
    return max(guard["resume"], guard["pause"])


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
        pair, warning = _skip_args(word, src)
        skips += pair
        if warning is not None:
            warnings.append(warning)
    return CacheArgs((*args, *skips), tuple(skips), tuple(warnings))


def _skip_args(word: str, src: Sources) -> tuple[list[str], str | None]:
    """One SNAPSHOT_CACHES_SKIP word: its --caches-skip pair, given to the source that holds it
    (Workspace when there is one and the path is in it, else home), or no pair and the warning."""
    rel = word.removeprefix("./").removesuffix("/")
    if not rel or rel.startswith("/") or "/../" in f"/{rel}/" or "/./" in f"/{rel}/":
        return [], f"SNAPSHOT_CACHES_SKIP: '{rel}' is not a path inside your home; ignored"
    if "Workspace" in src.names and (rel == "Workspace" or rel.startswith("Workspace/")):
        rel = "Workspace/." if rel == "Workspace" else rel
        return ["--caches-skip", f"Workspace={rel.removeprefix('Workspace/')}"], None
    return ["--caches-skip", f"home={src.home_rel + '/' if src.home_rel else ''}{rel}"], None


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
        strategy, path = read_fields(line, 2)
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


# Messages of restic check (0.19) that name damaged data, not a transport problem.
RESTIC_DAMAGE_RE: Final = re.compile(
    r"error for tree|not found in index|ciphertext verification failed|does not match|index is damaged|"
    r"without subtree|decompression failed|invalid compression|contains [0-9]+ errors|no such file|does not exist",
    re.IGNORECASE,
)
_UNITS: Final[Mapping[str, int]] = {"": 1, "s": 1, "m": MINUTE, "h": HOUR, "d": DAY}


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
    return "" if m is None else group(m, 1)


def restic_time(name: str, entry: str) -> str:
    """restic --time of a snapshot NAME.YYYYMMDDTHHMM[_N]: its time from the name."""
    ts = entry.removeprefix(f"{name}.").split("_", 1)[0]
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:00"


def features_type(out: str) -> str:
    """The type of device-id-for-hardlinks in restic features' table (awk $1 == ... {print $2})."""
    for line in out.split("\n"):
        if awk_field(line, 1) == "device-id-for-hardlinks":
            return awk_field(line, 2)
    return ""


def summary_text(out: str) -> str:
    """One line per source about its latest restic snapshot (restic snapshots --json), for
    snapshots status; raises on output that is not restic's JSON (ShapeError, a TypeError,
    where a part it reads is of another shape)."""
    lines: list[str] = []
    # A first path that is not text failed the name below, if not the sort: text only.
    snaps = [object_of(s) for s in _restic_snapshots(parse_json(out))]
    keyed = [(_restic_text(_first_or(s.get("paths"), "")), s) for s in snaps]
    for _, snap in sorted(keyed, key=lambda k: k[0]):
        summary = object_or_empty(snap.get("summary"))
        name = _first_or(snap.get("paths"), "?")
        when = snap.get("time", "")
        if not isinstance(name, str) or not isinstance(when, str):
            raise ShapeError("a path or a time that is not text")
        size = _restic_number(summary.get("total_bytes_processed", 0)) / 2**30
        files = summary.get("total_files_processed", 0)
        added = _restic_number(summary.get("data_added_packed", 0)) / 2**20
        name = name.rsplit("/", 1)[-1]
        when = when[:16].replace("T", " ")
        lines.append(f"{name}: {when}, {size:.1f} GiB in {files} files, {added:.0f} MiB new on the NAS\n")
    return "".join(lines)


def _restic_snapshots(data: object) -> list[object]:
    """The snapshots of restic snapshots --json (a list of objects; an empty string or object
    has none); ShapeError for any other value."""
    if isinstance(data, str | dict) and not data:
        return []
    if not isinstance(data, list):
        raise ShapeError(f"not a list: {type(data).__name__}")
    return items_of(data)


def _first_or(value: object, default: str) -> object:
    """(value or [default])[0]: a list's first item, a string's first character, default for a
    falsy value; ShapeError for any other value."""
    return first_item(value) if value else default


def _restic_text(value: object) -> str:
    """value when it is a string; ShapeError for any other value."""
    if isinstance(value, str):
        return value
    raise ShapeError(f"not text: {type(value).__name__}")


def _restic_number(value: object) -> int | float:
    """A JSON number (a bool counts, as arithmetic on it did); ShapeError for any other value."""
    if isinstance(value, int | float):
        return value
    raise ShapeError(f"not a number: {type(value).__name__}")

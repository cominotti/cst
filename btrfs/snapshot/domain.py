# SPDX-License-Identifier: MPL-2.0
"""The rules of btrfs-snapshot-core, with no I/O: retention policies and
btrbk's schedule() (plan), snapshot names and wall-clock time, the circuit
breaker's limits, and parsing of --source, --caches-skip, the state file and
`btrfs subvolume show`.
"""

from __future__ import annotations

from collections.abc import Collection
from collections.abc import Iterable
from collections.abc import Mapping
from typing import NewType, TypedDict
import dataclasses
import datetime as dt
import os
import re
from cst_core.domain.errors import Usage

EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_BUSY, EXIT_REFUSED = 0, 1, 2, 3, 4
DEFAULT_LOCK = "/run/btrfs-snapshot.lock"
DEFAULT_LIST_HINT = "btrfs-snapshot-core list --snapdir DIR --source NAME=PATH:POLICY ..."
DEFAULT_CONFIRM_HINT = "btrfs-snapshot-core prune --snapdir DIR --source NAME=PATH:POLICY ... --allow-mass-delete"
DEFAULT_PIN_FILE = "/run/btrfs-snapshot/pinned"
DEFAULT_STATE_DIR = "/var/lib/btrfs-snapshot"
STATE_FILE = "core-last-run"  # "EPOCH BOOT_ID BOOTTIME" of the last successful synchronized run
UNFINISHED_FILE = "core-unfinished"  # snapshots created writable and not yet read-only, one entry per line
WRITABLE_FILE = "core-writable"  # writable entries named like ours that the last run saw (for cst doctor)
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
# Cache folders: the CACHEDIR.TAG standard (https://bford.info/cachedir/),
# the same test as restic's --exclude-caches.
CACHE_TAG = "CACHEDIR.TAG"
CACHE_SIGNATURE = b"Signature: 8a477f597d28d172789f06886806bc55"
WALK_PRUNE = frozenset({".git", "node_modules"})  # never entered (size of the walk)
TS_FORMAT = "%Y%m%dT%H%M"  # btrbk timestamp_format long, local time
NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
KEEP_NEWEST = 3  # never delete the 3 newest snapshots of a source
MIN_AGE = 24 * 3600  # ... nor any younger than this
SAFETY_NET = 24 * 3600  # snapshot an unchanged source at least this often
CLOCK_SLACK = 5 * 60  # tolerated clock step backwards (NTP slews, RTC drift)
NTP_GRACE = 30 * 60  # after boot: wait this long for NTP before trusting a far-ahead clock
MASS_DELETE_MIN = 5  # circuit breaker: more than max(5, 10% of owned)
MASS_DELETE_PERCENT = 10
CMD_TIMEOUT = 600
# Policy letters in btrbk's order; '*' is stored as "all".
POLICY_UNITS = "hdwmy"
POLICY_TOKEN = re.compile(r"(\*|[0-9]+)([hdwmy])", re.IGNORECASE)
DAY_NAMES = ("sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday")


class ParseError(Exception):
    pass


# ---- policy --------------------------------------------------------------------------
def parse_policy(text: str) -> dict[str, int | str]:
    """'72h 7d 4w 3m' -> {'h': 72, 'd': 7, 'w': 4, 'm': 3}; '*' -> 'all'.

    Same tokens as btrbk's preserve matrix (case-insensitive unit, each unit at
    most once); commas are accepted as separators too. An empty policy ("keep
    only the latest") is refused: it is never what a caller of this tool means.
    """
    tokens = [t for t in re.split(r"[\s,]+", text.strip()) if t]
    if not tokens:
        raise ValueError("empty retention policy")
    policy: dict[str, int | str] = {}
    for tok in tokens:
        m = POLICY_TOKEN.fullmatch(tok)
        if not m:
            raise ValueError(f"bad retention token {tok!r} (expected <N>h, <N>d, <N>w, <N>m or <N>y)")
        unit = m.group(2).lower()
        if unit in policy:
            raise ValueError(f"retention unit {unit!r} given twice")
        policy[unit] = "all" if m.group(1) == "*" else int(m.group(1))
    return policy


def format_policy(policy: Mapping[str, int | str]) -> str:
    return " ".join(f"{'*' if policy[u] == 'all' else policy[u]}{u}" for u in POLICY_UNITS if u in policy)


def policy_max_kept(policy: Mapping[str, int | str]) -> int | None:
    """Upper bound of snapshots the policy keeps (excluding future-dated ones),
    or None if a unit is '*'."""
    total = 1  # the latest
    for u in POLICY_UNITS:
        n = policy.get(u)
        if n == "all":
            return None
        if isinstance(n, int) and n:
            total += n + 1  # buckets 0..N
    return total


def wall_to_epoch(wall: dt.datetime, tz: dt.tzinfo) -> int:
    """Local wall time -> epoch: an ambiguous time (DST end) maps to the EARLIER
    instant, a non-existent one (DST start) to the instant one DST shift later
    (zoneinfo fold=0). That is Perl's timelocal() (btrbk) for every zone with
    whole-hour DST shifts. Not for Australia/Lord_Howe (30-minute shift): there
    Time::Local 1.35 maps e.g. 01:38 on DST end to 14:08 UTC, an instant that
    localtime() shows as 01:08, not 01:38; fold=0 gives 14:38 UTC, which does
    show as 01:38, so we keep it and accept that plan() may pick a different
    first-of-hour/first-weekly snapshot than btrbk in that half hour."""
    return int(wall.replace(tzinfo=tz, fold=0).timestamp())


def epoch_to_wall(epoch: float, tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime.fromtimestamp(epoch, tz).replace(tzinfo=None)


def perl_int_div(a: int, b: int) -> int:
    """Perl's int(a / b) for b > 0: truncates toward zero (matters for future dates)."""
    q = abs(a) // b
    return q if a >= 0 else -q


def snapshot_name(name: str, wall: dt.datetime, seq: int = 0) -> str:
    return f"{name}.{wall.strftime(TS_FORMAT)}" + (f"_{seq}" if seq else "")


def name_regex(name: str) -> re.Pattern[str]:
    return re.compile(re.escape(name) + r"\.([0-9]{8}T[0-9]{4})(?:_([0-9]+))?")


def parse_snapshot_name(name: str, entry: str) -> tuple[dt.datetime, int] | None:
    """'home.20261013T1400_1' -> (wall datetime, 1), or None if not ours / invalid date."""
    m = name_regex(name).fullmatch(entry)
    if not m:
        return None
    try:
        wall = dt.datetime.strptime(m.group(1), TS_FORMAT)
    except ValueError:
        return None
    return wall, int(m.group(2) or 0)


# ---- retention (pure) ----------------------------------------------------------------
@dataclasses.dataclass(frozen=True, slots=True)
class Snap:
    entry: str  # directory entry, e.g. home.20261013T1400
    wall: dt.datetime  # local wall time from the name (naive)
    seq: int = 0  # collision suffix _N
    path: str = ""
    uuid: str = ""
    cgen: int = 0  # Gen at creation
    virtual: bool = False  # dry run: the snapshot this run would create


@dataclasses.dataclass(frozen=True, slots=True)
class Decision:
    snap: Snap
    action: str  # "keep" | "delete"
    reason: str


class _PlanItem(TypedDict):
    snap: Snap
    key: tuple[int, int]
    dh: int
    dd: int
    dw: int
    dm: int
    dy: int
    hfh: int
    dfe: int
    year: int
    month: int
    reason: str | None


def plan(
    snaps: Iterable[Snap],
    now: float | dt.datetime,
    policy: Mapping[str, int | str],
    tz: dt.tzinfo,
    *,
    hod: int = 0,
    dow: int = 0,
) -> list[Decision]:
    """Retention of btrbk 0.32.7 schedule() (snapshot_preserve_min latest,
    preserve_date_in_future as in its snapshot deletion). Identical decisions
    and reasons in every zone with whole-hour DST shifts; in Australia/Lord_Howe
    names in the repeated half hour of the DST end are read differently from
    btrbk (see wall_to_epoch()).

    snaps: iterable of Snap (only .wall, .seq used); now: epoch seconds or an
    aware datetime; policy: from parse_policy(); tz: zone of the names.
    Returns [Decision] sorted oldest first; the reason is btrbk's (the last
    rule that matched wins, as in btrbk's output).
    """
    now_ts = now.timestamp() if isinstance(now, dt.datetime) else now
    now_wall = epoch_to_wall(now_ts, tz)
    # btrbk: timegm() of the local hour fields, i.e. DST-blind wall-clock hours.
    now_h = now_wall.replace(minute=0, second=0, microsecond=0)
    dow_name = DAY_NAMES[dow]

    items: list[_PlanItem] = []
    for s in snaps:
        epoch = wall_to_epoch(s.wall, tz)
        tm = epoch_to_wall(epoch, tz)  # localtime(): normalizes non-existent times
        wday = (tm.weekday() + 1) % 7  # Perl: Sunday = 0
        hfh = tm.hour - hod  # hours from start of day
        dfe = wday - dow  # days from start of week
        if hfh < 0:
            hfh += 24
            dfe -= 1
        if dfe < 0:
            dfe += 7
        month_corr, year_corr = tm.month - 1, tm.year
        if tm.day <= dfe:
            # The month (and year) starts on its first preserve_day_of_week.
            month_corr -= 1
            if month_corr < 0:
                month_corr, year_corr = 11, year_corr - 1
        dh = int((now_h - tm.replace(minute=0, second=0)).total_seconds()) // 3600
        dd = perl_int_div(dh + hfh, 24)
        dw = perl_int_div(dd + dfe, 7)
        dy = now_wall.year - year_corr
        dm = dy * 12 + (now_wall.month - 1 - month_corr)
        items.append(
            {
                "snap": s,
                "key": (epoch, s.seq),
                "dh": dh,
                "dd": dd,
                "dw": dw,
                "dm": dm,
                "dy": dy,
                "hfh": hfh,
                "dfe": dfe,
                "year": year_corr,
                "month": month_corr + 1,
                "reason": None,
            }
        )
    items.sort(key=lambda i: i["key"])

    def delta_text(i: _PlanItem) -> str:
        s = (f" {i['dfe']}d" if i["dfe"] else "") + (f" {i['hfh']}h" if i["hfh"] else "")
        return (f"{s} after " if s else " at ") + f"{dow_name} {hod:02d}:00"

    def within(unit: str, delta: int) -> bool:
        n = policy.get(unit)
        return bool(n) and (n == "all" or (isinstance(n, int) and delta <= n))

    first_hours: dict[int, _PlanItem] = {}
    for i in items:
        if i["dh"] < 0:
            i["reason"] = f"preserve forced: {-i['dh']} hours in the future"
        first_hours.setdefault(i["dh"], i)
    if items:
        items[-1]["reason"] = "preserve min: latest"

    first_days: dict[int, _PlanItem] = {}
    for k in sorted(first_hours, reverse=True):
        i = first_hours[k]
        if within("h", i["dh"]):
            i["reason"] = f"preserve hourly: first of hour, {i['dh']} hours ago"
        first_days.setdefault(i["dd"], i)
    first_weeks: dict[int, _PlanItem] = {}
    for k in sorted(first_days, reverse=True):
        i = first_days[k]
        if within("d", i["dd"]):
            i["reason"] = (
                f"preserve daily: first of day, {i['dd']} days ago"
                + (f", {i['hfh']}h after " if i["hfh"] else ", at ")
                + f"{hod:02d}:00"
            )
        first_weeks.setdefault(i["dw"], i)
    first_months: dict[int, _PlanItem] = {}
    for k in sorted(first_weeks, reverse=True):
        i = first_weeks[k]
        if within("w", i["dw"]):
            i["reason"] = f"preserve weekly: {i['dw']} weeks ago," + delta_text(i)
        first_months.setdefault(i["dm"], i)
    first_years: dict[int, _PlanItem] = {}
    for k in sorted(first_months, reverse=True):
        i = first_months[k]
        if within("m", i["dm"]):
            i["reason"] = (
                f"preserve monthly: first weekly of month {i['year']}-{i['month']:02d} "
                f"({i['dm']} months ago," + delta_text(i) + ")"
            )
        first_years.setdefault(i["dy"], i)
    for k in sorted(first_years, reverse=True):
        i = first_years[k]
        if within("y", i["dy"]):
            i["reason"] = (
                f"preserve yearly: first weekly of year {i['year']} ({i['dy']} years ago," + delta_text(i) + ")"
            )

    out: list[Decision] = []
    for i in items:
        reason = i["reason"]
        if reason:
            out.append(Decision(i["snap"], "keep", reason))
        else:
            out.append(Decision(i["snap"], "delete", f"not kept by policy '{format_policy(policy)}'"))
    return out


def newest_of(snaps: Iterable[Snap]) -> Snap | None:
    """Newest by Gen at creation (names can lie after a clock step), then by date."""
    return max(snaps, key=lambda s: (s.cgen, s.wall, s.seq), default=None)


def mass_delete_limit(owned: int) -> int:
    return max(MASS_DELETE_MIN, owned * MASS_DELETE_PERCENT // 100)


def is_mass_delete(count: int, owned: int) -> bool:
    """More than max(5, 10% of owned)."""
    return count > MASS_DELETE_MIN and count * 100 > owned * MASS_DELETE_PERCENT


def err_text(e: BaseException) -> str:
    """An OSError's strerror, else the exception's text (ParseError, an OSError without one)."""
    return getattr(e, "strerror", None) or str(e)


# ---- btrfs ---------------------------------------------------------------------------
# The path of a btrfs subvolume (a snapshot or a source), as the btrfs
# commands of adapters.Btrfs take it; a plain folder path is not one.
SubvolPath = NewType("SubvolPath", str)


class SubvolInfo(TypedDict):
    uuid: str
    parent_uuid: str | None
    generation: int
    cgen: int


SHOW_LABELS = {"UUID": "uuid", "Parent UUID": "parent_uuid", "Generation": "generation", "Gen at creation": "cgen"}


def parse_subvolume_show(text: str) -> SubvolInfo:
    """The four fields we use from `btrfs subvolume show`; ParseError on anything odd.

    Label lines are indented; the list after "Snapshot(s):" holds paths that are
    not ours to interpret, so parsing stops there.
    """
    found: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip().startswith("Snapshot(s):"):
            break
        m = re.match(r"^\s+([^:\t]+?):\s*(.*?)\s*$", line)
        if not m or m.group(1) not in SHOW_LABELS:
            continue
        key = SHOW_LABELS[m.group(1)]
        if key in found:
            raise ParseError(f"label {m.group(1)!r} appears twice")
        found[key] = m.group(2)
    missing = [label for label, key in SHOW_LABELS.items() if key not in found]
    if missing:
        raise ParseError("missing " + ", ".join(missing))
    if not UUID_RE.fullmatch(found["uuid"]):
        raise ParseError(f"bad UUID {found['uuid']!r}")
    if found["parent_uuid"] != "-" and not UUID_RE.fullmatch(found["parent_uuid"]):
        raise ParseError(f"bad Parent UUID {found['parent_uuid']!r}")
    for key in ("generation", "cgen"):
        if not found[key].isdigit():
            raise ParseError(f"bad {key} {found[key]!r}")
    return {
        "uuid": found["uuid"],
        "parent_uuid": None if found["parent_uuid"] == "-" else found["parent_uuid"],
        "generation": int(found["generation"]),
        "cgen": int(found["cgen"]),
    }


def other_parent(info: SubvolInfo, source_uuid: str) -> str | None:
    """Why a subvolume with show() INFO is no snapshot of SOURCE_UUID (None = it is)."""
    if info["parent_uuid"] == source_uuid:
        return None
    return f"snapshot of another subvolume (Parent UUID {info['parent_uuid'] or '-'})"


# ---- run context -----------------------------------------------------------------------
@dataclasses.dataclass(frozen=True, slots=True)
class Source:
    name: str
    path: str
    policy: dict[str, int | str]
    policy_text: str
    skips: list[str] = dataclasses.field(default_factory=list)  # --caches-skip, relative to path


def require_absolute(option: str, path: str | None) -> None:
    """Usage unless path is absolute (None: the option was not given)."""
    if path is not None and not path.startswith("/"):
        raise Usage(f"{option} must be absolute")


def parse_source(text: str, need_policy: bool = True) -> Source:
    if "=" not in text:
        raise Usage(f"--source {text!r}: expected NAME=PATH:POLICY")
    name, rest = text.split("=", 1)
    if not NAME_RE.fullmatch(name):
        raise Usage(f"--source {text!r}: NAME must match [A-Za-z0-9._-]+")
    if ":" in rest:
        path, policy_text = rest.rsplit(":", 1)
    elif need_policy:
        raise Usage(f"--source {text!r}: missing :POLICY")
    else:
        path, policy_text = rest, ""
    if not path.startswith("/"):
        raise Usage(f"--source {text!r}: PATH must be absolute")
    policy: dict[str, int | str] = {}
    if need_policy or policy_text.strip():
        try:
            policy = parse_policy(policy_text)
        except ValueError as e:
            raise Usage(f"--source {text!r}: {e}") from None
    return Source(name, os.path.normpath(path), policy, policy_text.strip())


def parse_skip(text: str, names: Collection[str]) -> tuple[str, str]:
    """'NAME=REL' -> (NAME, normalized REL); REL relative, inside the source."""
    name, sep, rel = text.partition("=")
    if not sep or name not in names:
        raise Usage(f"--caches-skip {text!r}: expected NAME=REL with NAME one of the --source names")
    norm = os.path.normpath(rel) if rel else ""
    if not norm or norm.startswith("/") or norm == ".." or norm.startswith("../"):
        raise Usage(f"--caches-skip {text!r}: REL must be a path inside the source, relative to its root")
    return name, norm


@dataclasses.dataclass(frozen=True, slots=True)
class LastRun:
    epoch: int
    boot_id: str | None = None  # None: written without (or by an older format)
    boottime: float | None = None


def parse_state(text: str) -> LastRun | None:
    """'EPOCH [BOOT_ID BOOTTIME]' -> LastRun, or None if malformed."""
    parts = text.split()
    if not parts or not parts[0].isdigit():
        return None
    if len(parts) == 1:
        return LastRun(int(parts[0]))
    if len(parts) != 3 or not UUID_RE.fullmatch(parts[1]):
        return None
    try:
        boottime = float(parts[2])
    except ValueError:
        return None
    return LastRun(int(parts[0]), parts[1], boottime)


def fmt_span(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 2 * 86400:
        return f"{seconds // 86400} days"
    if seconds >= 2 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 60} min"


def fmt_epoch(epoch: float, tz: dt.tzinfo) -> str:
    return dt.datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d %H:%M %Z")

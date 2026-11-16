"""Unit tests for btrfs/btrfs-snapshot-core (no root, no btrfs needed).

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest
    python3 -I -B -m unittest discover -s btrfs/tests -v
or directly:
    python3 -I -B btrfs/tests/test_btrfs_snapshot_core.py

Optional cross-check against btrbk itself: point BTRBK_SOURCE at the btrbk
0.32.7 script (e.g. the one in the Fedora btrbk rpm) and have perl with
Time::Local; schedule() is extracted from it and fed the same random cases.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl  # noqa: F401 - preimported environment check
import importlib.machinery
import io
import json
import os
import random
import shutil
import stat
import subprocess
import sys
import tempfile
import time  # noqa: F401
import types
import unittest
import zoneinfo
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from typing import Any, Protocol, TypedDict, cast
from unittest import mock

sys.dont_write_bytecode = True  # keep __pycache__ out of the repository

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_PATH = os.path.join(os.path.dirname(HERE), "btrfs-snapshot-core")


class _SnapProto(Protocol):
    entry: str
    wall: dt.datetime
    seq: int
    path: str
    uuid: str
    cgen: int
    virtual: bool


class _SnapCtor(Protocol):
    def __call__(
        self,
        entry: str,
        wall: dt.datetime,
        seq: int = 0,
        path: str = "",
        uuid: str = "",
        cgen: int = 0,
        virtual: bool = False,
    ) -> _SnapProto: ...


class _DecisionProto(Protocol):
    snap: _SnapProto
    action: str
    reason: str


class _CacheDirProto(Protocol):
    rel: str
    files: int
    size: int


class _CacheDirCtor(Protocol):
    def __call__(self, rel: str, files: int = 0, size: int = 0) -> _CacheDirProto: ...


class _CacheWalkProto(Protocol):
    folders: list[_CacheDirProto]
    kept: list[str]
    skipped: list[str]
    problems: list[tuple[str, str]]
    seconds: float
    files: int
    size: int


class _CacheWalkCtor(Protocol):
    def __call__(
        self,
        folders: list[_CacheDirProto],
        kept: list[str],
        skipped: list[str],
        problems: list[tuple[str, str]],
        seconds: float = 0.0,
    ) -> _CacheWalkProto: ...


class _SourceProto(Protocol):
    name: str
    path: str
    policy: dict[str, int | str]
    policy_text: str
    skips: list[str]


class _SubvolInfoProto(TypedDict):
    uuid: str
    parent_uuid: str | None
    generation: int
    cgen: int


class _HostProto(Protocol):
    def run(self, argv: Sequence[str]) -> tuple[int, str, str]: ...
    def time(self) -> float: ...
    def boot(self) -> tuple[str | None, float | None]: ...
    def listdir(self, path: str) -> list[str]: ...
    def kind(self, path: str) -> str: ...
    def mkdir(self, path: str, mode: int) -> None: ...
    def walk_caches(self, root: str, skips: Sequence[str], mode: str) -> _CacheWalkProto: ...


class _SnapshotCoreModule(Protocol):
    CACHE_SIGNATURE: bytes
    CACHE_TAG: str
    NTP_GRACE: int
    STATE_FILE: str
    TS_FORMAT: str
    UNFINISHED_FILE: str
    WRITABLE_FILE: str
    os: types.ModuleType
    ParseError: type[Exception]
    UsageError: type[Exception]
    Snap: _SnapCtor
    CacheDir: _CacheDirCtor
    CacheWalk: _CacheWalkCtor

    def parse_policy(self, text: str) -> dict[str, int | str]: ...
    def format_policy(self, policy: Mapping[str, int | str]) -> str: ...
    def policy_max_kept(self, policy: Mapping[str, int | str]) -> int | None: ...
    def wall_to_epoch(self, wall: dt.datetime, tz: dt.tzinfo) -> int: ...
    def epoch_to_wall(self, epoch: float, tz: dt.tzinfo) -> dt.datetime: ...
    def snapshot_name(self, name: str, wall: dt.datetime, seq: int = 0) -> str: ...
    def parse_snapshot_name(self, name: str, entry: str) -> tuple[dt.datetime, int] | None: ...
    def parse_source(self, text: str, need_policy: bool = True) -> _SourceProto: ...
    def parse_subvolume_show(self, text: str) -> _SubvolInfoProto: ...
    def plan(
        self,
        snaps: Iterable[_SnapProto],
        now: float | dt.datetime,
        policy: Mapping[str, int | str],
        tz: dt.tzinfo,
        *,
        hod: int = 0,
        dow: int = 0,
    ) -> list[_DecisionProto]: ...
    def apply_safeguards(
        self,
        decisions: list[_DecisionProto],
        now: float,
        tz: dt.tzinfo,
        pins: Collection[str],
    ) -> list[_DecisionProto]: ...
    def is_mass_delete(self, count: int, owned: int) -> bool: ...
    def unexplained_deletions(
        self,
        decisions: Sequence[_DecisionProto],
        snaps: Sequence[_SnapProto],
        policy: Mapping[str, int | str],
        tz: dt.tzinfo,
        pins: Collection[str],
        since: int | None,
        ref: float | None = None,
    ) -> list[_DecisionProto]: ...
    def fmt_epoch(self, epoch: float, tz: dt.tzinfo) -> str: ...
    def walk_caches(self, root: str, skips: Sequence[str] = (), mode: str = "count") -> _CacheWalkProto: ...
    def main(self, argv: Sequence[str] | None = None, host: _HostProto | None = None) -> int: ...


def load_core() -> _SnapshotCoreModule:
    loader = importlib.machinery.SourceFileLoader("btrfs_snapshot_core", CORE_PATH)
    mod = types.ModuleType("btrfs_snapshot_core")
    loader.exec_module(mod)
    return cast("_SnapshotCoreModule", mod)


core = load_core()

BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")
NEW_YORK = zoneinfo.ZoneInfo("America/New_York")
UTC = zoneinfo.ZoneInfo("UTC")


def W(text: str) -> dt.datetime:
    """'2026-10-13 14:05' -> naive wall datetime."""
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M")


def snap(text: str, seq: int = 0, name: str = "home") -> _SnapProto:
    wall = W(text)
    return core.Snap(entry=core.snapshot_name(name, wall, seq), wall=wall, seq=seq)


def at(text: str, tz: dt.tzinfo) -> int:
    """Epoch of a local wall time (earlier instant if ambiguous)."""
    return core.wall_to_epoch(W(text), tz)


def decide(snaps: Sequence[_SnapProto], now_text: str, policy: str, tz: dt.tzinfo) -> dict[str, str]:
    rows = core.plan(snaps, at(now_text, tz), core.parse_policy(policy), tz)
    return {r.snap.entry: r.action for r in rows}


def kept(snaps: Sequence[_SnapProto], now_text: str, policy: str, tz: dt.tzinfo) -> list[str]:
    return sorted(e for e, a in decide(snaps, now_text, policy, tz).items() if a == "keep")


def hourly_series(start: str, end: str, minute: int = 5, step_minutes: int = 60) -> list[_SnapProto]:
    out: list[_SnapProto] = []
    t = W(start).replace(minute=minute)
    stop = W(end).replace(minute=minute)
    while t <= stop:
        out.append(core.Snap(entry=core.snapshot_name("home", t), wall=t))
        t += dt.timedelta(minutes=step_minutes)
    return out


def E(text: str, seq: int = 0) -> str:
    return core.snapshot_name("home", W(text), seq)


# ---- reference model ----------------------------------------------------------------
def reference_keep(
    snaps: Sequence[_SnapProto],
    now: float,
    policy: Mapping[str, int | str],
    tz: dt.tzinfo,
) -> set[str]:
    """Independent formulation of btrbk's rules (past snapshots only): at each
    level, the oldest snapshot of every bucket whose distance is within N; days
    start 00:00, weeks Sunday 00:00, months/years on their first Sunday; plus
    the newest. Returns the set of kept entries."""

    def norm(s: _SnapProto) -> tuple[int, dt.datetime]:
        e = core.wall_to_epoch(s.wall, tz)
        return e, core.epoch_to_wall(e, tz)

    def corr_month(w: dt.datetime) -> tuple[int, int]:
        sunday_start = w.date() - dt.timedelta(days=(w.weekday() + 1) % 7)
        if sunday_start.month != w.month or sunday_start.year != w.year:
            # this week began in the previous month, so the month has not started yet
            first = w.replace(day=1) - dt.timedelta(days=1)
            return first.year, first.month
        return w.year, w.month

    def week_start(d: dt.date) -> dt.date:
        return d - dt.timedelta(days=(d.weekday() + 1) % 7)

    now_w = core.epoch_to_wall(now, tz)
    now_h = now_w.replace(minute=0, second=0, microsecond=0)
    items: list[dict[str, Any]] = []
    for s in snaps:
        e, w = norm(s)
        y, m = corr_month(w)
        items.append(
            {
                "entry": s.entry,
                "key": (e, s.seq),
                "h": int((now_h - w.replace(minute=0)).total_seconds() // 3600),
                "d": (now_w.date() - w.date()).days,
                "w": (week_start(now_w.date()) - week_start(w.date())).days // 7,
                "m": (now_w.year * 12 + now_w.month) - (y * 12 + m),
                "y": now_w.year - y,
            }
        )
    items.sort(key=lambda i: i["key"])
    keep: set[str] = set()
    if items:
        keep.add(items[-1]["entry"])
    for unit in "hdwmy":
        n = policy.get(unit)
        if not n:
            continue
        seen: set[int] = set()
        for i in items:
            if i[unit] in seen:
                continue
            seen.add(i[unit])
            if n == "all" or i[unit] <= n:
                keep.add(i["entry"])
    return keep


def random_timeline(rng: random.Random, tz: dt.tzinfo, count: int) -> list[tuple[int, _SnapProto]]:
    """Creation epochs and names as the tool would produce them (local names, _N)."""
    t = core.wall_to_epoch(
        dt.datetime(
            2025 + rng.randrange(4), 1 + rng.randrange(12), 1 + rng.randrange(28), rng.randrange(24), rng.randrange(60)
        ),
        tz,
    )
    snaps: list[tuple[int, _SnapProto]] = []
    taken: dict[str, int] = {}
    for _ in range(count):
        r = rng.random()
        if r < 0.70:
            t += rng.randrange(40, 80) * 60  # hourly-ish
        elif r < 0.85:
            t += rng.randrange(1, 30) * 60  # several per hour
        elif r < 0.97:
            t += rng.randrange(2, 30) * 3600  # off for a while
        else:
            t += rng.randrange(2, 40) * 86400  # suspended / away for days or weeks
        wall = core.epoch_to_wall(t, tz).replace(second=0)
        base = wall.strftime(core.TS_FORMAT)
        seq = taken.get(base, -1) + 1
        taken[base] = seq
        snaps.append((t, core.Snap(entry=core.snapshot_name("home", wall, seq), wall=wall, seq=seq)))
    return snaps


RANDOM_TZS = ["Europe/Berlin", "America/New_York", "UTC", "Asia/Kolkata", "Australia/Lord_Howe", "America/Sao_Paulo"]
RANDOM_POLICIES = [
    "72h 7d 4w 3m",
    "48h 7d",
    "24h 3d",
    "1h",
    "6h 2d 1w 1m 1y",
    "3d",
    "2w 2m",
    "*h 1d",
    "10h,5d,3w,2m,1y",
    "1m",
    "1y",
    "*m",
]


# ---- policy and names -----------------------------------------------------------------
class PolicyTests(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(core.parse_policy("72h 7d 4w 3m"), {"h": 72, "d": 7, "w": 4, "m": 3})
        self.assertEqual(core.parse_policy("48h,7d"), {"h": 48, "d": 7})
        self.assertEqual(core.parse_policy(" 2H, 3D  *y "), {"h": 2, "d": 3, "y": "all"})
        self.assertEqual(core.format_policy(core.parse_policy("3m 72h *y")), "72h 3m *y")

    def test_parse_rejects(self) -> None:
        for bad in ("", "  ", "no", "72", "h", "7x", "1h 2h", "1h;2d", "-1h", "1.5d"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.parse_policy(bad)

    def test_max_kept(self) -> None:
        self.assertEqual(core.policy_max_kept(core.parse_policy("72h 7d 4w 3m")), 73 + 8 + 5 + 4 + 1)
        self.assertIsNone(core.policy_max_kept(core.parse_policy("*h 1d")))

    def test_names(self) -> None:
        self.assertEqual(core.snapshot_name("home", W("2026-03-05 07:09")), "home.20260305T0709")
        self.assertEqual(core.snapshot_name("home", W("2026-03-05 07:09"), 2), "home.20260305T0709_2")
        self.assertEqual(core.parse_snapshot_name("home", "home.20260305T0709_2"), (W("2026-03-05 07:09"), 2))
        for bad in (
            "home.20260305T0709x",
            "home.2026030T0709",
            "home.20261305T0709",
            "home.20260230T0000",
            "homeX20260305T0709",
            "Workspace.20260305T0709",
            "home.20260305T0709_",
            "home.20260305T0709/x",
        ):
            with self.subTest(bad=bad):
                self.assertIsNone(core.parse_snapshot_name("home", bad))
        # A dot in NAME is literal.
        self.assertIsNone(core.parse_snapshot_name("a.b", "aXb.20260305T0709"))

    def test_source_parse(self) -> None:
        s = core.parse_source("Workspace=/top/home/u/Workspace/:48h 7d")
        self.assertEqual((s.name, s.path, s.policy), ("Workspace", "/top/home/u/Workspace", {"h": 48, "d": 7}))
        s = core.parse_source("x=/a:b/c:1d")  # the policy is after the LAST colon
        self.assertEqual(s.path, "/a:b/c")
        for bad in ("home", "ho me=/x:1d", "home=rel:1d", "home=/x", "home=/x:", "home=/x:1q", "=/x:1d"):
            with self.subTest(bad=bad), self.assertRaises(core.UsageError):
                core.parse_source(bad)


class ShowParseTests(unittest.TestCase):
    REAL = (
        "home\n"
        "\tName: \t\t\thome\n"
        "\tUUID: \t\t\tf0402c44-a6dc-ad45-b805-24977d4eaee4\n"
        "\tParent UUID: \t\t-\n"
        "\tReceived UUID: \t\t1111aaaa-a6dc-ad45-b805-24977d4eaee4\n"
        "\tCreation time: \t\t2026-05-08 23:21:53 -0300\n"
        "\tSubvolume ID: \t\t256\n"
        "\tGeneration: \t\t924444\n"
        "\tGen at creation: \t9\n"
        "\tParent ID: \t\t5\n"
        "\tTop level ID: \t\t5\n"
        "\tFlags: \t\t\t-\n"
        "\tSend transid: \t\t0\n"
        "\tSend time: \t\t2026-05-08 23:21:53 -0300\n"
        "\tReceive transid: \t0\n"
        "\tReceive time: \t\t-\n"
        "\tSnapshot(s):\n"
        "\t\t\t\tsnapshots/UUID: 00000000-0000-0000-0000-000000000000\n"
        "\t\t\t\tsnapshots/home.20261013T1400\n"
        "\tQuota group:\t\tn/a\n"
    )

    def test_real_output(self) -> None:
        self.assertEqual(
            core.parse_subvolume_show(self.REAL),
            {"uuid": "f0402c44-a6dc-ad45-b805-24977d4eaee4", "parent_uuid": None, "generation": 924444, "cgen": 9},
        )

    def test_parent_uuid(self) -> None:
        text = self.REAL.replace("Parent UUID: \t\t-", "Parent UUID: \t\t0badc0de-0000-0000-0000-000000000001")
        self.assertEqual(core.parse_subvolume_show(text)["parent_uuid"], "0badc0de-0000-0000-0000-000000000001")

    def test_failures(self) -> None:
        cases = {
            "missing generation": self.REAL.replace("\tGeneration: \t\t924444\n", ""),
            "missing cgen": self.REAL.replace("Gen at creation", "Gen at birth"),
            "duplicate": self.REAL.replace("\tName:", "\tUUID: \t\t\tf0402c44-a6dc-ad45-b805-24977d4eaee4\n\tName:"),
            "bad uuid": self.REAL.replace("f0402c44-a6dc", "zzzz2c44-a6dc"),
            "bad generation": self.REAL.replace("924444", "92x444"),
            "bad parent": self.REAL.replace("Parent UUID: \t\t-", "Parent UUID: \t\tnope"),
            "empty": "",
            "unindented labels": self.REAL.replace("\t", ""),
        }
        for what, text in cases.items():
            with self.subTest(what=what), self.assertRaises(core.ParseError):
                core.parse_subvolume_show(text)


# ---- retention boundaries -------------------------------------------------------------
class RetentionBoundaryTests(unittest.TestCase):
    def test_73rd_and_74th_hour(self) -> None:
        snaps = hourly_series("2026-06-11 23:00", "2026-06-15 10:00")
        d = decide(snaps, "2026-06-15 10:30", "72h", BERLIN)
        self.assertEqual(d[E("2026-06-15 10:05")], "keep")  # hour 0
        self.assertEqual(d[E("2026-06-12 10:05")], "keep")  # hour 72: inclusive
        self.assertEqual(d[E("2026-06-12 09:05")], "delete")  # hour 73
        self.assertEqual(d[E("2026-06-12 08:05")], "delete")  # hour 74
        self.assertEqual(sum(1 for a in d.values() if a == "keep"), 73)
        # With dailies, the 73rd hour still goes; the first of that day stays.
        d = decide(snaps, "2026-06-15 10:30", "72h 7d", BERLIN)
        self.assertEqual(d[E("2026-06-12 09:05")], "delete")
        self.assertEqual(d[E("2026-06-12 00:05")], "keep")
        self.assertEqual(d[E("2026-06-11 23:05")], "keep")  # first of 2026-06-11
        # The hour boundary is the wall-clock hour, not 60 minutes before now.
        d = decide(snaps, "2026-06-15 11:00", "72h", BERLIN)
        self.assertEqual(d[E("2026-06-12 10:05")], "delete")  # now hour 73
        self.assertEqual(d[E("2026-06-12 11:05")], "keep")

    def test_several_per_hour_keeps_the_first(self) -> None:
        snaps = [
            snap("2026-06-15 05:05"),
            snap("2026-06-15 05:20"),
            snap("2026-06-15 05:40"),
            snap("2026-06-15 05:40", 1),
            snap("2026-06-15 06:01"),
            snap("2026-06-15 06:59"),
        ]
        self.assertEqual(
            kept(snaps, "2026-06-15 07:00", "24h", BERLIN),
            sorted([E("2026-06-15 05:05"), E("2026-06-15 06:01"), E("2026-06-15 06:59")]),
        )
        d = decide(snaps, "2026-06-15 07:00", "24h", BERLIN)
        self.assertEqual(d[E("2026-06-15 05:40", 1)], "delete")
        # 06:59 is kept only as the latest.
        rows = core.plan(snaps, at("2026-06-15 07:00", BERLIN), core.parse_policy("24h"), BERLIN)
        self.assertEqual(rows[-1].reason, "preserve min: latest")

    def test_suffix_orders_within_minute(self) -> None:
        snaps = [snap("2026-06-15 05:40", 2), snap("2026-06-15 05:40"), snap("2026-06-15 05:40", 1)]
        rows = core.plan(snaps, at("2026-06-15 07:00", BERLIN), core.parse_policy("24h"), BERLIN)
        self.assertEqual([r.snap.seq for r in rows], [0, 1, 2])
        self.assertEqual([r.action for r in rows], ["keep", "delete", "keep"])

    def test_first_sunday_of_month(self) -> None:
        snaps = []
        day = W("2026-08-25 12:00")
        while day <= W("2026-11-20 12:00"):
            snaps.append(core.Snap(entry=core.snapshot_name("home", day), wall=day))
            day += dt.timedelta(days=1)
        self.assertEqual(
            kept(snaps, "2026-11-20 13:00", "2m", BERLIN),
            sorted(
                [
                    E("2026-09-06 12:00"),  # first Sunday of September: month 2 starts here
                    E("2026-10-04 12:00"),  # first Sunday of October
                    E("2026-11-01 12:00"),  # 1 November 2026 is a Sunday
                    E("2026-11-20 12:00"),  # latest
                ]
            ),
        )
        # Sep 1-5 still belong to August's month (months start on their first Sunday).
        rows = {
            r.snap.entry: r.reason
            for r in core.plan(snaps, at("2026-11-20 13:00", BERLIN), core.parse_policy("3m"), BERLIN)
        }
        self.assertIn("first weekly of month 2026-08", rows[E("2026-08-25 12:00")])
        self.assertNotIn("preserve", rows[E("2026-09-05 12:00")])

    def test_week_starts_sunday_midnight(self) -> None:
        snaps = [snap("2026-10-17 23:30"), snap("2026-10-18 00:30"), snap("2026-10-21 10:00"), snap("2026-10-28 11:00")]
        self.assertEqual(
            kept(snaps, "2026-10-28 12:00", "1w", BERLIN), sorted([E("2026-10-18 00:30"), E("2026-10-28 11:00")])
        )
        self.assertEqual(
            kept(snaps, "2026-10-28 12:00", "2w", BERLIN),
            sorted([E("2026-10-17 23:30"), E("2026-10-18 00:30"), E("2026-10-28 11:00")]),
        )

    def test_year_starts_first_sunday(self) -> None:
        snaps = [snap("2026-07-01 10:00"), snap("2027-01-01 10:00"), snap("2027-01-03 10:00"), snap("2027-05-31 10:00")]
        self.assertEqual(
            kept(snaps, "2027-06-01 12:00", "1y", BERLIN),
            sorted([E("2026-07-01 10:00"), E("2027-01-03 10:00"), E("2027-05-31 10:00")]),
        )

    def test_leap_day(self) -> None:
        snaps = [snap(f"2028-{md} 10:00") for md in ("02-26", "02-27", "02-28", "02-29", "03-01", "03-02")]
        self.assertEqual(
            kept(snaps, "2028-03-02 12:00", "3d", BERLIN),
            sorted(E(f"2028-{md} 10:00") for md in ("02-28", "02-29", "03-01", "03-02")),
        )
        hours = hourly_series("2028-02-28 00:00", "2028-03-01 05:00")
        d = decide(hours, "2028-03-01 05:30", "29h", BERLIN)
        self.assertEqual(d[E("2028-02-29 00:05")], "keep")  # 29 h
        self.assertEqual(d[E("2028-02-28 23:05")], "delete")  # 30 h

    def test_dst_spring_berlin(self) -> None:
        # 2026-03-29 02:00 CET -> 03:00 CEST: hour 02 does not exist.
        snaps = [snap("2026-03-29 00:10"), snap("2026-03-29 01:10"), snap("2026-03-29 03:10"), snap("2026-03-29 04:10")]
        # btrbk counts wall-clock hours: 01:10 -> 05:30 is hour 4 although 3h20m passed.
        d = decide(snaps, "2026-03-29 05:30", "3h", BERLIN)
        self.assertEqual(d[E("2026-03-29 01:10")], "delete")
        self.assertEqual(d[E("2026-03-29 03:10")], "keep")
        self.assertEqual(decide(snaps, "2026-03-29 05:30", "4h", BERLIN)[E("2026-03-29 01:10")], "keep")
        # A name in the gap (clock set by hand) reads as one hour later, like timelocal().
        gap = snap("2026-03-29 02:30")
        self.assertEqual(core.epoch_to_wall(core.wall_to_epoch(gap.wall, BERLIN), BERLIN), W("2026-03-29 03:30"))
        d = decide(snaps + [gap], "2026-03-29 05:30", "4h", BERLIN)
        self.assertEqual(d[gap.entry], "delete")  # hour 03's first is 03:10
        self.assertEqual(d[E("2026-03-29 03:10")], "keep")

    def test_dst_autumn_berlin(self) -> None:
        # 2026-10-25 03:00 CEST -> 02:00 CET: 02:30 happens twice -> _1 suffix.
        snaps = [
            snap("2026-10-25 01:30"),
            snap("2026-10-25 02:30"),
            snap("2026-10-25 02:30", 1),
            snap("2026-10-25 03:30"),
        ]
        d = decide(snaps, "2026-10-25 04:00", "5h", BERLIN)
        self.assertEqual(
            d,
            {
                E("2026-10-25 01:30"): "keep",
                E("2026-10-25 02:30"): "keep",
                E("2026-10-25 02:30", 1): "delete",
                E("2026-10-25 03:30"): "keep",
            },
        )
        # Day boundary unaffected: the 25th has 25 hours, all one day.
        day = hourly_series("2026-10-24 22:00", "2026-10-26 01:00")
        self.assertEqual(
            kept(day, "2026-10-26 01:30", "1d", BERLIN),
            sorted([E("2026-10-25 00:05"), E("2026-10-26 00:05"), E("2026-10-26 01:05")]),
        )

    def test_dst_new_york(self) -> None:
        # 2026-03-08 02:00 EST -> 03:00 EDT; 2026-11-01 02:00 EDT -> 01:00 EST.
        snaps = [snap("2026-03-07 23:30"), snap("2026-03-08 00:30"), snap("2026-03-08 03:30"), snap("2026-03-09 06:00")]
        self.assertEqual(
            kept(snaps, "2026-03-09 12:00", "1d", NEW_YORK), sorted([E("2026-03-08 00:30"), E("2026-03-09 06:00")])
        )
        snaps = [
            snap("2026-11-01 00:30"),
            snap("2026-11-01 01:30"),
            snap("2026-11-01 01:30", 1),
            snap("2026-11-01 02:30"),
        ]
        d = decide(snaps, "2026-11-01 03:00", "3h", NEW_YORK)
        self.assertEqual(
            d,
            {
                E("2026-11-01 00:30"): "keep",
                E("2026-11-01 01:30"): "keep",
                E("2026-11-01 01:30", 1): "delete",
                E("2026-11-01 02:30"): "keep",
            },
        )
        # Weekly across the autumn change (the 1st is a Sunday: new week, new month).
        snaps = [snap("2026-10-31 23:30"), snap("2026-11-01 00:30"), snap("2026-11-02 00:30")]
        self.assertEqual(
            kept(snaps, "2026-11-03 09:00", "0h 1w", NEW_YORK),
            sorted([E("2026-10-31 23:30"), E("2026-11-01 00:30"), E("2026-11-02 00:30")]),
        )
        self.assertEqual(
            kept(snaps, "2026-11-03 09:00", "1m", NEW_YORK),
            sorted([E("2026-10-31 23:30"), E("2026-11-01 00:30"), E("2026-11-02 00:30")]),
        )

    def test_lord_howe_half_hour_dst_end(self) -> None:
        # Documented deviation from btrbk (see wall_to_epoch()): 2027-04-04 02:00
        # LHDT (+11) -> 01:30 LHST (+10:30), so 01:30-01:59 happens twice. We read
        # 01:38 as the earlier instant, 14:38 UTC, which localtime() shows as
        # 01:38 again; btrbk's Time::Local 1.35 says 14:08 UTC (shown as 01:08),
        # orders 01:38 before 01:09 and so keeps 01:38 as the first of the year.
        lh = zoneinfo.ZoneInfo("Australia/Lord_Howe")
        e = core.wall_to_epoch(W("2027-04-04 01:38"), lh)
        self.assertEqual(dt.datetime.fromtimestamp(e, dt.UTC), dt.datetime(2027, 4, 3, 14, 38, tzinfo=dt.UTC))
        self.assertEqual(core.epoch_to_wall(e, lh), W("2027-04-04 01:38"))
        rows = core.plan(
            [snap("2027-04-04 01:09"), snap("2027-04-04 01:38")],
            core.wall_to_epoch(W("2027-04-05 03:41"), lh),
            core.parse_policy("72h 7d 4w 3m 2y"),
            lh,
        )
        self.assertEqual(
            [(r.snap.entry, r.reason.split(":")[0]) for r in rows],
            [(E("2027-04-04 01:09"), "preserve yearly"), (E("2027-04-04 01:38"), "preserve min")],
        )

    def test_gap_after_suspend(self) -> None:
        before = hourly_series("2026-06-08 00:00", "2026-06-10 18:00")
        after = hourly_series("2026-06-14 09:00", "2026-06-15 10:00")
        snaps = before + after
        policy = "72h 7d 4w 3m"
        d = decide(snaps, "2026-06-15 10:30", policy, BERLIN)
        self.assertEqual(d[E("2026-06-10 18:05")], "delete")  # hour 112, not first of day
        self.assertEqual(d[E("2026-06-10 00:05")], "keep")  # first of day 5
        self.assertEqual(d[E("2026-06-08 00:05")], "keep")  # first of day 7 (and of its week)
        self.assertTrue(all(d[s.entry] == "keep" for s in after))
        ref = reference_keep(snaps, at("2026-06-15 10:30", BERLIN), core.parse_policy(policy), BERLIN)
        self.assertEqual({e for e, a in d.items() if a == "keep"}, ref)

    def test_future_dates_are_kept(self) -> None:
        snaps = [snap("2026-10-01 10:00"), snap("2026-10-13 09:00"), snap("2026-10-14 08:00")]
        rows = core.plan(snaps, at("2026-10-13 12:00", BERLIN), core.parse_policy("1h"), BERLIN)
        self.assertEqual([r.action for r in rows], ["delete", "delete", "keep"])
        # The reason is the last rule that matched (btrbk): with no hourly rule, "forced".
        rows = core.plan(
            snaps + [snap("2026-10-14 09:00")], at("2026-10-13 12:00", BERLIN), core.parse_policy("0h"), BERLIN
        )
        self.assertEqual([r.action for r in rows], ["delete", "delete", "keep", "keep"])
        self.assertEqual(rows[2].reason, "preserve forced: 20 hours in the future")

    def test_zero_count_disables_unit(self) -> None:
        # btrbk treats a 0 count as "not set" (Perl false): 0h keeps no hourly.
        snaps = [snap("2026-10-13 10:05"), snap("2026-10-13 11:05"), snap("2026-10-13 11:30")]
        self.assertEqual(kept(snaps, "2026-10-13 11:40", "0h", BERLIN), [E("2026-10-13 11:30")])

    def test_reasons_match_btrbk_wording(self) -> None:
        snaps = [snap("2026-08-02 00:00"), snap("2026-10-05 03:00"), snap("2026-10-13 05:00"), snap("2026-10-13 11:00")]
        rows = core.plan(snaps, at("2026-10-13 11:30", BERLIN), core.parse_policy("2h 1d 1w 3m"), BERLIN)
        self.assertEqual(
            [r.reason for r in rows],
            [
                "preserve monthly: first weekly of month 2026-08 (2 months ago, at sunday 00:00)",
                # 2026-10-04 is October's first Sunday: the 5th is the first weekly of October.
                "preserve monthly: first weekly of month 2026-10 (0 months ago, 1d 3h after sunday 00:00)",
                "preserve weekly: 0 weeks ago, 2d 5h after sunday 00:00",
                "preserve hourly: first of hour, 0 hours ago",
            ],
        )
        rows = core.plan(snaps, at("2026-10-13 11:30", BERLIN), core.parse_policy("12h 1d"), BERLIN)
        self.assertEqual(rows[2].reason, "preserve daily: first of day, 0 days ago, 5h after 00:00")


class RetentionPropertyTests(unittest.TestCase):
    CASES = 120

    def cases(
        self, seed: int
    ) -> Iterator[tuple[random.Random, zoneinfo.ZoneInfo, list[tuple[int, _SnapProto]], int, dict[str, int | str]]]:
        rng = random.Random(seed)
        for _ in range(self.CASES):
            tz = zoneinfo.ZoneInfo(rng.choice(RANDOM_TZS))
            timeline = random_timeline(rng, tz, rng.randrange(1, 160))
            now = timeline[-1][0] + rng.randrange(0, 72 * 3600)
            yield rng, tz, timeline, now, core.parse_policy(rng.choice(RANDOM_POLICIES))

    def test_newest_kept_and_matches_reference(self) -> None:
        for _rng, tz, timeline, now, policy in self.cases(1):
            snaps = [s for _, s in timeline]
            rows = core.plan(snaps, now, policy, tz)
            self.assertEqual(rows[-1].action, "keep")
            got = {r.snap.entry for r in rows if r.action == "keep"}
            self.assertEqual(got, reference_keep(snaps, now, policy, tz), (tz, core.format_policy(policy)))

    def test_idempotent(self) -> None:
        for _rng, tz, timeline, now, policy in self.cases(2):
            rows = core.plan([s for _, s in timeline], now, policy, tz)
            remaining = [r.snap for r in rows if r.action == "keep"]
            again = core.plan(remaining, now, policy, tz)
            self.assertEqual([r.action for r in again], ["keep"] * len(remaining))

    def test_incremental_equals_batch(self) -> None:
        for _rng, tz, timeline, _now, policy in self.cases(3):
            have: list[_SnapProto] = []
            for created, s in timeline:
                have.append(s)
                have = [r.snap for r in core.plan(have, created, policy, tz) if r.action == "keep"]
            last = timeline[-1][0]
            batch = [r.snap.entry for r in core.plan([s for _, s in timeline], last, policy, tz) if r.action == "keep"]
            self.assertEqual(sorted(s.entry for s in have), sorted(batch), (tz, core.format_policy(policy)))

    def test_totals_bounded(self) -> None:
        for _rng, tz, timeline, now, policy in self.cases(4):
            limit = core.policy_max_kept(policy)
            rows = core.plan([s for _, s in timeline], now, policy, tz)
            if limit is not None:
                self.assertLessEqual(sum(1 for r in rows if r.action == "keep"), limit)
        # And a dense year of 15-minute snapshots stays within the policy's count.
        snaps = hourly_series("2025-10-01 00:00", "2026-10-01 00:00", minute=0, step_minutes=15)
        policy = core.parse_policy("72h 7d 4w 3m")
        rows = core.plan(snaps, at("2026-10-01 00:10", BERLIN), policy, BERLIN)
        n = sum(1 for r in rows if r.action == "keep")
        max_kept = core.policy_max_kept(policy)
        assert max_kept is not None
        self.assertLessEqual(n, max_kept)
        self.assertGreaterEqual(n, 73 + 5)


# ---- btrbk cross-check (optional) -----------------------------------------------------
PERL_HARNESS = r"""
use strict; use warnings;
use Time::Local qw( timelocal timegm timegm_nocheck );
our %day_of_week_map = ( sunday => 0, monday => 1, tuesday => 2, wednesday => 3,
                         thursday => 4, friday => 5, saturday => 6 );
our @tm_now; our $do_trace = 0;
sub DEBUG {} sub TRACE {} sub timestamp { return ""; }
my $src = do { local $/; open(my $fh, "<", $ENV{BTRBK_SUBS}) or die; <$fh> };
eval $src; die $@ if $@;
my ($now, $policy) = @ARGV;
@tm_now = localtime($now);
my %preserve = ( hod => 0, dow => 'sunday', min => 'latest', min_q => 'latest' );
foreach (split /[\s,]+/, $policy) {
  /^(\*|\d+)([hdwmy])$/ or die "policy $_";
  $preserve{$2} = ($1 eq '*') ? 'all' : $1;
}
my @schedule; my $i = 0;
while (my $line = <STDIN>) {
  my ($Y, $M, $D, $h, $m, $nn) = split ' ', $line;
  push @schedule, { value => $i++, btrbk_date => [ timelocal(0, $m, $h, $D, $M - 1, $Y - 1900), $nn, 1 ] };
}
my $results = [];
schedule(schedule => \@schedule, preserve => \%preserve, preserve_date_in_future => 1, results => $results);
foreach (sort { $a->{value} <=> $b->{value} } @$results) {
  print $_->{value}, "\t", (($_->{action} // '') eq 'delete' ? 'delete' : 'keep'), "\t", ($_->{reason} // ''), "\n";
}
"""


@unittest.skipUnless(
    os.environ.get("BTRBK_SOURCE") and shutil.which("perl"),
    "set BTRBK_SOURCE=/path/to/btrbk (0.32.7) and have perl to compare with btrbk itself",
)
class BtrbkOracleTests(unittest.TestCase):
    def setUp(self) -> None:
        with open(os.environ["BTRBK_SOURCE"]) as f:
            text = f.read()
        start = text.index("\nsub cmp_date(")
        end = text.index("\nsub timestamp(")
        self.tmp = tempfile.TemporaryDirectory()
        self.subs = os.path.join(self.tmp.name, "subs.pl")
        self.harness = os.path.join(self.tmp.name, "harness.pl")
        with open(self.subs, "w") as f:
            f.write(text[start:end] + "\n1;\n")
        with open(self.harness, "w") as f:
            f.write(PERL_HARNESS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def btrbk(
        self, snaps: Sequence[_SnapProto], now: float, policy_text: str, tzname: str
    ) -> dict[int, tuple[str, str]]:
        lines = "".join(f"{s.wall:%Y %m %d %H %M} {s.seq}\n" for s in snaps)
        env = {"PATH": os.environ.get("PATH", "/usr/bin"), "TZ": tzname, "BTRBK_SUBS": self.subs}
        out = subprocess.run(
            ["perl", self.harness, str(int(now)), policy_text],
            input=lines,
            text=True,
            capture_output=True,
            env=env,
            check=True,
        ).stdout
        return {int(i): (a, r) for i, a, r in (ln.split("\t") for ln in out.splitlines())}

    def compare(self, snaps: Sequence[_SnapProto], now: float, policy_text: str, tzname: str) -> None:
        tz = zoneinfo.ZoneInfo(tzname)
        ours = core.plan(snaps, now, core.parse_policy(policy_text), tz)
        theirs = self.btrbk(snaps, now, policy_text, tzname)
        index = {id(s): i for i, s in enumerate(snaps)}
        for r in ours:
            action, reason = theirs[index[id(r.snap)]]
            self.assertEqual(
                (r.action, r.reason if r.action == "keep" else ""),
                (action, reason),
                (r.snap.entry, tzname, policy_text, now),
            )

    def test_random_against_btrbk(self) -> None:
        # Not Australia/Lord_Howe: Perl's Time::Local assumes one-hour DST shifts and
        # misreads its 30-minute one (01:38 on the change day comes back as 01:08);
        # zoneinfo gets it right, so there the two differ by design (documented in
        # wall_to_epoch() and test_lord_howe_half_hour_dst_end).
        rng = random.Random(99)
        zones = [z for z in RANDOM_TZS if z != "Australia/Lord_Howe"] + ["Europe/London", "Asia/Tehran"]
        for _ in range(80):
            tzname = rng.choice(zones)
            tz = zoneinfo.ZoneInfo(tzname)
            timeline = random_timeline(rng, tz, rng.randrange(1, 160))
            now = timeline[-1][0] + rng.randrange(-3600, 72 * 3600)  # also a few future-dated ones
            self.compare([s for _, s in timeline], now, rng.choice(RANDOM_POLICIES).replace(",", " "), tzname)

    def test_dst_edges_against_btrbk(self) -> None:
        for tzname, day in (
            ("Europe/Berlin", "2026-03-29"),
            ("Europe/Berlin", "2026-10-25"),
            ("America/New_York", "2026-03-08"),
            ("America/New_York", "2026-11-01"),
        ):
            tz = zoneinfo.ZoneInfo(tzname)
            snaps = [snap(f"{day} {h:02d}:{m:02d}", seq) for h in range(0, 5) for m in (0, 30) for seq in (0, 1)]
            for now_h in ("05:10", "23:00"):
                for policy in ("3h", "2h 1d", "1h 1d 1w 1m 1y"):
                    self.compare(snaps, at(f"{day} {now_h}", tz), policy, tzname)


# ---- the command line with a fake btrfs ------------------------------------------------
def uuid_n(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


class FakeHost:
    """A tiny in-memory btrfs: subvolumes with UUID/parent/generation/ro.

    Time: `now` is the wall clock; CLOCK_BOOTTIME is derived from it (now -
    boot_at), so advancing `now` is real time passing. step_clock() moves only
    the wall clock (a clock jump), reboot() starts a new boot."""

    def __init__(self, now: float, tz: dt.tzinfo = BERLIN) -> None:
        self.tz = tz
        self.now = float(now)
        self.boot_id = uuid_n(0xB007)
        self.boot_at = self.now - 10 * 86400
        self.subvols: dict[str, dict[str, Any]] = {}  # path -> dict
        self.other: dict[str, str] = {}  # path -> "symlink" | "other"
        self.dirs = {"/top", "/top/snapshots"}
        self.calls: list[list[Any]] = []
        self.ntp = "yes"
        self.transid = 5000
        self.next_uuid = 1
        self.on_delete: Callable[[FakeHost, str], None] | None = None
        self.on_listdir: Callable[[FakeHost, int], None] | None = None
        self.listdir_calls = 0
        self.show_override: dict[str, str] = {}  # path -> raw text
        self.on_snapshot: Callable[[FakeHost, str], None] | None = (
            None  # callback(host, dest) before a snapshot is made
        )
        self.set_ro_rc = 0  # rc of `btrfs property set ... ro true`
        self.walk_error: Exception | None = None  # exception walk_caches raises
        self.walk_result: Callable[[str, Sequence[str], str], _CacheWalkProto] | None = (
            None  # callable(root, skips, mode) -> CacheWalk
        )

    # helpers
    def new_uuid(self) -> str:
        self.next_uuid += 1
        return uuid_n(self.next_uuid)

    def add_source(self, path: str, generation: int) -> str:
        u = self.new_uuid()
        self.subvols[path] = {"uuid": u, "parent": None, "gen": generation, "cgen": 9, "ro": False}
        return u

    def add_snapshot(
        self,
        entry: str,
        source: str,
        cgen: int,
        ro: bool = True,
        parent: str | None = None,
        snapdir: str = "/top/snapshots",
    ) -> str:
        path = f"{snapdir}/{entry}"
        self.subvols[path] = {
            "uuid": self.new_uuid(),
            "parent": parent if parent is not None else self.subvols[source]["uuid"],
            "gen": cgen,
            "cgen": cgen,
            "ro": ro,
        }
        return path

    def snapshots(self, snapdir: str = "/top/snapshots") -> list[str]:
        return sorted(os.path.basename(p) for p in self.subvols if os.path.dirname(p) == snapdir)

    def mutating_calls(self) -> list[list[Any]]:
        return [
            c
            for c in self.calls
            if c[:3]
            in (["btrfs", "subvolume", "snapshot"], ["btrfs", "subvolume", "delete"], ["btrfs", "property", "set"])
            or (c[0] == "walk" and c[3] == "drop")
        ]

    def walk_caches(self, root: str, skips: Sequence[str], mode: str) -> _CacheWalkProto:
        self.calls.append(["walk", root, list(skips), mode])
        if self.walk_error:
            raise self.walk_error
        if self.walk_result:
            return self.walk_result(root, skips, mode)
        return core.CacheWalk([core.CacheDir("u/proj/target", 3, 1024)], [], list(skips), [], 0.5)

    def step_clock(self, seconds: float) -> None:
        """The wall clock jumps; the time since boot does not."""
        self.now += seconds
        self.boot_at += seconds

    def reboot(self, uptime: float = 60) -> None:
        self.boot_id = uuid_n(self.next_uuid + 0xB000)
        self.next_uuid += 1
        self.boot_at = self.now - uptime

    # Host interface
    def time(self) -> float:
        return self.now

    def boot(self) -> tuple[str | None, float | None]:
        return self.boot_id, float(self.now - self.boot_at)

    def listdir(self, path: str) -> list[str]:
        self.listdir_calls += 1
        if self.on_listdir:
            self.on_listdir(self, self.listdir_calls)
        if path not in self.dirs:
            raise FileNotFoundError(path)
        names = {
            os.path.basename(p)
            for p in list(self.subvols) + list(self.other) + list(self.dirs)
            if os.path.dirname(p) == path and p != path
        }
        return sorted(names)

    def kind(self, path: str) -> str:
        if path in self.subvols:
            return "subvol"
        if path in self.other:
            return self.other[path]
        if path in self.dirs:
            return "other"
        return "missing"

    def mkdir(self, path: str, mode: int) -> None:
        self.dirs.add(path)

    def show_text(self, path: str, sv: Mapping[str, Any]) -> str:
        return (
            f"{os.path.basename(path)}\n\tName: \t\t\t{os.path.basename(path)}\n"
            f"\tUUID: \t\t\t{sv['uuid']}\n\tParent UUID: \t\t{sv['parent'] or '-'}\n"
            f"\tReceived UUID: \t\t-\n\tCreation time: \t\t2026-05-08 23:21:53 +0200\n"
            f"\tSubvolume ID: \t\t300\n\tGeneration: \t\t{sv['gen']}\n\tGen at creation: \t{sv['cgen']}\n"
            f"\tParent ID: \t\t5\n\tTop level ID: \t\t5\n\tFlags: \t\t\t{'readonly' if sv['ro'] else '-'}\n"
            f"\tSnapshot(s):\n\tQuota group:\t\tn/a\n"
        )

    def run(self, argv: Sequence[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if argv[:3] == ["btrfs", "subvolume", "show"]:
            path = argv[3]
            if path in self.show_override:
                return 0, self.show_override[path], ""
            sv = self.subvols.get(path)
            if sv is None:
                return 1, "", f"ERROR: Not a Btrfs subvolume: {path}"
            return 0, self.show_text(path, sv), ""
        if argv[:4] == ["btrfs", "property", "get", "-ts"] and argv[5] == "ro":
            sv = self.subvols.get(argv[4])
            if sv is None:
                return 1, "", "ERROR: not a subvolume"
            return 0, f"ro={'true' if sv['ro'] else 'false'}\n", ""
        if argv[:3] == ["btrfs", "subvolume", "snapshot"]:
            ro = argv[3] == "-r"
            src, dest = argv[4:6] if ro else argv[3:5]
            if self.on_snapshot:
                self.on_snapshot(self, dest)
            if dest in self.subvols or dest in self.other or os.path.dirname(dest) not in self.dirs:
                return 1, "", "ERROR: cannot snapshot"
            self.transid += 1
            # Kernel >= 7.1: the source's generation stays behind the new cgen while idle.
            self.subvols[dest] = {
                "uuid": self.new_uuid(),
                "parent": self.subvols[src]["uuid"],
                "gen": self.transid,
                "cgen": self.transid,
                "ro": ro,
            }
            return 0, f"Create {'readonly ' if ro else ''}snapshot of '{src}' in '{dest}'\n", ""
        if argv[:4] == ["btrfs", "property", "set", "-ts"] and argv[5:] == ["ro", "true"]:
            sv = self.subvols.get(argv[4])
            if sv is None:
                return 1, "", "ERROR: not a subvolume"
            if self.set_ro_rc:
                return self.set_ro_rc, "", "ERROR: failed to set flags"
            sv["ro"] = True
            return 0, "", ""
        if argv[:3] == ["btrfs", "subvolume", "delete"]:
            path = argv[3]
            if self.on_delete:
                self.on_delete(self, path)
            if path not in self.subvols:
                return 1, "", "ERROR: no such subvolume"
            del self.subvols[path]
            return 0, f"Delete subvolume '{path}'\n", ""
        if argv[:1] == ["timedatectl"]:
            return 0, self.ntp + "\n", ""
        raise AssertionError(f"unexpected command {argv}")


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")
        self.lock = os.path.join(self.tmp.name, "lock")
        self.pins = os.path.join(self.tmp.name, "pinned")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def host(self, now_text: str, gen: int = 100) -> FakeHost:
        h = FakeHost(at(now_text, BERLIN))
        h.add_source("/top/home", gen)
        return h

    def cli(
        self,
        host: FakeHost,
        cmd: str,
        *extra: str,
        source: str = "home=/top/home:72h 7d 4w 3m",
        sources: Sequence[str] | None = None,
        clock_ok: bool = False,
        snapdir: str = "/top/snapshots",
    ) -> int:
        argv = [cmd, "--snapdir", snapdir]
        if cmd != "latest":
            argv += ["--tz", "Europe/Berlin"]
        for s in sources if sources is not None else [source]:
            argv += ["--source", s]
        if cmd in ("snapshot", "prune", "list", "finish", "caches"):
            argv += ["--state-dir", self.state, "--pin-file", self.pins]
        if cmd in ("snapshot", "prune", "finish"):
            argv += ["--lock", self.lock]
        if clock_ok:
            argv += ["--assume-clock-ok"]
        argv += list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = core.main(argv, host=host)
            except SystemExit as e:  # argparse
                rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def add_history(
        self,
        host: FakeHost,
        start: str,
        end: str,
        step_hours: int = 1,
        source: str = "/top/home",
        name: str = "home",
    ) -> list[str]:
        """Old snapshots with increasing cgen; returns entries."""
        entries = []
        t, stop = W(start), W(end)
        cgen = 10
        while t <= stop:
            e = core.snapshot_name(name, t)
            host.add_snapshot(e, source, cgen)
            entries.append(e)
            cgen += 1
            t += dt.timedelta(hours=step_hours)
        host.subvols[source]["gen"] = max(host.subvols[source]["gen"], cgen)
        return entries


class ChangeDetectionTests(CliTestCase):
    def test_first_run_creates(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(h.snapshots(), ["home.20261013T1407"])
        self.assertIn("summary: home: created home.20261013T1407", self.out)
        self.assertTrue(os.path.exists(os.path.join(self.state, core.STATE_FILE)))

    def test_the_last_run_record_does_not_sync_its_folder(self) -> None:
        # It is fsynced itself; a folder fsync on every run (twice an hour)
        # would only cost a btrfs log sync.
        h = self.host("2026-10-13 14:07")
        folders = []
        real = os.fsync

        def fsync(fd: int) -> None:
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                folders.append(fd)
            return real(fd)

        with mock.patch.object(core.os, "fsync", fsync):
            self.assertEqual(self.cli(h, "snapshot", clock_ok=True), 0)
        with open(os.path.join(self.state, core.STATE_FILE)) as f:
            self.assertEqual(f.read().split()[0], str(int(h.now)))
        self.assertEqual(folders, [])

    def test_idle_source_creates_nothing(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        h.now += 3600
        self.assertEqual(self.cli(h, "snapshot"), 0)  # gen 100 < cgen 5001 (kernel >= 7.1)
        self.assertEqual(h.snapshots(), ["home.20261013T1407"])
        self.assertIn("unchanged", self.out)

    def test_equal_generation_is_unchanged(self) -> None:
        # Older kernels: snapshot creation bumps the source to the same generation.
        h = self.host("2026-10-13 14:07", gen=500)
        h.add_snapshot("home.20261013T1300", "/top/home", 500)
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(h.snapshots(), ["home.20261013T1300"])

    def test_write_creates(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.cli(h, "snapshot")
        h.now += 3600
        h.subvols["/top/home"]["gen"] = h.transid + 7
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(h.snapshots(), ["home.20261013T1407", "home.20261013T1507"])

    def test_newest_by_generation_not_name(self) -> None:
        h = self.host("2026-10-13 14:07", gen=300)
        # The name says 13:00 is newer, but 12:00 was created later (clock stepped back).
        h.add_snapshot("home.20261013T1300", "/top/home", 200)
        h.add_snapshot("home.20261013T1200", "/top/home", 400)
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(len(h.snapshots()), 2)  # 300 < 400: unchanged
        h.subvols["/top/home"]["gen"] = 401
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(len(h.snapshots()), 3)

    def test_safety_net_after_24h(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20261012T1406", "/top/home", 50)
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertIn("home.20261013T1407", h.snapshots())
        self.assertIn("older than 24 h", self.out)
        h2 = self.host("2026-10-13 14:07", gen=10)
        h2.add_snapshot("home.20261012T1408", "/top/home", 50)
        self.cli(h2, "snapshot")
        self.assertEqual(h2.snapshots(), ["home.20261012T1408"])

    def test_force(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20261013T1400", "/top/home", 50)
        self.assertEqual(self.cli(h, "snapshot", "--force"), 0)
        self.assertEqual(len(h.snapshots()), 2)

    def test_collision_suffix(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.cli(h, "snapshot")
        for expected in ("home.20261013T1407_1", "home.20261013T1407_2"):
            self.assertEqual(self.cli(h, "snapshot", "--force"), 0)
            self.assertIn(expected, h.snapshots())
        # Any entry with the name counts, even one that is not ours.
        h.other["/top/snapshots/home.20261013T1407_7"] = "other"
        self.cli(h, "snapshot", "--force")
        self.assertIn("home.20261013T1407_8", h.snapshots())

    def test_local_time_names(self) -> None:
        h = FakeHost(at("2026-07-01 23:30", NEW_YORK))
        h.add_source("/top/home", 100)
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(h.snapshots(), ["home.20260702T0530"])  # rendered in --tz Europe/Berlin

    def test_two_sources(self) -> None:
        h = self.host("2026-10-13 14:07")
        h.add_source("/top/home/u/Workspace", 100)
        rc = self.cli(h, "snapshot", sources=["home=/top/home:72h 7d", "Workspace=/top/home/u/Workspace:48h 7d"])
        self.assertEqual(rc, 0)
        self.assertEqual(h.snapshots(), ["Workspace.20261013T1407", "home.20261013T1407"])
        ws = h.subvols["/top/snapshots/Workspace.20261013T1407"]
        self.assertEqual(ws["parent"], h.subvols["/top/home/u/Workspace"]["uuid"])

    def test_missing_snapdir_is_created(self) -> None:
        h = self.host("2026-10-13 14:07")
        h.dirs.discard("/top/snapshots")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertIn("/top/snapshots", h.dirs)
        self.assertEqual(self.cli(h, "prune"), 0)

    def test_source_errors(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/nothere:1d"), 1)
        h.show_override["/top/home"] = "home\n\tUUID: \t\tgarbage\n"
        self.assertEqual(self.cli(h, "snapshot"), 1)
        self.assertEqual(h.mutating_calls(), [])

    def test_usage(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:7q"), 2)
        self.assertEqual(self.cli(h, "snapshot", sources=["a=/top/home:1d", "b=/top/home:1d"]), 2)
        self.assertEqual(self.cli(h, "snapshot", sources=["a=/top/home:1d", "a=/top/x:1d"]), 2)
        self.assertEqual(self.cli(h, "frobnicate"), 2)


class OwnershipTests(CliTestCase):
    def setup_mixed(self) -> tuple[FakeHost, list[str]]:
        h = self.host("2026-10-13 14:07", gen=10)
        old = self.add_history(h, "2026-06-01 00:00", "2026-06-01 03:00")  # due for deletion
        h.subvols["/top/home"]["gen"] = 10
        other = h.add_source("/top/other", 10)
        h.add_snapshot("home.20260601T0030", "/top/home", 900, ro=False)  # writable
        h.add_snapshot("home.20260601T0045", "/top/home", 901, parent=other)  # other source
        h.add_snapshot("home.20260601T0050", "/top/home", 902, parent=None)
        h.subvols["/top/snapshots/home.20260601T0050"]["parent"] = None  # no parent at all
        h.other["/top/snapshots/home.20260601T0055"] = "symlink"
        h.other["/top/snapshots/home.20260601T0056"] = "other"  # plain directory
        h.add_snapshot("home.20261399T0000", "/top/home", 903)  # invalid date
        h.add_snapshot("home.20260601T0057.bak", "/top/home", 904)  # name mismatch
        h.add_snapshot("homeX20260601T0058", "/top/home", 905)
        for e in ("home.20261012T0900", "home.20261013T0900", "home.20261013T1000", "home.20261013T1100"):
            h.add_snapshot(e, "/top/home", 1000 + len(e))
        return h, old

    def test_only_owned_are_deleted(self) -> None:
        h, old = self.setup_mixed()
        before = set(h.snapshots())
        rc = self.cli(h, "snapshot", source="home=/top/home:24h", clock_ok=True)
        self.assertEqual(rc, 0, self.out)
        deleted = before - set(h.snapshots())
        self.assertEqual(deleted, set(old) | {"home.20261012T0900"})
        for e in (
            "home.20260601T0030",
            "home.20260601T0045",
            "home.20260601T0050",
            "home.20261399T0000",
            "home.20260601T0057.bak",
            "homeX20260601T0058",
        ):
            self.assertIn(e, h.snapshots())

    def test_list_shows_ignored(self) -> None:
        h, _ = self.setup_mixed()
        self.assertEqual(self.cli(h, "list", "--json", source="home=/top/home:24h"), 0)
        data = json.loads(self.out)["sources"][0]
        reasons = {os.path.basename(i["path"]): i["reason"] for i in data["ignored"]}
        self.assertEqual(reasons["home.20260601T0030"], "not read-only")
        self.assertIn("another subvolume", reasons["home.20260601T0045"])
        self.assertIn("another subvolume", reasons["home.20260601T0050"])
        self.assertEqual(reasons["home.20260601T0055"], "symlink")
        self.assertEqual(reasons["home.20260601T0056"], "not a subvolume")
        self.assertEqual(reasons["home.20261399T0000"], "invalid date in the name")
        self.assertNotIn("home.20260601T0057.bak", reasons)
        self.assertEqual(
            {s["entry"] for s in data["snapshots"] if s["action"] == "delete"},
            {
                "home.20260601T0000",
                "home.20260601T0100",
                "home.20260601T0200",
                "home.20260601T0300",
                "home.20261012T0900",
            },
        )
        self.assertEqual(h.mutating_calls(), [])
        self.assertEqual(self.cli(h, "list", source="home=/top/home:24h"), 0)
        self.assertIn("IGNORED home.20260601T0030", self.out)

    def test_unparsable_entry_blocks_deletions(self) -> None:
        h, old = self.setup_mixed()
        h.show_override["/top/snapshots/home.20260601T0300"] = "home.20260601T0300\n\tUUID: \t\tbroken\n"
        before = h.snapshots()
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:24h", clock_ok=True), 4)
        self.assertEqual(h.snapshots(), before)
        self.assertIn("could not be verified", self.out)


class SafeguardTests(CliTestCase):
    def test_three_newest_and_24h_kept(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        for e, cgen in (("home.20260101T0000", 11), ("home.20260102T0000", 12), ("home.20260103T0000", 13)):
            h.add_snapshot(e, "/top/home", cgen)
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertEqual(len(h.snapshots()), 3)
        self.cli(h, "list", source="home=/top/home:1h", clock_ok=True)
        self.assertIn("safeguard: one of the 3 newest", self.out)
        # Younger than 24 h: kept even when the policy drops it.
        for e, cgen in (
            ("home.20261012T1500", 14),
            ("home.20261012T1600", 15),
            ("home.20261013T1000", 16),
            ("home.20261013T1100", 17),
            ("home.20261013T1200", 18),
        ):
            h.add_snapshot(e, "/top/home", cgen)
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertEqual(
            h.snapshots(),
            [
                "home.20261012T1500",
                "home.20261012T1600",
                "home.20261013T1000",
                "home.20261013T1100",
                "home.20261013T1200",
            ],
        )

    def test_pinned_kept(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        old = self.add_history(h, "2026-06-01 00:00", "2026-06-01 03:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        with open(self.pins, "w") as f:
            f.write(f"/top/snapshots/{old[1]}\n\n/top/snapshots/./{old[2]}\n")
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertEqual(sorted(set(old) & set(h.snapshots())), [old[1], old[2]])

    def test_unreadable_pin_file_blocks(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        self.add_history(h, "2026-06-01 00:00", "2026-06-01 03:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        os.mkdir(self.pins)  # reading a directory fails
        before = h.snapshots()
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 4)
        self.assertEqual(h.snapshots(), before)


class ClockGuardTests(CliTestCase):
    def history(self, now_text: str) -> tuple[FakeHost, list[str]]:
        h = self.host(now_text, gen=10)
        old = self.add_history(h, "2026-06-01 00:00", "2026-06-01 02:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        return h, old

    def recorded(self) -> int | None:
        """The recorded last successful run (epoch), or None."""
        try:
            with open(os.path.join(self.state, core.STATE_FILE)) as f:
                return int(f.read().split()[0])
        except FileNotFoundError:
            return None

    def test_ntp_unsynchronized_blocks_deletions_only(self) -> None:
        h, old = self.history("2026-10-13 14:07")
        h.ntp = "no"
        h.subvols["/top/home"]["gen"] = 10_000
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h"), 4)
        self.assertIn("home.20261013T1407", h.snapshots())  # creation still happened
        self.assertIn("the name uses the unverified time", self.out)
        self.assertTrue(set(old) <= set(h.snapshots()))
        self.assertIn("not NTP-synchronized", self.out)
        self.assertIsNone(self.recorded())
        h.ntp = "yes"
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h"), 0)
        self.assertFalse(set(old) & set(h.snapshots()))

    def test_unsynchronized_run_is_not_recorded(self) -> None:
        h = self.host("2026-10-13 14:07")
        h.ntp = "no"
        self.assertEqual(self.cli(h, "snapshot"), 0)  # nothing to refuse
        self.assertEqual(h.snapshots(), ["home.20261013T1407"])
        self.assertIsNone(self.recorded())
        self.assertIn("not recorded", self.out)
        h.ntp = "yes"
        h.now += 3600
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(self.recorded(), int(h.now))

    def test_clock_behind_newest_snapshot(self) -> None:
        # Unsynchronized: which one is wrong is unknown - neither create nor delete.
        h, old = self.history("2026-10-13 11:50")  # newest is 12:00: 10 minutes ahead
        h.ntp = "no"
        h.subvols["/top/home"]["gen"] = 10_000
        before = h.snapshots()
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h"), 4)
        self.assertEqual(h.snapshots(), before)
        self.assertIn("is behind the newest snapshot home.20261013T1200 and not NTP-synchronized", self.out)
        self.assertIn("btrfs subvolume delete /top/snapshots/home.20261013T1200", self.out)
        # Within the 5-minute slack it proceeds (deletions still wait for NTP).
        h.now = at("2026-10-13 11:56", BERLIN)
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h"), 4)
        self.assertIn("home.20261013T1156", h.snapshots())

    def test_synchronized_clock_supersedes_a_future_name(self) -> None:
        # NTP-synchronized: the future name is the wrong one. A correctly named
        # snapshot becomes the newest by generation and deletions go ahead.
        h, old = self.history("2026-10-13 11:50")
        h.subvols["/top/home"]["gen"] = 10_000
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h"), 0, self.out)
        self.assertIn("home.20261013T1150", h.snapshots())
        self.assertIn(
            "<4>btrfs-snapshot: home: the newest snapshot home.20261013T1200 is dated in the future", self.out
        )
        self.assertIn("btrfs subvolume delete /top/snapshots/home.20261013T1200", self.out)
        self.assertFalse(set(old) & set(h.snapshots()))
        self.assertIn("home.20261013T1200", h.snapshots())  # kept until its date passes

    def test_future_name_on_idle_source(self) -> None:
        # A run while the clock was two years ahead (unsynchronized, long up, so
        # trusted) left a future name; the source stays idle afterwards.
        h, old = self.history("2026-10-13 14:07")
        h.add_snapshot("home.20281013T1407", "/top/home", 900)
        h.subvols["/top/home"]["gen"] = 800
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h"), 0, self.out)
        self.assertIn(
            "created home.20261013T1407 (newest snapshot home.20281013T1407 is dated in the future)", self.out
        )
        self.assertFalse(set(old) & set(h.snapshots()))
        h.now += 3600
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h"), 0)
        self.assertIn("unchanged since home.20261013T1407", self.out)
        self.cli(h, "list", source="home=/top/home:1h")
        # btrbk's wording: a future date matches "<N>h" with a negative distance.
        self.assertIn(
            "KEEP    home.20281013T1407               preserve hourly: first of hour, -17543 hours ago", self.out
        )
        # Unsynchronized, nothing is created for that reason.
        h2, _ = self.history("2026-10-13 14:07")
        h2.add_snapshot("home.20281013T1407", "/top/home", 900)
        h2.subvols["/top/home"]["gen"] = 800
        h2.ntp = "no"
        self.assertEqual(self.cli(h2, "snapshot", source="home=/top/home:1h"), 4)
        self.assertNotIn("home.20261013T1407", h2.snapshots())

    def test_unsynchronized_far_ahead_right_after_boot_waits(self) -> None:
        # The Persistent timer's catch-up run starts before chrony has corrected
        # an RTC that is two years ahead: no snapshot with that name.
        h = self.host("2026-10-13 14:07", gen=10)
        self.add_history(h, "2026-10-10 00:05", "2026-10-13 13:05")
        h.subvols["/top/home"]["gen"] = 10_000
        self.assertEqual(self.cli(h, "snapshot", "--allow-mass-delete"), 0)
        recorded = self.recorded()
        h.now = at("2028-10-13 14:07", BERLIN)
        h.reboot(uptime=40)
        h.ntp = "no"
        h.subvols["/top/home"]["gen"] = 20_000
        self.assertEqual(self.cli(h, "snapshot"), 4)
        self.assertEqual([s for s in h.snapshots() if s.startswith("home.2028")], [])
        self.assertIn("not NTP-synchronized yet, the system booted 0 min ago", self.out)
        self.assertEqual(self.recorded(), recorded)
        # chrony steps the clock back; the next run catches up normally.
        h.now = at("2026-10-13 15:10", BERLIN)
        h.reboot(uptime=600)
        h.ntp = "yes"
        self.assertEqual(self.cli(h, "snapshot"), 0, self.out)
        self.assertIn("home.20261013T1510", h.snapshots())
        self.assertEqual(self.recorded(), int(h.now))

    def test_unsynchronized_after_the_grace_period_creates(self) -> None:
        # Offline machine, up for hours: the clock is all there is.
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20261010T1000", "/top/home", 11)
        h.subvols["/top/home"]["gen"] = 10_000
        h.reboot(uptime=core.NTP_GRACE + 60)
        h.ntp = "no"
        self.assertEqual(self.cli(h, "snapshot"), 0, self.out)
        self.assertIn("home.20261013T1407", h.snapshots())
        self.assertIn("unverified time", self.out)

    def test_unsynchronized_clock_jump_in_the_same_boot(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        h.now += 3600  # one real hour
        h.step_clock(3 * 86400)  # and the clock jumps three days
        h.ntp = "no"
        h.subvols["/top/home"]["gen"] = 20_000
        self.assertEqual(self.cli(h, "snapshot"), 4)
        self.assertEqual(h.snapshots(), ["home.20261013T1407"])
        self.assertIn("moved 3 days further than the time elapsed since the last successful run", self.out)

    def test_clock_behind_last_run(self) -> None:
        h, old = self.history("2026-10-13 14:07")
        os.makedirs(self.state)
        with open(os.path.join(self.state, core.STATE_FILE), "w") as f:
            f.write(f"{int(h.now) + 3600}\n")  # the old one-number format is read too
        before = h.snapshots()
        h.subvols["/top/home"]["gen"] = 10_000
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h", clock_ok=True), 4)
        self.assertEqual(h.snapshots(), before)
        self.assertIn("behind the last successful run", self.out)
        h.now += 3600
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertEqual(self.recorded(), int(h.now))

    def test_state_never_moves_backwards(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        recorded = self.recorded()
        self.assertEqual(recorded, int(h.now))
        # The clock steps back two days. The idle source's newest snapshot now
        # looks future-dated, but the record says the clock is the wrong one.
        h.step_clock(-2 * 86400)
        self.assertEqual(self.cli(h, "snapshot"), 4)
        self.assertIn("behind the last successful run", self.out)
        self.assertEqual(h.snapshots(), ["home.20261013T1407"])
        self.assertEqual(self.recorded(), recorded)
        # A run with nothing to create or delete succeeds without lowering the record.
        for _ in range(2):
            h.now += 3600
            self.assertEqual(self.cli(h, "prune"), 0)
            self.assertIn("keeping that record", self.out)
            self.assertEqual(self.recorded(), recorded)
        # A source with no snapshot yet would get a wrong past name: refused.
        h.add_source("/top/ws", 10)
        rc = self.cli(h, "snapshot", sources=["home=/top/home:72h", "ws=/top/ws:48h"])
        self.assertEqual(rc, 4)
        self.assertNotIn("ws.20261011T1507", h.snapshots())
        self.assertIn("ws: not creating a snapshot: the clock", self.out)
        # A small step back within the slack is not worth a warning either.
        assert recorded is not None
        h.now = recorded - 60
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertNotIn("keeping that record", self.out)
        self.assertEqual(self.recorded(), recorded)

    def test_corrupt_state_blocks(self) -> None:
        h, old = self.history("2026-10-13 14:07")
        os.makedirs(self.state)
        for garbage in ("garbage", "123 not-a-uuid 5.0", f"123 {uuid_n(1)}"):
            with self.subTest(garbage=garbage):
                with open(os.path.join(self.state, core.STATE_FILE), "w") as f:
                    f.write(garbage)
                self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 4)
                self.assertTrue(set(old) <= set(h.snapshots()))
                self.assertIn("unreadable state", self.out)

    def test_state_records_the_boot(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        with open(os.path.join(self.state, core.STATE_FILE)) as f:
            epoch, boot_id, boottime = f.read().split()
        self.assertEqual((int(epoch), boot_id, float(boottime)), (int(h.now), h.boot_id, 10 * 86400.0))


class CircuitBreakerTests(CliTestCase):
    def build(self, n_old: int) -> tuple[FakeHost, list[str]]:
        h = self.host("2026-10-13 14:07", gen=10)
        old = self.add_history(h, "2026-06-01 00:00", f"2026-06-01 {n_old - 1:02d}:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        return h, old

    def steady_state(
        self, policy: str, end: str = "2026-10-13 14:05", days: int = 120, name: str = "home", source: str = "/top/home"
    ) -> FakeHost:
        """A host whose snapshots are what hourly runs with POLICY left behind,
        the last one at END, and a successful run recorded right after it."""
        self.state = tempfile.mkdtemp(dir=self.tmp.name)  # fresh per call (subtests)
        h = self.host(end, gen=10)
        if source != "/top/home":
            h.add_source(source, 10)
        end_wall = W(end)
        walls = [end_wall - dt.timedelta(hours=k) for k in range(days * 24, -1, -1)]
        snaps = [core.Snap(entry=core.snapshot_name(name, w), wall=w) for w in walls]
        keep = [
            r.snap for r in core.plan(snaps, at(end, BERLIN), core.parse_policy(policy), BERLIN) if r.action == "keep"
        ]
        for cgen, s in enumerate(keep, start=10):
            h.add_snapshot(s.entry, source, cgen)
        h.subvols[source]["gen"] = 9  # idle (kernel >= 7.1)
        h.now += 60
        self.assertEqual(self.cli(h, "prune", source=f"{name}={source}:{policy}"), 0, self.out)
        self.assertIn("deleted 0", self.out)
        return h

    def test_limits(self) -> None:
        self.assertFalse(core.is_mass_delete(5, 10))
        self.assertTrue(core.is_mass_delete(6, 10))
        self.assertFalse(core.is_mass_delete(10, 100))
        self.assertTrue(core.is_mass_delete(11, 100))
        self.assertFalse(core.is_mass_delete(21, 210))
        self.assertTrue(core.is_mass_delete(22, 210))

    def test_five_pass(self) -> None:
        h, old = self.build(5)
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertFalse(set(old) & set(h.snapshots()))

    def test_six_trip_then_allowed(self) -> None:
        h, old = self.build(6)
        before = h.snapshots()
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h", clock_ok=True), 4)
        self.assertEqual(h.snapshots(), before)
        self.assertIn("circuit breaker: 6 of 9", self.out)
        self.assertIn("no successful run is recorded", self.out)
        self.assertEqual(self.cli(h, "prune", "--allow-mass-delete", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertEqual(h.snapshots(), ["home.20261013T1000", "home.20261013T1100", "home.20261013T1200"])

    def test_message_names_the_callers_commands(self) -> None:
        h, old = self.build(6)
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 4)
        self.assertIn("btrfs-snapshot-core prune --snapdir DIR", self.out)
        self.assertEqual(
            self.cli(
                h,
                "prune",
                "--list-hint",
                "btrfs-setup.sh snapshots list",
                "--confirm-hint",
                "btrfs-setup.sh snapshots prune --confirm",
                source="home=/top/home:1h",
                clock_ok=True,
            ),
            4,
        )
        self.assertIn(
            "check with 'btrfs-setup.sh snapshots list', then delete them with "
            "'btrfs-setup.sh snapshots prune --confirm'",
            self.out,
        )

    def test_per_source(self) -> None:
        h, old = self.build(6)
        h.add_source("/top/ws", 10)
        rc = self.cli(h, "prune", sources=["home=/top/home:1h", "ws=/top/ws:1h"], clock_ok=True)
        self.assertEqual(rc, 4)
        self.assertIn("ws: deleted 0", self.out)

    def test_downtime_does_not_trip(self) -> None:
        # Off overnight, over a weekend, on vacation: whatever aged out meanwhile
        # is deleted at the first run back, although it is far over the limit.
        cases = (
            ("48h 7d", "Workspace", "/top/ws", 6),
            ("72h 7d 4w 3m", "home", "/top/home", 9),
            ("72h 7d 4w 3m", "home", "/top/home", 12),
            ("72h 7d 4w 3m", "home", "/top/home", 62),
            ("48h 7d", "Workspace", "/top/ws", 9 * 24),
        )
        for (policy, name, src, hours), reboot in [(c, r) for c in cases for r in (True, False)]:
            # reboot False: suspended (CLOCK_BOOTTIME counts the suspended time too).
            with self.subTest(policy=policy, hours=hours, reboot=reboot):
                h = self.steady_state(policy, name=name, source=src)
                owned = len(h.snapshots())
                h.now += hours * 3600
                if reboot:
                    h.reboot(uptime=3600)
                h.subvols[src]["gen"] = 50_000
                self.assertEqual(self.cli(h, "snapshot", source=f"{name}={src}:{policy}"), 0, self.out)
                deleted = owned + 1 - len(h.snapshots())
                self.assertTrue(core.is_mass_delete(deleted, owned + 1), (deleted, owned))
                self.assertIn(f"deleted {deleted},", self.out)

    def test_policy_shrink_trips(self) -> None:
        h = self.steady_state("72h 7d 4w 3m")
        before = h.snapshots()
        for _ in range(2):  # the trip does not record a successful run: it persists
            h.now += 3600
            self.assertEqual(self.cli(h, "prune", source="home=/top/home:24h 7d 4w 3m"), 4)
            self.assertEqual(h.snapshots(), before)
            self.assertIn("was the policy shortened?", self.out)
        self.assertEqual(self.cli(h, "prune", "--allow-mass-delete", source="home=/top/home:24h 7d 4w 3m"), 0)
        self.assertLess(len(h.snapshots()), len(before) - 40)

    def test_clock_jump_in_the_same_boot_trips(self) -> None:
        h = self.steady_state("72h 7d 4w 3m")
        before = h.snapshots()
        h.now += 1800
        h.step_clock(10 * 86400)  # NTP-synchronized, yet ten days ahead
        h.subvols["/top/home"]["gen"] = 50_000
        self.assertEqual(self.cli(h, "snapshot"), 4)
        self.assertIn("the clock moved 10 days further than the time elapsed since then", self.out)
        self.assertTrue(set(before) <= set(h.snapshots()))
        # The same ten days really passing (or across a reboot, where the jump is
        # indistinguishable from downtime) is normal aging.
        h2 = self.steady_state("72h 7d 4w 3m")
        h2.now += 10 * 86400 + 1800
        h2.subvols["/top/home"]["gen"] = 50_000
        self.assertEqual(self.cli(h2, "snapshot"), 0, self.out)

    def test_safeguarded_snapshots_age_out_quietly(self) -> None:
        # Eight forced snapshots in one hour: the policy wants only the first,
        # the 24 h safeguard holds the rest; a day later they go together.
        h = self.steady_state("72h 7d 4w 3m")
        for _ in range(8):
            self.assertEqual(self.cli(h, "snapshot", "--force"), 0)
            h.now += 60
        burst = [e for e in h.snapshots() if e.startswith("home.20261013T14") and e != "home.20261013T1405"]
        self.assertEqual(len(burst), 8)
        for _ in range(25):
            h.now += 3600
            h.subvols["/top/home"]["gen"] = h.transid + 1
            self.assertEqual(self.cli(h, "snapshot"), 0, self.out)
        self.assertFalse(set(burst[1:]) & set(h.snapshots()))

    def test_simulated_schedules_never_trip(self) -> None:
        # The reviewer's model: one run per hour while the machine is on.
        schedules = {
            "weekdays only": lambda w: w.weekday() < 5,
            "off 23-08": lambda w: 8 <= w.hour < 23,
            "office hours, weekdays": lambda w: w.weekday() < 5 and 9 <= w.hour < 18,
        }
        tz = zoneinfo.ZoneInfo("America/Sao_Paulo")
        for policy_text in ("72h 7d 4w 3m", "48h 7d", "24h 3d"):
            policy = core.parse_policy(policy_text)
            for label, on in schedules.items():
                with self.subTest(policy=policy_text, schedule=label):
                    t0 = core.wall_to_epoch(W("2026-09-01 00:05"), tz)
                    alive, since, deleted_total = [], None, 0
                    for k in range(24 * 40):
                        t = t0 + k * 3600
                        wall = core.epoch_to_wall(t, tz)
                        if not on(wall):
                            continue
                        alive.append(core.Snap(entry=core.snapshot_name("home", wall), wall=wall, cgen=k))
                        decisions = core.apply_safeguards(core.plan(alive, t, policy, tz), t, tz, set())
                        unexplained = core.unexplained_deletions(decisions, alive, policy, tz, set(), since)
                        self.assertEqual(unexplained, [], (label, core.fmt_epoch(t, tz)))
                        doomed = {id(d.snap) for d in decisions if d.action == "delete"}
                        deleted_total += len(doomed)
                        alive = [s for s in alive if id(s) not in doomed]
                        since = t
                    self.assertGreater(deleted_total, 100)


class DryRunAndLockTests(CliTestCase):
    def test_dry_run_changes_nothing(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        old = self.add_history(h, "2026-06-01 00:00", "2026-06-01 02:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        h.subvols["/top/home"]["gen"] = 10_000
        before = h.snapshots()
        self.assertEqual(self.cli(h, "snapshot", "--dry-run", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertEqual(h.snapshots(), before)
        self.assertEqual(h.mutating_calls(), [])
        self.assertIn("would create /top/snapshots/home.20261013T1407", self.out)
        for e in old:
            self.assertIn(f"would delete /top/snapshots/{e}", self.out)
        self.assertIn("dry run snapshot summary", self.out)
        self.assertFalse(os.path.exists(self.lock))
        self.assertFalse(os.path.exists(os.path.join(self.state, core.STATE_FILE)))

    def test_dry_run_counts_the_new_snapshot(self) -> None:
        # The would-be snapshot is the newest: with it, 12:00 is no longer protected
        # as one of the 3 newest... but still younger than 24 h; 2026-06-01 goes.
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20260601T0000", "/top/home", 11)
        h.add_snapshot("home.20261013T1100", "/top/home", 12)
        h.add_snapshot("home.20261013T1200", "/top/home", 13)
        h.subvols["/top/home"]["gen"] = 10_000
        self.cli(h, "snapshot", "--dry-run", source="home=/top/home:1h", clock_ok=True)
        self.assertIn("would delete /top/snapshots/home.20260601T0000", self.out)
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertNotIn("home.20260601T0000", h.snapshots())

    def test_lock_busy(self) -> None:
        h = self.host("2026-10-13 14:07")
        fd = os.open(self.lock, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.cli(h, "snapshot"), 3)
            self.assertEqual(h.snapshots(), [])
            self.assertEqual(self.cli(h, "prune"), 3)
            self.assertEqual(self.cli(h, "snapshot", "--no-lock"), 0)  # the caller holds it
            self.assertEqual(self.cli(h, "list"), 0)  # list takes no lock
        finally:
            os.close(fd)
        self.assertEqual(self.cli(h, "snapshot", "--force"), 0)


class RecheckTests(CliTestCase):
    def build(self) -> tuple[FakeHost, list[str]]:
        h = self.host("2026-10-13 14:07", gen=10)
        old = self.add_history(h, "2026-06-01 00:00", "2026-06-01 02:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        return h, old

    def test_replaced_before_relist(self) -> None:
        h, old = self.build()
        target = f"/top/snapshots/{old[0]}"

        def swap(host: FakeHost, n: int) -> None:
            if n == 2:  # the re-list right before deleting
                host.subvols[target]["uuid"] = uuid_n(999)

        h.on_listdir = swap
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertIn(old[0], h.snapshots())
        self.assertNotIn(old[1], h.snapshots())
        self.assertIn("no longer due", self.out)

    def test_replaced_between_deletions(self) -> None:
        h, old = self.build()

        def swap(host: FakeHost, path: str) -> None:
            if path.endswith(old[0]):
                host.subvols[f"/top/snapshots/{old[1]}"]["ro"] = False

        h.on_delete = swap
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 4)
        self.assertNotIn(old[0], h.snapshots())
        self.assertIn(old[1], h.snapshots())
        self.assertNotIn(old[2], h.snapshots())
        self.assertIn("no longer read-only", self.out)

    def test_pinned_between_plan_and_delete(self) -> None:
        h, old = self.build()

        def pin(host: FakeHost, path: str) -> None:
            if path.endswith(old[0]):
                with open(self.pins, "w") as f:
                    f.write(f"/top/snapshots/{old[2]}\n")

        h.on_delete = pin
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertIn(old[2], h.snapshots())
        self.assertNotIn(old[1], h.snapshots())
        self.assertIn("pinned meanwhile", self.out)

    def test_delete_failure_stops(self) -> None:
        h, old = self.build()

        def vanish(host: FakeHost, path: str) -> None:
            host.subvols.pop(path, None)  # the delete command then fails

        h.on_delete = vanish
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 1)
        self.assertEqual(len([c for c in h.calls if c[:3] == ["btrfs", "subvolume", "delete"]]), 1)


class LatestTests(CliTestCase):
    def test_latest_by_generation(self) -> None:
        h = self.host("2026-10-13 14:07")
        other = h.add_source("/top/other", 1)
        h.add_snapshot("home.20261013T1300", "/top/home", 200)
        h.add_snapshot("home.20261013T1200", "/top/home", 300)
        h.add_snapshot("home.20261013T1400", "/top/home", 100)
        h.add_snapshot("home.20261013T1405", "/top/home", 999, ro=False)
        h.add_snapshot("home.20261013T1406", "/top/home", 998, parent=other)
        rc = self.cli(h, "latest", "--name", "home", sources=[])
        self.assertEqual((rc, self.out.strip()), (0, "/top/snapshots/home.20261013T1406"))
        rc = self.cli(h, "latest", "--name", "home", "--source-path", "/top/home", sources=[])
        self.assertEqual((rc, self.out.strip()), (0, "/top/snapshots/home.20261013T1200"))
        self.assertEqual(self.cli(h, "latest", "--name", "Workspace", sources=[]), 1)
        self.assertEqual(self.out, "")


# ---- cache folders (CACHEDIR.TAG) ----------------------------------------------------------
TAG_TEXT = core.CACHE_SIGNATURE.decode() + "\n# This file is a cache directory tag.\n"


def put(root: str, rel: str, text: str = "x") -> str:
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


def tag(root: str, rel: str, text: str = TAG_TEXT) -> str:
    return put(root, os.path.join(rel, core.CACHE_TAG), text)


class CacheWalkTests(unittest.TestCase):
    """walk_caches on real folders in a temporary directory."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "root")
        os.makedirs(self.root)

    def tearDown(self) -> None:
        for dirpath, dirnames, _ in os.walk(self.tmp.name):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o755)
        self.tmp.cleanup()

    def rels(self, walk: _CacheWalkProto) -> list[str]:
        return [c.rel for c in walk.folders]

    def test_tagged_folders_only_the_top_one_and_never_the_root(self) -> None:
        tag(self.root, ".")  # the root itself: never a candidate
        tag(self.root, "proj/target")
        tag(self.root, "proj/target/debug/build/inner")  # below a cache folder: not looked at
        put(self.root, "proj/src/main.rs")
        tag(self.root, "a/b/c/deep")
        walk = core.walk_caches(self.root)
        self.assertEqual(sorted(self.rels(walk)), ["a/b/c/deep", "proj/target"])
        self.assertEqual(walk.problems, [])

    def test_signature_rules(self) -> None:
        tag(self.root, "good")
        tag(self.root, "longer", core.CACHE_SIGNATURE.decode() + " and more text")
        tag(self.root, "wrong", "Signature: 0000000000000000000000000000000\n")
        tag(self.root, "short", core.CACHE_SIGNATURE.decode()[:-1])
        tag(self.root, "empty", "")
        tag(self.root, "lower", core.CACHE_SIGNATURE.decode().lower())
        os.makedirs(os.path.join(self.root, "tagdir", core.CACHE_TAG))  # a folder named like the tag
        os.makedirs(os.path.join(self.root, "symtag"))
        os.symlink(
            os.path.join(self.root, "good", core.CACHE_TAG),  # restic follows it; we do not
            os.path.join(self.root, "symtag", core.CACHE_TAG),
        )
        os.makedirs(os.path.join(self.root, "fifo"))
        os.mkfifo(os.path.join(self.root, "fifo", core.CACHE_TAG))  # must not block the walk
        self.assertEqual(sorted(self.rels(core.walk_caches(self.root))), ["good", "longer"])

    def test_symlinks_are_not_followed(self) -> None:
        outside = os.path.join(self.tmp.name, "outside")
        tag(outside, "cache")
        tag(self.root, "real")
        os.symlink(outside, os.path.join(self.root, "to-outside"))
        os.symlink(os.path.join(self.root, "real"), os.path.join(self.root, "to-real"))
        self.assertEqual(self.rels(core.walk_caches(self.root)), ["real"])

    def test_git_and_node_modules_are_not_entered(self) -> None:
        tag(self.root, "p/.git/lfs/cache")
        tag(self.root, "p/node_modules/.cache")
        tag(self.root, "p/git/cache")  # only the exact names are pruned
        tag(self.root, "p/node_modules2/.cache")
        self.assertEqual(sorted(self.rels(core.walk_caches(self.root))), ["p/git/cache", "p/node_modules2/.cache"])

    def test_skip_list(self) -> None:
        tag(self.root, "u/.local/share/containers/storage/overlay/l1/diff/var/cache/dnf")
        tag(self.root, "u/.local")  # holds a skipped path: kept and walked
        tag(self.root, "u/.local/state/tool-cache")  # ... so this one is still found
        tag(self.root, "u/other")
        walk = core.walk_caches(self.root, ["u/.local/share/containers", "u/not-there"])
        self.assertEqual(sorted(self.rels(walk)), ["u/.local/state/tool-cache", "u/other"])
        self.assertEqual(walk.kept, ["u/.local"])
        self.assertEqual(walk.skipped, ["u/.local/share/containers"])
        whole = core.walk_caches(self.root, ["."])
        self.assertEqual((whole.folders, whole.skipped), ([], ["."]))

    def test_count_mode_counts_without_deleting(self) -> None:
        tag(self.root, "t")
        put(self.root, "t/a", "12345")
        put(self.root, "t/sub/b", "123")
        os.symlink("a", os.path.join(self.root, "t", "link"))
        walk = core.walk_caches(self.root, mode="count")
        (c,) = walk.folders
        # Files: the tag, a, b and the symlink; bytes: regular files only.
        self.assertEqual((c.rel, c.files, c.size), ("t", 4, len(TAG_TEXT) + 8))
        self.assertTrue(os.path.exists(os.path.join(self.root, "t", "sub", "b")))

    def test_drop_deletes_only_cache_folders_and_follows_no_link(self) -> None:
        outside = put(self.tmp.name, "outside/keep.txt", "precious")
        outside_dir = os.path.dirname(outside)
        tag(self.root, "proj/target")
        put(self.root, "proj/target/debug/app", "binary")
        os.symlink(outside, os.path.join(self.root, "proj", "target", "to-file"))
        os.symlink(outside_dir, os.path.join(self.root, "proj", "target", "to-dir"))
        os.symlink("target", os.path.join(self.root, "proj", "link-to-target"))
        put(self.root, "proj/src/main.rs", "fn main() {}")
        tag(self.root, "proj/wrong", "not a tag")
        walk = core.walk_caches(self.root, mode="drop")
        self.assertEqual(self.rels(walk), ["proj/target"])
        self.assertEqual(walk.problems, [])
        self.assertEqual(walk.folders[0].files, 4)
        self.assertFalse(os.path.lexists(os.path.join(self.root, "proj", "target")))
        self.assertTrue(os.path.islink(os.path.join(self.root, "proj", "link-to-target")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "proj", "src", "main.rs")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "proj", "wrong", core.CACHE_TAG)))
        with open(outside) as f:
            self.assertEqual(f.read(), "precious")
        # A second drop finds nothing more.
        self.assertEqual(core.walk_caches(self.root, mode="drop").folders, [])

    @unittest.skipIf(os.geteuid() == 0, "root ignores the permission that makes the delete fail")
    def test_drop_failure_keeps_the_tag_and_goes_on(self) -> None:
        tag(self.root, "a/stuck")
        put(self.root, "a/stuck/locked/file")
        os.chmod(os.path.join(self.root, "a", "stuck", "locked"), 0o555)  # its file cannot be deleted
        tag(self.root, "b/fine")
        walk = core.walk_caches(self.root, mode="drop")
        self.assertEqual(sorted(self.rels(walk)), ["a/stuck", "b/fine"])
        self.assertTrue([p for p in walk.problems if p[0] == "a/stuck/locked/file"], walk.problems)
        # The tag is deleted last: the half-deleted folder is still a cache for the next attempt.
        self.assertTrue(os.path.exists(os.path.join(self.root, "a", "stuck", core.CACHE_TAG)))
        self.assertFalse(os.path.lexists(os.path.join(self.root, "b", "fine")))
        os.chmod(os.path.join(self.root, "a", "stuck", "locked"), 0o755)
        self.assertEqual(self.rels(core.walk_caches(self.root, mode="drop")), ["a/stuck"])
        self.assertFalse(os.path.lexists(os.path.join(self.root, "a", "stuck")))

    def test_deep_trees(self) -> None:
        deep = os.path.join(*["d"] * 300)
        tag(self.root, "cache")
        put(self.root, os.path.join("cache", deep, "f"))
        tag(self.root, os.path.join("plain", deep))
        walk = core.walk_caches(self.root, mode="drop")
        self.assertEqual(sorted(self.rels(walk)), ["cache", os.path.join("plain", deep)])
        self.assertEqual(walk.problems, [])
        self.assertFalse(os.path.lexists(os.path.join(self.root, "cache")))

    def test_reading_writes_no_access_time(self) -> None:
        """With relatime a read after a change (or after a day) writes the access
        time: one metadata write per folder of the snapshot, and a change of
        the source in a dry run. O_NOATIME avoids it."""
        tag(self.root, "a/t")
        put(self.root, "a/t/f")
        put(self.root, "b/c/x")
        paths = [self.root] + [os.path.join(self.root, r) for r in ("a", "a/t", "a/t/CACHEDIR.TAG", "b", "b/c")]
        old = time.time_ns() - 2 * 86400 * 10**9
        for p in paths:
            os.utime(p, ns=(old, os.stat(p).st_mtime_ns))
        self.assertEqual(self.rels(core.walk_caches(self.root, mode="count")), ["a/t"])
        self.assertEqual([os.stat(p).st_atime_ns for p in paths], [old] * len(paths))

    def test_other_devices_are_never_entered(self) -> None:
        """A nested subvolume or a mount (another st_dev, faked here) is never
        searched, never counted as a cache even with a tag, and never emptied."""
        tag(self.root, "nested")  # like ~/Bulk: a tagged nested subvolume
        tag(self.root, "nested/inner")
        tag(self.root, "t")
        put(self.root, "t/mnt/f")  # something mounted inside a cache folder
        real = os.fstat

        def fstat(fd: int) -> os.stat_result:
            st = real(fd)
            if os.readlink(f"/proc/self/fd/{fd}").endswith(("/nested", "/t/mnt")):
                return os.stat_result((st[0], st[1], st[2] + 1) + tuple(st)[3:])
            return st

        with mock.patch.object(core.os, "fstat", fstat):
            listed = core.walk_caches(self.root, mode="count")
            dropped = core.walk_caches(self.root, mode="drop")
        self.assertEqual(self.rels(listed), ["t"])
        self.assertEqual(listed.folders[0].files, 1)  # only the tag: mnt/f is on the other device
        self.assertEqual(self.rels(dropped), ["t"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "nested", "inner", core.CACHE_TAG)))
        self.assertTrue(os.path.exists(os.path.join(self.root, "t", "mnt", "f")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "t", core.CACHE_TAG)))
        self.assertEqual([p[0] for p in dropped.problems], ["t/mnt"])
        self.assertIn("on another device, not entered", dropped.problems[0][1])


class ExcludeCachesTests(CliTestCase):
    """snapshot/prune/finish/list/caches with --exclude-caches and the fake btrfs."""

    def state_list(self, name: str) -> list[str] | None:
        try:
            with open(os.path.join(self.state, name), encoding="utf-8") as f:
                return f.read().split()
        except FileNotFoundError:
            return None

    def unfinished(self) -> list[str] | None:
        return self.state_list(core.UNFINISHED_FILE)

    def writable(self) -> list[str] | None:
        return self.state_list(core.WRITABLE_FILE)

    def listed(self, *entries: str) -> None:
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, core.UNFINISHED_FILE), "w") as f:
            f.write("".join(f"{e}\n" for e in entries))

    def owned(self, host: FakeHost) -> list[str]:
        self.assertEqual(self.cli(host, "list", "--json"), 0)
        return [s["entry"] for s in json.loads(self.out)["sources"][0]["snapshots"]]

    def test_order_record_snapshot_writable_drop_read_only_check_clear(self) -> None:
        h = self.host("2026-10-13 14:07")
        seen = []
        h.on_snapshot = lambda host, dest: seen.append(self.unfinished())
        self.assertEqual(self.cli(h, "snapshot", "--exclude-caches"), 0, self.out)
        dest = "/top/snapshots/home.20261013T1407"
        self.assertEqual(seen, [["home.20261013T1407"]])  # recorded before it exists
        steps = [
            c
            for c in h.calls
            if c[0] == "walk"
            or c[:3] in (["btrfs", "subvolume", "snapshot"], ["btrfs", "property", "set"], ["btrfs", "property", "get"])
        ]
        self.assertEqual(
            steps[:4],
            [
                ["btrfs", "subvolume", "snapshot", "/top/home", dest],
                ["btrfs", "property", "get", "-ts", dest, "ro"],
                ["walk", dest, [], "drop"],
                ["btrfs", "property", "set", "-ts", dest, "ro", "true"],
            ],
        )
        self.assertEqual(steps[4], ["btrfs", "property", "get", "-ts", dest, "ro"])
        self.assertTrue(h.subvols[dest]["ro"])
        self.assertIsNone(self.unfinished())
        self.assertIsNone(self.writable())
        self.assertEqual(self.owned(h), ["home.20261013T1407"])

    def test_the_drop_only_ever_touches_the_new_snapshot(self) -> None:
        h = self.host("2026-10-13 14:07")
        h.add_source("/top/home/u/Workspace", 100)
        srcs = ["home=/top/home:72h", "Workspace=/top/home/u/Workspace:48h"]
        self.assertEqual(self.cli(h, "snapshot", "--exclude-caches", sources=srcs), 0, self.out)
        drops = [c[1] for c in h.calls if c[0] == "walk" and c[3] == "drop"]
        self.assertEqual(sorted(drops), ["/top/snapshots/Workspace.20261013T1407", "/top/snapshots/home.20261013T1407"])

    def test_logs_folders_files_bytes_and_seconds(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.cli(h, "snapshot", "--exclude-caches")
        self.assertIn("<6>btrfs-snapshot: home: dropped u/proj/target (3 files, 1.0 KiB)", self.out)
        self.assertIn(
            "<5>btrfs-snapshot: home: dropped 1 cache folder(s) from home.20261013T1407: 3 files, "
            "1.0 KiB (apparent size), 0.5 s",
            self.out,
        )

    def test_skips_reach_the_walk(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.cli(
            h,
            "snapshot",
            "--exclude-caches",
            "--caches-skip",
            "home=u/.local/share/containers/",
            "--caches-skip",
            "home=./u/x",
        )
        (walk,) = [c for c in h.calls if c[0] == "walk"]
        self.assertEqual(walk[2], ["u/.local/share/containers", "u/x"])

    def test_bad_skips_are_usage_errors(self) -> None:
        h = self.host("2026-10-13 14:07")
        for bad in ("Workspace=x", "home", "home=", "home=/abs", "home=../up", "home=a/../../up"):
            self.assertEqual(self.cli(h, "snapshot", "--exclude-caches", "--caches-skip", bad), 2, bad)
            self.assertIn(f"--caches-skip {bad!r}", self.err)
        self.assertEqual(h.mutating_calls(), [])
        self.assertEqual(self.cli(h, "snapshot", "--exclude-caches", "--caches-skip", "home=u/ok"), 0, self.out)

    def test_a_failed_drop_still_ends_read_only(self) -> None:
        for error in (OSError(5, "Input/output error"), RuntimeError("a bug")):
            h = self.host("2026-10-13 14:07")
            h.walk_error = error
            self.assertEqual(self.cli(h, "snapshot", "--exclude-caches"), 0, self.out)
            self.assertIn(
                "<3>btrfs-snapshot: home: dropping the cache folders from home.20261013T1407 failed", self.out
            )
            self.assertTrue(h.subvols["/top/snapshots/home.20261013T1407"]["ro"])
            self.assertIsNone(self.unfinished())
            self.assertEqual(self.owned(h), ["home.20261013T1407"])

    def test_problems_in_the_drop_are_errors_in_the_log_not_in_the_result(self) -> None:
        h = self.host("2026-10-13 14:07")
        h.walk_result = lambda root, skips, mode: core.CacheWalk(
            [core.CacheDir("u/t", 2, 10)], [], [], [("u/t/locked/f", "cannot delete: Permission denied")], 0.1
        )
        self.assertEqual(self.cli(h, "snapshot", "--exclude-caches"), 0, self.out)
        self.assertIn("<3>btrfs-snapshot: home: home.20261013T1407: u/t/locked/f: cannot delete", self.out)
        self.assertIn("1 problem(s): what could not be deleted stays in the snapshot", self.out)
        self.assertIn("result: ok", self.out)
        self.assertTrue(h.subvols["/top/snapshots/home.20261013T1407"]["ro"])

    def test_read_only_failure_stays_listed_and_the_next_run_finishes_it(self) -> None:
        h = self.host("2026-10-13 14:07")
        h.set_ro_rc = 1
        self.assertEqual(self.cli(h, "snapshot", "--exclude-caches"), 1)
        self.assertIn("making /top/snapshots/home.20261013T1407 read-only failed", self.out)
        self.assertEqual(self.unfinished(), ["home.20261013T1407"])
        self.assertEqual(self.writable(), ["home.20261013T1407"])
        h.set_ro_rc = 0
        h.now += 3600
        h.calls.clear()
        self.assertEqual(self.cli(h, "snapshot", "--exclude-caches"), 0, self.out)
        self.assertIn(
            "<4>btrfs-snapshot: home: finishing home.20261013T1407, left writable by an interrupted run", self.out
        )
        self.assertTrue(h.subvols["/top/snapshots/home.20261013T1407"]["ro"])
        self.assertIsNone(self.unfinished())
        self.assertIsNone(self.writable())
        # Finished before the scan: it counts for the change detection, so no new snapshot.
        self.assertEqual(h.snapshots(), ["home.20261013T1407"])
        self.assertIn("unchanged since home.20261013T1407", self.out)

    def test_without_the_option_the_old_read_only_snapshot(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(
            h.mutating_calls(),
            [["btrfs", "subvolume", "snapshot", "-r", "/top/home", "/top/snapshots/home.20261013T1407"]],
        )
        self.assertIsNone(self.unfinished())

    def test_no_state_folder_falls_back_to_a_read_only_snapshot(self) -> None:
        h = self.host("2026-10-13 14:07")
        os.makedirs(os.path.join(self.state, core.UNFINISHED_FILE))  # the list cannot be read or written
        self.assertEqual(self.cli(h, "snapshot", "--exclude-caches"), 0, self.out)
        self.assertIn("this snapshot keeps its cache folders", self.out)
        self.assertIn(
            ["btrfs", "subvolume", "snapshot", "-r", "/top/home", "/top/snapshots/home.20261013T1407"], h.calls
        )
        self.assertFalse([c for c in h.calls if c[0] == "walk"])

    def test_failed_creation_is_forgotten(self) -> None:
        h = self.host("2026-10-13 14:07")
        h.dirs.discard("/top/snapshots")
        h.mkdir = lambda path, mode: None  # the folder does not appear
        self.assertEqual(self.cli(h, "snapshot", "--exclude-caches"), 1)
        self.assertIn("creating /top/snapshots/home.20261013T1407 failed", self.out)
        self.assertIsNone(self.unfinished())

    def test_dry_run_shows_the_drop_and_changes_nothing(self) -> None:
        h = self.host("2026-10-13 14:07")
        self.listed("home.20261013T1300")
        h.add_snapshot("home.20261013T1300", "/top/home", 50, ro=False)
        self.assertEqual(self.cli(h, "snapshot", "--dry-run", "--exclude-caches"), 0, self.out)
        self.assertEqual(h.mutating_calls(), [])
        self.assertEqual([c for c in h.calls if c[0] == "walk"], [["walk", "/top/home", [], "count"]])
        self.assertIn("would create /top/snapshots/home.20261013T1407", self.out)
        self.assertIn("would drop u/proj/target (3 files, 1.0 KiB)", self.out)
        self.assertIn("would drop 1 cache folder(s) from the new snapshot: 3 files, 1.0 KiB", self.out)
        self.assertIn("would finish home.20261013T1300", self.out)
        self.assertEqual(self.unfinished(), ["home.20261013T1300"])
        self.assertFalse(h.subvols["/top/snapshots/home.20261013T1300"]["ro"])
        self.assertIsNone(self.writable())

    def test_recovery_finishes_only_what_is_proven_ours(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        other = h.add_source("/top/other", 10)
        ours = h.add_snapshot("home.20261013T1300", "/top/home", 50, ro=False)
        done = h.add_snapshot("home.20261013T1200", "/top/home", 40)  # already read-only
        foreign = h.add_snapshot("home.20261013T1100", "/top/home", 30, ro=False, parent=other)
        h.other["/top/snapshots/home.20261013T1000"] = "other"  # a plain folder
        self.listed(
            "home.20261013T1300",
            "home.20261013T1200",
            "home.20261013T1100",
            "home.20261013T1000",
            "home.20261013T0900",  # never created
            "Workspace.20261013T1300",
            "../home.20261013T1300",
            "home.20261013T1300/x",
        )
        self.assertEqual(self.cli(h, "snapshot", "--exclude-caches"), 0, self.out)
        self.assertTrue(h.subvols[ours]["ro"])
        self.assertIn(["walk", ours, [], "drop"], h.calls)
        self.assertNotIn(["walk", done, [], "drop"], h.calls)
        self.assertFalse(h.subvols[foreign]["ro"])
        touched = [c for c in h.mutating_calls() if foreign in c or "/top/snapshots/home.20261013T1000" in c]
        self.assertEqual(touched, [])
        self.assertIn("home.20261013T1100 is listed in", self.out)
        self.assertIn("a snapshot of another subvolume", self.out)
        self.assertIn("home.20261013T1000 is listed in", self.out)
        self.assertIn("home.20261013T0900 (listed in", self.out)
        self.assertIn("'Workspace.20261013T1300' is listed in", self.out)
        self.assertEqual(
            self.unfinished(),
            [
                "home.20261013T1100",
                "home.20261013T1000",
                "Workspace.20261013T1300",
                "../home.20261013T1300",
                "home.20261013T1300/x",
            ],
        )
        self.assertEqual(self.writable(), ["home.20261013T1100"])
        self.assertEqual(sorted(self.owned(h)), ["home.20261013T1200", "home.20261013T1300"])

    def test_recovery_without_the_option_only_sets_read_only(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        ours = h.add_snapshot("home.20261013T1300", "/top/home", 50, ro=False)
        self.listed("home.20261013T1300")
        self.assertEqual(self.cli(h, "prune"), 0, self.out)
        self.assertTrue(h.subvols[ours]["ro"])
        self.assertFalse([c for c in h.calls if c[0] == "walk"])
        self.assertIsNone(self.unfinished())

    def test_finish_only_finishes(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10_000)
        old = self.add_history(h, "2026-06-01 00:00", "2026-06-01 02:00")  # due for deletion
        ours = h.add_snapshot("home.20261013T1300", "/top/home", 50, ro=False)
        self.listed("home.20261013T1300")
        self.assertEqual(self.cli(h, "finish", "--exclude-caches", "--no-lock", clock_ok=True), 0, self.out)
        self.assertTrue(h.subvols[ours]["ro"])
        self.assertEqual(
            [c for c in h.mutating_calls() if c[:3] != ["btrfs", "property", "set"]], [["walk", ours, [], "drop"]]
        )
        for e in old:
            self.assertIn(e, h.snapshots())
        self.assertIn("finish summary: result: ok", self.out)

    def test_writable_record_follows_what_the_scans_see(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20261013T1300", "/top/home", 50)
        stray = h.add_snapshot("home.20261013T1200", "/top/home", 40, ro=False)
        self.cli(h, "snapshot", "--dry-run")
        self.assertIsNone(self.writable())  # a dry run writes nothing
        self.assertEqual(self.cli(h, "prune"), 0)
        self.assertEqual(self.writable(), ["home.20261013T1200"])
        with open(os.path.join(self.state, core.WRITABLE_FILE)) as f:
            self.assertEqual(os.stat(f.fileno()).st_mode & 0o777, 0o644)  # cst doctor reads it as the user
        del h.subvols[stray]
        self.assertEqual(self.cli(h, "prune"), 0)
        self.assertIsNone(self.writable())

    def test_writable_record_is_world_readable_whatever_the_umask(self) -> None:
        # `sudo cst btrfs snapshots prune` keeps a user's umask 077.
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20261013T1300", "/top/home", 50)
        h.add_snapshot("home.20261013T1200", "/top/home", 40, ro=False)
        old = os.umask(0o077)
        try:
            self.assertEqual(self.cli(h, "prune"), 0)
        finally:
            os.umask(old)
        self.assertEqual(os.stat(os.path.join(self.state, core.WRITABLE_FILE)).st_mode & 0o777, 0o644)

    def test_an_unchanged_writable_record_is_not_rewritten(self) -> None:
        # Every hourly run would otherwise write, fsync and rename the same file.
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20261013T1300", "/top/home", 50)
        h.add_snapshot("home.20261013T1200", "/top/home", 40, ro=False)
        self.assertEqual(self.cli(h, "prune"), 0)
        path = os.path.join(self.state, core.WRITABLE_FILE)
        first = os.stat(path).st_ino
        self.assertEqual(self.cli(h, "prune"), 0)
        self.assertEqual(os.stat(path).st_ino, first)
        self.assertEqual(self.writable(), ["home.20261013T1200"])

    def test_a_list_line_that_is_not_utf8_is_left_alone(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        ours = h.add_snapshot("home.20261013T1300", "/top/home", 50, ro=False)
        os.makedirs(self.state, exist_ok=True)
        with open(os.path.join(self.state, core.UNFINISHED_FILE), "wb") as f:
            f.write(b"home.2026\xff1013T1200\nhome.20261013T1300\n")
        self.assertEqual(self.cli(h, "prune", "--exclude-caches"), 0, self.out)
        self.assertTrue(h.subvols[ours]["ro"])
        self.assertIn("is no snapshot name of home; left alone", self.out)
        self.assertEqual(self.unfinished(), ["home.2026\ufffd1013T1200"])

    def test_finishing_takes_the_entry_off_the_writable_record(self) -> None:
        # purge runs `finish` and writes no new record: a stale line would make
        # cst doctor call the finished snapshot a foreign writable entry.
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20261013T1300", "/top/home", 50)
        ours = h.add_snapshot("home.20261013T1200", "/top/home", 40, ro=False)
        self.listed("home.20261013T1200")
        with open(os.path.join(self.state, core.WRITABLE_FILE), "w") as f:
            f.write("home.20261013T1200\nhome.20261013T1100\n")
        self.assertEqual(self.cli(h, "finish", "--no-lock"), 0, self.out)
        self.assertTrue(h.subvols[ours]["ro"])
        self.assertIsNone(self.unfinished())
        self.assertEqual(self.writable(), ["home.20261013T1100"])

    def test_list_marks_an_unfinished_entry(self) -> None:
        h = self.host("2026-10-13 14:07", gen=10)
        other = h.add_source("/top/other", 10)
        h.add_snapshot("home.20261013T1300", "/top/home", 50, ro=False)
        h.add_snapshot("home.20261013T1200", "/top/home", 40, ro=False, parent=other)
        self.listed("home.20261013T1300", "home.20261013T1200")
        self.assertEqual(self.cli(h, "list", "--json"), 0)
        reasons = {os.path.basename(i["path"]): i["reason"] for i in json.loads(self.out)["sources"][0]["ignored"]}
        self.assertEqual(
            reasons["home.20261013T1300"], "not read-only, left by an interrupted run: the next run finishes it"
        )
        self.assertIn(
            "listed as left by an interrupted run but a snapshot of another subvolume", reasons["home.20261013T1200"]
        )
        self.assertEqual(h.mutating_calls(), [])

    def test_caches_lists_without_changing_anything(self) -> None:
        h = self.host("2026-10-13 14:07")
        h.walk_result = lambda root, skips, mode: core.CacheWalk(
            [core.CacheDir("u/proj/target", 45200, 15 << 30), core.CacheDir("u/.npm/_cacache", 10, 2048)],
            ["u/.local"],
            list(skips),
            [("u/odd", "cannot list: Permission denied")],
            1.25,
        )
        rc = self.cli(h, "caches", "--caches-skip", "home=u/.local/share/containers")
        self.assertEqual(rc, 0, self.err)
        self.assertEqual(h.mutating_calls(), [])
        self.assertEqual(
            [c for c in h.calls if c[0] == "walk"], [["walk", "/top/home", ["u/.local/share/containers"], "count"]]
        )
        self.assertIn("home: /top/home: 2 cache folder(s), 45210 files, 15.0 GiB (apparent size); walk 1.2 s", self.out)
        self.assertRegex(self.out, r"DROP   u/proj/target +45200 files +15\.0 GiB")
        self.assertIn("KEEP   u/.local  (a cache folder that holds a skipped path)", self.out)
        self.assertIn("SKIP   u/.local/share/containers  (skip list: kept whole)", self.out)
        self.assertIn("ERROR  u/odd: cannot list: Permission denied", self.out)
        self.assertEqual(self.cli(h, "caches", "--json"), 0)
        data = json.loads(self.out)["sources"][0]
        self.assertEqual(
            (data["files"], data["bytes"], data["folders"][1]["path"]), (45210, (15 << 30) + 2048, "u/.npm/_cacache")
        )
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.cli(h, "caches", source="home=/top/nothere:1h"), 1)


class RealTreeHost(FakeHost):
    """FakeHost whose snapshots are real copies in a temporary folder, so the
    real walk, the real deletion and the real state files run end to end."""

    def __init__(self, now: float, top: str) -> None:
        super().__init__(now)
        self.dirs = {top, os.path.join(top, "snapshots")}

    def run(self, argv: Sequence[str]) -> tuple[int, str, str]:
        out = super().run(argv)
        if argv[:3] == ["btrfs", "subvolume", "snapshot"] and out[0] == 0:
            src, dest = argv[-2:]
            shutil.copytree(src, dest, symlinks=True)
        return out

    def walk_caches(self, root: str, skips: Sequence[str], mode: str) -> _CacheWalkProto:
        self.calls.append(["walk", root, list(skips), mode])
        return core.walk_caches(root, skips, mode)


class RealTreeTests(CliTestCase):
    def test_end_to_end_on_real_folders(self) -> None:
        top = os.path.join(self.tmp.name, "top")
        src = os.path.join(top, "home")
        os.makedirs(os.path.join(top, "snapshots"))
        tag(src, "u/proj/target")
        put(src, "u/proj/target/debug/app", "binary")
        put(src, "u/proj/src/main.rs")
        tag(src, "u/.local/share/containers/storage/x")
        h = RealTreeHost(at("2026-10-13 14:07", BERLIN), top)
        h.add_source(src, 100)
        rc = self.cli(
            h,
            "snapshot",
            "--exclude-caches",
            "--caches-skip",
            "home=u/.local/share/containers",
            source=f"home={src}:72h",
            snapdir=os.path.join(top, "snapshots"),
        )
        self.assertEqual(rc, 0, self.out)
        snap = os.path.join(top, "snapshots", "home.20261013T1407")
        self.assertFalse(os.path.lexists(os.path.join(snap, "u", "proj", "target")))
        self.assertTrue(os.path.exists(os.path.join(snap, "u", "proj", "src", "main.rs")))
        self.assertTrue(
            os.path.exists(os.path.join(snap, "u", ".local", "share", "containers", "storage", "x", core.CACHE_TAG))
        )
        self.assertTrue(os.path.exists(os.path.join(src, "u", "proj", "target", "debug", "app")))  # source intact
        self.assertTrue(h.subvols[snap]["ro"])
        self.assertIn("dropped 1 cache folder(s) from home.20261013T1407: 2 files", self.out)
        self.assertFalse(os.path.exists(os.path.join(self.state, core.UNFINISHED_FILE)))


if __name__ == "__main__":
    unittest.main()

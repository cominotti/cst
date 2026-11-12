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

import contextlib
import datetime as dt
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest
import zoneinfo

sys.dont_write_bytecode = True   # keep __pycache__ out of the repository

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_PATH = os.path.join(os.path.dirname(HERE), "btrfs-snapshot-core")


def load_core():
    loader = importlib.machinery.SourceFileLoader("btrfs_snapshot_core", CORE_PATH)
    spec = importlib.util.spec_from_loader("btrfs_snapshot_core", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


core = load_core()

BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")
NEW_YORK = zoneinfo.ZoneInfo("America/New_York")
UTC = zoneinfo.ZoneInfo("UTC")


def W(text):
    """'2026-10-13 14:05' -> naive wall datetime."""
    return dt.datetime.strptime(text, "%Y-%m-%d %H:%M")


def snap(text, seq=0, name="home"):
    wall = W(text)
    return core.Snap(entry=core.snapshot_name(name, wall, seq), wall=wall, seq=seq)


def at(text, tz):
    """Epoch of a local wall time (earlier instant if ambiguous)."""
    return core.wall_to_epoch(W(text), tz)


def decide(snaps, now_text, policy, tz):
    rows = core.plan(snaps, at(now_text, tz), core.parse_policy(policy), tz)
    return {r.snap.entry: r.action for r in rows}


def kept(snaps, now_text, policy, tz):
    return sorted(e for e, a in decide(snaps, now_text, policy, tz).items() if a == "keep")


def hourly_series(start, end, minute=5, step_minutes=60):
    out, t = [], W(start).replace(minute=minute)
    stop = W(end).replace(minute=minute)
    while t <= stop:
        out.append(core.Snap(entry=core.snapshot_name("home", t), wall=t))
        t += dt.timedelta(minutes=step_minutes)
    return out


def E(text, seq=0):
    return core.snapshot_name("home", W(text), seq)


# ---- reference model ----------------------------------------------------------------
def reference_keep(snaps, now, policy, tz):
    """Independent formulation of btrbk's rules (past snapshots only): at each
    level, the oldest snapshot of every bucket whose distance is within N; days
    start 00:00, weeks Sunday 00:00, months/years on their first Sunday; plus
    the newest. Returns the set of kept entries."""
    def norm(s):
        e = core.wall_to_epoch(s.wall, tz)
        return e, core.epoch_to_wall(e, tz)

    def corr_month(w):
        sunday_start = w.date() - dt.timedelta(days=(w.weekday() + 1) % 7)
        if sunday_start.month != w.month or sunday_start.year != w.year:
            # this week began in the previous month, so the month has not started yet
            first = w.replace(day=1) - dt.timedelta(days=1)
            return first.year, first.month
        return w.year, w.month

    def week_start(d):
        return d - dt.timedelta(days=(d.weekday() + 1) % 7)

    now_w = core.epoch_to_wall(now, tz)
    now_h = now_w.replace(minute=0, second=0, microsecond=0)
    items = []
    for s in snaps:
        e, w = norm(s)
        y, m = corr_month(w)
        items.append({
            "entry": s.entry, "key": (e, s.seq),
            "h": int((now_h - w.replace(minute=0)).total_seconds() // 3600),
            "d": (now_w.date() - w.date()).days,
            "w": (week_start(now_w.date()) - week_start(w.date())).days // 7,
            "m": (now_w.year * 12 + now_w.month) - (y * 12 + m),
            "y": now_w.year - y,
        })
    items.sort(key=lambda i: i["key"])
    keep = set()
    if items:
        keep.add(items[-1]["entry"])
    for unit in "hdwmy":
        n = policy.get(unit)
        if not n:
            continue
        seen = set()
        for i in items:
            if i[unit] in seen:
                continue
            seen.add(i[unit])
            if n == "all" or i[unit] <= n:
                keep.add(i["entry"])
    return keep


def random_timeline(rng, tz, count):
    """Creation epochs and names as the tool would produce them (local names, _N)."""
    t = core.wall_to_epoch(dt.datetime(2025 + rng.randrange(4), 1 + rng.randrange(12), 1 + rng.randrange(28),
                                       rng.randrange(24), rng.randrange(60)), tz)
    snaps, taken = [], {}
    for _ in range(count):
        r = rng.random()
        if r < 0.70:
            t += rng.randrange(40, 80) * 60          # hourly-ish
        elif r < 0.85:
            t += rng.randrange(1, 30) * 60           # several per hour
        elif r < 0.97:
            t += rng.randrange(2, 30) * 3600         # off for a while
        else:
            t += rng.randrange(2, 40) * 86400        # suspended / away for days or weeks
        wall = core.epoch_to_wall(t, tz).replace(second=0)
        base = wall.strftime(core.TS_FORMAT)
        seq = taken.get(base, -1) + 1
        taken[base] = seq
        snaps.append((t, core.Snap(entry=core.snapshot_name("home", wall, seq), wall=wall, seq=seq)))
    return snaps


RANDOM_TZS = ["Europe/Berlin", "America/New_York", "UTC", "Asia/Kolkata", "Australia/Lord_Howe",
              "America/Sao_Paulo"]
RANDOM_POLICIES = ["72h 7d 4w 3m", "48h 7d", "24h 3d", "1h", "6h 2d 1w 1m 1y", "3d", "2w 2m",
                   "*h 1d", "10h,5d,3w,2m,1y", "1m", "1y", "*m"]


# ---- policy and names -----------------------------------------------------------------
class PolicyTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(core.parse_policy("72h 7d 4w 3m"), {"h": 72, "d": 7, "w": 4, "m": 3})
        self.assertEqual(core.parse_policy("48h,7d"), {"h": 48, "d": 7})
        self.assertEqual(core.parse_policy(" 2H, 3D  *y "), {"h": 2, "d": 3, "y": "all"})
        self.assertEqual(core.format_policy(core.parse_policy("3m 72h *y")), "72h 3m *y")

    def test_parse_rejects(self):
        for bad in ("", "  ", "no", "72", "h", "7x", "1h 2h", "1h;2d", "-1h", "1.5d"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                core.parse_policy(bad)

    def test_max_kept(self):
        self.assertEqual(core.policy_max_kept(core.parse_policy("72h 7d 4w 3m")), 73 + 8 + 5 + 4 + 1)
        self.assertIsNone(core.policy_max_kept(core.parse_policy("*h 1d")))

    def test_names(self):
        self.assertEqual(core.snapshot_name("home", W("2026-03-05 07:09")), "home.20260305T0709")
        self.assertEqual(core.snapshot_name("home", W("2026-03-05 07:09"), 2), "home.20260305T0709_2")
        self.assertEqual(core.parse_snapshot_name("home", "home.20260305T0709_2"), (W("2026-03-05 07:09"), 2))
        for bad in ("home.20260305T0709x", "home.2026030T0709", "home.20261305T0709", "home.20260230T0000",
                    "homeX20260305T0709", "Workspace.20260305T0709", "home.20260305T0709_", "home.20260305T0709/x"):
            with self.subTest(bad=bad):
                self.assertIsNone(core.parse_snapshot_name("home", bad))
        # A dot in NAME is literal.
        self.assertIsNone(core.parse_snapshot_name("a.b", "aXb.20260305T0709"))

    def test_source_parse(self):
        s = core.parse_source("Workspace=/top/home/u/Workspace/:48h 7d")
        self.assertEqual((s.name, s.path, s.policy), ("Workspace", "/top/home/u/Workspace", {"h": 48, "d": 7}))
        s = core.parse_source("x=/a:b/c:1d")    # the policy is after the LAST colon
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
        "\tQuota group:\t\tn/a\n")

    def test_real_output(self):
        self.assertEqual(core.parse_subvolume_show(self.REAL), {
            "uuid": "f0402c44-a6dc-ad45-b805-24977d4eaee4", "parent_uuid": None,
            "generation": 924444, "cgen": 9})

    def test_parent_uuid(self):
        text = self.REAL.replace("Parent UUID: \t\t-", "Parent UUID: \t\t0badc0de-0000-0000-0000-000000000001")
        self.assertEqual(core.parse_subvolume_show(text)["parent_uuid"], "0badc0de-0000-0000-0000-000000000001")

    def test_failures(self):
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
    def test_73rd_and_74th_hour(self):
        snaps = hourly_series("2026-06-11 23:00", "2026-06-15 10:00")
        d = decide(snaps, "2026-06-15 10:30", "72h", BERLIN)
        self.assertEqual(d[E("2026-06-15 10:05")], "keep")    # hour 0
        self.assertEqual(d[E("2026-06-12 10:05")], "keep")    # hour 72: inclusive
        self.assertEqual(d[E("2026-06-12 09:05")], "delete")  # hour 73
        self.assertEqual(d[E("2026-06-12 08:05")], "delete")  # hour 74
        self.assertEqual(sum(1 for a in d.values() if a == "keep"), 73)
        # With dailies, the 73rd hour still goes; the first of that day stays.
        d = decide(snaps, "2026-06-15 10:30", "72h 7d", BERLIN)
        self.assertEqual(d[E("2026-06-12 09:05")], "delete")
        self.assertEqual(d[E("2026-06-12 00:05")], "keep")
        self.assertEqual(d[E("2026-06-11 23:05")], "keep")    # first of 2026-06-11
        # The hour boundary is the wall-clock hour, not 60 minutes before now.
        d = decide(snaps, "2026-06-15 11:00", "72h", BERLIN)
        self.assertEqual(d[E("2026-06-12 10:05")], "delete")  # now hour 73
        self.assertEqual(d[E("2026-06-12 11:05")], "keep")

    def test_several_per_hour_keeps_the_first(self):
        snaps = [snap("2026-06-15 05:05"), snap("2026-06-15 05:20"), snap("2026-06-15 05:40"),
                 snap("2026-06-15 05:40", 1), snap("2026-06-15 06:01"), snap("2026-06-15 06:59")]
        self.assertEqual(kept(snaps, "2026-06-15 07:00", "24h", BERLIN),
                         sorted([E("2026-06-15 05:05"), E("2026-06-15 06:01"), E("2026-06-15 06:59")]))
        d = decide(snaps, "2026-06-15 07:00", "24h", BERLIN)
        self.assertEqual(d[E("2026-06-15 05:40", 1)], "delete")
        # 06:59 is kept only as the latest.
        rows = core.plan(snaps, at("2026-06-15 07:00", BERLIN), core.parse_policy("24h"), BERLIN)
        self.assertEqual(rows[-1].reason, "preserve min: latest")

    def test_suffix_orders_within_minute(self):
        snaps = [snap("2026-06-15 05:40", 2), snap("2026-06-15 05:40"), snap("2026-06-15 05:40", 1)]
        rows = core.plan(snaps, at("2026-06-15 07:00", BERLIN), core.parse_policy("24h"), BERLIN)
        self.assertEqual([r.snap.seq for r in rows], [0, 1, 2])
        self.assertEqual([r.action for r in rows], ["keep", "delete", "keep"])

    def test_first_sunday_of_month(self):
        snaps = []
        day = W("2026-08-25 12:00")
        while day <= W("2026-11-20 12:00"):
            snaps.append(core.Snap(entry=core.snapshot_name("home", day), wall=day))
            day += dt.timedelta(days=1)
        self.assertEqual(kept(snaps, "2026-11-20 13:00", "2m", BERLIN), sorted([
            E("2026-09-06 12:00"),   # first Sunday of September: month 2 starts here
            E("2026-10-04 12:00"),   # first Sunday of October
            E("2026-11-01 12:00"),   # 1 November 2026 is a Sunday
            E("2026-11-20 12:00"),   # latest
        ]))
        # Sep 1-5 still belong to August's month (months start on their first Sunday).
        rows = {r.snap.entry: r.reason for r in
                core.plan(snaps, at("2026-11-20 13:00", BERLIN), core.parse_policy("3m"), BERLIN)}
        self.assertIn("first weekly of month 2026-08", rows[E("2026-08-25 12:00")])
        self.assertNotIn("preserve", rows[E("2026-09-05 12:00")])

    def test_week_starts_sunday_midnight(self):
        snaps = [snap("2026-10-17 23:30"), snap("2026-10-18 00:30"), snap("2026-10-21 10:00"),
                 snap("2026-10-28 11:00")]
        self.assertEqual(kept(snaps, "2026-10-28 12:00", "1w", BERLIN),
                         sorted([E("2026-10-18 00:30"), E("2026-10-28 11:00")]))
        self.assertEqual(kept(snaps, "2026-10-28 12:00", "2w", BERLIN),
                         sorted([E("2026-10-17 23:30"), E("2026-10-18 00:30"), E("2026-10-28 11:00")]))

    def test_year_starts_first_sunday(self):
        snaps = [snap("2026-07-01 10:00"), snap("2027-01-01 10:00"), snap("2027-01-03 10:00"),
                 snap("2027-05-31 10:00")]
        self.assertEqual(kept(snaps, "2027-06-01 12:00", "1y", BERLIN),
                         sorted([E("2026-07-01 10:00"), E("2027-01-03 10:00"), E("2027-05-31 10:00")]))

    def test_leap_day(self):
        snaps = [snap(f"2028-{md} 10:00") for md in ("02-26", "02-27", "02-28", "02-29", "03-01", "03-02")]
        self.assertEqual(kept(snaps, "2028-03-02 12:00", "3d", BERLIN),
                         sorted(E(f"2028-{md} 10:00") for md in ("02-28", "02-29", "03-01", "03-02")))
        hours = hourly_series("2028-02-28 00:00", "2028-03-01 05:00")
        d = decide(hours, "2028-03-01 05:30", "29h", BERLIN)
        self.assertEqual(d[E("2028-02-29 00:05")], "keep")     # 29 h
        self.assertEqual(d[E("2028-02-28 23:05")], "delete")   # 30 h

    def test_dst_spring_berlin(self):
        # 2026-03-29 02:00 CET -> 03:00 CEST: hour 02 does not exist.
        snaps = [snap("2026-03-29 00:10"), snap("2026-03-29 01:10"), snap("2026-03-29 03:10"),
                 snap("2026-03-29 04:10")]
        # btrbk counts wall-clock hours: 01:10 -> 05:30 is hour 4 although 3h20m passed.
        d = decide(snaps, "2026-03-29 05:30", "3h", BERLIN)
        self.assertEqual(d[E("2026-03-29 01:10")], "delete")
        self.assertEqual(d[E("2026-03-29 03:10")], "keep")
        self.assertEqual(decide(snaps, "2026-03-29 05:30", "4h", BERLIN)[E("2026-03-29 01:10")], "keep")
        # A name in the gap (clock set by hand) reads as one hour later, like timelocal().
        gap = snap("2026-03-29 02:30")
        self.assertEqual(core.epoch_to_wall(core.wall_to_epoch(gap.wall, BERLIN), BERLIN), W("2026-03-29 03:30"))
        d = decide(snaps + [gap], "2026-03-29 05:30", "4h", BERLIN)
        self.assertEqual(d[gap.entry], "delete")          # hour 03's first is 03:10
        self.assertEqual(d[E("2026-03-29 03:10")], "keep")

    def test_dst_autumn_berlin(self):
        # 2026-10-25 03:00 CEST -> 02:00 CET: 02:30 happens twice -> _1 suffix.
        snaps = [snap("2026-10-25 01:30"), snap("2026-10-25 02:30"), snap("2026-10-25 02:30", 1),
                 snap("2026-10-25 03:30")]
        d = decide(snaps, "2026-10-25 04:00", "5h", BERLIN)
        self.assertEqual(d, {E("2026-10-25 01:30"): "keep", E("2026-10-25 02:30"): "keep",
                             E("2026-10-25 02:30", 1): "delete", E("2026-10-25 03:30"): "keep"})
        # Day boundary unaffected: the 25th has 25 hours, all one day.
        day = hourly_series("2026-10-24 22:00", "2026-10-26 01:00")
        self.assertEqual(kept(day, "2026-10-26 01:30", "1d", BERLIN),
                         sorted([E("2026-10-25 00:05"), E("2026-10-26 00:05"), E("2026-10-26 01:05")]))

    def test_dst_new_york(self):
        # 2026-03-08 02:00 EST -> 03:00 EDT; 2026-11-01 02:00 EDT -> 01:00 EST.
        snaps = [snap("2026-03-07 23:30"), snap("2026-03-08 00:30"), snap("2026-03-08 03:30"),
                 snap("2026-03-09 06:00")]
        self.assertEqual(kept(snaps, "2026-03-09 12:00", "1d", NEW_YORK),
                         sorted([E("2026-03-08 00:30"), E("2026-03-09 06:00")]))
        snaps = [snap("2026-11-01 00:30"), snap("2026-11-01 01:30"), snap("2026-11-01 01:30", 1),
                 snap("2026-11-01 02:30")]
        d = decide(snaps, "2026-11-01 03:00", "3h", NEW_YORK)
        self.assertEqual(d, {E("2026-11-01 00:30"): "keep", E("2026-11-01 01:30"): "keep",
                             E("2026-11-01 01:30", 1): "delete", E("2026-11-01 02:30"): "keep"})
        # Weekly across the autumn change (the 1st is a Sunday: new week, new month).
        snaps = [snap("2026-10-31 23:30"), snap("2026-11-01 00:30"), snap("2026-11-02 00:30")]
        self.assertEqual(kept(snaps, "2026-11-03 09:00", "0h 1w", NEW_YORK),
                         sorted([E("2026-10-31 23:30"), E("2026-11-01 00:30"), E("2026-11-02 00:30")]))
        self.assertEqual(kept(snaps, "2026-11-03 09:00", "1m", NEW_YORK),
                         sorted([E("2026-10-31 23:30"), E("2026-11-01 00:30"), E("2026-11-02 00:30")]))

    def test_lord_howe_half_hour_dst_end(self):
        # Documented deviation from btrbk (see wall_to_epoch()): 2027-04-04 02:00
        # LHDT (+11) -> 01:30 LHST (+10:30), so 01:30-01:59 happens twice. We read
        # 01:38 as the earlier instant, 14:38 UTC, which localtime() shows as
        # 01:38 again; btrbk's Time::Local 1.35 says 14:08 UTC (shown as 01:08),
        # orders 01:38 before 01:09 and so keeps 01:38 as the first of the year.
        lh = zoneinfo.ZoneInfo("Australia/Lord_Howe")
        e = core.wall_to_epoch(W("2027-04-04 01:38"), lh)
        self.assertEqual(dt.datetime.fromtimestamp(e, dt.timezone.utc), dt.datetime(2027, 4, 3, 14, 38,
                                                                                   tzinfo=dt.timezone.utc))
        self.assertEqual(core.epoch_to_wall(e, lh), W("2027-04-04 01:38"))
        rows = core.plan([snap("2027-04-04 01:09"), snap("2027-04-04 01:38")],
                         core.wall_to_epoch(W("2027-04-05 03:41"), lh), core.parse_policy("72h 7d 4w 3m 2y"), lh)
        self.assertEqual([(r.snap.entry, r.reason.split(":")[0]) for r in rows],
                         [(E("2027-04-04 01:09"), "preserve yearly"), (E("2027-04-04 01:38"), "preserve min")])

    def test_gap_after_suspend(self):
        before = hourly_series("2026-06-08 00:00", "2026-06-10 18:00")
        after = hourly_series("2026-06-14 09:00", "2026-06-15 10:00")
        snaps = before + after
        policy = "72h 7d 4w 3m"
        d = decide(snaps, "2026-06-15 10:30", policy, BERLIN)
        self.assertEqual(d[E("2026-06-10 18:05")], "delete")    # hour 112, not first of day
        self.assertEqual(d[E("2026-06-10 00:05")], "keep")      # first of day 5
        self.assertEqual(d[E("2026-06-08 00:05")], "keep")      # first of day 7 (and of its week)
        self.assertTrue(all(d[s.entry] == "keep" for s in after))
        ref = reference_keep(snaps, at("2026-06-15 10:30", BERLIN), core.parse_policy(policy), BERLIN)
        self.assertEqual({e for e, a in d.items() if a == "keep"}, ref)

    def test_future_dates_are_kept(self):
        snaps = [snap("2026-10-01 10:00"), snap("2026-10-13 09:00"), snap("2026-10-14 08:00")]
        rows = core.plan(snaps, at("2026-10-13 12:00", BERLIN), core.parse_policy("1h"), BERLIN)
        self.assertEqual([r.action for r in rows], ["delete", "delete", "keep"])
        # The reason is the last rule that matched (btrbk): with no hourly rule, "forced".
        rows = core.plan(snaps + [snap("2026-10-14 09:00")], at("2026-10-13 12:00", BERLIN),
                         core.parse_policy("0h"), BERLIN)
        self.assertEqual([r.action for r in rows], ["delete", "delete", "keep", "keep"])
        self.assertEqual(rows[2].reason, "preserve forced: 20 hours in the future")

    def test_zero_count_disables_unit(self):
        # btrbk treats a 0 count as "not set" (Perl false): 0h keeps no hourly.
        snaps = [snap("2026-10-13 10:05"), snap("2026-10-13 11:05"), snap("2026-10-13 11:30")]
        self.assertEqual(kept(snaps, "2026-10-13 11:40", "0h", BERLIN), [E("2026-10-13 11:30")])

    def test_reasons_match_btrbk_wording(self):
        snaps = [snap("2026-08-02 00:00"), snap("2026-10-05 03:00"), snap("2026-10-13 05:00"),
                 snap("2026-10-13 11:00")]
        rows = core.plan(snaps, at("2026-10-13 11:30", BERLIN), core.parse_policy("2h 1d 1w 3m"), BERLIN)
        self.assertEqual([r.reason for r in rows], [
            "preserve monthly: first weekly of month 2026-08 (2 months ago, at sunday 00:00)",
            # 2026-10-04 is October's first Sunday: the 5th is the first weekly of October.
            "preserve monthly: first weekly of month 2026-10 (0 months ago, 1d 3h after sunday 00:00)",
            "preserve weekly: 0 weeks ago, 2d 5h after sunday 00:00",
            "preserve hourly: first of hour, 0 hours ago",
        ])
        rows = core.plan(snaps, at("2026-10-13 11:30", BERLIN), core.parse_policy("12h 1d"), BERLIN)
        self.assertEqual(rows[2].reason, "preserve daily: first of day, 0 days ago, 5h after 00:00")


class RetentionPropertyTests(unittest.TestCase):
    CASES = 120

    def cases(self, seed):
        rng = random.Random(seed)
        for _ in range(self.CASES):
            tz = zoneinfo.ZoneInfo(rng.choice(RANDOM_TZS))
            timeline = random_timeline(rng, tz, rng.randrange(1, 160))
            now = timeline[-1][0] + rng.randrange(0, 72 * 3600)
            yield rng, tz, timeline, now, core.parse_policy(rng.choice(RANDOM_POLICIES))

    def test_newest_kept_and_matches_reference(self):
        for _rng, tz, timeline, now, policy in self.cases(1):
            snaps = [s for _, s in timeline]
            rows = core.plan(snaps, now, policy, tz)
            self.assertEqual(rows[-1].action, "keep")
            got = {r.snap.entry for r in rows if r.action == "keep"}
            self.assertEqual(got, reference_keep(snaps, now, policy, tz), (tz, core.format_policy(policy)))

    def test_idempotent(self):
        for _rng, tz, timeline, now, policy in self.cases(2):
            rows = core.plan([s for _, s in timeline], now, policy, tz)
            remaining = [r.snap for r in rows if r.action == "keep"]
            again = core.plan(remaining, now, policy, tz)
            self.assertEqual([r.action for r in again], ["keep"] * len(remaining))

    def test_incremental_equals_batch(self):
        for _rng, tz, timeline, _now, policy in self.cases(3):
            have = []
            for created, s in timeline:
                have.append(s)
                have = [r.snap for r in core.plan(have, created, policy, tz) if r.action == "keep"]
            last = timeline[-1][0]
            batch = [r.snap.entry for r in core.plan([s for _, s in timeline], last, policy, tz)
                     if r.action == "keep"]
            self.assertEqual(sorted(s.entry for s in have), sorted(batch), (tz, core.format_policy(policy)))

    def test_totals_bounded(self):
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
        self.assertLessEqual(n, core.policy_max_kept(policy))
        self.assertGreaterEqual(n, 73 + 5)


# ---- btrbk cross-check (optional) -----------------------------------------------------
PERL_HARNESS = r'''
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
'''


@unittest.skipUnless(os.environ.get("BTRBK_SOURCE") and shutil.which("perl"),
                     "set BTRBK_SOURCE=/path/to/btrbk (0.32.7) and have perl to compare with btrbk itself")
class BtrbkOracleTests(unittest.TestCase):
    def setUp(self):
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

    def tearDown(self):
        self.tmp.cleanup()

    def btrbk(self, snaps, now, policy_text, tzname):
        lines = "".join(f"{s.wall:%Y %m %d %H %M} {s.seq}\n" for s in snaps)
        env = {"PATH": os.environ.get("PATH", "/usr/bin"), "TZ": tzname, "BTRBK_SUBS": self.subs}
        out = subprocess.run(["perl", self.harness, str(int(now)), policy_text], input=lines, text=True,
                             capture_output=True, env=env, check=True).stdout
        return {int(i): (a, r) for i, a, r in (ln.split("\t") for ln in out.splitlines())}

    def compare(self, snaps, now, policy_text, tzname):
        tz = zoneinfo.ZoneInfo(tzname)
        ours = core.plan(snaps, now, core.parse_policy(policy_text), tz)
        theirs = self.btrbk(snaps, now, policy_text, tzname)
        index = {id(s): i for i, s in enumerate(snaps)}
        for r in ours:
            action, reason = theirs[index[id(r.snap)]]
            self.assertEqual((r.action, r.reason if r.action == "keep" else ""), (action, reason),
                             (r.snap.entry, tzname, policy_text, now))

    def test_random_against_btrbk(self):
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
            now = timeline[-1][0] + rng.randrange(-3600, 72 * 3600)   # also a few future-dated ones
            self.compare([s for _, s in timeline], now, rng.choice(RANDOM_POLICIES).replace(",", " "), tzname)

    def test_dst_edges_against_btrbk(self):
        for tzname, day in (("Europe/Berlin", "2026-03-29"), ("Europe/Berlin", "2026-10-25"),
                            ("America/New_York", "2026-03-08"), ("America/New_York", "2026-11-01")):
            tz = zoneinfo.ZoneInfo(tzname)
            snaps = [snap(f"{day} {h:02d}:{m:02d}", seq) for h in range(0, 5) for m in (0, 30) for seq in (0, 1)]
            for now_h in ("05:10", "23:00"):
                for policy in ("3h", "2h 1d", "1h 1d 1w 1m 1y"):
                    self.compare(snaps, at(f"{day} {now_h}", tz), policy, tzname)


# ---- the command line with a fake btrfs ------------------------------------------------
def uuid_n(n):
    return f"{n:08x}-0000-4000-8000-{n:012x}"


class FakeHost:
    """A tiny in-memory btrfs: subvolumes with UUID/parent/generation/ro.

    Time: `now` is the wall clock; CLOCK_BOOTTIME is derived from it (now -
    boot_at), so advancing `now` is real time passing. step_clock() moves only
    the wall clock (a clock jump), reboot() starts a new boot."""

    def __init__(self, now, tz=BERLIN):
        self.tz = tz
        self.now = now
        self.boot_id = uuid_n(0xb007)
        self.boot_at = now - 10 * 86400
        self.subvols = {}         # path -> dict
        self.other = {}           # path -> "symlink" | "other"
        self.dirs = {"/top", "/top/snapshots"}
        self.calls = []
        self.ntp = "yes"
        self.transid = 5000
        self.next_uuid = 1
        self.on_delete = None
        self.on_listdir = None
        self.listdir_calls = 0
        self.show_override = {}   # path -> raw text

    # helpers
    def new_uuid(self):
        self.next_uuid += 1
        return uuid_n(self.next_uuid)

    def add_source(self, path, generation):
        u = self.new_uuid()
        self.subvols[path] = {"uuid": u, "parent": None, "gen": generation, "cgen": 9, "ro": False}
        return u

    def add_snapshot(self, entry, source, cgen, ro=True, parent=None, snapdir="/top/snapshots"):
        path = f"{snapdir}/{entry}"
        self.subvols[path] = {"uuid": self.new_uuid(),
                              "parent": parent if parent is not None else self.subvols[source]["uuid"],
                              "gen": cgen, "cgen": cgen, "ro": ro}
        return path

    def snapshots(self, snapdir="/top/snapshots"):
        return sorted(os.path.basename(p) for p in self.subvols if os.path.dirname(p) == snapdir)

    def mutating_calls(self):
        return [c for c in self.calls if c[:3] in (["btrfs", "subvolume", "snapshot"],
                                                    ["btrfs", "subvolume", "delete"])]

    def step_clock(self, seconds):
        """The wall clock jumps; the time since boot does not."""
        self.now += seconds
        self.boot_at += seconds

    def reboot(self, uptime=60):
        self.boot_id = uuid_n(self.next_uuid + 0xb000)
        self.next_uuid += 1
        self.boot_at = self.now - uptime

    # Host interface
    def time(self):
        return self.now

    def boot(self):
        return self.boot_id, float(self.now - self.boot_at)

    def listdir(self, path):
        self.listdir_calls += 1
        if self.on_listdir:
            self.on_listdir(self, self.listdir_calls)
        if path not in self.dirs:
            raise FileNotFoundError(path)
        names = {os.path.basename(p) for p in list(self.subvols) + list(self.other) + list(self.dirs)
                 if os.path.dirname(p) == path and p != path}
        return sorted(names)

    def kind(self, path):
        if path in self.subvols:
            return "subvol"
        if path in self.other:
            return self.other[path]
        if path in self.dirs:
            return "other"
        return "missing"

    def mkdir(self, path, mode):
        self.dirs.add(path)

    def show_text(self, path, sv):
        return (f"{os.path.basename(path)}\n\tName: \t\t\t{os.path.basename(path)}\n"
                f"\tUUID: \t\t\t{sv['uuid']}\n\tParent UUID: \t\t{sv['parent'] or '-'}\n"
                f"\tReceived UUID: \t\t-\n\tCreation time: \t\t2026-05-08 23:21:53 +0200\n"
                f"\tSubvolume ID: \t\t300\n\tGeneration: \t\t{sv['gen']}\n\tGen at creation: \t{sv['cgen']}\n"
                f"\tParent ID: \t\t5\n\tTop level ID: \t\t5\n\tFlags: \t\t\t{'readonly' if sv['ro'] else '-'}\n"
                f"\tSnapshot(s):\n\tQuota group:\t\tn/a\n")

    def run(self, argv):
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
        if argv[:4] == ["btrfs", "subvolume", "snapshot", "-r"]:
            src, dest = argv[4], argv[5]
            if dest in self.subvols or dest in self.other or os.path.dirname(dest) not in self.dirs:
                return 1, "", "ERROR: cannot snapshot"
            self.transid += 1
            # Kernel >= 7.1: the source's generation stays behind the new cgen while idle.
            self.subvols[dest] = {"uuid": self.new_uuid(), "parent": self.subvols[src]["uuid"],
                                  "gen": self.transid, "cgen": self.transid, "ro": True}
            return 0, f"Create readonly snapshot of '{src}' in '{dest}'\n", ""
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
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")
        self.lock = os.path.join(self.tmp.name, "lock")
        self.pins = os.path.join(self.tmp.name, "pinned")

    def tearDown(self):
        self.tmp.cleanup()

    def host(self, now_text, gen=100):
        h = FakeHost(at(now_text, BERLIN))
        h.add_source("/top/home", gen)
        return h

    def cli(self, host, cmd, *extra, source="home=/top/home:72h 7d 4w 3m", sources=None, clock_ok=False):
        argv = [cmd, "--snapdir", "/top/snapshots"]
        if cmd != "latest":
            argv += ["--tz", "Europe/Berlin"]
        for s in (sources if sources is not None else [source]):
            argv += ["--source", s]
        if cmd in ("snapshot", "prune", "list"):
            argv += ["--state-dir", self.state, "--pin-file", self.pins]
        if cmd in ("snapshot", "prune"):
            argv += ["--lock", self.lock]
        if clock_ok:
            argv += ["--assume-clock-ok"]
        argv += list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = core.main(argv, host=host)
            except SystemExit as e:   # argparse
                rc = e.code
        self.out, self.err = out.getvalue(), err.getvalue()
        return rc

    def add_history(self, host, start, end, step_hours=1, source="/top/home", name="home"):
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
    def test_first_run_creates(self):
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(h.snapshots(), ["home.20261013T1407"])
        self.assertIn("summary: home: created home.20261013T1407", self.out)
        self.assertTrue(os.path.exists(os.path.join(self.state, core.STATE_FILE)))

    def test_idle_source_creates_nothing(self):
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        h.now += 3600
        self.assertEqual(self.cli(h, "snapshot"), 0)      # gen 100 < cgen 5001 (kernel >= 7.1)
        self.assertEqual(h.snapshots(), ["home.20261013T1407"])
        self.assertIn("unchanged", self.out)

    def test_equal_generation_is_unchanged(self):
        # Older kernels: snapshot creation bumps the source to the same generation.
        h = self.host("2026-10-13 14:07", gen=500)
        h.add_snapshot("home.20261013T1300", "/top/home", 500)
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(h.snapshots(), ["home.20261013T1300"])

    def test_write_creates(self):
        h = self.host("2026-10-13 14:07")
        self.cli(h, "snapshot")
        h.now += 3600
        h.subvols["/top/home"]["gen"] = h.transid + 7
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(h.snapshots(), ["home.20261013T1407", "home.20261013T1507"])

    def test_newest_by_generation_not_name(self):
        h = self.host("2026-10-13 14:07", gen=300)
        # The name says 13:00 is newer, but 12:00 was created later (clock stepped back).
        h.add_snapshot("home.20261013T1300", "/top/home", 200)
        h.add_snapshot("home.20261013T1200", "/top/home", 400)
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(len(h.snapshots()), 2)            # 300 < 400: unchanged
        h.subvols["/top/home"]["gen"] = 401
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(len(h.snapshots()), 3)

    def test_safety_net_after_24h(self):
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20261012T1406", "/top/home", 50)
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertIn("home.20261013T1407", h.snapshots())
        self.assertIn("older than 24 h", self.out)
        h2 = self.host("2026-10-13 14:07", gen=10)
        h2.add_snapshot("home.20261012T1408", "/top/home", 50)
        self.cli(h2, "snapshot")
        self.assertEqual(h2.snapshots(), ["home.20261012T1408"])

    def test_force(self):
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20261013T1400", "/top/home", 50)
        self.assertEqual(self.cli(h, "snapshot", "--force"), 0)
        self.assertEqual(len(h.snapshots()), 2)

    def test_collision_suffix(self):
        h = self.host("2026-10-13 14:07")
        self.cli(h, "snapshot")
        for expected in ("home.20261013T1407_1", "home.20261013T1407_2"):
            self.assertEqual(self.cli(h, "snapshot", "--force"), 0)
            self.assertIn(expected, h.snapshots())
        # Any entry with the name counts, even one that is not ours.
        h.other["/top/snapshots/home.20261013T1407_7"] = "other"
        self.cli(h, "snapshot", "--force")
        self.assertIn("home.20261013T1407_8", h.snapshots())

    def test_local_time_names(self):
        h = FakeHost(at("2026-07-01 23:30", NEW_YORK))
        h.add_source("/top/home", 100)
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(h.snapshots(), ["home.20260702T0530"])   # rendered in --tz Europe/Berlin

    def test_two_sources(self):
        h = self.host("2026-10-13 14:07")
        h.add_source("/top/home/u/Workspace", 100)
        rc = self.cli(h, "snapshot", sources=["home=/top/home:72h 7d", "Workspace=/top/home/u/Workspace:48h 7d"])
        self.assertEqual(rc, 0)
        self.assertEqual(h.snapshots(), ["Workspace.20261013T1407", "home.20261013T1407"])
        ws = h.subvols["/top/snapshots/Workspace.20261013T1407"]
        self.assertEqual(ws["parent"], h.subvols["/top/home/u/Workspace"]["uuid"])

    def test_missing_snapdir_is_created(self):
        h = self.host("2026-10-13 14:07")
        h.dirs.discard("/top/snapshots")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertIn("/top/snapshots", h.dirs)
        self.assertEqual(self.cli(h, "prune"), 0)

    def test_source_errors(self):
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/nothere:1d"), 1)
        h.show_override["/top/home"] = "home\n\tUUID: \t\tgarbage\n"
        self.assertEqual(self.cli(h, "snapshot"), 1)
        self.assertEqual(h.mutating_calls(), [])

    def test_usage(self):
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:7q"), 2)
        self.assertEqual(self.cli(h, "snapshot", sources=["a=/top/home:1d", "b=/top/home:1d"]), 2)
        self.assertEqual(self.cli(h, "snapshot", sources=["a=/top/home:1d", "a=/top/x:1d"]), 2)
        self.assertEqual(self.cli(h, "frobnicate"), 2)


class OwnershipTests(CliTestCase):
    def setup_mixed(self):
        h = self.host("2026-10-13 14:07", gen=10)
        old = self.add_history(h, "2026-06-01 00:00", "2026-06-01 03:00")   # due for deletion
        h.subvols["/top/home"]["gen"] = 10
        other = h.add_source("/top/other", 10)
        h.add_snapshot("home.20260601T0030", "/top/home", 900, ro=False)                 # writable
        h.add_snapshot("home.20260601T0045", "/top/home", 901, parent=other)              # other source
        h.add_snapshot("home.20260601T0050", "/top/home", 902, parent=None)
        h.subvols["/top/snapshots/home.20260601T0050"]["parent"] = None                  # no parent at all
        h.other["/top/snapshots/home.20260601T0055"] = "symlink"
        h.other["/top/snapshots/home.20260601T0056"] = "other"                            # plain directory
        h.add_snapshot("home.20261399T0000", "/top/home", 903)                            # invalid date
        h.add_snapshot("home.20260601T0057.bak", "/top/home", 904)                        # name mismatch
        h.add_snapshot("homeX20260601T0058", "/top/home", 905)
        for e in ("home.20261012T0900", "home.20261013T0900", "home.20261013T1000", "home.20261013T1100"):
            h.add_snapshot(e, "/top/home", 1000 + len(e))
        return h, old

    def test_only_owned_are_deleted(self):
        h, old = self.setup_mixed()
        before = set(h.snapshots())
        rc = self.cli(h, "snapshot", source="home=/top/home:24h", clock_ok=True)
        self.assertEqual(rc, 0, self.out)
        deleted = before - set(h.snapshots())
        self.assertEqual(deleted, set(old) | {"home.20261012T0900"})
        for e in ("home.20260601T0030", "home.20260601T0045", "home.20260601T0050", "home.20261399T0000",
                  "home.20260601T0057.bak", "homeX20260601T0058"):
            self.assertIn(e, h.snapshots())

    def test_list_shows_ignored(self):
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
        self.assertEqual({s["entry"] for s in data["snapshots"] if s["action"] == "delete"},
                         {"home.20260601T0000", "home.20260601T0100", "home.20260601T0200", "home.20260601T0300",
                          "home.20261012T0900"})
        self.assertEqual(h.mutating_calls(), [])
        self.assertEqual(self.cli(h, "list", source="home=/top/home:24h"), 0)
        self.assertIn("IGNORED home.20260601T0030", self.out)

    def test_unparsable_entry_blocks_deletions(self):
        h, old = self.setup_mixed()
        h.show_override["/top/snapshots/home.20260601T0300"] = "home.20260601T0300\n\tUUID: \t\tbroken\n"
        before = h.snapshots()
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:24h", clock_ok=True), 4)
        self.assertEqual(h.snapshots(), before)
        self.assertIn("could not be verified", self.out)


class SafeguardTests(CliTestCase):
    def test_three_newest_and_24h_kept(self):
        h = self.host("2026-10-13 14:07", gen=10)
        for e, cgen in (("home.20260101T0000", 11), ("home.20260102T0000", 12), ("home.20260103T0000", 13)):
            h.add_snapshot(e, "/top/home", cgen)
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertEqual(len(h.snapshots()), 3)
        self.cli(h, "list", source="home=/top/home:1h", clock_ok=True)
        self.assertIn("safeguard: one of the 3 newest", self.out)
        # Younger than 24 h: kept even when the policy drops it.
        for e, cgen in (("home.20261012T1500", 14), ("home.20261012T1600", 15), ("home.20261013T1000", 16),
                        ("home.20261013T1100", 17), ("home.20261013T1200", 18)):
            h.add_snapshot(e, "/top/home", cgen)
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertEqual(h.snapshots(), ["home.20261012T1500", "home.20261012T1600", "home.20261013T1000",
                                         "home.20261013T1100", "home.20261013T1200"])

    def test_pinned_kept(self):
        h = self.host("2026-10-13 14:07", gen=10)
        old = self.add_history(h, "2026-06-01 00:00", "2026-06-01 03:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        with open(self.pins, "w") as f:
            f.write(f"/top/snapshots/{old[1]}\n\n/top/snapshots/./{old[2]}\n")
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertEqual(sorted(set(old) & set(h.snapshots())), [old[1], old[2]])

    def test_unreadable_pin_file_blocks(self):
        h = self.host("2026-10-13 14:07", gen=10)
        self.add_history(h, "2026-06-01 00:00", "2026-06-01 03:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        os.mkdir(self.pins)   # reading a directory fails
        before = h.snapshots()
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 4)
        self.assertEqual(h.snapshots(), before)


class ClockGuardTests(CliTestCase):
    def history(self, now_text):
        h = self.host(now_text, gen=10)
        old = self.add_history(h, "2026-06-01 00:00", "2026-06-01 02:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        return h, old

    def recorded(self):
        """The recorded last successful run (epoch), or None."""
        try:
            with open(os.path.join(self.state, core.STATE_FILE)) as f:
                return int(f.read().split()[0])
        except FileNotFoundError:
            return None

    def test_ntp_unsynchronized_blocks_deletions_only(self):
        h, old = self.history("2026-10-13 14:07")
        h.ntp = "no"
        h.subvols["/top/home"]["gen"] = 10_000
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h"), 4)
        self.assertIn("home.20261013T1407", h.snapshots())                 # creation still happened
        self.assertIn("the name uses the unverified time", self.out)
        self.assertTrue(set(old) <= set(h.snapshots()))
        self.assertIn("not NTP-synchronized", self.out)
        self.assertIsNone(self.recorded())
        h.ntp = "yes"
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h"), 0)
        self.assertFalse(set(old) & set(h.snapshots()))

    def test_unsynchronized_run_is_not_recorded(self):
        h = self.host("2026-10-13 14:07")
        h.ntp = "no"
        self.assertEqual(self.cli(h, "snapshot"), 0)                       # nothing to refuse
        self.assertEqual(h.snapshots(), ["home.20261013T1407"])
        self.assertIsNone(self.recorded())
        self.assertIn("not recorded", self.out)
        h.ntp = "yes"
        h.now += 3600
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertEqual(self.recorded(), int(h.now))

    def test_clock_behind_newest_snapshot(self):
        # Unsynchronized: which one is wrong is unknown - neither create nor delete.
        h, old = self.history("2026-10-13 11:50")    # newest is 12:00: 10 minutes ahead
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

    def test_synchronized_clock_supersedes_a_future_name(self):
        # NTP-synchronized: the future name is the wrong one. A correctly named
        # snapshot becomes the newest by generation and deletions go ahead.
        h, old = self.history("2026-10-13 11:50")
        h.subvols["/top/home"]["gen"] = 10_000
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h"), 0, self.out)
        self.assertIn("home.20261013T1150", h.snapshots())
        self.assertIn("<4>btrfs-snapshot: home: the newest snapshot home.20261013T1200 is dated in the future",
                      self.out)
        self.assertIn("btrfs subvolume delete /top/snapshots/home.20261013T1200", self.out)
        self.assertFalse(set(old) & set(h.snapshots()))
        self.assertIn("home.20261013T1200", h.snapshots())                 # kept until its date passes

    def test_future_name_on_idle_source(self):
        # A run while the clock was two years ahead (unsynchronized, long up, so
        # trusted) left a future name; the source stays idle afterwards.
        h, old = self.history("2026-10-13 14:07")
        h.add_snapshot("home.20281013T1407", "/top/home", 900)
        h.subvols["/top/home"]["gen"] = 800
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h"), 0, self.out)
        self.assertIn("created home.20261013T1407 (newest snapshot home.20281013T1407 is dated in the future)",
                      self.out)
        self.assertFalse(set(old) & set(h.snapshots()))
        h.now += 3600
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h"), 0)
        self.assertIn("unchanged since home.20261013T1407", self.out)
        self.cli(h, "list", source="home=/top/home:1h")
        # btrbk's wording: a future date matches "<N>h" with a negative distance.
        self.assertIn("KEEP    home.20281013T1407               preserve hourly: first of hour, -17543 hours ago",
                      self.out)
        # Unsynchronized, nothing is created for that reason.
        h2, _ = self.history("2026-10-13 14:07")
        h2.add_snapshot("home.20281013T1407", "/top/home", 900)
        h2.subvols["/top/home"]["gen"] = 800
        h2.ntp = "no"
        self.assertEqual(self.cli(h2, "snapshot", source="home=/top/home:1h"), 4)
        self.assertNotIn("home.20261013T1407", h2.snapshots())

    def test_unsynchronized_far_ahead_right_after_boot_waits(self):
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

    def test_unsynchronized_after_the_grace_period_creates(self):
        # Offline machine, up for hours: the clock is all there is.
        h = self.host("2026-10-13 14:07", gen=10)
        h.add_snapshot("home.20261010T1000", "/top/home", 11)
        h.subvols["/top/home"]["gen"] = 10_000
        h.reboot(uptime=core.NTP_GRACE + 60)
        h.ntp = "no"
        self.assertEqual(self.cli(h, "snapshot"), 0, self.out)
        self.assertIn("home.20261013T1407", h.snapshots())
        self.assertIn("unverified time", self.out)

    def test_unsynchronized_clock_jump_in_the_same_boot(self):
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        h.now += 3600                                    # one real hour
        h.step_clock(3 * 86400)                          # and the clock jumps three days
        h.ntp = "no"
        h.subvols["/top/home"]["gen"] = 20_000
        self.assertEqual(self.cli(h, "snapshot"), 4)
        self.assertEqual(h.snapshots(), ["home.20261013T1407"])
        self.assertIn("moved 3 days further than the time elapsed since the last successful run", self.out)

    def test_clock_behind_last_run(self):
        h, old = self.history("2026-10-13 14:07")
        os.makedirs(self.state)
        with open(os.path.join(self.state, core.STATE_FILE), "w") as f:
            f.write(f"{int(h.now) + 3600}\n")         # the old one-number format is read too
        before = h.snapshots()
        h.subvols["/top/home"]["gen"] = 10_000
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h", clock_ok=True), 4)
        self.assertEqual(h.snapshots(), before)
        self.assertIn("behind the last successful run", self.out)
        h.now += 3600
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertEqual(self.recorded(), int(h.now))

    def test_state_never_moves_backwards(self):
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
        h.now = recorded - 60
        self.assertEqual(self.cli(h, "snapshot"), 0)
        self.assertNotIn("keeping that record", self.out)
        self.assertEqual(self.recorded(), recorded)

    def test_corrupt_state_blocks(self):
        h, old = self.history("2026-10-13 14:07")
        os.makedirs(self.state)
        for garbage in ("garbage", "123 not-a-uuid 5.0", f"123 {uuid_n(1)}"):
            with self.subTest(garbage=garbage):
                with open(os.path.join(self.state, core.STATE_FILE), "w") as f:
                    f.write(garbage)
                self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 4)
                self.assertTrue(set(old) <= set(h.snapshots()))
                self.assertIn("unreadable state", self.out)

    def test_state_records_the_boot(self):
        h = self.host("2026-10-13 14:07")
        self.assertEqual(self.cli(h, "snapshot"), 0)
        with open(os.path.join(self.state, core.STATE_FILE)) as f:
            epoch, boot_id, boottime = f.read().split()
        self.assertEqual((int(epoch), boot_id, float(boottime)), (int(h.now), h.boot_id, 10 * 86400.0))


class CircuitBreakerTests(CliTestCase):
    def build(self, n_old):
        h = self.host("2026-10-13 14:07", gen=10)
        old = self.add_history(h, "2026-06-01 00:00", f"2026-06-01 {n_old - 1:02d}:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        return h, old

    def steady_state(self, policy, end="2026-10-13 14:05", days=120, name="home", source="/top/home"):
        """A host whose snapshots are what hourly runs with POLICY left behind,
        the last one at END, and a successful run recorded right after it."""
        self.state = tempfile.mkdtemp(dir=self.tmp.name)     # fresh per call (subtests)
        h = self.host(end, gen=10)
        if source != "/top/home":
            h.add_source(source, 10)
        end_wall = W(end)
        walls = [end_wall - dt.timedelta(hours=k) for k in range(days * 24, -1, -1)]
        snaps = [core.Snap(entry=core.snapshot_name(name, w), wall=w) for w in walls]
        keep = [r.snap for r in core.plan(snaps, at(end, BERLIN), core.parse_policy(policy), BERLIN)
                if r.action == "keep"]
        for cgen, s in enumerate(keep, start=10):
            h.add_snapshot(s.entry, source, cgen)
        h.subvols[source]["gen"] = 9                     # idle (kernel >= 7.1)
        h.now += 60
        self.assertEqual(self.cli(h, "prune", source=f"{name}={source}:{policy}"), 0, self.out)
        self.assertIn("deleted 0", self.out)
        return h

    def test_limits(self):
        self.assertFalse(core.is_mass_delete(5, 10))
        self.assertTrue(core.is_mass_delete(6, 10))
        self.assertFalse(core.is_mass_delete(10, 100))
        self.assertTrue(core.is_mass_delete(11, 100))
        self.assertFalse(core.is_mass_delete(21, 210))
        self.assertTrue(core.is_mass_delete(22, 210))

    def test_five_pass(self):
        h, old = self.build(5)
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertFalse(set(old) & set(h.snapshots()))

    def test_six_trip_then_allowed(self):
        h, old = self.build(6)
        before = h.snapshots()
        self.assertEqual(self.cli(h, "snapshot", source="home=/top/home:1h", clock_ok=True), 4)
        self.assertEqual(h.snapshots(), before)
        self.assertIn("circuit breaker: 6 of 9", self.out)
        self.assertIn("no successful run is recorded", self.out)
        self.assertEqual(self.cli(h, "prune", "--allow-mass-delete", source="home=/top/home:1h",
                                  clock_ok=True), 0)
        self.assertEqual(h.snapshots(), ["home.20261013T1000", "home.20261013T1100", "home.20261013T1200"])

    def test_message_names_the_callers_commands(self):
        h, old = self.build(6)
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 4)
        self.assertIn("btrfs-snapshot-core prune --snapdir DIR", self.out)
        self.assertEqual(self.cli(h, "prune", "--list-hint", "btrfs-setup.sh snapshots list",
                                  "--confirm-hint", "btrfs-setup.sh snapshots prune --confirm",
                                  source="home=/top/home:1h", clock_ok=True), 4)
        self.assertIn("check with 'btrfs-setup.sh snapshots list', then delete them with "
                      "'btrfs-setup.sh snapshots prune --confirm'", self.out)

    def test_per_source(self):
        h, old = self.build(6)
        h.add_source("/top/ws", 10)
        rc = self.cli(h, "prune", sources=["home=/top/home:1h", "ws=/top/ws:1h"], clock_ok=True)
        self.assertEqual(rc, 4)
        self.assertIn("ws: deleted 0", self.out)

    def test_downtime_does_not_trip(self):
        # Off overnight, over a weekend, on vacation: whatever aged out meanwhile
        # is deleted at the first run back, although it is far over the limit.
        cases = (("48h 7d", "Workspace", "/top/ws", 6),
                 ("72h 7d 4w 3m", "home", "/top/home", 9),
                 ("72h 7d 4w 3m", "home", "/top/home", 12),
                 ("72h 7d 4w 3m", "home", "/top/home", 62),
                 ("48h 7d", "Workspace", "/top/ws", 9 * 24))
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

    def test_policy_shrink_trips(self):
        h = self.steady_state("72h 7d 4w 3m")
        before = h.snapshots()
        for _ in range(2):          # the trip does not record a successful run: it persists
            h.now += 3600
            self.assertEqual(self.cli(h, "prune", source="home=/top/home:24h 7d 4w 3m"), 4)
            self.assertEqual(h.snapshots(), before)
            self.assertIn("was the policy shortened?", self.out)
        self.assertEqual(self.cli(h, "prune", "--allow-mass-delete", source="home=/top/home:24h 7d 4w 3m"), 0)
        self.assertLess(len(h.snapshots()), len(before) - 40)

    def test_clock_jump_in_the_same_boot_trips(self):
        h = self.steady_state("72h 7d 4w 3m")
        before = h.snapshots()
        h.now += 1800
        h.step_clock(10 * 86400)                         # NTP-synchronized, yet ten days ahead
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

    def test_safeguarded_snapshots_age_out_quietly(self):
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

    def test_simulated_schedules_never_trip(self):
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
    def test_dry_run_changes_nothing(self):
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

    def test_dry_run_counts_the_new_snapshot(self):
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

    def test_lock_busy(self):
        h = self.host("2026-10-13 14:07")
        fd = os.open(self.lock, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.assertEqual(self.cli(h, "snapshot"), 3)
            self.assertEqual(h.snapshots(), [])
            self.assertEqual(self.cli(h, "prune"), 3)
            self.assertEqual(self.cli(h, "snapshot", "--no-lock"), 0)    # the caller holds it
            self.assertEqual(self.cli(h, "list"), 0)                      # list takes no lock
        finally:
            os.close(fd)
        self.assertEqual(self.cli(h, "snapshot", "--force"), 0)


class RecheckTests(CliTestCase):
    def build(self):
        h = self.host("2026-10-13 14:07", gen=10)
        old = self.add_history(h, "2026-06-01 00:00", "2026-06-01 02:00")
        for e in ("home.20261013T1000", "home.20261013T1100", "home.20261013T1200"):
            h.add_snapshot(e, "/top/home", 500 + int(e[-4:-2]))
        return h, old

    def test_replaced_before_relist(self):
        h, old = self.build()
        target = f"/top/snapshots/{old[0]}"

        def swap(host, n):
            if n == 2:   # the re-list right before deleting
                host.subvols[target]["uuid"] = uuid_n(999)
        h.on_listdir = swap
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertIn(old[0], h.snapshots())
        self.assertNotIn(old[1], h.snapshots())
        self.assertIn("no longer due", self.out)

    def test_replaced_between_deletions(self):
        h, old = self.build()

        def swap(host, path):
            if path.endswith(old[0]):
                host.subvols[f"/top/snapshots/{old[1]}"]["ro"] = False
        h.on_delete = swap
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 4)
        self.assertNotIn(old[0], h.snapshots())
        self.assertIn(old[1], h.snapshots())
        self.assertNotIn(old[2], h.snapshots())
        self.assertIn("no longer read-only", self.out)

    def test_pinned_between_plan_and_delete(self):
        h, old = self.build()

        def pin(host, path):
            if path.endswith(old[0]):
                with open(self.pins, "w") as f:
                    f.write(f"/top/snapshots/{old[2]}\n")
        h.on_delete = pin
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 0)
        self.assertIn(old[2], h.snapshots())
        self.assertNotIn(old[1], h.snapshots())
        self.assertIn("pinned meanwhile", self.out)

    def test_delete_failure_stops(self):
        h, old = self.build()

        def vanish(host, path):
            host.subvols.pop(path, None)     # the delete command then fails
        h.on_delete = vanish
        self.assertEqual(self.cli(h, "prune", source="home=/top/home:1h", clock_ok=True), 1)
        self.assertEqual(len([c for c in h.calls if c[:3] == ["btrfs", "subvolume", "delete"]]), 1)


class LatestTests(CliTestCase):
    def test_latest_by_generation(self):
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


if __name__ == "__main__":
    unittest.main()

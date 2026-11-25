# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the zswap numbers of btrfs/btrfs-stall-report (no root needed).

Daemons before version 7 read only the first page of /proc/vmstat, so their
samples hold 0 for zswpin, zswpout and zswpwb. The first sample of a v7
daemon holds the real counter since boot. A report window that spans the
upgrade must not add that whole counter as if zswap wrote it in a few
seconds. The records here are built in memory; nothing reads the real
/var/log/btrfs-stall-monitor or runs below.

StallWatchWaitTest runs btrfs/btrfs-stall-watch with a stand-in report.
ReportCommandsTest runs the report's commands (date, docker, podman, below)
as fakes, with only their folder on PATH.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_stall_report -v
"""

from __future__ import annotations

import contextlib
import copy
import gzip
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Protocol, cast, override
from unittest import mock

# Run directly (python3 -I -B btrfs/tests/test_stall_report.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from btrfs.stall_monitord import domain as md_domain
from btrfs.stall_report import adapters as rp_adapters
from btrfs.stall_report import cli as rp_cli
from btrfs.stall_report.domain import categories as rp_categories
from btrfs.stall_report.domain import records as rp_records
from btrfs.stall_report.domain import verdict as rp_verdict
from btrfs.stall_report.domain import window as rp_window
from tests.package_view import PackageView

sys.dont_write_bytecode = True

VM_KEYS = ["nr_dirty", "nr_writeback", "pswpin", "pswpout", "zswpin", "zswpout", "zswpwb"]
T0 = 1_700_000_000.0
BOOT_ZSWPWB = 6_837_407  # pages: the real counter since boot on the test machine


class _PatternProto(Protocol):
    pattern: str


class _StallMonitordModule(Protocol):
    SWAP_FRAME: _PatternProto
    MMAP_LOCK_FRAME: _PatternProto


class _StallReportModule(Protocol):
    PAGE: int
    UNITS: Mapping[str, str] | Sequence[str]
    STACK_RULES: Sequence[tuple[str, _PatternProto]]
    CATEGORIES: Sequence[tuple[str, str, str]]

    def analyse(
        self,
        recs: Sequence[Mapping[str, Any]],
        since: float,
        until: float,
        *,
        with_below: bool,
        verbose: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]: ...

    def print_report(self, res: Mapping[str, Any], meta: Mapping[str, Any], *, verbose: bool) -> None: ...

    def verdict(self, res: Mapping[str, Any], meta: Mapping[str, Any]) -> list[str]: ...

    def open_episode(
        self, samples: Sequence[Mapping[str, Any]], episodes: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any] | None: ...


def load() -> _StallReportModule:
    """The report's package as one namespace (the program is a thin entry over it)."""
    view = PackageView(rp_cli, rp_adapters, rp_window, rp_verdict, rp_categories, rp_records)
    return cast("_StallReportModule", cast("object", view))


def meta(ts: float, v: int) -> dict[str, Any]:
    return {"t": "meta", "ts": ts, "v": v, "vm_keys": VM_KEYS, "base_interval": 5.0, "kernel": "7.2.0", "disks": {}}


def sample(ts: float, pswpout: int, zswpwb: int) -> dict[str, Any]:
    zswpin = zswpout = zswpwb
    return {
        "t": "s",
        "ts": ts,
        "mono": ts - T0 + 100,
        "psi": {"io": [0, 0, 0, 0], "memory": [0, 0, 0, 0], "cpu": [0, 0, 0, 0]},
        "procs": [1, 0],
        "vm": [0, 0, 0, pswpout, zswpin, zswpout, zswpwb],
        "fs": {},
        "disk": {},
        "cg": {},
        "d": 0,
        "d_all": 0,
        "d_maint": 0,
    }


def old_run(n: int = 10) -> list[dict[str, Any]]:
    """A v6 daemon: zswap counters always 0, pswpout grows 10 pages per sample."""
    return [meta(T0, 6)] + [sample(T0 + 5 * i, 1000 + 10 * i, 0) for i in range(n)]


def new_run(start: float, n: int = 10, first: int = BOOT_ZSWPWB) -> list[dict[str, Any]]:
    """A v7 daemon: real zswpwb, 5 pages written back per sample."""
    return [meta(start, 7)] + [sample(start + 1 + 5 * i, 2000 + 10 * i, first + 5 * i) for i in range(n)]


class RecordTypesTest(unittest.TestCase):
    def test_fields_older_daemons_lack_stay_optional_at_run_time(self) -> None:
        # NotRequired holds at run time only without `from __future__ import
        # annotations` (a string annotation hides it from TypedDict).
        self.assertLessEqual({"d_mem", "d_maint", "vm", "mono"}, rp_records.SampleRecord.__optional_keys__)
        self.assertLessEqual({"max_d_mem", "max_d_all"}, rp_records.EpisodeRecord.__optional_keys__)
        self.assertIn("psi", rp_records.SampleRecord.__required_keys__)


class ByteFormatTest(unittest.TestCase):
    """fmt_bytes: MiB with commas below 10 GiB, GiB with one decimal from it. It is cst_core's
    format_bytes_gib_mib (since 5fa45a0), which compares the size of the count: a count at or
    below -10 GiB, which no figure of the report reaches, prints in GiB as the positive one does
    (the copy before compared the count itself and printed it in MiB)."""

    def test_mib_below_10_gib_and_gib_from_it_on_either_side_of_zero(self) -> None:
        self.assertEqual(rp_verdict.fmt_bytes(1500 * 2**20), "1,500 MiB")
        self.assertEqual(rp_verdict.fmt_bytes(11 * 2**30), "11.0 GiB")
        self.assertEqual(rp_verdict.fmt_bytes(-1500 * 2**20), "-1,500 MiB")
        self.assertEqual(rp_verdict.fmt_bytes(-11 * 2**30), "-11.0 GiB")


class ZswapAcrossUpgradeTest(unittest.TestCase):
    m: _StallReportModule

    @override
    def setUp(self) -> None:
        self.m = load()

    def analyse(self, recs: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], str]:
        """(res, meta) of analyse() and the printed report, from one analysis."""
        res, meta_ = self.m.analyse(recs, T0, max(r["ts"] for r in recs), with_below=False, verbose=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.print_report(res, meta_, verbose=False)
        return res, out.getvalue()

    def test_only_old_daemon_shows_na(self) -> None:
        res, text = self.analyse(old_run())
        self.assertIsNone(res["vm"]["zswap_writeback_bytes"])
        self.assertIsNone(res["vm"]["zswap_in_bytes"])
        self.assertIsNone(res["vm"]["zswap_out_bytes"])
        self.assertIsNone(res["vm"]["swap_out_bypass_bytes"])
        self.assertEqual(res["vm"]["swap_out_bytes"], 90 * self.m.PAGE)  # other counters still count
        self.assertIn("zswap in/out n/a (daemon < v7)   disk swap in/out 0 MiB / 0 MiB", text)

    def test_window_across_upgrade_counts_only_v7_pairs(self) -> None:
        old = old_run()
        recs = old + new_run(old[-1]["ts"] + 3)  # restart: 3 s gap, under GAP_MAX
        res, text = self.analyse(recs)
        # 9 pairs of the new daemon, 5 pages each; not the counter since boot.
        self.assertEqual(res["vm"]["zswap_writeback_bytes"], 45 * self.m.PAGE)
        self.assertEqual(res["vm"]["zswap_in_bytes"], 45 * self.m.PAGE)
        self.assertEqual(res["vm"]["zswap_out_bytes"], 45 * self.m.PAGE)
        # Bypass only from the v7 pairs: 90 pages swapped out, 45 of them written back by zswap.
        self.assertEqual(res["vm"]["swap_out_bypass_bytes"], 45 * self.m.PAGE)
        self.assertEqual(res["vm"]["zswap_counted_from"], old[-1]["ts"] + 3)
        self.assertRegex(text, r"zswap in/out 0 MiB / 0 MiB \(from \d\d:\d\d:\d\d\)   disk swap in/out")
        self.assertNotIn("GiB", text)

    def test_only_new_daemon_counts_normally(self) -> None:
        recs = new_run(T0)
        res, text = self.analyse(recs)
        self.assertEqual(res["vm"]["zswap_writeback_bytes"], 45 * self.m.PAGE)
        self.assertIsNone(res["vm"]["zswap_counted_from"])
        line = next(row for row in text.splitlines() if row.startswith("memory"))
        self.assertIn("zswap in/out 0 MiB / 0 MiB   disk swap in/out", line)
        self.assertNotIn("from", line)

    def test_new_daemon_counts_large_writeback(self) -> None:
        recs = [meta(T0, 7), sample(T0 + 1, 0, BOOT_ZSWPWB), sample(T0 + 6, 0, BOOT_ZSWPWB + 2560)]
        res, text = self.analyse(recs)
        self.assertEqual(res["vm"]["zswap_writeback_bytes"], 2560 * self.m.PAGE)  # 10 MiB
        self.assertIn("zswap in/out 10 MiB / 10 MiB", text)


# ---- general synthetic logs -------------------------------------------------

ROLES = {
    "nvme0n1": "btrfs:fedora",
    "nvme2n1": "btrfs:fedora",
    "dm-0": "btrfs:fedora",
    "dm-1": "btrfs:fedora",
    "nvme1n1": "swap",
    "dm-2": "swap",
}
DISK_N = 17  # fields per device in a sample, as in /proc/diskstats after the name


def meta_full(ts: float, v: int = 8) -> dict[str, Any]:
    return {"t": "meta", "ts": ts, "v": v, "vm_keys": VM_KEYS, "base_interval": 5.0, "kernel": "7.2.15", "disks": ROLES}


def disk_counters(i: int, rate: Mapping[str, int]) -> list[int]:
    """Cumulative diskstats fields after i intervals; rate = per-interval deltas by field name."""
    d = [0] * DISK_N
    for k, v in rate.items():
        d[
            [
                "rd_ios",
                "rd_merges",
                "rd_sectors",
                "rd_ticks",
                "wr_ios",
                "wr_merges",
                "wr_sectors",
                "wr_ticks",
                "inflight",
                "io_ticks",
                "time_in_queue",
                "dc_ios",
                "dc_merges",
                "dc_sectors",
                "dc_ticks",
                "fl_ios",
                "fl_ticks",
            ].index(k)
        ] = v * i
    return d


def full_sample(
    ts: float,
    i: int,
    *,
    io_pct: float = 0.0,
    dt: float = 5.0,
    disks: Mapping[str, Mapping[str, int]] | None = None,
    d: int = 0,
    d_mem: int | None = None,
    d_maint: int = 0,
    vm: Sequence[int] | None = None,
) -> dict[str, Any]:
    """One sample record. io_pct: IO "some" pressure of each interval; disks:
    {name: per-interval deltas}; d_mem None = a daemon before v8 (no "d_mem")."""
    rec: dict[str, Any] = {
        "t": "s",
        "ts": ts,
        "mono": ts - T0 + 100,
        "psi": {"io": [io_pct, int(io_pct / 100 * dt * 1e6 * i), 0.0, 0], "memory": [0, 0, 0, 0], "cpu": [0, 0, 0, 0]},
        "procs": [1, 0],
        "vm": vm or [0] * len(VM_KEYS),
        "fs": {},
        "disk": {n: disk_counters(i, r) for n, r in (disks or {}).items()},
        "cg": {},
        "d": d,
        "d_all": d + (d_mem or 0) + d_maint,
        "d_maint": d_maint,
    }
    if d_mem is not None:
        rec["d_mem"] = d_mem
    return rec


def episode(
    start: float,
    end: float,
    max_d: int,
    sigs: Sequence[tuple[str, int, str]],
    max_d_mem: int | None = None,
) -> dict[str, Any]:
    """sigs: [(sig, n, kind)]. max_d_mem None = a daemon before v8."""
    ep: dict[str, Any] = {
        "t": "ep",
        "ts": start,
        "start": start,
        "end": end,
        "reason": f"{max_d} tasks waiting on storage",
        "snapshots": int(end - start),
        "max_d": max_d,
        "max_d_all": max_d + (max_d_mem or 0),
        "peak": {"io_some_avg10": 1.0, "io_full_avg10": 1.0, "iowait_tasks": 0, "commit_ms": 50},
        "sigs": [
            {
                "sig": sig,
                "ctx": "",
                "n": n,
                "kind": kind,
                "comms": {"java": n},
                "cgroups": {"app.slice": n},
                "examples": [],
            }
            for sig, n, kind in sigs
        ],
    }
    if max_d_mem is not None:
        ep["max_d_mem"] = max_d_mem
    return ep


class ReportCase(unittest.TestCase):
    m: _StallReportModule

    @override
    def setUp(self) -> None:
        self.m = load()

    def run_report(
        self, recs: Sequence[Mapping[str, Any]], until: float | None = None
    ) -> tuple[dict[str, Any], list[str], str]:
        until = until or float(max(r.get("ts", 0) for r in recs))
        res, meta_ = self.m.analyse(recs, T0, until, with_below=False, verbose=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.print_report(res, meta_, verbose=False)
        return res, self.m.verdict(res, meta_), out.getvalue()


# This PC in quiet hours (2026-11-12..14): NVMe write await 6-8 ms on the btrfs
# members while the dm-crypt layer above, which btrfs waits for, answered in
# about 1.1 ms; IO pressure 1.1-1.3 %.
QUIET_NVME = {"rd_ios": 10, "rd_ticks": 2, "wr_ios": 100, "wr_ticks": 750, "io_ticks": 300, "time_in_queue": 800}
QUIET_DM = {"rd_ios": 10, "rd_ticks": 11, "wr_ios": 100, "wr_ticks": 110, "io_ticks": 200, "time_in_queue": 130}
# Really saturated: requests wait 15 ms at the dm-crypt layer, long queue.
BUSY_DM = {"rd_ios": 10, "rd_ticks": 150, "wr_ios": 100, "wr_ticks": 1500, "io_ticks": 5000, "time_in_queue": 50000}


class SaturationTest(ReportCase):
    """storage-saturated-verdict: judged at the dm-crypt layer, not at the NVMe."""

    def recs(self, nvme: Mapping[str, int], dm: Mapping[str, int]) -> list[dict[str, Any]]:
        disks = {"nvme0n1": nvme, "nvme2n1": nvme, "dm-0": dm, "dm-1": dm}
        recs = [meta_full(T0)] + [full_sample(T0 + 5 * i, i, io_pct=1.3, disks=disks, d_mem=0) for i in range(13)]
        # A fsync-heavy episode with 3 tasks, so the report reaches the disk verdict.
        recs.append(episode(T0 + 10, T0 + 30, 3, [("btrfs_sync_log", 30, "storage")], max_d_mem=0))
        return recs

    def test_quiet_hour_numbers_are_not_saturated(self) -> None:
        res, verdict, text = self.run_report(self.recs(QUIET_NVME, QUIET_DM))
        self.assertAlmostEqual(res["pressure"]["io_some_pct"], 1.3, places=1)
        line = next(row for row in verdict if row.startswith("btrfs disks"))
        self.assertNotIn("SATURATED", line)
        self.assertIn("NOT saturated", line)
        self.assertIn("dm-crypt layer (what btrfs waits for) await 1.1 ms (read 1.1, write 1.1)", line)
        # The NVMe figure is still shown, read and write apart, as information only.
        self.assertIn("NVMe below await 6.8 ms (read 0.2, write 7.5), information only", line)
        self.assertNotIn("encryption queueing", line)
        self.assertIn("(r 0.2 / w 7.5)", text)  # the disks section splits read and write too

    def test_saturated_dm_layer(self) -> None:
        _, verdict, _ = self.run_report(self.recs(QUIET_NVME, BUSY_DM))
        line = next(row for row in verdict if row.startswith("btrfs disks"))
        self.assertIn("look SATURATED", line)
        self.assertIn("await 15.0 ms (read 15.0, write 15.0)", line)
        self.assertIn("encryption queueing adds latency", line)

    def test_without_dm_crypt_uses_the_member_disks(self) -> None:
        recs = [meta_full(T0)] + [full_sample(T0 + 5 * i, i, disks={"nvme0n1": BUSY_DM}, d_mem=0) for i in range(13)]
        recs[0]["disks"] = {"nvme0n1": "btrfs:fedora"}
        recs.append(episode(T0 + 10, T0 + 30, 3, [("btrfs_sync_log", 30, "storage")], max_d_mem=0))
        _, verdict, _ = self.run_report(recs)
        line = next(row for row in verdict if row.startswith("btrfs disks"))
        self.assertIn("look SATURATED: member disks await 15.0 ms", line)
        self.assertNotIn("NVMe below", line)


class MemoryWaitsTest(ReportCase):
    """storage-memory-waits-as-storage: reclaim/zswap waits are not storage."""

    def test_v7_memory_episode_is_not_storage(self) -> None:
        # A JVM GC event recorded by daemon v7: 74 tasks in zswap and the mmap
        # lock, all tagged "storage" by that daemon, in samples and episode.
        recs = [meta_full(T0, 7)]
        recs += [full_sample(T0 + 5 * i, i, io_pct=1.1, d=2) for i in range(6)]
        recs += [full_sample(T0 + 30 + i, 6 + i, io_pct=1.1, dt=1.0, d=74) for i in range(20)]
        recs += [full_sample(T0 + 50 + 5 * i, 26 + i, io_pct=1.1, d=1) for i in range(6)]
        recs.append(
            episode(
                T0 + 30,
                T0 + 50,
                74,
                [
                    ("zswap_compress", 900, "storage"),
                    ("zswap_decompress", 300, "storage"),
                    ("lock_mm_and_find_vma", 250, "storage"),
                    ("__vma_start_exclude_readers", 30, "storage"),
                ],
            )
        )
        res, verdict, text = self.run_report(recs)
        self.assertEqual(res["episodes"][0]["max_d"], 0)
        self.assertEqual(res["episodes"][0]["max_d_mem"], 74)
        self.assertEqual(res["peak"]["d_tasks"], 2)  # samples inside the episode are capped
        self.assertEqual(res["peak"]["mem_tasks"], 74)
        self.assertTrue(verdict[0].startswith("Storage was NOT a bottleneck"), verdict)
        self.assertIn("at most 2 task(s) waiting on storage", verdict[0])
        self.assertTrue(
            any(
                row.startswith(
                    "Memory, not storage, blocked up to 74 tasks (memory reclaim, swap-in or zswap; "
                    "estimated from the stacks: recorded by a daemon < v8)"
                )
                for row in verdict
            )
        )
        self.assertFalse(any(row.startswith("Blocked tasks") for row in verdict))
        self.assertEqual(res["categories"], [])
        self.assertEqual(res["non_storage_waits"], {"reclaim": 1480})
        self.assertIn("[reclaim] zswap_compress", text)

    def test_v7_mixed_episode_keeps_the_storage_part(self) -> None:
        # The episode opens at the first sample with 3+ tasks (here the first sample).
        recs = [meta_full(T0, 7)] + [full_sample(T0 + i, i, dt=1.0, d=8) for i in range(30)]
        recs.append(episode(T0, T0 + 29, 8, [("btrfs_sync_log", 150, "storage"), ("zswap_compress", 50, "storage")]))
        res, verdict, _ = self.run_report(recs)
        self.assertEqual(res["episodes"][0]["max_d"], 6)
        self.assertEqual(res["episodes"][0]["max_d_mem"], 2)
        self.assertTrue(
            any(row.startswith("Blocked tasks: up to 6 tasks waiting on storage") for row in verdict), verdict
        )
        self.assertFalse(any(row.startswith("Memory") for row in verdict))
        self.assertEqual([c["category"] for c in res["categories"]], ["log-sync"])

    def test_v7_storage_episode_unchanged(self) -> None:
        recs = [meta_full(T0, 7)] + [full_sample(T0 + i, i, dt=1.0, d=5) for i in range(30)]
        recs.append(episode(T0 + 2, T0 + 29, 5, [("btrfs_sync_log", 100, "storage")]))
        res, verdict, _ = self.run_report(recs)
        self.assertEqual(res["episodes"][0]["max_d"], 5)
        self.assertNotIn("estimated", res["episodes"][0])
        self.assertEqual(res["peak"]["d_tasks"], 5)
        self.assertTrue(any(row.startswith("Blocked tasks: up to 5 tasks") for row in verdict))

    def test_v8_memory_waits_from_samples(self) -> None:
        # Daemon v8: memory waits counted in d_mem; they open no episode.
        recs = [meta_full(T0)] + [
            full_sample(T0 + 5 * i, i, io_pct=1.0, d=1, d_mem=40 if 5 <= i <= 7 else 0) for i in range(13)
        ]
        res, verdict, text = self.run_report(recs)
        self.assertEqual(res["peak"]["d_tasks"], 1)
        self.assertEqual(res["peak"]["mem_tasks"], 40)
        self.assertTrue(verdict[0].startswith("Storage was NOT a bottleneck"))
        self.assertIn(
            "Memory, not storage, blocked up to 40 tasks (memory reclaim, swap-in or zswap). See the memory line",
            " ".join(verdict),
        )
        self.assertIn("tasks waiting on storage 1  on memory 40", text)

    def test_v8_episode_memory_sigs_are_not_storage(self) -> None:
        recs = [meta_full(T0)] + [full_sample(T0 + i, i, dt=1.0, d=4, d_mem=10) for i in range(30)]
        recs.append(
            episode(
                T0 + 2,
                T0 + 29,
                4,
                [("btrfs_sync_log", 100, "storage"), ("zswap_compress", 250, "memory")],
                max_d_mem=10,
            )
        )
        res, verdict, _ = self.run_report(recs)
        self.assertEqual(res["episodes"][0]["max_d"], 4)  # v8 counts are used as recorded
        self.assertEqual([c["category"] for c in res["categories"]], ["log-sync"])
        self.assertEqual(res["non_storage_waits"], {"reclaim": 250})
        joined = " | ".join(verdict)
        self.assertIn("Memory, not storage, blocked up to 10 tasks (memory", joined)
        self.assertIn("Blocked tasks: also up to 4 tasks waiting on storage", joined)
        self.assertNotIn("btrfs commit/log/lock waits", joined)

    def test_v8_hung_memory_event_is_not_a_disk_problem(self) -> None:
        recs = [meta_full(T0)] + [full_sample(T0 + 5 * i, i, d_mem=1) for i in range(13)]
        recs.append(
            {
                "t": "ev",
                "ts": T0 + 40,
                "kind": "hung-memory",
                "severe": False,
                "ours": True,
                "msg": "java:123 (java; cgroup app.slice) waited 31s without progress in memory reclaim",
            }
        )
        _, verdict, _ = self.run_report(recs)
        joined = " | ".join(verdict)
        self.assertNotIn("KERNEL/FS PROBLEMS", joined)
        self.assertIn("1 task(s) stuck 30+ s, see 'events'", joined)


class OpenEpisodeTest(ReportCase):
    """storage-open-episode-verdict: an episode without a record yet."""

    def recs(self, v8: bool = True) -> list[dict[str, Any]]:
        recs = [meta_full(T0, 8 if v8 else 7)]
        recs += [full_sample(T0 + i, i, io_pct=1.0, dt=1.0, d=20, d_mem=0 if v8 else None) for i in range(195)]
        return recs

    def check_open(self, recs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        res, verdict, _ = self.run_report(recs)
        joined = " | ".join(verdict)
        self.assertNotIn("Storage was NOT a bottleneck", joined)
        self.assertIn("Blocked tasks: up to 20 tasks waiting on storage", joined)
        self.assertIn("Episode still open: since", joined)
        self.assertEqual(res["open_episode"]["d_tasks"], 20)
        return res

    def test_v8_open_episode(self) -> None:
        self.check_open(self.recs())

    def test_v7_open_episode(self) -> None:
        self.check_open(self.recs(v8=False))

    def test_closed_episode_is_not_open(self) -> None:
        recs = self.recs()
        recs.append(episode(T0, T0 + 194, 20, [("btrfs_sync_log", 3900, "storage")], max_d_mem=0))
        res, verdict, _ = self.run_report(recs)
        self.assertIsNone(res["open_episode"])
        self.assertNotIn("Episode still open", " | ".join(verdict))
        self.assertIn("Blocked tasks: up to 20 tasks", " | ".join(verdict))

    def test_three_storage_waits_open_an_episode_two_do_not(self) -> None:
        # The daemon opens an episode at 3 storage waits; the report uses the
        # same threshold for one it has not written yet: exactly 3 counts,
        # exactly 2 does not.
        for d, want in ((3, 3), (2, None)):
            with self.subTest(d=d):
                recs = [meta_full(T0)] + [full_sample(T0 + i, i, dt=1.0, d=d, d_mem=0) for i in range(60)]
                res, verdict, _ = self.run_report(recs)
                got = res["open_episode"]
                self.assertEqual(None if got is None else got["d_tasks"], want)
                self.assertEqual("Episode still open" in " | ".join(verdict), want is not None)

    def test_quiet_one_second_samples_are_not_an_episode(self) -> None:
        recs = [meta_full(T0)] + [full_sample(T0 + i, i, dt=1.0, d=1, d_mem=0) for i in range(60)]
        res, verdict, _ = self.run_report(recs)
        self.assertIsNone(res["open_episode"])
        self.assertTrue(verdict[0].startswith("Storage was NOT a bottleneck"))


def waits(ts: float, d: int, v5: bool = True) -> dict[str, Any]:
    """A sample as open_episode reads it: its time, its storage waits and,
    from a daemon >= v5, d_maint."""
    return {"ts": ts, "d": d, "d_maint": 0} if v5 else {"ts": ts, "d": d}


class OpenEpisodeFunctionTest(unittest.TestCase):
    """open_episode called directly, on its own boundaries.

    Every timestamp is T0 plus a multiple of 0.5 s, so the gaps and the
    window edges below are exact in floating point."""

    m: ClassVar[_StallReportModule]

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.m = load()

    def open_episode(
        self, samples: Sequence[Mapping[str, Any]], episodes: Sequence[Mapping[str, Any]] = ()
    ) -> dict[str, Any] | None:
        return self.m.open_episode(samples, list(episodes))

    def test_two_samples_one_second_apart_are_an_open_episode(self) -> None:
        got = self.open_episode([waits(T0, 3), waits(T0 + 1, 5)])
        self.assertEqual(got, {"since": T0, "last": T0 + 1, "d_tasks": 5})

    def test_fewer_than_two_samples_are_not_an_episode(self) -> None:
        self.assertIsNone(self.open_episode([]))
        self.assertIsNone(self.open_episode([waits(T0, 9)]))

    def test_since_last_and_peak_come_from_the_whole_run(self) -> None:
        # since = the oldest sample of the run, last = the newest one, d_tasks =
        # the most storage waits in any sample of the run, wherever it is.
        for where, ds in (("newest", (4, 3, 5, 7)), ("middle", (4, 7, 5, 3))):
            with self.subTest(peak=where):
                samples = [waits(T0 + i, d) for i, d in enumerate(ds)]
                self.assertEqual(self.open_episode(samples), {"since": T0, "last": T0 + 3, "d_tasks": 7})

    def test_newest_sample_without_d_maint_is_not_an_episode(self) -> None:
        # A sample of a daemon < v5 (no d_maint) does not count storage waits
        # apart, even when the older samples before it do.
        samples = [waits(T0, 9), waits(T0 + 1, 9), waits(T0 + 2, 9, v5=False)]
        self.assertIsNone(self.open_episode(samples))

    def test_newest_sample_alone_after_a_gap_is_not_an_episode(self) -> None:
        samples = [waits(T0, 9), waits(T0 + 1, 9), waits(T0 + 3, 9)]
        self.assertIsNone(self.open_episode(samples))

    def test_gap_of_more_than_1_5_s_ends_the_run(self) -> None:
        """Pins current behaviour: a gap of exactly 1.5 s keeps the samples in one
        run, 2 s splits it (neither the docstring nor btrfs/README.md states the
        1.5 s limit). A busy sample before the gap does not count."""
        on_edge = [waits(T0, 4), waits(T0 + 1.5, 4), waits(T0 + 2.5, 4)]
        self.assertEqual(self.open_episode(on_edge), {"since": T0, "last": T0 + 2.5, "d_tasks": 4})
        past_edge = [waits(T0, 9), waits(T0 + 2, 4), waits(T0 + 3, 4)]
        self.assertEqual(self.open_episode(past_edge), {"since": T0 + 2, "last": T0 + 3, "d_tasks": 4})

    def test_episode_record_covers_the_newest_sample_within_one_second(self) -> None:
        """Pins current behaviour: an episode record from start - 1 s to end + 1 s
        (both edges included) covers the newest sample; 1.5 s outside it does not
        (the docstring says "no episode record covers them" without a margin)."""
        start, end = T0 + 100, T0 + 110
        episodes = [{"start": T0 - 1000, "end": T0 - 990}, {"start": start, "end": end}]
        for last, covered in (
            (start - 1.5, False),
            (start - 1, True),
            (start + 5, True),
            (end + 1, True),
            (end + 1.5, False),
        ):
            with self.subTest(last=last - T0):
                got = self.open_episode([waits(last - 1, 5), waits(last, 5)], episodes)
                self.assertEqual(got, None if covered else {"since": last - 1, "last": last, "d_tasks": 5})


class ZswapTrafficTest(ReportCase):
    """memory-1: zswap pool traffic, and disk swap-out shown once."""

    @staticmethod
    def vm(i: int, zin: int, zout: int, pout: int, zwb: int) -> list[int]:
        # VM_KEYS: nr_dirty, nr_writeback, pswpin, pswpout, zswpin, zswpout, zswpwb
        return [0, 0, 0, pout * i, zin * i, zout * i, zwb * i]

    def test_pool_traffic_and_disk_swap(self) -> None:
        # Per 5 s: 50 MiB decompressed, 100 MiB compressed, 10 MiB written to
        # the swap disk, all of it zswap writeback (pswpout == zswpwb here).
        recs = [meta_full(T0)] + [
            full_sample(T0 + 5 * i, i, d_mem=0, vm=self.vm(i, 12800, 25600, 2560, 2560)) for i in range(2)
        ]
        res, _, text = self.run_report(recs)
        self.assertEqual(res["vm"]["zswap_in_bytes"], 50 * 2**20)
        self.assertEqual(res["vm"]["zswap_out_bytes"], 100 * 2**20)
        self.assertEqual(res["vm"]["swap_out_bytes"], 10 * 2**20)
        self.assertEqual(res["vm"]["swap_out_bypass_bytes"], 0)
        line = next(row for row in text.splitlines() if row.startswith("memory"))
        self.assertIn("zswap in/out 50 MiB / 100 MiB   disk swap in/out 0 MiB / 10 MiB   direct reclaim", line)
        self.assertNotIn("not through zswap", line)
        self.assertNotIn("writeback", line)  # the old duplicate figure is gone

    def test_swap_out_that_bypassed_zswap(self) -> None:
        recs = [meta_full(T0)] + [
            full_sample(T0 + 5 * i, i, d_mem=0, vm=self.vm(i, 0, 2560, 5120, 2560)) for i in range(2)
        ]
        res, _, text = self.run_report(recs)
        self.assertEqual(res["vm"]["swap_out_bypass_bytes"], 10 * 2**20)
        self.assertIn("disk swap in/out 0 MiB / 20 MiB (10 MiB of it not through zswap)", text)

    def test_json_units_document_the_new_keys(self) -> None:
        units = " ".join(self.m.UNITS)
        for key in (
            "zswap_in_bytes",
            "zswap_out_bytes",
            "swap_out_bypass_bytes",
            "mem_tasks",
            "read_await_ms",
            "write_await_ms",
            "sample_d_tasks",
            "open_episode",
            "max_d_mem",
            "estimated",
        ):
            self.assertIn(key, units)


class FrameListsTest(unittest.TestCase):
    """The report re-classifies old logs with the daemon's memory frames: it
    uses the daemon's lists (btrfs.stall_monitord.domain), not copies."""

    def test_report_and_daemon_agree(self) -> None:
        report = load()
        daemon = cast("_StallMonitordModule", cast("object", md_domain))
        self.assertEqual(dict(report.STACK_RULES)["reclaim"].pattern, daemon.SWAP_FRAME.pattern)
        reclaim = next(rx for k, rx, _ in report.CATEGORIES if k == "reclaim")
        self.assertTrue(reclaim.endswith("|" + daemon.MMAP_LOCK_FRAME.pattern), reclaim)


class VerdictLinesTest(unittest.TestCase):
    """verdict()'s lines for each way a window can go, on a quiet window's result with fields changed."""

    m: _StallReportModule

    @override
    def setUp(self) -> None:
        self.m = load()
        recs = old_run()
        self.res, self.meta = self.m.analyse(recs, T0, max(r["ts"] for r in recs), with_below=False, verbose=False)

    def verdict(
        self,
        top: Mapping[str, object] | None = None,
        meta: Mapping[str, object] | None = None,
        **parts: Mapping[str, object],
    ) -> list[str]:
        """The verdict with the top-level keys TOP set, each part's keys updated, and META's keys set."""
        res, meta_ = copy.deepcopy(self.res), {**self.meta, **(meta or {})}
        res.update(top or {})
        for key, changes in parts.items():
            res[key].update(changes)
        return self.m.verdict(res, meta_)

    @staticmethod
    def at(offset: float) -> str:
        return time.strftime("%H:%M:%S", time.localtime(T0 + offset))

    def test_no_data_and_a_quiet_window(self) -> None:
        self.assertEqual(
            self.verdict({"samples": 1}, coverage={"skipped": 2}),
            [
                (
                    "NO DATA for this window. Is btrfs-stall-monitor.service running? (`systemctl status "
                    "btrfs-stall-monitor`; inside toolbox prefix with flatpak-spawn --host)"
                ),
                "INCOMPLETE DATA: 2 damaged record(s) in this window were left out (re-running does not help)",
            ],
        )
        self.assertEqual(
            self.verdict(),
            [
                (
                    "Storage was NOT a bottleneck: IO pressure 0.0%, at most 0 task(s) waiting on storage, longest "
                    "btrfs commit 0 ms (written 0 MiB)."
                ),
                "Little activity recorded: check that the window really covers the workload.",
            ],
        )

    def test_a_quiet_window_with_problems(self) -> None:
        got = self.verdict(
            {
                "events": [{"t": "ev", "ts": T0 + 3, "kind": "hung", "severe": True, "msg": "x"}],
                "fs": {"/": {"new_device_errors": 1, "max_commit_ms": 200, "max_at": T0 + 10}},
            },
            coverage={"complete": False, "covered_s": 30, "last_sample": T0 + 30, "skipped": 1},
            pressure={"cpu_some_pct": 25.0, "mem_some_pct": 6.0},
        )
        self.assertEqual(
            got,
            [
                (
                    f"INCOMPLETE DATA: 30 s of the 45 s window recorded, last sample {self.at(30)}. Data is written "
                    "every ~10 s; re-run shortly, or narrow the window."
                ),
                "INCOMPLETE DATA: 1 damaged record(s) in this window were left out (re-running does not help)",
                "KERNEL/FS PROBLEMS: 1 severe event(s) (hung task, btrfs or device error); see 'events'.",
                "btrfs device error counters INCREASED in this window.",
                (
                    "Storage was NOT a bottleneck: IO pressure 0.0%, at most 0 task(s) waiting on storage, longest "
                    "btrfs commit 200 ms (written 0 MiB)."
                ),
                "CPU contention: runnable tasks waited for a CPU 25% of the time.",
                "Memory pressure 6.0%: see the memory line.",
            ],
        )

    def test_a_storage_stall(self) -> None:
        cats = [
            {
                "category": "zstd-workspace",
                "description": "waiting for a btrfs zstd compression workspace",
                "share": 0.6,
            },
            {"category": "commit", "description": "btrfs transaction commit", "share": 0.3},
            {"category": "flush", "description": "disk cache flushes", "share": 0.06},
            {"category": "wbt", "description": "block-layer writeback throttling (WBT)", "share": 0.04},
        ]
        top = {
            "episodes": [
                {"start": T0 + 5, "end": T0 + 25, "max_d": 4, "peak": {"commit_ms": 2500}, "sigs": [], "cats": {}}
            ],
            "open_episode": {"since": T0 + 30, "last": T0 + 40, "d_tasks": 3},
            "fs": {"/": {"new_device_errors": 0, "max_commit_ms": 2500, "max_at": T0 + 12}},
            "categories": cats,
        }
        parts = {
            "pressure": {"io_some_pct": 5.0, "io_full_pct": 1.0},
            "peak": {"io_some_avg10": 20.0},
            "vm": {"swap_out_bytes": 300 * 2**20},
        }
        head = [
            "IO wait: tasks waited on IO 5.0% of the time (every non-idle task at once 1.0%), peak 10 s average 20%.",
            (
                "Blocked tasks: up to 4 tasks waiting on storage in D state (btrfs commit/log/lock waits block tasks "
                "without showing up as IO pressure)."
            ),
            (
                f"Episode still open: since {self.at(30)} the monitor samples every second and up to 3 tasks wait on "
                "storage. Its record (stacks, categories) is written when it ends; run the report again after that "
                "for the details."
            ),
            f"Slow btrfs commits: longest 2.5 s at {self.at(12)}.",
            (
                "Where blocked tasks waited (share of sampled D-state tasks): waiting for a btrfs zstd compression "
                "workspace 60%, btrfs transaction commit 30%, disk cache flushes 6%."
            ),
        ]
        tail = [
            f"Worst episode: {self.at(5)}-{self.at(25)} (20 s, 4 D tasks, commit 2500 ms).",
            "Swap-out of 300 MiB: memory pressure also loaded the swap disk.",
        ]
        self.assertEqual(
            self.verdict(top, {"kernel": "7.1.0", "stacks": False}, **parts),
            [
                *head,
                "Matches the btrfs zstd lost-wakeup bug fixed in 7.2.6; a kernel update fixes it.",
                "Caveat: the daemon could not read kernel stacks, so attribution is coarse (wchan only).",
                *tail,
            ],
        )
        self.assertEqual(
            self.verdict(top, {"kernel": "7.2.6"}, **parts),
            [
                *head,
                (
                    "zstd workspace waits on a kernel that already has the 7.2.6 lost-wakeup fix; may be plain "
                    "compression contention."
                ),
                *tail,
            ],
        )
        self.assertEqual(
            self.verdict(
                {"fs": {"/": {"new_device_errors": 0, "max_commit_ms": 1500, "max_at": 0}}, "categories": cats[1:]}
            ),
            [
                "Slow btrfs commits: longest 1.5 s.",
                (
                    "Where blocked tasks waited (share of sampled D-state tasks): btrfs transaction commit 30%, disk "
                    "cache flushes 6%, block-layer writeback throttling (WBT) 4%."
                ),
                rp_categories.HINTS["commit"],
            ],
        )
        self.assertEqual(
            self.verdict({"sample_d_tasks": 5, "categories": cats[3:]}),
            [
                (
                    "Blocked tasks: up to 5 tasks waiting on storage in D state (btrfs commit/log/lock waits block tasks "
                    "without showing up as IO pressure)."
                ),
                "Where blocked tasks waited (share of sampled D-state tasks): block-layer writeback throttling (WBT) 4%.",
                rp_categories.HINTS["wbt"],
            ],
        )

    def test_memory_lines_in_a_stall(self) -> None:
        ev = {"events": [{"t": "ev", "ts": T0 + 3, "kind": "hung-memory", "msg": "m"}]}
        memory = (
            "Memory, not storage, blocked up to 9 tasks (memory reclaim, swap-in or zswap; 1 task(s) stuck 30+ s, "
            "see 'events'). See the memory line; `cst swap status` shows which cgroup swaps."
        )
        self.assertEqual(
            self.verdict(ev, pressure={"io_some_pct": 5.0, "io_full_pct": 1.0}, peak={"mem_tasks": 9}),
            [
                "IO wait: tasks waited on IO 5.0% of the time (every non-idle task at once 1.0%), peak 10 s average 0%.",
                memory,
            ],
        )
        self.assertEqual(
            self.verdict({**ev, "sample_d_tasks": 3}, peak={"mem_tasks": 9}),
            [memory, "Blocked tasks: also up to 3 tasks waiting on storage in D state."],
        )


def rich_result() -> dict[str, object]:
    """A result with every section of the text report: two filesystems, disks of each role, slices,
    containers and more cgroups than shown, below's lists, more episodes and events than shown, alerts."""

    def disk(role: str, aw: float, wb: int) -> dict[str, object]:
        return {
            "role": role,
            "await_ms": aw,
            "read_await_ms": aw / 2,
            "write_await_ms": aw * 2,
            "flush_await_ms": 1.5,
            "fl_ios": 9,
            "queue_depth": 2.5,
            "read_bytes": 100 * 2**20,
            "write_bytes": wb,
            "rd_ios": 1,
            "wr_ios": 2,
            "rd_ticks": 3,
            "wr_ticks": 4,
            "rd_sectors": 5,
            "wr_sectors": 6,
            "io_ticks": 7,
            "time_in_queue": 8,
            "fl_ticks": 10,
            "peak_util_pct": 50.0,
            "peak_at": T0 + 3,
            "util_pct": 20.0,
        }

    def cgroup(io: float, wb: int) -> dict[str, object]:
        return {
            "io_some": io * 10,
            "io_full": io,
            "io_some_pct": io,
            "io_full_pct": io / 2,
            "read_bytes": 5 * 2**20,
            "write_bytes": wb,
        }

    def sig(i: int, ctx: str) -> dict[str, object]:
        return {
            "n": i + 1,
            "cat": "commit",
            "sig": f"fn{i}",
            "ctx": ctx,
            "comms": {"a": 3, "b": 2, "c": 1, "d": 1, "e": 1},
            "cgroups": {"x.slice": 1, "y.slice": 1, "z.slice": 1, "w.slice": 1},
            "examples": [[f"f{j}" for j in range(12)], ["g"]],
        }

    def episode(i: int, sigs: int) -> dict[str, object]:
        return {
            "start": T0 + i,
            "end": T0 + i + 20,
            "max_d": 4 + i,
            "reason": f"psi {i}",
            "peak": {"io_full_avg10": 12.0 + i, "commit_ms": 100 * i},
            "sigs": [sig(j, "via x" if j % 2 else "") for j in range(sigs)],
            "cats": {},
        }

    names = [
        "user.slice",
        "system.slice",
        "docker-0123456789ab",
        "libpod-fedcba987654",
        "app.scope",
        "a.service",
        "b.service",
        "c.service",
        "d.service",
        "e.service",
        "idle.service",
    ]
    loads = [
        (30.0, 0),
        (2.0, 0),
        (5.0, 2**30),
        (0.5, 100 * 2**20),
        (2.0, 0),
        (3.0, 0),
        (4.0, 0),
        (6.0, 0),
        (7.0, 0),
        (8.0, 0),
        (0.5, 10),
    ]
    return {
        "window": {"since": T0, "until": T0 + 60},
        "kernel": "7.2.0",
        "samples": 12,
        "coverage": {
            "window_s": 60,
            "covered_s": 55,
            "first_sample": T0 + 1,
            "last_sample": T0 + 56,
            "complete": True,
            "skipped": 0,
            "gaps": 2,
        },
        "pressure": {
            "io_some_pct": 1.25,
            "io_full_pct": 0.5,
            "mem_some_pct": 0.75,
            "mem_full_pct": 0.25,
            "cpu_some_pct": 2.5,
        },
        "peak": {
            "io_some_avg10": 9.0,
            "io_full_avg10": 4.0,
            "mem_some_avg10": 0.0,
            "cpu_some_avg10": 0.0,
            "iowait_tasks": 2,
            "d_tasks": 2,
            "mem_tasks": 1,
            "maint_tasks": 0,
            "dirty_bytes": 3 * 2**20,
            "writeback_bytes": 2**20,
        },
        "maintenance_samples": 0,
        "vm": {
            "swap_in_bytes": 2**20,
            "swap_out_bytes": 2 * 2**20,
            "zswap_in_bytes": None,
            "zswap_out_bytes": None,
            "zswap_writeback_bytes": None,
            "swap_out_bypass_bytes": None,
            "zswap_counted_from": None,
            "direct_reclaim_scanned_pages": 12345,
            "allocstalls": 1234,
            "dirty_limit_bytes": 64 * 2**20,
        },
        "fs": {
            "aaaaaaaa-1-2-3-4": {
                "commits": 4,
                "avg_commit_ms": 33.3,
                "max_commit_ms": 1200,
                "max_at": T0 + 4,
                "new_device_errors": 0,
                "space": {"metadata": [5, 20, 0, 0, 0]},
            },
            "bbbbbbbbbb-1-2-3-4": {
                "commits": 0,
                "avg_commit_ms": 0.0,
                "max_commit_ms": 0,
                "max_at": None,
                "new_device_errors": 1,
            },
        },
        "disks": {
            "nvme0n1": disk("btrfs:data", 1.0, 3 * 2**30),
            "dm-0": disk("btrfs:data", 6.0, 3 * 2**30),
            "dm-1": disk("swap", 2.0, 0),
            "sda": disk("", 0.0, 0),
        },
        "cgroups": {n: cgroup(io, wb) for n, (io, wb) in zip(names, loads, strict=True)},
        "below": {
            "error": "below dump failed: partial",
            "top_writers": [
                {"comm": f"w{i}", "cgroup": "" if i % 2 else "g.service", "bytes": 2**20 * i} for i in range(8)
            ],
            "top_readers": [{"comm": "r", "cgroup": "q", "bytes": 5}],
        },
        "categories": [],
        "non_storage_waits": {},
        "sample_d_tasks": 0,
        "open_episode": None,
        "v6_since": T0,
        "episodes": [episode(i, s) for i, s in enumerate([9, 2, 0, 1, 0, 0, 0])],
        "events": [{"t": "ev", "ts": T0 + i, "kind": "x", "severe": i % 3 == 0, "msg": f"event {i}"} for i in range(10)]
        + [{"t": "ev", "ts": T0 + 40, "kind": "x"}],
        "alerts": [{"t": "al", "ts": T0 + 2, "title": "Disk", "body": "slow"}, {"t": "al", "ts": T0 + 3}],
    }


class ReportTextTest(unittest.TestCase):
    """The whole text report of a result with every section, verbose and not: the window, the
    verdict, pressure and peaks, the filesystems, the disks (dm-crypt rows only when verbose), memory,
    the cgroups (slices first, at most 6 others, the container names), below's error and lists, the
    episodes and their signatures (more of both when verbose, with a stack), the events and alerts."""

    def report(self, *, verbose: bool) -> list[str]:
        meta_ = {"kernel": "7.2.0", "fs": {"aaaaaaaa-1-2-3-4": {"label": "data"}}}
        out = io.StringIO()
        with (
            mock.patch.object(rp_cli, "container_names", return_value={"docker-0123456789ab": "web"}),
            contextlib.redirect_stdout(out),
        ):
            load().print_report(rich_result(), meta_, verbose=verbose)
        return out.getvalue().split("\n")[:-1]

    def test_the_lines_before_the_cgroups_are_printed_before_the_container_names_are_asked(self) -> None:
        # docker ps and podman ps can take seconds: the window, the verdict and the lines up to memory
        # are out by then (on a terminal they show at once), as before the report's lines moved to report.py.
        meta_ = {"kernel": "7.2.0", "fs": {"aaaaaaaa-1-2-3-4": {"label": "data"}}}
        out = io.StringIO()
        printed: list[list[str]] = []

        def names() -> dict[str, str]:
            printed.append(out.getvalue().split("\n"))
            return {}

        with mock.patch.object(rp_cli, "container_names", names), contextlib.redirect_stdout(out):
            load().print_report(rich_result(), meta_, verbose=False)
        [lines] = printed
        self.assertTrue(lines[0].startswith("window   "), lines)
        self.assertTrue(lines[-2].startswith("memory   "), lines)
        self.assertEqual(lines[-1], "")

    def test_every_section(self) -> None:
        def at(offset: int) -> str:
            return time.strftime("%H:%M:%S", time.localtime(T0 + offset))

        day = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T0))
        self.assertEqual(
            self.report(verbose=False),
            [
                f"window   {day} -> {at(60)}  kernel 7.2.0",
                f"recorded {at(1)} -> {at(56)} (55 s of 60 s, 2 gap(s))",
                "",
                "verdict  KERNEL/FS PROBLEMS: 4 severe event(s) (hung task, btrfs or device error); see 'events'.",
                "verdict  btrfs device error counters INCREASED in this window.",
                "verdict  Blocked tasks: up to 10 tasks waiting on storage in D state (btrfs commit/log/lock waits block tasks without showing up as IO pressure).",
                f"verdict  Slow btrfs commits: longest 1.2 s at {at(4)}.",
                f"verdict  Worst episode: {at(0)}-{at(20)} (20 s, 4 D tasks, commit 0 ms).",
                "verdict  btrfs disks look SATURATED: dm-crypt layer (what btrfs waits for) await 6.0 ms (read 3.0, write 12.0), queue depth 2.5; NVMe below await 1.0 ms (read 0.5, write 2.0), information only (its time can include disk cache flushes); encryption queueing adds latency.",
                "",
                "pressure io some/full 1.2% / 0.5%   memory some/full 0.8% / 0.2%   cpu some 2.5%   (% of recorded time)",
                "peaks    io 10s-avg some/full 9%/4%  tasks waiting on storage 2  on memory 1  iowait tasks 2  dirty 3 MiB (limit 64 MiB)  writeback 1 MiB",
                f"btrfs    data: 4 commits, avg 33 ms, longest 1200 ms @{at(4)}; metadata 25% of allocated chunks; new device errors 0",
                "btrfs    bbbbbbbb: 0 commits, avg 0 ms, longest 0 ms; metadata 0% of allocated chunks; new device errors 1",
                "disks    per device; each RAID1 member carries a full copy, dm-N is the dm-crypt layer above it",
                "  nvme0n1  btrfs:data    await   1.0 ms (r 0.5 / w 2.0)  flush await   1.5 ms (9 flushes)  queue  2.5  read 100 MiB  write 3,072 MiB",
                "  sda                    await   0.0 ms (r 0.0 / w 0.0)  flush await   1.5 ms (9 flushes)  queue  2.5  read 100 MiB  write 0 MiB",
                "  dm-1     swap          await   2.0 ms (r 1.0 / w 4.0)  flush await   1.5 ms (9 flushes)  queue  2.5  read 100 MiB  write 0 MiB",
                "memory   zswap in/out n/a (daemon < v7)   disk swap in/out 1 MiB / 2 MiB   direct reclaim scanned 12,345 pages  allocstalls 1,234",
                "cgroups  IO pressure = time tasks in the cgroup waited on IO; bytes = writes charged to it at the dm-crypt layer (RAID1 mirror copies are charged unevenly, often to kernel threads: compare cgroups with each other, not with disk totals)",
                "  user.slice                                   io some/full  30.0% /  15.0%  read 5 MiB  write 0 MiB",
                "  system.slice                                 io some/full   2.0% /   1.0%  read 5 MiB  write 0 MiB",
                "  docker-0123456789ab (web)                    io some/full   5.0% /   2.5%  read 5 MiB  write 1,024 MiB",
                "  libpod-fedcba987654                          io some/full   0.5% /   0.2%  read 5 MiB  write 100 MiB",
                "  e.service                                    io some/full   8.0% /   4.0%  read 5 MiB  write 0 MiB",
                "  d.service                                    io some/full   7.0% /   3.5%  read 5 MiB  write 0 MiB",
                "  c.service                                    io some/full   6.0% /   3.0%  read 5 MiB  write 0 MiB",
                "  b.service                                    io some/full   4.0% /   2.0%  read 5 MiB  write 0 MiB",
                "below    below dump failed: partial",
                "below    top writers: w0[g.service] 0 MiB, w1 1 MiB, w2[g.service] 2 MiB, w3 3 MiB, w4[g.service] 4 MiB, w5 5 MiB",
                "below    top readers: r[q] 0 MiB",
                "",
                "episodes 7 (worst first, showing 6)",
                f"  {at(0)}-{at(20)}   20s  opened by: psi 0; peak io full 12%, commit 0 ms, D tasks max 4",
                "        1 task-samples [commit] fn0  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "        2 task-samples [commit] fn1 via via x  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "        3 task-samples [commit] fn2  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                f"  {at(1)}-{at(21)}   20s  opened by: psi 1; peak io full 13%, commit 100 ms, D tasks max 5",
                "        1 task-samples [commit] fn0  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "        2 task-samples [commit] fn1 via via x  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                f"  {at(2)}-{at(22)}   20s  opened by: psi 2; peak io full 14%, commit 200 ms, D tasks max 6",
                f"  {at(3)}-{at(23)}   20s  opened by: psi 3; peak io full 15%, commit 300 ms, D tasks max 7",
                "        1 task-samples [commit] fn0  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                f"  {at(4)}-{at(24)}   20s  opened by: psi 4; peak io full 16%, commit 400 ms, D tasks max 8",
                f"  {at(5)}-{at(25)}   20s  opened by: psi 5; peak io full 17%, commit 500 ms, D tasks max 9",
                "         (task-samples: one task seen in D state in one 1 s sample, roughly task-seconds)",
                "",
                "events   11 kernel/daemon event(s) ('!' = severe, on this host's disks):",
                f"  ! {at(3)} event 3",
                f"    {at(4)} event 4",
                f"    {at(5)} event 5",
                f"  ! {at(6)} event 6",
                f"    {at(7)} event 7",
                f"    {at(8)} event 8",
                f"  ! {at(9)} event 9",
                f"    {at(40)} ",
                f"alert    {at(2)} Disk: slow",
                f"alert    {at(3)} ?: ",
            ],
        )
        self.assertEqual(
            self.report(verbose=True),
            [
                f"window   {day} -> {at(60)}  kernel 7.2.0",
                f"recorded {at(1)} -> {at(56)} (55 s of 60 s, 2 gap(s))",
                "",
                "verdict  KERNEL/FS PROBLEMS: 4 severe event(s) (hung task, btrfs or device error); see 'events'.",
                "verdict  btrfs device error counters INCREASED in this window.",
                "verdict  Blocked tasks: up to 10 tasks waiting on storage in D state (btrfs commit/log/lock waits block tasks without showing up as IO pressure).",
                f"verdict  Slow btrfs commits: longest 1.2 s at {at(4)}.",
                f"verdict  Worst episode: {at(0)}-{at(20)} (20 s, 4 D tasks, commit 0 ms).",
                "verdict  btrfs disks look SATURATED: dm-crypt layer (what btrfs waits for) await 6.0 ms (read 3.0, write 12.0), queue depth 2.5; NVMe below await 1.0 ms (read 0.5, write 2.0), information only (its time can include disk cache flushes); encryption queueing adds latency.",
                "",
                "pressure io some/full 1.2% / 0.5%   memory some/full 0.8% / 0.2%   cpu some 2.5%   (% of recorded time)",
                "peaks    io 10s-avg some/full 9%/4%  tasks waiting on storage 2  on memory 1  iowait tasks 2  dirty 3 MiB (limit 64 MiB)  writeback 1 MiB",
                f"btrfs    data: 4 commits, avg 33 ms, longest 1200 ms @{at(4)}; metadata 25% of allocated chunks; new device errors 0",
                "btrfs    bbbbbbbb: 0 commits, avg 0 ms, longest 0 ms; metadata 0% of allocated chunks; new device errors 1",
                "disks    per device; each RAID1 member carries a full copy, dm-N is the dm-crypt layer above it",
                "  nvme0n1  btrfs:data    await   1.0 ms (r 0.5 / w 2.0)  flush await   1.5 ms (9 flushes)  queue  2.5  read 100 MiB  write 3,072 MiB",
                "  sda                    await   0.0 ms (r 0.0 / w 0.0)  flush await   1.5 ms (9 flushes)  queue  2.5  read 100 MiB  write 0 MiB",
                "  dm-0     btrfs:data    await   6.0 ms (r 3.0 / w 12.0)  flush await   1.5 ms (9 flushes)  queue  2.5  read 100 MiB  write 3,072 MiB",
                "  dm-1     swap          await   2.0 ms (r 1.0 / w 4.0)  flush await   1.5 ms (9 flushes)  queue  2.5  read 100 MiB  write 0 MiB",
                "memory   zswap in/out n/a (daemon < v7)   disk swap in/out 1 MiB / 2 MiB   direct reclaim scanned 12,345 pages  allocstalls 1,234",
                "cgroups  IO pressure = time tasks in the cgroup waited on IO; bytes = writes charged to it at the dm-crypt layer (RAID1 mirror copies are charged unevenly, often to kernel threads: compare cgroups with each other, not with disk totals)",
                "  user.slice                                   io some/full  30.0% /  15.0%  read 5 MiB  write 0 MiB",
                "  system.slice                                 io some/full   2.0% /   1.0%  read 5 MiB  write 0 MiB",
                "  docker-0123456789ab (web)                    io some/full   5.0% /   2.5%  read 5 MiB  write 1,024 MiB",
                "  libpod-fedcba987654                          io some/full   0.5% /   0.2%  read 5 MiB  write 100 MiB",
                "  e.service                                    io some/full   8.0% /   4.0%  read 5 MiB  write 0 MiB",
                "  d.service                                    io some/full   7.0% /   3.5%  read 5 MiB  write 0 MiB",
                "  c.service                                    io some/full   6.0% /   3.0%  read 5 MiB  write 0 MiB",
                "  b.service                                    io some/full   4.0% /   2.0%  read 5 MiB  write 0 MiB",
                "below    below dump failed: partial",
                "below    top writers: w0[g.service] 0 MiB, w1 1 MiB, w2[g.service] 2 MiB, w3 3 MiB, w4[g.service] 4 MiB, w5 5 MiB",
                "below    top readers: r[q] 0 MiB",
                "",
                "episodes 7 (worst first)",
                f"  {at(0)}-{at(20)}   20s  opened by: psi 0; peak io full 12%, commit 0 ms, D tasks max 4",
                "        1 task-samples [commit] fn0  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                "        2 task-samples [commit] fn1 via via x  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                "        3 task-samples [commit] fn2  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                "        4 task-samples [commit] fn3 via via x  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                "        5 task-samples [commit] fn4  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                "        6 task-samples [commit] fn5 via via x  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                "        7 task-samples [commit] fn6  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                "        8 task-samples [commit] fn7 via via x  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                f"  {at(1)}-{at(21)}   20s  opened by: psi 1; peak io full 13%, commit 100 ms, D tasks max 5",
                "        1 task-samples [commit] fn0  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                "        2 task-samples [commit] fn1 via via x  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                f"  {at(2)}-{at(22)}   20s  opened by: psi 2; peak io full 14%, commit 200 ms, D tasks max 6",
                f"  {at(3)}-{at(23)}   20s  opened by: psi 3; peak io full 15%, commit 300 ms, D tasks max 7",
                "        1 task-samples [commit] fn0  comms: a(3), b(2), c(1), d(1)  cgroups: x.slice, y.slice, z.slice",
                "          stack: f0 <- f1 <- f2 <- f3 <- f4 <- f5 <- f6 <- f7 <- f8 <- f9",
                f"  {at(4)}-{at(24)}   20s  opened by: psi 4; peak io full 16%, commit 400 ms, D tasks max 8",
                f"  {at(5)}-{at(25)}   20s  opened by: psi 5; peak io full 17%, commit 500 ms, D tasks max 9",
                f"  {at(6)}-{at(26)}   20s  opened by: psi 6; peak io full 18%, commit 600 ms, D tasks max 10",
                "         (task-samples: one task seen in D state in one 1 s sample, roughly task-seconds)",
                "",
                "events   11 kernel/daemon event(s) ('!' = severe, on this host's disks):",
                f"  ! {at(0)} event 0",
                f"    {at(1)} event 1",
                f"    {at(2)} event 2",
                f"  ! {at(3)} event 3",
                f"    {at(4)} event 4",
                f"    {at(5)} event 5",
                f"  ! {at(6)} event 6",
                f"    {at(7)} event 7",
                f"    {at(8)} event 8",
                f"  ! {at(9)} event 9",
                f"    {at(40)} ",
                f"alert    {at(2)} Disk: slow",
                f"alert    {at(3)} ?: ",
            ],
        )


CAT_LOG_SYNC = "fsync log-tree sync (btrfs_sync_log)"
MAINT_TITLE = "btrfs maintenance (fstrim/scrub) waiting (recorded as stuck on IO)"


class SummariseBranchesTest(unittest.TestCase):
    """summarise() on small windows that each reach one branch: coverage and gaps, the btrfs, disk and
    cgroup totals, the episodes of older daemons and their estimates, and the events it renames."""

    @staticmethod
    def rec(ts: float, **fields: object) -> dict[str, object]:
        """A sample record of a v8 daemon at ts (mono follows ts), with fields replaced."""
        base: dict[str, object] = {
            "t": "s",
            "ts": ts,
            "mono": ts - T0 + 100,
            "psi": {"io": [1.0, 1000 * ts, 0.5, 500 * ts], "memory": [0, 0, 0, 0], "cpu": [0, 0, 0, 0]},
            "procs": [1, 2],
            "vm": [1, 2, 0, 0, 0, 0, 0],
            "fs": {},
            "disk": {},
            "cg": {},
            "d": 1,
            "d_all": 2,
            "d_maint": 0,
            "d_mem": 1,
        }
        base.update(fields)
        return base

    @staticmethod
    def window(records: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
        """The records of a window [T0, T0 + 60] after a v8 meta record, for analyse()."""
        return [meta(T0 - 100, 8), *records]

    def test_a_vmstat_value_the_daemon_could_not_read_adds_nothing(self) -> None:
        # pswpout 100, 120, null, 130: the pairs add 20, nothing, nothing (main recorded null as 0: the last
        # pair added all 130 pages, every page since boot as a burst). A null nr_dirty gives no peak.
        def vm(nr_dirty: int | None, pswpout: int | None) -> list[int | None]:
            return [nr_dirty, 0, 0, pswpout, 0, 0, 0]

        samples = [
            self.rec(T0 + 1, vm=vm(1, 100)),
            self.rec(T0 + 6, vm=vm(3, 120)),
            self.rec(T0 + 11, vm=vm(None, None)),
            self.rec(T0 + 16, vm=vm(2, 130)),
        ]
        res, _ = load().analyse(self.window(samples), T0, T0 + 20, with_below=False, verbose=False)
        self.assertEqual(res["vm"]["swap_out_bytes"], 20 * rp_records.PAGE)
        self.assertEqual(res["peak"]["dirty_bytes"], 3 * rp_records.PAGE)

    def test_totals_of_the_sample_pairs(self) -> None:
        fs_a = {"u": {"c": [10, 100, 0, 0, 0, 50], "err": {"1": 1, "2": 0}, "space": {"metadata": [1, 2, 0, 0, 0]}}}
        fs_b = {"u": {"c": [12, 160, 0, 0, 0, 900], "err": {"1": 3, "2": 0}}, "v": {"c": [1, 1, 0, 0, 0, 5], "err": {}}}
        fs_c = {
            "u": {"c": [11, 170, 0, 0, 0, 20], "err": {"1": 3, "2": 1}},
            "v": {"c": [2, 3, 0, 0, 0, 5], "err": {"9": 4}},
        }

        def disk(n: int) -> list[int]:
            return [n] * 17

        samples = [
            self.rec(T0 - 20),
            self.rec(T0 - 10),
            self.rec(T0 + 1, fs=fs_a, disk={"nvme0n1": disk(10), "sda": []}, cg={"user.slice": [1, 1, 0, 0, 10, 20]}),
            self.rec(
                T0 + 6,
                fs=fs_b,
                disk={"nvme0n1": disk(20), "sda": disk(1)},
                cg={"user.slice": [5, 2, 0, 0, 30, 60], "x": [1, 1, 0, 0, 1, 1]},
            ),
            self.rec(
                T0 + 11,
                fs=fs_c,
                disk={"nvme0n1": disk(15), "sda": disk(2)},
                cg={"user.slice": [4, 3, 0, 0, 40, 70], "x": [2, 2, 0, 0, 2, 2]},
            ),
            self.rec(T0 + 11),
            self.rec(T0 + 50),
            self.rec(T0 + 55, d=4, d_mem=2, d_maint=1),
        ]
        # A meta record after the window does not count; the samples before the one at or before since go.
        m = load()
        later = {"t": "meta", "ts": T0 + 100, "v": 8, "kernel": "later"}
        res, _ = m.analyse(self.window([later, *samples]), T0, T0 + 60, with_below=False, verbose=False)
        self.assertEqual((res["kernel"], res["samples"]), ("7.2.0", 7))
        # Two gaps: a pair 0 s apart, one 39 s apart.
        self.assertEqual(
            res["coverage"],
            {"window_s": 60, "covered_s": 26, "first_sample": T0 - 10, "last_sample": T0 + 55, "complete": False}
            | {"skipped": 0, "gaps": 2},
        )
        # A filesystem new in a pair counts its longest commit only; commits that went back are left out;
        # device errors count what grew.
        self.assertEqual(
            res["fs"],
            {
                "u": {"commits": 2, "commit_ms": 60, "max_commit_ms": 900, "max_at": T0 + 6, "new_device_errors": 3}
                | {"space": {}, "avg_commit_ms": 30.0},
                "v": {"commits": 1, "commit_ms": 2, "max_commit_ms": 5, "max_at": T0 + 6, "new_device_errors": 0}
                | {"space": {}, "avg_commit_ms": 2.0},
            },
        )
        # A disk new in a pair, or with a counter that went back, is left out of that pair.
        disks = res["disks"]
        self.assertEqual(
            {
                n: (d["rd_ios"], d["peak_util_pct"], d["peak_at"], d["await_ms"], d["util_pct"])
                for n, d in disks.items()
            },
            {"nvme0n1": (10, 0.2, T0 + 6, 1.0, 1 / 26), "sda": (1, 0.02, T0 + 11, 1.0, 0.1 / 26)},
        )
        self.assertEqual(
            res["cgroups"],
            {
                "user.slice": {"io_some": 4, "io_full": 1, "read_bytes": 20, "write_bytes": 40, "wall": 5.0}
                | {"io_some_pct": 4 / 1e6 / 5 * 100, "io_full_pct": 1 / 1e6 / 5 * 100},
                "x": {"io_some": 1, "io_full": 1, "read_bytes": 1, "write_bytes": 1, "wall": 5.0}
                | {"io_some_pct": 1 / 1e6 / 5 * 100, "io_full_pct": 1 / 1e6 / 5 * 100},
            },
        )
        peak = res["peak"]
        self.assertEqual(
            (
                peak["d_tasks"],
                peak["mem_tasks"],
                peak["maint_tasks"],
                res["maintenance_samples"],
                res["sample_d_tasks"],
            ),
            (4, 2, 1, 1, 4),
        )
        # One sample: the window, the counts and the coverage only.
        self.assertEqual(
            m.analyse(self.window(samples[:1]), T0, T0 + 60, with_below=False, verbose=False)[0],
            {
                "window": {"since": T0, "until": T0 + 60},
                "kernel": "7.2.0",
                "samples": 1,
                "episodes": [],
                "events": [],
                "alerts": [],
                "coverage": {"window_s": 60, "covered_s": 0, "first_sample": T0 - 20, "last_sample": T0 - 20}
                | {"complete": False, "skipped": 0},
            },
        )

    def test_older_daemons_episodes_and_notices(self) -> None:
        def sig(name: str, n: int, kind: str | None = None) -> dict[str, object]:
            s: dict[str, object] = {
                "sig": name,
                "ctx": "",
                "n": n,
                "comms": {"a": n},
                "cgroups": {"x.slice": n},
                "examples": [[name]],
            }
            if kind is not None:
                s["kind"] = kind
            return s

        v2 = {
            "t": "ep",
            "start": T0 + 20,
            "end": T0 + 30,
            "reason": "psi",
            "max_d": 10,
            "peak": {"commit_ms": 5},
            "sigs": [sig("btrfs_sync_log", 6), sig("zswap_load", 2), sig("fuse_simple_request", 2)],
        }
        v2_empty = {"t": "ep", "start": T0 + 31, "end": T0 + 32, "reason": "psi", "max_d": 3, "peak": {}, "sigs": []}
        v5 = {
            "t": "ep",
            "start": T0 + 45,
            "end": T0 + 50,
            "reason": "hung task",
            "max_d": 8,
            "max_d_all": 9,
            "peak": {"io_full_avg10": 3},
            "sigs": [
                sig("btrfs_sync_log", 3, "storage"),
                sig("shrink_node", 1, "storage"),
                sig("btrfs_ioctl_fitrim", 4, "storage"),
                sig("futex_wait", 5, "idle"),
            ],
        }
        v5_storage = {
            "t": "ep",
            "start": T0 + 52,
            "end": T0 + 53,
            "reason": "psi",
            "max_d": 4,
            "max_d_all": 4,
            "peak": {},
            "sigs": [sig("btrfs_sync_log", 3, "storage")],
        }
        v8 = {
            "t": "ep",
            "start": T0 + 54,
            "end": T0 + 56,
            "reason": "psi",
            "max_d": 2,
            "max_d_all": 3,
            "max_d_mem": 1,
            "peak": {"commit_ms": 9},
            "sigs": [sig("btrfs_sync_log", 3, "storage"), sig("zswap_load", 1, "memory")],
        }
        old = {k: v for k, v in self.rec(0).items() if k not in ("d_maint", "d_mem")}
        samples = [
            self.rec(T0 + 1),
            {**old, "ts": T0 + 5, "mono": 105.0, "d": 5},  # v3/v4: d counted memory and maintenance too
            {**old, "ts": T0 + 10, "mono": 110.0, "d": 2},
            {**old, "ts": T0 + 15, "mono": 115.0, "d": 9},
            {**old, "ts": T0 + 20, "mono": 120.0, "d": 7},
            {**{k: v for k, v in self.rec(0).items() if k != "d_mem"}, "ts": T0 + 25, "mono": 125.0, "d": 6},
            {**{k: v for k, v in self.rec(0).items() if k != "d_mem"}, "ts": T0 + 46, "mono": 146.0, "d": 9},
            self.rec(T0 + 50),
        ]
        v2_fuse = "task gvfsd:1 blocked 30 s at fuse_simple_request"
        maint = "z:3 waited 31 s"
        events = [
            {"t": "ev", "ts": T0 + 5, "kind": "hung", "severe": True, "msg": v2_fuse},
            {
                "t": "ev",
                "ts": T0 + 6,
                "kind": "hung",
                "severe": True,
                "msg": maint,
                "stack": ["schedule", "btrfs_ioctl_fitrim"],
            },
            {"t": "ev", "ts": T0 + 7, "kind": "hung", "severe": True, "msg": "task fstrim:123 blocked at x"},
            {
                "t": "ev",
                "ts": T0 + 8,
                "kind": "hung",
                "severe": True,
                "msg": "w:4 at btrfs_sync_log",
                "stack": ["btrfs_sync_log"],
            },
            {"t": "ev", "ts": T0 - 5, "kind": "hung", "msg": "outside the window at fuse_x"},
        ]
        alerts = [
            {"t": "al", "ts": T0 + 5, "kind": "hung", "title": "T", "body": "y:2 stuck at request_wait_answer"},
            {"t": "al", "ts": T0 + 6, "kind": "hung", "title": "T", "body": maint},
            {"t": "al", "ts": T0 + 9, "kind": "hung", "title": "T", "body": "in cgroup btrfs-scrub@home.service"},
            {"t": "al", "ts": T0 + 9, "kind": "hung", "title": "T", "body": "w:4 at btrfs_sync_log"},
        ]
        for verbose in (False, True):
            with self.subTest(verbose=verbose):
                records = [meta(T0, 4), *samples, v2, v2_empty, v5, v5_storage, v8, *events, *alerts]
                res, _ = load().analyse(
                    self.window(copy.deepcopy(records)), T0, T0 + 60, with_below=False, verbose=verbose
                )
                eps = res["episodes"]
                # v2 (no max_d_all) and v3-v7 (no max_d_mem): max_d estimated from the storage share.
                self.assertEqual(
                    [(e["start"] - T0, e["max_d"], e["max_d_all"], e["max_d_mem"], e.get("estimated")) for e in eps],
                    [
                        (20, 6, 10, 2, True),
                        (52, 4, 4, 0, None),
                        (45, 3, 9, 1, True),
                        (54, 2, 3, 1, None),
                        (31, 0, 3, 0, True),
                    ],
                )
                self.assertEqual({"examples" in s for e in eps for s in e["sigs"]}, {verbose})
                self.assertEqual([s["cat"] for s in eps[2]["sigs"]], ["log-sync", "reclaim", "maintenance", "idle"])
                self.assertEqual(
                    res["categories"],
                    [{"category": "log-sync", "description": CAT_LOG_SYNC, "task_samples": 15, "share": 1.0}],
                )
                self.assertEqual(res["non_storage_waits"], {"reclaim": 4, "fuse": 2, "maintenance": 4, "idle": 5})
                # The v3-v7 samples within a capped episode (from 35 s before a hung task's) count its estimate.
                peak = res["peak"]
                self.assertEqual((peak["d_tasks"], peak["mem_tasks"], res["sample_d_tasks"]), (6, 2, 3))
                notices = res["events"] + res["alerts"]
                self.assertEqual(
                    [(n["kind"], n.get("severe"), n.get("title")) for n in notices],
                    [
                        ("fuse-wait", False, None),
                        ("maintenance-wait", False, None),
                        ("maintenance-wait", False, None),
                        ("hung", True, None),
                        ("fuse", None, "Program waiting on a FUSE filesystem (recorded as stuck on IO)"),
                        ("maintenance-wait", None, MAINT_TITLE),
                        ("maintenance-wait", None, MAINT_TITLE),
                        ("hung", None, "T"),
                    ],
                )
                self.assertEqual((res["open_episode"], res["v6_since"]), (None, T0 - 100))


class ClassifyBranchesTest(unittest.TestCase):
    """Each way classify() decides, one case per branch (the mutation run left them unchecked)."""

    def test_cases(self) -> None:
        c = rp_categories.classify
        cases = [
            # (sig, examples, comms, kind) -> category
            (("x", [], (), "memory"), "reclaim"),
            (("x", [], (), "idle"), "idle"),
            (("x", [["btrfs_sync_log"]], (), None), "log-sync"),
            # A commit anywhere on the stack wins over the innermost frame (wbt).
            (("x", [["wbt_wait", "btrfs_commit_transaction"]], (), None), "commit"),
            (("no-stack", [], ("fstrim",), None), "maintenance"),
            (("no-stack", [], ("btrfs-discard-1",), None), "maintenance"),
            (("no-stack", [], ("zsh",), None), "no-stack"),
            (("no-stack", [], ("btrfs-transaction",), None), "commit"),
            (("no-stack", [], ("kcryptd/253:0",), None), "device"),
            (("no-stack", [], ("bash",), None), "no-stack"),
            (("wchan:balance_dirty_pages", [], (), None), "dirty-throttle"),
            (("wchan:", [], (), None), "no-stack"),
            (("ep_poll", [], (), None), "idle"),
            (("ep_poll", [], (), "storage"), "other"),
            (("mystery", [["x", "wbt_wait"]], (), None), "wbt"),
            (("mystery", [["x", "y"]], (), None), "other"),
            # A FUSE signature, before the stack rules.
            (("fuse_simple_request", [["btrfs_sync_log"]], (), None), "fuse"),
            # The stack rules go rule by rule over each example; the last step frame by frame.
            (("mystery", [["btrfs_sync_log", "btrfs_commit_transaction"]], (), None), "commit"),
            (("mystery", [["x", "wbt_wait", "zstd_get_workspace"]], (), None), "wbt"),
            (("mystery", [["x"], ["zstd_get_workspace", "wbt_wait"]], (), None), "zstd-workspace"),
        ]
        for (sig, examples, comms, kind), want in cases:
            with self.subTest(sig=sig, comms=comms, kind=kind):
                self.assertEqual(c(sig, examples, comms, kind), want)


class ClosedPipeTest(unittest.TestCase):
    def test_report_into_a_closed_pipe_stops_quietly(self) -> None:
        # `btrfs-stall-report | head -1` ended in "Exception ignored ...
        # BrokenPipeError" (exit 120). Now it ends on SIGPIPE, silently. An empty log folder: the
        # report still prints its window and verdict.
        tool = os.path.join(_CHECKOUT, "btrfs", "btrfs-stall-report")
        with tempfile.TemporaryDirectory() as logs:
            for args in (["--log-dir", logs, "--since", "@1", "--until", "@2", "--no-below"], ["--help"]):
                with self.subTest(args=args):
                    r, w = os.pipe()
                    os.close(r)
                    try:
                        p = subprocess.run(
                            ["/usr/bin/python3", "-I", tool, *args],
                            check=False,
                            stdout=w,
                            stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL,
                            env={"PATH": "/usr/bin:/bin"},
                            timeout=60,
                        )
                    finally:
                        os.close(w)
                    self.assertEqual((p.returncode, p.stderr), (-signal.SIGPIPE, b""))


DAMAGED = "INCOMPLETE DATA: {} damaged record(s) in this window were left out (re-running does not help)"


class MalformedLineTest(unittest.TestCase):
    """Lines the report cannot use: skipped, counted in the window they belong to, and named in the
    verdict, never a traceback and never silent."""

    tool = os.path.join(_CHECKOUT, "btrfs", "btrfs-stall-report")

    def report(self, files: Mapping[float, Sequence[str]], since: float, until: float, *args: str) -> str:
        """The report of the window over log files (the day of each key: its lines, as written)."""
        with tempfile.TemporaryDirectory() as logs:
            for day, lines in files.items():
                path = os.path.join(logs, time.strftime("stall-%Y-%m-%d.jsonl", time.localtime(day)))
                with open(path, "w", encoding="utf-8") as f:
                    f.write("".join(lines))
            window = ["--since", f"@{since:.0f}", "--until", f"@{until:.0f}"]
            p = subprocess.run(
                ["/usr/bin/python3", "-I", self.tool, "--log-dir", logs, *window, "--no-below", *args],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin"},
                timeout=60,
            )
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        return p.stdout

    @staticmethod
    def lines(records: Sequence[object]) -> list[str]:
        return [(r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records]

    def test_lines_that_are_json_but_no_record_are_skipped_and_counted(self) -> None:
        # A line that parses to a list, number, string or null (a cut or foreign write) is skipped
        # like a line that is not JSON, and counted: before, both were dropped without a word.
        now = time.time()
        good = [meta(now - 30, 7), *(sample(now - 25 + 5 * i, 1000, 0) for i in range(3))]
        junk = ["[1, 2]", "5", '"s"', "null", "true", "not json"]
        clean = self.report({now: self.lines(good)}, now - 60, now)
        damaged = self.report({now: self.lines([*junk, *good, *junk])}, now - 60, now)
        self.assertEqual(damaged.replace(f"verdict  {DAMAGED.format(12)}\n", ""), clean)
        self.assertIn(DAMAGED.format(12), damaged)
        self.assertNotIn("damaged", clean)

    def test_a_record_without_what_the_report_reads_is_skipped_and_counted(self) -> None:
        # Before: a TypeError or KeyError in summarise(), the whole report a traceback.
        now = time.time()
        good = [meta(now - 30, 7), *(sample(now - 25 + 5 * i, 1000, 0) for i in range(3))]
        bad_sample = {**sample(now - 12, 1000, 0), "ts": "x"}
        no_psi = {k: v for k, v in sample(now - 11, 1000, 0).items() if k != "psi"}
        bad_episode = {k: v for k, v in episode(now - 20, now - 15, 3, [("a", 1, "storage")]).items() if k != "end"}
        bad_event = {"t": "ev", "ts": now - 10, "msg": 5}
        out = self.report({now: self.lines([*good, bad_sample, no_psi, bad_episode, bad_event])}, now - 60, now)
        self.assertIn(DAMAGED.format(4), out)
        report = json.loads(self.report({now: self.lines([*good, bad_sample, no_psi])}, now - 60, now, "--json"))
        self.assertEqual(report["coverage"]["skipped"], 2)
        self.assertIn(DAMAGED.format(2), report["verdict"])
        self.assertEqual(
            json.loads(self.report({now: self.lines(good)}, now - 60, now, "--json"))["coverage"]["skipped"], 0
        )

    def test_events_and_alerts_without_their_texts_are_shown(self) -> None:
        # Notice keeps msg, title and body NotRequired; the text report read them with [] (KeyError).
        now = time.time()
        recs = [meta(now - 30, 7), *(sample(now - 25 + 5 * i, 1000, 0) for i in range(3))]
        out = self.report(
            {now: self.lines([*recs, {"t": "ev", "ts": now - 20}, {"t": "al", "ts": now - 19}])}, now - 60, now
        )
        self.assertIn("events   1 kernel/daemon event(s)", out)
        self.assertRegex(out, r"alert    \S+ \?: \n")
        self.assertNotIn("damaged", out)

    def test_what_is_not_counted(self) -> None:
        now = time.time()
        good = [meta(now - 30, 7), *(sample(now - 25 + 5 * i, 1000, 0) for i in range(3))]
        # The last line without its newline: the daemon may be writing it.
        torn = [*self.lines(good), '{"t": "s", "ts": 1']
        self.assertNotIn("damaged", self.report({now: torn}, now - 60, now))
        # A damaged line of another day's file, among that day's records: another window.
        yesterday = now - 86400
        old = self.lines([meta(yesterday - 30, 7), "not json", sample(yesterday - 20, 1000, 0)])
        self.assertNotIn("damaged", self.report({now: self.lines(good), yesterday: old}, now - 60, now))
        # A kind of record the report does not read.
        self.assertNotIn("damaged", self.report({now: self.lines([*good, {"t": "x", "ts": "y"}])}, now - 60, now))

    def test_a_record_whose_kind_is_no_string_is_not_read(self) -> None:
        # A "t" that is a list or an object names no kind the report reads: the record is not read,
        # like one of another kind. Before, records_of grouped by it and the report died (unhashable).
        now = time.time()
        good = [meta(now - 30, 7), *(sample(now - 25 + 5 * i, 1000, 0) for i in range(3))]
        clean = self.report({now: self.lines(good)}, now - 60, now)
        for kind in ([], {}):
            with self.subTest(t=kind):
                odd = {"t": kind, "ts": now - 10}
                self.assertEqual(self.report({now: self.lines([*good, odd])}, now - 60, now), clean)

    def test_a_cut_gz_file_keeps_the_records_before_the_cut(self) -> None:
        # A .gz whose end is missing (a copy cut short): the lines read before the error count, with a
        # warning, as they always did.
        now = time.time()
        good = [meta(now - 30, 7), *(sample(now - 25 + 5 * i, 1000, 0) for i in range(3))]
        with tempfile.TemporaryDirectory() as logs:
            path = os.path.join(logs, time.strftime("stall-%Y-%m-%d.jsonl.gz", time.localtime(now)))
            data = gzip.compress("".join(self.lines(good)).encode())
            with open(path, "wb") as f:
                f.write(data[:-8])  # without its trailer (CRC and size): EOFError at the end
            window = ["--since", f"@{now - 60:.0f}", "--until", f"@{now:.0f}"]
            p = subprocess.run(
                ["/usr/bin/python3", "-I", self.tool, "--log-dir", logs, *window, "--no-below"],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin"},
                timeout=60,
            )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"warning: cannot fully read {path}", p.stderr)
        self.assertNotIn("NO DATA", p.stdout)

    def test_records_of_every_daemon_version_are_read(self) -> None:
        # The oldest shapes: a sample with only what every daemon wrote (no mono, vm, d, d_all,
        # d_maint, d_mem, cg), an episode without max_d_all, max_d_mem, kind or examples, a meta
        # record with only its version, events and alerts with their texts.
        now = time.time()
        oldest = {
            "t": "s",
            "ts": 0.0,
            "psi": {"io": [0, 0, 0, 0], "memory": [0, 0, 0, 0], "cpu": [0, 0, 0, 0]},
            "procs": [1, 0],
            "fs": {},
            "disk": {},
        }
        ep = {
            "t": "ep",
            "ts": now - 20,
            "start": now - 20,
            "end": now - 15,
            "reason": "hung task",
            "max_d": 2,
            "peak": {"io_full_avg10": 1.0},
            "sigs": [{"sig": "a", "ctx": "", "n": 1, "comms": {}, "cgroups": {}}],
        }
        recs = [
            {"t": "meta", "ts": now - 30, "v": 1},
            *({**oldest, "ts": now - 25 + 5 * i} for i in range(3)),
            ep,
            {"t": "ev", "ts": now - 18, "msg": "x", "severe": False},
            {"t": "al", "ts": now - 17, "title": "t", "body": "b"},
            *old_run(3),
            *new_run(now - 40, 3),
        ]
        out = self.report({now: self.lines(recs)}, now - 60, now)
        self.assertNotIn("damaged", out)
        self.assertIn("episodes 1", out)

    def test_a_meta_whose_dirty_bytes_is_null_is_read(self) -> None:
        # The daemon writes read_int's None when it cannot read /proc/sys/vm/dirty_bytes (a
        # container, a restricted /proc): the meta still counts, with its vm_keys, disks and kernel,
        # and the limit reads as 0, as for a meta without the key. Before: the meta was damaged.
        now = time.time()
        rest = [*(sample(now - 25 + 5 * i, 1000, 0) for i in range(3))]
        null, zero = ({**meta(now - 30, 7), "dirty_bytes": d} for d in (None, 0))
        out = self.report({now: self.lines([null, *rest])}, now - 60, now)
        self.assertNotIn("damaged", out)
        self.assertEqual(out, self.report({now: self.lines([zero, *rest])}, now - 60, now))
        report = json.loads(self.report({now: self.lines([null, *rest])}, now - 60, now, "--json"))
        self.assertEqual((report["coverage"]["skipped"], report["vm"]["dirty_limit_bytes"]), (0, 0))

    def test_no_data_says_what_was_damaged(self) -> None:
        now = time.time()
        out = self.report({now: self.lines([meta(now - 30, 7), "not json", sample(now - 20, 1000, 0)])}, now - 60, now)
        verdicts = [line for line in out.splitlines() if line.startswith("verdict")]
        self.assertTrue(verdicts[0].startswith("verdict  NO DATA"), out)
        self.assertEqual(verdicts[1], f"verdict  {DAMAGED.format(1)}")


# A stand-in for btrfs-stall-report, for btrfs-stall-watch: samples every 5 s around the
# moment the workload ended (the workload writes it to $T/end), of which the recorder has
# handed to disk only those up to 2 s before that end at the first call, and all of them
# from the second call on (it writes about every 10 s). Like the real report it keeps only
# samples in [--since, --until] (--until: default now). Every call is logged to $T/calls.
FAKE_REPORT = """#!/usr/bin/python3 -I
import json, os, sys, time
t = os.environ["T"]
with open(os.path.join(t, "calls"), "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
with open(os.path.join(t, "calls")) as f:
    n = len(f.read().splitlines())
args = sys.argv[1:]
def opt(name, default):
    return float(args[args.index(name) + 1].lstrip("@")) if name in args else default
since, until = opt("--since", 0.0), opt("--until", time.time() + 3600)
end = float(open(os.path.join(t, "end")).read())
samples = [] if os.environ.get("NO_SAMPLES") else [end + d for d in (-12.5, -7.5, -2.5, 2.5, 7.5)]
written = [s for s in samples if n > 1 or s <= end - 2]
kept = [s for s in written if since <= s <= until]
if "--json" in args:
    print(json.dumps({"window": {"since": since, "until": until},
                      "coverage": {"last_sample": kept[-1] if kept else None}, "samples": len(kept)}))
else:
    print("verdict  fake")
sys.exit(int(os.environ.get("FAKE_RC", "0")))
"""


class StallWatchWaitTest(unittest.TestCase):
    """btrfs/btrfs-stall-watch waits for the recorder to cover the end of the run, then
    saves the report of a window that reaches that sample. sleep is a no-op on PATH."""

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-stall-watch-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        for name, text in (("btrfs-stall-report", FAKE_REPORT), ("sleep", "#!/bin/sh\nexit 0\n")):
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o755)

    def watch(
        self, *, no_samples: bool = False, report_rc: int = 0
    ) -> tuple[subprocess.CompletedProcess[str], float, float | None, list[str]]:
        """The run, and from the saved report.json: the window's end and the last sample in it."""
        env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.tmp,
            "XDG_STATE_HOME": os.path.join(self.tmp, "state"),
            "T": self.tmp,
            "LC_ALL": "C",
        }
        if no_samples:
            env["NO_SAMPLES"] = "1"
        env["FAKE_RC"] = str(report_rc)
        script = os.path.join(_CHECKOUT, "btrfs", "btrfs-stall-watch")
        r = subprocess.run(
            ["bash", script, "-l", "t", "--", "sh", "-c", 'date +%s >"$T/end"; exit 3'],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=120,
        )
        base = os.path.join(self.tmp, "state", "btrfs-stall-watch")
        runs = [d for d in os.listdir(base) if d != "runs.tsv"]
        self.assertEqual(len(runs), 1, runs)
        with open(os.path.join(base, runs[0], "report.json")) as f:
            report = json.load(f)
        with open(os.path.join(self.tmp, "calls")) as f:
            calls = f.read().splitlines()
        last = report["coverage"]["last_sample"]
        return r, float(report["window"]["until"]), None if last is None else float(last), calls

    def test_it_waits_for_the_first_sample_after_the_run_and_saves_a_window_reaching_it(self) -> None:
        r, until, last, calls = self.watch()
        self.assertEqual(r.returncode, 3, r.stderr)
        with open(os.path.join(self.tmp, "end")) as f:
            end = float(f.read())
        # The old wait polled with --until end+1, so the samples after the end never counted:
        # it ran out its 20 polls and saved a window ending before them.
        assert last is not None
        self.assertGreater(last, end)
        self.assertGreaterEqual(last, until - 2)
        polls = [c for c in calls if "--json" in c]
        self.assertEqual(len(polls), 3, calls)  # two waits, then the saved report
        self.assertNotIn("--until", polls[0])
        self.assertIn("verdict  fake", r.stdout)

    def test_a_report_that_exits_non_zero_still_ends_the_wait(self) -> None:
        # JSON printed with an exit status other than 0 (today's report exits 0 whenever it
        # prints JSON) still counts, and does not keep watch waiting.
        r, until, last, calls = self.watch(report_rc=1)
        self.assertEqual(r.returncode, 3, r.stderr)
        assert last is not None
        self.assertGreaterEqual(last, until - 2)
        self.assertEqual(len([c for c in calls if "--json" in c]), 3, calls)

    def test_with_no_recording_it_gives_up_and_keeps_the_runs_own_window(self) -> None:
        r, until, last, calls = self.watch(no_samples=True)
        self.assertEqual(r.returncode, 3, r.stderr)
        with open(os.path.join(self.tmp, "end")) as f:
            end = float(f.read())
        self.assertIsNone(last)
        self.assertIn(until - end, (1.0, 2.0))  # end + 1 (date may tick between)
        self.assertEqual(len([c for c in calls if "--json" in c]), 21, calls)


class ReportCommandsTest(unittest.TestCase):
    """The three commands the report runs (date -d for --since/--until, docker/podman ps for container names,
    below dump), with fake programs: PATH is only the folder of fakes (they name /usr/bin/python3 themselves), so
    no real program can be reached."""

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-report-cmds-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        patcher = mock.patch.dict(os.environ, {"PATH": self.bin})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, name: str, out: bytes = b"", err: str = "", rc: int = 0, *, first_line: str = "") -> None:
        """A program NAME that writes its arguments (JSON) to NAME.argv, prints OUT and ERR and exits RC;
        FIRST_LINE replaces its #! line (a program that cannot start)."""
        log = os.path.join(self.tmp, f"{name}.argv")
        body = (
            "import json, sys\n"
            f"with open({log!r}, 'w', encoding='utf-8') as f:\n"
            "    json.dump(sys.argv[1:], f)\n"
            f"sys.stdout.buffer.write({out!r})\n"
            f"sys.stderr.write({err!r})\n"
            f"sys.exit({rc})\n"
        )
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write((first_line or "#!/usr/bin/python3 -I") + "\n" + body)
        os.chmod(path, 0o755)

    def argv(self, name: str) -> list[str] | None:
        """The arguments the fake NAME got, None when it did not run."""
        try:
            with open(os.path.join(self.tmp, f"{name}.argv"), encoding="utf-8") as f:
                args: object = json.load(f)
        except FileNotFoundError:
            return None
        assert isinstance(args, list)
        return [str(a) for a in args]

    # ---- date -d -------------------------------------------------------------------------------
    def test_a_time_in_words_is_read_by_date(self) -> None:
        self.fake("date", b"1234\n")
        self.assertEqual(rp_adapters.parse_time(" 10:30 ", 5.0), 1234.0)
        self.assertEqual(self.argv("date"), ["-d", "10:30", "+%s"])

    def test_a_time_date_refuses_ends_the_report_with_its_words(self) -> None:
        self.fake("date", err="bad\n", rc=2)
        with self.assertRaises(SystemExit) as cm:
            rp_adapters.parse_time("10:30", 5.0)
        self.assertEqual(cm.exception.code, "cannot parse time: '10:30' (bad)")

    def test_numbers_and_no_time_never_run_date(self) -> None:
        self.fake("date", b"1\n")
        self.assertEqual(rp_adapters.parse_time("@5", 0.0), 5.0)
        self.assertEqual(rp_adapters.parse_time("7", 0.0), 7.0)
        self.assertEqual(rp_adapters.parse_time(None, 3.0), 3.0)
        self.assertEqual(rp_adapters.parse_time("", 3.0), 3.0)
        self.assertIsNone(self.argv("date"))

    def test_without_date_the_report_ends_with_its_error(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            rp_adapters.parse_time("10:30", 5.0)
        self.assertEqual(cm.exception.code, "cannot parse time: '10:30' ([Errno 2] No such file or directory: 'date')")

    def test_a_date_that_cannot_start_ends_the_report_with_its_error(self) -> None:
        self.fake("date", first_line="not a program")
        with self.assertRaises(SystemExit) as cm:
            rp_adapters.parse_time("10:30", 5.0)
        self.assertRegex(str(cm.exception.code), r"^cannot parse time: '10:30' \(\[Errno 8\] Exec format error: ")

    # ---- docker / podman ps ----------------------------------------------------------------------
    def test_container_names_of_docker_and_podman(self) -> None:
        self.fake("docker", b"0123456789abcdef0000\tweb\n\torphan\n\n")
        self.fake("podman", b"fedcba9876543210\tdb\n", err="warning\n", rc=3)  # a failed run is still read
        self.assertEqual(rp_adapters.container_names(), {"docker-0123456789ab": "web", "libpod-fedcba987654": "db"})
        want = ["ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"]
        self.assertEqual(self.argv("docker"), want)
        self.assertEqual(self.argv("podman"), want)

    def test_a_tool_that_is_missing_or_cannot_start_is_skipped(self) -> None:
        self.assertEqual(rp_adapters.container_names(), {})
        self.fake("docker", first_line="#!/nonexistent/interpreter")  # found by which, its exec fails
        self.fake("podman", first_line="not a program")
        self.assertEqual(rp_adapters.container_names(), {})
        self.fake("podman", b"abc\tdb\n")
        self.assertEqual(rp_adapters.container_names(), {"libpod-abc": "db"})

    # ---- below dump ----------------------------------------------------------------------------
    def test_below_rows_become_the_top_list(self) -> None:
        rows = [
            {"comm": "dd", "cgroup": "/system.slice/a.service", "io.rbytes_per_sec": 9, "io.wbytes_per_sec": 100},
            {"comm": "cp", "cgroup": "/user.slice/", "io.wbytes_per_sec": "300.5 B/s"},
            {"comm": "dd", "cgroup": "/system.slice/a.service", "io.wbytes_per_sec": 50},
            {"comm": "idle", "cgroup": "/", "io.wbytes_per_sec": 0},
        ]
        self.fake("below", json.dumps(rows).encode())
        items, err = rp_adapters.below_top(100.7, 200.2, "io.wbytes_per_sec", top=3)
        self.assertIsNone(err)
        self.assertEqual(
            items,
            [
                {"comm": "cp", "cgroup": "user.slice", "bytes": 4507},  # rows without a time: 15 s each
                {"comm": "dd", "cgroup": "a.service", "bytes": 2250},
            ],
        )
        self.assertEqual(
            self.argv("below"),
            [
                *("dump", "process", "-b", "100", "-e", "200", "-O", "json", "--raw", "-f", "timestamp", "pid"),
                *("comm", "cgroup", "io.rbytes_per_sec", "io.wbytes_per_sec"),
                *("-s", "io.wbytes_per_sec", "--rsort", "--top", "3"),
            ],
        )

    def test_below_output_in_several_documents(self) -> None:
        # Not one JSON document: each line that starts a list or an object is read on its own, one
        # that does not parse is dropped; nested lists are flattened and other values skipped. A
        # row without a name is "?", the columns are found by part of their name, and a rate that
        # is no number counts 0.
        lines = (
            '{"comm": "a", "cgroup": "/x/y.service", "io.rbytes_per_sec": 2}',
            '[{"comm": "b", "io.rbytes_per_sec": "4 B/s"}, 7]',
            "{bad",
            '[[{"name": "c", "Cgroup": "/z/", "Read": 1}]]',
            '{"io.rbytes_per_sec": 3}',
            '{"comm": "d", "io.rbytes_per_sec": "n/a"}',
        )
        self.fake("below", "\n".join(lines).encode())
        self.assertEqual(
            rp_adapters.below_top(1, 2, "io.rbytes_per_sec"),
            (
                [
                    {"comm": "b", "cgroup": "", "bytes": 60},
                    {"comm": "?", "cgroup": "", "bytes": 45},
                    {"comm": "a", "cgroup": "y.service", "bytes": 30},
                    {"comm": "c", "cgroup": "z", "bytes": 15},
                ],
                None,
            ),
        )
        self.fake("below", b"")
        self.assertEqual(rp_adapters.below_top(1, 2, "io.rbytes_per_sec"), ([], None))

    def _below_bytes(self, *rows: str) -> list[rp_records.BelowItem] | None:
        self.fake("below", ("[" + ", ".join(rows) + "]").encode())
        items, err = rp_adapters.below_top(1, 2, "io.rbytes_per_sec")
        self.assertIsNone(err)
        return items

    def test_a_row_without_a_time_counts_below_s_15_s_interval(self) -> None:
        self.assertEqual(
            self._below_bytes('{"comm": "a", "io.rbytes_per_sec": 10}'), [{"comm": "a", "cgroup": "", "bytes": 150}]
        )

    def test_non_finite_rates_count_zero_and_non_finite_times_are_no_time(self) -> None:
        # parse_json reads NaN and Infinity; a NaN rate made the total NaN, and int() of it raised.
        self.assertEqual(
            self._below_bytes(
                '{"comm": "a", "io.rbytes_per_sec": NaN}',
                '{"comm": "b", "io.rbytes_per_sec": Infinity}',
                '{"comm": "c", "io.rbytes_per_sec": 2}',
            ),
            [{"comm": "c", "cgroup": "", "bytes": 30}],
        )
        self.assertEqual(
            self._below_bytes(
                '{"comm": "a", "timestamp": 1000, "io.rbytes_per_sec": 1}',
                '{"comm": "a", "timestamp": 1030, "io.rbytes_per_sec": 1}',
                '{"comm": "a", "timestamp": NaN, "io.rbytes_per_sec": 1}',
                '{"comm": "a", "timestamp": "inf", "io.rbytes_per_sec": 1}',
            ),
            [{"comm": "a", "cgroup": "", "bytes": 120}],  # 30 s each: the typical gap of the usable times
        )

    def test_a_time_without_a_zone_is_local_time(self) -> None:
        patcher = mock.patch.dict(os.environ, {"PATH": self.bin, "TZ": "UTC+3"})  # local time is UTC-3
        patcher.start()
        self.addCleanup(time.tzset)
        self.addCleanup(patcher.stop)
        time.tzset()
        # 10:00:00-03:00 and 10:00:15 local are 15 s apart (read as UTC, 3 h minus 15 s: no usable gap).
        self.assertEqual(
            self._below_bytes(
                '{"comm": "a", "timestamp": "2026-11-22T10:00:00-03:00", "io.rbytes_per_sec": 1}',
                '{"comm": "a", "timestamp": "2026-11-22T10:00:15", "io.rbytes_per_sec": 1}',
            ),
            [{"comm": "a", "cgroup": "", "bytes": 30}],
        )

    def test_a_late_sample_counts_its_whole_gap(self) -> None:
        # below's rates of a sample cover the time since the one before: a gap up to 300 s counts in full
        # (a sample written late during a stall is where the IO was); longer gaps count the typical one.
        self.assertEqual(
            self._below_bytes(
                *(
                    f'{{"comm": "a", "timestamp": {ts}, "io.rbytes_per_sec": 1}}'
                    for ts in (1000, 1015, 1030, 1150, 2000)
                )
            ),
            [{"comm": "a", "cgroup": "", "bytes": 15 + 15 + 15 + 120 + 15}],
        )

    def test_a_failed_below_is_the_error_of_the_section(self) -> None:
        self.fake("below", b"ignored", err="x\n", rc=1)
        self.assertEqual(rp_adapters.below_top(1, 2, "io.rbytes_per_sec"), (None, "below dump failed: x"))
        self.fake("below", b"only stdout\n", rc=1)
        self.assertEqual(rp_adapters.below_top(1, 2, "io.rbytes_per_sec"), (None, "below dump failed: only stdout"))

    def test_a_missing_below_is_the_error_of_the_section(self) -> None:
        self.assertEqual(
            rp_adapters.below_top(1, 2, "io.rbytes_per_sec"),
            (None, "below dump failed: [Errno 2] No such file or directory: 'below'"),
        )

    def test_output_that_is_not_utf8_is_read_with_replacement_characters(self) -> None:
        # A process or container name with bytes that are not UTF-8 used to end the report with
        # UnicodeDecodeError; it is now read with U+FFFD for each bad byte.
        self.fake("below", b'[{"comm": "a\xffb", "cgroup": "/system.slice/a.service", "io.rbytes_per_sec": 2}]')
        self.assertEqual(
            rp_adapters.below_top(1, 2, "io.rbytes_per_sec"),
            ([{"comm": "a\ufffdb", "cgroup": "a.service", "bytes": 30}], None),
        )
        self.fake("docker", b"0123456789abcdef\tw\xffeb\n")
        self.assertEqual(rp_adapters.container_names(), {"docker-0123456789ab": "w\ufffdeb"})

    def test_below_top_integrates_actual_timestamp_intervals(self) -> None:
        # 15 s interval (e.g. below --interval 15) across epoch timestamps + ISO timestamps, plus a gap > 300 s.
        rows = [
            {"timestamp": 1000, "comm": "worker", "cgroup": "/builds.slice", "io.wbytes_per_sec": 100},
            {"timestamp": 1015, "comm": "worker", "cgroup": "/builds.slice", "io.wbytes_per_sec": 100},
            {"timestamp": 1030, "comm": "worker", "cgroup": "/builds.slice", "io.wbytes_per_sec": 100},
            {"timestamp": 1600, "comm": "worker", "cgroup": "/builds.slice", "io.wbytes_per_sec": 100},
        ]
        self.fake("below", json.dumps(rows).encode())
        items, err = rp_adapters.below_top(1000, 1600, "io.wbytes_per_sec")
        self.assertIsNone(err)
        self.assertEqual(items, [{"comm": "worker", "cgroup": "builds.slice", "bytes": 6000}])

        iso_rows = [
            {"timestamp": "2026-11-22T12:00:00Z", "comm": "iso", "cgroup": "/user.slice", "io.wbytes_per_sec": 200},
            {"timestamp": "2026-11-22T12:00:30Z", "comm": "iso", "cgroup": "/user.slice", "io.wbytes_per_sec": 200},
        ]
        self.fake("below", json.dumps(iso_rows).encode())
        items2, err2 = rp_adapters.below_top(1, 2, "io.wbytes_per_sec")
        self.assertIsNone(err2)
        self.assertEqual(items2, [{"comm": "iso", "cgroup": "user.slice", "bytes": 12000}])


if __name__ == "__main__":
    unittest.main()

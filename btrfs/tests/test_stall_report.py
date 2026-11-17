# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the zswap numbers of btrfs/btrfs-stall-report (no root needed).

Daemons before version 7 read only the first page of /proc/vmstat, so their
samples hold 0 for zswpin, zswpout and zswpwb. The first sample of a v7
daemon holds the real counter since boot. A report window that spans the
upgrade must not add that whole counter as if zswap wrote it in a few
seconds. The records here are built in memory; nothing reads the real
/var/log/btrfs-stall-monitor or runs below.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_stall_report -v
"""

from __future__ import annotations

import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, Protocol, cast

# Run directly (python3 -I -B btrfs/tests/test_stall_report.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from btrfs.stall_monitord import domain as md_domain
from btrfs.stall_report import adapters as rp_adapters
from btrfs.stall_report import analysis as rp_analysis
from btrfs.stall_report import cli as rp_cli
from btrfs.stall_report import domain as rp_domain
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
    view = PackageView(rp_cli, rp_analysis, rp_adapters, rp_domain)
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
        self.assertLessEqual({"d_mem", "d_maint", "vm", "mono"}, rp_domain.SampleRecord.__optional_keys__)
        self.assertLessEqual({"max_d_mem", "max_d_all"}, rp_domain.EpisodeRecord.__optional_keys__)
        self.assertIn("psi", rp_domain.SampleRecord.__required_keys__)


class ZswapAcrossUpgradeTest(unittest.TestCase):
    m: _StallReportModule

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
        line = next(l for l in text.splitlines() if l.startswith("memory"))
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

    def setUp(self) -> None:
        self.m = load()

    def run_report(
        self, recs: Sequence[Mapping[str, Any]], until: float | None = None
    ) -> tuple[dict[str, Any], list[str], str]:
        until = until or max(r.get("ts", 0) for r in recs)
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
        line = next(l for l in verdict if l.startswith("btrfs disks"))
        self.assertNotIn("SATURATED", line)
        self.assertIn("NOT saturated", line)
        self.assertIn("dm-crypt layer (what btrfs waits for) await 1.1 ms (read 1.1, write 1.1)", line)
        # The NVMe figure is still shown, read and write apart, as information only.
        self.assertIn("NVMe below await 6.8 ms (read 0.2, write 7.5), information only", line)
        self.assertNotIn("encryption queueing", line)
        self.assertIn("(r 0.2 / w 7.5)", text)  # the disks section splits read and write too

    def test_saturated_dm_layer(self) -> None:
        _, verdict, _ = self.run_report(self.recs(QUIET_NVME, BUSY_DM))
        line = next(l for l in verdict if l.startswith("btrfs disks"))
        self.assertIn("look SATURATED", line)
        self.assertIn("await 15.0 ms (read 15.0, write 15.0)", line)
        self.assertIn("encryption queueing adds latency", line)

    def test_without_dm_crypt_uses_the_member_disks(self) -> None:
        recs = [meta_full(T0)] + [full_sample(T0 + 5 * i, i, disks={"nvme0n1": BUSY_DM}, d_mem=0) for i in range(13)]
        recs[0]["disks"] = {"nvme0n1": "btrfs:fedora"}
        recs.append(episode(T0 + 10, T0 + 30, 3, [("btrfs_sync_log", 30, "storage")], max_d_mem=0))
        _, verdict, _ = self.run_report(recs)
        line = next(l for l in verdict if l.startswith("btrfs disks"))
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
                l.startswith(
                    "Memory, not storage, blocked up to 74 tasks (memory reclaim, swap-in or zswap; "
                    "estimated from the stacks: recorded by a daemon < v8)"
                )
                for l in verdict
            )
        )
        self.assertFalse(any(l.startswith("Blocked tasks") for l in verdict))
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
        self.assertTrue(any(l.startswith("Blocked tasks: up to 6 tasks waiting on storage") for l in verdict), verdict)
        self.assertFalse(any(l.startswith("Memory") for l in verdict))
        self.assertEqual([c["category"] for c in res["categories"]], ["log-sync"])

    def test_v7_storage_episode_unchanged(self) -> None:
        recs = [meta_full(T0, 7)] + [full_sample(T0 + i, i, dt=1.0, d=5) for i in range(30)]
        recs.append(episode(T0 + 2, T0 + 29, 5, [("btrfs_sync_log", 100, "storage")]))
        res, verdict, _ = self.run_report(recs)
        self.assertEqual(res["episodes"][0]["max_d"], 5)
        self.assertNotIn("estimated", res["episodes"][0])
        self.assertEqual(res["peak"]["d_tasks"], 5)
        self.assertTrue(any(l.startswith("Blocked tasks: up to 5 tasks") for l in verdict))

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
        line = next(l for l in text.splitlines() if l.startswith("memory"))
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


class ClassifyBranchesTest(unittest.TestCase):
    """Each way classify() decides, one case per branch (the mutation run left them unchecked)."""

    def test_cases(self) -> None:
        c = rp_domain.classify
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


if __name__ == "__main__":
    unittest.main()

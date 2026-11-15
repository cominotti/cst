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

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "btrfs-stall-report")
VM_KEYS = ["nr_dirty", "nr_writeback", "pswpin", "pswpout", "zswpin", "zswpout", "zswpwb"]
T0 = 1_700_000_000.0
BOOT_ZSWPWB = 6_837_407        # pages: the real counter since boot on the test machine


def load():
    loader = importlib.machinery.SourceFileLoader("btrfs_stall_report", TOOL)
    spec = importlib.util.spec_from_loader("btrfs_stall_report", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def meta(ts, v):
    return {"t": "meta", "ts": ts, "v": v, "vm_keys": VM_KEYS, "base_interval": 5.0,
            "kernel": "7.2.0", "disks": {}}


def sample(ts, pswpout, zswpwb):
    zswpin = zswpout = zswpwb
    return {"t": "s", "ts": ts, "mono": ts - T0 + 100,
            "psi": {"io": [0, 0, 0, 0], "memory": [0, 0, 0, 0], "cpu": [0, 0, 0, 0]},
            "procs": [1, 0], "vm": [0, 0, 0, pswpout, zswpin, zswpout, zswpwb],
            "fs": {}, "disk": {}, "cg": {}, "d": 0, "d_all": 0, "d_maint": 0}


def old_run(n=10):
    """A v6 daemon: zswap counters always 0, pswpout grows 10 pages per sample."""
    return [meta(T0, 6)] + [sample(T0 + 5 * i, 1000 + 10 * i, 0) for i in range(n)]


def new_run(start, n=10, first=BOOT_ZSWPWB):
    """A v7 daemon: real zswpwb, 5 pages written back per sample."""
    return [meta(start, 7)] + [sample(start + 1 + 5 * i, 2000 + 10 * i, first + 5 * i) for i in range(n)]


class ZswapAcrossUpgradeTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def analyse(self, recs):
        """(res, meta) of analyse() and the printed report, from one analysis."""
        res, meta_ = self.m.analyse(recs, T0, max(r["ts"] for r in recs), with_below=False, verbose=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.print_report(res, meta_, verbose=False)
        return res, out.getvalue()

    def test_only_old_daemon_shows_na(self):
        res, text = self.analyse(old_run())
        self.assertIsNone(res["vm"]["zswap_writeback_bytes"])
        self.assertEqual(res["vm"]["swap_out_bytes"], 90 * self.m.PAGE)   # other counters still count
        self.assertIn("zswap writeback n/a (daemon < v7)", text)

    def test_window_across_upgrade_counts_only_v7_pairs(self):
        old = old_run()
        recs = old + new_run(old[-1]["ts"] + 3)        # restart: 3 s gap, under GAP_MAX
        res, text = self.analyse(recs)
        # 9 pairs of the new daemon, 5 pages each; not the counter since boot.
        self.assertEqual(res["vm"]["zswap_writeback_bytes"], 45 * self.m.PAGE)
        self.assertEqual(res["vm"]["zswap_counted_from"], old[-1]["ts"] + 3)
        self.assertRegex(text, r"zswap writeback 0 MiB \(from \d\d:\d\d:\d\d\)")
        self.assertNotIn("GiB", text)

    def test_only_new_daemon_counts_normally(self):
        recs = new_run(T0)
        res, text = self.analyse(recs)
        self.assertEqual(res["vm"]["zswap_writeback_bytes"], 45 * self.m.PAGE)
        self.assertIsNone(res["vm"]["zswap_counted_from"])
        line = next(l for l in text.splitlines() if l.startswith("memory"))
        self.assertIn("zswap writeback 0 MiB  ", line)
        self.assertNotIn("from", line)

    def test_new_daemon_counts_large_writeback(self):
        recs = [meta(T0, 7), sample(T0 + 1, 0, BOOT_ZSWPWB), sample(T0 + 6, 0, BOOT_ZSWPWB + 2560)]
        res, text = self.analyse(recs)
        self.assertEqual(res["vm"]["zswap_writeback_bytes"], 2560 * self.m.PAGE)   # 10 MiB
        self.assertIn("zswap writeback 10 MiB", text)


if __name__ == "__main__":
    unittest.main()

"""Unit tests for swap/swap-status (pure maths and parsing) and for the
marker handling of swap/swap-lib.bash (on scratch files; no root needed).

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s swap/tests -t . -v
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
SWAP = os.path.dirname(HERE)
REPO = os.path.dirname(SWAP)
STATUS = os.path.join(SWAP, "swap-status")
LIB = os.path.join(SWAP, "swap-lib.bash")
SETUP = os.path.join(REPO, "swap-setup.sh")

OLD = "# managed-by: apply-swap-config.sh"
OLDER = "# managed-by: apply-zram-config.sh"
NEW = "# managed-by: swap-setup.sh (CST)"

# What apply-swap-config.sh wrote on the host (fstab line verbatim; the
# crypttab line is the one that script builds).
OLD_FSTAB = f"/dev/mapper/swap0 none swap defaults,nofail,discard 0 0 {OLD}"
OLD_CRYPTTAB = (
    "swap0 /dev/disk/by-partuuid/612551b1-ca02-4514-86ad-7d8902e612dc /dev/urandom "
    f"swap,cipher=aes-xts-plain64,size=512,sector-size=4096,discard {OLD}"
)
OLD_TMPFILES = """\
# Managed by apply-swap-config.sh — enable zswap at boot.
# Written via tmpfiles.d (not kernel args) so the zstd module is loadable
# when the compressor is selected; see script header.
w /sys/module/zswap/parameters/compressor       - - - - zstd
w /sys/module/zswap/parameters/max_pool_percent - - - - 20
w /sys/module/zswap/parameters/shrinker_enabled - - - - 1
w /sys/module/zswap/parameters/enabled          - - - - 1
"""
OLD_DROPIN = """\
# Managed by apply-swap-config.sh — protect libvirt guests from reclaim.
# Soft floor: reclaim prefers unprotected cgroups while they have anything
# reclaimable; ignored when they don't. Propagates to the VM scope via the
# memory_recursiveprot cgroup2 mount option.
[Slice]
MemoryLow=48G
"""


def load_status():
    loader = importlib.machinery.SourceFileLoader("swap_status", STATUS)
    spec = importlib.util.spec_from_loader("swap_status", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class StatusMathTest(unittest.TestCase):
    def setUp(self):
        self.m = load_status()

    def test_meminfo_in_bytes(self):
        d = self.m.parse_meminfo("MemTotal:  1000 kB\nZswap:  10 kB\nHugePages_Total:  3\n")
        self.assertEqual(d["MemTotal"], 1024000)
        self.assertEqual(d["Zswap"], 10240)
        self.assertEqual(d["HugePages_Total"], 3)

    def test_kv_skips_non_numbers(self):
        self.assertEqual(self.m.parse_kv("zswpout 5\nbad x\nshort\n"), {"zswpout": 5})

    def test_summary_no_bypass(self):
        gib = 2**30
        s = self.m.zswap_summary(
            {"MemTotal": 100 * gib, "Zswap": 2 * gib, "Zswapped": 6 * gib},
            {"zswpout": 1000, "zswpin": 250, "zswpwb": 400, "pswpout": 400, "pswpin": 100},
            {"max_pool_percent": "20", "accept_threshold_percent": "90"},
        )
        self.assertEqual(s["limit"], 20 * gib)
        self.assertAlmostEqual(s["pool_of_limit"], 0.1)
        self.assertEqual(s["accept_below"], 18 * gib)
        self.assertAlmostEqual(s["ratio"], 3.0)
        self.assertAlmostEqual(s["loaded_back"], 0.25)
        self.assertAlmostEqual(s["written_back"], 0.4)
        self.assertEqual(s["bypassed"], 0)
        self.assertAlmostEqual(s["read_back"], 0.25)  # = pswpin/zswpwb here

    def test_summary_bypass_and_empty(self):
        s = self.m.zswap_summary({"MemTotal": 0}, {"pswpout": 50, "zswpwb": 20}, {})
        self.assertEqual(s["bypassed"], 30)
        self.assertIsNone(s["written_back"])  # nothing went into zswap
        self.assertIsNone(s["limit"])
        self.assertIsNone(s["pool"])
        self.assertIsNone(s["ratio"])
        self.assertEqual(self.m.pct(None), "-")

    def test_summary_accept_below_needs_both_values(self):
        # accept_below = accept_threshold_percent of the pool limit: without
        # either value there is nothing to show, not a crash or a guess.
        gib = 2**30
        meminfo = {"MemTotal": 100 * gib, "Zswap": 2 * gib, "Zswapped": 6 * gib}
        for params, limit in (
            ({"max_pool_percent": "20"}, 20 * gib),
            ({"max_pool_percent": "20", "accept_threshold_percent": "?"}, 20 * gib),
            ({"accept_threshold_percent": "90"}, None),
        ):
            with self.subTest(params=params):
                s = self.m.zswap_summary(meminfo, {}, params)
                self.assertEqual(s["limit"], limit)
                self.assertIsNone(s["accept_below"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.show_pool({"max_pool_percent": "20"}, self.m.zswap_summary(meminfo, {}, {"max_pool_percent": "20"}))
        self.assertIn("pool in RAM: 2.0 GiB of a 20.0 GiB limit (10.0 %)", out.getvalue())
        self.assertNotIn("takes new pages again", out.getvalue())

    def test_cgroup_row(self):
        r = self.m.cgroup_row({"zswpout": 10, "zswpwb": 5, "pswpout": 5, "pswpin": 1, "zswapped": 4096}, 123)
        self.assertEqual(r["swap"], 123)
        self.assertEqual(r["zswpwb"], 5 * self.m.PAGE)
        self.assertEqual(r["zswpout"], 10 * self.m.PAGE)  # pages -> bytes, like zswpwb
        self.assertEqual(r["pswpin"], 1 * self.m.PAGE)
        self.assertEqual(r["zswapped"], 4096)  # memory.stat sizes are bytes already
        self.assertAlmostEqual(r["written_back"], 0.5)
        self.assertAlmostEqual(r["read_back"], 0.2)
        self.assertIsNone(self.m.cgroup_row({}, None)["read_back"])

    def test_summary_full_result_and_types(self):
        # Sizes are whole bytes (int, rounded down), shares are float. 20 % of
        # 1023 KiB and 85 % of that limit do not divide evenly.
        s = self.m.zswap_summary(
            {"MemTotal": 1023 * 1024, "Zswap": 41902, "Zswapped": 146657},
            {"zswpout": 800, "zswpin": 200, "zswpwb": 300, "pswpout": 500, "pswpin": 125},
            {"max_pool_percent": "20", "accept_threshold_percent": "85"},
        )
        self.assertEqual(
            s,
            {
                "pool": 41902,
                "stored": 146657,
                "limit": 209510,  # 209510.4
                "pool_of_limit": 0.2,
                "accept_below": 178083,  # 178083.5
                "ratio": 3.5,
                "zswpout": 800,
                "zswpin": 200,
                "zswpwb": 300,
                "pswpout": 500,
                "pswpin": 125,
                "loaded_back": 0.25,
                "written_back": 0.375,
                "bypassed": 200,
                "read_back": 0.25,
            },
        )
        self.assertIs(type(s["limit"]), int)
        self.assertIs(type(s["accept_below"]), int)
        self.assertIs(type(s["ratio"]), float)
        self.assertIs(type(s["pool_of_limit"]), float)

    def test_summary_empty_pool_and_missing_stored(self):
        # None where the divisor is 0 or a value is missing (ZswapSummary).
        meminfo = {"MemTotal": 1023 * 1024, "Zswap": 0, "Zswapped": 0}
        s = self.m.zswap_summary(meminfo, {}, {"max_pool_percent": "20"})
        self.assertEqual((s["pool"], s["stored"], s["limit"]), (0, 0, 209510))
        self.assertEqual(s["pool_of_limit"], 0.0)
        self.assertIs(type(s["pool_of_limit"]), float)
        self.assertIsNone(s["ratio"])
        s = self.m.zswap_summary({"MemTotal": 1023 * 1024, "Zswap": 41902}, {}, {"max_pool_percent": "20"})
        self.assertIsNone(s["stored"])
        self.assertIsNone(s["ratio"])
        self.assertEqual(s["pool_of_limit"], 0.2)

    def test_summary_bypassed_is_never_negative(self):
        # bypassed = pswpout - zswpwb, at least 0 (ZswapSummary).
        s = self.m.zswap_summary({}, {"zswpout": 20, "zswpwb": 12, "pswpout": 10}, {})
        self.assertEqual(s["bypassed"], 0)
        self.assertEqual(s["written_back"], 0.6)

    def test_summary_missing_keys(self):
        """Pins current behaviour: a counter missing from vmstat reads as 0, and a
        missing MemTotal gives a limit of 0 bytes, not None (neither the
        docstrings nor swap/README.md say what a missing key means)."""
        self.assertEqual(
            self.m.zswap_summary({}, {}, {}),
            {
                "pool": None,
                "stored": None,
                "limit": None,
                "pool_of_limit": None,
                "accept_below": None,
                "ratio": None,
                "zswpout": 0,
                "zswpin": 0,
                "zswpwb": 0,
                "pswpout": 0,
                "pswpin": 0,
                "loaded_back": None,
                "written_back": None,
                "bypassed": 0,
                "read_back": None,
            },
        )
        # 100 %: a MemTotal that defaulted to anything but 0 would show in the limit.
        s = self.m.zswap_summary({}, {}, {"max_pool_percent": "100", "accept_threshold_percent": "90"})
        self.assertEqual((s["limit"], s["accept_below"], s["pool_of_limit"]), (0, 0, None))

    def test_cgroup_row_full_result_and_types(self):
        page = self.m.PAGE
        r = self.m.cgroup_row(
            {"zswpout": 8, "zswpwb": 3, "pswpout": 4, "pswpin": 1, "zswap": 1000, "zswapped": 3000}, 5000
        )
        self.assertEqual(
            r,
            {
                "swap": 5000,
                "zswap": 1000,
                "zswapped": 3000,
                "zswpout": 8 * page,
                "zswpwb": 3 * page,
                "pswpin": 1 * page,
                "written_back": 0.375,
                "read_back": 0.25,
            },
        )
        for key in ("zswpout", "zswpwb", "pswpin"):
            self.assertIs(type(r[key]), int, key)

    def test_cgroup_row_missing_keys(self):
        """Pins current behaviour: a counter missing from memory.stat reads as 0,
        a missing size as None (the docstring does not say)."""
        self.assertEqual(
            self.m.cgroup_row({}, None),
            {
                "swap": None,
                "zswap": None,
                "zswapped": None,
                "zswpout": 0,
                "zswpwb": 0,
                "pswpin": 0,
                "written_back": None,
                "read_back": None,
            },
        )
        # pswpout alone: nothing was read back, a share of 0, not None.
        self.assertEqual(self.m.cgroup_row({"pswpout": 4}, None)["read_back"], 0.0)

    def test_desired_files(self):
        with open(self.m.TMPFILES) as f:
            params = self.m.parse_tmpfiles_params(f.read())
        names = [p for p, _ in params]
        # compressor before enabled: the first pool is zstd from the start
        self.assertEqual(names[0], "compressor")
        self.assertEqual(names[-1], "enabled")
        with open(self.m.SYSCTL) as f:
            self.assertIn(("vm.swappiness", "100"), self.m.parse_sysctl(f.read()))
        with open(self.m.DROPIN) as f:
            self.assertEqual(self.m.parse_memory_low(f.read()), 48 * 2**30)

    def settings_output(self, wsf, exp_on):
        """show_settings against a fake /proc/sys and cpu-setup run folder."""
        tmp = tempfile.mkdtemp(prefix="swap-test-")
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "proc", "sys", "vm"))
        with open(self.m.SYSCTL) as f:
            for key, want in self.m.parse_sysctl(f.read()):
                with open(os.path.join(tmp, "proc", "sys", key.replace(".", "/")), "w") as g:
                    g.write((wsf if key == "vm.watermark_scale_factor" else want) + "\n")
        run = os.path.join(tmp, "run")
        os.makedirs(run)
        if exp_on:
            open(os.path.join(run, "exp-wsf-300"), "w").close()
        self.m.PROC, self.m.CGROUP, self.m.CPU_RUN = (os.path.join(tmp, "proc"), os.path.join(tmp, "cg"), run)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.show_settings({})
        return [
            l.split()[:2]
            for l in out.getvalue().splitlines()
            if "watermark_scale" in l and not l.lstrip().startswith("exp:")
        ], out.getvalue()

    def test_wsf_experiment_is_not_a_diff(self):
        rows, text = self.settings_output("300", exp_on=True)
        self.assertEqual(rows, [["exp", "vm.watermark_scale_factor"]])
        self.assertIn("experiment off wsf-300", text)
        self.assertNotIn("  DIFF", text)  # apply's verification passes
        rows, text = self.settings_output("300", exp_on=False)
        self.assertEqual(rows, [["DIFF", "vm.watermark_scale_factor"]])
        self.assertNotIn("exp:", text)
        rows, _ = self.settings_output("125", exp_on=True)
        self.assertEqual(rows, [["ok", "vm.watermark_scale_factor"]])

    def test_cpu_run_folder_in_a_toolbox(self):
        # Inside a container /run is its own; the host's records are under /run/host.
        want = "/run/host/run/cpu-setup" if os.path.exists("/run/.containerenv") else "/run/cpu-setup"
        self.assertEqual(self.m.CPU_RUN, want)

    def test_debug_reject_labels(self):
        # The kernel names are misleading (mm/zswap.c): reject_compress_fail
        # counts pages that did not compress while their cgroup had zswap
        # writeback off (the game profile), and reject_reclaim_fail counts
        # failed shrinker writebacks, not a full pool.
        help_ = self.m.DEBUG_HELP
        self.assertIn("writeback off", help_["reject_compress_fail"])
        self.assertIn("writeback off", help_["reject_compress_poor"])
        self.assertIn("shrinker", help_["reject_reclaim_fail"])

        tmp = tempfile.mkdtemp(prefix="swap-test-")
        self.addCleanup(shutil.rmtree, tmp)
        for name, val in (
            ("reject_compress_fail", "164000"),
            ("reject_reclaim_fail", "42"),
            ("pool_limit_hit", "0"),
            ("new_kernel_counter", "7"),
        ):
            with open(os.path.join(tmp, name), "w") as f:
                f.write(val + "\n")
        self.m.ZSWAP_DEBUG = tmp
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.show_debug("")
        lines = {l.split()[0]: l for l in out.getvalue().splitlines()[1:] if l.strip()}
        self.assertIn("164000", lines["reject_compress_fail"])
        self.assertTrue(lines["reject_compress_fail"].endswith(help_["reject_compress_fail"]))
        self.assertTrue(lines["reject_reclaim_fail"].endswith(help_["reject_reclaim_fail"]))
        self.assertEqual(lines["new_kernel_counter"].split(), ["new_kernel_counter", "7"])

    def test_size_and_bool(self):
        self.assertEqual(self.m.parse_size("512M"), 512 * 2**20)
        self.assertEqual(self.m.parse_size("1024"), 1024)
        self.assertIsNone(self.m.parse_size("infinity"))
        self.assertTrue(self.m.same_param("Y", "1"))
        self.assertFalse(self.m.same_param("N", "1"))
        self.assertTrue(self.m.same_param("zstd", "zstd"))


class MarkerTest(unittest.TestCase):
    """swap-lib.bash on scratch files, with the lines swap-setup.sh defines."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="swap-test-")
        with open(SETUP) as f:
            self.defs = "".join(
                l
                for l in f
                if l.startswith(
                    (
                        "DISK_SWAP_PARTUUID=",
                        "DISK_SWAP_NAME=",
                        "DISK_SWAP_MAPPER=",
                        "DISK_SWAP_DEV=",
                        "CRYPTTAB_LINE=",
                        "FSTAB_LINE=",
                    )
                )
            )

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def bash(self, script, *args):
        r = subprocess.run(
            ["bash", "-c", f'set -euo pipefail; source "$0"; {self.defs}\n{script}', LIB, *args],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        return r.stdout.strip()

    def path(self, name, text=None):
        p = os.path.join(self.tmp, name)
        if text is not None:
            with open(p, "w") as f:
                f.write(text)
        return p

    def state(self, p, which="fstab"):
        line, key = ("$FSTAB_LINE", "$DISK_SWAP_MAPPER") if which == "fstab" else ("$CRYPTTAB_LINE", "$DISK_SWAP_NAME")
        return self.bash(f'marked_line_state "$1" "{line}" "{key}"', p)

    def write(self, p, which="fstab"):
        line = "$FSTAB_LINE" if which == "fstab" else "$CRYPTTAB_LINE"
        self.bash(f'write_marked_line "$1" "{line}" 0644', p)

    def test_old_host_lines_are_adopted_in_place(self):
        for which, old in (("fstab", OLD_FSTAB), ("crypttab", OLD_CRYPTTAB)):
            p = self.path(which, f"# head\nUUID=x / btrfs defaults 0 0\n{old}\n# tail\n")
            self.assertEqual(self.state(p, which), "adopt")
            self.write(p, which)
            with open(p) as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 4)  # no duplicate
            self.assertEqual(lines[2], old.replace(OLD, NEW))  # same place
            self.assertEqual(self.state(p, which), "ok")
            with open(p + ".pre-swap-config.bak") as f:
                self.assertIn(OLD, f.read())

    def test_zram_marker_is_adopted(self):
        p = self.path("fstab", OLD_FSTAB.replace(OLD, OLDER) + "\n")
        self.assertEqual(self.state(p), "adopt")

    def test_missing_change_duplicate_conflict(self):
        p = self.path("fstab", "UUID=x / btrfs defaults 0 0\n#/dev/mapper/swap0 old note\n")
        self.assertEqual(self.state(p), "missing")
        self.write(p)
        self.assertEqual(self.state(p), "ok")
        with open(p) as f:
            self.assertTrue(f.read().endswith(NEW + "\n"))
        self.path("fstab", OLD_FSTAB.replace("discard", "pri=5") + "\n")
        self.assertEqual(self.state(p), "change")
        # The same entry twice (the old script ran again after the move): only
        # a duplicate, not a change of the entry.
        self.path("fstab", f"{OLD_FSTAB.replace(OLD, NEW)}\n{OLD_FSTAB}\n")
        self.assertEqual(self.state(p), "duplicate")
        self.write(p)
        with open(p) as f:
            self.assertEqual(f.read().count("swap0"), 1)
        self.assertEqual(self.state(p), "ok")
        # Twice, and one of them differs: a real change.
        self.path("fstab", f"{OLD_FSTAB}\n{OLD_FSTAB.replace('discard', 'pri=5')}\n")
        self.assertEqual(self.state(p), "change")
        self.write(p)
        with open(p) as f:
            self.assertEqual(f.read().count("swap0"), 1)
        self.path("fstab", "/dev/mapper/swap0 none swap defaults 0 0\n")
        self.assertEqual(self.state(p), "conflict")
        self.assertEqual(self.state(self.path("absent")), "missing")

    def test_backup_taken_once(self):
        p = self.path("fstab", "first\n")
        self.write(p)
        self.path("fstab", "second\n")
        self.write(p)
        with open(p + ".pre-swap-config.bak") as f:
            self.assertEqual(f.read(), "first\n")

    def test_file_state(self):
        src_tmp = os.path.join(SWAP, "tmpfiles", "zswap.conf")
        src_drop = os.path.join(SWAP, "units", "machine.slice.d-50-memory-low.conf")
        fs = lambda s, d: self.bash('file_state "$1" "$2"', s, d)  # noqa: E731
        self.assertEqual(fs(src_tmp, self.path("t", OLD_TMPFILES)), "adopt")
        self.assertEqual(fs(src_drop, self.path("d", OLD_DROPIN)), "adopt")
        self.assertEqual(fs(src_drop, self.path("d2", OLD_DROPIN.replace("48G", "32G"))), "change")
        self.assertEqual(fs(src_tmp, self.path("none")), "missing")
        shutil.copy(src_tmp, self.path("same"))
        self.assertEqual(fs(src_tmp, self.path("same")), "ok")
        os.symlink(self.path("same"), self.path("link"))
        self.assertEqual(fs(src_tmp, self.path("link")), "change")
        dest = self.path("sub/dir/new")
        self.bash('write_file "$1" "$2" 0644', src_tmp, dest)
        self.assertEqual(fs(src_tmp, dest), "ok")
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o644)

    def test_sysctl_payload_matches_old_script(self):
        # Only the header comment changed: same settings, same order.
        src = os.path.join(SWAP, "sysctl", "99-swap-tuning.conf")
        old = self.path(
            "s",
            "# Managed by apply-swap-config.sh — VM tunables\n"
            "vm.swappiness = 100\n\nvm.page-cluster = 0\n"
            "vm.watermark_boost_factor = 0\nvm.watermark_scale_factor = 125\n",
        )
        self.assertEqual(self.bash('file_state "$1" "$2"', src, old), "adopt")


if __name__ == "__main__":
    unittest.main()

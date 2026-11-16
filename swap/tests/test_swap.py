"""Unit tests for swap domain, presenter, application service, and CLI.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s swap/tests -t . -v
"""

from collections.abc import Mapping, Sequence
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import NoReturn
import unittest

sys.dont_write_bytecode = True

from cst_core.adapters import HostExecutionContext, LocalFileSystemAdapter
from cst_core.domain.files import MarkedLineSpec, inspect_managed_file
from cst_core.ports import CommandResult
from swap.application import MissingDesiredFileError, SwapApplicationService, SwapEnvironment
from swap.cli import execute_cli, format_usage
import swap.domain as D
from swap.domain import (
    DEBUG_HELP,
    PAGE,
    SwapHardwareSpec,
    cgroup_row,
    parse_kv,
    parse_meminfo,
    parse_memory_low,
    parse_size,
    parse_sysctl,
    parse_tmpfiles_params,
    pct,
    same_param,
    share,
    zswap_summary,
)
from swap.presenter import render_apply, render_debug, render_plan, render_pool, render_settings, render_status

HERE = os.path.dirname(os.path.abspath(__file__))
SWAP = os.path.dirname(HERE)
REPO = os.path.dirname(SWAP)
SETUP = os.path.join(REPO, "swap-setup.sh")
TMPFILES = os.path.join(SWAP, "tmpfiles", "zswap.conf")
SYSCTL = os.path.join(SWAP, "sysctl", "99-swap-tuning.conf")
DROPIN = os.path.join(SWAP, "units", "machine.slice.d-50-memory-low.conf")

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


# 1023 KiB of RAM: 20 % of it, and 85 % of that, do not divide evenly.
ODD_RAM = 1023 * 1024


class StatusMathTest(unittest.TestCase):
    m = D

    def test_meminfo_in_bytes(self) -> None:
        d = parse_meminfo("MemTotal:  1000 kB\nZswap:  10 kB\nHugePages_Total:  3\n")
        self.assertEqual(d["MemTotal"], 1024000)
        self.assertEqual(d["Zswap"], 10240)
        self.assertEqual(d["HugePages_Total"], 3)

    def test_kv_skips_non_numbers(self) -> None:
        self.assertEqual(parse_kv("zswpout 5\nbad x\nshort\n"), {"zswpout": 5})

    def test_share(self) -> None:
        self.assertEqual(share(1, 2), 0.5)
        self.assertEqual(share(-1, 2), -0.5)
        self.assertEqual(share(0, 4), 0.0)
        self.assertIsNone(share(1, 0))
        self.assertIsNone(share(1, None))

    def test_summary_no_bypass(self) -> None:
        gib = 2**30
        s = zswap_summary(
            {"MemTotal": 100 * gib, "Zswap": 2 * gib, "Zswapped": 6 * gib},
            {"zswpout": 1000, "zswpin": 250, "zswpwb": 400, "pswpout": 400, "pswpin": 100},
            {"max_pool_percent": "20", "accept_threshold_percent": "90"},
        )
        self.assertEqual(s["limit"], 20 * gib)
        self.assertAlmostEqual(s["pool_of_limit"] or 0.0, 0.1)
        self.assertEqual(s["accept_below"], 18 * gib)
        self.assertAlmostEqual(s["ratio"] or 0.0, 3.0)
        self.assertAlmostEqual(s["loaded_back"] or 0.0, 0.25)
        self.assertAlmostEqual(s["written_back"] or 0.0, 0.4)
        self.assertEqual(s["bypassed"], 0)
        self.assertAlmostEqual(s["read_back"] or 0.0, 0.25)  # = pswpin/zswpwb here

    def test_summary_bypass_and_empty(self) -> None:
        s = zswap_summary({"MemTotal": 0}, {"pswpout": 50, "zswpwb": 20}, {})
        self.assertEqual(s["bypassed"], 30)
        self.assertIsNone(s["written_back"])  # nothing went into zswap
        self.assertIsNone(s["limit"])
        self.assertIsNone(s["pool"])
        self.assertIsNone(s["ratio"])
        self.assertEqual(pct(None), "-")

    def test_summary_accept_below_needs_both_values(self) -> None:
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
                s = zswap_summary(meminfo, {}, params)
                self.assertEqual(s["limit"], limit)
                self.assertIsNone(s["accept_below"])
        out = io.StringIO()
        render_pool({"max_pool_percent": "20"}, zswap_summary(meminfo, {}, {"max_pool_percent": "20"}), out=out)
        self.assertIn("pool in RAM: 2.0 GiB of a 20.0 GiB limit (10.0 %)", out.getvalue())
        self.assertNotIn("takes new pages again", out.getvalue())

    def test_cgroup_row(self) -> None:
        r = cgroup_row({"zswpout": 10, "zswpwb": 5, "pswpout": 5, "pswpin": 1, "zswapped": 4096}, 123)
        self.assertEqual(r["swap"], 123)
        self.assertEqual(r["zswpwb"], 5 * PAGE)
        self.assertEqual(r["zswpout"], 10 * PAGE)  # pages -> bytes, like zswpwb
        self.assertEqual(r["pswpin"], 1 * PAGE)
        self.assertEqual(r["zswapped"], 4096)  # memory.stat sizes are bytes already
        self.assertAlmostEqual(r["written_back"] or 0.0, 0.5)
        self.assertAlmostEqual(r["read_back"] or 0.0, 0.2)
        self.assertIsNone(cgroup_row({}, None)["read_back"])

    def test_summary_full_result_and_types(self) -> None:
        # Sizes are whole bytes (int, rounded down), shares are float.
        s = zswap_summary(
            {"MemTotal": ODD_RAM, "Zswap": 41902, "Zswapped": 146657},
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

    def test_summary_empty_pool_and_missing_stored(self) -> None:
        # None where the divisor is 0 or a value is missing (ZswapSummary).
        params = {"max_pool_percent": "20"}
        s = zswap_summary({"MemTotal": ODD_RAM, "Zswap": 0, "Zswapped": 0}, {}, params)
        self.assertEqual((s["pool"], s["stored"], s["limit"]), (0, 0, 209510))
        self.assertEqual(s["pool_of_limit"], 0.0)
        self.assertIs(type(s["pool_of_limit"]), float)
        self.assertIsNone(s["ratio"])
        s = zswap_summary({"MemTotal": ODD_RAM, "Zswap": 41902}, {}, params)
        self.assertIsNone(s["stored"])
        self.assertIsNone(s["ratio"])
        self.assertEqual(s["pool_of_limit"], 0.2)

    def test_summary_bypassed_is_never_negative(self) -> None:
        # bypassed = pswpout - zswpwb, at least 0 (ZswapSummary).
        s = zswap_summary({}, {"zswpout": 20, "zswpwb": 12, "pswpout": 10}, {})
        self.assertEqual(s["bypassed"], 0)
        self.assertEqual(s["written_back"], 0.6)

    def test_summary_missing_keys(self) -> None:
        """Pins current behaviour: a counter missing from vmstat reads as 0, and a
        missing MemTotal gives a limit of 0 bytes, not None."""
        self.assertEqual(
            zswap_summary({}, {}, {}),
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
        s = zswap_summary({}, {}, {"max_pool_percent": "100", "accept_threshold_percent": "90"})
        self.assertEqual((s["limit"], s["accept_below"], s["pool_of_limit"]), (0, 0, None))

    def test_cgroup_row_full_result_and_types(self) -> None:
        page = PAGE
        r = cgroup_row(
            {"zswpout": 8, "zswpwb": 3, "pswpout": 4, "pswpin": 1, "zswap": 1000, "zswapped": 3000},
            5000,
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
        self.assertIs(type(r["zswpout"]), int)
        self.assertIs(type(r["zswpwb"]), int)
        self.assertIs(type(r["pswpin"]), int)

    def test_cgroup_row_missing_keys(self) -> None:
        """Pins current behaviour: a counter missing from memory.stat reads as 0,
        a missing size as None."""
        self.assertEqual(
            cgroup_row({}, None),
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
        self.assertEqual(cgroup_row({"pswpout": 4}, None)["read_back"], 0.0)

    def test_desired_files(self) -> None:
        with open(TMPFILES, encoding="utf-8") as f:
            params = parse_tmpfiles_params(f.read())
        names = [p for p, _ in params]
        # compressor before enabled: the first pool is zstd from the start
        self.assertEqual(names[0], "compressor")
        self.assertEqual(names[-1], "enabled")
        with open(SYSCTL, encoding="utf-8") as f:
            self.assertIn(("vm.swappiness", "100"), parse_sysctl(f.read()))
        with open(DROPIN, encoding="utf-8") as f:
            self.assertEqual(parse_memory_low(f.read()), 48 * 2**30)

    def _service(self, proc: str, cg: str, run: str, zdebug: str = "") -> SwapApplicationService:
        env = SwapEnvironment(
            src_dir=SWAP,
            etc_dir=os.path.join(proc, "etc"),
            proc_dir=proc,
            sys_dir=os.path.join(proc, "sysfs"),
            cgroup_dir=cg,
            zswap_params_dir=os.path.join(proc, "zparams"),
            zswap_debug_dir=zdebug or os.path.join(proc, "zdebug"),
            cpu_run_dir=run,
        )
        return SwapApplicationService(
            env,
            LocalFileSystemAdapter(),
            _FakeSystemd(os.path.join(proc, "swaps"), "/dev/dm-0"),
            _FakeSwapSystem(proc, size_bytes=SwapHardwareSpec().expected_size_bytes),
        )

    def settings_output(self, wsf: str, exp_on: bool) -> tuple[list[list[str]], str]:
        """render_settings against a fake /proc/sys and cpu-setup run folder."""
        tmp = tempfile.mkdtemp(prefix="swap-test-")
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "proc", "sys", "vm"))
        with open(SYSCTL, encoding="utf-8") as f:
            for key, want in parse_sysctl(f.read()):
                with open(os.path.join(tmp, "proc", "sys", key.replace(".", "/")), "w", encoding="utf-8") as g:
                    g.write((wsf if key == "vm.watermark_scale_factor" else want) + "\n")
        run = os.path.join(tmp, "run")
        os.makedirs(run)
        if exp_on:
            with open(os.path.join(run, "exp-wsf-300"), "w", encoding="utf-8"):
                pass
        service = self._service(os.path.join(tmp, "proc"), os.path.join(tmp, "cg"), run)
        out = io.StringIO()
        render_settings(service.build_settings_report({}), out=out)
        rows = [
            line.split()[:2]
            for line in out.getvalue().splitlines()
            if "watermark_scale" in line and not line.lstrip().startswith("exp:")
        ]
        return rows, out.getvalue()

    def test_wsf_experiment_is_not_a_diff(self) -> None:
        rows, text = self.settings_output("300", exp_on=True)
        self.assertEqual(rows, [["exp", "vm.watermark_scale_factor"]])
        self.assertIn("experiment off wsf-300", text)
        self.assertNotIn("  DIFF", text)  # apply's verification passes
        rows, text = self.settings_output("300", exp_on=False)
        self.assertEqual(rows, [["DIFF", "vm.watermark_scale_factor"]])
        self.assertNotIn("exp:", text)
        rows, _ = self.settings_output("125", exp_on=True)
        self.assertEqual(rows, [["ok", "vm.watermark_scale_factor"]])

    def test_cpu_run_folder_in_a_toolbox(self) -> None:
        # Inside a container /run is its own; the host's records are under /run/host.
        want = "/run/host/run/cpu-setup" if os.path.exists("/run/.containerenv") else "/run/cpu-setup"
        self.assertEqual(HostExecutionContext().host_run_dir("cpu-setup"), want)

    def test_debug_reject_labels(self) -> None:
        help_ = DEBUG_HELP
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
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                f.write(val + "\n")
        service = self._service(os.path.join(tmp, "proc"), os.path.join(tmp, "cg"), os.path.join(tmp, "run"), zdebug=tmp)
        report = service.status_report()
        assert report.debug is not None
        out = io.StringIO()
        render_debug(report.debug, out=out)
        lines = {line.split()[0]: line for line in out.getvalue().splitlines()[1:] if line.strip()}
        self.assertIn("164000", lines["reject_compress_fail"])
        self.assertTrue(lines["reject_compress_fail"].endswith(help_["reject_compress_fail"]))
        self.assertTrue(lines["reject_reclaim_fail"].endswith(help_["reject_reclaim_fail"]))
        self.assertEqual(lines["new_kernel_counter"].split(), ["new_kernel_counter", "7"])

    def test_size_and_bool(self) -> None:
        self.assertEqual(parse_size("512M"), 512 * 2**20)
        self.assertEqual(parse_size("1024"), 1024)
        self.assertIsNone(parse_size("infinity"))
        self.assertTrue(same_param("Y", "1"))
        self.assertFalse(same_param("N", "1"))
        self.assertTrue(same_param("zstd", "zstd"))

    def test_main_settings_only(self) -> None:
        tmp = tempfile.mkdtemp(prefix="swap-test-")
        self.addCleanup(shutil.rmtree, tmp)
        service = self._service(os.path.join(tmp, "proc"), os.path.join(tmp, "cg"), os.path.join(tmp, "run"))
        out = io.StringIO()
        rc = render_status(service.status_report(settings_only=True, include_config=False), out=out)
        self.assertEqual(rc, 0)
        self.assertIn("live settings vs desired", out.getvalue())
        self.assertNotIn("zswap pool", out.getvalue())


class MarkerTest(unittest.TestCase):
    """Domain MarkedLineSpec and inspect_managed_file on scratch files via SwapHardwareSpec."""

    tmp: str
    fs: LocalFileSystemAdapter
    spec: SwapHardwareSpec

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="swap-test-")
        self.fs = LocalFileSystemAdapter()
        self.spec = SwapHardwareSpec()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def path(self, name: str, text: str | None = None) -> str:
        p = os.path.join(self.tmp, name)
        if text is not None:
            with open(p, "w", encoding="utf-8") as f:
                f.write(text)
        return p

    def line_spec(self, which: str = "fstab") -> MarkedLineSpec:
        return self.spec.fstab_spec() if which == "fstab" else self.spec.crypttab_spec()

    def state(self, p: str, which: str = "fstab") -> str:
        exists = self.fs.exists(p)
        content = self.fs.read_text(p) if exists else None
        return self.line_spec(which).inspect(content, exists=exists, readable=True)

    def write_marked(self, p: str, which: str = "fstab") -> None:
        if not self.fs.exists(p):
            self.fs.write_atomic(p, "", 0o644)
        self.fs.copy_if_absent(p, f"{p}.pre-swap-config.bak")
        existing = self.fs.read_text(p, "") or ""
        self.fs.write_atomic(p, self.line_spec(which).render_updated(existing), 0o644)

    def file_state(self, src: str, dest: str) -> str:
        desired = self.fs.read_text(src) or ""
        is_symlink = self.fs.is_symlink(dest)
        exists = self.fs.exists(dest)
        actual = self.fs.read_text(dest) if (exists and not is_symlink) else None
        return inspect_managed_file(desired, actual, exists=exists, is_symlink=is_symlink, readable=True)

    def test_old_host_lines_are_adopted_in_place(self) -> None:
        for which, old in (("fstab", OLD_FSTAB), ("crypttab", OLD_CRYPTTAB)):
            p = self.path(which, f"# head\nUUID=x / btrfs defaults 0 0\n{old}\n# tail\n")
            self.assertEqual(self.state(p, which), "adopt")
            self.write_marked(p, which)
            with open(p, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual(len(lines), 4)  # no duplicate
            self.assertEqual(lines[2], old.replace(OLD, NEW))  # same place
            self.assertEqual(self.state(p, which), "ok")
            with open(p + ".pre-swap-config.bak", encoding="utf-8") as f:
                self.assertIn(OLD, f.read())

    def test_zram_marker_is_adopted(self) -> None:
        p = self.path("fstab", OLD_FSTAB.replace(OLD, OLDER) + "\n")
        self.assertEqual(self.state(p), "adopt")

    def test_missing_change_duplicate_conflict(self) -> None:
        p = self.path("fstab", "UUID=x / btrfs defaults 0 0\n#/dev/mapper/swap0 old note\n")
        self.assertEqual(self.state(p), "missing")
        self.write_marked(p)
        self.assertEqual(self.state(p), "ok")
        with open(p, encoding="utf-8") as f:
            self.assertTrue(f.read().endswith(NEW + "\n"))
        self.path("fstab", OLD_FSTAB.replace("discard", "pri=5") + "\n")
        self.assertEqual(self.state(p), "change")
        # The same entry twice: only a duplicate, not a change of the entry.
        self.path("fstab", f"{OLD_FSTAB.replace(OLD, NEW)}\n{OLD_FSTAB}\n")
        self.assertEqual(self.state(p), "duplicate")
        self.write_marked(p)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read().count("swap0"), 1)
        self.assertEqual(self.state(p), "ok")
        # Twice, and one of them differs: a real change.
        self.path("fstab", f"{OLD_FSTAB}\n{OLD_FSTAB.replace('discard', 'pri=5')}\n")
        self.assertEqual(self.state(p), "change")
        self.write_marked(p)
        with open(p, encoding="utf-8") as f:
            self.assertEqual(f.read().count("swap0"), 1)
        self.path("fstab", "/dev/mapper/swap0 none swap defaults 0 0\n")
        self.assertEqual(self.state(p), "conflict")
        self.assertEqual(self.state(self.path("absent")), "missing")

    def test_backup_taken_once(self) -> None:
        p = self.path("fstab", "first\n")
        self.write_marked(p)
        self.path("fstab", "second\n")
        self.write_marked(p)
        with open(p + ".pre-swap-config.bak", encoding="utf-8") as f:
            self.assertEqual(f.read(), "first\n")

    def test_file_state(self) -> None:
        src_tmp = os.path.join(SWAP, "tmpfiles", "zswap.conf")
        src_drop = os.path.join(SWAP, "units", "machine.slice.d-50-memory-low.conf")
        self.assertEqual(self.file_state(src_tmp, self.path("t", OLD_TMPFILES)), "adopt")
        self.assertEqual(self.file_state(src_drop, self.path("d", OLD_DROPIN)), "adopt")
        self.assertEqual(self.file_state(src_drop, self.path("d2", OLD_DROPIN.replace("48G", "32G"))), "change")
        self.assertEqual(self.file_state(src_tmp, self.path("none")), "missing")
        shutil.copy(src_tmp, self.path("same"))
        self.assertEqual(self.file_state(src_tmp, self.path("same")), "ok")
        os.symlink(self.path("same"), self.path("link"))
        self.assertEqual(self.file_state(src_tmp, self.path("link")), "change")
        dest = self.path("sub/dir/new")
        self.fs.write_atomic(dest, self.fs.read_text(src_tmp) or "", 0o644)
        self.assertEqual(self.file_state(src_tmp, dest), "ok")
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o644)

    def test_sysctl_payload_matches_old_script(self) -> None:
        src = os.path.join(SWAP, "sysctl", "99-swap-tuning.conf")
        old = self.path(
            "s",
            "# Managed by apply-swap-config.sh — VM tunables\n"
            "vm.swappiness = 100\n\nvm.page-cluster = 0\n"
            "vm.watermark_boost_factor = 0\nvm.watermark_scale_factor = 125\n",
        )
        self.assertEqual(self.file_state(src, old), "adopt")


class _FakeTempFileSystem(LocalFileSystemAdapter):
    """LocalFileSystemAdapter in temp dirs with virtualized /dev block devices and unreadables."""

    def __init__(self, block_links: Mapping[str, str], unreadable_paths: set[str] | None = None) -> None:
        self._block_links = dict(block_links)
        self._unreadable = set() if unreadable_paths is None else set(unreadable_paths)

    def add_block_link(self, link: str, target: str) -> None:
        self._block_links[link] = target

    def is_block_device(self, path: str) -> bool:
        return path in self._block_links or path in set(self._block_links.values())

    def readlink(self, path: str, *, canonicalize: bool = False) -> str | None:
        if path in self._block_links:
            return self._block_links[path] if canonicalize else self._block_links[path]
        return super().readlink(path, canonicalize=canonicalize)

    def is_readable(self, path: str) -> bool:
        if path in self._unreadable:
            return False
        return super().is_readable(path)


class _FakeSystemd:
    """In-memory SystemdPort updating simulated /proc/swaps on start/stop."""

    def __init__(
        self,
        swaps_path: str,
        mapper_real: str,
        *,
        fs: _FakeTempFileSystem | None = None,
        mapper_link: str = "/dev/mapper/swap0",
        fail_unit: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.daemon_reloads = 0
        self._swaps_path = swaps_path
        self._mapper_real = mapper_real
        self._fs = fs
        self._mapper_link = mapper_link
        self._fail_unit = fail_unit

    def daemon_reload(self) -> None:
        self.daemon_reloads += 1

    def _set_active(self, device: str, active: bool) -> None:
        lines = []
        if os.path.exists(self._swaps_path):
            with open(self._swaps_path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        if not lines:
            lines = ["Filename\tType\tSize\tUsed\tPriority"]
        header, rest = lines[0], [line for line in lines[1:] if line.split()[0] != device]
        if active:
            rest.append(f"{device} partition 134217724 0 -2")
        with open(self._swaps_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join([header, *rest]) + "\n")

    def start(self, unit: str) -> bool:
        self.calls.append(("start", unit))
        if unit == self._fail_unit:
            return False
        if unit == "systemd-cryptsetup@swap0.service" and self._fs is not None:
            self._fs.add_block_link(self._mapper_link, self._mapper_real)
        if unit == "dev-mapper-swap0.swap":
            self._set_active(self._mapper_real, True)
        return True

    def stop(self, unit: str) -> bool:
        self.calls.append(("stop", unit))
        if unit == self._fail_unit:
            return False
        if unit == "dev-mapper-swap0.swap":
            self._set_active(self._mapper_real, False)
        elif unit == "dev-zram0.swap":
            self._set_active("/dev/zram0", False)
        return True

    def restart(self, unit: str) -> bool:
        self.calls.append(("restart", unit))
        return unit != self._fail_unit

    def escape_path(self, path: str, *, suffix: str) -> str:
        cleaned = path.lstrip("/").replace("/", "-")
        return f"{cleaned}.{suffix}"


class _FakeSwapSystem:
    """In-memory SwapSystemPort simulating blockdev/blkid/modprobe/sysctl/zramctl/swapon."""

    def __init__(
        self,
        proc_dir: str,
        *,
        size_bytes: int | None,
        part_type: str = "",
        swapon_text: str = "NAME TYPE SIZE USED PRIO\n/dev/dm-0 partition 128G 0B -2\n",
    ) -> None:
        self._proc_dir = proc_dir
        self._size_bytes = size_bytes
        self._part_type = part_type
        self._swapon_text = swapon_text
        self.modprobes: list[str] = []
        self.sysctl_calls: list[tuple[str, str | None]] = []
        self.zram_resets = 0

    def blockdev_size_bytes(self, dev_path: str) -> int | None:
        del dev_path
        return self._size_bytes

    def blkid_type(self, dev_path: str) -> str:
        del dev_path
        return self._part_type

    def modprobe(self, module: str) -> None:
        self.modprobes.append(module)

    def apply_sysctl_file(self, sysctl_conf: str, *, exclude_key: str | None = None) -> None:
        self.sysctl_calls.append((sysctl_conf, exclude_key))
        with open(sysctl_conf, encoding="utf-8") as fh:
            for key, val in parse_sysctl(fh.read()):
                if key == exclude_key:
                    continue
                target = os.path.join(self._proc_dir, "sys", key.replace(".", "/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8") as out:
                    out.write(val + "\n")

    def reset_zram0(self) -> None:
        self.zram_resets += 1

    def swapon_show(self) -> str:
        return self._swapon_text


class _FakeContext:
    def __init__(self, *, container: bool, root: bool, etc_dir: str, run_dir: str) -> None:
        self._container = container
        self._root = root
        self._etc_dir = etc_dir
        self._run_dir = run_dir

    def is_container(self) -> bool:
        return self._container

    def is_root(self) -> bool:
        return self._root

    def etc_dir(self) -> str:
        return self._etc_dir

    def host_run_dir(self, subpath: str) -> str:
        return os.path.join(self._run_dir, subpath) if subpath else self._run_dir


class _FakeRunner:
    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        del argv, check, timeout, input_text, env
        return CommandResult(0, "", "")


class SwapApplicationTest(unittest.TestCase):
    """End-to-end application service and CLI tests inside temporary directories."""

    tmp: str
    spec: SwapHardwareSpec
    env: SwapEnvironment

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="swap-app-test-")
        self.spec = SwapHardwareSpec()
        etc = os.path.join(self.tmp, "etc")
        proc = os.path.join(self.tmp, "proc")
        sys_dir = os.path.join(self.tmp, "sys")
        cg = os.path.join(self.tmp, "cgroup")
        zparams = os.path.join(sys_dir, "module", "zswap", "parameters")
        zdebug = os.path.join(sys_dir, "kernel", "debug", "zswap")
        run_dir = os.path.join(self.tmp, "run", "cpu-setup")
        for d in (
            etc,
            os.path.join(proc, "sys", "vm"),
            os.path.join(proc, "pressure"),
            os.path.join(sys_dir, "class", "block", "nvme0n1p5"),
            os.path.join(sys_dir, "class", "block", "dm-0", "dm"),
            os.path.join(cg, "machine.slice"),
            zparams,
            zdebug,
            run_dir,
        ):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(proc, "crypto"), "w", encoding="utf-8") as f:
            f.write("name         : zstd\ndriver       : zstd-generic\n")
        with open(os.path.join(proc, "meminfo"), "w", encoding="utf-8") as f:
            f.write(
                "MemTotal:       97000000 kB\nMemAvailable:   64000000 kB\nZswap:           10240 kB\nZswapped:        30720 kB\n"
            )
        with open(os.path.join(proc, "vmstat"), "w", encoding="utf-8") as f:
            f.write("zswpout 100\nzswpin 10\nzswpwb 5\npswpout 5\npswpin 1\n")
        with open(os.path.join(proc, "swaps"), "w", encoding="utf-8") as f:
            f.write("Filename\tType\tSize\tUsed\tPriority\n/dev/zram0 partition 8388604 102400 100\n")
        with open(os.path.join(proc, "pressure", "memory"), "w", encoding="utf-8") as f:
            f.write("some avg10=0.00 avg60=0.00 avg300=0.00 total=0\n")
        with open(os.path.join(sys_dir, "class", "block", "nvme0n1p5", "size"), "w", encoding="utf-8") as f:
            f.write(f"{self.spec.expected_size_bytes // 512}\n")
        with open(os.path.join(sys_dir, "class", "block", "dm-0", "dm", "name"), "w", encoding="utf-8") as f:
            f.write("swap0\n")
        with open(os.path.join(cg, "machine.slice", "memory.low"), "w", encoding="utf-8") as f:
            f.write(f"{48 * 2**30}\n")
        for param, init in (
            ("compressor", "lz4"),
            ("max_pool_percent", "10"),
            ("shrinker_enabled", "N"),
            ("enabled", "N"),
        ):
            with open(os.path.join(zparams, param), "w", encoding="utf-8") as f:
                f.write(init + "\n")
        with open(os.path.join(SWAP, "sysctl", "99-swap-tuning.conf"), encoding="utf-8") as fh:
            sysctl_text = fh.read()
        for key, _ in parse_sysctl(sysctl_text):
            with open(os.path.join(proc, "sys", key.replace(".", "/")), "w", encoding="utf-8") as f:
                f.write("0\n")

        self.env = SwapEnvironment(
            src_dir=SWAP,
            etc_dir=etc,
            proc_dir=proc,
            sys_dir=sys_dir,
            cgroup_dir=cg,
            zswap_params_dir=zparams,
            zswap_debug_dir=zdebug,
            cpu_run_dir=run_dir,
            spec=self.spec,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _fs(self, *, mapper_present: bool = False, unreadable: set[str] | None = None) -> _FakeTempFileSystem:
        links = {self.spec.device_path: "/dev/nvme0n1p5"}
        if mapper_present:
            links[self.spec.mapper_path] = "/dev/dm-0"
        return _FakeTempFileSystem(links, unreadable)

    def test_plan_apply_idempotent_and_status_cycle(self) -> None:
        # Legacy zram sysctl exists beforehand so apply should remove it.
        os.makedirs(os.path.join(self.env.etc_dir, "sysctl.d"), exist_ok=True)
        with open(self.env.old_sysctl_conf, "w", encoding="utf-8") as f:
            f.write("vm.swappiness = 60\n")
        fs = self._fs(mapper_present=False)
        systemd = _FakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(self.env, fs, systemd, system)

        # 1. plan changes nothing.
        plan_out = io.StringIO()
        self.assertEqual(render_plan(service.plan_report(), out=plan_out), 0)
        plan_text = plan_out.getvalue()
        self.assertIn("write", plan_text)
        self.assertIn("mask", plan_text)
        self.assertIn("swapoff", plan_text)
        self.assertFalse(os.path.exists(self.env.crypttab))
        self.assertFalse(os.path.exists(self.env.fstab))

        # 2. apply writes everything, evicts zram0, starts swap0, verifies clean.
        apply_out, apply_err = io.StringIO(), io.StringIO()
        self.assertEqual(render_apply(service.apply_changes(), out=apply_out, err=apply_err), 0, apply_err.getvalue())
        self.assertEqual(apply_err.getvalue(), "")
        out_text = apply_out.getvalue()
        self.assertIn("zram0: removed", out_text)
        self.assertIn("Done. Details: cst swap status", out_text)
        self.assertFalse(os.path.exists(self.env.old_sysctl_conf))
        self.assertEqual(os.readlink(self.env.zram_conf), "/dev/null")
        self.assertEqual(system.zram_resets, 1)
        self.assertGreaterEqual(systemd.daemon_reloads, 2)
        with open(self.env.src_tmpfiles, encoding="utf-8") as fh:
            tmpfiles_text = fh.read()
        for name, val in parse_tmpfiles_params(tmpfiles_text):
            self.assertEqual(fs.read_first_line(os.path.join(self.env.zswap_params_dir, name)), val)

        # 3. switch fs to include /dev/mapper/swap0 -> /dev/dm-0 as active, then re-apply: idempotent!
        fs2 = self._fs(mapper_present=True)
        systemd2 = _FakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs2)
        system2 = _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service2 = SwapApplicationService(self.env, fs2, systemd2, system2)

        again_out, again_err = io.StringIO(), io.StringIO()
        self.assertEqual(render_apply(service2.apply_changes(), out=again_out, err=again_err), 0, again_err.getvalue())
        self.assertIn("unchanged:", again_out.getvalue())
        self.assertEqual(systemd2.daemon_reloads, 0)
        self.assertEqual(systemd2.calls, [])

        # 4. status reports ok across managed files.
        status_out = io.StringIO()
        self.assertEqual(render_status(service2.status_report(include_config=True), out=status_out), 0)
        status_text = status_out.getvalue()
        self.assertIn("ok        crypttab: swap0 line", status_text)
        self.assertIn("ok        fstab: /dev/mapper/swap0 line", status_text)
        self.assertIn("zswap pool", status_text)
        self.assertIn("per cgroup", status_text)
        self.assertIn("memory pressure", status_text)

    def test_apply_refuses_conflict_wrong_size_and_unsafe_fs(self) -> None:
        fs = self._fs()
        systemd = _FakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0")

        # Wrong blockdev size refuses before touching files.
        bad_size_sys = _FakeSwapSystem(self.env.proc_dir, size_bytes=12345)
        err = io.StringIO()
        self.assertEqual(
            render_apply(SwapApplicationService(self.env, fs, systemd, bad_size_sys).apply_changes(), out=io.StringIO(), err=err),
            1,
        )
        self.assertIn("expected", err.getvalue())

        # Unexpected filesystem signature refuses.
        bad_fs_sys = _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes, part_type="ext4")
        err = io.StringIO()
        self.assertEqual(
            render_apply(SwapApplicationService(self.env, fs, systemd, bad_fs_sys).apply_changes(), out=io.StringIO(), err=err),
            1,
        )
        self.assertIn("ext4", err.getvalue())

        # Unmarked swap0 conflict in fstab refuses without writing crypttab/fstab.
        with open(self.env.fstab, "w", encoding="utf-8") as f:
            f.write("/dev/mapper/swap0 none swap defaults 0 0\n")
        good_sys = _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        err = io.StringIO()
        self.assertEqual(
            render_apply(SwapApplicationService(self.env, fs, systemd, good_sys).apply_changes(), out=io.StringIO(), err=err),
            1,
        )
        self.assertIn("unmarked entry", err.getvalue())
        self.assertFalse(os.path.exists(self.env.crypttab))

    def test_apply_low_ram_keeps_zram_and_wsf_experiment_excludes_key(self) -> None:
        with open(os.path.join(self.env.proc_dir, "meminfo"), "w", encoding="utf-8") as f:
            f.write("MemTotal: 8000000 kB\nMemAvailable: 500000 kB\n")
        with open(os.path.join(self.env.cpu_run_dir, "exp-wsf-300"), "w", encoding="utf-8"):
            pass
        with open(os.path.join(self.env.proc_dir, "sys", "vm", "watermark_scale_factor"), "w", encoding="utf-8") as f:
            f.write("300\n")

        fs = self._fs()
        systemd = _FakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(self.env, fs, systemd, system)

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(render_apply(service.apply_changes(), out=out, err=err), 0, err.getvalue())
        self.assertIn("not enough free RAM", err.getvalue())
        self.assertEqual(system.zram_resets, 0)
        self.assertIn("kept:      vm.watermark_scale_factor = 300", out.getvalue())
        self.assertEqual(system.sysctl_calls, [(self.env.sysctl_conf, "vm.watermark_scale_factor")])

    def test_missing_desired_file_raises(self) -> None:
        empty_env = SwapEnvironment(src_dir=os.path.join(self.tmp, "missing-src"), etc_dir=self.env.etc_dir)
        service = SwapApplicationService(
            empty_env,
            self._fs(),
            _FakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0"),
            _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes),
        )
        with self.assertRaises(MissingDesiredFileError):
            service.plan_report()

    def test_execute_cli_and_bash_wrapper(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(execute_cli(["help"], env={"CST_PROG": "cst swap"}, out=out, err=err), 0)
        self.assertIn("Usage: cst swap <command> [args]", out.getvalue())
        self.assertEqual(format_usage("cst swap").splitlines()[-1], "Usage: cst swap <command> [args]")

        for bad_args in (["bogus"], ["plan", "--nope"], ["apply", "extra"]):
            with self.subTest(bad_args=bad_args):
                b_out, b_err = io.StringIO(), io.StringIO()
                self.assertEqual(execute_cli(bad_args, env={}, out=b_out, err=b_err), 2)

        # Container refuses apply before elevation or touching the host.
        c_out, c_err = io.StringIO(), io.StringIO()
        c_ctx = _FakeContext(container=True, root=False, etc_dir=self.env.etc_dir, run_dir=self.tmp)
        called: list[list[str]] = []

        def _fake_elevate(argv: list[str]) -> NoReturn:
            called.append(argv)
            raise SystemExit(99)

        rc = execute_cli(
            ["apply"],
            env={"CST_PROG": "cst swap"},
            out=c_out,
            err=c_err,
            runner=_FakeRunner(),
            fs=self._fs(),
            ctx=c_ctx,
            elevate=_fake_elevate,
        )
        self.assertEqual(rc, 1)
        self.assertIn("toolbox/container", c_err.getvalue())
        self.assertEqual(called, [])

        # Host non-root `plan --root` invokes sudo elevation.
        h_ctx = _FakeContext(container=False, root=False, etc_dir=self.env.etc_dir, run_dir=self.tmp)
        with self.assertRaises(SystemExit) as cm:
            execute_cli(
                ["plan", "--root"],
                env={"CST_PROG": "cst swap"},
                out=io.StringIO(),
                err=io.StringIO(),
                runner=_FakeRunner(),
                fs=self._fs(),
                ctx=h_ctx,
                elevate=_fake_elevate,
            )
        self.assertEqual(cm.exception.code, 99)
        self.assertEqual(called[0][:4], ["sudo", "--", "env", "CST_PROG=cst swap"])
        self.assertEqual(called[0][-2:], ["plan", "--root"])

        # Compatibility shim swap-setup.sh delegates cleanly with HostExecutionContext etc.
        proc = subprocess.run(
            ["bash", SETUP, "help"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "CST_PROG": "cst swap"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Usage: cst swap <command> [args]", proc.stdout)
        self.assertTrue(HostExecutionContext().etc_dir() in ("/etc", "/run/host/etc"))


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: MPL-2.0
"""Unit tests for swap domain, presenter, application service, and CLI.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s swap/tests -t . -v
"""

from collections.abc import Callable, Mapping, Sequence
import dataclasses
import hashlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from typing import NoReturn, override
import unittest
from unittest import mock

sys.dont_write_bytecode = True

from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget
from cst_core.application.install import InstallService, Placement
from cst_core.domain.files import MarkedLineSpec
from cst_core.domain.install import ArtifactKind, Owner
from cst_core.ports import CommandFailed, CommandResult, OutputMode, UnitName
from tests.probes import NoUnitQueries
from swap.application.apply import SwapApplicationService
from swap.application.memory_tuning import MM_TMPFILES_ENTRY, URESOURCED_ENTRY, effective_uresourced_memory_min
from swap.application.reports import MissingDesiredFileError, SwapEnvironment
from swap.adapters import LinuxSwapSystemAdapter
from swap.cli import execute_cli, format_usage, main as swap_main
from swap import wiring as swap_wiring
import swap.domain as D
from swap.domain import (
    DEBUG_HELP,
    PAGE,
    SettingCheckRow,
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
from swap.presenter import (
    RunCommand,
    render_apply,
    render_debug,
    render_plan,
    render_pool,
    render_settings,
    render_status,
    run_command,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SWAP = os.path.dirname(HERE)
REPO = os.path.dirname(SWAP)
SETUP = os.path.join(REPO, "swap-setup.sh")
TMPFILES = os.path.join(SWAP, "tmpfiles", "zswap.conf")
SYSCTL = os.path.join(SWAP, "sysctl", "99-swap-tuning.conf")
DROPIN = os.path.join(SWAP, "units", "machine.slice.d-10-swap-memory-low.conf")

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


def swap_installer() -> InstallService:
    """The installer's use case on real files (the tests' folders), with a record port: swap writes its
    files through it, and lists them in env.install_record when a test sets one."""
    return InstallService(LocalInstallTarget(), lambda _e: b"", record=JsonInstallRecord())


# The partition of the tests (the values swap/domain.py had in code until 2026-11), and its settings file.
TEST_SPEC = SwapHardwareSpec("612551b1-ca02-4514-86ad-7d8902e612dc", "swap0", 137438953472)
TEST_SETTINGS = "SWAP_PARTUUID=612551b1-ca02-4514-86ad-7d8902e612dc\nSWAP_SIZE_BYTES=137438953472\n"

# The drop-in as CST installed it until 2026-11, as machine.slice.d/50-memory-low.conf.
# Its first line, split: the license gate wants that line on line 1 of this file only.
MAIN_DROPIN = (
    "# SPDX-"
    "License-Identifier: MPL-2.0\n"
    """\
# Managed by swap-setup.sh (CST): protect libvirt guests from reclaim.
# Soft floor: reclaim prefers unprotected cgroups while they have anything
# reclaimable; ignored when they don't. Propagates to the VM scope via the
# memory_recursiveprot cgroup2 mount option. 48G matches the win11-devbox
# allocation; raise it if the VM grows.
[Slice]
MemoryLow=48G
"""
)


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

    def test_sysfs_size_is_ascii_digits_within_the_digit_limit(self) -> None:
        # The partition's size from sysfs: "2048" sectors is 1 MiB; a digit int() refuses ("²"), one of
        # another script ("٣", which int() reads as 3) or more digits than int() reads is no number.
        def part(size: str | None) -> tuple[str, int] | None:
            return D.sysfs_partition(
                "/dev/disk/by-partuuid/p",
                resolve=lambda _p: "/dev/sda3",
                is_block=lambda _p: True,
                first_line=lambda p: size if p == "/sys/class/block/sda3/size" else None,
                sys_dir="/sys",
            )

        self.assertEqual(part("2048"), ("/dev/sda3", 1048576))
        for bad in ("\N{SUPERSCRIPT TWO}", "\N{ARABIC-INDIC DIGIT THREE}", "9" * 5000, "-1", "", None):
            with self.subTest(size=(bad or "")[:8]):
                self.assertIsNone(part(bad))

    def test_proc_swaps_skips_a_row_whose_sizes_are_no_ascii_number(self) -> None:
        head = "Filename\tType\tSize\tUsed\tPriority\n"
        good = "/dev/dm-0 partition 8388604 1024 -2\n"
        for bad in ("\N{SUPERSCRIPT TWO}", "\N{ARABIC-INDIC DIGIT THREE}", "9" * 5000):
            for row in (f"/dev/zram0 partition {bad} 0 100\n", f"/dev/zram0 partition 10 {bad} 100\n"):
                with self.subTest(row=row[:30]):
                    entries = D.parse_proc_swaps(head + row + good)
                    self.assertEqual(
                        [(e.device, e.size_kib, e.used_kib) for e in entries], [("/dev/dm-0", 8388604, 1024)]
                    )

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
            SwapFakeSystemd(os.path.join(proc, "swaps"), "/dev/dm-0"),
            FakeSwapSystem(proc, size_bytes=TEST_SPEC.expected_size_bytes),
            installer=swap_installer(),
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

    def zswap_rows(self, params: dict[str, str]) -> tuple[list[SettingCheckRow], str]:
        tmp = tempfile.mkdtemp(prefix="swap-test-")
        self.addCleanup(shutil.rmtree, tmp)
        service = self._service(os.path.join(tmp, "proc"), os.path.join(tmp, "cg"), os.path.join(tmp, "run"))
        report = service.build_settings_report(params)
        out = io.StringIO()
        render_settings(report, out=out)
        return [r for r in report.rows if r.label.startswith("zswap")], out.getvalue()

    def test_a_zswap_parameter_the_kernel_lacks_is_a_missing_row(self) -> None:
        # tmpfiles' w skips a parameter file that is not there without a word: status says it.
        rows, text = self.zswap_rows({"compressor": "zstd", "max_pool_percent": "20", "enabled": "Y"})
        self.assertEqual(
            rows,
            [
                SettingCheckRow(D.SettingState.OK, "zswap compressor", "zstd", "zstd"),
                SettingCheckRow(D.SettingState.OK, "zswap max_pool_percent", "20", "20"),
                SettingCheckRow(
                    D.SettingState.MISSING, "zswap shrinker_enabled", "this kernel has no such parameter", "1"
                ),
                SettingCheckRow(D.SettingState.OK, "zswap enabled", "Y", "1"),
            ],
        )
        self.assertIn("MISSING zswap shrinker_enabled       this kernel has no such parameter   (desired 1)", text)

    def test_no_zswap_at_all_is_one_bad_row(self) -> None:
        rows, text = self.zswap_rows({})
        self.assertEqual(
            rows, [SettingCheckRow(D.SettingState.BAD, "zswap", "not in this kernel (no /sys/module/zswap)", "")]
        )
        # The state word as status prints it (a member's value that changes fails here).
        self.assertEqual(
            [ln for ln in text.splitlines() if " zswap " in f"{ln} "],
            ["  BAD     zswap                        not in this kernel (no /sys/module/zswap)"],
        )

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
        # The marker files are faked so the result does not depend on where the test runs
        # (CI runs inside Docker, developers often inside a podman toolbox).
        for marker in ("/run/.containerenv", "/.dockerenv"):
            ctx = HostExecutionContext(exists=lambda p, m=marker: p == m)
            self.assertEqual(ctx.host_run_dir("cpu-setup"), "/run/host/run/cpu-setup", marker)
        self.assertEqual(HostExecutionContext(exists=lambda _p: False).host_run_dir("cpu-setup"), "/run/cpu-setup")

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
        service = self._service(
            os.path.join(tmp, "proc"), os.path.join(tmp, "cg"), os.path.join(tmp, "run"), zdebug=tmp
        )
        report = service.status_report()
        assert report.debug is not None
        out = io.StringIO()
        render_debug(report.debug, out=out)
        lines = {line.split()[0]: line for line in out.getvalue().splitlines()[1:] if line.strip()}
        self.assertIn("164000", lines["reject_compress_fail"])
        self.assertTrue(lines["reject_compress_fail"].endswith(help_["reject_compress_fail"]))
        self.assertTrue(lines["reject_reclaim_fail"].endswith(help_["reject_reclaim_fail"]))
        self.assertEqual(lines["new_kernel_counter"].split(), ["new_kernel_counter", "7"])

    def test_is_cst_dropin(self) -> None:
        self.assertTrue(D.is_cst_dropin(MAIN_DROPIN))
        self.assertTrue(D.is_cst_dropin(OLD_DROPIN))
        with open(DROPIN, encoding="utf-8") as f:
            self.assertTrue(D.is_cst_dropin(f.read()))
        self.assertFalse(D.is_cst_dropin("[Slice]\nMemoryLow=8G\n"))
        self.assertFalse(D.is_cst_dropin("[Slice]\nMemoryLow=8G # Managed by swap-setup.sh (CST)\n"))

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
    """The service's marked-line and managed-file steps (the code apply runs) on scratch files."""

    tmp: str
    fs: SwapTempFileSystem
    spec: SwapHardwareSpec
    svc: SwapApplicationService

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="swap-test-")
        self.fs = SwapTempFileSystem({})
        self.spec = TEST_SPEC
        swaps = os.path.join(self.tmp, "swaps")
        self.svc = SwapApplicationService(
            SwapEnvironment(src_dir=SWAP, etc_dir=self.tmp),
            self.fs,
            SwapFakeSystemd(swaps, "/dev/dm-0"),
            FakeSwapSystem(self.tmp, size_bytes=self.spec.expected_size_bytes),
            installer=swap_installer(),
        )

    @override
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
        return self.svc._inspect_marked(p, self.line_spec(which))

    def write_marked(self, p: str, which: str = "fstab") -> None:
        events: list[D.ApplyEvent] = []
        ok, _changed, _written = self.svc._apply_marked_line(p, self.line_spec(which), 0o644, events, name="swap0")
        self.assertTrue(ok, events)

    def file_state(self, src: str, dest: str) -> str:
        return self.svc._inspect_file(src, dest)

    def test_unreadable_files(self) -> None:
        p = self.path("fstab", "x\n")
        self.fs._unreadable.add(p)
        self.assertEqual(self.state(p), "unreadable")
        self.assertEqual(self.file_state(TMPFILES, p), "unreadable")
        events: list[D.ApplyEvent] = []
        self.assertEqual(
            self.svc._apply_marked_line(p, self.line_spec(), 0o644, events, name="swap0"), (False, False, False)
        )
        self.assertIn("unreadable", events[-1].text)

    def test_other_lines_come_back_byte_for_byte(self) -> None:
        # main read and rewrote fstab/crypttab with bash `read -r`: bytes that are not UTF-8, a CR, a form
        # feed and a last line without a newline all came back as they were (the last one with a newline).
        p = os.path.join(self.tmp, "fstab")
        foreign = b"# caf\xe9 comment\r\n\x0cform feed\nUUID=x / btrfs defaults 0 0"
        with open(p, "wb") as f:
            f.write(foreign)
        self.assertEqual(self.state(p), "missing")
        self.write_marked(p)
        with open(p, "rb") as f:
            self.assertEqual(f.read(), foreign + b"\n" + self.line_spec().desired_line.encode() + b"\n")
        self.assertEqual(self.state(p), "ok")
        with open(p + ".pre-swap-config.bak", "rb") as f:
            self.assertEqual(f.read(), foreign)

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
        src_drop = DROPIN
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
        old_four_key = self.path(
            "s4",
            "# Managed by apply-swap-config.sh — VM tunables\n"
            "vm.swappiness = 100\n\nvm.page-cluster = 0\n"
            "vm.watermark_boost_factor = 0\nvm.watermark_scale_factor = 125\n",
        )
        self.assertEqual(self.file_state(src, old_four_key), "change")
        old_five_key = self.path(
            "s5",
            "# Managed by apply-swap-config.sh — VM tunables\n"
            "vm.swappiness = 100\n\nvm.page-cluster = 0\n"
            "vm.watermark_boost_factor = 0\nvm.watermark_scale_factor = 125\n"
            "vm.min_free_kbytes = 262144\n",
        )
        self.assertEqual(self.file_state(src, old_five_key), "adopt")


class SwapTempFileSystem(LocalFileSystemAdapter):
    """LocalFileSystemAdapter in temp dirs with virtualized /dev block devices and unreadables."""

    def __init__(self, block_links: Mapping[str, str], unreadable_paths: set[str] | None = None) -> None:
        self._block_links = dict(block_links)
        self._unreadable = set() if unreadable_paths is None else set(unreadable_paths)

    @property
    def block_links(self) -> dict[str, str]:
        return dict(self._block_links)

    def add_block_link(self, link: str, target: str) -> None:
        self._block_links[link] = target

    @override
    def is_block_device(self, path: str) -> bool:
        return path in self._block_links or path in set(self._block_links.values())

    @override
    def readlink(self, path: str, *, canonicalize: bool = False) -> str | None:
        if path in self._block_links:
            return self._block_links[path]
        return super().readlink(path, canonicalize=canonicalize)

    @override
    def is_readable(self, path: str) -> bool:
        if path in self._unreadable:
            return False
        return super().is_readable(path)


class _ClosedFolder(SwapTempFileSystem):
    """A folder this user may not search (mode 0700 of another owner): nothing in it can be seen."""

    def __init__(self, block_links: Mapping[str, str], *, closed: str) -> None:
        super().__init__(block_links)
        self._closed = closed

    def _inside(self, path: str) -> bool:
        return path.startswith(self._closed + os.sep)

    @override
    def lexists(self, path: str) -> bool:
        return not self._inside(path) and super().lexists(path)

    @override
    def exists(self, path: str) -> bool:
        return not self._inside(path) and super().exists(path)

    @override
    def is_file(self, path: str) -> bool:
        return not self._inside(path) and super().is_file(path)

    @override
    def is_dir(self, path: str) -> bool:
        return not self._inside(path) and super().is_dir(path)

    @override
    def is_readable(self, path: str) -> bool:
        return path != self._closed and not self._inside(path) and super().is_readable(path)

    @override
    def is_searchable(self, path: str) -> bool:
        return path != self._closed and not self._inside(path) and super().is_searchable(path)

    @override
    def read_text(self, path: str, default: str | None = None) -> str | None:
        return default if self._inside(path) else super().read_text(path, default)


class _BufferedToPipe(io.StringIO):
    """A stdout on a pipe: what is written reaches the pipe only when flushed."""

    def __init__(self, pipe: list[str]) -> None:
        super().__init__()
        self._pipe = pipe
        self._sent = 0

    @override
    def flush(self) -> None:
        text = self.getvalue()
        if text[self._sent :]:
            self._pipe.append(text[self._sent :])
        self._sent = len(text)


class _UnbufferedToPipe(io.StringIO):
    """A stderr on the same pipe: each write reaches it at once."""

    def __init__(self, pipe: list[str]) -> None:
        super().__init__()
        self._pipe = pipe

    @override
    def write(self, s: str, /) -> int:
        if s:
            self._pipe.append(s)
        return super().write(s)


class _ReadsRecorded(SwapTempFileSystem):
    """Records each path read_text is asked for; a path in unopened gives the default without being opened
    (a FIFO's open would block the test)."""

    def __init__(self, block_links: Mapping[str, str], *, unopened: set[str]) -> None:
        super().__init__(block_links)
        self._unopened = set(unopened)
        self.reads: list[str] = []

    @override
    def read_text(self, path: str, default: str | None = None) -> str | None:
        self.reads.append(path)
        return default if path in self._unopened else super().read_text(path, default)


class _TextUnreadable(SwapTempFileSystem):
    """read_text gives its default for the paths in unread, as for a file the process may not open."""

    def __init__(
        self, block_links: Mapping[str, str], unreadable_paths: set[str] | None = None, *, unread: set[str]
    ) -> None:
        super().__init__(block_links, unreadable_paths)
        self._unread = set(unread)

    @override
    def read_text(self, path: str, default: str | None = None) -> str | None:
        return default if path in self._unread else super().read_text(path, default)


class SwapFakeSystemd(NoUnitQueries):
    """In-memory SystemdPort updating simulated /proc/swaps on start/stop."""

    def __init__(
        self,
        swaps_path: str,
        mapper_real: str,
        *,
        fs: SwapTempFileSystem | None = None,
        mapper_link: str = "/dev/mapper/swap0",
        fail_unit: str | None = None,
        fail_result: CommandResult | None = None,
        on_call: Callable[[str, str], object] | None = None,
        shown: Mapping[str, str] | None = None,
        unit_shown: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.shown = {} if shown is None else dict(shown)  # what `systemctl show machine.slice` says ({}: no answer)
        self._unit_shown = {} if unit_shown is None else {k: dict(v) for k, v in unit_shown.items()}
        self._fail_result = CommandResult(1, "", "") if fail_result is None else fail_result
        self._on_call = on_call
        self.daemon_reloads = 0
        self._swaps_path = swaps_path
        self._mapper_real = mapper_real
        self._fs = fs
        self._mapper_link = mapper_link
        self._fail_unit = fail_unit

    def daemon_reload(self) -> None:
        self.daemon_reloads += 1

    def enable(self, unit: str, *, now: bool = False, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del now, output
        raise AssertionError(f"swap never enables a unit: {unit}")

    def disable(self, unit: str, *, now: bool = False, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del now, output
        raise AssertionError(f"swap never disables a unit: {unit}")

    def clean(self, unit: str, *, what: str, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del what, output
        raise AssertionError(f"swap never cleans a unit: {unit}")

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

    def _call(self, verb: str, unit: str) -> bool:
        self.calls.append((verb, unit))
        if self._on_call is not None:
            self._on_call(verb, unit)
        return unit != self._fail_unit

    def start(self, unit: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del output
        if not self._call("start", unit):
            return self._fail_result
        if unit == "systemd-cryptsetup@swap0.service" and self._fs is not None:
            self._fs.add_block_link(self._mapper_link, self._mapper_real)
        if unit == "dev-mapper-swap0.swap":
            self._set_active(self._mapper_real, True)
        return CommandResult(0, "", "")

    def stop(self, unit: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del output
        if not self._call("stop", unit):
            return self._fail_result
        if unit == "dev-mapper-swap0.swap":
            self._set_active(self._mapper_real, False)
        elif unit == "dev-zram0.swap":
            self._set_active("/dev/zram0", False)
        return CommandResult(0, "", "")

    def restart(self, unit: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del output
        return CommandResult(0, "", "") if self._call("restart", unit) else self._fail_result

    def show(self, unit: UnitName, props: Sequence[str], *, on_host: bool = True, user: bool = False) -> dict[str, str]:
        del on_host, user
        if unit in self._unit_shown:
            return {k: v for k, v in self._unit_shown[unit].items() if k in props}
        return {k: v for k, v in self.shown.items() if k in props} if unit == "machine.slice" else {}

    def escape_path(self, path: str, *, suffix: str) -> UnitName:
        cleaned = path.lstrip("/").replace("/", "-")
        return UnitName(f"{cleaned}.{suffix}")


class FakeSwapSystem:
    """In-memory SwapSystemPort simulating blockdev/blkid/modprobe/sysctl/zramctl/swapon."""

    def __init__(
        self,
        proc_dir: str,
        *,
        size_bytes: int | None,
        part_type: str = "",
        swapon_text: str = "NAME TYPE SIZE USED PRIO\n/dev/dm-0 partition 128G 0B -2\n",
        sysctl_result: CommandResult | None = None,
        swapon_result: CommandResult | None = None,
        on_sleep: Callable[[float], object] | None = None,
    ) -> None:
        self._sysctl_result = CommandResult(0, "", "") if sysctl_result is None else sysctl_result
        self._swapon_result = CommandResult(0, swapon_text, "") if swapon_result is None else swapon_result
        self._proc_dir = proc_dir
        self._size_bytes = size_bytes
        self._part_type = part_type
        self._swapon_text = swapon_text
        self._on_sleep = on_sleep
        self.modprobes: list[str] = []
        self.sysctl_calls: list[tuple[str, str | None]] = []
        self.zram_resets = 0
        self.sleeps: list[float] = []

    def blockdev_size(self, dev_path: str) -> str:
        del dev_path
        return str(self._size_bytes)

    def blkid_type(self, dev_path: str) -> str:
        del dev_path
        return self._part_type

    def modprobe(self, module: str) -> None:
        self.modprobes.append(module)

    def apply_sysctl_file(self, sysctl_conf: str, *, exclude_key: str | None = None) -> CommandResult:
        self.sysctl_calls.append((sysctl_conf, exclude_key))
        if self._sysctl_result.returncode != 0:
            return self._sysctl_result
        with open(sysctl_conf, encoding="utf-8") as fh:
            for key, val in parse_sysctl(fh.read()):
                if key == exclude_key:
                    continue
                target = os.path.join(self._proc_dir, "sys", key.replace(".", "/"))
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "w", encoding="utf-8") as out:
                    out.write(val + "\n")
        return self._sysctl_result

    def reset_zram0(self) -> None:
        self.zram_resets += 1

    def swapon_show(self) -> CommandResult:
        return self._swapon_result

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self._on_sleep is not None:
            self._on_sleep(seconds)


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

    def path(self, host_path: str) -> str:
        return f"/run/host/{host_path.lstrip('/')}" if self._container and host_path.startswith("/") else host_path

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
        **_options: object,
    ) -> CommandResult:
        del argv, check, timeout, input_text, env
        return CommandResult(0, "", "")


def write_zswap_params(zparams: str) -> None:
    """zswap's parameter files in the folder zparams (made when missing), as a kernel boots with them:
    the values before an apply."""
    os.makedirs(zparams, exist_ok=True)
    for param, init in (
        ("compressor", "lz4"),
        ("max_pool_percent", "10"),
        ("shrinker_enabled", "N"),
        ("enabled", "N"),
    ):
        with open(os.path.join(zparams, param), "w", encoding="utf-8") as f:
            f.write(init + "\n")


def swap_scratch(tmp: str, spec: SwapHardwareSpec) -> SwapEnvironment:
    """A scratch /etc (with TEST_SETTINGS in /etc/cst/swap.conf), /proc, /sys and cgroup tree in the folder
    tmp (which the caller removes), for spec's partition; the application service's environment over it."""
    etc = os.path.join(tmp, "etc")
    proc = os.path.join(tmp, "proc")
    sys_dir = os.path.join(tmp, "sys")
    cg = os.path.join(tmp, "cgroup")
    zparams = os.path.join(sys_dir, "module", "zswap", "parameters")
    zdebug = os.path.join(sys_dir, "kernel", "debug", "zswap")
    run_dir = os.path.join(tmp, "run", "cpu-setup")
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
        f.write(f"{spec.expected_size_bytes // 512}\n")
    with open(os.path.join(sys_dir, "class", "block", "dm-0", "dm", "name"), "w", encoding="utf-8") as f:
        f.write("swap0\n")
    with open(os.path.join(cg, "machine.slice", "memory.low"), "w", encoding="utf-8") as f:
        f.write(f"{48 * 2**30}\n")
    write_zswap_params(zparams)
    with open(os.path.join(SWAP, "sysctl", "99-swap-tuning.conf"), encoding="utf-8") as fh:
        sysctl_text = fh.read()
    for key, _ in parse_sysctl(sysctl_text):
        with open(os.path.join(proc, "sys", key.replace(".", "/")), "w", encoding="utf-8") as f:
            f.write("0\n")

    env = SwapEnvironment(
        src_dir=SWAP,
        etc_dir=etc,
        proc_dir=proc,
        sys_dir=sys_dir,
        cgroup_dir=cg,
        zswap_params_dir=zparams,
        zswap_debug_dir=zdebug,
        cpu_run_dir=run_dir,
        seats_dir=os.path.join(tmp, "run", "systemd", "seats"),
    )
    # A desktop: seat0 has a session in the foreground (uresourced gives it its floor).
    os.makedirs(env.seats_dir)
    with open(os.path.join(env.seats_dir, "seat0"), "w", encoding="utf-8") as f:
        f.write("# This is private data. Do not parse.\nIS_SEAT0=1\nACTIVE=3\nACTIVE_UID=1000\nSESSIONS=3 c1\n")
    os.makedirs(os.path.join(etc, "cst"))
    with open(env.settings_path, "w", encoding="utf-8") as f:
        f.write(TEST_SETTINGS)
    return env


class _SwapScratch(unittest.TestCase):
    """A scratch /etc, /proc, /sys and cgroup tree for the application service."""

    tmp: str
    spec: SwapHardwareSpec
    env: SwapEnvironment

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="swap-app-test-")
        self.spec = TEST_SPEC
        self.env = swap_scratch(self.tmp, self.spec)

    @override
    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def _fs(self, *, mapper_present: bool = False, unreadable: set[str] | None = None) -> SwapTempFileSystem:
        links = {self.spec.device_path: "/dev/nvme0n1p5"}
        if mapper_present:
            links[self.spec.mapper_path] = "/dev/dm-0"
        return SwapTempFileSystem(links, unreadable)

    def _apply_once(self, env: SwapEnvironment, *, mapper_present: bool) -> tuple[int, str, str, SwapFakeSystemd]:
        fs = self._fs(mapper_present=mapper_present)
        systemd = SwapFakeSystemd(os.path.join(env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = FakeSwapSystem(env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(env, fs, systemd, system, installer=swap_installer())
        out, err = io.StringIO(), io.StringIO()
        rc = render_apply(service.apply_changes(), out=out, err=err)
        return rc, out.getvalue(), err.getvalue(), systemd

    def _plan_text(
        self, env: SwapEnvironment, *, unreadable: set[str] | None = None, fs: SwapTempFileSystem | None = None
    ) -> str:
        fs = self._fs(unreadable=unreadable) if fs is None else fs
        service = SwapApplicationService(
            env,
            fs,
            SwapFakeSystemd(os.path.join(env.proc_dir, "swaps"), "/dev/dm-0", fs=fs),
            FakeSwapSystem(env.proc_dir, size_bytes=self.spec.expected_size_bytes),
            installer=swap_installer(),
        )
        out = io.StringIO()
        render_plan(service.plan_report(), out=out)
        return out.getvalue()


class SwapApplicationTest(_SwapScratch):
    """End-to-end application service and CLI tests inside temporary directories."""

    def test_apply_lists_its_files_in_the_install_record(self) -> None:
        record = os.path.join(self.tmp, "var/lib/cst/installed.json")
        env = dataclasses.replace(self.env, install_record=record)
        first = b""
        for n in (1, 2):
            fs = self._fs(mapper_present=n == 2)
            systemd = SwapFakeSystemd(os.path.join(env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
            system = FakeSwapSystem(env.proc_dir, size_bytes=self.spec.expected_size_bytes)
            service = SwapApplicationService(env, fs, systemd, system, installer=swap_installer())
            err = io.StringIO()
            self.assertEqual(render_apply(service.apply_changes(), out=io.StringIO(), err=err), 0, err.getvalue())
            with open(record, "rb") as f:
                data = f.read()
            if n == 1:
                first = data
            else:
                self.assertEqual(data, first, "a second apply that changes nothing rewrote the record")
        rec = json.loads(first)
        # Listed under their real destinations (not the test's folder), as `cst doctor` reads them.
        self.assertEqual(
            sorted(rec["files"]),
            [
                "/etc/sysctl.d/99-swap-tuning.conf",
                "/etc/systemd/system/machine.slice.d/10-swap-memory-low.conf",
                "/etc/tmpfiles.d/cst-mm-responsiveness.conf",
                "/etc/tmpfiles.d/zswap.conf",
                "/etc/uresourced.conf",
            ],
        )
        self.assertEqual(rec["components"], {"swap:core": "done"})
        with open(env.sysctl_conf, "rb") as f:
            self.assertEqual(
                rec["files"]["/etc/sysctl.d/99-swap-tuning.conf"]["sha256"], hashlib.sha256(f.read()).hexdigest()
            )

    def test_apply_with_a_record_of_a_newer_cst_is_an_error_line_not_a_traceback(self) -> None:
        record = os.path.join(self.tmp, "var/lib/cst/installed.json")
        os.makedirs(os.path.dirname(record))
        with open(record, "w") as f:
            f.write('{"format": 99, "files": {}}')
        env = dataclasses.replace(self.env, install_record=record)
        fs = self._fs(mapper_present=False)
        systemd = SwapFakeSystemd(os.path.join(env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = FakeSwapSystem(env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(env, fs, systemd, system, installer=swap_installer())
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(run_command(D.SwapCommand.APPLY, service, hint="", out=out, err=err), 1)
        self.assertIn("cst swap: ERROR: ", err.getvalue())
        self.assertIn("written by a newer CST", err.getvalue())
        with open(record) as f:
            self.assertEqual(f.read(), '{"format": 99, "files": {}}', "the newer record was changed")

    def _old_dropin_install(self, record: str, text: str) -> str:
        """machine.slice.d/50-memory-low.conf as an older CST's apply left it: the file, listed in the
        install record by the same installer call (put) that apply made then."""
        old = os.path.join(self.env.etc_dir, "systemd/system/machine.slice.d/50-memory-low.conf")
        place = Placement(
            path=old,
            dest="/etc/systemd/system/machine.slice.d/50-memory-low.conf",
            record=record,
            component="swap:core",
            src="swap/units/machine.slice.d-50-memory-low.conf",
            kind=ArtifactKind.UNIT,
            owner=Owner.ROOT,
            mode=0o644,
        )
        swap_installer().put(place, text.encode(), as_root=False)
        return old

    def test_apply_moves_the_drop_in_to_its_10_name_and_forgets_the_old_one(self) -> None:
        # Upgrade: the drop-in under the name an older CST used (it sorted after set-property's
        # 50-MemoryLow.conf and won over a runtime MemoryLow at every daemon-reload).
        record = os.path.join(self.tmp, "var/lib/cst/installed.json")
        env = dataclasses.replace(self.env, install_record=record)
        old = self._old_dropin_install(record, MAIN_DROPIN)
        new = env.slice_dropin
        self.assertEqual(os.path.basename(new), "10-swap-memory-low.conf")

        plan = self._plan_text(env)
        self.assertIn("  remove    machine.slice.d/50-memory-low.conf", plan)
        self.assertIn("  write     machine.slice.d/10-swap-memory-low.conf", plan)
        self.assertTrue(os.path.exists(old), "plan removed the old drop-in")

        rc, out, err, systemd = self._apply_once(env, mapper_present=False)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"removed:   {old} (the old name of 10-swap-memory-low.conf)", out)
        self.assertFalse(os.path.lexists(old))
        with open(new, encoding="utf-8") as f, open(DROPIN, encoding="utf-8") as want:
            self.assertEqual(f.read(), want.read())
        with open(record, encoding="utf-8") as f:
            files = json.load(f)["files"]
        self.assertNotIn("/etc/systemd/system/machine.slice.d/50-memory-low.conf", files)
        self.assertIn("/etc/systemd/system/machine.slice.d/10-swap-memory-low.conf", files)
        self.assertGreaterEqual(systemd.daemon_reloads, 1)

        self.assertNotIn("50-memory-low.conf", self._plan_text(env))
        rc, out, err, systemd = self._apply_once(env, mapper_present=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("removed:", out)
        self.assertEqual(systemd.daemon_reloads, 0)

    def test_an_old_drop_in_from_the_script_before_cst_is_removed_too(self) -> None:
        old = self._old_dropin_install("", OLD_DROPIN)
        rc, out, err, _ = self._apply_once(self.env, mapper_present=False)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"removed:   {old}", out)
        self.assertFalse(os.path.lexists(old))

    def test_a_50_memory_low_conf_cst_did_not_write_is_left_alone(self) -> None:
        foreign = "[Slice]\nMemoryLow=8G\n"
        old = self._old_dropin_install("", foreign)
        self.assertNotIn("50-memory-low.conf", self._plan_text(self.env))
        rc, out, err, _ = self._apply_once(self.env, mapper_present=False)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("removed:", out)
        with open(old, encoding="utf-8") as f:
            self.assertEqual(f.read(), foreign)

    def test_the_old_record_line_goes_also_when_the_file_is_gone_or_not_cst_s(self) -> None:
        old_dest = "/etc/systemd/system/machine.slice.d/50-memory-low.conf"
        foreign = "[Slice]\nMemoryLow=8G\n"
        for case in ("gone", "foreign"):
            with self.subTest(case=case):
                record = os.path.join(self.tmp, f"var/lib/cst/{case}.json")
                env = dataclasses.replace(self.env, install_record=record)
                old = self._old_dropin_install(record, MAIN_DROPIN)
                if case == "gone":
                    os.remove(old)
                else:
                    with open(old, "w", encoding="utf-8") as f:
                        f.write(foreign)
                rc, out, err, _ = self._apply_once(env, mapper_present=False)
                self.assertEqual(rc, 0, err)
                self.assertNotIn("removed:", out)
                with open(record, encoding="utf-8") as f:
                    files = json.load(f)["files"]
                self.assertNotIn(old_dest, files)
                self.assertIn("/etc/systemd/system/machine.slice.d/10-swap-memory-low.conf", files)
                if case == "foreign":
                    with open(old, encoding="utf-8") as f:
                        self.assertEqual(f.read(), foreign)
                with open(record, "rb") as f:
                    first = f.read()
                rc, _out, err, _ = self._apply_once(env, mapper_present=True)
                self.assertEqual(rc, 0, err)
                with open(record, "rb") as f:
                    self.assertEqual(f.read(), first, "a second apply changed the record")

    def test_plan_apply_idempotent_and_status_cycle(self) -> None:
        # Legacy zram sysctl exists beforehand so apply should remove it.
        os.makedirs(os.path.join(self.env.etc_dir, "sysctl.d"), exist_ok=True)
        with open(self.env.old_sysctl_conf, "w", encoding="utf-8") as f:
            f.write("vm.swappiness = 60\n")
        fs = self._fs(mapper_present=False)
        systemd = SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(self.env, fs, systemd, system, installer=swap_installer())

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
        systemd2 = SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs2)
        system2 = FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service2 = SwapApplicationService(self.env, fs2, systemd2, system2, installer=swap_installer())

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
        systemd = SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0")

        # Wrong blockdev size refuses before touching files.
        bad_size_sys = FakeSwapSystem(self.env.proc_dir, size_bytes=12345)
        err = io.StringIO()
        self.assertEqual(
            render_apply(
                SwapApplicationService(self.env, fs, systemd, bad_size_sys, installer=swap_installer()).apply_changes(),
                out=io.StringIO(),
                err=err,
            ),
            1,
        )
        self.assertIn("expected", err.getvalue())

        # Unexpected filesystem signature refuses.
        bad_fs_sys = FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes, part_type="ext4")
        err = io.StringIO()
        self.assertEqual(
            render_apply(
                SwapApplicationService(self.env, fs, systemd, bad_fs_sys, installer=swap_installer()).apply_changes(),
                out=io.StringIO(),
                err=err,
            ),
            1,
        )
        self.assertIn("ext4", err.getvalue())

        # Unmarked swap0 conflict in fstab refuses without writing crypttab/fstab.
        with open(self.env.fstab, "w", encoding="utf-8") as f:
            f.write("/dev/mapper/swap0 none swap defaults 0 0\n")
        good_sys = FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        err = io.StringIO()
        self.assertEqual(
            render_apply(
                SwapApplicationService(self.env, fs, systemd, good_sys, installer=swap_installer()).apply_changes(),
                out=io.StringIO(),
                err=err,
            ),
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
        systemd = SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(self.env, fs, systemd, system, installer=swap_installer())

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
            SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0"),
            FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes),
            installer=swap_installer(),
        )
        with self.assertRaises(MissingDesiredFileError):
            service.plan_report()

    def test_execute_cli_and_bash_wrapper(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(execute_cli(["help"], env={"CST_PROG": "cst swap"}, out=out, err=err), 0)
        self.assertIn("Usage: cst swap <command> [args]", out.getvalue())
        for word in ("-h", "--help"):
            with self.subTest(word=word):
                h_out, h_err = io.StringIO(), io.StringIO()
                self.assertEqual(execute_cli([word], env={"CST_PROG": "cst swap"}, out=h_out, err=h_err), 0)
                self.assertEqual((h_out.getvalue(), h_err.getvalue()), (out.getvalue(), err.getvalue()))
        self.assertEqual(format_usage("cst swap").splitlines()[-1], "Usage: cst swap <command> [args]")
        # The layout of the bash usage() it replaces: a blank line first, one before "Usage:".
        text = format_usage("cst swap")
        self.assertTrue(text.startswith("\nswap-setup.sh  (the `swap` module"), text[:40])
        self.assertIn("memory.zswap.writeback.\n\nUsage: cst swap <command> [args]\n", text)

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
        # A closed pipe stops it quietly, as the bash swap-setup.sh stopped (SIGPIPE at its default).
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            piped = subprocess.run(
                ["bash", SETUP, "help"],
                stdout=write_end,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin", "CST_PROG": "cst swap"},
                timeout=60,
            )
        finally:
            os.close(write_end)
        self.assertEqual(piped.stderr, "")
        self.assertEqual(piped.returncode, -signal.SIGPIPE)
        # No container marker (faked: CI itself runs in Docker): the host /etc.
        self.assertEqual(HostExecutionContext(exists=lambda _p: False, euid=lambda: 1000).etc_dir(), "/etc")


def file_tree(root: str) -> dict[str, bytes]:
    """Every file under root (links: their target), to prove a run changed nothing."""
    out: dict[str, bytes] = {}
    for d, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(d, name)
            if os.path.islink(p):
                out[p] = os.readlink(p).encode()
            else:
                with open(p, "rb") as f:
                    out[p] = f.read()
    return out


class SwapSettingsFileTest(unittest.TestCase):
    """parse_swap_settings, render_swap_settings and the marked lines an older CST wrote."""

    def test_a_partition_is_read_with_the_default_name(self) -> None:
        s = D.parse_swap_settings(
            "# mine\nSWAP_PARTUUID=612551b1-ca02-4514-86ad-7d8902e612dc\nSWAP_SIZE_BYTES = 4096\n"
        )
        self.assertEqual(s, D.SwapSettings(SwapHardwareSpec("612551b1-ca02-4514-86ad-7d8902e612dc", "swap0", 4096)))
        s = D.parse_swap_settings("SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=8192\nSWAP_NAME=cryptswap\n")
        self.assertEqual(s.spec, SwapHardwareSpec("0a1b2c3d-02", "cryptswap", 8192))

    def test_no_partuuid_is_not_configured(self) -> None:
        self.assertEqual(D.parse_swap_settings(""), D.SwapSettings(None))
        self.assertEqual(D.parse_swap_settings("# nothing yet\nSWAP_NAME=swap1\n"), D.SwapSettings(None))

    def test_bad_values_are_problems_and_no_partition(self) -> None:
        cases = {
            "SWAP_PARTUUID=612551B1-CA02-4514-86AD-7D8902E612DC\nSWAP_SIZE_BYTES=1\n": "SWAP_PARTUUID=612551B1",
            "SWAP_PARTUUID=0a1b2c3d-02\n": "SWAP_SIZE_BYTES=: the partition's exact size",
            "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=128G\n": "SWAP_SIZE_BYTES=128G",
            "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=0\n": "SWAP_SIZE_BYTES=0",
            "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=1\nSWAP_NAME='a b'\n": "SWAP_NAME=a b",
            "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=1\nSWAP_NAME=../x\n": "SWAP_NAME=../x",
            "SWAP_SIZE_BYTES=4096\n": "SWAP_SIZE_BYTES is set, SWAP_PARTUUID is not",
            "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=1\nSWAP_NAME=$(id)\n": "line 3: SWAP_NAME=$(id)",
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                s = D.parse_swap_settings(text)
                self.assertIsNone(s.spec)
                self.assertEqual(len(s.problems), 1, s.problems)
                self.assertIn(want, s.problems[0])

    def test_a_key_that_is_not_a_setting_is_a_problem(self) -> None:
        good = "SWAP_PARTUUID=0a1b2c3d-02\nSWAP_SIZE_BYTES=8192\n"
        for text, key in (
            (good + "SWAP_NAM=swap1\n", "SWAP_NAM"),  # a typo: not swap0 without a word
            ("SWAP_PARTUID=0a1b2c3d-02\n", "SWAP_PARTUID"),  # not "not configured"
        ):
            with self.subTest(text=text):
                s = D.parse_swap_settings(text)
                self.assertIsNone(s.spec)
                self.assertEqual(
                    s.problems, (f"{key}: not a setting of this file (SWAP_PARTUUID, SWAP_SIZE_BYTES, SWAP_NAME)",)
                )

    def test_render_reads_back(self) -> None:
        text = D.render_swap_settings(TEST_SPEC, "from somewhere")
        self.assertIn("# Written by cst swap apply from somewhere.", text)
        self.assertEqual(D.parse_swap_settings(text), D.SwapSettings(TEST_SPEC))

    def test_the_marked_crypttab_line_names_the_partition(self) -> None:
        mine = "other /dev/sda2 none luks\n"
        want = ("swap0", "612551b1-ca02-4514-86ad-7d8902e612dc")
        self.assertEqual(D.marked_crypttab_entry(mine + OLD_CRYPTTAB + "\n"), want)
        new = TEST_SPEC.crypttab_line + "\n"
        self.assertEqual(D.marked_crypttab_entry(mine + new), want)
        self.assertEqual(D.marked_crypttab_entry(new + new), want)  # the old script ran again
        self.assertIsNone(D.marked_crypttab_entry(mine))
        self.assertIsNone(D.marked_crypttab_entry(new + new.replace("612551b1", "712551b1")))
        self.assertIsNone(D.marked_crypttab_entry(f"swap0 /dev/nvme0n1p5 /dev/urandom swap {NEW}\n"))
        self.assertIsNone(D.marked_crypttab_entry(new.replace("swap0 ", "a#b ", 1)))

    def test_the_marked_fstab_line_names_the_device(self) -> None:
        self.assertEqual(D.marked_fstab_name("UUID=x / btrfs defaults 0 0\n" + OLD_FSTAB + "\n"), "swap0")
        self.assertEqual(D.marked_fstab_name(TEST_SPEC.fstab_line + "\n"), "swap0")
        self.assertIsNone(D.marked_fstab_name("/dev/mapper/swap0 none swap defaults 0 0\n"))


class SwapSettingsTest(_SwapScratch):
    """Which partition plan, status and apply use: /etc/cst/swap.conf, or what an older CST set up."""

    @override
    def setUp(self) -> None:
        super().setUp()
        os.remove(self.env.settings_path)

    def _status_text(self) -> str:
        fs = self._fs()
        service = SwapApplicationService(
            self.env,
            fs,
            SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs),
            FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes),
            installer=swap_installer(),
        )
        out = io.StringIO()
        render_status(service.status_report(), out=out)
        return out.getvalue()

    def _write(self, path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_not_configured_plan_says_so_and_apply_refuses_changing_nothing(self) -> None:
        before = file_tree(self.tmp)
        plan = self._plan_text(self.env)
        self.assertIn(
            "  REFUSE    cst/swap.conf                          no swap partition configured: set SWAP_PARTUUID", plan
        )
        self.assertNotIn("crypttab", plan)
        self.assertNotIn("partition 6", plan)
        self.assertIn("tmpfiles.d/zswap.conf", plan)  # the rest is still shown
        self.assertIn("no swap partition configured", self._status_text())
        rc, out, err, systemd = self._apply_once(self.env, mapper_present=False)
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertEqual(
            err,
            f"cst swap: ERROR: {self.env.settings_path}: no swap partition configured: set SWAP_PARTUUID and"
            " SWAP_SIZE_BYTES (swap/README.md, 2.3); apply stops.\n",
        )
        self.assertEqual(systemd.calls, [])
        self.assertEqual(file_tree(self.tmp), before)

    def test_a_settings_file_with_a_problem_refuses_and_is_kept(self) -> None:
        self._write(self.env.settings_path, "SWAP_PARTUUID=0a1b2c3d-02\n")
        self._write(self.env.crypttab, TEST_SPEC.crypttab_line + "\n")  # no migration over a file of the user's
        before = file_tree(self.tmp)
        self.assertIn("  REFUSE    cst/swap.conf                          SWAP_SIZE_BYTES=", self._plan_text(self.env))
        rc, _out, err, _ = self._apply_once(self.env, mapper_present=False)
        self.assertEqual(rc, 1)
        self.assertIn(f"cst swap: ERROR: {self.env.settings_path}: SWAP_SIZE_BYTES=: ", err)
        self.assertEqual(file_tree(self.tmp), before)

    def test_a_settings_file_without_a_partition_is_not_overwritten(self) -> None:
        self._write(self.env.settings_path, "# to do\n")
        self._write(self.env.crypttab, TEST_SPEC.crypttab_line + "\n")
        rc, _out, err, _ = self._apply_once(self.env, mapper_present=False)
        self.assertEqual(rc, 1)
        self.assertIn("no swap partition configured", err)
        with open(self.env.settings_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# to do\n")

    def test_upgrade_apply_writes_the_settings_from_what_an_older_cst_set_up(self) -> None:
        # What an older CST's apply left: its marked lines, no /etc/cst/swap.conf (the partition was in code).
        self._write(self.env.crypttab, "other /dev/sda2 none luks\n" + OLD_CRYPTTAB + "\n")
        self._write(self.env.fstab, OLD_FSTAB + "\n")
        before = file_tree(self.tmp)
        plan = self._plan_text(self.env)
        self.assertIn(
            "  write     cst/swap.conf                          from the swap0 line an older CST wrote in crypttab:"
            " partition 612551b1-ca02-4514-86ad-7d8902e612dc, 137438953472 bytes (its size now)",
            plan,
        )
        self.assertIn("  adopt     crypttab: swap0 line", plan)
        self.assertIn("  ok        partition 612551b1...", plan)
        self.assertEqual(file_tree(self.tmp), before, "plan wrote something")

        rc, out, err, _ = self._apply_once(self.env, mapper_present=False)
        self.assertEqual(rc, 0, err)
        self.assertIn(
            f"== settings\nwritten:   {self.env.settings_path} (SWAP_PARTUUID=612551b1-ca02-4514-86ad-7d8902e612dc,",
            out,
        )
        self.assertLess(out.index("== settings"), out.index("== swap0: crypttab and fstab"))
        with open(self.env.settings_path, encoding="utf-8") as f:
            self.assertEqual(D.parse_swap_settings(f.read()), D.SwapSettings(TEST_SPEC))
        self.assertEqual(stat.S_IMODE(os.stat(self.env.settings_path).st_mode), 0o644)

        self.assertIn(
            "  ok        cst/swap.conf                          partition 612551b1-ca02-4514-86ad-7d8902e612dc,"
            " 137438953472 bytes",
            self._plan_text(self.env),
        )
        rc, out, err, _ = self._apply_once(self.env, mapper_present=True)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("== settings", out)

    def test_an_upgrade_apply_refused_by_a_hand_written_line_writes_no_settings(self) -> None:
        # The refusals for a line written by hand come before the settings file is written.
        for path, line in (
            (self.env.fstab, "/dev/mapper/swap0 none swap defaults 0 0\n"),
            (self.env.crypttab, "swap0 /dev/nvme0n1p5 none luks\n"),
        ):
            with self.subTest(path=path):
                if os.path.exists(self.env.settings_path):
                    os.remove(self.env.settings_path)  # an older apply's, from the subtest before
                self._write(self.env.crypttab, OLD_CRYPTTAB + "\n")
                self._write(self.env.fstab, OLD_FSTAB + "\n")
                self._write(path, (OLD_CRYPTTAB + "\n" if path == self.env.crypttab else "") + line)
                before = file_tree(self.tmp)
                rc, out, err, _ = self._apply_once(self.env, mapper_present=False)
                self.assertEqual(rc, 1)
                self.assertIn(f"cst swap: ERROR: {path} has an unmarked entry for ", err)
                self.assertEqual(out, "")
                self.assertFalse(os.path.exists(self.env.settings_path))
                self.assertEqual(file_tree(self.tmp), before)

    def test_the_size_comes_from_the_partition_and_apply_still_checks_it(self) -> None:
        self._write(self.env.crypttab, OLD_CRYPTTAB + "\n")
        size = os.path.join(self.env.sys_dir, "class", "block", "nvme0n1p5", "size")
        self._write(size, "2048\n")
        self.assertIn(
            "partition 612551b1-ca02-4514-86ad-7d8902e612dc, 1048576 bytes (its size now)", self._plan_text(self.env)
        )
        rc, _out, err, _ = self._apply_once(self.env, mapper_present=False)  # blockdev says 137438953472
        self.assertEqual(rc, 1)
        self.assertIn("expected 1048576: the PARTUUID points at an unexpected device; refusing.", err)
        self.assertFalse(os.path.exists(self.env.settings_path))

    def test_upgrade_with_the_partition_gone_refuses(self) -> None:
        self._write(self.env.crypttab, OLD_CRYPTTAB + "\n")
        env = self.env
        fs = SwapTempFileSystem({})  # no /dev/disk/by-partuuid link
        service = SwapApplicationService(
            env,
            fs,
            SwapFakeSystemd(os.path.join(env.proc_dir, "swaps"), "/dev/dm-0", fs=fs),
            FakeSwapSystem(env.proc_dir, size_bytes=self.spec.expected_size_bytes),
            installer=swap_installer(),
        )
        out = io.StringIO()
        render_plan(service.plan_report(), out=out)
        self.assertIn(
            "no swap partition configured (the swap0 line in crypttab names"
            " /dev/disk/by-partuuid/612551b1-ca02-4514-86ad-7d8902e612dc, which is not here)",
            out.getvalue(),
        )
        err = io.StringIO()
        self.assertEqual(render_apply(service.apply_changes(), out=io.StringIO(), err=err), 1)
        self.assertFalse(os.path.exists(env.settings_path))

    def _on(self, fs: SwapTempFileSystem) -> tuple[SwapApplicationService, SwapFakeSystemd]:
        systemd = SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        return SwapApplicationService(self.env, fs, systemd, system, installer=swap_installer()), systemd

    def _run_on(self, cmd: RunCommand, fs: SwapTempFileSystem) -> tuple[int, str, str, SwapFakeSystemd]:
        service, systemd = self._on(fs)
        out, err = io.StringIO(), io.StringIO()
        rc = run_command(cmd, service, hint="", out=out, err=err)
        return rc, out.getvalue(), err.getvalue(), systemd

    def _older_install(self) -> None:
        """What an older CST set up: the migration would write /etc/cst/swap.conf from it."""
        self._write(self.env.crypttab, OLD_CRYPTTAB + "\n")
        self._write(self.env.fstab, OLD_FSTAB + "\n")

    def test_a_settings_file_this_user_cannot_read_asks_for_root_and_is_not_taken_as_missing(self) -> None:
        self._older_install()
        self._write(self.env.settings_path, TEST_SETTINGS)
        path = self.env.settings_path
        before = file_tree(self.tmp)
        for cmd in (D.SwapCommand.PLAN, D.SwapCommand.STATUS):
            with self.subTest(cmd=cmd):
                fs = _TextUnreadable(self._fs(unreadable={path}).block_links, unreadable_paths={path}, unread={path})
                rc, out, err, _ = self._run_on(cmd, fs)
                self.assertEqual(rc, 0, err)
                self.assertIn(
                    "  ?         cst/swap.conf                          cannot be read as this user: on the host,"
                    f" cst swap {cmd} --root\n",
                    out,
                )
                self.assertNotIn("  write     cst/swap.conf", out)
                self.assertNotIn("REFUSE    cst/swap.conf", out)
        self.assertEqual(file_tree(self.tmp), before)

    def test_a_settings_path_that_is_not_a_regular_file_refuses_and_apply_writes_nothing(self) -> None:
        self._older_install()  # with the partition there: the migration's write case
        os.makedirs(self.env.settings_path)
        before = file_tree(self.tmp)
        rc, out, err, _ = self._run_on(D.SwapCommand.PLAN, self._fs())
        self.assertEqual(rc, 0, err)
        self.assertIn("  REFUSE    cst/swap.conf                          not a regular file: apply stops\n", out)
        rc, out, err, systemd = self._run_on(D.SwapCommand.APPLY, self._fs())
        self.assertEqual(rc, 1)
        self.assertEqual(err, f"cst swap: ERROR: {self.env.settings_path}: not a regular file: apply stops.\n")
        self.assertNotIn("written:", out)
        self.assertEqual((systemd.daemon_reloads, systemd.calls), (0, []))
        self.assertEqual(file_tree(self.tmp), before)
        self.assertTrue(os.path.isdir(self.env.settings_path))

    def test_a_settings_file_that_cannot_be_read_although_access_allows_it_refuses(self) -> None:
        # Root, and the read still fails (EIO, a security policy): apply stops, nothing is migrated.
        self._older_install()
        self._write(self.env.settings_path, TEST_SETTINGS)
        path = self.env.settings_path
        before = file_tree(self.tmp)
        fs = _TextUnreadable(self._fs().block_links, unread={path})
        rc, out, err, _ = self._run_on(D.SwapCommand.PLAN, fs)
        self.assertEqual(rc, 0, err)
        self.assertIn("  REFUSE    cst/swap.conf                          cannot be read: apply stops\n", out)
        fs = _TextUnreadable(self._fs().block_links, unread={path})
        rc, out, err, _ = self._run_on(D.SwapCommand.APPLY, fs)
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, f"cst swap: ERROR: {path}: cannot be read: apply stops.\n")
        self.assertEqual(file_tree(self.tmp), before)

    def test_a_settings_file_in_a_folder_this_user_may_not_search_is_not_taken_as_missing(self) -> None:
        # /etc/cst at 0700 of root: lexists cannot see swap.conf there, and the migration must not run.
        self._older_install()
        self._write(self.env.settings_path, TEST_SETTINGS)
        closed = os.path.dirname(self.env.settings_path)
        before = file_tree(self.tmp)
        for cmd in (D.SwapCommand.PLAN, D.SwapCommand.STATUS):
            with self.subTest(cmd=cmd):
                rc, out, err, _ = self._run_on(cmd, _ClosedFolder(self._fs().block_links, closed=closed))
                self.assertEqual(rc, 0, err)
                self.assertIn(
                    "  ?         cst/swap.conf                          cannot be read as this user: on the host,"
                    f" cst swap {cmd} --root\n",
                    out,
                )
                self.assertNotIn("  write     cst/swap.conf", out)
        self.assertEqual(file_tree(self.tmp), before)

    def test_a_settings_file_missing_from_a_folder_this_user_may_search_is_migrated(self) -> None:
        # The folder is searchable (not readable: 0711): lexists is right, and the migration runs.
        self._older_install()
        folder = os.path.dirname(self.env.settings_path)
        os.makedirs(folder, exist_ok=True)
        fs = SwapTempFileSystem(self._fs().block_links, unreadable_paths={folder})
        rc, out, err, _ = self._run_on(D.SwapCommand.PLAN, fs)
        self.assertEqual(rc, 0, err)
        self.assertIn("  write     cst/swap.conf", out)

    def test_a_settings_file_root_may_not_read_refuses_and_does_not_ask_for_root(self) -> None:
        # Root, and access refuses (a security policy, a root-squashed mount): --root cannot help.
        self._older_install()
        self._write(self.env.settings_path, TEST_SETTINGS)
        path = self.env.settings_path
        self.env = dataclasses.replace(self.env, as_root=True)
        before = file_tree(self.tmp)
        for cmd in (D.SwapCommand.PLAN, D.SwapCommand.STATUS):
            with self.subTest(cmd=cmd):
                fs = _TextUnreadable(self._fs().block_links, unreadable_paths={path}, unread={path})
                rc, out, err, _ = self._run_on(cmd, fs)
                self.assertEqual(rc, 0, err)
                self.assertIn("  REFUSE    cst/swap.conf                          cannot be read: apply stops\n", out)
                self.assertNotIn("--root", out)
        fs = _TextUnreadable(self._fs().block_links, unreadable_paths={path}, unread={path})
        rc, out, err, _ = self._run_on(D.SwapCommand.APPLY, fs)
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, f"cst swap: ERROR: {path}: cannot be read: apply stops.\n")
        self.assertEqual(file_tree(self.tmp), before)

    def test_as_a_user_the_fstab_line_says_an_older_cst_set_it_up(self) -> None:
        self._write(self.env.crypttab, OLD_CRYPTTAB + "\n")
        self._write(self.env.fstab, OLD_FSTAB + "\n")
        plan = self._plan_text(self.env, unreadable={self.env.crypttab})
        self.assertIn(
            "  ?         cst/swap.conf                          not written yet; an older CST set up swap0 (fstab):"
            " apply writes it from crypttab, which needs root to read: on the host, cst swap plan --root",
            plan,
        )
        self.assertNotIn("  REFUSE", plan)
        os.remove(self.env.fstab)
        self.assertIn("REFUSE    cst/swap.conf", self._plan_text(self.env, unreadable={self.env.crypttab}))

    def test_as_root_a_crypttab_or_fstab_it_cannot_read_says_apply_refuses_not_root(self) -> None:
        # Root, and access refuses (a security policy, a root-squashed mount): --root cannot help, and
        # apply refuses that file before it writes anything.
        self._older_install()
        self._write(self.env.settings_path, TEST_SETTINGS)
        rows = {self.env.crypttab: "crypttab: swap0 line", self.env.fstab: "fstab: /dev/mapper/swap0 line"}
        for as_root in (True, False):
            env = dataclasses.replace(self.env, as_root=as_root)
            for path, row in rows.items():
                for cmd in (D.SwapCommand.PLAN, D.SwapCommand.STATUS):
                    with self.subTest(as_root=as_root, path=path, cmd=cmd):
                        self.env = env
                        rc, out, err, _ = self._run_on(cmd, self._fs(unreadable={path}))
                        self.assertEqual(rc, 0, err)
                        line = next(x for x in out.splitlines() if row in x)
                        if as_root:
                            self.assertEqual(
                                line.split(row, 1)[1].strip(),
                                "cannot be read, even as root: apply refuses before it writes anything",
                            )
                            self.assertNotIn("--root", out)
                        else:
                            self.assertEqual(
                                line.split(row, 1)[1].strip(), f"needs root to read: on the host, cst swap {cmd} --root"
                            )
                        self.assertTrue(line.startswith("  ?  "), line)

    def test_as_root_a_file_apply_writes_whole_that_it_cannot_read_says_apply_refuses_not_root(self) -> None:
        # Root on the host, and access refuses (a security policy, a root-squashed mount): --root cannot help,
        # and apply refuses that file before it writes anything. As a user (root in a toolbox too) the row
        # asks for --root.
        self._older_install()
        self._write(self.env.settings_path, TEST_SETTINGS)
        env = self.env
        dests = (env.tmpfiles_conf, env.etc_path(MM_TMPFILES_ENTRY), env.sysctl_conf, env.etc_path(URESOURCED_ENTRY))
        dests += (env.slice_dropin,)
        for dest in dests:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            self._write(dest, "# edited by hand\n")
        for as_root, in_container in ((True, False), (False, False), (True, True)):
            reader = dataclasses.replace(env, as_root=as_root, in_container=in_container)
            for cmd in (D.SwapCommand.PLAN, D.SwapCommand.STATUS):
                self.env = reader
                rc, out, err, _ = self._run_on(cmd, self._fs(mapper_present=True, unreadable=set(dests)))
                self.assertEqual(rc, 0, err)
                for dest in dests:
                    with self.subTest(as_root=as_root, in_container=in_container, cmd=cmd, dest=dest):
                        short = dest.removeprefix(env.etc_dir.rstrip("/") + "/").removeprefix("systemd/system/")
                        line = next(x for x in out.splitlines() if f" {short} " in x)
                        self.assertTrue(line.startswith("  ?  "), line)
                        want = (
                            "cannot be read, even as root: apply refuses before it writes anything"
                            if as_root and not in_container
                            else f"needs root to read: on the host, cst swap {cmd} --root"
                        )
                        self.assertEqual(line.split(short, 1)[1].strip(), want)

    def test_root_in_a_toolbox_is_still_sent_to_the_host_s_root(self) -> None:
        # sudo cst swap plan in a toolbox: its root is root of the user namespace only, and the host's
        # root-only files are closed to it. On the host, --root reads them: the notes say so, as for a user,
        # and none says "even as root" or that apply stops.
        self._older_install()
        self._write(self.env.settings_path, TEST_SETTINGS)
        box = dataclasses.replace(self.env, as_root=True, in_container=True)
        rows = {self.env.crypttab: "crypttab: swap0 line", self.env.fstab: "fstab: /dev/mapper/swap0 line"}
        for path, row in rows.items():
            for cmd in (D.SwapCommand.PLAN, D.SwapCommand.STATUS):
                with self.subTest(path=path, cmd=cmd):
                    self.env = box
                    rc, out, err, _ = self._run_on(cmd, self._fs(unreadable={path}))
                    self.assertEqual(rc, 0, err)
                    line = next(x for x in out.splitlines() if row in x)
                    self.assertEqual(
                        line.split(row, 1)[1].strip(), f"needs root to read: on the host, cst swap {cmd} --root"
                    )
                    self.assertNotIn("even as root", out)
        # No swap.conf, the fstab line of an older CST, a crypttab closed to it.
        os.remove(self.env.settings_path)
        self.assertIn(
            "  ?         cst/swap.conf                          not written yet; an older CST set up swap0 (fstab):"
            " apply writes it from crypttab, which needs root to read: on the host, cst swap plan --root",
            self._plan_text(box, unreadable={self.env.crypttab}),
        )
        # A swap.conf closed to it: asked for --root, not refused.
        self._write(self.env.settings_path, TEST_SETTINGS)
        path = self.env.settings_path
        fs = _TextUnreadable(self._fs().block_links, unreadable_paths={path}, unread={path})
        rc, out, err, _ = self._run_on(D.SwapCommand.PLAN, fs)
        self.assertEqual(rc, 0, err)
        self.assertIn("  ?         cst/swap.conf                          cannot be read as this user:", out)
        self.assertNotIn("REFUSE    cst/swap.conf", out)

    def test_as_root_an_older_install_whose_crypttab_it_cannot_read_says_apply_refuses(self) -> None:
        # No /etc/cst/swap.conf, the fstab line an older CST wrote, and a crypttab root may not read:
        # the swap.conf row does not ask for --root, and apply stops with it before any write.
        self._older_install()
        self.env = dataclasses.replace(self.env, as_root=True)
        note = (
            "not written yet; an older CST set up swap0 (fstab), and crypttab cannot be read, even as root:"
            " apply refuses before it writes anything"
        )
        before = file_tree(self.tmp)
        for cmd in (D.SwapCommand.PLAN, D.SwapCommand.STATUS):
            with self.subTest(cmd=cmd):
                rc, out, err, _ = self._run_on(cmd, self._fs(unreadable={self.env.crypttab}))
                self.assertEqual(rc, 0, err)
                self.assertIn(f"  ?         cst/swap.conf                          {note}\n", out)
                self.assertNotIn("--root", out)
        rc, out, err, systemd = self._run_on(D.SwapCommand.APPLY, self._fs(unreadable={self.env.crypttab}))
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, f"cst swap: ERROR: {self.env.settings_path}: {note}.\n")
        self.assertEqual((systemd.daemon_reloads, systemd.calls), (0, []))
        self.assertEqual(file_tree(self.tmp), before)


class MemoryLowOverrideTest(_SwapScratch):
    """machine.slice's MemoryLow from a drop-in systemd applies after CST's: an "over" row naming it, no
    DIFF, apply does not fail; DIFF stays for a cgroup that disagrees with the value systemd reports."""

    CST = "/etc/systemd/system/machine.slice.d/10-swap-memory-low.conf"
    RUNTIME = "/run/systemd/system.control/machine.slice.d/50-MemoryLow.conf"
    FOREIGN = "/etc/systemd/system/machine.slice.d/50-memory-low.conf"

    @override
    def setUp(self) -> None:
        super().setUp()
        rc, _out, err, _ = self._apply_once(self.env, mapper_present=False)  # CST's drop-in in place
        self.assertEqual(rc, 0, err)

    def _host_file(self, host_path: str, text: str) -> None:
        p = self.env.host_view(host_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)

    def _cgroup_low(self, value: int) -> None:
        with open(os.path.join(self.env.cgroup_dir, "machine.slice", "memory.low"), "w", encoding="utf-8") as f:
            f.write(f"{value}\n")

    def _run(self, cmd: RunCommand, shown: Mapping[str, str]) -> tuple[int, str, str]:
        fs = self._fs(mapper_present=True)
        service = SwapApplicationService(
            self.env,
            fs,
            SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs, shown=shown),
            FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes),
            installer=swap_installer(),
        )
        out, err = io.StringIO(), io.StringIO()
        rc = run_command(cmd, service, hint="", out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_a_runtime_value_is_an_over_row_and_apply_does_not_fail(self) -> None:
        self._host_file(self.RUNTIME, "[Slice]\nMemoryLow=1G\n")
        self._cgroup_low(2**30)
        shown = {"MemoryLow": str(2**30), "DropInPaths": f"{self.CST} {self.RUNTIME}"}
        note = (
            f"  over: {self.RUNTIME} sets machine.slice's MemoryLow; systemd applies it after CST's"
            " 10-swap-memory-low.conf (drop-ins in file-name order, the last one wins). apply leaves it."
        )
        for cmd in (D.SwapCommand.STATUS, D.SwapCommand.PLAN, D.SwapCommand.APPLY):
            with self.subTest(cmd=cmd):
                rc, out, err = self._run(cmd, shown)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("  over    machine.slice MemoryLow      1.0 GiB   (desired 48.0 GiB)\n", out)
                self.assertIn(note + "\n", out)
                self.assertNotIn("  DIFF", out)
                self.assertNotIn("machine.slice memory.low", out)
                self.assertNotIn("systemctl", out)  # no undo command named
        self.assertIn(
            "over = MemoryLow set by a drop-in systemd applies after CST's, left alone;",
            self._run(D.SwapCommand.PLAN, shown)[1],
        )

    def test_a_live_memory_low_of_other_than_ascii_digits_is_shown_as_it_is(self) -> None:
        # isdigit() takes these: int() refused "²" (a traceback out of status), and read "٣" as 3 bytes.
        for low in ("\N{SUPERSCRIPT TWO}", "\N{ARABIC-INDIC DIGIT THREE}"):
            with self.subTest(low=low):
                with open(os.path.join(self.env.cgroup_dir, "machine.slice", "memory.low"), "w", encoding="utf-8") as f:
                    f.write(low + "\n")
                rc, out, err = self._run(D.SwapCommand.STATUS, {"ActiveState": "active"})
                self.assertEqual(rc, 0, out + err)
                rows = [ln for ln in out.splitlines() if "machine.slice memory.low" in ln]
                self.assertEqual(len(rows), 1, out)
                self.assertIn("DIFF", rows[0])
                self.assertIn(low, rows[0])
                self.assertNotIn("3 B", out)

    def test_a_foreign_50_memory_low_conf_is_an_over_row(self) -> None:
        self._host_file(self.FOREIGN, "[Slice]\nMemoryLow=8G\n")
        self._cgroup_low(8 * 2**30)
        rc, out, _err = self._run(
            D.SwapCommand.STATUS, {"MemoryLow": str(8 * 2**30), "DropInPaths": f"{self.CST} {self.FOREIGN}"}
        )
        self.assertEqual(rc, 0)
        self.assertIn("  over    machine.slice MemoryLow      8.0 GiB   (desired 48.0 GiB)\n", out)
        self.assertIn(f"  over: {self.FOREIGN} sets machine.slice's MemoryLow;", out)

    def test_diff_when_systemd_reports_cst_s_value_but_the_cgroup_disagrees(self) -> None:
        self._cgroup_low(2**30)
        shown = {"MemoryLow": str(48 * 2**30), "DropInPaths": self.CST, "ActiveState": "active"}
        rc, out, _err = self._run(D.SwapCommand.STATUS, shown)
        self.assertEqual(rc, 0)
        self.assertIn("  DIFF    machine.slice memory.low     1.0 GiB   (desired 48.0 GiB)\n", out)
        self.assertNotIn("over", out.replace("over = ", ""))
        rc, out, err = self._run(D.SwapCommand.APPLY, shown)
        self.assertEqual(rc, 1)
        self.assertIn("one or more settings did not apply as expected", err)

    def test_a_slice_systemd_reports_not_active_is_not_checked_whatever_its_cgroup_folder_holds(self) -> None:
        # A cgroup folder an inactive machine.slice left behind reads memory.low 0: not the slice's floor.
        self._cgroup_low(0)
        for active in ("inactive", "failed", "deactivating", "a-word-systemd-adds-later"):
            with self.subTest(active=active):
                shown = {"MemoryLow": str(48 * 2**30), "DropInPaths": self.CST, "ActiveState": active}
                for cmd in (D.SwapCommand.STATUS, D.SwapCommand.APPLY):
                    rc, out, err = self._run(cmd, shown)
                    self.assertEqual(rc, 0, out + err)
                    self.assertEqual(
                        [ln for ln in out.splitlines() if "machine.slice memory.low" in ln],
                        [
                            "  -       machine.slice memory.low     slice not active (no VM running)   (desired 48.0 GiB)"
                        ],
                    )
                    self.assertNotIn("  DIFF", out)

    def test_an_active_slice_s_cgroup_is_compared(self) -> None:
        self._cgroup_low(0)
        shown = {"MemoryLow": str(48 * 2**30), "DropInPaths": self.CST, "ActiveState": "active"}
        rc, out, _err = self._run(D.SwapCommand.STATUS, shown)
        self.assertEqual(rc, 0)
        self.assertIn("  DIFF    machine.slice memory.low     0 B   (desired 48.0 GiB)\n", out)

    def test_a_runtime_value_is_an_over_row_also_on_a_slice_not_active(self) -> None:
        # The over row comes first: a stopped slice does not hide a drop-in that wins over CST's.
        self._host_file(self.RUNTIME, "[Slice]\nMemoryLow=1G\n")
        shown = {"MemoryLow": str(2**30), "DropInPaths": f"{self.CST} {self.RUNTIME}", "ActiveState": "inactive"}
        rc, out, err = self._run(D.SwapCommand.STATUS, shown)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("  over    machine.slice MemoryLow      1.0 GiB   (desired 48.0 GiB)\n", out)
        self.assertNotIn("machine.slice memory.low", out)

    def test_without_a_running_vm_the_floor_is_not_checked(self) -> None:
        # No memory.low in machine.slice's cgroup (no VM running): the row's state is "-".
        os.remove(os.path.join(self.env.cgroup_dir, "machine.slice", "memory.low"))
        rc, out, _err = self._run(D.SwapCommand.STATUS, {})
        self.assertEqual(rc, 0)
        self.assertEqual(
            [ln for ln in out.splitlines() if "machine.slice memory.low" in ln],
            ["  -       machine.slice memory.low     slice not active (no VM running)   (desired 48.0 GiB)"],
        )

    def test_an_active_slice_without_memory_low_says_so(self) -> None:
        # The memory controller is not on for the slice: the floor is not applied, and no VM is said to be
        # missing. The row stays "-": apply does not fail where the controller is absent.
        os.remove(os.path.join(self.env.cgroup_dir, "machine.slice", "memory.low"))
        shown = {"MemoryLow": str(48 * 2**30), "DropInPaths": self.CST, "ActiveState": "active"}
        for cmd in (D.SwapCommand.STATUS, D.SwapCommand.APPLY):
            with self.subTest(cmd=cmd):
                rc, out, err = self._run(cmd, shown)
                self.assertEqual(rc, 0, out + err)
                self.assertEqual(
                    [ln for ln in out.splitlines() if "machine.slice memory.low" in ln],
                    [
                        (
                            "  -       machine.slice memory.low     slice active, no memory.low"
                            " (memory controller off)   (desired 48.0 GiB)"
                        )
                    ],
                )

    def test_without_an_answer_from_systemd_the_cgroup_is_compared(self) -> None:
        self._host_file(self.RUNTIME, "[Slice]\nMemoryLow=1G\n")
        self._cgroup_low(2**30)
        rc, out, _err = self._run(D.SwapCommand.STATUS, {})
        self.assertEqual(rc, 0)
        self.assertIn("  DIFF    machine.slice memory.low     1.0 GiB   (desired 48.0 GiB)\n", out)


class MemoryLowOverrideRuleTest(unittest.TestCase):
    """memory_low_override: which drop-in systemd takes machine.slice's MemoryLow from, if not CST's."""

    CST = "/etc/systemd/system/machine.slice.d/10-swap-memory-low.conf"
    CST_TEXT = "[Slice]\nMemoryLow=48G\n"
    RUN = "/run/systemd/system.control/machine.slice.d/50-MemoryLow.conf"
    WANT = 48 * 2**30

    def test_the_last_drop_in_that_sets_it_by_file_name(self) -> None:
        cpu = "/etc/systemd/system/machine.slice.d/10-cpu-setup.conf"
        game = "/run/systemd/system.control/machine.slice.d/50-CPUWeight.conf"  # the game profile's: no MemoryLow
        dropins = [
            (self.RUN, "[Slice]\nMemoryLow=1G\n"),  # listed first: still applied after 10-
            (cpu, "[Slice]\nCPUWeight=50\n"),
            (self.CST, self.CST_TEXT),
            (game, "[Slice]\nCPUWeight=100\n"),
        ]
        self.assertEqual(D.memory_low_override(str(2**30), dropins, self.CST, self.WANT), self.RUN)
        self.assertIsNone(D.memory_low_override(str(2**30), dropins[1:], self.CST, self.WANT))  # CST's is the last
        self.assertEqual(D.memory_low_override("infinity", dropins, self.CST, self.WANT), self.RUN)

    def test_none_when_cst_s_value_applies_or_its_drop_in_is_not_loaded(self) -> None:
        dropins = [(self.CST, self.CST_TEXT), (self.RUN, "[Slice]\nMemoryLow=48G\n")]
        self.assertIsNone(D.memory_low_override(str(self.WANT), dropins, self.CST, self.WANT))
        old = "/etc/systemd/system/machine.slice.d/50-memory-low.conf"  # before apply renamed it
        self.assertIsNone(D.memory_low_override("0", [(old, self.CST_TEXT)], self.CST, self.WANT))

    def test_none_when_the_later_drop_in_cannot_be_read(self) -> None:
        dropins = [(self.CST, self.CST_TEXT), (self.RUN, None)]
        self.assertIsNone(D.memory_low_override(str(2**30), dropins, self.CST, self.WANT))

    def test_an_assignment_as_systemd_reads_it(self) -> None:
        mine = "/etc/systemd/system/machine.slice.d/60-mine.conf"
        for text, effective in (
            ("[Slice]\nMemoryLow = 32G\n", str(32 * 2**30)),  # blanks around "="
            ("[Slice]\n  MemoryLow=32G\n", str(32 * 2**30)),  # blanks before the key
            ("[Slice]\nMemoryLow=\n", "0"),  # an empty value resets it
        ):
            with self.subTest(text=text):
                dropins = [(self.CST, self.CST_TEXT), (mine, text)]
                self.assertEqual(D.memory_low_override(effective, dropins, self.CST, self.WANT), mine)
        for comment in (
            "[Slice]\n#MemoryLow=1G\n",  # comments, also after blanks
            "[Slice]\n; MemoryLow=1G\n",
            "[Slice]\n  # MemoryLow=1G\n",
            "[Slice]\nMemoryLowX=1G\n",  # other keys
            "[Slice]\nStartupMemoryLow=1G\n",
            "[Slice]\nDefaultMemoryLow=1G\n",
        ):
            with self.subTest(text=comment):
                dropins = [(self.CST, self.CST_TEXT), (mine, comment)]
                self.assertIsNone(D.memory_low_override(str(2**30), dropins, self.CST, self.WANT))


class _ScriptedRunner:
    """CommandRunnerPort answering by program name (argv[0]); records each argv."""

    def __init__(self, results: Mapping[str, CommandResult]) -> None:
        self._results = dict(results)
        self.calls: list[list[str]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        **_options: object,
    ) -> CommandResult:
        del timeout, input_text, env
        self.calls.append(list(argv))
        res = self._results.get(argv[0], CommandResult(0, "", ""))
        if check and res.returncode != 0:
            raise CommandFailed(argv, res.returncode, res.stdout, res.stderr)
        return res


class _ZswapWriteFails(SwapTempFileSystem):
    @override
    def write_text_direct(self, path: str, content: str) -> None:
        raise PermissionError(13, "Permission denied", path)


class _SysfsNoCreate(SwapTempFileSystem):
    """sysfs (kernfs) has no create: opening a name that is not there for writing fails, root or not."""

    @override
    def write_text_direct(self, path: str, content: str) -> None:
        if not os.path.exists(path):
            raise PermissionError(13, "Permission denied", path)
        super().write_text_direct(path, content)


class _SwapNeverActive(SwapFakeSystemd):
    """systemctl start succeeds, and the swap device still does not show up in /proc/swaps."""

    @override
    def start(self, unit: str, *, output: OutputMode = OutputMode.CAPTURE) -> CommandResult:
        del output
        self.calls.append(("start", unit))
        return CommandResult(0, "", "")


class SwapApplyOutputTest(_SwapScratch):
    """apply prints each step as it runs and stops at a failing command with its message and exit status,
    as swap-setup.sh did under `set -Eeuo pipefail`."""

    def _service(
        self,
        *,
        systemd: SwapFakeSystemd | None = None,
        system: FakeSwapSystem | LinuxSwapSystemAdapter | None = None,
        fs: SwapTempFileSystem | None = None,
    ) -> tuple[SwapApplicationService, SwapFakeSystemd]:
        fs = self._fs() if fs is None else fs
        sd = (
            SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
            if systemd is None
            else systemd
        )
        sw = FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes) if system is None else system
        return SwapApplicationService(self.env, fs, sd, sw, installer=swap_installer()), sd

    def _apply(self, service: SwapApplicationService) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        rc = run_command(D.SwapCommand.APPLY, service, hint="", out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_each_step_is_printed_before_the_next_one_runs(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        seen: dict[str, str] = {}
        fs = self._fs()
        systemd = SwapFakeSystemd(
            os.path.join(self.env.proc_dir, "swaps"),
            "/dev/dm-0",
            fs=fs,
            on_call=lambda verb, unit: seen.setdefault(f"{verb} {unit}", out.getvalue()),
        )
        service, _ = self._service(systemd=systemd, fs=fs)
        self.assertEqual(run_command(D.SwapCommand.APPLY, service, hint="", out=out, err=err), 0, err.getvalue())
        before_start = seen["start systemd-cryptsetup@swap0.service"]
        self.assertIn("== swap0: crypttab and fstab", before_start)
        self.assertIn("/dev/mapper/swap0: activating", before_start)
        self.assertIn("zram0: swapping off", seen["stop dev-zram0.swap"])
        self.assertEqual(out.getvalue().count("== zswap"), 1, "each line once")

    def test_every_settings_row_and_verification_note_starts_its_text_in_one_column(self) -> None:
        # The state column fits its longest word (MISSING): no row's label is pushed to the right of
        # the others, and apply's notes under the rows (BAD, later) line up with them.
        os.remove(os.path.join(self.env.zswap_params_dir, "shrinker_enabled"))  # a MISSING row
        with open(os.path.join(self.env.proc_dir, "meminfo"), "w", encoding="utf-8") as f:
            f.write("MemTotal:       97000000 kB\nMemAvailable:       1000 kB\n")  # zram0 cannot be swapped off
        fs = self._fs()
        service, _ = self._service(
            systemd=_SwapNeverActive(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs), fs=fs
        )
        _rc, out, _err = self._apply(service)
        rows = out.split("== verification\n", 1)[1].split("Done.", 1)[0]
        lines = [ln for ln in rows.splitlines() if ln.startswith("  ") and not ln.startswith(("  over:", "  exp:"))]
        words = {ln.split()[0] for ln in lines}
        self.assertLessEqual({"ok", "MISSING", "BAD", "later"}, words, rows)
        self.assertEqual({len(ln) - len(ln[2:].lstrip(" ").split(" ", 1)[1].lstrip(" ")) for ln in lines}, {10}, rows)
        self.assertIn("  BAD     swap0 is not active as swap\n", rows)
        self.assertIn(
            "  later   zram0 still active as swap (gone after a reboot, or apply again under lower pressure)\n", rows
        )

    def test_the_settings_state_column_fits_every_state_word(self) -> None:
        report = D.SettingsReport(
            rows=tuple(SettingCheckRow(state, f"label-{state.name}", "live", "want") for state in D.SettingState),
        )
        out = io.StringIO()
        render_settings(report, out=out, header=False)
        lines = [ln for ln in out.getvalue().splitlines() if "label-" in ln]  # the exp row adds a note
        self.assertEqual(len(lines), len(D.SettingState))
        self.assertEqual({ln.index("label-") for ln in lines}, {10}, out.getvalue())

    def test_verification_has_no_blank_line_before_it(self) -> None:
        rc, out, err = self._apply(self._service()[0])
        self.assertEqual(rc, 0, err)
        self.assertIn("\n== verification\n", out)
        self.assertNotIn("\n\n== verification", out)

    def test_a_failing_systemctl_start_shows_its_message_and_exit_status(self) -> None:
        job = "Job for systemd-cryptsetup@swap0.service failed because the control process exited with error code.\n"
        fs = self._fs()
        systemd = SwapFakeSystemd(
            os.path.join(self.env.proc_dir, "swaps"),
            "/dev/dm-0",
            fs=fs,
            fail_unit="systemd-cryptsetup@swap0.service",
            fail_result=CommandResult(5, "", job),
        )
        rc, out, err = self._apply(self._service(systemd=systemd, fs=fs)[0])
        self.assertEqual(rc, 5)
        self.assertEqual(err, job)
        self.assertIn("/dev/mapper/swap0: activating", out)
        self.assertNotIn("== zswap", out, "apply stops at the failing command")

    def test_a_failing_zram_swapoff_shows_systemctl_before_the_warning(self) -> None:
        fs = self._fs()
        systemd = SwapFakeSystemd(
            os.path.join(self.env.proc_dir, "swaps"),
            "/dev/dm-0",
            fs=fs,
            fail_unit="dev-zram0.swap",
            fail_result=CommandResult(1, "", "Job for dev-zram0.swap failed.\n"),
        )
        rc, _out, err = self._apply(self._service(systemd=systemd, fs=fs)[0])
        self.assertEqual(rc, 0, "zram0 still active is a 'later' note, not a failure, as before")
        self.assertIn("Job for dev-zram0.swap failed.\nWARNING: swapoff of zram0 failed", err)

    def test_a_failing_sysctl_shows_its_message_and_exit_status(self) -> None:
        msg = "sysctl: cannot stat /proc/sys/vm/nope: No such file or directory\n"
        system = FakeSwapSystem(
            self.env.proc_dir, size_bytes=self.spec.expected_size_bytes, sysctl_result=CommandResult(255, "", msg)
        )
        rc, out, err = self._apply(self._service(system=system)[0])
        self.assertEqual(rc, 255)
        self.assertEqual(err, msg)
        self.assertIn("== sysctl", out)
        self.assertIn("zram0: removed", out, "the lines of the steps done before are on screen")
        self.assertNotIn("== machine.slice", out)

    def test_a_failing_daemon_reload_shows_its_message_and_exit_status(self) -> None:
        class _ReloadFails(SwapFakeSystemd):
            @override
            def daemon_reload(self) -> None:
                raise CommandFailed(["systemctl", "daemon-reload"], 4, "", "Failed to reload daemon: Access denied\n")

        fs = self._fs()
        rc, out, err = self._apply(
            self._service(systemd=_ReloadFails(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs), fs=fs)[0]
        )
        self.assertEqual(rc, 4)
        self.assertEqual(err, "Failed to reload daemon: Access denied\n")
        self.assertIn(f"written:   {self.env.fstab}", out)
        self.assertNotIn("Traceback", err)

    def test_a_command_killed_by_a_signal_gives_128_plus_n(self) -> None:
        # bash under set -e exited with 128+N for a command killed by signal N, never with 256-N.
        fs = self._fs()
        systemd = SwapFakeSystemd(
            os.path.join(self.env.proc_dir, "swaps"),
            "/dev/dm-0",
            fs=fs,
            fail_unit="systemd-cryptsetup@swap0.service",
            fail_result=CommandResult(-signal.SIGTERM, "", ""),
        )
        self.assertEqual(self._apply(self._service(systemd=systemd, fs=fs)[0])[0], 128 + signal.SIGTERM)

        class _ReloadKilled(SwapFakeSystemd):
            @override
            def daemon_reload(self) -> None:
                raise CommandFailed(["systemctl", "daemon-reload"], -signal.SIGKILL)

        fs = self._fs()
        killed = _ReloadKilled(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        self.assertEqual(self._apply(self._service(systemd=killed, fs=fs)[0])[0], 128 + signal.SIGKILL)

    def _killer_runner(self, program: str, results: Mapping[str, CommandResult]) -> _ScriptedRunner:
        """A runner where program is a real child that SIGTERM kills (a stub in a scratch PATH), the rest scripted."""
        stubs = tempfile.mkdtemp(prefix="cst-swap-killed-")
        self.addCleanup(shutil.rmtree, stubs)
        stub = os.path.join(stubs, program)
        with open(stub, "w") as f:
            f.write("#!/usr/bin/python3 -I\nimport os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n")
        os.chmod(stub, 0o755)
        self.enterContext(mock.patch.dict(os.environ, {"PATH": stubs}))
        real = SubprocessCommandRunner()

        class _Killer(_ScriptedRunner):
            @override
            def run(
                self,
                argv: Sequence[str],
                *,
                check: bool = False,
                timeout: float | None = None,
                input_text: str | None = None,
                env: Mapping[str, str] | None = None,
                **_options: object,
            ) -> CommandResult:
                if argv[0] != program:
                    return super().run(argv, check=check, timeout=timeout, input_text=input_text, env=env)
                self.calls.append(list(argv))
                return real.run(argv, check=check, timeout=timeout, input_text=input_text, env=env)

        return _Killer(results)

    def test_a_command_killed_by_a_signal_says_so(self) -> None:
        # bash printed "line N: PID Terminated  COMMAND"; cst swap names the command and the signal.
        prog = self.env.prog
        runner = self._killer_runner("blockdev", {})
        rc, _out, err = self._apply(self._service(system=LinuxSwapSystemAdapter(runner, LocalFileSystemAdapter()))[0])
        self.assertEqual(rc, 128 + signal.SIGTERM)
        self.assertEqual(err, f"{prog}: blockdev --getsize64 {self.spec.device_path} was killed by signal 15\n")

    def test_a_killed_sysctl_says_so_after_its_output(self) -> None:
        prog = self.env.prog
        size = CommandResult(0, f"{self.spec.expected_size_bytes}\n", "")
        runner = self._killer_runner("sysctl", {"blockdev": size})
        rc, out, err = self._apply(self._service(system=LinuxSwapSystemAdapter(runner, LocalFileSystemAdapter()))[0])
        self.assertEqual(rc, 128 + signal.SIGTERM, err)
        self.assertTrue(err.endswith(f"{prog}: sysctl -q -p {self.env.sysctl_conf} was killed by signal 15\n"), err)
        self.assertIn("== sysctl", out)
        self.assertNotIn("== machine.slice", out)
        self.assertEqual(D.killed_text("cst swap", (), -9), "cst swap: a command was killed by signal 9")

    def test_a_failing_write_is_an_error_line_not_a_traceback(self) -> None:
        fs = _ZswapWriteFails({self.spec.device_path: "/dev/nvme0n1p5"})
        rc, out, err = self._apply(self._service(fs=fs)[0])
        self.assertEqual(rc, 1)
        self.assertIn("cst swap: ERROR: [Errno 13] Permission denied", err)
        self.assertIn("== zswap", out)

    def test_a_zswap_parameter_this_kernel_lacks_is_skipped_and_apply_goes_on(self) -> None:
        # plan shows it MISSING and the README says it does not make apply fail.
        os.remove(os.path.join(self.env.zswap_params_dir, "shrinker_enabled"))
        fs = _SysfsNoCreate({self.spec.device_path: "/dev/nvme0n1p5"})
        rc, out, err = self._apply(self._service(fs=fs)[0])
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("skipped:   zswap shrinker_enabled (this kernel has no such parameter)", out)
        self.assertIn("== zram", out)
        self.assertFalse(os.path.exists(os.path.join(self.env.zswap_params_dir, "shrinker_enabled")))

    def test_a_failing_blockdev_shows_its_message_and_exit_status(self) -> None:
        runner = _ScriptedRunner(
            {"blockdev": CommandResult(1, "", "blockdev: cannot open /dev/nvme0n1p5: No such device\n")}
        )
        system = LinuxSwapSystemAdapter(runner, LocalFileSystemAdapter())
        rc, out, err = self._apply(self._service(system=system)[0])
        self.assertEqual(rc, 1)
        self.assertEqual(err, "blockdev: cannot open /dev/nvme0n1p5: No such device\n")
        self.assertNotIn("None", out + err)

    def test_blockdev_size_is_compared_as_printed(self) -> None:
        runner = _ScriptedRunner({"blockdev": CommandResult(0, "12345\n", "")})
        system = LinuxSwapSystemAdapter(runner, LocalFileSystemAdapter())
        rc, _out, err = self._apply(self._service(system=system)[0])
        self.assertEqual(rc, 1)
        self.assertIn(f"/dev/disk/by-partuuid/{self.spec.partuuid} is 12345 bytes, expected", err)

    def test_swapon_show_output_is_shown_even_when_it_fails(self) -> None:
        system = FakeSwapSystem(
            self.env.proc_dir,
            size_bytes=self.spec.expected_size_bytes,
            swapon_result=CommandResult(4, "", "swapon: /proc/swaps: open failed\n"),
        )
        rc, _out, err = self._apply(self._service(system=system)[0])
        self.assertEqual(rc, 0, "`swapon --show || true`")
        self.assertIn("swapon: /proc/swaps: open failed\n", err)

    def test_linux_adapter_returns_the_command_results(self) -> None:
        runner = _ScriptedRunner(
            {
                "sysctl": CommandResult(255, "", "sysctl: cannot stat x\n"),
                "swapon": CommandResult(4, "out\n", "err\n"),
            }
        )
        system = LinuxSwapSystemAdapter(runner, LocalFileSystemAdapter())
        self.assertEqual(system.apply_sysctl_file("/x.conf"), CommandResult(255, "", "sysctl: cannot stat x\n"))
        self.assertEqual(system.swapon_show(), CommandResult(4, "out\n", "err\n"))
        self.assertEqual(runner.calls, [["sysctl", "-q", "-p", "/x.conf"], ["swapon", "--show"]])

    def test_fstab_that_is_not_utf8_is_read_and_kept(self) -> None:
        with open(self.env.fstab, "wb") as f:
            f.write(b"# caf\xe9 comment\nUUID=x / btrfs defaults 0 0\n")
        plan = io.StringIO()
        self.assertEqual(run_command(D.SwapCommand.PLAN, self._service()[0], hint="", out=plan, err=io.StringIO()), 0)
        self.assertIn("fstab: /dev/mapper/swap0 line", plan.getvalue())
        rc, _out, err = self._apply(self._service()[0])
        self.assertEqual(rc, 0, err)
        with open(self.env.fstab, "rb") as f:
            self.assertTrue(f.read().startswith(b"# caf\xe9 comment\nUUID=x / btrfs defaults 0 0\n"))


class SwapApplyBranchesTest(_SwapScratch):
    """apply's and the reports' branches no other test reached: a marked line apply cannot read (refused
    before the first write), swap0
    active when its line changed, zram0 that cannot be swapped off now, the root cgroup without
    memory.swap.current, and --root as root."""

    def _swaps(self, *lines: str) -> None:
        with open(os.path.join(self.env.proc_dir, "swaps"), "w", encoding="utf-8") as f:
            f.write("\n".join(["Filename\tType\tSize\tUsed\tPriority", *lines]) + "\n")

    def _run(
        self, *, mapper_present: bool, unreadable: set[str] | None = None, fail_unit: str | None = None
    ) -> tuple[int, str, str, SwapFakeSystemd]:
        fs = self._fs(mapper_present=mapper_present, unreadable=unreadable)
        rc, out, err, systemd, _system = self._apply_on(fs, fail_unit=fail_unit)
        return rc, out, err, systemd

    def test_an_unreadable_crypttab_stops_apply_before_any_change(self) -> None:
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        before = file_tree(self.tmp)
        rc, out, err, systemd = self._run(mapper_present=False, unreadable={self.env.crypttab})
        self.assertEqual(rc, 1)
        self.assertEqual(err, f"cst swap: ERROR: {self.env.crypttab}: unreadable\n")
        self.assertNotIn("== swap0: crypttab and fstab", out)
        self.assertEqual(file_tree(self.tmp), before)
        self.assertEqual((systemd.calls, systemd.daemon_reloads), ([], 0))

    def test_an_unreadable_fstab_stops_apply_before_it_writes_crypttab(self) -> None:
        with open(self.env.fstab, "w", encoding="utf-8") as f:
            f.write("UUID=1 / btrfs defaults 0 0\n")
        before = file_tree(self.tmp)
        rc, out, err, systemd = self._run(mapper_present=False, unreadable={self.env.fstab})
        self.assertEqual(rc, 1)
        self.assertEqual(err, f"cst swap: ERROR: {self.env.fstab}: unreadable\n")
        self.assertNotIn("written:", out)
        self.assertFalse(os.path.exists(self.env.crypttab))
        self.assertEqual(file_tree(self.tmp), before)
        self.assertEqual((systemd.calls, systemd.daemon_reloads), ([], 0))

    def test_a_folder_in_place_of_fstab_stops_apply_before_any_write(self) -> None:
        # As root a folder is "unreadable" (read_text gives None): before, apply wrote crypttab and its
        # .pre-swap-config.bak first, then stopped at fstab.
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        os.makedirs(self.env.fstab)
        before = file_tree(self.tmp)
        rc, out, err, systemd = self._run(mapper_present=False)
        self.assertEqual(rc, 1)
        self.assertEqual(err, f"cst swap: ERROR: {self.env.fstab}: unreadable\n")
        self.assertNotIn("written:", out)
        self.assertEqual(file_tree(self.tmp), before)
        self.assertFalse(os.path.exists(f"{self.env.crypttab}.pre-swap-config.bak"))
        self.assertEqual((systemd.calls, systemd.daemon_reloads), ([], 0))

    def test_a_folder_in_place_of_fstab_stops_apply_before_it_writes_the_settings_file(self) -> None:
        # No /etc/cst/swap.conf, the crypttab line an older CST wrote: apply would write the settings
        # file from it first (resolve_spec's "write").
        os.remove(self.env.settings_path)
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write(OLD_CRYPTTAB + "\n")
        os.makedirs(self.env.fstab)
        before = file_tree(self.tmp)
        rc, out, err, systemd = self._run(mapper_present=False)
        self.assertEqual(rc, 1)
        self.assertEqual(err, f"cst swap: ERROR: {self.env.fstab}: unreadable\n")
        self.assertNotIn("== settings", out)
        self.assertFalse(os.path.exists(self.env.settings_path))
        self.assertEqual(file_tree(self.tmp), before)
        self.assertEqual((systemd.calls, systemd.daemon_reloads), ([], 0))

    def test_an_unmarked_fstab_line_is_named_before_an_unreadable_crypttab(self) -> None:
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        with open(self.env.fstab, "w", encoding="utf-8") as f:
            f.write("/dev/mapper/swap0 none swap defaults 0 0\n")
        before = file_tree(self.tmp)
        rc, _out, err, systemd = self._run(mapper_present=False, unreadable={self.env.crypttab})
        self.assertEqual(rc, 1)
        self.assertEqual(
            err,
            f"cst swap: ERROR: {self.env.fstab} has an unmarked entry for /dev/mapper/swap0: "
            "remove it by hand, then run apply again.\n",
        )
        self.assertEqual(file_tree(self.tmp), before)
        self.assertEqual((systemd.calls, systemd.daemon_reloads), ([], 0))

    def _newer_record(self) -> str:
        """An install record of a newer CST in the scratch tree (a downgrade); its path."""
        record = os.path.join(self.tmp, "var/lib/cst/installed.json")
        os.makedirs(os.path.dirname(record))
        with open(record, "w", encoding="utf-8") as f:
            f.write('{"format": 99, "files": {}}')
        self.env = dataclasses.replace(self.env, install_record=record)
        return record

    def test_a_record_of_a_newer_cst_stops_apply_before_any_write(self) -> None:
        # Before, apply rewrote crypttab and fstab (and their .bak) and started swap0, and only its
        # first put (the zswap section's) refused the record.
        record = self._newer_record()
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        before = file_tree(self.tmp)
        rc, out, err, systemd = self._run(mapper_present=False)
        self.assertEqual(rc, 1)
        self.assertEqual(
            err,
            f"cst swap: ERROR: {record}: format 99, written by a newer CST (this one reads format 1):"
            " nothing was changed\n",
        )
        self.assertNotIn("== swap0: crypttab and fstab", out)
        self.assertNotIn("written:", out)
        self.assertEqual(file_tree(self.tmp), before)
        self.assertFalse(os.path.exists(f"{self.env.crypttab}.pre-swap-config.bak"))
        self.assertEqual((systemd.calls, systemd.daemon_reloads), ([], 0))

    def test_a_record_of_a_newer_cst_stops_apply_before_it_writes_the_settings_file(self) -> None:
        # No /etc/cst/swap.conf, the crypttab line an older CST wrote: apply would write the settings
        # file from it first (resolve_spec's "write").
        record = self._newer_record()
        os.remove(self.env.settings_path)
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write(OLD_CRYPTTAB + "\n")
        before = file_tree(self.tmp)
        rc, out, err, systemd = self._run(mapper_present=False)
        self.assertEqual(rc, 1)
        self.assertIn(f"cst swap: ERROR: {record}: format 99, written by a newer CST", err)
        self.assertNotIn("== settings", out)
        self.assertFalse(os.path.exists(self.env.settings_path))
        self.assertEqual(file_tree(self.tmp), before)
        self.assertEqual((systemd.calls, systemd.daemon_reloads), ([], 0))

    def test_an_unmarked_line_is_named_before_a_record_of_a_newer_cst(self) -> None:
        self._newer_record()
        with open(self.env.fstab, "w", encoding="utf-8") as f:
            f.write("/dev/mapper/swap0 none swap defaults 0 0\n")
        rc, _out, err, _systemd = self._run(mapper_present=False)
        self.assertEqual(rc, 1)
        self.assertIn(f"{self.env.fstab} has an unmarked entry for /dev/mapper/swap0", err)
        self.assertNotIn("newer CST", err)

    def _fresh_scratch(self) -> str:
        """A new scratch tree (removed at the end of the test) for self.env: one per subTest, so each
        refusal starts from a machine apply has not touched yet. Its folder."""
        tmp = tempfile.mkdtemp(prefix="swap-app-test-")
        self.addCleanup(shutil.rmtree, tmp)
        self.env = swap_scratch(tmp, self.spec)
        # An unrelated crypttab line: apply would write swap0's line first.
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        return tmp

    def _apply_on(
        self, fs: SwapTempFileSystem, *, fail_unit: str | None = None
    ) -> tuple[int, str, str, SwapFakeSystemd, FakeSwapSystem]:
        """apply through the command line's run_command on fs, with its systemd and system fakes (fail_unit:
        a unit whose start fails with "Job failed.")."""
        systemd = SwapFakeSystemd(
            os.path.join(self.env.proc_dir, "swaps"),
            "/dev/dm-0",
            fs=fs,
            fail_unit=fail_unit,
            fail_result=CommandResult(3, "", "Job failed.\n"),
        )
        system = FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(self.env, fs, systemd, system, installer=swap_installer())
        out, err = io.StringIO(), io.StringIO()
        rc = run_command(D.SwapCommand.APPLY, service, hint="", out=out, err=err)
        return rc, out.getvalue(), err.getvalue(), systemd, system

    def _assert_refused_before_any_write(
        self, ran: tuple[int, str, str, SwapFakeSystemd, FakeSwapSystem], err_line: str
    ) -> None:
        """apply exited 1 with err_line alone on stderr, printed no step, wrote no .bak and ran nothing."""
        rc, out, err, systemd, system = ran
        self.assertEqual((rc, out, err), (1, "", err_line + "\n"))
        self.assertFalse(os.path.exists(f"{self.env.crypttab}.pre-swap-config.bak"))
        self.assertEqual((systemd.calls, systemd.daemon_reloads), ([], 0))
        self.assertEqual((system.sysctl_calls, system.zram_resets), ([], 0))

    def test_a_folder_in_place_of_a_file_apply_writes_whole_stops_apply_before_any_write(self) -> None:
        # Before, apply wrote crypttab, fstab and their .bak, started swap0 (and, for the later files, set
        # zswap, masked zram, swapped zram0 off and ran sysctl) and only then failed at that file.
        for index in range(len(self.env.managed_files)):
            tmp = self._fresh_scratch()
            _src, dest = self.env.managed_files[index]
            with self.subTest(dest=dest):
                os.makedirs(dest)
                before = file_tree(tmp)
                fs = self._fs()
                self._assert_refused_before_any_write(
                    self._apply_on(fs), f"cst swap: ERROR: {dest}: cannot be read: nothing was changed"
                )
                self.assertEqual(file_tree(tmp), before)
                self.assertTrue(os.path.isdir(dest))
                # The folder gone, the same apply works, and a second one changes nothing more.
                os.rmdir(dest)
                rc, out, err, _systemd, _system = self._apply_on(fs)
                self.assertEqual(rc, 0, out + err)
                self.assertIn("written:   " + self.env.crypttab + " (swap0 entry)", out)
                self.assertTrue(os.path.isfile(dest))
                after = file_tree(tmp)
                rc, out, err, systemd, _system = self._apply_on(fs)
                self.assertEqual(rc, 0, out + err)
                self.assertNotIn("written:", out)
                self.assertEqual(file_tree(tmp), after)
                self.assertEqual((systemd.calls, systemd.daemon_reloads), ([], 0))

    @unittest.skipIf(os.geteuid() == 0, "not as root")
    def test_an_uresourced_conf_root_cannot_read_stops_apply_before_any_write(self) -> None:
        # A real permission denial (mode 0; this process is not root) as apply as root meets one under a
        # policy. Before, apply wrote crypttab, fstab, swap0 and four managed files, then failed with
        # "[Errno 13] Permission denied" at the copy of uresourced.conf aside.
        self.env = dataclasses.replace(self.env, as_root=True)
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        dest = self.env.etc_path(URESOURCED_ENTRY)
        with open(dest, "w", encoding="utf-8") as f:
            f.write("# mine\n")
        before = file_tree(self.tmp)  # file_tree cannot read a mode 0 file
        os.chmod(dest, 0)  # tearDown's rmtree removes it whatever its mode
        self._assert_refused_before_any_write(
            self._apply_on(self._fs()), f"cst swap: ERROR: {dest}: cannot be read: nothing was changed"
        )
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0)
        os.chmod(dest, 0o644)
        self.assertEqual(file_tree(self.tmp), before)
        self.assertFalse(os.path.exists(f"{dest}.pre-swap-config.bak"))

    def test_a_file_apply_writes_whole_that_it_cannot_read_by_policy_stops_apply(self) -> None:
        # Someone else's file that access refuses to read (a security policy: is_readable says no). Before,
        # apply exited 0 and wrote over it, and its bytes were lost.
        for index in range(len(self.env.managed_files)):
            tmp = self._fresh_scratch()
            _src, dest = self.env.managed_files[index]
            with self.subTest(dest=dest):
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8") as f:
                    f.write("# mine\n")
                before = file_tree(tmp)
                self._assert_refused_before_any_write(
                    self._apply_on(self._fs(unreadable={dest})),
                    f"cst swap: ERROR: {dest}: cannot be read: nothing was changed",
                )
                self.assertEqual(file_tree(tmp), before)

    def test_a_link_at_uresourced_conf_to_a_folder_stops_apply_before_any_write(self) -> None:
        # apply copies uresourced.conf aside through the link before it writes it. Before, it failed there
        # with "[Errno 21] Is a directory" after crypttab, fstab, swap0, zswap, zram, sysctl and the mm file.
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        dest = self.env.etc_path(URESOURCED_ENTRY)
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, dest)
        before = file_tree(self.tmp)
        self._assert_refused_before_any_write(
            self._apply_on(self._fs()), f"cst swap: ERROR: {dest}: cannot be read: nothing was changed"
        )
        self.assertEqual(file_tree(self.tmp), before)
        self.assertEqual(os.readlink(dest), elsewhere)  # file_tree does not list a link to a folder
        self.assertFalse(os.path.lexists(f"{dest}.pre-swap-config.bak"))
        # As root on the host, plan's row of the file says apply refuses it.
        plan = self._plan_text(dataclasses.replace(self.env, as_root=True))
        line = next(x for x in plan.splitlines() if " uresourced.conf " in x)
        self.assertTrue(line.startswith("  ?  "), line)
        self.assertTrue(
            line.endswith(
                " uresourced.conf                        cannot be read, even as root: apply refuses before"
                " it writes anything"
            ),
            line,
        )
        # A link to a file it reads is copied aside and replaced by CST's file, as before.
        os.remove(dest)
        mine = os.path.join(self.tmp, "mine.conf")
        with open(mine, "w", encoding="utf-8") as f:
            f.write("# mine\n")
        os.symlink(mine, dest)
        rc, out, err, _systemd, _system = self._apply_on(self._fs())
        self.assertEqual(rc, 0, out + err)
        with open(f"{dest}.pre-swap-config.bak", encoding="utf-8") as f:
            self.assertEqual(f.read(), "# mine\n")
        self.assertFalse(os.path.islink(dest))
        with open(dest, encoding="utf-8") as f, open(self.env.src_path(URESOURCED_ENTRY), encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read())

    def test_a_link_at_uresourced_conf_to_a_fifo_is_refused_without_reading_through_it(self) -> None:
        # The open of a FIFO blocks until something writes into it (a device such as /dev/zero never ends).
        # Before, plan, status and apply's check before its first write read the file through the link, and
        # plan hung there; apply's copy aside of a FIFO fails.
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        dest = self.env.etc_path(URESOURCED_ENTRY)
        elsewhere = tempfile.mkdtemp(prefix="swap-fifo-")  # outside self.tmp: file_tree would open the FIFO
        self.addCleanup(shutil.rmtree, elsewhere)
        fifo = os.path.join(elsewhere, "uresourced.fifo")
        os.mkfifo(fifo)
        os.symlink(fifo, dest)
        before = file_tree(self.tmp)
        fs = _ReadsRecorded(self._fs().block_links, unopened={dest})
        self._assert_refused_before_any_write(
            self._apply_on(fs), f"cst swap: ERROR: {dest}: cannot be read: nothing was changed"
        )
        plan = self._plan_text(dataclasses.replace(self.env, as_root=True), fs=fs)
        line = next(x for x in plan.splitlines() if " uresourced.conf " in x)
        self.assertTrue(line.startswith("  ?  "), line)
        self.assertNotIn(dest, fs.reads)
        self.assertEqual(file_tree(self.tmp), before)
        self.assertEqual(os.readlink(dest), fifo)
        self.assertFalse(os.path.lexists(f"{dest}.pre-swap-config.bak"))

    @unittest.skipIf(os.geteuid() == 0, "not as root")
    def test_a_link_at_uresourced_conf_to_a_file_root_cannot_read_stops_apply_before_any_write(self) -> None:
        # The copy aside follows the link, and _inspect_file reads nothing through a link: the check before the
        # first write looks at the file the link leads to (mode 0; this process is not root, as apply as root
        # under a policy).
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        dest = self.env.etc_path(URESOURCED_ENTRY)
        elsewhere = tempfile.mkdtemp(prefix="swap-mode0-")  # outside self.tmp: file_tree cannot read a mode 0 file
        self.addCleanup(shutil.rmtree, elsewhere)
        mine = os.path.join(elsewhere, "mine.conf")
        with open(mine, "w", encoding="utf-8") as f:
            f.write("# mine\n")
        os.chmod(mine, 0)
        os.symlink(mine, dest)
        before = file_tree(self.tmp)
        self._assert_refused_before_any_write(
            self._apply_on(self._fs()), f"cst swap: ERROR: {dest}: cannot be read: nothing was changed"
        )
        self.assertEqual(file_tree(self.tmp), before)
        self.assertEqual(os.readlink(dest), mine)
        self.assertEqual(stat.S_IMODE(os.stat(mine).st_mode), 0)
        self.assertFalse(os.path.lexists(f"{dest}.pre-swap-config.bak"))

    def test_a_link_at_uresourced_conf_to_a_folder_is_replaced_once_its_copy_aside_is_there(self) -> None:
        # An earlier apply left uresourced.conf.pre-swap-config.bak: step 7 copies nothing aside, so the link
        # is no reason to refuse; the installer replaces it with CST's file.
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        dest = self.env.etc_path(URESOURCED_ENTRY)
        bak = f"{dest}.pre-swap-config.bak"
        with open(bak, "w", encoding="utf-8") as f:
            f.write("# the copy an earlier apply made\n")
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, dest)
        plan = self._plan_text(dataclasses.replace(self.env, as_root=True))
        line = next(x for x in plan.splitlines() if " uresourced.conf " in x)
        self.assertFalse(line.startswith("  ?  "), line)
        rc, out, err, _systemd, _system = self._apply_on(self._fs())
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(os.path.islink(dest))
        with open(dest, encoding="utf-8") as f, open(self.env.src_path(URESOURCED_ENTRY), encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read())
        with open(bak, encoding="utf-8") as f:
            self.assertEqual(f.read(), "# the copy an earlier apply made\n")
        self.assertTrue(os.path.isdir(elsewhere))

    def test_a_folder_where_apply_puts_a_link_or_removes_a_file_stops_apply_before_any_write(self) -> None:
        # zram-generator.conf (apply renames a link over it) and the legacy sysctl file (apply unlinks it).
        # Before, apply wrote crypttab and fstab, started swap0, set zswap (and, for the sysctl file, masked
        # zram) and only then failed with "Is a directory". A link to a folder there is no reason to refuse:
        # the rename replaces the link, the unlink removes it.
        etc = self.env.etc_dir
        for rel in (os.path.relpath(self.env.zram_conf, etc), os.path.relpath(self.env.old_sysctl_conf, etc)):
            tmp = self._fresh_scratch()
            path = os.path.join(self.env.etc_dir, rel)
            with self.subTest(path=rel):
                os.makedirs(path)
                before = file_tree(tmp)
                self._assert_refused_before_any_write(
                    self._apply_on(self._fs()),
                    f"cst swap: ERROR: {path}: a folder is in its place: nothing was changed",
                )
                self.assertEqual(file_tree(tmp), before)
                self.assertTrue(os.path.isdir(path))
                # A link to a folder in its place: apply works.
                os.rmdir(path)
                elsewhere = os.path.join(tmp, "elsewhere")
                os.makedirs(elsewhere)
                os.symlink(elsewhere, path)
                rc, out, err, _systemd, _system = self._apply_on(self._fs())
                self.assertEqual(rc, 0, out + err)
                if path == self.env.zram_conf:
                    self.assertEqual(os.readlink(path), "/dev/null")
                else:
                    self.assertFalse(os.path.lexists(path))
                self.assertTrue(os.path.isdir(elsewhere))

    def test_a_file_in_place_of_the_folder_of_a_file_apply_writes_whole_stops_apply_before_any_write(self) -> None:
        # The installer creates a missing folder, and cannot create one below a file. Before, plan's row said
        # "write", and apply wrote crypttab and fstab, started swap0 and the steps before that file, and
        # only then failed with "Not a directory".
        etc = self.env.etc_dir
        cases: dict[str, str] = {}  # the name made a file -> the first file apply checks below it (relative to etc)
        for _src, dest in self.env.managed_files:
            if os.path.dirname(dest) != etc:
                cases.setdefault(os.path.relpath(os.path.dirname(dest), etc), os.path.relpath(dest, etc))
        cases[os.path.join("systemd", "system")] = os.path.relpath(self.env.slice_dropin, etc)  # one higher up
        self.assertEqual(len(cases), 4)
        for rel_blocked, rel_dest in cases.items():
            tmp = self._fresh_scratch()
            blocked = os.path.join(self.env.etc_dir, rel_blocked)
            dest = os.path.join(self.env.etc_dir, rel_dest)
            with self.subTest(blocked=rel_blocked):
                if os.path.isdir(blocked):
                    shutil.rmtree(blocked)
                os.makedirs(os.path.dirname(blocked), exist_ok=True)
                with open(blocked, "w", encoding="utf-8") as f:
                    f.write("# not a folder\n")
                before = file_tree(tmp)
                self._assert_refused_before_any_write(
                    self._apply_on(self._fs()),
                    f"cst swap: ERROR: {dest}: {blocked} is not a folder: nothing was changed",
                )
                self.assertEqual(file_tree(tmp), before)
                # The file gone, the same apply works.
                os.remove(blocked)
                rc, out, err, _systemd, _system = self._apply_on(self._fs())
                self.assertEqual(rc, 0, out + err)
                self.assertTrue(os.path.isfile(dest))

    def test_a_missing_source_stops_apply_before_any_write(self) -> None:
        # A copy of CST without units/uresourced.conf. Before, apply wrote crypttab, fstab, swap0 and the
        # earlier files first, and stopped only at that step.
        src = os.path.join(self.tmp, "src", "swap")
        for folder in ("tmpfiles", "sysctl", "units"):
            shutil.copytree(os.path.join(SWAP, folder), os.path.join(src, folder))
        self.env = dataclasses.replace(self.env, src_dir=src)
        missing = self.env.src_path(URESOURCED_ENTRY)
        os.remove(missing)
        with open(self.env.crypttab, "w", encoding="utf-8") as f:
            f.write("other /dev/sdz none\n")
        before = file_tree(self.tmp)
        self._assert_refused_before_any_write(
            self._apply_on(self._fs()), f"cst swap: ERROR: swap: {missing} is missing (incomplete copy of CST?)"
        )
        self.assertEqual(file_tree(self.tmp), before)

    def test_an_empty_active_swap0_whose_line_changed_is_activated_again(self) -> None:
        self._swaps("/dev/dm-0 partition 134217724 0 -2", "/dev/zram0 partition 8388604 102400 100")
        rc, out, err, systemd = self._run(mapper_present=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("/dev/mapper/swap0: entry changed and the device is empty: re-activating\n", out)
        self.assertEqual(
            systemd.calls[:3],
            [
                ("stop", "dev-mapper-swap0.swap"),
                ("restart", "systemd-cryptsetup@swap0.service"),
                ("start", "dev-mapper-swap0.swap"),
            ],
        )
        self.assertEqual(systemd.calls[3:], [("stop", "dev-zram0.swap"), ("stop", "systemd-zram-setup@zram0.service")])

    def test_a_failing_step_of_the_reactivation_stops_apply_with_its_status(self) -> None:
        self._swaps("/dev/dm-0 partition 134217724 0 -2")
        rc, out, err, systemd = self._run(mapper_present=True, fail_unit="systemd-cryptsetup@swap0.service")
        self.assertEqual(rc, 3)
        self.assertEqual(err, "Job failed.\n")
        self.assertIn("re-activating", out)
        self.assertNotIn("== zswap", out)
        self.assertEqual(
            systemd.calls, [("stop", "dev-mapper-swap0.swap"), ("restart", "systemd-cryptsetup@swap0.service")]
        )

    def test_an_active_swap0_in_use_whose_line_changed_keeps_running_with_a_warning(self) -> None:
        self._swaps("/dev/dm-0 partition 134217724 2048 -2")
        rc, out, err, systemd = self._run(mapper_present=True)
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "WARNING: /dev/mapper/swap0: entry changed but 2 MiB are in use; "
            "the live device keeps the old options until reboot.\n",
            err,
        )
        self.assertNotIn("re-activating", out)
        self.assertEqual(systemd.calls, [])

    def test_plan_says_later_for_a_zram0_free_ram_cannot_take(self) -> None:
        with open(os.path.join(self.env.proc_dir, "meminfo"), "w", encoding="utf-8") as f:
            f.write("MemTotal: 8000000 kB\nMemAvailable: 500 kB\n")
        plan = self._plan_text(self.env)
        self.assertRegex(plan, r"later +zram0 +active; not enough free RAM to swap it off now: gone after a reboot")
        self.assertNotRegex(plan, r"swapoff +zram0")

    def test_cgroup_numbers_of_other_than_ascii_digits_are_no_numbers(self) -> None:
        # memory.swap.current, memory.low and zswap's pool_total_size: isdigit() takes "²", int() refused it.
        cg = os.path.join(self.env.cgroup_dir, "machine.slice")
        for name in ("memory.swap.current", "memory.low"):
            with open(os.path.join(cg, name), "w", encoding="utf-8") as f:
                f.write("\N{SUPERSCRIPT TWO}\n")
        os.makedirs(self.env.zswap_debug_dir, exist_ok=True)
        with open(os.path.join(self.env.zswap_debug_dir, "pool_total_size"), "w", encoding="utf-8") as f:
            f.write("\N{ARABIC-INDIC DIGIT THREE}\n")
        fs = self._fs()
        service = SwapApplicationService(
            self.env,
            fs,
            SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs),
            FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes),
            installer=swap_installer(),
        )
        report = service.status_report(include_config=False)
        rows = {r.label: r for r in report.cgroups}
        self.assertIsNone(rows["machine.slice"].row["swap"])
        self.assertEqual(rows["machine.slice"].low_display, "\N{SUPERSCRIPT TWO}")
        assert report.debug is not None
        pool = [r for r in report.debug.rows if r.name == "pool_total_size"]
        self.assertEqual([r.value for r in pool], ["\N{ARABIC-INDIC DIGIT THREE}"])

    def test_the_whole_system_row_takes_its_swap_from_meminfo_without_memory_swap_current(self) -> None:
        with open(os.path.join(self.env.proc_dir, "meminfo"), "a", encoding="utf-8") as f:
            f.write("SwapTotal:       4096 kB\nSwapFree:        1024 kB\n")
        with open(
            os.path.join(self.env.cgroup_dir, "machine.slice", "memory.swap.current"), "w", encoding="utf-8"
        ) as f:
            f.write("512\n")
        fs = self._fs()
        service = SwapApplicationService(
            self.env,
            fs,
            SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs),
            FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes),
            installer=swap_installer(),
        )
        rows = {r.label: r for r in service.status_report(include_config=False).cgroups}
        self.assertEqual(rows["(whole system)"].row["swap"], 3072 * 1024, "SwapTotal - SwapFree, in bytes")
        self.assertEqual(rows["machine.slice"].row["swap"], 512)
        self.assertEqual(rows["machine.slice"].low_display, "48.0 GiB")
        for low in ("0", "max"):
            with open(os.path.join(self.env.cgroup_dir, "machine.slice", "memory.low"), "w", encoding="utf-8") as f:
                f.write(low + "\n")
            rows = {r.label: r for r in service.status_report(include_config=False).cgroups}
            self.assertEqual(rows["machine.slice"].low_display, low)
        os.remove(os.path.join(self.env.proc_dir, "meminfo"))
        rows = {r.label: r for r in service.status_report(include_config=False).cgroups}
        self.assertIsNone(rows["(whole system)"].row["swap"], "no meminfo: no number")

    def test_root_on_the_host_goes_on_without_sudo(self) -> None:
        def _no_elevate(argv: list[str]) -> NoReturn:
            raise AssertionError(f"already root: no sudo, got {argv}")

        ctx = _FakeContext(container=False, root=True, etc_dir=self.env.etc_dir, run_dir=os.path.join(self.tmp, "run"))
        env = {
            "CST_PROG": "cst swap",
            "CST_SWAP_PROC": self.env.proc_dir,
            "CST_SWAP_SYS": os.path.join(self.tmp, "sys"),
        }
        out, err = io.StringIO(), io.StringIO()
        rc = execute_cli(
            ["plan", "--root"],
            env=env,
            out=out,
            err=err,
            runner=_ScriptedRunner({}),
            fs=self._fs(),
            ctx=ctx,
            elevate=_no_elevate,
        )
        self.assertEqual(rc, 0, err.getvalue())
        self.assertIn("/dev/nvme0n1p5, 137438953472 bytes as expected", out.getvalue())
        self.assertIn("vm.swappiness", out.getvalue())


class SwapArgumentsTest(unittest.TestCase):
    """The command line, as swap-setup.sh parsed it."""

    def test_an_empty_command_is_help(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(execute_cli([""], env={"CST_PROG": "cst swap"}, out=out, err=err), 0)
        self.assertIn("Usage: cst swap <command> [args]", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_only_the_first_argument_of_plan_and_status_is_read(self) -> None:
        box = _FakeContext(container=True, root=False, etc_dir="/nonexistent", run_dir="/nonexistent")
        for cmd in ("plan", "status"):
            with self.subTest(cmd=cmd):
                err = io.StringIO()
                rc = execute_cli(
                    [cmd, "--root", "x"], env={"CST_PROG": "cst swap"}, out=io.StringIO(), err=err, ctx=box
                )
                self.assertEqual(rc, 1)
                self.assertIn(f"'cst swap {cmd} --root x' must run on the host", err.getvalue())
                err = io.StringIO()
                self.assertEqual(execute_cli([cmd, "--nope", "--root"], env={}, out=io.StringIO(), err=err), 2)
                self.assertEqual(err.getvalue(), f"{cmd}: unknown argument '--nope' (only --root)\n")


class SwapLocaleHandoverTest(unittest.TestCase):
    """The caller's LC_CTYPE reaches the commands `cst swap` runs (Python sets C.UTF-8 under a C locale, PEP 538)."""

    def test_swap_setup_hands_lc_ctype_over(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-swap-lc-")
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copy2(SETUP, tmp)
        os.makedirs(os.path.join(tmp, "swap"))
        with open(os.path.join(tmp, "swap", "cli.py"), "w", encoding="utf-8") as fh:
            fh.write("import os\nprint(repr(os.environ.get('CST_LC_CTYPE')))\n")
        for caller, handed in (({}, "''"), ({"LC_CTYPE": "C"}, "'=C'"), ({"LC_CTYPE": ""}, "'='")):
            with self.subTest(caller=caller):
                proc = subprocess.run(
                    ["bash", os.path.join(tmp, "swap-setup.sh"), "help"],
                    capture_output=True,
                    text=True,
                    check=False,
                    stdin=subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/bin", **caller},
                    timeout=60,
                )
                self.assertEqual(proc.stdout, handed + "\n", proc.stderr)

    def test_main_puts_the_caller_s_lc_ctype_back(self) -> None:
        saved = {sig: signal.getsignal(sig) for sig in (signal.SIGPIPE, signal.SIGXFSZ)}
        for sig, handler in saved.items():
            self.addCleanup(signal.signal, sig, handler)
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"CST_LC_CTYPE": "=C", "LC_CTYPE": "C.UTF-8"}, clear=True),
            mock.patch.object(sys, "argv", ["swap/cli.py", "help"]),
            mock.patch.object(sys, "stdout", out),
        ):
            self.assertEqual(swap_main(), 0)
            self.assertEqual(dict(os.environ), {"LC_CTYPE": "C"}, "what the commands it runs inherit")
        self.assertIn("Usage: cst swap", out.getvalue())

    def test_the_sudo_rerun_hands_lc_ctype_over(self) -> None:
        called: list[list[str]] = []

        def _fake_elevate(argv: list[str]) -> NoReturn:
            called.append(argv)
            raise SystemExit(99)

        ctx = _FakeContext(container=False, root=False, etc_dir="/nonexistent", run_dir="/nonexistent")
        for env, setting in (({}, "CST_LC_CTYPE="), ({"LC_CTYPE": "C"}, "CST_LC_CTYPE==C")):
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                with self.assertRaises(SystemExit):
                    execute_cli(["apply"], env={"CST_PROG": "cst swap"}, ctx=ctx, elevate=_fake_elevate)
                self.assertEqual(called[-1][:5], ["sudo", "--", "env", "CST_PROG=cst swap", setting])


class SwapWiringTest(unittest.TestCase):
    """swap/wiring.py: what cst swap takes from the process (the composition root)."""

    def test_the_sudo_rerun_starts_swap_cli_again(self) -> None:
        called: list[list[str]] = []

        def _fake_elevate(argv: list[str]) -> NoReturn:
            called.append(argv)
            raise SystemExit(99)

        ctx = _FakeContext(container=False, root=False, etc_dir="/nonexistent", run_dir="/nonexistent")
        with mock.patch.dict(os.environ, {"LC_CTYPE": "C"}, clear=True), self.assertRaises(SystemExit):
            execute_cli(["status", "--root"], env={"CST_PROG": "cst swap"}, ctx=ctx, elevate=_fake_elevate)
        cli = os.path.realpath(os.path.join(SWAP, "cli.py"))
        self.assertEqual(
            called,
            [
                [
                    "sudo",
                    "--",
                    "env",
                    "CST_PROG=cst swap",
                    "CST_LC_CTYPE==C",
                    sys.executable,
                    "-I",
                    "-B",
                    cli,
                    "status",
                    "--root",
                ]
            ],
        )

    def test_the_environment_is_read_when_asked(self) -> None:
        with mock.patch.dict(os.environ, {"CST_PROG": "my swap"}, clear=True):
            out = io.StringIO()
            self.assertEqual(execute_cli(["help"], out=out, err=io.StringIO()), 0)
        self.assertIn("Usage: my swap <command> [args]", out.getvalue())

    def test_the_kernel_dirs_come_from_the_environment_only_when_set(self) -> None:
        ctx = _FakeContext(container=False, root=True, etc_dir="/etc", run_dir="/run")
        plain = swap_wiring.desired_paths("cst swap", ctx, fallback_root="/nowhere", env={"CST_SWAP_PROC": ""})
        self.assertEqual(
            (plain.proc_dir, plain.sys_dir, plain.zswap_params_dir, plain.zswap_debug_dir, plain.cgroup_dir),
            ("/proc", "/sys", "/sys/module/zswap/parameters", "/sys/kernel/debug/zswap", "/sys/fs/cgroup"),
        )
        env = {"CST_SWAP_PROC": "/w/proc", "CST_SWAP_SYS": "/w/sys"}
        moved = swap_wiring.desired_paths("cst swap", ctx, fallback_root="/nowhere", env=env)
        self.assertEqual(
            (moved.proc_dir, moved.sys_dir, moved.zswap_params_dir, moved.zswap_debug_dir, moved.cgroup_dir),
            ("/w/proc", "/w/sys", "/w/sys/module/zswap/parameters", "/w/sys/kernel/debug/zswap", "/sys/fs/cgroup"),
        )

    def test_the_cli_reads_the_kernel_dirs_from_its_environment(self) -> None:
        # plan in a scratch /proc: the live sysctl values come from there.
        tmp = tempfile.mkdtemp(prefix="swap-kdirs-")
        self.addCleanup(shutil.rmtree, tmp)
        vm = os.path.join(tmp, "proc", "sys", "vm")
        os.makedirs(vm)
        with open(os.path.join(vm, "swappiness"), "w", encoding="utf-8") as f:
            f.write("77\n")
        ctx = _FakeContext(
            container=False, root=False, etc_dir=os.path.join(tmp, "etc"), run_dir=os.path.join(tmp, "run")
        )
        out = io.StringIO()
        env = {"CST_SWAP_PROC": os.path.join(tmp, "proc"), "CST_SWAP_SYS": os.path.join(tmp, "sys")}
        runner = _ScriptedRunner({})  # never the real systemctl (nor flatpak-spawn --host from a toolbox)
        self.assertEqual(execute_cli(["plan"], env=env, out=out, err=io.StringIO(), runner=runner, ctx=ctx), 0)
        self.assertRegex(out.getvalue(), r"DIFF    vm\.swappiness +77 ")
        self.assertEqual(
            runner.calls,
            [
                ["systemctl", "show", "-p", "ActiveState", "uresourced.service"],
                ["systemctl", "show", "-p", "MemoryLow", "-p", "DropInPaths", "-p", "ActiveState", "machine.slice"],
            ],
        )

    def _seam_output(self, cmd: RunCommand, env: Mapping[str, str]) -> str:
        # cst swap's own wiring, as root on a scratch /etc that is not there: apply stops before any write.
        tmp = tempfile.mkdtemp(prefix="swap-seam-")
        self.addCleanup(shutil.rmtree, tmp)
        ctx = _FakeContext(
            container=False, root=True, etc_dir=os.path.join(tmp, "etc"), run_dir=os.path.join(tmp, "run")
        )
        env = {k: v.replace("TMP", tmp) for k, v in env.items()}
        runner = _ScriptedRunner({})  # never the real systemctl
        service = swap_wiring.build_service("cst swap", ctx, runner, None, fallback_root="/nowhere", env=env)
        out = io.StringIO()
        run_command(cmd, service, hint="", out=out, err=io.StringIO())
        return out.getvalue().replace(tmp, "TMP")

    def test_a_test_seam_in_the_environment_is_the_first_line_of_plan_status_and_apply(self) -> None:
        both = {"CST_SWAP_PROC": "TMP/proc", "CST_SWAP_SYS": "TMP/sys"}
        for cmd in (D.SwapCommand.PLAN, D.SwapCommand.STATUS, D.SwapCommand.APPLY):
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    self._seam_output(cmd, both).splitlines()[0],
                    "test seam (CST_SWAP_PROC, CST_SWAP_SYS): /proc in TMP/proc, /sys in TMP/sys, not the kernel's;"
                    " /etc is the real one, and sysctl, modprobe and zramctl run as PATH finds them",
                )
        self.assertEqual(
            self._seam_output(D.SwapCommand.PLAN, {"CST_SWAP_PROC": "", "CST_SWAP_SYS": "TMP/sys"}).splitlines()[0],
            "test seam (CST_SWAP_SYS): /sys in TMP/sys, not the kernel's; /etc is the real one, and sysctl,"
            " modprobe and zramctl run as PATH finds them",
        )

    def test_the_test_seam_line_comes_before_apply_s_first_error_on_one_pipe(self) -> None:
        # stdout and stderr on one pipe: stdout's text reaches it only when flushed, stderr's at once.
        tmp = tempfile.mkdtemp(prefix="swap-seam-")
        self.addCleanup(shutil.rmtree, tmp)
        ctx = _FakeContext(
            container=False, root=True, etc_dir=os.path.join(tmp, "etc"), run_dir=os.path.join(tmp, "run")
        )
        env = {"CST_SWAP_PROC": os.path.join(tmp, "proc"), "CST_SWAP_SYS": os.path.join(tmp, "sys")}
        service = swap_wiring.build_service(
            "cst swap", ctx, _ScriptedRunner({}), None, fallback_root="/nowhere", env=env
        )
        pipe: list[str] = []
        out, err = _BufferedToPipe(pipe), _UnbufferedToPipe(pipe)
        self.assertEqual(run_command(D.SwapCommand.APPLY, service, hint="", out=out, err=err), 1)
        self.assertGreaterEqual(len(pipe), 2, pipe)
        self.assertTrue(pipe[0].startswith("test seam (CST_SWAP_PROC, CST_SWAP_SYS): "), pipe)
        self.assertIn("ERROR: ", pipe[1])

    def test_without_a_test_seam_there_is_no_such_line(self) -> None:
        for env in ({}, {"CST_SWAP_PROC": "", "CST_SWAP_SYS": ""}):
            for cmd in (D.SwapCommand.PLAN, D.SwapCommand.APPLY):
                with self.subTest(env=env, cmd=cmd):
                    out = self._seam_output(cmd, env)
                    self.assertNotIn("test seam", out)

    def test_the_desired_files_come_from_this_checkout(self) -> None:
        ctx = _FakeContext(container=True, root=False, etc_dir="/x/etc", run_dir="/x/run")
        self.assertEqual(
            swap_wiring.desired_paths("cst swap", ctx, fallback_root="/nowhere"),
            SwapEnvironment(
                src_dir=os.path.join(os.path.realpath(REPO), "swap"),
                etc_dir="/x/etc",
                cpu_run_dir="/x/run/cpu-setup",
                seats_dir="/x/run/systemd/seats",
                prog="cst swap",
                install_record="/x/var/lib/cst/installed.json",
                in_container=True,
            ),
        )

    def test_root_in_a_toolbox_is_not_the_host_s_root(self) -> None:
        # Root of a toolbox's user namespace: the host's root-only files may be closed to it, so its notes
        # still send the user to --root on the host (cst doctor's rule: uid 0 and no container).
        for container, root, host_root in ((True, True, False), (False, True, True), (True, False, False)):
            with self.subTest(container=container, root=root):
                ctx = _FakeContext(container=container, root=root, etc_dir="/x/etc", run_dir="/x/run")
                env = swap_wiring.desired_paths("cst swap", ctx, fallback_root="/nowhere")
                self.assertEqual((env.as_root, env.in_container, env.host_root), (root, container, host_root))


class SwapStatusDiagnosisTest(_SwapScratch):
    """`cst swap status` appends an objective `== diagnosis` block using `perf` memory rules."""

    def _diag_service(self) -> SwapApplicationService:
        fs = self._fs()
        return SwapApplicationService(
            self.env,
            fs,
            SwapFakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs),
            FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes),
            installer=swap_installer(),
        )

    def test_diagnosis_reports_zswap_and_headroom_and_skips_settings_only(self) -> None:
        svc = self._diag_service()
        out = io.StringIO()
        render_status(svc.status_report(include_config=False), out=out)
        text = out.getvalue()
        self.assertIn("== diagnosis", text)
        self.assertIn("zswap is disabled", text)
        self.assertIn("fix: cst swap apply", text)

        only_out = io.StringIO()
        render_status(svc.status_report(settings_only=True, include_config=False), out=only_out)
        self.assertNotIn("== diagnosis", only_out.getvalue())

    def test_diagnosis_reports_optimized_when_memory_knobs_healthy(self) -> None:
        with open(os.path.join(self.env.zswap_params_dir, "enabled"), "w", encoding="utf-8") as f:
            f.write("Y\n")
        with open(os.path.join(self.env.proc_dir, "sys", "vm", "min_free_kbytes"), "w", encoding="utf-8") as f:
            f.write("262144\n")
        out = io.StringIO()
        render_status(self._diag_service().status_report(include_config=False), out=out)
        text = out.getvalue()
        self.assertIn("== diagnosis", text)
        self.assertIn("[OPTIMIZED]", text)


class Phase1MemoryTuningTest(_SwapScratch):
    """Phase 1: min_free_kbytes, THP defrag, MGLRU min_ttl_ms, and uresourced MemoryMin."""

    def _populate_mm_nodes(self, *, defrag: str = "always defer [madvise] never", min_ttl: str = "0") -> None:
        thp = os.path.join(self.env.sys_dir, "kernel", "mm", "transparent_hugepage", "defrag")
        ttl = os.path.join(self.env.sys_dir, "kernel", "mm", "lru_gen", "min_ttl_ms")
        user_min = os.path.join(self.env.cgroup_dir, "user.slice", "memory.min")
        for p, val in ((thp, defrag), (ttl, min_ttl), (user_min, "262144000")):
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(val + "\n")

    def _service_with(
        self,
        *,
        unit_shown: Mapping[str, Mapping[str, str]] | None = None,
        mapper_present: bool = False,
        on_call: Callable[[str, str], object] | None = None,
        system: FakeSwapSystem | None = None,
    ) -> tuple[SwapApplicationService, SwapFakeSystemd]:
        fs = self._fs(mapper_present=mapper_present)
        systemd = SwapFakeSystemd(
            os.path.join(self.env.proc_dir, "swaps"),
            "/dev/dm-0",
            fs=fs,
            unit_shown=unit_shown,
            on_call=on_call,
        )
        swap_system = (
            FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes) if system is None else system
        )
        return SwapApplicationService(self.env, fs, systemd, swap_system, installer=swap_installer()), systemd

    def test_plan_apply_backup_restart_and_idempotency(self) -> None:
        self._populate_mm_nodes()
        ures_dest = os.path.join(self.env.etc_dir, "uresourced.conf")
        mm_dest = os.path.join(self.env.etc_dir, "tmpfiles.d", "cst-mm-responsiveness.conf")
        vendor_uresourced = "[Global]\nMaxMemoryMin=10%\n\n[ActiveUser]\nMemoryMin=250M\n"
        with open(ures_dest, "w", encoding="utf-8") as fh:
            fh.write(vendor_uresourced)

        user_min_path = os.path.join(self.env.cgroup_dir, "user.slice", "memory.min")

        def _simulate_uresourced(verb: str, unit: str) -> None:
            if verb == "restart" and unit == "uresourced.service":
                with open(user_min_path, "w", encoding="utf-8") as fh:
                    fh.write(f"{1024**3}\n")

        svc1, sd1 = self._service_with(
            unit_shown={"uresourced.service": {"ActiveState": "active"}},
            on_call=_simulate_uresourced,
        )
        plan_out = io.StringIO()
        render_plan(svc1.plan_report(), out=plan_out)
        plan_txt = plan_out.getvalue()
        self.assertIn("cst-mm-responsiveness.conf", plan_txt)
        self.assertIn("uresourced.conf", plan_txt)
        self.assertIn("THP defrag", plan_txt)
        self.assertIn("MGLRU min_ttl_ms", plan_txt)
        self.assertIn("user.slice memory.min", plan_txt)
        self.assertIn("desired 262144", plan_txt)
        with open(ures_dest, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), vendor_uresourced, "plan must leave /etc/uresourced.conf untouched")

        apply_out, apply_err = io.StringIO(), io.StringIO()
        rc = render_apply(svc1.apply_changes(), out=apply_out, err=apply_err)
        self.assertEqual(rc, 0, apply_err.getvalue())
        self.assertIn("== memory responsiveness", apply_out.getvalue())
        self.assertIn("set:       THP defrag = defer+madvise (was madvise)", apply_out.getvalue())
        self.assertIn("set:       MGLRU min_ttl_ms = 1000 (was 0)", apply_out.getvalue())
        self.assertIn("restarted: uresourced.service", apply_out.getvalue())
        self.assertIn(("restart", "uresourced.service"), sd1.calls)
        with open(ures_dest + ".pre-swap-config.bak", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), vendor_uresourced)
        with open(os.path.join(self.env.proc_dir, "sys", "vm", "min_free_kbytes"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "262144")

        # Second apply sees matching files and live values: zero restarts.
        svc2, sd2 = self._service_with(
            unit_shown={"uresourced.service": {"ActiveState": "active"}}, mapper_present=True
        )
        out2, err2 = io.StringIO(), io.StringIO()
        self.assertEqual(render_apply(svc2.apply_changes(), out=out2, err=err2), 0, err2.getvalue())
        self.assertNotIn("restart", [c[0] for c in sd2.calls])
        self.assertIn("unchanged: " + mm_dest, out2.getvalue())
        self.assertIn("unchanged: " + ures_dest, out2.getvalue())

        status_out = io.StringIO()
        render_status(svc2.status_report(include_config=True), out=status_out)
        self.assertIn("  ok      user.slice memory.min", status_out.getvalue())
        self.assertIn("  ok      THP defrag", status_out.getvalue())
        self.assertIn("  ok      MGLRU min_ttl_ms", status_out.getvalue())

    def test_thp_experiment_override_and_missing_kernel_knob(self) -> None:
        self._populate_mm_nodes(defrag="always [defer] defer+madvise madvise never", min_ttl="1000")
        os.remove(os.path.join(self.env.sys_dir, "kernel", "mm", "lru_gen", "min_ttl_ms"))
        with open(os.path.join(self.env.cpu_run_dir, "exp-thp-defer"), "w", encoding="utf-8") as fh:
            fh.write("madvise\n")
        svc, sd = self._service_with()
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(render_apply(svc.apply_changes(), out=out, err=err), 0, err.getvalue())
        self.assertIn("kept:      THP defrag = defer", out.getvalue())
        self.assertIn("skipped:   MGLRU min_ttl_ms", out.getvalue())
        self.assertNotIn(("restart", "uresourced.service"), sd.calls)
        st_out = io.StringIO()
        render_status(svc.status_report(include_config=False), out=st_out)
        self.assertIn("  exp     THP defrag", st_out.getvalue())
        self.assertIn("cst cpu experiment off thp-defer", st_out.getvalue())

    def test_both_experiments_on_get_a_note_each(self) -> None:
        # wsf-300 holds vm.watermark_scale_factor and thp-defer THP defrag at once: status names both.
        self._populate_mm_nodes(defrag="always [defer] defer+madvise madvise never", min_ttl="1000")
        with open(os.path.join(self.env.proc_dir, "sys", "vm", "watermark_scale_factor"), "w", encoding="utf-8") as fh:
            fh.write("300\n")
        for name, saved in (("wsf-300", "125\n"), ("thp-defer", "madvise\n")):
            with open(os.path.join(self.env.cpu_run_dir, f"exp-{name}"), "w", encoding="utf-8") as fh:
                fh.write(saved)
        svc, _ = self._service_with()
        st_out = io.StringIO()
        render_status(svc.status_report(include_config=False), out=st_out)
        notes = [line for line in st_out.getvalue().splitlines() if line.startswith("  exp:")]
        self.assertEqual(
            notes,
            [
                (
                    "  exp: the cpu experiment wsf-300 is on and sets vm.watermark_scale_factor; apply leaves it alone."
                    " Back to the desired value: `cst cpu experiment off wsf-300`, then apply"
                ),
                (
                    "  exp: the cpu experiment thp-defer is on and sets /sys/kernel/mm/transparent_hugepage/defrag;"
                    " apply leaves it alone. Back to the desired value: `cst cpu experiment off thp-defer`, then apply"
                ),
            ],
        )

    def test_apply_waits_for_uresourced_async_memory_min_update(self) -> None:
        self._populate_mm_nodes()
        with open(os.path.join(self.env.etc_dir, "uresourced.conf"), "w", encoding="utf-8") as fh:
            fh.write("[Global]\nMaxMemoryMin=10%\n\n[ActiveUser]\nMemoryMin=250M\n")

        user_min_path = os.path.join(self.env.cgroup_dir, "user.slice", "memory.min")
        sleep_calls = 0

        def _delayed_uresourced_update(_seconds: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls == 2:
                with open(user_min_path, "w", encoding="utf-8") as fh:
                    fh.write(f"{1024**3}\n")

        system = FakeSwapSystem(
            self.env.proc_dir,
            size_bytes=self.spec.expected_size_bytes,
            on_sleep=_delayed_uresourced_update,
        )
        svc, sd = self._service_with(
            unit_shown={"uresourced.service": {"ActiveState": "active"}},
            system=system,
        )
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(render_apply(svc.apply_changes(), out=out, err=err), 0, err.getvalue())
        self.assertIn(("restart", "uresourced.service"), sd.calls)
        self.assertEqual(len(system.sleeps), 2)

    def _uresourced_case(self, live_min: str, *, mem_total_kib: int | None = None) -> str:
        """The mm nodes, MemTotal and user.slice's live memory.min: the path of that file."""
        self._populate_mm_nodes(defrag="always defer [defer+madvise] madvise never", min_ttl="1000")
        if mem_total_kib is not None:
            with open(os.path.join(self.env.proc_dir, "meminfo"), "w", encoding="utf-8") as fh:
                fh.write(f"MemTotal:       {mem_total_kib} kB\nMemAvailable:   {mem_total_kib // 2} kB\n")
        user_min_path = os.path.join(self.env.cgroup_dir, "user.slice", "memory.min")
        with open(user_min_path, "w", encoding="utf-8") as fh:
            fh.write(live_min + "\n")
        return user_min_path

    def _apply_with_uresourced(
        self, on_call: Callable[[str, str], object] | None = None
    ) -> tuple[int, str, SwapFakeSystemd]:
        svc, sd = self._service_with(
            unit_shown={"uresourced.service": {"ActiveState": "active"}}, mapper_present=True, on_call=on_call
        )
        out, err = io.StringIO(), io.StringIO()
        rc = render_apply(svc.apply_changes(), out=out, err=err)
        return rc, out.getvalue() + err.getvalue(), sd

    def test_max_memory_min_forms(self) -> None:
        one_g = 2**30
        for text, total, want in (
            ("[Global]\nMaxMemoryMin=10%\n[ActiveUser]\nMemoryMin=1G\n", 64 * one_g, (one_g, False)),
            ("[Global]\nMaxMemoryMin=10%\n[ActiveUser]\nMemoryMin=1G\n", None, (one_g, False)),
            ("[Global]\nMaxMemoryMin=512M\n[ActiveUser]\nMemoryMin=1G\n", 64 * one_g, (512 * 2**20, False)),
            # A comment, or the key in another section, sets nothing.
            ("[Global]\n#MaxMemoryMin=1%\n[ActiveUser]\nMaxMemoryMin=1%\nMemoryMin=1G\n", 64 * one_g, (one_g, False)),
            ("[Global]\nMaxMemoryMin=1%\n", 64 * one_g, (None, False)),
            # ASCII digits only, as uresourced reads them: Arabic-Indic "2G" was 2 GiB, and "512M" a cap of 512 MiB.
            ("[ActiveUser]\nMemoryMin=\N{ARABIC-INDIC DIGIT TWO}G\n", 64 * one_g, (None, False)),
            (
                (
                    "[Global]\nMaxMemoryMin=\N{ARABIC-INDIC DIGIT FIVE}\N{ARABIC-INDIC DIGIT ONE}"
                    "\N{ARABIC-INDIC DIGIT TWO}M\n[ActiveUser]\nMemoryMin=1G\n"
                ),
                64 * one_g,
                (one_g, False),
            ),
        ):
            with self.subTest(text=text, total=total):
                self.assertEqual(effective_uresourced_memory_min(text, total), want)

    def test_uresourced_floor_is_capped_by_max_memory_min(self) -> None:
        # 8 GiB of RAM: [Global] MaxMemoryMin=10% caps MemoryMin=1G to 10% of MemTotal, in whole pages.
        capped = 8 * 2**30 * 10 // 100 // 4096 * 4096
        for live in (capped, capped - 4096):
            with self.subTest(live=live):
                self._uresourced_case(str(live), mem_total_kib=8 * 2**20)
                rc, out, _ = self._apply_with_uresourced()
                self.assertEqual(rc, 0, out)
                self.assertIn("  ok      user.slice memory.min", out)
        self._uresourced_case(str(2**30), mem_total_kib=8 * 2**20)
        rc, out, _ = self._apply_with_uresourced()
        self.assertNotEqual(rc, 0, out)
        self.assertRegex(out, r"DIFF +user\.slice memory\.min")

    def test_uresourced_floor_without_a_seat_session_is_inactive(self) -> None:
        # A machine with no session in a seat's foreground (a server, an ssh login): uresourced protects
        # nobody, user.slice's memory.min stays 0, and that is no difference for apply to fail on.
        self._uresourced_case("0")
        with open(os.path.join(self.env.seats_dir, "seat0"), "w", encoding="utf-8") as fh:
            fh.write("IS_SEAT0=1\nCAN_GRAPHICAL=1\n")
        rc, out, _ = self._apply_with_uresourced()
        self.assertEqual(rc, 0, out)
        self.assertIn("no active seat session", out)
        self.assertNotRegex(out, r"DIFF +user\.slice memory\.min")
        # With a session in the foreground, a live 0 is a difference.
        with open(os.path.join(self.env.seats_dir, "seat0"), "w", encoding="utf-8") as fh:
            fh.write("IS_SEAT0=1\nACTIVE=3\nACTIVE_UID=1000\n")
        rc, out, _ = self._apply_with_uresourced()
        self.assertNotEqual(rc, 0, out)
        self.assertRegex(out, r"DIFF +user\.slice memory\.min")

    def test_apply_restarts_uresourced_when_the_live_floor_drifted(self) -> None:
        user_min_path = self._uresourced_case(str(2**30))
        rc, out, _ = self._apply_with_uresourced()
        self.assertEqual(rc, 0, out)  # writes /etc/uresourced.conf

        def _uresourced_applies(verb: str, unit: str) -> None:
            if (verb, unit) == ("restart", "uresourced.service"):
                with open(user_min_path, "w", encoding="utf-8") as fh:
                    fh.write(f"{2**30}\n")

        # The file is current; the live floor is not (uresourced started before it): apply restarts it.
        with open(user_min_path, "w", encoding="utf-8") as fh:
            fh.write("262144000\n")
        rc, out, sd = self._apply_with_uresourced(_uresourced_applies)
        self.assertEqual(rc, 0, out)
        self.assertIn(("restart", "uresourced.service"), sd.calls)
        self.assertIn("unchanged:", out)
        self.assertIn("  ok      user.slice memory.min", out)
        # Current file and live floor: no restart.
        rc, out, sd = self._apply_with_uresourced(_uresourced_applies)
        self.assertEqual(rc, 0, out)
        self.assertNotIn(("restart", "uresourced.service"), sd.calls)

    def _seats(self, **seats: str) -> None:
        for name in os.listdir(self.env.seats_dir):
            os.remove(os.path.join(self.env.seats_dir, name))
        for name, text in seats.items():
            with open(os.path.join(self.env.seats_dir, name), "w", encoding="utf-8") as fh:
                fh.write(text)

    def test_a_greeter_in_front_is_no_active_user(self) -> None:
        # The login screen (GDM's greeter, a system uid) in seat0's foreground: uresourced protects nobody,
        # so a live 0 is inactive, not a difference apply restarts uresourced for.
        self._uresourced_case(str(2**30))
        rc, out, _ = self._apply_with_uresourced()
        self.assertEqual(rc, 0, out)  # writes /etc/uresourced.conf
        self._seats(seat0="IS_SEAT0=1\nACTIVE=c1\nACTIVE_UID=42\nSESSIONS=c1\nUIDS=42\n")
        self._uresourced_case("0")
        rc, out, sd = self._apply_with_uresourced()
        self.assertEqual(rc, 0, out)
        self.assertIn("no active seat session", out)
        self.assertNotRegex(out, r"DIFF +user\.slice memory\.min")
        self.assertNotIn(("restart", "uresourced.service"), sd.calls)

    def test_a_live_floor_of_other_than_ascii_digits_is_a_difference(self) -> None:
        # isdigit() takes these: int() refused "²" and 5000 digits (a traceback out of apply).
        for live in ("\N{SUPERSCRIPT TWO}", "1" * 5000):
            with self.subTest(live=live[:10]):
                self._uresourced_case(live)
                rc, out, _ = self._apply_with_uresourced()
                self.assertNotEqual(rc, 0, out)
                self.assertRegex(out, r"DIFF +user\.slice memory\.min")

    def test_a_seat_uid_of_other_than_ascii_digits_is_a_user(self) -> None:
        # ACTIVE_UID "٣" was read as uid 3, a greeter's: the seat counted nobody, and a live 0 was no
        # difference. Not a number, it counts as the seat's user, as a seat without ACTIVE_UID does.
        self._uresourced_case(str(2**30))
        rc, out, _ = self._apply_with_uresourced()
        self.assertEqual(rc, 0, out)  # writes /etc/uresourced.conf
        self._seats(seat0="IS_SEAT0=1\nACTIVE=c1\nACTIVE_UID=\N{ARABIC-INDIC DIGIT THREE}\nSESSIONS=c1\n")
        self._uresourced_case("0")
        rc, out, _ = self._apply_with_uresourced()
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("no active seat session", out)
        self.assertRegex(out, r"DIFF +user\.slice memory\.min")

    def test_the_floor_counts_each_active_user_once(self) -> None:
        # Two users in front of two seats: uresourced gives user.slice MemoryMin for each (2G), and that is
        # no drift; the same user in front of both seats counts once (1G).
        one_g = 2**30
        for seats, live in (
            ({"seat0": "ACTIVE=3\nACTIVE_UID=1000\n", "seat1": "ACTIVE=7\nACTIVE_UID=1001\n"}, 2 * one_g),
            ({"seat0": "ACTIVE=3\nACTIVE_UID=1000\n", "seat1": "ACTIVE=7\nACTIVE_UID=1000\n"}, one_g),
            # A seat file without ACTIVE_UID still counts its foreground user.
            ({"seat0": "ACTIVE=3\nACTIVE_UID=1000\n", "seat1": "ACTIVE=7\n"}, 2 * one_g),
        ):
            with self.subTest(seats=seats):
                self._uresourced_case(str(one_g))
                rc, out, _ = self._apply_with_uresourced()  # the file in place first
                self._seats(**seats)
                self._uresourced_case(str(live))
                rc, out, sd = self._apply_with_uresourced()
                self.assertEqual(rc, 0, out)
                self.assertIn("  ok      user.slice memory.min", out)
                self.assertNotIn(("restart", "uresourced.service"), sd.calls)

    def test_memtotal_is_read_as_swaps_other_meminfo_readers_read_it(self) -> None:
        # MemTotal through swap.domain.parse_meminfo, as the zswap summary reads it: a value without
        # "kB" is bytes. 8 GiB written so: MaxMemoryMin=10% caps the floor to 10% of 8 GiB.
        capped = 8 * 2**30 * 10 // 100 // 4096 * 4096
        self._uresourced_case(str(capped))
        with open(os.path.join(self.env.proc_dir, "meminfo"), "w", encoding="utf-8") as fh:
            fh.write(f"MemTotal:       {8 * 2**30}\nMemAvailable:   {4 * 2**30}\n")
        rc, out, _ = self._apply_with_uresourced()
        self.assertEqual(rc, 0, out)
        self.assertIn("  ok      user.slice memory.min", out)

    def test_max_memory_min_caps_the_total_of_the_active_users(self) -> None:
        text = "[Global]\nMaxMemoryMin=10%\n[ActiveUser]\nMemoryMin=1G\n"
        one_g = 2**30
        self.assertEqual(effective_uresourced_memory_min(text, 64 * one_g, active_users=2), (2 * one_g, False))
        self.assertEqual(effective_uresourced_memory_min(text, 64 * one_g, active_users=0), (one_g, False))
        cap = 16 * one_g * 10 // 100 // 4096 * 4096  # 1.6G: below 2G, above 1G
        self.assertEqual(effective_uresourced_memory_min(text, 16 * one_g, active_users=2), (cap, True))
        self.assertEqual(effective_uresourced_memory_min(text, 16 * one_g, active_users=1), (one_g, False))


if __name__ == "__main__":
    unittest.main()

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
from typing import NoReturn
import unittest
from unittest import mock

sys.dont_write_bytecode = True

from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.files import LocalFileSystemAdapter
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget
from cst_core.application.install import InstallService, Placement
from cst_core.domain.files import MarkedLineSpec
from cst_core.ports import CommandFailed, CommandResult, UnitName
from swap.application.apply import SwapApplicationService
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


def _installer() -> InstallService:
    """The installer's use case on real files (the tests' folders), with a record port: swap writes its
    files through it, and lists them in env.install_record when a test sets one."""
    return InstallService(LocalInstallTarget(), lambda _e: b"", record=JsonInstallRecord())


# The drop-in as CST installed it until 2026-11, as machine.slice.d/50-memory-low.conf.
# Its first line, split: the license gate wants that line on line 1 of this file only.
MAIN_DROPIN = (
    "# SPDX-"
    + "License-Identifier: MPL-2.0\n"
    + """\
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
            installer=_installer(),
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
                SettingCheckRow("ok", "zswap compressor", "zstd", "zstd"),
                SettingCheckRow("ok", "zswap max_pool_percent", "20", "20"),
                SettingCheckRow("MISSING", "zswap shrinker_enabled", "this kernel has no such parameter", "1"),
                SettingCheckRow("ok", "zswap enabled", "Y", "1"),
            ],
        )
        self.assertIn("MISSING zswap shrinker_enabled       this kernel has no such parameter   (desired 1)", text)

    def test_no_zswap_at_all_is_one_bad_row(self) -> None:
        rows, _ = self.zswap_rows({})
        self.assertEqual(rows, [SettingCheckRow("BAD", "zswap", "not in this kernel (no /sys/module/zswap)", "")])

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
    fs: _FakeTempFileSystem
    spec: SwapHardwareSpec
    svc: SwapApplicationService

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="swap-test-")
        self.fs = _FakeTempFileSystem({})
        self.spec = SwapHardwareSpec()
        swaps = os.path.join(self.tmp, "swaps")
        self.svc = SwapApplicationService(
            SwapEnvironment(src_dir=SWAP, etc_dir=self.tmp, spec=self.spec),
            self.fs,
            _FakeSystemd(swaps, "/dev/dm-0"),
            _FakeSwapSystem(self.tmp, size_bytes=self.spec.expected_size_bytes),
            installer=_installer(),
        )

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
        ok, _changed, _written = self.svc._apply_marked_line(p, self.line_spec(which), 0o644, events)
        self.assertTrue(ok, events)

    def file_state(self, src: str, dest: str) -> str:
        return self.svc._inspect_file(src, dest)

    def test_unreadable_files(self) -> None:
        p = self.path("fstab", "x\n")
        self.fs._unreadable.add(p)
        self.assertEqual(self.state(p), "unreadable")
        self.assertEqual(self.file_state(TMPFILES, p), "unreadable")
        events: list[D.ApplyEvent] = []
        self.assertEqual(self.svc._apply_marked_line(p, self.line_spec(), 0o644, events)[0], False)
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
        fail_result: CommandResult | None = None,
        on_call: Callable[[str, str], object] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
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

    def start(self, unit: str) -> CommandResult:
        if not self._call("start", unit):
            return self._fail_result
        if unit == "systemd-cryptsetup@swap0.service" and self._fs is not None:
            self._fs.add_block_link(self._mapper_link, self._mapper_real)
        if unit == "dev-mapper-swap0.swap":
            self._set_active(self._mapper_real, True)
        return CommandResult(0, "", "")

    def stop(self, unit: str) -> CommandResult:
        if not self._call("stop", unit):
            return self._fail_result
        if unit == "dev-mapper-swap0.swap":
            self._set_active(self._mapper_real, False)
        elif unit == "dev-zram0.swap":
            self._set_active("/dev/zram0", False)
        return CommandResult(0, "", "")

    def restart(self, unit: str) -> CommandResult:
        return CommandResult(0, "", "") if self._call("restart", unit) else self._fail_result

    def escape_path(self, path: str, *, suffix: str) -> UnitName:
        cleaned = path.lstrip("/").replace("/", "-")
        return UnitName(f"{cleaned}.{suffix}")


class _FakeSwapSystem:
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
    ) -> None:
        self._sysctl_result = CommandResult(0, "", "") if sysctl_result is None else sysctl_result
        self._swapon_result = CommandResult(0, swapon_text, "") if swapon_result is None else swapon_result
        self._proc_dir = proc_dir
        self._size_bytes = size_bytes
        self._part_type = part_type
        self._swapon_text = swapon_text
        self.modprobes: list[str] = []
        self.sysctl_calls: list[tuple[str, str | None]] = []
        self.zram_resets = 0

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
        **_options: object,
    ) -> CommandResult:
        del argv, check, timeout, input_text, env
        return CommandResult(0, "", "")


class _SwapScratch(unittest.TestCase):
    """A scratch /etc, /proc, /sys and cgroup tree for the application service."""

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


class SwapApplicationTest(_SwapScratch):
    """End-to-end application service and CLI tests inside temporary directories."""

    def test_apply_lists_its_files_in_the_install_record(self) -> None:
        record = os.path.join(self.tmp, "var/lib/cst/installed.json")
        env = dataclasses.replace(self.env, install_record=record)
        first = b""
        for n in (1, 2):
            fs = self._fs(mapper_present=n == 2)
            systemd = _FakeSystemd(os.path.join(env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
            system = _FakeSwapSystem(env.proc_dir, size_bytes=self.spec.expected_size_bytes)
            service = SwapApplicationService(env, fs, systemd, system, installer=_installer())
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
                "/etc/tmpfiles.d/zswap.conf",
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
        systemd = _FakeSystemd(os.path.join(env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = _FakeSwapSystem(env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(env, fs, systemd, system, installer=_installer())
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(run_command("apply", service, hint="", out=out, err=err), 1)
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
            kind="unit",
            owner="root",
            mode=0o644,
        )
        _installer().put(place, text.encode(), as_root=False)
        return old

    def _apply_once(self, env: SwapEnvironment, *, mapper_present: bool) -> tuple[int, str, str, _FakeSystemd]:
        fs = self._fs(mapper_present=mapper_present)
        systemd = _FakeSystemd(os.path.join(env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = _FakeSwapSystem(env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(env, fs, systemd, system, installer=_installer())
        out, err = io.StringIO(), io.StringIO()
        rc = render_apply(service.apply_changes(), out=out, err=err)
        return rc, out.getvalue(), err.getvalue(), systemd

    def _plan_text(self, env: SwapEnvironment) -> str:
        fs = self._fs()
        service = SwapApplicationService(
            env,
            fs,
            _FakeSystemd(os.path.join(env.proc_dir, "swaps"), "/dev/dm-0", fs=fs),
            _FakeSwapSystem(env.proc_dir, size_bytes=self.spec.expected_size_bytes),
            installer=_installer(),
        )
        out = io.StringIO()
        render_plan(service.plan_report(), out=out)
        return out.getvalue()

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

    def test_plan_apply_idempotent_and_status_cycle(self) -> None:
        # Legacy zram sysctl exists beforehand so apply should remove it.
        os.makedirs(os.path.join(self.env.etc_dir, "sysctl.d"), exist_ok=True)
        with open(self.env.old_sysctl_conf, "w", encoding="utf-8") as f:
            f.write("vm.swappiness = 60\n")
        fs = self._fs(mapper_present=False)
        systemd = _FakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(self.env, fs, systemd, system, installer=_installer())

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
        service2 = SwapApplicationService(self.env, fs2, systemd2, system2, installer=_installer())

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
            render_apply(
                SwapApplicationService(self.env, fs, systemd, bad_size_sys, installer=_installer()).apply_changes(),
                out=io.StringIO(),
                err=err,
            ),
            1,
        )
        self.assertIn("expected", err.getvalue())

        # Unexpected filesystem signature refuses.
        bad_fs_sys = _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes, part_type="ext4")
        err = io.StringIO()
        self.assertEqual(
            render_apply(
                SwapApplicationService(self.env, fs, systemd, bad_fs_sys, installer=_installer()).apply_changes(),
                out=io.StringIO(),
                err=err,
            ),
            1,
        )
        self.assertIn("ext4", err.getvalue())

        # Unmarked swap0 conflict in fstab refuses without writing crypttab/fstab.
        with open(self.env.fstab, "w", encoding="utf-8") as f:
            f.write("/dev/mapper/swap0 none swap defaults 0 0\n")
        good_sys = _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        err = io.StringIO()
        self.assertEqual(
            render_apply(
                SwapApplicationService(self.env, fs, systemd, good_sys, installer=_installer()).apply_changes(),
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
        systemd = _FakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs)
        system = _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes)
        service = SwapApplicationService(self.env, fs, systemd, system, installer=_installer())

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
            installer=_installer(),
        )
        with self.assertRaises(MissingDesiredFileError):
            service.plan_report()

    def test_execute_cli_and_bash_wrapper(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(execute_cli(["help"], env={"CST_PROG": "cst swap"}, out=out, err=err), 0)
        self.assertIn("Usage: cst swap <command> [args]", out.getvalue())
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


class _ZswapWriteFails(_FakeTempFileSystem):
    def write_text_direct(self, path: str, content: str) -> None:
        raise PermissionError(13, "Permission denied", path)


class SwapApplyOutputTest(_SwapScratch):
    """apply prints each step as it runs and stops at a failing command with its message and exit status,
    as swap-setup.sh did under `set -Eeuo pipefail`."""

    def _service(
        self,
        *,
        systemd: _FakeSystemd | None = None,
        system: _FakeSwapSystem | LinuxSwapSystemAdapter | None = None,
        fs: _FakeTempFileSystem | None = None,
    ) -> tuple[SwapApplicationService, _FakeSystemd]:
        fs = self._fs() if fs is None else fs
        sd = _FakeSystemd(os.path.join(self.env.proc_dir, "swaps"), "/dev/dm-0", fs=fs) if systemd is None else systemd
        sw = _FakeSwapSystem(self.env.proc_dir, size_bytes=self.spec.expected_size_bytes) if system is None else system
        return SwapApplicationService(self.env, fs, sd, sw, installer=_installer()), sd

    def _apply(self, service: SwapApplicationService) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        rc = run_command("apply", service, hint="", out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_each_step_is_printed_before_the_next_one_runs(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        seen: dict[str, str] = {}
        fs = self._fs()
        systemd = _FakeSystemd(
            os.path.join(self.env.proc_dir, "swaps"),
            "/dev/dm-0",
            fs=fs,
            on_call=lambda verb, unit: seen.setdefault(f"{verb} {unit}", out.getvalue()),
        )
        service, _ = self._service(systemd=systemd, fs=fs)
        self.assertEqual(run_command("apply", service, hint="", out=out, err=err), 0, err.getvalue())
        before_start = seen["start systemd-cryptsetup@swap0.service"]
        self.assertIn("== swap0: crypttab and fstab", before_start)
        self.assertIn("/dev/mapper/swap0: activating", before_start)
        self.assertIn("zram0: swapping off", seen["stop dev-zram0.swap"])
        self.assertEqual(out.getvalue().count("== zswap"), 1, "each line once")

    def test_verification_has_no_blank_line_before_it(self) -> None:
        rc, out, err = self._apply(self._service()[0])
        self.assertEqual(rc, 0, err)
        self.assertIn("\n== verification\n", out)
        self.assertNotIn("\n\n== verification", out)

    def test_a_failing_systemctl_start_shows_its_message_and_exit_status(self) -> None:
        job = "Job for systemd-cryptsetup@swap0.service failed because the control process exited with error code.\n"
        fs = self._fs()
        systemd = _FakeSystemd(
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
        systemd = _FakeSystemd(
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
        system = _FakeSwapSystem(
            self.env.proc_dir, size_bytes=self.spec.expected_size_bytes, sysctl_result=CommandResult(255, "", msg)
        )
        rc, out, err = self._apply(self._service(system=system)[0])
        self.assertEqual(rc, 255)
        self.assertEqual(err, msg)
        self.assertIn("== sysctl", out)
        self.assertIn("zram0: removed", out, "the lines of the steps done before are on screen")
        self.assertNotIn("== machine.slice", out)

    def test_a_failing_daemon_reload_shows_its_message_and_exit_status(self) -> None:
        class _ReloadFails(_FakeSystemd):
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
        systemd = _FakeSystemd(
            os.path.join(self.env.proc_dir, "swaps"),
            "/dev/dm-0",
            fs=fs,
            fail_unit="systemd-cryptsetup@swap0.service",
            fail_result=CommandResult(-signal.SIGTERM, "", ""),
        )
        self.assertEqual(self._apply(self._service(systemd=systemd, fs=fs)[0])[0], 128 + signal.SIGTERM)

        class _ReloadKilled(_FakeSystemd):
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
        system = _FakeSwapSystem(
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
        self.assertEqual(run_command("plan", self._service()[0], hint="", out=plan, err=io.StringIO()), 0)
        self.assertIn("fstab: /dev/mapper/swap0 line", plan.getvalue())
        rc, _out, err = self._apply(self._service()[0])
        self.assertEqual(rc, 0, err)
        with open(self.env.fstab, "rb") as f:
            self.assertTrue(f.read().startswith(b"# caf\xe9 comment\nUUID=x / btrfs defaults 0 0\n"))


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

    def test_the_desired_files_come_from_this_checkout(self) -> None:
        ctx = _FakeContext(container=True, root=False, etc_dir="/x/etc", run_dir="/x/run")
        self.assertEqual(
            swap_wiring.desired_paths("cst swap", ctx, fallback_root="/nowhere"),
            SwapEnvironment(
                src_dir=os.path.join(os.path.realpath(REPO), "swap"),
                etc_dir="/x/etc",
                cpu_run_dir="/x/run/cpu-setup",
                prog="cst swap",
                install_record="/x/var/lib/cst/installed.json",
            ),
        )


if __name__ == "__main__":
    unittest.main()

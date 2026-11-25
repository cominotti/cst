# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst perf` parsers, diagnosis thresholds, adapter, CLI, and presenter."""

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import override
import unittest
from unittest import mock

from cst_core.domain.values import GIB, MIB
from perf.adapters import LinuxPerfProbe, PerfPaths
from perf.application import build_perf_report, collect_cpu_from_reader, collect_memory_from_reader
from perf.cli import main as perf_cli_main
from perf.domain.axis import worst_perf_verdict
from perf.domain.evaluate import (
    evaluate_perf_cpu,
    evaluate_perf_gpu,
    evaluate_perf_io,
    evaluate_perf_memory,
    evaluate_perf_snapshot,
    perf_report_to_json,
    verdict_exit_code,
)
from perf.domain.models import (
    ALL_PERF_AXES,
    BtrfsCommitSample,
    BlockDeviceSample,
    CgroupMemorySlice,
    CpuIdleStateSample,
    CpuProbeSnapshot,
    EXIT_PERF_FINDINGS,
    EXIT_PERF_OK,
    EXIT_PERF_USAGE,
    GpuDeviceSample,
    GpuProbeSnapshot,
    IoProbeSnapshot,
    IrqLineSample,
    MemoryProbeSnapshot,
    PerfAxis,
    PerfAxisReport,
    PerfCliError,
    PerfPathKey,
    PerfProbeSnapshot,
    PerfRootKind,
    PerfVerdict,
)
from perf.domain.parsers import (
    assemble_io_probe,
    parse_amd_active_dpm_profile,
    parse_bracket_choice,
    parse_btrfs_commit_stats,
    parse_optional_cpuset,
    parse_optional_int,
    parse_perf_args,
    parse_preempt_mode,
    parse_proc_interrupts,
    parse_proc_kib_table,
    parse_proc_space_kv,
    parse_psi_avg60,
    parse_uevent_fields,
    parse_uptime_seconds,
)
from perf.presenter import render_perf_report
from perf.wiring import perf_paths_from_env

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
_CST_PERF = os.path.join(_REPO, "perf", "cst-perf")


class PerfParserTest(unittest.TestCase):
    def test_parse_perf_args_variants(self) -> None:
        default_opts = parse_perf_args([])
        self.assertEqual(default_opts.axes, ALL_PERF_AXES)
        self.assertFalse(default_opts.as_json)
        self.assertFalse(default_opts.compact)

        combo = parse_perf_args(["--axis", "io,cpu", "--axis=cpu,gpu", "--json", "--compact"])
        self.assertEqual(combo.axes, (PerfAxis.IO, PerfAxis.CPU, PerfAxis.GPU))
        self.assertTrue(combo.as_json)
        self.assertTrue(combo.compact)

        self.assertTrue(parse_perf_args(["--help"]).help_mode)
        self.assertTrue(parse_perf_args(["-h"]).help_mode)

        with self.assertRaises(PerfCliError):
            parse_perf_args(["--axis"])
        with self.assertRaises(PerfCliError):
            parse_perf_args(["--axis", "network"])
        # ASCII blanks around an axis are dropped; a no-break space is part of the word (str.strip()
        # used to drop it).
        self.assertEqual(parse_perf_args(["--axis", "cpu, memory\t"]).axes, (PerfAxis.CPU, PerfAxis.MEMORY))
        for spec in ("cpu,\N{NO-BREAK SPACE}memory", "cpu\N{IDEOGRAPHIC SPACE}"):
            with self.subTest(spec=spec), self.assertRaisesRegex(PerfCliError, "^unknown axis"):
                parse_perf_args(["--axis", spec])
        with self.assertRaises(PerfCliError):
            parse_perf_args(["--bogus"])

    def test_proc_and_sys_parsers(self) -> None:
        self.assertEqual(parse_proc_kib_table("MemTotal:   1024 kB\nFoo: bar\nBad: ² kB\n")["MemTotal"], 1024)
        self.assertNotIn("Bad", parse_proc_kib_table("Bad: ² kB\n"))
        self.assertEqual(
            parse_proc_space_kv("compact_stall 42\nneg -5\nbad --1\nuni ²\nbad\n"), {"compact_stall": 42, "neg": -5}
        )
        self.assertIsNone(parse_optional_int("--1"))
        self.assertIsNone(parse_optional_int("²"))
        self.assertIsNone(parse_optional_int("-²"))
        self.assertEqual(parse_optional_int("-42"), -42)
        from perf.domain.models import PerfPathKey

        zero_commit = assemble_io_probe(
            {PerfPathKey.PROC_MOUNTS: "/dev/dm-0 /var btrfs rw,commit=0,flushoncommit 0 0\n"}
        )
        self.assertEqual(zero_commit.root_btrfs_commit_s, 30)
        self.assertFalse(zero_commit.root_btrfs_commit_explicit)
        self.assertEqual(parse_bracket_choice("always defer [madvise] never\n"), "madvise")
        self.assertEqual(parse_bracket_choice("none\n"), "none")
        self.assertIsNone(parse_bracket_choice("   "))
        self.assertAlmostEqual(parse_uptime_seconds("7200.5 14000.0\n") or 0.0, 7200.5)
        self.assertIsNone(parse_uptime_seconds("bad"))
        self.assertEqual(parse_preempt_mode("none voluntary full [lazy]\n", None), "lazy")
        self.assertEqual(parse_preempt_mode(None, "quiet preempt=full root=/dev/null"), "full")
        self.assertEqual(
            parse_psi_avg60("some avg10=0.00 avg60=1.25 avg300=0.10 total=1\nfull avg10=0.00 avg60=0.05 total=2\n"),
            (1.25, 0.05),
        )
        self.assertEqual(
            parse_amd_active_dpm_profile("PROFILE_INDEX(NAME)\n 0 BOOTUP_DEFAULT*:\n 1 3D_FULL_SCREEN:\n"),
            "BOOTUP_DEFAULT",
        )
        self.assertEqual(
            parse_btrfs_commit_stats(
                "uuid-1", "commits 100\ncur_commit_ms 0\nlast_commit_ms 12\nmax_commit_ms 250\ntotal_commit_ms 1500\n"
            ),
            BtrfsCommitSample("uuid-1", 100, 12, 250, 1500),
        )
        self.assertEqual(parse_uevent_fields("DRIVER=amdgpu\nPCI_SLOT_NAME=0000:84:00.0\n"), ("amdgpu", "0000:84:00.0"))

    def test_parse_proc_interrupts(self) -> None:
        raw = (
            "           CPU0       CPU1       CPU2\n"
            "  NMI:        1          0          0   Non-maskable interrupts\n"
            "  246:      100          0    1200000   IR-PCI-MSIX-0000:84:00.0  amdgpu\n"
        )
        lines = parse_proc_interrupts(raw)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].irq_id, "246")
        self.assertEqual(lines[0].total_count, 1200100)
        self.assertEqual(lines[0].counts_by_cpu, {0: 100, 2: 1200000})

    def test_interrupts_take_ascii_cpu_columns_and_counts_within_int_s_limit(self) -> None:
        # A CPU column is "CPU" and ASCII digits: "CPU²" (int() refuses it) and "CPU٣" (int() reads 3) are none.
        line = "  246:      100          7   IR-PCI-MSIX  amdgpu\n"
        for second in ("CPU\N{SUPERSCRIPT TWO}", "CPU\N{ARABIC-INDIC DIGIT THREE}", "CPU" + "9" * 5000):
            with self.subTest(column=second[:5]):
                samples = parse_proc_interrupts(f"   CPU0  {second}\n" + line)
                self.assertEqual([s.counts_by_cpu for s in samples], [{0: 100}])
        # A count of more digits than int() reads drops that line, as a count that is no number does.
        samples = parse_proc_interrupts("   CPU0  CPU1\n" + f"  7:  {'9' * 5000}  1  x\n" + line)
        self.assertEqual([(s.irq_id, s.counts_by_cpu) for s in samples], [("246", {0: 100, 1: 7})])

    def test_an_interrupt_line_is_numbered_by_ascii_digits_only(self) -> None:
        # isdigit() took any Unicode digit: a superscript two or an Arabic-Indic three was a numbered IRQ, and
        # cst perf built /proc/irq paths from it.
        line = "  246:      100          7   IR-PCI-MSIX  amdgpu\n"
        for irq in ("\N{SUPERSCRIPT TWO}", "\N{ARABIC-INDIC DIGIT THREE}", "+5", " "):
            with self.subTest(irq=irq):
                samples = parse_proc_interrupts(f"   CPU0  CPU1\n {irq}:   1   2   IO-APIC x\n" + line)
                self.assertEqual([s.irq_id for s in samples], ["246"])

    def test_proc_numbers_past_int_s_digit_limit_are_no_number(self) -> None:
        many = "9" * 5000
        self.assertEqual(parse_proc_kib_table(f"MemTotal: 12 kB\nBig: {many} kB\n"), {"MemTotal": 12})
        self.assertEqual(parse_proc_space_kv(f"a 12\nb -3\nbig {many}\nneg -{many}\n"), {"a": 12, "b": -3})
        for bad in (many, "-" + many, "\N{ARABIC-INDIC DIGIT THREE}"):
            with self.subTest(value=bad[:6]):
                self.assertIsNone(parse_optional_int(bad))
        self.assertEqual([parse_optional_int("12"), parse_optional_int(" -3\n")], [12, -3])
        from perf.domain.models import PerfPathKey

        def commit_of(opts: str) -> tuple[int | None, bool]:
            probe = assemble_io_probe({PerfPathKey.PROC_MOUNTS: f"/dev/dm-0 / btrfs rw,{opts} 0 0\n"})
            return probe.root_btrfs_commit_s, probe.root_btrfs_commit_explicit

        self.assertEqual(commit_of("commit=120"), (120, True))
        for bad in (many, "\N{ARABIC-INDIC DIGIT THREE}"):
            with self.subTest(commit=bad[:6]):
                self.assertEqual(commit_of("commit=" + bad), (30, False))


class PerfEvaluatorTest(unittest.TestCase):
    def test_empty_snapshot_returns_skip_and_exit_zero(self) -> None:
        report = evaluate_perf_snapshot(PerfProbeSnapshot())
        self.assertEqual(report.verdict, PerfVerdict.SKIP)
        self.assertEqual(verdict_exit_code(report.verdict), EXIT_PERF_OK)
        self.assertEqual(worst_perf_verdict(()), PerfVerdict.SKIP)

    def test_memory_optimized_vs_findings(self) -> None:
        healthy = MemoryProbeSnapshot(
            uptime_seconds=7200.0,
            mem_total_bytes=64 * GIB,
            mem_available_bytes=40 * GIB,
            min_free_bytes=256 * MIB,
            watermark_scale_factor=125,
            allocstall_count=0,
            compact_stall_count=0,
            thp_enabled="madvise",
            thp_defrag="defer+madvise",
            lru_gen_enabled="0x0007",
            lru_gen_min_ttl_ms=1000,
            zswap_enabled=True,
            zswap_max_pool_percent=20,
            zswap_compressor="lz4",
            psi_some_avg60=0.0,
            psi_full_avg60=0.0,
            session_slices=(
                CgroupMemorySlice(
                    name="user-1000.slice/session.slice",
                    current_bytes=600 * MIB,
                    min_bytes=1024 * MIB,
                    low_bytes=0,
                    parent_min_bytes=1024 * MIB,
                ),
            ),
        )
        ok_rep = evaluate_perf_memory(healthy)
        self.assertEqual(ok_rep.verdict, PerfVerdict.OPTIMIZED)
        self.assertEqual(ok_rep.findings, ())

        sick = MemoryProbeSnapshot(
            uptime_seconds=3600.0,
            mem_total_bytes=64 * GIB,
            mem_available_bytes=8 * GIB,
            min_free_bytes=64 * MIB,
            allocstall_count=60,
            compact_stall_count=120,
            thp_defrag="madvise",
            lru_gen_enabled="0x0007",
            lru_gen_min_ttl_ms=0,
            zswap_enabled=False,
            psi_some_avg60=6.5,
            psi_full_avg60=1.2,
            session_slices=(
                CgroupMemorySlice(
                    name="user-1000.slice/session.slice",
                    current_bytes=700 * MIB,
                    min_bytes=1024 * MIB,
                    low_bytes=0,
                    parent_min_bytes=250 * MIB,
                ),
            ),
        )
        sick_rep = evaluate_perf_memory(sick)
        self.assertEqual(sick_rep.verdict, PerfVerdict.BOTTLENECK)
        ids = {f.finding_id for f in sick_rep.findings}
        self.assertEqual(
            ids,
            {
                "memory-min-free",
                "memory-allocstall",
                "memory-compact-stall",
                "memory-thp-defrag",
                "memory-mglru-min-ttl",
                "memory-zswap-disabled",
                "memory-psi-high",
                "memory-session-floor",
            },
        )
        zswap_f = next(f for f in sick_rep.findings if f.finding_id == "memory-zswap-disabled")
        self.assertEqual(zswap_f.fix_command, "cst swap apply")
        for fid in ("memory-min-free", "memory-thp-defrag", "memory-mglru-min-ttl", "memory-session-floor"):
            match_f = next(f for f in sick_rep.findings if f.finding_id == fid)
            self.assertEqual(match_f.fix_command, "cst swap apply", fid)

        # GDM greeter (uid < 1000) and inactive secondary human sessions do not trigger false positives.
        multi_user = MemoryProbeSnapshot(
            session_slices=(
                CgroupMemorySlice("user-42.slice/session.slice", 120 * MIB, 0, 0, parent_min_bytes=0, uid=42),
                CgroupMemorySlice(
                    "user-1000.slice/session.slice", 600 * MIB, 1024 * MIB, 0, parent_min_bytes=1024 * MIB, uid=1000
                ),
                CgroupMemorySlice("user-1001.slice/session.slice", 40 * MIB, 0, 0, parent_min_bytes=0, uid=1001),
            ),
        )
        self.assertEqual(evaluate_perf_memory(multi_user).verdict, PerfVerdict.OPTIMIZED)
        gdm_only = MemoryProbeSnapshot(
            session_slices=(
                CgroupMemorySlice("user-42.slice/session.slice", 120 * MIB, 0, 0, parent_min_bytes=0, uid=42),
            )
        )
        self.assertEqual(evaluate_perf_memory(gdm_only).verdict, PerfVerdict.OPTIMIZED)
        unprotected = MemoryProbeSnapshot(
            session_slices=(
                CgroupMemorySlice("user-42.slice/session.slice", 120 * MIB, 0, 0, parent_min_bytes=0, uid=42),
                CgroupMemorySlice("user-1000.slice/session.slice", 500 * MIB, 0, 0, parent_min_bytes=0, uid=1000),
            ),
        )
        unprot_rep = evaluate_perf_memory(unprotected)
        self.assertEqual([f.finding_id for f in unprot_rep.findings], ["memory-session-floor-unset"])
        self.assertIn("user-1000.slice", unprot_rep.findings[0].summary)

    def test_deep_cstates_warn_only_where_cpu_power_caps_them_by_default(self) -> None:
        # cpu-power leaves the C-states on by default with any cpuidle driver but intel_idle (AMD's
        # acpi_idle): a deep state there is not a finding `cst cpu install` could clear.
        deep = (CpuIdleStateSample("C1", 2, disabled=False), CpuIdleStateSample("C3", 350, disabled=False))
        for driver, warned in (("intel_idle", True), (None, True), ("acpi_idle", False), ("haltpoll", False)):
            with self.subTest(driver=driver):
                rep = evaluate_perf_cpu(CpuProbeSnapshot(cpuidle_driver=driver, idle_states=deep))
                ids = {f.finding_id for f in rep.findings}
                self.assertEqual("cpu-deep-cstates" in ids, warned, ids)
                self.assertIn(f"{driver or 'cpuidle'}: 350 µs", [m.value for m in rep.metrics])

    def test_deep_cstates_follow_the_cap_cpu_power_applies(self) -> None:
        # cpu-power's cap: CPU_CSTATE_MAX_LATENCY_US when cpu-setup.conf sets it (off or 0: none), else
        # 200 us with intel_idle (or an unknown driver) and none with any other. A state at or above the
        # cap that is still enabled is the finding; one cpu-power leaves on by design is not.
        c3, c6 = CpuIdleStateSample("C3", 350, disabled=False), CpuIdleStateSample("C6", 1048, disabled=False)
        c1 = CpuIdleStateSample("C1", 2, disabled=False)
        cases = (
            ("acpi_idle", "200", (c1, c3), "C3 enabled (350 µs >= 200 µs"),
            ("intel_idle", "off", (c1, c3, c6), None),
            ("intel_idle", "0", (c1, c3, c6), None),
            ("intel_idle", "500", (c1, c3), None),
            ("intel_idle", "500", (c1, c3, c6), "C6 enabled (1048 µs >= 500 µs"),
            ("intel_idle", "", (c1, c3), "C3 enabled (350 µs >= 200 µs"),
            ("acpi_idle", "", (c1, c3, c6), None),
            ("intel_idle", "junk", (c1, c3), "C3 enabled (350 µs >= 200 µs"),
            (None, " 300 ", (c1, c3), "C3 enabled (350 µs >= 300 µs"),
            # A setting of a no-break space is set (bash keeps it): cpu-power's 200 us, also with
            # acpi_idle (str.strip() made it "not set": no cap there, no finding).
            ("acpi_idle", "\N{NO-BREAK SPACE}", (c1, c3), "C3 enabled (350 µs >= 200 µs"),
        )
        for driver, setting, states, summary in cases:
            with self.subTest(driver=driver, setting=setting):
                cpu = CpuProbeSnapshot(cpuidle_driver=driver, idle_states=states, cstate_setting=setting)
                found = [f for f in evaluate_perf_cpu(cpu).findings if f.finding_id == "cpu-deep-cstates"]
                if summary is None:
                    self.assertEqual(found, [])
                else:
                    self.assertEqual(len(found), 1, found)
                    self.assertIn(summary, found[0].summary)
                    self.assertIn("the cap cpu-power applies)", found[0].summary)
                    self.assertEqual(found[0].fix_command, "cst cpu install")

    def test_cpu_evaluation(self) -> None:
        ok_cpu = CpuProbeSnapshot(
            cpuidle_driver="intel_idle",
            idle_states=(CpuIdleStateSample("C1", 2, disabled=False), CpuIdleStateSample("C3", 1048, disabled=True)),
            preempt_mode="lazy",
            ntsync_present=True,
            irqbalance_active=True,
            irqbalance_banned_cpus=parse_optional_cpuset("12-23"),
            build_cpus=parse_optional_cpuset("12-23"),
            irq_lines=(IrqLineSample("246", "IR-PCI-MSIX amdgpu", {1: 500_000}),),
        )
        self.assertEqual(evaluate_perf_cpu(ok_cpu).verdict, PerfVerdict.OPTIMIZED)

        bad_cpu = CpuProbeSnapshot(
            cpuidle_driver="intel_idle",
            idle_states=(CpuIdleStateSample("C3_ACPI", 1048, disabled=False),),
            preempt_mode="voluntary",
            ntsync_present=False,
            irqbalance_active=False,
            build_cpus=parse_optional_cpuset("12-23"),
            irq_lines=(IrqLineSample("246", "IR-PCI-MSIX amdgpu", {20: 900_000, 1: 1000}),),
        )
        bad_rep = evaluate_perf_cpu(bad_cpu)
        self.assertEqual(bad_rep.verdict, PerfVerdict.ATTENTION)
        self.assertEqual(
            {f.finding_id for f in bad_rep.findings},
            {
                "cpu-deep-cstates",
                "cpu-preempt-mode",
                "cpu-ntsync-absent",
                "cpu-irqbalance-inactive",
                "cpu-irq-build-overlap",
            },
        )
        cstate_f = next(f for f in bad_rep.findings if f.finding_id == "cpu-deep-cstates")
        self.assertEqual(cstate_f.fix_command, "cst cpu install")
        ntsync_f = next(f for f in bad_rep.findings if f.finding_id == "cpu-ntsync-absent")
        self.assertEqual(ntsync_f.fix_command, "cst cpu install")
        irq_f = next(f for f in bad_rep.findings if f.finding_id == "cpu-irqbalance-inactive")
        self.assertEqual(irq_f.fix_command, "cst cpu irq apply")
        ov_f = next(f for f in bad_rep.findings if f.finding_id == "cpu-irq-build-overlap")
        self.assertEqual(ov_f.fix_command, "cst cpu irq apply")

        # Active irqbalance without BUILD_CPUS banned -> cpu-irqbalance-ban-missing
        missing_ban = CpuProbeSnapshot(
            irqbalance_active=True,
            irqbalance_banned_cpus=None,
            build_cpus=parse_optional_cpuset("12-23"),
            irq_lines=(
                # Managed per-CPU blk-mq queues (q1+) must be ignored; q0 + amdgpu checked
                IrqLineSample("130", "IR-PCI-MSIX nvme0q13", {20: 900_000}),
                # Historical counter on CPU 20, but live effective_affinity moved to P-core 2 -> healthy
                IrqLineSample(
                    "246",
                    "IR-PCI-MSIX amdgpu",
                    {20: 900_000, 2: 100},
                    effective_cpus=parse_optional_cpuset("2"),
                ),
            ),
        )
        miss_rep = evaluate_perf_cpu(missing_ban)
        self.assertEqual([f.finding_id for f in miss_rep.findings], ["cpu-irqbalance-ban-missing"])
        self.assertEqual(miss_rep.findings[0].fix_command, "cst cpu irq apply")

        # Partial BUILD_CPUS ban (8-23 configured, only 12-23 banned) must still warn; 2-CPU host expects no ban.
        partial_ban = CpuProbeSnapshot(
            irqbalance_active=True,
            irqbalance_banned_cpus=parse_optional_cpuset("12-23"),
            build_cpus=parse_optional_cpuset("8-23"),
            online_cpus=parse_optional_cpuset("0-23"),
        )
        self.assertEqual(
            [f.finding_id for f in evaluate_perf_cpu(partial_ban).findings], ["cpu-irqbalance-ban-missing"]
        )
        tiny_host = CpuProbeSnapshot(
            irqbalance_active=True,
            irqbalance_banned_cpus=None,
            build_cpus=parse_optional_cpuset("1"),
            online_cpus=parse_optional_cpuset("0-1"),
        )
        self.assertEqual(evaluate_perf_cpu(tiny_host).verdict, PerfVerdict.OPTIMIZED)

        # Live effective_affinity hitting BUILD_CPUS and nvme0q0 admin queue
        live_hit = CpuProbeSnapshot(
            irqbalance_active=True,
            irqbalance_banned_cpus=parse_optional_cpuset("12-23"),
            build_cpus=parse_optional_cpuset("12-23"),
            irq_lines=(
                IrqLineSample("120", "IR-PCI-MSIX nvme0q0", {1: 500_000}, effective_cpus=parse_optional_cpuset("18")),
            ),
        )
        live_rep = evaluate_perf_cpu(live_hit)
        self.assertEqual([f.finding_id for f in live_rep.findings], ["cpu-irq-build-overlap"])
        self.assertIn("nvme0q0", live_rep.findings[0].summary)

    def test_io_and_gpu_evaluation(self) -> None:
        bad_io = IoProbeSnapshot(
            block_devices=(BlockDeviceSample("nvme0n1", "none", rotational=False),),
            nvme_ps_max_latency_us=100_000,
            dirty_background_bytes=0,
            dirty_bytes=0,
            iocost_qos_active=False,
            nonzero_slice_io_weights=(("builds.slice", 20),),
            btrfs_commits=(BtrfsCommitSample("afed93e2-1111", 100, 30, 836, 3500),),
            btrfs_scrub_limit=None,
            btrfs_conf_present=True,
        )
        io_rep = evaluate_perf_io(bad_io)
        self.assertEqual(io_rep.verdict, PerfVerdict.ATTENTION)
        self.assertEqual(
            {f.finding_id for f in io_rep.findings},
            {
                "io-weight-no-iocost",
                "io-nvme-apst",
                "io-dirty-bytes-unbounded",
                "io-btrfs-scrub-limit",
                "io-btrfs-commit-latency",
            },
        )
        nvme_f = next(f for f in io_rep.findings if f.finding_id == "io-nvme-apst")
        self.assertEqual(nvme_f.fix_command, "cst btrfs install")
        dirty_f = next(f for f in io_rep.findings if f.finding_id == "io-dirty-bytes-unbounded")
        self.assertEqual(dirty_f.fix_command, "cst btrfs install")
        scrub_f = next(f for f in io_rep.findings if f.finding_id == "io-btrfs-scrub-limit")
        self.assertEqual(scrub_f.fix_command, "cst btrfs scrub-limit set 500M")
        iocost_f = next(f for f in io_rep.findings if f.finding_id == "io-weight-no-iocost")
        self.assertEqual(iocost_f.fix_command, "cst btrfs iocost apply")

        # Short 30s commit with flushoncommit + expire < commit flagged; coherent 60s + 60s expire is clean.
        short_commit = IoProbeSnapshot(
            dirty_background_bytes=512 * MIB,
            dirty_bytes=2 * GIB,
            dirty_writeback_cs=500,
            dirty_expire_cs=3000,
            root_btrfs_commit_s=30,
            root_btrfs_flushoncommit=True,
        )
        short_rep = evaluate_perf_io(short_commit)
        self.assertIn("io-btrfs-commit-short", [f.finding_id for f in short_rep.findings])

        mismatch_commit = IoProbeSnapshot(
            dirty_background_bytes=512 * MIB,
            dirty_bytes=2 * GIB,
            dirty_writeback_cs=1500,
            dirty_expire_cs=3000,
            root_btrfs_commit_s=60,
            root_btrfs_commit_explicit=True,
            root_btrfs_flushoncommit=True,
        )
        mismatch_rep = evaluate_perf_io(mismatch_commit)
        self.assertEqual([f.finding_id for f in mismatch_rep.findings], ["io-dirty-expire-mismatch"])

        aligned_commit = IoProbeSnapshot(
            dirty_background_bytes=512 * MIB,
            dirty_bytes=2 * GIB,
            dirty_writeback_cs=1500,
            dirty_expire_cs=6000,
            root_btrfs_commit_s=60,
            root_btrfs_commit_explicit=True,
            root_btrfs_flushoncommit=True,
        )
        aligned_rep = evaluate_perf_io(aligned_commit)
        self.assertEqual(aligned_rep.verdict, PerfVerdict.OPTIMIZED)
        self.assertTrue(any(m.label == "dirty writeback" and "exp=60s" in m.value for m in aligned_rep.metrics))

        gpu_rep = evaluate_perf_gpu(
            GpuProbeSnapshot(
                gpus=(
                    GpuDeviceSample("card1", "xe", "0000:04:00.0", (), "auto", "suspended"),
                    GpuDeviceSample(
                        "card3", "amdgpu", "0000:84:00.0", ("DP-7",), "auto", "active", "auto", "BOOTUP_DEFAULT"
                    ),
                ),
                game_profile_active=True,
            )
        )
        self.assertEqual(gpu_rep.verdict, PerfVerdict.ATTENTION)
        self.assertEqual(
            {f.finding_id for f in gpu_rep.findings},
            {"gpu-runtime-pm-card3", "gpu-game-profile-card3"},
        )
        pm_f = next(f for f in gpu_rep.findings if f.finding_id == "gpu-runtime-pm-card3")
        self.assertEqual(pm_f.fix_command, "cst cpu install")
        prof_f = next(f for f in gpu_rep.findings if f.finding_id == "gpu-game-profile-card3")
        self.assertEqual(prof_f.fix_command, "sudo systemctl restart cpu-game.service")

        # GPU_PM_MODE=game outside game mode suppresses gpu-runtime-pm; manual game mode emits game off && game on.
        game_mode_idle = evaluate_perf_gpu(
            GpuProbeSnapshot(
                gpus=(GpuDeviceSample("card3", "amdgpu", "0000:84:00.0", ("DP-7",), "auto", "active"),),
                game_profile_active=False,
                gpu_pm_mode="game",
            )
        )
        self.assertEqual(game_mode_idle.verdict, PerfVerdict.OPTIMIZED)
        manual_game = evaluate_perf_gpu(
            GpuProbeSnapshot(
                gpus=(
                    GpuDeviceSample(
                        "card3", "amdgpu", "0000:84:00.0", ("DP-7",), "on", "active", "auto", "BOOTUP_DEFAULT"
                    ),
                ),
                game_profile_active=True,
                gpu_pm_mode="always",
                game_profile_manual=True,
            )
        )
        self.assertEqual([f.finding_id for f in manual_game.findings], ["gpu-game-profile-card3"])
        self.assertEqual(manual_game.findings[0].fix_command, "cst cpu game off && cst cpu game on")

    def test_each_rule_writes_its_metric_texts_in_order(self) -> None:
        # Pins the metric texts and findings of the rules that are split into helpers: moderate memory PSI
        # (some only), zswap off with full PSI high, a GPU without power/control, the io queues with iocost
        # configured, the dirty and commit texts with and without a cadence, irqbalance without a ban-list.
        def axis(rep: PerfAxisReport) -> tuple[list[tuple[str, str]], list[tuple[str, str, str, str]]]:
            return (
                [(m.label, m.value) for m in rep.metrics],
                [(f.finding_id, f.verdict.value, f.summary, f.fix_command) for f in rep.findings],
            )

        elevated = evaluate_perf_memory(
            MemoryProbeSnapshot(zswap_enabled=True, zswap_compressor="zstd", psi_some_avg60=2.5)
        )
        self.assertEqual(
            axis(elevated),
            (
                [("zswap", "enabled, zstd"), ("PSI memory avg60", "some=2.50% full=?")],
                [("memory-psi-elevated", "ATTENTION", "Moderate memory pressure PSI avg60 (some=2.50%)", "")],
            ),
        )
        zswap_off = evaluate_perf_memory(MemoryProbeSnapshot(zswap_enabled=False, psi_full_avg60=12.0))
        self.assertEqual(
            axis(zswap_off),
            (
                [("zswap", "disabled"), ("PSI memory avg60", "some=? full=12.00%")],
                [
                    ("memory-zswap-disabled", "ATTENTION", "zswap is disabled", "cst swap apply"),
                    ("memory-psi-high", "BOTTLENECK", "Elevated memory PSI avg60 (some=?, full=12.00%)", ""),
                ],
            ),
        )
        gpu = evaluate_perf_gpu(
            GpuProbeSnapshot(
                gpus=(
                    GpuDeviceSample("card0", None, None, ()),
                    GpuDeviceSample(
                        "card1", "amdgpu", None, ("DP-1", "HDMI-A-1"), "on", None, "auto", "3D_FULL_SCREEN"
                    ),
                )
            )
        )
        self.assertEqual(
            axis(gpu),
            (
                [
                    ("card0", "?, outputs=none"),
                    ("card1", "amdgpu, outputs=DP-1,HDMI-A-1, pm=on(?), dpm=auto, profile=3D_FULL_SCREEN"),
                ],
                [],
            ),
        )
        io_ok = evaluate_perf_io(
            IoProbeSnapshot(
                block_devices=(
                    BlockDeviceSample("sda", None, rotational=True),
                    BlockDeviceSample("nvme0n1", "none", rotational=False),
                ),
                iocost_qos_active=True,
                iocost_enabled_devices=2,
                nonzero_slice_io_weights=(("a.slice", 100), ("b.slice", 50)),
                nvme_ps_max_latency_us=5000,
                dirty_background_bytes=512 * MIB,
                dirty_bytes=0,
                root_btrfs_commit_s=30,
                root_btrfs_flushoncommit=False,
            )
        )
        self.assertEqual(
            axis(io_ok),
            (
                [
                    ("block schedulers", "nvme0n1=none"),
                    ("blk-iocost / io.weight", "qos=configured (2 device(s)); weights=a.slice=100, b.slice=50"),
                    ("nvme APST max latency", "5000 µs"),
                    ("dirty writeback", "bg=512.0 MiB, max=0 (ratio)"),
                    ("root btrfs commit", "30s (default)"),
                ],
                [],
            ),
        )
        io_warn = evaluate_perf_io(
            IoProbeSnapshot(
                nonzero_slice_io_weights=(("a.slice", 20),),
                dirty_background_bytes=0,
                dirty_bytes=2 * GIB,
                dirty_writeback_cs=500,
                dirty_expire_cs=1000,
                root_btrfs_commit_s=30,
                root_btrfs_flushoncommit=True,
            )
        )
        self.assertEqual(
            axis(io_warn),
            (
                [
                    ("blk-iocost / io.weight", "qos=empty; weights=a.slice=20"),
                    ("dirty writeback", "bg=0 (ratio), max=2.0 GiB, wb=5s, exp=10s"),
                    ("root btrfs commit", "30s (default), flushoncommit"),
                ],
                [
                    (
                        "io-weight-no-iocost",
                        "ATTENTION",
                        "Cgroup io.weight is configured without blk-iocost QoS",
                        "cst btrfs iocost apply",
                    ),
                    (
                        "io-btrfs-commit-short",
                        "ATTENTION",
                        "Root btrfs commit interval is 30s with flushoncommit",
                        "cst btrfs commit-interval apply",
                    ),
                    (
                        "io-dirty-expire-mismatch",
                        "ATTENTION",
                        "vm.dirty_expire_centisecs (10s) is shorter than root btrfs commit (30s)",
                        "cst btrfs install",
                    ),
                ],
            ),
        )
        cpu = evaluate_perf_cpu(
            CpuProbeSnapshot(
                idle_states=(CpuIdleStateSample("C1", 2, disabled=False),),
                irqbalance_active=True,
            )
        )
        self.assertEqual(
            axis(cpu),
            ([("cpuidle max enabled", "cpuidle: 2 µs"), ("irqbalance", "active (no ban-list)")], []),
        )


class _DictReader:
    """A PerfFsReader over a dict of files: (root, relative path) -> text, and keys -> text. It can hold
    names a real folder cannot (more than 255 bytes), and records every read_rel."""

    def __init__(self, files: dict[tuple[PerfRootKind, str], str], keys: dict[PerfPathKey, str] | None = None) -> None:
        self.files = files
        self.keys = keys or {}
        self.reads: list[tuple[PerfRootKind, str]] = []

    def read_key(self, key: PerfPathKey) -> str | None:
        return self.keys.get(key)

    def read_preempt(self) -> tuple[str | None, bool]:
        return None, False

    def read_rel(self, root: PerfRootKind, rel: str) -> str | None:
        self.reads.append((root, rel))
        return self.files.get((root, rel))

    def list_rel(self, root: PerfRootKind, rel: str) -> tuple[str, ...]:
        prefix = rel + "/"
        return tuple(
            sorted({p[len(prefix) :].split("/")[0] for r, p in self.files if r == root and p.startswith(prefix)})
        )

    def exists_rel(self, root: PerfRootKind, rel: str) -> bool:
        return (root, rel) in self.files or bool(self.list_rel(root, rel))

    def is_dir_rel(self, root: PerfRootKind, rel: str) -> bool:
        return bool(self.list_rel(root, rel))


class PerfAsciiNamesTest(unittest.TestCase):
    """The uid of a user-UID.slice, the N of a cpuidle stateN and an IRQ number are ASCII digits within
    int()'s limit: str.isdigit() also took "\N{SUPERSCRIPT TWO}" (which int() refuses: a traceback) and
    "\N{ARABIC-INDIC DIGIT THREE}" (which int() reads as 3), and int() raised past 4300 digits."""

    def test_a_session_slice_uid_is_ascii_digits(self) -> None:
        files: dict[tuple[PerfRootKind, str], str] = {}
        for uid in ("1000", "\N{SUPERSCRIPT TWO}", "1" * 5000, "\N{ARABIC-INDIC DIGIT THREE}"):
            sess = f"user.slice/user-{uid}.slice/user@{uid}.service/session.slice"
            files[PerfRootKind.CGROUP, f"{sess}/memory.current"] = f"{600 * MIB}\n"
            files[PerfRootKind.CGROUP, f"{sess}/memory.min"] = f"{1024 * MIB}\n"
        mem = collect_memory_from_reader(_DictReader(files))
        self.assertEqual([(s.name, s.uid) for s in mem.session_slices], [("user-1000.slice/session.slice", 1000)])

    def test_a_cpuidle_state_and_an_irq_number_are_ascii_digits(self) -> None:
        base = "devices/system/cpu/cpu0/cpuidle"
        files: dict[tuple[PerfRootKind, str], str] = {}
        for n, name in (("0", "C1"), ("\N{SUPERSCRIPT TWO}", "C2"), ("\N{ARABIC-INDIC DIGIT THREE}", "C3")):
            files[PerfRootKind.SYS, f"{base}/state{n}/name"] = f"{name}\n"
            files[PerfRootKind.SYS, f"{base}/state{n}/latency"] = "2\n"
        # An IRQ numbered "\N{SUPERSCRIPT TWO}" never gets its affinity read (parse_proc_interrupts drops
        # it already; this pins the reader's own check too, which passes on the old code as well).
        interrupts = "           CPU0       CPU1\n  10:  500000  0  amdgpu\n  \N{SUPERSCRIPT TWO}:  500000  0  x\n"
        files[PerfRootKind.PROC, "irq/10/effective_affinity_list"] = "0\n"
        reader = _DictReader(files, {PerfPathKey.INTERRUPTS: interrupts})
        cpu = collect_cpu_from_reader(reader)
        self.assertEqual([s.name for s in cpu.idle_states], ["C1"])
        self.assertIn((PerfRootKind.PROC, "irq/10/effective_affinity_list"), reader.reads)
        self.assertFalse([r for r in reader.reads if r[1].startswith("irq/\N{SUPERSCRIPT TWO}")])


class PerfScratchIntegrationTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-perf-test-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def _write(self, rel: str, content: str) -> str:
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)
        return full

    def _populate_healthy_tree(self) -> None:
        self._write("proc/uptime", "7200.0 14400.0\n")
        self._write("proc/meminfo", f"MemTotal: {64 * 1024 * 1024} kB\nMemAvailable: {48 * 1024 * 1024} kB\n")
        self._write("proc/vmstat", "compact_stall 0\nallocstall_normal 0\nworkingset_refault_file 10\n")
        self._write("proc/pressure/memory", "some avg10=0.00 avg60=0.00 total=0\nfull avg10=0.00 avg60=0.00 total=0\n")
        self._write("proc/sys/vm/min_free_kbytes", "262144\n")
        self._write("proc/sys/vm/watermark_scale_factor", "125\n")
        self._write("proc/sys/vm/dirty_background_bytes", "536870912\n")
        self._write("proc/sys/vm/dirty_bytes", "2147483648\n")
        self._write("proc/cmdline", "quiet preempt=lazy\n")
        self._write("proc/interrupts", "           CPU0       CPU1\n  10:       100          0   amdgpu\n")
        self._write("sys/kernel/mm/transparent_hugepage/enabled", "always [madvise] never\n")
        self._write("sys/kernel/mm/transparent_hugepage/defrag", "always defer [defer+madvise] madvise never\n")
        self._write("sys/kernel/mm/lru_gen/enabled", "0x0007\n")
        self._write("sys/kernel/mm/lru_gen/min_ttl_ms", "1000\n")
        self._write("sys/module/zswap/parameters/enabled", "Y\n")
        self._write("sys/module/zswap/parameters/compressor", "lz4\n")
        self._write("sys/module/zswap/parameters/max_pool_percent", "20\n")
        self._write("sys/devices/system/cpu/cpuidle/current_driver", "intel_idle\n")
        self._write("sys/devices/system/cpu/cpu0/cpuidle/state0/name", "C1\n")
        self._write("sys/devices/system/cpu/cpu0/cpuidle/state0/latency", "2\n")
        self._write("sys/devices/system/cpu/cpu0/cpuidle/state0/disable", "0\n")
        self._write("sys/class/misc/ntsync/dev", "10:123\n")
        self._write("run/systemd/units/invocation:irqbalance.service", "1\n")
        self._write("sys/block/nvme0n1/queue/scheduler", "[none] mq-deadline\n")
        self._write("sys/block/nvme0n1/queue/rotational", "0\n")
        self._write("sys/module/nvme_core/parameters/default_ps_max_latency_us", "10000\n")
        self._write("sys/fs/cgroup/io.cost.qos", "259:0 enable=1\n")
        self._write("sys/fs/cgroup/builds.slice/io.weight", "default 20\n")
        self._write("sys/fs/btrfs/12345678-aaaa/commit_stats", "commits 100\nmax_commit_ms 40\ntotal_commit_ms 500\n")
        self._write("etc/btrfs-setup/snapshots.conf", "SCRUB_LIMIT='100M'\n")
        self._write("sys/class/drm/card0/device/uevent", "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:84:00.0\n")
        self._write("sys/class/drm/card0/card0-DP-1/status", "connected\n")
        self._write("sys/class/drm/card0/device/power/control", "on\n")
        self._write("sys/class/drm/card0/device/power/runtime_status", "active\n")
        self._write("sys/class/drm/card0/device/pp_power_profile_mode", " 0 BOOTUP_DEFAULT*:\n")

    def test_adapter_build_and_presenter_outputs(self) -> None:
        self._populate_healthy_tree()
        with mock.patch.dict(os.environ, {"CST_PERF_ROOT": self.tmp}, clear=True):
            paths = perf_paths_from_env()
        self.assertEqual(paths.proc_dir, os.path.join(self.tmp, "proc"))
        rep = build_perf_report(LinuxPerfProbe(paths), ALL_PERF_AXES)
        self.assertEqual(rep.verdict, PerfVerdict.OPTIMIZED)

        dash_out = io.StringIO()
        self.assertEqual(render_perf_report(rep, out=dash_out), EXIT_PERF_OK)
        self.assertIn("perf: OPTIMIZED", dash_out.getvalue())
        self.assertIn("== diagnosis", dash_out.getvalue())

        json_out = io.StringIO()
        self.assertEqual(render_perf_report(rep, as_json=True, out=json_out), EXIT_PERF_OK)
        parsed = json.loads(json_out.getvalue())
        self.assertEqual(parsed, perf_report_to_json(rep))
        self.assertEqual(parsed["verdict"], "OPTIMIZED")

    def test_cli_subprocess_with_findings_and_unreadable_preempt(self) -> None:
        self._populate_healthy_tree()
        self._write("sys/class/drm/card0/device/power/control", "auto\n")
        preempt_p = os.path.join(self.tmp, "sys/kernel/debug/sched/preempt")
        self._write("sys/kernel/debug/sched/preempt", "lazy\n")
        if os.geteuid() != 0:
            os.chmod(preempt_p, 0o000)
            self.addCleanup(os.chmod, preempt_p, 0o600)

        env = {"PATH": "/usr/bin:/bin", "CST_PERF_ROOT": self.tmp}
        proc = subprocess.run(
            [sys.executable, "-I", "-B", _CST_PERF, "--compact"],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(proc.returncode, EXIT_PERF_FINDINGS)
        self.assertIn("gpu=ATTENTION", proc.stdout)
        self.assertIn("power/control=auto", proc.stdout)

        bad = io.StringIO()
        ok = io.StringIO()
        # main() puts SIGPIPE and SIGXFSZ back at their default: in-process, the rest of this worker's tests
        # would inherit that (a closed-stdout test then died of SIGPIPE).
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        with mock.patch.dict(os.environ, {"CST_PROG": "./cst perf"}, clear=True):
            self.assertEqual(perf_cli_main(["--help"], out=ok, err=bad), EXIT_PERF_OK)
            self.assertIn("Usage: ./cst perf", ok.getvalue())
            self.assertEqual(perf_cli_main(["--unknown"], out=ok, err=bad), EXIT_PERF_USAGE)
            self.assertIn("./cst perf: error:", bad.getvalue())

    def test_probe_defaults_without_root_env(self) -> None:
        from cst_core.adapters.runner import HostExecutionContext

        with mock.patch.dict(os.environ, {}, clear=True):
            paths = perf_paths_from_env()
        self.assertTrue(paths.proc_dir.endswith("/proc"))
        # Container without /run/host/proc falls back to local /proc rather than empty /run/host/*.
        missing_host = os.path.join(self.tmp, "missing-host")
        container_no_host = HostExecutionContext(exists=lambda p: p == "/.dockerenv", host_root=missing_host)
        with mock.patch.dict(os.environ, {}, clear=True):
            local_paths = perf_paths_from_env(ctx=container_no_host)
        self.assertEqual(local_paths.proc_dir, "/proc")
        # Container with mounted host_root/proc and host_root/etc uses host view.
        present_host = os.path.join(self.tmp, "present-host")
        os.makedirs(os.path.join(present_host, "proc"), exist_ok=True)
        os.makedirs(os.path.join(present_host, "etc"), exist_ok=True)
        container_with_host = HostExecutionContext(exists=lambda p: p == "/run/.containerenv", host_root=present_host)
        with mock.patch.dict(os.environ, {}, clear=True):
            host_paths = perf_paths_from_env(ctx=container_with_host)
        self.assertEqual(host_paths.proc_dir, f"{present_host}/proc")
        probe = LinuxPerfProbe(
            PerfPaths(
                proc_dir=self.tmp,
                sys_dir=self.tmp,
                cgroup_dir=self.tmp,
                etc_dir=self.tmp,
                run_dir=self.tmp,
                dev_dir=self.tmp,
                zswap_params_dir=self.tmp,
            )
        )
        self.assertEqual(evaluate_perf_snapshot(probe.collect_snapshot()).verdict, PerfVerdict.SKIP)

    def test_session_slice_clamped_by_root_user_slice_ancestor(self) -> None:
        self._populate_healthy_tree()
        self._write("sys/fs/cgroup/user.slice/memory.min", f"{250 * MIB}\n")
        self._write("sys/fs/cgroup/user.slice/user-1000.slice/memory.min", f"{1024 * MIB}\n")
        self._write("sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/memory.min", f"{1024 * MIB}\n")
        sess = "sys/fs/cgroup/user.slice/user-1000.slice/user@1000.service/session.slice"
        self._write(f"{sess}/memory.min", f"{1024 * MIB}\n")
        self._write(f"{sess}/memory.low", "0\n")
        self._write(f"{sess}/memory.current", f"{600 * MIB}\n")

        with mock.patch.dict(os.environ, {"CST_PERF_ROOT": self.tmp}, clear=True):
            rep = build_perf_report(LinuxPerfProbe(perf_paths_from_env()), (PerfAxis.MEMORY,))
        self.assertEqual(rep.verdict, PerfVerdict.ATTENTION)
        self.assertTrue(any(f.finding_id == "memory-session-floor" for f in rep.axes[0].findings))

    def test_deep_cstates_read_the_cap_cpu_setup_conf_sets(self) -> None:
        # The healthy tree has intel_idle; an enabled 350 us state is a finding under cpu-power's 200 us
        # default and none when cpu-setup.conf turns the cap off, as cpu-power then leaves it on.
        self._populate_healthy_tree()
        state = "sys/devices/system/cpu/cpu0/cpuidle/state9"
        self._write(f"{state}/name", "C3\n")
        self._write(f"{state}/latency", "350\n")
        self._write(f"{state}/disable", "0\n")
        conf = "etc/cpu-setup/cpu-setup.conf"
        for text, setting, warned in (
            (None, "", True),
            ("BUILD_CPUS='12-23'\n", "", True),
            ("BUILD_CPUS='12-23'\nCPU_CSTATE_MAX_LATENCY_US='off'\n", "off", False),
            ("CPU_CSTATE_MAX_LATENCY_US=400\n", "400", False),
        ):
            with self.subTest(conf=text):
                path = os.path.join(self.tmp, conf)
                if text is None:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(path)
                else:
                    self._write(conf, text)
                with mock.patch.dict(os.environ, {"CST_PERF_ROOT": self.tmp}, clear=True):
                    probe = LinuxPerfProbe(perf_paths_from_env())
                    self.assertEqual(probe.collect_snapshot().cpu.cstate_setting, setting)
                    rep = build_perf_report(probe, (PerfAxis.CPU,))
                ids = {f.finding_id for f in rep.axes[0].findings}
                self.assertEqual("cpu-deep-cstates" in ids, warned, ids)

    def test_irqbalance_env_precedence_effective_affinity_and_iocost_enable_zero(self) -> None:
        self._populate_healthy_tree()
        self._write("etc/cpu-setup/cpu-setup.conf", "BUILD_CPUS='12-23'\nGPU_PM_MODE='game'\n")
        self._write("etc/sysconfig/irqbalance", "IRQBALANCE_BANNED_CPULIST=0\n")
        self._write("etc/cpu-setup/irqbalance.env", "IRQBALANCE_BANNED_CPULIST=12-23\n")
        self._write("proc/interrupts", "           CPU0       CPU20\n  246:      100    500000   IR-PCI-MSIX amdgpu\n")
        self._write("proc/irq/246/effective_affinity_list", "2\n")
        self._write("sys/fs/cgroup/io.cost.qos", "259:1 enable=0 ctrl=auto\n")
        # Without 10-cst-ban.conf drop-in, irqbalance.env is not active and sysconfig (=0) fails BUILD_CPUS coverage.
        with mock.patch.dict(os.environ, {"CST_PERF_ROOT": self.tmp}, clear=True):
            no_dropin = build_perf_report(LinuxPerfProbe(perf_paths_from_env()), (PerfAxis.CPU,))
        self.assertEqual([f.finding_id for f in no_dropin.axes[0].findings], ["cpu-irqbalance-ban-missing"])

        self._write(
            "etc/systemd/system/irqbalance.service.d/10-cst-ban.conf",
            "[Service]\nEnvironmentFile=-/etc/cpu-setup/irqbalance.env\n",
        )
        self._write("sys/class/drm/card0/device/power/control", "auto\n")
        self._write("run/cpu-setup/manual", "1\n")
        with mock.patch.dict(os.environ, {"CST_PERF_ROOT": self.tmp}, clear=True):
            rep = build_perf_report(LinuxPerfProbe(perf_paths_from_env()), (PerfAxis.CPU, PerfAxis.IO, PerfAxis.GPU))
        cpu_rep = next(ax for ax in rep.axes if ax.axis == PerfAxis.CPU)
        io_rep = next(ax for ax in rep.axes if ax.axis == PerfAxis.IO)
        gpu_rep = next(ax for ax in rep.axes if ax.axis == PerfAxis.GPU)
        self.assertEqual(cpu_rep.verdict, PerfVerdict.OPTIMIZED)
        self.assertEqual([f.finding_id for f in io_rep.findings], ["io-weight-no-iocost"])
        self.assertEqual(io_rep.findings[0].fix_command, "cst btrfs iocost apply")
        self.assertEqual(gpu_rep.verdict, PerfVerdict.OPTIMIZED)


if __name__ == "__main__":
    unittest.main()

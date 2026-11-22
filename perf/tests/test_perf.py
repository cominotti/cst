# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst perf` parsers, diagnosis thresholds, adapter, CLI, and presenter."""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import override
import unittest
from unittest import mock

from cst_core.domain.values import GIB, MIB
from perf.adapters import LinuxPerfProbe, PerfPaths
from perf.application import build_perf_report
from perf.cli import main as perf_cli_main
from perf.domain.evaluate import (
    evaluate_perf_cpu,
    evaluate_perf_gpu,
    evaluate_perf_io,
    evaluate_perf_memory,
    evaluate_perf_snapshot,
    perf_report_to_json,
    verdict_exit_code,
    worst_perf_verdict,
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
    PerfCliError,
    PerfProbeSnapshot,
    PerfVerdict,
)
from perf.domain.parsers import (
    parse_amd_active_dpm_profile,
    parse_bracket_choice,
    parse_btrfs_commit_stats,
    parse_optional_cpuset,
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
        with self.assertRaises(PerfCliError):
            parse_perf_args(["--bogus"])

    def test_proc_and_sys_parsers(self) -> None:
        self.assertEqual(parse_proc_kib_table("MemTotal:   1024 kB\nFoo: bar\n")["MemTotal"], 1024)
        self.assertEqual(parse_proc_space_kv("compact_stall 42\nbad\n"), {"compact_stall": 42})
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
        self.assertEqual(zswap_f.fix_command, "sudo cst swap apply")
        for fid in ("memory-min-free", "memory-thp-defrag", "memory-mglru-min-ttl", "memory-session-floor"):
            match_f = next(f for f in sick_rep.findings if f.finding_id == fid)
            self.assertEqual(match_f.fix_command, "sudo cst swap apply", fid)

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
        self.assertEqual(cstate_f.fix_command, "sudo cst cpu install")
        ntsync_f = next(f for f in bad_rep.findings if f.finding_id == "cpu-ntsync-absent")
        self.assertEqual(ntsync_f.fix_command, "sudo cst cpu install")
        irq_f = next(f for f in bad_rep.findings if f.finding_id == "cpu-irqbalance-inactive")
        self.assertEqual(irq_f.fix_command, "sudo cst cpu irq apply")
        ov_f = next(f for f in bad_rep.findings if f.finding_id == "cpu-irq-build-overlap")
        self.assertEqual(ov_f.fix_command, "sudo cst cpu irq apply")

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
        self.assertEqual(miss_rep.findings[0].fix_command, "sudo cst cpu irq apply")

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
        self.assertEqual(nvme_f.fix_command, "sudo cst btrfs install")
        scrub_f = next(f for f in io_rep.findings if f.finding_id == "io-btrfs-scrub-limit")
        self.assertEqual(scrub_f.fix_command, "sudo cst btrfs scrub-limit set 500M")
        iocost_f = next(f for f in io_rep.findings if f.finding_id == "io-weight-no-iocost")
        self.assertEqual(iocost_f.fix_command, "sudo cst btrfs iocost apply")

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
        self.assertEqual(pm_f.fix_command, "sudo cst cpu install")
        prof_f = next(f for f in gpu_rep.findings if f.finding_id == "gpu-game-profile-card3")
        self.assertEqual(prof_f.fix_command, "sudo systemctl restart cpu-game.service")


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
        self.assertEqual(perf_cli_main(["--help"], out=ok, err=bad), EXIT_PERF_OK)
        self.assertIn("Usage: cst perf", ok.getvalue())
        self.assertEqual(perf_cli_main(["--unknown"], out=ok, err=bad), EXIT_PERF_USAGE)
        self.assertIn("error:", bad.getvalue())

    def test_probe_defaults_without_root_env(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            paths = perf_paths_from_env()
        self.assertTrue(paths.proc_dir.endswith("/proc"))
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

    def test_irqbalance_env_precedence_effective_affinity_and_iocost_enable_zero(self) -> None:
        self._populate_healthy_tree()
        self._write("etc/cpu-setup/cpu-setup.conf", "BUILD_CPUS='12-23'\n")
        self._write("etc/sysconfig/irqbalance", "IRQBALANCE_BANNED_CPULIST=0\n")
        self._write("etc/cpu-setup/irqbalance.env", "IRQBALANCE_BANNED_CPULIST=12-23\n")
        self._write("proc/interrupts", "           CPU0       CPU20\n  246:      100    500000   IR-PCI-MSIX amdgpu\n")
        self._write("proc/irq/246/effective_affinity_list", "2\n")
        self._write("sys/fs/cgroup/io.cost.qos", "259:1 enable=0 ctrl=auto\n")
        with mock.patch.dict(os.environ, {"CST_PERF_ROOT": self.tmp}, clear=True):
            rep = build_perf_report(LinuxPerfProbe(perf_paths_from_env()), (PerfAxis.CPU, PerfAxis.IO))
        cpu_rep = next(ax for ax in rep.axes if ax.axis == PerfAxis.CPU)
        io_rep = next(ax for ax in rep.axes if ax.axis == PerfAxis.IO)
        self.assertEqual(cpu_rep.verdict, PerfVerdict.OPTIMIZED)
        self.assertEqual([f.finding_id for f in io_rep.findings], ["io-weight-no-iocost"])
        self.assertEqual(io_rep.findings[0].fix_command, "sudo cst btrfs iocost apply")


if __name__ == "__main__":
    unittest.main()

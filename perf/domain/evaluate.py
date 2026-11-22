# SPDX-License-Identifier: MPL-2.0
"""Pure threshold evaluation and objective diagnosis rules for `cst perf`."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
import re
from typing import Final

from cst_core.domain.text import group
from cst_core.domain.values import CpuSet, HOUR, format_bytes_human
from perf.domain.models import (
    ALL_PERF_AXES,
    ALLOCSTALL_ATTENTION_PER_HOUR,
    ALLOCSTALL_BOTTLENECK_PER_HOUR,
    BTRFS_AVG_COMMIT_ATTENTION_MS,
    BTRFS_AVG_COMMIT_BOTTLENECK_MS,
    BTRFS_MAX_COMMIT_ATTENTION_MS,
    BTRFS_MAX_COMMIT_BOTTLENECK_MS,
    BTRFS_SHORT_UUID_LEN,
    CENTISECONDS_PER_SEC,
    CgroupMemorySlice,
    COMPACT_ATTENTION_PER_HOUR,
    COMPACT_BOTTLENECK_PER_HOUR,
    CSTATE_DEEP_LATENCY_US,
    CpuIdleStateSample,
    CpuProbeSnapshot,
    DEFAULT_CG_IO_WEIGHT,
    EXIT_PERF_FINDINGS,
    EXIT_PERF_OK,
    GPU_PM_MODE_ALWAYS,
    GPU_PM_MODE_GAME,
    GpuProbeSnapshot,
    HUMAN_UID_MIN,
    IRQ_ACTIVE_MIN_COUNT,
    IrqLineSample,
    IRQ_DOMINANT_SHARE,
    IRQ_MIN_UNBANNED_CPUS,
    IoProbeSnapshot,
    KERNEL_DEFAULT_BTRFS_COMMIT_S,
    LARGE_RAM_THRESHOLD_BYTES,
    MAX_BLOCK_DISPLAY,
    MEM_MIN_FREE_LARGE_RAM_BYTES,
    MEM_MIN_FREE_SMALL_RAM_BYTES,
    MGLRU_RECOMMENDED_MIN_TTL_MS,
    MIN_UPTIME_FOR_RATE_SECONDS,
    MemoryProbeSnapshot,
    NVME_PS_MAX_LATENCY_WARN_US,
    PSI_MEM_FULL_BOTTLENECK_PCT,
    PSI_MEM_SOME_ATTENTION_PCT,
    PSI_MEM_SOME_BOTTLENECK_PCT,
    PerfAxis,
    PerfAxisJson,
    PerfAxisReport,
    PerfFinding,
    PerfFindingJson,
    PerfMetric,
    PerfMetricJson,
    PerfProbeSnapshot,
    PerfReport,
    PerfReportJson,
    PerfVerdict,
    ROOT_BTRFS_TARGET_COMMIT_S,
    SESSION_FLOOR_HEADROOM_DEN,
    SESSION_FLOOR_HEADROOM_NUM,
    SESSION_MIN_RECOMMENDED_BYTES,
    TOP_ACTION_LIMIT,
    TOP_IRQ_DISPLAY_LIMIT,
)

_VERDICT_RANK: Final[dict[PerfVerdict, int]] = {
    PerfVerdict.SKIP: 0,
    PerfVerdict.OPTIMIZED: 1,
    PerfVerdict.ATTENTION: 2,
    PerfVerdict.BOTTLENECK: 3,
}

_THP_DEFRAG_PAUSE_MODES: Final[frozenset[str]] = frozenset({"always", "madvise"})
_PREEMPT_GOOD_MODES: Final[frozenset[str]] = frozenset({"full", "lazy"})
_LATENCY_IRQ_TOKENS: Final[tuple[str, ...]] = (
    "nvme",
    "amdgpu",
    "xe",
    "i915",
    "nvidia",
    "xhci",
    "snd_",
    "ahci",
    "enp",
    "eno",
    "eth",
    "wlan",
    "wlp",
    "kvm",
    "vfio",
)
_GPU_RUNTIME_CONTROL_AUTO: Final = "auto"
_GPU_AMD_BOOTUP_PROFILE: Final = "BOOTUP_DEFAULT"
_SCHED_NONE: Final = "none"
_MANAGED_NVME_QUEUE_RE: Final = re.compile(r"\bnvme[0-9]+q[1-9][0-9]*\b")
_ZERO_HEX: Final = "0x0000"
_ZERO_DEC: Final = "0"


@dataclass(frozen=True, slots=True)
class _StallThreshold:
    label: str
    finding_id: str
    attn_rate: float
    crit_rate: float
    summary_prefix: str
    detail: str
    recommendation: str


_STALL_RULES: Final[tuple[_StallThreshold, ...]] = tuple(
    _StallThreshold(*row)
    for row in (
        (
            "allocstall",
            "memory-allocstall",
            ALLOCSTALL_ATTENTION_PER_HOUR,
            ALLOCSTALL_BOTTLENECK_PER_HOUR,
            "Direct page reclaim stalls",
            "Foreground allocations blocked waiting for synchronous page reclaim.",
            "Raise watermark headroom / vm.min_free_kbytes so kswapd reclaims earlier.",
        ),
        (
            "compact_stall",
            "memory-compact-stall",
            COMPACT_ATTENTION_PER_HOUR,
            COMPACT_BOTTLENECK_PER_HOUR,
            "Synchronous compaction stalls",
            "Threads stalled defragmenting pages during hugepage or high-order allocations.",
            "Set THP defrag=defer+madvise (avoid synchronous madvise compaction).",
        ),
    )
)


class _AxisCollector:
    """Collect ordered metrics and findings for a single axis cleanly."""

    __slots__ = ("axis", "findings", "metrics", "seen")

    def __init__(self, axis: PerfAxis) -> None:
        self.axis = axis
        self.metrics: list[PerfMetric] = []
        self.findings: list[PerfFinding] = []
        self.seen = False

    def metric(self, label: str, value: str) -> None:
        self.seen = True
        self.metrics.append(PerfMetric(label, value))

    def warn(self, finding_id: str, summary: str, detail: str, recommendation: str = "") -> None:
        self.findings.append(PerfFinding(finding_id, self.axis, PerfVerdict.ATTENTION, summary, detail, recommendation))

    def warn_fix(self, finding_id: str, summary: str, detail: str, guidance: tuple[str, str]) -> None:
        recommendation, fix_command = guidance
        self.findings.append(
            PerfFinding(finding_id, self.axis, PerfVerdict.ATTENTION, summary, detail, recommendation, fix_command)
        )

    def add(self, finding: PerfFinding) -> None:
        self.findings.append(finding)

    def report(self) -> PerfAxisReport:
        if not self.seen:
            verdict = PerfVerdict.SKIP
        elif not self.findings:
            verdict = PerfVerdict.OPTIMIZED
        else:
            verdict = worst_perf_verdict(f.verdict for f in self.findings)
        return PerfAxisReport(
            axis=self.axis,
            verdict=verdict,
            metrics=tuple(self.metrics),
            findings=tuple(self.findings),
        )


def format_perf_cpuset(cpuset: CpuSet) -> str:
    """Format a `CpuSet` compactly using Linux range syntax (`0-7,12-23`)."""
    nums = cpuset.sorted_cpus()
    if not nums:
        return ""
    ranges: list[str] = []
    start = end = nums[0]
    for cpu in nums[1:]:
        if cpu == end + 1:
            end = cpu
        else:
            ranges.append(f"{start}-{end}" if start != end else str(start))
            start = end = cpu
    ranges.append(f"{start}-{end}" if start != end else str(start))
    return ",".join(ranges)


def worst_perf_verdict(verdicts: Iterable[PerfVerdict]) -> PerfVerdict:
    """Return the most severe verdict among `verdicts`, or `SKIP` if empty."""
    best = PerfVerdict.SKIP
    for v in verdicts:
        if _VERDICT_RANK[v] > _VERDICT_RANK[best]:
            best = v
    return best


def verdict_exit_code(verdict: PerfVerdict) -> int:
    """Map overall `PerfVerdict` to process exit code (`0` OK/SKIP, `1` ATTENTION/BOTTLENECK)."""
    if verdict in (PerfVerdict.ATTENTION, PerfVerdict.BOTTLENECK):
        return EXIT_PERF_FINDINGS
    return EXIT_PERF_OK


def _hourly_rate(count: int, uptime_seconds: float | None) -> float | None:
    if uptime_seconds is None or uptime_seconds < MIN_UPTIME_FOR_RATE_SECONDS:
        return None
    return count / (uptime_seconds / float(HOUR))


def _rate_str(count: int, uptime_seconds: float | None) -> str:
    rate = _hourly_rate(count, uptime_seconds)
    return f"{count} ({rate:.1f}/h)" if rate is not None else str(count)


def _eval_memory_headroom(col: _AxisCollector, mem: MemoryProbeSnapshot) -> None:
    if mem.mem_total_bytes is not None:
        avail = format_bytes_human(mem.mem_available_bytes) if mem.mem_available_bytes is not None else "?"
        col.metric("RAM available", f"{avail} / {format_bytes_human(mem.mem_total_bytes)}")
    if mem.min_free_bytes is not None:
        col.metric("vm.min_free_kbytes", format_bytes_human(mem.min_free_bytes))
        if mem.mem_total_bytes is not None:
            target = (
                MEM_MIN_FREE_LARGE_RAM_BYTES
                if mem.mem_total_bytes >= LARGE_RAM_THRESHOLD_BYTES
                else MEM_MIN_FREE_SMALL_RAM_BYTES
            )
            if mem.min_free_bytes < target:
                col.warn_fix(
                    "memory-min-free",
                    f"vm.min_free_kbytes is low ({format_bytes_human(mem.min_free_bytes)}) for host RAM",
                    f"Current free reserve is below {format_bytes_human(target)} headroom.",
                    ("Apply CST swap tuning to set vm.min_free_kbytes = 256 MiB.", "cst swap apply"),
                )
    if mem.watermark_scale_factor is not None:
        col.metric("vm.watermark_scale_factor", str(mem.watermark_scale_factor))


def _eval_stall_counter(col: _AxisCollector, count: int | None, uptime: float | None, rule: _StallThreshold) -> None:
    if count is None:
        return
    col.metric(rule.label, _rate_str(count, uptime))
    rate = _hourly_rate(count, uptime)
    if rate is not None and rate >= rule.attn_rate:
        verdict = PerfVerdict.BOTTLENECK if rate >= rule.crit_rate else PerfVerdict.ATTENTION
        col.add(
            PerfFinding(
                finding_id=rule.finding_id,
                axis=col.axis,
                verdict=verdict,
                summary=f"{rule.summary_prefix} ({rate:.1f}/h, total {count})",
                detail=rule.detail,
                recommendation=rule.recommendation,
            )
        )


def _eval_memory_stalls(col: _AxisCollector, mem: MemoryProbeSnapshot) -> None:
    _eval_stall_counter(col, mem.allocstall_count, mem.uptime_seconds, _STALL_RULES[0])
    _eval_stall_counter(col, mem.compact_stall_count, mem.uptime_seconds, _STALL_RULES[1])
    if mem.workingset_refault_file is not None:
        col.metric("workingset_refault_file", _rate_str(mem.workingset_refault_file, mem.uptime_seconds))


def _eval_memory_mm_policy(col: _AxisCollector, mem: MemoryProbeSnapshot) -> None:
    if mem.thp_enabled is not None or mem.thp_defrag is not None:
        col.metric("THP", f"enabled={mem.thp_enabled or '?'} defrag={mem.thp_defrag or '?'}")
        if mem.thp_defrag in _THP_DEFRAG_PAUSE_MODES:
            col.warn_fix(
                "memory-thp-defrag",
                f"THP defrag is {mem.thp_defrag!r} (synchronous compaction on page faults)",
                "Apps requesting MADV_HUGEPAGE stall waiting for direct compaction.",
                ("Switch THP defrag to defer+madvise so kcompactd compacts in background.", "cst swap apply"),
            )
    if mem.lru_gen_enabled is not None:
        ttl_str = f"{mem.lru_gen_min_ttl_ms} ms" if mem.lru_gen_min_ttl_ms is not None else "?"
        col.metric("MGLRU", f"enabled={mem.lru_gen_enabled}, min_ttl_ms={ttl_str}")
        if mem.lru_gen_enabled in (_ZERO_DEC, _ZERO_HEX):
            col.warn(
                "memory-mglru-disabled",
                "MGLRU is disabled",
                "Kernel uses legacy active/inactive LRU instead of Multi-Gen LRU.",
                "Enable /sys/kernel/mm/lru_gen/enabled (7) and set min_ttl_ms >= 1000.",
            )
        elif mem.lru_gen_min_ttl_ms is not None and mem.lru_gen_min_ttl_ms == 0:
            col.warn_fix(
                "memory-mglru-min-ttl",
                "MGLRU min_ttl_ms is 0 (no thrashing floor)",
                "Newly faulted working-set pages can be evicted immediately during bursts.",
                (f"Set lru_gen/min_ttl_ms to {MGLRU_RECOMMENDED_MIN_TTL_MS}-5000 ms.", "cst swap apply"),
            )


def _eval_memory_zswap_and_psi(col: _AxisCollector, mem: MemoryProbeSnapshot) -> None:
    if mem.zswap_enabled is not None:
        z_state = "enabled" if mem.zswap_enabled else "disabled"
        z_extra = f", {mem.zswap_compressor}" if mem.zswap_compressor is not None else ""
        z_pool = f", max_pool={mem.zswap_max_pool_percent}%" if mem.zswap_max_pool_percent is not None else ""
        col.metric("zswap", f"{z_state}{z_extra}{z_pool}")
        if mem.zswap_enabled is False:
            col.warn_fix(
                "memory-zswap-disabled",
                "zswap is disabled",
                "Anonymous pages swap straight to disk instead of fast compressed RAM pool.",
                ("Apply CST swap profile to enable zswap + encrypted swap0.", "cst swap apply"),
            )
    if mem.psi_some_avg60 is not None or mem.psi_full_avg60 is not None:
        some_s = f"{mem.psi_some_avg60:.2f}%" if mem.psi_some_avg60 is not None else "?"
        full_s = f"{mem.psi_full_avg60:.2f}%" if mem.psi_full_avg60 is not None else "?"
        col.metric("PSI memory avg60", f"some={some_s} full={full_s}")
        if (mem.psi_full_avg60 is not None and mem.psi_full_avg60 >= PSI_MEM_FULL_BOTTLENECK_PCT) or (
            mem.psi_some_avg60 is not None and mem.psi_some_avg60 >= PSI_MEM_SOME_BOTTLENECK_PCT
        ):
            col.add(
                PerfFinding(
                    finding_id="memory-psi-high",
                    axis=PerfAxis.MEMORY,
                    verdict=PerfVerdict.BOTTLENECK,
                    summary=f"Elevated memory PSI avg60 (some={some_s}, full={full_s})",
                    detail="Tasks are measurably stalled waiting on memory reclaim or thrashing.",
                    recommendation="Inspect heavy cgroups (`cst swap status`) and increase reclaim/floor headroom.",
                )
            )
        elif mem.psi_some_avg60 is not None and mem.psi_some_avg60 >= PSI_MEM_SOME_ATTENTION_PCT:
            col.warn(
                "memory-psi-elevated",
                f"Moderate memory pressure PSI avg60 (some={some_s})",
                "Some tasks waited briefly for memory over the last minute.",
            )


_SLICE_UID_RE: Final = re.compile(r"^user-([0-9]+)\.slice/")


def _slice_uid(sslice: CgroupMemorySlice) -> int | None:
    if sslice.uid is not None:
        return sslice.uid
    match = _SLICE_UID_RE.match(sslice.name)
    return int(group(match, 1)) if match is not None else None


def _eval_memory_cgroups(col: _AxisCollector, mem: MemoryProbeSnapshot) -> None:
    entries = [
        (
            sslice,
            min(sslice.min_bytes, sslice.parent_min_bytes) if sslice.parent_min_bytes is not None else sslice.min_bytes,
            _slice_uid(sslice),
        )
        for sslice in mem.session_slices
    ]
    any_human_protected = any(eff > 0 and (uid is None or uid >= HUMAN_UID_MIN) for _, eff, uid in entries)
    for sslice, eff_min, uid in entries:
        col.metric(
            sslice.name,
            f"current={format_bytes_human(sslice.current_bytes)}, eff_min={format_bytes_human(eff_min)}",
        )
        if uid is not None and uid < HUMAN_UID_MIN:
            continue
        headroom_cap = eff_min * SESSION_FLOOR_HEADROOM_NUM // SESSION_FLOOR_HEADROOM_DEN
        if eff_min == 0:
            if any_human_protected:
                continue
            col.warn_fix(
                "memory-session-floor-unset",
                f"{sslice.name} has effective memory.min=0",
                "Desktop compositor/shell lacks hard reclaim protection under pressure.",
                ("Configure a hard MemoryMin floor on user/session slices via uresourced.", "cst swap apply"),
            )
        elif sslice.current_bytes > headroom_cap and eff_min < SESSION_MIN_RECOMMENDED_BYTES:
            col.warn_fix(
                "memory-session-floor",
                (
                    f"{sslice.name} current ({format_bytes_human(sslice.current_bytes)}) exceeds "
                    f"effective memory.min ({format_bytes_human(eff_min)})"
                ),
                "Effective session protection is capped below active Shell/compositor working set.",
                ("Raise uresourced MemoryMin above active compositor footprint (1G).", "cst swap apply"),
            )


def evaluate_perf_memory(mem: MemoryProbeSnapshot) -> PerfAxisReport:
    """Evaluate memory pressure, reclaim stalls, THP, MGLRU, zswap, and cgroup floors."""
    col = _AxisCollector(PerfAxis.MEMORY)
    _eval_memory_headroom(col, mem)
    _eval_memory_stalls(col, mem)
    _eval_memory_mm_policy(col, mem)
    _eval_memory_zswap_and_psi(col, mem)
    _eval_memory_cgroups(col, mem)
    return col.report()


def _worst_enabled_deep_cstate(states: Sequence[CpuIdleStateSample]) -> CpuIdleStateSample | None:
    enabled_deep = [s for s in states if not s.disabled and s.latency_us >= CSTATE_DEEP_LATENCY_US]
    return max(enabled_deep, key=lambda s: s.latency_us) if enabled_deep else None


def _irq_overlap_detail(line: IrqLineSample, build_set: frozenset[int]) -> str | None:
    """Return a formatted overlap label if a movable IRQ hits `build_set`, else None."""
    if _MANAGED_NVME_QUEUE_RE.search(line.label) is not None:
        return None
    dev_name = line.label.split()[-1] if line.label.strip() else "irq"
    if line.effective_cpus is not None:
        overlap = line.effective_cpus.cpus & build_set
        if not overlap:
            return None
        return f"IRQ {line.irq_id} ({dev_name} on CPU {format_perf_cpuset(CpuSet(frozenset(overlap)))})"
    total = line.total_count
    build_hits = sum(cnt for c, cnt in line.counts_by_cpu.items() if c in build_set)
    if total <= 0 or build_hits / total < IRQ_DOMINANT_SHARE:
        return None
    dom_cpu = max(line.counts_by_cpu.items(), key=lambda kv: kv[1])[0]
    return f"IRQ {line.irq_id} ({dev_name} on CPU {dom_cpu})"


def _eval_irq_build_overlap(col: _AxisCollector, cpu: CpuProbeSnapshot) -> None:
    if cpu.build_cpus is None or not cpu.irq_lines:
        return
    build_spec = format_perf_cpuset(cpu.build_cpus)
    build_set = cpu.build_cpus.cpus
    hot_on_build: list[str] = []
    for line in cpu.irq_lines:
        if line.total_count < IRQ_ACTIVE_MIN_COUNT:
            continue
        low_label = line.label.lower()
        if not any(tok in low_label for tok in _LATENCY_IRQ_TOKENS):
            continue
        detail = _irq_overlap_detail(line, build_set)
        if detail is not None:
            hot_on_build.append(detail)
    col.metric("BUILD_CPUS", build_spec)
    if hot_on_build:
        summary_list = ", ".join(hot_on_build[:TOP_IRQ_DISPLAY_LIMIT])
        if len(hot_on_build) > TOP_IRQ_DISPLAY_LIMIT:
            summary_list += f" (+{len(hot_on_build) - TOP_IRQ_DISPLAY_LIMIT} more)"
        col.warn_fix(
            "cpu-irq-build-overlap",
            f"Latency-sensitive IRQs hit BUILD_CPUS ({build_spec}): {summary_list}",
            "Hardware interrupts routed onto build cores compete with heavy compilation/indexing.",
            (
                "Configure irqbalance ban-list (`IRQBALANCE_BANNED_CPULIST`) or steer IRQs off BUILD_CPUS.",
                "cst cpu irq apply",
            ),
        )


def _eval_cpu_kernel(col: _AxisCollector, cpu: CpuProbeSnapshot) -> None:
    if cpu.preempt_mode is not None:
        col.metric("preempt", cpu.preempt_mode)
        if cpu.preempt_mode not in _PREEMPT_GOOD_MODES:
            col.warn(
                "cpu-preempt-mode",
                f"Kernel preemption is {cpu.preempt_mode!r}",
                "Non-preemptible / voluntary mode increases scheduling wakeup latency under load.",
                "Enable preempt=lazy or preempt=full for desktop responsiveness.",
            )
    elif cpu.preempt_unreadable:
        col.metrics.append(PerfMetric("preempt", "unreadable (debugfs root-only)"))
    if cpu.sched_ext_state is not None:
        col.metric("sched_ext", cpu.sched_ext_state)
    if cpu.ntsync_present is not None:
        col.metric("ntsync", "present" if cpu.ntsync_present else "absent")
        if cpu.ntsync_present is False:
            col.warn_fix(
                "cpu-ntsync-absent",
                "/dev/ntsync is not active",
                "Wine/Proton falls back to higher-overhead fsync/esync syscall emulation.",
                ("Load/autoload the `ntsync` kernel module via modules-load.d.", "cst cpu install"),
            )


def _expected_banned_cpus(build_cpus: CpuSet, online_cpus: CpuSet | None) -> frozenset[int]:
    if online_cpus is None:
        return build_cpus.cpus
    wanted = sorted(build_cpus.cpus & online_cpus.cpus)
    total_online = len(online_cpus.cpus)
    if not wanted or total_online <= IRQ_MIN_UNBANNED_CPUS:
        return frozenset()
    max_banned = total_online - IRQ_MIN_UNBANNED_CPUS
    return frozenset(wanted[len(wanted) - max_banned :] if len(wanted) > max_banned else wanted)


def evaluate_perf_cpu(cpu: CpuProbeSnapshot) -> PerfAxisReport:
    """Evaluate cpuidle wakeup latency, preemption mode, ntsync, and IRQ placement."""
    col = _AxisCollector(PerfAxis.CPU)
    if cpu.idle_states:
        worst_deep = _worst_enabled_deep_cstate(cpu.idle_states)
        max_enabled = max((s.latency_us for s in cpu.idle_states if not s.disabled), default=0)
        col.metric("cpuidle max enabled", f"{cpu.cpuidle_driver or 'cpuidle'}: {max_enabled} µs")
        if worst_deep is not None:
            col.warn_fix(
                "cpu-deep-cstates",
                (
                    f"Deep idle state {worst_deep.name} enabled "
                    f"({worst_deep.latency_us} µs >= {CSTATE_DEEP_LATENCY_US} µs)"
                ),
                "Deep package/core C-states add wakeup tail latency spikes during interactive/gaming loads.",
                ("Cap C-state latency via CPU_CSTATE_MAX_LATENCY_US (`cst cpu power`).", "cst cpu install"),
            )
    _eval_cpu_kernel(col, cpu)
    if cpu.irqbalance_active is not None:
        if cpu.irqbalance_active is False:
            col.metric("irqbalance", "inactive")
            col.warn_fix(
                "cpu-irqbalance-inactive",
                "irqbalance service is inactive",
                "Hardware interrupts stay pinned statically instead of balancing across cores.",
                ("Enable irqbalance with BUILD_CPUS excluded via `cst cpu irq`.", "cst cpu irq apply"),
            )
        else:
            ban_txt = (
                f"ban={format_perf_cpuset(cpu.irqbalance_banned_cpus)}"
                if cpu.irqbalance_banned_cpus is not None
                else "no ban-list"
            )
            col.metric("irqbalance", f"active ({ban_txt})")
            expected_ban = (
                _expected_banned_cpus(cpu.build_cpus, cpu.online_cpus) if cpu.build_cpus is not None else frozenset()
            )
            if expected_ban and (
                cpu.irqbalance_banned_cpus is None or not expected_ban.issubset(cpu.irqbalance_banned_cpus.cpus)
            ):
                col.warn_fix(
                    "cpu-irqbalance-ban-missing",
                    "irqbalance is active without banning BUILD_CPUS",
                    "Movable hardware interrupts may still be steered onto busy build cores.",
                    ("Configure CST's adaptive irqbalance ban-list via `cst cpu irq`.", "cst cpu irq apply"),
                )
    _eval_irq_build_overlap(col, cpu)
    return col.report()


def _eval_io_queues(col: _AxisCollector, io: IoProbeSnapshot) -> None:
    if io.block_devices:
        nvmes = [d for d in io.block_devices if d.name.startswith("nvme")]
        sample_devs = nvmes or list(io.block_devices)
        col.metric(
            "block schedulers",
            ", ".join(f"{d.name}={d.scheduler or '?'}" for d in sample_devs[:MAX_BLOCK_DISPLAY]),
        )
    has_non_default_weight = any(w != DEFAULT_CG_IO_WEIGHT for _, w in io.nonzero_slice_io_weights)
    if io.iocost_qos_active is not None or io.nonzero_slice_io_weights:
        weights_txt = (
            ", ".join(f"{name}={w}" for name, w in io.nonzero_slice_io_weights)
            if io.nonzero_slice_io_weights
            else "none"
        )
        if io.iocost_qos_active is True:
            qos_txt = (
                f"configured ({io.iocost_enabled_devices} device(s))" if io.iocost_enabled_devices > 0 else "configured"
            )
        else:
            qos_txt = "empty"
        col.metric("blk-iocost / io.weight", f"qos={qos_txt}; weights={weights_txt}")
        nvme_none = any(d.name.startswith("nvme") and d.scheduler == _SCHED_NONE for d in io.block_devices)
        if has_non_default_weight and io.iocost_qos_active is not True and (nvme_none or not io.block_devices):
            col.warn_fix(
                "io-weight-no-iocost",
                "Cgroup io.weight is configured without blk-iocost QoS",
                (
                    f"Slices ({weights_txt}) set non-default io.weight, which has no effect "
                    "when io.cost.qos is empty and NVMe queues use scheduler=none."
                ),
                (
                    "Enable kernel blk-iocost QoS (`enable=1 ctrl=auto`) on SSDs via `cst btrfs iocost`.",
                    "cst btrfs iocost apply",
                ),
            )


def _eval_io_nvme_and_dirty(col: _AxisCollector, io: IoProbeSnapshot) -> None:
    if io.nvme_ps_max_latency_us is not None:
        col.metric("nvme APST max latency", f"{io.nvme_ps_max_latency_us} µs")
        if io.nvme_ps_max_latency_us > NVME_PS_MAX_LATENCY_WARN_US:
            col.warn_fix(
                "io-nvme-apst",
                f"nvme_core default_ps_max_latency_us={io.nvme_ps_max_latency_us} µs",
                "Allows deep NVMe Autonomous Power State Transitions with multi-ms wakeup pauses.",
                (
                    "Tighten nvme_core.default_ps_max_latency_us (e.g. <= 10000 µs or 0 on desktop).",
                    "cst btrfs install",
                ),
            )
    if io.dirty_background_bytes is not None and io.dirty_bytes is not None:
        bg_txt = format_bytes_human(io.dirty_background_bytes) if io.dirty_background_bytes else "0 (ratio)"
        lim_txt = format_bytes_human(io.dirty_bytes) if io.dirty_bytes else "0 (ratio)"
        cadence_txt = ""
        if io.dirty_writeback_cs is not None and io.dirty_expire_cs is not None:
            wb_s = io.dirty_writeback_cs / CENTISECONDS_PER_SEC
            exp_s = io.dirty_expire_cs / CENTISECONDS_PER_SEC
            cadence_txt = f", wb={wb_s:g}s, exp={exp_s:g}s"
        col.metric("dirty writeback", f"bg={bg_txt}, max={lim_txt}{cadence_txt}")
        if io.dirty_background_bytes == 0 and io.dirty_bytes == 0:
            col.warn_fix(
                "io-dirty-bytes-unbounded",
                "vm.dirty_bytes / dirty_background_bytes are 0 (RAM-percentage defaults)",
                "Large-RAM hosts accumulate gigabytes of dirty buffers before writeback starts.",
                ("Bound dirty writeback in bytes (e.g. 512 MiB / 2 GiB via cst btrfs).", "cst btrfs install"),
            )
    if io.root_btrfs_commit_s is not None:
        flush_txt = ", flushoncommit" if io.root_btrfs_flushoncommit else ""
        default_txt = (
            " (default)"
            if io.root_btrfs_commit_s == KERNEL_DEFAULT_BTRFS_COMMIT_S and not io.root_btrfs_commit_explicit
            else ""
        )
        col.metric("root btrfs commit", f"{io.root_btrfs_commit_s}s{default_txt}{flush_txt}")
        if io.root_btrfs_flushoncommit and io.root_btrfs_commit_s < ROOT_BTRFS_TARGET_COMMIT_S:
            col.warn_fix(
                "io-btrfs-commit-short",
                f"Root btrfs commit interval is {io.root_btrfs_commit_s}s with flushoncommit",
                "Frequent 30s flushoncommit barriers increase metadata commits and SSD write amplification.",
                (
                    f"Align root btrfs mounts to commit={ROOT_BTRFS_TARGET_COMMIT_S} via `cst btrfs commit-interval`.",
                    "cst btrfs commit-interval apply",
                ),
            )
        if (
            io.dirty_expire_cs is not None
            and io.dirty_expire_cs > 0
            and io.dirty_expire_cs < io.root_btrfs_commit_s * CENTISECONDS_PER_SEC
        ):
            exp_s = io.dirty_expire_cs / CENTISECONDS_PER_SEC
            col.warn_fix(
                "io-dirty-expire-mismatch",
                f"vm.dirty_expire_centisecs ({exp_s:g}s) is shorter than root btrfs commit ({io.root_btrfs_commit_s}s)",
                "Kernel page expiry forces dirty pages to disk before the btrfs commit window closes.",
                ("Apply CST writeback sysctl tuning so page expiry matches commit=60.", "cst btrfs install"),
            )


def _eval_io_btrfs(col: _AxisCollector, io: IoProbeSnapshot) -> None:
    if io.btrfs_conf_present and io.btrfs_commits:
        col.metric("btrfs SCRUB_LIMIT", io.btrfs_scrub_limit if io.btrfs_scrub_limit is not None else "unset")
        if io.btrfs_scrub_limit is None or not io.btrfs_scrub_limit:
            col.warn_fix(
                "io-btrfs-scrub-limit",
                "btrfs SCRUB_LIMIT is unset",
                "Scheduled scrub runs at unthrottled NVMe bandwidth, starving interactive I/O.",
                (
                    "Set SCRUB_LIMIT (e.g. '500M') in /etc/btrfs-setup/snapshots.conf via cst btrfs scrub-limit.",
                    "cst btrfs scrub-limit set 500M",
                ),
            )
    for fs in io.btrfs_commits:
        avg_ms = fs.total_commit_ms / fs.commits if fs.commits > 0 else 0.0
        short_id = fs.fs_id[:BTRFS_SHORT_UUID_LEN]
        col.metric(f"btrfs {short_id} commits", f"n={fs.commits}, avg={avg_ms:.1f} ms, max={fs.max_commit_ms} ms")
        if fs.max_commit_ms >= BTRFS_MAX_COMMIT_BOTTLENECK_MS or avg_ms >= BTRFS_AVG_COMMIT_BOTTLENECK_MS:
            col.add(
                PerfFinding(
                    finding_id="io-btrfs-commit-latency",
                    axis=PerfAxis.IO,
                    verdict=PerfVerdict.BOTTLENECK,
                    summary=f"Severe btrfs commit latency on {short_id} (avg {avg_ms:.1f} ms, max {fs.max_commit_ms} ms)",
                    detail="Transaction commits stalled filesystem sync callers.",
                    recommendation="Inspect `cst btrfs report` and IO throttling/blk-iocost.",
                )
            )
        elif fs.max_commit_ms >= BTRFS_MAX_COMMIT_ATTENTION_MS or avg_ms >= BTRFS_AVG_COMMIT_ATTENTION_MS:
            col.warn(
                "io-btrfs-commit-latency",
                f"Elevated btrfs commit latency on {short_id} (avg {avg_ms:.1f} ms, max {fs.max_commit_ms} ms)",
                "Transaction commit tail exceeds smooth desktop latency target (< 500 ms max / 25 ms avg).",
                "Throttle background writes / scrub and inspect `cst btrfs report`.",
            )


def evaluate_perf_io(io: IoProbeSnapshot) -> PerfAxisReport:
    """Evaluate block schedulers, blk-iocost coherence, NVMe APST, dirty writeback, and btrfs commits."""
    col = _AxisCollector(PerfAxis.IO)
    _eval_io_queues(col, io)
    _eval_io_nvme_and_dirty(col, io)
    _eval_io_btrfs(col, io)
    return col.report()


def evaluate_perf_gpu(gpu: GpuProbeSnapshot) -> PerfAxisReport:
    """Evaluate connected GPU runtime PM and AMD DPM power profile state."""
    col = _AxisCollector(PerfAxis.GPU)
    for dev in gpu.gpus:
        drv = dev.driver or "?"
        outputs = ",".join(dev.connected_outputs) if dev.connected_outputs else "none"
        parts = [drv, f"outputs={outputs}"]
        if dev.power_control is not None:
            parts.append(f"pm={dev.power_control}({dev.runtime_status or '?'})")
        if dev.dpm_force_level is not None:
            parts.append(f"dpm={dev.dpm_force_level}")
        if dev.power_profile_mode is not None:
            parts.append(f"profile={dev.power_profile_mode}")
        col.metric(dev.card, ", ".join(parts))
        pm_expected_on = gpu.gpu_pm_mode in (None, GPU_PM_MODE_ALWAYS) or (
            gpu.gpu_pm_mode == GPU_PM_MODE_GAME and gpu.game_profile_active is True
        )
        if dev.connected_outputs and dev.power_control == _GPU_RUNTIME_CONTROL_AUTO and pm_expected_on:
            col.warn_fix(
                f"gpu-runtime-pm-{dev.card}",
                (
                    f"Connected display GPU {dev.card} ({drv}, {outputs}) "
                    f"has power/control=auto (runtime_status={dev.runtime_status or '?'})"
                ),
                "Runtime PM auto can gate/sleep a display-driving GPU between compositor/game frames.",
                ("Force power/control=on for GPUs with active monitors.", "cst cpu install"),
            )
        if (
            dev.connected_outputs
            and gpu.game_profile_active is True
            and dev.power_profile_mode == _GPU_AMD_BOOTUP_PROFILE
            and dev.dpm_force_level == _GPU_RUNTIME_CONTROL_AUTO
        ):
            fix_cmd = (
                "cst cpu game off && cst cpu game on"
                if gpu.game_profile_manual
                else "sudo systemctl restart cpu-game.service"
            )
            col.warn_fix(
                f"gpu-game-profile-{dev.card}",
                f"Game mode active while {dev.card} remains on {dev.power_profile_mode}",
                "GPU stays on conservative desktop clock-ramp heuristics under active game session.",
                ("Switch active GPU pp_power_profile_mode to 3D_FULL_SCREEN / VR in game mode.", fix_cmd),
            )
    return col.report()


def evaluate_perf_snapshot(
    snapshot: PerfProbeSnapshot,
    axes: Sequence[PerfAxis] = ALL_PERF_AXES,
) -> PerfReport:
    """Evaluate requested responsiveness axes from an immutable `PerfProbeSnapshot`."""
    axis_reports: list[PerfAxisReport] = []
    for axis in axes:
        match axis:
            case PerfAxis.MEMORY:
                axis_reports.append(evaluate_perf_memory(snapshot.memory))
            case PerfAxis.CPU:
                axis_reports.append(evaluate_perf_cpu(snapshot.cpu))
            case PerfAxis.IO:
                axis_reports.append(evaluate_perf_io(snapshot.io))
            case PerfAxis.GPU:
                axis_reports.append(evaluate_perf_gpu(snapshot.gpu))
    all_findings = [f for rep in axis_reports for f in rep.findings]
    ranked = sorted(all_findings, key=lambda f: (-_VERDICT_RANK[f.verdict], f.axis.value, f.finding_id))
    return PerfReport(
        verdict=worst_perf_verdict(rep.verdict for rep in axis_reports),
        uptime_seconds=snapshot.uptime_seconds,
        axes=tuple(axis_reports),
        top_actions=tuple(ranked[:TOP_ACTION_LIMIT]),
    )


def _finding_to_json(finding: PerfFinding) -> PerfFindingJson:
    return PerfFindingJson(
        id=finding.finding_id,
        axis=finding.axis.value,
        verdict=finding.verdict.value,
        summary=finding.summary,
        detail=finding.detail,
        recommendation=finding.recommendation,
        fix_command=finding.fix_command,
    )


def perf_report_to_json(report: PerfReport) -> PerfReportJson:
    """Serialize `PerfReport` into a schema-typed JSON-ready dict."""
    return PerfReportJson(
        verdict=report.verdict.value,
        uptime_seconds=report.uptime_seconds,
        axes=[
            PerfAxisJson(
                axis=ax.axis.value,
                verdict=ax.verdict.value,
                metrics=[PerfMetricJson(label=m.label, value=m.value) for m in ax.metrics],
                findings=[_finding_to_json(f) for f in ax.findings],
            )
            for ax in report.axes
        ],
        top_actions=[_finding_to_json(f) for f in report.top_actions],
    )

# SPDX-License-Identifier: MPL-2.0
"""Pure models, constants, enums, and JSON schemas for `cst perf`."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypedDict

from cst_core.domain.values import CpuSet, GIB, MIB

KIB: Final = 1024
PERCENT_SCALE: Final = 100
EXIT_PERF_OK: Final = 0
EXIT_PERF_FINDINGS: Final = 1
EXIT_PERF_USAGE: Final = 2

MIN_UPTIME_FOR_RATE_SECONDS: Final = 600.0
LARGE_RAM_THRESHOLD_BYTES: Final = 32 * GIB
MEM_MIN_FREE_LARGE_RAM_BYTES: Final = 192 * MIB
MEM_MIN_FREE_SMALL_RAM_BYTES: Final = 32 * MIB
ALLOCSTALL_ATTENTION_PER_HOUR: Final = 5.0
ALLOCSTALL_BOTTLENECK_PER_HOUR: Final = 30.0
COMPACT_ATTENTION_PER_HOUR: Final = 10.0
COMPACT_BOTTLENECK_PER_HOUR: Final = 60.0
PSI_MEM_SOME_ATTENTION_PCT: Final = 1.0
PSI_MEM_SOME_BOTTLENECK_PCT: Final = 5.0
PSI_MEM_FULL_BOTTLENECK_PCT: Final = 1.0
MGLRU_RECOMMENDED_MIN_TTL_MS: Final = 1000
SESSION_MIN_RECOMMENDED_BYTES: Final = 512 * MIB
SESSION_FLOOR_HEADROOM_NUM: Final = 12
SESSION_FLOOR_HEADROOM_DEN: Final = 10

CSTATE_DEEP_LATENCY_US: Final = 200
IRQ_ACTIVE_MIN_COUNT: Final = 100_000
IRQ_DOMINANT_SHARE: Final = 0.5
TOP_IRQ_DISPLAY_LIMIT: Final = 3

NVME_PS_MAX_LATENCY_WARN_US: Final = 50_000
ROOT_BTRFS_TARGET_COMMIT_S: Final = 60
KERNEL_DEFAULT_BTRFS_COMMIT_S: Final = 30
CENTISECONDS_PER_SEC: Final = 100
BTRFS_MAX_COMMIT_ATTENTION_MS: Final = 500
BTRFS_MAX_COMMIT_BOTTLENECK_MS: Final = 2000
BTRFS_AVG_COMMIT_ATTENTION_MS: Final = 25.0
BTRFS_AVG_COMMIT_BOTTLENECK_MS: Final = 100.0
DEFAULT_CG_IO_WEIGHT: Final = 100
TOP_ACTION_LIMIT: Final = 6
MAX_BLOCK_DISPLAY: Final = 4
BTRFS_SHORT_UUID_LEN: Final = 8
PAIR_LEN: Final = 2
CPU_COL_PREFIX: Final = "CPU"
CPU_COL_PREFIX_LEN: Final = 3

PERF_USAGE_TEXT: Final = (
    "Usage: cst perf [--axis AXIS[,AXIS...]] [--compact] [--json] [-h|--help]\n"
    "\n"
    "Read-only responsiveness snapshot and objective bottleneck diagnosis across\n"
    "memory, cpu, io, and gpu.\n"
    "\n"
    "Options:\n"
    "  --axis AXIS   One or more axes: memory, cpu, io, gpu (repeatable or comma-separated)\n"
    "  --compact     One-screen verdict + findings summary (no raw metric tables)\n"
    "  --json        Emit machine-readable JSON report\n"
    "  -h, --help    Show this help message\n"
)


class PerfCliFlag(StrEnum):
    """Supported command-line flags for `cst perf`."""

    HELP_SHORT = "-h"
    HELP_LONG = "--help"
    JSON = "--json"
    COMPACT = "--compact"
    AXIS = "--axis"


class PerfAxis(StrEnum):
    """Responsiveness measurement axes."""

    MEMORY = "memory"
    CPU = "cpu"
    IO = "io"
    GPU = "gpu"


ALL_PERF_AXES: Final[tuple[PerfAxis, ...]] = (
    PerfAxis.MEMORY,
    PerfAxis.CPU,
    PerfAxis.IO,
    PerfAxis.GPU,
)


class PerfVerdict(StrEnum):
    """Objective health verdict ordered from best to worst."""

    SKIP = "SKIP"
    OPTIMIZED = "OPTIMIZED"
    ATTENTION = "ATTENTION"
    BOTTLENECK = "BOTTLENECK"


class PerfRootKind(StrEnum):
    """Logical filesystem roots resolved by adapters."""

    PROC = "proc"
    SYS = "sys"
    CGROUP = "cgroup"
    ETC = "etc"
    RUN = "run"
    DEV = "dev"
    ZSWAP_PARAMS = "zswap_params"


class PerfPathKey(StrEnum):
    """Logical file keys shared across adapters, doctor, swap status, and CPU status."""

    UPTIME = "proc/uptime"
    MEMINFO = "proc/meminfo"
    VMSTAT = "proc/vmstat"
    PSI_MEMORY = "proc/pressure/memory"
    VM_MIN_FREE = "proc/sys/vm/min_free_kbytes"
    VM_WSF = "proc/sys/vm/watermark_scale_factor"
    VM_SWAPPINESS = "proc/sys/vm/swappiness"
    VM_COMPACTION = "proc/sys/vm/compaction_proactiveness"
    VM_DIRTY_BG_BYTES = "proc/sys/vm/dirty_background_bytes"
    VM_DIRTY_BYTES = "proc/sys/vm/dirty_bytes"
    VM_DIRTY_WRITEBACK_CS = "proc/sys/vm/dirty_writeback_centisecs"
    VM_DIRTY_EXPIRE_CS = "proc/sys/vm/dirty_expire_centisecs"
    PROC_MOUNTS = "proc/mounts"
    THP_ENABLED = "sys/kernel/mm/transparent_hugepage/enabled"
    THP_DEFRAG = "sys/kernel/mm/transparent_hugepage/defrag"
    LRU_GEN_ENABLED = "sys/kernel/mm/lru_gen/enabled"
    LRU_GEN_MIN_TTL = "sys/kernel/mm/lru_gen/min_ttl_ms"
    ZSWAP_ENABLED = "zswap_params/enabled"
    ZSWAP_MAX_POOL = "zswap_params/max_pool_percent"
    ZSWAP_COMPRESSOR = "zswap_params/compressor"
    CPUIDLE_DRIVER = "sys/devices/system/cpu/cpuidle/current_driver"
    SCHED_PREEMPT = "sys/kernel/debug/sched/preempt"
    CMDLINE = "proc/cmdline"
    SCHED_EXT_STATE = "sys/kernel/sched_ext/state"
    SCHED_AUTOGROUP = "proc/sys/kernel/sched_autogroup_enabled"
    INTERRUPTS = "proc/interrupts"
    DEFAULT_SMP_AFFINITY = "proc/irq/default_smp_affinity"
    NVME_APST_LATENCY = "sys/module/nvme_core/parameters/default_ps_max_latency_us"
    IO_COST_QOS = "cgroup/io.cost.qos"
    CPU_CONF = "etc/cpu-setup/cpu-setup.conf"
    BTRFS_CONF = "etc/btrfs-setup/snapshots.conf"
    IRQBALANCE_CST_ENV = "etc/cpu-setup/irqbalance.env"
    IRQBALANCE_SYSCONFIG = "etc/sysconfig/irqbalance"


def perf_path_spec(key: PerfPathKey) -> tuple[PerfRootKind, str]:
    """Split a `PerfPathKey` into its `(PerfRootKind, relative_path)` pair."""
    root_name, rel = key.value.split("/", 1)
    return (PerfRootKind(root_name), rel)


MEMORY_FILE_KEYS: Final[tuple[PerfPathKey, ...]] = tuple(
    PerfPathKey(p)
    for p in (
        "proc/uptime",
        "proc/meminfo",
        "proc/vmstat",
        "proc/pressure/memory",
        "proc/sys/vm/min_free_kbytes",
        "proc/sys/vm/watermark_scale_factor",
        "proc/sys/vm/swappiness",
        "proc/sys/vm/compaction_proactiveness",
        "sys/kernel/mm/transparent_hugepage/enabled",
        "sys/kernel/mm/transparent_hugepage/defrag",
        "sys/kernel/mm/lru_gen/enabled",
        "sys/kernel/mm/lru_gen/min_ttl_ms",
        "zswap_params/enabled",
        "zswap_params/max_pool_percent",
        "zswap_params/compressor",
    )
)

CPU_FILE_KEYS: Final[tuple[PerfPathKey, ...]] = tuple(
    PerfPathKey(p)
    for p in (
        "sys/devices/system/cpu/cpuidle/current_driver",
        "sys/kernel/debug/sched/preempt",
        "proc/cmdline",
        "sys/kernel/sched_ext/state",
        "proc/sys/kernel/sched_autogroup_enabled",
        "proc/interrupts",
        "proc/irq/default_smp_affinity",
        "etc/cpu-setup/cpu-setup.conf",
        "etc/cpu-setup/irqbalance.env",
        "etc/sysconfig/irqbalance",
    )
)

IO_FILE_KEYS: Final[tuple[PerfPathKey, ...]] = tuple(
    PerfPathKey(p)
    for p in (
        "sys/module/nvme_core/parameters/default_ps_max_latency_us",
        "proc/sys/vm/dirty_background_bytes",
        "proc/sys/vm/dirty_bytes",
        "proc/sys/vm/dirty_writeback_centisecs",
        "proc/sys/vm/dirty_expire_centisecs",
        "proc/mounts",
        "cgroup/io.cost.qos",
        "etc/btrfs-setup/snapshots.conf",
    )
)


class PerfMetricJson(TypedDict):
    """JSON representation of a single key/value metric."""

    label: str
    value: str


class PerfFindingJson(TypedDict):
    """JSON representation of an actionable finding."""

    id: str
    axis: str
    verdict: str
    summary: str
    detail: str
    recommendation: str
    fix_command: str


class PerfAxisJson(TypedDict):
    """JSON representation of an evaluated axis."""

    axis: str
    verdict: str
    metrics: list[PerfMetricJson]
    findings: list[PerfFindingJson]


class PerfReportJson(TypedDict):
    """JSON representation of the full `cst perf` report."""

    verdict: str
    uptime_seconds: float | None
    axes: list[PerfAxisJson]
    top_actions: list[PerfFindingJson]


@dataclass(frozen=True, slots=True)
class PerfCliOptions:
    """Validated command-line options for `cst perf`."""

    axes: tuple[PerfAxis, ...]
    as_json: bool = False
    compact: bool = False
    help_mode: bool = False


class PerfCliError(ValueError):
    """Raised on invalid CLI flags or axis names."""


@dataclass(frozen=True, slots=True)
class CgroupMemorySlice:
    """Memory floor and usage sampled from one systemd cgroup slice."""

    name: str
    current_bytes: int
    min_bytes: int
    low_bytes: int
    parent_min_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class MemoryProbeSnapshot:
    """Raw memory, reclaim, zswap, THP, MGLRU, and cgroup measurements."""

    uptime_seconds: float | None = None
    mem_total_bytes: int | None = None
    mem_available_bytes: int | None = None
    min_free_bytes: int | None = None
    watermark_scale_factor: int | None = None
    swappiness: int | None = None
    compaction_proactiveness: int | None = None
    allocstall_count: int | None = None
    compact_stall_count: int | None = None
    workingset_refault_file: int | None = None
    thp_enabled: str | None = None
    thp_defrag: str | None = None
    lru_gen_enabled: str | None = None
    lru_gen_min_ttl_ms: int | None = None
    zswap_enabled: bool | None = None
    zswap_max_pool_percent: int | None = None
    zswap_compressor: str | None = None
    psi_some_avg60: float | None = None
    psi_full_avg60: float | None = None
    session_slices: tuple[CgroupMemorySlice, ...] = ()
    machine_memory_low_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class CpuIdleStateSample:
    """One cpuidle C-state reported under `cpu0/cpuidle/state*`."""

    name: str
    latency_us: int
    disabled: bool
    usage: int | None = None


@dataclass(frozen=True, slots=True)
class IrqLineSample:
    """One `/proc/interrupts` line with per-CPU counts and optional live effective affinity."""

    irq_id: str
    label: str
    counts_by_cpu: dict[int, int]
    effective_cpus: CpuSet | None = None

    @property
    def total_count(self) -> int:
        """Total interrupts summed across all CPUs."""
        return sum(self.counts_by_cpu.values())


@dataclass(frozen=True, slots=True)
class CpuProbeExtra:
    """Topology, runtime service, idle, and device flags combined into `CpuProbeSnapshot`."""

    idle_states: tuple[CpuIdleStateSample, ...] = ()
    preempt_unreadable: bool = False
    ntsync_present: bool | None = None
    build_cpus: CpuSet | None = None
    p_cores: CpuSet | None = None
    irqbalance_active: bool | None = None
    irqbalance_banned_cpus: CpuSet | None = None


@dataclass(frozen=True, slots=True)
class CpuProbeSnapshot:
    """Raw CPU idle, preemption, sched_ext, ntsync, and IRQ measurements."""

    cpuidle_driver: str | None = None
    idle_states: tuple[CpuIdleStateSample, ...] = ()
    preempt_mode: str | None = None
    preempt_unreadable: bool = False
    sched_ext_state: str | None = None
    autogroup_enabled: bool | None = None
    ntsync_present: bool | None = None
    build_cpus: CpuSet | None = None
    p_cores: CpuSet | None = None
    irqbalance_active: bool | None = None
    irqbalance_banned_cpus: CpuSet | None = None
    irq_lines: tuple[IrqLineSample, ...] = ()
    default_smp_affinity_hex: str | None = None


@dataclass(frozen=True, slots=True)
class BlockDeviceSample:
    """Queue and hardware attributes of one block device under `/sys/block`."""

    name: str
    scheduler: str | None
    rotational: bool | None


@dataclass(frozen=True, slots=True)
class BtrfsCommitSample:
    """Cumulative commit latency statistics for one mounted btrfs filesystem."""

    fs_id: str
    commits: int
    last_commit_ms: int
    max_commit_ms: int
    total_commit_ms: int


@dataclass(frozen=True, slots=True)
class IoProbeSnapshot:
    """Raw block scheduler, blk-iocost, NVMe APST, dirty writeback, and btrfs telemetry."""

    block_devices: tuple[BlockDeviceSample, ...] = ()
    nvme_ps_max_latency_us: int | None = None
    dirty_background_bytes: int | None = None
    dirty_bytes: int | None = None
    dirty_writeback_cs: int | None = None
    dirty_expire_cs: int | None = None
    root_btrfs_commit_s: int | None = None
    root_btrfs_commit_explicit: bool = False
    root_btrfs_flushoncommit: bool = False
    iocost_qos_active: bool | None = None
    iocost_enabled_devices: int = 0
    nonzero_slice_io_weights: tuple[tuple[str, int], ...] = ()
    btrfs_commits: tuple[BtrfsCommitSample, ...] = ()
    btrfs_scrub_limit: str | None = None
    btrfs_conf_present: bool = False


@dataclass(frozen=True, slots=True)
class GpuDeviceSample:
    """Power/DPM/connector state of one DRM card under `/sys/class/drm`."""

    card: str
    driver: str | None
    pci_slot: str | None
    connected_outputs: tuple[str, ...]
    power_control: str | None = None
    runtime_status: str | None = None
    dpm_force_level: str | None = None
    power_profile_mode: str | None = None


@dataclass(frozen=True, slots=True)
class GpuProbeSnapshot:
    """Raw GPU telemetry and game profile marker state."""

    gpus: tuple[GpuDeviceSample, ...] = ()
    game_profile_active: bool | None = None


@dataclass(frozen=True, slots=True)
class PerfProbeSnapshot:
    """Complete raw snapshot across all four responsiveness axes."""

    uptime_seconds: float | None = None
    memory: MemoryProbeSnapshot = MemoryProbeSnapshot()
    cpu: CpuProbeSnapshot = CpuProbeSnapshot()
    io: IoProbeSnapshot = IoProbeSnapshot()
    gpu: GpuProbeSnapshot = GpuProbeSnapshot()


@dataclass(frozen=True, slots=True)
class PerfMetric:
    """One formatted metric displayed for an axis."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class PerfFinding:
    """One objective responsiveness diagnosis finding."""

    finding_id: str
    axis: PerfAxis
    verdict: PerfVerdict
    summary: str
    detail: str
    recommendation: str = ""
    fix_command: str = ""


@dataclass(frozen=True, slots=True)
class PerfAxisReport:
    """Evaluated verdict, key metrics, and findings for one axis."""

    axis: PerfAxis
    verdict: PerfVerdict
    metrics: tuple[PerfMetric, ...]
    findings: tuple[PerfFinding, ...]


@dataclass(frozen=True, slots=True)
class PerfReport:
    """Evaluated report across all requested axes."""

    verdict: PerfVerdict
    uptime_seconds: float | None
    axes: tuple[PerfAxisReport, ...]
    top_actions: tuple[PerfFinding, ...]

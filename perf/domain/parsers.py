# SPDX-License-Identifier: MPL-2.0
"""Pure CLI flag, procfs/sysfs parsers, and snapshot assemblers for `perf`."""

from collections.abc import Mapping, Sequence
import re
from typing import Final

from cst_core.domain.text import enum_member, group, strip_ascii_blanks
from cst_core.domain.values import CpuSet, parse_integer, parse_whole_number
from perf.domain.models import (
    ALL_PERF_AXES,
    BlockDeviceSample,
    BtrfsCommitSample,
    CPU_COL_PREFIX,
    CPU_COL_PREFIX_LEN,
    CgroupMemorySlice,
    CpuProbeExtra,
    CpuProbeSnapshot,
    IoProbeSnapshot,
    IrqLineSample,
    KIB,
    MemoryProbeSnapshot,
    PAIR_LEN,
    PerfAxis,
    PerfCliError,
    PerfCliFlag,
    PerfCliOptions,
    PerfPathKey,
)

_BRACKET_RE: Final = re.compile(r"\[([^\]]+)\]")
_AMD_PROFILE_ACTIVE_RE: Final = re.compile(r"^\s*\d+\s+([A-Za-z0-9_-]+)\*\s*:?")
_PREEMPT_TOKEN_RE: Final = re.compile(r"\bpreempt=([a-z]+)\b")
_ALLOCSTALL_PREFIX: Final = "allocstall_"
_FLUSHONCOMMIT_OPT: Final = "flushoncommit"
_DEFAULT_BTRFS_COMMIT_S: Final = 30
_COMMIT_OPT_PREFIX: Final = "commit="
_ROOT_BTRFS_PRIORITY: Final = ("/var", "/var/home", "/sysroot", "/")
_PROC_MOUNTS_FIELDS: Final = 4
_BOOL_TRUE_TOKENS: Final[frozenset[str]] = frozenset({"Y", "1", "TRUE", "YES"})
_BOOL_FALSE_TOKENS: Final[frozenset[str]] = frozenset({"N", "0", "FALSE", "NO"})
_COLON: Final = ":"
_AVG60_PREFIX: Final = "avg60="
_PSI_SOME: Final = "some"
_PSI_FULL: Final = "full"
_DRIVER_KEY: Final = "DRIVER"
_PCI_SLOT_KEY: Final = "PCI_SLOT_NAME"
_CARD_PREFIX: Final = "card"
_DRM_CONNECTED: Final = "connected"
_TMPFILES_SYS_WRITE_RE: Final = re.compile(r"^w\s+(/sys/\S+)\s+(?:\S+\s+){4}(\S+)")


def clean_opt_str(text: str | None) -> str | None:
    """Strip optional string and normalize blank values to `None`."""
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def parse_perf_args(argv: Sequence[str]) -> PerfCliOptions:
    """Parse `cst perf` arguments or raise `PerfCliError`."""
    selected: list[PerfAxis] = []
    as_json = False
    compact = False
    axis_eq_prefix = f"{PerfCliFlag.AXIS.value}="
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg.startswith(axis_eq_prefix):
            selected.extend(_parse_axis_list(arg.split("=", 1)[1]))
            i += 1
            continue
        match enum_member(PerfCliFlag, arg):
            case PerfCliFlag.HELP_SHORT | PerfCliFlag.HELP_LONG:
                return PerfCliOptions(axes=ALL_PERF_AXES, help_mode=True)
            case PerfCliFlag.JSON:
                as_json = True
            case PerfCliFlag.COMPACT:
                compact = True
            case PerfCliFlag.AXIS:
                i += 1
                if i >= len(argv):
                    raise PerfCliError("missing value after --axis")
                selected.extend(_parse_axis_list(argv[i]))
            case None:
                raise PerfCliError(f"unknown argument: {arg}")
        i += 1
    deduped = tuple(dict.fromkeys(selected)) if selected else ALL_PERF_AXES
    return PerfCliOptions(axes=deduped, as_json=as_json, compact=compact, help_mode=False)


def _parse_axis_list(spec: str) -> list[PerfAxis]:
    axes: list[PerfAxis] = []
    for raw in spec.split(","):
        token = strip_ascii_blanks(raw).lower()  # a no-break space is part of the word
        if not token:
            continue
        parsed = enum_member(PerfAxis, token)
        if parsed is None:
            valid = ", ".join(axis.value for axis in ALL_PERF_AXES)
            raise PerfCliError(f"unknown axis {token!r} (choose from {valid})")
        axes.append(parsed)
    if not axes:
        raise PerfCliError("empty --axis value")
    return axes


def parse_proc_kib_table(text: str | None) -> dict[str, int]:
    """Parse `/proc/meminfo`-style `Key: <N> kB` text into `{key: kib}`."""
    result: dict[str, int] = {}
    if text is None:
        return result
    for line in text.splitlines():
        if _COLON not in line:
            continue
        key, rest = line.split(_COLON, 1)
        fields = rest.split()
        kib = parse_whole_number(fields[0]) if fields else None  # ASCII digits within int()'s limit
        if kib is not None:
            result[key.strip()] = kib
    return result


def parse_proc_space_kv(text: str | None) -> dict[str, int]:
    """Parse `/proc/vmstat` or `commit_stats`-style `<key> <int>` lines."""
    result: dict[str, int] = {}
    if text is None:
        return result
    for line in text.splitlines():
        parts = line.split()
        value = parse_integer(parts[1]) if len(parts) == PAIR_LEN else None  # ASCII, within int()'s limit
        if value is not None:
            result[parts[0]] = value
    return result


def parse_bracket_choice(text: str | None) -> str | None:
    """Return the active `[bracketed]` choice from sysfs attribute text."""
    stripped = clean_opt_str(text)
    if stripped is None:
        return None
    match = _BRACKET_RE.search(stripped)
    if match is not None:
        return group(match, 1)
    parts = stripped.split()
    return parts[0] if len(parts) == 1 else None


def parse_uptime_seconds(text: str | None) -> float | None:
    """Parse the first float from `/proc/uptime`."""
    cleaned = clean_opt_str(text)
    if cleaned is None:
        return None
    fields = cleaned.split()
    try:
        value = float(fields[0])
    except ValueError:
        return None
    return value if value >= 0.0 else None


def parse_preempt_mode(debugfs_text: str | None, cmdline_text: str | None) -> str | None:
    """Extract preemption mode from debugfs `sched/preempt` or kernel cmdline."""
    bracketed = parse_bracket_choice(debugfs_text)
    if bracketed is not None:
        return bracketed
    cleaned_cmd = clean_opt_str(cmdline_text)
    if cleaned_cmd is not None:
        match = _PREEMPT_TOKEN_RE.search(cleaned_cmd)
        if match is not None:
            return group(match, 1)
    return None


def parse_psi_avg60(text: str | None) -> tuple[float | None, float | None]:
    """Return `(some_avg60, full_avg60)` from `/proc/pressure/memory` text."""
    some_val: float | None = None
    full_val: float | None = None
    if text is None:
        return (None, None)
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        avg60 = _extract_avg60(parts[1:])
        if parts[0] == _PSI_SOME:
            some_val = avg60
        elif parts[0] == _PSI_FULL:
            full_val = avg60
    return (some_val, full_val)


def _extract_avg60(tokens: Sequence[str]) -> float | None:
    for token in tokens:
        if token.startswith(_AVG60_PREFIX):
            try:
                return float(token.split("=", 1)[1])
            except ValueError:
                return None
    return None


def parse_amd_active_dpm_profile(text: str | None) -> str | None:
    """Extract active `*` profile name from amdgpu `pp_power_profile_mode`."""
    if text is None:
        return None
    for line in text.splitlines():
        match = _AMD_PROFILE_ACTIVE_RE.match(line)
        if match is not None:
            return group(match, 1)
    return None


def parse_btrfs_commit_stats(fs_id: str, text: str | None) -> BtrfsCommitSample | None:
    """Parse `/sys/fs/btrfs/<uuid>/commit_stats` into a `BtrfsCommitSample`."""
    kv = parse_proc_space_kv(text)
    if not kv or "commits" not in kv:
        return None
    return BtrfsCommitSample(
        fs_id=fs_id,
        commits=kv["commits"],
        last_commit_ms=kv.get("last_commit_ms", 0),
        max_commit_ms=kv.get("max_commit_ms", 0),
        total_commit_ms=kv.get("total_commit_ms", 0),
    )


def parse_proc_interrupts(text: str | None) -> tuple[IrqLineSample, ...]:
    """Parse numeric IRQ lines with per-CPU counters from `/proc/interrupts`."""
    if text is None:
        return ()
    lines = text.splitlines()
    if not lines:
        return ()
    cpu_indices = [
        index
        for tok in lines[0].split()
        if tok.startswith(CPU_COL_PREFIX) and (index := parse_whole_number(tok[CPU_COL_PREFIX_LEN:])) is not None
    ]
    if not cpu_indices:
        return ()
    samples: list[IrqLineSample] = []
    for line in lines[1:]:
        sample = _parse_interrupt_line(line, cpu_indices)
        if sample is not None:
            samples.append(sample)
    return tuple(samples)


def _parse_interrupt_line(line: str, cpu_indices: Sequence[int]) -> IrqLineSample | None:
    if _COLON not in line:
        return None
    prefix, rest = line.split(_COLON, 1)
    irq_id = prefix.strip()
    if parse_whole_number(irq_id) is None:  # a numbered IRQ: ASCII digits (NMI, LOC and "²" are none)
        return None
    fields = rest.split()
    n_cpus = len(cpu_indices)
    if len(fields) < n_cpus:
        return None
    counts: dict[int, int] = {}
    for idx, cpu in enumerate(cpu_indices):
        val = parse_whole_number(fields[idx])  # ASCII digits within int()'s limit, else no line
        if val is None:
            return None
        if val:
            counts[cpu] = val
    return IrqLineSample(irq_id=irq_id, label=" ".join(fields[n_cpus:]), counts_by_cpu=counts)


def parse_uevent_fields(text: str | None) -> tuple[str | None, str | None]:
    """Extract `(driver, pci_slot)` from `/sys/class/drm/card*/device/uevent`."""
    driver: str | None = None
    pci_slot: str | None = None
    if text is None:
        return (None, None)
    for line in text.splitlines():
        if line.startswith(f"{_DRIVER_KEY}="):
            driver = clean_opt_str(line.split("=", 1)[1])
        elif line.startswith(f"{_PCI_SLOT_KEY}="):
            pci_slot = clean_opt_str(line.split("=", 1)[1])
    return (driver, pci_slot)


def parse_optional_int(text: str | None) -> int | None:
    """Parse trimmed integer sysfs/procfs text, returning `None` if absent/invalid."""
    stripped = clean_opt_str(text)
    return None if stripped is None else parse_integer(stripped)  # ASCII, within int()'s limit


def parse_optional_cpuset(spec: str | None) -> CpuSet | None:
    """Parse comma/range CPU list string if valid and non-empty. ASCII blanks around it are dropped, as
    CpuSet.parse drops them; a no-break space is part of the list (an invalid one), as cpu irq and vm-pin
    read BUILD_CPUS and P_CORES."""
    if spec is None:
        return None
    cleaned = strip_ascii_blanks(spec)
    if cleaned == "":
        return None
    try:
        return CpuSet.parse(cleaned)
    except ValueError:
        return None


def parse_optional_bool_flag(text: str | None) -> bool | None:
    """Parse sysfs `Y`/`N`/`1`/`0` boolean parameter text."""
    cleaned = clean_opt_str(text)
    if cleaned is None:
        return None
    token = cleaned.upper()
    if token in _BOOL_TRUE_TOKENS:
        return True
    if token in _BOOL_FALSE_TOKENS:
        return False
    return None


def parse_kib_sysctl(text: str | None) -> int | None:
    """Parse a `/proc/sys/vm/*` KiB integer into bytes."""
    kib = parse_optional_int(text)
    return kib * KIB if kib is not None else None


def parse_sysfs_tmpfiles_writes(text: str) -> list[tuple[str, str]]:
    """Extract `(/sys/..., value)` pairs from a systemd-tmpfiles `w` snippet."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _TMPFILES_SYS_WRITE_RE.match(line.strip())
        if match is not None:
            out.append((group(match, 1), group(match, 2)))
    return out


def is_drm_card_name(name: str) -> bool:
    """Return True for root DRM card directories like `card0` (not connectors)."""
    return name.startswith(_CARD_PREFIX) and "-" not in name and name[len(_CARD_PREFIX) :].isdigit()


def is_connected_drm_status(text: str | None) -> bool:
    """Return True when DRM connector status file equals `connected`."""
    return clean_opt_str(text) == _DRM_CONNECTED


def assemble_memory_probe(
    files: Mapping[PerfPathKey, str | None],
    *,
    session_slices: Sequence[CgroupMemorySlice] = (),
    machine_memory_low_bytes: int | None = None,
) -> MemoryProbeSnapshot:
    """Build `MemoryProbeSnapshot` from raw text keyed by `PerfPathKey`."""
    mem = parse_proc_kib_table(files.get(PerfPathKey.MEMINFO))
    vmstat = parse_proc_space_kv(files.get(PerfPathKey.VMSTAT))
    has_allocstall = any(key.startswith(_ALLOCSTALL_PREFIX) for key in vmstat)
    allocstall = (
        sum(val for key, val in vmstat.items() if key.startswith(_ALLOCSTALL_PREFIX)) if has_allocstall else None
    )
    some_avg60, full_avg60 = parse_psi_avg60(files.get(PerfPathKey.PSI_MEMORY))
    return MemoryProbeSnapshot(
        uptime_seconds=parse_uptime_seconds(files.get(PerfPathKey.UPTIME)),
        mem_total_bytes=mem["MemTotal"] * KIB if "MemTotal" in mem else None,
        mem_available_bytes=mem["MemAvailable"] * KIB if "MemAvailable" in mem else None,
        min_free_bytes=parse_kib_sysctl(files.get(PerfPathKey.VM_MIN_FREE)),
        watermark_scale_factor=parse_optional_int(files.get(PerfPathKey.VM_WSF)),
        swappiness=parse_optional_int(files.get(PerfPathKey.VM_SWAPPINESS)),
        compaction_proactiveness=parse_optional_int(files.get(PerfPathKey.VM_COMPACTION)),
        allocstall_count=allocstall,
        compact_stall_count=vmstat.get("compact_stall"),
        workingset_refault_file=vmstat.get("workingset_refault_file"),
        thp_enabled=parse_bracket_choice(files.get(PerfPathKey.THP_ENABLED)),
        thp_defrag=parse_bracket_choice(files.get(PerfPathKey.THP_DEFRAG)),
        lru_gen_enabled=clean_opt_str(files.get(PerfPathKey.LRU_GEN_ENABLED)),
        lru_gen_min_ttl_ms=parse_optional_int(files.get(PerfPathKey.LRU_GEN_MIN_TTL)),
        zswap_enabled=parse_optional_bool_flag(files.get(PerfPathKey.ZSWAP_ENABLED)),
        zswap_max_pool_percent=parse_optional_int(files.get(PerfPathKey.ZSWAP_MAX_POOL)),
        zswap_compressor=clean_opt_str(files.get(PerfPathKey.ZSWAP_COMPRESSOR)),
        psi_some_avg60=some_avg60,
        psi_full_avg60=full_avg60,
        session_slices=tuple(session_slices),
        machine_memory_low_bytes=machine_memory_low_bytes,
    )


def assemble_cpu_probe(
    files: Mapping[PerfPathKey, str | None],
    extra: CpuProbeExtra | None = None,
) -> CpuProbeSnapshot:
    """Build `CpuProbeSnapshot` from raw text keyed by `PerfPathKey` and from `extra`.

    The IRQ lines come only from `extra.irq_lines` (the probe reads them with each IRQ's effective
    affinity): `files[PerfPathKey.INTERRUPTS]` is not read here, and without `extra` the snapshot has none.
    """
    meta = CpuProbeExtra() if extra is None else extra
    return CpuProbeSnapshot(
        cpuidle_driver=clean_opt_str(files.get(PerfPathKey.CPUIDLE_DRIVER)),
        idle_states=meta.idle_states,
        cstate_setting=meta.cstate_setting,
        preempt_mode=parse_preempt_mode(files.get(PerfPathKey.SCHED_PREEMPT), files.get(PerfPathKey.CMDLINE)),
        preempt_unreadable=meta.preempt_unreadable,
        sched_ext_state=clean_opt_str(files.get(PerfPathKey.SCHED_EXT_STATE)),
        autogroup_enabled=parse_optional_bool_flag(files.get(PerfPathKey.SCHED_AUTOGROUP)),
        ntsync_present=meta.ntsync_present,
        build_cpus=meta.build_cpus,
        p_cores=meta.p_cores,
        online_cpus=meta.online_cpus,
        irqbalance_active=meta.irqbalance_active,
        irqbalance_banned_cpus=meta.irqbalance_banned_cpus,
        irq_lines=meta.irq_lines,
        default_smp_affinity_hex=clean_opt_str(files.get(PerfPathKey.DEFAULT_SMP_AFFINITY)),
    )


def count_iocost_enabled(text: str) -> int:
    """Count `/sys/fs/cgroup/io.cost.qos` device lines where `enable=1`."""
    enabled = 0
    for line in text.splitlines():
        parts = line.split()
        if parts and ":" in parts[0] and "enable=1" in parts[1:]:
            enabled += 1
    return enabled


def _parse_root_btrfs_opts(proc_mounts_text: str) -> tuple[int, bool, bool] | None:
    by_target: dict[str, str] = {}
    for line in proc_mounts_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) >= _PROC_MOUNTS_FIELDS and fields[2] == "btrfs" and fields[1] in _ROOT_BTRFS_PRIORITY:
            by_target[fields[1]] = fields[3]
    raw_opts = next((by_target[t] for t in _ROOT_BTRFS_PRIORITY if t in by_target), None)
    if raw_opts is None:
        return None
    commit_vals = [
        parsed
        for tok in raw_opts.split(",")
        if tok.startswith(_COMMIT_OPT_PREFIX)
        and (parsed := parse_whole_number(tok.removeprefix(_COMMIT_OPT_PREFIX))) is not None
        and parsed > 0
    ]
    explicit = bool(commit_vals)
    commit_s = commit_vals[-1] if explicit else _DEFAULT_BTRFS_COMMIT_S
    return commit_s, explicit, (_FLUSHONCOMMIT_OPT in raw_opts.split(","))


def assemble_io_probe(
    files: Mapping[PerfPathKey, str | None],
    *,
    block_devices: Sequence[BlockDeviceSample] = (),
    nonzero_slice_io_weights: Sequence[tuple[str, int]] = (),
    btrfs_commits: Sequence[BtrfsCommitSample] = (),
    btrfs_scrub_limit: str | None = None,
) -> IoProbeSnapshot:
    """Build `IoProbeSnapshot` cleanly from raw text keyed by `PerfPathKey`."""
    qos_text = files.get(PerfPathKey.IO_COST_QOS)
    iocost_enabled = count_iocost_enabled(qos_text) if qos_text is not None else 0
    iocost_active: bool | None = (iocost_enabled > 0) if qos_text is not None else None
    btrfs_conf = files.get(PerfPathKey.BTRFS_CONF)
    parsed_root = _parse_root_btrfs_opts(files.get(PerfPathKey.PROC_MOUNTS) or "")
    root_commit_s, root_commit_explicit, root_flushoncommit = (
        parsed_root if parsed_root is not None else (None, False, False)
    )
    return IoProbeSnapshot(
        block_devices=tuple(block_devices),
        nvme_ps_max_latency_us=parse_optional_int(files.get(PerfPathKey.NVME_APST_LATENCY)),
        dirty_background_bytes=parse_optional_int(files.get(PerfPathKey.VM_DIRTY_BG_BYTES)),
        dirty_bytes=parse_optional_int(files.get(PerfPathKey.VM_DIRTY_BYTES)),
        dirty_writeback_cs=parse_optional_int(files.get(PerfPathKey.VM_DIRTY_WRITEBACK_CS)),
        dirty_expire_cs=parse_optional_int(files.get(PerfPathKey.VM_DIRTY_EXPIRE_CS)),
        root_btrfs_commit_s=root_commit_s,
        root_btrfs_commit_explicit=root_commit_explicit,
        root_btrfs_flushoncommit=root_flushoncommit,
        iocost_qos_active=iocost_active,
        iocost_enabled_devices=iocost_enabled,
        nonzero_slice_io_weights=tuple(nonzero_slice_io_weights),
        btrfs_commits=tuple(btrfs_commits),
        btrfs_scrub_limit=btrfs_scrub_limit,
        btrfs_conf_present=btrfs_conf is not None,
    )

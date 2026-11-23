# SPDX-License-Identifier: MPL-2.0
"""Application use cases and shared collector for `cst perf`."""

from collections.abc import Sequence
import posixpath
from typing import Final

from cst_core.domain.text import parse_shell_assignments, parse_snapshots_conf
from dataclasses import replace

from perf.domain.evaluate import evaluate_perf_snapshot
from perf.domain.models import (
    IRQ_ACTIVE_MIN_COUNT,
    ALL_PERF_AXES,
    BlockDeviceSample,
    BtrfsCommitSample,
    CPU_FILE_KEYS,
    CgroupMemorySlice,
    CpuIdleStateSample,
    CpuProbeExtra,
    CpuProbeSnapshot,
    GpuDeviceSample,
    GpuProbeSnapshot,
    IO_FILE_KEYS,
    IoProbeSnapshot,
    MEMORY_FILE_KEYS,
    MemoryProbeSnapshot,
    PerfAxis,
    PerfPathKey,
    PerfProbeSnapshot,
    PerfReport,
    PerfRootKind,
)
from perf.domain.parsers import (
    assemble_cpu_probe,
    assemble_io_probe,
    assemble_memory_probe,
    clean_opt_str,
    is_connected_drm_status,
    is_drm_card_name,
    parse_amd_active_dpm_profile,
    parse_bracket_choice,
    parse_btrfs_commit_stats,
    parse_optional_bool_flag,
    parse_optional_cpuset,
    parse_optional_int,
    parse_uevent_fields,
    parse_uptime_seconds,
)
from perf.ports import PerfFsReader, PerfProbePort

_CG_USER_SLICE: Final = "user.slice"
_CG_MACHINE_SLICE: Final = "machine.slice"
_CG_SESSION_SLICE: Final = "session.slice"
_CG_SLICE_WEIGHT_CANDIDATES: Final[tuple[str, ...]] = (
    "machine.slice",
    "builds.slice",
    "user.slice",
    "app.slice",
)
_USER_UID_PREFIX: Final = "user-"
_USER_SERVICE_FMT: Final = "user@{uid}.service"
_SLICE_SUFFIX: Final = ".slice"
_STATE_PREFIX: Final = "state"
_BLOCK_SKIP_PREFIXES: Final[tuple[str, ...]] = ("loop", "zram", "dm-", "md", "ram", "sr")
_IRQBALANCE_INVOCATION: Final = "systemd/units/invocation:irqbalance.service"
_IRQBALANCE_CST_DROPIN: Final = "systemd/system/irqbalance.service.d/10-cst-ban.conf"
_SYSTEMD_UNITS_DIR: Final = "systemd/units"
_NTSYNC_DEV: Final = "ntsync"
_NTSYNC_CLASS: Final = "class/misc/ntsync"
_NTSYNC_MODULE: Final = "module/ntsync"
_GAME_STATE_RUN: Final = "cpu-setup/game.state"
_GAME_MANUAL_RUN: Final = "cpu-setup/manual"


def build_perf_report(
    probe: PerfProbePort,
    axes: Sequence[PerfAxis] = ALL_PERF_AXES,
) -> PerfReport:
    """Collect host metrics via `probe` and produce an objective `PerfReport`."""
    snapshot = probe.collect_snapshot(axes)
    return evaluate_perf_snapshot(snapshot, axes)


def _read_key_map(reader: PerfFsReader, keys: Sequence[PerfPathKey]) -> dict[PerfPathKey, str | None]:
    return {key: reader.read_key(key) for key in keys}


def collect_memory_from_reader(reader: PerfFsReader) -> MemoryProbeSnapshot:
    """Collect memory, reclaim, THP, MGLRU, zswap, PSI, and cgroup metrics cleanly."""
    files = _read_key_map(reader, MEMORY_FILE_KEYS)
    user_base_min = parse_optional_int(
        reader.read_rel(PerfRootKind.CGROUP, posixpath.join(_CG_USER_SLICE, "memory.min"))
    )
    session_slices: list[CgroupMemorySlice] = []
    for entry in reader.list_rel(PerfRootKind.CGROUP, _CG_USER_SLICE):
        if not (entry.startswith(_USER_UID_PREFIX) and entry.endswith(_SLICE_SUFFIX)):
            continue
        uid_part = entry[len(_USER_UID_PREFIX) : -len(_SLICE_SUFFIX)]
        if not uid_part.isdigit():
            continue
        user_rel = posixpath.join(_CG_USER_SLICE, entry)
        service_rel = posixpath.join(user_rel, _USER_SERVICE_FMT.format(uid=uid_part))
        sess_rel = posixpath.join(service_rel, _CG_SESSION_SLICE)
        if not reader.is_dir_rel(PerfRootKind.CGROUP, sess_rel):
            continue
        sess_cur = parse_optional_int(reader.read_rel(PerfRootKind.CGROUP, posixpath.join(sess_rel, "memory.current")))
        sess_min = parse_optional_int(reader.read_rel(PerfRootKind.CGROUP, posixpath.join(sess_rel, "memory.min")))
        sess_low = parse_optional_int(reader.read_rel(PerfRootKind.CGROUP, posixpath.join(sess_rel, "memory.low")))
        user_uid_min = parse_optional_int(reader.read_rel(PerfRootKind.CGROUP, posixpath.join(user_rel, "memory.min")))
        service_min = parse_optional_int(
            reader.read_rel(PerfRootKind.CGROUP, posixpath.join(service_rel, "memory.min"))
        )
        ancestors = [val for val in (user_base_min, user_uid_min, service_min) if val is not None]
        parent_min = min(ancestors) if ancestors else None
        if sess_cur is not None and sess_min is not None:
            session_slices.append(
                CgroupMemorySlice(
                    name=f"{entry}/session.slice",
                    current_bytes=sess_cur,
                    min_bytes=sess_min,
                    low_bytes=sess_low or 0,
                    parent_min_bytes=parent_min,
                    uid=int(uid_part),
                )
            )
    machine_low = parse_optional_int(
        reader.read_rel(PerfRootKind.CGROUP, posixpath.join(_CG_MACHINE_SLICE, "memory.low"))
    )
    return assemble_memory_probe(files, session_slices=session_slices, machine_memory_low_bytes=machine_low)


def _collect_idle_states(reader: PerfFsReader) -> tuple[CpuIdleStateSample, ...]:
    base = "devices/system/cpu/cpu0/cpuidle"
    samples: list[CpuIdleStateSample] = []
    for entry in reader.list_rel(PerfRootKind.SYS, base):
        if not (entry.startswith(_STATE_PREFIX) and entry[len(_STATE_PREFIX) :].isdigit()):
            continue
        state_rel = posixpath.join(base, entry)
        raw_name = reader.read_rel(PerfRootKind.SYS, posixpath.join(state_rel, "name"))
        name = clean_opt_str(raw_name) or entry
        latency = parse_optional_int(reader.read_rel(PerfRootKind.SYS, posixpath.join(state_rel, "latency")))
        if latency is None:
            continue
        disabled = bool(
            parse_optional_int(reader.read_rel(PerfRootKind.SYS, posixpath.join(state_rel, "disable"))) or 0
        )
        usage = parse_optional_int(reader.read_rel(PerfRootKind.SYS, posixpath.join(state_rel, "usage")))
        samples.append(CpuIdleStateSample(name=name, latency_us=latency, disabled=disabled, usage=usage))
    return tuple(samples)


def collect_cpu_from_reader(reader: PerfFsReader) -> CpuProbeSnapshot:
    """Collect cpuidle, preempt, sched_ext, ntsync, irqbalance, and IRQ counts cleanly."""
    files = _read_key_map(reader, CPU_FILE_KEYS)
    preempt_text, preempt_unreadable = reader.read_preempt()
    files[PerfPathKey.SCHED_PREEMPT] = preempt_text
    conf_text = files.get(PerfPathKey.CPU_CONF)
    build_cpus = None
    p_cores = None
    cstate_setting = ""
    if conf_text is not None:
        assigned = parse_shell_assignments(conf_text).values
        build_cpus = parse_optional_cpuset(assigned.get("BUILD_CPUS"))
        p_cores = parse_optional_cpuset(assigned.get("P_CORES"))
        cstate_setting = assigned.get("CPU_CSTATE_MAX_LATENCY_US", "")
    banned_cpus = None
    cst_dropin_present = reader.exists_rel(PerfRootKind.ETC, _IRQBALANCE_CST_DROPIN)
    cst_irq_env = files.get(PerfPathKey.IRQBALANCE_CST_ENV) if cst_dropin_present else None
    cst_assigned = parse_shell_assignments(cst_irq_env).values if cst_irq_env is not None else {}
    if "IRQBALANCE_BANNED_CPULIST" in cst_assigned:
        banned_cpus = parse_optional_cpuset(cst_assigned.get("IRQBALANCE_BANNED_CPULIST"))
    else:
        irq_sysconf = files.get(PerfPathKey.IRQBALANCE_SYSCONFIG)
        if irq_sysconf is not None:
            banned_cpus = parse_optional_cpuset(
                parse_shell_assignments(irq_sysconf).values.get("IRQBALANCE_BANNED_CPULIST")
            )
    online_cpus = parse_optional_cpuset(files.get(PerfPathKey.CPU_ONLINE))
    irqbalance_active: bool | None = None
    if reader.is_dir_rel(PerfRootKind.RUN, _SYSTEMD_UNITS_DIR):
        irqbalance_active = reader.exists_rel(PerfRootKind.RUN, _IRQBALANCE_INVOCATION)
    ntsync_present: bool | None = None
    if reader.is_dir_rel(PerfRootKind.SYS, "class"):
        ntsync_present = (
            reader.exists_rel(PerfRootKind.DEV, _NTSYNC_DEV)
            or reader.is_dir_rel(PerfRootKind.SYS, _NTSYNC_CLASS)
            or reader.is_dir_rel(PerfRootKind.SYS, _NTSYNC_MODULE)
        )
    snap = assemble_cpu_probe(
        files,
        CpuProbeExtra(
            idle_states=_collect_idle_states(reader),
            cstate_setting=cstate_setting,
            preempt_unreadable=preempt_unreadable,
            ntsync_present=ntsync_present,
            build_cpus=build_cpus,
            p_cores=p_cores,
            online_cpus=online_cpus,
            irqbalance_active=irqbalance_active,
            irqbalance_banned_cpus=banned_cpus,
        ),
    )
    if not snap.irq_lines:
        return snap
    enriched = tuple(
        replace(
            line,
            effective_cpus=parse_optional_cpuset(
                reader.read_rel(PerfRootKind.PROC, posixpath.join("irq", line.irq_id, "effective_affinity_list"))
            ),
        )
        if line.irq_id.isdigit() and line.total_count >= IRQ_ACTIVE_MIN_COUNT
        else line
        for line in snap.irq_lines
    )
    return replace(snap, irq_lines=enriched)


def collect_io_from_reader(reader: PerfFsReader) -> IoProbeSnapshot:
    """Collect block schedulers, io.weight vs blk-iocost, NVMe APST, dirty sysctls, and btrfs commits."""
    files = _read_key_map(reader, IO_FILE_KEYS)
    blocks: list[BlockDeviceSample] = []
    for dev in reader.list_rel(PerfRootKind.SYS, "block"):
        if dev.startswith(_BLOCK_SKIP_PREFIXES):
            continue
        sched = parse_bracket_choice(reader.read_rel(PerfRootKind.SYS, posixpath.join("block", dev, "queue/scheduler")))
        rot = parse_optional_bool_flag(
            reader.read_rel(PerfRootKind.SYS, posixpath.join("block", dev, "queue/rotational"))
        )
        blocks.append(BlockDeviceSample(name=dev, scheduler=sched, rotational=rot))
    weights: list[tuple[str, int]] = []
    for sl in _CG_SLICE_WEIGHT_CANDIDATES:
        raw_w = clean_opt_str(reader.read_rel(PerfRootKind.CGROUP, posixpath.join(sl, "io.weight")))
        if raw_w is not None:
            tokens = raw_w.split()
            w = parse_optional_int(tokens[1] if len(tokens) > 1 else tokens[0])
            if w is not None:
                weights.append((sl, w))
    btrfs_commits: list[BtrfsCommitSample] = []
    for fs_entry in reader.list_rel(PerfRootKind.SYS, "fs/btrfs"):
        if fs_entry == "features":
            continue
        parsed = parse_btrfs_commit_stats(
            fs_entry, reader.read_rel(PerfRootKind.SYS, posixpath.join("fs/btrfs", fs_entry, "commit_stats"))
        )
        if parsed is not None:
            btrfs_commits.append(parsed)
    scrub_limit: str | None = None
    btrfs_conf = files.get(PerfPathKey.BTRFS_CONF)
    if btrfs_conf is not None:
        raw_val = parse_snapshots_conf(btrfs_conf).values.get("SCRUB_LIMIT")
        scrub_limit = clean_opt_str(raw_val)
    return assemble_io_probe(
        files,
        block_devices=blocks,
        nonzero_slice_io_weights=weights,
        btrfs_commits=btrfs_commits,
        btrfs_scrub_limit=scrub_limit,
    )


def collect_gpu_from_reader(reader: PerfFsReader) -> GpuProbeSnapshot:
    """Collect DRM connector, runtime PM, DPM level, and AMDGPU power profile states."""
    gpus: list[GpuDeviceSample] = []
    for entry in reader.list_rel(PerfRootKind.SYS, "class/drm"):
        if not is_drm_card_name(entry):
            continue
        card_rel = posixpath.join("class/drm", entry)
        driver, pci_slot = parse_uevent_fields(
            reader.read_rel(PerfRootKind.SYS, posixpath.join(card_rel, "device/uevent"))
        )
        connected: list[str] = []
        prefix = f"{entry}-"
        for sub in reader.list_rel(PerfRootKind.SYS, card_rel):
            if not sub.startswith(prefix):
                continue
            status_text = reader.read_rel(PerfRootKind.SYS, posixpath.join(card_rel, sub, "status"))
            if is_connected_drm_status(status_text):
                connected.append(sub[len(prefix) :])
        dev_rel = posixpath.join(card_rel, "device")
        gpus.append(
            GpuDeviceSample(
                card=entry,
                driver=driver,
                pci_slot=pci_slot,
                connected_outputs=tuple(connected),
                power_control=clean_opt_str(
                    reader.read_rel(PerfRootKind.SYS, posixpath.join(dev_rel, "power/control"))
                ),
                runtime_status=clean_opt_str(
                    reader.read_rel(PerfRootKind.SYS, posixpath.join(dev_rel, "power/runtime_status"))
                ),
                dpm_force_level=clean_opt_str(
                    reader.read_rel(PerfRootKind.SYS, posixpath.join(dev_rel, "power_dpm_force_performance_level"))
                ),
                power_profile_mode=parse_amd_active_dpm_profile(
                    reader.read_rel(PerfRootKind.SYS, posixpath.join(dev_rel, "pp_power_profile_mode"))
                ),
            )
        )
    game_active: bool | None = None
    game_manual = False
    if reader.is_dir_rel(PerfRootKind.RUN, "cpu-setup"):
        game_active = reader.exists_rel(PerfRootKind.RUN, _GAME_STATE_RUN)
        game_manual = reader.exists_rel(PerfRootKind.RUN, _GAME_MANUAL_RUN)
    gpu_pm_mode: str | None = None
    conf_text = reader.read_key(PerfPathKey.CPU_CONF)
    if conf_text is not None:
        raw_mode = clean_opt_str(parse_shell_assignments(conf_text).values.get("GPU_PM_MODE"))
        if raw_mode is not None:
            gpu_pm_mode = raw_mode.lower()
    return GpuProbeSnapshot(
        gpus=tuple(gpus),
        game_profile_active=game_active,
        gpu_pm_mode=gpu_pm_mode,
        game_profile_manual=game_manual,
    )


def collect_snapshot_from_reader(
    reader: PerfFsReader,
    axes: Sequence[PerfAxis] = ALL_PERF_AXES,
) -> PerfProbeSnapshot:
    """Collect only requested responsiveness axes using `reader`."""
    axis_set = set(axes)
    uptime = parse_uptime_seconds(reader.read_key(PerfPathKey.UPTIME))
    return PerfProbeSnapshot(
        uptime_seconds=uptime,
        memory=(
            collect_memory_from_reader(reader)
            if PerfAxis.MEMORY in axis_set
            else MemoryProbeSnapshot(uptime_seconds=uptime)
        ),
        cpu=collect_cpu_from_reader(reader) if PerfAxis.CPU in axis_set else CpuProbeSnapshot(),
        io=collect_io_from_reader(reader) if PerfAxis.IO in axis_set else IoProbeSnapshot(),
        gpu=collect_gpu_from_reader(reader) if PerfAxis.GPU in axis_set else GpuProbeSnapshot(),
    )

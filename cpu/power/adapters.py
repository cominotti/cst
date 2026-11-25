# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox sysfs/state adapters for `cst cpu power` / `cpu-power`."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import os
import re
from typing import Final

from cst_core.adapters.files import ensure_dir, read_text, write_atomic
from cst_core.adapters.locks import take_lock
from cst_core.adapters.runner import HostExecutionContext
from cst_core.adapters.runtime import host_view_path, undo_python_start
from cst_core.domain.contracts import CPU_SETTINGS, StateFormatKey, state_format
from cst_core.domain.text import describe_problem_lines, strip_ascii_blanks
from cst_core.domain.values import CpuSet, DomainValidationError
from cst_core.ports import ExecutionContextPort
from cpu.game.adapters.host import read_settings, read_stripped, write_sysfs_line
from cpu.game.domain import RUN_LOCK_WAIT_S, held_sys_paths, parse_cstate_limit_us, run_lock
from cpu.power.domain import (
    CSTATE_OFF,
    CSTATE_ON,
    DEFAULT_GAME_CSTATE_US,
    PM_ON,
    CpuIdleEntry,
    CpuTopology,
    GpuPowerCard,
    PowerAction,
    PowerParsedArgs,
    PowerPlan,
    PowerSnapshot,
    SysfsChange,
    boot_cstate_limit_us,
    format_power_plan,
    format_power_status,
    parse_gpu_pm_mode,
    plan_power_apply,
    plan_power_revert,
)

_CARD_RE: Final = re.compile(r"^card([0-9]+)$")
_CPU_RE: Final = re.compile(r"^cpu([0-9]+)$")
_STATE_RE: Final = re.compile(r"^state([0-9]+)$")
_CONNECTED_VALUE: Final = "connected"
# The run lock's wait, as cpu-game-profile's (a name of this module, which a test shortens).
_POWER_LOCK_TIMEOUT_S: Final = RUN_LOCK_WAIT_S


@dataclass(frozen=True, slots=True)
class PowerPaths:
    """Resolved host paths and runtime state files for `cpu-power`."""

    conf: str = CPU_SETTINGS
    sys_drm: str = "/sys/class/drm"
    sys_cpu: str = "/sys/devices/system/cpu"
    run: str = "/run/cpu-setup"
    game_state: str = state_format(StateFormatKey.GAME_STATE).path

    @property
    def lock_file(self) -> str:
        """The run lock cpu-game-profile's on, off and tick hold (/run/cpu-setup/lock): apply and revert
        take it too, so a udev hotplug run or the unit never writes GPU and C-state files meanwhile."""
        return run_lock(self.run)

    @property
    def gpu_managed_file(self) -> str:
        """File tracking PCI slots whose GPU runtime PM `cpu-power` forced on."""
        return f"{self.run}/gpu-pm-managed"

    @property
    def cstate_managed_file(self) -> str:
        """File tracking `cpuN/stateM` entries disabled by `cpu-power`."""
        return f"{self.run}/cstate-disabled"


def start_power_process() -> None:
    """Restore default SIGPIPE/SIGXFSZ and caller LC_CTYPE."""
    undo_python_start()


def power_paths_for(*, ctx: ExecutionContextPort | None = None, read_only: bool = True) -> PowerPaths:
    """Return `PowerPaths`, mapped through `/run/host` inside a container for read-only views."""
    exec_ctx = HostExecutionContext() if ctx is None else ctx
    if not read_only:
        return PowerPaths()
    return PowerPaths(
        conf=host_view_path(exec_ctx, CPU_SETTINGS),
        sys_drm=host_view_path(exec_ctx, "/sys/class/drm"),
        sys_cpu=host_view_path(exec_ctx, "/sys/devices/system/cpu"),
        run=host_view_path(exec_ctx, "/run/cpu-setup"),
        game_state=host_view_path(exec_ctx, state_format(StateFormatKey.GAME_STATE).path),
    )


def _num_key(name: str, pattern: re.Pattern[str]) -> int:
    m = pattern.fullmatch(name)
    return int(m.group(1)) if m is not None else -1


def _uevent_values(path: str) -> tuple[str, str]:
    driver, slot = "", ""
    for line in (read_text(path) or "").splitlines():
        key, _, val = line.partition("=")
        if key == "DRIVER":
            driver = val.strip()
        elif key == "PCI_SLOT_NAME":
            slot = val.strip()
    return driver, slot


def _sorted_entries(folder: str, pattern: re.Pattern[str]) -> list[str]:
    if not os.path.isdir(folder):
        return []
    return sorted(
        (n for n in os.listdir(folder) if pattern.fullmatch(n) is not None),
        key=lambda n: _num_key(n, pattern),
    )


def _scan_gpu_cards(sys_drm: str) -> tuple[GpuPowerCard, ...]:
    cards: list[GpuPowerCard] = []
    for card in _sorted_entries(sys_drm, _CARD_RE):
        cdir = os.path.join(sys_drm, card)
        dev_dir = os.path.join(cdir, "device")
        driver, slot = _uevent_values(os.path.join(dev_dir, "uevent"))
        prefix = f"{card}-"
        outputs = sorted(
            child[len(prefix) :]
            for child in os.listdir(cdir)
            if child.startswith(prefix) and read_stripped(os.path.join(cdir, child, "status")) == _CONNECTED_VALUE
        )
        ctrl_path = os.path.join(dev_dir, "power", "control")
        ctrl = read_stripped(ctrl_path) or ""
        rt = read_stripped(os.path.join(dev_dir, "power", "runtime_status")) or ""
        if ctrl:
            cards.append(GpuPowerCard(card, slot, driver, tuple(outputs), ctrl, rt, ctrl_path))
    return tuple(cards)


def _scan_cpuidle(sys_cpu: str) -> tuple[CpuIdleEntry, ...]:
    out: list[CpuIdleEntry] = []
    for cpu_dir in _sorted_entries(sys_cpu, _CPU_RE):
        cpu = cpu_dir[len("cpu") :]
        idle_dir = os.path.join(sys_cpu, cpu_dir, "cpuidle")
        for state in _sorted_entries(idle_dir, _STATE_RE):
            sdir = os.path.join(idle_dir, state)
            lat_raw = read_stripped(os.path.join(sdir, "latency")) or ""
            if re.fullmatch(r"[0-9]+", lat_raw) is None:
                continue
            dis_path = os.path.join(sdir, "disable")
            dis_raw = read_stripped(dis_path)
            if dis_raw is None:
                continue
            name = read_stripped(os.path.join(sdir, "name")) or state
            out.append(CpuIdleEntry(cpu, state, name, int(lat_raw), dis_raw != "0", dis_path))
    return tuple(out)


def _read_managed_set(path: str) -> frozenset[str]:
    text = read_text(path)
    if text is None:
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines() if line.strip())


def _write_managed_set(path: str, keys: list[str]) -> None:
    unique = sorted(set(keys))
    if not unique:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        return
    ensure_dir(os.path.dirname(path), 0o755)
    write_atomic(path, "".join(f"{k}\n" for k in unique), mode=0o644, durable=False, relabel=False)


def _cpuidle_driver(sys_cpu: str) -> str:
    return read_stripped(os.path.join(sys_cpu, "cpuidle", "current_driver")) or ""


def read_cpu_topology(sys_dir: str, p_cores: str, e_cores: str) -> CpuTopology:
    """The host's CPU topology under sys_dir (/sys), p_cores and e_cores being the hybrid CPU's
    cpu_core/cpus and cpu_atom/cpus files: for cpu-setup.conf's first defaults (cst cpu install)."""
    sys_cpu = os.path.join(sys_dir, "devices", "system", "cpu")
    online = read_stripped(os.path.join(sys_cpu, "online")) or ""
    try:
        cpus = CpuSet.parse(online).sorted_cpus() if online else ()
    except DomainValidationError:
        cpus = ()
    return CpuTopology(
        p_cores=read_stripped(p_cores) or "",
        e_cores=read_stripped(e_cores) or "",
        online=online,
        siblings=tuple(
            read_stripped(os.path.join(sys_cpu, f"cpu{c}", "topology", "thread_siblings_list")) or "" for c in cpus
        ),
        cpuidle_driver=_cpuidle_driver(sys_cpu),
    )


def collect_power_snapshot(paths: PowerPaths) -> PowerSnapshot:
    """Collect configuration and live sysfs state into a pure `PowerSnapshot`."""
    settings, problems = read_settings(paths.conf)
    warnings: list[str] = describe_problem_lines(paths.conf, problems)
    gpu_mode, gpu_err = parse_gpu_pm_mode(settings.gpu_pm_mode)
    if gpu_err is not None:
        warnings.append(gpu_err)
    # A file without the setting leaves it "" (GameSettings' default): the driver then decides the cap.
    cstate_limit, cs_err = boot_cstate_limit_us(
        settings.cpu_cstate_max_latency_us,
        explicit=bool(strip_ascii_blanks(settings.cpu_cstate_max_latency_us)),  # as the parse reads it
        cpuidle_driver=_cpuidle_driver(paths.sys_cpu),
    )
    if cs_err is not None:
        warnings.append(f"CPU_CSTATE_MAX_LATENCY_US: {cs_err}")
    game_limit, gs_err = parse_cstate_limit_us(settings.game_cstate_max_latency_us, default_us=DEFAULT_GAME_CSTATE_US)
    if gs_err is not None:
        warnings.append(f"GAME_CSTATE_MAX_LATENCY_US: {gs_err}")
    game_text = read_text(paths.game_state) or ""
    return PowerSnapshot(
        conf_path=paths.conf,
        gpu_mode=gpu_mode,
        cstate_limit_us=cstate_limit,
        game_cstate_limit_us=game_limit,
        gpu_cards=_scan_gpu_cards(paths.sys_drm),
        idle_entries=_scan_cpuidle(paths.sys_cpu),
        gpu_managed=_read_managed_set(paths.gpu_managed_file),
        cstate_managed=_read_managed_set(paths.cstate_managed_file),
        game_held_paths=held_sys_paths(game_text),
        config_warnings=tuple(warnings),
    )


def _apply_change_group(changes: tuple[SysfsChange, ...], managed: set[str], *, adding: bool, verb: str) -> list[str]:
    lines: list[str] = []
    for c in changes:
        if write_sysfs_line(c.path, c.new):
            lines.append(f"{verb:<10} {c.path} {c.old} -> {c.new} ({c.reason})")
        else:
            lines.append(f"skipped:   {c.path} (not writable)")
            if adding:
                managed.discard(c.key)
            else:
                managed.add(c.key)
    return lines


def _apply_gpu_plan(paths: PowerPaths, plan: PowerPlan, verb: str) -> list[str]:
    """Write the GPU changes, each with its own direction (on or auto, in card order), then the managed set."""
    gpu_next = set(plan.next_gpu_managed)
    gpu_lines: list[str] = []
    for c in plan.gpu_changes:
        adding = not plan.revert_mode and c.new == PM_ON
        gpu_lines.extend(_apply_change_group((c,), gpu_next, adding=adding, verb=verb))
    _write_managed_set(paths.gpu_managed_file, list(gpu_next))
    return gpu_lines or ["unchanged: GPU runtime PM"]


def _apply_cstate_plan(paths: PowerPaths, plan: PowerPlan, verb: str) -> list[str]:
    """Write the C-state caps, then their reverts, then the managed set."""
    cs_next = set(plan.next_cstate_managed)
    add_changes = tuple(c for c in plan.cstate_changes if c.new == CSTATE_OFF)
    rev_changes = tuple(c for c in plan.cstate_changes if c.new == CSTATE_ON)
    cs_lines = _apply_change_group(add_changes, cs_next, adding=True, verb=verb)
    cs_lines.extend(_apply_change_group(rev_changes, cs_next, adding=False, verb="reverted:"))
    _write_managed_set(paths.cstate_managed_file, list(cs_next))
    return cs_lines or ["unchanged: CPU cpuidle"]


def apply_power_plan(
    paths: PowerPaths, snapshot: PowerSnapshot, plan: PowerPlan, *, gpu_only: bool = False
) -> tuple[int, list[str]]:
    """Execute planned sysfs writes and persist CST managed marker sets in `paths.run`."""
    lines = [f"WARN: {w}" for w in snapshot.config_warnings]
    verb = "reverted:" if plan.revert_mode else "set:"
    lines.extend(_apply_gpu_plan(paths, plan, verb))
    if not gpu_only:
        lines.extend(_apply_cstate_plan(paths, plan, verb))
    return 0, lines


def execute_power_action(parsed: PowerParsedArgs, paths: PowerPaths) -> tuple[int, list[str]]:
    """Collect a snapshot and format or execute a validated `cpu-power` action (apply and revert under
    the run lock, the snapshot taken once it is held)."""
    if parsed.action in (PowerAction.STATUS, PowerAction.PLAN):
        snap = collect_power_snapshot(paths)
        if parsed.action == PowerAction.STATUS:
            return 0, format_power_status(snap)
        pl = plan_power_apply(snap, gpu_only=parsed.gpu_only)
        return 0, format_power_plan(snap, pl, gpu_only=parsed.gpu_only)
    try:
        ensure_dir(paths.run, 0o755)
        fd = take_lock(paths.lock_file, wait=True, mode=0o644, timeout=_POWER_LOCK_TIMEOUT_S)
    except OSError as exc:
        return 1, [f"ERROR: cannot lock {paths.lock_file}: {exc.strerror or exc}"]
    if fd is None:
        return 1, [f"ERROR: {paths.lock_file} still held after {_POWER_LOCK_TIMEOUT_S} s; nothing changed"]
    try:
        snap = collect_power_snapshot(paths)
        if parsed.action == PowerAction.APPLY:
            return apply_power_plan(
                paths, snap, plan_power_apply(snap, gpu_only=parsed.gpu_only), gpu_only=parsed.gpu_only
            )
        return apply_power_plan(paths, snap, plan_power_revert(snap))
    finally:
        os.close(fd)

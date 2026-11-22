# SPDX-License-Identifier: MPL-2.0
"""Pure domain planning, parsing, and status formatting for `cst cpu power` / `cpu-power`."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from cst_core.domain.text import HELP_WORDS, enum_member
from cpu.game.domain import parse_cstate_limit_us

DEFAULT_CPU_CSTATE_US: Final = 200
# The cpuidle driver whose C-states the 200 us default was measured on (Intel); AMD hosts use acpi_idle.
INTEL_IDLE_DRIVER: Final = "intel_idle"
DEFAULT_GAME_CSTATE_US: Final = 50
PM_ON: Final = "on"
PM_AUTO: Final = "auto"
CSTATE_ON: Final = "0"
CSTATE_OFF: Final = "1"
_KIND_GPU: Final = "gpu"
_KIND_CSTATE: Final = "cstate"
_GPU_ONLY_FLAG: Final = "--gpu-only"


@dataclass(frozen=True, slots=True)
class CpuTopology:
    """What the host's sysfs says about its CPUs, for cpu-setup.conf's first defaults ("" when unreadable)."""

    p_cores: str = ""  # /sys/devices/cpu_core/cpus (hybrid Intel only)
    e_cores: str = ""  # /sys/devices/cpu_atom/cpus (hybrid Intel only)
    online: str = ""  # /sys/devices/system/cpu/online
    # Each online CPU's topology/thread_siblings_list (the CPUs of its physical core).
    siblings: tuple[str, ...] = ()
    cpuidle_driver: str = ""  # /sys/devices/system/cpu/cpuidle/current_driver


def default_cstate_cap_off(cpuidle_driver: str) -> bool:
    """Whether CPU_CSTATE_MAX_LATENCY_US's 200 us default is off on this driver: on with intel_idle or a
    driver that cannot be read (""), off with any other (AMD's acpi_idle)."""
    return bool(cpuidle_driver) and cpuidle_driver != INTEL_IDLE_DRIVER


def boot_cstate_limit_us(raw: str, *, explicit: bool, cpuidle_driver: str) -> tuple[int | None, str | None]:
    """CPU_CSTATE_MAX_LATENCY_US as cpu-power applies it, `(limit_us_or_None, error)`: a value
    cpu-setup.conf sets always counts; without one, the 200 us default applies with intel_idle (or a
    driver that cannot be read) and the cap is off with any other driver (AMD's acpi_idle)."""
    if not explicit and default_cstate_cap_off(cpuidle_driver):
        return None, None
    return parse_cstate_limit_us(raw, default_us=DEFAULT_CPU_CSTATE_US)


class GpuPmMode(StrEnum):
    """When GPU runtime power management (`power/control=on`) is kept forced."""

    ALWAYS = "always"
    GAME = "game"
    OFF = "off"


def parse_gpu_pm_mode(raw: str) -> tuple[GpuPmMode, str | None]:
    """Parse `GPU_PM_MODE`; blank uses `'always'`, invalid returns `('always', error)`."""
    cleaned = raw.strip().lower()
    if not cleaned:
        return GpuPmMode.ALWAYS, None
    member = enum_member(GpuPmMode, cleaned)
    if member is None:
        return GpuPmMode.ALWAYS, f"GPU_PM_MODE={raw!r} must be always, game, or off"
    return member, None


class PowerAction(StrEnum):
    """Supported subcommands of `cpu-power` / `cst cpu power`."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"
    HELP = "help"


@dataclass(frozen=True, slots=True)
class PowerParsedArgs:
    """Validated CLI arguments for `cpu-power`."""

    action: PowerAction
    gpu_only: bool = False
    error: str | None = None
    code: int = 0


@dataclass(frozen=True, slots=True)
class GpuPowerCard:
    """Observed DRM card runtime PM and connector state."""

    card: str
    pci_slot: str
    driver: str
    connected_outputs: tuple[str, ...]
    power_control: str
    runtime_status: str
    control_path: str


@dataclass(frozen=True, slots=True)
class CpuIdleEntry:
    """Observed cpuidle state on one logical CPU."""

    cpu: str
    state: str
    name: str
    latency_us: int
    disabled: bool
    disable_path: str


@dataclass(frozen=True, slots=True)
class SysfsChange:
    """One planned or reverted sysfs write."""

    key: str
    path: str
    old: str
    new: str
    reason: str
    kind: str


@dataclass(frozen=True, slots=True)
class PowerPlan:
    """Planned GPU runtime PM and CPU C-state changes plus next managed key sets."""

    gpu_changes: tuple[SysfsChange, ...]
    cstate_changes: tuple[SysfsChange, ...]
    next_gpu_managed: tuple[str, ...]
    next_cstate_managed: tuple[str, ...]
    revert_mode: bool = False


@dataclass(frozen=True, slots=True)
class PowerSnapshot:
    """All sysfs, configuration, and CST state inputs needed for power planning."""

    conf_path: str
    gpu_mode: GpuPmMode
    cstate_limit_us: int | None
    game_cstate_limit_us: int | None
    gpu_cards: tuple[GpuPowerCard, ...]
    idle_entries: tuple[CpuIdleEntry, ...]
    gpu_managed: frozenset[str]
    cstate_managed: frozenset[str]
    game_held_paths: frozenset[str]
    config_warnings: tuple[str, ...] = ()


def format_power_usage(prog: str) -> str:
    """Return usage and command summary for `cpu-power` / `cst cpu power`."""
    return (
        f"{prog}: manage connected display GPU runtime PM and CPU deep C-state ceiling.\n\n"
        "Commands:\n"
        "  status                read-only: show GPU runtime PM and CPU cpuidle states\n"
        "  plan [--gpu-only]     dry run showing what `apply` would change\n"
        "  apply [--gpu-only]    (root) apply GPU_PM_MODE and CPU_CSTATE_MAX_LATENCY_US\n"
        "  revert                (root) restore CST-managed GPUs and C-states\n"
        "  help                  show this help\n\n"
        f"Usage: {prog} <status|plan|apply|revert|help> [--gpu-only]\n"
    )


def parse_power_args(argv: Sequence[str]) -> PowerParsedArgs:
    """Parse CLI arguments after `cpu-power`."""
    if not argv:
        return PowerParsedArgs(PowerAction.STATUS)
    cmd, rest = argv[0], argv[1:]
    if cmd in HELP_WORDS:
        return PowerParsedArgs(PowerAction.HELP)
    action = enum_member(PowerAction, cmd)
    if action is None:
        return PowerParsedArgs(PowerAction.HELP, error=f"unknown command {cmd!r}", code=2)
    gpu_only = False
    error: str | None = None
    if action in (PowerAction.PLAN, PowerAction.APPLY) and len(rest) == 1 and rest[0] == _GPU_ONLY_FLAG:
        gpu_only = True
    elif action in (PowerAction.PLAN, PowerAction.APPLY) and rest:
        error = f"unexpected argument(s): {' '.join(rest)}"
    elif action in (PowerAction.STATUS, PowerAction.REVERT) and rest:
        error = f"{action.value} takes no arguments (got {rest[0]!r})"
    return PowerParsedArgs(action, gpu_only=gpu_only, error=error, code=2 if error is not None else 0)


def plan_gpu_pm(snapshot: PowerSnapshot) -> tuple[tuple[SysfsChange, ...], tuple[str, ...]]:
    """Compute GPU `power/control` changes and updated managed PCI slot keys."""
    changes: list[SysfsChange] = []
    next_managed: list[str] = []
    for card in snapshot.gpu_cards:
        key = card.pci_slot or card.card
        was_managed = key in snapshot.gpu_managed
        want_on = snapshot.gpu_mode == GpuPmMode.ALWAYS and bool(card.connected_outputs)
        if want_on:
            if card.power_control == PM_AUTO:
                outputs = ", ".join(card.connected_outputs)
                changes.append(
                    SysfsChange(key, card.control_path, PM_AUTO, PM_ON, f"{card.card} connected ({outputs})", _KIND_GPU)
                )
                next_managed.append(key)
            elif was_managed:
                next_managed.append(key)
        elif was_managed:
            if card.control_path in snapshot.game_held_paths:
                next_managed.append(key)
            elif card.power_control == PM_ON:
                reason = (
                    f"{card.card} disconnected"
                    if snapshot.gpu_mode == GpuPmMode.ALWAYS
                    else f"GPU_PM_MODE={snapshot.gpu_mode.value}"
                )
                changes.append(SysfsChange(key, card.control_path, PM_ON, PM_AUTO, reason, _KIND_GPU))
    return tuple(changes), tuple(next_managed)


def plan_cstate_cap(snapshot: PowerSnapshot) -> tuple[tuple[SysfsChange, ...], tuple[str, ...]]:
    """Compute CPU `cpuidle/state*/disable` changes and updated managed state keys."""
    changes: list[SysfsChange] = []
    next_managed: list[str] = []
    limit = snapshot.cstate_limit_us
    for entry in snapshot.idle_entries:
        key = f"cpu{entry.cpu}/{entry.state}"
        was_managed = key in snapshot.cstate_managed
        should_disable = limit is not None and entry.latency_us >= limit
        if should_disable:
            if not entry.disabled and entry.disable_path not in snapshot.game_held_paths:
                changes.append(
                    SysfsChange(
                        key,
                        entry.disable_path,
                        CSTATE_ON,
                        CSTATE_OFF,
                        f"{entry.name} {entry.latency_us} us >= {limit} us",
                        _KIND_CSTATE,
                    )
                )
                next_managed.append(key)
            elif was_managed:
                next_managed.append(key)
        elif was_managed:
            if entry.disable_path in snapshot.game_held_paths:
                next_managed.append(key)
            elif entry.disabled:
                changes.append(
                    SysfsChange(
                        key,
                        entry.disable_path,
                        CSTATE_OFF,
                        CSTATE_ON,
                        f"{entry.name} {entry.latency_us} us below ceiling",
                        _KIND_CSTATE,
                    )
                )
    return tuple(changes), tuple(next_managed)


def plan_power_apply(snapshot: PowerSnapshot, *, gpu_only: bool = False) -> PowerPlan:
    """Compute combined GPU PM and (unless `gpu_only`) CPU C-state changes."""
    gpu_changes, next_gpu = plan_gpu_pm(snapshot)
    if gpu_only:
        return PowerPlan(
            gpu_changes=gpu_changes,
            cstate_changes=(),
            next_gpu_managed=next_gpu,
            next_cstate_managed=tuple(sorted(snapshot.cstate_managed)),
            revert_mode=False,
        )
    cstate_changes, next_cstates = plan_cstate_cap(snapshot)
    return PowerPlan(
        gpu_changes=gpu_changes,
        cstate_changes=cstate_changes,
        next_gpu_managed=next_gpu,
        next_cstate_managed=next_cstates,
        revert_mode=False,
    )


def plan_power_revert(snapshot: PowerSnapshot) -> PowerPlan:
    """Compute changes to revert all CST-managed GPU runtime PM and CPU C-state caps."""
    gpu_changes: list[SysfsChange] = []
    keep_gpu: list[str] = []
    for card in snapshot.gpu_cards:
        key = card.pci_slot or card.card
        if key not in snapshot.gpu_managed:
            continue
        if card.control_path in snapshot.game_held_paths:
            keep_gpu.append(key)
        elif card.power_control == PM_ON:
            gpu_changes.append(SysfsChange(key, card.control_path, PM_ON, PM_AUTO, "revert CST gpu-pm", _KIND_GPU))

    cstate_changes: list[SysfsChange] = []
    keep_cstate: list[str] = []
    for entry in snapshot.idle_entries:
        key = f"cpu{entry.cpu}/{entry.state}"
        if key not in snapshot.cstate_managed:
            continue
        if entry.disable_path in snapshot.game_held_paths:
            keep_cstate.append(key)
        elif entry.disabled:
            cstate_changes.append(
                SysfsChange(key, entry.disable_path, CSTATE_OFF, CSTATE_ON, "revert CST cstate cap", _KIND_CSTATE)
            )
    return PowerPlan(
        gpu_changes=tuple(gpu_changes),
        cstate_changes=tuple(cstate_changes),
        next_gpu_managed=tuple(keep_gpu),
        next_cstate_managed=tuple(keep_cstate),
        revert_mode=True,
    )


def _limit_label(limit_us: int | None) -> str:
    return f"{limit_us} us" if limit_us is not None else "off"


def format_power_status(snapshot: PowerSnapshot) -> list[str]:
    """Format readable `cpu-power status` lines."""
    lines = [
        (
            f"settings: GPU_PM_MODE={snapshot.gpu_mode.value}, "
            f"CPU_CSTATE_MAX_LATENCY_US={_limit_label(snapshot.cstate_limit_us)}, "
            f"GAME_CSTATE_MAX_LATENCY_US={_limit_label(snapshot.game_cstate_limit_us)} ({snapshot.conf_path})"
        )
    ]
    lines.extend(f"WARN: {w}" for w in snapshot.config_warnings)
    if not snapshot.gpu_cards:
        lines.append("GPU runtime PM: none found")
    else:
        lines.append("GPU runtime PM:")
        for card in snapshot.gpu_cards:
            key = card.pci_slot or card.card
            out_txt = ",".join(card.connected_outputs) if card.connected_outputs else "none"
            tag = " [managed]" if key in snapshot.gpu_managed else ""
            slot = card.pci_slot or "-"
            lines.append(
                f"  {card.card} ({card.driver or '?'}, {slot}): outputs={out_txt}, "
                f"power/control={card.power_control} ({card.runtime_status or '?'}){tag}"
            )
    if not snapshot.idle_entries:
        lines.append("CPU cpuidle: none found")
    else:
        lines.append("CPU cpuidle:")
        grouped: dict[tuple[str, str, int], list[CpuIdleEntry]] = defaultdict(list)
        for entry in snapshot.idle_entries:
            grouped[entry.state, entry.name, entry.latency_us].append(entry)
        for (state, name, latency), entries in grouped.items():
            dis = sum(1 for e in entries if e.disabled)
            managed = sum(1 for e in entries if f"cpu{e.cpu}/{e.state}" in snapshot.cstate_managed)
            mg_txt = f" [managed: {managed}]" if managed else ""
            lines.append(f"  {state} {name} ({latency} us): disabled on {dis}/{len(entries)} CPUs{mg_txt}")
    return lines


def format_power_plan(snapshot: PowerSnapshot, plan: PowerPlan, *, gpu_only: bool = False) -> list[str]:
    """Format dry-run `cpu-power plan` output."""
    lines = ["Dry run: nothing is changed."]
    lines.extend(f"WARN: {w}" for w in snapshot.config_warnings)
    if not plan.gpu_changes:
        lines.append(f"  unchanged: GPU runtime PM (GPU_PM_MODE={snapshot.gpu_mode.value})")
    else:
        lines.extend(f"  set:       {c.path} {c.old} -> {c.new} ({c.reason})" for c in plan.gpu_changes)
    if not gpu_only:
        if not plan.cstate_changes:
            lines.append(f"  unchanged: CPU cpuidle (ceiling={_limit_label(snapshot.cstate_limit_us)})")
        else:
            lines.append(
                f"  cstates:   {len(plan.cstate_changes)} change(s) -> "
                f"{plan.cstate_changes[0].new} (ceiling={_limit_label(snapshot.cstate_limit_us)})"
            )
            lines.extend(f"    {c.path} {c.old} -> {c.new} ({c.reason})" for c in plan.cstate_changes)
    return lines

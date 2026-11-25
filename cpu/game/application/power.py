# SPDX-License-Identifier: MPL-2.0
"""The power steps of 'on' and the tick: dynamic GPU power boost, AMD DPM 3D_FULL_SCREEN, P-core EPP and C-state
caps, each old value recorded in game.state before the write. No I/O: sysfs, the cgroup tree and systemd are the
profile's ports (the DRM discovery is cpu.game.adapters.power's)."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING, Final

from cst_core.domain.text import group
from cpu.game.domain import (
    GameRecordKind,
    expand_cpus,
    has_sys_record,
    parse_amd_profile_modes,
    parse_cstate_limit_us,
)
from cpu.power.domain import GpuPmMode, parse_gpu_pm_mode

if TYPE_CHECKING:
    from cpu.game.application.profile import GameProfileSteps

_DEFAULT_GAME_CSTATE_US: Final = 50
_MAX_CGROUP_WALK_DEPTH: Final = 3
_CARD_NAME_RE: Final = re.compile(r"^card([0-9]+)$")
_STATE_NAME_RE: Final = re.compile(r"^state([0-9]+)$")
_GPU_PM_ON: Final = "on"
_DPM_MANUAL: Final = "manual"
_CSTATE_ENABLE: Final = "0"
_CSTATE_DISABLE: Final = "1"
# The Steam client's own processes (their /proc/PID/comm, which the kernel cuts to 15 bytes): the client
# UI, its shader pre-compiler (fossilize_replay) and its driver probes (vulkandriverquery[64],
# gldriverquery[64]) open render nodes too, which says nothing about the GPU the game runs on.
_STEAM_CLIENT_COMMS: Final = frozenset(
    {"steam", "steamwebhelper", "fossilize_repla", "vulkandriverque", "gldriverquery", "gldriverquery64"}
)


def _suffix_num(name: str, pattern: re.Pattern[str]) -> int:
    m = pattern.fullmatch(name)
    return int(group(m, 1)) if m is not None else -1


def card_number(name: str) -> int:
    """N of a DRM cardN name (-1 for another name): the order cards are listed and handled in."""
    return _suffix_num(name, _CARD_NAME_RE)


def is_card(name: str) -> bool:
    """The name is a DRM card's (cardN), not one of its connectors."""
    return _CARD_NAME_RE.fullmatch(name) is not None


def _parse_int_stripped(profile: GameProfileSteps, path: str) -> int | None:
    raw = profile.ports.power.read_stripped(path)
    if raw is None or not re.fullmatch(r"[0-9]+", raw):
        return None
    return int(raw)


def _record_and_write_sys(profile: GameProfileSteps, path: str, old: str, new: str) -> bool:
    if has_sys_record(profile.state_text(), path):
        return False
    if not profile.append_state(GameRecordKind.SYSFS, path, old):
        profile.report_unrecorded(f"{path} = {new}")
        return False
    return profile.ports.power.write(path, new)


def boost_p_cores(profile: GameProfileSteps) -> None:
    """EPP performance on each P-core whose preference can be written, its old value recorded first."""
    sysfs = profile.ports.power
    for cpu in expand_cpus(profile.s.p_cores):
        f = profile.paths.epp(cpu)
        if not sysfs.writable(f):
            continue
        if not profile.append_state(GameRecordKind.EPP, cpu, sysfs.read_stripped(f) or ""):
            profile.report_unrecorded(f"cpu{cpu}: EPP performance")
            continue
        sysfs.write(f, "performance")


def apply_game_cstates(profile: GameProfileSteps) -> None:
    """Disable enabled cpuidle states on `P_CORES` at or above `GAME_CSTATE_MAX_LATENCY_US`."""
    limit_us, _ = parse_cstate_limit_us(profile.s.game_cstate_max_latency_us, default_us=_DEFAULT_GAME_CSTATE_US)
    if limit_us is None:
        return
    sysfs = profile.ports.power
    sys_cpu = profile.paths.sys_cpu
    for cpu in expand_cpus(profile.s.p_cores):
        idle_dir = os.path.join(sys_cpu, f"cpu{cpu}", "cpuidle")
        if not sysfs.is_dir(idle_dir):
            continue
        states = sorted(
            (name for name in sysfs.list_dir(idle_dir) if _STATE_NAME_RE.fullmatch(name) is not None),
            key=lambda n: _suffix_num(n, _STATE_NAME_RE),
        )
        for state_name in states:
            sdir = os.path.join(idle_dir, state_name)
            disable_path = os.path.join(sdir, "disable")
            if not sysfs.writable(disable_path):
                continue
            latency = _parse_int_stripped(profile, os.path.join(sdir, "latency"))
            disabled_now = sysfs.read_stripped(disable_path)
            if latency is not None and latency >= limit_us and disabled_now == _CSTATE_ENABLE:
                _record_and_write_sys(profile, disable_path, _CSTATE_ENABLE, _CSTATE_DISABLE)


def _gamemode_pids(profile: GameProfileSteps) -> set[str]:
    """The processes that have libgamemode mapped."""
    kernel = profile.ports.kernel
    return {
        os.path.basename(os.path.dirname(maps))
        for maps in kernel.process_maps(profile.paths.proc)
        if kernel.holds(maps, "libgamemode")
    }


def _cgroup_procs(profile: GameProfileSteps, folder: str) -> set[str]:
    """The processes of a cgroup and of its children down to _MAX_CGROUP_WALK_DEPTH."""
    seen: set[str] = set()
    for text in profile.ports.kernel.procs_texts(folder, _MAX_CGROUP_WALK_DEPTH):
        lines = text.splitlines()
        seen.update(line.strip() for line in lines if line.strip())
    return seen


def _scope_pids(profile: GameProfileSteps, scope: str) -> set[str]:
    """The processes of one Steam scope, from the cgroup systemd names for it."""
    cg = profile.ask("usr", "show", "-P", "ControlGroup", scope).stdout.rstrip("\n")
    if not cg:
        return set()
    folder = f"{profile.paths.cgroot}{cg}"
    if not profile.ports.kernel.is_dir(folder):
        return set()
    return _cgroup_procs(profile, folder)


def _game_pids(profile: GameProfileSteps) -> list[str]:
    seen = _gamemode_pids(profile)
    for scope in profile.steam_scopes():
        seen |= _scope_pids(profile, scope)
    return sorted(seen)


def active_game_gpu_cards(profile: GameProfileSteps) -> list[str]:
    """Return sorted DRM `cardN` names the game's processes render on; without any, the connected display
    cards; without those, the cards the Steam client's own processes (_STEAM_CLIENT_COMMS) hold."""
    sysfs, sys_drm = profile.ports.power, profile.paths.sys_drm
    dev_to_card, cards = sysfs.drm_cards(sys_drm)
    if not cards:
        return []
    matched: set[str] = set()
    client: set[str] = set()
    for pid in _game_pids(profile):
        comm = sysfs.read_stripped(os.path.join(profile.paths.proc, pid, "comm")) or ""
        held = sysfs.pid_cards(profile.paths.proc, sys_drm, pid, dev_to_card)
        (client if comm in _STEAM_CLIENT_COMMS else matched).update(held)
    if matched:
        return sorted(matched, key=card_number)
    return sysfs.connected_cards(sys_drm, cards) or sorted(client, key=card_number)


def _keep_gpu_awake(profile: GameProfileSteps, dev_dir: str) -> None:
    """power/control=on, its old value recorded first."""
    pm_path = os.path.join(dev_dir, "power", "control")
    if not profile.ports.power.writable(pm_path):
        return
    pm_old = profile.ports.power.read_stripped(pm_path) or ""
    if pm_old and pm_old != _GPU_PM_ON:
        _record_and_write_sys(profile, pm_path, pm_old, _GPU_PM_ON)


def _switch_amd_fullscreen(profile: GameProfileSteps, dev_dir: str) -> None:
    """AMD DPM manual, then the 3D_FULL_SCREEN power profile, each old value recorded first."""
    sysfs = profile.ports.power
    dpm_path = os.path.join(dev_dir, "power_dpm_force_performance_level")
    prof_path = os.path.join(dev_dir, "pp_power_profile_mode")
    if not (sysfs.writable(dpm_path) and sysfs.writable(prof_path)):
        return
    if has_sys_record(profile.state_text(), prof_path):
        return
    current_idx, fullscreen_idx = parse_amd_profile_modes(sysfs.read_text(prof_path) or "")
    # No current profile (no "*" row) means nothing to restore: DPM stays as it is, not manual.
    if current_idx is None or fullscreen_idx is None or current_idx == fullscreen_idx:
        return
    dpm_old = sysfs.read_stripped(dpm_path) or ""
    if dpm_old and dpm_old != _DPM_MANUAL:
        _record_and_write_sys(profile, dpm_path, dpm_old, _DPM_MANUAL)
    if sysfs.read_stripped(dpm_path) == _DPM_MANUAL:
        _record_and_write_sys(profile, prof_path, current_idx, fullscreen_idx)


def apply_game_gpu(profile: GameProfileSteps) -> None:
    """Keep active game GPUs awake (`power/control=on`) and switch AMD DPM to `3D_FULL_SCREEN`."""
    pm_mode, _ = parse_gpu_pm_mode(profile.s.gpu_pm_mode)
    for card in active_game_gpu_cards(profile):
        dev_dir = os.path.join(profile.paths.sys_drm, card, "device")
        if pm_mode != GpuPmMode.OFF:
            _keep_gpu_awake(profile, dev_dir)
        _switch_amd_fullscreen(profile, dev_dir)

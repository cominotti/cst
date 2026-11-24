# SPDX-License-Identifier: MPL-2.0
"""Dynamic GPU power boost, AMD DPM 3D_FULL_SCREEN, P-core EPP and C-state caps for `cpu-game-profile`."""

from __future__ import annotations

import glob
import os
import re
from typing import TYPE_CHECKING, Final

from cst_core.adapters.files import read_text
from cst_core.domain.text import group
from cpu.game.adapters.host import holds, read_stripped, write_sysfs_line
from cpu.game.domain import (
    GameRecordKind,
    expand_cpus,
    has_sys_record,
    parse_amd_profile_modes,
    parse_cstate_limit_us,
)
from cpu.power.domain import GpuPmMode, parse_gpu_pm_mode

if TYPE_CHECKING:
    from cpu.game.adapters.profile import GameProfile

_DEFAULT_GAME_CSTATE_US: Final = 50
_MAX_CGROUP_WALK_DEPTH: Final = 3
_CARD_NAME_RE: Final = re.compile(r"^card([0-9]+)$")
_STATE_NAME_RE: Final = re.compile(r"^state([0-9]+)$")
_DRI_FD_RE: Final = re.compile(r"^/dev/dri/(renderD[0-9]+|card[0-9]+)")
_GPU_PM_ON: Final = "on"
_DPM_MANUAL: Final = "manual"
_CSTATE_ENABLE: Final = "0"
_CSTATE_DISABLE: Final = "1"
_CONNECTED_STATE: Final = "connected"
# The Steam client's own processes (their /proc/PID/comm, which the kernel cuts to 15 bytes): the client
# UI, its shader pre-compiler (fossilize_replay) and its driver probes (vulkandriverquery[64],
# gldriverquery[64]) open render nodes too, which says nothing about the GPU the game runs on.
_STEAM_CLIENT_COMMS: Final = frozenset(
    {"steam", "steamwebhelper", "fossilize_repla", "vulkandriverque", "gldriverquery", "gldriverquery64"}
)


def _suffix_num(name: str, pattern: re.Pattern[str]) -> int:
    m = pattern.fullmatch(name)
    return int(group(m, 1)) if m is not None else -1


def _card_number(name: str) -> int:
    return _suffix_num(name, _CARD_NAME_RE)


def _parse_int_stripped(path: str) -> int | None:
    raw = read_stripped(path)
    if raw is None or not re.fullmatch(r"[0-9]+", raw):
        return None
    return int(raw)


def _record_and_write_sys(profile: GameProfile, path: str, old: str, new: str) -> bool:
    if has_sys_record(profile.state_text(), path):
        return False
    if not profile.append_state(GameRecordKind.SYSFS, path, old):
        profile.report_unrecorded(f"{path} = {new}")
        return False
    return write_sysfs_line(path, new)


def boost_p_cores(profile: GameProfile) -> None:
    """EPP performance on each P-core whose preference can be written, its old value recorded first."""
    for cpu in expand_cpus(profile.s.p_cores):
        f = profile.paths.epp(cpu)
        if not os.access(f, os.W_OK):
            continue
        if not profile.append_state(GameRecordKind.EPP, cpu, read_stripped(f) or ""):
            profile.report_unrecorded(f"cpu{cpu}: EPP performance")
            continue
        write_sysfs_line(f, "performance")


def apply_game_cstates(profile: GameProfile) -> None:
    """Disable enabled cpuidle states on `P_CORES` at or above `GAME_CSTATE_MAX_LATENCY_US`."""
    limit_us, _ = parse_cstate_limit_us(profile.s.game_cstate_max_latency_us, default_us=_DEFAULT_GAME_CSTATE_US)
    if limit_us is None:
        return
    sys_cpu = profile.paths.sys_cpu
    for cpu in expand_cpus(profile.s.p_cores):
        idle_dir = os.path.join(sys_cpu, f"cpu{cpu}", "cpuidle")
        if not os.path.isdir(idle_dir):
            continue
        states = sorted(
            (name for name in os.listdir(idle_dir) if _STATE_NAME_RE.fullmatch(name) is not None),
            key=lambda n: _suffix_num(n, _STATE_NAME_RE),
        )
        for state_name in states:
            sdir = os.path.join(idle_dir, state_name)
            disable_path = os.path.join(sdir, "disable")
            if not os.access(disable_path, os.W_OK):
                continue
            latency = _parse_int_stripped(os.path.join(sdir, "latency"))
            disabled_now = read_stripped(disable_path)
            if latency is not None and latency >= limit_us and disabled_now == _CSTATE_ENABLE:
                _record_and_write_sys(profile, disable_path, _CSTATE_ENABLE, _CSTATE_DISABLE)


def _gamemode_pids(proc_dir: str) -> set[str]:
    """The processes that have libgamemode mapped."""
    return {
        os.path.basename(os.path.dirname(maps))
        for maps in sorted(glob.glob(os.path.join(glob.escape(proc_dir), "[0-9]*", "maps")))
        if holds(maps, "libgamemode")
    }


def _cgroup_procs(folder: str) -> set[str]:
    """The processes of a cgroup and of its children down to _MAX_CGROUP_WALK_DEPTH."""
    seen: set[str] = set()
    top = folder.rstrip("/").count("/")
    for d, subdirs, files in os.walk(folder):
        if d.rstrip("/").count("/") - top >= _MAX_CGROUP_WALK_DEPTH:
            subdirs.clear()
        if "cgroup.procs" in files:
            lines = (read_text(os.path.join(d, "cgroup.procs")) or "").splitlines()
            seen.update(line.strip() for line in lines if line.strip())
    return seen


def _scope_pids(profile: GameProfile, scope: str) -> set[str]:
    """The processes of one Steam scope, from the cgroup systemd names for it."""
    cg = profile.ask("usr", "show", "-P", "ControlGroup", scope).stdout.rstrip("\n")
    if not cg:
        return set()
    folder = f"{profile.paths.cgroot}{cg}"
    if not os.path.isdir(folder):
        return set()
    return _cgroup_procs(folder)


def _game_pids(profile: GameProfile) -> list[str]:
    seen = _gamemode_pids(profile.paths.proc)
    for scope in profile.steam_scopes():
        seen |= _scope_pids(profile, scope)
    return sorted(seen)


def _drm_device_map(sys_drm: str) -> tuple[dict[str, str], list[str]]:
    dev_to_card: dict[str, str] = {}
    cards: list[str] = []
    if not os.path.isdir(sys_drm):
        return dev_to_card, cards
    for entry in os.listdir(sys_drm):
        if _CARD_NAME_RE.fullmatch(entry) is None:
            continue
        cards.append(entry)
        dev_link = os.path.join(sys_drm, entry, "device")
        if os.path.exists(dev_link):
            dev_to_card[os.path.realpath(dev_link)] = entry
    cards.sort(key=_card_number)
    return dev_to_card, cards


def _connected_cards(sys_drm: str, cards: list[str]) -> list[str]:
    connected: list[str] = []
    for card in cards:
        card_dir = os.path.join(sys_drm, card)
        prefix = f"{card}-"
        for child in os.listdir(card_dir):
            if not child.startswith(prefix):
                continue
            if read_stripped(os.path.join(card_dir, child, "status")) == _CONNECTED_STATE:
                connected.append(card)
                break
    return connected


def _pid_cards(profile: GameProfile, pid: str, dev_to_card: dict[str, str]) -> set[str]:
    """The DRM cards whose render or card node one process holds open (none when its fds cannot be listed)."""
    fd_dir = os.path.join(profile.paths.proc, pid, "fd")
    try:
        entries = os.listdir(fd_dir)
    except OSError:
        return set()
    cards: set[str] = set()
    for fd_name in entries:
        try:
            target = os.readlink(os.path.join(fd_dir, fd_name))
        except OSError:
            continue
        m = _DRI_FD_RE.match(target)
        if m is None:
            continue
        dev_path = os.path.join(profile.paths.sys_drm, group(m, 1), "device")
        card = dev_to_card.get(os.path.realpath(dev_path))
        if card is not None:
            cards.add(card)
    return cards


def active_game_gpu_cards(profile: GameProfile) -> list[str]:
    """Return sorted DRM `cardN` names the game's processes render on; without any, the connected display
    cards; without those, the cards the Steam client's own processes (_STEAM_CLIENT_COMMS) hold."""
    sys_drm = profile.paths.sys_drm
    dev_to_card, cards = _drm_device_map(sys_drm)
    if not cards:
        return []
    matched: set[str] = set()
    client: set[str] = set()
    for pid in _game_pids(profile):
        comm = read_stripped(os.path.join(profile.paths.proc, pid, "comm")) or ""
        (client if comm in _STEAM_CLIENT_COMMS else matched).update(_pid_cards(profile, pid, dev_to_card))
    if matched:
        return sorted(matched, key=_card_number)
    return _connected_cards(sys_drm, cards) or sorted(client, key=_card_number)


def _keep_gpu_awake(profile: GameProfile, dev_dir: str) -> None:
    """power/control=on, its old value recorded first."""
    pm_path = os.path.join(dev_dir, "power", "control")
    if not os.access(pm_path, os.W_OK):
        return
    pm_old = read_stripped(pm_path) or ""
    if pm_old and pm_old != _GPU_PM_ON:
        _record_and_write_sys(profile, pm_path, pm_old, _GPU_PM_ON)


def _switch_amd_fullscreen(profile: GameProfile, dev_dir: str) -> None:
    """AMD DPM manual, then the 3D_FULL_SCREEN power profile, each old value recorded first."""
    dpm_path = os.path.join(dev_dir, "power_dpm_force_performance_level")
    prof_path = os.path.join(dev_dir, "pp_power_profile_mode")
    if not (os.access(dpm_path, os.W_OK) and os.access(prof_path, os.W_OK)):
        return
    if has_sys_record(profile.state_text(), prof_path):
        return
    current_idx, fullscreen_idx = parse_amd_profile_modes(read_text(prof_path) or "")
    # No current profile (no "*" row) means nothing to restore: DPM stays as it is, not manual.
    if current_idx is None or fullscreen_idx is None or current_idx == fullscreen_idx:
        return
    dpm_old = read_stripped(dpm_path) or ""
    if dpm_old and dpm_old != _DPM_MANUAL:
        _record_and_write_sys(profile, dpm_path, dpm_old, _DPM_MANUAL)
    if read_stripped(dpm_path) == _DPM_MANUAL:
        _record_and_write_sys(profile, prof_path, current_idx, fullscreen_idx)


def apply_game_gpu(profile: GameProfile) -> None:
    """Keep active game GPUs awake (`power/control=on`) and switch AMD DPM to `3D_FULL_SCREEN`."""
    pm_mode, _ = parse_gpu_pm_mode(profile.s.gpu_pm_mode)
    for card in active_game_gpu_cards(profile):
        dev_dir = os.path.join(profile.paths.sys_drm, card, "device")
        if pm_mode != GpuPmMode.OFF:
            _keep_gpu_awake(profile, dev_dir)
        _switch_amd_fullscreen(profile, dev_dir)

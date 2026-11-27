# SPDX-License-Identifier: MPL-2.0
"""`cst doctor` checks for `cst desktop` (`gnome localsearch` and `desktop:psd`)."""

from __future__ import annotations

from collections.abc import Iterator
import posixpath
from typing import Final

from cst_core.domain.units import parse_show_props
from cst_core.domain.values import ROOT_ACCOUNT, DomainValidationError, parse_whole_number
from desktop.domain import DESKTOP_CONF_REL, LOCALSEARCH_STATE_REL, PSD_RECOVERY_REL, PSD_RUN_ROOT, parse_desktop_config
from desktop.domain.localsearch import (
    KEY_MONITORS,
    KEY_RECURSIVE,
    KEY_SINGLE,
    LOCALSEARCH_UNITS,
    TRACKER_SCHEMA,
    LocalsearchHealth,
    LocalsearchSnapshot,
    classify_localsearch_health,
    parse_gvariant_bool,
    parse_gvariant_str_list,
    parse_localsearch_state_json,
    parse_systemd_user_show_map,
)
from desktop.domain.psd import (
    KNOWN_PROFILES,
    KnownProfile,
    PsdParsedMounts,
    parse_psd_mounts,
    resolve_profile_selection,
)
from doctor.application.shared import check, read_text
from doctor.domain import INFO, OK, WARN, HostPath, Result, host_join
from doctor.ports import CheckFn, DoctorHostPort

CHECKS: list[tuple[str, CheckFn]] = []

_PSD_SERVICE_UNIT: Final = "/etc/systemd/system/cst-desktop-psd@.service"
_PSD_TMPFS_WARN_PCT: Final = 80
_STATFS_FIELDS: Final = 2


def _host_home_variants(host: DoctorHostPort) -> tuple[str, ...]:
    """Return both the logical `host.home` and its symlink-resolved host path when distinct."""
    real_view = str(host.realpath(host.path(host.home)))
    root_prefix = host.root.rstrip("/")
    if not root_prefix:
        resolved = real_view
    elif real_view.startswith(root_prefix + "/"):
        resolved = real_view[len(root_prefix) :]
    elif real_view == root_prefix:
        resolved = "/"
    else:
        resolved = str(host.home)
    if resolved != str(host.home):
        return (str(host.home), resolved)
    return (str(host.home),)


def _statfs_used_pct(host: DoctorHostPort, target: str) -> int | None:
    rc, out, _ = host.run(["stat", "-f", "-c", "%b %a", target], timeout=10)
    if rc != 0:
        return None
    fields = out.split()
    if len(fields) != _STATFS_FIELDS:
        return None
    total = parse_whole_number(fields[0])
    avail = parse_whole_number(fields[1])
    if total is None or avail is None or total <= 0:
        return None
    used = max(0, total - avail)
    return (used * 100) // total


def _gsettings_list(host: DoctorHostPort, key: str) -> tuple[str, ...] | None:
    rc, out, _ = host.run(["gsettings", "get", TRACKER_SCHEMA, key], timeout=10)
    return parse_gvariant_str_list(out) if rc == 0 else None


def _gsettings_bool(host: DoctorHostPort, key: str) -> bool | None:
    rc, out, _ = host.run(["gsettings", "get", TRACKER_SCHEMA, key], timeout=10)
    return parse_gvariant_bool(out) if rc == 0 else None


def _user_unit_rows(host: DoctorHostPort) -> tuple[tuple[str, dict[str, str]], ...]:
    rows: list[tuple[str, dict[str, str]]] = []
    for unit in LOCALSEARCH_UNITS:
        rc, out, _ = host.run(
            ["systemctl", "--user", "show", "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState", unit],
            timeout=10,
        )
        rows.append((unit, parse_show_props(out) if rc == 0 else {}))
    return tuple(rows)


def _profile_symlink_blocked(host: DoctorHostPort, rel_path: str) -> bool:
    cur = str(host.home)
    for seg in rel_path.split("/"):
        if not seg:
            continue
        cur = posixpath.join(cur, seg)
        if host.islink(host.path(HostPath(cur))):
            return True
    return False


@check(CHECKS, "desktop")
def check_gnome_localsearch(host: DoctorHostPort) -> Iterator[Result]:
    """Check whether GNOME `localsearch-3` still indexes all of `$HOME` or matches CST's setting."""
    if host.uid == 0 or not str(host.home):
        return
    rc_sch, out_sch, _ = host.run(["gsettings", "list-schemas"], timeout=10)
    if rc_sch != 0 or TRACKER_SCHEMA not in out_sch.splitlines():
        return
    saved_raw = read_text(host, host.path(host_join(host.home, LOCALSEARCH_STATE_REL)), 65536)
    saved_state = None
    state_err = ""
    if saved_raw:
        try:
            saved_state = parse_localsearch_state_json(saved_raw)
        except ValueError as exc:
            state_err = str(exc)
    if state_err:
        yield Result(WARN, f"gnome localsearch: invalid state file ~/{LOCALSEARCH_STATE_REL}: {state_err}")
        return
    unit_rows = _user_unit_rows(host)
    snap = LocalsearchSnapshot(
        gsettings_available=True,
        schema_installed=True,
        recursive_dirs=_gsettings_list(host, KEY_RECURSIVE),
        single_dirs=_gsettings_list(host, KEY_SINGLE),
        enable_monitors=_gsettings_bool(host, KEY_MONITORS),
        units=parse_systemd_user_show_map(unit_rows),
        saved_state=saved_state,
        systemd_reachable=any(bool(props) for _, props in unit_rows),
    )
    health, summary = classify_localsearch_health(snap)
    if health == LocalsearchHealth.HOME_DEFAULT:
        yield Result(INFO, f"gnome localsearch: {summary}", f"{host.cst} desktop gnome localsearch apply")
    elif health == LocalsearchHealth.DRIFT:
        mode_flag = f" --mode {saved_state['mode']}" if saved_state is not None else ""
        yield Result(WARN, f"gnome localsearch: {summary}", f"{host.cst} desktop gnome localsearch apply{mode_flag}")
    elif health in (LocalsearchHealth.RESTRICTED_OK, LocalsearchHealth.DISABLED_OK):
        yield Result(OK, f"gnome localsearch: {summary}")


def _classify_psd_profiles(
    host: DoctorHostPort,
    selected: tuple[KnownProfile, ...],
    mounts: PsdParsedMounts,
    home_variants: tuple[str, ...],
) -> tuple[list[str], list[str], int, int]:
    symlink_blocked: list[str] = []
    conflict_mounts: list[str] = []
    active_cnt = 0
    healthy_target = 0
    for p in selected:
        variants = tuple(posixpath.join(h, p.rel_path) for h in home_variants)
        if _profile_symlink_blocked(host, p.rel_path):
            symlink_blocked.append(p.profile_id)
        elif any(v in mounts.other_mounts for v in variants):
            conflict_mounts.append(p.profile_id)
        elif any(v in mounts.overlay_targets for v in variants):
            active_cnt += 1
            healthy_target += 1
        elif host.isdir(host.path(host_join(host.home, p.rel_path))):
            healthy_target += 1
    return symlink_blocked, conflict_mounts, active_cnt, healthy_target


def _emit_psd_profile_results(
    host: DoctorHostPort,
    user: str,
    selected: tuple[KnownProfile, ...],
    mounts: PsdParsedMounts,
    *,
    pending: list[str],
) -> Iterator[Result]:
    home_variants = _host_home_variants(host)
    symlink_blocked, conflict_mounts, active_cnt, healthy_target = _classify_psd_profiles(
        host, selected, mounts, home_variants
    )
    if symlink_blocked:
        yield Result(
            WARN,
            f"desktop psd: profile folder uses a symlink ({', '.join(symlink_blocked)}); overlayfs refused",
        )
    if conflict_mounts:
        yield Result(
            WARN,
            f"desktop psd: non-CST mount over profile folder ({', '.join(conflict_mounts)})",
        )
    if (
        healthy_target > 0
        and active_cnt == healthy_target
        and not pending
        and not symlink_blocked
        and not conflict_mounts
    ):
        yield Result(OK, f"desktop psd: {active_cnt} profile overlay(s) active")
    elif healthy_target > 0 and active_cnt < healthy_target:
        yield Result(
            INFO,
            f"desktop psd: {active_cnt}/{healthy_target} profile overlay(s) mounted",
            f"{host.cst} desktop psd start --user {user}",
        )


@check(CHECKS, "desktop")
def check_desktop_psd(host: DoctorHostPort) -> Iterator[Result]:
    """Check `desktop:psd` overlayfs mounts, tmpfs headroom, and unmerged crash recovery snapshots."""
    if not host.exists(host.path(HostPath(_PSD_SERVICE_UNIT))):
        return
    user = host.user_name(host.uid)
    if host.uid == 0 or not user or user == ROOT_ACCOUNT or not str(host.home):
        return
    run_dir = posixpath.join(PSD_RUN_ROOT, str(host.uid))
    mounts_raw = read_text(host, host.path(HostPath("/proc/mounts")), 131072)
    mounts = parse_psd_mounts(mounts_raw, run_base=run_dir)
    conf_raw = read_text(host, host.path(host_join(host.home, DESKTOP_CONF_REL)), 65536)
    profiles_raw = "auto"
    if conf_raw:
        try:
            profiles_raw = parse_desktop_config(conf_raw, source=DESKTOP_CONF_REL).profiles_raw
        except DomainValidationError:
            yield Result(WARN, f"desktop psd: invalid ~/{DESKTOP_CONF_REL}")
            return

    home_variants = _host_home_variants(host)
    existing_rels = {
        p.rel_path
        for p in KNOWN_PROFILES
        if host.isdir(host.path(host_join(host.home, p.rel_path)))
        or any(posixpath.join(h, p.rel_path) in mounts.overlay_targets for h in home_variants)
        or host.isdir(host.path(host_join(host.home, PSD_RECOVERY_REL, p.profile_id)))
    }
    try:
        selected = resolve_profile_selection(profiles_raw, existing_rels=existing_rels)
    except DomainValidationError:
        yield Result(WARN, f"desktop psd: invalid PSD_PROFILES={profiles_raw!r} in ~/{DESKTOP_CONF_REL}")
        return

    pending = [
        p.profile_id
        for p in KNOWN_PROFILES
        if host.isdir(host.path(host_join(host.home, PSD_RECOVERY_REL, p.profile_id)))
    ]
    if pending:
        yield Result(
            WARN,
            f"desktop psd: unmerged crash-recovery snapshots ({', '.join(pending)})",
            f"{host.cst} desktop psd recover --user {user}",
        )
    if mounts.tmpfs_mounted:
        pct = _statfs_used_pct(host, run_dir)
        if pct is not None and pct >= _PSD_TMPFS_WARN_PCT:
            yield Result(
                WARN, f"desktop psd: {run_dir} tmpfs is {pct}% full", f"{host.cst} desktop psd sync --user {user}"
            )
    yield from _emit_psd_profile_results(host, user, selected, mounts, pending=pending)

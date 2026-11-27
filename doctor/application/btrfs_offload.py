# SPDX-License-Identifier: MPL-2.0
"""NVMe cache offload (`cst btrfs cache offload`) checks for `doctor/application/btrfs.py`."""

from __future__ import annotations

from collections.abc import Iterator
import posixpath

from btrfs.offload.domain import (
    OFFLOAD_CONF_REL,
    OFFLOAD_SCRATCHPAD_REL,
    OFFLOAD_STATE_REL,
    OffloadConfig,
    OffloadHealthState,
    parse_offload_config,
)
from btrfs.offload.domain.plan import (
    OffloadBackupStateRecord,
    OffloadEntryProbe,
    OffloadSnapshot,
    OffloadUnitState,
    PathProbe,
    ScratchpadProbe,
    assess_offload_health,
    parse_backup_record,
)
from btrfs.offload.domain.units import OFFLOAD_TIMER
from cst_core.domain.units import parse_show_props
from doctor.domain import INFO, OK, WARN, HostPath, Result, host_join
from doctor.ports import DoctorHostPort

_SEV_MAP = {
    OffloadHealthState.OK: OK,
    OffloadHealthState.INFO: INFO,
    OffloadHealthState.WARN: WARN,
}


def _strip_view_root(real_str: str, host_root: str) -> str:
    prefix = host_root.rstrip("/")
    return real_str.removeprefix(prefix) if prefix and real_str.startswith(prefix + "/") else real_str


def _doctor_path_probe(host: DoctorHostPort, hp: HostPath) -> PathProbe:
    vp = host.path(hp)
    if host.islink(vp):
        resolved = _strip_view_root(str(host.realpath(vp)), host.root)
        return PathProbe.link(str(hp), target=resolved, resolved=resolved)
    exists = host.exists(vp)
    is_dir = exists and host.isdir(vp)
    return PathProbe(
        path=str(hp), exists=exists, is_dir=is_dir, is_symlink=False, nonempty=is_dir and bool(host.listdir(vp))
    )


def _read_regular_optional(host: DoctorHostPort, hp: HostPath) -> tuple[str | None, bool, bool]:
    """Return `(text, exists, readable)` distinguishing missing vs empty vs unreadable."""
    vp = host.path(hp)
    if not (host.islink(vp) or host.exists(vp)):
        return None, False, True
    try:
        with host.open_regular_text(vp) as fh:
            return fh.read(65536), True, True
    except OSError:
        return None, True, False


def _load_doctor_config(host: DoctorHostPort, conf_hp: HostPath) -> tuple[OffloadConfig | None, str | None, bool]:
    raw_cfg, exists, readable = _read_regular_optional(host, conf_hp)
    if not exists:
        return None, None, False
    if not readable or raw_cfg is None:
        return None, f"{conf_hp}: unreadable", True
    cfg = parse_offload_config(raw_cfg, str(conf_hp))
    cfg_err = "; ".join(cfg.problems) if cfg.problems else None
    return cfg, cfg_err, True


def _load_doctor_backup_state(
    host: DoctorHostPort, state_hp: HostPath
) -> tuple[OffloadBackupStateRecord | None, str | None]:
    raw_state, exists, readable = _read_regular_optional(host, state_hp)
    if not exists:
        return None, None
    if not readable or raw_state is None:
        return None, f"{state_hp}: unreadable backup state"
    return parse_backup_record(raw_state)


def run_btrfs_offload_check(host: DoctorHostPort) -> Iterator[Result]:
    """Check `~/.config/cst/scratchpad-offload.toml`, symlinks, backup state, and user timer."""
    home_str = str(host.home)
    if not home_str:
        return
    cfg, cfg_err, conf_present = _load_doctor_config(host, host_join(host.home, OFFLOAD_CONF_REL))
    if not conf_present:
        return

    alt_home = _strip_view_root(str(host.realpath(host.path(host.home))), host.root)
    sp_hp = host_join(host.home, OFFLOAD_SCRATCHPAD_REL)
    sp_vp = host.path(sp_hp)
    sp_link = host.islink(sp_vp)
    scratchpad = ScratchpadProbe(
        path=str(sp_hp),
        exists=sp_link or host.exists(sp_vp),
        is_dir=not sp_link and host.isdir(sp_vp),
        is_symlink=sp_link,
    )

    probes = (
        tuple(
            OffloadEntryProbe(
                entry=e,
                source=_doctor_path_probe(host, HostPath(posixpath.join(home_str, e.source_rel))),
                scratchpad=_doctor_path_probe(host, HostPath(posixpath.join(home_str, e.scratchpad_rel))),
                mirror=_doctor_path_probe(host, HostPath(posixpath.join(home_str, e.mirror_rel))),
            )
            for e in cfg.entries
        )
        if cfg is not None
        else ()
    )

    backup_state, state_err = _load_doctor_backup_state(host, host_join(host.home, OFFLOAD_STATE_REL))

    timer_active, timer_enabled = ("", "")
    rc, out, _ = host.run(
        ["systemctl", "--user", "show", "-p", "ActiveState", "-p", "UnitFileState", OFFLOAD_TIMER],
        timeout=10,
        locale_c=True,
    )
    if rc == 0:
        props = parse_show_props(out)
        timer_active = props.get("ActiveState", "")
        timer_enabled = props.get("UnitFileState", "")

    snap = OffloadSnapshot(
        home=home_str,
        alt_home="" if alt_home == home_str else alt_home,
        config=cfg,
        config_error=cfg_err,
        scratchpad=scratchpad,
        entries=probes,
        backup_state=backup_state,
        backup_state_error=state_err,
        unit_state=OffloadUnitState(active=timer_active, enabled=timer_enabled, timer_checked=rc == 0),
    )
    prog = f"{host.cst} btrfs cache offload"
    for sev, msg, fix in assess_offload_health(snap, now_ts=host.now, prog=prog):
        yield Result(_SEV_MAP[sev], f"btrfs offload: {msg}", fix)

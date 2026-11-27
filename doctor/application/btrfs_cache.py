# SPDX-License-Identifier: MPL-2.0
"""NVMe cache drive (`cst btrfs cache`) checks for `doctor/application/btrfs.py`."""

from __future__ import annotations

from collections.abc import Iterator

from btrfs.cache.domain import parse_all_mounts, parse_managed_fstab_entries
from btrfs.cache.domain.health import CacheMountFindingSeverity, assess_cache_mount_health, dm_device_names
from btrfs.cache.domain.provision import CACHE_CONF_PATH, parse_cache_conf
from btrfs.cache.domain.system import parse_system_fstab_entries
from btrfs.cache.domain.systemd_view import MOUNT_UNIT_PROPS, fstab_lines_unread
from cst_core.domain.units import parse_show_units, show_units_argv
from doctor.application.findmnt import findmnt_all_mounts
from doctor.application.shared import read_text
from doctor.domain import FSTAB, INFO, OK, WARN, HostPath, Result
from doctor.ports import DoctorHostPort


def _fstab_loaded_results(host: DoctorHostPort, fstab_raw: str) -> Iterator[Result]:
    """WARN when systemd has not loaded /etc/fstab as it is on disk."""
    rc, out, _ = host.run(show_units_argv(["systemctl"], "*.mount", MOUNT_UNIT_PROPS), timeout=20, locale_c=True)
    if rc != 0:
        return
    targets = {e.target for e in parse_managed_fstab_entries(fstab_raw)} | {
        e.target for e in parse_system_fstab_entries(fstab_raw)
    }
    if fstab_lines_unread(parse_show_units(out), targets) is True:
        yield Result(
            WARN,
            "btrfs cache: systemd has not loaded /etc/fstab as it is on disk (an edit, or a "
            "`cst btrfs cache attach` or `detach` that stopped before its daemon-reload), so it keeps the mounts "
            "of the /etc/fstab it loaded before",
            "sudo systemctl daemon-reload",
        )


def _cache_conf_results(host: DoctorHostPort) -> tuple[bool, tuple[Result, ...]]:
    try:
        with host.open_regular_text(host.path(HostPath(CACHE_CONF_PATH))) as fh:
            conf_raw = fh.read(65536)
    except FileNotFoundError, NotADirectoryError:
        return False, ()
    except PermissionError:
        return True, (Result(INFO, f"btrfs cache: {CACHE_CONF_PATH} is not readable as this user"),)
    except OSError:
        return True, (Result(WARN, f"btrfs cache: {CACHE_CONF_PATH} is unreadable or not a regular file"),)

    parsed = parse_cache_conf(conf_raw)
    if parsed.problems or parsed.spec is None:
        detail = "; ".join(parsed.problems) if parsed.problems else "invalid configuration"
        return True, (Result(WARN, f"btrfs cache: {CACHE_CONF_PATH}: {detail}"),)
    spec = parsed.spec
    if not host.exists(host.path(HostPath(spec.device_path))):
        return True, (
            Result(
                WARN,
                f"btrfs cache: {CACHE_CONF_PATH} names {spec.device_path} which is missing",
            ),
        )
    return True, (Result(OK, f"btrfs cache: {CACHE_CONF_PATH} ({spec.device_path}, mapper={spec.mapper_name})"),)


def run_btrfs_cache_check(host: DoctorHostPort, *, has_sysfs_btrfs: bool) -> Iterator[Result]:
    """Validate `/etc/cst/btrfs-cache.conf`, cache mounts, crypttab `nofail`, and user bind mounts."""
    has_conf, conf_rows = _cache_conf_results(host)
    yield from conf_rows
    fstab_raw = read_text(host, host.path(FSTAB), 65536)
    proc_raw = read_text(host, host.path(HostPath("/proc/mounts")), 65536)
    hint_text = f"{fstab_raw}\n{proc_raw}"
    has_hints = any(k in hint_text for k in ("/var/mnt/cache", "/var/mnt/scratchpad", "btrfs-cache"))
    if not (has_hints or has_conf or has_sysfs_btrfs):
        return
    crypttab_path = host.path(HostPath("/etc/crypttab"))
    crypttab_raw, crypttab_unreadable = "", False
    try:
        with host.open_regular_text(crypttab_path) as fh:
            crypttab_raw = fh.read(65536)
    except PermissionError:
        crypttab_unreadable = True
    except OSError:
        pass
    live_findmnt = findmnt_all_mounts(host)
    mounts_text = f"{proc_raw}\n{live_findmnt}" if live_findmnt is not None else proc_raw
    sev_map = {
        CacheMountFindingSeverity.OK: OK,
        CacheMountFindingSeverity.INFO: INFO,
        CacheMountFindingSeverity.WARN: WARN,
    }
    sources = [m.source for m in parse_all_mounts(f"{mounts_text}\n{fstab_raw}")]
    dm_names = {
        dm: name
        for dm in sorted(dm_device_names(sources))
        if (name := read_text(host, host.path(HostPath(f"/sys/class/block/{dm}/dm/name"))))
    }
    for finding in assess_cache_mount_health(fstab_raw or None, mounts_text or None, crypttab_raw or None, dm_names):
        fix_cmd = f"{host.cst} {finding.fix}" if finding.fix else ""
        yield Result(sev_map[finding.severity], f"btrfs cache: {finding.message}", fix_cmd)
    if has_hints:
        yield from _fstab_loaded_results(host, fstab_raw)
    if crypttab_unreadable and has_hints:
        yield Result(INFO, "btrfs cache: /etc/crypttab is not readable as this user (nofail not checked)")

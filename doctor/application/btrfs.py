# SPDX-License-Identifier: MPL-2.0
"""The btrfs area of `cst doctor`: chunk allocation, the weekly scrub's speed limit, the snapshot settings,
snapshots left writable, Docker's postponed NOCOW."""

from collections.abc import Iterator, Mapping
import os
import re
import shlex
import time

from btrfs.docker_volumes_nocow.domain import DAEMON_JSON, is_config_reason, is_stored_postponed
from cst_core.domain.shapes import parse_json
from cst_core.domain.text import ShellAssignments, parse_shell_assignments
from cst_core.domain.values import GIB, format_bytes_gib_mib
from doctor.application.shared import check, conf_value, read_text, settings_file_warnings
from doctor.domain import (
    BTRFS_SYSFS,
    ERROR,
    FS_UUID,
    HEALTH_CHECK_HOST,
    INFO,
    MIRROR_COPIES,
    NOCOW_POSTPONED,
    NOCOW_WARN_BOOTS,
    OK,
    ONE_DEVICE_FACTOR,
    RECLAIM_CONF,
    RESIZE_SLACK,
    SCRUB_DROPIN,
    SCRUB_UNIT,
    SNAP_CONF,
    SNAP_STATE,
    SNAP_UNFINISHED,
    SNAP_WRITABLE,
    UNALLOC_DEFAULTS,
    WARN,
    HostPath,
    Result,
    name_list,
    parse_unalloc_thresholds_text,
    per_device_allocated,
    pick_mount,
    scrub_dropin_limit,
    scrub_limit_lines,
    scrub_limit_setting,
    view_join,
    ViewPath,
)
from doctor.ports import CheckFn, DoctorHostPort

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []


# Where btrfs-health-check's levels are: in the checkout (host.repo_path gives where it is), and the same
# module inside the installed zipapp.
HEALTH_CHECK_SRC = "btrfs/health_check/domain.py"


# ---- btrfs: chunk allocation ---------------------------------------------------------------------
def unalloc_thresholds(host: DoctorHostPort, *, health_check_repo: str | None = None) -> tuple[int, int]:
    """(warn_gib, crit_gib) from btrfs-health-check: the installed copy (the one
    that alerts: the levels module inside its zipapp, or the text of a bash
    version), else the checkout's; the defaults if none reads. An installed
    copy older than this checkout shows in the install area."""
    installed = host.path(HEALTH_CHECK_HOST)
    repo_path = host.repo_path(HEALTH_CHECK_SRC) if health_check_repo is None else ViewPath(health_check_repo)

    def text_of(path: ViewPath) -> str:
        with host.open_text(path) as f:
            return f.read(65536)

    readers = (
        lambda: host.zip_text(installed, HEALTH_CHECK_SRC, 65536),
        lambda: text_of(installed),
        lambda: text_of(repo_path),
    )
    for read in readers:
        try:
            text = read()
        except OSError:
            continue
        parsed = parse_unalloc_thresholds_text(text)
        if parsed is not None:
            return parsed
    return UNALLOC_DEFAULTS


def read_int(host: DoctorHostPort, path: ViewPath) -> int | None:
    try:
        with host.open_text(path) as f:
            v = f.read(64).strip()
    except OSError:
        return None
    return int(v) if v.isdigit() else None


def btrfs_mounts(host: DoctorHostPort) -> dict[str, list[str]]:
    """{block device name (dm-1, nvme0n1p3): [mount points]}, from findmnt
    on the host; {} when findmnt does not answer."""
    rc, out, _ = host.run(["findmnt", "-rn", "-t", "btrfs", "-o", "SOURCE,TARGET"], timeout=15)
    if rc != 0:
        return {}
    mounts: dict[str, list[str]] = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) != 2:
            continue
        src = re.sub(r"\[.*\]$", "", f[0])
        target = f[1].replace("\\x20", " ")
        dev = os.path.basename(host.realpath(host.path(HostPath(src)))) if src.startswith("/dev/") else ""
        if dev:
            mounts.setdefault(dev, [])
            if target not in mounts[dev]:
                mounts[dev].append(target)
    return mounts


def btrfs_profiles(host: DoctorHostPort, alloc: ViewPath) -> dict[str, dict[str, int]]:
    """{kind: {profile: logical chunk bytes}} for data, metadata, system."""
    out: dict[str, dict[str, int]] = {}
    for kind in ("data", "metadata", "system"):
        d = view_join(alloc, kind)
        prof: dict[str, int] = {}
        try:
            names = sorted(host.listdir(d))
        except OSError:
            names = []
        for name in names:
            total = read_int(host, view_join(d, name, "total_bytes"))
            if total is not None and host.isdir(view_join(d, name)):
                prof[name] = total
        out[kind] = prof
    return out


def btrfs_device_bytes(host: DoctorHostPort, mnt: str, profiles: Mapping[str, Mapping[str, int]]) -> int | None:
    """Sum of the btrfs device sizes (what btrfs uses of each block device),
    from statfs on the mount: btrfs reports f_blocks as that sum divided by
    the copies of the data profile. None when it cannot be read or the data
    profile is not one known value."""
    data = list(profiles.get("data", {}))
    factor = {**ONE_DEVICE_FACTOR, **MIRROR_COPIES}.get(data[0]) if len(data) == 1 else None
    if not mnt or factor is None:
        return None
    rc, out, _ = host.run(["stat", "-f", "-c", "%S %b", mnt], timeout=15)
    f = out.split() if rc == 0 else []
    if len(f) != 2 or not all(x.isdigit() for x in f):
        return None
    return int(f[0]) * int(f[1]) * factor


def btrfs_devices(host: DoctorHostPort, base: ViewPath) -> tuple[list[tuple[str, str, int]], str]:
    """([(name, label, size bytes)], problem): the members, from
    devices/ (block size in 512-byte sectors) and devinfo/ (missing flag)."""
    try:
        names = sorted(host.listdir(view_join(base, "devices")))
        ids = sorted(host.listdir(view_join(base, "devinfo")))
    except OSError:
        return [], "no device list in sysfs"
    if any(read_int(host, view_join(base, "devinfo", i, "missing")) == 1 for i in ids):
        return [], "a member device is missing"
    if len(names) != len(ids):
        return [], f"{len(ids)} members but {len(names)} device(s) present"
    devs: list[tuple[str, str, int]] = []
    for n in names:
        sectors = read_int(host, view_join(base, "devices", n, "size"))
        if sectors is None or sectors == 0:
            return [], f"no size for {n}"
        dm = read_text(host, view_join(base, "devices", n, "dm", "name"))
        devs.append((n, f"/dev/mapper/{dm}" if dm else f"/dev/{n}", sectors * 512))
    return devs, ""


@check(CHECKS, "btrfs")
def check_btrfs_allocation(host: DoctorHostPort, *, health_check_repo: str | None = None) -> Iterator[Result]:
    sysfs = host.path(BTRFS_SYSFS)
    try:
        uuids = sorted(
            n for n in host.listdir(sysfs) if FS_UUID.match(n) and host.isdir(view_join(sysfs, n, "allocation"))
        )
    except OSError:
        uuids = []
    if not uuids:
        yield Result(INFO, f"allocation: no mounted btrfs filesystem ({BTRFS_SYSFS} has none)")
        return
    warn_gib, crit_gib = unalloc_thresholds(host, health_check_repo=health_check_repo)
    mounts = btrfs_mounts(host)
    conf = read_text(host, host.path(RECLAIM_CONF), 65536)
    readme = "btrfs/README.md, section 13: 'No space left' but df shows free space"
    for uuid in uuids:
        base = view_join(sysfs, uuid)
        alloc = view_join(base, "allocation")
        devs, dev_problem = btrfs_devices(host, base)
        targets: list[str] = []
        try:
            for n in host.listdir(view_join(base, "devices")):
                targets += [t for t in mounts.get(n, []) if t not in targets]
        except OSError:
            pass
        mnt = pick_mount(targets)
        name = f"btrfs {mnt}" if mnt else f"btrfs {uuid[:8]}"
        where = mnt or "MOUNTPOINT"
        system = bool({"/", "/var", "/sysroot"} & set(targets))

        # How full the chunks are (logical bytes: one copy).
        fill: dict[str, tuple[int, int] | None] = {}
        for kind in ("data", "metadata"):
            total = read_int(host, view_join(alloc, kind, "total_bytes"))
            used = read_int(host, view_join(alloc, kind, "bytes_used"))
            fill[kind] = (used, total) if total is not None and total != 0 and used is not None else None
        profiles = btrfs_profiles(host, alloc)
        layout = "/".join(sorted({p.upper() for prof in profiles.values() for p in prof})) or "?"
        parts: list[str] = []
        for kind in ("data", "metadata"):
            pair = fill[kind]
            if pair:
                u, t = pair
                parts.append(
                    f"{kind} chunks {format_bytes_gib_mib(u)} used of {format_bytes_gib_mib(t)} allocated ({100 * u // t}%)"
                )
        if fill["data"] and fill["metadata"] and fill["data"][0]:
            parts.append(f"metadata is {100 * fill['metadata'][0] / fill['data'][0]:.1f}% of data")
        if parts:
            yield Result(OK, f"{name} ({layout}): " + ", ".join(parts))
        else:
            yield Result(INFO, f"{name}: chunk usage not readable in sysfs")

        # Automatic reclaim of half-empty data chunks (Linux 6.11+, set by cst btrfs install).
        dyn = read_int(host, view_join(alloc, "data", "dynamic_reclaim"))
        per = read_int(host, view_join(alloc, "data", "periodic_reclaim"))
        configured = f"/sys/fs/btrfs/{uuid}/allocation/data/" in conf
        if dyn is None or per is None:
            yield Result(INFO, f"{name}: this kernel has no automatic reclaim switches (Linux 6.11 or later)")
        elif dyn == 1 and per == 1:
            count = read_int(host, view_join(alloc, "data", "reclaim_count"))
            errs = read_int(host, view_join(alloc, "data", "reclaim_errors"))
            extra = f": {count} chunk(s) reclaimed since mount" if count is not None else ""
            if errs is not None and errs != 0:
                yield Result(
                    INFO,
                    f"{name}: automatic data reclaim on (dynamic, periodic){extra}, "
                    f"{errs} failed (journalctl -k -g BTRFS)",
                )
            else:
                yield Result(OK, f"{name}: automatic data reclaim on (dynamic, periodic){extra}")
        else:
            off = ", ".join(k for k, v in (("dynamic_reclaim", dyn), ("periodic_reclaim", per)) if v != 1)
            if configured or system:
                why = "configured by CST but switched off" if configured else "not set up"
                yield Result(
                    WARN,
                    f"{name}: automatic data reclaim is {why} ({off} = 0): btrfs then never "
                    "compacts half-empty data chunks by itself as unallocated space runs out",
                    f"{host.cst} btrfs install",
                )
            else:
                yield Result(
                    INFO, f"{name}: automatic data reclaim off ({off}); CST sets it up only for the root filesystem"
                )

        # Unallocated space, per device.
        limits = f"warning below {warn_gib} GiB, critical below {crit_gib} GiB"
        needs_root = (
            f"per-device unallocated needs root: sudo btrfs filesystem usage -T {where} "
            "(the hourly btrfs-health-check watches it)"
        )
        if not devs:
            yield Result(INFO, f"{name}: {needs_root}; not estimated: {dev_problem}")
            continue
        each = per_device_allocated(profiles, len(devs))
        if each is not None and all(size >= each for _, _, size in devs):
            fs_bytes = btrfs_device_bytes(host, mnt, profiles)
            block_bytes = sum(size for _, _, size in devs)
            exact = fs_bytes is not None and abs(block_bytes - fs_bytes) <= RESIZE_SLACK
            if fs_bytes is None:
                unknown = f"the filesystem size could not be read (stat -f {where})"
            elif block_bytes > fs_bytes:
                unknown = (
                    f"the btrfs devices use {format_bytes_gib_mib(block_bytes - fs_bytes)} less than their block "
                    "devices (shrunk with btrfs filesystem resize?)"
                )
            else:
                unknown = "the filesystem size from statfs does not match its block devices"
            free = [(label, size - each) for _, label, size in devs]
            low = min(b for _, b in free)
            listed = ", ".join(f"{label} {format_bytes_gib_mib(b)}" for label, b in free)
            if not exact:
                listed = "at most " + listed
            meta_free = (fill["metadata"][1] - fill["metadata"][0]) if fill["metadata"] else None
            rsv = read_int(host, view_join(alloc, "global_rsv_size")) or 0
            if low < crit_gib * GIB:
                yield Result(
                    ERROR,
                    f"{name}: CRITICAL: unallocated per device {listed} ({limits}): "
                    f"ENOSPC is close. Free space first, then a filtered data balance; never a "
                    f"metadata or full balance. Steps: {readme}",
                )
            elif low < warn_gib * GIB:
                yield Result(
                    WARN,
                    f"{name}: little unallocated space per device: {listed} ({limits}; "
                    f"metadata grows only from it). Free space; then, if it stays low, "
                    f"see {readme}",
                )
            elif exact:
                yield Result(
                    OK,
                    f"{name}: unallocated per device {listed} ({limits}; from sysfs, "
                    "each btrfs device fills its block device)",
                )
            else:
                yield Result(INFO, f"{name}: {needs_root}; not estimated: {unknown}")
            if meta_free is not None and meta_free < 2 * rsv and low < 2 * GIB:
                yield Result(
                    ERROR,
                    f"{name}: metadata almost full ({meta_free // 2**20} MiB free in "
                    f"metadata chunks, global reserve {rsv // 2**20} MiB): writes may fail "
                    f"with ENOSPC soon. Steps: {readme}",
                )
            continue
        # Mixed or striped layout: only the sum over all devices is exact.
        disk_total = 0
        for kind in ("data", "metadata", "system"):
            disk_total += read_int(host, view_join(alloc, kind, "disk_total")) or 0
        total_free = sum(size for _, _, size in devs) - disk_total
        if disk_total and total_free < warn_gib * GIB * len(devs):
            yield Result(
                WARN,
                f"{name}: {format_bytes_gib_mib(total_free)} unallocated over {len(devs)} device(s), so at least "
                f"one device has less than {warn_gib} GiB ({limits}); {needs_root}. "
                f"Steps: {readme}",
            )
        elif disk_total:
            yield Result(
                INFO,
                f"{name}: {format_bytes_gib_mib(total_free)} unallocated over {len(devs)} device(s) (layout {layout}); {needs_root}",
            )
        else:
            yield Result(INFO, f"{name}: {needs_root}")


# ---- btrfs: snapshots left writable & docker NOCOW -----------------------------------------------
def read_entries(host: DoctorHostPort, path: HostPath) -> list[str] | None:
    """The non-empty lines of a small state file: [] if missing, None if unreadable."""
    try:
        with host.open_text(host.path(path)) as f:
            return [ln.strip() for ln in f.read(65536).splitlines() if ln.strip()]
    except FileNotFoundError:
        return []
    except OSError:
        return None


def live_scrub_limits(host: DoctorHostPort) -> str:
    """scrub_speed_max of every device of every mounted btrfs, as one phrase."""
    sysfs = host.path(BTRFS_SYSFS)
    limits: list[str] = []
    try:
        uuids = sorted(n for n in host.listdir(sysfs) if FS_UUID.match(n))
    except OSError:
        uuids = []
    for uuid in uuids:
        devinfo = view_join(sysfs, uuid, "devinfo")
        try:
            devids = sorted(host.listdir(devinfo), key=lambda d: (len(d), d))
        except OSError:
            continue
        for devid in devids:
            v = read_int(host, view_join(devinfo, devid, "scrub_speed_max"))
            if v is not None and v != 0:
                limits.append(f"{uuid[:8]} device {devid} {format_bytes_gib_mib(v)}/s")
    return ", ".join(limits) if limits else "no limit on any device"


def scrub_settings(host: DoctorHostPort) -> ShellAssignments:
    """The btrfs settings file as `cst btrfs install` reads SCRUB_LIMIT from it: the whole file with its
    line ends, as bash reads it ("\r" is part of a value there)."""
    try:
        with host.open_bytes(host.path(SNAP_CONF)) as f:
            conf = f.read().decode("utf-8", "replace")
    except OSError:
        conf = ""
    return parse_shell_assignments(conf)


@check(CHECKS, "btrfs")
def check_scrub_limit(host: DoctorHostPort) -> Iterator[Result]:
    """The weekly scrub's speed limit: SCRUB_LIMIT, the service's drop-in, sysfs now."""
    settings = scrub_settings(host)
    setting, problem = scrub_limit_setting(settings)
    # Every SCRUB_LIMIT problem line (check_snapshot_settings leaves them to this row), else the value's.
    for why in [str(p) for p in scrub_limit_lines(settings)] or ([problem] if problem else []):
        yield Result(
            WARN,
            f"{SNAP_CONF} {why}: `cst btrfs install` refuses to run until it is fixed (btrfs/README.md, 4.3)",
        )
    if not host.exists(host.path(SCRUB_UNIT)):
        return
    installed = scrub_dropin_limit(read_text(host, host.path(SCRUB_DROPIN), 4096))
    if not problem and installed != setting:
        yield Result(
            WARN,
            f"weekly scrub: speed limit {installed or 'none'} in the service but {setting or 'none'} in "
            f"SCRUB_LIMIT of {SNAP_CONF}: run `cst btrfs install`",
        )
    limit = f"{installed} per device" if installed else "none (full speed; SCRUB_LIMIT, btrfs/README.md 4.3)"
    yield Result(INFO, f"weekly scrub: speed limit {limit}; now in sysfs: {live_scrub_limits(host)}")


@check(CHECKS, "btrfs")
def check_snapshot_settings(host: DoctorHostPort) -> Iterator[Result]:
    # btrfs-snapshot reads the file as data too (bash's rules, its own keys): it ignores the line. A
    # SCRUB_LIMIT problem line that check_scrub_limit reports (the same line, read the same) is warned there.
    scrub = set(scrub_limit_lines(scrub_settings(host)))
    yield from settings_file_warnings(
        host,
        SNAP_CONF,
        bash_sourced=True,
        note=" (btrfs-snapshot ignores it too, with a warning in its journal)",
        skip=scrub.__contains__,
    )


@check(CHECKS, "btrfs")
def check_snapshots_writable(host: DoctorHostPort) -> Iterator[Result]:
    if not host.isdir(host.path(SNAP_STATE)):
        return  # snapshots never set up
    unfinished = read_entries(host, SNAP_UNFINISHED)
    writable = read_entries(host, SNAP_WRITABLE)
    if unfinished is None or writable is None:
        yield Result(INFO, f"snapshots: cannot read {SNAP_UNFINISHED} or {SNAP_WRITABLE}")
        return
    snapdir = os.path.join(
        conf_value(host, SNAP_CONF, "TOP") or "/var/mnt/btrfs-top",
        conf_value(host, SNAP_CONF, "SNAP_DIR_NAME") or "snapshots",
    )
    if unfinished:
        yield Result(
            WARN,
            f"snapshots: {name_list(unfinished)} left writable by an interrupted run (cache "
            f"folders perhaps not dropped yet); the next hourly run finishes it (if this stays: "
            f"journalctl -u btrfs-snapshot)",
            "sudo systemctl start btrfs-snapshot.service",
        )
    foreign = [e for e in writable if e not in unfinished]
    if foreign:
        yield Result(
            WARN,
            f"snapshots: {name_list(foreign)} in {snapdir}: writable, named like CST's "
            f"snapshots, but not left by a CST run; never deleted by the retention and not counted: "
            f"look at it, then delete or rename it",
            f"sudo btrfs subvolume show {shlex.quote(os.path.join(snapdir, foreign[0]))}",
        )
    if not unfinished and not foreign:
        yield Result(OK, "snapshots: none left writable")


@check(CHECKS, "btrfs")
def check_docker_nocow_postponed(host: DoctorHostPort) -> Iterator[Result]:
    try:
        with host.open_text(host.path(NOCOW_POSTPONED)) as f:
            rec = parse_json(f.read(65536))
    except FileNotFoundError:
        return  # never postponed, or not set up
    except OSError, ValueError:
        rec = None
    if not is_stored_postponed(rec):
        yield Result(INFO, f"Docker volumes NOCOW: cannot read {NOCOW_POSTPONED}")
        return
    boots, last = rec.get("boots"), rec.get("last")
    if type(boots) is not int or boots < 1:
        boots = 1
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if type(last) is int else "at an unknown time"
    reason = str(rec.get("reason") or "no reason recorded")
    msg = f"Docker volumes NOCOW: the boot-time migration was postponed at {boots} boot(s) in a row, last {when}: {reason}"
    if boots >= NOCOW_WARN_BOOTS and is_config_reason(reason):
        # Waiting for a quiet boot does not help: only a fixed file lets the migration run.
        yield Result(
            WARN,
            f"{msg}; fix {DAEMON_JSON} (the fix line checks it as dockerd reads it), and the next boot runs it",
            f"sudo dockerd --validate --config-file {DAEMON_JSON}",
        )
    elif boots >= NOCOW_WARN_BOOTS:
        yield Result(
            WARN,
            f"{msg}; the volumes keep copy-on-write data (and new ones may too) until a boot where nothing uses them",
            "journalctl -b -u docker-volumes-nocow",
        )
    else:
        yield Result(INFO, f"{msg}; tried again at the next boot")

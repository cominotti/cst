# SPDX-License-Identifier: MPL-2.0
"""The btrfs area of `cst doctor`: chunk allocation, the weekly scrub's speed limit, the snapshot settings,
snapshots left writable, Docker's postponed NOCOW."""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import os
import re
import shlex
import time
from typing import Final, NamedTuple

from btrfs.commit.domain import (
    DEFAULT_TARGET_COMMIT_S,
    ROOTFLAGS_PREFIX as COMMIT_ROOTFLAGS_PREFIX,
    assess_commit_coherence,
    effective_commit_seconds,
)
from btrfs.compress.domain import (
    OSTREE_BOOTED_PATH,
    TOP_MOUNT_UNIT_PATH,
    CoherenceReport,
    CompressMode,
    CompressSnapshot,
    assess_coherence,
    extract_rootflags_tokens,
    parse_compress_spec,
    parse_fstab_root_entries,
    parse_proc_mounts,
    parse_top_mount_unit,
    pick_root_mounts,
)
from btrfs.docker_volumes_nocow.domain import DAEMON_JSON, is_config_reason, is_stored_postponed
from btrfs.cache.domain import parse_all_mounts
from btrfs.cache.domain.health import CacheMountFindingSeverity, assess_cache_mount_health, dm_device_names
from cst_core.domain.shapes import parse_json
from cst_core.domain.text import YES_WORDS, ShellAssignments, group, parse_shell_assignments, unescape_mount_field
from cst_core.domain.values import GIB, format_bytes_gib_mib
from doctor.application.cpu import unit_props
from doctor.application.shared import (
    check,
    conf_value,
    ignored_line_text,
    read_text,
    settings_file_warnings,
    settings_of,
    snapshots_conf_text,
)
from doctor.domain import (
    BTRFS_SYSFS,
    ERROR,
    FS_UUID,
    FSTAB,
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
    ScrubLimitLines,
    scrub_limit_lines_both,
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


# Where the ENOSPC lines send the reader for the steps.
ENOSPC_STEPS: Final = "btrfs/README.md, section 13: 'No space left' but df shows free space"


class _Levels(NamedTuple):
    """btrfs-health-check's unallocated levels, in GiB."""

    warn_gib: int
    crit_gib: int

    @property
    def limits(self) -> str:
        return f"warning below {self.warn_gib} GiB, critical below {self.crit_gib} GiB"


@dataclass(frozen=True)
class _Fs:
    """What the allocation check reads of one mounted btrfs before it judges it."""

    uuid: str
    alloc: ViewPath
    mnt: str
    name: str
    where: str
    system: bool
    devs: list[tuple[str, str, int]]
    dev_problem: str
    fill: dict[str, tuple[int, int] | None]
    profiles: dict[str, dict[str, int]]
    layout: str

    @property
    def needs_root(self) -> str:
        return (
            f"per-device unallocated needs root: sudo btrfs filesystem usage -T {self.where} "
            "(the hourly btrfs-health-check watches it)"
        )


def _fs_facts(host: DoctorHostPort, sysfs: ViewPath, uuid: str, mounts: Mapping[str, list[str]]) -> _Fs:
    """One filesystem's devices, mount point, chunk fill and profiles, read in this order."""
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
    # How full the chunks are (logical bytes: one copy).
    fill: dict[str, tuple[int, int] | None] = {}
    for kind in ("data", "metadata"):
        total = read_int(host, view_join(alloc, kind, "total_bytes"))
        used = read_int(host, view_join(alloc, kind, "bytes_used"))
        fill[kind] = (used, total) if total is not None and total != 0 and used is not None else None
    profiles = btrfs_profiles(host, alloc)
    return _Fs(
        uuid=uuid,
        alloc=alloc,
        mnt=mnt,
        name=f"btrfs {mnt}" if mnt else f"btrfs {uuid[:8]}",
        where=mnt or "MOUNTPOINT",
        system=bool({"/", "/var", "/sysroot"} & set(targets)),
        devs=devs,
        dev_problem=dev_problem,
        fill=fill,
        profiles=profiles,
        layout="/".join(sorted({p.upper() for prof in profiles.values() for p in prof})) or "?",
    )


def _chunk_fill_result(fs: _Fs) -> Result:
    """How full the data and metadata chunks are."""
    parts: list[str] = []
    for kind in ("data", "metadata"):
        pair = fs.fill[kind]
        if pair:
            u, t = pair
            parts.append(
                f"{kind} chunks {format_bytes_gib_mib(u)} used of {format_bytes_gib_mib(t)} allocated ({100 * u // t}%)"
            )
    data, meta = fs.fill["data"], fs.fill["metadata"]
    if data and meta and data[0]:
        parts.append(f"metadata is {100 * meta[0] / data[0]:.1f}% of data")
    if parts:
        return Result(OK, f"{fs.name} ({fs.layout}): " + ", ".join(parts))
    return Result(INFO, f"{fs.name}: chunk usage not readable in sysfs")


def _reclaim_result(host: DoctorHostPort, fs: _Fs, conf: str) -> Result:
    """Automatic reclaim of half-empty data chunks (Linux 6.11+, set by cst btrfs install)."""
    dyn = read_int(host, view_join(fs.alloc, "data", "dynamic_reclaim"))
    per = read_int(host, view_join(fs.alloc, "data", "periodic_reclaim"))
    if dyn is None or per is None:
        return Result(INFO, f"{fs.name}: this kernel has no automatic reclaim switches (Linux 6.11 or later)")
    if dyn == 1 and per == 1:
        return _reclaim_on_result(host, fs)
    off = ", ".join(k for k, v in (("dynamic_reclaim", dyn), ("periodic_reclaim", per)) if v != 1)
    configured = f"/sys/fs/btrfs/{fs.uuid}/allocation/data/" in conf
    if configured or fs.system:
        why = "configured by CST but switched off" if configured else "not set up"
        return Result(
            WARN,
            f"{fs.name}: automatic data reclaim is {why} ({off} = 0): btrfs then never "
            "compacts half-empty data chunks by itself as unallocated space runs out",
            f"{host.cst} btrfs install",
        )
    return Result(INFO, f"{fs.name}: automatic data reclaim off ({off}); CST sets it up only for the root filesystem")


def _reclaim_on_result(host: DoctorHostPort, fs: _Fs) -> Result:
    """Both reclaim switches on: how many chunks it reclaimed, and whether some failed."""
    count = read_int(host, view_join(fs.alloc, "data", "reclaim_count"))
    errs = read_int(host, view_join(fs.alloc, "data", "reclaim_errors"))
    extra = f": {count} chunk(s) reclaimed since mount" if count is not None else ""
    if errs is not None and errs != 0:
        return Result(
            INFO,
            f"{fs.name}: automatic data reclaim on (dynamic, periodic){extra}, {errs} failed (journalctl -k -g BTRFS)",
        )
    return Result(OK, f"{fs.name}: automatic data reclaim on (dynamic, periodic){extra}")


def _unallocated_results(host: DoctorHostPort, fs: _Fs, levels: _Levels) -> Iterator[Result]:
    """Unallocated space, per device: exact when each device holds one copy of every chunk, else the sum."""
    if not fs.devs:
        yield Result(INFO, f"{fs.name}: {fs.needs_root}; not estimated: {fs.dev_problem}")
        return
    each = per_device_allocated(fs.profiles, len(fs.devs))
    if each is not None and all(size >= each for _, _, size in fs.devs):
        yield from _unallocated_exact(host, fs, each, levels)
    else:
        yield _unallocated_striped(host, fs, levels)


def _size_unknown(fs: _Fs, fs_bytes: int | None, block_bytes: int) -> str:
    """Why the per-device numbers are only an upper bound (statfs does not match the block devices)."""
    if fs_bytes is None:
        return f"the filesystem size could not be read (stat -f {fs.where})"
    if block_bytes > fs_bytes:
        return (
            f"the btrfs devices use {format_bytes_gib_mib(block_bytes - fs_bytes)} less than their block "
            "devices (shrunk with btrfs filesystem resize?)"
        )
    return "the filesystem size from statfs does not match its block devices"


def _unallocated_exact(host: DoctorHostPort, fs: _Fs, each: int, levels: _Levels) -> Iterator[Result]:
    """Each device holds one copy of every chunk: its unallocated space is its size less each."""
    fs_bytes = btrfs_device_bytes(host, fs.mnt, fs.profiles)
    block_bytes = sum(size for _, _, size in fs.devs)
    exact = fs_bytes is not None and abs(block_bytes - fs_bytes) <= RESIZE_SLACK
    free = [(label, size - each) for _, label, size in fs.devs]
    low = min(b for _, b in free)
    listed = ", ".join(f"{label} {format_bytes_gib_mib(b)}" for label, b in free)
    if not exact:
        listed = "at most " + listed
    meta = fs.fill["metadata"]
    meta_free = (meta[1] - meta[0]) if meta else None
    rsv = read_int(host, view_join(fs.alloc, "global_rsv_size")) or 0
    if low < levels.crit_gib * GIB:
        yield Result(
            ERROR,
            f"{fs.name}: CRITICAL: unallocated per device {listed} ({levels.limits}): "
            f"ENOSPC is close. Free space first, then a filtered data balance; never a "
            f"metadata or full balance. Steps: {ENOSPC_STEPS}",
        )
    elif low < levels.warn_gib * GIB:
        yield Result(
            WARN,
            f"{fs.name}: little unallocated space per device: {listed} ({levels.limits}; "
            f"metadata grows only from it). Free space; then, if it stays low, "
            f"see {ENOSPC_STEPS}",
        )
    elif exact:
        yield Result(
            OK,
            f"{fs.name}: unallocated per device {listed} ({levels.limits}; from sysfs, "
            "each btrfs device fills its block device)",
        )
    else:
        yield Result(INFO, f"{fs.name}: {fs.needs_root}; not estimated: {_size_unknown(fs, fs_bytes, block_bytes)}")
    if meta_free is not None and meta_free < 2 * rsv and low < 2 * GIB:
        yield Result(
            ERROR,
            f"{fs.name}: metadata almost full ({meta_free // 2**20} MiB free in "
            f"metadata chunks, global reserve {rsv // 2**20} MiB): writes may fail "
            f"with ENOSPC soon. Steps: {ENOSPC_STEPS}",
        )


def _unallocated_striped(host: DoctorHostPort, fs: _Fs, levels: _Levels) -> Result:
    """Mixed or striped layout: only the sum over all devices is exact."""
    disk_total = 0
    for kind in ("data", "metadata", "system"):
        disk_total += read_int(host, view_join(fs.alloc, kind, "disk_total")) or 0
    total_free = sum(size for _, _, size in fs.devs) - disk_total
    n = len(fs.devs)
    if disk_total and total_free < levels.warn_gib * GIB * n:
        return Result(
            WARN,
            f"{fs.name}: {format_bytes_gib_mib(total_free)} unallocated over {n} device(s), so at least "
            f"one device has less than {levels.warn_gib} GiB ({levels.limits}); {fs.needs_root}. "
            f"Steps: {ENOSPC_STEPS}",
        )
    if disk_total:
        return Result(
            INFO,
            f"{fs.name}: {format_bytes_gib_mib(total_free)} unallocated over {n} device(s) (layout {fs.layout}); "
            f"{fs.needs_root}",
        )
    return Result(INFO, f"{fs.name}: {fs.needs_root}")


def _btrfs_sysfs_uuids(host: DoctorHostPort, sysfs: ViewPath) -> list[str]:
    try:
        return sorted(
            n for n in host.listdir(sysfs) if FS_UUID.match(n) and host.isdir(view_join(sysfs, n, "allocation"))
        )
    except OSError:
        return []


@check(CHECKS, "btrfs")
def check_btrfs_allocation(host: DoctorHostPort, *, health_check_repo: str | None = None) -> Iterator[Result]:
    sysfs = host.path(BTRFS_SYSFS)
    uuids = _btrfs_sysfs_uuids(host, sysfs)
    if not uuids:
        yield Result(INFO, f"allocation: no mounted btrfs filesystem ({BTRFS_SYSFS} has none)")
        return
    levels = _Levels(*unalloc_thresholds(host, health_check_repo=health_check_repo))
    mounts = btrfs_mounts(host)
    conf = read_text(host, host.path(RECLAIM_CONF), 65536)
    for uuid in uuids:
        fs = _fs_facts(host, sysfs, uuid, mounts)
        yield _chunk_fill_result(fs)
        yield _reclaim_result(host, fs, conf)
        yield from _unallocated_results(host, fs, levels)


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


# What btrfs-snapshot does with a problem line of snapshots.conf (the warnings' note).
SNAPSHOT_NOTE: Final = " (btrfs-snapshot ignores it too, with a warning in its journal)"


def scrub_settings(host: DoctorHostPort) -> ShellAssignments:
    """The btrfs settings file as `cst btrfs install` reads SCRUB_LIMIT from it: the whole file with its
    line ends, as bash reads it ("\r" is part of a value there)."""
    return parse_shell_assignments(snapshots_conf_text(host) or "")


def scrub_lines(host: DoctorHostPort) -> ScrubLimitLines:
    """The SCRUB_LIMIT problem lines, as `cst btrfs install` reads them and as CST's Python programs do."""
    return scrub_limit_lines_both(scrub_settings(host), settings_of(host, SNAP_CONF))


@check(CHECKS, "btrfs")
def check_scrub_limit(host: DoctorHostPort) -> Iterator[Result]:
    """The weekly scrub's speed limit: SCRUB_LIMIT, the service's drop-in, sysfs now."""
    setting, problem = scrub_limit_setting(scrub_settings(host))
    lines = scrub_lines(host)
    refuses = "`cst btrfs install` refuses to run until it is fixed (btrfs/README.md, 4.3)"
    # Every SCRUB_LIMIT problem line, with what the settings row would have said of the lines it leaves
    # to this row (CST's Python programs find a problem there too); else the value's problem.
    for p in lines.bash:
        also = f". {ignored_line_text(SNAPSHOT_NOTE)}" if p.line in lines.python_too else ""
        yield Result(WARN, f"{SNAP_CONF} {p}: {refuses}{also}")
    if not lines.bash and problem:
        yield Result(WARN, f"{SNAP_CONF} {problem}: {refuses}")
    if not host.exists(host.path(SCRUB_UNIT)):
        return
    installed = scrub_dropin_limit(read_text(host, host.path(SCRUB_DROPIN), 4096))
    if not problem and installed != setting:
        yield Result(
            WARN,
            f"weekly scrub: speed limit {installed or 'none'} in the service but {setting or 'none'} in "
            f"SCRUB_LIMIT of {SNAP_CONF}: run `cst btrfs install`",
        )
    # NeedDaemonReload yes: a change stopped before its daemon-reload, the scrub runs the limit loaded before.
    unit = os.path.basename(SCRUB_UNIT)
    if (unit_props(host, ["systemctl"], unit, ["NeedDaemonReload"]) or {}).get("NeedDaemonReload") in YES_WORDS:
        yield Result(
            WARN,
            f"weekly scrub: systemd has not loaded {unit} as it is on disk (a change stopped before its "
            "daemon-reload), so the scrub runs with the speed limit systemd loaded before",
            "sudo systemctl daemon-reload",
        )
    limit = f"{installed} per device" if installed else "none (full speed; SCRUB_LIMIT, btrfs/README.md 4.3)"
    yield Result(INFO, f"weekly scrub: speed limit {limit}; now in sysfs: {live_scrub_limits(host)}")


@check(CHECKS, "btrfs")
def check_snapshot_settings(host: DoctorHostPort) -> Iterator[Result]:
    # btrfs-snapshot reads the file as data too (bash's rules, its own keys): it ignores the line. A
    # SCRUB_LIMIT problem line that check_scrub_limit reports too (the same place in the file) is warned
    # there, with these words.
    yield from settings_file_warnings(
        host, SNAP_CONF, bash_sourced=True, note=SNAPSHOT_NOTE, skip=scrub_lines(host).taken
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


def _findmnt_to_octal_field(field: str) -> str:
    """Convert `findmnt` hex escapes (`\\x20`) to `/proc/mounts` octal escapes (`\\040`)."""
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: f"\\{int(group(m, 1), 16):03o}", field)


def _findmnt_root_btrfs_info(host: DoctorHostPort) -> tuple[str | None, str]:
    """The live btrfs mounts as /proc/mounts lines, and the root filesystem's UUID: that of the mounts
    pick_root_mounts takes (the device of /sysroot, else /, else /var), never a separate btrfs /home's."""
    rc, out, _ = host.run(["findmnt", "-rn", "-t", "btrfs", "-o", "SOURCE,TARGET,OPTIONS,UUID"], timeout=15)
    if rc != 0 or not out.strip():
        return None, ""
    lines: list[str] = []
    uuids: list[tuple[str, str]] = []  # (target, UUID) of each line that has one
    for raw in out.splitlines():
        match raw.strip().split(None, 3):
            case [src, tgt, opts, *rest]:  # rest: the UUID, when the line has one
                esc_src, esc_tgt = _findmnt_to_octal_field(src), _findmnt_to_octal_field(tgt)
                lines.append(f"{esc_src} {esc_tgt} btrfs {opts} 0 0\n")
                if rest:
                    uuids.append((unescape_mount_field(esc_tgt), rest[0]))
    text = "".join(lines)
    root_targets = {m.target for m in pick_root_mounts(parse_proc_mounts(text))}
    root_uuid = next((uuid for tgt, uuid in uuids if tgt in root_targets), "")
    return (text or None), root_uuid


def _findmnt_all_mounts(host: DoctorHostPort) -> str | None:
    rc, out, _ = host.run(["findmnt", "-rn", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"], timeout=15)
    if rc != 0 or not out.strip():
        return None
    lines: list[str] = []
    for raw in out.splitlines():
        match raw.strip().split(None, 3):
            case [src, tgt, fstype, opts]:
                lines.append(f"{_findmnt_to_octal_field(src)} {_findmnt_to_octal_field(tgt)} {fstype} {opts} 0 0\n")
    return "".join(lines) or None


_COMPRESSION_LABEL: Final[str] = "root btrfs compression"
_COMMIT_INTERVAL_LABEL: Final[str] = "root btrfs commit interval"


def _read_ostree_kargs(host: DoctorHostPort) -> str | None:
    rc, kout, _ = host.run(["rpm-ostree", "kargs"])
    if rc == 0 and kout.strip():
        return kout
    return read_text(host, host.path(HostPath("/proc/cmdline")), 65536) or None


def _read_root_btrfs_snapshot(host: DoctorHostPort) -> tuple[CompressSnapshot, bool] | None:
    fstab_raw = read_text(host, host.path(FSTAB), 65536)
    proc_mounts_raw = read_text(host, host.path(HostPath("/proc/mounts")), 65536)
    top_text = read_text(host, host.path(HostPath(TOP_MOUNT_UNIT_PATH)), 65536)
    has_btrfs_fstab = bool(parse_proc_mounts(fstab_raw))
    has_btrfs_proc = bool(pick_root_mounts(parse_proc_mounts(proc_mounts_raw)))
    if not (has_btrfs_fstab or has_btrfs_proc or top_text or _btrfs_sysfs_uuids(host, host.path(BTRFS_SYSFS))):
        return None
    live_findmnt, root_uuid = _findmnt_root_btrfs_info(host)
    live_text = live_findmnt or proc_mounts_raw
    if not (
        parse_fstab_root_entries(fstab_raw, root_uuid) or pick_root_mounts(parse_proc_mounts(live_text)) or top_text
    ):
        return None
    is_ostree = host.exists(host.path(HostPath(OSTREE_BOOTED_PATH)))
    kargs_text = _read_ostree_kargs(host) if is_ostree else None
    top_mount = parse_top_mount_unit(top_text) if top_text else None
    snap = CompressSnapshot(
        is_ostree=is_ostree,
        root_uuid=root_uuid,
        fstab_text=fstab_raw or None,
        kargs_text=kargs_text,
        proc_mounts_text=live_text or None,
        top_mount=top_mount,
    )
    return snap, bool(fstab_raw or live_text or top_text)


def _compression_report_result(
    host: DoctorHostPort,
    report: CoherenceReport,
    *,
    has_sources: bool,
) -> Result | None:
    if not report.coherent:
        has_force = any(
            "=" in d
            and (sp := parse_compress_spec(d.split("=", 1)[1])) is not None
            and sp.mode == CompressMode.COMPRESS_FORCE
            for d in report.drift_details
        )
        fix_sub = "apply" if has_force else "revert"
        return Result(
            WARN,
            f"{_COMPRESSION_LABEL}: {report.summary}",
            f"{host.cst} btrfs compress-force {fix_sub}",
        )
    if report.has_compression:
        return Result(OK, f"{_COMPRESSION_LABEL}: {report.summary}")
    if has_sources:
        return Result(INFO, f"{_COMPRESSION_LABEL}: {report.summary}")
    return None


@check(CHECKS, "btrfs")
def check_btrfs_compression(host: DoctorHostPort) -> Iterator[Result]:
    """Coherence of root btrfs compression across /etc/fstab, rpm-ostree kargs, live mounts, and top-mount."""
    loaded = _read_root_btrfs_snapshot(host)
    if loaded is None:
        return
    snap, has_sources = loaded
    res = _compression_report_result(host, assess_coherence(snap), has_sources=has_sources)
    if res is not None:
        yield res


def _commit_fix_subcmd(snap: CompressSnapshot) -> str:
    custom_secs: list[int] = [
        effective_commit_seconds(m.options) for m in parse_fstab_root_entries(snap.fstab_text or "", snap.root_uuid)
    ]
    if snap.is_ostree and snap.kargs_text is not None:
        custom_secs.extend(
            effective_commit_seconds(t.removeprefix(COMMIT_ROOTFLAGS_PREFIX))
            for t in extract_rootflags_tokens(snap.kargs_text)
        )
    non_default = sorted({s for s in custom_secs if s not in {DEFAULT_TARGET_COMMIT_S, 30}})
    if len(non_default) == 1:
        return f"apply {non_default[0]}"
    return "apply"


@check(CHECKS, "btrfs")
def check_btrfs_commit_interval(host: DoctorHostPort) -> Iterator[Result]:
    """Coherence of root btrfs `commit=N` across /etc/fstab, rpm-ostree kargs, live mounts, and top-mount."""
    loaded = _read_root_btrfs_snapshot(host)
    if loaded is None:
        return
    snap, _ = loaded
    report = assess_commit_coherence(snap)
    if not report.coherent:
        yield Result(
            WARN,
            f"{_COMMIT_INTERVAL_LABEL}: {report.summary}",
            f"{host.cst} btrfs commit-interval {_commit_fix_subcmd(snap)}",
        )
    elif report.is_custom:
        yield Result(OK, f"{_COMMIT_INTERVAL_LABEL}: {report.summary}")


@check(CHECKS, "btrfs")
def check_btrfs_cache(host: DoctorHostPort) -> Iterator[Result]:
    """Check the dedicated NVMe cache drive's mounts, crypttab `nofail`, and user bind mounts."""
    fstab_raw = read_text(host, host.path(FSTAB), 65536)
    proc_raw = read_text(host, host.path(HostPath("/proc/mounts")), 65536)
    hint_text = f"{fstab_raw}\n{proc_raw}"
    has_hints = any(k in hint_text for k in ("/var/mnt/cache", "/var/mnt/scratchpad", "btrfs-cache"))
    if not (has_hints or _btrfs_sysfs_uuids(host, host.path(BTRFS_SYSFS))):
        return
    crypttab_path = host.path(HostPath("/etc/crypttab"))
    crypttab_raw, crypttab_unreadable = "", False
    try:
        with host.open_text(crypttab_path) as fh:
            crypttab_raw = fh.read(65536)
    except PermissionError:
        crypttab_unreadable = True
    except OSError:
        pass
    live_findmnt = _findmnt_all_mounts(host)
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
    if crypttab_unreadable and has_hints:
        yield Result(INFO, "btrfs cache: /etc/crypttab is not readable as this user (nofail not checked)")

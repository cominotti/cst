# SPDX-License-Identifier: MPL-2.0
"""Application probes, check registry, runner, and renderer for `cst doctor`."""

from collections.abc import Callable, Iterable, Iterator, Mapping
import json
import os
import re
import shlex
import time
import xml.etree.ElementTree as ET

from cst_core.domain.text import ShellAssignments, first_line, parse_shell_assignments, parse_snapshots_conf
from cpu.domain import CPU_DISABLE_DEFAULT
from cst_core.domain.values import GIB, format_bytes_gib_mib
from doctor.domain import (
    ALERT_KEEP_DAYS,
    ALERT_MAX_FILE,
    ALERT_NAME,
    BTRFS_SYSFS,
    CET_ISSUE,
    CPU_CONF,
    ERROR,
    FS_UUID,
    GAME_DROPIN,
    GAME_DROPIN_DIR,
    GAME_INTENDED,
    GAME_MANUAL,
    GAME_PROFILE_HOST,
    GAME_STATE,
    HEALTH_CHECK_HOST,
    INFO,
    MIRROR_COPIES,
    NOCOW_POSTPONED,
    NOCOW_WARN_BOOTS,
    OK,
    ONE_DEVICE_FACTOR,
    RECLAIM_CONF,
    RESIZE_SLACK,
    SNAP_CONF,
    SNAP_STATE,
    SNAP_UNFINISHED,
    SNAP_WRITABLE,
    STAGED_OLD_DAYS,
    TRANSIENT,
    UNALLOC_DEFAULTS,
    USER_KNOWN_SLICES,
    VIRSH_URI,
    VM_CONF,
    VM_INDEX,
    VM_LIVE_STATES,
    VM_OVERLAY_MARK,
    VM_RUN_DIR,
    VM_TIMER_WANTS,
    WARN,
    ZSWAP_CONF,
    ZSWAP_PARAMS,
    Deployment,
    HostPath,
    OstreeAnswer,
    Request,
    Result,
    classify_requests,
    cpu_time,
    describe,
    mtime_matches_ns,
    name_list,
    parse_alert_text,
    parse_cpu_stat_seconds,
    parse_offered_features_xml,
    parse_unalloc_thresholds_text,
    per_device_allocated,
    pick_mount,
    pkg_names,
    read_intended,
    unit_results,
    unpin_command,
    view_join,
    virt_changes,
    virt_versions,
    ViewPath,
    host_join,
)
from doctor.ports import CheckFn, DoctorHostPort, VmToolsPort
from swap.domain import parse_tmpfiles_params
from vm.domain import guest_kind, tune_domain

# The checkout's copies the doctor reads (host.repo_path gives where they are).
HEALTH_CHECK_SRC = "btrfs/btrfs-health-check"

type DomainsFn = Callable[[DoctorHostPort], tuple[list[str] | None, str]]

CHECKS: list[tuple[str, CheckFn]] = []


def check[F: CheckFn](area: str) -> Callable[[F], F]:
    """Register a check function under an output area."""

    def register(fn: F) -> F:
        CHECKS.append((area, fn))
        return fn

    return register


# ---- rpm-ostree ----------------------------------------------------------------------------------
def ostree_deployments(host: DoctorHostPort) -> OstreeAnswer:
    """(booted, pending, error). pending is the deployment of the next boot
    when it is not the booted one: rpm-ostree lists it first (for the booted
    system's osname), staged or not (rpm-ostree rollback leaves an unstaged
    one there). It is the one rpm-ostree install/uninstall edit."""
    if not host.exists(host.path(HostPath("/run/ostree-booted"))):
        return None, None, "not an rpm-ostree system"
    rc, out, err = host.run(["rpm-ostree", "status", "--json"])
    if rc != 0:
        return None, None, f"rpm-ostree status --json failed: {first_line(err) or f'exit {rc}'}"
    try:
        data = json.loads(out)
        deps = [d for d in data["deployments"] if isinstance(d, dict)]
    except (ValueError, KeyError, TypeError) as e:
        return None, None, f"rpm-ostree status --json: unreadable output ({e})"
    host._ostree_list = deps  # every deployment, in the order ostree admin status numbers them
    booted = next((d for d in deps if d.get("booted")), None)
    osname = booted.get("osname") if booted else None
    first = next((d for d in deps if osname is None or d.get("osname") == osname), None)
    pending = first if first is not None and first is not booted else None
    return booted, pending, ""


def installed_packages(host: DoctorHostPort, d: Deployment) -> list[str] | None:
    """The NEVRAs in a deployment (rpm-ostree db list CHECKSUM), or None."""
    csum = d.get("checksum")
    if not csum:
        return None
    rc, out, _ = host.run(["rpm-ostree", "db", "list", csum])
    if rc != 0:
        return None
    pkgs = [ln.strip() for ln in out.splitlines() if ln.startswith(" ") and ln.strip()]
    return pkgs or None


def deployments(host: DoctorHostPort) -> OstreeAnswer:
    """ostree_deployments, asked once per run (several checks use it)."""
    if not hasattr(host, "_ostree"):
        host._ostree = ostree_deployments(host)
    return host._ostree


@check("rpm-ostree")
def check_pinned_layering(host: DoctorHostPort) -> Iterator[Result]:
    booted, pending, err = deployments(host)
    if err:
        yield Result(INFO if err.startswith("not an") else WARN, f"layered packages: not checked ({err})")
        return
    found: dict[int, tuple[Deployment, list[str], list[Request], list[Request]]] = {}
    for d in (booted, pending):
        if d is None:
            continue
        reqs = d.get("requested-packages") or []
        cls = classify_requests(reqs, installed_packages(host, d), host.arch)
        found[id(d)] = (d, reqs, cls, [c for c in cls if c[2] != "name"])
    if not found:
        yield Result(WARN, "layered packages: no deployment in rpm-ostree status")
        return
    # rpm-ostree uninstall edits the next boot's deployment: the fix is built from it.
    target = pending if pending is not None else booted
    assert target is not None
    # found[id(d)] starts with d itself: this binds target to the same object (typed as found's).
    target, treqs, tcls, tpinned = found[id(target)]
    for d, reqs, cls, pinned in found.values():
        if not pinned:
            continue
        listed = ", ".join(r for r, _, how in pinned if how != "inactive")
        inactive = [r for r, _, how in pinned if how == "inactive"]
        msg = f"{describe(d)}: {len(pinned)} of {len(reqs)} layered packages are pinned to a version"
        if listed:
            msg += " (they never update, and an upgrade fails once that version leaves the repositories): " + listed
        if any(how == "pinned-form" for _, _, how in pinned):
            msg += " (recognised by their form only: the package list was not readable)"
        if inactive:
            msg += (
                "; pinned but matching no installed package (probably inactive requests: the base "
                "image may provide them now; the fix drops them without asking them back): " + ", ".join(inactive)
            )
        if d is target:
            yield Result(WARN, msg, unpin_command(cls))
        elif not tpinned:
            # Booted pinned, but the next boot's deployment (target, the pending one: with no
            # pending deployment the booted one is target, and found holds nothing else) is clean.
            yield Result(INFO, msg + f"; the {describe(target)} has no version-pinned requests: reboot to use it")
    if not tpinned:
        yield Result(OK, f"{describe(target)}: {len(treqs)} layered packages, all requested by name")


@check("rpm-ostree")
def check_local_packages(host: DoctorHostPort) -> Iterator[Result]:
    booted, pending, err = deployments(host)
    if err:
        return
    target = pending or booted
    if target is None:
        return
    local = list(target.get("requested-local-packages") or []) + list(
        target.get("requested-local-fileoverride-packages") or []
    )
    if not local:
        yield Result(OK, f"{describe(target)}: no local RPM files layered")
        return
    names = pkg_names(local)
    words = ["sudo", "rpm-ostree", "uninstall"] + [str(x) for x in local] + [f"--install={n}" for n in names]
    yield Result(
        WARN,
        f"{describe(target)}: {len(local)} local RPM file(s) layered: "
        f"{', '.join(map(str, local))}. rpm-ostree upgrade never updates them. "
        "Run the fix only if a repository has the package: it then comes from there",
        " ".join(shlex.quote(w) for w in words),
    )


@check("rpm-ostree")
def check_overrides(host: DoctorHostPort) -> Iterator[Result]:
    booted, pending, err = deployments(host)
    if err:
        return
    target = pending or booted
    if target is None:
        return
    replaced = pkg_names(
        [
            target.get("base-remote-replacements") or {},
            target.get("base-local-replacements") or [],
            target.get("requested-base-local-replacements") or [],
        ]
    )
    removed = pkg_names([target.get("base-removals") or [], target.get("requested-base-removals") or []])
    if replaced:
        yield Result(
            WARN,
            f"{describe(target)}: base packages replaced by another version: "
            f"{', '.join(replaced)}. They stay at that version through every upgrade",
            " ".join(shlex.quote(w) for w in ["sudo", "rpm-ostree", "override", "reset"] + replaced),
        )
    if removed:
        yield Result(
            INFO,
            f"{describe(target)}: base packages removed on purpose: {', '.join(removed)} "
            f"(to bring them back: sudo rpm-ostree override reset {' '.join(removed)})",
        )
    if not replaced and not removed:
        yield Result(OK, f"{describe(target)}: no base package replaced or removed")


@check("rpm-ostree")
def check_staged(host: DoctorHostPort) -> Iterator[Result]:
    booted, pending, err = deployments(host)
    if err:
        return
    if pending is None:
        yield Result(OK, "no pending deployment: the next boot is the booted one")
        return
    if not pending.get("staged"):
        yield Result(
            INFO,
            f"{describe(pending)} is not the booted one (for example after "
            "rpm-ostree rollback): it becomes the system at the next boot",
        )
        return
    ts = pending.get("timestamp")
    age = ""
    if isinstance(ts, (int, float)) and ts > 0:
        days = (host.now - ts) / 86400
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        age = f", built {when}"
        if days >= STAGED_OLD_DAYS:
            age += f", {int(days)} days ago"
    yield Result(INFO, f"{describe(pending)} waits for a reboot{age} (it becomes the system at the next boot)")


# ---- services ------------------------------------------------------------------------------------
def failed_units(host: DoctorHostPort, user: bool) -> tuple[list[str] | None, str]:
    cmd = (
        ["systemctl"]
        + (["--user"] if user else [])
        + ["list-units", "--state=failed", "--no-legend", "--plain", "--full", "--no-pager"]
    )
    rc, out, err = host.run(cmd, timeout=30)
    if rc != 0:
        return None, first_line(err) or f"exit {rc}"
    units: list[str] = []
    for line in out.splitlines():
        f = line.split()
        if f and f[0] not in units:
            units.append(f[0].lstrip("●*"))
    return [u for u in units if u], ""


@check("services")
def check_system_units(host: DoctorHostPort) -> Iterator[Result]:
    units, err = failed_units(host, user=False)
    if units is None:
        yield Result(WARN, f"system units: not checked (systemctl: {err})")
        return
    yield from unit_results("system", units)


@check("services")
def check_user_units(host: DoctorHostPort) -> Iterator[Result]:
    if host.uid == 0:
        yield Result(INFO, "user units: skipped (running as root; run it as your user)")
        return
    units, err = failed_units(host, user=True)
    if units is None:
        yield Result(WARN, f"user units: not checked (systemctl --user: {err})")
        return
    yield from unit_results("user", units)


# ---- alerts --------------------------------------------------------------------------------------
def read_alert(host: DoctorHostPort, path: ViewPath) -> tuple[str, str, int] | None:
    """(urgency, title, last) of a pending alert file, or None."""
    try:
        with host.open_bytes(path) as f:
            data = f.read(ALERT_MAX_FILE).decode("utf-8", "replace")
    except OSError:
        return None
    return parse_alert_text(data)


def seen_record(host: DoctorHostPort) -> dict[str, str]:
    """name -> recorded mtime string, from cst-pending-alerts' record of what
    this user saw (the f44-tools one while the new one does not exist)."""
    new = view_join(host.state_home, "cst-alerts")
    old = view_join(host.state_home, "f44-alerts")
    d = old if not host.exists(new) and host.isdir(old) and not host.islink(old) else new
    rec: dict[str, str] = {}
    try:
        with host.open_text(view_join(d, "delivered")) as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and ALERT_NAME.match(p[0]):
                    rec[p[0]] = p[1]
    except OSError:
        pass
    return rec


def mtime_matches(recorded: str, st: os.stat_result) -> bool:
    """As cst-pending-alerts: full precision when the record has it."""
    return mtime_matches_ns(recorded, st.st_mtime_ns)


@check("alerts")
def check_pending_alerts(host: DoctorHostPort) -> Iterator[Result]:
    pending = host.path(HostPath("/var/lib/cst-alerts/pending"))
    if not host.isdir(pending):
        yield Result(INFO, f"no alert folder /var/lib/cst-alerts/pending (installed by {host.cst} btrfs install)")
        return
    try:
        names = sorted(n for n in host.listdir(pending) if ALERT_NAME.match(n))
    except OSError as e:
        yield Result(WARN, f"pending alerts: not readable ({e.strerror})")
        return
    seen = seen_record(host)
    unseen: list[tuple[str, str, int]] = []
    seen_n = 0
    for n in names:
        p = view_join(pending, n)
        try:
            st = host.lstat(p)
        except OSError:
            continue
        if not host.isfile(p) or host.islink(p) or host.now - st.st_mtime > ALERT_KEEP_DAYS * 86400:
            continue
        a = read_alert(host, p)
        if a is None:
            continue
        if n in seen and mtime_matches(seen[n], st):
            seen_n += 1
        else:
            unseen.append(a)
    for urgency, title, last in sorted(unseen, key=lambda a: a[2]):
        when = time.strftime("%a %d %b %H:%M", time.localtime(last)) if last else "?"
        yield Result(
            ERROR if urgency == "critical" else WARN,
            f"alert you have not seen yet ({urgency}, {when}): {title} (shown at your next login; "
            f"the fix shows it now, as does {host.cst} btrfs status)",
            "cst-pending-alerts --list",
        )
    if seen_n:
        yield Result(
            INFO,
            f"{seen_n} alert(s) of the last {ALERT_KEEP_DAYS} days already shown to you (cst-pending-alerts --list)",
        )
    if not unseen and not seen_n:
        yield Result(OK, "no pending alerts")


@check("alerts")
def check_legacy_alerts(host: DoctorHostPort) -> Iterator[Result]:
    old = host.path(HostPath("/var/lib/f44-alerts"))
    if host.islink(old):
        yield Result(
            WARN,
            "/var/lib/f44-alerts: link left by the move from f44-tools to CST (the fix removes it)",
            f"{host.cst} btrfs install",
        )
    elif host.exists(old):
        yield Result(
            WARN,
            "/var/lib/f44-alerts: folder of the versions named f44-tools still there "
            "(the fix moves its alerts to /var/lib/cst-alerts and removes it)",
            f"{host.cst} btrfs install",
        )
    else:
        yield Result(OK, "no leftovers of f44-tools in /var/lib")


# ---- btrfs: chunk allocation ---------------------------------------------------------------------
def unalloc_thresholds(host: DoctorHostPort, *, health_check_repo: str | None = None) -> tuple[int, int]:
    """(warn_gib, crit_gib) from btrfs-health-check (the repository copy, else
    the installed one), so both always agree; the defaults if neither reads."""
    repo_path = host.repo_path(HEALTH_CHECK_SRC) if health_check_repo is None else ViewPath(health_check_repo)
    for path in (repo_path, host.path(HEALTH_CHECK_HOST)):
        try:
            with host.open_text(path) as f:
                text = f.read(65536)
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


def read_text(host: DoctorHostPort, path: ViewPath, limit: int = 256) -> str:
    try:
        with host.open_text(path) as f:
            return f.read(limit).strip()
    except OSError:
        return ""


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
    if not mnt or not factor:
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
        if not sectors:
            return [], f"no size for {n}"
        dm = read_text(host, view_join(base, "devices", n, "dm", "name"))
        devs.append((n, f"/dev/mapper/{dm}" if dm else f"/dev/{n}", sectors * 512))
    return devs, ""


@check("btrfs")
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
            fill[kind] = (used, total) if total and used is not None else None
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
            if errs:
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


@check("btrfs")
def check_snapshot_settings(host: DoctorHostPort) -> Iterator[Result]:
    yield from settings_file_warnings(host, SNAP_CONF, bash_sourced=True)


@check("btrfs")
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


@check("btrfs")
def check_docker_nocow_postponed(host: DoctorHostPort) -> Iterator[Result]:
    try:
        with host.open_text(host.path(NOCOW_POSTPONED)) as f:
            rec = json.loads(f.read(65536))
    except FileNotFoundError:
        return  # never postponed, or not set up
    except OSError, ValueError:
        rec = None
    if not isinstance(rec, dict):
        yield Result(INFO, f"Docker volumes NOCOW: cannot read {NOCOW_POSTPONED}")
        return
    boots, last = rec.get("boots"), rec.get("last")
    if type(boots) is not int or boots < 1:
        boots = 1
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if type(last) is int else "at an unknown time"
    reason = str(rec.get("reason") or "no reason recorded")
    msg = f"Docker volumes NOCOW: the boot-time migration was postponed at {boots} boot(s) in a row, last {when}: {reason}"
    if boots >= NOCOW_WARN_BOOTS:
        yield Result(
            WARN,
            f"{msg}; the volumes keep copy-on-write data (and new ones may too) until a boot where nothing uses them",
            "journalctl -b -u docker-volumes-nocow",
        )
    else:
        yield Result(INFO, f"{msg}; tried again at the next boot")


# ---- cpu: the game profile ----------------------------------------------------------------------
def game_dropins_left(host: DoctorHostPort) -> list[str]:
    """Host paths of the game profile's MemoryLow drop-ins that exist now."""
    try:
        names = sorted(host.listdir(host.path(GAME_DROPIN_DIR)))
    except OSError:
        return []
    return [
        f"{GAME_DROPIN_DIR}/{n}/{GAME_DROPIN}"
        for n in names
        if n.endswith(".d") and host.isfile(host.path(host_join(GAME_DROPIN_DIR, n, GAME_DROPIN)))
    ]


def game_again(host: DoctorHostPort) -> str:
    """The command that switches the profile off and on again in its mode:
    'off' drops the by-hand mark, so after a restart of the unit alone the
    watchdog would switch a by-hand profile off when no game runs."""
    if host.exists(host.path(GAME_MANUAL)):
        return "cst cpu game off && cst cpu game on"
    return "sudo systemctl restart cpu-game.service"


def settings_of(host: DoctorHostPort, path: HostPath, *, bash_sourced: bool = True) -> ShellAssignments | None:
    """A settings file read with cst_core's one reader (as bash reads it when
    bash_sourced), or None when it cannot be read. snapshots.conf also takes the
    lines an older CST wrote there with printf %q (parse_snapshots_conf)."""
    try:
        with host.open_text(host.path(path)) as f:
            text = f.read()
        if path == SNAP_CONF:
            return parse_snapshots_conf(text)
        return parse_shell_assignments(text, bash_sourced=bash_sourced)
    except OSError:
        return None


def conf_value(host: DoctorHostPort, path: HostPath, key: str) -> str | None:
    """The value of KEY in a settings file that bash also sources (the last
    assignment wins, as in bash and in vm-pin), or None. A problem line sets
    nothing; check_settings_files reports it."""
    parsed = settings_of(host, path)
    return None if parsed is None else parsed.values.get(key)


def settings_file_warnings(host: DoctorHostPort, path: HostPath, *, bash_sourced: bool) -> Iterator[Result]:
    """WARN for each line of a settings file that is not a plain assignment."""
    parsed = settings_of(host, path, bash_sourced=bash_sourced)
    bash = " (bash, which also sources this file, reads it its own way)" if bash_sourced else ""
    for p in parsed.problems if parsed else ():
        yield Result(
            WARN,
            f"{path} {p}. CST's Python programs take nothing from the line{bash}, and the cst commands "
            "that change the machine with these settings refuse to run until it is fixed",
        )


@check("cpu")
def check_cpu_settings(host: DoctorHostPort) -> Iterator[Result]:
    yield from settings_file_warnings(host, CPU_CONF, bash_sourced=True)


def user_manager(host: DoctorHostPort) -> tuple[list[str] | None, str]:
    """(systemctl args for the target user's manager, or None, why not)."""
    target = conf_value(host, CPU_CONF, "TARGET_USER") or ""
    if host.uid == 0:
        if not target:
            return None, f"no TARGET_USER in {CPU_CONF}"
        return ["systemctl", "--user", "-M", f"{target}@"], ""
    me = host.user_name(host.uid)
    if target and me and target != me:
        return None, f"the profile is for {target}, this is {me}"
    return ["systemctl", "--user"], ""


def unit_props(host: DoctorHostPort, base: list[str], unit: str, props: list[str]) -> dict[str, str] | None:
    """{prop: value} of a loaded unit ("[not set]" as ""), or None when the
    manager cannot be asked or the unit is not loaded."""
    cmd = base + ["show", "-p", "LoadState"] + [a for p in props for a in ("-p", p)] + [unit]
    rc, out, _ = host.run(cmd, timeout=20)
    if rc != 0:
        return None
    vals: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k] = "" if v == "[not set]" else v
    if vals.get("LoadState") != "loaded":
        return None
    return vals


@check("cpu")
def check_game_profile_drift(host: DoctorHostPort) -> Iterator[Result]:
    """WARN while the game profile is on and a setting it made no longer
    holds; ok when they all hold; info when it cannot tell."""
    if not host.exists(host.path(GAME_STATE)):
        left = game_dropins_left(host)
        if left:
            yield Result(
                WARN,
                f"game profile: off, but its MemoryLow drop-ins are still there: {', '.join(left)}. "
                "At every daemon-reload systemd applies them, so the game's memory protection comes "
                "back without a game (that memory is then harder to get for the VM and the builds) "
                "until the next reboot. The watchdog (cpu-game-watchdog.timer) removes them within "
                "30 s; if this stays, 'off' removes them and reloads systemd",
                f"sudo {GAME_PROFILE_HOST} off",
            )
        elif host.exists(host.path(CPU_CONF)):
            yield Result(OK, "game profile: off")
        return
    try:
        with host.open_text(host.path(GAME_INTENDED)) as f:
            records = read_intended(f.read(65536))
    except FileNotFoundError:
        yield Result(
            INFO,
            "game profile: ON, but switched on by an older cpu-game-profile that kept no "
            "record of the values it set, so drift is not checked. Switching it off and on again "
            f"records them: {game_again(host)}",
        )
        return
    except OSError as e:
        yield Result(INFO, f"game profile: ON; drift not checked ({GAME_INTENDED}: {e.strerror})")
        return
    managers: dict[str, tuple[list[str] | None, str]] = {"sys": (["systemctl"], "")}
    managers["usr"] = user_manager(host)
    wanted: dict[tuple[str, str], list[str]] = {}
    for r in records:
        if r[0] == "prop" and r[5]:
            wanted.setdefault((r[1], r[2]), []).append(r[3])
    current: dict[tuple[str, str, str], str] = {}
    unknown: list[str] = []
    for (mgr, unit), props in wanted.items():
        base, why = managers[mgr]
        vals = unit_props(host, base, unit, props) if base else None
        if vals is None:
            unknown.append(f"{unit} ({why or 'not loaded, or systemd did not answer'})")
            continue
        for p in props:
            current[(mgr, unit, p)] = vals.get(p, "")
    drifted: list[str] = []
    checked = 0
    for r in records:
        if r[0] == "prop":
            key = (r[1], r[2], r[3])
            if not r[5] or key not in current:
                continue
            checked += 1
            if current[key] != r[5]:
                drifted.append(f"{r[2]} {r[3]} is {current[key] or 'not set'} (the profile set {r[5]})")
        elif r[3]:
            try:
                with host.open_text(host.path(HostPath(r[1]))) as f:
                    now = f.read(256).strip()
            except OSError:
                continue  # the cgroup is gone: nothing to compare
            checked += 1
            if now != r[3]:
                drifted.append(f"{r[1]} is {now or 'empty'} (the profile wrote {r[3]})")
    if drifted:
        yield Result(
            WARN,
            f"game profile: ON, but {len(drifted)} of its settings no longer hold: "
            f"{'; '.join(drifted)}. A daemon-reload or uresourced undid them. The watchdog "
            "(cpu-game-watchdog.timer) sets them again within 30 s; if this stays, switch the "
            "profile off and on again",
            game_again(host),
        )
    elif checked:
        yield Result(OK, f"game profile: ON, the {checked} settings it made still hold")
    if unknown:
        yield Result(INFO, f"game profile: drift not checked for {', '.join(unknown)}")
    if not drifted and not checked and not unknown:
        yield Result(INFO, "game profile: ON, no recorded setting to compare")


def user_manager_cgroup(host: DoctorHostPort) -> HostPath | None:
    """The cgroup folder of the user manager to look at (the doctor's own
    user, or TARGET_USER when run as root), or None."""
    uid = host.uid
    if uid == 0:
        target = conf_value(host, CPU_CONF, "TARGET_USER")
        if not target:
            return None
        found = host.user_id(target)
        if found is None:
            return None
        uid = found
    return HostPath(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service")


def cpu_seconds(host: DoctorHostPort, path: ViewPath) -> float | None:
    """CPU time a cgroup used since it was created (cpu.stat usage_usec),
    in seconds, or None."""
    return parse_cpu_stat_seconds(read_text(host, view_join(path, "cpu.stat"), 4096))


def cgroup_units(host: DoctorHostPort, path: ViewPath) -> list[tuple[str, ViewPath]]:
    """[(unit, cgroup folder)] of the child cgroups of PATH; systemd puts a
    '_' before a name that could clash with a controller file (cpu.slice ->
    _cpu.slice)."""
    try:
        names = sorted(host.listdir(path))
    except OSError:
        return []
    return [(n[1:] if n.startswith("_") else n, view_join(path, n)) for n in names if host.isdir(view_join(path, n))]


@check("cpu")
def check_user_manager_boxes(host: DoctorHostPort) -> Iterator[Result]:
    """WARN for each top-level slice of the user manager that CST does not
    know (it competes with the game's app.slice at the default weight, on
    every CPU); info for systemd-run jobs straight in app.slice. Reads
    /sys/fs/cgroup only; nothing when there is no user manager to look at."""
    rel = user_manager_cgroup(host)
    if not rel or not host.isdir(host.path(rel)):
        return
    base = host.path(rel)
    loose = [(u, d) for u, d in cgroup_units(host, base) if u.endswith(".slice") and u not in USER_KNOWN_SLICES]
    for unit, d in loose:
        name = unit[: -len(".slice")]
        yield Result(
            WARN,
            f"user manager: {unit} is a slice of its own next to app.slice, where the game runs "
            f"({cpu_time(cpu_seconds(host, d))} since it was created). Outside games it runs at the default "
            "weight on every CPU, without the toolboxes' limits; the game profile only makes it idle "
            f"while a game runs. Name it user-{name}.slice so it nests under the toolboxes' user.slice, or "
            f"start it with --slice=user-{name}.slice",
        )
    jobs = [
        (u, cpu_seconds(host, d)) for u, d in cgroup_units(host, view_join(base, "app.slice")) if TRANSIENT.match(u)
    ]
    if jobs:
        jobs.sort(key=lambda j: -(j[1] or 0))
        yield Result(
            INFO,
            f"user manager: {len(jobs)} systemd-run job(s) run straight in app.slice, next to the "
            f"game, at the default weight on every CPU: "
            f"{name_list([f'{u} ({cpu_time(s)})' for u, s in jobs])}. The game profile makes them idle "
            "while a game runs; for builds, start them with --slice=user-<name>.slice to get the "
            "toolboxes' limits always",
        )
    if not loose and not jobs:
        yield Result(OK, "user manager: no slices or systemd-run jobs outside the boxes CST manages")


# ---- swap: zswap --------------------------------------------------------------------------------
@check("swap")
def check_zswap_params(host: DoctorHostPort) -> Iterator[Result]:
    """WARN for each zswap parameter that swap's zswap.conf sets and this
    kernel does not have: systemd-tmpfiles skips it at boot without a word
    (a new kernel can rename or drop one). Nothing when that file is not
    installed."""
    try:
        with host.open_text(host.path(ZSWAP_CONF)) as f:
            wanted = [name for name, _ in parse_tmpfiles_params(f.read(65536))]
    except FileNotFoundError:
        return
    except OSError:
        yield Result(INFO, f"zswap: cannot read {ZSWAP_CONF}")
        return
    if not wanted:
        return
    try:
        have = set(host.listdir(host.path(ZSWAP_PARAMS)))
    except OSError:
        yield Result(WARN, f"zswap: not in this kernel (no {ZSWAP_PARAMS}): {ZSWAP_CONF} sets nothing")
        return
    missing = [name for name in wanted if name not in have]
    for name in missing:
        yield Result(
            WARN,
            f"zswap {name}: this kernel has no such parameter; {ZSWAP_CONF} sets it, and systemd-tmpfiles "
            "skips it at boot without a word (cst swap status shows the zswap settings)",
        )
    if not missing:
        yield Result(OK, f"zswap: every parameter {ZSWAP_CONF} sets is in this kernel")


# ---- vm: libvirt guests --------------------------------------------------------------------------
def virsh(host: DoctorHostPort, *args: str, ro: bool = True) -> tuple[int, str, str]:
    """virsh on the system connection; -r (read-only) where libvirt allows it.
    In the C locale: the checks compare its answers ("running", "shut off"),
    which virsh translates."""
    r = host.virsh(read_only=ro).call(*args, timeout=20)
    return r.returncode, r.stdout, r.stderr


def virsh_problem(rc: int, err: str) -> str:
    if rc == 127:
        return "no virsh: libvirt is not installed"
    return first_line(err) or f"virsh exit {rc}"


def libvirt_domains(host: DoctorHostPort) -> tuple[list[str] | None, str]:
    """(names, problem), asked once per run."""
    if not hasattr(host, "_domains"):
        rc, out, err = virsh(host, "list", "--all", "--name")
        if rc != 0:
            host._domains = (None, virsh_problem(rc, err))
        else:
            host._domains = ([n.strip() for n in out.splitlines() if n.strip()], "")
    return host._domains


def domain_xml(host: DoctorHostPort, vm: str) -> tuple[int, str, str]:
    """(rc, xml, err) of `dumpxml --inactive` (no secrets), asked once per VM and run."""
    if not hasattr(host, "_dumpxml"):
        host._dumpxml = {}
    cache = host._dumpxml
    if vm not in cache:
        cache[vm] = virsh(host, "dumpxml", "--inactive", vm)
    return cache[vm]


def cpu_disable_names(host: DoctorHostPort) -> list[str]:
    """VM_CPU_DISABLE of cpu-setup.conf when set there (also empty), else
    vm-pin's default (cpu.domain.CPU_DISABLE_DEFAULT)."""
    value = conf_value(host, CPU_CONF, "VM_CPU_DISABLE")
    if value is not None:
        return value.split()
    return CPU_DISABLE_DEFAULT.split()


def offered_features(host: DoctorHostPort) -> tuple[set[str | None] | None, str]:
    """(names, problem): the CPU features QEMU and KVM can give a guest on
    this host."""
    rc, out, err = virsh(host, "domcapabilities", ro=False)
    if rc != 0:
        return None, virsh_problem(rc, err)
    return parse_offered_features_xml(out)


@check("vm")
def check_vm_cpu_features(host: DoctorHostPort, *, domains_fn: DomainsFn | None = None) -> Iterator[Result]:
    dom_fn = libvirt_domains if domains_fn is None else domains_fn
    names, problem = dom_fn(host)
    if names is None:
        yield Result(INFO, f"libvirt VMs: not checked ({problem})")
        return
    if not names:
        yield Result(OK, "libvirt: no VM defined")
        return
    wanted = cpu_disable_names(host)
    if not wanted:
        yield Result(
            INFO, f"VM CPU features: not checked (VM_CPU_DISABLE is empty in {CPU_CONF}: switched off on purpose)"
        )
        return
    offered: set[str | None] | None = None
    oproblem: str | None = None
    for vm in names:
        rc, out, err = domain_xml(host, vm)
        try:
            cpu = ET.fromstring(out).find("cpu") if rc == 0 else None
        except ET.ParseError:
            rc, err = 1, "definition unreadable"
            cpu = None
        if rc != 0:
            yield Result(INFO, f"VM {vm}: definition not readable ({virsh_problem(rc, err)})")
            continue
        mode = cpu.get("mode", "custom") if cpu is not None else "not set"
        if cpu is None or mode != "host-passthrough":
            yield Result(OK, f"VM {vm}: cpu mode {mode} (the CET/MBEC bootloop concerns host-passthrough only)")
            continue
        off = {f.get("name") for f in cpu.findall("feature") if f.get("policy") == "disable"}
        missing = [n for n in wanted if n not in off]
        if missing and oproblem is None:
            offered, oproblem = offered_features(host)
        if not missing:
            yield Result(OK, f"VM {vm}: host-passthrough with {', '.join(wanted)} disabled")
        elif offered is None:
            yield Result(
                INFO,
                f"VM {vm}: host-passthrough without disabling {', '.join(missing)}; whether "
                f"this QEMU passes them was not checked ({oproblem}). If Windows loops in "
                f"Automatic Repair: {host.cst} cpu vm-pin plan ({CET_ISSUE})",
            )
        elif not [n for n in missing if n in offered]:
            yield Result(
                OK, f"VM {vm}: host-passthrough; this QEMU does not offer {', '.join(missing)} (nothing to disable yet)"
            )
        else:
            bad = [n for n in missing if n in offered]
            yield Result(
                WARN,
                f"VM {vm}: host-passthrough passes {', '.join(bad)} to the guest (QEMU 11 and "
                "later offer them); Windows with Hyper-V/VBS/WSL2 then loops in Automatic Repair. "
                f"The fix shows the change to the definition; then {host.cst} cpu vm-pin apply. "
                f"Upstream: {CET_ISSUE}",
                f"{host.cst} cpu vm-pin plan",
            )


def newest_vm_backup(host: DoctorHostPort) -> float:
    """Epoch of the newest backup in cst vm's index, 0 when none or unreadable."""
    try:
        with host.open_text(host.path(VM_INDEX)) as f:
            data = json.load(f)
    except OSError, ValueError:
        return 0
    entries = data.get("backups") if isinstance(data, dict) else None
    return max(
        (e["epoch"] for e in entries or [] if isinstance(e, dict) and isinstance(e.get("epoch"), (int, float))),
        default=0,
    )


def nas_configured(host: DoctorHostPort) -> bool:
    """The /home NAS backup is set up (snapshots.conf is world-readable; read as btrfs-snapshot reads it)."""
    parsed = settings_of(host, SNAP_CONF)
    values = parsed.values if parsed else {}
    return values.get("NAS_ENABLED") == "1" and bool(values.get("NAS_PATH"))


@check("vm")
def check_vm_settings(host: DoctorHostPort) -> Iterator[Result]:
    yield from settings_file_warnings(host, VM_CONF, bash_sourced=False)


@check("vm")
def check_virt_stack_change(host: DoctorHostPort, *, domains_fn: DomainsFn | None = None) -> Iterator[Result]:
    booted, _, err = deployments(host)
    if err or booted is None:
        yield Result(INFO, f"virtualization packages: not compared ({err or 'no booted deployment'})")
        return
    deps = getattr(host, "_ostree_list", [])
    i = next(n for n, d in enumerate(deps) if d is booted)
    rb = next(((n, d) for n, d in enumerate(deps) if n > i and d.get("osname") == booted.get("osname")), None)
    if rb is None:
        yield Result(OK, "virtualization packages: no rollback deployment to compare with")
        return
    index, rollback = rb
    old, new = installed_packages(host, rollback), installed_packages(host, booted)
    if old is None or new is None:
        yield Result(INFO, "virtualization packages: not compared (rpm-ostree db list did not answer)")
        return
    change = virt_changes(virt_versions(old), virt_versions(new))
    if not change:
        yield Result(OK, "virtualization packages: the same in the booted and the rollback deployment")
        return
    dom_fn = libvirt_domains if domains_fn is None else domains_fn
    names, problem = dom_fn(host)
    rb_ver = rollback.get("version") or (rollback.get("checksum") or "?")[:10]
    what = f"virtualization packages changed from the rollback deployment {rb_ver} to the booted one: {change}"
    if not names:
        yield Result(INFO, f"{what}; no libvirt VM {'defined' if names is not None else f'seen ({problem})'}")
        return
    pinned = bool(rollback.get("pinned"))
    ts = booted.get("timestamp")
    built = ts if isinstance(ts, (int, float)) else 0
    backup = newest_vm_backup(host)
    fresh = backup > built > 0
    if pinned and fresh:
        yield Result(INFO, f"{what}; rollback pinned and VM backup present")
        return
    if not pinned:
        yield Result(
            WARN,
            f"{what}. The rollback deployment (index {index}) is not pinned: the next upgrade "
            "can remove it, and with it the way back if a VM stops booting. Keep it pinned until "
            f"the VMs ({', '.join(names)}) work on the new one",
            f"sudo ostree admin pin {index}",
        )
    if not fresh:
        when = time.strftime("%Y-%m-%d", time.localtime(built)) if built else "?"
        last = time.strftime("%Y-%m-%d", time.localtime(backup)) if backup else "never"
        if nas_configured(host):
            yield Result(
                WARN,
                f"{what}. No VM backup since the booted deployment was built ({when}; last "
                f"backup: {last}). The NAS backup works with the VM running",
                f"{host.cst} vm backup --nas",
            )
        else:
            yield Result(
                WARN,
                f"{what}. No VM backup since the booted deployment was built ({when}; last "
                f"backup: {last}). Shut the VM down first",
                f"{host.cst} vm backup",
            )


def nas_backup_running(host: DoctorHostPort, vm: str) -> int:
    """The pid of a `cst vm backup --nas` of vm in progress, or 0. The pid
    must still be cst-vm: a lock file a killed run left keeps its pid, and
    the kernel reuses pids."""
    try:
        with host.open_text(host.path(host_join(VM_RUN_DIR, f"nas-{vm}.lock"))) as f:
            pid = f.read().strip()
    except OSError:
        return 0
    if not pid.isdigit():
        return 0
    try:
        with host.open_bytes(host.path(HostPath(f"/proc/{pid}/cmdline"))) as f:
            cmdline = f.read()
    except OSError:
        return 0
    return int(pid) if b"cst-vm" in cmdline else 0


def file_newer(host: DoctorHostPort, a: HostPath, b: HostPath) -> bool | None:
    """True when file a was written after file b, None when unknown."""
    try:
        return host.stat(host.path(a)).st_mtime_ns > host.stat(host.path(b)).st_mtime_ns
    except OSError:
        return None


@check("vm")
def check_vm_overlays(host: DoctorHostPort, *, domains_fn: DomainsFn | None = None) -> Iterator[Result]:
    """WARN for a disk whose definition names an overlay of a NAS backup."""
    dom_fn = libvirt_domains if domains_fn is None else domains_fn
    names, _ = dom_fn(host)
    for vm in names or []:
        live: dict[str, str] = {}
        defined: dict[str, str] = {}
        sources: dict[str, str] | None = None
        rc, out, _ = virsh(host, "domblklist", vm, "--details")
        if rc == 0:
            sources = {}
            for ln in out.splitlines():
                parts = ln.split(None, 3)
                if len(parts) == 4 and parts[0] != "Type" and not parts[0].startswith("-"):
                    sources[parts[2]] = parts[3].strip()
                    if VM_OVERLAY_MARK in parts[3]:
                        live.setdefault(parts[2], parts[3].strip())
        rc, out, _ = domain_xml(host, vm)
        try:
            disks = ET.fromstring(out).findall("devices/disk") if rc == 0 else []
        except ET.ParseError:
            disks = []
        for disk in disks:
            src, tgt = disk.find("source"), disk.find("target")
            path = src.get("file") if src is not None else None
            if path and VM_OVERLAY_MARK in path:
                target = (tgt.get("dev") if tgt is not None else None) or "?"
                defined.setdefault(target, path)
        if not live and not defined:
            continue
        rc, out, _ = virsh(host, "domstate", vm)
        state = first_line(out) if rc == 0 else None
        pid = nas_backup_running(host, vm)
        q, v = shlex.quote, f"sudo virsh -c {VIRSH_URI}"
        left = f"an overlay left by `{host.cst} vm backup --nas` (it could not merge it back)"
        guide = '(vm/README.md, "If a backup left an overlay")'
        for dev in sorted(set(live) | set(defined)):
            path = live.get(dev) or defined[dev]
            base = path.split(VM_OVERLAY_MARK, 1)[0]
            if pid:
                yield Result(
                    INFO,
                    f"VM {vm}: disk {dev} on {path}: a NAS backup is in progress (pid {pid}); "
                    "it merges the overlay back when restic is done",
                )
                continue
            if state in VM_LIVE_STATES:
                kind = (
                    "live"
                    if dev in live
                    else "definition"
                    if sources is not None and sources.get(dev) == base
                    else "unknown"
                )
            elif state in ("shut off", "crashed"):
                kind = "offline" if file_newer(host, HostPath(path), HostPath(base)) else "unknown"
            else:
                kind = "unknown"
            if kind == "live":
                yield Result(
                    WARN,
                    f"VM {vm}: disk {dev} is on {path}, {left}. The VM writes into it, and it "
                    f"grows. If `virsh blockjob {vm} {dev} --info` shows a commit ready (100 %), finish "
                    f"it with `virsh blockjob {vm} {dev} --pivot`; with no block job, merge it with the "
                    f"command below. Then check that `virsh domblklist {vm} --details` (also with "
                    f"--inactive) shows {base}, and remove the overlay file {guide}",
                    f"{v} blockcommit {q(vm)} {dev} --active --pivot --wait --verbose",
                )
            elif kind == "definition":
                yield Result(
                    WARN,
                    f"VM {vm}: disk {dev}: the definition names {path}, {left}, but the VM runs "
                    f"on {base} again: the overlay is stale. Do NOT qemu-img commit it (that would overwrite newer data in "
                    f"the image). Point the disk back with the command below (<source file='{base}'/>, "
                    f"and remove its <backingStore>), check `virsh domblklist {vm} --details --inactive`, "
                    f"then remove the overlay file {guide}",
                    f"{v} edit {q(vm)}",
                )
            elif kind == "offline":
                yield Result(
                    WARN,
                    f"VM {vm}: disk {dev} is on {path}, {left}. The VM is {state} on it, and it is "
                    f"newer than {base}: it holds the guest's last writes. Merge it offline with the "
                    f"command below, then point the disk back with `virsh edit {vm}` (<source "
                    f"file='{base}'/>, no <backingStore>), check `virsh domblklist {vm} --details "
                    f"--inactive`, and remove the overlay file {guide}",
                    f"sudo qemu-img commit -p {q(path)}",
                )
            elif state in ("shut off", "crashed"):
                yield Result(
                    WARN,
                    f"VM {vm}: disk {dev} is on {path}, {left}. The VM is {state}, and the doctor "
                    f"cannot tell whether that overlay still holds the guest's last writes: the image is "
                    f"newer, or the times are unreadable. The image is newer both after a merge cut short "
                    f"(the overlay is still needed) and after a merge that left the definition behind "
                    f"(stale). Do not remove or commit anything yet: look at the times with the command "
                    f"below and at the backup's own message or alert {guide}",
                    f"sudo ls -l --time-style=full-iso {q(base)} {q(path)}",
                )
            else:
                yield Result(
                    WARN,
                    f"VM {vm}: disk {dev} is on {path}, {left}. The VM's state "
                    f"({state or 'unreadable'}) or its disk listing does not tell how to put it back. "
                    f"Do not remove or commit anything yet: look with `virsh domstate {vm}` and "
                    f"`virsh domblklist {vm} --details` (and --inactive) {guide}",
                    f"sudo virsh -c {VIRSH_URI} domblklist {q(vm)} --details",
                )


@check("vm")
def check_vm_backup_timer(host: DoctorHostPort, *, domains_fn: DomainsFn | None = None) -> Iterator[Result]:
    """INFO: whether the weekly NAS backup timer of each VM is on."""
    dom_fn = libvirt_domains if domains_fn is None else domains_fn
    names, _ = dom_fn(host)
    if not names:
        return
    try:
        links = set(host.listdir(host.path(VM_TIMER_WANTS)))
    except OSError:
        links = set()
    for vm in names:
        on = f"cst-vm-backup@{vm}.timer" in links
        yield Result(
            INFO,
            f"VM {vm}: weekly NAS backup timer: {'on' if on else 'off'}"
            + ("" if on else f" ({host.cst} vm backup-timer on {vm} turns it on)"),
        )


@check("vm")
def check_vm_hardware(
    host: DoctorHostPort,
    *,
    vm_tools: VmToolsPort | None = None,
    domains_fn: DomainsFn | None = None,
) -> Iterator[Result]:
    """INFO for VMs whose disks or network card differ from `cst vm tune`."""
    dom_fn = libvirt_domains if domains_fn is None else domains_fn
    names, _ = dom_fn(host)
    if not names:
        return
    if vm_tools is None:
        yield Result(INFO, "VM hardware: not checked (the vm package is not wired in)")
        return
    kn = vm_tools.knobs(host.path(VM_CONF))
    for vm in names:
        rc, out, _ = domain_xml(host, vm)
        if rc != 0:
            continue
        try:
            _, changes, _, _ = tune_domain(out, kn, True)
            kind, _ = guest_kind(ET.fromstring(out))
        except Exception as e:  # noqa: BLE001 - an odd definition is an info line
            yield Result(INFO, f"VM {vm}: hardware not checked ({type(e).__name__}: {e})")
            continue
        if not changes:
            yield Result(OK, f"VM {vm}: disks and network card as {host.cst} vm tune recommends")
            continue
        first = "; ".join(changes[:3]) + (f"; and {len(changes) - 3} more" if len(changes) > 3 else "")
        extra = " Windows: the virtio drivers go in first (the plan shows how)." if kind != "linux" else ""
        yield Result(
            INFO,
            f"VM {vm}: hardware differs from what {host.cst} vm tune recommends: {first}.{extra} "
            f"See: {host.cst} vm tune plan {vm}",
        )


@check("vm")
def check_vm_image_dirs(host: DoctorHostPort, *, vm_tools: VmToolsPort | None = None) -> Iterator[Result]:
    """INFO for a VM image folder on btrfs without +C, or with images that
    are still copy-on-write."""
    if vm_tools is None:
        return
    if not vm_tools.knobs(host.path(VM_CONF))["VM_NOCOW"]:
        yield Result(INFO, f"VM image folders: NOCOW not managed (VM_NOCOW=no in {VM_CONF})")
        return
    system = vm_tools.system_images
    for d in vm_tools.image_dirs():
        p = vm_tools.probe_dir(d, (lambda x: host.path(HostPath(x))) if d["path"] == system else (lambda x: x))
        if not p["present"] or p["btrfs"] is False:
            continue
        path = p["path"]
        if p["nocow"] is None and not p["listed"]:
            yield Result(
                INFO,
                f"VM image folder {path}: NOCOW mark not readable as your user (root's "
                f"folder); {host.cst} vm nocow plan --root shows it",
            )
            continue
        cow = [i for i in p["images"] if i["nocow"] is False]
        if p["nocow"] is False:
            yield Result(
                INFO,
                f"VM image folder {path}: no NOCOW mark (+C), so new images there are "
                f"copy-on-write; {host.cst} vm nocow apply sets it",
            )
        if cow:
            listed = ", ".join(f"{i['name']} ({format_bytes_gib_mib(i['alloc'], mib_decimals=1)})" for i in cow)
            yield Result(
                INFO,
                f"VM image folder {path}: still copy-on-write: {listed}; "
                f"{host.cst} vm nocow plan prints how to convert (never done automatically)",
            )
        if p["nocow"] and not cow:
            yield Result(OK, f"VM image folder {path}: NOCOW, no copy-on-write image")


def run_checks(host: DoctorHostPort, checks: Iterable[tuple[str, CheckFn]] | None = None) -> list[tuple[str, Result]]:
    """[(area, Result)] in registry order. A check that crashes is an ERROR
    line, not a crash of the whole check-up."""
    out: list[tuple[str, Result]] = []
    for area, fn in CHECKS if checks is None else checks:
        try:
            for r in fn(host):
                out.append((area, r))
        except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
            out.append((area, Result(ERROR, f"{fn.__name__}: the check itself failed ({type(e).__name__}: {e})")))
    return out

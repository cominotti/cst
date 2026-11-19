# SPDX-License-Identifier: MPL-2.0
"""The vm area of `cst doctor`: the libvirt guests (CPU features, the backup index, the virt stack, overlays,
the backup timer, the virtual hardware, the image folders, the backup alerts)."""

from collections.abc import Callable, Iterator
import json
import shlex
import time
import xml.etree.ElementTree as ET

from cpu.domain import CPU_DISABLE_DEFAULT
from cst_core.domain.errors import ExitStatus
from cst_core.domain.text import first_line
from cst_core.domain.values import format_bytes_gib_mib
from doctor.application.shared import (
    check,
    conf_value,
    facts,
    installed_packages,
    ostree_facts,
    refresh,
    runs,
    settings_file_warnings,
    settings_of,
)
from doctor.domain import (
    CET_ISSUE,
    CPU_CONF,
    INFO,
    NOTIFY_HOST,
    OK,
    SNAP_CONF,
    VIRSH_URI,
    VM_CONF,
    VM_INDEX,
    VM_INDEX_FORMAT,
    VM_LIVE_STATES,
    VM_OVERLAY_MARK,
    VM_RUN_DIR,
    VM_TIMER_WANTS,
    WARN,
    HostPath,
    Result,
    parse_offered_features_xml,
    virt_changes,
    virt_versions,
    host_join,
)
from doctor.ports import CheckFn, DoctorHostPort, VmToolsPort
from vm.domain import VmOverlayKind
from vm.domain.tune import GuestKind, guest_kind, tune_domain

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []


type DomainsFn = Callable[[DoctorHostPort], tuple[list[str] | None, str]]


def virsh(host: DoctorHostPort, *args: str, ro: bool = True) -> tuple[int, str, str]:
    """virsh on the system connection; -r (read-only) where libvirt allows it.
    In the C locale: the checks compare its answers ("running", "shut off"),
    which virsh translates."""
    r = host.virsh(read_only=ro).call(*args, timeout=20)
    return r.returncode, r.stdout, r.stderr


def virsh_problem(rc: int, err: str) -> str:
    if rc == ExitStatus.NOT_FOUND:
        return "no virsh: libvirt is not installed"
    return first_line(err) or f"virsh exit {rc}"


def libvirt_domains(host: DoctorHostPort) -> tuple[list[str] | None, str]:
    """(names, problem), asked once per run."""
    known = facts(host)
    if known.domains is None:
        rc, out, err = virsh(host, "list", "--all", "--name")
        if rc != 0:
            known.domains = (None, virsh_problem(rc, err))
        else:
            known.domains = ([n.strip() for n in out.splitlines() if n.strip()], "")
    return known.domains


def domain_xml(host: DoctorHostPort, vm: str) -> tuple[int, str, str]:
    """(rc, xml, err) of `dumpxml --inactive` (no secrets), asked once per VM and run."""
    cache = facts(host).dumpxml
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


@check(CHECKS, "vm")
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


def vm_index_data(host: DoctorHostPort) -> object:
    """cst vm's backup index as read, None when it is missing or unreadable."""
    try:
        with host.open_text(host.path(VM_INDEX)) as f:
            return json.load(f)
    except OSError, ValueError:
        return None


def newest_vm_backup(host: DoctorHostPort) -> float | None:
    """Epoch of the newest backup in cst vm's index, 0 when none or unreadable, None when the index is of
    a newer cst vm (not read: check_vm_index says so)."""
    data = vm_index_data(host)
    if VM_INDEX_FORMAT.newer(data, "version") is not None:
        return None
    entries = data.get("backups") if isinstance(data, dict) else None
    return max(
        (e["epoch"] for e in entries or [] if isinstance(e, dict) and isinstance(e.get("epoch"), (int, float))),
        default=0,
    )


def nas_configured(host: DoctorHostPort) -> bool:
    """The /home NAS backup is set up (snapshots.conf is world-readable; read as btrfs-snapshot reads it)."""
    parsed = settings_of(host, SNAP_CONF)
    if not parsed:
        return False
    values = parsed.values
    return values.get("NAS_ENABLED") == "1" and bool(values.get("NAS_PATH"))


@check(CHECKS, "vm")
def check_vm_index(host: DoctorHostPort) -> Iterator[Result]:
    """WARN when cst vm's backup index is of a newer format: this checkout's cst vm refuses to back up
    or list with it (after going back to an older CST)."""
    found = VM_INDEX_FORMAT.newer(vm_index_data(host), "version")
    if found is not None:
        yield Result(
            WARN,
            f"{VM_INDEX_FORMAT.newer_text(VM_INDEX, found)}: {host.cst} vm refuses to back up or list "
            "with it; use the cst vm of that CST (its checkout: cst vm backup-timer on)",
        )


@check(CHECKS, "vm")
def check_vm_settings(host: DoctorHostPort) -> Iterator[Result]:
    yield from settings_file_warnings(host, VM_CONF, bash_sourced=False)


@check(CHECKS, "vm")
def check_virt_stack_change(host: DoctorHostPort, *, domains_fn: DomainsFn | None = None) -> Iterator[Result]:
    (booted, _, err), deps = ostree_facts(host)
    if err or booted is None:
        yield Result(INFO, f"virtualization packages: not compared ({err or 'no booted deployment'})")
        return
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
    fresh = backup is not None and backup > built > 0
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
        last = (
            "unknown (see the index line)"
            if backup is None
            else time.strftime("%Y-%m-%d", time.localtime(backup))
            if backup
            else "never"
        )
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


@check(CHECKS, "vm")
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
                    VmOverlayKind.LIVE
                    if dev in live
                    else VmOverlayKind.DEFINITION
                    if sources is not None and sources.get(dev) == base
                    else VmOverlayKind.UNKNOWN
                )
            elif state in ("shut off", "crashed"):
                kind = (
                    VmOverlayKind.OFFLINE if file_newer(host, HostPath(path), HostPath(base)) else VmOverlayKind.UNKNOWN
                )
            else:
                kind = VmOverlayKind.UNKNOWN
            match kind:
                case VmOverlayKind.LIVE:
                    yield Result(
                        WARN,
                        f"VM {vm}: disk {dev} is on {path}, {left}. The VM writes into it, and it "
                        f"grows. If `virsh blockjob {vm} {dev} --info` shows a commit ready (100 %), finish "
                        f"it with `virsh blockjob {vm} {dev} --pivot`; with no block job, merge it with the "
                        f"command below. Then check that `virsh domblklist {vm} --details` (also with "
                        f"--inactive) shows {base}, and remove the overlay file {guide}",
                        f"{v} blockcommit {q(vm)} {dev} --active --pivot --wait --verbose",
                    )
                case VmOverlayKind.DEFINITION:
                    yield Result(
                        WARN,
                        f"VM {vm}: disk {dev}: the definition names {path}, {left}, but the VM runs "
                        f"on {base} again: the overlay is stale. Do NOT qemu-img commit it (that would overwrite newer data in "
                        f"the image). Point the disk back with the command below (<source file='{base}'/>, "
                        f"and remove its <backingStore>), check `virsh domblklist {vm} --details --inactive`, "
                        f"then remove the overlay file {guide}",
                        f"{v} edit {q(vm)}",
                    )
                case VmOverlayKind.OFFLINE:
                    yield Result(
                        WARN,
                        f"VM {vm}: disk {dev} is on {path}, {left}. The VM is {state} on it, and it is "
                        f"newer than {base}: it holds the guest's last writes. Merge it offline with the "
                        f"command below, then point the disk back with `virsh edit {vm}` (<source "
                        f"file='{base}'/>, no <backingStore>), check `virsh domblklist {vm} --details "
                        f"--inactive`, and remove the overlay file {guide}",
                        f"sudo qemu-img commit -p {q(path)}",
                    )
                case _ if state in ("shut off", "crashed"):
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
                case _:
                    yield Result(
                        WARN,
                        f"VM {vm}: disk {dev} is on {path}, {left}. The VM's state "
                        f"({state or 'unreadable'}) or its disk listing does not tell how to put it back. "
                        f"Do not remove or commit anything yet: look with `virsh domstate {vm}` and "
                        f"`virsh domblklist {vm} --details` (and --inactive) {guide}",
                        f"sudo virsh -c {VIRSH_URI} domblklist {q(vm)} --details",
                    )


@check(CHECKS, "vm")
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


@check(CHECKS, "vm")
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
        extra = " Windows: the virtio drivers go in first (the plan shows how)." if kind != GuestKind.LINUX else ""
        yield Result(
            INFO,
            f"VM {vm}: hardware differs from what {host.cst} vm tune recommends: {first}.{extra} "
            f"See: {host.cst} vm tune plan {vm}",
        )


@check(CHECKS, "vm")
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
        p = vm_tools.probe_dir(d, (lambda x: host.path(HostPath(x))) if d.path == system else (lambda x: x))
        if not p.present or p.btrfs is False:
            continue
        path = p.path
        if p.nocow is None and not p.listed:
            yield Result(
                INFO,
                f"VM image folder {path}: NOCOW mark not readable as your user (root's "
                f"folder); {host.cst} vm nocow plan --root shows it",
            )
            continue
        cow = [i for i in p.images if i.nocow is False]
        if p.nocow is False:
            yield Result(
                INFO,
                f"VM image folder {path}: no NOCOW mark (+C), so new images there are "
                f"copy-on-write; {host.cst} vm nocow apply sets it",
            )
        if cow:
            listed = ", ".join(f"{i.name} ({format_bytes_gib_mib(i.alloc, mib_decimals=1)})" for i in cow)
            yield Result(
                INFO,
                f"VM image folder {path}: still copy-on-write: {listed}; "
                f"{host.cst} vm nocow plan prints how to convert (never done automatically)",
            )
        if p.nocow and not cow:
            yield Result(OK, f"VM image folder {path}: NOCOW, no copy-on-write image")


@check(CHECKS, "vm")
def check_vm_backup_alerts(host: DoctorHostPort) -> Iterator[Result]:
    """WARN when a VM's weekly NAS backup timer is on but btrfs-notify, which its failure alerts through,
    is missing (cst btrfs uninstall removes it when snapshots are not set up)."""
    try:
        timers = sorted(n for n in host.listdir(host.path(VM_TIMER_WANTS)) if n.startswith("cst-vm-backup@"))
    except OSError:
        return
    if not timers or runs(host, NOTIFY_HOST):
        return
    yield Result(
        WARN,
        f"{', '.join(timers)} on, but {NOTIFY_HOST} is missing: a failed NAS backup of the VM reaches no desktop, only the journal and the pending alerts above",
        refresh(host, "btrfs:alerts"),
    )

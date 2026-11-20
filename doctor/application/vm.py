# SPDX-License-Identifier: MPL-2.0
"""The vm area of `cst doctor`: the libvirt guests (CPU features, the backup index, the virt stack, overlays,
the backup timer, the virtual hardware, the image folders, the backup alerts)."""

from collections.abc import Callable, Iterator
import shlex
import time
from typing import Final, NamedTuple
import xml.etree.ElementTree as ET

from cpu.domain import CPU_DISABLE_DEFAULT
from cst_core.domain.errors import ExitStatus
from cst_core.domain.install import Component
from cst_core.domain.shapes import parse_json
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
    VM_OVERLAY_MARK,
    VM_RUN_DIR,
    VM_TIMER_WANTS,
    VM_TORN,
    VM_TORN_FORMAT,
    WARN,
    Deployment,
    HostPath,
    Result,
    name_list,
    parse_offered_features_xml,
    virt_changes,
    virt_versions,
    host_join,
)
from doctor.ports import CheckFn, DoctorHostPort, VmToolsPort
from vm.domain import LIVE_STATES, STOPPED_STATES, ImageProbe, VmOverlayKind
from vm.domain.backup import NO_BACKUPS_LIST, NO_TORN_LIST, is_index_entry, is_torn_entry, stored_list, stored_torn
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


def _passthrough_cpu(host: DoctorHostPort, vm: str) -> ET.Element | Result:
    """The <cpu> element of vm's definition when its mode is host-passthrough; else the line that says
    why there is nothing more to check (the definition cannot be read, or another mode)."""
    rc, out, err = domain_xml(host, vm)
    try:
        cpu = ET.fromstring(out).find("cpu") if rc == 0 else None
    except ET.ParseError:
        rc, err = 1, "definition unreadable"
        cpu = None
    if rc != 0:
        return Result(INFO, f"VM {vm}: definition not readable ({virsh_problem(rc, err)})")
    mode = cpu.get("mode", "custom") if cpu is not None else "not set"
    if cpu is None or mode != "host-passthrough":
        return Result(OK, f"VM {vm}: cpu mode {mode} (the CET/MBEC bootloop concerns host-passthrough only)")
    return cpu


def _features_result(
    host: DoctorHostPort, vm: str, missing: list[str], offered: set[str | None] | None, oproblem: str | None
) -> Result:
    """A host-passthrough VM whose definition does not disable the features of missing, against what this
    QEMU offers (offered_features' answer: offered, None with why in oproblem when it could not be read)."""
    if offered is None:
        return Result(
            INFO,
            f"VM {vm}: host-passthrough without disabling {', '.join(missing)}; whether "
            f"this QEMU passes them was not checked ({oproblem}). If Windows loops in "
            f"Automatic Repair: {host.cst} cpu vm-pin plan ({CET_ISSUE})",
        )
    bad = [n for n in missing if n in offered]
    if not bad:
        return Result(
            OK, f"VM {vm}: host-passthrough; this QEMU does not offer {', '.join(missing)} (nothing to disable yet)"
        )
    return Result(
        WARN,
        f"VM {vm}: host-passthrough passes {', '.join(bad)} to the guest (QEMU 11 and "
        "later offer them); Windows with Hyper-V/VBS/WSL2 then loops in Automatic Repair. "
        f"The fix shows the change to the definition; then {host.cst} cpu vm-pin apply. "
        f"Upstream: {CET_ISSUE}",
        f"{host.cst} cpu vm-pin plan",
    )


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
        cpu = _passthrough_cpu(host, vm)
        if isinstance(cpu, Result):
            yield cpu
            continue
        off = {f.get("name") for f in cpu.findall("feature") if f.get("policy") == "disable"}
        missing = [n for n in wanted if n not in off]
        if not missing:
            yield Result(OK, f"VM {vm}: host-passthrough with {', '.join(wanted)} disabled")
            continue
        # Asked at most once per check-up, and only for a VM that misses a disable.
        if oproblem is None:
            offered, oproblem = offered_features(host)
        yield _features_result(host, vm, missing, offered, oproblem)


def read_vm_index(host: DoctorHostPort) -> tuple[object, str | None] | None:
    """cst vm's backup index read as cst vm reads it (vm.adapters.backup's stored_backups): None when it
    is missing; (data, None) when it is JSON; (None, why) when it is there but cannot be read, or is not
    JSON. The one reader of the index here, so every check sees what cst vm sees; read once per check-up,
    so they all judge the same contents."""
    known = facts(host)
    if known.vm_index is None:
        known.vm_index = (_read_json_state(host, VM_INDEX),)
    return known.vm_index[0]


def _read_json_state(host: DoctorHostPort, path: HostPath) -> tuple[object, str | None] | None:
    """A JSON state file of cst vm read as cst vm reads it (vm.adapters.backup's load_json_file): None when
    it is missing; (data, None) when it is JSON; (None, why) when it is there but cannot be read, or is
    not JSON."""
    try:
        with host.open_bytes(host.path(path)) as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    except OSError as e:
        return None, e.strerror or str(e)
    try:  # decoded strictly, as cst vm decodes it: a byte that is not UTF-8 is not JSON there either
        return parse_json(raw.decode()), None
    except ValueError, RecursionError:
        return None, "not JSON"


def vm_index_data(host: DoctorHostPort) -> object:
    """cst vm's backup index as read, None when it is missing or unreadable."""
    read = read_vm_index(host)
    return None if read is None else read[0]


def vm_index_problem(host: DoctorHostPort) -> str | None:
    """Why cst vm's backup index cannot be read as one, as cst vm reads it; None when it is missing,
    readable, or of a newer cst vm."""
    read = read_vm_index(host)
    if read is None:
        return None
    data, why = read
    if why is not None:
        return why
    if VM_INDEX_FORMAT.newer(data, "version") is not None:
        return None
    return NO_BACKUPS_LIST if stored_list(data) is None else None


def newest_vm_backup(host: DoctorHostPort) -> float | None:
    """Epoch of the newest backup in cst vm's index, 0 when none or unreadable, None when the index is of
    a newer cst vm (not read: check_vm_index says so). Only the entries cst vm lists count
    (is_index_entry): one it keeps without understanding it is no backup it can restore."""
    data = vm_index_data(host)
    if VM_INDEX_FORMAT.newer(data, "version") is not None:
        return None
    return max((e.get("epoch", 0) for e in stored_list(data) or [] if is_index_entry(e)), default=0)


def nas_configured(host: DoctorHostPort) -> bool:
    """The /home NAS backup is set up (snapshots.conf is world-readable; read as btrfs-snapshot reads it)."""
    parsed = settings_of(host, SNAP_CONF)
    if not parsed:
        return False
    values = parsed.values
    return values.get("NAS_ENABLED") == "1" and bool(values.get("NAS_PATH"))


@check(CHECKS, "vm")
def check_vm_index(host: DoctorHostPort) -> Iterator[Result]:
    """WARN when cst vm's backup index is of a newer format (after going back to an older CST), or is
    there but cannot be read as an index: cst vm refuses to back up or list with it."""
    if (why := vm_index_problem(host)) is not None:
        yield Result(
            WARN,
            f"{VM_INDEX} cannot be read as cst vm's backup index ({why}): {host.cst} vm refuses to back up "
            "or list with it; move it aside to start a new index",
        )
        return
    found = VM_INDEX_FORMAT.newer(vm_index_data(host), "version")
    if found is not None:
        yield Result(
            WARN,
            f"{VM_INDEX_FORMAT.newer_text(VM_INDEX, found)}: {host.cst} vm refuses to back up or list "
            "with it; use the cst vm of that CST (its checkout: cst vm backup-timer on)",
        )


@check(CHECKS, "vm")
def check_vm_nas_torn(host: DoctorHostPort) -> Iterator[Result]:
    """WARN while cst vm's record of torn NAS snapshots (restic snapshots a failed NAS backup recorded
    as no backup, not forgotten as far as the record knows) names one: the next NAS backup of that VM
    to that repository forgets it first (or drops it, when restic no longer lists it), and skips its
    retention until then; the fix line points to the ways out by hand. WARN when the record is of a newer format or cannot be read as one: cst vm backup --nas
    refuses to run with it. Reads the file only (no restic)."""
    read = _read_json_state(host, VM_TORN)
    if read is None:
        return
    data, why = read
    if why is None and (found := VM_TORN_FORMAT.newer(data, "version")) is not None:
        yield Result(
            WARN,
            f"{VM_TORN_FORMAT.newer_text(VM_TORN, found)}: {host.cst} vm backup --nas refuses to run with it; "
            "use the cst vm of that CST",
        )
        return
    torn = stored_torn(data) if why is None else None
    if torn is None:
        yield Result(
            WARN,
            f"{VM_TORN} cannot be read as cst vm's record of torn NAS snapshots ({why or NO_TORN_LIST}): "
            f"{host.cst} vm backup --nas refuses to run with it; forget the snapshots it names by hand, then "
            "move it aside",
        )
        return
    named = [f"{e['vm']} {e['snapshot'][:8]} ({e['timestamp'] or '?'})" for e in torn if is_torn_entry(e)]
    if named:
        yield Result(
            WARN,
            f"{len(named)} restic snapshot(s) recorded as torn (no backup) by a failed NAS backup, in {VM_TORN}: "
            f"{name_list(named)}; the next `{host.cst} vm backup VM --nas` to the same repository forgets them "
            "(or drops those already forgotten) before its retention, and skips the retention until then",
            "or by hand: restic forget ID, with the repository's variables of vm/README.md, section 10.9; for a "
            f"VM or repository with no more NAS backups, and for an older cst-vm the weekly timer runs "
            f"(`{host.cst} vm backup-timer on VM`), see section 10.6",
        )


@check(CHECKS, "vm")
def check_vm_settings(host: DoctorHostPort) -> Iterator[Result]:
    yield from settings_file_warnings(host, VM_CONF, bash_sourced=False)


def _rollback(deps: list[Deployment], booted: Deployment) -> tuple[int, Deployment] | None:
    """(index, deployment) of the rollback: the first deployment after the booted one of the same OS."""
    i = next(n for n, d in enumerate(deps) if d is booted)
    return next(((n, d) for n, d in enumerate(deps) if n > i and d.get("osname") == booted.get("osname")), None)


def _backup_warning(host: DoctorHostPort, what: str, built: float, backup: float | None) -> Result:
    """No VM backup since the booted deployment was built (built: its epoch, 0 unknown; backup: the
    newest backup's, 0 none, None unknown)."""
    when = time.strftime("%Y-%m-%d", time.localtime(built)) if built else "?"
    if backup is None:
        last = "unknown (see the index line)"
    elif backup:
        last = time.strftime("%Y-%m-%d", time.localtime(backup))
    else:
        last = "never"
    if nas_configured(host):
        advice, fix = "The NAS backup works with the VM running", f"{host.cst} vm backup --nas"
    else:
        advice, fix = "Shut the VM down first", f"{host.cst} vm backup"
    return Result(
        WARN,
        f"{what}. No VM backup since the booted deployment was built ({when}; last backup: {last}). {advice}",
        fix,
    )


def _virt_change_results(
    host: DoctorHostPort, booted: Deployment, rb: tuple[int, Deployment], change: str, dom_fn: DomainsFn
) -> Iterator[Result]:
    """The virtualization packages changed from the rollback deployment rb (index, deployment) to the
    booted one: what keeps the way back for the VMs (the rollback pinned, a VM backup since the build)."""
    index, rollback = rb
    names, problem = dom_fn(host)
    rb_ver = rollback.get("version") or (rollback.get("checksum") or "?")[:10]
    what = f"virtualization packages changed from the rollback deployment {rb_ver} to the booted one: {change}"
    if names is None or len(names) == 0:
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
        yield _backup_warning(host, what, built, backup)


@check(CHECKS, "vm")
def check_virt_stack_change(host: DoctorHostPort, *, domains_fn: DomainsFn | None = None) -> Iterator[Result]:
    (booted, _, err), deps = ostree_facts(host)
    if err or booted is None:
        yield Result(INFO, f"virtualization packages: not compared ({err or 'no booted deployment'})")
        return
    rb = _rollback(deps, booted)
    if rb is None:
        yield Result(OK, "virtualization packages: no rollback deployment to compare with")
        return
    _, rollback = rb
    old, new = installed_packages(host, rollback), installed_packages(host, booted)
    if old is None or new is None:
        yield Result(INFO, "virtualization packages: not compared (rpm-ostree db list did not answer)")
        return
    change = virt_changes(virt_versions(old), virt_versions(new))
    if not change:
        yield Result(OK, "virtualization packages: the same in the booted and the rollback deployment")
        return
    dom_fn = libvirt_domains if domains_fn is None else domains_fn
    yield from _virt_change_results(host, booted, rb, change, dom_fn)


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


# Where the overlay lines send the reader.
OVERLAY_GUIDE: Final = '(vm/README.md, "If a backup left an overlay")'


class _OverlayDisks(NamedTuple):
    """A VM's disks on a NAS backup's overlay: {target: overlay} live (domblklist) and in its definition,
    and {target: source} of every live disk (None when domblklist did not answer)."""

    live: dict[str, str]
    defined: dict[str, str]
    sources: dict[str, str] | None


class _OverlayDisk(NamedTuple):
    """One disk on an overlay: its VM, target, overlay, the image under it, and the VM's state (None when
    virsh did not answer)."""

    vm: str
    dev: str
    path: str
    base: str
    state: str | None


def _live_sources(host: DoctorHostPort, vm: str) -> tuple[dict[str, str], dict[str, str] | None]:
    """({target: overlay}; {target: source} of the running VM's disks, None when domblklist did not answer)."""
    rc, out, _ = virsh(host, "domblklist", vm, "--details")
    if rc != 0:
        return {}, None
    sources: dict[str, str] = {}
    live: dict[str, str] = {}
    for ln in out.splitlines():
        parts = ln.split(None, 3)
        if len(parts) == 4 and parts[0] != "Type" and not parts[0].startswith("-"):
            sources[parts[2]] = parts[3].strip()
            if VM_OVERLAY_MARK in parts[3]:
                live.setdefault(parts[2], parts[3].strip())
    return live, sources


def _defined_overlays(host: DoctorHostPort, vm: str) -> dict[str, str]:
    """{target: overlay} of the disks vm's definition puts on an overlay ("?" for a disk without a target)."""
    rc, out, _ = domain_xml(host, vm)
    try:
        disks = ET.fromstring(out).findall("devices/disk") if rc == 0 else []
    except ET.ParseError:
        disks = []
    defined: dict[str, str] = {}
    for disk in disks:
        src, tgt = disk.find("source"), disk.find("target")
        path = src.get("file") if src is not None else None
        if path is not None and VM_OVERLAY_MARK in path:
            target = (tgt.get("dev") if tgt is not None else None) or "?"
            defined.setdefault(target, path)
    return defined


def _overlay_disks(host: DoctorHostPort, vm: str) -> _OverlayDisks:
    """vm's disks on an overlay, from domblklist, then from the definition."""
    live, sources = _live_sources(host, vm)
    return _OverlayDisks(live=live, defined=_defined_overlays(host, vm), sources=sources)


def _vm_state(host: DoctorHostPort, vm: str) -> str | None:
    """virsh domstate's answer ("running", "shut off", ...), None when virsh did not answer."""
    rc, out, _ = virsh(host, "domstate", vm)
    return first_line(out) if rc == 0 else None


def _overlay_kind(host: DoctorHostPort, disk: _OverlayDisk, disks: _OverlayDisks) -> VmOverlayKind:
    """How the disk can be put back on its image: the VM's state and where it runs; for a VM that is shut
    off or crashed, whether the overlay is newer than the image."""
    if disk.state in LIVE_STATES:
        if disk.dev in disks.live:
            return VmOverlayKind.LIVE
        if disks.sources is not None and disks.sources.get(disk.dev) == disk.base:
            return VmOverlayKind.DEFINITION
        return VmOverlayKind.UNKNOWN
    if disk.state in STOPPED_STATES:
        return (
            VmOverlayKind.OFFLINE
            if file_newer(host, HostPath(disk.path), HostPath(disk.base)) is True
            else VmOverlayKind.UNKNOWN
        )
    return VmOverlayKind.UNKNOWN


def _overlay_result(host: DoctorHostPort, disk: _OverlayDisk, kind: VmOverlayKind) -> Result:
    """The warning for a disk left on an overlay, with the way back for its kind."""
    vm, dev, path, base, state = disk
    q, v = shlex.quote, f"sudo virsh -c {VIRSH_URI}"
    left = f"an overlay left by `{host.cst} vm backup --nas` (it could not merge it back)"
    match kind:
        case VmOverlayKind.LIVE:
            return Result(
                WARN,
                f"VM {vm}: disk {dev} is on {path}, {left}. The VM writes into it, and it "
                f"grows. If `virsh blockjob {vm} {dev} --info` shows a commit ready (100 %), finish "
                f"it with `virsh blockjob {vm} {dev} --pivot`; with no block job, merge it with the "
                f"command below. Then check that `virsh domblklist {vm} --details` (also with "
                f"--inactive) shows {base}, and remove the overlay file {OVERLAY_GUIDE}",
                f"{v} blockcommit {q(vm)} {dev} --active --pivot --wait --verbose",
            )
        case VmOverlayKind.DEFINITION:
            return Result(
                WARN,
                f"VM {vm}: disk {dev}: the definition names {path}, {left}, but the VM runs "
                f"on {base} again: the overlay is stale. Do NOT qemu-img commit it (that would overwrite newer data in "
                f"the image). Point the disk back with the command below (<source file='{base}'/>, "
                f"and remove its <backingStore>), check `virsh domblklist {vm} --details --inactive`, "
                f"then remove the overlay file {OVERLAY_GUIDE}",
                f"{v} edit {q(vm)}",
            )
        case VmOverlayKind.OFFLINE:
            return Result(
                WARN,
                f"VM {vm}: disk {dev} is on {path}, {left}. The VM is {state} on it, and it is "
                f"newer than {base}: it holds the guest's last writes. Merge it offline with the "
                f"command below, then point the disk back with `virsh edit {vm}` (<source "
                f"file='{base}'/>, no <backingStore>), check `virsh domblklist {vm} --details "
                f"--inactive`, and remove the overlay file {OVERLAY_GUIDE}",
                f"sudo qemu-img commit -p {q(path)}",
            )
        case _ if state in STOPPED_STATES:
            return Result(
                WARN,
                f"VM {vm}: disk {dev} is on {path}, {left}. The VM is {state}, and the doctor "
                f"cannot tell whether that overlay still holds the guest's last writes: the image is "
                f"newer, or the times are unreadable. The image is newer both after a merge cut short "
                f"(the overlay is still needed) and after a merge that left the definition behind "
                f"(stale). Do not remove or commit anything yet: look at the times with the command "
                f"below and at the backup's own message or alert {OVERLAY_GUIDE}",
                f"sudo ls -l --time-style=full-iso {q(base)} {q(path)}",
            )
        case _:
            return Result(
                WARN,
                f"VM {vm}: disk {dev} is on {path}, {left}. The VM's state "
                f"({state or 'unreadable'}) or its disk listing does not tell how to put it back. "
                f"Do not remove or commit anything yet: look with `virsh domstate {vm}` and "
                f"`virsh domblklist {vm} --details` (and --inactive) {OVERLAY_GUIDE}",
                f"{v} domblklist {q(vm)} --details",
            )


@check(CHECKS, "vm")
def check_vm_overlays(host: DoctorHostPort, *, domains_fn: DomainsFn | None = None) -> Iterator[Result]:
    """WARN for a disk whose definition names an overlay of a NAS backup."""
    dom_fn = libvirt_domains if domains_fn is None else domains_fn
    names, _ = dom_fn(host)
    for vm in names or []:
        disks = _overlay_disks(host, vm)
        if not disks.live and not disks.defined:
            continue
        state = _vm_state(host, vm)
        pid = nas_backup_running(host, vm)
        for dev in sorted(set(disks.live) | set(disks.defined)):
            path = disks.live.get(dev) or disks.defined[dev]
            disk = _OverlayDisk(vm, dev, path, path.split(VM_OVERLAY_MARK, 1)[0], state)
            if pid:
                yield Result(
                    INFO,
                    f"VM {vm}: disk {dev} on {path}: a NAS backup is in progress (pid {pid}); "
                    "it merges the overlay back when restic is done",
                )
                continue
            yield _overlay_result(host, disk, _overlay_kind(host, disk, disks))


@check(CHECKS, "vm")
def check_vm_backup_timer(host: DoctorHostPort, *, domains_fn: DomainsFn | None = None) -> Iterator[Result]:
    """INFO: whether the weekly NAS backup timer of each VM is on."""
    dom_fn = libvirt_domains if domains_fn is None else domains_fn
    names, _ = dom_fn(host)
    if names is None or len(names) == 0:
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
    if names is None or len(names) == 0:
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
        yield from _image_dir_results(host, p)


def _image_dir_results(host: DoctorHostPort, p: ImageProbe) -> Iterator[Result]:
    """The lines of one image folder: none when it is missing or not on btrfs."""
    if not p.present or p.btrfs is False:
        return
    path = p.path
    if p.nocow is None and not p.listed:
        yield Result(
            INFO,
            f"VM image folder {path}: NOCOW mark not readable as your user (root's "
            f"folder); {host.cst} vm nocow plan --root shows it",
        )
        return
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
    if p.nocow is True and not cow:
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
        refresh(host, Component.BTRFS_ALERTS),
    )

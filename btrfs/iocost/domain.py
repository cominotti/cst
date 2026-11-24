# SPDX-License-Identifier: MPL-2.0
"""Pure domain planning, parsing, and formatting for `cst btrfs iocost` (`btrfs/btrfs-iocost`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Final

from cst_core.domain.text import HELP_WORDS, parse_subcommand_head

IOCOST_QOS_PATH: Final = "/sys/fs/cgroup/io.cost.qos"
IOCOST_MODEL_PATH: Final = "/sys/fs/cgroup/io.cost.model"
CGROUP_CONTROLLERS_PATH: Final = "/sys/fs/cgroup/cgroup.controllers"
IOCOST_MANAGED_FILE: Final = "/run/btrfs-setup/iocost-managed"
IOCOST_SERVICE: Final = "cst-btrfs-iocost.service"
IOCOST_RULE_DEST: Final = "/etc/udev/rules.d/91-cst-iocost.rules"
UDEV_CONTROL: Final = "/run/udev/control"  # udevd's control socket: there only while udevd runs
# One run at a time (the boot service, the udev rule's per-disk runs, the commands): each holds this lock
# from its read of io.cost.qos and the managed file to its save of the managed file.
IOCOST_LOCK: Final = "/run/btrfs-setup/iocost-managed.lock"
IOCOST_LOCK_TIMEOUT_S: Final = 30.0  # inside the 180 s udev gives a RUN program
# udev properties of a disk (`udevadm info -q property`): 90-iocost.rules' hwdb solution, and the stamp
# udev sets once it has processed the device (without it, the device's rules have not run yet).
IOCOST_SOLUTIONS_PROP: Final = "IOCOST_SOLUTIONS"
USEC_INITIALIZED_PROP: Final = "USEC_INITIALIZED"
UDEVADM_TIMEOUT_S: Final = 10.0
IOCOST_ENABLE_CTRL_AUTO: Final = "enable=1 ctrl=auto"
IOCOST_DISABLE_CTRL_AUTO: Final = "enable=0 ctrl=auto"
_CTRL_AUTO: Final = "auto"  # the ctrl CST writes; 90-iocost.rules' hwdb solutions write ctrl=user

_AUTO_DISK_RE: Final = re.compile(r"^(?:nvme[0-9]+n[0-9]+|sd[a-z]+|vd[a-z]+)$")
_LIVE_ONLY_FLAG: Final = "--live-only"
_DEVICE_FLAG: Final = "--device"


class IocostAction(StrEnum):
    """Supported subcommands of `cst btrfs iocost` / `btrfs-iocost`."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"
    HELP = "help"


@dataclass(frozen=True, slots=True)
class IocostParsedArgs:
    """Validated CLI arguments for `btrfs-iocost`."""

    action: IocostAction
    live_only: bool = False
    device: str | None = None
    error: str | None = None
    code: int = 0


def is_iocost_write(action: IocostAction) -> bool:
    """True for mutating `btrfs-iocost` subcommands."""
    return action in (IocostAction.APPLY, IocostAction.REVERT)


@dataclass(frozen=True, slots=True)
class IocostQosEntry:
    """Parsed line from `/sys/fs/cgroup/io.cost.qos`."""

    devno: str
    enable: int
    ctrl: str
    raw: str


@dataclass(frozen=True, slots=True)
class BlockDisk:
    """Block disk metadata scanned under `/sys/block`."""

    name: str
    devno: str
    rotational: bool
    scheduler: str
    model: str = ""
    hwdb_solution: str = ""  # IOCOST_SOLUTIONS from udev: 90-iocost.rules configures this disk itself
    udev_ready: bool = True  # False: udev has not processed it yet (no USEC_INITIALIZED)
    udev_error: str = ""  # why udevadm info could not tell (the disk is then judged without it)


@dataclass(frozen=True, slots=True)
class IocostSnapshot:
    """Collected kernel blk-iocost state, block devices, and installed units."""

    io_controller_present: bool | None
    qos_exists: bool
    qos_entries: Mapping[str, IocostQosEntry]
    model_raw: str
    disks: tuple[BlockDisk, ...]
    managed_devnos: frozenset[str]
    service_present: bool
    service_active: str
    service_enabled: str
    rule_installed: bool


@dataclass(frozen=True, slots=True)
class IocostWriteOp:
    """One planned sysfs write to `/sys/fs/cgroup/io.cost.qos`."""

    devno: str
    device_name: str
    line: str
    reason: str


@dataclass(frozen=True, slots=True)
class IocostPlan:
    """Pure plan for `apply` or `revert`."""

    writes: tuple[IocostWriteOp, ...]
    unchanged: tuple[str, ...]
    preexisting: tuple[str, ...]
    next_managed: frozenset[str]
    error: str | None = None
    revert_mode: bool = False
    notes: tuple[str, ...] = ()


def normalize_device_name(raw: str) -> str:
    """Strip `/dev/` prefix and surrounding whitespace from a block device argument."""
    return raw.strip().removeprefix("/dev/")


def parse_iocost_qos(text: str) -> dict[str, IocostQosEntry]:
    """Parse kernel `io.cost.qos` lines keyed by `MAJOR:MINOR`."""
    entries: dict[str, IocostQosEntry] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        devno = parts[0]
        if ":" not in devno:
            continue
        kv = {key: value for key, sep, value in (item.partition("=") for item in parts[1:]) if sep}
        enable_val = 1 if kv.get("enable") == "1" else 0
        ctrl_val = kv.get("ctrl", "auto")
        entries[devno] = IocostQosEntry(devno=devno, enable=enable_val, ctrl=ctrl_val, raw=line.strip())
    return entries


def is_auto_eligible_disk(disk: BlockDisk) -> bool:
    """True for non-rotational NVMe/SATA/VirtIO disks selected automatically by baseline."""
    return not disk.rotational and _AUTO_DISK_RE.fullmatch(disk.name) is not None


def format_iocost_usage(prog: str) -> str:
    """Return CLI help text for `btrfs-iocost` / `cst btrfs iocost`."""
    return (
        f"{prog}: enable kernel blk-iocost (enable=1 ctrl=auto) on SSDs without an upstream hwdb solution.\n\n"
        "Commands:\n"
        "  status                             read-only (user/toolbox): show cgroup io controller, io.cost.qos, disks, unit\n"
        "  plan [--live-only [--device N]]    dry run (user/toolbox): show what `apply` would install and write\n"
        "  apply [--live-only [--device N]]   (root on host) enable blk-iocost and install persistent unit + udev rule;\n"
        "                                     --live-only writes io.cost.qos only, --device N one disk only\n"
        "  revert [--live-only]               (root on host) reset CST-managed io.cost.qos entries and remove unit/rule\n"
        "  help                               show this help\n\n"
        f"Usage: {prog} <status|plan|apply|revert|help> [--live-only [--device NAME]]\n"
    )


def _consume_device_flag(rest: Sequence[str], idx: int) -> tuple[str | None, int, str | None]:
    tok = rest[idx]
    if tok == _DEVICE_FLAG:
        if idx + 1 >= len(rest):
            return None, idx, f"{_DEVICE_FLAG} expects a block device name (e.g. nvme0n1)"
        norm = normalize_device_name(rest[idx + 1])
        return (norm or None), idx + 2, (None if norm else f"{_DEVICE_FLAG} device name cannot be empty")
    prefix = f"{_DEVICE_FLAG}="
    norm = normalize_device_name(tok.removeprefix(prefix))
    return (norm or None), idx + 1, (None if norm else f"{_DEVICE_FLAG} device name cannot be empty")


def _parse_iocost_flags(action: IocostAction, rest: Sequence[str]) -> tuple[bool, str | None, str | None]:
    """--live-only and --device of the arguments after the action: (live_only, device, error)."""
    live_only = False
    device: str | None = None
    idx = 0
    err: str | None = None
    while idx < len(rest):
        tok = rest[idx]
        if tok == _LIVE_ONLY_FLAG and action != IocostAction.STATUS and not live_only:
            live_only = True
            idx += 1
        elif (tok == _DEVICE_FLAG or tok.startswith(f"{_DEVICE_FLAG}=")) and action in (
            IocostAction.PLAN,
            IocostAction.APPLY,
        ):
            device, idx, err = _consume_device_flag(rest, idx)
            if err is not None:
                break
        else:
            err = f"{action.value}: unexpected argument {tok!r}"
            break
    return live_only, device, err


def parse_iocost_args(argv: Sequence[str]) -> IocostParsedArgs:
    """Parse CLI arguments after `btrfs-iocost` / `cst btrfs iocost`."""
    action, rest, head_err = parse_subcommand_head(
        argv, IocostAction, default=IocostAction.STATUS, help_action=IocostAction.HELP
    )
    if head_err is not None:
        return IocostParsedArgs(action, error=head_err, code=2)
    if any(tok in HELP_WORDS for tok in rest):
        return IocostParsedArgs(IocostAction.HELP)

    live_only, device, err = _parse_iocost_flags(action, rest)
    if err is None and device is not None and not live_only:
        # One disk now: without --live-only, apply would also install the service, which writes every disk.
        err = f"{_DEVICE_FLAG} needs {_LIVE_ONLY_FLAG} (it writes one disk now and installs nothing)"
    if err is not None:
        return IocostParsedArgs(action, error=err, code=2)
    return IocostParsedArgs(action, live_only=live_only, device=device)


@dataclass(frozen=True, slots=True)
class _ApplyPlanLists:
    """What plan_iocost_apply gathers, disk by disk."""

    next_managed: set[str]
    writes: list[IocostWriteOp] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    preexisting: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _select_iocost_disks(snap: IocostSnapshot, target_device: str | None) -> list[BlockDisk] | None:
    """The disks to plan: target_device's (None when it is not a whole disk), else every eligible one."""
    if target_device is None:
        return [d for d in snap.disks if is_auto_eligible_disk(d)]
    matched = [d for d in snap.disks if d.name == target_device]
    return matched or None


def _plan_hwdb_disk(acc: _ApplyPlanLists, snap: IocostSnapshot, disk: BlockDisk) -> None:
    entry = snap.qos_entries.get(disk.devno)
    if disk.devno in snap.managed_devnos and entry is not None and (entry.enable, entry.ctrl) == (1, _CTRL_AUTO):
        # CST's own write of earlier this boot is still there (udev never replaced it): it stays
        # CST's, so revert resets it; the next boot leaves the disk to udev.
        acc.unchanged.append(
            f"{disk.devno} ({disk.name}: CST's enable=1 auto from earlier this boot, kept until revert"
            f" or reboot; hwdb solution {disk.hwdb_solution})"
        )
        return
    acc.preexisting.append(f"{disk.devno} ({disk.name}: hwdb solution {disk.hwdb_solution}; left to udev)")
    acc.next_managed.discard(disk.devno)


def _plan_iocost_disk(acc: _ApplyPlanLists, snap: IocostSnapshot, disk: BlockDisk, *, scan_all: bool) -> None:
    # The scan of every disk (the boot service) leaves udev's own disks alone; one --device run is
    # the udev rule's, which only matches a disk without a hwdb solution.
    if scan_all and disk.hwdb_solution:
        _plan_hwdb_disk(acc, snap, disk)
        return
    if scan_all and not disk.udev_ready:
        acc.notes.append(f"{disk.name}: not processed by udev yet; the udev rule writes it when udev does")
        return
    if scan_all and disk.udev_error:
        acc.notes.append(f"{disk.name}: udevadm info failed ({disk.udev_error}); judged without its hwdb solution")
    entry = snap.qos_entries.get(disk.devno)
    if entry is not None and entry.enable == 1:
        if disk.devno in snap.managed_devnos:
            acc.unchanged.append(f"{disk.devno} ({disk.name}: already enable=1 {entry.ctrl})")
        else:
            acc.preexisting.append(f"{disk.devno} ({disk.name}: pre-existing/vendor enable=1 {entry.ctrl}; kept)")
        return
    acc.next_managed.add(disk.devno)
    acc.writes.append(
        IocostWriteOp(
            devno=disk.devno,
            device_name=disk.name,
            line=f"{disk.devno} {IOCOST_ENABLE_CTRL_AUTO}",
            reason="non-rotational block disk" if not disk.rotational else "requested device",
        )
    )


def plan_iocost_apply(snap: IocostSnapshot, *, target_device: str | None = None) -> IocostPlan:
    """Compute desired `io.cost.qos` writes while respecting pre-existing/vendor `enable=1` rows."""
    selected = _select_iocost_disks(snap, target_device)
    if selected is None:
        return IocostPlan(
            writes=(),
            unchanged=(),
            preexisting=(),
            next_managed=snap.managed_devnos,
            error=f"block device {target_device!r} not found as a whole disk under /sys/block",
        )
    acc = _ApplyPlanLists(next_managed=set(snap.managed_devnos))
    for disk in selected:
        _plan_iocost_disk(acc, snap, disk, scan_all=target_device is None)
    return IocostPlan(
        writes=tuple(acc.writes),
        unchanged=tuple(acc.unchanged),
        preexisting=tuple(acc.preexisting),
        next_managed=frozenset(acc.next_managed),
        notes=tuple(acc.notes),
    )


def plan_iocost_revert(snap: IocostSnapshot) -> IocostPlan:
    """Compute sysfs reverts solely for `devno`s previously enabled by CST."""
    by_devno = {d.devno: d.name for d in snap.disks}
    writes: list[IocostWriteOp] = []
    unchanged: list[str] = []
    for devno in sorted(snap.managed_devnos):
        name = by_devno.get(devno, "unknown")
        entry = snap.qos_entries.get(devno)
        if entry is not None and entry.enable == 1:
            writes.append(
                IocostWriteOp(
                    devno=devno,
                    device_name=name,
                    line=f"{devno} {IOCOST_DISABLE_CTRL_AUTO}",
                    reason="revert CST blk-iocost",
                )
            )
        else:
            unchanged.append(f"{devno} ({name}: already enable=0 or absent)")
    preexisting = tuple(
        f"{devno} ({by_devno.get(devno, 'unknown')}: non-CST enable=1 kept)"
        for devno, entry in sorted(snap.qos_entries.items())
        if entry.enable == 1 and devno not in snap.managed_devnos
    )
    return IocostPlan(
        writes=tuple(writes),
        unchanged=tuple(unchanged),
        preexisting=preexisting,
        next_managed=frozenset(),
        revert_mode=True,
    )


def _io_controller_text(snap: IocostSnapshot) -> str:
    if snap.io_controller_present is True:
        return "present"
    if snap.io_controller_present is False:
        return "absent"
    return "unknown"


def _iocost_disk_tag(snap: IocostSnapshot, disk: BlockDisk, entry: IocostQosEntry | None) -> str:
    if disk.devno in snap.managed_devnos:
        return "CST"
    if disk.hwdb_solution:
        return f"hwdb solution {disk.hwdb_solution}"
    return "vendor/manual" if entry and entry.enable == 1 else "off"


def _iocost_disk_line(snap: IocostSnapshot, disk: BlockDisk) -> str:
    entry = snap.qos_entries.get(disk.devno)
    tag = _iocost_disk_tag(snap, disk, entry)
    state = f"enable={entry.enable} ctrl={entry.ctrl}" if entry is not None else "unset"
    model = f" [{disk.model}]" if disk.model else ""
    return f"  {disk.name:<10} ({disk.devno}, sched={disk.scheduler}){model}: {state} ({tag})"


def format_iocost_status(snap: IocostSnapshot) -> list[str]:
    """Render readable `cst btrfs iocost status` lines."""
    ctrl_txt = _io_controller_text(snap)
    active_count = sum(1 for e in snap.qos_entries.values() if e.enable == 1)
    by_devno = {d.devno: d for d in snap.disks}
    qos_state = "present" if snap.qos_exists else "absent"
    lines = [f"cgroup io:   {ctrl_txt}, {IOCOST_QOS_PATH} {qos_state} ({active_count} active device(s))"]
    if snap.service_present:
        lines.append(f"service:     {IOCOST_SERVICE} ({snap.service_active}, {snap.service_enabled})")
    else:
        lines.append(f"service:     {IOCOST_SERVICE} (not installed)")
    lines.append(f"udev rule:   {IOCOST_RULE_DEST} ({'installed' if snap.rule_installed else 'absent'})")

    eligible = [d for d in snap.disks if is_auto_eligible_disk(d)]
    if not eligible:
        lines.append("disks:       no non-rotational nvme/sd/vd disks found under /sys/block")
    lines.extend(_iocost_disk_line(snap, d) for d in eligible)

    extra = [e for devno, e in sorted(snap.qos_entries.items()) if devno not in {d.devno for d in eligible}]
    for entry in extra:
        dname = by_devno[entry.devno].name if entry.devno in by_devno else entry.devno
        lines.append(f"  {dname:<10} ({entry.devno}): {entry.raw}")
    return lines


def format_iocost_plan(
    snap: IocostSnapshot,
    plan: IocostPlan,
    *,
    live_only: bool = False,
    component_changes: Sequence[str] | None = None,
) -> list[str]:
    """Render dry-run output for `cst btrfs iocost plan` (live_only: only the io.cost.qos writes, no component).

    component_changes: the component's files apply would write or record again (the installer's plan);
    None when that could not be asked (no checkout, or the installer's plan failed)."""
    if plan.error is not None:
        return [f"ERROR: {plan.error}", "Dry run: nothing is changed."]
    lines: list[str] = []
    if snap.io_controller_present is False:
        lines.append("WARN: cgroup v2 'io' controller is not listed in cgroup.controllers")
    if not snap.qos_exists:
        lines.append(f"WARN: {IOCOST_QOS_PATH} is absent (kernel without CONFIG_BLK_CGROUP_IOCOST?)")
    if live_only:
        lines.append("live only:     io.cost.qos writes; no unit, rule or record change")
    elif not snap.service_present or not snap.rule_installed:
        lines.append(f"would install: component btrfs:iocost ({IOCOST_SERVICE}, {IOCOST_RULE_DEST}) + enable service")
    elif component_changes is not None and len(component_changes) > 0:
        lines.append(f"would update:  component btrfs:iocost ({', '.join(component_changes)}) + enable service")
    else:
        lines.append(f"unchanged:     component btrfs:iocost ({snap.service_active}, {snap.service_enabled})")
    lines.extend(f"would write:   {IOCOST_QOS_PATH} <- '{w.line}' ({w.device_name}, {w.reason})" for w in plan.writes)
    lines.extend(f"unchanged:     {u}" for u in plan.unchanged)
    lines.extend(f"pre-existing:  {p}" for p in plan.preexisting)
    lines.extend(f"note:          {n}" for n in plan.notes)
    lines.append("Dry run: nothing is changed.")
    return lines

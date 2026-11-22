# SPDX-License-Identifier: MPL-2.0
"""Pure domain planning, parsing, and formatting for `cst btrfs iocost` (`btrfs/btrfs-iocost`)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Final

from cst_core.domain.text import parse_subcommand_head

IOCOST_QOS_PATH: Final = "/sys/fs/cgroup/io.cost.qos"
IOCOST_MODEL_PATH: Final = "/sys/fs/cgroup/io.cost.model"
CGROUP_CONTROLLERS_PATH: Final = "/sys/fs/cgroup/cgroup.controllers"
IOCOST_MANAGED_FILE: Final = "/run/btrfs-setup/iocost-managed"
IOCOST_SERVICE: Final = "cst-btrfs-iocost.service"
IOCOST_RULE_DEST: Final = "/etc/udev/rules.d/91-cst-iocost.rules"
IOCOST_ENABLE_CTRL_AUTO: Final = "enable=1 ctrl=auto"
IOCOST_DISABLE_CTRL_AUTO: Final = "enable=0 ctrl=auto"

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
        kv = dict(item.partition("=")[0::2] for item in parts[1:] if "=" in item)
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
        "  plan [--device NAME]               dry run (user/toolbox): show what `apply` would install and write\n"
        "  apply [--live-only] [--device N]   (root on host) enable blk-iocost and install persistent unit + udev rule\n"
        "  revert [--live-only]               (root on host) reset CST-managed io.cost.qos entries and remove unit/rule\n"
        "  help                               show this help\n\n"
        f"Usage: {prog} <status|plan|apply|revert|help> [--live-only] [--device NAME]\n"
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


def parse_iocost_args(argv: Sequence[str]) -> IocostParsedArgs:
    """Parse CLI arguments after `btrfs-iocost` / `cst btrfs iocost`."""
    action, rest, head_err = parse_subcommand_head(
        argv, IocostAction, default=IocostAction.STATUS, help_action=IocostAction.HELP
    )
    if head_err is not None:
        return IocostParsedArgs(action, error=head_err, code=2)

    live_only = False
    device: str | None = None
    idx = 0
    err: str | None = None
    while idx < len(rest):
        tok = rest[idx]
        if tok == _LIVE_ONLY_FLAG and action in (IocostAction.APPLY, IocostAction.REVERT) and not live_only:
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
    if err is not None:
        return IocostParsedArgs(action, error=err, code=2)
    return IocostParsedArgs(action, live_only=live_only, device=device)


def plan_iocost_apply(snap: IocostSnapshot, *, target_device: str | None = None) -> IocostPlan:
    """Compute desired `io.cost.qos` writes while respecting pre-existing/vendor `enable=1` rows."""
    if target_device is not None:
        matched = [d for d in snap.disks if d.name == target_device]
        if not matched:
            return IocostPlan(
                writes=(),
                unchanged=(),
                preexisting=(),
                next_managed=snap.managed_devnos,
                error=f"block device {target_device!r} not found as a whole disk under /sys/block",
            )
        selected = matched
    else:
        selected = [d for d in snap.disks if is_auto_eligible_disk(d)]

    writes: list[IocostWriteOp] = []
    unchanged: list[str] = []
    preexisting: list[str] = []
    next_managed = set(snap.managed_devnos)

    for disk in selected:
        entry = snap.qos_entries.get(disk.devno)
        if entry is not None and entry.enable == 1:
            if disk.devno in snap.managed_devnos:
                unchanged.append(f"{disk.devno} ({disk.name}: already enable=1 {entry.ctrl})")
            else:
                preexisting.append(f"{disk.devno} ({disk.name}: pre-existing/vendor enable=1 {entry.ctrl}; kept)")
            continue
        next_managed.add(disk.devno)
        writes.append(
            IocostWriteOp(
                devno=disk.devno,
                device_name=disk.name,
                line=f"{disk.devno} {IOCOST_ENABLE_CTRL_AUTO}",
                reason="non-rotational block disk" if not disk.rotational else "requested device",
            )
        )
    return IocostPlan(
        writes=tuple(writes),
        unchanged=tuple(unchanged),
        preexisting=tuple(preexisting),
        next_managed=frozenset(next_managed),
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


def format_iocost_status(snap: IocostSnapshot) -> list[str]:
    """Render readable `cst btrfs iocost status` lines."""
    ctrl_txt = (
        "present"
        if snap.io_controller_present is True
        else ("absent" if snap.io_controller_present is False else "unknown")
    )
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
    for d in eligible:
        entry = snap.qos_entries.get(d.devno)
        tag = "CST" if d.devno in snap.managed_devnos else ("vendor/manual" if entry and entry.enable == 1 else "off")
        state = f"enable={entry.enable} ctrl={entry.ctrl}" if entry is not None else "unset"
        model = f" [{d.model}]" if d.model else ""
        lines.append(f"  {d.name:<10} ({d.devno}, sched={d.scheduler}){model}: {state} ({tag})")

    extra = [e for devno, e in sorted(snap.qos_entries.items()) if devno not in {d.devno for d in eligible}]
    for entry in extra:
        dname = by_devno[entry.devno].name if entry.devno in by_devno else entry.devno
        lines.append(f"  {dname:<10} ({entry.devno}): {entry.raw}")
    return lines


def format_iocost_plan(snap: IocostSnapshot, plan: IocostPlan) -> list[str]:
    """Render dry-run output for `cst btrfs iocost plan`."""
    if plan.error is not None:
        return [f"ERROR: {plan.error}", "Dry run: nothing is changed."]
    lines: list[str] = []
    if snap.io_controller_present is False:
        lines.append("WARN: cgroup v2 'io' controller is not listed in cgroup.controllers")
    if not snap.qos_exists:
        lines.append(f"WARN: {IOCOST_QOS_PATH} is absent (kernel without CONFIG_BLK_CGROUP_IOCOST?)")
    if not snap.service_present or not snap.rule_installed:
        lines.append(f"would install: component btrfs:iocost ({IOCOST_SERVICE}, {IOCOST_RULE_DEST}) + enable service")
    else:
        lines.append(f"unchanged:     component btrfs:iocost ({snap.service_active}, {snap.service_enabled})")
    lines.extend(f"would write:   {IOCOST_QOS_PATH} <- '{w.line}' ({w.device_name}, {w.reason})" for w in plan.writes)
    lines.extend(f"unchanged:     {u}" for u in plan.unchanged)
    lines.extend(f"pre-existing:  {p}" for p in plan.preexisting)
    lines.append("Dry run: nothing is changed.")
    return lines

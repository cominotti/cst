# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapters for `cst btrfs iocost` (`btrfs/btrfs-iocost`)."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Final

from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import ensure_dir, read_text, write_atomic
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import (
    ManagedCliEnv,
    host_view_path,
    installer_script_for,
    probe_systemd_unit,
    run_installer,
)
from cst_core.domain.install import Component
from cst_core.domain.text import group
from cst_core.ports import CommandResult, CommandRunnerPort, ExecutionContextPort, OutputMode
from btrfs.iocost.domain import (
    CGROUP_CONTROLLERS_PATH,
    IOCOST_MANAGED_FILE,
    IOCOST_MODEL_PATH,
    IOCOST_QOS_PATH,
    IOCOST_RULE_DEST,
    IOCOST_SERVICE,
    UDEV_CONTROL,
    BlockDisk,
    IocostAction,
    IocostParsedArgs,
    IocostPlan,
    IocostSnapshot,
    format_iocost_plan,
    format_iocost_status,
    parse_iocost_qos,
    plan_iocost_apply,
    plan_iocost_revert,
)

_DEVNO_RE: Final = re.compile(r"^[0-9]+:[0-9]+$")
_ACTIVE_SCHED_RE: Final = re.compile(r"\[([^\]]+)\]")


@dataclass(frozen=True, slots=True)
class IocostPaths:
    """Resolved host sysfs, cgroup, unit, and runtime paths for `btrfs-iocost`."""

    qos_path: str = IOCOST_QOS_PATH
    model_path: str = IOCOST_MODEL_PATH
    controllers_path: str = CGROUP_CONTROLLERS_PATH
    sys_block: str = "/sys/block"
    managed_file: str = IOCOST_MANAGED_FILE
    rule_dest: str = IOCOST_RULE_DEST
    installer_script: str = ""
    udev_control: str = UDEV_CONTROL


def iocost_paths_for(
    entry_file: str, *, ctx: ExecutionContextPort | None = None, read_only: bool = True, fallback_root: str = ""
) -> IocostPaths:
    """Resolve `IocostPaths`, mapping host views through `/run/host` for read-only containers."""
    exec_ctx = ctx or HostExecutionContext()
    installer = installer_script_for(entry_file, fallback_root)
    if not read_only:
        return IocostPaths(installer_script=installer)
    raw = (
        IOCOST_QOS_PATH,
        IOCOST_MODEL_PATH,
        CGROUP_CONTROLLERS_PATH,
        "/sys/block",
        IOCOST_MANAGED_FILE,
        IOCOST_RULE_DEST,
    )
    return IocostPaths(*[host_view_path(exec_ctx, item) for item in raw], installer_script=installer)


def _active_scheduler(raw: str | None) -> str:
    if raw is None:
        return "unknown"
    text = raw.strip()
    m = _ACTIVE_SCHED_RE.search(text)
    return group(m, 1) if m is not None else (text or "none")


def _scan_sys_block(sys_block: str) -> tuple[BlockDisk, ...]:
    if not os.path.isdir(sys_block):
        return ()
    disks: list[BlockDisk] = []
    for name in sorted(os.listdir(sys_block)):
        base = os.path.join(sys_block, name)
        if os.path.exists(os.path.join(base, "partition")):
            continue
        dev_raw = (read_text(os.path.join(base, "dev")) or "").strip()
        if _DEVNO_RE.fullmatch(dev_raw) is None:
            continue
        rot_raw = (read_text(os.path.join(base, "queue", "rotational")) or "").strip()
        sched = _active_scheduler(read_text(os.path.join(base, "queue", "scheduler")))
        model = (read_text(os.path.join(base, "device", "model")) or "").strip()
        disks.append(BlockDisk(name=name, devno=dev_raw, rotational=(rot_raw != "0"), scheduler=sched, model=model))
    return tuple(disks)


def _read_managed_devnos(path: str) -> frozenset[str]:
    text = read_text(path)
    if text is None:
        return frozenset()
    return frozenset(line.strip() for line in text.splitlines() if _DEVNO_RE.fullmatch(line.strip()))


def _save_managed_devnos(path: str, devnos: frozenset[str]) -> None:
    ordered = sorted(devnos)
    if not ordered:
        if os.path.lexists(path):
            os.remove(path)
        return
    ensure_dir(os.path.dirname(path), 0o755)
    write_atomic(path, "".join(f"{d}\n" for d in ordered), mode=0o644, durable=False, relabel=False)


def _write_qos_line(qos_path: str, payload: str) -> OSError | None:
    try:
        with open(qos_path, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
    except OSError as exc:
        return exc
    return None


def collect_iocost_snapshot(paths: IocostPaths, *, runner: CommandRunnerPort | None = None) -> IocostSnapshot:
    """Collect kernel `io.cost.qos`, block devices, runtime state, and systemd unit state."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    ctrl_raw = read_text(paths.controllers_path)
    io_present = ("io" in ctrl_raw.split()) if ctrl_raw is not None else None
    qos_raw = read_text(paths.qos_path)
    svc_present, active, enabled = probe_systemd_unit(cmd_runner, IOCOST_SERVICE)
    return IocostSnapshot(
        io_controller_present=io_present,
        qos_exists=qos_raw is not None,
        qos_entries=parse_iocost_qos(qos_raw or ""),
        model_raw=(read_text(paths.model_path) or "").strip(),
        disks=_scan_sys_block(paths.sys_block),
        managed_devnos=_read_managed_devnos(paths.managed_file),
        service_present=svc_present,
        service_active=active,
        service_enabled=enabled,
        rule_installed=os.path.exists(paths.rule_dest),
    )


def _apply_qos_writes(paths: IocostPaths, snap: IocostSnapshot, plan: IocostPlan) -> tuple[int, list[str]]:
    """Write the plan's io.cost.qos lines and save the devices CST manages: (1 when a write failed, lines)."""
    if not snap.qos_exists:
        if plan.revert_mode:
            # No blk-iocost here: nothing to reset, and nothing stays CST's.
            _save_managed_devnos(paths.managed_file, frozenset())
        return 0, [f"skipped:   {paths.qos_path} unavailable"]
    managed = set(plan.next_managed)
    out: list[str] = []
    rc = 0
    verb = "reverted:" if plan.revert_mode else "set:"
    for op in plan.writes:
        err = _write_qos_line(paths.qos_path, op.line)
        if err is None:
            out.append(f"{verb:<10} {paths.qos_path} <- '{op.line}' ({op.device_name})")
            continue
        rc = 1
        out.append(f"ERROR: {paths.qos_path} {op.device_name}: {err.strerror or err}")
        if plan.revert_mode:
            managed.add(op.devno)
        else:
            managed.discard(op.devno)
    out.extend(f"unchanged: {item}" for item in plan.unchanged)
    out.extend(f"kept:      {item}" for item in plan.preexisting)
    _save_managed_devnos(paths.managed_file, frozenset(managed))
    return rc, out


def _installer_step(runner: CommandRunnerPort, script: str, subargs: list[str]) -> tuple[int, list[str]]:
    rc, lines, err = run_installer(runner, script, subargs)
    return rc, [*lines, *([err] if err else [])]


def _failed(what: str, result: CommandResult) -> tuple[int, list[str]]:
    return (result.returncode or 1), [f"ERROR: {what} failed: {result.stderr.strip() or f'exit {result.returncode}'}"]


def _reload_udev_rules(runner: CommandRunnerPort, paths: IocostPaths) -> tuple[int, list[str]]:
    """`udevadm control --reload-rules` when udevd runs; without it there is nothing to reload."""
    if not os.path.exists(paths.udev_control):
        return 0, ["note:      udevd not running; rules are read when it starts"]
    res = runner.run(["udevadm", "control", "--reload-rules"], output=OutputMode.CAPTURE)
    return _failed("udevadm control --reload-rules", res) if res.returncode != 0 else (0, [])


def _enable_iocost_service(runner: CommandRunnerPort, paths: IocostPaths) -> tuple[int, list[str]]:
    systemd = SystemctlAdapter(runner, on_host=SystemctlHost.LOCAL)
    dr = systemd.daemon_reload_result()
    if dr.returncode != 0:
        return _failed("systemctl daemon-reload", dr)
    en = systemd.enable(IOCOST_SERVICE, now=True)
    if en.returncode != 0:
        return _failed(f"systemctl enable --now {IOCOST_SERVICE}", en)
    rc_u, lines_u = _reload_udev_rules(runner, paths)
    return rc_u, [f"enabled:   {IOCOST_SERVICE}", *lines_u]


def apply_iocost(
    paths: IocostPaths,
    *,
    runner: CommandRunnerPort | None = None,
    live_only: bool = False,
    target_device: str | None = None,
) -> tuple[int, list[str]]:
    """Enable `blk-iocost` in `io.cost.qos` and optionally install unit + udev persistence.

    Not live-only: install btrfs:iocost, write, daemon-reload, enable --now, reload the udev rules, and
    only then mark the component done; the first step that fails stops there (rc != 0, still pending)."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    snap = collect_iocost_snapshot(paths, runner=cmd_runner)
    plan = plan_iocost_apply(snap, target_device=target_device)
    if plan.error is not None:
        return (
            (0, [f"skipped:   {target_device} ({plan.error})"])
            if (live_only and target_device is not None)
            else (1, [f"ERROR: {plan.error}"])
        )

    out: list[str] = []
    component = Component.BTRFS_IOCOST.value
    if not live_only:
        rc_i, lines_i = _installer_step(cmd_runner, paths.installer_script, ["install", "--component", component])
        out.extend(lines_i)
        if rc_i != 0:
            return rc_i, out

    rc_w, lines_w = _apply_qos_writes(paths, snap, plan)
    out.extend(lines_w)
    if rc_w != 0 or live_only:
        return rc_w, out
    for step in (
        lambda: _enable_iocost_service(cmd_runner, paths),
        lambda: _installer_step(cmd_runner, paths.installer_script, ["mark", "--component", component, "done"]),
    ):
        rc_s, lines_s = step()
        out.extend(lines_s)
        if rc_s != 0:
            return rc_s, out
    return 0, out


def _remove_iocost_component(runner: CommandRunnerPort, paths: IocostPaths) -> tuple[int, list[str]]:
    """Stop the service, remove btrfs:iocost and reload systemd and udev; the first failure stops there."""
    systemd = SystemctlAdapter(runner, on_host=SystemctlHost.LOCAL)
    out: list[str] = []
    present, _, _ = probe_systemd_unit(runner, IOCOST_SERVICE)
    if present:
        dis = systemd.disable(IOCOST_SERVICE, now=True)
        if dis.returncode != 0:
            rc_d, lines_d = _failed(f"systemctl disable --now {IOCOST_SERVICE}", dis)
            return rc_d, [*lines_d, "nothing was removed"]
        out.append(f"disabled:  {IOCOST_SERVICE}")
    rc_r, lines_r = _installer_step(
        runner, paths.installer_script, ["remove", "--component", Component.BTRFS_IOCOST.value]
    )
    out.extend(lines_r)
    if rc_r != 0:
        return rc_r, out
    dr = systemd.daemon_reload_result()
    if dr.returncode != 0:
        rc_dr, lines_dr = _failed("systemctl daemon-reload", dr)
        return rc_dr, [*out, *lines_dr]
    rc_u, lines_u = _reload_udev_rules(runner, paths)
    return rc_u, [*out, *lines_u]


def revert_iocost(
    paths: IocostPaths, *, live_only: bool = False, runner: CommandRunnerPort | None = None
) -> tuple[int, list[str]]:
    """Disable CST-managed `blk-iocost` entries in `io.cost.qos` and optionally uninstall unit/rule.

    Not live-only, the service and the rule go first (so no udev event enables a disk again after its
    reset), then the managed devices are reset."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    out: list[str] = []
    if not live_only:
        rc_c, out = _remove_iocost_component(cmd_runner, paths)
        if rc_c != 0:
            return rc_c, out
    snap = collect_iocost_snapshot(paths, runner=cmd_runner)
    rc_w, lines_w = _apply_qos_writes(paths, snap, plan_iocost_revert(snap))
    return rc_w, [*out, *lines_w]


def dispatch_iocost_cli_action(
    parsed: IocostParsedArgs, env: ManagedCliEnv, *, read_only: bool
) -> tuple[int, list[str]]:
    """Resolve `IocostPaths` and invoke the requested `btrfs-iocost` subcommand."""
    paths = iocost_paths_for(env.entry_file, ctx=env.ctx, read_only=read_only, fallback_root=env.fallback_root)
    match parsed.action:
        case IocostAction.STATUS:
            return 0, format_iocost_status(collect_iocost_snapshot(paths, runner=env.runner))
        case IocostAction.PLAN:
            snap = collect_iocost_snapshot(paths, runner=env.runner)
            pl = plan_iocost_apply(snap, target_device=parsed.device)
            return (1 if pl.error is not None else 0), format_iocost_plan(snap, pl, live_only=parsed.live_only)
        case IocostAction.APPLY:
            return apply_iocost(paths, live_only=parsed.live_only, target_device=parsed.device, runner=env.runner)
        case _:
            return revert_iocost(paths, live_only=parsed.live_only, runner=env.runner)

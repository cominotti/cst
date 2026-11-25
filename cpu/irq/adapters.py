# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapters for `cst cpu irq` (`cpu/cpu-irq`)."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import os
from typing import Final

from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import ensure_dir, read_text
from cst_core.adapters.install import program_installer
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import ManagedCliEnv, host_view_path, probe_systemd_unit
from cst_core.application.install import InstallRefused, InstallService, Placement
from cst_core.domain.contracts import CPU_SETTINGS
from cst_core.domain.install import Component, GeneratedKind, InstallReportWord, Owner
from cst_core.domain.record import ROOT_RECORD
from cst_core.domain.text import parse_shell_assignments, strip_ascii_blanks
from cst_core.domain.units import ActiveState, EnablementState
from cst_core.domain.values import CpuSet, DomainValidationError
from cst_core.ports import CommandRunnerPort, ExecutionContextPort, OutputMode, WriteFailed
from cpu.game.adapters.host import read_settings
from cpu.irq.domain import (
    IRQ_DROPIN_PATH,
    IRQ_ENV_PATH,
    IRQ_SYSCONFIG_PATH,
    IRQBALANCE_SERVICE,
    IrqAction,
    IrqParsedArgs,
    IrqSnapshot,
    compute_banned_cpulist,
    format_irq_plan,
    format_irq_status,
    render_irq_dropin,
    render_irq_env,
)

# The host's rpm-ostree: read through /run/host in a toolbox, not looked up on this container's PATH.
_RPM_OSTREE_PATH: Final = "/usr/bin/rpm-ostree"
# What writing or removing a managed file through the InstallService can raise.
_INSTALL_ERRORS: Final = (InstallRefused, WriteFailed, OSError)


@dataclass(frozen=True, slots=True)
class IrqPaths:
    """Resolved host paths and helper locations for `cpu-irq`."""

    conf: str = CPU_SETTINGS
    online_cpus: str = "/sys/devices/system/cpu/online"
    env_path: str = IRQ_ENV_PATH
    sysconfig_path: str = IRQ_SYSCONFIG_PATH
    dropin_path: str = IRQ_DROPIN_PATH
    rpm_ostree_path: str = _RPM_OSTREE_PATH
    record_path: str = ROOT_RECORD
    entry_file: str = ""
    fallback_root: str = ""


def irq_paths_for(
    entry_file: str, *, ctx: ExecutionContextPort | None = None, read_only: bool = True, fallback_root: str = ""
) -> IrqPaths:
    """Build `IrqPaths`, mapping read-only reads through `/run/host` inside containers."""
    exec_ctx = HostExecutionContext() if ctx is None else ctx
    if not read_only:
        return IrqPaths(entry_file=entry_file, fallback_root=fallback_root)
    paths = [
        host_view_path(exec_ctx, p)
        for p in (
            CPU_SETTINGS,
            "/sys/devices/system/cpu/online",
            IRQ_ENV_PATH,
            IRQ_SYSCONFIG_PATH,
            IRQ_DROPIN_PATH,
            _RPM_OSTREE_PATH,
        )
    ]
    return IrqPaths(*paths, entry_file=entry_file, fallback_root=fallback_root)


def _read_banned_var(path: str) -> tuple[str | None, str | None]:
    text = read_text(path)
    if text is None:
        return None, None
    return text, parse_shell_assignments(text).values.get("IRQBALANCE_BANNED_CPULIST")


def collect_irq_snapshot(
    paths: IrqPaths,
    *,
    runner: CommandRunnerPort | None = None,
    has_rpm_ostree: bool | None = None,
) -> IrqSnapshot:
    """Collect settings, online CPUs, env/drop-in state, and systemd status for `cpu-irq`."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    settings, problems = read_settings(paths.conf)
    warnings: list[str] = [f"{paths.conf} line {p.line}: {p.text}" for p in problems]
    build_cpus_error: str | None = (
        f"{paths.conf} has syntax errors; fix them before running apply" if problems else None
    )

    online_opt = read_text(paths.online_cpus)
    online_raw = online_opt.strip() if online_opt is not None and online_opt.strip() else None

    banned = ""
    notes: tuple[str, ...] = ()
    build_set: CpuSet | None = None
    msg: str | None = None
    if not strip_ascii_blanks(settings.build_cpus):  # as CpuSet.parse reads it
        msg = f"BUILD_CPUS is empty in {paths.conf}"
    else:
        try:
            build_set = CpuSet.parse(settings.build_cpus)
        except DomainValidationError as exc:
            msg = f"invalid BUILD_CPUS ({settings.build_cpus}): {exc}"
    if msg is not None:
        build_cpus_error = build_cpus_error or msg
        warnings.append(msg)
    if online_raw is None:
        warnings.append(f"cannot read {paths.online_cpus}")
    elif build_set is not None:
        try:
            banned, notes = compute_banned_cpulist(build_set, CpuSet.parse(online_raw))
        except DomainValidationError as exc:
            warnings.append(f"invalid online cpulist: {exc}")

    env_text, env_banned = _read_banned_var(paths.env_path)
    _, sysconfig_banned = _read_banned_var(paths.sysconfig_path)
    unit_present, active, enabled = probe_systemd_unit(cmd_runner, IRQBALANCE_SERVICE)
    rpm_ostree_ok = os.path.exists(paths.rpm_ostree_path) if has_rpm_ostree is None else has_rpm_ostree

    return IrqSnapshot(
        conf_path=paths.conf,
        build_cpus_raw=settings.build_cpus,
        online_cpus_raw=online_raw,
        banned_cpulist=banned,
        compute_notes=notes,
        env_path=paths.env_path,
        env_text=env_text,
        env_banned=env_banned,
        sysconfig_path=paths.sysconfig_path,
        sysconfig_banned=sysconfig_banned,
        dropin_path=paths.dropin_path,
        dropin_text=read_text(paths.dropin_path),
        service_unit_present=unit_present,
        service_active=active,
        service_enabled=enabled,
        rpm_ostree_present=rpm_ostree_ok,
        warnings=tuple(warnings),
        build_cpus_error=build_cpus_error,
    )


def _irq_placement(path: str, dest: str, record_path: str) -> Placement:
    return Placement(
        path=path,
        dest=dest,
        record=record_path,
        component=Component.CPU_IRQ,
        src=None,
        kind=GeneratedKind.TEXT,
        owner=Owner.ROOT,
        mode=0o644,
    )


def _remove_managed_files(svc: InstallService, paths: IrqPaths) -> tuple[int, list[str], bool, bool]:
    places = [
        _irq_placement(paths.env_path, IRQ_ENV_PATH, paths.record_path),
        _irq_placement(paths.dropin_path, IRQ_DROPIN_PATH, paths.record_path),
    ]
    try:
        lines = list(svc.remove(places))
    except _INSTALL_ERRORS as exc:
        return 1, [f"ERROR: {exc}"], False, False
    with contextlib.suppress(OSError):
        os.rmdir(os.path.dirname(paths.dropin_path))
    dropin_removed = any(line.startswith(InstallReportWord.REMOVED) and paths.dropin_path in line for line in lines)
    env_removed = any(line.startswith(InstallReportWord.REMOVED) and paths.env_path in line for line in lines)
    return 0, lines, dropin_removed, env_removed


def _sync_irq_files(svc: InstallService, paths: IrqPaths, snap: IrqSnapshot) -> tuple[int, list[str], bool, bool]:
    if not snap.banned_cpulist:
        return _remove_managed_files(svc, paths)
    out: list[str] = []
    dropin_changed = False
    env_changed = False
    as_root = os.geteuid() == 0
    try:
        ensure_dir(os.path.dirname(paths.env_path), 0o755)
        for path, dest, text, is_env in (
            (paths.env_path, IRQ_ENV_PATH, render_irq_env(snap.banned_cpulist), True),
            (paths.dropin_path, IRQ_DROPIN_PATH, render_irq_dropin(), False),
        ):
            place = _irq_placement(path, dest, paths.record_path)
            written = svc.put(place, text.encode("utf-8"), as_root=as_root)
            out.append((InstallReportWord.WRITTEN if written else InstallReportWord.UNCHANGED).line(path))
            env_changed = env_changed or (is_env and written)
            dropin_changed = dropin_changed or (not is_env and written)
    except _INSTALL_ERRORS as exc:
        return 1, [*out, f"ERROR: {exc}"], dropin_changed, env_changed
    return 0, out, dropin_changed, env_changed


def _ensure_irq_package(
    runner: CommandRunnerPort,
    snap: IrqSnapshot,
    *,
    no_pkg: bool,
) -> tuple[int, list[str], tuple[bool, str, str]]:
    if snap.service_unit_present:
        return 0, [], (True, snap.service_active, snap.service_enabled)
    if no_pkg:
        return (
            0,
            ["skipped:   rpm-ostree install irqbalance (--no-pkg)"],
            (False, snap.service_active, snap.service_enabled),
        )
    if not snap.rpm_ostree_present:
        msg = (
            "ERROR: irqbalance.service is not installed and rpm-ostree was not found; "
            "install irqbalance and re-run (or pass --no-pkg to write config only)"
        )
        return 1, [msg], (False, snap.service_active, snap.service_enabled)
    pkg_res = runner.run(
        ["rpm-ostree", "install", "--idempotent", "--apply-live", "irqbalance"],
        output=OutputMode.CAPTURE,
    )
    out = [line for line in pkg_res.stdout.splitlines() if line.strip()]
    if pkg_res.returncode != 0:
        if pkg_res.stderr.strip():
            out.append(pkg_res.stderr.strip())
        return pkg_res.returncode, out, (False, snap.service_active, snap.service_enabled)
    out.append("installed: irqbalance RPM (rpm-ostree)")
    return 0, out, probe_systemd_unit(runner, IRQBALANCE_SERVICE)


def _sync_active_irq_service(
    systemd: SystemctlAdapter,
    unit_state: tuple[bool, str, str],
    *,
    banned_cpulist: str,
    changed: bool,
) -> tuple[int, list[str]]:
    unit_present, active_state, enabled_state = unit_state
    if not unit_present:
        return 0, []
    out: list[str] = []
    if enabled_state != EnablementState.ENABLED:
        en = systemd.enable(IRQBALANCE_SERVICE)
        if en.returncode != 0:
            return en.returncode, [en.stderr.strip() or f"systemctl enable {IRQBALANCE_SERVICE} failed"]
        out.append(f"enabled:   {IRQBALANCE_SERVICE}")
    if active_state != ActiveState.ACTIVE or changed:
        rs = systemd.restart(IRQBALANCE_SERVICE)
        if rs.returncode != 0:
            return rs.returncode, [*out, rs.stderr.strip() or f"systemctl restart {IRQBALANCE_SERVICE} failed"]
        out.append(f"restarted: {IRQBALANCE_SERVICE} (ban {banned_cpulist})")
    else:
        out.append(f"unchanged: {IRQBALANCE_SERVICE} enabled and active")
    return 0, out


def _finish_irq_apply(
    systemd: SystemctlAdapter,
    snap: IrqSnapshot,
    unit_state: tuple[bool, str, str],
    *,
    changed: bool,
) -> tuple[int, list[str]]:
    if not snap.banned_cpulist:
        if snap.service_unit_present and snap.service_active == ActiveState.ACTIVE and changed:
            rr = systemd.restart(IRQBALANCE_SERVICE)
            if rr.returncode != 0:
                return rr.returncode, [rr.stderr.strip() or f"systemctl restart {IRQBALANCE_SERVICE} failed"]
            return 0, [f"restarted: {IRQBALANCE_SERVICE} (no CST ban-list)"]
        return 0, []
    return _sync_active_irq_service(systemd, unit_state, banned_cpulist=snap.banned_cpulist, changed=changed)


def _apply_irq_changes(
    cmd_runner: CommandRunnerPort, svc: InstallService, paths: IrqPaths, snap: IrqSnapshot, *, no_pkg: bool
) -> tuple[int, list[str]]:
    """The package (with a ban-list), the files, the service, then cpu:irq done; stops at the first failure."""
    out: list[str] = []
    unit_state = (snap.service_unit_present, snap.service_active, snap.service_enabled)
    if snap.banned_cpulist:
        rc_p, pkg_lines, unit_state = _ensure_irq_package(cmd_runner, snap, no_pkg=no_pkg)
        out.extend(pkg_lines)
        if rc_p != 0:
            return rc_p, out

    rc_f, file_lines, dropin_changed, env_changed = _sync_irq_files(svc, paths, snap)
    out.extend(file_lines)
    if rc_f != 0:
        return rc_f, out

    systemd = SystemctlAdapter(cmd_runner, on_host=SystemctlHost.LOCAL)
    if dropin_changed:
        dr = systemd.daemon_reload_result()
        if dr.returncode != 0:
            return dr.returncode, [*out, dr.stderr.strip() or "systemctl daemon-reload failed"]

    rc_end, tail_lines = _finish_irq_apply(systemd, snap, unit_state, changed=(env_changed or dropin_changed))
    out.extend(tail_lines)
    # cpu:irq is done only once irqbalance runs with the ban-list: a failed restart, or no irqbalance.service
    # (--no-pkg, or a package rpm-ostree installs only for the next boot), leaves it pending.
    if rc_end == 0 and snap.banned_cpulist and not unit_state[0]:
        out.append(f"pending:   cpu:irq ({IRQBALANCE_SERVICE} is not there yet: run apply again once it is)")
    elif rc_end == 0 and snap.banned_cpulist:
        try:
            svc.mark(paths.record_path, Component.CPU_IRQ)
        except _INSTALL_ERRORS as exc:
            return 1, [*out, f"ERROR: {exc}"]
    return rc_end, out


def apply_irq_policy(
    paths: IrqPaths,
    *,
    no_pkg: bool = False,
    runner: CommandRunnerPort | None = None,
    has_rpm_ostree: bool | None = None,
    installer: InstallService | None = None,
) -> tuple[int, list[str]]:
    """Write adaptive `irqbalance` config/drop-in, optionally layer RPM, and start/restart service."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    svc = program_installer(paths.entry_file, paths.fallback_root) if installer is None else installer
    snap = collect_irq_snapshot(paths, runner=cmd_runner, has_rpm_ostree=has_rpm_ostree)
    out: list[str] = [f"WARN: {w}" for w in snap.warnings]
    if snap.build_cpus_error is not None:
        # Nothing written, removed or restarted: an empty ban-list here would remove the files.
        return 1, [*out, f"ERROR: {snap.build_cpus_error}"]
    if snap.online_cpus_raw is None:
        return 1, out
    out.extend(f"note:      {n}" for n in snap.compute_notes)
    rc, lines = _apply_irq_changes(cmd_runner, svc, paths, snap, no_pkg=no_pkg)
    return rc, [*out, *lines]


def revert_irq_policy(
    paths: IrqPaths,
    *,
    runner: CommandRunnerPort | None = None,
    installer: InstallService | None = None,
) -> tuple[int, list[str]]:
    """Remove CST's irqbalance env file and drop-in; restart service only if it is active."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    svc = program_installer(paths.entry_file, paths.fallback_root) if installer is None else installer
    snap = collect_irq_snapshot(paths, runner=cmd_runner)
    rc_r, out, dropin_removed, env_removed = _remove_managed_files(svc, paths)
    if rc_r != 0:
        return rc_r, out
    if not dropin_removed and not env_removed:
        return 0, [*out, "unchanged: no CST irqbalance files present"]

    systemd = SystemctlAdapter(cmd_runner, on_host=SystemctlHost.LOCAL)
    if dropin_removed:
        dr = systemd.daemon_reload_result()
        if dr.returncode != 0:
            return dr.returncode, [*out, dr.stderr.strip() or "systemctl daemon-reload failed"]
    if snap.service_unit_present and snap.service_active == ActiveState.ACTIVE:
        rr = systemd.restart(IRQBALANCE_SERVICE)
        if rr.returncode != 0:
            return rr.returncode, [*out, rr.stderr.strip() or f"systemctl restart {IRQBALANCE_SERVICE} failed"]
        out.append(f"restarted: {IRQBALANCE_SERVICE} (CST ban-list removed)")
    if snap.service_unit_present:
        out.append("note:      irqbalance package stays installed; revert removed CST ban config only")
    return 0, out


def dispatch_irq_cli_action(parsed: IrqParsedArgs, env: ManagedCliEnv, *, read_only: bool) -> tuple[int, list[str]]:
    """Resolve `IrqPaths` and run the validated `cpu-irq` command."""
    paths = irq_paths_for(env.entry_file, ctx=env.ctx, read_only=read_only, fallback_root=env.fallback_root)
    if parsed.action == IrqAction.STATUS:
        return 0, format_irq_status(collect_irq_snapshot(paths, runner=env.runner))
    if parsed.action == IrqAction.PLAN:
        return 0, format_irq_plan(collect_irq_snapshot(paths, runner=env.runner), no_pkg=parsed.no_pkg)
    if parsed.action == IrqAction.APPLY:
        return apply_irq_policy(paths, no_pkg=parsed.no_pkg, runner=env.runner)
    return revert_irq_policy(paths, runner=env.runner)

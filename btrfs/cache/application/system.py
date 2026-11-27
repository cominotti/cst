# SPDX-License-Identifier: MPL-2.0
"""Application layer for `cst btrfs cache system <status|plan|apply|revert>`."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from btrfs.cache.application.fstab_ops import commit_fstab, format_undo_error, probe_base_mount, restore_fstab
from btrfs.cache.domain import (
    CACHE_BASE,
    BaseMountInfo,
    LiveMount,
    mount_holding,
    mount_stacks,
    mounts_by_target,
    parse_all_mounts,
    unmanaged_fstab_targets,
)
from btrfs.cache.domain.format import cache_seam_lines
from btrfs.cache.domain.system import (
    SystemBindProbe,
    SystemCacheSnapshot,
    SystemPlanOptions,
    format_system_plan,
    format_system_status,
    is_system_bind_from,
    parse_system_fstab_entries,
    plan_system_apply_fstab,
    plan_system_revert_fstab,
    selected_system_specs,
    should_mount_live,
    validate_system_apply,
    validate_system_revert,
)
from btrfs.cache.domain.system_spec import (
    SYSTEM_CACHE_ROOT,
    SystemAction,
    SystemArgsResult,
    SystemBindId,
    SystemBindSpec,
)
from btrfs.cache.domain.systemd_view import catch_up_words, fstab_lines_unread
from btrfs.cache.ports import CacheSystemBindPort, CacheSystemdPort, FstabPort, MountTablePort
from btrfs.compress.domain import FSTAB_PATH
from cst_core.domain.values import ExitAndLines

_BASE_DIR_MODE: Final = 0o755


@dataclass(frozen=True, slots=True)
class SystemEnv:
    """Port bundle and seam root for `cst btrfs cache system` use cases."""

    binds: CacheSystemBindPort | None
    fstab: FstabPort
    mounts: MountTablePort
    systemd: CacheSystemdPort
    seam_root: str = ""


@dataclass(frozen=True, slots=True)
class _SystemProbeCtx:
    base_info: BaseMountInfo
    fstab_targets: frozenset[str]
    unmanaged: frozenset[str]
    stacks: dict[str, tuple[LiveMount, ...]]


def _probe_system_item(
    spec: SystemBindSpec,
    ctx: _SystemProbeCtx,
    binds: CacheSystemBindPort,
) -> SystemBindProbe:
    stack = ctx.stacks.get(spec.target, ())
    live = stack[-1] if stack else None
    paths = binds.probe_paths(spec)
    base = ctx.base_info
    from_src = live is not None and is_system_bind_from(live, base, spec.rel_dir)
    mounted_other = live is not None and base.mounted and not from_src
    svc_active = binds.service_is_active(spec.service) if spec.service else None
    return SystemBindProbe(
        spec=spec,
        in_fstab=spec.target in ctx.fstab_targets,
        mounted_from_source=from_src,
        mounted_other=mounted_other,
        target_is_symlink=paths.target_is_link,
        target_exists=paths.target_exists,
        target_is_dir=paths.target_is_dir,
        target_nonempty=paths.target_nonempty,
        source_is_symlink=paths.source_is_link,
        source_exists=paths.source_exists,
        source_is_dir=paths.source_is_dir,
        source_nonempty=paths.source_nonempty,
        fstab_unmanaged=spec.target in ctx.unmanaged,
        source_unchecked=live is not None and not base.mounted,
        bind_below=mounted_other and any(is_system_bind_from(b, base, spec.rel_dir) for b in stack[:-1]),
        service_active=svc_active,
    )


def probe_system_snapshot(parsed: SystemArgsResult, env: SystemEnv) -> SystemCacheSnapshot | tuple[int, list[str]]:
    """Collect read-only snapshot of `/var/mnt/cache/system/*` state and mounts."""
    if env.binds is None:
        return 1, ["ERROR: system cache bind adapter is unavailable"]
    fstab_error: str | None = None
    try:
        fstab_text = env.fstab.read()
    except OSError as exc:
        fstab_text, fstab_error = None, f"cannot read {FSTAB_PATH}: {exc.strerror or exc}"
    mounts, mounts_error = env.mounts.read()
    live_map = mounts_by_target(mounts)
    fstab_all = {e.target: e for e in parse_all_mounts(fstab_text or "")}
    base_info = probe_base_mount(CACHE_BASE, live_map, fstab_all)
    var_mount = mount_holding(mounts, "/var")
    managed = parse_system_fstab_entries(fstab_text or "")
    ctx = _SystemProbeCtx(
        base_info=base_info,
        fstab_targets=frozenset(e.target for e in managed),
        unmanaged=unmanaged_fstab_targets(fstab_text or "", known_lines=frozenset(e.raw_line for e in managed)),
        stacks=mount_stacks(mounts),
    )
    probes = tuple(_probe_system_item(s, ctx, env.binds) for s in selected_system_specs(parsed.only))
    behind = (
        fstab_lines_unread(env.systemd.mount_units(), ctx.fstab_targets)
        if (managed or base_info.mounted or base_info.in_fstab)
        else False
    )
    return SystemCacheSnapshot(
        cache_base=base_info,
        var_uuid=var_mount.uuid if var_mount is not None else "",
        probes=probes,
        managed_entries=managed,
        fstab_text=fstab_text,
        mounts_error=mounts_error,
        fstab_error=fstab_error,
        fstab_behind=behind,
    )


def _restart_services(binds: CacheSystemBindPort, units: Sequence[str]) -> list[str]:
    return [err for unit in units if (err := binds.start_service(unit)) is not None]


def _stop_services(
    binds: CacheSystemBindPort,
    probes: Sequence[SystemBindProbe],
    *,
    only_mounted: bool,
) -> tuple[list[str], str | None, list[str]]:
    stopped: list[str] = []
    for probe in probes:
        s = probe.spec
        state_matches = probe.mounted_from_source if only_mounted else not probe.mounted_from_source
        if not s.service or not state_matches or probe.service_active is not True:
            continue
        err = binds.stop_service(s.service)
        if err is not None:
            return [], err, _restart_services(binds, stopped)
        stopped.append(s.service)
    return stopped, None, []


def _undo_system_apply(
    binds: CacheSystemBindPort,
    mounted: Sequence[SystemBindSpec],
    copied: Sequence[SystemBindSpec],
    stopped_services: Sequence[str],
) -> list[str]:
    errors = [err for spec in reversed(mounted) if (err := binds.unbind(spec.target)) is not None]
    for spec in copied:
        binds.clear_dir_contents(spec.source)
    errors.extend(_restart_services(binds, stopped_services))
    return errors


def _prepare_and_copy_system(
    binds: CacheSystemBindPort,
    probes: Sequence[SystemBindProbe],
) -> tuple[list[SystemBindSpec], str | None]:
    copied: list[SystemBindSpec] = []
    try:
        binds.ensure_root_dir(SYSTEM_CACHE_ROOT, _BASE_DIR_MODE)
    except OSError as exc:
        return [], f"cannot create {SYSTEM_CACHE_ROOT}: {exc.strerror or exc}"

    for probe in probes:
        spec = probe.spec
        try:
            if not probe.target_exists:
                binds.ensure_root_dir(spec.target, spec.mode)
            binds.ensure_root_dir(spec.source, spec.mode, reference_selinux=spec.target)
        except OSError as exc:
            return copied, f"cannot prepare {spec.source} / {spec.target}: {exc.strerror or exc}"
        if (
            not probe.mounted_from_source
            and spec.mount_live_by_default
            and probe.target_nonempty
            and not probe.source_nonempty
        ):
            copy_err = binds.copy_tree(spec.target, spec.source)
            if copy_err is not None:
                binds.clear_dir_contents(spec.source)
                return copied, copy_err
            try:
                # `cp -a src/. dst` overwrites `dst` directory mode/attrs with `src`'s; restore target mode + SELinux.
                binds.ensure_root_dir(spec.source, spec.mode, reference_selinux=spec.target)
            except OSError as exc:
                binds.clear_dir_contents(spec.source)
                return copied, f"cannot finalize {spec.source}: {exc.strerror or exc}"
            copied.append(spec)
    return copied, None


def _commit_system_fstab(
    snap: SystemCacheSnapshot,
    env: SystemEnv,
    mounted: Sequence[SystemBindSpec],
    copied: Sequence[SystemBindSpec],
    stopped: Sequence[str],
) -> tuple[bool, list[str], str | None]:
    assert env.binds is not None
    cur_fstab = snap.fstab_text or ""
    new_fstab = plan_system_apply_fstab(cur_fstab, [p.spec for p in snap.probes])
    if new_fstab != cur_fstab:
        failed = commit_fstab(snap.fstab_text, new_fstab, env.fstab, env.systemd)
        if failed is None:
            return True, [], None
        undo_errs = [*_undo_system_apply(env.binds, mounted, copied, stopped), *failed.undo_errors]
        return True, [], format_undo_error(failed.cause, undo_errs)
    behind = fstab_lines_unread(env.systemd.mount_units(), {p.spec.target for p in snap.probes})
    if behind is False:
        return False, [], None
    cause, reloaded = catch_up_words(behind=behind, reload_error=env.systemd.daemon_reload())
    if cause is None:
        return False, reloaded, None
    undo_errs = _undo_system_apply(env.binds, mounted, copied, stopped)
    return False, [], format_undo_error(cause, undo_errs)


@dataclass(frozen=True, slots=True)
class _ApplySummary:
    copied: Sequence[SystemBindSpec]
    mounted: Sequence[SystemBindSpec]
    reloaded: Sequence[str]
    wrote_fstab: bool
    mount_var_tmp_now: bool


def _format_system_apply_lines(snap: SystemCacheSnapshot, summary: _ApplySummary) -> list[str]:
    out = [f"Copied existing data from {spec.target} -> {spec.source}." for spec in summary.copied]
    out.extend(f"Mounted {spec.source} -> {spec.target}." for spec in summary.mounted)
    out.extend(
        f"Staged {probe.spec.target} in {FSTAB_PATH} (activates on next boot; currently non-empty)."
        for probe in snap.probes
        if (
            not should_mount_live(probe, mount_var_tmp_now=summary.mount_var_tmp_now)
            and not probe.mounted_from_source
            and not probe.in_fstab
        )
    )
    out.extend(summary.reloaded)
    if summary.wrote_fstab:
        out.append(f"Updated {FSTAB_PATH} and reloaded systemd.")
    elif not summary.mounted and not summary.copied and not summary.reloaded:
        out.append("System cache bind mounts already configured; nothing to do.")
    return out


def _bind_live_targets(
    binds: CacheSystemBindPort, snap: SystemCacheSnapshot, *, mount_var_tmp_now: bool
) -> tuple[list[SystemBindSpec], str | None]:
    mounted: list[SystemBindSpec] = []
    for probe in snap.probes:
        if should_mount_live(probe, mount_var_tmp_now=mount_var_tmp_now):
            err = binds.bind(probe.spec.source, probe.spec.target)
            if err is not None:
                return mounted, err
            mounted.append(probe.spec)
    return mounted, None


def _apply_system(snap: SystemCacheSnapshot, parsed: SystemArgsResult, env: SystemEnv) -> ExitAndLines:
    assert env.binds is not None
    binds = env.binds
    lines = cache_seam_lines(env.seam_root)
    blockers = validate_system_apply(
        snap, allow_nonempty=parsed.allow_nonempty, mount_var_tmp_now=parsed.mount_var_tmp_now
    )
    if blockers:
        return 1, [*lines, *(f"ERROR: {msg}" for msg in blockers)]

    stopped, stop_err, stop_undo = _stop_services(binds, snap.probes, only_mounted=False)
    if stop_err is not None:
        return 1, [*lines, format_undo_error(stop_err, stop_undo)]

    copied, copy_err = _prepare_and_copy_system(binds, snap.probes)
    if copy_err is not None:
        return 1, [*lines, format_undo_error(copy_err, _undo_system_apply(binds, (), copied, stopped))]

    mounted, bind_err = _bind_live_targets(binds, snap, mount_var_tmp_now=parsed.mount_var_tmp_now)
    if bind_err is not None:
        return 1, [*lines, format_undo_error(bind_err, _undo_system_apply(binds, mounted, copied, stopped))]

    wrote_fstab, reloaded, fstab_err = _commit_system_fstab(snap, env, mounted, copied, stopped)
    if fstab_err is not None:
        return 1, [*lines, fstab_err]

    restart_errs = _restart_services(binds, stopped)
    summary = _ApplySummary(copied, mounted, reloaded, wrote_fstab, parsed.mount_var_tmp_now)
    return (
        (1, [*lines, *(f"ERROR: {err}" for err in restart_errs)])
        if restart_errs
        else (0, [*lines, *_format_system_apply_lines(snap, summary)])
    )


@dataclass(frozen=True, slots=True)
class _UnbindResult:
    stopped: list[str]
    unmounted: list[SystemBindSpec]
    notes: list[str]
    error: str | None = None
    extra_undo: tuple[str, ...] = ()


def _unbind_system_mounts(binds: CacheSystemBindPort, probes: Sequence[SystemBindProbe]) -> _UnbindResult:
    stopped, stop_err, stop_undo = _stop_services(binds, probes, only_mounted=True)
    if stop_err is not None:
        return _UnbindResult([], [], [], stop_err, tuple(stop_undo))

    unmounted: list[SystemBindSpec] = []
    notes: list[str] = []
    for probe in probes:
        if not probe.mounted_from_source:
            continue
        spec = probe.spec
        err = binds.unbind(spec.target)
        if err is not None:
            if spec.bind_id == SystemBindId.VAR_TMP:
                fstab_note = f"removed from {FSTAB_PATH}" if probe.in_fstab else f"already absent from {FSTAB_PATH}"
                notes.append(
                    f"NOTE: {spec.target} could not be unmounted while busy ({err}); "
                    f"{fstab_note} and stays unmounted on next boot."
                )
                continue
            return _UnbindResult(stopped, unmounted, notes, err)
        unmounted.append(spec)
    return _UnbindResult(stopped, unmounted, notes)


def _revert_system(snap: SystemCacheSnapshot, env: SystemEnv) -> ExitAndLines:
    assert env.binds is not None
    binds = env.binds
    lines = cache_seam_lines(env.seam_root)
    blockers = validate_system_revert(snap)
    if blockers:
        return 1, [*lines, *(f"ERROR: {msg}" for msg in blockers)]

    cur_fstab = snap.fstab_text or ""
    new_fstab = plan_system_revert_fstab(cur_fstab, {p.spec.bind_id for p in snap.probes})
    if new_fstab != cur_fstab:
        failed = commit_fstab(snap.fstab_text, new_fstab, env.fstab, env.systemd)
        if failed is not None:
            return 1, [*lines, format_undo_error(failed.cause, failed.undo_errors)]
    elif snap.fstab_behind is not False:
        cause, reloaded = catch_up_words(behind=snap.fstab_behind, reload_error=env.systemd.daemon_reload())
        if cause is not None:
            return 1, [*lines, f"ERROR: {cause}"]
        lines.extend(reloaded)

    res = _unbind_system_mounts(binds, snap.probes)
    if res.error is not None:
        undo_errs = [e for spec in reversed(res.unmounted) if (e := binds.bind(spec.source, spec.target)) is not None]
        if new_fstab != cur_fstab:
            undo_errs.extend(restore_fstab(snap.fstab_text, env.fstab, env.systemd))
        undo_errs.extend(res.extra_undo)
        undo_errs.extend(_restart_services(binds, res.stopped))
        return 1, [*lines, format_undo_error(res.error, undo_errs)]

    restart_errs = _restart_services(binds, res.stopped)
    if not restart_errs:
        lines.extend(f"Unmounted {spec.target}." for spec in res.unmounted)
        lines.extend(res.notes)
        if new_fstab != cur_fstab:
            lines.append(f"Updated {FSTAB_PATH} and reloaded systemd.")
        if res.unmounted or new_fstab != cur_fstab or res.notes:
            lines.append(f"Data in {SYSTEM_CACHE_ROOT} kept untouched.")
        else:
            lines.append("No system cache bind mounts configured; nothing to do.")
    return (1, [*lines, *(f"ERROR: {e}" for e in restart_errs)]) if restart_errs else (0, lines)


def run_system_action(parsed: SystemArgsResult, env: SystemEnv, prog: str) -> ExitAndLines:
    """Dispatch `cst btrfs cache system <status|plan|apply|revert>`."""
    probed = probe_system_snapshot(parsed, env)
    if not isinstance(probed, SystemCacheSnapshot):
        return probed
    action = parsed.action
    if action == SystemAction.PLAN:
        opts = SystemPlanOptions(
            allow_nonempty=parsed.allow_nonempty,
            mount_var_tmp_now=parsed.mount_var_tmp_now,
            seam_root=env.seam_root,
        )
        res: ExitAndLines = (0, format_system_plan(replace(probed), prog, options=opts))
    elif action == SystemAction.APPLY:
        res = _apply_system(probed, parsed, env)
    elif action == SystemAction.REVERT:
        res = _revert_system(probed, env)
    else:
        res = (0, format_system_status(probed, seam_root=env.seam_root))
    return res

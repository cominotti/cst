# SPDX-License-Identifier: MPL-2.0
"""Host and toolbox adapters for `cst btrfs commit-interval` (`btrfs/btrfs-commit-interval`)."""

from __future__ import annotations

from collections.abc import Callable
import os
from typing import Final

from btrfs.commit.domain import (
    DEFAULT_TARGET_COMMIT_S,
    KERNEL_DEFAULT_COMMIT_S,
    CommitAction,
    CommitParsedArgs,
    CommitPlan,
    assess_commit_coherence,
    booted_commit_diff,
    build_commit_plan,
    effective_commit_seconds,
    format_commit_label,
    has_explicit_commit,
)
from btrfs.compress.adapters import CompressAdapterConfig, LinuxCompressHostAdapter
from btrfs.compress.domain import (
    CompressSnapshot,
    FSTAB_PATH,
    PROC_MOUNTS_PATH,
    TOP_MOUNT_UNIT_PATH,
    extract_rootflags_tokens,
    parse_proc_mounts,
    pick_root_mounts,
)
from btrfs.compress.ports import CompressHostPort
from btrfs.compress.presenter import format_layer_plan_lines, format_layer_status_lines
from cst_core.adapters.runtime import ManagedCliEnv
from cst_core.domain.errors import Refused
from cst_core.ports import CommandFailed, WriteFailed

SEAM_COMMIT_PROC: Final = "CST_BTRFS_COMMIT_PROC"
SEAM_COMMIT_ROOT: Final = "CST_BTRFS_COMMIT_ROOT"


def _host_for_env(env: ManagedCliEnv) -> tuple[CompressHostPort, str, dict[str, str]]:
    env_map = dict(os.environ if env.env is None else env.env)
    seam_proc = env_map.get(SEAM_COMMIT_PROC, "")
    host_root = env_map.get(SEAM_COMMIT_ROOT, "")
    cfg = CompressAdapterConfig(
        entry_file=env.entry_file,
        fallback_root=env.fallback_root,
        proc_dir=seam_proc or "/proc",
        host_root=host_root,
    )
    return LinuxCompressHostAdapter(cfg, ctx=env.ctx, runner=env.runner), seam_proc, env_map


def read_commit_snapshot(host: CompressHostPort) -> tuple[CompressSnapshot, bool, str]:
    """Read current state across fstab, rpm-ostree kargs, live mounts, and top-mount."""
    is_ostree = host.is_ostree()
    kargs_ok, kargs_text, kargs_err = host.read_kargs() if is_ostree else (True, "", "")
    snap = CompressSnapshot(
        is_ostree=is_ostree,
        root_uuid=host.root_uuid(),
        fstab_text=host.read_fstab(),
        kargs_text=kargs_text if (is_ostree and kargs_ok) else None,
        proc_mounts_text=host.read_proc_mounts(),
        top_mount=host.read_top_mount(),
    )
    return snap, (True if not is_ostree else kargs_ok), kargs_err


def _unwind_rollback(undo_steps: list[Callable[[], str | None]]) -> str:
    errors: list[str] = [err for step in reversed(undo_steps) if (err := step()) is not None]
    return f"ROLLBACK INCOMPLETE ({'; '.join(errors)})" if errors else "previous configuration restored."


def _undo_fstab(host: CompressHostPort, prev_fstab: str) -> str | None:
    try:
        host.write_fstab(prev_fstab)
    except (OSError, CommandFailed) as exc:
        return f"fstab: {exc}"
    return None


def _undo_kargs(host: CompressHostPort, edits: tuple[tuple[str, str], ...]) -> str | None:
    ok, err = host.update_kargs([(new, old) for old, new in edits])
    return None if ok else f"kargs: {err}"


def _undo_remount(host: CompressHostPort, target: str, prev_seconds: int) -> str | None:
    ok, err = host.remount(target, f"commit={prev_seconds}")
    return None if ok else f"remount: {err}"


def _step_fstab(
    host: CompressHostPort,
    snap: CompressSnapshot,
    plan: CommitPlan,
    msgs: list[str],
    undo: list[Callable[[], str | None]],
) -> str | None:
    if plan.new_fstab_text is None or snap.fstab_text is None:
        return None
    prev_fstab = snap.fstab_text
    try:
        host.write_fstab(plan.new_fstab_text)
    except (OSError, CommandFailed) as exc:
        return f"Failed to update {FSTAB_PATH}: {exc}"
    undo.append(lambda: _undo_fstab(host, prev_fstab))
    msgs.append(f"Updated {FSTAB_PATH} (commit={plan.target_seconds}s) and reloaded systemd.")
    return None


def _step_kargs(
    host: CompressHostPort,
    plan: CommitPlan,
    msgs: list[str],
    undo: list[Callable[[], str | None]],
) -> str | None:
    if not plan.kargs_edits:
        return None
    ok, kerr = host.update_kargs(plan.kargs_edits)
    if not ok:
        return f"Failed to update rpm-ostree kargs: {kerr}"
    edits = plan.kargs_edits
    undo.append(lambda: _undo_kargs(host, edits))
    msgs.append(f"Updated rpm-ostree kargs (commit={plan.target_seconds}s for next boot).")
    return None


def _step_remount(
    host: CompressHostPort,
    snap: CompressSnapshot,
    plan: CommitPlan,
    msgs: list[str],
    undo: list[Callable[[], str | None]],
) -> str | None:
    if plan.remount_target is None:
        return None
    prior_roots = pick_root_mounts(parse_proc_mounts(snap.proc_mounts_text or ""))
    prior_entry = next((m for m in prior_roots if m.target == plan.remount_target), None)
    prev_sec = effective_commit_seconds(prior_entry.options) if prior_entry is not None else KERNEL_DEFAULT_COMMIT_S
    opt = f"commit={plan.target_seconds}"
    target = plan.remount_target
    ok, rerr = host.remount(target, opt)
    if not ok:
        return f"Failed to remount {target} with {opt}: {rerr}"
    undo.append(lambda: _undo_remount(host, target, prev_sec))
    after_roots = pick_root_mounts(parse_proc_mounts(host.read_proc_mounts() or ""))
    if not after_roots or any(effective_commit_seconds(m.options) != plan.target_seconds for m in after_roots):
        return f"Post-remount verification failed in {PROC_MOUNTS_PATH}: expected commit={plan.target_seconds}s"
    msgs.append(f"Remounted {target} with {opt} (verified in {PROC_MOUNTS_PATH}).")
    return None


def _step_top_mount(
    host: CompressHostPort,
    snap: CompressSnapshot,
    plan: CommitPlan,
    msgs: list[str],
) -> str | None:
    if plan.new_top_mount_options is None or snap.top_mount is None:
        return None
    uuid = snap.top_mount.uuid or snap.root_uuid
    if not uuid:
        return f"Cannot update {TOP_MOUNT_UNIT_PATH}: root filesystem UUID unknown"
    try:
        host.update_top_mount(uuid, plan.new_top_mount_options)
    except (OSError, Refused, CommandFailed, WriteFailed, ValueError) as exc:
        return f"Failed to update {TOP_MOUNT_UNIT_PATH}: {exc}"
    msgs.append(f"Updated {TOP_MOUNT_UNIT_PATH} and reloaded systemd.")
    return None


def execute_commit_change(
    host: CompressHostPort,
    parsed: CommitParsedArgs,
    *,
    prog: str = "cst btrfs commit-interval",
    seam_proc: str = "",
) -> tuple[int, list[str]]:
    """Run transactional `apply` or `revert` against `host` (`CompressHostPort`)."""
    snap, kargs_ok, kargs_err = read_commit_snapshot(host)
    if not kargs_ok:
        return 1, [f"ERROR: cannot inspect rpm-ostree kargs: {kargs_err}"]
    plan = build_commit_plan(snap, target_seconds=parsed.seconds, revert=parsed.revert)
    lines = format_commit_plan_lines(plan, prog, dry_run=False, seam_proc=seam_proc)
    if not plan.has_changes:
        return 0, lines
    if not parsed.yes:
        try:
            confirmed = host.confirm(f"Apply commit={plan.target_seconds}s across active layers?")
        except Refused as exc:
            return 1, [*lines, f"ERROR: {exc} (pass --yes to apply non-interactively)."]
        if not confirmed:
            return 1, [*lines, "Aborted; nothing changed."]

    undo: list[Callable[[], str | None]] = []
    msgs: list[str] = []
    for err in (
        _step_fstab(host, snap, plan, msgs, undo),
        _step_kargs(host, plan, msgs, undo),
        _step_remount(host, snap, plan, msgs, undo),
        _step_top_mount(host, snap, plan, msgs),
    ):
        if err is not None:
            rollback = _unwind_rollback(undo)
            return 1, [*lines, *msgs, f"ERROR: {err}; {rollback}"]
    return 0, [*lines, *msgs]


def format_commit_status(
    snap: CompressSnapshot,
    *,
    kargs_ok: bool,
    kargs_error: str,
    proc_cmdline: str | None,
    seam_proc: str = "",
) -> list[str]:
    """Render status lines for `cst btrfs commit-interval status`."""
    out: list[str] = []
    if seam_proc:
        out.append(f"test seam (CST_BTRFS_COMMIT_PROC): /proc in {seam_proc}, not the kernel's")
    live_roots = pick_root_mounts(parse_proc_mounts(snap.proc_mounts_text or ""))
    live_entries = [
        (
            entry.target,
            format_commit_label(effective_commit_seconds(entry.options), explicit=has_explicit_commit(entry.options)),
        )
        for entry in live_roots
    ]
    coherence = assess_commit_coherence(snap)
    plan_view = build_commit_plan(snap, target_seconds=coherence.seconds or DEFAULT_TARGET_COMMIT_S)
    out.extend(
        format_layer_status_lines(
            "Root btrfs commit interval:",
            live_entries,
            plan_view.layers,
            kargs_error="" if kargs_ok else kargs_error,
            booted_diff=booted_commit_diff(snap, proc_cmdline, kargs_ok=kargs_ok),
        )
    )
    if snap.is_ostree and not kargs_ok:
        out.append(f"  overall         incomplete (rpm-ostree kargs error: {kargs_error})")
    elif snap.is_ostree and snap.kargs_text is not None and not extract_rootflags_tokens(snap.kargs_text):
        out.append("  overall         incomplete (no rootflags= in rpm-ostree kargs)")
    else:
        out.append(f"  overall         {coherence.summary}")
    return out


def format_commit_plan_lines(plan: CommitPlan, prog: str, *, dry_run: bool, seam_proc: str = "") -> list[str]:
    """Render plan lines for `plan`, `apply`, and `revert`."""
    out: list[str] = []
    if seam_proc:
        out.append(f"test seam (CST_BTRFS_COMMIT_PROC): /proc in {seam_proc}, not the kernel's")
    out.extend(format_layer_plan_lines(f"Target root btrfs commit interval: {plan.target_seconds}s", plan.layers))
    if not plan.has_changes:
        out.append(f"Already at {plan.target_seconds}s across all active layers; nothing to do.")
        return out
    if dry_run:
        hint = "revert" if plan.revert else f"apply {plan.target_seconds}"
        out.append(f"Dry run, nothing changed. Run `{prog} {hint}` to apply.")
    return out


def dispatch_commit_cli_action(
    parsed: CommitParsedArgs, env: ManagedCliEnv, *, read_only: bool
) -> tuple[int, list[str]]:
    """Resolve host adapter and execute the requested `commit-interval` subcommand."""
    del read_only
    host, seam_proc, env_map = _host_for_env(env)
    prog = env_map.get("CST_PROG") or "cst btrfs commit-interval"
    match parsed.action:
        case CommitAction.STATUS:
            snap, kargs_ok, kargs_err = read_commit_snapshot(host)
            cmdline = host.read_proc_cmdline()
            return 0, format_commit_status(
                snap,
                kargs_ok=kargs_ok,
                kargs_error=kargs_err,
                proc_cmdline=cmdline,
                seam_proc=seam_proc,
            )
        case CommitAction.PLAN:
            snap, kargs_ok, kargs_err = read_commit_snapshot(host)
            if not kargs_ok:
                return 1, [f"ERROR: cannot inspect rpm-ostree kargs: {kargs_err}"]
            plan = build_commit_plan(snap, target_seconds=parsed.seconds, revert=parsed.revert)
            return 0, format_commit_plan_lines(plan, prog, dry_run=True, seam_proc=seam_proc)
        case _:
            return execute_commit_change(host, parsed, prog=prog, seam_proc=seam_proc)

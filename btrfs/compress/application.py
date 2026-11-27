# SPDX-License-Identifier: MPL-2.0
"""Use cases for `cst btrfs compress-force`: status, plan, apply, revert (no direct terminal I/O)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from btrfs.cache.domain.systemd_view import catch_up_words, fstab_lines_unread
from btrfs.compress.domain import (
    FSTAB_PATH,
    FSTAB_UNREAD,
    PROC_MOUNTS_PATH,
    REMOUNT_UNREAD,
    TOP_MOUNT_UNIT_PATH,
    TOP_MOUNT_UNREAD,
    CoherenceReport,
    CompressMode,
    CompressPlan,
    CompressSnapshot,
    CompressStatusView,
    MountEntry,
    RootOptionLayerPlan,
    assess_coherence,
    build_plan,
    effective_compress_spec,
    extract_rootflags_tokens,
    kargs_edits_staged,
    kargs_unread,
    matches_target_spec,
    parse_proc_mounts,
    pick_root_mounts,
    top_mount_changed,
)
from btrfs.compress.domain.args import CompressArgs, CompressCommand
from btrfs.compress.domain.rpm_ostree import kargs_refused
from btrfs.compress.ports import TOP_MOUNT_ERRORS, CompressHostPort
from cst_core.domain.errors import Refused
from cst_core.domain.values import ExitAndLines
from cst_core.ports import CommandFailed


FSTAB_RELOAD_PENDING: Final = f"daemon-reload: systemd has not loaded {FSTAB_PATH} as it is on disk"
"""What plan and status say when a run would reload systemd first (catch_up_reload)."""
FSTAB_RELOAD_UNKNOWN: Final = (
    f"daemon-reload: systemctl cannot say whether systemd has loaded {FSTAB_PATH} as it is on disk"
)
"""What plan and status say when systemctl gives no answer: a run then reloads systemd first too."""


def fstab_behind(host: CompressHostPort) -> bool | None:
    """Whether systemd has not loaded /etc/fstab as it is on disk: a unit it loaded from /etc/fstab says
    NeedDaemonReload=yes (an earlier run stopped between its /etc/fstab write and its reload); None when
    systemctl gives no answer. Only that rule of cache's fstab_lines_unread: compress-force and
    commit-interval manage no bind line of their own."""
    return fstab_lines_unread(host.mount_units(), ())


def reload_pending_line(host: CompressHostPort) -> str:
    """What plan and status say of the catch-up reload a run would do (catch_up_reload, which reloads in both
    cases): FSTAB_RELOAD_PENDING when systemd is behind /etc/fstab, FSTAB_RELOAD_UNKNOWN when systemctl gives
    no answer, "" when it is not behind."""
    behind = fstab_behind(host)
    if behind is None:
        return FSTAB_RELOAD_UNKNOWN
    return FSTAB_RELOAD_PENDING if behind else ""


def catch_up_reload(host: CompressHostPort, new_fstab_text: str | None) -> tuple[str | None, list[str]]:
    """For a run that writes no /etc/fstab (new_fstab_text None; one that writes it reloads with it):
    daemon-reload when systemd is behind /etc/fstab or systemctl cannot say, as cache's catch-up reload:
    (why the run fails, or None; the lines to add), worded by catch_up_words."""
    if new_fstab_text is not None:
        return None, []
    behind = fstab_behind(host)
    if behind is False:
        return None, []
    return catch_up_words(behind=behind, reload_error=host.daemon_reload())


type LayerUndo = Callable[[], str | None]
"""The undo of one layer a run changed: None, or what it could not put back ("LAYER: why")."""


@dataclass(frozen=True, slots=True)
class LayerRun:
    """One apply or revert of compress-force or commit-interval over CompressHostPort: the host, what it read,
    the lines it says, and the undo of each layer it changed (unwind_rollback runs them, the last first)."""

    host: CompressHostPort
    snap: CompressSnapshot
    msgs: list[str] = field(default_factory=list)
    undo: list[LayerUndo] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StepFailure:
    """Why a layer step failed. unwind False: the run ends with the error alone, no rollback words (compress-force's
    /etc/fstab write that failed with the old text still in place)."""

    error: str
    unwind: bool = True


@dataclass(frozen=True, slots=True)
class RemountStep:
    """The live remount of a tool: its target and option, the option that undoes it, whether a root mount shows
    the new value, and what the verification expected (in its error)."""

    target: str
    option: str
    undo_option: str
    at_target: Callable[[MountEntry], bool]
    expected: str


type LayerStep = Callable[[], StepFailure | None]


def unwind_rollback(undo: Sequence[LayerUndo]) -> str:
    """Run the undo of every layer changed, the last first: "previous configuration restored." or
    "ROLLBACK INCOMPLETE (...)" naming what could not be put back."""
    errors: list[str] = [err for step in reversed(undo) if (err := step()) is not None]
    return f"ROLLBACK INCOMPLETE ({'; '.join(errors)})" if errors else "previous configuration restored."


def unknown_layer(layer_error: str) -> LayerUndo:
    """The undo of a layer that cannot be read back after its step failed: nothing to run, its state named."""
    return lambda: layer_error


def _undo_fstab(host: CompressHostPort, prev_fstab: str) -> str | None:
    try:
        host.write_fstab(prev_fstab, put_back=False)
    except (OSError, CommandFailed) as exc:
        return f"fstab: {exc}"
    return None


def _undo_kargs(host: CompressHostPort, edits: tuple[tuple[str, str], ...]) -> str | None:
    ok, err = host.update_kargs([(new, old) for old, new in edits])
    return None if ok else f"kargs: {err}"


def _undo_remount(host: CompressHostPort, target: str, option: str) -> str | None:
    ok, err = host.remount(target, option)
    return None if ok else f"remount: {err}"


def _undo_top_mount(host: CompressHostPort, uuid: str, options: str) -> str | None:
    try:
        host.update_top_mount(uuid, options, put_back=False)
    except TOP_MOUNT_ERRORS as exc:
        return f"top-mount: {exc}"
    return None


def step_fstab(run: LayerRun, new_text: str | None, done: str, *, unwind_untouched: bool = True) -> StepFailure | None:
    """Write /etc/fstab (and reload systemd); done: the line a write that worked says. A write that failed after
    the new text was in place is undone like one that worked; one that cannot be read back is named; one that
    left the old text ends the run without rollback words when unwind_untouched is False."""
    host, prev_fstab = run.host, run.snap.fstab_text
    if new_text is None or prev_fstab is None:
        return None
    try:
        host.write_fstab(new_text)
    except (OSError, CommandFailed) as exc:
        failure = f"Failed to update {FSTAB_PATH}: {exc}"
        now = host.read_fstab()
        if now is None:
            run.undo.append(unknown_layer(FSTAB_UNREAD))
        elif now == new_text:
            run.undo.append(lambda: _undo_fstab(host, prev_fstab))
        else:
            return StepFailure(failure, unwind=unwind_untouched)
        return StepFailure(failure)
    run.undo.append(lambda: _undo_fstab(host, prev_fstab))
    run.msgs.append(done)
    return None


def step_kargs(run: LayerRun, edits: tuple[tuple[str, str], ...], done: str) -> StepFailure | None:
    """Stage the rootflags= edits with rpm-ostree; done: the line a change that worked says."""
    if not edits:
        return None
    host = run.host
    ok, kerr = host.update_kargs(edits)
    if not ok:
        # rpm-ostree can stage the change and still answer a failure (a client that timed out while its
        # transaction went on): the kernel arguments are read once no transaction runs. A change refused
        # while another transaction runs staged nothing: they are read at once.
        read = host.read_kargs() if kargs_refused(kerr) else host.read_settled_kargs()
        if not read[0]:
            run.undo.append(unknown_layer(kargs_unread(read[2])))
        elif kargs_edits_staged(read, edits):
            run.undo.append(lambda: _undo_kargs(host, edits))
        return StepFailure(f"Failed to update rpm-ostree kargs: {kerr}")
    run.undo.append(lambda: _undo_kargs(host, edits))
    run.msgs.append(done)
    return None


def step_remount(run: LayerRun, remount: RemountStep | None) -> StepFailure | None:
    """Remount the live root, then check that every root mount shows the new value."""
    if remount is None:
        return None
    host, target, undo_option = run.host, remount.target, remount.undo_option
    ok, rerr = host.remount(target, remount.option)
    if not ok:
        # mount(8) can answer a failure after the kernel changed the mount (it could not update its mount table,
        # or it was killed after the call): a mount that shows the new value is undone with the rest; mounts
        # that cannot be read back are named.
        now = host.read_proc_mounts()
        if now is None:
            run.undo.append(unknown_layer(REMOUNT_UNREAD))
        elif any(e.target == target and remount.at_target(e) for e in pick_root_mounts(parse_proc_mounts(now))):
            run.undo.append(lambda: _undo_remount(host, target, undo_option))
        return StepFailure(f"Failed to remount {target} with {remount.option}: {rerr}")
    run.undo.append(lambda: _undo_remount(host, target, undo_option))
    after_roots = pick_root_mounts(parse_proc_mounts(host.read_proc_mounts() or ""))
    if not after_roots or not all(remount.at_target(e) for e in after_roots):
        return StepFailure(f"Post-remount verification failed in {PROC_MOUNTS_PATH}: expected {remount.expected}")
    run.msgs.append(f"Remounted {target} with {remount.option} (verified in {PROC_MOUNTS_PATH}).")
    return None


def step_top_mount(run: LayerRun, options: str | None) -> StepFailure | None:
    """Re-render the top-mount unit with options, with its own UUID or else the root filesystem's."""
    top = run.snap.top_mount
    if options is None or top is None:
        return None
    uuid = top.uuid or run.snap.root_uuid
    if not uuid:
        return StepFailure(f"Cannot update {TOP_MOUNT_UNIT_PATH}: root filesystem UUID unknown")
    host = run.host
    try:
        host.update_top_mount(uuid, options)
    except TOP_MOUNT_ERRORS as exc:
        # A failure whose put-back failed too leaves the new unit: it is undone with the rest; a unit that cannot
        # be read back is named.
        now = host.read_top_mount()
        if now is None:
            run.undo.append(unknown_layer(TOP_MOUNT_UNREAD))
        elif top_mount_changed(now, options):
            prev = top.options
            run.undo.append(lambda: _undo_top_mount(host, uuid, prev))
        return StepFailure(f"Failed to update {TOP_MOUNT_UNIT_PATH}: {exc}")
    run.msgs.append(f"Updated {TOP_MOUNT_UNIT_PATH} and reloaded systemd.")
    return None


def run_layers(run: LayerRun, steps: Sequence[LayerStep]) -> str | None:
    """Run each step only after the one before it worked: the first that fails stops the run before the later
    layers, and its error comes back with the rollback of the layers changed (unwind_rollback); None when every
    step worked."""
    for step in steps:
        failed = step()
        if failed is not None:
            return f"{failed.error}; {unwind_rollback(run.undo)}" if failed.unwind else failed.error
    return None


@dataclass(frozen=True, slots=True)
class CompressPlanResult:
    """Outcome of `plan`."""

    exit_code: int
    seam_proc: str
    plan: CompressPlan | None = None
    error: str = ""
    hint_cmd: CompressCommand = CompressCommand.APPLY
    reload_pending: str = ""  # a run would reload systemd first: what plan says of it (reload_pending_line)


@dataclass(frozen=True, slots=True)
class CompressChangeResult:
    """Outcome of `apply` or `revert`."""

    exit_code: int
    seam_proc: str
    plan: CompressPlan | None = None
    messages: tuple[str, ...] = ()
    error: str = ""


def _booted_kargs_diff(
    snap: CompressSnapshot,
    proc_cmdline: str | None,
    *,
    kargs_ok: bool,
) -> tuple[str, str] | None:
    if not (snap.is_ostree and kargs_ok and proc_cmdline is not None and snap.kargs_text is not None):
        return None
    boot_rf = extract_rootflags_tokens(proc_cmdline)
    next_rf = extract_rootflags_tokens(snap.kargs_text)
    # The kernel uses the last rootflags= of its command line.
    if boot_rf and next_rf and boot_rf[-1] != next_rf[-1]:
        return (boot_rf[-1], next_rf[-1])
    return None


def _apply_kargs_coherence(
    snap: CompressSnapshot,
    coherence: CoherenceReport,
    kargs_err: str,
    *,
    kargs_ok: bool,
) -> CoherenceReport:
    incomplete_summary: str | None = None
    if snap.is_ostree and not kargs_ok:
        incomplete_summary = f"incomplete (rpm-ostree kargs error: {kargs_err})"
    elif snap.is_ostree and snap.kargs_text is not None and not extract_rootflags_tokens(snap.kargs_text):
        incomplete_summary = "incomplete (no rootflags= in rpm-ostree kargs)"
    if incomplete_summary is None:
        return coherence
    return CoherenceReport(
        coherent=False,
        mode=coherence.mode,
        level=coherence.level,
        summary=incomplete_summary,
        drift_details=coherence.drift_details,
        has_compression=coherence.has_compression,
    )


class CompressService:
    """Orchestrates reading and updating root btrfs compression across all config layers."""

    def __init__(self, host: CompressHostPort, *, seam_proc: str = "") -> None:
        self._host = host
        self._seam_proc = seam_proc

    def _snapshot(self) -> tuple[CompressSnapshot, bool, str]:
        is_ostree = self._host.is_ostree()
        kargs_ok, kargs_text, kargs_err = self._host.read_kargs() if is_ostree else (True, "", "")
        snap = CompressSnapshot(
            is_ostree=is_ostree,
            root_uuid=self._host.root_uuid(),
            fstab_text=self._host.read_fstab(),
            kargs_text=kargs_text if (is_ostree and kargs_ok) else None,
            proc_mounts_text=self._host.read_proc_mounts(),
            top_mount=self._host.read_top_mount(),
        )
        ok = True if not is_ostree else kargs_ok
        return snap, ok, kargs_err

    def gather_status(self) -> CompressStatusView:
        """Collect read-only status across live mounts, fstab, kargs, and top-mount."""
        snap, kargs_ok, kargs_err = self._snapshot()
        coherence = _apply_kargs_coherence(snap, assess_coherence(snap), kargs_err, kargs_ok=kargs_ok)
        live_roots = pick_root_mounts(parse_proc_mounts(snap.proc_mounts_text or ""))
        live_entries = tuple(
            (e.target, sp.raw if (sp := effective_compress_spec(e.options)) is not None else "(none)")
            for e in live_roots
        )
        plan_view = build_plan(
            snap,
            target_mode=coherence.mode or CompressMode.COMPRESS_FORCE,
            level_override=coherence.level,
        )
        booted_diff = _booted_kargs_diff(snap, self._host.read_proc_cmdline(), kargs_ok=kargs_ok)
        kargs_err_msg = kargs_err if (snap.is_ostree and not kargs_ok) else ""
        return CompressStatusView(
            seam_proc=self._seam_proc,
            live_entries=live_entries,
            layers=plan_view.layers,
            kargs_error=kargs_err_msg,
            booted_diff=booted_diff,
            coherence=coherence,
            left_alone_notes=plan_view.left_alone_notes,
            reload_pending=reload_pending_line(self._host),
        )

    def build_change_plan(self, args: CompressArgs) -> CompressPlanResult:
        """Compute the transition plan without mutating the machine."""
        snap, kargs_ok, kargs_err = self._snapshot()
        hint_cmd = CompressCommand.REVERT if args.revert else CompressCommand.APPLY
        if not kargs_ok:
            return CompressPlanResult(1, self._seam_proc, error=f"cannot inspect rpm-ostree kargs: {kargs_err}")
        target_mode = CompressMode.COMPRESS if args.revert else CompressMode.COMPRESS_FORCE
        plan = build_plan(snap, target_mode=target_mode, level_override=args.level)
        pending = reload_pending_line(self._host) if plan.new_fstab_text is None else ""
        return CompressPlanResult(0, self._seam_proc, plan=plan, hint_cmd=hint_cmd, reload_pending=pending)

    def _apply_layers(self, snap: CompressSnapshot, plan: CompressPlan) -> tuple[tuple[str, ...], str | None]:
        run = LayerRun(self._host, snap)
        orig_live_spec = next(
            (
                effective_compress_spec(e.options)
                for e in pick_root_mounts(parse_proc_mounts(snap.proc_mounts_text or ""))
                if e.target == plan.remount_target
            ),
            None,
        )
        remount = (
            None
            if plan.remount_target is None
            else RemountStep(
                plan.remount_target,
                plan.target_option,
                orig_live_spec.raw if orig_live_spec is not None else "compress=no",
                lambda e: matches_target_spec(effective_compress_spec(e.options), plan.target_mode, plan.target_level),
                plan.target_option,
            )
        )
        option = plan.target_option
        steps: tuple[LayerStep, ...] = (
            lambda: step_fstab(
                run,
                plan.new_fstab_text,
                f"Updated {FSTAB_PATH} ({option}) and reloaded systemd.",
                unwind_untouched=False,
            ),
            lambda: step_kargs(run, plan.kargs_edits, f"Updated rpm-ostree kargs ({option} for next boot)."),
            lambda: step_remount(run, remount),
            lambda: step_top_mount(run, plan.new_top_mount_options),
        )
        err = run_layers(run, steps)
        return tuple(run.msgs), err

    def _not_confirmed(self, plan: CompressPlan) -> CompressChangeResult | None:
        """Ask before a change: None when confirmed, else the result of the run that stops there."""
        try:
            confirmed = self._host.confirm(f"Apply {plan.target_option} across active layers?")
        except Refused as exc:
            return CompressChangeResult(
                1,
                self._seam_proc,
                plan=plan,
                error=f"{exc} (pass --yes to apply non-interactively).",
            )
        if not confirmed:
            return CompressChangeResult(1, self._seam_proc, plan=plan, messages=("Aborted; nothing changed.",))
        return None

    def execute_change(self, args: CompressArgs) -> CompressChangeResult:
        """Run `apply` or `revert` atomically across all active layers."""
        snap, kargs_ok, kargs_err = self._snapshot()
        if not kargs_ok:
            return CompressChangeResult(1, self._seam_proc, error=f"cannot inspect rpm-ostree kargs: {kargs_err}")
        target_mode = CompressMode.COMPRESS if args.command == CompressCommand.REVERT else CompressMode.COMPRESS_FORCE
        plan = build_plan(snap, target_mode=target_mode, level_override=args.level)
        if not plan.has_changes:
            cause, reloaded = catch_up_reload(self._host, None)
            if cause is not None:
                return CompressChangeResult(1, self._seam_proc, plan=plan, error=cause)
            return CompressChangeResult(
                0,
                self._seam_proc,
                plan=plan,
                messages=tuple(reloaded)
                or (f"Already at {plan.target_option} across all active layers; nothing to do.",),
            )
        refused = None if args.yes else self._not_confirmed(plan)
        if refused is not None:
            return refused
        cause, reloaded = catch_up_reload(self._host, plan.new_fstab_text)
        if cause is not None:
            return CompressChangeResult(1, self._seam_proc, plan=plan, error=cause)
        msgs, err_msg = self._apply_layers(snap, plan)
        return CompressChangeResult(
            1 if err_msg is not None else 0,
            self._seam_proc,
            plan=plan,
            messages=(*reloaded, *msgs),
            error=err_msg or "",
        )


@dataclass(frozen=True, slots=True)
class RootOptionChangeOps[PlanT]:
    """Callbacks parameterizing transactional apply/revert for root btrfs option tools."""

    build_plan: Callable[[CompressSnapshot], PlanT]
    has_changes: Callable[[PlanT], bool]
    new_fstab_text: Callable[[PlanT], str | None]
    confirm_prompt: Callable[[PlanT], str]
    steps: Callable[[LayerRun, PlanT], tuple[LayerStep, ...]]
    format_plan: Callable[[PlanT, str], list[str]]


def _confirm_layer_change(host: CompressHostPort, lines: list[str], prompt: str) -> ExitAndLines | None:
    try:
        confirmed = host.confirm(prompt)
    except Refused as exc:
        return 1, [*lines, f"ERROR: {exc} (pass --yes to apply non-interactively)."]
    if not confirmed:
        return 1, [*lines, "Aborted; nothing changed."]
    return None


def run_root_option_change[PlanT](
    host: CompressHostPort,
    snap_read: tuple[CompressSnapshot, bool, str],
    ops: RootOptionChangeOps[PlanT],
    *,
    yes: bool,
) -> ExitAndLines:
    """Shared confirmation, systemd catch-up reload, layer execution, and rollback driver."""
    snap, kargs_ok, kargs_err = snap_read
    if not kargs_ok:
        return 1, [f"ERROR: cannot inspect rpm-ostree kargs: {kargs_err}"]
    plan = ops.build_plan(snap)
    if not ops.has_changes(plan):
        cause, reloaded = catch_up_reload(host, None)
        said = cause or "\n".join(reloaded)
        lines = ops.format_plan(plan, said)
        return (1, [*lines, f"ERROR: {cause}"]) if cause is not None else (0, [*lines, *reloaded])
    lines = ops.format_plan(plan, "")
    if not yes and (stopped := _confirm_layer_change(host, lines, ops.confirm_prompt(plan))) is not None:
        return stopped
    cause, reloaded = catch_up_reload(host, ops.new_fstab_text(plan))
    if cause is not None:
        return 1, [*lines, f"ERROR: {cause}"]
    lines.extend(reloaded)
    run = LayerRun(host, snap)
    err = run_layers(run, ops.steps(run, plan))
    return (1, [*lines, *run.msgs, f"ERROR: {err}"]) if err is not None else (0, [*lines, *run.msgs])


def make_layer_steps(
    run: LayerRun,
    plan: RootOptionLayerPlan,
    remount: RemountStep | None,
    *,
    fstab_done: str,
    kargs_done: str,
) -> tuple[LayerStep, ...]:
    """Build standard 4-layer execution sequence for root btrfs option tools."""
    return (
        lambda: step_fstab(run, plan.new_fstab_text, fstab_done),
        lambda: step_kargs(run, plan.kargs_edits, kargs_done),
        lambda: step_remount(run, remount),
        lambda: step_top_mount(run, plan.new_top_mount_options),
    )

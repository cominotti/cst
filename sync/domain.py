# SPDX-License-Identifier: MPL-2.0
"""Pure domain model, generated-file rendering lookup, CLI parsing, and sync planning for `cst sync`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import posixpath
from typing import Final

from cpu.irq.domain import IRQ_DROPIN_PATH, IRQ_ENV_PATH, render_irq_dropin, render_irq_env
from cpu.setup.domain import (
    DROPIN_NAME as CPU_DROPIN_NAME,
    SetupPaths as CpuSetupPaths,
    helper_dropin_text,
    hwp_tmpfiles_text,
    ntsync_modules_load_text,
    toolbox_dropin_text,
)
from cst_core.domain.contracts import CARGO_BUILD_TMPFS_TMPFILES_PREFIX
from cst_core.domain.install import (
    COMPONENT_SYNC,
    COMPONENTS,
    INSTALL_MANIFEST,
    Component,
    InstallEntry,
    Owner,
    SyncSafety,
    expand_dest,
    manifest_by_src,
)
from cst_core.domain.record import ComponentState, InstallRecord, RecordedFile
from cst_core.domain.text import HELP_WORDS, NO_WORDS, YES_WORDS, enum_member, parse_subcommand_head
from cst_core.domain.units import SYSTEM_UNIT_DIR
from cst_core.domain.values import parse_whole_number
from toolchains.domain.auto import render_service as render_auto_service, render_timer as render_auto_timer
from toolchains.domain.common import SERVICE as AUTO_SERVICE, TIMER as AUTO_TIMER
from toolchains.domain.config import WrapperValues, render_wrapper
from toolchains.domain.tmpfs import TmpfsTargetUser, render_tmpfs_mount_unit, render_tmpfs_tmpfiles
from vm.domain import (
    SERVICE as VM_SERVICE,
    TIMER as VM_TIMER,
    render_service as render_vm_service,
    render_timer as render_vm_timer,
)

SYNC_PROG: Final = "cst sync"


class SyncAction(StrEnum):
    """Supported subcommands of `cst sync`."""

    HELP = "help"
    PLAN = "plan"
    APPLY = "apply"


class _SyncFlag(StrEnum):
    """CLI flags accepted by `cst sync`."""

    INCLUDE_RISKY = "--include-risky"
    OVERWRITE_MODIFIED = "--overwrite-modified"
    ROOT_ONLY = "--root-only"


class FileDrift(StrEnum):
    """Why an installed file causes its component to need a refresh."""

    OUTDATED = "outdated"
    MISSING = "missing"
    MODIFIED = "modified"
    PRE_RECORD = "pre-record"


class StepDisposition(StrEnum):
    """Whether a due refresh command runs now or is held back by a safety policy."""

    SYNC = "sync"
    SKIP_RISKY = "skip-risky"
    SKIP_MODIFIED = "skip-modified"


class StepOutcome(StrEnum):
    """Execution result of one scheduled refresh command."""

    UPDATED = "updated"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SyncArgs:
    """Validated CLI arguments for `cst sync`."""

    action: SyncAction
    include_risky: bool = False
    overwrite_modified: bool = False
    root_only: bool = False
    error: str | None = None
    code: int = 0


@dataclass(frozen=True, slots=True)
class SyncFileStatus:
    """Per-file drift finding included in a component status."""

    dest: str
    drift: FileDrift


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    """Computed sync state for one CST component."""

    component: Component
    installed: bool
    pending: bool
    files: tuple[SyncFileStatus, ...]

    @property
    def needs_sync(self) -> bool:
        """True when at least one file drifted or the component stayed pending."""
        return self.pending or bool(self.files)

    @property
    def has_modified(self) -> bool:
        """True when at least one file differs on disk from its install record."""
        return any(f.drift == FileDrift.MODIFIED for f in self.files)


@dataclass(frozen=True, slots=True)
class SyncStep:
    """One deduplicated refresh command covering one or more components."""

    command_label: str
    argv: tuple[str, ...]
    needs_root: bool
    safety: SyncSafety
    reason: str
    disposition: StepDisposition
    components: tuple[ComponentStatus, ...]


@dataclass(frozen=True, slots=True)
class SyncPlan:
    """Complete read-only plan for `cst sync plan` and execution input for `apply`."""

    steps: tuple[SyncStep, ...]
    up_to_date_components: int
    unreadable_files: tuple[str, ...]
    notes: tuple[str, ...] = ()

    @property
    def steps_to_run(self) -> tuple[SyncStep, ...]:
        """Steps cleared to run under the requested flags."""
        return tuple(s for s in self.steps if s.disposition == StepDisposition.SYNC)

    @property
    def skipped_risky(self) -> tuple[SyncStep, ...]:
        """Steps held back because they carry elevated risk without --include-risky."""
        return tuple(s for s in self.steps if s.disposition == StepDisposition.SKIP_RISKY)

    @property
    def skipped_modified(self) -> tuple[SyncStep, ...]:
        """Steps held back because a file was modified on disk without --overwrite-modified."""
        return tuple(s for s in self.steps if s.disposition == StepDisposition.SKIP_MODIFIED)


@dataclass(frozen=True, slots=True)
class StepResult:
    """Result of executing a single `SyncStep`."""

    step: SyncStep
    outcome: StepOutcome
    returncode: int = 0


@dataclass(frozen=True, slots=True)
class SyncApplyReport:
    """Summary returned after `cst sync apply` finishes."""

    plan: SyncPlan
    results: tuple[StepResult, ...]
    warnings: tuple[str, ...] = ()
    root_only: bool = False
    root_subpass_rc: int | None = None

    @property
    def ok(self) -> bool:
        """True when every executed step (and any delegated root subpass) succeeded."""
        return all(r.outcome == StepOutcome.UPDATED for r in self.results) and self.root_subpass_rc in (None, 0)


def parse_sync_args(argv: Sequence[str]) -> SyncArgs:
    """Parse argv after `cst sync` into a validated `SyncArgs`."""
    action, rest, err = parse_subcommand_head(argv, SyncAction, default=SyncAction.PLAN, help_action=SyncAction.HELP)
    if err is not None:
        return SyncArgs(SyncAction.HELP, error=err, code=2)
    if any(tok in HELP_WORDS for tok in rest):
        return SyncArgs(SyncAction.HELP)
    include_risky = False
    overwrite_modified = False
    root_only = False
    for tok in rest:
        flag = enum_member(_SyncFlag, tok)
        match flag:
            case _SyncFlag.INCLUDE_RISKY:
                include_risky = True
            case _SyncFlag.OVERWRITE_MODIFIED:
                overwrite_modified = True
            case _SyncFlag.ROOT_ONLY:
                root_only = True
            case _:
                return SyncArgs(SyncAction.HELP, error=f"Unknown option: {tok}", code=2)
    if action != SyncAction.APPLY and root_only:
        return SyncArgs(SyncAction.HELP, error="--root-only is only valid with apply", code=2)
    return SyncArgs(
        action=action,
        include_risky=include_risky,
        overwrite_modified=overwrite_modified,
        root_only=root_only,
    )


def format_sync_help(prog: str = SYNC_PROG) -> str:
    """Render concise CLI help text for `cst sync`."""
    return (
        "Idempotent refresh of installed CST components from the current checkout.\n"
        "\n"
        "Commands:\n"
        "  plan [--include-risky] [--overwrite-modified]\n"
        "      Dry run: inspect /var/lib/cst/installed.json and ~/.local/state/cst/installed.json,\n"
        "      list components to refresh, components held back by precaution, and files edited on disk.\n"
        "  apply [--include-risky] [--overwrite-modified]\n"
        "      Run the safe refresh commands for installed components that are outdated, missing files,\n"
        "      or pending; asks sudo at most once if root components need updating.\n"
        "  help\n"
        "      Show this help.\n"
        "\n"
        "Options:\n"
        "  --include-risky       Also run components marked precautionary (degraded-boot initramfs,\n"
        "                        snapshots setup / top-mount, swap, Bluetooth restarts).\n"
        "  --overwrite-modified  Also refresh components whose installed file differs on disk from its\n"
        "                        install record (manual edits).\n"
        "\n"
        f"Usage: {prog} <plan|apply|help> [options]\n"
    )


def _wrapper_bytes(values: Mapping[str, str] | None, raw_src_text: str | None) -> bytes | None:
    if raw_src_text is None or values is None:
        return None
    size = values.get("cache_size")
    ws_word = values.get("workspace")
    if size is None or size == "" or ws_word is None:
        return None
    if ws_word in YES_WORDS:
        ws = True
    elif ws_word in NO_WORDS:
        ws = False
    else:
        return None
    rendered = render_wrapper(raw_src_text, WrapperValues(size=size, workspace=ws))
    return None if rendered is None else rendered.encode("utf-8")


def _toolchains_auto_bytes(dest: str, values: Mapping[str, str] | None) -> bytes | None:
    del values
    if dest.endswith(f"/{AUTO_SERVICE}"):
        return render_auto_service().encode("utf-8")
    if dest.endswith(f"/{AUTO_TIMER}"):
        return render_auto_timer().encode("utf-8")
    return None


def _vm_timer_bytes(dest: str, values: Mapping[str, str] | None) -> bytes | None:
    if dest == posixpath.join(SYSTEM_UNIT_DIR, VM_TIMER):
        return render_vm_timer().encode("utf-8")
    if dest == posixpath.join(SYSTEM_UNIT_DIR, VM_SERVICE) and values is not None:
        program = values.get("program", "")
        stop_timeout = values.get("stop_timeout", "")
        if program and stop_timeout:
            return render_vm_service(program, stop_timeout).encode("utf-8")
    return None


def _cpu_core_bytes(dest: str, values: Mapping[str, str] | None) -> bytes | None:
    del values
    cpu_paths = CpuSetupPaths()
    if dest == cpu_paths.ntsync_modules_load:
        return ntsync_modules_load_text().encode("utf-8")
    if dest == cpu_paths.hwp_tmpfiles:
        return hwp_tmpfiles_text(cpu_paths.hwp_boost).encode("utf-8")
    return None


def _cpu_toolbox_bytes(dest: str, values: Mapping[str, str] | None) -> bytes | None:
    del values
    if dest.endswith(f"/user.slice.d/{CPU_DROPIN_NAME}"):
        return toolbox_dropin_text().encode("utf-8")
    if dest.endswith(f"/flatpak-session-helper.service.d/{CPU_DROPIN_NAME}"):
        return helper_dropin_text().encode("utf-8")
    return None


def _cpu_irq_bytes(dest: str, values: Mapping[str, str] | None) -> bytes | None:
    if dest == IRQ_DROPIN_PATH:
        return render_irq_dropin().encode("utf-8")
    if dest == IRQ_ENV_PATH and values is not None and "banned_cpulist" in values:
        return render_irq_env(values["banned_cpulist"]).encode("utf-8")
    return None


def _tmpfs_generated_bytes(dest: str, values: Mapping[str, str] | None) -> bytes | None:
    if values is None:
        return None
    user = values.get("user", "")
    group = values.get("group", "")
    home = values.get("home", "")
    uid = parse_whole_number(values.get("uid", ""))
    gid = parse_whole_number(values.get("gid", ""))
    if not (user and group and home and uid is not None and gid is not None):
        return None
    target = TmpfsTargetUser.from_home(user=user, group=group, uid=uid, gid=gid, real_home=home)
    if dest.startswith(f"{SYSTEM_UNIT_DIR}/") and dest.endswith(".mount"):
        size_bytes = parse_whole_number(values.get("size_bytes", ""))
        return None if size_bytes is None else render_tmpfs_mount_unit(target, size_bytes).encode("utf-8")
    if dest.startswith(CARGO_BUILD_TMPFS_TMPFILES_PREFIX):
        return render_tmpfs_tmpfiles(target).encode("utf-8")
    return None


_GENERATED_RENDERERS: Final = {
    Component.TOOLCHAINS_AUTO: _toolchains_auto_bytes,
    Component.VM_BACKUP_TIMER: _vm_timer_bytes,
    Component.CPU_CORE: _cpu_core_bytes,
    Component.CPU_TOOLBOX: _cpu_toolbox_bytes,
    Component.CPU_IRQ: _cpu_irq_bytes,
    Component.TOOLCHAINS_TMPFS: _tmpfs_generated_bytes,
}


def desired_generated_bytes(dest: str, f: RecordedFile, raw_src_text: str | None = None) -> bytes | None:
    """Compute desired bytes for CST-generated text files and `toolchains/rustc-wrapper`.

    Returns None for files whose rendering depends on unrecorded external state or legacy records
    that predate parameter recording (`values=None`).
    """
    if f.src == "toolchains/rustc-wrapper":
        return _wrapper_bytes(f.values, raw_src_text)
    if f.src is not None:
        return None
    comp = enum_member(Component, f.component)
    renderer = _GENERATED_RENDERERS.get(comp) if comp is not None else None
    return None if renderer is None else renderer(dest, f.values)


@dataclass(frozen=True, slots=True)
class SyncHostObservation:
    """Pure snapshot of installed records, current disk hashes, and checkout hashes."""

    root_record: InstallRecord | None
    user_record: InstallRecord | None
    user_home: str
    disk_sha256: Mapping[str, str | None]  # dest -> sha256 or None if unreadable (absent dests not mapped)
    missing_dests: frozenset[str]
    desired_manifest_sha256: Mapping[tuple[str, tuple[tuple[str, str], ...]], str | None]
    desired_generated_sha256: Mapping[str, str | None]


def values_key(values: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    """Canonical sorted key for a recorded template's `--set` mapping."""
    return tuple(sorted((values or {}).items()))


def _pre_record_comparable(e: InstallEntry, owner: Owner, recorded_components: frozenset[str]) -> bool:
    return (
        e.owner == owner
        and e.component not in recorded_components
        and not e.is_template
        and e.src != "toolchains/rustc-wrapper"
    )


def _desired_sha_for_recorded(obs: SyncHostObservation, dest: str, f: RecordedFile) -> str | None:
    if f.src is not None and f.src != "":
        entry = manifest_by_src().get(f.src)
        if entry is None:
            return None
        if f.values is not None and not entry.is_template and entry.src != "toolchains/rustc-wrapper":
            return None
        return obs.desired_manifest_sha256.get((entry.src, values_key(f.values)))
    return obs.desired_generated_sha256.get(dest)


@dataclass(frozen=True, slots=True)
class _PlanAccumulator:
    per_comp_files: dict[Component, list[SyncFileStatus]]
    pending_comps: set[Component]
    installed_comps: set[Component]
    unreadable: list[str]


def _collect_record_components(obs: SyncHostObservation, rec: InstallRecord, acc: _PlanAccumulator) -> None:
    for comp_name, state in rec.components.items():
        comp = enum_member(Component, comp_name)
        if comp is None:
            continue
        acc.installed_comps.add(comp)
        if state == ComponentState.PENDING:
            acc.pending_comps.add(comp)
    for dest, f in sorted(rec.files.items()):
        comp = enum_member(Component, f.component)
        if comp is None:
            continue
        acc.installed_comps.add(comp)
        if dest in obs.missing_dests:
            acc.per_comp_files.setdefault(comp, []).append(SyncFileStatus(dest, FileDrift.MISSING))
            continue
        disk_sha = obs.disk_sha256.get(dest)
        if disk_sha is None:
            acc.unreadable.append(dest)
        elif disk_sha != f.sha256:
            acc.per_comp_files.setdefault(comp, []).append(SyncFileStatus(dest, FileDrift.MODIFIED))
        want_sha = _desired_sha_for_recorded(obs, dest, f)
        if want_sha is not None and want_sha != f.sha256:
            acc.per_comp_files.setdefault(comp, []).append(SyncFileStatus(dest, FileDrift.OUTDATED))


def _collect_pre_record_components(
    obs: SyncHostObservation,
    owner: Owner,
    recorded_components: frozenset[str],
    acc: _PlanAccumulator,
) -> None:
    home = obs.user_home if owner == Owner.USER else None
    if owner == Owner.USER and (home is None or home == ""):
        return
    for e in INSTALL_MANIFEST:
        if not _pre_record_comparable(e, owner, recorded_components):
            continue
        comp = enum_member(Component, e.component)
        if comp is None:
            continue
        dest = expand_dest(e.dest, home)
        if dest in obs.missing_dests:
            continue
        disk_sha = obs.disk_sha256.get(dest)
        if disk_sha is None:
            continue
        acc.installed_comps.add(comp)
        want = obs.desired_manifest_sha256.get((e.src, ()))
        if want is not None and disk_sha != want:
            acc.per_comp_files.setdefault(comp, []).append(SyncFileStatus(dest, FileDrift.PRE_RECORD))


_IMPLICIT_MODIFIED_DEPS: Final[Mapping[Component, tuple[Component, ...]]] = {
    Component.BTRFS_SNAPSHOTS: (Component.BTRFS_ALERTS,),
}


def _step_disposition(
    spec_safety: SyncSafety,
    comps: Sequence[ComponentStatus],
    args: SyncArgs,
    *,
    modified_comps: frozenset[Component] = frozenset(),
) -> StepDisposition:
    if not args.overwrite_modified and (
        any(c.has_modified for c in comps)
        or any(dep in modified_comps for c in comps for dep in _IMPLICIT_MODIFIED_DEPS.get(c.component, ()))
    ):
        return StepDisposition.SKIP_MODIFIED
    if spec_safety == SyncSafety.RISKY and not args.include_risky:
        return StepDisposition.SKIP_RISKY
    return StepDisposition.SYNC


def plan_sync(obs: SyncHostObservation, args: SyncArgs) -> SyncPlan:
    """Compute a deterministic, deduplicated `SyncPlan` across root and user installs."""
    acc = _PlanAccumulator(per_comp_files={}, pending_comps=set(), installed_comps=set(), unreadable=[])

    for owner, rec in ((Owner.ROOT, obs.root_record), (Owner.USER, obs.user_record)):
        if rec is None:
            _collect_pre_record_components(obs, owner, frozenset(), acc)
        else:
            _collect_record_components(obs, rec, acc)
            recorded_names = frozenset(f.component for f in rec.files.values())
            _collect_pre_record_components(obs, owner, recorded_names, acc)

    statuses: list[ComponentStatus] = []
    up_to_date = 0
    for comp in Component:
        if comp not in acc.installed_comps:
            continue
        st = ComponentStatus(
            component=comp,
            installed=True,
            pending=comp in acc.pending_comps,
            files=tuple(acc.per_comp_files.get(comp, ())),
        )
        if st.needs_sync:
            statuses.append(st)
        else:
            up_to_date += 1

    modified_comps = frozenset(st.component for st in statuses if st.has_modified)
    grouped: dict[tuple[str, ...], list[ComponentStatus]] = {}
    for st in statuses:
        spec = COMPONENT_SYNC[st.component]
        grouped.setdefault(spec.argv, []).append(st)

    steps: list[SyncStep] = []
    for argv, comps in grouped.items():
        first_comp = comps[0].component
        spec = COMPONENT_SYNC[first_comp]
        label = COMPONENTS[first_comp]
        steps.append(
            SyncStep(
                command_label=label,
                argv=argv,
                needs_root=spec.needs_root,
                safety=spec.safety,
                reason=spec.reason,
                disposition=_step_disposition(spec.safety, comps, args, modified_comps=modified_comps),
                components=tuple(comps),
            )
        )
    return SyncPlan(
        steps=tuple(steps),
        up_to_date_components=up_to_date,
        unreadable_files=tuple(sorted(set(acc.unreadable))),
    )

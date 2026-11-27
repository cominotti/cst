# SPDX-License-Identifier: MPL-2.0
"""Application use cases for `cst sync plan` and `cst sync apply`."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from cst_core.domain.errors import Refused
from cst_core.domain.install import INSTALL_MANIFEST, Owner, expand_dest, manifest_by_src
from cst_core.domain.record import InstallRecord, RecordedFile
from sync.domain import (
    StepOutcome,
    StepResult,
    SyncApplyReport,
    SyncArgs,
    SyncHostObservation,
    SyncPlan,
    SyncStep,
    plan_sync,
    values_key,
)
from sync.ports import SyncExecPort, SyncInspectPort


@dataclass(slots=True)
class _ObsAccumulator:
    disk_sha256: dict[str, str | None] = field(default_factory=dict)
    missing_dests: set[str] = field(default_factory=set)
    desired_manifest: dict[tuple[str, tuple[tuple[str, str], ...]], str | None] = field(default_factory=dict)
    desired_generated: dict[str, str | None] = field(default_factory=dict)

    def probe_dest(self, inspect: SyncInspectPort, dest: str) -> None:
        if dest in self.disk_sha256 or dest in self.missing_dests:
            return
        exists, sha = inspect.stat_and_hash(dest)
        if not exists:
            self.missing_dests.add(dest)
        else:
            self.disk_sha256[dest] = sha

    def cache_desired(self, inspect: SyncInspectPort, dest: str, f: RecordedFile) -> None:
        if f.src is None or f.src == "":
            self.desired_generated[dest] = inspect.desired_generated_sha256(dest, f)
            return
        entry = manifest_by_src().get(f.src)
        if entry is None or (
            f.values is not None and not entry.is_template and entry.src != "toolchains/rustc-wrapper"
        ):
            return
        key = (entry.src, values_key(f.values))
        if key not in self.desired_manifest:
            val_dict = dict(f.values) if f.values is not None else None
            self.desired_manifest[key] = inspect.desired_manifest_sha256(entry.src, val_dict)


def _collect_owner_observations(
    inspect: SyncInspectPort, owner: Owner, rec: InstallRecord | None, home: str, acc: _ObsAccumulator
) -> None:
    if rec is not None:
        for dest, f in rec.files.items():
            acc.probe_dest(inspect, dest)
            acc.cache_desired(inspect, dest, f)
    if owner == Owner.USER and not home:
        return
    recorded_comps = frozenset(f.component for f in rec.files.values()) if rec is not None else frozenset()
    for e in INSTALL_MANIFEST:
        if e.owner != owner or e.component in recorded_comps or e.is_template or e.src == "toolchains/rustc-wrapper":
            continue
        dest = expand_dest(e.dest, home if owner == Owner.USER else None)
        acc.probe_dest(inspect, dest)
        key = (e.src, ())
        if key not in acc.desired_manifest:
            acc.desired_manifest[key] = inspect.desired_manifest_sha256(e.src, None)


def plan_host_sync(inspect: SyncInspectPort, args: SyncArgs) -> SyncPlan:
    """Read install records and files via INSPECT and compute a `SyncPlan`."""
    _, _, user_home = inspect.target_user()
    root_rec, root_err = inspect.read_record(Owner.ROOT, "")
    if root_err is not None:
        raise Refused(root_err)
    user_rec, user_err = (None, None)
    if user_home:
        user_rec, user_err = inspect.read_record(Owner.USER, user_home)
        if user_err is not None:
            raise Refused(user_err)

    acc = _ObsAccumulator()
    _collect_owner_observations(inspect, Owner.ROOT, root_rec, "", acc)
    _collect_owner_observations(inspect, Owner.USER, user_rec, user_home, acc)

    obs = SyncHostObservation(
        root_record=root_rec,
        user_record=user_rec,
        user_home=user_home,
        disk_sha256=acc.disk_sha256,
        missing_dests=frozenset(acc.missing_dests),
        desired_manifest_sha256=acc.desired_manifest,
        desired_generated_sha256=acc.desired_generated,
    )
    plan = plan_sync(obs, args)
    if inspect.euid() == 0 and not user_home:
        return replace(
            plan,
            notes=(
                "SUDO_USER is unset (running directly as root): user-level components (~/.local/state/cst/installed.json) were not checked.",
            ),
        )
    return plan


def _run_step(executor: SyncExecPort, step: SyncStep, *, as_user: str | None) -> StepResult:
    rc = executor.run_cst_subcommand(step.argv, as_user=as_user)
    outcome = StepOutcome.UPDATED if rc == 0 else StepOutcome.FAILED
    return StepResult(step=step, outcome=outcome, returncode=rc)


def run_apply(
    inspect: SyncInspectPort, executor: SyncExecPort, args: SyncArgs, *, prog: str = "cst sync"
) -> SyncApplyReport:
    """Execute scheduled refresh steps cleanly, asking sudo at most once when needed."""
    if inspect.in_container():
        raise Refused(
            f"'{prog} apply' must run on the host, not inside a toolbox/container.\nIn a host terminal: {prog} apply"
        )
    euid = inspect.euid()
    if args.root_only and euid != 0:
        raise Refused(f"'{prog} apply --root-only' requires root privileges.")
    plan = plan_host_sync(inspect, args)
    to_run = plan.steps_to_run
    if not to_run:
        return SyncApplyReport(plan=plan, results=(), root_only=args.root_only)

    target_user, _, _ = inspect.target_user()
    results: list[StepResult] = []
    warnings: list[str] = []

    if args.root_only:
        results.extend(_run_step(executor, step, as_user=None) for step in to_run if step.needs_root)
        return SyncApplyReport(plan=plan, results=tuple(results), root_only=True)

    user_steps = [s for s in to_run if not s.needs_root]
    root_steps = [s for s in to_run if s.needs_root]

    if euid != 0:
        results.extend(_run_step(executor, step, as_user=None) for step in user_steps)
        root_subpass_rc = executor.run_root_subpass(args) if root_steps else None
        return SyncApplyReport(
            plan=plan,
            results=tuple(results),
            warnings=tuple(warnings),
            root_subpass_rc=root_subpass_rc,
        )

    results.extend(_run_step(executor, step, as_user=None) for step in root_steps)
    if user_steps:
        if not target_user:
            warnings.append("Skipped user-level sync steps because no invoking non-root user (SUDO_USER) was found.")
        else:
            results.extend(_run_step(executor, step, as_user=target_user) for step in user_steps)
    return SyncApplyReport(plan=plan, results=tuple(results), warnings=tuple(warnings))

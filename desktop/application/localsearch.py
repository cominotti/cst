# SPDX-License-Identifier: MPL-2.0
"""Application use cases for `cst desktop gnome localsearch`."""

from __future__ import annotations

from cst_core.domain.errors import Refused
from cst_core.domain.values import ExitAndLines
from cst_core.ports import CommandFailed, WriteFailed
from desktop.domain import LocalsearchAction, LocalsearchMode
from desktop.domain.localsearch import (
    KEY_MONITORS,
    KEY_RECURSIVE,
    KEY_SINGLE,
    LocalsearchPlan,
    LocalsearchSnapshot,
    build_localsearch_plan,
    format_gvariant_str_list,
    format_localsearch_plan,
    format_localsearch_status,
    render_localsearch_state_json,
    update_state_record_mode,
)
from desktop.ports import LocalsearchHostPort


def _require_ready(snap: LocalsearchSnapshot) -> None:
    if not snap.gsettings_available:
        raise Refused("gsettings is not installed; nothing changed")
    if not snap.schema_installed:
        raise Refused("org.freedesktop.Tracker3.Miner.Files schema is not installed; nothing changed")
    if snap.state_error:
        raise Refused(f"cannot read localsearch backup state ({snap.state_error}); fix or remove it first")


class LocalsearchService:
    """Orchestrates `status`, `plan`, `apply`, and `revert` for GNOME `localsearch-3`."""

    def __init__(self, host: LocalsearchHostPort, *, prog: str) -> None:
        self._host = host
        self._prog = prog

    def status_lines(self) -> list[str]:
        """Return `status` output lines."""
        return format_localsearch_status(self._host.read_snapshot())

    def _apply_plan(self, snap: LocalsearchSnapshot, plan: LocalsearchPlan) -> list[str]:
        lines = format_localsearch_plan(plan, self._prog, dry_run=False)
        if not plan.has_changes:
            return lines
        if plan.mode == LocalsearchMode.DISABLE and not snap.systemd_reachable:
            raise Refused("cannot reach user systemd manager (systemctl --user)")
        if plan.mode is not None:
            record = update_state_record_mode(snap, plan.mode)
            self._host.save_state(render_localsearch_state_json(record))
        if plan.unmask_units:
            self._host.unmask_units(plan.unmask_units)
        if plan.set_recursive is not None:
            self._host.set_key(KEY_RECURSIVE, format_gvariant_str_list(plan.set_recursive))
        if plan.set_single is not None:
            self._host.set_key(KEY_SINGLE, format_gvariant_str_list(plan.set_single))
        if plan.set_monitors is not None:
            self._host.set_key(KEY_MONITORS, "true" if plan.set_monitors else "false")
        for key in plan.reset_keys:
            self._host.reset_key(key)
        if plan.mask_units:
            self._host.mask_units(plan.mask_units)
        if plan.remove_state_file:
            self._host.remove_state()
        action_desc = (
            "Reverted GNOME localsearch-3 settings and unmasked units."
            if plan.action == LocalsearchAction.REVERT
            else (
                f"Applied GNOME localsearch-3 policy (mode={plan.mode.value if plan.mode is not None else 'restrict'})."
            )
        )
        lines.append(action_desc)
        return lines

    def run(self, action: LocalsearchAction, *, mode: LocalsearchMode = LocalsearchMode.RESTRICT) -> ExitAndLines:
        """Execute one `gnome localsearch` subcommand."""
        if action == LocalsearchAction.STATUS:
            return 0, self.status_lines()
        snap = self._host.read_snapshot()
        try:
            _require_ready(snap)
        except Refused as exc:
            return 1, [f"ERROR: {exc}"]
        plan = build_localsearch_plan(snap, mode=mode, revert=(action == LocalsearchAction.REVERT))
        if action == LocalsearchAction.PLAN:
            return 0, format_localsearch_plan(plan, self._prog, dry_run=True)
        try:
            return 0, self._apply_plan(snap, plan)
        except (Refused, WriteFailed, CommandFailed, OSError) as exc:
            return 1, [f"ERROR: {exc}"]

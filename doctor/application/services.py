# SPDX-License-Identifier: MPL-2.0
"""The services area of `cst doctor`: failed and crash-looping system and user units."""

from collections.abc import Iterator, Sequence
import shlex
from typing import Final

from cst_core.domain.text import first_line
from doctor.application.shared import check
from doctor.domain import INFO, TRANSIENT, WARN, Result, UnitScope, unit_results
from doctor.ports import CheckFn, DoctorHostPort

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []

UNIT_PROBLEM_STATES: Final = "failed,auto-restart,auto-restart-queued"
RESTARTING_SUBSTATES: Final[frozenset[str]] = frozenset({"auto-restart", "auto-restart-queued"})
_SYSTEMCTL_SUBSTATE_COL: Final = 3


def parse_problem_units(out: str) -> tuple[list[str], list[str]]:
    """Parse `systemctl list-units --state=failed,auto-restart,auto-restart-queued` into `(failed, restarting)`."""
    failed: list[str] = []
    restarting: list[str] = []
    for line in out.splitlines():
        fields = line.split()
        if not fields:
            continue
        unit = fields[0].lstrip("●*")
        if not unit:
            continue
        sub = fields[_SYSTEMCTL_SUBSTATE_COL] if len(fields) > _SYSTEMCTL_SUBSTATE_COL else ""
        if sub in RESTARTING_SUBSTATES:
            if unit not in restarting and unit not in failed:
                restarting.append(unit)
        elif unit not in failed:
            failed.append(unit)
            if unit in restarting:
                restarting.remove(unit)
    return failed, restarting


def restarting_unit_results(
    scope: UnitScope, restarting: Sequence[str], failed: Sequence[str] = ()
) -> Iterator[Result]:
    """Yield WARN/INFO results for units stuck in `auto-restart` without a misleading `reset-failed` fix."""
    journal = "journalctl --user" if scope == UnitScope.USER else "journalctl"
    real = [u for u in restarting if u not in failed and not TRANSIENT.match(u)]
    transient = [u for u in restarting if u not in failed and TRANSIENT.match(u)]
    if real:
        logs = " ".join(f"-u {shlex.quote(u)}" for u in real)
        yield Result(
            WARN,
            f"{scope} units restarting (crash loop?): {', '.join(real)}. Read why: {journal} -b {logs}",
        )
    if transient:
        yield Result(
            INFO,
            f"{scope}: {len(transient)} transient unit(s) restarting (systemd-run, low priority): {', '.join(transient)}",
        )


def problem_units(host: DoctorHostPort, user: bool) -> tuple[list[str] | None, list[str], str]:
    """Return `(failed, restarting, err)` from `systemctl list-units --state=...`."""
    cmd = (
        ["systemctl"]
        + (["--user"] if user else [])
        + ["list-units", f"--state={UNIT_PROBLEM_STATES}", "--no-legend", "--plain", "--full", "--no-pager"]
    )
    rc, out, err = host.run(cmd, timeout=30)
    if rc != 0:
        return None, [], first_line(err) or f"exit {rc}"
    failed, restarting = parse_problem_units(out)
    return failed, restarting, ""


def _scope_results(scope: UnitScope, failed: list[str], restarting: list[str]) -> Iterator[Result]:
    if failed or not restarting:
        yield from unit_results(scope, failed)
    yield from restarting_unit_results(scope, restarting, failed)


@check(CHECKS, "services")
def check_system_units(host: DoctorHostPort) -> Iterator[Result]:
    failed, restarting, err = problem_units(host, user=False)
    if failed is None:
        yield Result(WARN, f"system units: not checked (systemctl: {err})")
        return
    yield from _scope_results(UnitScope.SYSTEM, failed, restarting)


@check(CHECKS, "services")
def check_user_units(host: DoctorHostPort) -> Iterator[Result]:
    if host.uid == 0:
        yield Result(INFO, "user units: skipped (running as root; run it as your user)")
        return
    failed, restarting, err = problem_units(host, user=True)
    if failed is None:
        yield Result(WARN, f"user units: not checked (systemctl --user: {err})")
        return
    yield from _scope_results(UnitScope.USER, failed, restarting)

# SPDX-License-Identifier: MPL-2.0
"""The services area of `cst doctor`: failed system and user units."""

from collections.abc import Iterator

from cst_core.domain.text import first_line
from doctor.application.shared import check
from doctor.domain import INFO, WARN, Result, UnitScope, unit_results
from doctor.ports import CheckFn, DoctorHostPort

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []


def failed_units(host: DoctorHostPort, user: bool) -> tuple[list[str] | None, str]:
    cmd = (
        ["systemctl"]
        + (["--user"] if user else [])
        + ["list-units", "--state=failed", "--no-legend", "--plain", "--full", "--no-pager"]
    )
    rc, out, err = host.run(cmd, timeout=30)
    if rc != 0:
        return None, first_line(err) or f"exit {rc}"
    units: list[str] = []
    for line in out.splitlines():
        f = line.split()
        if f and f[0] not in units:
            units.append(f[0].lstrip("●*"))
    return [u for u in units if u], ""


@check(CHECKS, "services")
def check_system_units(host: DoctorHostPort) -> Iterator[Result]:
    units, err = failed_units(host, user=False)
    if units is None:
        yield Result(WARN, f"system units: not checked (systemctl: {err})")
        return
    yield from unit_results(UnitScope.SYSTEM, units)


@check(CHECKS, "services")
def check_user_units(host: DoctorHostPort) -> Iterator[Result]:
    if host.uid == 0:
        yield Result(INFO, "user units: skipped (running as root; run it as your user)")
        return
    units, err = failed_units(host, user=True)
    if units is None:
        yield Result(WARN, f"user units: not checked (systemctl --user: {err})")
        return
    yield from unit_results(UnitScope.USER, units)

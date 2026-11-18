# SPDX-License-Identifier: MPL-2.0
"""The checks of `cst doctor`, in their output order, and the run of them all."""

from collections.abc import Iterable, Iterator
from typing import Final

from doctor.application import alerts, btrfs, cpu, install, rpm_ostree, services, swap, vm
from doctor.application.install import check_install
from doctor.application.vm import check_vm_hardware, check_vm_image_dirs
from doctor.domain import ERROR, Result
from doctor.ports import CheckFn, DoctorHostPort, InstalledFilesPort, VmToolsPort

# The areas in the order the report prints them; an area's checks come in the order its module
# registers them (@check).
AREAS: Final = (rpm_ostree, services, alerts, btrfs, cpu, swap, vm, install)
CHECKS: list[tuple[str, CheckFn]] = [c for area in AREAS for c in area.CHECKS]


def wired_checks(vm: VmToolsPort, installed: InstalledFilesPort | None = None) -> list[tuple[str, CheckFn]]:
    """The registered checks, in order; the VM hardware and image-folder checks get vm, the install
    area the install records and this checkout (installed)."""

    def vm_hardware(host: DoctorHostPort) -> Iterator[Result]:
        return check_vm_hardware(host, vm_tools=vm)

    def vm_image_dirs(host: DoctorHostPort) -> Iterator[Result]:
        return check_vm_image_dirs(host, vm_tools=vm)

    def install(host: DoctorHostPort) -> Iterator[Result]:
        return check_install(host, installed=installed)

    wired: dict[CheckFn, CheckFn] = {}
    for fn, bound in ((check_vm_hardware, vm_hardware), (check_vm_image_dirs, vm_image_dirs), (check_install, install)):
        bound.__name__ = fn.__name__  # a crash names the check, as unwired
        wired[fn] = bound
    return [(area, wired.get(fn, fn)) for area, fn in CHECKS]


def run_checks(host: DoctorHostPort, checks: Iterable[tuple[str, CheckFn]] | None = None) -> list[tuple[str, Result]]:
    """[(area, Result)] in registry order. A check that crashes is an ERROR
    line, not a crash of the whole check-up."""
    out: list[tuple[str, Result]] = []
    for area, fn in CHECKS if checks is None else checks:
        try:
            for r in fn(host):
                out.append((area, r))
        except Exception as e:  # noqa: BLE001 - one broken check must not hide the others
            out.append((area, Result(ERROR, f"{fn.__name__}: the check itself failed ({type(e).__name__}: {e})")))
    return out

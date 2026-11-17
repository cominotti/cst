# SPDX-License-Identifier: MPL-2.0
"""The `cst doctor` command line: argv to the checks, the report to a stream.

It takes the Host (the machine, or the host seen from a toolbox) and the VM
tools from doctor/wiring.py, wires the checks that reuse the vm package,
runs them all and prints the report.
"""

from collections.abc import Iterator, Mapping, Sequence
import sys
from typing import TextIO

from doctor import application as app, wiring
from cst_core.domain.errors import Usage
from doctor.domain import NAME, Result, parse_args
from doctor.ports import CheckFn, DoctorHostPort, VmToolsPort
from doctor.presenter import render


def wired_checks(vm: VmToolsPort) -> list[tuple[str, CheckFn]]:
    """The registered checks, in order; the VM hardware and image-folder checks get vm."""

    def check_vm_hardware(host: DoctorHostPort) -> Iterator[Result]:
        return app.check_vm_hardware(host, vm_tools=vm)

    def check_vm_image_dirs(host: DoctorHostPort) -> Iterator[Result]:
        return app.check_vm_image_dirs(host, vm_tools=vm)

    wired: dict[CheckFn, CheckFn] = {app.check_vm_hardware: check_vm_hardware}
    wired[app.check_vm_image_dirs] = check_vm_image_dirs
    return [(area, wired.get(fn, fn)) for area, fn in app.CHECKS]


def main(
    argv: Sequence[str],
    *,
    doc: str,
    entry_file: str,
    out: TextIO | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run `cst doctor [--brief] | help`; exit 0 all ok/info, 1 a WARN or ERROR, 2 usage."""
    env = wiring.doctor_env() if env is None else env
    stdout = sys.stdout if out is None else out
    prog = env.get("CST_PROG") or NAME
    wiring.start_doctor()  # read-only, help too: `cst doctor | head` ends quietly
    try:
        cmd, brief = parse_args(argv)
    except Usage as e:
        print(f"{prog}: {e}\nusage: {prog} [--brief] | help", file=sys.stderr)
        return 2
    if cmd == "help":
        print(doc.replace("doctor [--brief]", f"{prog} [--brief]", 1), file=stdout)
        return 0
    host = wiring.host(env, entry_file)
    return render(app.run_checks(host, wired_checks(wiring.vm_tools())), brief, stdout, host.container, prog)

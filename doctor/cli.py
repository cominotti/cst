# SPDX-License-Identifier: MPL-2.0
"""The `cst doctor` command line: argv to the checks, the report to a stream.

It takes the Host (the machine, or the host seen from a toolbox), the VM
tools and the install records from doctor/wiring.py, hands them to the
checks that take them (application.wired_checks), runs them all and prints
the report.
"""

from collections.abc import Mapping, Sequence
import sys
from typing import TextIO

from doctor import application as app, wiring
from cst_core.domain.errors import Usage
from doctor.domain import NAME, parse_args
from doctor.presenter import render


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
    checks = app.wired_checks(wiring.vm_tools(), wiring.installed_files(entry_file))
    return render(app.run_checks(host, checks), brief, stdout, host.container, prog)

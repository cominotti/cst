"""The `cst doctor` command line: argv to the checks, the report to a stream.

A driving adapter: it builds the Host (the machine, or the host seen from a
toolbox), wires the checks that need the checkout's vm/cst-vm, runs them
all and prints the report.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
import functools
import os
import sys
from types import ModuleType
from typing import TextIO

from cst_core.adapters.runtime import checkout_root
from doctor import application as app
from doctor.adapters import Host, load_vm_module
from cst_core.domain.errors import Usage
from doctor.domain import NAME, Result, parse_args
from doctor.ports import CheckFn, DoctorHostPort
from doctor.presenter import render


def wired_checks(vm_mod_fn: Callable[[], ModuleType | None]) -> list[tuple[str, CheckFn]]:
    """The registered checks, in order; the VM hardware and image-folder checks get vm_mod_fn."""

    def check_vm_hardware(host: DoctorHostPort) -> Iterator[Result]:
        return app.check_vm_hardware(host, vm_mod_fn=vm_mod_fn)

    def check_vm_image_dirs(host: DoctorHostPort) -> Iterator[Result]:
        return app.check_vm_image_dirs(host, vm_mod_fn=vm_mod_fn)

    wired: dict[CheckFn, CheckFn] = {
        app.check_vm_hardware: check_vm_hardware,
        app.check_vm_image_dirs: check_vm_image_dirs,
    }
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
    env = os.environ if env is None else env
    stdout = sys.stdout if out is None else out
    prog = env.get("CST_PROG") or NAME
    try:
        cmd, brief = parse_args(argv)
    except Usage as e:
        print(f"{prog}: {e}\nusage: {prog} [--brief] | help", file=sys.stderr)
        return 2
    if cmd == "help":
        print(doc.replace("doctor [--brief]", f"{prog} [--brief]", 1), file=stdout)
        return 0
    host = Host(env, repo=checkout_root(entry_file) or "")
    vm_mod = functools.cache(lambda: load_vm_module(host.repo_path(app.VM_TOOL_SRC)))
    return render(app.run_checks(host, wired_checks(vm_mod)), brief, stdout, host.container, prog)

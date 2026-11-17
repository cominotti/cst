#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""cst module router: runs a command of a module whose setup script is a shim.

vm-setup.sh and toolchains-setup.sh run `python3 -I -B cst_core/route.py
MODULE ARGS...` with CST_SELF (the script's real path), CST_ARG0 (its $0)
and CST_LC_CTYPE (the caller's LC_CTYPE). The module's routing table
(ROUTING in cst_core/domain/dispatch.py) says which commands only read,
which change the caller's files and which need root; the router prints the
help or a usage error, or execs the program, flatpak-spawn, sudo or runuser
the way the bash scripts did.
"""

import os.path as _cst_path
import sys as _cst_sys

# CST bootstrap (the same in every entry point; tests/test_entrypoints.py checks
# it): from a checkout, cst_core and the module packages are in the folder above
# this file; inside an installed zipapp, sys.path already holds the archive.
_cst_root = _cst_path.dirname(_cst_path.dirname(_cst_path.realpath(__file__)))
if not _cst_path.isfile(_cst_path.dirname(__file__)) and _cst_root not in _cst_sys.path:
    _cst_sys.path.insert(0, _cst_root)

from collections.abc import Sequence
import sys

from cst_core import wiring


def main(argv: Sequence[str] | None = None) -> int:
    """Route `MODULE ARGS...`; the exit status when it does not exec (help, usage errors, refusals)."""
    wiring.start_router()
    args = list(sys.argv[1:] if argv is None else argv)
    router = wiring.module_router(args[0]) if args else None
    if router is None:
        print(
            "usage: route.py MODULE [ARGS...] (MODULE: a shim module of cst_core.domain.dispatch.ROUTING)",
            file=sys.stderr,
        )
        return 2
    return router.route(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())

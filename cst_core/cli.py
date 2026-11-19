#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""The top-level `cst` command: argv to the use case (cst_core/application/dispatch.py), built by cst_core/wiring.py."""

import os.path as _cst_path
import sys as _cst_sys

# CST bootstrap (the same in every entry point; tests/test_entrypoints.py checks
# it): from a checkout, cst_core and the module packages are in the folder above
# this file, and Python writes no bytecode there (no __pycache__ in the checkout,
# even for a program run by its shebang as root); inside an installed zipapp,
# sys.path already holds the archive.
_cst_root = _cst_path.dirname(_cst_path.dirname(_cst_path.realpath(__file__)))
if not _cst_path.isfile(_cst_path.dirname(__file__)) and _cst_root not in _cst_sys.path:
    _cst_sys.dont_write_bytecode = True
    _cst_sys.path.insert(0, _cst_root)

from collections.abc import Sequence
import sys

from cst_core import wiring


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `cst`."""
    wiring.start_cst()
    dispatcher = wiring.cst_dispatcher(fallback_root=_cst_root, stdout=sys.stdout, stderr=sys.stderr)
    return dispatcher.dispatch(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())

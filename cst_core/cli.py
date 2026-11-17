#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""The top-level `cst` command: wiring only (the use case is cst_core/application/dispatch.py)."""

import os.path as _cst_path
import sys as _cst_sys

# CST bootstrap (the same in every entry point; tests/test_entrypoints.py checks
# it): from a checkout, cst_core and the module packages are in the folder above
# this file; inside an installed zipapp, sys.path already holds the archive.
_cst_root = _cst_path.dirname(_cst_path.dirname(_cst_path.realpath(__file__)))
if not _cst_path.isfile(_cst_path.dirname(__file__)) and _cst_root not in _cst_sys.path:
    _cst_sys.path.insert(0, _cst_root)

from collections.abc import Sequence
import os
import sys

from cst_core.adapters.dispatch import LocalCstDispatchAdapter, ignore_quit
from cst_core.adapters.runtime import checkout_root, undo_python_start
from cst_core.application.dispatch import CstDispatcher


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for `cst`."""
    undo_python_start()
    ignore_quit()
    # What the cst wrapper hands over; popped, so the modules do not inherit it.
    repo_dir = os.environ.pop("CST_DIR", "") or checkout_root(__file__) or _cst_root
    self_path = os.environ.pop("CST_SELF", "") or os.path.join(repo_dir, "cst")
    arg0 = os.environ.pop("CST_ARG0", "") or (sys.argv[0] if sys.argv else "cst")
    dispatcher = CstDispatcher(
        port=LocalCstDispatchAdapter(),
        repo_dir=repo_dir,
        self_path=self_path,
        arg0=arg0,
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    return dispatcher.dispatch(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    raise SystemExit(main())

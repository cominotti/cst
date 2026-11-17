# SPDX-License-Identifier: MPL-2.0
"""The composition root of cst_core's programs: `cst` (cst_core/cli.py) and the installer.

What these programs take from the process lives here: the environment the
shell wrappers hand over, the signals Python changes at start, and the
adapters their use cases run on. The programs parse argv, call one use case
and print its answer.
"""

import os
import sys
from typing import TextIO

from cst_core.adapters.dispatch import LocalCstDispatchAdapter, ignore_quit
from cst_core.adapters.install import LocalInstallTarget
from cst_core.adapters.runtime import checkout_root, undo_python_start
from cst_core.adapters.zipapp import entry_payload
from cst_core.application.dispatch import CstDispatcher
from cst_core.application.install import InstallService


def start_cst() -> None:
    """Undo what Python changes at start (SIGPIPE, LC_CTYPE); ignore Ctrl-\\ as the bash cst did."""
    undo_python_start()
    ignore_quit()


def start_installer() -> None:
    """Undo what Python changes at start: a reader that stops early (`cmp -s <(installer.py payload ...)`)
    ends the installer quietly, and the commands it runs get the caller's LC_CTYPE."""
    undo_python_start()


def cst_dispatcher(*, fallback_root: str, stdout: TextIO, stderr: TextIO) -> CstDispatcher:
    """The `cst` use case over the host, with what the cst wrapper hands over.

    CST_DIR, CST_SELF and CST_ARG0 are popped, so the modules do not inherit
    them. Without CST_DIR: this checkout, else fallback_root (the bootstrap's root).
    """
    repo_dir = os.environ.pop("CST_DIR", "") or checkout_root(__file__) or fallback_root
    self_path = os.environ.pop("CST_SELF", "") or os.path.join(repo_dir, "cst")
    arg0 = os.environ.pop("CST_ARG0", "") or (sys.argv[0] if sys.argv else "cst")
    return CstDispatcher(
        port=LocalCstDispatchAdapter(),
        repo_dir=repo_dir,
        self_path=self_path,
        arg0=arg0,
        stdout=stdout,
        stderr=stderr,
    )


def install_service() -> InstallService | None:
    """The installer's use case over this machine's files, building payloads from this
    checkout (None: it runs from no checkout)."""
    root = checkout_root(__file__)
    if root is None:
        return None
    return InstallService(LocalInstallTarget(), lambda e: entry_payload(root, e))

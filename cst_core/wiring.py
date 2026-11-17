# SPDX-License-Identifier: MPL-2.0
"""The composition root of cst_core's programs: `cst` (cst_core/cli.py), the installer and the module router (route.py).

What these programs take from the process lives here: the environment the
shell wrappers hand over, the signals Python changes at start, and the
adapters their use cases run on. The programs parse argv, call one use case
and print its answer.
"""

import os
import sys
from typing import TYPE_CHECKING, TextIO

from cst_core.adapters.dispatch import LocalCstDispatchAdapter, default_ctrl_c, ignore_quit
from cst_core.adapters.runtime import checkout_root, undo_python_start
from cst_core.application.dispatch import CstDispatcher, ModuleRouter, RouteSettings
from cst_core.domain.dispatch import ROUTING

if TYPE_CHECKING:
    from cst_core.application.install import InstallService


def start_cst() -> None:
    """Undo what Python changes at start (SIGPIPE, LC_CTYPE); ignore Ctrl-\\ as the bash cst did."""
    undo_python_start()
    ignore_quit()


def start_router() -> None:
    """As cst: Python's SIGPIPE and LC_CTYPE undone (the program it execs gets the caller's), Ctrl-\\ ignored
    until the exec; and Ctrl-C kills it as it killed the bash script (no traceback)."""
    start_cst()
    default_ctrl_c()


def module_router(module: str) -> ModuleRouter | None:
    """The router of a shim module's commands (None: no such module, or no shim handed CST_SELF over),
    with what its shim hands over.

    CST_SELF (the shim's real path) and CST_ARG0 (its $0) are popped, so the
    program does not inherit them; CST_PROG, the module's container
    override and SUDO_USER are read, and stay as the caller set them.
    """
    routing = ROUTING.get(module)
    if routing is None:
        return None
    script = os.environ.pop("CST_SELF", "")
    if not script:
        return None
    arg0 = os.environ.pop("CST_ARG0", "") or script
    settings = RouteSettings(
        script=script,
        arg0=arg0,
        cst_prog=os.environ.get("CST_PROG", ""),
        container=os.environ.get(routing.container_override, "") if routing.container_override else "",
        sudo_user=os.environ.get("SUDO_USER", ""),
    )
    return ModuleRouter(port=LocalCstDispatchAdapter(), routing=routing, settings=settings)


def start_installer() -> None:
    """Undo what Python changes at start: a reader that stops early (`cmp -s <(installer.py payload ...)`)
    ends the installer quietly, and the commands it runs get the caller's LC_CTYPE."""
    undo_python_start()


def installer_identity() -> tuple[bool, str]:
    """(as root, the home of this account): whose record the installer writes generated text into."""
    from cst_core.adapters.runtime import process_identity

    return process_identity()


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
    checkout (None: it runs from no checkout). Its modules are imported here, so cst and the
    router do not load them."""
    from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget, SystemdUnitVerifier
    from cst_core.adapters.runtime import checkout_commit
    from cst_core.adapters.zipapp import entry_payload
    from cst_core.application.install import InstallService

    root = checkout_root(__file__)
    if root is None:
        return None
    return InstallService(
        LocalInstallTarget(),
        lambda e: entry_payload(root, e),
        record=JsonInstallRecord(),
        commit=checkout_commit(root),
        verifier=SystemdUnitVerifier(),
    )

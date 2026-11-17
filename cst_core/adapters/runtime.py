# SPDX-License-Identifier: MPL-2.0
"""Where a CST program finds itself, its siblings and its data: in a checkout or installed.

A python-entry runs two ways: from the checkout (`python3 -I btrfs/docker-gc`,
what the setup scripts and tests do), or installed as a zipapp (the
manifest's dest, what systemd units and timers run). These helpers give
the same answer in both, so no program computes paths from `__file__` by
itself.
"""

import os
from pathlib import Path
import signal
import zipfile

from cst_core.adapters.zipapp import build_python_zipapp
from cst_core.domain.install import expand_dest, manifest_by_src

SYSTEM_PYTHON = "/usr/bin/python3"


def running_archive(main_file: str) -> str | None:
    """The zipapp archive `main_file` (a `__file__`) runs from, or None in a checkout."""
    parent = os.path.dirname(os.path.abspath(main_file))
    return parent if os.path.isfile(parent) and zipfile.is_zipfile(parent) else None


def checkout_root(main_file: str) -> str | None:
    """The CST checkout an entry runs from (the folder above its own), or None when installed."""
    if running_archive(main_file) is not None:
        return None
    root = os.path.dirname(os.path.dirname(os.path.realpath(main_file)))
    return root if os.path.isfile(os.path.join(root, "cst_core", "__init__.py")) else None


def program_path(main_file: str) -> str:
    """The file to run to start this same program again (the archive when installed)."""
    return running_archive(main_file) or os.path.realpath(main_file)


def program_argv(main_file: str) -> list[str]:
    """The command that starts this same program again, on the system Python in isolated mode."""
    return [SYSTEM_PYTHON, "-I", "-B", program_path(main_file)]


def sibling_program(src_rel: str, main_file: str, home: str | None = None) -> str:
    """Another CST program: its checkout path from a checkout, else its installed dest."""
    root = checkout_root(main_file)
    if root is not None:
        return os.path.join(root, src_rel)
    return expand_dest(manifest_by_src()[src_rel].dest, home or os.path.expanduser("~"))


def install_payload(src_rel: str, main_file: str) -> bytes:
    """The bytes `install` writes for the python-entry `src_rel`, as seen by the program `main_file`.

    From a checkout: the zipapp built from it. Installed: the running archive
    itself (what was installed is, by definition, the current version).
    """
    archive = running_archive(main_file)
    if archive is not None:
        return Path(archive).read_bytes()
    root = checkout_root(main_file)
    if root is None:
        raise FileNotFoundError(f"{main_file}: neither in a CST checkout nor in a zipapp")
    if src_rel not in manifest_by_src():
        raise KeyError(f"{src_rel}: not in the install manifest")
    return build_python_zipapp(root, src_rel)


def _restore_lc_ctype(handed_over: str | None) -> None:
    """Put the caller's LC_CTYPE back into the environment that cst's modules get.

    Under a C or POSIX locale Python sets LC_CTYPE=C.UTF-8 in its own
    environment at start (PEP 538), and every program cst runs would inherit
    it; the bash cst passed the caller's LC_CTYPE on as it was. The cst
    wrapper hands it over as "" (it was unset) or "=VALUE"; None (cli.py run
    without the wrapper) leaves the environment as it is.
    """
    if handed_over is None:
        return
    if handed_over.startswith("="):
        os.environ["LC_CTYPE"] = handed_over[1:]
    else:
        os.environ.pop("LC_CTYPE", None)


def lc_ctype_handover() -> str:
    """The CST_LC_CTYPE=... setting that hands this process's LC_CTYPE to a
    CST program it starts again through sudo's `env` (the wrapper's form:
    "" when unset, "=VALUE" when set)."""
    value = os.environ.get("LC_CTYPE")
    return "CST_LC_CTYPE=" + ("" if value is None else "=" + value)


def undo_python_start() -> None:
    """Undo what Python changes at start, for the commands this program runs.

    SIGPIPE and SIGXFSZ go back to their default (restore_default_signals),
    and LC_CTYPE to the caller's, as a shell wrapper (cst, swap-setup.sh,
    cst-install.bash) hands it over in CST_LC_CTYPE (_restore_lc_ctype); the
    setting itself is removed. Used by cst, `cst swap` and the installer.
    """
    restore_default_signals()
    _restore_lc_ctype(os.environ.pop("CST_LC_CTYPE", None))


def restore_default_signals() -> None:
    """Undo what Python changes at start: SIGPIPE (and SIGXFSZ) ignored.

    With SIGPIPE at its default, cst stops silently when its reader goes away
    (`cst help | head -1`), as the bash cst did; and a module it execs starts
    with the signal at its default, which bash itself could not restore.
    """
    for sig in (signal.SIGPIPE, signal.SIGXFSZ):
        signal.signal(sig, signal.SIG_DFL)

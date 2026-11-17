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


def restore_default_signals() -> None:
    """Undo what Python changes at start: SIGPIPE (and SIGXFSZ) ignored.

    With SIGPIPE at its default, cst stops silently when its reader goes away
    (`cst help | head -1`), as the bash cst did; and a module it execs starts
    with the signal at its default, which bash itself could not restore.
    """
    for name in ("SIGPIPE", "SIGXFSZ"):
        sig = getattr(signal, name, None)
        if sig is not None:
            signal.signal(sig, signal.SIG_DFL)

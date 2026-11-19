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
import pwd
import signal
import stat
import zipfile

from cst_core.adapters.zipapp import build_python_zipapp
from cst_core.domain.install import expand_dest, manifest_by_src

SYSTEM_PYTHON = "/usr/bin/python3"


def running_archive(main_file: str) -> str | None:
    """The zipapp archive `main_file` (a `__file__`) runs from, or None in a checkout."""
    parent = os.path.dirname(os.path.abspath(main_file))
    return parent if os.path.isfile(parent) and zipfile.is_zipfile(parent) else None


def beside_program(name: str, started_as: str) -> str:
    """The program NAME in the folder of started_as (the path this program was run by, sys.argv[0]),
    not resolved: a copy in any folder pairs with the neighbour it was copied with (a shell front
    finds it the same way, by $(dirname "${BASH_SOURCE[0]}")), and errors name it as the shell
    does. "" when started_as is empty."""
    return os.path.join(os.path.dirname(os.path.abspath(started_as)), name) if started_as else ""


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


def restore_lc_ctype() -> None:
    """Only the LC_CTYPE part of undo_python_start: the caller's LC_CTYPE back from CST_LC_CTYPE
    (which goes), signals left as they are. For a program with cleanup (SIGPIPE stays ignored, so a
    closed pipe cannot cut it short) that a shell front hands LC_CTYPE over to."""
    _restore_lc_ctype(os.environ.pop("CST_LC_CTYPE", None))


def undo_python_start() -> None:
    """Undo what Python changes at start, for the commands this program runs.

    SIGPIPE and SIGXFSZ go back to their default (restore_default_signals),
    and LC_CTYPE to the caller's, as a shell wrapper (cst, swap-setup.sh,
    cpu-setup.sh, cst-install.bash) hands it over in CST_LC_CTYPE (_restore_lc_ctype); the
    setting itself is removed. Used by cst, `cst swap`, `cst cpu` and the installer.
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


def checkout_commit(root: str | None) -> str:
    """The commit the checkout at root has out (HEAD), read from .git by hand; "unknown" when it
    cannot be read (no checkout, an installed program, an unusual layout).

    No git command: as root in a user's checkout, git refuses to work
    (safe.directory). A worktree's .git file names its own git folder; its
    branches live in the common one (commondir); packed-refs holds the rest.

    The checkout belongs to its user while the installer may run as root, so
    every file is opened without following a link and read only when it is a
    regular file owned by the checkout's owner: a crafted .git (a link, a
    gitdir or ref pointing elsewhere) cannot make root copy another file's text
    into the world-readable install record.
    """
    if not root:
        return "unknown"
    try:
        owner = os.lstat(root).st_uid

        def read(path: str) -> str | None:
            return _read_owned(path, owner)

        git = os.path.join(root, ".git")
        if os.path.isfile(git) and not os.path.islink(git):
            line = (read(git) or "").strip()
            if not line.startswith("gitdir:"):
                return "unknown"
            git = os.path.normpath(os.path.join(root, line.split(":", 1)[1].strip()))
        common = git
        commondir = read(os.path.join(git, "commondir"))
        if commondir is not None:
            common = os.path.normpath(os.path.join(git, commondir.strip()))
        head = (read(os.path.join(git, "HEAD")) or "").strip()
        if not head.startswith("ref:"):
            return head if _is_sha(head) else "unknown"
        ref = head.split(":", 1)[1].strip()
        if not _is_ref_name(ref):
            return "unknown"
        for folder in (git, common):
            text = read(os.path.join(folder, ref))
            if text is not None:
                sha = text.strip()
                return sha if _is_sha(sha) else "unknown"
        for line in (read(os.path.join(common, "packed-refs")) or "").splitlines():
            sha, _, name = line.partition(" ")
            if name == ref and _is_sha(sha):
                return sha
    except OSError, UnicodeDecodeError:
        return "unknown"
    return "unknown"


def _read_owned(path: str, owner: int) -> str | None:
    """The text of path when it is a regular file owned by owner, opened without following a
    link at its last part; None otherwise (absent, a link, a folder, someone else's file)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != owner:
            return None
        with os.fdopen(fd, encoding="utf-8", closefd=False) as fh:
            return fh.read(1 << 20)
    finally:
        os.close(fd)


def _is_ref_name(ref: str) -> bool:
    """A branch ref as git writes it in HEAD (refs/heads/...): no absolute path, no '..' and no
    odd characters, so a crafted HEAD (the checkout belongs to the user, the installer runs as
    root) cannot make this read a file outside the git folder."""
    parts = ref.split("/")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-+@")
    return (
        len(parts) >= 3 and parts[0] == "refs" and all(p and p not in (".", "..") and set(p) <= allowed for p in parts)
    )


def _is_sha(text: str) -> bool:
    return len(text) in (40, 64) and all(c in "0123456789abcdef" for c in text)


def process_identity() -> tuple[bool, str]:
    """(running as root, this account's home folder from the password database): whose files and
    whose install record a process such as the installer writes. Not $HOME: runuser keeps root's."""
    euid = os.geteuid()
    return euid == 0, pwd.getpwuid(euid).pw_dir

# SPDX-License-Identifier: MPL-2.0
"""chrome-cache's machine side: the flags file reached without following symlinks, the app's install, cache sizes."""

from __future__ import annotations

import os
import stat
from btrfs.chrome_cache.domain import (
    APP,
    FLAGS_REL,
    OLD_CACHE_REL,
    SYSTEM_INSTALLS,
    USER_INSTALL_REL,
    ChromeFlagsState,
    decide,
    inspect,
    off_message,
    turn_off,
)
from cst_core.domain.errors import Refused

RUNTIME_DIR = f"/run/user/{os.getuid()}"


# ---- safe file access (as the user; no symlinks on the way) ------------------------------
def open_dir_chain(home: str, rel_dir: str, create: bool) -> int | None:
    """fd of HOME/REL_DIR walked component by component with O_NOFOLLOW (a
    symlink anywhere below HOME is refused). Missing folders are created when
    CREATE, else None is returned."""
    fd = os.open(os.path.realpath(home), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel_dir.split("/"):
            try:
                st = os.lstat(part, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    os.close(fd)
                    return None
                os.mkdir(part, 0o755, dir_fd=fd)
                st = os.lstat(part, dir_fd=fd)
            if stat.S_ISLNK(st.st_mode):
                raise Refused(f"~/{rel_dir}: '{part}' is a symlink (refused; nothing changed)")
            if not stat.S_ISDIR(st.st_mode):
                raise Refused(f"~/{rel_dir}: '{part}' is not a folder (refused; nothing changed)")
            nfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_flags(dfd: int, name: str) -> tuple[str, os.stat_result | None]:
    """(text, stat) of the flags file in DFD; ("", None) if absent."""
    try:
        st = os.lstat(name, dir_fd=dfd)
    except FileNotFoundError:
        return "", None
    if stat.S_ISLNK(st.st_mode):
        raise Refused(f"~/{FLAGS_REL} is a symlink (refused; nothing changed)")
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f"~/{FLAGS_REL} is not a regular file (refused; nothing changed)")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
    with os.fdopen(fd, "rb") as f:
        fst = os.fstat(f.fileno())
        data = f.read()
    if (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
        raise Refused(f"~/{FLAGS_REL} changed while it was read (nothing changed; run again)")
    return data.decode("utf-8", "surrogateescape"), fst


def write_flags(dfd: int, name: str, text: str, old_st: os.stat_result | None) -> None:
    """Atomic replace: a new file next to it, then rename. Refused if the file
    changed since it was read."""
    tmp = f".{name}.cst-new"
    try:
        os.unlink(tmp, dir_fd=dfd)
    except FileNotFoundError:
        pass
    mode = stat.S_IMODE(old_st.st_mode) if old_st else 0o644
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dfd)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("utf-8", "surrogateescape"))
            f.flush()
            os.fchmod(f.fileno(), mode)
            os.fsync(f.fileno())
        try:
            now = os.lstat(name, dir_fd=dfd)
        except FileNotFoundError:
            now = None
        same = (now is None and old_st is None) or (
            now is not None
            and old_st is not None
            and stat.S_ISREG(now.st_mode)
            and (now.st_ino, now.st_size, now.st_mtime_ns) == (old_st.st_ino, old_st.st_size, old_st.st_mtime_ns)
        )
        if not same:
            raise Refused(f"~/{FLAGS_REL} changed meanwhile (nothing changed; run again)")
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dfd)
        except FileNotFoundError:
            pass
        raise


# ---- facts about the app -------------------------------------------------------------------
def installed(home: str) -> str | None:
    """ "system", "user" or None."""
    for base in SYSTEM_INSTALLS:
        if os.path.isdir(os.path.join(base, "app", APP, "current", "active")):
            return "system"
    if os.path.isdir(os.path.join(home, USER_INSTALL_REL, "app", APP, "current", "active")):
        return "user"
    return None


def allocated_bytes(path: str) -> int:
    """Disk usage (allocated blocks) of a tree; symlinks not followed."""
    total = 0
    try:
        total += os.lstat(path).st_blocks * 512
    except OSError:
        return 0
    for root, dirs, files in os.walk(path, onerror=lambda e: None):
        for n in dirs + files:
            try:
                total += os.lstat(os.path.join(root, n)).st_blocks * 512
            except OSError:
                pass
    return total


def old_cache(home: str) -> tuple[int, int, int]:
    """(HTTP cache bytes, Code Cache bytes, profiles with an HTTP cache) in
    ~/.var/app/com.google.Chrome/cache/google-chrome/<profile>/."""
    top = os.path.join(home, OLD_CACHE_REL)
    http = code = 0
    profiles = 0
    try:
        names = sorted(os.listdir(top))
    except OSError:
        return 0, 0, 0
    for p in names:
        d = os.path.join(top, p)
        if os.path.islink(d) or not os.path.isdir(d):
            continue
        c = os.path.join(d, "Cache")
        if os.path.isdir(c) and not os.path.islink(c):
            profiles += 1
            http += allocated_bytes(c)
        cc = os.path.join(d, "Code Cache")
        if os.path.isdir(cc) and not os.path.islink(cc):
            code += allocated_bytes(cc)
    return http, code, profiles


def is_subvol(path: str) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_ino == 256


# ---- actions -------------------------------------------------------------------------------
def current(home: str, create: bool = False) -> tuple[int | None, str, os.stat_result | None]:
    """(dir fd or None, text, stat)."""
    dfd = open_dir_chain(home, os.path.dirname(FLAGS_REL), create)
    if dfd is None:
        return None, "", None
    try:
        text, st = read_flags(dfd, os.path.basename(FLAGS_REL))
    except BaseException:
        os.close(dfd)
        raise
    return dfd, text, st


def flags_text(home: str) -> str:
    """The text of the flags file ("" when it or its folder is missing); creates nothing."""
    dfd, text, _ = current(home, create=False)
    if dfd is not None:
        os.close(dfd)
    return text


def plan_line(home: str) -> str:
    if not installed(home):
        return "not installed: nothing to do"
    state, _, msg = decide(flags_text(home))
    return msg if state in (ChromeFlagsState.ABSENT, ChromeFlagsState.CAP_MISSING) else f"nothing to do ({msg})"


def apply(home: str) -> tuple[ChromeFlagsState, str, bool]:
    """(state, message, changed)."""
    if not installed(home):
        return ChromeFlagsState.NO_APP, "Chrome Flatpak not installed: nothing to do", False
    dfd, text, st = current(home, create=False)
    try:
        state, new, msg = decide(text)
        if new is None:
            return state, msg, False
        if dfd is None:
            dfd, text, st = current(home, create=True)
            assert dfd is not None
            state, new, msg = decide(text)
            if new is None:
                return state, msg, False
        write_flags(dfd, os.path.basename(FLAGS_REL), new, st)
        return state, msg, True
    finally:
        if dfd is not None:
            os.close(dfd)


def off(home: str) -> tuple[str, bool, bool]:
    """(message, changed, still_set): STILL_SET when cache flags remain."""
    dfd, text, st = current(home, create=False)
    try:
        new = turn_off(text)
        if new is None:
            return "already off: nothing changed", False, False
        if dfd is None:
            dfd, text, st = current(home, create=True)
            assert dfd is not None
            new = turn_off(text)
            if new is None:
                return "already off: nothing changed", False, False
        write_flags(dfd, os.path.basename(FLAGS_REL), new, st)
        left = inspect(new)
        return off_message(text, new), True, left["dir"] is not None or left["size"] is not None
    finally:
        if dfd is not None:
            os.close(dfd)


# ---- the user and the home -----------------------------------------------------------------
def real_home(home: str | None) -> str:
    """HOME given (or the caller's ~), symlinks resolved."""
    return os.path.realpath(home or os.path.expanduser("~"))


def owned_by_caller(home: str) -> bool:
    """HOME belongs to the calling user (OSError when it cannot be stat-ed)."""
    return os.stat(home).st_uid == os.getuid()


def ram_cache() -> str | None:
    """The cache folder in the sandbox /tmp (RAM) when it exists now, else None."""
    ram = os.path.join(RUNTIME_DIR, ".flatpak", APP, "tmp", "chrome-cache")
    return ram if os.path.isdir(ram) else None

# SPDX-License-Identifier: MPL-2.0
"""cst-pending-alerts' I/O: the pending files (trusted only), the record, the desktop's notifications."""

from collections.abc import Sequence
import io
import locale
import os
import stat
import sys
import time

from btrfs.pending_alerts.domain import (
    ICON,
    KEEP_DAYS,
    MAX_FILE,
    MAX_FILES,
    MAX_RECORD,
    NAME,
    TAG,
    Alert,
    Seen,
    PendingPaths,
    find_mtime,
    parse_alert,
    parse_delivered,
)
from cst_core.adapters.files import read_bytes, write_atomic
from cst_core.ports import CommandRunnerPort


def pass_bytes_through() -> None:
    """Titles and bodies reach the terminal byte for byte (bytes that are not UTF-8 too), as from bash."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="surrogateescape")


def use_callers_time_locale() -> None:
    """Dates in the caller's language, as date(1) prints them (C when that locale is missing)."""
    try:
        locale.setlocale(locale.LC_TIME, "")
    except locale.Error:
        pass


def trusted(path: str, owner: str) -> bool:
    """Owned by owner, not a symlink, not writable by group or others: nothing a user could plant."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return not stat.S_ISLNK(st.st_mode) and str(st.st_uid) == owner and st.st_mode & 0o022 == 0


def pending_files(s: PendingPaths) -> list[tuple[str, str]]:
    """The pending files of the last KEEP_DAYS, oldest first, as (mtime as find prints it, name):
    regular files with a hash name, at most MAX_FILE bytes, the newest MAX_FILES."""
    if not (trusted(s.alerts_dir, s.owner) and os.path.isdir(s.pending) and trusted(s.pending, s.owner)):
        return []
    now = time.time_ns()
    found: list[tuple[int, str]] = []
    try:
        entries = list(os.scandir(s.pending))
    except OSError:
        return []
    for e in entries:
        if not NAME.fullmatch(e.name):
            continue
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if e.is_file(follow_symlinks=False) and st.st_size <= MAX_FILE:
            if now - st.st_mtime_ns < KEEP_DAYS * 86400 * 10**9:
                found.append((st.st_mtime_ns, e.name))
    return [(find_mtime(ns), name) for ns, name in sorted(found)[-MAX_FILES:]]


def read_pending(s: PendingPaths, name: str) -> Alert | None:
    """The pending file name, if it is trusted and well formed."""
    path = f"{s.pending}/{name}"
    if not trusted(path, s.owner):
        return None
    data = read_bytes(path, limit=MAX_FILE)
    return None if data is None else parse_alert(data.decode("utf-8", "surrogateescape"))


def pending_exists(s: PendingPaths, name: str) -> bool:
    return os.path.isfile(f"{s.pending}/{name}")


def boot_time() -> int | None:
    """btime of /proc/stat: when the running boot started."""
    data = read_bytes("/proc/stat")
    for line in (data or b"").decode("ascii", "replace").split("\n"):
        f = line.split()
        if len(f) >= 2 and f[0] == "btime":
            return int(f[1]) if f[1].isascii() and f[1].isdigit() else None
    return None


def old_record(s: PendingPaths) -> bool:
    """Only the record of a version named f44-tools is there (read as it is until a login moves it)."""
    return not os.path.exists(s.state_dir) and os.path.isdir(s.old_state_dir) and not os.path.islink(s.old_state_dir)


def migrate_record(s: PendingPaths) -> bool:
    """Move the f44-tools record to the new place, when it is the only one; True when moved."""
    if not old_record(s):
        return False
    try:
        os.rename(s.old_state_dir, s.state_dir)
    except OSError:
        return False
    return True


def load_record(s: PendingPaths) -> dict[str, Seen]:
    """What this user saw of each pending file (from the f44-tools record while it is the only one)."""
    path = f"{s.old_state_dir}/delivered" if old_record(s) else s.record
    if not os.path.isfile(path) or os.path.islink(path):
        return {}
    data = read_bytes(path, limit=MAX_RECORD)
    return {} if data is None else parse_delivered(data.decode("utf-8", "surrogateescape"))


def save_record(s: PendingPaths, text: str) -> None:
    """Replace the record (folder 0700, file 0600). OSError when the folder cannot be made (the run then
    fails); a record that cannot be written or put in place is said on stderr and the old one stays, the
    run going on as the bash version's "mv ... || rm -f" did."""
    os.makedirs(s.state_dir, exist_ok=True)
    os.chmod(s.state_dir, 0o700)
    try:
        write_atomic(s.record, text, mode=0o600, durable=False, relabel=False)
    except OSError as e:
        print(f"{TAG}: cannot write {s.record}: {e.strerror}", file=sys.stderr)


def notification_server_up(runner: CommandRunnerPort) -> bool:
    """A notification server answers on the session bus."""
    r = runner.run(
        [
            "busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", "s", "org.freedesktop.Notifications",
        ]
    )  # fmt: skip
    return r.fault is None and r.returncode == 0 and "true" in r.stdout


def wait_for_notification_server(runner: CommandRunnerPort, wait: int) -> bool:
    """Started with the session: give the desktop's notification server up to wait seconds to come up."""
    waited = 0
    while not notification_server_up(runner):
        if waited >= wait:
            return False
        time.sleep(2)
        waited += 2
    return True


def show_notification(runner: CommandRunnerPort, urgency: str, app: str, title: str, body: str) -> bool:
    """One desktop notification (notify-send); True when it was shown."""
    argv: Sequence[str] = ["notify-send", "-u", urgency, "-a", app, "-i", ICON, "--", title, body]
    r = runner.run(argv, output="inherit")
    return r.fault is None and r.returncode == 0

# SPDX-License-Identifier: MPL-2.0
"""A CST desktop alert through btrfs-notify, and what happens when it cannot be sent.

btrfs-notify (the manifest's btrfs/btrfs-notify, component btrfs:alerts)
shows the alert in every graphical session, or keeps it in the pending
alerts folder for the next login. When btrfs-notify is not installed, does
not answer, or could do neither, the alert is not lost silently: a journal
line says so, and the alert is kept in the same folder in the same format
(the "alert-spool" state format), where cst doctor reports it and
cst-pending-alerts shows it at the next login. Only when that write fails
too is the journal line all that is left.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
import errno
import hashlib
import os
import posixpath
import re
import time

from cst_core.adapters.files import ensure_dir, write_atomic
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.locks import take_lock
from cst_core.domain.contracts import NOTIFY_PROGRAM, StateFormatKey, state_format
from cst_core.ports import CommandRunnerPort

_SPOOL = state_format(StateFormatKey.ALERT_SPOOL)
PENDING_ALERTS = _SPOOL.folder
_TIMEOUT = 60  # s: btrfs-notify waits up to 15 s per session and 30 s for its lock
_LOCK_WAIT = 30  # s: as btrfs-notify waits for the same lock


class Urgency(StrEnum):
    """An alert's urgency, as btrfs-notify -u takes it."""

    LOW = "low"
    NORMAL = "normal"
    CRITICAL = "critical"


class _Outcome(StrEnum):
    """What became of an alert."""

    SENT = "sent"  # btrfs-notify showed or kept it
    KEPT = "kept"  # CST kept it in its place
    LOST = "lost"  # only the journal has it


# As btrfs-notify bounds what it shows and keeps.
_MAX_APP, _MAX_TITLE, _MAX_BODY = 40, 200, 4000


def _bounded(urgency: Urgency, app: str, title: str, body: str) -> tuple[str, str, str, str]:
    """urgency, app, title and body as btrfs-notify normalizes them before it shows or keeps an alert."""
    app = re.sub(r"[^A-Za-z0-9._-]", "_", app)[:_MAX_APP] or "btrfs"
    title = re.sub(r"[\n\r\t]", " ", title)[:_MAX_TITLE] or f"{app} alert"
    return (Urgency.CRITICAL if urgency == Urgency.CRITICAL else Urgency.NORMAL), app, title, body[:_MAX_BODY]


def _pending_name(app: str, title: str) -> str:
    """The pending file of an alert, as btrfs-notify names it: one per app and title (a hash, no text in the path)."""
    return hashlib.sha256(f"{app}\n{title}".encode()).hexdigest()[:32] + ".alert"


def _earlier(path: str) -> tuple[int | None, int]:
    """(first, count) of an earlier occurrence kept at path ((None, 0) when there is none to merge)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None, 0
    with os.fdopen(fd, "rb") as f:
        head = f.read(16384).decode("utf-8", "replace")
    first, count = None, 0
    for line in head.splitlines():
        if not line:
            break
        if m := re.fullmatch(r"first=([0-9]{1,12})", line):
            first = int(m.group(1))
        elif m := re.fullmatch(r"count=([0-9]{1,9})", line):
            count = int(m.group(1))
    return first, count


def _keep_alert(pending: str, urgency: Urgency, app: str, title: str, body: str) -> str:
    """Keep an alert for the next login in pending, as btrfs-notify keeps it (merged with an earlier
    occurrence of the same alert, under the folder's lock); the file. Raises OSError."""
    level, app, title, body = _bounded(urgency, app, title, body)
    alerts = posixpath.dirname(pending)
    ensure_dir(pending, 0o755)
    lock = posixpath.join(alerts, ".lock")
    fd = take_lock(lock, wait=True, mode=0o644, timeout=_LOCK_WAIT)
    if fd is None:
        raise OSError(errno.EAGAIN, f"{lock} is still held after {_LOCK_WAIT} s")
    try:
        path = posixpath.join(pending, _pending_name(app, title))
        stamp = int(time.time())
        first, count = _earlier(path)
        text = (
            f"cst-alert {_SPOOL.version}\nurgency={level}\napp={app}\nfirst={stamp if first is None else first}\n"
            f"last={stamp}\ncount={count + 1}\ntitle={title}\n\n{body}\n"
        )
        write_atomic(path, text, mode=0o644, relabel=False)
    finally:
        os.close(fd)
    return path


@dataclass(frozen=True, slots=True)
class Notifier:
    """btrfs-notify at program, with the pending alerts folder for when it cannot be sent.

    log receives (priority, message) journal lines; by default "<prio>APP: message" on stdout.
    """

    runner: CommandRunnerPort
    program: str = NOTIFY_PROGRAM
    pending: str = PENDING_ALERTS
    log: Callable[[int, str], None] | None = None

    def send(self, urgency: Urgency, app: str, title: str, body: str) -> _Outcome:
        """Show the alert (or have btrfs-notify keep it); when that fails, keep it here and say so."""
        r = self.runner.run([self.program, "-u", urgency, "-a", app, title, body], timeout=_TIMEOUT, new_session=True)
        if r.fault is None and r.returncode == 0:
            return _Outcome.SENT
        if r.fault == "missing":
            why = f"{self.program} is not installed"
        elif r.fault == "timeout":
            why = f"{self.program} did not answer within {_TIMEOUT} s"
        elif r.fault is not None:
            why = f"{self.program} cannot run ({r.cause or r.fault})"
        else:
            why = f"{self.program} exited {r.returncode}"
        say = self.log or (lambda prio, msg: journal_log(prio, msg, tag=app))
        try:
            path = _keep_alert(self.pending, urgency, app, title, body)
        except (OSError, ValueError) as e:  # ValueError: a folder that is no plain path, a title no file holds
            say(3, f"alert not delivered ({why}) and not kept ({e}): {title}; it is only in this journal")
            return _Outcome.LOST
        say(3, f"alert not delivered ({why}): {title}; kept in {path} (cst doctor shows it)")
        return _Outcome.KEPT

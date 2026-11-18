# SPDX-License-Identifier: MPL-2.0
"""cst-pending-alerts' rules (pure): the alert files btrfs-notify keeps, the
record of what this user has seen, and what one login shows.

A pending alert ("alert-spool" in cst_core/domain/contracts.py) is a file
named after a hash, in /var/lib/cst-alerts/pending:

    cst-alert 1                  (versions named f44-tools: "f44-alert 1")
    urgency=critical|normal
    app=NAME
    first=EPOCH
    last=EPOCH
    count=N
    title=TEXT

    BODY

The record (~/.local/state/cst-alerts/delivered) has one line per pending
file this user had at a login: "<file name> <mtime> <shown|summarized>
<count> <first>", the mtime as GNU find's %T@ prints it (ten decimals). A
copy of another version of CST reads and writes the same record.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import posixpath
import re
import time
from typing import Final

from cst_core.domain.contracts import state_format
from cst_core.domain.text import file_lines, read_fields

_SPOOL: Final = state_format("alert-spool")

TAG: Final = "cst-pending-alerts"
USAGE: Final = "usage: cst-pending-alerts [--list]"
MAX_FILE: Final = 16384  # bytes; btrfs-notify writes far less
MAX_FILES: Final = 50
MAX_SHOWN: Final = 8  # at one login; older normal ones are summed up in one notification
KEEP_DAYS: Final = 7  # same as btrfs-notify
MAX_RECORD: Final = 65536  # bytes of the record read
ICON: Final = "drive-harddisk"
NAME: Final = re.compile(r"[0-9a-f]{32}\.alert")
SUMMARY_TITLE: Final = "{n} more storage alert(s) while nobody was logged in"
SUMMARY_BODY: Final = (
    "Older and not critical (critical ones are always shown). Read them: cst-pending-alerts --list "
    "(or cst btrfs status)."
)
MARKS: Final[Mapping[str, str]] = {
    "shown": "  [shown to you]",
    "summarized": "  [only counted in the summary at login; text not shown]",
    "": "  [NOT shown to you yet: next login]",
}
_HEADERS: Final = (f"cst-alert {_SPOOL.version}", "f44-alert 1")  # f44-tools wrote the second
_APP: Final = re.compile(r"[A-Za-z0-9._-]{1,40}")
_EPOCH: Final = re.compile(r"[0-9]{1,12}")
_COUNT: Final = re.compile(r"[0-9]{1,9}")
_MTIME: Final = re.compile(r"[0-9]{1,12}(\.[0-9]{1,10})?")


@dataclass(frozen=True, slots=True)
class PendingPaths:
    """Where the alerts and the record are, from the environment (tests point them elsewhere)."""

    alerts_dir: str
    owner: str  # uid the alert folders and files must belong to, as stat prints it
    wait: int  # seconds to wait for the desktop's notification server
    state_dir: str
    old_state_dir: str  # where versions named f44-tools kept the record

    @property
    def pending(self) -> str:
        return f"{self.alerts_dir}/pending"

    @property
    def record(self) -> str:
        return f"{self.state_dir}/delivered"


def alert_paths(environ: Mapping[str, str]) -> PendingPaths | str:
    """The settings from the environment, or why there are none (HOME unset)."""
    state = environ.get("XDG_STATE_HOME") or None
    if state is None:
        home = environ.get("HOME")
        if home is None:
            return "HOME: unbound variable"
        state = f"{home}/.local/state"
    wait = environ.get("CST_ALERTS_WAIT") or "90"
    return PendingPaths(
        alerts_dir=environ.get("CST_ALERTS_DIR") or posixpath.dirname(_SPOOL.folder),
        owner=environ.get("CST_ALERTS_OWNER") or "0",
        wait=int(wait) if wait.isascii() and wait.isdigit() else 0,
        state_dir=f"{state}/cst-alerts",
        old_state_dir=f"{state}/f44-alerts",
    )


@dataclass(frozen=True, slots=True)
class Alert:
    """One pending alert, its fields checked and bounded."""

    urgency: str
    app: str
    title: str
    body: str
    first: int
    last: int
    count: int


def parse_alert(text: str) -> Alert | None:
    """The alert in a pending file's text (its first MAX_FILE bytes), or None for anything unexpected."""
    lines = file_lines(text.replace("\0", ""))  # bash's read dropped every NUL byte
    if not lines or lines[0] not in _HEADERS:
        return None
    head: dict[str, str] = {}
    body: list[str] = []
    in_head = True
    for line in lines[1:]:
        if in_head:
            if not line:
                in_head = False
                continue
            key, sep, val = line.partition("=")
            head[key] = val if sep else line
        else:
            body.append(line)
    urgency, app, title = head.get("urgency", ""), head.get("app", ""), head.get("title", "")
    first, last, count = head.get("first", "0"), head.get("last", "0"), head.get("count", "1")
    if urgency not in ("critical", "normal") or not _APP.fullmatch(app):
        return None
    if not (_EPOCH.fullmatch(first) and _EPOCH.fullmatch(last) and _COUNT.fullmatch(count)) or not title:
        return None
    return Alert(urgency, app, title[:200], "\n".join(body).rstrip("\n")[:4000], int(first), int(last), int(count))


@dataclass(frozen=True, slots=True)
class Seen:
    """What this user had of one pending file at a login."""

    mtime: str  # the file's mtime then (whole seconds in records of the first version)
    how: str  # shown or summarized
    count: int  # its count then (0: unknown)
    first: int  # its first occurrence then (0: unknown)

    def line(self, name: str) -> str:
        return f"{name} {self.mtime} {self.how} {self.count} {self.first}"


def parse_delivered(text: str) -> dict[str, Seen]:
    """The record: name -> what was seen, the newest entry of each name. Only whole lines count."""
    rec: dict[str, Seen] = {}
    for line in text.replace("\0", "").split("\n")[:-1]:  # bash's read dropped every NUL byte
        name, mtime, how, count, first = read_fields(line, 5)
        if not NAME.fullmatch(name) or not _MTIME.fullmatch(mtime):
            continue
        # Lines of the first version had only "<name> <mtime>": shown, count unknown.
        seen = Seen(
            mtime,
            "summarized" if how == "summarized" else "shown",
            int(count) if _COUNT.fullmatch(count) else 0,
            int(first) if _EPOCH.fullmatch(first) else 0,
        )
        # Several lines for one file only in records of the first version.
        old = rec.get(name)
        if old is None or int(mtime.split(".")[0]) >= int(old.mtime.split(".")[0]):
            rec[name] = seen
    return rec


def seen_as(rec: Mapping[str, Seen], name: str, mtime: str) -> str:
    """ "shown" or "summarized" if this user had this very version of the file at a login; "" otherwise."""
    seen = rec.get(name)
    if seen is None:
        return ""
    # Full precision: a repeat kept within the same second is a new version.
    # Records of the first version had whole seconds only.
    same = seen.mtime == mtime if "." in seen.mtime else seen.mtime == mtime.split(".")[0]
    return seen.how if same else ""


def find_mtime(mtime_ns: int) -> str:
    """An mtime as GNU find's %T@ prints it (and the record keeps it): ten decimals."""
    return f"{mtime_ns // 10**9}.{mtime_ns % 10**9:09d}0"


def happened_at(epoch: int, btime: int | None) -> str:
    """ "Mon 13 Oct 03:12", or "at boot, Mon 13 Oct 03:12" within the first 10 minutes of the running
    boot (the units that run before the login). In the time locale in effect (as date(1))."""
    text = time.strftime("%a %d %b %H:%M", time.localtime(epoch))
    return f"at boot, {text}" if btime is not None and 0 <= epoch - btime < 600 else text


def login_text(alert: Alert, seen: Seen | None, at: Callable[[int], str]) -> str:
    """The text shown: when it happened, then the original body. If this user saw an earlier version
    of the same file (same first occurrence), only the occurrences since then are counted."""
    text = ""
    if seen is not None and seen.how == "shown" and seen.first == alert.first and 0 < seen.count < alert.count:
        new = alert.count - seen.count
        if new == 1:
            text = f"Happened again {at(alert.last)}, while no desktop session could show it."
        else:
            text = (
                f"Happened {new} more times since you last saw it, while no desktop session could show it: "
                f"last {at(alert.last)}."
            )
    if not text:
        if alert.count > 1:
            text = (
                f"Happened {alert.count} times while no desktop session could show it: first {at(alert.first)}, "
                f"last {at(alert.last)}."
            )
        else:
            text = f"Happened {at(alert.last)}, while no desktop session could show it."
    return f"{text}\n\n{alert.body}".rstrip("\n")


def list_line(alert: Alert, at: Callable[[int], str], mark: str) -> str:
    """One line of --list."""
    times = f" ({alert.count} times)" if alert.count > 1 else ""
    return f"{at(alert.last)}  {alert.urgency:<8} {alert.app}: {alert.title}{times}{mark}"


def plan_login(urgencies: Sequence[str]) -> tuple[list[int], list[int]]:
    """(summed up, shown one by one) indexes of the alerts of one login, oldest first: every critical
    one, then the newest normal ones up to MAX_SHOWN; older normal ones go into one summary."""
    crit = [i for i, u in enumerate(urgencies) if u == "critical"]
    norm = [i for i, u in enumerate(urgencies) if u != "critical"]
    room = max(MAX_SHOWN - len(crit), 0)
    nskip = len(norm) - room if len(norm) > room else 0
    return norm[:nskip], sorted(crit + norm[nskip:])


def record_text(rec: Mapping[str, Seen], still_there: Callable[[str], bool]) -> str:
    """The record rewritten: one line per pending file that is still there (the others are gone for
    good, so the record stays small), sorted."""
    return "".join(f"{line}\n" for line in sorted(s.line(n) for n, s in rec.items() if still_there(n)))

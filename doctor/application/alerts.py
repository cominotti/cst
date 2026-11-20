# SPDX-License-Identifier: MPL-2.0
"""The alerts area of `cst doctor`: pending alerts, the program the alerts go through, f44-tools' leftovers."""

from collections.abc import Iterator
import os
import time

from cst_core.domain.contracts import Urgency
from cst_core.domain.values import DAY
from doctor.application.shared import check, refresh, runs
from doctor.domain import (
    ALERT_KEEP_DAYS,
    ALERT_MAX_FILE,
    ALERT_NAME,
    ALERT_PENDING,
    ALERTING_UNITS,
    KEEPING_UNITS,
    ERROR,
    INFO,
    NOTIFY_HOST,
    PENDING_READER_HOST,
    OK,
    WARN,
    HostPath,
    Result,
    mtime_matches_ns,
    parse_alert_text,
    view_join,
    ViewPath,
)
from doctor.ports import CheckFn, DoctorHostPort

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []


def read_alert(host: DoctorHostPort, path: ViewPath) -> tuple[str, str, int] | None:
    """(urgency, title, last) of a pending alert file, or None."""
    try:
        with host.open_bytes(path) as f:
            data = f.read(ALERT_MAX_FILE).decode("utf-8", "replace")
    except OSError:
        return None
    return parse_alert_text(data)


def seen_record(host: DoctorHostPort) -> dict[str, str]:
    """name -> recorded mtime string, from cst-pending-alerts' record of what
    this user saw (the f44-tools one while the new one does not exist)."""
    new = view_join(host.state_home, "cst-alerts")
    old = view_join(host.state_home, "f44-alerts")
    d = old if not host.exists(new) and host.isdir(old) and not host.islink(old) else new
    rec: dict[str, str] = {}
    try:
        with host.open_text(view_join(d, "delivered")) as f:
            for line in f:
                p = line.split()
                if len(p) >= 2 and ALERT_NAME.match(p[0]):
                    rec[p[0]] = p[1]
    except OSError:
        pass
    return rec


def mtime_matches(recorded: str, st: os.stat_result) -> bool:
    """As cst-pending-alerts: full precision when the record has it."""
    return mtime_matches_ns(recorded, st.st_mtime_ns)


def _sort_alerts(
    host: DoctorHostPort, pending: ViewPath, names: list[str], seen: dict[str, str]
) -> tuple[list[tuple[str, str, int]], int]:
    """([(urgency, title, last) of each alert this user has not seen], how many were seen), of the
    pending alerts named: files only (no link), younger than ALERT_KEEP_DAYS, that parse."""
    unseen: list[tuple[str, str, int]] = []
    seen_n = 0
    for n in names:
        p = view_join(pending, n)
        try:
            st = host.lstat(p)
        except OSError:
            continue
        if not host.isfile(p) or host.islink(p) or host.now - st.st_mtime > ALERT_KEEP_DAYS * DAY:
            continue
        a = read_alert(host, p)
        if a is None:
            continue
        if n in seen and mtime_matches(seen[n], st):
            seen_n += 1
        else:
            unseen.append(a)
    return unseen, seen_n


def _unseen_result(host: DoctorHostPort, alert: tuple[str, str, int], *, reader: bool) -> Result:
    """One alert not seen yet (reader: cst-pending-alerts is installed, so a login shows it)."""
    urgency, title, last = alert
    when = time.strftime("%a %d %b %H:%M", time.localtime(last)) if last else "?"
    return Result(
        ERROR if urgency == Urgency.CRITICAL else WARN,
        f"alert you have not seen yet ({urgency}, {when}): {title} ("
        + (
            f"shown at your next login; the fix shows it now, as does {host.cst} btrfs status)"
            if reader
            else f"no login shows it while {PENDING_READER_HOST} is missing: the fix installs it)"
        ),
        "cst-pending-alerts --list" if reader else refresh(host, "btrfs:alerts"),
    )


@check(CHECKS, "alerts")
def check_pending_alerts(host: DoctorHostPort) -> Iterator[Result]:
    pending = host.path(ALERT_PENDING)
    if not host.isdir(pending):
        yield Result(INFO, f"no alert folder {ALERT_PENDING} (installed by {host.cst} btrfs install)")
        return
    try:
        names = sorted(n for n in host.listdir(pending) if ALERT_NAME.match(n))
    except OSError as e:
        yield Result(WARN, f"pending alerts: not readable ({e.strerror})")
        return
    unseen, seen_n = _sort_alerts(host, pending, names, seen_record(host))
    reader = runs(host, PENDING_READER_HOST)
    for alert in sorted(unseen, key=lambda a: a[2]):
        yield _unseen_result(host, alert, reader=reader)
    if seen_n:
        yield Result(
            INFO,
            f"{seen_n} alert(s) of the last {ALERT_KEEP_DAYS} days already shown to you (cst-pending-alerts --list)",
        )
    if not unseen and not seen_n:
        yield Result(OK, "no pending alerts")


@check(CHECKS, "alerts")
def check_alert_program(host: DoctorHostPort) -> Iterator[Result]:
    """WARN when installed units alert through btrfs-notify but it is missing (KEEPING_UNITS: kept for this doctor)."""
    units = [os.path.basename(u) for u in ALERTING_UNITS if host.exists(host.path(u))]
    if not units or runs(host, NOTIFY_HOST):
        return
    kept = ", ".join(u for u in units if u in KEEPING_UNITS)
    yield Result(
        WARN,
        f"{', '.join(units)} alert through {NOTIFY_HOST}, which is missing: their alerts reach no desktop, only the "
        "journal" + (f"; {kept}: also the pending alerts above" if kept else ""),
        refresh(host, "btrfs:alerts"),
    )


@check(CHECKS, "alerts")
def check_legacy_alerts(host: DoctorHostPort) -> Iterator[Result]:
    old = host.path(HostPath("/var/lib/f44-alerts"))
    if host.islink(old):
        yield Result(
            WARN,
            "/var/lib/f44-alerts: link left by the move from f44-tools to CST (the fix removes it)",
            f"{host.cst} btrfs install",
        )
    elif host.exists(old):
        yield Result(
            WARN,
            "/var/lib/f44-alerts: folder of the versions named f44-tools still there "
            "(the fix moves its alerts to /var/lib/cst-alerts and removes it)",
            f"{host.cst} btrfs install",
        )
    else:
        yield Result(OK, "no leftovers of f44-tools in /var/lib")

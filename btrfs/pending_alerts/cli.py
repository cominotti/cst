# SPDX-License-Identifier: MPL-2.0
"""cst-pending-alerts' commands (show at a login, --list) and main()."""

from collections.abc import Sequence
from functools import partial
import os
import sys

from btrfs.pending_alerts.adapters import (
    boot_time,
    load_record,
    migrate_record,
    pass_bytes_through,
    pending_exists,
    pending_files,
    read_pending,
    save_record,
    show_notification,
    use_callers_time_locale,
    wait_for_notification_server,
)
from btrfs.pending_alerts.domain import (
    MARKS,
    SUMMARY_BODY,
    SUMMARY_TITLE,
    TAG,
    USAGE,
    Alert,
    PendingPaths,
    Seen,
    SeenHow,
    alert_paths,
    happened_at,
    list_line,
    login_text,
    plan_login,
    record_text,
    seen_as,
)
from cst_core.adapters.dispatch import default_ctrl_c
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import restore_default_signals
from cst_core.domain.contracts import Urgency
from cst_core.ports import CommandRunnerPort


def log(prio: int, msg: str) -> None:
    journal_log(prio, msg, tag=TAG, stream=sys.stderr)


def list_alerts(s: PendingPaths) -> int:
    """Every pending alert, and whether this user has seen it (cst btrfs status). Changes nothing."""
    root = HostExecutionContext().is_root()
    btime = boot_time()
    rec = {} if root else load_record(s)
    at = partial(happened_at, btime=btime)
    n = 0
    for mtime, name in pending_files(s):
        alert = read_pending(s, name)
        if alert is None:
            continue
        n += 1
        mark = "" if root else MARKS[seen_as(rec, name, mtime)]
        print(list_line(alert, at, mark))
    if not n:
        print("none")
    return 0


def show_alerts(s: PendingPaths, runner: CommandRunnerPort) -> int:
    """At a login: show the alerts this user has not seen yet, then record them."""
    if migrate_record(s):
        log(6, f"moved {s.old_state_dir} to {s.state_dir}")
    rec = load_record(s)
    at = partial(happened_at, btime=boot_time())

    todo: list[tuple[str, str, Alert]] = []  # oldest first: (mtime, name, alert as first read)
    for mtime, name in pending_files(s):
        if seen_as(rec, name, mtime) is not None:
            continue
        alert = read_pending(s, name)
        if alert is None:
            log(4, f"{name}: not a valid pending alert; skipped")
            continue
        todo.append((mtime, name, alert))
    if not todo:
        return 0
    if not wait_for_notification_server(runner, s.wait):
        log(4, f"no notification server after {s.wait} s; trying at the next login")
        return 0
    skip, pick = plan_login([a.urgency for _, _, a in todo])
    done: dict[str, Seen] = {}
    if skip and show_notification(runner, Urgency.NORMAL, TAG, SUMMARY_TITLE.format(n=len(skip)), SUMMARY_BODY):
        for i in skip:
            mtime, name, alert = todo[i]
            done[name] = Seen(mtime, SeenHow.SUMMARIZED, alert.count, alert.first)
    for i in pick:
        mtime, name, first_read = todo[i]
        alert = read_pending(s, name)
        if alert is None:
            log(4, f"{name}: not a valid pending alert; skipped")
            continue
        if show_notification(runner, alert.urgency, alert.app, alert.title, login_text(alert, rec.get(name), at)):
            done[name] = Seen(mtime, SeenHow.SHOWN, first_read.count, first_read.first)
            log(5, f"shown: {alert.app}: {alert.title} (last {at(alert.last)})")
    if not done:
        return 0
    try:
        save_record(s, record_text({**rec, **done}, lambda name: pending_exists(s, name)))
    except OSError as e:
        print(f"{TAG}: cannot write {s.record}: {e.strerror}", file=sys.stderr)
        return 1
    return 0


def main(argv: Sequence[str]) -> int:
    pass_bytes_through()
    settings = alert_paths(os.environ)
    if isinstance(settings, str):
        print(f"{TAG}: {settings}", file=sys.stderr)
        return 1
    mode = argv[1] if len(argv) > 1 else ""
    if mode == "--list":
        restore_default_signals()  # only reads: a closed pipe ends it quietly
        use_callers_time_locale()
        return list_alerts(settings)
    if mode:
        print(USAGE, file=sys.stderr)
        return 2
    default_ctrl_c()
    use_callers_time_locale()
    return show_alerts(settings, SubprocessCommandRunner())

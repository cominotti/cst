# SPDX-License-Identifier: MPL-2.0
"""hourly, prune and purge of btrfs-snapshot, under /run/btrfs-snapshot.lock: the space guard, the
waits, btrfs-snapshot-core's runs and their alerts, as btrfs/btrfs-snapshot (bash) had them."""

from collections.abc import Sequence
import glob
import json
import os
import re
import sys

from btrfs.health_check.domain import bash_int
from btrfs.snapshot_runtime.adapters.machine import SnapshotRun
from btrfs.snapshot_runtime.domain import (
    CORE_BLOCKED,
    CORE_LOCK_BUSY,
    DAY_S,
    GIB,
    NOT_SET_UP,
    PURGE_BATCH,
    SHOWN,
    AlertKind,
    CoreCommand,
    Stderr,
    Urgency,
    blocked_alert,
    core_reason,
    head_spaced,
    nonempty_count,
    plan_entries,
    read_lines,
    source_args,
)

NL = "\n"
LOCK_BUSY = "another conversion/snapshot run is active; skipped"
PRUNE_USAGE = "usage: btrfs-snapshot prune [--dry-run] [--allow-mass-delete]"


# ---- alerts ------------------------------------------------------------------------------------
def send_alert(m: SnapshotRun, kind: AlertKind, urgency: Urgency, title: str, body: str) -> None:
    """A desktop alert at most once a day per kind (see the bash's alert): btrfs-notify shows it now
    or keeps it for the next login; only when it could do neither does the stamp stay as it was."""
    stamp = f"{m.state}/alerted-{kind}"
    m.log(4, f"{title}: {body}")
    if os.path.exists(stamp):
        made = m.mtime(stamp)
        if made is not None and m.now() - made < DAY_S:
            return
    if os.access(m.notify, os.X_OK):
        res = m.run([m.notify, "-u", urgency, "-a", "btrfs-snapshot", title, body])
        if res.fault is None and res.returncode == 0:
            m.touch(stamp)


def clear_alert(m: SnapshotRun, kind: AlertKind) -> None:
    m.rm_f(f"{m.state}/alerted-{kind}")


def waiting(m: SnapshotRun, kind: AlertKind, msg: str, title: str, body: str) -> None:
    """No snapshot this hour (logged); after a day of waiting, an alert. STATE/KIND-since: the start."""
    since = f"{m.state}/{kind}-since"
    if not os.path.exists(since):
        m.touch(since)
    m.log(5, f"{msg}; no snapshot this hour")
    made = m.mtime(since)
    if made is not None and m.now() - made > DAY_S:
        send_alert(m, kind, Urgency.NORMAL, title, body)


# ---- btrfs-snapshot-core ---------------------------------------------------------------------
class CoreRun:
    """One run of the core: its exit status and CORE_REASON (its line naming a block, else its first
    warning or error), its lines passed on as they come."""

    def __init__(self, m: SnapshotRun, cmd: CoreCommand, args: Sequence[str]) -> None:
        lines: list[str] = []

        def relay(line: str) -> None:
            lines.append(line)
            print(line, flush=True)

        res = m.run([*m.core_common(cmd), *args], output="stdout-show-stderr", on_line=relay)
        self.rc = m.rc(res)
        self.reason = core_reason(lines)


def core_result(m: SnapshotRun, what: str, run: CoreRun) -> bool:
    """Alerts for the core's exit status; True only for 0."""
    match run.rc:
        case 0:
            clear_alert(m, AlertKind.BLOCKED)
            return True
        case 3:
            m.log(5, f"{what}: another run holds {m.lock_path}; skipped")
        case 4:
            title, body = blocked_alert(run.reason, m.conf)
            send_alert(m, AlertKind.BLOCKED, Urgency.NORMAL, title, body)
        case _:
            m.log(3, f"{what}: btrfs-snapshot-core failed (rc={run.rc})")
    return False


def core_args(m: SnapshotRun, home_policy: str, ws_policy: str) -> list[str]:
    """The sources and the cache options (their warnings logged)."""
    src = m.sources()
    return [*source_args(src, home_policy, ws_policy), *m.cache_args(src).args]


def prune_only(m: SnapshotRun) -> None:
    """Deletions by the normal retention only (no new snapshot this hour)."""
    s = m.settings
    run = CoreRun(m, CoreCommand.PRUNE, core_args(m, s["HOME_PRESERVE"], s["WS_PRESERVE"]))
    if not core_result(m, "prune", run) and run.rc not in (CORE_LOCK_BUSY, CORE_BLOCKED):
        send_alert(
            m,
            AlertKind.PRUNE_FAILED,
            Urgency.CRITICAL,
            "Snapshots: purge failed",
            f"btrfs-snapshot-core rc={run.rc}; see journalctl -u btrfs-snapshot.",
        )


# ---- what hourly reads -------------------------------------------------------------------------
def free_bytes(m: SnapshotRun) -> int | None:
    """btrfs' "Free (estimated)" in bytes (it accounts for RAID1); None when it is no number."""
    out = m.capture(["btrfs", "filesystem", "usage", "-b", "/var"])
    for line in out.split("\n"):
        if "Free (estimated)" in line:
            fields = re.split(r"[ \t]+", line.strip(" \t"))
            n = [*fields, "", "", ""][2]  # awk's $3
            return int(n) if re.fullmatch(r"[0-9]+", n) else None
    return None


def gib_setting(m: SnapshotRun, key: str) -> int:
    """A GiB setting as bash arithmetic reads it: digits (a leading 0 octal), blanks around them,
    empty as 0. Anything else is no number here: said on stderr, and read as 0."""
    text = m.settings[key].strip(" \t\n")
    if not text:
        return 0
    sign = -1 if text.startswith("-") else 1
    n = bash_int(text.lstrip("+-"))
    if n is None:
        print(f"btrfs-snapshot: {key}={m.settings[key]!r} is not a number; read as 0", file=sys.stderr, flush=True)
        return 0
    return sign * n


def skipped(m: SnapshotRun, path: str) -> bool:
    """The engine's skipped list (STATE/skipped: PATH<TAB>REASON lines) names PATH."""
    text = m.read_raw(f"{m.state}/skipped")
    return bool(text) and any(ln.split("\t", 1)[0] == path for ln in text.split("\n"))


def planned_left(m: SnapshotRun) -> str:
    """The engine's planned_left: the planned paths not converted yet, one per line."""
    if not os.access(m.plan, os.R_OK):
        return ""
    out: list[str] = []
    for strategy, path in plan_entries(m.read_raw(m.plan)):
        parent = os.path.dirname(path)
        if strategy == "aside":
            if os.path.isdir(path) and not m.is_subvol(path) and not m.is_subvol(parent):
                out.append(path)
        elif (os.path.exists(path) or os.path.isdir(parent)) and not m.is_subvol(path) and not skipped(m, path):
            out.append(path)
    return "".join(f"{p}\n" for p in out)


def mounts_below(m: SnapshotRun, path: str) -> list[str]:
    """findmnt's mount points below PATH."""
    prefix = f"{path}/"
    out = m.capture(["findmnt", "-rn", "-o", "TARGET"], stderr=Stderr.SHOW)
    return [t for t in out.split("\n") if t.startswith(prefix)]


def unfinished_bulk_move(m: SnapshotRun) -> bool:
    """A btrfs-bulk journal (a move into ~/Bulk not finished) that is not marked "bulk_only": true."""
    found = glob.glob(glob.escape(m.bulk_state) + "/journal-*.json")
    for f in found:
        try:
            with open(f, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b'"bulk_only": true' not in data:
            return True
    return False


# ---- hourly ------------------------------------------------------------------------------------
def hourly(m: SnapshotRun, args: Sequence[str]) -> int:
    del args
    fd = m.lock(m.lock_path, wait=False)
    if fd is None:
        m.log(5, LOCK_BUSY)
        return 0
    s = m.settings
    if not s["HOME_DIR"]:
        m.log(3, NOT_SET_UP)
        return 1
    if not os.access(m.core, os.X_OK):
        m.log(3, f"{m.core} is missing; re-run 'cst btrfs snapshots setup'")
        return 1
    # The planned conversions and the old trees: the engine's, under this lock.
    m.run([m.front, "convert-planned", "--lock-held", str(fd)])
    free = free_bytes(m)
    if free is None:
        m.log(3, "cannot read free space")
        return 1
    paused = os.path.exists(f"{m.state}/paused")
    emergency_gib = gib_setting(m, "EMERGENCY_AT_OR_BELOW_GIB")
    if free <= emergency_gib * GIB:
        return emergency(m, free)
    m.rm_f(f"{m.state}/emergency")
    skip_gib, resume_gib = gib_setting(m, "SKIP_AT_OR_BELOW_GIB"), gib_setting(m, "RESUME_ABOVE_GIB")
    if free <= skip_gib * GIB or (paused and free <= resume_gib * GIB):
        m.touch(f"{m.state}/paused")
        send_alert(
            m,
            AlertKind.PAUSED,
            Urgency.NORMAL,
            "Snapshots paused: low space",
            f"{free // GIB} GiB free; hourly snapshots resume above {s['RESUME_ABOVE_GIB']} GiB. "
            "Older snapshots still expire normally.",
        )
        prune_only(m)
        return 0
    m.rm_f(f"{m.state}/paused")
    clear_alert(m, AlertKind.PAUSED)
    clear_alert(m, AlertKind.EMERGENCY)
    if wait_for_data_leaving_home(m):
        prune_only(m)
        return 0
    run = CoreRun(m, CoreCommand.SNAPSHOT, core_args(m, s["HOME_PRESERVE"], s["WS_PRESERVE"]))
    if core_result(m, "snapshot", run):
        m.write(f"{m.state}/last-ok", f"{m.now()}\n")
        clear_alert(m, AlertKind.PRUNE_FAILED)
    # 3 (busy) and 4 (blocked, alerted) are no failures of this unit.
    return 0 if run.rc in (0, CORE_LOCK_BUSY, CORE_BLOCKED) else 1


def emergency(m: SnapshotRun, free: int) -> int:
    """At or below EMERGENCY_AT_OR_BELOW_GIB: keep only EMERGENCY_PRESERVE (mass deletion is the
    point: no circuit breaker; pinned snapshots, the 3 newest and the last 24 h still kept)."""
    s = m.settings
    m.touch(f"{m.state}/paused", f"{m.state}/emergency")
    send_alert(
        m,
        AlertKind.EMERGENCY,
        Urgency.CRITICAL,
        "Snapshots: emergency purge",
        f"Only {free // GIB} GiB free (limit {s['EMERGENCY_AT_OR_BELOW_GIB']}): keeping only the last day and "
        "3 dailies. Free space on /home or /var.",
    )
    policy = s["EMERGENCY_PRESERVE"]
    run = CoreRun(m, CoreCommand.PRUNE, [*core_args(m, policy, policy), "--allow-mass-delete"])
    if not core_result(m, "emergency purge", run) and run.rc not in (CORE_LOCK_BUSY, CORE_BLOCKED):
        send_alert(
            m,
            AlertKind.PRUNE_FAILED,
            Urgency.CRITICAL,
            "Snapshots: emergency purge failed",
            f"btrfs-snapshot-core rc={run.rc}; see journalctl -u btrfs-snapshot.",
        )
    # Deletions free space asynchronously: wait before the next decision.
    m.ok(["btrfs", "subvolume", "sync", "/var"])
    now = free_bytes(m)
    if now is None:
        m.log(4, f"emergency purge done (rc={run.rc}); free space cannot be read now")
    else:
        m.log(4, f"emergency purge done (rc={run.rc}); free now {now // GIB} GiB")
    return 0


def wait_for_data_leaving_home(m: SnapshotRun) -> bool:
    """True (logged, alerted after a day) while data that is about to leave /home is still in it: a
    snapshot taken now would keep it for months. Each wait's records cleared once it is over."""
    pending = f"{m.state}/drop-pending"
    if m.non_empty(pending):
        text = m.read_raw(pending)
        held_list = [t for p in read_lines(text) for t in mounts_below(m, p)]
        held = head_spaced("".join(f"{t}\n" for t in held_list), SHOWN)
        waiting(
            m,
            AlertKind.PENDING,
            f"old trees still being deleted ({nonempty_count(text)})",
            "Snapshots: waiting for old trees to be deleted",
            f"For over a day: {head_spaced(text, SHOWN)}"
            + (f"- mounted below them, so kept until unmounted: {held}" if held else ""),
        )
        return True
    m.rm_f(f"{m.state}/pending-since")
    if os.path.exists(m.boot_flag):
        left = planned_left(m).rstrip("\n")
        if left:
            waiting(
                m,
                AlertKind.BOOT_WAIT,
                f"{nonempty_count(left)} planned conversion(s) wait for the next boot",
                "Snapshots wait for a reboot",
                "Busy directories are converted at the next boot, before login; snapshots start after that. "
                f"Left: {head_spaced(left + NL, SHOWN)}",
            )
            return True
        m.rm_f(m.boot_flag, f"{m.state}/boot-attempts")
        m.log(5, "all planned conversions done; snapshots start")
    m.rm_f(f"{m.state}/boot-wait-since")
    clear_alert(m, AlertKind.BOOT_WAIT)
    if unfinished_bulk_move(m):
        waiting(
            m,
            AlertKind.BULK_JOURNAL,
            f"an unfinished move into ~/Bulk ({m.bulk_state})",
            "Snapshots wait for ~/Bulk",
            "A move into ~/Bulk was interrupted or needs a look; snapshots start after that. "
            "Run: cst btrfs bulk status",
        )
        return True
    m.rm_f(f"{m.state}/bulk-journal-since")
    clear_alert(m, AlertKind.BULK_JOURNAL)
    if os.path.exists(m.bulk_flag):
        items = m.read_raw(m.bulk_flag).replace("\n", " ")
        waiting(
            m,
            AlertKind.BULK_WAIT,
            f"items still to move into ~/Bulk at the next boot ({items})",
            "Snapshots wait for a reboot",
            "Busy items are moved into ~/Bulk at the next boot, before login; snapshots start after that. "
            f"Left: {items}",
        )
        return True
    m.rm_f(f"{m.state}/bulk-wait-since")
    clear_alert(m, AlertKind.BULK_WAIT)
    return False


# ---- prune ---------------------------------------------------------------------------------
def manual_prune(m: SnapshotRun, args: Sequence[str]) -> int:
    """cst btrfs snapshots prune [--confirm]: the normal retention by hand; --allow-mass-delete
    deletes what the circuit breaker held back."""
    if m.lock(m.lock_path, wait=True) is None:
        m.log(5, LOCK_BUSY)
        return 0
    s = m.settings
    if not s["HOME_DIR"]:
        m.say(NOT_SET_UP, sys.stderr)
        return 1
    if not os.access(m.core, os.X_OK):
        m.say(f"{m.core} is missing; re-run 'cst btrfs snapshots setup'", sys.stderr)
        return 1
    dry = False
    extra: list[str] = []
    for a in args:
        match a:
            case "--dry-run":
                dry = True
                extra.append(a)
            case "--allow-mass-delete":
                extra.append(a)
            case _:
                m.say(PRUNE_USAGE, sys.stderr)
                return 2
    run = CoreRun(m, CoreCommand.PRUNE, [*core_args(m, s["HOME_PRESERVE"], s["WS_PRESERVE"]), *extra])
    if run.rc == 0 and not dry:
        clear_alert(m, AlertKind.BLOCKED)
    return run.rc


# ---- purge ---------------------------------------------------------------------------------
def listed_paths(out: str) -> list[str] | None:
    """The snapshot paths of the core's list --json (None: not its JSON)."""
    try:
        printed: list[str] = []
        for src in json.loads(out)["sources"]:
            printed.extend(str(snap["path"]) for snap in src.get("snapshots", []))
    except ValueError, KeyError, TypeError, AttributeError, RecursionError:
        return None  # whatever it holds: not the core's JSON
    return [p for p in "\n".join(printed).split("\n") if p]


def purge(m: SnapshotRun, args: Sequence[str]) -> int:
    """Delete every snapshot the core owns (pinned ones skipped; unfinished ones finished first)."""
    del args
    if m.lock(m.lock_path, wait=True) is None:
        m.log(5, LOCK_BUSY)
        return 0
    if not m.settings["HOME_DIR"]:
        m.log(3, f"purge: not set up (no HOME_DIR in {m.conf}); nothing deleted")
        return 1
    if not os.path.isdir(m.snapdir):
        m.say("No snapshot directory: nothing to delete.")
        return 0
    sources = source_args(m.sources(), "1h", "1h")
    if m.non_empty(f"{m.state}/core-unfinished") and CoreRun(m, CoreCommand.FINISH, sources).rc != 0:
        m.log(4, "purge: finishing interrupted snapshots failed")
    listing = [m.core, "list", "--json", "--snapdir", m.snapdir, "--pin-file", m.pin_file, "--state-dir", m.state]
    res = m.run([*listing, *sources], output="stdout-show-stderr")
    if res.fault is not None or res.returncode != 0:
        m.log(3, "purge: cannot list the snapshots; nothing deleted")
        return 1
    listed = listed_paths(res.stdout)
    if listed is None:
        m.log(3, "purge: btrfs-snapshot-core's list is not readable (not its JSON); nothing deleted")
        return 1
    paths: list[str] = []
    for p in listed:
        if m.non_empty(m.pin_file) and p in m.read_raw(m.pin_file).split("\n"):
            m.log(4, f"purge: {p} is pinned (NAS backup running?); kept")
            continue
        paths.append(p)
    m.say(f"Deleting {len(paths)} snapshot(s) in batches of {PURGE_BATCH}...")
    for i in range(0, len(paths), PURGE_BATCH):
        res = m.run(["btrfs", "subvolume", "delete", *paths[i : i + PURGE_BATCH]], output="stdout-show-stderr")
        if res.fault is not None or res.returncode != 0:
            m.log(3, "purge: a deletion failed")
        m.ok(["btrfs", "subvolume", "sync", "/var"])
    try:
        n_left = len(os.listdir(m.snapdir))
    except OSError:
        n_left = 0
    if n_left:
        m.say(
            f"{n_left} entr{'y' if n_left == 1 else 'ies'} left in {m.snapdir} "
            "(not owned by btrfs-snapshot-core, or pinned): not deleted."
        )
    else:
        try:
            os.rmdir(m.snapdir)
        except OSError:
            pass
    m.rm_f(f"{m.state}/core-unfinished", f"{m.state}/core-writable")
    return 0

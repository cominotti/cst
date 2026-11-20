# SPDX-License-Identifier: MPL-2.0
"""btrfs-health-check's I/O: the commands it asks (btrfs, findmnt, lsblk, nvme, journalctl,
systemctl, rpm-ostree, date), the files it reads under the root, the state it keeps, btrfs-notify.

Every check reads the machine the way the bash version did, in the same order, and asks
btrfs/health_check/domain.py what the answers mean. A command's stderr goes where it went in
bash: to the journal (passed on), nowhere (2>/dev/null) or into its output (2>&1).
"""

from collections.abc import Sequence
import contextlib
from dataclasses import dataclass, field
import os
import stat
import sys
import time

from btrfs.health_check.domain import (
    ALERT_TITLE,
    KNOBS,
    NAS_MAX_AGE_DAYS,
    RECLAIM_KINDS,
    SCRUB_MAX_AGE_DAYS,
    SNAPSHOT_GRACE,
    SNAPSHOT_MAX_AGE,
    SUSPEND_UNITS,
    TAG,
    UPSTREAM_RULE_LINE,
    StderrRoute,
    alert_body,
    awake_since,
    awk_int,
    bash_div,
    bash_int,
    controller,
    disks_of,
    has_missing,
    kernel_problem,
    marker_problem,
    md_problem,
    member_paths,
    metadata_problem,
    missing_bytes,
    mixed_profiles,
    mount_of,
    output,
    parse_scrub,
    pick_mounts,
    rule_uuid,
    sed_values,
    smart_problems,
    stats_lines,
    swap_partitions,
    unallocated,
    unallocated_problems,
)
from cst_core.adapters.files import read_bytes, write_atomic
from cst_core.adapters.journal import log
from cst_core.domain.contracts import NOTIFY_PROGRAM, SNAPSHOTS_CONF, Urgency
from cst_core.domain.install import manifest_by_src
from cst_core.domain.ostree import query_status
from cst_core.domain.text import file_lines, parse_snapshots_conf, read_fields
from cst_core.domain.values import DAY
from cst_core.ports import CommandFault, CommandResult, CommandRunnerPort, OutputMode

NOTIFY = NOTIFY_PROGRAM
RECLAIM_CONF = manifest_by_src()["btrfs/tmpfiles/btrfs-automatic-reclaim.conf.in"].dest


@dataclass(frozen=True, slots=True)
class Probe:
    """The machine the check reads: files under root ("" outside the tests), commands through runner."""

    root: str
    runner: CommandRunnerPort

    def path(self, p: str) -> str:
        return self.root + p

    @property
    def state_dir(self) -> str:
        return self.path("/var/lib/btrfs-health-check")

    @property
    def notify(self) -> str:
        return self.path(NOTIFY)

    def cat(self, p: str) -> str | None:
        """The file's text as $(cat FILE) keeps it; None when it cannot be read."""
        text = self.text(p)
        return None if text is None else output(text)

    def text(self, p: str) -> str | None:
        """The file's whole text; None when it cannot be read."""
        data = read_bytes(p)
        return None if data is None else data.decode("utf-8", "surrogateescape")

    def run(self, argv: Sequence[str], stderr: StderrRoute) -> CommandResult:
        """Run argv; its stderr passed on, dropped or merged into its output."""
        r = self.runner.run(argv, output=OutputMode.MERGE if stderr == StderrRoute.MERGE else OutputMode.CAPTURE)
        if r.fault is not None:
            why = "command not found" if r.fault == CommandFault.MISSING else str(r.cause or r.fault)
            note = f"{TAG}: {argv[0]}: {why}\n"
            if stderr == StderrRoute.MERGE:
                return CommandResult(r.returncode, note, "", r.argv, r.fault, r.cause)
            if stderr == StderrRoute.PASS:
                sys.stderr.write(note)
                sys.stderr.flush()
            return r
        if stderr == StderrRoute.PASS and r.stderr:
            sys.stderr.write(r.stderr)
            sys.stderr.flush()
        return r

    def out(self, argv: Sequence[str], stderr: StderrRoute = StderrRoute.DROP) -> str:
        """$(argv): its output without the newlines at the end."""
        return output(self.run(argv, stderr).stdout)

    def first_line(self, argv: Sequence[str]) -> str:
        """$(argv | head -n1), its stderr passed on."""
        lines = file_lines(self.run(argv, StderrRoute.PASS).stdout)
        return lines[0] if lines else ""

    def epoch(self, when: str) -> int | None:
        """date -d WHEN +%s: the time as date(1) reads it; None when it cannot."""
        r = self.run(["date", "-d", when, "+%s"], StderrRoute.DROP)
        value = output(r.stdout)
        try:
            return int(value) if r.returncode == 0 and r.fault is None else None
        except ValueError:
            return None


def now() -> int:
    """$(date +%s)."""
    return int(time.time())


def is_block(p: str) -> bool:
    try:
        return stat.S_ISBLK(os.stat(p).st_mode)
    except OSError:
        return False


def replace_state(p: str, text: str) -> None:
    """Replace a small state file (as printf > p.tmp && mv -f p.tmp p); a failure is said on stderr."""
    try:
        write_atomic(p, text, mode="umask", durable=False, relabel=False)
    except OSError as e:
        sys.stderr.write(f"{TAG}: cannot write {p}: {e.strerror}\n")
        sys.stderr.flush()


@dataclass(slots=True)
class Members:
    """The member devices of the btrfs filesystems: device-mapper names and disks (for the kernel
    filter and the health logs)."""

    dms: list[str] = field(default_factory=list)
    disks: list[str] = field(default_factory=list)


def housekeeping(m: Probe) -> None:
    """Alerts kept for the next login expire (btrfs-notify --prune); the stall monitor's queue, normally
    empty, is delivered: an alert written in the instant btrfs-stall-alert.service was finishing would
    otherwise wait for the next stall alert."""
    if not os.access(m.notify, os.X_OK):
        return
    m.runner.run([m.notify, "--prune"], output=OutputMode.INHERIT)
    queue = m.path("/run/btrfs-stall-monitor")
    if os.path.isdir(queue):
        m.runner.run([m.notify, "--queue", queue], output=OutputMode.INHERIT)


def mounts(m: Probe) -> list[str]:
    """One mount point per btrfs filesystem."""
    return pick_mounts(m.run(["findmnt", "-rn", "-t", "btrfs", "-o", "UUID,TARGET"], StderrRoute.PASS).stdout)


def check_filesystem(m: Probe, mnt: str, members: Members) -> list[str]:
    """The problems of one btrfs filesystem; its member devices go into members."""
    problems: list[str] = []
    show = m.out(["btrfs", "filesystem", "show", mnt], StderrRoute.MERGE)
    if has_missing(show):
        problems.append(f"btrfs {mnt}: member device MISSING (running degraded)")
    # A transaction abort or a tree-checker rejection forces the fs read-only without touching the
    # device error counters below.
    opts = m.first_line(["findmnt", "-rn", "-o", "OPTIONS", "-M", mnt])
    if mnt != "/sysroot" and ",ro," in f",{opts},":
        problems.append(f"btrfs {mnt}: mounted READ-ONLY (forced after an error?) - check 'journalctl -k -g BTRFS'")
    usage = m.out(["btrfs", "filesystem", "usage", "-b", mnt])
    missing = missing_bytes(usage)
    if missing and missing != "0":
        problems.append(f"btrfs {mnt}: {missing} bytes on missing device")
    if mixed_profiles(usage):
        problems.append(
            f"btrfs {mnt}: mixed chunk profiles (degraded writes?) - run balance -dconvert=raid1,soft "
            "-mconvert=raid1,soft"
        )
    # Unallocated space is what metadata (and new chunks) grow into, per device: every RAID1 chunk
    # needs room on both members (a missing member is listed as "missing": skipped).
    found, least = unallocated_problems(mnt, unallocated(m.out(["btrfs", "device", "usage", "-b", mnt])))
    problems += found
    uuid = m.first_line(["findmnt", "-rn", "-o", "UUID", "-M", mnt])
    alloc = m.path(f"/sys/fs/btrfs/{uuid}/allocation")
    if uuid and os.path.isdir(alloc):
        problems += allocation_problems(m, mnt, uuid, alloc, least)
    if m.run(["btrfs", "device", "stats", "--check", mnt], StderrRoute.DROP).returncode != 0:
        stats = stats_lines(m.run(["btrfs", "device", "stats", mnt], StderrRoute.PASS).stdout)
        problems.append(f"btrfs {mnt}: non-zero device error counters: {stats}")
    problems += scrub_problems(m, mnt)
    for devpath in member_paths(show):
        if not is_block(devpath):
            continue
        members.dms.append(os.path.basename(os.path.realpath(devpath)))
        members.disks += disks_of(m.run(["lsblk", "-lnso", "NAME,TYPE", devpath], StderrRoute.PASS).stdout)
    return problems


def _sysfs_number(m: Probe, p: str) -> int:
    """$(cat FILE 2>/dev/null || echo 0) in arithmetic (0 for a value that is not a number)."""
    value = m.cat(p)
    return 0 if value is None else bash_int(value) or 0


def allocation_problems(m: Probe, mnt: str, uuid: str, alloc: str, least: int | None) -> list[str]:
    """Metadata room, the automatic reclaim's knobs and its error counts (sysfs)."""
    problems: list[str] = []
    meta_free = _sysfs_number(m, f"{alloc}/metadata/total_bytes") - _sysfs_number(m, f"{alloc}/metadata/bytes_used")
    found = metadata_problem(mnt, meta_free, _sysfs_number(m, f"{alloc}/global_rsv_size"), least)
    if found is not None:
        problems.append(found)
    # Automatic reclaim installed by btrfs-setup.sh, but not active (e.g. the knob was reset). Both
    # knobs are needed.
    conf_path = m.path(RECLAIM_CONF)
    conf = m.text(conf_path) or ""
    problems.extend(
        f"btrfs {mnt}: automatic reclaim is configured but data/{knob} is off - run "
        f"'systemd-tmpfiles --create {conf_path}'"
        for knob in KNOBS
        if f"/sys/fs/btrfs/{uuid}/allocation/data/{knob}" in conf and m.cat(f"{alloc}/data/{knob}") != "1"
    )
    problems.extend(_reclaim_error_problems(m, mnt, uuid, alloc))
    return problems


def _reclaim_error_problems(m: Probe, mnt: str, uuid: str, alloc: str) -> list[str]:
    """The automatic reclaim's new errors since the last check, per kind; writes the state file."""
    problems: list[str] = []
    # reclaim_errors counts since mount (it resets on reboot). The state file remembers boot id and
    # count, so errors right after a reboot are still compared against 0, and a damaged file just
    # restarts at 0.
    boot_id = m.cat(m.path("/proc/sys/kernel/random/boot_id")) or ""
    for kind in RECLAIM_KINDS:
        errs = m.cat(f"{alloc}/{kind}/reclaim_errors")
        errs = "0" if errs is None else errs
        if not errs.isascii() or not errs.isdigit():
            continue
        seen_file = f"{m.state_dir}/reclaim-errors-{uuid}-{kind}"
        seen_boot, seen = read_fields((m.text(seen_file) or "").split("\n")[0], 2)
        if seen_boot != boot_id or not seen.isascii() or not seen.isdigit():
            seen = "0"
        count, seen_count = bash_int(errs), bash_int(seen)  # None: bash's arithmetic fails on it (08)
        if count is not None and seen_count is not None and count > seen_count:
            problems.append(
                f"btrfs {mnt}: automatic {kind} reclaim failed {count - seen_count} time(s) since the last check "
                f"(reclaim_errors={errs}) - see 'journalctl -k -g BTRFS'"
            )
        replace_state(seen_file, f"{boot_id} {errs}\n")
    return problems


def scrub_problems(m: Probe, mnt: str) -> list[str]:
    """The last scrub: errors, never run, not finished, or too old."""
    problems: list[str] = []
    scrub = parse_scrub(m.out(["btrfs", "scrub", "status", mnt]))
    if scrub.errors:
        problems.append(f"btrfs {mnt}: last scrub reported errors: {scrub.errors}")
    if not scrub.started:
        problems.append(f"btrfs {mnt}: no scrub has ever completed")
    elif scrub.status == "running":
        # A scrub in progress is no problem: this arm keeps it out of the
        # "did not finish" and age checks below.
        pass
    elif scrub.status != "finished":
        problems.append(
            f"btrfs {mnt}: last scrub (started {scrub.started}) did not finish: status {scrub.status or 'unknown'}"
        )
    elif (started := m.epoch(scrub.started)) is not None:
        age_days = bash_div(now() - started, DAY)
        if age_days > SCRUB_MAX_AGE_DAYS:
            problems.append(f"btrfs {mnt}: last scrub is {age_days} days old")
    return problems


def degraded_boot_problems(m: Probe) -> list[str]:
    """btrfs-degraded-boot-setup.sh's parts (only when it is installed), this boot's degraded flags
    and the markers of degraded boots."""
    problems: list[str] = []
    lr_rule = m.path("/etc/udev/rules.d/65-btrfs-last-resort.rules")
    if os.access(lr_rule, os.R_OK):
        found = rule_uuid(m.text(lr_rule) or "")
        root_uuid = m.first_line(["findmnt", "-rn", "-o", "UUID", "--target", "/sysroot"])
        if found != root_uuid:
            problems.append(
                f"btrfs-last-resort: {lr_rule} is for {found or '?'} but the root fs is {root_uuid or '?'}; "
                "re-run 'cst btrfs degraded-boot'"
            )
        # Upstream rule semantics the last resort builds on: booted and next boot.
        ups = [m.path("/usr/lib/udev/rules.d/64-btrfs.rules")]
        nxt, unknown = next_root(m)
        if nxt:
            ups.append(m.path(f"{nxt}/usr/lib/udev/rules.d/64-btrfs.rules"))
        else:
            # A journal warning, not a problem: the booted system's rule is still checked.
            log(4, f"udev: next deployment unknown ({unknown}); its 64-btrfs.rules was not checked", tag=TAG)
        problems.extend(
            f"udev: {up} changed semantics; review btrfs/btrfs-degraded-boot-setup.sh (CST)"
            for up in ups
            if UPSTREAM_RULE_LINE not in (m.text(up) or "")
        )
    flags = m.path("/run/btrfs-allow-degraded")
    problems.extend(
        f"btrfs {name}: this boot is DEGRADED (btrfs-last-resort: member disk missing at boot)"
        for name in _glob_names(flags)
        if os.path.exists(f"{flags}/{name}")
    )
    folder = m.path("/var/lib/btrfs-degraded-boot")
    for name in _glob_names(folder):
        marker = f"{folder}/{name}"
        if name.startswith("degraded-") and os.path.exists(marker):
            problems += marker_problems(m, marker)
    return problems


def _glob_names(folder: str) -> list[str]:
    """The names a glob FOLDER/* gives, sorted (no hidden ones)."""
    try:
        return sorted(n for n in os.listdir(folder) if not n.startswith("."))
    except OSError:
        return []


def next_root(m: Probe) -> tuple[str, str]:
    """The root of the deployment rpm-ostree lists first (the next boot's) and "", or "" and why it is
    unknown (one line)."""

    def run(argv: list[str]) -> tuple[int, str, str]:
        r = m.run(argv, StderrRoute.DROP)
        return r.returncode, r.stdout, r.stderr

    status = query_status(run)
    if isinstance(status, str):
        return "", status
    if not status.deployments:
        return "", "rpm-ostree lists no deployment"
    first = status.deployments[0]
    serial = first.get("serial")
    # The bash's "%d" % ... failed on the others: no next root.
    if "osname" in first and "checksum" in first and isinstance(serial, (int, float)):
        with contextlib.suppress(OverflowError, ValueError):  # inf, nan
            return f"/ostree/deploy/{first['osname']}/deploy/{first['checksum']}.{int(serial)}", ""
    return "", "its first deployment has no osname, checksum or whole serial"


def marker_problems(m: Probe, marker: str) -> list[str]:
    """A marker of a degraded boot (since=, complete_since=): a problem until a scrub that STARTED after
    the fs was seen complete again finished clean, with no missing device and no mixed profiles;
    then it is removed."""
    uuid = marker.rsplit("/degraded-", 1)[1]
    text = m.text(marker) or ""
    since, since_h, complete = (sed_values(text, k) for k in ("since", "since_human", "complete_since"))
    mnt = mount_of(m.run(["findmnt", "-rn", "-t", "btrfs", "-o", "TARGET,UUID"], StderrRoute.PASS).stdout, uuid)
    state = "not mounted"
    since_n = bash_int(since)
    if mnt and since_n is not None:
        if has_missing(m.out(["btrfs", "filesystem", "show", mnt], StderrRoute.MERGE)):
            state = "still missing a device"
            keep = "".join(line + "\n" for line in file_lines(text) if not line.startswith("complete_since="))
            _rewrite(marker, keep)
        else:
            complete_n = bash_int(complete)
            if complete_n is None:
                complete_n = now()
                complete = str(complete_n)
                _append_text(marker, f"complete_since={complete}\n")
            # date(1), as in bash: a value it cannot show leaves the time out (its error goes to the journal).
            at = m.out(["date", "-d", f"@{complete}", "+%F %T"], StderrRoute.PASS)
            state = f"all devices present since {at}, waiting for a clean scrub started after that"
            scrub = parse_scrub(m.out(["btrfs", "scrub", "status", mnt]))
            started = m.epoch(scrub.started) or 0
            if (
                started > since_n
                and started > complete_n
                and scrub.clean_finish
                and not mixed_profiles(m.out(["btrfs", "filesystem", "usage", "-b", mnt]))
            ):
                _remove(marker)
                log(
                    5,
                    f"btrfs {uuid}: RAID1 restored (clean scrub started {scrub.started} after the degraded boot), "
                    f"cleared {marker}",
                    tag=TAG,
                )
                return []
    return [marker_problem(uuid, since_h, state)]


def _remove(p: str) -> None:
    """As rm -f: a missing file is no error, another failure is said on stderr and the run goes on
    (/var forced read-only must not keep the alert about it from going out)."""
    try:
        os.unlink(p)
    except FileNotFoundError:
        pass
    except OSError as e:
        sys.stderr.write(f"{TAG}: cannot remove {p}: {e.strerror}\n")


def _rewrite(p: str, text: str) -> None:
    """As sed -i: the file replaced with text, its mode kept."""
    try:
        mode = stat.S_IMODE(os.stat(p).st_mode)
        write_atomic(p, text, mode=mode, durable=False, relabel=False)
    except OSError as e:
        sys.stderr.write(f"{TAG}: cannot rewrite {p}: {e.strerror}\n")


def _append_text(p: str, text: str) -> None:
    try:
        with open(p, "a", encoding="utf-8", errors="surrogateescape") as f:
            f.write(text)
    except OSError as e:
        sys.stderr.write(f"{TAG}: cannot write {p}: {e.strerror}\n")


def kernel_log(m: Probe, ours: Sequence[str]) -> list[str]:
    """Kernel messages since the previous run (journal cursor): errors that forced the fs read-only,
    csum failures (repaired from the other RAID1 copy, but a sign of a failing device), hung tasks."""
    problems: list[str] = []
    cursor = f"{m.state_dir}/kernel.cursor"
    base = ["journalctl", "-k", "-q", "--no-pager", "-o", "short-iso"]
    klog = None
    if _size(cursor) > 0:
        r = m.run([*base, f"--cursor-file={cursor}"], StderrRoute.MERGE)
        klog = output(r.stdout) if r.returncode == 0 else None
    if klog is None:
        # journalctl refuses --since together with --cursor-file, so the first run (or a run after a
        # bad cursor) looks back 65 min and then records a cursor.
        _remove(cursor)
        r = m.run([*base, "--since", "-65min"], StderrRoute.MERGE)
        klog = output(r.stdout)
        if r.returncode != 0:
            problems.append(f"kernel log scan failed: {klog[:200]}")
        m.run(["journalctl", "-k", "-q", "-n", "1", "-o", "short-iso", f"--cursor-file={cursor}"], StderrRoute.DROP)
    found = kernel_problem(klog, ours)
    return problems + ([found] if found is not None else [])


def _size(p: str) -> int:
    try:
        return os.stat(p).st_size
    except OSError:
        return 0


def snapshot_problems(m: Probe) -> list[str]:
    """Hourly /home snapshots ('cst btrfs snapshots setup'): still running? The space guard,
    btrfs-snapshot-core's checks and the NAS backup alert on their own; this catches a timer or a
    snapshot run that silently stopped working."""
    if m.run(["systemctl", "is-enabled", "-q", "btrfs-snapshot.timer"], StderrRoute.DROP).returncode != 0:
        return []
    problems: list[str] = []
    snap = m.path("/var/lib/btrfs-snapshot")
    last = m.cat(f"{snap}/last-ok") or ""
    journal = m.run(
        [
            "journalctl",
            "-q",
            "-o",
            "short-unix",
            "--no-pager",
            *(a for u in SUSPEND_UNITS for a in ("-u", u)),
            "-n",
            "1",
        ],
        StderrRoute.DROP,
    ).stdout
    uptime = m.text(m.path("/proc/uptime")) or ""
    boot_since = now() - awk_int(uptime.split("\n")[0])
    awake = awake_since(journal, boot_since)
    # A pause by the space guard, old trees still being deleted after a conversion, or a conversion
    # or a move into ~/Bulk waiting for the next boot are announced by btrfs-snapshot itself.
    quiet = (
        os.path.exists(f"{snap}/paused")
        or _size(f"{snap}/drop-pending") > 0
        or os.path.exists(m.path("/etc/btrfs-setup/convert-at-boot"))
        or os.path.exists(m.path("/etc/btrfs-setup/bulk-at-boot"))
    )
    last_n = bash_int(last)
    if not quiet and last_n is not None and now() - last_n > SNAPSHOT_MAX_AGE and now() - awake > SNAPSHOT_GRACE:
        problems.append("snapshots: no successful hourly run for over 3 hours - check 'journalctl -u btrfs-snapshot'")
    _log_stale_nas(m, snap)
    return problems


def _log_stale_nas(m: Probe, snap: str) -> None:
    """A journal line when the NAS backup is on and had no success for NAS_MAX_AGE_DAYS; no problem
    of its own: btrfs-snapshot already notifies once a day about a stale NAS backup."""
    conf = m.path(SNAPSHOTS_CONF)
    settings = parse_snapshots_conf(m.text(conf) or "").values if os.access(conf, os.R_OK) else {}
    if settings.get("NAS_ENABLED", "0") != "1":
        return
    # The last success, or when the backup was turned on if later (as btrfs-snapshot).
    nas_last = 0
    for name in ("nas-last-ok", "nas-enabled-at"):
        v = bash_int(m.cat(f"{snap}/{name}") or "")
        if v is not None and v > nas_last:
            nas_last = v
    if nas_last > 0 and now() - nas_last > NAS_MAX_AGE_DAYS * DAY:
        log(
            4,
            f"no successful NAS backup (restic) for over {NAS_MAX_AGE_DAYS} days (journalctl -u btrfs-snapshot-nas)",
            tag=TAG,
        )


def md_problems(m: Probe) -> list[str]:
    mdstat = m.path("/proc/mdstat")
    found = md_problem(m.text(mdstat) or "") if os.access(mdstat, os.R_OK) else None
    return [found] if found is not None else []


def smart(m: Probe, disk: str, role: str = "") -> list[str]:
    """The disk's own NVMe health log (none: nothing to say)."""
    r = m.run(["nvme", "smart-log", controller(disk)], StderrRoute.DROP)
    return smart_problems(disk, role, r.stdout) if r.returncode == 0 and r.fault is None else []


def swap_disks(m: Probe, members: Sequence[str]) -> list[str]:
    """The NVMe disks under every swap device (an encrypted swap dm-2 -> nvme1n1p2 -> nvme1n1), minus
    the btrfs member disks (already checked). Swap files and zram are skipped: the first lives on a
    filesystem checked above, the second is RAM."""
    swaps = m.path("/proc/swaps")
    if not os.access(swaps, os.R_OK):
        return []
    found: list[str] = []
    for dev in swap_partitions(m.text(swaps) or ""):
        for name in disks_of(m.run(["lsblk", "-lnso", "NAME,TYPE", dev], StderrRoute.DROP).stdout):
            if name.startswith("nvme") and name not in members and name not in found:
                found.append(name)
    return found


def alert(m: Probe, problems: Sequence[str]) -> None:
    """The alert through btrfs-notify (shown on the desktop, or kept for the next login)."""
    r = m.runner.run(
        [m.notify, "-u", Urgency.CRITICAL, "-a", TAG, ALERT_TITLE, alert_body(problems)], output=OutputMode.INHERIT
    )
    if r.fault is not None:
        sys.stderr.write(f"{TAG}: {m.notify}: {getattr(r.cause, 'strerror', None) or r.fault}\n")
        sys.stderr.flush()


def make_state_dir(m: Probe) -> None:
    try:
        os.makedirs(m.state_dir, exist_ok=True)
    except OSError as e:
        sys.stderr.write(f"{TAG}: cannot create {m.state_dir}: {e.strerror}\n")
        sys.stderr.flush()

# SPDX-License-Identifier: MPL-2.0
"""btrfs-health-check's rules (pure): its thresholds, and how it reads what btrfs, nvme-cli,
the kernel log and its own state files print, as the bash version read them.

The thresholds are the one place doctor takes its levels for unallocated space
from (doctor/domain.py UNALLOC_DEFAULTS).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Final

from cst_core.domain.text import file_lines, read_fields
from cst_core.domain.values import GIB, HOUR, MIB, MINUTE

TAG: Final = "btrfs-health-check"
ALERT_TITLE: Final = "Disk health: problems detected"
SCRUB_MAX_AGE_DAYS: Final = 10
WEAR_ALERT_PCT: Final = 80
UNALLOC_MIN_GIB: Final = 50  # early warning, per device
UNALLOC_CRIT_GIB: Final = 5  # near ENOSPC: btrfs' own dynamic reclaim only starts here
NAS_MAX_AGE_DAYS: Final = 8
SNAPSHOT_MAX_AGE: Final = 3 * HOUR  # s since the last successful hourly snapshot
SNAPSHOT_GRACE: Final = 20 * MINUTE  # s after boot or resume: the timer catches up first
KNOBS: Final = ("dynamic_reclaim", "periodic_reclaim")
RECLAIM_KINDS: Final = ("data", "metadata")
UPSTREAM_RULE_LINE: Final = 'ENV{ID_BTRFS_READY}=="0", ENV{SYSTEMD_READY}="0"'
SUSPEND_UNITS: Final = (
    "systemd-suspend.service",
    "systemd-hibernate.service",
    "systemd-suspend-then-hibernate.service",
)
MISSING_DEVICE: Final = "missing"  # the name btrfs device usage gives a member that is not there


class StderrRoute(StrEnum):
    """Where a command's stderr goes, as in the bash version."""

    PASS = "pass"  # passed on (to the journal)
    DROP = "drop"  # 2>/dev/null
    MERGE = "merge"  # 2>&1: into its output


_SPACE: Final = r"[ \t\n\r\f\v]"  # [[:space:]]
_DIGITS: Final = re.compile(r"[0-9]+")
_HEX: Final = re.compile(r"0[xX][0-9a-fA-F]+")
_NUMBER: Final = r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?"
_BLANKS: Final = re.compile(r"[ \t\n]+")
_KERNEL_BAD: Final = re.compile(
    r"BTRFS (critical|error)|forced readonly|Transaction aborted|corrupt (leaf|node)|csum failed|"
    r"blocked for more than [0-9]+ seconds|I/O error",
    re.IGNORECASE,
)
_DEVICE: Final = re.compile(r"\(device ([A-Za-z0-9._-]+)\)")
_DEV: Final = re.compile(_SPACE + r"dev ([A-Za-z0-9._-]+),")
_MD_DEGRADED: Final = re.compile(r"\[[U_]*_[U_]*\]")
_RULE_UUID: Final = re.compile(r'ENV\{ID_FS_UUID\}!="([0-9a-f-]*)"')


# ---- the shell's ways of reading text, kept so that the results stay the same ---------------------------------


def output(text: str) -> str:
    """What $(...) keeps of a command's output: every NUL byte dropped, every newline at the end removed."""
    return text.replace("\0", "").rstrip("\n")


def lines_of(items: Sequence[str]) -> str:
    """Items printed one per line, as $(...) keeps them."""
    return output("".join(item + "\n" for item in items))


def bash_int(text: str) -> int | None:
    """A string of digits as bash arithmetic reads it (a leading 0 is octal); None where bash fails
    (not digits, or an 8 or 9 after a leading 0)."""
    if not _DIGITS.fullmatch(text):
        return None
    if len(text) > 1 and text.startswith("0"):
        return int(text, 8) if all(c in "01234567" for c in text) else None
    return int(text)


def bash_div(a: int, b: int) -> int:
    """a / b in bash arithmetic: truncated towards zero."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def fields(line: str) -> list[str]:
    """The words of a line as awk splits it by default."""
    s = line.strip(" \t\n")
    return _BLANKS.split(s) if s else []


def awk_field(line: str, n: int) -> str:
    """awk's $n with the default field separator ("" when there are fewer fields)."""
    f = fields(line)
    return f[n - 1] if len(f) >= n else ""


def awk_int(line: str) -> int:
    """awk's int($1): the leading number of the first word, truncated (0 for none)."""
    m = re.match(_NUMBER, awk_field(line, 1))
    return int(float(m.group(0))) if m else 0


def sed_values(text: str, key: str) -> str:
    """sed -n 's/^KEY=//p': the value of every line that starts with KEY=, one per line."""
    return lines_of([line[len(key) + 1 :] for line in file_lines(text) if line.startswith(key + "=")])


def cut_at_last(text: str, letter: str) -> str:
    """bash's ${text%L[0-9]*}: cut at the last L followed by a digit (text as it is without one)."""
    for i in range(len(text) - 2, -1, -1):
        if text[i] == letter and text[i + 1] in "0123456789":
            return text[:i]
    return text


# ---- findmnt, btrfs ---------------------------------------------------------------------------------------------


def pick_mounts(findmnt_uuid_target: str) -> list[str]:
    """One mount point per btrfs filesystem (findmnt's UUID TARGET lines); / or /var win over bind
    mounts like /etc. The filesystems in the order findmnt lists them."""
    chosen: dict[str, str] = {}
    for line in file_lines(findmnt_uuid_target):
        uuid, target = awk_field(line, 1), awk_field(line, 2)
        if uuid not in chosen or target in ("/", "/var"):
            chosen[uuid] = target
    return list(chosen.values())


def mount_of(findmnt_target_uuid: str, uuid: str) -> str:
    """The first mount point findmnt's TARGET UUID lines give for uuid ("" when it is not mounted)."""
    for line in file_lines(findmnt_target_uuid):
        if awk_field(line, 2) == uuid:
            return awk_field(line, 1)
    return ""


def has_missing(show: str) -> bool:
    """btrfs filesystem show names a missing device."""
    return "missing" in show.lower()


def mixed_profiles(usage: str) -> bool:
    return re.search("Multiple profiles:" + _SPACE + "+yes", usage) is not None


def missing_bytes(usage: str) -> str:
    """The "Device missing" value(s) of btrfs filesystem usage, blanks removed."""
    return lines_of(
        [re.sub(_SPACE, "", [*line.split(":"), ""][1]) for line in file_lines(usage) if "Device missing" in line]
    )


def unallocated(device_usage: str) -> list[tuple[str, str]]:
    """(device, unallocated bytes as printed) of btrfs device usage -b, as its awk filter gave them."""
    rows: list[tuple[str, str]] = []
    dev = ""
    for line in file_lines(device_usage):
        if ", ID:" in line:
            dev = re.sub(r",$", "", awk_field(line, 1))
        if re.match(_SPACE + "+Unallocated:", line):
            name, value = read_fields(f"{dev} {awk_field(line, 2)}", 2)
            rows.append((name, value))
    return rows


def unallocated_problems(mnt: str, rows: Sequence[tuple[str, str]]) -> tuple[list[str], int | None]:
    """The problems of each device's unallocated space, and the smallest value (None: none read)."""
    problems: list[str] = []
    least: int | None = None
    for dev, text in rows:
        unalloc = bash_int(text)
        if dev == MISSING_DEVICE or unalloc is None:
            continue
        least = unalloc if least is None or unalloc < least else least
        if unalloc < UNALLOC_CRIT_GIB * GIB:
            # One line (one alert line per problem). Order matters: a balance needs unallocated room to
            # work in, so freeing space comes first; the ladder starts at -dusage=0 (empty chunks, no copying).
            problems.append(
                f"btrfs {mnt}: CRITICAL: only {unalloc // MIB} MiB unallocated on {dev}: ENOSPC is close. First free "
                f"space (old snapshots, caches, images), then 'btrfs balance start -dusage=0 {mnt}', then "
                "-dusage=5, 15, 30; never a metadata or full balance. If a balance hangs the PC or the boot: boot once "
                "with ,skip_balance added to the rootflags= of the kernel command line, then "
                f"'btrfs balance cancel {mnt}' (cst btrfs README, section 13)"
            )
        elif unalloc < UNALLOC_MIN_GIB * GIB:
            problems.append(
                f"btrfs {mnt}: only {unalloc // GIB} GiB unallocated on {dev} (warning below {UNALLOC_MIN_GIB} GiB; "
                "metadata grows only from unallocated space). Free space, or compact half-empty data chunks: "
                f"'btrfs balance start -dusage=20 {mnt}'"
            )
    return problems, least


def metadata_problem(mnt: str, meta_free: int, reserve: int, least: int | None) -> str | None:
    """Metadata ENOSPC proper: little room left in metadata chunks (less than twice the global
    reserve) and almost nothing to allocate more."""
    if least is None or not (meta_free < 2 * reserve and least < 2 * GIB):
        return None
    return (
        f"btrfs {mnt}: CRITICAL: metadata almost full ({bash_div(meta_free, MIB)} MiB free in metadata chunks, "
        f"{bash_div(least, MIB)} MiB unallocated): writes may fail with ENOSPC soon"
    )


def awk_nonzero(line: str) -> bool:
    """awk's '$2 != 0': as numbers when $2 looks like one, else as text."""
    second = awk_field(line, 2)
    return float(second) != 0 if re.fullmatch(_NUMBER, second) else second != "0"


def stats_lines(stats: str) -> str:
    """The device stats lines that are not 0, run together (each followed by a blank)."""
    return "".join(line + " " for line in file_lines(stats) if awk_nonzero(line))


@dataclass(frozen=True, slots=True)
class Scrub:
    """What btrfs scrub status says, as the bash version read it."""

    text: str
    started: str  # the "Scrub started" value(s), one per line
    status: str  # the second word of the "Status:" line(s)

    @property
    def errors(self) -> str:
        """The "Error summary" line(s) when they name errors, else ""."""
        if re.search(r"Error summary:.*(csum|read|verify|super)=", self.text) is None:
            return ""
        return lines_of([line for line in file_lines(self.text) if "Error summary" in line])

    @property
    def clean_finish(self) -> bool:
        """Finished, with no errors found."""
        return re.search("Status:" + _SPACE + "+finished", self.text) is not None and "no errors found" in self.text


def parse_scrub(text: str) -> Scrub:
    lines = file_lines(text)
    started = [[*re.split(r": +", line), ""][1] for line in lines if "Scrub started" in line]
    status = [awk_field(line, 2) for line in lines if line.startswith("Status:")]
    return Scrub(text, lines_of(started), lines_of(status))


def member_paths(show: str) -> list[str]:
    """The device paths btrfs filesystem show lists (the last word of each devid line)."""
    return [fields(line)[-1] for line in file_lines(show) if "devid" in line]


def disks_of(lsblk: str) -> list[str]:
    """The names lsblk -lnso NAME,TYPE gives the type disk."""
    out: list[str] = []
    for line in file_lines(lsblk):
        name, kind = read_fields(line, 2)
        if kind == "disk":
            out.append(name)
    return out


# ---- the kernel log --------------------------------------------------------------------------------------------


def kernel_problem(klog: str, ours: Sequence[str]) -> str | None:
    """The problem of the kernel's error messages since the last run. Messages about devices that are
    not ours (test images, USB disks) are left out."""
    known = " " + " ".join(ours) + " "
    bad: list[str] = []
    for line in klog.split("\n"):
        if not line or not _KERNEL_BAD.search(line):
            continue
        m = _DEVICE.search(line) or _DEV.search(line)
        if m is not None:
            dev = m.group(1)
            if f" {dev} " not in known and f" {cut_at_last(dev, 'p')} " not in known:
                continue
        bad.append(line)
    if not bad:
        return None
    examples = "".join(f"{line} | " for line in bad[:3])
    return f"kernel: {len(bad)} btrfs/IO error message(s) since last check, e.g.: {examples}"


# ---- md RAID ---------------------------------------------------------------------------------------------------


def md_problem(mdstat: str) -> str | None:
    """md RAID with a missing member: the lines grep -B1 shows, run together as tr -s ' \\n' ' ' does."""
    lines = file_lines(mdstat)
    hits = [i for i, line in enumerate(lines) if _MD_DEGRADED.search(line)]
    if not hits:
        return None
    shown: list[str] = []
    last = -2
    for i in hits:
        start = max(i - 1, last + 1)
        if shown and start > last + 1:
            shown.append("--")
        shown.extend(lines[start : i + 1])
        last = i
    return "md RAID degraded: " + re.sub(r" +", " ", "".join(line + "\n" for line in shown).replace("\n", " "))


# ---- the NVMe health log -----------------------------------------------------------------------------------------


def _smart_value(log: str, key: str) -> str | None:
    """The second colon-separated field of the first line whose first one is key (awk -F:)."""
    for line in file_lines(log):
        parts = line.split(":")
        if re.fullmatch(re.escape(key) + _SPACE + "*", parts[0]):
            return parts[1] if len(parts) > 1 else ""
    return None


def smart_raw(log: str, key: str) -> str:
    """A field of nvme smart-log as printed, without blanks (critical_warning is in hex: 0x8)."""
    v = _smart_value(log, key)
    return "" if v is None else re.sub(_SPACE, "", v)


def smart_field(log: str, key: str) -> str:
    """A field of nvme smart-log, its digits only ("100%" -> "100")."""
    v = _smart_value(log, key)
    return "" if v is None else re.sub(r"[^0-9]", "", v)


def controller(disk: str) -> str:
    """The NVMe controller of a namespace: nvme1n1 -> /dev/nvme1."""
    return "/dev/" + cut_at_last(disk, "n")


def smart_problems(disk: str, role: str, log: str) -> list[str]:
    """The problems in one disk's health log; role "swap" for a disk under a swap device."""
    who = f"NVMe {disk}{f' ({role})' if role else ''}"
    problems: list[str] = []
    crit = smart_raw(log, "critical_warning")
    if _HEX.fullmatch(crit):
        crit = str(int(crit, 16))
    elif _DIGITS.fullmatch(crit):
        crit = str(int(crit, 10))
    elif crit:
        problems.append(f"{who}: cannot read critical_warning ({crit})")
        crit = "0"
    media = smart_field(log, "media_errors")
    spare = smart_field(log, "available_spare")
    spare_thr = smart_field(log, "available_spare_threshold")
    used = smart_field(log, "percentage_used")

    def num(text: str, default: int) -> int | None:
        return default if not text else bash_int(text)

    c, m, s, t, u = num(crit, 0), num(media, 0), num(spare, 100), num(spare_thr, 0), num(used, 0)
    if c is not None and c != 0:
        problems.append(f"{who}: critical_warning={crit}")
    if m is not None and m > 0:
        problems.append(f"{who}: media_errors={media}")
    if s is not None and t is not None and s < t:
        problems.append(f"{who}: available_spare {spare}% below threshold {spare_thr}%")
    if u is not None and u >= WEAR_ALERT_PCT:
        problems.append(f"{who}: wear percentage_used={used}%")
    return problems


def swap_partitions(swaps: str) -> list[str]:
    """The block devices /proc/swaps lists as partitions (swap files and zram's are kept out later)."""
    out: list[str] = []
    for line in file_lines(swaps)[1:]:
        dev, kind, _ = read_fields(line, 3)
        if kind == "partition" and dev.startswith("/dev/"):
            out.append(dev)
    return out


# ---- the rest ------------------------------------------------------------------------------------------------------


def rule_uuid(rule: str) -> str:
    """The filesystem the last-resort udev rule is for."""
    return lines_of([m.group(1) for line in file_lines(rule) if (m := _RULE_UUID.match(line))])


def marker_problem(uuid: str, since_human: str, state: str) -> str:
    return (
        f"btrfs {uuid}: booted DEGRADED on {since_human or '?'} ({state}); restore RAID1 (replace the disk if "
        "needed), then 'btrfs balance start -dconvert=raid1,soft -mconvert=raid1,soft /var' and 'btrfs scrub start "
        "/var'. Clears after a clean scrub started once all devices are back."
    )


def awake_since(suspend_journal: str, boot_since: int) -> int:
    """Since when the machine runs without a pause: the last resume from suspend (journalctl's one
    short-unix line), or the boot."""
    lines = file_lines(suspend_journal)
    last = awk_int(lines[0]) if len(lines) == 1 else 0
    return max(boot_since, last)


def alert_body(problems: Sequence[str]) -> str:
    """The alert's text: the count, then one problem per line. (The title stays fixed: an alert kept
    for the next login is replaced by its repeat only when app and title match.)"""
    return output(f"{len(problems)} problem(s):\n" + "".join(p + "\n" for p in problems))


def ok_line(mounts: Sequence[str], disks: Sequence[str], swap: Sequence[str]) -> str:
    swap_part = f"; swap disks: {' '.join(swap)}" if " ".join(swap) else ""
    return f"OK ({' '.join(mounts)}; disks: {' '.join(disks)}{swap_part})"

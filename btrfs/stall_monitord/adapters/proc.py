# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-monitord's reads of the machine: /proc, /sys and cgroupfs read to the end of each file, a read that may wait for another program done in a helper thread with a time limit (TimedReader), the btrfs/disk/container topology, D-state tasks, the journal lines, the log folder and the writer thread of the log files."""

from __future__ import annotations

from collections.abc import Callable, Set as AbstractSet
import glob
import os
import queue
import re
import threading
import time
from typing import Final, override

from btrfs.stall_monitord.domain import (
    CGROUP_RESCAN,
    KEEP_DAYS,
    FsTopo,
)
from cst_core.adapters.files import ensure_dir
from cst_core.adapters.journal import log as journal_log
from cst_core.domain.text import cgroup_v2_path
from cst_core.domain.values import DAY, parse_integer, parse_whole_number

CGROUP_ROOT = "/sys/fs/cgroup"


def log(msg: str, prio: int = 6) -> None:
    """One journal line: msg with its sd-daemon priority prefix (cst_core's journal log)."""
    journal_log(prio, msg)


def read_bytes(path: str, size: int = 65536) -> bytes | None:
    """Up to `size` bytes of a file, or None if it cannot be read.

    Reads until end of file: one read() of a /proc file such as /proc/vmstat
    returns only about one page (4 KiB), and the rest comes in later reads.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        chunks = []
        left = size
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)


def read_text(path: str) -> str | None:
    data = read_bytes(path)
    return data.decode("utf-8", "replace") if data is not None else None


def read_kv(path: str) -> dict[str, int]:
    """The 'name value' lines of a file (/proc/vmstat, memory.stat) as {name: int}; a value that is not an
    integer in ASCII digits within int()'s limit (parse_integer) leaves its name out."""
    out: dict[str, int] = {}
    text = read_text(path)
    if text is not None and text != "":
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and (value := parse_integer(parts[1])) is not None:
                out[parts[0]] = value
    return out


def read_int(path: str) -> int:
    """The file's integer (one leading "-", then ASCII digits within int()'s limit), else 0."""
    number = parse_integer((read_text(path) or "").strip())
    return 0 if number is None else number


# An avg10 as the kernel writes it ("%lu.%02lu"): ASCII digits, an optional fraction. float() alone also
# takes "nan", "inf", "1e9", "1_0" and digits of other scripts.
_PSI_AVG: Final = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def parse_psi(path: str, *, unreadable: list[float | int] | None = None) -> list[float | int] | None:
    """Return [some_avg10, some_total_us, full_avg10, full_total_us].

    UNREADABLE (None by default) for a file that cannot be read or is empty. None for one whose avg10 or
    total is not a number as the kernel writes it (the total: ASCII digits within int()'s limit): the
    whole reading, not a 0 for one field, since the totals are cumulative and a 0 would make the next
    sample's difference a false spike. A field that is not there stays 0."""
    text = read_text(path)
    if text is None or text == "":
        return unreadable
    res: list[float | int] = [0.0, 0, 0.0, 0]
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] not in ("some", "full"):
            continue
        vals = {k: v for k, _, v in (f.partition("=") for f in fields[1:] if "=" in f)}
        idx = 0 if fields[0] == "some" else 2
        avg = vals.get("avg10", "0")
        total = parse_whole_number(vals.get("total", "0"))
        if total is None or not _PSI_AVG.fullmatch(avg):
            return None
        res[idx] = float(avg)
        res[idx + 1] = total
    return res


def parse_io_stat(path: str, devices: AbstractSet[str], *, unreadable: list[int] | None = None) -> list[int] | None:
    """Sum rbytes/wbytes of a cgroup io.stat over the given "maj:min" devices.

    Only the top-level devices (dm-crypt of each btrfs member, swap) are
    counted, otherwise every byte is counted again for the NVMe below it. UNREADABLE (None by default)
    for a file that cannot be read; None for one whose rbytes or wbytes is not ASCII digits within
    int()'s limit.
    """
    text = read_text(path)
    if text is None:
        return unreadable
    rb = wb = 0
    for line in text.splitlines():
        if line.split(" ", 1)[0] not in devices:
            continue
        for item in line.split()[1:]:
            k, _, v = item.partition("=")
            if k not in ("rbytes", "wbytes"):
                continue
            n = parse_whole_number(v)
            if n is None:
                return None
            if k == "rbytes":
                rb += n
            else:
                wb += n
    return [rb, wb]


def block_parents(name: str, seen: set[str] | None = None) -> list[str]:
    """Whole-disk ancestors of a block device (dm-0 -> nvme0n1p3 -> nvme0n1)."""
    seen = seen if seen is not None else set()
    if name in seen:
        return []
    seen.add(name)
    slaves = glob.glob(f"/sys/class/block/{name}/slaves/*")
    if slaves:
        out: list[str] = []
        for s in slaves:
            out += block_parents(os.path.basename(s), seen)
        return out
    if os.path.exists(f"/sys/class/block/{name}/partition"):
        return [os.path.basename(os.path.dirname(os.path.realpath(f"/sys/class/block/{name}")))]
    return [name]


class Topology:
    """btrfs filesystems, their devices, swap devices and container cgroups."""

    def __init__(self) -> None:
        self.fs: dict[str, FsTopo] = {}  # uuid -> {"label", "devs", "disks", "devids"}
        self.disks: dict[str, str] = {}  # disk or dm name -> role string
        self.top_devs: set[str] = set()  # "maj:min" of btrfs member devices and swap
        self.containers: dict[str, str] = {}  # short name -> cgroup dir
        self.last_cgroup_scan = -1e9
        self.refresh_fs()

    def refresh_fs(self) -> None:
        self._refresh_btrfs()
        self._refresh_swaps()

    def _refresh_btrfs(self) -> None:
        """Each btrfs filesystem: its devices and disks, label and devids; their roles and dev numbers."""
        for d in glob.glob("/sys/fs/btrfs/*-*-*-*-*"):
            uuid = os.path.basename(d)
            devs = sorted(os.listdir(f"{d}/devices")) if os.path.isdir(f"{d}/devices") else []
            disks = sorted({p for dev in devs for p in block_parents(dev)})
            label = (read_text(f"{d}/label") or "").strip()
            devids = sorted(os.listdir(f"{d}/devinfo")) if os.path.isdir(f"{d}/devinfo") else []
            self.fs[uuid] = {"label": label, "devs": devs, "disks": disks, "devids": devids}
            for name in devs + disks:
                self.disks.setdefault(name, f"btrfs:{label or uuid[:8]}")
            for name in devs:
                self.top_devs.add((read_text(f"/sys/class/block/{name}/dev") or "").strip())

    def _refresh_swaps(self) -> None:
        """The swap devices of /proc/swaps and their disks: role "swap" unless they have one."""
        for line in (read_text("/proc/swaps") or "").splitlines()[1:]:
            dev = line.split()[0] if line.split() else ""
            if dev.startswith("/dev/"):
                name = os.path.basename(os.path.realpath(dev))
                self.top_devs.add((read_text(f"/sys/class/block/{name}/dev") or "").strip())
                for n in [name, *block_parents(name)]:
                    self.disks.setdefault(n, "swap")

    def scan_cgroups(self, now: float) -> None:
        if now - self.last_cgroup_scan < CGROUP_RESCAN:
            return
        self.last_cgroup_scan = now
        found = {}
        for path in glob.glob(f"{CGROUP_ROOT}/system.slice/docker-*.scope"):
            found["docker-" + os.path.basename(path)[7:19]] = path
        for path in glob.glob(f"{CGROUP_ROOT}/machine.slice/libpod-*.scope"):
            found["libpod-" + os.path.basename(path)[7:19]] = path
        for path in glob.glob(
            f"{CGROUP_ROOT}/user.slice/user-*.slice/user@*.service/**/libpod-*.scope", recursive=True
        ):
            name = os.path.basename(path)
            if not name.startswith("libpod-conmon"):
                found["libpod-" + name[7:19]] = path
        for path in glob.glob(f"{CGROUP_ROOT}/machine.slice/machine-*.scope"):
            found[os.path.basename(path)[:40]] = path
        self.containers = found


class TimedReader:
    """Reads a file in a helper thread and waits at most `timeout` seconds for it.

    Some /proc files make the reader wait inside the kernel for another program:
    /proc/PID/cmdline takes that program's memory map lock (mmap_read_lock_killable
    in access_remote_vm), which a thread stuck in reclaim, swap or a page fault on a
    stalled disk can hold for minutes. Read in the sampling loop, such a file would
    stop the loop for as long. read() raises TimeoutError when the answer does not
    come in time, and at once while an earlier read is still waiting: one helper
    thread at a time, never more (the unit's TasksMax). Each read has a thread and
    an answer of its own, so a late answer never reaches the next read.
    """

    def __init__(self, timeout: float, read: Callable[[str], str | None] = read_text) -> None:
        self.timeout = timeout
        self._read = read
        self._waiting: threading.Thread | None = None  # a read that did not answer in time

    def read(self, path: str) -> str | None:
        """The file's text (read_text: None when it cannot be read); TimeoutError when it takes too long."""
        if self._waiting is not None and self._waiting.is_alive():
            raise TimeoutError(f"an earlier read is still waiting; {path} not read")
        answer: list[str | None] = []
        thread = threading.Thread(target=lambda: answer.append(self._read(path)), name="timed-reader", daemon=True)
        try:
            thread.start()
        except RuntimeError as e:  # no thread (the unit's TasksMax reached): not read
            raise TimeoutError(f"{path} not read: {e}") from None
        thread.join(self.timeout)
        if thread.is_alive():
            self._waiting = thread
            raise TimeoutError(f"{path} not read in {self.timeout:g} s")
        return answer[0] if answer else None  # no answer: the read raised (its thread logged it)


def short_cgroup(pid: str | int) -> str:
    """The name of the task's cgroup as an event line shows it (the last part of its cgroup v2
    path, a docker or podman scope cut to its id's first 12 characters); "?" when unreadable. A
    character that is not printable (a carriage return, an escape) is written as its Python
    escape (\\r, \\x1b): a cgroup's name may hold any character but "/" and a newline, and the
    name goes into the event text, the alert and the report's lines."""
    text = read_text(f"/proc/{pid}/cgroup")
    if text is None or text == "":
        return "?"
    path = cgroup_v2_path(text) or "/"
    leaf = path.rstrip("/").rsplit("/", 1)[-1] or "/"
    if leaf.startswith(("docker-", "libpod-")) and leaf.endswith(".scope"):
        kind, _, rest = leaf.partition("-")
        return _printable_name(f"{kind}-{rest[:12]}")
    return _printable_name(leaf[:60])


def _printable_name(name: str) -> str:
    """NAME with each character that is not printable written as its Python escape."""
    return "".join(ch if ch.isprintable() else ch.encode("unicode_escape").decode("ascii") for ch in name)


def run_slices(pid: str | int, tid: str | int) -> int | None:
    """Times the task was scheduled onto a CPU (schedstat field 3); None if
    unreadable (gone, or schedstats unavailable): then no progress is assumed."""
    s = read_text(f"/proc/{pid}/task/{tid}/schedstat")
    try:
        return int(s.split()[2]) if s is not None and s != "" else None
    except IndexError, ValueError:
        return None


def d_comm(pid: str | int, tid: str | int) -> str | None:
    """comm of the task if it is in D state, else None. comm may contain ')':
    the state follows the last one."""
    data = read_bytes(f"/proc/{pid}/task/{tid}/stat", 1024)
    if data is None or len(data) == 0:
        return None
    rp = data.rfind(b")")
    if rp > 0 and data[rp + 2 : rp + 3] == b"D":
        return data[data.find(b"(") + 1 : rp].decode("utf-8", "replace")
    return None


def scan_d_tasks() -> list[tuple[str, str, str]]:
    """[(pid, tid, comm)] of all tasks in uninterruptible sleep. Stat-only."""
    out = []
    try:
        pids = os.listdir("/proc")
    except OSError:
        return out
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            comm = d_comm(pid, tid)
            if comm is not None:
                out.append((pid, tid, comm))
    return out


# The modes the daemon gives the folders it makes for a run by hand with --log-dir, as systemd
# makes a LogsDirectory=: the log folder gets the unit's LogsDirectoryMode=, its missing parents
# 0755. (Only the mode: the folder's group is the caller's, not the unit's Group=.)
LOG_DIR_MODE: Final = 0o750
LOG_PARENT_MODE: Final = 0o755


def prepare_log_dir(log_dir: str) -> None:
    """The folder of the daily log files, made when missing: LOG_DIR_MODE, and LOG_PARENT_MODE for
    each missing parent; a folder that is there is left as it is."""
    folder = os.path.abspath(log_dir)
    ensure_dir(os.path.dirname(folder), LOG_PARENT_MODE)
    ensure_dir(folder, LOG_DIR_MODE)


class Writer(threading.Thread):
    """Appends batches to per-day files; slow writes only delay this thread."""

    def __init__(self, log_dir: str) -> None:
        super().__init__(name="writer", daemon=True)
        self.log_dir = log_dir
        self.q: queue.Queue[list[tuple[float, str]] | None] = queue.Queue(maxsize=64)
        self.last_housekeeping_day: str | None = None

    @override
    def run(self) -> None:
        while True:
            batch = self.q.get()
            if batch is None:
                return
            by_day = {}
            for ts, line in batch:
                by_day.setdefault(time.strftime("%Y-%m-%d", time.localtime(ts)), []).append(line)
            for day, lines in sorted(by_day.items()):
                path = os.path.join(self.log_dir, f"stall-{day}.jsonl")
                try:
                    with open(path, "a", encoding="utf-8") as f:
                        f.write("\n".join(lines) + "\n")
                except OSError as e:
                    log(f"cannot write {path}: {e}", 3)
            today = time.strftime("%Y-%m-%d")
            if today != self.last_housekeeping_day:
                self.last_housekeeping_day = today
                self.housekeeping()

    def housekeeping(self) -> None:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - KEEP_DAYS * DAY))
        for path in glob.glob(os.path.join(self.log_dir, "stall-*.jsonl*")):
            if os.path.basename(path)[6:16] < cutoff:
                try:
                    os.unlink(path)
                except OSError as e:
                    log(f"cannot remove {path}: {e}", 4)

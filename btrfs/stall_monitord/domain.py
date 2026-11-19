# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-monitord's rules, no I/O: thresholds and intervals, the record version and the records, what a kernel stack waits for (the frame lists, wait_kind), the episode being recorded, and the sampling loop's rules on the values it read (commits, episodes, hung and busy tasks, kernel log lines, alerts)."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple, NotRequired
from typing import TypedDict
import re

from cst_core.domain.contracts import StateFormatKey, state_format
from cst_core.domain.text import group_or_none
from cst_core.domain.values import MINUTE

# v7: reads /proc and /sys files to the end (zswap counters were 0 before).
# v8: a "memory" wait kind (reclaim, swap-in, zswap, page faults and mmap calls
#     waiting for the memory map lock). Samples count it in "d_mem", not in "d"; it opens no
#     episode; episodes record "max_d_mem". Daemons before v8 counted these
#     waits as storage.
VERSION = state_format(StateFormatKey.STALL_RECORDS).version  # the number lives in cst_core/domain/contracts.py
BASE_INTERVAL = 5.0  # s between full samples
EPISODE_INTERVAL = 1.0  # s between full samples during an episode
PROBE_INTERVAL = 1.0  # s between cheap commit_stats probes
# Kernel PSI trigger: wake up when tasks were stalled on IO for >= 200 ms
# within a 2 s window (10%). 2 s windows are also allowed for unprivileged
# users, which keeps --log-dir test runs working.
PSI_TRIGGER = b"some 200000 2000000"
ENTER_IO_SOME_PCT = 10.0  # interval IO "some" pressure that opens an episode
ENTER_COMMIT_MS = 1000  # a commit running/finishing this long opens one
ENTER_D_TASKS = 3  # this many tasks in D state open one (idle: 0-1)
QUIET_IO_SOME_PCT = 5.0
QUIET_D_TASKS = 1
QUIET_SECONDS_TO_CLOSE = 10
HUNG_SECONDS = 30  # continuous D state reported as a hung task
HUNG_REPEAT = 15 * MINUTE  # s before the same process is reported again
# A report reads the task's /proc/PID/cmdline in a helper thread (it waits for that
# program's memory map lock) and gives up after this long, so the loop goes on.
CMDLINE_WAIT = 0.5
CMDLINE_SLOW = f"cmdline not read in {CMDLINE_WAIT:g} s"
MAINT_HUNG_SECONDS = 15 * MINUTE  # continuous D state of btrfs maintenance (fstrim, scrub) that alerts
BUSY_ALERT_SECONDS = 10 * MINUTE  # in D at every sample this long alerts even while progressing
MAX_STACK_SNAPSHOTS = 600  # per episode; afterwards only counts are kept
MAX_EXAMPLE_STACKS = 3  # stored stacks per signature
FLUSH_EVERY = 10.0  # s between hand-offs to the writer thread
KEEP_DAYS = 8
ALERT_COMMIT_MS = 5000
ALERT_MIN_GAP = 15 * MINUTE  # s between alerts of the same kind
# The alert hand-off starts btrfs-stall-alert.service from a helper thread and
# waits at most this long for `systemctl --no-block start` to end once it runs
# (then kills it and waits for the kill to take effect).
ALERT_START_WAIT = 60.0
CGROUP_RESCAN = 30.0  # s between container cgroup discovery
SLICES = ("system.slice", "user.slice", "machine.slice")
VMSTAT_KEYS = (
    "nr_dirty",
    "nr_writeback",
    "nr_dirtied",
    "nr_written",
    "pgpgin",
    "pgpgout",
    "pswpin",
    "pswpout",
    "zswpin",
    "zswpout",
    "zswpwb",
    "pgscan_direct",
    "pgsteal_direct",
    "pgscan_kswapd",
    "allocstall_normal",
    "allocstall_movable",
    "workingset_refault_file",
    "workingset_refault_anon",
    "nr_dirty_threshold",
    "nr_dirty_background_threshold",
)
SPACE_KEYS = ("bytes_used", "total_bytes", "bytes_may_use", "bytes_pinned", "bytes_reserved")
KMSG_INTEREST = re.compile(
    r"BTRFS (?:critical|error|warning)|forced readonly|Transaction aborted|corrupt (?:leaf|node)"
    r"|csum failed|I/O error|nvme\S*: .*(?:timeout|reset|abort|fail)|Out of memory: Killed"
    r"|blocked for more than \d+ seconds",
    re.IGNORECASE,
)
KMSG_SEVERE = re.compile(
    r"BTRFS critical|BTRFS error|forced readonly|Transaction aborted|corrupt (?:leaf|node)"
    r"|csum failed|I/O error|nvme\S*: .*(?:timeout|reset|abort)",
    re.IGNORECASE,
)
# The device a kernel line names, as in "BTRFS error (device dm-0 state EA): ..."
# or "I/O error, dev nvme0n1, sector ...".
KMSG_DEVICE = re.compile(r"\(device ([\w.-]+)[ )]|\bdev ([\w.-]+),")
GENERIC_FRAME = re.compile(
    r"^(?:__schedule|schedule|schedule_timeout|schedule_timeout_uninterruptible"
    r"|schedule_preempt_disabled|io_schedule|io_schedule_timeout|msleep|usleep_range_state"
    r"|__wait_for_common|wait_for_completion\w*|rwsem_down_\w+|down_read\w*|down_write\w*"
    r"|__down\w*|mutex_lock\w*|__mutex_lock\w*|rt_mutex\w*|folio_wait_bit_common"
    r"|wait_on_page_bit_common|bit_wait_io|out_of_line_wait_on_bit\w*|__wait_on_bit\w*"
    r"|prepare_to_wait\w*|percpu_rwsem_wait|__percpu_down_read\w*|__flush_work|flush_work"
    r"|wait_woken|__wait_event\w*|finish_wait|do_wait_intr\w*|kthread|ret_from_fork\w*"
    r"|entry_SYSCALL\w*|do_syscall_64|x64_sys_call|__x64_sys_\w+|syscall_exit\w*"
    r"|exc_page_fault|asm_exc_page_fault|do_user_addr_fault|handle_mm_fault|__handle_mm_fault"
    r"|handle_pte_fault|do_fault|worker_thread|process_one_work|process_scheduled_works"
    r"|wb_workfn|io_wq_worker|io_worker_handle_work)$"
)


def parse_stack(text: str | None) -> list[str]:
    frames = []
    for line in (text or "").splitlines():
        # A line reads like "[<0>] btrfs_commit_transaction+0x7c3/0xb40 [btrfs]".
        part = line.split("] ", 1)[-1].strip()
        if part:
            frames.append(part.split("+", 1)[0].split(" ", 1)[0])
    return frames


def stack_signature(frames: Sequence[str], wchan: str | None) -> tuple[str, str]:
    """Innermost non-generic frame plus the syscall/context it came from."""
    if not frames:
        return (f"wchan:{wchan}" if wchan is not None and wchan != "" else "no-stack"), ""
    sig = next((f for f in frames if not GENERIC_FRAME.match(f)), frames[0])
    ctx = ""
    for f in frames:
        if f.startswith("__x64_sys_"):
            ctx = f[len("__x64_sys_") :]
    if not ctx:
        if "exc_page_fault" in frames or "handle_mm_fault" in frames:
            ctx = "page_fault"
        elif not KWORKER_FRAMES.isdisjoint(frames):
            ctx = "kworker"
        elif "kthread" in frames:
            ctx = "kthread"
    return sig, ctx


KWORKER_FRAMES = frozenset({"worker_thread", "process_one_work", "wb_workfn"})
FUSE_FRAME = re.compile(r"^(?:request_wait_answer|fuse_\w+)$")
# Interruptible-looking sleeps; in D state only when caught frozen. A task
# whose stack shows one of these as the innermost meaningful frame is not
# waiting on storage.
IDLE_FRAME = re.compile(
    r"^(?:futex_\w+|__futex_\w+|ep_poll|do_epoll_wait|do_select|do_sys_poll|usb_\w+|do_nanosleep"
    r"|hrtimer_nanosleep|pipe_\w+|unix_\w+|n_tty_\w+|tty_\w+|wait_for_partner|do_wait|kernel_wait4"
    r"|rcu_\w+|synchronize_rcu\w*|devtmpfsd|wait_for_vfork_done|do_freezer_trap|__refrigerator"
    r"|poll_schedule_timeout\w*|intel_\w+|drm_\w+|i915_\w+|xe_\w+|pci_\w+)$"
)
# btrfs housekeeping anywhere on the stack: FITRIM (fstrim), async discard
# workers, scrub, balance/relocation and zoned/auto reclaim. These issue IO
# back to back for minutes by design; they are not a workload stall.
# Deliberately absent: btrfs_discard_extent/btrfs_issue_discard (also reached
# from a transaction commit with discard=sync, a real workload wait; the trim
# and discard-worker callers above them are matched instead) and
# btrfs_scrub_pause/cancel (a commit or unmount waiting for scrub to pause).
MAINT_FRAME = re.compile(
    r"^(?:btrfs_ioctl_fitrim|btrfs_trim_\w+|do_trimming|trim_bitmaps|trim_no_bitmap"
    r"|btrfs_discard_workfn|btrfs_ioctl_scrub|btrfs_scrub_dev|scrub_\w+|flush_scrub_stripes"
    r"|btrfs_ioctl_balance|btrfs_balance\w*|btrfs_relocate_\w+|relocate_\w+|btrfs_reclaim_bgs_work)$"
)
NEW_KTHREAD = {"kthread", "ret_from_fork", "ret_from_fork_asm"}
# Memory waits. The swap path anywhere on the stack: a page fault that reads
# a page back from swap or from the zswap pool, or reclaim that writes one
# out, waits because memory ran short, even when the innermost frame is the
# block layer of the swap disk.
SWAP_FRAME = re.compile(
    r"^(?:do_swap_page|swap_read_folio\w*|swap_cluster_readahead|swapin_readahead|swap_vma_readahead"
    r"|__read_swap_cache_async|read_swap_cache_async|__swap_writepage|swap_writepage\w*|swap_writeout"
    r"|zswap_\w+)$"
)
# Reclaim, allocation and memory-cgroup throttling: these decide only as the
# innermost frame that names a subsystem (see wait_kind), because reclaim can
# call into btrfs (a shrinker evicting an inode) and then wait on btrfs.
MEM_FRAME = re.compile(
    r"^(?:shrink_(?:node|lruvec|list|inactive_list|active_list|folio_list|zones|slab|many|one)\w*"
    r"|do_shrink_slab|try_to_free_(?:pages|mem_cgroup_pages)|throttle_direct_reclaim|reclaim_throttle|__perform_reclaim"
    r"|__alloc_pages\w*|alloc_pages\w*|__alloc_frozen_pages\w*|__folio_alloc\w*"
    r"|mem_cgroup_handle_over_high|try_charge_memcg|charge_memcg|__mem_cgroup_charge\w*"
    r"|try_to_compact_pages|compact_zone\w*|__alloc_pages_direct_compact|kcompactd\w*)$"
)
# Waits for the process's memory map lock (mmap_lock, or a VMA lock): a page
# fault (lock_mm_and_find_vma), or mmap/munmap/mprotect/madvise waiting for
# the faulting threads to leave. Seen in JVM GC threads while other threads
# fault through zswap. Only as the innermost meaningful frame (mmap of a file
# can also wait inside the filesystem). Memory, unless the same process has a
# thread waiting on storage or FUSE (see describe_all).
MMAP_LOCK_FRAME = re.compile(
    r"^(?:lock_mm_and_find_vma|__vma_start_exclude_readers|__vma_start_write|vma_start_write\w*"
    r"|vm_mmap_pgoff|__vm_munmap|vm_munmap|do_mprotect_pkey|do_madvise|__do_sys_brk)$"
)
# Frames that prove a wait is on the local block stack, not on a FUSE daemon
# (and, as the innermost such frame, not on memory reclaim).
LOCAL_IO_FRAME = re.compile(
    r"^(?:btrfs_\w+|blk_\w+|bio_\w+|submit_bio\w*|nvme_\w+|dm_\w+|kcryptd\w*"
    r"|folio_wait_writeback|filemap_fdatawait\w*|balance_dirty_pages\w*)$"
)


def wait_kind(frames: Sequence[str], wchan: str | None) -> str:
    """ "fuse", "maintenance", "memory", "idle" or "storage" for a task in D state.

    Precedence: FUSE (the wait is on a userspace daemon even if btrfs is
    below it), then btrfs maintenance, then the swap path anywhere on the
    stack (memory), then the innermost meaningful frame: idle-ish (idle) or
    a memory map lock (memory), then the innermost frame that names a
    subsystem: memory reclaim/allocation (memory) or btrfs and the block
    layer (storage).
    Conservative: anything not positively idle or memory counts as storage,
    e.g. io_destroy waiting for in-flight AIO, whose frames are all generic."""
    if frames:
        if any(FUSE_FRAME.match(f) for f in frames):
            return "fuse"
        if any(MAINT_FRAME.match(f) for f in frames):
            return "maintenance"
        if any(SWAP_FRAME.match(f) for f in frames):
            return "memory"
        meaningful = [f for f in frames if not GENERIC_FRAME.match(f)]
        if not meaningful:
            # A freshly created kthread waits in D inside kthread() until its first wakeup.
            return "idle" if set(frames) <= NEW_KTHREAD else "storage"
        if IDLE_FRAME.match(meaningful[0]):
            return "idle"
        if MMAP_LOCK_FRAME.match(meaningful[0]):
            return "memory"
        for f in meaningful:
            if MEM_FRAME.match(f):
                return "memory"
            if LOCAL_IO_FRAME.match(f):
                return "storage"
        return "storage"
    if wchan is not None and wchan != "":
        if FUSE_FRAME.match(wchan):
            return "fuse"
        if MAINT_FRAME.match(wchan):
            return "maintenance"
        if SWAP_FRAME.match(wchan) or MEM_FRAME.match(wchan) or MMAP_LOCK_FRAME.match(wchan):
            return "memory"
        return "idle" if IDLE_FRAME.match(wchan) else "storage"
    return "storage"


def mmap_lock_only(frames: Sequence[str], wchan: str | None) -> bool:
    """True if a "memory" wait is only a wait for the memory map lock (no
    reclaim or swap of its own): its innermost meaningful frame (or wchan)."""
    if not frames:
        return bool(wchan and MMAP_LOCK_FRAME.match(wchan))
    meaningful = [f for f in frames if not GENERIC_FRAME.match(f)]
    return (
        bool(meaningful) and bool(MMAP_LOCK_FRAME.match(meaningful[0])) and not any(SWAP_FRAME.match(f) for f in frames)
    )


# Timed sleeps wake up periodically (and so get scheduled) while still
# waiting: a timeslice count change there is not progress.
TIMED_WAIT_FRAME = re.compile(
    r"^(?:balance_dirty_pages\w*|io_schedule_timeout|reclaim_throttle|schedule_timeout\w*|msleep\w*"
    r"|usleep_range\w*|schedule_hrtimeout\w*|wait_for_completion_\w*timeout\w*)$"
)


HUNG_TASK = "hung task"  # the reason of an episode a hung task opened


class StallRecordTag(StrEnum):
    """The kind of a stall log record, its "t" key: the daemon writes it, btrfs-stall-report reads it."""

    META = "meta"
    SAMPLE = "s"
    EPISODE = "ep"
    EVENT = "ev"
    ALERT = "al"


# One D-state task as the sampling sees it: (pid, tid, comm, frames, wchan, wait kind).
type DTask = tuple[str, str, str, list[str], str | None, str]


class SigStats(TypedDict):
    """What an episode counted for one wait signature ("sig|ctx")."""

    n: int
    kind: str
    comms: dict[str, int]
    cgroups: dict[str, int]


class SigOut(TypedDict):
    sig: str
    ctx: str
    n: int
    kind: str
    comms: dict[str, int]
    cgroups: dict[str, int]
    examples: list[list[str]]


class EpisodeOut(TypedDict):
    """The episode record ("t": "ep")."""

    t: str
    ts: float
    start: float
    end: float
    reason: str
    snapshots: int
    max_d: int
    max_d_mem: int
    max_d_all: int
    peak: dict[str, float]
    sigs: list[SigOut]


class FsTopo(TypedDict):
    """One btrfs filesystem of the topology (the meta record's "fs", by UUID)."""

    label: str
    devs: list[str]
    disks: list[str]
    devids: list[str]


class MetaOut(TypedDict):
    """The meta record ("t": "meta"): at the start and at each new day."""

    t: str
    ts: float
    v: int
    kernel: str
    boot_id: str
    base_interval: float
    fs: dict[str, FsTopo]
    disks: dict[str, str]
    vm_keys: tuple[str, ...]
    space_keys: tuple[str, ...]
    dirty_bytes: int
    dirty_background_bytes: int
    wbt_lat_usec: dict[str, int]
    stacks: bool
    kmsg: bool
    hung_task_detector: bool
    hung_seconds: int


class FsSampleOut(TypedDict):
    c: list[int]  # commits, total ms, current ms, last ms, max ms, longest since the last sample
    err: dict[str, int]
    space: dict[str, list[int]]
    discard: list[int]


class SampleOut(TypedDict):
    """A sample ("t": "s")."""

    t: str
    ts: float
    mono: float
    psi: dict[str, list[float | int] | None]
    vm: list[int]
    procs: list[int]
    d: int
    d_all: int
    d_fuse: int
    d_maint: int
    d_mem: int
    load1: float
    fs: dict[str, FsSampleOut]
    disk: dict[str, list[int]]
    cg: dict[str, list[float | int]]


class EventOut(TypedDict):
    """An event ("t": "ev"): a kernel message, a hung, busy or waiting task."""

    t: str
    ts: float
    msg: str
    severe: bool
    comm: NotRequired[str]  # io-busy events
    ours: bool
    kind: str
    stack: NotRequired[list[str]]  # task events


class AlertOut(TypedDict):
    """An alert ("t": "al"): what was handed to btrfs-notify."""

    t: str
    ts: float
    kind: str
    level: str
    title: str
    body: str


type RecordOut = SampleOut | MetaOut | EventOut | AlertOut | EpisodeOut


class DTaskSince(TypedDict):
    first: float
    busy: float
    comm: str
    pid: str
    slices: int | None


class Episode:
    """The stall episode being recorded: its peaks and the wait signatures of its D-state tasks."""

    def __init__(self, ts: float, reason: str) -> None:
        self.start = ts
        self.end = ts
        self.reason = reason
        self.quiet = 0
        self.snapshots = 0
        self.max_d = 0  # storage waits only
        self.max_d_mem = 0  # memory waits (reclaim, swap, zswap)
        self.max_d_all = 0
        self.peak: dict[str, float] = {"io_some_avg10": 0.0, "io_full_avg10": 0.0, "iowait_tasks": 0, "commit_ms": 0}
        self.sigs: dict[str, SigStats] = {}  # "sig|ctx" -> {"n", "kind", "comms", "cgroups"}
        self.examples: dict[str, list[list[str]]] = {}  # "sig|ctx" -> [stack frames lists]

    def observe(self, ts: float, io: Sequence[float], iowait_tasks: int, commit_ms: int) -> None:
        """One sample in the episode: its end and the peaks."""
        self.end = ts
        self.peak["io_some_avg10"] = max(self.peak["io_some_avg10"], io[0])
        self.peak["io_full_avg10"] = max(self.peak["io_full_avg10"], io[2])
        self.peak["iowait_tasks"] = max(self.peak["iowait_tasks"], iowait_tasks)
        self.peak["commit_ms"] = max(self.peak["commit_ms"], commit_ms)

    def add_snapshot(self, d_tasks: Sequence[DTask], cgroup_of: Callable[[str], str]) -> None:
        """The D-state tasks of one sample (cgroup_of: a task's short cgroup, asked once per process)."""
        self.snapshots += 1
        counts = wait_counts(d_tasks)
        self.max_d = max(self.max_d, counts.d)
        self.max_d_mem = max(self.max_d_mem, counts.d_mem)
        self.max_d_all = max(self.max_d_all, counts.d_all)
        if self.snapshots > MAX_STACK_SNAPSHOTS:
            return
        cg_cache: dict[str, str] = {}
        for pid, _tid, comm, frames, wchan, kind in d_tasks:
            sig, ctx = stack_signature(frames, wchan)
            key = f"{sig}|{ctx}"
            if pid not in cg_cache:
                cg_cache[pid] = cgroup_of(pid)
            entry = self.sigs.setdefault(key, {"n": 0, "kind": kind, "comms": {}, "cgroups": {}})
            entry["n"] += 1
            entry["comms"][comm] = entry["comms"].get(comm, 0) + 1
            entry["cgroups"][cg_cache[pid]] = entry["cgroups"].get(cg_cache[pid], 0) + 1
            ex = self.examples.setdefault(key, [])
            if frames and len(ex) < MAX_EXAMPLE_STACKS and frames[:16] not in ex:
                ex.append(frames[:16])

    def quiet_step(self, io_pct: float, d: int, cur_commit: int) -> bool:
        """Count one quiet sample (or start over); True once the episode is quiet long enough to close."""
        if io_pct < QUIET_IO_SOME_PCT and d <= QUIET_D_TASKS and cur_commit < ENTER_COMMIT_MS // 2:
            self.quiet += 1
        else:
            self.quiet = 0
        return self.quiet >= QUIET_SECONDS_TO_CLOSE

    def is_stall(self) -> bool:
        """PSI triggers also fire on harmless 200 ms blips; only an episode that
        shows an actual stall is recorded (the 1 s samples are stored either way)."""
        return not (
            self.peak["io_some_avg10"] < QUIET_IO_SOME_PCT
            and self.max_d < ENTER_D_TASKS
            and self.peak["commit_ms"] < ENTER_COMMIT_MS
            and self.reason != HUNG_TASK
        )

    def to_record(self) -> EpisodeOut:
        out: list[SigOut] = []
        for key, v in sorted(self.sigs.items(), key=lambda kv: -kv[1]["n"])[:25]:
            sig, ctx = key.split("|", 1)
            out.append(
                {
                    "sig": sig,
                    "ctx": ctx,
                    "n": v["n"],
                    "kind": v.get("kind", "storage"),
                    "comms": dict(sorted(v["comms"].items(), key=lambda kv: -kv[1])[:6]),
                    "cgroups": dict(sorted(v["cgroups"].items(), key=lambda kv: -kv[1])[:6]),
                    "examples": self.examples.get(key, []),
                }
            )
        return {
            "t": StallRecordTag.EPISODE,
            "ts": round(self.start, 3),
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "reason": self.reason,
            "snapshots": self.snapshots,
            "max_d": self.max_d,
            "max_d_mem": self.max_d_mem,
            "max_d_all": self.max_d_all,
            "peak": self.peak,
            "sigs": out,
        }

    def summary(self, rec: EpisodeOut) -> str:
        """The journal line of the recorded episode."""
        top = rec["sigs"][:1]
        top_s = f"{top[0]['sig']} x{top[0]['n']}" if top else "no D tasks"
        return (
            f"episode {self.end - self.start:.0f}s ({self.reason}); peak io_full_avg10="
            f"{self.peak['io_full_avg10']:.0f}% commit={self.peak['commit_ms']}ms max_D={self.max_d}; top: {top_s}"
        )


# ---- the rules of the sampling loop, on values already read ------------------


def settle_kinds(tasks: list[DTask]) -> list[DTask]:
    """The wait kinds of one sample's D-state tasks, settled across the threads of a process.

    Threads of a process that also has a thread inside FUSE, blocked on a VFS
    lock that thread holds (d_alloc_parallel, inode rwsem), wait on FUSE too,
    unless their stack shows the local block layer or btrfs. A thread waiting
    for the memory map lock waits for whatever the holder (another thread of
    the process) waits on: memory if a thread of the process is in reclaim or
    swap itself (a JVM whose threads fault through zswap, even with one
    thread in fsync), else storage or FUSE if a thread of the process waits
    there, memory otherwise.
    """
    out = list(tasks)
    fuse_pids = {t[0] for t in out if t[5] == "fuse"}
    for i, t in enumerate(out):
        if t[5] == "storage" and t[0] in fuse_pids and not any(LOCAL_IO_FRAME.match(f) for f in t[3]):
            out[i] = (t[0], t[1], t[2], t[3], t[4], "fuse")
    lock_only = [i for i, t in enumerate(out) if t[5] == "memory" and mmap_lock_only(t[3], t[4])]
    lock_tids = {out[i][1] for i in lock_only}
    mem_pids = {t[0] for t in out if t[5] == "memory" and t[1] not in lock_tids}
    storage_pids = {t[0] for t in out if t[5] == "storage"}
    fuse_pids = {t[0] for t in out if t[5] == "fuse"}
    for i in lock_only:
        t = out[i]
        if t[0] in mem_pids:
            continue
        if t[0] in storage_pids:
            out[i] = (t[0], t[1], t[2], t[3], t[4], "storage")
        elif t[0] in fuse_pids:
            out[i] = (t[0], t[1], t[2], t[3], t[4], "fuse")
    return out


class WaitCounts(NamedTuple):
    """The waits of one sample's D-state tasks: "d" counts the tasks waiting on
    storage; "d_all" also FUSE, maintenance, memory and idle-ish waits."""

    d: int
    d_all: int
    d_fuse: int
    d_maint: int
    d_mem: int


def wait_counts(d_tasks: Sequence[DTask]) -> WaitCounts:
    """The storage, all, FUSE, maintenance and memory waits of one sample."""
    return WaitCounts(
        d=sum(1 for t in d_tasks if t[5] == "storage"),
        d_all=len(d_tasks),
        d_fuse=sum(1 for t in d_tasks if t[5] == "fuse"),
        d_maint=sum(1 for t in d_tasks if t[5] == "maintenance"),
        d_mem=sum(1 for t in d_tasks if t[5] == "memory"),
    )


def io_some_pct(prev: SampleOut | None, rec: SampleOut) -> float:
    """IO "some" pressure between two samples, in % of the time between them."""
    if not prev:
        return 0.0
    io, prev_io = rec["psi"]["io"], prev["psi"]["io"]
    if io is None or len(io) == 0 or prev_io is None or len(prev_io) == 0:
        return 0.0
    dt = rec["mono"] - prev["mono"]
    if dt <= 0:
        return 0.0
    return (io[1] - prev_io[1]) / 1e6 / dt * 100


def commit_long_ms(cs: Mapping[str, int], prev: tuple[int, int, int] | None) -> int:
    """The longest commit one commit_stats probe shows (prev: (commits, total ms, max ms) of the last probe).

    last_commit_ms counts only when a commit actually finished (otherwise it
    repeats an old value forever); several commits that finished within one
    probe give their mean as a floor; a new max_commit_ms counts too.
    """
    commits, total = cs.get("commits", 0), cs.get("total_commit_ms", 0)
    cur, last, mx = cs.get("cur_commit_ms", 0), cs.get("last_commit_ms", 0), cs.get("max_commit_ms", 0)
    long_ms = cur
    if prev and commits > prev[0]:
        long_ms = max(long_ms, last)
        if total - prev[1] >= ENTER_COMMIT_MS and commits - prev[0] > 1:
            long_ms = max(long_ms, (total - prev[1]) // (commits - prev[0]))
        if prev[2] < mx:
            long_ms = max(long_ms, mx)
    return long_ms


def commit_alert(label: str, cur_ms: int, long_ms: int) -> tuple[str, str]:
    """(title, body) of the alert for a long commit."""
    state = "still running after" if cur_ms >= ALERT_COMMIT_MS else "took"
    return (
        f"btrfs commit stall on {label}",
        (
            f"A transaction commit {state} {long_ms / 1000:.1f} s. "
            f"Ask an agent or run: btrfs-stall-report --since '10 min ago'"
        ),
    )


def episode_reason(want: str | None, io_pct: float, d: int, commit_ms: int) -> str | None:
    """What opens an episode at this sample (want: the probe's reason), or None."""
    reason = want
    if io_pct >= ENTER_IO_SOME_PCT:
        reason = f"io_some {io_pct:.0f}%"
    elif d >= ENTER_D_TASKS:
        reason = f"{d} tasks waiting on storage"
    elif commit_ms >= ENTER_COMMIT_MS:
        reason = f"commit {commit_ms} ms"
    return reason


def stuck_since(
    prev: DTaskSince | None, pid: str, slices: int | None, frames: Sequence[str], now: float
) -> tuple[float, float]:
    """(first, busy) of a task seen in D: 'busy' = in D at every sample since
    then; 'first' = in D since then *without running in between* (stuck).
    Timed sleeps wake up periodically (and so get scheduled) while still
    waiting: a timeslice count change there is not progress."""
    if prev and prev["pid"] == pid:
        moved = (
            slices is not None
            and prev["slices"] is not None
            and slices != prev["slices"]
            and not any(TIMED_WAIT_FRAME.match(f) for f in frames or ())
        )
        return (now if moved else prev["first"]), prev["busy"]
    return now, now


def hung_start(kind: str, t: DTaskSince, now: float) -> float:
    """Since when a task counts as waiting: FUSE waits and the maintenance note
    keep the old meaning (time in D, progressing or not: a slow network walk
    does progress); the stuck alerts need time in D without progress."""
    first = t["busy"] if kind == "fuse" else t["first"]
    if kind == "maintenance" and now - t["first"] < MAINT_HUNG_SECONDS:
        first = t["busy"]
    return first


def hung_key(kind: str, t: DTaskSince, now: float) -> str:
    """The kind of report: btrfs maintenance gives one informational event at
    HUNG_SECONDS and an alert only after MAINT_HUNG_SECONDS without any
    progress (keyed apart, so the HUNG_REPEAT dedupe of the event does not
    delay the alert); workload tasks queued behind it are "storage"."""
    if kind == "maintenance" and now - t["first"] >= MAINT_HUNG_SECONDS:
        return "maintenance-hung"
    return kind


def wait_at(frames: Sequence[str], wchan: str | None) -> str:
    """Where a task waits: its signature, and the syscall or context it came from."""
    sig, ctx = stack_signature(frames, wchan)
    return f"{sig} via {ctx}" if ctx else sig


def cmdline_text(raw: str | None) -> str:
    """A /proc/PID/cmdline as one line of at most 160 characters."""
    return (raw or "").replace("\0", " ").strip()[:160]


def task_where(comm: str, tid: str, cmd: str, cgroup: str) -> str:
    return f"{comm}:{tid} ({cmd or 'no cmdline'}; cgroup {cgroup})"


@dataclass(frozen=True, slots=True)
class HungReport:
    """What one long D-state wait makes: an event, maybe an alert (kind, level, title) and an episode."""

    kind: str
    msg: str
    severe: bool
    ours: bool
    alert: tuple[str, str, str] | None
    opens_episode: bool


def hung_report(key: str, where: str, waited: float, at: str) -> HungReport:
    """The report of a task in D for HUNG_SECONDS or more (key: hung_key())."""
    if key == "maintenance":
        msg = (
            f"{where} in btrfs maintenance (trim/discard/scrub/balance) for {waited:.0f}s at {at}: not a workload stall"
        )
        return HungReport("maintenance-wait", msg, severe=False, ours=True, alert=None, opens_episode=False)
    if key == "maintenance-hung":
        msg = f"{where} in btrfs maintenance, D state for {waited:.0f}s at {at}"
        alert = ("maintenance-hung", "warning", "btrfs maintenance (fstrim/scrub) stuck for 15 min")
        return HungReport("maintenance-hung", msg, severe=True, ours=True, alert=alert, opens_episode=True)
    if key == "fuse":
        msg = (
            f"{where} waited {waited:.0f}s on a FUSE filesystem (gvfs share or document "
            f"portal): usually the network or the FUSE daemon, not the local disks (check the "
            f"report for a storage wait at the same time); at {at}"
        )
        alert = ("fuse", "normal", "Program waiting on a FUSE filesystem (network share / portal)")
        return HungReport("fuse-wait", msg, severe=False, ours=False, alert=alert, opens_episode=False)
    if key == "memory":
        # Not a disk or filesystem problem, so not severe and no storage
        # episode; the report shows it with the memory waits.
        msg = (
            f"{where} waited {waited:.0f}s without progress in memory reclaim / swap "
            f"(too little free memory, not the disks) at {at}"
        )
        alert = ("hung-memory", "warning", "Task stuck in an uninterruptible wait (memory reclaim / swap)")
        return HungReport("hung-memory", msg, severe=False, ours=True, alert=alert, opens_episode=False)
    msg = f"{where} in D state for {waited:.0f}s at {at}"
    alert = ("hung", "warning", "Task stuck in an uninterruptible wait (likely storage)")
    return HungReport("hung", msg, severe=True, ours=True, alert=alert, opens_episode=True)


def io_busy_key(busy_s: float) -> str:
    return "io-busy-long" if busy_s >= BUSY_ALERT_SECONDS else "io-busy"


def io_busy_message(where: str, busy_s: float, at: str) -> str:
    return (
        f"{where} waited on storage at "
        f"every sample for {busy_s:.0f}s while still being scheduled (progressing, e.g. an fsync loop; "
        f"check the window's storage metrics) at {at}"
    )


@dataclass(frozen=True, slots=True)
class KmsgLine:
    """A kernel log line worth recording: severe (an alert) only on this host's disks."""

    msg: str
    severe: bool
    ours: bool


def kmsg_line(data: bytes, members: Collection[str]) -> KmsgLine | None:
    """One /dev/kmsg record, if it is of interest (members: the disks and devices of the topology)."""
    msg = data.decode("utf-8", "replace").partition(";")[2].split("\n", 1)[0]
    if not KMSG_INTEREST.search(msg):
        return None
    m = KMSG_DEVICE.search(msg)
    dev = (group_or_none(m, 1) or group_or_none(m, 2)) if m else None
    ours = dev is None or dev in members or re.sub(r"p\d+$", "", dev) in members
    return KmsgLine(msg, severe=bool(KMSG_SEVERE.search(msg)) and ours, ours=ours)


def alert_text(level: str, title: str, body: str) -> str:
    """The plain-text queue file for /usr/local/sbin/btrfs-notify --queue:
    line 1 urgency, line 2 title, rest body."""
    return f"{'critical' if level == 'crit' else 'normal'}\n{title.replace(chr(10), ' ')}\n{body}\n"

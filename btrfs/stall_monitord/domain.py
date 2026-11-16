"""btrfs-stall-monitord's rules, no I/O: thresholds and intervals, the record version and keys, what a kernel stack waits for (the frame lists, wait_kind), and the episode being recorded."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from typing import TypedDict
import re

# v7: reads /proc and /sys files to the end (zswap counters were 0 before).
# v8: a "memory" wait kind (reclaim, swap-in, zswap, page faults and mmap calls
#     waiting for the memory map lock). Samples count it in "d_mem", not in "d"; it opens no
#     episode; episodes record "max_d_mem". Daemons before v8 counted these
#     waits as storage.
VERSION = 8
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
HUNG_REPEAT = 15 * 60  # s before the same process is reported again
MAINT_HUNG_SECONDS = 15 * 60  # continuous D state of btrfs maintenance (fstrim, scrub) that alerts
BUSY_ALERT_SECONDS = 10 * 60  # in D at every sample this long alerts even while progressing
MAX_STACK_SNAPSHOTS = 600  # per episode; afterwards only counts are kept
MAX_EXAMPLE_STACKS = 3  # stored stacks per signature
FLUSH_EVERY = 10.0  # s between hand-offs to the writer thread
KEEP_DAYS = 8
ALERT_COMMIT_MS = 5000
ALERT_MIN_GAP = 15 * 60  # s between alerts of the same kind
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
# "BTRFS error (device dm-0 state EA): ..." / "I/O error, dev nvme0n1, sector ..."
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
        # "[<0>] btrfs_commit_transaction+0x7c3/0xb40 [btrfs]"
        part = line.split("] ", 1)[-1].strip()
        if part:
            frames.append(part.split("+", 1)[0].split(" ", 1)[0])
    return frames


def stack_signature(frames: Sequence[str], wchan: str | None) -> tuple[str, str]:
    """Innermost non-generic frame plus the syscall/context it came from."""
    if not frames:
        return (f"wchan:{wchan}" if wchan else "no-stack"), ""
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
    if wchan:
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


class Episode:
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
        self.sigs: dict[str, dict[str, Any]] = {}  # "sig|ctx" -> {"n", "comms", "cgroups"}
        self.examples: dict[str, list[list[str]]] = {}  # "sig|ctx" -> [stack frames lists]

    def to_record(self) -> dict[str, Any]:
        out = []
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
            "t": "ep",
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


class _DTaskSince(TypedDict):
    first: float
    busy: float
    comm: str
    pid: str
    slices: int | None

"""btrfs-stall-report's rules, no I/O: the record formats, the wait categories and hints, classifying a stack, the open episode, the verdict lines and number formats."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from typing import NotRequired
from typing import Required
from typing import TypedDict
from typing import cast
import re
import time

from btrfs.stall_monitord.domain import (
    FUSE_FRAME as FUSE_RE,
    HUNG_SECONDS,
    MAINT_FRAME as MAINT_RE,
    MMAP_LOCK_FRAME,
    SWAP_FRAME,
)

GAP_MAX = 30.0  # s; a larger gap between samples means the daemon was not running
FRESH_MAX = 25.0  # s; newer data may still be buffered in the daemon
PAGE = 4096
# /proc/vmstat keys that daemons before v7 always stored as 0 (they sit after
# the first 4 KiB of the file, and those daemons read only one page).
ZSWAP_KEYS = ("zswpin", "zswpout", "zswpwb")
DISK_FIELDS = (
    "rd_ios",
    "rd_merges",
    "rd_sectors",
    "rd_ticks",
    "wr_ios",
    "wr_merges",
    "wr_sectors",
    "wr_ticks",
    "inflight",
    "io_ticks",
    "time_in_queue",
    "dc_ios",
    "dc_merges",
    "dc_sectors",
    "dc_ticks",
    "fl_ios",
    "fl_ticks",
)
DI = {k: i for i, k in enumerate(DISK_FIELDS)}
DISK_SUM = (
    "rd_ios",
    "wr_ios",
    "rd_ticks",
    "wr_ticks",
    "rd_sectors",
    "wr_sectors",
    "io_ticks",
    "time_in_queue",
    "fl_ios",
    "fl_ticks",
)
# Rules that look at the whole stack first: the reason a task waits is often
# several frames below the innermost wait (a commit waiting for ordered extents
# is still a commit; a page fault waiting for swap-in is reclaim, not a read).
# The frame lists the daemon uses too come from btrfs.stall_monitord.domain,
# so the report classifies old and new recordings with the daemon's lists:
# - FUSE_RE (the daemon's FUSE_FRAME): a FUSE filesystem (gvfs network shares,
#   document portal) answers from a userspace daemon: the wait is on that
#   daemon / the network, not the disks.
# - MAINT_RE (the daemon's MAINT_FRAME): btrfs housekeeping (fstrim/FITRIM,
#   async discard workers, scrub, balance): background work that sits in D
#   state for minutes by design (daemon v5+ tags these waits "maintenance");
#   matching it here also re-classifies older recordings.
# Old "hung" events/alerts (daemon <= v4) of maintenance tasks, for records
# without a usable stack (v2 events, alerts): the task's comm or its systemd
# unit. Alerts carry the same text as their event.
MAINT_TEXT = re.compile(r"(?:^|task )fstrim:\d+|cgroup (?:fstrim|btrfs-scrub|btrfs-balance)[\w@.-]*\.service")
STACK_RULES = [
    ("fuse", FUSE_RE),
    ("maintenance", MAINT_RE),
    # The swap path anywhere on the stack: the daemon's own list.
    ("reclaim", SWAP_FRAME),
    # A log sync can fall back to a full commit: then commit_transaction is on the stack.
    ("commit", re.compile(r"^(?:btrfs_commit_transaction|btrfs_wait_for_commit)$")),
    ("log-sync", re.compile(r"^btrfs_sync_log$")),
]
# Then the innermost meaningful frame, first match wins.
CATEGORIES = [
    ("maintenance", MAINT_RE.pattern, "btrfs maintenance (fstrim/discard, scrub, balance)"),
    ("zstd-workspace", r"zstd|get_workspace|find_workspace", "waiting for a btrfs zstd compression workspace"),
    ("dirty-throttle", r"^balance_dirty_pages", "writers throttled by the dirty-page limit (vm.dirty_bytes)"),
    (
        "commit",
        r"commit_transaction|transaction_kthread|wait_current_trans|wait_for_commit|btrfs_sync_fs|join_transaction"
        r"|start_transaction",
        "btrfs transaction commit",
    ),
    ("log-sync", r"tree_log|sync_log|wait_log_commit|wait_for_writer", "fsync log-tree sync (btrfs_sync_log)"),
    (
        "flush",
        r"barrier_all_devices|write_dev_flush|wait_dev_flush|wait_dev_supers|blkdev_issue_flush"
        r"|write_all_supers",
        "disk cache flushes (fsync / commit barriers on every RAID1 member)",
    ),
    (
        "space",
        r"reserve_metadata|reserve_data|reserve_ticket|flush_space|btrfs_reserve|reserve_space"
        r"|delalloc_reserve",
        "btrfs space reservation / flushing",
    ),
    (
        "writeback",
        r"ordered_extent|wait_ordered|lock_and_flush|folio_wait_writeback|page_writeback|fdatawait"
        r"|wait_sb_inodes|writeback_inodes|wb_wait|sync_inodes",
        "waiting for data writeback (btrfs ordered extents / fsync)",
    ),
    ("wbt", r"wbt_wait|rq_qos_wait", "block-layer writeback throttling (WBT)"),
    (
        "metadata-read",
        r"read_extent_buffer_pages|read_tree_block|btrfs_read_extent_buffer|read_block_for_search"
        r"|btrfs_readahead_tree_block",
        "reading btrfs metadata from disk (cold cache)",
    ),
    (
        "btrfs-locks",
        r"btrfs_tree_lock|btrfs_tree_read_lock|__btrfs_tree|btrfs_lock_root|btrfs_search_slot"
        r"|lock_extent|btrfs_inode_lock|btrfs_read_lock",
        "btrfs b-tree / extent / inode lock contention",
    ),
    # The last alternative: waits for the memory map lock, the daemon's MMAP_LOCK_FRAME.
    (
        "reclaim",
        r"shrink_|try_to_free|alloc_pages|swap|zswap|mem_cgroup_handle_over_high|throttle_direct_reclaim|"
        + MMAP_LOCK_FRAME.pattern,
        "memory reclaim / swap",
    ),
    ("discard", r"discard", "async discard (discard=async)"),
    ("overlayfs", r"^ovl_", "overlayfs copy-up (containers)"),
    (
        "device",
        r"blk_mq_get_tag|submit_bio_wait|^blk_|nvme|^dm_|kcryptd|dmcrypt|^crypt_|^bio_",
        "block device queue (device or dm-crypt)",
    ),
    (
        "page-read",
        r"filemap_read|filemap_get_pages|folio_wait_locked|folio_lock|filemap_fault|readahead"
        r"|read_folio|do_readpage",
        "reading data not in the page cache",
    ),
]
# Waits that are not about storage at all (D state is used by more than IO).
# worker_thread/kthread here only matter for daemon v2 data, where a task that
# woke up between its stat and stack reads was still sampled.
IDLE_SIG = re.compile(
    r"^(?:futex_\w+|__futex_\w+|ep_poll|do_epoll_wait|do_select|do_sys_poll|usb_\w+"
    r"|worker_thread|kthread|do_nanosleep|hrtimer_nanosleep|pipe_\w+"
    r"|unix_\w+|n_tty_\w+|tty_\w+|wait_for_partner|do_wait|kernel_wait4|rcu_\w+"
    r"|poll_schedule_timeout\w*|intel_\w+|drm_\w+|i915_\w+|xe_\w+|pci_\w+)$"
)
# "maintenance" replaces the former "scrub" category (scrub is maintenance too).
# "reclaim" (memory reclaim, swap, zswap) waits on memory, not on the disks:
# daemon v8+ tags it "memory" and leaves it out of "d"; for older records the
# report takes it out of the storage counts itself (see analyse()).
NON_STORAGE = ("fuse", "idle", "maintenance", "reclaim")
V2_HUNG_SIG = re.compile(r" at (\S+)")
CAT_RE = [(k, re.compile(rx)) for k, rx, _ in CATEGORIES]
CAT_DESC = {k: d for k, _, d in CATEGORIES}
CAT_DESC["other"] = "other kernel waits"
CAT_DESC["no-stack"] = "unknown (no kernel stack captured)"
CAT_DESC["fuse"] = "FUSE filesystem (gvfs share / document portal; usually network or the FUSE daemon)"
CAT_DESC["idle"] = "non-storage waits (futex, epoll, USB, GPU / PCI power management)"
HINTS = {
    "commit": "btrfs transaction commits dominate; with flushoncommit every commit also flushes all dirty data. "
    "Options to PROPOSE to the user (don't apply): lower vm.dirty_bytes, or test without flushoncommit.",
    "log-sync": "fsync-heavy workload: every fsync writes the btrfs log tree and flushes the disk cache of "
    "both RAID1 members (flushoncommit and vm.dirty_* do not matter here). The lever is fsync "
    "frequency in the application, e.g. relaxed durability settings for test databases.",
    "dirty-throttle": "Writers were paced by vm.dirty_bytes. Expected under heavy writes; it bounds commit "
    "latency by design.",
    "writeback": "Tasks waited for their own data to reach disk (fsync / ordered extents).",
    "flush": "fsync/commit cache flushes; each flush goes to both RAID1 members. Frequent fsync "
    "(databases, package managers, test fixtures) pays this.",
    "metadata-read": "Cold metadata reads: something walked many files or directories (find/bfs, indexers, du, "
    "a big checkout) after their metadata left the page cache.",
    "btrfs-locks": "Contention inside btrfs metadata (many writers in the same subvolume / directory).",
    "space": "btrfs had to flush to reserve metadata/data space; check the metadata line.",
    "overlayfs": "Container copy-up of large files from lower image layers.",
    "device": "Requests queued at the block layer (NVMe or dm-crypt busy).",
    "wbt": "The block layer throttled background writeback to protect read latency.",
    "discard": "Async discard work; usually harmless.",
    "page-read": "Cold reads: data was not in the page cache.",
}
UNITS = {
    "time": "epoch seconds",
    "*_pct": "percent of covered wall time",
    "*_bytes": "bytes",
    "*_ms": "milliseconds",
    "share": "fraction of sampled D-state task observations",
    "task_samples": "one task seen in D state in one sample (1 s apart during episodes, so roughly task-seconds)",
    "vm.zswap_in_bytes / zswap_out_bytes": "bytes decompressed from / compressed into the zswap pool "
    "(RAM, no disk IO); null when no sample came from a daemon >= v7",
    "vm.swap_in_bytes / swap_out_bytes": "bytes read from / written to the swap disk (pswpin/pswpout); "
    "swap-out includes zswap writeback",
    "vm.swap_out_bypass_bytes": "swap-out that did not pass through zswap (pswpout minus zswpwb, "
    "v7+ samples only); null like zswap_*",
    "peak.d_tasks / mem_tasks": "most tasks at once waiting on storage / on memory (reclaim, swap, zswap); "
    "for records of a daemon < v8 estimated from the episodes' stacks",
    "disks.*.read_await_ms / write_await_ms": "average time per read / write request",
    "sample_d_tasks": "most tasks waiting on storage in one sample (samples of a daemon >= v5)",
    "open_episode": "null, or an episode without a record yet: since / last (epoch s) and d_tasks "
    "(most tasks waiting on storage in its samples)",
    "episodes.*.max_d_mem": "most tasks waiting on memory at once in the episode",
    "episodes.*.estimated": "true when max_d / max_d_mem were estimated from the stacks "
    "(the episode was recorded by a daemon < v8)",
}


class FsInfo(TypedDict, total=False):
    """One btrfs filesystem in the meta record ("fs", by UUID)."""

    label: str  # empty when the filesystem has no label


class MetaRecord(TypedDict, total=False):
    """The meta record ("t": "meta"), written when the daemon starts. Every
    key is optional: analyse() uses {} when the window has no meta record,
    and reads every key with .get()."""

    ts: float
    v: int  # daemon version
    kernel: str
    base_interval: float  # s between full samples
    vm_keys: list[str]  # the /proc/vmstat names of each sample's "vm" list
    dirty_bytes: int  # vm.dirty_bytes
    disks: dict[str, str]  # block device name -> role ("btrfs ...", "swap", ...)
    fs: dict[str, FsInfo]
    stacks: bool  # the daemon could read kernel stacks


class FsSample(TypedDict):
    """One btrfs filesystem in a sample ("fs", by UUID). Counters are raw."""

    c: list[int]  # commits, total ms, current ms, last ms, max ms, longest since the last sample
    err: dict[str, int]  # device id -> sum of its error counters
    space: NotRequired[dict[str, list[int]]]  # allocation kind -> SPACE_KEYS values


class SampleRecord(TypedDict):
    """A sample ("t": "s"): every 5 s, every second during an episode.
    Counters are cumulative; the report subtracts consecutive samples."""

    ts: float
    mono: NotRequired[float]  # monotonic clock
    psi: dict[str, list[float] | None]  # io/memory/cpu: some avg10, some total us, full avg10, full total us
    procs: list[int]  # running, blocked (iowait)
    vm: NotRequired[list[int]]  # values of the meta record's vm_keys
    # v3+: tasks waiting on storage (before v8 memory waits too, before v5
    # maintenance). Older daemons counted something else in "d"; the report
    # reads it only when "d_all" (v3+) is there.
    d: NotRequired[int]
    d_all: NotRequired[int]  # v3+: every D-state task
    d_maint: NotRequired[int]  # v5+: btrfs maintenance waits (not in "d")
    d_mem: NotRequired[int]  # v8+: memory waits (not in "d")
    fs: dict[str, FsSample]
    disk: dict[str, list[int]]  # /proc/diskstats fields (DISK_FIELDS order)
    cg: NotRequired[dict[str, list[int]]]  # io some/full us, memory some/full us, read/write bytes


class SigRecord(TypedDict):
    """One wait signature of an episode: where in the kernel tasks waited."""

    sig: str
    ctx: str
    n: int  # task-samples
    kind: NotRequired[str]  # v3+: storage, fuse, idle, maintenance (v5+), memory (v8+)
    comms: dict[str, int]  # command name -> task-samples
    cgroups: dict[str, int]
    examples: NotRequired[list[list[str]]]  # kernel stacks, innermost frame first


class _EpisodeBase(TypedDict):
    start: float
    end: float
    reason: str  # what opened it ("hung task", "commit 1200 ms", ...)
    max_d: int  # most tasks waiting on storage at once
    max_d_all: NotRequired[int]  # v3+: every D-state task
    max_d_mem: NotRequired[int]  # v8+: memory waits
    peak: dict[str, float]  # io_some_avg10, io_full_avg10, iowait_tasks, commit_ms


class EpisodeRecord(_EpisodeBase):
    """An episode record ("t": "ep"), written when a stall episode ends."""

    sigs: list[SigRecord]


class Sig(SigRecord):
    """A SigRecord with its category (classify())."""

    cat: str
    task_samples: int


class Episode(_EpisodeBase):
    """An EpisodeRecord as analyse() returns it."""

    sigs: list[Sig]
    estimated: NotRequired[bool]  # max_d / max_d_mem estimated from the stacks (daemon < v8)


class Notice(TypedDict, total=False):
    """An event ("t": "ev": kernel messages, hung or busy tasks) or an alert
    ("t": "al", the notification the daemon sent). One class for both: the
    same loop in analyse() rewrites the kind of each, and tells them apart by
    "severe", which only events have. "ts" is in both, so it is required.
    The report also reads "msg" (in every event) and "title" and "body" (in
    every alert) with [], but each is only in one of the two, so they stay
    NotRequired."""

    ts: Required[float]
    kind: str
    msg: str  # events
    severe: bool  # events
    stack: list[str]  # events of hung or busy tasks
    comm: str  # io-busy events (v6+)
    title: str  # alerts
    body: str  # alerts


class Window(TypedDict):
    since: float
    until: float


class Coverage(TypedDict):
    window_s: int
    covered_s: int
    first_sample: float | None
    last_sample: float | None
    complete: bool
    gaps: NotRequired[int]  # set when the window has 2+ samples


class FsTotals(TypedDict):
    """One btrfs filesystem over the window."""

    commits: int
    commit_ms: int
    max_commit_ms: int
    max_at: float | None
    new_device_errors: int
    space: dict[str, list[int]] | None
    avg_commit_ms: NotRequired[float]


class CgroupTotals(TypedDict):
    """One cgroup over the window (pressure in us, wall in s)."""

    io_some: int
    io_full: int
    read_bytes: int
    write_bytes: int
    wall: float
    io_some_pct: NotRequired[float]
    io_full_pct: NotRequired[float]


class VmTotals(TypedDict):
    """Memory traffic over the window. None: no sample came from a v7+ daemon."""

    swap_in_bytes: int
    swap_out_bytes: int
    zswap_in_bytes: int | None
    zswap_out_bytes: int | None
    zswap_writeback_bytes: int | None
    swap_out_bypass_bytes: int | None
    zswap_counted_from: float | None
    direct_reclaim_scanned_pages: int
    allocstalls: int
    dirty_limit_bytes: int


class CategoryShare(TypedDict):
    category: str
    description: str
    task_samples: int
    share: float


class OpenEpisode(TypedDict):
    since: float
    last: float
    d_tasks: int


class BelowItem(TypedDict):
    comm: str
    cgroup: str
    bytes: int


class BelowSection(TypedDict):
    top_writers: NotRequired[list[BelowItem] | None]
    top_readers: NotRequired[list[BelowItem] | None]
    error: str | None


class AnalyseResult(TypedDict):
    """What analyse() returns. The keys after "coverage" are only there when
    the window has 2+ samples; main() pops "coverage" for --json."""

    window: Window
    kernel: str | None
    samples: int
    episodes: list[Episode]
    events: list[Notice]
    alerts: list[Notice]
    coverage: NotRequired[Coverage]
    pressure: NotRequired[dict[str, float]]  # "<psi key>_pct"
    peak: NotRequired[dict[str, float]]
    maintenance_samples: NotRequired[int]
    vm: NotRequired[VmTotals]
    fs: NotRequired[dict[str, FsTotals]]
    # Keys from DISK_SUM plus derived figures; values mix counters, floats,
    # the role text and None (peak_at), so a plain dict.
    disks: NotRequired[dict[str, dict[str, Any]]]
    cgroups: NotRequired[dict[str, CgroupTotals]]
    categories: NotRequired[list[CategoryShare]]
    non_storage_waits: NotRequired[dict[str, int]]
    sample_d_tasks: NotRequired[int]
    open_episode: NotRequired[OpenEpisode | None]
    v6_since: NotRequired[float | None]
    below: NotRequired[BelowSection]


def classify(sig: str, examples: Sequence[Sequence[str]], comms: Sequence[str] = (), kind: str | None = None) -> str:
    if kind in NON_STORAGE:  # daemon v3 already classified the wait
        return kind
    if kind == "memory":  # daemon v8+
        return "reclaim"
    if FUSE_RE.match(sig):
        return "fuse"
    for frames in examples:
        for k, rx in STACK_RULES:
            if any(rx.match(f) for f in frames):
                return k
    if sig == "no-stack" or sig.startswith("wchan:"):
        name = sig[6:] if sig.startswith("wchan:") else ""
        # A wchan such as submit_bio_wait does not tell trim from a workload read: trust the comm.
        if any("btrfs-discard" in c or c == "fstrim" for c in comms):
            return "maintenance"
        for k, rx in CAT_RE:
            if name and rx.search(name):
                return k
        for c in comms:
            if "btrfs-transaction" in c:
                return "commit"
            if c.startswith(("kcryptd", "dmcrypt_write")):
                return "device"
        return "no-stack"
    for k, rx in CAT_RE:
        if rx.search(sig):
            return k
    if kind != "storage" and IDLE_SIG.match(sig):
        return "idle"
    for frames in examples:
        for f in frames:
            for k, rx in CAT_RE:
                if rx.search(f):
                    return k
    return "other"


def kernel_tuple(release: str | None) -> tuple[int, ...]:
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", release or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def fmt_bytes(b: float) -> str:
    if b >= 10 * 2**30:
        return f"{b / 2**30:,.1f} GiB"
    return f"{b / 2**20:,.0f} MiB"


def fmt_ts(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def zswap_text(vm: VmTotals) -> str:
    """zswap pool traffic and disk swap: 'zswap in/out X / Y   disk swap in/out A / B'.
    pswpout counts zswap writeback too, so only swap-out that bypassed zswap
    gets its own figure."""
    disk = f"disk swap in/out {fmt_bytes(vm['swap_in_bytes'])} / {fmt_bytes(vm['swap_out_bytes'])}"
    if vm["zswap_out_bytes"] is None:
        return f"zswap in/out n/a (daemon < v7)   {disk}"
    since = vm.get("zswap_counted_from")
    bypass = vm.get("swap_out_bypass_bytes") or 0
    # analyse() sets zswap_in_bytes and zswap_out_bytes together (both None
    # without a v7+ sample); ty only sees the check of the second one.
    return (
        f"zswap in/out {fmt_bytes(cast('int', vm['zswap_in_bytes']))} / {fmt_bytes(vm['zswap_out_bytes'])}"
        f"{f' (from {fmt_ts(since)})' if since else ''}   {disk}"
        f"{f' ({fmt_bytes(bypass)} of it not through zswap)' if bypass >= 2**20 else ''}"
    )


def fmt_counts(d: dict[str, int], n: int = 4) -> str:
    return ", ".join(f"{k}({v})" for k, v in list(d.items())[:n])


def open_episode(samples: list[SampleRecord], episodes: list[Episode]) -> OpenEpisode | None:
    """An episode the daemon has not written yet: the newest samples are 1 s
    apart (episode sampling) with 3+ storage waits (v5+ samples), and no
    episode record covers them. None otherwise."""
    if len(samples) < 2 or "d_maint" not in samples[-1]:
        return None
    run = [samples[-1]]
    for a in reversed(samples[:-1]):
        if run[-1]["ts"] - a["ts"] > 1.5:
            break
        run.append(a)
    if len(run) < 2:
        return None
    last = samples[-1]["ts"]
    if any(ep["start"] - 1 <= last <= ep["end"] + 1 for ep in episodes):
        return None
    peak = max(r.get("d", 0) for r in run)
    if peak < 3:
        return None
    return {"since": run[-1]["ts"], "last": last, "d_tasks": peak}


def memory_lines(res: AnalyseResult, storage_d: int) -> list[str]:
    """Memory waits (reclaim, swap, zswap): not counted as storage."""
    peak = res.get("peak", {}).get("mem_tasks", 0)
    ev = [e for e in res["events"] if e.get("kind") == "hung-memory"]
    if peak < 3 and not ev:
        return []
    notes = "".join(
        (
            ["; estimated from the stacks: recorded by a daemon < v8"]
            if any(e.get("estimated") and e.get("max_d_mem") for e in res["episodes"])
            else []
        )
        + ([f"; {len(ev)} task(s) stuck 30+ s, see 'events'"] if ev else [])
    )
    if peak >= storage_d:
        return [
            f"Memory, not storage, blocked up to {peak} tasks (memory reclaim, swap-in or zswap{notes}). "
            f"See the memory line; `cst swap status` shows which cgroup swaps."
        ]
    return [f"Memory waits: up to {peak} tasks also waited on memory reclaim, swap-in or zswap{notes}."]


def saturation_line(res: AnalyseResult) -> str | None:
    """Were the btrfs disks at their limit? Judged at the dm-crypt layer,
    which is what btrfs waits for; the NVMe figures below it are information
    only. Without dm-crypt, the member disks themselves."""
    nvme = [d for n, d in res["disks"].items() if d["role"].startswith("btrfs") and not n.startswith("dm-")]
    dm = [d for n, d in res["disks"].items() if d["role"].startswith("btrfs") and n.startswith("dm-")]
    top = dm or nvme
    if not top:
        return None

    def fig(ds: list[dict[str, Any]]) -> str:
        return (
            f"await {max(d['await_ms'] for d in ds):.1f} ms (read {max(d['read_await_ms'] for d in ds):.1f}, "
            f"write {max(d['write_await_ms'] for d in ds):.1f})"
        )

    aw = max(d["await_ms"] for d in top)
    qd = max(d["queue_depth"] for d in top)
    busy = aw >= 5 or qd >= 8
    line = (
        f"btrfs disks {'look SATURATED' if busy else 'NOT saturated'}: "
        f"{'dm-crypt layer (what btrfs waits for)' if dm else 'member disks'} {fig(top)}, queue depth {qd:.1f}"
    )
    if dm and nvme:
        aw_nvme = max(d["await_ms"] for d in nvme)
        line += (
            f"; NVMe below {fig(nvme)}, information only (its time can include disk cache flushes)"
            f"{'; encryption queueing adds latency' if aw > 2 * aw_nvme + 1 else ''}"
        )
    return line + "."


def fuse_lines(res: AnalyseResult) -> list[str]:
    n = res.get("non_storage_waits", {}).get("fuse", 0)
    ev = [e for e in res["events"] if e.get("kind") == "fuse-wait"]
    if not n and not ev:
        return []
    return [
        f"FUSE waits: programs waited on a FUSE filesystem ({n} task-samples"
        f"{', ' + str(len(ev)) + ' long wait(s) in events' if ev else ''}): a gvfs network share "
        f"(/run/user/*/gvfs; /run/host/run/user/*/gvfs inside toolbox) or the document portal answered "
        f"slowly. Usually the network or the FUSE daemon, typically a find/search walking into a share "
        f"(see 'events' for the command); it only involves the local disks if storage waits show up "
        f"at the same time."
    ]


def busy_lines(res: AnalyseResult, meta: MetaRecord) -> list[str]:
    """Storage waits that kept being scheduled (daemon v6+), and, for records
    of older daemons, 'hung' alerts in the fsync path that may have been one."""
    out = []
    ev = [e for e in res["events"] if e.get("kind") in ("io-busy", "io-busy-long")]
    if ev:
        comms = sorted({e.get("comm") or re.sub(r":\d+ .*", "", e.get("msg", "")) for e in ev})
        long_ = sum(e.get("kind") == "io-busy-long" for e in ev)
        out.append(
            f"Busy waits: {len(ev)} task(s) waited on storage at every sample for {HUNG_SECONDS}+ s while "
            f"still being scheduled ({', '.join(comms)[:120]})"
            f"{f', {long_} for 10+ min' if long_ else ''}: usually an fsync/IO loop (lever: that "
            f"program's fsync rate). It is a slow device only if the disk lines show high await or "
            f"errors."
        )
    v6 = res.get("v6_since")
    old = [
        e
        for e in res["events"]
        if e.get("kind") == "hung"
        and e.get("severe")
        and (v6 is None or e.get("ts", 0) < v6)
        and any(f in ("btrfs_sync_log", "btrfs_sync_file") for f in e.get("stack") or ())
    ]
    if old:
        out.append(
            f"Note: {len(old)} 'hung task' event(s) in the fsync path were recorded before progress checks "
            f"(daemon < v6): they may have been a program calling fsync in a loop rather than a hang; "
            f"check the disk lines (await, errors) of the same window."
        )
    return out


def maint_lines(res: AnalyseResult) -> list[str]:
    n = res.get("non_storage_waits", {}).get("maintenance", 0)
    ev = [e for e in res["events"] if e.get("kind") == "maintenance-wait"]
    peak = res.get("peak", {}).get("maint_tasks", 0)
    # A single async-discard blip is not worth a verdict line.
    if n < 3 and not ev and res.get("maintenance_samples", 0) < 3:
        return []
    return [
        f"Maintenance: btrfs housekeeping ran (fstrim/discard, scrub or balance; {n} task-samples"
        f"{', ' + str(len(ev)) + ' long wait(s) in events' if ev else ''}"
        f"{', up to ' + str(peak) + ' task(s) at once' if peak else ''}). Expected background work, "
        f"not a workload stall; it only slowed the workload if storage waits show up at the same time."
    ]


def verdict(res: AnalyseResult, meta: MetaRecord) -> list[str]:
    out = []
    cov = res["coverage"]
    if res["samples"] < 2:
        return [
            "NO DATA for this window. Is btrfs-stall-monitor.service running? "
            "(`systemctl status btrfs-stall-monitor`; inside toolbox prefix with flatpak-spawn --host)"
        ]
    if not cov["complete"]:
        out.append(
            f"INCOMPLETE DATA: {cov['covered_s']} s of the {cov['window_s']} s window recorded, last sample "
            f"{fmt_ts(cov['last_sample'])}. Data is written every ~10 s; re-run shortly, or narrow the window."
        )
    sev = [e for e in res["events"] if e.get("severe")]
    if sev:
        out.append(f"KERNEL/FS PROBLEMS: {len(sev)} severe event(s) (hung task, btrfs or device error); see 'events'.")
    if any(f["new_device_errors"] for f in res["fs"].values()):
        out.append("btrfs device error counters INCREASED in this window.")
    pct, peak = res["pressure"], res["peak"]
    longest = max((f["max_commit_ms"] for f in res["fs"].values()), default=0)
    longest_at = next((f["max_at"] for f in res["fs"].values() if f["max_commit_ms"] == longest), None)
    psi_stall = pct["io_some_pct"] >= 3 or peak["io_some_avg10"] >= 15
    # Episode records (written when an episode ends) and v5+ samples, which
    # also show an episode that is still open.
    storage_d = max([ep["max_d"] for ep in res["episodes"]] + [res.get("sample_d_tasks", 0)])
    d_stall = storage_d >= 3
    mem_lines = memory_lines(res, storage_d)
    mem_dominates = bool(mem_lines) and mem_lines[0].startswith("Memory, not storage")
    oe = res.get("open_episode")
    open_lines = (
        [
            f"Episode still open: since {fmt_ts(oe['since'])} the monitor samples every second and up to "
            f"{oe['d_tasks']} tasks wait on storage. Its record (stacks, categories) is written when it "
            f"ends; run the report again after that for the details."
        ]
        if oe
        else []
    )
    commit_stall = longest >= 1000
    # Logical bytes: one RAID1 member's dm-crypt device.
    written = max(
        (d["write_bytes"] for n, d in res["disks"].items() if n.startswith("dm-") and d["role"].startswith("btrfs")),
        default=0,
    )
    if not (psi_stall or d_stall or commit_stall):
        out.append(
            f"Storage was NOT a bottleneck: IO pressure {pct['io_some_pct']:.1f}%, at most "
            f"{peak['d_tasks']} task(s) waiting on storage, longest btrfs commit {longest} ms "
            f"(written {fmt_bytes(written)})."
        )
        out += mem_lines
        if pct["cpu_some_pct"] >= 20:
            out.append(f"CPU contention: runnable tasks waited for a CPU {pct['cpu_some_pct']:.0f}% of the time.")
        if pct["mem_some_pct"] >= 5:
            out.append(f"Memory pressure {pct['mem_some_pct']:.1f}%: see the memory line.")
        if written < 50 * 2**20 and pct["cpu_some_pct"] < 2:
            out.append("Little activity recorded: check that the window really covers the workload.")
        return out + fuse_lines(res) + busy_lines(res, meta) + maint_lines(res)
    if psi_stall:
        out.append(
            f"IO wait: tasks waited on IO {pct['io_some_pct']:.1f}% of the time (every non-idle task at "
            f"once {pct['io_full_pct']:.1f}%), peak 10 s average {peak['io_some_avg10']:.0f}%."
        )
    if d_stall and mem_dominates:
        out += mem_lines + [f"Blocked tasks: also up to {storage_d} tasks waiting on storage in D state."]
    elif d_stall:
        out += [
            f"Blocked tasks: up to {storage_d} tasks waiting on storage in D state "
            f"(btrfs commit/log/lock waits block tasks without showing up as IO pressure)."
        ] + mem_lines
    else:
        out += mem_lines
    out += open_lines
    if commit_stall:
        out.append(
            f"Slow btrfs commits: longest {longest / 1000:.1f} s{' at ' + fmt_ts(longest_at) if longest_at else ''}."
        )
    cats = res["categories"]
    if cats:
        desc = ", ".join(f"{c['description']} {c['share'] * 100:.0f}%" for c in cats[:3])
        out.append(f"Where blocked tasks waited (share of sampled D-state tasks): {desc}.")
        main = cats[0]["category"]
        if main == "zstd-workspace":
            if kernel_tuple(meta.get("kernel")) < (7, 2, 6):
                out.append("Matches the btrfs zstd lost-wakeup bug fixed in 7.2.6; a kernel update fixes it.")
            else:
                out.append(
                    "zstd workspace waits on a kernel that already has the 7.2.6 lost-wakeup fix; "
                    "may be plain compression contention."
                )
        elif HINTS.get(main):
            out.append(HINTS[main])
    if meta.get("stacks") is False:
        out.append("Caveat: the daemon could not read kernel stacks, so attribution is coarse (wchan only).")
    out += fuse_lines(res) + busy_lines(res, meta) + maint_lines(res)
    if res["episodes"]:
        w = res["episodes"][0]
        out.append(
            f"Worst episode: {fmt_ts(w['start'])}-{fmt_ts(w['end'])} ({w['end'] - w['start']:.0f} s, "
            f"{w['max_d']} D tasks, commit {w['peak'].get('commit_ms', 0)} ms)."
        )
    sat = saturation_line(res)
    if sat:
        out.append(sat)
    if res["vm"]["swap_out_bytes"] >= 256 * 2**20:
        out.append(f"Swap-out of {fmt_bytes(res['vm']['swap_out_bytes'])}: memory pressure also loaded the swap disk.")
    return out

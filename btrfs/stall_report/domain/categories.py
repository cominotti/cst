# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-report's wait categories, no I/O: the stack rules (with the daemon's frame lists, so the two never drift), the categories and their hints, and classifying one wait signature."""

from collections.abc import Sequence
import re

from btrfs.stall_monitord.domain import (
    FUSE_FRAME as FUSE_RE,
    MAINT_FRAME as MAINT_RE,
    MMAP_LOCK_FRAME,
    SWAP_FRAME,
    WaitKind,
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
        (
            r"commit_transaction|transaction_kthread|wait_current_trans|wait_for_commit|btrfs_sync_fs|join_transaction"
            r"|start_transaction"
        ),
        "btrfs transaction commit",
    ),
    ("log-sync", r"tree_log|sync_log|wait_log_commit|wait_for_writer", "fsync log-tree sync (btrfs_sync_log)"),
    (
        "flush",
        (
            r"barrier_all_devices|write_dev_flush|wait_dev_flush|wait_dev_supers|blkdev_issue_flush"
            r"|write_all_supers"
        ),
        "disk cache flushes (fsync / commit barriers on every RAID1 member)",
    ),
    (
        "space",
        (
            r"reserve_metadata|reserve_data|reserve_ticket|flush_space|btrfs_reserve|reserve_space"
            r"|delalloc_reserve"
        ),
        "btrfs space reservation / flushing",
    ),
    (
        "writeback",
        (
            r"ordered_extent|wait_ordered|lock_and_flush|folio_wait_writeback|page_writeback|fdatawait"
            r"|wait_sb_inodes|writeback_inodes|wb_wait|sync_inodes"
        ),
        "waiting for data writeback (btrfs ordered extents / fsync)",
    ),
    ("wbt", r"wbt_wait|rq_qos_wait", "block-layer writeback throttling (WBT)"),
    (
        "metadata-read",
        (
            r"read_extent_buffer_pages|read_tree_block|btrfs_read_extent_buffer|read_block_for_search"
            r"|btrfs_readahead_tree_block"
        ),
        "reading btrfs metadata from disk (cold cache)",
    ),
    (
        "btrfs-locks",
        (
            r"btrfs_tree_lock|btrfs_tree_read_lock|__btrfs_tree|btrfs_lock_root|btrfs_search_slot"
            r"|lock_extent|btrfs_inode_lock|btrfs_read_lock"
        ),
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
        (
            r"filemap_read|filemap_get_pages|folio_wait_locked|folio_lock|filemap_fault|readahead"
            r"|read_folio|do_readpage"
        ),
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
# report takes it out of the storage counts itself (see summarise()).
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


def classify(sig: str, examples: Sequence[Sequence[str]], comms: Sequence[str] = (), kind: str | None = None) -> str:
    if kind in NON_STORAGE:  # daemon v3 already classified the wait
        return kind
    if kind == WaitKind.MEMORY:  # daemon v8+
        return "reclaim"
    if FUSE_RE.match(sig):
        return "fuse"
    if (rule := _stack_rule(examples)) is not None:
        return rule
    if sig == "no-stack" or sig.startswith("wchan:"):
        return _no_stack_category(sig, comms)
    if (cat := _first_category(sig)) is not None:
        return cat
    if kind != WaitKind.STORAGE and IDLE_SIG.match(sig):
        return "idle"
    return _frame_category(examples)


def _stack_rule(examples: Sequence[Sequence[str]]) -> str | None:
    """The first STACK_RULES rule that matches a frame of an example, example by example."""
    for frames in examples:
        for k, rx in STACK_RULES:
            if any(rx.match(f) for f in frames):
                return k
    return None


def _first_category(text: str) -> str | None:
    """The first category whose pattern TEXT contains (CAT_RE order), or None."""
    for k, rx in CAT_RE:
        if rx.search(text):
            return k
    return None


def _no_stack_category(sig: str, comms: Sequence[str]) -> str:
    """classify of a wait without a stack ("no-stack", or "wchan:NAME"): the comms and the wchan."""
    name = sig[6:] if sig.startswith("wchan:") else ""
    # A wchan such as submit_bio_wait does not tell trim from a workload read: trust the comm.
    if any("btrfs-discard" in c or c == "fstrim" for c in comms):
        return "maintenance"
    if name and (cat := _first_category(name)) is not None:
        return cat
    for c in comms:
        if "btrfs-transaction" in c:
            return "commit"
        if c.startswith(("kcryptd", "dmcrypt_write")):
            return "device"
    return "no-stack"


def _frame_category(examples: Sequence[Sequence[str]]) -> str:
    """The category of the first frame, example by example, that a category's pattern finds; else "other"."""
    for frames in examples:
        for f in frames:
            if (cat := _first_category(f)) is not None:
                return cat
    return "other"

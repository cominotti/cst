# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the file readers of btrfs/btrfs-stall-monitord (no root needed).

The daemon reads /proc and /sys files. Some of them, like /proc/vmstat, are
kernel "seq files": one read() returns only about one page (4 KiB), and the
rest comes in later reads. These tests make a temporary file behave the same
way by limiting each os.read() to one page, so they never touch the real
/proc. An old reader that did a single read() fails them: it lost every line
after the first page (zswpin, zswpout and zswpwb in /proc/vmstat).

The wait-kind tests (daemon v8) feed recorded-looking kernel stacks to the
classifier and to the sampling, episode and hung-task code, with every /proc
read replaced by a fake.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_stall_monitord -v
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast
from unittest import mock

# Run directly (python3 -I -B btrfs/tests/test_stall_monitord.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from btrfs.stall_monitord import cli as md_cli
from btrfs.stall_monitord import domain as md_domain
from btrfs.stall_monitord.adapters import monitor as md_monitor
from btrfs.stall_monitord.adapters import proc as md_proc
from cst_core.adapters.sd_notify import ServiceNotifier
from tests.package_view import PackageView

sys.dont_write_bytecode = True

PAGE = 4096


class _EpisodeProto(Protocol):
    reason: str
    max_d: int
    max_d_mem: int

    def to_record(self) -> dict[str, Any]: ...


class _MonitorProto(Protocol):
    prev: dict[str, Any] | None
    episode: _EpisodeProto | None
    want_episode: str | None
    buffer: list[tuple[float, str]]
    alerts: list[tuple[str, str]]
    d_since: dict[str, dict[str, Any]]
    hung_reported: dict[tuple[str, str], float]
    cmax: dict[str, int]
    topo: types.SimpleNamespace
    fake: FakeProc
    alert: Callable[[str, str, str, str], None]
    describe: Callable[[str | int, str | int], tuple[list[str], str | None]]
    cmdlines: md_proc.TimedReader

    def describe_all(
        self, d_tasks: Sequence[tuple[str, str, str]]
    ) -> list[tuple[str, str, str, list[str], str | None, str]]: ...

    def sample(self, d_tasks: Sequence[tuple[str, str, str, list[str], str | None, str]]) -> dict[str, Any]: ...

    def update_episode(
        self, rec: Mapping[str, Any], d_tasks: Sequence[tuple[str, str, str, list[str], str | None, str]]
    ) -> None: ...

    def track_hung(self, d_tasks: Sequence[tuple[str, str, str, list[str], str | None, str]], now: float) -> None: ...

    def report_io_busy(
        self, t: md_domain.DTaskSince, tid: str, frames: list[str], wchan: str | None, now: float
    ) -> None: ...


class _StallMonitordModule(Protocol):
    os: types.ModuleType
    VERSION: int
    HUNG_SECONDS: int
    Monitor: type[object]

    def read_bytes(self, path: str, size: int = 65536) -> bytes | None: ...

    def read_text(self, path: str) -> str | None: ...

    def read_kv(self, path: str) -> dict[str, int]: ...

    def read_int(self, path: str) -> int: ...

    def wait_kind(self, frames: Sequence[str], wchan: str | None) -> str: ...

    def mmap_lock_only(self, frames: Sequence[str], wchan: str | None) -> bool: ...


def load() -> _StallMonitordModule:
    """The daemon's package as one namespace (the program is a thin entry over
    it). A new view puts back what the previous test replaced; loading runs
    nothing: main() runs only from the program file."""
    view = PackageView(md_cli, md_monitor, md_proc, md_domain)
    return cast("_StallMonitordModule", cast("object", view))


def fake_vmstat() -> str:
    """A vmstat text larger than one page, with the zswap lines after byte 4096."""
    lines = [f"filler_counter_{i:04d} {i}" for i in range(200)]
    head = "nr_dirty 11\npswpout 6830873\n" + "\n".join(lines) + "\n"
    assert len(head) > PAGE + 100
    return head + "zswpin 123\nzswpout 456789\nzswpwb 6830873\n"


class ReadBytesTest(unittest.TestCase):
    m: _StallMonitordModule
    tmp: str
    opened: list[int]
    reads: int

    def setUp(self) -> None:
        self.m = load()
        self.tmp = tempfile.mkdtemp(prefix="stall-monitord-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.opened = []
        self.reads = 0
        real_open, real_read = os.open, os.read

        def tracking_open(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            self.opened.append(fd)
            return fd

        def page_read(fd: int, n: int) -> bytes:
            # A seq file: never more than one page per read().
            self.reads += 1
            return real_read(fd, min(n, PAGE))

        self.real_read = real_read
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(self.m.os, "open", tracking_open))
        stack.enter_context(mock.patch.object(self.m.os, "read", page_read))

    def write(self, name: str, data: bytes) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def assert_closed(self) -> None:
        self.assertTrue(self.opened, "read_bytes did not open the file")
        for fd in self.opened:
            with self.assertRaises(OSError) as cm:
                os.fstat(fd)
            self.assertEqual(cm.exception.errno, errno.EBADF, f"fd {fd} left open")

    def test_reads_past_the_first_page(self) -> None:
        data = bytes(range(256)) * 40  # 10240 bytes: two and a half pages
        path = self.write("big", data)
        self.assertEqual(self.m.read_bytes(path), data)
        self.assertGreaterEqual(self.reads, 3)
        self.assert_closed()

    def test_vmstat_zswap_counters_after_first_page(self) -> None:
        text = fake_vmstat()
        self.assertGreater(text.index("zswpwb"), PAGE)
        path = self.write("vmstat", text.encode())
        kv = self.m.read_kv(path)
        self.assertEqual(kv["zswpwb"], 6830873)
        self.assertEqual(kv["zswpout"], 456789)
        self.assertEqual(kv["zswpin"], 123)
        self.assertEqual(kv["nr_dirty"], 11)
        self.assertEqual(kv["filler_counter_0199"], 199)
        self.assert_closed()

    def test_read_text_returns_whole_file(self) -> None:
        text = fake_vmstat()
        path = self.write("vmstat", text.encode())
        self.assertEqual(self.m.read_text(path), text)

    def test_size_caps_the_result(self) -> None:
        path = self.write("stat", b"x" * (3 * PAGE))
        got = self.m.read_bytes(path, 1024)
        self.assertEqual(got, b"x" * 1024)
        self.assertEqual(self.reads, 1)  # stops once it has `size` bytes
        self.assert_closed()

    def test_size_cap_across_several_reads(self) -> None:
        path = self.write("big", b"y" * (5 * PAGE))
        got = self.m.read_bytes(path, 2 * PAGE + 10)
        assert got is not None
        self.assertEqual(len(got), 2 * PAGE + 10)
        self.assert_closed()

    def test_default_cap_is_64_kib(self) -> None:
        path = self.write("huge", b"z" * (70 * 1024))
        data = self.m.read_bytes(path)
        assert data is not None
        self.assertEqual(len(data), 65536)

    def test_short_file_under_the_cap(self) -> None:
        path = self.write("stat", b"1234 (bash) S 1\n")
        self.assertEqual(self.m.read_bytes(path, 1024), b"1234 (bash) S 1\n")
        self.assert_closed()

    def test_empty_file(self) -> None:
        path = self.write("empty", b"")
        self.assertEqual(self.m.read_bytes(path), b"")
        self.assertEqual(self.m.read_text(path), "")
        self.assertEqual(self.m.read_kv(path), {})
        self.assert_closed()

    def test_missing_path(self) -> None:
        missing = os.path.join(self.tmp, "no-such-file")
        self.assertIsNone(self.m.read_bytes(missing))
        self.assertIsNone(self.m.read_text(missing))
        self.assertEqual(self.m.read_kv(missing), {})
        self.assertEqual(self.m.read_int(missing), 0)
        self.assertEqual(self.opened, [])

    def test_error_during_first_read(self) -> None:
        path = self.write("big", b"a" * (2 * PAGE))

        def failing_read(fd: int, n: int) -> bytes:
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch.object(self.m.os, "read", failing_read):
            self.assertIsNone(self.m.read_bytes(path))
        self.assert_closed()

    def test_error_after_the_first_page(self) -> None:
        # A task can exit between two reads of its /proc files: the next
        # read() fails (ESRCH). The partial data must not be returned.
        path = self.write("big", b"a" * (3 * PAGE))
        calls = []

        def second_read_fails(fd: int, n: int) -> bytes:
            calls.append(n)
            if len(calls) > 1:
                raise OSError(errno.ESRCH, "No such process")
            return self.real_read(fd, min(n, PAGE))

        with mock.patch.object(self.m.os, "read", second_read_fails):
            self.assertIsNone(self.m.read_bytes(path))
        self.assertEqual(len(calls), 2)
        self.assert_closed()


# Kernel stacks, innermost frame first, as parse_stack() returns them.
PAGE_FAULT = ["do_user_addr_fault", "exc_page_fault", "asm_exc_page_fault"]
DIRECT_RECLAIM = [
    "shrink_folio_list",
    "shrink_lruvec",
    "shrink_node",
    "do_try_to_free_pages",
    "try_to_free_pages",
    "__alloc_pages_slowpath",
    "__alloc_frozen_pages_noprof",
]
STACKS = {
    # A JVM GC thread compressing a page into zswap during direct reclaim
    # (recorded on this PC, 2026-11-14 23:15, MGLRU reclaim).
    "zswap_compress": [
        "__schedule",
        "schedule",
        "schedule_preempt_disabled",
        "__mutex_lock",
        "zswap_compress",
        "zswap_store_page",
        "zswap_store",
        "swap_writeout",
        "pageout",
        "shrink_folio_list",
        "evict_folios",
        "try_to_shrink_lruvec",
        "shrink_one",
        "shrink_many",
    ],
    # A page fault that reads the page back from the zswap pool.
    "zswap_decompress": [
        "__schedule",
        "schedule",
        "__mutex_lock",
        "zswap_decompress",
        "zswap_load",
        "swap_read_folio",
        "do_swap_page",
        "__handle_mm_fault",
        "handle_mm_fault",
    ]
    + PAGE_FAULT,
    # A page fault reading from the swap disk: block layer innermost, still memory.
    "swap_in_disk": [
        "__schedule",
        "schedule",
        "io_schedule",
        "submit_bio_wait",
        "swap_read_folio_bdev_sync",
        "swap_read_folio",
        "do_swap_page",
        "handle_mm_fault",
    ]
    + PAGE_FAULT,
    "throttle_direct_reclaim": [
        "__schedule",
        "schedule",
        "schedule_timeout",
        "throttle_direct_reclaim",
        "try_to_free_pages",
        "__alloc_pages_slowpath",
        "__alloc_frozen_pages_noprof",
    ],
    "memcg_high": [
        "__schedule",
        "schedule",
        "schedule_timeout",
        "mem_cgroup_handle_over_high",
        "irqentry_exit_to_user_mode",
    ],
    # Another thread of the same JVM waiting for the memory map lock.
    "mmap_lock": ["__schedule", "schedule", "rwsem_down_read_slowpath", "down_read_killable", "lock_mm_and_find_vma"]
    + PAGE_FAULT,
    # mmap() waiting for the threads faulting in that VMA (same event).
    "vma_write": [
        "__schedule",
        "schedule",
        "__vma_start_exclude_readers",
        "__vma_start_write",
        "vma_expand",
        "vma_merge_new_range",
        "__mmap_region",
        "do_mmap",
        "vm_mmap_pgoff",
        "do_syscall_64",
        "entry_SYSCALL_64_after_hwframe",
    ],
    "mmap_write_lock": [
        "__schedule",
        "schedule",
        "rwsem_down_write_slowpath",
        "down_write_killable",
        "vm_mmap_pgoff",
        "do_syscall_64",
        "entry_SYSCALL_64_after_hwframe",
    ],
    # mmap() of a file that waits inside btrfs: storage, although vm_mmap_pgoff is on the stack.
    "mmap_file_btrfs": [
        "__schedule",
        "schedule",
        "io_schedule",
        "folio_wait_bit_common",
        "folio_wait_writeback",
        "btrfs_file_mmap",
        "mmap_region",
        "do_mmap",
        "vm_mmap_pgoff",
        "do_syscall_64",
    ],
    "fsync": [
        "__schedule",
        "schedule",
        "io_schedule",
        "folio_wait_bit_common",
        "folio_wait_writeback",
        "btrfs_wait_ordered_range",
        "btrfs_sync_file",
        "__x64_sys_fsync",
        "do_syscall_64",
    ],
    "sync_log": ["__schedule", "schedule", "btrfs_sync_log", "btrfs_sync_file", "__x64_sys_fdatasync"],
    # Reclaim that entered btrfs through a shrinker and waits on a btrfs lock: storage.
    "shrinker_btrfs_lock": [
        "__schedule",
        "schedule",
        "btrfs_tree_lock_nested",
        "btrfs_lock_root_node",
        "btrfs_search_slot",
        "btrfs_evict_inode",
        "evict",
        "prune_icache_sb",
        "super_cache_scan",
        "do_shrink_slab",
        "shrink_slab",
    ]
    + DIRECT_RECLAIM[2:],
    "fuse": ["__schedule", "schedule", "request_wait_answer", "fuse_simple_request", "fuse_lookup"],
    "fstrim": [
        "__schedule",
        "schedule",
        "io_schedule",
        "submit_bio_wait",
        "btrfs_trim_free_extents",
        "btrfs_trim_fs",
        "btrfs_ioctl_fitrim",
    ],
    "futex": ["__schedule", "schedule", "futex_wait_queue", "futex_wait", "do_futex"],
}


class WaitKindTest(unittest.TestCase):
    m: _StallMonitordModule

    def setUp(self) -> None:
        self.m = load()

    def kind(self, name: str) -> str:
        return self.m.wait_kind(STACKS[name], None)

    def test_memory_waits(self) -> None:
        for name in (
            "zswap_compress",
            "zswap_decompress",
            "swap_in_disk",
            "throttle_direct_reclaim",
            "memcg_high",
            "mmap_lock",
            "vma_write",
            "mmap_write_lock",
        ):
            with self.subTest(name):
                self.assertEqual(self.kind(name), "memory")

    def test_storage_waits_stay_storage(self) -> None:
        for name in ("fsync", "sync_log", "shrinker_btrfs_lock", "mmap_file_btrfs"):
            with self.subTest(name):
                self.assertEqual(self.kind(name), "storage")

    def test_other_kinds_unchanged(self) -> None:
        self.assertEqual(self.kind("fuse"), "fuse")
        self.assertEqual(self.kind("fstrim"), "maintenance")
        self.assertEqual(self.kind("futex"), "idle")
        self.assertEqual(self.m.wait_kind(["__schedule", "schedule", "io_schedule"], None), "storage")

    def test_wchan_only(self) -> None:
        self.assertEqual(self.m.wait_kind([], "throttle_direct_reclaim"), "memory")
        self.assertEqual(self.m.wait_kind([], "zswap_decompress"), "memory")
        self.assertEqual(self.m.wait_kind([], "btrfs_sync_log"), "storage")
        self.assertEqual(self.m.wait_kind([], None), "storage")

    def test_mmap_lock_only(self) -> None:
        self.assertTrue(self.m.mmap_lock_only(STACKS["mmap_lock"], None))
        self.assertTrue(self.m.mmap_lock_only(STACKS["vma_write"], None))
        self.assertFalse(self.m.mmap_lock_only(STACKS["zswap_decompress"], None))
        self.assertFalse(self.m.mmap_lock_only(STACKS["throttle_direct_reclaim"], None))
        self.assertTrue(self.m.mmap_lock_only([], "lock_mm_and_find_vma"))


class FakeProc:
    """The /proc reads of the sampling code, from fixed data."""

    tasks: dict[str, tuple[str, str, list[str]]]

    def __init__(self, m: _StallMonitordModule, tasks: dict[str, tuple[str, str, list[str]]]) -> None:
        self.tasks = tasks  # tid -> (pid, comm, frames)

        def _d_comm(_pid: str | int, tid: str | int) -> str | None:
            key = str(tid)
            return self.tasks[key][1] if key in self.tasks else None

        def _short_cgroup(_pid: str | int) -> str:
            return "app.slice"

        def _run_slices(_pid: str | int, _tid: str | int) -> int:
            return 7

        def _read_text(_path: str) -> None:
            return None

        def _read_kv(_path: str) -> dict[str, int]:
            return {}

        def _read_int(_path: str) -> int:
            return 0

        def _parse_psi(_path: str) -> None:
            return None

        self.patches = [
            mock.patch.object(m, "d_comm", _d_comm),
            mock.patch.object(m, "short_cgroup", _short_cgroup),
            mock.patch.object(m, "run_slices", _run_slices),  # never scheduled: no progress
            mock.patch.object(m, "read_text", _read_text),
            mock.patch.object(m, "read_kv", _read_kv),
            mock.patch.object(m, "read_int", _read_int),
            mock.patch.object(m, "parse_psi", _parse_psi),
        ]

    def __enter__(self) -> FakeProc:
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self.patches):
            p.stop()


def monitor(m: _StallMonitordModule) -> _MonitorProto:
    """A Monitor without __init__ (which reads the real /sys and /proc)."""
    mon = cast("_MonitorProto", object.__new__(m.Monitor))
    mon.prev = mon.episode = mon.want_episode = None
    mon.buffer, mon.alerts = [], []
    mon.d_since, mon.hung_reported, mon.cmax = {}, {}, {}

    def _scan_cgroups(_now: float) -> None:
        return None

    def _alert(kind: str, _level: str, title: str, _body: str) -> None:
        mon.alerts.append((kind, title))

    def _describe(_pid: str | int, tid: str | int) -> tuple[list[str], str | None]:
        return (mon.fake.tasks[str(tid)][2], None)

    mon.topo = types.SimpleNamespace(fs={}, disks={}, top_devs=set(), containers={}, scan_cgroups=_scan_cgroups)
    mon.alert = _alert
    mon.describe = _describe
    # The command lines through the view's read_text at each read (FakeProc and the tests replace it).
    mon.cmdlines = md_proc.TimedReader(md_domain.CMDLINE_WAIT, lambda path: m.read_text(path))
    return mon


class MemoryKindSamplingTest(unittest.TestCase):
    m: _StallMonitordModule
    mon: _MonitorProto

    def setUp(self) -> None:
        self.m = load()
        self.mon = monitor(self.m)

    def tasks(self, spec: Sequence[tuple[str, str]]) -> dict[str, tuple[str, str, list[str]]]:
        """spec: [(pid, stack name)] -> {tid: (pid, comm, frames)}"""
        return {
            str(100 + i): (pid, "java" if pid == "10" else "cargo", STACKS[name]) for i, (pid, name) in enumerate(spec)
        }

    def run_sample(
        self, spec: Sequence[tuple[str, str]]
    ) -> tuple[dict[str, Any], list[tuple[str, str, str, list[str], str | None, str]]]:
        self.mon.fake = FakeProc(self.m, self.tasks(spec))
        with self.mon.fake:
            d_tasks = self.mon.describe_all([(pid, tid, comm) for tid, (pid, comm, _) in self.mon.fake.tasks.items()])
            rec = self.mon.sample(d_tasks)
            self.mon.update_episode(rec, d_tasks)
        return rec, d_tasks

    def test_memory_waits_count_in_d_mem_and_open_no_episode(self) -> None:
        rec, _ = self.run_sample(
            [("10", "zswap_compress")] * 20
            + [("10", "mmap_lock")] * 10
            + [("10", "zswap_decompress")] * 5
            + [("20", "fsync")]
        )
        self.assertEqual(rec["d"], 1)
        self.assertEqual(rec["d_mem"], 35)
        self.assertEqual(rec["d_all"], 36)
        self.assertIsNone(self.mon.episode)

    def test_storage_waits_still_open_an_episode(self) -> None:
        rec, _ = self.run_sample([("20", "fsync")] * 3 + [("10", "zswap_compress")] * 4)
        self.assertEqual(rec["d"], 3)
        self.assertIsNotNone(self.mon.episode)
        ep = self.mon.episode
        assert ep is not None
        self.assertEqual(ep.reason, "3 tasks waiting on storage")
        self.assertEqual(ep.max_d, 3)
        self.assertEqual(ep.max_d_mem, 4)
        record = ep.to_record()
        self.assertEqual(record["max_d_mem"], 4)
        kinds = {s["sig"]: s["kind"] for s in record["sigs"]}
        self.assertEqual(kinds["zswap_compress"], "memory")
        self.assertEqual(kinds["folio_wait_writeback"], "storage")

    def test_mmap_lock_waiter_follows_a_storage_thread_of_its_process(self) -> None:
        _, d_tasks = self.run_sample(
            [("20", "fsync"), ("20", "mmap_lock"), ("10", "mmap_lock"), ("10", "vma_write"), ("10", "zswap_compress")]
        )
        self.assertEqual([t[5] for t in d_tasks], ["storage", "storage", "memory", "memory", "memory"])

    def test_mmap_lock_waiter_stays_memory_when_its_process_also_swaps(self) -> None:
        # One JVM: threads faulting through zswap, GC threads waiting for the
        # memory map lock, and one thread in fsync. The lock waiters wait on
        # the faulting threads, not on the fsync.
        _, d_tasks = self.run_sample(
            [("10", "zswap_decompress"), ("10", "mmap_lock"), ("10", "vma_write"), ("10", "fsync")]
        )
        self.assertEqual([t[5] for t in d_tasks], ["memory", "memory", "memory", "storage"])

    def test_version_is_8(self) -> None:
        self.assertEqual(self.m.VERSION, 8)


class EpisodeThresholdsTest(unittest.TestCase):
    """What opens an episode: IO pressure, a long commit, or tasks waiting on
    storage, each at its threshold (ENTER_*); below all three, nothing."""

    m: _StallMonitordModule
    mon: _MonitorProto

    def setUp(self) -> None:
        self.m = load()
        self.mon = monitor(self.m)

    @staticmethod
    def rec(mono: float, io_total_us: int, commit_ms: int = 0, d: int = 0) -> dict[str, Any]:
        return {
            "ts": 1000.0 + mono,
            "mono": mono,
            "psi": {"io": [0.0, io_total_us, 0.0, 0]},
            "fs": {"uuid": {"c": [0, 0, 0, 0, 0, commit_ms]}},
            "d": d,
            "procs": [1, 0],
        }

    def reason(self, io_pct: float, commit_ms: int, d: int) -> str | None:
        """The episode one sample opens, 1 s after the previous one."""
        self.mon.prev = self.rec(0.0, 0)
        self.mon.episode = None
        self.mon.update_episode(self.rec(1.0, round(io_pct * 1e4), commit_ms, d), [])
        return None if self.mon.episode is None else self.mon.episode.reason

    def test_each_threshold_opens_an_episode(self) -> None:
        self.assertEqual(self.reason(10.0, 0, 0), "io_some 10%")
        self.assertEqual(self.reason(0.0, 1000, 0), "commit 1000 ms")
        self.assertEqual(self.reason(0.0, 0, 3), "3 tasks waiting on storage")
        self.assertEqual(self.reason(12.0, 1200, 4), "io_some 12%")  # IO pressure is named first

    def test_below_every_threshold_nothing_opens(self) -> None:
        self.assertIsNone(self.reason(9.9, 999, 2))
        self.assertIsNone(self.reason(2.0, 100, 0))


class HungKindTest(unittest.TestCase):
    m: _StallMonitordModule
    mon: _MonitorProto

    def setUp(self) -> None:
        self.m = load()
        self.mon = monitor(self.m)

    def stuck(self, name: str) -> list[dict[str, Any]]:
        self.mon.fake = FakeProc(self.m, {"100": ("10", "java", STACKS[name])})
        d_tasks = [("10", "100", "java", STACKS[name], None, self.m.wait_kind(STACKS[name], None))]
        with self.mon.fake:
            self.mon.track_hung(d_tasks, 1000.0)
            self.mon.track_hung(d_tasks, 1000.0 + self.m.HUNG_SECONDS + 1)
        return [json.loads(line) for _, line in self.mon.buffer]

    def test_memory_hung_task_is_not_called_storage(self) -> None:
        events = self.stuck("zswap_compress")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "hung-memory")
        self.assertFalse(events[0]["severe"])
        self.assertIn("memory reclaim / swap", events[0]["msg"])
        self.assertEqual(
            self.mon.alerts, [("hung-memory", "Task stuck in an uninterruptible wait (memory reclaim / swap)")]
        )
        self.assertIsNone(self.mon.episode)  # no storage episode

    def test_storage_hung_task_unchanged(self) -> None:
        events = self.stuck("sync_log")
        self.assertEqual(events[0]["kind"], "hung")
        self.assertTrue(events[0]["severe"])
        self.assertEqual(self.mon.alerts, [("hung", "Task stuck in an uninterruptible wait (likely storage)")])
        self.assertIsNotNone(self.mon.episode)


class TimedReaderTest(unittest.TestCase):
    """The helper thread that reads files for the loop (proc.TimedReader): it waits at most its timeout for one
    read, refuses at once while an earlier read is still waiting, and never hands a late answer to the next read."""

    def setUp(self) -> None:
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.calls: list[tuple[str, str]] = []

    def read(self, path: str) -> str | None:
        self.calls.append((path, threading.current_thread().name))
        if path.startswith("slow"):
            self.release.wait(10)
        return f"text of {path}"

    def test_reads_in_one_helper_thread(self) -> None:
        reader = md_proc.TimedReader(5.0, self.read)
        self.assertEqual([reader.read("a"), reader.read("b")], ["text of a", "text of b"])
        self.assertEqual({name for _, name in self.calls}, {"timed-reader"})

    def test_default_reader_reads_the_file_or_gives_none(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-stall-timed-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "cmdline")
        with open(path, "w", encoding="utf-8") as f:
            f.write("java\0-jar\0app.jar\0")
        reader = md_proc.TimedReader(5.0)
        self.assertEqual(reader.read(path), "java\0-jar\0app.jar\0")
        self.assertIsNone(reader.read(os.path.join(tmp, "missing")))

    def test_a_slow_read_times_out_and_the_next_is_refused_at_once(self) -> None:
        reader = md_proc.TimedReader(0.2, self.read)
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            reader.read("slow")
        self.assertGreaterEqual(time.monotonic() - start, 0.2)
        with self.assertRaises(TimeoutError):
            reader.read("other")
        self.assertEqual([path for path, _ in self.calls], ["slow"])  # "other" was never handed to a thread

    def test_a_read_that_raises_reads_as_none_and_the_next_read_works(self) -> None:
        def broken(path: str) -> str | None:
            if path == "bad":
                raise MemoryError(path)
            return self.read(path)

        reader = md_proc.TimedReader(5.0, broken)
        with mock.patch.object(threading, "excepthook", lambda _args: None):  # the thread's traceback
            self.assertIsNone(reader.read("bad"))
        self.assertEqual(reader.read("b"), "text of b")

    def test_a_read_still_waiting_never_holds_the_daemon_s_exit(self) -> None:
        # A stop or a watchdog restart during a stall must not wait for the stalled program's lock: the
        # thread left waiting is a daemon thread, which the interpreter does not join at exit.
        reader = md_proc.TimedReader(0.1, self.read)
        with self.assertRaises(TimeoutError):
            reader.read("slow")
        waiting = [t for t in threading.enumerate() if t.name == "timed-reader" and t.is_alive()]
        self.assertEqual(len(waiting), 1)
        self.assertTrue(waiting[0].daemon)

    def test_a_thread_that_cannot_start_is_a_read_not_made(self) -> None:
        reader = md_proc.TimedReader(5.0, self.read)
        with (
            mock.patch.object(threading.Thread, "start", side_effect=RuntimeError("can't start new thread")),
            self.assertRaises(TimeoutError),
        ):
            reader.read("a")
        self.assertEqual(reader.read("b"), "text of b")

    def test_a_late_answer_is_not_handed_to_the_next_read(self) -> None:
        reader = md_proc.TimedReader(0.2, self.read)
        with self.assertRaises(TimeoutError):
            reader.read("slow")
        self.release.set()
        deadline = time.monotonic() + 5
        while True:
            try:
                value = reader.read("b")
                break
            except TimeoutError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.01)
        self.assertEqual(value, "text of b")
        self.assertEqual(reader.read("c"), "text of c")


class CmdlineReadTest(unittest.TestCase):
    """A hung-task or io-busy report names the task with its /proc/PID/cmdline. That read takes the program's
    memory map lock, which can stay taken for minutes during the stall being recorded: the report waits at most
    CMDLINE_WAIT for it and says so, and the loop goes on."""

    m: _StallMonitordModule
    mon: _MonitorProto

    def setUp(self) -> None:
        self.m = load()
        self.mon = monitor(self.m)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.cmdline_reads: list[str] = []

    def blocked_read_text(self, path: str) -> str | None:
        """/proc/PID/cmdline waits (as for a memory map lock held by a stuck thread); every other file reads as gone."""
        if path.endswith("/cmdline"):
            self.cmdline_reads.append(path)
            self.release.wait(30)
            return "java\0-jar\0app.jar\0"
        return None

    def events(self) -> list[tuple[str, str]]:
        """(kind, msg) of each event the monitor emitted."""
        return [(str(e["kind"]), str(e["msg"])) for e in (json.loads(line) for _, line in self.mon.buffer)]

    def without_waiting(self, report: Callable[[], None]) -> list[tuple[str, str]]:
        """The events report() emits, with the cmdline read blocked; fails if report() waits for it."""
        self.mon.fake = FakeProc(self.m, {"100": ("10", "java", STACKS["sync_log"])})
        done = threading.Event()

        def run() -> None:
            with self.mon.fake, mock.patch.object(self.m, "read_text", self.blocked_read_text):
                report()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(md_domain.CMDLINE_WAIT + 4.5), "the report waited for the blocked cmdline read")
        return self.events()

    def hung(self, pid: str = "10", tid: str = "100") -> None:
        d_tasks = [(pid, tid, "java", STACKS["sync_log"], None, self.m.wait_kind(STACKS["sync_log"], None))]
        self.mon.track_hung(d_tasks, 1000.0)
        self.mon.track_hung(d_tasks, 1000.0 + self.m.HUNG_SECONDS + 1)

    def test_hung_report_does_not_wait_for_a_blocked_cmdline(self) -> None:
        [(kind, msg)] = self.without_waiting(self.hung)
        self.assertEqual(kind, "hung")
        self.assertIn(f"java:100 ({md_domain.CMDLINE_SLOW}; cgroup app.slice)", msg)

    def test_io_busy_report_does_not_wait_for_a_blocked_cmdline(self) -> None:
        now = 5000.0
        task: md_domain.DTaskSince = {
            "pid": "10",
            "comm": "java",
            "first": now,
            "busy": now - self.m.HUNG_SECONDS - 1,
            "slices": 7,
        }
        [(kind, msg)] = self.without_waiting(
            lambda: self.mon.report_io_busy(task, "100", STACKS["sync_log"], None, now)
        )
        self.assertEqual(kind, "io-busy")
        self.assertIn(f"java:100 ({md_domain.CMDLINE_SLOW}; cgroup app.slice)", msg)

    def test_while_one_read_waits_the_next_report_waits_not_at_all(self) -> None:
        def two_reports() -> None:
            self.hung("10", "100")
            self.hung("20", "200")

        events = self.without_waiting(two_reports)
        self.assertEqual(self.cmdline_reads, ["/proc/10/cmdline"])  # the second report did not even try
        self.assertEqual(len(events), 2)
        self.assertIn(f"java:200 ({md_domain.CMDLINE_SLOW};", events[1][1])

    def test_a_readable_cmdline_is_in_the_report(self) -> None:
        self.mon.fake = FakeProc(self.m, {"100": ("10", "java", STACKS["sync_log"])})
        with self.mon.fake, mock.patch.object(self.m, "read_text", lambda _path: "java\0-jar\0app.jar\0"):
            self.hung()
        [(_, msg)] = self.events()
        self.assertIn("java:100 (java -jar app.jar; cgroup app.slice)", msg)


class _FakeNotifier(ServiceNotifier):
    """A ServiceNotifier that records its messages in the test's event list and stops the loop after some pings."""

    def __init__(self, events: list[str], pings: int, ready_fails: int = 0) -> None:
        super().__init__(None)
        self.events = events
        self.pings = pings
        self.ready_fails = ready_fails  # this many READY=1 sends fail first (systemd's queue full)
        self.monitor: md_monitor.Monitor | None = None

    def ready(self) -> bool:
        if self.ready_fails:
            self.ready_fails -= 1
            self.events.append("READY=1 failed")
            return False
        self.events.append("READY=1")
        return True

    def watchdog(self) -> bool:
        self.events.append("WATCHDOG=1")
        self.pings -= 1
        if self.pings == 0 and self.monitor is not None:
            self.monitor.stop()
        return True


class _FakeWriter:
    """The writer thread, not started: its queue only."""

    def __init__(self, _log_dir: str) -> None:
        self.q: queue.Queue[object] = queue.Queue()

    def start(self) -> None:
        return None

    def join(self, timeout: float | None = None) -> None:
        return None


class _FakePoller:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def poll(self, _timeout: float) -> list[tuple[int, int]]:
        self.events.append("poll")
        return []


class NotifyLoopTest(unittest.TestCase):
    """systemd's Type=notify: READY=1 once the PSI trigger and /dev/kmsg are open and SIGTERM is handled, then
    WATCHDOG=1 at every pass of the sampling loop. Every /proc and /sys read, the writer thread and the signal
    handlers are fakes."""

    def setUp(self) -> None:
        self.events: list[str] = []
        topo = types.SimpleNamespace(fs={}, disks=[], top_devs=set(), containers={})
        for target, name, value in (
            (md_monitor, "Topology", lambda: topo),
            (md_monitor, "Writer", _FakeWriter),
            (md_monitor, "read_text", lambda _path: ""),
            (md_monitor, "scan_d_tasks", list),
            (md_monitor, "log", lambda msg, _prio=6: self.events.append("log:" + msg.split(":", 1)[0])),
            (md_monitor.signal, "signal", lambda sig, _handler: self.events.append(f"signal:{sig}")),
            (md_monitor.Monitor, "probe_stack_access", staticmethod(lambda: False)),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def monitor(self, notifier: _FakeNotifier | None) -> md_monitor.Monitor:
        mon = md_monitor.Monitor("/nonexistent/log", "", None, notifier)
        record = self.events.append
        fakes: dict[str, object] = {
            "poller": _FakePoller(self.events),
            "open_psi_trigger": lambda: record("psi"),
            "open_kmsg": lambda: record("kmsg"),
            "meta_record": lambda: {"t": "meta", "ts": 0.0},
            "probe_commits": lambda: None,
            "describe_all": lambda _tasks: [],
            "sample": lambda _tasks: {"t": "s", "ts": 0.0},
            "update_episode": lambda _rec, _tasks: None,
            "track_hung": lambda _tasks, _now: None,
        }
        for name, value in fakes.items():
            setattr(mon, name, value)
        if notifier is not None:
            notifier.monitor = mon
        return mon

    def test_ready_after_setup_then_one_watchdog_per_pass(self) -> None:
        self.monitor(_FakeNotifier(self.events, pings=3)).run()
        term, intr = f"signal:{int(md_monitor.signal.SIGTERM)}", f"signal:{int(md_monitor.signal.SIGINT)}"
        self.assertEqual(
            self.events,
            ["psi", "kmsg", term, intr, "log:started", "READY=1"] + ["WATCHDOG=1", "poll"] * 3 + ["log:stopped"],
        )

    def test_a_ready_that_could_not_be_sent_is_sent_again(self) -> None:
        self.monitor(_FakeNotifier(self.events, pings=3, ready_fails=2)).run()
        self.assertEqual(
            self.events[4:],
            ["log:started", "READY=1 failed", "READY=1 failed", "WATCHDOG=1", "poll", "READY=1"]
            + ["WATCHDOG=1", "poll"] * 2
            + ["log:stopped"],
        )

    def test_a_blocked_cmdline_read_does_not_stop_the_pings(self) -> None:
        """A hung-task report at every pass whose /proc/PID/cmdline read never ends: the loop still pings."""
        release = threading.Event()
        self.addCleanup(release.set)

        def blocked(_path: str) -> str | None:
            release.wait(30)
            return ""

        mon = self.monitor(_FakeNotifier(self.events, pings=3))
        mon.cmdlines = md_proc.TimedReader(md_domain.CMDLINE_WAIT, blocked)

        def track_hung(d_tasks: Sequence[md_domain.DTask], now: float) -> None:
            del d_tasks, now
            self.events.append(mon.where("10", "100", "java"))

        mon.track_hung = track_hung
        done = threading.Event()

        def run() -> None:
            with (
                mock.patch.object(md_monitor, "BASE_INTERVAL", 0.0),  # a sample (and a report) at every pass
                mock.patch.object(md_monitor, "short_cgroup", lambda _pid: "app.slice"),
            ):
                mon.run()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(md_domain.CMDLINE_WAIT + 9.5), "the loop waited for the blocked cmdline read")
        where = f"java:100 ({md_domain.CMDLINE_SLOW}; cgroup app.slice)"
        self.assertEqual(self.events[5:], ["READY=1"] + ["WATCHDOG=1", "poll", where] * 3 + ["log:stopped"])

    def test_without_a_notifier_the_loop_sends_nothing(self) -> None:
        mon = self.monitor(None)
        mon.duration = 0.01  # the default sends nothing: no socket is even opened
        with mock.patch("socket.socket", side_effect=AssertionError("no socket may be opened")) as opened:
            mon.run()
        opened.assert_not_called()
        term, intr = f"signal:{int(md_monitor.signal.SIGTERM)}", f"signal:{int(md_monitor.signal.SIGINT)}"
        self.assertEqual(self.events[:5], ["psi", "kmsg", term, intr, "log:started"])
        self.assertEqual(self.events[-1], "log:stopped")

    def test_main_hands_the_monitor_the_socket_systemd_names(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-stall-notify-")
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "notify")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind(path)
        sock.settimeout(5)
        made: list[tuple[object, ...]] = []

        class _Monitor:
            def __init__(self, *args: object) -> None:
                made.append(args)

            def run(self) -> None:
                return None

        with (
            mock.patch.object(md_cli, "Monitor", _Monitor),
            mock.patch.object(md_cli, "prepare_log_dir", lambda _d: None),
            mock.patch.dict(os.environ, {"NOTIFY_SOCKET": path, "LOGS_DIRECTORY": tmp}),
        ):
            self.assertEqual(md_cli.main([]), 0)
        self.assertEqual(len(made), 1)
        notifier = made[0][3]
        if not isinstance(notifier, ServiceNotifier):
            self.fail(f"main handed the monitor {notifier!r}, not a ServiceNotifier")
        self.assertTrue(notifier.ready())
        self.assertEqual(sock.recv(64), b"READY=1")


if __name__ == "__main__":
    unittest.main()

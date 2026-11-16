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
import shutil
import sys
import tempfile
import types
import unittest
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, cast
from unittest import mock
from tests.script_loader import load_script

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "btrfs-stall-monitord")
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

    def describe_all(
        self, d_tasks: Sequence[tuple[str, str, str]]
    ) -> list[tuple[str, str, str, list[str], str | None, str]]: ...

    def sample(self, d_tasks: Sequence[tuple[str, str, str, list[str], str | None, str]]) -> dict[str, Any]: ...

    def update_episode(
        self, rec: Mapping[str, Any], d_tasks: Sequence[tuple[str, str, str, list[str], str | None, str]]
    ) -> None: ...

    def track_hung(self, d_tasks: Sequence[tuple[str, str, str, list[str], str | None, str]], now: float) -> None: ...


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
    # Loading only defines constants and functions: main() runs under
    # `if __name__ == "__main__"`, which is not the case here.
    mod = load_script(TOOL, "btrfs_stall_monitord")
    return cast("_StallMonitordModule", cast("object", mod))


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


if __name__ == "__main__":
    unittest.main()

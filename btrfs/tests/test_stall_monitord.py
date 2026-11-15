"""Unit tests for the file readers of btrfs/btrfs-stall-monitord (no root needed).

The daemon reads /proc and /sys files. Some of them, like /proc/vmstat, are
kernel "seq files": one read() returns only about one page (4 KiB), and the
rest comes in later reads. These tests make a temporary file behave the same
way by limiting each os.read() to one page, so they never touch the real
/proc. An old reader that did a single read() fails them: it lost every line
after the first page (zswpin, zswpout and zswpwb in /proc/vmstat).

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_stall_monitord -v
"""

import contextlib
import errno
import importlib.machinery
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "btrfs-stall-monitord")
PAGE = 4096


def load():
    # Loading only defines constants and functions: main() runs under
    # `if __name__ == "__main__"`, which is not the case here.
    loader = importlib.machinery.SourceFileLoader("btrfs_stall_monitord", TOOL)
    spec = importlib.util.spec_from_loader("btrfs_stall_monitord", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def fake_vmstat():
    """A vmstat text larger than one page, with the zswap lines after byte 4096."""
    lines = [f"filler_counter_{i:04d} {i}" for i in range(200)]
    head = "nr_dirty 11\npswpout 6830873\n" + "\n".join(lines) + "\n"
    assert len(head) > PAGE + 100
    return head + "zswpin 123\nzswpout 456789\nzswpwb 6830873\n"


class ReadBytesTest(unittest.TestCase):
    def setUp(self):
        self.m = load()
        self.tmp = tempfile.mkdtemp(prefix="stall-monitord-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.opened = []
        self.reads = 0
        real_open, real_read = os.open, os.read

        def tracking_open(path, flags, *args, **kwargs):
            fd = real_open(path, flags, *args, **kwargs)
            self.opened.append(fd)
            return fd

        def page_read(fd, n):
            # A seq file: never more than one page per read().
            self.reads += 1
            return real_read(fd, min(n, PAGE))

        self.real_read = real_read
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(self.m.os, "open", tracking_open))
        stack.enter_context(mock.patch.object(self.m.os, "read", page_read))

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def assert_closed(self):
        self.assertTrue(self.opened, "read_bytes did not open the file")
        for fd in self.opened:
            with self.assertRaises(OSError) as cm:
                os.fstat(fd)
            self.assertEqual(cm.exception.errno, errno.EBADF, f"fd {fd} left open")

    def test_reads_past_the_first_page(self):
        data = bytes(range(256)) * 40          # 10240 bytes: two and a half pages
        path = self.write("big", data)
        self.assertEqual(self.m.read_bytes(path), data)
        self.assertGreaterEqual(self.reads, 3)
        self.assert_closed()

    def test_vmstat_zswap_counters_after_first_page(self):
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

    def test_read_text_returns_whole_file(self):
        text = fake_vmstat()
        path = self.write("vmstat", text.encode())
        self.assertEqual(self.m.read_text(path), text)

    def test_size_caps_the_result(self):
        path = self.write("stat", b"x" * (3 * PAGE))
        got = self.m.read_bytes(path, 1024)
        self.assertEqual(got, b"x" * 1024)
        self.assertEqual(self.reads, 1)        # stops once it has `size` bytes
        self.assert_closed()

    def test_size_cap_across_several_reads(self):
        path = self.write("big", b"y" * (5 * PAGE))
        got = self.m.read_bytes(path, 2 * PAGE + 10)
        self.assertEqual(len(got), 2 * PAGE + 10)
        self.assert_closed()

    def test_default_cap_is_64_kib(self):
        path = self.write("huge", b"z" * (70 * 1024))
        self.assertEqual(len(self.m.read_bytes(path)), 65536)

    def test_short_file_under_the_cap(self):
        path = self.write("stat", b"1234 (bash) S 1\n")
        self.assertEqual(self.m.read_bytes(path, 1024), b"1234 (bash) S 1\n")
        self.assert_closed()

    def test_empty_file(self):
        path = self.write("empty", b"")
        self.assertEqual(self.m.read_bytes(path), b"")
        self.assertEqual(self.m.read_text(path), "")
        self.assertEqual(self.m.read_kv(path), {})
        self.assert_closed()

    def test_missing_path(self):
        missing = os.path.join(self.tmp, "no-such-file")
        self.assertIsNone(self.m.read_bytes(missing))
        self.assertIsNone(self.m.read_text(missing))
        self.assertEqual(self.m.read_kv(missing), {})
        self.assertEqual(self.m.read_int(missing), 0)
        self.assertEqual(self.opened, [])

    def test_error_during_first_read(self):
        path = self.write("big", b"a" * (2 * PAGE))

        def failing_read(fd, n):
            raise OSError(errno.EIO, "Input/output error")

        with mock.patch.object(self.m.os, "read", failing_read):
            self.assertIsNone(self.m.read_bytes(path))
        self.assert_closed()

    def test_error_after_the_first_page(self):
        # A task can exit between two reads of its /proc files: the next
        # read() fails (ESRCH). The partial data must not be returned.
        path = self.write("big", b"a" * (3 * PAGE))
        calls = []

        def second_read_fails(fd, n):
            calls.append(n)
            if len(calls) > 1:
                raise OSError(errno.ESRCH, "No such process")
            return self.real_read(fd, min(n, PAGE))

        with mock.patch.object(self.m.os, "read", second_read_fails):
            self.assertIsNone(self.m.read_bytes(path))
        self.assertEqual(len(calls), 2)
        self.assert_closed()


if __name__ == "__main__":
    unittest.main()

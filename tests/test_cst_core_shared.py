"""Unit tests for the concepts several CST modules share through cst_core.

errors, text, units, sizes, owned-file states (domain); file reads, journal
lines, run locks and the NOCOW attribute (adapters). The unit renderer is
checked against the timers the vm and toolchains programs write today, so
those modules can switch to it without changing a byte.
"""

import io
import os
import shutil
import struct
import tempfile
from typing import Any
import unittest
from unittest import mock

from cst_core.adapters import nocow
from cst_core.adapters.files import read_bytes, read_int, read_text
from cst_core.adapters.journal import log
from cst_core.adapters.locks import take_lock
from cst_core.domain.errors import Refused, Usage
from cst_core.domain.files import owned_file_state
from cst_core.domain.text import first_line
from cst_core.domain.units import render_timer, render_unit_file
from cst_core.domain.values import GIB, MIB, format_bytes_gib_mib
from tests.script_loader import load_script

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DomainTest(unittest.TestCase):
    def test_errors_are_distinct_exceptions(self) -> None:
        self.assertTrue(issubclass(Usage, Exception) and issubclass(Refused, Exception))
        self.assertFalse(issubclass(Usage, Refused) or issubclass(Refused, Usage))

    def test_first_line(self) -> None:
        self.assertEqual(first_line("\n  \n  error: x \nmore"), "error: x")
        self.assertEqual(first_line(None), "")
        self.assertEqual(first_line(" \n"), "")

    def test_sizes(self) -> None:
        self.assertEqual((MIB, GIB), (1 << 20, 1 << 30))
        self.assertEqual(format_bytes_gib_mib(3 * MIB // 2), "1 MiB")
        self.assertEqual(format_bytes_gib_mib(3 * MIB // 2, mib_floor=False), "2 MiB")
        self.assertEqual(format_bytes_gib_mib(GIB // 10 + 1, gib_threshold=GIB / 10), "0.1 GiB")
        self.assertEqual(format_bytes_gib_mib(12 * GIB, gib_threshold=10 * GIB, comma=True), "12.0 GiB")
        self.assertEqual(format_bytes_gib_mib(1500 * MIB, gib_threshold=10 * GIB, comma=True), "1,500 MiB")

    def test_owned_file_state(self) -> None:
        self.assertEqual(owned_file_state("a", is_link=False, wanted="a", ours=True), "ok")
        self.assertEqual(owned_file_state("b", is_link=False, wanted="a", ours=True), "change")
        self.assertEqual(owned_file_state("b", is_link=False, wanted="a", ours=False), "conflict")
        self.assertEqual(owned_file_state(None, is_link=False, wanted="a", ours=False), "missing")
        self.assertEqual(owned_file_state("a", is_link=True, wanted="a", ours=True), "conflict")
        self.assertEqual(owned_file_state("a", is_link=False, wanted=None, ours=True), "change")

    def test_render_unit_layout(self) -> None:
        text = render_unit_file(["# head"], [("Unit", ["Description=x"]), ("Service", ["# why", "Type=oneshot"])])
        self.assertEqual(text, "# head\n[Unit]\nDescription=x\n\n[Service]\n# why\nType=oneshot\n")

    def test_render_timer_matches_the_toolchains_timer(self) -> None:
        tc: Any = load_script(os.path.join(REPO, "toolchains", "cst-toolchains"), "cst_toolchains_units")
        header = [f"{tc.MARKER}: installed by `cst toolchains auto on` (or snapshots setup); not edited by hand."]
        self.assertEqual(
            render_timer(header, "CST: prune old toolchain caches, hourly", "hourly", "10min"), tc.render_timer()
        )

    def test_render_timer_matches_the_vm_backup_timer(self) -> None:
        vm: Any = load_script(os.path.join(REPO, "vm", "cst-vm"), "cst_vm_units")
        header = [
            f"{vm.UNIT_MARKER}: installed by `cst vm backup-timer on`; not edited by hand.",
            "# Saturday morning: the /home NAS job starts between 00:00 and 01:00 every",
            "# day, the btrfs scrub runs on Sunday at 02:00.",
        ]
        text = render_timer(header, "CST: weekly backup of the VM %i to the NAS", vm.ON_CALENDAR, "30min")
        self.assertEqual(text, vm.render_timer())


class AdaptersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-shared-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def path(self, name: str, data: bytes | None = None) -> str:
        p = os.path.join(self.tmp, name)
        if data is not None:
            with open(p, "wb") as fh:
                fh.write(data)
        return p

    def test_reads(self) -> None:
        p = self.path("f", b" 42\n\xff")
        self.assertEqual(read_text(p), " 42\n�")
        self.assertEqual(read_text(p, "", limit=3), " 42")
        self.assertIsNone(read_text(self.path("missing")))
        self.assertEqual(read_text(self.path("missing"), "d"), "d")
        self.assertEqual(read_bytes(p, limit=2), b" 4")
        self.assertIsNone(read_bytes(self.path("missing")))
        self.assertEqual(read_int(self.path("n", b"-7\n")), -7)
        self.assertIsNone(read_int(self.path("x", b"7 MiB")))
        self.assertEqual(read_int(self.path("missing"), 0), 0)

    def test_journal_line(self) -> None:
        out = io.StringIO()
        log(4, "slow", tag="docker-gc", stream=out)
        log(6, "plain", stream=out)
        self.assertEqual(out.getvalue(), "<4>docker-gc: slow\n<6>plain\n")

    def test_lock_is_exclusive_and_refuses_a_link(self) -> None:
        lock = self.path("lock")
        fd = take_lock(lock, wait=False)
        assert fd is not None
        self.assertIsNone(take_lock(lock, wait=False), "a second holder got the lock")
        self.assertEqual(os.stat(lock).st_mode & 0o777, 0o600)
        os.symlink(lock, self.path("link"))
        with self.assertRaises(OSError):
            take_lock(self.path("link"), wait=False)
        os.close(fd)
        fd2 = take_lock(lock, wait=True)
        assert fd2 is not None
        os.close(fd2)

    def test_nocow_sets_and_clears_only_its_bit(self) -> None:
        p = self.path("img", b"")
        flags = {"v": 0x80}  # another attribute that must stay

        def ioctl(fd: int, req: int, arg: bytes) -> bytes:
            if req == nocow.FS_IOC_GETFLAGS:
                return struct.pack("l", flags["v"])
            flags["v"] = struct.unpack("l", arg)[0]
            return arg

        with mock.patch.object(nocow.fcntl, "ioctl", ioctl):
            self.assertTrue(nocow.set_nocow(p))
            self.assertEqual(flags["v"], 0x80 | nocow.FS_NOCOW_FL)
            self.assertFalse(nocow.set_nocow(p), "set twice")
            self.assertEqual(nocow.inode_flags(p), 0x80 | nocow.FS_NOCOW_FL)
            self.assertTrue(nocow.set_nocow(p, on=False))
            self.assertEqual(flags["v"], 0x80)
            with self.assertRaises(OSError):
                nocow.set_nocow(p, directory=True)  # not a folder
        os.symlink(p, self.path("link"))
        with self.assertRaises(OSError):
            nocow.inode_flags(self.path("link"))


if __name__ == "__main__":
    unittest.main()

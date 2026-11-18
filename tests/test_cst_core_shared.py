# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the concepts several CST modules share through cst_core.

errors, text, units, sizes, owned-file states (domain); file reads, journal
lines, run locks, the NOCOW attribute and sd_notify messages to systemd (adapters). The unit renderer is
checked against the timers the vm and toolchains programs write today, so
those modules can switch to it without changing a byte.
"""

from collections.abc import Callable, Sequence
import contextlib
import hashlib
import io
import os
import re
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from cst_core import ports as core_ports
from cst_core.adapters import runner as core_runner, virsh as core_virsh
from cst_core.adapters import files as core_files, nocow
from cst_core.adapters.files import (
    CounterFile,
    ensure_dir,
    is_masked_unit,
    owned_file_state_at,
    read_bytes,
    read_int,
    read_text,
    unit_file_state_at,
)
from cst_core.adapters.journal import log
from cst_core.adapters import notify as core_notify
from cst_core.adapters.notify import Notifier
from cst_core.adapters.sd_notify import ServiceNotifier
from cst_core.adapters.locks import take_lock
from cst_core.adapters.terminal import confirm
from cst_core.domain.errors import Refused, Usage
from doctor.domain import parse_alert_text
from cst_core.domain.files import owned_file_state
from cst_core.domain.files import checked_path
from cst_core.domain.text import (
    describe_problems,
    first_line,
    is_yes,
    parse_shell_assignments,
    parse_snapshots_conf,
    printf_q,
    printf_q_words,
    read_fields,
)
from cst_core.domain.units import masked_refusal, render_timer, render_unit_file
from cpu.setup.domain import conf_lines as cpu_setup_conf_lines
from cst_core.domain.values import GIB, MIB, format_bytes_gib_mib
from tests.probes import assert_replaced_durably, recorded_fsyncs
from toolchains.domain import auto as tcd_auto
from toolchains.domain import common as tcd_common
from vm import domain as vm_domain

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CheckedPathTest(unittest.TestCase):
    def test_plain_absolute_paths_pass(self) -> None:
        for p in ("/", "/etc/x.conf", "/home/u/.config/a b"):
            self.assertEqual(checked_path(p), p)

    def test_other_paths_are_refused(self) -> None:
        for p in ("", "etc/x", "./x", "/etc/../tmp/x", "/etc/./x", "//etc/x", "/etc//x", "/etc/x/", "/etc/x\0y"):
            with self.subTest(p=p), self.assertRaises(ValueError):
                checked_path(p)


class DomainTest(unittest.TestCase):
    def test_errors_are_distinct_exceptions(self) -> None:
        self.assertTrue(issubclass(Usage, Exception) and issubclass(Refused, Exception))
        self.assertFalse(issubclass(Usage, Refused) or issubclass(Refused, Usage))

    def test_is_yes(self) -> None:
        for answer in ("y", "Y", "yes", " s", "Sim\n"):
            self.assertTrue(is_yes(answer), answer)
        for answer in ("", "n", "N", "no", " ", "x y", "não"):
            self.assertFalse(is_yes(answer), answer)

    def test_read_fields_as_bash_read_assigns_them(self) -> None:
        lines = ["  a  b   c  d  ", "a", "", "\ta\tb", "a b c\t", "x  y", "a\x0bb c", " \t "]
        for line in lines:
            for n in (1, 2, 3):
                names = " ".join(f"v{i}" for i in range(n))
                values = " ".join(f'"$v{i}"' for i in range(n))
                r = subprocess.run(
                    ["bash", "-c", f'read -r {names} <<<"$1"; printf "%s\\x1f" {values}', "_", line],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL, check=True,
                )  # fmt: skip
                with self.subTest(line=line, n=n):
                    self.assertEqual(read_fields(line, n), r.stdout.split("\x1f")[:-1])

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
        for owner in ("root", "user"):
            with self.subTest(owner=owner):
                self.assertEqual(owned_file_state(b"a", is_link=False, wanted=b"a", ours=True, owner=owner), "ok")
                self.assertEqual(owned_file_state(b"b", is_link=False, wanted=b"a", ours=True, owner=owner), "change")
                self.assertEqual(
                    owned_file_state(b"b", is_link=False, wanted=b"a", ours=False, owner=owner), "conflict"
                )
                self.assertEqual(owned_file_state(None, is_link=False, wanted=b"a", ours=False, owner=owner), "missing")
                self.assertEqual(owned_file_state(b"a", is_link=False, wanted=None, ours=True, owner=owner), "change")
        # The installer's link rule: replaced at a root destination, judged by its target at a user one.
        self.assertEqual(owned_file_state(b"a", is_link=True, wanted=b"a", ours=True, owner="root"), "change")
        self.assertEqual(owned_file_state(None, is_link=True, wanted=b"a", ours=False, owner="root"), "change")
        self.assertEqual(owned_file_state(b"a", is_link=True, wanted=b"a", ours=True, owner="user"), "ok")
        self.assertEqual(owned_file_state(b"b", is_link=True, wanted=b"a", ours=True, owner="user"), "change")
        self.assertEqual(owned_file_state(b"b", is_link=True, wanted=b"a", ours=False, owner="user"), "conflict")

    def test_render_unit_layout(self) -> None:
        text = render_unit_file(["# head"], [("Unit", ["Description=x"]), ("Service", ["# why", "Type=oneshot"])])
        self.assertEqual(text, "# head\n[Unit]\nDescription=x\n\n[Service]\n# why\nType=oneshot\n")

    def test_masked_refusal_names_each_unit_and_how_to_unmask_it(self) -> None:
        self.assertIsNone(masked_refusal([], user=False))
        self.assertEqual(
            masked_refusal(["/etc/systemd/system/a@.timer", "/etc/systemd/system/a@vm.timer"], user=False),
            "a@.timer is masked (systemctl mask); unmask it first: sudo systemctl unmask a@.timer; "
            "a@vm.timer is masked (systemctl mask); unmask it first: sudo systemctl unmask a@vm.timer",
        )
        self.assertEqual(
            masked_refusal(["/home/u/.config/systemd/user/b.timer"], user=True),
            "b.timer is masked (systemctl mask); unmask it first: systemctl --user unmask b.timer",
        )

    def test_render_timer_matches_the_toolchains_timer(self) -> None:
        # The timer cst-toolchains wrote before it used render_timer, byte for byte.
        before = (
            "# managed-by: toolchains-setup.sh (CST): installed by `cst toolchains auto on` (or snapshots setup); "
            "not edited by hand.\n"
            "[Unit]\nDescription=CST: prune old toolchain caches, hourly\n\n"
            "[Timer]\nOnCalendar=hourly\nRandomizedDelaySec=10min\nPersistent=true\n\n"
            "[Install]\nWantedBy=timers.target\n"
        )
        header = [
            f"{tcd_common.MARKER}: installed by `cst toolchains auto on` (or snapshots setup); not edited by hand."
        ]
        self.assertEqual(render_timer(header, "CST: prune old toolchain caches, hourly", "hourly", "10min"), before)
        self.assertEqual(tcd_auto.render_timer(), before)

    def test_render_timer_matches_the_vm_backup_timer(self) -> None:
        # vm renders its timer with render_timer; the bytes stay those of the
        # text cst-vm wrote before (a change would rewrite every installed timer).
        before = (
            "# Managed by cst vm (CST): installed by `cst vm backup-timer on`; not edited by hand.\n"
            "# Saturday morning: the /home NAS job starts between 00:00 and 01:00 every\n"
            "# day, the btrfs scrub runs on Sunday at 02:00.\n"
            "[Unit]\n"
            "Description=CST: weekly backup of the VM %i to the NAS\n"
            "\n"
            "[Timer]\n"
            "OnCalendar=Sat *-*-* 04:30:00\n"
            "RandomizedDelaySec=30min\n"
            "Persistent=true\n"
            "\n"
            "[Install]\n"
            "WantedBy=timers.target\n"
        )
        self.assertEqual(vm_domain.render_timer(), before)


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

    def test_read_int_gives_the_default_for_anything_but_an_ascii_integer(self) -> None:
        # "--5" and a superscript two passed str.isdigit and then raised ValueError out of int().
        superscript_two, arabic_three = chr(0xB2).encode(), chr(0x663).encode()
        for i, data in enumerate((b"--5", superscript_two, b"-", b"", b"1.5", b"+3", b"\xff", arabic_three)):
            with self.subTest(data=data):
                self.assertEqual(read_int(self.path(f"bad{i}", data), -1), -1)
        self.assertEqual(read_int(self.path("ok", b"  12 \n")), 12)

    def test_the_two_text_readers_keep_their_decode_policies(self) -> None:
        # Lenient (files.read_text): bad bytes become U+FFFD. Byte-transparent
        # (LocalFileSystemAdapter.read_text, for files written back): every byte survives a round trip.
        p = self.path("mixed", b"a\xffb\r\nc\n")
        self.assertEqual(read_text(p), "a" + chr(0xFFFD) + "b\nc\n")
        fs = core_files.LocalFileSystemAdapter()
        text = fs.read_text(p)
        self.assertEqual(text, "a" + chr(0xDCFF) + "b\r\nc\n")
        assert text is not None
        out = self.path("out")
        fs.write_atomic(out, text)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), b"a\xffb\r\nc\n")

    def test_ensure_dir_gives_exactly_the_mode_to_what_it_creates(self) -> None:
        existing = self.path("existing")
        os.mkdir(existing, 0o700)
        target = os.path.join(existing, "a", "b")
        old = os.umask(0o077)
        try:
            created = ensure_dir(target, 0o755)
            self.assertEqual(ensure_dir(target, 0o755), [], "a second call creates nothing")
        finally:
            os.umask(old)
        self.assertEqual(created, [os.path.join(existing, "a"), target])
        for d in created:
            self.assertEqual(os.stat(d).st_mode & 0o7777, 0o755, d)
        self.assertEqual(os.stat(existing).st_mode & 0o7777, 0o700, "an existing folder was changed")
        with self.assertRaises(FileExistsError):
            ensure_dir(self.path("file", b"x"), 0o755)

    def test_owned_file_state_at(self) -> None:
        ours = self.path("ours", b"#!/x\nMARK v1\n")
        theirs = self.path("theirs", b"#!/x\nv1\n")
        os.symlink(ours, self.path("link"))
        os.mkdir(self.path("dir"))

        def mark(data: bytes) -> bool:
            return b"MARK" in data

        for owner in ("root", "user"):
            with self.subTest(owner=owner):
                self.assertEqual(owned_file_state_at(ours, b"#!/x\nMARK v1\n", mark, owner=owner), "ok")
                self.assertEqual(owned_file_state_at(ours, b"#!/x\nMARK v2\n", mark, owner=owner), "change")
                self.assertEqual(owned_file_state_at(theirs, b"#!/x\nMARK v2\n", mark, owner=owner), "conflict")
                self.assertEqual(owned_file_state_at(theirs, b"#!/x\nv1\n", mark, owner=owner), "ok")
                self.assertEqual(owned_file_state_at(self.path("missing"), b"x", mark, owner=owner), "missing")
                self.assertEqual(owned_file_state_at(self.path("dir"), b"x", mark, owner=owner), "conflict")
                self.assertEqual(owned_file_state_at(ours, None, mark, owner=owner), "change")
        link = self.path("link")
        os.symlink(theirs, self.path("link-theirs"))
        os.symlink(self.path("missing"), self.path("dangling"))
        # One link rule with the installer: at a root destination a link is
        # always replaced ("change", whatever it points to); at a user destination its target counts.
        self.assertEqual(owned_file_state_at(link, b"#!/x\nMARK v1\n", mark, owner="root"), "change")
        self.assertEqual(owned_file_state_at(self.path("link-theirs"), b"y", mark, owner="root"), "change")
        self.assertEqual(owned_file_state_at(link, b"#!/x\nMARK v1\n", mark, owner="user"), "ok")
        self.assertEqual(owned_file_state_at(link, b"#!/x\nMARK v2\n", mark, owner="user"), "change")
        self.assertEqual(owned_file_state_at(self.path("link-theirs"), b"y", mark, owner="user"), "conflict")
        self.assertEqual(owned_file_state_at(self.path("dangling"), b"y", mark, owner="user"), "missing")

    def test_unit_file_state_at_sees_a_mask(self) -> None:
        # `systemctl mask` leaves a link to /dev/null; the link rule alone replaced it (root) or
        # called it someone else's file (user).
        def mark(data: bytes) -> bool:
            return b"MARK" in data

        masked = self.path("masked.timer")
        os.symlink("/dev/null", masked)
        os.symlink("/dev/null", self.path("hop"))
        chained = self.path("chained.timer")
        os.symlink(self.path("hop"), chained)
        ours = self.path("ours.timer", b"MARK v1\n")
        os.symlink(ours, self.path("linked.timer"))
        os.symlink(self.path("missing"), self.path("dangling.timer"))
        empty = self.path("empty.timer", b"")
        for owner in ("root", "user"):
            with self.subTest(owner=owner):
                self.assertTrue(is_masked_unit(masked))
                self.assertTrue(is_masked_unit(chained))
                self.assertEqual(unit_file_state_at(masked, b"MARK v1\n", mark, owner=owner), "masked")
                self.assertEqual(unit_file_state_at(chained, b"MARK v1\n", mark, owner=owner), "masked")
                # Anything else: the link rule, exactly as owned_file_state_at.
                for path in (ours, self.path("linked.timer"), self.path("dangling.timer"), empty):
                    self.assertFalse(is_masked_unit(path), path)
                    self.assertEqual(
                        unit_file_state_at(path, b"MARK v1\n", mark, owner=owner),
                        owned_file_state_at(path, b"MARK v1\n", mark, owner=owner),
                        path,
                    )
        self.assertFalse(is_masked_unit(self.path("missing")))
        self.assertEqual(unit_file_state_at(self.path("linked.timer"), b"MARK v1\n", mark, owner="root"), "change")

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

    def test_a_wait_with_a_timeout_gives_up_and_takes_a_freed_lock(self) -> None:
        lock = self.path("lock")
        fd = take_lock(lock, wait=False)
        assert fd is not None
        start = time.monotonic()
        self.assertIsNone(take_lock(lock, wait=True, timeout=0.3))
        self.assertGreaterEqual(time.monotonic() - start, 0.3)
        os.close(fd)
        fd2 = take_lock(lock, wait=True, timeout=0.3)
        assert fd2 is not None
        os.close(fd2)

    def test_a_lock_file_it_creates_gets_exactly_the_mode(self) -> None:
        # Under a 077 umask (sudo keeps a user's) the 0644 lock cst doctor reads came out 0600.
        lock = self.path("pid.lock")
        old = os.umask(0o077)
        try:
            fd = take_lock(lock, wait=False, mode=0o644)
        finally:
            os.umask(old)
        assert fd is not None
        os.close(fd)
        self.assertEqual(os.stat(lock).st_mode & 0o7777, 0o644)
        os.chmod(lock, 0o600)
        fd = take_lock(lock, wait=False, mode=0o644)  # an existing file keeps its mode
        assert fd is not None
        os.close(fd)
        self.assertEqual(os.stat(lock).st_mode & 0o7777, 0o600)
        # A dangling link without nofollow: created through it, as before (no endless retry).
        os.symlink(self.path("lock-target"), self.path("dangling"))
        fd = take_lock(self.path("dangling"), wait=False, nofollow=False)
        assert fd is not None
        os.close(fd)
        self.assertTrue(os.path.exists(self.path("lock-target")))

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


class CounterFileTest(unittest.TestCase):
    """CounterFile: the boot counter of btrfs-bulk and docker-volumes-nocow."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-counter-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.path = os.path.join(self.tmp, "attempts")
        self.counter = CounterFile(self.path)

    def test_absent_or_not_a_number_counts_as_zero(self) -> None:
        self.assertEqual(self.counter.count(), 0)
        with open(self.path, "w") as f:
            f.write("two")
        self.assertEqual(self.counter.count(), 0)
        os.remove(self.path)
        os.mkdir(self.path)  # unreadable as a file
        self.assertEqual(self.counter.count(), 0)

    def test_save_is_durable_and_read_back(self) -> None:
        with recorded_fsyncs() as synced:
            self.counter.save(2)
        assert_replaced_durably(self, self.path, synced)
        self.assertEqual(self.counter.count(), 2)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o644)
        with open(self.path) as f:
            self.assertEqual(f.read(), "2")

    def test_remove_needs_a_counter_and_forget_does_not(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.counter.remove()
        self.counter.forget()
        self.counter.save(1)
        self.counter.remove()
        self.assertFalse(os.path.exists(self.path))
        self.counter.save(1)
        self.counter.forget()
        self.assertFalse(os.path.exists(self.path))


class WriteBytesAtomicTest(unittest.TestCase):
    """write_bytes_atomic: the mode is exact, a link is replaced (not followed), restorecon as root."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-wba-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_mode_ignores_umask_and_a_link_is_replaced(self) -> None:
        target = os.path.join(self.tmp, "target")
        with open(target, "wb") as fh:
            fh.write(b"keep")
        dest = os.path.join(self.tmp, "sub", "dest")
        os.makedirs(os.path.dirname(dest))
        os.symlink(target, dest)
        old = os.umask(0o077)
        try:
            core_files.write_bytes_atomic(dest, b"new", mode=0o755)
        finally:
            os.umask(old)
        self.assertFalse(os.path.islink(dest))
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"new")
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"keep")
        self.assertEqual(os.stat(dest).st_mode & 0o7777, 0o755)
        self.assertEqual(sorted(os.listdir(os.path.dirname(dest))), ["dest"])  # no temporary file left

    def restorecon(self, dest: str, rc: int, err: str) -> tuple[list[list[str]], str]:
        calls: list[list[str]] = []

        def run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, rc, "", err)

        stderr = io.StringIO()
        with (
            mock.patch.object(core_files.os, "geteuid", return_value=0),
            mock.patch.object(core_files.os, "fchown"),
            mock.patch.object(core_files.shutil, "which", return_value="/usr/sbin/restorecon"),
            mock.patch.object(core_files.subprocess, "run", side_effect=run),
            contextlib.redirect_stderr(stderr),
        ):
            core_files.write_bytes_atomic(dest, b"x")
        return calls, stderr.getvalue()

    def test_as_root_restorecon_gets_the_path_as_an_operand(self) -> None:
        dest = os.path.join(self.tmp, "-odd")
        calls, err = self.restorecon(dest, 0, "")
        self.assertEqual(calls, [["/usr/sbin/restorecon", "-F", "--", dest]])
        self.assertEqual(err, "")

    def test_a_failed_restorecon_is_reported(self) -> None:
        # Its result was dropped.
        dest = os.path.join(self.tmp, "f")
        _, err = self.restorecon(dest, 255, "restorecon: lstat(f) failed: Permission denied\n")
        self.assertEqual(
            err,
            f"warning: restorecon {dest}: restorecon: lstat(f) failed: Permission denied "
            "(its SELinux label may be wrong)\n",
        )
        with open(dest, "rb") as fh:
            self.assertEqual(fh.read(), b"x")

    def test_a_link_at_dest_is_never_removed_before_the_rename(self) -> None:
        # Dest was unlinked first, a moment with no file at all.
        target = os.path.join(self.tmp, "target")
        with open(target, "wb") as fh:
            fh.write(b"keep")
        dest = os.path.join(self.tmp, "dest")
        os.symlink(target, dest)
        with mock.patch.object(core_files.os, "unlink", side_effect=AssertionError("unlink called")):
            core_files.write_bytes_atomic(dest, b"new", mode=0o644)
        self.assertFalse(os.path.islink(dest))

    def test_new_folders_get_exactly_0755_under_a_077_umask(self) -> None:
        dest = os.path.join(self.tmp, "new", "sub", "f")
        old = os.umask(0o077)
        try:
            core_files.write_bytes_atomic(dest, b"x", mode=0o644)
        finally:
            os.umask(old)
        for d in (os.path.join(self.tmp, "new"), os.path.dirname(dest)):
            self.assertEqual(os.stat(d).st_mode & 0o7777, 0o755, d)


class WriteAtomicTest(unittest.TestCase):
    """cst_core.adapters.files.write_atomic: unique temp, exact mode, fsync file then folder, temp gone on errors."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-wa-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.dest = os.path.join(self.tmp, "sub", "counter")

    def record(self) -> tuple[list[str], contextlib.ExitStack]:
        events: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd: int) -> None:
            events.append("fsync folder" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync file")
            real_fsync(fd)

        def replace(src: str, dst: str) -> None:
            events.append("replace")
            real_replace(src, dst)

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(core_files.os, "fsync", fsync))
        stack.enter_context(mock.patch.object(core_files.os, "replace", replace))
        return events, stack

    def test_durable_write_syncs_the_file_then_the_folder(self) -> None:
        events, stack = self.record()
        old = os.umask(0o077)
        try:
            with stack:
                core_files.write_atomic(self.dest, "2", mode=0o644)
        finally:
            os.umask(old)
        self.assertEqual(events, ["fsync file", "replace", "fsync folder"])
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), b"2")
        self.assertEqual(os.stat(self.dest).st_mode & 0o7777, 0o644)
        self.assertEqual(os.stat(os.path.dirname(self.dest)).st_mode & 0o7777, 0o755)
        self.assertEqual(os.listdir(os.path.dirname(self.dest)), ["counter"])

    def test_not_durable_skips_the_syncs(self) -> None:
        events, stack = self.record()
        with stack:
            core_files.write_atomic(self.dest, b"x", mode=0o600, durable=False)
        self.assertEqual(events, ["replace"])
        self.assertEqual(os.stat(self.dest).st_mode & 0o7777, 0o600)

    def test_file_durability_syncs_the_file_but_not_the_folder(self) -> None:
        events, stack = self.record()
        with stack:
            core_files.write_atomic(self.dest, b"x", mode=0o644, durable="file")
        self.assertEqual(events, ["fsync file", "replace"])
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), b"x")

    def test_umask_mode_is_what_open_gives_a_new_file(self) -> None:
        os.makedirs(os.path.dirname(self.dest))
        plain = os.path.join(self.tmp, "plain")
        for umask in (0o022, 0o077, 0o002):
            old = os.umask(umask)
            try:
                with open(plain, "w"):
                    pass
                core_files.write_atomic(self.dest, b"x", mode="umask", durable=False)
            finally:
                os.umask(old)
            with self.subTest(umask=oct(umask)):
                self.assertEqual(os.stat(self.dest).st_mode & 0o7777, 0o666 & ~umask)
                self.assertEqual(os.stat(self.dest).st_mode & 0o7777, os.stat(plain).st_mode & 0o7777)
            os.unlink(plain)

    def test_locale_bytes_are_what_open_w_writes(self) -> None:
        plain = os.path.join(self.tmp, "plain")
        text = "a\nentry é\n"
        with open(plain, "w") as fh:
            fh.write(text)
        with open(plain, "rb") as fh:
            self.assertEqual(core_files.locale_bytes(text), fh.read())

    def test_the_umask_is_read_without_changing_it(self) -> None:
        old = os.umask(0o027)
        try:
            with mock.patch.object(core_files.os, "umask", side_effect=AssertionError("umask changed")):
                self.assertEqual(core_files._umask(), 0o027)
        finally:
            os.umask(old)

    def test_without_proc_the_umask_is_assumed_strict_and_never_changed(self) -> None:
        with (
            mock.patch("builtins.open", side_effect=OSError("no /proc")),
            mock.patch.object(core_files.os, "umask", side_effect=AssertionError("umask changed")),
        ):
            self.assertEqual(core_files._umask(), 0o077)

    def test_relabel_false_starts_no_restorecon_as_root(self) -> None:
        for relabel, calls in ((True, 1), (False, 0)):
            with (
                self.subTest(relabel=relabel),
                mock.patch.object(core_files.os, "geteuid", return_value=0),
                mock.patch.object(core_files.shutil, "which", return_value="/usr/sbin/restorecon"),
                mock.patch.object(
                    core_files.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
                ) as run,
            ):
                core_files.write_atomic(self.dest, b"x", mode=0o644, relabel=relabel)
                self.assertEqual(run.call_count, calls)

    def test_the_temporary_file_is_removed_on_every_error(self) -> None:
        os.makedirs(os.path.dirname(self.dest))
        with open(self.dest, "w") as fh:
            fh.write("old")
        for name, error in (("write", OSError(28, "No space left on device")), ("replace", KeyboardInterrupt())):
            with self.subTest(name=name), mock.patch.object(core_files.os, name, side_effect=error):
                with self.assertRaises(type(error)):
                    core_files.write_atomic(self.dest, b"new", mode=0o644)
                self.assertEqual(os.listdir(os.path.dirname(self.dest)), ["counter"])
                with open(self.dest) as fh:
                    self.assertEqual(fh.read(), "old")

    def test_owner_root_chowns_only_as_root(self) -> None:
        for owner, euid, calls in (("root", 0, 1), ("caller", 0, 0), ("root", 1000, 0)):
            with (
                self.subTest(owner=owner, euid=euid),
                mock.patch.object(core_files.os, "geteuid", return_value=euid),
                mock.patch.object(core_files.os, "fchown") as fchown,
                mock.patch.object(core_files.shutil, "which", return_value=None),
            ):
                core_files.write_atomic(self.dest, b"x", mode=0o644, owner=owner)
                self.assertEqual(fchown.call_count, calls)
                if calls:
                    self.assertEqual(fchown.call_args.args[1:], (0, 0))

    def test_a_link_at_dest_is_replaced_not_followed(self) -> None:
        target = os.path.join(self.tmp, "target")
        with open(target, "w") as fh:
            fh.write("keep")
        os.makedirs(os.path.dirname(self.dest))
        os.symlink(target, self.dest)
        core_files.write_atomic(self.dest, "new", mode=0o644)
        self.assertFalse(os.path.islink(self.dest))
        with open(target) as fh:
            self.assertEqual(fh.read(), "keep")


class ForceSymlinkAndRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-fsl-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_force_symlink_replaces_by_rename(self) -> None:
        link = os.path.join(self.tmp, "zram.conf")
        with open(link, "w") as fh:
            fh.write("old\n")
        fs = core_files.LocalFileSystemAdapter()
        # Unlink then symlink left a moment with nothing at the path.
        with mock.patch.object(core_files.os, "unlink", side_effect=AssertionError("unlink called")):
            fs.force_symlink("/dev/null", link)
            fs.force_symlink("/dev/zero", link)
        self.assertEqual(os.readlink(link), "/dev/zero")
        self.assertEqual(os.listdir(self.tmp), ["zram.conf"], "a temporary link was left")

    def test_runner_decodes_bytes_that_are_not_utf8(self) -> None:
        # Printf '\\377abc' raised UnicodeDecodeError out of run().
        res = core_runner.SubprocessCommandRunner().run(["printf", "\\377abc"])
        self.assertEqual((res.returncode, res.stdout), (0, chr(0xFFFD) + "abc"))


BASH = "/usr/bin/bash"
NBSP = chr(0xA0)
PROBLEM = None
# (line of a file bash also sources, what parse_shell_assignments must give for K: a value or PROBLEM).
STRICT_TABLE: tuple[tuple[str, str | None], ...] = (
    ("K=1", "1"),
    ("K='a b'", "a b"),
    ('K="a b"', "a b"),
    ("K='12-23' # E-cores", "12-23"),
    ('K="x" # note', "x"),
    ("export K=v", "v"),
    ("  K=v", "v"),
    ("\tK=v  # c", "v"),
    ("K=", ""),
    ("K=''", ""),
    ('K=""', ""),
    ("K= # c", ""),
    ("K=a#b", "a#b"),
    ("K=a #b", "a"),
    ("K=#x", "#x"),
    ("K=1 # on", "1"),
    ("K=user@host:22", "user@host:22"),
    ("K=/volume1/backups/sophia", "/volume1/backups/sophia"),
    ("K={a,b}", "{a,b}"),
    ("K=a{1..3}", "a{1..3}"),  # an assignment word is not brace-expanded...
    ("export K={a,b}", PROBLEM),  # ...but an argument of export is: bash sets K=b
    ("export K=a{1..3}", PROBLEM),
    ("export K=*", "*"),
    ("K=*", "*"),
    ("K='$HOME'", "$HOME"),
    ("K=a=b", "a=b"),
    ("K=a\x0cb", "a\x0cb"),  # bash splits words at blanks and tabs only
    ("K=a\x0bb", "a\x0bb"),
    (f"K=a{NBSP}b", f"a{NBSP}b"),
    ("K = 1", PROBLEM),
    ("K =1", PROBLEM),
    ("K= 1", PROBLEM),
    ("K=a b", PROBLEM),
    ("K=$HOME", PROBLEM),
    ('K="$HOME"', PROBLEM),
    ("K=`true`", PROBLEM),
    ("K=a\\b", PROBLEM),
    ('K="a\\b"', PROBLEM),
    ("K=~/x", PROBLEM),
    ("K=a:~/b", PROBLEM),
    ("K='a", PROBLEM),
    ('K="a', PROBLEM),
    ("K='a'b", PROBLEM),
    ("K=a'b'", PROBLEM),
    ("K=x;y", PROBLEM),
    ("K=(a)", PROBLEM),
    ("K=a|b", PROBLEM),
    ("K+=1", PROBLEM),
    ("K[0]=1", PROBLEM),
    ("readonly K=1", PROBLEM),
    ("just words", PROBLEM),
    ("K=x\r", PROBLEM),
)


def _container(*, inside: bool) -> core_runner.HostExecutionContext:
    """A HostExecutionContext that sees a container, or not."""
    return core_runner.HostExecutionContext(exists=lambda _path: inside)


class _Recorder:
    """A stand-in for subprocess.run: records argv and kwargs, answers rc 0."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        return subprocess.CompletedProcess(argv, 0, "out", "err")


class CommandRunnerTest(unittest.TestCase):
    """SubprocessCommandRunner's options (CommandRunnerPort), on real commands where they can run here."""

    py = sys.executable

    def recorded(
        self,
        runner: core_runner.SubprocessCommandRunner,
        call: Callable[[core_runner.SubprocessCommandRunner], object],
    ) -> tuple[list[str], dict[str, object]]:
        """(argv, kwargs) that subprocess.run got from call(runner)."""
        rec = _Recorder()
        with mock.patch.object(subprocess, "run", rec):
            call(runner)
        self.assertEqual(len(rec.calls), 1)
        return rec.calls[0]

    def test_the_defaults_reach_subprocess_run_as_before(self) -> None:
        # Only the arguments an option sets: a caller's test double that takes
        # a fixed set of arguments keeps working when the caller moves here.
        argv, kw = self.recorded(
            core_runner.SubprocessCommandRunner(_container(inside=False)), lambda r: r.run(["cmd", "arg"])
        )
        self.assertEqual(argv, ["cmd", "arg"])
        want: dict[str, object] = {
            "check": False,
            "capture_output": True,
            "text": True,
            "errors": "replace",
            "stdin": subprocess.DEVNULL,
        }
        self.assertEqual(kw, want)

    def test_strict_decoding_raises_as_text_true_did(self) -> None:
        runner = core_runner.SubprocessCommandRunner()
        with self.assertRaises(UnicodeDecodeError):
            runner.run(["printf", "\\377abc"], decode_errors="strict")
        _, kw = self.recorded(runner, lambda r: r.run(["cmd", "arg"], decode_errors="strict"))
        self.assertNotIn("errors", kw)

    def test_faults_are_marked_with_their_cause(self) -> None:
        tmp = tempfile.mkdtemp(prefix="cst-runner-")
        self.addCleanup(shutil.rmtree, tmp)
        runner = core_runner.SubprocessCommandRunner()
        missing = runner.run([os.path.join(tmp, "nope")])
        self.assertEqual((missing.returncode, missing.fault), (127, "missing"))
        self.assertIsInstance(missing.cause, FileNotFoundError)
        plain = os.path.join(tmp, "plain")
        with open(plain, "w") as fh:
            fh.write("not a program\n")
        unstartable = runner.run([plain])
        self.assertEqual((unstartable.returncode, unstartable.fault), (126, "unstartable"))
        self.assertIsInstance(unstartable.cause, PermissionError)
        slow = runner.run([self.py, "-c", "import time; time.sleep(30)"], timeout=0.3)
        self.assertEqual((slow.returncode, slow.fault, slow.stderr), (124, "timeout", "timed out after 0.3s"))
        self.assertIsInstance(slow.cause, subprocess.TimeoutExpired)
        with self.assertRaises(core_ports.CommandFailed) as stop:
            runner.run([self.py, "-c", "import time; time.sleep(30)"], timeout=0.3, check=True)
        self.assertEqual(stop.exception.returncode, 124)
        # A command that exits 127 by itself ran: no fault.
        own = runner.run([self.py, "-c", "raise SystemExit(127)"])
        self.assertEqual((own.returncode, own.fault, own.cause), (127, None, None))

    def test_environment_options(self) -> None:
        runner = core_runner.SubprocessCommandRunner()
        show = [
            self.py,
            "-c",
            "import os; print(os.environ.get('LC_ALL'), os.environ.get('CST_A'), os.environ.get('CST_B'))",
        ]
        with mock.patch.dict(os.environ, {"LC_ALL": "pt_BR.UTF-8", "CST_A": "a", "CST_B": "b"}):
            self.assertEqual(runner.run(show).stdout, "pt_BR.UTF-8 a b\n")
            self.assertEqual(runner.run(show, locale_c=True).stdout, "C a b\n")
            self.assertEqual(runner.run(show, locale_c=True, env={"LC_ALL": "en_US.UTF-8"}).stdout, "en_US.UTF-8 a b\n")
            self.assertEqual(runner.run(show, env={"CST_A": "x"}, unset=["CST_B"]).stdout, "pt_BR.UTF-8 x None\n")
            base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "CST_B": "base"}
            self.assertEqual(runner.run(show, base_env=base, locale_c=True).stdout, "C None base\n")
        _, kw = self.recorded(runner, lambda r: r.run(["cmd", "arg"]))
        self.assertNotIn("env", kw, "no option: the command inherits our environment")

    def test_a_missing_cwd_is_a_command_that_could_not_start(self) -> None:
        # subprocess raises FileNotFoundError naming the folder: the program was found.
        tmp = tempfile.mkdtemp(prefix="cst-runner-")
        self.addCleanup(shutil.rmtree, tmp)
        gone = os.path.join(tmp, "gone")
        r = core_runner.SubprocessCommandRunner().run([self.py, "-c", "pass"], cwd=gone)
        self.assertEqual((r.returncode, r.fault), (126, "unstartable"))
        self.assertIsInstance(r.cause, FileNotFoundError)
        self.assertIn(gone, r.stderr)

    def test_cwd_new_session_and_nice(self) -> None:
        runner = core_runner.SubprocessCommandRunner()
        tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-runner-"))
        self.addCleanup(shutil.rmtree, tmp)
        where = [self.py, "-c", "import os; print(os.getcwd())"]
        self.assertEqual(runner.run(where, cwd=tmp).stdout, tmp + "\n")
        sid = [self.py, "-c", "import os; print(os.getsid(0))"]
        self.assertEqual(runner.run(sid).stdout, f"{os.getsid(0)}\n")
        self.assertNotEqual(runner.run(sid, new_session=True).stdout, f"{os.getsid(0)}\n")
        niceness = [self.py, "-c", "import os; print(os.nice(0))"]
        ours = os.nice(0)
        self.assertEqual(runner.run(niceness, nice=3).stdout, f"{min(ours + 3, 19)}\n")
        self.assertEqual(runner.run(niceness).stdout, f"{ours}\n")

    def test_stdin_and_output_modes(self) -> None:
        runner = core_runner.SubprocessCommandRunner()
        both = [self.py, "-c", "import sys; print('o'); print('e', file=sys.stderr)"]
        self.assertEqual((runner.run(both).stdout, runner.run(both).stderr), ("o\n", "e\n"))
        merged = runner.run(both, output="merge")
        self.assertEqual((sorted(merged.stdout.split()), merged.stderr), (["e", "o"], ""))
        only = runner.run(both, output="stdout")
        self.assertEqual((only.stdout, only.stderr), ("o\n", ""))
        _, kw = self.recorded(runner, lambda r: r.run(["cmd", "arg"], output="inherit", stdin="inherit"))
        self.assertEqual(kw, {"check": False}, "both left on ours: nothing captured, no stdin given")
        _, kw = self.recorded(runner, lambda r: r.run(["cmd", "arg"], input_text="abc"))
        self.assertEqual((kw["input"], "stdin" in kw), ("abc", False))
        with self.assertRaises(ValueError):
            runner.run(["cmd"], input_text="abc", stdin="inherit")

    def test_words_rewrite_only_faults_with_the_callers_argv(self) -> None:
        seen: list[tuple[list[str], int, float | None]] = []

        def words(argv: Sequence[str], r: core_ports.CommandResult, timeout: float | None) -> core_ports.CommandResult:
            seen.append((list(argv), r.returncode, timeout))
            return core_ports.CommandResult(
                r.returncode, "", f"{argv[0]}: {r.fault}", argv=r.argv, fault=r.fault, cause=r.cause
            )

        tmp = tempfile.mkdtemp(prefix="cst-runner-")
        self.addCleanup(shutil.rmtree, tmp)
        missing = os.path.join(tmp, "nope")
        inside = core_runner.SubprocessCommandRunner(_container(inside=True), words=words)
        rec = _Recorder()
        rec_fault: list[str] = []

        def raising(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            rec(argv, **kwargs)
            rec_fault.append(argv[0])
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        with mock.patch.object(subprocess, "run", raising):
            r = inside.run([missing, "x"], on_host=True, timeout=7)
        # words gets the argv the caller gave, not the flatpak-spawn one that ran.
        self.assertEqual(rec_fault, ["flatpak-spawn"])
        self.assertEqual(seen, [([missing, "x"], 127, 7)])
        self.assertEqual(
            (r.returncode, r.stderr, r.fault, r.argv[0]), (127, f"{missing}: missing", "missing", "flatpak-spawn")
        )
        runner = core_runner.SubprocessCommandRunner(_container(inside=False), words=words)
        with self.assertRaises(core_ports.CommandFailed) as stop:
            runner.run([missing], check=True)
        self.assertEqual((stop.exception.returncode, stop.exception.stderr), (127, f"{missing}: missing"))
        seen.clear()
        plain = runner.run([self.py, "-c", "import sys; print('e', file=sys.stderr); raise SystemExit(3)"])
        self.assertEqual((plain.returncode, plain.stderr, plain.fault, seen), (3, "e\n", None, []))

    def test_the_constructors_base_env_is_the_default_and_a_call_can_name_its_own(self) -> None:
        path = os.environ.get("PATH", "/usr/bin:/bin")
        runner = core_runner.SubprocessCommandRunner(base_env={"PATH": path, "CST_A": "runner"})
        show = [self.py, "-c", "import os; print(os.environ.get('CST_A'), os.environ.get('LC_ALL'))"]
        with mock.patch.dict(os.environ, {"CST_A": "process"}):
            self.assertEqual(runner.run(show).stdout, "runner None\n")
            self.assertEqual(runner.run(show, locale_c=True).stdout, "runner C\n")
            self.assertEqual(runner.run(show, base_env={"PATH": path, "CST_A": "call"}).stdout, "call None\n")
            self.assertEqual(core_runner.SubprocessCommandRunner().run(show).stdout, "process None\n")

    def test_on_host_wraps_only_inside_a_container(self) -> None:
        inside = core_runner.SubprocessCommandRunner(_container(inside=True))
        outside = core_runner.SubprocessCommandRunner(_container(inside=False))
        argv, kw = self.recorded(inside, lambda r: r.run(["cmd", "arg"], on_host=True, locale_c=True, env={"K": "v"}))
        # The host command does not get flatpak-spawn's environment: the settings go in --env.
        self.assertEqual(argv, ["flatpak-spawn", "--host", "--env=LC_ALL=C", "--env=K=v", "cmd", "arg"])
        env = kw["env"]
        assert isinstance(env, dict)
        self.assertEqual((env["LC_ALL"], env["K"]), ("C", "v"))
        on_host: Callable[[core_runner.SubprocessCommandRunner], object] = lambda r: r.run(["cmd", "arg"], on_host=True)
        self.assertEqual(self.recorded(inside, on_host)[0], ["flatpak-spawn", "--host", "cmd", "arg"])
        self.assertEqual(self.recorded(inside, lambda r: r.run(["cmd", "arg"]))[0], ["cmd", "arg"], "on_host is opt-in")
        self.assertEqual(self.recorded(outside, on_host)[0], ["cmd", "arg"])
        with self.assertRaises(ValueError):
            inside.run(["cmd"], on_host=True, unset=["X"])


class _RecordingRunner:
    """CommandRunnerPort double for the virsh adapter: records argv and options."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def run(self, argv: Sequence[str], **options: object) -> core_ports.CommandResult:
        self.calls.append((list(argv), options))
        return core_ports.CommandResult(0, "", "")


class VirshAdapterTest(unittest.TestCase):
    """SubprocessVirshAdapter builds virsh's argv and hands its options to the one runner."""

    def test_options_reach_the_runner(self) -> None:
        runner = _RecordingRunner()
        virsh = core_virsh.SubprocessVirshAdapter("qemu:///x", runner, on_host=False)
        virsh.call("domstate", "vm1", timeout=15, start_new_session=True)
        argv, opts = runner.calls[-1]
        self.assertEqual(argv, ["virsh", "-c", "qemu:///x", "domstate", "vm1"])
        want: dict[str, object] = {
            "check": False,
            "timeout": 15,
            "locale_c": True,
            "new_session": True,
            "on_host": False,
        }
        self.assertEqual(opts, want)
        ro = core_virsh.SubprocessVirshAdapter(runner=runner, read_only=True)
        ro.call("list")
        self.assertEqual(runner.calls[-1][0], ["virsh", "-r", "-c", "qemu:///system", "list"])
        self.assertEqual((runner.calls[-1][1]["on_host"], runner.calls[-1][1]["timeout"]), (True, 60))

    def test_domains_and_dumpxml(self) -> None:
        class Answers(_RecordingRunner):
            def run(self, argv: Sequence[str], **options: object) -> core_ports.CommandResult:
                super().run(argv, **options)
                if "list" in argv:
                    return core_ports.CommandResult(0, "a\n\n b \n", "")
                return core_ports.CommandResult(1, "", "no domain")

        virsh = core_virsh.SubprocessVirshAdapter(runner=Answers(), on_host=False)
        self.assertEqual(virsh.domains(), ["a", "b"])
        self.assertIsNone(virsh.dumpxml("x", inactive=True))

    def test_a_ctx_with_a_runner_is_refused(self) -> None:
        # The runner decides on_host; a ctx next to it would be ignored.
        with self.assertRaises(ValueError):
            core_virsh.SubprocessVirshAdapter(runner=_RecordingRunner(), ctx=_container(inside=True))

    def test_in_a_container_virsh_runs_on_the_host_in_the_c_locale(self) -> None:
        rec = _Recorder()
        virsh = core_virsh.SubprocessVirshAdapter(ctx=_container(inside=True))
        with mock.patch.object(subprocess, "run", rec):
            virsh.call("domstate", "vm1")
        self.assertEqual(
            rec.calls[0][0],
            ["flatpak-spawn", "--host", "--env=LC_ALL=C", "virsh", "-c", "qemu:///system", "domstate", "vm1"],
        )


class PrintfQTest(unittest.TestCase):
    """printf_q and printf_q_words, the two sides of the state files a bash program writes with
    printf %q and reads back with eval (the game profile's game.state), against bash itself."""

    CORPUS: list[str] = (
        [chr(i) for i in range(1, 128)]
        + [f"a{chr(i)}b" for i in range(1, 128)]
        + ["", "a b", "~a", "a~", "a=~b", "a:~", "#a", "a#", "-", "new", "it's", "lush\\x2dbatch.slice"]
        + ["é", "aé b", "a\x01é", "\x85x", "a\tb\nc"]
        # Printable for glibc, not for Python: a space (NBSP, em space), format characters (soft
        # hyphen, zero-width space), a private-use character.
        + ["a\u00a0b", "a\u2003b", "a\u00adb", "a\u200bb", "\ue000", "a\u00a0\tb"]
    )

    def bash_q(self, values: Sequence[str]) -> list[str]:
        r = subprocess.run(
            [BASH, "--norc", "--noprofile", "-c", 'for a in "$@"; do printf "%q\\0" "$a"; done', "_", *values],
            check=True,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
            stdin=subprocess.DEVNULL,
        )
        return r.stdout.decode("utf-8", "surrogateescape").split("\0")[:-1]

    @unittest.skipUnless(os.access(BASH, os.X_OK), "needs /usr/bin/bash")
    def test_printf_q_writes_what_bash_writes(self) -> None:
        for value, want in zip(self.CORPUS, self.bash_q(self.CORPUS), strict=True):
            with self.subTest(value=value):
                self.assertEqual(printf_q(value), want)

    def test_words_read_back_what_printf_q_wrote(self) -> None:
        for value in self.CORPUS:
            with self.subTest(value=value):
                self.assertEqual(printf_q_words(f"x {printf_q(value)}  y"), ["x", value, "y"])

    def test_words_of_other_quoting_and_lines_bash_would_not_read_as_words(self) -> None:
        self.assertEqual(printf_q_words("cgw /a ''"), ["cgw", "/a", ""])
        self.assertEqual(printf_q_words("a'b c'\"d\\\"\" $'\\101\\x42'"), ['ab cd"', "AB"])
        self.assertEqual(printf_q_words(""), [])
        for line in ("a 'b", "a $x", "a `b`", "a;b", "a\\", "a (b)", 'a "$x"'):
            with self.subTest(line=line):
                self.assertIsNone(printf_q_words(line))


class ShellAssignmentsTest(unittest.TestCase):
    """parse_shell_assignments, the one KEY=VALUE reader (cpu, vm, doctor, toolchains, bluetooth, bulk), against bash."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-kv-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.empty_path = os.path.join(self.tmp, "no-commands")
        os.mkdir(self.empty_path)

    def bash_values(self, text: str, keys: list[str]) -> dict[str, str]:
        """What bash sets when it sources text: PATH is an empty folder, so a line that is a
        command runs nothing (the table holds no harmful builtin)."""
        path = os.path.join(self.tmp, "settings.conf")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        script = (
            '. "$1" 2>/dev/null; shift; for k in "$@"; do if [[ -v $k ]]; then printf "%s=%s\\0" "$k" "${!k}"; fi; done'
        )
        r = subprocess.run(
            [BASH, "--norc", "--noprofile", "-c", script, "_", path, *keys],
            check=False,
            capture_output=True,
            env={"PATH": self.empty_path, "HOME": self.tmp},
            cwd=self.tmp,
            stdin=subprocess.DEVNULL,
        )
        out: dict[str, str] = {}
        for item in r.stdout.split(b"\0")[:-1]:
            k, _, v = item.partition(b"=")
            out[k.decode()] = v.decode("utf-8", "surrogateescape")
        return out

    def test_table(self) -> None:
        for line, want in STRICT_TABLE:
            with self.subTest(line=line):
                got = parse_shell_assignments(line + "\n")
                if want is PROBLEM:
                    self.assertNotIn("K", got.values)
                    self.assertEqual([p.line for p in got.problems], [1], got)
                else:
                    self.assertEqual((dict(got.values), got.problems), ({"K": want}, ()))

    @unittest.skipUnless(os.access(BASH, os.X_OK), "needs /usr/bin/bash")
    def test_every_value_is_what_bash_sets(self) -> None:
        for line, _ in STRICT_TABLE:
            with self.subTest(line=line):
                values = parse_shell_assignments(line + "\n").values
                if "K" in values:
                    self.assertEqual(self.bash_values(line + "\n", ["K"]), {"K": values["K"]})

    def test_problems_name_the_line_and_set_nothing(self) -> None:
        got = parse_shell_assignments("A='1'\n# c\nB = 2\nA=$X\n")
        self.assertEqual(dict(got.values), {"A": "1"}, "a problem line must not change an earlier value")
        self.assertEqual(
            [str(p) for p in got.problems],
            [
                "line 3: B = 2: blanks around =: bash runs it as a command and sets nothing",
                "line 4: A=$X: $, ` or \\ outside single quotes: bash would expand it (use single quotes)",
            ],
        )
        self.assertEqual(
            describe_problems("/etc/x.conf", got.problems).splitlines()[0],
            "/etc/x.conf line 3: B = 2: blanks around =: bash runs it as a command and sets nothing",
        )
        self.assertEqual([(a.line, a.key) for a in got.entries], [(1, "A")])
        # An escaped blank (printf %q) is named for its backslash, not as a command after a blank.
        (escaped,) = parse_shell_assignments("NAS_PATH=/volume1/My\\ Backups\n").problems
        self.assertIn("outside single quotes: bash would expand it", escaped.reason)

    def test_the_last_assignment_wins_and_only_a_newline_ends_a_line(self) -> None:
        self.assertEqual(dict(parse_shell_assignments("K=1\nK=2\n").values), {"K": "2"})
        self.assertEqual(dict(parse_shell_assignments("K=1\nK = 0\nK=1\n", bash_sourced=False).values), {"K": "1"})
        for sep in ("\x0c", "\x0b", "\x1c", "\x1d", "\x1e", chr(0x85), chr(0x2028), chr(0x2029)):
            with self.subTest(sep=repr(sep)):
                self.assertEqual(dict(parse_shell_assignments(f"A=1{sep}B=2\n").values), {"A": f"1{sep}B=2"})

    def test_python_only_files_keep_what_their_readers_accepted(self) -> None:
        # vm.conf, toolchains.conf, bluetooth.conf: blanks around = (VM_NOCOW = 0 has always worked in vm.conf),
        # several words, a Windows line end; still no $, backtick or backslash.
        text = "VM_NOCOW = 0\nA = 'a b' # c\nB = x y  # z\n  C=1h\nD=v\r\nE=$X\n"
        got = parse_shell_assignments(text, bash_sourced=False)
        self.assertEqual(dict(got.values), {"VM_NOCOW": "0", "A": "a b", "B": "x y", "C": "1h", "D": "v"})
        self.assertEqual([p.line for p in got.problems], [6])

    # Lines an older CST's snap_conf_set wrote with printf %q, and what bash makes of them.
    OLD_WRITER_LINES = (
        "K=/volume1/My\\ Backups",
        "K=it\\'s",
        "K=\\~x",
        "K=a:\\~b",
        'K=say\\ \\"hi\\"',
        "K=\\$HOME\\ \\`id\\`",
        "K=back\\\\slash",
        "K=\\#x\\ \\*\\?\\[a\\]\\{a\\,b\\}\\;\\&\\|\\<\\>\\(\\)",
        "K=é\\ ü",
        "K=$'a\\tb'",
        "K=$'line1\\nline2'",
        "K=$'\\303\\251 \\303\\274'",
        "K=$'\\E[1m\\a\\b\\f\\r\\v\\\\\\'\\\"\\?'",
        "K=$'\\x41\\x4a'",
        "K=$'\\377raw'",
        "K=$'a\\0b'",
        "K=$'\\777'",
    )
    # Not what printf %q writes: still a problem in snapshots.conf too (never a guessed value).
    NOT_OLD_WRITER_LINES = (
        "K=$X",
        "K=a\\ $X",
        "K=a\\ `id`",
        "K=~\\ x",
        "K=a:~b\\ c",
        'K="a\\ b"',
        "K=$'" + "\\" + "u00e9'",
        "K=$'\\cA'",
        "K=$'a'b",
        "K=a\\ b # c",
        " K=a\\ b",
        "export K=a\\ b",
        "K=a\\ b;id",
        "K=a\\",
        "K = a\\ b",
    )

    @unittest.skipUnless(os.access(BASH, os.X_OK), "needs /usr/bin/bash")
    def test_snapshots_conf_reads_old_printf_q_lines_as_bash_does(self) -> None:
        for line in self.OLD_WRITER_LINES:
            with self.subTest(line=line):
                got = parse_snapshots_conf(line + "\n")
                self.assertEqual(got.problems, ())
                want = self.bash_values(line + "\n", ["K"])
                self.assertEqual(
                    dict(got.values), {"K": want["K"].encode("utf-8", "surrogateescape").decode("utf-8", "replace")}
                )
                # The generic reader stays strict: in every other file the line is a problem.
                self.assertEqual([p.line for p in parse_shell_assignments(line + "\n").problems], [1])

    def test_snapshots_conf_keeps_other_backslash_lines_a_problem(self) -> None:
        for line in self.NOT_OLD_WRITER_LINES:
            with self.subTest(line=line):
                got = parse_snapshots_conf(line + "\n")
                self.assertEqual((dict(got.values), [p.line for p in got.problems]), ({}, [1]))
                self.assertEqual(got.problems, parse_shell_assignments(line + "\n").problems)

    def test_snapshots_conf_last_line_wins_in_file_order(self) -> None:
        got = parse_snapshots_conf("A=1\nK='x'\nK=a\\ b\nB=$X\nC=c\\ d\n")
        self.assertEqual(dict(got.values), {"A": "1", "K": "a b", "C": "c d"})
        self.assertEqual([(a.line, a.key) for a in got.entries], [(1, "A"), (2, "K"), (3, "K"), (5, "C")])
        self.assertEqual([p.line for p in got.problems], [4])
        self.assertEqual(dict(parse_snapshots_conf("K=a\\ b\nK='c'\n").values), {"K": "c"})

    @unittest.skipUnless(os.access(BASH, os.X_OK), "needs /usr/bin/bash")
    def test_the_files_cst_writes_and_ships_have_no_problem(self) -> None:
        # The upgrade path: every file CST generates or ships reads without a problem, as bash reads it.
        with open(os.path.join(REPO, "btrfs", "setup-snapshots.bash"), encoding="utf-8") as fh:
            snap = re.search(r"<<'DEFAULTS'\n(.*?)\nDEFAULTS\n", fh.read(), re.S)
        assert snap is not None
        generated = cpu_setup_conf_lines("alice")  # what cst cpu install writes
        # snapshots.conf as the real writer (snap_conf_set) leaves it, blanks and a ' included.
        conf = os.path.join(self.tmp, "snapshots.conf")
        quoted = self.bash_text(
            f"TMPDIR={shlex.quote(self.tmp)}; SRC={shlex.quote(os.path.join(REPO, 'btrfs'))}; "
            f'. "$SRC/setup-snapshots.bash"; SNAP_CONF={shlex.quote(conf)}; '
            "snap_conf_set NAS_HOST=nas.local NAS_USER=danilo NAS_PATH=/volume1/backup/sophia NAS_PORT=2204 "
            'NAS_RESTIC_HOST="it\'s" HOME_DIR=\'/var/home/a b\' && cat "$SNAP_CONF"'
        )
        self.assertIn("HOME_DIR='/var/home/a b'", quoted.splitlines())
        for name, text in (
            ("cpu-setup.conf", generated),
            ("snapshots.conf", quoted),
            ("bulk.conf", "# Managed by btrfs-setup.sh (CST)\nBULK_USER=alice\n"),
        ):
            with self.subTest(name=name):
                got = parse_shell_assignments(text)
                self.assertEqual(got.problems, ())
                self.assertEqual(dict(got.values), self.bash_values(text, sorted(got.values)))
        self.assertEqual(parse_shell_assignments(generated).values["VM_CPU_DISABLE"].split()[0], "cet-ss")
        # btrfs-bulk reads bulk.conf as a Python-only file (bulk_user): the same value there.
        bulk = parse_shell_assignments("# Managed by btrfs-setup.sh (CST)\nBULK_USER=alice\n", bash_sourced=False)
        self.assertEqual((dict(bulk.values), bulk.problems), ({"BULK_USER": "alice"}, ()))
        for rel in ("toolchains/toolchains.conf", "bluetooth/bluetooth.conf"):
            with self.subTest(rel=rel), open(os.path.join(REPO, rel), encoding="utf-8") as fh:
                self.assertEqual(parse_shell_assignments(fh.read(), bash_sourced=False).problems, ())

    def bash_text(self, script: str) -> str:
        r = subprocess.run(
            [BASH, "--norc", "--noprofile", "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
            stdin=subprocess.DEVNULL,
        )
        return r.stdout

    def test_old_ansi_c_line_without_closing_quote_fails_fast(self) -> None:
        # Sixteen \NNNN escapes and no closing quote: with a backtracking regex each
        # escape could be cut three ways (\1|111, \11|11, \111|1), 3**16 tries
        # (about 4 s here, so a regression fails the 1 s limit in seconds instead
        # of hanging the run); the possessive form fails at once and reports the line.
        line = "NAS_PATH=$'" + "\\1111" * 16 + "\n"
        started = time.perf_counter()
        parsed = parse_snapshots_conf(line)
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(parsed.entries, ())
        self.assertEqual([p.line for p in parsed.problems], [1])
        # The same escapes with the closing quote: bash takes up to three octal digits.
        self.assertEqual(parse_snapshots_conf("K=$'\\1111'\n").entries[0].value, "\x491")


class ConfirmTest(unittest.TestCase):
    """confirm: a yes/no question on the terminal (vm apply/nocow, cpu vm-pin apply)."""

    def ask(self, answer: str, *, tty: bool = True) -> tuple[bool, str]:
        out = io.StringIO()
        with (
            mock.patch("sys.stdin") as stdin,
            mock.patch("builtins.input", side_effect=lambda prompt: (out.write(prompt), answer)[1]),
        ):
            stdin.isatty.return_value = tty
            return confirm("\nDefine it?"), out.getvalue()

    def test_a_yes_is_true_and_the_question_ends_in_the_choices(self) -> None:
        for answer in ("y", "Y", "yes", " yes ", "sim"):
            with self.subTest(answer=answer):
                self.assertEqual(self.ask(answer), (True, "\nDefine it? [y/N] "))

    def test_anything_else_is_no(self) -> None:
        for answer in ("", "n", "no", "x", " nao"):
            with self.subTest(answer=answer):
                self.assertFalse(self.ask(answer)[0])

    def test_no_terminal_refuses_without_asking(self) -> None:
        with self.assertRaisesRegex(Refused, "^apply asks for confirmation: run it in a terminal$"):
            self.ask("y", tty=False)


class _NotifyRunner:
    """CommandRunnerPort for Notifier: one answer for every command, and the calls with their options."""

    def __init__(self, result: core_ports.CommandResult) -> None:
        self.result = result
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def run(self, argv: Sequence[str], **kw: object) -> core_ports.CommandResult:
        self.calls.append((list(argv), kw))
        return self.result


class NotifierTest(unittest.TestCase):
    """cst_core.adapters.notify: btrfs-notify's command line, and an alert it could not take kept for cst doctor."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-notify-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pending = os.path.join(self.tmp, "alerts", "pending")
        self.lines: list[tuple[int, str]] = []

    def send(
        self,
        result: core_ports.CommandResult,
        title: str = "Disk: problem",
        *,
        app: str = "btrfs-bulk",
        body: str = "the body\nline 2",
    ) -> tuple[str, _NotifyRunner]:
        runner = _NotifyRunner(result)
        notifier = Notifier(runner, "/x/btrfs-notify", self.pending, log=lambda p, m: self.lines.append((p, m)))
        return notifier.send("critical", app, title, body), runner

    def missing(self, title: str = "Disk: problem", *, app: str = "btrfs-bulk", body: str = "the body") -> str:
        """Send with btrfs-notify missing; the file the alert was kept in."""
        cause = FileNotFoundError(2, "No such file or directory", "/x/btrfs-notify")
        outcome, _ = self.send(
            core_ports.CommandResult(127, "", "", fault="missing", cause=cause), title, app=app, body=body
        )
        self.assertEqual(outcome, "kept")
        return self.lines[-1][1].split("; kept in ", 1)[1].removesuffix(" (cst doctor shows it)")

    def kept(self) -> list[str]:
        return sorted(os.listdir(self.pending)) if os.path.isdir(self.pending) else []

    def test_a_delivered_alert_is_one_btrfs_notify_call_and_nothing_else(self) -> None:
        outcome, runner = self.send(core_ports.CommandResult(0, "", ""))
        self.assertEqual(outcome, "sent")
        self.assertEqual(
            runner.calls,
            [
                (
                    ["/x/btrfs-notify", "-u", "critical", "-a", "btrfs-bulk", "Disk: problem", "the body\nline 2"],
                    {"timeout": 60, "new_session": True},
                )
            ],
        )
        self.assertEqual((self.lines, self.kept()), ([], []))

    def test_without_btrfs_notify_the_alert_is_kept_and_the_journal_says_so(self) -> None:
        missing = FileNotFoundError(2, "No such file or directory", "/x/btrfs-notify")
        outcome, _ = self.send(core_ports.CommandResult(127, "", "", fault="missing", cause=missing))
        self.assertEqual(outcome, "kept")
        [name] = self.kept()
        path = os.path.join(self.pending, name)
        self.assertEqual(name, hashlib.sha256(b"btrfs-bulk\nDisk: problem").hexdigest()[:32] + ".alert")
        self.assertEqual(
            self.lines,
            [
                (
                    3,
                    f"alert not delivered (/x/btrfs-notify is not installed): Disk: problem; kept in {path} (cst doctor shows it)",
                )
            ],
        )
        with open(path) as f:
            head, body = f.read().split("\n\n", 1)
        lines = head.splitlines()
        self.assertEqual(lines[:3], ["cst-alert 1", "urgency=critical", "app=btrfs-bulk"])
        self.assertEqual((lines[5], lines[6], body), ("count=1", "title=Disk: problem", "the body\nline 2\n"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(self.pending).st_mode), 0o755)

    def test_a_failure_or_no_answer_keeps_the_alert_too(self) -> None:
        for result, why in (
            (core_ports.CommandResult(1, "", ""), "/x/btrfs-notify exited 1"),
            (core_ports.CommandResult(124, "", "", fault="timeout"), "/x/btrfs-notify did not answer within 60 s"),
        ):
            with self.subTest(why):
                self.lines.clear()
                outcome, _ = self.send(result)
                self.assertEqual(outcome, "kept")
                self.assertIn(f"alert not delivered ({why})", self.lines[0][1])

    def test_a_repeat_is_merged_into_the_kept_alert(self) -> None:
        path = self.missing(body="first")
        with open(path, "r+") as f:
            text = f.read().replace("\nfirst=", "\nfirst=1000\nignored=", 1)
            f.seek(0)
            f.write(text)
        self.assertEqual(self.missing(body="second"), path)
        with open(path) as f:
            text = f.read()
        self.assertIn("\nfirst=1000\n", text)
        self.assertIn("\ncount=2\n", text)
        self.assertTrue(text.endswith("\n\nsecond\n"))
        self.assertEqual(len(self.kept()), 1)

    def test_app_title_and_body_are_bounded_as_btrfs_notify_bounds_them(self) -> None:
        path = self.missing("a\tb\nc" + "t" * 300, app="my app/" + "x" * 60, body="b" * 5000)
        with open(path) as f:
            head, body = f.read().split("\n\n", 1)
        app, title = ("my_app_" + "x" * 60)[:40], ("a b c" + "t" * 300)[:200]
        self.assertIn(f"\napp={app}\n", head)
        self.assertTrue(head.endswith(f"\ntitle={title}"), head)
        self.assertEqual(body, "b" * 4000 + "\n")
        self.assertEqual(os.path.basename(path), hashlib.sha256(f"{app}\n{title}".encode()).hexdigest()[:32] + ".alert")
        self.assertTrue(
            self.missing("", app="").endswith(hashlib.sha256(b"btrfs\nbtrfs alert").hexdigest()[:32] + ".alert")
        )

    def test_when_the_alert_cannot_be_kept_either_only_the_journal_has_it(self) -> None:
        with open(os.path.join(self.tmp, "alerts"), "w"):
            pass  # a file where the folder should be
        missing = FileNotFoundError(2, "No such file or directory", "/x/btrfs-notify")
        outcome, _ = self.send(core_ports.CommandResult(127, "", "", fault="missing", cause=missing))
        self.assertEqual(outcome, "lost")
        [(prio, msg)] = self.lines
        self.assertEqual(prio, 3)
        self.assertTrue(msg.startswith("alert not delivered (/x/btrfs-notify is not installed) and not kept ("), msg)
        self.assertTrue(msg.endswith("): Disk: problem; it is only in this journal"), msg)

    def test_a_folder_or_title_no_file_takes_leaves_the_alert_in_the_journal_only(self) -> None:
        # A caller's error handler sends this alert: a ValueError (a folder from an override that is no
        # plain path, a title with a lone surrogate) must end as "lost" with its line, not as a traceback.
        missing = FileNotFoundError(2, "No such file or directory", "/x/btrfs-notify")
        result = core_ports.CommandResult(127, "", "", fault="missing", cause=missing)
        for pending, title in (
            (os.path.join(self.tmp, "x", "..", "alerts", "pending"), "Disk"),
            (self.pending, "VM \udcff"),
        ):
            with self.subTest(pending=pending, title=title):
                self.lines.clear()
                notifier = Notifier(
                    _NotifyRunner(result), "/x/btrfs-notify", pending, log=lambda p, m: self.lines.append((p, m))
                )
                self.assertEqual(notifier.send("normal", "cst-vm", title, "body"), "lost")
                [(prio, msg)] = self.lines
                self.assertEqual(prio, 3)
                self.assertTrue(msg.endswith(f"): {title}; it is only in this journal"), msg)
                self.assertEqual(self.kept(), [])

    def test_a_spool_lock_held_too_long_leaves_the_alert_in_the_journal_only(self) -> None:
        # btrfs-notify gives up on this lock after 30 s; at boot nothing may wait for it forever.
        os.makedirs(self.pending)
        held = take_lock(os.path.join(os.path.dirname(self.pending), ".lock"), wait=False)
        assert held is not None
        self.addCleanup(os.close, held)
        missing = FileNotFoundError(2, "No such file or directory", "/x/btrfs-notify")
        with mock.patch.object(core_notify, "_LOCK_WAIT", 0.3):
            start = time.monotonic()
            outcome, _ = self.send(core_ports.CommandResult(127, "", "", fault="missing", cause=missing))
        self.assertGreaterEqual(time.monotonic() - start, 0.3)
        self.assertEqual((outcome, self.kept()), ("lost", []))
        self.assertIn(
            "/alerts/.lock is still held after 0.3 s): Disk: problem; it is only in this journal", self.lines[0][1]
        )

    def test_btrfs_notify_and_cst_pending_alerts_read_what_it_keeps(self) -> None:
        """The same alert kept by btrfs-notify (no desktop session) and then by cst_core is one file, merged;
        cst-pending-alerts --list and cst doctor read it."""
        alerts = os.path.dirname(self.pending)
        run_user = os.path.join(self.tmp, "run-user")
        os.mkdir(run_user)
        env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8", "CST_ALERTS_DIR": alerts, "CST_RUN_USER": run_user}
        notify = os.path.join(REPO, "btrfs", "btrfs-notify")
        done = subprocess.run(
            ["bash", notify, "-u", "critical", "-a", "btrfs-bulk", "Disk: problem", "from bash"],
            env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
        )  # fmt: skip
        self.assertEqual(done.returncode, 0, done.stderr)
        [name] = self.kept()
        path = self.missing(body="from python")
        self.assertEqual(self.kept(), [name])
        self.assertEqual(os.path.basename(path), name)
        with open(path) as f:
            text = f.read()
        self.assertIn("\ncount=2\n", text)
        parsed = parse_alert_text(text)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed[:2], ("critical", "Disk: problem"))
        home = os.path.join(self.tmp, "home")
        os.mkdir(home)
        listed = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", os.path.join(REPO, "btrfs", "cst-pending-alerts"), "--list"],
            env={**env, "HOME": home, "CST_ALERTS_OWNER": str(os.getuid())},
            capture_output=True, text=True, stdin=subprocess.DEVNULL, check=False,
        )  # fmt: skip
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("critical btrfs-bulk: Disk: problem (2 times)", listed.stdout)


class _FailingSocket:
    """A socket whose sends fail, and which records that it was closed."""

    def __init__(self, err: OSError) -> None:
        self.err = err
        self.closed = False

    def sendto(self, _data: bytes, _address: str) -> int:
        raise self.err

    def close(self) -> None:
        self.closed = True


class ServiceNotifierTest(unittest.TestCase):
    """cst_core.adapters.sd_notify: sd_notify(3) datagrams to systemd's NOTIFY_SOCKET, and nothing without it."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-sd-notify-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def listener(self, address: str) -> socket.socket:
        """A datagram socket bound at address, as systemd's notification socket."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.bind(address)
        sock.settimeout(5)
        return sock

    def test_ready_and_watchdog_reach_the_socket_notify_socket_names(self) -> None:
        path = os.path.join(self.tmp, "notify")
        sock = self.listener(path)
        notifier = ServiceNotifier.from_environ({"NOTIFY_SOCKET": path, "PATH": "/usr/bin"})
        self.assertTrue(notifier.ready())
        self.assertTrue(notifier.watchdog())
        self.assertTrue(notifier.watchdog())
        self.assertEqual([sock.recv(64) for _ in range(3)], [b"READY=1", b"WATCHDOG=1", b"WATCHDOG=1"])

    def test_an_abstract_socket_name_starts_with_at(self) -> None:
        name = f"cst-sd-notify-test-{os.getpid()}-{time.monotonic_ns()}"
        sock = self.listener("\0" + name)
        self.assertTrue(ServiceNotifier.from_environ({"NOTIFY_SOCKET": "@" + name}).ready())
        self.assertEqual(sock.recv(64), b"READY=1")

    def test_without_a_usable_notify_socket_nothing_is_opened_or_sent(self) -> None:
        out = io.StringIO()
        opened = mock.patch("socket.socket", side_effect=AssertionError("no socket may be opened"))
        for environ in ({}, {"NOTIFY_SOCKET": ""}, {"NOTIFY_SOCKET": "@"}, {"NOTIFY_SOCKET": "relative/notify"},
                        {"NOTIFY_SOCKET": "vsock:2:999"}):  # fmt: skip
            with self.subTest(environ=environ), contextlib.redirect_stdout(out), opened as made:
                notifier = ServiceNotifier.from_environ(environ)
                self.assertFalse(notifier.ready())
                self.assertFalse(notifier.watchdog())
                made.assert_not_called()
        self.assertEqual(out.getvalue(), "")

    def test_a_failed_send_warns_once_until_a_send_works_again(self) -> None:
        path = os.path.join(self.tmp, "notify")  # nothing bound there yet: ENOENT
        out = io.StringIO()
        notifier = ServiceNotifier(path)
        with contextlib.redirect_stdout(out):
            self.assertFalse(notifier.ready())
            self.assertFalse(notifier.watchdog())
        self.assertEqual(
            out.getvalue().splitlines(), ["<4>cannot notify systemd (READY): [Errno 2] No such file or directory"]
        )
        sock = self.listener(path)
        with contextlib.redirect_stdout(out):
            self.assertTrue(notifier.watchdog())
        self.assertEqual(sock.recv(64), b"WATCHDOG=1")
        sock.close()
        os.unlink(path)
        with contextlib.redirect_stdout(out):
            self.assertFalse(notifier.watchdog())
            self.assertFalse(notifier.watchdog())
        self.assertEqual(len(out.getvalue().splitlines()), 2)
        self.assertTrue(out.getvalue().splitlines()[1].startswith("<4>cannot notify systemd (WATCHDOG): "))

    def test_a_socket_whose_send_failed_is_closed_and_opened_again(self) -> None:
        made: list[_FailingSocket] = []

        def failing() -> _FailingSocket:
            made.append(_FailingSocket(ConnectionRefusedError(111, "Connection refused")))
            return made[-1]

        notifier = ServiceNotifier("/run/systemd/notify", open_socket=failing)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(notifier.watchdog())
            self.assertFalse(notifier.watchdog())
        self.assertEqual([s.closed for s in made], [True, True])

    def test_a_full_queue_fails_the_send_at_once(self) -> None:
        """systemd not reading its socket (PID 1 busy or stuck) fills the queue: a send then fails, it never waits."""
        path = os.path.join(self.tmp, "notify")
        sock = self.listener(path)  # bound, never read
        notifier = ServiceNotifier(path)
        sent: list[bool] = []
        out = io.StringIO()

        def flood() -> None:
            with contextlib.redirect_stdout(out):
                sent.extend(notifier.watchdog() for _ in range(5000))

        thread = threading.Thread(target=flood, daemon=True)
        thread.start()
        thread.join(10)
        waited = thread.is_alive()
        sock.close()  # wakes a send that waits, so the thread ends either way
        thread.join(5)
        self.assertFalse(waited, "a send waited for the full queue")
        self.assertEqual(len(sent), 5000)
        self.assertTrue(sent[0])
        self.assertFalse(sent[-1])
        [warning] = out.getvalue().splitlines()
        self.assertTrue(warning.startswith("<4>cannot notify systemd (WATCHDOG): [Errno 11]"), warning)

    def test_a_socket_that_cannot_be_opened_is_a_failed_send(self) -> None:
        def no_socket() -> socket.socket:
            raise OSError(24, "Too many open files")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertFalse(ServiceNotifier("/run/systemd/notify", open_socket=no_socket).ready())
        self.assertEqual(out.getvalue(), "<4>cannot notify systemd (READY): [Errno 24] Too many open files\n")


if __name__ == "__main__":
    unittest.main()

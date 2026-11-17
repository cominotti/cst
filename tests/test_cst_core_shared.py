"""Unit tests for the concepts several CST modules share through cst_core.

errors, text, units, sizes, owned-file states (domain); file reads, journal
lines, run locks and the NOCOW attribute (adapters). The unit renderer is
checked against the timers the vm and toolchains programs write today, so
those modules can switch to it without changing a byte.
"""

import contextlib
import io
import os
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

from cst_core import adapters as core_adapters
from cst_core.adapters import files as core_files, nocow
from cst_core.adapters.files import ensure_dir, owned_file_state_at, read_bytes, read_int, read_text
from cst_core.adapters.journal import log
from cst_core.adapters.locks import take_lock
from cst_core.domain.errors import Refused, Usage
from cst_core.domain.files import owned_file_state
from cst_core.domain.files import checked_path
from cst_core.domain.text import first_line, is_yes, parse_assignments
from cst_core.domain.units import render_timer, render_unit_file
from cst_core.domain.values import GIB, MIB, format_bytes_gib_mib
from toolchains import domain as tc_domain
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
            f"{tc_domain.MARKER}: installed by `cst toolchains auto on` (or snapshots setup); not edited by hand."
        ]
        self.assertEqual(render_timer(header, "CST: prune old toolchain caches, hourly", "hourly", "10min"), before)
        self.assertEqual(tc_domain.render_timer(), before)

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
        # PRIM-07: "--5" and a superscript two passed str.isdigit and then raised ValueError out of int().
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
        # One link rule with the installer (pr-a preexisting #11): at a root destination a link is
        # always replaced ("change", whatever it points to); at a user destination its target counts.
        self.assertEqual(owned_file_state_at(link, b"#!/x\nMARK v1\n", mark, owner="root"), "change")
        self.assertEqual(owned_file_state_at(self.path("link-theirs"), b"y", mark, owner="root"), "change")
        self.assertEqual(owned_file_state_at(link, b"#!/x\nMARK v1\n", mark, owner="user"), "ok")
        self.assertEqual(owned_file_state_at(link, b"#!/x\nMARK v2\n", mark, owner="user"), "change")
        self.assertEqual(owned_file_state_at(self.path("link-theirs"), b"y", mark, owner="user"), "conflict")
        self.assertEqual(owned_file_state_at(self.path("dangling"), b"y", mark, owner="user"), "missing")

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

    def test_a_lock_file_it_creates_gets_exactly_the_mode(self) -> None:
        # PRIM-03: under a 077 umask (sudo keeps a user's) the 0644 lock cst doctor reads came out 0600.
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
        # PRIM-08: its result was dropped.
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
        # PRIM-08: dest was unlinked first, a moment with no file at all.
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
        # PRIM-08: unlink then symlink left a moment with nothing at the path.
        with mock.patch.object(core_files.os, "unlink", side_effect=AssertionError("unlink called")):
            fs.force_symlink("/dev/null", link)
            fs.force_symlink("/dev/zero", link)
        self.assertEqual(os.readlink(link), "/dev/zero")
        self.assertEqual(os.listdir(self.tmp), ["zram.conf"], "a temporary link was left")

    def test_runner_decodes_bytes_that_are_not_utf8(self) -> None:
        # PRIM-04 (decode part): printf '\\377abc' raised UnicodeDecodeError out of run().
        res = core_adapters.SubprocessCommandRunner().run(["printf", "\\377abc"])
        self.assertEqual((res.returncode, res.stdout), (0, chr(0xFFFD) + "abc"))


class ParseAssignmentsTest(unittest.TestCase):
    """The one KEY=VALUE reader of cpu-setup.conf, vm.conf and the snapshot settings (cpu, vm, doctor)."""

    def test_quotes_comments_and_the_last_assignment(self) -> None:
        text = "FOO='a'\n# FOO=b\n  #BAR=x\nFOO=\"c\"\n  BAZ = d=e \nEMPTY=''\nnot a setting\n"
        self.assertEqual(parse_assignments(text), {"FOO": "c", "BAZ ": "d=e", "EMPTY": ""})

    def test_a_blank_before_the_equal_sign_is_no_assignment(self) -> None:
        # The shell runs `VM_NAME =x` as a command; no lookup finds it here either.
        self.assertNotIn("VM_NAME", parse_assignments("VM_NAME =x\n"))
        self.assertEqual(parse_assignments(""), {})

    def test_strip_keys_keeps_the_last_line(self) -> None:
        # vm's read_conf strips the key; the last line of the file still wins, as in main's cst-vm.
        self.assertEqual(parse_assignments("K=1\nK = 0\nK=1\n", strip_keys=True), {"K": "1"})
        self.assertEqual(parse_assignments("K = 0\nK=1\n", strip_keys=True), {"K": "1"})

    def test_only_a_newline_ends_a_line(self) -> None:
        # The callers read files line by line on main (open() iteration): form feed, vertical tab,
        # the file/group/record separators, NEL and the Unicode line/paragraph separators are not line ends.
        for sep in ("\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(sep=repr(sep)):
                self.assertEqual(parse_assignments(f"A=1{sep}B=2\n"), {"A": f"1{sep}B=2"})


if __name__ == "__main__":
    unittest.main()

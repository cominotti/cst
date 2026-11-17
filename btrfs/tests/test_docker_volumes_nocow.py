"""Unit tests for btrfs/docker-volumes-nocow (Docker volumes without copy-on-write).

The helper runs as root at boot and rewrites Docker volumes. These tests use
its package (btrfs/docker_volumes_nocow, through a PackageView that also
holds cst_core's NOCOW ioctls) and never let it reach the machine:

- every path it writes or reads state from (VOLUMES, FLAG, ATTEMPTS,
  POSTPONED, NOTIFY) points into a temporary folder of the test, and so does
  PROC, the /proc
  where it looks for a dockerd (a fake tree: no process unless a test adds
  one);
- the file attribute ioctls (FS_IOC_GETFLAGS / FS_IOC_SETFLAGS, under
  flags() and set_nocow()) go to a fake that keeps the flags of each inode in
  a dictionary, and refuses any file outside the temporary folder;
- the commands it runs (systemctl, chcon, cp, btrfs, btrfs-notify) go to a
  fake subprocess module; any other command fails the test. The fake cp
  copies the tree in Python and gives each new entry the NOCOW flag of its
  folder, as btrfs does when a file is created;
- os.sync() is replaced by a counter.

Starting the program (its package imported anew) only reads
/etc/docker/daemon.json and does nothing else (ImportTest checks both).

A crash (a kill, a timeout or a power loss) is simulated by raising Crash, a
BaseException, at one step: migrate() catches Exception for each volume, so
Crash leaves the helper the way a dead process would. The next call of
migrate() is the next boot.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -B -m unittest btrfs.tests.test_docker_volumes_nocow -v
"""

import builtins
import contextlib
import fcntl
import gc
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from collections.abc import Callable, Iterator
from types import ModuleType, SimpleNamespace
from unittest import mock

# Run directly (python3 -I -B btrfs/tests/test_docker_volumes_nocow.py): the checkout root holds the tests package.
_CHECKOUT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _CHECKOUT not in sys.path:
    sys.path.insert(0, _CHECKOUT)

from btrfs.docker_volumes_nocow import adapters as nocow_adapters
from btrfs.docker_volumes_nocow import cli as nocow_cli
from cst_core.adapters import nocow as core_nocow
from tests.package_view import PackageView
from tests.script_loader import load_script

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "docker-volumes-nocow")
PKG = "btrfs.docker_volumes_nocow"


def load() -> PackageView:
    """The program's package as one namespace (the program is a thin entry
    over it), plus cst_core's NOCOW ioctls, so a fake fcntl reaches them."""
    return PackageView(nocow_cli, nocow_adapters, core_nocow)


def load_program_fresh() -> ModuleType:
    """Run the program file as a module with its package imported anew, as a
    start of the program does; return that fresh adapters module. The
    package modules the tests use are put back afterwards."""
    saved = {n: m for n, m in sys.modules.items() if n == PKG or n.startswith(PKG + ".")}
    for n in saved:
        del sys.modules[n]
    try:
        load_script(TOOL, "docker_volumes_nocow_import_check")
        return sys.modules[PKG + ".adapters"]
    finally:
        sys.modules.pop("docker_volumes_nocow_import_check", None)
        for n in [n for n in sys.modules if n == PKG or n.startswith(PKG + ".")]:
            del sys.modules[n]
        sys.modules.update(saved)
        vars(sys.modules["btrfs"])["docker_volumes_nocow"] = saved[PKG]


M = load()

NOCOW = 0x00800000  # FS_NOCOW_FL, what chattr +C sets
OTHER = 0x00000080  # FS_NOATIME_FL: another attribute the helper must leave alone
TAG = "docker-volumes-nocow"
GIB = 1024**3

# The volume content most tests use: nested folders, files with data, an empty
# file, a symlink, a fifo and an empty folder. 2 MiB of data in all, plus 13
# bytes, so the log and status show "2 MiB".
DATA = {
    "db/base/1": b"\x01" * (1 << 20),
    "db/base/empty": b"",
    "db/wal/000001": bytes(range(256)) * 4096,
    "README": b"hello volume\n",
}
LINKS = {"latest": "db/wal/000001"}
FIFOS = ("db/pipe",)
EMPTY_DIRS = ("cache",)
DATA_BYTES = sum(len(v) for v in DATA.values())
DATA_ENTRIES = len(DATA) + len(LINKS) + len(FIFOS) + len(EMPTY_DIRS) + 3  # + db, db/base, db/wal


class Crash(BaseException):
    """The process dies at this point (kill, timeout, power loss)."""


def ino(path: str) -> int:
    return os.lstat(path).st_ino


def entries(top: str) -> Iterator[tuple[str, os.stat_result]]:
    """(path, lstat) of every entry below top. Like the helper, os.walk lists
    a symlink to a folder but does not enter it."""
    for root, dirs, files in os.walk(top):
        for name in dirs + files:
            p = os.path.join(root, name)
            yield p, os.lstat(p)


def carries_flags(st: os.stat_result) -> bool:
    """Folders and regular files: the entries whose NOCOW flag matters."""
    return stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)


def tree(top: str) -> dict[str, tuple]:
    """Everything below top (and top's own mode), as comparable values."""
    out: dict[str, tuple] = {".": ("dir", stat.S_IMODE(os.lstat(top).st_mode))}
    for p, st in entries(top):
        rel = os.path.relpath(p, top)
        if stat.S_ISLNK(st.st_mode):
            out[rel] = ("link", os.readlink(p))
        elif stat.S_ISDIR(st.st_mode):
            out[rel] = ("dir", stat.S_IMODE(st.st_mode))
        elif stat.S_ISREG(st.st_mode):
            with open(p, "rb") as f:
                out[rel] = ("file", stat.S_IMODE(st.st_mode), f.read())
        else:
            out[rel] = ("other", stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode))
    return out


def cp_fails(src: str, dst: str) -> tuple[int, str]:
    """A cp that fails at once, copying nothing."""
    return 1, "cp: boom"


def check_failed(entries_new: int, bytes_new: int, cow_files: int) -> str:
    """The helper's message when the copy of the DATA volume does not match."""
    return (
        f"copy check failed (entries {DATA_ENTRIES} vs {entries_new}, bytes {DATA_BYTES} vs {bytes_new}, "
        f"CoW files {cow_files})"
    )


class OtherDevice:
    """os.lstat() of a mount point: the same folder, on another filesystem."""

    def __init__(self, st: os.stat_result) -> None:
        self.st = st

    @property
    def st_dev(self) -> int:
        return self.st.st_dev + 1

    def __getattr__(self, name: str) -> object:
        return getattr(self.st, name)


class FakeSystem:
    """The inode flags and the commands the helper runs, for one test."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.attrs: dict[tuple[int, int], int] = {}
        self.labels: dict[tuple[int, int], bytes] = {}  # security.selinux of each inode; none by default
        self.sets: list[str] = []  # paths whose flags the helper set (FS_IOC_SETFLAGS)
        self.calls: list[list[str]] = []
        self.docker_state = "inactive"
        self.systemctl_rc = 0
        self.systemctl_out_on_error = False  # a failing systemctl still prints docker_state
        self.systemctl_missing = False
        self.free = 1 << 50
        self.btrfs_out: str | None = None  # None: a normal `btrfs filesystem usage -b` answer
        self.chcon_rc = 0
        self.syncs = 0
        self.cp: Callable[[str, str], tuple[int, str]] = self.copy

    # ---- the ioctls -------------------------------------------------------

    def key(self, path: str) -> tuple[int, int]:
        st = os.lstat(path)
        return st.st_dev, st.st_ino

    def set(self, path: str, nocow: bool, other: int = 0) -> None:
        """Give path's inode these flags (the test's own setup, not recorded)."""
        self.attrs[self.key(path)] = (NOCOW if nocow else 0) | other

    def flags_of(self, path: str) -> int:
        return self.attrs.get(self.key(path), 0)

    def nocow(self, path: str) -> bool:
        return bool(self.flags_of(path) & NOCOW)

    def sets_below(self, top: str) -> list[str]:
        """The paths at or below top whose flags the helper set."""
        return [p for p in self.sets if p == top or p.startswith(top + os.sep)]

    def ioctl(self, fd: int, request: int, arg: bytes | int = 0, mutate_flag: bool = True) -> bytes | int:
        target = os.readlink(f"/proc/self/fd/{fd}")
        if target != self.root and not target.startswith(self.root + os.sep):
            raise AssertionError(f"ioctl on {target}, outside the test folder {self.root}")
        st = os.fstat(fd)
        key = (st.st_dev, st.st_ino)
        if request == core_nocow.FS_IOC_GETFLAGS:
            return struct.pack("l", self.attrs.get(key, 0))
        if request == core_nocow.FS_IOC_SETFLAGS:
            if not isinstance(arg, bytes):
                raise AssertionError(f"SETFLAGS without a buffer: {arg!r}")
            self.attrs[key] = struct.unpack("l", arg)[0]
            self.sets.append(target)
            return 0
        raise AssertionError(f"unexpected ioctl request {request:#x}")

    # ---- SELinux labels (os.getxattr) ---------------------------------------

    def label(self, path: str, value: bytes | None) -> None:
        """Give path's inode this label (None: no label)."""
        if value is None:
            self.labels.pop(self.key(path), None)
        else:
            self.labels[self.key(path)] = value

    def label_of(self, path: str) -> bytes | None:
        return self.labels.get(self.key(path))

    def getxattr(self, path: str, attribute: str, *, follow_symlinks: bool = True) -> bytes:
        if attribute != "security.selinux":
            raise AssertionError(f"unexpected xattr {attribute} of {path}")
        if follow_symlinks:
            raise AssertionError(f"getxattr follows the link {path}")
        if path != self.root and not path.startswith(self.root + os.sep):
            raise AssertionError(f"getxattr on {path}, outside the test folder {self.root}")
        value = self.labels.get(self.key(path))
        if value is None:
            raise OSError(61, "No data available", path)  # ENODATA
        return value

    def copy_label(self, src: str, dst: str) -> None:
        """What cp -a and chcon --reference do with the label."""
        self.label(dst, self.label_of(src))

    # ---- the commands -----------------------------------------------------

    def run(self, args: list[str], **kw: object) -> subprocess.CompletedProcess:
        args = list(args)
        self.calls.append(args)
        text = bool(kw.get("text"))

        def done(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
            if text:
                return subprocess.CompletedProcess(args, rc, out, err)
            return subprocess.CompletedProcess(args, rc, out.encode(), err.encode())

        if args[0] == "systemctl":
            if args != ["systemctl", "show", "-P", "ActiveState", "docker.service"]:
                raise AssertionError(f"unexpected systemctl call: {args}")
            if self.systemctl_missing:
                raise FileNotFoundError(2, "No such file or directory", "systemctl")
            answers = self.systemctl_rc == 0 or self.systemctl_out_on_error
            return done(self.systemctl_rc, self.docker_state + "\n" if answers else "")
        if args[0] == "chcon":
            if args[1:2] != ["--reference"] or len(args) != 4:
                raise AssertionError(f"unexpected chcon call: {args}")
            if self.chcon_rc == 0:
                self.copy_label(args[2], args[3])
            return done(self.chcon_rc)
        if args[0] == "btrfs":
            if args[:4] != ["btrfs", "filesystem", "usage", "-b"]:
                raise AssertionError(f"unexpected btrfs call: {args}")
            if self.btrfs_out is not None:
                return done(1, self.btrfs_out, "ERROR: not a btrfs filesystem")
            return done(
                0,
                "Overall:\n"
                "    Device size:\t\t 2000398934016\n"
                f"    Free (estimated):\t\t {self.free}\t(min: {self.free})\n"
                "    Data ratio:\t\t\t      2.00\n",
            )
        if args[0] == "cp":
            if args[:5] != ["cp", "-a", "--reflink=never", "--sparse=auto", "--"] or len(args) != 7:
                raise AssertionError(f"unexpected cp call: {args}")
            src, dst = args[5], args[6]
            if not (src.endswith("/.") and dst.endswith("/")):
                raise AssertionError(f"cp must copy the contents: {args}")
            rc, err = self.cp(src[:-2], dst[:-1])
            return done(rc, "", err)
        if args[0] == nocow_adapters.NOTIFY:
            return done()
        raise AssertionError(f"unexpected command: {args}")

    def sync(self) -> None:
        self.syncs += 1

    def commands(self, name: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == name]

    # ---- the fake cp ------------------------------------------------------

    def inherit(self, path: str) -> None:
        """A new file or folder: btrfs gives it +C when its folder has +C."""
        parent = self.nocow(os.path.dirname(path))
        self.attrs[self.key(path)] = NOCOW if parent else 0

    def copy(self, src: str, dst: str) -> tuple[int, str]:
        """`cp -a SRC/. DST/`: the contents and SRC's own mode, times and label."""
        self.copy_into(src, dst)
        self.copy_label(src, dst)
        st = os.lstat(src)
        os.chmod(dst, stat.S_IMODE(st.st_mode))
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
        return 0, ""

    def copy_then(self, change: Callable[[str], object]) -> Callable[[str, str], tuple[int, str]]:
        """A cp that copies, then spoils the copy with change(dst), and reports success."""

        def cp(src: str, dst: str) -> tuple[int, str]:
            self.copy(src, dst)
            change(dst)
            return 0, ""

        return cp

    def copy_into(self, src: str, dst: str, stop_after: int | None = None) -> int:
        """Copy the entries of src into dst; with stop_after, raise Crash after that many."""
        n = 0
        for name in sorted(os.listdir(src)):
            s, d = os.path.join(src, name), os.path.join(dst, name)
            st = os.lstat(s)
            if stat.S_ISLNK(st.st_mode):
                os.symlink(os.readlink(s), d)
            elif stat.S_ISDIR(st.st_mode):
                os.mkdir(d)
                self.inherit(d)
                n += self.copy_into(s, d, None if stop_after is None else stop_after - n)
            elif stat.S_ISREG(st.st_mode):
                open(d, "wb").close()
                self.inherit(d)  # +C is set while the file is still empty
                shutil.copyfile(s, d)
            elif stat.S_ISFIFO(st.st_mode):
                os.mkfifo(d)
            else:
                raise AssertionError(f"fake cp: unexpected entry {s}")
            if not stat.S_ISLNK(st.st_mode):
                os.chmod(d, stat.S_IMODE(st.st_mode))
            os.utime(d, ns=(st.st_atime_ns, st.st_mtime_ns), follow_symlinks=False)
            self.copy_label(s, d)
            n += 1
            if stop_after is not None and n >= stop_after:
                raise Crash(f"killed during the copy, after {n} entries")
        return n


class Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="cst-nocow-test-")
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)
        self.vols = os.path.join(self.tmp, "var/lib/docker/volumes")
        self.flag = os.path.join(self.tmp, "etc/btrfs-setup/docker-nocow-pending")
        self.attempts = os.path.join(self.tmp, "var/lib/docker-volumes-nocow/attempts")
        self.postponed = os.path.join(self.tmp, "var/lib/docker-volumes-nocow/postponed")
        self.notify = os.path.join(self.tmp, "usr/local/sbin/btrfs-notify")
        # The processes the helper looks at: none, unless a test adds one.
        self.proc = os.path.join(self.tmp, "proc")
        os.makedirs(self.proc)
        os.makedirs(self.vols)
        os.makedirs(os.path.dirname(self.flag))
        # Docker keeps its volume database next to the volumes: a file, not a volume.
        with open(os.path.join(self.vols, "metadata.db"), "wb") as f:
            f.write(b"bolt")
        self.fs = FakeSystem(self.tmp)
        for name, value in (
            ("VOLUMES", self.vols),
            ("FLAG", self.flag),
            ("ATTEMPTS", self.attempts),
            ("POSTPONED", self.postponed),
            ("NOTIFY", self.notify),
            ("PROC", self.proc),
            ("fcntl", SimpleNamespace(ioctl=self.fs.ioctl)),
            ("subprocess", SimpleNamespace(run=self.fs.run)),
        ):
            self.enterContext(mock.patch.object(M, name, value))
        self.enterContext(mock.patch.object(os, "sync", self.fs.sync))
        self.enterContext(mock.patch.object(os, "getxattr", self.fs.getxattr))
        # Nothing of the helper may point outside the test folder.
        for path in (
            nocow_adapters.VOLUMES,
            nocow_adapters.FLAG,
            nocow_adapters.ATTEMPTS,
            nocow_adapters.POSTPONED,
            nocow_adapters.NOTIFY,
            nocow_adapters.PROC,
        ):
            self.assertTrue(path.startswith(self.tmp + os.sep), path)

    def fresh(self) -> None:
        """A new test folder and new fakes, for the next subTest."""
        self.doCleanups()
        self.setUp()

    # ---- set-up helpers ---------------------------------------------------

    def fill(self, top: str, nocow: bool = False, data: dict[str, bytes] | None = None) -> str:
        """Create folder top with DATA (or data); nocow: every folder and file has +C."""
        os.makedirs(top)
        os.chmod(top, 0o750)
        self.fs.set(top, nocow)
        for rel, content in (DATA if data is None else data).items():
            p = os.path.join(top, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(content)
            os.chmod(p, 0o640)
        if data is None:
            for rel, target in LINKS.items():
                os.symlink(target, os.path.join(top, rel))
            for rel in FIFOS:
                os.mkfifo(os.path.join(top, rel))
            for rel in EMPTY_DIRS:
                os.makedirs(os.path.join(top, rel))
        for p, st in entries(top):
            if carries_flags(st):
                self.fs.set(p, nocow)
        return top

    def volume(self, name: str, nocow: bool = False, data: dict[str, bytes] | None = None) -> str:
        """VOLUMES/NAME/_data, filled by fill(); returns the _data path."""
        os.mkdir(os.path.join(self.vols, name))
        self.fs.set(os.path.join(self.vols, name), nocow)
        return self.fill(os.path.join(self.vols, name, "_data"), nocow, data)

    def set_attempts(self, text: str) -> None:
        os.makedirs(os.path.dirname(self.attempts), exist_ok=True)
        with open(self.attempts, "w") as f:
            f.write(text)

    def read_attempts(self) -> str | None:
        try:
            with open(self.attempts) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def schedule(self) -> None:
        """What `cst btrfs install` leaves when mark finds CoW data."""
        open(self.flag, "a").close()

    def call(self, fn: Callable[[], int | None]) -> tuple[int | None, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = fn()
        return rc, out.getvalue()

    def main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(sys, "argv", [TOOL, *argv]),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            rc = nocow_cli.main()
        return rc, out.getvalue(), err.getvalue()

    # ---- checks -----------------------------------------------------------

    def assert_all_nocow(self, top: str) -> None:
        self.assertTrue(self.fs.nocow(top), top)
        for p, st in entries(top):
            if carries_flags(st):
                self.assertTrue(self.fs.nocow(p), p)

    def assert_no_leftovers(self, name: str) -> None:
        self.assertEqual(sorted(os.listdir(os.path.join(self.vols, name))), ["_data"])


class ImportTest(unittest.TestCase):
    def test_import_only_reads_daemon_json(self) -> None:
        opened: list[tuple[str, str]] = []

        def fake_open(path: str, mode: str = "r", *a: object, **kw: object) -> io.StringIO:
            opened.append((str(path), mode))
            raise FileNotFoundError(2, "No such file or directory", path)

        def trap(name: str) -> Callable[..., object]:
            def refuse(*a: object, **kw: object) -> object:
                raise AssertionError(f"loading the module called {name}")

            return refuse

        # Everything the helper uses to change the machine or run a command.
        changes = (
            (os, ("open", "mkdir", "makedirs", "remove", "unlink", "rename", "rmdir", "chown", "chmod", "sync")),
            (shutil, ("rmtree",)),
            (subprocess, ("run", "Popen")),
            (fcntl, ("ioctl",)),
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(builtins, "open", fake_open))
            for module, names in changes:
                for name in names:
                    stack.enter_context(mock.patch.object(module, name, trap(f"{module.__name__}.{name}")))
            loaded = load_program_fresh()
        self.assertEqual(opened, [("/etc/docker/daemon.json", "r")])
        self.assertEqual(loaded.VOLUMES, "/var/lib/docker/volumes")
        self.assertEqual(loaded.FLAG, "/etc/btrfs-setup/docker-nocow-pending")
        self.assertEqual(loaded.ATTEMPTS, "/var/lib/docker-volumes-nocow/attempts")
        self.assertEqual(loaded.POSTPONED, "/var/lib/docker-volumes-nocow/postponed")
        self.assertEqual(loaded.PROC, "/proc")

    def test_docker_root_reads_data_root(self) -> None:
        for text, want in (
            ('{"data-root": "/srv/docker", "cgroup-parent": "builds.slice"}', "/srv/docker/volumes"),
            ('{"cgroup-parent": "builds.slice"}', "/var/lib/docker/volumes"),
            ('{"data-root": ""}', "/var/lib/docker/volumes"),
            ("not json {", "/var/lib/docker/volumes"),
            # Not an object, or not a path: dockerd's default (these crashed the helper at load).
            ('["data-root"]', "/var/lib/docker/volumes"),
            ('{"data-root": 5}', "/var/lib/docker/volumes"),
        ):
            with self.subTest(text=text):

                def fake_open(path: str, mode: str = "r", *a: object, text: str = text, **kw: object) -> io.StringIO:
                    self.assertEqual(path, "/etc/docker/daemon.json")
                    return io.StringIO(text)

                with mock.patch.object(builtins, "open", fake_open):
                    self.assertEqual(nocow_adapters.docker_root(), want)


class StatusTest(Base):
    def test_volumes_folder_absent(self) -> None:
        shutil.rmtree(self.vols)
        rc, out = self.call(nocow_cli.status)
        self.assertEqual((rc, out), (0, f"{self.vols}: absent\n"))
        self.assertEqual(self.fs.calls, [])

    def test_counts_and_changes_nothing(self) -> None:
        self.fs.set(self.vols, True)
        self.volume("done", nocow=True)
        self.volume("cow")
        # Data files all +C, but one folder without it: new files there are CoW.
        mixed = self.volume("mixed", nocow=True)
        self.fs.set(os.path.join(mixed, "db/wal"), False)
        before = dict(self.fs.attrs)
        rc, out = self.call(nocow_cli.status)
        self.assertEqual(rc, 0)
        self.assertEqual(
            out,
            f"{self.vols}: new volumes NOCOW; 1 of 3 volume(s) fully NOCOW\n"
            "  1 with copy-on-write data (2 MiB), migration not scheduled\n",
        )
        self.schedule()
        rc, out = self.call(nocow_cli.status)
        self.assertIn("  1 with copy-on-write data (2 MiB), migration at the next boot\n", out)
        # status is read only: no flag set, no command run.
        self.assertEqual(self.fs.sets, [])
        self.assertEqual(self.fs.attrs, before)
        self.assertEqual(self.fs.calls, [])

    def test_volumes_folder_without_nocow(self) -> None:
        self.volume("done", nocow=True)
        rc, out = self.call(nocow_cli.status)
        self.assertEqual(out, f"{self.vols}: new volumes copy-on-write; 1 of 1 volume(s) fully NOCOW\n")


class NotVolumeDataTest(Base):
    """What the helper must neither count nor change: a _data that is a
    symlink, and a filesystem mounted below a volume."""

    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)
        self.volume("done", nocow=True)
        self.all_done = f"{self.vols}: new volumes NOCOW; 1 of 1 volume(s) fully NOCOW\n"

    def test_symlinked_data_is_not_a_volume(self) -> None:
        elsewhere = self.fill(os.path.join(self.tmp, "elsewhere"))  # copy-on-write data
        os.mkdir(os.path.join(self.vols, "linked"))
        os.symlink(elsewhere, os.path.join(self.vols, "linked", "_data"))
        before = tree(elsewhere)
        self.assertEqual(self.call(nocow_cli.status), (0, self.all_done))
        self.schedule()
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        self.assertIn(f"<5>{TAG}: rewriting 0 volume(s)", out)
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertEqual(self.fs.sets_below(elsewhere), [])
        self.assertEqual(tree(elsewhere), before)
        self.assertTrue(os.path.islink(os.path.join(self.vols, "linked", "_data")))

    def test_mount_below_a_volume_is_not_entered(self) -> None:
        mnt = self.fill(os.path.join(self.vols, "done", "_data", "mnt"))  # copy-on-write data
        before = tree(mnt)
        # Seen as part of the volume, the folder holds copy-on-write data.
        rc, out = self.call(nocow_cli.status)
        self.assertIn("0 of 1 volume(s) fully NOCOW\n  1 with copy-on-write data (2 MiB)", out)
        real_lstat = os.lstat

        def lstat(path: str) -> object:
            st = real_lstat(path)
            return OtherDevice(st) if path == mnt else st

        with mock.patch.object(os, "lstat", lstat):
            self.assertEqual(self.call(nocow_cli.status), (0, self.all_done))
            self.assertEqual(self.call(nocow_cli.mark), (0, ""))
        self.assertEqual(self.fs.sets_below(mnt), [])
        self.assertEqual(tree(mnt), before)


class MarkUnmarkTest(Base):
    def test_mark_absent(self) -> None:
        shutil.rmtree(self.vols)
        rc, out = self.call(nocow_cli.mark)
        self.assertEqual((rc, out), (0, f"{self.vols}: absent (Docker not set up yet): nothing to mark\n"))
        self.assertEqual(self.fs.sets, [])

    def test_mark_fixes_what_it_can_in_place_and_reports_the_rest(self) -> None:
        self.fs.set(self.vols, False, other=OTHER)
        top = self.volume("cow")
        self.set_attempts("2")
        rc, out = self.call(nocow_cli.mark)
        self.assertEqual(rc, 1)
        self.assertEqual(
            out,
            "1 existing volume(s) hold copy-on-write data (2 MiB): rewritten at the next boot, before Docker starts\n",
        )
        self.assertEqual(self.fs.flags_of(self.vols), NOCOW | OTHER)  # the other attribute kept
        # In place: the folders and the empty file. Files with data need the copy.
        for rel in ("", "db", "db/base", "db/wal", "cache", "db/base/empty"):
            self.assertTrue(self.fs.nocow(os.path.join(top, rel)), rel)
        for rel in ("db/base/1", "db/wal/000001", "README"):
            self.assertFalse(self.fs.nocow(os.path.join(top, rel)), rel)
        self.assertIsNone(self.read_attempts())  # a new request gets its two boots again
        self.assertFalse(os.path.exists(self.flag))  # the flag is install's job
        self.assertEqual(self.fs.calls, [])

    def test_mark_with_nothing_to_copy(self) -> None:
        self.volume("done", nocow=True)
        self.volume("empty", data={"a/b": b""})
        self.set_attempts("1")
        rc, out = self.call(nocow_cli.mark)
        self.assertEqual((rc, out), (0, ""))
        self.assertTrue(self.fs.nocow(self.vols))
        self.assert_all_nocow(os.path.join(self.vols, "empty/_data"))
        self.assertIsNone(self.read_attempts())

    def test_unmark_clears_only_the_volumes_folder(self) -> None:
        self.fs.set(self.vols, True, other=OTHER)
        top = self.volume("done", nocow=True)
        rc, out = self.call(nocow_cli.unmark)
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(self.fs.flags_of(self.vols), OTHER)
        self.assert_all_nocow(top)  # existing volumes stay NOCOW
        self.assertEqual(self.fs.sets, [self.vols])

    def test_unmark_absent(self) -> None:
        shutil.rmtree(self.vols)
        self.assertEqual(self.call(nocow_cli.unmark), (0, ""))
        self.assertEqual(self.fs.sets, [])


class MainTest(Base):
    def test_usage_and_default(self) -> None:
        rc, out, err = self.main("bogus")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("usage: docker-volumes-nocow status|mark|migrate|unmark", err)
        rc, out, err = self.main()
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith(f"{self.vols}: new volumes copy-on-write;"), out)

    def test_oserror_exit_codes(self) -> None:
        # A data-root that is not on btrfs: the flag ioctls fail.
        def no_flags(fd: int, request: int, arg: bytes | int = 0, mutate_flag: bool = True) -> bytes | int:
            raise OSError(25, "Inappropriate ioctl for device")

        self.volume("cow")
        with mock.patch.object(M, "fcntl", SimpleNamespace(ioctl=no_flags)):
            for cmd, want in (("status", 2), ("mark", 2), ("migrate", 0), ("unmark", 0)):
                with self.subTest(cmd=cmd):
                    rc, out, _ = self.main(cmd)
                    self.assertEqual(rc, want)
                    self.assertIn(f"<3>{TAG}: {self.vols}: [Errno 25] Inappropriate ioctl for device\n", out)


class MigrateTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)  # what install's mark left

    def test_happy_path(self) -> None:
        done = self.volume("done", nocow=True)
        done_ino = ino(done)
        top = self.volume("cow")
        before, old_ino = tree(top), ino(top)
        self.schedule()
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(tree(top), before)  # same entries, modes, data and link targets
        self.assertNotEqual(ino(top), old_ino)  # a new copy
        self.assert_all_nocow(top)
        self.assert_no_leftovers("cow")
        self.assertEqual(ino(done), done_ino)  # a NOCOW volume is not copied
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(self.read_attempts())
        new = top + ".nocow-new"
        self.assertEqual(self.fs.commands("chcon"), [["chcon", "--reference", top, new]])
        self.assertEqual(
            self.fs.commands("cp"), [["cp", "-a", "--reflink=never", "--sparse=auto", "--", top + "/.", new + "/"]]
        )
        self.assertEqual(self.fs.commands("btrfs"), [["btrfs", "filesystem", "usage", "-b", self.vols]])
        self.assertEqual(self.fs.syncs, 1)  # before the names are swapped (RecoverTest checks the order)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"<5>{TAG}: rewriting 1 volume(s) without copy-on-write before Docker starts")
        self.assertRegex(lines[1], rf"^<6>{TAG}: cow: 3 file\(s\), 2 MiB in \d+s$")
        self.assertEqual(lines[2], f"<5>{TAG}: all Docker volumes are NOCOW now")
        self.assertEqual(len(lines), 3)

    def test_volumes_folder_absent(self) -> None:
        shutil.rmtree(self.vols)
        self.schedule()
        self.assertEqual(self.call(nocow_cli.migrate), (0, ""))
        self.assertEqual(self.fs.calls, [])
        self.assertTrue(os.path.exists(self.flag))

    def test_not_scheduled_copies_nothing(self) -> None:
        top = self.volume("cow")
        before = tree(top)
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual(tree(top), before)
        self.assertFalse(self.fs.nocow(os.path.join(top, "README")))
        self.assertEqual([c[0] for c in self.fs.calls], ["systemctl"])
        self.assertIsNone(self.read_attempts())

    def test_volumes_folder_lost_its_flag_schedules_and_migrates_at_once(self) -> None:
        # Docker installed after `cst btrfs install`: its volumes folder has no +C.
        self.fs.set(self.vols, False)
        top = self.volume("cow")
        before = tree(top)
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        self.assertTrue(self.fs.nocow(self.vols))
        self.assertEqual(tree(top), before)
        self.assert_all_nocow(top)
        self.assert_no_leftovers("cow")
        self.assertFalse(os.path.exists(self.flag))  # created, then removed once done
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)

    def test_flag_folder_missing_is_created_and_the_volumes_migrated(self) -> None:
        # The same case as above on a machine where /etc/btrfs-setup does not
        # exist (install only creates it when mark found CoW data, or the
        # snapshots setup did). The helper creates the folder: before the fix,
        # open(FLAG) failed after +C was already set on the volumes folder, so
        # no later boot scheduled the migration again.
        shutil.rmtree(os.path.dirname(self.flag))
        self.fs.set(self.vols, False)
        top = self.volume("cow")
        before = tree(top)
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assertNotIn(f"<3>{TAG}", out)
        self.assertTrue(self.fs.nocow(self.vols))
        self.assertEqual(tree(top), before)
        self.assert_all_nocow(top)
        self.assert_no_leftovers("cow")
        self.assertTrue(os.path.isdir(os.path.dirname(self.flag)))
        self.assertFalse(os.path.exists(self.flag))  # created, then removed once done
        rc, out, _ = self.main("status")
        self.assertNotIn("with copy-on-write data", out)

    def test_flag_is_durable_before_nocow_is_set(self) -> None:
        # The flag, its entry in its folder and the folder's entry in /etc are
        # flushed before +C is set: after a power loss, +C on the volumes
        # folder must not stay while the flag is lost.
        shutil.rmtree(os.path.dirname(self.flag))
        self.fs.set(self.vols, False)
        self.volume("cow")
        synced: list[tuple[str, list[str]]] = []
        real_fsync = os.fsync

        def fsync(fd: int) -> None:
            synced.append((os.readlink(f"/proc/self/fd/{fd}"), list(self.fs.sets)))
            real_fsync(fd)

        with mock.patch.object(os, "fsync", fsync):
            rc, out = self.call(nocow_cli.migrate)
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        folder = os.path.dirname(self.flag)
        self.assertEqual(synced[:3], [(self.flag, []), (folder, []), (os.path.dirname(folder), [])])
        # Then, and only then, the attempts counter (write_atomic): its temporary file, then its folder.
        counter = os.path.dirname(self.attempts)
        self.assertEqual(len(synced), 5, synced)
        self.assertTrue(synced[3][0].startswith(os.path.join(counter, ".attempts.")), synced[3])
        self.assertEqual(synced[4][0], counter)

    def test_flag_not_durable_leaves_the_folder_copy_on_write(self) -> None:
        # fsync fails (an I/O error): treated like a flag that cannot be written.
        self.fs.set(self.vols, False)
        top = self.volume("cow")

        def fsync(fd: int) -> None:
            raise OSError(5, "Input/output error")

        with mock.patch.object(os, "fsync", fsync):
            rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(
            out,
            f"<3>{TAG}: cannot schedule the migration ([Errno 5] Input/output error); "
            f"{self.vols} keeps copy-on-write, the next boot tries again\n",
        )
        self.assertEqual(self.fs.sets, [])
        self.assertFalse(self.fs.nocow(os.path.join(top, "README")))

    def test_flag_not_written_leaves_the_folder_copy_on_write(self) -> None:
        # The flag cannot be written (here its folder is a file): nothing is
        # set, so the volumes folder still has no +C and the next boot sees
        # the request again.
        shutil.rmtree(os.path.dirname(self.flag))
        with open(os.path.dirname(self.flag), "w") as f:
            f.write("in the way\n")
        self.fs.set(self.vols, False)
        top = self.volume("cow")
        before = tree(top)
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        self.assertRegex(
            out,
            rf"^<3>{TAG}: cannot schedule the migration \(\[Errno 17\] File exists: '[^']*'\); "
            rf"{self.vols} keeps copy-on-write, the next boot tries again\n$",
        )
        self.assertEqual(self.fs.sets, [])  # no +C anywhere, the volumes folder included
        self.assertFalse(self.fs.nocow(self.vols))
        self.assertEqual(tree(top), before)
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertIsNone(self.read_attempts())
        # The obstacle goes: the next boot schedules and migrates.
        os.remove(os.path.dirname(self.flag))
        rc, out, _ = self.main("migrate")
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assert_all_nocow(top)
        self.assertTrue(self.fs.nocow(self.vols))

    def test_folder_without_nocow_and_no_cow_data_needs_no_flag(self) -> None:
        # Only folders and empty files: fixed in place, nothing scheduled, and
        # no flag folder is created for it.
        shutil.rmtree(os.path.dirname(self.flag))
        self.fs.set(self.vols, False)
        top = self.volume("empty", data={"a/b": b""})
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual((rc, out), (0, ""))
        self.assertTrue(self.fs.nocow(self.vols))
        self.assert_all_nocow(top)
        self.assertFalse(os.path.exists(os.path.dirname(self.flag)))

    def test_scheduled_with_nothing_left_to_copy(self) -> None:
        top = self.volume("empty", data={"a/b": b""})
        self.schedule()
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        self.assert_all_nocow(top)  # fixed in place
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(self.read_attempts())
        self.assertIn(f"<5>{TAG}: rewriting 0 volume(s)", out)

    def migrate_refused(
        self, cp: Callable[[str, str], tuple[int, str]], why: str, make: Callable[[], str] | None = None
    ) -> None:
        """Migrate the CoW volume "cow" (made by make, if given) with this cp:
        the copy is refused, and the original stays as it was, to be tried
        again at the next boot."""
        top = make() if make else self.volume("cow")
        before, old_ino = tree(top), ino(top)
        self.schedule()
        self.fs.cp = cp
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(tree(top), before)
        self.assertEqual(ino(top), old_ino)  # the original, untouched
        self.assertFalse(self.fs.nocow(os.path.join(top, "README")))
        self.assert_no_leftovers("cow")
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(self.read_attempts(), "1")
        self.assertIn(f"<3>{TAG}: cow: {why}; left as it was\n", out)
        self.assertNotIn("all Docker volumes are NOCOW now", out)
        self.assertEqual(self.fs.syncs, 0)

    def test_copy_check_refuses(self) -> None:
        for name, change, why in (
            (
                "drops a file",
                lambda dst: os.remove(os.path.join(dst, "README")),
                check_failed(DATA_ENTRIES - 1, DATA_BYTES - 13, 0),
            ),
            (
                "truncates a file",
                lambda dst: os.truncate(os.path.join(dst, "db/base/1"), 1000),
                check_failed(DATA_ENTRIES, DATA_BYTES - (1 << 20) + 1000, 0),
            ),
            (
                "leaves a CoW file",
                lambda dst: self.fs.set(os.path.join(dst, "db/wal/000001"), False),
                check_failed(DATA_ENTRIES, DATA_BYTES, 1),
            ),
            (
                "leaves a CoW folder",
                lambda dst: self.fs.set(os.path.join(dst, "cache"), False),
                check_failed(DATA_ENTRIES, DATA_BYTES, 0),
            ),
        ):
            with self.subTest(cp=name):
                self.fresh()
                self.migrate_refused(self.fs.copy_then(change), why)

    def test_copy_fails(self) -> None:
        def full_disk(src: str, dst: str) -> tuple[int, str]:
            with open(os.path.join(dst, "partial"), "wb") as f:
                f.write(b"x")
            return 1, "cp: error writing 'db/base/1': No space left on device\n"

        self.migrate_refused(full_disk, "copy failed: cp: error writing 'db/base/1': No space left on device")

    # ---- SELinux labels -----------------------------------------------------
    # Tier 2 cannot show real labels (CI's kernel has no SELinux); these use
    # the fake getxattr, which cp -a and chcon --reference copy across.
    CONTAINER = b"system_u:object_r:container_file_t:s0:c12,c34"

    def labelled_volume(self) -> str:
        top = self.volume("cow")
        self.fs.label(top, self.CONTAINER + b"\0")  # as the kernel returns it
        for p, _ in entries(top):
            self.fs.label(p, self.CONTAINER)
        return top

    def test_labels_kept(self) -> None:
        top = self.labelled_volume()
        self.schedule()
        rc, out = self.call(nocow_cli.migrate)
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assertEqual(self.fs.label_of(top), self.CONTAINER + b"\0")
        for p, _ in entries(top):
            self.assertEqual(self.fs.label_of(p), self.CONTAINER, p)

    def test_copy_check_refuses_a_lost_or_changed_label(self) -> None:
        other = b"system_u:object_r:var_lib_t:s0"
        old = self.CONTAINER.decode()
        for name, change, why in (
            (
                "drops a file's label",
                lambda dst: self.fs.label(os.path.join(dst, "db/base/1"), None),
                f"db/base/1: {old} vs none",
            ),
            (
                "changes a folder's label",
                lambda dst: self.fs.label(os.path.join(dst, "db/wal"), other),
                f"db/wal: {old} vs {other.decode()}",
            ),
            (
                "changes a link's own label",
                lambda dst: self.fs.label(os.path.join(dst, "latest"), other),
                f"latest: {old} vs {other.decode()}",
            ),
            ("loses the top label", lambda dst: self.fs.label(dst, None), f".: {old} vs none"),
        ):
            with self.subTest(cp=name):
                self.fresh()
                self.migrate_refused(
                    self.fs.copy_then(change), f"copy check failed (SELinux label of {why})", self.labelled_volume
                )

    def test_labels_equal_up_to_the_trailing_nul(self) -> None:
        top = self.labelled_volume()
        self.schedule()
        # The copy's README reads back with a NUL, the original's without.
        self.fs.cp = self.fs.copy_then(lambda dst: self.fs.label(os.path.join(dst, "README"), self.CONTAINER + b"\0"))
        rc, out = self.call(nocow_cli.migrate)
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assert_all_nocow(top)

    def test_label_unsupported_or_unreadable(self) -> None:
        top = self.volume("cow")
        for err, want in ((95, None), (61, None)):  # EOPNOTSUPP, ENODATA: no label
            with self.subTest(errno=err):

                def getxattr(path: str, attribute: str, *, follow_symlinks: bool = True, err: int = err) -> bytes:
                    raise OSError(err, os.strerror(err), path)

                with mock.patch.object(os, "getxattr", getxattr):
                    self.assertEqual(nocow_adapters.label(top), want)
                    self.assertEqual(set(nocow_adapters.labels(top).values()), {None})

        def denied(path: str, attribute: str, *, follow_symlinks: bool = True) -> bytes:
            raise PermissionError(13, "Permission denied", path)

        with mock.patch.object(os, "getxattr", denied), self.assertRaises(PermissionError):
            nocow_adapters.label(top)

    def test_one_failing_volume_does_not_stop_the_others(self) -> None:
        bad = self.volume("a-bad")
        good = self.volume("b-good")
        bad_before, good_before = tree(bad), tree(good)
        self.schedule()

        def fails_for_a(src: str, dst: str) -> tuple[int, str]:
            return cp_fails(src, dst) if "/a-bad/" in src else self.fs.copy(src, dst)

        self.fs.cp = fails_for_a
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(tree(bad), bad_before)
        self.assertFalse(self.fs.nocow(os.path.join(bad, "README")))
        self.assertEqual(tree(good), good_before)
        self.assert_all_nocow(good)
        self.assert_no_leftovers("a-bad")
        self.assert_no_leftovers("b-good")
        self.assertIn(f"<3>{TAG}: a-bad: copy failed: cp: boom; left as it was\n", out)
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(self.read_attempts(), "1")

    def test_free_space(self) -> None:
        top = self.volume("cow")
        before = tree(top)
        self.schedule()
        # One byte short of the copy plus the 20 GiB margin: skipped.
        self.fs.free = DATA_BYTES + 20 * GIB - 1
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        self.assertIn(f"<4>{TAG}: cow: not enough free space for a 2 MiB copy; skipped\n", out)
        self.assertEqual(self.fs.commands("cp"), [])
        self.assertEqual(tree(top), before)
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(self.read_attempts(), "1")
        # Exactly enough: copied.
        self.fs.free = DATA_BYTES + 20 * GIB
        rc, out = self.call(nocow_cli.migrate)
        self.assert_all_nocow(top)
        self.assertFalse(os.path.exists(self.flag))

    def test_free_space_unknown_is_a_skip(self) -> None:
        self.volume("cow")
        self.schedule()
        self.fs.btrfs_out = ""
        rc, out = self.call(nocow_cli.migrate)
        self.assertIn(f"<4>{TAG}: cow: not enough free space for a 2 MiB copy; skipped\n", out)
        self.assertEqual(self.fs.commands("cp"), [])

    def test_gives_up_after_two_boots(self) -> None:
        top = self.volume("cow")
        before = tree(top)
        self.schedule()
        self.fs.cp = cp_fails
        msg = "Gave up after two boots; see journalctl -u docker-volumes-nocow."
        for boot, attempts in ((1, "1"), (2, "2")):
            with self.subTest(boot=boot):
                rc, out = self.call(nocow_cli.migrate)
                self.assertEqual(rc, 0)
                self.assertEqual(self.read_attempts(), attempts)
                self.assertTrue(os.path.exists(self.flag))
                self.assertNotIn(msg, out)
        # Third boot: no third try; the flag and the counter go, the user is told.
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(out, f"<3>{TAG}: {msg}\n")
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(self.read_attempts())
        self.assertEqual(len(self.fs.commands("cp")), 2)
        self.assertEqual(
            self.fs.commands(self.notify),
            [[self.notify, "-u", "normal", "-a", "btrfs-setup", "Docker volumes: NOCOW migration gave up", msg]],
        )
        self.assertEqual(tree(top), before)
        # Later boots: nothing scheduled, nothing tried, no second alert.
        self.assertEqual(self.call(nocow_cli.migrate), (0, ""))
        self.assertEqual(len(self.fs.commands("cp")), 2)
        self.assertEqual(len(self.fs.commands(self.notify)), 1)

    def test_attempts_counter_is_durable_before_the_rewrite_starts(self) -> None:
        # PRIM-02: the counter was written with open("w") and no fsync, then the long rewrite
        # started; a hang or hard reset before the next btrfs commit lost the increment, so
        # "gave up after two boots" could never trip.
        self.volume("cow")
        self.schedule()
        events: list[str] = []
        real_fsync = os.fsync
        folder = os.path.dirname(self.attempts)

        def fsync(fd: int) -> None:
            path = os.readlink(f"/proc/self/fd/{fd}")
            events.append("fsync counter folder" if path == folder else f"fsync {os.path.dirname(path)}")
            real_fsync(fd)

        def cp(src: str, dst: str) -> tuple[int, str]:
            events.append("cp")
            return cp_fails(src, dst)

        self.fs.cp = cp
        with mock.patch.object(os, "fsync", fsync):
            self.call(nocow_cli.migrate)
        self.assertEqual(self.read_attempts(), "1")
        first_cp = events.index("cp")
        self.assertIn(f"fsync {folder}", events[:first_cp], events)  # the counter's own bytes
        self.assertIn("fsync counter folder", events[:first_cp], events)  # its rename
        self.assertEqual(sorted(os.listdir(folder)), ["attempts"], "a temporary file was left")

    def test_mark_gives_two_boots_again(self) -> None:
        self.volume("cow")
        self.schedule()
        self.set_attempts("2")
        self.call(nocow_cli.mark)
        self.fs.cp = cp_fails
        self.call(nocow_cli.migrate)
        self.assertEqual(self.read_attempts(), "1")  # tried, not given up
        self.assertEqual(len(self.fs.commands("cp")), 1)

    def test_attempts_file_is_closed(self) -> None:
        # The counter is read and written inside `with open(...)`: no file is
        # left for the garbage collector to close (it warned "unclosed file").
        self.volume("cow")
        self.schedule()
        self.set_attempts("1")
        self.fs.cp = cp_fails
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            self.call(nocow_cli.migrate)
            gc.collect()
        self.assertEqual([str(w.message) for w in caught if issubclass(w.category, ResourceWarning)], [])
        self.assertEqual(self.read_attempts(), "2")

    def test_unreadable_counter_counts_as_zero(self) -> None:
        self.volume("cow")
        self.schedule()
        self.set_attempts("garbage")
        self.fs.cp = cp_fails
        self.call(nocow_cli.migrate)
        self.assertEqual(self.read_attempts(), "1")


class PostponeBase(Base):
    """A scheduled CoW volume, one attempt made, and a leftover of an
    interrupted copy (recover() would remove it): what a postponed boot must
    leave as it is."""

    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)
        self.top = self.volume("cow")
        # A leftover of an interrupted copy: recover() would remove it.
        os.mkdir(self.top + ".nocow-new")
        self.before = tree(os.path.join(self.vols, "cow"))
        self.schedule()
        self.set_attempts("1")

    def assert_migrated(self, out: str) -> None:
        self.assertIn(f"<5>{TAG}: all Docker volumes are NOCOW now\n", out)
        self.assert_all_nocow(self.top)
        self.assert_no_leftovers("cow")

    def assert_postponed(self, out: str, why: str) -> None:
        self.assertEqual(out, f"<4>{TAG}: {why}: migration left for the next boot\n")
        self.assertEqual(self.fs.sets, [])
        self.assertEqual(tree(os.path.join(self.vols, "cow")), self.before)  # recover() not run either
        self.assertTrue(os.path.exists(self.flag))
        self.assertEqual(self.read_attempts(), "1")


class DockerRunningTest(PostponeBase):
    """docker_busy(): the ActiveState of docker.service, failing closed."""

    def test_running_postpones_without_touching_anything(self) -> None:
        for state in ("active", "activating", "reloading", "deactivating"):
            with self.subTest(state=state):
                self.fs.docker_state = state
                self.fs.calls.clear()
                rc, out = self.call(nocow_cli.migrate)
                self.assertEqual(rc, 0)
                self.assertEqual(out, f"<4>{TAG}: Docker is running: migration left for the next boot\n")
                self.assertEqual(self.fs.calls, [["systemctl", "show", "-P", "ActiveState", "docker.service"]])
                self.assertEqual(self.fs.sets, [])
                self.assertEqual(tree(os.path.join(self.vols, "cow")), self.before)  # recover() not run either
                self.assertTrue(os.path.exists(self.flag))
                # Not counted as an attempt and no alert; the boot is
                # recorded instead (PostponedRecordTest).
                self.assertEqual(self.read_attempts(), "1")
                self.assertEqual(self.fs.commands(self.notify), [])

    def test_stopped_migrates(self) -> None:
        for state in ("inactive", "failed"):
            with self.subTest(state=state):
                self.fresh()
                self.fs.docker_state = state
                self.assert_migrated(self.call(nocow_cli.migrate)[1])

    def test_unknown_state_postpones(self) -> None:
        # Fails closed: only exit code 0 with "inactive" or "failed" lets the
        # migration run. systemctl failing (no manager on the bus) with empty
        # output, an unknown word, or a stopped state with a failing exit code
        # all postpone, with a line that is not "Docker is running".
        for rc, state, shown in (
            (1, "", "systemctl exit 1, ActiveState ''"),
            (0, "", "systemctl exit 0, ActiveState ''"),
            (0, "maintenance", "systemctl exit 0, ActiveState 'maintenance'"),
            (0, "refreshing", "systemctl exit 0, ActiveState 'refreshing'"),
            (1, "inactive", "systemctl exit 1, ActiveState 'inactive'"),
            (4, "active", "systemctl exit 4, ActiveState 'active'"),
        ):
            with self.subTest(rc=rc, state=state):
                self.fresh()
                self.fs.systemctl_rc, self.fs.docker_state = rc, state
                self.fs.systemctl_out_on_error = True
                rc_, out = self.call(nocow_cli.migrate)
                self.assertEqual(rc_, 0)
                self.assert_postponed(out, f"docker.service state unknown ({shown})")

    def test_no_systemctl_postpones(self) -> None:
        # subprocess.run raises FileNotFoundError: postponed like an unknown
        # state, nothing touched, attempts not counted.
        self.fs.systemctl_missing = True
        rc, out, _ = self.main("migrate")
        self.assertEqual(rc, 0)
        self.assert_postponed(
            out, "cannot ask systemd about docker.service ([Errno 2] No such file or directory: 'systemctl')"
        )


class DockerdTest(PostponeBase):
    """dockerd_busy(): a dockerd outside docker.service, in a fake /proc.

    Each process is PROC/PID with comm, cmdline, status (its uids), and the
    links exe, root (the / of its mount namespace) and cwd. root pointing at
    the test folder is a dockerd in our mount namespace (or in rootless
    Docker's, which keeps our /); pointing at another folder, one in a
    container."""

    def setUp(self) -> None:
        super().setUp()
        # Another mount namespace (a container): its own /var/lib/docker.
        self.other = os.path.join(self.tmp, "other-ns")
        os.makedirs(os.path.join(self.other, "var/lib/docker/volumes"))

    def process(
        self,
        pid: int,
        args: list[str],
        root: str | None = None,
        comm: str = "dockerd",
        exe: str | None = "/usr/bin/dockerd",
        cwd: str = "/",
        uid_line: str = "Uid:\t0\t0\t0\t0",
    ) -> str:
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d)
        with open(os.path.join(d, "comm"), "w") as f:
            f.write(comm + "\n")
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"".join(a.encode() + b"\0" for a in args))
        with open(os.path.join(d, "status"), "w") as f:
            f.write(f"Name:\t{comm}\nUmask:\t0022\nState:\tS (sleeping)\n{uid_line}\nGid:\t0\t0\t0\t0\n")
        if exe is not None:
            os.symlink(exe, os.path.join(d, "exe"))
        top = self.tmp if root is None else root
        os.symlink(top, os.path.join(d, "root"))
        os.symlink(os.path.join(top, cwd.lstrip("/")), os.path.join(d, "cwd"))
        return d

    def daemon_json(self, top: str, text: str, name: str = "daemon.json") -> None:
        os.makedirs(os.path.join(top, "etc/docker"), exist_ok=True)
        with open(os.path.join(top, "etc/docker", name), "w") as f:
            f.write(text)

    def decoy(self) -> None:
        """/etc/docker/daemon.json names /data/docker, a data root that is
        not ours: a dockerd whose flags were not read would look elsewhere."""
        os.makedirs(os.path.join(self.tmp, "data/docker/volumes"))
        self.daemon_json(self.tmp, '{"data-root": "/data/docker"}')

    def alias(self) -> str:
        """/srv/docker, another name for our data root, with the decoy: a
        case that postpones proves the path was read."""
        self.decoy()
        os.makedirs(os.path.join(self.tmp, "srv"))
        os.symlink("../var/lib/docker", os.path.join(self.tmp, "srv/docker"))
        return "/srv/docker"

    # ---- dockerd processes that use this data root -------------------------

    def ours_default(self) -> None:
        self.process(7, ["/usr/bin/dockerd"])

    def ours_data_root_equals(self) -> None:
        self.process(7, ["dockerd", f"--data-root={self.alias()}"])

    def ours_data_root_space(self) -> None:
        self.process(7, ["dockerd", "-H", "fd://", "--data-root", self.alias()])

    def ours_last_data_root_wins(self) -> None:
        self.process(7, ["dockerd", "--data-root=/data/docker", "--data-root", self.alias()])

    def ours_short_g(self) -> None:
        # -g and --graph: the old names of --data-root (removed in Docker 23).
        self.process(7, ["dockerd", "-g", self.alias()])

    def ours_short_g_attached(self) -> None:
        self.process(7, ["dockerd", f"-g{self.alias()}"])

    def ours_short_g_equals(self) -> None:
        self.process(7, ["dockerd", f"-g={self.alias()}"])

    def ours_graph(self) -> None:
        self.process(7, ["dockerd", "--graph", self.alias()])

    def ours_graph_equals(self) -> None:
        self.process(7, ["dockerd", f"--graph={self.alias()}"])

    def ours_relative(self) -> None:
        self.decoy()
        self.process(7, ["dockerd", "--data-root=docker"], cwd="/var/lib")

    def ours_daemon_json(self) -> None:
        self.daemon_json(self.tmp, f'{{"data-root": "{self.alias()}"}}')
        self.process(7, ["dockerd"])

    def ours_daemon_json_not_an_object(self) -> None:
        self.daemon_json(self.tmp, '["data-root"]')  # dockerd's default then
        self.process(7, ["dockerd"])

    def ours_config_file(self) -> None:
        # --config-file names ours; /etc/docker/daemon.json (the decoy) is not read then.
        self.daemon_json(self.tmp, f'{{"data-root": "{self.alias()}"}}', "custom.json")
        self.process(7, ["dockerd", "--config-file", "/etc/docker/custom.json"])

    def ours_config_file_equals(self) -> None:
        self.daemon_json(self.tmp, f'{{"data-root": "{self.alias()}"}}', "custom.json")
        self.process(7, ["dockerd", "--config-file=/etc/docker/custom.json"])

    def ours_by_program_file(self) -> None:
        # Renamed process, program file replaced by an upgrade.
        self.process(7, ["d"], comm="d", exe="/usr/bin/dockerd (deleted)")

    def ours_by_name(self) -> None:
        # Its program file cannot be read (exe missing): the name is enough.
        self.process(7, ["/opt/docker/bin/x"], exe=None)

    def ours_no_uid_line(self) -> None:
        # Whether it runs as root cannot be told: counted (fails closed).
        self.process(7, ["dockerd"], uid_line="")

    def ours_uid_garbled(self) -> None:
        self.process(7, ["dockerd"], uid_line="Uid:\t1000\tx\t1000\t1000")

    def ours_root_after_setuid(self) -> None:
        # The effective uid decides (the second field), not the real one.
        self.process(7, ["dockerd"], uid_line="Uid:\t1000\t0\t0\t0")

    def ours_bind_mounted(self) -> None:
        # A container that bind-mounts this data root (a link stands in for
        # the bind mount: the same directory, seen from another namespace).
        os.makedirs(os.path.join(self.other, "srv"))
        os.symlink("../../var/lib/docker", os.path.join(self.other, "srv/docker"))
        self.process(7, ["dockerd", "--data-root=/srv/docker"], root=self.other)

    def test_dockerd_on_this_data_root_postpones(self) -> None:
        for add in (
            self.ours_default,
            self.ours_data_root_equals,
            self.ours_data_root_space,
            self.ours_last_data_root_wins,
            self.ours_short_g,
            self.ours_short_g_attached,
            self.ours_short_g_equals,
            self.ours_graph,
            self.ours_graph_equals,
            self.ours_relative,
            self.ours_daemon_json,
            self.ours_daemon_json_not_an_object,
            self.ours_config_file,
            self.ours_config_file_equals,
            self.ours_by_program_file,
            self.ours_by_name,
            self.ours_no_uid_line,
            self.ours_uid_garbled,
            self.ours_root_after_setuid,
            self.ours_bind_mounted,
        ):
            with self.subTest(case=add.__name__):
                self.fresh()
                add()
                rc, out = self.call(nocow_cli.migrate)
                self.assertEqual(rc, 0)
                self.assert_postponed(out, f"dockerd (pid 7) uses {self.vols} outside docker.service")

    def test_unreadable_config_file_postpones(self) -> None:
        # Fails closed: a config file that exists but cannot be read may name
        # our data root (dockerd's default is not assumed).
        d = self.process(7, ["dockerd"])
        self.daemon_json(self.tmp, '{"data-root": "/data/docker"}')
        blocked = os.path.join(d, "root", "etc/docker/daemon.json")
        real_open = builtins.open

        def fake_open(path: str, mode: str = "r") -> object:
            if path == blocked:
                raise PermissionError(13, "Permission denied", path)
            return real_open(path, mode)

        with mock.patch.object(builtins, "open", fake_open):
            rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        why = f"dockerd (pid 7) is running and its data root cannot be read ([Errno 13] Permission denied: '{blocked}')"
        self.assert_postponed(out, why)

    def test_unreadable_data_root_postpones(self) -> None:
        # Fails closed: a dockerd whose data root cannot be read may be ours.
        d = self.process(7, ["dockerd"])
        real_stat = os.stat
        blocked = os.path.join(d, "root", "var/lib/docker/volumes")

        def stat(path: str, *a: object, **kw: object) -> os.stat_result:
            if path == blocked:
                raise PermissionError(13, "Permission denied", path)
            return real_stat(path)

        with mock.patch.object(os, "stat", stat):
            rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        why = f"dockerd (pid 7) is running and its data root cannot be read ([Errno 13] Permission denied: '{blocked}')"
        self.assert_postponed(out, why)

    # ---- processes that must not hold the migration back -------------------

    def other_rootless(self) -> None:
        # Rootless Docker as dockerd-rootless.sh starts it: plain `dockerd`
        # (its data root comes from $XDG_DATA_HOME or $HOME, not the command
        # line) as the user, in a namespace that keeps our / (so our
        # /var/lib/docker is what its default path shows). And the script
        # that starts it, whose name begins with "dockerd" (comm keeps 15
        # characters).
        os.makedirs(os.path.join(self.tmp, "home/u/.local/share/docker/volumes"))
        self.process(7, ["dockerd"], uid_line="Uid:\t1000\t1000\t1000\t1000")
        self.process(
            8,
            ["/bin/sh", "/usr/bin/dockerd-rootless.sh"],
            comm="dockerd-rootles",
            exe="/usr/bin/bash",
            uid_line="Uid:\t1000\t1000\t1000\t1000",
        )

    def other_user_dockerd_in_a_userns(self) -> None:
        # The same, as root in its own user namespace that maps to uid
        # 100000 here (status shows the uid as seen from this namespace).
        self.process(7, ["dockerd", "--data-root=/var/lib/docker"], uid_line="Uid:\t100000\t100000\t100000\t100000")

    def other_config_file(self) -> None:
        # /etc/docker/daemon.json names our data root, but --config-file another.
        self.daemon_json(self.tmp, f'{{"data-root": "{self.alias()}"}}')
        self.daemon_json(self.tmp, '{"data-root": "/data/docker"}', "custom.json")
        self.process(7, ["dockerd", "--config-file", "/etc/docker/custom.json"])

    def other_odd_program_name(self) -> None:
        # A program file name that is not valid UTF-8 (read as bytes, not decoded).
        self.process(7, ["x"], comm="caf", exe=os.fsdecode(b"/opt/x/caf\xe9"))

    def other_containerised(self) -> None:
        # In a container: the /var/lib/docker of its own mount namespace.
        self.process(7, ["dockerd"], root=self.other)

    def other_containerised_daemon_json(self) -> None:
        # Its own daemon.json is read, not ours (which names our data root).
        self.daemon_json(self.tmp, '{"data-root": "/var/lib/docker"}')
        self.daemon_json(self.other, '{"data-root": "/data/docker"}')
        os.makedirs(os.path.join(self.other, "data/docker/volumes"))
        self.process(7, ["dockerd"], root=self.other)

    def other_data_root(self) -> None:
        # Our namespace, another data root, with no volumes folder yet.
        self.process(7, ["dockerd", "--data-root=/srv/docker"])

    def other_not_dockerd(self) -> None:
        self.process(7, ["sleep", "--data-root=/var/lib/docker"], comm="sleep", exe=None)

    def other_ended(self) -> None:
        # Ended while the helper looked: only its empty folder is left.
        os.makedirs(os.path.join(self.proc, "7"))

    def other_not_processes(self) -> None:
        os.makedirs(os.path.join(self.proc, "sys/kernel"))
        os.symlink("8", os.path.join(self.proc, "self"))
        self.process(
            8,
            ["/usr/bin/python3", "-I", "/usr/local/sbin/docker-volumes-nocow", "migrate"],
            comm="python3",
            exe="/usr/bin/python3.14",
        )

    def test_other_dockerd_does_not_postpone(self) -> None:
        for add in (
            self.other_rootless,
            self.other_user_dockerd_in_a_userns,
            self.other_config_file,
            self.other_odd_program_name,
            self.other_containerised,
            self.other_containerised_daemon_json,
            self.other_data_root,
            self.other_not_dockerd,
            self.other_ended,
            self.other_not_processes,
        ):
            with self.subTest(case=add.__name__):
                self.fresh()
                add()
                rc, out = self.call(nocow_cli.migrate)
                self.assertEqual(rc, 0)
                self.assert_migrated(out)


class PostponedRecordTest(PostponeBase):
    """POSTPONED: each boot at which the migration had to wait, for status
    and `cst doctor` (a postponed boot counts no attempt and sends no alert)."""

    def boot(self, boot_id: str, at: int) -> tuple[int | None, str]:
        """migrate() during boot boot_id, at time at, with Docker running."""
        os.makedirs(os.path.join(self.proc, "sys/kernel/random"), exist_ok=True)
        with open(os.path.join(self.proc, "sys/kernel/random/boot_id"), "w") as f:
            f.write(boot_id + "\n")
        with mock.patch.object(time, "time", lambda: at + 0.5):
            return self.call(nocow_cli.migrate)

    def record(self) -> dict[str, object] | None:
        try:
            with open(self.postponed) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def test_counts_boots_not_runs(self) -> None:
        self.fs.docker_state = "active"
        self.boot("b1", 1000)
        self.assertEqual(
            self.record(),
            {"reason": "Docker is running", "boots": 1, "first": 1000, "last": 1000, "boot_id": "b1"},
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.postponed).st_mode), 0o644)  # cst doctor reads it as the user
        # Run again in the same boot, for another reason: still one boot.
        self.fs.docker_state = "maintenance"
        self.boot("b1", 1100)
        rec = self.record()
        assert rec is not None
        self.assertEqual((rec["boots"], rec["first"], rec["last"]), (1, 1000, 1100))
        self.assertEqual(rec["reason"], "docker.service state unknown (systemctl exit 0, ActiveState 'maintenance')")
        # The next boots.
        self.fs.docker_state = "active"
        self.boot("b2", 2000)
        self.boot("b3", 3000)
        self.assertEqual(
            self.record(),
            {"reason": "Docker is running", "boots": 3, "first": 1000, "last": 3000, "boot_id": "b3"},
        )
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.postponed))), ["attempts", "postponed"])  # no .new
        self.assertEqual(self.read_attempts(), "1")
        self.assertEqual(self.fs.commands(self.notify), [])
        # status shows it.
        rc, out = self.call(nocow_cli.status)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(3000))
        self.assertIn(
            "  1 with copy-on-write data (2 MiB), migration at the next boot\n"
            f"  migration postponed at 3 boot(s) in a row, last {when}: Docker is running\n",
            out,
        )
        # A boot that is not held back removes the record.
        self.fs.docker_state = "inactive"
        rc, out = self.boot("b4", 4000)
        self.assert_migrated(out)
        self.assertIsNone(self.record())
        self.assertNotIn("postponed", self.call(nocow_cli.status)[1])

    def test_recorded_when_the_folder_has_no_nocow_flag(self) -> None:
        # Nothing scheduled, but the volumes folder lost +C (Docker installed
        # after install): that is work the boot could not do either.
        os.remove(self.flag)
        self.fs.set(self.vols, False)
        self.fs.docker_state = "active"
        self.boot("b1", 1000)
        rec = self.record()
        assert rec is not None
        self.assertEqual(rec["boots"], 1)
        self.assertEqual(self.fs.sets, [])

    def test_nothing_to_do_records_nothing_and_drops_an_old_record(self) -> None:
        os.remove(self.flag)  # nothing scheduled, the folder has +C
        self.fs.docker_state = "active"
        os.makedirs(os.path.dirname(self.postponed), exist_ok=True)
        with open(self.postponed, "w") as f:
            f.write('{"reason": "Docker is running", "boots": 4, "first": 1, "last": 2, "boot_id": "old"}\n')
        rc, out = self.boot("b1", 1000)
        self.assertEqual(out, f"<4>{TAG}: Docker is running: migration left for the next boot\n")
        self.assertIsNone(self.record())

    def test_volumes_folder_gone_drops_the_record(self) -> None:
        # Docker removed, or data-root moved: no volumes, nothing waits. The
        # old record must not keep `cst doctor` warning (status hides it).
        self.fs.docker_state = "active"
        self.boot("b1", 1000)
        self.assertIsNotNone(self.record())
        shutil.rmtree(self.vols)
        self.assertEqual(self.boot("b2", 2000), (0, ""))
        self.assertIsNone(self.record())

    def test_a_damaged_record_starts_afresh(self) -> None:
        self.fs.docker_state = "active"
        for text in ("not json", "[1, 2]", '{"boots": "3", "first": 1}', '{"boots": 2, "first": true}', ""):
            with self.subTest(text=text):
                os.makedirs(os.path.dirname(self.postponed), exist_ok=True)
                with open(self.postponed, "w") as f:
                    f.write(text)
                self.boot("b1", 1000)
                rec = self.record()
                assert rec is not None
                self.assertEqual((rec["boots"], rec["first"]), (1, 1000))
                self.assertIn("postponed at 1 boot(s) in a row", self.call(nocow_cli.status)[1])

    def test_without_a_boot_id(self) -> None:
        # No boot id to read: the first postponement is still recorded.
        self.fs.docker_state = "active"
        with mock.patch.object(time, "time", lambda: 1000.0):
            self.call(nocow_cli.migrate)
        rec = self.record()
        assert rec is not None
        self.assertEqual((rec["boots"], rec["boot_id"]), (1, ""))

    def test_unmark_removes_the_record(self) -> None:
        self.fs.docker_state = "active"
        self.boot("b1", 1000)
        self.assertIsNotNone(self.record())
        self.call(nocow_cli.unmark)
        self.assertIsNone(self.record())
        self.assertEqual(self.call(nocow_cli.unmark), (0, ""))  # and again, with nothing to remove


class RecoverTest(Base):
    """A crash at each step of migrate_one(), then the next boot."""

    def setUp(self) -> None:
        super().setUp()
        self.fs.set(self.vols, True)
        self.top = self.volume("cow")
        self.vol = os.path.join(self.vols, "cow")
        self.before = tree(self.top)
        self.ino = ino(self.top)
        self.schedule()

    def second_rename_raises(self, exc: BaseException) -> contextlib.AbstractContextManager[object]:
        """rename(new, data), the second rename of the swap, raises exc; the others run."""
        real_rename = os.rename

        def rename(src: str, dst: str) -> None:
            if src.endswith(".nocow-new"):
                raise exc
            real_rename(src, dst)

        return mock.patch.object(os, "rename", rename)

    def crash(self) -> None:
        with self.assertRaises(Crash):
            self.call(nocow_cli.migrate)
        self.assertEqual(self.read_attempts(), "1")
        self.assertTrue(os.path.exists(self.flag))

    def next_boot_finishes(self, restored: bool) -> None:
        rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(f"<4>{TAG}: cow: restored the original after an interrupted migration\n" in out, restored, out)
        self.assertEqual(tree(self.top), self.before)
        self.assert_all_nocow(self.top)
        self.assert_no_leftovers("cow")
        self.assertFalse(os.path.exists(self.flag))
        self.assertIsNone(self.read_attempts())

    def test_crash_during_the_copy(self) -> None:
        self.fs.cp = lambda src, dst: (self.fs.copy_into(src, dst, stop_after=4), "")
        self.crash()
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data", "_data.nocow-new"])
        self.assertEqual(ino(self.top), self.ino)
        self.assertEqual(tree(self.top), self.before)
        self.fs.cp = self.fs.copy
        self.next_boot_finishes(restored=False)

    def test_crash_between_the_renames(self) -> None:
        with self.second_rename_raises(Crash("power loss between the two renames")):
            self.crash()
        self.assertEqual(self.fs.syncs, 1)  # the copy was on disk before the first rename
        names = sorted(os.listdir(self.vol))
        self.assertEqual(len(names), 2, names)
        self.assertRegex(names[0], r"^_data\.cow-old-\d+$")
        self.assertEqual(names[1], "_data.nocow-new")
        old = os.path.join(self.vol, names[0])
        self.assertEqual(ino(old), self.ino)
        # recover() alone: the original back under its name, the copy gone.
        rc, out = self.call(nocow_adapters.recover)
        self.assertEqual(out, f"<4>{TAG}: cow: restored the original after an interrupted migration\n")
        self.assertEqual(ino(self.top), self.ino)
        self.assertEqual(tree(self.top), self.before)
        self.assert_no_leftovers("cow")
        self.assertFalse(self.fs.nocow(os.path.join(self.top, "README")))
        # The same state again, through the next boot: restored, then migrated.
        os.rename(self.top, old)
        os.mkdir(os.path.join(self.vol, "_data.nocow-new"))
        self.next_boot_finishes(restored=True)

    def test_crash_before_the_old_copy_is_deleted(self) -> None:
        real_rmtree = shutil.rmtree

        def rmtree(path: str, *a: object, **kw: object) -> None:
            if ".cow-old-" in path:
                os.remove(os.path.join(path, "README"))  # partly deleted
                raise Crash("power loss while deleting the old copy")
            real_rmtree(path)

        with mock.patch.object(shutil, "rmtree", rmtree):
            self.crash()
        names = sorted(os.listdir(self.vol))
        self.assertEqual(names[0], "_data")
        self.assertRegex(names[1], r"^_data\.cow-old-\d+$")
        self.assertEqual(len(names), 2, names)
        self.assert_all_nocow(self.top)  # the new copy is in place already
        self.next_boot_finishes(restored=False)

    def test_failed_second_rename_puts_the_original_back(self) -> None:
        # rename(new, data) fails with an OSError: the original goes back at
        # once; the copy stays until the next boot's recover() removes it.
        with self.second_rename_raises(OSError(16, "Device or resource busy")):
            rc, out = self.call(nocow_cli.migrate)
        self.assertEqual(rc, 0)
        self.assertEqual(self.fs.syncs, 1)  # before the first rename
        self.assertIn(f"<3>{TAG}: cow: [Errno 16] Device or resource busy; left as it was\n", out)
        self.assertEqual(ino(self.top), self.ino)
        self.assertEqual(tree(self.top), self.before)
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data", "_data.nocow-new"])
        self.assertEqual(self.read_attempts(), "1")
        self.next_boot_finishes(restored=False)

    def test_recover_cases(self) -> None:
        self.call(nocow_adapters.recover)  # nothing to do: nothing changes
        self.assert_no_leftovers("cow")
        # _data there: every old copy and the new one go.
        for x in ("_data.cow-old-1700000000", "_data.cow-old-1700000100", "_data.nocow-new"):
            os.makedirs(os.path.join(self.vol, x, "sub"))
        rc, out = self.call(nocow_adapters.recover)
        self.assertEqual(out, "")  # removed without a log line
        self.assert_no_leftovers("cow")
        self.assertEqual(ino(self.top), self.ino)
        # _data missing, two old copies: the newest (by name) comes back.
        os.rename(self.top, os.path.join(self.vol, "_data.cow-old-1700000100"))
        os.makedirs(os.path.join(self.vol, "_data.cow-old-1700000000"))
        self.call(nocow_adapters.recover)
        self.assertEqual(ino(self.top), self.ino)
        self.assert_no_leftovers("cow")
        # _data missing and only the new copy: kept, nothing is deleted.
        os.rename(self.top, os.path.join(self.vol, "_data.nocow-new"))
        self.assertEqual(self.call(nocow_adapters.recover), (None, ""))
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data.nocow-new"])

    def test_recover_leaves_a_data_that_is_not_a_folder(self) -> None:
        # Something named _data that is not a folder (here a dangling link):
        # recover() neither puts the old copy over it nor deletes the copies.
        os.rename(self.top, os.path.join(self.vol, "_data.cow-old-1700000000"))
        os.symlink("gone", self.top)
        os.mkdir(os.path.join(self.vol, "_data.nocow-new"))
        self.assertEqual(self.call(nocow_adapters.recover), (None, ""))
        self.assertEqual(os.readlink(self.top), "gone")
        self.assertEqual(sorted(os.listdir(self.vol)), ["_data", "_data.cow-old-1700000000", "_data.nocow-new"])
        self.assertEqual(ino(os.path.join(self.vol, "_data.cow-old-1700000000")), self.ino)

    def test_recover_skips_files_and_links(self) -> None:
        # metadata.db (a file) and a symlinked volume folder are not volumes:
        # what the link points to keeps its look-alike leftovers.
        elsewhere = os.path.join(self.tmp, "elsewhere")
        names = ["_data", "_data.cow-old-1700000000", "_data.nocow-new"]
        for x in names:
            os.makedirs(os.path.join(elsewhere, x))
        os.symlink(elsewhere, os.path.join(self.vols, "link"))
        os.mkdir(os.path.join(self.vol, "_data.nocow-new"))
        self.assertEqual(self.call(nocow_adapters.recover), (None, ""))
        self.assertTrue(os.path.isfile(os.path.join(self.vols, "metadata.db")))
        self.assertTrue(os.path.islink(os.path.join(self.vols, "link")))
        self.assertEqual(sorted(os.listdir(elsewhere)), names)
        self.assert_no_leftovers("cow")  # the real volume is still cleaned


if __name__ == "__main__":
    unittest.main()

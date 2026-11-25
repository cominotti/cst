# SPDX-License-Identifier: MPL-2.0
"""Fakes that several of btrfs's test modules share, one section per module they stand in for.

- any module: read and write, a text file of a test's folder (read: None for one that is not there);
- docker-volumes-nocow (test_docker_volumes_nocow and its fault tests): FakeSystem, the inode-flag
  ioctls, getxattr, the commands, os.sync and the fake cp; Crash, which its copy_into raises; the
  flag bits NOCOW and OTHER; entries, carries_flags and tree, to walk and compare a folder;
- systemd (scrub-limit's and iocost's fakes): SystemdState, what systemd has loaded of one unit
  (its files' text at the last reload that worked, whether they changed since) and whether the unit
  is enabled and active; one model, not a copy per package;
- scrub-limit (test_scrub_limit, test_scrub_limit_ports): ScrubFakeRunner, the installer and
  systemctl; RecordingInstallerRunner, the installer's text, remove-text and mark through the real
  InstallService and one install record; SystemdModel, the same and what systemd has loaded of the
  drop-in; core_text, a generated text of btrfs:core; (its fault tests, tests/mbt/mbt_scrub_rollback
  and test_scrub_limit_ports) ScrubWorld and FakeScrubSettings, FakeScrubDropin, FakeScrubSystemd and
  FakeScrubSysfs over it, the in-memory fakes of btrfs/scrub_limit/ports.py;
- iocost (its fault tests, test_iocost_ports and tests/mbt/mbt_iocost_apply): IocostWorld and
  FakeQosControl, FakeManagedDevices, FakeIocostSysfs, FakeUdev, FakeIocostSystemd,
  FakeIocostInstaller, FakeIocostUninstall and FakeIocostLock over it, the in-memory fakes of
  btrfs/iocost/ports.py; IOCOST_ENTRIES, btrfs:iocost's manifest entries;
- cache (test_cache, its fault tests, test_cache_ports and tests/mbt/mbt_cache_rollback): MountTable, the
  mounts and the kernel's rules for them (a bind's device and FSROOT, umount of the top mount, findmnt's
  JSON); FstabSystemdView, what systemd has loaded of /etc/fstab, and mount_units_listing, its answer to
  `systemctl show '*.mount'`; FakeMountRunner, findmnt, mount, umount and systemctl over them and a test
  root's /etc/fstab; CacheTestCtx, the host or toolbox the command runs in; the UUIDs the mounts carry;
  CacheWorld and FakeFstab, FakeCacheMountTable, FakeCacheSystemd, FakeCacheFolders, FakeCacheHostView and
  FakeCacheAccounts over it, the in-memory fakes of btrfs/cache/ports.py (over the same MountTable and
  FstabSystemdView).

Not collected as a test module (unittest finds test*.py).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
import contextlib
import errno
import json
import os
from pathlib import Path
import posixpath
import shutil
import stat
import struct
import subprocess
import sys
from typing import Final, override

from btrfs.cache.application import CacheMounts
from btrfs.cache.domain import (
    FINDMNT_COLUMNS,
    MIN_HUMAN_UID,
    CacheMountBinding,
    CacheMountUser,
    LiveMount,
    parse_findmnt_mounts,
)
from btrfs.cache.domain.systemd_view import MOUNT_UNIT_PROPS
from btrfs.cache.ports import BindingPaths
from btrfs.iocost.application import Iocost
from btrfs.iocost.domain import (
    IOCOST_COMPONENT_LOCK_NAME,
    IOCOST_LOCK,
    IOCOST_MANAGED_FILE,
    IOCOST_QOS_PATH,
    IOCOST_SERVICE,
    USEC_INITIALIZED_PROP,
    BlockDisk,
    UdevProperties,
)
from btrfs.scrub_limit.application import ScrubLimit
from btrfs.scrub_limit.domain import SCRUB_CONF_PATH, SCRUB_DROPIN_PATH
from btrfs.snapshot.domain import CACHE_TAG
from cst_core.adapters import nocow as core_nocow
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget
from cst_core.application.install import InstallRefused, InstallRequest, InstallService, Placement, record_file
from cst_core.domain.install import (
    INSTALL_MANIFEST,
    ArtifactKind,
    Component,
    GeneratedKind,
    InstallReportWord,
    Owner,
    select_entries,
)
from cst_core.domain.record import ComponentState
from cst_core.domain.units import parse_show_units
from cst_core.installer import NAME as INSTALLER_NAME
from cst_core.ports import CommandFault, CommandResult, WriteFailed
from tests.fault_injection import FaultPlan
from tests.test_install import MemoryTarget
from tests.test_install_record import MemoryRecord

# ---- files ---------------------------------------------------------------


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# ---- docker-volumes-nocow ------------------------------------------------

NOCOW = 0x00800000  # FS_NOCOW_FL, what chattr +C sets
OTHER = 0x00000080  # FS_NOATIME_FL: another attribute the helper must leave alone


class Crash(BaseException):
    """The process dies at this point (kill, timeout, power loss)."""


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


def tree(top: str) -> dict[str, tuple[object, ...]]:
    """Everything below top (and top's own mode), as comparable values."""
    out: dict[str, tuple[object, ...]] = {".": ("dir", stat.S_IMODE(os.lstat(top).st_mode))}
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
        self.free = 1 << 50
        self.btrfs_out: str | None = None  # None: a normal `btrfs filesystem usage -b` answer
        self.chcon_rc = 0
        self.syncs = 0
        self.cp: Callable[[str, str], tuple[int, str]] = self.copy
        self.notify = ""  # btrfs-notify's path in the test folder
        self.missing: set[str] = set()  # programs that are not installed

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

    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        """CommandRunnerPort.run: each command the helper may run, answered by the fake."""
        args = list(argv)
        self.calls.append(args)
        not_found = self.not_found(args, kw)
        if not_found is not None:
            return not_found

        def done(rc: int = 0, out: str = "", err: str = "") -> CommandResult:
            return CommandResult(rc, out, err, argv=tuple(args))

        if args[0] == "systemctl":
            if args != ["systemctl", "show", "-P", "ActiveState", "docker.service"]:
                raise AssertionError(f"unexpected systemctl call: {args}")
            answers = self.systemctl_rc == 0 or self.systemctl_out_on_error
            return done(self.systemctl_rc, self.docker_state + "\n" if answers else "")
        if args[0] == "chcon":
            if args[1:2] != ["--reference"] or len(args) != 4:
                raise AssertionError(f"unexpected chcon call: {args}")
            if self.chcon_rc == 0:
                self.copy_label(args[2], args[3])
                return done()
            return done(self.chcon_rc, "", f"chcon: failed to change context of '{args[3]}': Operation not permitted\n")
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
        if args[0] == self.notify:
            return done()
        raise AssertionError(f"unexpected command: {args}")

    def not_found(self, args: list[str], kw: Mapping[str, object]) -> CommandResult | None:
        """Checks the options of a command, and answers for one that is missing (None: it is installed).

        stdin ours; systemctl's answer decoded strictly, cp's, chcon's and btrfs's leniently
        (U+FFFD); btrfs-notify's output was never decoded."""
        want = (
            {"timeout": 60, "new_session": True}
            if args[0] == self.notify
            else {"stdin": "inherit", "decode_errors": "strict" if args[0] == "systemctl" else "replace"}
        )
        if kw != want:
            raise AssertionError(f"{args[0]} run with {kw}, not {want}")
        if args[0] not in self.missing:
            return None
        missing = FileNotFoundError(2, "No such file or directory", args[0])
        return CommandResult(127, "", str(missing), argv=tuple(args), fault=CommandFault.MISSING, cause=missing)

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


# ---- systemd (scrub-limit and iocost) -------------------------------------


class SystemdState:
    """What systemd has of one unit, for the fakes of every package that reloads it: the text of its
    files at the last daemon-reload that worked (loaded), whether a write or a removal of them came since
    (dirty: what NeedDaemonReload follows), and whether the unit is enabled and active."""

    def __init__(self, *, loaded: str | None = None, dirty: bool = False) -> None:
        self.loaded = loaded
        self.dirty = dirty
        self.enabled = False
        self.active = False

    def changed(self) -> None:
        """A write or a removal of the unit's files: systemd is behind until the next reload."""
        self.dirty = True

    def reloaded(self, text: str | None) -> None:
        """A daemon-reload that worked: systemd has text, as the files are now."""
        self.loaded, self.dirty = text, False


# ---- scrub-limit ---------------------------------------------------------


class ScrubFakeRunner:
    def __init__(self, *, installer_root: str = "") -> None:
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []
        self._installer_root = installer_root
        # a part of the command line ("systemctl daemon-reload", "installer.py text") whose calls exit 1
        self.fail_on: set[str] = set()
        # the value `systemctl show -p NeedDaemonReload UNIT` prints after "NeedDaemonReload=" ("": none)
        self.need_daemon_reload = ""

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        **_kw: object,
    ) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        self.inputs.append(input_text)
        if any(part in " ".join(cmd) for part in self.fail_on):
            return CommandResult(1, "", "Access denied", argv=tuple(cmd))
        if "installer.py" in " ".join(cmd):
            return self._installer(cmd, input_text)
        if cmd[:4] == ["systemctl", "show", "-p", "NeedDaemonReload"]:
            return CommandResult(0, f"NeedDaemonReload={self.need_daemon_reload}", "", argv=tuple(cmd))
        return CommandResult(0, "", "", argv=tuple(cmd))

    def _installer(self, cmd: list[str], input_text: str | None) -> CommandResult:
        if self._installer_root:
            real = subprocess.run(
                [sys.executable, "-I", "-B", cmd[3], cmd[4], "--root", self._installer_root, *cmd[5:]],
                input=input_text,
                capture_output=True,
                text=True,
                stdin=None if input_text is not None else subprocess.DEVNULL,
                check=False,
            )
            return CommandResult(real.returncode, real.stdout, real.stderr, argv=tuple(cmd))
        if "text" in cmd:
            dest = cmd[cmd.index("text") + 1]
            existed = os.path.exists(dest)
            old = None
            if existed:
                with open(dest, encoding="utf-8") as fh:
                    old = fh.read()
            if old == input_text:
                return CommandResult(0, f"unchanged: {dest}\n", "", argv=tuple(cmd))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write(input_text or "")
            return CommandResult(0, f"written:   {dest}\n", "", argv=tuple(cmd))
        if "remove-text" in cmd:
            dest = cmd[cmd.index("remove-text") + 1]
            if os.path.exists(dest):
                os.remove(dest)
                return CommandResult(0, f"removed:   {dest}\n", "", argv=tuple(cmd))
            return CommandResult(0, "", "", argv=tuple(cmd))
        return CommandResult(0, "", "", argv=tuple(cmd))


def core_text(path: str, record: str) -> Placement:
    """A generated text of btrfs:core at path, listed in record (as installer.py text --component writes it)."""
    return Placement(
        path=path,
        dest=path,
        record=record,
        component=Component.BTRFS_CORE.value,
        src=None,
        kind=GeneratedKind.TEXT,
        owner=Owner.ROOT,
        mode=0o644,
    )


class RecordingInstallerRunner(ScrubFakeRunner):
    """installer.py text and remove-text as the installer runs them as root: the files written and
    removed, and their lines and btrfs:core's state in one install record (a write sets it pending,
    unless --keep-state keeps the state the record has). scrub-limit runs no other installer command: any
    other (a mark of btrfs:core, a text-plan, a forget-text) fails the test."""

    def __init__(self, record: str) -> None:
        super().__init__()
        self.record = record
        self.svc = InstallService(LocalInstallTarget(), lambda _e: b"", record=JsonInstallRecord())

    @override
    def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
        cmd = list(argv)
        joined = " ".join(cmd)
        if "installer.py" not in joined or any(part in joined for part in self.fail_on):
            return super().run(argv, input_text=input_text, **kw)
        self.calls.append(cmd)
        word = cmd[4]
        if word == "mark":
            raise AssertionError(f"scrub-limit ran the installer's mark: {cmd}")
        place = core_text(cmd[5], self.record)
        if word == "text":
            os.makedirs(os.path.dirname(place.path), exist_ok=True)
            data = (input_text or "").encode()
            written = self.svc.put(place, data, as_root=False, keep_state="--keep-state" in cmd)
            word_out = InstallReportWord.WRITTEN if written else InstallReportWord.UNCHANGED
            return CommandResult(0, word_out.line(place.path) + "\n", "", argv=tuple(cmd))
        if word == "remove-text":
            return CommandResult(0, "".join(f"{line}\n" for line in self.svc.remove([place])), "", argv=tuple(cmd))
        raise AssertionError(f"scrub-limit ran an installer command this fake does not model: {cmd}")


class SystemdModel(RecordingInstallerRunner):
    """The installer (its writes and their record through the real InstallService) and systemctl, and
    what systemd has loaded of the drop-in."""

    def __init__(self, dropin: str, record: str, *, loaded: str | None = None, behind: bool = False) -> None:
        super().__init__(record)
        self.dropin = dropin
        # dirty: a write or a removal of the drop-in since the last reload that worked
        self.systemd = SystemdState(loaded=loaded, dirty=behind)

    @property
    def loaded(self) -> str | None:
        """The drop-in's text at the last daemon-reload that worked."""
        return self.systemd.loaded

    @property
    def dirty(self) -> bool:
        """A write or a removal of the drop-in since the last reload that worked."""
        return self.systemd.dirty

    @override
    def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
        cmd = list(argv)
        self.need_daemon_reload = "yes\n" if self.dirty else "no\n"
        r = super().run(argv, input_text=input_text, **kw)
        if "installer.py" in " ".join(cmd) and r.stdout.startswith(("written:", "removed:")):
            self.systemd.changed()
        if cmd == ["systemctl", "daemon-reload"] and r.returncode == 0:
            self.systemd.reloaded(read(self.dropin))
        return r


class FaultPoints:
    """Every mutating call of a world's port fakes as one named point: recorded in calls, then run through
    plan (tests/fault_injection.py) and failed by fail() when one is armed there; answered lists the
    points where an armed fail() answered (so a test can tell a fault that fired from one that did not)."""

    def __init__(self) -> None:
        self.calls: list[str] = []  # every mutating call, by its point name
        self.plan = FaultPlan()
        self.failing: dict[str, tuple[OSError, int | None]] = {}
        self.answered: list[str] = []

    def fail(self, point: str, err: OSError, *, times: int | None = None) -> None:
        """The next times calls of point fail with err (None: every one)."""
        self.failing[point] = (err, times)

    def step[T](self, point: str, effect: Callable[[], T], failed: Callable[[OSError], T] | None = None) -> T:
        """One mutating call: recorded, then run through plan, its effect failed by fail() when one is armed
        there (so plan records it too, and a process the plan has killed runs it no more)."""
        self.calls.append(point)
        err, left = self.failing.get(point, (None, 0))
        if err is None or left == 0:
            return self.plan.step(point, effect, failed)
        armed = err

        def fails() -> T:
            self.failing[point] = (armed, None if left is None else left - 1)
            self.answered.append(point)
            if failed is None:
                raise armed
            return failed(armed)

        return self.plan.step(point, fails, failed)


class ScrubWorld(FaultPoints):
    """What scrub-limit's ports see, in memory, for the use cases of btrfs/scrub_limit/application.py:
    snapshots.conf and its folder, the drop-in (in an in-memory install target, written and removed
    through the real InstallService with an in-memory install record, as installer.py text --keep-state and
    remove-text do) and its folder, and what systemd has loaded of it.

    loaded is the drop-in's text at the last daemon-reload that worked; dirty, a write or a removal of the
    drop-in since (NeedDaemonReload follows writes and the list of drop-ins, not their text, as
    SystemdModel models it). Every mutating call of a port is one named point (`calls`), through plan
    (tests/fault_injection.py) and through fail(): snapshots.conf's write is three, "settings folder" (its
    folder made, on every write), "settings replace" (the text in place) and "settings sync" (the folder's
    sync after the rename: a failure there leaves the new text in place); "settings remove", "settings
    rmdir", "drop-in text", "drop-in remove-text", "drop-in rmdir" and "systemctl daemon-reload". A failed
    rmdir is no error (the port's contract; as os.rmdir, one of a folder that is not there or not empty
    raises inside the call, before a kill armed there fires), a failed installer call or daemon-reload is an
    exit 1 with the error on stderr, a failed settings call raises it."""

    def __init__(
        self,
        *,
        conf_path: str = SCRUB_CONF_PATH,
        dropin_path: str = SCRUB_DROPIN_PATH,
        record: str = "/var/lib/cst/installed.json",
    ) -> None:
        super().__init__()
        self.conf_path = conf_path
        self.dropin_path = dropin_path
        self.record = record
        self.conf: str | None = None
        self.conf_folder = False
        self.conf_unreadable = False
        self.dropin_folder = False
        self.dropin_unreadable = False
        self.systemd = SystemdState()  # what systemd has loaded of the drop-in (loaded, dirty)
        self.show_fails = False  # systemctl cannot say whether systemd loaded the files
        self.live: tuple[str, ...] = ()  # sysfs's lines (ScrubSysfsPort)
        self.reload_timeouts: list[float | None] = []  # the time limit of each daemon-reload asked for
        self.built_for: list[bool] = []  # the read_only of each use case build() gave
        self.target = MemoryTarget()
        self.records = MemoryRecord()
        self.svc = InstallService(self.target, lambda _e: b"", record=self.records)

    @property
    def loaded(self) -> str | None:
        """The drop-in's text at the last daemon-reload that worked."""
        return self.systemd.loaded

    @loaded.setter
    def loaded(self, text: str | None) -> None:
        self.systemd.loaded = text

    @property
    def dirty(self) -> bool:
        """A write or a removal of the drop-in since the last reload that worked."""
        return self.systemd.dirty

    @dirty.setter
    def dirty(self, behind: bool) -> None:
        self.systemd.dirty = behind

    @property
    def dropin(self) -> str | None:
        """The drop-in's text (None: not there)."""
        held = self.target.files.get(self.dropin_path)
        return None if held is None else held[0].decode()

    def put_conf(self, text: str | None) -> None:
        """snapshots.conf as a test's start state (None: not there, its folder neither)."""
        self.conf, self.conf_folder = text, text is not None

    def put_dropin(self, text: str | None) -> None:
        """The drop-in on disk as a test's start state, not recorded and not loaded (None: not there)."""
        if text is None:
            self.target.files.pop(self.dropin_path, None)
        else:
            self.target.files[self.dropin_path] = (text.encode(), 0o644)
        self.dropin_folder = text is not None

    def core_state(self) -> ComponentState | None:
        """btrfs:core's state in the install record (None: no record, or none for it)."""
        rec = self.records.read(self.record)
        return None if rec is None else rec.components.get(Component.BTRFS_CORE.value)

    def core_has_a_file(self) -> bool:
        rec = self.records.read(self.record)
        return rec is not None and any(f.component == Component.BTRFS_CORE.value for f in rec.files.values())

    def install_core(self, *, done: bool) -> None:
        """btrfs:core as an install leaves it: a seed text and the drop-in (when there) recorded, then done
        (or still pending)."""
        seed = posixpath.join(posixpath.dirname(self.record), "core-seed")
        self.svc.put(core_text(seed, self.record), b"seed\n", as_root=False)
        if self.dropin is not None:
            self.svc.put(core_text(self.dropin_path, self.record), self.dropin.encode(), as_root=False)
        if done:
            self.svc.mark(self.record, Component.BTRFS_CORE.value)

    def build(self) -> Callable[[bool], ScrubLimit]:
        """What run_scrub_limit_cli's build takes: this world's use cases, whatever read_only it is asked
        for, which built_for records."""
        return lambda read_only: self._built(read_only=read_only)

    def _built(self, *, read_only: bool) -> ScrubLimit:
        self.built_for.append(read_only)
        return self.service()

    def service(self) -> ScrubLimit:
        """The use cases over this world's ports."""
        return ScrubLimit(
            settings=FakeScrubSettings(self),
            dropin=FakeScrubDropin(self),
            systemd=FakeScrubSystemd(self),
            sysfs=FakeScrubSysfs(self),
        )


def _rmdir(*, exists: bool, empty: bool) -> None:
    """What os.rmdir raises for a folder that is not there or not empty."""
    if not exists:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))
    if not empty:
        raise OSError(errno.ENOTEMPTY, os.strerror(errno.ENOTEMPTY))


def _installer_failed(err: OSError) -> tuple[int, list[str], str]:
    return 1, [], err.strerror or str(err)


class FakeScrubSettings:
    """ScrubSettingsPort over a ScrubWorld."""

    def __init__(self, world: ScrubWorld) -> None:
        self.world = world

    @property
    def path(self) -> str:
        return self.world.conf_path

    def read(self) -> tuple[str | None, bool]:
        w = self.world
        return (None, True) if w.conf_unreadable else (w.conf, False)

    def folder_exists(self) -> bool:
        return self.world.conf_folder

    def write(self, text: str) -> None:
        w = self.world

        def made() -> None:
            w.conf_folder = True

        def replaced() -> None:
            w.conf = text

        w.step("settings folder", made)
        w.step("settings replace", replaced)
        w.step("settings sync", lambda: None)

    def remove(self) -> None:
        w = self.world

        def removed() -> None:
            w.conf = None

        w.step("settings remove", removed)

    def remove_folder_if_empty(self) -> None:
        w = self.world

        def rmdir() -> None:
            _rmdir(exists=w.conf_folder, empty=w.conf is None)
            w.conf_folder = False

        with contextlib.suppress(OSError):  # as the adapter's: a folder not there, or not empty, stays
            w.step("settings rmdir", rmdir)


class FakeScrubDropin:
    """ScrubDropinPort over a ScrubWorld: the installer's text --keep-state and remove-text through its
    InstallService, with the installer's report lines."""

    def __init__(self, world: ScrubWorld) -> None:
        self.world = world

    @property
    def path(self) -> str:
        return self.world.dropin_path

    def read(self) -> tuple[str | None, bool]:
        w = self.world
        return (None, True) if w.dropin_unreadable else (w.dropin, False)

    def write(self, text: str) -> tuple[int, list[str], str]:
        w = self.world

        def write_text() -> tuple[int, list[str], str]:
            place = core_text(w.dropin_path, w.record)
            written = w.svc.put(place, text.encode(), as_root=False, keep_state=True)
            w.dropin_folder = True
            if written:
                w.systemd.changed()
            word = InstallReportWord.WRITTEN if written else InstallReportWord.UNCHANGED
            return 0, [word.line(w.dropin_path)], ""

        return w.step("drop-in text", write_text, _installer_failed)

    def remove(self) -> tuple[int, list[str], str]:
        w = self.world

        def remove_text() -> tuple[int, list[str], str]:
            lines = list(w.svc.remove([core_text(w.dropin_path, w.record)]))
            if lines:
                w.systemd.changed()
            return 0, lines, ""

        return w.step("drop-in remove-text", remove_text, _installer_failed)

    def remove_folder_if_empty(self) -> None:
        w = self.world

        def rmdir() -> None:
            _rmdir(exists=w.dropin_folder, empty=w.dropin is None)
            w.dropin_folder = False

        with contextlib.suppress(OSError):  # as the adapter's: a folder not there, or not empty, stays
            w.step("drop-in rmdir", rmdir)


class FakeScrubSystemd:
    """ScrubSystemdPort over a ScrubWorld."""

    def __init__(self, world: ScrubWorld) -> None:
        self.world = world

    def need_daemon_reload(self) -> bool | None:
        w = self.world
        return None if w.show_fails else w.dirty

    def daemon_reload(self, *, timeout: float | None) -> CommandResult:
        w = self.world
        argv = ("systemctl", "daemon-reload")

        def reload() -> CommandResult:
            w.reload_timeouts.append(timeout)
            w.systemd.reloaded(w.dropin)
            return CommandResult(0, "", "", argv=argv)

        def failed(err: OSError) -> CommandResult:
            w.reload_timeouts.append(timeout)  # asked for, and it failed
            return CommandResult(1, "", f"{err.strerror}\n", argv=argv)

        return w.step("systemctl daemon-reload", reload, failed)


class FakeScrubSysfs:
    """ScrubSysfsPort over a ScrubWorld."""

    def __init__(self, world: ScrubWorld) -> None:
        self.world = world

    def live_limits(self) -> tuple[str, ...]:
        return self.world.live


# ---- iocost --------------------------------------------------------------

IOCOST_ENTRIES: Final = tuple(select_entries(INSTALL_MANIFEST, component=Component.BTRFS_IOCOST))
UDEV_PROCESSED: Final = UdevProperties({USEC_INITIALIZED_PROP: "1234"})  # udev processed it, no hwdb solution


class IocostWorld(FaultPoints):
    """What iocost's ports see, in memory, for the use cases of btrfs/iocost/application.py: the kernel's
    io.cost.qos (one line per device, a write replacing its device's line), the cgroup controllers,
    io.cost.model and the block disks, udev's answer for each disk and whether udevd runs, the
    managed-devices set (or a folder at its path), btrfs:iocost's files in an in-memory install target and
    its state in an in-memory install record (installed, marked and removed through the real
    InstallService, as installer.py install and mark and the in-process removal do; release: the CST whose
    payload an install writes, so a new one rewrites every file), what systemd has loaded of the unit
    (SystemdState, shared with scrub-limit's fakes), the rule as udev loaded it, and the two locks.

    Every mutating call is one point (FaultPoints): "installer install", "installer mark done",
    "managed save", "qos MAJ:MIN ..." (the line), "systemd daemon-reload", "systemd enable --now",
    "systemd disable --now", "udev reload-rules", "uninstall mark pending|done" and "uninstall remove". A
    failed command is an exit 1 with the error on stderr; a failed save, write, mark or removal raises it.
    The fakes check the locks as the protocol must hold them (an AssertionError names the call): the
    io.cost.qos lock at every io.cost.qos write and save of the managed set; the component lock at every
    install, mark and removal of btrfs:iocost and at enable --now, which also needs the io.cost.qos lock
    free (the service it starts takes it). Reads are not points; each read gives a new object, never the
    one the world holds, so a use case that compares by identity is caught."""

    def __init__(self, *, install_root: str = "") -> None:
        super().__init__()
        self.qos_path = IOCOST_QOS_PATH
        self.managed_path = IOCOST_MANAGED_FILE
        self.lock_paths = {
            "qos": IOCOST_LOCK,
            "component": posixpath.join(posixpath.dirname(IOCOST_LOCK), IOCOST_COMPONENT_LOCK_NAME),
        }
        self.qos: dict[str, str] | None = {}  # MAJ:MIN -> its line; None: no io.cost.qos (no blk-iocost)
        self.qos_refusal: OSError | None = None  # the kernel refuses every write with it
        self.controllers: str | None = "cpuset cpu io memory pids"
        self.model = ""
        self.disks: list[BlockDisk] = []
        self.udev_answers: dict[str, UdevProperties] = {}  # by disk name; UDEV_PROCESSED by default
        self.udevd = True
        self.managed: set[str] = set()
        self.managed_folder = False  # a folder where the managed-devices file belongs
        self.systemd = SystemdState()  # loaded: the unit file's text at its last daemon-reload that worked
        self.show_fails = False  # systemctl cannot say what the unit is
        self.udev_loaded: str | None = None  # the rule's text as udev loaded it at its last reload
        self.installer_available = True
        self.plan_answer: list[str] | None = []  # the installer plan's changes (None: it cannot tell)
        self.release = 0
        self.target = MemoryTarget()
        self.records = MemoryRecord()
        self.req = InstallRequest(root=install_root, as_root=True)
        self.record = record_file(Owner.ROOT, None, install_root)
        self.svc = InstallService(self.target, lambda e: f"{e.dest} {self.release}\n".encode(), record=self.records)
        self.held = {"qos": False, "component": False}
        self.busy: set[str] = set()  # the locks another run holds
        self.lock_errors: dict[str, OSError] = {}  # the locks whose folder or file cannot be made
        self.events: list[str] = []  # what worked, in order: install, reload, enable, udev, mark, disable, ...
        self.before_mark: Callable[[], None] | None = None  # runs before a mark done of btrfs:iocost

    # ---- the files ---------------------------------------------------------------------------------
    def dest_path(self, dest: str) -> str:
        """Where btrfs:iocost's file dest is in the target (under the install root)."""
        entry = next(e for e in IOCOST_ENTRIES if e.dest == dest)
        return self.svc.placement(entry, self.req).path

    @property
    def unit_path(self) -> str:
        return self.dest_path(next(e.dest for e in IOCOST_ENTRIES if e.kind == ArtifactKind.UNIT))

    @property
    def rule_path(self) -> str:
        return self.dest_path(next(e.dest for e in IOCOST_ENTRIES if e.dest.endswith(".rules")))

    def file(self, path: str) -> str | None:
        """A file's text in the target (None: not there)."""
        held = self.target.files.get(path)
        return None if held is None else held[0].decode()

    def in_place(self) -> list[str]:
        """btrfs:iocost's destinations whose file is in the target."""
        return [e.dest for e in IOCOST_ENTRIES if self.dest_path(e.dest) in self.target.files]

    def state(self) -> ComponentState | None:
        """btrfs:iocost's state in the install record (None: no record, or none for it)."""
        rec = self.records.read(self.record)
        return None if rec is None else rec.components.get(Component.BTRFS_IOCOST.value)

    def recorded(self) -> set[str]:
        """The destinations the install record lists for btrfs:iocost."""
        rec = self.records.read(self.record)
        return (
            set() if rec is None else {d for d, f in rec.files.items() if f.component == Component.BTRFS_IOCOST.value}
        )

    def qos_text(self) -> str | None:
        """io.cost.qos as the kernel shows it (None: not there)."""
        return None if self.qos is None else "".join(f"{line}\n" for line in self.qos.values())

    # ---- start states ------------------------------------------------------------------------------
    def put_disk(
        self, name: str, devno: str, *, rotational: bool = False, scheduler: str = "none", model: str = ""
    ) -> None:
        """A whole block disk the scan finds."""
        self.disks.append(BlockDisk(name=name, devno=devno, rotational=rotational, scheduler=scheduler, model=model))

    def install(self, *, done: bool) -> None:
        """btrfs:iocost as an apply leaves it: its files and their lines, systemd reloaded, the service
        enabled and the rule loaded (done), or still pending with the service off."""
        list(self.svc.install(IOCOST_ENTRIES, self.req))
        self.systemd.reloaded(self.file(self.unit_path))
        if done:
            self.svc.mark(self.record, Component.BTRFS_IOCOST.value)
            self.systemd.enabled = self.systemd.active = True
            self.udev_loaded = self.file(self.rule_path)

    # ---- the locks the fakes check -----------------------------------------------------------------
    def holds(self, lock: str, what: str) -> None:
        if not self.held[lock]:
            raise AssertionError(f"{what} without the {lock} lock")

    def service(self) -> Iocost:
        """The use cases over this world's ports."""
        return Iocost(
            sysfs=FakeIocostSysfs(self),
            qos=FakeQosControl(self),
            managed=FakeManagedDevices(self),
            udev=FakeUdev(self),
            systemd=FakeIocostSystemd(self),
            installer=FakeIocostInstaller(self),
            qos_lock=FakeIocostLock(self, "qos"),
            component_lock=FakeIocostLock(self, "component"),
            uninstall=FakeIocostUninstall(self),
        )


def _command_failed(argv: tuple[str, ...]) -> Callable[[OSError], CommandResult]:
    def failed(err: OSError) -> CommandResult:
        return CommandResult(1, "", f"{err.strerror}\n", argv=argv)

    return failed


def _installer_failed_lines(err: OSError) -> tuple[int, list[str]]:
    return 1, [err.strerror or str(err)]


class FakeQosControl:
    """QosControlPort over an IocostWorld."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    @property
    def path(self) -> str:
        return self.world.qos_path

    def read(self) -> str | None:
        return self.world.qos_text()

    def write(self, line: str) -> None:
        w = self.world
        w.holds("qos", f"io.cost.qos <- {line!r}")
        if w.qos is None:
            raise AssertionError(f"io.cost.qos <- {line!r} with no io.cost.qos (the kernel has none to write)")

        def merged() -> None:
            if w.qos_refusal is not None:
                w.events.append("qos failed")
                raise w.qos_refusal
            assert w.qos is not None
            w.qos[line.split(maxsplit=1)[0]] = line

        w.step(f"qos {line}", merged)


class FakeManagedDevices:
    """ManagedDevicesPort over an IocostWorld: a folder at its path cannot be written or removed (EISDIR)."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    @property
    def path(self) -> str:
        return self.world.managed_path

    def read(self) -> frozenset[str]:
        w = self.world
        return frozenset() if w.managed_folder else frozenset(list(w.managed))

    def save(self, devnos: frozenset[str]) -> None:
        w = self.world
        w.holds("qos", "the managed-devices file's save")

        def saved() -> None:
            if w.managed_folder:
                raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR))
            w.managed = set(devnos)

        w.step("managed save", saved)


class FakeIocostSysfs:
    """IocostSysfsPort over an IocostWorld."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    def disks(self) -> tuple[BlockDisk, ...]:
        return tuple(self.world.disks)

    def io_controller(self) -> bool | None:
        ctrl = self.world.controllers
        return None if ctrl is None else "io" in ctrl.split()

    def model(self) -> str:
        return self.world.model.strip()


class FakeUdev:
    """UdevPort over an IocostWorld."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    def running(self) -> bool:
        return self.world.udevd

    def rule_installed(self) -> bool:
        w = self.world
        return w.rule_path in w.target.files

    def properties(self, name: str) -> UdevProperties:
        answer = self.world.udev_answers.get(name, UDEV_PROCESSED)
        return UdevProperties(None if answer.props is None else dict(answer.props), answer.error)

    def reload_rules(self) -> CommandResult:
        w = self.world
        argv = ("udevadm", "control", "--reload-rules")

        def reload() -> CommandResult:
            w.udev_loaded = w.file(w.rule_path)
            w.events.append("udev")
            return CommandResult(0, "", "", argv=argv)

        return w.step("udev reload-rules", reload, _command_failed(argv))


class FakeIocostSystemd:
    """IocostSystemdPort over an IocostWorld: the unit is loaded while its file is in the target."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    def probe(self) -> tuple[bool, str, str]:
        w = self.world
        if w.show_fails:
            return False, "unknown", "unknown"
        s = w.systemd
        return (
            w.unit_path in w.target.files,
            "active" if s.active else "inactive",
            "enabled" if s.enabled else "disabled",
        )

    def daemon_reload(self) -> CommandResult:
        w = self.world
        argv = ("systemctl", "daemon-reload")

        def reload() -> CommandResult:
            w.systemd.reloaded(w.file(w.unit_path))
            w.events.append("reload")
            return CommandResult(0, "", "", argv=argv)

        return w.step("systemd daemon-reload", reload, _command_failed(argv))

    def enable_now(self) -> CommandResult:
        w = self.world
        argv = ("systemctl", "enable", "--now", IOCOST_SERVICE)
        w.holds("component", "enable --now")
        if w.held["qos"]:
            raise AssertionError("enable --now while this run holds the io.cost.qos lock the service takes")

        def enable() -> CommandResult:
            if w.unit_path not in w.target.files:
                return CommandResult(
                    1, "", f"Failed to enable unit: Unit file {IOCOST_SERVICE} does not exist\n", argv=argv
                )
            w.systemd.enabled = w.systemd.active = True
            w.events.append("enable")
            return CommandResult(0, "", "", argv=argv)

        return w.step("systemd enable --now", enable, _command_failed(argv))

    def disable_now(self) -> CommandResult:
        w = self.world
        argv = ("systemctl", "disable", "--now", IOCOST_SERVICE)
        w.holds("component", "disable --now")

        def disable() -> CommandResult:
            w.systemd.enabled = w.systemd.active = False
            w.events.append("disable")
            return CommandResult(0, "", "", argv=argv)

        return w.step("systemd disable --now", disable, _command_failed(argv))


class FakeIocostInstaller:
    """IocostInstallerPort over an IocostWorld: install and mark through its InstallService, as
    installer.py does (its report lines, its error line on a failure)."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    def available(self) -> bool:
        return self.world.installer_available

    def install(self) -> tuple[int, list[str]]:
        w = self.world
        w.holds("component", "the installer's install")

        def install() -> tuple[int, list[str]]:
            lines: list[str] = []
            try:
                lines.extend(w.svc.install(IOCOST_ENTRIES, w.req))
            except (WriteFailed, InstallRefused) as e:
                w.events.append("install failed")
                return 1, [*lines, f"{INSTALLER_NAME}: {e}"]
            if any(not line.startswith(InstallReportWord.UNCHANGED) for line in lines):
                w.systemd.changed()
            w.events.append("install")
            return 0, lines

        return w.step("installer install", install, _installer_failed_lines)

    def mark_done(self) -> tuple[int, list[str]]:
        w = self.world
        w.holds("component", "the installer's mark done")

        def mark() -> tuple[int, list[str]]:
            if w.before_mark is not None:
                w.before_mark()
            w.svc.mark(w.record, Component.BTRFS_IOCOST.value)
            w.events.append("mark")
            return 0, []

        return w.step("installer mark done", mark, _installer_failed_lines)

    def changes(self) -> tuple[str, ...] | None:
        answer = self.world.plan_answer
        return None if answer is None else tuple(answer)


class FakeIocostUninstall:
    """IocostUninstallPort over an IocostWorld's InstallService (its target's fail_on fails a removal)."""

    def __init__(self, world: IocostWorld) -> None:
        self.world = world

    @property
    def record_path(self) -> str:
        return self.world.record

    def mark(self, state: ComponentState) -> ComponentState | None:
        w = self.world
        w.holds("component", f"the uninstall's mark {state}")
        return w.step(f"uninstall mark {state}", lambda: w.svc.mark(w.record, Component.BTRFS_IOCOST.value, state))

    def remove(self) -> list[str]:
        w = self.world
        w.holds("component", "the uninstall's removal")

        def remove() -> list[str]:
            lines = list(w.svc.remove([w.svc.placement(e, w.req) for e in IOCOST_ENTRIES]))
            if lines:
                w.systemd.changed()
            return lines

        return w.step("uninstall remove", remove)


class FakeIocostLock:
    """IocostLockPort over an IocostWorld: busy, another run holds it; lock_errors, its folder or file
    cannot be made. A run never takes a lock it already holds."""

    def __init__(self, world: IocostWorld, name: str) -> None:
        self.world = world
        self.name = name

    @property
    def path(self) -> str:
        return self.world.lock_paths[self.name]

    def take(self) -> bool:
        w = self.world
        err = w.lock_errors.get(self.name)
        if err is not None:
            raise err
        if self.name in w.busy:
            return False
        if w.held[self.name]:
            raise AssertionError(f"the {self.name} lock taken twice")
        w.held[self.name] = True
        return True

    def release(self) -> None:
        self.world.held[self.name] = False


# ---- cache ---------------------------------------------------------------

UUID_ROOT = "11111111-1111-1111-1111-111111111111"
UUID_CACHE_DRIVE = "22222222-2222-2222-2222-222222222222"


class CacheTestCtx:
    def __init__(self, *, container: bool = False, root: bool = True) -> None:
        self._container = container
        self._root = root

    def is_container(self) -> bool:
        return self._container

    def is_root(self) -> bool:
        return self._root

    def path(self, host_path: str) -> str:
        return f"/run/host/{host_path.lstrip('/')}" if self._container and host_path.startswith("/") else host_path

    def etc_dir(self) -> str:
        return "/run/host/etc" if self._container else "/etc"

    def host_run_dir(self, subpath: str) -> str:
        return f"/run/host/{subpath}" if self._container else f"/run/{subpath}"


class MountTable:
    """A list of mounts by the paths the user sees, and the kernel's rules for them: a bind mount takes the
    device of the mount holding its source and shows the folder it binds as its FSROOT ("DEVICE[FSROOT]" in
    findmnt's source), an umount takes the top mount of its target off, and findmnt answers `-J -l -o FINDMNT_COLUMNS`.
    The root and /var/home of the root filesystem are mounted from the start."""

    def __init__(self, *, same_uuid: bool = False) -> None:
        self.same_uuid = same_uuid  # findmnt shows every btrfs mount with the root's UUID
        self.mounts: list[dict[str, str | None]] = []
        self.add_mount("/dev/dm-0", "/", fsroot="/root", uuid=UUID_ROOT)
        self.add_mount("/dev/dm-0", "/var/home", fsroot="/home", uuid=UUID_ROOT)

    def add_mount(
        self,
        dev: str,
        tgt: str,
        *,
        fsroot: str,
        uuid: str | None = UUID_CACHE_DRIVE,
        bind: bool = False,
    ) -> None:
        """One mount: on btrfs with its UUID, or a tmpfs (no UUID); a bind shows its FSROOT in its source."""
        self.mounts.append(
            {
                "source": f"{dev}[{fsroot}]" if bind else dev,
                "target": tgt,
                "fstype": "btrfs" if uuid is not None else "tmpfs",
                "options": "rw,noatime,compress=zstd:1",
                "uuid": uuid,
                "fsroot": fsroot,
            }
        )

    def holding(self, path: str) -> dict[str, str | None]:
        """The mount whose target is the longest leading folder of path (the last one of equal length)."""
        best = self.mounts[0]
        for m in self.mounts:
            tgt = str(m["target"]).rstrip("/") or "/"
            if (tgt in (path, "/") or path.startswith(f"{tgt}/")) and len(tgt) >= len(str(best["target"]).rstrip("/")):
                best = m
        return best

    def bind(self, src: str, tgt: str) -> None:
        """mount --bind src tgt: a mount of src's device at tgt, its FSROOT the folder src is on it."""
        held = self.holding(src)
        rel = src.removeprefix(str(held["target"]).rstrip("/"))
        dev = str(held["source"]).split("[", 1)[0]
        fsroot = posixpath.normpath(f"{held['fsroot']}/{rel}")
        self.add_mount(dev, tgt, fsroot=fsroot, uuid=held["uuid"], bind=True)

    def unbind(self, tgt: str) -> None:
        """umount tgt: its top mount taken off (nothing when nothing is mounted there)."""
        for i in range(len(self.mounts) - 1, -1, -1):
            if self.mounts[i]["target"] == tgt:
                del self.mounts[i]
                break

    def targets(self) -> list[str]:
        """Every mount's target, in the order they were mounted."""
        return [str(m["target"]) for m in self.mounts]

    def findmnt_json(self) -> str:
        fss = []
        for m in self.mounts:
            uuid = UUID_ROOT if self.same_uuid and m["uuid"] is not None else m["uuid"]
            fss.append({**m, "uuid": uuid})
        return json.dumps({"filesystems": fss}, indent=3)


class FstabSystemdView:
    """What systemd has loaded of /etc/fstab: the file's identity and its text at the last daemon-reload that
    worked, taken at the first look (boot) as a systemd that booted with what was there then. The identity
    is whatever tells one write of the file from another (a real file's inode and mtime, a fake's write
    number; None for no file): NeedDaemonReload goes by the file, not its text, so a rewrite of the same
    bytes counts."""

    def __init__(self) -> None:
        self.loaded: tuple[object, str | None] | None = None  # (identity, text); None before the first look

    def boot(self, identity: object, text: str | None) -> None:
        """The first look: systemd has the file as it is now (nothing when it has looked already)."""
        if self.loaded is None:
            self.loaded = (identity, text)

    def current(self, identity: object) -> bool:
        """Whether systemd has loaded the file whose identity is now identity."""
        return self.loaded is None or self.loaded[0] == identity

    def reloaded(self, identity: object, text: str | None) -> None:
        """A daemon-reload that worked: systemd has the file as it is now."""
        self.loaded = (identity, text)

    def text(self) -> str | None:
        """The text systemd loaded (None: no file, or no look yet)."""
        return None if self.loaded is None else self.loaded[1]


def mount_units_listing(loaded: str | None, *, need_reload: bool, mounted: Sequence[str], every_loaded: bool) -> str:
    """`systemctl show [--state=loaded] -p ... '*.mount'`'s answer: a block per loaded unit. A unit of each
    line of the loaded /etc/fstab (SourcePath=/etc/fstab, NeedDaemonReload=yes once the file changed since),
    mounted or not, and of each other mount (SourcePath=/proc/self/mountinfo, as systemd loads a mount it did
    not start). As the real systemctl: only with every_loaded (--state=loaded or --all) does a glob list the
    inactive units too; without it systemctl hides the inactive units that have no job queued (a failed or
    activating one is still listed). These fakes have no job and no failed or activating unit: their inactive
    units are those whose Where is not mounted, so without it only the units whose Where is mounted are listed."""
    need = "yes" if need_reload else "no"
    units: dict[str, tuple[str, str]] = {}
    for line in (loaded or "").splitlines():
        fields = line.split()
        if fields and not fields[0].startswith("#") and len(fields) > 1:
            units[fields[1]] = ("/etc/fstab", need)
    for tgt in mounted:
        units.setdefault(tgt, ("/proc/self/mountinfo", "no"))
    blocks = [
        f"Id={t.strip('/').replace('/', '-') or '-'}.mount\nLoadState=loaded\nSourcePath={src}\n"
        f"NeedDaemonReload={nd}\nWhere={t}\n"
        for t, (src, nd) in units.items()
        if every_loaded or t in mounted
    ]
    return "\n".join(blocks)


class FakeMountRunner:
    """findmnt, mount and umount over a MountTable, and systemctl over an FstabSystemdView of the test root's
    /etc/fstab. findmnt answers `-J -l -o FINDMNT_COLUMNS` as the real one does: a bind mount's source is
    "DEVICE[FSROOT]", its fsroot the folder it shows."""

    def __init__(self, root: str, *, same_uuid: bool = False) -> None:
        self.root = root
        self.table = MountTable(same_uuid=same_uuid)
        self.findmnt_fails = False
        self.reload_fails = False
        self.on_failed_reload: Callable[[], None] | None = None  # runs when a reload fails, before the answer
        self.mount_fails: set[str] = set()
        self.umount_fails: set[str] = set()  # targets whose umount answers "target is busy" (exit 32)
        self.fstab_at_mount: list[str | None] = []
        self.fstab_at_umount: list[str | None] = []
        self.calls: list[list[str]] = []
        # systemd's view of the test root's /etc/fstab: the file (its inode and mtime: a rewrite of the same
        # bytes counts, as NeedDaemonReload goes by the file's time) and its text as the last daemon-reload
        # loaded them; taken at the first command, as a systemd that booted with what is there then.
        self.systemd = FstabSystemdView()
        self.reloads = 0  # the daemon-reloads that worked
        self.show_fails = False  # systemctl show answers exit 1: no answer
        self.units_visible = True  # False: systemctl show lists no unit (test_160's systemd, which sees none)
        self.before_reload: Callable[[], None] | None = None  # runs before a reload that works (a kill there)

    @property
    def mounts(self) -> list[dict[str, str | None]]:
        """The mounts, in the order they were mounted."""
        return self.table.mounts

    @mounts.setter
    def mounts(self, mounts: list[dict[str, str | None]]) -> None:
        self.table.mounts = mounts

    @property
    def same_uuid(self) -> bool:
        return self.table.same_uuid

    @same_uuid.setter
    def same_uuid(self, same: bool) -> None:
        self.table.same_uuid = same

    @property
    def loaded_fstab(self) -> tuple[object, str | None] | None:
        """(the identity, the text) of /etc/fstab at the last daemon-reload that worked; None before the first
        command."""
        return self.systemd.loaded

    @loaded_fstab.setter
    def loaded_fstab(self, loaded: tuple[object, str | None] | None) -> None:
        self.systemd.loaded = loaded

    def add_mount(
        self,
        dev: str,
        tgt: str,
        *,
        fsroot: str,
        uuid: str | None = UUID_CACHE_DRIVE,
        bind: bool = False,
    ) -> None:
        """One mount: on btrfs with its UUID, or a tmpfs (no UUID); a bind shows its FSROOT in its source."""
        self.table.add_mount(dev, tgt, fsroot=fsroot, uuid=uuid, bind=bind)

    def _fstab_now(self) -> str | None:
        fstab = os.path.join(self.root, "etc", "fstab")
        return Path(fstab).read_text(encoding="utf-8", errors="surrogateescape") if os.path.isfile(fstab) else None

    def _bind(self, src_path: str, tgt_path: str) -> CommandResult:
        self.fstab_at_mount.append(self._fstab_now())
        if tgt_path.removeprefix(self.root) in self.mount_fails:
            return CommandResult(32, "", "mount: permission denied")
        self.table.bind(src_path.removeprefix(self.root), tgt_path.removeprefix(self.root))
        return CommandResult(0, "", "")

    def _unbind(self, tgt_path: str) -> CommandResult:
        self.fstab_at_umount.append(self._fstab_now())
        logical_tgt = tgt_path.removeprefix(self.root)
        if logical_tgt in self.umount_fails:
            return CommandResult(32, "", "umount: target is busy")
        self.table.unbind(logical_tgt)
        return CommandResult(0, "", "")

    def _fstab_file(self) -> tuple[tuple[int, int] | None, str | None]:
        """(inode and mtime, text) of the test root's /etc/fstab; (None, None) without one, and no text for
        one that cannot be read (a test's)."""
        try:
            st = os.stat(os.path.join(self.root, "etc", "fstab"))
            return (st.st_ino, st.st_mtime_ns), self._fstab_now()
        except OSError:
            return None, None

    def view_current(self) -> bool:
        """Whether systemd has loaded the test root's /etc/fstab as it is on disk."""
        return self.systemd.current(self._fstab_file()[0])

    def _mount_units(self, *, every_loaded: bool) -> CommandResult:
        """`systemctl show [--state=loaded] -p ... '*.mount'` (mount_units_listing)."""
        if self.show_fails:
            return CommandResult(1, "", "Failed to connect to bus")
        if not self.units_visible:
            return CommandResult(0, "", "")
        listing = mount_units_listing(
            self.systemd.text(),
            need_reload=not self.view_current(),
            mounted=self.table.targets(),
            every_loaded=every_loaded,
        )
        return CommandResult(0, listing, "")

    def _findmnt(self) -> CommandResult:
        if self.findmnt_fails:
            return CommandResult(1, "", "findmnt: cannot read the mount table")
        return CommandResult(0, self.table.findmnt_json(), "")

    def _daemon_reload(self) -> CommandResult:
        """A reload that fails (reload_fails, after on_failed_reload), or one that works: before_reload runs
        (a kill there), then systemd has loaded the test root's /etc/fstab as it is now."""
        if self.reload_fails:
            if self.on_failed_reload is not None:
                self.on_failed_reload()
            return CommandResult(1, "", "Failed to reload daemon")
        if self.before_reload is not None:
            self.before_reload()
        self.systemd.reloaded(*self._fstab_file())
        self.reloads += 1
        return CommandResult(0, "", "")

    def run(self, argv: Sequence[str], **_kwargs: object) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        if self.systemd.loaded is None:
            self.systemd.boot(*self._fstab_file())
        if cmd == ["findmnt", "-J", "-l", "-o", FINDMNT_COLUMNS]:
            return self._findmnt()
        if cmd[:2] == ["systemctl", "show"] and cmd[-1:] == ["*.mount"]:
            return self._mount_units(every_loaded=bool({"--state=loaded", "--all"} & set(cmd)))
        if cmd[-1:] == ["daemon-reload"]:
            return self._daemon_reload()
        if cmd[:2] == ["mount", "--bind"]:
            return self._bind(cmd[2], cmd[3])
        if cmd[:1] == ["umount"]:
            return self._unbind(cmd[1])
        return CommandResult(0, "", "")


def _os_error(code: int, path: str) -> OSError:
    return OSError(code, os.strerror(code), path)


def _fresh(text: str | None) -> str | None:
    """A copy of text that is not the same object (a read never hands out the text the world holds)."""
    return None if text is None else text.encode("utf-8", "surrogateescape").decode("utf-8", "surrogateescape")


class CacheWorld(FaultPoints):
    """What cache's ports see, in memory, for the use cases of btrfs/cache/application.py: /etc/fstab (its
    text, None for no file, or a folder at its path, which every read, write and removal fails on with
    EISDIR), /etc/crypttab, the accounts (passwd entries by name and the caller's name, and the folder links
    the home is resolved through, as /home -> /var/home on Silverblue), the file system as paths (folders,
    files and links; the folders ensure_private made with their owner, the tags ensure_tag wrote), the
    mounts (a MountTable, shared with FakeMountRunner) and what systemd has loaded of /etc/fstab (an
    FstabSystemdView, shared with FakeMountRunner, fed a new identity per write and None after a removal,
    taken at the first call of any fake as a systemd that booted with what was there).

    Every mutating call is one point (FaultPoints), named as main's fault matrix named its call:
    "folder PATH" (each ensure_private), "tag SOURCE" (a CACHEDIR.TAG written: none when one is there),
    "mount --bind SOURCE TARGET", "umount TARGET", "fstab write" (the write and the put-back),
    "fstab remove" (the put-back when there was no file) and "systemctl daemon-reload". A failed command
    answers "WHAT failed: REASON" (the error's text); a failed folder, tag or /etc/fstab call raises it.
    Every error the fakes model (a link at a private folder: ENOTDIR, as O_NOFOLLOW with O_DIRECTORY gives;
    a file or a dangling link in its way: EEXIST; a file at or above a tag's folder: EEXIST, as write_atomic's
    ensure_dir gives; /etc/fstab a folder: EISDIR) raises inside the call, before a
    kill armed there fires. before_reload runs before a reload takes effect (a kill there leaves systemd
    behind); a KILL of the plan at the reload point is one after it took effect. Reads are not points;
    each read gives a new object, never the one the world holds."""

    def __init__(self) -> None:
        super().__init__()
        self.fstab: str | None = None
        self.fstab_folder = False  # a folder where /etc/fstab belongs
        self.fstab_writes = 0  # the identity of the file now: a new number per write (NeedDaemonReload)
        self.fstab_id: int | None = None  # None: no file
        self.crypttab: str | None = None
        self.users: dict[str, CacheMountUser] = {}
        self.caller = ""
        self.links: dict[str, str] = {}  # path -> the path it points to
        self.dirs: set[str] = {"/"}
        self.files: set[str] = set()
        self.made: dict[str, tuple[int, int]] = {}  # the folders ensure_private made private: path -> (uid, gid)
        self.tags: dict[str, tuple[int, int]] = {}  # the sources whose CACHEDIR.TAG ensure_tag wrote -> its owner
        self.table = MountTable()
        self.systemd = FstabSystemdView()
        self.reloads = 0  # the daemon-reloads that took effect
        self.findmnt_fails = False
        self.findmnt_bad_json = False
        self.show_fails = False  # systemctl show gives no answer
        self.units_visible = True  # False: systemctl show lists no unit
        self.before_reload: Callable[[], None] | None = None  # runs before a reload takes effect (a kill there)
        self.failing_at: dict[str, tuple[int, OSError]] = {}  # point -> (the n-th call from now, its error)
        self.seen: dict[str, int] = {}

    def fail_at(self, point: str, n: int, err: OSError) -> None:
        """The n-th call of point from now fails with err (once), the ones before it work."""
        self.failing_at[point] = (n, err)
        self.seen[point] = 0

    @override
    def step[T](self, point: str, effect: Callable[[], T], failed: Callable[[OSError], T] | None = None) -> T:
        at = self.failing_at.get(point)
        if at is not None:
            self.seen[point] += 1
            if self.seen[point] == at[0]:
                del self.failing_at[point]
                self.fail(point, at[1], times=1)
        return super().step(point, effect, failed)

    # ---- the start state ---------------------------------------------------------------------------
    def put_fstab(self, text: str | None) -> None:
        """/etc/fstab as a test's start state (None: no file), as systemd booted with it."""
        self.fstab = text
        self.fstab_writes += 1
        self.fstab_id = None if text is None else self.fstab_writes

    def add_user(self, name: str, uid: int, home: str) -> None:
        """An account (its gid its uid), and its home folder."""
        self.users[name] = CacheMountUser(name, uid, uid, home)
        self.mkdirs(home)

    def real_path(self, path: str, *, follow_last: bool = True) -> str:
        """path with each link among its folders followed, and its last part's too when follow_last, as the
        kernel walks a path: /home/carol is /var/home/carol where /home is a link to /var/home."""
        parts = [part for part in posixpath.normpath(path).split("/") if part]
        out = "/"
        for i, part in enumerate(parts):
            out = posixpath.join(out, part)
            hops = 0
            while (follow_last or i < len(parts) - 1) and out in self.links and hops <= len(self.links):
                out, hops = posixpath.normpath(self.links[out]), hops + 1
        return out

    def mkdirs(self, path: str) -> None:
        """The folder path and its parents, made where its links lead (os.makedirs)."""
        p = self.real_path(path)
        while p not in self.dirs:
            self.dirs.add(p)
            p = posixpath.dirname(p)

    def put_file(self, path: str) -> None:
        self.mkdirs(posixpath.dirname(path))
        self.files.add(path)

    def put_link(self, path: str, to: str) -> None:
        self.mkdirs(posixpath.dirname(path))
        self.links[path] = to

    def mount_bind(self, source: str, target: str) -> None:
        """A bind of source at target in place (its target folder made), recorded as no call."""
        self.mkdirs(target)
        self.table.bind(source, target)

    # ---- what the state is -------------------------------------------------------------------------
    def view_current(self) -> bool:
        """Whether systemd has loaded /etc/fstab as it is now."""
        return self.systemd.current(self.fstab_id)

    def commands(self) -> list[str]:
        """The points that ran a command (mount, umount, daemon-reload), in order."""
        return [c for c in self.calls if c.startswith(("mount ", "umount ", "systemctl "))]

    def exists(self, path: str) -> bool:
        """Anything at path, a dangling link too (os.path.lexists), through the links among its folders."""
        p = self.real_path(path, follow_last=False)
        return p in self.dirs or p in self.files or p in self.links

    def is_dir(self, path: str) -> bool:
        """A folder at path, through its links (os.path.isdir)."""
        return self.real_path(path) in self.dirs

    def nonempty(self, path: str) -> bool:
        """_dir_is_nonempty's answer: not there or a link is False (asked only for a non-link), a file
        is True, a folder whether anything is in it."""
        if path in self.files:
            return True
        if path not in self.dirs:
            return False
        inside = f"{path.rstrip('/')}/"
        return any(p.startswith(inside) for p in (*self.dirs, *self.files, *self.links))

    def look(self) -> None:
        """Any call of a fake: systemd has booted with /etc/fstab as it is at the first one."""
        if self.systemd.loaded is None:
            self.systemd.boot(self.fstab_id, self.fstab)

    def service(self) -> CacheMounts:
        """The use cases over this world's ports."""
        return CacheMounts(
            fstab=FakeFstab(self),
            mounts=FakeCacheMountTable(self),
            systemd=FakeCacheSystemd(self),
            folders=FakeCacheFolders(self),
            host=FakeCacheHostView(self),
            accounts=FakeCacheAccounts(self),
        )


class FakeFstab:
    """FstabPort over a CacheWorld."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def read(self) -> str | None:
        w = self.world
        w.look()
        if w.fstab_folder:
            raise _os_error(errno.EISDIR, "/etc/fstab")
        return _fresh(w.fstab)

    def write(self, text: str) -> None:
        w = self.world
        w.look()
        copy = _fresh(text)

        def write() -> None:
            if w.fstab_folder:
                raise _os_error(errno.EISDIR, "/etc/fstab")
            w.fstab_writes += 1
            w.fstab, w.fstab_id = copy, w.fstab_writes

        w.step("fstab write", write)

    def remove(self) -> None:
        w = self.world
        w.look()

        def remove() -> None:
            if w.fstab_folder:
                raise _os_error(errno.EISDIR, "/etc/fstab")
            w.fstab, w.fstab_id = None, None

        w.step("fstab remove", remove)


class FakeCacheMountTable:
    """MountTablePort over a CacheWorld's MountTable."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def read(self) -> tuple[tuple[LiveMount, ...], str | None]:
        w = self.world
        w.look()
        if w.findmnt_fails:
            return (), "findmnt failed: findmnt: cannot read the mount table"
        mounts = parse_findmnt_mounts('{"filesystems": 3}' if w.findmnt_bad_json else w.table.findmnt_json())
        if mounts is None:
            return (), "findmnt's answer is not the JSON it should be"
        return mounts, None

    def bind(self, binding: CacheMountBinding) -> str | None:
        w = self.world
        w.look()
        what = f"mount --bind {binding.source} {binding.target}"

        def bind() -> str | None:
            w.table.bind(binding.source, binding.target)
            return None

        return w.step(what, bind, lambda err: f"{what} failed: {err.strerror}")

    def unbind(self, binding: CacheMountBinding) -> str | None:
        w = self.world
        w.look()
        what = f"umount {binding.target}"

        def unbind() -> str | None:
            w.table.unbind(binding.target)
            return None

        return w.step(what, unbind, lambda err: f"{what} failed: {err.strerror}")


class FakeCacheSystemd:
    """CacheSystemdPort over a CacheWorld's FstabSystemdView."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def daemon_reload(self) -> str | None:
        w = self.world
        w.look()
        what = "systemctl daemon-reload"

        def reload() -> str | None:
            if w.before_reload is not None:
                w.before_reload()
            w.systemd.reloaded(w.fstab_id, _fresh(w.fstab))
            w.reloads += 1
            return None

        return w.step(what, reload, lambda err: f"{what} failed: {err.strerror}")

    def mount_units(self) -> list[dict[str, str]] | None:
        """The units mount_units_listing gives with --state=loaded, as SystemctlAdapter.show_units parses them."""
        w = self.world
        w.look()
        if w.show_fails:
            return None
        if not w.units_visible:
            return []
        listing = mount_units_listing(
            w.systemd.text(), need_reload=not w.view_current(), mounted=w.table.targets(), every_loaded=True
        )
        return [{p: unit[p] for p in MOUNT_UNIT_PROPS if p in unit} for unit in parse_show_units(listing)]


class FakeCacheFolders:
    """CacheFoldersPort over a CacheWorld's paths."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def ensure_private(self, path: str, user: CacheMountUser) -> None:
        w = self.world
        w.look()

        def ensure() -> None:
            p = posixpath.normpath(path)
            in_the_way = [q for q in _self_and_parents(p) if q in w.files or (q in w.links and not w.is_dir(q))]
            if in_the_way:
                raise _os_error(errno.EEXIST, in_the_way[-1])
            if p in w.links:
                raise _os_error(errno.ENOTDIR, p)  # O_NOFOLLOW with O_DIRECTORY on a link
            w.mkdirs(p)
            w.made[p] = (user.uid, user.gid)

        w.step(f"folder {path}", ensure)

    def ensure_tag(self, source: str, user: CacheMountUser) -> None:
        w = self.world
        w.look()
        tag = posixpath.join(source, CACHE_TAG)
        if w.exists(tag):
            return

        def write() -> None:
            in_the_way = [q for q in _self_and_parents(source) if q in w.files]
            if in_the_way:
                raise _os_error(errno.EEXIST, in_the_way[-1])  # ensure_dir's mkdir of a file's path
            w.mkdirs(source)  # write_atomic makes the folder it writes in
            w.files.add(tag)
            w.tags[source] = (user.uid, user.gid)

        w.step(f"tag {source}", write)


def _self_and_parents(path: str) -> list[str]:
    """path and each folder above it, the root last."""
    out = [path]
    while out[-1] != posixpath.dirname(out[-1]):
        out.append(posixpath.dirname(out[-1]))
    return out


class FakeCacheHostView:
    """CacheHostViewPort over a CacheWorld's paths."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def crypttab(self) -> str | None:
        self.world.look()
        return _fresh(self.world.crypttab)

    def binding_paths(self, binding: CacheMountBinding) -> BindingPaths:
        w = self.world
        w.look()
        src, tgt = binding.source, binding.target
        is_link = tgt in w.links
        exists = w.exists(tgt)
        return BindingPaths(
            target_is_link=is_link,
            target_exists=exists,
            target_nonempty=False if is_link else w.nonempty(tgt),
            source_is_dir=w.is_dir(src),
            source_is_link=src in w.links,
            target_is_dir=exists and not is_link and w.is_dir(tgt),
        )

    def home_exists(self, home: str) -> bool:
        self.world.look()
        return self.world.is_dir(home)


class FakeCacheAccounts:
    """CacheAccountsPort over a CacheWorld's accounts: the home resolved through its folder links."""

    def __init__(self, world: CacheWorld) -> None:
        self.world = world

    def resolve(self, requested: str | None) -> tuple[CacheMountUser | None, str | None]:
        w = self.world
        w.look()
        name = requested or w.caller
        user = w.users.get(name)
        if user is None:
            return None, f"user {name!r} does not exist"
        if user.uid < MIN_HUMAN_UID:
            return None, f"refusing system user {user.name!r} (uid={user.uid} < {MIN_HUMAN_UID})"
        home = user.home
        for link, to in w.links.items():
            if home.startswith(f"{link}/"):
                home = to + home.removeprefix(link)
        return CacheMountUser(user.name, user.uid, user.gid, home), None

# SPDX-License-Identifier: MPL-2.0
"""Fakes that several of btrfs's test modules share, one section per module they stand in for.

- any module: read and write, a text file of a test's folder (read: None for one that is not there);
- docker-volumes-nocow (test_docker_volumes_nocow and its fault tests): FakeSystem, the inode-flag
  ioctls, getxattr, the commands, os.sync and the fake cp; Crash, which its copy_into raises; the
  flag bits NOCOW and OTHER; entries, carries_flags and tree, to walk and compare a folder;
- scrub-limit (test_scrub_limit, its fault tests and tests/mbt/mbt_scrub_rollback): ScrubFakeRunner,
  the installer and systemctl; RecordingInstallerRunner, the installer's text, remove-text and mark
  through the real InstallService and one install record; SystemdModel, the same and what systemd
  has loaded of the drop-in; core_text, a generated text of btrfs:core;
- cache (test_cache): FakeMountRunner, findmnt, mount and umount over a list of mounts;
  CacheTestCtx, the host or toolbox the command runs in; the UUIDs the mounts carry.

Not collected as a test module (unittest finds test*.py).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
import json
import os
from pathlib import Path
import posixpath
import shutil
import stat
import struct
import subprocess
import sys
from typing import override

from btrfs.cache.domain import FINDMNT_COLUMNS
from cst_core.adapters import nocow as core_nocow
from cst_core.adapters.install import JsonInstallRecord, LocalInstallTarget
from cst_core.application.install import InstallService, Placement
from cst_core.domain.install import Component, GeneratedKind, InstallReportWord, Owner
from cst_core.ports import CommandFault, CommandResult

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
        self.loaded = loaded
        self.dirty = behind  # a write or a removal of the drop-in since the last reload that worked

    @override
    def run(self, argv: Sequence[str], *, input_text: str | None = None, **kw: object) -> CommandResult:
        cmd = list(argv)
        self.need_daemon_reload = "yes\n" if self.dirty else "no\n"
        r = super().run(argv, input_text=input_text, **kw)
        if "installer.py" in " ".join(cmd) and r.stdout.startswith(("written:", "removed:")):
            self.dirty = True
        if cmd == ["systemctl", "daemon-reload"] and r.returncode == 0:
            self.loaded, self.dirty = read(self.dropin), False
        return r


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


class FakeMountRunner:
    """findmnt, mount and umount over a list of mounts. findmnt answers `-J -l -o FINDMNT_COLUMNS` as the
    real one does: a bind mount's source is "DEVICE[FSROOT]", its fsroot the folder it shows."""

    def __init__(self, root: str, *, same_uuid: bool = False) -> None:
        self.root = root
        self.same_uuid = same_uuid
        self.findmnt_fails = False
        self.reload_fails = False
        self.on_failed_reload: Callable[[], None] | None = None  # runs when a reload fails, before the answer
        self.mount_fails: set[str] = set()
        self.fstab_at_mount: list[str | None] = []
        self.calls: list[list[str]] = []
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

    def _holding(self, path: str) -> dict[str, str | None]:
        best = self.mounts[0]
        for m in self.mounts:
            tgt = str(m["target"]).rstrip("/") or "/"
            if (tgt in (path, "/") or path.startswith(f"{tgt}/")) and len(tgt) >= len(str(best["target"]).rstrip("/")):
                best = m
        return best

    def _findmnt_json(self) -> str:
        fss = []
        for m in self.mounts:
            uuid = UUID_ROOT if self.same_uuid and m["uuid"] is not None else m["uuid"]
            fss.append({**m, "uuid": uuid})
        return json.dumps({"filesystems": fss}, indent=3)

    def _bind(self, src_path: str, tgt_path: str) -> CommandResult:
        fstab = os.path.join(self.root, "etc", "fstab")
        self.fstab_at_mount.append(
            Path(fstab).read_text(encoding="utf-8", errors="surrogateescape") if os.path.isfile(fstab) else None
        )
        if tgt_path.removeprefix(self.root) in self.mount_fails:
            return CommandResult(32, "", "mount: permission denied")
        src = src_path.removeprefix(self.root)
        held = self._holding(src)
        rel = src.removeprefix(str(held["target"]).rstrip("/"))
        dev = str(held["source"]).split("[", 1)[0]
        fsroot = posixpath.normpath(f"{held['fsroot']}/{rel}")
        self.add_mount(dev, tgt_path.removeprefix(self.root), fsroot=fsroot, uuid=held["uuid"], bind=True)
        return CommandResult(0, "", "")

    def _unbind(self, tgt_path: str) -> None:
        logical_tgt = tgt_path.removeprefix(self.root)
        for i in range(len(self.mounts) - 1, -1, -1):
            if self.mounts[i]["target"] == logical_tgt:
                del self.mounts[i]
                break

    def run(self, argv: Sequence[str], **_kwargs: object) -> CommandResult:
        cmd = list(argv)
        self.calls.append(cmd)
        if cmd == ["findmnt", "-J", "-l", "-o", FINDMNT_COLUMNS]:
            if self.findmnt_fails:
                return CommandResult(1, "", "findmnt: cannot read the mount table")
            return CommandResult(0, self._findmnt_json(), "")
        if cmd[-1:] == ["daemon-reload"] and self.reload_fails:
            if self.on_failed_reload is not None:
                self.on_failed_reload()
            return CommandResult(1, "", "Failed to reload daemon")
        if cmd[:2] == ["mount", "--bind"]:
            return self._bind(cmd[2], cmd[3])
        if cmd[:1] == ["umount"]:
            self._unbind(cmd[1])
        return CommandResult(0, "", "")

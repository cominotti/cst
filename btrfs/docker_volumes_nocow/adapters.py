# SPDX-License-Identifier: MPL-2.0
"""docker-volumes-nocow's machine side: where Docker keeps its volumes, their files and NOCOW flags,
whether a dockerd uses them, the helper's state files, SELinux labels, the journal and btrfs-notify."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
import contextlib
import errno
import json
import os
import shutil
import stat

from btrfs.docker_volumes_nocow.domain import (
    DAEMON_JSON,
    NEW_SUFFIX,
    OLD_MARK,
    NocowPaths,
    PostponedRecord,
    StoredPostponed,
    TreeScan,
    daemon_args,
    is_stored_postponed,
    service_wait_reason,
)
from cst_core.adapters.files import CounterFile, write_atomic
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.notify import Notifier
from cst_core.domain.contracts import Urgency
from cst_core.domain.shapes import json_object, parse_json
from cst_core.adapters.nocow import FS_NOCOW_FL as NOCOW, inode_flags as flags, set_nocow
from cst_core.ports import CommandResult, CommandRunnerPort


def daemon_config(path: str) -> tuple[str | None, str]:
    """(data-root, problem) of the dockerd config file at path: (None, "") when there is no such
    file or it names no data root (dockerd's default then). problem: why the file cannot be read
    as one (not JSON, not an object, a data-root that is
    not a folder name); dockerd refuses such a file, so which folder Docker uses is unknown.
    A read error other than a missing file is raised."""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return None, ""
    try:
        data = parse_json(text)
    except ValueError, RecursionError:
        return None, f"{path}: not JSON"
    if not json_object(data):
        return None, f"{path}: not a JSON object"
    if "data-root" not in data:
        return None, ""
    root = data["data-root"]
    if not isinstance(root, str) or not root:
        return None, f"{path}: data-root is not a folder name"
    return root, ""


def configured_root(path: str) -> str | None:
    """data-root in the dockerd config file at path; None when there is no such file or it names
    no data root. OSError when the file cannot be read, or (with daemon_config's reason) read as one."""
    root, problem = daemon_config(path)
    if problem:
        raise OSError(problem)
    return root


def docker_root() -> tuple[str, str]:
    """(the volumes folder of this machine's docker.service: data-root of /etc/docker/daemon.json,
    why that file cannot be read: "" when it can or there is none)."""
    try:
        root, problem = daemon_config(DAEMON_JSON)
    except OSError as e:
        root, problem = None, f"{DAEMON_JSON}: cannot be read ({e.strerror or e})"
    return os.path.join(root or "/var/lib/docker", "volumes"), problem


def checked(r: CommandResult) -> CommandResult:
    """A command that could not run raises its error, as subprocess.run did."""
    if r.cause is not None:
        raise r.cause
    return r


def real_dirs(root: str, names: Sequence[str], dev: int) -> list[str]:
    """Subdirectories to descend into: real directories on the same filesystem
    (os.walk lists symlinks to directories among them; those are entries,
    and NOCOW flags are never read or set through a link)."""
    out = []
    for d in names:
        st = os.lstat(os.path.join(root, d))
        if stat.S_ISDIR(st.st_mode) and st.st_dev == dev:
            out.append(d)
    return out


def regular_file(path: str, *, fix: bool) -> tuple[int, int] | None:
    """(apparent bytes, copy-on-write bytes) of a regular file, None for anything else.
    A file with data and without +C is copy-on-write; fix sets +C on an empty one."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        return None
    if flags(path) & NOCOW:
        return st.st_size, 0
    if not st.st_size and fix:
        set_nocow(path, True)
    return st.st_size, st.st_size


def mark_dirs(root: str, names: Sequence[str]) -> None:
    """+C on these folders of root (what is created in them later is NOCOW)."""
    for d in names:
        set_nocow(os.path.join(root, d), True)


def regular_files(root: str, names: Sequence[str], *, fix: bool) -> Iterator[tuple[int, int]]:
    """regular_file() of each name in root that is a regular file, in order."""
    for name in names:
        found = regular_file(os.path.join(root, name), fix=fix)
        if found is not None:
            yield found


def label(path: str) -> bytes | None:
    """The SELinux label of path itself (a link is not followed), without the
    trailing NUL the kernel may keep; None when it has none or the
    filesystem keeps no labels."""
    try:
        value = os.getxattr(path, "security.selinux", follow_symlinks=False)
    except OSError as e:
        if e.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
            return None
        raise
    return value.rstrip(b"\0")


class LocalVolumeTree:
    """VolumeTreePort over the volumes folder (NOCOW through cst_core's ioctls; cp, chcon and btrfs through the runner)."""

    def __init__(self, paths: NocowPaths, runner: CommandRunnerPort) -> None:
        self.paths = paths
        self._runner = runner

    def _run(self, argv: list[str]) -> CommandResult:
        # As subprocess.run(capture_output=True, text=True) did: stdin ours, output decoded strictly.
        return checked(self._runner.run(argv, stdin="inherit", decode_errors="strict"))

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def volumes(self) -> list[tuple[str, str]]:
        if not os.path.isdir(self.paths.volumes):
            return []
        out = []
        for name in sorted(os.listdir(self.paths.volumes)):
            data = os.path.join(self.paths.volumes, name, "_data")
            if os.path.isdir(data) and not os.path.islink(data):
                out.append((name, data))
        return out

    def scan(self, top: str, *, fix: bool = False) -> TreeScan:
        n = size = entries = total = 0
        dev = os.lstat(top).st_dev
        if fix:
            set_nocow(top, True)
        for root, dirs, files in os.walk(top):
            entries += len(dirs) + len(files)  # symlinks to directories included
            dirs[:] = real_dirs(root, dirs, dev)
            if fix:
                mark_dirs(root, dirs)
            for apparent, cow in regular_files(root, files, fix=fix):
                total += apparent
                if cow:
                    n += 1
                    size += cow
        return TreeScan(n, size, entries, total)

    def dirs_cow(self, top: str) -> bool:
        if not flags(top) & NOCOW:
            return True
        dev = os.lstat(top).st_dev
        for root, dirs, _ in os.walk(top):
            dirs[:] = real_dirs(root, dirs, dev)
            if any(not flags(os.path.join(root, d)) & NOCOW for d in dirs):
                return True
        return False

    def is_nocow(self, path: str) -> bool:
        return bool(flags(path) & NOCOW)

    def set_nocow(self, path: str, *, on: bool) -> None:
        set_nocow(path, on)

    def restore_interrupted(self) -> Iterator[str]:
        if not os.path.isdir(self.paths.volumes):
            return
        for name in os.listdir(self.paths.volumes):
            d = os.path.join(self.paths.volumes, name)
            if not os.path.isdir(d) or os.path.islink(d):
                continue
            data = os.path.join(d, "_data")
            olds = sorted(x for x in os.listdir(d) if x.startswith("_data" + OLD_MARK))
            if not os.path.lexists(data) and olds:
                # Died between the two renames: the original goes back.
                os.rename(os.path.join(d, olds[-1]), data)
                olds = olds[:-1]
                yield name
            if os.path.isdir(data):
                for x in olds + [x for x in os.listdir(d) if x == "_data" + NEW_SUFFIX]:
                    shutil.rmtree(os.path.join(d, x), ignore_errors=True)

    def make_twin(self, data: str, new: str) -> None:
        if os.path.lexists(new):
            shutil.rmtree(new)
        os.mkdir(new)
        st = os.lstat(data)
        os.chown(new, st.st_uid, st.st_gid)
        os.chmod(new, stat.S_IMODE(st.st_mode))

    def reference_label(self, data: str, new: str) -> None:
        """Give the empty folder new the SELinux label of data (chcon
        --reference), before anything is copied into it. When data has a label
        (SELinux keeps labels here) and chcon fails, new is removed and
        RuntimeError raised: a container could not open the copy. Without labels
        (SELinux disabled) chcon cannot work and its failure changes nothing."""
        r = self._run(["chcon", "--reference", data, new])
        if r.returncode != 0 and label(data) is not None:
            shutil.rmtree(new, ignore_errors=True)
            raise RuntimeError(f"SELinux label not copied: {r.stderr.strip()[:200] or f'chcon exit {r.returncode}'}")

    def copy(self, data: str, new: str) -> CommandResult:
        return self._run(["cp", "-a", "--reflink=never", "--sparse=auto", "--", data + "/.", new + "/"])

    def labels(self, top: str) -> dict[str, bytes | None]:
        """{path relative to top: label} for top and every entry below it, walked
        like scan (a link is an entry; another filesystem is not entered)."""
        out = {".": label(top)}
        dev = os.lstat(top).st_dev
        for root, dirs, files in os.walk(top):
            for name in dirs + files:
                p = os.path.join(root, name)
                out[os.path.relpath(p, top)] = label(p)
            dirs[:] = real_dirs(root, dirs, dev)
        return out

    def remove_tree(self, path: str, *, ignore_errors: bool = False) -> None:
        shutil.rmtree(path, ignore_errors=ignore_errors)

    def rename(self, src: str, dst: str) -> None:
        os.rename(src, dst)

    def sync(self) -> None:
        os.sync()

    def free_bytes(self) -> int:
        r = self._run(["btrfs", "filesystem", "usage", "-b", self.paths.volumes])
        for line in r.stdout.splitlines():
            if "Free (estimated)" in line:
                return int(line.split()[2])
        return 0


class NocowStateFiles:
    """NocowStatePort over the flag, the attempts counter and the postponed record."""

    def __init__(self, paths: NocowPaths) -> None:
        self.paths = paths
        self.counter = CounterFile(paths.attempts)

    def flag_exists(self) -> bool:
        return os.path.exists(self.paths.flag)

    def request_migration(self) -> None:
        """The flag and its folder (install creates it only when it found CoW data), fsynced:
        the file, its entry, its folder's entry."""
        folder = os.path.dirname(self.paths.flag)
        os.makedirs(folder, exist_ok=True)
        os.close(os.open(self.paths.flag, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o666))
        for path in (self.paths.flag, folder, os.path.dirname(folder)):
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def remove_flag(self) -> None:
        os.remove(self.paths.flag)

    def attempts(self) -> int:
        return self.counter.count()

    def save_attempts(self, count: int) -> None:
        self.counter.save(count)

    def remove_attempts(self) -> None:
        self.counter.remove()

    def forget_attempts(self) -> None:
        self.counter.forget()

    def boot_id(self) -> str:
        try:
            with open(os.path.join(self.paths.proc, "sys/kernel/random/boot_id")) as f:
                return f.read().strip()
        except OSError:
            return ""

    def read_postponed(self) -> StoredPostponed | None:
        try:
            with open(self.paths.postponed) as f:
                rec = parse_json(f.read())
        except OSError, ValueError:
            return None
        return rec if is_stored_postponed(rec) else None

    def write_postponed(self, record: PostponedRecord) -> None:
        os.makedirs(os.path.dirname(self.paths.postponed), exist_ok=True)
        # 0644: cst doctor reads it as the user. Durable (file and folder fsynced): a
        # boot counter, like the attempts counter; a reset must not lose a boot.
        write_atomic(self.paths.postponed, json.dumps(record) + "\n", mode=0o644, relabel=False)

    def clear_postponed(self) -> None:
        with contextlib.suppress(FileNotFoundError):  # nothing postponed
            os.remove(self.paths.postponed)


def is_dockerd(proc: str) -> bool:
    """The process in /proc/PID is a dockerd (its name or its program file)."""
    with open(os.path.join(proc, "comm"), "rb") as f:
        names = {f.read().strip()}
    with contextlib.suppress(OSError):  # a kernel thread, or a process that just ended
        # As bytes: a program file name need not be valid UTF-8.
        names.add(os.path.basename(os.readlink(os.fsencode(os.path.join(proc, "exe"))).removesuffix(b" (deleted)")))
    return b"dockerd" in names


def runs_as_root(proc: str) -> bool:
    """False when the process in /proc/PID has an effective uid other than 0
    (as this process sees it); True when it is 0 or cannot be told."""
    with open(os.path.join(proc, "status"), "rb") as f:
        for line in f:
            if line.startswith(b"Uid:"):
                fields = line.split()
                return not (len(fields) > 2 and fields[2].isdigit() and int(fields[2]) != 0)
    return True


def daemon_volumes(proc: str, args: Sequence[str]) -> str:
    """The volumes folder of the dockerd in /proc/PID, as a path this process
    can open: its --data-root, else data-root in its config file
    (--config-file, default /etc/docker/daemon.json), else /var/lib/docker.
    Paths are read in the daemon's own view, through /proc/PID/root (its
    mount namespace) and /proc/PID/cwd (a relative path). An absolute
    symlink on that path is still followed from our own root."""

    def inside(path: str) -> str:
        if path.startswith("/"):
            return os.path.join(proc, "root", path.lstrip("/"))
        return os.path.join(proc, "cwd", path)

    given = daemon_args(args)
    root = given.root or configured_root(inside(given.config)) or "/var/lib/docker"
    return os.path.join(inside(root), "volumes")


def root_dockerd_volumes(proc: str) -> os.stat_result | None:
    """The volumes folder (stat) of the process in /proc/PID when it is a dockerd running as root, else None."""
    if not is_dockerd(proc) or not runs_as_root(proc):
        return None
    with open(os.path.join(proc, "cmdline"), "rb") as f:
        args = [a.decode(errors="surrogateescape") for a in f.read().split(b"\0")]
    return os.stat(daemon_volumes(proc, args))


class DockerDaemons:
    """DockerDaemonPort: systemd's docker.service, and the dockerd processes in /proc."""

    def __init__(self, paths: NocowPaths, runner: CommandRunnerPort) -> None:
        self.paths = paths
        self._runner = runner

    def service_busy(self) -> str | None:
        """At boot docker.service reads "inactive" while its start job waits for this
        unit. No systemctl, an error or any other answer postpones."""
        r = self._runner.run(
            ["systemctl", "show", "-P", "ActiveState", "docker.service"], stdin="inherit", decode_errors="strict"
        )
        if r.cause is not None:
            return f"cannot ask systemd about docker.service ({r.cause})"
        return service_wait_reason(r.returncode, r.stdout.strip())

    def dockerd_busy(self) -> str | None:
        """A dockerd started by hand or by another unit is not docker.service. It
        counts when its volumes folder is the same directory as ours (device
        and inode, seen through its own mount namespace): one in a container
        keeps its data elsewhere and does not count, while a container that
        bind-mounts this data root does. A dockerd that does not run as root does
        not count either: it cannot write to the root-owned data root. That is
        rootless Docker, which runs plain `dockerd` (its data in the home folder)
        in a namespace that keeps this machine's /, so its default path would
        look like ours. A dockerd whose uid or data root cannot be read counts
        (fails closed)."""
        mine = os.stat(self.paths.volumes)
        for pid in sorted(os.listdir(self.paths.proc)):
            if not pid.isdigit():
                continue
            try:
                st = root_dockerd_volumes(os.path.join(self.paths.proc, pid))
            except FileNotFoundError, ProcessLookupError:
                continue  # the process ended, or its data root has no volumes folder
            except OSError as e:
                return f"dockerd (pid {pid}) is running and its data root cannot be read ({e})"
            if st is not None and (st.st_dev, st.st_ino) == (mine.st_dev, mine.st_ino):
                return f"dockerd (pid {pid}) uses {self.paths.volumes} outside docker.service"
        return None


class NocowOutput:
    """NocowOutputPort: stdout, the journal, btrfs-notify."""

    def __init__(self, paths: NocowPaths, runner: CommandRunnerPort) -> None:
        self.paths = paths
        self._runner = runner

    def say(self, line: str) -> None:
        print(line)

    def log(self, prio: int, msg: str) -> None:
        journal_log(prio, msg, tag="docker-volumes-nocow")

    def notify(self, title: str, body: str) -> None:
        Notifier(self._runner, self.paths.notify, self.paths.alerts, log=self.log).send(
            Urgency.NORMAL, "btrfs-setup", title, body
        )

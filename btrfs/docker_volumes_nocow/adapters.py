# SPDX-License-Identifier: MPL-2.0
"""docker-volumes-nocow's machine side: where Docker keeps its volumes, their files and NOCOW flags, whether a dockerd uses them, the postponed record, SELinux labels."""

from __future__ import annotations

from collections.abc import Sequence
import errno
from functools import partial
import json
import os
import shutil
import stat
import subprocess
import time
from typing import Any

from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.nocow import FS_NOCOW_FL as NOCOW, inode_flags as flags, set_nocow

FLAG = "/etc/btrfs-setup/docker-nocow-pending"
ATTEMPTS = "/var/lib/docker-volumes-nocow/attempts"
# The boots at which migrate had to wait (JSON, world-readable): shown by
# status and by `cst doctor`, removed at the first boot that is not held back.
POSTPONED = "/var/lib/docker-volumes-nocow/postponed"
NOTIFY = "/usr/local/sbin/btrfs-notify"
PROC = "/proc"  # where running dockerd processes are looked for (tests point it elsewhere)
MARGIN = 20 * 1024**3  # free space kept besides the copy itself

# One journal line per message, "<prio>docker-volumes-nocow: msg".
log = partial(journal_log, tag="docker-volumes-nocow")


def configured_root(path: str) -> str | None:
    """data-root in the dockerd config file at path; None when there is no
    such file, it is not a JSON object, or it names no data root (dockerd's
    default then). Any other error reading it is raised."""
    try:
        with open(path) as f:
            root = json.load(f).get("data-root")
    except FileNotFoundError, ValueError, AttributeError:
        return None
    return root if isinstance(root, str) and root else None


def docker_root() -> str:
    try:
        root = configured_root("/etc/docker/daemon.json")
    except OSError:
        root = None
    return os.path.join(root or "/var/lib/docker", "volumes")


VOLUMES = docker_root()


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


def cow_files(top: str, fix: bool = False) -> tuple[int, int, int, int]:
    """(regular files with data and without NOCOW, their apparent bytes, all
    entries, total apparent bytes) below top. fix=True also sets +C in place
    where that works: on directories (affects what is created later) and on
    empty files; only files holding data need a copy."""
    n = size = entries = total = 0
    dev = os.lstat(top).st_dev
    if fix:
        set_nocow(top, True)
    for root, dirs, files in os.walk(top):
        entries += len(dirs) + len(files)  # symlinks to directories included
        dirs[:] = real_dirs(root, dirs, dev)
        if fix:
            for d in dirs:
                set_nocow(os.path.join(root, d), True)
        for f in files:
            p = os.path.join(root, f)
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                continue
            total += st.st_size
            if flags(p) & NOCOW:
                continue
            if st.st_size:
                n += 1
                size += st.st_size
            elif fix:
                set_nocow(p, True)
    return n, size, entries, total


def dirs_cow(top: str) -> bool:
    """True if top or a directory below it lacks +C (new files there: CoW)."""
    if not flags(top) & NOCOW:
        return True
    dev = os.lstat(top).st_dev
    for root, dirs, _ in os.walk(top):
        dirs[:] = real_dirs(root, dirs, dev)
        for d in dirs:
            if not flags(os.path.join(root, d)) & NOCOW:
                return True
    return False


def volumes() -> list[tuple[str, str]]:
    """(name, path of _data) for every volume of the local driver."""
    if not os.path.isdir(VOLUMES):
        return []
    out = []
    for name in sorted(os.listdir(VOLUMES)):
        data = os.path.join(VOLUMES, name, "_data")
        if os.path.isdir(data) and not os.path.islink(data):
            out.append((name, data))
    return out


def pending(fix: bool = False) -> list[tuple[str, str, int, int]]:
    """Volumes that still have CoW data: [(name, data, files, bytes)].
    fix=True first sets +C in place on directories and empty files."""
    out = []
    for name, data in volumes():
        n, size, _, _ = cow_files(data, fix)
        if n:
            out.append((name, data, n, size))
    return out


def not_fully_nocow() -> list[str]:
    """Volumes with CoW data or a CoW directory (status)."""
    return [name for name, data in volumes() if cow_files(data)[0] or dirs_cow(data)]


def recover() -> None:
    """Leftovers of an interrupted migration (kill, timeout, power loss)."""
    if not os.path.isdir(VOLUMES):
        return
    for name in os.listdir(VOLUMES):
        d = os.path.join(VOLUMES, name)
        if not os.path.isdir(d) or os.path.islink(d):
            continue
        data = os.path.join(d, "_data")
        olds = sorted(x for x in os.listdir(d) if x.startswith("_data.cow-old-"))
        if not os.path.lexists(data) and olds:
            # Died between the two renames: the original goes back.
            os.rename(os.path.join(d, olds[-1]), data)
            olds = olds[:-1]
            log(4, f"{name}: restored the original after an interrupted migration")
        if os.path.isdir(data):
            for x in olds + [x for x in os.listdir(d) if x == "_data.nocow-new"]:
                shutil.rmtree(os.path.join(d, x), ignore_errors=True)


def free_bytes() -> int:
    r = subprocess.run(["btrfs", "filesystem", "usage", "-b", VOLUMES], check=False, capture_output=True, text=True)
    for line in r.stdout.splitlines():
        if "Free (estimated)" in line:
            return int(line.split()[2])
    return 0


def docker_busy() -> str | None:
    """None when docker.service is known to be stopped, else why the migration
    waits. Fails closed: only a clear "inactive" or "failed" lets it run (at
    boot docker.service reads "inactive" while its start job waits for this
    unit). No systemctl, an error or any other answer postpones."""
    try:
        r = subprocess.run(
            ["systemctl", "show", "-P", "ActiveState", "docker.service"], check=False, capture_output=True, text=True
        )
    except OSError as e:
        return f"cannot ask systemd about docker.service ({e})"
    state = r.stdout.strip()
    if r.returncode == 0 and state in ("inactive", "failed"):
        return None
    if r.returncode == 0 and state in ("active", "activating", "reloading", "deactivating"):
        return "Docker is running"
    return f"docker.service state unknown (systemctl exit {r.returncode}, ActiveState {state!r})"


def is_dockerd(proc: str) -> bool:
    """The process in /proc/PID is a dockerd (its name or its program file)."""
    with open(os.path.join(proc, "comm"), "rb") as f:
        names = {f.read().strip()}
    try:
        # As bytes: a program file name need not be valid UTF-8.
        names.add(os.path.basename(os.readlink(os.fsencode(os.path.join(proc, "exe"))).removesuffix(b" (deleted)")))
    except OSError:
        pass  # a kernel thread, or a process that just ended
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

    root, config = None, "/etc/docker/daemon.json"
    rest = iter(args[1:])
    for arg in rest:
        if arg[:2] == "-g" and arg[2:3] not in ("", "="):
            arg = "-g=" + arg[2:]  # the shorthand with its value attached: -g/path
        key, eq, value = arg.partition("=")
        if key in ("--data-root", "-g", "--graph", "--config-file"):
            if not eq:
                value = next(rest, "")
            if key == "--config-file":
                config = value
            else:
                root = value
    root = root or configured_root(inside(config)) or "/var/lib/docker"
    return os.path.join(inside(root), "volumes")


def dockerd_busy() -> str | None:
    """None when no dockerd uses VOLUMES, else why the migration waits.

    A dockerd started by hand or by another unit is not docker.service. It
    counts when its volumes folder is the same directory as VOLUMES (device
    and inode, seen through its own mount namespace): one in a container
    keeps its data elsewhere and does not count, while a container that
    bind-mounts this data root does. A dockerd that does not run as root does
    not count either: it cannot write to the root-owned data root. That is
    rootless Docker, which runs plain `dockerd` (its data in the home folder)
    in a namespace that keeps this machine's /, so its default path would
    look like ours. A dockerd whose uid or data root cannot be read counts
    (fails closed)."""
    mine = os.stat(VOLUMES)
    for pid in sorted(os.listdir(PROC)):
        if not pid.isdigit():
            continue
        proc = os.path.join(PROC, pid)
        try:
            if not is_dockerd(proc) or not runs_as_root(proc):
                continue
            with open(os.path.join(proc, "cmdline"), "rb") as f:
                args = [a.decode(errors="surrogateescape") for a in f.read().split(b"\0")]
            st = os.stat(daemon_volumes(proc, args))
        except FileNotFoundError, ProcessLookupError:
            continue  # the process ended, or its data root has no volumes folder
        except OSError as e:
            return f"dockerd (pid {pid}) is running and its data root cannot be read ({e})"
        if (st.st_dev, st.st_ino) == (mine.st_dev, mine.st_ino):
            return f"dockerd (pid {pid}) uses {VOLUMES} outside docker.service"
    return None


def read_postponed() -> dict[str, Any] | None:
    """The record of postponed boots (a dict), or None."""
    try:
        with open(POSTPONED) as f:
            rec = json.load(f)
    except OSError, ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def note_postponed(why: str) -> None:
    """Count this boot as postponed (once per boot, by the kernel's boot id)
    and keep the latest reason."""
    try:
        with open(os.path.join(PROC, "sys/kernel/random/boot_id")) as f:
            boot = f.read().strip()
    except OSError:
        boot = ""
    rec = read_postponed() or {}
    now = int(time.time())
    boots, first = rec.get("boots"), rec.get("first")
    if type(boots) is not int or type(first) is not int or boots < 1:
        boots, first = 0, now  # no record, or one this helper did not write
    if not boots or rec.get("boot_id") != boot:
        boots += 1
    os.makedirs(os.path.dirname(POSTPONED), exist_ok=True)
    tmp = POSTPONED + ".new"
    with open(tmp, "w") as f:
        json.dump({"reason": why, "boots": boots, "first": first, "last": now, "boot_id": boot}, f)
        f.write("\n")
    os.chmod(tmp, 0o644)  # cst doctor reads it as the user
    os.replace(tmp, POSTPONED)


def clear_postponed() -> None:
    try:
        os.remove(POSTPONED)
    except FileNotFoundError:
        pass


def postponed_line() -> str:
    """The status line of a postponed migration, or ""."""
    rec = read_postponed()
    if not rec:
        return ""
    last = rec.get("last")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if isinstance(last, int) else "?"
    return f"  migration postponed at {rec.get('boots', '?')} boot(s) in a row, last {when}: {rec.get('reason', '?')}"


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


def reference_label(data: str, new: str) -> None:
    """Give the empty folder new the SELinux label of data (chcon
    --reference), before anything is copied into it. When data has a label
    (SELinux keeps labels here) and chcon fails, new is removed and
    RuntimeError raised: a container could not open the copy. Without labels
    (SELinux disabled) chcon cannot work and its failure changes nothing."""
    r = subprocess.run(["chcon", "--reference", data, new], check=False, capture_output=True, text=True)
    if r.returncode != 0 and label(data) is not None:
        shutil.rmtree(new, ignore_errors=True)
        raise RuntimeError(f"SELinux label not copied: {r.stderr.strip()[:200] or f'chcon exit {r.returncode}'}")


def labels(top: str) -> dict[str, bytes | None]:
    """{path relative to top: label} for top and every entry below it, walked
    like cow_files (a link is an entry; another filesystem is not entered)."""
    out = {".": label(top)}
    dev = os.lstat(top).st_dev
    for root, dirs, files in os.walk(top):
        for name in dirs + files:
            p = os.path.join(root, name)
            out[os.path.relpath(p, top)] = label(p)
        dirs[:] = real_dirs(root, dirs, dev)
    return out

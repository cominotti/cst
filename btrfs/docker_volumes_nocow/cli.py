"""docker-volumes-nocow's commands: migrate (one volume's NOCOW copy, the boot's run), mark, unmark, status and main()."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time

from btrfs.docker_volumes_nocow.adapters import (
    ATTEMPTS,
    FLAG,
    MARGIN,
    NOTIFY,
    VOLUMES,
    clear_postponed,
    cow_files,
    dirs_cow,
    docker_busy,
    dockerd_busy,
    free_bytes,
    labels,
    log,
    not_fully_nocow,
    note_postponed,
    pending,
    postponed_line,
    recover,
    volumes,
)
from cst_core.adapters.files import write_atomic
from cst_core.adapters.nocow import FS_NOCOW_FL as NOCOW, inode_flags as flags, set_nocow


def migrate_one(name: str, data: str) -> None:
    """Copy _data into a NOCOW twin, check it, swap the names, drop the old copy."""
    new, old = data + ".nocow-new", data + f".cow-old-{int(time.time())}"
    if os.path.lexists(new):
        shutil.rmtree(new)
    os.mkdir(new)
    st = os.lstat(data)
    os.chown(new, st.st_uid, st.st_gid)
    os.chmod(new, stat.S_IMODE(st.st_mode))
    subprocess.run(["chcon", "--reference", data, new], check=False, capture_output=True)
    set_nocow(new, True)
    # Real copy (NOCOW and CoW extents cannot share data); every file and
    # directory created below `new` inherits +C before any data is written.
    r = subprocess.run(
        ["cp", "-a", "--reflink=never", "--sparse=auto", "--", data + "/.", new + "/"],
        check=False,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        shutil.rmtree(new, ignore_errors=True)
        raise RuntimeError(f"copy failed: {r.stderr.strip()[:200]}")
    n_cow, _, entries_new, bytes_new = cow_files(new)
    _, _, entries_old, bytes_old = cow_files(data)
    if entries_new != entries_old or bytes_new != bytes_old or n_cow or dirs_cow(new):
        shutil.rmtree(new, ignore_errors=True)
        raise RuntimeError(
            f"copy check failed (entries {entries_old} vs {entries_new}, bytes "
            f"{bytes_old} vs {bytes_new}, CoW files {n_cow})"
        )
    # cp -a keeps SELinux labels when it can and says nothing when it cannot;
    # a container cannot open a file whose label changed.
    labels_old, labels_new = labels(data), labels(new)
    differ = sorted(k for k in labels_old.keys() | labels_new.keys() if labels_old.get(k) != labels_new.get(k))
    if differ:
        rel = differ[0]

        def shown(v: bytes | None) -> str:
            return "none" if v is None else v.decode(errors="replace")

        shutil.rmtree(new, ignore_errors=True)
        raise RuntimeError(
            f"copy check failed (SELinux label of {rel}: {shown(labels_old.get(rel))} vs {shown(labels_new.get(rel))})"
        )
    os.sync()
    os.rename(data, old)
    try:
        os.rename(new, data)
    except OSError:
        os.rename(old, data)
        raise
    shutil.rmtree(old)


def request_migration() -> bool:
    """Write the flag (and its folder, which install creates only when it
    found CoW data), and make it durable before +C is set: the data root may
    be on another filesystem than /etc, and after a power loss +C must not
    stay while the flag is lost. False, with a log line, when that fails."""
    folder = os.path.dirname(FLAG)
    try:
        os.makedirs(folder, exist_ok=True)
        with open(FLAG, "a"):
            pass
        for path in (FLAG, folder, os.path.dirname(folder)):  # the file, its entry, its folder's entry
            fd = os.open(path, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError as e:
        log(3, f"cannot schedule the migration ({e}); {VOLUMES} keeps copy-on-write, the next boot tries again")
        return False
    return True


def migrate() -> int:
    if not os.path.isdir(VOLUMES):
        clear_postponed()  # no volumes (Docker removed, data-root moved): nothing waits
        return 0
    busy = docker_busy() or dockerd_busy()
    if busy:
        log(4, f"{busy}: migration left for the next boot")
        # Not an attempt and no alert, but recorded when there was work to
        # do (a scheduled migration, or the dir still without +C), so status
        # and `cst doctor` can show a migration held back boot after boot.
        if os.path.exists(FLAG) or not flags(VOLUMES) & NOCOW:
            note_postponed(busy)
        else:
            clear_postponed()
        return 0
    clear_postponed()
    recover()
    # Every boot: Docker may have been installed after btrfs-setup, creating
    # the volumes dir without +C; the volumes made in between are migrated too.
    # The dir without +C is the only sign of those volumes, so the flag is
    # written first: if that fails, the dir keeps no +C and the next boot sees
    # the same sign again (setting +C first would lose the request for good).
    if not flags(VOLUMES) & NOCOW:
        if pending() and not request_migration():
            return 0
        set_nocow(VOLUMES, True)
        pending(fix=True)
    if not os.path.exists(FLAG):
        return 0
    try:
        with open(ATTEMPTS) as f:
            attempts = int(f.read())
    except OSError, ValueError:
        attempts = 0
    if attempts >= 2:
        os.remove(FLAG)
        os.remove(ATTEMPTS)  # a later 'install' starts afresh
        msg = "Gave up after two boots; see journalctl -u docker-volumes-nocow."
        log(3, msg)
        # At boot nobody is logged in yet: btrfs-notify keeps it and it is
        # shown at the next graphical login.
        subprocess.run(
            [NOTIFY, "-u", "normal", "-a", "btrfs-setup", "Docker volumes: NOCOW migration gave up", msg],
            check=False,
            capture_output=True,
        )
        return 0
    # Durable before the long rewrite starts: a hang or hard reset must still count this boot,
    # or "gave up after two boots" never trips.
    write_atomic(ATTEMPTS, str(attempts + 1), mode=0o644)
    set_nocow(VOLUMES, True)
    todo = pending(fix=True)
    log(5, f"rewriting {len(todo)} volume(s) without copy-on-write before Docker starts")
    failed = 0
    for name, data, n, size in todo:
        if free_bytes() < size + MARGIN:
            log(4, f"{name}: not enough free space for a {size >> 20} MiB copy; skipped")
            failed += 1
            continue
        t0 = time.time()
        try:
            migrate_one(name, data)
            log(6, f"{name}: {n} file(s), {size >> 20} MiB in {time.time() - t0:.0f}s")
        except Exception as e:  # one volume must not stop the others
            log(3, f"{name}: {e}; left as it was")
            failed += 1
    if not failed:
        os.remove(FLAG)
        os.remove(ATTEMPTS)
        log(5, "all Docker volumes are NOCOW now")
    return 0


def mark() -> int:
    if not os.path.isdir(VOLUMES):
        print(f"{VOLUMES}: absent (Docker not set up yet): nothing to mark")
        return 0
    set_nocow(VOLUMES, True)
    todo = pending(fix=True)  # directories and empty files: in place, now
    try:
        os.remove(ATTEMPTS)  # a new request gets its two boots again
    except OSError:
        pass
    if not todo:
        return 0
    print(
        f"{len(todo)} existing volume(s) hold copy-on-write data "
        f"({sum(t[3] for t in todo) >> 20} MiB): rewritten at the next boot, before Docker starts"
    )
    return 1


def unmark() -> int:
    clear_postponed()
    if os.path.isdir(VOLUMES):
        set_nocow(VOLUMES, False)
    return 0


def status() -> int:
    if not os.path.isdir(VOLUMES):
        print(f"{VOLUMES}: absent")
        return 0
    on = bool(flags(VOLUMES) & NOCOW)
    todo = pending()
    print(
        f"{VOLUMES}: new volumes {'NOCOW' if on else 'copy-on-write'}; "
        f"{len(volumes()) - len(not_fully_nocow())} of {len(volumes())} volume(s) fully NOCOW"
    )
    if todo:
        state = "at the next boot" if os.path.exists(FLAG) else "not scheduled"
        print(f"  {len(todo)} with copy-on-write data ({sum(t[3] for t in todo) >> 20} MiB), migration {state}")
    line = postponed_line()
    if line:
        print(line)
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    fn = {"status": status, "mark": mark, "migrate": migrate, "unmark": unmark}.get(cmd)
    if not fn:
        print("usage: docker-volumes-nocow status|mark|migrate|unmark", file=sys.stderr)
        return 2
    try:
        return fn()
    except OSError as e:  # e.g. a data-root that is not on btrfs
        log(3, f"{VOLUMES}: {e}")
        return 0 if cmd in ("migrate", "unmark") else 2

"""The vm module's reach into the host: virsh, rpm, rpm-ostree, systemctl and
restic; the backup index; the NOCOW flag and the file system of the image
folders; the per-VM lock; signals and the terminal during a NAS backup.

Every path below can be moved by its CST_VM_* variable, for the tests only
(scratch folders instead of the host's).
"""

from collections.abc import Callable, Mapping, Sequence
import ctypes
import datetime
import json
import os
import pwd
import signal
import struct
import subprocess
import sys
import tempfile
import time
from types import FrameType
from typing import Any, TextIO

from cpu.domain import CPU_DISABLE_DEFAULT
from cst_core.adapters import nocow as inode_attrs
from cst_core.adapters.files import read_text
from cst_core.adapters.locks import take_lock
from cst_core.domain.errors import Refused
from cst_core.domain.install import manifest_by_src
from cst_core.domain.text import first_line, is_yes, parse_assignments
from vm.domain import (
    BTRFS_MAGIC,
    DEFAULT_VM,
    KNOBS,
    NOT_INSTALLED,
    UNIT_MARKER,
    URI,
    USER_IMAGES,
    VIRT_PACKAGES,
    is_cst_vm_program,
)

NAME = "cst-vm"

PROG = os.environ.get("CST_PROG") or NAME

CST = PROG[: -len(" vm")] if PROG.endswith(" vm") else "cst"

CPU_CONF = os.environ.get("CST_VM_CPU_CONF") or "/etc/cpu-setup/cpu-setup.conf"  # override: tests

# Overridable for the tests only (scratch folders instead of the host's).
BACKUP_DIR = os.environ.get("CST_VM_BACKUP_DIR") or "/var/lib/libvirt/cst-backups"

INDEX = os.environ.get("CST_VM_INDEX") or "/var/lib/cst/vm-backups.json"

SWTPM_DIR = os.environ.get("CST_VM_SWTPM_DIR") or "/var/lib/libvirt/swtpm"

# Settings of this module (optional file; every knob defaults to yes).
VM_CONF = os.environ.get("CST_VM_CONF") or "/etc/cst/vm.conf"  # override: tests

# VM image folders. The system one is libvirt's default storage pool.
SYSTEM_IMAGES = os.environ.get("CST_VM_SYSTEM_IMAGES") or "/var/lib/libvirt/images"  # override: tests

# The NAS backup reuses the restic repository, password and cache of the
# /home NAS backup (btrfs/btrfs-snapshot: NAS_SSH_ALIAS, RESTIC_PASSWORD,
# RESTIC_CACHE; a test checks they stay the same). The password never leaves
# its root-only file: restic reads it through RESTIC_PASSWORD_FILE.
NAS_CONF = os.environ.get("CST_VM_NAS_CONF") or "/etc/btrfs-setup/snapshots.conf"  # override: tests

RESTIC_PASSWORD = os.environ.get("CST_VM_RESTIC_PASSWORD") or "/etc/btrfs-setup/restic-password"

RESTIC_CACHE = os.environ.get("CST_VM_RESTIC_CACHE") or "/var/cache/btrfs-snapshot-restic"

# The definition and the manifest, written here for restic to read: a fixed
# path per VM, so restic's parent snapshot and forget's groups stay the same.
NAS_STAGE = os.environ.get("CST_VM_NAS_STAGE") or "/var/lib/cst/vm-nas"

RUN_DIR = os.environ.get("CST_VM_RUN_DIR") or "/run/cst-vm"  # per-VM lock (holds the pid)

NOTIFY = os.environ.get("CST_VM_NOTIFY") or "/usr/local/sbin/btrfs-notify"

# The merge back (every blockcommit of a run together; it copies the guest's
# writes of the backup's duration). The timer's TimeoutStopSec leaves room
# for it after a stop.
COMMIT_TIMEOUT = int(os.environ.get("CST_VM_COMMIT_TIMEOUT") or 4 * 3600)

# virsh snapshot-create-as; after a timeout the run waits up to
# SNAPSHOT_SETTLE seconds for libvirt to finish (or not) on its own.
SNAPSHOT_TIMEOUT = int(os.environ.get("CST_VM_SNAPSHOT_TIMEOUT") or 600)

SNAPSHOT_SETTLE = int(os.environ.get("CST_VM_SNAPSHOT_SETTLE") or 600)

# The weekly timer (backup-timer).
UNIT_DIR = os.environ.get("CST_VM_UNIT_DIR") or "/etc/systemd/system"  # override: tests

SBIN_COPY = os.environ.get("CST_VM_SBIN_COPY") or manifest_by_src()["vm/cst-vm"].dest  # as `install` places it

# True during the merge back of a NAS backup: every child then runs in its own
# session, so a Ctrl-C or a closed terminal cannot reach it (virsh cancels a
# `blockcommit --wait` on SIGINT).
DETACH = {"on": False}


def run(
    cmd: Sequence[str],
    timeout: int = 120,
    extra_env: Mapping[str, str] | None = None,
    detach: bool | None = None,
) -> tuple[int, str, str]:
    """(rc, stdout, stderr); rc 127 when the command does not exist."""
    # LC_ALL=C: virsh translates its answers ("shut off" is "desligado" in pt_BR).
    env = {**os.environ, "LC_ALL": "C", **(extra_env or {})}
    try:
        p = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
            start_new_session=DETACH["on"] if detach is None else detach,
        )
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: no answer within {timeout} s"
    return p.returncode, p.stdout, p.stderr


def virsh(*args: str, timeout: int = 60, detach: bool | None = None) -> tuple[int, str, str]:
    return run(["virsh", "-c", URI, *args], timeout=timeout, detach=detach)


def read_conf(path: str) -> dict[str, str]:
    """The settings of a KEY=VALUE file ({} when it cannot be read)."""
    return parse_assignments(read_text(path, ""))


def default_vm() -> str:
    return read_conf(CPU_CONF).get("VM_NAME") or DEFAULT_VM


def cpu_disable_list(conf_path: str | None = None) -> list[str]:
    """The features vm-pin keeps disabled: VM_CPU_DISABLE from cpu-setup.conf
    when set there (also empty), else vm-pin's default (cpu.domain)."""
    conf = read_conf(conf_path or CPU_CONF)
    if "VM_CPU_DISABLE" in conf:
        return conf["VM_CPU_DISABLE"].split()
    return CPU_DISABLE_DEFAULT.split()


def utc_now() -> tuple[float, str]:
    now = float(os.environ.get("CST_VM_NOW") or time.time())
    return now, datetime.datetime.fromtimestamp(now, datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def qcow2_backing_file(path: str) -> bool:
    """True when path is a qcow2 image with a backing file (an overlay: the
    copy would be useless without the base), False otherwise."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return len(head) == 16 and head[:4] == b"QFI\xfb" and struct.unpack(">Q", head[8:16])[0] != 0


def fingerprint(path: str) -> tuple[Any, ...]:
    """What changes when something writes to a file or a folder tree, or when
    libvirt relabels it on start (ctime): compared before and after the copy."""

    def one(p: str) -> tuple[int, int, int, int]:
        st = os.lstat(p)
        return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

    if os.path.isdir(path) and not os.path.islink(path):
        out: list[tuple[str, tuple[int, int, int, int] | None]] = [("", one(path))]
        for d, dirs, files in os.walk(path):
            dirs.sort()
            for n in sorted(dirs + files):
                q = os.path.join(d, n)
                try:
                    out.append((os.path.relpath(q, path), one(q)))
                except OSError:
                    out.append((os.path.relpath(q, path), None))
        return tuple(out)
    return one(path)


def tree_sizes(path: str) -> tuple[int, int]:
    """(allocated, apparent) bytes of a file or a folder tree."""
    alloc = apparent = 0
    if os.path.isdir(path) and not os.path.islink(path):
        for d, _, files in os.walk(path):
            for n in files:
                try:
                    st = os.lstat(os.path.join(d, n))
                except OSError:
                    continue
                alloc += st.st_blocks * 512
                apparent += st.st_size
        return alloc, apparent
    st = os.stat(path)
    return st.st_blocks * 512, st.st_size


def package_versions() -> dict[str, str]:
    """{package: name-version-release.arch or "not installed"}, one rpm -q each
    (its exit code tells whether it is installed)."""
    out: dict[str, str] = {}
    for p in VIRT_PACKAGES:
        rc, so, _ = run(["rpm", "-q", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\\n", p], timeout=30)
        out[p] = first_line(so) if rc == 0 and first_line(so) else NOT_INSTALLED
    return out


def booted_deployment() -> dict[str, Any] | None:
    """{version, checksum, timestamp} of the booted rpm-ostree deployment, or None."""
    rc, so, _ = run(["rpm-ostree", "status", "--json"], timeout=60)
    if rc != 0:
        return None
    try:
        deps = json.loads(so).get("deployments") or []
        d = next(x for x in deps if isinstance(x, dict) and x.get("booted"))
    except ValueError, StopIteration, AttributeError:
        return None
    return {"version": d.get("version"), "checksum": d.get("checksum"), "timestamp": d.get("timestamp")}


def read_index(path: str | None = None) -> list[dict[str, Any]]:
    path = path or INDEX
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError, ValueError:
        return []
    entries = data.get("backups") if isinstance(data, dict) else None
    return [
        e
        for e in entries or []
        if isinstance(e, dict) and e.get("vm") and (e.get("path") or e.get("mode") == "nas" and e.get("snapshot"))
    ]


def write_index(entries: Sequence[Mapping[str, Any]], path: str | None = None) -> None:
    """Atomically (temporary file + rename), mode 0644, folder 0755."""
    path = path or INDEX
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o755)
    fd, tmp = tempfile.mkstemp(prefix=".vm-backups.", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "backups": list(entries)}, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def flag_state(flags: int | None) -> bool | None:
    return None if flags is None else bool(flags & inode_attrs.FS_NOCOW_FL)


def nocow(path: str) -> bool:
    return flag_state(readable_inode_flags(path)) is True


def precreate_nocow(dest: str) -> None:
    """btrfs clones only between files with the same NOCOW flag: an empty
    destination with the flag set, which cp then fills. Best effort."""
    os.close(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    try:
        inode_attrs.set_nocow(dest)
    except OSError:
        pass


class Stop(Exception):
    """SIGTERM/SIGHUP/SIGINT (systemd stopping the unit, a closed terminal,
    Ctrl-C): raised so the overlays are always committed back before the
    program ends."""


STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


def on_signal(signum: int, _frame: FrameType | None) -> None:
    # A second signal must not cut the cleanup short: ignored from here on.
    for s in STOP_SIGNALS:
        signal.signal(s, signal.SIG_IGN)
    raise Stop(f"stopped by signal {signal.Signals(signum).name}")


class signals_held:
    """SIGTERM, SIGHUP and SIGINT wait (blocked) until the block ends, so a
    stop never kills `virsh snapshot-create-as` while libvirt goes on making
    the snapshot: the run then always knows what the VM runs on."""

    def __enter__(self) -> signals_held:
        self.old = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        return self

    def __exit__(self, *_: object) -> None:
        signal.pthread_sigmask(signal.SIG_SETMASK, self.old)


class SafeStream:
    """stdout/stderr of a NAS backup: a closed terminal (EIO) or a reader
    that went away (EPIPE: `| head`) must never cut the merge back or the
    alert short. On the first failed write both fds go to /dev/null."""

    def __init__(self, inner: TextIO) -> None:
        self.inner = inner

    def write(self, text: str) -> int:
        try:
            return self.inner.write(text)
        except OSError, ValueError:
            mute()
            return len(text)

    def flush(self) -> None:
        try:
            self.inner.flush()
        except OSError, ValueError:
            mute()

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)


def mute() -> None:
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    for n in (1, 2):
        try:
            os.dup2(fd, n)
        except OSError:
            pass
    os.close(fd)


def safe_output() -> None:
    if not isinstance(sys.stdout, SafeStream):
        sys.stdout, sys.stderr = SafeStream(sys.stdout), SafeStream(sys.stderr)


def restic(nas: Mapping[str, Any], *args: str, timeout: int = 1800) -> tuple[int, str, str]:
    return run(["restic", *args], timeout=timeout, extra_env=nas["env"])


def blklist(vm: str, inactive: bool = False) -> tuple[dict[str, str] | None, str]:
    """({target: source}, problem) from virsh domblklist --details."""
    rc, so, err = virsh("domblklist", vm, "--details", *(["--inactive"] if inactive else []))
    if rc != 0:
        return None, first_line(err) or f"exit {rc}"
    out: dict[str, str] = {}
    for ln in so.splitlines():
        parts = ln.split(None, 3)
        if len(parts) == 4 and parts[0] != "Type" and not parts[0].startswith("-"):
            out[parts[2]] = parts[3].strip()
    return out, ""


def notify(urgency: str, title: str, body: str) -> None:
    """A CST desktop alert (btrfs-notify keeps it for the next login when no
    session shows it). Sent first, then printed (best effort)."""
    if os.access(NOTIFY, os.X_OK):
        run([NOTIFY, "-u", urgency, "-a", NAME, title, body], timeout=60, detach=True)
    print(f"{title}: {body}", file=sys.stderr)


def domain_state_or_none(vm: str) -> str | None:
    rc, so, _ = virsh("domstate", vm)
    return first_line(so) if rc == 0 else None


class VmLock:
    """One NAS backup per VM at a time (the timer and a manual run). The
    lock file holds the pid: cst doctor reads it to tell a backup in
    progress from an overlay left behind."""

    def __init__(self, vm: str) -> None:
        self.path = os.path.join(RUN_DIR, f"nas-{vm}.lock")
        self.fd: int | None = None

    def __enter__(self) -> VmLock:
        os.makedirs(RUN_DIR, mode=0o755, exist_ok=True)
        fd = take_lock(self.path, wait=False, mode=0o644, nofollow=False)
        if fd is None:
            raise Refused(f"another NAS backup of this VM is running ({self.path})")
        self.fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.ftruncate(self.fd, 0)
            os.close(self.fd)
            self.fd = None


def is_ours(path: str, data: bytes) -> bool:
    """Whether the file at PATH with these bytes is CST's: a unit with CST's
    marker, or (at SBIN_COPY) a copy of cst-vm from any CST version."""
    return UNIT_MARKER.encode() in data or path == SBIN_COPY and is_cst_vm_program(data)


def wants_links() -> list[str]:
    d = os.path.join(UNIT_DIR, "timers.target.wants")
    try:
        return sorted(n for n in os.listdir(d) if n.startswith("cst-vm-backup@") and n.endswith(".timer"))
    except OSError:
        return []


def systemctl(*args: str) -> tuple[int, str, str]:
    rc, so, err = run(["systemctl", *args], timeout=120)
    return rc, so.strip(), first_line(err)


def write_file(path: str, data: bytes, mode: int) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix="." + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def describe_entry(e: Mapping[str, Any]) -> str:
    dep = e.get("deployment") or {}
    qemu = next(
        (
            v
            for k, v in (e.get("versions") or {}).items()
            if k.startswith(("qemu-kvm", "qemu-system-x86")) and v != NOT_INSTALLED
        ),
        "?",
    )
    if e.get("mode") == "nas":
        keep = f", kept: {e['keep']}" if e.get("keep") else ""
        return (
            f"{e['vm']:<16} {e['timestamp']}  nas      deployment {dep.get('version') or '?'}"
            f"  {qemu}  restic {str(e.get('snapshot'))[:8]} ({e.get('consistency') or '?'}{keep})"
        )
    gone = ""
    try:
        os.stat(e["path"])
    except FileNotFoundError:
        gone = "  (folder gone)"
    except OSError:
        pass  # not visible to this user (the folders are root's, 0700)
    return (
        f"{e['vm']:<16} {e['timestamp']}  {e.get('mode', '?'):<7}  deployment {dep.get('version') or '?'}"
        f"  {qemu}  {e['path']}{gone}"
    )


def knobs(path: str | None = None) -> dict[str, bool]:
    """{knob: bool} from /etc/cst/vm.conf; a missing file or knob is yes."""
    conf = read_conf(path or VM_CONF)
    return {k: conf.get(k, "yes").strip().lower() not in ("no", "0", "false", "off") for k in KNOBS}


def user_home() -> str:
    """The home of the user who runs CST: under sudo, SUDO_USER's (sudo may
    set HOME to /root), else HOME."""
    if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
        try:
            return pwd.getpwnam(os.environ["SUDO_USER"]).pw_dir
        except KeyError:
            pass
    return os.path.expanduser("~")


def is_btrfs(path: str) -> bool | None:
    """True/False, or None when statfs fails (statfs needs only search
    permission, so a root folder of mode 0711 still answers)."""
    buf = ctypes.create_string_buffer(256)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.statfs(os.fsencode(path), buf) != 0:
            return None
    except OSError, AttributeError:
        return None
    return struct.unpack_from("l", buf.raw, 0)[0] == BTRFS_MAGIC


def readable_inode_flags(path: str) -> int | None:
    """The inode flags (FS_IOC_GETFLAGS), or None when this user may not read
    them: the system folder is root's (0711) and the images are qemu's (0600).
    Unreadable is not "copy-on-write": callers say "not readable"."""
    try:
        return inode_attrs.inode_flags(path)
    except OSError:
        return None


def mark_folder_nocow(path: str) -> None:
    """Set +C on a folder this user owns (files created in it later inherit
    it). Raises OSError."""
    inode_attrs.set_nocow(path, directory=True)


def image_dirs(home: str | None = None) -> list[dict[str, str]]:
    """[{scope, path, what}] of every known VM image folder."""
    home = home or user_home()
    out = [{"scope": "system", "path": SYSTEM_IMAGES, "what": "libvirt qemu:///system: virt-manager, virsh"}]
    out += [{"scope": "user", "path": os.path.join(home, rel), "what": what} for rel, what in USER_IMAGES]
    return out


def probe_dir(d: Mapping[str, Any], readable: Callable[[str], str] = lambda p: p) -> dict[str, Any]:
    """What a VM image folder looks like. readable maps a path to where this
    process reads it (the doctor in a toolbox: under /run/host). Adds:
    present, link (the target when the path is a symbolic link: never
    created, marked or listed; present is then False), btrfs (None: unknown), nocow (True/False/None: not readable),
    listed (False: the folder cannot be listed by this user), images
    [{name, path, alloc, size, nocow}] (regular files, not .iso, not the
    *.cow-old leftovers of a conversion) and leftovers [names]."""
    res: dict[str, Any] = dict(d)
    rp = readable(res["path"])
    link = os.readlink(rp) if os.path.islink(rp) else None
    res.update(
        present=link is None and os.path.isdir(rp),
        link=link,
        btrfs=None,
        nocow=None,
        listed=False,
        images=[],
        leftovers=[],
    )
    if not res["present"]:
        return res
    res["btrfs"] = is_btrfs(rp)
    if res["btrfs"] is False:
        return res
    res["nocow"] = flag_state(readable_inode_flags(rp))
    try:
        entries = sorted(os.scandir(rp), key=lambda e: e.name)
    except OSError:
        return res
    res["listed"] = True
    for e in entries:
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if e.name.endswith(".cow-old"):
            res["leftovers"].append(e.name)
            continue
        if e.name.lower().endswith(".iso"):
            continue  # read-only installers: nothing to gain
        res["images"].append(
            {
                "name": e.name,
                "path": os.path.join(res["path"], e.name),
                "alloc": st.st_blocks * 512,
                "size": st.st_size,
                "nocow": flag_state(readable_inode_flags(os.path.join(rp, e.name))),
            }
        )
    return res


def needs_mark(d: Mapping[str, Any]) -> bool:
    """True when nocow apply would act on the folder (also: unknown state of
    the system folder, or the system folder missing while libvirt is there).
    A symbolic link is never acted on (install -d and chattr would follow it)."""
    if d.get("link") is not None or os.path.lexists(d["path"]) and not d["present"]:
        return False
    if d["scope"] == "system" and not d["present"]:
        return os.path.isdir(os.path.dirname(d["path"]))
    return d["present"] and d["btrfs"] is not False and d["nocow"] is not True


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        raise Refused("apply asks for confirmation: run it in a terminal")
    return is_yes(input(f"{question} [y/N] "))


def domain_state(vm: str) -> str:
    rc, so, err = virsh("domstate", vm)
    if rc != 0:
        raise Refused(f"virsh domstate {vm}: {first_line(err) or f'exit {rc}'}")
    return first_line(so)


def has_managed_save(vm: str) -> bool:
    rc, so, err = virsh("list", "--all", "--with-managed-save", "--name")
    if rc != 0:
        raise Refused(f"virsh list --with-managed-save: {first_line(err) or f'exit {rc}'}")
    return vm in (ln.strip() for ln in so.splitlines())

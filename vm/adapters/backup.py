# SPDX-License-Identifier: MPL-2.0
"""The backups' files and the run of a backup: the index, the fingerprints and sizes of what
is copied, the qcow2 check, the stop signals and the terminal of a NAS backup, its lock and alert."""

from collections.abc import Mapping, Sequence
import datetime
import json
import os
import signal
import struct
import sys
import tempfile
import time
from types import FrameType
from typing import Any, TextIO

from cst_core.adapters import nocow as inode_attrs
from cst_core.adapters.files import ensure_dir, write_atomic
from cst_core.adapters.locks import take_lock
from cst_core.adapters.notify import Notifier, Urgency
from cst_core.domain.errors import Refused
from vm.adapters import INDEX_FORMAT, NAME, RUNNER
from vm.domain import NOT_INSTALLED, VmSettings
from vm.ports import VmCommandsPort


def utc_now(override: str | None = None) -> tuple[float, str]:
    """(seconds, its UTC stamp) of now, or of override (settings.now: CST_VM_NOW, tests)."""
    now = float(override or time.time())
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


def read_index(cfg: VmSettings) -> list[dict[str, Any]]:
    """The backups in the index ([] when there is none or it cannot be read). Refused for an index of a
    newer cst vm ("version" above INDEX_FORMAT's): listing it could mislead, and writing it back would
    lose what this one does not know. The index is cfg.paths.index."""
    path = cfg.paths.index
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError, ValueError:
        return []
    if (found := INDEX_FORMAT.newer(data, "version")) is not None:
        raise Refused(
            f"{INDEX_FORMAT.newer_text(path, found)}: left as it is, and nothing is backed up or listed "
            f"with it; use the cst vm of that CST (in that checkout: {cfg.cst} vm backup-timer on installs it)"
        )
    entries = data.get("backups") if isinstance(data, dict) else None
    return [
        e
        for e in entries or []
        if isinstance(e, dict) and e.get("vm") and (e.get("path") or e.get("mode") == "nas" and e.get("snapshot"))
    ]


def check_backup_start(s: VmSettings, what: str) -> None:
    """Before a backup copies anything: root (or the tests' override), and an index this cst vm can
    record it in (read_index refuses one of a newer cst vm)."""
    if os.geteuid() != 0 and not s.nonroot_ok:
        raise Refused(f"{what} needs root: run it as `cst vm {what}` (it calls sudo by itself)")
    read_index(s)


def write_index(entries: Sequence[Mapping[str, Any]], path: str) -> None:
    """Atomically (temporary file + rename), mode 0644, folder 0755; durable
    (the file and the folder fsynced): a lost index hides a finished backup."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o755)
    text = json.dumps({"version": INDEX_FORMAT.version, "backups": list(entries)}, indent=2, sort_keys=True) + "\n"
    write_atomic(path, text, mode=0o644, relabel=False)


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


def notify(s: VmSettings, urgency: Urgency, title: str, body: str) -> None:
    """A CST desktop alert (btrfs-notify keeps it for the next login when no
    session shows it; without btrfs-notify, cst_core keeps it there and says
    so). Sent first, then printed."""
    Notifier(
        RUNNER, s.paths.notify, s.paths.alerts, log=lambda _prio, msg: print(f"{s.prog}: {msg}", file=sys.stderr)
    ).send(urgency, NAME, title, body)
    print(f"{title}: {body}", file=sys.stderr)


class VmLock:
    """One NAS backup per VM at a time (the timer and a manual run). The
    lock file holds the pid: cst doctor reads it to tell a backup in
    progress from an overlay left behind."""

    def __init__(self, vm: str, run_dir: str) -> None:
        self.run_dir = run_dir
        self.path = os.path.join(run_dir, f"nas-{vm}.lock")
        self.fd: int | None = None

    def __enter__(self) -> VmLock:
        # 0755 and 0644 exactly, whatever the umask (sudo keeps a user's 077): cst doctor reads the pid.
        ensure_dir(self.run_dir, 0o755)
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


class DefinitionFiles:
    """DefinitionStorePort on this machine: the definitions tune keeps under
    BACKUP_DIR/VM/definitions/ (folders 0700, the file 0600 from the start: it holds
    --security-info secrets, graphics passwords), and virsh define through a temporary file."""

    def __init__(self, backup_dir: str, commands: VmCommandsPort) -> None:
        self.backup_dir = backup_dir
        self.commands = commands

    def save(self, vm: str, stamp: str, text: str) -> str:
        keep = os.path.join(self.backup_dir, vm, "definitions")
        for d in (self.backup_dir, os.path.join(self.backup_dir, vm), keep):
            os.makedirs(d, mode=0o700, exist_ok=True)
            os.chmod(d, 0o700)
        saved = os.path.join(keep, f"{stamp}.before-tune.xml")
        with os.fdopen(os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(text)
        return saved

    def define(self, text: str) -> tuple[int, str, str]:
        fd, tmp = tempfile.mkstemp(prefix="cst-vm-tune.", suffix=".xml")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(text)
            return self.commands.virsh(("define", tmp), 60, detach=None)
        finally:
            os.unlink(tmp)

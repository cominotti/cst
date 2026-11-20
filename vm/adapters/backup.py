# SPDX-License-Identifier: MPL-2.0
"""The backups' files and the run of a backup: the index, the fingerprints and sizes of what
is copied, the qcow2 check, the stop signals and the terminal of a NAS backup, its lock and alert."""

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager, suppress
import json
import os
import shutil
import signal
import struct
import sys
from types import FrameType
from typing import Final, Self, TextIO

from cst_core.adapters import nocow as inode_attrs
from cst_core.adapters.files import LocalFileSystemAdapter, create_private, ensure_dir, write_atomic
from cst_core.adapters.locks import take_lock
from cst_core.adapters.notify import Notifier
from cst_core.domain.contracts import Urgency
from cst_core.domain.errors import Refused
from cst_core.domain.shapes import parse_json
from cst_core.domain.text import first_line
from cst_core.domain.values import HOUR
from vm.adapters import INDEX_FORMAT, NAME, RUNNER, TORN_FORMAT, run
from vm.adapters.images import nocow
from vm.domain import NOT_INSTALLED, VmSettings
from vm.domain.backup import (
    DOMAIN_XML,
    MANIFEST_JSON,
    NO_BACKUPS_LIST,
    NO_TORN_LIST,
    TORN_LOCK_TIMEOUT,
    Fingerprint,
    IndexEntry,
    IndexRefused,
    Stop,
    TornEntry,
    TornRefused,
    is_index_entry,
    is_torn_entry,
    stored_list,
    stored_torn,
)


def qcow2_backing_file(path: str) -> bool:
    """True when path is a qcow2 image with a backing file (an overlay: the
    copy would be useless without the base), False otherwise."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return len(head) == 16 and head[:4] == b"QFI\xfb" and struct.unpack(">Q", head[8:16])[0] != 0


def fingerprint(path: str) -> Fingerprint:
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


def stored_backups(cfg: VmSettings) -> list[object]:
    """The "backups" list of the index as stored ([] when there is no index). IndexRefused, naming the file,
    when the index is there but cannot be read as one (an error other than a missing file, not JSON,
    not an object with a "backups" list): reading it as empty would let the next backup write an index
    without every older entry. IndexRefused for an index of a newer cst vm ("version" above INDEX_FORMAT's):
    listing it could mislead, and writing it back would lose what this one does not know. The index is
    cfg.paths.index."""
    path = cfg.paths.index
    try:
        data = load_json_file(path, _unreadable)
    except FileNotFoundError:
        return []
    if (found := INDEX_FORMAT.newer(data, "version")) is not None:
        raise IndexRefused(
            INDEX_FORMAT.newer_text(path, found),
            f"use the cst vm of that CST (in that checkout: {cfg.cst} vm backup-timer on installs it)",
        )
    backups = stored_list(data)
    if backups is None:
        raise _unreadable(path, NO_BACKUPS_LIST)
    return backups


def load_json_file(path: str, unreadable: Callable[[str, str], Refused]) -> object:
    """The JSON a state file of cst vm holds, parsed. FileNotFoundError when there is none; unreadable(path,
    why) raised when it is there but cannot be read (why: the error) or is not JSON (bytes that are not
    UTF-8 included)."""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        raise
    except OSError as e:
        raise unreadable(path, e.strerror or str(e)) from None
    except UnicodeDecodeError:  # a ValueError, not an OSError: bytes that are not text are not JSON
        raise unreadable(path, "not JSON") from None
    try:
        return parse_json(text)
    except ValueError, RecursionError:
        raise unreadable(path, "not JSON") from None


def _unreadable(path: str, why: str) -> IndexRefused:
    return IndexRefused(
        f"{path} cannot be read as cst vm's backup index ({why})",
        f"move it aside (mv {path} {path}.bad) to start a new index",
    )


def read_index(cfg: VmSettings) -> list[IndexEntry]:
    """The backups in the index that this cst vm understands (is_index_entry); [] when there is no
    index. Refused as stored_backups is."""
    return [e for e in stored_backups(cfg) if is_index_entry(e)]


def write_index(entries: Sequence[IndexEntry], path: str, carried: Sequence[object] = ()) -> None:
    """ENTRIES, then CARRIED (the stored entries this cst vm does not understand, kept as they are),
    atomically (temporary file + rename), mode 0644, folder 0755; durable (the file and the folder
    fsynced): a lost index hides a finished backup."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o755)
    doc = {"version": INDEX_FORMAT.version, "backups": [*entries, *carried]}
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    write_atomic(path, text, mode=0o644, relabel=False)


# The lock of the torn record, in RUN_DIR: the record is shared by every VM, and the NAS backups of two
# VMs (two timers) can run at once; the per-VM lock does not cover that. Outside the nas-<vm>.lock names
# of VmLock, so a VM's own lock (a VM named "torn" included) is never this one.
TORN_LOCK: Final = "torn-record.lock"


def _torn_unreadable(path: str, why: str) -> TornRefused:
    return TornRefused(
        f"{path} cannot be read as cst vm's record of torn NAS snapshots ({why}): left as it is, and no NAS "
        "backup runs with it (its retention could count a torn snapshot as a backup); forget the snapshots "
        f"it names by hand (restic forget ID), then move it aside (mv {path} {path}.bad)"
    )


def stored_torn_entries(cfg: VmSettings) -> list[object]:
    """The "torn" list of the record (cfg.paths.torn) as stored; [] when there is none. TornRefused,
    naming the file, when it is there but cannot be read as the record, or a newer cst vm wrote it:
    reading it as empty would let the retention count a torn snapshot."""
    path = cfg.paths.torn
    try:
        data = load_json_file(path, _torn_unreadable)
    except FileNotFoundError:
        return []
    if (found := TORN_FORMAT.newer(data, "version")) is not None:
        raise TornRefused(
            f"{TORN_FORMAT.newer_text(path, found)}: left as it is, and no NAS backup runs with it; use the "
            f"cst vm of that CST (in that checkout: {cfg.cst} vm backup-timer on installs it)"
        )
    torn = stored_torn(data)
    if torn is None:
        raise _torn_unreadable(path, NO_TORN_LIST)
    return torn


def read_torn(cfg: VmSettings) -> list[TornEntry]:
    """The torn snapshots in the record that this cst vm understands; refused as stored_torn_entries."""
    return [e for e in stored_torn_entries(cfg) if is_torn_entry(e)]


def _same_torn(a: TornEntry, b: TornEntry) -> bool:
    return (a["snapshot"], a["vm"], a["repository"]) == (b["snapshot"], b["vm"], b["repository"])


def change_torn(
    cfg: VmSettings, change: Callable[[list[object]], list[object]], *, lock_timeout: float = TORN_LOCK_TIMEOUT
) -> None:
    """Read the record, change its entries, write it back: under the record's lock (RUN_DIR/TORN_LOCK),
    so two VMs' backups never lose each other's entry; atomically and durably, 0644 (cst doctor reads
    it as the user), folder 0755. TornRefused when the lock stays taken lock_timeout seconds, or as
    stored_torn_entries; OSError when it cannot be written."""
    ensure_dir(cfg.paths.run_dir, 0o755)
    lock = os.path.join(cfg.paths.run_dir, TORN_LOCK)
    fd = take_lock(lock, wait=True, mode=0o644, timeout=lock_timeout)
    if fd is None:
        raise TornRefused(f"{lock} stayed taken for {lock_timeout:g} s (another NAS backup)")
    try:
        entries = change(stored_torn_entries(cfg))
        ensure_dir(os.path.dirname(cfg.paths.torn), 0o755)
        doc = {"version": TORN_FORMAT.version, "torn": entries}
        write_atomic(cfg.paths.torn, json.dumps(doc, indent=2, sort_keys=True) + "\n", mode=0o644, relabel=False)
    finally:
        os.close(fd)


def record_torn(cfg: VmSettings, entry: TornEntry, *, lock_timeout: float = TORN_LOCK_TIMEOUT) -> None:
    """entry added to the record (once: the same snapshot of the same VM and repository is there once)."""
    change_torn(
        cfg,
        lambda stored: stored if any(is_torn_entry(e) and _same_torn(e, entry) for e in stored) else [*stored, entry],
        lock_timeout=lock_timeout,
    )


def drop_torn(cfg: VmSettings, entries: Sequence[TornEntry], *, lock_timeout: float = TORN_LOCK_TIMEOUT) -> None:
    """entries taken out of the record (matched by snapshot, VM and repository); the others are kept as
    they are, those this cst vm does not understand included."""
    change_torn(
        cfg,
        lambda stored: [e for e in stored if not (is_torn_entry(e) and any(_same_torn(e, d) for d in entries))],
        lock_timeout=lock_timeout,
    )


def precreate_nocow(dest: str) -> None:
    """btrfs clones only between files with the same NOCOW flag: an empty
    destination with the flag set, which cp then fills. Best effort."""
    create_private(dest, "")
    with suppress(OSError):
        inode_attrs.set_nocow(dest)


def copy(cfg: VmSettings, src: str, dest: str, mode: str, big: bool) -> None:
    """Copy src (a file or a folder) to dest with cp -a. Disks (big) follow
    the mode; the small parts (UEFI variables, TPM state) are plain copies."""
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    if not big:
        flags = ["--reflink=never"] if mode == "full" else []
    elif mode == "reflink":
        flags = ["--reflink=always"]  # cp allows --reflink only with --sparse=auto
        if os.path.isfile(src) and nocow(src):
            precreate_nocow(dest)
    else:
        # --sparse=auto keeps the source's holes only. --sparse=always also turns
        # every run of zero blocks inside the data into a hole: a 92 GiB image
        # became 1.05 million extents (the source had 0.6 million), and the guest
        # writing into those holes allocates new extents even on a NOCOW file.
        flags = ["--reflink=never", "--sparse=auto"]
    rc, _, err = run(["cp", *flags, "-a", "--", src, dest], timeout=6 * HOUR)
    if rc != 0:
        raise Refused(
            f"cp {' '.join(flags)} {src}: {first_line(err) or f'exit {rc}'}"
            + (
                f"\nReflink copies need the backup folder on the same btrfs filesystem as the "
                f"disks. Use real copies instead: {cfg.prog} backup VM --full"
                if big and mode == "reflink"
                else ""
            )
        )


def private_dirs(*folders: str) -> None:
    """Each folder, in order, made 0700 (also when it exists: the backups hold secrets)."""
    for d in folders:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)


class BackupFiles(LocalFileSystemAdapter):
    """BackupStorePort on this machine: the paths of cfg, the functions of this module (and the
    path tests of cst_core's LocalFileSystemAdapter: os.path.isfile, isdir, exists, lexists)."""

    def __init__(self, cfg: VmSettings) -> None:
        self.cfg = cfg

    def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    def has_backing_file(self, path: str) -> bool:
        return qcow2_backing_file(path)

    def sizes(self, path: str) -> tuple[int, int]:
        return tree_sizes(path)

    def fingerprint(self, path: str) -> Fingerprint:
        return fingerprint(path)

    def free_space(self, path: str) -> tuple[str, int]:
        probe = path
        while not os.path.exists(probe):
            probe = os.path.dirname(probe)
        st = os.statvfs(probe)
        return probe, st.f_bavail * st.f_frsize

    def make_backup_folder(self, backup_dir: str, vm_dir: str, dest_dir: str) -> None:
        private_dirs(backup_dir, vm_dir)
        os.mkdir(dest_dir, 0o700)

    def copy(self, src: str, dest: str, mode: str, *, big: bool) -> None:
        copy(self.cfg, src, dest, mode, big)

    def write_private(self, path: str, text: str) -> None:
        create_private(path, text)

    def remove_tree(self, path: str) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def stage(self, vm: str, xml_text: str, manifest: str) -> str:
        """domain.xml (with --security-info: graphics passwords; restic stores it
        encrypted) and manifest.json, 0600 in a 0700 folder. Returns the folder."""
        d = os.path.join(self.cfg.paths.nas_stage, vm)
        private_dirs(self.cfg.paths.nas_stage, d)
        for name, text in ((DOMAIN_XML, xml_text), (MANIFEST_JSON, manifest)):
            path = os.path.join(d, name)
            with suppress(FileNotFoundError):
                os.unlink(path)
            create_private(path, text)
        return d

    def unstage(self, folder: str) -> None:
        for name in (DOMAIN_XML, MANIFEST_JSON):
            with suppress(OSError):
                os.unlink(os.path.join(folder, name))

    def remove_overlay(self, path: str) -> str | None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            return str(e.strerror)
        return None

    def drop(self, path: str) -> None:
        with suppress(OSError):
            os.unlink(path)

    def describe(self, entry: IndexEntry) -> str:
        return describe_entry(entry)

    def read_index(self) -> list[IndexEntry]:
        return read_index(self.cfg)

    def read_torn(self) -> list[TornEntry]:
        return read_torn(self.cfg)

    def record_torn(self, entry: TornEntry) -> None:
        record_torn(self.cfg, entry)

    def drop_torn(self, entries: Sequence[TornEntry]) -> None:
        drop_torn(self.cfg, entries)

    def write_index(self, entries: Sequence[IndexEntry]) -> None:
        # Read again (the caller holds the VM's lock, as when it read): the entries this cst vm does
        # not understand were never shown to the caller, so none of them is dropped.
        carried = [e for e in stored_backups(self.cfg) if not is_index_entry(e)]
        write_index(entries, self.cfg.paths.index, carried)


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

    def __enter__(self) -> Self:
        self.old = signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
        return self

    def __exit__(self, *_: object) -> None:
        signal.pthread_sigmask(signal.SIG_SETMASK, self.old)


class StopSignals:
    """SignalsPort of this process: on_signal for the stops, signals_held, and the stops ignored
    during a block (the handlers before it put back after it)."""

    def catch_stops(self) -> None:
        for sig in STOP_SIGNALS:
            signal.signal(sig, on_signal)

    def held(self) -> signals_held:
        return signals_held()

    @contextmanager
    def ignored(self) -> Generator[None]:
        old = {s: signal.signal(s, signal.SIG_IGN) for s in STOP_SIGNALS}
        try:
            yield
        finally:
            for s, h in old.items():
                if h is not None:
                    signal.signal(s, h)


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
        with suppress(OSError):
            os.dup2(fd, n)
    os.close(fd)


def safe_output() -> None:
    if not isinstance(sys.stdout, SafeStream):
        sys.stdout, sys.stderr = SafeStream(sys.stdout), SafeStream(sys.stderr)


class TerminalLines:
    """ProgressPort on this process's terminal: each line printed when it comes, to sys.stdout or
    sys.stderr as they are then (a backup's SafeStream, see safe_output)."""

    def say(self, text: str, *, flush: bool = False) -> None:
        print(text, flush=flush)

    def warn(self, text: str) -> None:
        print(text, file=sys.stderr)


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

    def __enter__(self) -> Self:
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


def describe_entry(e: IndexEntry) -> str:
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
        keep = f", kept: {kept}" if (kept := e.get("keep")) is not None and kept != "" else ""
        return (
            f"{e['vm']:<16} {e.get('timestamp') or '?'}  nas      deployment {dep.get('version') or '?'}"
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
        f"{e['vm']:<16} {e.get('timestamp') or '?'}  {e.get('mode', '?'):<7}  deployment {dep.get('version') or '?'}"
        f"  {qemu}  {e['path']}{gone}"
    )


class DefinitionFiles:
    """DefinitionStorePort on this machine: the definitions tune keeps under
    BACKUP_DIR/VM/definitions/ (folders 0700, the file 0600 from the start: it holds
    --security-info secrets, graphics passwords)."""

    def __init__(self, backup_dir: str) -> None:
        self.backup_dir = backup_dir

    def save(self, vm: str, stamp: str, text: str) -> str:
        keep = os.path.join(self.backup_dir, vm, "definitions")
        private_dirs(self.backup_dir, os.path.join(self.backup_dir, vm), keep)
        saved = os.path.join(keep, f"{stamp}.before-tune.xml")
        create_private(saved, text)
        return saved

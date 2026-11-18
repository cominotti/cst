# SPDX-License-Identifier: MPL-2.0
"""The vm module's reach into the host: virsh, rpm, rpm-ostree, systemctl and
restic; the backup index; the NOCOW flag and the file system of the image
folders; the per-VM lock; signals and the terminal during a NAS backup.

Every path below can be moved by its CST_VM_* variable, for the tests only
(scratch folders instead of the host's).
"""

from collections.abc import Callable, Mapping, Sequence
import ctypes
from dataclasses import replace
import datetime
import functools
import json
import os
import pwd
import signal
import struct
import sys
import time
from types import FrameType
from typing import Any, TextIO

from cpu.domain import CPU_DISABLE_DEFAULT
from cst_core.adapters import nocow as inode_attrs
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.virsh import SubprocessVirshAdapter
from cst_core.adapters.files import (
    ensure_dir,
    is_masked_unit,
    owned_file_state_at,
    read_text,
    unit_file_state_at,
    write_atomic,
)
from cst_core.adapters.locks import take_lock
from cst_core.adapters.install import program_installer
from cst_core.adapters.notify import PENDING_ALERTS, Notifier, Urgency
from cst_core.adapters.runtime import install_payload, restore_default_signals
from cst_core.application.install import InstallService, Placement
from cst_core.domain.contracts import NOTIFY_PROGRAM, state_format
from cst_core.domain.errors import Refused
from cst_core.domain.files import UnitFileState
from cst_core.domain.install import manifest_by_src
from cst_core.domain.record import ROOT_RECORD
from cst_core.ports import CommandResult, CommandRunnerPort, VirshPort
from cst_core.domain.text import (
    ShellAssignments,
    describe_problems,
    first_line,
    parse_shell_assignments,
    parse_snapshots_conf,
)
from vm.domain import (
    BTRFS_MAGIC,
    DEFAULT_VM,
    KNOBS,
    NAS_DEFAULTS,
    NOT_INSTALLED,
    SERVICE,
    TIMER,
    UNIT_MARKER,
    URI,
    USER_IMAGES,
    VIRT_PACKAGES,
    ImageDir,
    ImageInfo,
    ImageProbe,
    is_cst_vm_program,
    render_service,
    render_timer,
)

NAME = "cst-vm"

PROG = os.environ.get("CST_PROG") or NAME

CST = PROG[: -len(" vm")] if PROG.endswith(" vm") else "cst"

CPU_CONF = os.environ.get("CST_VM_CPU_CONF") or "/etc/cpu-setup/cpu-setup.conf"  # override: tests

# Overridable for the tests only (scratch folders instead of the host's).
BACKUP_DIR = os.environ.get("CST_VM_BACKUP_DIR") or "/var/lib/libvirt/cst-backups"

INDEX_FORMAT = state_format("vm-index")
INDEX = os.environ.get("CST_VM_INDEX") or INDEX_FORMAT.path

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

NOTIFY = os.environ.get("CST_VM_NOTIFY") or NOTIFY_PROGRAM

# Where an alert btrfs-notify could not take is kept for cst doctor and the next login.
ALERTS = os.environ.get("CST_VM_ALERTS") or PENDING_ALERTS  # override: tests

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

# The install record that lists what backup-timer on writes (override: tests).
INSTALL_RECORD = os.environ.get("CST_VM_INSTALL_RECORD") or ROOT_RECORD
TIMER_COMPONENT = manifest_by_src()["vm/cst-vm"].component

# True during the merge back of a NAS backup: every child then runs in its own
# session, so a Ctrl-C or a closed terminal cannot reach it (virsh cancels a
# `blockcommit --wait` on SIGINT).
DETACH = {"on": False}

# The commands cst-vm runs (where it runs: not moved to the host from a
# toolbox), and virsh over the same runner. Bytes of their output that are
# not UTF-8 become U+FFFD.
RUNNER: CommandRunnerPort = SubprocessCommandRunner()

VIRSH: VirshPort = SubprocessVirshAdapter(URI, RUNNER, on_host=False)


def _triple(r: CommandResult, name: str, timeout: float) -> tuple[int, str, str]:
    """(rc, stdout, stderr) of r with cst-vm's own words for a command that is
    missing or does not answer; one that cannot start raises (as subprocess.run did)."""
    if r.fault == "missing":
        return 127, "", f"{name}: not found"
    if r.fault == "timeout":
        return 124, "", f"{name}: no answer within {timeout} s"
    if r.cause is not None:
        raise r.cause
    return r.returncode, r.stdout, r.stderr


def run(
    cmd: Sequence[str],
    timeout: int = 120,
    extra_env: Mapping[str, str] | None = None,
    detach: bool | None = None,
) -> tuple[int, str, str]:
    """(rc, stdout, stderr); rc 127 when the command does not exist. In the
    C locale (virsh translates its answers: "shut off" is "desligado" in
    pt_BR); extra_env on top. Bytes that are not UTF-8 become U+FFFD."""
    r = RUNNER.run(
        cmd,
        timeout=timeout,
        env=extra_env,
        locale_c=True,
        new_session=DETACH["on"] if detach is None else detach,
    )
    return _triple(r, cmd[0], timeout)


def run_on_terminal(cmd: Sequence[str]) -> int:
    """Exit status of cmd in the C locale, on our terminal (stdin and output:
    sudo may ask for the password). A command that cannot start raises (as
    subprocess.run did)."""
    r = RUNNER.run(cmd, locale_c=True, stdin="inherit", output="inherit")
    if r.cause is not None:
        raise r.cause
    return r.returncode


def virsh(*args: str, timeout: int = 60, detach: bool | None = None) -> tuple[int, str, str]:
    """virsh -c URI ARGS through VIRSH, with run()'s answers."""
    r = VIRSH.call(*args, timeout=timeout, start_new_session=DETACH["on"] if detach is None else detach)
    return _triple(r, "virsh", timeout)


def read_conf(path: str, *, bash_sourced: bool = False) -> dict[str, str]:
    """The settings of a KEY=VALUE file ({} when it cannot be read), through
    cst_core's one reader (parse_shell_assignments); the last line of a key
    wins. bash_sourced: cpu-setup.conf and snapshots.conf, which bash sources
    too, read exactly as bash reads them; vm.conf (only Python reads it) also
    accepts blanks around = (`VM_NOCOW = 0`), as cst-vm always did. A line
    that is not an assignment sets nothing (prepare_command reports it).
    Undecodable bytes are replaced (main's cst-vm stopped with a traceback)."""
    return dict(settings_file(path, bash_sourced=bash_sourced).values)


def settings_file(path: str, *, bash_sourced: bool) -> ShellAssignments:
    """A settings file through cst_core's one reader ("" when it cannot be read).
    snapshots.conf (NAS_CONF) also takes the lines an older CST wrote there with
    printf %q, read as bash reads them (parse_snapshots_conf)."""
    text = read_text(path, "")
    if path == NAS_CONF:
        return parse_snapshots_conf(text)
    return parse_shell_assignments(text, bash_sourced=bash_sourced)


def conf_files(cmd: str, nas: bool, vm: str | None) -> list[tuple[str, bool]]:
    """[(settings file, bash_sourced)] that a cst-vm command reads; vm: the VM named on the command line."""
    files = [(VM_CONF, False)]
    # cpu-setup.conf: VM_NAME when no VM is named, VM_CPU_DISABLE for status and tune.
    # A backup (or backup-timer) of a named VM reads neither: a line there must not stop it.
    if cmd != "nocow" and not (vm and cmd in ("backup", "backup-timer")):
        files.append((CPU_CONF, True))
    if nas or cmd == "backup-timer":
        files.append((NAS_CONF, True))
    return files


def prepare_command(cmd: str, sub: str | None, nas: bool, vm: str | None) -> None:
    """Before a cst-vm command runs (help: only the SIGPIPE default below). Problem lines of the settings files it
    reads: a command that changes the machine with them (backup, tune/nocow
    apply, backup-timer on) refuses to start, since a default must never
    stand in for a line that says something else; the others print them on
    stderr and go on (backup-timer off too: it reads no setting, and it is
    the way back). A command that only reads (status, list, the plans,
    backup-timer status, help) also gets SIGPIPE back at its default, so `| head`
    ends it quietly; commands that change the machine keep Python's handling
    (backup: SafeStream), so a closed pipe never cuts their cleanup short."""
    if cmd == "help":
        restore_default_signals()
        return
    found = []
    for path, bash_sourced in conf_files(cmd, nas, vm):
        problems = settings_file(path, bash_sourced=bash_sourced).problems
        if problems:
            found.append(describe_problems(path, problems))
    changes = cmd == "backup" or sub in ("apply", "on", "off")
    if found and changes and sub != "off":
        raise Refused("\n".join(found) + "\nFix these lines first; nothing was changed.")
    if found:
        print("\n".join(found), file=sys.stderr)
    if not changes:
        restore_default_signals()


def conf_number(key: str, path: str | None = None) -> int:
    """A whole number of /etc/cst/vm.conf (NAS_DEFAULTS when missing or not a number >= 0)."""
    v = read_conf(path or VM_CONF).get(key, "")
    return int(v) if v.isdigit() else NAS_DEFAULTS[key]


def default_vm() -> str:
    return read_conf(CPU_CONF, bash_sourced=True).get("VM_NAME") or DEFAULT_VM


def cpu_disable_list(conf_path: str | None = None) -> list[str]:
    """The features vm-pin keeps disabled: VM_CPU_DISABLE from cpu-setup.conf
    when set there (also empty), else vm-pin's default (cpu.domain)."""
    conf = read_conf(conf_path or CPU_CONF, bash_sourced=True)
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
    """The backups in the index ([] when there is none or it cannot be read). Refused for an index of a
    newer cst vm ("version" above INDEX_FORMAT's): listing it could mislead, and writing it back would
    lose what this one does not know."""
    path = path or INDEX
    try:
        with open(path) as f:
            data = json.load(f)
    except OSError, ValueError:
        return []
    if (found := INDEX_FORMAT.newer(data, "version")) is not None:
        raise Refused(
            f"{INDEX_FORMAT.newer_text(path, found)}: left as it is, and nothing is backed up or listed "
            f"with it; use the cst vm of that CST (in that checkout: {CST} vm backup-timer on installs it)"
        )
    entries = data.get("backups") if isinstance(data, dict) else None
    return [
        e
        for e in entries or []
        if isinstance(e, dict) and e.get("vm") and (e.get("path") or e.get("mode") == "nas" and e.get("snapshot"))
    ]


def check_backup_start(what: str) -> None:
    """Before a backup copies anything: root (or the tests' override), and an index this cst vm can
    record it in (read_index refuses one of a newer cst vm)."""
    if os.geteuid() != 0 and not os.environ.get("CST_VM_NONROOT_OK"):
        raise Refused(f"{what} needs root: run it as `cst vm {what}` (it calls sudo by itself)")
    read_index()


def write_index(entries: Sequence[Mapping[str, Any]], path: str | None = None) -> None:
    """Atomically (temporary file + rename), mode 0644, folder 0755; durable
    (the file and the folder fsynced): a lost index hides a finished backup."""
    path = path or INDEX
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o755)
    text = json.dumps({"version": INDEX_FORMAT.version, "backups": list(entries)}, indent=2, sort_keys=True) + "\n"
    write_atomic(path, text, mode=0o644, relabel=False)


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


def notify(urgency: Urgency, title: str, body: str) -> None:
    """A CST desktop alert (btrfs-notify keeps it for the next login when no
    session shows it; without btrfs-notify, cst_core keeps it there and says
    so). Sent first, then printed."""
    Notifier(RUNNER, NOTIFY, ALERTS, log=lambda _prio, msg: print(f"{PROG}: {msg}", file=sys.stderr)).send(
        urgency, NAME, title, body
    )
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
        # 0755 and 0644 exactly, whatever the umask (sudo keeps a user's 077): cst doctor reads the pid.
        ensure_dir(RUN_DIR, 0o755)
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


def stop_timeout() -> str:
    """A stop may arrive while the snapshot call is held (SNAPSHOT_TIMEOUT +
    SNAPSHOT_SETTLE), then the merge runs (COMMIT_TIMEOUT, plus virsh's own
    300 s), then the checks and the alert: 30 minutes of margin."""
    return f"{(SNAPSHOT_TIMEOUT + SNAPSHOT_SETTLE + COMMIT_TIMEOUT + 300 + 1800 + 59) // 60}min"


def timer_files(entry_file: str) -> dict[str, tuple[bytes, int]]:
    """{path: (wanted bytes, mode)}: the program as `install` writes it (a
    zipapp; entry_file is the running cst-vm), the service and the timer."""
    return {
        SBIN_COPY: (install_payload("vm/cst-vm", entry_file), 0o755),
        os.path.join(UNIT_DIR, SERVICE): (render_service(SBIN_COPY, stop_timeout()).encode(), 0o644),
        os.path.join(UNIT_DIR, TIMER): (render_timer().encode(), 0o644),
    }


def timer_installer(entry_file: str) -> InstallService:
    """The installer's use case for backup-timer's files: written only when they changed, and listed in
    the install record (INSTALL_RECORD) under the component vm:backup-timer."""
    return program_installer(entry_file)


def install_timer_files(
    files: Mapping[str, tuple[bytes, int]], states: Mapping[str, UnitFileState], entry_file: str
) -> None:
    """Write (through the installer) each of backup-timer's files that is not current, and say so.
    Its own refusals (a foreign or masked file) come first, in cmd_timer."""
    svc = timer_installer(entry_file)
    as_root = os.geteuid() == 0
    program_entry = manifest_by_src()["vm/cst-vm"]
    for p, (text, mode) in files.items():
        program = p == SBIN_COPY
        place = Placement(
            path=p,
            dest=p,
            record=INSTALL_RECORD,
            component=TIMER_COMPONENT,
            src=program_entry.src if program else None,
            kind=program_entry.kind if program else "text",
            owner="root",
            mode=mode,
        )
        if svc.put(place, text, as_root=as_root):
            print(f"  {'installed' if states[p] == 'missing' else 'updated'}: {p}")


def timer_record_done(entry_file: str) -> None:
    """backup-timer on finished: the record says vm:backup-timer is complete."""
    timer_installer(entry_file).mark(INSTALL_RECORD, TIMER_COMPONENT)


def forget_timer_files(entry_file: str, removed: Sequence[str]) -> None:
    """backup-timer off removed these files: the record no longer lists them."""
    timer_installer(entry_file).forget(INSTALL_RECORD, removed)


def timer_file_states(files: Mapping[str, tuple[bytes, int]], instance_timer: str) -> dict[str, UnitFileState]:
    """{path: state} of what backup-timer on writes (timer_files): the program by
    the installer's link rule, the two units also "masked" (systemctl mask: a
    link to /dev/null). instance_timer, the VM's own timer (cst-vm-backup@VM.timer
    in UNIT_DIR, which `systemctl mask` of that one VM creates), is added only
    when it is masked: CST never writes it."""
    states: dict[str, UnitFileState] = {
        p: (owned_file_state_at if p == SBIN_COPY else unit_file_state_at)(
            p, t, functools.partial(is_ours, p), owner="root"
        )
        for p, (t, _) in files.items()
    }
    if is_masked_unit(instance_timer):
        states[instance_timer] = "masked"
    return states


def systemctl(*args: str) -> tuple[int, str, str]:
    rc, so, err = run(["systemctl", *args], timeout=120)
    return rc, so.strip(), first_line(err)


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


def image_dirs(home: str | None = None) -> list[ImageDir]:
    """Every known VM image folder."""
    home = home or user_home()
    out = [ImageDir("system", SYSTEM_IMAGES, "libvirt qemu:///system: virt-manager, virsh")]
    out += [ImageDir("user", os.path.join(home, rel), what) for rel, what in USER_IMAGES]
    return out


def probe_dir(d: ImageDir, readable: Callable[[str], str] = lambda p: p) -> ImageProbe:
    """What a VM image folder looks like (ImageProbe). readable maps a path to where this
    process reads it (the doctor in a toolbox: under /run/host)."""
    rp = readable(d.path)
    link = os.readlink(rp) if os.path.islink(rp) else None
    here = ImageProbe(d.scope, d.path, d.what, present=link is None and os.path.isdir(rp), link=link)
    if not here.present:
        return here
    btrfs = is_btrfs(rp)
    if btrfs is False:
        return replace(here, btrfs=False)
    nocow = flag_state(readable_inode_flags(rp))
    try:
        entries = sorted(os.scandir(rp), key=lambda e: e.name)
    except OSError:
        return replace(here, btrfs=btrfs, nocow=nocow)
    images: list[ImageInfo] = []
    leftovers: list[str] = []
    for e in entries:
        try:
            if not e.is_file(follow_symlinks=False):
                continue
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        if e.name.endswith(".cow-old"):
            leftovers.append(e.name)
            continue
        if e.name.lower().endswith(".iso"):
            continue  # read-only installers: nothing to gain
        images.append(
            ImageInfo(
                name=e.name,
                path=os.path.join(d.path, e.name),
                alloc=st.st_blocks * 512,
                size=st.st_size,
                nocow=flag_state(readable_inode_flags(os.path.join(rp, e.name))),
            )
        )
    return replace(here, btrfs=btrfs, nocow=nocow, listed=True, images=images, leftovers=leftovers)


def needs_mark(d: ImageProbe) -> bool:
    """True when nocow apply would act on the folder (also: unknown state of
    the system folder, or the system folder missing while libvirt is there).
    A symbolic link is never acted on (install -d and chattr would follow it)."""
    if d.link is not None or os.path.lexists(d.path) and not d.present:
        return False
    if d.scope == "system" and not d.present:
        return os.path.isdir(os.path.dirname(d.path))
    return d.present and d.btrfs is not False and d.nocow is not True


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

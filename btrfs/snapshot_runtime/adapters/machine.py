# SPDX-License-Identifier: MPL-2.0
"""The machine as btrfs-snapshot sees it (SnapshotRun): its paths, below CST_SNAPSHOT_ROOT for a run that
is not root (the test seam), the settings of snapshots.conf read as data, the command runner, the
journal lines, and btrfs-snapshot-core's command lines with the common arguments.
"""

from collections.abc import Buffer, Callable, Mapping, Sequence
import contextlib
import glob
import io
import os
import signal
import sys
import time
from typing import TextIO, override

from btrfs.snapshot_runtime.domain import (
    CORE_HINTS,
    DEFAULTS,
    SUBVOL_ROOT_INODE,
    TAG,
    Stderr,
    CacheArgs,
    Sources,
    cache_args,
    nas_out_of_range,
    settings_problem_line,
    sources,
)
from cst_core.adapters.files import LocalFileSystemAdapter, write_atomic
from cst_core.adapters.journal import log
from cst_core.adapters.locks import take_lock
from cst_core.adapters.notify import PENDING_ALERTS, Notifier, NotifyOutcome
from cst_core.adapters.runtime import beside_program
from cst_core.domain.contracts import (
    NOTIFY_PROGRAM,
    RESTIC_CACHE_DIR,
    RESTIC_PW_FILE,
    SNAPSHOTS_CONF,
    StateFormatKey,
    Urgency,
    state_format,
)
from cst_core.domain.errors import shell_exit_status
from cst_core.domain.text import Assignment, file_lines, parse_snapshots_conf
from cst_core.ports import CommandFault, CommandResult, CommandRunnerPort, OutputMode


_BULK_JOURNAL = state_format(StateFormatKey.BULK_JOURNAL)
# The byte-transparent reader (bash's $(cat ...) and read -r kept every byte).
_FILES = LocalFileSystemAdapter()


class Stopped(Exception):
    """TERM, INT or HUP during a run that traps them (nas): after the command that was running."""


class SnapshotRun:
    """Paths, settings and commands of one run (see the module docstring)."""

    def __init__(self, environ: Mapping[str, str], runner: CommandRunnerPort, main_file: str = "") -> None:
        # The test seam never moves a run as root.
        r = "" if os.geteuid() == 0 else environ.get("CST_SNAPSHOT_ROOT", "")
        self.root = r
        self.conf = f"{r}{SNAPSHOTS_CONF}"
        self.plan = f"{r}/etc/btrfs-setup/convert-plan"
        self.state = f"{r}/var/lib/btrfs-snapshot"
        self.core = f"{r}/usr/local/sbin/btrfs-snapshot-core"
        self.notify = f"{r}{NOTIFY_PROGRAM}"
        self.pending_alerts = f"{r}{PENDING_ALERTS}"
        self.lock_path = f"{r}/run/btrfs-snapshot.lock"
        self.run_dir = f"{r}/run/btrfs-snapshot"
        self.pin_file = f"{self.run_dir}/pinned"
        self.nas_mnt = f"{self.run_dir}/nas"
        self.nas_lock = f"{r}/run/btrfs-snapshot-nas.lock"
        self.restic_password = f"{r}{RESTIC_PW_FILE}"
        self.restic_cache = f"{r}{RESTIC_CACHE_DIR}"
        self.boot_flag = f"{r}/etc/btrfs-setup/convert-at-boot"
        self.bulk_flag = f"{r}/etc/btrfs-setup/bulk-at-boot"
        # btrfs-bulk's journals (the registered bulk-journal format; any version: one there is a move
        # not finished).
        self.bulk_state = f"{r}{_BULK_JOURNAL.folder}"
        self.bulk_journals = f"{glob.escape(self.bulk_state)}/{_BULK_JOURNAL.file_name}"
        self.runner = runner
        # The bash front and engine (btrfs/btrfs-snapshot), in this program's folder (installed, in
        # a checkout or a copy alike), as the front finds this one, by the path the front ran this
        # program by (not resolved: as bash names itself in its errors).
        self.front = beside_program("btrfs-snapshot", main_file)
        # The descriptors every command gets (the run's locks: fd 9 and 8 of the bash).
        self.fds: tuple[int, ...] = ()
        # A trapped signal (see trap_stop): asked for, and whether a command is running now.
        self.stop_asked = False
        self.in_command = False
        self.env_home = environ.get("HOME_DIR", "")
        self.settings: dict[str, str] = dict(DEFAULTS)
        self.snapdir = ""

    # ---- the start, as the bash's ------------------------------------------------------------
    def start(self) -> None:
        """The settings (snapshots.conf as data; HOME_DIR of the environment wins), the restic host
        name (hostname -s when unset) and the state folder."""
        self.settings["HOME_DIR"] = self.env_home
        if os.access(self.conf, os.R_OK):
            # Bytes that are not UTF-8 kept as they are (bash kept them): surrogateescape.
            raw = self.read_raw(self.conf)
            got = parse_snapshots_conf(raw)
            for problem in got.problems:
                print(
                    settings_problem_line(self.conf, problem.line, problem.text, problem.reason, kept=problem.kept),
                    file=sys.stderr,
                    flush=True,
                )
            for a in got.entries:
                if a.key in DEFAULTS:
                    self.settings[a.key] = a.value
            self.refuse_wrapping(got.entries, raw)
        if self.env_home:
            self.settings["HOME_DIR"] = self.env_home
        self.snapdir = f"{self.settings['TOP']}/{self.settings['SNAP_DIR_NAME']}"
        if not self.settings["NAS_RESTIC_HOST"]:
            self.settings["NAS_RESTIC_HOST"] = self.capture(["hostname", "-s"], stderr=Stderr.SHOW).rstrip("\n")
        try:
            os.makedirs(self.state, exist_ok=True)
        except OSError as e:
            print(f"mkdir: cannot create directory '{self.state}': {e.strerror}", file=sys.stderr, flush=True)

    def refuse_wrapping(self, entries: Sequence[Assignment], raw: str) -> None:
        """A NAS number of snapshots.conf that bash computes only by wrapping past 64 bits
        (nas_out_of_range) is a problem line of its last assignment: its warning, and the default."""
        lines = file_lines(raw)
        for key in nas_out_of_range(self.settings, self.now()):
            last = next(a for a in reversed(entries) if a.key == key)
            reason = "a number past 2**63 - 1 (bash would wrap it): not wrapped"
            print(
                settings_problem_line(self.conf, last.line, lines[last.line - 1], reason), file=sys.stderr, flush=True
            )
            self.settings[key] = DEFAULTS[key]

    # ---- output --------------------------------------------------------------------------------
    def log(self, prio: int, msg: str, stream: TextIO | None = None) -> None:
        """A journal line, <prio>btrfs-snapshot: msg, flushed (stdout unless stream)."""
        log(prio, msg, tag=TAG, stream=stream)

    @staticmethod
    def say(text: str, stream: TextIO | None = None) -> None:
        print(text, file=sys.stdout if stream is None else stream, flush=True)

    # ---- commands ------------------------------------------------------------------------------
    def run(
        self,
        argv: Sequence[str],
        *,
        output: OutputMode = OutputMode.INHERIT,
        input_text: str | None = None,
        pass_fds: Sequence[int] = (),
        on_line: Callable[[str], None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """argv with this process's environment and stdin from /dev/null; a command that cannot start
        says so on stderr as bash does (127 not found, 126 not runnable)."""
        fds = tuple(pass_fds) or self.fds
        res = self._call(
            lambda: self.runner.run(
                argv,
                output=output,
                input_text=input_text,
                pass_fds=fds,
                on_line=on_line,
                env=env,
                decode_errors="surrogateescape",
            )
        )
        if res.fault is not None:
            # As bash says it (without its line number).
            if res.fault != CommandFault.MISSING:
                what = "Permission denied"
            elif "/" in argv[0]:
                what = "No such file or directory"
            else:
                what = "command not found"
            print(f"{self.front or TAG}: {argv[0]}: {what}", file=sys.stderr, flush=True)
        return res

    def rc(self, res: CommandResult) -> int:
        """A command's exit status as bash gives it: 128+N for a child killed by signal N."""
        return shell_exit_status(res.returncode)

    def capture(self, argv: Sequence[str], *, stderr: Stderr = Stderr.DROP, input_text: str | None = None) -> str:
        """$(argv): its stdout (stderr shown, or dropped as 2>/dev/null)."""
        output = OutputMode.STDOUT_SHOW_STDERR if stderr is Stderr.SHOW else OutputMode.STDOUT
        return self.run(argv, output=output, input_text=input_text).stdout

    def ok(self, argv: Sequence[str], *, input_text: str | None = None) -> bool:
        """argv >/dev/null 2>&1 succeeded."""
        res = self._call(
            lambda: self.runner.run(
                argv,
                output=OutputMode.CAPTURE,
                input_text=input_text,
                pass_fds=self.fds,
                decode_errors="surrogateescape",
            )
        )
        return res.fault is None and res.returncode == 0

    def desktop_alert(self, urgency: Urgency, title: str, body: str) -> bool:
        """btrfs-notify -u URGENCY -a btrfs-snapshot TITLE BODY through cst_core's Notifier: True when
        btrfs-notify showed the alert or kept it for the next login, or, when it is missing, fails or
        does not answer within 60 s, the Notifier kept it in the pending alerts folder (cst doctor and
        the next login show it; it says so in the journal). False when the alert is only in the
        journal."""
        notifier = Notifier(self.runner, self.notify, self.pending_alerts, log=self.log)
        return self._call(lambda: notifier.send(urgency, TAG, title, body)) in (NotifyOutcome.SENT, NotifyOutcome.KEPT)

    def _call[T](self, call: Callable[[], T]) -> T:
        """One command; a signal trap_stop caught while it ran ends the run once it is done (bash
        runs a trap after the foreground command, which is never killed for it)."""
        self.in_command = True
        try:
            res = call()
        finally:
            self.in_command = False
        if self.stop_asked:
            self.stop_asked = False  # the cleanup's commands run; a new signal stops it again
            raise Stopped
        return res

    def trap_stop(self, signum: int, frame: object) -> None:
        """The handler of trap 'exit 143' TERM INT HUP: at once between commands, else after the
        running one."""
        del signum, frame
        if not self.in_command:
            raise Stopped
        self.stop_asked = True

    # ---- what the bash asked through commands ---------------------------------------------------
    def is_subvol(self, path: str) -> bool:
        """[[ -d PATH && $(stat -c %i PATH) == 256 ]]: a btrfs subvolume's root."""
        return os.path.isdir(path) and self.capture(["stat", "-c", "%i", path]).rstrip("\n") == SUBVOL_ROOT_INODE

    def is_mountpoint(self, path: str) -> bool:
        """mountpoint -q PATH: True when PATH is an explicit mountpoint (e.g. external bind mount)."""
        return os.path.isdir(path) and self.ok(["mountpoint", "-q", path])

    def sources(self) -> Sources:
        ws = f"{self.settings['HOME_DIR']}/Workspace"
        return sources(self.settings, workspace_is_subvol=self.is_subvol(ws) and not self.is_mountpoint(ws))

    def cache_args(self, src: Sources, stream: TextIO | None = None) -> CacheArgs:
        """The core's cache options, their warnings logged (on stream)."""
        got = cache_args(src, self.settings["SNAPSHOT_EXCLUDE_CACHES"], self.settings["SNAPSHOT_CACHES_SKIP"])
        for w in got.warnings:
            self.log(4, w, stream)
        return got

    def core_common(self, cmd: str) -> list[str]:
        """btrfs-snapshot-core CMD with the arguments of every snapshot/prune/finish run: we hold
        LOCK, hence --no-lock."""
        return [
            self.core,
            cmd,
            "--snapdir",
            self.snapdir,
            "--no-lock",
            "--pin-file",
            self.pin_file,
            "--state-dir",
            self.state,
            *CORE_HINTS,
        ]

    # ---- files, as the bash's commands treat them ----------------------------------------------
    @staticmethod
    def now() -> int:
        """date +%s."""
        return int(time.time())

    @staticmethod
    def mtime(path: str) -> int | None:
        """stat -c %Y PATH (None when it cannot be read)."""
        try:
            return int(os.stat(path).st_mtime)
        except OSError:
            return None

    @staticmethod
    def rm_f(*paths: str) -> None:
        """rm -f PATHS: a missing one is no error; any other failure is said on stderr."""
        for p in paths:
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"rm: cannot remove '{p}': {e.strerror}", file=sys.stderr, flush=True)

    @staticmethod
    def touch(*paths: str) -> None:
        """touch PATHS: created (umask mode) or their times set to now."""
        for p in paths:
            try:
                with open(p, "a", encoding="utf-8"):
                    pass
                os.utime(p, None)
            except OSError as e:
                print(f"touch: cannot touch '{p}': {e.strerror}", file=sys.stderr, flush=True)

    @staticmethod
    def write(path: str, text: str) -> None:
        """echo/printf ... >PATH, but replaced whole: a temporary file, synced, renamed over it, so a
        crash, a power loss or a full disk leaves the old record or the new one, never an empty or
        cut one (umask mode, as a new file gets). A failure is said on stderr, as bash's redirection."""
        try:
            write_atomic(path, text, mode="umask", durable="file", relabel=False)
        except OSError as e:
            print(f"{TAG}: {path}: {e.strerror}", file=sys.stderr, flush=True)

    @staticmethod
    def read(path: str) -> str | None:
        """$(cat PATH 2>/dev/null) without the trailing newlines: None when it cannot be read."""
        text = _FILES.read_text(path)
        return None if text is None else text.rstrip("\n")

    @staticmethod
    def read_raw(path: str) -> str:
        """The whole text of PATH, byte-transparent ("" when it cannot be read)."""
        return _FILES.read_text(path) or ""

    @staticmethod
    def non_empty(path: str) -> bool:
        """[[ -s PATH ]]."""
        try:
            return os.stat(path).st_size > 0
        except OSError:
            return False

    # ---- the lock ------------------------------------------------------------------------------
    def lock(self, path: str, *, wait: bool, timeout: float | None = None) -> int | None:
        """exec N>PATH; flock: an fd holding the lock, handed on to every command from now on; None
        when another run holds it (or it cannot be opened: said on stderr, as bash says it)."""
        mask = os.umask(0)
        os.umask(mask)
        try:
            fd = take_lock(path, wait=wait, mode=0o666 & ~mask, nofollow=False, timeout=timeout)
        except OSError as e:
            print(f"{TAG}: {path}: {e.strerror}", file=sys.stderr, flush=True)
            return None
        if fd is not None:
            self.fds = (*self.fds, fd)
        return fd

    def unlock(self, fd: int) -> None:
        """exec N>&-: the lock released (unless a command still holds it)."""
        self.fds = tuple(f for f in self.fds if f != fd)
        with contextlib.suppress(OSError):
            os.close(fd)


class _OutputFd(io.RawIOBase):
    """fd 1 or 2 for this program's own lines: a write that fails (the stream closed while SIGPIPE
    is ignored, as systemd runs a service) is dropped without a word and the run goes on, as bash's
    echo and printf did; nothing is kept for a later retry or for the exit."""

    def __init__(self, fd: int) -> None:
        super().__init__()
        self.fd = fd

    @override
    def writable(self) -> bool:
        return True

    @override
    def write(self, b: Buffer, /) -> int:
        data = bytes(b)
        try:
            return os.write(self.fd, data)
        except OSError:
            return len(data)


def _bash_like(fd: int, encoding: str) -> io.TextIOWrapper:
    return io.TextIOWrapper(
        io.BufferedWriter(_OutputFd(fd)), encoding=encoding, errors="surrogateescape", line_buffering=True
    )


def keep_bytes_on_output() -> None:
    """Our stdout and stderr as bash's: a setting or path that is not UTF-8 goes out as the bytes it
    was, and a failed write of a line, on either, does not stop the run (_OutputFd). Under systemd
    both are the one journal stream: they go away together."""
    sys.stdout = _bash_like(1, getattr(sys.stdout, "encoding", None) or "utf-8")
    sys.stderr = _bash_like(2, getattr(sys.stderr, "encoding", None) or "utf-8")


def sigpipe_handed_over() -> bool:
    """CST_SIGPIPE_IGNORED=1 (which goes): the front started with SIGPIPE ignored, as systemd runs
    its services. Python ignores SIGPIPE whatever it got, so only the front can tell. Then the
    commands this run starts get SIGPIPE ignored too, as bash's children did (a closed journal
    stream is EPIPE for them, not a kill), and SIGXFSZ at its default, as bash had it; otherwise
    both at their default (subprocess restores them)."""
    if os.environ.pop("CST_SIGPIPE_IGNORED", "") != "1":
        return False
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
    return True

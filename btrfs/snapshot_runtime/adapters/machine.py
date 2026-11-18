# SPDX-License-Identifier: MPL-2.0
"""The machine as btrfs-snapshot sees it (SnapshotRun): its paths, below CST_SNAPSHOT_ROOT for a run that
is not root (the test seam), the settings of snapshots.conf read as data, the command runner, the
journal lines, and btrfs-snapshot-core's command lines with the common arguments.
"""

from collections.abc import Callable, Mapping, Sequence
import contextlib
import os
import sys
import time
from typing import TextIO

from btrfs.snapshot_runtime.domain import (
    CORE_HINTS,
    DEFAULTS,
    SUBVOL_ROOT_INODE,
    TAG,
    Stderr,
    CacheArgs,
    Sources,
    cache_args,
    settings_problem_line,
    sources,
)
from cst_core.adapters.files import read_text
from cst_core.adapters.journal import log
from cst_core.adapters.locks import take_lock
from cst_core.adapters.runtime import sibling_program
from cst_core.domain.errors import shell_exit_status
from cst_core.domain.text import parse_snapshots_conf
from cst_core.ports import CommandResult, CommandRunnerPort, OutputMode


class SnapshotRun:
    """Paths, settings and commands of one run (see the module docstring)."""

    def __init__(self, environ: Mapping[str, str], runner: CommandRunnerPort, main_file: str = "") -> None:
        # The test seam never moves a run as root.
        r = "" if os.geteuid() == 0 else environ.get("CST_SNAPSHOT_ROOT", "")
        self.root = r
        self.conf = f"{r}/etc/btrfs-setup/snapshots.conf"
        self.plan = f"{r}/etc/btrfs-setup/convert-plan"
        self.state = f"{r}/var/lib/btrfs-snapshot"
        self.core = f"{r}/usr/local/sbin/btrfs-snapshot-core"
        self.notify = f"{r}/usr/local/sbin/btrfs-notify"
        self.lock_path = f"{r}/run/btrfs-snapshot.lock"
        self.run_dir = f"{r}/run/btrfs-snapshot"
        self.pin_file = f"{self.run_dir}/pinned"
        self.nas_mnt = f"{self.run_dir}/nas"
        self.nas_lock = f"{r}/run/btrfs-snapshot-nas.lock"
        self.restic_password = f"{r}/etc/btrfs-setup/restic-password"
        self.restic_cache = f"{r}/var/cache/btrfs-snapshot-restic"
        self.boot_flag = f"{r}/etc/btrfs-setup/convert-at-boot"
        self.bulk_flag = f"{r}/etc/btrfs-setup/bulk-at-boot"
        self.bulk_state = f"{r}/var/lib/btrfs-bulk"
        self.runner = runner
        # The bash front and engine (btrfs/btrfs-snapshot), next to this program.
        self.front = sibling_program("btrfs/btrfs-snapshot", main_file) if main_file else ""
        # The descriptors every command gets (the run's locks: fd 9 and 8 of the bash).
        self.fds: tuple[int, ...] = ()
        self.env_home = environ.get("HOME_DIR", "")
        self.settings: dict[str, str] = dict(DEFAULTS)
        self.snapdir = ""

    # ---- the start, as the bash's ------------------------------------------------------------
    def start(self) -> None:
        """The settings (snapshots.conf as data; HOME_DIR of the environment wins), the restic host
        name (hostname -s when unset) and the state folder."""
        self.settings["HOME_DIR"] = self.env_home
        if os.access(self.conf, os.R_OK):
            got = parse_snapshots_conf(read_text(self.conf) or "")
            for problem in got.problems:
                print(
                    settings_problem_line(self.conf, problem.line, problem.text, problem.reason),
                    file=sys.stderr,
                    flush=True,
                )
            for a in got.entries:
                if a.key in DEFAULTS:
                    self.settings[a.key] = a.value
        if self.env_home:
            self.settings["HOME_DIR"] = self.env_home
        self.snapdir = f"{self.settings['TOP']}/{self.settings['SNAP_DIR_NAME']}"
        if not self.settings["NAS_RESTIC_HOST"]:
            self.settings["NAS_RESTIC_HOST"] = self.capture(["hostname", "-s"], stderr=Stderr.SHOW).rstrip("\n")
        try:
            os.makedirs(self.state, exist_ok=True)
        except OSError as e:
            print(f"mkdir: cannot create directory '{self.state}': {e.strerror}", file=sys.stderr, flush=True)

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
        output: OutputMode = "inherit",
        input_text: str | None = None,
        pass_fds: Sequence[int] = (),
        on_line: Callable[[str], None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """argv with this process's environment and stdin from /dev/null; a command that cannot start
        says so on stderr as bash does (127 not found, 126 not runnable)."""
        fds = tuple(pass_fds) or self.fds
        res = self.runner.run(argv, output=output, input_text=input_text, pass_fds=fds, on_line=on_line, env=env)
        if res.fault is not None:
            what = "command not found" if res.fault == "missing" else "cannot be run"
            print(f"{TAG}: {argv[0]}: {what}", file=sys.stderr, flush=True)
        return res

    def rc(self, res: CommandResult) -> int:
        """A command's exit status as bash gives it: 128+N for a child killed by signal N."""
        return shell_exit_status(res.returncode)

    def capture(self, argv: Sequence[str], *, stderr: Stderr = Stderr.DROP, input_text: str | None = None) -> str:
        """$(argv): its stdout (stderr shown, or dropped as 2>/dev/null)."""
        res = self.run(argv, output="capture" if stderr is Stderr.SHOW else "stdout", input_text=input_text)
        if stderr is Stderr.SHOW and res.stderr and res.fault is None:
            sys.stderr.write(res.stderr)
            sys.stderr.flush()
        return res.stdout

    def ok(self, argv: Sequence[str], *, input_text: str | None = None) -> bool:
        """argv >/dev/null 2>&1 succeeded."""
        res = self.runner.run(argv, output="capture", input_text=input_text, pass_fds=self.fds)
        return res.fault is None and res.returncode == 0

    # ---- what the bash asked through commands ---------------------------------------------------
    def is_subvol(self, path: str) -> bool:
        """[[ -d PATH && $(stat -c %i PATH) == 256 ]]: a btrfs subvolume's root."""
        return os.path.isdir(path) and self.capture(["stat", "-c", "%i", path]).rstrip("\n") == SUBVOL_ROOT_INODE

    def sources(self) -> Sources:
        return sources(self.settings, workspace_is_subvol=self.is_subvol(f"{self.settings['HOME_DIR']}/Workspace"))

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
        """echo/printf ... >PATH (umask mode); a failure said on stderr, as bash's redirection."""
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            print(f"{TAG}: {path}: {e.strerror}", file=sys.stderr, flush=True)

    @staticmethod
    def read(path: str) -> str | None:
        """$(cat PATH 2>/dev/null) without the trailing newlines: None when it cannot be read."""
        try:
            with open(path, encoding="utf-8", errors="surrogateescape") as f:
                return f.read().rstrip("\n")
        except OSError:
            return None

    @staticmethod
    def read_raw(path: str) -> str:
        """The whole text of PATH ("" when it cannot be read)."""
        try:
            with open(path, encoding="utf-8", errors="surrogateescape") as f:
                return f.read()
        except OSError:
            return ""

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

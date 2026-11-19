# SPDX-License-Identifier: MPL-2.0
"""What btrfs-bulk asks of the rest of the machine: btrfs-snapshot (the
busy check, the conversion of a plain ~/Bulk, its space limits), Steam's
processes, the desktop alert, the clock; its output; who runs it.
"""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
import os
import pwd
import re
import secrets
import stat
import time
import traceback

from btrfs.bulk.adapters.machine import HomeTree, kind, log
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.notify import Notifier, PENDING_ALERTS
from cst_core.domain.contracts import Urgency
from cst_core.ports import CommandResult, CommandRunnerPort
from btrfs.bulk.domain import BIG, BulkPaths, Ctx, LstatKind, PathChain
from btrfs.bulk.domain.steam import PROC, RUN_USER, STEAM_APP, STEAM_COMMS


# btrfs-snapshot's test seam (every file below another folder): never handed on to the runs below.
SNAPSHOT_SEAM = "CST_SNAPSHOT_ROOT"


def snapshot_env() -> dict[str, str]:
    """This process's environment for a btrfs-snapshot run: without its test seam."""
    return {k: v for k, v in os.environ.items() if k != SNAPSHOT_SEAM}


def error_trace(e: BaseException) -> str:
    """e and its traceback, as Python prints an uncaught exception (it reads the source lines)."""
    return "".join(traceback.format_exception(e))


def is_root() -> bool:
    return os.geteuid() == 0


def lookup_user(name: str) -> pwd.struct_passwd:
    """The account NAME (KeyError: none)."""
    return pwd.getpwnam(name)


def default_home() -> str:
    return os.path.expanduser("~")


def real_path(path: str) -> str:
    return os.path.realpath(path)


class Output:
    """OutputPort on stdout: log lines are flushed at once (the journal of the
    boot service, a long run), plan/status lines are not."""

    def log(self, msg: str) -> None:
        log(msg)

    def say(self, line: str) -> None:
        print(line)


class Host:
    """HostPort. snapshot_program: btrfs-snapshot's path, asked when needed; alerts: where an alert
    btrfs-notify could not take is kept (for cst doctor and the next login); runner: the
    btrfs-snapshot runs (by default the cst_core runner)."""

    def __init__(
        self,
        paths: BulkPaths,
        tree: HomeTree,
        snapshot_program: Callable[[], str],
        proc: str = PROC,
        run_user: str = RUN_USER,
        alerts: str = PENDING_ALERTS,
        runner: CommandRunnerPort | None = None,
    ) -> None:
        self.paths, self.tree, self.snapshot_program = paths, tree, snapshot_program
        self.proc, self.run_user, self.alerts = proc, run_user, alerts
        self.runner: CommandRunnerPort = SubprocessCommandRunner() if runner is None else runner

    def busy_paths(self, paths: Sequence[str], home: str) -> set[str]:
        """The subset of PATHS some process uses (open file, cwd, root, binary,
        mapped file, or a container's bind mount of the path, of something below
        it, or of a folder between it and HOME, e.g. -v ~/.m2:/root/.m2), from
        btrfs-snapshot's own detection. If it cannot be asked, all are busy."""
        if not paths:
            return set()
        r = self._snapshot("busy", "--home", home, *paths, timeout=300)
        if r.cause is not None:  # bash could not start, or timed out
            log(f"  cannot check open files ({r.cause}): treated as busy")
            return set(paths)
        if r.returncode != 0:
            log(f"  cannot check open files ({r.stderr.strip()[:200]}): treated as busy")
            return set(paths)
        return {line for line in r.stdout.splitlines() if line}

    def convert_bulk_dir(self, bulk: str) -> list[str]:
        """~/Bulk exists as a plain folder: btrfs-snapshot turns it into a
        subvolume (its convert_one: reflink clone + switch, busy -> later)."""
        r = self._snapshot("convert-one", bulk).raise_cause()
        return (r.stdout + r.stderr).splitlines()

    def _snapshot(self, *args: str, timeout: float | None = None) -> CommandResult:
        """bash btrfs-snapshot ARGS: stdin ours, its output decoded strictly, the environment
        without its test seam (snapshot_env)."""
        return self.runner.run(
            ["bash", self.snapshot_program(), *args],
            stdin="inherit",
            decode_errors="strict",
            base_env=snapshot_env(),
            timeout=timeout,
        )

    def steam_running(self, ctx: Ctx) -> list[str]:
        """Why Steam seems to run (empty: it does not). Every process of the
        host (a Flatpak's processes are visible there too), Flatpak instances of
        Steam, and Steam's own pid files."""
        return sorted(set(self.steam_processes() + self.steam_instances() + self.steam_pid_files(ctx)))

    def steam_processes(self) -> list[str]:
        """Processes of Steam (by name or binary) among all of the host's."""
        proc = self.proc
        why: list[str] = []
        try:
            pids = [p for p in os.listdir(proc) if p.isdigit()]
        except OSError:
            pids = []
        for pid in pids:
            try:
                with open(os.path.join(proc, pid, "comm")) as f:
                    comm = f.read().strip()
            except OSError:
                continue
            try:
                exe = os.readlink(os.path.join(proc, pid, "exe"))
            except OSError:
                exe = ""
            exe = exe.removesuffix(" (deleted)")
            if (
                comm in STEAM_COMMS
                or comm.startswith("pressure-vessel")
                or exe.endswith(("/ubuntu12_32/steam", "/ubuntu12_64/steamwebhelper", "/ubuntu12_32/reaper"))
            ):
                why.append(f"process {pid} ({comm})")
        return why

    def steam_instances(self) -> list[str]:
        """Running Flatpak instances of Steam, of every user."""
        why: list[str] = []
        try:
            uids = os.listdir(self.run_user)
        except OSError:
            uids = []
        for uid in uids:
            base = os.path.join(self.run_user, uid, ".flatpak")
            try:
                insts = os.listdir(base)
            except OSError:
                continue
            why += [
                f"Flatpak instance {inst} (pid {pid})" for inst in insts if (pid := self.steam_instance_pid(base, inst))
            ]
        return why

    def steam_instance_pid(self, base: str, inst: str) -> str:
        """The live pid of the Flatpak instance base/inst if it is Steam's, else ''."""
        try:
            with open(os.path.join(base, inst, "info")) as f:
                info = f.read()
            with open(os.path.join(base, inst, "pid")) as f:
                pid = f.read().strip()
        except OSError:
            return ""
        if (
            f"\nname={STEAM_APP}\n" in "\n" + info + "\n"
            and pid.isdigit()
            and os.path.exists(os.path.join(self.proc, pid))
        ):
            return pid
        return ""

    def steam_pid_files(self, ctx: Ctx) -> list[str]:
        """Steam's own pid files in the home that name a live Steam process."""
        why: list[str] = []
        for rel in (".steam/steam.pid", ".var/app/com.valvesoftware.Steam/.steam/steam.pid"):
            if (
                self.tree.chain(ctx.home, os.path.dirname(rel)) != PathChain.OK
                or kind(os.path.join(ctx.home, rel)) != LstatKind.OTHER
                or not stat.S_ISREG(os.lstat(os.path.join(ctx.home, rel)).st_mode)
            ):
                continue
            try:
                with open(os.path.join(ctx.home, rel)) as f:
                    pid = f.read(32).strip()
                if not pid.isdigit():  # the user writes this file: never a path for root to open
                    continue
                with open(os.path.join(self.proc, pid, "comm")) as f:
                    comm = f.read().strip()
            except OSError, ValueError:
                continue
            if comm.startswith("steam"):
                why.append(f"~/{rel} names the live process {pid}")
        return why

    def snapshot_guard(self) -> dict[str, int]:
        """btrfs-snapshot's space guard, in bytes: {'pause': no new /home
        snapshot at or below it, 'resume': they come back only above it,
        'emergency': at or below it the hourly run purges most of the snapshot
        history}. Read the way btrfs-snapshot gets them, so the two cannot drift
        apart: its defaults (snapshot_program()), then snapshots.conf in the
        settings folder, which overrides them (a plain KEY=NUMBER line, quotes
        allowed; the last one wins). The built-in defaults if neither can be
        read. btrfs-snapshot measures btrfs' "Free (estimated)", this program
        statvfs: close, not the same, so MARGIN stays on top."""
        vals = {"pause": 250, "resume": 270, "emergency": 150}
        names = {
            "pause": "SKIP_AT_OR_BELOW_GIB",
            "resume": "RESUME_ABOVE_GIB",
            "emergency": "EMERGENCY_AT_OR_BELOW_GIB",
        }
        for path in (self.snapshot_program(), self.paths.snapshots_conf):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read(1 << 20)
            except OSError:
                continue
            for key, name in names.items():
                found = re.findall(rf"""^[ \t]*{name}=(["']?)(\d{{1,6}})\1[ \t]*(?:#.*)?$""", text, re.MULTILINE)
                if found:
                    vals[key] = int(found[-1][1])
        return {k: v * BIG for k, v in vals.items()}

    def alert(self, title: str, body: str) -> None:
        log(f"<4>btrfs-bulk: {title}: {body}")
        notifier = Notifier(
            self.runner,
            self.paths.notify,
            self.alerts,
            log=lambda p, m: log(f"<{p}>btrfs-bulk: {m}"),
        )
        notifier.send(Urgency.CRITICAL, "btrfs-bulk", title, body)

    def now(self) -> float:
        return time.monotonic()

    def new_contentid(self) -> str:
        return str(secrets.randbits(63) or 1)

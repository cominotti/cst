# SPDX-License-Identifier: MPL-2.0
"""The game profile on the machine: systemctl for both managers, the cgroup tree, /proc, systemd's runtime unit
files, its state files, the run lock and its output.

GameProfile is the front: cpu.game.application.GameProfileSteps (on, off, tick and state) over the real
adapters below, game.leftovers' (cpu.game.adapters.leftovers) and sysfs's (cpu.game.adapters.power). The
lookups and reads it shares with cst cpu (the settings, the accounts, a file's text) are in
cpu.game.adapters.host.
"""

from collections.abc import Callable, Iterator, Sequence
import glob
import locale
import os
import sys
from typing import TextIO

from cst_core.adapters.files import ensure_dir, read_bytes, read_text, write_atomic
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.locks import take_lock
from cst_core.adapters.terminal import stdout
from cst_core.domain.text import SettingsProblem
from cst_core.ports import CommandResult, CommandRunnerPort, OutputMode
from cpu.game.adapters.host import (
    holds,
    read_settings,
    read_snapshot,
    read_stripped,
    user_id,
    user_name,
    write_sysfs_line,
)
from cpu.game.adapters.leftovers import GameLeftoversFile
from cpu.game.adapters.power import GameSysfsFiles
from cpu.game.application.profile import GameProfileSteps
from cpu.game.domain import RUN_LOCK_WAIT_S, TAG, GamePaths, GameSettings, Manager
from cpu.game.ports import GamePorts


class GameSystemctl:
    """systemctl through the command runner: `systemctl ARGS`, or `systemctl --user -M USER@ ARGS`."""

    def __init__(self, runner: CommandRunnerPort) -> None:
        self._runner = runner

    def run(self, mgr: Manager, user: str, args: Sequence[str], *, output: OutputMode) -> CommandResult:
        """systemctl ARGS in that manager, its output as output says."""
        argv = ["systemctl", "--user", "-M", f"{user}@", *args] if mgr == "usr" else ["systemctl", *args]
        return self._runner.run(argv, output=output)


class GameStateFiles:
    """game.state, game.intended and the marks as files."""

    def exists(self, path: str) -> bool:
        """The file is there."""
        return os.path.exists(path)

    def read_text(self, path: str) -> str | None:
        """Its text, bad bytes replaced; None when it cannot be read."""
        return read_text(path)

    def read_bytes(self, path: str) -> bytes | None:
        """Its bytes; None when it cannot be read."""
        return read_bytes(path)

    def snapshot(self, path: str) -> str | None:
        """Its text in one look (read_snapshot)."""
        return read_snapshot(path)

    def create(self, path: str, text: str) -> None:
        """The file holds text, from its start."""
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def append(self, path: str, text: str) -> None:
        """text at the end of the file (a field's undecodable bytes written back as they were)."""
        with open(path, "a", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(text)

    def replace(self, path: str, text: str) -> None:
        """The file replaced whole, mode 0644 (in /run: no fsync, no relabel)."""
        write_atomic(path, text, mode=0o644, durable=False, relabel=False)

    def truncate(self, path: str, size: int) -> None:
        """The file cut to its first size bytes."""
        os.truncate(path, size)

    def remove(self, path: str) -> None:
        """The file removed."""
        os.unlink(path)


class GameUnitFiles:
    """systemd's runtime unit files as files."""

    def exists(self, path: str) -> bool:
        """The file is there."""
        return os.path.exists(path)

    def is_dir(self, path: str) -> bool:
        """The folder is there."""
        return os.path.isdir(path)

    def dropins(self, folder: str, name: str) -> list[str]:
        """Every FOLDER/*.d/NAME there is, sorted."""
        found = glob.glob(os.path.join(glob.escape(folder), "*.d", name))
        return [f for f in sorted(found) if os.path.exists(f)]

    def make_folder(self, path: str) -> None:
        """The folder made, with its parents (one there already is fine)."""
        os.makedirs(path, exist_ok=True)

    def write_dropin(self, path: str, text: str) -> None:
        """The drop-in replaced whole, mode as the umask gives (in /run: no fsync, no relabel)."""
        write_atomic(path, text, mode="umask", durable=False, relabel=False)

    def remove(self, path: str) -> None:
        """The file removed."""
        os.unlink(path)

    def remove_folder(self, path: str) -> None:
        """The empty folder removed."""
        os.rmdir(path)


class GameKernelFiles:
    """The cgroup tree and /proc as files."""

    def read_text(self, path: str) -> str | None:
        """The file's text; None when it cannot be read."""
        return read_text(path)

    def read_stripped(self, path: str) -> str | None:
        """The file's text without its last newlines; None when it cannot be read."""
        return read_stripped(path)

    def exists(self, path: str) -> bool:
        """The file is there."""
        return os.path.exists(path)

    def lexists(self, path: str) -> bool:
        """The path is there (a link counts)."""
        return os.path.lexists(path)

    def is_dir(self, path: str) -> bool:
        """The folder is there."""
        return os.path.isdir(path)

    def writable(self, path: str) -> bool:
        """The file can be written."""
        return os.access(path, os.W_OK)

    def holds(self, path: str, needle: str) -> bool:
        """The file holds needle (grep -qsF)."""
        return holds(path, needle)

    def process_maps(self, proc: str) -> list[str]:
        """Every PROC/PID/maps there is, sorted."""
        return sorted(glob.glob(os.path.join(glob.escape(proc), "[0-9]*", "maps")))

    def boxes(self, folder: str, pattern: str) -> list[str]:
        """The folders FOLDER/PATTERN matches, in the order of the locale's collation."""
        found = sorted(glob.glob(f"{glob.escape(folder)}/{pattern}"), key=locale.strxfrm)
        return [d for d in found if os.path.isdir(d)]

    def procs_texts(self, folder: str, depth: int) -> Iterator[str]:
        """The text of each cgroup.procs in the folder and below it, depth levels below at most."""
        top = folder.rstrip("/").count("/")
        for d, subdirs, files in os.walk(folder):
            if d.rstrip("/").count("/") - top >= depth:
                subdirs.clear()
            if "cgroup.procs" in files:
                yield read_text(os.path.join(d, "cgroup.procs")) or ""

    def write(self, path: str, value: str) -> bool:
        """value and a newline written to the file; False when the write failed."""
        return write_sysfs_line(path, value)


class GameRunLock:
    """The run lock: the run folder made (0755, also when it is there), the lock taken within its wait."""

    def __init__(self, run: str, lock: str, *, timeout: float) -> None:
        self._run, self._lock, self._timeout = run, lock, timeout
        self._fd: int | None = None

    def take(self) -> bool:
        """Held now; False when it stays busy or cannot be taken."""
        try:
            ensure_dir(self._run, 0o755)
            os.chmod(self._run, 0o755)  # install -d -m 0755 sets the mode of a folder already there too
            self._fd = take_lock(self._lock, wait=True, mode=0o644, timeout=self._timeout)
        except OSError:
            self._fd = None
        return self._fd is not None

    def release(self) -> None:
        """Given back."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class GameStreamOutput:
    """The journal's line format on our stream (stdout, or the stream given), errors on stderr."""

    def __init__(self, out: TextIO | None = None) -> None:
        self._out = out

    def _stream(self) -> TextIO:
        return stdout() if self._out is None else self._out

    def log(self, prio: int, msg: str) -> None:
        """A journal line (<prio>game-profile: msg)."""
        journal_log(prio, msg, tag=TAG, stream=self._stream())

    def say(self, text: str) -> None:
        """One line."""
        print(text, file=self._stream(), flush=True)

    def flush(self) -> None:
        """What was said is out."""
        self._stream().flush()

    def relay(self, res: CommandResult) -> None:
        """What a command printed, on our streams (as if it had written there itself)."""
        if res.stdout:
            self._stream().write(res.stdout)
            self._stream().flush()
        if res.stderr:
            sys.stderr.write(res.stderr)
            sys.stderr.flush()

    def error(self, text: str) -> None:
        """One line on stderr."""
        print(text, file=sys.stderr, flush=True)

    def stderr_text(self, text: str) -> None:
        """text on stderr as it is."""
        sys.stderr.write(text)
        sys.stderr.flush()


def machine_ports(
    paths: GamePaths,
    runner: CommandRunnerPort,
    *,
    uid_of: Callable[[str], str] = user_id,
    name_of: Callable[[str], str] = user_name,
    out: TextIO | None = None,
) -> GamePorts:
    """The real adapters, over paths and runner."""
    return GamePorts(
        systemd=GameSystemctl(runner),
        state=GameStateFiles(),
        leftovers=GameLeftoversFile(paths.leftovers),
        units=GameUnitFiles(),
        kernel=GameKernelFiles(),
        power=GameSysfsFiles(),
        lock=GameRunLock(paths.run, paths.lock, timeout=RUN_LOCK_WAIT_S),
        out=GameStreamOutput(out),
        uid_of=uid_of,
        name_of=name_of,
    )


def load_profile(paths: GamePaths, runner: CommandRunnerPort) -> GameProfile:
    """The game profile over paths.conf's settings (and its problem lines)."""
    settings, problems = read_settings(paths.conf)
    return GameProfile(paths, settings, runner, problems=problems)


class GameProfile(GameProfileSteps):
    """on, off, tick and state of the game profile, over one runner and one set of paths (the real adapters)."""

    def __init__(
        self,
        paths: GamePaths,
        settings: GameSettings,
        runner: CommandRunnerPort,
        *,
        uid_of: Callable[[str], str] = user_id,
        name_of: Callable[[str], str] = user_name,
        problems: Sequence[SettingsProblem] = (),
        out: TextIO | None = None,
    ) -> None:
        ports = machine_ports(paths, runner, uid_of=uid_of, name_of=name_of, out=out)
        super().__init__(paths, settings, ports, problems=problems)

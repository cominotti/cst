# SPDX-License-Identifier: MPL-2.0
"""The game profile on the machine: systemctl for both managers, the cgroup tree, /proc, sysfs, its state files.

GameProfile carries what every step needs (where things are, the settings,
the command runner, the user's uid) across the steps of on, off, tick and
state; cpu.game.domain decides what the records and the answers say.
"""

from collections.abc import Callable, Sequence
import contextlib
import glob
import locale
import os
import pwd
import sys
from typing import TextIO

from cst_core.adapters.files import ensure_dir, read_bytes, read_text, write_atomic
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.locks import take_lock
from cst_core.domain.text import parse_shell_assignments, printf_q
from cst_core.ports import CommandResult, CommandRunnerPort
from cpu.game.domain import (
    DOCKER_SCOPES,
    GAME_DROPIN,
    STEAM_SCOPES,
    TAG,
    Drift,
    GamePaths,
    GameSettings,
    Manager,
    cpuset_in,
    ctl_file,
    drift_lines,
    drifted,
    dropin_text,
    escaped_unit,
    drift_readable,
    expand_cpus,
    first_cpu,
    has_record_part,
    intended_props,
    off_records,
    on_text,
    record_line,
    records,
    repaired_text,
    game_settings,
    show_values,
    shown,
    state_escapes,
    transient_unit,
    vm_io_weight,
)


def collate_as_bash() -> None:
    """Sort names as bash's globs do: by the collation of the caller's locale (LC_ALL, LC_COLLATE,
    LANG), C when it has none or an unknown one. The order the boxes next to the game are handled
    in, and listed by 'state', is then the bash version's."""
    with contextlib.suppress(locale.Error):
        locale.setlocale(locale.LC_COLLATE, "")


def read_settings(conf: str) -> GameSettings:
    """cpu-setup.conf's settings over the defaults (a file that cannot be read: the defaults).
    The lines bash would not read as plain assignments set nothing."""
    text = read_text(conf)
    return game_settings({} if text is None else parse_shell_assignments(text).values)


def user_id(user: str) -> str:
    """The user's uid as `id -u USER` prints it ("" and id's message on stderr for an unknown user)."""
    try:
        return str(pwd.getpwnam(user).pw_uid)
    except KeyError:
        print(f"id: '{user}': no such user", file=sys.stderr, flush=True)
        return ""


def read_stripped(path: str) -> str | None:
    """A file's text without its last newlines (a shell's $(cat FILE)), None when it cannot be read."""
    text = read_text(path)
    return None if text is None else text.rstrip("\n")


def _write_line(path: str, value: str) -> bool:
    """echo VALUE >PATH (a sysfs or cgroup file); False when it cannot be written."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value + "\n")
    except OSError:
        return False
    return True


def holds(path: str, needle: str) -> bool:
    """The file holds needle (grep -qsF): False when it cannot be read."""
    data = read_bytes(path)
    return data is not None and needle.encode() in data


class GameProfile:
    """on, off, tick and state of the game profile, over one runner and one set of paths."""

    def __init__(
        self,
        paths: GamePaths,
        settings: GameSettings,
        runner: CommandRunnerPort,
        *,
        uid_of: Callable[[str], str] = user_id,
        out: TextIO | None = None,
    ) -> None:
        self.paths = paths
        self.s = settings
        self._runner = runner
        self._uid_of = uid_of
        self._out = out

    # ---- output ----------------------------------------------------------------------
    def _stream(self) -> TextIO:
        return sys.stdout if self._out is None else self._out

    def log(self, prio: int, msg: str) -> None:
        """A journal line (<prio>game-profile: msg) on stdout."""
        journal_log(prio, msg, tag=TAG, stream=self._stream())

    def say(self, text: str) -> None:
        """A line of 'state'."""
        print(text, file=self._stream(), flush=True)

    def _relay(self, res: CommandResult) -> None:
        """What a command printed, on our streams (as if it had written there itself)."""
        if res.stdout:
            self._stream().write(res.stdout)
            self._stream().flush()
        if res.stderr:
            sys.stderr.write(res.stderr)
            sys.stderr.flush()

    # ---- systemd -------------------------------------------------------------------------
    def uid(self) -> str:
        """The target user's uid."""
        return self._uid_of(self.s.target_user)

    def _argv(self, mgr: Manager, args: Sequence[str]) -> list[str]:
        if mgr == "usr":
            return ["systemctl", "--user", "-M", f"{self.s.target_user}@", *args]
        return ["systemctl", *args]

    def ctl(self, mgr: Manager, *args: str, quiet: bool = False) -> int:
        """systemctl ARGS for the system manager (sys) or the user's (usr), its output on ours
        (quiet: its stderr dropped); the exit status."""
        argv = self._argv(mgr, args)
        if not quiet:
            self._stream().flush()
            return self._runner.run(argv, output="inherit").returncode
        res = self._runner.run(argv, output="stdout")
        self._relay(res)
        return res.returncode

    def ask(self, mgr: Manager, *args: str, keep_stderr: bool = False) -> CommandResult:
        """systemctl ARGS, its output captured (its stderr dropped, or shown with keep_stderr)."""
        res = self._runner.run(self._argv(mgr, args), output="capture" if keep_stderr else "stdout")
        if keep_stderr and res.stderr:
            sys.stderr.write(res.stderr)
            sys.stderr.flush()
        return res

    def prop(self, mgr: Manager, unit: str, name: str) -> str:
        """The property as `systemctl show -P` shows it ("" for [not set] and on an error)."""
        return shown(self.ask(mgr, "show", "-P", name, unit).stdout)

    def ctl_file(self, mgr: Manager, unit: str, prop: str) -> str:
        """Where `set-property --runtime` writes PROP of UNIT."""
        return ctl_file(self.paths, mgr, unit, prop, self.uid() if mgr == "usr" else "")

    def _units(self, mgr: Manager, pattern: str) -> list[str]:
        """The running scopes matching pattern (list-units, first column)."""
        res = self.ask(mgr, "list-units", "--type=scope", "--state=running", "--plain", "--no-legend", pattern)
        return [line.split()[0] for line in res.stdout.split("\n") if line.split()]

    # ---- the state files --------------------------------------------------------------------
    def _append(self, path: str, *fields: str) -> None:
        """One record at the end of path, on disk before the change it describes is made."""
        with open(path, "a", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(record_line(*fields))

    def _state_text(self) -> str:
        return read_text(self.paths.state) or ""

    def setp(self, mgr: Manager, unit: str, *settings: str) -> None:
        """Record, per property, whether a runtime override existed (and its value), then set
        them all; for a fixed unit, also record in game.intended what systemd shows right after."""
        for kv in settings:
            p = kv.split("=", 1)[0]
            if os.path.exists(self.ctl_file(mgr, unit, p)):
                self._append(self.paths.state, "prop", mgr, unit, p, "existed", self.prop(mgr, unit, p))
            else:
                self._append(self.paths.state, "prop", mgr, unit, p, "new", "-")
        if self.ctl(mgr, "set-property", "--runtime", unit, *settings) != 0:
            self.log(4, f"{unit}: could not set {' '.join(settings)}")
            return
        if transient_unit(unit):
            return
        for kv in settings:
            p, _, value = kv.partition("=")
            self._append(self.paths.intended, "prop", mgr, unit, p, value, self.prop(mgr, unit, p))

    # ---- the boxes ----------------------------------------------------------------------------
    def steam_scopes(self) -> list[str]:
        """Every Steam scope: the client's, and each game it started."""
        return self._units("usr", STEAM_SCOPES)

    def loose_docker_scopes(self) -> list[str]:
        """Docker containers outside builds.slice."""
        return [
            s
            for s in self._units("sys", DOCKER_SCOPES)
            if shown(self.ask("sys", "show", "-P", "Slice", s, keep_stderr=True).stdout) != "builds.slice"
        ]

    def uses_builds_slice(self) -> bool:
        """builds.slice is active (cpu docker-slice)."""
        return self.ask("sys", "-q", "is-active", "builds.slice").returncode == 0

    def gamemode_client(self) -> bool:
        """A process has GameMode's client library loaded (gamemoderun / libgamemodeauto)."""
        for maps in sorted(glob.glob(os.path.join(glob.escape(self.paths.proc), "[0-9]*", "maps"))):
            if holds(maps, "libgamemode"):
                return True
        return False

    def gamemode_pid(self, pid: str) -> bool:
        """The process has GameMode's client library loaded."""
        return holds(os.path.join(self.paths.proc, pid, "maps"), "libgamemode")

    def user_cg(self) -> str:
        """The user manager's own cgroup folder."""
        uid = self.uid()
        return f"{self.paths.cgroot}/user.slice/user-{uid}.slice/user@{uid}.service"

    def cpuset_delegated(self) -> bool:
        """cpuset in use by the user manager (cpu-setup install, next login)."""
        return cpuset_in(read_text(os.path.join(self.user_cg(), "cgroup.subtree_control")) or "")

    def escaped_cgroups(self) -> list[str]:
        """The cgroup folders of the user manager's boxes that no other rule covers: its top-level
        slices other than app, session and user.slice, and the systemd-run jobs straight in app.slice."""
        cg = glob.escape(self.user_cg())
        found: list[str] = []
        for pattern in ("*.slice", "app.slice/run-*.scope", "app.slice/run-*.service"):
            for d in sorted(glob.glob(f"{cg}/{pattern}"), key=locale.strxfrm):
                if os.path.isdir(d) and escaped_unit(os.path.basename(d)) is not None:
                    found.append(d)
        return found

    def gamemode_in(self, folder: str) -> bool:
        """A GameMode client (the game itself) runs in the cgroup folder or below it (4 levels)."""
        top = folder.rstrip("/").count("/")
        for d, subdirs, files in os.walk(folder):
            if d.rstrip("/").count("/") - top >= 3:
                subdirs.clear()
            if "cgroup.procs" in files:
                for pid in (read_text(os.path.join(d, "cgroup.procs")) or "").split("\n"):
                    if pid.strip() and self.gamemode_pid(pid.strip()):
                        return True
        return False

    def protect_steam(self) -> None:
        """MemoryLow for each Steam scope not handled yet (a game started after 'on')."""
        for s in self.steam_scopes():
            if has_record_part(self._state_text(), f" {s} MemoryLow "):
                continue
            self.setp("usr", s, f"MemoryLow={self.s.game_memory_low}")

    def pin_docker(self) -> None:
        """The game's CPUs off for each Docker container not handled yet."""
        for s in self.loose_docker_scopes():
            if has_record_part(self._state_text(), f" {s} AllowedCPUs "):
                continue
            self.setp("sys", s, f"AllowedCPUs={self.s.build_cpus}")

    def cpus_fallback(self, unit: str) -> None:
        """A user manager started before cpuset was delegated to it accepts AllowedCPUs but never
        writes cpuset.cpus: the cgroup file is written directly, and recorded for 'off'."""
        cg = self.ask("usr", "show", "-P", "ControlGroup", unit).stdout.rstrip("\n")
        f = f"{self.paths.cgroot}{cg}/cpuset.cpus"
        if not cg or not os.access(f, os.W_OK):
            return
        old = read_stripped(f) or ""
        if old == self.s.build_cpus:
            return
        self._append(self.paths.state, "cgw", f, old)
        if _write_line(f, self.s.build_cpus):
            self._append(self.paths.intended, "cgw", f, self.s.build_cpus, read_stripped(f) or "")
        else:
            self.log(4, f"{unit}: could not write {f}")

    def demote_escapes(self) -> None:
        """Idle-class (and off the game's E-cores, with cpuset delegated) for each box of
        escaped_cgroups not handled yet; a box that holds a GameMode client is the game itself."""
        settings = ["CPUWeight=idle"]
        if self.cpuset_delegated():
            settings.append(f"AllowedCPUs={self.s.build_cpus}")
        for d in self.escaped_cgroups():
            unit = os.path.basename(d).removeprefix("_")
            if has_record_part(self._state_text(), f"prop usr {printf_q(unit)} CPUWeight "):
                continue
            if self.gamemode_in(d):
                continue
            self.log(5, f"{unit}: idle while the game runs (a box of the user manager next to the game)")
            self.setp("usr", unit, *settings)

    # ---- the MemoryLow drop-ins -----------------------------------------------------------------
    def game_dropin_units(self) -> list[str]:
        """The memory chain on the system side."""
        uid = self.uid()
        return ["user.slice", f"user-{uid}.slice", f"user@{uid}.service"]

    def write_game_dropins(self) -> None:
        """The memory chain as drop-ins too, which hold across a daemon-reload (no reload now:
        setp set the live value already)."""
        for unit in self.game_dropin_units():
            d = os.path.join(self.paths.unit_runtime, f"{unit}.d")
            try:
                os.makedirs(d, exist_ok=True)
                write_atomic(
                    os.path.join(d, GAME_DROPIN),
                    dropin_text(unit, self.s.game_memory_low),
                    mode="umask",
                    durable=False,
                    relabel=False,
                )
            except OSError:
                self.log(4, f"{unit}: could not write {d}/{GAME_DROPIN}")

    def remove_game_dropins(self) -> bool:
        """Remove every such drop-in, whatever unit and user it is for; True when one was removed
        (a daemon-reload is due)."""
        removed = False
        for f in sorted(glob.glob(os.path.join(glob.escape(self.paths.unit_runtime), "*.d", GAME_DROPIN))):
            if not os.path.exists(f):
                continue
            with contextlib.suppress(OSError):
                os.unlink(f)
            with contextlib.suppress(OSError):
                os.rmdir(os.path.dirname(f))
            removed = True
        return removed

    def remove_leftover_dropins(self) -> None:
        """With the profile off: drop-ins left behind are removed, and the reload drops their MemoryLow."""
        if not self.remove_game_dropins():
            return
        self.ctl("sys", "daemon-reload")
        self.log(4, f"profile off, but its MemoryLow drop-ins ({GAME_DROPIN}) were still there: removed them")

    # ---- on, off, tick ---------------------------------------------------------------------------
    def on(self) -> int:
        """Switch the profile on (see the program's help)."""
        p, s = self.paths, self.s
        if os.path.exists(p.state):
            self.log(6, "already on")
            return 0
        if not s.target_user:
            self.log(3, f"TARGET_USER not set in {p.conf}")
            return 1
        for f in (p.state, p.intended):
            with open(f, "w", encoding="utf-8"):
                pass
        self.setp(
            "sys",
            "machine.slice",
            f"AllowedCPUs={s.build_cpus}",
            f"CPUWeight={s.vm_weight}",
            f"IOWeight={vm_io_weight(s.vm_weight)}",
        )
        if self.uses_builds_slice():
            self.setp("sys", "builds.slice", f"AllowedCPUs={s.build_cpus}", "CPUWeight=idle")
        self.pin_docker()
        self.setp("usr", "user.slice", "CPUWeight=idle")
        if self.cpuset_delegated():
            self.setp("usr", "user.slice", f"AllowedCPUs={s.build_cpus}")
            self.setp("usr", "flatpak-session-helper.service", f"AllowedCPUs={s.build_cpus}")
            self.cpus_fallback("user.slice")
            self.cpus_fallback("flatpak-session-helper.service")
        self.demote_escapes()
        for unit in self.game_dropin_units():
            self.setp("sys", unit, f"MemoryLow={s.game_memory_low}")
        self.write_game_dropins()
        self.setp("usr", "app.slice", f"MemoryLow={s.game_memory_low}")
        self.protect_steam()
        if s.game_zswap_writeback == "no" and self.prop("usr", "app.slice", "MemoryZSwapWriteback"):
            self.setp("usr", "app.slice", "MemoryZSwapWriteback=no")
        for cpu in expand_cpus(s.p_cores):
            f = p.epp(cpu)
            if not os.access(f, os.W_OK):
                continue
            self._append(p.state, "epp", cpu, read_stripped(f) or "")
            _write_line(f, "performance")
        self.log(5, on_text(s))
        return 0

    def off(self) -> int:
        """Exact revert: the overrides 'on' created go (then daemon-reload), the ones that existed
        before get their old value back; the MemoryLow drop-ins go also without a game.state."""
        p = self.paths
        if not os.path.exists(p.state):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(p.manual)
            self.remove_leftover_dropins()
            return 0
        reload = {"sys": self.remove_game_dropins(), "usr": False}
        # Newest first, so a property touched twice ends at its oldest value.
        recs, stopped = off_records(self._state_text())
        for rec in recs:
            self._undo(rec, reload)
        if stopped is not None:  # as bash's set -u ended it: no reload, game.state kept
            print(f"{p.state} line {stopped}: no record 'off' can read", file=sys.stderr, flush=True)
            return 1
        if reload["sys"]:
            self.ctl("sys", "daemon-reload")
        if reload["usr"]:
            self.ctl("usr", "daemon-reload")
        for f in (p.state, p.intended, p.manual):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(f)
        self.log(5, "off: previous CPU, weight, memory, zswap writeback and EPP settings restored")
        return 0

    def _undo(self, rec: Sequence[str], reload: dict[str, bool]) -> None:
        """Undo one record of game.state."""
        kind = rec[0] if rec else ""
        if kind == "cgw" and len(rec) >= 3:
            _write_line(rec[1], rec[2])
        elif kind == "epp" and len(rec) >= 3:
            _write_line(self.paths.epp(rec[1]), rec[2])
        elif len(rec) >= 6:
            mgr: Manager = "usr" if rec[1] == "usr" else "sys"
            unit, prop, mode, old = rec[2], rec[3], rec[4], rec[5]
            if mode == "new":
                f = self.ctl_file(mgr, unit, prop)
                with contextlib.suppress(OSError):
                    os.unlink(f)
                with contextlib.suppress(OSError):
                    os.rmdir(os.path.dirname(f))
                reload[mgr] = True
            else:
                self.ctl(mgr, "set-property", "--runtime", unit, f"{prop}={old}", quiet=True)

    def drift(self) -> list[Drift]:
        """The settings in game.intended that no longer hold (one systemctl show per unit)."""
        text = read_text(self.paths.intended) or ""
        if not text or not drift_readable(text):
            return []
        recs = records(text)
        now: dict[tuple[str, str], dict[str, str]] = {}
        for (mgr, unit), props in intended_props(recs).items():
            args = ["show", "-p", "LoadState"] + [a for prop in props for a in ("-p", prop)] + [unit]
            res = self.ask(mgr, *args)
            if res.returncode != 0:
                continue
            vals = show_values(res.stdout)
            if vals.get("LoadState") == "loaded":
                now[(mgr, unit)] = vals
        return drifted(recs, now, lambda f: (read_stripped(f) or "") if os.path.exists(f) else None)

    def repair_drift(self) -> None:
        """Set again what drift() lists, straight with set-property: not with setp, which would add
        a second record to game.state and make 'off' restore the profile's own value."""
        for d in self.drift():
            r = d.record
            if d.is_cgroup_file:
                ok = _write_line(r[1], r[2])
            else:
                mgr: Manager = "sys" if r[1] == "sys" else "usr"
                ok = self.ctl(mgr, "set-property", "--runtime", r[2], f"{r[3]}={r[4]}", quiet=True) == 0
            self.log(*repaired_text(d, ok=ok))

    def tick(self) -> int:
        """The watchdog (every 30 s); see the program's help."""
        p = self.paths
        if not os.path.exists(p.state):
            self.remove_leftover_dropins()
            if not self.gamemode_client():
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(p.suppress)
            elif not os.path.exists(p.suppress):
                self.log(5, "GameMode client running but profile off: switching on")
                self.ctl("sys", "start", "--no-block", "cpu-game.service")
            return 0
        if not os.path.exists(p.manual) and not self.gamemode_client():
            self.log(5, "no GameMode client left: switching off")
            self.ctl("sys", "stop", "--no-block", "cpu-game.service")
            return 0
        self.pin_docker()  # containers started meanwhile
        self.protect_steam()  # the game's own scope, started after 'on'
        self.demote_escapes()  # boxes next to the game that appeared after 'on'
        self.repair_drift()  # settings a daemon-reload or uresourced undid
        return 0

    # ---- state ------------------------------------------------------------------------------------
    def state(self) -> int:
        """What is applied now, and which settings drifted from what 'on' set."""
        p, s = self.paths, self.s
        on = os.path.exists(p.state)
        manual = os.path.exists(p.manual)
        self.say(f"game profile: ON{' (by hand)' if manual else ''}" if on else "game profile: off")
        self._slice_line("machine.slice")
        if self.uses_builds_slice():
            self._slice_line("builds.slice")
        if s.target_user:
            self.say(f"  toolboxes       weight={self.prop('usr', 'user.slice', 'CPUWeight')}")
            self.say(f"  app.slice       zswap writeback={self.prop('usr', 'app.slice', 'MemoryZSwapWriteback')}")
            cg = self.ask("usr", "show", "-P", "ControlGroup", "user.slice").stdout.rstrip("\n")
            effective = read_stripped(f"{p.cgroot}{cg}/cpuset.cpus.effective") or ""
            self.say(f"  toolboxes       cpus effective={effective}")
            if on:
                self.say(f"  other boxes     idle: {state_escapes(self._state_text())}")
        first = first_cpu(s.build_cpus)
        self.say(f"  EPP cpu0 {read_stripped(p.epp('0')) or ''}, cpu{first} {read_stripped(p.epp(first)) or ''}")
        if on:
            for line in self.drift_report():
                self.say(line)
        return 0

    def drift_report(self) -> list[str]:
        """The drift part of 'state': each setting that no longer holds, or that none drifted."""
        p = self.paths
        drifts = self.drift() if os.path.exists(p.intended) else None
        return drift_lines(drifts, manual=os.path.exists(p.manual))

    def _slice_line(self, unit: str) -> None:
        cpus, weight, io = (self.prop("sys", unit, n) for n in ("AllowedCPUs", "CPUWeight", "IOWeight"))
        self.say(f"  {unit:<15} cpus={cpus:<8} weight={weight:<4} io={io}")

    def locked(self, action: str) -> int:
        """on, off or tick under the run lock (60 s at most; then exit 1)."""
        run = self.paths.run
        try:
            ensure_dir(run, 0o755)
            os.chmod(run, 0o755)  # install -d -m 0755 sets the mode of a folder already there too
            fd = take_lock(self.paths.lock, wait=True, mode=0o644, timeout=60)
        except OSError:
            fd = None
        if fd is None:
            self.log(3, "lock busy")
            return 1
        try:
            return {"on": self.on, "off": self.off, "tick": self.tick}[action]()
        finally:
            os.close(fd)

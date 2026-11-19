# SPDX-License-Identifier: MPL-2.0
"""The game profile on the machine: systemctl for both managers, the cgroup tree, /proc, sysfs, its state files.

GameProfile carries what every step needs (where things are, the settings,
the command runner, the user's uid) across the steps of on, off, tick and
state; cpu.game.domain decides what the records and the answers say.
"""

from collections.abc import Callable, Sequence
import contextlib
import dataclasses
import glob
import locale
import os
import pwd
import sys
from typing import Final, TextIO

from cst_core.adapters.files import ensure_dir, read_bytes, read_text, write_atomic
from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.locks import take_lock
from cst_core.adapters.terminal import stdout
from cst_core.domain.text import SettingsProblem, describe_problems, parse_shell_assignments, printf_q
from cst_core.ports import CommandResult, CommandRunnerPort, OutputMode
from cpu.game.domain import (
    DOCKER_SCOPES,
    GAME_DROPIN,
    STEAM_SCOPES,
    TAG,
    CgroupUndo,
    Drift,
    EppUndo,
    GameRecordKind,
    GamePaths,
    GameSettings,
    Manager,
    Record,
    cpuset_in,
    ctl_file,
    cut_tail,
    drift_lines,
    drifted,
    dropin_text,
    escaped_unit,
    escapes_in_state,
    expand_cpus,
    first_cpu,
    has_record_part,
    intended_header,
    intended_props,
    intended_records,
    newer_intended,
    manager_of,
    on_text,
    PropUndo,
    record_line,
    records,
    repaired_text,
    unreadable_lines,
    game_settings,
    show_values,
    state_uid,
    state_undo,
    shown,
    transient_unit,
    unheld,
    unheld_text,
    vm_io_weight,
)


# How the watchdog's words on the settings end: it says them once per boot (GameProfile._say_once).
_ONCE: Final = " (fix it; said once per boot)"


def collate_as_bash() -> None:
    """Sort names as bash's globs do: by the collation of the caller's locale (LC_ALL, LC_COLLATE,
    LANG), C when it has none or an unknown one. The order the boxes next to the game are handled
    in, and listed by 'state', is then the bash version's."""
    with contextlib.suppress(locale.Error):
        locale.setlocale(locale.LC_COLLATE, "")


def read_settings(conf: str) -> tuple[GameSettings, tuple[SettingsProblem, ...]]:
    """cpu-setup.conf's settings over the defaults (a file that cannot be read: the defaults), and its
    problem lines: lines bash would not read as plain assignments, which set nothing."""
    text = read_text(conf)
    if text is None:
        return game_settings({}), ()
    parsed = parse_shell_assignments(text)
    return game_settings(parsed.values), parsed.problems


def load_profile(paths: GamePaths, runner: CommandRunnerPort) -> GameProfile:
    """The game profile over paths.conf's settings (and its problem lines)."""
    settings, problems = read_settings(paths.conf)
    return GameProfile(paths, settings, runner, problems=problems)


def user_id(user: str) -> str:
    """The user's uid as `id -u USER` prints it, "" for an unknown user (quietly: GameProfile says id's
    message where it is wanted)."""
    try:
        return str(pwd.getpwnam(user).pw_uid)
    except KeyError:
        return ""


def user_name(uid: str) -> str:
    """The name of the user with that uid, "" for none."""
    try:
        return pwd.getpwuid(int(uid)).pw_name
    except KeyError, ValueError, OverflowError:
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
        name_of: Callable[[str], str] = user_name,
        problems: Sequence[SettingsProblem] = (),
        out: TextIO | None = None,
    ) -> None:
        self.paths = paths
        self.s = settings
        self.problems = tuple(problems)
        self._runner = runner
        self._uid_of = uid_of
        self._name_of = name_of
        self._out = out
        self._uids: dict[str, str] = {}

    # ---- output ----------------------------------------------------------------------
    def _stream(self) -> TextIO:
        return stdout() if self._out is None else self._out

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
    def uid(self, *, say: bool = False) -> str:
        """The target user's uid (say: see _lookup_uid)."""
        return self._lookup_uid(self.s.target_user, say=say)

    def _lookup_uid(self, user: str, *, say: bool) -> str:
        """The uid of user ("" for none), looked up once per run. With say ('off' only), the first lookup
        of a user with no account prints id's "no such user" line on stderr, once; on, tick and state
        never print it."""
        if user not in self._uids:
            found = self._uid_of(user)
            if say and not found:
                print(f"id: '{user}': no such user", file=sys.stderr, flush=True)
            self._uids[user] = found
        return self._uids[user]

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
            return self._runner.run(argv, output=OutputMode.INHERIT).returncode
        res = self._runner.run(argv, output=OutputMode.STDOUT)
        self._relay(res)
        return res.returncode

    def ask(self, mgr: Manager, *args: str, keep_stderr: bool = False) -> CommandResult:
        """systemctl ARGS, its output captured (its stderr dropped, or shown with keep_stderr)."""
        res = self._runner.run(self._argv(mgr, args), output=OutputMode.CAPTURE if keep_stderr else OutputMode.STDOUT)
        if keep_stderr and res.stderr:
            sys.stderr.write(res.stderr)
            sys.stderr.flush()
        return res

    def prop(self, mgr: Manager, unit: str, name: str) -> str:
        """The property as `systemctl show -P` shows it ("" for [not set] and on an error)."""
        return shown(self.ask(mgr, "show", "-P", name, unit).stdout)

    def ctl_file(self, mgr: Manager, unit: str, prop: str, *, say: bool = False) -> str:
        """Where `set-property --runtime` writes PROP of UNIT (say: see _lookup_uid)."""
        return ctl_file(self.paths, mgr, unit, prop, self.uid(say=say) if mgr == "usr" else "")

    def _units(self, mgr: Manager, pattern: str) -> list[str]:
        """The running scopes matching pattern (list-units, first column)."""
        res = self.ask(mgr, "list-units", "--type=scope", "--state=running", "--plain", "--no-legend", pattern)
        return [line.split()[0] for line in res.stdout.split("\n") if line.split()]

    # ---- the state files --------------------------------------------------------------------
    def _append(self, path: str, *fields: str) -> bool:
        """One record at the end of path, on disk before the change it describes is made. False when it
        cannot be written (a full /run): said on stderr, as bash's failed redirection said it. A last
        line cut short is removed first (_drop_cut_tail)."""
        if not self._drop_cut_tail(path):
            return False
        try:
            with open(path, "a", encoding="utf-8", errors="surrogateescape") as fh:
                fh.write(record_line(*fields))
        except OSError as e:
            print(f"{path}: {e.strerror or e}", file=sys.stderr, flush=True)
            return False
        return True

    def _drop_cut_tail(self, path: str) -> bool:
        """Remove a last line with no newline at its end (a record cut short when 'on' died while it
        wrote it) and name it in the journal: a record glued onto it would become one line with the
        cut fields, which 'off' would read as a whole record (an old IOWeight "1" of "100"). False when
        the file cannot be cut (said on stderr, as a failed write)."""
        data = read_bytes(path) or b""
        cut = cut_tail(data)
        if cut is None:
            return True
        keep, line = cut
        try:
            os.truncate(path, keep)
        except OSError as e:
            print(f"{path}: {e.strerror or e}", file=sys.stderr, flush=True)
            return False
        why = "never read by the drift repair" if path == self.paths.intended else "its change was never made"
        number = data.count(b"\n") + 1
        text = line.decode("utf-8", "replace")
        self.log(4, f"{path} line {number}: a record cut short ({why}), removed before the next record: {text}")
        return True

    def _unrecorded(self, what: str) -> None:
        """A change not made because its record could not be written: 'off' could not undo it."""
        self.log(4, f"{what}: not done, its record could not be written to {self.paths.state}")

    def _state_text(self) -> str:
        return read_text(self.paths.state) or ""

    def setp(self, mgr: Manager, unit: str, *settings: str) -> None:
        """Record, per property, whether a runtime override existed (and its value), then set
        them all; for a fixed unit, also record in game.intended what systemd shows right after."""
        for kv in settings:
            p = kv.split("=", 1)[0]
            if os.path.exists(self.ctl_file(mgr, unit, p)):
                rec = (GameRecordKind.PROP, mgr, unit, p, "existed", self.prop(mgr, unit, p))
            else:
                rec = (GameRecordKind.PROP, mgr, unit, p, "new", "-")
            if not self._append(self.paths.state, *rec):
                self._unrecorded(f"{unit}: {' '.join(settings)}")
                return
        if self.ctl(mgr, "set-property", "--runtime", unit, *settings) != 0:
            self.log(4, f"{unit}: could not set {' '.join(settings)}")
            return
        if transient_unit(unit):
            return
        for kv in settings:
            p, _, value = kv.partition("=")
            self._append(self.paths.intended, GameRecordKind.PROP, mgr, unit, p, value, self.prop(mgr, unit, p))

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
            found.extend(
                d
                for d in sorted(glob.glob(f"{cg}/{pattern}"), key=locale.strxfrm)
                if os.path.isdir(d) and escaped_unit(os.path.basename(d)) is not None
            )
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
        if not self._append(self.paths.state, GameRecordKind.CGW, f, old):
            self._unrecorded(f"{unit}: {f} = {self.s.build_cpus}")
            return
        if _write_line(f, self.s.build_cpus):
            self._append(self.paths.intended, GameRecordKind.CGW, f, self.s.build_cpus, read_stripped(f) or "")
        else:
            self.log(4, f"{unit}: could not write {f}")

    def _recorded(self, unit: str, settings: Sequence[str]) -> bool:
        """game.state holds a whole record of each of settings for unit, a box of the user manager: setp
        records them one after the other before it sets any, so a box whose last record was cut short
        (or never written) is not handled yet, even when its first record is whole."""
        text = self._state_text()
        return all(has_record_part(text, f"prop usr {printf_q(unit)} {kv.split('=', 1)[0]} ") for kv in settings)

    def demote_escapes(self) -> None:
        """Idle-class (and off the game's E-cores, with cpuset delegated) for each box of
        escaped_cgroups not handled yet; a box that holds a GameMode client is the game itself."""
        settings = ["CPUWeight=idle"]
        if self.cpuset_delegated():
            settings.append(f"AllowedCPUs={self.s.build_cpus}")
        for d in self.escaped_cgroups():
            unit = os.path.basename(d).removeprefix("_")
            if self._recorded(unit, settings):
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
        why = None if self.problems else self._target_refusal()
        if self.problems or why is not None:
            # Not switched on: a by-hand mark ('cst cpu game on' makes it first) would keep a profile a
            # later game switches on from being switched off when that game ends.
            with contextlib.suppress(FileNotFoundError):
                os.unlink(p.manual)
        if self.problems:
            self.refuse("not switched on")
            return 1
        if why is not None:
            self.log(3, why)
            return 1
        try:
            with open(p.state, "w", encoding="utf-8"):
                pass
        except OSError as e:
            self.log(3, f"not switched on: could not write {p.state}: {e.strerror or e}")
            return 1
        with contextlib.suppress(OSError):  # a new session: its drift is said again
            os.unlink(p.drift_warned)
        try:
            with open(p.intended, "w", encoding="utf-8") as fh:
                fh.write(intended_header())
        except OSError as e:  # no drift repair then; the profile itself still works
            print(f"{p.intended}: {e.strerror or e}", file=sys.stderr, flush=True)
        # The memory chain first, leaf first: game.state's first record is then user@UID.service's,
        # so 'off' knows the user 'on' acted for whatever step 'on' died at (state_uid).
        for unit in reversed(self.game_dropin_units()):
            self.setp("sys", unit, f"MemoryLow={s.game_memory_low}")
        self.write_game_dropins()
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
        self.setp("usr", "app.slice", f"MemoryLow={s.game_memory_low}")
        self.protect_steam()
        if s.game_zswap_writeback == "no" and self.prop("usr", "app.slice", "MemoryZSwapWriteback"):
            self.setp("usr", "app.slice", "MemoryZSwapWriteback=no")
        for cpu in expand_cpus(s.p_cores):
            f = p.epp(cpu)
            if not os.access(f, os.W_OK):
                continue
            if not self._append(p.state, GameRecordKind.EPP, cpu, read_stripped(f) or ""):
                self._unrecorded(f"cpu{cpu}: EPP performance")
                continue
            _write_line(f, "performance")
        self.log(5, on_text(s))
        return 0

    def _target_refusal(self) -> str | None:
        """Why 'on' refuses for TARGET_USER: unset, or a name with no account (it would act for user@.service
        and user-.slice); None for an account. The lookup is quiet: the caller's line names the user."""
        name, conf = self.s.target_user, self.paths.conf
        if not name:
            return f"TARGET_USER not set in {conf}"
        if not self._lookup_uid(name, say=False):
            return f"TARGET_USER names no account: {name} (in {conf})"
        return None

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
        text = self._state_text()
        for number, line in unreadable_lines(text, intended=False):
            self.log(4, f"{p.state} line {number}: no record 'off' can read, skipped: {line}")
        recs = records(text)
        self._user_from_state(recs)
        # Newest first, so a property touched twice ends at its oldest value.
        for rec in reversed(recs):
            self._undo(rec, reload)
        if reload["sys"]:
            self.ctl("sys", "daemon-reload")
        if reload["usr"]:
            self.ctl("usr", "daemon-reload")
        for f in (p.state, p.intended, p.manual, p.drift_warned):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(f)
        self.log(5, "off: previous CPU, weight, memory, zswap writeback and EPP settings restored")
        return 0

    def _user_from_state(self, recs: Sequence[Record], *, say: bool = True) -> None:
        """'off' undoes the user manager's side for the user 'on' acted for, whose uid game.state names:
        also with no TARGET_USER (its line sets nothing, or is gone) or one that names another user now.
        The tick and 'state' act for that user too, quietly (say False: neither the line below nor id's
        "no such user" line, which 'off' says once: _lookup_uid), so what the tick sets in the user
        manager is what 'off' undoes."""
        uid = state_uid(recs)
        if uid is None:
            return
        target = self.s.target_user
        if target and self._lookup_uid(target, say=say) == uid:
            return
        name = self._name_of(uid)
        if name:
            if say:
                conf = self.paths.conf
                why = f"TARGET_USER in {conf} is {target}" if target else f"TARGET_USER not set in {conf}"
                self.log(5, f"{why}: undoing for {name} (uid {uid}), the user 'on' acted for")
            self.s = dataclasses.replace(self.s, target_user=name)

    def _undo(self, rec: Sequence[str], reload: dict[str, bool]) -> None:
        """Undo one record of game.state (as state_undo reads it)."""
        match state_undo(rec):
            case CgroupUndo(file=f, old=old):
                _write_line(f, old)
            case EppUndo(cpu=cpu, old=old):
                _write_line(self.paths.epp(cpu), old)
            case PropUndo(mgr=mgr, unit=unit, prop=prop, mode=mode, old=old):
                if mode == "new":
                    f = self.ctl_file(mgr, unit, prop, say=True)
                    with contextlib.suppress(OSError):
                        os.unlink(f)
                    with contextlib.suppress(OSError):
                        os.rmdir(os.path.dirname(f))
                    reload[mgr] = True
                else:
                    self.ctl(mgr, "set-property", "--runtime", unit, f"{prop}={old}", quiet=True)
            case None:
                pass

    def drift(self, text: str) -> list[Drift]:
        """The settings in game.intended (its text) that no longer hold (one systemctl show per unit)."""
        recs = intended_records(text)  # as cst doctor reads it
        now: dict[tuple[str, str], dict[str, str]] = {}
        for (mgr, unit), props in intended_props(recs).items():
            args = ["show", "-p", "LoadState"] + [a for prop in props for a in ("-p", prop)] + [unit]
            res = self.ask(mgr, *args)
            if res.returncode != 0:
                continue
            vals = show_values(res.stdout)
            if vals.get("LoadState") == "loaded":
                now[mgr, unit] = vals
        return drifted(recs, now, lambda f: (read_stripped(f) or "") if os.path.exists(f) else None)

    def repair_drift(self) -> None:
        """Set again what drift() lists, straight with set-property: not with setp, which would add
        a second record to game.state and make 'off' restore the profile's own value. A game.intended
        of a newer CST is not read (its records may mean something else), and a setting game.state holds
        no record of is not set again ('off' could not undo it): each said in the journal once per boot
        (again when the text changes, and when it comes back after a tick that had nothing to say: _say_unheld)."""
        text, newer = self._intended()
        if newer is not None:
            said = f"drift not repaired: {newer}"
            self._say_once(self.paths.drift_warned, said, lambda: self.log(4, said))
            return
        drifts = self.drift(text)
        skipped = self._unheld(drifts)
        self._say_unheld(skipped)
        for d in drifts:
            if d in skipped:
                continue
            r = d.record
            if d.is_cgroup_file:
                ok = _write_line(r[1], r[2])
            else:
                ok = (
                    self.ctl(manager_of(r[1]) or "usr", "set-property", "--runtime", r[2], f"{r[3]}={r[4]}", quiet=True)
                    == 0
                )
            self.log(*repaired_text(d, ok=ok))

    def _unheld(self, drifts: Sequence[Drift]) -> list[Drift]:
        """The drifts game.state holds no record of (unheld)."""
        return unheld(drifts, records(self._state_text()))

    def _say_unheld(self, skipped: Sequence[Drift]) -> None:
        """Say the drifts the tick does not set again ('off' could not undo them) once per boot, at
        notice; with none, forget the mark, so a later one is said again."""
        if not skipped:
            with contextlib.suppress(OSError):
                os.unlink(self.paths.drift_warned)
            return
        said = "\n".join(unheld_text(d) for d in skipped)

        def emit() -> None:
            for line in said.split("\n"):
                self.log(5, line)

        self._say_once(self.paths.drift_warned, said, emit)

    def tick(self) -> int:
        """The watchdog (every 30 s); see the program's help."""
        p = self.paths
        if not self.problems:  # fixed: a line or a name that breaks again is said again
            self._forget_fixed_settings()
        if not os.path.exists(p.state):
            self.remove_leftover_dropins()
            if not self.gamemode_client():
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(p.suppress)
            elif not os.path.exists(p.suppress):
                if self.problems:  # 'on' would refuse: no failed cpu-game.service every 30 s
                    self.warn_once("GameMode client running, profile not switched on")
                    return 0
                why = self._target_refusal()  # the same for TARGET_USER, with the same mark
                if why is not None:
                    self._say_refusal("GameMode client running, profile not switched on", why)
                else:
                    self.log(5, "GameMode client running but profile off: switching on")
                    self.ctl("sys", "start", "--no-block", "cpu-game.service")
            return 0
        if not os.path.exists(p.manual) and not self.gamemode_client():
            self.log(5, "no GameMode client left: switching off")
            self.ctl("sys", "stop", "--no-block", "cpu-game.service")
            return 0
        if self.problems:
            self.warn_once("tick changes nothing")
            return 0
        self._user_from_state(records(self._state_text()), say=False)  # the user 'off' undoes for
        self.pin_docker()  # containers started meanwhile
        self.protect_steam()  # the game's own scope, started after 'on'
        self.demote_escapes()  # boxes next to the game that appeared after 'on'
        self.repair_drift()  # settings a daemon-reload or uresourced undid
        return 0

    def _say_refusal(self, what: str, why: str) -> None:
        """The watchdog's word on a TARGET_USER 'on' refuses (_target_refusal's why), once per boot, in the
        mark of the settings file's problem lines (_forget_fixed_settings)."""
        said = f"{what}: {why}{_ONCE}"
        self._say_once(self.paths.settings_warned, said, lambda: self.log(4, said))

    def _forget_fixed_settings(self) -> None:
        """With no problem line: forget what the watchdog said about the settings (a line or a name that
        breaks again is then said again), unless it is about a TARGET_USER 'on' still refuses for. The
        name is looked up only when there is such a mark."""
        mark = self.paths.settings_warned
        said = read_text(mark)
        if said is not None:
            why = self._target_refusal()
            if why is not None and said.endswith(f": {why}{_ONCE}"):
                return
        with contextlib.suppress(OSError):
            os.unlink(mark)

    def _problem_lines(self) -> list[str]:
        """The settings file's problem lines, one message each (none when it has none)."""
        return describe_problems(self.paths.conf, self.problems).split("\n") if self.problems else []

    def _log_problems(self, prio: int, what: str, suffix: str = "") -> None:
        """The problem lines in the journal, then what the profile did not do."""
        for line in self._problem_lines():
            self.log(prio, line)
        self.log(prio, f"{what}: a line of {self.paths.conf} sets nothing (fix it; 'off' still works{suffix})")

    def refuse(self, what: str) -> None:
        """The settings file's problem lines in the journal, and what the profile did not do."""
        self._log_problems(3, what)

    def warn_once(self, what: str) -> None:
        """The watchdog's word on the settings file's problem lines: logged at the first tick that meets
        them (and again when they or the word change, or after a tick found the file fixed), then quiet
        until the next boot (the mark is in /run); the watchdog's unit does not fail on them."""
        said = "\n".join([what, *self._problem_lines()])  # another word on the same lines is said too
        self._say_once(self.paths.settings_warned, said, lambda: self._log_problems(4, what, "; said once per boot"))

    @staticmethod
    def _say_once(mark: str, said: str, emit: Callable[[], None]) -> None:
        """emit() unless the mark holds said (it was said already this boot: the mark is in /run); then
        the mark holds said, so another text is said again."""
        if read_text(mark) == said:
            return
        emit()
        with contextlib.suppress(OSError):
            write_atomic(mark, said, mode=0o644, durable=False, relabel=False)

    # ---- state ------------------------------------------------------------------------------------
    def state(self) -> int:
        """What is applied now, and which settings drifted from what 'on' set (with the profile on, in the
        user manager of the user 'on' acted for)."""
        on = os.path.exists(self.paths.state)
        if on:
            self._user_from_state(records(self._state_text()), say=False)
        p, s = self.paths, self.s
        manual = os.path.exists(p.manual)
        self.say(f"game profile: ON{' (by hand)' if manual else ''}" if on else "game profile: off")
        for line in self._problem_lines():
            self.say(
                f"  settings: {line} (sets nothing: 'on' refuses and the watchdog changes nothing until it is fixed)"
            )
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
                self.say(f"  other boxes     idle: {escapes_in_state(records(self._state_text()))}")
        if on:
            intended_text, newer = self._intended()
            files = [(p.state, self._state_text(), False)]
            if newer is None:  # a newer format's lines are not judged
                files.append((p.intended, intended_text, True))
            for path, text, intended in files:
                for number, line in unreadable_lines(text, intended=intended):
                    self.say(f"  unreadable: {path} line {number} (skipped): {line}")
        first = first_cpu(s.build_cpus)
        self.say(f"  EPP cpu0 {read_stripped(p.epp('0')) or ''}, cpu{first} {read_stripped(p.epp(first)) or ''}")
        if on:
            for line in self.drift_report():
                self.say(line)
        return 0

    def drift_report(self) -> list[str]:
        """The drift part of 'state': each setting that no longer holds, or that none drifted."""
        p = self.paths
        if not os.path.exists(p.intended):
            return drift_lines(None, manual=os.path.exists(p.manual))
        text, newer = self._intended()
        drifts = self.drift(text) if newer is None else None
        not_held = [] if drifts is None else self._unheld(drifts)
        return drift_lines(drifts, manual=os.path.exists(p.manual), newer=newer, not_held=not_held)

    def _intended(self) -> tuple[str, str | None]:
        """game.intended's text, and why it cannot be compared (None: it can)."""
        text = read_text(self.paths.intended) or ""
        return text, newer_intended(self.paths.intended, text)

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

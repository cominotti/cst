# SPDX-License-Identifier: MPL-2.0
"""The game profile's protocol: on, off, tick and state, over its ports (cpu.game.ports). No I/O.

GameProfileSteps carries what every step needs (where things are, the settings, the ports, the user's
uid) across the steps of on, off, tick and state; cpu.game.domain decides what the records and the
answers say. leftovers, drift, boxes, report and power hold the steps of game.leftovers, the drift
repair, the boxes handled already, 'state' and the power settings. The front that builds the real
adapters is cpu.game.adapters.profile.GameProfile.
"""

from collections.abc import Callable, Iterable, Sequence
import contextlib
import dataclasses
import os
from typing import Final

from cst_core.domain.text import SettingsProblem, describe_problems
from cst_core.ports import CommandResult, OutputMode
from cpu.game.application.boxes import set_again
from cpu.game.application.drift import repair_drift, settings_drift
from cpu.game.application.leftovers import Reloads, keep, read_kept, retry_kept, undo_pass, unit_gone, user_side
from cpu.game.application.power import apply_game_cstates, apply_game_gpu, boost_p_cores
from cpu.game.application.report import show_state
from cpu.game.domain.boxes import shown_form
from cpu.game.domain.leftovers import (
    LEFTOVER_DROPINS_TEXT,
    UserSide,
    merged_entries,
    of_user_manager,
    off_text,
    user_side_text,
)
from cpu.game.domain import (
    APP_SLICE,
    BUILDS_SLICE,
    DOCKER_SCOPES,
    GAME_DROPIN,
    STEAM_SCOPES,
    USER_SLICE,
    CgroupUndo,
    Drift,
    EppUndo,
    GameRecordKind,
    GamePaths,
    GameSettings,
    IDLE_WEIGHT,
    Manager,
    Record,
    SysfsUndo,
    cpuset_in,
    ctl_file,
    cut_tail,
    drift_lines,
    dropin_text,
    escaped_unit,
    has_prop_record,
    intended_header,
    newer_intended,
    on_text,
    PropUndo,
    record_line,
    records,
    unreadable_lines,
    state_uid,
    state_undo,
    shown,
    transient_unit,
    unheld,
    unheld_text,
    vm_io_weight,
)
from cpu.game.ports import GamePorts


# How the watchdog's words on the settings end: it says them once per boot (GameProfile.say_once).
_ONCE: Final = " (fix it; said once per boot)"


# How deep below a box the game is looked for (gamemode_in: 4 levels).
_BOX_DEPTH: Final = 3


class GameProfileSteps:
    """on, off, tick and state of the game profile, over one set of ports and one set of paths."""

    def __init__(
        self,
        paths: GamePaths,
        settings: GameSettings,
        ports: GamePorts,
        *,
        problems: Sequence[SettingsProblem] = (),
    ) -> None:
        self.paths = paths
        self.s = settings
        self.problems = tuple(problems)
        self.ports = ports
        self._uids: dict[str, str] = {}

    # ---- output ----------------------------------------------------------------------
    def log(self, prio: int, msg: str) -> None:
        """A journal line (<prio>game-profile: msg) on stdout."""
        self.ports.out.log(prio, msg)

    def say(self, text: str) -> None:
        """A line of 'state'."""
        self.ports.out.say(text)

    def say_lines(self, lines: Iterable[str]) -> None:
        """Lines of 'state', one say each."""
        for line in lines:
            self.say(line)

    # ---- systemd -------------------------------------------------------------------------
    def uid(self, *, say: bool = False) -> str:
        """The target user's uid (say: see _lookup_uid)."""
        return self._lookup_uid(self.s.target_user, say=say)

    def _lookup_uid(self, user: str, *, say: bool) -> str:
        """The uid of user ("" for none), looked up once per run. With say ('off' only), the first lookup
        of a user with no account prints id's "no such user" line on stderr, once; on, tick and state
        never print it."""
        if user not in self._uids:
            found = self.ports.uid_of(user)
            if say and not found:
                self.ports.out.error(f"id: '{user}': no such user")
            self._uids[user] = found
        return self._uids[user]

    def ctl(self, mgr: Manager, *args: str, quiet: bool = False, user: str | None = None) -> int:
        """systemctl ARGS for the system manager (sys) or the user's (usr: the user acted for, or user),
        its output on ours (quiet: its stderr dropped); the exit status."""
        name = self.s.target_user if user is None else user
        if not quiet:
            self.ports.out.flush()
            return self.ports.systemd.run(mgr, name, args, output=OutputMode.INHERIT).returncode
        res = self.ports.systemd.run(mgr, name, args, output=OutputMode.STDOUT)
        self.ports.out.relay(res)
        return res.returncode

    def ask(self, mgr: Manager, *args: str, keep_stderr: bool = False) -> CommandResult:
        """systemctl ARGS, its output captured (its stderr dropped, or shown with keep_stderr)."""
        output = OutputMode.CAPTURE if keep_stderr else OutputMode.STDOUT
        res = self.ports.systemd.run(mgr, self.s.target_user, args, output=output)
        if keep_stderr and res.stderr:
            self.ports.out.stderr_text(res.stderr)
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
            self.ports.state.append(path, record_line(*fields))
        except OSError as e:
            self.ports.out.error(f"{path}: {e.strerror or e}")
            return False
        return True

    def _drop_cut_tail(self, path: str) -> bool:
        """Remove a last line with no newline at its end (a record cut short when 'on' died while it
        wrote it) and name it in the journal: a record glued onto it would become one line with the
        cut fields, which 'off' would read as a whole record (an old IOWeight "1" of "100"). False when
        the file cannot be cut (said on stderr, as a failed write)."""
        data = self.ports.state.read_bytes(path) or b""
        cut = cut_tail(data)
        if cut is None:
            return True
        keep, line = cut
        try:
            self.ports.state.truncate(path, keep)
        except OSError as e:
            self.ports.out.error(f"{path}: {e.strerror or e}")
            return False
        why = "never read by the drift repair" if path == self.paths.intended else "its change was never made"
        number = data.count(b"\n") + 1
        text = line.decode("utf-8", "replace")
        self.log(4, f"{path} line {number}: a record cut short ({why}), removed before the next record: {text}")
        return True

    def report_unrecorded(self, what: str) -> None:
        """A change not made because its record could not be written: 'off' could not undo it."""
        self.log(4, f"{what}: not done, its record could not be written to {self.paths.state}")

    def state_text(self) -> str:
        """Read current `game.state` contents (empty when missing)."""
        return self.ports.state.read_text(self.paths.state) or ""

    def append_state(self, *fields: str) -> bool:
        """Append one whole record to `game.state`."""
        return self._append(self.paths.state, *fields)

    def append_intended(self, *fields: str) -> bool:
        """Append one whole record to game.intended (best effort: a failed write stops nothing)."""
        return self._append(self.paths.intended, *fields)

    def setp(self, mgr: Manager, unit: str, *settings: str) -> None:
        """Record, per property, whether a runtime override existed (and its value), then set them all. For a
        fixed unit, game.intended gets each value as systemd will show it (shown_form) before the set, so the
        drift repair sets one whose set-property failed; after a set that worked, a second record when systemd
        shows another form (the last record of a setting wins: intended_records), never for an empty read-back."""
        for kv in settings:
            p = kv.split("=", 1)[0]
            if self.ports.units.exists(self.ctl_file(mgr, unit, p)):
                rec = (GameRecordKind.PROP, mgr, unit, p, "existed", self.prop(mgr, unit, p))
            else:
                rec = (GameRecordKind.PROP, mgr, unit, p, "new", "-")
            if not self._append(self.paths.state, *rec):
                self.report_unrecorded(f"{unit}: {' '.join(settings)}")
                return
        fixed = [kv.partition("=")[::2] for kv in settings] if not transient_unit(unit) else []
        for p, value in fixed:
            self.append_intended(GameRecordKind.PROP, mgr, unit, p, value, shown_form(p, value))
        if self.ctl(mgr, "set-property", "--runtime", unit, *settings) != 0:
            self.log(4, f"{unit}: could not set {' '.join(settings)}")
            return
        for p, value in fixed:
            # An empty read-back (an error, [not set]) says nothing of the form: the predicted record stays last.
            if (now := self.prop(mgr, unit, p)) not in {"", shown_form(p, value)}:
                self.append_intended(GameRecordKind.PROP, mgr, unit, p, value, now)

    # ---- the boxes ----------------------------------------------------------------------------
    def steam_scopes(self) -> list[str]:
        """Every Steam scope: the client's, and each game it started."""
        return self._units("usr", STEAM_SCOPES)

    def loose_docker_scopes(self) -> list[str]:
        """Docker containers outside builds.slice."""
        return [
            s
            for s in self._units("sys", DOCKER_SCOPES)
            if shown(self.ask("sys", "show", "-P", "Slice", s, keep_stderr=True).stdout) != BUILDS_SLICE
        ]

    def uses_builds_slice(self) -> bool:
        """builds.slice is active (cpu docker-slice)."""
        return self.ask("sys", "-q", "is-active", BUILDS_SLICE).returncode == 0

    def gamemode_client(self) -> bool:
        """A process has GameMode's client library loaded (gamemoderun / libgamemodeauto)."""
        for maps in self.ports.kernel.process_maps(self.paths.proc):
            if self.ports.kernel.holds(maps, "libgamemode"):
                return True
        return False

    def gamemode_pid(self, pid: str) -> bool:
        """The process has GameMode's client library loaded."""
        return self.ports.kernel.holds(os.path.join(self.paths.proc, pid, "maps"), "libgamemode")

    def user_cg(self) -> str:
        """The user manager's own cgroup folder."""
        uid = self.uid()
        return f"{self.paths.cgroot}/user.slice/user-{uid}.slice/user@{uid}.service"

    def cpuset_delegated(self) -> bool:
        """cpuset in use by the user manager (cpu-setup install, next login)."""
        return cpuset_in(self.ports.kernel.read_text(os.path.join(self.user_cg(), "cgroup.subtree_control")) or "")

    def escaped_cgroups(self) -> list[str]:
        """The cgroup folders of the user manager's boxes that no other rule covers: its top-level
        slices other than app, session and user.slice, and the systemd-run jobs straight in app.slice."""
        cg = self.user_cg()
        found: list[str] = []
        for pattern in ("*.slice", "app.slice/run-*.scope", "app.slice/run-*.service"):
            found.extend(
                d for d in self.ports.kernel.boxes(cg, pattern) if escaped_unit(os.path.basename(d)) is not None
            )
        return found

    def gamemode_in(self, folder: str) -> bool:
        """A GameMode client (the game itself) runs in the cgroup folder or below it (4 levels)."""
        for text in self.ports.kernel.procs_texts(folder, _BOX_DEPTH):
            for pid in text.split("\n"):
                if pid.strip() and self.gamemode_pid(pid.strip()):
                    return True
        return False

    def protect_steam(self) -> None:
        """MemoryLow for each Steam scope not handled yet (a game started after 'on'); one handled already
        is set again when its MemoryLow does not hold (set_again)."""
        for s in self.steam_scopes():
            setting = f"MemoryLow={self.s.game_memory_low}"
            if has_prop_record(self.state_text(), "usr", s, "MemoryLow"):
                set_again(self, "usr", s, [setting])
                continue
            self.setp("usr", s, setting)

    def pin_docker(self) -> None:
        """The game's CPUs off for each Docker container not handled yet; one handled already is set again
        when its AllowedCPUs does not hold (set_again)."""
        for s in self.loose_docker_scopes():
            setting = f"AllowedCPUs={self.s.build_cpus}"
            if has_prop_record(self.state_text(), "sys", s, "AllowedCPUs"):
                set_again(self, "sys", s, [setting])
                continue
            self.setp("sys", s, setting)

    def cpus_fallback(self, unit: str) -> None:
        """A user manager started before cpuset was delegated to it accepts AllowedCPUs but never
        writes cpuset.cpus: the cgroup file is written directly, and recorded for 'off'."""
        cg = self.ask("usr", "show", "-P", "ControlGroup", unit).stdout.rstrip("\n")
        f = f"{self.paths.cgroot}{cg}/cpuset.cpus"
        if not cg or not self.ports.kernel.writable(f):
            return
        old = self.ports.kernel.read_stripped(f) or ""
        if old == self.s.build_cpus:
            return
        if not self._append(self.paths.state, GameRecordKind.CGW, f, old):
            self.report_unrecorded(f"{unit}: {f} = {self.s.build_cpus}")
            return
        if self.ports.kernel.write(f, self.s.build_cpus):
            self._append(
                self.paths.intended, GameRecordKind.CGW, f, self.s.build_cpus, self.ports.kernel.read_stripped(f) or ""
            )
        else:
            self.log(4, f"{unit}: could not write {f}")

    def _recorded(self, unit: str, settings: Sequence[str]) -> bool:
        """game.state holds a whole record of each of settings for unit, a box of the user manager: setp
        records them one after the other before it sets any, so a box whose last record was cut short
        (or never written) is not handled yet, even when its first record is whole."""
        text = self.state_text()
        return all(has_prop_record(text, "usr", unit, kv.split("=", 1)[0]) for kv in settings)

    def demote_escapes(self) -> None:
        """Idle-class (and off the game's E-cores, with cpuset delegated) for each box of
        escaped_cgroups not handled yet; a box that holds a GameMode client is the game itself. One handled
        already is set again when a setting of it does not hold (set_again)."""
        settings = [IDLE_WEIGHT]
        if self.cpuset_delegated():
            settings.append(f"AllowedCPUs={self.s.build_cpus}")
        for d in self.escaped_cgroups():
            unit = os.path.basename(d).removeprefix("_")
            if self._recorded(unit, settings):
                set_again(self, "usr", unit, settings, folder=d)
                continue
            if self.gamemode_in(d):
                continue
            self.log(5, f"{unit}: idle while the game runs (a box of the user manager next to the game)")
            self.setp("usr", unit, *settings)

    # ---- the MemoryLow drop-ins -----------------------------------------------------------------
    def game_dropin_units(self) -> list[str]:
        """The memory chain on the system side."""
        uid = self.uid()
        return [USER_SLICE, f"user-{uid}.slice", f"user@{uid}.service"]

    def write_game_dropins(self) -> None:
        """The memory chain as drop-ins too, which hold across a daemon-reload (no reload now:
        setp set the live value already)."""
        for unit in self.game_dropin_units():
            d = os.path.join(self.paths.unit_runtime, f"{unit}.d")
            try:
                self.ports.units.make_folder(d)
                self.ports.units.write_dropin(os.path.join(d, GAME_DROPIN), dropin_text(unit, self.s.game_memory_low))
            except OSError:
                self.log(4, f"{unit}: could not write {d}/{GAME_DROPIN}")

    def game_dropins(self) -> list[str]:
        """Every such drop-in there is, whatever unit and user it is for."""
        return self.ports.units.dropins(self.paths.unit_runtime, GAME_DROPIN)

    def remove_game_dropins(self) -> bool:
        """Remove every such drop-in; True when one was removed (a daemon-reload is due)."""
        removed = False
        for f in self.game_dropins():
            with contextlib.suppress(OSError):
                self.ports.units.remove(f)
            with contextlib.suppress(OSError):
                self.ports.units.remove_folder(os.path.dirname(f))
            removed = True
        return removed

    def remove_leftover_dropins(self) -> None:
        """With the profile off and a game.leftovers this CST does not read (retry_kept): drop-ins left behind
        are removed, and the reload drops their MemoryLow."""
        if not self.remove_game_dropins():
            return
        self.ctl("sys", "daemon-reload")
        self.log(4, LEFTOVER_DROPINS_TEXT)

    # ---- on, off, tick ---------------------------------------------------------------------------
    def on(self) -> int:
        """Switch the profile on (see the program's help)."""
        p, s = self.paths, self.s
        if self.ports.state.exists(p.state):
            self.log(6, "already on")
            return 0
        why = None if self.problems else self.target_refusal()
        if self.problems or why is not None:
            # Not switched on: a by-hand mark ('cst cpu game on' makes it first) would keep a profile a
            # later game switches on from being switched off when that game ends.
            with contextlib.suppress(FileNotFoundError):
                self.ports.state.remove(p.manual)
        if self.problems:
            self.refuse("not switched on")
            return 1
        if why is not None:
            self.log(3, why)
            return 1
        try:
            self.ports.state.create(p.state, "")  # an empty game.state: the profile is on, with nothing to undo yet
        except OSError as e:
            self.log(3, f"not switched on: could not write {p.state}: {e.strerror or e}")
            return 1
        with contextlib.suppress(OSError):  # a new session: its drift is said again
            self.ports.state.remove(p.drift_warned)
        try:
            self.ports.state.create(p.intended, intended_header())
        except OSError as e:  # no drift repair then; the profile itself still works
            self.ports.out.error(f"{p.intended}: {e.strerror or e}")
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
            self.setp("sys", BUILDS_SLICE, f"AllowedCPUs={s.build_cpus}", IDLE_WEIGHT)
        self.pin_docker()
        self.setp("usr", USER_SLICE, IDLE_WEIGHT)
        if self.cpuset_delegated():
            self.setp("usr", USER_SLICE, f"AllowedCPUs={s.build_cpus}")
            self.setp("usr", "flatpak-session-helper.service", f"AllowedCPUs={s.build_cpus}")
            self.cpus_fallback(USER_SLICE)
            self.cpus_fallback("flatpak-session-helper.service")
        self.demote_escapes()
        self.setp("usr", APP_SLICE, f"MemoryLow={s.game_memory_low}")
        self.protect_steam()
        if s.game_zswap_writeback == "no" and self.prop("usr", APP_SLICE, "MemoryZSwapWriteback"):
            self.setp("usr", APP_SLICE, "MemoryZSwapWriteback=no")
        boost_p_cores(self)
        apply_game_cstates(self)
        apply_game_gpu(self)
        self.log(5, on_text(s))
        return 0

    def target_refusal(self) -> str | None:
        """Why 'on' refuses for TARGET_USER: unset, or a name with no account (it would act for user@.service
        and user-.slice); None for an account. The lookup is quiet: the caller's line names the user."""
        name, conf = self.s.target_user, self.paths.conf
        if not name:
            return f"TARGET_USER not set in {conf}"
        if not self._lookup_uid(name, say=False):
            return f"TARGET_USER names no account: {name} (in {conf})"
        return None

    def foreign_refusal(self, recs: Sequence[Record] | None = None) -> str | None:
        """Why the tick leaves the user manager's side alone while the profile is on: no account has the uid
        game.state records (the user 'on' acted for), so no name reaches that uid's manager, and TARGET_USER's
        is another user's (G7). None when game.state records no uid, or an account has it. recs: game.state's
        records, as a snapshot read them (None: read the file now)."""
        uid = state_uid(records(self.state_text()) if recs is None else recs)
        target = self.s.target_user
        if uid is None or (target and self._lookup_uid(target, say=False) == uid) or self.ports.name_of(uid):
            return None
        return f"no account has uid {uid}, the user 'on' acted for"

    def off(self) -> int:
        """Exact revert: the overrides 'on' created go (then daemon-reload), the ones that existed
        before get their old value back; the MemoryLow drop-ins go also without a game.state. What it
        could not undo is kept in game.leftovers for the next 'off' and tick (retry_kept), and game.state
        is removed anyway: it means the profile is on."""
        p = self.paths
        if not self.ports.state.exists(p.state):
            with contextlib.suppress(FileNotFoundError):
                self.ports.state.remove(p.manual)
            retry_kept(self, say=True)
            return 0
        text = self.state_text()
        for number, line in unreadable_lines(text, intended=False):
            self.log(4, f"{p.state} line {number}: no record 'off' can read, skipped: {line}")
        recs = records(text)
        uid = state_uid(recs)
        side = user_side(self, uid, say=True)  # a user manager's record only in the manager of its uid
        owner = None if side == UserSide.OWN else uid
        said = None if owner is None else user_side_text(side, owner, sum(map(of_user_manager, recs)))
        if said is not None:
            self.log(*said)
        kept = read_kept(self, say=True)
        acting = (self.uid(say=True) or None) if bool(kept) else None  # the uid an entry of no user is for
        entries = merged_entries(kept or [], recs, owner=owner, acting=acting)
        failed, _dropped, _skipped = undo_pass(self, entries, kept, say=True)
        held = keep(self, failed, kept)
        for f in (p.state, p.intended, p.manual, p.drift_warned):
            with contextlib.suppress(FileNotFoundError):
                self.ports.state.remove(f)
        self.log(5, off_text(len(failed), p.leftovers, kept=held))
        return 0

    def user_from_state(self, recs: Sequence[Record], *, say: bool = True) -> bool:
        """'off' undoes the user manager's side for the user 'on' acted for, whose uid game.state names:
        also with no TARGET_USER (its line sets nothing, or is gone) or one that names another user now.
        The tick and 'state' act for that user too, quietly (say False: neither the line below nor id's
        "no such user" line, which 'off' says once: _lookup_uid), so what the tick sets in the user
        manager is what 'off' undoes. False when no account has that uid now (act_for)."""
        return self.act_for(state_uid(recs), say=say)

    def act_for(self, uid: str | None, *, say: bool = True) -> bool:
        """Act for the user with that uid in the user manager's side from now on (None: TARGET_USER as it
        is): user_from_state's rule, also for the user a kept record of game.leftovers names. False when no
        account has that uid: the user acted for stays as it was, which is not that user."""
        if uid is None:
            return True
        target = self.s.target_user
        if target and self._lookup_uid(target, say=say) == uid:
            return True
        name = self.ports.name_of(uid)
        if not name:
            return False
        if say:
            conf = self.paths.conf
            why = f"TARGET_USER in {conf} is {target}" if target else f"TARGET_USER not set in {conf}"
            self.log(5, f"{why}: undoing for {name} (uid {uid}), the user 'on' acted for")
        self.s = dataclasses.replace(self.s, target_user=name)
        return True

    def write_back(self, path: str, old: str) -> bool:
        """An undo's write of a cgroup, sysfs or EPP file's old value: also done (True) when the write failed
        because the file's folder is gone (a cgroup that ended at a logout, a CPU taken offline): nothing is left
        to undo, and a folder made again later (the next login's cgroup) must not get the old session's value."""
        return self.ports.kernel.write(path, old) or not self.ports.kernel.lexists(os.path.dirname(path))

    def undo_record(self, rec: Sequence[str], reload: Reloads, *, say: bool = True) -> bool:
        """Undo one record of game.state (as state_undo reads it); False when the undo failed: a file write
        or set-property that failed, an override file there that cannot be removed. A unit that is gone is
        no failure (unit_gone), nor is a file whose folder is gone (write_back), nor a record it cannot read
        (it is skipped). say: see _lookup_uid ('off' only; the tick's retry passes False)."""
        match state_undo(rec):
            case CgroupUndo(file=f, old=old) | SysfsUndo(file=f, old=old):
                return self.write_back(f, old)
            case EppUndo(cpu=cpu, old=old):
                return self.write_back(self.paths.epp(cpu), old)
            case PropUndo(mgr=mgr, unit=unit, prop=prop, mode=mode, old=old):
                if mode == "new":
                    f = self.ctl_file(mgr, unit, prop, say=say)
                    try:
                        self.ports.units.remove(f)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        return False  # the override is still there: no daemon-reload for it
                    with contextlib.suppress(OSError):
                        self.ports.units.remove_folder(os.path.dirname(f))
                    reload.add(mgr, self.uid(say=say) if mgr == "usr" else "", self.s.target_user)
                    return True
                rc = self.ctl(mgr, "set-property", "--runtime", unit, f"{prop}={old}", quiet=True)
                return rc == 0 or unit_gone(self, mgr, unit)
            case None:
                return True

    def drift(self, text: str, *, user_manager: bool = True) -> list[Drift]:
        """The settings in game.intended (its text) that no longer hold (settings_drift)."""
        return settings_drift(self, text, user_manager=user_manager)

    def say_unheld(self, skipped: Sequence[Drift], failed: Sequence[tuple[int, str]] = ()) -> None:
        """Say the drifts the tick does not set again ('off' could not undo them), at notice, and the ones whose
        repair failed (failed: their lines), once per boot; with none, forget the mark, so a later one is said
        again."""
        lines = [(5, unheld_text(d)) for d in skipped] + list(failed)
        if not lines:
            with contextlib.suppress(OSError):
                self.ports.state.remove(self.paths.drift_warned)
            return
        said = "\n".join(text for _prio, text in lines)

        def emit() -> None:
            for prio, text in lines:
                self.log(prio, text)

        self.say_once(self.paths.drift_warned, said, emit)

    def tick(self) -> int:
        """The watchdog (every 30 s); see the program's help."""
        p = self.paths
        if not self.problems:  # fixed: a line or a name that breaks again is said again
            self._forget_fixed_settings()
        if not self.ports.state.exists(p.state):
            self._tick_while_off()
            return 0
        if not self.ports.state.exists(p.manual) and not self.gamemode_client():
            self.log(5, "no GameMode client left: switching off")
            self.ctl("sys", "stop", "--no-block", "cpu-game.service")
            return 0
        if self.problems:
            self.warn_once("tick changes nothing")
            return 0
        owned = self.user_from_state(records(self.state_text()), say=False)  # the user 'off' undoes for
        self.pin_docker()  # containers started meanwhile
        # No account for the user acted for, or for the uid 'on' acted for: no user manager to change.
        why = self.target_refusal() or (None if owned else self.foreign_refusal())
        if why is None:
            self.protect_steam()  # the game's own scope, started after 'on'
            self.demote_escapes()  # boxes next to the game that appeared after 'on'
        else:
            self._say_refusal("profile on, its user manager's side not handled", why)
        apply_game_gpu(self)
        repair_drift(self, user_manager=owned)  # settings a daemon-reload or uresourced undid
        return 0

    def _tick_while_off(self) -> None:
        """The tick with the profile off: leftover drop-ins go, and a GameMode client switches the profile on,
        unless it was switched off by hand while that client runs (suppress) or 'on' would refuse."""
        p = self.paths
        retry_kept(self, say=False)  # leftover drop-ins, and what an 'off' could not undo, now that it is off
        if not self.gamemode_client():
            with contextlib.suppress(FileNotFoundError):
                self.ports.state.remove(p.suppress)
        elif not self.ports.state.exists(p.suppress):
            if self.problems:  # 'on' would refuse: no failed cpu-game.service every 30 s
                self.warn_once("GameMode client running, profile not switched on")
                return
            why = self.target_refusal()  # the same for TARGET_USER, with the same mark
            if why is not None:
                self._say_refusal("GameMode client running, profile not switched on", why)
            else:
                self.log(5, "GameMode client running but profile off: switching on")
                self.ctl("sys", "start", "--no-block", "cpu-game.service")

    def _say_refusal(self, what: str, why: str) -> None:
        """The watchdog's word on a TARGET_USER 'on' refuses (target_refusal's why), once per boot, in the
        mark of the settings file's problem lines (_forget_fixed_settings)."""
        said = f"{what}: {why}{_ONCE}"
        self.say_once(self.paths.settings_warned, said, lambda: self.log(4, said))

    def _forget_fixed_settings(self) -> None:
        """With no problem line: forget what the watchdog said about the settings (a line or a name that
        breaks again is then said again), unless it is about a TARGET_USER 'on' still refuses for. The
        name is looked up only when there is such a mark."""
        mark = self.paths.settings_warned
        said = self.ports.state.read_text(mark)
        if said is not None:
            for why in (self.target_refusal(), self.foreign_refusal()):
                if why is not None and said.endswith(f": {why}{_ONCE}"):
                    return
        with contextlib.suppress(OSError):
            self.ports.state.remove(mark)

    def problem_lines(self) -> list[str]:
        """The settings file's problem lines, one message each (none when it has none)."""
        return describe_problems(self.paths.conf, self.problems).split("\n") if self.problems else []

    def _log_problems(self, prio: int, what: str, suffix: str = "") -> None:
        """The problem lines in the journal, then what the profile did not do."""
        for line in self.problem_lines():
            self.log(prio, line)
        self.log(prio, f"{what}: a line of {self.paths.conf} sets nothing (fix it; 'off' still works{suffix})")

    def refuse(self, what: str) -> None:
        """The settings file's problem lines in the journal, and what the profile did not do."""
        self._log_problems(3, what)

    def warn_once(self, what: str) -> None:
        """The watchdog's word on the settings file's problem lines: logged at the first tick that meets
        them (and again when they or the word change, or after a tick found the file fixed), then quiet
        until the next boot (the mark is in /run); the watchdog's unit does not fail on them."""
        said = "\n".join([what, *self.problem_lines()])  # another word on the same lines is said too
        self.say_once(self.paths.settings_warned, said, lambda: self._log_problems(4, what, "; said once per boot"))

    def say_once(self, mark: str, said: str, emit: Callable[[], None]) -> None:
        """emit() unless the mark holds said (it was said already this boot: the mark is in /run); then
        the mark holds said, so another text is said again."""
        if self.ports.state.read_text(mark) == said:
            return
        emit()
        with contextlib.suppress(OSError):
            self.ports.state.replace(mark, said)

    # ---- state ------------------------------------------------------------------------------------
    def state(self) -> int:
        """What is applied now, and which settings drifted from what 'on' set (show_state)."""
        return show_state(self)

    def drift_report(
        self,
        state_recs: Sequence[Record],
        intended_text: str | None,
        newer: str | None,
        *,
        manual: bool,
        user_manager: bool = True,
    ) -> list[str]:
        """The drift part of 'state' from its snapshot (game.state's records, game.intended's text or None, why
        it cannot be compared, the by-hand mark; user_manager: see drift, and when False a line says why the user
        manager's settings were not compared): each setting that no longer holds, or that none drifted."""
        if intended_text is None:
            return drift_lines(None, manual=manual)
        drifts = self.drift(intended_text, user_manager=user_manager) if newer is None else None
        not_held = [] if drifts is None else unheld(drifts, state_recs)
        unchecked = None if user_manager else self.foreign_refusal(state_recs)
        return drift_lines(drifts, manual=manual, newer=newer, not_held=not_held, unchecked=unchecked)

    def intended(self) -> tuple[str, str | None]:
        """game.intended's text, and why it cannot be compared (None: it can)."""
        text = self.ports.state.read_text(self.paths.intended) or ""
        return text, newer_intended(self.paths.intended, text)

    def locked(self, action: str) -> int:
        """on, off or tick under the run lock (60 s at most; then exit 1)."""
        if not self.ports.lock.take():
            self.log(3, "lock busy")
            return 1
        try:
            return {"on": self.on, "off": self.off, "tick": self.tick}[action]()
        finally:
            self.ports.lock.release()

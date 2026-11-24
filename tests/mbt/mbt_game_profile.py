# SPDX-License-Identifier: MPL-2.0
"""A state machine over the game profile's on, tick and off, with faults inside them (Hypothesis).

It runs the real GameProfile (cpu/game/adapters/profile.py) through GameProfile.locked, as the units
run it, on the unit tests' scratch (cpu/tests/test_game_profile.py game_scratch): a fake cgroup tree,
/proc and sysfs, and their fake systemctl for both managers (FakeSystemd), here as ReloadingSystemd:
each property has a base (the unit file's value); a daemon-reload sets every shown value from the
zz-cst-game.conf drop-in for the system manager's MemoryLow when there is one (it wins over a
runtime override: zz- sorts last; tier 1 test_07 proves that order on the real systemd), else from
the runtime override file when there is one, else the base. Settings: TARGET_USER with an account,
P-cores 0-1 with EPP files and two cpuidle states each (one at or above the 50 us limit),
builds.slice active or not, cpuset delegated to the user manager or not, and maybe a runtime
override of machine.slice's CPUWeight from before the game ('existed').

Between the profile's calls the machine changes the world: the game starts or ends (a GameMode
client in /proc), the by-hand mark comes and goes, Docker containers and Steam scopes start, boxes
of the user manager appear next to the game (a slice, or a systemd-run scope in app.slice; with the
game inside or not), a folder whose name is no unit name appears there (a blank, a byte that is not
UTF-8: made by hand), a setting the profile made drifts (uresourced, someone else's daemon-reload),
and an outside daemon-reload runs. A tick that asks systemd to stop cpu-game.service is followed by
its ExecStop ('off'), one that asks to start it by 'on'.

Each on, tick and off arms at most one fault (tests/fault_injection.py FaultPlan), at the k-th of the
call's points: each set-property (FaultyRunner: exit 1), each file the profile writes (game.state's
and game.intended's appends, the sysfs, EPP and cpuidle writes: a write that fails with ENOSPC and
no effect, or is done whole), each write_atomic (the drop-ins), and, for a kill only, its
os.unlink, os.truncate, os.rmdir and os.makedirs. FAIL: that call fails and the run goes on; KILL:
that call takes effect and the process dies (Killed ends the call). A point past the call's end
is no fault.

Checked, relative to the baseline (the machine at the start: every runtime override file by the
value it sets, every value systemd shows, every sysfs file; the drop-ins and the state files):
1. Record cover, after every step: every override file, shown value and sysfs file that differs
   from the baseline has a record in game.state or game.leftovers that 'off' undoes (state_undo):
   so with neither, nothing differs. The one exception is a MemoryLow of the memory chain that its
   zz-cst-game.conf drop-in sets after a daemon-reload: 'off' and the tick remove every such
   drop-in, with a record or without (a setp whose record could not be written is skipped, the
   drop-ins are written for the whole chain).
2. Off restores: after an 'off' with no fault, whatever ran between (faults, kills, drift, outside
   reloads, more boxes): every override file and shown value as at the baseline (an 'existed'
   override comes back as its shown value: 16G as 17179869184), every sysfs file byte for byte, no
   drop-in, no game.state, game.intended, game.leftovers, manual or drift-warned. check_off runs
   such an off at points Hypothesis chooses on a copy of the machine, and puts the machine back.
   After a kill inside 'off', the next 'off' restores it. After a set-property or a sysfs write that
   fails inside 'off' (O4): game.state is gone, every other undo is done, that one setting keeps the
   value it had before that 'off', and game.leftovers holds exactly its record; the baseline does not
   move, and after the next tick with no fault while the profile is off (or the next whole 'off')
   every setting is at the baseline and game.leftovers is gone.
3. On twice equals on once: 'on' with game.state there changes nothing and sets nothing.
4. A tick leaves nothing half applied: after a tick with no fault while the profile stays on,
   every running Docker scope has AllowedCPUs = BUILD_CPUS and its record, every Steam scope its
   MemoryLow, every box without the game all of its settings (CPUWeight idle, and AllowedCPUs with
   cpuset delegated), and drift() lists no drift game.state holds a record of. With no exception:
   a box whose record a faulted step wrote without its setting (its set-property failed, or the
   process died between the two: O2), and a box whose setting an 'off' that was killed undid while
   game.state stayed (O6), are set again by the next whole tick, with no new record.
5. A folder whose name is no unit name (O5) is left alone, after every step: no record of
   game.state or game.leftovers names it and no systemctl call of the last step does.

Not modelled: the lock between processes (one call at a time here), cpu-power, the GPU, a settings
file with problem lines and a TARGET_USER without an account (the unit tests cover the refusals),
a record cut short in the middle of its write (tests/mbt/mbt_game_state.py Q5), a full /run from
some point on (FullRunFolderTest), a failing daemon-reload, and real systemd's merge of drop-ins
(test_07, test_08).
"""

from collections.abc import Callable, Sequence
import contextlib
from enum import StrEnum
import io
import os
import shutil
import tempfile
from types import TracebackType
from typing import Final, Self, override
import unittest
from unittest import mock

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from cpu.game.adapters import host as game_host, profile as game_profile
from cpu.game.adapters.profile import GameProfile
from cpu.game.domain import (
    CgroupUndo,
    EppUndo,
    GameSettings,
    PropUndo,
    SysfsUndo,
    has_prop_record,
    has_sys_record,
    intended_records,
    manager_of,
    records,
    state_undo,
    unheld,
)
from cpu.game.domain.leftovers import leftover_entries
from cpu.tests.test_game_profile import UID, USER, FakeSystemd, add_gamemode_pid, game_scratch, override_path
from cst_core.ports import CommandResult
from tests.fault_injection import FaultMode, FaultPlan, FaultyOs, FaultyRunner, Killed, stub_fsync
from tests.mbt import profiles

profiles.load()

SETTINGS: Final = GameSettings(target_user=USER, game_zswap_writeback="yes", p_cores="0-1")
BUILD: Final = SETTINGS.build_cpus
MEMORY_LOW_BYTES: Final = "17179869184"  # 16G as systemctl show gives it
# The last point a fault is armed at: 'on' makes about 40 calls that are points, a tick or an 'off' a few.
ON_POINTS: Final = 40
FEW_POINTS: Final = 8
MAX_SCOPES: Final = 3
# Folders whose name is no unit name (O5): a blank, and a byte that is not UTF-8 (as glob gives it).
ODD_NAMES: Final = ("a b.slice", os.fsdecode(b"x\xff.slice"), os.fsdecode(b"app.slice/run-\xff.scope"))
STEPS: Final = 30
# The profile module's os functions that are points of a kill (never a failure: none of them fails with
# ENOSPC, and the profile suppresses only the errors those calls give).
KILL_ONLY_OS: Final = ("unlink", "truncate", "rmdir", "makedirs")

type Key = tuple[str, str, str]  # (manager, unit, property)


class ProfileFault(StrEnum):
    """What goes wrong in one call of the profile."""

    NONE = "none"
    FAIL = "fail"
    KILL = "kill"


def norm(value: str) -> str:
    """A value as systemctl show gives it (16G in bytes)."""
    return MEMORY_LOW_BYTES if value == "16G" else value


def override_value(path: str) -> str:
    """The value a runtime override file (or a drop-in) sets: its last KEY=VALUE line's VALUE."""
    with open(path, encoding="utf-8") as fh:
        line = [x for x in fh.read().splitlines() if "=" in x][-1]
    return norm(line.split("=", 1)[1].strip())


class ReloadingSystemd(FakeSystemd):
    """FakeSystemd whose daemon-reload sets each shown value from the files, as systemd does."""

    def __init__(self, root: str, unit_runtime: str) -> None:
        super().__init__(root)
        self.base: dict[Key, str] = {}
        self.unit_runtime = unit_runtime

    def set_base(self, mgr: str, unit: str, **props: str) -> None:
        for prop, value in props.items():
            self.base[mgr, unit, prop] = value
        self.set_now(mgr, unit, **props)

    @override
    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        args = list(argv[1:])
        mgr = "sys"
        if args[:2] == ["--user", "-M"]:
            mgr, args = "usr", args[3:]
        if args[:1] == ["daemon-reload"]:
            self.actions.append(" ".join(argv))
            self.reload(mgr)
            return CommandResult(0, "", "")
        return super().run(argv, **kw)

    def reload(self, mgr: str) -> None:
        keys = {k for k in self.base if k[0] == mgr}
        folder = os.path.join(self.store, mgr)
        if os.path.isdir(folder):
            for unit in os.listdir(folder):
                keys.update((mgr, unit, prop) for prop in os.listdir(os.path.join(folder, unit)))
        for m, unit, prop in sorted(keys):
            value = self.base.get((m, unit, prop))
            f = override_path(self.root, m, unit, prop)
            if os.path.exists(f):
                value = override_value(f)
            dropin = os.path.join(self.unit_runtime, f"{unit}.d", "zz-cst-game.conf")
            if m == "sys" and prop == "MemoryLow" and os.path.exists(dropin):
                value = override_value(dropin)
            if value is None:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(os.path.join(self.store, m, unit, prop))
            else:
                self.set_now(m, unit, **{prop: norm(value)})


class BufferedWrite:
    """A file the profile opens to write: what it writes is kept, and written whole at the end of its
    with block, as one point of the plan (a failure there raises ENOSPC, with no effect)."""

    def __init__(self, plan: FaultPlan, path: str, mode: str, encoding: str | None, errors: str | None) -> None:
        self._plan, self._path, self._mode = plan, path, mode
        self._encoding, self._errors = encoding, errors
        self._parts: list[str] = []

    def __enter__(self) -> Self:
        return self

    def write(self, text: str) -> int:
        self._parts.append(text)
        return len(text)

    def _flush(self) -> None:
        with open(self._path, self._mode, encoding=self._encoding, errors=self._errors) as fh:
            fh.write("".join(self._parts))

    def __exit__(self, kind: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        if kind is None:
            self._plan.step(f"write {self._path}", self._flush)


def faulty_open(plan: FaultPlan) -> Callable[..., object]:
    """open() as the profile's modules see it: a write is a BufferedWrite, a read the real open."""

    def opener(path: str, mode: str = "r", *, encoding: str | None = None, errors: str | None = None) -> object:
        if mode in {"w", "a"}:
            return BufferedWrite(plan, path, mode, encoding, errors)
        return open(path, mode, encoding=encoding, errors=errors)

    return opener


class FaultyAtomic:
    """write_atomic as the profile module sees it: each call one point of the plan."""

    def __init__(self, plan: FaultPlan, real: Callable[..., object]) -> None:
        self.plan = plan
        self.real = real

    def __call__(self, path: str, *args: object, **kw: object) -> object:
        return self.plan.step(f"write {path}", lambda: self.real(path, *args, **kw))


def _changes(argv: Sequence[str]) -> bool:
    """The command is a point of the plan: set-property (every other command only reads, or is a
    daemon-reload, start or stop, which do not fail here)."""
    return "set-property" in argv


def failed_key(name: str) -> tuple[str, str] | None:
    """What a point that failed changed nothing of: ("prop", "MGR UNIT PROP") for a set-property of one
    setting, ("file", PATH) for a write; None for another point."""
    words = name.split()
    if words[:1] == ["write"]:
        return "file", name.removeprefix("write ")
    if "set-property" in words:
        mgr = "usr" if "--user" in words else "sys"
        unit, setting = words[words.index("--runtime") + 1], words[-1]
        return "prop", f"{mgr} {unit} {setting.split('=', 1)[0]}"
    return None


class View:
    """What the profile changes on the machine, as one comparable snapshot."""

    def __init__(self, tmp: str, systemd: ReloadingSystemd) -> None:
        self.overrides: dict[Key, str] = {}
        for mgr, root in (("sys", os.path.join(tmp, "rs")), ("usr", os.path.join(tmp, "ru", UID, "systemd"))):
            for d, _dirs, files in os.walk(root):
                for name in files:
                    unit = os.path.basename(d).removesuffix(".d")
                    prop = name.removeprefix("50-").removesuffix(".conf")
                    self.overrides[mgr, unit, prop] = override_value(os.path.join(d, name))
        self.live: dict[Key, str] = {}
        for mgr in ("sys", "usr"):
            folder = os.path.join(systemd.store, mgr)
            for unit in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
                for prop in os.listdir(os.path.join(folder, unit)):
                    self.live[mgr, unit, prop] = systemd.now(mgr, unit, prop)
        self.sysfs: dict[str, bytes] = {}
        for d, _dirs, files in os.walk(os.path.join(tmp, "sys-cpu")):
            for name in files:
                with open(os.path.join(d, name), "rb") as fh:
                    self.sysfs[os.path.join(d, name)] = fh.read()
        self.dropins = sorted(
            os.path.join(d, n) for d, _dirs, files in os.walk(os.path.join(tmp, "unit-runtime")) for n in files
        )

    def differences(self, base: View) -> tuple[set[Key], set[str]]:
        """The settings (overrides and shown values) and the sysfs files that differ from base."""
        keys = {
            k for k in self.overrides.keys() | base.overrides.keys() if self.overrides.get(k) != base.overrides.get(k)
        }
        keys |= {k for k in self.live.keys() | base.live.keys() if self.live.get(k) != base.live.get(k)}
        files = {f for f in self.sysfs.keys() | base.sysfs.keys() if self.sysfs.get(f) != base.sysfs.get(f)}
        return keys, files


# Steps of one example: enough for on, a few ticks with boxes and drift, and off (the budget: under 6 s at ci).
@settings(stateful_step_count=STEPS)
class GameProfileMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-mbt-gp-"))
        self.scratch = game_scratch(self.tmp)
        self.paths = self.scratch.paths
        self.systemd = ReloadingSystemd(self.tmp, self.paths.unit_runtime)
        self.plan = FaultPlan()
        self.base = View(self.tmp, self.systemd)
        self.cpuset = False
        self.scopes = 0
        self.boxes: dict[str, str] = {}  # unit -> its cgroup folder
        self.odd: list[str] = []  # the names of the folders odd_box made
        self.log = io.StringIO()

    @override
    def teardown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers -----------------------------------------------------------------------------
    def view(self) -> View:
        return View(self.tmp, self.systemd)

    def state_text(self) -> str:
        if not os.path.exists(self.paths.state):
            return ""
        with open(self.paths.state, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def profile(self, runner: FaultyRunner | ReloadingSystemd) -> GameProfile:
        return GameProfile(
            self.paths,
            SETTINGS,
            runner,
            uid_of=lambda u: UID if u == USER else "",
            name_of=lambda uid: USER if uid == UID else "",
            out=self.log,
        )

    def call(self, action: str, fault: ProfileFault, at: int) -> tuple[bool, str | None]:
        """One on, off or tick through locked, with the fault armed at the point at; (it was killed,
        the name of the point that failed or None)."""
        mode = {ProfileFault.FAIL: FaultMode.FAIL, ProfileFault.KILL: FaultMode.KILL}.get(fault)
        self.plan.arm(*([] if mode is None else [(mode, at)]))
        self.systemd.actions = []
        runner = FaultyRunner(self.plan, self.systemd, reads=lambda argv: not _changes(argv))
        killed, rc = False, 0
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            for module in (game_profile, game_host):
                stack.enter_context(mock.patch.object(module, "open", faulty_open(self.plan), create=True))
            stack.enter_context(
                mock.patch.object(game_profile, "os", FaultyOs(self.plan, KILL_ONLY_OS, kill_only=True))
            )
            atomic = FaultyAtomic(self.plan, game_profile.write_atomic)
            stack.enter_context(mock.patch.object(game_profile, "write_atomic", atomic))
            try:
                rc = self.profile(runner).locked(action)
            except Killed:
                killed = True
        failed = None
        if mode == FaultMode.FAIL and len(self.plan.calls) >= at and not self.plan.calls[at - 1].startswith("os."):
            failed = self.plan.calls[at - 1]
        # Exit 1 only for an 'on' that could not create game.state (it changed nothing then).
        if not killed and rc != 0:
            assert (action, failed) == ("on", f"write {self.paths.state}"), (
                f"{action} exit {rc}: {self.log.getvalue()[-2000:]}"
            )
        return killed, failed

    def game_in(self, folder: str) -> bool:
        procs = os.path.join(folder, "cgroup.procs")
        return os.path.exists(procs) and os.path.exists(os.path.join(self.paths.proc, "1", "maps"))

    def box_settings(self) -> list[tuple[str, str, str, str]]:
        """(manager, unit, property, value) a tick sets on each running box."""
        out = [("sys", s, "AllowedCPUs", BUILD) for s in self.systemd.units["sys"]]
        out += [("usr", s, "MemoryLow", MEMORY_LOW_BYTES) for s in self.systemd.units["usr"]]
        for unit, folder in self.boxes.items():
            if self.game_in(folder):
                continue
            out.append(("usr", unit, "CPUWeight", "idle"))
            if self.cpuset:
                out.append(("usr", unit, "AllowedCPUs", BUILD))
        return out

    def assert_restored(
        self,
        what: str,
        *,
        was_on: bool,
        skip: frozenset[Key] = frozenset(),
        skip_files: frozenset[str] = frozenset(),
    ) -> None:
        now = self.view()
        keys, files = now.differences(self.base)
        assert keys <= skip, f"{what}: settings not restored: {sorted(keys - skip)}"
        assert files <= skip_files, f"{what}: sysfs files not restored: {sorted(files - skip_files)}"
        assert now.dropins == [], f"{what}: drop-ins left: {now.dropins}"
        # With no game.state, off removes the by-hand mark and the drop-ins only (and undoes the leftovers).
        p = self.paths
        files = (p.state, p.intended, p.manual, p.drift_warned) if was_on else (p.state, p.manual)
        if not (skip or skip_files):
            files = (*files, p.leftovers)
        left = [f for f in files if os.path.exists(f)]
        assert left == [], f"{what}: files left: {left}"

    def leftover_keys(self) -> set[tuple[str, str]]:
        """What game.leftovers holds, as failed_key names a point: ("prop", "MGR UNIT PROP"), ("file", PATH)."""
        out: set[tuple[str, str]] = set()
        for _uid, rec in leftover_entries(self.read(self.paths.leftovers)):
            match state_undo(rec):
                case PropUndo(mgr=mgr, unit=unit, prop=prop):
                    out.add(("prop", f"{mgr} {unit} {prop}"))
                case CgroupUndo(file=f) | SysfsUndo(file=f):
                    out.add(("file", f))
                case EppUndo(cpu=cpu):
                    out.add(("file", self.paths.epp(cpu)))
                case None:
                    pass
        return out

    def read(self, path: str) -> str:
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def after_off(
        self, what: str, failed: str | None, *, killed: bool, was_on: bool, before: View | None = None
    ) -> None:
        """The checks after an 'off' (point 2 of the docstring; was_on: game.state was there; before: the
        machine before it, for a fault)."""
        if killed:
            return
        if failed is None:
            self.assert_restored(what, was_on=was_on)
            return
        key = failed_key(failed)
        assert key is not None, f"{what}: an off failed at a point it has not: {failed}"
        kind, which = key
        skip: frozenset[Key] = frozenset()
        if kind == "prop":
            mgr, unit, prop = which.split()
            skip = frozenset({(mgr, unit, prop)})
        skip_files = frozenset({which}) if kind == "file" else frozenset()
        # O4: game.state is gone, every other undo done; the failed one is as it was before this off, and
        # game.leftovers holds exactly its record, for the next off and tick.
        self.assert_restored(f"{what} (failed at {failed})", skip=skip, skip_files=skip_files, was_on=was_on)
        assert before is not None, f"{what}: a fault without the machine before it"
        now = self.view()
        for k in skip:
            was, after = (before.overrides.get(k), before.live.get(k)), (now.overrides.get(k), now.live.get(k))
            assert was == after, f"{failed}: {k} went from {was} to {after}"
        for f in skip_files:
            assert before.sysfs.get(f) == now.sysfs.get(f), f"{failed}: {f} changed"
        kept = self.leftover_keys()
        assert kept == {key}, f"{what} (failed at {failed}): game.leftovers holds {kept}, not {key}"

    def assert_off_restored(self, what: str) -> None:
        """With the profile off after a whole tick: every setting at the baseline, no drop-in, no game.leftovers."""
        now = self.view()
        keys, files = now.differences(self.base)
        assert (keys, files) == (set(), set()), f"{what}: not restored: {sorted(keys)} {sorted(files)}"
        assert now.dropins == [], f"{what}: drop-ins left: {now.dropins}"
        assert not os.path.exists(self.paths.leftovers), (
            f"{what}: game.leftovers left: {self.read(self.paths.leftovers)}"
        )

    def run_follow_up(self) -> None:
        """A tick's start or stop of cpu-game.service: its ExecStart ('on') or ExecStop ('off'), no fault."""
        asked = self.systemd.actions
        if "systemctl stop --no-block cpu-game.service" in asked:
            was_on = os.path.exists(self.paths.state)
            killed, failed = self.call("off", ProfileFault.NONE, 1)
            self.after_off("the tick's off", failed, killed=killed, was_on=was_on)
        elif "systemctl start --no-block cpu-game.service" in asked:
            self.call("on", ProfileFault.NONE, 1)

    # ---- rules -------------------------------------------------------------------------------
    @initialize(prior=st.booleans(), cpuset=st.booleans(), builds=st.booleans())
    def start(self, *, prior: bool, cpuset: bool, builds: bool) -> None:
        """The baseline: the unit tests' values, P-cores 0-1 with EPP and cpuidle files, and the draws."""
        sd, p = self.systemd, self.paths
        sd.set_base("sys", "machine.slice", CPUWeight="50", IOWeight="50")
        for unit in ("user.slice", f"user-{UID}.slice", f"user@{UID}.service"):
            sd.set_base("sys", unit, MemoryLow="0")
        sd.set_base("usr", "user.slice", CPUWeight="30")
        if prior:  # a runtime override from before the game: 'on' records it as 'existed'
            f = override_path(self.tmp, "sys", "machine.slice", "CPUWeight")
            os.makedirs(os.path.dirname(f))
            with open(f, "w", encoding="utf-8") as fh:
                fh.write("[Slice]\nCPUWeight=70\n")
            sd.set_now("sys", "machine.slice", CPUWeight="70")
        if builds:
            sd.active.add("builds.slice")
        for cpu in ("0", "1"):
            os.makedirs(os.path.dirname(p.epp(cpu)))
            with open(p.epp(cpu), "w", encoding="utf-8") as fh:
                fh.write("balance_performance\n")
            for state, latency in (("state0", "0"), ("state1", "100")):
                d = os.path.join(p.sys_cpu, f"cpu{cpu}", "cpuidle", state)
                os.makedirs(d)
                for name, value in (("latency", latency), ("disable", "0")):
                    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                        fh.write(value + "\n")
        with open(os.path.join(self.scratch.ucg, "cgroup.subtree_control"), "w", encoding="utf-8") as fh:
            fh.write("cpuset cpu memory\n" if cpuset else "cpu memory\n")
        self.cpuset = cpuset
        self.base = self.view()

    @rule(running=st.booleans())
    def game(self, *, running: bool) -> None:
        """The game (a GameMode client, pid 1) starts or ends."""
        if running:
            add_gamemode_pid(self.paths.proc, "1")
        else:
            shutil.rmtree(os.path.join(self.paths.proc, "1"), ignore_errors=True)

    @rule(on=st.booleans())
    def manual(self, *, on: bool) -> None:
        """The by-hand mark ('cst cpu game on' makes it) comes or goes."""
        if on:
            with open(self.paths.manual, "w", encoding="utf-8"):
                pass
        else:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.paths.manual)

    @precondition(lambda self: self.scopes < MAX_SCOPES)
    @rule(steam=st.booleans())
    def scope(self, *, steam: bool) -> None:
        """A Docker container (outside builds.slice) or a Steam scope starts."""
        self.scopes += 1
        if steam:
            self.systemd.units["usr"].append(f"app-flatpak-com.valvesoftware.Steam-{self.scopes}.scope")
        else:
            self.systemd.units["sys"].append(f"docker-{self.scopes}.scope")

    @precondition(lambda self: len(self.boxes) < MAX_SCOPES)
    @rule(run_scope=st.booleans(), with_game=st.booleans())
    def box(self, *, run_scope: bool, with_game: bool) -> None:
        """A box of the user manager next to the game: a slice, or a systemd-run job in app.slice."""
        n = len(self.boxes) + 1
        unit = f"run-u{n}.scope" if run_scope else f"box{n}.slice"
        folder = (
            os.path.join(self.scratch.ucg, "app.slice", unit) if run_scope else os.path.join(self.scratch.ucg, unit)
        )
        os.makedirs(folder)
        if with_game:
            with open(os.path.join(folder, "cgroup.procs"), "w", encoding="utf-8") as fh:
                fh.write("1\n")
        self.boxes[unit] = folder

    @precondition(lambda self: not self.odd)
    @rule(name=st.sampled_from(ODD_NAMES))
    def odd_box(self, name: str) -> None:
        """A folder whose name is no unit name, made by hand in the user manager's tree."""
        os.makedirs(os.path.join(self.scratch.ucg, *name.split("/")))
        self.odd.append(os.path.basename(name))

    @precondition(lambda self: os.path.exists(self.paths.state))
    @rule(data=st.data())
    def drift(self, data: st.DataObject) -> None:
        """A setting game.intended holds gets another value, its files untouched (uresourced, say)."""
        text = ""
        if os.path.exists(self.paths.intended):
            with open(self.paths.intended, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        props = [r for r in intended_records(text) if r[0] == "prop"]
        if not props:
            return
        r = data.draw(st.sampled_from(props), label="drifts")
        self.systemd.set_now(r[1], r[2], **{r[3]: "1"})

    @rule(mgr=st.sampled_from(("sys", "usr")))
    def reload(self, mgr: str) -> None:
        """Someone else's daemon-reload."""
        self.systemd.reload(mgr)

    @rule(fault=st.sampled_from(ProfileFault), at=st.integers(1, ON_POINTS))
    def on(self, fault: ProfileFault, at: int) -> None:
        was_on = os.path.exists(self.paths.state)
        before = self.view()
        self.call("on", fault, at)
        if was_on:
            # On twice equals on once.
            assert not [a for a in self.systemd.actions if "set-property" in a], "on with game.state set something"
            keys, files = self.view().differences(before)
            assert (keys, files) == (set(), set()), f"on with game.state changed {keys} {files}"

    @rule(fault=st.sampled_from(ProfileFault), at=st.integers(1, FEW_POINTS))
    def tick(self, fault: ProfileFault, at: int) -> None:
        was_off = not os.path.exists(self.paths.state)
        killed, failed = self.call("tick", fault, at)
        if killed or failed is not None:
            return
        if was_off:
            self.assert_off_restored("a tick with the profile off")
        stays_on = os.path.exists(self.paths.state) and not [a for a in self.systemd.actions if "cpu-game.service" in a]
        if stays_on:
            self.assert_tick_whole()
        self.run_follow_up()

    @rule(fault=st.sampled_from(ProfileFault), at=st.integers(1, FEW_POINTS))
    def off(self, fault: ProfileFault, at: int) -> None:
        was_on, before = os.path.exists(self.paths.state), self.view()
        killed, failed = self.call("off", fault, at)
        self.after_off("off", failed, killed=killed, was_on=was_on, before=before)

    @precondition(lambda self: os.path.exists(self.paths.state))
    @rule()
    def check_off(self) -> None:
        """An 'off' with no fault restores the baseline, run on a copy of the machine, which is then put back."""
        side = self.tmp + ".side"
        shutil.copytree(self.tmp, side, symlinks=True)
        try:
            killed, failed = self.call("off", ProfileFault.NONE, 1)
            self.after_off("check_off", failed, killed=killed, was_on=True)
        finally:
            shutil.rmtree(self.tmp)
            os.rename(side, self.tmp)

    # ---- checks ------------------------------------------------------------------------------
    def assert_tick_whole(self) -> None:
        """Point 4 of the docstring."""
        text = self.state_text()
        for mgr, unit, prop, want in self.box_settings():
            manager = manager_of(mgr)
            assert manager is not None
            assert has_prop_record(text, manager, unit, prop), f"tick: {unit} {prop} has no record: {text}"
            now = self.systemd.now(mgr, unit, prop)
            assert now == want, f"tick: {unit} {prop} is {now}, not {want}"
        intended = ""  # none when 'on' died before it wrote it
        if os.path.exists(self.paths.intended):
            with open(self.paths.intended, encoding="utf-8", errors="replace") as fh:
                intended = fh.read()
        drifts = self.profile(self.systemd).drift(intended)
        assert unheld(drifts, records(text)) == drifts, f"tick left a drift game.state holds: {drifts}"

    @invariant()
    def no_odd_box_is_named(self) -> None:
        """Point 5 of the docstring."""
        text = self.state_text() + self.read(self.paths.leftovers)
        for name in self.odd:
            shown = name.encode("utf-8", "surrogateescape").decode("utf-8", "replace")  # as game.state reads back
            assert shown not in text, f"a record names {shown!r}: {text}"
            named = [a for a in self.systemd.actions if name in a or shown in a]
            assert named == [], f"a systemctl call names {shown!r}: {named}"

    @invariant()
    def every_change_has_its_record(self) -> None:
        now = self.view()
        keys, files = now.differences(self.base)
        text = self.state_text() + self.read(self.paths.leftovers)  # off keeps what it could not undo there
        for mgr, unit, prop in sorted(keys):
            dropin = os.path.join(self.paths.unit_runtime, f"{unit}.d", "zz-cst-game.conf")
            same_file = now.overrides.get((mgr, unit, prop)) == self.base.overrides.get((mgr, unit, prop))
            if (mgr, prop) == ("sys", "MemoryLow") and same_file and dropin in now.dropins:
                continue  # the drop-in's value: 'off' and the tick remove every such drop-in, record or not
            manager = manager_of(mgr)
            assert manager is not None
            assert has_prop_record(text, manager, unit, prop), f"{mgr} {unit} {prop} changed with no record"
        for f in sorted(files):
            cpu = os.path.basename(os.path.dirname(os.path.dirname(f))).removeprefix("cpu")
            epp = f == self.paths.epp(cpu) and f"epp {cpu} " in text
            assert epp or has_sys_record(text, f), f"{f} changed with no record"


class GameProfileMachineTest(profiles.MbtTestCase):
    # Not `GameProfileMachine.TestCase`: Hypothesis makes that class in hypothesis.stateful, where
    # run_unit's workers cannot load it by name, and ty takes no subclass of it (unsupported-base).
    @override
    def setUp(self) -> None:
        stub_fsync(self)

    def test_any_sequence_of_on_tick_and_off_keeps_the_invariants(self) -> None:
        profiles.run_machine(GameProfileMachine)

    def replay(self, *steps: Callable[[GameProfileMachine], object]) -> None:
        """A sequence of the machine's steps, in order, with its invariant after each: one that
        Hypothesis found (or that its draws today miss), pinned whatever the draws become."""
        machine = GameProfileMachine()
        self.addCleanup(machine.teardown)
        for step in steps:
            step(machine)
            machine.every_change_has_its_record()

    def test_a_box_whose_set_failed_in_a_tick_is_set_by_the_next_tick(self) -> None:
        # O2: the Steam scope's set-property fails in a tick, after its record was written; the next
        # whole tick sets it (point 4), with no new record. The old profile skipped it (it had a record)
        # until the session ended.
        self.replay(
            lambda m: m.start(prior=False, cpuset=False, builds=False),
            lambda m: m.on(ProfileFault.NONE, 1),
            lambda m: m.scope(steam=True),
            lambda m: m.tick(ProfileFault.FAIL, 3),  # the Steam scope's set-property
            lambda m: m.tick(ProfileFault.NONE, 1),
            lambda m: m.off(ProfileFault.NONE, 1),
        )

    def test_a_steam_scope_a_killed_off_undid_is_set_again(self) -> None:
        # Found by the simplify stage (Hypothesis 6.170.17); the ci profile's draws miss it today.
        # The tick sets the Steam scope's MemoryLow; an 'off' killed at its first point (the unlink of
        # that override) keeps game.state; after a user daemon-reload the scope has no MemoryLow, and
        # the next tick sets it again (O6; the old profile left it undone).
        self.replay(
            lambda m: m.start(prior=False, cpuset=False, builds=False),
            lambda m: m.on(ProfileFault.NONE, 1),
            lambda m: m.scope(steam=True),
            lambda m: m.off(ProfileFault.KILL, 5),  # at the last drop-in's unlink: no undo yet
            lambda m: m.tick(ProfileFault.NONE, 1),
            lambda m: m.off(ProfileFault.KILL, 1),
            lambda m: m.reload("usr"),
            lambda m: m.tick(ProfileFault.NONE, 1),
        )

    def test_a_setting_an_off_could_not_undo_is_undone_by_the_next_tick(self) -> None:
        # O4: the set-property that gives machine.slice its CPUWeight back fails inside 'off'; game.state
        # is gone, and game.leftovers keeps that record, which the next tick while off undoes. The old
        # profile dropped it with game.state: the setting stayed the game's until a reboot.
        self.replay(
            lambda m: m.start(prior=True, cpuset=False, builds=False),
            lambda m: m.on(ProfileFault.NONE, 1),
            lambda m: m.off(ProfileFault.FAIL, 17),  # systemctl set-property --runtime machine.slice CPUWeight
            lambda m: m.tick(ProfileFault.NONE, 1),
        )

    def test_a_setting_an_off_could_not_undo_is_undone_by_the_next_off(self) -> None:
        # O4: the same failed undo, then a whole 'off' with no game.state undoes the kept record.
        self.replay(
            lambda m: m.start(prior=True, cpuset=False, builds=False),
            lambda m: m.on(ProfileFault.NONE, 1),
            lambda m: m.off(ProfileFault.FAIL, 17),
            lambda m: m.off(ProfileFault.NONE, 1),
        )


if __name__ == "__main__":
    unittest.main()

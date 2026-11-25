# SPDX-License-Identifier: MPL-2.0
"""The game profile's systemd model and scratch machine, shared by its unit tests (cpu/tests/test_game_profile.py),
its machines (tests/mbt/mbt_game_profile.py, mbt_game_state.py) and the contract of its adapters
(cpu/tests/test_game_ports.py): a fake systemctl for both managers (FakeSystemd; ReloadingSystemd, whose
daemon-reload sets each shown value from the files, as systemd does) and a fake cgroup tree, /proc and sysfs in a
temporary folder (game_scratch).

The Faulty* adapters are the real ones (cpu.game.adapters) with each mutating call one point of a FaultPlan
(tests/fault_injection.py), named as the machine names them: "write PATH" for a file written (a failure raises
ENOSPC with no effect; for a cgroup or sysfs write it answers False, as the real write does on an OSError), and
"os.unlink", "os.truncate", "os.rmdir" and "os.makedirs" for the calls that are points of a kill only (an armed
failure runs the call: the profile handles only the errors they give by themselves). A test injects them through
GamePorts; nothing is patched."""

from collections.abc import Callable, Sequence
import contextlib
import dataclasses
import fnmatch
import functools
import os
from typing import Final, override

from cpu.game.adapters.leftovers import GameLeftoversFile
from cpu.game.adapters.power import GameSysfsFiles
from cpu.game.adapters.profile import GameKernelFiles, GameStateFiles, GameUnitFiles
from cpu.game.domain import GamePaths, GameSettings
from cst_core.ports import CommandResult
from tests.fault_injection import FaultPlan

USER = "testuser"
UID = "999999"
SYS = "systemctl"
USR = f"systemctl --user -M {USER}@"


def override_path(root: str, mgr: str, unit: str, prop: str) -> str:
    """Where systemd keeps `set-property --runtime` (the fake's own copy of the rule)."""
    base = os.path.join(root, "rs") if mgr == "sys" else os.path.join(root, "ru", UID, "systemd")
    transient = fnmatch.fnmatchcase(unit, "*.scope") or fnmatch.fnmatchcase(unit, "run-*.service")
    folder = "transient" if transient else ("system.control" if mgr == "sys" else "user.control")
    return os.path.join(base, folder, f"{unit}.d", f"50-{prop}.conf")


class FakeSystemd:
    """A fake systemctl for both managers: STORE/<mgr>/<unit>/<prop> holds what `systemctl show`
    reports (16G reads back in bytes, as in systemd); set-property also writes the runtime override
    file, as systemd does. Every call is logged as its command line."""

    def __init__(self, root: str) -> None:
        self.root = root
        self.store = os.path.join(root, "store")
        self.actions: list[str] = []
        self.fail_set: set[tuple[str, str]] = set()
        self.not_loaded: set[tuple[str, str]] = set()
        self.units: dict[str, list[str]] = {"sys": [], "usr": []}
        self.active: set[str] = set()
        self.fail_reload: set[str] = set()  # daemon-reload fails: "sys", or the name of a user's manager
        self.fail_show: set[tuple[str, str, str]] = set()  # show -P PROP UNIT of that manager fails

    def run(self, argv: Sequence[str], **_kw: object) -> CommandResult:
        self.actions.append(" ".join(argv))
        args = list(argv[1:])
        mgr = "sys"
        if args[:2] == ["--user", "-M"]:
            mgr, args = "usr", args[3:]
        quiet = args[:1] == ["-q"]
        if quiet:
            args = args[1:]
        sub = args[0] if args else ""
        if sub == "set-property":
            return self._set(mgr, args[2], args[3:])
        if sub == "show":
            return self._show(mgr, args[1:])
        if sub == "list-units":
            return CommandResult(
                0,
                "".join(f"{n} loaded active running x\n" for n in self.units[mgr] if fnmatch.fnmatchcase(n, args[-1])),
                "",
            )
        if sub == "is-active":
            return CommandResult(0 if args[1] in self.active else 3, "", "")
        if sub == "daemon-reload" and (argv[3].removesuffix("@") if mgr == "usr" else "sys") in self.fail_reload:
            return CommandResult(1, "", "Failed to reload daemon")
        return CommandResult(0, "", "")

    def _set(self, mgr: str, unit: str, settings: Sequence[str]) -> CommandResult:
        if (mgr, unit) in self.fail_set:
            return CommandResult(1, "", f"Failed to set unit properties on {unit}")
        for kv in settings:
            prop, _, value = kv.partition("=")
            self.set_now(mgr, unit, **{prop: "17179869184" if value == "16G" else value})
            f = override_path(self.root, mgr, unit, prop)
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "w") as fh:
                fh.write(f"[Slice]\n{kv}\n")
        return CommandResult(0, "", "")

    def _show(self, mgr: str, args: Sequence[str]) -> CommandResult:
        if args[0] == "-P" and (mgr, args[2], args[1]) in self.fail_show:
            return CommandResult(1, "", "Failed to get properties: Connection timed out")
        if args[0] == "-P":
            return CommandResult(0, self.now(mgr, args[2], args[1]) + "\n", "")
        props: list[str] = []
        i = 0
        while args[i] == "-p":
            props.append(args[i + 1])
            i += 2
        unit = args[i]
        lines = []
        for p in props:
            if p == "LoadState":
                lines.append("LoadState=" + ("not-found" if (mgr, unit) in self.not_loaded else "loaded"))
            else:
                lines.append(f"{p}={self.now(mgr, unit, p)}")
        return CommandResult(0, "".join(line + "\n" for line in lines), "")

    def set_now(self, mgr: str, unit: str, **props: str) -> None:
        d = os.path.join(self.store, mgr, unit)
        os.makedirs(d, exist_ok=True)
        for k, v in props.items():
            with open(os.path.join(d, k), "w") as f:
                f.write(v + "\n")

    def now(self, mgr: str, unit: str, prop: str) -> str:
        try:
            with open(os.path.join(self.store, mgr, unit, prop)) as f:
                return f.read().strip()
        except OSError:
            return "[not set]"


@dataclasses.dataclass
class GameScratch:
    """The game profile's scratch machine (game_scratch): where things are, the fake systemd with its
    baseline, the user manager's cgroup folder and the settings of the tests."""

    paths: GamePaths
    systemd: FakeSystemd
    ucg: str
    settings: GameSettings


def add_gamemode_pid(proc: str, pid: str) -> None:
    """A process pid under the /proc folder proc with GameMode's client library loaded (the game)."""
    d = os.path.join(proc, pid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "maps"), "w") as f:
        f.write("7f00-7f01 r-xp 00000000 00:00 0 /usr/lib64/libgamemodeauto.so.0\n")


def game_scratch(tmp: str) -> GameScratch:
    """A fake cgroup tree, /proc and sysfs, and a fake systemd with its baseline, in the folder tmp
    (which the caller removes); a GameMode client runs."""
    t = tmp
    paths = GamePaths(
        conf=os.path.join(t, "conf"),
        run=os.path.join(t, "run"),
        cgroot=os.path.join(t, "cgroup"),
        unit_runtime=os.path.join(t, "unit-runtime"),
        run_systemd=os.path.join(t, "rs"),
        run_user=os.path.join(t, "ru"),
        sys_cpu=os.path.join(t, "sys-cpu"),
        sys_drm=os.path.join(t, "sys-drm"),
        proc=os.path.join(t, "proc"),
    )
    os.makedirs(paths.run)
    settings = GameSettings(target_user=USER, game_zswap_writeback="yes")
    # The user manager's cgroup folder (uid 999999), as cgroupfs shows it.
    ucg = os.path.join(paths.cgroot, "user.slice", f"user-{UID}.slice", f"user@{UID}.service")
    os.makedirs(ucg)
    # The user manager runs: its runtime folder is there (FakeSystemd writes its runtime overrides into it).
    os.makedirs(os.path.join(paths.run_user, UID, "systemd"))
    systemd = FakeSystemd(t)
    # The baseline as systemd shows it before the profile.
    systemd.set_now("sys", "machine.slice", CPUWeight="50", IOWeight="50")
    for u in ("user.slice", f"user-{UID}.slice", f"user@{UID}.service"):
        systemd.set_now("sys", u, MemoryLow="0")
    systemd.set_now("usr", "user.slice", CPUWeight="30")
    # A GameMode client runs (the game): pid 1 has its library loaded.
    add_gamemode_pid(paths.proc, "1")
    return GameScratch(paths, systemd, ucg, settings)


MEMORY_LOW_BYTES: Final = "17179869184"  # 16G as systemctl show gives it

type Key = tuple[str, str, str]  # (manager, unit, property)


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


def _kill_only[T](plan: FaultPlan, name: str, effect: Callable[[], T]) -> T:
    """A point of a kill only: a failure armed there runs the call."""
    return plan.step(name, effect, lambda _err: effect())


class FaultyStateFiles(GameStateFiles):
    """game.state, game.intended and the marks, each write and removal a point of plan."""

    def __init__(self, plan: FaultPlan) -> None:
        self.plan = plan

    @override
    def create(self, path: str, text: str) -> None:
        self.plan.step(f"write {path}", functools.partial(super().create, path, text))

    @override
    def append(self, path: str, text: str) -> None:
        self.plan.step(f"write {path}", functools.partial(super().append, path, text))

    @override
    def replace(self, path: str, text: str) -> None:
        self.plan.step(f"write {path}", functools.partial(super().replace, path, text))

    @override
    def truncate(self, path: str, size: int) -> None:
        _kill_only(self.plan, "os.truncate", functools.partial(super().truncate, path, size))

    @override
    def remove(self, path: str) -> None:
        _kill_only(self.plan, "os.unlink", functools.partial(super().remove, path))


class FaultyLeftoversFile(GameLeftoversFile):
    """game.leftovers, its write and removal points of plan."""

    def __init__(self, plan: FaultPlan, path: str) -> None:
        super().__init__(path)
        self.plan, self.path = plan, path

    @override
    def write(self, text: str) -> None:
        self.plan.step(f"write {self.path}", functools.partial(super().write, text))

    @override
    def remove(self) -> None:
        _kill_only(self.plan, "os.unlink", super().remove)


class FaultyUnitFiles(GameUnitFiles):
    """The runtime unit files, each write, folder made and removal a point of plan."""

    def __init__(self, plan: FaultPlan) -> None:
        self.plan = plan

    @override
    def make_folder(self, path: str) -> None:
        _kill_only(self.plan, "os.makedirs", functools.partial(super().make_folder, path))

    @override
    def write_dropin(self, path: str, text: str) -> None:
        self.plan.step(f"write {path}", functools.partial(super().write_dropin, path, text))

    @override
    def remove(self, path: str) -> None:
        _kill_only(self.plan, "os.unlink", functools.partial(super().remove, path))

    @override
    def remove_folder(self, path: str) -> None:
        _kill_only(self.plan, "os.rmdir", functools.partial(super().remove_folder, path))


class FaultyKernelFiles(GameKernelFiles):
    """The cgroup tree, each write a point of plan (a failure answers False, as an OSError does)."""

    def __init__(self, plan: FaultPlan) -> None:
        self.plan = plan

    @override
    def write(self, path: str, value: str) -> bool:
        return self.plan.step(f"write {path}", functools.partial(super().write, path, value), lambda _err: False)


class FaultySysfsFiles(GameSysfsFiles):
    """sysfs, each write a point of plan (a failure answers False, as an OSError does)."""

    def __init__(self, plan: FaultPlan) -> None:
        self.plan = plan

    @override
    def write(self, path: str, value: str) -> bool:
        return self.plan.step(f"write {path}", functools.partial(super().write, path, value), lambda _err: False)

# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the game profile (cpu/game, installed as cpu-game-profile): CPU range expansion,
where systemd keeps runtime overrides, on/off (exact revert, in reverse order), the drift repair and
report, the boxes of the user manager it makes idle (with a fake cgroup tree) and its MemoryLow
drop-ins; with a fake systemd. Also the state files' format: the fields as bash's printf %q writes
them, read back by bash's eval (an installed bash game-profile of an older CST reads them), and
files the bash version wrote read by this one.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest cpu.tests.test_game_profile -v
"""

from collections.abc import Sequence
import contextlib
import fnmatch
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

sys.dont_write_bytecode = True

from cpu.game import cli as game_cli
from cpu.game.adapters import GameProfile, load_profile, read_settings
from cpu.game.domain import GamePaths, GameSettings, ctl_file, escapes_in_state, expand_cpus, records, game_settings
from cst_core.domain.contracts import state_format
from cst_core.ports import CommandResult

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


class GameProfileCase(unittest.TestCase):
    """The game profile with a fake systemd, a fake cgroup tree, /proc and sysfs."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-game-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        t = self.tmp
        self.paths = GamePaths(
            conf=os.path.join(t, "conf"),
            run=os.path.join(t, "run"),
            cgroot=os.path.join(t, "cgroup"),
            unit_runtime=os.path.join(t, "unit-runtime"),
            run_systemd=os.path.join(t, "rs"),
            run_user=os.path.join(t, "ru"),
            sys_cpu=os.path.join(t, "sys-cpu"),
            proc=os.path.join(t, "proc"),
        )
        os.makedirs(self.paths.run)
        self.state, self.intended, self.manual = self.paths.state, self.paths.intended, self.paths.manual
        self.unit_runtime = self.paths.unit_runtime
        self.settings = GameSettings(target_user=USER, game_zswap_writeback="yes")
        # The user manager's cgroup folder (uid 999999), as cgroupfs shows it.
        self.ucg = os.path.join(self.paths.cgroot, "user.slice", f"user-{UID}.slice", f"user@{UID}.service")
        os.makedirs(self.ucg)
        self.systemd = FakeSystemd(t)
        # The baseline as systemd shows it before the profile.
        self.systemd.set_now("sys", "machine.slice", CPUWeight="50", IOWeight="50")
        for u in ("user.slice", f"user-{UID}.slice", f"user@{UID}.service"):
            self.systemd.set_now("sys", u, MemoryLow="0")
        self.systemd.set_now("usr", "user.slice", CPUWeight="30")
        # A GameMode client runs (the game): pid 1 has its library loaded.
        self.gamemode_pid("1")

    def gamemode_pid(self, pid: str) -> None:
        d = os.path.join(self.paths.proc, pid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "maps"), "w") as f:
            f.write("7f00-7f01 r-xp 00000000 00:00 0 /usr/lib64/libgamemodeauto.so.0\n")

    def no_game(self) -> None:
        shutil.rmtree(self.paths.proc)

    def set_now(self, mgr: str, unit: str, **props: str) -> None:
        self.systemd.set_now(mgr, unit, **props)

    def now(self, mgr: str, unit: str, prop: str) -> str:
        return self.systemd.now(mgr, unit, prop)

    def ctrl(self, mgr: str, unit: str, prop: str) -> str:
        return override_path(self.tmp, mgr, unit, prop)

    def profile(self, settings: GameSettings | None = None) -> tuple[GameProfile, io.StringIO]:
        out = io.StringIO()
        p = GameProfile(self.paths, settings or self.settings, self.systemd, uid_of=lambda _u: UID, out=out)
        return p, out

    def run_profile(self, action: str, settings: GameSettings | None = None) -> tuple[str, list[str]]:
        """One action (on, off, tick or state) with the fakes; (its output, the systemctl calls)."""
        self.systemd.actions = []
        p, out = self.profile(settings)
        rc = {"on": p.on, "off": p.off, "tick": p.tick, "state": p.state}[action]()
        self.assertEqual(rc, 0, out.getvalue())
        return out.getvalue(), self.systemd.actions

    def drift_report(self) -> tuple[str, list[str]]:
        self.systemd.actions = []
        p, _ = self.profile()
        return "".join(line + "\n" for line in p.drift_report()), self.systemd.actions

    def read(self, path: str) -> str:
        with open(path) as f:
            return f.read()

    def write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def sets(self, actions: Sequence[str]) -> list[str]:
        return [a for a in actions if "set-property" in a]


class PureRulesTest(unittest.TestCase):
    def test_expand_cpus(self) -> None:
        self.assertEqual(expand_cpus("0-3,6,12-14"), ["0", "1", "2", "3", "6", "12", "13", "14"])
        self.assertEqual(expand_cpus("0-1,,5,"), ["0", "1", "5"])
        self.assertEqual(expand_cpus("x-2,7"), ["7"])  # what seq cannot count gives nothing

    def test_override_files_of_fixed_and_transient_units(self) -> None:
        # systemd writes `set-property --runtime` of a transient unit (scopes,
        # systemd-run's run-*.service) into its transient folder: 'off' must
        # delete that file, or the unit keeps the setting until it ends.
        p = GamePaths()
        self.assertEqual(
            [
                ctl_file(p, "sys", "machine.slice", "CPUWeight", ""),
                ctl_file(p, "sys", "docker-abc.scope", "AllowedCPUs", ""),
                ctl_file(p, "usr", "lushtext.slice", "CPUWeight", "1000"),
                ctl_file(p, "usr", "run-u7.service", "CPUWeight", "1000"),
                ctl_file(p, "usr", "app-flatpak-com.valvesoftware.Steam-1.scope", "MemoryLow", "1000"),
                ctl_file(p, "usr", "flatpak-session-helper.service", "AllowedCPUs", "1000"),
            ],
            [
                "/run/systemd/system.control/machine.slice.d/50-CPUWeight.conf",
                "/run/systemd/transient/docker-abc.scope.d/50-AllowedCPUs.conf",
                "/run/user/1000/systemd/user.control/lushtext.slice.d/50-CPUWeight.conf",
                "/run/user/1000/systemd/transient/run-u7.service.d/50-CPUWeight.conf",
                "/run/user/1000/systemd/transient/app-flatpak-com.valvesoftware.Steam-1.scope.d/50-MemoryLow.conf",
                "/run/user/1000/systemd/user.control/flatpak-session-helper.service.d/50-AllowedCPUs.conf",
            ],
        )

    def test_the_state_files_are_the_registered_ones(self) -> None:
        p = GamePaths()
        self.assertEqual(p.state, state_format("game-state").path)
        self.assertEqual(p.intended, state_format("game-intended").path)

    def test_settings_over_the_defaults(self) -> None:
        self.assertEqual(game_settings({}), GameSettings())
        s = game_settings({"TARGET_USER": "u", "VM_WEIGHT": "idle", "OTHER": "x"})
        self.assertEqual((s.target_user, s.vm_weight, s.build_cpus), ("u", "idle", "12-23"))

    def test_settings_file_read_as_bash_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "cpu-setup.conf")
            self.assertEqual(read_settings(conf), (GameSettings(), ()))  # no file: the defaults
            with open(conf, "w") as f:
                f.write(
                    "# Managed by cpu-setup.sh (CST); values may be edited.\n"
                    "TARGET_USER='tester'\nBUILD_CPUS='12-23' # E-cores\nP_CORES=0-7\nVM_WEIGHT=10\n"
                    "GAME_MEMORY_LOW=\"16G\"\nGAME_ZSWAP_WRITEBACK='no'\nVM_NAME='win11-devbox'\n"
                )
            self.assertEqual(
                read_settings(conf),
                (GameSettings("tester", "12-23", "0-7", "10", "16G", "no"), ()),
            )


class GameProfileOnOffTest(GameProfileCase):
    """on and off: what each records, sets and reverts."""

    def test_off_reverts_in_reverse_order_and_cleans_state(self) -> None:
        cg_file = os.path.join(self.tmp, "cpuset.cpus")
        self.write(cg_file, "12-23\n")
        new_dropin = self.ctrl("usr", "user.slice", "CPUWeight")
        self.write(new_dropin, "[Slice]\nCPUWeight=idle\n")
        self.write(self.manual, "")
        self.write(self.intended, "prop sys machine.slice CPUWeight 10 10\n")
        self.write(
            self.state,
            f"prop sys machine.slice CPUWeight existed 50\nprop usr user.slice CPUWeight new -\ncgw {cg_file} 8-23\n",
        )
        _, actions = self.run_profile("off")
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.manual))
        self.assertFalse(os.path.exists(self.intended))
        self.assertFalse(os.path.exists(new_dropin))
        self.assertEqual(self.read(cg_file).strip(), "8-23")
        self.assertEqual(
            actions,
            [f"{SYS} set-property --runtime machine.slice CPUWeight=50", f"{USR} daemon-reload"],
        )

    def _run_on_off(self, vm_weight: str, prior_io_weight: str | None = None) -> tuple[str, list[str], bool, str]:
        io_dropin = self.ctrl("sys", "machine.slice", "IOWeight")
        if prior_io_weight is not None:
            self.write(io_dropin, f"[Slice]\nIOWeight={prior_io_weight}\n")
            self.set_now("sys", "machine.slice", IOWeight=prior_io_weight)
        s = GameSettings(target_user=USER, vm_weight=vm_weight, game_zswap_writeback="yes")
        _, on_actions = self.run_profile("on", s)
        state_after_on, intended_after_on = self.read(self.state), self.read(self.intended)
        _, off_actions = self.run_profile("off", s)
        self.assertFalse(os.path.exists(self.intended))
        return state_after_on, on_actions + off_actions, os.path.exists(io_dropin), intended_after_on

    def test_on_and_off_machine_slice_io_weight(self) -> None:
        # Numeric VM_WEIGHT reused for IOWeight, no prior runtime override.
        state_on, actions, io_exists, intended = self._run_on_off("25")
        self.assertIn("prop sys machine.slice IOWeight new -", state_on)
        self.assertIn(f"{SYS} set-property --runtime machine.slice AllowedCPUs=12-23 CPUWeight=25 IOWeight=25", actions)
        self.assertIn(f"{SYS} daemon-reload", actions)
        self.assertFalse(io_exists)
        # What 'on' set, as systemd shows it right after.
        self.assertIn("prop sys machine.slice CPUWeight 25 25\n", intended)
        self.assertIn(f"prop sys user-{UID}.slice MemoryLow 16G 17179869184\n", intended)
        self.assertIn("prop usr user.slice CPUWeight idle idle\n", intended)

    def test_vm_weight_idle_gives_io_weight_10_and_a_prior_override_comes_back(self) -> None:
        state_idle, actions_idle, io_exists_idle, _ = self._run_on_off("idle", prior_io_weight="80")
        self.assertIn("prop sys machine.slice IOWeight existed 80", state_idle)
        self.assertIn(
            f"{SYS} set-property --runtime machine.slice AllowedCPUs=12-23 CPUWeight=idle IOWeight=10", actions_idle
        )
        self.assertIn(f"{SYS} set-property --runtime machine.slice IOWeight=80", actions_idle)
        self.assertTrue(io_exists_idle)

    def test_on_without_a_target_user_refuses_and_changes_nothing(self) -> None:
        p, out = self.profile(GameSettings())
        self.assertEqual(p.on(), 1)
        self.assertEqual(out.getvalue(), f"<3>game-profile: TARGET_USER not set in {self.paths.conf}\n")
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.systemd.actions, [])

    def test_on_twice_says_already_on(self) -> None:
        self.run_profile("on")
        state = self.read(self.state)
        out, actions = self.run_profile("on")
        self.assertEqual((out, actions), ("<6>game-profile: already on\n", []))
        self.assertEqual(self.read(self.state), state)

    def test_epp_of_the_p_cores_is_recorded_and_restored(self) -> None:
        for cpu in range(3):
            self.write(self.paths.epp(str(cpu)), "balance_performance\n")
        s = GameSettings(target_user=USER, p_cores="0-1,7")
        self.run_profile("on", s)
        self.assertIn("epp 0 balance_performance\nepp 1 balance_performance\n", self.read(self.state))
        self.assertEqual(self.read(self.paths.epp("1")), "performance\n")
        self.assertEqual(self.read(self.paths.epp("2")), "balance_performance\n")  # not a P-core here
        self.run_profile("off", s)
        self.assertEqual(self.read(self.paths.epp("1")), "balance_performance\n")

    def test_zswap_writeback_kept_in_ram_only_when_asked_and_known(self) -> None:
        self.set_now("usr", "app.slice", MemoryZSwapWriteback="yes")
        _, actions = self.run_profile("on", GameSettings(target_user=USER))
        self.assertIn(f"{USR} set-property --runtime app.slice MemoryZSwapWriteback=no", actions)
        self.run_profile("off")
        _, actions = self.run_profile("on")  # GAME_ZSWAP_WRITEBACK=yes: left as it is
        self.assertFalse([a for a in actions if "MemoryZSwapWriteback=no" in a], actions)

    def test_containers_and_steam_scopes_get_their_settings_once(self) -> None:
        self.systemd.units = {
            "sys": ["docker-a.scope", "docker-b.scope"],
            "usr": ["app-flatpak-com.valvesoftware.Steam-9.scope"],
        }
        self.set_now("sys", "docker-a.scope", Slice="system.slice")
        self.set_now("sys", "docker-b.scope", Slice="builds.slice")
        self.systemd.active.add("builds.slice")
        _, actions = self.run_profile("on")
        sets = self.sets(actions)
        self.assertIn(f"{SYS} set-property --runtime builds.slice AllowedCPUs=12-23 CPUWeight=idle", sets)
        self.assertIn(f"{SYS} set-property --runtime docker-a.scope AllowedCPUs=12-23", sets)
        self.assertFalse([a for a in sets if "docker-b" in a], sets)
        self.assertIn(f"{USR} set-property --runtime app-flatpak-com.valvesoftware.Steam-9.scope MemoryLow=16G", sets)
        self.write(self.manual, "")
        _, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [])
        _, actions = self.run_profile("off")
        self.assertFalse(os.path.exists(self.ctrl("sys", "docker-a.scope", "AllowedCPUs")))

    def test_a_failed_set_property_is_logged_and_records_no_intended_value(self) -> None:
        self.systemd.fail_set.add(("sys", "machine.slice"))
        out, _ = self.run_profile("on")
        self.assertIn("<4>game-profile: machine.slice: could not set AllowedCPUs=12-23 CPUWeight=10 IOWeight=10\n", out)
        self.assertNotIn("machine.slice", self.read(self.intended))
        self.assertIn("prop sys machine.slice CPUWeight new -", self.read(self.state))

    def test_the_lock_keeps_actions_apart(self) -> None:
        p, out = self.profile()
        self.assertEqual(p.locked("on"), 0)
        self.assertTrue(os.path.exists(self.state))
        self.assertEqual(os.stat(self.paths.lock).st_mode & 0o777, 0o644)
        from cst_core.adapters.locks import take_lock

        fd = take_lock(self.paths.lock, wait=False)
        assert fd is not None
        try:
            with unittest.mock.patch("cpu.game.adapters.take_lock", return_value=None) as held:
                self.assertEqual(p.locked("off"), 1)
            self.assertEqual(held.call_args.kwargs["timeout"], 60)
        finally:
            os.close(fd)
        self.assertTrue(out.getvalue().endswith("<3>game-profile: lock busy\n"), out.getvalue())
        self.assertTrue(os.path.exists(self.state))  # off did not run


class GameProfileDriftTest(GameProfileCase):
    """The drift repair of the game profile (a daemon-reload or uresourced undoing a setting while
    the profile is on) and the drift report of 'state'."""

    def test_on_records_what_systemd_reads_back_and_not_for_scopes(self) -> None:
        self.run_profile("on")
        p, _ = self.profile()
        p.setp("usr", "app-flatpak-com.valvesoftware.Steam-1.scope", "MemoryLow=16G")
        intended = self.read(self.intended)
        self.assertIn("prop sys machine.slice CPUWeight 10 10\n", intended)
        self.assertIn("prop sys machine.slice IOWeight 10 10\n", intended)
        self.assertIn("prop sys user.slice MemoryLow 16G 17179869184\n", intended)
        self.assertIn(f"prop sys user@{UID}.service MemoryLow 16G 17179869184\n", intended)
        self.assertIn("prop usr user.slice CPUWeight idle idle\n", intended)
        self.assertNotIn("Steam", intended)
        self.assertIn("Steam-1.scope MemoryLow new -", self.read(self.state))  # off() still undoes it
        # game.state keeps the format off() reads: six fields at most.
        for line in self.read(self.state).splitlines():
            self.assertLessEqual(len(line.split()), 6, line)

    def test_tick_repairs_drift_without_touching_the_state_and_off_restores(self) -> None:
        # IOWeight had a runtime override of its own before the profile.
        self.set_now("sys", "machine.slice", IOWeight="80")
        self.write(self.ctrl("sys", "machine.slice", "IOWeight"), "[Slice]\nIOWeight=80\n")
        self.run_profile("on")
        state_after_on = self.read(self.state)
        self.write(self.manual, "")  # on by hand: tick does not switch it off
        # A daemon-reload with the old baseline name, and uresourced:
        self.set_now("sys", "machine.slice", CPUWeight="50", IOWeight="50")
        self.set_now("sys", "user.slice", MemoryLow="0")
        self.set_now("usr", "user.slice", CPUWeight="30")
        out, actions = self.run_profile("tick")
        self.assertEqual(
            sorted(self.sets(actions)),
            sorted(
                [
                    f"{SYS} set-property --runtime machine.slice CPUWeight=10",
                    f"{SYS} set-property --runtime machine.slice IOWeight=10",
                    f"{SYS} set-property --runtime user.slice MemoryLow=16G",
                    f"{USR} set-property --runtime user.slice CPUWeight=idle",
                ]
            ),
        )
        self.assertIn(
            "<5>game-profile: drift: user.slice MemoryLow was '0' instead of '17179869184' "
            "(daemon-reload or uresourced?): set MemoryLow=16G again",
            out,
        )
        self.assertEqual(self.now("sys", "machine.slice", "CPUWeight"), "10")
        self.assertEqual(self.now("sys", "user.slice", "MemoryLow"), "17179869184")
        self.assertEqual(self.read(self.state), state_after_on)  # no second record
        # One show per unit, and nothing to repair on the next tick.
        shows = [a for a in actions if " show -p " in a]
        self.assertEqual(len(shows), 6, shows)  # machine.slice, 4 of the memory chain, toolboxes
        out, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [])
        self.assertEqual(out, "")
        # off: the value from before 'on' comes back, the profile's own files go.
        out, actions = self.run_profile("off")
        self.assertIn(f"{SYS} set-property --runtime machine.slice IOWeight=80", actions)
        self.assertNotIn(f"{SYS} set-property --runtime machine.slice IOWeight=10", actions)
        self.assertFalse(os.path.exists(self.ctrl("sys", "machine.slice", "CPUWeight")))
        self.assertFalse(os.path.exists(self.ctrl("sys", "user.slice", "MemoryLow")))
        self.assertFalse(os.path.exists(self.ctrl("usr", "user.slice", "CPUWeight")))
        self.assertTrue(os.path.exists(self.ctrl("sys", "machine.slice", "IOWeight")))
        self.assertIn(f"{SYS} daemon-reload", actions)
        self.assertIn(f"{USR} daemon-reload", actions)
        for p in (self.state, self.intended, self.manual):
            self.assertFalse(os.path.exists(p), p)

    def test_cgroup_file_drift_is_rewritten(self) -> None:
        cg = os.path.join(self.tmp, "cpuset.cpus")
        self.write(cg, "8-23\n")
        self.write(self.state, f"cgw {cg} ''\n")
        self.write(self.intended, f"cgw {cg} 12-23 12-23\n")
        self.write(self.manual, "")
        out, _ = self.run_profile("tick")
        self.assertEqual(self.read(cg).strip(), "12-23")
        self.assertIn(f"drift: {cg} was '8-23', wrote 12-23 again", out)
        # The cgroup went away (its unit stopped): nothing to compare, nothing logged.
        os.remove(cg)
        out, _ = self.run_profile("tick")
        self.assertEqual(out, "")
        self.assertFalse(os.path.exists(cg))

    def test_unloaded_unit_and_empty_readback_are_not_repaired(self) -> None:
        self.write(self.state, "prop sys builds.slice CPUWeight new -\nprop usr app.slice MemoryZSwapWriteback new -\n")
        self.write(
            self.intended, "prop sys builds.slice CPUWeight idle idle\nprop usr app.slice MemoryZSwapWriteback no ''\n"
        )
        self.systemd.not_loaded.add(("sys", "builds.slice"))
        self.write(self.manual, "")
        out, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [])
        self.assertEqual(out, "")

    def test_old_format_state_is_tolerated(self) -> None:
        # Switched on by a version without game.intended: no comparison at all.
        self.write(self.state, "prop sys machine.slice CPUWeight new -\nprop sys user.slice MemoryLow new -\n")
        self.write(self.manual, "")
        self.set_now("sys", "machine.slice", CPUWeight="50")
        out, actions = self.run_profile("tick")
        self.assertEqual(out, "")
        self.assertEqual([a for a in actions if " show " in a or "set-property" in a], [])
        out, _ = self.drift_report()
        self.assertIn("drift: not checked (switched on by an older version", out)
        # On by hand (the manual mark): a restart would drop the mark, so the
        # command keeps the profile on by hand.
        self.assertIn("cst cpu game off && cst cpu game on switches it off and on again", out)
        os.remove(self.manual)
        out, _ = self.drift_report()
        self.assertIn("sudo systemctl restart cpu-game.service switches it off and on again", out)
        self.write(self.manual, "")
        _, actions = self.run_profile("off")
        self.assertIn(f"{SYS} daemon-reload", actions)
        self.assertFalse(os.path.exists(self.state))

    def test_state_shows_drift_per_property(self) -> None:
        self.run_profile("on")
        out, _ = self.drift_report()
        self.assertEqual(out, "  drift: none (every setting the profile made still holds)\n")
        self.set_now("sys", "machine.slice", CPUWeight="50")
        self.set_now("sys", f"user-{UID}.slice", MemoryLow="0")
        out, actions = self.drift_report()
        self.assertIn("  DRIFT: machine.slice CPUWeight is '50', the profile set '10'\n", out)
        self.assertIn(f"  DRIFT: user-{UID}.slice MemoryLow is '0', the profile set '17179869184'\n", out)
        self.assertIn("the watchdog sets them again within 30 s", out)
        self.assertIn("if they stay: sudo systemctl restart cpu-game.service", out)
        self.assertEqual(self.sets(actions), [])  # state only reads

    def test_state_lines(self) -> None:
        self.write(self.paths.epp("0"), "balance_performance\n")
        self.write(self.paths.epp("12"), "power\n")
        self.set_now("sys", "machine.slice", AllowedCPUs="12-23")
        self.set_now("usr", "user.slice", ControlGroup="/user.slice/x")
        self.write(os.path.join(self.paths.cgroot, "user.slice", "x", "cpuset.cpus.effective"), "8-23\n")
        out, actions = self.run_profile("state")
        self.assertEqual(
            out,
            "game profile: off\n"
            "  machine.slice   cpus=12-23    weight=50   io=50\n"
            "  toolboxes       weight=30\n"
            "  app.slice       zswap writeback=\n"
            "  toolboxes       cpus effective=8-23\n"
            "  EPP cpu0 balance_performance, cpu12 power\n",
        )
        self.assertEqual(self.sets(actions), [])
        self.systemd.active.add("builds.slice")
        self.run_profile("on")
        self.write(self.manual, "")
        out, _ = self.run_profile("state")
        self.assertTrue(
            out.startswith("game profile: ON (by hand)\n  machine.slice   cpus=12-23    weight=10   io=10\n"), out
        )
        self.assertIn("  builds.slice    cpus=12-23    weight=idle io=\n", out)
        self.assertIn("  other boxes     idle: none\n", out)
        self.assertTrue(out.endswith("  drift: none (every setting the profile made still holds)\n"), out)


class IntendedFormatTest(GameProfileCase):
    """game.intended names its format on its first line; a newer one is left alone."""

    def test_on_writes_the_format_line_first(self) -> None:
        self.run_profile("on")
        first, _, rest = self.read(self.intended).partition("\n")
        self.assertEqual(first, "cst-game-intended 1")
        self.assertTrue(rest.startswith("prop sys machine.slice AllowedCPUs 12-23 "), rest)
        self.assertEqual(records(first), [("cst-game-intended", "1")])
        # Drift is compared below it as before.
        self.write(self.manual, "")
        self.set_now("sys", "machine.slice", CPUWeight="50")
        out, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [f"{SYS} set-property --runtime machine.slice CPUWeight=10"])
        self.assertIn("drift: machine.slice CPUWeight was '50'", out)

    def test_a_newer_format_is_neither_repaired_nor_compared(self) -> None:
        self.write(self.state, "prop sys machine.slice CPUWeight new -\n")
        self.write(self.intended, "cst-game-intended 2\nprop sys machine.slice CPUWeight 10 10\n")
        self.write(self.manual, "")
        self.set_now("sys", "machine.slice", CPUWeight="50")
        out, actions = self.run_profile("tick")
        why = f"{self.intended} is format 2 of the game-intended (this CST reads format 1 and older): written by a newer CST"
        self.assertEqual(out, f"<4>game-profile: drift not repaired: {why}\n")
        self.assertEqual([a for a in actions if " show -p " in a or "set-property" in a], [])
        report, _ = self.drift_report()
        self.assertEqual(report, f"  drift: not checked ({why})\n")
        self.write(self.intended, "cst-game-intended x\nprop sys machine.slice CPUWeight 10 10\n")
        report, _ = self.drift_report()
        self.assertEqual(report, f"  drift: not checked ({self.intended}: its first line names no format version)\n")
        # off still undoes everything: it reads game.state only.
        _, actions = self.run_profile("off")
        self.assertIn(f"{SYS} daemon-reload", actions)
        self.assertFalse(os.path.exists(self.intended))


class SettingsProblemTest(GameProfileCase):
    """A line of cpu-setup.conf that bash would run or expand sets nothing: the profile does not
    switch on, and a tick changes nothing, until it is fixed; off still undoes everything."""

    def problem_profile(self) -> tuple[GameProfile, io.StringIO]:
        text = "TARGET_USER='testuser'\nBUILD_CPUS=$(nproc)\n"
        self.write(self.paths.conf, text)
        settings, problems = read_settings(self.paths.conf)
        self.assertEqual([p.line for p in problems], [2])
        out = io.StringIO()
        return GameProfile(self.paths, settings, self.systemd, uid_of=lambda _u: UID, problems=problems, out=out), out

    def test_on_refuses_before_it_changes_anything(self) -> None:
        p, out = self.problem_profile()
        self.assertEqual(p.on(), 1)
        conf = self.paths.conf
        self.assertEqual(
            out.getvalue(),
            f"<3>game-profile: {conf} line 2: BUILD_CPUS=$(nproc): $, ` or \\ outside single quotes: bash would expand it"
            " (use single quotes)\n"
            f"<3>game-profile: not switched on: a line of {conf} sets nothing (fix it; 'off' still works)\n",
        )
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.systemd.actions, [])
        self.assertEqual(p.locked("on"), 1)  # what the unit's ExecStart gets

    def test_tick_changes_nothing_but_still_switches_off_and_off_works(self) -> None:
        self.run_profile("on")  # switched on before the line was broken
        self.write(self.manual, "")
        self.set_now("sys", "machine.slice", CPUWeight="50")
        p, out = self.problem_profile()
        self.systemd.actions = []
        self.assertEqual(p.tick(), 1)
        self.assertIn("<3>game-profile: tick changed nothing: a line of", out.getvalue())
        self.assertEqual(self.sets(self.systemd.actions), [])
        os.remove(self.manual)
        self.no_game()
        self.systemd.actions = []
        self.assertEqual(p.tick(), 0)  # no game left: switched off as before
        self.assertEqual(self.systemd.actions, [f"{SYS} stop --no-block cpu-game.service"])
        self.assertEqual(p.off(), 0)
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(
            self.now("sys", "machine.slice", "CPUWeight"), "50"
        )  # the override is gone; the store keeps the fake's last

    def test_the_program_builds_the_profile_over_the_settings_file(self) -> None:
        self.write(self.paths.conf, "TARGET_USER='testuser'\nBUILD_CPUS=$(nproc)\n")
        p = load_profile(self.paths, self.systemd)
        self.assertEqual((p.s.target_user, p.s.build_cpus, [x.line for x in p.problems]), ("testuser", "12-23", [2]))
        self.assertEqual(p.on(), 1)
        os.remove(self.paths.conf)
        p = load_profile(self.paths, self.systemd)
        self.assertEqual((p.s, p.problems), (GameSettings(), ()))

    def test_state_names_the_line(self) -> None:
        p, out = self.problem_profile()
        self.assertEqual(p.state(), 0)
        self.assertIn(f"\n  settings: {self.paths.conf} line 2: BUILD_CPUS=$(nproc): ", out.getvalue())
        self.assertIn("(sets nothing: 'on' and 'tick' refuse until it is fixed)\n", out.getvalue())


class GameProfileEscapesTest(GameProfileCase):
    """The boxes of the user manager that sit next to the game without a rule
    of their own (top-level slices, systemd-run jobs in app.slice): idle while
    the profile is on, picked up by tick, reverted by off. And the MemoryLow
    drop-ins that win over uresourced's at a daemon-reload."""

    def cg(self, *parts: str, procs: Sequence[int] = ()) -> str:
        d = os.path.join(self.ucg, *parts)
        os.makedirs(d, exist_ok=True)
        if procs:
            with open(os.path.join(d, "cgroup.procs"), "w") as f:
                f.write("".join(f"{p}\n" for p in procs))
        return d

    def tree(self, delegated: bool = False) -> None:
        with open(os.path.join(self.ucg, "cgroup.subtree_control"), "w") as f:
            f.write("cpuset cpu memory pids\n" if delegated else "cpu memory pids\n")
        for d in (
            "app.slice/app-gnome-firefox-1.scope",
            "app.slice/flatpak-session-helper.service",
            "app.slice/app-flatpak-com.valvesoftware.Steam-2.scope",
            "session.slice/org.gnome.Shell@wayland.service",
            "user.slice/libpod-1.scope",
            "init.scope",
            "uresourced.service",
            "background.slice/localsearch-3.service",
            "lushtext.slice/run-u5.service",
            "_cpu.slice",
            "app.slice/run-u7.service",
            "app.slice/run-r1a2.scope",
        ):
            self.cg(*d.split("/"))

    def test_on_makes_the_other_boxes_idle_and_records_only_slices(self) -> None:
        self.tree()
        out, actions = self.run_profile("on")
        sets = self.sets(actions)
        for u in ("background.slice", "lushtext.slice", "cpu.slice", "run-u7.service", "run-r1a2.scope"):
            self.assertIn(f"{USR} set-property --runtime {u} CPUWeight=idle", sets)
        for u in (
            "app.slice CPUWeight",
            "session.slice",
            "init.scope",
            "uresourced.service",
            "firefox",
            "flatpak-session-helper",
            "Steam",
            "localsearch",
            "run-u5.service",
            "_cpu",
        ):
            self.assertFalse([a for a in sets if u in a], (u, sets))
        self.assertFalse([a for a in sets if a.startswith(USR) and "AllowedCPUs" in a], sets)  # not delegated
        self.assertIn("lushtext.slice: idle while the game runs", out)
        state, intended = self.read(self.state), self.read(self.intended)
        for u in ("background.slice", "lushtext.slice", "cpu.slice", "run-u7.service", "run-r1a2.scope"):
            self.assertIn(f"prop usr {u} CPUWeight new -\n", state)
        # Drift repair covers the slices; transient units have no drop-in that could win.
        self.assertIn("prop usr lushtext.slice CPUWeight idle idle\n", intended)
        self.assertIn("prop usr background.slice CPUWeight idle idle\n", intended)
        self.assertNotIn("run-", intended)
        # Sorted: the glob order of _cpu.slice depends on the locale.
        self.assertEqual(
            sorted(escapes_in_state(records(state)).split()),
            ["background.slice", "cpu.slice", "lushtext.slice", "run-r1a2.scope", "run-u7.service"],
        )

    def test_with_cpuset_delegated_they_also_leave_the_game_cpus(self) -> None:
        self.tree(delegated=True)
        _, actions = self.run_profile("on")
        sets = self.sets(actions)
        self.assertIn(f"{USR} set-property --runtime lushtext.slice CPUWeight=idle AllowedCPUs=12-23", sets)
        self.assertIn(f"{USR} set-property --runtime run-u7.service CPUWeight=idle AllowedCPUs=12-23", sets)
        self.assertIn(f"{USR} set-property --runtime user.slice AllowedCPUs=12-23", sets)

    def test_with_cpuset_delegated_a_manager_that_ignores_it_gets_the_cgroup_file(self) -> None:
        self.tree(delegated=True)
        cpus = self.cg("user.slice")
        self.write(os.path.join(cpus, "cpuset.cpus"), "8-23\n")
        rel = os.path.relpath(cpus, self.paths.cgroot)
        self.set_now("usr", "user.slice", ControlGroup="/" + rel)
        self.run_profile("on")
        f = os.path.join(cpus, "cpuset.cpus")
        self.assertEqual(self.read(f), "12-23\n")
        self.assertIn(f"cgw {f} 8-23\n", self.read(self.state))
        self.assertIn(f"cgw {f} 12-23 12-23\n", self.read(self.intended))
        self.run_profile("off")
        self.assertEqual(self.read(f), "8-23\n")

    def test_a_box_with_the_game_in_it_is_left_alone(self) -> None:
        self.tree()
        self.cg("app.slice", "run-r9.scope", procs=(4242,))
        self.cg("games.slice", "run-u3.service", procs=(77, 4243))
        for pid in (4242, 4243):
            self.gamemode_pid(str(pid))
        _, actions = self.run_profile("on")
        sets = self.sets(actions)
        self.assertFalse([a for a in sets if "run-r9.scope" in a or "games.slice" in a], sets)
        self.assertIn(f"{USR} set-property --runtime lushtext.slice CPUWeight=idle", sets)

    def test_tick_picks_up_new_boxes_once(self) -> None:
        self.tree()
        self.run_profile("on")
        self.write(self.manual, "")
        state_after_on = self.read(self.state)
        _, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [])
        self.assertEqual(self.read(self.state), state_after_on)
        # Appear while the profile is on: one with a name printf %q quotes.
        self.cg("lush\\x2dbatch.slice")
        self.cg("app.slice", "run-u8.service")
        _, actions = self.run_profile("tick")
        self.assertEqual(
            sorted(self.sets(actions)),
            sorted(
                [
                    f"{USR} set-property --runtime lush\\x2dbatch.slice CPUWeight=idle",
                    f"{USR} set-property --runtime run-u8.service CPUWeight=idle",
                ]
            ),
        )
        grown = self.read(self.state)
        self.assertIn("prop usr lush\\\\x2dbatch.slice CPUWeight new -\n", grown)
        # A second tick adds nothing: game.state does not grow every 30 s.
        _, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [])
        self.assertEqual(self.read(self.state), grown)

    def test_off_reverts_them_and_tolerates_units_that_are_gone(self) -> None:
        self.tree()
        self.run_profile("on")
        self.assertTrue(os.path.exists(self.ctrl("usr", "lushtext.slice", "CPUWeight")))
        # The job ended: its unit and cgroup are gone (systemd may already
        # have removed the transient unit's control file too).
        shutil.rmtree(os.path.join(self.ucg, "app.slice", "run-u7.service"))
        shutil.rmtree(os.path.dirname(self.ctrl("usr", "run-u7.service", "CPUWeight")))
        _, actions = self.run_profile("off")
        for u in ("background.slice", "lushtext.slice", "cpu.slice", "run-u7.service", "run-r1a2.scope"):
            self.assertFalse(os.path.exists(os.path.dirname(self.ctrl("usr", u, "CPUWeight"))), u)
        self.assertIn(f"{USR} daemon-reload", actions)
        self.assertFalse(os.path.exists(self.state))

    def test_upgrade_while_on_from_the_previous_version(self) -> None:
        # Switched on by the previous version: game.state and game.intended
        # in the same format, but no zz- drop-ins and no other boxes handled.
        self.run_profile("on")
        shutil.rmtree(self.unit_runtime)
        for f in (self.state, self.intended):
            self.assertNotIn("lushtext", self.read(f))
        self.write(self.manual, "")
        # Installed while the game runs: the next tick picks up the boxes and
        # leaves the profile on (no drop-ins to remove, no reload).
        self.tree()
        _, actions = self.run_profile("tick")
        self.assertIn(f"{USR} set-property --runtime lushtext.slice CPUWeight=idle", self.sets(actions))
        self.assertFalse([a for a in actions if "daemon-reload" in a], actions)
        self.assertIn("prop usr lushtext.slice CPUWeight new -\n", self.read(self.state))
        self.assertTrue(os.path.exists(self.state))
        # off undoes both what the old version set and what the new tick added.
        old_set = os.path.dirname(self.ctrl("sys", "machine.slice", "CPUWeight"))
        self.assertTrue(os.path.exists(old_set))
        _, actions = self.run_profile("off")
        self.assertIn(f"{SYS} daemon-reload", actions)
        self.assertIn(f"{USR} daemon-reload", actions)
        self.assertFalse(os.path.exists(old_set))
        self.assertFalse(os.path.exists(os.path.dirname(self.ctrl("usr", "lushtext.slice", "CPUWeight"))))
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.unit_runtime))

    def test_a_slice_with_its_own_override_gets_it_back_and_drift_is_repaired(self) -> None:
        self.tree()
        # Someone had set lushtext.slice to weight 50 at run time before the game.
        own = self.ctrl("usr", "lushtext.slice", "CPUWeight")
        self.write(own, "[Slice]\nCPUWeight=50\n")
        self.set_now("usr", "lushtext.slice", CPUWeight="50")
        self.run_profile("on")
        self.write(self.manual, "")
        self.assertIn("prop usr lushtext.slice CPUWeight existed 50\n", self.read(self.state))
        # A daemon-reload put 50 back meanwhile: the tick sets idle again,
        # without a second record.
        self.set_now("usr", "lushtext.slice", CPUWeight="50")
        state = self.read(self.state)
        out, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [f"{USR} set-property --runtime lushtext.slice CPUWeight=idle"])
        self.assertIn("drift: lushtext.slice CPUWeight was '50'", out)
        self.assertEqual(self.read(self.state), state)
        _, actions = self.run_profile("off")
        self.assertIn(f"{USR} set-property --runtime lushtext.slice CPUWeight=50", actions)
        self.assertEqual(self.now("usr", "lushtext.slice", "CPUWeight"), "50")
        self.assertTrue(os.path.exists(own))

    def test_on_writes_the_memorylow_dropins_and_off_removes_them(self) -> None:
        self.run_profile("on")
        files = {
            u: os.path.join(self.unit_runtime, f"{u}.d", "zz-cst-game.conf")
            for u in ("user.slice", f"user-{UID}.slice", f"user@{UID}.service")
        }
        for u, f in files.items():
            lines = [line for line in self.read(f).splitlines() if not line.startswith("#")]
            self.assertEqual(lines, ["[Service]" if u.endswith(".service") else "[Slice]", "MemoryLow=16G"], u)
        _, actions = self.run_profile("off")
        self.assertIn(f"{SYS} daemon-reload", actions)
        for f in files.values():
            self.assertFalse(os.path.exists(os.path.dirname(f)), f)

    def test_on_sets_memorylow_live_without_a_reload(self) -> None:
        _, actions = self.run_profile("on")
        self.assertIn(f"{SYS} set-property --runtime user-{UID}.slice MemoryLow=16G", actions)
        self.assertFalse([a for a in actions if "daemon-reload" in a], actions)

    def test_off_without_state_still_removes_leftover_dropins(self) -> None:
        d = os.path.join(self.unit_runtime, f"user-{UID}.slice.d")
        self.write(os.path.join(d, "zz-cst-game.conf"), "[Slice]\nMemoryLow=16G\n")
        other = os.path.join(self.unit_runtime, "user.slice.d", "10-other.conf")
        self.write(other, "")
        out, actions = self.run_profile("off")
        self.assertFalse(os.path.exists(d))
        self.assertTrue(os.path.exists(other))  # only the profile's own file goes
        self.assertEqual(actions, [f"{SYS} daemon-reload"])
        self.assertIn("were still there: removed them", out)
        # Nothing left: no reload at all.
        _, actions = self.run_profile("off")
        self.assertEqual(actions, [])

    def test_tick_with_the_profile_off_removes_leftover_dropins(self) -> None:
        d = os.path.join(self.unit_runtime, f"user@{UID}.service.d")
        self.write(os.path.join(d, "zz-cst-game.conf"), "")
        self.no_game()
        out, actions = self.run_profile("tick")
        self.assertFalse(os.path.exists(d))
        self.assertEqual(actions, [f"{SYS} daemon-reload"])
        self.assertIn("were still there: removed them", out)
        out, actions = self.run_profile("tick")
        self.assertEqual((out, actions), ("", []))


class GameProfileWatchdogTest(GameProfileCase):
    """tick switches the profile on and off with the game, and leaves a by-hand choice alone."""

    def test_a_game_with_the_profile_off_switches_it_on_unless_switched_off_by_hand(self) -> None:
        out, actions = self.run_profile("tick")
        self.assertEqual(out, "<5>game-profile: GameMode client running but profile off: switching on\n")
        self.assertEqual(actions, [f"{SYS} start --no-block cpu-game.service"])
        self.write(self.paths.suppress, "")
        self.assertEqual(self.run_profile("tick"), ("", []))
        self.no_game()  # the game ended: the next one switches it on again
        self.assertEqual(self.run_profile("tick"), ("", []))
        self.assertFalse(os.path.exists(self.paths.suppress))

    def test_no_game_left_switches_it_off_unless_on_by_hand(self) -> None:
        self.run_profile("on")
        self.no_game()
        self.write(self.manual, "")
        _, actions = self.run_profile("tick")
        self.assertNotIn(f"{SYS} stop --no-block cpu-game.service", actions)
        os.remove(self.manual)
        out, actions = self.run_profile("tick")
        self.assertEqual(out, "<5>game-profile: no GameMode client left: switching off\n")
        self.assertEqual(actions, [f"{SYS} stop --no-block cpu-game.service"])


class StateFileFormatTest(unittest.TestCase):
    """The fields of game.state and game.intended are written as bash's printf %q writes them: an
    installed bash game-profile (an older CST) greps them for its own quoted form and reads them
    with eval; and this version reads what that one wrote."""

    def bash(self, script: str, *args: str) -> str:
        r = subprocess.run(
            ["bash", "-c", script, "bash", *args],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
        )
        return r.stdout

    def test_bash_reads_our_records_back_field_by_field(self) -> None:
        from cpu.game.domain import record_line

        fields = ["prop", "usr", "lush\\x2dbatch.slice", "CPUWeight", "existed", "", "a b", "$(x)", "~u", "#c", "a\tb"]
        line = record_line(*fields)
        # What off(), drift() and escapes_in_state() of the bash version do with a line.
        got = self.bash('line="$1"; eval "rec=(${line})"; printf "%s\\0" "${rec[@]}"', line.rstrip("\n"))
        self.assertEqual(got.split("\0")[:-1], fields)
        # And the line is the one bash's printf %q writes, so its grep -F finds it.
        self.assertEqual(self.bash('printf "%q %q %q %q %q %q %q %q %q %q %q\\n" "$@"', *fields), line)

    def test_we_read_what_the_bash_version_wrote(self) -> None:
        fields = ["prop", "usr", "lush\\x2dbatch.slice", "MemoryLow", "existed", "", "it's", "a\nb", "é"]
        line = self.bash('printf "%q %q %q %q %q %q %q %q %q\\n" "$@"', *fields)
        self.assertEqual(records(line), [tuple(fields)])


class GameProfileCliTest(unittest.TestCase):
    def test_usage(self) -> None:
        for argv in ([], ["bogus"], ["--help"], [""]):
            err = io.StringIO()
            with unittest.mock.patch("sys.stderr", err):
                self.assertEqual(game_cli.main(argv), 2)
            self.assertEqual(err.getvalue(), "usage: game-profile on|off|tick|state\n")

    def test_the_program_prints_its_usage_and_writes_no_bytecode(self) -> None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        r = subprocess.run(
            ["/usr/bin/python3", "-I", os.path.join(here, "game-profile"), "bogus"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual((r.returncode, r.stdout, r.stderr), (2, "", "usage: game-profile on|off|tick|state\n"))


class BashReadsTheRecordsTest(GameProfileCase):
    """A line of game.state or game.intended that holds no record, read as bash's eval read it: 'off'
    stops at a record cut short (exit 1, game.state kept, no reload), an unclosed quote repeats the
    record before it; the drift check compares nothing; 'state' lists no idle boxes."""

    def off_rc(self) -> tuple[int, str, str]:
        self.systemd.actions = []
        p, out = self.profile()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = p.off()
        return rc, out.getvalue(), err.getvalue()

    def test_off_stops_at_a_record_cut_short_and_keeps_game_state(self) -> None:
        self.run_profile("on")
        with open(self.state, "a") as f:
            f.write("prop sys machine.slice IOWeight existed\n")
        n = len(self.read(self.state).split("\n")) - 1
        before = self.read(self.state)
        rc, out, err = self.off_rc()
        self.assertEqual((rc, out), (1, ""))
        self.assertEqual(err, f"{self.state} line {n}: no record 'off' can read\n")
        self.assertEqual(self.read(self.state), before)
        self.assertNotIn(f"{SYS} daemon-reload", self.systemd.actions)
        self.assertTrue(os.path.exists(self.ctrl("sys", "machine.slice", "CPUWeight")))  # nothing older undone

    def test_an_unclosed_quote_repeats_the_record_before_it(self) -> None:
        self.write(self.state, "prop sys machine.slice CPUWeight existed 50\nprop sys 'user.slice MemoryLow new -\n\n")
        # Newest first: the bad line (with no record before it) stops bash at once.
        rc, _, _ = self.off_rc()
        self.assertEqual(rc, 1)
        self.write(self.state, "prop sys 'user.slice MemoryLow new -\nprop sys machine.slice CPUWeight existed 50\n")
        rc, out, _ = self.off_rc()
        self.assertEqual(rc, 0, out)
        sets = [a for a in self.systemd.actions if "set-property" in a]
        self.assertEqual(sets, [f"{SYS} set-property --runtime machine.slice CPUWeight=50"] * 2)
        self.assertFalse(os.path.exists(self.state))

    def test_the_drift_check_compares_nothing_and_state_lists_no_idle_boxes(self) -> None:
        self.run_profile("on")
        self.write(self.manual, "")
        self.set_now("sys", "machine.slice", CPUWeight="50")  # drifted
        with open(self.intended, "a") as f:
            f.write("prop sys machine.slice\n")
        out, actions = self.run_profile("state")
        self.assertIn("  drift: none", out)
        self.assertFalse([a for a in actions if " show -p " in a])
        with open(self.state, "a") as f:
            f.write("prop usr 'x CPUWeight new -\n")
        out, _ = self.run_profile("state")
        self.assertIn("\n  other boxes     idle: \n", out)


if __name__ == "__main__":
    unittest.main()

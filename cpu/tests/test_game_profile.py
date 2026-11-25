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

from collections.abc import Callable, Generator, Sequence
import contextlib
import dataclasses
import errno
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from typing import override

sys.dont_write_bytecode = True

from cpu.game import cli as game_cli
from cpu.game.adapters.host import read_settings, read_snapshot, user_id, user_name
from cpu.game.adapters.files import GameLeftoversFile, GameRunLock, GameStateFiles, GameUnitFiles
from cpu.game.adapters.kernel import GameKernelFiles
from cpu.game.adapters.power import GameSysfsFiles
from cpu.game.adapters.profile import GameProfile, load_profile, machine_ports
from cpu.game.application.leftovers import unit_gone, user_manager_gone
from cpu.game.application.profile import GameProfileSteps
from cst_core.adapters import files as cst_files
from cst_core.adapters.files import read_text
from tests.probes import time_bound
from cpu.game.domain import (
    GamePaths,
    GameSettings,
    ctl_file,
    Drift,
    cut_tail,
    drifted,
    escaped_unit,
    escapes_in_state,
    expand_cpus,
    has_prop_record,
    intended_header,
    intended_records,
    newer_intended,
    record_line,
    records,
    game_settings,
    unheld,
    unit_name_valid,
    vm_io_weight,
)
from cpu.game.domain.boxes import (
    Unset,
    cpuset_file_form,
    intended_covers,
    set_again_text,
    setting_holds,
    unset_settings,
)
from cpu.game.domain.leftovers import (
    Entry,
    entry_key,
    kept_count_text,
    leftover_entries,
    leftovers_line,
    leftovers_text,
    merged_entries,
    newer_leftovers,
    off_text,
    undo_text,
)
from cst_core.domain.contracts import StateFormatKey, state_format
from cst_core.ports import CommandResult
from cpu.tests.fakes import SYS, UID, USER, USR, add_gamemode_pid, game_scratch, override_path


class FailingOnceKernel(GameKernelFiles):
    """The cgroup tree, whose first write of each of these files fails (False, as an OSError gives)."""

    def __init__(self, failing: set[str]) -> None:
        self.failing = set(failing)

    @override
    def write(self, path: str, value: str) -> bool:
        if path in self.failing:
            self.failing.discard(path)
            return False
        return super().write(path, value)


class GameProfileCase(unittest.TestCase):
    """The game profile with a fake systemd, a fake cgroup tree, /proc and sysfs."""

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-game-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        scratch = game_scratch(self.tmp)
        self.paths = scratch.paths
        self.state, self.intended, self.manual = self.paths.state, self.paths.intended, self.paths.manual
        self.unit_runtime = self.paths.unit_runtime
        self.settings = scratch.settings
        self.ucg = scratch.ucg
        self.systemd = scratch.systemd
        self.over: dict[str, object] = {}

    def gamemode_pid(self, pid: str) -> None:
        add_gamemode_pid(self.paths.proc, pid)

    def no_game(self) -> None:
        shutil.rmtree(self.paths.proc)

    def set_now(self, mgr: str, unit: str, **props: str) -> None:
        self.systemd.set_now(mgr, unit, **props)

    def now(self, mgr: str, unit: str, prop: str) -> str:
        return self.systemd.now(mgr, unit, prop)

    def ctrl(self, mgr: str, unit: str, prop: str) -> str:
        return override_path(self.tmp, mgr, unit, prop)

    def profile(self, settings: GameSettings | None = None) -> tuple[GameProfileSteps, io.StringIO]:
        """The profile over the real adapters (GameProfile); within ports(), with those adapters replaced."""
        out = io.StringIO()
        if not self.over:
            return GameProfile(self.paths, settings or self.settings, self.systemd, uid_of=lambda _u: UID, out=out), out
        real = machine_ports(self.paths, self.systemd, uid_of=lambda _u: UID, out=out)
        return GameProfileSteps(self.paths, settings or self.settings, dataclasses.replace(real, **self.over)), out

    @contextlib.contextmanager
    def ports(self, **over: object) -> Generator[None]:
        """The profiles made meanwhile (profile, run_profile) have these adapters (GamePorts fields) instead."""
        self.over = over
        try:
            yield
        finally:
            self.over = {}

    def run_profile(self, action: str, settings: GameSettings | None = None) -> tuple[str, list[str]]:
        """One action (on, off, tick or state) with the fakes; (its output, the systemctl calls)."""
        self.systemd.actions = []
        p, out = self.profile(settings)
        rc = {"on": p.on, "off": p.off, "tick": p.tick, "state": p.state}[action]()
        self.assertEqual(rc, 0, out.getvalue())
        return out.getvalue(), self.systemd.actions

    def drift_report(self) -> tuple[str, list[str]]:
        """'state''s drift part over a snapshot of the two files, as 'state' reads them."""
        self.systemd.actions = []
        p, _ = self.profile()
        intended = read_text(self.intended)
        newer = None if intended is None else newer_intended(self.intended, intended)
        report = p.drift_report(
            records(read_text(self.state) or ""), intended, newer, manual=os.path.exists(self.manual)
        )
        return "".join(line + "\n" for line in report), self.systemd.actions

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
    def test_a_cpu_list_as_the_kernel_shows_it_in_cpuset_cpus(self) -> None:
        self.assertEqual(cpuset_file_form("12-23"), "12-23")
        self.assertEqual(cpuset_file_form("12,13,14"), "12-14")
        self.assertEqual(cpuset_file_form("0-3,8 20-23"), "0-3,8,20-23")  # systemd's blanks too: no blank
        self.assertEqual(cpuset_file_form("junk"), "junk")  # as it is: no CPU list
        self.assertEqual(cpuset_file_form(""), "")

    def test_the_last_record_of_a_setting_in_game_intended_wins_at_its_place(self) -> None:
        one_each = "cst-game-intended 1\nprop sys a.slice CPUWeight 10 10\ncgw /cg/cpuset.cpus 1-2 1-2\n"
        self.assertEqual(
            intended_records(one_each),
            [("prop", "sys", "a.slice", "CPUWeight", "10", "10"), ("cgw", "/cg/cpuset.cpus", "1-2", "1-2")],
        )  # a file with one record per setting (every one before G1) reads as before
        text = (
            one_each + "prop sys b.slice CPUWeight 5 5\nprop usr a.slice CPUWeight 7 7\n"
            "prop sys a.slice CPUWeight 10 x\ncgw /cg/cpuset.cpus 1-2 3\n"
        )
        self.assertEqual(
            intended_records(text),
            [
                ("prop", "sys", "b.slice", "CPUWeight", "5", "5"),
                ("prop", "usr", "a.slice", "CPUWeight", "7", "7"),
                ("prop", "sys", "a.slice", "CPUWeight", "10", "x"),
                ("cgw", "/cg/cpuset.cpus", "1-2", "3"),
            ],
        )

    def test_unit_name_valid(self) -> None:
        for name in (
            "lushtext.slice",
            "background.slice",
            "run-u7.service",
            "run-r1a2.scope",
            "lush\\x2dbatch.slice",  # systemd's escape of a dash, with its backslash
            "user-lushtext.slice",
            "lushtext-cap-1234-5678.scope",  # where lushtext runs its jobs
            "a@b.service",
            "a@b@c.service",  # systemd checks only the first '@'
            "a:b_c.target",
            "a" * 249 + ".slice",  # 255 characters
        ):
            self.assertTrue(unit_name_valid(name), name)
        for name in (
            "a b.slice",
            os.fsdecode(b"x\xff.slice"),  # a byte that is not UTF-8, as glob gives it
            "caf\N{LATIN SMALL LETTER E WITH ACUTE}.slice",
            "",
            ".slice",
            "slice",
            "a.notatype",
            "a.slice.",
            "@a.service",
            "a" * 250 + ".slice",  # 256 characters
        ):
            self.assertFalse(unit_name_valid(name), repr(name))

    def test_escaped_unit_leaves_out_known_slices_and_names_that_are_no_unit(self) -> None:
        self.assertEqual(escaped_unit("_cpu.slice"), "cpu.slice")
        self.assertEqual(escaped_unit("lushtext.slice"), "lushtext.slice")
        self.assertIsNone(escaped_unit("app.slice"))
        self.assertIsNone(escaped_unit("a b.slice"))
        self.assertIsNone(escaped_unit(os.fsdecode(b"x\xff.slice")))

    def test_expand_cpus(self) -> None:
        self.assertEqual(expand_cpus("0-3,6,12-14"), ["0", "1", "2", "3", "6", "12", "13", "14"])
        self.assertEqual(expand_cpus("0-1,,5,"), ["0", "1", "5"])
        self.assertEqual(expand_cpus("x-2,7"), ["7"])  # what seq cannot count gives nothing
        self.assertEqual(expand_cpus(" +3- 5"), ["3", "4", "5"])
        # A range end of more digits than int() reads is no number either (main: ValueError).
        self.assertEqual(expand_cpus("1" * 5000 + "-1,7"), ["7"])
        self.assertEqual(expand_cpus("0-" + "1" * 5000 + ",7"), ["7"])

    def test_expand_cpus_takes_ascii_blanks_only_and_no_range_past_the_cpu_limit(self) -> None:
        # seq's blanks around a range end are spaces and tabs; a no-break space is none (before: Unicode
        # \s, and str.split() then split at it). A range end at or above CPU_INDEX_LIMIT (8192: no CPU has
        # that index) gives nothing, as a bad end does (before: every number up to it).
        nbsp = "\N{NO-BREAK SPACE}"
        for spec, want in (
            (f"0-{nbsp}3", []),
            (f"{nbsp}0-3", []),
            (f"5{nbsp},7", [f"5{nbsp}", "7"]),
            ("0-8191", [str(n) for n in range(8192)]),
            ("0-8192", []),
            ("8190-9000,3", ["3"]),
            ("0-3,6", ["0", "1", "2", "3", "6"]),
            ("1\t-\t2", ["1", "2"]),
        ):
            with self.subTest(spec=spec):
                self.assertEqual(expand_cpus(spec), want)

    def test_a_huge_range_end_gives_nothing_at_once(self) -> None:
        # Never run against the old code: it would build a list of 10**11 numbers.
        with time_bound(5):
            self.assertEqual(expand_cpus("0-99999999999,4"), ["4"])

    def test_vm_io_weight(self) -> None:
        for value, weight in (("25", "25"), ("1", "1"), ("10000", "10000"), ("0", "10"), ("10001", "10")):
            with self.subTest(value):
                self.assertEqual(vm_io_weight(value), weight)
        for value in ("idle", "", "+5", " 5", "\N{ARABIC-INDIC DIGIT THREE}", "1" * 5000):
            with self.subTest(value=value[:24]):
                self.assertEqual(vm_io_weight(value), "10")  # main: ValueError for 5000 digits

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

    def test_drifted_skips_what_it_cannot_compare(self) -> None:
        # A record cut short, a record with an empty SHOWN, a prop record of a unit systemd showed nothing
        # for and a record of another kind never drift, and the cgroup file is read only for a whole cgw
        # record that has a SHOWN.
        read: list[str] = []

        def read_file(path: str) -> str | None:
            read.append(path)
            return "0-23"

        now = {("sys", "machine.slice"): {"CPUWeight": "50"}}
        skipped = [
            ("cgw", "/cg/a/cpuset.cpus", "12-23", ""),
            ("cgw", "/cg/b/cpuset.cpus", "12-23"),
            ("prop", "sys", "machine.slice", "CPUWeight", "10"),
            ("prop", "sys", "machine.slice", "CPUWeight", "10", ""),
            ("prop", "usr", "app.slice", "MemoryLow", "16G", "16G"),
            ("epp", "0", "performance"),
        ]
        self.assertEqual(drifted(skipped, now, read_file), [])
        self.assertEqual(read, [])
        whole = ("cgw", "/cg/c/cpuset.cpus", "12-23", "12-23")
        prop = ("prop", "sys", "machine.slice", "CPUWeight", "10", "10")
        self.assertEqual(drifted([*skipped, whole, prop], now, read_file), [Drift(whole, "0-23"), Drift(prop, "50")])
        self.assertEqual(read, ["/cg/c/cpuset.cpus"])

    def test_a_last_line_cut_short(self) -> None:
        self.assertIsNone(cut_tail(b""))
        self.assertIsNone(cut_tail(b"a\n"))
        self.assertEqual(cut_tail(b"cut"), (0, b"cut"))
        self.assertEqual(cut_tail(b"a\nb"), (2, b"b"))

    def test_a_prop_record_is_matched_by_its_fields(self) -> None:
        self.assertFalse(has_prop_record("prop usr S MemoryLow existed 1", "usr", "S", "MemoryLow"))  # cut short
        self.assertTrue(has_prop_record("prop usr S MemoryLow existed 1\n", "usr", "S", "MemoryLow"))
        self.assertTrue(has_prop_record("prop usr S MemoryLow new -\nepp 0 x", "usr", "S", "MemoryLow"))
        self.assertFalse(has_prop_record("prop usr S MemoryLow new -\nprop usr T MemoryLow", "usr", "T", "MemoryLow"))
        self.assertFalse(has_prop_record("", "usr", "S", "MemoryLow"))
        # A line bash cannot read (an unclosed quote) is no record 'off' can undo, so it holds none.
        self.assertFalse(has_prop_record("prop usr S MemoryLow existed 'x\n", "usr", "S", "MemoryLow"))
        # The unit as it is, whatever printf %q made of it; another manager, unit or property is none.
        quoted = record_line("prop", "usr", "a\\x2db.scope", "MemoryLow", "new", "-")
        self.assertIn("\\\\x2d", quoted)
        self.assertTrue(has_prop_record(quoted, "usr", "a\\x2db.scope", "MemoryLow"))
        self.assertFalse(has_prop_record(quoted, "sys", "a\\x2db.scope", "MemoryLow"))
        self.assertFalse(has_prop_record("prop usr Steam.scope MemoryLow new -\n", "usr", "S", "MemoryLow"))
        self.assertFalse(has_prop_record("prop usr S IOWeight new -\n", "usr", "S", "MemoryLow"))

    def test_intended_records(self) -> None:
        text = (
            "cst-game-intended 1\n"
            "prop sys user.slice MemoryLow 16G 17179869184\n"
            "prop usr app.slice MemoryZSwapWriteback no ''\n"
            "prop xyz a.slice CPUWeight 1 1\n"
            "prop sys a.slice CPUWeight 1\n"
            "cgw $'/sys/fs/cgroup/in\\tner/cpuset.cpus' 12-23 12-23 extra\n"
            "bogus line\nprop sys 'unclosed\n"
            "prop sys machine.slice CPUWeight 10 1"
        )
        self.assertEqual(
            intended_records(text),
            [
                ("prop", "sys", "user.slice", "MemoryLow", "16G", "17179869184"),
                ("prop", "usr", "app.slice", "MemoryZSwapWriteback", "no", ""),
                ("cgw", "/sys/fs/cgroup/in\tner/cpuset.cpus", "12-23", "12-23"),
            ],
        )

    def test_drift_game_state_holds_no_record_of(self) -> None:
        prop, cgw = (
            Drift(("prop", "sys", "a.slice", "CPUWeight", "10", "10"), "50"),
            Drift(("cgw", "/f", "1", "1"), "2"),
        )
        usr = Drift(("prop", "usr", "a.slice", "CPUWeight", "idle", "idle"), "100")
        drifts = [prop, cgw, usr]
        self.assertEqual(unheld(drifts, []), drifts)
        held = [("prop", "sys", "a.slice", "CPUWeight", "existed", "50"), ("cgw", "/f", "")]
        self.assertEqual(unheld(drifts, held), [usr])  # another manager's record holds no other
        self.assertEqual(unheld(drifts, [("prop", "sys", "a.slice", "CPUWeight", "new")]), drifts)  # 5 fields
        self.assertEqual(unheld([cgw], [("cgw", "/f")]), [cgw])  # 2 fields: off undoes nothing

    def test_a_setting_holds_as_systemd_reads_it(self) -> None:
        cases = [
            # MemoryLow: by its bytes; "infinity"; a percentage cannot be judged.
            ("MemoryLow", "16G", "17179869184", True),
            ("MemoryLow", "16G", "16G", True),
            ("MemoryLow", "16G", "0", False),
            ("MemoryLow", "16G", "", False),
            ("MemoryLow", "16G", "infinity", False),
            ("MemoryLow", "infinity", "infinity", True),
            ("MemoryLow", "10%", "1717986918", None),
            ("MemoryLow", "16G", "10%", None),
            # AllowedCPUs: as a set of CPUs, commas or blanks between ranges; none at all differs.
            ("AllowedCPUs", "12-23", "12-23", True),
            ("AllowedCPUs", "12-23", "12-15 16-23", True),
            ("AllowedCPUs", "0,2", "0 2", True),
            ("AllowedCPUs", "12-23", "0-23", False),
            ("AllowedCPUs", "12-23", "", False),
            ("AllowedCPUs", "12-23", "x", None),
            ("AllowedCPUs", "", "12-23", None),
            # CPUWeight: "idle" or a whole number.
            ("CPUWeight", "idle", "idle", True),
            ("CPUWeight", "idle", "100", False),
            ("CPUWeight", "10", "10", True),
            ("CPUWeight", "10", "010", True),
            ("CPUWeight", "idle", "", False),
            ("CPUWeight", "idle", "weird", None),
            ("CPUWeight", "\N{ARABIC-INDIC DIGIT THREE}", "3", None),  # a digit of another script is no weight
            # Any other property is never judged.
            ("IOWeight", "10", "50", None),
        ]
        for prop, value, now, want in cases:
            self.assertIs(setting_holds(prop, value, now), want, (prop, value, now))

    def test_the_settings_of_a_box_that_do_not_hold_and_their_words(self) -> None:
        unset = unset_settings(
            ["CPUWeight=idle", "AllowedCPUs=12-23", "MemoryLow=10%"],
            {"CPUWeight": "100", "AllowedCPUs": "12-23", "MemoryLow": "0"},
        )
        self.assertEqual(unset, [Unset("CPUWeight", "idle", "100")])
        self.assertEqual(unset_settings(["CPUWeight=idle"], {}), [Unset("CPUWeight", "idle", "")])
        both = [Unset("MemoryLow", "16G", ""), Unset("AllowedCPUs", "0,2-3", "0-23")]
        self.assertEqual(
            set_again_text("a.scope", both, ok=True),
            (
                5,
                (
                    "a.scope: MemoryLow is '', not '17179869184'; AllowedCPUs is '0-23', not '0 2-3' "
                    "(its set failed, or something undid it): set MemoryLow=16G AllowedCPUs=0,2-3 again"
                ),
            ),
        )
        self.assertEqual(
            set_again_text("a.scope", both[:1], ok=False),
            (4, "a.scope: MemoryLow is '', not '17179869184': could not set MemoryLow=16G again"),
        )

    def test_what_the_drift_repair_compares(self) -> None:
        recs = intended_records(
            "cst-game-intended 1\n"
            "prop usr a.slice CPUWeight idle idle\n"
            "prop usr a.slice AllowedCPUs 12-23 ''\n"
            "prop sys a.slice MemoryLow 16G 17179869184\n"
            "cgw /f 12-23 12-23\n"
        )
        self.assertEqual(intended_covers(recs, "usr", "a.slice"), frozenset({"CPUWeight"}))  # an empty SHOWN: none
        self.assertEqual(intended_covers(recs, "sys", "a.slice"), frozenset({"MemoryLow"}))
        self.assertEqual(intended_covers(recs, "usr", "b.slice"), frozenset())

    def test_the_state_files_are_the_registered_ones(self) -> None:
        p = GamePaths()
        self.assertEqual(p.state, state_format(StateFormatKey.GAME_STATE).path)
        self.assertEqual(p.intended, state_format(StateFormatKey.GAME_INTENDED).path)

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

    def test_a_failed_set_property_is_logged_and_records_the_intended_value_the_tick_sets(self) -> None:
        # G1: game.intended holds what 'on' sets before its set-property, as systemd will show it, so a
        # fixed unit whose set-property failed is a drift the next tick sets (the old 'on' recorded nothing
        # for it, and nothing set it again until 'off').
        self.systemd.fail_set.add(("sys", "machine.slice"))
        out, _ = self.run_profile("on")
        self.assertIn("<4>game-profile: machine.slice: could not set AllowedCPUs=12-23 CPUWeight=10 IOWeight=10\n", out)
        intended = self.read(self.intended)
        for rec in ("AllowedCPUs 12-23 12-23", "CPUWeight 10 10", "IOWeight 10 10"):
            self.assertEqual(intended.count(f"prop sys machine.slice {rec}\n"), 1, intended)
        self.assertIn("prop sys machine.slice CPUWeight new -", self.read(self.state))
        self.systemd.fail_set.clear()
        self.write(self.manual, "")
        out, actions = self.run_profile("tick")
        self.assertEqual(
            sorted(a for a in self.sets(actions) if "machine.slice" in a),
            [
                f"{SYS} set-property --runtime machine.slice AllowedCPUs=12-23",
                f"{SYS} set-property --runtime machine.slice CPUWeight=10",
                f"{SYS} set-property --runtime machine.slice IOWeight=10",
            ],
        )
        self.assertIn("drift: machine.slice CPUWeight was '50' instead of '10'", out)
        self.assertEqual(self.now("sys", "machine.slice", "CPUWeight"), "10")
        _, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [])
        self.assertEqual(self.read(self.intended), intended)  # each repair showed what the record says
        self.run_profile("off")
        self.assertFalse(os.path.exists(self.ctrl("sys", "machine.slice", "CPUWeight")))  # 'off' undoes it

    def test_a_set_property_that_keeps_failing_is_said_once_per_boot(self) -> None:
        self.systemd.fail_set.add(("sys", "machine.slice"))
        self.run_profile("on")
        self.write(self.manual, "")
        out, actions = self.run_profile("tick")
        self.assertIn(
            "<4>game-profile: drift: machine.slice CPUWeight is '50' instead of '10': could not set it again", out
        )
        self.assertIn(f"{SYS} set-property --runtime machine.slice CPUWeight=10", actions)
        out, actions = self.run_profile("tick")
        self.assertIn(f"{SYS} set-property --runtime machine.slice CPUWeight=10", actions)  # tried again
        self.assertNotIn("could not set it again", out)  # said once per boot

    def test_a_value_systemd_shows_in_another_form_gets_a_second_record(self) -> None:
        # 16384M: the prediction is its bytes; the fake systemd shows it as given. 'on' writes the prediction,
        # then what systemd shows after the set; the last record of a setting wins, so no drift.
        settings = dataclasses.replace(self.settings, game_memory_low="16384M")
        self.run_profile("on", settings)
        intended = self.read(self.intended)
        self.assertIn(
            f"prop sys user@{UID}.service MemoryLow 16384M 17179869184\n"
            f"prop sys user@{UID}.service MemoryLow 16384M 16384M\n",
            intended,
        )
        self.assertEqual(intended.count("prop sys machine.slice CPUWeight "), 1)  # shown as predicted: one
        self.write(self.manual, "")
        out, actions = self.run_profile("tick", settings)
        self.assertEqual(self.sets(actions), [])
        self.assertNotIn("drift", out)

    def test_a_repair_that_systemd_shows_in_another_form_records_that_form_once(self) -> None:
        # The memory chain's leaf failed in 'on': game.intended holds only the prediction (bytes); the tick's
        # set shows 16384M: that form is recorded, and the next tick sets nothing.
        settings = dataclasses.replace(self.settings, game_memory_low="16384M")
        self.systemd.fail_set.add(("sys", f"user@{UID}.service"))
        self.run_profile("on", settings)
        self.systemd.fail_set.clear()
        self.write(self.manual, "")
        _, actions = self.run_profile("tick", settings)
        self.assertIn(f"{SYS} set-property --runtime user@{UID}.service MemoryLow=16384M", self.sets(actions))
        self.assertTrue(self.read(self.intended).endswith(f"prop sys user@{UID}.service MemoryLow 16384M 16384M\n"))
        out, actions = self.run_profile("tick", settings)
        self.assertEqual(self.sets(actions), [])
        self.assertNotIn("drift", out)

    def test_the_lock_keeps_actions_apart(self) -> None:
        # The real lock with a short wait (the front's 60 s is pinned in test_game_ports).
        with self.ports(lock=GameRunLock(self.paths.run, self.paths.lock, timeout=0.2)):
            p, out = self.profile()
        self.assertEqual(p.locked("on"), 0)
        self.assertTrue(os.path.exists(self.state))
        self.assertEqual(os.stat(self.paths.lock).st_mode & 0o777, 0o644)
        from cst_core.adapters.locks import take_lock

        fd = take_lock(self.paths.lock, wait=False)
        assert fd is not None
        try:
            self.assertEqual(p.locked("off"), 1)
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

    def test_a_read_back_that_fails_after_a_set_keeps_the_value_as_systemd_will_show_it(self) -> None:
        # 'on' sets machine.slice's CPUWeight, then `systemctl show -P` cannot answer for it (a busy manager):
        # game.intended keeps the value as systemd will show it, so the tick still sees the drift a daemon-reload
        # makes later. The old setp added a record with an empty form after it, which won, and no drift of that
        # setting was ever seen.
        self.systemd.fail_show.add(("sys", "machine.slice", "CPUWeight"))
        self.run_profile("on")
        self.systemd.fail_show.clear()
        recs = [r for r in intended_records(self.read(self.intended)) if r[2:4] == ("machine.slice", "CPUWeight")]
        self.assertEqual(recs, [("prop", "sys", "machine.slice", "CPUWeight", "10", "10")])
        self.write(self.manual, "")
        self.set_now("sys", "machine.slice", CPUWeight="50")
        _, actions = self.run_profile("tick")
        self.assertIn(f"{SYS} set-property --runtime machine.slice CPUWeight=10", actions)
        self.assertEqual(self.now("sys", "machine.slice", "CPUWeight"), "10")

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

    def lose_machine_slice_records(self) -> None:
        """After 'on': machine.slice's records gone from game.state and its overrides removed (game.state
        rewritten by hand, or written by a version that recorded it elsewhere): its values are back to
        the unit's own, which game.intended calls drift."""
        self.run_profile("on")
        self.write(self.manual, "")
        kept = [x for x in self.read(self.state).split("\n") if x and " machine.slice " not in x]
        self.write(self.state, "".join(x + "\n" for x in kept))
        for prop in ("AllowedCPUs", "CPUWeight", "IOWeight"):
            os.remove(self.ctrl("sys", "machine.slice", prop))
        self.set_now("sys", "machine.slice", CPUWeight="50")

    def test_a_setting_game_state_holds_no_record_of_is_not_set_again(self) -> None:
        self.lose_machine_slice_records()
        self.set_now("sys", "user.slice", MemoryLow="0")  # held: repaired as before
        out, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [f"{SYS} set-property --runtime user.slice MemoryLow=16G"])
        self.assertIn(
            "<5>game-profile: drift not repaired: machine.slice CPUWeight: game.state holds no record of it, so"
            " 'off' could not undo it\n",
            out,
        )
        self.assertFalse(os.path.exists(self.ctrl("sys", "machine.slice", "CPUWeight")))
        out, actions = self.run_profile("tick")
        self.assertEqual((out, self.sets(actions)), ("", []))  # said once per boot
        report, _ = self.drift_report()
        self.assertIn(
            "  DRIFT: machine.slice CPUWeight is '50', the profile set '10' (not set again: game.state holds no"
            " record of it)\n",
            report,
        )
        self.assertIn("(the watchdog does not set these again; cst cpu game off && cst cpu game on", report)
        out, actions = self.run_profile("off")
        self.assertEqual([a for a in actions if "machine.slice" in a], [])
        self.assertFalse(os.path.exists(self.paths.drift_warned))

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

    def test_a_short_record_of_game_intended_asks_systemd_nothing(self) -> None:
        # game.intended read as cst doctor reads it (intended_records): a prop line of five fields is no
        # drift record, so no systemctl show asks about it.
        self.write(self.state, "prop sys builds.slice CPUWeight new -\n")
        self.write(self.intended, "cst-game-intended 1\nprop sys builds.slice CPUWeight idle\n")
        self.write(self.manual, "")
        out, actions = self.run_profile("tick")
        self.assertEqual(out, "")
        self.assertEqual([a for a in actions if " show -p " in a or "set-property" in a], [])
        report, actions = self.drift_report()
        self.assertEqual([a for a in actions if " show " in a], [])
        self.assertEqual(report, "  drift: none (every setting the profile made still holds)\n")

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

    def test_on_starts_a_session_without_the_drift_mark(self) -> None:
        self.write(self.paths.drift_warned, "drift not repaired: x")  # left by an earlier session
        self.run_profile("on")
        self.assertFalse(os.path.exists(self.paths.drift_warned))

    def test_on_writes_the_format_line_first(self) -> None:
        self.run_profile("on")
        first, _, rest = self.read(self.intended).partition("\n")
        self.assertEqual(first, "cst-game-intended 1")
        self.assertTrue(rest.startswith(f"prop sys user@{UID}.service MemoryLow 16G "), rest)
        self.assertEqual(records(first + "\n"), [("cst-game-intended", "1")])
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
        self.assertEqual(self.run_profile("tick")[0], "")  # said once per boot
        report, _ = self.drift_report()
        self.assertEqual(report, f"  drift: not checked ({why})\n")
        self.write(self.intended, "cst-game-intended x\nprop sys machine.slice CPUWeight 10 10\n")
        report, _ = self.drift_report()
        self.assertEqual(report, f"  drift: not checked ({self.intended}: its first line names no format version)\n")
        # off still undoes everything: it reads game.state only.
        _, actions = self.run_profile("off")
        self.assertIn(f"{SYS} daemon-reload", actions)
        self.assertFalse(os.path.exists(self.intended))

    def test_the_newer_format_is_said_once_per_boot_and_again_when_it_changes(self) -> None:
        self.run_profile("on")
        self.write(self.manual, "")
        intended = self.read(self.intended).replace("cst-game-intended 1\n", "cst-game-intended 2\n")
        self.write(self.intended, intended)
        said = "<4>game-profile: drift not repaired: "
        self.assertEqual(self.run_profile("tick")[0].count(said), 1)
        self.assertEqual(self.run_profile("tick")[0], "")
        self.write(self.intended, intended.replace("cst-game-intended 2\n", "cst-game-intended 3\n"))
        self.assertIn(f"{said}{self.intended} is format 3 ", self.run_profile("tick")[0])
        self.assertEqual(self.run_profile("tick")[0], "")
        # Readable again: a newer format later is said again.
        self.write(self.intended, intended.replace("cst-game-intended 2\n", "cst-game-intended 1\n"))
        self.run_profile("tick")
        self.write(self.intended, intended.replace("cst-game-intended 2\n", "cst-game-intended 3\n"))
        self.assertIn(said, self.run_profile("tick")[0])
        # A new session says it again.
        self.run_profile("off")
        self.run_profile("on")
        self.write(self.manual, "")
        self.write(self.intended, intended.replace("cst-game-intended 2\n", "cst-game-intended 3\n"))
        self.assertIn(said, self.run_profile("tick")[0])
        self.run_profile("off")
        self.assertFalse(os.path.exists(self.paths.drift_warned))


class StateSnapshotTest(GameProfileCase):
    """'state' takes no lock: it reads game.state and game.intended once, and every line it prints
    judges that one snapshot, also when 'off' (or a tick) changes the files meanwhile."""

    def test_state_reads_each_file_once_and_judges_that_snapshot(self) -> None:
        self.run_profile("on")
        self.write(self.manual, "")
        os.makedirs(os.path.join(self.ucg, "e2e.slice"))
        self.run_profile("tick")  # e2e.slice next to the game: idle, recorded in game.state
        seen: list[str] = []
        state, intended = self.state, self.intended

        class ThenOff(GameStateFiles):
            """Each read of game.state or game.intended noted, and the file gone after it ('off' ended meanwhile:
            the next read would find nothing)."""

            def _then_off(self, path: str) -> None:
                if path in (state, intended):
                    seen.append(path)
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(path)

            @override
            def snapshot(self, path: str) -> str | None:
                text = super().snapshot(path)
                self._then_off(path)
                return text

            @override
            def read_text(self, path: str) -> str | None:
                text = super().read_text(path)
                self._then_off(path)
                return text

            @override
            def read_bytes(self, path: str) -> bytes | None:
                data = super().read_bytes(path)
                self._then_off(path)
                return data

        with self.ports(state=ThenOff()):
            out, _ = self.run_profile("state")
        self.assertEqual(sorted(seen), sorted([self.state, self.intended]))
        self.assertIn("game profile: ON (by hand)\n", out)
        self.assertIn("  other boxes     idle: e2e.slice\n", out)
        self.assertIn("  drift: none (every setting the profile made still holds)\n", out)

    def test_the_by_hand_mark_is_read_once(self) -> None:
        # The header ("ON (by hand)") and the drift line's fix command judge one reading of the mark.
        self.run_profile("on")
        self.write(self.manual, "")
        asked: list[str] = []
        manual = self.manual

        class Asked(GameStateFiles):
            @override
            def exists(self, path: str) -> bool:
                if path == manual:
                    asked.append(path)
                return super().exists(path)

        with self.ports(state=Asked()):
            out, _ = self.run_profile("state")
        self.assertIn("game profile: ON (by hand)\n", out)
        self.assertEqual(asked, [self.manual])

    def test_a_game_state_there_but_unreadable_is_still_on(self) -> None:
        # A game.state there that cannot be read (a folder here) is on, as os.path.exists said before the
        # snapshot, with no records.
        self.run_profile("on")
        for path in (self.state, self.intended):
            os.unlink(path)
            os.mkdir(path)
        out, _ = self.run_profile("state")
        self.assertIn("game profile: ON\n", out)  # before the fix: "game profile: off"
        self.assertIn("  drift: none", out)  # an unreadable game.intended holds no record, as before the snapshot

    def test_without_game_intended_only_game_state_is_judged_and_drift_is_not_checked(self) -> None:
        # Switched on by an older version (no game.intended), not by hand: game.state's unreadable lines are
        # named, game.intended has none, and the drift line says how to switch the profile on again.
        self.run_profile("on")
        os.unlink(self.intended)
        with open(self.state, "a") as f:
            f.write("epp 3\n")
        number = len(self.read(self.state).split("\n")) - 1
        out, _ = self.run_profile("state")
        self.assertIn("game profile: ON\n", out)
        self.assertEqual(
            [x for x in out.split("\n") if "unreadable:" in x],
            [f"  unreadable: {self.state} line {number} (skipped): epp 3"],
        )
        self.assertTrue(
            out.endswith(
                "  drift: not checked (switched on by an older version, which kept no record of its values;\n"
                "         sudo systemctl restart cpu-game.service switches it off and on again with one)\n"
            ),
            out,
        )


class ReadSnapshotTest(unittest.TestCase):
    """read_snapshot looks once: None for a file that is not there, "" for one there that it cannot read
    (still on), else its text. What changes after that look does not change its answer."""

    @override
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "game.state")

    def test_not_there_unreadable_and_read(self) -> None:
        self.assertIsNone(read_snapshot(self.path))
        os.mkdir(self.path)
        self.assertEqual(read_snapshot(self.path), "")
        os.rmdir(self.path)
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("x\n")
        self.assertEqual(read_snapshot(self.path), "x\n")

    def test_a_file_there_that_goes_after_the_look_was_there(self) -> None:
        # 'off' ended right after the read: the unreadable file it saw still counts as there.
        os.mkdir(self.path)
        real = cst_files.read_text

        def then_gone(path: str, *, unreadable: str | None = None) -> str | None:
            text = real(path, unreadable=unreadable)
            os.rmdir(path)
            return text

        with unittest.mock.patch("cpu.game.adapters.host.read_text", side_effect=then_gone):
            self.assertEqual(read_snapshot(self.path), "")  # before the fix: None (off)

    def test_a_file_that_comes_after_the_look_was_not_there(self) -> None:
        # 'on' began right after the read: the file it did not see is still not there.
        real = cst_files.read_text

        def then_made(path: str, *, unreadable: str | None = None) -> str | None:
            text = real(path, unreadable=unreadable)
            os.mkdir(path)
            return text

        with unittest.mock.patch("cpu.game.adapters.host.read_text", side_effect=then_made):
            self.assertIsNone(read_snapshot(self.path))  # before the fix: "" (on)


class UnreadableRecordTest(GameProfileCase):
    """A line of game.state or game.intended that holds no record (cut short when 'on' died while it
    wrote it, an unclosed quote): 'off' names it in the journal, skips it and undoes every other
    record; 'state' names it. bash's eval stopped 'off' there (exit 1, game.state kept)."""

    def test_off_skips_and_names_a_record_it_cannot_read_and_undoes_the_rest(self) -> None:
        self.run_profile("on")
        self.assertTrue(os.path.exists(self.ctrl("sys", "machine.slice", "CPUWeight")))
        with open(self.state, "a") as f:
            f.write("prop sys 'user.slice MemoryLow new -\nprop sys machine.slice IOWeight existed\n")
        n = len(self.read(self.state).split("\n")) - 1
        out, _ = self.run_profile("off")
        self.assertEqual(
            out.split("\n")[:2],
            [
                (
                    f"<4>game-profile: {self.state} line {n - 1}: no record 'off' can read, skipped:"
                    " prop sys 'user.slice MemoryLow new -"
                ),
                (
                    f"<4>game-profile: {self.state} line {n}: no record 'off' can read, skipped:"
                    " prop sys machine.slice IOWeight existed"
                ),
            ],
        )
        self.assertIn("<5>game-profile: off: previous CPU", out)
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.ctrl("sys", "machine.slice", "CPUWeight")))  # the rest undone

    def test_a_last_line_cut_short_is_named_and_never_undone(self) -> None:
        # 'on' writes each record before the change it describes: a last line with no newline at its
        # end was being written when 'on' died, so its change was never made, and its value may be cut
        # short (an old IOWeight "1" of "100"). 'off' names it and leaves the property alone.
        self.run_profile("on")
        with open(self.state, "a") as f:
            f.write("prop sys machine.slice IOWeight existed 1")
        n = len(self.read(self.state).split("\n"))
        out, actions = self.run_profile("off")
        self.assertIn(
            f"<4>game-profile: {self.state} line {n}: no record 'off' can read, skipped:"
            " prop sys machine.slice IOWeight existed 1\n",
            out,
        )
        self.assertEqual([a for a in actions if "IOWeight=" in a], [])
        self.assertFalse(os.path.exists(self.state))
        # The same line with its newline is a record, undone as before.
        self.assertEqual(records("epp 0 performance\nepp 1 balance"), [("epp", "0", "performance")])
        self.assertEqual(records("epp 0 performance\nepp 1 balance\n")[1], ("epp", "1", "balance"))

    CUT = "prop sys machine.slice IOWeight existed 1"

    def cut_and_box(self, *paths: str) -> None:
        """After 'on': a record cut short at the end of each path, and a new box next to the game."""
        self.run_profile("on")
        self.write(self.manual, "")
        for path in paths:
            with open(path, "a") as f:
                f.write(self.CUT)
        os.makedirs(os.path.join(self.ucg, "e2e.slice"))

    def test_a_record_after_a_line_cut_short_removes_that_line_first(self) -> None:
        # Glued onto the cut line, the box's record would make it a whole record: 'off' would set
        # IOWeight=1 (or worse), and the box's override would stay.
        self.cut_and_box(self.state, self.intended)
        n_state, n_intended = (len(self.read(p).split("\n")) for p in (self.state, self.intended))
        out, _ = self.run_profile("tick")
        self.assertIn(
            f"<4>game-profile: {self.state} line {n_state}: a record cut short (its change was never made),"
            f" removed before the next record: {self.CUT}\n",
            out,
        )
        self.assertIn(
            f"<4>game-profile: {self.intended} line {n_intended}: a record cut short (never read by the drift"
            f" repair), removed before the next record: {self.CUT}\n",
            out,
        )
        state, intended = self.read(self.state), self.read(self.intended)
        self.assertNotIn(self.CUT, state + intended)
        self.assertTrue(state.endswith("\nprop usr e2e.slice CPUWeight new -\n"), state)
        self.assertTrue(intended.endswith("\nprop usr e2e.slice CPUWeight idle idle\n"), intended)
        self.assertTrue(os.path.exists(self.ctrl("usr", "e2e.slice", "CPUWeight")))
        out, actions = self.run_profile("off")
        self.assertEqual([a for a in actions if "IOWeight=1" in a], [])
        self.assertFalse(os.path.exists(self.ctrl("usr", "e2e.slice", "CPUWeight")))

    def test_a_line_cut_short_that_cannot_be_removed_stops_the_change(self) -> None:
        self.cut_and_box(self.state)
        err = io.StringIO()

        class ReadOnly(GameStateFiles):
            @override
            def truncate(self, path: str, size: int) -> None:
                raise OSError(errno.EROFS, "Read-only file system")

        with self.ports(state=ReadOnly()), contextlib.redirect_stderr(err):
            out, actions = self.run_profile("tick")
        self.assertIn(f"{self.state}: Read-only file system\n", err.getvalue())
        self.assertIn(f"e2e.slice: CPUWeight=idle: not done, its record could not be written to {self.state}\n", out)
        self.assertEqual([a for a in self.sets(actions) if "e2e.slice" in a], [])
        self.assertTrue(self.read(self.state).endswith(f"\n{self.CUT}"))

    def tick_after_a_cut_record(self, record: str, mgr: str, unit: str, setting: str) -> None:
        """After 'on': record cut short at the end of game.state (on died while it wrote it, before it
        set the box). The tick sets the box (the cut line is no record of a box handled), removes the
        cut line first and writes the whole record; 'off' undoes it."""
        self.write(self.manual, "")
        with open(self.state, "a") as f:
            f.write(record)
        n = len(self.read(self.state).split("\n"))
        out, actions = self.run_profile("tick")
        prefix = USR if mgr == "usr" else SYS
        self.assertIn(f"{prefix} set-property --runtime {unit} {setting}", actions)
        self.assertIn(f"{self.state} line {n}: a record cut short (its change was never made)", out)
        state = self.read(self.state)
        self.assertTrue(state.endswith(f"\n{record}\n"), state)
        self.assertEqual(state.count(record), 1)
        override = self.ctrl(mgr, unit, setting.partition("=")[0])
        self.assertTrue(os.path.exists(override))
        _, actions = self.run_profile("tick")  # handled now: nothing more
        self.assertEqual([a for a in self.sets(actions) if f" {unit} " in a], [])
        self.run_profile("off")
        self.assertFalse(os.path.exists(override))

    def test_a_box_next_to_the_game_whose_record_was_cut_short_is_set(self) -> None:
        self.run_profile("on")
        os.makedirs(os.path.join(self.ucg, "e2e.slice"))
        self.tick_after_a_cut_record("prop usr e2e.slice CPUWeight new -", "usr", "e2e.slice", "CPUWeight=idle")

    def second_record_cut(self, *, steam: bool) -> None:
        """cpuset delegated: a box next to the game gets two settings, which setp records one after the
        other before it sets either. 'on' (or a tick) died while it wrote the second record (AllowedCPUs):
        the first (CPUWeight) is a whole line, but neither change was made. The next tick sets the box
        (steam: a new Steam scope is recorded first in that tick, which removes the cut line before the
        box is looked at); a second tick sets nothing more, and 'off' removes both overrides."""
        self.write(os.path.join(self.ucg, "cgroup.subtree_control"), "cpuset cpu memory pids\n")
        self.run_profile("on")
        self.write(self.manual, "")
        os.makedirs(os.path.join(self.ucg, "e2e.slice"))
        if steam:
            self.systemd.units["usr"] = ["app-flatpak-com.valvesoftware.Steam-7.scope"]
        with open(self.state, "a") as f:
            f.write("prop usr e2e.slice CPUWeight new -\nprop usr e2e.slice AllowedCPUs new")
        _, actions = self.run_profile("tick")
        self.assertIn(f"{USR} set-property --runtime e2e.slice CPUWeight=idle AllowedCPUs=12-23", actions)
        overrides = [self.ctrl("usr", "e2e.slice", prop) for prop in ("CPUWeight", "AllowedCPUs")]
        for path in overrides:
            self.assertTrue(os.path.exists(path), path)
        self.assertTrue(self.read(self.state).endswith("\nprop usr e2e.slice AllowedCPUs new -\n"))
        _, actions = self.run_profile("tick")  # handled now: nothing more
        self.assertEqual([a for a in self.sets(actions) if " e2e.slice " in a], [])
        self.run_profile("off")
        for path in overrides:
            self.assertFalse(os.path.exists(path), path)

    def test_a_box_whose_second_record_was_cut_short_is_set(self) -> None:
        self.second_record_cut(steam=False)

    def test_a_box_whose_second_record_was_cut_short_is_set_after_a_new_steam_scope(self) -> None:
        self.second_record_cut(steam=True)

    def test_a_steam_scope_whose_record_was_cut_short_is_protected(self) -> None:
        steam = "app-flatpak-com.valvesoftware.Steam-7.scope"
        self.run_profile("on")
        self.systemd.units["usr"] = [steam]  # started after 'on'
        self.tick_after_a_cut_record(f"prop usr {steam} MemoryLow new -", "usr", steam, "MemoryLow=16G")

    def test_a_docker_container_whose_record_was_cut_short_is_pinned(self) -> None:
        scope = "docker-abc.scope"
        self.run_profile("on")
        self.systemd.units["sys"] = [scope]  # started after 'on', outside builds.slice
        self.tick_after_a_cut_record(f"prop sys {scope} AllowedCPUs new -", "sys", scope, "AllowedCPUs=12-23")

    def test_a_steam_scope_whose_record_bash_cannot_read_is_protected(self) -> None:
        # A record of the scope with an unclosed quote: 'off' cannot read it, so the scope is not
        # handled; the tick sets MemoryLow again and writes a record 'off' can undo.
        steam = "app-flatpak-com.valvesoftware.Steam-7.scope"
        self.run_profile("on")
        self.write(self.manual, "")
        self.systemd.units["usr"] = [steam]
        with open(self.state, "a") as f:
            f.write(f"prop usr {steam} MemoryLow existed 'x\n")
        _, actions = self.run_profile("tick")
        self.assertIn(f"{USR} set-property --runtime {steam} MemoryLow=16G", actions)
        self.assertTrue(self.read(self.state).endswith(f"\nprop usr {steam} MemoryLow new -\n"))
        self.run_profile("off")
        self.assertFalse(os.path.exists(self.ctrl("usr", steam, "MemoryLow")))

    def test_a_steam_scope_whose_name_printf_quotes_is_handled_once(self) -> None:
        # systemd escapes a dash of a name part as \x2d; printf %q writes that backslash quoted, and
        # the scope is still the same one at the next tick: set once, not at every tick.
        steam = "app-flatpak-com.valvesoftware.Steam-a\\x2db.scope"
        self.run_profile("on")
        self.write(self.manual, "")
        self.systemd.units["usr"] = [steam]
        _, actions = self.run_profile("tick")
        self.assertIn(f"{USR} set-property --runtime {steam} MemoryLow=16G", actions)
        _, actions = self.run_profile("tick")
        self.assertEqual([a for a in self.sets(actions) if steam in a], [])
        self.assertEqual(
            [r for r in records(self.read(self.state)) if r[2:4] == (steam, "MemoryLow")],
            [("prop", "usr", steam, "MemoryLow", "new", "-")],
        )

    def test_state_names_such_lines_of_both_files(self) -> None:
        self.run_profile("on")
        self.write(self.manual, "")
        with open(self.state, "a") as f:
            f.write("epp 3\n\n")
        with open(self.intended, "a") as f:
            f.write("prop sys machine.slice\ncgw $(x) 1 1\nfuture-kind a\n")
        state_lines = len(self.read(self.state).split("\n")) - 2
        intended_lines = len(self.read(self.intended).split("\n")) - 1
        out, _ = self.run_profile("state")
        self.assertIn(f"\n  unreadable: {self.state} line {state_lines} (skipped): epp 3\n", out)
        self.assertIn(
            f"\n  unreadable: {self.intended} line {intended_lines - 2} (skipped): prop sys machine.slice\n", out
        )
        self.assertIn(f"\n  unreadable: {self.intended} line {intended_lines - 1} (skipped): cgw $(x) 1 1\n", out)
        self.assertEqual(out.count("unreadable:"), 3)  # a blank line and a kind it does not know are none
        self.assertIn("  drift: none", out)  # the readable records are still compared

    def test_state_lists_the_idle_boxes_around_them_and_judges_no_newer_format(self) -> None:
        self.run_profile("on")
        self.write(self.manual, "")
        with open(self.state, "a") as f:
            f.write("prop usr 'lushtext.slice CPUWeight new -\nprop usr steam-x.slice CPUWeight new -\n")
        self.write(self.intended, "cst-game-intended 2\nprop sys machine.slice\n")
        out, _ = self.run_profile("state")
        self.assertIn("\n  other boxes     idle: steam-x.slice\n", out)  # bash's eval printed nothing here
        self.assertEqual(
            [x for x in out.split("\n") if "unreadable:" in x],
            [
                (
                    f"  unreadable: {self.state} line {len(self.read(self.state).split(chr(10))) - 2} (skipped):"
                    " prop usr 'lushtext.slice CPUWeight new -"
                )
            ],
        )
        self.assertIn("drift: not checked", out)

    def test_tick_says_nothing_about_them(self) -> None:
        self.run_profile("on")
        self.write(self.manual, "")
        with open(self.intended, "a") as f:
            f.write("prop sys machine.slice\n")
        out, _ = self.run_profile("tick")
        self.assertNotIn("unreadable", out)


class _OnDiedError(Exception):
    """'on' stopped halfway (killed, or a crash) in a test."""


class OffWithoutTargetUserTest(GameProfileCase):
    """'off' with no TARGET_USER (its line became one that sets nothing, `TARGET_USER="$(id -un 1000)"`,
    or it is gone) after 'on' ran for a user: the user manager's side is undone for the user game.state
    names (its user@UID.service record), not for an empty name."""

    def off_without_user(self, conf: str, name_of: dict[str, str]) -> tuple[int, str]:
        self.write(self.paths.conf, conf)
        settings, problems = read_settings(self.paths.conf)
        self.assertEqual(settings.target_user, "")
        out = io.StringIO()
        p = GameProfile(
            self.paths,
            settings,
            self.systemd,
            uid_of=lambda u: UID if u == USER else "",
            name_of=lambda uid: name_of.get(uid, ""),
            problems=problems,
            out=out,
        )
        self.systemd.actions = []
        return p.off(), out.getvalue()

    def test_off_undoes_the_user_side_for_the_user_on_acted_for(self) -> None:
        self.run_profile("on")
        user_override = self.ctrl("usr", "user.slice", "CPUWeight")
        self.assertTrue(os.path.exists(user_override))
        rc, out = self.off_without_user('TARGET_USER="$(id -un 1000)"\n', {UID: USER})
        self.assertEqual(rc, 0)
        self.assertNotIn("systemctl --user -M @ daemon-reload", self.systemd.actions)
        self.assertIn(f"{USR} daemon-reload", self.systemd.actions)
        self.assertFalse(os.path.exists(user_override))
        self.assertFalse(os.path.exists(self.state))
        self.assertIn(
            f"<5>game-profile: TARGET_USER not set in {self.paths.conf}: undoing for {USER} (uid {UID}), the user"
            " 'on' acted for\n",
            out,
        )

    def test_off_acts_for_the_user_on_acted_for_when_target_user_names_another_one(self) -> None:
        self.run_profile("on")
        user_override = self.ctrl("usr", "user.slice", "CPUWeight")
        self.assertTrue(os.path.exists(user_override))
        self.write(self.paths.conf, "TARGET_USER='other'\n")  # changed while the profile was on
        settings, problems = read_settings(self.paths.conf)
        out = io.StringIO()
        p = GameProfile(
            self.paths,
            settings,
            self.systemd,
            uid_of={USER: UID, "other": "424242"}.__getitem__,
            name_of={UID: USER, "424242": "other"}.__getitem__,
            problems=problems,
            out=out,
        )
        self.systemd.actions = []
        self.assertEqual(p.off(), 0)
        self.assertFalse(os.path.exists(user_override))
        self.assertIn(f"{USR} daemon-reload", self.systemd.actions)
        self.assertEqual([a for a in self.systemd.actions if "-M other@" in a], [])
        self.assertIn(
            f"<5>game-profile: TARGET_USER in {self.paths.conf} is other: undoing for {USER} (uid {UID}), the user"
            " 'on' acted for\n",
            out.getvalue(),
        )
        # The same user under TARGET_USER: nothing to say, as before.
        self.run_profile("on")
        out, actions = self.run_profile("off")
        self.assertNotIn("undoing for", out)
        self.assertIn(f"{USR} daemon-reload", actions)

    def test_tick_and_state_act_for_the_user_on_acted_for_when_target_user_names_another_one(self) -> None:
        self.run_profile("on")
        self.write(self.manual, "")
        self.set_now("usr", "user.slice", CPUWeight="30")  # undone meanwhile (a daemon-reload)
        self.systemd.units["usr"] = ["app-flatpak-com.valvesoftware.Steam-7.scope"]  # a game started after 'on'
        steam_override = self.ctrl("usr", "app-flatpak-com.valvesoftware.Steam-7.scope", "MemoryLow")

        def other_profile() -> tuple[GameProfile, io.StringIO]:
            out = io.StringIO()
            p = GameProfile(
                self.paths,
                GameSettings(target_user="other"),  # changed while the profile was on
                self.systemd,
                uid_of={USER: UID, "other": "424242"}.__getitem__,
                name_of={UID: USER, "424242": "other"}.__getitem__,
                out=out,
            )
            self.systemd.actions = []
            return p, out

        p, out = other_profile()
        self.assertEqual(p.tick(), 0)
        # Everything in the user manager for the user 'off' undoes for, nothing in other's.
        self.assertEqual([a for a in self.systemd.actions if "-M other@" in a], [])
        self.assertIn(f"{USR} set-property --runtime user.slice CPUWeight=idle", self.systemd.actions)
        self.assertIn(
            f"{USR} set-property --runtime app-flatpak-com.valvesoftware.Steam-7.scope MemoryLow=16G",
            self.systemd.actions,
        )
        self.assertIn("drift: user.slice CPUWeight was '30' instead of 'idle'", out.getvalue())
        self.assertNotIn("undoing for", out.getvalue())  # the tick switches quietly
        self.assertTrue(os.path.exists(steam_override))
        p, out = other_profile()
        self.assertEqual(p.state(), 0)
        self.assertEqual([a for a in self.systemd.actions if "-M other@" in a], [])
        self.assertIn("  toolboxes       weight=idle\n", out.getvalue())
        self.assertIn("  drift: none (every setting the profile made still holds)\n", out.getvalue())
        p, out = other_profile()
        self.assertEqual(p.off(), 0)
        self.assertFalse(os.path.exists(steam_override))  # what the tick set, 'off' undid
        self.assertEqual([a for a in self.systemd.actions if "-M other@" in a], [])

    def test_tick_says_nothing_about_a_target_user_that_names_no_account(self) -> None:
        # TARGET_USER changed to a name no account has while the profile was on: the tick (every 30 s)
        # acts for the user 'on' acted for, and the uid lookup's "no such user" stays out of the journal.
        self.run_profile("on")
        self.write(self.manual, "")
        nobody = "cst-test-no-such-user"

        def uid_of(user: str) -> str:
            return UID if user == USER else user_id(user)  # the real lookup (and its message) for the other

        for _ in range(2):
            out, err = io.StringIO(), io.StringIO()
            p = GameProfile(
                self.paths,
                GameSettings(target_user=nobody),
                self.systemd,
                uid_of=uid_of,
                name_of={UID: USER}.__getitem__,
                out=out,
            )
            self.systemd.actions = []
            with contextlib.redirect_stderr(err):
                self.assertEqual(p.tick(), 0)
            self.assertEqual(err.getvalue(), "")
            self.assertEqual([a for a in self.systemd.actions if f"-M {nobody}@" in a], [])
        # 'off' still says it (once, when it runs).
        err = io.StringIO()
        p = GameProfile(
            self.paths,
            GameSettings(target_user=nobody),
            self.systemd,
            uid_of=uid_of,
            name_of={UID: USER}.__getitem__,
            out=io.StringIO(),
        )
        with contextlib.redirect_stderr(err):
            self.assertEqual(p.off(), 0)
        self.assertEqual(err.getvalue(), f"id: '{nobody}': no such user\n")

    STEAM = "app-flatpak-com.valvesoftware.Steam-7.scope"

    def quiet_ticks(self, target: str) -> None:
        """Two ticks for target with the real uid lookup and no account for the recorded uid: each ends
        in success and prints nothing on stderr. Neither changes the user manager of a user with no
        account (a Steam scope started after 'on' gets no setting and no record); the journal says why,
        once per boot."""
        self.systemd.units["usr"] = [self.STEAM]
        said: list[str] = []
        for _ in range(2):
            out, err = io.StringIO(), io.StringIO()
            p = GameProfile(
                self.paths, GameSettings(target_user=target), self.systemd, name_of=lambda _uid: "", out=out
            )
            self.systemd.actions = []
            with contextlib.redirect_stderr(err):
                self.assertEqual(p.tick(), 0)
            self.assertEqual(err.getvalue(), "")
            self.assertEqual([a for a in self.sets(self.systemd.actions) if "--user" in a], [])
            said.append(out.getvalue())
        self.assertNotIn(f" {self.STEAM} ", self.read(self.state))
        self.assertIn("<4>game-profile: profile on, its user manager's side not handled: TARGET_USER ", said[0])
        self.assertNotIn("not handled", said[1])  # said once per boot

    def off_err(self, target: str) -> str:
        """'off' for target with the real uid lookup and no account for the recorded uid: its stderr."""
        err = io.StringIO()
        p = GameProfile(
            self.paths, GameSettings(target_user=target), self.systemd, name_of=lambda _uid: "", out=io.StringIO()
        )
        with contextlib.redirect_stderr(err):
            self.assertEqual(p.off(), 0)
        self.assertFalse(os.path.exists(self.state))
        return err.getvalue()

    def test_tick_says_nothing_when_the_account_of_the_recorded_uid_is_gone(self) -> None:
        self.run_profile("on")
        self.write(self.manual, "")
        self.quiet_ticks("cst-test-no-such-user")

    def test_tick_says_nothing_with_target_user_empty_and_the_account_gone(self) -> None:
        self.run_profile("on")
        self.write(self.manual, "")
        self.quiet_ticks("")

    def test_off_says_no_such_user_once(self) -> None:
        self.run_profile("on")
        self.assertGreater(self.read(self.state).count("\nprop usr "), 1)  # each one asks for the uid
        self.assertEqual(self.off_err("cst-test-no-such-user"), "id: 'cst-test-no-such-user': no such user\n")

    def test_tick_and_off_after_an_older_on_for_a_name_without_account(self) -> None:
        # An 'on' of an older version switched on for a TARGET_USER that names no account: its records
        # name user@.service (no uid), and its user manager's ones need the uid of that name.
        self.write(
            self.state,
            "prop sys user@.service MemoryLow new -\n"
            "prop sys user-.slice MemoryLow new -\n"
            "prop sys user.slice MemoryLow new -\n"
            "prop usr user.slice CPUWeight new -\n"
            "prop usr app.slice MemoryLow new -\n",
        )
        self.write(self.manual, "")
        self.quiet_ticks("cst-test-no-such-user")
        self.assertEqual(self.off_err("cst-test-no-such-user"), "id: 'cst-test-no-such-user': no such user\n")

    def test_on_records_the_user_first(self) -> None:
        self.run_profile("on")
        self.assertTrue(self.read(self.state).startswith(f"prop sys user@{UID}.service MemoryLow new -\n"))

    def test_off_after_on_died_at_the_user_manager_acts_for_the_user_on_acted_for(self) -> None:
        # 'on' dies at its first call to the user manager (its user.slice): game.state names the uid already.
        systemd = self.systemd

        class Dying:
            """The fake systemd, and 'on' dies right after the first set-property of the user manager."""

            def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
                res = systemd.run(argv, **kw)
                if list(argv[:2]) == ["systemctl", "--user"] and "set-property" in argv:
                    raise _OnDiedError
                return res

        p = GameProfile(self.paths, self.settings, Dying(), uid_of=lambda _u: UID, out=io.StringIO())
        with self.assertRaises(_OnDiedError):
            p.on()
        user_override = self.ctrl("usr", "user.slice", "CPUWeight")
        self.assertTrue(os.path.exists(user_override))
        self.assertTrue(self.read(self.state).endswith("\nprop usr user.slice CPUWeight new -\n"))
        out = io.StringIO()
        p = GameProfile(
            self.paths,
            GameSettings(target_user="other"),
            self.systemd,
            uid_of={USER: UID, "other": "424242"}.__getitem__,
            name_of={UID: USER, "424242": "other"}.__getitem__,
            out=out,
        )
        self.systemd.actions = []
        self.assertEqual(p.off(), 0)
        self.assertFalse(os.path.exists(user_override))
        self.assertIn(f"{USR} daemon-reload", self.systemd.actions)
        self.assertEqual([a for a in self.systemd.actions if "-M other@" in a], [])
        self.assertIn(f"undoing for {USER} (uid {UID})", out.getvalue())

    def test_the_user_name_of_a_uid(self) -> None:
        import pwd

        self.assertEqual(user_name(str(os.getuid())), pwd.getpwuid(os.getuid()).pw_name)
        self.assertEqual((user_name("x"), user_name("99999999999999999999")), ("", ""))

    def test_without_such_a_record_or_user_off_works_as_before(self) -> None:
        self.run_profile("on")
        rc, out = self.off_without_user("BUILD_CPUS='0'\n", {})  # no such user any more
        self.assertEqual(rc, 0)
        self.assertNotIn("TARGET_USER not set", out)
        self.assertFalse(os.path.exists(self.state))


class NoAccountNoUserManagerCallTest(GameProfileCase):
    """No `systemctl --user` runs for a name no account has (TARGET_USER unset, or naming none, and no uid
    in game.state to act for): `-M @` reaches the caller's own user manager (root's), `-M NAME@` none. A
    record of the user manager of no user is dropped, said, never undone elsewhere (r26.2)."""

    NOBODY = "cst-test-nobody"

    def nameless(self, target: str) -> tuple[GameProfile, io.StringIO]:
        """The profile for target, a name no account has; only USER has one (uid UID)."""
        out = io.StringIO()
        p = GameProfile(
            self.paths,
            GameSettings(target_user=target),
            self.systemd,
            uid_of=lambda u: UID if u == USER else "",
            name_of=lambda uid: USER if uid == UID else "",
            out=out,
        )
        self.systemd.actions = []
        return p, out

    def user_calls(self) -> list[str]:
        return [a for a in self.systemd.actions if a.startswith("systemctl --user")]

    def test_off_drops_an_old_value_of_the_user_manager_with_no_user_to_undo_it_for(self) -> None:
        # An 'on' whose first record failed: no user@UID.service record, so no uid to act for.
        for target in ("", self.NOBODY):
            with self.subTest(target=target):
                self.write(
                    self.state,
                    "prop usr app.slice CPUWeight existed 100\nprop sys machine.slice CPUWeight existed 50\n",
                )
                p, out = self.nameless(target)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(p.off(), 0)
                self.assertEqual(self.user_calls(), [])  # the old 'off' ran `--user -M @ set-property`
                self.assertIn(f"{SYS} set-property --runtime machine.slice CPUWeight=50", self.systemd.actions)
                self.assertIn(
                    "<4>game-profile: off: could not undo app.slice CPUWeight=100: TARGET_USER names no account,"
                    " no user manager to undo it in: dropped\n",
                    out.getvalue(),
                )
                self.assertEqual(err.getvalue(), f"id: '{target}': no such user\n")  # said once
                self.assertFalse(os.path.exists(self.state))
                self.assertFalse(os.path.exists(self.paths.leftovers))  # dropped, not kept

    def test_the_tick_drops_a_kept_old_value_of_no_user(self) -> None:
        self.no_game()
        self.write(self.paths.leftovers, "cst-game-leftovers 2\nuser -\nprop usr app.slice CPUWeight existed 100\n")
        p, out = self.nameless(self.NOBODY)
        self.assertEqual(p.tick(), 0)
        self.assertEqual(self.user_calls(), [])  # the old tick ran `--user -M cst-test-nobody@ set-property`
        self.assertIn(
            "<4>game-profile: could not undo app.slice CPUWeight=100: TARGET_USER names no account,"
            " no user manager to undo it in: dropped\n",
            out.getvalue(),
        )
        self.assertFalse(os.path.exists(self.paths.leftovers))

    def test_the_tick_and_state_ask_no_user_manager_for_no_user(self) -> None:
        # On with no uid in game.state and TARGET_USER unset: the user manager's drift is not compared, set or
        # shown (the old tick asked `--user -M @` and set user.slice's CPUWeight in root's user manager).
        self.write(self.state, "prop sys machine.slice CPUWeight existed 50\n")
        self.write(self.intended, intended_header() + "prop usr user.slice CPUWeight idle idle\n")
        self.write(self.manual, "")
        p, out = self.nameless("")
        self.assertEqual(p.tick(), 0)
        self.assertEqual(self.user_calls(), [])
        self.assertNotIn("user.slice CPUWeight", out.getvalue())
        p, out = self.nameless("")
        self.assertEqual(p.state(), 0)
        self.assertEqual(self.user_calls(), [])
        self.assertNotIn("DRIFT", out.getvalue())

    def test_off_s_no_such_user_line_survives_a_quiet_lookup_before_it(self) -> None:
        p, _ = self.nameless(self.NOBODY)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(p.ask("usr", "show", "-P", "CPUWeight", "user.slice").returncode, 1)  # quiet, first
            self.assertEqual(p.uid(say=True), "")
            self.assertEqual(p.uid(say=True), "")
        self.assertEqual(err.getvalue(), f"id: '{self.NOBODY}': no such user\n")  # once
        self.assertEqual(self.user_calls(), [])

    def test_a_user_manager_of_no_uid_is_never_gone(self) -> None:
        p, _ = self.nameless("")
        self.assertFalse(user_manager_gone(p, ""))
        self.assertFalse(unit_gone(p, "usr", "app.slice"))  # the old one: True, the call counted done
        self.assertFalse(unit_gone(p, "usr", "run-u7.scope"))
        self.assertEqual(self.user_calls(), [])
        # A uid whose runtime folder is gone (a logout): gone, as before.
        mine, _ = self.profile()
        self.assertFalse(user_manager_gone(mine, UID))
        shutil.rmtree(os.path.join(self.paths.run_user, UID))
        self.assertTrue(user_manager_gone(mine, UID))
        self.assertTrue(unit_gone(mine, "usr", "app.slice"))


class TickForARecordedUidWithoutAccountTest(GameProfileCase):
    """G7, the tick and 'state': with game.state of a uid no account has now and a TARGET_USER that names another
    account, the user manager's side is left alone: no setting and no record in TARGET_USER's manager (it is
    another user's), no drift compared there; said once per boot. The system manager's side still runs."""

    STEAM = "app-flatpak-com.valvesoftware.Steam-7.scope"

    @override
    def setUp(self) -> None:
        super().setUp()
        self.run_profile("on")  # for USER, uid UID
        self.write(self.manual, "")
        self.systemd.units["usr"] = [self.STEAM]  # a game started after 'on'
        self.set_now("usr", "user.slice", CPUWeight="30")  # drifted: game.intended holds idle
        self.systemd.units["sys"] = ["docker-1.scope"]  # a container started after 'on'
        self.set_now("sys", "docker-1.scope", Slice="system.slice", AllowedCPUs="0-23")

    def other_profile(self) -> tuple[GameProfile, io.StringIO]:
        """TARGET_USER other (uid 1001, an account); no account has UID, the uid 'on' acted for, now."""
        out = io.StringIO()
        p = GameProfile(
            self.paths,
            dataclasses.replace(self.settings, target_user="other"),
            self.systemd,
            uid_of=lambda u: {"other": "1001"}.get(u, ""),
            name_of=lambda uid: {"1001": "other"}.get(uid, ""),
            out=out,
        )
        self.systemd.actions = []
        return p, out

    def test_the_tick_sets_nothing_in_another_user_s_manager_and_says_why_once(self) -> None:
        state = self.read(self.state)
        said: list[str] = []
        for _ in range(2):
            p, out = self.other_profile()
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(p.tick(), 0)
            self.assertEqual(err.getvalue(), "")
            self.assertEqual([a for a in self.sets(self.systemd.actions) if "--user" in a], [])
            said.append(out.getvalue())
        self.assertEqual(self.now("usr", "user.slice", "CPUWeight"), "30")
        self.assertNotIn(self.STEAM, self.read(self.state))
        self.assertIn("prop sys docker-1.scope AllowedCPUs new -\n", self.read(self.state))  # system side
        self.assertEqual(self.read(self.state).replace("prop sys docker-1.scope AllowedCPUs new -\n", ""), state)
        self.assertIn(
            "<4>game-profile: profile on, its user manager's side not handled: no account has uid"
            f" {UID}, the user 'on' acted for (fix it; said once per boot)\n",
            said[0],
        )
        self.assertNotIn("not handled", said[1])  # said once per boot
        self.assertNotIn("drift: user.slice", said[0] + said[1])

    def test_state_compares_no_drift_in_another_user_s_manager(self) -> None:
        p, out = self.other_profile()
        self.assertEqual(p.state(), 0)
        self.assertEqual([a for a in self.systemd.actions if "--user" in a and "LoadState" in a], [])
        self.assertNotIn("DRIFT: user.slice", out.getvalue())
        # It says so: the user manager's settings were not compared (the old 'state' said every setting held).
        self.assertNotIn("every setting the profile made still holds", out.getvalue())
        self.assertIn(
            "  drift: none in the system manager's settings and the files\n"
            f"  drift: the user manager's settings not checked (no account has uid {UID}, the user 'on' acted for)\n",
            out.getvalue(),
        )


class OnTargetUserWithoutAccountTest(GameProfileCase):
    """'on' with a TARGET_USER that names no account: refused before it changes anything (it would set
    user@.service and user-.slice, and the user manager of no one)."""

    NOBODY = "cst-test-no-such-user"

    def test_on_refuses_and_changes_nothing(self) -> None:
        self.write(self.manual, "")  # 'cst cpu game on' makes it, then starts cpu-game.service
        out, err = io.StringIO(), io.StringIO()
        p = GameProfile(self.paths, GameSettings(target_user=self.NOBODY), self.systemd, uid_of=user_id, out=out)
        self.systemd.actions = []
        with contextlib.redirect_stderr(err):
            self.assertEqual(p.on(), 1)
            self.assertEqual(p.locked("on"), 1)  # what the unit's ExecStart gets
        line = f"<3>game-profile: TARGET_USER names no account: {self.NOBODY} (in {self.paths.conf})\n"
        self.assertEqual(out.getvalue(), line * 2)
        self.assertEqual(err.getvalue(), "")  # no "id: ...: no such user" line on top of it
        self.assertEqual(self.systemd.actions, [])
        for path in (self.state, self.intended, self.manual):
            self.assertFalse(os.path.exists(path), path)
        self.assertFalse(os.path.exists(self.unit_runtime) and os.listdir(self.unit_runtime))
        # Nothing to undo: 'state' says off, and 'off' works.
        out, _ = self.run_profile("state")
        self.assertTrue(out.startswith("game profile: off\n"), out)
        self.run_profile("off")

    def test_state_says_why_on_refuses(self) -> None:
        # 'state' names the reason (as it names a problem line) and asks no user manager of no one.
        for name, why in (
            (self.NOBODY, f"TARGET_USER names no account: {self.NOBODY} (in {self.paths.conf})"),
            ("", f"TARGET_USER not set in {self.paths.conf}"),
        ):
            out, err = io.StringIO(), io.StringIO()
            p = GameProfile(self.paths, GameSettings(target_user=name), self.systemd, uid_of=user_id, out=out)
            self.systemd.actions = []
            with contextlib.redirect_stderr(err):
                self.assertEqual(p.state(), 0)
            text = out.getvalue()
            self.assertTrue(
                text.startswith(f"game profile: off\n  settings: {why} ('on' refuses until it is fixed)\n"), text
            )
            self.assertNotIn("toolboxes", text)
            self.assertEqual([a for a in self.systemd.actions if "--user" in a], [])
            self.assertEqual(err.getvalue(), "")

    def test_the_same_name_with_an_account_switches_on(self) -> None:
        # The check reads the lookup, not the name.
        out, _ = self.run_profile("on", GameSettings(target_user=self.NOBODY))
        self.assertIn("<5>game-profile: on: ", out)
        self.assertTrue(os.path.exists(self.state))


class TickTargetUserWithoutAccountTest(GameProfileCase):
    """The watchdog, with the profile off and a GameMode client running, does not start cpu-game.service
    for a TARGET_USER 'on' refuses (unset, or a name with no account): it would fail again every 30 s.
    It says why once per boot; once the name has an account it starts the unit again."""

    NOBODY = "cst-test-no-such-user"

    def tick(self, name: str, uid_of: Callable[[str], str] = user_id) -> tuple[str, str, list[str]]:
        """One tick for TARGET_USER name: (its journal lines, its stderr, the systemctl calls)."""
        out, err = io.StringIO(), io.StringIO()
        p = GameProfile(self.paths, GameSettings(target_user=name), self.systemd, uid_of=uid_of, out=out)
        self.systemd.actions = []
        with contextlib.redirect_stderr(err):
            self.assertEqual(p.locked("tick"), 0)  # the watchdog's unit does not fail
        return out.getvalue(), err.getvalue(), self.systemd.actions

    def said(self, why: str) -> str:
        return f"GameMode client running, profile not switched on: {why} (fix it; said once per boot)"

    def test_no_start_for_a_name_without_account_said_once(self) -> None:
        why = f"TARGET_USER names no account: {self.NOBODY} (in {self.paths.conf})"
        self.assertEqual(self.tick(self.NOBODY), (f"<4>game-profile: {self.said(why)}\n", "", []))
        self.assertEqual(self.read(self.paths.settings_warned), self.said(why))
        self.assertEqual(self.tick(self.NOBODY), ("", "", []))  # said already this boot
        self.assertFalse(os.path.exists(self.state))

    def test_no_start_for_an_unset_name_said_once(self) -> None:
        why = f"TARGET_USER not set in {self.paths.conf}"
        self.assertEqual(self.tick(""), (f"<4>game-profile: {self.said(why)}\n", "", []))
        self.assertEqual(self.tick(""), ("", "", []))

    def test_fixed_it_starts_the_unit_and_a_name_that_breaks_again_is_said_again(self) -> None:
        self.tick(self.NOBODY)
        out, err, actions = self.tick(self.NOBODY, lambda _u: UID)  # the account exists now
        self.assertEqual(out, "<5>game-profile: GameMode client running but profile off: switching on\n")
        self.assertEqual((err, actions), ("", [f"{SYS} start --no-block cpu-game.service"]))
        self.assertFalse(os.path.exists(self.paths.settings_warned))
        out, _, actions = self.tick(self.NOBODY)
        self.assertIn("TARGET_USER names no account", out)
        self.assertEqual(actions, [])

    def test_a_name_fixed_while_the_profile_is_on_is_forgotten_too(self) -> None:
        self.tick(self.NOBODY)
        self.assertTrue(os.path.exists(self.paths.settings_warned))
        self.run_profile("on")  # started by GameMode's hook, for a name with an account
        self.tick(self.NOBODY, lambda _u: UID)
        self.assertFalse(os.path.exists(self.paths.settings_warned))

    def test_a_settings_line_and_the_name_share_the_mark_each_said_when_it_comes(self) -> None:
        self.write(self.paths.conf, "TARGET_USER='cst-test-no-such-user'\nBUILD_CPUS=$(nproc)\n")
        settings, problems = read_settings(self.paths.conf)
        out = io.StringIO()
        p = GameProfile(self.paths, settings, self.systemd, uid_of=user_id, problems=problems, out=out)
        self.assertEqual(p.tick(), 0)
        self.assertIn("line 2: BUILD_CPUS=$(nproc)", out.getvalue())  # the line first: 'on' refuses for it
        self.assertNotIn("names no account", out.getvalue())
        # The line fixed, the name still without an account: the line's word is forgotten, the name is said.
        out, err, actions = self.tick(self.NOBODY)
        self.assertEqual((out.count("TARGET_USER names no account"), err, actions), (1, "", []))

    def test_a_fixed_line_is_forgotten_while_the_name_still_has_no_account(self) -> None:
        # The tick forgets its word on a settings line once the line is fixed, also while TARGET_USER still
        # names no account: the same line broken again is said again (and not hidden by the old mark).
        def tick_with(conf: str) -> str:
            self.write(self.paths.conf, conf)
            settings, problems = read_settings(self.paths.conf)
            out = io.StringIO()
            p = GameProfile(self.paths, settings, self.systemd, uid_of=user_id, problems=problems, out=out)
            self.assertEqual(p.tick(), 0)
            return out.getvalue()

        broken = f"TARGET_USER='{self.NOBODY}'\nBUILD_CPUS=$(nproc)\n"
        self.assertIn("line 2: BUILD_CPUS=$(nproc)", tick_with(broken))
        self.no_game()
        self.assertEqual(tick_with(f"TARGET_USER='{self.NOBODY}'\n"), "")  # fixed, and no game runs
        self.assertFalse(os.path.exists(self.paths.settings_warned))
        self.gamemode_pid("1")
        self.assertIn("line 2: BUILD_CPUS=$(nproc)", tick_with(broken))

    def test_a_tick_looks_up_the_name_only_when_it_needs_it(self) -> None:
        # No GameMode client, or the profile switched off by hand during this game: the tick has nothing to
        # start, so no lookup (NSS, sssd or LDAP every 30 s for nothing).
        names: list[str] = []

        def uid_of(user: str) -> str:
            names.append(user)
            return user_id(user)

        self.no_game()
        self.assertEqual(self.tick(self.NOBODY, uid_of), ("", "", []))
        self.gamemode_pid("1")
        self.write(self.paths.suppress, "")
        self.assertEqual(self.tick(self.NOBODY, uid_of), ("", "", []))
        self.assertEqual(names, [])
        os.remove(self.paths.suppress)  # a client runs and the profile is off: 'on' would need the name
        self.assertIn("TARGET_USER names no account", self.tick(self.NOBODY, uid_of)[0])
        self.assertEqual(names, [self.NOBODY])

    def test_a_link_at_the_mark_is_replaced_and_never_followed(self) -> None:
        victim = os.path.join(self.tmp, "victim")
        self.write(victim, "keep\n")
        os.symlink(victim, self.paths.settings_warned)
        out, _, _ = self.tick(self.NOBODY)
        self.assertIn("TARGET_USER names no account", out)
        self.assertEqual(self.read(victim), "keep\n")
        self.assertFalse(os.path.islink(self.paths.settings_warned))
        self.assertEqual(os.stat(self.paths.settings_warned).st_mode & 0o7777, 0o644)


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
            f"<3>game-profile: not switched on: a line of {conf} is not a plain setting (fix it; 'off' still works)\n",
        )
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.systemd.actions, [])
        self.assertEqual(p.locked("on"), 1)  # what the unit's ExecStart gets

    def test_a_blank_beside_a_value_refuses_on_too(self) -> None:
        # The line keeps bash's value (a file bash sources), but 'on' refuses on it as on any problem line:
        # BUILD_CPUS='12-23<NBSP>' must not pin the boxes to what CpuSet makes of it.
        self.write(self.paths.conf, "TARGET_USER='testuser'\nBUILD_CPUS='12-23\N{NO-BREAK SPACE}'\n")
        settings, problems = read_settings(self.paths.conf)
        self.assertEqual([(p.line, p.kept) for p in problems], [(2, True)])
        self.assertEqual(settings.build_cpus, "12-23\N{NO-BREAK SPACE}")
        out = io.StringIO()
        p = GameProfile(self.paths, settings, self.systemd, uid_of=lambda _u: UID, problems=problems, out=out)
        self.assertEqual(p.on(), 1)
        conf = self.paths.conf
        self.assertEqual(
            out.getvalue(),
            f"<3>game-profile: {conf} line 2: BUILD_CPUS='12-23\N{NO-BREAK SPACE}': a no-break space (U+00A0) at "
            "the end of the value: CST does not trim it (delete it)\n"
            f"<3>game-profile: not switched on: a line of {conf} is not a plain setting (fix it; 'off' still works)\n",
        )
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.systemd.actions, [])

    def test_a_refused_on_drops_the_by_hand_mark_and_a_later_game_is_switched_off(self) -> None:
        self.write(self.manual, "")  # 'cst cpu game on' makes it, then starts cpu-game.service
        p, _ = self.problem_profile()
        self.assertEqual(p.on(), 1)
        self.assertFalse(os.path.exists(self.manual))
        p, _ = self.profile(GameSettings())  # no TARGET_USER: refused the same way
        self.write(self.manual, "")
        self.assertEqual(p.on(), 1)
        self.assertFalse(os.path.exists(self.manual))
        # Fixed; a game switches the profile on, and its end switches it off again.
        self.write(self.paths.conf, "TARGET_USER='testuser'\n")
        self.run_profile("on")
        self.no_game()
        _, actions = self.run_profile("tick")
        self.assertEqual(actions, [f"{SYS} stop --no-block cpu-game.service"])

    def test_tick_changes_nothing_but_still_switches_off_and_off_works(self) -> None:
        self.run_profile("on")  # switched on before the line was broken
        self.write(self.manual, "")
        self.set_now("sys", "machine.slice", CPUWeight="50")
        p, out = self.problem_profile()
        self.systemd.actions = []
        self.assertEqual(p.tick(), 0)  # the watchdog's unit does not fail on it
        conf = self.paths.conf
        self.assertEqual(
            out.getvalue(),
            f"<4>game-profile: {conf} line 2: BUILD_CPUS=$(nproc): $, ` or \\ outside single quotes: bash would expand it"
            " (use single quotes)\n"
            f"<4>game-profile: tick changes nothing: a line of {conf} is not a plain setting (fix it; 'off' still works;"
            " said once per boot)\n",
        )
        self.assertEqual(self.sets(self.systemd.actions), [])
        p, out = self.problem_profile()
        self.assertEqual(p.locked("tick"), 0)  # the next tick: said already
        self.assertEqual(out.getvalue(), "")
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

    def test_a_tick_with_a_game_running_does_not_start_the_unit_on_would_fail(self) -> None:
        # Profile off, a GameMode client running: 'on' would refuse, so the watchdog does not start
        # cpu-game.service (which would fail again every 30 s); it says why once per boot.
        p, out = self.problem_profile()
        self.assertEqual(p.locked("tick"), 0)
        self.assertNotIn(f"{SYS} start --no-block cpu-game.service", self.systemd.actions)
        self.assertIn("<4>game-profile: GameMode client running, profile not switched on: a line of", out.getvalue())
        self.assertFalse(os.path.exists(self.state))
        for _ in range(2):
            p, out = self.problem_profile()
            self.assertEqual(p.tick(), 0)
            self.assertEqual(out.getvalue(), "")
        self.assertEqual(self.systemd.actions, [])
        # Another problem line: said again.
        self.write(self.paths.conf, "TARGET_USER='testuser'\nP_CORES=`nproc`\n")
        settings, problems = read_settings(self.paths.conf)
        out = io.StringIO()
        p = GameProfile(self.paths, settings, self.systemd, uid_of=lambda _u: UID, problems=problems, out=out)
        self.assertEqual(p.tick(), 0)
        self.assertIn("line 2: P_CORES=`nproc`", out.getvalue())
        # Fixed: the watchdog switches the profile on again (for a TARGET_USER with an account).
        self.write(self.paths.conf, "TARGET_USER='testuser'\n")
        settings, problems = read_settings(self.paths.conf)
        p = GameProfile(self.paths, settings, self.systemd, uid_of=lambda _u: UID, problems=problems)
        self.systemd.actions = []
        self.assertEqual(p.tick(), 0)
        self.assertEqual(self.systemd.actions, [f"{SYS} start --no-block cpu-game.service"])

    def test_the_watchdog_says_it_again_after_a_fix_and_for_another_word(self) -> None:
        p, out = self.problem_profile()
        self.assertEqual(p.tick(), 0)
        self.assertIn("GameMode client running, profile not switched on", out.getvalue())
        # Fixed (no game now), then broken again the same way: said again.
        self.no_game()
        self.write(self.paths.conf, "TARGET_USER='testuser'\n")
        settings, problems = read_settings(self.paths.conf)  # for a TARGET_USER with an account
        p = GameProfile(self.paths, settings, self.systemd, uid_of=lambda _u: UID, problems=problems)
        self.assertEqual(p.tick(), 0)
        self.assertFalse(os.path.exists(self.paths.settings_warned))
        self.gamemode_pid("1")
        p, out = self.problem_profile()
        self.assertEqual(p.tick(), 0)
        self.assertIn("GameMode client running, profile not switched on", out.getvalue())
        # The profile on (by hand), the same lines: the other word is said too, once.
        self.write(self.state, "")
        self.write(self.manual, "")
        for expected in ("tick changes nothing", ""):
            p, out = self.problem_profile()
            self.assertEqual(p.tick(), 0)
            if expected:
                self.assertIn(f"<4>game-profile: {expected}: a line of", out.getvalue())
            else:
                self.assertEqual(out.getvalue(), "")

    def test_a_link_at_the_watchdog_mark_is_replaced_and_never_followed(self) -> None:
        victim = os.path.join(self.tmp, "victim")
        self.write(victim, "keep\n")
        os.symlink(victim, self.paths.settings_warned)
        p, out = self.problem_profile()
        self.assertEqual(p.tick(), 0)
        self.assertIn("profile not switched on", out.getvalue())
        self.assertEqual(self.read(victim), "keep\n")
        self.assertFalse(os.path.islink(self.paths.settings_warned))
        self.assertEqual(os.stat(self.paths.settings_warned).st_mode & 0o7777, 0o644)
        # A mark that cannot be written: the tick still ends in success, and says it again next time.
        os.remove(self.paths.settings_warned)
        os.mkdir(self.paths.settings_warned)
        for _ in range(2):
            p, out = self.problem_profile()
            self.assertEqual(p.tick(), 0)
            self.assertIn("profile not switched on", out.getvalue())

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
        self.assertIn(
            "(not a plain setting: 'on' refuses and the watchdog changes nothing until it is fixed)\n", out.getvalue()
        )


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

    def fallback_tree(self) -> str:
        """cpuset delegated, and a user manager that ignores AllowedCPUs on user.slice: its cpuset.cpus (8-23)."""
        self.tree(delegated=True)
        cpus = self.cg("user.slice")
        self.write(os.path.join(cpus, "cpuset.cpus"), "8-23\n")
        self.set_now("usr", "user.slice", ControlGroup="/" + os.path.relpath(cpus, self.paths.cgroot))
        return os.path.join(cpus, "cpuset.cpus")

    def test_a_cpuset_fallback_whose_write_failed_in_on_is_written_by_the_next_tick(self) -> None:
        # r26.1, G1's twin: the record of game.intended comes before the write, so the tick writes it; the old
        # 'on' wrote it only after a write that worked, and the toolboxes kept the game's CPUs until 'off'.
        f = self.fallback_tree()
        kernel = FailingOnceKernel({f})
        with self.ports(kernel=kernel):
            out, _ = self.run_profile("on")
        self.assertEqual(self.read(f), "8-23\n")  # the write failed
        self.assertIn(f"<4>game-profile: user.slice: could not write {f}\n", out)
        self.assertIn(f"cgw {f} 8-23\n", self.read(self.state))
        self.assertEqual(
            [r for r in intended_records(self.read(self.intended)) if r[1] == f], [("cgw", f, "12-23", "12-23")]
        )
        self.write(self.manual, "")
        out, _ = self.run_profile("tick")
        self.assertEqual(self.read(f), "12-23\n")
        self.assertIn(f"<5>game-profile: drift: {f} was '8-23', wrote 12-23 again\n", out)
        out, _ = self.run_profile("tick")
        self.assertNotIn(f, out)  # held: nothing written again
        self.run_profile("off")
        self.assertEqual(self.read(f), "8-23\n")

    def test_a_cpuset_fallback_the_kernel_shows_in_another_form_gets_a_second_record(self) -> None:
        # The prediction is the kernel's form of the list (commas and ranges); a read-back in another form after
        # a write that worked is recorded too, so the tick does not write it every 30 s. The same for the tick's
        # own write of a prediction whose write failed in 'on'.
        f = self.fallback_tree()
        commas = dataclasses.replace(self.settings, build_cpus="12,13,14,15")
        self.run_profile("on", commas)
        self.assertEqual(
            [r for r in intended_records(self.read(self.intended)) if r[1] == f],
            [("cgw", f, "12,13,14,15", "12,13,14,15")],
        )
        self.assertEqual(  # the prediction first, then the read-back
            [r for r in records(self.read(self.intended)) if r[:2] == ("cgw", f)],
            [("cgw", f, "12,13,14,15", "12-15"), ("cgw", f, "12,13,14,15", "12,13,14,15")],
        )
        self.write(self.manual, "")
        out, _ = self.run_profile("tick", commas)
        self.assertNotIn(f, out)
        self.run_profile("off", commas)
        self.assertEqual(self.read(f), "8-23\n")
        with self.ports(kernel=FailingOnceKernel({f})):
            self.run_profile("on", commas)
        self.write(self.manual, "")
        out, _ = self.run_profile("tick", commas)
        self.assertIn(f"<5>game-profile: drift: {f} was '8-23', wrote 12,13,14,15 again\n", out)
        self.assertEqual(
            [r for r in intended_records(self.read(self.intended)) if r[1] == f],
            [("cgw", f, "12,13,14,15", "12,13,14,15")],
        )
        out, _ = self.run_profile("tick", commas)
        self.assertNotIn(f, out)

    def test_a_cpuset_fallback_the_manager_wrote_in_the_kernel_s_form_is_left_alone(self) -> None:
        # A user manager that honours AllowedCPUs has written user.slice's cpuset.cpus in the kernel's form (12-15)
        # when the fallback reads it, for a BUILD_CPUS written another way (12,13,14,15): nothing to write and
        # nothing recorded. The old one compared the file with BUILD_CPUS as written: it recorded the game's own
        # value as the file's old one (cgw FILE 12-15), wrote the file again, and 'off' wrote 12-15 back.
        f = self.fallback_tree()
        self.write(f, "12-15\n")
        commas = dataclasses.replace(self.settings, build_cpus="12,13,14,15")
        self.run_profile("on", commas)
        self.assertEqual([r for r in records(self.read(self.state)) if r[:2] == ("cgw", f)], [])
        self.assertEqual([r for r in records(self.read(self.intended)) if r[:2] == ("cgw", f)], [])
        self.assertEqual(self.read(f), "12-15\n")
        # BUILD_CPUS in the kernel's form already: left alone as before.
        self.run_profile("off", commas)
        self.write(f, "12-23\n")
        self.run_profile("on")
        self.assertEqual([r for r in records(self.read(self.state)) if r[:2] == ("cgw", f)], [])

    def test_a_game_intended_of_the_previous_version_reads_the_same(self) -> None:
        # Upgrade: the previous 'on' wrote one cgw record per file, after a write that worked.
        f = self.fallback_tree()
        self.write(self.state, f"prop sys user@{UID}.service MemoryLow new -\ncgw {f} 8-23\n")
        self.write(self.intended, intended_header() + f"cgw {f} 12-23 12-23\n")
        self.write(f, "12-23\n")
        self.write(self.manual, "")
        out, _ = self.run_profile("tick")
        self.assertNotIn(f, out)
        self.write(f, "8-23\n")
        out, _ = self.run_profile("tick")
        self.assertIn(f"<5>game-profile: drift: {f} was '8-23', wrote 12-23 again\n", out)
        self.assertEqual(self.read(f), "12-23\n")

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

    def test_a_folder_whose_name_is_no_unit_name_is_left_alone(self) -> None:
        # O5: a folder made by hand in the user manager's tree, with a blank or a byte that is not UTF-8
        # in its name: no unit systemd could have. The old profile recorded and set it on every run
        # (game.state is read back with "replace", so the record never matched).
        self.tree()
        bad, replaced = os.fsdecode(b"\xff"), "\N{REPLACEMENT CHARACTER}"
        for name in ("a b.slice", f"x{bad}.slice", f"app.slice/run-{bad}.scope"):
            self.cg(*name.split("/"))
        out, actions = self.run_profile("on")
        self.write(self.manual, "")
        state = self.read(self.state)
        self.assertIn(f"{USR} set-property --runtime lushtext.slice CPUWeight=idle", self.sets(actions))
        for _ in range(3):
            self.assertFalse([a for a in self.sets(actions) if "a b" in a or replaced in a or bad in a])
            self.assertNotIn("a b.slice", out)
            self.assertNotIn(replaced, out)
            out, actions = self.run_profile("tick")
            self.assertEqual(self.sets(actions), [])
            self.assertEqual(self.read(self.state), state)
        self.assertNotIn("a b", state)
        self.assertNotIn(replaced, state)

    def test_lushtext_jobs_under_user_slice_are_covered_by_its_rule_only(self) -> None:
        # Where lushtext runs its jobs: a valid unit name under the toolboxes' user.slice, whose own
        # rule covers it; 'on' and the ticks never name the job or its slice, and a tick with every
        # value holding runs no set-property (lushtext measures timing there).
        self.tree()
        self.cg("user.slice", "user-lushtext.slice", "lushtext-cap-1234-5678.scope", procs=(5151,))
        _, actions = self.run_profile("on")
        self.write(self.manual, "")
        self.assertTrue([a for a in self.sets(actions) if f"{USR} set-property --runtime user.slice " in a])
        for _ in range(2):
            names = [a for a in self.sets(actions) if "lushtext-cap" in a or "user-lushtext" in a]
            self.assertEqual(names, [])
            _, actions = self.run_profile("tick")
            self.assertEqual(self.sets(actions), [])
        state = self.read(self.state)
        self.assertNotIn("lushtext-cap", state)
        self.assertNotIn("user-lushtext", state)

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


class HandledBoxSetAgainTest(GameProfileCase):
    """A box the tick handled already (game.state holds its record) whose setting does not hold: its
    set-property failed (O2), or an 'off' that was killed undid it while the profile stayed on (O6). The
    tick reads its recorded settings back and sets each one that does not hold again, with no new record;
    when every one holds it writes nothing."""

    STEAM = "app-flatpak-com.valvesoftware.Steam-1.scope"

    def docker(self) -> None:
        self.systemd.units["sys"] = ["docker-1.scope"]
        self.set_now("sys", "docker-1.scope", Slice="system.slice")

    def box(self, *parts: str, procs: Sequence[int] = ()) -> str:
        d = os.path.join(self.ucg, *parts)
        os.makedirs(d, exist_ok=True)
        if procs:
            self.write(os.path.join(d, "cgroup.procs"), "".join(f"{p}\n" for p in procs))
        return d

    def delegate(self) -> None:
        self.write(os.path.join(self.ucg, "cgroup.subtree_control"), "cpuset cpu memory pids\n")

    def failed_then_tick(self, mgr: str, unit: str) -> tuple[str, str, list[str]]:
        """'on' with unit's set-property failing, then a tick with it working: (game.state after 'on', the
        tick's output, its set-property calls)."""
        self.systemd.fail_set.add((mgr, unit))
        out, _ = self.run_profile("on")
        self.assertIn(f"{unit}: could not set", out)
        state = self.read(self.state)
        self.systemd.fail_set.clear()
        out, actions = self.run_profile("tick")
        return state, out, self.sets(actions)

    def test_a_docker_container_whose_set_failed_is_set_again_with_no_new_record(self) -> None:
        self.docker()
        state, out, sets = self.failed_then_tick("sys", "docker-1.scope")
        self.assertEqual(sets, [f"{SYS} set-property --runtime docker-1.scope AllowedCPUs=12-23"])
        self.assertEqual(self.read(self.state), state)
        self.assertIn(
            "<5>game-profile: docker-1.scope: AllowedCPUs is '', not '12-23' (its set failed, or something undid "
            "it): set AllowedCPUs=12-23 again\n",
            out,
        )
        self.assertEqual(self.now("sys", "docker-1.scope", "AllowedCPUs"), "12-23")
        _, actions = self.run_profile("tick")  # it holds now: nothing more
        self.assertEqual(self.sets(actions), [])
        self.run_profile("off")  # its "new" record still undoes it
        self.assertFalse(os.path.exists(self.ctrl("sys", "docker-1.scope", "AllowedCPUs")))

    def test_a_steam_scope_whose_set_failed_is_set_again(self) -> None:
        self.systemd.units["usr"] = [self.STEAM]
        state, out, sets = self.failed_then_tick("usr", self.STEAM)
        self.assertEqual(sets, [f"{USR} set-property --runtime {self.STEAM} MemoryLow=16G"])
        self.assertEqual(self.read(self.state), state)
        self.assertIn(f"{self.STEAM}: MemoryLow is '', not '17179869184'", out)

    def test_systemd_run_jobs_whose_set_failed_get_both_settings_again(self) -> None:
        self.delegate()
        for unit in ("run-u7.service", "run-r1a2.scope"):
            self.box("app.slice", unit)
            self.systemd.fail_set.add(("usr", unit))
        self.run_profile("on")
        state = self.read(self.state)
        for unit in ("run-u7.service", "run-r1a2.scope"):
            self.assertIn(f"prop usr {unit} AllowedCPUs new -\n", state)
        self.systemd.fail_set.clear()
        _, actions = self.run_profile("tick")
        self.assertEqual(
            sorted(self.sets(actions)),
            [
                f"{USR} set-property --runtime run-r1a2.scope CPUWeight=idle AllowedCPUs=12-23",
                f"{USR} set-property --runtime run-u7.service CPUWeight=idle AllowedCPUs=12-23",
            ],
        )
        self.assertEqual(self.read(self.state), state)

    def test_a_slice_next_to_the_game_whose_set_failed_in_on_is_set_again(self) -> None:
        # Its set-property failed; game.intended holds what 'on' meant to set (G1), so the drift repair sets it
        # (once: the box check leaves a setting the drift repair covers to it).
        self.box("e2e.slice")
        state, out, sets = self.failed_then_tick("usr", "e2e.slice")
        self.assertIn("prop usr e2e.slice CPUWeight idle idle\n", self.read(self.intended))
        self.assertEqual(sets, [f"{USR} set-property --runtime e2e.slice CPUWeight=idle"])
        self.assertEqual(self.read(self.state), state)
        self.assertIn("drift: e2e.slice CPUWeight was '' instead of 'idle'", out)
        self.assertNotIn("its set failed", out)

    def test_a_steam_scope_a_killed_off_undid_is_set_again_and_off_still_undoes_it(self) -> None:
        self.systemd.units["usr"] = [self.STEAM]
        self.run_profile("on")
        override = self.ctrl("usr", self.STEAM, "MemoryLow")
        self.assertTrue(os.path.exists(override))
        state = self.read(self.state)
        # What an 'off' killed after this undo leaves, once a user daemon-reload ran: no override, the
        # scope's own value, and game.state still there.
        os.unlink(override)
        self.set_now("usr", self.STEAM, MemoryLow="0")
        out, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [f"{USR} set-property --runtime {self.STEAM} MemoryLow=16G"])
        self.assertIn(f"{self.STEAM}: MemoryLow is '0', not '17179869184'", out)
        self.assertEqual(self.read(self.state), state)
        self.assertTrue(os.path.exists(override))
        self.run_profile("off")
        self.assertFalse(os.path.exists(override))

    def test_with_every_setting_holding_a_tick_writes_and_says_nothing(self) -> None:
        # Another project measures timing on these boxes: a tick that finds everything in place runs no
        # set-property and changes no file.
        self.delegate()
        self.docker()
        self.systemd.units["usr"] = [self.STEAM]
        for parts in (("lushtext.slice",), ("background.slice",), ("app.slice", "run-u7.service")):
            self.box(*parts)
        self.run_profile("on")
        files = (self.state, self.intended)
        before = [self.read(f) for f in files]
        for _ in range(2):
            out, actions = self.run_profile("tick")
            self.assertEqual(self.sets(actions), [])
            self.assertEqual(out, "")
            self.assertEqual([self.read(f) for f in files], before)

    def test_a_setting_the_drift_repair_covers_is_left_to_it(self) -> None:
        self.box("lushtext.slice")
        self.run_profile("on")
        self.assertIn("prop usr lushtext.slice CPUWeight idle idle\n", self.read(self.intended))
        self.set_now("usr", "lushtext.slice", CPUWeight="100")
        out, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [f"{USR} set-property --runtime lushtext.slice CPUWeight=idle"])
        self.assertIn("drift: lushtext.slice CPUWeight was '100'", out)
        self.assertNotIn("its set failed", out)
        # Not even asked about by the box check: the drift repair's own show is the only one (beside its
        # read-back of the value it set, show -P).
        shows = [a for a in actions if a.startswith(f"{USR} show -p") and a.endswith(" lushtext.slice")]
        self.assertEqual(len(shows), 1, shows)

    def test_a_box_that_holds_the_game_by_now_is_left_alone(self) -> None:
        d = self.box("app.slice", "run-r9.scope")
        self.systemd.fail_set.add(("usr", "run-r9.scope"))
        self.run_profile("on")
        self.systemd.fail_set.clear()
        self.write(os.path.join(d, "cgroup.procs"), "4242\n")
        self.gamemode_pid("4242")
        _, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [])

    def test_a_unit_systemd_has_not_loaded_is_skipped(self) -> None:
        self.docker()
        self.systemd.fail_set.add(("sys", "docker-1.scope"))
        self.run_profile("on")
        self.systemd.fail_set.clear()
        self.systemd.not_loaded.add(("sys", "docker-1.scope"))
        _, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [])

    def test_a_set_that_fails_again_is_said_and_tried_again_at_the_next_tick(self) -> None:
        self.docker()
        self.systemd.fail_set.add(("sys", "docker-1.scope"))
        self.run_profile("on")
        state = self.read(self.state)
        for _ in range(2):
            out, actions = self.run_profile("tick")
            self.assertEqual(self.sets(actions), [f"{SYS} set-property --runtime docker-1.scope AllowedCPUs=12-23"])
            self.assertIn(
                "<4>game-profile: docker-1.scope: AllowedCPUs is '', not '12-23': could not set AllowedCPUs=12-23 "
                "again\n",
                out,
            )
            self.assertEqual(self.read(self.state), state)

    def test_with_a_newer_game_intended_only_transient_units_are_set_again(self) -> None:
        self.box("e2e.slice")
        self.box("app.slice", "run-u7.service")
        self.systemd.fail_set |= {("usr", "e2e.slice"), ("usr", "run-u7.service")}
        self.run_profile("on")
        self.systemd.fail_set.clear()
        self.write(self.intended, "cst-game-intended 99\n")
        _, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [f"{USR} set-property --runtime run-u7.service CPUWeight=idle"])

    def test_a_value_it_cannot_judge_is_not_set_again(self) -> None:
        settings = dataclasses.replace(self.settings, game_memory_low="10%")
        self.systemd.units["usr"] = [self.STEAM]
        self.systemd.fail_set.add(("usr", self.STEAM))
        self.run_profile("on", settings)
        self.systemd.fail_set.clear()
        _, actions = self.run_profile("tick", settings)
        self.assertEqual([a for a in self.sets(actions) if "Steam" in a], [])

    def test_a_settings_edit_while_on_reaches_the_handled_boxes_once(self) -> None:
        self.docker()
        self.run_profile("on")
        state = self.read(self.state)
        settings = dataclasses.replace(self.settings, build_cpus="14-23")
        _, actions = self.run_profile("tick", settings)
        self.assertEqual(self.sets(actions), [f"{SYS} set-property --runtime docker-1.scope AllowedCPUs=14-23"])
        self.assertEqual(self.read(self.state), state)
        _, actions = self.run_profile("tick", settings)
        self.assertEqual(self.sets(actions), [])  # machine.slice keeps what 'on' set (its game.intended line)
        self.assertEqual(self.now("sys", "machine.slice", "AllowedCPUs"), "12-23")


class LeftoversFormatTest(unittest.TestCase):
    """game.leftovers' format (cpu/game/domain/leftovers.py): its text and back, the undo order, the words."""

    def test_the_text_and_back_with_the_user_of_each_record(self) -> None:
        entries = [
            (None, ("prop", "sys", "machine.slice", "CPUWeight", "existed", "70")),
            ("1000", ("prop", "usr", "a b.slice", "CPUWeight", "existed", "50")),
            ("1000", ("prop", "usr", "c.slice", "CPUWeight", "new", "-")),
            ("1001", ("prop", "usr", "d.slice", "CPUWeight", "new", "-")),
            (None, ("sysfs", "/sys/x", "old")),
        ]
        text = leftovers_text(entries)
        self.assertEqual(
            text,
            "cst-game-leftovers 2\n"
            "user -\n"
            "prop sys machine.slice CPUWeight existed 70\n"
            "user 1000\n"
            "prop usr a\\ b.slice CPUWeight existed 50\n"
            "prop usr c.slice CPUWeight new -\n"
            "user 1001\n"
            "prop usr d.slice CPUWeight new -\n"
            "user -\n"
            "sysfs /sys/x old\n",
        )
        self.assertEqual(leftover_entries(text), entries)
        self.assertIsNone(newer_leftovers("/l", text))
        self.assertEqual(leftover_entries(""), [])

    def test_a_record_of_no_user_after_one_of_a_uid_reads_back_as_no_user_s(self) -> None:
        # G5: format 1 named a user only when the uid changed to another one, so an entry of no user after one
        # of uid 1000 read back as 1000's. Format 2 writes "user -" before it.
        entries: list[tuple[str | None, tuple[str, ...]]] = [
            ("1000", ("prop", "usr", "a.slice", "CPUWeight", "new", "-")),
            (None, ("prop", "usr", "b.slice", "CPUWeight", "new", "-")),
        ]
        text = leftovers_text(entries)
        self.assertEqual(
            text,
            "cst-game-leftovers 2\nuser 1000\nprop usr a.slice CPUWeight new -\nuser -\nprop usr b.slice CPUWeight new -\n",
        )
        self.assertEqual(leftover_entries(text), entries)
        # A format 1 file reads as before: a user line holds until the next one.
        old = "cst-game-leftovers 1\nuser 1000\nprop usr a.slice CPUWeight new -\nprop sys m.slice CPUWeight new -\n"
        self.assertEqual(
            leftover_entries(old),
            [
                ("1000", ("prop", "usr", "a.slice", "CPUWeight", "new", "-")),
                (None, ("prop", "sys", "m.slice", "CPUWeight", "new", "-")),
            ],
        )

    def test_reload_entries_name_their_manager_and_user(self) -> None:
        entries: list[tuple[str | None, tuple[str, ...]]] = [
            (None, ("reload", "sys")),
            ("1000", ("reload", "usr")),
            ("1001", ("reload", "usr")),
        ]
        text = leftovers_text(entries)
        self.assertEqual(
            text, "cst-game-leftovers 2\nuser -\nreload sys\nuser 1000\nreload usr\nuser 1001\nreload usr\n"
        )
        self.assertEqual(leftover_entries(text), entries)
        # A user manager's reload with no user: no manager to reload, no entry.
        self.assertEqual(leftover_entries("cst-game-leftovers 2\nuser -\nreload usr\nreload sys\n"), [entries[0]])

        def epp(cpu: str) -> str:
            return f"/cpu{cpu}/epp"

        self.assertEqual(undo_text(("reload", "sys"), epp), "the daemon-reload of the system manager")
        self.assertEqual(
            undo_text(("reload", "usr"), epp, short=True, uid="1000"), "daemon-reload of uid 1000's user manager"
        )

    def test_a_newer_format_or_a_first_line_with_no_version_is_not_read(self) -> None:
        self.assertIn("is format 3 of the game-leftovers", newer_leftovers("/l", "cst-game-leftovers 3\n") or "")
        self.assertIsNone(newer_leftovers("/l", "cst-game-leftovers 1\n"))  # format 1 is still read
        self.assertEqual(newer_leftovers("/l", "cst-game-leftovers x\n"), "/l: its first line names no format version")
        self.assertIsNone(newer_leftovers("/l", "prop sys a.slice CPUWeight new -\n"))  # no format line: version 0

    def test_the_undo_order_drops_a_kept_record_of_this_game_state(self) -> None:
        old = ("prop", "sys", "machine.slice", "CPUWeight", "existed", "70")
        mine = ("prop", "sys", "machine.slice", "CPUWeight", "existed", "10")
        both = ("epp", "0", "balance_performance")
        self.assertEqual(
            merged_entries([(None, old), (None, both)], [both, mine]), [(None, old), (None, both), (None, mine)]
        )

    def test_the_undo_order_keeps_another_uid_s_entry_of_a_record_of_this_game_state(self) -> None:
        # A user manager's record has the same text for every user: a kept entry of it is dropped only when it is
        # for the uid game.state's records are for (acting, for an entry that names none), never another uid's.
        rec = ("prop", "usr", "user.slice", "CPUWeight", "new", "-")
        self.assertEqual(merged_entries([("1001", rec)], [rec], acting="1000"), [("1001", rec), (None, rec)])
        self.assertEqual(merged_entries([("1000", rec)], [rec], acting="1000"), [(None, rec)])
        self.assertEqual(merged_entries([(None, rec)], [rec], acting="1000"), [(None, rec)])
        # game.state's records for a uid 'off' does not act for (owner): that uid's kept entry is the duplicate.
        self.assertEqual(
            merged_entries([("1001", rec), ("1000", rec)], [rec], owner="1001", acting="1000"),
            [("1000", rec), ("1001", rec)],
        )
        self.assertEqual(entry_key((None, rec), "1000"), ("1000", rec))
        self.assertEqual(entry_key((None, ("reload", "sys")), "1000"), (None, ("reload", "sys")))

    def test_the_words(self) -> None:
        def epp(cpu: str) -> str:
            return f"/cpu{cpu}/epp"

        self.assertEqual(
            undo_text(("prop", "sys", "m.slice", "CPUWeight", "existed", "70"), epp), "m.slice CPUWeight=70"
        )
        self.assertEqual(
            undo_text(("prop", "usr", "a.scope", "MemoryLow", "new", "-"), epp),
            "a.scope MemoryLow (its runtime override)",
        )
        self.assertEqual(undo_text(("epp", "3", "balance_power"), epp), "/cpu3/epp = balance_power")
        self.assertEqual(undo_text(("cgw", "/cg/cpuset.cpus", ""), epp, short=True), "/cg/cpuset.cpus")
        prop: Entry = (None, ("prop", "sys", "m.slice", "CPUWeight", "existed", "70"))
        sysfs: Entry = (None, ("sysfs", "/x", "0"))
        reload_sys: Entry = (None, ("reload", "sys"))
        reload_usr: Entry = ("1000", ("reload", "usr"))
        self.assertEqual(
            off_text([], "/l", kept=0), "off: previous CPU, weight, memory, zswap writeback and EPP settings restored"
        )
        # Settings only: the words as before.
        self.assertTrue(off_text([prop, sysfs], "/l", kept=2).endswith("restored, except 2 setting(s) kept in /l"))
        self.assertTrue(off_text([prop, sysfs], "/l", kept=0).endswith("restored, except 2 setting(s) not undone"))
        self.assertTrue(
            off_text([prop, sysfs, prop], "/l", kept=2).endswith(
                "restored, except 3 setting(s): 2 kept in /l, 1 not undone"
            )
        )
        # r26.6: a daemon-reload is counted apart, and one not kept is "not done".
        self.assertTrue(off_text([reload_sys], "/l", kept=1).endswith("restored, except 1 daemon-reload(s) kept in /l"))
        self.assertTrue(
            off_text([reload_sys, reload_usr], "/l", kept=0).endswith("restored, except 2 daemon-reload(s) not done")
        )
        self.assertTrue(
            off_text([prop, sysfs, reload_sys], "/l", kept=3).endswith(
                "restored, except 2 setting(s) and 1 daemon-reload(s) kept in /l"
            )
        )
        self.assertTrue(
            off_text([prop, sysfs, reload_sys], "/l", kept=2).endswith(
                "restored, except 2 setting(s) and 1 daemon-reload(s): 2 kept in /l, 1 not done"
            )
        )
        self.assertEqual(kept_count_text([prop]), "1 setting(s)")
        self.assertEqual(kept_count_text([reload_usr]), "1 daemon-reload(s)")
        self.assertEqual(kept_count_text([reload_sys, prop, reload_usr]), "1 setting(s) and 2 daemon-reload(s)")
        self.assertEqual(
            leftovers_line([prop], epp),
            "  leftovers: 1 setting(s) 'off' could not undo, retried by the next off and tick: m.slice CPUWeight",
        )
        self.assertEqual(
            leftovers_line([prop, reload_usr], epp),
            "  leftovers: 1 setting(s) and 1 daemon-reload(s) 'off' could not finish, retried by the next off and"
            " tick: m.slice CPUWeight, daemon-reload of uid 1000's user manager",
        )


class LeftoversTest(GameProfileCase):
    """O4: a setting 'off' could not undo (a set-property that failed, a sysfs file that refused the write) is
    named in the journal and kept in game.leftovers; the next 'off' and the tick with the profile off undo it
    again; game.state is removed anyway, so it still means on."""

    def prior_machine_weight(self) -> None:
        """machine.slice had a runtime override CPUWeight=70 before the game: 'on' records it as existed."""
        self.write(self.ctrl("sys", "machine.slice", "CPUWeight"), "[Slice]\nCPUWeight=70\n")
        self.set_now("sys", "machine.slice", CPUWeight="70")

    def failed_off(self) -> str:
        """'on', then an 'off' whose set-property of machine.slice fails: its output."""
        self.prior_machine_weight()
        self.run_profile("on")
        self.systemd.fail_set.add(("sys", "machine.slice"))
        out, _ = self.run_profile("off")
        self.systemd.fail_set.clear()
        return out

    def test_off_keeps_what_it_could_not_undo_and_removes_game_state(self) -> None:
        out = self.failed_off()
        left = self.paths.leftovers
        self.assertIn(
            f"<4>game-profile: off: could not undo machine.slice CPUWeight=70: kept in {left} for the next off and tick\n",
            out,
        )
        self.assertTrue(
            out.endswith(f"restored, except 1 setting(s) kept in {left}\n"),
            out,
        )
        for f in (self.state, self.intended, self.manual, self.paths.drift_warned):
            self.assertFalse(os.path.exists(f), f)
        self.assertEqual(self.read(left), "cst-game-leftovers 2\nuser -\nprop sys machine.slice CPUWeight existed 70\n")
        # Every other undo is done: machine.slice's new overrides are gone, the memory chain's too.
        self.assertFalse(os.path.exists(self.ctrl("sys", "machine.slice", "IOWeight")))
        self.assertFalse(os.path.exists(self.ctrl("sys", f"user@{UID}.service", "MemoryLow")))
        self.assertEqual(self.now("sys", "machine.slice", "CPUWeight"), "10")  # the profile's, kept

    def test_the_next_off_undoes_it_and_removes_the_file(self) -> None:
        self.failed_off()
        out, actions = self.run_profile("off")
        self.assertEqual(self.sets(actions), [f"{SYS} set-property --runtime machine.slice CPUWeight=70"])
        self.assertIn(f"undid machine.slice CPUWeight=70, kept in {self.paths.leftovers} by an earlier off", out)
        self.assertFalse(os.path.exists(self.paths.leftovers))
        self.assertEqual(self.now("sys", "machine.slice", "CPUWeight"), "70")
        _, actions = self.run_profile("off")
        self.assertEqual(actions, [])

    def test_a_tick_with_the_profile_off_undoes_it(self) -> None:
        self.failed_off()
        self.no_game()
        _, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), [f"{SYS} set-property --runtime machine.slice CPUWeight=70"])
        self.assertFalse(os.path.exists(self.paths.leftovers))

    def test_a_tick_with_the_profile_on_leaves_it_and_the_next_off_ends_at_the_oldest_value(self) -> None:
        self.failed_off()
        left = self.read(self.paths.leftovers)
        self.run_profile("on")  # records machine.slice's CPUWeight as it is now: existed 10
        self.assertIn("prop sys machine.slice CPUWeight existed 10\n", self.read(self.state))
        _, actions = self.run_profile("tick")
        self.assertFalse([a for a in self.sets(actions) if "CPUWeight=70" in a], actions)
        self.assertEqual(self.read(self.paths.leftovers), left)
        _, actions = self.run_profile("off")
        weights = [
            a for a in self.sets(actions) if a.startswith(f"{SYS} set-property --runtime machine.slice CPUWeight=")
        ]
        self.assertEqual(
            weights,
            [
                f"{SYS} set-property --runtime machine.slice CPUWeight=10",
                f"{SYS} set-property --runtime machine.slice CPUWeight=70",
            ],
        )
        self.assertEqual(self.now("sys", "machine.slice", "CPUWeight"), "70")
        self.assertFalse(os.path.exists(self.paths.leftovers))

    def test_a_retry_that_fails_again_is_said_by_off_and_not_by_the_tick(self) -> None:
        self.failed_off()
        left = self.read(self.paths.leftovers)
        self.systemd.fail_set.add(("sys", "machine.slice"))
        out, _ = self.run_profile("off")
        self.assertIn("off: could not undo machine.slice CPUWeight=70", out)
        self.assertEqual(self.read(self.paths.leftovers), left)
        self.no_game()
        out, actions = self.run_profile("tick")
        self.assertEqual(out, "")
        self.assertEqual(len(self.sets(actions)), 1)
        self.assertEqual(self.read(self.paths.leftovers), left)

    def test_a_kept_record_of_this_game_state_is_undone_once(self) -> None:
        # An 'off' killed after it wrote game.leftovers and before it removed game.state left both.
        self.prior_machine_weight()
        self.run_profile("on")
        self.write(self.paths.leftovers, "cst-game-leftovers 1\nprop sys machine.slice CPUWeight existed 70\n")
        _, actions = self.run_profile("off")
        self.assertEqual(
            [a for a in self.sets(actions) if "CPUWeight=70" in a],
            [f"{SYS} set-property --runtime machine.slice CPUWeight=70"],
        )
        self.assertFalse(os.path.exists(self.paths.leftovers))

    def user_slice_override(self) -> None:
        """The toolboxes' user.slice had a runtime override CPUWeight=30 before the game (existed)."""
        self.write(self.ctrl("usr", "user.slice", "CPUWeight"), "[Slice]\nCPUWeight=30\n")

    def test_a_user_manager_s_record_is_undone_for_the_user_it_was_kept_for(self) -> None:
        self.user_slice_override()
        self.run_profile("on")
        self.systemd.fail_set.add(("usr", "user.slice"))
        self.run_profile("off")
        self.systemd.fail_set.clear()
        self.assertEqual(
            self.read(self.paths.leftovers),
            f"cst-game-leftovers 2\nuser {UID}\nprop usr user.slice CPUWeight existed 30\n",
        )
        # TARGET_USER names another user now: the kept record is still undone for the user it was kept for.
        other = dataclasses.replace(self.settings, target_user="other")
        out = io.StringIO()
        p = GameProfile(
            self.paths,
            other,
            self.systemd,
            uid_of=lambda u: UID if u == USER else "1234",
            name_of=lambda uid: USER if uid == UID else "",
            out=out,
        )
        self.systemd.actions = []
        self.assertEqual(p.off(), 0)
        self.assertEqual(self.sets(self.systemd.actions), [f"{USR} set-property --runtime user.slice CPUWeight=30"])
        self.assertFalse(os.path.exists(self.paths.leftovers))

    def accounts_profile(self, target: str) -> tuple[GameProfile, io.StringIO]:
        """A profile whose TARGET_USER is target, on accounts where only USER (uid UID) has one."""
        out = io.StringIO()
        p = GameProfile(
            self.paths,
            dataclasses.replace(self.settings, target_user=target),
            self.systemd,
            uid_of=lambda u: UID if u == USER else "",
            name_of=lambda uid: USER if uid == UID else "",
            out=out,
        )
        return p, out

    def kept_for_the_user(self, target: str) -> tuple[GameProfile, io.StringIO]:
        """A game.leftovers whose record of the user manager, for USER, still fails; a profile whose
        TARGET_USER is target (accounts_profile)."""
        self.write(
            self.paths.leftovers, f"cst-game-leftovers 1\nuser {UID}\nprop usr user.slice CPUWeight existed 30\n"
        )
        self.systemd.fail_set.add(("usr", "user.slice"))
        os.makedirs(os.path.join(self.paths.run_user, UID, "systemd"), exist_ok=True)  # USER is logged in
        return self.accounts_profile(target)

    def two_users_profile(self) -> tuple[GameProfile, io.StringIO]:
        """A profile for USER (uid UID), on accounts where "other" (uid 1001) has one too."""
        out = io.StringIO()
        p = GameProfile(
            self.paths,
            self.settings,
            self.systemd,
            uid_of=lambda u: {USER: UID, "other": "1001"}.get(u, ""),
            name_of=lambda uid: {UID: USER, "1001": "other"}.get(uid, ""),
            out=out,
        )
        return p, out

    @staticmethod
    def reloads(actions: Sequence[str]) -> list[str]:
        return [a for a in actions if a.endswith(" daemon-reload")]

    def test_each_user_manager_an_override_was_removed_in_gets_its_daemon_reload(self) -> None:
        # 'off' undoes game.state's new overrides of USER's manager, and a kept record of another user's
        # (uid 1001, whose manager runs), left by an earlier failed 'off' for that user: USER's manager gets
        # its daemon-reload, not the manager of the user acted for last.
        os.makedirs(os.path.join(self.paths.run_user, "1001", "systemd"))
        self.run_profile("on")
        self.assertIn("prop usr user.slice CPUWeight new -\n", self.read(self.state))
        self.write(
            self.paths.leftovers, "cst-game-leftovers 1\nuser 1001\nprop usr background.slice CPUWeight existed 100\n"
        )
        p, _ = self.two_users_profile()
        self.systemd.actions = []
        self.assertEqual(p.off(), 0)
        self.assertIn(
            "systemctl --user -M other@ set-property --runtime background.slice CPUWeight=100", self.systemd.actions
        )
        self.assertEqual(self.reloads(self.systemd.actions), [f"{SYS} daemon-reload", f"{USR} daemon-reload"])
        self.assertFalse(os.path.exists(self.paths.leftovers))
        # A retry of two kept new overrides, one of each user's manager: each manager is reloaded.
        self.write(
            self.paths.leftovers,
            f"cst-game-leftovers 1\nuser {UID}\nprop usr app.slice CPUWeight new -\n"
            "user 1001\nprop usr app.slice MemoryLow new -\n",
        )
        self.no_game()
        p, out = self.two_users_profile()
        self.systemd.actions = []
        self.assertEqual(p.tick(), 0)
        self.assertEqual(out.getvalue().count("undid"), 2, out.getvalue())
        self.assertEqual(
            self.reloads(self.systemd.actions),
            ["systemctl --user -M other@ daemon-reload", f"{USR} daemon-reload"],
        )
        self.assertEqual(p.s.target_user, USER)

    def test_the_tick_while_off_judges_target_user_not_the_user_of_a_kept_record(self) -> None:
        # A GameMode client runs (game_scratch's) and TARGET_USER is unset or names no account: 'on' would
        # refuse, so the tick must not start cpu-game.service, whatever user a kept record is for.
        for target in ("", "nobody-here"):
            with self.subTest(target=target):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(self.paths.settings_warned)
                p, out = self.kept_for_the_user(target)
                self.systemd.actions = []
                self.assertEqual(p.tick(), 0)
                # The kept record was tried for the user it is for, and is still kept.
                self.assertEqual(
                    self.sets(self.systemd.actions), [f"{USR} set-property --runtime user.slice CPUWeight=30"]
                )
                self.assertTrue(os.path.exists(self.paths.leftovers))
                self.assertNotIn("switching on", out.getvalue())
                self.assertNotIn(f"{SYS} start --no-block cpu-game.service", self.systemd.actions)
                self.assertIn("GameMode client running, profile not switched on: TARGET_USER", out.getvalue())
                self.assertEqual(p.s.target_user, target)

    def test_a_kept_record_whose_uid_has_no_account_is_never_undone_for_another_user(self) -> None:
        # The record was kept for uid 1001, whose account is gone now; TARGET_USER is USER (another uid).
        text = "cst-game-leftovers 1\nuser 1001\nprop usr app.slice MemoryZSwapWriteback existed yes\n"
        self.no_game()
        # Its user manager is still there (/run/user/1001/systemd): kept as it is, for that uid.
        os.makedirs(os.path.join(self.paths.run_user, "1001", "systemd"))
        for action in ("tick", "off"):
            with self.subTest(action=action):
                self.write(self.paths.leftovers, text)
                p, out = self.accounts_profile(USER)
                self.systemd.actions = []
                self.assertEqual(p.tick() if action == "tick" else p.off(), 0)
                self.assertEqual(self.sets(self.systemd.actions), [])
                # Kept as it is (written back in format 2).
                self.assertEqual(self.read(self.paths.leftovers), text.replace("leftovers 1", "leftovers 2"))
                self.assertNotIn("undid", out.getvalue())
                if action == "off":
                    self.assertIn("off: could not undo app.slice MemoryZSwapWriteback=yes: kept in", out.getvalue())
        # Its user manager is gone (a logout): nothing is left to undo, the record is done.
        shutil.rmtree(os.path.join(self.paths.run_user, "1001"))
        p, _ = self.accounts_profile(USER)
        self.systemd.actions = []
        self.assertEqual(p.tick(), 0)
        self.assertEqual(self.sets(self.systemd.actions), [])
        self.assertFalse(os.path.exists(self.paths.leftovers))

    def test_an_entry_of_no_user_after_one_of_a_uid_is_never_undone_in_that_uid_s_manager(self) -> None:
        # TARGET_USER names no account. The pass undoes the newest entry first: uid 1001's ("other", an account
        # whose manager runs), in its own manager. The entry of no user before it is then dropped (no user manager
        # to undo it in), not undone in other's manager: the old pass still acted for other there, and removed
        # other's own override of user.slice.
        os.makedirs(os.path.join(self.paths.run_user, "1001", "systemd"))
        own = os.path.join(self.paths.run_user, "1001", "systemd", "user.control", "user.slice.d", "50-CPUWeight.conf")
        self.write(own, "[Slice]\nCPUWeight=40\n")
        self.write(
            self.paths.leftovers,
            "cst-game-leftovers 2\nuser -\nprop usr user.slice CPUWeight new -\n"
            "user 1001\nprop usr e2e.slice MemoryLow new -\n",
        )
        self.no_game()
        out = io.StringIO()
        p = GameProfile(
            self.paths,
            dataclasses.replace(self.settings, target_user="nobody-here"),
            self.systemd,
            uid_of=lambda u: {"other": "1001"}.get(u, ""),
            name_of=lambda uid: {"1001": "other"}.get(uid, ""),
            out=out,
        )
        self.assertEqual(p.tick(), 0)
        self.assertTrue(os.path.exists(own))
        self.assertIn(
            "could not undo user.slice CPUWeight (its runtime override): TARGET_USER names no account, no user"
            " manager to undo it in: dropped",
            out.getvalue(),
        )
        self.assertIn("undid e2e.slice MemoryLow (its runtime override), kept in", out.getvalue())
        self.assertFalse(os.path.exists(self.paths.leftovers))
        self.assertEqual(p.s.target_user, "nobody-here")

    def test_off_keeps_another_uid_s_entry_of_the_same_record_as_game_state_s(self) -> None:
        # uid 1001's account is gone while its user manager runs: an earlier 'off' kept its new override of
        # user.slice's CPUWeight under its uid. USER's own 'on' records the same text. USER's 'off' undoes its own
        # and keeps 1001's (the old 'off' dropped it as a record of this game.state: no entry, no warning).
        os.makedirs(os.path.join(self.paths.run_user, "1001", "systemd"))
        kept = "cst-game-leftovers 2\nuser 1001\nprop usr user.slice CPUWeight new -\n"
        self.write(self.paths.leftovers, kept)
        self.run_profile("on")
        self.assertIn("prop usr user.slice CPUWeight new -\n", self.read(self.state))
        p, out = self.accounts_profile(USER)
        self.assertEqual(p.off(), 0)
        self.assertFalse(os.path.exists(self.ctrl("usr", "user.slice", "CPUWeight")))  # USER's undone
        self.assertEqual(self.read(self.paths.leftovers), kept)
        self.assertIn("off: could not undo user.slice CPUWeight (its runtime override): kept in", out.getvalue())

    def test_a_retry_says_one_user_s_entry_undone_when_another_s_of_the_same_record_is_kept(self) -> None:
        # Two kept entries of one record, USER's and uid 1001's (no account now, its user manager runs). USER's is
        # undone and said so; 1001's is kept (the old retry took both for one entry: no line for USER's).
        os.makedirs(os.path.join(self.paths.run_user, "1001", "systemd"))
        mine = self.ctrl("usr", "user.slice", "CPUWeight")
        self.write(mine, "[Slice]\nCPUWeight=idle\n")
        rec = "prop usr user.slice CPUWeight new -\n"
        self.write(self.paths.leftovers, f"cst-game-leftovers 2\nuser {UID}\n{rec}user 1001\n{rec}")
        self.no_game()
        p, out = self.accounts_profile(USER)
        self.assertEqual(p.tick(), 0)
        self.assertFalse(os.path.exists(mine))
        self.assertIn(
            f"undid user.slice CPUWeight (its runtime override), kept in {self.paths.leftovers} by an earlier off",
            out.getvalue(),
        )
        self.assertEqual(self.read(self.paths.leftovers), f"cst-game-leftovers 2\nuser 1001\n{rec}")

    def foreign_state(self) -> str:
        """A game.state of an 'on' for uid 1001 (another uid than USER's), with records of its user manager; USER
        has a runtime override of user.slice's CPUWeight of its own. The user.slice record's override path."""
        self.write(
            self.state,
            "prop sys user@1001.service MemoryLow existed 0\n"
            "prop usr user.slice CPUWeight new -\n"
            "prop usr app.slice CPUWeight existed 100\n",
        )
        user_override = self.ctrl("usr", "user.slice", "CPUWeight")  # USER's own override
        self.write(user_override, "[Slice]\nCPUWeight=30\n")
        return user_override

    def test_off_drops_game_state_s_user_manager_records_of_a_uid_no_account_has(self) -> None:
        # 'on' acted for uid 1001, whose account is gone now, and so is its user manager (no runtime folder);
        # TARGET_USER is USER (another uid). Its user manager's records are not undone in USER's manager, nor
        # kept for USER's uid: what they held went with that manager, dropped, said once.
        user_override = self.foreign_state()
        self.systemd.fail_set.add(("usr", "app.slice"))
        p, out = self.accounts_profile(USER)
        self.systemd.actions = []
        self.assertEqual(p.off(), 0)
        self.assertEqual(
            self.sets(self.systemd.actions), [f"{SYS} set-property --runtime user@1001.service MemoryLow=0"]
        )
        self.assertNotIn(f"{USR} daemon-reload", self.systemd.actions)
        self.assertTrue(os.path.exists(user_override))
        self.assertFalse(os.path.exists(self.paths.leftovers))
        self.assertEqual(out.getvalue().count("dropped"), 1, out.getvalue())
        self.assertIn(
            "<4>game-profile: off: the user manager of uid 1001, the user 'on' acted for, is not running: 2"
            " setting(s) of it went with it, dropped\n",
            out.getvalue(),
        )
        self.assertFalse(os.path.exists(self.state))

    def test_off_calls_no_stopped_user_manager_of_the_recorded_uid(self) -> None:
        # G7: 'on' acted for uid 1001 ("other", an account), whose user manager is not running now (a logout):
        # 'off' undoes the system manager's records and drops the user manager's, with one line; it calls
        # neither that manager (the old 'off' ran set-property and daemon-reload into it) nor USER's.
        user_override = self.foreign_state()
        p, out = self.two_users_profile()
        self.systemd.actions = []
        self.assertEqual(p.off(), 0)
        self.assertEqual([a for a in self.systemd.actions if "--user" in a], [])
        self.assertEqual(
            self.sets(self.systemd.actions), [f"{SYS} set-property --runtime user@1001.service MemoryLow=0"]
        )
        self.assertIn(
            "<4>game-profile: off: the user manager of uid 1001, the user 'on' acted for, is not running: 2"
            " setting(s) of it went with it, dropped\n",
            out.getvalue(),
        )
        self.assertNotIn("undoing for", out.getvalue())
        self.assertTrue(os.path.exists(user_override))
        self.assertFalse(os.path.exists(self.paths.leftovers))
        self.assertFalse(os.path.exists(self.state))

    def test_off_keeps_the_records_of_a_uid_no_account_has_while_its_manager_runs(self) -> None:
        # G7: no account has uid 1001 now, but its user manager still runs (its runtime folder is there): its
        # records stay live there, so 'off' keeps them in game.leftovers under that uid, never undone in USER's
        # manager (the old 'off' dropped them as if undone). A tick once that manager is gone drops them.
        user_override = self.foreign_state()
        os.makedirs(os.path.join(self.paths.run_user, "1001", "systemd"))
        p, out = self.accounts_profile(USER)
        self.systemd.actions = []
        self.assertEqual(p.off(), 0)
        self.assertEqual([a for a in self.systemd.actions if "--user" in a], [])
        left = self.paths.leftovers
        self.assertEqual(
            self.read(left),
            "cst-game-leftovers 2\nuser 1001\nprop usr user.slice CPUWeight new -\n"
            "prop usr app.slice CPUWeight existed 100\n",
        )
        said = out.getvalue()
        self.assertIn(
            "<5>game-profile: off: no account has uid 1001, the user 'on' acted for, now, and its user manager still"
            " runs: its 2 setting(s) are not undone in another user's manager\n",
            said,
        )
        self.assertIn(f"<4>game-profile: off: could not undo app.slice CPUWeight=100: kept in {left}", said)
        self.assertTrue(said.endswith(f"restored, except 2 setting(s) kept in {left}\n"), said)
        self.assertTrue(os.path.exists(user_override))
        self.assertFalse(os.path.exists(self.state))
        # A tick while off keeps them while that manager runs, and drops them once it is gone.
        self.no_game()
        p, _ = self.accounts_profile(USER)
        self.systemd.actions = []
        self.assertEqual(p.tick(), 0)
        self.assertEqual([a for a in self.systemd.actions if "--user" in a], [])
        self.assertTrue(os.path.exists(left))
        shutil.rmtree(os.path.join(self.paths.run_user, "1001"))
        p, _ = self.accounts_profile(USER)
        self.systemd.actions = []
        self.assertEqual(p.tick(), 0)
        self.assertEqual([a for a in self.systemd.actions if "--user" in a], [])
        self.assertFalse(os.path.exists(left))
        self.assertTrue(os.path.exists(user_override))

    def test_a_transient_unit_that_is_gone_is_no_leftover(self) -> None:
        # A Docker scope with an override of its own before the game, which ended before 'off'.
        self.systemd.units["sys"] = ["docker-1.scope"]
        self.set_now("sys", "docker-1.scope", Slice="system.slice", AllowedCPUs="0-23")
        self.write(self.ctrl("sys", "docker-1.scope", "AllowedCPUs"), "[Scope]\nAllowedCPUs=0-23\n")
        self.run_profile("on")
        self.assertIn("prop sys docker-1.scope AllowedCPUs existed 0-23\n", self.read(self.state))
        self.systemd.fail_set.add(("sys", "docker-1.scope"))
        self.systemd.not_loaded.add(("sys", "docker-1.scope"))
        out, actions = self.run_profile("off")
        self.assertIn(f"{SYS} set-property --runtime docker-1.scope AllowedCPUs=0-23", actions)
        self.assertNotIn("could not undo", out)
        self.assertFalse(os.path.exists(self.paths.leftovers))

    def test_a_fixed_unit_not_loaded_stays_a_leftover(self) -> None:
        self.prior_machine_weight()
        self.run_profile("on")
        self.systemd.fail_set.add(("sys", "machine.slice"))
        self.systemd.not_loaded.add(("sys", "machine.slice"))
        self.run_profile("off")
        self.assertIn("prop sys machine.slice CPUWeight existed 70\n", self.read(self.paths.leftovers))

    def test_a_user_manager_gone_after_a_logout_is_no_leftover(self) -> None:
        self.user_slice_override()
        self.run_profile("on")
        self.systemd.fail_set.add(("usr", "user.slice"))
        shutil.rmtree(os.path.join(self.tmp, "ru", UID, "systemd"))  # the logout took every runtime override
        out, _ = self.run_profile("off")
        self.assertNotIn("could not undo", out)
        self.assertFalse(os.path.exists(self.paths.leftovers))

    def test_an_undo_reports_its_failure(self) -> None:
        from cpu.game.application.leftovers import Reloads

        p, _ = self.profile()
        reload = Reloads()
        self.systemd.fail_set.add(("sys", "machine.slice"))
        self.assertFalse(p.undo_record(("prop", "sys", "machine.slice", "CPUWeight", "existed", "70"), reload))
        folder = os.path.join(self.tmp, "epp-folder")
        os.makedirs(folder)  # a folder in place of the sysfs file
        self.assertFalse(p.undo_record(("sysfs", folder, "x"), reload))
        busy = self.ctrl("sys", "a.slice", "CPUWeight")
        os.makedirs(os.path.join(busy, "inside"))  # a non-empty folder where the override file is: EISDIR
        self.assertFalse(p.undo_record(("prop", "sys", "a.slice", "CPUWeight", "new", "-"), reload))
        self.assertTrue(p.undo_record(("prop", "sys", "b.slice", "CPUWeight", "new", "-"), reload))  # not there
        self.assertTrue(p.undo_record(("bogus",), reload))  # no record: nothing to undo

    def test_an_override_that_cannot_be_removed_runs_no_daemon_reload(self) -> None:
        # An 'off' kept machine.slice's IOWeight override, which cannot be removed (a non-empty folder in its
        # place: EISDIR): the ticks that retry it keep it and reload no systemd for it (nothing was removed).
        self.write(self.paths.leftovers, "cst-game-leftovers 1\nprop sys machine.slice IOWeight new -\n")
        text = "cst-game-leftovers 2\nuser -\nprop sys machine.slice IOWeight new -\n"  # written back in format 2
        os.makedirs(os.path.join(self.ctrl("sys", "machine.slice", "IOWeight"), "inside"))
        self.no_game()
        for _ in range(2):
            _, actions = self.run_profile("tick")
            self.assertNotIn(f"{SYS} daemon-reload", actions)
            self.assertEqual(self.read(self.paths.leftovers), text)
        # An 'off' with no game.state retries it the same way, and says it.
        out, actions = self.run_profile("off")
        self.assertIn("off: could not undo machine.slice IOWeight (its runtime override): kept in", out)
        self.assertNotIn(f"{SYS} daemon-reload", actions)
        self.assertEqual(self.read(self.paths.leftovers), text)

    def test_a_file_whose_folder_is_gone_is_no_leftover(self) -> None:
        # A cgroup file 'on' wrote, whose cgroup ended before 'off' (a logout took user@UID.service's tree).
        gone = os.path.join(self.ucg, "user.slice", "cpuset.cpus")
        self.run_profile("on")
        with open(self.state, "a") as f:
            f.write(f"cgw {gone} 0-23\n")
        out, _ = self.run_profile("off")
        self.assertNotIn("could not undo", out)
        self.assertFalse(os.path.exists(self.paths.leftovers))
        # The same for a kept one, also an EPP file of a CPU taken offline; the folder made again later (the
        # next login) does not get the old value.
        epp_gone = self.paths.epp("7")
        self.write(self.paths.leftovers, f"cst-game-leftovers 1\ncgw {gone} 0-23\nepp 7 balance_performance\n")
        self.no_game()
        out, _ = self.run_profile("tick")
        self.assertEqual(out.count("undid"), 2, out)
        self.assertFalse(os.path.exists(self.paths.leftovers))
        os.makedirs(os.path.dirname(gone))
        self.run_profile("tick")
        self.assertFalse(os.path.exists(gone))
        self.assertFalse(os.path.exists(epp_gone))
        # A folder that is a file is no folder gone: that undo is kept (ENOTDIR).
        self.write(os.path.join(self.tmp, "plain"), "a file\n")
        self.write(self.paths.leftovers, f"cst-game-leftovers 1\nsysfs {self.tmp}/plain/knob OLD\n")
        self.run_profile("tick")
        self.assertEqual(
            self.read(self.paths.leftovers), f"cst-game-leftovers 2\nuser -\nsysfs {self.tmp}/plain/knob OLD\n"
        )

    def test_a_file_that_cannot_be_written_is_said_lost_and_game_state_still_goes(self) -> None:
        self.prior_machine_weight()
        self.run_profile("on")
        self.systemd.fail_set.add(("sys", "machine.slice"))
        with self.ports(leftovers=FullLeftovers(self.paths.leftovers)):
            out, _ = self.run_profile("off")
        self.assertIn(
            f"<3>game-profile: off: could not keep machine.slice CPUWeight=70 in {self.paths.leftovers}: not undone\n",
            out,
        )
        self.assertTrue(out.endswith("restored, except 1 setting(s) not undone\n"), out)  # none is kept
        self.assertNotIn("kept in", out)  # said not undone, once: never kept first
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.paths.leftovers))

    def test_a_tick_that_cannot_write_the_file_says_the_lost_ones_once(self) -> None:
        # game.leftovers holds two records: machine.slice's undo works now, user.slice's fails again; /run is
        # full. The old file goes (else both would be undone and said again at every tick), and the lost one
        # is said once, in the tick's words.
        self.prior_machine_weight()
        text = (
            "cst-game-leftovers 1\nprop sys machine.slice CPUWeight existed 70\n"
            f"user {UID}\nprop usr user.slice CPUWeight existed 30\n"
        )
        self.write(self.paths.leftovers, text)
        os.makedirs(os.path.join(self.paths.run_user, UID, "systemd"), exist_ok=True)
        self.systemd.fail_set.add(("usr", "user.slice"))
        self.no_game()
        with self.ports(leftovers=FullLeftovers(self.paths.leftovers)):
            out, _ = self.run_profile("tick")
            self.assertEqual(
                out,
                f"<5>game-profile: undid machine.slice CPUWeight=70, kept in {self.paths.leftovers} by an earlier off\n"
                f"<3>game-profile: could not keep user.slice CPUWeight=30 in {self.paths.leftovers}: not undone\n",
            )
            self.assertFalse(os.path.exists(self.paths.leftovers))
            out, actions = self.run_profile("tick")
            self.assertEqual((out, self.sets(actions)), ("", []))

    def test_a_full_run_leaves_a_file_that_holds_no_record_undone_since(self) -> None:
        # game.leftovers holds a record that still fails (a sysfs file under a plain file: ENOTDIR); /run is
        # full during one tick, which undid nothing: the file is right as it is, so it stays, the tick says
        # nothing, and the next tick retries the record.
        self.write(os.path.join(self.tmp, "plain"), "a file\n")
        text = f"cst-game-leftovers 1\nsysfs {self.tmp}/plain/knob OLD\n"
        self.write(self.paths.leftovers, text)
        self.no_game()
        full = FullLeftovers(self.paths.leftovers)
        with self.ports(leftovers=full):
            out, _ = self.run_profile("tick")
        self.assertTrue(full.writes)
        self.assertEqual(out, "")
        self.assertEqual(self.read(self.paths.leftovers), text)
        os.unlink(os.path.join(self.tmp, "plain"))  # the next tick, with room in /run: its folder is gone, done
        out, _ = self.run_profile("tick")
        self.assertIn(f"undid {self.tmp}/plain/knob = OLD", out)
        self.assertFalse(os.path.exists(self.paths.leftovers))

    def test_a_full_run_at_off_keeps_the_old_file_and_says_the_rest_not_undone(self) -> None:
        # The same in an 'off' whose own machine.slice record fails too: the old file still holds its record,
        # which stays kept; machine.slice's, which it cannot hold, is said not undone.
        self.write(os.path.join(self.tmp, "plain"), "a file\n")
        text = f"cst-game-leftovers 1\nsysfs {self.tmp}/plain/knob OLD\n"
        self.write(self.paths.leftovers, text)
        self.prior_machine_weight()
        self.run_profile("on")
        self.systemd.fail_set.add(("sys", "machine.slice"))
        with self.ports(leftovers=FullLeftovers(self.paths.leftovers)):
            out, _ = self.run_profile("off")
        left = self.paths.leftovers
        self.assertEqual(self.read(left), text)
        self.assertIn(f"<4>game-profile: off: could not undo {self.tmp}/plain/knob = OLD: kept in {left}", out)
        self.assertIn(f"<3>game-profile: off: could not keep machine.slice CPUWeight=70 in {left}: not undone\n", out)
        self.assertTrue(out.endswith(f"restored, except 2 setting(s): 1 kept in {left}, 1 not undone\n"), out)

    def test_the_tick_prints_no_id_line_for_a_kept_record(self) -> None:
        # Kept while TARGET_USER named no account (no "user" line); it still names none. The tick says nothing
        # on stderr (id's "no such user" line is 'off''s alone: _lookup_uid); 'off' says it once.
        self.no_game()
        for action, said in (("tick", ""), ("off", "id: 'nobody-here': no such user\n")):
            with self.subTest(action=action):
                self.write(self.paths.leftovers, "cst-game-leftovers 1\nprop usr app.slice CPUWeight new -\n")
                p, _ = self.accounts_profile("nobody-here")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(p.tick() if action == "tick" else p.off(), 0)
                self.assertEqual(err.getvalue(), said)

    def test_a_kept_new_override_of_no_user_s_manager_is_dropped(self) -> None:
        # Kept while TARGET_USER named no account (no "user" line), and it still names none: no user manager
        # holds the override, so nothing is reloaded and nothing said undone; it is dropped, in one warning.
        self.no_game()
        for action, who in (("tick", ""), ("off", "off: ")):
            with self.subTest(action=action):
                self.write(self.paths.leftovers, "cst-game-leftovers 1\nprop usr app.slice CPUWeight new -\n")
                p, out = self.accounts_profile("nobody-here")
                self.systemd.actions = []
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(p.tick() if action == "tick" else p.off(), 0)
                self.assertEqual(self.systemd.actions, [])
                self.assertEqual(
                    out.getvalue(),
                    f"<4>game-profile: {who}could not undo app.slice CPUWeight (its runtime override): TARGET_USER"
                    " names no account, no user manager to undo it in: dropped\n",
                )
                self.assertFalse(os.path.exists(self.paths.leftovers))

    def test_a_newer_file_and_a_failed_undo_end_with_not_undone(self) -> None:
        newer = "cst-game-leftovers 9\nprop sys machine.slice CPUWeight existed 70\n"
        self.write(self.paths.leftovers, newer)
        self.prior_machine_weight()
        self.run_profile("on")
        self.systemd.fail_set.add(("sys", "machine.slice"))
        out, _ = self.run_profile("off")
        self.assertIn(f"<3>game-profile: off: could not keep machine.slice CPUWeight=70 in {self.paths.leftovers}", out)
        self.assertNotIn("kept in", out)  # said not undone, once: never kept first
        self.assertTrue(out.endswith("restored, except 1 setting(s) not undone\n"), out)
        self.assertEqual(self.read(self.paths.leftovers), newer)

    def test_a_newer_file_is_left_alone(self) -> None:
        newer = "cst-game-leftovers 9\nprop sys machine.slice CPUWeight existed 70\n"
        self.write(self.paths.leftovers, newer)
        out, actions = self.run_profile("off")
        self.assertIn(f"{self.paths.leftovers} is format 9 of the game-leftovers", out)
        self.assertIn("left alone, neither undone nor removed", out)
        self.assertEqual(self.sets(actions), [])
        self.assertEqual(self.read(self.paths.leftovers), newer)
        self.no_game()
        out, actions = self.run_profile("tick")
        self.assertEqual((out, self.sets(actions)), ("", []))

    def test_a_file_that_cannot_be_read_is_left_alone(self) -> None:
        # A folder in game.leftovers' place (also what an EIO or a denied read does): 'off' and the tick neither
        # take it for a missing file nor remove it; 'off' still undoes game.state's records and removes it.
        os.makedirs(os.path.join(self.paths.leftovers, "inside"))
        self.prior_machine_weight()
        self.run_profile("on")
        out, _ = self.run_profile("off")
        self.assertIn(
            f"<4>game-profile: {self.paths.leftovers}: cannot be read (Is a directory): left alone, neither", out
        )
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.now("sys", "machine.slice", "CPUWeight"), "70")
        self.assertTrue(os.path.isdir(os.path.join(self.paths.leftovers, "inside")))
        self.no_game()
        out, actions = self.run_profile("tick")
        self.assertEqual((out, self.sets(actions)), ("", []))
        out, _ = self.run_profile("off")
        self.assertIn("cannot be read (Is a directory): left alone", out)
        self.assertTrue(os.path.isdir(os.path.join(self.paths.leftovers, "inside")))

    def test_a_failed_system_reload_in_off_is_kept_and_run_by_the_next_tick(self) -> None:
        # G2: the system manager's daemon-reload fails in 'off' (after the drop-ins and its new overrides went):
        # it is kept as a reload due, said after the file holds it, and the next tick while off runs it. The old
        # 'off' never read the reload's exit status: the drop-ins' MemoryLow stayed live until a reboot.
        self.run_profile("on")
        self.systemd.fail_reload.add("sys")
        out, _ = self.run_profile("off")
        self.systemd.fail_reload.clear()
        left = self.paths.leftovers
        self.assertFalse(os.path.exists(self.state))
        self.assertEqual(self.read(left), "cst-game-leftovers 2\nuser -\nreload sys\n")
        self.assertIn(
            f"<4>game-profile: off: the daemon-reload of the system manager failed: kept in {left} for the next off"
            " and tick\n",
            out,
        )
        self.assertTrue(out.endswith(f"restored, except 1 daemon-reload(s) kept in {left}\n"), out)
        out, _ = self.run_profile("state")
        self.assertIn(
            "  leftovers: 1 daemon-reload(s) 'off' could not finish, retried by the next off and tick: daemon-reload"
            " of the system manager\n",
            out,
        )
        self.no_game()
        out, actions = self.run_profile("tick")
        self.assertEqual(actions, [f"{SYS} daemon-reload"])
        self.assertIn(
            f"<5>game-profile: ran the daemon-reload of the system manager, kept in {left} by an earlier off", out
        )
        self.assertFalse(os.path.exists(left))
        _, actions = self.run_profile("tick")
        self.assertEqual(actions, [])

    def test_each_user_manager_s_failed_reload_is_kept_for_its_uid(self) -> None:
        # G4: 'off' undoes USER's new overrides and a kept one of uid 1001 ("other", its manager runs); other's
        # daemon-reload fails, USER's works: only other's is kept, under its uid; the next off runs it.
        os.makedirs(os.path.join(self.paths.run_user, "1001", "systemd"))
        self.run_profile("on")
        self.write(self.paths.leftovers, "cst-game-leftovers 1\nuser 1001\nprop usr background.slice CPUWeight new -\n")
        self.systemd.fail_reload.add("other")
        p, out = self.two_users_profile()
        self.systemd.actions = []
        self.assertEqual(p.off(), 0)
        self.systemd.fail_reload.clear()
        self.assertEqual(
            self.reloads(self.systemd.actions),
            [f"{SYS} daemon-reload", f"{USR} daemon-reload", "systemctl --user -M other@ daemon-reload"],
        )
        self.assertEqual(self.read(self.paths.leftovers), "cst-game-leftovers 2\nuser 1001\nreload usr\n")
        self.assertIn("off: the daemon-reload of uid 1001's user manager failed: kept in", out.getvalue())
        p, out = self.two_users_profile()
        self.systemd.actions = []
        self.assertEqual(p.off(), 0)
        self.assertEqual(self.reloads(self.systemd.actions), ["systemctl --user -M other@ daemon-reload"])
        self.assertFalse(os.path.exists(self.paths.leftovers))
        # A reload due in a user manager that is not running any more is done with no call.
        self.write(self.paths.leftovers, "cst-game-leftovers 2\nuser 1001\nreload usr\n")
        shutil.rmtree(os.path.join(self.paths.run_user, "1001"))
        self.no_game()
        p, out = self.two_users_profile()
        self.systemd.actions = []
        self.assertEqual(p.tick(), 0)
        self.assertEqual(self.systemd.actions, [])
        self.assertFalse(os.path.exists(self.paths.leftovers))
        self.assertNotIn("ran", out.getvalue())

    def test_an_off_killed_after_it_removed_the_dropins_leaves_the_reload_due(self) -> None:
        # G3: game.state holds no new override of the system manager (its records are of overrides that existed)
        # and the drop-ins are there; an 'off' killed right after it removed them left their MemoryLow live with
        # no record. 'off' writes the reload due first, so the next 'off' runs it. The old one ran none.
        self.write(self.state, f"prop sys user@{UID}.service MemoryLow existed 0\n")
        unit_runtime = self.unit_runtime

        class DyingAfterTheDropins(GameUnitFiles):
            """'off' dies right after it removed the last drop-in (its folder)."""

            @override
            def remove_folder(self, path: str) -> None:
                super().remove_folder(path)
                if not self.dropins(unit_runtime, "zz-cst-game.conf"):
                    raise _OnDiedError

        with self.ports(units=DyingAfterTheDropins()):
            p, _ = self.profile()
        p.write_game_dropins()
        dropin = os.path.join(self.unit_runtime, f"user@{UID}.service.d", "zz-cst-game.conf")
        self.assertTrue(os.path.exists(dropin))
        with self.assertRaises(_OnDiedError):
            p.off()
        self.assertFalse(os.path.exists(dropin))
        self.assertTrue(os.path.exists(self.state))
        self.assertEqual(self.read(self.paths.leftovers), "cst-game-leftovers 2\nuser -\nreload sys\n")
        out, actions = self.run_profile("off")
        self.assertIn(f"{SYS} daemon-reload", actions)
        self.assertNotIn("failed", out)
        self.assertFalse(os.path.exists(self.paths.leftovers))
        self.assertFalse(os.path.exists(self.state))

    def test_a_reload_due_that_cannot_be_kept_is_said_not_done(self) -> None:
        # G6: the system manager's reload fails and /run is full: never said kept, said not done once.
        self.run_profile("on")
        self.systemd.fail_reload.add("sys")
        with self.ports(leftovers=FullLeftovers(self.paths.leftovers)):
            out, _ = self.run_profile("off")
        left = self.paths.leftovers
        self.assertIn(
            f"<3>game-profile: off: could not keep the daemon-reload of the system manager in {left}: not done\n", out
        )
        self.assertNotIn("kept in", out)
        self.assertTrue(out.endswith("restored, except 1 daemon-reload(s) not done\n"), out)
        self.assertFalse(os.path.exists(left))

    def test_state_names_them(self) -> None:
        self.failed_off()
        out, _ = self.run_profile("state")
        self.assertIn(
            "  leftovers: 1 setting(s) 'off' could not undo, retried by the next off and tick: machine.slice CPUWeight\n",
            out,
        )

    def test_an_off_whose_undos_all_work_writes_no_file(self) -> None:
        self.prior_machine_weight()
        self.run_profile("on")
        out, _ = self.run_profile("off")
        self.assertFalse(os.path.exists(self.paths.leftovers))
        self.assertTrue(out.endswith("EPP settings restored\n"), out)
        out, _ = self.run_profile("state")
        self.assertNotIn("leftovers", out)

    def test_a_failed_undo_of_another_uid_s_entry_stays_that_uid_s(self) -> None:
        # USER's 'off' retries uid 1001's kept entry; its undo, in the manager of other (uid 1001's name), fails
        # again. The entry stays uid 1001's, even where the name other now looks up to another uid (1002: the
        # accounts changed between the two lookups), never the uid of the user acted for.
        for uid in ("1001", "1002"):  # both user managers run
            os.makedirs(os.path.join(self.paths.run_user, uid, "systemd"))
        kept = "cst-game-leftovers 2\nuser 1001\nprop usr background.slice CPUWeight existed 100\n"
        self.write(self.paths.leftovers, kept)
        self.systemd.fail_set.add(("usr", "background.slice"))
        out = io.StringIO()
        p = GameProfile(
            self.paths,
            self.settings,
            self.systemd,
            uid_of=lambda u: {USER: UID, "other": "1002"}.get(u, ""),
            name_of=lambda uid: {UID: USER, "1001": "other"}.get(uid, ""),
            out=out,
        )
        p.off()
        self.assertIn("-M other@ set-property --runtime background.slice CPUWeight=100", " ".join(self.systemd.actions))
        self.assertEqual(self.read(self.paths.leftovers), kept)
        self.assertIn("off: could not undo background.slice CPUWeight=100", out.getvalue())

    def test_a_file_that_cannot_be_written_keeps_one_user_s_entry_not_another_s_of_the_same_record(self) -> None:
        # uid 1001's entry and USER's of one record both failed; the file held 1001's and cannot be written: it
        # is left as it is, so 1001's entry is still kept and USER's is said lost.
        from cpu.game.application.leftovers import keep

        rec = ("prop", "usr", "user.slice", "CPUWeight", "new", "-")
        out = io.StringIO()
        ports = machine_ports(self.paths, self.systemd, uid_of=lambda _u: UID, out=out)
        p = GameProfileSteps(
            self.paths, self.settings, dataclasses.replace(ports, leftovers=FullLeftovers(self.paths.leftovers))
        )
        self.assertEqual(keep(p, [(UID, rec), ("1001", rec)], [("1001", rec)]), 1)
        self.assertEqual(out.getvalue().count("could not keep"), 1, out.getvalue())
        self.assertEqual(out.getvalue().count("kept in"), 1, out.getvalue())


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
            with contextlib.redirect_stderr(err):
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


class FullLeftovers(GameLeftoversFile):
    """game.leftovers in a full /run: each write fails with ENOSPC (writes: how many were tried)."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.writes = 0

    @override
    def write(self, text: str) -> None:
        self.writes += 1
        raise OSError(errno.ENOSPC, "No space left on device")


class FullAfter(GameStateFiles):
    """The state files in a /run that fills up: the writes of target ("a": appends, "w": creations) fail with
    ENOSPC after records of them."""

    def __init__(self, target: str, records: int, *, mode: str = "a") -> None:
        self.target, self.records, self.mode, self.written = target, records, mode, 0

    def _count(self, path: str, mode: str) -> None:
        if path == self.target and mode == self.mode:
            self.written += 1
            if self.written > self.records:
                raise OSError(errno.ENOSPC, "No space left on device", path)

    @override
    def append(self, path: str, text: str) -> None:
        self._count(path, "a")
        super().append(path, text)

    @override
    def create(self, path: str, text: str) -> None:
        self._count(path, "w")
        super().create(path, text)


class FullRunFolderTest(GameProfileCase):
    """/run full (ENOSPC) while 'on' writes game.state: a change whose record cannot be written is not
    made (off could not undo it), and 'on' still ends with exit 0, so cpu-game.service stays active and
    its 'off' (ExecStop) runs later. A traceback there left the oneshot unit failed: its ExecStop never
    ran, and the watchdog's and 'game off''s stop did nothing, so the profile stayed on until reboot."""

    def full_after(
        self, records: int, *, mode: str = "a", target: str | None = None
    ) -> contextlib.AbstractContextManager[object]:
        """The game profile's own writes of target (game.state by default) in mode fail with ENOSPC after
        records of them (for the profiles made meanwhile)."""
        return self.ports(state=FullAfter(self.state if target is None else target, records, mode=mode))

    def test_on_skips_what_it_cannot_record_and_still_succeeds_then_off_undoes_the_rest(self) -> None:
        self.systemd.actions = []
        err = io.StringIO()
        with self.full_after(6), contextlib.redirect_stderr(err):  # the memory chain's and machine.slice's records fit
            p, out = self.profile()
            self.assertEqual(p.locked("on"), 0)
        log = out.getvalue()
        self.assertIn(
            f"<4>game-profile: user.slice: CPUWeight=idle: not done, its record could not be written to {self.state}\n",
            log,
        )
        self.assertIn("<5>game-profile: on: ", log)
        self.assertIn(f"{self.state}: No space left on device\n", err.getvalue())
        sets = self.sets(self.systemd.actions)
        self.assertEqual(
            sets[:4],
            [
                f"{SYS} set-property --runtime user@{UID}.service MemoryLow=16G",
                f"{SYS} set-property --runtime user-{UID}.slice MemoryLow=16G",
                f"{SYS} set-property --runtime user.slice MemoryLow=16G",
                f"{SYS} set-property --runtime machine.slice AllowedCPUs=12-23 CPUWeight=10 IOWeight=10",
            ],
        )
        # The user's user.slice is never changed without its record.
        self.assertFalse([a for a in sets if a.startswith(USR) and " user.slice " in a])
        self.assertEqual(self.now("usr", "user.slice", "CPUWeight"), "30")
        self.assertTrue(os.path.exists(self.ctrl("sys", "machine.slice", "CPUWeight")))
        # What 'on' recorded, 'off' undoes.
        out, _ = self.run_profile("off")
        self.assertFalse(os.path.exists(self.ctrl("sys", "machine.slice", "CPUWeight")))
        self.assertFalse(os.path.exists(self.state))

    def test_on_that_cannot_create_game_state_changes_nothing(self) -> None:
        self.systemd.actions = []
        with self.full_after(0, mode="w"):
            p, out = self.profile()
            self.assertEqual(p.locked("on"), 1)
        self.assertIn(
            f"<3>game-profile: not switched on: could not write {self.state}: No space left on device\n", out.getvalue()
        )
        self.assertEqual(self.sets(self.systemd.actions), [])

    def test_on_whose_game_intended_cannot_be_written_still_switches_on(self) -> None:
        # No drift repair then; the profile itself still works.
        self.systemd.actions = []
        err = io.StringIO()
        with self.full_after(0, mode="w", target=self.intended), contextlib.redirect_stderr(err):
            p, out = self.profile()
            self.assertEqual(p.locked("on"), 0)
        self.assertEqual(err.getvalue(), f"{self.intended}: No space left on device\n")
        self.assertIn("<5>game-profile: on: ", out.getvalue())
        self.assertIn(f"prop sys user@{UID}.service MemoryLow new -\n", self.read(self.state))
        self.assertIn(
            f"{SYS} set-property --runtime machine.slice AllowedCPUs=12-23 CPUWeight=10 IOWeight=10",
            self.systemd.actions,
        )
        self.assertNotIn("cst-game-intended", self.read(self.intended))

    def test_on_skips_each_p_core_whose_epp_record_cannot_be_written(self) -> None:
        for cpu in ("0", "1"):
            self.write(self.paths.epp(cpu), "balance_performance\n")
        s = GameSettings(target_user=USER, p_cores="0-1")
        # How many records a whole 'on' writes before the first EPP record.
        self.run_profile("on", s)
        before_epp = [line for line in self.read(self.state).splitlines() if not line.startswith("epp ")]
        self.run_profile("off", s)
        self.assertFalse(os.path.exists(self.state))
        err = io.StringIO()
        with self.full_after(len(before_epp)), contextlib.redirect_stderr(err):
            p, out = self.profile(s)
            self.assertEqual(p.locked("on"), 0)
        log = out.getvalue()
        for cpu in ("0", "1"):  # the first one does not stop the loop
            self.assertIn(
                f"<4>game-profile: cpu{cpu}: EPP performance: not done, its record could not be written to {self.state}\n",
                log,
            )
            self.assertEqual(self.read(self.paths.epp(cpu)), "balance_performance\n")
        self.assertIn("<5>game-profile: on: ", log)
        self.assertEqual(err.getvalue(), f"{self.state}: No space left on device\n" * 2)
        self.assertNotIn("epp ", self.read(self.state))


class GameProfilePowerTest(GameProfileCase):
    """Dynamic GPU discovery (render offload + connected fallback), AMD 3D_FULL_SCREEN, and game C-states."""

    def _setup_gpus(self) -> tuple[str, str]:
        pci1 = os.path.join(self.tmp, "pci", "0000:04:00.0")
        pci3 = os.path.join(self.tmp, "pci", "0000:84:00.0")
        self.write(os.path.join(pci1, "uevent"), "DRIVER=xe\nPCI_SLOT_NAME=0000:04:00.0\n")
        self.write(os.path.join(pci1, "power", "control"), "auto\n")
        self.write(os.path.join(pci3, "uevent"), "DRIVER=amdgpu\nPCI_SLOT_NAME=0000:84:00.0\n")
        self.write(os.path.join(pci3, "power", "control"), "auto\n")
        self.write(os.path.join(pci3, "power_dpm_force_performance_level"), "auto\n")
        self.write(
            os.path.join(pci3, "pp_power_profile_mode"),
            "PROFILE_INDEX(NAME)\n 0 BOOTUP_DEFAULT*:\n 1 3D_FULL_SCREEN :\n",
        )
        for node, target in (("card1", pci1), ("renderD128", pci1), ("card3", pci3), ("renderD130", pci3)):
            ndir = os.path.join(self.paths.sys_drm, node)
            os.makedirs(ndir, exist_ok=True)
            os.symlink(target, os.path.join(ndir, "device"))
        self.write(os.path.join(self.paths.sys_drm, "card1", "card1-DP-1", "status"), "disconnected\n")
        self.write(os.path.join(self.paths.sys_drm, "card3", "card3-DP-7", "status"), "connected\n")
        return pci1, pci3

    def test_render_offload_gpu_boost_and_reverse_order_revert(self) -> None:
        pci1, pci3 = self._setup_gpus()
        fd_dir = os.path.join(self.paths.proc, "1", "fd")
        os.makedirs(fd_dir, exist_ok=True)
        os.symlink("/dev/dri/renderD128", os.path.join(fd_dir, "9"))

        self.run_profile("on")
        self.assertEqual(self.read(os.path.join(pci1, "power", "control")), "on\n")
        self.assertEqual(self.read(os.path.join(pci3, "power", "control")), "auto\n")
        self.assertIn(
            f"sysfs {os.path.join(self.paths.sys_drm, 'card1', 'device', 'power', 'control')} auto",
            self.read(self.state),
        )

        self.run_profile("off")
        self.assertEqual(self.read(os.path.join(pci1, "power", "control")), "auto\n")

    def test_a_cpu_whose_cpuidle_folder_goes_meanwhile_does_not_stop_on(self) -> None:
        # r36.1: CPU 0 taken offline between the power step's isdir and its listdir of cpuidle. 'on' ends in
        # success with its records (the old adapter's listdir raised, 'on' failed and cpu-game.service with it,
        # so its ExecStop never undid what 'on' had set).
        gone = os.path.join(self.paths.sys_cpu, "cpu0", "cpuidle")

        class Vanishing(GameSysfsFiles):
            @override
            def is_dir(self, path: str) -> bool:
                return path == gone or super().is_dir(path)

        p_cores = dataclasses.replace(self.settings, p_cores="0")
        with self.ports(power=Vanishing()):
            out, _ = self.run_profile("on", p_cores)  # asserts exit 0
        self.assertIn(f"prop sys user@{UID}.service MemoryLow", self.read(self.state))
        self.assertIn("<5>game-profile: on: ", out)
        self.run_profile("off", p_cores)
        self.assertFalse(os.path.exists(self.state))

    def test_card0_is_a_gpu_card(self) -> None:
        # The DRM node card0 (number 0) is a card like card1: on a machine whose only GPU is card0,
        # its connected display picks it and on keeps it out of runtime suspend.
        pci0 = os.path.join(self.tmp, "pci", "0000:00:02.0")
        self.write(os.path.join(pci0, "uevent"), "DRIVER=xe\nPCI_SLOT_NAME=0000:00:02.0\n")
        self.write(os.path.join(pci0, "power", "control"), "auto\n")
        node = os.path.join(self.paths.sys_drm, "card0")
        os.makedirs(node, exist_ok=True)
        os.symlink(pci0, os.path.join(node, "device"))
        self.write(os.path.join(node, "card0-DP-1", "status"), "connected\n")

        self.run_profile("on")
        self.assertEqual(self.read(os.path.join(pci0, "power", "control")), "on\n")
        self.assertIn(
            f"sysfs {os.path.join(self.paths.sys_drm, 'card0', 'device', 'power', 'control')} auto",
            self.read(self.state),
        )

        self.run_profile("off")
        self.assertEqual(self.read(os.path.join(pci0, "power", "control")), "auto\n")

    def test_amd_profile_without_a_current_row_keeps_dpm_auto(self) -> None:
        # pp_power_profile_mode with no "*" row: there is no profile to restore, so DPM is not forced to
        # manual (it would stay there with the profile unchanged) and nothing is recorded for it.
        _, pci3 = self._setup_gpus()
        self.write(
            os.path.join(pci3, "pp_power_profile_mode"),
            "PROFILE_INDEX(NAME)\n 0 BOOTUP_DEFAULT :\n 1 3D_FULL_SCREEN :\n",
        )
        self.run_profile("on")
        self.assertEqual(self.read(os.path.join(pci3, "power", "control")), "on\n")
        self.assertEqual(self.read(os.path.join(pci3, "power_dpm_force_performance_level")), "auto\n")
        self.assertNotIn("power_dpm_force_performance_level", self.read(self.state))
        self.run_profile("off")
        self.assertEqual(self.read(os.path.join(pci3, "power", "control")), "auto\n")

    def test_steam_client_render_fds_do_not_pick_the_game_gpu(self) -> None:
        pci1, pci3 = self._setup_gpus()

        def holds(pid: str, comm: str, node: str) -> None:
            fd_dir = os.path.join(self.paths.proc, pid, "fd")
            os.makedirs(fd_dir, exist_ok=True)
            os.symlink(f"/dev/dri/{node}", os.path.join(fd_dir, "9"))
            self.write(os.path.join(self.paths.proc, pid, "comm"), comm + "\n")

        # Only steamwebhelper holds the iGPU (card1, no monitor): the connected dGPU (card3) is the one.
        holds("1", "steamwebhelper", "renderD128")
        self.run_profile("on")
        self.assertEqual(self.read(os.path.join(pci1, "power", "control")), "auto\n")
        self.assertEqual(self.read(os.path.join(pci3, "power", "control")), "on\n")
        self.run_profile("off")
        self.assertEqual(self.read(os.path.join(pci3, "power", "control")), "auto\n")

    def test_steam_shader_and_driver_helpers_do_not_pick_the_game_gpu(self) -> None:
        # While a game loads, Steam's fossilize_replay and its driver probes hold the iGPU's render node
        # in the Steam scope: they are the client, not the game; the connected dGPU (card3) is the one.
        pci1, pci3 = self._setup_gpus()
        for pid, comm in (("1", "fossilize_repla"), ("2", "vulkandriverque"), ("3", "gldriverquery")):
            fd_dir = os.path.join(self.paths.proc, pid, "fd")
            os.makedirs(fd_dir, exist_ok=True)
            os.symlink("/dev/dri/renderD128", os.path.join(fd_dir, "9"))
            self.write(os.path.join(self.paths.proc, pid, "comm"), comm + "\n")
        self.run_profile("on")
        self.assertEqual(self.read(os.path.join(pci1, "power", "control")), "auto\n")
        self.assertEqual(self.read(os.path.join(pci3, "power", "control")), "on\n")
        self.run_profile("off")
        self.assertEqual(self.read(os.path.join(pci3, "power", "control")), "auto\n")

    def test_connected_amd_fallback_3d_fullscreen_and_strict_write_order(self) -> None:
        _, pci3 = self._setup_gpus()
        writes: list[tuple[str, str]] = []

        class TrackedSysfs(GameSysfsFiles):
            @override
            def write(self, path: str, value: str) -> bool:
                writes.append((path.rsplit("/", 1)[-1], value))
                return super().write(path, value)

        class TrackedKernel(GameKernelFiles):
            @override
            def write(self, path: str, value: str) -> bool:
                writes.append((path.rsplit("/", 1)[-1], value))
                return super().write(path, value)

        with self.ports(power=TrackedSysfs(), kernel=TrackedKernel()):
            self.run_profile("on")
            self.assertEqual(self.read(os.path.join(pci3, "power", "control")), "on\n")
            self.assertEqual(self.read(os.path.join(pci3, "power_dpm_force_performance_level")), "manual\n")
            self.assertEqual(self.read(os.path.join(pci3, "pp_power_profile_mode")), "1\n")
            self.assertEqual(
                writes,
                [("control", "on"), ("power_dpm_force_performance_level", "manual"), ("pp_power_profile_mode", "1")],
            )
            writes.clear()
            self.run_profile("off")
            self.assertEqual(
                writes,
                [("pp_power_profile_mode", "0"), ("power_dpm_force_performance_level", "auto"), ("control", "auto")],
            )
            self.assertEqual(self.read(os.path.join(pci3, "power", "control")), "auto\n")
            self.assertEqual(self.read(os.path.join(pci3, "power_dpm_force_performance_level")), "auto\n")
            self.assertEqual(self.read(os.path.join(pci3, "pp_power_profile_mode")), "0\n")

    def test_tick_boosts_late_render_offload_without_duplicate_records(self) -> None:
        pci1, pci3 = self._setup_gpus()
        # Simulate card3 already controlled (on + manual/3D_FULL_SCREEN) during initial `on`.
        self.write(os.path.join(pci3, "power", "control"), "on\n")
        self.write(os.path.join(pci3, "power_dpm_force_performance_level"), "manual\n")
        self.write(os.path.join(pci3, "pp_power_profile_mode"), " 0 BOOTUP_DEFAULT:\n 1 3D_FULL_SCREEN*:\n")
        self.run_profile("on")
        self.assertEqual(self.read(os.path.join(pci1, "power", "control")), "auto\n")

        fd_dir = os.path.join(self.paths.proc, "1", "fd")
        os.makedirs(fd_dir, exist_ok=True)
        os.symlink("/dev/dri/renderD128", os.path.join(fd_dir, "12"))
        self.run_profile("tick")
        self.assertEqual(self.read(os.path.join(pci1, "power", "control")), "on\n")
        recs_after_first = self.read(self.state)
        self.run_profile("tick")
        self.assertEqual(self.read(self.state), recs_after_first)
        self.run_profile("off")
        self.assertEqual(self.read(os.path.join(pci1, "power", "control")), "auto\n")

    def test_a_cstate_latency_that_does_not_read_is_skipped(self) -> None:
        # More digits than int() reads (main: ValueError) or no number: that state is left alone.
        for lat in ("1" * 5000, "abc", ""):
            with self.subTest(lat=lat[:24]):
                for st, st_lat in (("state1", "127"), ("state2", lat)):
                    self.write(os.path.join(self.paths.sys_cpu, "cpu0", "cpuidle", st, "latency"), f"{st_lat}\n")
                    self.write(os.path.join(self.paths.sys_cpu, "cpu0", "cpuidle", st, "disable"), "0\n")
                s = GameSettings(target_user=USER, p_cores="0", game_cstate_max_latency_us="50")
                self.run_profile("on", s)
                self.assertEqual(self.read(os.path.join(self.paths.sys_cpu, "cpu0/cpuidle/state1/disable")), "1\n")
                self.assertEqual(self.read(os.path.join(self.paths.sys_cpu, "cpu0/cpuidle/state2/disable")), "0\n")
                self.run_profile("off", s)
                self.assertEqual(self.read(os.path.join(self.paths.sys_cpu, "cpu0/cpuidle/state1/disable")), "0\n")

    def test_game_cstates_on_p_cores_compose_with_baseline_disabled_states(self) -> None:
        for cpu in ("0", "1", "12"):
            for st, lat, dis in (("state0", "0", "0"), ("state1", "127", "0"), ("state2", "1048", "1")):
                self.write(os.path.join(self.paths.sys_cpu, f"cpu{cpu}", "cpuidle", st, "latency"), f"{lat}\n")
                self.write(os.path.join(self.paths.sys_cpu, f"cpu{cpu}", "cpuidle", st, "disable"), f"{dis}\n")

        s = GameSettings(target_user=USER, p_cores="0-1", game_cstate_max_latency_us="50", game_zswap_writeback="yes")
        self.run_profile("on", s)
        for cpu in ("0", "1"):
            self.assertEqual(self.read(os.path.join(self.paths.sys_cpu, f"cpu{cpu}/cpuidle/state0/disable")), "0\n")
            self.assertEqual(self.read(os.path.join(self.paths.sys_cpu, f"cpu{cpu}/cpuidle/state1/disable")), "1\n")
            self.assertEqual(self.read(os.path.join(self.paths.sys_cpu, f"cpu{cpu}/cpuidle/state2/disable")), "1\n")
        self.assertEqual(self.read(os.path.join(self.paths.sys_cpu, "cpu12/cpuidle/state1/disable")), "0\n")

        self.run_profile("off", s)
        for cpu in ("0", "1"):
            self.assertEqual(self.read(os.path.join(self.paths.sys_cpu, f"cpu{cpu}/cpuidle/state1/disable")), "0\n")
            # Pre-existing baseline-disabled C3_ACPI stays disabled after game ends.
            self.assertEqual(self.read(os.path.join(self.paths.sys_cpu, f"cpu{cpu}/cpuidle/state2/disable")), "1\n")

        self._setup_gpus()
        s_off = GameSettings(
            target_user=USER,
            p_cores="0-1",
            gpu_pm_mode="off",
            game_cstate_max_latency_us="off",
            game_zswap_writeback="yes",
        )
        self.run_profile("on", s_off)
        self.assertEqual(self.read(os.path.join(self.paths.sys_cpu, "cpu0/cpuidle/state1/disable")), "0\n")
        self.assertEqual(self.read(os.path.join(self.tmp, "pci", "0000:84:00.0", "power", "control")), "auto\n")
        self.run_profile("off", s_off)


if __name__ == "__main__":
    unittest.main()

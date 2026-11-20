# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the cpu module: vm-pin XML rewriting, cpu-watch frame-time
and run-summary helpers, cpu-watch quiet (fake /proc files and clock). The game
profile's tests are in test_game_profile.py, cpu-setup.sh's in test_cpu_setup.py.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s cpu/tests -t . -v
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
import csv
import dataclasses
import difflib
import functools
import io
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import ClassVar, override
import unittest.mock
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True

from cpu import adapters as cpu_adapters, cli as cpu_cli, domain as cpu_domain
from cst_core.domain.errors import Refused

HERE = os.path.dirname(os.path.abspath(__file__))
CPU = os.path.dirname(HERE)
REPO = os.path.dirname(CPU)
VM_PIN = os.path.join(CPU, "vm-pin")
CPU_WATCH = os.path.join(CPU, "cpu-watch")

SAMPLE_DOMAIN_XML = """\
<domain type="kvm" xmlns:libosinfo="http://libosinfo.org/xmlns/libvirt/domain/1.0">
  <name>win11-devbox</name>
  <metadata>
    <libosinfo:libosinfo>
      <libosinfo:os id="http://microsoft.com/win/11"/>
    </libosinfo:libosinfo>
  </metadata>
  <vcpu placement="static" current="16">24</vcpu>
  <cputune>
    <shares>1024</shares>
    <vcpupin vcpu="0" cpuset="0-23"/>
    <emulatorpin cpuset="0-23"/>
  </cputune>
  <cpu mode="host-passthrough">
    <topology sockets="1" dies="1" clusters="1" cores="24" threads="1"/>
  </cpu>
</domain>
"""


class CpusOfTest(unittest.TestCase):
    """cpus_of reads a core type's CPU list (the kernel's cpulist format) through cst_core CpuSet."""

    def cpus(self, text: str) -> list[int]:
        with unittest.mock.patch.object(cpu_adapters, "read_text", lambda _path, default="": text or default):
            return cpu_adapters.cpus_of("cpu_core")

    def test_ranges_and_single_cpus(self) -> None:
        self.assertEqual(self.cpus("0-3,8,10-11\n"), [0, 1, 2, 3, 8, 10, 11])

    def test_a_core_type_the_machine_does_not_have_has_no_cpus(self) -> None:
        self.assertEqual(self.cpus(""), [])  # no /sys/devices/cpu_atom on a CPU without E-cores

    def test_text_that_is_no_cpu_list_raises_value_error(self) -> None:
        for text in ("a-b-c", "0-1-2", "x"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.cpus(text)


class GameCgroupTest(unittest.TestCase):
    """game_cgroup reads the game's cgroup v2 path from /proc/PID/cgroup (cst_core cgroup_v2_path),
    without its leading slash."""

    def cgroup(self, text: str) -> str:
        with unittest.mock.patch.object(cpu_adapters, "read_text", lambda _path, default="", **_kw: text or default):
            return cpu_adapters.game_cgroup(4242)

    def test_the_path_of_the_0_line_without_its_leading_slash(self) -> None:
        scope = "user.slice/user-1000.slice/user@1000.service/app.slice/app-steam-1.scope"
        self.assertEqual(self.cgroup(f"0::/{scope}\n"), scope)
        self.assertEqual(self.cgroup(f"12:cpuset:/\n1:name=systemd:/{scope}\n0::/{scope}\n"), scope)

    def test_only_the_0_line_when_other_lines_follow_it(self) -> None:
        text = "0::/user.slice/x.scope\n1:name=systemd:/user.slice/x.scope\n"
        self.assertEqual(self.cgroup(text), "user.slice/x.scope")

    def test_a_carriage_return_in_the_path_is_kept(self) -> None:
        # Only a newline ends a line of /proc/PID/cgroup; a cgroup name may hold a carriage return.
        self.assertEqual(self.cgroup("0::/user.slice/a\rb.scope\n"), "user.slice/a\rb.scope")

    def test_a_carriage_return_in_the_real_file_is_kept(self) -> None:
        # Read through cst_core's read_text from a real file: in universal-newlines mode the "\r"
        # became a newline and cgroup_v2_path cut the path there.
        with tempfile.TemporaryDirectory() as proc:
            os.mkdir(os.path.join(proc, "4242"))
            with open(os.path.join(proc, "4242", "cgroup"), "wb") as f:
                f.write(b"0::/user.slice/a\rb.scope\n")
            self.assertEqual(cpu_adapters.game_cgroup(4242, proc), "user.slice/a\rb.scope")
            self.assertEqual(cpu_adapters.game_cgroup(7, proc), "")  # no such process

    def test_no_0_line_or_no_file_is_no_cgroup(self) -> None:
        for text in ("", "1:name=systemd:/user.slice/x.scope\n", "1:name=systemd:/a::b\n"):
            with self.subTest(text=text):
                self.assertEqual(self.cgroup(text), "")


class VmPinTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.m = cpu_domain

    def test_pins_vcpus_emulator_and_topology(self) -> None:
        out = self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23")
        self.assertIn("xmlns:libosinfo=", out)
        self.assertNotIn("ns0:", out)
        root = ET.fromstring(out)
        vcpu = root.find("vcpu")
        assert vcpu is not None
        self.assertEqual(vcpu.text, "12")
        self.assertEqual(vcpu.attrib, {"placement": "static"})
        topo = root.find("cpu/topology")
        assert topo is not None
        self.assertEqual(topo.get("cores"), "12")
        self.assertEqual(topo.get("sockets"), "1")
        self.assertEqual(topo.get("threads"), "1")
        tune = root.find("cputune")
        assert tune is not None
        shares = tune.find("shares")
        assert shares is not None
        self.assertEqual(shares.text, "1024")
        pins = [(el.get("vcpu"), el.get("cpuset")) for el in tune.findall("vcpupin")]
        self.assertEqual(pins, [(str(i), "12-23") for i in range(12)])
        emupin = tune.findall("emulatorpin")
        self.assertEqual(len(emupin), 1)
        self.assertEqual(emupin[0].get("cpuset"), "12-23")

    def test_a_default_namespace_in_metadata_does_not_move_domain(self) -> None:
        xml = SAMPLE_DOMAIN_XML.replace("<metadata>", "<metadata><app xmlns='http://example.com/app/1'/>", 1)
        self.assertIn("example.com/app/1", xml)
        root = ET.fromstring(self.m.pinned(xml, 12, "12-23"))
        self.assertEqual(root.tag, "domain")
        self.assertIsNotNone(root.find("metadata/{http://example.com/app/1}app"))
        again = self.m.pinned(self.m.pinned(xml, 12, "12-23"), 12, "12-23")  # ns0: on the way back
        self.assertEqual(ET.fromstring(again).tag, "domain")

    def test_inserts_cputune_when_absent_and_is_idempotent(self) -> None:
        minimal = "<domain><name>vm</name><vcpu>8</vcpu></domain>\n"
        first = self.m.pinned(minimal, 4, "8-11")
        second = self.m.pinned(first, 4, "8-11")
        self.assertEqual(
            list(difflib.unified_diff(first.splitlines(True), second.splitlines(True))),
            [],
        )


class VmPinCpuFeaturesTest(unittest.TestCase):
    """The CET/MBEC features vm-pin keeps disabled on host-passthrough."""

    @override
    def setUp(self) -> None:
        self.m = cpu_domain
        self.names = self.m.CPU_DISABLE_DEFAULT.split()

    def features(self, out: str) -> list[tuple[str | None, str | None]]:
        return [(f.get("name"), f.get("policy")) for f in ET.fromstring(out).findall("cpu/feature")]

    def test_default_list(self) -> None:
        self.assertEqual(self.names, ["cet-ss", "cet-ibt", "vmx-mbec", "vmx-exit-save-cet", "vmx-entry-load-cet"])

    def test_self_closing_cpu(self) -> None:
        xml = "<domain><name>vm</name><vcpu>4</vcpu><cpu mode='host-passthrough' check='none'/></domain>\n"
        out = self.m.pinned(xml, 4, "8-11", self.names)
        self.assertEqual(self.features(out), [(n, "disable") for n in self.names])
        cpu = ET.fromstring(out).find("cpu")
        assert cpu is not None
        self.assertEqual(cpu.get("check"), "none")
        self.assertEqual(cpu.get("mode"), "host-passthrough")

    def test_topology_and_other_features_kept(self) -> None:
        out = self.m.pinned(
            SAMPLE_DOMAIN_XML.replace(
                '<topology sockets="1" dies="1" clusters="1" cores="24" threads="1"/>',
                '<topology sockets="1" dies="1" clusters="1" cores="24" threads="1"/>\n'
                '    <feature policy="require" name="invtsc"/>',
            ),
            12,
            "12-23",
            self.names,
        )
        cpu = ET.fromstring(out).find("cpu")
        assert cpu is not None
        self.assertEqual(cpu[0].tag, "topology")
        self.assertEqual(cpu[0].get("cores"), "12")
        self.assertEqual(self.features(out), [("invtsc", "require")] + [(n, "disable") for n in self.names])

    def test_existing_feature_switched_in_place(self) -> None:
        xml = (
            "<domain><name>vm</name><vcpu>4</vcpu><cpu mode='host-passthrough'>"
            "<feature policy='require' name='vmx-mbec'/><feature policy='disable' name='cet-ss'/>"
            "</cpu></domain>\n"
        )
        out = self.m.pinned(xml, 4, "8-11", self.names)
        got = self.features(out)
        self.assertEqual(got[:2], [("vmx-mbec", "disable"), ("cet-ss", "disable")])
        self.assertEqual(sorted(n for n, _ in got if n is not None), sorted(self.names))
        self.assertTrue(all(p == "disable" for _, p in got))

    def test_idempotent(self) -> None:
        once = self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", self.names)
        self.assertEqual(self.m.pinned(once, 12, "12-23", self.names), once)

    def test_other_modes_untouched(self) -> None:
        for cpu in (
            "<cpu mode='host-model' check='partial'/>",
            "<cpu mode='custom'><model>Skylake</model><feature policy='require' name='cet-ss'/></cpu>",
            "",
        ):
            xml = f"<domain><name>vm</name><vcpu>4</vcpu>{cpu}</domain>\n"
            base = self.m.pinned(xml, 4, "8-11")
            self.assertEqual(self.m.pinned(xml, 4, "8-11", self.names), base, cpu)

    def test_empty_knob_untouched(self) -> None:
        self.assertEqual(
            self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", []), self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23")
        )
        self.assertEqual(self.features(self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", [])), [])

    def test_conf_knob_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cpu-setup.conf")
            with open(p, "w") as f:
                f.write("VM_NAME='x'\nVM_CPU_DISABLE=''\n")
            self.assertEqual(cpu_adapters.conf(p)["VM_CPU_DISABLE"].split(), [])
            with open(p, "w") as f:
                f.write("VM_CPU_DISABLE='cet-ss vmx-mbec'\n")
            self.assertEqual(cpu_adapters.conf(p)["VM_CPU_DISABLE"].split(), ["cet-ss", "vmx-mbec"])
            self.assertEqual(cpu_adapters.conf(os.path.join(tmp, "absent"))["VM_CPU_DISABLE"].split(), self.names)

    def test_undecodable_bytes_are_replaced(self) -> None:
        # Intended change from main, where vm-pin and cpu-watch stopped with a UnicodeDecodeError
        # traceback: a non-UTF-8 byte in cpu-setup.conf, or in a mapped file's name in
        # /proc/PID/maps (the kernel does not escape it), no longer stops the program.
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cpu-setup.conf")
            with open(p, "wb") as f:
                f.write(b"VM_VCPUS=8\n# caf\xe9\n")
            self.assertEqual(cpu_adapters.conf(p)["VM_VCPUS"], "8")
            maps = os.path.join(tmp, "maps")
            with open(maps, "wb") as f:
                f.write(
                    b"7f00 r-xp 0 00:00 1 /home/u/caf\xe9/lib.so\n7f01 r-xp 0 00:00 2 /usr/lib/libgamemodeauto.so.0\n"
                )
            self.assertIn("libgamemode", cpu_adapters.read_text(maps, ""))

    def test_messages(self) -> None:
        root = ET.fromstring(self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", self.names))
        self.assertIn("CPU features disabled: cet-ss", self.m.features_note(root, self.names))
        self.assertIn("not managed", self.m.features_note(root, []))
        other = ET.fromstring("<domain><cpu mode='host-model'/></domain>")
        self.assertIn("left alone", self.m.features_note(other, self.names))
        self.assertIn("NOT disabled yet: cet-ss", self.m.cpu_line(SAMPLE_DOMAIN_XML, self.names))
        self.assertIn("; disabled: cet-ss", self.m.cpu_line(ET.tostring(root, encoding="unicode"), self.names))


class CpuWatchTest(unittest.TestCase):
    def test_pressure_fields_watch_and_quiet_read(self) -> None:
        # One parser of the pressure line format: watch's totals and quiet's avg10.
        text = "some avg10=1.25 avg60=0.50 avg300=0.10 total=123456\nfull avg10=0.75 avg60=0.00 avg300=0.00 total=789\n"
        f = cpu_domain.pressure_field
        self.assertEqual((f(text, "some", "avg10"), f(text, "full", "avg10")), ("1.25", "0.75"))
        self.assertEqual((f(text, "some", "total"), f(text, "full", "total")), ("123456", "789"))
        self.assertIsNone(f("some avg10=x avg60=0.00 total=1\n", "some", "avg10"))
        self.assertIsNone(f(text, "some", "avg6"))  # a whole key, not a prefix
        self.assertIsNone(f("some avg10=1.00 total=1\n", "full", "total"))
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cpu.pressure")
            with open(p, "w") as fh:
                fh.write(text)
            self.assertEqual(
                (cpu_adapters.pressure_total(p, "some"), cpu_adapters.pressure_total(p, "full")), (123456, 789)
            )
            self.assertIsNone(cpu_adapters.pressure_total(os.path.join(tmp, "missing"), "some"))

    def test_frametimes_and_lows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "mangohud.csv")
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["os", "Linux"])
                w.writerow(["fps", "FrameTime", "cpu_load"])
                for ft in [10.0] * 99 + [20.0]:
                    w.writerow(["100", str(ft), "50"])
                w.writerow(["bad", "not-a-number", "0"])
            ft = cpu_adapters.frametimes(p)
            self.assertEqual(len(ft), 100)
            res = cpu_domain.lows(ft)
            assert res is not None
            avg_fps, low1, low01 = res
            self.assertAlmostEqual(avg_fps, 1000 / 10.1, places=2)
            self.assertAlmostEqual(low1, 50.0, places=2)
            self.assertAlmostEqual(low01, 50.0, places=2)
            self.assertIsNone(cpu_domain.lows([]))

    def test_summarize_with_and_without_game(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.join(tmp, "20261111T120000-baseline")
            os.makedirs(run)
            with open(os.path.join(run, "meta.json"), "w") as f:
                json.dump({"label": "baseline"}, f)
            fields = [
                "t",
                "profile_on",
                "game_pid",
                "game_cpu_wait_ms",
                "game_mem_stall_ms",
                "game_invol_switches",
                "sys_cpu_some_ms",
                "p_mhz",
                "e_mhz",
                "vm_cpus_used",
                "builds_cpus_used",
            ]
            with open(os.path.join(run, "samples.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(fields)
                w.writerow([1.0, 1, 1234, 25.0, 2.0, 40, 10.0, 5200, 3800, 2.5, 4.0])
                w.writerow([2.0, 1, 1234, 35.0, 3.0, 60, 12.0, 5400, 4000, 3.5, 6.0])
            s = cpu_adapters.summarize(run)
            self.assertIn("20261111T120000-baseline", s)
            self.assertIn("game wait   3.0% (max   3.5%)", s)
            self.assertIn("mem stall       5 ms", s)
            self.assertIn("profile on", s)

            # Without a detected game PID
            with open(os.path.join(run, "samples.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(fields)
                w.writerow([1.0, 0, "", "", "", "", 5.0, 5000, 3600, 0.0, 0.0])
            s_nogame = cpu_adapters.summarize(run)
            self.assertIn("no game found", s_nogame)
            self.assertIn("profile off", s_nogame)


def sample_too_early(taken: list[int]) -> cpu_domain.QuietSample:
    """A quiet sample that must not be taken: noted in TAKEN."""
    taken.append(1)
    raise AssertionError("sampled before the usage check")


class WatchArgsTest(unittest.TestCase):
    """`cpu-watch watch`'s options, as the user types them (no run is recorded here)."""

    def watch(self, *argv: str) -> tuple[int, dict[str, object], str]:
        calls: list[dict[str, object]] = []

        def record_run(runs: str, **kw: object) -> tuple[str, int]:
            del runs
            calls.append(kw)
            return "/nonexistent/run", 3

        err = io.StringIO()
        with (
            unittest.mock.patch.object(cpu_cli, "record_run", record_run),
            unittest.mock.patch.object(cpu_cli, "summarize", lambda run_dir: f"summary of {run_dir}"),
            unittest.mock.patch("sys.stdout", io.StringIO()),
            unittest.mock.patch("sys.stderr", err),
        ):
            rc = cpu_cli.watch(list(argv))
        return rc, (calls[0] if calls else {}), err.getvalue()

    def test_every_option(self) -> None:
        argv = ("-l", "a b", "--game", "g", "--seconds", "5", "--mangohud", "/m", "--", "x", "--label", "y")
        rc, kw, _ = self.watch(*argv)
        self.assertEqual(rc, 3)
        self.assertEqual(
            kw, {"label": "a_b", "game": "g", "seconds": 5, "mangohud": "/m", "cmd": ["x", "--label", "y"]}
        )
        self.assertEqual(self.watch("--label", "z")[1]["label"], "z")

    def test_an_unknown_option_is_a_usage_error(self) -> None:
        rc, kw, err = self.watch("--mangohudX", "/m")
        self.assertEqual((rc, kw, err), (2, {}, "unknown argument --mangohudX\n"))


class WatchRowTest(unittest.TestCase):
    """cpu-watch watch: one samples.csv row from two readings of the counters."""

    PREV = cpu_domain.WatchCounters(t=100.0, sys=5_000, vm=1_000_000, builds=4_000_000, game=(10_000, 2_000, 30))
    CUR = cpu_domain.WatchCounters(t=102.0, sys=9_000, vm=4_000_000, builds=3_000_000, game=(13_000, 2_600, 90))

    def test_rates_per_second_of_wall_time(self) -> None:
        row = cpu_domain.watch_row(self.CUR, self.PREV, start=90.0, profile_on=True, pid=4242, p_mhz=4100, e_mhz=3200)
        self.assertEqual(len(row), len(cpu_domain.SAMPLE_FIELDS))
        # 2 s apart: game wait 3000 us -> 1.5 ms/s, memory stall 0.3 ms/s, 30 switches/s, system 2.0 ms/s,
        # 1.5 VM CPUs; the builds' counter went down (a cgroup gone): 0, never negative.
        self.assertEqual(row, [12.0, 1, 4242, 1.5, 0.3, 30, 2.0, 4100, 3200, 1.5, 0.0])

    def test_game_columns_stay_empty_until_two_readings_have_the_game(self) -> None:
        no_game = dataclasses.replace(self.PREV, game=None)
        for cur, prev in ((self.CUR, no_game), (dataclasses.replace(self.CUR, game=None), self.PREV)):
            with self.subTest(cur=cur.game, prev=prev.game):
                row = cpu_domain.watch_row(cur, prev, start=90.0, profile_on=False, pid=None, p_mhz=0, e_mhz=0)
                self.assertEqual(row, [12.0, 0, "", "", "", "", 2.0, 0, 0, 1.5, 0.0])


class FakeProc:
    """A fake /proc with the files `cpu-watch quiet` reads, advanced one second
    at a time: CPU busy share, MB written, each pressure's avg10, game profile."""

    HZ_CPUS = 2400  # jiffies per second over all CPUs (100 Hz x 24)
    IDLE: ClassVar[dict[str, float]] = {
        "cpu": 3.0,
        "writes": 2.5,
        "cpu-pressure": 0.3,
        "io-pressure": 1.4,
        "memory-pressure": 0.0,
        "game": False,
    }

    def __init__(self, root: str) -> None:
        self.root = root
        self.game_state = os.path.join(root, "run", "game.state")
        os.makedirs(os.path.join(root, "pressure"))
        os.makedirs(os.path.dirname(self.game_state))
        self.jiffies = [5000, 100, 2000, 900000, 3000, 50, 40, 0]  # user..steal
        self.guest = 4000
        self.kib = 7_000_000
        self.set(self.IDLE)

    def set(self, sec: Mapping[str, float]) -> None:
        """Write the files as they read after a second like `sec`."""
        busy = round(self.HZ_CPUS * sec["cpu"] / 100)
        self.jiffies[0] += busy  # user, which counts guest too
        self.guest += busy  # the VM: in user, and again in guest
        idle = self.HZ_CPUS - busy
        self.jiffies[3] += idle - idle // 4
        self.jiffies[4] += idle // 4  # iowait: idle CPUs, waiting on a disk
        self.kib += round(sec["writes"] * 1024)
        with open(os.path.join(self.root, "stat"), "w") as f:
            f.write("cpu  " + " ".join(map(str, self.jiffies)) + f" {self.guest} 0\ncpu0 1 2 3 4 5 6 7 8 0 0\n")
        with open(os.path.join(self.root, "vmstat"), "w") as f:
            f.write(f"pgpgin 123\npgpgout {self.kib}\npswpout 0\n")
        for kind in ("cpu", "io", "memory"):
            with open(os.path.join(self.root, "pressure", kind), "w") as f:
                f.write(
                    f"some avg10={sec[kind + '-pressure']:.2f} avg60=0.00 avg300=0.00 total=1\n"
                    "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
                )
        if sec["game"]:
            open(self.game_state, "w").close()
        elif os.path.exists(self.game_state):
            os.remove(self.game_state)


class QuietTest(unittest.TestCase):
    """`cpu-watch quiet`: fake /proc files, a fake clock and sleep (each sleep
    is one second: the fake files advance by that second)."""

    @override
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = FakeProc(self.tmp.name)

    def run_quiet(
        self,
        argv: Sequence[str],
        second: Callable[[int], Mapping[str, float]] = lambda i: {},
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str, int]:
        """second(i) -> what differs from an idle machine in second i (1, 2, ...).
        Returns (exit code, output, number of sleeps)."""
        clock, sleeps = [1000.0], []

        def sleep(d: float) -> None:
            sleeps.append(d)
            clock[0] += d
            self.proc.set({**FakeProc.IDLE, **second(len(sleeps))})

        out = io.StringIO()
        with unittest.mock.patch("sys.stderr", io.StringIO()):  # "waiting up to ..."
            rc = cpu_cli.quiet(
                argv,
                sample=lambda: cpu_adapters.quiet_sample(
                    proc=self.proc.root, game_state=self.proc.game_state, clock=lambda: clock[0]
                ),
                sleep=sleep,
                clock=lambda: clock[0],
                out=out,
                environ=env or {},
            )
        return rc, out.getvalue(), len(sleeps)

    def test_idle_window_is_quiet_with_each_value_beside_its_limit(self) -> None:
        rc, out, n = self.run_quiet([])
        self.assertEqual((rc, n), (0, 60))  # 60 s by default: 61 readings
        lines = out.splitlines()
        # The VM's guest time is already in user: counted once, 3 % stays 3 %.
        self.assertEqual(lines[0], "  CPU busy         3.00 %       limit < 10 %      ok")
        self.assertEqual(lines[1], "  CPU pressure     0.30 %       limit < 5 %       ok")
        self.assertEqual(lines[2], "  IO pressure      1.40 %       limit < 5 %       ok")
        self.assertEqual(lines[3], "  memory pressure  0.00 %       limit < 1 %       ok")
        self.assertEqual(lines[4], "  disk writes      2.50 MB/s    limit < 10 MB/s   ok")
        self.assertEqual(lines[5], "  game profile     off          limit off         ok")
        self.assertRegex(lines[6], r"^quiet \(60 s, \S+ \S+ to \S+ \S+\)$")
        self.assertEqual(len(lines), 7)

    def test_each_test_fails_alone_with_its_value(self) -> None:
        cases: list[tuple[str, float | bool, str]] = [
            ("cpu", 12.0, "CPU busy         12.00 %      limit < 10 %      FAIL"),
            ("cpu", 10.0, "CPU busy         10.00 %      limit < 10 %      FAIL"),  # "below" is strict
            ("cpu-pressure", 6.0, "CPU pressure     6.00 %       limit < 5 %       FAIL"),
            ("io-pressure", 7.5, "IO pressure      7.50 %       limit < 5 %       FAIL"),
            ("memory-pressure", 1.5, "memory pressure  1.50 %       limit < 1 %       FAIL"),
            ("writes", 12.0, "disk writes      12.00 MB/s   limit < 10 MB/s   FAIL"),
            ("writes", 10.0, "disk writes      10.00 MB/s   limit < 10 MB/s   FAIL"),
            ("game", True, "game profile     on in 10 of 11 readings limit off         FAIL"),
        ]
        labels = {
            "cpu": "CPU busy",
            "cpu-pressure": "CPU pressure",
            "io-pressure": "IO pressure",
            "memory-pressure": "memory pressure",
            "writes": "disk writes",
            "game": "game profile",
        }
        for key, value, line in cases:
            with self.subTest(key=key, value=value):
                self.proc = FakeProc(tempfile.mkdtemp(dir=self.tmp.name))
                rc, out, _ = self.run_quiet(["--for", "10"], lambda i, key=key, value=value: {key: value})
                self.assertEqual(rc, 1)
                self.assertIn("  " + line + "\n", out)
                self.assertEqual(out.count(" ok\n"), 5)
                self.assertRegex(out.splitlines()[-1], rf"^NOT quiet: {labels[key]} \(10 s, ")

    def test_the_verdict_matches_the_printed_value(self) -> None:
        # 2399 busy jiffies of 24000: 9.996 %, printed as 10.00 %, so it fails "< 10".
        rc, out, _ = self.run_quiet(["--for", "10"], lambda i: {"cpu": 10.0 if i < 10 else 239 / 24})
        self.assertEqual(rc, 1)
        self.assertIn("  CPU busy         10.00 %      limit < 10 %      FAIL\n", out)

    def test_ctrl_c_exits_130_without_a_result(self) -> None:
        def interrupted(d: float) -> None:
            raise KeyboardInterrupt

        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("sys.stderr", err):
            rc = cpu_cli.quiet(
                ["--for", "5"],
                sample=lambda: cpu_adapters.quiet_sample(proc=self.proc.root, game_state=self.proc.game_state),
                sleep=interrupted,
                out=out,
                environ={},
            )
        self.assertEqual((rc, out.getvalue(), err.getvalue()), (130, "", "quiet: interrupted\n"))

    def test_window_means_let_a_short_burst_through(self) -> None:
        # One second of a 10 s window at 50 % CPU and 40 MB written: the means
        # are 5 % and 4 MB/s, below the limits (a commit or a snapshot, not a build).
        rc, out, _ = self.run_quiet(
            ["--for", "10"], lambda i: {"cpu": 50.0, "writes": 40.0} if i == 4 else {"cpu": 0.0, "writes": 0.0}
        )
        self.assertEqual(rc, 0, out)
        self.assertIn("CPU busy         5.00 %", out)
        self.assertIn("disk writes      4.00 MB/s", out)

    def test_pressure_is_the_mean_of_each_seconds_avg10(self) -> None:
        # avg10 at 2 % in 5 of 10 seconds: mean 1 %, which fails the memory limit
        # (< 1 %) and passes the IO one.
        def sec(i: int) -> dict[str, float]:
            return (
                {"memory-pressure": 2.0, "io-pressure": 2.0} if i % 2 else {"memory-pressure": 0.0, "io-pressure": 0.0}
            )

        rc, out, _ = self.run_quiet(["--for", "10", "--json"], sec)
        r = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertEqual(r["failed"], ["memory-pressure"])
        t = {x["name"]: x for x in r["tests"]}
        self.assertEqual((t["memory-pressure"]["value"], t["io-pressure"]["value"]), (1.0, 1.0))

    def test_wait_slides_the_noisy_seconds_out(self) -> None:
        def noisy(i: int) -> dict[str, float]:
            return {"cpu": 90.0} if i <= 3 else {}

        rc, out, n = self.run_quiet(["--for", "5"], noisy)
        self.assertEqual((rc, n), (1, 5))  # without --wait: one window
        self.proc = FakeProc(tempfile.mkdtemp(dir=self.tmp.name))
        rc, out, n = self.run_quiet(["--for", "5", "--wait", "1m"], noisy)
        # The windows ending at seconds 5..7 hold a 90 % second; the one of seconds 4-8 does not.
        self.assertEqual((rc, n), (0, 8), out)
        self.assertIn("CPU busy         3.00 %", out)
        self.assertRegex(out.splitlines()[-1], r"^quiet \(5 s, .*, after waiting 8 s\)$")
        self.assertEqual(out.count("CPU busy"), 1)  # one result, not one per second

    def test_wait_times_out(self) -> None:
        rc, out, n = self.run_quiet(["--for", "3", "--wait", "10"], lambda i: {"writes": 50.0})
        self.assertEqual((rc, n), (1, 10))
        self.assertRegex(out.splitlines()[-1], r"^NOT quiet: disk writes \(3 s, .*, after waiting 10 s\)$")
        self.assertIn("disk writes      50.00 MB/s   limit < 10 MB/s   FAIL", out)

    def test_game_profile_on_for_one_second_breaks_the_window(self) -> None:
        rc, out, _ = self.run_quiet(["--for", "10"], lambda i: {"game": i == 7})
        self.assertEqual(rc, 1)
        self.assertIn("game profile     on in 1 of 11 readings limit off         FAIL", out)
        self.assertIn("NOT quiet: game profile", out)

    def test_unreadable_file_is_not_quiet_with_the_reason(self) -> None:
        os.remove(os.path.join(self.proc.root, "pressure", "io"))
        orig = self.proc.set

        def set_without_io(sec: Mapping[str, float]) -> None:
            orig(sec)
            os.remove(os.path.join(self.proc.root, "pressure", "io"))

        self.proc.set = set_without_io
        path = os.path.join(self.proc.root, "pressure", "io")
        rc, out, n = self.run_quiet(["--for", "5"])
        self.assertEqual((rc, n), (1, 5))
        self.assertIn(
            f"  IO pressure      unreadable   limit < 5 %       FAIL ({path}: No such file or directory)", out
        )
        self.assertIn("  CPU busy         3.00 %       limit < 10 %      ok", out)  # the others still measured
        self.assertIn("NOT quiet: IO pressure (", out)
        # It cannot become readable: --wait gives up after the first window.
        rc, out, n = self.run_quiet(["--for", "5", "--wait", "2h"])
        self.assertEqual((rc, n), (1, 5))

    def test_unparsable_files_are_not_quiet(self) -> None:
        orig = self.proc.set

        def broken(sec: Mapping[str, float]) -> None:
            orig(sec)
            with open(os.path.join(self.proc.root, "stat"), "w") as f:
                f.write("intr 1 2 3\n")
            with open(os.path.join(self.proc.root, "vmstat"), "w") as f:
                f.write("pgpgin 5\n")
            with open(os.path.join(self.proc.root, "pressure", "cpu"), "w") as f:
                f.write("some avg10=x\n")

        self.proc.set = broken
        broken(FakeProc.IDLE)
        rc, out, _ = self.run_quiet(["--for", "2", "--json"])
        r = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertEqual(r["failed"], ["cpu", "cpu-pressure", "writes"])
        t = {x["name"]: x for x in r["tests"]}
        self.assertIn("no 'cpu' line with 8 counters first", t["cpu"]["error"])
        self.assertIn("no 'pgpgout' line", t["writes"]["error"])
        self.assertIn("no 'some avg10=' line", t["cpu-pressure"]["error"])
        self.assertIsNone(t["cpu"]["value"])
        self.assertEqual(t["io-pressure"]["value"], 1.4)

    def test_json(self) -> None:
        rc, out, _ = self.run_quiet(["--for", "4", "--json"], lambda i: {"cpu": 20.0})
        self.assertEqual(rc, 1)
        r = json.loads(out)  # only the JSON on stdout
        self.assertEqual((r["quiet"], r["seconds"], r["waited_s"], r["failed"]), (False, 4, None, ["cpu"]))
        self.assertEqual(
            [t["name"] for t in r["tests"]],
            ["cpu", "cpu-pressure", "io-pressure", "memory-pressure", "writes", "game-profile"],
        )
        self.assertEqual(
            r["tests"][0],
            {"name": "cpu", "label": "CPU busy", "unit": "%", "limit": 10.0, "value": 20.0, "ok": False, "error": None},
        )
        self.assertEqual(r["tests"][4]["value"], 2.5)
        self.assertRegex(r["start"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_limits_from_flags_and_the_environment(self) -> None:
        def busy(i: int) -> dict[str, float]:
            return {"cpu": 12.0, "writes": 6.0, "memory-pressure": 1.2, "io-pressure": 5.5, "cpu-pressure": 5.5}

        env = {
            "QUIET_CPU_PCT": "20",
            "QUIET_WRITE_MBPS": "7",
            "QUIET_MEMORY_PSI": "1.5",
            "QUIET_IO_PSI": "6",
            "QUIET_CPU_PSI": "6",
        }
        rc, out, _ = self.run_quiet(["--for", "3"], busy, env=env)
        self.assertEqual(rc, 0, out)
        self.assertIn("limit < 20 %", out)
        self.assertIn("limit < 7 MB/s", out)
        # A flag wins over the environment.
        rc, out, _ = self.run_quiet(["--for", "3", "--cpu", "11", "--write-mbps", "6.5", "--json"], busy, env=env)
        r = json.loads(out)
        self.assertEqual((rc, r["failed"]), (1, ["cpu"]))
        self.assertEqual(
            {t["name"]: t["limit"] for t in r["tests"]},
            {
                "cpu": 11.0,
                "cpu-pressure": 6.0,
                "io-pressure": 6.0,
                "memory-pressure": 1.5,
                "writes": 6.5,
                "game-profile": "off",
            },
        )
        rc, _, _ = self.run_quiet(
            ["--for", "3", "--cpu-pressure", "5", "--io-pressure", "5", "--memory-pressure", "1"], busy, env=env
        )
        self.assertEqual(rc, 1)

    def test_usage_errors_exit_2_before_sampling(self) -> None:
        cases: list[tuple[list[str], dict[str, str]]] = [
            (["--for", "0"], {}),
            (["--for", "1.5"], {}),
            (["--for"], {}),
            (["--for", "5", "--wait", "3"], {}),
            (["--wait", "2x"], {}),
            (["--wait", "0"], {}),
            (["--cpu", "-1"], {}),
            (["--cpu", "abc"], {}),
            (["--write-mbps", "inf"], {}),
            (["--foo"], {}),
            (["watch"], {}),
            ([], {"QUIET_IO_PSI": "zero"}),
            ([], {"QUIET_CPU_PCT": "0"}),
        ]
        for argv, env in cases:
            with self.subTest(argv=argv, env=env):
                taken: list[int] = []
                err = io.StringIO()
                with unittest.mock.patch("sys.stderr", err):
                    rc = cpu_cli.quiet(
                        argv,
                        sample=functools.partial(sample_too_early, taken),
                        sleep=lambda d: None,
                        out=io.StringIO(),
                        environ=env,
                    )
                self.assertEqual((rc, taken), (2, []))
                self.assertTrue(err.getvalue().startswith("quiet: "), err.getvalue())

    def test_duration(self) -> None:
        self.assertEqual(
            [cpu_domain.duration(x) for x in ("120", "90s", "30m", "2h", "1.5h")], [120, 90, 1800, 7200, 5400]
        )

    def test_command_line_dispatch(self) -> None:
        r = subprocess.run(
            [sys.executable, "-I", "-B", CPU_WATCH, "quiet", "--for", "0"],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 2)
        self.assertIn("quiet: --for: a whole number of seconds", r.stderr)
        self.assertEqual(r.stdout, "")


class _CtrlDTerminal(io.StringIO):
    """stdin of a terminal where the user pressed Ctrl-D: isatty() is true, nothing to read."""

    @override
    def isatty(self) -> bool:
        return True


class FindGameTest(unittest.TestCase):
    """find_game with a game name: the first pid `pgrep -xo NAME` prints (a fake pgrep on PATH)."""

    def find(self, body: str | None) -> tuple[int | None, list[str]]:
        """find_game("dota2") with a pgrep that runs body (None: no pgrep on PATH); its result and pgrep's argv."""
        with tempfile.TemporaryDirectory() as tmp:
            args = os.path.join(tmp, "args")
            if body is not None:
                fake = os.path.join(tmp, "pgrep")
                with open(fake, "w") as f:
                    f.write(
                        "#!/usr/bin/python3 -I\n"
                        "import sys\n"
                        f"with open({args!r}, 'w') as f:\n"
                        "    f.write(' '.join(sys.argv[1:]))\n"
                        f"{body}\n"
                    )
                os.chmod(fake, 0o755)
            with unittest.mock.patch.dict(os.environ, {"PATH": tmp}):
                pid = cpu_adapters.find_game("dota2")
            argv: list[str] = []
            if os.path.exists(args):
                with open(args) as f:
                    argv = f.read().split()
        return pid, argv

    def test_the_oldest_pid_of_the_name(self) -> None:
        self.assertEqual(self.find("print('123'); print('456')"), (123, ["-xo", "dota2"]))

    def test_no_such_process(self) -> None:
        self.assertEqual(self.find("sys.exit(1)"), (None, ["-xo", "dota2"]))

    def test_a_first_word_that_is_no_pid_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.find("print('abc')")

    def test_no_pgrep(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.find(None)


class VmPinCliTest(unittest.TestCase):
    """cpu-vm-pin's command line (cpu.cli.vm_pin) with a fake virsh and settings."""

    CONF: ClassVar[dict[str, str]] = {
        "VM_NAME": "win11-devbox",
        "VM_VCPUS": "12",
        "BUILD_CPUS": "12-23",
        "VM_CPU_DISABLE": "cet-ss cet-ibt",
    }

    def run_vm_pin(self, argv: list[str], xml: str) -> tuple[int, str, list[tuple[str, ...]]]:
        calls: list[tuple[str, ...]] = []

        def fake_virsh(*args: str, check: bool = True) -> str:
            calls.append(args)
            if args[0] == "dumpxml":
                return xml
            if args[0] == "domstate":
                return "shut off\n"
            return ""

        out = io.StringIO()
        with (
            unittest.mock.patch.object(cpu_cli, "virsh", fake_virsh),
            # autospec: a call that does not fit vm_pin_settings(path, action) fails.
            unittest.mock.patch.object(cpu_cli, "vm_pin_settings", autospec=True, return_value=dict(self.CONF)),
            contextlib.redirect_stdout(out),
        ):
            rc = cpu_cli.vm_pin(argv, doc="DOC")
        return rc, out.getvalue(), calls

    def test_conf_reads_the_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cpu-setup.conf")
            with open(p, "w") as f:
                f.write("VM_NAME='box'\nVM_VCPUS=8\nBUILD_CPUS=4-7,9\nVM_CPU_DISABLE=\"cet-ss\"\nTARGET_USER=alice\n")
            c = cpu_adapters.conf(p)
        self.assertEqual(
            (c["VM_NAME"], c["VM_VCPUS"], c["BUILD_CPUS"], c["VM_CPU_DISABLE"], c["TARGET_USER"]),
            ("box", "8", "4-7,9", "cet-ss", "alice"),
        )

    def test_conf_reads_values_as_bash_does_and_refuses_problem_lines_on_apply(self) -> None:
        # The header says "values may be edited"; BUILD_CPUS='12-23' # E-cores was
        # "12-23' # E-cores" for vm-pin (written into the cpuset) and 12-23 for game-profile (bash).
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cpu-setup.conf")
            with open(p, "w") as f:
                f.write("BUILD_CPUS='12-23' # E-cores\nexport VM_VCPUS=8\n")
            c = cpu_adapters.conf(p)
            self.assertEqual((c["BUILD_CPUS"], c["VM_VCPUS"]), ("12-23", "8"))
            with open(p, "w") as f:
                f.write("VM_NAME='box'\nBUILD_CPUS = 8-11\n")
            err = io.StringIO()
            calls: list[tuple[str, ...]] = []

            def virsh(*args: str, check: bool = True) -> str:
                calls.append(args)
                return SAMPLE_DOMAIN_XML

            with (
                unittest.mock.patch.object(cpu_cli, "virsh", virsh),
                contextlib.redirect_stderr(err),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                c = cpu_adapters.conf(p)  # plan and status: printed, the key keeps vm-pin's default
                self.assertEqual(c["BUILD_CPUS"], "12-23")
                self.assertIn(f"{p} line 2: BUILD_CPUS = 8-11: blanks around =", err.getvalue())
                with self.assertRaises(SystemExit) as cm:
                    cpu_cli.vm_pin(["apply"], doc="DOC", paths=cpu_domain.CpuPaths(runs=tmp, conf=p))
            self.assertIn(f"{p} line 2: BUILD_CPUS = 8-11", str(cm.exception.code))
            self.assertIn("nothing changed", str(cm.exception.code))
            self.assertEqual(calls, [], "apply reached virsh with a default BUILD_CPUS")

    def test_plan_prints_the_diff_and_is_idempotent(self) -> None:
        rc, out, calls = self.run_vm_pin(["plan"], SAMPLE_DOMAIN_XML)
        self.assertEqual(rc, 0)
        self.assertIn("+++ pinned", out)
        self.assertIn("Plan only. Apply with: cst cpu vm-pin apply", out)
        self.assertEqual(calls, [("dumpxml", "--inactive", "--security-info", "win11-devbox")])
        after = cpu_domain.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", ["cet-ss", "cet-ibt"])
        rc, out, _ = self.run_vm_pin(["plan", "win11-devbox"], after)
        self.assertEqual(rc, 0)
        self.assertIn("win11-devbox: already 12 vCPUs pinned to 12-23; CPU features disabled: cet-ss cet-ibt.", out)

    def test_status_reports_the_features(self) -> None:
        rc, out, calls = self.run_vm_pin(["status", "box"], SAMPLE_DOMAIN_XML)
        self.assertEqual(rc, 0)
        self.assertIn("box: shut off", out)
        self.assertIn("NOT disabled yet: cet-ss cet-ibt", out)
        self.assertEqual(calls[0], ("domstate", "box"))
        after = cpu_domain.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", ["cet-ss", "cet-ibt"])
        _, out, _ = self.run_vm_pin(["status"], after)
        self.assertIn("; disabled: cet-ss cet-ibt", out)

    def test_refuses_vcpu_tied_elements_and_unknown_actions(self) -> None:
        bad_xml = "<domain><name>win11-devbox</name><vcpu>4</vcpu><cputune><vcpusched/></cputune></domain>"
        with self.assertRaisesRegex(SystemExit, "has <cputune/vcpusched>: adjust it by hand"):
            self.run_vm_pin(["plan"], bad_xml)
        rc, out, calls = self.run_vm_pin(["bogus"], SAMPLE_DOMAIN_XML)
        self.assertEqual((rc, out, calls), (2, "DOC\n", []))

    def test_save_definition_once_keeps_the_first_and_is_private_from_the_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            etc = os.path.join(tmp, "etc", "cpu-setup")
            old_umask = os.umask(0)  # 0600 comes from the open, not from the umask
            try:
                path = cpu_adapters.save_definition_once(etc, "box", "<domain>first</domain>\n")
            finally:
                os.umask(old_umask)
            self.assertEqual(path, os.path.join(etc, "box.xml.before-vm-pin"))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            # A later apply keeps the first one: it is the one to go back to.
            self.assertEqual(cpu_adapters.save_definition_once(etc, "box", "<domain>second</domain>\n"), path)
            with open(path) as f:
                self.assertEqual(f.read(), "<domain>first</domain>\n")
            self.assertEqual(os.listdir(etc), ["box.xml.before-vm-pin"])

    def test_apply_saves_the_definition_then_defines_the_pinned_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = cpu_domain.CpuPaths(runs=tmp, conf=os.path.join(tmp, "absent.conf"), etc=os.path.join(tmp, "etc"))
            defined: list[tuple[str, bool]] = []
            temp_files: list[str] = []

            def adapter_virsh(*args: str, check: bool = True) -> str:
                # define_domain's call: the temporary file still exists while virsh reads it.
                temp_files.append(args[1])
                with open(args[1]) as f:
                    defined.append((f.read(), check))
                self.assertEqual(args[0], "define")
                return ""

            def cli_virsh(*args: str, check: bool = True) -> str:
                return SAMPLE_DOMAIN_XML

            for answer in ("n", "y"):
                out = io.StringIO()
                with (
                    unittest.mock.patch.object(cpu_cli, "virsh", cli_virsh),
                    unittest.mock.patch.object(cpu_adapters, "virsh", adapter_virsh),
                    unittest.mock.patch.object(cpu_cli, "confirm", return_value=answer == "y") as confirm,
                    contextlib.redirect_stdout(out),
                ):
                    self.assertEqual(cpu_cli.vm_pin(["apply", "box"], doc="DOC", paths=paths), 0)
                self.assertIn(
                    "\nDefine box with 12 vCPUs pinned to 12-23 (CPU features to be disabled: ",
                    confirm.call_args.args[0],
                )
                if answer == "n":
                    self.assertTrue(out.getvalue().endswith("Nothing changed.\n"))
                    self.assertFalse(os.path.exists(paths.etc), "no is no: nothing saved")
                    self.assertEqual(defined, [])
            backup = os.path.join(paths.etc, "box.xml.before-vm-pin")
            with open(backup) as f:
                self.assertEqual(f.read(), SAMPLE_DOMAIN_XML)
            after = cpu_domain.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", cpu_domain.CPU_DISABLE_DEFAULT.split())
            self.assertEqual(defined, [(after, True)])
            self.assertFalse(os.path.exists(temp_files[0]), "the temporary definition is removed")
            self.assertTrue(
                out.getvalue().endswith(
                    "Defined. Effective at the next start of box (shut it down and start it when convenient).\n"
                    f"Previous definition saved in {backup} (restore: virsh -c {cpu_domain.URI} define {backup}).\n"
                )
            )

    def test_define_leaves_no_temporary_file_when_writing_it_fails(self) -> None:
        # The definition holds --security-info secrets: a failed write (here a text the
        # encoding refuses) must not leave a copy of it in the temporary folder.
        calls: list[tuple[str, ...]] = []

        def adapter_virsh(*args: str, check: bool = True) -> str:
            calls.append(args)
            return ""

        with tempfile.TemporaryDirectory() as tmp:
            old_tempdir = tempfile.tempdir
            tempfile.tempdir = tmp
            self.addCleanup(setattr, tempfile, "tempdir", old_tempdir)
            with (
                unittest.mock.patch.object(cpu_adapters, "virsh", adapter_virsh),
                self.assertRaises(UnicodeEncodeError),
            ):
                cpu_adapters.define_domain("<domain>\udc80</domain>\n")
            self.assertEqual(calls, [], "virsh is not called with a file that was not written")
            self.assertEqual(os.listdir(tmp), [], "the temporary file is removed")

    def test_ctrl_d_at_the_question_changes_nothing(self) -> None:
        # The real confirm and input(): Ctrl-D (an empty terminal) is a no, after a newline.
        with tempfile.TemporaryDirectory() as tmp:
            paths = cpu_domain.CpuPaths(runs=tmp, conf=os.path.join(tmp, "absent.conf"), etc=os.path.join(tmp, "etc"))
            calls: list[tuple[str, ...]] = []

            def cli_virsh(*args: str, check: bool = True) -> str:
                return SAMPLE_DOMAIN_XML

            def adapter_virsh(*args: str, check: bool = True) -> str:
                calls.append(args)
                return ""

            out = io.StringIO()
            with (
                unittest.mock.patch.object(cpu_cli, "virsh", cli_virsh),
                unittest.mock.patch.object(cpu_adapters, "virsh", adapter_virsh),
                unittest.mock.patch("sys.stdin", _CtrlDTerminal()),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(cpu_cli.vm_pin(["apply", "box"], doc="DOC", paths=paths), 0)
            self.assertTrue(out.getvalue().endswith("[y/N] \nNothing changed.\n"), out.getvalue()[-200:])
            self.assertFalse(os.path.exists(paths.etc), "no definition saved")
            self.assertEqual(calls, [], "no virsh define")

    def test_virsh_adapter(self) -> None:
        ok = subprocess.CompletedProcess(["virsh"], 0, "<domain/>", "")
        with unittest.mock.patch.object(cpu_adapters.subprocess, "run", return_value=ok) as run:
            self.assertEqual(cpu_adapters.virsh("dumpxml", "win11-devbox"), "<domain/>")
        self.assertEqual(run.call_args.args[0], ["virsh", "-c", "qemu:///system", "dumpxml", "win11-devbox"])
        fail = subprocess.CompletedProcess(["virsh"], 1, "partial", "boom")
        with unittest.mock.patch.object(cpu_adapters.subprocess, "run", return_value=fail):
            with self.assertRaisesRegex(Refused, "virsh dumpxml win11-devbox: boom"):
                cpu_adapters.virsh("dumpxml", "win11-devbox")
            self.assertEqual(cpu_adapters.virsh("dumpxml", "win11-devbox", check=False), "partial")

    def test_virsh_runs_in_the_c_locale_with_no_stdin_and_a_timeout(self) -> None:
        # virsh translates its answers ("shut off" is "desligado" in pt_BR); vm-pin compares them.
        ok = subprocess.CompletedProcess(["virsh"], 0, "running\n", "")
        caller = {"LC_ALL": "pt_BR.UTF-8", "LANG": "pt_BR.UTF-8"}
        with (
            unittest.mock.patch.dict(os.environ, caller),
            unittest.mock.patch.object(cpu_adapters.subprocess, "run", return_value=ok) as run,
        ):
            self.assertEqual(cpu_adapters.virsh("domstate", "win11-devbox"), "running\n")
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["env"]["LC_ALL"], "C")
        self.assertEqual(kwargs["env"]["LANG"], "pt_BR.UTF-8", "the rest of the environment is the caller's")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], cpu_adapters.VIRSH_TIMEOUT)
        self.assertEqual(kwargs["errors"], "replace")

    def test_a_virsh_that_does_not_answer(self) -> None:
        # check: Refused with one line (vm_pin stops with it, no traceback); without check the run goes on, as after a failure.
        hang = subprocess.TimeoutExpired(["virsh"], cpu_adapters.VIRSH_TIMEOUT)
        with unittest.mock.patch.object(cpu_adapters.subprocess, "run", side_effect=hang):
            with self.assertRaises(Refused) as stop:
                cpu_adapters.virsh("domstate", "win11-devbox")
            self.assertEqual(str(stop.exception), "virsh domstate win11-devbox: no answer within 60 s")
            self.assertEqual(cpu_adapters.virsh("vcpucount", "win11-devbox", check=False), "")


class CpuWatchCliTest(unittest.TestCase):
    """cpu-watch's numbers and command line, on the package (cpu.domain, cpu.cli)."""

    def test_frame_lows_and_quiet_args(self) -> None:
        res = cpu_domain.lows([10.0, 20.0])
        assert res is not None
        self.assertAlmostEqual(res[0], 1000.0 / 15.0, places=2)
        self.assertIsNone(cpu_domain.lows([]))
        seconds, wait, as_json, limits = cpu_domain.quiet_args(["--for", "5", "--json"], {"QUIET_CPU_PCT": "15"})
        self.assertEqual((seconds, wait, as_json, limits["cpu"]), (5, None, True, 15.0))

    def test_dispatch(self) -> None:
        # In-process: SIGPIPE at its default would stay so for the rest of the test run.
        with unittest.mock.patch.object(cpu_cli, "restore_default_signals") as restored:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cpu_cli.cpu_watch(["quiet", "--for", "0"], doc="DOC"), 2)
                self.assertEqual(cpu_cli.cpu_watch(["watch", "--bogus"], doc="DOC"), 2)
            self.assertIn("quiet: --for: a whole number of seconds", err.getvalue())
            self.assertIn("unknown argument --bogus", err.getvalue())
            self.assertEqual(restored.call_count, 0, "watch and quiet keep Python's SIGPIPE handling")
            for argv in ([], ["bogus"], ["--help"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cpu_cli.cpu_watch(argv, doc="DOC"), 2)
                self.assertEqual(out.getvalue(), "DOC\n")
            self.assertEqual(restored.call_count, 3, "the help only reads: SIGPIPE back at its default")
        with tempfile.TemporaryDirectory() as home:
            out = io.StringIO()
            with (
                unittest.mock.patch.dict(os.environ, {"HOME": home}),
                # In-process: SIGPIPE at its default would stay so for the rest of the test run.
                unittest.mock.patch.object(cpu_cli, "restore_default_signals") as restored,
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(cpu_cli.cpu_watch(["report"], doc="DOC"), 0)
            self.assertEqual(restored.call_count, 1)
        runs = os.path.join(home, ".local", "state", "cpu-setup", "runs")
        self.assertEqual(out.getvalue(), f"no runs in {runs}; record one with: cst cpu watch -l <label> [-- <cmd>]\n")


class ClosedPipeTest(unittest.TestCase):
    def closed_pipe(self, *args: str) -> subprocess.CompletedProcess[bytes]:
        """cpu-watch ARGS with its stdout a pipe whose reader is already gone."""
        with tempfile.TemporaryDirectory() as home:
            r, w = os.pipe()
            os.close(r)
            try:
                return subprocess.run(
                    ["/usr/bin/python3", "-I", CPU_WATCH, *args],
                    check=False,
                    stdout=w,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/bin", "HOME": home},
                    timeout=60,
                )
            finally:
                os.close(w)

    def test_report_into_a_closed_pipe_stops_quietly(self) -> None:
        # `cst cpu watch report | head -1` ended in "Exception ignored ...
        # BrokenPipeError" (exit 120). A read-only report now ends on SIGPIPE, silently.
        p = self.closed_pipe("report")
        self.assertEqual((p.returncode, p.stderr), (-signal.SIGPIPE, b""))

    def test_help_into_a_closed_pipe_stops_quietly(self) -> None:
        # `cpu-watch | head -3` (the help) ended in "Exception ignored ... BrokenPipeError", exit 120.
        for args in ((), ("help",), ("--help",)):
            p = self.closed_pipe(*args)
            self.assertEqual((p.returncode, p.stderr), (-signal.SIGPIPE, b""), args)

    def test_vm_pin_restores_sigpipe_only_for_what_only_reads(self) -> None:
        with (
            unittest.mock.patch.object(cpu_adapters, "conf", return_value={"K": "v"}) as conf,
            unittest.mock.patch.object(cpu_adapters, "restore_default_signals") as restored,
        ):
            for action in ("status", "plan", "apply"):
                self.assertEqual(cpu_adapters.vm_pin_settings("/x.conf", action), {"K": "v"})
                self.assertEqual(conf.call_args, unittest.mock.call("/x.conf", refuse=action == "apply"))
        self.assertEqual(restored.call_count, 2, "apply (a temp file removed in a finally) must keep Python's handling")


class QuietNumbersTest(unittest.TestCase):
    """cpu-watch quiet: its command line, its CPU busy number and vm-pin's feature rule, case by case."""

    def test_quiet_args(self) -> None:
        q = cpu_domain.quiet_args
        seconds, wait, as_json, limits = q([], {"QUIET_CPU_PCT": ""})
        self.assertEqual((seconds, wait, as_json), (60, None, False))
        self.assertEqual(limits, {lim.name: lim.default for lim in cpu_domain.QUIET_LIMITS})  # "" = the default
        seconds, wait, as_json, limits = q(
            ["--json", "--for", "1", "--wait", "2m", "--io-pressure", "2.5", "--write-mbps", "40"], {}
        )
        self.assertEqual((seconds, wait, as_json, limits["io-pressure"], limits["writes"]), (1, 120.0, True, 2.5, 40.0))
        self.assertEqual(q(["--for", "12", "--cpu", "3"], {})[0], 12)
        for argv in (
            ["--for"],
            ["--for", "0"],
            ["--for", "1.5"],
            ["--cpu"],
            ["--cpu", "0"],
            ["--json", "--wait"],
            ["-x"],
        ):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                q(argv, {})
        with self.assertRaises(ValueError):
            q([], {"QUIET_CPU_PCT": "-1"})

    def test_busy_pct(self) -> None:
        window: list[cpu_domain.Readings] = [{"v": {"cpu": (300, 100)}}, {"v": {"cpu": (1200, 250)}}]
        self.assertAlmostEqual(cpu_domain.busy_pct(window, "cpu"), 750 / 9)  # 900 ticks, 150 idle
        window = [{"v": {"cpu": (7, 3)}}, {"v": {"cpu": (17, 3)}}]
        self.assertAlmostEqual(cpu_domain.busy_pct(window, "cpu"), 100.0)
        for end in (1000, 999):
            with self.subTest(end=end), self.assertRaises(ValueError):
                cpu_domain.busy_pct([{"v": {"cpu": (1000, 600)}}, {"v": {"cpu": (end, 600)}}], "cpu")

    def test_disable_features_only_for_host_passthrough(self) -> None:
        for mode in ("maximum", "host-model", "custom"):
            root = ET.fromstring(f"<domain><cpu mode='{mode}'/></domain>")
            cpu_domain.disable_features(root, ["cet-ss"])
            self.assertEqual(root.findall("cpu/feature"), [], mode)


if __name__ == "__main__":
    unittest.main()

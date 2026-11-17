# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the cpu module: vm-pin XML rewriting, cpu-watch frame-time
and run-summary helpers, cpu-watch quiet (fake /proc files and clock) and its
dispatch from cpu-setup.sh (host and toolbox), game-profile CPU range expansion, on/off, drift
repair and drift report (with a fake systemd), the boxes of the user manager
it makes idle and its MemoryLow drop-ins (with a fake cgroup tree), the
baseline drop-in name and its migration, and cpu-setup.sh CLI.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s cpu/tests -t . -v
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
import csv
import difflib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from typing import Any
import unittest
import unittest.mock
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True

from cpu import adapters as cpu_adapters, cli as cpu_cli, domain as cpu_domain

HERE = os.path.dirname(os.path.abspath(__file__))
CPU = os.path.dirname(HERE)
REPO = os.path.dirname(CPU)
VM_PIN = os.path.join(CPU, "vm-pin")
CPU_WATCH = os.path.join(CPU, "cpu-watch")
GAME_PROFILE = os.path.join(CPU, "game-profile")
SETUP = os.path.join(REPO, "cpu-setup.sh")

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


class VmPinTest(unittest.TestCase):
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
        self.assertEqual(self.features(self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", "".split())), [])

    def test_conf_knob_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cpu-setup.conf")
            with open(p, "w") as f:
                f.write("VM_NAME='x'\nVM_CPU_DISABLE=''\n")
            with unittest.mock.patch.object(cpu_adapters, "CONF", p):
                self.assertEqual(cpu_adapters.conf()["VM_CPU_DISABLE"].split(), [])
                with open(p, "w") as f:
                    f.write("VM_CPU_DISABLE='cet-ss vmx-mbec'\n")
                self.assertEqual(cpu_adapters.conf()["VM_CPU_DISABLE"].split(), ["cet-ss", "vmx-mbec"])
            with unittest.mock.patch.object(cpu_adapters, "CONF", os.path.join(tmp, "absent")):
                self.assertEqual(cpu_adapters.conf()["VM_CPU_DISABLE"].split(), self.names)

    def test_undecodable_bytes_are_replaced(self) -> None:
        # Intended change from main, where vm-pin and cpu-watch stopped with a UnicodeDecodeError
        # traceback: a non-UTF-8 byte in cpu-setup.conf, or in a mapped file's name in
        # /proc/PID/maps (the kernel does not escape it), no longer stops the program.
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cpu-setup.conf")
            with open(p, "wb") as f:
                f.write(b"VM_VCPUS=8\n# caf\xe9\n")
            with unittest.mock.patch.object(cpu_adapters, "CONF", p):
                self.assertEqual(cpu_adapters.conf()["VM_VCPUS"], "8")
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


class FakeProc:
    """A fake /proc with the files `cpu-watch quiet` reads, advanced one second
    at a time: CPU busy share, MB written, each pressure's avg10, game profile."""

    HZ_CPUS = 2400  # jiffies per second over all CPUs (100 Hz x 24)
    IDLE: dict[str, Any] = {
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

    def set(self, sec: Mapping[str, Any]) -> None:
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

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proc = FakeProc(self.tmp.name)

    def run_quiet(
        self,
        argv: Sequence[str],
        second: Callable[[int], Mapping[str, Any]] = lambda i: {},
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
        sec = lambda i: (
            {"memory-pressure": 2.0, "io-pressure": 2.0} if i % 2 else {"memory-pressure": 0.0, "io-pressure": 0.0}
        )
        rc, out, _ = self.run_quiet(["--for", "10", "--json"], sec)
        r = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertEqual(r["failed"], ["memory-pressure"])
        t = {x["name"]: x for x in r["tests"]}
        self.assertEqual((t["memory-pressure"]["value"], t["io-pressure"]["value"]), (1.0, 1.0))

    def test_wait_slides_the_noisy_seconds_out(self) -> None:
        noisy = lambda i: {"cpu": 90.0} if i <= 3 else {}
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

        def set_without_io(sec: Mapping[str, Any]) -> None:
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

        def broken(sec: Mapping[str, Any]) -> None:
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
        busy = lambda i: {"cpu": 12.0, "writes": 6.0, "memory-pressure": 1.2, "io-pressure": 5.5, "cpu-pressure": 5.5}
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
                        sample=lambda taken=taken: taken.append(1),
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


class SetupQuietDispatchTest(unittest.TestCase):
    """cpu-setup.sh quiet|watch|report: on the host it runs the checkout's
    cpu-watch; inside a container it runs it on the host with flatpak-spawn,
    handing over the QUIET_* limits that are set (flatpak-spawn does not pass
    the environment on). A copy of the script whose container markers are
    temporary files, with fake python3 and flatpak-spawn on PATH."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = self.tmp.name
        os.makedirs(os.path.join(t, "cst", "cpu"))
        os.makedirs(os.path.join(t, "bin"))
        self.marker = os.path.join(t, "containerenv")
        with open(SETUP) as f:
            src = f.read()
        line = next(ln for ln in src.splitlines() if ln.startswith("in_container()"))
        patched = line.replace("/run/.containerenv", self.marker).replace("/.dockerenv", self.marker + "2")
        self.assertNotEqual(line, patched)
        self.script = os.path.join(t, "cst", "cpu-setup.sh")
        with open(self.script, "w") as f:
            f.write(src.replace(line, patched))
        open(os.path.join(t, "cst", "cpu", "cpu-watch"), "w").close()
        # Sourced at the top of cpu-setup.sh (only its install commands use it).
        shutil.copy(os.path.join(REPO, "cst-install.bash"), os.path.join(t, "cst", "cst-install.bash"))
        self.log = os.path.join(t, "log")
        for name in ("python3", "flatpak-spawn"):
            p = os.path.join(t, "bin", name)
            with open(p, "w") as f:
                f.write(f'#!/bin/bash\necho "{name} $*" >>"{self.log}"\n')
            os.chmod(p, 0o755)

    def run_setup(self, *args: str, env: Mapping[str, str] | None = None) -> tuple[int, list[str], str]:
        r = subprocess.run(
            ["bash", self.script, *args],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.path.join(self.tmp.name, "bin") + ":/usr/bin:/bin", **(env or {})},
            stdin=subprocess.DEVNULL,
        )
        log = []
        if os.path.exists(self.log):
            with open(self.log) as f:
                log = f.read().splitlines()
            os.remove(self.log)
        return r.returncode, log, r.stderr

    def test_host_runs_the_checkouts_cpu_watch(self) -> None:
        watch = os.path.join(self.tmp.name, "cst", "cpu", "cpu-watch")
        rc, log, err = self.run_setup("quiet", "--for", "5", env={"QUIET_CPU_PCT": "20"})
        self.assertEqual((rc, log), (0, [f"python3 -I -B {watch} quiet --for 5"]), err)

    def test_container_runs_it_on_the_host_with_the_limits(self) -> None:
        open(self.marker, "w").close()
        watch = os.path.join(self.tmp.name, "cst", "cpu", "cpu-watch")
        rc, log, err = self.run_setup(
            "quiet", "--wait", "2h", "--json", env={"QUIET_CPU_PCT": "20", "QUIET_WRITE_MBPS": "8", "QUIET_OTHER": "x"}
        )
        self.assertEqual(
            (rc, log),
            (
                0,
                [
                    f"flatpak-spawn --host --env=QUIET_CPU_PCT=20 --env=QUIET_WRITE_MBPS=8 "
                    f"python3 -I -B {watch} quiet --wait 2h --json"
                ],
            ),
            err,
        )
        # No limit set: nothing extra (an empty list under set -u).
        rc, log, err = self.run_setup("quiet")
        self.assertEqual((rc, log), (0, [f"flatpak-spawn --host python3 -I -B {watch} quiet"]), err)
        rc, log, err = self.run_setup("report")
        self.assertEqual((rc, log), (0, [f"flatpak-spawn --host python3 -I -B {watch} report"]), err)

    def test_container_hands_over_every_limit_cpu_watch_reads(self) -> None:
        # The list in cpu-setup.sh must follow cpu-watch's QUIET_LIMITS: a limit
        # added there but not here would be dropped inside a toolbox.
        open(self.marker, "w").close()
        envs = [lim.env for lim in cpu_domain.QUIET_LIMITS]
        self.assertTrue(envs)
        rc, log, err = self.run_setup("quiet", env={v: str(i + 1) for i, v in enumerate(envs)})
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(log), 1, log)
        for i, v in enumerate(envs):
            self.assertIn(f" --env={v}={i + 1} ", log[0])

    def test_help_lists_quiet(self) -> None:
        r = subprocess.run(
            ["bash", SETUP, "help"], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL
        )
        self.assertIn("quiet [--for SECONDS] [--wait MAX] [--json]", r.stdout)


class GameProfileAndCliTest(unittest.TestCase):
    def test_expand_cpus(self) -> None:
        cmd = 'sed -n \'/^expand_cpus()/,/^}/p\' "$1" > "$2" && . "$2" && expand_cpus \'0-3,6,12-14\''
        with tempfile.TemporaryDirectory() as tmp:
            fn_file = os.path.join(tmp, "fn.bash")
            r = subprocess.run(
                ["bash", "-c", cmd, "bash", GAME_PROFILE, fn_file],
                capture_output=True,
                text=True,
                check=True,
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual(r.stdout.strip(), "0 1 2 3 6 12 13 14")

    def test_off_reverts_in_reverse_order_and_cleans_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = os.path.join(tmp, "run")
            os.makedirs(run_dir)
            state_file = os.path.join(run_dir, "game.state")
            manual_file = os.path.join(run_dir, "manual")
            intended_file = os.path.join(run_dir, "game.intended")
            cg_file = os.path.join(tmp, "cpuset.cpus")
            new_dropin = os.path.join(tmp, "50-CPUWeight.conf")
            log_file = os.path.join(tmp, "actions.log")

            with open(cg_file, "w") as f:
                f.write("12-23\n")
            with open(new_dropin, "w") as f:
                f.write("[Slice]\nCPUWeight=idle\n")
            with open(manual_file, "w") as f:
                f.write("")
            with open(intended_file, "w") as f:
                f.write("prop sys machine.slice CPUWeight 10 10\n")
            with open(state_file, "w") as f:
                f.write(
                    f"prop sys machine.slice CPUWeight existed 50\n"
                    f"prop usr user.slice CPUWeight new -\n"
                    f"cgw {cg_file} 8-23\n"
                )

            script = f"""
set -Euo pipefail
RUN={run_dir!r}
STATE={state_file!r}
MANUAL={manual_file!r}
INTENDED={intended_file!r}
UNIT_RUNTIME={tmp!r}/unit-runtime
GAME_DROPIN=zz-cst-game.conf
log() {{ :; }}
ctl_file() {{ echo {new_dropin!r}; }}
ctl() {{ echo "ctl $*" >> {log_file!r}; }}
systemctl() {{ echo "systemctl $*" >> {log_file!r}; }}
eval "$(sed -n '/^off()/,/^}}/p' {GAME_PROFILE!r})"
eval "$(sed -n '/^remove_game_dropins()/,/^}}/p' {GAME_PROFILE!r})"
off
"""
            r = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL
            )
            self.assertEqual(r.stderr, "")
            self.assertFalse(os.path.exists(state_file))
            self.assertFalse(os.path.exists(manual_file))
            self.assertFalse(os.path.exists(intended_file))
            self.assertFalse(os.path.exists(new_dropin))
            with open(cg_file) as f:
                self.assertEqual(f.read().strip(), "8-23")
            with open(log_file) as f:
                actions = f.read().splitlines()
            self.assertEqual(
                actions,
                [
                    "ctl sys set-property --runtime machine.slice CPUWeight=50",
                    "ctl usr daemon-reload",
                ],
            )

    def test_override_files_of_fixed_and_transient_units(self) -> None:
        # systemd writes `set-property --runtime` of a transient unit (scopes,
        # systemd-run's run-*.service) into its transient folder: 'off' must
        # delete that file, or the unit keeps the setting until it ends.
        script = f"""
uid() {{ echo 1000; }}
for fn in transient_unit ctl_file; do eval "$(sed -n "/^${{fn}}()/,/^}}/p" {GAME_PROFILE!r})"; done
ctl_file sys machine.slice CPUWeight
ctl_file sys docker-abc.scope AllowedCPUs
ctl_file usr lushtext.slice CPUWeight
ctl_file usr run-u7.service CPUWeight
ctl_file usr app-flatpak-com.valvesoftware.Steam-1.scope MemoryLow
ctl_file usr flatpak-session-helper.service AllowedCPUs
"""
        r = subprocess.run(
            ["bash", "-c", script], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
        self.assertEqual(
            r.stdout.splitlines(),
            [
                "/run/systemd/system.control/machine.slice.d/50-CPUWeight.conf",
                "/run/systemd/transient/docker-abc.scope.d/50-AllowedCPUs.conf",
                "/run/user/1000/systemd/user.control/lushtext.slice.d/50-CPUWeight.conf",
                "/run/user/1000/systemd/transient/run-u7.service.d/50-CPUWeight.conf",
                "/run/user/1000/systemd/transient/app-flatpak-com.valvesoftware.Steam-1.scope.d/50-MemoryLow.conf",
                "/run/user/1000/systemd/user.control/flatpak-session-helper.service.d/50-AllowedCPUs.conf",
            ],
            r.stderr,
        )

    def test_machine_slice_dropin_has_cpu_and_io_weight(self) -> None:
        dropin = os.path.join(CPU, "units", "machine.slice.d-10-cpu-setup.conf")
        with open(dropin) as f:
            lines = [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(lines, ["[Slice]", "CPUWeight=50", "IOWeight=50"])

    def _run_game_profile_on_off(
        self, vm_weight: str, prior_io_weight: str | None = None
    ) -> tuple[str, list[str], bool, str]:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = os.path.join(tmp, "run")
            ctrl_dir = os.path.join(tmp, "ctrl")
            os.makedirs(run_dir)
            os.makedirs(ctrl_dir)
            state_file = os.path.join(run_dir, "game.state")
            manual_file = os.path.join(run_dir, "manual")
            intended_file = os.path.join(run_dir, "game.intended")
            log_file = os.path.join(tmp, "actions.log")

            if prior_io_weight is not None:
                io_dropin = os.path.join(ctrl_dir, "sys", "machine.slice.d", "50-IOWeight.conf")
                os.makedirs(os.path.dirname(io_dropin), exist_ok=True)
                with open(io_dropin, "w") as f:
                    f.write(f"[Slice]\nIOWeight={prior_io_weight}\n")

            script = f"""
set -Euo pipefail
RUN={run_dir!r}
STATE={state_file!r}
MANUAL={manual_file!r}
INTENDED={intended_file!r}
CONF=/dev/null
TARGET_USER=testuser
BUILD_CPUS=12-23
P_CORES=0-7
VM_WEIGHT={vm_weight!r}
GAME_MEMORY_LOW=16G
GAME_ZSWAP_WRITEBACK=yes
CGROOT={tmp!r}/cgroup
UNIT_RUNTIME={tmp!r}/unit-runtime
GAME_DROPIN=zz-cst-game.conf
log() {{ :; }}
uid() {{ echo 999999; }}
expand_cpus() {{ echo ""; }}
uses_builds_slice() {{ return 1; }}
pin_docker() {{ :; }}
protect_steam() {{ :; }}
cpus_fallback() {{ :; }}
prop() {{
    local f
    f="$(ctl_file "$1" "$2" "$3")"
    if [[ -e "${{f}}" ]]; then
        sed -n "s/^$3=//p" "${{f}}"
    fi
}}
ctl_file() {{
    echo {ctrl_dir!r}"/$1/$2.d/50-$3.conf"
}}
ctl() {{
    echo "ctl $*" >> {log_file!r}
    local mgr="$1" sub="$2"
    shift 2
    if [[ "${{sub}}" == set-property ]]; then
        shift   # --runtime
        local unit="$1" kv p v f
        shift
        for kv in "$@"; do
            p="${{kv%%=*}}"
            v="${{kv#*=}}"
            f="$(ctl_file "${{mgr}}" "${{unit}}" "${{p}}")"
            mkdir -p "$(dirname "${{f}}")"
            printf '[Slice]\\n%s=%s\\n' "${{p}}" "${{v}}" > "${{f}}"
        done
    fi
}}
systemctl() {{ echo "systemctl $*" >> {log_file!r}; }}
for fn in setp on off {PROFILE_HELPERS}; do
    eval "$(sed -n "/^${{fn}}()/,/^}}/p" {GAME_PROFILE!r})"
done
on
cat {state_file!r} > {tmp!r}/state.after_on
cat {intended_file!r} > {tmp!r}/intended.after_on
off
test ! -e {intended_file!r}
"""
            r = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL
            )
            self.assertEqual(r.stderr, "")
            with open(os.path.join(tmp, "state.after_on")) as f:
                state_after_on = f.read()
            with open(os.path.join(tmp, "intended.after_on")) as f:
                intended_after_on = f.read()
            with open(log_file) as f:
                actions = f.read().splitlines()
            io_file_exists_after = os.path.exists(os.path.join(ctrl_dir, "sys", "machine.slice.d", "50-IOWeight.conf"))
            return state_after_on, actions, io_file_exists_after, intended_after_on

    def test_on_and_off_machine_slice_io_weight(self) -> None:
        # Numeric VM_WEIGHT reused for IOWeight, no prior runtime override.
        state_on, actions, io_exists, intended = self._run_game_profile_on_off("25", prior_io_weight=None)
        self.assertIn("prop sys machine.slice IOWeight new -", state_on)
        self.assertIn(
            "ctl sys set-property --runtime machine.slice AllowedCPUs=12-23 CPUWeight=25 IOWeight=25",
            actions,
        )
        self.assertIn("systemctl daemon-reload", actions)
        self.assertFalse(io_exists)
        # What 'on' set, as read back right after (here the fake echoes it).
        self.assertIn("prop sys machine.slice CPUWeight 25 25\n", intended)
        self.assertIn("prop sys user-999999.slice MemoryLow 16G 16G\n", intended)
        self.assertIn("prop usr user.slice CPUWeight idle idle\n", intended)

        # VM_WEIGHT=idle falls back to IOWeight=10, and prior runtime override is restored.
        state_idle, actions_idle, io_exists_idle, _ = self._run_game_profile_on_off("idle", prior_io_weight="80")
        self.assertIn("prop sys machine.slice IOWeight existed 80", state_idle)
        self.assertIn(
            "ctl sys set-property --runtime machine.slice AllowedCPUs=12-23 CPUWeight=idle IOWeight=10",
            actions_idle,
        )
        self.assertIn("ctl sys set-property --runtime machine.slice IOWeight=80", actions_idle)
        self.assertTrue(io_exists_idle)

    def test_setup_help_and_unknown_command(self) -> None:
        r = subprocess.run(
            ["bash", SETUP, "help"],
            env={**os.environ, "CST_PROG": "cst cpu"},
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        self.assertIn("Usage: cst cpu <command> [args]", r.stdout)

        bad = subprocess.run(
            ["bash", SETUP, "not-a-command"], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
        self.assertEqual(bad.returncode, 2)

    def test_e_cores_missing_returns_empty_cleanly(self) -> None:
        # On uniform CPUs without /sys/devices/cpu_atom/cpus, e_cores() must exit 0 under set -e.
        with open(SETUP) as f:
            src = f.read()
        line = next(line_text for line_text in src.splitlines() if line_text.startswith("e_cores()"))
        fn = line.replace("/sys/devices/cpu_atom/cpus", "/nonexistent/cpu_atom/cpus")
        r = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", f'{fn}\necores="$(e_cores)"\nprintf \'<%s>\' "$ecores"'],
            capture_output=True,
            text=True,
            check=True,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.stdout, "<>")


# The helpers of game-profile that on/off/tick call, for the harnesses
# below (each one is cut out of the script by name).
PROFILE_HELPERS = (
    "transient_unit user_cg cpuset_delegated escaped_cgroups gamemode_pid gamemode_in demote_escapes "
    "game_dropin_units write_game_dropins remove_game_dropins remove_leftover_dropins escapes_in_state"
)


# A fake systemd for game-profile: STORE/<mgr>/<unit>/<prop> holds what
# `systemctl show` reports (16G reads back in bytes, as in systemd);
# set-property also writes the runtime override file, like systemd does.
FAKE_SYSTEMD = r"""
ctl_file() { echo "${CTRL}/$1/$2.d/50-$3.conf"; }
ctl() {
    echo "ctl $*" >>"${ACTIONS}"
    local mgr="$1" sub="$2" unit kv p v f
    shift 2
    case "${sub}" in
        set-property)
            shift; unit="$1"; shift
            for kv in "$@"; do
                p="${kv%%=*}"; v="${kv#*=}"
                [[ "${v}" == 16G ]] && v=17179869184
                mkdir -p "${STORE}/${mgr}/${unit}"
                echo "${v}" >"${STORE}/${mgr}/${unit}/${p}"
                f="$(ctl_file "${mgr}" "${unit}" "${p}")"
                mkdir -p "$(dirname "${f}")"
                printf '[Slice]\n%s\n' "${kv}" >"${f}"
            done ;;
        show)
            if [[ "$1" == -P ]]; then
                cat "${STORE}/${mgr}/$3/$2" 2>/dev/null || echo '[not set]'
                return 0
            fi
            local -a ps=()
            while [[ "$1" == -p ]]; do ps+=("$2"); shift 2; done
            for p in "${ps[@]}"; do
                if [[ "${p}" == LoadState ]]; then
                    if [[ -e "${STORE}/${mgr}/$1.notloaded" ]]; then echo LoadState=not-found; else echo LoadState=loaded; fi
                else
                    echo "${p}=$(cat "${STORE}/${mgr}/$1/${p}" 2>/dev/null || echo '[not set]')"
                fi
            done ;;
    esac
}
systemctl() { echo "systemctl $*" >>"${ACTIONS}"; }
"""


class GameProfileFake(unittest.TestCase):
    """game-profile's functions with a fake systemd and a fake cgroup tree."""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cst-cpu-test-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.run_dir = os.path.join(self.tmp, "run")
        os.makedirs(self.run_dir)
        self.state = os.path.join(self.run_dir, "game.state")
        self.intended = os.path.join(self.run_dir, "game.intended")
        self.manual = os.path.join(self.run_dir, "manual")
        self.store = os.path.join(self.tmp, "store")
        self.ctrl = os.path.join(self.tmp, "ctrl")
        self.actions = os.path.join(self.tmp, "actions.log")
        self.cgroot = os.path.join(self.tmp, "cgroup")
        self.unit_runtime = os.path.join(self.tmp, "unit-runtime")
        # The user manager's cgroup folder (uid 999999), as cgroupfs shows it.
        self.ucg = os.path.join(self.cgroot, "user.slice", "user-999999.slice", "user@999999.service")
        os.makedirs(self.ucg)
        # The baseline as systemd shows it before the profile.
        self.set_now("sys", "machine.slice", CPUWeight="50", IOWeight="50")
        for u in ("user.slice", "user-999999.slice", "user@999999.service"):
            self.set_now("sys", u, MemoryLow="0")
        self.set_now("usr", "user.slice", CPUWeight="30")

    def set_now(self, mgr: str, unit: str, **props: str) -> None:
        d = os.path.join(self.store, mgr, unit)
        os.makedirs(d, exist_ok=True)
        for k, v in props.items():
            with open(os.path.join(d, k), "w") as f:
                f.write(v + "\n")

    def now(self, mgr: str, unit: str, prop: str) -> str:
        with open(os.path.join(self.store, mgr, unit, prop)) as f:
            return f.read().strip()

    def run_profile(self, body: str) -> tuple[str, list[str]]:
        """game-profile's functions with a fake systemd, then BODY; returns
        (stdout, actions)."""
        open(self.actions, "w").close()
        script = f"""
set -Euo pipefail
RUN={self.run_dir!r}
STATE={self.state!r}
MANUAL={self.manual!r}
SUPPRESS={self.run_dir!r}/suppress
INTENDED={self.intended!r}
STORE={self.store!r}
CTRL={self.ctrl!r}
ACTIONS={self.actions!r}
TARGET_USER=testuser
BUILD_CPUS=12-23
P_CORES=0-7
VM_WEIGHT=10
GAME_MEMORY_LOW=16G
GAME_ZSWAP_WRITEBACK=yes
CGROOT={self.cgroot!r}
UNIT_RUNTIME={self.unit_runtime!r}
GAME_DROPIN=zz-cst-game.conf
for fn in log prop setp on off drift repair_drift tick again_cmd state_drift {PROFILE_HELPERS}; do
    eval "$(sed -n "/^${{fn}}()/,/^}}/p" {GAME_PROFILE!r})"
done
{FAKE_SYSTEMD}
uid() {{ echo 999999; }}
expand_cpus() {{ echo ""; }}
uses_builds_slice() {{ return 1; }}
pin_docker() {{ :; }}
protect_steam() {{ :; }}
cpus_fallback() {{ :; }}
gamemode_client() {{ return 0; }}
gamemode_pid() {{ [[ -e {self.tmp!r}/gamemode-pids/"$1" ]]; }}
{body}
"""
        r = subprocess.run(
            ["bash", "-c", script], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "")
        with open(self.actions) as f:
            return r.stdout, f.read().splitlines()

    def read(self, path: str) -> str:
        with open(path) as f:
            return f.read()


class GameProfileDriftTest(GameProfileFake):
    """The baseline drop-in name, its migration, and the drift repair of the
    game profile (a daemon-reload or uresourced undoing a setting while the
    profile is on)."""

    def test_baseline_dropins_sort_before_the_runtime_overrides(self) -> None:
        # systemd reads drop-ins in file-name order (byte order) and the last
        # one read wins: the baseline must come first, so that the 50-<Prop>
        # files of `systemctl set-property --runtime` win after a reload.
        with open(SETUP) as f:
            src = f.read()
        name = next(line.split("=", 1)[1] for line in src.splitlines() if line.startswith("DROPIN_NAME="))
        old = next(line.split("=", 1)[1].split()[0] for line in src.splitlines() if line.startswith("OLD_DROPIN_NAME="))
        self.assertEqual(name, "10-cpu-setup.conf")
        self.assertTrue(os.path.exists(os.path.join(CPU, "units", f"machine.slice.d-{name}")))
        overrides = [
            "50-CPUWeight.conf",
            "50-IOWeight.conf",
            "50-AllowedCPUs.conf",
            "50-MemoryLow.conf",
            "50-MemoryZSwapWriteback.conf",
        ]
        self.assertEqual(sorted(overrides + [name], key=os.fsencode)[0], name)
        # The old name sorted after them (upper case before lower case): the bug.
        self.assertEqual(sorted(overrides + [old], key=os.fsencode)[-1], old)
        # The user drop-ins get the same name.
        script = f"""
eval "$(grep -E '^DROPIN_NAME=' {SETUP!r})"
for fn in toolbox_dropin helper_dropin; do eval "$(grep "^${{fn}}()" {SETUP!r})"; done
user_home() {{ echo /home/u; }}
toolbox_dropin; helper_dropin; toolbox_dropin 50-cpu-setup.conf
"""
        r = subprocess.run(
            ["bash", "-c", script], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
        self.assertEqual(
            r.stdout.splitlines(),
            [
                "/home/u/.config/systemd/user/user.slice.d/10-cpu-setup.conf",
                "/home/u/.config/systemd/user/flatpak-session-helper.service.d/10-cpu-setup.conf",
                "/home/u/.config/systemd/user/user.slice.d/50-cpu-setup.conf",
            ],
            r.stderr,
        )

    def test_install_and_uninstall_remove_the_old_name(self) -> None:
        home = os.path.join(self.tmp, "home")
        unit_dir = os.path.join(self.tmp, "etc-systemd")
        old = [
            os.path.join(unit_dir, "machine.slice.d", "50-cpu-setup.conf"),
            os.path.join(home, ".config/systemd/user/user.slice.d/50-cpu-setup.conf"),
            os.path.join(home, ".config/systemd/user/flatpak-session-helper.service.d/50-cpu-setup.conf"),
        ]
        keep = [
            os.path.join(unit_dir, "machine.slice.d", "10-cpu-setup.conf"),
            os.path.join(unit_dir, "machine.slice.d", "50-memory-low.conf"),  # the swap module's
            os.path.join(home, ".config/systemd/user/user.slice.d/10-cpu-setup.conf"),
        ]
        for p in old + keep:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        script = f"""
set -Eeuo pipefail
UNIT_DIR={unit_dir!r}
eval "$(grep -E '^(DROPIN_NAME|OLD_DROPIN_NAME|MACHINE_DROPIN)=' {SETUP!r})"
for fn in toolbox_dropin helper_dropin; do eval "$(grep "^${{fn}}()" {SETUP!r})"; done
eval "$(sed -n '/^remove_old_dropins()/,/^}}/p' {SETUP!r})"
user_home() {{ echo {home!r}; }}
target_user=tester
remove_old_dropins
remove_old_dropins   # again: nothing left, no error under set -e
target_user=''
remove_old_dropins
"""
        r = subprocess.run(
            ["bash", "-c", script], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for p in old:
            self.assertFalse(os.path.exists(p), p)
        for p in keep:
            self.assertTrue(os.path.exists(p), p)
        self.assertEqual(r.stdout.count("removed"), 3, r.stdout)
        # install and uninstall both call it, and install reloads both managers after.
        with open(SETUP) as f:
            src = f.read()
        install = src[src.index("install_all() {") : src.index("\nuninstall() {")]
        uninstall = src[src.index("\nuninstall() {") : src.index("# ---- docker builds.slice")]
        self.assertLess(install.index("remove_old_dropins"), install.index("systemctl daemon-reload"))
        self.assertIn("usys daemon-reload", install)
        self.assertIn("remove_old_dropins", uninstall)

    def test_uninstall_removes_leftover_memorylow_dropins(self) -> None:
        rt = os.path.join(self.tmp, "run-systemd-system")
        left = [
            os.path.join(rt, "user.slice.d", "zz-cst-game.conf"),
            os.path.join(rt, "user@1000.service.d", "zz-cst-game.conf"),
        ]
        keep = os.path.join(rt, "user-1000.slice.d", "10-other.conf")
        for p in left + [keep]:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").close()
        script = f"""
set -Eeuo pipefail
eval "$(grep -E '^GAME_DROPINS_GLOB=' {SETUP!r})"
GAME_DROPINS_GLOB="{rt}/${{GAME_DROPINS_GLOB#/run/systemd/system/}}"
eval "$(sed -n '/^remove_game_dropins()/,/^}}/p' {SETUP!r})"
remove_game_dropins
remove_game_dropins   # again: nothing left, no error under set -e
"""
        r = subprocess.run(
            ["bash", "-c", script], check=False, capture_output=True, text=True, stdin=subprocess.DEVNULL
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for p in left:
            self.assertFalse(os.path.exists(os.path.dirname(p)), p)
        self.assertTrue(os.path.exists(keep))
        self.assertEqual(r.stdout.count("removed"), 2, r.stdout)

    def test_on_records_what_systemd_reads_back_and_not_for_scopes(self) -> None:
        out, actions = self.run_profile("on\nsetp usr app-flatpak-com.valvesoftware.Steam-1.scope MemoryLow=16G")
        intended = self.read(self.intended)
        self.assertIn("prop sys machine.slice CPUWeight 10 10\n", intended)
        self.assertIn("prop sys machine.slice IOWeight 10 10\n", intended)
        self.assertIn("prop sys user.slice MemoryLow 16G 17179869184\n", intended)
        self.assertIn("prop sys user@999999.service MemoryLow 16G 17179869184\n", intended)
        self.assertIn("prop usr user.slice CPUWeight idle idle\n", intended)
        self.assertNotIn("Steam", intended)
        self.assertIn("Steam-1.scope MemoryLow new -", self.read(self.state))  # off() still undoes it
        # game.state keeps the format off() reads: six fields at most.
        for line in self.read(self.state).splitlines():
            self.assertLessEqual(len(line.split()), 6, line)

    def test_failed_set_property_records_no_intended_value(self) -> None:
        self.run_profile(
            'ctl() { echo "ctl $*" >>"${ACTIONS}"; [[ "$2" != set-property ]]; }\n'
            ': >"${STATE}"; : >"${INTENDED}"\nsetp sys machine.slice CPUWeight=10'
        )
        self.assertEqual(self.read(self.intended), "")
        self.assertIn("prop sys machine.slice CPUWeight new -", self.read(self.state))

    def test_tick_repairs_drift_without_touching_the_state_and_off_restores(self) -> None:
        # IOWeight had a runtime override of its own before the profile.
        self.set_now("sys", "machine.slice", IOWeight="80")
        os.makedirs(os.path.join(self.ctrl, "sys", "machine.slice.d"))
        with open(os.path.join(self.ctrl, "sys", "machine.slice.d", "50-IOWeight.conf"), "w") as f:
            f.write("[Slice]\nIOWeight=80\n")
        self.run_profile("on")
        state_after_on = self.read(self.state)
        open(self.manual, "w").close()  # on by hand: tick does not switch it off
        # A daemon-reload with the old baseline name, and uresourced:
        self.set_now("sys", "machine.slice", CPUWeight="50", IOWeight="50")
        self.set_now("sys", "user.slice", MemoryLow="0")
        self.set_now("usr", "user.slice", CPUWeight="30")
        out, actions = self.run_profile("tick")
        sets = [a for a in actions if "set-property" in a]
        self.assertEqual(
            sorted(sets),
            sorted(
                [
                    "ctl sys set-property --runtime machine.slice CPUWeight=10",
                    "ctl sys set-property --runtime machine.slice IOWeight=10",
                    "ctl sys set-property --runtime user.slice MemoryLow=16G",
                    "ctl usr set-property --runtime user.slice CPUWeight=idle",
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
        self.assertEqual([a for a in actions if "set-property" in a], [])
        self.assertEqual(out, "")
        # off: the value from before 'on' comes back, the profile's own files go.
        out, actions = self.run_profile("off")
        self.assertIn("ctl sys set-property --runtime machine.slice IOWeight=80", actions)
        self.assertNotIn("ctl sys set-property --runtime machine.slice IOWeight=10", actions)
        self.assertFalse(os.path.exists(os.path.join(self.ctrl, "sys", "machine.slice.d", "50-CPUWeight.conf")))
        self.assertFalse(os.path.exists(os.path.join(self.ctrl, "sys", "user.slice.d", "50-MemoryLow.conf")))
        self.assertFalse(os.path.exists(os.path.join(self.ctrl, "usr", "user.slice.d", "50-CPUWeight.conf")))
        self.assertTrue(os.path.exists(os.path.join(self.ctrl, "sys", "machine.slice.d", "50-IOWeight.conf")))
        self.assertIn("systemctl daemon-reload", actions)
        self.assertIn("ctl usr daemon-reload", actions)
        for p in (self.state, self.intended, self.manual):
            self.assertFalse(os.path.exists(p), p)

    def test_cgroup_file_drift_is_rewritten(self) -> None:
        cg = os.path.join(self.tmp, "cpuset.cpus")
        with open(cg, "w") as f:
            f.write("8-23\n")
        with open(self.state, "w") as f:
            f.write(f"cgw {cg} ''\n")
        with open(self.intended, "w") as f:
            f.write(f"cgw {cg} 12-23 12-23\n")
        open(self.manual, "w").close()
        out, _ = self.run_profile("tick")
        self.assertEqual(self.read(cg).strip(), "12-23")
        self.assertIn(f"drift: {cg} was '8-23', wrote 12-23 again", out)
        # The cgroup went away (its unit stopped): nothing to compare, nothing logged.
        os.remove(cg)
        out, _ = self.run_profile("tick")
        self.assertEqual(out, "")
        self.assertFalse(os.path.exists(cg))

    def test_unloaded_unit_and_empty_readback_are_not_repaired(self) -> None:
        with open(self.state, "w") as f:
            f.write("prop sys builds.slice CPUWeight new -\nprop usr app.slice MemoryZSwapWriteback new -\n")
        with open(self.intended, "w") as f:
            f.write("prop sys builds.slice CPUWeight idle idle\nprop usr app.slice MemoryZSwapWriteback no ''\n")
        open(os.path.join(self.store, "sys", "builds.slice.notloaded"), "w").close()
        open(self.manual, "w").close()
        out, actions = self.run_profile("tick")
        self.assertEqual([a for a in actions if "set-property" in a], [])
        self.assertEqual(out, "")

    def test_old_format_state_is_tolerated(self) -> None:
        # Switched on by a version without game.intended: no comparison at all.
        with open(self.state, "w") as f:
            f.write("prop sys machine.slice CPUWeight new -\nprop sys user.slice MemoryLow new -\n")
        open(self.manual, "w").close()
        self.set_now("sys", "machine.slice", CPUWeight="50")
        out, actions = self.run_profile("tick")
        self.assertEqual(out, "")
        self.assertEqual([a for a in actions if " show " in a or "set-property" in a], [])
        out, _ = self.run_profile("state_drift")
        self.assertIn("drift: not checked (switched on by an older version", out)
        # On by hand (the manual mark): a restart would drop the mark, so the
        # command keeps the profile on by hand.
        self.assertIn("cst cpu game off && cst cpu game on switches it off and on again", out)
        os.remove(self.manual)
        out, _ = self.run_profile("state_drift")
        self.assertIn("sudo systemctl restart cpu-game.service switches it off and on again", out)
        open(self.manual, "w").close()
        _, actions = self.run_profile("off")
        self.assertIn("systemctl daemon-reload", actions)
        self.assertFalse(os.path.exists(self.state))

    def test_state_shows_drift_per_property(self) -> None:
        self.run_profile("on")
        out, _ = self.run_profile("state_drift")
        self.assertEqual(out, "  drift: none (every setting the profile made still holds)\n")
        self.set_now("sys", "machine.slice", CPUWeight="50")
        self.set_now("sys", "user-999999.slice", MemoryLow="0")
        out, actions = self.run_profile("state_drift")
        self.assertIn("  DRIFT: machine.slice CPUWeight is '50', the profile set '10'\n", out)
        self.assertIn("  DRIFT: user-999999.slice MemoryLow is '0', the profile set '17179869184'\n", out)
        self.assertIn("the watchdog sets them again within 30 s", out)
        self.assertIn("if they stay: sudo systemctl restart cpu-game.service", out)
        self.assertEqual([a for a in actions if "set-property" in a], [])  # state only reads


class GameProfileEscapesTest(GameProfileFake):
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

    def sets(self, actions: Sequence[str]) -> list[str]:
        return [a for a in actions if "set-property" in a]

    def test_on_makes_the_other_boxes_idle_and_records_only_slices(self) -> None:
        self.tree()
        out, actions = self.run_profile("on")
        sets = self.sets(actions)
        for u in ("background.slice", "lushtext.slice", "cpu.slice", "run-u7.service", "run-r1a2.scope"):
            self.assertIn(f"ctl usr set-property --runtime {u} CPUWeight=idle", sets)
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
        self.assertFalse([a for a in sets if "ctl usr" in a and "AllowedCPUs" in a], sets)  # not delegated
        self.assertIn("lushtext.slice: idle while the game runs", out)
        state, intended = self.read(self.state), self.read(self.intended)
        for u in ("background.slice", "lushtext.slice", "cpu.slice", "run-u7.service", "run-r1a2.scope"):
            self.assertIn(f"prop usr {u} CPUWeight new -\n", state)
        # Drift repair covers the slices; transient units have no drop-in that could win.
        self.assertIn("prop usr lushtext.slice CPUWeight idle idle\n", intended)
        self.assertIn("prop usr background.slice CPUWeight idle idle\n", intended)
        self.assertNotIn("run-", intended)
        out, _ = self.run_profile("escapes_in_state")
        # Sorted: the glob order of _cpu.slice depends on the locale.
        self.assertEqual(
            sorted(out.split()), ["background.slice", "cpu.slice", "lushtext.slice", "run-r1a2.scope", "run-u7.service"]
        )

    def test_with_cpuset_delegated_they_also_leave_the_game_cpus(self) -> None:
        self.tree(delegated=True)
        _, actions = self.run_profile("on")
        sets = self.sets(actions)
        self.assertIn("ctl usr set-property --runtime lushtext.slice CPUWeight=idle AllowedCPUs=12-23", sets)
        self.assertIn("ctl usr set-property --runtime run-u7.service CPUWeight=idle AllowedCPUs=12-23", sets)
        self.assertIn("ctl usr set-property --runtime user.slice AllowedCPUs=12-23", sets)

    def test_a_box_with_the_game_in_it_is_left_alone(self) -> None:
        self.tree()
        self.cg("app.slice", "run-r9.scope", procs=(4242,))
        self.cg("games.slice", "run-u3.service", procs=(77, 4243))
        os.makedirs(os.path.join(self.tmp, "gamemode-pids"))
        for pid in (4242, 4243):
            open(os.path.join(self.tmp, "gamemode-pids", str(pid)), "w").close()
        _, actions = self.run_profile("on")
        sets = self.sets(actions)
        self.assertFalse([a for a in sets if "run-r9.scope" in a or "games.slice" in a], sets)
        self.assertIn("ctl usr set-property --runtime lushtext.slice CPUWeight=idle", sets)

    def test_tick_picks_up_new_boxes_once(self) -> None:
        self.tree()
        self.run_profile("on")
        open(self.manual, "w").close()
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
                    "ctl usr set-property --runtime lush\\x2dbatch.slice CPUWeight=idle",
                    "ctl usr set-property --runtime run-u8.service CPUWeight=idle",
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
        ctrl = os.path.join(self.ctrl, "usr")
        self.assertTrue(os.path.exists(os.path.join(ctrl, "lushtext.slice.d", "50-CPUWeight.conf")))
        # The job ended: its unit and cgroup are gone (systemd may already
        # have removed the transient unit's control file too).
        shutil.rmtree(os.path.join(self.ucg, "app.slice", "run-u7.service"))
        shutil.rmtree(os.path.join(ctrl, "run-u7.service.d"))
        out, actions = self.run_profile("off")
        for u in ("background.slice", "lushtext.slice", "cpu.slice", "run-u7.service", "run-r1a2.scope"):
            self.assertFalse(os.path.exists(os.path.join(ctrl, f"{u}.d")), u)
        self.assertIn("ctl usr daemon-reload", actions)
        self.assertFalse(os.path.exists(self.state))

    def test_upgrade_while_on_from_the_previous_version(self) -> None:
        # Switched on by the previous version: game.state and game.intended
        # in the same format, but no zz- drop-ins and no other boxes handled.
        self.run_profile("on")
        shutil.rmtree(self.unit_runtime)
        for f in (self.state, self.intended):
            self.assertNotIn("lushtext", self.read(f))
        open(self.manual, "w").close()
        # Installed while the game runs: the next tick picks up the boxes and
        # leaves the profile on (no drop-ins to remove, no reload).
        self.tree()
        _, actions = self.run_profile("tick")
        self.assertIn("ctl usr set-property --runtime lushtext.slice CPUWeight=idle", self.sets(actions))
        self.assertFalse([a for a in actions if "daemon-reload" in a], actions)
        self.assertIn("prop usr lushtext.slice CPUWeight new -\n", self.read(self.state))
        self.assertTrue(os.path.exists(self.state))
        # off undoes both what the old version set and what the new tick added.
        old_set = os.path.join(self.ctrl, "sys", "machine.slice.d")
        self.assertTrue(os.path.exists(old_set))
        _, actions = self.run_profile("off")
        self.assertIn("systemctl daemon-reload", actions)
        self.assertIn("ctl usr daemon-reload", actions)
        self.assertFalse(os.path.exists(old_set))
        self.assertFalse(os.path.exists(os.path.join(self.ctrl, "usr", "lushtext.slice.d")))
        self.assertFalse(os.path.exists(self.state))
        self.assertFalse(os.path.exists(self.unit_runtime))

    def test_a_slice_with_its_own_override_gets_it_back_and_drift_is_repaired(self) -> None:
        self.tree()
        # Someone had set lushtext.slice to weight 50 at run time before the game.
        own = os.path.join(self.ctrl, "usr", "lushtext.slice.d", "50-CPUWeight.conf")
        os.makedirs(os.path.dirname(own))
        with open(own, "w") as f:
            f.write("[Slice]\nCPUWeight=50\n")
        self.set_now("usr", "lushtext.slice", CPUWeight="50")
        self.run_profile("on")
        open(self.manual, "w").close()
        self.assertIn("prop usr lushtext.slice CPUWeight existed 50\n", self.read(self.state))
        # A daemon-reload put 50 back meanwhile: the tick sets idle again,
        # without a second record.
        self.set_now("usr", "lushtext.slice", CPUWeight="50")
        state = self.read(self.state)
        out, actions = self.run_profile("tick")
        self.assertEqual(self.sets(actions), ["ctl usr set-property --runtime lushtext.slice CPUWeight=idle"])
        self.assertIn("drift: lushtext.slice CPUWeight was '50'", out)
        self.assertEqual(self.read(self.state), state)
        _, actions = self.run_profile("off")
        self.assertIn("ctl usr set-property --runtime lushtext.slice CPUWeight=50", actions)
        self.assertEqual(self.now("usr", "lushtext.slice", "CPUWeight"), "50")
        self.assertTrue(os.path.exists(own))

    def test_on_writes_the_memorylow_dropins_and_off_removes_them(self) -> None:
        self.run_profile("on")
        files = {
            u: os.path.join(self.unit_runtime, f"{u}.d", "zz-cst-game.conf")
            for u in ("user.slice", "user-999999.slice", "user@999999.service")
        }
        for u, f in files.items():
            lines = [line for line in self.read(f).splitlines() if not line.startswith("#")]
            self.assertEqual(lines, ["[Service]" if u.endswith(".service") else "[Slice]", "MemoryLow=16G"], u)
        _, actions = self.run_profile("off")
        self.assertIn("systemctl daemon-reload", actions)
        for f in files.values():
            self.assertFalse(os.path.exists(os.path.dirname(f)), f)

    def test_on_sets_memorylow_live_without_a_reload(self) -> None:
        _, actions = self.run_profile("on")
        self.assertIn("ctl sys set-property --runtime user-999999.slice MemoryLow=16G", actions)
        self.assertFalse([a for a in actions if "daemon-reload" in a], actions)

    def test_off_without_state_still_removes_leftover_dropins(self) -> None:
        d = os.path.join(self.unit_runtime, "user-999999.slice.d")
        os.makedirs(d)
        with open(os.path.join(d, "zz-cst-game.conf"), "w") as f:
            f.write("[Slice]\nMemoryLow=16G\n")
        other = os.path.join(self.unit_runtime, "user.slice.d", "10-other.conf")
        os.makedirs(os.path.dirname(other))
        open(other, "w").close()
        out, actions = self.run_profile("off")
        self.assertFalse(os.path.exists(d))
        self.assertTrue(os.path.exists(other))  # only the profile's own file goes
        self.assertEqual(actions, ["systemctl daemon-reload"])
        self.assertIn("were still there: removed them", out)
        # Nothing left: no reload at all.
        _, actions = self.run_profile("off")
        self.assertEqual(actions, [])

    def test_tick_with_the_profile_off_removes_leftover_dropins(self) -> None:
        d = os.path.join(self.unit_runtime, "user@999999.service.d")
        os.makedirs(d)
        open(os.path.join(d, "zz-cst-game.conf"), "w").close()
        out, actions = self.run_profile("gamemode_client() { return 1; }\ntick")
        self.assertFalse(os.path.exists(d))
        self.assertEqual(actions, ["systemctl daemon-reload"])
        self.assertIn("were still there: removed them", out)
        out, actions = self.run_profile("gamemode_client() { return 1; }\ntick")
        self.assertEqual((out, actions), ("", []))


class VmPinCliTest(unittest.TestCase):
    """cpu-vm-pin's command line (cpu.cli.vm_pin) with a fake virsh and settings."""

    CONF = {"VM_NAME": "win11-devbox", "VM_VCPUS": "12", "BUILD_CPUS": "12-23", "VM_CPU_DISABLE": "cet-ss cet-ibt"}

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
            unittest.mock.patch.object(cpu_cli, "vm_pin_settings", lambda action: dict(self.CONF)),
            contextlib.redirect_stdout(out),
        ):
            rc = cpu_cli.vm_pin(argv, doc="DOC")
        return rc, out.getvalue(), calls

    def test_conf_reads_the_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cpu-setup.conf")
            with open(p, "w") as f:
                f.write("VM_NAME='box'\nVM_VCPUS=8\nBUILD_CPUS=4-7,9\nVM_CPU_DISABLE=\"cet-ss\"\nTARGET_USER=alice\n")
            with unittest.mock.patch.object(cpu_adapters, "CONF", p):
                c = cpu_adapters.conf()
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
            with unittest.mock.patch.object(cpu_adapters, "CONF", p):
                c = cpu_adapters.conf()
            self.assertEqual((c["BUILD_CPUS"], c["VM_VCPUS"]), ("12-23", "8"))
            with open(p, "w") as f:
                f.write("VM_NAME='box'\nBUILD_CPUS = 8-11\n")
            err = io.StringIO()
            calls: list[tuple[str, ...]] = []

            def virsh(*args: str, check: bool = True) -> str:
                calls.append(args)
                return SAMPLE_DOMAIN_XML

            with (
                unittest.mock.patch.object(cpu_adapters, "CONF", p),
                unittest.mock.patch.object(cpu_cli, "virsh", virsh),
                contextlib.redirect_stderr(err),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                c = cpu_adapters.conf()  # plan and status: printed, the key keeps vm-pin's default
                self.assertEqual(c["BUILD_CPUS"], "12-23")
                self.assertIn(f"{p} line 2: BUILD_CPUS = 8-11: blanks around =", err.getvalue())
                with self.assertRaises(SystemExit) as cm:
                    cpu_cli.vm_pin(["apply"], doc="DOC")
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

    def test_virsh_adapter(self) -> None:
        ok = subprocess.CompletedProcess(["virsh"], 0, "<domain/>", "")
        with unittest.mock.patch.object(cpu_adapters.subprocess, "run", return_value=ok) as run:
            self.assertEqual(cpu_adapters.virsh("dumpxml", "win11-devbox"), "<domain/>")
        self.assertEqual(run.call_args.args[0], ["virsh", "-c", "qemu:///system", "dumpxml", "win11-devbox"])
        fail = subprocess.CompletedProcess(["virsh"], 1, "partial", "boom")
        with unittest.mock.patch.object(cpu_adapters.subprocess, "run", return_value=fail):
            with self.assertRaisesRegex(SystemExit, "virsh dumpxml win11-devbox: boom"):
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
        # check: the run stops with one line (no traceback); without check it goes on, as after a failure.
        hang = subprocess.TimeoutExpired(["virsh"], cpu_adapters.VIRSH_TIMEOUT)
        with unittest.mock.patch.object(cpu_adapters.subprocess, "run", side_effect=hang):
            with self.assertRaises(SystemExit) as stop:
                cpu_adapters.virsh("domstate", "win11-devbox")
            self.assertEqual(str(stop.exception.code), "virsh domstate win11-devbox: no answer within 60 s")
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
        restored: list[str] = []
        # In-process: SIGPIPE at its default would stay so for the rest of the test run.
        with unittest.mock.patch.object(cpu_cli, "restore_default_signals", lambda: restored.append("x")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cpu_cli.cpu_watch(["quiet", "--for", "0"], doc="DOC"), 2)
                self.assertEqual(cpu_cli.cpu_watch(["watch", "--bogus"], doc="DOC"), 2)
            self.assertIn("quiet: --for: a whole number of seconds", err.getvalue())
            self.assertIn("unknown argument --bogus", err.getvalue())
            self.assertEqual(restored, [], "watch and quiet keep Python's SIGPIPE handling")
            for argv in ([], ["bogus"], ["--help"]):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(cpu_cli.cpu_watch(argv, doc="DOC"), 2)
                self.assertEqual(out.getvalue(), "DOC\n")
            self.assertEqual(restored, ["x", "x", "x"], "the help only reads: SIGPIPE back at its default")
        with tempfile.TemporaryDirectory() as tmp:
            out = io.StringIO()
            restored = []
            with (
                unittest.mock.patch.object(cpu_cli, "RUNS", tmp),
                unittest.mock.patch.object(cpu_adapters, "RUNS", tmp),
                # In-process: SIGPIPE at its default would stay so for the rest of the test run.
                unittest.mock.patch.object(cpu_cli, "restore_default_signals", lambda: restored.append("x")),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(cpu_cli.cpu_watch(["report"], doc="DOC"), 0)
            self.assertEqual(restored, ["x"])
        self.assertEqual(out.getvalue(), f"no runs in {tmp}; record one with: cst cpu watch -l <label> [-- <cmd>]\n")


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
        restored: list[str] = []
        with (
            unittest.mock.patch.object(cpu_adapters, "conf", lambda refuse=False: {"refuse": str(refuse)}),
            unittest.mock.patch.object(cpu_adapters, "restore_default_signals", lambda: restored.append("x")),
        ):
            for action in ("status", "plan", "apply"):
                self.assertEqual(cpu_adapters.vm_pin_settings(action), {"refuse": str(action == "apply")})
        self.assertEqual(restored, ["x", "x"], "apply (a temp file removed in a finally) must keep Python's handling")


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
        window = [{"v": {"cpu": (300, 100)}}, {"v": {"cpu": (1200, 250)}}]
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


class DockerSliceInstallTest(unittest.TestCase):
    """`docker-slice install` writes cpu:docker-slice (builds.slice, a
    template) through the real installer, staged under the test's folder;
    its bytes are what the shell wrote before it was a template: the file,
    plus AllowedCPUs= only when there are E-cores."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-cpu-slice-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)

    def install(self, ecores: str) -> str:
        script = f"""
set -Eeuo pipefail
. {REPO!r}/cst-install.bash
_cst_installer_as() {{ shift; local c="$1"; shift; /usr/bin/python3 -I -B "$CST_INSTALLER" "$c" --root "$T/stage" "$@"; }}
systemctl() {{ echo "systemctl $*"; }}
e_cores() {{ printf '%s' {ecores!r}; }}
UNIT_DIR=/etc/systemd/system; SRC={CPU!r}; DAEMON_JSON="$T/daemon.json"; PROG=cst-cpu-test
eval "$(sed -n '/^docker_slice()/,/^}}/p' {SETUP!r})"
docker_slice install
"""
        r = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "T": self.tmp},
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("written:   " + self.tmp + "/stage/etc/systemd/system/builds.slice", r.stdout)
        with open(os.path.join(self.tmp, "stage/etc/systemd/system/builds.slice")) as f:
            return f.read()

    def test_with_and_without_e_cores(self) -> None:
        with open(os.path.join(CPU, "units/builds.slice.in")) as f:
            plain = f.read().replace("@ALLOWED@\n", "")
        self.assertEqual(self.install("16-23"), plain + "AllowedCPUs=16-23\n")
        shutil.rmtree(os.path.join(self.tmp, "stage"))
        self.assertEqual(self.install(""), plain)
        self.assertNotIn("@", plain.split("[Slice]")[1])


if __name__ == "__main__":
    unittest.main()

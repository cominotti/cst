"""Unit tests for the cpu module: vm-pin XML rewriting, cpu-watch frame-time
and run-summary helpers, game-profile CPU range expansion, and cpu-setup.sh CLI.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s cpu/tests -t . -v
"""

import csv
import difflib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True

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


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class VmPinTest(unittest.TestCase):
    def setUp(self):
        self.m = load_module("vm_pin", VM_PIN)

    def test_pins_vcpus_emulator_and_topology(self):
        out = self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23")
        self.assertIn("xmlns:libosinfo=", out)
        self.assertNotIn("ns0:", out)
        root = ET.fromstring(out)
        vcpu = root.find("vcpu")
        self.assertEqual(vcpu.text, "12")
        self.assertEqual(vcpu.attrib, {"placement": "static"})
        topo = root.find("cpu/topology")
        self.assertEqual(topo.get("cores"), "12")
        self.assertEqual(topo.get("sockets"), "1")
        self.assertEqual(topo.get("threads"), "1")
        tune = root.find("cputune")
        self.assertEqual(tune.find("shares").text, "1024")
        pins = [(el.get("vcpu"), el.get("cpuset")) for el in tune.findall("vcpupin")]
        self.assertEqual(pins, [(str(i), "12-23") for i in range(12)])
        emupin = tune.findall("emulatorpin")
        self.assertEqual(len(emupin), 1)
        self.assertEqual(emupin[0].get("cpuset"), "12-23")

    def test_inserts_cputune_when_absent_and_is_idempotent(self):
        minimal = "<domain><name>vm</name><vcpu>8</vcpu></domain>\n"
        first = self.m.pinned(minimal, 4, "8-11")
        second = self.m.pinned(first, 4, "8-11")
        self.assertEqual(
            list(difflib.unified_diff(first.splitlines(True), second.splitlines(True))),
            [],
        )


class CpuWatchTest(unittest.TestCase):
    def setUp(self):
        self.m = load_module("cpu_watch", CPU_WATCH)

    def test_frametimes_and_lows(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "mangohud.csv")
            with open(p, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["os", "Linux"])
                w.writerow(["fps", "FrameTime", "cpu_load"])
                for ft in [10.0] * 99 + [20.0]:
                    w.writerow(["100", str(ft), "50"])
                w.writerow(["bad", "not-a-number", "0"])
            ft = self.m.frametimes(p)
            self.assertEqual(len(ft), 100)
            avg_fps, low1, low01 = self.m.lows(ft)
            self.assertAlmostEqual(avg_fps, 1000 / 10.1, places=2)
            self.assertAlmostEqual(low1, 50.0, places=2)
            self.assertAlmostEqual(low01, 50.0, places=2)
            self.assertIsNone(self.m.lows([]))

    def test_summarize_with_and_without_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = os.path.join(tmp, "20261111T120000-baseline")
            os.makedirs(run)
            with open(os.path.join(run, "meta.json"), "w") as f:
                json.dump({"label": "baseline"}, f)
            fields = [
                "t", "profile_on", "game_pid", "game_cpu_wait_ms",
                "game_mem_stall_ms", "game_invol_switches", "sys_cpu_some_ms",
                "p_mhz", "e_mhz", "vm_cpus_used", "builds_cpus_used",
            ]
            with open(os.path.join(run, "samples.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(fields)
                w.writerow([1.0, 1, 1234, 25.0, 2.0, 40, 10.0, 5200, 3800, 2.5, 4.0])
                w.writerow([2.0, 1, 1234, 35.0, 3.0, 60, 12.0, 5400, 4000, 3.5, 6.0])
            s = self.m.summarize(run)
            self.assertIn("20261111T120000-baseline", s)
            self.assertIn("game wait   3.0% (max   3.5%)", s)
            self.assertIn("mem stall       5 ms", s)
            self.assertIn("profile on", s)

            # Without a detected game PID
            with open(os.path.join(run, "samples.csv"), "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(fields)
                w.writerow([1.0, 0, "", "", "", "", 5.0, 5000, 3600, 0.0, 0.0])
            s_nogame = self.m.summarize(run)
            self.assertIn("no game found", s_nogame)
            self.assertIn("profile off", s_nogame)


class GameProfileAndCliTest(unittest.TestCase):
    def test_expand_cpus(self):
        cmd = (
            "sed -n '/^expand_cpus()/,/^}/p' \"$1\" > \"$2\" && "
            ". \"$2\" && expand_cpus '0-3,6,12-14'"
        )
        with tempfile.TemporaryDirectory() as tmp:
            fn_file = os.path.join(tmp, "fn.bash")
            r = subprocess.run(
                ["bash", "-c", cmd, "bash", GAME_PROFILE, fn_file],
                capture_output=True, text=True, check=True,
            )
            self.assertEqual(r.stdout.strip(), "0 1 2 3 6 12 13 14")

    def test_off_reverts_in_reverse_order_and_cleans_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = os.path.join(tmp, "run")
            os.makedirs(run_dir)
            state_file = os.path.join(run_dir, "game.state")
            manual_file = os.path.join(run_dir, "manual")
            cg_file = os.path.join(tmp, "cpuset.cpus")
            new_dropin = os.path.join(tmp, "50-CPUWeight.conf")
            log_file = os.path.join(tmp, "actions.log")

            with open(cg_file, "w") as f:
                f.write("12-23\n")
            with open(new_dropin, "w") as f:
                f.write("[Slice]\nCPUWeight=idle\n")
            with open(manual_file, "w") as f:
                f.write("")
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
log() {{ :; }}
ctl_file() {{ echo {new_dropin!r}; }}
ctl() {{ echo "ctl $*" >> {log_file!r}; }}
systemctl() {{ echo "systemctl $*" >> {log_file!r}; }}
eval "$(sed -n '/^off()/,/^}}/p' {GAME_PROFILE!r})"
off
"""
            subprocess.run(["bash", "-c", script], check=True)
            self.assertFalse(os.path.exists(state_file))
            self.assertFalse(os.path.exists(manual_file))
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

    def test_setup_help_and_unknown_command(self):
        r = subprocess.run(
            ["bash", SETUP, "help"],
            env={**os.environ, "CST_PROG": "cst cpu"},
            capture_output=True, text=True, check=True,
        )
        self.assertIn("Usage: cst cpu <command> [args]", r.stdout)

        bad = subprocess.run(
            ["bash", SETUP, "not-a-command"],
            capture_output=True, text=True,
        )
        self.assertEqual(bad.returncode, 2)


if __name__ == "__main__":
    unittest.main()

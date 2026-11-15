"""Unit tests for the cpu module: vm-pin XML rewriting, cpu-watch frame-time
and run-summary helpers, game-profile CPU range expansion, on/off, drift
repair and drift report (with a fake systemd), the baseline drop-in name and
its migration, and cpu-setup.sh CLI.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s cpu/tests -t . -v
"""

import csv
import difflib
import importlib.machinery
import importlib.util
import json
import os
import shutil
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

    def test_a_default_namespace_in_metadata_does_not_move_domain(self):
        xml = SAMPLE_DOMAIN_XML.replace("<metadata>", "<metadata><app xmlns='http://example.com/app/1'/>", 1)
        self.assertIn("example.com/app/1", xml)
        root = ET.fromstring(self.m.pinned(xml, 12, "12-23"))
        self.assertEqual(root.tag, "domain")
        self.assertIsNotNone(root.find("metadata/{http://example.com/app/1}app"))
        again = self.m.pinned(self.m.pinned(xml, 12, "12-23"), 12, "12-23")     # ns0: on the way back
        self.assertEqual(ET.fromstring(again).tag, "domain")

    def test_inserts_cputune_when_absent_and_is_idempotent(self):
        minimal = "<domain><name>vm</name><vcpu>8</vcpu></domain>\n"
        first = self.m.pinned(minimal, 4, "8-11")
        second = self.m.pinned(first, 4, "8-11")
        self.assertEqual(
            list(difflib.unified_diff(first.splitlines(True), second.splitlines(True))),
            [],
        )


class VmPinCpuFeaturesTest(unittest.TestCase):
    """The CET/MBEC features vm-pin keeps disabled on host-passthrough."""

    def setUp(self):
        self.m = load_module("vm_pin", VM_PIN)
        self.names = self.m.CPU_DISABLE_DEFAULT.split()

    def features(self, out):
        return [(f.get("name"), f.get("policy")) for f in ET.fromstring(out).findall("cpu/feature")]

    def test_default_list(self):
        self.assertEqual(self.names, ["cet-ss", "cet-ibt", "vmx-mbec", "vmx-exit-save-cet", "vmx-entry-load-cet"])

    def test_self_closing_cpu(self):
        xml = "<domain><name>vm</name><vcpu>4</vcpu><cpu mode='host-passthrough' check='none'/></domain>\n"
        out = self.m.pinned(xml, 4, "8-11", self.names)
        self.assertEqual(self.features(out), [(n, "disable") for n in self.names])
        cpu = ET.fromstring(out).find("cpu")
        self.assertEqual(cpu.get("check"), "none")
        self.assertEqual(cpu.get("mode"), "host-passthrough")

    def test_topology_and_other_features_kept(self):
        out = self.m.pinned(SAMPLE_DOMAIN_XML.replace(
            '<topology sockets="1" dies="1" clusters="1" cores="24" threads="1"/>',
            '<topology sockets="1" dies="1" clusters="1" cores="24" threads="1"/>\n'
            '    <feature policy="require" name="invtsc"/>'), 12, "12-23", self.names)
        cpu = ET.fromstring(out).find("cpu")
        self.assertEqual(cpu[0].tag, "topology")
        self.assertEqual(cpu[0].get("cores"), "12")
        self.assertEqual(self.features(out), [("invtsc", "require")] + [(n, "disable") for n in self.names])

    def test_existing_feature_switched_in_place(self):
        xml = ("<domain><name>vm</name><vcpu>4</vcpu><cpu mode='host-passthrough'>"
               "<feature policy='require' name='vmx-mbec'/><feature policy='disable' name='cet-ss'/>"
               "</cpu></domain>\n")
        out = self.m.pinned(xml, 4, "8-11", self.names)
        got = self.features(out)
        self.assertEqual(got[:2], [("vmx-mbec", "disable"), ("cet-ss", "disable")])
        self.assertEqual(sorted(n for n, _ in got), sorted(self.names))
        self.assertTrue(all(p == "disable" for _, p in got))

    def test_idempotent(self):
        once = self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", self.names)
        self.assertEqual(self.m.pinned(once, 12, "12-23", self.names), once)

    def test_other_modes_untouched(self):
        for cpu in ("<cpu mode='host-model' check='partial'/>",
                    "<cpu mode='custom'><model>Skylake</model><feature policy='require' name='cet-ss'/></cpu>", ""):
            xml = f"<domain><name>vm</name><vcpu>4</vcpu>{cpu}</domain>\n"
            base = self.m.pinned(xml, 4, "8-11")
            self.assertEqual(self.m.pinned(xml, 4, "8-11", self.names), base, cpu)

    def test_empty_knob_untouched(self):
        self.assertEqual(self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", []),
                         self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23"))
        self.assertEqual(self.features(self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", "".split())), [])

    def test_conf_knob_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "cpu-setup.conf")
            with open(p, "w") as f:
                f.write("VM_NAME='x'\nVM_CPU_DISABLE=''\n")
            self.m.CONF = p
            self.assertEqual(self.m.conf()["VM_CPU_DISABLE"].split(), [])
            with open(p, "w") as f:
                f.write("VM_CPU_DISABLE='cet-ss vmx-mbec'\n")
            self.assertEqual(self.m.conf()["VM_CPU_DISABLE"].split(), ["cet-ss", "vmx-mbec"])
            self.m.CONF = os.path.join(tmp, "absent")
            self.assertEqual(self.m.conf()["VM_CPU_DISABLE"].split(), self.names)

    def test_messages(self):
        root = ET.fromstring(self.m.pinned(SAMPLE_DOMAIN_XML, 12, "12-23", self.names))
        self.assertIn("CPU features disabled: cet-ss", self.m.features_note(root, self.names))
        self.assertIn("not managed", self.m.features_note(root, []))
        other = ET.fromstring("<domain><cpu mode='host-model'/></domain>")
        self.assertIn("left alone", self.m.features_note(other, self.names))
        self.assertIn("NOT disabled yet: cet-ss", self.m.cpu_line(SAMPLE_DOMAIN_XML, self.names))
        self.assertIn("; disabled: cet-ss", self.m.cpu_line(ET.tostring(root, encoding="unicode"), self.names))


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
                stdin=subprocess.DEVNULL)
            self.assertEqual(r.stdout.strip(), "0 1 2 3 6 12 13 14")

    def test_off_reverts_in_reverse_order_and_cleans_state(self):
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
log() {{ :; }}
ctl_file() {{ echo {new_dropin!r}; }}
ctl() {{ echo "ctl $*" >> {log_file!r}; }}
systemctl() {{ echo "systemctl $*" >> {log_file!r}; }}
eval "$(sed -n '/^off()/,/^}}/p' {GAME_PROFILE!r})"
off
"""
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)
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

    def test_machine_slice_dropin_has_cpu_and_io_weight(self):
        dropin = os.path.join(CPU, "units", "machine.slice.d-10-cpu-setup.conf")
        with open(dropin) as f:
            lines = [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(lines, ["[Slice]", "CPUWeight=50", "IOWeight=50"])

    def _run_game_profile_on_off(self, vm_weight, prior_io_weight=None):
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
for fn in setp on off; do
    eval "$(sed -n "/^${{fn}}()/,/^}}/p" {GAME_PROFILE!r})"
done
on
cat {state_file!r} > {tmp!r}/state.after_on
cat {intended_file!r} > {tmp!r}/intended.after_on
off
test ! -e {intended_file!r}
"""
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL)
            self.assertEqual(r.stderr, "")
            with open(os.path.join(tmp, "state.after_on")) as f:
                state_after_on = f.read()
            with open(os.path.join(tmp, "intended.after_on")) as f:
                intended_after_on = f.read()
            with open(log_file) as f:
                actions = f.read().splitlines()
            io_file_exists_after = os.path.exists(
                os.path.join(ctrl_dir, "sys", "machine.slice.d", "50-IOWeight.conf")
            )
            return state_after_on, actions, io_file_exists_after, intended_after_on

    def test_on_and_off_machine_slice_io_weight(self):
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

    def test_setup_help_and_unknown_command(self):
        r = subprocess.run(
            ["bash", SETUP, "help"],
            env={**os.environ, "CST_PROG": "cst cpu"},
            capture_output=True, text=True, check=True,
            stdin=subprocess.DEVNULL)
        self.assertIn("Usage: cst cpu <command> [args]", r.stdout)

        bad = subprocess.run(
            ["bash", SETUP, "not-a-command"],
            capture_output=True, text=True,
            stdin=subprocess.DEVNULL)
        self.assertEqual(bad.returncode, 2)

    def test_e_cores_missing_returns_empty_cleanly(self):
        # On uniform CPUs without /sys/devices/cpu_atom/cpus, e_cores() must exit 0 under set -e.
        with open(SETUP) as f:
            src = f.read()
        line = next(l for l in src.splitlines() if l.startswith("e_cores()"))
        fn = line.replace("/sys/devices/cpu_atom/cpus", "/nonexistent/cpu_atom/cpus")
        r = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", f"{fn}\necores=\"$(e_cores)\"\nprintf '<%s>' \"$ecores\""],
            capture_output=True, text=True, check=True,
            stdin=subprocess.DEVNULL)
        self.assertEqual(r.stdout, "<>")


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


class GameProfileDriftTest(unittest.TestCase):
    """The baseline drop-in name, its migration, and the drift repair of the
    game profile (a daemon-reload or uresourced undoing a setting while the
    profile is on)."""

    def setUp(self):
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
        # The baseline as systemd shows it before the profile.
        self.set_now("sys", "machine.slice", CPUWeight="50", IOWeight="50")
        for u in ("user.slice", "user-999999.slice", "user@999999.service"):
            self.set_now("sys", u, MemoryLow="0")
        self.set_now("usr", "user.slice", CPUWeight="30")

    def set_now(self, mgr, unit, **props):
        d = os.path.join(self.store, mgr, unit)
        os.makedirs(d, exist_ok=True)
        for k, v in props.items():
            with open(os.path.join(d, k), "w") as f:
                f.write(v + "\n")

    def now(self, mgr, unit, prop):
        with open(os.path.join(self.store, mgr, unit, prop)) as f:
            return f.read().strip()

    def run_profile(self, body):
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
for fn in log prop setp on off drift repair_drift tick again_cmd state_drift; do
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
{body}
"""
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr, "")
        with open(self.actions) as f:
            return r.stdout, f.read().splitlines()

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_baseline_dropins_sort_before_the_runtime_overrides(self):
        # systemd reads drop-ins in file-name order (byte order) and the last
        # one read wins: the baseline must come first, so that the 50-<Prop>
        # files of `systemctl set-property --runtime` win after a reload.
        with open(SETUP) as f:
            src = f.read()
        name = next(l.split("=", 1)[1] for l in src.splitlines() if l.startswith("DROPIN_NAME="))
        old = next(l.split("=", 1)[1].split()[0] for l in src.splitlines() if l.startswith("OLD_DROPIN_NAME="))
        self.assertEqual(name, "10-cpu-setup.conf")
        self.assertTrue(os.path.exists(os.path.join(CPU, "units", f"machine.slice.d-{name}")))
        overrides = ["50-CPUWeight.conf", "50-IOWeight.conf", "50-AllowedCPUs.conf", "50-MemoryLow.conf",
                     "50-MemoryZSwapWriteback.conf"]
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
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.stdout.splitlines(), [
            "/home/u/.config/systemd/user/user.slice.d/10-cpu-setup.conf",
            "/home/u/.config/systemd/user/flatpak-session-helper.service.d/10-cpu-setup.conf",
            "/home/u/.config/systemd/user/user.slice.d/50-cpu-setup.conf",
        ], r.stderr)

    def test_install_and_uninstall_remove_the_old_name(self):
        home = os.path.join(self.tmp, "home")
        unit_dir = os.path.join(self.tmp, "etc-systemd")
        old = [os.path.join(unit_dir, "machine.slice.d", "50-cpu-setup.conf"),
               os.path.join(home, ".config/systemd/user/user.slice.d/50-cpu-setup.conf"),
               os.path.join(home, ".config/systemd/user/flatpak-session-helper.service.d/50-cpu-setup.conf")]
        keep = [os.path.join(unit_dir, "machine.slice.d", "10-cpu-setup.conf"),
                os.path.join(unit_dir, "machine.slice.d", "50-memory-low.conf"),     # the swap module's
                os.path.join(home, ".config/systemd/user/user.slice.d/10-cpu-setup.conf")]
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
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for p in old:
            self.assertFalse(os.path.exists(p), p)
        for p in keep:
            self.assertTrue(os.path.exists(p), p)
        self.assertEqual(r.stdout.count("removed"), 3, r.stdout)
        # install and uninstall both call it, and install reloads both managers after.
        with open(SETUP) as f:
            src = f.read()
        install = src[src.index("install_all() {"):src.index("\nuninstall() {")]
        uninstall = src[src.index("\nuninstall() {"):src.index("# ---- docker builds.slice")]
        self.assertLess(install.index("remove_old_dropins"), install.index("systemctl daemon-reload"))
        self.assertIn("usys daemon-reload", install)
        self.assertIn("remove_old_dropins", uninstall)

    def test_on_records_what_systemd_reads_back_and_not_for_scopes(self):
        out, actions = self.run_profile("on\nsetp usr app-flatpak-com.valvesoftware.Steam-1.scope MemoryLow=16G")
        intended = self.read(self.intended)
        self.assertIn("prop sys machine.slice CPUWeight 10 10\n", intended)
        self.assertIn("prop sys machine.slice IOWeight 10 10\n", intended)
        self.assertIn("prop sys user.slice MemoryLow 16G 17179869184\n", intended)
        self.assertIn("prop sys user@999999.service MemoryLow 16G 17179869184\n", intended)
        self.assertIn("prop usr user.slice CPUWeight idle idle\n", intended)
        self.assertNotIn("Steam", intended)
        self.assertIn("Steam-1.scope MemoryLow new -", self.read(self.state))   # off() still undoes it
        # game.state keeps the format off() reads: six fields at most.
        for line in self.read(self.state).splitlines():
            self.assertLessEqual(len(line.split()), 6, line)

    def test_failed_set_property_records_no_intended_value(self):
        self.run_profile('ctl() { echo "ctl $*" >>"${ACTIONS}"; [[ "$2" != set-property ]]; }\n'
                         ': >"${STATE}"; : >"${INTENDED}"\nsetp sys machine.slice CPUWeight=10')
        self.assertEqual(self.read(self.intended), "")
        self.assertIn("prop sys machine.slice CPUWeight new -", self.read(self.state))

    def test_tick_repairs_drift_without_touching_the_state_and_off_restores(self):
        # IOWeight had a runtime override of its own before the profile.
        self.set_now("sys", "machine.slice", IOWeight="80")
        os.makedirs(os.path.join(self.ctrl, "sys", "machine.slice.d"))
        with open(os.path.join(self.ctrl, "sys", "machine.slice.d", "50-IOWeight.conf"), "w") as f:
            f.write("[Slice]\nIOWeight=80\n")
        self.run_profile("on")
        state_after_on = self.read(self.state)
        open(self.manual, "w").close()            # on by hand: tick does not switch it off
        # A daemon-reload with the old baseline name, and uresourced:
        self.set_now("sys", "machine.slice", CPUWeight="50", IOWeight="50")
        self.set_now("sys", "user.slice", MemoryLow="0")
        self.set_now("usr", "user.slice", CPUWeight="30")
        out, actions = self.run_profile("tick")
        sets = [a for a in actions if "set-property" in a]
        self.assertEqual(sorted(sets), sorted([
            "ctl sys set-property --runtime machine.slice CPUWeight=10",
            "ctl sys set-property --runtime machine.slice IOWeight=10",
            "ctl sys set-property --runtime user.slice MemoryLow=16G",
            "ctl usr set-property --runtime user.slice CPUWeight=idle",
        ]))
        self.assertIn("<5>game-profile: drift: user.slice MemoryLow was '0' instead of '17179869184' "
                      "(daemon-reload or uresourced?): set MemoryLow=16G again", out)
        self.assertEqual(self.now("sys", "machine.slice", "CPUWeight"), "10")
        self.assertEqual(self.now("sys", "user.slice", "MemoryLow"), "17179869184")
        self.assertEqual(self.read(self.state), state_after_on)   # no second record
        # One show per unit, and nothing to repair on the next tick.
        shows = [a for a in actions if " show -p " in a]
        self.assertEqual(len(shows), 6, shows)   # machine.slice, 4 of the memory chain, toolboxes
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

    def test_cgroup_file_drift_is_rewritten(self):
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

    def test_unloaded_unit_and_empty_readback_are_not_repaired(self):
        with open(self.state, "w") as f:
            f.write("prop sys builds.slice CPUWeight new -\nprop usr app.slice MemoryZSwapWriteback new -\n")
        with open(self.intended, "w") as f:
            f.write("prop sys builds.slice CPUWeight idle idle\nprop usr app.slice MemoryZSwapWriteback no ''\n")
        open(os.path.join(self.store, "sys", "builds.slice.notloaded"), "w").close()
        open(self.manual, "w").close()
        out, actions = self.run_profile("tick")
        self.assertEqual([a for a in actions if "set-property" in a], [])
        self.assertEqual(out, "")

    def test_old_format_state_is_tolerated(self):
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

    def test_state_shows_drift_per_property(self):
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
        self.assertEqual([a for a in actions if "set-property" in a], [])   # state only reads


if __name__ == "__main__":
    unittest.main()

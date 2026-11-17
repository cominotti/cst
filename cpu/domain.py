# SPDX-License-Identifier: MPL-2.0
"""The cpu module's rules: vm-pin's rewrite of a libvirt domain definition
(vCPU count and pinning, the CPU features kept off) and cpu-watch's numbers.

Pure: text in, text or values out. Reading the host is cpu.adapters' job.
"""

from collections.abc import Callable, Mapping, Sequence
from itertools import islice
import re
from typing import Any, Literal, NamedTuple
import xml.etree.ElementTree as ET

from cst_core.domain.libvirt_xml import keep_prefixes

URI = "qemu:///system"
GAME_STATE = "/run/cpu-setup/game.state"  # cpu/game-profile: the profile is on
# The columns of a cpu-watch run's samples.csv (watch writes them, summarize reads them by name).
SAMPLE_FIELDS = (
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
)

# CPU features hidden from a host-passthrough guest. QEMU 11 (Fedora 45)
# started to pass Intel CET (shadow stacks, indirect branch tracking) and
# nested MBEC to the guest; Windows 11 with Hyper-V/VBS/WSL2 then loops in
# Automatic Repair. Upstream reports:
#   https://gitlab.com/qemu-project/qemu/-/issues/3215
#   https://forum.proxmox.com/threads/windows-11-bootloop-with-kernel-7-0-works-with-kernel-6-17.185608/
# Retire this list (set VM_CPU_DISABLE='' and re-apply) once QEMU/KVM or
# Windows fix it upstream. cpu-setup.conf's VM_CPU_DISABLE overrides it;
# cst vm (status) and cst doctor import it from here.
CPU_DISABLE_DEFAULT: str = "cet-ss cet-ibt vmx-mbec vmx-exit-save-cet vmx-entry-load-cet"


def disable_features(root: ET.Element, names: Sequence[str]) -> None:
    """With <cpu mode='host-passthrough'>, every feature in names present as
    <feature policy='disable' name=.../>: an existing one keeps its place
    (only its policy changes), missing ones are appended. Other modes, or no
    <cpu>, are left alone."""
    cpu = root.find("cpu")
    if not names or cpu is None or cpu.get("mode") != "host-passthrough":
        return
    have: dict[str, ET.Element] = {name: f for f in cpu.findall("feature") if (name := f.get("name")) is not None}
    for n in names:
        if n in have:
            have[n].set("policy", "disable")
        else:
            ET.SubElement(cpu, "feature", {"policy": "disable", "name": n})


def pinned(xml_text: str, vcpus: int, cpus: str, disable: Sequence[str] = ()) -> str:
    keep_prefixes(xml_text)
    root = ET.fromstring(xml_text)
    vcpu = root.find("vcpu")
    assert vcpu is not None
    vcpu.text = str(vcpus)
    vcpu.attrib.pop("current", None)
    vcpu.set("placement", "static")
    # A topology must multiply up to the vCPU count.
    topo = root.find("cpu/topology")
    if topo is not None:
        for k, v in (("sockets", "1"), ("dies", "1"), ("clusters", "1"), ("cores", str(vcpus)), ("threads", "1")):
            if k in topo.attrib or k in ("sockets", "cores", "threads"):
                topo.set(k, v)
    tune = root.find("cputune")
    if tune is None:
        tune = ET.Element("cputune")
        root.insert(list(root).index(vcpu) + 1, tune)
    for el in list(tune):
        if el.tag in ("vcpupin", "emulatorpin"):
            tune.remove(el)
    for i in range(vcpus):
        ET.SubElement(tune, "vcpupin", {"vcpu": str(i), "cpuset": cpus})
    ET.SubElement(tune, "emulatorpin", {"cpuset": cpus})
    disable_features(root, disable)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def features_note(root: ET.Element, disable: Sequence[str], planned: bool = False) -> str:
    """How the CPU features stand (or will stand, planned) for the messages."""
    cpu = root.find("cpu")
    mode = cpu.get("mode") if cpu is not None else None
    if not disable:
        return "CPU features not managed (VM_CPU_DISABLE is empty)"
    if mode != "host-passthrough":
        return f"CPU features left alone (cpu mode {mode or 'not set'}, not host-passthrough)"
    return f"CPU features {'to be ' if planned else ''}disabled: {' '.join(disable)}"


def cpu_line(xml_text: str, disable: Sequence[str]) -> str:
    """One status line: the cpu mode and which managed features are off."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "cpu: definition not readable"
    cpu = root.find("cpu")
    mode = cpu.get("mode") if cpu is not None else "not set"
    off = {f.get("name") for f in (cpu.findall("feature") if cpu is not None else []) if f.get("policy") == "disable"}
    missing = [n for n in disable if n not in off]
    if not disable or mode != "host-passthrough":
        return f"cpu mode: {mode} ({features_note(root, disable)})"
    if missing:
        return f"cpu mode: {mode}; NOT disabled yet: {' '.join(missing)} (fix: cst cpu vm-pin plan)"
    return f"cpu mode: {mode}; disabled: {' '.join(disable)}"


# ---- cpu-watch ---------------------------------------------------------------
def pressure_field(text: str, kind: Literal["some", "full"], key: str) -> str | None:
    """The KEY= value (as text) on the KIND line ('some' or 'full') of a
    pressure file's text, or None."""
    m = re.search(rf"^{kind} (?:.* )?{key}=(\d+(?:\.\d+)?)\b", text, re.M)
    return m.group(1) if m else None


def lows(ft: Sequence[float]) -> tuple[float, float, float] | None:
    """avg fps, 1% low and 0.1% low (fps of the mean of the worst frames)."""
    if not ft:
        return None
    s = sorted(ft, reverse=True)

    def low(frac: float) -> float:
        worst = s[: max(1, int(len(s) * frac))]
        return 1000 / (sum(worst) / len(worst))

    return 1000 / (sum(ft) / len(ft)), low(0.01), low(0.001)


# ---- quiet: is the machine quiet enough for a benchmark? ------------------------
#
# The defaults come from a 3-day `below` recording of this machine (cpu/README.md,
# "Is the machine quiet?"): the CPU and write limits pass the machine's quiet
# hours with room to spare; the pressure limits are reasoned from its quietest window.


def stat_cpu(text: str) -> tuple[int, int]:
    """(total, idle) jiffies of the 'cpu' line that /proc/stat starts with."""
    f = text.split("\n", 1)[0].split()
    # user nice system idle iowait irq softirq steal; guest and guest_nice
    # (after them) are already counted in user and nice.
    if f[:1] != ["cpu"] or len(f) < 9:
        raise ValueError("no 'cpu' line with 8 counters first")
    v = [int(x) for x in f[1:9]]
    return sum(v), v[3] + v[4]  # iowait is idle time too


def vmstat_pgpgout(text: str) -> int:
    """KiB written to the disks since boot (/proc/vmstat pgpgout)."""
    m = re.search(r"^pgpgout (\d+)$", text, re.M)
    if not m:
        raise ValueError("no 'pgpgout' line")
    return int(m.group(1))


def some_avg10(text: str) -> float:
    """The 'some avg10=' share (%) of a pressure file."""
    v = pressure_field(text, "some", "avg10")
    if v is None:
        raise ValueError("no 'some avg10=' line")
    return float(v)


def busy_pct(window: Sequence[Mapping[str, Any]], name: str) -> float:
    """CPU busy (%) from the counters at both ends of the window."""
    (total0, idle0), (total1, idle1) = window[0]["v"][name], window[-1]["v"][name]
    if total1 <= total0:
        raise ValueError("/proc/stat: the CPU counters did not advance")
    return 100.0 * ((total1 - total0) - (idle1 - idle0)) / (total1 - total0)


def write_mbps(window: Sequence[Mapping[str, Any]], name: str) -> float:
    """MB written per second, from the counters at both ends of the window."""
    elapsed = window[-1]["t"] - window[0]["t"]
    if elapsed <= 0:
        raise ValueError("the clock did not advance")
    # KiB -> MB (1 MB = 1024 KiB)
    return (window[-1]["v"][name] - window[0]["v"][name]) / 1024 / elapsed


def mean_of_seconds(window: Sequence[Mapping[str, Any]], name: str) -> float:
    """Mean of each second's reading (the first reading only starts the window)."""
    return sum(s["v"][name] for s in islice(window, 1, None)) / (len(window) - 1)


# One row per test. file: under /proc; parse: its text -> one reading (or
# ValueError); value: (window of readings, name) -> the window's value.
class Limit(NamedTuple):
    name: str
    label: str
    unit: str
    flag: str
    env: str
    default: float
    file: str
    parse: Callable[[str], Any]
    value: Callable[[Sequence[Mapping[str, Any]], str], float]


QUIET_LIMITS = (
    Limit("cpu", "CPU busy", "%", "--cpu", "QUIET_CPU_PCT", 10.0, "stat", stat_cpu, busy_pct),
    Limit(
        "cpu-pressure",
        "CPU pressure",
        "%",
        "--cpu-pressure",
        "QUIET_CPU_PSI",
        5.0,
        "pressure/cpu",
        some_avg10,
        mean_of_seconds,
    ),
    Limit(
        "io-pressure",
        "IO pressure",
        "%",
        "--io-pressure",
        "QUIET_IO_PSI",
        5.0,
        "pressure/io",
        some_avg10,
        mean_of_seconds,
    ),
    Limit(
        "memory-pressure",
        "memory pressure",
        "%",
        "--memory-pressure",
        "QUIET_MEMORY_PSI",
        1.0,
        "pressure/memory",
        some_avg10,
        mean_of_seconds,
    ),
    Limit(
        "writes", "disk writes", "MB/s", "--write-mbps", "QUIET_WRITE_MBPS", 10.0, "vmstat", vmstat_pgpgout, write_mbps
    ),
)


def quiet_tests(window: Sequence[Mapping[str, Any]], limits: Mapping[str, float]) -> list[dict[str, Any]]:
    """The tests of one window (SECONDS + 1 readings, a second apart): CPU busy
    and writes from the counters at both ends (the window's mean), pressure as
    the mean of each second's avg10, the game profile off in every reading."""
    tests: list[dict[str, Any]] = []
    for lim in QUIET_LIMITS:
        value: float | None = None
        err = next((s["err"][lim.name] for s in window if lim.name in s["err"]), None)
        if err is None:
            try:
                # Rounded before the comparison: the verdict matches the printed value.
                value = round(lim.value(window, lim.name), 2)
            except ValueError as e:
                err = str(e)
        tests.append(
            {
                "name": lim.name,
                "label": lim.label,
                "unit": lim.unit,
                "limit": limits[lim.name],
                "value": value,
                "ok": err is None and value is not None and value < limits[lim.name],
                "error": err,
            }
        )
    on = sum(1 for s in window if s["game"])
    tests.append(
        {
            "name": "game-profile",
            "label": "game profile",
            "unit": "",
            "limit": "off",
            "value": "off" if not on else f"on in {on} of {len(window)} readings",
            "ok": not on,
            "error": None,
        }
    )
    return tests


def duration(text: str) -> float:
    """Seconds from 120, 90s, 30m or 2h."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", text.strip())
    if not m or float(m.group(1)) <= 0:
        raise ValueError(f"not a duration: {text!r} (examples: 120, 90s, 30m, 2h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def quiet_args(argv: Sequence[str], environ: Mapping[str, str]) -> tuple[int, float | None, bool, dict[str, float]]:
    """(seconds, wait or None, json, limits), or ValueError for a usage error."""
    limits = {
        lim.name: positive(environ[lim.env], lim.env) if environ.get(lim.env, "") != "" else lim.default
        for lim in QUIET_LIMITS
    }
    seconds = 60
    wait: float | None = None
    as_json = False
    flags = {lim.flag: lim.name for lim in QUIET_LIMITS}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
            i += 1
            continue
        if a not in flags and a not in ("--for", "--wait"):
            raise ValueError(f"unknown argument {a}")
        if i + 1 >= len(argv):
            raise ValueError(f"{a} needs a value")
        val = argv[i + 1]
        if a == "--for":
            if not re.fullmatch(r"\d+", val) or int(val) < 1:
                raise ValueError(f"--for: a whole number of seconds, at least 1 (got {val!r})")
            seconds = int(val)
        elif a == "--wait":
            wait = duration(val)
        else:
            limits[flags[a]] = positive(val, a)
        i += 2
    if wait is not None and wait < seconds:
        raise ValueError(f"--wait ({wait:g} s) is shorter than the window (--for {seconds})")
    return seconds, wait, as_json, limits


def positive(text: str, what: str) -> float:
    """TEXT as a finite number above 0, or ValueError naming WHAT."""
    try:
        v = float(text)
        if 0 < v < float("inf"):
            return v
    except ValueError:
        pass
    raise ValueError(f"{what}: a number above 0 (got {text!r})")


def quiet_text(result: Mapping[str, Any]) -> str:
    lines = []
    for t in result["tests"]:
        unit = f" {t['unit']}" if t["unit"] else ""
        if t["error"]:
            value = "unreadable"
        elif isinstance(t["value"], float):
            value = f"{t['value']:.2f}{unit}"
        else:
            value = t["value"]
        limit = f"< {t['limit']:g}{unit}" if t["unit"] else t["limit"]
        verdict = "ok" if t["ok"] else "FAIL" + (f" ({t['error']})" if t["error"] else "")
        lines.append(f"  {t['label']:<16} {value:<12} limit {limit:<11} {verdict}")
    span = f"{result['seconds']} s, {result['start']} to {result['end']}"
    if result.get("waited_s") is not None:
        span += f", after waiting {result['waited_s']:.0f} s"
    labels = {t["name"]: t["label"] for t in result["tests"]}
    failed = ", ".join(labels[name] for name in result["failed"])
    lines.append(f"quiet ({span})" if result["quiet"] else f"NOT quiet: {failed} ({span})")
    return "\n".join(lines)

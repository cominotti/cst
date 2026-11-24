# SPDX-License-Identifier: MPL-2.0
"""The cpu module's rules: vm-pin's rewrite of a libvirt domain definition
(vCPU count and pinning, the CPU features kept off) and cpu-watch's numbers
and records.

Pure: text in, text or values out. Reading the host is cpu.adapters' job.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import difflib
from enum import StrEnum
from itertools import islice
import posixpath
import re
from typing import Final, Literal, NamedTuple, NotRequired, TypedDict
import xml.etree.ElementTree as ET

from cst_core.domain.contracts import CPU_SETTINGS, StateFormatKey, state_format
from cst_core.domain.errors import Refused
from cst_core.domain.libvirt_xml import keep_prefixes
from cst_core.domain.text import group
from cst_core.domain.values import DomainValidationError, Duration, parse_whole_number

URI = "qemu:///system"


class VmPinAction(StrEnum):
    """What `cpu-vm-pin` does (its first argument): show, show the change, or make it."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"


class CpuWatchOption(StrEnum):
    """The options of `cpu-watch watch` (END: the command to record follows)."""

    END = "--"
    LABEL_SHORT = "-l"
    LABEL = "--label"
    GAME = "--game"
    SECONDS = "--seconds"
    MANGOHUD = "--mangohud"


GAME_STATE = state_format(StateFormatKey.GAME_STATE).path  # cpu/game-profile: the profile is on
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

# Elements tied to the vCPU count that vm-pin does not rewrite.
VCPU_TIED = ("cputune/vcpusched", "cputune/cachetune", "cpu/numa", "vcpus")


@dataclass(frozen=True, slots=True)
class CpuPaths:
    """Where the cpu programs read and write: runs, cpu-watch's recorded runs
    (~/.local/state/cpu-setup/runs of the user running it); conf, the settings
    file; etc, where vm-pin keeps the definition it replaced."""

    runs: str
    conf: str = CPU_SETTINGS
    etc: str = posixpath.dirname(CPU_SETTINGS)


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
    for el in [el for el in tune if el.tag in ("vcpupin", "emulatorpin")]:
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


@dataclass(frozen=True, slots=True)
class PinPlan:
    """What vm-pin would change: diff, the unified diff of the indented
    current definition against after (empty: nothing to do); after, the
    pinned definition; features and planned_features, how the CPU features
    stand now and once applied (features_note)."""

    diff: tuple[str, ...]
    after: str
    features: str
    planned_features: str


def pin_plan(name: str, cur: str, vcpus: int, cpus: str, disable: Sequence[str]) -> PinPlan:
    """The plan for domain NAME, whose definition is CUR (virsh dumpxml);
    Refused when it has an element tied to the vCPU count (VCPU_TIED)."""
    keep_prefixes(cur)
    croot = ET.fromstring(cur)
    for path in VCPU_TIED:
        if croot.find(path) is not None:
            raise Refused(f"{name} has <{path}>: adjust it by hand (virsh edit {name}); nothing changed")
    cur_root = ET.fromstring(cur)
    ET.indent(cur_root, space="  ")
    before = ET.tostring(cur_root, encoding="unicode") + "\n"
    after = pinned(cur, vcpus, cpus, disable)
    diff = tuple(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "current", "pinned"))
    return PinPlan(diff, after, features_note(croot, disable), features_note(croot, disable, True))


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
@dataclass(frozen=True, slots=True)
class WatchCounters:
    """One reading of the cumulative counters cpu-watch turns into rates: t,
    the wall clock; sys, the system's CPU pressure (some, µs); vm and builds,
    the CPU time (µs) of machine.slice and of the build cgroups; game, the
    game cgroup's CPU and memory pressure (µs) and the game's involuntary
    context switches, when a game is found."""

    t: float
    sys: int
    vm: int
    builds: int
    game: tuple[int, int, int] | None = None


def watch_row(
    cur: WatchCounters, prev: WatchCounters, *, start: float, profile_on: bool, pid: int | None, p_mhz: int, e_mhz: int
) -> list[object]:
    """One samples.csv row (SAMPLE_FIELDS) from two readings, a second or so apart."""
    dt = cur.t - prev.t
    # per second of wall time (samples drift a little past 1 s)
    game: list[object] = ["", "", ""]
    if cur.game is not None and prev.game is not None:
        g_cur, g_prev = cur.game, prev.game
        game = [
            round((g_cur[0] - g_prev[0]) / 1000 / dt, 1),
            round((g_cur[1] - g_prev[1]) / 1000 / dt, 1),
            round((g_cur[2] - g_prev[2]) / dt),
        ]
    return [
        round(cur.t - start, 1),
        int(profile_on),
        pid or "",
        *game,
        round((cur.sys - prev.sys) / 1000 / dt, 1),
        p_mhz,
        e_mhz,
        round((cur.vm - prev.vm) / 1e6 / dt, 2),
        round(max(0, cur.builds - prev.builds) / 1e6 / dt, 2),
    ]


def run_folder(start: float, label: str) -> str:
    """The name of a run's folder: its start time (local) and its label."""
    return datetime.fromtimestamp(start).strftime("%Y%m%dT%H%M%S") + "-" + label


class RunMeta(TypedDict):
    """A run's meta.json (written once, at the end of the run)."""

    label: str
    start: float
    end: float
    cmd: list[str]
    game: str | None
    rc: int | None
    mangohud: NotRequired[str]


def pressure_field(text: str, kind: Literal["some", "full"], key: str) -> str | None:
    """The KEY= value (as text) on the KIND line ('some' or 'full') of a
    pressure file's text, or None."""
    m = re.search(rf"^{kind} (?:.* )?{key}=(\d+(?:\.\d+)?)\b", text, re.MULTILINE)
    return group(m, 1) if m else None


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


# One reading of a test's file: (total, idle) jiffies, KiB written or a share (%).
type Reading = tuple[int, int] | int | float


class Readings(TypedDict):
    """What every reading of a window has: each test's value by test name."""

    v: Mapping[str, Reading]


class TimedReadings(Readings):
    """Readings with the monotonic clock they were taken at."""

    t: float


class QuietSample(TimedReadings):
    """One reading of the quiet tests' files: wall, the wall clock; game,
    whether the game profile is on; err, the reason a test's file could not
    be read or parsed, by test name (that test has no value then)."""

    wall: float
    game: bool
    err: Mapping[str, str]


class QuietTest(TypedDict):
    """One test of a window, as --json prints it."""

    name: str
    label: str
    unit: str
    limit: float | str
    value: float | str | None
    ok: bool
    error: str | None


class QuietResult(TypedDict):
    """The verdict on a window, as --json prints it."""

    quiet: bool
    seconds: int
    start: str
    end: str
    waited_s: float | None
    tests: list[QuietTest]
    failed: list[str]


def counters(r: Reading) -> tuple[int, int]:
    """A reading that is a pair of counters (stat_cpu)."""
    if not isinstance(r, tuple):
        raise TypeError(f"not a pair of counters: {r!r}")
    return r


def number(r: Reading) -> float:
    """A reading that is one number (vmstat_pgpgout, some_avg10)."""
    if isinstance(r, tuple):
        raise TypeError(f"not a number: {r!r}")
    return r


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
    m = re.search(r"^pgpgout (\d+)$", text, re.MULTILINE)
    if not m:
        raise ValueError("no 'pgpgout' line")
    return int(m.group(1))


def some_avg10(text: str) -> float:
    """The 'some avg10=' share (%) of a pressure file."""
    v = pressure_field(text, "some", "avg10")
    if v is None:
        raise ValueError("no 'some avg10=' line")
    return float(v)


def busy_pct(window: Sequence[Readings], name: str) -> float:
    """CPU busy (%) from the counters at both ends of the window."""
    (total0, idle0), (total1, idle1) = counters(window[0]["v"][name]), counters(window[-1]["v"][name])
    if total1 <= total0:
        raise ValueError("/proc/stat: the CPU counters did not advance")
    return 100.0 * ((total1 - total0) - (idle1 - idle0)) / (total1 - total0)


def write_mbps(window: Sequence[TimedReadings], name: str) -> float:
    """MB written per second, from the counters at both ends of the window."""
    elapsed = window[-1]["t"] - window[0]["t"]
    if elapsed <= 0:
        raise ValueError("the clock did not advance")
    # KiB -> MB (1 MB = 1024 KiB)
    return (number(window[-1]["v"][name]) - number(window[0]["v"][name])) / 1024 / elapsed


def mean_of_seconds(window: Sequence[Readings], name: str) -> float:
    """Mean of each second's reading (the first reading only starts the window)."""
    return sum(number(s["v"][name]) for s in islice(window, 1, None)) / (len(window) - 1)


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
    parse: Callable[[str], Reading]
    value: Callable[[Sequence[QuietSample], str], float]


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


def quiet_tests(window: Sequence[QuietSample], limits: Mapping[str, float]) -> list[QuietTest]:
    """The tests of one window (SECONDS + 1 readings, a second apart): CPU busy
    and writes from the counters at both ends (the window's mean), pressure as
    the mean of each second's avg10, the game profile off in every reading."""
    tests: list[QuietTest] = []
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
    """Seconds from 120, 1.5, 90s, 30m, 2h, 1d or 1w (Duration.parse_float: ASCII digits, above 0 and
    finite); ValueError with --wait's message for anything else."""
    try:
        return Duration.parse_float(text, allow_unitless=True, positive_only=True)
    except DomainValidationError:
        raise ValueError(f"not a duration: {text!r} (examples: 120, 90s, 30m, 2h)") from None


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
    known = {*flags, "--for", "--wait"}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--json":
            as_json = True
            i += 1
            continue
        if a not in known:
            raise ValueError(f"unknown argument {a}")
        if i + 1 >= len(argv):
            raise ValueError(f"{a} needs a value")
        val = argv[i + 1]
        if a == "--for":
            seconds = _window_seconds(val)
        elif a == "--wait":
            wait = duration(val)
        else:
            limits[flags[a]] = positive(val, a)
        i += 2
    if wait is not None and wait < seconds:
        raise ValueError(f"--wait ({wait:g} s) is shorter than the window (--for {seconds})")
    return seconds, wait, as_json, limits


# The longest --for window: quiet keeps SECONDS + 1 readings in a deque, whose maxlen is at most
# sys.maxsize (2**63 - 1 on the 64-bit machines CST runs on; domain code does not read sys).
MAX_WINDOW_SECONDS: Final = (1 << 63) - 2


def _window_seconds(val: str) -> int:
    """--for's value: a whole number of seconds, from 1 to MAX_WINDOW_SECONDS; ValueError for anything else."""
    seconds = parse_whole_number(val)  # ASCII digits within int()'s digit limit
    if seconds is None or not 1 <= seconds <= MAX_WINDOW_SECONDS:
        raise ValueError(f"--for: a whole number of seconds, from 1 to {MAX_WINDOW_SECONDS} (got {val!r})")
    return seconds


# A limit's number: ASCII digits, a fraction and an exponent allowed, blanks around it (float() alone also
# took every Unicode digit, Unicode blanks and 1_0).
_LIMIT_NUMBER: Final = re.compile(r"[ \t]*(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?[ \t]*")


def positive(text: str, what: str) -> float:
    """TEXT as a finite number above 0 (_LIMIT_NUMBER), or ValueError naming WHAT."""
    if _LIMIT_NUMBER.fullmatch(text):
        v = float(text)
        if 0 < v < float("inf"):
            return v
    raise ValueError(f"{what}: a number above 0 (got {text!r})")


def stamp(s: QuietSample) -> str:
    """A reading's wall clock, as the result shows it (local time)."""
    return datetime.fromtimestamp(s["wall"]).isoformat(sep=" ", timespec="seconds")


def quiet_result(
    window: Sequence[QuietSample], tests: list[QuietTest], *, ok: bool, seconds: int, waited_s: float | None
) -> QuietResult:
    """The verdict on the last window (OK: every test passed)."""
    return {
        "quiet": ok,
        "seconds": seconds,
        "start": stamp(window[0]),
        "end": stamp(window[-1]),
        "waited_s": waited_s,
        "tests": tests,
        "failed": [t["name"] for t in tests if not t["ok"]],
    }


def _quiet_test_line(t: QuietTest) -> str:
    """One test's line of quiet's result: its value, its limit and its verdict."""
    unit = f" {t['unit']}" if t["unit"] else ""
    err = t["error"]
    unreadable = bool(err)
    if unreadable:
        value = "unreadable"
    elif isinstance(t["value"], float):
        value = f"{t['value']:.2f}{unit}"
    else:
        value = t["value"]
    limit = f"< {t['limit']:g}{unit}" if t["unit"] else t["limit"]
    verdict = "ok"
    if not t["ok"]:
        verdict = f"FAIL ({err})" if unreadable else "FAIL"
    return f"  {t['label']:<16} {value:<12} limit {limit:<11} {verdict}"


def quiet_text(result: QuietResult) -> str:
    lines = [_quiet_test_line(t) for t in result["tests"]]
    span = f"{result['seconds']} s, {result['start']} to {result['end']}"
    if result.get("waited_s") is not None:
        span += f", after waiting {result['waited_s']:.0f} s"
    labels = {t["name"]: t["label"] for t in result["tests"]}
    failed = ", ".join(labels[name] for name in result["failed"])
    lines.append(f"quiet ({span})" if result["quiet"] else f"NOT quiet: {failed} ({span})")
    return "\n".join(lines)

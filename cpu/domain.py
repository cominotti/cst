"""Pure domain model, configuration value objects, and scheduling/pinning rules for `cpu`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import io
import math
import re
from typing import Final
import xml.etree.ElementTree as ET

URI: Final = "qemu:///system"
CONF: Final = "/etc/cpu-setup/cpu-setup.conf"
CPU_DISABLE_DEFAULT: Final = "cet-ss cet-ibt vmx-mbec vmx-exit-save-cet vmx-entry-load-cet"
GAME_STATE: Final = "/run/cpu-setup/game.state"
GAME_DROPIN: Final = "zz-cst-game.conf"
DROPIN_NAME: Final = "10-cpu-setup.conf"
OLD_DROPIN_NAME: Final = "50-cpu-setup.conf"

UNSUPPORTED_VCPU_PATHS: Final[tuple[str, ...]] = (
    "cputune/vcpusched",
    "cputune/cachetune",
    "cpu/numa",
    "vcpus",
)

QUIET_LIMIT_SPECS: Final[tuple[tuple[str, str, str, float], ...]] = (
    ("cpu", "--cpu", "QUIET_CPU_PCT", 10.0),
    ("cpu-pressure", "--cpu-pressure", "QUIET_CPU_PSI", 5.0),
    ("io-pressure", "--io-pressure", "QUIET_IO_PSI", 5.0),
    ("memory-pressure", "--memory-pressure", "QUIET_MEMORY_PSI", 1.0),
    ("writes", "--write-mbps", "QUIET_WRITE_MBPS", 10.0),
)


@dataclass(frozen=True, slots=True)
class CpuSetupConfig:
    """Validated settings read from `/etc/cpu-setup/cpu-setup.conf`."""

    vm_name: str = "win11-devbox"
    vm_vcpus: int = 12
    build_cpus: str = "12-23"
    p_cores: str = "0-7"
    vm_weight: str = "10"
    game_memory_low: str = "16G"
    game_zswap_writeback: str = "no"
    vm_cpu_disable: tuple[str, ...] = ("cet-ss", "cet-ibt", "vmx-mbec", "vmx-exit-save-cet", "vmx-entry-load-cet")
    target_user: str = ""

    @classmethod
    def from_text(cls, text: str) -> CpuSetupConfig:
        """Parse `KEY=VALUE` assignments from `cpu-setup.conf` text."""
        vals: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, val = stripped.split("=", 1)
            vals[key.strip()] = val.strip().strip("'\"")
        raw_disable = vals.get("VM_CPU_DISABLE", CPU_DISABLE_DEFAULT)
        return cls(
            vm_name=vals.get("VM_NAME", "win11-devbox"),
            vm_vcpus=int(vals.get("VM_VCPUS", "12")),
            build_cpus=vals.get("BUILD_CPUS", "12-23"),
            p_cores=vals.get("P_CORES", "0-7"),
            vm_weight=vals.get("VM_WEIGHT", "10"),
            game_memory_low=vals.get("GAME_MEMORY_LOW", "16G"),
            game_zswap_writeback=vals.get("GAME_ZSWAP_WRITEBACK", "no"),
            vm_cpu_disable=tuple(raw_disable.split()),
            target_user=vals.get("TARGET_USER", ""),
        )


@dataclass(frozen=True, slots=True)
class FrameStats:
    """Summary FPS and frame-pacing percentiles derived from MangoHud frame times."""

    avg_fps: float
    low_1pct_fps: float
    low_01pct_fps: float


@dataclass(frozen=True, slots=True)
class QuietQuery:
    """Validated arguments and thresholds for `cpu-watch quiet`."""

    seconds: int
    wait: float | None
    as_json: bool
    limits: dict[str, float]


def expand_cpu_range(spec: str) -> list[int]:
    """Expand a Linux CPU list such as `0-3,6,12-14` into individual CPU IDs."""
    out: list[int] = []
    for raw_part in spec.strip().split(","):
        part = raw_part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            out.extend(range(int(start_s), int(end_s) + 1))
        else:
            out.append(int(part))
    return out


def keep_prefixes(xml_text: str) -> None:
    """Register XML namespace prefixes so serialization does not rename them to `ns0`."""
    for _, (prefix, uri) in ET.iterparse(io.StringIO(xml_text), events=("start-ns",)):
        if prefix and not re.fullmatch(r"ns\d+", prefix):
            ET.register_namespace(prefix, uri)


def disable_features(root: ET.Element, names: Sequence[str]) -> None:
    """Ensure named CPU features have `policy='disable'` under `host-passthrough`."""
    cpu = root.find("cpu")
    if not names or cpu is None or cpu.get("mode") != "host-passthrough":
        return
    have: dict[str, ET.Element] = {
        feat_name: feat for feat in cpu.findall("feature") if (feat_name := feat.get("name")) is not None
    }
    for name in names:
        if name in have:
            have[name].set("policy", "disable")
        else:
            ET.SubElement(cpu, "feature", {"policy": "disable", "name": name})


def pinned(xml_text: str, vcpus: int, cpus: str, disable: Sequence[str] = ()) -> str:
    """Rewrite `<vcpu>`, topology, `<cputune>` pinning, and disabled CPU features."""
    keep_prefixes(xml_text)
    root = ET.fromstring(xml_text)
    vcpu = root.find("vcpu")
    if vcpu is None:
        raise ValueError("domain XML has no <vcpu> element")
    vcpu.text = str(vcpus)
    vcpu.attrib.pop("current", None)
    vcpu.set("placement", "static")
    topo = root.find("cpu/topology")
    if topo is not None:
        for key, val in (("sockets", "1"), ("dies", "1"), ("clusters", "1"), ("cores", str(vcpus)), ("threads", "1")):
            if key in topo.attrib or key in ("sockets", "cores", "threads"):
                topo.set(key, val)
    tune = root.find("cputune")
    if tune is None:
        tune = ET.Element("cputune")
        root.insert(list(root).index(vcpu) + 1, tune)
    for el in list(tune):
        if el.tag in ("vcpupin", "emulatorpin"):
            tune.remove(el)
    for idx in range(vcpus):
        ET.SubElement(tune, "vcpupin", {"vcpu": str(idx), "cpuset": cpus})
    ET.SubElement(tune, "emulatorpin", {"cpuset": cpus})
    disable_features(root, disable)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def features_note(root: ET.Element, disable: Sequence[str], planned: bool = False) -> str:
    """Describe how `VM_CPU_DISABLE` applies to the domain's `<cpu>` element."""
    cpu = root.find("cpu")
    mode = cpu.get("mode") if cpu is not None else None
    if not disable:
        return "CPU features not managed (VM_CPU_DISABLE is empty)"
    if mode != "host-passthrough":
        return f"CPU features left alone (cpu mode {mode or 'not set'}, not host-passthrough)"
    return f"CPU features {'to be ' if planned else ''}disabled: {' '.join(disable)}"


def cpu_line(xml_text: str, disable: Sequence[str]) -> str:
    """Format a one-line status summary of domain CPU mode and disabled features."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return "cpu: definition not readable"
    cpu = root.find("cpu")
    mode = cpu.get("mode") if cpu is not None else "not set"
    off = {
        feat.get("name")
        for feat in (cpu.findall("feature") if cpu is not None else [])
        if feat.get("policy") == "disable"
    }
    missing = [name for name in disable if name not in off]
    if not disable or mode != "host-passthrough":
        return f"cpu mode: {mode} ({features_note(root, disable)})"
    if missing:
        return f"cpu mode: {mode}; NOT disabled yet: {' '.join(missing)} (fix: cst cpu vm-pin plan)"
    return f"cpu mode: {mode}; disabled: {' '.join(disable)}"


def unsupported_vcpu_elements(root: ET.Element) -> list[str]:
    """Return any XML paths tied to vCPU count that `vm-pin` refuses to rewrite."""
    return [path for path in UNSUPPORTED_VCPU_PATHS if root.find(path) is not None]


def pressure_field(text: str, kind: str, key: str) -> str | None:
    """Extract `key=` value on `kind` (`some` or `full`) line of a PSI file."""
    m = re.search(rf"^{kind} (?:.* )?{key}=(\d+(?:\.\d+)?)\b", text, re.M)
    return m.group(1) if m else None


def lows(ft: Sequence[float]) -> tuple[float, float, float] | None:
    """Return `(avg_fps, low_1pct_fps, low_01pct_fps)` from frame times in ms."""
    if not ft:
        return None
    sorted_ft = sorted(ft, reverse=True)

    def low(frac: float) -> float:
        worst = sorted_ft[: max(1, int(len(sorted_ft) * frac))]
        return 1000.0 / (sum(worst) / len(worst))

    return 1000.0 / (sum(ft) / len(ft)), low(0.01), low(0.001)


def frame_stats(ft: Sequence[float]) -> FrameStats | None:
    """Convert MangoHud frame times into a `FrameStats` value object."""
    res = lows(ft)
    if res is None:
        return None
    return FrameStats(avg_fps=res[0], low_1pct_fps=res[1], low_01pct_fps=res[2])


def stat_cpu(text: str) -> tuple[int, int]:
    """Parse `(total, idle)` jiffies from the first `cpu` line of `/proc/stat`."""
    fields = text.split("\n", 1)[0].split()
    if fields[:1] != ["cpu"] or len(fields) < 9:
        raise ValueError("no 'cpu' line with 8 counters first")
    vals = [int(x) for x in fields[1:9]]
    return sum(vals), vals[3] + vals[4]


def vmstat_pgpgout(text: str) -> int:
    """Extract `pgpgout` KiB counter from `/proc/vmstat`."""
    m = re.search(r"^pgpgout (\d+)$", text, re.M)
    if not m:
        raise ValueError("no 'pgpgout' line")
    return int(m.group(1))


def some_avg10(text: str) -> float:
    """Extract `some avg10` percentage from a PSI file."""
    val = pressure_field(text, "some", "avg10")
    if val is None:
        raise ValueError("no 'some avg10=' line")
    return float(val)


def positive(text: str, what: str) -> float:
    """Parse a finite positive float or raise `ValueError` naming `what`."""
    try:
        val = float(text)
        if val > 0 and math.isfinite(val):
            return val
    except ValueError:
        pass
    raise ValueError(f"{what}: a number above 0 (got {text!r})")


def duration(text: str) -> float:
    """Parse a duration string such as `120`, `90s`, `30m`, or `2h` into seconds."""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", text.strip())
    if not m or float(m.group(1)) <= 0:
        raise ValueError(f"not a duration: {text!r} (examples: 120, 90s, 30m, 2h)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def parse_quiet_args(argv: Sequence[str], environ: Mapping[str, str]) -> QuietQuery:
    """Validate `cpu-watch quiet` flags and `QUIET_*` environment overrides."""
    limits = {
        name: positive(environ[env_key], env_key) if environ.get(env_key, "") != "" else default
        for name, _, env_key, default in QUIET_LIMIT_SPECS
    }
    seconds = 60
    wait: float | None = None
    as_json = False
    flags = {flag: name for name, flag, _, _ in QUIET_LIMIT_SPECS}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--json":
            as_json = True
            i += 1
            continue
        if arg not in flags and arg not in ("--for", "--wait"):
            raise ValueError(f"unknown argument {arg}")
        if i + 1 >= len(argv):
            raise ValueError(f"{arg} needs a value")
        val = argv[i + 1]
        if arg == "--for":
            if not re.fullmatch(r"\d+", val) or int(val) < 1:
                raise ValueError(f"--for: a whole number of seconds, at least 1 (got {val!r})")
            seconds = int(val)
        elif arg == "--wait":
            wait = duration(val)
        else:
            limits[flags[arg]] = positive(val, arg)
        i += 2
    if wait is not None and wait < seconds:
        raise ValueError(f"--wait ({wait:g} s) is shorter than the window (--for {seconds})")
    return QuietQuery(seconds=seconds, wait=wait, as_json=as_json, limits=limits)

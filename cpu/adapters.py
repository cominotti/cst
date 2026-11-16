"""The cpu module's reach into the host: cpu-setup.conf and virsh (vm-pin);
sysfs, procfs, cgroups and the recorded runs (cpu-watch)."""

from collections.abc import Callable, Sequence
import csv
import glob
import json
import os
import re
import subprocess
import sys
import time
from typing import Any

from cpu.domain import CPU_DISABLE_DEFAULT, GAME_STATE, QUIET_LIMITS, URI, lows, pressure_field
from cst_core.adapters.files import read_text
from cst_core.domain.text import parse_assignments

CONF = "/etc/cpu-setup/cpu-setup.conf"
RUNS = os.path.expanduser("~/.local/state/cpu-setup/runs")
CG = "/sys/fs/cgroup"
PROC = "/proc"


def conf() -> dict[str, str]:
    """cpu-setup.conf's settings over vm-pin's defaults."""
    vals = {"VM_NAME": "win11-devbox", "VM_VCPUS": "12", "BUILD_CPUS": "12-23", "VM_CPU_DISABLE": CPU_DISABLE_DEFAULT}
    vals.update(parse_assignments(read_text(CONF, "")))
    return vals


def virsh(*args: str, check: bool = True) -> str:
    r = subprocess.run(["virsh", "-c", URI, *args], check=False, capture_output=True, text=True)
    if check and r.returncode != 0:
        sys.exit(f"virsh {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def cpus_of(kind: str) -> list[int]:
    """CPU numbers of core type kind ('cpu_core' or 'cpu_atom')."""
    out: list[int] = []
    for part in read_text(f"/sys/devices/{kind}/cpus", "").strip().split(","):
        if "-" in part:
            a, b = part.split("-")
            out += range(int(a), int(b) + 1)
        elif part:
            out.append(int(part))
    return out


def mean_mhz(cpus: Sequence[int]) -> int:
    vals = [int(read_text(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq", "0") or 0) for c in cpus]
    vals = [v for v in vals if v]
    return round(sum(vals) / len(vals) / 1000) if vals else 0


def pressure_total(path: str, kind: str) -> int | None:
    """Cumulative stall microseconds ('some' or 'full') of a pressure file."""
    v = pressure_field(read_text(path, ""), kind, "total")
    return int(v) if v is not None else None


def usage_usec(cg: str) -> int:
    m = re.search(r"^usage_usec (\d+)", read_text(os.path.join(CG, cg, "cpu.stat"), ""), re.M)
    return int(m.group(1)) if m else 0


def find_game(name: str | None) -> int | None:
    """PID of the game: NAME if given, else a process with GameMode's client
    library loaded."""
    if name:
        r = subprocess.run(["pgrep", "-xo", name], check=False, capture_output=True, text=True)
        return int(r.stdout.split()[0]) if r.stdout.split() else None
    # Several processes of the launch chain (reaper, pressure-vessel) map it:
    # the game is the one with the most threads.
    best: int | None = None
    nthreads = 0
    for maps in glob.glob("/proc/[0-9]*/maps"):
        if "libgamemode" in read_text(maps, ""):
            pid = int(maps.split("/")[2])
            n = len(glob.glob(f"/proc/{pid}/task/*"))
            if n > nthreads:
                best, nthreads = pid, n
    return best


def game_cgroup(pid: int) -> str:
    line = read_text(f"/proc/{pid}/cgroup", "").strip()
    return line.split("::", 1)[1].lstrip("/") if "::" in line else ""


def invol(pid: int) -> int:
    total = 0
    for st in glob.glob(f"/proc/{pid}/task/*/status"):
        m = re.search(r"^nonvoluntary_ctxt_switches:\s+(\d+)", read_text(st, ""), re.M)
        total += int(m.group(1)) if m else 0
    return total


def build_cgroups() -> list[str]:
    out = ["builds.slice"] if os.path.isdir(os.path.join(CG, "builds.slice")) else []
    out += [os.path.relpath(p, CG) for p in glob.glob(os.path.join(CG, "system.slice", "docker-*.scope"))]
    return out


def frametimes(path: str) -> list[float]:
    """Frame times (ms) from a MangoHud CSV (header row with 'frametime')."""
    out: list[float] = []
    with open(path, newline="") as f:
        rd = csv.reader(f)
        col: int | None = None
        for row in rd:
            if col is None:
                low = [c.strip().lower() for c in row]
                if "frametime" in low:
                    col = low.index("frametime")
                continue
            try:
                out.append(float(row[col]))
            except ValueError, IndexError:
                pass
    return out


def summarize(run_dir: str) -> str:
    # The value is not used; the line stays so that a corrupt meta.json
    # still stops the report here, as before.
    json.loads(read_text(os.path.join(run_dir, "meta.json"), "{}") or "{}")
    with open(os.path.join(run_dir, "samples.csv"), newline="") as f:
        rows = list(csv.DictReader(f))

    def col(name: str) -> list[float]:
        out_col: list[float] = []
        for r in rows:
            val = r.get(name)
            if val is not None and val != "":
                out_col.append(float(val))
        return out_col

    def avg(v: Sequence[float]) -> float:
        return sum(v) / len(v) if v else float("nan")

    wait, mem, inv = col("game_cpu_wait_ms"), col("game_mem_stall_ms"), col("game_invol_switches")
    game = (
        (
            f"game wait {avg(wait) / 10:5.1f}% (max {max(wait) / 10:5.1f}%)  mem stall {sum(mem):7.0f} ms"
            f"  invol/s {avg(inv):7.0f}"
        )
        if wait
        else f"{'no game found (--game NAME?)':62}"
    )
    out = (
        f"{os.path.basename(run_dir):40} {len(rows):5d}s  {game}"
        f"  P {avg(col('p_mhz')):5.0f} MHz  E {avg(col('e_mhz')):5.0f} MHz"
        f"  VM {avg(col('vm_cpus_used')):4.1f} cpu  builds {avg(col('builds_cpus_used')):4.1f} cpu"
        f"  profile {'on' if avg(col('profile_on')) > 0.5 else 'off'}"
    )
    mh = os.path.join(run_dir, "mangohud.csv")
    if os.path.exists(mh):
        r = lows(frametimes(mh))
        if r:
            out += f"\n{'':40} frames: avg {r[0]:.0f} fps, 1% low {r[1]:.0f}, 0.1% low {r[2]:.0f}"
    return out


def quiet_sample(
    proc: str = PROC, game_state: str = GAME_STATE, clock: Callable[[], float] = time.monotonic
) -> dict[str, Any]:
    """One reading of each test's file (s['v'][test name]) and whether the game
    profile is on. A file that cannot be read or parsed leaves its reason in
    s['err'][test name] instead."""
    s: dict[str, Any] = {"t": clock(), "wall": time.time(), "game": os.path.exists(game_state), "v": {}, "err": {}}
    for lim in QUIET_LIMITS:
        path = os.path.join(proc, lim.file)
        try:
            with open(path) as f:
                s["v"][lim.name] = lim.parse(f.read())
        except OSError as e:
            s["err"][lim.name] = f"{path}: {e.strerror or e}"
        except ValueError as e:
            s["err"][lim.name] = f"{path}: {e}"
    return s

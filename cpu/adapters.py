# SPDX-License-Identifier: MPL-2.0
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
from cst_core.adapters import SubprocessVirshAdapter
from cst_core.adapters.files import read_text
from cst_core.adapters.runtime import restore_default_signals
from cst_core.domain.text import describe_problems, parse_shell_assignments
from cst_core.ports import VirshPort

CONF = "/etc/cpu-setup/cpu-setup.conf"
RUNS = os.path.expanduser("~/.local/state/cpu-setup/runs")
CG = "/sys/fs/cgroup"
PROC = "/proc"
VIRSH_TIMEOUT = 60  # seconds for one virsh call
# virsh where vm-pin runs (also in a toolbox: not moved to the host); output
# that is not UTF-8 is replaced.
VIRSH: VirshPort = SubprocessVirshAdapter(URI, on_host=False)


def conf(*, refuse: bool = False) -> dict[str, str]:
    """cpu-setup.conf's settings over vm-pin's defaults, read as bash (game-profile) reads them.

    A line that is not a plain assignment sets nothing: with refuse (apply)
    the run stops and names it (a default BUILD_CPUS would re-pin the VM);
    otherwise (plan, status) it is printed on stderr.
    """
    vals = {"VM_NAME": "win11-devbox", "VM_VCPUS": "12", "BUILD_CPUS": "12-23", "VM_CPU_DISABLE": CPU_DISABLE_DEFAULT}
    parsed = parse_shell_assignments(read_text(CONF, ""))
    if parsed.problems:
        text = describe_problems(CONF, parsed.problems)
        if refuse:
            sys.exit(f"{text}\nFix these lines first; nothing changed.")
        print(text, file=sys.stderr)
    vals.update(parsed.values)
    return vals


def vm_pin_settings(action: str) -> dict[str, str]:
    """conf() for a vm-pin action. apply refuses problem lines and keeps
    Python's SIGPIPE handling (its temporary XML file is removed in a
    finally); status and plan print them and, since they only read, get
    SIGPIPE back at its default: `cst cpu vm-pin plan | head` ends quietly."""
    settings = conf(refuse=action == "apply")
    if action != "apply":
        restore_default_signals()
    return settings


def recorded_runs(names: Sequence[str]) -> list[str]:
    """The runs named (relative to RUNS, or absolute), else the last ten recorded."""
    named = [os.path.join(RUNS, r) if not os.path.isabs(r) else r for r in names]
    return named or sorted(glob.glob(os.path.join(RUNS, "*")))[-10:]


def virsh(*args: str, check: bool = True) -> str:
    """virsh's output, in the C locale (virsh translates its answers: "shut off" is "desligado" in pt_BR).

    A failure, or no answer within VIRSH_TIMEOUT seconds, stops the run with
    one line when check is set; without check the run goes on with what
    virsh printed (nothing, on a timeout). A virsh that cannot be started
    raises (as subprocess.run did).
    """
    r = VIRSH.call(*args, timeout=VIRSH_TIMEOUT)
    if r.fault == "timeout":
        if check:
            sys.exit(f"virsh {' '.join(args)}: no answer within {VIRSH_TIMEOUT} s")
        return ""
    if r.cause is not None:
        raise r.cause
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

# SPDX-License-Identifier: MPL-2.0
"""The cpu module's reach into the host: cpu-setup.conf, virsh and the saved
definition (vm-pin); sysfs, procfs, cgroups, the wrapped command and the
recorded runs (cpu-watch)."""

from collections.abc import Callable, Sequence
from dataclasses import replace
import csv
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Literal, TextIO

from cpu.domain import (
    CPU_DISABLE_DEFAULT,
    GAME_STATE,
    QUIET_LIMITS,
    SAMPLE_FIELDS,
    URI,
    CpuPaths,
    QuietSample,
    Reading,
    RunMeta,
    WatchCounters,
    lows,
    pressure_field,
    run_folder,
    watch_row,
)
from cst_core.adapters.virsh import SubprocessVirshAdapter
from cst_core.adapters.files import read_text
from cst_core.adapters.runtime import restore_default_signals
from cst_core.domain.errors import Refused
from cst_core.domain.text import describe_problems, parse_shell_assignments
from cst_core.domain.values import CpuSet
from cst_core.ports import VirshPort

CG = "/sys/fs/cgroup"
PROC = "/proc"
VIRSH_TIMEOUT = 60  # seconds for one virsh call
# virsh where vm-pin runs (also in a toolbox: not moved to the host); output
# that is not UTF-8 is replaced.
VIRSH: VirshPort = SubprocessVirshAdapter(URI, on_host=False)


def cpu_paths() -> CpuPaths:
    """The paths of the user running the program (cpu-watch's runs are in its home)."""
    return CpuPaths(runs=os.path.expanduser("~/.local/state/cpu-setup/runs"))


def conf(path: str, *, refuse: bool = False) -> dict[str, str]:
    """cpu-setup.conf's settings over vm-pin's defaults, read as bash (game-profile) reads them.

    A line that is not a plain assignment sets nothing: with refuse (apply)
    the run stops and names it (a default BUILD_CPUS would re-pin the VM);
    otherwise (plan, status) it is printed on stderr.
    """
    vals = {"VM_NAME": "win11-devbox", "VM_VCPUS": "12", "BUILD_CPUS": "12-23", "VM_CPU_DISABLE": CPU_DISABLE_DEFAULT}
    parsed = parse_shell_assignments(read_text(path, ""))
    if parsed.problems:
        text = describe_problems(path, parsed.problems)
        if refuse:
            raise Refused(f"{text}\nFix these lines first; nothing changed.")
        print(text, file=sys.stderr)
    vals.update(parsed.values)
    return vals


def vm_pin_settings(path: str, action: str) -> dict[str, str]:
    """conf() for a vm-pin action. apply refuses problem lines and keeps
    Python's SIGPIPE handling (its temporary XML file is removed in a
    finally); status and plan print them and, since they only read, get
    SIGPIPE back at its default: `cst cpu vm-pin plan | head` ends quietly."""
    settings = conf(path, refuse=action == "apply")
    if action != "apply":
        restore_default_signals()
    return settings


def recorded_runs(runs: str, names: Sequence[str]) -> list[str]:
    """The runs named (relative to RUNS, or absolute), else the last ten recorded."""
    named = [os.path.join(runs, r) if not os.path.isabs(r) else r for r in names]
    return named or sorted(glob.glob(os.path.join(runs, "*")))[-10:]


def virsh(*args: str, check: bool = True) -> str:
    """virsh's output, in the C locale (virsh translates its answers: "shut off" is "desligado" in pt_BR).

    A failure, or no answer within VIRSH_TIMEOUT seconds, is Refused with
    one line when check is set (the command line stops with it); without
    check the run goes on with what virsh printed (nothing, on a timeout). A
    virsh that cannot be started raises (as subprocess.run did).
    """
    r = VIRSH.call(*args, timeout=VIRSH_TIMEOUT)
    if r.fault == "timeout":
        if check:
            raise Refused(f"virsh {' '.join(args)}: no answer within {VIRSH_TIMEOUT} s")
        return ""
    if r.cause is not None:
        raise r.cause
    if check and r.returncode != 0:
        raise Refused(f"virsh {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def save_definition_once(etc: str, name: str, xml_text: str) -> str:
    """Keep XML_TEXT, the definition vm-pin replaces, as ETC/NAME.xml.before-vm-pin,
    unless an earlier apply already did (the first one is the one to go back to).
    Returns its path."""
    backup = f"{etc}/{name}.xml.before-vm-pin"
    os.makedirs(etc, exist_ok=True)
    if not os.path.exists(backup):
        # Holds --security-info secrets (graphics passwords): 0600 from the start.
        with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(xml_text)
    return backup


def define_domain(xml_text: str) -> None:
    """`virsh define` a domain from its XML text (a temporary file, removed after)."""
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
        f.write(xml_text)
    try:
        virsh("define", f.name)
    finally:
        os.unlink(f.name)


def cpus_of(kind: Literal["cpu_core", "cpu_atom"]) -> list[int]:
    """CPU numbers of core type kind ('cpu_core' or 'cpu_atom')."""
    text = read_text(f"/sys/devices/{kind}/cpus", "").strip()
    return list(CpuSet.parse(text).sorted_cpus()) if text else []  # no such core type: none


def mean_mhz(cpus: Sequence[int]) -> int:
    vals = [int(read_text(f"/sys/devices/system/cpu/cpu{c}/cpufreq/scaling_cur_freq", "0") or 0) for c in cpus]
    vals = [v for v in vals if v]
    return round(sum(vals) / len(vals) / 1000) if vals else 0


def pressure_total(path: str, kind: Literal["some", "full"]) -> int | None:
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


def watch_counters(pid: int | None, cg: str) -> WatchCounters:
    """The counters now; the game's too when its PID and cgroup are known."""
    now = time.time()
    sys_some = pressure_total("/proc/pressure/cpu", "some") or 0
    vm = usage_usec("machine.slice")
    builds = sum(usage_usec(c) for c in build_cgroups())
    game = None
    if pid and cg:
        game = (
            pressure_total(os.path.join(CG, cg, "cpu.pressure"), "some") or 0,
            pressure_total(os.path.join(CG, cg, "memory.pressure"), "full") or 0,
            invol(pid),
        )
    return WatchCounters(now, sys_some, vm, builds, game)


def run_over(proc: subprocess.Popen[bytes] | None, seconds: int | None, start: float) -> bool:
    """The wrapped command has exited, or SECONDS have passed since START."""
    return bool(proc and proc.poll() is not None) or bool(seconds and time.time() - start >= seconds)


def game_and_cgroup(game: str | None) -> tuple[int | None, str]:
    """The game's PID (find_game) and its cgroup, or (None, "")."""
    pid = find_game(game)
    return pid, game_cgroup(pid) if pid else ""


def sample_until_stopped(
    f: TextIO,
    *,
    start: float,
    game: str | None,
    seconds: int | None,
    proc: subprocess.Popen[bytes] | None,
    stop: Sequence[int],
    p_cpus: Sequence[int],
    e_cpus: Sequence[int],
) -> None:
    """Write a samples.csv row into F once per second until PROC exits,
    SECONDS pass or STOP is no longer empty (a signal); P_CPUS and E_CPUS
    are the CPUs of each core type, for their mean clock."""
    w = csv.writer(f)
    w.writerow(SAMPLE_FIELDS)
    prev: WatchCounters | None = None
    pid: int | None = None
    cg = ""
    while not stop and not run_over(proc, seconds, start):
        if not pid or not os.path.exists(f"/proc/{pid}"):
            pid, cg = game_and_cgroup(game)
            # A new game (or none): its counters start again.
            prev = None if prev is None else replace(prev, game=None)
        cur = watch_counters(pid, cg)
        if prev is not None:
            profile_on = os.path.exists(GAME_STATE)
            row = watch_row(
                cur, prev, start=start, profile_on=profile_on, pid=pid, p_mhz=mean_mhz(p_cpus), e_mhz=mean_mhz(e_cpus)
            )
            w.writerow(row)
            f.flush()
        prev = cur
        time.sleep(1)


def newest_mangohud_log(folder: str, start: float, end: float) -> str | None:
    """The newest MangoHud CSV in FOLDER written during the run (5 s before
    its start to 30 s after its end), or None."""
    logs = [p for p in glob.glob(os.path.join(folder, "*.csv")) if start - 5 <= os.path.getmtime(p) <= end + 30]
    return max(logs, key=os.path.getmtime) if logs else None


def record_run(
    runs: str, *, label: str, game: str | None, seconds: int | None, mangohud: str | None, cmd: Sequence[str]
) -> tuple[str, int | None]:
    """`cpu-watch watch`: a new run folder under RUNS, sampled once per second
    while CMD runs (or for SECONDS, or until SIGINT/SIGTERM), then the
    MangoHud log written meanwhile and meta.json. (the run folder, CMD's
    return code or None: no CMD, or still running when the run stopped)."""
    p_cpus, e_cpus = cpus_of("cpu_core"), cpus_of("cpu_atom")
    start = time.time()
    run_dir = os.path.join(runs, run_folder(start, label))
    os.makedirs(run_dir)
    proc = subprocess.Popen(cmd) if cmd else None
    stop: list[int] = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    print(f"recording into {run_dir} (Ctrl-C to stop)", file=sys.stderr)
    with open(os.path.join(run_dir, "samples.csv"), "w", newline="") as f:
        sample_until_stopped(
            f, start=start, game=game, seconds=seconds, proc=proc, stop=stop, p_cpus=p_cpus, e_cpus=e_cpus
        )
    end = time.time()
    if proc and proc.poll() is None:
        proc.terminate()
    rc = proc.returncode if proc else None
    meta: RunMeta = {"label": label, "start": start, "end": end, "cmd": list(cmd), "game": game, "rc": rc}
    if mangohud:
        newest = newest_mangohud_log(os.path.expanduser(mangohud), start, end)
        if newest:
            shutil.copy2(newest, os.path.join(run_dir, "mangohud.csv"))
            meta["mangohud"] = newest
    with open(os.path.join(run_dir, "meta.json"), "w") as f:
        json.dump(meta, f)
    return run_dir, rc


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
) -> QuietSample:
    """One reading of each test's file (s['v'][test name]) and whether the game
    profile is on. A file that cannot be read or parsed leaves its reason in
    s['err'][test name] instead."""
    t, wall, game = clock(), time.time(), os.path.exists(game_state)
    v: dict[str, Reading] = {}
    err: dict[str, str] = {}
    for lim in QUIET_LIMITS:
        path = os.path.join(proc, lim.file)
        try:
            with open(path) as f:
                v[lim.name] = lim.parse(f.read())
        except OSError as e:
            err[lim.name] = f"{path}: {e.strerror or e}"
        except ValueError as e:
            err[lim.name] = f"{path}: {e}"
    return {"t": t, "wall": wall, "game": game, "v": v, "err": err}

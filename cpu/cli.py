"""The cpu module's command lines: cpu-vm-pin and cpu-watch.

Driving adapters: argv in, the rules of cpu.domain applied to what
cpu.adapters reads from the host, the answer printed.
"""

from collections import deque
from collections.abc import Callable, Mapping, Sequence
import csv
from datetime import datetime
import difflib
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
from typing import Any, TextIO
import xml.etree.ElementTree as ET

from cst_core.adapters.runtime import restore_default_signals
from cst_core.domain.libvirt_xml import keep_prefixes
from cst_core.domain.text import is_yes
from cpu.adapters import (
    CG,
    RUNS,
    build_cgroups,
    cpus_of,
    find_game,
    game_cgroup,
    invol,
    mean_mhz,
    pressure_total,
    quiet_sample,
    recorded_runs,
    summarize,
    usage_usec,
    virsh,
    vm_pin_settings,
)
from cpu.domain import (
    GAME_STATE,
    SAMPLE_FIELDS,
    URI,
    cpu_line,
    features_note,
    pinned,
    quiet_args,
    quiet_tests,
    quiet_text,
)


def vm_pin(argv: Sequence[str], doc: str) -> int:
    """`cpu-vm-pin plan|apply|status [VM]` (argv without the program name); doc is its help."""
    action = argv[0] if len(argv) > 0 else "status"
    c = vm_pin_settings(action)
    name = argv[1] if len(argv) > 1 else c["VM_NAME"]
    vcpus, cpus = int(c["VM_VCPUS"]), c["BUILD_CPUS"]
    disable = c["VM_CPU_DISABLE"].split()
    if action == "status":
        state = virsh("domstate", name).strip()
        print(f"{name}: {state}")
        print(cpu_line(virsh("dumpxml", "--inactive", name, check=False), disable))
        print(virsh("vcpucount", name, check=False).rstrip())
        print(virsh("vcpupin", name, "--config", check=False).rstrip())
        print("emulator (persistent):", virsh("emulatorpin", name, "--config", check=False).split()[-1:])
        return 0
    if action not in ("plan", "apply"):
        print(doc)
        return 2
    cur = virsh("dumpxml", "--inactive", "--security-info", name)
    keep_prefixes(cur)
    croot = ET.fromstring(cur)
    # Elements tied to the vCPU count that this tool does not rewrite.
    for path in ("cputune/vcpusched", "cputune/cachetune", "cpu/numa", "vcpus"):
        if croot.find(path) is not None:
            sys.exit(f"{name} has <{path}>: adjust it by hand (virsh edit {name}); nothing changed")
    ET.indent(cur_root := ET.fromstring(cur), space="  ")
    before = ET.tostring(cur_root, encoding="unicode") + "\n"
    after = pinned(cur, vcpus, cpus, disable)
    diff = list(difflib.unified_diff(before.splitlines(True), after.splitlines(True), "current", "pinned"))
    if not diff:
        print(f"{name}: already {vcpus} vCPUs pinned to {cpus}; {features_note(croot, disable)}.")
        return 0
    sys.stdout.writelines(diff)
    if action == "plan":
        print("\nPlan only. Apply with: cst cpu vm-pin apply   (effective at the VM's next start)")
        return 0
    if not sys.stdin.isatty():
        sys.exit("apply asks for confirmation: run it in a terminal")
    if not is_yes(
        input(f"\nDefine {name} with {vcpus} vCPUs pinned to {cpus} ({features_note(croot, disable, True)})? [y/N] ")
    ):
        print("Nothing changed.")
        return 0
    backup = f"/etc/cpu-setup/{name}.xml.before-vm-pin"
    os.makedirs("/etc/cpu-setup", exist_ok=True)
    if not os.path.exists(backup):
        # Holds --security-info secrets (graphics passwords): 0600 from the start.
        with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(cur)
    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False) as f:
        f.write(after)
    try:
        virsh("define", f.name)
    finally:
        os.unlink(f.name)
    print(f"Defined. Effective at the next start of {name} (shut it down and start it when convenient).")
    print(f"Previous definition saved in {backup} (restore: virsh -c {URI} define {backup}).")
    return 0


# ---- cpu-watch ---------------------------------------------------------------
def watch(argv: Sequence[str]) -> int:
    label = "run"
    game: str | None = None
    seconds: int | None = None
    mangohud: str | None = None
    cmd: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            cmd = list(argv[i + 1 :])
            break
        if a in ("-l", "--label"):
            label = argv[i + 1]
            i += 2
        elif a == "--game":
            game = argv[i + 1]
            i += 2
        elif a == "--seconds":
            seconds = int(argv[i + 1])
            i += 2
        elif a == "--mangohud":
            mangohud = os.path.expanduser(argv[i + 1])
            i += 2
        else:
            print(f"unknown argument {a}", file=sys.stderr)
            return 2
    label = re.sub(r"[^A-Za-z0-9._-]", "_", label)
    p_cpus, e_cpus = cpus_of("cpu_core"), cpus_of("cpu_atom")
    start = time.time()
    run_dir = os.path.join(RUNS, datetime.fromtimestamp(start).strftime("%Y%m%dT%H%M%S") + "-" + label)
    os.makedirs(run_dir)
    proc = subprocess.Popen(cmd) if cmd else None
    stop: list[int] = []
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    print(f"recording into {run_dir} (Ctrl-C to stop)", file=sys.stderr)

    prev: dict[str, Any] = {}
    rows: list[list[object]] = []
    pid: int | None = None
    cg = ""
    with open(os.path.join(run_dir, "samples.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SAMPLE_FIELDS)
        while not stop:
            if proc and proc.poll() is not None:
                break
            if seconds and time.time() - start >= seconds:
                break
            if not pid or not os.path.exists(f"/proc/{pid}"):
                pid = find_game(game)
                cg = game_cgroup(pid) if pid else ""
                prev.pop("game", None)
            now = time.time()
            cur: dict[str, Any] = {
                "t": now,
                "sys": pressure_total("/proc/pressure/cpu", "some") or 0,
                "vm": usage_usec("machine.slice"),
                "builds": sum(usage_usec(c) for c in build_cgroups()),
            }
            if pid and cg:
                cur["game"] = (
                    pressure_total(os.path.join(CG, cg, "cpu.pressure"), "some") or 0,
                    pressure_total(os.path.join(CG, cg, "memory.pressure"), "full") or 0,
                    invol(pid),
                )
            if prev:
                dt = now - prev["t"]
                g_cur: tuple[int, int, int] | None = cur.get("game")
                g_prev: tuple[int, int, int] | None = prev.get("game")
                has_game = g_cur is not None and g_prev is not None
                row: list[object] = [
                    round(now - start, 1),
                    int(os.path.exists(GAME_STATE)),
                    pid or "",
                    # per second of wall time (samples drift a little past 1 s)
                    round((g_cur[0] - g_prev[0]) / 1000 / dt, 1) if g_cur is not None and g_prev is not None else "",
                    round((g_cur[1] - g_prev[1]) / 1000 / dt, 1) if g_cur is not None and g_prev is not None else "",
                    round((g_cur[2] - g_prev[2]) / dt) if has_game and g_cur is not None and g_prev is not None else "",
                    round((cur["sys"] - prev["sys"]) / 1000 / dt, 1),
                    mean_mhz(p_cpus),
                    mean_mhz(e_cpus),
                    round((cur["vm"] - prev["vm"]) / 1e6 / dt, 2),
                    round(max(0, cur["builds"] - prev["builds"]) / 1e6 / dt, 2),
                ]
                w.writerow(row)
                f.flush()
                rows.append(row)
            prev = cur
            time.sleep(1)
    end = time.time()
    if proc and proc.poll() is None:
        proc.terminate()
    meta: dict[str, object] = {
        "label": label,
        "start": start,
        "end": end,
        "cmd": cmd,
        "game": game,
        "rc": proc.returncode if proc else None,
    }
    if mangohud:
        logs = [p for p in glob.glob(os.path.join(mangohud, "*.csv")) if start - 5 <= os.path.getmtime(p) <= end + 30]
        if logs:
            newest = max(logs, key=os.path.getmtime)
            shutil.copy2(newest, os.path.join(run_dir, "mangohud.csv"))
            meta["mangohud"] = newest
    with open(os.path.join(run_dir, "meta.json"), "w") as f:
        json.dump(meta, f)
    print(summarize(run_dir))
    return proc.returncode if proc and proc.returncode else 0


def report(argv: Sequence[str]) -> int:
    runs = recorded_runs(argv)
    if not runs:
        print(f"no runs in {RUNS}; record one with: cst cpu watch -l <label> [-- <cmd>]")
        return 0
    print("game wait = share of time the game's threads were runnable but waiting for a CPU")
    for r in runs:
        print(summarize(r))
    return 0


def quiet(
    argv: Sequence[str],
    sample: Callable[[], Any] | None = None,
    sleep: Callable[[float], object] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    out: TextIO | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Exit 0 if a window of SECONDS was quiet, 1 if not (or --wait ran out),
    2 for a usage error. Reads only; writes nothing but its output."""
    out_stream: TextIO = sys.stdout if out is None else out
    try:
        seconds, wait, as_json, limits = quiet_args(argv, os.environ if environ is None else environ)
    except ValueError as e:
        print(f"quiet: {e}", file=sys.stderr)
        return 2
    sample_fn: Callable[[], Any] = (lambda: quiet_sample(clock=clock)) if sample is None else sample
    if wait is not None:
        print(f"waiting up to {wait:g} s for {seconds} quiet seconds in a row", file=sys.stderr)
    window: deque[Any] = deque(maxlen=seconds + 1)
    try:
        start = clock()
        window.append(sample_fn())
        n = 0
        while True:
            n += 1
            sleep(max(0.0, start + n - clock()))
            window.append(sample_fn())
            if len(window) <= seconds:
                continue
            tests = quiet_tests(window, limits)
            ok = all(t["ok"] for t in tests)
            # A file that cannot be read stays so: waiting longer cannot help.
            if ok or wait is None or clock() - start >= wait or any(t["error"] for t in tests):
                break
    except KeyboardInterrupt:
        print("quiet: interrupted", file=sys.stderr)
        return 130

    def stamp(s: Mapping[str, Any]) -> str:
        return datetime.fromtimestamp(s["wall"]).isoformat(sep=" ", timespec="seconds")

    result: dict[str, Any] = {
        "quiet": ok,
        "seconds": seconds,
        "start": stamp(window[0]),
        "end": stamp(window[-1]),
        "waited_s": None if wait is None else round(clock() - start, 1),
        "tests": tests,
        "failed": [t["name"] for t in tests if not t["ok"]],
    }
    print(json.dumps(result) if as_json else quiet_text(result), file=out_stream)
    return 0 if ok else 1


def cpu_watch(argv: Sequence[str], doc: str) -> int:
    """`cpu-watch watch|report|quiet ...` (argv without the program name); doc is its help."""
    cmd = argv[0] if argv else ""
    if cmd not in ("watch", "quiet"):
        restore_default_signals()  # report and the help only read: `cpu-watch report | head` ends quietly
    if cmd not in ("watch", "report", "quiet"):
        print(doc)
        return 2
    if cmd == "watch":
        return watch(argv[1:])
    if cmd == "report":
        return report(argv[1:])
    return quiet(argv[1:])

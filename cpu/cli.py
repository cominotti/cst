# SPDX-License-Identifier: MPL-2.0
"""The cpu module's command lines: cpu-vm-pin and cpu-watch.

Driving adapters: argv in, the rules of cpu.domain applied to what
cpu.adapters reads from the host, the answer printed. Light profile: each
command's main is the composition root (docs/architecture.md, "Package
profile").
"""

from collections import deque
from collections.abc import Callable, Mapping, Sequence
import json
import os
import re
import sys
from typing import TextIO

from cst_core.adapters import SystemClockAdapter
from cst_core.adapters.runtime import restore_default_signals
from cst_core.adapters.terminal import confirm
from cst_core.domain.errors import Refused
from cpu.adapters import (
    cpu_paths,
    define_domain,
    quiet_sample,
    record_run,
    recorded_runs,
    save_definition_once,
    summarize,
    virsh,
    vm_pin_settings,
)
from cpu.domain import (
    URI,
    CpuPaths,
    QuietSample,
    QuietTest,
    cpu_line,
    pin_plan,
    quiet_args,
    quiet_result,
    quiet_tests,
    quiet_text,
)

# The clock and the pause of `cpu-watch quiet` (its tests pass their own).
CLOCK = SystemClockAdapter()


def vm_pin(argv: Sequence[str], doc: str, paths: CpuPaths | None = None) -> int:
    """`cpu-vm-pin plan|apply|status [VM]` (argv without the program name); doc is its help.

    What the run refuses (a settings problem on apply, a failed virsh call, a
    definition it cannot rewrite) ends it with that one line, exit 1.
    """
    try:
        return pin_vm(argv, doc, cpu_paths() if paths is None else paths)
    except Refused as e:
        sys.exit(str(e))


def pin_vm(argv: Sequence[str], doc: str, paths: CpuPaths) -> int:
    """vm_pin's work (Refused ends it)."""
    action = argv[0] if len(argv) > 0 else "status"
    c = vm_pin_settings(paths.conf, action)
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
    plan = pin_plan(name, cur, vcpus, cpus, disable)
    if not plan.diff:
        print(f"{name}: already {vcpus} vCPUs pinned to {cpus}; {plan.features}.")
        return 0
    sys.stdout.writelines(plan.diff)
    if action == "plan":
        print("\nPlan only. Apply with: cst cpu vm-pin apply   (effective at the VM's next start)")
        return 0
    if not confirm(f"\nDefine {name} with {vcpus} vCPUs pinned to {cpus} ({plan.planned_features})?"):
        print("Nothing changed.")
        return 0
    backup = save_definition_once(paths.etc, name, cur)
    define_domain(plan.after)
    print(f"Defined. Effective at the next start of {name} (shut it down and start it when convenient).")
    print(f"Previous definition saved in {backup} (restore: virsh -c {URI} define {backup}).")
    return 0


# ---- cpu-watch ---------------------------------------------------------------
def watch(argv: Sequence[str]) -> int:
    """`cpu-watch watch ...`: record a run, print its summary; the exit status is CMD's (0 without one)."""
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
            mangohud = argv[i + 1]
            i += 2
        else:
            print(f"unknown argument {a}", file=sys.stderr)
            return 2
    label = re.sub(r"[^A-Za-z0-9._-]", "_", label)
    run_dir, rc = record_run(cpu_paths().runs, label=label, game=game, seconds=seconds, mangohud=mangohud, cmd=cmd)
    print(summarize(run_dir))
    return rc if rc else 0


def report(argv: Sequence[str]) -> None:
    """`cpu-watch report [RUN...]`: one line per run."""
    runs_dir = cpu_paths().runs
    runs = recorded_runs(runs_dir, argv)
    if not runs:
        print(f"no runs in {runs_dir}; record one with: cst cpu watch -l <label> [-- <cmd>]")
        return
    print("game wait = share of time the game's threads were runnable but waiting for a CPU")
    for r in runs:
        print(summarize(r))


def quiet(
    argv: Sequence[str],
    sample: Callable[[], QuietSample] | None = None,
    sleep: Callable[[float], object] = CLOCK.sleep,
    clock: Callable[[], float] = CLOCK.monotonic,
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
    sample_fn: Callable[[], QuietSample] = (lambda: quiet_sample(clock=clock)) if sample is None else sample
    if wait is not None:
        print(f"waiting up to {wait:g} s for {seconds} quiet seconds in a row", file=sys.stderr)
    window: deque[QuietSample] = deque(maxlen=seconds + 1)
    tests: list[QuietTest] = []
    ok = False
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
    waited_s = None if wait is None else round(clock() - start, 1)
    result = quiet_result(window, tests, ok=ok, seconds=seconds, waited_s=waited_s)
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
        report(argv[1:])
        return 0
    return quiet(argv[1:])

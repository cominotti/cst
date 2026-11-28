---
name: generate-report
description: "Generate a read-only CST workstation health and performance report for a time window (default: last 3 hours) covering btrfs, zswap/memory, CPU/cgroups, and CST config health, with plain-language per-category summaries, explicit 'worth improving vs. nothing worth changing' verdicts, and a mandatory independent review step before presenting the report. Use when the user invokes /generate-report or asks to check/report btrfs, zswap, memory, CPU, or CST configuration health."
---
<!-- SPDX-License-Identifier: MPL-2.0 -->

# Generating a CST Workstation Health and Performance Report

Read-only runbook for inspecting `btrfs`, `zswap`/memory, `cpu`/cgroups, and active CST settings over a time window (default: **last 3 hours**), producing a plain-language summary plus detailed evidence, and verifying every claim via an independent review before answering the user.

See [AGENTS.md](../../../AGENTS.md), [perf/README.md](../../../perf/README.md), [btrfs/README.md](../../../btrfs/README.md), [swap/README.md](../../../swap/README.md), and [cpu/README.md](../../../cpu/README.md) for module details and metric semantics.

## Non-Negotiable Rules

- **Strictly read-only.** Never run `sudo`, never modify host files, sysctls, or systemd units, and never walk `/` (`find /`, `fd . /`).
- **Separate window metrics from since-boot counters.** `cst perf`, `cst doctor`, `/sys/fs/btrfs/*/commit_stats`, `/proc/vmstat`, `/proc/interrupts`, and `cst swap status` traffic counters are **cumulative since boot**. Never present a since-boot peak, average, or count (such as `max_commit_ms`, `allocstall/h`, `rb%`, or pre-ban interrupts) as if it occurred inside the requested window. Always compute window-scoped deltas from `/var/log/btrfs-stall-monitor/stall-*.jsonl*` and `below dump`.
- **Match the user's language.** Write the final report in the language the user asked in (for example Portuguese or English). Default to the **last 3 hours** unless the user specifies another window (such as `1h`, `6h`, `24h`, or "since <change>").
- **Evidence-only recommendations.** Only suggest a tuning change or fix when backed by measured numbers and verified root cause (stack traces, cgroup metrics, journal logs, or source code). When a category is healthy, explicitly write **"Nothing worth changing"** (or the user-language equivalent) — never invent placebo tweaks.
- **Graceful degradation.** If running inside a toolbox container (`test -f /run/.containerenv`), or if `below` or `/var/log/btrfs-stall-monitor` is missing/unreadable, report what was unavailable instead of guessing.

---

## Step 1: Collect Read-Only Telemetry (Batch in Parallel)

Run these read-only checks in parallel from the CST checkout root (adjust `--since` / `--begin` / `WINDOW_SECONDS` if the user requested a window other than 3 hours).

### 1.1 Environment, `cst perf`, `cst doctor`, and Module Statuses

```bash
uname -a; uptime; test -f /run/.containerenv && echo "IN_CONTAINER" || echo "ON_HOST"
./cst perf; echo "=== EXIT: $? ==="
./cst doctor; echo "=== EXIT: $? ==="
./cst swap status; echo "=== EXIT: $? ==="
```

```bash
for sub in "btrfs flushoncommit status" "btrfs commit-interval status" "btrfs iocost status" "btrfs compress-force status" "btrfs scrub-limit status" "btrfs cache status" "cpu power status" "cpu irq status"; do
  echo "== $sub =="
  ./cst $sub
done
```

### 1.2 Live Kernel, Mounts, `commit_stats`, CPU, and Cgroup State

```bash
cat /proc/cmdline; echo "---"
findmnt -t btrfs -o TARGET,SOURCE,OPTIONS; echo "---"
grep ^ /sys/fs/btrfs/*/commit_stats 2>/dev/null; echo "---"
cat /sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost 2>/dev/null; echo "---"
head -n 1 /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null; echo "---"
grep -E "allocstall|compact_stall|pgscan_|pgsteal_|zswp|pswp" /proc/vmstat
```

Read live weights, cpusets, and PSI across the cgroup hierarchy (discovering `user-*.slice/user@*.service` dynamically without hardcoding UID `1000`):

```bash
python3 - <<'EOF'
import glob, os

paths = [
    "/sys/fs/cgroup",
    "/sys/fs/cgroup/system.slice",
    "/sys/fs/cgroup/machine.slice",
    "/sys/fs/cgroup/builds.slice",
    "/sys/fs/cgroup/user.slice",
]
for u in sorted(glob.glob("/sys/fs/cgroup/user.slice/user-*.slice/user@*.service")):
    paths.extend([os.path.dirname(u), u] + [os.path.join(u, s) for s in ("session.slice", "app.slice", "user.slice", "background.slice")])

for path in paths:
    if not os.path.isdir(path):
        continue
    name = path.removeprefix("/sys/fs/cgroup") or "/"
    print(f"=== {name} ===")
    for f in ["cpu.weight", "io.weight", "cpuset.cpus.effective", "cpu.pressure", "io.pressure", "memory.pressure", "cpu.stat"]:
        fp = os.path.join(path, f)
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as fh:
                    print(f"  {f}: {fh.read().strip().replace(chr(10), ' | ')}")
            except OSError as exc:
                print(f"  {f}: ERR:{exc}")
EOF
```

### 1.3 Window-Scoped Btrfs & Stall Monitor Telemetry

Run both `cst btrfs report` views (overview with `below`, and verbose stacks without `below`):

```bash
./cst btrfs report --since "3 hours ago"
./cst btrfs report --since "3 hours ago" -v --no-below
```

Inspect recent CST changes and kernel btrfs/OOM logs:

```bash
journalctl -b _COMM=sudo -g "flushoncommit|commit-interval|compress-force|iocost|swap|cpu|btrfs" --no-pager 2>/dev/null | tail -n 40
journalctl -k -b -g "BTRFS|remount|flushoncommit|commit=|oom" --no-pager 2>/dev/null | tail -n 40
```

Analyze `/var/log/btrfs-stall-monitor/stall-*.jsonl*` (both `.jsonl` and rotated `.jsonl.gz`). Accumulate counters **pairwise** across consecutive samples (skipping `dt > 30s` gaps and counter resets across reboots), resolve `"vm"` keys from each `meta` record's `"vm_keys"` (falling back to `VMSTAT_KEYS` in `btrfs/stall_monitord/domain.py`), and inspect `c[5]` (`longest since last sample`) alongside `c[3]` (`last_commit_ms`). If a config change occurred inside the retained logs, add extra `summarize(...)` calls for the before/after windows.

```bash
python3 - <<'EOF'
import datetime, glob, gzip, json, os, statistics, sys

sys.path.insert(0, os.getcwd())
from btrfs.stall_monitord.domain import VMSTAT_KEYS

WINDOW_SECONDS = 10800  # 3h default; adjust to match user request
BUCKETS = 6
MAX_PAIR_DT = 30.0

paths = sorted(
    glob.glob("/var/log/btrfs-stall-monitor/stall-*.jsonl")
    + glob.glob("/var/log/btrfs-stall-monitor/stall-*.jsonl.gz")
)
plain = {p for p in paths if p.endswith(".jsonl")}
paths = [p for p in paths if not (p.endswith(".gz") and p[:-3] in plain)]
if not paths:
    print("NO_STALL_LOGS_AVAILABLE")
    sys.exit(0)

fs_labels = {}
samples = []
cur_vm_keys = list(VMSTAT_KEYS)
for p in paths:
    opener = gzip.open if p.endswith(".gz") else open
    try:
        with opener(p, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                t = d.get("t")
                if t == "meta":
                    for uid, info in d.get("fs", {}).items():
                        fs_labels[uid] = info.get("label") or uid[:8]
                    if isinstance(d.get("vm_keys"), list):
                        cur_vm_keys = d["vm_keys"]
                elif t == "s":
                    vm_list = d.get("vm")
                    vm_map = {k: vm_list[i] for i, k in enumerate(cur_vm_keys) if isinstance(vm_list, list) and i < len(vm_list)}
                    d["_vm"] = vm_map
                    samples.append(d)
    except OSError as exc:
        print(f"SKIP {p}: {exc}")

samples.sort(key=lambda x: x["ts"])
if len(samples) < 2:
    print("INSUFFICIENT_STALL_SAMPLES")
    sys.exit(0)

fmt = lambda ts: datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
print(f"Loaded {len(samples)} samples: {fmt(samples[0]['ts'])} -> {fmt(samples[-1]['ts'])}")
print("Filesystems:", fs_labels)

# List >=500ms commit events in the last 48h for root/cache correlation
cutoff_48h = samples[-1]["ts"] - 48 * 3600
prev_c = {}
for s in samples:
    for uid, fs in s.get("fs", {}).items():
        c = fs.get("c")
        if not c:
            continue
        pc = prev_c.get(uid)
        if pc is not None and c[0] > pc[0] and s["ts"] >= cutoff_48h:
            dc, dt_ms = c[0] - pc[0], max(0, c[1] - pc[1])
            peak = max(c[3], c[5] if len(c) > 5 else 0, dt_ms / dc)
            if peak >= 500:
                dirty = (s["_vm"].get("nr_dirty") or 0) * 4 / 1024
                print(f"  [commit>=500ms] {fs_labels.get(uid, uid[:8]):6s} {fmt(s['ts'])} dc={dc} dt_ms={dt_ms} peak={peak:.0f}ms d={s.get('d')} d_mem={s.get('d_mem')} dirty={dirty:.0f}MiB")
        prev_c[uid] = c

def summarize(label, t0, t1):
    ws = [s for s in samples if t0 <= s["ts"] <= t1]
    if len(ws) < 2:
        print(f"\n[{label}] insufficient samples ({len(ws)})")
        return
    valid_dt = sum(
        ws[i]["ts"] - ws[i - 1]["ts"]
        for i in range(1, len(ws))
        if 0 < ws[i]["ts"] - ws[i - 1]["ts"] <= MAX_PAIR_DT
    )
    dur_h = valid_dt / 3600.0
    if dur_h <= 0:
        print(f"\n[{label}] zero continuous duration")
        return
    print(f"\n[{label}] ({dur_h:.2f}h)")
    for uid, name in sorted(fs_labels.items(), key=lambda kv: kv[1]):
        nc = tot_ms = gt500 = gt1s = mx = 0
        vals = []
        for i in range(1, len(ws)):
            dt_s = ws[i]["ts"] - ws[i - 1]["ts"]
            if not (0 < dt_s <= MAX_PAIR_DT):
                continue
            pc = ws[i - 1].get("fs", {}).get(uid, {}).get("c")
            cc = ws[i].get("fs", {}).get(uid, {}).get("c")
            if not pc or not cc or cc[0] <= pc[0] or cc[1] < pc[1]:
                continue
            dc, dms = cc[0] - pc[0], cc[1] - pc[1]
            nc += dc
            tot_ms += dms
            avg_i = dms / dc
            vals.extend([avg_i] * dc)
            peak_i = max(cc[3], cc[5] if len(cc) > 5 else 0, avg_i)
            mx = max(mx, peak_i)
            if peak_i >= 500: gt500 += dc
            if peak_i >= 1000: gt1s += dc
        if nc > 0:
            vals.sort()
            p50 = vals[int(0.50 * len(vals))]
            p95 = vals[int(0.95 * len(vals))]
            p99 = vals[int(0.99 * len(vals))]
            print(f"  btrfs {name:6s}: n={nc} ({nc/dur_h:.0f}/h) avg={tot_ms/nc:.1f}ms p50={p50:.0f} p95={p95:.0f} p99={p99:.0f} max~={mx:.0f}ms >=500:{gt500} >=1s:{gt1s}")
    vm_keys = ("zswpout", "zswpin", "zswpwb", "pswpout", "pswpin", "allocstall_normal", "allocstall_movable", "pgscan_direct", "pgsteal_direct", "pgscan_kswapd")
    vm_tot = {k: 0 for k in vm_keys}
    psi_tot = {"io": [0, 0], "memory": [0, 0], "cpu": [0, 0]}
    psi_dt_us = 0.0
    for i in range(1, len(ws)):
        dt_s = ws[i]["ts"] - ws[i - 1]["ts"]
        if not (0 < dt_s <= MAX_PAIR_DT):
            continue
        psi_dt_us += dt_s * 1e6
        v0, v1 = ws[i - 1]["_vm"], ws[i]["_vm"]
        for k in vm_keys:
            a, b = v0.get(k), v1.get(k)
            if a is not None and b is not None and b >= a:
                vm_tot[k] += b - a
        p0, p1 = ws[i - 1].get("psi") or {}, ws[i].get("psi") or {}
        for axis in ("io", "memory", "cpu"):
            pa, pb = p0.get(axis), p1.get(axis)
            if pa and pb and pb[1] >= pa[1] and pb[3] >= pa[3]:
                psi_tot[axis][0] += pb[1] - pa[1]
                psi_tot[axis][1] += pb[3] - pa[3]
    dirty = [s["_vm"]["nr_dirty"] * 4 / 1024 for s in ws if s["_vm"].get("nr_dirty") is not None]
    loads = [s.get("load1", 0.0) for s in ws]
    dirty_str = f"avg={statistics.mean(dirty):.0f} p95={sorted(dirty)[int(0.95*len(dirty))]:.0f} max={max(dirty):.0f}" if dirty else "n/a"
    print(f"  dirty MiB   : {dirty_str} | D_io_max={max(s.get('d',0) for s in ws)} D_mem_max={max(s.get('d_mem',0) for s in ws)}")
    if psi_dt_us > 0:
        print(f"  PSI         : io={psi_tot['io'][0]/psi_dt_us*100:.2f}%/{psi_tot['io'][1]/psi_dt_us*100:.2f}%  mem={psi_tot['memory'][0]/psi_dt_us*100:.2f}%/{psi_tot['memory'][1]/psi_dt_us*100:.2f}%  cpu={psi_tot['cpu'][0]/psi_dt_us*100:.2f}% | load1 avg={statistics.mean(loads):.2f} max={max(loads):.2f}")
    astall = vm_tot["allocstall_normal"] + vm_tot["allocstall_movable"]
    print(f"  zswap/swap  : zout={vm_tot['zswpout']*4/1024:.0f}MiB zin={vm_tot['zswpin']*4/1024:.0f}MiB zwb={vm_tot['zswpwb']*4/1024:.0f}MiB pout={vm_tot['pswpout']*4/1024:.0f}MiB pin={vm_tot['pswpin']*4/1024:.0f}MiB | allocstall={astall} ({astall/dur_h:.0f}/h) pgscan_d={vm_tot['pgscan_direct']} pgsteal_d={vm_tot['pgsteal_direct']} pgscan_k={vm_tot['pgscan_kswapd']}")

now = samples[-1]["ts"]
summarize("REQUESTED WINDOW", now - WINDOW_SECONDS, now)
step = WINDOW_SECONDS / BUCKETS
for i in range(BUCKETS):
    a, b = now - WINDOW_SECONDS + i * step, now - WINDOW_SECONDS + (i + 1) * step
    summarize(f"Bucket {fmt(a)[6:11]}->{fmt(b)[6:11]}", a, b)
EOF
```

### 1.4 Window-Scoped CPU, Cgroups (`below`), and IRQ Affinity Verification

Use `below dump ... -O json --raw -f ...` to fetch exact numeric fields without multi-megabyte unused fields, handle empty or multi-document JSON across boot boundaries, and match user slices dynamically across any UID:

```bash
python3 - <<'EOF'
import json, math, re, shutil, statistics, subprocess, sys

if not shutil.which("below"):
    print("BELOW_NOT_INSTALLED")
    sys.exit(0)

BEGIN = "3 hours ago"  # adjust to match user window

def parse_below_json(stdout):
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        rows = []
        for chunk in re.split(r"\n(?=[\[{])", text):
            try:
                v = json.loads(chunk)
                rows.extend(v if isinstance(v, list) else [v])
            except ValueError:
                continue
        return rows

def run_below(sub, fields):
    cmd = ["below", "dump", sub, "--begin", BEGIN, "--end", "now", "-O", "json", "--raw", "-f", *fields]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    if p.returncode != 0:
        print(f"BELOW_{sub.upper()}_FAILED: {p.stderr.strip()}")
        return []
    return parse_below_json(p.stdout)

def num(v):
    if v in (None, "", "?"):
        return None
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except ValueError, TypeError:
        return None

sys_rows = run_below("system", ["timestamp", "datetime", "cpu.usage_pct", "cpu.user_pct", "cpu.system_pct", "mem.free", "vm.oom_kill", "vm.pgscan_direct"])
if sys_rows:
    usages = sorted(x for r in sys_rows if (x := num(r.get("Usage"))) is not None)
    users = [x for r in sys_rows if (x := num(r.get("User"))) is not None]
    syss = [x for r in sys_rows if (x := num(r.get("System"))) is not None]
    frees = [x / 1e9 for r in sys_rows if (x := num(r.get("Free"))) is not None]
    ooms = [int(x) for r in sys_rows if (x := num(r.get("OOM Kills"))) is not None]
    oom_delta = sum(max(0, b - a) for a, b in zip(ooms, ooms[1:]))
    pgd = [x for r in sys_rows if (x := num(r.get("Pgscan Direct"))) is not None]
    print(f"=== BELOW SYSTEM ({len(sys_rows)} samples: {sys_rows[0].get('Datetime')} -> {sys_rows[-1].get('Datetime')}) ===")
    if usages:
        print(f"  CPU%   : avg={statistics.mean(usages):.1f}% p50={usages[len(usages)//2]:.1f}% p95={usages[int(0.95*len(usages))]:.1f}% max={max(usages):.1f}%")
    if users and syss:
        print(f"  Usr/Sys: user avg={statistics.mean(users):.1f}% (max {max(users):.1f}%), sys avg={statistics.mean(syss):.1f}% (max {max(syss):.1f}%)")
    if frees:
        print(f"  Free GB: min={min(frees):.2f} avg={statistics.mean(frees):.2f} max={max(frees):.2f} | OOM kills={oom_delta} | pgscan_d nonzero={sum(1 for x in pgd if x > 0)}/{len(pgd)}")

cg_rows = run_below("cgroup", ["name", "full_path", "cpu.usage_pct", "pressure.cpu_some_pct", "pressure.io_some_pct", "pressure.memory_some_pct", "mem.total"])
if cg_rows:
    stats = {}
    def walk(node):
        path = node.get("Full Path") or "/"
        st = stats.setdefault(path, {"cpu": [], "cpsi": [], "ipsi": [], "mpsi": [], "mem": []})
        for key, fld in [("cpu", "CPU Usage"), ("cpsi", "CPU Some Pressure"), ("ipsi", "I/O Some Pressure"), ("mpsi", "Mem Some Pressure")]:
            if (x := num(node.get(fld))) is not None:
                st[key].append(x)
        if (x := num(node.get("Mem Total"))) is not None:
            st["mem"].append(x / 1024**3)
        for ch in node.get("children", []):
            walk(ch)
    for r in cg_rows:
        walk(r)
    user_slice_re = re.compile(r"^/user\.slice/user-\d+\.slice/user@\d+\.service/(?:session|app|user|background)\.slice$")
    print(f"\n=== BELOW KEY CGROUPS ({len(cg_rows)} samples) ===")
    for p, st in sorted(stats.items()):
        if not st["cpu"]:
            continue
        is_key = p in ("/", "/system.slice", "/machine.slice", "/builds.slice", "/user.slice") or bool(user_slice_re.match(p))
        high_mem = max(st["mpsi"] or [0.0]) > 0.5
        heavy_cpu = statistics.mean(st["cpu"]) >= 50.0 and len(st["cpu"]) >= 10
        if is_key or high_mem or heavy_cpu:
            print(f"  {p[-68:]:68s} n={len(st['cpu']):3d} cpu={statistics.mean(st['cpu']):5.1f}%/{max(st['cpu']):6.1f}% cPSI={statistics.mean(st['cpsi'] or [0]):4.2f}%/{max(st['cpsi'] or [0]):5.2f}% ioPSI={statistics.mean(st['ipsi'] or [0]):4.2f}%/{max(st['ipsi'] or [0]):5.2f}% mPSI={statistics.mean(st['mpsi'] or [0]):4.2f}%/{max(st['mpsi'] or [0]):5.2f}% mem={statistics.mean(st['mem'] or [0]):4.1f}/{max(st['mem'] or [0]):4.1f}G")
EOF
```

Check live IRQ affinity against `IRQBALANCE_BANNED_CPULIST` / `BUILD_CPUS`. Note the interpretation rules from [perf/README.md](../../../perf/README.md):
- `/proc/interrupts` counters are **cumulative since boot** (`on_banned > 0` may simply come from boot-time/pre-ban interrupts); judge live placement by `effective_affinity_list`.
- Kernel-managed per-CPU `blk-mq` queues (`nvme*q1+`) are expected on all CPUs including `BUILD_CPUS`; only movable latency-sensitive IRQs (`xhci`, GPUs, NICs, audio, `kvm`, `nvme*q0`) should stay outside `BUILD_CPUS`.

```bash
python3 - <<'EOF'
import os, re

def parse_cpulist(text):
    out = set()
    for part in text.strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out

banned = set()
for cfg, key in [("/etc/cpu-setup/irqbalance.env", "IRQBALANCE_BANNED_CPULIST"), ("/etc/cpu-setup/cpu-setup.conf", "BUILD_CPUS")]:
    if os.path.isfile(cfg):
        with open(cfg, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(rf"^\s*{key}=['\"]?([0-9,-]+)", line)
                if m:
                    banned = parse_cpulist(m.group(1))
                    break
    if banned:
        break

with open("/proc/interrupts", encoding="utf-8") as fh:
    lines = fh.readlines()
cpu_ids = [int(c[3:]) for c in lines[0].split() if c.startswith("CPU") and c[3:].isdigit()]
rows = []
for line in lines[1:]:
    parts = line.split()
    if not parts or not parts[0].endswith(":") or not parts[0][:-1].isdigit():
        continue
    irq = parts[0][:-1]
    counts = [int(x) for x in parts[1:1 + len(cpu_ids)]]
    total = sum(counts)
    if total <= 10000:
        continue
    aff_p = f"/proc/irq/{irq}/effective_affinity_list"
    aff = "?"
    if os.path.exists(aff_p):
        with open(aff_p, encoding="utf-8") as afh:
            aff = afh.read().strip()
    on_ban = sum(cnt for cid, cnt in zip(cpu_ids, counts) if cid in banned)
    rows.append((total, irq, aff, on_ban, " ".join(parts[1 + len(cpu_ids):])))
print(f"Banned CPUs: {sorted(banned)}")
for total, irq, aff, on_ban, desc in sorted(rows, reverse=True)[:20]:
    print(f"IRQ {irq:3s} aff={aff:6s} total={total:12d} on_banned={on_ban:10d} {desc}")
EOF
```

### 1.5 Root-Cause Drill-Down for Any Anomaly

For every spike, burst, episode, or `cst doctor` `WARN` observed above:
- Correlate exact timestamps against `journalctl --since "<window>"` (timers such as `btrfs-snapshot.service`, `cst-desktop-psd-resync`, `cst-toolchains-prune`, `btrfs-scrub-weekly`).
- For `allocstall` bursts or `D`-state episodes, inspect second-by-second `stall-*.jsonl*` and `below dump` samples around those timestamps to identify the exact cgroup/process and whether memory/IO pressure spilled over to `session.slice` or `app.slice`.
- Inspect the relevant CST source module before claiming a configuration or code issue.

---

## Step 2: Draft the Report Structure

Structure the report in the following order (in the user's language):

### 1. Plain-Language Overview ("Resumo simples" / "Executive Overview")

Start with a scannable table covering each category in plain words anyone can understand without knowing kernel internals:

| Category | Status | Plain-language summary | Worth changing? |
|---|---|---|---|
| **Btrfs / Storage** | `Healthy` / `Minor hygiene` / `Improvement worth doing` | 1–2 plain sentences on stability, stalls, and disk headroom during the window | Explicit verdict: **"Nothing worth changing"** or the specific high-value fix |
| **zswap & Memory** | `Healthy` / `Minor hygiene` / `Improvement worth doing` | 1–2 plain sentences on RAM pressure, swap speed, and whether desktop lagged | Explicit verdict |
| **CPU & Scheduling** | `Healthy` / `Minor hygiene` / `Improvement worth doing` | 1–2 plain sentences on saturation vs. desktop/UI smoothness | Explicit verdict |
| **Other CST / Desktop** | `Healthy` / `Minor hygiene` / `Improvement worth doing` | 1–2 plain sentences on any other finding from `cst doctor` / timers | Explicit verdict |

Immediately follow with a short callout explaining any gap between cumulative since-boot `cst perf` / `cst doctor` alerts and the actual window telemetry, plus any staged deployment awaiting reboot.

### 2. Btrfs / Storage (Window + Before/After)

- Plain-language takeaway + explicit improvement verdict.
- Config coherence line (`flushoncommit`, `commit=60`, `compress-force`, `vm.dirty_*`, `blk-iocost`, `nvme APST`, `SCRUB_LIMIT`).
- **Before vs. after table** (if a relevant CST change happened in the log history) and/or window bucket breakdown.
- Window metrics: root & cache commits/h, avg, p50, p95, p99, max, `>=500ms`, `>=1s`; `dirty` avg/p95/max vs. `vm.dirty_bytes`; `IO PSI`; `dm-crypt` & NVMe `await`, flushes, write totals, device errors; unallocated & metadata chunk percentages (from `cst doctor`, as non-root `btrfs device usage` prints `N/A`); worst episodes with proven root cause.

### 3. zswap & Memory (Window)

- Plain-language takeaway + explicit improvement verdict.
- Active config summary (`zswap`, compressor, pool cap, `swappiness`, `page-cluster`, `watermark_scale_factor`, `min_free_kbytes`, `THP defrag`, `MGLRU min_ttl_ms`, `MemoryMin`/`MemoryLow`).
- Pool usage & compression ratio and since-boot `rb%` (from `cst swap status`); window `zswpout`, `zswpin`, `zswpwb`, `pswpout`, `pswpin`; **bypass check (`pswpout == zswpwb`)**; **window read-back % (`pswpin / pswpout`)** vs. since-boot; `swap0` `await`; OOM kills.
- `Memory PSI` system-wide and on `session.slice` / `app.slice`.
- `allocstall` / `pgscan_direct` / `pgscan_kswapd` bucket breakdown explaining whether reclaim was continuous or isolated to a specific burst (with cgroup and timestamps).

### 4. CPU & Scheduling (Window)

- Plain-language takeaway + explicit improvement verdict.
- Active config summary (`preempt`, `hwp_dynamic_boost`, governor/EPP, C-states cap, `ntsync`, `irqbalance` ban-list verified against `/proc/interrupts`, game profile, GPU PM).
- System CPU utilization (`avg`, `p50`, `p95`, `max`), User/System, `load1` avg/max, `CPU PSI some`, `nr_throttled`.
- **Per-cgroup isolation table** (`cpu.weight` / `io.weight`, CPU avg/max, `CPU PSI`, `IO PSI`, `Mem PSI`) for `session.slice`, `app.slice`, inner `user.slice` (and heavy child workloads), `builds.slice`, `machine.slice`, `system.slice`.

### 5. Improvements & Hygiene (Strictly Prioritized)

Split into two explicit lists:
1. **Improvements worth doing (meaningful impact)** — only items with proven measurable impact, citing exact numbers and files; or explicitly state *"None — current CST settings are already working well for this workload."*
2. **Minor hygiene (optional, low priority)** — e.g., pending reboot, resetting failed transient user scopes.

---

## Step 3: Mandatory Independent Review (Before Answering the User)

Before presenting the report to the user, launch a **fresh read-only subagent** using `invoke_subagent` (`TypeName="research"`, `Role="Report Accuracy Reviewer"`, or the runtime's equivalent read-only subagent tool). Provide it with:
1. The full draft report.
2. The raw command outputs (or paths to saved outputs / log files) so it can cross-check numbers directly.

Instruct the reviewer to verify:
1. **Numerical & unit accuracy:** check every count, average, percentile, percentage, MiB/GiB conversion, and timestamp against raw outputs; confirm no cumulative since-boot metric is presented as a window metric.
2. **Causal rigor:** confirm every root-cause claim (for episodes, commit spikes, `allocstall` bursts, IRQ placement, or timer interactions) is backed by observed timestamp/stack/code evidence rather than speculation.
3. **Recommendation discipline:** challenge every proposed improvement (is the impact real and proven?) and check whether any real bottleneck or anomaly in the raw data was missed or understated.
4. **Plain-language clarity:** confirm each category's summary is genuinely easy for a non-kernel-expert to read and clearly states whether anything is worth improving or not.

Wait for the reviewer's reply. Fix any factual error, unsupported claim, missed finding, or unclear wording (re-checking any new query if needed) before outputting the final report. End the report with a brief one-line note confirming the independent review verdict (and any correction applied). If the runtime does not support subagents, state explicitly that independent review could not be run — never fabricate a review.

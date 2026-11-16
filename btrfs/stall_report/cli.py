"""btrfs-stall-report's text report, the arguments and main()."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from btrfs.stall_report.adapters import LOG_DIR, container_names, load, parse_time
from btrfs.stall_report.analysis import analyse
from btrfs.stall_report.domain import (
    AnalyseResult,
    MetaRecord,
    UNITS,
    fmt_bytes,
    fmt_counts,
    fmt_ts,
    verdict,
    zswap_text,
)


def print_report(res: AnalyseResult, meta: MetaRecord, verbose: bool) -> None:
    w = res["window"]
    print(
        f"window   {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(w['since']))} -> {fmt_ts(w['until'])}"
        f"  kernel {res.get('kernel') or '?'}"
    )
    cov = res["coverage"]
    if cov.get("first_sample"):
        print(
            f"recorded {fmt_ts(cov['first_sample'])} -> {fmt_ts(cov['last_sample'])} "
            f"({cov['covered_s']} s of {cov['window_s']} s{', ' + str(cov.get('gaps')) + ' gap(s)' if cov.get('gaps') else ''})"
        )
    print()
    for line in verdict(res, meta):
        print(f"verdict  {line}")
    if res["samples"] < 2:
        return
    print()
    pct, peak, vm = res["pressure"], res["peak"], res["vm"]
    print(
        f"pressure io some/full {pct['io_some_pct']:.1f}% / {pct['io_full_pct']:.1f}%   memory some/full "
        f"{pct['mem_some_pct']:.1f}% / {pct['mem_full_pct']:.1f}%   cpu some {pct['cpu_some_pct']:.1f}%   "
        f"(% of recorded time)"
    )
    print(
        f"peaks    io 10s-avg some/full {peak['io_some_avg10']:.0f}%/{peak['io_full_avg10']:.0f}%  "
        f"tasks waiting on storage {peak['d_tasks']}  on memory {peak['mem_tasks']}  "
        f"iowait tasks {peak['iowait_tasks']}  "
        f"dirty {fmt_bytes(peak['dirty_bytes'])} (limit {fmt_bytes(vm['dirty_limit_bytes'])})  "
        f"writeback {fmt_bytes(peak['writeback_bytes'])}"
    )
    for uuid, f in res["fs"].items():
        label = (meta.get("fs") or {}).get(uuid, {}).get("label") or uuid[:8]
        at = f" @{fmt_ts(f['max_at'])}" if f["max_at"] and f["max_commit_ms"] else ""
        sp = (f.get("space") or {}).get("metadata") or [0, 0, 0, 0, 0]
        mu = sp[0] / sp[1] * 100 if sp[1] else 0
        print(
            f"btrfs    {label}: {f['commits']} commits, avg {f['avg_commit_ms']:.0f} ms, longest "
            f"{f['max_commit_ms']} ms{at}; metadata {mu:.0f}% of allocated chunks; new device errors "
            f"{f['new_device_errors']}"
        )
    print("disks    per device; each RAID1 member carries a full copy, dm-N is the dm-crypt layer above it")
    for name, d in sorted(res["disks"].items(), key=lambda kv: (kv[0].startswith("dm-"), kv[0])):
        if name.startswith("dm-") and not verbose and not d["role"].startswith("swap"):
            continue
        print(
            f"  {name:<8} {d['role']:<13} await {d['await_ms']:5.1f} ms (r {d['read_await_ms']:.1f} / "
            f"w {d['write_await_ms']:.1f})  flush await {d['flush_await_ms']:5.1f} ms "
            f"({d['fl_ios']} flushes)  queue {d['queue_depth']:4.1f}  read {fmt_bytes(d['read_bytes'])}  "
            f"write {fmt_bytes(d['write_bytes'])}"
        )
    print(
        f"memory   {zswap_text(vm)}   direct reclaim scanned "
        f"{vm['direct_reclaim_scanned_pages']:,} pages  allocstalls {vm['allocstalls']:,}"
    )
    names = container_names() if any(k.startswith(("docker-", "libpod-")) for k in res["cgroups"]) else {}
    print(
        "cgroups  IO pressure = time tasks in the cgroup waited on IO; bytes = writes charged to it at the "
        "dm-crypt layer (RAID1 mirror copies are charged unevenly, often to kernel threads: compare "
        "cgroups with each other, not with disk totals)"
    )
    shown = 0
    for name, c in sorted(
        res["cgroups"].items(),
        key=lambda kv: (not kv[0].endswith(".slice"), -(kv[1]["io_some"] + kv[1]["write_bytes"] / 1e3)),
    ):
        is_slice = name.endswith(".slice")
        if not is_slice and (c["io_some_pct"] < 1 and c["write_bytes"] < 64 * 2**20 or shown >= 6):
            continue
        shown += 0 if is_slice else 1
        label = f"{name} ({names[name]})" if name in names else name
        print(
            f"  {label:<44} io some/full {c['io_some_pct']:5.1f}% / {c['io_full_pct']:5.1f}%  "
            f"read {fmt_bytes(c['read_bytes'])}  write {fmt_bytes(c['write_bytes'])}"
        )
    b = res.get("below")
    if b:
        if b.get("error"):
            print(f"below    {b['error']}")
        for title, key in (("writers", "top_writers"), ("readers", "top_readers")):
            items = b.get(key) or []
            if items:
                print(
                    f"below    top {title}: "
                    + ", ".join(
                        f"{i['comm']}[{i['cgroup']}] {fmt_bytes(i['bytes'])}"
                        if i["cgroup"]
                        else f"{i['comm']} {fmt_bytes(i['bytes'])}"
                        for i in items[:6]
                    )
                )
    eps = res["episodes"]
    limit = 50 if verbose else 6
    print()
    print(
        f"episodes {len(eps)}"
        + (
            " (worst first" + (f", showing {limit}" if len(eps) > limit else "") + ")"
            if eps
            else " (no stall episode recorded)"
        )
    )
    for ep in eps[:limit]:
        pk = ep["peak"]
        print(
            f"  {fmt_ts(ep['start'])}-{fmt_ts(ep['end'])} {ep['end'] - ep['start']:4.0f}s  opened by: {ep['reason']}; "
            f"peak io full {pk.get('io_full_avg10', 0):.0f}%, commit {pk.get('commit_ms', 0)} ms, "
            f"D tasks max {ep['max_d']}"
        )
        for s in ep["sigs"][: (8 if verbose else 3)]:
            via = f" via {s['ctx']}" if s["ctx"] else ""
            print(
                f"    {s['n']:5d} task-samples [{s['cat']}] {s['sig']}{via}  "
                f"comms: {fmt_counts(s['comms'])}  cgroups: {', '.join(list(s['cgroups'])[:3])}"
            )
            if verbose:
                for frames in s.get("examples", [])[:1]:
                    print("          stack: " + " <- ".join(frames[:10]))
    if eps:
        print("         (task-samples: one task seen in D state in one 1 s sample, roughly task-seconds)")
    if res["events"]:
        print()
        print(f"events   {len(res['events'])} kernel/daemon event(s) ('!' = severe, on this host's disks):")
        for e in res["events"][-(30 if verbose else 8) :]:
            print(f"  {'!' if e.get('severe') else ' '} {fmt_ts(e['ts'])} {e['msg'][:170]}")
    for a in res["alerts"]:
        print(f"alert    {fmt_ts(a['ts'])} {a['title']}: {a['body']}")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="btrfs-stall-report", description="Summarise recorded IO/btrfs stalls for a time window."
    )
    ap.add_argument(
        "--since",
        "-s",
        default="30 min ago",
        help="start: date -d syntax ('20 min ago', '10:31') or @epoch; default 30 min ago",
    )
    ap.add_argument("--until", "-u", default=None, help="end; default now")
    ap.add_argument("--json", action="store_true", help="machine-readable output (verdict first)")
    ap.add_argument("-v", "--verbose", action="store_true", help="more episodes, dm-crypt rows, example stacks")
    ap.add_argument("--no-below", action="store_true", help="skip below's per-process section (faster)")
    ap.add_argument("--log-dir", default=LOG_DIR)
    args = ap.parse_args()
    now = time.time()
    until = parse_time(args.until, now)
    since = parse_time(args.since, now - 1800)
    if since >= until:
        sys.exit("--since must be before --until")
    if not os.path.isdir(args.log_dir):
        sys.exit(
            f"{args.log_dir} not found: btrfs-stall-monitor is not installed on this host. "
            f"Installer: cst btrfs install (run on the host, not in toolbox)"
        )
    res, meta = analyse(load(args.log_dir, since, until), since, until, not args.no_below, args.verbose)
    if args.json:
        out: dict[str, object] = {"verdict": verdict(res, meta), "coverage": res.pop("coverage"), "units": UNITS}
        out.update(res)
        json.dump(out, sys.stdout, indent=1, default=str)
        print()
    else:
        print_report(res, meta, args.verbose)

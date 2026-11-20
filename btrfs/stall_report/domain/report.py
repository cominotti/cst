# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-report's text report, no I/O: the lines of one window's report (verdict, pressure,
btrfs, disks, memory, cgroups, below, episodes, events and alerts)."""

from collections.abc import Callable, Iterator, Mapping
import time

from btrfs.stall_report.domain.records import AnalyseResult, Episode, MetaRecord
from btrfs.stall_report.domain.verdict import fmt_bytes, fmt_counts, fmt_ts, verdict, zswap_text


def report_lines(
    res: AnalyseResult, meta: MetaRecord, container_names: Callable[[], Mapping[str, str]], *, verbose: bool
) -> Iterator[str]:
    """The text report's lines, in order ("" for an empty line), each made when it is asked for: a
    printer shows the lines before the cgroups' while container_names() runs. container_names() gives
    the containers' names by cgroup; it is asked only when a cgroup is a container's."""
    yield from _window_lines(res)
    yield ""
    yield from (f"verdict  {line}" for line in verdict(res, meta))
    if res["samples"] < 2:
        return
    yield ""
    yield from _pressure_lines(res)
    yield from _fs_lines(res, meta)
    yield from _disk_lines(res, verbose=verbose)
    yield _memory_line(res)
    yield from _cgroup_lines(res, container_names)
    yield from _below_lines(res)
    yield from _episode_lines(res, verbose=verbose)
    yield from _event_lines(res, verbose=verbose)


def _window_lines(res: AnalyseResult) -> list[str]:
    """The window and kernel, and what of it was recorded."""
    w = res["window"]
    out = [
        (
            f"window   {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(w['since']))} -> {fmt_ts(w['until'])}"
            f"  kernel {res.get('kernel') or '?'}"
        )
    ]
    cov = res["coverage"]
    if (first := cov.get("first_sample")) is not None and first != 0:
        out.append(
            f"recorded {fmt_ts(first)} -> {fmt_ts(cov['last_sample'])} "
            f"({cov['covered_s']} s of {cov['window_s']} s{', ' + str(cov.get('gaps')) + ' gap(s)' if cov.get('gaps', 0) else ''})"
        )
    return out


def _pressure_lines(res: AnalyseResult) -> list[str]:
    """The pressure shares and the peaks."""
    pct, peak, vm = res["pressure"], res["peak"], res["vm"]
    return [
        (
            f"pressure io some/full {pct['io_some_pct']:.1f}% / {pct['io_full_pct']:.1f}%   memory some/full "
            f"{pct['mem_some_pct']:.1f}% / {pct['mem_full_pct']:.1f}%   cpu some {pct['cpu_some_pct']:.1f}%   "
            f"(% of recorded time)"
        ),
        (
            f"peaks    io 10s-avg some/full {peak['io_some_avg10']:.0f}%/{peak['io_full_avg10']:.0f}%  "
            f"tasks waiting on storage {peak['d_tasks']}  on memory {peak['mem_tasks']}  "
            f"iowait tasks {peak['iowait_tasks']}  "
            f"dirty {fmt_bytes(peak['dirty_bytes'])} (limit {fmt_bytes(vm['dirty_limit_bytes'])})  "
            f"writeback {fmt_bytes(peak['writeback_bytes'])}"
        ),
    ]


def _fs_lines(res: AnalyseResult, meta: MetaRecord) -> list[str]:
    """One line per btrfs filesystem: its commits, metadata room and new device errors."""
    out = []
    for uuid, f in res["fs"].items():
        label = (meta.get("fs") or {}).get(uuid, {}).get("label") or uuid[:8]
        at = f" @{fmt_ts(f['max_at'])}" if f["max_at"] is not None and f["max_at"] != 0 and f["max_commit_ms"] else ""
        sp = (f.get("space") or {}).get("metadata") or [0, 0, 0, 0, 0]
        mu = sp[0] / sp[1] * 100 if sp[1] else 0
        out.append(
            f"btrfs    {label}: {f['commits']} commits, avg {f['avg_commit_ms']:.0f} ms, longest "
            f"{f['max_commit_ms']} ms{at}; metadata {mu:.0f}% of allocated chunks; new device errors "
            f"{f['new_device_errors']}"
        )
    return out


def _disk_lines(res: AnalyseResult, *, verbose: bool) -> list[str]:
    """The disks: the member disks, then the dm-crypt layer (only when verbose, swap always)."""
    out = ["disks    per device; each RAID1 member carries a full copy, dm-N is the dm-crypt layer above it"]
    for name, d in sorted(res["disks"].items(), key=lambda kv: (kv[0].startswith("dm-"), kv[0])):
        if name.startswith("dm-") and not verbose and not d["role"].startswith("swap"):
            continue
        out.append(
            f"  {name:<8} {d['role']:<13} await {d['await_ms']:5.1f} ms (r {d['read_await_ms']:.1f} / "
            f"w {d['write_await_ms']:.1f})  flush await {d['flush_await_ms']:5.1f} ms "
            f"({d['fl_ios']} flushes)  queue {d['queue_depth']:4.1f}  read {fmt_bytes(d['read_bytes'])}  "
            f"write {fmt_bytes(d['write_bytes'])}"
        )
    return out


def _memory_line(res: AnalyseResult) -> str:
    """The memory line: zswap and swap, direct reclaim and allocation stalls."""
    vm = res["vm"]
    return (
        f"memory   {zswap_text(vm)}   direct reclaim scanned "
        f"{vm['direct_reclaim_scanned_pages']:,} pages  allocstalls {vm['allocstalls']:,}"
    )


def _cgroup_lines(res: AnalyseResult, container_names: Callable[[], Mapping[str, str]]) -> list[str]:
    """The cgroups: the slices, and at most 6 others that waited or wrote (with the names of the
    containers among them)."""
    names = container_names() if any(k.startswith(("docker-", "libpod-")) for k in res["cgroups"]) else {}
    out = [
        (
            "cgroups  IO pressure = time tasks in the cgroup waited on IO; bytes = writes charged to it at the "
            "dm-crypt layer (RAID1 mirror copies are charged unevenly, often to kernel threads: compare "
            "cgroups with each other, not with disk totals)"
        )
    ]
    shown = 0
    for name, c in sorted(
        res["cgroups"].items(),
        key=lambda kv: (not kv[0].endswith(".slice"), -(kv[1]["io_some"] + kv[1]["write_bytes"] / 1e3)),
    ):
        is_slice = name.endswith(".slice")
        if not is_slice and ((c["io_some_pct"] < 1 and c["write_bytes"] < 64 * 2**20) or shown >= 6):
            continue
        shown += 0 if is_slice else 1
        label = f"{name} ({names[name]})" if name in names else name
        out.append(
            f"  {label:<44} io some/full {c['io_some_pct']:5.1f}% / {c['io_full_pct']:5.1f}%  "
            f"read {fmt_bytes(c['read_bytes'])}  write {fmt_bytes(c['write_bytes'])}"
        )
    return out


def _below_lines(res: AnalyseResult) -> list[str]:
    """below's error, and its top writers and readers."""
    b = res.get("below")
    if not b:
        return []
    out = []
    if (err := b.get("error")) is not None and err != "":
        out.append(f"below    {err}")
    for title, key in (("writers", "top_writers"), ("readers", "top_readers")):
        items = b.get(key) or []
        if items:
            out.append(
                f"below    top {title}: "
                + ", ".join(
                    f"{i['comm']}[{i['cgroup']}] {fmt_bytes(i['bytes'])}"
                    if i["cgroup"]
                    else f"{i['comm']} {fmt_bytes(i['bytes'])}"
                    for i in items[:6]
                )
            )
    return out


def _episode_lines(res: AnalyseResult, *, verbose: bool) -> list[str]:
    """The episodes, worst first, with their main signatures (more of both, and a stack, when verbose)."""
    eps = res["episodes"]
    limit = 50 if verbose else 6
    showing = f", showing {limit}" if len(eps) > limit else ""
    out: list[str] = [
        "",
        f"episodes {len(eps)}" + (f" (worst first{showing})" if eps else " (no stall episode recorded)"),
    ]
    for ep in eps[:limit]:
        pk = ep["peak"]
        out.append(
            f"  {fmt_ts(ep['start'])}-{fmt_ts(ep['end'])} {ep['end'] - ep['start']:4.0f}s  opened by: {ep['reason']}; "
            f"peak io full {pk.get('io_full_avg10', 0):.0f}%, commit {pk.get('commit_ms', 0)} ms, "
            f"D tasks max {ep['max_d']}"
        )
        out.extend(_signature_lines(ep, verbose=verbose))
    if eps:
        out.append("         (task-samples: one task seen in D state in one 1 s sample, roughly task-seconds)")
    return out


def _signature_lines(ep: Episode, *, verbose: bool) -> list[str]:
    """An episode's main wait signatures (8 when verbose, else 3), with one example stack when verbose."""
    out = []
    for s in ep["sigs"][: (8 if verbose else 3)]:
        via = f" via {s['ctx']}" if s["ctx"] else ""
        out.append(
            f"    {s['n']:5d} task-samples [{s['cat']}] {s['sig']}{via}  "
            f"comms: {fmt_counts(s['comms'])}  cgroups: {', '.join(list(s['cgroups'])[:3])}"
        )
        if verbose:
            out.extend("          stack: " + " <- ".join(frames[:10]) for frames in s.get("examples", [])[:1])
    return out


def _event_lines(res: AnalyseResult, *, verbose: bool) -> list[str]:
    """The last events (more when verbose) and the alerts."""
    out = []
    if res["events"]:
        out.append("")
        out.append(f"events   {len(res['events'])} kernel/daemon event(s) ('!' = severe, on this host's disks):")
        out.extend(
            f"  {'!' if e.get('severe', False) else ' '} {fmt_ts(e['ts'])} {e.get('msg', '')[:170]}"
            for e in res["events"][-(30 if verbose else 8) :]
        )
    out.extend(f"alert    {fmt_ts(a['ts'])} {a.get('title', '?')}: {a.get('body', '')}" for a in res["alerts"])
    return out

# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-report's verdict, no I/O: the verdict lines of one window (storage, memory, FUSE, busy and maintenance waits, disk saturation) and the number and time formats of the report."""

import re
import time

from btrfs.stall_monitord.domain import HUNG_SECONDS
from btrfs.stall_report.domain.categories import HINTS
from btrfs.stall_report.domain.records import (
    AnalyseResult,
    CategoryShare,
    DiskTotals,
    MetaRecord,
    OpenEpisode,
    VmTotals,
)
from cst_core.domain.values import GIB, format_bytes_gib_mib


def kernel_tuple(release: str | None) -> tuple[int, ...]:
    m = re.match(r"(\d+)\.(\d+)(?:\.(\d+))?", release or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def fmt_bytes(b: float) -> str:
    return format_bytes_gib_mib(b, gib_threshold=10 * GIB, comma=True, mib_floor=False)


def fmt_ts(ts: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def zswap_text(vm: VmTotals) -> str:
    """zswap pool traffic and disk swap: 'zswap in/out X / Y   disk swap in/out A / B'.
    pswpout counts zswap writeback too, so only swap-out that bypassed zswap
    gets its own figure."""
    disk = f"disk swap in/out {fmt_bytes(vm['swap_in_bytes'])} / {fmt_bytes(vm['swap_out_bytes'])}"
    zswap_in, zswap_out = vm["zswap_in_bytes"], vm["zswap_out_bytes"]
    # summarise() sets the two together: both None without a v7+ sample.
    if zswap_out is None or zswap_in is None:
        return f"zswap in/out n/a (daemon < v7)   {disk}"
    since = vm.get("zswap_counted_from")
    bypass = vm.get("swap_out_bypass_bytes") or 0
    return (
        f"zswap in/out {fmt_bytes(zswap_in)} / {fmt_bytes(zswap_out)}"
        f"{f' (from {fmt_ts(since)})' if since is not None and since != 0 else ''}   {disk}"
        f"{f' ({fmt_bytes(bypass)} of it not through zswap)' if bypass >= 2**20 else ''}"
    )


def fmt_counts(d: dict[str, int], n: int = 4) -> str:
    return ", ".join(f"{k}({v})" for k, v in list(d.items())[:n])


def memory_lines(res: AnalyseResult, storage_d: int) -> list[str]:
    """Memory waits (reclaim, swap, zswap): not counted as storage."""
    peak = res.get("peak", {}).get("mem_tasks", 0)
    ev = [e for e in res["events"] if e.get("kind") == "hung-memory"]
    if peak < 3 and not ev:
        return []
    notes = "".join(
        (
            ["; estimated from the stacks: recorded by a daemon < v8"]
            if any(e.get("estimated") and e.get("max_d_mem") for e in res["episodes"])
            else []
        )
        + ([f"; {len(ev)} task(s) stuck 30+ s, see 'events'"] if ev else [])
    )
    if peak >= storage_d:
        return [
            (
                f"Memory, not storage, blocked up to {peak} tasks (memory reclaim, swap-in or zswap{notes}). "
                f"See the memory line; `cst swap status` shows which cgroup swaps."
            )
        ]
    return [f"Memory waits: up to {peak} tasks also waited on memory reclaim, swap-in or zswap{notes}."]


def saturation_line(res: AnalyseResult) -> str | None:
    """Were the btrfs disks at their limit? Judged at the dm-crypt layer,
    which is what btrfs waits for; the NVMe figures below it are information
    only. Without dm-crypt, the member disks themselves."""
    nvme = [d for n, d in res["disks"].items() if d["role"].startswith("btrfs") and not n.startswith("dm-")]
    dm = [d for n, d in res["disks"].items() if d["role"].startswith("btrfs") and n.startswith("dm-")]
    top = dm or nvme
    if not top:
        return None

    def fig(ds: list[DiskTotals]) -> str:
        return (
            f"await {max(d['await_ms'] for d in ds):.1f} ms (read {max(d['read_await_ms'] for d in ds):.1f}, "
            f"write {max(d['write_await_ms'] for d in ds):.1f})"
        )

    aw = max(d["await_ms"] for d in top)
    qd = max(d["queue_depth"] for d in top)
    busy = aw >= 5 or qd >= 8
    line = (
        f"btrfs disks {'look SATURATED' if busy else 'NOT saturated'}: "
        f"{'dm-crypt layer (what btrfs waits for)' if dm else 'member disks'} {fig(top)}, queue depth {qd:.1f}"
    )
    if dm and nvme:
        aw_nvme = max(d["await_ms"] for d in nvme)
        line += (
            f"; NVMe below {fig(nvme)}, information only (its time can include disk cache flushes)"
            f"{'; encryption queueing adds latency' if aw > 2 * aw_nvme + 1 else ''}"
        )
    return line + "."


def fuse_lines(res: AnalyseResult) -> list[str]:
    n = res.get("non_storage_waits", {}).get("fuse", 0)
    ev = [e for e in res["events"] if e.get("kind") == "fuse-wait"]
    if not n and not ev:
        return []
    return [
        (
            f"FUSE waits: programs waited on a FUSE filesystem ({n} task-samples"
            f"{', ' + str(len(ev)) + ' long wait(s) in events' if ev else ''}): a gvfs network share "
            f"(/run/user/*/gvfs; /run/host/run/user/*/gvfs inside toolbox) or the document portal answered "
            f"slowly. Usually the network or the FUSE daemon, typically a find/search walking into a share "
            f"(see 'events' for the command); it only involves the local disks if storage waits show up "
            f"at the same time."
        )
    ]


def busy_lines(res: AnalyseResult) -> list[str]:
    """Storage waits that kept being scheduled (daemon v6+), and, for records
    of older daemons, 'hung' alerts in the fsync path that may have been one."""
    out = []
    ev = [e for e in res["events"] if e.get("kind") in ("io-busy", "io-busy-long")]
    if ev:
        comms = sorted({e.get("comm") or re.sub(r":\d+ .*", "", e.get("msg", "")) for e in ev})
        long_ = sum(e.get("kind") == "io-busy-long" for e in ev)
        out.append(
            f"Busy waits: {len(ev)} task(s) waited on storage at every sample for {HUNG_SECONDS}+ s while "
            f"still being scheduled ({', '.join(comms)[:120]})"
            f"{f', {long_} for 10+ min' if long_ else ''}: usually an fsync/IO loop (lever: that "
            f"program's fsync rate). It is a slow device only if the disk lines show high await or "
            f"errors."
        )
    v6 = res.get("v6_since")
    old = [
        e
        for e in res["events"]
        if e.get("kind") == "hung"
        and e.get("severe", False)
        and (v6 is None or e.get("ts", 0) < v6)
        and any(f in ("btrfs_sync_log", "btrfs_sync_file") for f in e.get("stack") or ())
    ]
    if old:
        out.append(
            f"Note: {len(old)} 'hung task' event(s) in the fsync path were recorded before progress checks "
            f"(daemon < v6): they may have been a program calling fsync in a loop rather than a hang; "
            f"check the disk lines (await, errors) of the same window."
        )
    return out


def maint_lines(res: AnalyseResult) -> list[str]:
    n = res.get("non_storage_waits", {}).get("maintenance", 0)
    ev = [e for e in res["events"] if e.get("kind") == "maintenance-wait"]
    peak = res.get("peak", {}).get("maint_tasks", 0)
    # A single async-discard blip is not worth a verdict line.
    if n < 3 and not ev and res.get("maintenance_samples", 0) < 3:
        return []
    return [
        (
            f"Maintenance: btrfs housekeeping ran (fstrim/discard, scrub or balance; {n} task-samples"
            f"{', ' + str(len(ev)) + ' long wait(s) in events' if ev else ''}"
            f"{', up to ' + str(peak) + ' task(s) at once' if peak else ''}). Expected background work, "
            f"not a workload stall; it only slowed the workload if storage waits show up at the same time."
        )
    ]


def _not_a_bottleneck(res: AnalyseResult, longest: int, written: int, mem_lines: list[str]) -> list[str]:
    """The verdict's lines when storage was not a bottleneck: the figures, the memory lines, and
    CPU, memory pressure or too little activity when they show."""
    pct, peak = res["pressure"], res["peak"]
    out = [
        (
            f"Storage was NOT a bottleneck: IO pressure {pct['io_some_pct']:.1f}%, at most "
            f"{peak['d_tasks']} task(s) waiting on storage, longest btrfs commit {longest} ms "
            f"(written {fmt_bytes(written)})."
        ),
        *mem_lines,
    ]
    if pct["cpu_some_pct"] >= 20:
        out.append(f"CPU contention: runnable tasks waited for a CPU {pct['cpu_some_pct']:.0f}% of the time.")
    if pct["mem_some_pct"] >= 5:
        out.append(f"Memory pressure {pct['mem_some_pct']:.1f}%: see the memory line.")
    if written < 50 * 2**20 and pct["cpu_some_pct"] < 2:
        out.append("Little activity recorded: check that the window really covers the workload.")
    return out


def _blocked_lines(storage_d: int, mem_lines: list[str], *, d_stall: bool, mem_dominates: bool) -> list[str]:
    """The blocked tasks' line (when D-state tasks waited on storage) and the memory lines, memory
    first when it dominates."""
    if d_stall and mem_dominates:
        return [*mem_lines, f"Blocked tasks: also up to {storage_d} tasks waiting on storage in D state."]
    if d_stall:
        return [
            (
                f"Blocked tasks: up to {storage_d} tasks waiting on storage in D state "
                f"(btrfs commit/log/lock waits block tasks without showing up as IO pressure)."
            ),
            *mem_lines,
        ]
    return mem_lines


def _open_episode_lines(oe: OpenEpisode | None) -> list[str]:
    """The line of an episode that is still open (no record yet), or none."""
    if not oe:
        return []
    return [
        (
            f"Episode still open: since {fmt_ts(oe['since'])} the monitor samples every second and up to "
            f"{oe['d_tasks']} tasks wait on storage. Its record (stacks, categories) is written when it "
            f"ends; run the report again after that for the details."
        )
    ]


def _commit_line(longest: int, longest_at: float | None) -> str:
    """The slow commits' line: the longest, and when it was."""
    at = " at " + fmt_ts(longest_at) if longest_at is not None and longest_at != 0 else ""
    return f"Slow btrfs commits: longest {longest / 1000:.1f} s{at}."


def _category_lines(cats: list[CategoryShare], meta: MetaRecord) -> list[str]:
    """Where blocked tasks waited (the first three categories) and the main category's hint."""
    if not cats:
        return []
    desc = ", ".join(f"{c['description']} {c['share'] * 100:.0f}%" for c in cats[:3])
    out = [f"Where blocked tasks waited (share of sampled D-state tasks): {desc}."]
    main = cats[0]["category"]
    if main == "zstd-workspace":
        if kernel_tuple(meta.get("kernel")) < (7, 2, 6):
            out.append("Matches the btrfs zstd lost-wakeup bug fixed in 7.2.6; a kernel update fixes it.")
        else:
            out.append(
                "zstd workspace waits on a kernel that already has the 7.2.6 lost-wakeup fix; "
                "may be plain compression contention."
            )
    elif HINTS.get(main) is not None:
        out.append(HINTS[main])
    return out


def verdict(res: AnalyseResult, meta: MetaRecord) -> list[str]:
    out = []
    cov = res["coverage"]
    skipped = cov.get("skipped", 0)
    damaged = (
        [f"INCOMPLETE DATA: {skipped} damaged record(s) in this window were left out (re-running does not help)"]
        if skipped
        else []
    )
    if res["samples"] < 2:
        return [
            (
                "NO DATA for this window. Is btrfs-stall-monitor.service running? "
                "(`systemctl status btrfs-stall-monitor`; inside toolbox prefix with flatpak-spawn --host)"
            ),
            *damaged,
        ]
    if not cov["complete"]:
        out.append(
            f"INCOMPLETE DATA: {cov['covered_s']} s of the {cov['window_s']} s window recorded, last sample "
            f"{fmt_ts(cov['last_sample'])}. Data is written every ~10 s; re-run shortly, or narrow the window."
        )
    out += damaged
    sev = [e for e in res["events"] if e.get("severe", False)]
    if sev:
        out.append(f"KERNEL/FS PROBLEMS: {len(sev)} severe event(s) (hung task, btrfs or device error); see 'events'.")
    if any(f["new_device_errors"] for f in res["fs"].values()):
        out.append("btrfs device error counters INCREASED in this window.")
    pct, peak = res["pressure"], res["peak"]
    longest = max((f["max_commit_ms"] for f in res["fs"].values()), default=0)
    longest_at = next((f["max_at"] for f in res["fs"].values() if f["max_commit_ms"] == longest), None)
    psi_stall = pct["io_some_pct"] >= 3 or peak["io_some_avg10"] >= 15
    # Episode records (written when an episode ends) and v5+ samples, which
    # also show an episode that is still open.
    storage_d = max([ep["max_d"] for ep in res["episodes"]] + [res.get("sample_d_tasks", 0)])
    d_stall = storage_d >= 3
    mem_lines = memory_lines(res, storage_d)
    mem_dominates = bool(mem_lines) and mem_lines[0].startswith("Memory, not storage")
    commit_stall = longest >= 1000
    # Logical bytes: one RAID1 member's dm-crypt device.
    written = max(
        (d["write_bytes"] for n, d in res["disks"].items() if n.startswith("dm-") and d["role"].startswith("btrfs")),
        default=0,
    )
    if not (psi_stall or d_stall or commit_stall):
        return (
            out
            + _not_a_bottleneck(res, longest, written, mem_lines)
            + fuse_lines(res)
            + busy_lines(res)
            + maint_lines(res)
        )
    if psi_stall:
        out.append(
            f"IO wait: tasks waited on IO {pct['io_some_pct']:.1f}% of the time (every non-idle task at "
            f"once {pct['io_full_pct']:.1f}%), peak 10 s average {peak['io_some_avg10']:.0f}%."
        )
    out += _blocked_lines(storage_d, mem_lines, d_stall=d_stall, mem_dominates=mem_dominates)
    out += _open_episode_lines(res.get("open_episode"))
    if commit_stall:
        out.append(_commit_line(longest, longest_at))
    out += _category_lines(res["categories"], meta)
    if meta.get("stacks") is False:
        out.append("Caveat: the daemon could not read kernel stacks, so attribution is coarse (wchan only).")
    return out + fuse_lines(res) + busy_lines(res) + maint_lines(res) + _worst_and_load_lines(res)


def _worst_and_load_lines(res: AnalyseResult) -> list[str]:
    """The verdict's last lines: the worst episode, a saturated disk and a large swap-out."""
    out = []
    if res["episodes"]:
        w = res["episodes"][0]
        out.append(
            f"Worst episode: {fmt_ts(w['start'])}-{fmt_ts(w['end'])} ({w['end'] - w['start']:.0f} s, "
            f"{w['max_d']} D tasks, commit {w['peak'].get('commit_ms', 0)} ms)."
        )
    sat = saturation_line(res)
    if sat is not None:
        out.append(sat)
    if res["vm"]["swap_out_bytes"] >= 256 * 2**20:
        out.append(f"Swap-out of {fmt_bytes(res['vm']['swap_out_bytes'])}: memory pressure also loaded the swap disk.")
    return out

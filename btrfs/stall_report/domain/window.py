# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-report's analysis of one window, no I/O: coverage, pressure and peaks, the btrfs, disk, memory and cgroup totals, the categories of the episodes' waits, the corrections for records of older daemons, and an episode still open."""

import itertools
import math

from btrfs.stall_monitord.domain import FUSE_FRAME as FUSE_RE, MAINT_FRAME as MAINT_RE
from btrfs.stall_report.domain.categories import CAT_DESC, MAINT_TEXT, NON_STORAGE, V2_HUNG_SIG, classify
from btrfs.stall_report.domain.records import (
    DI,
    DISK_SUM,
    FRESH_MAX,
    GAP_MAX,
    PAGE,
    ZSWAP_KEYS,
    AnalyseResult,
    CgroupTotals,
    DiskTotals,
    Episode,
    FsTotals,
    MetaRecord,
    OpenEpisode,
    Recorded,
    SampleRecord,
    Sig,
)


def open_episode(samples: list[SampleRecord], episodes: list[Episode]) -> OpenEpisode | None:
    """An episode the daemon has not written yet: the newest samples are 1 s
    apart (episode sampling) with 3+ storage waits (v5+ samples), and no
    episode record covers them. None otherwise."""
    if len(samples) < 2 or "d_maint" not in samples[-1]:
        return None
    run = [samples[-1]]
    for a in reversed(samples[:-1]):
        if run[-1]["ts"] - a["ts"] > 1.5:
            break
        run.append(a)
    if len(run) < 2:
        return None
    last = samples[-1]["ts"]
    if any(ep["start"] - 1 <= last <= ep["end"] + 1 for ep in episodes):
        return None
    peak = max(r.get("d", 0) for r in run)
    if peak < 3:
        return None
    return {"since": run[-1]["ts"], "last": last, "d_tasks": peak}


def summarise(
    recorded: Recorded, since: float, until: float, *, verbose: bool, now: float
) -> tuple[AnalyseResult, MetaRecord]:
    """One window [since, until] of the records: coverage, totals, categories,
    episodes and events (now: the clock, for whether the newest data is in yet).
    The events and alerts in the window are rewritten in place where an older
    daemon named their kind wrongly."""
    meta: MetaRecord | None = None
    # Earliest record of a daemon at version >= N: v6 added progress checks,
    # v7 reads all of /proc/vmstat (zswap counters).
    first_at: dict[int, float] = {}
    for r in recorded.metas:
        if r.get("ts", 0) <= until:
            meta = r
        for n in (6, 7):
            if r.get("v", 0) >= n:
                first_at[n] = min(first_at.get(n, r.get("ts", 0)), r.get("ts", 0))
    samples = [r for r in recorded.samples if since - GAP_MAX <= r["ts"] <= until]
    episodes = [r for r in recorded.episodes if r["end"] >= since and r["start"] <= until]
    events = [r for r in recorded.events if since <= r["ts"] <= until]
    alerts = [r for r in recorded.alerts if since <= r["ts"] <= until]
    samples.sort(key=lambda r: r["ts"])
    while len(samples) > 1 and samples[1]["ts"] <= since:
        samples.pop(0)

    meta = meta or {}
    res: AnalyseResult = {
        "window": {"since": since, "until": until},
        "kernel": meta.get("kernel"),
        "samples": len(samples),
        "episodes": [],
        "events": events,
        "alerts": alerts,
    }
    window_s = until - since
    res["coverage"] = {
        "window_s": round(window_s),
        "covered_s": 0,
        "first_sample": samples[0]["ts"] if samples else None,
        "last_sample": samples[-1]["ts"] if samples else None,
        "complete": False,
        "skipped": sum(1 for t in recorded.damaged if since <= t <= until),
    }
    if len(samples) < 2:
        return res, meta

    vm_keys = meta.get("vm_keys") or []
    vm_idx = {k: i for i, k in enumerate(vm_keys)}

    def vmget(rec: SampleRecord, key: str) -> int:
        i = vm_idx.get(key, -1)
        return rec["vm"][i] if 0 <= i < len(rec.get("vm", [])) else 0

    covered = 0.0
    psi: dict[str, float] = {"io_some": 0, "io_full": 0, "mem_some": 0, "mem_full": 0, "cpu_some": 0}
    peak = {
        "io_some_avg10": 0.0,
        "io_full_avg10": 0.0,
        "mem_some_avg10": 0.0,
        "cpu_some_avg10": 0.0,
        "iowait_tasks": 0,
        "d_tasks": 0,
        "mem_tasks": 0,
        "maint_tasks": 0,
        "dirty_bytes": 0,
        "writeback_bytes": 0,
    }
    vmd = dict.fromkeys(
        (
            k
            for k in vm_keys
            if k not in ("nr_dirty", "nr_writeback", "nr_dirty_threshold", "nr_dirty_background_threshold")
        ),
        0,
    )
    fs: dict[str, FsTotals] = {}
    disks: dict[str, DiskTotals] = {}
    cg: dict[str, CgroupTotals] = {}
    gaps = 0
    # (ts, d, v5+) of samples of daemons v3-v7: their "d" also counted memory
    # waits (and, before v5, btrfs maintenance); corrected after the episodes.
    old_d = []
    sample_d = 0  # most storage waits in one v8+ sample
    maint_samples = 0  # v5+ samples with a btrfs maintenance task in D state
    pswpout_v7 = 0  # pswpout of the v7+ sample pairs (compared with zswpwb)
    v6_since, v7_since = first_at.get(6), first_at.get(7)
    zswap_ok = False  # some sample pair came from a v7+ daemon (zswap counters usable)
    for a, b in itertools.pairwise(samples):
        dt = (b["mono"] - a["mono"]) if "mono" in a and "mono" in b else (b["ts"] - a["ts"])
        if dt <= 0 or dt > GAP_MAX or b["ts"] - a["ts"] > GAP_MAX:
            gaps += 1
            continue
        covered += dt
        for key, res_key, idx in (
            ("io", "io_some", 1),
            ("io", "io_full", 3),
            ("memory", "mem_some", 1),
            ("memory", "mem_full", 3),
            ("cpu", "cpu_some", 1),
        ):
            pa, pb = a["psi"].get(key), b["psi"].get(key)
            if pa and pb and pb[idx] >= pa[idx]:
                psi[res_key] += pb[idx] - pa[idx]
        p = b["psi"]
        peak["io_some_avg10"] = max(peak["io_some_avg10"], (p.get("io") or [0])[0])
        peak["io_full_avg10"] = max(peak["io_full_avg10"], (p.get("io") or [0, 0, 0])[2])
        peak["mem_some_avg10"] = max(peak["mem_some_avg10"], (p.get("memory") or [0])[0])
        peak["cpu_some_avg10"] = max(peak["cpu_some_avg10"], (p.get("cpu") or [0])[0])
        peak["iowait_tasks"] = max(peak["iowait_tasks"], b["procs"][1])
        if "d_all" in b:  # daemon v3+: "d" counts storage waits only
            if "d_maint" in b:  # v5+: maintenance excluded from "d"
                peak["maint_tasks"] = max(peak["maint_tasks"], b["d_maint"])
                maint_samples += b["d_maint"] > 0
            if "d_mem" in b:  # v8+: memory waits excluded from "d"
                sample_d = max(sample_d, b.get("d", 0))
                peak["mem_tasks"] = max(peak["mem_tasks"], b["d_mem"])
            else:
                old_d.append((b["ts"], b.get("d", 0), "d_maint" in b))
        peak["dirty_bytes"] = max(peak["dirty_bytes"], vmget(b, "nr_dirty") * PAGE)
        peak["writeback_bytes"] = max(peak["writeback_bytes"], vmget(b, "nr_writeback") * PAGE)
        # Daemons before v7 read only the first page of /proc/vmstat and stored
        # the zswap counters as 0. Skip them there: a pair that spans the
        # upgrade would otherwise add the whole counter since boot.
        full_vm = v7_since is not None and a["ts"] >= v7_since
        zswap_ok |= full_vm
        for k in vmd:
            if k in ZSWAP_KEYS and not full_vm:
                continue
            d = vmget(b, k) - vmget(a, k)
            if d > 0:
                vmd[k] += d
                if k == "pswpout" and full_vm:
                    pswpout_v7 += d
        for uuid, fb in b["fs"].items():
            fa = a["fs"].get(uuid)
            f = fs.setdefault(
                uuid,
                {
                    "commits": 0,
                    "commit_ms": 0,
                    "max_commit_ms": 0,
                    "max_at": None,
                    "new_device_errors": 0,
                    "space": None,
                },
            )
            if fb["c"][5] > f["max_commit_ms"]:
                f["max_commit_ms"], f["max_at"] = fb["c"][5], b["ts"]
            f["space"] = fb.get("space") or {}
            if not fa:
                continue
            dc, dms = fb["c"][0] - fa["c"][0], fb["c"][1] - fa["c"][1]
            if dc >= 0 and dms >= 0:
                f["commits"] += dc
                f["commit_ms"] += dms
            for devid, n in fb["err"].items():
                if n > fa["err"].get(devid, n):
                    f["new_device_errors"] += n - fa["err"][devid]
        for name, xb in b["disk"].items():
            xa = a["disk"].get(name)
            if not xa:
                continue
            delta = {k: xb[DI[k]] - xa[DI[k]] for k in DISK_SUM}
            if any(v < 0 for v in delta.values()):
                continue
            d = disks.setdefault(
                name,
                {
                    "rd_ios": 0,
                    "wr_ios": 0,
                    "rd_ticks": 0,
                    "wr_ticks": 0,
                    "rd_sectors": 0,
                    "wr_sectors": 0,
                    "io_ticks": 0,
                    "time_in_queue": 0,
                    "fl_ios": 0,
                    "fl_ticks": 0,
                    "peak_util_pct": 0.0,
                    "peak_at": None,
                },
            )
            for k in DISK_SUM:  # the keys of delta, in its order
                d[k] += delta[k]
            util = min(delta["io_ticks"] / (dt * 1000) * 100, 100.0)
            if util > d["peak_util_pct"]:
                d["peak_util_pct"], d["peak_at"] = util, b["ts"]
        for name, vb in b.get("cg", {}).items():
            va = a.get("cg", {}).get(name)
            if not va:
                continue
            dl = [y - x for x, y in zip(va, vb, strict=False)]
            if any(v < 0 for v in dl):
                continue
            c = cg.setdefault(name, {"io_some": 0, "io_full": 0, "read_bytes": 0, "write_bytes": 0, "wall": 0.0})
            c["io_some"] += dl[0]
            c["io_full"] += dl[1]
            c["read_bytes"] += dl[4]
            c["write_bytes"] += dl[5]
            c["wall"] += dt

    last = samples[-1]["ts"]
    res["coverage"].update(
        {
            "covered_s": round(covered),
            "gaps": gaps,
            # Samples are 5 s apart: tolerate up to two missing ones on short windows.
            "complete": covered >= min(0.8 * window_s, window_s - 2 * meta.get("base_interval", 5.0))
            and last >= min(until, now) - FRESH_MAX,
        }
    )
    pct = {f"{k}_pct": v / 1e6 / covered * 100 if covered else 0.0 for k, v in psi.items()}
    res["pressure"] = pct
    res["peak"] = peak
    res["maintenance_samples"] = maint_samples

    # None: no sample in the window came from a v7+ daemon.
    def zswap_bytes(key: str) -> int | None:
        return vmd.get(key, 0) * PAGE if zswap_ok else None

    # Set when the window starts before the v7 upgrade: zswap is counted from there on.
    counted_from = v7_since if zswap_ok and v7_since is not None and v7_since > samples[0]["ts"] else None
    res["vm"] = {
        "swap_in_bytes": vmd.get("pswpin", 0) * PAGE,
        "swap_out_bytes": vmd.get("pswpout", 0) * PAGE,
        "zswap_in_bytes": zswap_bytes("zswpin"),
        "zswap_out_bytes": zswap_bytes("zswpout"),
        "zswap_writeback_bytes": zswap_bytes("zswpwb"),
        # pswpout also counts zswap writeback; the rest bypassed zswap.
        "swap_out_bypass_bytes": max(0, pswpout_v7 - vmd.get("zswpwb", 0)) * PAGE if zswap_ok else None,
        "zswap_counted_from": counted_from,
        "direct_reclaim_scanned_pages": vmd.get("pgscan_direct", 0),
        "allocstalls": vmd.get("allocstall_normal", 0) + vmd.get("allocstall_movable", 0),
        "dirty_limit_bytes": meta.get("dirty_bytes", 0),
    }
    for f in fs.values():
        f["avg_commit_ms"] = f["commit_ms"] / f["commits"] if f["commits"] else 0
    res["fs"] = fs
    roles = meta.get("disks") or {}
    for name, d in disks.items():
        ios = d["rd_ios"] + d["wr_ios"]
        d["role"] = roles.get(name, "")
        d["read_bytes"] = d["rd_sectors"] * 512
        d["write_bytes"] = d["wr_sectors"] * 512
        d["await_ms"] = (d["rd_ticks"] + d["wr_ticks"]) / ios if ios else 0
        d["read_await_ms"] = d["rd_ticks"] / d["rd_ios"] if d["rd_ios"] else 0
        d["write_await_ms"] = d["wr_ticks"] / d["wr_ios"] if d["wr_ios"] else 0
        d["flush_await_ms"] = d["fl_ticks"] / d["fl_ios"] if d["fl_ios"] else 0
        d["util_pct"] = d["io_ticks"] / (covered * 1000) * 100 if covered else 0
        d["queue_depth"] = d["time_in_queue"] / (covered * 1000) if covered else 0
    res["disks"] = disks
    for c in cg.values():
        c["io_some_pct"] = c["io_some"] / 1e6 / c["wall"] * 100 if c["wall"] else 0.0
        c["io_full_pct"] = c["io_full"] / 1e6 / c["wall"] * 100 if c["wall"] else 0.0
    res["cgroups"] = cg

    cat_n, other_n = {}, {}
    cap_eps = []  # (start, end, corrected max_d) of episodes of daemons v3-v7 with non-storage waits
    for record in episodes:
        sigs: list[Sig] = []
        for rec in record["sigs"]:
            cat = classify(rec["sig"], rec.get("examples", []), list(rec.get("comms", {})), rec.get("kind"))
            bucket = other_n if cat in NON_STORAGE else cat_n
            bucket[cat] = bucket.get(cat, 0) + rec["n"]
            s: Sig = {**rec, "cat": cat, "task_samples": rec["n"]}
            if not verbose:
                s.pop("examples", None)
            sigs.append(s)
        ep: Episode = {**record, "sigs": sigs}
        if "max_d_all" not in ep:
            # Recorded by daemon v2, whose max_d counted every D-state task
            # (FUSE and idle waits too): estimate the storage ones as the mean
            # number of storage task-samples per snapshot.
            storage_n = sum(x["n"] for x in sigs if x["cat"] not in NON_STORAGE)
            mem_n = sum(x["n"] for x in sigs if x["cat"] == "reclaim")
            total_n = sum(x["n"] for x in sigs)
            ep["max_d_all"] = ep["max_d"]
            ep["max_d"] = math.ceil(ep["max_d_all"] * storage_n / total_n) if total_n else 0
            ep["max_d_mem"] = math.ceil(ep["max_d_all"] * mem_n / total_n) if total_n else 0
            ep["estimated"] = True
        elif "max_d_mem" not in ep:
            # Daemons v3-v7 counted in max_d every wait they tagged "storage":
            # memory reclaim / swap, and before v5 btrfs maintenance (fstrim,
            # scrub), too. Same estimate, applied to those task-samples.
            counted = [x for x in sigs if x.get("kind", "storage") == "storage"]
            counted_n = sum(x["n"] for x in counted)
            storage_n = sum(x["n"] for x in counted if x["cat"] not in NON_STORAGE)
            mem_n = sum(x["n"] for x in counted if x["cat"] == "reclaim")
            ep["max_d_mem"] = 0
            if counted_n and storage_n < counted_n:
                counted_d = ep["max_d"]
                ep["max_d"] = math.ceil(counted_d * storage_n / counted_n)
                ep["max_d_mem"] = math.ceil(counted_d * mem_n / counted_n)
                ep["estimated"] = True
                # A "hung task" episode opens HUNG_SECONDS (30 s) after the wait began.
                lead = 35 if ep.get("reason") == "hung task" else 0
                cap_eps.append((ep["start"] - lead, ep["end"], ep["max_d"]))
        res["episodes"].append(ep)
    res["episodes"].sort(
        key=lambda e: (-e["max_d"], -e["peak"].get("commit_ms", 0), -e["peak"].get("io_full_avg10", 0))
    )
    total = sum(cat_n.values())
    res["categories"] = [
        {"category": k, "description": CAT_DESC[k], "task_samples": n, "share": round(n / total, 3)}
        for k, n in sorted(cat_n.items(), key=lambda kv: -kv[1])
    ]
    res["non_storage_waits"] = other_n
    # v3-v7 samples during an episode with memory or maintenance waits: cap
    # at the episode's estimate (their "d" counted those waits as storage).
    for ts, recorded_d, v5 in old_d:
        d = recorded_d
        for start, end, max_d in cap_eps:
            if start <= ts <= end:
                d = min(d, max_d)
        res["peak"]["d_tasks"] = max(res["peak"]["d_tasks"], d)
        if v5:
            sample_d = max(sample_d, d)
    res["peak"]["d_tasks"] = max(res["peak"]["d_tasks"], sample_d)
    # Peak storage waits seen in v5+ samples: an episode opens at 3, so this
    # also catches an episode that is still open (no record yet).
    res["sample_d_tasks"] = sample_d
    # Samples from daemon v2 did not separate storage waits: use the episodes' estimate.
    res["peak"]["d_tasks"] = max([res["peak"]["d_tasks"]] + [e["max_d"] for e in res["episodes"]])
    res["peak"]["mem_tasks"] = max([res["peak"]["mem_tasks"]] + [e.get("max_d_mem", 0) for e in res["episodes"]])
    res["open_episode"] = open_episode(samples, res["episodes"])
    # Daemon v2 flagged FUSE waits as hung *disk* tasks; they are not.
    for e in res["events"] + res["alerts"]:
        m = V2_HUNG_SIG.search(e.get("msg", e.get("body", "")))
        if e.get("kind") == "hung" and m and FUSE_RE.match(m.group(1)):
            if "severe" in e:
                e["kind"], e["severe"] = "fuse-wait", False
            else:
                e["kind"], e["title"] = "fuse", "Program waiting on a FUSE filesystem (recorded as stuck on IO)"
    # Daemon <= v4 flagged btrfs maintenance (weekly fstrim, scrub) in D for
    # 30 s as a hung task. Rule: a "hung" event is maintenance if its stack has
    # a MAINT_RE frame, or (no stack, v2) its text names fstrim or a
    # btrfs-scrub/-balance unit (MAINT_TEXT); an alert (text only) if its body
    # is the message of such an event (the daemon emits both with the same
    # text at the same time) or matches MAINT_TEXT.
    maint_msgs = set()
    for e in res["events"]:
        if e.get("kind") == "hung" and (
            any(MAINT_RE.match(f) for f in e.get("stack") or ()) or MAINT_TEXT.search(e.get("msg", ""))
        ):
            e["kind"], e["severe"] = "maintenance-wait", False
            maint_msgs.add(e.get("msg"))
    for a in res["alerts"]:
        if a.get("kind") == "hung" and (a.get("body") in maint_msgs or MAINT_TEXT.search(a.get("body", ""))):
            a["kind"], a["title"] = (
                "maintenance-wait",
                "btrfs maintenance (fstrim/scrub) waiting (recorded as stuck on IO)",
            )
    res["v6_since"] = v6_since
    return res, meta

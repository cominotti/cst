# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-monitord's sampling loop: probes, samples, episodes, hung and busy tasks, kernel log, alerts."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
import errno
import json
import os
import queue
import re
import select
import signal
import subprocess
import time

from cst_core.adapters.files import write_atomic
from cst_core.domain.contracts import state_format
from btrfs.stall_monitord.adapters import (
    CGROUP_ROOT,
    Topology,
    Writer,
    d_comm,
    log,
    parse_io_stat,
    parse_psi,
    read_int,
    read_kv,
    read_text,
    run_slices,
    scan_d_tasks,
    short_cgroup,
)
from btrfs.stall_monitord.domain import (
    ALERT_COMMIT_MS,
    ALERT_MIN_GAP,
    BASE_INTERVAL,
    BUSY_ALERT_SECONDS,
    ENTER_COMMIT_MS,
    ENTER_D_TASKS,
    ENTER_IO_SOME_PCT,
    EPISODE_INTERVAL,
    Episode,
    FLUSH_EVERY,
    HUNG_REPEAT,
    HUNG_SECONDS,
    KMSG_DEVICE,
    KMSG_INTEREST,
    KMSG_SEVERE,
    LOCAL_IO_FRAME,
    MAINT_HUNG_SECONDS,
    MAX_EXAMPLE_STACKS,
    MAX_STACK_SNAPSHOTS,
    PROBE_INTERVAL,
    PSI_TRIGGER,
    QUIET_D_TASKS,
    QUIET_IO_SOME_PCT,
    QUIET_SECONDS_TO_CLOSE,
    SLICES,
    SPACE_KEYS,
    TIMED_WAIT_FRAME,
    VERSION,
    VMSTAT_KEYS,
    DTaskSince,
    mmap_lock_only,
    parse_stack,
    stack_signature,
    wait_kind,
)


class Monitor:
    def __init__(self, log_dir: str, run_dir: str, duration: float | None = None) -> None:
        self.log_dir = log_dir
        self.run_dir = run_dir
        self.duration = duration
        self.topo = Topology()
        self.writer = Writer(log_dir)
        self.buffer: list[tuple[float, str]] = []
        self.last_flush = time.monotonic()
        self.prev: dict[str, Any] | None = None
        self.episode: Episode | None = None
        self.cmax: dict[str, int] = {}  # uuid -> longest commit (ms) seen since the last sample
        self.prev_commit: dict[
            str, tuple[int, int, int]
        ] = {}  # uuid -> (commits, total_ms, max_commit_ms) at last probe
        self.commit_alerted: dict[str, int] = {}  # uuid -> commits count already alerted for
        self.want_episode: str | None = None  # reason set by the probe; sampled right away
        self.last_alert: dict[str, float] = {}
        self.stack_ok = self.probe_stack_access()
        self.stack_fail = 0
        self.d_since: dict[str, DTaskSince] = {}  # tid -> {first (stuck since), busy (in D since), comm, pid, slices}
        self.hung_reported: dict[tuple[str, str], float] = {}  # (pid, kind) -> time of the last report
        self.kmsg_fd: int | None = None
        self.psi_fd: int | None = None
        self.running = True
        self.poller = select.poll()
        self.boot_id = (read_text("/proc/sys/kernel/random/boot_id") or "").strip()
        self.kernel = os.uname().release

    @staticmethod
    def probe_stack_access() -> bool:
        # PID 1's stack needs exactly what other tasks' stacks need.
        try:
            with open("/proc/1/stack", "rb") as f:
                f.read()
            return True
        except OSError:
            return False

    # ---- setup -------------------------------------------------------------

    def open_psi_trigger(self) -> None:
        try:
            fd = os.open("/proc/pressure/io", os.O_RDWR | os.O_NONBLOCK)
            os.write(fd, PSI_TRIGGER + b"\0")
        except OSError as e:
            log(f"PSI trigger unavailable ({e}); falling back to timed sampling only", 4)
            return
        self.psi_fd = fd
        self.poller.register(fd, select.POLLPRI)

    def open_kmsg(self) -> None:
        try:
            fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK)
            os.lseek(fd, 0, os.SEEK_END)
        except OSError as e:
            log(f"/dev/kmsg unavailable ({e}); kernel errors will not be recorded", 4)
            return
        self.kmsg_fd = fd
        self.poller.register(fd, select.POLLIN)

    # ---- storage -----------------------------------------------------------

    def emit(self, rec: Mapping[str, Any]) -> None:
        self.buffer.append((rec.get("ts", time.time()), json.dumps(rec, separators=(",", ":"))))

    def meta_record(self) -> dict[str, Any]:
        wbt = {
            n: read_int(f"/sys/block/{n}/queue/wbt_lat_usec")
            for n in self.topo.disks
            if os.path.exists(f"/sys/block/{n}/queue/wbt_lat_usec")
        }
        return {
            "t": "meta",
            "ts": round(time.time(), 3),
            "v": VERSION,
            "kernel": self.kernel,
            "boot_id": self.boot_id,
            "base_interval": BASE_INTERVAL,
            "fs": self.topo.fs,
            "disks": self.topo.disks,
            "vm_keys": VMSTAT_KEYS,
            "space_keys": SPACE_KEYS,
            "dirty_bytes": read_int("/proc/sys/vm/dirty_bytes"),
            "dirty_background_bytes": read_int("/proc/sys/vm/dirty_background_bytes"),
            "wbt_lat_usec": wbt,
            "stacks": self.stack_ok,
            "kmsg": self.kmsg_fd is not None,
            "hung_task_detector": os.path.exists("/proc/sys/kernel/hung_task_timeout_secs"),
            "hung_seconds": HUNG_SECONDS,
        }

    def flush(self) -> None:
        if not self.buffer:
            return
        try:
            self.writer.q.put_nowait(self.buffer)
            self.buffer = []
        except queue.Full:
            # Writer is stuck (filesystem stalled); keep the newest data only.
            if len(self.buffer) > 20000:
                self.buffer = self.buffer[-10000:]
        self.last_flush = time.monotonic()

    # ---- sampling ----------------------------------------------------------

    def probe_commits(self) -> None:
        """Every second: catch long commits that 5 s samples could miss."""
        for uuid in self.topo.fs:
            cs = read_kv(f"/sys/fs/btrfs/{uuid}/commit_stats")
            if not cs:
                continue
            commits, total = cs.get("commits", 0), cs.get("total_commit_ms", 0)
            cur, last, mx = cs.get("cur_commit_ms", 0), cs.get("last_commit_ms", 0), cs.get("max_commit_ms", 0)
            long_ms = cur
            prev = self.prev_commit.get(uuid)
            if prev and commits > prev[0]:
                # Only count last_commit_ms when a commit actually finished;
                # otherwise it repeats an old value forever.
                long_ms = max(long_ms, last)
                if total - prev[1] >= ENTER_COMMIT_MS and commits - prev[0] > 1:
                    # Several commits finished within one probe; their sum is a floor.
                    long_ms = max(long_ms, (total - prev[1]) // (commits - prev[0]))
                if prev[2] < mx:
                    long_ms = max(long_ms, mx)
            self.prev_commit[uuid] = (commits, total, mx)
            if long_ms > self.cmax.get(uuid, 0):
                self.cmax[uuid] = long_ms
            if long_ms >= ENTER_COMMIT_MS and self.episode is None:
                self.want_episode = f"commit {long_ms} ms"
            if long_ms >= ALERT_COMMIT_MS and self.commit_alerted.get(uuid) != commits:
                self.commit_alerted[uuid] = commits
                label = self.topo.fs[uuid]["label"] or uuid[:8]
                state = "still running after" if cur >= ALERT_COMMIT_MS else "took"
                self.alert(
                    "commit",
                    "warning",
                    f"btrfs commit stall on {label}",
                    f"A transaction commit {state} {long_ms / 1000:.1f} s. "
                    f"Ask an agent or run: btrfs-stall-report --since '10 min ago'",
                )

    def sample(self, d_tasks: Sequence[tuple[str, str, str, list[str], str | None, str]]) -> dict[str, Any]:
        rec: dict[str, Any] = {"t": "s", "ts": round(time.time(), 3), "mono": round(time.monotonic(), 3)}
        rec["psi"] = {r: parse_psi(f"/proc/pressure/{r}") for r in ("io", "memory", "cpu")}
        vm = read_kv("/proc/vmstat")
        rec["vm"] = [vm.get(k, 0) for k in VMSTAT_KEYS]
        st = read_kv("/proc/stat")
        # procs_blocked counts tasks in iowait only; "d" counts every D-state task.
        rec["procs"] = [st.get("procs_running", 0), st.get("procs_blocked", 0)]
        # "d": tasks waiting on storage; "d_all" also counts FUSE, maintenance,
        # memory and idle-ish waits.
        rec["d"] = sum(1 for t in d_tasks if t[5] == "storage")
        rec["d_all"] = len(d_tasks)
        rec["d_fuse"] = sum(1 for t in d_tasks if t[5] == "fuse")
        rec["d_maint"] = sum(1 for t in d_tasks if t[5] == "maintenance")
        rec["d_mem"] = sum(1 for t in d_tasks if t[5] == "memory")
        rec["load1"] = float((read_text("/proc/loadavg") or "0").split()[0])

        fs = {}
        for uuid, info in self.topo.fs.items():
            base = f"/sys/fs/btrfs/{uuid}"
            cs = read_kv(f"{base}/commit_stats")
            errs = {devid: sum(read_kv(f"{base}/devinfo/{devid}/error_stats").values()) for devid in info["devids"]}
            space = {
                kind: [read_int(f"{base}/allocation/{kind}/{k}") for k in SPACE_KEYS] for kind in ("data", "metadata")
            }
            space["global_rsv"] = [
                read_int(f"{base}/allocation/global_rsv_size"),
                read_int(f"{base}/allocation/global_rsv_reserved"),
            ]
            disc = [read_int(f"{base}/discard/discardable_bytes"), read_int(f"{base}/discard/discardable_extents")]
            fs[uuid] = {
                "c": [
                    cs.get("commits", 0),
                    cs.get("total_commit_ms", 0),
                    cs.get("cur_commit_ms", 0),
                    cs.get("last_commit_ms", 0),
                    cs.get("max_commit_ms", 0),
                    max(self.cmax.pop(uuid, 0), cs.get("cur_commit_ms", 0)),
                ],
                "err": errs,
                "space": space,
                "discard": disc,
            }
        rec["fs"] = fs

        disks = {}
        for line in (read_text("/proc/diskstats") or "").splitlines():
            f = line.split()
            if len(f) >= 14 and f[2] in self.topo.disks:
                disks[f[2]] = [int(x) for x in f[3:]]
        rec["disk"] = disks

        cg: dict[str, list[float | int]] = {}
        for sl in SLICES:
            d = f"{CGROUP_ROOT}/{sl}"
            p = parse_psi(f"{d}/io.pressure")
            if p is None:
                continue
            m = parse_psi(f"{d}/memory.pressure")
            cg[sl] = [
                p[1],
                p[3],
                m[1] if m else 0,
                m[3] if m else 0,
                *(parse_io_stat(f"{d}/io.stat", self.topo.top_devs) or [0, 0]),
            ]
        self.topo.scan_cgroups(time.monotonic())
        for name, d in self.topo.containers.items():
            p = parse_psi(f"{d}/io.pressure")
            if p is not None:
                cg[name] = [p[1], p[3], 0, 0, *(parse_io_stat(f"{d}/io.stat", self.topo.top_devs) or [0, 0])]
        rec["cg"] = cg
        return rec

    def interval_io_some_pct(self, rec: Mapping[str, Any]) -> float:
        if not self.prev or not rec["psi"]["io"] or not self.prev["psi"]["io"]:
            return 0.0
        dt = rec["mono"] - self.prev["mono"]
        if dt <= 0:
            return 0.0
        return (rec["psi"]["io"][1] - self.prev["psi"]["io"][1]) / 1e6 / dt * 100

    # ---- D-state tasks -----------------------------------------------------

    def read_stack(self, pid: str | int, tid: str | int) -> list[str]:
        if not self.stack_ok:
            return []
        try:
            with open(f"/proc/{pid}/task/{tid}/stack", "rb") as f:
                frames = parse_stack(f.read().decode("utf-8", "replace"))
            self.stack_fail = 0
            return frames
        except PermissionError:
            # One non-dumpable task is no reason to stop; many in a row are.
            self.stack_fail += 1
            if self.stack_fail >= 50:
                self.stack_ok = False
                log("kernel stacks became unreadable; using wchan only", 4)
            return []
        except OSError:
            return []

    def describe(self, pid: str | int, tid: str | int) -> tuple[list[str], str | None]:
        frames = self.read_stack(pid, tid)
        wchan = None
        if not frames:
            wchan = (read_text(f"/proc/{pid}/task/{tid}/wchan") or "").strip()
            wchan = None if wchan in ("", "0") else wchan
        return frames, wchan

    def describe_all(
        self, d_tasks: Sequence[tuple[str, str, str]]
    ) -> list[tuple[str, str, str, list[str], str | None, str]]:
        """[(pid, tid, comm, frames, wchan, kind)] of tasks still in D state
        after their stack was read (a task that woke up in between shows the
        stack of whatever it sleeps in next, e.g. a futex)."""
        out: list[tuple[str, str, str, list[str], str | None, str]] = []
        for pid, tid, comm in d_tasks:
            frames, wchan = self.describe(pid, tid)
            if not (frames or wchan) or d_comm(pid, tid) is None:
                continue
            out.append((pid, tid, comm, frames, wchan, wait_kind(frames, wchan)))
        # Threads of a process that also has a thread inside FUSE, blocked on a
        # VFS lock that thread holds (d_alloc_parallel, inode rwsem), wait on
        # FUSE too, unless their stack shows the local block layer or btrfs.
        fuse_pids = {t[0] for t in out if t[5] == "fuse"}
        for i, t in enumerate(out):
            if t[5] == "storage" and t[0] in fuse_pids and not any(LOCAL_IO_FRAME.match(f) for f in t[3]):
                out[i] = (t[0], t[1], t[2], t[3], t[4], "fuse")
        # A thread waiting for the memory map lock waits for whatever the
        # holder (another thread of the process) waits on: memory if a thread
        # of the process is in reclaim or swap itself (a JVM whose threads
        # fault through zswap, even with one thread in fsync), else storage or
        # FUSE if a thread of the process waits there, memory otherwise.
        lock_only = [i for i, t in enumerate(out) if t[5] == "memory" and mmap_lock_only(t[3], t[4])]
        lock_tids = {out[i][1] for i in lock_only}
        mem_pids = {t[0] for t in out if t[5] == "memory" and t[1] not in lock_tids}
        storage_pids = {t[0] for t in out if t[5] == "storage"}
        fuse_pids = {t[0] for t in out if t[5] == "fuse"}
        for i in lock_only:
            t = out[i]
            if t[0] in mem_pids:
                continue
            if t[0] in storage_pids:
                out[i] = (t[0], t[1], t[2], t[3], t[4], "storage")
            elif t[0] in fuse_pids:
                out[i] = (t[0], t[1], t[2], t[3], t[4], "fuse")
        return out

    def snapshot_stacks(self, ep: Episode, d_tasks: Sequence[tuple[str, str, str, list[str], str | None, str]]) -> None:
        ep.snapshots += 1
        ep.max_d = max(ep.max_d, sum(1 for t in d_tasks if t[5] == "storage"))
        ep.max_d_mem = max(ep.max_d_mem, sum(1 for t in d_tasks if t[5] == "memory"))
        ep.max_d_all = max(ep.max_d_all, len(d_tasks))
        if ep.snapshots > MAX_STACK_SNAPSHOTS:
            return
        cg_cache = {}
        for pid, _tid, comm, frames, wchan, kind in d_tasks:
            sig, ctx = stack_signature(frames, wchan)
            key = f"{sig}|{ctx}"
            if pid not in cg_cache:
                cg_cache[pid] = short_cgroup(pid)
            entry = ep.sigs.setdefault(key, {"n": 0, "kind": kind, "comms": {}, "cgroups": {}})
            entry["n"] += 1
            entry["comms"][comm] = entry["comms"].get(comm, 0) + 1
            entry["cgroups"][cg_cache[pid]] = entry["cgroups"].get(cg_cache[pid], 0) + 1
            ex = ep.examples.setdefault(key, [])
            if frames and len(ex) < MAX_EXAMPLE_STACKS and frames[:16] not in ex:
                ex.append(frames[:16])

    def track_hung(self, d_tasks: Sequence[tuple[str, str, str, list[str], str | None, str]], now: float) -> None:
        """Per task seen in D: 'busy' = in D at every sample since then;
        'first' = in D since then *without running in between* (stuck)."""
        current: dict[str, DTaskSince] = {}
        info: dict[str, tuple[list[str], str | None, str]] = {}
        for pid, tid, comm, frames, wchan, kind in d_tasks:
            prev = self.d_since.get(tid)
            slices = run_slices(pid, tid)
            if prev and prev["pid"] == pid:
                moved = (
                    slices is not None
                    and prev["slices"] is not None
                    and slices != prev["slices"]
                    and not any(TIMED_WAIT_FRAME.match(f) for f in frames or ())
                )
                first, busy = (now if moved else prev["first"]), prev["busy"]
            else:
                first = busy = now
            current[tid] = {"first": first, "busy": busy, "comm": comm, "pid": pid, "slices": slices}
            info[tid] = (frames, wchan, kind)
        self.d_since = current
        for tid, t in current.items():
            frames, wchan, kind = info[tid]
            comm, pid = t["comm"], t["pid"]
            if kind == "idle":
                continue
            # FUSE waits and the maintenance note keep the old meaning (time
            # in D, progressing or not: a slow network walk does progress);
            # the stuck alerts need time in D without progress.
            first = t["busy"] if kind == "fuse" else t["first"]
            if kind == "maintenance" and now - t["first"] < MAINT_HUNG_SECONDS:
                first = t["busy"]
            if now - first < HUNG_SECONDS:
                if kind == "storage" and now - t["busy"] >= HUNG_SECONDS:
                    self.report_io_busy(t, tid, frames, wchan, now)
                continue
            # btrfs maintenance: one informational event at HUNG_SECONDS, an
            # alert only after MAINT_HUNG_SECONDS (keyed separately so the
            # HUNG_REPEAT dedupe of the event does not delay the alert).
            # Workload tasks queued behind it are "storage" and alert as usual.
            key = kind
            if kind == "maintenance" and now - t["first"] >= MAINT_HUNG_SECONDS:
                key = "maintenance-hung"  # 15 min without any progress
            # One report per process and kind of wait (bfs/find run many
            # threads), so a FUSE wait never hides a disk hang in the same process.
            if now - self.hung_reported.get((pid, key), -1e9) < HUNG_REPEAT:
                continue
            self.hung_reported[(pid, key)] = now
            sig, ctx = stack_signature(frames, wchan)
            at = f"{sig} via {ctx}" if ctx else sig
            cmd = (read_text(f"/proc/{pid}/cmdline") or "").replace("\0", " ").strip()[:160]
            where = f"{comm}:{tid} ({cmd or 'no cmdline'}; cgroup {short_cgroup(pid)})"
            if key == "maintenance":
                msg = (
                    f"{where} in btrfs maintenance (trim/discard/scrub/balance) for {now - first:.0f}s "
                    f"at {at}: not a workload stall"
                )
                self.emit(
                    {
                        "t": "ev",
                        "ts": round(time.time(), 3),
                        "msg": msg,
                        "severe": False,
                        "ours": True,
                        "kind": "maintenance-wait",
                        "stack": frames[:16],
                    }
                )
                continue
            if key == "maintenance-hung":
                msg = f"{where} in btrfs maintenance, D state for {now - first:.0f}s at {at}"
                self.emit(
                    {
                        "t": "ev",
                        "ts": round(time.time(), 3),
                        "msg": msg,
                        "severe": True,
                        "ours": True,
                        "kind": "maintenance-hung",
                        "stack": frames[:16],
                    }
                )
                self.alert("maintenance-hung", "warning", "btrfs maintenance (fstrim/scrub) stuck for 15 min", msg)
                if self.episode is None:
                    self.start_episode(time.time(), "hung task")
                continue
            if kind == "fuse":
                msg = (
                    f"{where} waited {now - first:.0f}s on a FUSE filesystem (gvfs share or document "
                    f"portal): usually the network or the FUSE daemon, not the local disks (check the "
                    f"report for a storage wait at the same time); at {at}"
                )
                self.emit(
                    {
                        "t": "ev",
                        "ts": round(time.time(), 3),
                        "msg": msg,
                        "severe": False,
                        "ours": False,
                        "kind": "fuse-wait",
                        "stack": frames[:16],
                    }
                )
                self.alert("fuse", "normal", "Program waiting on a FUSE filesystem (network share / portal)", msg)
                continue
            if kind == "memory":
                # Not a disk or filesystem problem, so not severe and no
                # storage episode; the report shows it with the memory waits.
                msg = (
                    f"{where} waited {now - first:.0f}s without progress in memory reclaim / swap "
                    f"(too little free memory, not the disks) at {at}"
                )
                self.emit(
                    {
                        "t": "ev",
                        "ts": round(time.time(), 3),
                        "msg": msg,
                        "severe": False,
                        "ours": True,
                        "kind": "hung-memory",
                        "stack": frames[:16],
                    }
                )
                self.alert(
                    "hung-memory", "warning", "Task stuck in an uninterruptible wait (memory reclaim / swap)", msg
                )
                continue
            msg = f"{where} in D state for {now - first:.0f}s at {at}"
            self.emit(
                {
                    "t": "ev",
                    "ts": round(time.time(), 3),
                    "msg": msg,
                    "severe": True,
                    "ours": True,
                    "kind": "hung",
                    "stack": frames[:16],
                }
            )
            self.alert("hung", "warning", "Task stuck in an uninterruptible wait (likely storage)", msg)
            if self.episode is None:
                self.start_episode(time.time(), "hung task")
        # Forget processes that are gone.
        live = {v["pid"] for v in current.values()}
        self.hung_reported = {k: t for k, t in self.hung_reported.items() if k[0] in live or now - t < HUNG_REPEAT}

    def report_io_busy(self, t: DTaskSince, tid: str, frames: list[str], wchan: str | None, now: float) -> None:
        """In D at every sample for HUNG_SECONDS while still being scheduled
        (typically an fsync loop): an informational event; an alert once it
        lasts BUSY_ALERT_SECONDS (a device slowed to a crawl also completes
        some IO). Once per process and kind per HUNG_REPEAT."""
        pid = t["pid"]
        busy_s = now - t["busy"]
        key = "io-busy-long" if busy_s >= BUSY_ALERT_SECONDS else "io-busy"
        if now - self.hung_reported.get((pid, key), -1e9) < HUNG_REPEAT:
            return
        self.hung_reported[(pid, key)] = now
        sig, ctx = stack_signature(frames, wchan)
        at = f"{sig} via {ctx}" if ctx else sig
        cmd = (read_text(f"/proc/{pid}/cmdline") or "").replace("\0", " ").strip()[:160]
        msg = (
            f"{t['comm']}:{tid} ({cmd or 'no cmdline'}; cgroup {short_cgroup(pid)}) waited on storage at "
            f"every sample for {busy_s:.0f}s while still being scheduled (progressing, e.g. an fsync loop; "
            f"check the window's storage metrics) at {at}"
        )
        self.emit(
            {
                "t": "ev",
                "ts": round(time.time(), 3),
                "msg": msg,
                "severe": False,
                "comm": t["comm"],
                "ours": True,
                "kind": key,
                "stack": frames[:16],
            }
        )
        if key == "io-busy-long":
            self.alert("io-busy-long", "normal", "A program has waited on storage for 10+ minutes (progressing)", msg)

    # ---- episodes ----------------------------------------------------------

    def update_episode(
        self, rec: Mapping[str, Any], d_tasks: Sequence[tuple[str, str, str, list[str], str | None, str]]
    ) -> None:
        io_pct = self.interval_io_some_pct(rec)
        cmax = max((v["c"][5] for v in rec["fs"].values()), default=0)
        io = rec["psi"]["io"] or [0, 0, 0, 0]
        if self.episode is None:
            reason = self.want_episode
            if io_pct >= ENTER_IO_SOME_PCT:
                reason = f"io_some {io_pct:.0f}%"
            elif rec["d"] >= ENTER_D_TASKS:
                reason = f"{rec['d']} tasks waiting on storage"
            elif cmax >= ENTER_COMMIT_MS:
                reason = f"commit {cmax} ms"
            if reason:
                self.start_episode(rec["ts"], reason)
        self.want_episode = None
        ep = self.episode
        if ep is None:
            return
        ep.end = rec["ts"]
        ep.peak["io_some_avg10"] = max(ep.peak["io_some_avg10"], io[0])
        ep.peak["io_full_avg10"] = max(ep.peak["io_full_avg10"], io[2])
        ep.peak["iowait_tasks"] = max(ep.peak["iowait_tasks"], rec["procs"][1])
        ep.peak["commit_ms"] = max(ep.peak["commit_ms"], cmax)
        self.snapshot_stacks(ep, d_tasks)
        cur_commit = max((v["c"][2] for v in rec["fs"].values()), default=0)
        if io_pct < QUIET_IO_SOME_PCT and rec["d"] <= QUIET_D_TASKS and cur_commit < ENTER_COMMIT_MS // 2:
            ep.quiet += 1
        else:
            ep.quiet = 0
        if ep.quiet >= QUIET_SECONDS_TO_CLOSE:
            self.end_episode()

    def start_episode(self, ts: float, reason: str) -> None:
        self.episode = Episode(ts, reason)

    def end_episode(self) -> None:
        ep, self.episode = self.episode, None
        if ep is None:
            return
        # PSI triggers also fire on harmless 200 ms blips; keep only episodes
        # that show an actual stall (the 1 s samples are stored either way).
        if (
            ep.peak["io_some_avg10"] < QUIET_IO_SOME_PCT
            and ep.max_d < ENTER_D_TASKS
            and ep.peak["commit_ms"] < ENTER_COMMIT_MS
            and ep.reason != "hung task"
        ):
            return
        rec = ep.to_record()
        self.emit(rec)
        top = rec["sigs"][:1]
        top_s = f"{top[0]['sig']} x{top[0]['n']}" if top else "no D tasks"
        log(
            f"episode {ep.end - ep.start:.0f}s ({ep.reason}); peak io_full_avg10="
            f"{ep.peak['io_full_avg10']:.0f}% commit={ep.peak['commit_ms']}ms max_D={ep.max_d}; top: {top_s}"
        )
        self.flush()

    # ---- kernel log ----------------------------------------------------------

    def drain_kmsg(self) -> None:
        if self.kmsg_fd is None:
            return
        members = set(self.topo.disks)
        while True:
            try:
                data = os.read(self.kmsg_fd, 8192)
            except BlockingIOError:
                return
            except OSError as e:
                if e.errno == errno.EPIPE:  # ring buffer overran us; the kernel moved us forward
                    continue
                return
            if not data:
                return
            msg = data.decode("utf-8", "replace").partition(";")[2].split("\n", 1)[0]
            if not KMSG_INTEREST.search(msg):
                continue
            m = KMSG_DEVICE.search(msg)
            dev = (m.group(1) or m.group(2)) if m else None
            ours = dev is None or dev in members or re.sub(r"p\d+$", "", dev) in members
            severe = bool(KMSG_SEVERE.search(msg)) and ours
            self.emit(
                {
                    "t": "ev",
                    "ts": round(time.time(), 3),
                    "msg": msg[:400],
                    "severe": severe,
                    "ours": ours,
                    "kind": "kmsg",
                }
            )
            if severe:
                self.alert("fs-error", "crit", "Kernel: btrfs/device error", msg[:300])

    # ---- alerts --------------------------------------------------------------

    def alert(self, kind: str, level: str, title: str, body: str) -> None:
        now = time.monotonic()
        if now - self.last_alert.get(kind, -1e9) < ALERT_MIN_GAP:
            return
        self.last_alert[kind] = now
        self.emit({"t": "al", "ts": round(time.time(), 3), "kind": kind, "level": level, "title": title, "body": body})
        log(f"ALERT {title}: {body}", 2 if level == "crit" else 4)
        if not self.run_dir:
            return
        try:
            # Plain-text queue for /usr/local/sbin/btrfs-notify --queue (the stall-queue format):
            # line 1 urgency, line 2 title, rest body.
            path = os.path.join(self.run_dir, state_format("stall-queue").file_name.replace("*", str(time.time_ns())))
            text = f"{'critical' if level == 'crit' else 'normal'}\n{title.replace(chr(10), ' ')}\n{body}\n"
            write_atomic(path, text.encode("utf-8"), mode="umask", durable=False, relabel=False)
            subprocess.Popen(
                ["systemctl", "--no-block", "start", "btrfs-stall-alert.service"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            log(f"alert hand-off failed: {e}", 4)

    # ---- main loop -----------------------------------------------------------

    def run(self) -> None:
        self.writer.start()
        self.open_psi_trigger()
        self.open_kmsg()
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.emit(self.meta_record())
        log(
            f"started: kernel {self.kernel}, fs {[v['label'] or k[:8] for k, v in self.topo.fs.items()]}, "
            f"disks {self.topo.disks}, psi_trigger={'yes' if self.psi_fd else 'no'}, "
            f"kmsg={'yes' if self.kmsg_fd else 'no'}, stacks={'yes' if self.stack_ok else 'no (wchan only)'}, "
            f"dir={self.log_dir}"
        )
        t0 = time.monotonic()
        next_sample = next_probe = t0
        day = time.strftime("%Y-%m-%d")
        while self.running:
            now = time.monotonic()
            if self.duration and now - t0 >= self.duration:
                break
            try:
                events = self.poller.poll(max(0.0, (min(next_sample, next_probe) - now) * 1000))
            except InterruptedError:
                events = []
            triggered = False
            for fd, ev in events:
                if fd == self.psi_fd:
                    if ev & select.POLLERR:
                        self.poller.unregister(fd)
                        os.close(fd)
                        self.psi_fd = None
                        log("PSI trigger closed by kernel; re-arming", 4)
                        self.open_psi_trigger()
                    else:
                        triggered = True
                elif fd == self.kmsg_fd:
                    self.drain_kmsg()
            now = time.monotonic()
            if now >= next_probe:
                self.probe_commits()
                next_probe = now + PROBE_INTERVAL
            if self.episode is None and (triggered or self.want_episode):
                if triggered and not self.want_episode:
                    self.want_episode = "psi trigger"
                next_sample = now
            if now >= next_sample:
                d_tasks = self.describe_all(scan_d_tasks())
                rec = self.sample(d_tasks)
                self.emit(rec)
                self.update_episode(rec, d_tasks)
                self.track_hung(d_tasks, time.time())
                self.prev = rec
                next_sample = now + (EPISODE_INTERVAL if self.episode else BASE_INTERVAL)
                today = time.strftime("%Y-%m-%d")
                if today != day:
                    day = today
                    self.emit(self.meta_record())
            if time.monotonic() - self.last_flush >= FLUSH_EVERY:
                self.flush()
        if self.episode is not None:
            self.end_episode()
        self.flush()
        self.writer.q.put(None)
        self.writer.join(timeout=10)
        log("stopped")

    def stop(self, *_: object) -> None:
        self.running = False

# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-monitord's daemon (Monitor): its state across samples and its sampling loop, the PSI trigger, /dev/kmsg, the records it hands to the writer and the alert hand-off."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import errno
import json
import os
import queue
import select
import signal
import subprocess
import threading
import time
from typing import Protocol

from btrfs.stall_monitord.domain import (
    ALERT_COMMIT_MS,
    ALERT_MIN_GAP,
    ALERT_START_WAIT,
    BASE_INTERVAL,
    CMDLINE_SLOW,
    CMDLINE_WAIT,
    ENTER_COMMIT_MS,
    EPISODE_INTERVAL,
    FLUSH_EVERY,
    HUNG_REPEAT,
    HUNG_SECONDS,
    HUNG_TASK,
    PROBE_INTERVAL,
    PSI_TRIGGER,
    SLICES,
    SPACE_KEYS,
    VERSION,
    StallRecordTag,
    VMSTAT_KEYS,
    DTask,
    DTaskSince,
    FsTopo,
    Episode,
    FsSampleOut,
    MetaOut,
    RecordOut,
    SampleOut,
    alert_text,
    cmdline_text,
    commit_alert,
    commit_long_ms,
    episode_reason,
    hung_key,
    hung_report,
    hung_start,
    io_busy_key,
    io_busy_message,
    io_some_pct,
    kmsg_line,
    parse_stack,
    settle_kinds,
    stuck_since,
    task_where,
    wait_at,
    wait_counts,
    wait_kind,
    WaitKind,
)
from btrfs.stall_monitord.adapters.proc import (
    CGROUP_ROOT,
    TimedReader,
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
from cst_core.adapters.files import write_atomic
from cst_core.adapters.sd_notify import ServiceNotifier
from cst_core.domain.contracts import StateFormatKey, state_format


class _Started(Protocol):
    """What AlertStarter needs of the program it started (a subprocess.Popen; a fake in the tests)."""

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


def _start_quietly(argv: Sequence[str]) -> _Started:
    return subprocess.Popen(list(argv), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class AlertStarter:
    """Starts a program (`systemctl --no-block start btrfs-stall-alert.service`) from one helper thread, so the
    sampling loop never waits for it.

    Starting a program waits until the child has exec'd it, and exec reads the program from the disk: during
    the stalls the daemon records, that can take minutes. request() only marks a start as wanted and returns.
    The helper starts the program once for every request still pending when it gets to it (requests made
    meanwhile are merged: the service sends every queued alert at each start). Once the program runs, the
    helper waits at most `wait` seconds for it to end, then kills it and waits for the kill to take effect,
    so the child is reaped (no zombie) and there is never more than one child at a time. A non-zero exit is
    logged. One thread, never more (the unit's TasksMax).
    """

    def __init__(
        self,
        argv: Sequence[str],
        start: Callable[[Sequence[str]], _Started] = _start_quietly,
        wait: float = ALERT_START_WAIT,
    ) -> None:
        self.argv = list(argv)
        self._start = start
        self._wait = wait
        self._wanted = threading.Event()
        self._thread: threading.Thread | None = None

    def request(self) -> None:
        """Ask for one more start; returns at once."""
        self._wanted.set()
        if self._thread is not None and self._thread.is_alive():
            return
        thread = threading.Thread(target=self._serve, name="alert-start", daemon=True)
        try:
            thread.start()
        except RuntimeError as e:  # no thread (the unit's TasksMax reached): the alert stays queued
            log(f"alert hand-off: {self.argv[-1]} not started ({e}); the alert stays queued", 4)
            return
        self._thread = thread

    def _serve(self) -> None:
        while True:
            self._wanted.wait()
            self._wanted.clear()
            try:
                child = self._start(self.argv)
            except OSError as e:
                log(f"alert hand-off failed: {e}", 4)
                continue
            try:
                rc = child.wait(timeout=self._wait)
            except subprocess.TimeoutExpired:
                child.kill()
                log(f"alert hand-off: {' '.join(self.argv)} did not end in {self._wait:g} s; killed", 4)
                child.wait()  # reaped before the next start: one child at a time, no zombie under TasksMax
                continue
            if rc != 0:
                log(f"alert hand-off: {' '.join(self.argv)} exited with status {rc}", 4)


class Monitor:
    """The daemon: its state across samples, the probes, samples, episodes, hung and busy tasks, the kernel log and the alerts."""

    def __init__(
        self, log_dir: str, run_dir: str, duration: float | None = None, notifier: ServiceNotifier | None = None
    ) -> None:
        self.log_dir = log_dir
        # systemd's Type=notify: READY=1 once started, WATCHDOG=1 at every pass of the loop (WatchdogSec=).
        self.notifier = notifier if notifier is not None else ServiceNotifier(None)
        self.ready_sent = False  # READY=1 reached systemd (signs_of_life sends it again until it does)
        self.run_dir = run_dir
        self.duration = duration
        self.topo = Topology()
        self.writer = Writer(log_dir)
        self.buffer: list[tuple[float, str]] = []
        self.last_flush = time.monotonic()
        self.prev: SampleOut | None = None
        self.episode: Episode | None = None
        self.cmax: dict[str, int] = {}  # uuid -> longest commit (ms) seen since the last sample
        self.prev_commit: dict[
            str, tuple[int, int, int]
        ] = {}  # uuid -> (commits, total_ms, max_commit_ms) at last probe
        self.commit_alerted: dict[str, int] = {}  # uuid -> commits count already alerted for
        self.want_episode: str | None = None  # reason set by the probe; sampled right away
        self.last_alert: dict[str, float] = {}
        # btrfs-stall-alert.service sends the queued alerts; started from a helper thread (AlertStarter).
        self.alert_start = AlertStarter(["systemctl", "--no-block", "start", "btrfs-stall-alert.service"])
        self.stack_ok = self.probe_stack_access()
        self.stack_fail = 0
        self.d_since: dict[str, DTaskSince] = {}  # tid -> {first (stuck since), busy (in D since), comm, pid, slices}
        self.hung_reported: dict[tuple[str, str], float] = {}  # (pid, kind) -> time of the last report
        # /proc/PID/cmdline of a reported task, read in a helper thread: it can wait minutes for that program.
        self.cmdlines = TimedReader(CMDLINE_WAIT)
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

    def emit(self, rec: RecordOut) -> None:
        self.buffer.append((rec["ts"], json.dumps(rec, separators=(",", ":"))))

    def meta_record(self) -> MetaOut:
        wbt = {
            n: read_int(f"/sys/block/{n}/queue/wbt_lat_usec")
            for n in self.topo.disks
            if os.path.exists(f"/sys/block/{n}/queue/wbt_lat_usec")
        }
        return {
            "t": StallRecordTag.META,
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
            commits = cs.get("commits", 0)
            long_ms = commit_long_ms(cs, self.prev_commit.get(uuid))
            self.prev_commit[uuid] = (commits, cs.get("total_commit_ms", 0), cs.get("max_commit_ms", 0))
            if long_ms > self.cmax.get(uuid, 0):
                self.cmax[uuid] = long_ms
            if long_ms >= ENTER_COMMIT_MS and self.episode is None:
                self.want_episode = f"commit {long_ms} ms"
            if long_ms >= ALERT_COMMIT_MS and self.commit_alerted.get(uuid) != commits:
                self.commit_alerted[uuid] = commits
                label = self.topo.fs[uuid]["label"] or uuid[:8]
                title, body = commit_alert(label, cs.get("cur_commit_ms", 0), long_ms)
                self.alert("commit", "warning", title, body)

    def sample(self, d_tasks: Sequence[DTask]) -> SampleOut:
        ts, mono = round(time.time(), 3), round(time.monotonic(), 3)
        psi = {r: parse_psi(f"/proc/pressure/{r}") for r in ("io", "memory", "cpu")}
        vm = read_kv("/proc/vmstat")
        st = read_kv("/proc/stat")
        counts = wait_counts(d_tasks)
        load1 = float((read_text("/proc/loadavg") or "0").split()[0])

        return {
            "t": StallRecordTag.SAMPLE,
            "ts": ts,
            "mono": mono,
            "psi": psi,
            "vm": [vm.get(k, 0) for k in VMSTAT_KEYS],
            # procs_blocked counts tasks in iowait only; "d" counts every D-state task.
            "procs": [st.get("procs_running", 0), st.get("procs_blocked", 0)],
            "d": counts.d,
            "d_all": counts.d_all,
            "d_fuse": counts.d_fuse,
            "d_maint": counts.d_maint,
            "d_mem": counts.d_mem,
            "load1": load1,
            "fs": {uuid: self._fs_sample(uuid, info) for uuid, info in self.topo.fs.items()},
            "disk": self._disk_sample(),
            "cg": self._cgroup_sample(),
        }

    def _fs_sample(self, uuid: str, info: FsTopo) -> FsSampleOut:
        """One btrfs filesystem's figures: commit stats (with the longest commit seen since the last
        sample, taken from cmax), device errors, space and discard."""
        base = f"/sys/fs/btrfs/{uuid}"
        cs = read_kv(f"{base}/commit_stats")
        errs = {devid: sum(read_kv(f"{base}/devinfo/{devid}/error_stats").values()) for devid in info["devids"]}
        space = {kind: [read_int(f"{base}/allocation/{kind}/{k}") for k in SPACE_KEYS] for kind in ("data", "metadata")}
        space["global_rsv"] = [
            read_int(f"{base}/allocation/global_rsv_size"),
            read_int(f"{base}/allocation/global_rsv_reserved"),
        ]
        disc = [read_int(f"{base}/discard/discardable_bytes"), read_int(f"{base}/discard/discardable_extents")]
        return {
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

    def _disk_sample(self) -> dict[str, list[int]]:
        """/proc/diskstats' counters of the topology's disks."""
        disks = {}
        for line in (read_text("/proc/diskstats") or "").splitlines():
            f = line.split()
            if len(f) >= 14 and f[2] in self.topo.disks:
                disks[f[2]] = [int(x) for x in f[3:]]
        return disks

    def _cgroup_sample(self) -> dict[str, list[float | int]]:
        """IO and memory pressure and IO bytes of the slices, then IO pressure and bytes of the containers."""
        cg: dict[str, list[float | int]] = {}
        for sl in SLICES:
            path = f"{CGROUP_ROOT}/{sl}"
            p = parse_psi(f"{path}/io.pressure")
            if p is None:
                continue
            m = parse_psi(f"{path}/memory.pressure")
            cg[sl] = [
                p[1],
                p[3],
                m[1] if m is not None else 0,
                m[3] if m is not None else 0,
                *(parse_io_stat(f"{path}/io.stat", self.topo.top_devs) or [0, 0]),
            ]
        self.topo.scan_cgroups(time.monotonic())
        for name, path in self.topo.containers.items():
            p = parse_psi(f"{path}/io.pressure")
            if p is not None:
                cg[name] = [p[1], p[3], 0, 0, *(parse_io_stat(f"{path}/io.stat", self.topo.top_devs) or [0, 0])]
        return cg

    def interval_io_some_pct(self, rec: SampleOut) -> float:
        return io_some_pct(self.prev, rec)

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

    def describe_all(self, d_tasks: Sequence[tuple[str, str, str]]) -> list[DTask]:
        """[(pid, tid, comm, frames, wchan, kind)] of tasks still in D state
        after their stack was read (a task that woke up in between shows the
        stack of whatever it sleeps in next, e.g. a futex), their kinds
        settled across the threads of each process (settle_kinds)."""
        out: list[DTask] = []
        for pid, tid, comm in d_tasks:
            frames, wchan = self.describe(pid, tid)
            if not (frames or (wchan is not None and wchan != "")) or d_comm(pid, tid) is None:
                continue
            out.append((pid, tid, comm, frames, wchan, wait_kind(frames, wchan)))
        return settle_kinds(out)

    def track_hung(self, d_tasks: Sequence[DTask], now: float) -> None:
        """Per task seen in D: 'busy' = in D at every sample since then;
        'first' = in D since then *without running in between* (stuck)."""
        current: dict[str, DTaskSince] = {}
        info: dict[str, tuple[list[str], str | None, str]] = {}
        for pid, tid, comm, frames, wchan, kind in d_tasks:
            slices = run_slices(pid, tid)
            first, busy = stuck_since(self.d_since.get(tid), pid, slices, frames, now)
            current[tid] = {"first": first, "busy": busy, "comm": comm, "pid": pid, "slices": slices}
            info[tid] = (frames, wchan, kind)
        self.d_since = current
        for tid, t in current.items():
            self._check_hung(tid, t, info[tid], now)
        # Forget processes that are gone.
        live = {v["pid"] for v in current.values()}
        self.hung_reported = {k: t for k, t in self.hung_reported.items() if k[0] in live or now - t < HUNG_REPEAT}

    def _check_hung(self, tid: str, t: DTaskSince, seen: tuple[list[str], str | None, str], now: float) -> None:
        """track_hung for one task (seen: its frames, wchan and kind): a busy wait, or a hung task reported
        once per HUNG_REPEAT, with its alert and the episode it opens."""
        frames, wchan, kind = seen
        comm, pid = t["comm"], t["pid"]
        if kind == WaitKind.IDLE:
            return
        first = hung_start(kind, t, now)
        if now - first < HUNG_SECONDS:
            if kind == WaitKind.STORAGE and now - t["busy"] >= HUNG_SECONDS:
                self.report_io_busy(t, tid, frames, wchan, now)
            return
        key = hung_key(kind, t, now)
        # One report per process and kind of wait (bfs/find run many
        # threads), so a FUSE wait never hides a disk hang in the same process.
        if now - self.hung_reported.get((pid, key), -1e9) < HUNG_REPEAT:
            return
        self.hung_reported[pid, key] = now
        at = wait_at(frames, wchan)
        rep = hung_report(key, self.where(pid, tid, comm), now - first, at)
        self.emit(
            {
                "t": StallRecordTag.EVENT,
                "ts": round(time.time(), 3),
                "msg": rep.msg,
                "severe": rep.severe,
                "ours": rep.ours,
                "kind": rep.kind,
                "stack": frames[:16],
            }
        )
        if rep.alert is not None:
            self.alert(*rep.alert, rep.msg)
        if rep.opens_episode and self.episode is None:
            self.start_episode(time.time(), HUNG_TASK)

    def where(self, pid: str, tid: str, comm: str) -> str:
        """The task as a report names it: comm:tid, its command line (read now, waiting at most CMDLINE_WAIT) and its cgroup."""
        try:
            cmd = cmdline_text(self.cmdlines.read(f"/proc/{pid}/cmdline"))
        except TimeoutError:
            cmd = CMDLINE_SLOW
        return task_where(comm, tid, cmd, short_cgroup(pid))

    def report_io_busy(self, t: DTaskSince, tid: str, frames: list[str], wchan: str | None, now: float) -> None:
        """In D at every sample for HUNG_SECONDS while still being scheduled
        (typically an fsync loop): an informational event; an alert once it
        lasts BUSY_ALERT_SECONDS (a device slowed to a crawl also completes
        some IO). Once per process and kind per HUNG_REPEAT."""
        pid = t["pid"]
        busy_s = now - t["busy"]
        key = io_busy_key(busy_s)
        if now - self.hung_reported.get((pid, key), -1e9) < HUNG_REPEAT:
            return
        self.hung_reported[pid, key] = now
        at = wait_at(frames, wchan)
        msg = io_busy_message(self.where(pid, tid, t["comm"]), busy_s, at)
        self.emit(
            {
                "t": StallRecordTag.EVENT,
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

    def update_episode(self, rec: SampleOut, d_tasks: Sequence[DTask]) -> None:
        io_pct = self.interval_io_some_pct(rec)
        cmax = max((v["c"][5] for v in rec["fs"].values()), default=0)
        io = rec["psi"]["io"] or [0, 0, 0, 0]
        if self.episode is None:
            reason = episode_reason(self.want_episode, io_pct, rec["d"], cmax)
            if reason is not None:
                self.start_episode(rec["ts"], reason)
        self.want_episode = None
        ep = self.episode
        if ep is None:
            return
        ep.observe(rec["ts"], io, rec["procs"][1], cmax)
        ep.add_snapshot(d_tasks, short_cgroup)
        cur_commit = max((v["c"][2] for v in rec["fs"].values()), default=0)
        if ep.quiet_step(io_pct, rec["d"], cur_commit):
            self.end_episode()

    def start_episode(self, ts: float, reason: str) -> None:
        self.episode = Episode(ts, reason)

    def end_episode(self) -> None:
        ep, self.episode = self.episode, None
        if ep is None or not ep.is_stall():
            return
        rec = ep.to_record()
        self.emit(rec)
        log(ep.summary(rec))
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
            line = kmsg_line(data, members)
            if line is None:
                continue
            self.emit(
                {
                    "t": StallRecordTag.EVENT,
                    "ts": round(time.time(), 3),
                    "msg": line.msg[:400],
                    "severe": line.severe,
                    "ours": line.ours,
                    "kind": "kmsg",
                }
            )
            if line.severe:
                self.alert("fs-error", "crit", "Kernel: btrfs/device error", line.msg[:300])

    # ---- alerts --------------------------------------------------------------

    def alert(self, kind: str, level: str, title: str, body: str) -> None:
        now = time.monotonic()
        if now - self.last_alert.get(kind, -1e9) < ALERT_MIN_GAP:
            return
        self.last_alert[kind] = now
        self.emit(
            {
                "t": StallRecordTag.ALERT,
                "ts": round(time.time(), 3),
                "kind": kind,
                "level": level,
                "title": title,
                "body": body,
            }
        )
        log(f"ALERT {title}: {body}", 2 if level == "crit" else 4)
        if not self.run_dir:
            return
        try:
            # Plain-text queue for /usr/local/sbin/btrfs-notify --queue (the stall-queue format, alert_text).
            path = os.path.join(
                self.run_dir, state_format(StateFormatKey.STALL_QUEUE).file_name.replace("*", str(time.time_ns()))
            )
            write_atomic(
                path, alert_text(level, title, body).encode("utf-8"), mode="umask", durable=False, relabel=False
            )
        except OSError as e:
            log(f"alert hand-off failed: {e}", 4)
            return
        self.alert_start.request()

    # ---- main loop -----------------------------------------------------------

    def run(self) -> None:
        """The daemon until SIGTERM, SIGINT or --duration; its notifier's socket is closed when it ends (or raises)."""
        with self.notifier:
            self._loop()

    def _loop(self) -> None:
        """run's body (call run, which closes the notifier when this ends)."""
        self.writer.start()
        self.open_psi_trigger()
        self.open_kmsg()
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        self.emit(self.meta_record())
        log(self._started_text())
        # The PSI trigger and /dev/kmsg are open and SIGTERM is handled: the unit becomes active now.
        # A READY=1 that could not be sent (systemd's queue full) is sent again at the next passes.
        self.ready_sent = self.notifier.ready()
        t0 = time.monotonic()
        next_sample = next_probe = t0
        day = time.strftime("%Y-%m-%d")
        while self.running:
            self.signs_of_life()
            now = time.monotonic()
            if self.duration is not None and now - t0 >= self.duration:
                break
            events = self._poll(max(0.0, (min(next_sample, next_probe) - now) * 1000))
            triggered = self._handle_events(events)
            now = time.monotonic()
            if now >= next_probe:
                self.probe_commits()
                next_probe = now + PROBE_INTERVAL
            self._name_psi_episode(triggered=triggered)
            if self._sample_wanted():
                next_sample = now
            if now >= next_sample:
                next_sample, day = self._take_sample(now, day)
            if time.monotonic() - self.last_flush >= FLUSH_EVERY:
                self.flush()
        if self.episode is not None:
            self.end_episode()
        self.flush()
        self.writer.q.put(None)
        self.writer.join(timeout=10)
        log("stopped")

    def _started_text(self) -> str:
        """The journal line of the start: kernel, filesystems, disks, what could be opened, the log folder."""
        return (
            f"started: kernel {self.kernel}, fs {[v['label'] or k[:8] for k, v in self.topo.fs.items()]}, "
            f"disks {self.topo.disks}, psi_trigger={'yes' if self.psi_fd is not None else 'no'}, "
            f"kmsg={'yes' if self.kmsg_fd is not None else 'no'}, stacks={'yes' if self.stack_ok else 'no (wchan only)'}, "
            f"dir={self.log_dir}"
        )

    def _poll(self, timeout_ms: float) -> list[tuple[int, int]]:
        """poll's events within timeout_ms; none when a signal interrupted it."""
        try:
            return self.poller.poll(timeout_ms)
        except InterruptedError:
            return []

    def _handle_events(self, events: Sequence[tuple[int, int]]) -> bool:
        """poll's events: a PSI trigger the kernel closed (POLLERR) is opened again, /dev/kmsg drained.
        True when the PSI trigger fired."""
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
        return triggered

    def _name_psi_episode(self, *, triggered: bool) -> None:
        """A fired PSI trigger asks for an episode, "psi trigger" its reason, when none is open and nothing
        else (a long commit) asked for one first."""
        if triggered and self.episode is None and self.want_episode is None:
            self.want_episode = "psi trigger"

    def _sample_wanted(self) -> bool:
        """Whether a sample is due at once: no episode open, and something asked for one."""
        return self.episode is None and self.want_episode is not None

    def _take_sample(self, now: float, day: str) -> tuple[float, str]:
        """One sample: the D-state tasks, the record, the episode and hung-task checks; the meta record
        again on a new day. Returns when the next sample is due, and the day."""
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
        return next_sample, day

    def signs_of_life(self) -> None:
        """WATCHDOG=1 for this pass of the loop, after the READY=1 when it could not be sent before.

        A pass that never ends (a deadlock, a read stuck for minutes) stops these, and systemd restarts the
        unit after WatchdogSec=: "active" then means "still sampling"."""
        self.ready_sent = self.ready_sent or self.notifier.ready()
        self.notifier.watchdog()

    def stop(self, *_: object) -> None:
        self.running = False

# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-report's record formats, no I/O: the window limits, the diskstats fields, the JSON units, what the daemon's records hold and what the analysis of one window returns."""

from dataclasses import dataclass, field
from typing import Final
from typing import NotRequired
from typing import Required
from typing import TypedDict
from typing import TypeIs

from cst_core.domain.shapes import KeyTypes, has_shape, json_object, number_or_none

GAP_MAX = 30.0  # s; a larger gap between samples means the daemon was not running
FRESH_MAX = 25.0  # s; newer data may still be buffered in the daemon
PAGE = 4096
# /proc/vmstat keys that daemons before v7 always stored as 0 (they sit after
# the first 4 KiB of the file, and those daemons read only one page).
ZSWAP_KEYS = ("zswpin", "zswpout", "zswpwb")
DISK_FIELDS = (
    "rd_ios",
    "rd_merges",
    "rd_sectors",
    "rd_ticks",
    "wr_ios",
    "wr_merges",
    "wr_sectors",
    "wr_ticks",
    "inflight",
    "io_ticks",
    "time_in_queue",
    "dc_ios",
    "dc_merges",
    "dc_sectors",
    "dc_ticks",
    "fl_ios",
    "fl_ticks",
)
DI = {k: i for i, k in enumerate(DISK_FIELDS)}
# The diskstats fields the report sums over a window (DiskTotals has one key each, in this order).
DISK_SUM: Final = (
    "rd_ios",
    "wr_ios",
    "rd_ticks",
    "wr_ticks",
    "rd_sectors",
    "wr_sectors",
    "io_ticks",
    "time_in_queue",
    "fl_ios",
    "fl_ticks",
)
UNITS = {
    "time": "epoch seconds",
    "*_pct": "percent of covered wall time",
    "*_bytes": "bytes",
    "*_ms": "milliseconds",
    "share": "fraction of sampled D-state task observations",
    "task_samples": "one task seen in D state in one sample (1 s apart during episodes, so roughly task-seconds)",
    "vm.zswap_in_bytes / zswap_out_bytes": "bytes decompressed from / compressed into the zswap pool "
    "(RAM, no disk IO); null when no sample came from a daemon >= v7",
    "vm.swap_in_bytes / swap_out_bytes": "bytes read from / written to the swap disk (pswpin/pswpout); "
    "swap-out includes zswap writeback",
    "vm.swap_out_bypass_bytes": "swap-out that did not pass through zswap (pswpout minus zswpwb, "
    "v7+ samples only); null like zswap_*",
    "peak.d_tasks / mem_tasks": "most tasks at once waiting on storage / on memory (reclaim, swap, zswap); "
    "for records of a daemon < v8 estimated from the episodes' stacks",
    "disks.*.read_await_ms / write_await_ms": "average time per read / write request",
    "sample_d_tasks": "most tasks waiting on storage in one sample (samples of a daemon >= v5)",
    "open_episode": "null, or an episode without a record yet: since / last (epoch s) and d_tasks "
    "(most tasks waiting on storage in its samples)",
    "episodes.*.max_d_mem": "most tasks waiting on memory at once in the episode",
    "episodes.*.estimated": "true when max_d / max_d_mem were estimated from the stacks "
    "(the episode was recorded by a daemon < v8)",
}


class FsInfo(TypedDict, total=False):
    """One btrfs filesystem in the meta record ("fs", by UUID)."""

    label: str  # empty when the filesystem has no label


class MetaRecord(TypedDict, total=False):
    """The meta record ("t": "meta"), written when the daemon starts. Every
    key is optional: summarise() uses {} when the window has no meta record,
    and reads every key with .get()."""

    ts: float
    v: int  # daemon version
    kernel: str
    base_interval: float  # s between full samples
    vm_keys: list[str]  # the /proc/vmstat names of each sample's "vm" list
    dirty_bytes: int | None  # vm.dirty_bytes; None when the daemon could not read it
    disks: dict[str, str]  # block device name -> role ("btrfs ...", "swap", ...)
    fs: dict[str, FsInfo]
    stacks: bool  # the daemon could read kernel stacks


class FsSample(TypedDict):
    """One btrfs filesystem in a sample ("fs", by UUID). Counters are raw."""

    c: list[int]  # commits, total ms, current ms, last ms, max ms, longest since the last sample
    err: dict[str, int]  # device id -> sum of its error counters
    space: NotRequired[dict[str, list[int]]]  # allocation kind -> SPACE_KEYS values


class SampleRecord(TypedDict):
    """A sample ("t": "s"): every 5 s, every second during an episode.
    Counters are cumulative; the report subtracts consecutive samples."""

    ts: float
    mono: NotRequired[float]  # monotonic clock
    psi: dict[str, list[float] | None]  # io/memory/cpu: some avg10, some total us, full avg10, full total us
    procs: list[int]  # running, blocked (iowait)
    vm: NotRequired[list[int]]  # values of the meta record's vm_keys
    # v3+: tasks waiting on storage (before v8 memory waits too, before v5
    # maintenance). Older daemons counted something else in "d"; the report
    # reads it only when "d_all" (v3+) is there.
    d: NotRequired[int]
    d_all: NotRequired[int]  # v3+: every D-state task
    d_maint: NotRequired[int]  # v5+: btrfs maintenance waits (not in "d")
    d_mem: NotRequired[int]  # v8+: memory waits (not in "d")
    fs: dict[str, FsSample]
    disk: dict[str, list[int]]  # /proc/diskstats fields (DISK_FIELDS order)
    cg: NotRequired[dict[str, list[int]]]  # io some/full us, memory some/full us, read/write bytes


class SigRecord(TypedDict):
    """One wait signature of an episode: where in the kernel tasks waited."""

    sig: str
    ctx: str
    n: int  # task-samples
    kind: NotRequired[str]  # v3+: storage, fuse, idle, maintenance (v5+), memory (v8+)
    comms: dict[str, int]  # command name -> task-samples
    cgroups: dict[str, int]
    examples: NotRequired[list[list[str]]]  # kernel stacks, innermost frame first


class _EpisodeBase(TypedDict):
    start: float
    end: float
    reason: str  # what opened it ("hung task", "commit 1200 ms", ...)
    max_d: int  # most tasks waiting on storage at once
    max_d_all: NotRequired[int]  # v3+: every D-state task
    max_d_mem: NotRequired[int]  # v8+: memory waits
    peak: dict[str, float]  # io_some_avg10, io_full_avg10, iowait_tasks, commit_ms


class EpisodeRecord(_EpisodeBase):
    """An episode record ("t": "ep"), written when a stall episode ends."""

    sigs: list[SigRecord]


class Sig(SigRecord):
    """A SigRecord with its category (classify())."""

    cat: str
    task_samples: int


class Episode(_EpisodeBase):
    """An EpisodeRecord as summarise() returns it."""

    sigs: list[Sig]
    estimated: NotRequired[bool]  # max_d / max_d_mem estimated from the stacks (daemon < v8)


class Notice(TypedDict, total=False):
    """An event ("t": "ev": kernel messages, hung or busy tasks) or an alert
    ("t": "al", the notification the daemon sent). One class for both: the
    same loop in summarise() rewrites the kind of each, and tells them apart by
    "severe", which only events have. "ts" is in both, so it is required.
    The report also reads "msg" (in every event) and "title" and "body" (in
    every alert) with [], but each is only in one of the two, so they stay
    NotRequired."""

    ts: Required[float]
    kind: str
    msg: str  # events
    severe: bool  # events
    stack: list[str]  # events of hung or busy tasks
    comm: str  # io-busy events (v6+)
    title: str  # alerts
    body: str  # alerts


class Window(TypedDict):
    since: float
    until: float


class Coverage(TypedDict):
    window_s: int
    covered_s: int
    first_sample: float | None
    last_sample: float | None
    complete: bool
    skipped: int  # damaged records and lines in the window, left out (load)
    gaps: NotRequired[int]  # set when the window has 2+ samples


class FsTotals(TypedDict):
    """One btrfs filesystem over the window."""

    commits: int
    commit_ms: int
    max_commit_ms: int
    max_at: float | None
    new_device_errors: int
    space: dict[str, list[int]] | None
    avg_commit_ms: NotRequired[float]


class CgroupTotals(TypedDict):
    """One cgroup over the window (pressure in us, wall in s)."""

    io_some: int
    io_full: int
    read_bytes: int
    write_bytes: int
    wall: float
    io_some_pct: NotRequired[float]
    io_full_pct: NotRequired[float]


class VmTotals(TypedDict):
    """Memory traffic over the window. None: no sample came from a v7+ daemon."""

    swap_in_bytes: int
    swap_out_bytes: int
    zswap_in_bytes: int | None
    zswap_out_bytes: int | None
    zswap_writeback_bytes: int | None
    swap_out_bypass_bytes: int | None
    zswap_counted_from: float | None
    direct_reclaim_scanned_pages: int
    allocstalls: int
    dirty_limit_bytes: int


class DiskTotals(TypedDict):
    """One block device over the window: the DISK_SUM counters summed (in that
    order), the busiest interval, then the figures derived from them."""

    rd_ios: int
    wr_ios: int
    rd_ticks: int
    wr_ticks: int
    rd_sectors: int
    wr_sectors: int
    io_ticks: int
    time_in_queue: int
    fl_ios: int
    fl_ticks: int
    peak_util_pct: float
    peak_at: float | None
    role: NotRequired[str]  # the meta record's role ("btrfs ...", "swap", ""): set with the derived figures
    read_bytes: NotRequired[int]
    write_bytes: NotRequired[int]
    await_ms: NotRequired[float]
    read_await_ms: NotRequired[float]
    write_await_ms: NotRequired[float]
    flush_await_ms: NotRequired[float]
    util_pct: NotRequired[float]
    queue_depth: NotRequired[float]


class CategoryShare(TypedDict):
    category: str
    description: str
    task_samples: int
    share: float


class OpenEpisode(TypedDict):
    since: float
    last: float
    d_tasks: int


class BelowItem(TypedDict):
    comm: str
    cgroup: str
    bytes: int


class BelowSection(TypedDict):
    top_writers: NotRequired[list[BelowItem] | None]
    top_readers: NotRequired[list[BelowItem] | None]
    error: str | None


class WindowPeak(TypedDict):
    """AnalyseResult's "peak": the highest PSI 10 s averages (%), the most tasks at once (in iowait, in
    D state on storage, on memory, in btrfs maintenance) and the most dirty and writeback bytes."""

    io_some_avg10: float
    io_full_avg10: float
    mem_some_avg10: float
    cpu_some_avg10: float
    iowait_tasks: float
    d_tasks: float
    mem_tasks: float
    maint_tasks: float
    dirty_bytes: float
    writeback_bytes: float


class AnalyseResult(TypedDict):
    """What summarise() returns (analyse() adds "below"). The keys after
    "coverage" are only there when the window has 2+ samples; main() pops
    "coverage" for --json."""

    window: Window
    kernel: str | None
    samples: int
    episodes: list[Episode]
    events: list[Notice]
    alerts: list[Notice]
    coverage: NotRequired[Coverage]
    pressure: NotRequired[dict[str, float]]  # "<psi key>_pct"
    peak: NotRequired[WindowPeak]
    maintenance_samples: NotRequired[int]
    vm: NotRequired[VmTotals]
    fs: NotRequired[dict[str, FsTotals]]
    disks: NotRequired[dict[str, DiskTotals]]
    cgroups: NotRequired[dict[str, CgroupTotals]]
    categories: NotRequired[list[CategoryShare]]
    non_storage_waits: NotRequired[dict[str, int]]
    sample_d_tasks: NotRequired[int]
    open_episode: NotRequired[OpenEpisode | None]
    v6_since: NotRequired[float | None]
    below: NotRequired[BelowSection]


@dataclass(frozen=True, slots=True)
class Recorded:
    """The records of the log files by their kind ("t"), each list in file order; damaged: the
    time each damaged record or line belongs to (load: its own "ts", else its neighbour's)."""

    metas: list[MetaRecord]
    samples: list[SampleRecord]
    episodes: list[EpisodeRecord]
    events: list[Notice]
    alerts: list[Notice]
    damaged: list[float] = field(default_factory=list)


# What summarise() reads of each record, of its type (a bool is no number). The keys a later daemon
# added are optional: mono, vm, d, d_all (v3+), d_maint (v5+), d_mem (v8+), cg; max_d_all (v3+),
# max_d_mem (v8+). Records of every daemon version pass; a record that fails is damaged.
_NUM = (int, float)
_META_KEYS: KeyTypes = {
    "ts": _NUM,
    "v": (int,),
    "kernel": (str,),
    "base_interval": _NUM,
    "vm_keys": (list,),
    "dirty_bytes": (int, type(None)),  # null: the daemon could not read vm.dirty_bytes
    "disks": (dict,),
    "fs": (dict,),
    "stacks": (bool,),
}
_SAMPLE_REQUIRED: KeyTypes = {"ts": _NUM, "psi": (dict,), "procs": (list,), "fs": (dict,), "disk": (dict,)}
_SAMPLE_OPTIONAL: KeyTypes = {
    "mono": _NUM,
    "vm": (list,),
    "d": (int,),
    "d_all": (int,),
    "d_maint": (int,),
    "d_mem": (int,),
    "cg": (dict,),
}
_EPISODE_REQUIRED: KeyTypes = {
    "start": _NUM,
    "end": _NUM,
    "reason": (str,),
    "max_d": (int,),
    "peak": (dict,),
    "sigs": (list,),
}
_EPISODE_OPTIONAL: KeyTypes = {"max_d_all": (int,), "max_d_mem": (int,)}
_NOTICE_OPTIONAL: KeyTypes = {
    "kind": (str,),
    "msg": (str,),
    "severe": (bool,),
    "stack": (list,),
    "comm": (str,),
    "title": (str,),
    "body": (str,),
}


def is_meta_record(r: object) -> TypeIs[MetaRecord]:
    return has_shape(r, {}, _META_KEYS)


def is_sample_record(r: object) -> TypeIs[SampleRecord]:
    return has_shape(r, _SAMPLE_REQUIRED, _SAMPLE_OPTIONAL)


def is_episode_record(r: object) -> TypeIs[EpisodeRecord]:
    """An episode record with its keys, each of its sigs an object."""
    if not has_shape(r, _EPISODE_REQUIRED, _EPISODE_OPTIONAL) or not json_object(r):
        return False
    sigs = r["sigs"]
    return isinstance(sigs, list) and all(json_object(s) for s in sigs)


def is_notice(r: object) -> TypeIs[Notice]:
    return has_shape(r, {"ts": _NUM}, _NOTICE_OPTIONAL)


def record_ts(r: object) -> float | None:
    """A record's own time ("ts"), when it is a number."""
    return number_or_none(r.get("ts")) if json_object(r) else None

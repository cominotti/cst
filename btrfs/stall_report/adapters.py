# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-report's machine side: the log folder, `date -d` for --since/--until, reading the daily log files, the analysis with the clock and below, and container names."""

from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import IO, Final, NamedTuple
import glob
import gzip
import os
import re
import shutil
import subprocess
import sys
import time
import zlib

from btrfs.stall_monitord.domain import StallRecordTag
from btrfs.stall_report.domain.records import (
    AnalyseResult,
    BelowItem,
    BelowSection,
    MetaRecord,
    Recorded,
    is_episode_record,
    is_meta_record,
    is_notice,
    is_sample_record,
    record_ts,
)
from btrfs.stall_report.domain.window import summarise
from cst_core.domain.contracts import StateFormatKey, state_format
from cst_core.domain.shapes import json_object, parse_json
from cst_core.domain.values import DAY, MINUTE

LOG_DIR = state_format(StateFormatKey.STALL_RECORDS).folder


def parse_time(s: str | None, default: float) -> float:
    if s is None or s == "":
        return default
    s = s.strip()
    if s.startswith("@"):
        return float(s[1:])
    try:
        return float(s)
    except ValueError:
        pass
    out = subprocess.run(["date", "-d", s, "+%s"], check=False, capture_output=True, text=True)
    if out.returncode != 0:
        sys.exit(f"cannot parse time: {s!r} ({out.stderr.strip()})")
    return float(out.stdout.strip())


def report_window(since: str | None, until: str | None) -> tuple[float, float]:
    """(since, until) of the report: --until (default now), --since (default 30 min before now)."""
    now = time.time()
    end = parse_time(until, now)
    start = parse_time(since, now - 30 * MINUTE)
    if start >= end:
        sys.exit("--since must be before --until")
    return start, end


def log_dir_exists(log_dir: str) -> bool:
    return os.path.isdir(log_dir)


class Loaded(NamedTuple):
    """What load() read: each line that parses to a JSON object, in file order, and the time each
    damaged line belongs to."""

    records: list[object]
    damaged: list[float]


def _file_lines(path: str, opener: Callable[..., IO[str]]) -> tuple[list[object], list[float], BaseException | None]:
    """The objects of one log file, and the times of its damaged lines: a line that is not a JSON
    object, or a record of a known kind without what the report reads. A damaged line belongs to
    its own "ts" when it has one, else to the record before it in the file (the one after it when
    there is none before; the file's day when it has none). A last line without its newline is
    not counted: the daemon may be writing it. A file that cannot be read to its end (a cut .gz) gives
    the lines read before the error, and the error."""
    lines: list[str] = []
    error: BaseException | None = None
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as f:
            lines.extend(f)
    except (OSError, EOFError, zlib.error) as e:
        error = e
    objects: list[object] = []
    seen: list[float | None] = []  # per line: its record's time, None for a damaged line without one
    bad: list[int] = []
    for i, line in enumerate(lines):
        try:
            rec = parse_json(line)
        except ValueError, RecursionError:
            rec = None
        ts = record_ts(rec)
        seen.append(ts)
        if json_object(rec) and not damaged_record(rec):
            objects.append(rec)
        elif i < len(lines) - 1 or line.endswith("\n"):
            bad.append(i)
    day = os.path.basename(path)[6:16]
    return objects, _damaged_times(seen, bad, time.mktime(time.strptime(day, "%Y-%m-%d"))), error


def _damaged_times(seen: Sequence[float | None], bad: Sequence[int], file_day: float) -> list[float]:
    """The time each damaged line (BAD: its index in the file) belongs to: its own (SEEN: each
    line's record time, None for a damaged line without one), else the time of the record before
    it, else of the one after it, else FILE_DAY."""
    # One pass each way, not one per damaged line: the last record time before each line, the
    # first one after it.
    before = _last_seen(seen)
    after = _last_seen(reversed(seen))[::-1]
    out: list[float] = []
    for i in bad:
        found = next((t for t in (seen[i], before[i], after[i]) if t is not None), file_day)
        out.append(found)
    return out


def _last_seen(times: Iterable[float | None]) -> list[float | None]:
    """For each of TIMES, the last one before it that is not None (None when there is none)."""
    out: list[float | None] = []
    last: float | None = None
    for t in times:
        out.append(last)
        last = t if t is not None else last
    return out


def load(log_dir: str, since: float, until: float) -> Loaded:
    """The records of the log files around the window; analyse() picks them by their "t"."""
    loaded = Loaded([], [])
    first_day = time.strftime("%Y-%m-%d", time.localtime(since - DAY))
    # Records buffered across midnight can land in the next day's file.
    last_day = time.strftime("%Y-%m-%d", time.localtime(until + DAY))
    paths = sorted(
        glob.glob(os.path.join(log_dir, "stall-*.jsonl")) + glob.glob(os.path.join(log_dir, "stall-*.jsonl.gz"))
    )
    plain = {p for p in paths if p.endswith(".jsonl")}
    for path in paths:
        day = os.path.basename(path)[6:16]
        if day < first_day or day > last_day:
            continue
        if path.endswith(".gz") and path[:-3] in plain:
            continue
        opener = gzip.open if path.endswith(".gz") else open
        records, damaged, error = _file_lines(path, opener)
        if error is not None:
            print(f"warning: cannot fully read {path}: {error}", file=sys.stderr)
        loaded.records.extend(records)
        loaded.damaged.extend(damaged)
    return loaded


def damaged_record(r: object) -> bool:
    """r has a known kind ("t") and lacks what the report reads of it."""
    tag = r.get("t") if json_object(r) else None
    check = _CHECKS.get(tag) if isinstance(tag, str) else None
    return check is not None and not check(r)


_CHECKS: Final[dict[str, Callable[[object], bool]]] = {
    StallRecordTag.META: is_meta_record,
    StallRecordTag.SAMPLE: is_sample_record,
    StallRecordTag.EPISODE: is_episode_record,
    StallRecordTag.EVENT: is_notice,
    StallRecordTag.ALERT: is_notice,
}


def records_of(recs: Sequence[object], damaged: Sequence[float] = ()) -> Recorded:
    """The records by their kind ("t"), after DAMAGED (load's). One of a known kind without what the
    report reads is damaged too, at its own time (none: in no window). Another kind is not read."""
    by_tag: dict[object, list[object]] = {}  # one pass over the records: each one's tag read once
    for r in recs:
        by_tag.setdefault(r.get("t") if json_object(r) else None, []).append(r)
    tag = StallRecordTag
    return Recorded(
        metas=[r for r in by_tag.get(tag.META, []) if is_meta_record(r)],
        samples=[r for r in by_tag.get(tag.SAMPLE, []) if is_sample_record(r)],
        episodes=[r for r in by_tag.get(tag.EPISODE, []) if is_episode_record(r)],
        events=[r for r in by_tag.get(tag.EVENT, []) if is_notice(r)],
        alerts=[r for r in by_tag.get(tag.ALERT, []) if is_notice(r)],
        damaged=[*damaged, *(_own_time(r) for r in recs if damaged_record(r))],
    )


def _own_time(r: object) -> float:
    ts = record_ts(r)
    return float("-inf") if ts is None else ts


def analyse(
    recs: Loaded | Sequence[object], since: float, until: float, with_below: bool, verbose: bool
) -> tuple[AnalyseResult, MetaRecord]:
    """The analysis of the window (summarise(), with the clock) of the records load() read (or
    records given as they are), and below's per-process section when asked (a window with 2+
    samples only, the last key of the result)."""
    loaded = recs if isinstance(recs, Loaded) else Loaded(list(recs), [])
    recorded = records_of(loaded.records, loaded.damaged)
    res, meta = summarise(recorded, since, until, verbose=verbose, now=time.time())
    if with_below and res["samples"] >= 2:
        res["below"] = below_section(since, until)
    return res, meta


def below_section(since: float, until: float) -> BelowSection:
    """below's top writers and readers of the window, both at once, or why there are none."""
    if shutil.which("below") is None:
        return {"error": "below not installed (no per-process attribution)"}
    with ThreadPoolExecutor(2) as ex:
        fw = ex.submit(below_top, since, until, "io.wbytes_per_sec")
        fr = ex.submit(below_top, since, until, "io.rbytes_per_sec")
        (w, ew), (r, er) = fw.result(), fr.result()
    return {"top_writers": w, "top_readers": r, "error": ew or er}


def container_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for tool, prefix in (("docker", "docker-"), ("podman", "libpod-")):
        if shutil.which(tool) is None:
            continue
        try:
            out = subprocess.run(
                [tool, "ps", "-a", "--no-trunc", "--format", "{{.ID}}\t{{.Names}}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except OSError, subprocess.TimeoutExpired:
            continue
        for line in out.stdout.splitlines():
            cid, _, name = line.partition("\t")
            if cid:
                names[prefix + cid[:12]] = name
    return names


def below_top(since: float, until: float, field: str, top: int = 8) -> tuple[list[BelowItem] | None, str | None]:
    """below's per-process IO for the window: ([{comm, cgroup, bytes}], error)."""
    cmd = [
        "below",
        "dump",
        "process",
        "-b",
        str(int(since)),
        "-e",
        str(int(until)),
        "-O",
        "json",
        "--raw",
        "-f",
        "timestamp",
        "pid",
        "comm",
        "cgroup",
        "io.rbytes_per_sec",
        "io.wbytes_per_sec",
        "-s",
        field,
        "--rsort",
        "--top",
        str(top),
    ]
    try:
        out = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"below dump failed: {e}"
    if out.returncode != 0:
        return None, f"below dump failed: {(out.stderr or out.stdout).strip()[:200]}"
    try:
        rows = parse_json(out.stdout or "[]")
    except ValueError:
        rows = []
        for chunk in re.split(r"\n(?=[\[{])", out.stdout.strip()):
            try:
                v = parse_json(chunk)
            except ValueError:
                continue
            rows += v if isinstance(v, list) else [v]
    # below 0.9 prints one list per sample: [[{row}, ...], [{row}, ...], ...]
    flat, stack = [], [rows]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            flat.append(item)
    rows = flat

    def pick(row: dict[str, object], *cands: str) -> object:
        for k, v in row.items():
            lk = k.lower().replace(" ", "").replace("_", "").replace(".", "")
            if any(c in lk for c in cands):
                return v
        return None

    def num(v: object) -> float:
        if isinstance(v, (int, float)):
            return float(v)
        m = re.match(r"\s*([\d.]+)", str(v or ""))
        return float(m.group(1)) if m else 0.0

    agg = {}
    want_write = field.endswith("wbytes_per_sec")
    for row in rows:
        comm = str(pick(row, "comm", "name") or "?")
        cg = str(pick(row, "cgroup") or "").rstrip("/").rsplit("/", 1)[-1][:40]
        rate = num(pick(row, "wbytes", "write") if want_write else pick(row, "rbytes", "read"))
        agg[comm, cg] = agg.get((comm, cg), 0.0) + rate * 5.0  # below samples every 5 s
    items: list[BelowItem] = [{"comm": c, "cgroup": g, "bytes": int(v)} for (c, g), v in agg.items() if v > 0]
    return sorted(items, key=lambda x: -x["bytes"]), None

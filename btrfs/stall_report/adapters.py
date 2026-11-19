# SPDX-License-Identifier: MPL-2.0
"""btrfs-stall-report's machine side: the log folder, `date -d` for --since/--until, reading the daily log files, the analysis with the clock and below, and container names."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any
import glob
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zlib

from btrfs.stall_monitord.domain import StallRecordTag
from btrfs.stall_report.domain.records import AnalyseResult, BelowItem, BelowSection, MetaRecord, Recorded
from btrfs.stall_report.domain.window import summarise
from cst_core.domain.contracts import StateFormatKey, state_format
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


def load(log_dir: str, since: float, until: float) -> list[Any]:
    # Each line that parses to a JSON object; analyse() picks the records by their "t".
    recs = []
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
        try:
            with opener(path, "rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict):  # a list, number or string is no record either
                        recs.append(rec)
        except (OSError, EOFError, zlib.error) as e:
            print(f"warning: cannot fully read {path}: {e}", file=sys.stderr)
    return recs


def analyse(
    recs: list[Any], since: float, until: float, with_below: bool, verbose: bool
) -> tuple[AnalyseResult, MetaRecord]:
    """The analysis of the window (summarise(), with the clock) of the records
    load() read, and below's per-process section when asked (a window with 2+
    samples only, the last key of the result)."""
    recorded = Recorded([], [], [], [], [])
    by_tag = {
        StallRecordTag.META: recorded.metas,
        StallRecordTag.SAMPLE: recorded.samples,
        StallRecordTag.EPISODE: recorded.episodes,
        StallRecordTag.EVENT: recorded.events,
        StallRecordTag.ALERT: recorded.alerts,
    }
    for r in recs:
        tag = r.get("t")
        kept = by_tag.get(tag) if isinstance(tag, str) else None  # another kind of record: not read
        if kept is not None:
            kept.append(r)
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
        rows = json.loads(out.stdout or "[]")
    except ValueError:
        rows = []
        for chunk in re.split(r"\n(?=[\[{])", out.stdout.strip()):
            try:
                v = json.loads(chunk)
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

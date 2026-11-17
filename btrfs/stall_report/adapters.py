"""btrfs-stall-report's machine side: the log folder, `date -d` for --since/--until, reading the daily log files, below and container names."""

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

from btrfs.stall_report.domain import BelowItem

LOG_DIR = "/var/log/btrfs-stall-monitor"


def parse_time(s: str | None, default: float) -> float:
    if not s:
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
    start = parse_time(since, now - 1800)
    if start >= end:
        sys.exit("--since must be before --until")
    return start, end


def load(log_dir: str, since: float, until: float) -> list[Any]:
    # Whatever each line parses to; analyse() picks the records by their "t".
    recs: list[Any] = []
    first_day = time.strftime("%Y-%m-%d", time.localtime(since - 86400))
    # Records buffered across midnight can land in the next day's file.
    last_day = time.strftime("%Y-%m-%d", time.localtime(until + 86400))
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
                        recs.append(json.loads(line))
                    except ValueError:
                        pass
        except (OSError, EOFError, zlib.error) as e:
            print(f"warning: cannot fully read {path}: {e}", file=sys.stderr)
    return recs


def container_names() -> dict[str, str]:
    names: dict[str, str] = {}
    for tool, prefix in (("docker", "docker-"), ("podman", "libpod-")):
        if not shutil.which(tool):
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
        agg[(comm, cg)] = agg.get((comm, cg), 0.0) + rate * 5.0  # below samples every 5 s
    items: list[BelowItem] = [{"comm": c, "cgroup": g, "bytes": int(v)} for (c, g), v in agg.items() if v > 0]
    return sorted(items, key=lambda x: -x["bytes"]), None

"""Pure domain model, parsing, and calculations for the swap module."""

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Final, Literal, TypedDict

from cst_core.domain.files import ManagedState, MarkedLineSpec
from cst_core.domain.plan import PlanItem
from cst_core.domain.values import ByteSize, format_bytes_human

SWAP_MARKER: Final[str] = "# managed-by: swap-setup.sh (CST)"
SWAP_OLD_MARKERS: Final[tuple[str, ...]] = (
    "# managed-by: apply-swap-config.sh",
    "# managed-by: apply-zram-config.sh",
)
WSF_EXPERIMENT: Final[tuple[str, str]] = ("wsf-300", "vm.watermark_scale_factor")
PAGE: Final[int] = 4096

# What each debugfs counter means, in short (unknown ones are still shown).
DEBUG_HELP: Final[dict[str, str]] = {
    "pool_total_size": "bytes the compressed pool takes in RAM",
    "stored_pages": "pages in the pool now",
    "stored_incompressible_pages": "pages stored raw (did not compress)",
    "written_back_pages": "pages moved from the pool to disk swap",
    "pool_limit_hit": "times the pool was full (max_pool_percent)",
    "reject_reclaim_fail": "shrinker writeback failed (page often in swap cache)",
    "reject_compress_poor": "rejected: no gain from compression, writeback off",
    "reject_compress_fail": "rejected: did not compress, zswap writeback off",
    "reject_alloc_fail": "rejected: no memory for the compressed copy",
    "reject_kmemcache_fail": "rejected: no memory for the entry",
    "decompress_fail": "decompression errors (should be 0)",
    "duplicate_entry": "pages stored twice (should be 0)",
    "same_filled_pages": "pages of one repeated value (stored without data)",
}

_STATE_WORDS: Final[dict[ManagedState, str]] = {
    "ok": "ok",
    "adopt": "adopt",
    "duplicate": "dedupe",
    "missing": "write",
    "change": "update",
    "conflict": "CONFLICT",
    "unreadable": "?",
}

_SAFE_PARTITION_TYPES: Final[frozenset[str]] = frozenset({"", "swap", "crypto_PLAIN"})


@dataclass(frozen=True, slots=True)
class SwapHardwareSpec:
    """Identity and exact expected size of the encrypted NVMe swap partition."""

    partuuid: str = "612551b1-ca02-4514-86ad-7d8902e612dc"
    name: str = "swap0"
    expected_size_bytes: int = 137438953472

    @property
    def mapper_path(self) -> str:
        """Mapped dm-crypt device path."""
        return f"/dev/mapper/{self.name}"

    @property
    def device_path(self) -> str:
        """GPT PARTUUID symlink path for the underlying swap partition."""
        return f"/dev/disk/by-partuuid/{self.partuuid}"

    @property
    def crypttab_line(self) -> str:
        """Desired marked /etc/crypttab line for plain dm-crypt swap."""
        return (
            f"{self.name} {self.device_path} /dev/urandom "
            f"swap,cipher=aes-xts-plain64,size=512,sector-size=4096,discard {SWAP_MARKER}"
        )

    @property
    def fstab_line(self) -> str:
        """Desired marked /etc/fstab line for the encrypted swap mapper."""
        return f"{self.mapper_path} none swap defaults,nofail,discard 0 0 {SWAP_MARKER}"

    def crypttab_spec(self) -> MarkedLineSpec:
        """Return the MarkedLineSpec managing this partition in /etc/crypttab."""
        return MarkedLineSpec(
            desired_line=self.crypttab_line,
            key=self.name,
            current_marker=SWAP_MARKER,
            legacy_markers=SWAP_OLD_MARKERS,
        )

    def fstab_spec(self) -> MarkedLineSpec:
        """Return the MarkedLineSpec managing this swap device in /etc/fstab."""
        return MarkedLineSpec(
            desired_line=self.fstab_line,
            key=self.mapper_path,
            current_marker=SWAP_MARKER,
            legacy_markers=SWAP_OLD_MARKERS,
        )


@dataclass(frozen=True, slots=True)
class SwapDeviceEntry:
    """Single active entry parsed from /proc/swaps."""

    device: str
    swap_type: str
    size_kib: int
    used_kib: int
    priority: str


class ZswapSummary(TypedDict):
    """What zswap_summary returns. Sizes in bytes, counters in pages,
    shares as fractions (None where the divisor is 0 or a value is missing)."""

    pool: int | None
    stored: int | None
    limit: int | None
    pool_of_limit: float | None
    accept_below: int | None
    ratio: float | None
    zswpout: int
    zswpin: int
    zswpwb: int
    pswpout: int
    pswpin: int
    loaded_back: float | None
    written_back: float | None
    bypassed: int
    read_back: float | None


class CgroupRow(TypedDict):
    """What cgroup_row returns for one cgroup. Sizes in bytes."""

    swap: int | None
    zswap: int | None
    zswapped: int | None
    zswpout: int
    zswpwb: int
    pswpin: int
    written_back: float | None
    read_back: float | None


def parse_kv(text: str) -> dict[str, int]:
    """'name value' lines (vmstat, memory.stat) -> {name: int}."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                pass
    return out


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo -> {name: bytes}."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^(\S+):\s+(\d+)(?:\s+kB)?", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * (1024 if line.rstrip().endswith("kB") else 1)
    return out


def parse_tmpfiles_params(text: str) -> list[tuple[str, str]]:
    """zswap.conf -> [(param, value)] in file order."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        m = re.match(r"^w\s+/sys/module/zswap/parameters/(\S+)\s+(?:\S+\s+){4}(\S+)", line)
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def parse_sysctl(text: str) -> list[tuple[str, str]]:
    """sysctl.d file -> [(key, value)]."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] in "#;":
            continue
        key, sep, val = line.partition("=")
        if sep:
            out.append((key.strip(), val.strip()))
    return out


def parse_size(spec: str | None) -> int | None:
    """systemd size ('48G', '512M', '1024') -> bytes (powers of 1024)."""
    parsed = ByteSize.parse(spec)
    return None if parsed is None else parsed.bytes_count


def parse_memory_low(text: str) -> int | None:
    """Extract MemoryLow= value in bytes from a systemd drop-in."""
    m = re.search(r"^MemoryLow=(\S+)", text, re.M)
    return parse_size(m.group(1)) if m else None


def same_param(live: str, desired: str) -> bool:
    """zswap shows booleans as Y/N; tmpfiles writes 1/0."""
    norm = {"1": "Y", "0": "N", "y": "Y", "n": "N"}
    return norm.get(live, live) == norm.get(desired, desired)


def share(num: float, den: float | None) -> float | None:
    """num/den as a fraction, None when den is 0."""
    return None if not den else num / den


def pct(frac: float | None) -> str:
    """Format a fraction as a percentage string with 1 decimal, or '-'."""
    return "-" if frac is None else f"{frac * 100:.1f} %"


def human(n: int | None) -> str:
    """Format bytes into binary IEC units, or '-' when None."""
    return format_bytes_human(n)


def psi_value(text: str, kind: str = "some", key: str = "avg60") -> str | None:
    """Extract one metric from PSI file text (e.g. kind='some', key='avg60')."""
    for line in text.splitlines():
        if line.startswith(kind + " "):
            m = re.search(rf"{key}=(\S+)", line)
            if m:
                return m.group(1)
    return None


def parse_proc_swaps(text: str) -> list[SwapDeviceEntry]:
    """Parse /proc/swaps rows (skipping the header line)."""
    entries: list[SwapDeviceEntry] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 5:
            continue
        if not (fields[2].isdigit() and fields[3].isdigit()):
            continue
        entries.append(
            SwapDeviceEntry(
                device=fields[0],
                swap_type=fields[1],
                size_kib=int(fields[2]),
                used_kib=int(fields[3]),
                priority=fields[4],
            )
        )
    return entries


def find_swap_used_kib(swaps_text: str, device: str) -> int | None:
    """Return used KiB for device if active in /proc/swaps, else None."""
    for entry in parse_proc_swaps(swaps_text):
        if entry.device == device:
            return entry.used_kib
    return None


def can_evict_zram(zram_used_kib: int, mem_available_kib: int) -> bool:
    """Return True if MemAvailable covers zram contents plus 1 GiB headroom."""
    return zram_used_kib + 1024 * 1024 < mem_available_kib


def is_safe_partition_type(part_type: str) -> bool:
    """Return True when blkid TYPE is safe for plain dm-crypt swap overwrite."""
    return part_type in _SAFE_PARTITION_TYPES


def crypto_has_algorithm(proc_crypto_text: str, name: str) -> bool:
    """Return True if /proc/crypto lists an entry with `name : <name>`."""
    pattern = re.compile(rf"name *: {re.escape(name)}")
    return any(pattern.fullmatch(line) for line in proc_crypto_text.splitlines())


def describe_state(state: ManagedState) -> str:
    """Map a ManagedState into the column word shown by plan/status."""
    return _STATE_WORDS[state]


def state_note(state: ManagedState, swap_name: str, prog: str, verb: str) -> str:
    """Human-readable note explaining a managed-line or managed-file state."""
    notes: dict[ManagedState, str] = {
        "ok": "up to date",
        "adopt": "up to date; written by an older name: apply only renews the comment",
        "duplicate": "the same entry twice (the old script ran again?): apply keeps one, no restart",
        "missing": "missing",
        "change": "differs from swap/ in CST",
        "conflict": f"an unmarked {swap_name} entry exists: apply refuses; remove it by hand",
        "unreadable": f"needs root to read: on the host, {prog} {verb} --root",
    }
    return notes[state]


def zswap_summary(meminfo: Mapping[str, int], vmstat: Mapping[str, int], params: Mapping[str, str]) -> ZswapSummary:
    """The numbers status prints about the pool and the swap traffic.

    meminfo in bytes, vmstat in pages, params as read from sysfs.
    """
    total = meminfo.get("MemTotal", 0)
    pool = meminfo.get("Zswap")
    stored = meminfo.get("Zswapped")
    try:
        max_pct = int(params.get("max_pool_percent", ""))
    except ValueError:
        max_pct = None
    try:
        accept = int(params.get("accept_threshold_percent", ""))
    except ValueError:
        accept = None
    limit = total * max_pct // 100 if max_pct is not None else None
    zout, zin, zwb = (vmstat.get(k, 0) for k in ("zswpout", "zswpin", "zswpwb"))
    pout, pin = vmstat.get("pswpout", 0), vmstat.get("pswpin", 0)
    return {
        "pool": pool,
        "stored": stored,
        "limit": limit,
        "pool_of_limit": share(pool or 0, limit) if pool is not None else None,
        "accept_below": limit * accept // 100 if limit is not None and accept is not None else None,
        "ratio": (stored / pool) if pool and stored is not None else None,
        "zswpout": zout,
        "zswpin": zin,
        "zswpwb": zwb,
        "pswpout": pout,
        "pswpin": pin,
        "loaded_back": share(zin, zout),
        "written_back": share(zwb, zout),
        "bypassed": max(pout - zwb, 0),
        "read_back": share(pin, pout),
    }


def cgroup_row(stat: Mapping[str, int], swap_current: int | None) -> CgroupRow:
    """memory.stat (+ memory.swap.current) of one cgroup -> derived values."""
    zout, zwb = stat.get("zswpout", 0), stat.get("zswpwb", 0)
    pout, pin = stat.get("pswpout", 0), stat.get("pswpin", 0)
    return {
        "swap": swap_current,
        "zswap": stat.get("zswap"),
        "zswapped": stat.get("zswapped"),
        "zswpout": zout * PAGE,
        "zswpwb": zwb * PAGE,
        "pswpin": pin * PAGE,
        "written_back": share(zwb, zout),
        "read_back": share(pin, pout),
    }


@dataclass(frozen=True, slots=True)
class ConfigSectionReport:
    """Structured config file and runtime partition/swap inspection report."""

    etc_visible: bool
    etc_dir: str
    prog: str
    verb: str
    file_items: tuple[PlanItem, ...]
    runtime_items: tuple[PlanItem, ...]


@dataclass(frozen=True, slots=True)
class SettingCheckRow:
    """Single row comparing a live setting against its desired value."""

    state: str
    label: str
    live: str
    want: str


@dataclass(frozen=True, slots=True)
class SettingsReport:
    """Comparison of live zswap, sysctl, and cgroup settings against desired values."""

    rows: tuple[SettingCheckRow, ...]
    exp_name: str
    exp_key: str

    @property
    def has_diff(self) -> bool:
        """Return True if any inspected setting differs from desired."""
        return any(row.state == "DIFF" for row in self.rows)

    @property
    def has_exp(self) -> bool:
        """Return True if a transient CPU experiment override is active."""
        return any(row.state == "exp" for row in self.rows)


@dataclass(frozen=True, slots=True)
class DebugCounterRow:
    """Single counter line read from /sys/kernel/debug/zswap."""

    name: str
    value: str
    help_text: str


@dataclass(frozen=True, slots=True)
class DebugSectionReport:
    """Snapshot of zswap debugfs visibility and counter rows."""

    debug_dir: str
    status: Literal["ok", "permission_denied", "unavailable"]
    hint: str
    rows: tuple[DebugCounterRow, ...] = ()


@dataclass(frozen=True, slots=True)
class SwapDeviceRow:
    """Active swap device rendered in status output."""

    label: str
    swap_type: str
    used_bytes: int
    size_bytes: int
    priority: str


@dataclass(frozen=True, slots=True)
class CgroupSwapRow:
    """Per-cgroup swap/zswap metrics and PSI snapshot."""

    label: str
    row: CgroupRow
    writeback: str
    low_display: str
    psi60: str


@dataclass(frozen=True, slots=True)
class SwapPlanReport:
    """Complete dry-run plan output model."""

    config: ConfigSectionReport
    settings: SettingsReport
    prog: str


@dataclass(frozen=True, slots=True)
class SwapStatusReport:
    """Complete read-only status output model."""

    config: ConfigSectionReport | None
    settings: SettingsReport
    settings_only: bool
    params: dict[str, str]
    summary: ZswapSummary | None
    debug: DebugSectionReport | None
    devices: tuple[SwapDeviceRow, ...]
    cgroups: tuple[CgroupSwapRow, ...]
    psi_lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ApplyEvent:
    """One output/warning/error event emitted during `apply`."""

    stream: Literal["out", "err"]
    kind: Literal["section", "line", "raw", "warn", "error"]
    text: str


@dataclass(frozen=True, slots=True)
class SwapApplyResult:
    """Structured result of running `swap apply`."""

    returncode: int
    events: tuple[ApplyEvent, ...]
    settings: SettingsReport | None
    verify_notes: tuple[str, ...]
    swapon_out: str
    verify_error: str
    prog: str


# SPDX-License-Identifier: MPL-2.0
"""Pure domain model, parsing, and calculations for the swap module."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
import shlex
from typing import Final, Literal, TypedDict

from cst_core.domain.files import ManagedState, MarkedLineSpec
from cst_core.domain.plan import PlanItem
from cst_core.domain.text import parse_shell_assignments
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


# The swap settings, /etc/cst/swap.conf (KEY=VALUE, only Python reads it): which partition swap
# manages. Nothing is configured by default; apply refuses until it is (swap/README.md, 2.3).
SETTINGS_REL: Final[str] = "cst/swap.conf"
DEFAULT_SWAP_NAME: Final[str] = "swap0"
# GPT PARTUUIDs as /dev/disk/by-partuuid names them (lower case), or the MBR form (disk id-partition).
_PARTUUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{8}-[0-9a-f]{2}")
# A dm-crypt mapper name that is also safe in a crypttab line, an fstab line and a unit name.
_MAPPER_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_BY_PARTUUID: Final[str] = "/dev/disk/by-partuuid/"
_SETTING_KEYS: Final[tuple[str, ...]] = ("SWAP_PARTUUID", "SWAP_SIZE_BYTES", "SWAP_NAME")


@dataclass(frozen=True, slots=True)
class SwapHardwareSpec:
    """Identity and exact expected size of the encrypted swap partition (from /etc/cst/swap.conf)."""

    partuuid: str
    name: str
    expected_size_bytes: int

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
class SwapSettings:
    """What /etc/cst/swap.conf says: the partition (None: not configured) and the problems that make
    apply refuse (each "line N: TEXT: REASON" or "KEY: reason")."""

    spec: SwapHardwareSpec | None
    problems: tuple[str, ...] = ()


def parse_swap_settings(text: str) -> SwapSettings:
    """/etc/cst/swap.conf -> SwapSettings. No SWAP_PARTUUID is "not configured"; a bad line or value,
    or a PARTUUID without its size, is a problem (and no partition)."""
    parsed = parse_shell_assignments(text, bash_sourced=False)
    problems = [str(p) for p in parsed.problems]
    values = parsed.values
    problems += [
        f"{key}: not a setting of this file ({', '.join(_SETTING_KEYS)})" for key in values if key not in _SETTING_KEYS
    ]
    partuuid = values.get("SWAP_PARTUUID", "")
    name = values.get("SWAP_NAME", DEFAULT_SWAP_NAME)
    size = values.get("SWAP_SIZE_BYTES", "")
    if not partuuid:
        if size:
            problems.append("SWAP_SIZE_BYTES is set, SWAP_PARTUUID is not")
        return SwapSettings(None, tuple(problems))
    if not _PARTUUID.fullmatch(partuuid):
        problems.append(f"SWAP_PARTUUID={partuuid}: not a PARTUUID as /dev/disk/by-partuuid names it (lower case)")
    if not _MAPPER_NAME.fullmatch(name):
        problems.append(f"SWAP_NAME={name}: letters, digits, _ . - only (it names /dev/mapper/NAME and units)")
    if not size.isdecimal() or not size.isascii() or int(size) == 0:
        problems.append(
            f"SWAP_SIZE_BYTES={size}: the partition's exact size in bytes (blockdev --getsize64 /dev/disk/by-partuuid/...)"
        )
    if problems:
        return SwapSettings(None, tuple(problems))
    return SwapSettings(SwapHardwareSpec(partuuid, name, int(size)))


def render_swap_settings(spec: SwapHardwareSpec, origin: str) -> str:
    """The text of /etc/cst/swap.conf for spec (origin: where the values came from)."""
    return (
        "# The swap partition `cst swap` manages (swap/README.md in CST, section 2.3).\n"
        f"# Written by cst swap apply {origin}.\n"
        "# apply checks the exact size before it touches the partition: change both if it changes.\n"
        f"SWAP_NAME={spec.name}\n"
        f"SWAP_PARTUUID={spec.partuuid}\n"
        f"SWAP_SIZE_BYTES={spec.expected_size_bytes}\n"
    )


def _marked_lines(text: str) -> list[list[str]]:
    """The fields of each line that ends with swap's marker (or an older script's)."""
    markers = (SWAP_MARKER, *SWAP_OLD_MARKERS)
    return [line.split() for line in text.splitlines() if line.rstrip().endswith(markers)]


def marked_crypttab_entry(text: str) -> tuple[str, str] | None:
    """(name, PARTUUID) of the swap line an older CST (or the script before it) marked in /etc/crypttab,
    when there is one such entry (the same line twice counts once) and it names its partition by
    PARTUUID; else None."""
    found: set[tuple[str, str]] = set()
    for fields in _marked_lines(text):
        if len(fields) < 2 or not fields[1].startswith(_BY_PARTUUID):
            return None
        found.add((fields[0], fields[1][len(_BY_PARTUUID) :]))
    if len(found) != 1:
        return None
    name, partuuid = found.pop()
    return (name, partuuid) if _MAPPER_NAME.fullmatch(name) and _PARTUUID.fullmatch(partuuid) else None


def marked_fstab_name(text: str) -> str | None:
    """The mapper name of the swap line an older CST marked in /etc/fstab (readable by everyone, unlike
    /etc/crypttab), or None."""
    for fields in _marked_lines(text):
        if fields and fields[0].startswith("/dev/mapper/"):
            return fields[0][len("/dev/mapper/") :]
    return None


@dataclass(frozen=True, slots=True)
class SpecResolution:
    """Which partition plan, status and apply work with, and why.

    state: "ok" (from the settings file), "write" (derived from what an older CST set up: apply
    writes the settings file first), "unknown" (an older CST set swap up, but /etc/crypttab needs
    root to read), "unset" (not configured), "bad" (the settings file has problems).
    """

    spec: SwapHardwareSpec | None
    state: Literal["ok", "write", "unknown", "unset", "bad"]
    note: str = ""


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


def is_cst_dropin(text: str) -> bool:
    """A drop-in CST wrote: one of its comment lines starts with the "Managed by" of swap-setup.sh
    or of apply-swap-config.sh, the script before it (a file someone else wrote under the same name
    is left alone)."""
    return any(
        line.startswith(("# Managed by swap-setup.sh (CST)", "# Managed by apply-swap-config.sh"))
        for line in text.splitlines()
        if line.startswith("#")
    )


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


def killed_text(prog: str, argv: Sequence[str], returncode: int) -> str:
    """The line for a command that signal N killed (returncode -N), as bash
    printed "Terminated" for one: "PROG: COMMAND was killed by signal N"."""
    return f"{prog}: {shlex.join(argv) or 'a command'} was killed by signal {-returncode}"


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
    swapon_err: str = ""

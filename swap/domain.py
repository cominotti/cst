# SPDX-License-Identifier: MPL-2.0
"""Pure domain model, parsing, and calculations for the swap module."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import contextlib
import posixpath
import re
import shlex
from typing import Final, Literal, TypedDict

from cst_core.domain.files import ManagedState, MarkedLineSpec, OwnedState
from cst_core.domain.plan import PlanItem
from cst_core.domain.text import HELP_WORDS, enum_member, group, parse_shell_assignments
from cst_core.domain.values import ByteSize, format_bytes_human
from cst_core.domain.units import ActiveState
from swap.ports import SwapSettingsFacts

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
    OwnedState.OK: "ok",
    OwnedState.ADOPT: "adopt",
    OwnedState.DUPLICATE: "dedupe",
    OwnedState.MISSING: "write",
    OwnedState.CHANGE: "update",
    OwnedState.CONFLICT: "CONFLICT",
    OwnedState.UNREADABLE: "?",
}

_SAFE_PARTITION_TYPES: Final[frozenset[str]] = frozenset({"", "swap", "crypto_PLAIN"})


# The swap settings, /etc/cst/swap.conf (KEY=VALUE, only Python reads it): which partition swap
# manages. Nothing is configured by default; apply refuses until it is (swap/README.md, 2.3).
SETTINGS_REL: Final[str] = "cst/swap.conf"
DEFAULT_SWAP_NAME: Final[str] = "swap0"
# The slice libvirt runs the VM in, whose MemoryLow swap sets.
MACHINE_SLICE: Final[str] = "machine.slice"
# GPT PARTUUIDs as /dev/disk/by-partuuid names them (lower case), or the MBR form (disk id-partition).
_PARTUUID: Final = re.compile(r"[0-9a-f]{8}-(?:(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|[0-9a-f]{2})")
# A dm-crypt mapper name that is also safe in a crypttab line, an fstab line and a unit name.
_MAPPER_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_BY_PARTUUID: Final[str] = "/dev/disk/by-partuuid/"
_SETTING_KEYS: Final[tuple[str, ...]] = ("SWAP_PARTUUID", "SWAP_SIZE_BYTES", "SWAP_NAME")
_SWAP_MARKERS: Final[tuple[str, ...]] = (SWAP_MARKER, *SWAP_OLD_MARKERS)


class SwapCommand(StrEnum):
    """The commands of `cst swap` (help also answers to -h and --help: parse_swap_command)."""

    PLAN = "plan"
    STATUS = "status"
    APPLY = "apply"
    HELP = "help"


def parse_swap_command(word: str) -> SwapCommand | None:
    """The command a word on the command line names, or None for an unknown one."""
    if word in HELP_WORDS:
        return SwapCommand.HELP
    return enum_member(SwapCommand, word)


def partuuid_path(partuuid: str) -> str:
    """The /dev/disk/by-partuuid link of a partition."""
    return _BY_PARTUUID + partuuid


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
        return partuuid_path(self.partuuid)

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
    return [line.split() for line in text.splitlines() if line.rstrip().endswith(_SWAP_MARKERS)]


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


class SwapSpecState(StrEnum):
    """Where the partition plan, status and apply work with comes from (SpecResolution.state)."""

    OK = "ok"
    WRITE = "write"
    UNKNOWN = "unknown"
    UNSET = "unset"
    BAD = "bad"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class SpecResolution:
    """Which partition plan, status and apply work with, and why.

    state: "ok" (from the settings file), "write" (derived from what an older CST set up: apply
    writes the settings file first), "unknown" (an older CST set swap up, but /etc/crypttab needs
    root to read), "unset" (not configured), "bad" (the settings file has problems, is not a regular
    file or cannot be read), "unreadable" (the settings file is there and this user may not read it).
    """

    spec: SwapHardwareSpec | None
    state: SwapSpecState
    note: str = ""
    # "bad" from the settings file's own lines: each problem (note is their "; " join).
    problems: tuple[str, ...] = ()


# sysfs gives a block device's size in sectors of this many bytes, whatever the device's own sector size.
SYSFS_SECTOR_BYTES: Final = 512


def sysfs_partition(
    device_path: str,
    *,
    resolve: Callable[[str], str | None],
    is_block: Callable[[str], bool],
    first_line: Callable[[str], str | None],
    sys_dir: str,
) -> tuple[str, int] | None:
    """The block device a path leads to and its size in bytes, from sysfs's size of it
    (sys_dir/class/block/NAME/size); None when the path leads to no block device or sysfs gives no
    number. resolve: the path with every link followed (None: it leads nowhere); is_block: a block
    device is there; first_line: a file's first line, stripped (None: it cannot be read). cst swap
    and cst doctor both size the partition with it."""
    dev = resolve(device_path)
    if dev is None or not is_block(dev):
        return None
    sectors = first_line(posixpath.join(sys_dir, "class", "block", posixpath.basename(dev), "size"))
    if sectors is None or not sectors.isdigit():
        return None
    return dev, int(sectors) * SYSFS_SECTOR_BYTES


def resolve_settings(facts: SwapSettingsFacts, *, as_root: bool) -> SpecResolution:
    """The partition from /etc/cst/swap.conf; when that file is not there, the one an older CST set
    up (its marked crypttab line, the partition's size now: apply writes the file from them). A file
    that is there but cannot be read is never taken as missing. cst swap and cst doctor both read the
    file through this (as_root: the reader runs as root)."""
    text = facts.settings_text()
    if text is not None:
        settings = parse_swap_settings(text)
        if settings.problems:
            return SpecResolution(None, SwapSpecState.BAD, "; ".join(settings.problems), settings.problems)
        return SpecResolution(settings.spec, SwapSpecState.UNSET if settings.spec is None else SwapSpecState.OK)
    if facts.settings_seen():
        return _settings_not_read(facts, seen=True, as_root=as_root)
    if facts.settings_folder_closed():
        return _settings_not_read(facts, seen=False, as_root=as_root)
    return _spec_from_older_install(facts)


def _settings_not_read(facts: SwapSettingsFacts, *, seen: bool, as_root: bool) -> SpecResolution:
    """/etc/cst/swap.conf is there (seen), or may be in a folder this user may not search, and could
    not be read: never taken as missing (the migration would write over it). A user who may not read
    it is asked for --root; anything else stops apply."""
    if seen and not facts.settings_is_file():
        return SpecResolution(None, SwapSpecState.BAD, "not a regular file")
    if not as_root and not (seen and facts.settings_readable()):
        return SpecResolution(None, SwapSpecState.UNREADABLE)
    # Root, which --root would not help (a security policy, a root-squashed mount), or a failed read.
    return SpecResolution(None, SwapSpecState.BAD, "cannot be read")


def _spec_from_older_install(facts: SwapSettingsFacts) -> SpecResolution:
    """No /etc/cst/swap.conf: the partition an older CST set up (its marked crypttab line, the
    partition's size now), from which apply writes the file."""
    if facts.crypttab_closed():
        name = marked_fstab_name(facts.fstab_text())
        return (
            SpecResolution(None, SwapSpecState.UNSET)
            if name is None
            else SpecResolution(None, SwapSpecState.UNKNOWN, name)
        )
    entry = marked_crypttab_entry(facts.crypttab_text())
    if entry is None:
        return SpecResolution(None, SwapSpecState.UNSET)
    name, partuuid = entry
    size = facts.partition_size(partuuid_path(partuuid))
    if size is None:
        return SpecResolution(
            None,
            SwapSpecState.UNSET,
            f"the {name} line in crypttab names {partuuid_path(partuuid)}, which is not here",
        )
    return SpecResolution(SwapHardwareSpec(partuuid, name, size), SwapSpecState.WRITE, name)


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
            with contextlib.suppress(ValueError):
                out[parts[0]] = int(parts[1])
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
    for raw in text.splitlines():
        line = raw.strip()
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
    m = re.search(r"^MemoryLow=(\S+)", text, re.MULTILINE)
    return parse_size(m.group(1)) if m else None


# A line that assigns MemoryLow as systemd reads it: blanks before the key and around "=" are dropped,
# and an empty value is an assignment too (it resets MemoryLow). A "#" or ";" line is a comment.
_SETS_MEMORY_LOW: Final = re.compile(r"^[ \t]*MemoryLow[ \t]*=", re.MULTILINE)


def memory_low_override(
    effective: str, dropins: Sequence[tuple[str, str | None]], cst_dropin: str, want: int | None
) -> str | None:
    """The drop-in systemd takes machine.slice's MemoryLow from, when that is not CST's: CST's drop-in
    is loaded, systemd's value (effective, as `systemctl show` prints it) is not CST's (want), and a
    later drop-in sets MemoryLow. dropins: (path, text, None when unreadable) as DropInPaths lists them;
    systemd applies them in file-name order, the last value wins. None: CST's value applies, or CST's
    drop-in is not loaded yet, or the drop-in that sets it cannot be named (the cgroup is compared then)."""
    if cst_dropin not in (path for path, _ in dropins) or parse_size(effective) == want:
        return None
    source: str | None = None
    for path, text in sorted(dropins, key=lambda d: posixpath.basename(d[0])):
        if text is not None and _SETS_MEMORY_LOW.search(text):
            source = path
    return None if source == cst_dropin else source


def same_param(live: str, desired: str) -> bool:
    """zswap shows booleans as Y/N; tmpfiles writes 1/0."""
    norm = {"1": "Y", "0": "N", "y": "Y", "n": "N"}
    return norm.get(live, live) == norm.get(desired, desired)


def share(num: float, den: float | None) -> float | None:
    """num/den as a fraction, None when den is 0."""
    return None if den is None or den == 0 else num / den


def pct(frac: float | None) -> str:
    """Format a fraction as a percentage string with 1 decimal, or '-'."""
    return "-" if frac is None else f"{frac * 100:.1f} %"


def human_size(value: str) -> str:
    """A size as systemd prints it (`systemctl show`: bytes), in binary IEC units; other text (infinity)
    as it is."""
    size = parse_size(value)
    return value if size is None else format_bytes_human(size)


def psi_value(text: str, kind: str = "some", key: str = "avg60") -> str | None:
    """Extract one metric from PSI file text (e.g. kind='some', key='avg60')."""
    for line in text.splitlines():
        if line.startswith(kind + " "):
            m = re.search(rf"{key}=(\S+)", line)
            if m:
                return group(m, 1)
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
    )


def describe_state(state: ManagedState) -> str:
    """Map a ManagedState into the column word shown by plan/status."""
    return _STATE_WORDS[state]


def state_note(state: ManagedState, swap_name: str, prog: str, verb: str) -> str:
    """Human-readable note explaining a managed-line or managed-file state."""
    notes: dict[ManagedState, str] = {
        OwnedState.OK: "up to date",
        OwnedState.ADOPT: "up to date; written by an older name: apply only renews the comment",
        OwnedState.DUPLICATE: "the same entry twice (the old script ran again?): apply keeps one, no restart",
        OwnedState.MISSING: "missing",
        OwnedState.CHANGE: "differs from swap/ in CST",
        OwnedState.CONFLICT: f"an unmarked {swap_name} entry exists: apply refuses; remove it by hand",
        OwnedState.UNREADABLE: f"needs root to read: on the host, {prog} {verb} --root",
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
        "ratio": (stored / pool) if pool is not None and pool != 0 and stored is not None else None,
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


class SettingState(StrEnum):
    """The state column of a live setting against its desired value (the words plan and status print)."""

    OK = "ok"
    DIFF = "DIFF"
    EXP = "exp"
    OVER = "over"
    MISSING = "MISSING"
    BAD = "BAD"
    INACTIVE = "-"
    # Only in apply's verification notes under the rows: something that clears by itself later.
    LATER = "later"


# The ActiveStates in which swap reads machine.slice as active (systemd's words: cst_core's ActiveState).
SLICE_ACTIVE_STATES: Final = frozenset({ActiveState.ACTIVE})


def slice_not_active(active_state: str) -> bool:
    """systemd says the slice is not active: an ActiveState other than "active" (any word, known or not).
    "" (systemd gave no answer) is False: not known, so the cgroup is read as before."""
    return active_state != "" and active_state not in SLICE_ACTIVE_STATES


# The width of the state column of the settings rows and of apply's verification notes under them:
# the longest word, so every label starts in the same column.
SETTING_STATE_WIDTH: Final = max(len(state.value) for state in SettingState)


@dataclass(frozen=True, slots=True)
class SettingCheckRow:
    """Single row comparing a live setting against its desired value."""

    state: SettingState
    label: str
    live: str
    want: str


@dataclass(frozen=True, slots=True)
class SettingsReport:
    """Comparison of live zswap, sysctl, and cgroup settings against desired values."""

    rows: tuple[SettingCheckRow, ...]
    exp_name: str
    exp_key: str
    # The drop-in machine.slice's MemoryLow comes from when it is not CST's ("": CST's): its "over" row.
    override_source: str = ""
    cst_dropin: str = ""

    @property
    def has_diff(self) -> bool:
        """Return True if any inspected setting differs from desired."""
        return any(row.state == SettingState.DIFF for row in self.rows)

    @property
    def has_exp(self) -> bool:
        """Return True if a transient CPU experiment override is active."""
        return any(row.state == SettingState.EXP for row in self.rows)


@dataclass(frozen=True, slots=True)
class DebugCounterRow:
    """Single counter line read from /sys/kernel/debug/zswap."""

    name: str
    value: str
    help_text: str


class ZswapDebugStatus(StrEnum):
    """Whether the zswap debug counters could be read (DebugSectionReport.status)."""

    OK = "ok"
    PERMISSION_DENIED = "permission_denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DebugSectionReport:
    """Snapshot of zswap debugfs visibility and counter rows."""

    debug_dir: str
    status: ZswapDebugStatus
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


class ApplyEventKind(StrEnum):
    """What an apply event is: a section title, a line, a command's own output, a warning or an error."""

    SECTION = "section"
    LINE = "line"
    RAW = "raw"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ApplyEvent:
    """One output/warning/error event emitted during `apply`."""

    stream: Literal["out", "err"]
    kind: ApplyEventKind
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

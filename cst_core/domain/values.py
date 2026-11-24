# SPDX-License-Identifier: MPL-2.0
"""Pure domain value objects with invariant enforcement on construction.

CI runs this file on its E2E runner's python3, 3.12 (tests/e2e/selection.py
imports parse_whole_number; tests/e2e/README.md, "The Python that runs these
files"): it stays 3.12 code that imports the standard library only.
tests/test_e2e_host_python.py checks it.
"""

from collections.abc import Iterable
from dataclasses import dataclass
import math
import re
from typing import Final, Self


# A value JSON can carry as it is: json.loads(json.dumps(v)) == v. A tuple
# comes back as a list and a set does not go, so neither is one.
type Json = bool | int | float | str | list[Json] | dict[str, Json] | None


class DomainValidationError(ValueError):
    """Raised when a domain value object invariant is violated."""


# The superuser's account name (SUDO_USER, --user, an install's owner): not a FileOwner or Owner member.
ROOT_ACCOUNT: Final = "root"

MIB: Final = 1 << 20
GIB: Final = 1 << 30

# Durations in seconds.
MINUTE: Final = 60
HOUR: Final = 60 * MINUTE
DAY: Final = 24 * HOUR
WEEK: Final = 7 * DAY

_DURATION_UNITS: Final[dict[str, int]] = {
    "s": 1,
    "m": MINUTE,
    "h": HOUR,
    "d": DAY,
    "w": WEEK,
}

# The largest size ByteSize.parse accepts: systemd's sizes are 64-bit unsigned (above it, systemd refuses).
_SIZE_MAX: Final = 2**64 - 1

_SIZE_UNITS: Final[dict[str, int]] = {
    "k": 1 << 10,
    "m": 1 << 20,
    "g": 1 << 30,
    "t": 1 << 40,
}


def format_bytes_human(n: int | None) -> str:
    """Format a byte count in binary IEC units, or '-' when None."""
    if n is None:
        return "-"
    for unit, size in (("GiB", 2**30), ("MiB", 2**20), ("KiB", 2**10)):
        if abs(n) >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def format_bytes_gib_mib(
    n: float,
    *,
    gib_threshold: float = 2**30,
    mib_decimals: int = 0,
    comma: bool = False,
    mib_floor: bool = True,
) -> str:
    """Format a byte count as GiB (>= threshold, one decimal) or MiB.

    MiB with no decimals: an int count is floored (1.9 MiB -> "1 MiB") unless
    mib_floor=False, which rounds like a float (-> "2 MiB").
    """
    if abs(n) >= gib_threshold:
        return f"{n / 2**30:,.1f} GiB" if comma else f"{n / 2**30:.1f} GiB"
    if mib_decimals == 0 and isinstance(n, int) and mib_floor:
        val = n // 2**20
        return f"{val:,} MiB" if comma else f"{val} MiB"
    fmt = f",.{mib_decimals}f" if comma else f".{mib_decimals}f"
    return f"{n / 2**20:{fmt}} MiB"


def format_bytes_decimal(n: float) -> str:
    """Format a byte count in decimal SI units (B, KB, MB, GB, TB)."""
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1000 or unit == "TB":
            if unit == "B":
                shown = int(val) if val.is_integer() else val
                return f"{shown} B"
            return f"{val:.1f} {unit}"
        val /= 1000
    return f"{val} B"


def _whole_number(digits: str) -> int | None:
    """int(DIGITS), or None for more digits than int() reads (sys.get_int_max_str_digits)."""
    try:
        return int(digits)
    except ValueError:
        return None


def parse_whole_number(text: str) -> int | None:
    """TEXT as a whole number when it is ASCII digits only ("060" is 60); None for anything else.

    None for a sign, blanks, "_", a digit int() refuses (U+00B2, superscript two), a digit of another
    script (U+0663, Arabic-Indic three, which int() would read as 3), or more digits than int() reads
    (sys.get_int_max_str_digits): never a ValueError. tests/run_unit.py keeps a copy of this rule
    (_ascii_whole_number), since it cannot import cst_core: a change here changes it too.
    """
    if not (text.isascii() and text.isdigit()):
        return None
    return _whole_number(text)


@dataclass(frozen=True, slots=True)
class ByteSize:
    """Non-negative byte quantity with binary unit parsing and formatting."""

    bytes_count: int

    def __post_init__(self) -> None:
        if self.bytes_count < 0:
            raise DomainValidationError(f"ByteSize must be non-negative, got {self.bytes_count}")

    @classmethod
    def parse(cls, spec: str | None) -> Self | None:
        """Parse a systemd-style binary size ('48G', '512M', '1024'), or None if invalid or above
        _SIZE_MAX (2**64 - 1, the largest size systemd takes)."""
        m = re.fullmatch(r"\s*(\d+)\s*([KMGT]?)\s*", spec or "")
        number = _whole_number(m.group(1)) if m else None
        if m is None or number is None:
            return None
        count = number * 1024 ** " KMGT".index(m.group(2) or " ")
        return cls(count) if count <= _SIZE_MAX else None

    @classmethod
    def parse_iec(cls, spec: str) -> Self:
        """Parse '<float>[K|M|G|T][i][B]' or '0'/'off'/'no'/'none' into ByteSize."""
        t = spec.strip().lower()
        if re.fullmatch(r"0+(?:\.0*)?|off|none|no", t):
            return cls(0)
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt])(?:i?b)?", t)
        if not m:
            raise DomainValidationError(
                f"'{spec}' is not a size (a number with K, M, G or T, e.g. 150G; 0 or off for no cap)"
            )
        count = float(m.group(1)) * _SIZE_UNITS[m.group(2)]
        if not math.isfinite(count):  # int() of an infinite float raises OverflowError
            raise DomainValidationError(f"'{spec}' is too large for a size")
        return cls(int(count))

    def human(self) -> str:
        """Format this byte quantity in human-readable binary units."""
        return format_bytes_human(self.bytes_count)


@dataclass(frozen=True, slots=True)
class Duration:
    """Non-negative duration in seconds parsed from '<int>[s|m|h|d|w]'."""

    seconds: int

    def __post_init__(self) -> None:
        if self.seconds < 0:
            raise DomainValidationError(f"Duration must be non-negative, got {self.seconds}")

    @classmethod
    def parse(cls, spec: str) -> Self:
        """Parse '<int>[s|m|h|d|w]' into a Duration, raising DomainValidationError if invalid."""
        m = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", spec)
        number = _whole_number(m.group(1)) if m else None
        if m is None or number is None:
            raise DomainValidationError(f"invalid duration {spec!r} (expected integer with s, m, h, d or w)")
        return cls(number * _DURATION_UNITS[m.group(2)])

    @classmethod
    def parse_float(cls, spec: str, *, allow_unitless: bool = False, positive_only: bool = False) -> float:
        """Parse a possibly fractional duration into seconds as float."""
        pattern = r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*" if allow_unitless else r"\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*"
        m = re.fullmatch(pattern, spec, re.IGNORECASE)
        if not m:
            raise DomainValidationError(f"invalid duration {spec!r}")
        unit = m.group(2).lower() or "s"
        total = float(m.group(1)) * _DURATION_UNITS[unit]
        if not math.isfinite(total):
            raise DomainValidationError(f"duration too large: {spec!r}")
        if positive_only and total <= 0:
            raise DomainValidationError(f"duration must be positive: {spec!r}")
        return total


@dataclass(frozen=True, slots=True)
class CpuSet:
    """Non-empty set of non-negative CPU indices parsed from a Linux CPU range list."""

    cpus: frozenset[int]

    def __post_init__(self) -> None:
        if not self.cpus:
            raise DomainValidationError("CpuSet must not be empty")
        if any(cpu < 0 for cpu in self.cpus):
            raise DomainValidationError("CpuSet entries must be non-negative")

    @classmethod
    def parse(cls, spec: str) -> Self:
        """Parse a CPU range specification such as '0-3,6' into a CpuSet."""
        cleaned = spec.strip()
        if not cleaned:
            raise DomainValidationError("CpuSet specification must not be empty")
        selected: set[int] = set()
        for part in cleaned.split(","):
            token = part.strip()
            if not token:
                raise DomainValidationError(f"invalid empty range element in {spec!r}")
            if "-" in token:
                selected.update(_parse_range(token, spec))
            else:
                if not _is_index(token):
                    raise DomainValidationError(f"invalid CPU index {token!r} in {spec!r}")
                selected.add(int(token))
        return cls(frozenset(selected))

    def sorted_cpus(self) -> tuple[int, ...]:
        """Return CPU indices in ascending order."""
        return tuple(sorted(self.cpus))


def _is_index(text: str) -> bool:
    """A CPU index as the kernel writes one: ASCII digits only (str.isdigit also takes "²", which int() refuses)."""
    return text.isascii() and text.isdigit()


def _parse_range(token: str, spec: str) -> range:
    """The CPUs of one "a-b" element (token) of the list spec; DomainValidationError when token is
    not two indices around one dash, or when the range descends."""
    start_s, sep, end_s = token.partition("-")
    if not sep or "-" in end_s or not _is_index(start_s) or not _is_index(end_s):
        raise DomainValidationError(f"invalid CPU range {token!r} in {spec!r}")
    start, end = int(start_s), int(end_s)
    if start > end:
        raise DomainValidationError(f"descending CPU range {token!r} in {spec!r}")
    return range(start, end + 1)


def format_cpulist(cpus: Iterable[int]) -> str:
    """Format CPU indices as a compact Linux cpulist range string (e.g. `0-3,8-11`; "" for none): the
    syntax CpuSet.parse reads, as cpu irq writes IRQBALANCE_BANNED_CPULIST and cst perf shows CPU lists."""
    ordered = sorted(set(cpus))
    if not ordered:
        return ""
    ranges: list[str] = []
    start = prev = ordered[0]
    for cpu in ordered[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = cpu
    ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


@dataclass(frozen=True, slots=True)
class OctalMode:
    """POSIX file mode bits (0..0o7777)."""

    bits: int

    def __post_init__(self) -> None:
        if not (0 <= self.bits <= 0o7777):
            raise DomainValidationError(f"OctalMode out of range: {self.bits:#o}")

    @classmethod
    def parse(cls, spec: str | int) -> Self:
        """Parse an octal string ('0644') or integer mode into an OctalMode."""
        if isinstance(spec, bool):
            raise DomainValidationError(f"invalid mode {spec!r}")
        if isinstance(spec, int):
            return cls(spec)
        token = spec.strip()
        if not re.fullmatch(r"[0-7]{3,4}", token):
            raise DomainValidationError(f"invalid octal mode {spec!r}")
        return cls(int(token, 8))

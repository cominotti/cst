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


# The ASCII blanks str.strip() removes, apart from the separators U+001C-U+001F: space, tab, newline,
# carriage return, form feed, vertical tab. str.strip() with no argument also removes U+0085, U+00A0
# (no-break space), U+3000 (ideographic space) and other Unicode blanks, which neither bash nor systemd
# treats as blanks. A word read from a settings file or argv is trimmed of these only
# (cst_core.domain.text.strip_ascii_blanks).
ASCII_BLANKS: Final = " \t\n\r\f\v"


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


def parse_integer(text: str) -> int | None:
    """TEXT as an integer: one optional leading "-", then what parse_whole_number takes ("-0" is 0).

    None for anything else: "+5", "--5", "-" alone, blanks, a digit that is not ASCII, or more digits
    than int() reads. Never a ValueError.
    """
    if text.startswith("-"):
        number = parse_whole_number(text[1:])
        return None if number is None else -number
    return parse_whole_number(text)


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
        _SIZE_MAX (2**64 - 1, the largest size systemd takes). ASCII digits and blanks only, as systemd reads
        them (re.ASCII: the digit class is 0-9 only)."""
        m = re.fullmatch(r"\s*(\d+)\s*([KMGT]?)\s*", spec or "", re.ASCII)
        number = _whole_number(m.group(1)) if m else None
        if m is None or number is None:
            return None
        count = number * 1024 ** " KMGT".index(m.group(2) or " ")
        return cls(count) if count <= _SIZE_MAX else None

    @classmethod
    def parse_iec(cls, spec: str) -> Self:
        """Parse '<float>[K|M|G|T][i][B]' or '0'/'off'/'no'/'none' into ByteSize.

        ASCII only, as bash and systemd read a size: a digit of another script ("٣G"), a Unicode blank or a
        letter lower() would fold to an ASCII one (the Kelvin sign K) is no size."""
        m = None
        if spec.isascii():
            t = spec.strip(ASCII_BLANKS).lower()  # not U+001C-U+001F, which str.strip() also drops
            if re.fullmatch(r"0+(?:\.0*)?|off|none|no", t, re.ASCII):
                return cls(0)
            m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmgt])(?:i?b)?", t, re.ASCII)
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
        """Parse '<int>[s|m|h|d|w]' into a Duration, raising DomainValidationError if invalid (ASCII digits,
        blanks and letters only: re.ASCII)."""
        m = re.fullmatch(r"\s*(\d+)\s*([smhdw])\s*", spec, re.ASCII)
        number = _whole_number(m.group(1)) if m else None
        if m is None or number is None:
            raise DomainValidationError(f"invalid duration {spec!r} (expected integer with s, m, h, d or w)")
        return cls(number * _DURATION_UNITS[m.group(2)])

    @classmethod
    def parse_float(cls, spec: str, *, allow_unitless: bool = False, positive_only: bool = False) -> float:
        """Parse a possibly fractional duration into seconds as float.

        ASCII digits, blanks and unit letters only (re.ASCII: without it IGNORECASE also took the long s,
        U+017F, for "s", a KeyError on _DURATION_UNITS)."""
        pattern = r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*" if allow_unitless else r"\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*"
        m = re.fullmatch(pattern, spec, re.IGNORECASE | re.ASCII)
        if not m:
            raise DomainValidationError(f"invalid duration {spec!r}")
        unit = m.group(2).lower() or "s"
        total = float(m.group(1)) * _DURATION_UNITS[unit]
        if not math.isfinite(total):
            raise DomainValidationError(f"duration too large: {spec!r}")
        if positive_only and total <= 0:
            raise DomainValidationError(f"duration must be positive: {spec!r}")
        return total


# The first CPU index no CPU list may hold: no AllowedCPUs=, CPUAffinity= or kernel cpulist names one.
# systemd's src/shared/cpu-set-util.c: CPU_SET_MAX_NCPU is 8192 ("As of kernel 5.1, CONFIG_NR_CPUS can be
# set to 8192 on PowerPC") and cpu_set_add returns -ERANGE for an index (or a range end) >= it. Linux's
# arch/x86/Kconfig: NR_CPUS_RANGE_END is 8192 with MAXSMP or CPUMASK_OFFSTACK, the largest NR_CPUS of x86_64.
_CPU_INDEX_LIMIT: Final = 8192


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
        """Parse a CPU range specification such as '0-3,6' into a CpuSet. ASCII blanks around the list and
        around each element are dropped; a no-break space or another Unicode blank is an invalid element
        (systemd's AllowedCPUs= refuses it too). An index of _CPU_INDEX_LIMIT (8192) or more is invalid, as systemd
        refuses it (a range end too, before the range is built)."""
        cleaned = spec.strip(ASCII_BLANKS)
        if not cleaned:
            raise DomainValidationError("CpuSet specification must not be empty")
        selected: set[int] = set()
        for part in cleaned.split(","):
            token = part.strip(ASCII_BLANKS)
            if not token:
                raise DomainValidationError(f"invalid empty range element in {spec!r}")
            if "-" in token:
                selected.update(_parse_range(token, spec))
            else:
                index = parse_whole_number(token)
                if index is None or index >= _CPU_INDEX_LIMIT:
                    raise DomainValidationError(f"invalid CPU index {token!r} in {spec!r}")
                selected.add(index)
        return cls(frozenset(selected))

    def sorted_cpus(self) -> tuple[int, ...]:
        """Return CPU indices in ascending order."""
        return tuple(sorted(self.cpus))


def _parse_range(token: str, spec: str) -> range:
    """The CPUs of one "a-b" element (token) of the list spec; DomainValidationError when token is
    not two indices around one dash, or when the range descends. An index is what parse_whole_number
    reads: ASCII digits (str.isdigit also takes "²", which int() refuses) within int()'s digit limit."""
    start_s, sep, end_s = token.partition("-")
    start = parse_whole_number(start_s) if sep and "-" not in end_s else None
    end = parse_whole_number(end_s) if start is not None else None
    if start is None or end is None:
        raise DomainValidationError(f"invalid CPU range {token!r} in {spec!r}")
    if start > end:
        raise DomainValidationError(f"descending CPU range {token!r} in {spec!r}")
    if end >= _CPU_INDEX_LIMIT:
        raise DomainValidationError(f"CPU range {token!r} in {spec!r} beyond the CPU limit {_CPU_INDEX_LIMIT - 1}")
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

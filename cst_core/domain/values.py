"""Pure domain value objects with invariant enforcement on construction."""

from dataclasses import dataclass
import re
from typing import Final, Self


class DomainValidationError(ValueError):
    """Raised when a domain value object invariant is violated."""


_DURATION_UNITS: Final[dict[str, int]] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}

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
    n: int | float,
    *,
    gib_threshold: float = 2**30,
    mib_decimals: int = 0,
    comma: bool = False,
) -> str:
    """Format a byte count as GiB (>= threshold) or MiB."""
    if abs(n) >= gib_threshold:
        return f"{n / 2**30:,.1f} GiB" if comma else f"{n / 2**30:.1f} GiB"
    if mib_decimals == 0 and isinstance(n, int):
        val = n // 2**20
        return f"{val:,} MiB" if comma else f"{val} MiB"
    fmt = f",.{mib_decimals}f" if comma else f".{mib_decimals}f"
    return f"{n / 2**20:{fmt}} MiB"


def format_bytes_decimal(n: float) -> str:
    """Format a byte count in decimal SI units (B, KB, MB, GB, TB)."""
    val = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1000 or unit == "TB":
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val) if val.is_integer() else val} B"
        val /= 1000
    return f"{val} B"


@dataclass(frozen=True, slots=True)
class ByteSize:
    """Non-negative byte quantity with binary unit parsing and formatting."""

    bytes_count: int

    def __post_init__(self) -> None:
        if self.bytes_count < 0:
            raise DomainValidationError(f"ByteSize must be non-negative, got {self.bytes_count}")

    @classmethod
    def parse(cls, spec: str | None) -> Self | None:
        """Parse a systemd-style binary size ('48G', '512M', '1024'), or None if invalid."""
        m = re.fullmatch(r"\s*(\d+)\s*([KMGT]?)\s*", spec or "")
        if not m:
            return None
        count = int(m.group(1)) * 1024 ** " KMGT".index(m.group(2) or " ")
        return cls(count)

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
        return cls(int(float(m.group(1)) * _SIZE_UNITS[m.group(2)]))

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
        if not m:
            raise DomainValidationError(f"invalid duration {spec!r} (expected integer with s, m, h, d or w)")
        return cls(int(m.group(1)) * _DURATION_UNITS[m.group(2)])

    @classmethod
    def parse_float(cls, spec: str, *, allow_unitless: bool = False, positive_only: bool = False) -> float:
        """Parse a possibly fractional duration into seconds as float."""
        pattern = r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*" if allow_unitless else r"\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*"
        m = re.fullmatch(pattern, spec, re.I)
        if not m:
            raise DomainValidationError(f"invalid duration {spec!r}")
        unit = m.group(2).lower() or "s"
        total = float(m.group(1)) * _DURATION_UNITS[unit]
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
                start_s, sep, end_s = token.partition("-")
                if not sep or "-" in end_s or not start_s.isdigit() or not end_s.isdigit():
                    raise DomainValidationError(f"invalid CPU range {token!r} in {spec!r}")
                start, end = int(start_s), int(end_s)
                if start > end:
                    raise DomainValidationError(f"descending CPU range {token!r} in {spec!r}")
                selected.update(range(start, end + 1))
            else:
                if not token.isdigit():
                    raise DomainValidationError(f"invalid CPU index {token!r} in {spec!r}")
                selected.add(int(token))
        return cls(frozenset(selected))

    def sorted_cpus(self) -> tuple[int, ...]:
        """Return CPU indices in ascending order."""
        return tuple(sorted(self.cpus))


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

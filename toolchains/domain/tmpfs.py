# SPDX-License-Identifier: MPL-2.0
"""Pure domain rules for the dedicated `tmpfs` cargo `build-dir` mount (`cst toolchains tmpfs`)."""

from __future__ import annotations

import dataclasses
from enum import StrEnum
import posixpath
import re
from typing import TYPE_CHECKING, Final

from cst_core.domain.contracts import (
    CARGO_BUILD_TMPFS_MARKER,
    CARGO_BUILD_TMPFS_REL,
    CARGO_BUILD_TMPFS_TMPFILES_PREFIX,
)
from cst_core.domain.errors import Refused
from cst_core.domain.text import strip_ascii_blanks, unescape_mount_field
from cst_core.domain.units import render_unit_file
from cst_core.domain.values import GIB, MIB, ByteSize, DomainValidationError, parse_whole_number
from toolchains.domain.common import gib

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cst_core.domain.files import UnitFileState, WholeFileState


class TmpfsAction(StrEnum):
    """Subcommands of `cst toolchains tmpfs`."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"


TMPFS_CAP_MAX_BYTES: Final = 16 * GIB
TMPFS_CAP_MIN_BYTES: Final = 512 * MIB
TMPFS_RAM_PERCENT: Final = 20
TMPFS_MAX_RAM_PERCENT: Final = 50
TMPFS_FSTYPE: Final = "tmpfs"
_KIB_BYTES: Final = 1 << 10
_TMPFS_PERCENT_DIVISOR: Final = 100
_PROC_MOUNTS_MIN_FIELDS: Final = 4
_SIZE_MATCH_SLACK_BYTES: Final = 4 * _KIB_BYTES
_TMPFS_UNIT_HEADER: Final = (
    "# SPDX-License-Identifier: MPL-2.0",
    CARGO_BUILD_TMPFS_MARKER,
)


@dataclasses.dataclass(frozen=True, slots=True)
class TmpfsTargetUser:
    """Canonical user account and mountpoint paths for cargo build-dir tmpfs."""

    user: str
    group: str
    uid: int
    gid: int
    home: str
    mountpoint: str
    cache_parent: str

    @classmethod
    def from_home(cls, *, user: str, group: str, uid: int, gid: int, real_home: str) -> TmpfsTargetUser:
        """Build target paths from canonical real_home."""
        clean_home = posixpath.normpath(real_home)
        mountpoint = posixpath.join(clean_home, CARGO_BUILD_TMPFS_REL)
        return cls(
            user=user,
            group=group,
            uid=uid,
            gid=gid,
            home=clean_home,
            mountpoint=mountpoint,
            cache_parent=posixpath.dirname(mountpoint),
        )


@dataclasses.dataclass(frozen=True, slots=True)
class TmpfsMountInfo:
    """Observed live mount at the tmpfs mountpoint."""

    fstype: str
    options: Mapping[str, str]
    capacity_bytes: int | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class TmpfsSnapshot:
    """Current host and user state for `cst toolchains tmpfs`."""

    target: TmpfsTargetUser
    unit_name: str
    unit_path: str
    tmpfiles_path: str
    unit_state: UnitFileState
    tmpfiles_state: WholeFileState
    unit_active: str
    unit_enabled: str
    mount: TmpfsMountInfo | None
    mem_total_bytes: int | None
    cargo_uses_tmpfs: bool


def mem_total_from_meminfo(text: str) -> int | None:
    """Total RAM in bytes parsed from `/proc/meminfo` (`MemTotal: N kB`), or None."""
    for line in text.splitlines():
        if not line.startswith("MemTotal:"):
            continue
        rest = strip_ascii_blanks(line.split(":", 1)[1])
        parts = rest.split()
        if not parts:
            return None
        val = parse_whole_number(parts[0])
        if val is None or val <= 0:
            return None
        unit = parts[1].lower() if len(parts) > 1 else ""
        return val * _KIB_BYTES if unit in ("", "kb") else None
    return None


def recommended_tmpfs_bytes(mem_total_bytes: int | None) -> int:
    """Adaptive tmpfs size cap: min(16 GiB, 20% of RAM), rounded down to GiB or MiB."""
    if mem_total_bytes is None or mem_total_bytes <= 0:
        raise Refused("cannot determine system RAM from /proc/meminfo; pass --size SIZE")
    raw = min(TMPFS_CAP_MAX_BYTES, (mem_total_bytes * TMPFS_RAM_PERCENT) // _TMPFS_PERCENT_DIVISOR)
    rounded = (raw // GIB) * GIB if raw >= GIB else (raw // MIB) * MIB
    if rounded < TMPFS_CAP_MIN_BYTES:
        raise Refused(
            f"system RAM ({gib(mem_total_bytes)}) is too small for automatic tmpfs "
            f"(20% is under {gib(TMPFS_CAP_MIN_BYTES)}); keep builds on NVMe or pass --size SIZE"
        )
    return rounded


def validate_tmpfs_size(size_bytes: int, mem_total_bytes: int | None) -> int:
    """Check explicit or resolved tmpfs size against safety bounds."""
    if size_bytes < TMPFS_CAP_MIN_BYTES:
        raise Refused(f"tmpfs size {gib(size_bytes)} is below the minimum of {gib(TMPFS_CAP_MIN_BYTES)}")
    if mem_total_bytes is not None and mem_total_bytes > 0:
        limit = (mem_total_bytes * TMPFS_MAX_RAM_PERCENT) // _TMPFS_PERCENT_DIVISOR
        if size_bytes > limit:
            raise Refused(
                f"tmpfs size {gib(size_bytes)} exceeds {TMPFS_MAX_RAM_PERCENT}% of RAM ({gib(mem_total_bytes)})"
            )
    return size_bytes


def format_tmpfs_size_opt(size_bytes: int) -> str:
    """Kernel/systemd tmpfs `size=` option in whole G, M, or K."""
    if size_bytes % GIB == 0:
        return f"{size_bytes // GIB}G"
    if size_bytes % MIB == 0:
        return f"{size_bytes // MIB}M"
    return f"{-(-size_bytes // _KIB_BYTES)}K"


def tmpfs_mount_options(target: TmpfsTargetUser, size_bytes: int) -> str:
    """Mount options for CST's dedicated per-user cargo build tmpfs."""
    return f"mode=0700,uid={target.uid},gid={target.gid},size={format_tmpfs_size_opt(size_bytes)},nosuid,nodev,noatime"


def tmpfiles_path_for(user: str) -> str:
    """Path of CST's tmpfiles.d rule for USER's tmpfs mountpoint."""
    return f"{CARGO_BUILD_TMPFS_TMPFILES_PREFIX}{user}.conf"


def render_tmpfs_mount_unit(target: TmpfsTargetUser, size_bytes: int) -> str:
    """Render the systemd `.mount` unit for TARGET's cargo build tmpfs."""
    return render_unit_file(
        _TMPFS_UNIT_HEADER,
        [
            (
                "Unit",
                [
                    f"Description=Cargo build-dir tmpfs for {target.user}",
                    f"RequiresMountsFor={target.cache_parent}",
                ],
            ),
            (
                "Mount",
                [
                    "What=tmpfs",
                    f"Where={target.mountpoint}",
                    "Type=tmpfs",
                    f"Options={tmpfs_mount_options(target, size_bytes)}",
                    "DirectoryMode=0700",
                ],
            ),
            ("Install", ["WantedBy=local-fs.target"]),
        ],
    )


def render_tmpfs_tmpfiles(target: TmpfsTargetUser) -> str:
    """Render the `/etc/tmpfiles.d/*.conf` rule ensuring 0700 user ownership."""
    return (
        "\n".join(
            [
                *_TMPFS_UNIT_HEADER,
                f"d {target.mountpoint} 0700 {target.user} {target.group} -",
            ]
        )
        + "\n"
    )


def _parse_mount_options(raw_opts: str) -> dict[str, str]:
    opts: dict[str, str] = {}
    for tok in raw_opts.split(","):
        if not tok:
            continue
        k, sep, v = tok.partition("=")
        opts[k] = v if sep else ""
    return opts


def find_mount_in_proc_mounts(mounts_text: str, mountpoint: str) -> tuple[str, dict[str, str]] | None:
    """Topmost mount entry in `/proc/mounts` matching canonical MOUNTPOINT."""
    target = posixpath.normpath(mountpoint)
    found: tuple[str, dict[str, str]] | None = None
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < _PROC_MOUNTS_MIN_FIELDS:
            continue
        if posixpath.normpath(unescape_mount_field(fields[1])) == target:
            found = (fields[2], _parse_mount_options(fields[3]))
    return found


def _mount_size_matches(mount: TmpfsMountInfo, size_bytes: int) -> bool:
    size_tok = mount.options.get("size")
    if size_tok is not None:
        try:
            mounted_bytes = ByteSize.parse_iec(size_tok).bytes_count
        except DomainValidationError:
            return False
        return abs(mounted_bytes - size_bytes) <= _SIZE_MATCH_SLACK_BYTES
    if mount.capacity_bytes is None:
        return False
    return abs(mount.capacity_bytes - size_bytes) <= MIB


def mount_matches_desired(mount: TmpfsMountInfo | None, target: TmpfsTargetUser, size_bytes: int) -> bool:
    """True when MOUNT is an active tmpfs with the expected owner, mode 0700, flags, and size cap."""
    if mount is None or mount.fstype != TMPFS_FSTYPE:
        return False
    required_flags = ("nosuid", "nodev", "noatime")
    if any(flag not in mount.options for flag in required_flags):
        return False
    mode_tok = mount.options.get("mode")
    if mode_tok is None or not re.fullmatch(r"0*700", mode_tok):
        return False
    if mount.options.get("uid") != str(target.uid) or mount.options.get("gid") != str(target.gid):
        return False
    return _mount_size_matches(mount, size_bytes)

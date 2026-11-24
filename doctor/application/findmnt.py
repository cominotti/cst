# SPDX-License-Identifier: MPL-2.0
"""The live mounts as `findmnt` reports them, in /proc/mounts' line format (its octal escapes), for the
btrfs area of `cst doctor`: findmnt sees the host's mounts from a toolbox too."""

import re

from btrfs.compress.domain import parse_proc_mounts, pick_root_mounts
from cst_core.domain.text import group, unescape_mount_field
from doctor.ports import DoctorHostPort


def _findmnt_to_octal_field(field: str) -> str:
    """Convert `findmnt` hex escapes (`\\x20`) to `/proc/mounts` octal escapes (`\\040`)."""
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: f"\\{int(group(m, 1), 16):03o}", field)


def findmnt_root_btrfs_info(host: DoctorHostPort) -> tuple[str | None, str]:
    """The live btrfs mounts as /proc/mounts lines, and the root filesystem's UUID: that of the mounts
    pick_root_mounts takes (the device of /sysroot, else /, else /var), never a separate btrfs /home's."""
    rc, out, _ = host.run(["findmnt", "-rn", "-t", "btrfs", "-o", "SOURCE,TARGET,OPTIONS,UUID"], timeout=15)
    if rc != 0 or not out.strip():
        return None, ""
    lines: list[str] = []
    uuids: list[tuple[str, str]] = []  # (target, UUID) of each line that has one
    for raw in out.splitlines():
        match raw.strip().split(None, 3):
            case [src, tgt, opts, *rest]:  # rest: the UUID, when the line has one
                esc_src, esc_tgt = _findmnt_to_octal_field(src), _findmnt_to_octal_field(tgt)
                lines.append(f"{esc_src} {esc_tgt} btrfs {opts} 0 0\n")
                if rest:
                    uuids.append((unescape_mount_field(esc_tgt), rest[0]))
    text = "".join(lines)
    root_targets = {m.target for m in pick_root_mounts(parse_proc_mounts(text))}
    root_uuid = next((uuid for tgt, uuid in uuids if tgt in root_targets), "")
    return (text or None), root_uuid


def findmnt_all_mounts(host: DoctorHostPort) -> str | None:
    rc, out, _ = host.run(["findmnt", "-rn", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS"], timeout=15)
    if rc != 0 or not out.strip():
        return None
    lines: list[str] = []
    for raw in out.splitlines():
        match raw.strip().split(None, 3):
            case [src, tgt, fstype, opts]:
                lines.append(f"{_findmnt_to_octal_field(src)} {_findmnt_to_octal_field(tgt)} {fstype} {opts} 0 0\n")
    return "".join(lines) or None

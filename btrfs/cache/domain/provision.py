# SPDX-License-Identifier: MPL-2.0
"""Pure domain model for NVMe cache drive provisioning (`provision`, `unprovision`) and `/etc/cst/btrfs-cache.conf`.

No I/O: parses hardware configuration, validates partitions and anti-wipe safety rules, plans
byte-transparent edits to `/etc/crypttab` and `/etc/fstab` base lines, and formats plan/status output.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Final

from btrfs.cache.domain import (
    CACHE_BASE,
    FSTYPE_BTRFS,
    OPT_COMPRESS_FORCE,
    PROVISION_BASE_MARKER,
    SCRATCHPAD_BASE,
    CacheMountKind,
    ManagedFstabEntry,
    escape_mount_field,
    has_mount_option,
    parse_all_mounts,
    split_tagged_fstab_line,
    upsert_marked_lines,
)
from btrfs.cache.domain.health import CRYPTTAB_MIN_FIELDS
from btrfs.cache.domain.system import ManagedSystemEntry
from btrfs.compress.domain import FSTAB_PATH
from cst_core.domain.text import (
    file_lines,
    parse_shell_assignments,
    split_ascii_blanks,
    unescape_mount_field,
)
from cst_core.domain.units import SYSTEMD_WHITESPACE, systemd_lines
from cst_core.domain.values import parse_whole_number

CACHE_CONF_PATH: Final = "/etc/cst/btrfs-cache.conf"
CRYPTTAB_PATH: Final = "/etc/crypttab"
KEYS_DIR: Final = "/etc/cryptsetup-keys.d"
DEFAULT_MAPPER_NAME: Final = "cache"
DEFAULT_SUBVOL_TOP_PREFIX: Final = "cst-btrfs-cache-top-"

PROVISION_CRYPTTAB_MARKER: Final = "# managed-by: btrfs-cache (CST) provision=crypttab"
CRYPTTAB_OPTIONS: Final = "nofail,discard,no-read-workqueue,no-write-workqueue"
BASE_MOUNT_OPTIONS: Final = f"noatime,discard=async,{OPT_COMPRESS_FORCE},commit=60,nofail"
BTRFS_CACHE_LABEL: Final = "cache"

KEYFILE_BYTES: Final = 64
KEYFILE_MODE: Final = 0o400
KEYS_DIR_MODE: Final = 0o700
CONF_MODE: Final = 0o644
CONF_DIR_MODE: Final = 0o755
CRYPTTAB_MODE: Final = 0o600
BASE_DIR_MODE: Final = 0o755

SUBVOL_FOR_KIND: Final[Mapping[CacheMountKind, str]] = {
    CacheMountKind.CACHE: "cache",
    CacheMountKind.SCRATCHPAD: "scratchpad",
}
BASE_PATHS_TUPLE: Final = (CACHE_BASE, SCRATCHPAD_BASE)

_BY_PARTUUID_DIR: Final = "/dev/disk/by-partuuid/"
_CONF_KEYS: Final = ("CACHE_PARTUUID", "CACHE_SIZE_BYTES", "CACHE_MAPPER_NAME")
_PARTUUID_RE: Final = re.compile(r"[0-9a-f]{8}-(?:(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|[0-9a-f]{2})")
_MAPPER_NAME_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_SYSTEM_MOUNTS: Final = frozenset({"/", "/sysroot", "/var", "/var/home", "/home", "/boot", "/boot/efi"})
NOT_REGULAR_FILE_NOTE: Final = "not a regular file (a FIFO, a device, a folder, or a link to one)"


@dataclass(frozen=True, slots=True)
class CacheHardwareSpec:
    """Identity and exact expected size of the cache partition (from `/etc/cst/btrfs-cache.conf`)."""

    partuuid: str
    size_bytes: int
    mapper_name: str = DEFAULT_MAPPER_NAME

    @property
    def device_path(self) -> str:
        """Stable GPT/MBR PARTUUID symlink for the backing partition."""
        return f"{_BY_PARTUUID_DIR}{self.partuuid}"

    @property
    def mapper_path(self) -> str:
        """Mapped dm-crypt device path."""
        return f"/dev/mapper/{self.mapper_name}"

    @property
    def keyfile_path(self) -> str:
        """Root-only keyfile path used to unlock the cache partition at boot."""
        return f"{KEYS_DIR}/{self.mapper_name}.key"


@dataclass(frozen=True, slots=True)
class ConfParseResult:
    """Parsed `/etc/cst/btrfs-cache.conf`: `spec` when valid, else `(None, problems)`."""

    spec: CacheHardwareSpec | None
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BlockDeviceFacts:
    """Read-only hardware facts probed for a partition (`lsblk`, `blkid`, sysfs, `/proc/swaps`)."""

    dev_path: str
    real_path: str
    dev_type: str
    partuuid: str
    size_bytes: int
    model: str = ""
    fstype: str = ""
    pttype: str = ""
    uuid: str = ""
    mountpoints: tuple[str, ...] = ()
    holders: tuple[str, ...] = ()
    holder_dm_names: tuple[str, ...] = ()
    is_swap: bool = False


@dataclass(frozen=True, slots=True)
class ProvisionBaseState:
    """Observed host state for `/var/mnt/cache` or `/var/mnt/scratchpad`."""

    kind: CacheMountKind
    dir_exists: bool
    dir_is_symlink: bool
    dir_not_dir: bool
    mounted_source: str = ""
    mounted_fstype: str = ""
    mounted_uuid: str = ""
    mounted_fsroot: str = ""
    mounted_options: str = ""
    fstab_source: str = ""
    fstab_managed: bool = False
    fstab_unmanaged: bool = False


@dataclass(frozen=True, slots=True)
class LuksMapperState:
    """Observed state of the LUKS2 header, keyfile, dm-crypt mapper, and top-level Btrfs subvolumes."""

    keyfile_exists: bool = False
    keyfile_not_regular: bool = False
    mapper_exists: bool = False
    luks_uuid: str | None = None
    mapper_luks_uuid: str | None = None
    keyfile_unlocks_luks: bool | None = None
    mapper_fstype: str = ""
    mapper_uuid: str = ""
    mapper_label: str = ""
    subvols_present: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProvisionProbe:
    """Complete read-only state collected for `provision plan|apply` and `unprovision`."""

    conf_text: str | None
    conf_error: str | None
    spec: CacheHardwareSpec | None
    conf_problems: tuple[str, ...]
    device: BlockDeviceFacts | None
    device_error: str | None
    luks: LuksMapperState
    crypttab_text: str | None
    crypttab_error: str | None
    fstab_text: str | None
    fstab_error: str | None
    bases: Mapping[CacheMountKind, ProvisionBaseState]
    user_managed_entries: tuple[ManagedFstabEntry, ...]
    user_mounts_active: tuple[str, ...] = ()
    fstab_behind: bool | None = False
    system_managed_entries: tuple[ManagedSystemEntry, ...] = ()


def is_valid_mapper_name(name: str) -> bool:
    """Validate dm-crypt mapper name so it is safe in `/dev/mapper/NAME`, `crypttab`, `fstab`, and units."""
    return bool(_MAPPER_NAME_RE.fullmatch(name))


def is_valid_partuuid(partuuid: str) -> bool:
    """Return True when `partuuid` matches `/dev/disk/by-partuuid/...` lowercase form."""
    return bool(_PARTUUID_RE.fullmatch(partuuid))


def parse_cache_conf(text: str) -> ConfParseResult:
    """Parse `/etc/cst/btrfs-cache.conf` strictly without shell execution."""
    parsed = parse_shell_assignments(text, bash_sourced=False)
    problems = [str(p) for p in parsed.problems]
    values = parsed.values
    problems.extend(
        f"{key}: unknown setting (allowed: {', '.join(_CONF_KEYS)})" for key in values if key not in _CONF_KEYS
    )
    partuuid = values.get("CACHE_PARTUUID", "")
    size_raw = values.get("CACHE_SIZE_BYTES", "")
    mapper = values.get("CACHE_MAPPER_NAME", DEFAULT_MAPPER_NAME)
    if not partuuid:
        problems.append("CACHE_PARTUUID is required")
    elif not is_valid_partuuid(partuuid):
        problems.append(f"CACHE_PARTUUID={partuuid!r}: expected lowercase PARTUUID from /dev/disk/by-partuuid")
    if not is_valid_mapper_name(mapper):
        problems.append(f"CACHE_MAPPER_NAME={mapper!r}: letters, digits, '_', '.', '-' only")
    size_bytes = parse_whole_number(size_raw)
    if not size_raw or size_bytes is None or size_bytes == 0:
        problems.append(f"CACHE_SIZE_BYTES={size_raw!r}: exact partition size in bytes (> 0) is required")
    if problems or size_bytes is None:
        return ConfParseResult(None, tuple(problems))
    return ConfParseResult(CacheHardwareSpec(partuuid=partuuid, size_bytes=size_bytes, mapper_name=mapper))


def render_cache_conf(spec: CacheHardwareSpec, *, origin: str = "init") -> str:
    """Format `/etc/cst/btrfs-cache.conf` lines."""
    return (
        "# Dedicated NVMe cache drive managed by `cst btrfs cache` (btrfs/README.md, section 9.4).\n"
        f"# Written by cst btrfs cache provision {origin}.\n"
        "# provision apply verifies CACHE_PARTUUID and exact CACHE_SIZE_BYTES before touching the partition.\n"
        f"CACHE_MAPPER_NAME={spec.mapper_name}\n"
        f"CACHE_PARTUUID={spec.partuuid}\n"
        f"CACHE_SIZE_BYTES={spec.size_bytes}\n"
    )


def render_crypttab_line(spec: CacheHardwareSpec, luks_uuid: str) -> str:
    """Format the managed `/etc/crypttab` entry for the cache drive."""
    return f"{spec.mapper_name} UUID={luks_uuid} {spec.keyfile_path} {CRYPTTAB_OPTIONS} {PROVISION_CRYPTTAB_MARKER}"


def render_base_fstab_line(spec: CacheHardwareSpec, kind: CacheMountKind) -> str:
    """Format a managed `/etc/fstab` base mount line (`no flushoncommit`)."""
    base = CACHE_BASE if kind == CacheMountKind.CACHE else SCRATCHPAD_BASE
    subvol = SUBVOL_FOR_KIND[kind]
    return (
        f"{escape_mount_field(spec.mapper_path)} {escape_mount_field(base)} {FSTYPE_BTRFS} "
        f"subvol={subvol},{BASE_MOUNT_OPTIONS} 0 0 {PROVISION_BASE_MARKER} kind={kind.value}"
    )


def _crypttab_entry_name(raw: str) -> str | None:
    stripped = raw.strip(SYSTEMD_WHITESPACE)
    if not stripped or stripped.startswith("#"):
        return None
    fields = split_ascii_blanks(stripped)
    return fields[0] if len(fields) >= CRYPTTAB_MIN_FIELDS else None


def _is_provision_crypttab_line(raw: str, mapper_name: str | None = None) -> bool:
    if PROVISION_CRYPTTAB_MARKER not in raw:
        return False
    name = _crypttab_entry_name(raw)
    if name is None:
        return False
    return mapper_name is None or name == mapper_name


def unmanaged_crypttab_conflict(crypttab_text: str, mapper_name: str) -> bool:
    """True when `/etc/crypttab` already defines `mapper_name` without CST's provision marker."""
    for raw in systemd_lines(crypttab_text):
        if _is_provision_crypttab_line(raw):
            continue
        if _crypttab_entry_name(raw) == mapper_name:
            return True
    return False


def plan_provision_crypttab(crypttab_text: str, spec: CacheHardwareSpec, luks_uuid: str) -> str:
    """Upsert CST's managed `/etc/crypttab` line byte-transparently for all other lines."""
    desired = render_crypttab_line(spec, luks_uuid)
    return upsert_marked_lines(
        crypttab_text,
        {spec.mapper_name: desired},
        lambda raw: spec.mapper_name if _is_provision_crypttab_line(raw, spec.mapper_name) else None,
    )


def plan_unprovision_crypttab(crypttab_text: str, mapper_name: str | None = None) -> str:
    """Remove only CST's provision-managed `/etc/crypttab` line, preserving all other bytes."""
    out = [raw for raw in file_lines(crypttab_text) if not _is_provision_crypttab_line(raw, mapper_name)]
    return "\n".join(out) + "\n" if out else ""


def _parse_provision_base_line(raw: str) -> CacheMountKind | None:
    parsed = split_tagged_fstab_line(raw, PROVISION_BASE_MARKER)
    if parsed is None:
        return None
    fields, tags = parsed
    target = unescape_mount_field(fields[1])
    kind = (
        CacheMountKind.CACHE
        if tags.get("kind") == CacheMountKind.CACHE.value
        else (CacheMountKind.SCRATCHPAD if tags.get("kind") == CacheMountKind.SCRATCHPAD.value else None)
    )
    if kind is None:
        return None
    expected_target = CACHE_BASE if kind == CacheMountKind.CACHE else SCRATCHPAD_BASE
    return kind if target == expected_target else None


def unmanaged_base_fstab_conflicts(fstab_text: str) -> list[str]:
    """Return base mount targets present in `/etc/fstab` without CST's `provision=base` marker."""
    conflicts = [entry.target for entry in parse_all_mounts(fstab_text) if entry.target in BASE_PATHS_TUPLE]
    managed_targets = {
        CACHE_BASE if kind == CacheMountKind.CACHE else SCRATCHPAD_BASE
        for raw in file_lines(fstab_text)
        if (kind := _parse_provision_base_line(raw)) is not None
    }
    return sorted(set(conflicts) - managed_targets)


def has_managed_base_line(fstab_text: str, spec: CacheHardwareSpec, kind: CacheMountKind) -> bool:
    """True when `fstab_text` holds the exact CST base line for `kind`."""
    want = render_base_fstab_line(spec, kind)
    return any(raw == want and _parse_provision_base_line(raw) == kind for raw in file_lines(fstab_text))


def plan_provision_fstab(fstab_text: str, spec: CacheHardwareSpec) -> str:
    """Upsert both managed base mount lines in `/etc/fstab`, preserving all unrelated and user lines."""
    wanted = {kind: render_base_fstab_line(spec, kind) for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD)}
    return upsert_marked_lines(fstab_text, wanted, _parse_provision_base_line)


def plan_unprovision_fstab(fstab_text: str) -> str:
    """Remove only CST's `provision=base` lines from `/etc/fstab`, preserving everything else."""
    out = [raw for raw in file_lines(fstab_text) if _parse_provision_base_line(raw) is None]
    return "\n".join(out) + "\n" if out else ""


def _unexpected_holders(dev: BlockDeviceFacts, mapper_name: str) -> tuple[str, ...]:
    if not dev.holders:
        return ()
    if dev.fstype == "crypto_LUKS" and dev.holder_dm_names and all(n == mapper_name for n in dev.holder_dm_names):
        return ()
    return dev.holder_dm_names or dev.holders


def _device_common_blockers(dev: BlockDeviceFacts, mapper_name: str) -> list[str]:
    errors: list[str] = []
    if dev.dev_type != "part":
        errors.append(f"{dev.dev_path} has type {dev.dev_type or 'unknown'}, expected partition (TYPE=part)")
    if not dev.partuuid:
        errors.append(f"{dev.dev_path} has no PARTUUID (a GPT or MBR partition is required)")
    elif not is_valid_partuuid(dev.partuuid):
        errors.append(f"{dev.dev_path} has invalid PARTUUID {dev.partuuid!r}")
    if dev.size_bytes <= 0:
        errors.append(f"{dev.dev_path} has invalid size {dev.size_bytes} bytes")
    if dev.is_swap or dev.fstype == "swap":
        errors.append(f"{dev.dev_path} is active swap space")
    if dev.mountpoints:
        mounts = ", ".join(dev.mountpoints)
        note = " (system filesystem!)" if any(m in _SYSTEM_MOUNTS for m in dev.mountpoints) else ""
        errors.append(f"{dev.dev_path} is mounted at {mounts}{note}")
    unexpected = _unexpected_holders(dev, mapper_name)
    if unexpected:
        errors.append(f"{dev.dev_path} has active holder(s): {', '.join(unexpected)} (RAID/LVM/other dm-crypt)")
    return errors


def validate_device_for_init(
    dev: BlockDeviceFacts, *, existing_conf: CacheHardwareSpec | None, mapper_name: str
) -> list[str]:
    """Validate that `dev` is safe to record into `/etc/cst/btrfs-cache.conf`."""
    errors: list[str] = []
    if not is_valid_mapper_name(mapper_name):
        errors.append(f"invalid mapper name {mapper_name!r}")
    errors.extend(_device_common_blockers(dev, mapper_name))
    if existing_conf is not None and not errors:
        want = (dev.partuuid, dev.size_bytes, mapper_name)
        have = (existing_conf.partuuid, existing_conf.size_bytes, existing_conf.mapper_name)
        if want != have:
            errors.append(
                f"{CACHE_CONF_PATH} already names PARTUUID={existing_conf.partuuid} "
                f"({existing_conf.size_bytes} bytes, mapper={existing_conf.mapper_name}); remove it first to switch"
            )
    return errors


def _blank_luks_blockers(probe: ProvisionProbe, spec: CacheHardwareSpec, *, do_format: bool) -> list[str]:
    luks = probe.luks
    checks = (
        (not do_format, f"{spec.device_path} is blank/unformatted; pass --format to initialize LUKS2 and Btrfs"),
        (luks.keyfile_not_regular, f"{spec.keyfile_path} is {NOT_REGULAR_FILE_NOTE}"),
        (
            luks.keyfile_exists,
            f"{spec.keyfile_path} already exists while {spec.device_path} is blank; remove it before --format",
        ),
        (luks.mapper_exists, f"{spec.mapper_path} already exists while {spec.device_path} is blank"),
    )
    return [msg for on, msg in checks if on][:1]


def _existing_luks_blockers(probe: ProvisionProbe, spec: CacheHardwareSpec, *, do_format: bool) -> list[str]:
    luks = probe.luks
    non_luks_mapper = luks.mapper_exists and luks.mapper_luks_uuid is None
    luks_mismatch = (
        luks.mapper_exists
        and luks.luks_uuid is not None
        and luks.luks_uuid != ""
        and luks.mapper_luks_uuid is not None
        and luks.mapper_luks_uuid not in {"", luks.luks_uuid}
    )
    wrong_label = luks.mapper_fstype == FSTYPE_BTRFS and luks.mapper_label not in (
        "",
        BTRFS_CACHE_LABEL,
        spec.mapper_name,
    )
    checks = (
        (luks.keyfile_not_regular, f"{spec.keyfile_path} is {NOT_REGULAR_FILE_NOTE}"),
        (
            not luks.keyfile_exists,
            f"{spec.device_path} is already LUKS2, but {spec.keyfile_path} is missing (add/provide keyfile before apply)",
        ),
        (
            luks.keyfile_unlocks_luks is False,
            f"{spec.device_path} is LUKS2 and does not accept {spec.keyfile_path}; refusing to wipe existing LUKS2",
        ),
        (
            non_luks_mapper,
            f"{spec.mapper_path} exists and is not an open LUKS mapping",
        ),
        (
            luks_mismatch,
            f"{spec.mapper_path} is open for another LUKS UUID ({luks.mapper_luks_uuid} != {luks.luks_uuid})",
        ),
        (
            bool(luks.mapper_fstype and luks.mapper_fstype != FSTYPE_BTRFS),
            f"{spec.mapper_path} already holds filesystem {luks.mapper_fstype!r}, not {FSTYPE_BTRFS}; refusing to wipe",
        ),
        (
            wrong_label,
            f"{spec.mapper_path} holds btrfs with label {luks.mapper_label!r} (expected {BTRFS_CACHE_LABEL!r}); refusing",
        ),
        (
            bool(luks.mapper_exists and not luks.mapper_fstype and not do_format),
            f"{spec.mapper_path} has no filesystem yet; pass --format to create Btrfs subvolumes",
        ),
    )
    return [msg for on, msg in checks if on][:1]


def _format_and_luks_blockers(
    probe: ProvisionProbe, spec: CacheHardwareSpec, dev: BlockDeviceFacts, *, do_format: bool
) -> list[str]:
    if not dev.fstype and not dev.pttype:
        return _blank_luks_blockers(probe, spec, do_format=do_format)
    if dev.fstype != "crypto_LUKS":
        sig = f"fstype={dev.fstype or '-'}, pttype={dev.pttype or '-'}"
        return [
            (
                f"{spec.device_path} already has a filesystem or partition table ({sig}); "
                "refusing to wipe existing data (wipefs -a manually only if you are sure)"
            )
        ]
    return _existing_luks_blockers(probe, spec, do_format=do_format)


def _base_provision_blockers(probe: ProvisionProbe) -> list[str]:
    errors: list[str] = []
    expected_src = probe.spec.mapper_path if probe.spec is not None else ""
    for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD):
        b = probe.bases[kind]
        path = CACHE_BASE if kind == CacheMountKind.CACHE else SCRATCHPAD_BASE
        subvol = f"/{SUBVOL_FOR_KIND[kind]}"
        if b.dir_is_symlink:
            errors.append(f"{path} is a symlink (refusing to mount over a symlink)")
        elif b.dir_not_dir:
            errors.append(f"{path} exists and is not a folder")
        if b.fstab_unmanaged:
            errors.append(f"{path} has an /etc/fstab line CST does not manage; remove it first")
        if b.mounted_source:
            same_mapper_uuid = bool(probe.luks.mapper_uuid and b.mounted_uuid == probe.luks.mapper_uuid)
            same_no_uuid = (
                probe.luks.mapper_exists
                and (not probe.luks.mapper_uuid or not b.mounted_uuid)
                and bool(expected_src and b.mounted_source.split("[", 1)[0] == expected_src)
            )
            same_fs = (
                b.mounted_fstype == FSTYPE_BTRFS
                and (same_mapper_uuid or same_no_uuid)
                and b.mounted_fsroot in ("", subvol)
            )
            if not same_fs:
                errors.append(
                    f"{path} is already mounted from {b.mounted_source} ({b.mounted_fstype or 'unknown'}, "
                    f"fsroot={b.mounted_fsroot or '/'})"
                )
    return errors


def validate_provision_apply(probe: ProvisionProbe, *, do_format: bool) -> list[str]:
    """Return every blocking reason refusing `provision apply` (empty when safe to run)."""
    if probe.conf_error is not None:
        return [probe.conf_error]
    if probe.conf_problems:
        return [f"{CACHE_CONF_PATH}: {p}" for p in probe.conf_problems]
    if probe.spec is None:
        return [
            f"{CACHE_CONF_PATH} not found (run `provision plan --device ...` then `provision init --device ...` first)"
        ]
    spec = probe.spec
    errors: list[str] = []
    if probe.crypttab_error is not None:
        errors.append(probe.crypttab_error)
    if probe.fstab_error is not None:
        errors.append(probe.fstab_error)
    if probe.device_error is not None or probe.device is None:
        errors.append(probe.device_error or f"{spec.device_path} not found")
        return errors
    dev = probe.device
    errors.extend(_device_common_blockers(dev, spec.mapper_name))
    if dev.partuuid and dev.partuuid != spec.partuuid:
        errors.append(
            f"{spec.device_path} PARTUUID {dev.partuuid!r} does not match {CACHE_CONF_PATH} ({spec.partuuid!r})"
        )
    if dev.size_bytes != spec.size_bytes:
        errors.append(
            f"{spec.device_path} size {dev.size_bytes} bytes does not match {CACHE_CONF_PATH} ({spec.size_bytes} bytes)"
        )
    if not errors:
        errors.extend(_format_and_luks_blockers(probe, spec, dev, do_format=do_format))
    if probe.crypttab_text is not None and unmanaged_crypttab_conflict(probe.crypttab_text, spec.mapper_name):
        errors.append(f"{CRYPTTAB_PATH} already has an unmanaged entry for {spec.mapper_name!r}; remove it first")
    errors.extend(_base_provision_blockers(probe))
    return errors


def validate_unprovision(probe: ProvisionProbe) -> list[str]:
    """Return blocking reasons refusing `unprovision` (must detach users first; no foreign base mounts)."""
    errors: list[str] = []
    if probe.conf_error is not None:
        errors.append(probe.conf_error)
    if probe.conf_problems:
        errors.extend(f"{CACHE_CONF_PATH}: {p}" for p in probe.conf_problems)
    if probe.crypttab_error is not None:
        errors.append(probe.crypttab_error)
    if probe.fstab_error is not None:
        errors.append(probe.fstab_error)
    users = sorted({e.user for e in probe.user_managed_entries})
    if users:
        cmds = ", ".join(f"`detach --user {u}`" for u in users)
        errors.append(f"user bind mounts still configured in {FSTAB_PATH}; run {cmds} first")
    if probe.system_managed_entries:
        ids = ",".join(sorted({e.bind_id.value for e in probe.system_managed_entries}))
        errors.append(f"system bind mounts still configured in {FSTAB_PATH}; run `system revert --only {ids}` first")
    errors.extend(
        f"active mount under base path at {target}; unmount or detach it first" for target in probe.user_mounts_active
    )
    for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD):
        b = probe.bases[kind]
        path = CACHE_BASE if kind == CacheMountKind.CACHE else SCRATCHPAD_BASE
        if b.mounted_source and not b.fstab_managed:
            if b.fstab_unmanaged:
                errors.append(f"{path} has an /etc/fstab line CST does not manage; remove it first")
            elif b.mounted_fstype != FSTYPE_BTRFS:
                errors.append(f"{path} is mounted with non-btrfs filesystem ({b.mounted_fstype or 'unknown'})")
            elif probe.luks.mapper_uuid and b.mounted_uuid and b.mounted_uuid != probe.luks.mapper_uuid:
                errors.append(f"{path} is mounted from another filesystem ({b.mounted_source}, uuid={b.mounted_uuid})")
    return errors


def format_provision_device_plan(
    dev_probe: tuple[BlockDeviceFacts | None, str | None],
    *,
    existing_conf: CacheHardwareSpec | None,
    mapper_name: str,
    prog: str,
    seam_lines: Sequence[str] = (),
) -> list[str]:
    """Format dry-run inspection output for `provision plan --device PATH`."""
    dev, dev_error = dev_probe
    lines = [*seam_lines]
    if dev_error is not None or dev is None:
        return [*lines, f"BLOCKED: {dev_error or 'cannot inspect device'}"]
    spec = CacheHardwareSpec(partuuid=dev.partuuid, size_bytes=dev.size_bytes, mapper_name=mapper_name)
    gib = dev.size_bytes / (1024**3)
    lines.extend(
        [
            f"Candidate device: {dev.dev_path} (real={dev.real_path}, type={dev.dev_type or 'unknown'})",
            f"  model:     {dev.model or 'unknown'}",
            f"  size:      {dev.size_bytes} bytes ({gib:.1f} GiB)",
            f"  partuuid:  {dev.partuuid or 'none'}",
            f"  signature: fstype={dev.fstype or '-'}, pttype={dev.pttype or '-'}, uuid={dev.uuid or '-'}",
        ]
    )
    blockers = validate_device_for_init(dev, existing_conf=existing_conf, mapper_name=mapper_name)
    if blockers:
        lines.extend(f"BLOCKED: {msg}" for msg in blockers)
        return lines
    lines.append(f"Config to write to {CACHE_CONF_PATH} (via `{prog} provision init`):")
    lines.extend(f"  {row}" for row in render_cache_conf(spec).splitlines())
    lines.append(
        f"Dry run, nothing changed. Run `{prog} provision init --device {dev.dev_path}` "
        + ("" if mapper_name == DEFAULT_MAPPER_NAME else f"--mapper {mapper_name} ")
        + "to write the config (does not modify the partition)."
    )
    return lines


def needs_base_remount(base: ProvisionBaseState) -> bool:
    """True when an already-mounted base lacks `compress-force=zstd:1` in its observed mount options."""
    return bool(
        base.mounted_source and base.mounted_options and not has_mount_option(base.mounted_options, OPT_COMPRESS_FORCE)
    )


def _provision_plan_steps(probe: ProvisionProbe, spec: CacheHardwareSpec, *, do_format: bool) -> list[str]:
    steps: list[str] = []
    luks = probe.luks
    if not luks.keyfile_exists:
        steps.append(f"create root-only keyfile {spec.keyfile_path} (mode {KEYFILE_MODE:04o})")
    if probe.device is not None and not probe.device.fstype and do_format:
        steps.append(f"luksFormat (LUKS2) {spec.device_path} with {spec.keyfile_path}")
    if not luks.mapper_exists:
        steps.append(f"cryptsetup open {spec.device_path} -> {spec.mapper_name}")
    if not luks.mapper_fstype and do_format:
        steps.append(f"mkfs.btrfs -L {BTRFS_CACHE_LABEL} -m dup -d single {spec.mapper_path}")
        steps.append(f"run initial btrfs scrub on {CACHE_BASE}")
    missing_subvols = [s for s in SUBVOL_FOR_KIND.values() if s not in luks.subvols_present]
    if missing_subvols:
        steps.append(f"create btrfs subvolume(s): {', '.join(missing_subvols)}")
    crypt_ok = (
        probe.crypttab_text is not None
        and luks.luks_uuid is not None
        and render_crypttab_line(spec, luks.luks_uuid) in file_lines(probe.crypttab_text)
    )
    if not crypt_ok:
        steps.append(f"upsert managed entry in {CRYPTTAB_PATH}")
    fstab_ok = probe.fstab_text is not None and all(
        has_managed_base_line(probe.fstab_text, spec, k) for k in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD)
    )
    if not fstab_ok:
        steps.append(f"upsert base mount lines in {FSTAB_PATH} and reload systemd")
    to_mount = [k for k in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD) if not probe.bases[k].mounted_source]
    if to_mount:
        paths = ", ".join(CACHE_BASE if k == CacheMountKind.CACHE else SCRATCHPAD_BASE for k in to_mount)
        steps.append(f"mount base subvolume(s): {paths}")
    to_remount = [k for k in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD) if needs_base_remount(probe.bases[k])]
    if to_remount:
        paths = ", ".join(CACHE_BASE if k == CacheMountKind.CACHE else SCRATCHPAD_BASE for k in to_remount)
        steps.append(f"remount base subvolume(s) with {OPT_COMPRESS_FORCE}: {paths}")
    return steps


def format_provision_plan(
    probe: ProvisionProbe, prog: str, *, do_format: bool, seam_lines: Sequence[str] = ()
) -> list[str]:
    """Format dry-run lines for `cst btrfs cache provision plan`."""
    lines = [*seam_lines]
    if probe.spec is not None:
        spec = probe.spec
        lines.append(
            f"Configured partition ({CACHE_CONF_PATH}): {spec.device_path} "
            f"(size={spec.size_bytes} bytes, mapper={spec.mapper_name})"
        )
    blockers = validate_provision_apply(probe, do_format=do_format)
    if blockers:
        lines.extend(f"BLOCKED: {msg}" for msg in blockers)
        return lines
    if probe.spec is None:
        return [*lines, f"BLOCKED: {CACHE_CONF_PATH} not configured"]
    steps = _provision_plan_steps(probe, probe.spec, do_format=do_format)
    if not steps and probe.fstab_behind is False:
        return [*lines, f"Already provisioned ({CACHE_BASE}, {SCRATCHPAD_BASE}); nothing to do."]
    if not steps:
        why = (
            f"systemd has not loaded {FSTAB_PATH} as it is on disk"
            if probe.fstab_behind is True
            else f"systemctl cannot say whether systemd has loaded {FSTAB_PATH}"
        )
        lines.append(f"Base mounts are configured; {why}: provision apply runs systemctl daemon-reload.")
    else:
        lines.extend(f"  - {step}" for step in steps)
    fmt_flag = " --format" if do_format else ""
    lines.append(f"Dry run, nothing changed. Run `{prog} provision apply{fmt_flag}` to apply.")
    return lines

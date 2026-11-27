# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst desktop psd`: overlayfs profile catalog, mount inspection, and lifecycle planning."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
import posixpath
from typing import Final, TypedDict

from cst_core.domain.shapes import json_object, list_of, parse_json
from cst_core.domain.text import file_lines, mount_fields, strip_mount_blanks, unescape_mount_field
from cst_core.domain.values import DomainValidationError, format_bytes_human, parse_whole_number
from desktop.domain import (
    DEFAULT_TMPFS_BYTES,
    MAX_RAM_FRACTION_PCT,
    MAX_TMPFS_BYTES,
    MIN_TMPFS_BYTES,
    PSD_RECOVERY_REL,
    PSD_RUN_ROOT,
    is_str_val,
)

PSD_SERVICE_TEMPLATE: Final = "cst-desktop-psd@{user}.service"
PSD_RESYNC_SERVICE_TEMPLATE: Final = "cst-desktop-psd-resync@{user}.service"
PSD_RESYNC_TIMER_TEMPLATE: Final = "cst-desktop-psd-resync@{user}.timer"
PSD_SEAM_ROOT_ENV: Final = "CST_DESKTOP_PSD_ROOT"
PSD_SEAM_PROC_ENV: Final = "CST_DESKTOP_PSD_PROC"
PSD_TMPFS_FSTYPE: Final = "tmpfs"
PSD_OVERLAY_FSTYPE: Final = "overlay"
PSD_WHITEOUT_FORMAT: Final = 1
_MEMINFO_MIN_PARTS: Final = 2
_BUSY_PREVIEW_LIMIT: Final = 5


@dataclass(frozen=True, slots=True)
class KnownProfile:
    """One supported browser/Electron profile target relative to `$HOME`."""

    profile_id: str
    label: str
    rel_path: str
    auto: bool = True


KNOWN_PROFILES: Final[tuple[KnownProfile, ...]] = (
    KnownProfile("chrome-flatpak", "Google Chrome (Flatpak)", ".var/app/com.google.Chrome/config/google-chrome"),
    KnownProfile("chrome", "Google Chrome (RPM/XDG)", ".config/google-chrome"),
    KnownProfile("chromium-flatpak", "Chromium (Flatpak)", ".var/app/org.chromium.Chromium/config/chromium"),
    KnownProfile("firefox", "Firefox (XDG ~/.config)", ".config/mozilla/firefox"),
    KnownProfile("firefox-legacy", "Firefox (~/.mozilla)", ".mozilla/firefox"),
    KnownProfile(
        "fastmail-flatpak",
        "Fastmail (Flatpak)",
        ".var/app/com.fastmail.Fastmail/config/com.fastmail.Fastmail",
        auto=False,
    ),
    KnownProfile(
        "discord-flatpak",
        "Discord (Flatpak)",
        ".var/app/com.discordapp.Discord/config/discord",
        auto=False,
    ),
)
PROFILE_BY_ID: Final[dict[str, KnownProfile]] = {p.profile_id: p for p in KNOWN_PROFILES}


class PsdProfileState(StrEnum):
    """Runtime mount/recovery state for one configured browser profile."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    RECOVERY_PENDING = "recovery-pending"
    CONFLICT_MOUNT = "conflict-mount"
    MISSING_DIR = "missing"
    SYMLINK_BLOCKED = "symlink-blocked"


class PsdMountOutcome(StrEnum):
    """What `start`, `apply`, `stop`, or `sync` plans for one profile."""

    SKIP = "skip"
    MOUNT = "mount"
    UNMOUNT_MERGE = "unmount-merge"
    SNAPSHOT_RECOVERY = "snapshot-recovery"
    CYCLE_MERGE = "cycle-merge"
    RECOVER = "recover"


@dataclass(frozen=True, slots=True)
class PsdTargetUser:
    """Resolved non-root workstation account owning the managed profiles."""

    name: str
    group: str
    uid: int
    gid: int
    home: str
    home_aliases: tuple[str, ...] = ()

    @property
    def run_dir(self) -> str:
        """Runtime tmpfs mountpoint (`/run/cst-psd/<uid>`)."""
        return posixpath.join(PSD_RUN_ROOT, str(self.uid))

    @property
    def recovery_root(self) -> str:
        """Persistent unmerged snapshot folder in `$HOME`."""
        return posixpath.join(self.home, PSD_RECOVERY_REL)

    @property
    def home_variants(self) -> tuple[str, ...]:
        """All `$HOME` path variants (canonical path plus unresolved alias such as `/home/<user>`)."""
        return tuple(dict.fromkeys((self.home, *self.home_aliases)))


@dataclass(frozen=True, slots=True)
class PsdBusyProcess:
    """Running user process holding an open FD, cwd, or mapping under a profile."""

    pid: int
    comm: str
    path: str


@dataclass(frozen=True, slots=True)
class PsdParsedMounts:
    """Live `/proc/mounts` summary relevant to `cst desktop psd`."""

    tmpfs_mounted: bool
    tmpfs_size_opt: str
    overlay_targets: dict[str, tuple[str, str, str]]  # target -> (lowerdir, upperdir, workdir)
    other_mounts: dict[str, str]  # target -> fstype
    lower_binds: frozenset[str]


@dataclass(frozen=True, slots=True)
class PsdProfileProbe:
    """Observed disk, mount, busy, and recovery state for one profile."""

    spec: KnownProfile
    profile_path: str
    lower_dir: str
    upper_dir: str
    work_dir: str
    recovery_dir: str
    state: PsdProfileState
    overlay_active: bool
    lower_bind_active: bool
    recovery_exists: bool
    upper_used_bytes: int
    busy_procs: tuple[PsdBusyProcess, ...] = ()
    profile_dir_exists: bool = True


@dataclass(frozen=True, slots=True)
class PsdSnapshot:
    """Complete read-only snapshot for `cst desktop psd`."""

    user: PsdTargetUser | None
    user_error: str
    config_raw: str
    config_error: str
    requested_tmpfs_bytes: int
    effective_tmpfs_bytes: int
    mem_total_bytes: int | None
    tmpfs_mounted: bool
    tmpfs_used_bytes: int | None
    tmpfs_avail_bytes: int | None
    systemd_installed: bool
    service_enabled: bool
    service_active: bool
    timer_enabled: bool
    timer_active: bool
    profiles: tuple[PsdProfileProbe, ...]
    seam_root: str = ""
    orphan_recoveries: tuple[PsdProfileProbe, ...] = ()

    @classmethod
    def unavailable(cls, user_error: str, seam_root: str = "") -> PsdSnapshot:
        """Build a snapshot when the target non-root user could not be resolved."""
        return cls(
            user=None,
            user_error=user_error,
            config_raw="auto",
            config_error="",
            requested_tmpfs_bytes=DEFAULT_TMPFS_BYTES,
            effective_tmpfs_bytes=DEFAULT_TMPFS_BYTES,
            mem_total_bytes=None,
            tmpfs_mounted=False,
            tmpfs_used_bytes=None,
            tmpfs_avail_bytes=None,
            systemd_installed=False,
            service_enabled=False,
            service_active=False,
            timer_enabled=False,
            timer_active=False,
            profiles=(),
            seam_root=seam_root,
        )


class PsdWhiteoutManifest(TypedDict):
    """Whiteouts and opaque-dir markers stored in a crash-recovery snapshot."""

    format: int
    whiteouts: list[str]
    opaque_dirs: list[str]


def _valid_manifest_rel(rel: str, *, allow_empty: bool) -> bool:
    if not rel:
        return allow_empty
    if rel.startswith("/"):
        return False
    return all(part not in ("", ".", "..") for part in rel.split("/"))


def parse_whiteout_manifest(text: str) -> PsdWhiteoutManifest:
    """Validate `whiteouts.json` inside a recovery directory."""
    doc = parse_json(text)
    if not json_object(doc):
        raise ValueError("whiteouts manifest is not a JSON object")
    fmt = doc.get("format")
    if isinstance(fmt, bool) or not isinstance(fmt, int) or fmt != PSD_WHITEOUT_FORMAT:
        raise ValueError(f"unsupported whiteouts manifest format {fmt!r}")
    whiteouts = doc.get("whiteouts")
    opaque = doc.get("opaque_dirs")
    if not list_of(whiteouts, is_str_val) or not list_of(opaque, is_str_val):
        raise ValueError("invalid whiteouts/opaque_dirs lists in manifest")
    if not all(_valid_manifest_rel(w, allow_empty=False) for w in whiteouts):
        raise ValueError("unsafe path in whiteouts manifest")
    if not all(_valid_manifest_rel(o, allow_empty=True) for o in opaque):
        raise ValueError("unsafe path in opaque_dirs manifest")
    return PsdWhiteoutManifest(format=fmt, whiteouts=list(whiteouts), opaque_dirs=list(opaque))


def clamp_tmpfs_bytes(requested_bytes: int, mem_total_bytes: int | None) -> int:
    """Clamp requested tmpfs size to `[MIN_TMPFS_BYTES, min(MAX_TMPFS_BYTES, 25% host RAM)]`."""
    upper = MAX_TMPFS_BYTES
    if mem_total_bytes is not None and mem_total_bytes > 0:
        upper = min(upper, max(MIN_TMPFS_BYTES, (mem_total_bytes * MAX_RAM_FRACTION_PCT) // 100))
    return max(MIN_TMPFS_BYTES, min(requested_bytes, upper))


def parse_meminfo_total_bytes(meminfo_text: str | None) -> int | None:
    """Extract `MemTotal` in bytes from `/proc/meminfo` text."""
    if meminfo_text is None or not meminfo_text:
        return None
    for line in meminfo_text.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            kib = parse_whole_number(parts[1]) if len(parts) >= _MEMINFO_MIN_PARTS else None
            if kib is not None:
                return kib * 1024
    return None


def resolve_profile_selection(raw_spec: str, *, existing_rels: Collection[str]) -> tuple[KnownProfile, ...]:
    """Resolve `PSD_PROFILES` (`auto` or comma-separated IDs) into ordered `KnownProfile` tuples."""
    cleaned = raw_spec.strip()
    if not cleaned or cleaned == "auto":
        selected: list[KnownProfile] = []
        has_xdg_firefox = PROFILE_BY_ID["firefox"].rel_path in existing_rels
        for prof in KNOWN_PROFILES:
            if not prof.auto:
                continue
            if prof.profile_id == "firefox-legacy" and has_xdg_firefox:
                continue
            if prof.rel_path in existing_rels:
                selected.append(prof)
        return tuple(selected)

    seen: set[str] = set()
    out: list[KnownProfile] = []
    for raw_id in cleaned.split(","):
        pid = raw_id.strip()
        if not pid:
            continue
        prof = PROFILE_BY_ID.get(pid)
        if prof is None:
            valid = ", ".join(p.profile_id for p in KNOWN_PROFILES)
            raise DomainValidationError(f"unknown profile ID {pid!r} (valid: auto, {valid})")
        if pid not in seen:
            seen.add(pid)
            out.append(prof)
    return tuple(out)


def _extract_overlay_dirs(options_str: str) -> tuple[str, str, str]:
    lower = ""
    upper = ""
    work = ""
    for raw_tok in options_str.split(","):
        tok = strip_mount_blanks(raw_tok)
        if tok.startswith("lowerdir="):
            lower = tok.removeprefix("lowerdir=")
        elif tok.startswith("upperdir="):
            upper = tok.removeprefix("upperdir=")
        elif tok.startswith("workdir="):
            work = tok.removeprefix("workdir=")
    return lower, upper, work


def parse_psd_mounts(proc_mounts_text: str, *, run_base: str) -> PsdParsedMounts:
    """Parse `/proc/mounts` lines into tmpfs state, overlay mounts, and lower bind mounts."""
    tmpfs_mounted = False
    tmpfs_size_opt = ""
    overlay_targets: dict[str, tuple[str, str, str]] = {}
    other_mounts: dict[str, str] = {}
    lower_binds: set[str] = set()
    clean_base = run_base.rstrip("/")

    for line in file_lines(proc_mounts_text):
        match mount_fields(line):
            case [_src, raw_tgt, fstype, opts, *_]:
                tgt = unescape_mount_field(raw_tgt).rstrip("/") or "/"
                if tgt == clean_base and fstype == PSD_TMPFS_FSTYPE:
                    tmpfs_mounted = True
                    for tok in opts.split(","):
                        if tok.startswith("size="):
                            tmpfs_size_opt = tok.removeprefix("size=")
                elif tgt.startswith(f"{clean_base}/") and tgt.endswith("/lower"):
                    lower_binds.add(tgt)
                elif fstype == PSD_OVERLAY_FSTYPE:
                    low, up, wrk = _extract_overlay_dirs(opts)
                    if (
                        low.startswith(f"{clean_base}/")
                        and low.endswith("/lower")
                        and up.startswith(f"{clean_base}/")
                        and up.endswith("/upper")
                    ):
                        overlay_targets[tgt] = (low, up, wrk)
                    else:
                        other_mounts[tgt] = fstype
                else:
                    other_mounts[tgt] = fstype
    return PsdParsedMounts(
        tmpfs_mounted=tmpfs_mounted,
        tmpfs_size_opt=tmpfs_size_opt,
        overlay_targets=overlay_targets,
        other_mounts=other_mounts,
        lower_binds=frozenset(lower_binds),
    )


def format_busy_procs(procs: Sequence[PsdBusyProcess]) -> str:
    """Format busy process summary for human output and refusal messages."""
    shown = ", ".join(f"{p.comm}(pid={p.pid})" for p in procs[:_BUSY_PREVIEW_LIMIT])
    if len(procs) > _BUSY_PREVIEW_LIMIT:
        shown += f", +{len(procs) - _BUSY_PREVIEW_LIMIT} more"
    return shown


def _format_psd_profile_lines(profiles: Sequence[PsdProfileProbe]) -> list[str]:
    if not profiles:
        return ["  profiles             none found/selected"]
    out: list[str] = []
    for p in profiles:
        extra: list[str] = []
        if p.overlay_active:
            extra.append(f"upper={format_bytes_human(p.upper_used_bytes)}")
        if p.recovery_exists:
            extra.append("recovery-pending")
        if p.busy_procs:
            extra.append(f"busy: {format_busy_procs(p.busy_procs)}")
        suffix = f" ({'; '.join(extra)})" if extra else ""
        out.append(f"  profile {p.spec.profile_id:<12} {p.state.value} at {p.profile_path}{suffix}")
    return out


def format_psd_status(snap: PsdSnapshot) -> list[str]:
    """Render concise multi-line status for `cst desktop psd status`."""
    lines: list[str] = []
    if snap.seam_root:
        lines.append(f"test seam ({PSD_SEAM_ROOT_ENV}): {snap.seam_root}")
    lines.append("Browser profile overlayfs sync (psd):")
    if snap.user is None:
        lines.append(f"  target user          unavailable ({snap.user_error})")
        return lines
    lines.append(f"  target user          {snap.user.name} (uid={snap.user.uid}, home={snap.user.home})")
    lines.append(
        f"  config error         {snap.config_error}"
        if snap.config_error
        else f"  profiles setting     {snap.config_raw}"
    )
    cap_line = f"{format_bytes_human(snap.effective_tmpfs_bytes)} cap"
    if snap.tmpfs_mounted:
        used = format_bytes_human(snap.tmpfs_used_bytes)
        lines.append(f"  tmpfs runtime dir    {snap.user.run_dir} mounted ({used} used / {cap_line})")
    else:
        lines.append(f"  tmpfs runtime dir    {snap.user.run_dir} not mounted ({cap_line})")
    if snap.systemd_installed:
        lines.append(
            f"  systemd service      {PSD_SERVICE_TEMPLATE.format(user=snap.user.name)} "
            f"(enabled={snap.service_enabled}, active={snap.service_active})"
        )
        lines.append(
            f"  systemd timer        {PSD_RESYNC_TIMER_TEMPLATE.format(user=snap.user.name)} "
            f"(enabled={snap.timer_enabled}, active={snap.timer_active})"
        )
    else:
        lines.append("  systemd units        not installed (`cst desktop psd apply` installs them)")
    lines.extend(_format_psd_profile_lines(snap.profiles))
    return lines


def format_psd_plan(snap: PsdSnapshot, *, verb: str, prog: str) -> list[str]:
    """Render dry-run `plan` lines for `cst desktop psd plan`."""
    lines: list[str] = []
    if snap.seam_root:
        lines.append(f"test seam ({PSD_SEAM_ROOT_ENV}): {snap.seam_root}")
    if snap.user is None:
        return [*lines, f"BLOCKED: {snap.user_error}"]
    if snap.config_error:
        return [*lines, f"BLOCKED: {snap.config_error}"]
    lines.append(
        f"Plan for `cst desktop psd {verb}` (user={snap.user.name}, tmpfs cap={format_bytes_human(snap.effective_tmpfs_bytes)}):"
    )
    for p in snap.profiles:
        if p.state in (PsdProfileState.SYMLINK_BLOCKED, PsdProfileState.CONFLICT_MOUNT):
            lines.append(f"  {p.spec.profile_id:<16} [BLOCKED] {p.state.value} at {p.profile_path}")
        elif p.state == PsdProfileState.MISSING_DIR:
            lines.append(f"  {p.spec.profile_id:<16} [SKIP   ] missing directory at {p.profile_path}")
        elif p.overlay_active:
            lines.append(f"  {p.spec.profile_id:<16} [ok     ] overlay already active (left untouched)")
        elif p.busy_procs:
            lines.append(
                f"  {p.spec.profile_id:<16} [WAIT   ] busy ({format_busy_procs(p.busy_procs)}; closes before start mounts it)"
            )
        else:
            lines.append(f"  {p.spec.profile_id:<16} [MOUNT  ] overlay over {p.profile_path}")
    if not snap.systemd_installed:
        lines.append("  systemd units        [INSTALL] cst-desktop, psd service, resync service + hourly timer")
    elif not (snap.service_enabled and snap.timer_enabled):
        lines.append("  systemd units        [ENABLE ] cst-desktop-psd@.service and resync timer")
    else:
        lines.append("  systemd units        [ok     ] installed and enabled")
    lines.append(f"Dry run, nothing changed. Run `{prog} psd apply --user {snap.user.name}` to apply.")
    return lines

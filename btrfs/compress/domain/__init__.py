# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst btrfs compress-force`: parsing, option rewriting, planning, coherence."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Final

from cst_core.domain.text import (
    file_lines,
    is_mount_comment,
    mount_field_spans,
    mount_fields,
    strip_mount_blanks,
    unescape_mount_field,
)

DEFAULT_LEVEL: Final[int] = 1
BTRFS_ZSTD_DEFAULT_LEVEL: Final[int] = 3
MIN_ZSTD_LEVEL: Final[int] = 1
MAX_ZSTD_LEVEL: Final[int] = 15

FSTAB_PATH: Final[str] = "/etc/fstab"
PROC_MOUNTS_PATH: Final[str] = "/proc/mounts"
PROC_CMDLINE_PATH: Final[str] = "/proc/cmdline"
OSTREE_BOOTED_PATH: Final[str] = "/run/ostree-booted"
TOP_MOUNT_UNIT_PATH: Final[str] = r"/etc/systemd/system/var-mnt-btrfs\x2dtop.mount"
TOP_MOUNT_UNIT_NAME: Final[str] = r"var-mnt-btrfs\x2dtop.mount"
# What ROLLBACK INCOMPLETE says of a layer that cannot be read back after its step failed.
FSTAB_UNREAD: Final[str] = f"fstab: cannot read {FSTAB_PATH} back, its state is unknown"
TOP_MOUNT_UNREAD: Final[str] = f"top-mount: cannot read {TOP_MOUNT_UNIT_PATH} back, its state is unknown"

# rpm-ostree mounts the booted deployment's root filesystem there too.
_SYSROOT: Final = "/sysroot"
ROOT_TARGETS: Final[frozenset[str]] = frozenset({"/", _SYSROOT, "/var", "/var/home", "/home"})
# fstab: without a UUID=<root uuid> to compare, these targets are the root filesystem's; /home and
# /var/home are its only when they name the same source as one of them (a separate btrfs /home is another
# filesystem, never changed).
_PRIMARY_ROOT_TARGETS: Final[frozenset[str]] = frozenset({"/", _SYSROOT, "/var"})
_HOME_TARGETS: Final[frozenset[str]] = ROOT_TARGETS - _PRIMARY_ROOT_TARGETS
# The root filesystem is the btrfs of the first of these targets that is one (/sysroot, else /, else /var):
# pick_root_mounts picks the live root mounts by it, and the host adapter's root_uuid the fstab lines.
ROOT_ANCHOR_TARGETS: Final[tuple[str, ...]] = (_SYSROOT, "/", "/var")
_UUID_PREFIX: Final = "UUID="
_SUBVOL_SUFFIX_RE: Final = re.compile(r"\[[^\]]*\]$")
ROOTFLAGS_PREFIX: Final[str] = "rootflags="
_BTRFS_FSTYPE: Final[str] = "btrfs"
_ZSTD_ALGO: Final[str] = "zstd"


class CompressMode(StrEnum):
    """Btrfs compression mount option mode."""

    COMPRESS = "compress"
    COMPRESS_FORCE = "compress-force"


class LayerName(StrEnum):
    """Independent configuration/runtime layers holding the root btrfs compression option."""

    FSTAB = "fstab"
    KARGS = "kargs"
    LIVE = "live"
    TOP_MOUNT = "top-mount"


class LayerStatus(StrEnum):
    """Status of one layer relative to target compression option."""

    OK = "ok"
    CHANGE = "change"
    ABSENT = "absent"
    NOT_APPLICABLE = "not-applicable"


@dataclass(frozen=True, slots=True)
class CompressSpec:
    """Parsed compression mount option (`compress=zstd:1` or `compress-force=zstd:1`)."""

    mode: CompressMode
    algo: str
    level: int | None
    raw: str


@dataclass(frozen=True, slots=True)
class MountEntry:
    """One btrfs mount entry from `/proc/mounts` (or `/etc/fstab`)."""

    source: str
    target: str
    fstype: str
    options: str


@dataclass(frozen=True, slots=True)
class TopMountInfo:
    """State of `/etc/systemd/system/var-mnt-btrfs\\x2dtop.mount` when installed."""

    installed: bool
    uuid: str
    options: str


@dataclass(frozen=True, slots=True)
class CompressSnapshot:
    """All inputs describing current root btrfs compression across config/runtime layers."""

    is_ostree: bool
    root_uuid: str
    fstab_text: str | None
    kargs_text: str | None
    proc_mounts_text: str | None
    top_mount: TopMountInfo | None


@dataclass(frozen=True, slots=True)
class LayerPlan:
    """Planned state transition (or read-only finding) for a single layer."""

    layer: LayerName
    status: LayerStatus
    current: str
    target: str
    changed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class CompressPlan:
    """Complete plan for switching root btrfs compression across all active layers."""

    target_mode: CompressMode
    target_level: int
    target_option: str
    is_ostree: bool
    root_uuid: str
    layers: tuple[LayerPlan, ...]
    has_changes: bool
    new_fstab_text: str | None
    kargs_edits: tuple[tuple[str, str], ...]
    remount_target: str | None
    new_top_mount_options: str | None
    left_alone_notes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CoherenceReport:
    """Cross-layer agreement assessment used by both `status` and `cst doctor`."""

    coherent: bool
    mode: CompressMode | None
    level: int | None
    summary: str
    drift_details: tuple[str, ...]
    has_compression: bool


@dataclass(frozen=True, slots=True)
class CompressStatusView:
    """Read-only status payload produced by `CompressService.gather_status()`."""

    seam_proc: str
    live_entries: tuple[tuple[str, str], ...]
    layers: tuple[LayerPlan, ...]
    kargs_error: str
    booted_diff: tuple[str, str] | None
    coherence: CoherenceReport
    left_alone_notes: tuple[str, ...]


def format_compress_option(mode: CompressMode, level: int) -> str:
    """Render the canonical btrfs zstd mount option token."""
    return f"{mode}=zstd:{level}"


def matches_target_spec(spec: CompressSpec | None, target_mode: CompressMode, target_level: int) -> bool:
    """Return True if `spec` already enforces `(target_mode, target_level)` with zstd."""
    if spec is None or spec.mode != target_mode or spec.algo != _ZSTD_ALGO:
        return False
    if spec.level is not None:
        return spec.level == target_level
    return target_level == BTRFS_ZSTD_DEFAULT_LEVEL


def _is_compress_token(token: str) -> bool:
    return token in (CompressMode.COMPRESS, CompressMode.COMPRESS_FORCE) or token.startswith(
        (f"{CompressMode.COMPRESS}=", f"{CompressMode.COMPRESS_FORCE}=")
    )


def parse_compress_spec(token: str) -> CompressSpec | None:
    """Parse one mount option token if it is `compress*` or `compress-force*`."""
    if not _is_compress_token(token):
        return None
    if "=" not in token:
        mode = CompressMode.COMPRESS_FORCE if token == CompressMode.COMPRESS_FORCE else CompressMode.COMPRESS
        return CompressSpec(mode=mode, algo="zlib", level=None, raw=token)
    key, value = token.split("=", 1)
    mode = CompressMode.COMPRESS_FORCE if key == CompressMode.COMPRESS_FORCE else CompressMode.COMPRESS
    if ":" in value:
        algo, lvl_str = value.split(":", 1)
        try:
            lvl: int | None = int(lvl_str)
        except ValueError:
            lvl = None
        return CompressSpec(mode=mode, algo=algo, level=lvl, raw=token)
    return CompressSpec(mode=mode, algo=value, level=None, raw=token)


def extract_compress_specs(options_str: str) -> tuple[CompressSpec, ...]:
    """Return every compression option token from a comma-separated option string."""
    out: list[CompressSpec] = []
    for tok in options_str.split(","):
        spec = parse_compress_spec(strip_mount_blanks(tok))
        if spec is not None:
            out.append(spec)
    return tuple(out)


def effective_compress_spec(options_str: str) -> CompressSpec | None:
    """Return the effective compression option (last compression option wins in mount(8))."""
    specs = extract_compress_specs(options_str)
    return specs[-1] if specs else None


def detect_existing_level(specs: Sequence[CompressSpec], fallback: int = DEFAULT_LEVEL) -> int:
    """Pick the existing zstd level if within supported bounds, otherwise `fallback`."""
    for spec in specs:
        if spec.algo == _ZSTD_ALGO and spec.level is not None and MIN_ZSTD_LEVEL <= spec.level <= MAX_ZSTD_LEVEL:
            return spec.level
    return fallback


def rewrite_mount_options(options_str: str, target_option: str) -> str:
    """Replace compression options in a comma-separated list with `target_option`."""
    tokens = [tok for tok in options_str.split(",") if tok]
    out: list[str] = []
    inserted = False
    for tok in tokens:
        if _is_compress_token(tok):
            if not inserted:
                out.append(target_option)
                inserted = True
        else:
            out.append(tok)
    if not inserted:
        out.append(target_option)
    return ",".join(out)


def _fstab_uuid(source: str) -> str | None:
    """The UUID of a `UUID=...` fstab source (lower case), or None for another kind of source."""
    return source[len(_UUID_PREFIX) :].lower() if source[: len(_UUID_PREFIX)].upper() == _UUID_PREFIX else None


def _is_primary_root_entry(source: str, target: str, root_uuid: str) -> bool:
    """An fstab line of the root filesystem on its own: UUID=<root_uuid> on any target (another UUID= is
    another filesystem, whatever its target); without a UUID to compare, a target of /, /sysroot or /var."""
    uuid = _fstab_uuid(source)
    if uuid is not None and root_uuid:
        return uuid == root_uuid.lower()
    return target in _PRIMARY_ROOT_TARGETS


def _root_fstab_sources(entries: Sequence[MountEntry], root_uuid: str) -> frozenset[str]:
    return frozenset(e.source for e in entries if _is_primary_root_entry(e.source, e.target, root_uuid))


def _is_root_btrfs_entry(source: str, target: str, root_uuid: str, root_sources: frozenset[str]) -> bool:
    """/home and /var/home are the root filesystem's only when their source names it (Fedora's default:
    a subvolume of it); a separate btrfs /home is never one."""
    if _is_primary_root_entry(source, target, root_uuid):
        return True
    return target in _HOME_TARGETS and source in root_sources


def parse_proc_mounts(text: str) -> tuple[MountEntry, ...]:
    """Parse btrfs entries from `/proc/mounts` (or `/etc/fstab`), decoding standard octal escapes; only a
    newline ends a line (file_lines), and the fields split at spaces and tabs only, as libmount splits them
    (mount_fields)."""
    entries: list[MountEntry] = []
    for line in file_lines(text):
        if is_mount_comment(line):
            continue
        match mount_fields(line):
            case [raw_src, raw_tgt, fstype, opts, *_] if fstype == _BTRFS_FSTYPE:
                entries.append(
                    MountEntry(
                        source=unescape_mount_field(raw_src),
                        target=unescape_mount_field(raw_tgt),
                        fstype=_BTRFS_FSTYPE,
                        options=opts,
                    )
                )
    return tuple(entries)


def separate_var_entries(fstab_text: str, root_uuid: str) -> tuple[MountEntry, ...]:
    """The btrfs `/etc/fstab` lines of a separate filesystem at /var: those whose `UUID=` is that of the /var
    line, when it is not the root's (empty without a root UUID, or a /var line of another kind). A CST
    before `ROOT_ANCHOR_TARGETS` asked findmnt for /var first and took that filesystem for the root, so its
    `apply` changed these lines; none is the root's, so nothing changes them back."""
    if not root_uuid:
        return ()
    entries = parse_proc_mounts(fstab_text)
    var_uuid = next((u for e in entries if e.target == "/var" and (u := _fstab_uuid(e.source)) is not None), None)
    if var_uuid is None or var_uuid == root_uuid.lower():
        return ()
    return tuple(e for e in entries if _fstab_uuid(e.source) == var_uuid)


def left_alone_note(entry: MountEntry, current: str) -> str:
    """What `status` and `plan` say of a separate /var filesystem's fstab line that carries the option an
    older CST's `apply` may have set there (`current`, as the feature describes it)."""
    return (
        f"/etc/fstab {entry.target} ({entry.source}) is a separate btrfs filesystem, not the root's, and has "
        f"{current}: CST leaves it alone. An older CST took a separate btrfs /var for the root and may have "
        "set that; if you did not, put that line's options back by hand."
    )


def parse_fstab_root_entries(fstab_text: str, root_uuid: str = "") -> tuple[MountEntry, ...]:
    """Return btrfs entries in `/etc/fstab` belonging to the root filesystem."""
    entries = parse_proc_mounts(fstab_text)
    sources = _root_fstab_sources(entries, root_uuid)
    return tuple(e for e in entries if _is_root_btrfs_entry(e.source, e.target, root_uuid, sources))


def rewrite_fstab_options(fstab_text: str, option_rewriter: Callable[[str], str], root_uuid: str = "") -> str:
    """Rewrite options on root btrfs `/etc/fstab` lines using `option_rewriter`, keeping formatting. Only a
    newline ends a line, as libmount reads one: a form feed or a vertical tab is part of a field."""
    sources = _root_fstab_sources(parse_proc_mounts(fstab_text), root_uuid)
    out_lines: list[str] = []
    for raw_line in fstab_text.split("\n"):
        if is_mount_comment(raw_line):
            out_lines.append(raw_line)
            continue
        fields = [(raw_line[start:end], start, end) for start, end in mount_field_spans(raw_line)]
        match fields:
            case [(src, _, _), (tgt, _, _), (fstype, _, _), (opts, start, end), *_] if (
                fstype == _BTRFS_FSTYPE
                and _is_root_btrfs_entry(unescape_mount_field(src), unescape_mount_field(tgt), root_uuid, sources)
            ):
                out_lines.append(raw_line[:start] + option_rewriter(opts) + raw_line[end:])
            case _:
                out_lines.append(raw_line)
    return "\n".join(out_lines)


def rewrite_fstab(fstab_text: str, target_option: str, root_uuid: str = "") -> str:
    """Rewrite options on root btrfs `/etc/fstab` lines, preserving spacing, comments, and line ends."""
    return rewrite_fstab_options(fstab_text, lambda opts: rewrite_mount_options(opts, target_option), root_uuid)


def extract_kargs_line(kargs_stdout: str) -> str:
    """Extract the kernel argument line from `rpm-ostree kargs` stdout or `/proc/cmdline`."""
    for line in kargs_stdout.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def extract_rootflags_tokens(cmdline_or_kargs: str) -> tuple[str, ...]:
    """Return every `rootflags=...` token from kernel args."""
    line = extract_kargs_line(cmdline_or_kargs)
    return tuple(tok for tok in line.split() if tok.startswith(ROOTFLAGS_PREFIX))


def rewrite_kargs_options(kargs_stdout: str, option_rewriter: Callable[[str], str]) -> tuple[tuple[str, str], ...]:
    """Return `(old_token, new_token)` pairs for any `rootflags=` token changed by `option_rewriter`."""
    edits: list[tuple[str, str]] = []
    for tok in extract_rootflags_tokens(kargs_stdout):
        val = tok.removeprefix(ROOTFLAGS_PREFIX)
        new_val = option_rewriter(val)
        if new_val != val:
            edits.append((tok, f"{ROOTFLAGS_PREFIX}{new_val}"))
    return tuple(edits)


def kargs_edits_staged(read: tuple[bool, str, str], edits: Sequence[tuple[str, str]]) -> bool:
    """Whether `rpm-ostree kargs`'s answer (read_kargs's `(ok, stdout, err)`) already holds each new
    `rootflags=` token of edits and none of the old ones: a change that answered a failure but was staged
    (rpm-ostree runs a transaction it started to its end). False when the read fails."""
    ok, text, _err = read
    if not ok or not edits:
        return False
    toks = set(extract_rootflags_tokens(text))
    return all(new in toks and old not in toks for old, new in edits)


def top_mount_changed(top: TopMountInfo | None, new_options: str | None) -> bool:
    """Whether the installed top-mount unit holds new_options: an update that failed and could not put
    the previous unit back."""
    return top is not None and new_options is not None and top.options == new_options


def rewrite_kargs_rootflags(kargs_stdout: str, target_option: str) -> tuple[tuple[str, str], ...]:
    """Return `(old_token, new_token)` pairs for any `rootflags=` token needing an update."""
    return rewrite_kargs_options(kargs_stdout, lambda val: rewrite_mount_options(val, target_option))


def _extract_mount_lines(unit_text: str) -> list[tuple[str, str]]:
    section = ""
    pairs: list[tuple[str, str]] = []
    for line in unit_text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", ";")):
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s
            continue
        if section == "[Mount]" and "=" in s:
            k, v = s.split("=", 1)
            pairs.append((k.strip(), v.strip()))
    return pairs


def parse_top_mount_unit(unit_text: str) -> TopMountInfo:
    """Parse `What=` (UUID) and `Options=` from `var-mnt-btrfs\\x2dtop.mount`."""
    uuid = ""
    options = ""
    for key, val in _extract_mount_lines(unit_text):
        if key == "What" and val.startswith("/dev/disk/by-uuid/"):
            uuid = val.removeprefix("/dev/disk/by-uuid/")
        elif key == "Options":
            options = val
    return TopMountInfo(installed=True, uuid=uuid, options=options)


def top_mount_options_from_var(var_options: str) -> str:
    """Mirror `setup-snapshots.bash` option filter for `var-mnt-btrfs\\x2dtop.mount`."""
    kept = [
        tok
        for tok in var_options.split(",")
        if tok and tok not in ("rw", "ro", "seclabel") and not tok.startswith(("subvol=", "subvolid="))
    ]
    return ",".join(["subvolid=5", *kept])


def _mount_device(source: str) -> str:
    """A live mount's device: findmnt's SOURCE without its "[/subvolume]" part (btrfs shows one device
    name for every mount of a filesystem)."""
    return _SUBVOL_SUFFIX_RE.sub("", source)


def pick_root_mounts(mounts: Sequence[MountEntry]) -> tuple[MountEntry, ...]:
    """Filter `/proc/mounts` btrfs entries to root workstation mounts (`/sysroot`, `/var`, `/var/home`, `/`)
    of the root filesystem: the device of /sysroot, else /, else /var. A separate btrfs /home is left out;
    with none of those three on btrfs, nothing is a root mount."""
    by_target = {m.target: m for m in mounts}
    anchor = next((by_target[t] for t in ROOT_ANCHOR_TARGETS if t in by_target), None)
    if anchor is None:
        return ()
    device = _mount_device(anchor.source)
    return tuple(m for m in mounts if m.target in ROOT_TARGETS and _mount_device(m.source) == device)


def pick_remount_target(mounts: Sequence[MountEntry]) -> MountEntry | None:
    """Choose the best live writable root btrfs mountpoint for `mount -o remount,...`."""
    by_target = {m.target: m for m in mounts}
    for allow_ro in (False, True):
        for preferred in ("/var", "/var/home", _SYSROOT, "/"):
            entry = by_target.get(preferred)
            if entry is not None and (allow_ro or "ro" not in entry.options.split(",")):
                return entry
    return None


def _describe_specs(specs: Sequence[CompressSpec]) -> str:
    unique = list(dict.fromkeys(s.raw for s in specs))
    return ", ".join(unique) if unique else "(none)"


def _layer(layer: LayerName, status: LayerStatus, current: str, target_opt: str, detail: str) -> LayerPlan:
    return LayerPlan(layer, status, current, target_opt, changed=status == LayerStatus.CHANGE, detail=detail)


@dataclass(frozen=True, slots=True)
class OptionLayerOps:
    """Hooks parameterizing multi-layer planning for a root btrfs mount option."""

    target_label: str
    rewrite_opts: Callable[[str], str]
    describe_options: Callable[[Sequence[str]], str]
    describe_top: Callable[[str], str]
    needs_live_change: Callable[[Sequence[MountEntry]], bool]
    needs_top_change: Callable[[TopMountInfo], bool]
    # True for options that carry what this feature's `apply` writes (a note for a separate /var's lines).
    set_by_apply: Callable[[str], bool]
    fstab_absent_current: str = "(no root btrfs entries)"
    fstab_absent_detail: str = "no root btrfs entries in /etc/fstab"
    kargs_absent_current: str = "(no rootflags= in kargs)"
    kargs_absent_detail: str = "no rootflags= found in rpm-ostree kargs"
    kargs_skip_detail: str = "non-ostree system (skipped)"
    live_absent_detail: str = "no live root btrfs mount in /proc/mounts"


@dataclass(frozen=True, slots=True)
class RootOptionLayerPlan:
    """Generic multi-layer plan result before feature-specific wrapping."""

    layers: tuple[LayerPlan, ...]
    new_fstab_text: str | None
    kargs_edits: tuple[tuple[str, str], ...]
    remount_target: str | None
    new_top_mount_options: str | None
    # A note per line of a separate /var filesystem that carries this feature's option (separate_var_entries).
    left_alone_notes: tuple[str, ...]


def _plan_fstab(
    fstab_text: str | None, fstab_roots: tuple[MountEntry, ...], root_uuid: str, ops: OptionLayerOps
) -> tuple[LayerPlan, str | None]:
    if fstab_text is None:
        return _layer(
            LayerName.FSTAB, LayerStatus.ABSENT, "(missing)", ops.target_label, "/etc/fstab not present"
        ), None
    if not fstab_roots:
        return (
            _layer(
                LayerName.FSTAB,
                LayerStatus.ABSENT,
                ops.fstab_absent_current,
                ops.target_label,
                ops.fstab_absent_detail,
            ),
            None,
        )
    rewritten = rewrite_fstab_options(fstab_text, ops.rewrite_opts, root_uuid)
    changed = rewritten != fstab_text
    status = LayerStatus.CHANGE if changed else LayerStatus.OK
    lp = _layer(
        LayerName.FSTAB,
        status,
        ops.describe_options([m.options for m in fstab_roots]),
        ops.target_label,
        f"{len(fstab_roots)} root btrfs entry(ies) in /etc/fstab",
    )
    return lp, (rewritten if changed else None)


def _plan_kargs(
    *, is_ostree: bool, kargs_text: str | None, rootflags_toks: tuple[str, ...], ops: OptionLayerOps
) -> tuple[LayerPlan, tuple[tuple[str, str], ...]]:
    if not is_ostree:
        return (
            _layer(
                LayerName.KARGS, LayerStatus.NOT_APPLICABLE, "(not ostree)", ops.target_label, ops.kargs_skip_detail
            ),
            (),
        )
    if kargs_text is None or not rootflags_toks:
        return (
            _layer(
                LayerName.KARGS,
                LayerStatus.ABSENT,
                ops.kargs_absent_current,
                ops.target_label,
                ops.kargs_absent_detail,
            ),
            (),
        )
    edits = rewrite_kargs_options(kargs_text, ops.rewrite_opts)
    status = LayerStatus.CHANGE if edits else LayerStatus.OK
    current = ops.describe_options([tok.removeprefix(ROOTFLAGS_PREFIX) for tok in rootflags_toks])
    return _layer(LayerName.KARGS, status, current, ops.target_label, "rpm-ostree kargs rootflags="), edits


def _plan_live(live_roots: tuple[MountEntry, ...], ops: OptionLayerOps) -> tuple[LayerPlan, str | None]:
    remount_entry = pick_remount_target(live_roots)
    if remount_entry is None:
        return (
            _layer(LayerName.LIVE, LayerStatus.ABSENT, "(not mounted)", ops.target_label, ops.live_absent_detail),
            None,
        )
    needs_remount = ops.needs_live_change(live_roots)
    status = LayerStatus.CHANGE if needs_remount else LayerStatus.OK
    lp = _layer(
        LayerName.LIVE,
        status,
        ops.describe_options([m.options for m in live_roots]),
        ops.target_label,
        f"live remount via {remount_entry.target}",
    )
    return lp, (remount_entry.target if needs_remount else None)


def _plan_top_mount(top_mount: TopMountInfo | None, ops: OptionLayerOps) -> tuple[LayerPlan, str | None]:
    """The top-mount unit's own Options= with only this feature's option rewritten (what only the unit
    has stays; what the live /var has at the moment is not copied in)."""
    if top_mount is None or not top_mount.installed:
        return (
            _layer(
                LayerName.TOP_MOUNT,
                LayerStatus.NOT_APPLICABLE,
                "(not installed)",
                ops.target_label,
                r"var-mnt-btrfs\x2dtop.mount not installed",
            ),
            None,
        )
    cur = ops.describe_top(top_mount.options)
    desired_top = ops.rewrite_opts(top_mount.options)
    changed = ops.needs_top_change(top_mount) and desired_top != top_mount.options
    status = LayerStatus.CHANGE if changed else LayerStatus.OK
    lp = _layer(LayerName.TOP_MOUNT, status, cur, ops.target_label, r"systemd unit var-mnt-btrfs\x2dtop.mount")
    return lp, (desired_top if changed else None)


def plan_root_option_layers(snapshot: CompressSnapshot, ops: OptionLayerOps) -> RootOptionLayerPlan:
    """Compute a 4-layer root btrfs option plan using feature-specific `ops` hooks."""
    live_roots = pick_root_mounts(parse_proc_mounts(snapshot.proc_mounts_text or ""))
    fstab_roots = parse_fstab_root_entries(snapshot.fstab_text or "", snapshot.root_uuid)
    rootflags_toks = extract_rootflags_tokens(snapshot.kargs_text or "") if snapshot.is_ostree else ()
    lp_fstab, new_fstab = _plan_fstab(snapshot.fstab_text, fstab_roots, snapshot.root_uuid, ops)
    lp_kargs, kargs_edits = _plan_kargs(
        is_ostree=snapshot.is_ostree, kargs_text=snapshot.kargs_text, rootflags_toks=rootflags_toks, ops=ops
    )
    lp_live, remount_target = _plan_live(live_roots, ops)
    lp_top, new_top_opts = _plan_top_mount(snapshot.top_mount, ops)
    return RootOptionLayerPlan(
        layers=(lp_fstab, lp_kargs, lp_live, lp_top),
        new_fstab_text=new_fstab,
        kargs_edits=kargs_edits,
        remount_target=remount_target,
        new_top_mount_options=new_top_opts,
        left_alone_notes=tuple(
            left_alone_note(e, ops.describe_options([e.options]))
            for e in separate_var_entries(snapshot.fstab_text or "", snapshot.root_uuid)
            if ops.set_by_apply(e.options)
        ),
    )


def build_plan(
    snapshot: CompressSnapshot,
    *,
    target_mode: CompressMode,
    level_override: int | None,
) -> CompressPlan:
    """Build a complete plan across `fstab`, `kargs`, `live`, and `top-mount`."""
    live_roots = pick_root_mounts(parse_proc_mounts(snapshot.proc_mounts_text or ""))
    fstab_roots = parse_fstab_root_entries(snapshot.fstab_text or "", snapshot.root_uuid)
    rootflags_toks = extract_rootflags_tokens(snapshot.kargs_text or "") if snapshot.is_ostree else ()

    probe_sources = [m.options for m in (*live_roots, *fstab_roots)] + [
        t.removeprefix(ROOTFLAGS_PREFIX) for t in rootflags_toks
    ]
    probe_specs = [sp for opt in probe_sources if (sp := effective_compress_spec(opt)) is not None]
    target_level = level_override if level_override is not None else detect_existing_level(probe_specs)
    target_opt = format_compress_option(target_mode, target_level)

    def _describe_opts(opt_strings: Sequence[str]) -> str:
        return _describe_specs([sp for o in opt_strings if (sp := effective_compress_spec(o)) is not None])

    def _describe_top(opts: str) -> str:
        sp = effective_compress_spec(opts)
        return sp.raw if sp is not None else "(none)"

    core = plan_root_option_layers(
        snapshot,
        OptionLayerOps(
            target_label=target_opt,
            rewrite_opts=lambda opts: rewrite_mount_options(opts, target_opt),
            describe_options=_describe_opts,
            describe_top=_describe_top,
            needs_live_change=lambda ms: any(
                not matches_target_spec(effective_compress_spec(m.options), target_mode, target_level) for m in ms
            ),
            needs_top_change=lambda top: (
                not matches_target_spec(effective_compress_spec(top.options), target_mode, target_level)
            ),
            set_by_apply=_has_compress_force,
        ),
    )
    return CompressPlan(
        target_mode=target_mode,
        target_level=target_level,
        target_option=target_opt,
        is_ostree=snapshot.is_ostree,
        root_uuid=snapshot.root_uuid,
        layers=core.layers,
        has_changes=any(lp.changed for lp in core.layers),
        new_fstab_text=core.new_fstab_text,
        kargs_edits=core.kargs_edits,
        remount_target=core.remount_target,
        new_top_mount_options=core.new_top_mount_options,
        left_alone_notes=core.left_alone_notes,
    )


def _has_compress_force(options: str) -> bool:
    spec = effective_compress_spec(options)
    return spec is not None and spec.mode == CompressMode.COMPRESS_FORCE


def _canonical_spec(spec: CompressSpec | None) -> str:
    if spec is None:
        return "none"
    if spec.algo == _ZSTD_ALGO and spec.level is None:
        return f"{spec.mode}={_ZSTD_ALGO}:{BTRFS_ZSTD_DEFAULT_LEVEL}"
    return spec.raw


def collect_layer_options(snapshot: CompressSnapshot) -> list[tuple[str, str]]:
    """Collect raw mount option strings across fstab, kargs rootflags=, live mounts, and top-mount."""
    observed = [
        (f"fstab {entry.target}", entry.options)
        for entry in parse_fstab_root_entries(snapshot.fstab_text or "", snapshot.root_uuid)
    ]
    if snapshot.is_ostree and snapshot.kargs_text is not None:
        observed.extend(
            ("kargs rootflags", tok.removeprefix(ROOTFLAGS_PREFIX))
            for tok in extract_rootflags_tokens(snapshot.kargs_text)
        )
    live_roots = pick_root_mounts(parse_proc_mounts(snapshot.proc_mounts_text or ""))
    observed.extend((f"live {entry.target}", entry.options) for entry in live_roots)
    if snapshot.top_mount is not None and snapshot.top_mount.installed:
        observed.append(("top-mount", snapshot.top_mount.options))
    return observed


def assess_coherence(snapshot: CompressSnapshot) -> CoherenceReport:
    """Assess whether active layers agree on root btrfs compression."""
    observed: list[tuple[str, CompressSpec | None]] = [
        (name, effective_compress_spec(opts)) for name, opts in collect_layer_options(snapshot)
    ]

    if not observed:
        return CoherenceReport(
            coherent=True,
            mode=None,
            level=None,
            summary="no root btrfs mount sources found",
            drift_details=(),
            has_compression=False,
        )

    canonical_values = {_canonical_spec(spec) for _, spec in observed}
    if len(canonical_values) > 1:
        details = tuple(f"{name}={spec.raw if spec is not None else 'none'}" for name, spec in observed)
        return CoherenceReport(
            coherent=False,
            mode=None,
            level=None,
            summary="drift across layers: " + ", ".join(details),
            drift_details=details,
            has_compression=any(spec is not None for _, spec in observed),
        )

    first_spec = observed[0][1]
    if first_spec is None:
        return CoherenceReport(
            coherent=True,
            mode=None,
            level=None,
            summary="no compression option set across active layers",
            drift_details=(),
            has_compression=False,
        )
    return CoherenceReport(
        coherent=True,
        mode=first_spec.mode,
        level=first_spec.level,
        summary=f"coherent ({first_spec.raw})",
        drift_details=(),
        has_compression=True,
    )

# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst btrfs compress-force`: parsing, option rewriting, planning, coherence."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Final

from cst_core.domain.text import HELP_WORDS, enum_member

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

ROOT_TARGETS: Final[frozenset[str]] = frozenset({"/", "/sysroot", "/var", "/var/home", "/home"})
# fstab: without a UUID=<root uuid> to compare, these targets are the root filesystem's; /home and
# /var/home are its only when they name the same source as one of them (a separate btrfs /home is another
# filesystem, never changed).
_PRIMARY_ROOT_TARGETS: Final[frozenset[str]] = frozenset({"/", "/sysroot", "/var"})
_HOME_TARGETS: Final[frozenset[str]] = ROOT_TARGETS - _PRIMARY_ROOT_TARGETS
_UUID_PREFIX: Final = "UUID="
_SUBVOL_SUFFIX_RE: Final = re.compile(r"\[[^\]]*\]$")
ROOTFLAGS_PREFIX: Final[str] = "rootflags="
_BTRFS_FSTYPE: Final[str] = "btrfs"
_ZSTD_ALGO: Final[str] = "zstd"
_NON_WS_RE = re.compile(r"\S+")


class CompressCommand(StrEnum):
    """Supported subcommands of `btrfs-compress`."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"
    HELP = "help"


class CompressFlag(StrEnum):
    """Supported long flags of `btrfs-compress`."""

    REVERT = "--revert"
    YES = "--yes"
    LEVEL = "--level"


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


@dataclass(frozen=True, slots=True)
class CompressArgs:
    """Validated CLI arguments."""

    command: CompressCommand
    level: int | None = None
    revert: bool = False
    yes: bool = False


@dataclass(frozen=True, slots=True)
class ArgsError:
    """Invalid CLI invocation."""

    message: str
    code: int = 2


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


def _parse_level_value(raw: str) -> int | ArgsError:
    if not raw.isdigit():
        return ArgsError(f"invalid --level value {raw!r}: expected an integer {MIN_ZSTD_LEVEL}..{MAX_ZSTD_LEVEL}")
    val = int(raw)
    if val < MIN_ZSTD_LEVEL or val > MAX_ZSTD_LEVEL:
        return ArgsError(f"invalid --level {val}: must be between {MIN_ZSTD_LEVEL} and {MAX_ZSTD_LEVEL}")
    return val


def _parse_flag_token(tok: str) -> tuple[CompressFlag | None, str | None]:
    if tok.startswith(f"{CompressFlag.LEVEL}="):
        flag_str, inline = tok.split("=", 1)
        return enum_member(CompressFlag, flag_str), inline
    return enum_member(CompressFlag, tok), None


def _consume_level(cmd: CompressCommand, inline: str | None, next_arg: str | None) -> tuple[int | ArgsError, int]:
    if cmd == CompressCommand.STATUS:
        return ArgsError("--level is not supported with `status`"), 0
    if inline is not None:
        return _parse_level_value(inline), 0
    if next_arg is None:
        return ArgsError("--level requires a value"), 0
    return _parse_level_value(next_arg), 1


def _validate_flag(
    cmd: CompressCommand, tok: str, next_arg: str | None
) -> tuple[CompressFlag, int | bool, int] | ArgsError:
    flag, inline = _parse_flag_token(tok)
    res: tuple[CompressFlag, int | bool, int] | ArgsError
    match flag:
        case CompressFlag.REVERT:
            res = (
                (CompressFlag.REVERT, True, 0)
                if cmd == CompressCommand.PLAN
                else ArgsError("--revert is only supported with `plan`")
            )
        case CompressFlag.YES:
            res = (
                (CompressFlag.YES, True, 0)
                if cmd in (CompressCommand.APPLY, CompressCommand.REVERT)
                else ArgsError(f"--yes is not supported with `{cmd}`")
            )
        case CompressFlag.LEVEL:
            parsed, consumed = _consume_level(cmd, inline, next_arg)
            res = parsed if isinstance(parsed, ArgsError) else (CompressFlag.LEVEL, parsed, consumed)
        case None:
            res = ArgsError(f"unknown or unexpected argument: {tok}")
    return res


def _parse_compress_flags(cmd: CompressCommand, rest: Sequence[str]) -> CompressArgs | ArgsError:
    level: int | None = None
    revert = False
    yes = False
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in HELP_WORDS:
            return CompressArgs(command=CompressCommand.HELP)
        next_arg = rest[i + 1] if i + 1 < len(rest) else None
        step = _validate_flag(cmd, tok, next_arg)
        if isinstance(step, ArgsError):
            return step
        flag, val, extra = step
        match flag:
            case CompressFlag.REVERT:
                revert = bool(val)
            case CompressFlag.YES:
                yes = bool(val)
            case CompressFlag.LEVEL:
                level = int(val)
        i += 1 + extra
    return CompressArgs(command=cmd, level=level, revert=revert, yes=yes)


def parse_compress_args(argv: Sequence[str]) -> CompressArgs | ArgsError:
    """Parse argv (without program name) into `CompressArgs` or `ArgsError`."""
    if not argv:
        return CompressArgs(command=CompressCommand.STATUS)
    head = argv[0]
    if head in HELP_WORDS or head == CompressCommand.HELP:
        return (
            ArgsError(f"unexpected argument after {head}: {argv[1]}")
            if len(argv) > 1
            else CompressArgs(command=CompressCommand.HELP)
        )
    cmd = enum_member(CompressCommand, head)
    if cmd is None:
        return ArgsError(f"unknown command: {head}")
    return _parse_compress_flags(cmd, argv[1:])


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
        spec = parse_compress_spec(tok.strip())
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


def _unescape_fstab(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), field)


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
    """Parse btrfs entries from `/proc/mounts`, decoding standard octal escapes."""
    entries: list[MountEntry] = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match line.split():
            case [raw_src, raw_tgt, fstype, opts, *_] if fstype == _BTRFS_FSTYPE:
                entries.append(
                    MountEntry(
                        source=_unescape_fstab(raw_src),
                        target=_unescape_fstab(raw_tgt),
                        fstype=_BTRFS_FSTYPE,
                        options=opts,
                    )
                )
    return tuple(entries)


def parse_fstab_root_entries(fstab_text: str, root_uuid: str = "") -> tuple[MountEntry, ...]:
    """Return btrfs entries in `/etc/fstab` belonging to the root filesystem."""
    entries = parse_proc_mounts(fstab_text)
    sources = _root_fstab_sources(entries, root_uuid)
    return tuple(e for e in entries if _is_root_btrfs_entry(e.source, e.target, root_uuid, sources))


def rewrite_fstab_options(fstab_text: str, option_rewriter: Callable[[str], str], root_uuid: str = "") -> str:
    """Rewrite options on root btrfs `/etc/fstab` lines using `option_rewriter`, keeping formatting."""
    sources = _root_fstab_sources(parse_proc_mounts(fstab_text), root_uuid)
    out_lines: list[str] = []
    for raw_line in fstab_text.splitlines(keepends=True):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(raw_line)
            continue
        match list(_NON_WS_RE.finditer(raw_line)):
            case [s_src, s_tgt, s_type, s_opt, *_] if s_type.group() == _BTRFS_FSTYPE and _is_root_btrfs_entry(
                _unescape_fstab(s_src.group()), _unescape_fstab(s_tgt.group()), root_uuid, sources
            ):
                new_opts = option_rewriter(s_opt.group())
                out_lines.append(raw_line[: s_opt.start()] + new_opts + raw_line[s_opt.end() :])
            case _:
                out_lines.append(raw_line)
    return "".join(out_lines)


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
    anchor = next((by_target[t] for t in ("/sysroot", "/", "/var") if t in by_target), None)
    if anchor is None:
        return ()
    device = _mount_device(anchor.source)
    return tuple(m for m in mounts if m.target in ROOT_TARGETS and _mount_device(m.source) == device)


def pick_remount_target(mounts: Sequence[MountEntry]) -> MountEntry | None:
    """Choose the best live writable root btrfs mountpoint for `mount -o remount,...`."""
    by_target = {m.target: m for m in mounts}
    for allow_ro in (False, True):
        for preferred in ("/var", "/var/home", "/sysroot", "/"):
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


def _plan_live(
    live_roots: tuple[MountEntry, ...], ops: OptionLayerOps
) -> tuple[LayerPlan, str | None, MountEntry | None]:
    remount_entry = pick_remount_target(live_roots)
    if remount_entry is None:
        return (
            _layer(LayerName.LIVE, LayerStatus.ABSENT, "(not mounted)", ops.target_label, ops.live_absent_detail),
            None,
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
    return lp, (remount_entry.target if needs_remount else None), remount_entry


def _plan_top_mount(
    top_mount: TopMountInfo | None,
    remount_entry: MountEntry | None,
    ops: OptionLayerOps,
) -> tuple[LayerPlan, str | None]:
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
    base_opts = remount_entry.options if remount_entry is not None else top_mount.options
    desired_top = top_mount_options_from_var(ops.rewrite_opts(base_opts))
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
    lp_live, remount_target, remount_entry = _plan_live(live_roots, ops)
    lp_top, new_top_opts = _plan_top_mount(snapshot.top_mount, remount_entry, ops)
    return RootOptionLayerPlan(
        layers=(lp_fstab, lp_kargs, lp_live, lp_top),
        new_fstab_text=new_fstab,
        kargs_edits=kargs_edits,
        remount_target=remount_target,
        new_top_mount_options=new_top_opts,
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
    )


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

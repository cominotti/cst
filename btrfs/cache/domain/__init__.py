# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst btrfs cache`: CLI parsing, fstab planning, safety checks (cst doctor's check:
`health.py`)."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
import posixpath
import re
from typing import Final, TypedDict, TypeIs

from btrfs.compress.domain import MountEntry
from cst_core.domain.shapes import KeyTypes, has_shape, json_object, list_of, parse_json
from cst_core.domain.text import HELP_WORDS, enum_member, parse_subcommand_head

CACHE_BASE: Final = "/var/mnt/cache"
SCRATCHPAD_BASE: Final = "/var/mnt/scratchpad"
CACHE_REL: Final = ".cache"
SCRATCHPAD_REL: Final = "Scratchpad"
CACHE_MOUNT_MARKER: Final = "# managed-by: btrfs-cache (CST)"
CACHE_MOUNT_BIND_OPTS: Final = "bind,nofail"

MIN_HUMAN_UID: Final = 1000
DIR_MODE_PRIVATE: Final = 0o700
FILE_MODE_TAG: Final = 0o644
EXIT_USAGE: Final = 2

FSTYPE_BTRFS: Final = "btrfs"
OPT_FLUSHONCOMMIT: Final = "flushoncommit"
_FSTAB_MIN_FIELDS: Final = 4
_FSTAB_TARGET_FIELDS: Final = 2
_MOUNTS_MIN_FIELDS: Final = 4
_OCTAL_ESCAPE_RE: Final = re.compile(r"\\([0-7]{3})")
_USERNAME_RE: Final = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\$?$")
_BRACKET_SUFFIX_RE: Final = re.compile(r"\[[^\]]*\]$")

FINDMNT_COLUMNS: Final = "SOURCE,TARGET,FSTYPE,OPTIONS,UUID,FSROOT"
"""The columns `findmnt -J -l -o` is asked for: one call gives every mount with its filesystem UUID
and its root inside that filesystem (FSROOT: a bind mount's folder, a subvolume's path)."""


class FindmntFs(TypedDict):
    """One mount of `findmnt -J -l` (the columns of FINDMNT_COLUMNS); findmnt gives null for a value
    it does not know (a UUID it cannot read)."""

    source: str | None
    target: str
    fstype: str | None
    options: str | None
    uuid: str | None
    fsroot: str | None


_STR_OR_NULL: Final = (str, type(None))
_FINDMNT_REQUIRED: KeyTypes = {
    "target": (str,),
    **dict.fromkeys(("source", "fstype", "options", "uuid", "fsroot"), _STR_OR_NULL),
}


def is_findmnt_fs(value: object) -> TypeIs[FindmntFs]:
    """value is one mount of `findmnt -J -l -o FINDMNT_COLUMNS`."""
    return has_shape(value, _FINDMNT_REQUIRED, {})


class CacheMountAction(StrEnum):
    """Supported subcommands of `cst btrfs cache`."""

    STATUS = "status"
    PLAN = "plan"
    ATTACH = "attach"
    DETACH = "detach"
    HELP = "help"


class CacheMountKind(StrEnum):
    """Directory roles hosted on the dedicated cache drive."""

    CACHE = "cache"
    SCRATCHPAD = "scratchpad"


BASE_FOR_KIND: Final[dict[CacheMountKind, str]] = {
    CacheMountKind.CACHE: CACHE_BASE,
    CacheMountKind.SCRATCHPAD: SCRATCHPAD_BASE,
}
REL_FOR_KIND: Final[dict[CacheMountKind, str]] = {
    CacheMountKind.CACHE: CACHE_REL,
    CacheMountKind.SCRATCHPAD: SCRATCHPAD_REL,
}


@dataclass(frozen=True, slots=True)
class CacheMountParsedArgs:
    """Validated CLI arguments for `cst btrfs cache`."""

    action: CacheMountAction
    user: str | None = None
    include_cache: bool = True
    include_scratchpad: bool = True
    allow_nonempty: bool = False
    error: str | None = None
    code: int = 0


@dataclass(frozen=True, slots=True)
class CacheMountUser:
    """Target user identity and home directory."""

    name: str
    uid: int
    gid: int
    home: str


@dataclass(frozen=True, slots=True)
class CacheMountBinding:
    """One per-user source/target bind mount pair."""

    kind: CacheMountKind
    user: str
    base: str
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class ManagedFstabEntry:
    """One `/etc/fstab` line stamped with `CACHE_MOUNT_MARKER`."""

    source: str
    target: str
    user: str
    kind: CacheMountKind
    raw_line: str


@dataclass(frozen=True, slots=True)
class LiveMount:
    """One live mount as findmnt reports it (paths raw, no escapes; "" for a value it did not know)."""

    source: str
    target: str
    fstype: str
    options: str
    uuid: str
    fsroot: str


@dataclass(frozen=True, slots=True)
class BaseMountInfo:
    """Live and `/etc/fstab` state for `/var/mnt/cache` or `/var/mnt/scratchpad`."""

    path: str
    mounted: bool
    fstype: str
    uuid: str
    options: str
    in_fstab: bool
    fstab_options: str
    source: str = ""
    fsroot: str = ""


@dataclass(frozen=True, slots=True)
class BindingProbe:
    """Observed filesystem and mount state for one user binding."""

    binding: CacheMountBinding
    in_fstab: bool
    mounted_from_source: bool
    mounted_other: bool
    target_is_symlink: bool
    target_exists: bool
    target_nonempty: bool
    source_exists: bool
    fstab_unmanaged: bool = False
    source_is_symlink: bool = False
    target_not_dir: bool = False


@dataclass(frozen=True, slots=True)
class CacheMountSnapshot:
    """Complete read-only state collected for `status`, `plan`, `attach`, and `detach`."""

    user: CacheMountUser | None
    user_error: str | None
    home_uuid: str
    bases: dict[CacheMountKind, BaseMountInfo]
    bindings: tuple[BindingProbe, ...]
    all_managed: tuple[ManagedFstabEntry, ...]
    crypttab_text: str | None
    fstab_text: str | None
    mounts_error: str | None = None
    home_missing: bool = False


def is_cache_mount_help(parsed: CacheMountParsedArgs) -> bool:
    """Return True when CLI invocation asks for help."""
    return parsed.action == CacheMountAction.HELP


def cache_mount_parse_error(parsed: CacheMountParsedArgs) -> tuple[int, str] | None:
    """Return `(exit_code, message)` on CLI usage errors."""
    return None if parsed.error is None else (parsed.code, parsed.error)


def cache_mount_write_action(parsed: CacheMountParsedArgs) -> str | None:
    """Return subcommand name when the operation mutates the host."""
    return parsed.action.value if parsed.action in (CacheMountAction.ATTACH, CacheMountAction.DETACH) else None


def is_cache_mount_err(code: int, line: str) -> bool:
    """Return True when an output line should route to stderr."""
    return code != 0 and line.startswith(("ERROR:", "BLOCKED:"))


def is_valid_username(name: str) -> bool:
    """Validate POSIX account name before interpolating paths or fstab tags."""
    return bool(_USERNAME_RE.fullmatch(name))


def escape_mount_field(field: str) -> str:
    """Encode a path for one fstab field as the kernel and systemd-fstab-generator read it back:
    backslash, space, tab and newline as `\\134`, `\\040`, `\\011`, `\\012`."""
    return field.replace("\\", "\\134").replace(" ", "\\040").replace("\t", "\\011").replace("\n", "\\012")


def unescape_mount_field(field: str) -> str:
    """Decode octal escapes (`\\040`, `\\011`, ...) in fstab or `/proc/mounts` tokens."""
    return _OCTAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_all_mounts(text: str) -> tuple[MountEntry, ...]:
    """Parse `/proc/mounts` or `/etc/fstab` lines across all filesystem types."""
    entries: list[MountEntry] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) >= _MOUNTS_MIN_FIELDS:
            entries.append(
                MountEntry(
                    source=unescape_mount_field(fields[0]),
                    target=unescape_mount_field(fields[1]),
                    fstype=fields[2],
                    options=fields[3],
                )
            )
    return tuple(entries)


def parse_findmnt_mounts(text: str) -> tuple[LiveMount, ...] | None:
    """The mounts of `findmnt -J -l -o FINDMNT_COLUMNS`'s answer, in its order; None when the answer
    is not that JSON. A null value becomes ""."""
    try:
        doc = parse_json(text)
    except ValueError:
        return None
    if not json_object(doc):
        return None
    filesystems = doc.get("filesystems")
    if not list_of(filesystems, is_findmnt_fs):
        return None
    return tuple(
        LiveMount(
            source=fs["source"] or "",
            target=fs["target"],
            fstype=fs["fstype"] or "",
            options=fs["options"] or "",
            uuid=fs["uuid"] or "",
            fsroot=fs["fsroot"] or "",
        )
        for fs in filesystems
    )


def mounts_by_target(mounts: Sequence[LiveMount]) -> dict[str, LiveMount]:
    """The mount on top at each target: of mounts stacked on one target, findmnt lists the top one last."""
    return {m.target: m for m in mounts}


def mount_holding(mounts: Sequence[LiveMount], path: str) -> LiveMount | None:
    """The mount a path is on (what `findmnt --target PATH` answers): the one whose target is the
    longest leading folder of the path; of stacked ones, the last."""
    best: LiveMount | None = None
    for m in mounts:
        tgt = m.target.rstrip("/") or "/"
        inside = tgt in (path, "/") or path.startswith(f"{tgt}/")
        if inside and (best is None or len(tgt) >= len(best.target.rstrip("/") or "/")):
            best = m
    return best


def mount_device(source: str) -> str:
    """findmnt's SOURCE without the `[FSROOT]` it adds for a bind mount or a subvolume."""
    return _BRACKET_SUFFIX_RE.sub("", source)


def is_bind_from(live: LiveMount, base: BaseMountInfo, user: str) -> bool:
    """live is the bind mount of the base's folder for user: the same filesystem (its UUID, or its
    device when findmnt knows no UUID) and the root `<base FSROOT>/<user>` in it."""
    if not base.mounted:
        return False
    if live.uuid and base.uuid:
        same_fs = live.uuid == base.uuid
    else:
        dev = mount_device(live.source)
        same_fs = bool(dev) and dev == mount_device(base.source)
    return same_fs and live.fsroot == posixpath.join(base.fsroot or "/", user)


@dataclass(frozen=True, slots=True)
class _FlagState:
    user: str | None = None
    include_cache: bool = True
    include_scratchpad: bool = True
    allow_nonempty: bool = False


def _consume_user_flag(tok: str, next_tok: str | None, state: _FlagState) -> tuple[_FlagState, str | None, int]:
    if tok.startswith("--user="):
        val = tok.split("=", 1)[1].strip()
        if not val:
            return state, "--user requires a non-empty username", 0
        return replace(state, user=val), None, 1
    if next_tok is None or not next_tok.strip():
        return state, "--user requires a username", 0
    return replace(state, user=next_tok.strip()), None, 2


def _step_cache_mount_flag(
    action: CacheMountAction, tok: str, next_tok: str | None, state: _FlagState
) -> tuple[_FlagState, str | None, int]:
    if tok == "--user" or tok.startswith("--user="):
        return _consume_user_flag(tok, next_tok, state)
    if tok == "--no-cache" and action in (CacheMountAction.PLAN, CacheMountAction.ATTACH, CacheMountAction.DETACH):
        return replace(state, include_cache=False), None, 1
    if tok == "--no-scratchpad" and action in (CacheMountAction.PLAN, CacheMountAction.ATTACH, CacheMountAction.DETACH):
        return replace(state, include_scratchpad=False), None, 1
    if tok == "--allow-nonempty" and action in (CacheMountAction.PLAN, CacheMountAction.ATTACH):
        return replace(state, allow_nonempty=True), None, 1
    return state, f"unknown or unsupported option {tok!r} for `{action.value}`", 0


def parse_cache_mount_args(argv: Sequence[str]) -> CacheMountParsedArgs:
    """Parse `cst btrfs cache` arguments cleanly without touching I/O.

    A first word that is an option (`--user NAME`) means the default command, `status`, with it."""
    if argv and argv[0].startswith("-") and argv[0] not in HELP_WORDS:
        action, rest, head_err = CacheMountAction.STATUS, argv, None
    else:
        action, rest, head_err = parse_subcommand_head(
            argv, CacheMountAction, default=CacheMountAction.STATUS, help_action=CacheMountAction.HELP
        )
    if head_err is not None:
        return CacheMountParsedArgs(action=CacheMountAction.HELP, error=head_err, code=EXIT_USAGE)
    if any(tok in HELP_WORDS for tok in rest):
        return CacheMountParsedArgs(action=CacheMountAction.HELP)

    state = _FlagState()
    i = 0
    while i < len(rest):
        next_tok = rest[i + 1] if i + 1 < len(rest) else None
        state, err, step = _step_cache_mount_flag(action, rest[i], next_tok, state)
        if err is not None:
            return CacheMountParsedArgs(action=action, error=err, code=EXIT_USAGE)
        i += step

    if not state.include_cache and not state.include_scratchpad:
        return CacheMountParsedArgs(
            action=action, error="cannot combine --no-cache and --no-scratchpad", code=EXIT_USAGE
        )
    if state.user is not None and not is_valid_username(state.user):
        return CacheMountParsedArgs(action=action, error=f"invalid username {state.user!r}", code=EXIT_USAGE)
    return CacheMountParsedArgs(
        action=action,
        user=state.user,
        include_cache=state.include_cache,
        include_scratchpad=state.include_scratchpad,
        allow_nonempty=state.allow_nonempty,
    )


def selected_kinds(*, include_cache: bool = True, include_scratchpad: bool = True) -> tuple[CacheMountKind, ...]:
    """Return active `CacheMountKind` tuple matching caller flags."""
    kinds: list[CacheMountKind] = []
    if include_cache:
        kinds.append(CacheMountKind.CACHE)
    if include_scratchpad:
        kinds.append(CacheMountKind.SCRATCHPAD)
    return tuple(kinds)


def build_user_bindings(
    user: CacheMountUser, *, include_cache: bool = True, include_scratchpad: bool = True
) -> tuple[CacheMountBinding, ...]:
    """Construct `/var/mnt/<kind>/<user> -> <home>/<rel>` bindings for `user`."""
    home = user.home.rstrip("/") or "/"
    out: list[CacheMountBinding] = []
    for kind in selected_kinds(include_cache=include_cache, include_scratchpad=include_scratchpad):
        base = BASE_FOR_KIND[kind]
        rel = REL_FOR_KIND[kind]
        out.append(
            CacheMountBinding(
                kind=kind,
                user=user.name,
                base=base,
                source=f"{base}/{user.name}",
                target=f"{home}/{rel}",
            )
        )
    return tuple(out)


def render_bind_fstab_line(binding: CacheMountBinding) -> str:
    """Format one managed `/etc/fstab` bind-mount entry."""
    return (
        f"{escape_mount_field(binding.source)} {escape_mount_field(binding.target)} none "
        f"{CACHE_MOUNT_BIND_OPTS},x-systemd.requires-mounts-for={escape_mount_field(binding.base)} 0 0 "
        f"{CACHE_MOUNT_MARKER} user={binding.user} kind={binding.kind.value}"
    )


def _parse_one_managed_line(raw: str) -> ManagedFstabEntry | None:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#") or CACHE_MOUNT_MARKER not in raw:
        return None
    body, _, meta = raw.partition(CACHE_MOUNT_MARKER)
    fields = body.split()
    if len(fields) < _FSTAB_MIN_FIELDS:
        return None
    tags = dict(tok.split("=", 1) for tok in meta.split() if "=" in tok)
    kind = enum_member(CacheMountKind, tags.get("kind", ""))
    user = tags.get("user", "")
    if kind is None or not user:
        return None
    return ManagedFstabEntry(
        source=unescape_mount_field(fields[0]),
        target=unescape_mount_field(fields[1]),
        user=user,
        kind=kind,
        raw_line=raw.rstrip("\n"),
    )


def unmanaged_fstab_targets(fstab_text: str) -> frozenset[str]:
    """The targets (unescaped) of the /etc/fstab lines CST does not manage."""
    targets: set[str] = set()
    for raw in fstab_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or _parse_one_managed_line(raw) is not None:
            continue
        fields = stripped.split()
        if len(fields) >= _FSTAB_TARGET_FIELDS:
            targets.add(unescape_mount_field(fields[1]))
    return frozenset(targets)


def has_managed_line(managed: Sequence[ManagedFstabEntry], binding: CacheMountBinding) -> bool:
    """/etc/fstab has CST's line for this binding: its user and kind, with its source and target."""
    return any(
        (e.user, e.kind, e.source, e.target) == (binding.user, binding.kind, binding.source, binding.target)
        for e in managed
    )


def parse_managed_fstab_entries(fstab_text: str) -> tuple[ManagedFstabEntry, ...]:
    """Return every `btrfs-cache`-managed entry in `fstab_text`."""
    out: list[ManagedFstabEntry] = []
    for raw in fstab_text.splitlines():
        parsed = _parse_one_managed_line(raw)
        if parsed is not None:
            out.append(parsed)
    return tuple(out)


def plan_attach_fstab(fstab_text: str, bindings: Sequence[CacheMountBinding]) -> str:
    """Upsert managed bind lines for `bindings`, preserving all unrelated and other-user lines; a
    repeated managed line of a wanted (user, kind) goes (one line, one mount unit per target)."""
    wanted = {(b.user, b.kind): render_bind_fstab_line(b) for b in bindings}
    placed: set[tuple[str, CacheMountKind]] = set()
    out_lines: list[str] = []
    for line in fstab_text.splitlines():
        managed = _parse_one_managed_line(line)
        key = (managed.user, managed.kind) if managed is not None else None
        if key is None or key not in wanted:
            out_lines.append(line)
        elif key not in placed:
            out_lines.append(wanted[key])
            placed.add(key)
    out_lines.extend(text for key, text in wanted.items() if key not in placed)
    return "\n".join(out_lines) + "\n" if out_lines else ""


def plan_detach_fstab(fstab_text: str, user: str, kinds: Collection[CacheMountKind]) -> str:
    """Remove only `user`/`kinds` managed bind lines, keeping all other users' lines."""
    out_lines: list[str] = []
    for line in fstab_text.splitlines():
        managed = _parse_one_managed_line(line)
        if managed is not None and managed.user == user and managed.kind in kinds:
            continue
        out_lines.append(line)
    return "\n".join(out_lines) + "\n" if out_lines else ""


def _base_blockers(base: BaseMountInfo | None, base_path: str, home_uuid: str) -> list[str]:
    if base is None or not base.mounted:
        return [f"{base_path} is not mounted (provision & mount the cache drive's btrfs subvolumes first)"]
    if base.fstype != FSTYPE_BTRFS:
        return [f"{base_path} filesystem is {base.fstype or 'unknown'}, expected {FSTYPE_BTRFS}"]
    if not base.uuid or not home_uuid:
        return [f"cannot verify filesystem UUID separation between {base_path} and target home directory"]
    if base.uuid == home_uuid:
        return [f"{base_path} shares filesystem UUID {base.uuid} with target home (refusing same-filesystem bind)"]
    return []


def _target_blockers(probe: BindingProbe, *, allow_nonempty: bool) -> list[str]:
    b = probe.binding
    blockers = (
        (probe.target_is_symlink, f"{b.target} is a symlink (refusing to follow or overwrite symlink)"),
        (probe.source_is_symlink, f"{b.source} is a symlink (refusing to follow it)"),
        (probe.target_not_dir, f"{b.target} exists and is not a folder"),
        (probe.fstab_unmanaged, f"{b.target} has an /etc/fstab line CST does not manage; remove it first"),
        (probe.mounted_other, f"{b.target} already has a mount CST did not make (not {b.source}); unmount it first"),
        (
            probe.target_nonempty and not allow_nonempty and not probe.mounted_from_source,
            f"{b.target} is non-empty; move existing files to {b.source} first or pass --allow-nonempty",
        ),
    )
    return next(([msg] for on, msg in blockers if on), [])


def validate_attach_snapshot(snap: CacheMountSnapshot, *, allow_nonempty: bool = False) -> list[str]:
    """Return blocking error reasons preventing safe `attach`, or empty list when safe."""
    if snap.user is None:
        return [snap.user_error or "cannot resolve target user"]
    if snap.mounts_error is not None:
        return [f"cannot list mounts: {snap.mounts_error}"]
    if snap.home_missing:
        return [f"home folder {snap.user.home} does not exist"]
    errors: list[str] = []
    checked_bases: set[CacheMountKind] = set()
    for probe in snap.bindings:
        kind = probe.binding.kind
        if kind not in checked_bases:
            errors.extend(_base_blockers(snap.bases.get(kind), probe.binding.base, snap.home_uuid))
            checked_bases.add(kind)
        errors.extend(_target_blockers(probe, allow_nonempty=allow_nonempty))
    return errors


def has_mount_option(options: str, needle: str) -> bool:
    return needle in (tok.strip() for tok in options.split(",") if tok.strip())


def format_cache_mount_usage(prog: str) -> str:
    """Usage and help text for `cst btrfs cache`."""
    return (
        "Multi-user bind mounts from a dedicated NVMe cache drive (~/.cache and ~/Scratchpad).\n"
        "\n"
        "Commands:\n"
        "  status [--user USER]                 read-only: show base mounts, user bindings, and managed fstab\n"
        "                                       (works as your user and inside a toolbox; default command)\n"
        "  plan [OPTIONS]                       dry run showing directories, fstab edits, and blockers\n"
        "  attach [OPTIONS]                     (root, host only) create 0700 dirs, CACHEDIR.TAG, update fstab, mount\n"
        "  detach [OPTIONS]                     (root, host only) unmount and remove fstab lines; keeps NVMe data\n"
        "  help                                 show this help\n"
        "\n"
        "Options:\n"
        "  --user NAME                          target user account (default: $SUDO_USER or current user)\n"
        "  --no-cache                           skip ~/.cache binding (/var/mnt/cache/<user>)\n"
        "  --no-scratchpad                      skip ~/Scratchpad binding (/var/mnt/scratchpad/<user>)\n"
        "  --allow-nonempty                     (plan/attach) allow bind-mounting over non-empty target directory\n"
        "\n"
        f"Usage: {prog} <status|plan|attach|detach|help> [options]\n"
    )


def _binding_state_label(probe: BindingProbe) -> str:
    if probe.mounted_from_source:
        return "active (fstab + mounted)" if probe.in_fstab else "mounted (not in fstab)"
    states = (
        (probe.mounted_other, "mounted (other source!)"),
        (probe.fstab_unmanaged, "fstab line CST does not manage (blocked)"),
        (probe.in_fstab, "in fstab (not mounted)"),
        (probe.target_is_symlink, "symlink (blocked)"),
        (probe.source_is_symlink, "source is a symlink (blocked)"),
        (probe.target_not_dir, "target is not a folder (blocked)"),
        (probe.target_nonempty, "unattached (target non-empty)"),
    )
    return next((label for on, label in states if on), "not attached")


def format_cache_mount_status(snap: CacheMountSnapshot, *, seam_root: str = "") -> list[str]:
    """Render lines for `cst btrfs cache status`."""
    lines: list[str] = []
    if seam_root:
        lines.append(f"test seam (CST_BTRFS_CACHE_ROOT): {seam_root}")
    lines.append("Dedicated cache drive mounts:")
    if snap.mounts_error is not None:
        return [*lines, f"  cannot list mounts: {snap.mounts_error}"]
    for kind in (CacheMountKind.CACHE, CacheMountKind.SCRATCHPAD):
        base = snap.bases[kind]
        if base.mounted:
            flush_note = " [WARN: flushoncommit]" if has_mount_option(base.options, OPT_FLUSHONCOMMIT) else ""
            lines.append(f"  {base.path:<18} mounted ({base.fstype}, uuid={base.uuid or 'unknown'}){flush_note}")
        elif base.in_fstab:
            lines.append(f"  {base.path:<18} in fstab (not mounted)")
        else:
            lines.append(f"  {base.path:<18} not provisioned")
    if snap.user is None:
        lines.append(f"  target user        unavailable ({snap.user_error or 'unknown'})")
    else:
        lines.append(f"  target user        {snap.user.name} (uid={snap.user.uid}, home={snap.user.home})")
        for probe in snap.bindings:
            b = probe.binding
            lines.append(f"  {b.kind.value:<18} {b.source} -> {b.target} [{_binding_state_label(probe)}]")
    other_users = sorted({e.user for e in snap.all_managed if snap.user is None or e.user != snap.user.name})
    if other_users:
        lines.append(f"  other fstab users  {', '.join(other_users)}")
    return lines


def _plan_binding_line(probe: BindingProbe) -> str:
    b = probe.binding
    actions: list[str] = []
    if not probe.source_exists:
        actions.append(f"mkdir 0700 {b.source}")
    if b.kind == CacheMountKind.SCRATCHPAD:
        actions.append("ensure CACHEDIR.TAG")
    if not probe.in_fstab:
        actions.append("add /etc/fstab entry")
    if not probe.mounted_from_source:
        actions.append("mount --bind")
    summary = ", ".join(actions) if actions else "already attached"
    return f"  {b.kind.value:<10} {b.source} -> {b.target}: {summary}"


def format_cache_mount_plan(
    snap: CacheMountSnapshot, prog: str, *, allow_nonempty: bool = False, seam_root: str = ""
) -> list[str]:
    """Render dry-run lines for `cst btrfs cache plan`."""
    lines: list[str] = []
    if seam_root:
        lines.append(f"test seam (CST_BTRFS_CACHE_ROOT): {seam_root}")
    if snap.user is None:
        return [*lines, f"BLOCKED: {snap.user_error or 'cannot resolve target user'}"]
    lines.append(f"Target user: {snap.user.name} (uid={snap.user.uid}, home={snap.user.home})")
    lines.extend(_plan_binding_line(probe) for probe in snap.bindings)
    blockers = validate_attach_snapshot(snap, allow_nonempty=allow_nonempty)
    if blockers:
        lines.extend(f"BLOCKED: {msg}" for msg in blockers)
        return lines
    if all(p.in_fstab and p.mounted_from_source for p in snap.bindings):
        lines.append(f"Already attached for {snap.user.name}; nothing to do.")
        return lines
    user_flag = f" --user {snap.user.name}"
    extra = " --allow-nonempty" if allow_nonempty else ""
    lines.append(f"Dry run, nothing changed. Run `{prog} attach{user_flag}{extra}` to apply.")
    return lines

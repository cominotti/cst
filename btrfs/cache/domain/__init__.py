# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst btrfs cache`: CLI parsing, fstab planning, safety checks (cst doctor's check:
`health.py`)."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
import posixpath
import re
from typing import Final, TypedDict, TypeIs

from btrfs.compress.domain import MountEntry
from cst_core.domain.shapes import KeyTypes, has_shape, json_object, list_of, parse_json
from cst_core.domain.text import (
    HELP_WORDS,
    enum_member,
    file_lines,
    group,
    is_mount_comment,
    mount_fields,
    parse_subcommand_head,
    strip_ascii_blanks,
    strip_mount_blanks,
    unescape_mount_field,
)

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
CACHE_SEAM_ROOT_ENV: Final = "CST_BTRFS_CACHE_ROOT"
"""The test root: every host path below this folder (status, plan, attach and detach say so first)."""

FSTYPE_BTRFS: Final = "btrfs"
OPT_COMPRESS_FORCE: Final = "compress-force=zstd:1"
PROVISION_BASE_MARKER: Final = "# managed-by: btrfs-cache (CST) provision=base"
FSTAB_MIN_FIELDS: Final = 4
_FSTAB_TARGET_FIELDS: Final = 2
_MOUNTS_MIN_FIELDS: Final = 4
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
    PROVISION = "provision"
    UNPROVISION = "unprovision"
    SYSTEM = "system"
    HELP = "help"


class ProvisionSubcommand(StrEnum):
    """Supported subcommands of `cst btrfs cache provision`."""

    PLAN = "plan"
    INIT = "init"
    APPLY = "apply"
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
    migrate: bool = False
    allow_busy: bool = False
    provision_action: ProvisionSubcommand | None = None
    device: str | None = None
    mapper: str | None = None
    do_format: bool = False
    assume_yes: bool = False
    as_root: bool = False
    system_argv: tuple[str, ...] = ()
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
    source_unchecked: bool = False
    """A mount is at the target, but the base is not mounted: whether it is CST's bind cannot be told."""
    bind_below: bool = False
    """The mount on top is not CST's, and CST's bind is below it."""


@dataclass(frozen=True, slots=True)
class BusyProcess:
    """A running process holding an open fd or cwd inside `~/.cache`."""

    pid: int
    comm: str
    path: str


@dataclass(frozen=True, slots=True)
class MigrationProbe:
    """Read-only facts gathered before deciding `~/.cache --migrate`."""

    backup_path: str
    backup_exists: bool
    source_nonempty: bool
    target_is_subvol: bool
    busy_procs: tuple[BusyProcess, ...] = ()
    busy_error: str | None = None


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
    fstab_error: str | None = None
    """Why /etc/fstab, which is there, could not be read (attach, detach and plan refuse)."""
    fstab_behind: bool | None = False
    """Whether systemd has not loaded /etc/fstab as it is on disk (None: systemctl cannot say). Asked by
    plan, only when attach would write no /etc/fstab (systemd_behind_fstab, with the binds attach would
    mount), and by status where the cache is set up (fstab_lines_unread, every managed line); False
    otherwise."""
    migration: MigrationProbe | None = None


def is_cache_mount_help(parsed: CacheMountParsedArgs) -> bool:
    """Return True when CLI invocation asks for help."""
    return parsed.action == CacheMountAction.HELP or parsed.provision_action == ProvisionSubcommand.HELP


def cache_mount_parse_error(parsed: CacheMountParsedArgs) -> tuple[int, str] | None:
    """Return `(exit_code, message)` on CLI usage errors."""
    return None if parsed.error is None else (parsed.code, parsed.error)


def cache_mount_write_action(parsed: CacheMountParsedArgs) -> str | None:
    """Return subcommand name when the operation mutates the host (or needs root via `--root`)."""
    if parsed.action in (CacheMountAction.ATTACH, CacheMountAction.DETACH, CacheMountAction.UNPROVISION):
        return parsed.action.value
    if parsed.action == CacheMountAction.PROVISION:
        if parsed.provision_action in (ProvisionSubcommand.INIT, ProvisionSubcommand.APPLY):
            return f"provision {parsed.provision_action.value}"
        if parsed.provision_action == ProvisionSubcommand.PLAN and parsed.as_root:
            return "provision plan --root"
    return None


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


def parse_all_mounts(text: str) -> tuple[MountEntry, ...]:
    """Parse `/proc/mounts` or `/etc/fstab` lines across all filesystem types (only a newline ends
    a line, as the kernel and systemd-fstab-generator read them; the fields split at spaces and tabs only, as
    libmount splits them)."""
    entries: list[MountEntry] = []
    for line in file_lines(text):
        if is_mount_comment(line):
            continue
        fields = mount_fields(line)
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


def mount_stacks(mounts: Sequence[LiveMount]) -> dict[str, tuple[LiveMount, ...]]:
    """Every mount at each target, bottom first (findmnt lists the top one last)."""
    stacks: dict[str, list[LiveMount]] = {}
    for m in mounts:
        stacks.setdefault(m.target, []).append(m)
    return {target: tuple(stack) for target, stack in stacks.items()}


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


def source_fsroot(source: str) -> str | None:
    """The FSROOT in the `[FSROOT]` findmnt adds to a bind's or a subvolume's SOURCE; None when the
    source shows none (/proc/mounts never does; findmnt not for a filesystem's own root)."""
    m = _BRACKET_SUFFIX_RE.search(source)
    return group(m)[1:-1] if m is not None else None


def bind_matches_fsroot(live: LiveMount, base: BaseMountInfo, rel_subpath: str) -> bool:
    """Return True when `live` bind-mounts `<base.fsroot>/<rel_subpath>` on the same filesystem."""
    if not base.mounted:
        return False
    if live.uuid and base.uuid:
        same_fs = live.uuid == base.uuid
    else:
        dev = mount_device(live.source)
        same_fs = bool(dev) and dev == mount_device(base.source)
    return same_fs and live.fsroot == posixpath.join(base.fsroot or "/", rel_subpath.lstrip("/"))


def is_bind_from(live: LiveMount, base: BaseMountInfo, user: str) -> bool:
    """live is the bind mount of the base's folder for user: the same filesystem (its UUID, or its
    device when findmnt knows no UUID) and the root `<base FSROOT>/<user>` in it."""
    return bind_matches_fsroot(live, base, user)


@dataclass(frozen=True, slots=True)
class _FlagState:
    user: str | None = None
    include_cache: bool = True
    include_scratchpad: bool = True
    allow_nonempty: bool = False
    migrate: bool = False
    allow_busy: bool = False
    device: str | None = None
    mapper: str | None = None
    do_format: bool = False
    assume_yes: bool = False
    as_root: bool = False


_VALUE_FLAG_SPECS: Final[dict[str, tuple[str, str]]] = {
    "--user": ("user", "username"),
    "--device": ("device", "value"),
    "--mapper": ("mapper", "value"),
}
_CACHE_BOOL_FLAGS: Final[dict[str, tuple[tuple[CacheMountAction, ...], str, bool]]] = {
    "--no-cache": ((CacheMountAction.PLAN, CacheMountAction.ATTACH, CacheMountAction.DETACH), "include_cache", False),
    "--no-scratchpad": (
        (CacheMountAction.PLAN, CacheMountAction.ATTACH, CacheMountAction.DETACH),
        "include_scratchpad",
        False,
    ),
    "--allow-nonempty": ((CacheMountAction.PLAN, CacheMountAction.ATTACH), "allow_nonempty", True),
    "--migrate": ((CacheMountAction.PLAN, CacheMountAction.ATTACH), "migrate", True),
    "--allow-busy": ((CacheMountAction.PLAN, CacheMountAction.ATTACH), "allow_busy", True),
}


def _consume_value_flag(
    flag: str, tok: str, next_tok: str | None, state: _FlagState
) -> tuple[_FlagState, str | None, int]:
    """Parse `--FLAG=VALUE` or `--FLAG VALUE`, keeping interior characters and trimming ASCII blanks."""
    attr, noun = _VALUE_FLAG_SPECS[flag]
    if tok.startswith(f"{flag}="):
        val = strip_ascii_blanks(tok.split("=", 1)[1])
        return (
            (state, f"{flag} requires a non-empty {noun}", 0) if not val else (replace(state, **{attr: val}), None, 1)
        )
    if next_tok is None or not strip_ascii_blanks(next_tok):
        return state, f"{flag} requires a {noun}", 0
    return replace(state, **{attr: strip_ascii_blanks(next_tok)}), None, 2


def _step_cache_mount_flag(
    action: CacheMountAction, tok: str, next_tok: str | None, state: _FlagState
) -> tuple[_FlagState, str | None, int]:
    if tok == "--user" or tok.startswith("--user="):
        return _consume_value_flag("--user", tok, next_tok, state)
    rule = _CACHE_BOOL_FLAGS.get(tok)
    if rule is not None and action in rule[0]:
        return replace(state, **{rule[1]: rule[2]}), None, 1
    return state, f"unknown or unsupported option {tok!r} for `{action.value}`", 0


def _step_provision_flag(
    sub: ProvisionSubcommand, tok: str, next_tok: str | None, state: _FlagState
) -> tuple[_FlagState, str | None, int]:
    for vflag in ("--device", "--mapper"):
        if (tok == vflag or tok.startswith(f"{vflag}=")) and sub in (
            ProvisionSubcommand.PLAN,
            ProvisionSubcommand.INIT,
        ):
            return _consume_value_flag(vflag, tok, next_tok, state)
    if tok == "--format" and sub in (ProvisionSubcommand.PLAN, ProvisionSubcommand.APPLY):
        return replace(state, do_format=True), None, 1
    if tok == "--root" and sub == ProvisionSubcommand.PLAN:
        return replace(state, as_root=True), None, 1
    if tok == "--yes" and sub == ProvisionSubcommand.APPLY:
        return replace(state, assume_yes=True), None, 1
    return state, f"unknown or unsupported option {tok!r} for `provision {sub.value}`", 0


def _parse_provision_args(argv: Sequence[str]) -> CacheMountParsedArgs:
    sub, rest, head_err = parse_subcommand_head(
        argv, ProvisionSubcommand, default=ProvisionSubcommand.PLAN, help_action=ProvisionSubcommand.HELP
    )
    if head_err is not None:
        return CacheMountParsedArgs(
            action=CacheMountAction.PROVISION,
            provision_action=ProvisionSubcommand.HELP,
            error=head_err,
            code=EXIT_USAGE,
        )
    if sub == ProvisionSubcommand.HELP or any(tok in HELP_WORDS for tok in rest):
        return CacheMountParsedArgs(action=CacheMountAction.PROVISION, provision_action=ProvisionSubcommand.HELP)
    state = _FlagState()
    err: str | None = None
    i = 0
    while i < len(rest):
        next_tok = rest[i + 1] if i + 1 < len(rest) else None
        state, err, step = _step_provision_flag(sub, rest[i], next_tok, state)
        if err is not None:
            return CacheMountParsedArgs(
                action=CacheMountAction.PROVISION, provision_action=sub, error=err, code=EXIT_USAGE
            )
        i += step
    if sub == ProvisionSubcommand.INIT and state.device is None:
        err = "`provision init` requires --device PATH"
    elif state.mapper is not None and "/" in state.mapper:
        err = f"--mapper must be a plain mapper name, not a path: {state.mapper!r}"
    if err is not None:
        return CacheMountParsedArgs(CacheMountAction.PROVISION, provision_action=sub, error=err, code=EXIT_USAGE)
    return CacheMountParsedArgs(
        action=CacheMountAction.PROVISION,
        provision_action=sub,
        device=state.device,
        mapper=state.mapper,
        do_format=state.do_format,
        assume_yes=state.assume_yes,
        as_root=state.as_root,
    )


def _mount_flag_error(action: CacheMountAction, state: _FlagState) -> str | None:
    if not state.include_cache and not state.include_scratchpad:
        return "cannot combine --no-cache and --no-scratchpad"
    if state.migrate and state.allow_nonempty:
        return "cannot combine --migrate and --allow-nonempty"
    if state.migrate and not state.include_cache:
        return "cannot combine --migrate and --no-cache"
    if state.allow_busy and not state.migrate:
        return "--allow-busy requires --migrate"
    if state.user is not None and not is_valid_username(state.user):
        return f"invalid username {state.user!r}"
    del action
    return None


def _parse_mount_flags(action: CacheMountAction, rest: Sequence[str]) -> CacheMountParsedArgs:
    state = _FlagState()
    i = 0
    while i < len(rest):
        next_tok = rest[i + 1] if i + 1 < len(rest) else None
        state, err, step = _step_cache_mount_flag(action, rest[i], next_tok, state)
        if err is not None:
            return CacheMountParsedArgs(action=action, error=err, code=EXIT_USAGE)
        i += step
    if (flag_err := _mount_flag_error(action, state)) is not None:
        return CacheMountParsedArgs(action=action, error=flag_err, code=EXIT_USAGE)
    return CacheMountParsedArgs(
        action=action,
        user=state.user,
        include_cache=state.include_cache,
        include_scratchpad=state.include_scratchpad,
        allow_nonempty=state.allow_nonempty,
        migrate=state.migrate,
        allow_busy=state.allow_busy,
    )


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
    if action == CacheMountAction.HELP or (
        action not in (CacheMountAction.PROVISION, CacheMountAction.SYSTEM) and any(tok in HELP_WORDS for tok in rest)
    ):
        return CacheMountParsedArgs(action=CacheMountAction.HELP)
    if action in (CacheMountAction.PROVISION, CacheMountAction.SYSTEM):
        return (
            _parse_provision_args(rest)
            if action == CacheMountAction.PROVISION
            else CacheMountParsedArgs(action=action, system_argv=tuple(rest))
        )
    return _parse_mount_flags(action, rest)


def selected_kinds(*, include_cache: bool = True, include_scratchpad: bool = True) -> tuple[CacheMountKind, ...]:
    """Return active `CacheMountKind` tuple matching caller flags."""
    return tuple(
        k for k, on in ((CacheMountKind.CACHE, include_cache), (CacheMountKind.SCRATCHPAD, include_scratchpad)) if on
    )


def build_user_bindings(
    user: CacheMountUser, *, include_cache: bool = True, include_scratchpad: bool = True
) -> tuple[CacheMountBinding, ...]:
    """Construct `/var/mnt/<kind>/<user> -> <home>/<rel>` bindings for `user`."""
    home = user.home.rstrip("/") or "/"
    return tuple(
        CacheMountBinding(
            kind=k,
            user=user.name,
            base=BASE_FOR_KIND[k],
            source=f"{BASE_FOR_KIND[k]}/{user.name}",
            target=f"{home}/{REL_FOR_KIND[k]}",
        )
        for k in selected_kinds(include_cache=include_cache, include_scratchpad=include_scratchpad)
    )


def render_bind_fstab_line(binding: CacheMountBinding) -> str:
    """Format one managed `/etc/fstab` bind-mount entry."""
    return (
        f"{escape_mount_field(binding.source)} {escape_mount_field(binding.target)} none "
        f"{CACHE_MOUNT_BIND_OPTS},x-systemd.requires-mounts-for={escape_mount_field(binding.base)} 0 0 "
        f"{CACHE_MOUNT_MARKER} user={binding.user} kind={binding.kind.value}"
    )


def split_tagged_fstab_line(raw: str, marker: str) -> tuple[list[str], dict[str, str]] | None:
    """Split a non-comment tagged fstab line into `(mount_fields, tag_map)`."""
    if is_mount_comment(raw) or marker not in raw:
        return None
    body, _, meta = raw.partition(marker)
    fields = mount_fields(body)
    if len(fields) < FSTAB_MIN_FIELDS:
        return None
    tags = {key: value for key, sep, value in (tok.partition("=") for tok in meta.split()) if sep}
    return fields, tags


def _parse_one_managed_line(raw: str) -> ManagedFstabEntry | None:
    parsed = split_tagged_fstab_line(raw, CACHE_MOUNT_MARKER)
    if parsed is None:
        return None
    fields, tags = parsed
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


def unmanaged_fstab_targets(fstab_text: str, *, known_lines: Collection[str] = ()) -> frozenset[str]:
    """The targets (unescaped) of the /etc/fstab lines CST does not manage."""
    targets: set[str] = set()
    for raw in file_lines(fstab_text):
        if raw in known_lines or is_mount_comment(raw) or _parse_one_managed_line(raw) is not None:
            continue
        fields = mount_fields(raw)
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
    for raw in file_lines(fstab_text):
        parsed = _parse_one_managed_line(raw)
        if parsed is not None:
            out.append(parsed)
    return tuple(out)


def upsert_marked_lines[K](text: str, wanted: Mapping[K, str], classify: Callable[[str], K | None]) -> str:
    """Replace lines whose `classify(line)` key is in `wanted` in place (deduping extras) and append missing keys."""
    placed: set[K] = set()
    out_lines: list[str] = []
    for raw in file_lines(text):
        key = classify(raw)
        if key is None or key not in wanted:
            out_lines.append(raw)
        elif key not in placed:
            out_lines.append(wanted[key])
            placed.add(key)
    out_lines.extend(line for key, line in wanted.items() if key not in placed)
    return "\n".join(out_lines) + "\n" if out_lines else ""


def _managed_key(raw: str) -> tuple[str, CacheMountKind] | None:
    managed = _parse_one_managed_line(raw)
    return (managed.user, managed.kind) if managed is not None else None


def plan_attach_fstab(fstab_text: str, bindings: Sequence[CacheMountBinding]) -> str:
    """Upsert managed bind lines for `bindings`, preserving all unrelated and other-user lines; a
    repeated managed line of a wanted (user, kind) goes (one line, one mount unit per target). Only a
    newline ends a line: every other line keeps its bytes, a carriage return or a form feed included."""
    wanted = {(b.user, b.kind): render_bind_fstab_line(b) for b in bindings}
    return upsert_marked_lines(fstab_text, wanted, _managed_key)


def plan_detach_fstab(fstab_text: str, user: str, kinds: Collection[CacheMountKind]) -> str:
    """Remove only `user`/`kinds` managed bind lines, keeping all other users' lines (and their bytes)."""
    out_lines: list[str] = []
    for line in file_lines(fstab_text):
        managed = _parse_one_managed_line(line)
        if managed is not None and managed.user == user and managed.kind in kinds:
            continue
        out_lines.append(line)
    return "\n".join(out_lines) + "\n" if out_lines else ""


def snapshot_error(snap: CacheMountSnapshot) -> str | None:
    """Why the snapshot cannot carry a write: no target user, no mount table, or an /etc/fstab that
    is there but cannot be read; None when it can."""
    if snap.user is None:
        return snap.user_error or "cannot resolve target user"
    if snap.mounts_error is not None:
        return f"cannot list mounts: {snap.mounts_error}"
    return snap.fstab_error


def detach_blockers(snap: CacheMountSnapshot) -> list[str]:
    """Why detach must change nothing: a target whose mount it cannot tell (its base not mounted), or
    one where CST's bind is under a mount CST did not make (umount would take that one away)."""
    errors: list[str] = []
    for probe in snap.bindings:
        b = probe.binding
        if probe.source_unchecked:
            errors.append(
                f"{b.target} has a mount and {b.base} is not mounted, so CST cannot tell whether it is its "
                f"bind; mount {b.base} and run detach again"
            )
        elif probe.bind_below:
            errors.append(
                f"{b.target} has a mount CST did not make on top of CST's bind of {b.source}; "
                "unmount that one first, then run detach again"
            )
    return errors


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


def validate_attach_snapshot(
    snap: CacheMountSnapshot,
    *,
    allow_nonempty: bool = False,
    allow_nonempty_kinds: Collection[CacheMountKind] = (),
) -> list[str]:
    """Return blocking error reasons preventing safe `attach`, or empty list when safe."""
    err = snapshot_error(snap)
    if err is not None or snap.user is None:
        return [err or "cannot resolve target user"]
    if snap.home_missing:
        return [f"home folder {snap.user.home} does not exist"]
    errors: list[str] = []
    checked_bases: set[CacheMountKind] = set()
    for probe in snap.bindings:
        kind = probe.binding.kind
        if kind not in checked_bases:
            errors.extend(_base_blockers(snap.bases.get(kind), probe.binding.base, snap.home_uuid))
            checked_bases.add(kind)
        errors.extend(_target_blockers(probe, allow_nonempty=allow_nonempty or kind in allow_nonempty_kinds))
    return errors


def has_mount_option(options: str, needle: str) -> bool:
    return needle in (tok for tok in (strip_mount_blanks(t) for t in options.split(",")) if tok)

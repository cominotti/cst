# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst btrfs scratch`: CLI parsing, fstab planning, safety checks, health."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
import re
from typing import Final

from btrfs.compress.domain import MountEntry
from cst_core.domain.text import HELP_WORDS, enum_member, parse_subcommand_head

CACHE_BASE: Final = "/var/mnt/cache"
SCRATCH_BASE: Final = "/var/mnt/scratch"
CACHE_REL: Final = ".cache"
SCRATCH_REL: Final = "Scratch"
SCRATCH_MARKER: Final = "# managed-by: btrfs-scratch (CST)"
SCRATCH_BIND_OPTS: Final = "bind,nofail"

MIN_HUMAN_UID: Final = 1000
DIR_MODE_PRIVATE: Final = 0o700
FILE_MODE_TAG: Final = 0o644
EXIT_USAGE: Final = 2

_BTRFS: Final = "btrfs"
_FLUSHONCOMMIT: Final = "flushoncommit"
_NOFAIL: Final = "nofail"
_CRYPTTAB_MIN_FIELDS: Final = 2
_CRYPTTAB_OPT_IDX: Final = 3
_FSTAB_MIN_FIELDS: Final = 4
_MOUNTS_MIN_FIELDS: Final = 4
_OCTAL_ESCAPE_RE: Final = re.compile(r"\\([0-7]{3})")
_USERNAME_RE: Final = re.compile(r"^[a-z_][a-z0-9_-]{0,31}\$?$")


class ScratchAction(StrEnum):
    """Supported subcommands of `cst btrfs scratch`."""

    STATUS = "status"
    PLAN = "plan"
    ATTACH = "attach"
    DETACH = "detach"
    HELP = "help"


class ScratchKind(StrEnum):
    """Directory roles hosted on the dedicated scratch filesystem."""

    CACHE = "cache"
    SCRATCH = "scratch"


class ScratchFindingSeverity(StrEnum):
    """Severity levels emitted by `assess_scratch_health` for `cst doctor`."""

    OK = "ok"
    INFO = "info"
    WARN = "warn"


BASE_FOR_KIND: Final[dict[ScratchKind, str]] = {
    ScratchKind.CACHE: CACHE_BASE,
    ScratchKind.SCRATCH: SCRATCH_BASE,
}
REL_FOR_KIND: Final[dict[ScratchKind, str]] = {
    ScratchKind.CACHE: CACHE_REL,
    ScratchKind.SCRATCH: SCRATCH_REL,
}


@dataclass(frozen=True, slots=True)
class ScratchParsedArgs:
    """Validated CLI arguments for `cst btrfs scratch`."""

    action: ScratchAction
    user: str | None = None
    include_cache: bool = True
    include_scratch: bool = True
    allow_nonempty: bool = False
    error: str | None = None
    code: int = 0


@dataclass(frozen=True, slots=True)
class ScratchUser:
    """Target user identity and home directory."""

    name: str
    uid: int
    gid: int
    home: str


@dataclass(frozen=True, slots=True)
class ScratchBinding:
    """One per-user source/target bind mount pair."""

    kind: ScratchKind
    user: str
    base: str
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class ManagedFstabEntry:
    """One `/etc/fstab` line stamped with `SCRATCH_MARKER`."""

    source: str
    target: str
    user: str
    kind: ScratchKind
    raw_line: str


@dataclass(frozen=True, slots=True)
class BaseMountInfo:
    """Live and `/etc/fstab` state for `/var/mnt/cache` or `/var/mnt/scratch`."""

    path: str
    mounted: bool
    fstype: str
    uuid: str
    options: str
    in_fstab: bool
    fstab_options: str


@dataclass(frozen=True, slots=True)
class BindingProbe:
    """Observed filesystem and mount state for one user binding."""

    binding: ScratchBinding
    in_fstab: bool
    mounted_from_source: bool
    mounted_other: bool
    target_is_symlink: bool
    target_exists: bool
    target_nonempty: bool
    source_exists: bool


@dataclass(frozen=True, slots=True)
class ScratchSnapshot:
    """Complete read-only state collected for `status`, `plan`, `attach`, and `detach`."""

    user: ScratchUser | None
    user_error: str | None
    home_uuid: str
    bases: dict[ScratchKind, BaseMountInfo]
    bindings: tuple[BindingProbe, ...]
    all_managed: tuple[ManagedFstabEntry, ...]
    crypttab_text: str | None
    fstab_text: str | None
    proc_mounts_text: str | None


@dataclass(frozen=True, slots=True)
class ScratchDoctorFinding:
    """One diagnostic item produced by `assess_scratch_health`."""

    severity: ScratchFindingSeverity
    message: str
    fix: str = ""


def is_scratch_help(parsed: ScratchParsedArgs) -> bool:
    """Return True when CLI invocation asks for help."""
    return parsed.action == ScratchAction.HELP


def scratch_parse_error(parsed: ScratchParsedArgs) -> tuple[int, str] | None:
    """Return `(exit_code, message)` on CLI usage errors."""
    return None if parsed.error is None else (parsed.code, parsed.error)


def scratch_write_action(parsed: ScratchParsedArgs) -> str | None:
    """Return subcommand name when the operation mutates the host."""
    return parsed.action.value if parsed.action in (ScratchAction.ATTACH, ScratchAction.DETACH) else None


def is_scratch_err(code: int, line: str) -> bool:
    """Return True when an output line should route to stderr."""
    return code != 0 and line.startswith(("ERROR:", "BLOCKED:"))


def is_valid_username(name: str) -> bool:
    """Validate POSIX account name before interpolating paths or fstab tags."""
    return bool(_USERNAME_RE.fullmatch(name))


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


@dataclass(frozen=True, slots=True)
class _FlagState:
    user: str | None = None
    include_cache: bool = True
    include_scratch: bool = True
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


def _step_scratch_flag(
    action: ScratchAction, tok: str, next_tok: str | None, state: _FlagState
) -> tuple[_FlagState, str | None, int]:
    if tok == "--user" or tok.startswith("--user="):
        return _consume_user_flag(tok, next_tok, state)
    if tok == "--no-cache" and action in (ScratchAction.PLAN, ScratchAction.ATTACH, ScratchAction.DETACH):
        return replace(state, include_cache=False), None, 1
    if tok == "--no-scratch" and action in (ScratchAction.PLAN, ScratchAction.ATTACH, ScratchAction.DETACH):
        return replace(state, include_scratch=False), None, 1
    if tok == "--allow-nonempty" and action in (ScratchAction.PLAN, ScratchAction.ATTACH):
        return replace(state, allow_nonempty=True), None, 1
    return state, f"unknown or unsupported option {tok!r} for `{action.value}`", 0


def parse_scratch_args(argv: Sequence[str]) -> ScratchParsedArgs:
    """Parse `cst btrfs scratch` arguments cleanly without touching I/O."""
    action, rest, head_err = parse_subcommand_head(
        argv, ScratchAction, default=ScratchAction.STATUS, help_action=ScratchAction.HELP
    )
    if head_err is not None:
        return ScratchParsedArgs(action=ScratchAction.HELP, error=head_err, code=EXIT_USAGE)
    if any(tok in HELP_WORDS for tok in rest):
        return ScratchParsedArgs(action=ScratchAction.HELP)

    state = _FlagState()
    i = 0
    while i < len(rest):
        next_tok = rest[i + 1] if i + 1 < len(rest) else None
        state, err, step = _step_scratch_flag(action, rest[i], next_tok, state)
        if err is not None:
            return ScratchParsedArgs(action=action, error=err, code=EXIT_USAGE)
        i += step

    if not state.include_cache and not state.include_scratch:
        return ScratchParsedArgs(action=action, error="cannot combine --no-cache and --no-scratch", code=EXIT_USAGE)
    if state.user is not None and not is_valid_username(state.user):
        return ScratchParsedArgs(action=action, error=f"invalid username {state.user!r}", code=EXIT_USAGE)
    return ScratchParsedArgs(
        action=action,
        user=state.user,
        include_cache=state.include_cache,
        include_scratch=state.include_scratch,
        allow_nonempty=state.allow_nonempty,
    )


def selected_kinds(*, include_cache: bool = True, include_scratch: bool = True) -> tuple[ScratchKind, ...]:
    """Return active `ScratchKind` tuple matching caller flags."""
    kinds: list[ScratchKind] = []
    if include_cache:
        kinds.append(ScratchKind.CACHE)
    if include_scratch:
        kinds.append(ScratchKind.SCRATCH)
    return tuple(kinds)


def build_user_bindings(
    user: ScratchUser, *, include_cache: bool = True, include_scratch: bool = True
) -> tuple[ScratchBinding, ...]:
    """Construct `/var/mnt/<kind>/<user> -> <home>/<rel>` bindings for `user`."""
    home = user.home.rstrip("/") or "/"
    out: list[ScratchBinding] = []
    for kind in selected_kinds(include_cache=include_cache, include_scratch=include_scratch):
        base = BASE_FOR_KIND[kind]
        rel = REL_FOR_KIND[kind]
        out.append(
            ScratchBinding(
                kind=kind,
                user=user.name,
                base=base,
                source=f"{base}/{user.name}",
                target=f"{home}/{rel}",
            )
        )
    return tuple(out)


def render_bind_fstab_line(binding: ScratchBinding) -> str:
    """Format one managed `/etc/fstab` bind-mount entry."""
    return (
        f"{binding.source} {binding.target} none "
        f"{SCRATCH_BIND_OPTS},x-systemd.requires-mounts-for={binding.base} 0 0 "
        f"{SCRATCH_MARKER} user={binding.user} kind={binding.kind.value}"
    )


def _parse_one_managed_line(raw: str) -> ManagedFstabEntry | None:
    stripped = raw.strip()
    if not stripped or stripped.startswith("#") or SCRATCH_MARKER not in raw:
        return None
    body, _, meta = raw.partition(SCRATCH_MARKER)
    fields = body.split()
    if len(fields) < _FSTAB_MIN_FIELDS:
        return None
    tags = dict(tok.split("=", 1) for tok in meta.split() if "=" in tok)
    kind = enum_member(ScratchKind, tags.get("kind", ""))
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


def parse_managed_fstab_entries(fstab_text: str) -> tuple[ManagedFstabEntry, ...]:
    """Return every `btrfs-scratch`-managed entry in `fstab_text`."""
    out: list[ManagedFstabEntry] = []
    for raw in fstab_text.splitlines():
        parsed = _parse_one_managed_line(raw)
        if parsed is not None:
            out.append(parsed)
    return tuple(out)


def plan_attach_fstab(fstab_text: str, bindings: Sequence[ScratchBinding]) -> str:
    """Upsert managed bind lines for `bindings`, preserving all unrelated and other-user lines."""
    wanted = {(b.user, b.kind): render_bind_fstab_line(b) for b in bindings}
    out_lines: list[str] = []
    for line in fstab_text.splitlines():
        managed = _parse_one_managed_line(line)
        if managed is not None and (managed.user, managed.kind) in wanted:
            out_lines.append(wanted.pop((managed.user, managed.kind)))
        else:
            out_lines.append(line)
    out_lines.extend(wanted.values())
    return "\n".join(out_lines) + "\n" if out_lines else ""


def plan_detach_fstab(fstab_text: str, user: str, kinds: Collection[ScratchKind]) -> str:
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
        return [f"{base_path} is not mounted (provision & mount the scratch btrfs subvolumes first)"]
    if base.fstype != _BTRFS:
        return [f"{base_path} filesystem is {base.fstype or 'unknown'}, expected {_BTRFS}"]
    if not base.uuid or not home_uuid:
        return [f"cannot verify filesystem UUID separation between {base_path} and target home directory"]
    if base.uuid == home_uuid:
        return [f"{base_path} shares filesystem UUID {base.uuid} with target home (refusing same-filesystem bind)"]
    return []


def _target_blockers(probe: BindingProbe, *, allow_nonempty: bool) -> list[str]:
    b = probe.binding
    if probe.target_is_symlink:
        return [f"{b.target} is a symlink (refusing to follow or overwrite symlink)"]
    if probe.mounted_from_source:
        return []
    if probe.mounted_other:
        return [f"{b.target} already has another mount active; unmount it first"]
    if probe.target_nonempty and not allow_nonempty:
        return [f"{b.target} is non-empty; move existing files to {b.source} first or pass --allow-nonempty"]
    return []


def validate_attach_snapshot(snap: ScratchSnapshot, *, allow_nonempty: bool = False) -> list[str]:
    """Return blocking error reasons preventing safe `attach`, or empty list when safe."""
    if snap.user is None:
        return [snap.user_error or "cannot resolve target user"]
    errors: list[str] = []
    checked_bases: set[ScratchKind] = set()
    for probe in snap.bindings:
        kind = probe.binding.kind
        if kind not in checked_bases:
            errors.extend(_base_blockers(snap.bases.get(kind), probe.binding.base, snap.home_uuid))
            checked_bases.add(kind)
        errors.extend(_target_blockers(probe, allow_nonempty=allow_nonempty))
    return errors


def _has_opt(options: str, needle: str) -> bool:
    return needle in (tok.strip() for tok in options.split(",") if tok.strip())


def parse_crypttab_scratch_issues(crypttab_text: str | None, backing_sources: Collection[str]) -> list[str]:
    """Return missing-`nofail` warnings for any scratch dm-crypt mapping in `/etc/crypttab`."""
    if crypttab_text is None or not crypttab_text:
        return []
    mappers = {src.removeprefix("/dev/mapper/") for src in backing_sources if src.startswith("/dev/mapper/")}
    issues: list[str] = []
    for raw in crypttab_text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < _CRYPTTAB_MIN_FIELDS:
            continue
        name = fields[0]
        is_scratch = name == ScratchKind.SCRATCH or name.startswith(("scratch-", "luks-scratch")) or name in mappers
        if not is_scratch:
            continue
        opts = fields[_CRYPTTAB_OPT_IDX] if len(fields) > _CRYPTTAB_OPT_IDX else ""
        if not _has_opt(opts, _NOFAIL):
            issues.append(f"/etc/crypttab entry {name!r} is missing '{_NOFAIL}' (boot could stall if NVMe is absent)")
    return issues


def _collect_active_bases(
    live_by_target: dict[str, MountEntry],
    fstab_by_target: dict[str, MountEntry],
    managed: Sequence[ManagedFstabEntry],
) -> tuple[str, ...]:
    active: list[str] = []
    for base in (CACHE_BASE, SCRATCH_BASE):
        referenced = any(e.base_match for e in _managed_for_base(managed, base))
        if base in live_by_target or base in fstab_by_target or referenced:
            active.append(base)
    return tuple(active)


@dataclass(frozen=True, slots=True)
class _ManagedRef:
    entry: ManagedFstabEntry
    base_match: bool


def _managed_for_base(managed: Sequence[ManagedFstabEntry], base: str) -> list[_ManagedRef]:
    return [_ManagedRef(entry=m, base_match=(BASE_FOR_KIND[m.kind] == base)) for m in managed]


def _check_base_mount(
    base: str,
    live: MountEntry | None,
    fst: MountEntry | None,
    findings: list[ScratchDoctorFinding],
) -> None:
    if live is None:
        findings.append(ScratchDoctorFinding(ScratchFindingSeverity.WARN, f"{base} is configured but not mounted"))
    elif live.fstype != _BTRFS:
        findings.append(
            ScratchDoctorFinding(ScratchFindingSeverity.WARN, f"{base} filesystem is {live.fstype}, expected btrfs")
        )
    has_flush = (live is not None and _has_opt(live.options, _FLUSHONCOMMIT)) or (
        fst is not None and _has_opt(fst.options, _FLUSHONCOMMIT)
    )
    if has_flush:
        findings.append(
            ScratchDoctorFinding(
                ScratchFindingSeverity.WARN,
                f"{base} has 'flushoncommit' (disable it on dedicated scratch/cache to reduce NVMe writes)",
            )
        )


def assess_scratch_health(
    fstab_text: str | None,
    proc_mounts_text: str | None,
    crypttab_text: str | None,
) -> list[ScratchDoctorFinding]:
    """Evaluate dedicated NVMe cache/scratch mounts and crypttab for `cst doctor`."""
    live_by_target = {m.target: m for m in parse_all_mounts(proc_mounts_text or "")}
    fstab_by_target = {m.target: m for m in parse_all_mounts(fstab_text or "")}
    managed = parse_managed_fstab_entries(fstab_text or "")
    active_bases = _collect_active_bases(live_by_target, fstab_by_target, managed)
    if not active_bases and not managed:
        return []

    findings: list[ScratchDoctorFinding] = []
    backing_sources: list[str] = []
    for base in active_bases:
        live, fst = live_by_target.get(base), fstab_by_target.get(base)
        backing_sources.extend(entry.source for entry in (live, fst) if entry is not None)
        _check_base_mount(base, live, fst, findings)

    findings.extend(
        ScratchDoctorFinding(ScratchFindingSeverity.WARN, issue)
        for issue in parse_crypttab_scratch_issues(crypttab_text, backing_sources)
    )

    findings.extend(
        ScratchDoctorFinding(
            ScratchFindingSeverity.WARN,
            f"{entry.target} (user={entry.user}) is in /etc/fstab but not mounted",
            fix=f"btrfs scratch attach --user {entry.user}",
        )
        for entry in managed
        if entry.target not in live_by_target
    )

    if not findings:
        bases_str = ", ".join(active_bases)
        findings.append(
            ScratchDoctorFinding(
                ScratchFindingSeverity.OK,
                f"{bases_str} mounted (btrfs, no flushoncommit); {len(managed)} user bind mount(s) active",
            )
        )
    return findings


def format_scratch_usage(prog: str) -> str:
    """Usage and help text for `cst btrfs scratch`."""
    return (
        "Multi-user dedicated NVMe cache/scratch bind mounts (~/.cache and ~/Scratch).\n"
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
        "  --no-scratch                         skip ~/Scratch binding (/var/mnt/scratch/<user>)\n"
        "  --allow-nonempty                     (plan/attach) allow bind-mounting over non-empty target directory\n"
        "\n"
        f"Usage: {prog} <status|plan|attach|detach|help> [options]\n"
    )


def _binding_state_label(probe: BindingProbe) -> str:
    if probe.mounted_from_source and probe.in_fstab:
        return "active (fstab + mounted)"
    if probe.mounted_from_source:
        return "mounted (not in fstab)"
    if probe.mounted_other:
        return "mounted (other source!)"
    if probe.in_fstab:
        return "in fstab (not mounted)"
    if probe.target_is_symlink:
        return "symlink (blocked)"
    return "unattached (target non-empty)" if probe.target_nonempty else "not attached"


def format_scratch_status(snap: ScratchSnapshot, *, seam_root: str = "") -> list[str]:
    """Render lines for `cst btrfs scratch status`."""
    lines: list[str] = []
    if seam_root:
        lines.append(f"test seam (CST_BTRFS_SCRATCH_ROOT): {seam_root}")
    lines.append("Dedicated cache/scratch mounts:")
    for kind in (ScratchKind.CACHE, ScratchKind.SCRATCH):
        base = snap.bases[kind]
        if base.mounted:
            flush_note = " [WARN: flushoncommit]" if _has_opt(base.options, _FLUSHONCOMMIT) else ""
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
    if b.kind == ScratchKind.SCRATCH:
        actions.append("ensure CACHEDIR.TAG")
    if not probe.in_fstab:
        actions.append("add /etc/fstab entry")
    if not probe.mounted_from_source:
        actions.append("mount --bind")
    summary = ", ".join(actions) if actions else "already attached"
    return f"  {b.kind.value:<10} {b.source} -> {b.target}: {summary}"


def format_scratch_plan(
    snap: ScratchSnapshot, prog: str, *, allow_nonempty: bool = False, seam_root: str = ""
) -> list[str]:
    """Render dry-run lines for `cst btrfs scratch plan`."""
    lines: list[str] = []
    if seam_root:
        lines.append(f"test seam (CST_BTRFS_SCRATCH_ROOT): {seam_root}")
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

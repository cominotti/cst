# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst btrfs commit-interval` across all root btrfs layers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from btrfs.compress.domain import (
    CompressSnapshot,
    OptionLayerOps,
    RootOptionLayerPlan,
)
from btrfs.compress.domain.root_option import (
    RootOptionAction,
    RootOptionPlanMixin,
    assess_root_option_coherence,
    booted_rootflags_diff,
    describe_unique_options,
    plan_compact_option_layers,
)
from cst_core.domain.text import HELP_WORDS, parse_subcommand_head, strip_mount_blanks
from cst_core.domain.values import parse_whole_number

KERNEL_DEFAULT_COMMIT_S: Final = 30
DEFAULT_TARGET_COMMIT_S: Final = 60
MIN_COMMIT_S: Final = 15
MAX_COMMIT_S: Final = 300
_COMMIT_PREFIX: Final = "commit="
# How the kernel reads a commit= value (btrfs's fsparam_u32: kstrtouint with base 0, as C reads a number).
_U32_MAX: Final = 2**32 - 1
_HEX_PREFIX: Final = "0x"
_HEX_DIGITS: Final = frozenset("0123456789abcdefABCDEF")
_OCTAL_DIGITS: Final = frozenset("01234567")
# The command's name in its own messages, when CST_PROG does not give it.
COMMIT_PROG: Final = "cst btrfs commit-interval"

CommitAction = RootOptionAction


@dataclass(frozen=True, slots=True)
class CommitParsedArgs:
    """Validated CLI arguments for `cst btrfs commit-interval`."""

    action: CommitAction
    seconds: int = DEFAULT_TARGET_COMMIT_S
    revert: bool = False
    yes: bool = False
    error: str | None = None
    code: int = 0


@dataclass(frozen=True, slots=True)
class CommitPlan(RootOptionPlanMixin):
    """Complete plan for aligning root btrfs `commit=` across all active layers."""

    target_seconds: int
    revert: bool
    is_ostree: bool
    root_uuid: str
    core: RootOptionLayerPlan


@dataclass(frozen=True, slots=True)
class CommitCoherenceReport:
    """Cross-layer agreement report for root btrfs commit interval."""

    coherent: bool
    seconds: int | None
    is_custom: bool
    summary: str
    drift_details: tuple[str, ...]


def _kernel_u32(text: str) -> int | None:
    """TEXT as the kernel's kstrtouint(TEXT, 0) reads it, or None where the kernel refuses it.

    An optional "+", then "0x" (or "0X") and hex digits, or "0" and octal digits ("060" is 48), or
    decimal digits; ASCII only, at most 2**32 - 1. (A last newline, which the kernel also takes, never
    reaches here: the tokens are stripped.)
    """
    body = text.removeprefix("+")
    hex_digits = body[len(_HEX_PREFIX) :]
    if body[: len(_HEX_PREFIX)].lower() == _HEX_PREFIX and hex_digits and set(hex_digits) <= _HEX_DIGITS:
        value = int(hex_digits, 16)
    elif body.startswith("0"):
        value = int(body, 8) if set(body) <= _OCTAL_DIGITS else None
    else:
        value = parse_whole_number(body)
    return value if value is not None and value <= _U32_MAX else None


def parse_commit_token(token: str) -> int | None:
    """Parse one `commit=N` mount option token: N seconds as the kernel reads it (`_kernel_u32`), or None
    for a token that is no `commit=`, one the kernel refuses, and `commit=0` (the kernel's 30 s default)."""
    if not token.startswith(_COMMIT_PREFIX):
        return None
    return _kernel_u32(token[len(_COMMIT_PREFIX) :]) or None


def extract_commit_seconds(options_str: str) -> tuple[int, ...]:
    """Extract every explicit `commit=N` integer value from an option string."""
    out: list[int] = []
    for tok in options_str.split(","):
        parsed = parse_commit_token(strip_mount_blanks(tok))
        if parsed is not None:
            out.append(parsed)
    return tuple(out)


def has_explicit_commit(options_str: str) -> bool:
    """Return True when `options_str` explicitly sets `commit=N`."""
    return bool(extract_commit_seconds(options_str))


def has_malformed_commit(options_str: str) -> bool:
    """True when `options_str` holds a `commit=` token that is not a positive number of seconds as the
    kernel reads it: one a mount refuses (`commit=abc`, `commit=`, `commit=08`, digits other than ASCII
    0-9, more than 2**32 - 1) or `commit=0`, which the kernel takes as its 30 s default.
    rewrite_commit_options replaces it."""
    return any(
        tok.startswith(_COMMIT_PREFIX) and parse_commit_token(tok) is None
        for tok in (strip_mount_blanks(t) for t in options_str.split(","))
    )


def effective_commit_seconds(options_str: str) -> int:
    """Return effective btrfs commit interval in seconds (`30` when kernel omits `commit=30`)."""
    vals = extract_commit_seconds(options_str)
    return vals[-1] if vals else KERNEL_DEFAULT_COMMIT_S


def format_commit_label(seconds: int | None, *, explicit: bool = False) -> str:
    """Render concise display label for a layer's commit setting."""
    if seconds is None:
        return "(none)"
    if seconds == KERNEL_DEFAULT_COMMIT_S and not explicit:
        return f"commit={KERNEL_DEFAULT_COMMIT_S} (default)"
    return f"commit={seconds}"


def rewrite_commit_options(options_str: str, target_seconds: int | None) -> str:
    """Rewrite or remove `commit=N` in `options_str`, leaving other mount options untouched.

    Every `commit=` token goes, also one that is not a number (`commit=abc`, which a mount refuses) or
    `commit=0` (btrfs' default); the target takes the place of the first one."""
    tokens = [tok for tok in options_str.split(",") if tok]
    omit = target_seconds is None or target_seconds == KERNEL_DEFAULT_COMMIT_S
    target_tok = "" if omit else f"commit={target_seconds}"
    out: list[str] = []
    inserted = False
    for tok in tokens:
        if tok.startswith(_COMMIT_PREFIX):
            if not omit and not inserted:
                out.append(target_tok)
                inserted = True
        else:
            out.append(tok)
    if not omit and not inserted:
        out.append(target_tok)
    return ",".join(out)


def _parse_seconds_value(raw: str) -> int | str:
    val = parse_whole_number(raw)
    if val is None:
        return f"invalid commit interval {raw!r} (expected integer {MIN_COMMIT_S}..{MAX_COMMIT_S})"
    if val < MIN_COMMIT_S or val > MAX_COMMIT_S:
        return f"commit interval {val}s out of range ({MIN_COMMIT_S}..{MAX_COMMIT_S})"
    return val


def _parse_non_writing_args(action: CommitAction, rest: Sequence[str]) -> CommitParsedArgs:
    if action == CommitAction.STATUS and any(tok in HELP_WORDS for tok in rest):
        return CommitParsedArgs(action=CommitAction.HELP)
    if rest:
        return CommitParsedArgs(action=action, error=f"unexpected argument: {rest[0]}", code=2)
    return CommitParsedArgs(action=action)


def _flag_rejection(tok: str) -> str:
    if tok == "--yes":
        return "--yes is only valid with apply or revert"
    if tok == "--revert":
        return "--revert is only valid with plan"
    return f"unknown or unexpected argument: {tok}"


def _resolved_seconds(seconds: int | None, *, revert: bool) -> int:
    """The commit interval a run sets: the kernel's default for a revert, else the one given or ours."""
    if revert:
        return KERNEL_DEFAULT_COMMIT_S
    if seconds is None:
        return DEFAULT_TARGET_COMMIT_S
    return seconds


def _scan_commit_flags(action: CommitAction, rest: Sequence[str]) -> tuple[int | None, bool, bool] | CommitParsedArgs:
    """The arguments after the action: (seconds, revert, yes), or the usage error of the first bad one."""
    seconds: int | None = None
    revert = action == CommitAction.REVERT
    yes = False
    for tok in rest:
        if tok == "--yes" and action in (CommitAction.APPLY, CommitAction.REVERT):
            yes = True
        elif tok == "--revert" and action == CommitAction.PLAN:
            revert = True
        elif not tok.startswith("-") and action in (CommitAction.PLAN, CommitAction.APPLY) and seconds is None:
            parsed = _parse_seconds_value(tok)
            if isinstance(parsed, str):
                return CommitParsedArgs(action=action, error=parsed, code=2)
            seconds = parsed
        else:
            return CommitParsedArgs(action=action, error=_flag_rejection(tok), code=2)
    return seconds, revert, yes


def _parse_commit_flags(action: CommitAction, rest: Sequence[str]) -> CommitParsedArgs:
    if any(tok in HELP_WORDS for tok in rest):
        return CommitParsedArgs(action=CommitAction.HELP)
    scanned = _scan_commit_flags(action, rest)
    if isinstance(scanned, CommitParsedArgs):
        return scanned
    seconds, revert, yes = scanned
    if revert and seconds is not None:
        return CommitParsedArgs(action=action, error="cannot combine seconds with --revert", code=2)
    return CommitParsedArgs(action=action, seconds=_resolved_seconds(seconds, revert=revert), revert=revert, yes=yes)


def parse_commit_args(argv: Sequence[str]) -> CommitParsedArgs:
    """Parse `cst btrfs commit-interval` CLI arguments without touching I/O."""
    action, rest, head_err = parse_subcommand_head(
        argv, CommitAction, default=CommitAction.STATUS, help_action=CommitAction.HELP
    )
    if head_err is not None:
        return CommitParsedArgs(action=CommitAction.HELP, error=head_err, code=2)
    if action in (CommitAction.STATUS, CommitAction.HELP):
        return _parse_non_writing_args(action, rest)
    return _parse_commit_flags(action, rest)


def _describe_commit_opts(opt_strings: Sequence[str]) -> str:
    return describe_unique_options(
        opt_strings,
        lambda o: format_commit_label(effective_commit_seconds(o), explicit=has_explicit_commit(o)),
    )


def build_commit_plan(
    snap: CompressSnapshot,
    *,
    target_seconds: int = DEFAULT_TARGET_COMMIT_S,
    revert: bool = False,
) -> CommitPlan:
    """Build deterministic multi-layer plan for root btrfs `commit=N` interval."""
    eff_target = KERNEL_DEFAULT_COMMIT_S if revert else target_seconds
    opt_target = None if eff_target == KERNEL_DEFAULT_COMMIT_S else eff_target
    target_label = format_commit_label(eff_target, explicit=(opt_target is not None))
    core = plan_compact_option_layers(
        snap,
        OptionLayerOps(
            target_label=target_label,
            rewrite_opts=lambda opts: rewrite_commit_options(opts, opt_target),
            describe_options=_describe_commit_opts,
            describe_top=lambda opts: format_commit_label(
                effective_commit_seconds(opts), explicit=has_explicit_commit(opts)
            ),
            needs_live_change=lambda ms: any(effective_commit_seconds(m.options) != eff_target for m in ms),
            needs_top_change=lambda top: (
                effective_commit_seconds(top.options) != eff_target
                or has_explicit_commit(top.options) != (opt_target is not None)
                or has_malformed_commit(top.options)
            ),
            set_by_apply=has_explicit_commit,
        ),
    )
    return CommitPlan(
        target_seconds=eff_target,
        revert=revert,
        is_ostree=snap.is_ostree,
        root_uuid=snap.root_uuid,
        core=core,
    )


def assess_commit_coherence(snap: CompressSnapshot) -> CommitCoherenceReport:
    """Assess whether all active root btrfs layers agree on `commit=` seconds."""
    coherent, sec, distinct, summary, details = assess_root_option_coherence(
        snap,
        extract_layer=lambda opts: (
            (s := effective_commit_seconds(opts)),
            format_commit_label(s, explicit=has_explicit_commit(opts)),
        ),
        format_coherent=lambda s: format_commit_label(s, explicit=s != KERNEL_DEFAULT_COMMIT_S),
    )
    if not coherent:
        return CommitCoherenceReport(
            coherent=False,
            seconds=None,
            is_custom=any(s != KERNEL_DEFAULT_COMMIT_S for s in distinct),
            summary=summary,
            drift_details=details,
        )
    return CommitCoherenceReport(
        coherent=True,
        seconds=sec,
        is_custom=sec is not None and sec != KERNEL_DEFAULT_COMMIT_S,
        summary=summary,
        drift_details=(),
    )


def booted_commit_diff(snap: CompressSnapshot, proc_cmdline: str | None, *, kargs_ok: bool) -> tuple[str, str] | None:
    """Return `(booted_rootflags, next_rootflags)` when their commit interval differs."""
    return booted_rootflags_diff(snap, proc_cmdline, kargs_ok=kargs_ok, parse_value=effective_commit_seconds)


def format_commit_usage(prog: str) -> str:
    """Usage and help text for `cst btrfs commit-interval`."""
    return (
        "Root btrfs `commit=N` interval across /etc/fstab, rpm-ostree kargs, live mount, and btrfs:top-mount.\n"
        "\n"
        "Commands:\n"
        "  status                    read-only: show commit interval across live, fstab, kargs, and top-mount\n"
        "                            (works as your user and inside a toolbox; default command)\n"
        "  plan [SECONDS|--revert]   dry run of changes `apply` (or `revert`) would make; changes nothing\n"
        f"                            (default {DEFAULT_TARGET_COMMIT_S}s; allowed {MIN_COMMIT_S}..{MAX_COMMIT_S}s; works in toolbox)\n"
        "  apply [SECONDS] [--yes]   (root, host only) align active layers to commit=SECONDS "
        f"(default {DEFAULT_TARGET_COMMIT_S}s)\n"
        "  revert [--yes]            (root, host only) revert active layers to kernel default (30s)\n"
        "  help                      show this help\n"
        "\n"
        "Leaves other root RAID1 mount options untouched.\n"
        f"\nUsage: {prog} <status|plan|apply|revert|help> [options]\n"
    )

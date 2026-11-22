# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst btrfs commit-interval` across all root btrfs layers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from btrfs.compress.domain import (
    CompressSnapshot,
    LayerPlan,
    OptionLayerOps,
    ROOTFLAGS_PREFIX,
    RootOptionLayerPlan,
    collect_layer_options,
    extract_rootflags_tokens,
    plan_root_option_layers,
)
from cst_core.domain.text import HELP_WORDS, parse_subcommand_head

KERNEL_DEFAULT_COMMIT_S: Final = 30
DEFAULT_TARGET_COMMIT_S: Final = 60
MIN_COMMIT_S: Final = 15
MAX_COMMIT_S: Final = 300
_COMMIT_PREFIX: Final = "commit="


class CommitAction(StrEnum):
    """Supported subcommands of `cst btrfs commit-interval`."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"
    HELP = "help"


def is_commit_write(action: CommitAction) -> bool:
    """True for mutating `commit-interval` subcommands."""
    return action in (CommitAction.APPLY, CommitAction.REVERT)


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
class CommitPlan:
    """Complete plan for aligning root btrfs `commit=` across all active layers."""

    target_seconds: int
    revert: bool
    is_ostree: bool
    root_uuid: str
    core: RootOptionLayerPlan

    @property
    def layers(self) -> tuple[LayerPlan, ...]:
        """Per-layer plan entries from `core`."""
        return self.core.layers

    @property
    def has_changes(self) -> bool:
        """True when any layer needs updating."""
        return any(lp.changed for lp in self.core.layers)

    @property
    def new_fstab_text(self) -> str | None:
        """Updated `/etc/fstab` text when fstab changes."""
        return self.core.new_fstab_text

    @property
    def kargs_edits(self) -> tuple[tuple[str, str], ...]:
        """`rpm-ostree kargs` delete/append edit pairs."""
        return self.core.kargs_edits

    @property
    def remount_target(self) -> str | None:
        """Live root btrfs mount target to remount when needed."""
        return self.core.remount_target

    @property
    def new_top_mount_options(self) -> str | None:
        """Updated `Options=` string for the top-mount systemd unit."""
        return self.core.new_top_mount_options


@dataclass(frozen=True, slots=True)
class CommitCoherenceReport:
    """Cross-layer agreement report for root btrfs commit interval."""

    coherent: bool
    seconds: int | None
    is_custom: bool
    summary: str
    drift_details: tuple[str, ...]


def parse_commit_token(token: str) -> int | None:
    """Parse one `commit=N` mount option token (positive integer seconds), or None."""
    if not token.startswith(_COMMIT_PREFIX):
        return None
    raw = token[len(_COMMIT_PREFIX) :]
    if not raw.isdigit():
        return None
    val = int(raw)
    return val if val > 0 else None


def extract_commit_seconds(options_str: str) -> tuple[int, ...]:
    """Extract every explicit `commit=N` integer value from an option string."""
    out: list[int] = []
    for tok in options_str.split(","):
        parsed = parse_commit_token(tok.strip())
        if parsed is not None:
            out.append(parsed)
    return tuple(out)


def has_explicit_commit(options_str: str) -> bool:
    """Return True when `options_str` explicitly sets `commit=N`."""
    return bool(extract_commit_seconds(options_str))


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
    """Rewrite or remove `commit=N` in `options_str`, keeping `flushoncommit` and other flags."""
    tokens = [tok for tok in options_str.split(",") if tok]
    omit = target_seconds is None or target_seconds == KERNEL_DEFAULT_COMMIT_S
    target_tok = "" if omit else f"commit={target_seconds}"
    out: list[str] = []
    inserted = False
    for tok in tokens:
        if parse_commit_token(tok) is not None:
            if not omit and not inserted:
                out.append(target_tok)
                inserted = True
        else:
            out.append(tok)
    if not omit and not inserted:
        out.append(target_tok)
    return ",".join(out)


def _parse_seconds_value(raw: str) -> int | str:
    if not raw.isdigit():
        return f"invalid commit interval {raw!r} (expected integer {MIN_COMMIT_S}..{MAX_COMMIT_S})"
    val = int(raw)
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


def _parse_commit_flags(action: CommitAction, rest: Sequence[str]) -> CommitParsedArgs:
    if any(tok in HELP_WORDS for tok in rest):
        return CommitParsedArgs(action=CommitAction.HELP)
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
    if revert and seconds is not None:
        return CommitParsedArgs(action=action, error="cannot combine seconds with --revert", code=2)
    resolved = KERNEL_DEFAULT_COMMIT_S if revert else (DEFAULT_TARGET_COMMIT_S if seconds is None else seconds)
    return CommitParsedArgs(action=action, seconds=resolved, revert=revert, yes=yes)


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
    unique = list(
        dict.fromkeys(
            format_commit_label(effective_commit_seconds(o), explicit=has_explicit_commit(o)) for o in opt_strings
        )
    )
    return ", ".join(unique) if unique else "(none)"


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
    core = plan_root_option_layers(
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
            ),
            fstab_absent_current="(no root btrfs)",
            kargs_absent_current="(no rootflags=)",
            kargs_absent_detail="no rootflags= in rpm-ostree kargs",
            kargs_skip_detail="non-ostree host",
            live_absent_detail="no root btrfs mount in /proc/mounts",
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
    observed: list[tuple[str, int, bool]] = [
        (name, effective_commit_seconds(opts), has_explicit_commit(opts)) for name, opts in collect_layer_options(snap)
    ]
    if not observed:
        return CommitCoherenceReport(
            coherent=True,
            seconds=None,
            is_custom=False,
            summary="no root btrfs mount sources found",
            drift_details=(),
        )
    distinct = {sec for _, sec, _ in observed}
    if len(distinct) > 1:
        details = tuple(f"{name}={format_commit_label(sec, explicit=exp)}" for name, sec, exp in observed)
        return CommitCoherenceReport(
            coherent=False,
            seconds=None,
            is_custom=any(sec != KERNEL_DEFAULT_COMMIT_S for sec in distinct),
            summary="drift across layers: " + ", ".join(details),
            drift_details=details,
        )
    sec = observed[0][1]
    is_custom = sec != KERNEL_DEFAULT_COMMIT_S
    summary = f"coherent ({format_commit_label(sec, explicit=is_custom)})"
    return CommitCoherenceReport(coherent=True, seconds=sec, is_custom=is_custom, summary=summary, drift_details=())


def booted_commit_diff(snap: CompressSnapshot, proc_cmdline: str | None, *, kargs_ok: bool) -> tuple[str, str] | None:
    """Return `(booted_rootflags, next_rootflags)` when their commit interval differs."""
    if not (snap.is_ostree and kargs_ok and proc_cmdline is not None and snap.kargs_text is not None):
        return None
    boot_rf = extract_rootflags_tokens(proc_cmdline)
    next_rf = extract_rootflags_tokens(snap.kargs_text)
    if not (boot_rf and next_rf):
        return None
    b_sec = effective_commit_seconds(boot_rf[0].removeprefix(ROOTFLAGS_PREFIX))
    n_sec = effective_commit_seconds(next_rf[0].removeprefix(ROOTFLAGS_PREFIX))
    if b_sec == n_sec:
        return None
    return (boot_rf[0], next_rf[0])


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
        "Keeps `flushoncommit` untouched on the root RAID1 filesystem.\n"
        f"\nUsage: {prog} <status|plan|apply|revert|help> [options]\n"
    )

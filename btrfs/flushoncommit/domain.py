# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst btrfs flushoncommit` across all root btrfs layers."""

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
    OPT_FLUSHONCOMMIT,
    RootOptionAction,
    RootOptionPlanMixin,
    assess_root_option_coherence,
    booted_rootflags_diff,
    describe_unique_options,
    plan_compact_option_layers,
)
from cst_core.domain.text import HELP_WORDS, parse_subcommand_head, strip_mount_blanks

OPT_NOFLUSHONCOMMIT: Final = "noflushoncommit"
FLUSH_PROG: Final = "cst btrfs flushoncommit"

FlushAction = RootOptionAction


@dataclass(frozen=True, slots=True)
class FlushParsedArgs:
    """Validated CLI arguments for `cst btrfs flushoncommit`."""

    action: FlushAction
    revert: bool = False
    yes: bool = False
    error: str | None = None
    code: int = 0


@dataclass(frozen=True, slots=True)
class FlushPlan(RootOptionPlanMixin):
    """Complete plan for aligning root btrfs `flushoncommit` across all active layers."""

    enable: bool
    revert: bool
    core: RootOptionLayerPlan


def effective_flushoncommit(options_str: str) -> bool:
    """Return effective `flushoncommit` state from mount options (`False` by kernel default)."""
    state = False
    for raw_tok in options_str.split(","):
        tok = strip_mount_blanks(raw_tok)
        if tok == OPT_FLUSHONCOMMIT:
            state = True
        elif tok == OPT_NOFLUSHONCOMMIT:
            state = False
    return state


def has_flushoncommit_token(options_str: str) -> bool:
    """Return True when `options_str` explicitly carries `flushoncommit` or `noflushoncommit`."""
    return any(strip_mount_blanks(tok) in (OPT_FLUSHONCOMMIT, OPT_NOFLUSHONCOMMIT) for tok in options_str.split(","))


def format_flushoncommit_label(*, enabled: bool | None, explicit: bool = False) -> str:
    """Render concise display label for a layer's `flushoncommit` setting."""
    if enabled is None:
        return "(none)"
    if enabled:
        return OPT_FLUSHONCOMMIT
    return OPT_NOFLUSHONCOMMIT if explicit else f"no {OPT_FLUSHONCOMMIT} (default)"


def rewrite_flushoncommit_options(options_str: str, *, enable: bool) -> str:
    """Rewrite `flushoncommit`/`noflushoncommit` tokens; `enable=False` omits them cleanly."""
    tokens = [tok for tok in options_str.split(",") if tok]
    out: list[str] = []
    inserted = False
    for raw_tok in tokens:
        if strip_mount_blanks(raw_tok) in (OPT_FLUSHONCOMMIT, OPT_NOFLUSHONCOMMIT):
            if enable and not inserted:
                out.append(OPT_FLUSHONCOMMIT)
                inserted = True
        else:
            out.append(raw_tok)
    if enable and not inserted:
        out.append(OPT_FLUSHONCOMMIT)
    if not out and tokens:
        return "defaults"
    return ",".join(out)


def parse_flush_args(argv: Sequence[str]) -> FlushParsedArgs:
    """Parse `cst btrfs flushoncommit` CLI arguments without touching I/O."""
    action, rest, head_err = parse_subcommand_head(
        argv, FlushAction, default=FlushAction.STATUS, help_action=FlushAction.HELP
    )
    if head_err is not None:
        return FlushParsedArgs(action=FlushAction.HELP, error=head_err, code=2)
    if action == FlushAction.HELP or any(tok in HELP_WORDS for tok in rest):
        return FlushParsedArgs(action=FlushAction.HELP)
    if action == FlushAction.STATUS:
        if rest:
            return FlushParsedArgs(action=action, error=f"unexpected argument: {rest[0]}", code=2)
        return FlushParsedArgs(action=action)
    revert = action == FlushAction.REVERT
    yes = False
    for tok in rest:
        if tok == "--yes" and action in (FlushAction.APPLY, FlushAction.REVERT):
            yes = True
        elif tok == "--revert" and action == FlushAction.PLAN:
            revert = True
        else:
            return FlushParsedArgs(action=action, error=f"unknown or unexpected argument: {tok}", code=2)
    return FlushParsedArgs(action=action, revert=revert, yes=yes)


def _describe_flush_opts(opt_strings: Sequence[str]) -> str:
    return describe_unique_options(
        opt_strings,
        lambda o: format_flushoncommit_label(enabled=effective_flushoncommit(o), explicit=has_flushoncommit_token(o)),
    )


def build_flush_plan(snap: CompressSnapshot, *, revert: bool = False) -> FlushPlan:
    """Build deterministic multi-layer plan (`apply` disables `flushoncommit`; `revert` re-enables)."""
    enable = revert
    target_label = format_flushoncommit_label(enabled=enable, explicit=enable)
    core = plan_compact_option_layers(
        snap,
        OptionLayerOps(
            target_label=target_label,
            rewrite_opts=lambda opts: rewrite_flushoncommit_options(opts, enable=enable),
            describe_options=_describe_flush_opts,
            describe_top=lambda opts: format_flushoncommit_label(
                enabled=effective_flushoncommit(opts), explicit=has_flushoncommit_token(opts)
            ),
            needs_live_change=lambda ms: any(effective_flushoncommit(m.options) != enable for m in ms),
            needs_top_change=lambda top: rewrite_flushoncommit_options(top.options, enable=enable) != top.options,
            set_by_apply=effective_flushoncommit,
        ),
    )
    return FlushPlan(enable=enable, revert=revert, core=core)


@dataclass(frozen=True, slots=True)
class FlushCoherenceReport:
    """Cross-layer agreement report for root btrfs `flushoncommit`."""

    coherent: bool
    enabled: bool | None
    summary: str
    drift_details: tuple[str, ...]


def assess_flush_coherence(snap: CompressSnapshot) -> FlushCoherenceReport:
    """Assess whether all active root btrfs layers agree on `flushoncommit`."""
    coherent, enabled, _distinct, summary, details = assess_root_option_coherence(
        snap,
        extract_layer=lambda opts: (
            (en := effective_flushoncommit(opts)),
            format_flushoncommit_label(enabled=en, explicit=has_flushoncommit_token(opts)),
        ),
        format_coherent=lambda en: format_flushoncommit_label(enabled=en, explicit=en),
    )
    return FlushCoherenceReport(coherent=coherent, enabled=enabled, summary=summary, drift_details=details)


def booted_flush_diff(snap: CompressSnapshot, proc_cmdline: str | None, *, kargs_ok: bool) -> tuple[str, str] | None:
    """Return `(booted_rootflags, next_rootflags)` when their `flushoncommit` state differs."""
    return booted_rootflags_diff(snap, proc_cmdline, kargs_ok=kargs_ok, parse_value=effective_flushoncommit)


def format_flush_usage(prog: str) -> str:
    """Usage and help text for `cst btrfs flushoncommit`."""
    return (
        "Root btrfs `flushoncommit` mount option across /etc/fstab, rpm-ostree kargs, live mount, and btrfs:top-mount.\n"
        "\n"
        "Commands:\n"
        "  status            read-only: show flushoncommit state across live, fstab, kargs, and top-mount\n"
        "                    (works as your user and inside a toolbox; default command)\n"
        "  plan [--revert]   dry run of changes `apply` (remove) or `revert` (enable) would make\n"
        "  apply [--yes]     (root, host only) remove flushoncommit (CST default: reduce commit latency/SSD wear)\n"
        "  revert [--yes]    (root, host only) re-enable flushoncommit across active layers\n"
        "  help              show this help\n"
        "\n"
        f"Usage: {prog} <status|plan|apply|revert|help> [options]\n"
    )

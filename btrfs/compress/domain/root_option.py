# SPDX-License-Identifier: MPL-2.0
"""Shared root btrfs option helpers for `commit-interval` and `flushoncommit`."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from enum import StrEnum
from typing import Final

from btrfs.compress.domain import (
    ROOTFLAGS_PREFIX,
    CompressSnapshot,
    LayerPlan,
    OptionLayerOps,
    RootOptionLayerPlan,
    collect_layer_options,
    extract_rootflags_tokens,
    plan_root_option_layers,
)

OPT_FLUSHONCOMMIT: Final = "flushoncommit"


class RootOptionAction(StrEnum):
    """Supported subcommands of root btrfs option tools (`commit-interval`, `flushoncommit`)."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"
    HELP = "help"


class RootOptionPlanMixin:
    """Shared read-only properties delegating to `self.core` (`RootOptionLayerPlan`)."""

    __slots__ = ()
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

    @property
    def left_alone_notes(self) -> tuple[str, ...]:
        """Notes for any separate `/var` btrfs filesystem entry from `core`."""
        return self.core.left_alone_notes


def describe_unique_options(opt_strings: Sequence[str], format_one: Callable[[str], str]) -> str:
    """Deduplicate formatted layer option labels preserving encounter order."""
    unique = list(dict.fromkeys(format_one(o) for o in opt_strings))
    return ", ".join(unique) if unique else "(none)"


def plan_compact_option_layers(snap: CompressSnapshot, ops: OptionLayerOps) -> RootOptionLayerPlan:
    """Run `plan_root_option_layers` using compact absent/skip wording for root option tools."""
    compact_ops = replace(
        ops,
        fstab_absent_current="(no root btrfs)",
        kargs_absent_current="(no rootflags=)",
        kargs_absent_detail="no rootflags= in rpm-ostree kargs",
        kargs_skip_detail="non-ostree host",
        live_absent_detail="no root btrfs mount in /proc/mounts",
    )
    return plan_root_option_layers(snap, compact_ops)


def booted_rootflags_diff[ValT](
    snap: CompressSnapshot,
    proc_cmdline: str | None,
    *,
    kargs_ok: bool,
    parse_value: Callable[[str], ValT],
) -> tuple[str, str] | None:
    """Return `(booted_rootflags, next_rootflags)` when parsed `parse_value` differs between boots."""
    if not (snap.is_ostree and kargs_ok and proc_cmdline is not None and snap.kargs_text is not None):
        return None
    boot_rf = extract_rootflags_tokens(proc_cmdline)
    next_rf = extract_rootflags_tokens(snap.kargs_text)
    if not (boot_rf and next_rf):
        return None
    # The kernel uses the last rootflags= of its command line.
    boot_val = parse_value(boot_rf[-1].removeprefix(ROOTFLAGS_PREFIX))
    next_val = parse_value(next_rf[-1].removeprefix(ROOTFLAGS_PREFIX))
    return None if boot_val == next_val else (boot_rf[-1], next_rf[-1])


def assess_root_option_coherence[ValT](
    snap: CompressSnapshot,
    *,
    extract_layer: Callable[[str], tuple[ValT, str]],
    format_coherent: Callable[[ValT], str],
) -> tuple[bool, ValT | None, tuple[ValT, ...], str, tuple[str, ...]]:
    """Shared cross-layer coherence calculation for single-value root btrfs option tools."""
    observed = [(name, *extract_layer(opts)) for name, opts in collect_layer_options(snap)]
    if not observed:
        return True, None, (), "no root btrfs mount sources found", ()
    distinct = tuple(dict.fromkeys(val for _, val, _ in observed))
    if len(distinct) > 1:
        details = tuple(f"{name}={label}" for name, _, label in observed)
        return False, None, distinct, "drift across layers: " + ", ".join(details), details
    val = observed[0][1]
    return True, val, distinct, f"coherent ({format_coherent(val)})", ()

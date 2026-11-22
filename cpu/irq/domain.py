# SPDX-License-Identifier: MPL-2.0
"""Pure domain logic for `cst cpu irq` (`cpu/cpu-irq`): argument parsing, ban computation, and formatting."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from cst_core.domain.text import parse_subcommand_head
from cst_core.domain.units import ActiveState
from cst_core.domain.values import CpuSet

IRQ_ENV_PATH: Final = "/etc/cpu-setup/irqbalance.env"
IRQ_SYSCONFIG_PATH: Final = "/etc/sysconfig/irqbalance"
IRQ_DROPIN_PATH: Final = "/etc/systemd/system/irqbalance.service.d/10-cst-ban.conf"
IRQBALANCE_SERVICE: Final = "irqbalance.service"
MIN_UNBANNED_CPUS: Final = 2
_NO_PKG_FLAG: Final = "--no-pkg"
_BANNED_ENV_KEY: Final = "IRQBALANCE_BANNED_CPULIST"


class IrqAction(StrEnum):
    """Supported subcommands of `cst cpu irq` / `cpu-irq`."""

    STATUS = "status"
    PLAN = "plan"
    APPLY = "apply"
    REVERT = "revert"
    HELP = "help"


@dataclass(frozen=True, slots=True)
class IrqParsedArgs:
    """Validated CLI arguments for `cpu-irq`."""

    action: IrqAction
    no_pkg: bool = False
    error: str | None = None
    code: int = 0


@dataclass(frozen=True, slots=True)
class IrqSnapshot:
    """Observed settings, online CPU topology, irqbalance config files, and systemd state."""

    conf_path: str
    build_cpus_raw: str
    online_cpus_raw: str | None
    banned_cpulist: str
    compute_notes: tuple[str, ...]
    env_path: str
    env_text: str | None
    env_banned: str | None
    sysconfig_path: str
    sysconfig_banned: str | None
    dropin_path: str
    dropin_text: str | None
    service_unit_present: bool
    service_active: str
    service_enabled: str
    rpm_ostree_present: bool
    warnings: tuple[str, ...] = ()


def format_cpulist(cpus: Iterable[int]) -> str:
    """Format CPU indices as a compact Linux cpulist range string (e.g. `0-3,8-11`)."""
    ordered = sorted(set(cpus))
    if not ordered:
        return ""
    ranges: list[str] = []
    start = prev = ordered[0]
    for cpu in ordered[1:]:
        if cpu == prev + 1:
            prev = cpu
            continue
        ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = cpu
    ranges.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def compute_banned_cpulist(
    build_cpus: CpuSet,
    online_cpus: CpuSet,
    *,
    min_unbanned: int = MIN_UNBANNED_CPUS,
) -> tuple[str, tuple[str, ...]]:
    """Intersect `BUILD_CPUS` with online CPUs while keeping at least `min_unbanned` CPUs unbanned."""
    wanted = sorted(build_cpus.cpus & online_cpus.cpus)
    notes: list[str] = []
    if not wanted:
        return "", ("no online CPUs overlap BUILD_CPUS; leaving all online CPUs unbanned",)
    total_online = len(online_cpus.cpus)
    if total_online <= min_unbanned:
        return "", (f"only {total_online} online CPUs; leaving all unbanned (need >{min_unbanned} CPUs to ban any)",)
    max_banned = total_online - min_unbanned
    kept = wanted
    if len(wanted) > max_banned:
        drop_count = len(wanted) - max_banned
        kept = wanted[drop_count:]
        notes.append(
            f"trimmed {format_cpulist(wanted[:drop_count])} from ban-list to keep >= {min_unbanned} unbanned IRQ CPUs"
        )
    return format_cpulist(kept), tuple(notes)


def render_irq_env(banned_cpulist: str) -> str:
    """Render `/etc/cpu-setup/irqbalance.env` overriding `IRQBALANCE_BANNED_CPULIST`."""
    return (
        "# SPDX-License-Identifier: MPL-2.0\n"
        "# Managed by cst cpu irq (CST). Loaded via irqbalance.service.d/10-cst-ban.conf.\n"
        f"{_BANNED_ENV_KEY}={banned_cpulist}\n"
    )


def render_irq_dropin() -> str:
    """Render `/etc/systemd/system/irqbalance.service.d/10-cst-ban.conf`."""
    return (
        "# SPDX-License-Identifier: MPL-2.0\n"
        "# Managed by cst cpu irq (CST). Applies CST's adaptive IRQ ban-list after /etc/sysconfig/irqbalance.\n"
        "[Service]\n"
        f"EnvironmentFile=-{IRQ_ENV_PATH}\n"
    )


def format_irq_usage(prog: str) -> str:
    """Return CLI help text for `cpu-irq` / `cst cpu irq`."""
    return (
        f"{prog}: manage adaptive irqbalance CPU ban-list ({_BANNED_ENV_KEY}) from BUILD_CPUS and online CPUs.\n\n"
        "Commands:\n"
        "  status            read-only (user/toolbox): show BUILD_CPUS overlap, env/drop-in state, and service\n"
        "  plan [--no-pkg]   dry run (user/toolbox): show what `apply` would install, write, or restart\n"
        "  apply [--no-pkg]  (root on host) write irqbalance.env + drop-in, install/enable irqbalance if needed\n"
        "  revert            (root on host) remove CST's irqbalance env and drop-in; leave RPM installed\n"
        "  help              show this help\n\n"
        f"Usage: {prog} <status|plan|apply|revert|help> [--no-pkg]\n"
    )


def parse_irq_args(argv: Sequence[str]) -> IrqParsedArgs:
    """Parse CLI arguments after `cpu-irq` / `cst cpu irq`."""
    action, rest, head_err = parse_subcommand_head(
        argv, IrqAction, default=IrqAction.STATUS, help_action=IrqAction.HELP
    )
    if head_err is not None:
        return IrqParsedArgs(action, error=head_err, code=2)
    no_pkg = False
    bad: str | None = None
    for tok in rest:
        if tok == _NO_PKG_FLAG and action in (IrqAction.PLAN, IrqAction.APPLY) and not no_pkg:
            no_pkg = True
        else:
            bad = f"{action.value}: unexpected argument {tok!r}"
            break
    if bad is not None:
        return IrqParsedArgs(action, error=bad, code=2)
    return IrqParsedArgs(action, no_pkg=no_pkg)


def format_irq_status(snap: IrqSnapshot) -> list[str]:
    """Render concise `cst cpu irq status` lines."""
    target_txt = snap.banned_cpulist or "none (no ban)"
    lines = [f"WARN: {w}" for w in snap.warnings]
    lines.extend(
        [
            f"settings:  BUILD_CPUS={snap.build_cpus_raw}, online={snap.online_cpus_raw or 'unknown'}, target ban={target_txt}",
            *[f"note:      {n}" for n in snap.compute_notes],
        ]
    )
    if snap.env_text is None:
        lines.append(f"env file:  {snap.env_path} (absent)")
    else:
        val = snap.env_banned if snap.env_banned is not None else "unset"
        drift = "" if snap.env_banned == snap.banned_cpulist else f" (expected {snap.banned_cpulist or 'none'})"
        lines.append(f"env file:  {snap.env_path} ({_BANNED_ENV_KEY}={val}{drift})")
    if snap.sysconfig_banned is not None and snap.sysconfig_banned:
        lines.append(f"sysconfig: {snap.sysconfig_path} ({_BANNED_ENV_KEY}={snap.sysconfig_banned})")
    drop_state = "present" if snap.dropin_text is not None else "absent"
    lines.append(f"drop-in:   {snap.dropin_path} ({drop_state})")
    if snap.service_unit_present:
        lines.append(f"service:   {IRQBALANCE_SERVICE} ({snap.service_active}, {snap.service_enabled})")
    else:
        lines.append(f"service:   {IRQBALANCE_SERVICE} (unit not installed)")
    return lines


def format_irq_plan(snap: IrqSnapshot, *, no_pkg: bool = False) -> list[str]:
    """Render dry-run plan lines for `cst cpu irq plan` without side effects."""
    lines = [f"WARN: {w}" for w in snap.warnings]
    lines.append(
        f"settings:  BUILD_CPUS={snap.build_cpus_raw}, online={snap.online_cpus_raw or 'unknown'}, "
        f"target ban={snap.banned_cpulist or 'none'}"
    )
    lines.extend(f"note:      {n}" for n in snap.compute_notes)

    if snap.online_cpus_raw is None:
        lines.append("ERROR: cannot read online CPUs from /sys/devices/system/cpu/online")
        lines.append("Dry run: nothing is changed.")
        return lines

    if snap.banned_cpulist:
        want_env = render_irq_env(snap.banned_cpulist)
        want_dropin = render_irq_dropin()
        lines.append(
            f"unchanged:   {snap.env_path}"
            if snap.env_text == want_env
            else f"would write: {snap.env_path} ({_BANNED_ENV_KEY}={snap.banned_cpulist})"
        )
        lines.append(
            f"unchanged:   {snap.dropin_path}"
            if snap.dropin_text == want_dropin
            else f"would write: {snap.dropin_path}"
        )
    else:
        for label_path, content in ((snap.dropin_path, snap.dropin_text), (snap.env_path, snap.env_text)):
            lines.append(
                f"would remove: {label_path} (no BUILD_CPUS overlap)"
                if content is not None
                else f"unchanged:   {label_path} (absent)"
            )

    pkg_will_exist = snap.service_unit_present or (snap.rpm_ostree_present and not no_pkg and bool(snap.banned_cpulist))
    if not snap.service_unit_present and snap.banned_cpulist:
        if no_pkg:
            lines.append("would skip:  rpm-ostree install irqbalance (--no-pkg)")
        elif snap.rpm_ostree_present:
            lines.append("would install: irqbalance via rpm-ostree install --idempotent --apply-live irqbalance")
        else:
            lines.append("WARN: irqbalance.service not installed and rpm-ostree missing; install irqbalance manually")

    if pkg_will_exist and snap.banned_cpulist:
        cfg_changed = snap.env_text != render_irq_env(snap.banned_cpulist) or snap.dropin_text != render_irq_dropin()
        if (
            not snap.service_unit_present
            or snap.service_enabled != "enabled"
            or snap.service_active != ActiveState.ACTIVE
            or cfg_changed
        ):
            lines.append(f"would enable & start/restart: {IRQBALANCE_SERVICE}")
        else:
            lines.append(f"unchanged:   {IRQBALANCE_SERVICE} (enabled, active)")
    lines.append("Dry run: nothing is changed.")
    return lines

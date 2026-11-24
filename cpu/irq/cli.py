# SPDX-License-Identifier: MPL-2.0
"""CLI wrapper for `cpu-irq` / `cst cpu irq`."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli, undo_python_start
from cpu.irq import adapters as irq_adapters, domain as irq_domain
from cpu.irq.domain import IrqAction

IrqCliDeps = ManagedCliEnv


def _irq_write_action(parsed: irq_domain.IrqParsedArgs) -> str | None:
    return parsed.action.value if parsed.action in (IrqAction.APPLY, IrqAction.REVERT) else None


_IRQ_SPEC = ManagedCliSpec(
    default_prog=irq_domain.IRQ_PROG,
    parse=irq_domain.parse_irq_args,
    format_usage=irq_domain.format_irq_usage,
    is_help=lambda p: p.action == IrqAction.HELP,
    parse_error=lambda p: None if p.error is None else (p.code, p.error),
    write_action=_irq_write_action,
    elevation_reason="configures /etc/cpu-setup/irqbalance.env, systemd drop-in, and irqbalance.service",
    execute=irq_adapters.dispatch_irq_cli_action,
    resolve_prog=lambda raw: irq_domain.IRQ_PROG if raw == "cst cpu" else (raw or irq_domain.IRQ_PROG),
)


def run_irq_cli(argv: Sequence[str] | None = None, *, deps: IrqCliDeps | None = None) -> int:
    """Parse `cpu-irq` CLI options, gate host-root writes, and print output lines."""
    return run_managed_cli(sys.argv[1:] if argv is None else argv, _IRQ_SPEC, deps)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point invoked by `cpu/cpu-irq`."""
    undo_python_start()
    return run_irq_cli(argv, deps=IrqCliDeps(entry_file=entry_file))

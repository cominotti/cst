# SPDX-License-Identifier: MPL-2.0
"""Recorded older copies of `cst cpu` user drop-ins across changing UnitPaths."""

from dataclasses import dataclass
import os

from cst_core.application.user_units import RecordedUnitCopies
from cst_core.domain.install import Component
from cst_core.ports import InstallRecordPort
from cpu.game.adapters.host import holds
from cpu.setup.domain import MANAGED_MARK


@dataclass(frozen=True, slots=True)
class OlderDropins:
    """Recorded older user drop-in paths split into CST copies to remove and entries only to forget."""

    remove: tuple[str, ...]
    forget: tuple[str, ...]


def classify_older_dropins(
    records: InstallRecordPort,
    home: str,
    unit_path: str | None,
    pairs: tuple[tuple[str, str], ...],
) -> OlderDropins:
    """Split recorded older copies of cpu:toolbox drop-ins into remove vs forget-only.

    An entry that resolves to current by another path (e.g. Silverblue /home -> /var/home) or
    holds a non-CST file is only forgotten so the live or user-edited file is kept intact.
    """
    remove: list[str] = []
    forget: list[str] = []
    for current, rel in pairs:
        cur_real = os.path.realpath(current)
        for path in RecordedUnitCopies(records, home, Component.CPU_TOOLBOX, rel).older(current, unit_path):
            if os.path.lexists(path) and (os.path.realpath(path) == cur_real or not holds(path, MANAGED_MARK)):
                forget.append(path)
            else:
                remove.append(path)
    return OlderDropins(tuple(remove), tuple(forget))

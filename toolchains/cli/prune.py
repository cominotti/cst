# SPDX-License-Identifier: MPL-2.0
"""`prune plan|apply`: the settings, the targets the command line or the settings
choose, and the prune use case over them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from toolchains import wiring
from toolchains.application.prune import prune
from toolchains.domain.prune import select_targets

if TYPE_CHECKING:
    from collections.abc import Sequence

    from toolchains.wiring import Session


def cmd_prune(s: Session, action: str, names: Sequence[str]) -> int:
    settings = wiring.settings(s)
    chosen = select_targets(names, settings.enabled)
    return prune(
        chosen,
        lambda tid: wiring.target(s, settings, tid),
        settings,
        action,
        out=s.out,
        clock=s.machine.clock,
        limits=s.limits,
    )

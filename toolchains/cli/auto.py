# SPDX-License-Identifier: MPL-2.0
"""`auto on|off`: the AutoTimer use case (toolchains/application/auto.py) over the
session's adapters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from toolchains import wiring

if TYPE_CHECKING:
    from toolchains.wiring import Session


def cmd_auto_on(s: Session, *, unless_off: bool = False) -> int:
    return wiring.auto_timer(s).on(unless_off=unless_off)


def cmd_auto_off(s: Session) -> int:
    return wiring.auto_timer(s).off()

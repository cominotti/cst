# SPDX-License-Identifier: MPL-2.0
"""`status`: the ToolchainsStatus use case (toolchains/application/status.py) over the
session's adapters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from toolchains import wiring

if TYPE_CHECKING:
    from toolchains.wiring import Session


def cmd_status(s: Session, brief: bool) -> None:
    wiring.toolchains_status(s).show(brief=brief)

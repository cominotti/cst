# SPDX-License-Identifier: MPL-2.0
"""`config plan|apply`: the ToolConfig use case (toolchains/application/config.py)
over the session's adapters.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from toolchains import wiring

if TYPE_CHECKING:
    from toolchains.domain.common import ToolchainsAction
    from toolchains.wiring import Session


def cmd_config(s: Session, action: ToolchainsAction, *, lean: bool | None = None, sccache: bool | None = None) -> int:
    return wiring.tool_config(s).run(action, lean, sccache)

# SPDX-License-Identifier: MPL-2.0
"""`config plan|apply|init`: delegates to `wiring.run_config` over the session's adapters."""

from __future__ import annotations

from typing import TYPE_CHECKING

from toolchains import wiring

if TYPE_CHECKING:
    from toolchains.domain.common import ParsedArgs, ToolchainsAction
    from toolchains.wiring import Session


def cmd_config(s: Session, target: ParsedArgs | ToolchainsAction) -> int:
    return wiring.run_config(s, target)

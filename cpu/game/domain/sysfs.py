# SPDX-License-Identifier: MPL-2.0
"""The names in sysfs the game profile's power steps go by: a DRM card (cardN) and a cpuidle state (stateN),
each handled in the order of its number. No I/O."""

import re
from typing import Final

from cst_core.domain.text import group

_CARD_NAME_RE: Final = re.compile(r"^card(\d+)$", re.ASCII)


def suffix_number(name: str, pattern: re.Pattern[str]) -> int:
    """N of a name pattern matches whole, N its first group (-1 for another name)."""
    m = pattern.fullmatch(name)
    return int(group(m, 1)) if m is not None else -1


def card_number(name: str) -> int:
    """N of a DRM cardN name (-1 for another name): the order cards are listed and handled in."""
    return suffix_number(name, _CARD_NAME_RE)


def is_card(name: str) -> bool:
    """The name is a DRM card's (cardN), not one of its connectors."""
    return _CARD_NAME_RE.fullmatch(name) is not None

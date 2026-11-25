# SPDX-License-Identifier: MPL-2.0
"""sysfs as the game profile's power steps see it: EPP, cpuidle, the GPU's power files and the DRM discovery
(which cards the game's processes render on). The steps themselves are cpu.game.application.power."""

import os
import re
from typing import Final

from cst_core.domain.text import group
from cpu.game.adapters.host import read_stripped
from cpu.game.adapters.kernel import GameSysValues
from cpu.game.domain.sysfs import card_number, is_card

_DRI_FD_RE: Final = re.compile(r"^/dev/dri/(renderD\d+|card\d+)", re.ASCII)
_CONNECTED_STATE: Final = "connected"


class GameSysfsFiles(GameSysValues):
    """sysfs and the processes' DRM file descriptors, as files."""

    def list_dir(self, path: str) -> list[str]:
        """The names in the folder."""
        return os.listdir(path)

    def drm_cards(self, sys_drm: str) -> tuple[dict[str, str], list[str]]:
        """Each DRM card's device (its real path) to its cardN name, and every cardN, by number."""
        dev_to_card: dict[str, str] = {}
        cards: list[str] = []
        if not os.path.isdir(sys_drm):
            return dev_to_card, cards
        for entry in os.listdir(sys_drm):
            if not is_card(entry):
                continue
            cards.append(entry)
            dev_link = os.path.join(sys_drm, entry, "device")
            if os.path.exists(dev_link):
                dev_to_card[os.path.realpath(dev_link)] = entry
        cards.sort(key=card_number)
        return dev_to_card, cards

    def connected_cards(self, sys_drm: str, cards: list[str]) -> list[str]:
        """The cards with a connected display, in the order of cards."""
        connected: list[str] = []
        for card in cards:
            card_dir = os.path.join(sys_drm, card)
            prefix = f"{card}-"
            for child in os.listdir(card_dir):
                if not child.startswith(prefix):
                    continue
                if read_stripped(os.path.join(card_dir, child, "status")) == _CONNECTED_STATE:
                    connected.append(card)
                    break
        return connected

    def pid_cards(self, proc: str, sys_drm: str, pid: str, dev_to_card: dict[str, str]) -> set[str]:
        """The DRM cards whose render or card node one process holds open (none when its fds cannot be listed)."""
        fd_dir = os.path.join(proc, pid, "fd")
        try:
            entries = os.listdir(fd_dir)
        except OSError:
            return set()
        cards: set[str] = set()
        for fd_name in entries:
            try:
                target = os.readlink(os.path.join(fd_dir, fd_name))
            except OSError:
                continue
            m = _DRI_FD_RE.match(target)
            if m is None:
                continue
            dev_path = os.path.join(sys_drm, group(m, 1), "device")
            card = dev_to_card.get(os.path.realpath(dev_path))
            if card is not None:
                cards.add(card)
        return cards

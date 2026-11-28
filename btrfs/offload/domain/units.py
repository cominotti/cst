# SPDX-License-Identifier: MPL-2.0
"""Systemd user service and timer templates for `cst btrfs cache offload`."""

from __future__ import annotations

from typing import Final

from btrfs.offload.domain import OFFLOAD_CONF_REL, OFFLOAD_DEFAULT_CALENDAR, OFFLOAD_PROG_REL
from cst_core.domain.units import render_timer, render_user_background_service

OFFLOAD_SERVICE: Final = "cst-scratchpad-backup.service"
OFFLOAD_TIMER: Final = "cst-scratchpad-backup.timer"
OFFLOAD_UNIT_HEADER: Final = "# Managed by CST (`cst btrfs cache offload`); not edited by hand."


def render_offload_service() -> str:
    """Render the oneshot user service running online sqlite + rsync backups to `/home`."""
    return render_user_background_service(
        [
            OFFLOAD_UNIT_HEADER,
            "# Mirrors ~/Scratchpad offloaded folders back to /home using online sqlite3 .backup() + rsync.",
        ],
        "CST: backup ~/Scratchpad offloads to /home",
        (f"%h/{OFFLOAD_PROG_REL}", f"%h/{OFFLOAD_CONF_REL}"),
        f"/usr/bin/python3 -I %h/{OFFLOAD_PROG_REL} backup",
        "cst-scratchpad-backup",
    )


def render_offload_timer(on_calendar: str = OFFLOAD_DEFAULT_CALENDAR) -> str:
    """Render the persistent user timer triggering `cst-scratchpad-backup.service`."""
    return render_timer(
        [OFFLOAD_UNIT_HEADER],
        f"CST: cold backup of ~/Scratchpad offloads ({on_calendar})",
        on_calendar,
        "15min",
    )

# SPDX-License-Identifier: MPL-2.0
"""The user units `auto on` writes (rendered through cst_core), and the
environment systemctl --user gets to reach this user's own manager; no I/O."""

from __future__ import annotations

import dataclasses
from enum import StrEnum
import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

from cst_core.domain.units import ActiveState, render_timer as unit_timer, render_unit_file
from toolchains.domain.common import DEFAULTS_REL, MARKER, PROG_REL, SERVICE, TIMER, UNIT_DIR_REL


UNIT_HEADER = f"{MARKER}: installed by `cst toolchains auto on` (or snapshots setup); not edited by hand."


# The words of systemctl is-active (cst_core's ActiveState) that the timer's code tells apart: the
# timer runs (status shows its next run; auto off stops it), and a unit that needs no stop before its
# files are removed.
TIMER_RUNNING_STATES: Final = frozenset({ActiveState.ACTIVE})
UNIT_STOPPED_STATES: Final = frozenset({ActiveState.INACTIVE, ActiveState.FAILED})


class UserManagerVerb(StrEnum):
    """The systemctl --user commands cst-toolchains runs (the word after --user)."""

    IS_ACTIVE = "is-active"
    IS_ENABLED = "is-enabled"
    SHOW = "show"
    SHOW_ENVIRONMENT = "show-environment"
    START = "start"
    STOP = "stop"
    ENABLE = "enable"
    DISABLE = "disable"
    CLEAN = "clean"
    DAEMON_RELOAD = "daemon-reload"


def render_service() -> str:
    return render_unit_file(
        [
            UNIT_HEADER,
            "# Prunes the toolchain caches enabled in ~/.config/cst/toolchains.conf",
            "# (cargo's build folders in ~/.cache/cargo-build, ...). Why and how:",
            "# toolchains/README.md in CST.",
        ],
        [
            (
                "Unit",
                [
                    "Description=CST: prune old toolchain caches (cst toolchains)",
                    f"ConditionPathExists=%h/{PROG_REL}",
                    f"ConditionPathExists=%h/{DEFAULTS_REL}",
                ],
            ),
            (
                "Service",
                [
                    "Type=oneshot",
                    f"ExecStart=/usr/bin/python3 -I %h/{PROG_REL} prune apply",
                    "SyslogIdentifier=cst-toolchains",
                    "Nice=19",
                    "IOSchedulingClass=idle",
                    "TimeoutStartSec=30min",
                    "NoNewPrivileges=yes",
                ],
            ),
        ],
    )


def render_timer() -> str:
    return unit_timer([UNIT_HEADER], "CST: prune old toolchain caches, hourly", "hourly", "10min")


# The user units of auto on: {name: its text}.
AUTO_UNITS: dict[str, Callable[[], str]] = {SERVICE: render_service, TIMER: render_timer}

# The file auto off leaves (OFF_REL): while it exists, snapshots setup does not turn the timer on.
OFF_TEXT = (
    f"{MARKER}: written by `cst toolchains auto off`.\n"
    "# While this file exists, `cst btrfs snapshots setup` does not turn the hourly\n"
    "# pruning of toolchain caches back on. `cst toolchains auto on` removes it.\n"
)


@dataclasses.dataclass(frozen=True, slots=True)
class TimerFacts:
    """What the user manager says of a timer: is-enabled, is-active ('' where it gave no
    answer) and, only while it is active, its next run (show NextElapseUSecRealtime)."""

    enabled: str
    active: str
    next_elapse: str = ""


def label(key: str) -> str:
    return f"~/{key}" if "/" in key else f"~/{UNIT_DIR_REL}/{key}"


def own_session_env(env: Mapping[str, str], uid: int, owner: int | None, *, own_exists: bool) -> dict[str, str]:
    """The environment systemctl --user gets: env with this user's XDG_RUNTIME_DIR (owner: the
    owner of env's folder, None when it does not exist). None handed down (root through runuser):
    /run/user/UID when it exists. Another uid's (owned by it, or /run/user/N with another N, as
    sudo and runuser pass on root's /run/user/0): /run/user/UID, and a bus inside it, UID's bus."""
    out, own, xdg = dict(env), f"/run/user/{uid}", env.get("XDG_RUNTIME_DIR", "")
    if not xdg:
        if own_exists:
            out["XDG_RUNTIME_DIR"] = own
        return out
    named = re.fullmatch(r"/run/user/(\d+)/?", xdg, re.ASCII)
    if (owner is not None and owner != uid) or (named is not None and int(named.group(1)) != uid):
        out["XDG_RUNTIME_DIR"] = own
        if f"={xdg.rstrip('/')}/" in env.get("DBUS_SESSION_BUS_ADDRESS", ""):
            out["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={own}/bus"
    return out

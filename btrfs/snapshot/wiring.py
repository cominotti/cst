# SPDX-License-Identifier: MPL-2.0
"""The composition root of btrfs-snapshot-core: the environment (TZ), the
time zone, the journal stream and the adapters behind the ports.

btrfs/snapshot/cli.py parses argv, asks this module for the time zone, the
log and the Machine (a test hands its own host), runs one use case of
btrfs/snapshot/application.py and renders the answer.
"""

from __future__ import annotations

import datetime as dt
import os
from typing import TextIO
from btrfs.snapshot.adapters import Btrfs, Host, Log, PinFile, RunLock, StateDir, known_zone, local_tz
from btrfs.snapshot.application import Machine
from btrfs.snapshot.ports import LogFn, SnapshotHostPort
from cst_core.domain.errors import Usage


def journal(stream: TextIO) -> LogFn:
    """The journal lines of this run, on STREAM."""
    return Log(stream)


def run_time_zone(name: str | None) -> dt.tzinfo:
    """The zone of the names and the retention: --tz NAME, else $TZ, else /etc/localtime.

    Usage for an unknown NAME."""
    if name:
        tz = known_zone(name)
        if tz is None:
            raise Usage(f"unknown --tz {name!r}")
        return tz
    return local_tz(None, os.environ.get("TZ", ""))


def machine(host: SnapshotHostPort | None, log: LogFn, *, state_dir: str, pin_file: str) -> Machine:
    """The ports of one invocation over the real adapters (HOST: a test's fake, or None)."""
    h = Host() if host is None else host
    return Machine(h, Btrfs(h), StateDir(state_dir), PinFile(pin_file), RunLock(), log)

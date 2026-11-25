# SPDX-License-Identifier: MPL-2.0
"""The Docker volumes NOCOW checks of the btrfs area of `cst doctor`: what docker-volumes-nocow leaves on
disk (cst doctor never opens Docker's own folders, which are root's only). btrfs.py puts them in its
list (@check), in their place among its checks: one module registers each area's checks."""

from collections.abc import Iterator
import time

from btrfs.docker_volumes_nocow.domain import (
    DAEMON_JSON,
    NocowOutcome,
    OutcomeRead,
    is_config_reason,
    is_stored_postponed,
    outcome_unreadable,
    parse_outcome,
)
from cst_core.domain.shapes import parse_json
from doctor.domain import (
    INFO,
    NOCOW_ATTEMPTS,
    NOCOW_FLAG,
    NOCOW_HELPER_HOST,
    NOCOW_OUTCOME,
    NOCOW_POSTPONED,
    NOCOW_WARN_BOOTS,
    WARN,
    Result,
)
from doctor.ports import DoctorHostPort


def check_docker_nocow_postponed(host: DoctorHostPort) -> Iterator[Result]:
    try:
        with host.open_text(host.path(NOCOW_POSTPONED)) as f:
            rec = parse_json(f.read(65536))
    except FileNotFoundError:
        return  # never postponed, or not set up
    except OSError, ValueError:
        rec = None
    if not is_stored_postponed(rec):
        yield Result(INFO, f"Docker volumes NOCOW: cannot read {NOCOW_POSTPONED}")
        return
    boots, last = rec.get("boots"), rec.get("last")
    if type(boots) is not int or boots < 1:
        boots = 1
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(last)) if type(last) is int else "at an unknown time"
    reason = str(rec.get("reason") or "no reason recorded")
    msg = f"Docker volumes NOCOW: the boot-time migration was postponed at {boots} boot(s) in a row, last {when}: {reason}"
    if boots >= NOCOW_WARN_BOOTS and is_config_reason(reason):
        # Waiting for a quiet boot does not help: only a fixed file lets the migration run.
        yield Result(
            WARN,
            f"{msg}; fix {DAEMON_JSON} (the fix line checks it as dockerd reads it), and the next boot runs it",
            f"sudo dockerd --validate --config-file {DAEMON_JSON}",
        )
    elif boots >= NOCOW_WARN_BOOTS:
        yield Result(
            WARN,
            f"{msg}; the volumes keep copy-on-write data (and new ones may too) until a boot where nothing uses them",
            "journalctl -b -u docker-volumes-nocow",
        )
    else:
        yield Result(INFO, f"{msg}; tried again at the next boot")


def _nocow_outcome(host: DoctorHostPort) -> OutcomeRead:
    """docker-volumes-nocow's outcome record, read as the helper reads it."""
    try:
        with host.open_text(host.path(NOCOW_OUTCOME)) as f:
            text = f.read(65536)
    except FileNotFoundError, NotADirectoryError:  # none there (its folder may be a file), as the helper reads it
        return parse_outcome(None, NOCOW_OUTCOME)
    except OSError, ValueError:
        return outcome_unreadable(NOCOW_OUTCOME)
    return parse_outcome(text, NOCOW_OUTCOME)


def _nocow_count(host: DoctorHostPort) -> int:
    """The helper's count of boots, as CounterFile.count reads it (0: none, or no number)."""
    try:
        with host.open_text(host.path(NOCOW_ATTEMPTS)) as f:
            return int(f.read(256))
    except OSError, ValueError:
        return 0


def check_docker_nocow_outcome(host: DoctorHostPort) -> Iterator[Result]:
    """A Docker volumes migration no boot will ask for, from what docker-volumes-nocow leaves on disk."""
    if not host.exists(host.path(NOCOW_HELPER_HOST)):
        return  # not installed: nothing of it counts
    try:
        host.stat(host.path(NOCOW_FLAG))
        return  # a migration is scheduled
    except FileNotFoundError:
        pass
    except OSError as e:  # "cannot stat" is no "no flag"
        yield Result(INFO, f"Docker volumes NOCOW: cannot tell whether a migration is scheduled ({e.strerror or e})")
        return
    read = _nocow_outcome(host)
    if read.problem:
        yield Result(INFO, f"Docker volumes NOCOW: {read.problem}")
    elif read.record is not None and read.outcome == NocowOutcome.NOT_SCHEDULED:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(read.record["at"]))
        yield Result(
            WARN,
            f"Docker volumes NOCOW: {read.record['volumes']} keeps copy-on-write data and no boot will migrate it "
            f"(an older install stopped before it scheduled the migration, or a container wrote during the "
            f"install), and the install at {when} failed: {read.record['reason']}; fix that, then run the install",
            "cst btrfs install",
        )
        return
    count = _nocow_count(host)
    if count > 0:
        yield Result(
            WARN,
            f"Docker volumes NOCOW: a count of {count} boot(s) is left with no migration scheduled: an install or "
            "a boot could not reset it, so a migration may not be asked for (journalctl -u docker-volumes-nocow "
            "says why); fix that, then run the install",
            "cst btrfs install",
        )

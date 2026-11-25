# SPDX-License-Identifier: MPL-2.0
"""The Docker volumes NOCOW checks of the btrfs area of `cst doctor`: what docker-volumes-nocow leaves on
disk (cst doctor never opens Docker's own folders, which are root's only). btrfs.py puts them in its
list (@check), in their place among its checks: one module registers each area's checks."""

from collections.abc import Iterator
import time

from btrfs.docker_volumes_nocow.domain import DAEMON_JSON, is_config_reason, is_stored_postponed
from cst_core.domain.shapes import parse_json
from doctor.domain import INFO, NOCOW_POSTPONED, NOCOW_WARN_BOOTS, WARN, Result
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

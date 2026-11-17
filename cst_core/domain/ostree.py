# SPDX-License-Identifier: MPL-2.0
"""rpm-ostree's deployments: the one query (`rpm-ostree status --json`) and its reading.

The deployments are rpm-ostree's JSON objects as they come (many keys of
many types), read with .get where they are used. The bash copies of this
query (btrfs-setup.sh, btrfs/setup-snapshots.bash, btrfs/btrfs-health-check,
btrfs/btrfs-degraded-boot-setup.sh) stay until those scripts are Python;
vm's booted_deployment (vm/adapters.py) keeps its own reading, which answers
None on any failure, until the vm conversion.
"""

from collections.abc import Callable
from dataclasses import dataclass
import json
from typing import Final

from cst_core.domain.text import first_line
from cst_core.domain.values import Json

# The query, as rpm-ostree takes it.
_STATUS_ARGV: Final[tuple[str, ...]] = ("rpm-ostree", "status", "--json")

# Runs a read-only query command: argv -> (exit status, stdout, stderr).
type _QueryFn = Callable[[list[str]], tuple[int, str, str]]


@dataclass(frozen=True, slots=True)
class OstreeStatus:
    """What `rpm-ostree status --json` says about the deployments.

    deployments: every deployment that is a JSON object, in the order ostree
    admin status numbers them. booted: the booted one. pending: the
    deployment of the next boot when it is not the booted one: rpm-ostree
    lists it first (for the booted system's osname), staged or not
    (rpm-ostree rollback leaves an unstaged one there). It is the one
    rpm-ostree install/uninstall edit.
    """

    deployments: list[dict[str, Json]]
    booted: dict[str, Json] | None
    pending: dict[str, Json] | None


def _parse_status(text: str) -> OstreeStatus:
    """Read the JSON of `rpm-ostree status --json`.

    Raises ValueError (not JSON), KeyError (no "deployments") or TypeError
    (not an object, or "deployments" cannot be iterated), with the text
    Python gives each. A "deployments" string or object is iterated: none of
    its items is an object, so it reads as no deployments (as doctor always did).
    """
    data = json.loads(text)
    deps = [d for d in data["deployments"] if isinstance(d, dict)]
    booted = next((d for d in deps if d.get("booted")), None)
    osname = booted.get("osname") if booted else None
    first = next((d for d in deps if osname is None or d.get("osname") == osname), None)
    pending = first if first is not None and first is not booted else None
    return OstreeStatus(deps, booted, pending)


def query_status(run: _QueryFn) -> OstreeStatus | str:
    """Ask rpm-ostree through run; the status, or why there is none (one line)."""
    rc, out, err = run(list(_STATUS_ARGV))
    if rc != 0:
        return f"rpm-ostree status --json failed: {first_line(err) or f'exit {rc}'}"
    try:
        return _parse_status(out)
    except (ValueError, KeyError, TypeError) as e:
        return f"rpm-ostree status --json: unreadable output ({e})"

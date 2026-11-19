# SPDX-License-Identifier: MPL-2.0
"""rpm-ostree's deployments: the one query (`rpm-ostree status --json`) and its reading.

The deployments are rpm-ostree's JSON objects as they come (many keys of
many types), read with .get where they are used. The bash copies of this
query (btrfs-setup.sh, btrfs/setup-snapshots.bash,
btrfs/btrfs-degraded-boot-setup.sh) stay until those scripts are Python.
doctor asks query_status, and so do vm's booted_deployment (vm/adapters/__init__.py),
which records None for any failure, and btrfs-health-check (btrfs/health_check/adapters.py).

For the next boot's 64-btrfs.rules, btrfs-health-check (next_root) and
btrfs-degraded-boot-setup.sh (ostree_query next-root and tracked) take
deployments[0], the bootloader's default entry, on purpose, not pending
(which applies the booted system's osname rule, for rpm-ostree
install and initramfs-etc). The two differ only with several osnames
installed. The health check keeps the script's choice until degraded-boot
setup is Python.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from cst_core.domain.shapes import json_dict, json_object, parse_json
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

    Raises ValueError (not JSON, or not a JSON object), KeyError (no
    "deployments") or TypeError ("deployments" cannot be iterated), with the
    text Python gives each. A "deployments" string or object is iterated: none
    of its items is an object, so it reads as no deployments (as doctor always did).
    """
    data = parse_json(text)
    if not json_object(data):
        raise ValueError("not a JSON object")
    listed = data["deployments"]
    if not isinstance(listed, list | str | dict):
        raise TypeError(f"'{type(listed).__name__}' object is not iterable")
    deps = [d for d in listed if json_dict(d)]
    booted = next((d for d in deps if d.get("booted")), None)
    osname = booted.get("osname") if booted is not None else None
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
    except (ValueError, KeyError, TypeError, RecursionError) as e:  # RecursionError: nesting too deep
        return f"rpm-ostree status --json: unreadable output ({e})"

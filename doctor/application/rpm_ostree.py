# SPDX-License-Identifier: MPL-2.0
"""The rpm-ostree area of `cst doctor`: layered packages pinned to a version, local packages, overrides,
the staged deployment."""

from collections.abc import Iterator
import shlex
import time

from cst_core.domain.values import DAY
from doctor.application.shared import check, installed_packages, ostree_facts
from doctor.domain import (
    INFO,
    OK,
    STAGED_OLD_DAYS,
    WARN,
    Deployment,
    OstreeAnswer,
    PinHow,
    Request,
    Result,
    classify_requests,
    describe,
    pkg_names,
    unpin_command,
)
from doctor.ports import CheckFn, DoctorHostPort

# This area's checks, in the order the report prints them (doctor/application/registry.py puts the areas in order).
CHECKS: list[tuple[str, CheckFn]] = []


def deployments(host: DoctorHostPort) -> OstreeAnswer:
    """(booted, pending, error) of ostree_facts."""
    return ostree_facts(host)[0]


def _classified(
    host: DoctorHostPort, booted: Deployment | None, pending: Deployment | None
) -> dict[int, tuple[Deployment, list[str], list[Request], list[Request]]]:
    """{id(d): (d, its requests, each classified, those pinned to a version)} of the booted and the pending
    deployment, the ones there are."""
    found: dict[int, tuple[Deployment, list[str], list[Request], list[Request]]] = {}
    for d in (booted, pending):
        if d is None:
            continue
        reqs = d.get("requested-packages") or []
        cls = classify_requests(reqs, installed_packages(host, d), host.arch)
        found[id(d)] = (d, reqs, cls, [c for c in cls if c[2] != PinHow.NAME])
    return found


def _pinned_message(d: Deployment, reqs: list[str], pinned: list[Request]) -> str:
    """What a deployment's requests pinned to a version mean (reqs: all its requests)."""
    listed = ", ".join(r for r, _, how in pinned if how != PinHow.INACTIVE)
    inactive = [r for r, _, how in pinned if how == PinHow.INACTIVE]
    msg = f"{describe(d)}: {len(pinned)} of {len(reqs)} layered packages are pinned to a version"
    if listed:
        msg += " (they never update, and an upgrade fails once that version leaves the repositories): " + listed
    if any(how == PinHow.PINNED_FORM for _, _, how in pinned):
        msg += " (recognised by their form only: the package list was not readable)"
    if inactive:
        msg += (
            "; pinned but matching no installed package (probably inactive requests: the base "
            "image may provide them now; the fix drops them without asking them back): " + ", ".join(inactive)
        )
    return msg


@check(CHECKS, "rpm-ostree")
def check_pinned_layering(host: DoctorHostPort) -> Iterator[Result]:
    booted, pending, err = deployments(host)
    if err:
        yield Result(INFO if err.startswith("not an") else WARN, f"layered packages: not checked ({err})")
        return
    found = _classified(host, booted, pending)
    if not found:
        yield Result(WARN, "layered packages: no deployment in rpm-ostree status")
        return
    # rpm-ostree uninstall edits the next boot's deployment: the fix is built from it.
    target = pending if pending is not None else booted
    assert target is not None
    # found[id(d)] starts with d itself: this binds target to the same object (typed as found's).
    target, treqs, _tcls, tpinned = found[id(target)]
    for d, reqs, cls, pinned in found.values():
        if not pinned:
            continue
        msg = _pinned_message(d, reqs, pinned)
        if d is target:
            yield Result(WARN, msg, unpin_command(cls))
        elif not tpinned:
            # Booted pinned, but the next boot's deployment (target, the pending one: with no
            # pending deployment the booted one is target, and found holds nothing else) is clean.
            yield Result(INFO, msg + f"; the {describe(target)} has no version-pinned requests: reboot to use it")
    if not tpinned:
        yield Result(OK, f"{describe(target)}: {len(treqs)} layered packages, all requested by name")


@check(CHECKS, "rpm-ostree")
def check_local_packages(host: DoctorHostPort) -> Iterator[Result]:
    booted, pending, err = deployments(host)
    if err:
        return
    target = pending or booted
    if target is None:
        return
    local = list(target.get("requested-local-packages") or []) + list(
        target.get("requested-local-fileoverride-packages") or []
    )
    if not local:
        yield Result(OK, f"{describe(target)}: no local RPM files layered")
        return
    names = pkg_names(local)
    words = ["sudo", "rpm-ostree", "uninstall"] + [str(x) for x in local] + [f"--install={n}" for n in names]
    yield Result(
        WARN,
        f"{describe(target)}: {len(local)} local RPM file(s) layered: "
        f"{', '.join(map(str, local))}. rpm-ostree upgrade never updates them. "
        "Run the fix only if a repository has the package: it then comes from there",
        " ".join(shlex.quote(w) for w in words),
    )


@check(CHECKS, "rpm-ostree")
def check_overrides(host: DoctorHostPort) -> Iterator[Result]:
    booted, pending, err = deployments(host)
    if err:
        return
    target = pending or booted
    if target is None:
        return
    replaced = pkg_names(
        [
            target.get("base-remote-replacements") or {},
            target.get("base-local-replacements") or [],
            target.get("requested-base-local-replacements") or [],
        ]
    )
    removed = pkg_names([target.get("base-removals") or [], target.get("requested-base-removals") or []])
    if replaced:
        yield Result(
            WARN,
            f"{describe(target)}: base packages replaced by another version: "
            f"{', '.join(replaced)}. They stay at that version through every upgrade",
            " ".join(shlex.quote(w) for w in ["sudo", "rpm-ostree", "override", "reset", *replaced]),
        )
    if removed:
        yield Result(
            INFO,
            f"{describe(target)}: base packages removed on purpose: {', '.join(removed)} "
            f"(to bring them back: sudo rpm-ostree override reset {' '.join(removed)})",
        )
    if not replaced and not removed:
        yield Result(OK, f"{describe(target)}: no base package replaced or removed")


@check(CHECKS, "rpm-ostree")
def check_staged(host: DoctorHostPort) -> Iterator[Result]:
    _booted, pending, err = deployments(host)
    if err:
        return
    if pending is None:
        yield Result(OK, "no pending deployment: the next boot is the booted one")
        return
    if not pending.get("staged", False):
        yield Result(
            INFO,
            f"{describe(pending)} is not the booted one (for example after "
            "rpm-ostree rollback): it becomes the system at the next boot",
        )
        return
    ts = pending.get("timestamp")
    age = ""
    if isinstance(ts, (int, float)) and ts > 0:
        days = (host.now - ts) / DAY
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
        age = f", built {when}"
        if days >= STAGED_OLD_DAYS:
            age += f", {int(days)} days ago"
    yield Result(INFO, f"{describe(pending)} waits for a reboot{age} (it becomes the system at the next boot)")

# SPDX-License-Identifier: MPL-2.0
"""The `cst doctor` report: the check results as text lines (presentation only)."""

from collections.abc import Sequence
from typing import TextIO

from doctor.domain import NAME, SEVERITY, WARN, Result, summary


def _print_header(out: TextIO, line: str, prog: str, *, brief: bool, container: bool) -> None:
    """--brief: the summary line first; else the title, and in a toolbox where to run the commands."""
    if brief:
        print(line, file=out)
        return
    print(f"{prog}: read-only check-up (nothing is changed)", file=out)
    if container:
        print(
            "Inside a toolbox: run every command shown here on the host (a host terminal, "
            "or put flatpak-spawn --host in front of a command without sudo).",
            file=out,
        )


def _print_result(out: TextIO, area: str, r: Result, label: str, *, brief: bool) -> None:
    """One result and its fix (--brief: tagged with its area)."""
    tag = f"[{area}] " if brief else ""
    print(f"  {r.status:<5}  {tag}{r.message}", file=out)
    if r.fix:
        print(f"         {label} {r.fix}", file=out)


def render(
    results: Sequence[tuple[str, Result]], brief: bool, out: TextIO, container: bool = False, prog: str = NAME
) -> int:
    """Print the results; container: running in a toolbox, where the fix
    commands do not work (no rpm-ostree, no host journal): say "on the host"."""
    line, rc = summary(results)
    label = "fix (on the host):" if container else "fix:"
    _print_header(out, line, prog, brief=brief, container=container)
    area_now = None
    for area, r in results:
        if brief and SEVERITY[r.status] < SEVERITY[WARN]:
            continue
        if not brief and area != area_now:
            print(f"\n== {area}", file=out)
            area_now = area
        _print_result(out, area, r, label, brief=brief)
    if not brief:
        print(f"\n{line}", file=out)
        if rc:
            print("Each fix is a suggestion: read it before you run it. Details: doctor/README.md in CST.", file=out)
    return rc

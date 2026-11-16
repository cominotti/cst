"""The `cst doctor` report: the check results as text lines (presentation only)."""

from collections.abc import Sequence
from typing import TextIO

from doctor.domain import NAME, SEVERITY, WARN, Result, summary


def render(
    results: Sequence[tuple[str, Result]], brief: bool, out: TextIO, container: bool = False, prog: str = NAME
) -> int:
    """Print the results; container: running in a toolbox, where the fix
    commands do not work (no rpm-ostree, no host journal): say "on the host"."""
    line, rc = summary(results)
    label = "fix (on the host):" if container else "fix:"
    if brief:
        print(line, file=out)
    else:
        print(f"{prog}: read-only check-up (nothing is changed)", file=out)
        if container:
            print(
                "Inside a toolbox: run every command shown here on the host (a host terminal, "
                "or put flatpak-spawn --host in front of a command without sudo).",
                file=out,
            )
    area_now = None
    for area, r in results:
        if brief and SEVERITY[r.status] < SEVERITY[WARN]:
            continue
        if not brief and area != area_now:
            print(f"\n== {area}", file=out)
            area_now = area
        tag = f"[{area}] " if brief else ""
        print(f"  {r.status:<5}  {tag}{r.message}", file=out)
        if r.fix:
            print(f"         {label} {r.fix}", file=out)
    if not brief:
        print(f"\n{line}", file=out)
        if rc:
            print("Each fix is a suggestion: read it before you run it. Details: doctor/README.md in CST.", file=out)
    return rc

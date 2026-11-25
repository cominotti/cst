# SPDX-License-Identifier: MPL-2.0
"""What several check areas of `cst doctor` use: the facts asked once per run (rpm-ostree with every
deployment, the libvirt domains, each VM's definition), the settings files, an installed program, the
command that installs a component again, and the decorator that registers a check (@check)."""

from collections.abc import Callable, Container, Iterator
from dataclasses import dataclass, field

from cst_core.domain.install import COMPONENTS, Component, Owner
from cst_core.domain.ostree import OstreeStatus, query_status
from cst_core.domain.record import InstallRecord, RecordFormatError
from cst_core.domain.text import (
    SettingsProblem,
    ShellAssignments,
    enum_member,
    parse_shell_assignments,
    parse_snapshots_conf,
)
from doctor.domain import (
    SNAP_CONF,
    WARN,
    Deployment,
    HostPath,
    OstreeAnswer,
    Result,
    ViewPath,
    is_deployment,
)
from doctor.ports import CheckFn, DoctorHostPort


def check[F: CheckFn](checks: list[tuple[str, CheckFn]], area: str) -> Callable[[F], F]:
    """Register a check function under an output area, in its area module's list (checks)."""

    def register(fn: F) -> F:
        checks.append((area, fn))
        return fn

    return register


@dataclass(slots=True)
class DoctorFacts:
    """Answers several checks need, asked once per Host (one check-up): the
    rpm-ostree answer with every deployment, the libvirt domains, and each
    VM's inactive definition. Kept here, not on the host port: the port
    reaches the machine, this is the run's own state."""

    ostree: tuple[OstreeAnswer, list[Deployment]] | None = None
    domains: tuple[list[str] | None, str] | None = None
    dumpxml: dict[str, tuple[int, str, str]] = field(default_factory=dict)
    # Read once, boxed (None: not read yet): snapshots.conf's text with its line ends, None when it
    # cannot be read (snapshots_conf_text).
    snap_conf: tuple[str | None] | None = None
    # cst vm's backup index as read_vm_index reads it, boxed the same way.
    vm_index: tuple[tuple[object, str | None] | None] | None = None
    # Root's install record and the user's as the install check last read them (None: it has not run):
    # (owner, path, the record, None for none, or the error its read raised).
    records: list[tuple[Owner, HostPath, InstallRecord | RecordFormatError | OSError | None]] | None = None


# host -> its facts. A host hashes by identity, so a new Host starts with none.
_FACTS: dict[DoctorHostPort, DoctorFacts] = {}


def facts(host: DoctorHostPort) -> DoctorFacts:
    """The facts of this Host (by identity: a new Host starts with none)."""
    known = _FACTS.get(host)
    if known is None:
        known = _FACTS[host] = DoctorFacts()
    return known


def ostree_deployments(host: DoctorHostPort) -> tuple[OstreeAnswer, list[Deployment]]:
    """((booted, pending, error), every deployment in the order ostree admin
    status numbers them), from cst_core's one rpm-ostree query
    (cst_core.domain.ostree: pending is the next boot's deployment when it
    is not the booted one)."""
    if not host.exists(host.path(HostPath("/run/ostree-booted"))):
        return (None, None, "not an rpm-ostree system"), []
    status = query_status(host.run)
    if not isinstance(status, OstreeStatus):
        return (None, None, status), []
    booted = status.booted if is_deployment(status.booted) else None
    pending = status.pending if is_deployment(status.pending) else None
    return (booted, pending, ""), [d for d in status.deployments if is_deployment(d)]


def installed_packages(host: DoctorHostPort, d: Deployment) -> list[str] | None:
    """The NEVRAs in a deployment (rpm-ostree db list CHECKSUM), or None."""
    csum = d.get("checksum")
    if csum is None or csum == "":
        return None
    rc, out, _ = host.run(["rpm-ostree", "db", "list", csum])
    if rc != 0:
        return None
    pkgs = [ln.strip() for ln in out.splitlines() if ln.startswith(" ") and ln.strip()]
    return pkgs or None


def ostree_facts(host: DoctorHostPort) -> tuple[OstreeAnswer, list[Deployment]]:
    """ostree_deployments, asked once per run (several checks use it)."""
    known = facts(host)
    if known.ostree is None:
        known.ostree = ostree_deployments(host)
    return known.ostree


def runs(host: DoctorHostPort, program: HostPath) -> bool:
    """program is installed and executable."""
    view = host.path(program)
    return host.isfile(view) and bool(host.stat(view).st_mode & 0o111)


def read_text(host: DoctorHostPort, path: ViewPath, limit: int = 256) -> str:
    try:
        with host.open_text(path) as f:
            return f.read(limit).strip()
    except OSError:
        return ""


def snapshots_conf_text(host: DoctorHostPort) -> str | None:
    """snapshots.conf as UTF-8 (bad bytes replaced) with its line ends as they are, read once per
    check-up (several rows read it, and each must judge the same text); None when it cannot be read."""
    known = facts(host)
    if known.snap_conf is None:
        try:
            with host.open_bytes(host.path(SNAP_CONF)) as f:
                text: str | None = f.read().decode("utf-8", "replace")
        except OSError:
            text = None
        known.snap_conf = (text,)
    return known.snap_conf[0]


def settings_of(host: DoctorHostPort, path: HostPath, *, bash_sourced: bool = True) -> ShellAssignments | None:
    """A settings file read with cst_core's one reader (as bash reads it when
    bash_sourced), or None when it cannot be read. Read as text, as the game profile and cst vm
    read cpu-setup.conf and vm.conf (each line end a newline). snapshots.conf with its line ends
    as they are, as btrfs-snapshot and btrfs-health-check read it (a "\r" is part of its line,
    which then sets nothing), and also with the lines an older CST wrote there with printf %q
    (parse_snapshots_conf)."""
    if path == SNAP_CONF:
        text = snapshots_conf_text(host)
        return None if text is None else parse_snapshots_conf(text)
    try:
        with host.open_text(host.path(path)) as f:
            return parse_shell_assignments(f.read(), bash_sourced=bash_sourced)
    except OSError:
        return None


def conf_value(host: DoctorHostPort, path: HostPath, key: str) -> str | None:
    """The value of KEY in a settings file that bash also sources (the last
    assignment wins, as in bash and in vm-pin), or None. A problem line sets
    nothing; settings_file_warnings reports it."""
    parsed = settings_of(host, path)
    return None if parsed is None else parsed.values.get(key)


def settings_file_warnings(
    host: DoctorHostPort,
    path: HostPath,
    *,
    bash_sourced: bool,
    note: str | None = None,
    skip: Container[SettingsProblem] = (),
) -> Iterator[Result]:
    """WARN for each line of a settings file that is not a plain assignment, but the lines in skip
    (another check warns about them). note: what the warning adds after "take nothing from the line" (by
    default, for a file bash sources, that bash reads the line its own way)."""
    parsed = settings_of(host, path, bash_sourced=bash_sourced)
    if note is not None:
        bash = note
    elif bash_sourced:
        bash = " (bash, which also sources this file, reads it its own way)"
    else:
        bash = ""
    for p in parsed.problems if parsed else ():
        if p in skip:
            continue
        yield Result(WARN, f"{path} {p}. {ignored_line_text(bash)}")


def ignored_line_text(note: str) -> str:
    """What a problem line of a settings file means for CST (settings_file_warnings); note goes after
    "take nothing from the line"."""
    return (
        f"CST's Python programs take nothing from the line{note}, and the cst commands that change the "
        "machine with these settings refuse to run until it is fixed"
    )


def refresh(host: DoctorHostPort, component: str) -> str:
    """The command that installs component's files again, as the hints name cst ("" for a word that names
    no Component: a record line of a newer CST)."""
    known = enum_member(Component, component)
    cmd = "" if known is None else COMPONENTS[known]
    return f"{host.cst} {cmd.removeprefix('cst ')}" if cmd else ""

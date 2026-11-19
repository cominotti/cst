# SPDX-License-Identifier: MPL-2.0
"""Domain model and formatting for the top-level `cst` module dispatcher, and the
routing table of the modules whose setup script is a shim (vm, toolchains)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from cst_core.domain.text import HELP_WORDS


@dataclass(frozen=True, slots=True)
class ModuleSpec:
    """Descriptor for one top-level CST module entrypoint."""

    name: str
    script: str
    about: str
    status_needs_root: bool


MODULES: Final[tuple[ModuleSpec, ...]] = (
    ModuleSpec(
        name="btrfs",
        script="btrfs-setup.sh",
        about="storage: disk health, stalls, snapshots, ~/Bulk, Docker volumes and cleanup",
        status_needs_root=True,
    ),
    ModuleSpec(
        name="cpu",
        script="cpu-setup.sh",
        about="CPU: game profile, VM/toolbox/Docker priority and E-cores, measurements",
        status_needs_root=True,
    ),
    ModuleSpec(
        name="swap",
        script="swap-setup.sh",
        about="swap and memory: zswap, encrypted NVMe swap, zram off, swap sysctls, VM memory floor",
        status_needs_root=False,
    ),
    ModuleSpec(
        name="bluetooth",
        script="bluetooth-setup.sh",
        about="Bluetooth audio: LE Audio (BAP/LC3) in BlueZ, adaptive LDAC for the headset",
        status_needs_root=False,
    ),
    ModuleSpec(
        name="toolchains",
        script="toolchains-setup.sh",
        about="toolchain caches and settings: pruning of cargo build folders and uv's cache, cargo/uv settings",
        status_needs_root=False,
    ),
    ModuleSpec(
        name="vm",
        script="vm-setup.sh",
        about=(
            "the libvirt VM: backups (disks, UEFI variables, TPM) to the NAS weekly and before upgrades, "
            "virtio hardware (tune), NOCOW image folders, status"
        ),
        status_needs_root=False,
    ),
)

MODULE_BY_NAME: Final[Mapping[str, ModuleSpec]] = {spec.name: spec for spec in MODULES}

_USAGE_HEADER: Final[str] = """
cst

CST (Cominotti Setup Tools): one command for the setup tools of this
workstation. Each module is a script next to this one, and still works on
its own; through cst its help and hints say `cst MODULE ...`.

  cst btrfs [ARGS...]    storage: disk health, stalls, snapshots, ~/Bulk,
                         Docker volumes and cleanup (btrfs-setup.sh)
  cst cpu [ARGS...]      CPU: game profile, VM/toolbox/Docker priority and
                         E-cores, measurements (cpu-setup.sh)
  cst swap [ARGS...]     swap and memory: zswap, encrypted NVMe swap, zram
                         off, swap sysctls, VM memory floor (swap-setup.sh)
  cst bluetooth [ARGS...]
                         Bluetooth audio: LE Audio (BAP/LC3) in BlueZ,
                         adaptive LDAC for the headset (bluetooth-setup.sh)
  cst toolchains [ARGS...]
                         toolchain caches and settings: hourly pruning of
                         cargo's build folders, uv cache prune, cargo
                         build-dir and uv link-mode (toolchains-setup.sh;
                         as your user)
  cst vm [ARGS...]       the libvirt VM: backups of its disks, UEFI
                         variables and TPM state (weekly to the NAS with
                         the VM running, or a local copy before risky upgrades),
                         the recommended virtual hardware (tune), NOCOW
                         for the VM image folders (nocow), and its status
                         (vm-setup.sh; backup and tune apply as root)
  cst doctor [--brief]   read-only check-up across modules: ok/info/WARN/
                         ERROR lines, most WARNs with the command that fixes
                         it (layered packages pinned to a version, failed
                         units, unseen alerts, btrfs chunk allocation).
                         Exit 1 if any WARN/ERROR
                         (doctor/cst-doctor; as your user, also in a toolbox)
  cst status             the read-only status of every module; asks sudo
                         once when one of them needs root (inside a toolbox
                         those are skipped, with a note); ends with the
                         short form of cst doctor
  cst help               this overview; `cst MODULE help` lists the
                         commands of one module

Root commands of a module call sudo by themselves: type `cst btrfs install`,
not `sudo cst ...` (sudo would not find cst in its PATH). cst may be called
through a symlink, e.g. ~/.local/bin/cst -> the cst of this folder.

Modules here:"""


def format_cst_usage(present_modules: Mapping[str, bool]) -> str:
    """Format `cst help` output with availability annotations per module."""
    lines = [_USAGE_HEADER]
    for spec in MODULES:
        state = "" if present_modules.get(spec.name, False) else " (not in this copy)"
        lines.append(f"  {spec.name:<10} {spec.about}{state}")
    return "\n".join(lines)


# ---- Routing a module's command to its program (vm-setup.sh, toolchains-setup.sh) ----
#
# A module whose setup script is a shim (cst_core/route.py runs the route):
# which of its commands only read, which change the caller's files, which
# need root, and what each one execs. The privileges:
#   read     as the caller; in a toolbox, on the host through flatpak-spawn
#            (without it, from the container, after a note)
#   user     changes the caller's files only (the program calls sudo itself
#            for the rest): on the host, as the caller
#   root     on the host; as a user, the script runs again through sudo
#   as-user  the caller's own files: as root (sudo, or `cst status`), the
#            script runs again as SUDO_USER through runuser
class Privilege(StrEnum):
    """How a routed command runs (the privileges above)."""

    READ = "read"
    USER = "user"
    ROOT = "root"
    AS_USER = "as-user"


class RouteAction(StrEnum):
    """What to do with a command line (Routed.action)."""

    HELP = "help"
    USAGE = "usage"
    UNKNOWN = "unknown"
    RUN = "run"


@dataclass(frozen=True, slots=True)
class _Route:
    """How one command runs. root_with: an argument after the command that makes it a root command."""

    privilege: Privilege
    root_with: str = ""


@dataclass(frozen=True, slots=True)
class _CommandRoutes:
    """A command's route, or its subcommands' (then usage is the error for any other; {prog} is filled in)."""

    route: _Route | None = None
    subcommands: Mapping[str, _Route] | None = None
    usage: str = ""


@dataclass(frozen=True, slots=True)
class ModuleRouting:
    """A shim module's routing table.

    program: the program, relative to the CST folder. container_override:
    the variable that says "1" (in a toolbox) or "0" (on the host) instead
    of the detection (tests). user_reason: why an as-user command refuses
    root without a SUDO_USER. user_unset: what the step down to SUDO_USER
    removes from the environment besides SUDO_USER.
    """

    program: str
    commands: Mapping[str, _CommandRoutes]
    container_override: str = ""
    user_reason: str = ""
    user_unset: tuple[str, ...] = ()


_READ: Final = _Route(Privilege.READ)
_ROOT: Final = _Route(Privilege.ROOT)
_AS_USER: Final = _CommandRoutes(_Route(Privilege.AS_USER))

ROUTING: Final[Mapping[str, ModuleRouting]] = {
    "vm": ModuleRouting(
        program="vm/cst-vm",
        commands={
            "list": _CommandRoutes(_READ),
            "status": _CommandRoutes(_READ),
            "tune": _CommandRoutes(
                subcommands={"plan": _READ, "apply": _ROOT},
                usage="Usage: {prog} tune plan|apply [VM] [--guest-has-virtio]",
            ),
            "nocow": _CommandRoutes(
                subcommands={"plan": _Route(Privilege.READ, root_with="--root"), "apply": _Route(Privilege.USER)},
                usage="Usage: {prog} nocow plan [--root] | nocow apply",
            ),
            "backup-timer": _CommandRoutes(
                subcommands={"status": _READ, "on": _ROOT, "off": _ROOT},
                usage="Usage: {prog} backup-timer on|off|status [VM]",
            ),
            "backup": _CommandRoutes(_ROOT),
        },
        container_override="CST_VM_CONTAINER",
    ),
    "toolchains": ModuleRouting(
        program="toolchains/cst-toolchains",
        commands={"status": _AS_USER, "prune": _AS_USER, "auto": _AS_USER, "config": _AS_USER},
        user_reason="toolchain caches and settings belong to your user",
        user_unset=("XDG_CONFIG_HOME",),
    ),
}


@dataclass(frozen=True, slots=True)
class Routed:
    """What to do with a command line: help (exit 0), a usage error (message, exit 2), or run with privilege."""

    action: RouteAction
    privilege: Privilege = Privilege.READ
    message: str = ""


def route_command(routing: ModuleRouting, cmd: str, rest: Sequence[str], prog: str) -> Routed:
    """Where cmd (an empty one is help) and its arguments go, as the setup script decided."""
    if cmd in HELP_WORDS:
        return Routed(RouteAction.HELP)
    found = routing.commands.get(cmd)
    if found is None:
        return Routed(RouteAction.UNKNOWN, message=f"Unknown command: {cmd}")
    route = found.route
    if found.subcommands is not None:
        route = found.subcommands.get(rest[0] if rest else "")
        if route is None:
            return Routed(RouteAction.USAGE, message=found.usage.format(prog=prog))
    assert route is not None, cmd
    if route.root_with and route.root_with in rest:
        return Routed(RouteAction.RUN, Privilege.ROOT)
    return Routed(RouteAction.RUN, route.privilege)


def step_down_argv(user: str, uid: int, home: str, *, unset: Sequence[str], then: Sequence[str]) -> list[str]:
    """runuser to user (from root) with the session variables a login of that user sets; env removes
    unset and SUDO_USER first, then takes then (more assignments, and the command)."""
    drop = [word for name in (*unset, "SUDO_USER") for word in ("-u", name)]
    session = [
        f"HOME={home}",
        f"USER={user}",
        f"LOGNAME={user}",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
    ]
    return ["runuser", "-u", user, "--", "env", *drop, *session, *then]


def script_help(script_text: str, prog: str, *, words: str = "[args]") -> str:
    """A setup script's help: its header from line 2 to the first empty line (the license
    line left out, "# " taken off), then the usage line ("Usage: PROG <command> WORDS"). What
    `sed -n '2,/^$/{/SPDX-License-Identifier:/!p;}' | sed 's/^# \\{0,1\\}//'` printed."""
    out: list[str] = []
    lines = script_text.split("\n")
    if script_text.endswith("\n"):
        lines.pop()  # the newline ends the last line; it starts no empty one
    for number, line in enumerate(lines, start=1):
        if number < 2:
            continue
        if "SPDX-License-Identifier:" not in line:
            out.append(line[2:] if line.startswith("# ") else line.removeprefix("#"))
        if number > 2 and not line:
            break
    return "\n".join(out) + f"\nUsage: {prog} <command> {words}\n"

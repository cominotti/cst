# SPDX-License-Identifier: MPL-2.0
"""The rules of `cst cpu` (cpu-setup.sh): which commands it has, the files and lines it writes, how it
reads what it shows. No I/O.

The commands that change the machine run as root on the host (cpu/setup/adapters.py); watch,
report and quiet are cpu-watch's (as the caller, on the host also from a toolbox).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
import re
from typing import Final, Literal

from cst_core.domain.text import file_lines

type Route = Literal["help", "watch", "root", "unknown"]

# The commands cpu-watch answers (as the caller), and the ones that need root.
WATCH_COMMANDS: Final = frozenset({"watch", "report", "quiet"})
ROOT_COMMANDS: Final = frozenset({"install", "status", "uninstall", "game", "vm-pin", "docker-slice", "experiment"})
# quiet's limits a toolbox hands to the host (flatpak-spawn passes no environment on).
QUIET_ENV: Final = ("QUIET_CPU_PCT", "QUIET_CPU_PSI", "QUIET_IO_PSI", "QUIET_MEMORY_PSI", "QUIET_WRITE_MBPS")

# Baseline drop-ins start with 10-: systemd reads drop-ins in file-name order (the last one read
# wins), and the game profile's runtime overrides from `systemctl set-property` are named
# 50-<Property>.conf. A 50-cpu-setup.conf sorted after them ("50-CPUWeight" < "50-cpu-setup": upper
# case first), so every daemon-reload put the baseline back over the running profile.
DROPIN_NAME: Final = "10-cpu-setup.conf"
OLD_DROPIN_NAME: Final = "50-cpu-setup.conf"  # until 2026-11; install and uninstall remove it
MANAGED_MARK: Final = "Managed by cpu-setup.sh"

# Runtime-only memory experiments, in the order `experiment list` shows them: name -> (file, value).
EXPERIMENTS: Final[Mapping[str, tuple[str, str]]] = {
    "minfree-256m": ("/proc/sys/vm/min_free_kbytes", "262144"),
    "wsf-300": ("/proc/sys/vm/watermark_scale_factor", "300"),
    "thp-defer": ("/sys/kernel/mm/transparent_hugepage/defrag", "defer"),
}


def route(cmd: str) -> Route:
    """Where a command goes (an empty one, or none, is help)."""
    if cmd in ("", "help", "-h", "--help"):
        return "help"
    if cmd in WATCH_COMMANDS:
        return "watch"
    return "root" if cmd in ROOT_COMMANDS else "unknown"


@dataclass(frozen=True, slots=True)
class SetupPaths:
    """Where `cst cpu` reads and writes on the host (tests point them into a folder of their own)."""

    conf: str = "/etc/cpu-setup/cpu-setup.conf"
    unit_dir: str = "/etc/systemd/system"
    daemon_json: str = "/etc/docker/daemon.json"
    # intel_pstate: raise the frequency at once when a task wakes up after an I/O wait (builds with
    # many fsyncs, a game loading assets). Applied at boot by systemd-tmpfiles, and at once by install.
    hwp_boost: str = "/sys/devices/system/cpu/intel_pstate/hwp_dynamic_boost"
    hwp_tmpfiles: str = "/etc/tmpfiles.d/cpu-setup-hwp-boost.conf"
    e_cores: str = "/sys/devices/cpu_atom/cpus"
    unit_runtime: str = "/run/systemd/system"
    run: str = "/run/cpu-setup"
    cgroot: str = "/sys/fs/cgroup"
    sched_ext: str = "/sys/kernel/sched_ext/state"
    preempt: str = "/sys/kernel/debug/sched/preempt"
    etc_group: str = "/etc/group"
    lib_group: str = "/usr/lib/group"
    etc_gshadow: str = "/etc/gshadow"
    experiments: Mapping[str, tuple[str, str]] = field(default_factory=lambda: dict(EXPERIMENTS))

    @property
    def machine_dropin(self) -> str:
        """The VM's baseline weights."""
        return f"{self.unit_dir}/machine.slice.d/{DROPIN_NAME}"

    @property
    def delegate_dropin(self) -> str:
        """The cpuset delegation to the user manager."""
        return f"{self.unit_dir}/user@.service.d/50-cpu-setup-cpuset.conf"

    @property
    def builds_cgroup(self) -> str:
        """builds.slice's cgroup folder (Docker containers in it: docker-*.scope)."""
        return f"{self.cgroot}/builds.slice"

    def saved(self, experiment: str) -> str:
        """Where an experiment keeps the value it replaced."""
        return f"{self.run}/exp-{experiment}"


def toolbox_dropin(home: str, name: str = DROPIN_NAME) -> str:
    """The toolboxes' drop-in (the inner user.slice of the user manager)."""
    return f"{home}/.config/systemd/user/user.slice.d/{name}"


def helper_dropin(home: str, name: str = DROPIN_NAME) -> str:
    """The drop-in of flatpak-session-helper (what toolboxes run on the host)."""
    return f"{home}/.config/systemd/user/flatpak-session-helper.service.d/{name}"


def gamemode_ini(home: str) -> str:
    """The user's GameMode settings."""
    return f"{home}/.config/gamemode.ini"


def target_user_in(conf_text: str) -> str:
    """TARGET_USER as cpu-setup.sh read it from cpu-setup.conf
    (`sed -n "s/^TARGET_USER='\\{0,1\\}\\([^']*\\)'\\{0,1\\}$/\\1/p"`): each such line's value, one per line."""
    found = [m.group(1) for line in conf_text.split("\n") if (m := re.fullmatch(r"TARGET_USER='?([^']*)'?", line))]
    return "\n".join(found)


def conf_lines(target_user: str) -> str:
    """cpu-setup.conf as install writes it the first time."""
    return "".join(
        line + "\n"
        for line in (
            "# Managed by cpu-setup.sh (CST); values may be edited.",
            f"TARGET_USER='{target_user}'",
            "BUILD_CPUS='12-23'",
            "P_CORES='0-7'",
            "VM_WEIGHT=10",
            "GAME_MEMORY_LOW='16G'",
            "GAME_ZSWAP_WRITEBACK='no'",
            "VM_NAME='win11-devbox'",
            "VM_VCPUS=12",
            "VM_CPU_DISABLE='cet-ss cet-ibt vmx-mbec vmx-exit-save-cet vmx-entry-load-cet'",
        )
    )


def hwp_tmpfiles_text(hwp_boost: str) -> str:
    """The tmpfiles.d line that sets HWP dynamic boost at boot."""
    return f"# Managed by cpu-setup.sh (CST): intel_pstate HWP dynamic boost on.\nw {hwp_boost} - - - - 1\n"


def toolbox_dropin_text(e_cores: str) -> str:
    """The toolboxes' drop-in: on the E-cores (when there are), weighing like background.slice."""
    if not e_cores:
        return "# Managed by cpu-setup.sh (CST).\n[Slice]\nCPUWeight=30\n"
    return (
        "# Managed by cpu-setup.sh (CST): toolbox containers (inner user.slice of the\n"
        "# user manager, sibling of app.slice) on the E-cores, weighing like background.slice.\n"
        f"[Slice]\nCPUWeight=30\nAllowedCPUs={e_cores}\n"
    )


def helper_dropin_text(e_cores: str) -> str:
    """flatpak-session-helper's drop-in: the toolboxes' host commands on the E-cores too."""
    return (
        "# Managed by cpu-setup.sh (CST): host commands of toolboxes and Flatpak apps\n"
        "# (flatpak-spawn --host) on the E-cores, like the toolboxes themselves.\n"
        f"[Service]\nAllowedCPUs={e_cores}\n"
    )


def install_done_lines(prog: str) -> list[str]:
    """What install says at the end."""
    return [
        "",
        "Done. Still to do by you:",
        "  - log out and in once (gamemode group; cpuset delegation, which makes the",
        "    toolboxes' E-core restriction effective);",
        "  - Steam > game > Properties > Launch options: gamemoderun %command%",
        f"  - optional, confirmed separately: {prog} vm-pin plan   and   {prog} docker-slice install",
        f"  - measure: {prog} watch -l baseline --game dota2 --seconds 300   (while a build runs)",
    ]


def uninstall_kept_lines(prog: str, conf: str) -> list[str]:
    """What uninstall leaves, and how to undo the rest."""
    return [
        "Kept: ~/.local/bin/cst (the cst command; the other modules use it too)",
        f"Kept: {conf}, the 'gamemode' group membership, builds.slice/daemon.json",
        f"('{prog} docker-slice uninstall') and the VM definition ('{prog} vm-pin status';",
        "the previous one is in /etc/cpu-setup/*.xml.before-vm-pin).",
    ]


def preempt_modes(text: str) -> str:
    """The preemption mode debugfs shows in parentheses (one per line, as sed printed them)."""
    return "\n".join(m.group(1) for line in text.split("\n") if (m := re.fullmatch(r".*\(([a-z]*)\).*", line)))


def first_four_and_last(text: str) -> str:
    """`sed -n '1,4p;$p'`: lines 1 to 4 and the last one (twice when it is one of the first four)."""
    lines = file_lines(text)
    return "".join(line + "\n" for line in lines[:4] + lines[-1:])


def indented(text: str) -> str:
    """`sed 's/^/  /'`: each line two blanks in."""
    return "".join(f"  {line}\n" for line in file_lines(text))


def last_lines(text: str, n: int) -> str:
    """`tail -n N`."""
    return "".join(line + "\n" for line in file_lines(text)[-n:]) if n else ""


def current_choice(text: str) -> str:
    """What an experiment saves of its file (`sed -n 's/.*\\[\\(.*\\)\\].*/\\1/p;t;p'`): the [current] choice
    of a sysfs list (THP), else the line itself; a last line without a newline stays so."""
    parts = text.split("\n")
    for i, line in enumerate(parts[:-1] if text.endswith("\n") else parts):
        m = re.fullmatch(r".*\[(.*)\].*", line)
        if m:
            parts[i] = m.group(1)
    return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class JsonEdit:
    """What docker-slice does with daemon.json: the new content (None: write nothing), and the line it
    prints (on stdout, or on stderr with exit status 1 when error)."""

    content: Mapping[str, object] | None
    message: str
    error: bool = False


def with_builds_parent(path: str, current: Mapping[str, object]) -> JsonEdit:
    """daemon.json with "cgroup-parent": "builds.slice" (another parent already set is left alone)."""
    parent = current.get("cgroup-parent")
    if parent == "builds.slice":
        return JsonEdit(None, f"unchanged: {path}")
    if parent:
        return JsonEdit(None, f"{path} already sets cgroup-parent={parent!r}: left alone", error=True)
    return JsonEdit({**current, "cgroup-parent": "builds.slice"}, f"written:   {path} (cgroup-parent: builds.slice)")


def json_text(content: Mapping[str, object], *, newline: bool) -> str:
    """json.dump(content, indent=2), with a newline after it or not (as each writer did)."""
    return json.dumps(content, indent=2) + ("\n" if newline else "")


def cgroup_parent_shown(current: object) -> str:
    """What docker-slice status shows of daemon.json's cgroup-parent (print() of it, "-" without)."""
    if not isinstance(current, dict):
        raise TypeError("daemon.json holds no object")
    value = current.get("cgroup-parent", "-")
    return value if isinstance(value, str) else str(value)


def experiment_line(name: str, file: str, value: str, now: str, *, on: bool) -> str:
    """One line of `experiment list` (printf '  %-14s %s -> %-8s now: %s%s')."""
    return f"  {name:<14} {file} -> {value:<8} now: {now}{'  (ON)' if on else ''}"


def docker_slice_status(active: str, parent: str, containers: int) -> str:
    """The line of `docker-slice status`."""
    return f"builds.slice: {active}; daemon.json cgroup-parent: {parent}; containers in it: {containers}"


def first_arg(args: Sequence[str], default: str) -> str:
    """${1:-default}: the first argument, or default when there is none or it is empty."""
    return args[0] if args and args[0] else default


class Halt(Exception):
    """The command ends here with this exit status, after its message on stderr (if any): where
    cpu-setup.sh exited, or stopped under set -e at a command that failed."""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.message = message

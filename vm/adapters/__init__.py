# SPDX-License-Identifier: MPL-2.0
"""The vm module's reach into the host: virsh, rpm, rpm-ostree, systemctl and
restic, and the settings files. vm.adapters.backup has the backups' files
and the run of a backup, vm.adapters.timer the weekly timer's files,
vm.adapters.images the VM image folders.

Every path below can be moved by its CST_VM_* variable, for the tests only
(scratch folders instead of the host's).
"""

from collections.abc import Mapping, Sequence
import json
import os
import sys
from typing import Any

from cpu.domain import CPU_DISABLE_DEFAULT
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.virsh import SubprocessVirshAdapter
from cst_core.adapters.files import (
    read_text,
)
from cst_core.adapters.notify import PENDING_ALERTS
from cst_core.adapters.runtime import restore_default_signals
from cst_core.domain.contracts import NOTIFY_PROGRAM, state_format
from cst_core.domain.errors import Refused
from cst_core.domain.install import manifest_by_src
from cst_core.domain.record import ROOT_RECORD
from cst_core.ports import CommandResult, CommandRunnerPort, VirshPort
from cst_core.domain.text import (
    ShellAssignments,
    describe_problems,
    first_line,
    parse_shell_assignments,
    parse_snapshots_conf,
)
from vm.domain import (
    DEFAULT_VM,
    KNOBS,
    NAS_DEFAULTS,
    NOT_INSTALLED,
    URI,
    VIRT_PACKAGES,
)


NAME = "cst-vm"


PROG = os.environ.get("CST_PROG") or NAME


CST = PROG[: -len(" vm")] if PROG.endswith(" vm") else "cst"


CPU_CONF = os.environ.get("CST_VM_CPU_CONF") or "/etc/cpu-setup/cpu-setup.conf"  # override: tests


# Overridable for the tests only (scratch folders instead of the host's).
BACKUP_DIR = os.environ.get("CST_VM_BACKUP_DIR") or "/var/lib/libvirt/cst-backups"


INDEX_FORMAT = state_format("vm-index")


INDEX = os.environ.get("CST_VM_INDEX") or INDEX_FORMAT.path


SWTPM_DIR = os.environ.get("CST_VM_SWTPM_DIR") or "/var/lib/libvirt/swtpm"


# Settings of this module (optional file; every knob defaults to yes).
VM_CONF = os.environ.get("CST_VM_CONF") or "/etc/cst/vm.conf"  # override: tests


# VM image folders. The system one is libvirt's default storage pool.
SYSTEM_IMAGES = os.environ.get("CST_VM_SYSTEM_IMAGES") or "/var/lib/libvirt/images"  # override: tests


# The NAS backup reuses the restic repository, password and cache of the
# /home NAS backup (btrfs/btrfs-snapshot: NAS_SSH_ALIAS, RESTIC_PASSWORD,
# RESTIC_CACHE; a test checks they stay the same). The password never leaves
# its root-only file: restic reads it through RESTIC_PASSWORD_FILE.
NAS_CONF = os.environ.get("CST_VM_NAS_CONF") or "/etc/btrfs-setup/snapshots.conf"  # override: tests


RESTIC_PASSWORD = os.environ.get("CST_VM_RESTIC_PASSWORD") or "/etc/btrfs-setup/restic-password"


RESTIC_CACHE = os.environ.get("CST_VM_RESTIC_CACHE") or "/var/cache/btrfs-snapshot-restic"


# The definition and the manifest, written here for restic to read: a fixed
# path per VM, so restic's parent snapshot and forget's groups stay the same.
NAS_STAGE = os.environ.get("CST_VM_NAS_STAGE") or "/var/lib/cst/vm-nas"


RUN_DIR = os.environ.get("CST_VM_RUN_DIR") or "/run/cst-vm"  # per-VM lock (holds the pid)


NOTIFY = os.environ.get("CST_VM_NOTIFY") or NOTIFY_PROGRAM


# Where an alert btrfs-notify could not take is kept for cst doctor and the next login.
ALERTS = os.environ.get("CST_VM_ALERTS") or PENDING_ALERTS  # override: tests


# The merge back (every blockcommit of a run together; it copies the guest's
# writes of the backup's duration). The timer's TimeoutStopSec leaves room
# for it after a stop.
COMMIT_TIMEOUT = int(os.environ.get("CST_VM_COMMIT_TIMEOUT") or 4 * 3600)


# virsh snapshot-create-as; after a timeout the run waits up to
# SNAPSHOT_SETTLE seconds for libvirt to finish (or not) on its own.
SNAPSHOT_TIMEOUT = int(os.environ.get("CST_VM_SNAPSHOT_TIMEOUT") or 600)


SNAPSHOT_SETTLE = int(os.environ.get("CST_VM_SNAPSHOT_SETTLE") or 600)


# The weekly timer (backup-timer).
UNIT_DIR = os.environ.get("CST_VM_UNIT_DIR") or "/etc/systemd/system"  # override: tests


SBIN_COPY = os.environ.get("CST_VM_SBIN_COPY") or manifest_by_src()["vm/cst-vm"].dest  # as `install` places it


# The install record that lists what backup-timer on writes (override: tests).
INSTALL_RECORD = os.environ.get("CST_VM_INSTALL_RECORD") or ROOT_RECORD


TIMER_COMPONENT = manifest_by_src()["vm/cst-vm"].component


# True during the merge back of a NAS backup: every child then runs in its own
# session, so a Ctrl-C or a closed terminal cannot reach it (virsh cancels a
# `blockcommit --wait` on SIGINT).
DETACH = {"on": False}


# The commands cst-vm runs (where it runs: not moved to the host from a
# toolbox), and virsh over the same runner. Bytes of their output that are
# not UTF-8 become U+FFFD.
RUNNER: CommandRunnerPort = SubprocessCommandRunner()


VIRSH: VirshPort = SubprocessVirshAdapter(URI, RUNNER, on_host=False)


def _triple(r: CommandResult, name: str, timeout: float) -> tuple[int, str, str]:
    """(rc, stdout, stderr) of r with cst-vm's own words for a command that is
    missing or does not answer; one that cannot start raises (as subprocess.run did)."""
    if r.fault == "missing":
        return 127, "", f"{name}: not found"
    if r.fault == "timeout":
        return 124, "", f"{name}: no answer within {timeout} s"
    if r.cause is not None:
        raise r.cause
    return r.returncode, r.stdout, r.stderr


def run(
    cmd: Sequence[str],
    timeout: int = 120,
    extra_env: Mapping[str, str] | None = None,
    detach: bool | None = None,
) -> tuple[int, str, str]:
    """(rc, stdout, stderr); rc 127 when the command does not exist. In the
    C locale (virsh translates its answers: "shut off" is "desligado" in
    pt_BR); extra_env on top. Bytes that are not UTF-8 become U+FFFD."""
    r = RUNNER.run(
        cmd,
        timeout=timeout,
        env=extra_env,
        locale_c=True,
        new_session=DETACH["on"] if detach is None else detach,
    )
    return _triple(r, cmd[0], timeout)


def run_on_terminal(cmd: Sequence[str]) -> int:
    """Exit status of cmd in the C locale, on our terminal (stdin and output:
    sudo may ask for the password). A command that cannot start raises (as
    subprocess.run did)."""
    r = RUNNER.run(cmd, locale_c=True, stdin="inherit", output="inherit")
    if r.cause is not None:
        raise r.cause
    return r.returncode


def virsh(*args: str, timeout: int = 60, detach: bool | None = None) -> tuple[int, str, str]:
    """virsh -c URI ARGS through VIRSH, with run()'s answers."""
    r = VIRSH.call(*args, timeout=timeout, start_new_session=DETACH["on"] if detach is None else detach)
    return _triple(r, "virsh", timeout)


def read_conf(path: str, *, bash_sourced: bool = False) -> dict[str, str]:
    """The settings of a KEY=VALUE file ({} when it cannot be read), through
    cst_core's one reader (parse_shell_assignments); the last line of a key
    wins. bash_sourced: cpu-setup.conf and snapshots.conf, which bash sources
    too, read exactly as bash reads them; vm.conf (only Python reads it) also
    accepts blanks around = (`VM_NOCOW = 0`), as cst-vm always did. A line
    that is not an assignment sets nothing (prepare_command reports it).
    Undecodable bytes are replaced (main's cst-vm stopped with a traceback)."""
    return dict(settings_file(path, bash_sourced=bash_sourced).values)


def settings_file(path: str, *, bash_sourced: bool) -> ShellAssignments:
    """A settings file through cst_core's one reader ("" when it cannot be read).
    snapshots.conf (NAS_CONF) also takes the lines an older CST wrote there with
    printf %q, read as bash reads them (parse_snapshots_conf)."""
    text = read_text(path, "")
    if path == NAS_CONF:
        return parse_snapshots_conf(text)
    return parse_shell_assignments(text, bash_sourced=bash_sourced)


def conf_files(cmd: str, nas: bool, vm: str | None) -> list[tuple[str, bool]]:
    """[(settings file, bash_sourced)] that a cst-vm command reads; vm: the VM named on the command line."""
    files = [(VM_CONF, False)]
    # cpu-setup.conf: VM_NAME when no VM is named, VM_CPU_DISABLE for status and tune.
    # A backup (or backup-timer) of a named VM reads neither: a line there must not stop it.
    if cmd != "nocow" and not (vm and cmd in ("backup", "backup-timer")):
        files.append((CPU_CONF, True))
    if nas or cmd == "backup-timer":
        files.append((NAS_CONF, True))
    return files


def prepare_command(cmd: str, sub: str | None, nas: bool, vm: str | None) -> None:
    """Before a cst-vm command runs (help: only the SIGPIPE default below). Problem lines of the settings files it
    reads: a command that changes the machine with them (backup, tune/nocow
    apply, backup-timer on) refuses to start, since a default must never
    stand in for a line that says something else; the others print them on
    stderr and go on (backup-timer off too: it reads no setting, and it is
    the way back). A command that only reads (status, list, the plans,
    backup-timer status, help) also gets SIGPIPE back at its default, so `| head`
    ends it quietly; commands that change the machine keep Python's handling
    (backup: SafeStream), so a closed pipe never cuts their cleanup short."""
    if cmd == "help":
        restore_default_signals()
        return
    found = []
    for path, bash_sourced in conf_files(cmd, nas, vm):
        problems = settings_file(path, bash_sourced=bash_sourced).problems
        if problems:
            found.append(describe_problems(path, problems))
    changes = cmd == "backup" or sub in ("apply", "on", "off")
    if found and changes and sub != "off":
        raise Refused("\n".join(found) + "\nFix these lines first; nothing was changed.")
    if found:
        print("\n".join(found), file=sys.stderr)
    if not changes:
        restore_default_signals()


def conf_number(key: str, path: str | None = None) -> int:
    """A whole number of /etc/cst/vm.conf (NAS_DEFAULTS when missing or not a number >= 0)."""
    v = read_conf(path or VM_CONF).get(key, "")
    return int(v) if v.isdigit() else NAS_DEFAULTS[key]


def default_vm() -> str:
    return read_conf(CPU_CONF, bash_sourced=True).get("VM_NAME") or DEFAULT_VM


def cpu_disable_list(conf_path: str | None = None) -> list[str]:
    """The features vm-pin keeps disabled: VM_CPU_DISABLE from cpu-setup.conf
    when set there (also empty), else vm-pin's default (cpu.domain)."""
    conf = read_conf(conf_path or CPU_CONF, bash_sourced=True)
    if "VM_CPU_DISABLE" in conf:
        return conf["VM_CPU_DISABLE"].split()
    return CPU_DISABLE_DEFAULT.split()


def package_versions() -> dict[str, str]:
    """{package: name-version-release.arch or "not installed"}, one rpm -q each
    (its exit code tells whether it is installed)."""
    out: dict[str, str] = {}
    for p in VIRT_PACKAGES:
        rc, so, _ = run(["rpm", "-q", "--qf", "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\\n", p], timeout=30)
        out[p] = first_line(so) if rc == 0 and first_line(so) else NOT_INSTALLED
    return out


def booted_deployment() -> dict[str, Any] | None:
    """{version, checksum, timestamp} of the booted rpm-ostree deployment, or None."""
    rc, so, _ = run(["rpm-ostree", "status", "--json"], timeout=60)
    if rc != 0:
        return None
    try:
        deps = json.loads(so).get("deployments") or []
        d = next(x for x in deps if isinstance(x, dict) and x.get("booted"))
    except ValueError, StopIteration, AttributeError:
        return None
    return {"version": d.get("version"), "checksum": d.get("checksum"), "timestamp": d.get("timestamp")}


def restic(nas: Mapping[str, Any], *args: str, timeout: int = 1800) -> tuple[int, str, str]:
    return run(["restic", *args], timeout=timeout, extra_env=nas["env"])


def blklist(vm: str, inactive: bool = False) -> tuple[dict[str, str] | None, str]:
    """({target: source}, problem) from virsh domblklist --details."""
    rc, so, err = virsh("domblklist", vm, "--details", *(["--inactive"] if inactive else []))
    if rc != 0:
        return None, first_line(err) or f"exit {rc}"
    out: dict[str, str] = {}
    for ln in so.splitlines():
        parts = ln.split(None, 3)
        if len(parts) == 4 and parts[0] != "Type" and not parts[0].startswith("-"):
            out[parts[2]] = parts[3].strip()
    return out, ""


def domain_state_or_none(vm: str) -> str | None:
    rc, so, _ = virsh("domstate", vm)
    return first_line(so) if rc == 0 else None


def systemctl(*args: str) -> tuple[int, str, str]:
    rc, so, err = run(["systemctl", *args], timeout=120)
    return rc, so.strip(), first_line(err)


def knobs(path: str | None = None) -> dict[str, bool]:
    """{knob: bool} from /etc/cst/vm.conf; a missing file or knob is yes."""
    conf = read_conf(path or VM_CONF)
    return {k: conf.get(k, "yes").strip().lower() not in ("no", "0", "false", "off") for k in KNOBS}


def domain_state(vm: str) -> str:
    rc, so, err = virsh("domstate", vm)
    if rc != 0:
        raise Refused(f"virsh domstate {vm}: {first_line(err) or f'exit {rc}'}")
    return first_line(so)


def has_managed_save(vm: str) -> bool:
    rc, so, err = virsh("list", "--all", "--with-managed-save", "--name")
    if rc != 0:
        raise Refused(f"virsh list --with-managed-save: {first_line(err) or f'exit {rc}'}")
    return vm in (ln.strip() for ln in so.splitlines())

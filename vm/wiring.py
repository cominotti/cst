# SPDX-License-Identifier: MPL-2.0
"""The composition root of `cst vm`: the environment, turned into the settings of one run.

vm/cli parses argv, takes the settings from here and runs one command.
Every path can be moved by its CST_VM_* variable, for the tests only
(scratch folders instead of the host's); cst doctor reads the same ones.
"""

from collections.abc import Mapping
import os

from cst_core.adapters.notify import PENDING_ALERTS
from cst_core.domain.contracts import NOTIFY_PROGRAM, state_format
from cst_core.domain.install import manifest_by_src
from cst_core.domain.record import ROOT_RECORD
from cst_core.adapters import SystemClockAdapter, SystemctlAdapter
from cst_core.adapters.terminal import confirm
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.ports import SystemdPort
from cst_core.ports import ClockPort
from vm.adapters import COMMANDS_OF_VM, NAME, VmConfFiles, effective_root, fault_words, prepare_command, run
from vm.adapters.backup import BackupFiles, DefinitionFiles, StopSignals, TerminalLines, VmLock, notify, safe_output
from vm.adapters.images import ImageFolders
from vm.adapters.restic import Restic
from vm.adapters.timer import TimerFiles
from vm.application.backup import LocalBackup
from vm.application.nas import NasBackup
from vm.domain import VmArgs, VmPaths, VmSettings
from vm.ports import (
    BackupStorePort,
    DefinitionStorePort,
    ImagesPort,
    ProgressPort,
    ResticPort,
    SettingsPort,
    TimerFilesPort,
    VmCommandsPort,
)


def vm_env() -> Mapping[str, str]:
    """This process's environment, read when asked (tests pass their own)."""
    return os.environ


def vm_paths(env: Mapping[str, str]) -> VmPaths:
    """The machine's paths, each moved by its CST_VM_* variable when it is set (tests)."""
    rows = (  # (VmPaths field, its variable, the machine's path)
        ("backup_dir", "CST_VM_BACKUP_DIR", "/var/lib/libvirt/cst-backups"),
        ("index", "CST_VM_INDEX", state_format("vm-index").path),
        ("swtpm_dir", "CST_VM_SWTPM_DIR", "/var/lib/libvirt/swtpm"),
        ("vm_conf", "CST_VM_CONF", "/etc/cst/vm.conf"),
        ("cpu_conf", "CST_VM_CPU_CONF", "/etc/cpu-setup/cpu-setup.conf"),
        ("system_images", "CST_VM_SYSTEM_IMAGES", "/var/lib/libvirt/images"),
        # The NAS backup reuses the restic repository, password and cache of the /home NAS backup
        # (btrfs/btrfs-snapshot: NAS_SSH_ALIAS, RESTIC_PASSWORD, RESTIC_CACHE; a test checks they
        # stay the same). The password never leaves its root-only file.
        ("nas_conf", "CST_VM_NAS_CONF", "/etc/btrfs-setup/snapshots.conf"),
        ("restic_password", "CST_VM_RESTIC_PASSWORD", "/etc/btrfs-setup/restic-password"),
        ("restic_cache", "CST_VM_RESTIC_CACHE", "/var/cache/btrfs-snapshot-restic"),
        # A fixed path per VM, so restic's parent snapshot and forget's groups stay the same.
        ("nas_stage", "CST_VM_NAS_STAGE", "/var/lib/cst/vm-nas"),
        ("run_dir", "CST_VM_RUN_DIR", "/run/cst-vm"),
        ("notify", "CST_VM_NOTIFY", NOTIFY_PROGRAM),
        ("alerts", "CST_VM_ALERTS", PENDING_ALERTS),
        ("unit_dir", "CST_VM_UNIT_DIR", "/etc/systemd/system"),
        ("sbin_copy", "CST_VM_SBIN_COPY", manifest_by_src()["vm/cst-vm"].dest),
        ("install_record", "CST_VM_INSTALL_RECORD", ROOT_RECORD),
    )
    return VmPaths(**{field: env.get(var) or default for field, var, default in rows})


def vm_settings(env: Mapping[str, str] | None = None) -> VmSettings:
    """The settings of this run (env: this process's environment unless a test passes one).
    A timeout that is not a whole number stops the program (ValueError), as it always did."""
    e = vm_env() if env is None else env
    prog = e.get("CST_PROG") or NAME
    return VmSettings(
        paths=vm_paths(e),
        prog=prog,
        cst=prog[: -len(" vm")] if prog.endswith(" vm") else "cst",
        nonroot_ok=bool(e.get("CST_VM_NONROOT_OK")),
        under_systemd=bool(e.get("INVOCATION_ID")),
        now=e.get("CST_VM_NOW") or None,
        sudo_user=e.get("SUDO_USER") or None,
        commit_timeout=int(e.get("CST_VM_COMMIT_TIMEOUT") or 4 * 3600),
        snapshot_timeout=int(e.get("CST_VM_SNAPSHOT_TIMEOUT") or 600),
        snapshot_settle=int(e.get("CST_VM_SNAPSHOT_SETTLE") or 600),
    )


def timer_port(cfg: VmSettings, entry_file: str) -> TimerFilesPort:
    """The weekly timer's files on this machine (entry_file: the running cst-vm)."""
    return TimerFiles(cfg, entry_file)


def systemd() -> SystemdPort:
    """systemctl as cst-vm runs it: in the C locale, 120 s at most, with its own words for a
    systemctl that is missing or does not answer (fault_words)."""
    return SystemctlAdapter(SubprocessCommandRunner(words=fault_words), timeout=120, locale_c=True)


def vm_commands() -> VmCommandsPort:
    """virsh (and the other commands) of this process."""
    return COMMANDS_OF_VM


def definition_store(cfg: VmSettings) -> DefinitionStorePort:
    """The definitions tune keeps under the backup folder."""
    return DefinitionFiles(cfg.paths.backup_dir)


def restic() -> ResticPort:
    """restic as cst-vm runs it (vm.adapters.run: the C locale, its words for a missing restic)."""
    return Restic(run)


def progress() -> ProgressPort:
    """What a backup prints, as it goes: this process's stdout and stderr."""
    return TerminalLines()


def backup_store(cfg: VmSettings) -> BackupStorePort:
    """The backups' files on this machine (cfg's paths)."""
    return BackupFiles(cfg)


def local_backup(cfg: VmSettings) -> LocalBackup:
    """What `cst vm backup` reaches on this machine."""
    return LocalBackup(
        commands=vm_commands(),
        store=backup_store(cfg),
        clock=clock(),
        out=progress(),
        root=effective_root(),
    )


def settings_port(cfg: VmSettings) -> SettingsPort:
    """The settings files cst vm reads (cfg's paths)."""
    return VmConfFiles(cfg)


def nas_backup(cfg: VmSettings) -> NasBackup:
    """What `cst vm backup --nas` reaches on this machine."""
    return NasBackup(
        commands=vm_commands(),
        store=backup_store(cfg),
        settings=settings_port(cfg),
        restic=restic(),
        signals=StopSignals(),
        lock=lambda vm: VmLock(vm, cfg.paths.run_dir),
        alert=lambda title, body: notify(cfg, "critical", title, body),
        clock=clock(),
        out=progress(),
        root=effective_root(),
    )


def running_as_root() -> bool:
    """This process runs as root (asked each time: the tests change it)."""
    return effective_root()


def images(cfg: VmSettings) -> ImagesPort:
    """The VM image folders on this machine (cfg's paths, the user's home)."""
    return ImageFolders(cfg)


def clock() -> ClockPort:
    """This machine's clock."""
    return SystemClockAdapter()


def prepare(cfg: VmSettings, a: VmArgs) -> None:
    """Before a command runs: the problem lines of the settings files it reads (refused for a
    command that changes the machine) and SIGPIPE for the ones that only read
    (vm.adapters.prepare_command)."""
    prepare_command(cfg.paths, a.cmd, a.sub, a.nas, a.vm)


def safe_terminal() -> None:
    """From here on a closed terminal or pipe never cuts a backup or its cleanup short."""
    safe_output()


def failure_alert(cfg: VmSettings, title: str, body: str) -> None:
    """A desktop alert (normal urgency) for a weekly run that failed: nobody reads its terminal."""
    notify(cfg, "normal", title, body)


def ask(question: str) -> bool:
    """The question on the terminal (Refused without one)."""
    return confirm(question)

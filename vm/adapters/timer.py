# SPDX-License-Identifier: MPL-2.0
"""The weekly NAS backup timer's files: the program copy and the two units, through the installer
and the install record, and whether each is CST's, current, missing or masked."""

from collections.abc import Callable, Mapping, Sequence
import functools
import os

from cst_core.adapters.files import is_masked_unit, owned_file_state_at, unit_file_state_at
from cst_core.adapters.install import program_installer
from cst_core.adapters.runtime import install_payload
from cst_core.application.install import InstallService, Placement
from cst_core.domain.files import UnitFileState
from cst_core.domain.install import manifest_by_src
from vm.adapters import TIMER_COMPONENT
from vm.domain import (
    SERVICE,
    TIMER,
    UNIT_MARKER,
    TimerStep,
    VmPaths,
    VmSettings,
    is_cst_vm_program,
    render_service,
    render_timer,
)


def is_ours(sbin_copy: str, path: str, data: bytes) -> bool:
    """Whether the file at PATH with these bytes is CST's: a unit with CST's
    marker, or (at sbin_copy) a copy of cst-vm from any CST version."""
    return UNIT_MARKER.encode() in data or path == sbin_copy and is_cst_vm_program(data)


def wants_links(unit_dir: str) -> list[str]:
    d = os.path.join(unit_dir, "timers.target.wants")
    try:
        return sorted(n for n in os.listdir(d) if n.startswith("cst-vm-backup@") and n.endswith(".timer"))
    except OSError:
        return []


def stop_timeout(s: VmSettings) -> str:
    """A stop may arrive while the snapshot call is held (snapshot_timeout +
    snapshot_settle), then the merge runs (commit_timeout, plus virsh's own
    300 s), then the checks and the alert: 30 minutes of margin."""
    return f"{(s.snapshot_timeout + s.snapshot_settle + s.commit_timeout + 300 + 1800 + 59) // 60}min"


def timer_files(s: VmSettings, entry_file: str) -> dict[str, tuple[bytes, int]]:
    """{path: (wanted bytes, mode)}: the program as `install` writes it (a
    zipapp; entry_file is the running cst-vm), the service and the timer."""
    p = s.paths
    return {
        p.sbin_copy: (install_payload("vm/cst-vm", entry_file), 0o755),
        os.path.join(p.unit_dir, SERVICE): (render_service(p.sbin_copy, stop_timeout(s)).encode(), 0o644),
        os.path.join(p.unit_dir, TIMER): (render_timer().encode(), 0o644),
    }


def timer_installer(entry_file: str) -> InstallService:
    """The installer's use case for backup-timer's files: written only when they changed, and listed in
    the install record (paths.install_record) under the component vm:backup-timer."""
    return program_installer(entry_file)


def install_timer_files(
    paths: VmPaths,
    files: Mapping[str, tuple[bytes, int]],
    states: Mapping[str, UnitFileState],
    entry_file: str,
    report: Callable[[TimerStep], None],
) -> None:
    """Write (through the installer) each of backup-timer's files that is not current, and report it.
    Its own refusals (a foreign or masked file) come first, in vm.application.timer_on."""
    svc = timer_installer(entry_file)
    as_root = os.geteuid() == 0
    program_entry = manifest_by_src()["vm/cst-vm"]
    for p, (text, mode) in files.items():
        program = p == paths.sbin_copy
        place = Placement(
            path=p,
            dest=p,
            record=paths.install_record,
            component=TIMER_COMPONENT,
            src=program_entry.src if program else None,
            kind=program_entry.kind if program else "text",
            owner="root",
            mode=mode,
        )
        if svc.put(place, text, as_root=as_root):
            report(TimerStep("installed" if states[p] == "missing" else "updated", p))


def timer_record_done(paths: VmPaths, entry_file: str) -> None:
    """backup-timer on finished: the record says vm:backup-timer is complete."""
    timer_installer(entry_file).mark(paths.install_record, TIMER_COMPONENT)


def forget_timer_files(paths: VmPaths, entry_file: str, removed: Sequence[str]) -> None:
    """backup-timer off removed these files: the record no longer lists them."""
    timer_installer(entry_file).forget(paths.install_record, removed)


def timer_file_states(
    paths: VmPaths, files: Mapping[str, tuple[bytes, int]], instance_timer: str
) -> dict[str, UnitFileState]:
    """{path: state} of what backup-timer on writes (timer_files): the program by
    the installer's link rule, the two units also "masked" (systemctl mask: a
    link to /dev/null). instance_timer, the VM's own timer (cst-vm-backup@VM.timer
    in unit_dir, which `systemctl mask` of that one VM creates), is added only
    when it is masked: CST never writes it."""
    states: dict[str, UnitFileState] = {
        p: (owned_file_state_at if p == paths.sbin_copy else unit_file_state_at)(
            p, t, functools.partial(is_ours, paths.sbin_copy, p), owner="root"
        )
        for p, (t, _) in files.items()
    }
    if is_masked_unit(instance_timer):
        states[instance_timer] = "masked"
    return states


class TimerFiles:
    """TimerFilesPort on this machine: the files of timer_files (built once, from entry_file, the
    running cst-vm), written through the installer, and the timers.target.wants links."""

    def __init__(self, cfg: VmSettings, entry_file: str) -> None:
        self.cfg = cfg
        self.entry_file = entry_file
        self._files: dict[str, tuple[bytes, int]] | None = None

    def files(self) -> dict[str, tuple[bytes, int]]:
        if self._files is None:
            self._files = timer_files(self.cfg, self.entry_file)
        return self._files

    def file_states(self, unit: str) -> dict[str, UnitFileState]:
        return timer_file_states(self.cfg.paths, self.files(), os.path.join(self.cfg.paths.unit_dir, unit))

    def install(self, states: Mapping[str, UnitFileState], report: Callable[[TimerStep], None]) -> None:
        install_timer_files(self.cfg.paths, self.files(), states, self.entry_file, report)

    def mark_complete(self) -> None:
        timer_record_done(self.cfg.paths, self.entry_file)

    def remove(self, path: str) -> None:
        os.unlink(path)

    def forget(self, removed: Sequence[str]) -> None:
        forget_timer_files(self.cfg.paths, self.entry_file, removed)

    def timers_on(self) -> list[str]:
        return wants_links(self.cfg.paths.unit_dir)

# SPDX-License-Identifier: MPL-2.0
"""Host/toolbox filesystem and systemd adapters for `cst btrfs scrub-limit`."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import contextlib
from dataclasses import dataclass
import glob
import os
from typing import Final

from btrfs.scrub_limit.domain import (
    SCRUB_CONF_PATH,
    SCRUB_DROPIN_PATH,
    SCRUB_SERVICE,
    ScrubStatusSnapshot,
    render_scrub_dropin,
    scrub_limit_plan_lines,
    scrub_limit_status_lines,
    update_snapshots_conf_scrub_limit,
)
from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import ensure_dir, read_text, write_atomic
from cst_core.adapters.install import JsonInstallRecord
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import (
    host_view_path,
    installer_script_for,
    run_installer,
    undo_python_start,
)
from cst_core.domain.install import Component, InstallReportWord
from cst_core.domain.record import ROOT_RECORD, ComponentState, RecordFormatError
from cst_core.domain.text import YES_WORDS
from cst_core.ports import CommandRunnerPort, ExecutionContextPort

_SYSFS_DEVINFO_DEPTH: Final = 4
_SHORT_UUID_LEN: Final = 8
_BTRFS_SYSFS: Final = "/sys/fs/btrfs"


@dataclass(frozen=True, slots=True)
class ScrubPaths:
    """Resolved host paths and installer script location for `btrfs-scrub-limit`."""

    conf_path: str = SCRUB_CONF_PATH
    dropin_path: str = SCRUB_DROPIN_PATH
    btrfs_sysfs: str = _BTRFS_SYSFS
    installer_script: str = ""
    record_path: str = ROOT_RECORD  # where the installer records btrfs:core (and the drop-in)


def start_scrub_process() -> None:
    """Restore default SIGPIPE/SIGXFSZ and caller LC_CTYPE."""
    undo_python_start()


def scrub_paths_for(
    entry_file: str,
    *,
    ctx: ExecutionContextPort | None = None,
    read_only: bool = True,
    fallback_root: str = "",
) -> ScrubPaths:
    """Build `ScrubPaths` mapped to `/run/host` inside containers for read-only views."""
    exec_ctx = HostExecutionContext() if ctx is None else ctx
    installer = installer_script_for(entry_file, fallback_root)
    if read_only:
        return ScrubPaths(
            conf_path=host_view_path(exec_ctx, SCRUB_CONF_PATH),
            dropin_path=host_view_path(exec_ctx, SCRUB_DROPIN_PATH),
            btrfs_sysfs=host_view_path(exec_ctx, _BTRFS_SYSFS),
            installer_script=installer,
        )
    return ScrubPaths(
        conf_path=SCRUB_CONF_PATH,
        dropin_path=SCRUB_DROPIN_PATH,
        btrfs_sysfs=_BTRFS_SYSFS,
        installer_script=installer,
    )


def _read_optional_strict(path: str) -> tuple[str | None, bool]:
    """Read UTF-8 file text preserving line ends; return `(None, False)` if absent, `(None, True)` if unreadable."""
    try:
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as fh:
            return fh.read(), False
    except FileNotFoundError:
        return None, False
    except OSError:
        return None, True


def _live_scrub_sysfs(btrfs_sysfs: str) -> list[str]:
    pattern = os.path.join(glob.escape(btrfs_sysfs), "*", "devinfo", "*", "scrub_speed_max")
    out: list[str] = []
    for path in sorted(glob.glob(pattern)):
        raw = read_text(path)
        if raw is None:
            continue
        parts = path.split("/")
        if len(parts) >= _SYSFS_DEVINFO_DEPTH:
            uuid = parts[-_SYSFS_DEVINFO_DEPTH][:_SHORT_UUID_LEN]
            devid = parts[-2]
            out.append(f"{uuid} device {devid} {raw.strip()}")
    return out


def scrub_limit_status(paths: ScrubPaths) -> tuple[int, list[str]]:
    """Read-only status of `SCRUB_LIMIT`, drop-in, and sysfs."""
    conf_text, conf_unreadable = _read_optional_strict(paths.conf_path)
    dropin_text, _ = _read_optional_strict(paths.dropin_path)
    lines = scrub_limit_status_lines(
        ScrubStatusSnapshot(
            conf_path=paths.conf_path,
            conf_text=conf_text,
            conf_unreadable=conf_unreadable,
            dropin_path=paths.dropin_path,
            dropin_text=dropin_text,
            live_sysfs=tuple(_live_scrub_sysfs(paths.btrfs_sysfs)),
        )
    )
    return (1 if conf_unreadable else 0), lines


def scrub_limit_plan(paths: ScrubPaths, target_limit: str | None) -> tuple[int, list[str]]:
    """Read-only dry run of `cst btrfs scrub-limit set|off`."""
    conf_text, conf_unreadable = _read_optional_strict(paths.conf_path)
    if conf_unreadable:
        return 1, [f"ERROR: cannot read {paths.conf_path}"]
    dropin_text, dropin_unreadable = _read_optional_strict(paths.dropin_path)
    if dropin_unreadable:
        return 1, [f"ERROR: cannot read {paths.dropin_path}"]
    return 0, scrub_limit_plan_lines(
        conf_path=paths.conf_path,
        conf_text=conf_text,
        dropin_path=paths.dropin_path,
        dropin_text=dropin_text,
        target_limit=target_limit,
    )


def _write_conf(path: str, text: str) -> None:
    ensure_dir(os.path.dirname(path), 0o755)
    write_atomic(path, text, mode=0o644, durable=True, relabel=True)


def _restore_conf(paths: ScrubPaths, old_text: str | None, *, made_dir: bool) -> str | None:
    """snapshots.conf as it was before this run (removed when there was none); what failed, or None."""
    try:
        if old_text is not None:
            _write_conf(paths.conf_path, old_text)
            return None
        with contextlib.suppress(FileNotFoundError):
            os.remove(paths.conf_path)
        if made_dir:
            with contextlib.suppress(OSError):
                os.rmdir(os.path.dirname(paths.conf_path))
    except OSError as exc:
        return f"{paths.conf_path}: {exc.strerror or exc}"
    return None


def _write_dropin(runner: CommandRunnerPort, paths: ScrubPaths, text: str | None) -> tuple[int, list[str], str]:
    """The drop-in through the installer: written with text, or removed (None)."""
    if text is None:
        return run_installer(runner, paths.installer_script, ["remove-text", paths.dropin_path])
    return run_installer(
        runner,
        paths.installer_script,
        ["text", paths.dropin_path, "0644", "--component", Component.BTRFS_CORE.value],
        input_text=text,
    )


def _restore_dropin(runner: CommandRunnerPort, paths: ScrubPaths, old_text: str | None) -> str | None:
    rc, _lines, err = _write_dropin(runner, paths, old_text)
    if rc != 0:
        return f"{paths.dropin_path}: {err or f'installer exit {rc}'}"
    if old_text is None:
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(paths.dropin_path))
    return None


def _rolled_back(cause: str, failures: list[str | None], *, reload_hint: bool) -> str:
    left = [f for f in failures if f is not None]
    if left:
        return f"ERROR: {cause}; ROLLBACK INCOMPLETE ({'; '.join(left)})"
    hint = " (run systemctl daemon-reload once it works)" if reload_hint else ""
    return f"ERROR: {cause}; previous configuration restored.{hint}"


def _core_done(record_path: str) -> bool:
    """Whether the install record has btrfs:core done (no record, or one that cannot be read: no)."""
    try:
        rec = JsonInstallRecord().read(record_path)
    except RecordFormatError, OSError:
        return False
    return rec is not None and rec.components.get(Component.BTRFS_CORE.value) == ComponentState.DONE


def _mark_core_done(runner: CommandRunnerPort, paths: ScrubPaths) -> tuple[int, str]:
    """btrfs:core done again in the install record: (the installer's exit status, what failed or "")."""
    rc, _lines, err = run_installer(
        runner, paths.installer_script, ["mark", "--component", Component.BTRFS_CORE.value, "done"]
    )
    return rc, (err or f"exit {rc}") if rc != 0 else ""


def _read_conf_and_dropin(paths: ScrubPaths) -> tuple[str | None, str | None, str | None]:
    """snapshots.conf's and the drop-in's text as they are now, or the ERROR line of the first that
    cannot be read: (conf text, drop-in text, error)."""
    conf_text, conf_unreadable = _read_optional_strict(paths.conf_path)
    if conf_unreadable:
        return None, None, f"ERROR: cannot read {paths.conf_path}"
    dropin_before, dropin_unreadable = _read_optional_strict(paths.dropin_path)
    if dropin_unreadable:
        return None, None, f"ERROR: cannot read {paths.dropin_path}"
    return conf_text, dropin_before, None


def _apply_conf(paths: ScrubPaths, conf_text: str | None, target_limit: str | None) -> tuple[bool, bool, str]:
    """Write SCRUB_LIMIT into snapshots.conf when it changes: (changed, its folder was absent, the line).

    A write that fails (a full disk, say) leaves the file as it was, removes the folder again when this
    run made it, and raises the OSError."""
    new_conf = update_snapshots_conf_scrub_limit(conf_text, target_limit)
    conf_changed = new_conf is not None and new_conf != conf_text
    made_dir = not os.path.isdir(os.path.dirname(paths.conf_path))
    if new_conf is not None and conf_changed:
        try:
            _write_conf(paths.conf_path, new_conf)
        except OSError:
            if made_dir:  # write_atomic removed its temporary file: the folder is empty again
                with contextlib.suppress(OSError):
                    os.rmdir(os.path.dirname(paths.conf_path))
            raise
        verb = "written:" if conf_text is None else "updated:"
        note = f"SCRUB_LIMIT='{target_limit}'" if target_limit is not None else "removed SCRUB_LIMIT"
        return conf_changed, made_dir, f"{verb:<10} {paths.conf_path} ({note})"
    suffix = " (absent)" if conf_text is None else ""
    return conf_changed, made_dir, f"unchanged: {paths.conf_path}{suffix}"


def _reload_or_roll_back(
    cmd_runner: CommandRunnerPort,
    paths: ScrubPaths,
    restore_conf: Callable[[], str | None],
    dropin_before: str | None,
    *,
    core_done: bool,
) -> str | None:
    """systemctl daemon-reload; when it fails, snapshots.conf and the drop-in go back (and btrfs:core is
    marked done again) and the ERROR line comes back."""
    dr = SystemctlAdapter(cmd_runner, on_host=SystemctlHost.LOCAL).daemon_reload_result()
    if dr.returncode == 0:
        return None
    failures = [restore_conf(), _restore_dropin(cmd_runner, paths, dropin_before)]
    if core_done and failures[-1] is None:
        # Both writes of the drop-in (this run's and the rollback's) set btrfs:core pending.
        rc_m, err_m = _mark_core_done(cmd_runner, paths)
        failures.append(f"btrfs:core not marked done again: {err_m}" if rc_m != 0 else None)
    return _rolled_back(dr.failure("systemctl daemon-reload"), failures, reload_hint=True)


def _systemd_behind(cmd_runner: CommandRunnerPort) -> bool:
    """Whether systemd has not loaded the scrub unit's files as they are on disk (NeedDaemonReload: an
    earlier run wrote them and stopped before its daemon-reload); also when systemctl cannot say."""
    r = SystemctlAdapter(cmd_runner, on_host=SystemctlHost.LOCAL).show_value(SCRUB_SERVICE, "NeedDaemonReload")
    return r.returncode != 0 or r.stdout.strip().lower() in YES_WORDS


def _reload_what_is_on_disk(cmd_runner: CommandRunnerPort) -> str | None:
    """daemon-reload when only systemd's view was behind (this run changed no file: nothing to roll back);
    the ERROR line when it fails."""
    dr = SystemctlAdapter(cmd_runner, on_host=SystemctlHost.LOCAL).daemon_reload_result()
    if dr.returncode == 0:
        return None
    return (
        f"ERROR: {dr.failure('systemctl daemon-reload')}; snapshots.conf and the drop-in are as asked, "
        "systemd has not loaded them (run systemctl daemon-reload once it works)"
    )


def _load_in_systemd(
    cmd_runner: CommandRunnerPort,
    inst_lines: Sequence[str],
    reload_or_roll_back: Callable[[], str | None],
    *,
    conf_changed: bool,
) -> tuple[str | None, list[str]]:
    """daemon-reload when this run changed the drop-in, or when systemd has not loaded it as it is on
    disk (an earlier run stopped before its daemon-reload): (the ERROR line or None, the lines to add)."""
    if any(line.startswith((InstallReportWord.WRITTEN, InstallReportWord.REMOVED)) for line in inst_lines):
        return reload_or_roll_back(), []
    if not _systemd_behind(cmd_runner):
        return None, []
    # This run changed no file but snapshots.conf, or none at all: then there is nothing to roll back.
    error = reload_or_roll_back() if conf_changed else _reload_what_is_on_disk(cmd_runner)
    return error, [f"reloaded:  systemd had not loaded {SCRUB_SERVICE} as it is on disk (an earlier run stopped)"]


def _mark_core_after_write(
    cmd_runner: CommandRunnerPort, paths: ScrubPaths, inst_lines: Sequence[str], *, core_done: bool
) -> tuple[int, str | None]:
    """btrfs:core done again after the installer wrote the drop-in: (exit status, the ERROR line or None)."""
    if not (core_done and any(line.startswith(InstallReportWord.WRITTEN) for line in inst_lines)):
        return 0, None
    rc_m, err_m = _mark_core_done(cmd_runner, paths)
    if rc_m != 0:
        return rc_m, f"ERROR: the installer could not mark btrfs:core done: {err_m}"
    return 0, None


def scrub_limit_apply(
    paths: ScrubPaths,
    target_limit: str | None,
    *,
    runner: CommandRunnerPort | None = None,
) -> tuple[int, list[str]]:
    """Update `SCRUB_LIMIT` in `snapshots.conf`, update/remove `10-cst-limit.conf`, and reload systemd if needed.

    When the installer or daemon-reload fails, snapshots.conf and the drop-in go back to what they were."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    conf_text, dropin_before, read_error = _read_conf_and_dropin(paths)
    if read_error is not None:
        return 1, [read_error]
    # Writing the drop-in sets btrfs:core pending; a component install had finished stays done.
    core_done = _core_done(paths.record_path)
    try:
        conf_changed, made_dir, conf_line = _apply_conf(paths, conf_text, target_limit)
    except OSError as exc:  # nothing written yet: nothing to roll back
        return 1, [f"ERROR: cannot write {paths.conf_path}: {exc.strerror or exc}"]
    out: list[str] = [conf_line]

    def restore_conf() -> str | None:
        return _restore_conf(paths, conf_text, made_dir=made_dir) if conf_changed else None

    rc_i, inst_lines, err_i = _write_dropin(
        cmd_runner, paths, render_scrub_dropin(target_limit) if target_limit is not None else None
    )
    out.extend(inst_lines)
    if rc_i != 0:
        cause = f"the installer failed: {err_i or f'exit {rc_i}'}"
        return rc_i, [*out, _rolled_back(cause, [restore_conf()], reload_hint=False)]

    if target_limit is None:
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(paths.dropin_path))

    def reload_or_roll_back() -> str | None:
        return _reload_or_roll_back(cmd_runner, paths, restore_conf, dropin_before, core_done=core_done)

    error, note = _load_in_systemd(cmd_runner, inst_lines, reload_or_roll_back, conf_changed=conf_changed)
    if error is not None:
        return 1, [*out, error]
    out.extend(note)
    rc_m, mark_error = _mark_core_after_write(cmd_runner, paths, inst_lines, core_done=core_done)
    if mark_error is not None:
        return rc_m, [*out, mark_error]
    return 0, out

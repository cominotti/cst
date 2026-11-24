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
    scrub_limit_plan_reload_lines,
    scrub_limit_status_lines,
    update_snapshots_conf_scrub_limit,
)
from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import ensure_dir, read_text, write_atomic
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import (
    host_view_path,
    installer_script_for,
    run_installer,
    undo_python_start,
)
from cst_core.domain.install import Component, InstallReportWord
from cst_core.domain.text import YES_WORDS
from cst_core.domain.values import MINUTE
from cst_core.ports import CommandRunnerPort, ExecutionContextPort, UnitName

_SYSFS_DEVINFO_DEPTH: Final = 4
_SHORT_UUID_LEN: Final = 8
_BTRFS_SYSFS: Final = "/sys/fs/btrfs"
# The longest a daemon-reload this run's own write did not ask for may take (systemd was behind, or
# could not say): a PID 1 that does not answer must not hang a run with nothing to change.
_CATCH_UP_RELOAD_TIMEOUT: Final = MINUTE


@dataclass(frozen=True, slots=True)
class ScrubPaths:
    """Resolved host paths and installer script location for `btrfs-scrub-limit`."""

    conf_path: str = SCRUB_CONF_PATH
    dropin_path: str = SCRUB_DROPIN_PATH
    btrfs_sysfs: str = _BTRFS_SYSFS
    installer_script: str = ""


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


def _need_daemon_reload(cmd_runner: CommandRunnerPort, *, on_host: SystemctlHost) -> bool | None:
    """NeedDaemonReload of the scrub unit, through SystemctlAdapter.show (the C locale, and its time limit:
    a PID 1 that does not answer must not hang a run with nothing to change): True when systemd has not
    loaded its files as they are on disk (an earlier run wrote them and stopped before its daemon-reload);
    None when systemctl cannot say (it fails, or does not answer in time)."""
    props = SystemctlAdapter(cmd_runner, on_host=on_host).show(UnitName(SCRUB_SERVICE), ["NeedDaemonReload"])
    value = props.get("NeedDaemonReload")
    if value is None:
        return None
    return value.strip().lower() in YES_WORDS


def scrub_limit_status(paths: ScrubPaths, *, runner: CommandRunnerPort | None = None) -> tuple[int, list[str]]:
    """Read-only status of `SCRUB_LIMIT`, drop-in, sysfs, and whether systemd loaded the drop-in."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
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
            need_reload=_need_daemon_reload(cmd_runner, on_host=SystemctlHost.SHOW_ONLY),  # from a toolbox too
        )
    )
    return (1 if conf_unreadable else 0), lines


def scrub_limit_plan(
    paths: ScrubPaths, target_limit: str | None, *, runner: CommandRunnerPort | None = None
) -> tuple[int, list[str]]:
    """Read-only dry run of `cst btrfs scrub-limit set|off`, the daemon-reload included that apply runs
    when systemd has not loaded the drop-in as it is on disk."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    conf_text, conf_unreadable = _read_optional_strict(paths.conf_path)
    if conf_unreadable:
        return 1, [f"ERROR: cannot read {paths.conf_path}"]
    dropin_text, dropin_unreadable = _read_optional_strict(paths.dropin_path)
    if dropin_unreadable:
        return 1, [f"ERROR: cannot read {paths.dropin_path}"]
    lines = scrub_limit_plan_lines(
        conf_path=paths.conf_path,
        conf_text=conf_text,
        dropin_path=paths.dropin_path,
        dropin_text=dropin_text,
        target_limit=target_limit,
    )
    need_reload = _need_daemon_reload(cmd_runner, on_host=SystemctlHost.SHOW_ONLY)  # from a toolbox too
    return 0, [
        *lines,
        *scrub_limit_plan_reload_lines(dropin_text=dropin_text, target_limit=target_limit, need_reload=need_reload),
    ]


def _remove_if_empty(folder: str) -> None:
    """Remove folder when it is empty; one that is not, or is not there, stays as it is."""
    with contextlib.suppress(OSError):
        os.rmdir(folder)


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
            _remove_if_empty(os.path.dirname(paths.conf_path))
    except OSError as exc:
        return f"{paths.conf_path}: {exc.strerror or exc}"
    return None


def _write_dropin(runner: CommandRunnerPort, paths: ScrubPaths, text: str | None) -> tuple[int, list[str], str]:
    """The drop-in through the installer: written with text, or removed (None). Its record line is written
    or dropped and btrfs:core keeps the state it has in the install record (text --keep-state): this command
    is not btrfs:core's setup command, so no end of its run (killed, failed, rolled back) changes that state."""
    if text is None:
        return run_installer(runner, paths.installer_script, ["remove-text", paths.dropin_path])
    return run_installer(
        runner,
        paths.installer_script,
        ["text", paths.dropin_path, "0644", "--component", Component.BTRFS_CORE.value, "--keep-state"],
        input_text=text,
    )


def _restore_dropin(runner: CommandRunnerPort, paths: ScrubPaths, old_text: str | None) -> str | None:
    rc, _lines, err = _write_dropin(runner, paths, old_text)
    if rc != 0:
        return f"{paths.dropin_path}: {err or f'installer exit {rc}'}"
    if old_text is None:
        _remove_if_empty(os.path.dirname(paths.dropin_path))
    return None


def _rolled_back(cause: str, failures: list[str | None], *, reload_hint: bool) -> str:
    left = [f for f in failures if f is not None]
    if left:
        return f"ERROR: {cause}; ROLLBACK INCOMPLETE ({'; '.join(left)})"
    hint = " (run systemctl daemon-reload once it works)" if reload_hint else ""
    return f"ERROR: {cause}; previous configuration restored.{hint}"


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


def _conf_write_failed(paths: ScrubPaths, conf_text: str | None, exc: OSError, *, made_dir: bool) -> str:
    """The ERROR line of a write of snapshots.conf that failed. A failure before write_atomic's rename (a
    full disk) left the file as it was; one after it (the folder's fsync) left the new text there, which
    goes back then. The folder this run made is removed again either way."""
    cause = f"cannot write {paths.conf_path}: {exc.strerror or exc}"
    now, unreadable = _read_optional_strict(paths.conf_path)
    if not unreadable and now == conf_text:
        if made_dir:  # write_atomic removed its temporary file: the folder is empty again
            _remove_if_empty(os.path.dirname(paths.conf_path))
        return f"ERROR: {cause}"
    return _rolled_back(cause, [_restore_conf(paths, conf_text, made_dir=made_dir)], reload_hint=False)


def _apply_conf(
    paths: ScrubPaths, conf_text: str | None, target_limit: str | None
) -> tuple[bool, bool, str, str | None]:
    """Write SCRUB_LIMIT into snapshots.conf when it changes: (changed, its folder was absent, the line,
    the ERROR line of a write that failed, with snapshots.conf as it was before)."""
    new_conf = update_snapshots_conf_scrub_limit(conf_text, target_limit)
    conf_changed = new_conf is not None and new_conf != conf_text
    made_dir = not os.path.isdir(os.path.dirname(paths.conf_path))
    if new_conf is not None and conf_changed:
        try:
            _write_conf(paths.conf_path, new_conf)
        except OSError as exc:
            return conf_changed, made_dir, "", _conf_write_failed(paths, conf_text, exc, made_dir=made_dir)
        verb = "written:" if conf_text is None else "updated:"
        note = f"SCRUB_LIMIT='{target_limit}'" if target_limit is not None else "removed SCRUB_LIMIT"
        return conf_changed, made_dir, f"{verb:<10} {paths.conf_path} ({note})", None
    suffix = " (absent)" if conf_text is None else ""
    return conf_changed, made_dir, f"unchanged: {paths.conf_path}{suffix}", None


def _reload_or_roll_back(
    cmd_runner: CommandRunnerPort,
    restore_files: Callable[[], list[str | None]],
    *,
    timeout: float | None,
) -> str | None:
    """systemctl daemon-reload (at most timeout seconds; None: no limit); when it fails, snapshots.conf and
    the drop-in go back (restore_files: what failed of each, the drop-in last) and the ERROR line comes
    back."""
    dr = SystemctlAdapter(cmd_runner, timeout=timeout, on_host=SystemctlHost.LOCAL).daemon_reload_result()
    if dr.returncode == 0:
        return None
    return _rolled_back(dr.failure("systemctl daemon-reload"), restore_files(), reload_hint=True)


def _reload_what_is_on_disk(cmd_runner: CommandRunnerPort, *, behind: bool | None) -> str | None:
    """daemon-reload when only systemd's view was behind, or systemctl could not say (behind None; this
    run changed no file: nothing to roll back), at most _CATCH_UP_RELOAD_TIMEOUT; the ERROR line when it
    fails."""
    dr = SystemctlAdapter(
        cmd_runner, timeout=_CATCH_UP_RELOAD_TIMEOUT, on_host=SystemctlHost.LOCAL
    ).daemon_reload_result()
    if dr.returncode == 0:
        return None
    loaded = "systemd has not loaded them" if behind is True else "systemctl cannot say whether systemd has loaded them"
    return (
        f"ERROR: {dr.failure('systemctl daemon-reload')}; snapshots.conf and the drop-in are as asked, "
        f"{loaded} (run systemctl daemon-reload once it works)"
    )


def _load_in_systemd(
    cmd_runner: CommandRunnerPort,
    inst_lines: Sequence[str],
    reload_or_roll_back: Callable[[float | None], str | None],
    *,
    conf_changed: bool,
) -> tuple[str | None, list[str]]:
    """daemon-reload when this run changed the drop-in, or when systemd has not loaded it as it is on
    disk (an earlier run stopped before its daemon-reload), or systemctl cannot say (exit 0 must mean
    systemd loaded the limit): (the ERROR line or None, the lines to add)."""
    if any(line.startswith((InstallReportWord.WRITTEN, InstallReportWord.REMOVED)) for line in inst_lines):
        return reload_or_roll_back(None), []
    behind = _need_daemon_reload(cmd_runner, on_host=SystemctlHost.LOCAL)
    if behind is False:
        return None, []
    # This run changed no file but snapshots.conf, or none at all: then there is nothing to roll back.
    if conf_changed:
        error = reload_or_roll_back(_CATCH_UP_RELOAD_TIMEOUT)
    else:
        error = _reload_what_is_on_disk(cmd_runner, behind=behind)
    why = (
        f"systemd had not loaded {SCRUB_SERVICE} as it is on disk (an earlier run stopped)"
        if behind is True
        else f"systemctl could not say whether systemd had loaded {SCRUB_SERVICE} as it is on disk"
    )
    return error, [f"reloaded:  {why}"]


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
    conf_changed, made_dir, conf_line, write_error = _apply_conf(paths, conf_text, target_limit)
    if write_error is not None:  # snapshots.conf as it was (or ROLLBACK INCOMPLETE), the drop-in not touched
        return 1, [write_error]
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
        _remove_if_empty(os.path.dirname(paths.dropin_path))

    def restore_files() -> list[str | None]:
        return [restore_conf(), _restore_dropin(cmd_runner, paths, dropin_before)]

    def reload_or_roll_back(timeout: float | None) -> str | None:
        return _reload_or_roll_back(cmd_runner, restore_files, timeout=timeout)

    error, note = _load_in_systemd(cmd_runner, inst_lines, reload_or_roll_back, conf_changed=conf_changed)
    if error is not None:
        return 1, [*out, error]
    out.extend(note)
    return 0, out

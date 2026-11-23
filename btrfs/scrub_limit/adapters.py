# SPDX-License-Identifier: MPL-2.0
"""Host/toolbox filesystem and systemd adapters for `cst btrfs scrub-limit`."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import glob
import os
from typing import Final

from btrfs.scrub_limit.domain import (
    SCRUB_CONF_PATH,
    SCRUB_DROPIN_PATH,
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
from cst_core.domain.install import Component
from cst_core.domain.record import ROOT_RECORD, ComponentState, RecordFormatError
from cst_core.ports import CommandResult, CommandRunnerPort, ExecutionContextPort

_SYSFS_DEVINFO_DEPTH: Final = 4
_SHORT_UUID_LEN: Final = 8


@dataclass(frozen=True, slots=True)
class ScrubPaths:
    """Resolved host paths and installer script location for `btrfs-scrub-limit`."""

    conf_path: str = SCRUB_CONF_PATH
    dropin_path: str = SCRUB_DROPIN_PATH
    btrfs_sysfs: str = "/sys/fs/btrfs"
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
            btrfs_sysfs=host_view_path(exec_ctx, "/sys/fs/btrfs"),
            installer_script=installer,
        )
    return ScrubPaths(
        conf_path=SCRUB_CONF_PATH,
        dropin_path=SCRUB_DROPIN_PATH,
        btrfs_sysfs="/sys/fs/btrfs",
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


def _failure(what: str, result: CommandResult) -> str:
    return f"{what} failed: {result.stderr.strip() or f'exit {result.returncode}'}"


def scrub_limit_apply(
    paths: ScrubPaths,
    target_limit: str | None,
    *,
    runner: CommandRunnerPort | None = None,
) -> tuple[int, list[str]]:
    """Update `SCRUB_LIMIT` in `snapshots.conf`, update/remove `10-cst-limit.conf`, and reload systemd if needed.

    When the installer or daemon-reload fails, snapshots.conf and the drop-in go back to what they were."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    conf_text, conf_unreadable = _read_optional_strict(paths.conf_path)
    if conf_unreadable:
        return 1, [f"ERROR: cannot read {paths.conf_path}"]
    dropin_before, dropin_unreadable = _read_optional_strict(paths.dropin_path)
    if dropin_unreadable:
        return 1, [f"ERROR: cannot read {paths.dropin_path}"]
    # Writing the drop-in sets btrfs:core pending; a component install had finished stays done.
    core_done = _core_done(paths.record_path)
    out: list[str] = []
    new_conf = update_snapshots_conf_scrub_limit(conf_text, target_limit)
    conf_changed = new_conf is not None and new_conf != conf_text
    made_dir = not os.path.isdir(os.path.dirname(paths.conf_path))
    if new_conf is not None and conf_changed:
        _write_conf(paths.conf_path, new_conf)
        verb = "written:" if conf_text is None else "updated:"
        note = f"SCRUB_LIMIT='{target_limit}'" if target_limit is not None else "removed SCRUB_LIMIT"
        out.append(f"{verb:<10} {paths.conf_path} ({note})")
    else:
        suffix = " (absent)" if conf_text is None else ""
        out.append(f"unchanged: {paths.conf_path}{suffix}")

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
    if any(line.startswith(("written:", "removed:")) for line in inst_lines):
        dr = SystemctlAdapter(cmd_runner, on_host=SystemctlHost.LOCAL).daemon_reload_result()
        if dr.returncode != 0:
            failures = [restore_conf(), _restore_dropin(cmd_runner, paths, dropin_before)]
            return 1, [*out, _rolled_back(_failure("systemctl daemon-reload", dr), failures, reload_hint=True)]
    if core_done and any(line.startswith("written:") for line in inst_lines):
        rc_m, _lines, err_m = run_installer(
            cmd_runner, paths.installer_script, ["mark", "--component", Component.BTRFS_CORE.value, "done"]
        )
        if rc_m != 0:
            return rc_m, [*out, f"ERROR: the installer could not mark btrfs:core done: {err_m or f'exit {rc_m}'}"]
    return 0, out

# SPDX-License-Identifier: MPL-2.0
"""Host/toolbox filesystem and systemd adapters for `cst btrfs scrub-limit`."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import glob
import os
from typing import Final, NoReturn

from btrfs.scrub_limit.domain import (
    SCRUB_CONF_PATH,
    SCRUB_DROPIN_PATH,
    ScrubLimitAction,
    ScrubStatusSnapshot,
    render_scrub_dropin,
    scrub_limit_plan_lines,
    scrub_limit_status_lines,
    update_snapshots_conf_scrub_limit,
)
from cst_core.adapters.files import ensure_dir, read_text, write_atomic
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import (
    SYSTEM_PYTHON,
    RootElevation,
    checkout_root,
    enforce_host_root,
    host_view_path,
    lc_ctype_handover,
    undo_python_start,
)
from cst_core.domain.install import Component
from cst_core.ports import CommandRunnerPort, ExecutionContextPort, OutputMode

_SYSFS_DEVINFO_DEPTH: Final = 4
_SHORT_UUID_LEN: Final = 8


@dataclass(frozen=True, slots=True)
class ScrubPaths:
    """Resolved host paths and installer script location for `btrfs-scrub-limit`."""

    conf_path: str = SCRUB_CONF_PATH
    dropin_path: str = SCRUB_DROPIN_PATH
    btrfs_sysfs: str = "/sys/fs/btrfs"
    installer_script: str = ""


@dataclass(frozen=True, slots=True)
class ScrubInvocation:
    """Caller identity and command arguments for elevation checks."""

    prog: str
    args: tuple[str, ...]
    entry_file: str


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
    root = checkout_root(entry_file) or fallback_root
    installer = os.path.join(root, "cst_core", "installer.py") if root else ""
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


def enforce_scrub_host_root(
    action: ScrubLimitAction,
    invocation: ScrubInvocation,
    *,
    ctx: ExecutionContextPort | None = None,
    elevate: Callable[[list[str]], NoReturn] | None = None,
) -> str | None:
    """Enforce host execution and root privilege for mutating `set`/`off` commands."""
    if action not in (ScrubLimitAction.SET, ScrubLimitAction.OFF):
        return None
    return enforce_host_root(
        RootElevation(
            prog=invocation.prog,
            action=action.value,
            args=invocation.args,
            entry_file=invocation.entry_file,
            reason="writes /etc/btrfs-setup/snapshots.conf and systemd drop-in",
        ),
        ctx=ctx,
        elevate=elevate,
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


def scrub_limit_apply(
    paths: ScrubPaths,
    target_limit: str | None,
    *,
    runner: CommandRunnerPort | None = None,
) -> tuple[int, list[str]]:
    """Update `SCRUB_LIMIT` in `snapshots.conf`, update/remove `10-cst-limit.conf`, and reload systemd if needed."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    conf_text, conf_unreadable = _read_optional_strict(paths.conf_path)
    if conf_unreadable:
        return 1, [f"ERROR: cannot read {paths.conf_path}"]
    out: list[str] = []
    new_conf = update_snapshots_conf_scrub_limit(conf_text, target_limit)
    if new_conf is not None and new_conf != conf_text:
        ensure_dir(os.path.dirname(paths.conf_path), 0o755)
        write_atomic(paths.conf_path, new_conf, mode=0o644, durable=True, relabel=True)
        verb = "written:" if conf_text is None else "updated:"
        note = f"SCRUB_LIMIT='{target_limit}'" if target_limit is not None else "removed SCRUB_LIMIT"
        out.append(f"{verb:<10} {paths.conf_path} ({note})")
    else:
        suffix = " (absent)" if conf_text is None else ""
        out.append(f"unchanged: {paths.conf_path}{suffix}")

    handover = lc_ctype_handover().partition("=")
    env = {handover[0]: handover[2]}
    if target_limit is not None:
        dropin_text = render_scrub_dropin(target_limit)
        argv = [
            SYSTEM_PYTHON,
            "-I",
            "-B",
            paths.installer_script,
            "text",
            paths.dropin_path,
            "0644",
            "--component",
            Component.BTRFS_CORE.value,
        ]
        res = cmd_runner.run(argv, input_text=dropin_text, env=env, output=OutputMode.CAPTURE)
    else:
        argv = [SYSTEM_PYTHON, "-I", "-B", paths.installer_script, "remove-text", paths.dropin_path]
        res = cmd_runner.run(argv, env=env, output=OutputMode.CAPTURE)

    out.extend(line for line in res.stdout.splitlines() if line.strip())
    if res.returncode != 0:
        if res.stderr.strip():
            out.append(res.stderr.strip())
        return res.returncode, out

    if target_limit is None:
        with contextlib.suppress(OSError):
            os.rmdir(os.path.dirname(paths.dropin_path))
    dropin_changed = any(line.startswith(("written:", "removed:")) for line in res.stdout.splitlines())
    if dropin_changed:
        cmd_runner.run(["systemctl", "daemon-reload"], output=OutputMode.CAPTURE)
    return 0, out

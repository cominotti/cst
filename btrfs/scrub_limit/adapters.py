# SPDX-License-Identifier: MPL-2.0
"""Host/toolbox filesystem and systemd adapters for `cst btrfs scrub-limit`: the ports of ports.py over
snapshots.conf, the installer, systemctl and sysfs."""

from __future__ import annotations

from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import glob
import os
from typing import Final

from btrfs.scrub_limit.application import ScrubLimit
from btrfs.scrub_limit.domain import (
    SCRUB_CONF_PATH,
    SCRUB_DROPIN_PATH,
    SCRUB_SERVICE,
    ScrubLimitAction,
    ScrubParsedArgs,
)
from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import ensure_dir, read_text, write_atomic
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import (
    ManagedCliEnv,
    host_view_path,
    installer_script_for,
    run_installer,
    undo_python_start,
)
from cst_core.domain.install import Component
from cst_core.domain.text import YES_WORDS
from cst_core.ports import CommandResult, CommandRunnerPort, ExecutionContextPort, UnitName

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


def _remove_if_empty(folder: str) -> None:
    """Remove folder when it is empty; one that is not, or is not there, stays as it is."""
    with contextlib.suppress(OSError):
        os.rmdir(folder)


@dataclass(frozen=True, slots=True)
class ScrubSettingsFile:
    """ScrubSettingsPort over snapshots.conf at path: 0644, its folder 0755, replaced atomically and durably,
    relabelled."""

    path: str

    def read(self) -> tuple[str | None, bool]:
        """(its text, None when it is not there; True when it is there and cannot be read)."""
        return _read_optional_strict(self.path)

    def folder_exists(self) -> bool:
        """Whether its folder is there."""
        return os.path.isdir(os.path.dirname(self.path))

    def write(self, text: str) -> None:
        """Its folder made when it is missing, then the file replaced with text."""
        ensure_dir(os.path.dirname(self.path), 0o755)
        write_atomic(self.path, text, mode=0o644, durable=True, relabel=True)

    def remove(self) -> None:
        """The file removed (one that is not there: nothing)."""
        with contextlib.suppress(FileNotFoundError):
            os.remove(self.path)

    def remove_folder_if_empty(self) -> None:
        """Its folder removed when it is empty."""
        _remove_if_empty(os.path.dirname(self.path))


@dataclass(frozen=True, slots=True)
class ScrubDropinInstaller:
    """ScrubDropinPort through the installer (installer_script) run by runner. Its record line is written or
    dropped and btrfs:core keeps the state it has in the install record (text --keep-state): this command
    is not btrfs:core's setup command, so no end of its run (killed, failed, rolled back) changes that state."""

    path: str
    installer_script: str
    runner: CommandRunnerPort

    def read(self) -> tuple[str | None, bool]:
        """(its text, None when it is not there; True when it is there and cannot be read)."""
        return _read_optional_strict(self.path)

    def write(self, text: str) -> tuple[int, list[str], str]:
        """installer.py text PATH 0644 --component btrfs:core --keep-state, text on its stdin."""
        return run_installer(
            self.runner,
            self.installer_script,
            ["text", self.path, "0644", "--component", Component.BTRFS_CORE.value, "--keep-state"],
            input_text=text,
        )

    def remove(self) -> tuple[int, list[str], str]:
        """installer.py remove-text PATH."""
        return run_installer(self.runner, self.installer_script, ["remove-text", self.path])

    def remove_folder_if_empty(self) -> None:
        """Its folder removed when it is empty."""
        _remove_if_empty(os.path.dirname(self.path))


@dataclass(frozen=True, slots=True)
class ScrubSystemd:
    """ScrubSystemdPort through SystemctlAdapter over runner; on_host: where the question goes from a
    toolbox (status and plan ask the host's systemd; set|off run on the host)."""

    runner: CommandRunnerPort
    on_host: SystemctlHost

    def need_daemon_reload(self) -> bool | None:
        """NeedDaemonReload of the scrub unit, through SystemctlAdapter.show (the C locale, and its time limit:
        a PID 1 that does not answer must not hang a run with nothing to change): True when systemd has not
        loaded its files as they are on disk (an earlier run wrote them and stopped before its daemon-reload);
        None when systemctl cannot say (it fails, or does not answer in time)."""
        props = SystemctlAdapter(self.runner, on_host=self.on_host).show(UnitName(SCRUB_SERVICE), ["NeedDaemonReload"])
        value = props.get("NeedDaemonReload")
        if value is None:
            return None
        return value.strip().lower() in YES_WORDS

    def daemon_reload(self, *, timeout: float | None) -> CommandResult:
        """systemctl daemon-reload, at most timeout seconds (None: no limit), never on the host's systemd
        from a toolbox."""
        return SystemctlAdapter(self.runner, timeout=timeout, on_host=SystemctlHost.LOCAL).daemon_reload_result()


@dataclass(frozen=True, slots=True)
class ScrubSysfs:
    """ScrubSysfsPort over the btrfs sysfs tree at btrfs_sysfs."""

    btrfs_sysfs: str

    def live_limits(self) -> tuple[str, ...]:
        """scrub_speed_max of every device of every btrfs filesystem, in path order."""
        pattern = os.path.join(glob.escape(self.btrfs_sysfs), "*", "devinfo", "*", "scrub_speed_max")
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
        return tuple(out)


def build_scrub_limit(paths: ScrubPaths, runner: CommandRunnerPort, *, read_only: bool) -> ScrubLimit:
    """The use cases over the real files, installer and systemctl at paths; read_only (status and plan):
    systemd's answer comes from the host's systemd from a toolbox too."""
    return ScrubLimit(
        settings=ScrubSettingsFile(paths.conf_path),
        dropin=ScrubDropinInstaller(paths.dropin_path, paths.installer_script, runner),
        systemd=ScrubSystemd(runner, SystemctlHost.SHOW_ONLY if read_only else SystemctlHost.LOCAL),
        sysfs=ScrubSysfs(paths.btrfs_sysfs),
    )


def scrub_limit_status(paths: ScrubPaths, *, runner: CommandRunnerPort | None = None) -> tuple[int, list[str]]:
    """Read-only status of `SCRUB_LIMIT`, drop-in, sysfs, and whether systemd loaded the drop-in."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    return build_scrub_limit(paths, cmd_runner, read_only=True).status()


def scrub_limit_plan(
    paths: ScrubPaths, target_limit: str | None, *, runner: CommandRunnerPort | None = None
) -> tuple[int, list[str]]:
    """Read-only dry run of `cst btrfs scrub-limit set|off`, the daemon-reload included that apply runs
    when systemd has not loaded the drop-in as it is on disk."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    return build_scrub_limit(paths, cmd_runner, read_only=True).plan(target_limit)


def scrub_limit_apply(
    paths: ScrubPaths,
    target_limit: str | None,
    *,
    runner: CommandRunnerPort | None = None,
) -> tuple[int, list[str]]:
    """Update `SCRUB_LIMIT` in `snapshots.conf`, update/remove `10-cst-limit.conf`, and reload systemd if needed.

    When the installer or daemon-reload fails, snapshots.conf and the drop-in go back to what they were."""
    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    return build_scrub_limit(paths, cmd_runner, read_only=False).apply(target_limit)


type ScrubBuild = Callable[[bool], ScrubLimit]  # the use case for read_only (status and plan: True)


def _run_action(service: ScrubLimit, parsed: ScrubParsedArgs) -> tuple[int, list[str]]:
    if parsed.action == ScrubLimitAction.STATUS:
        return service.status()
    if parsed.action == ScrubLimitAction.PLAN:
        return service.plan(parsed.limit)
    return service.apply(parsed.limit)


@dataclass(frozen=True, slots=True)
class ScrubCliExecutor:
    """The command line's action: status, plan or set|off over the use case build gives (None: the real one,
    at the paths of the program's entry file and context, through the runner of its environment)."""

    build: ScrubBuild | None = None

    def __call__(self, parsed: ScrubParsedArgs, env: ManagedCliEnv, /, *, read_only: bool) -> tuple[int, list[str]]:
        """Run the parsed action; read_only: status and plan (the host's view from a toolbox)."""
        if self.build is not None:
            return _run_action(self.build(read_only), parsed)
        paths = scrub_paths_for(env.entry_file, ctx=env.ctx, read_only=read_only, fallback_root=env.fallback_root)
        runner = SubprocessCommandRunner() if env.runner is None else env.runner
        return _run_action(build_scrub_limit(paths, runner, read_only=read_only), parsed)

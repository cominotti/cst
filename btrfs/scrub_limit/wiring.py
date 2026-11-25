# SPDX-License-Identifier: MPL-2.0
"""The composition root of `cst btrfs scrub-limit`: the use cases built over the real adapters, the
command line's action, and the managed command line (help, refusals, sudo through run_managed_cli).

btrfs/scrub_limit/cli.py hands argv to this module.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from btrfs.scrub_limit import domain as scrub_domain
from btrfs.scrub_limit.adapters import (
    ScrubDropinInstaller,
    ScrubPaths,
    ScrubSettingsFile,
    ScrubSysfs,
    ScrubSystemd,
    scrub_paths_for,
)
from btrfs.scrub_limit.application import ScrubLimit
from btrfs.scrub_limit.domain import ScrubLimitAction, ScrubParsedArgs
from cst_core.adapters import SystemctlHost
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.runtime import ManagedCliEnv, ManagedCliSpec, run_managed_cli, undo_python_start
from cst_core.domain.values import ExitAndLines
from cst_core.ports import CommandRunnerPort

ScrubCliDeps = ManagedCliEnv
type ScrubBuild = Callable[[bool], ScrubLimit]  # the use case for read_only (status and plan: True)


def start_scrub_process() -> None:
    """Restore default SIGPIPE/SIGXFSZ and caller LC_CTYPE."""
    undo_python_start()


def build_scrub_limit(paths: ScrubPaths, runner: CommandRunnerPort | None, *, read_only: bool) -> ScrubLimit:
    """The use cases over the real files, installer and systemctl at paths, through runner (None: a
    SubprocessCommandRunner); read_only (status and plan): systemd's answer comes from the host's systemd
    from a toolbox too."""
    if runner is None:
        runner = SubprocessCommandRunner()
    return ScrubLimit(
        settings=ScrubSettingsFile(paths.conf_path),
        dropin=ScrubDropinInstaller(paths.dropin_path, paths.installer_script, runner),
        systemd=ScrubSystemd(runner, SystemctlHost.SHOW_ONLY if read_only else SystemctlHost.LOCAL),
        sysfs=ScrubSysfs(paths.btrfs_sysfs),
    )


def scrub_limit_status(paths: ScrubPaths, *, runner: CommandRunnerPort | None = None) -> ExitAndLines:
    """Read-only status of `SCRUB_LIMIT`, drop-in, sysfs, and whether systemd loaded the drop-in."""
    return build_scrub_limit(paths, runner, read_only=True).status()


def scrub_limit_plan(
    paths: ScrubPaths, target_limit: str | None, *, runner: CommandRunnerPort | None = None
) -> ExitAndLines:
    """Read-only dry run of `cst btrfs scrub-limit set|off`, the daemon-reload included that apply runs
    when systemd has not loaded the drop-in as it is on disk."""
    return build_scrub_limit(paths, runner, read_only=True).plan(target_limit)


def scrub_limit_apply(
    paths: ScrubPaths,
    target_limit: str | None,
    *,
    runner: CommandRunnerPort | None = None,
) -> ExitAndLines:
    """Update `SCRUB_LIMIT` in `snapshots.conf`, update/remove `10-cst-limit.conf`, and reload systemd if needed.

    When the installer or daemon-reload fails, snapshots.conf and the drop-in go back to what they were."""
    return build_scrub_limit(paths, runner, read_only=False).apply(target_limit)


def _run_action(service: ScrubLimit, parsed: ScrubParsedArgs) -> ExitAndLines:
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

    def __call__(self, parsed: ScrubParsedArgs, env: ManagedCliEnv, /, *, read_only: bool) -> ExitAndLines:
        """Run the parsed action; read_only: status and plan (the host's view from a toolbox)."""
        if self.build is not None:
            return _run_action(self.build(read_only), parsed)
        paths = scrub_paths_for(env.entry_file, ctx=env.ctx, read_only=read_only, fallback_root=env.fallback_root)
        return _run_action(build_scrub_limit(paths, env.runner, read_only=read_only), parsed)


_SCRUB_SPEC = ManagedCliSpec(
    default_prog="cst btrfs scrub-limit",
    parse=scrub_domain.parse_scrub_limit_args,
    format_usage=scrub_domain.format_scrub_usage,
    is_help=lambda p: p.action == ScrubLimitAction.HELP,
    parse_error=lambda p: None if p.error is None else (p.code, p.error),
    write_action=lambda p: p.action.value if p.action in (ScrubLimitAction.SET, ScrubLimitAction.OFF) else None,
    elevation_reason="writes /etc/btrfs-setup/snapshots.conf and systemd drop-in",
    execute=ScrubCliExecutor(),
)


def run_scrub_limit(args: Sequence[str], *, deps: ScrubCliDeps | None = None, build: ScrubBuild | None = None) -> int:
    """One run of the command line with args: help, usage errors, host and root refusals for writes (sudo
    re-runs it as root), then the action (build: a test's use case for read_only; None: the real one)."""
    spec = _SCRUB_SPEC if build is None else replace(_SCRUB_SPEC, execute=ScrubCliExecutor(build))
    return run_managed_cli(args, spec, deps)

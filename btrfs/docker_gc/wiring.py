# SPDX-License-Identifier: MPL-2.0
"""The composition root of docker-gc: its environment (settings and paths), the signal
handling of the read-only commands, and the use cases built over the real adapters.

btrfs/docker_gc/cli.py parses argv, asks this module for the service and runs one command.
"""

from collections.abc import Mapping
import os

from btrfs.docker_gc.adapters import DockerCli, GcHost, GcSettingsFile, GcStateFiles, TerminalConsole
from btrfs.docker_gc.application import DockerGc
from btrfs.docker_gc.domain import GcPaths, GcSettings
from cst_core.adapters import SystemClockAdapter
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.runtime import restore_default_signals as _restore_default_signals


def settings_from(env: Mapping[str, str]) -> GcSettings:
    """KEEP_CONTAINER_DAYS, KEEP_IMAGE_DAYS, KEEP_BUILD_CACHE_DAYS and DOCKER_GC_FLUSH (the unit's
    Environment=); a value that is not a number stops the program. The protected registries come
    from the settings file (DockerGc.read_protection), DOCKER_GC_PROTECT in env winning over it."""
    d = GcSettings()  # the defaults
    return GcSettings(
        keep_container_days=int(env.get("KEEP_CONTAINER_DAYS", d.keep_container_days)),
        keep_image_days=int(env.get("KEEP_IMAGE_DAYS", d.keep_image_days)),
        keep_build_cache_days=int(env.get("KEEP_BUILD_CACHE_DAYS", d.keep_build_cache_days)),
        flush_seconds=int(env.get("DOCKER_GC_FLUSH", d.flush_seconds)),
    )


def paths_from(env: Mapping[str, str]) -> GcPaths:
    """The state and runtime folders, the settings file and the installed program (DOCKER_GC_STATE,
    DOCKER_GC_RUNTIME, DOCKER_GC_CONF and DOCKER_GC_INSTALLED: tests; the last two also the installer's
    SettingsStep, cst_core/domain/settings_steps.py, which names the staged copy and file with them)."""
    d = GcPaths()  # the defaults
    return GcPaths(
        state_dir=env.get("DOCKER_GC_STATE", d.state_dir),
        run_dir=env.get("DOCKER_GC_RUNTIME", d.run_dir),
        settings_file=env.get("DOCKER_GC_CONF", d.settings_file),
        installed=env.get("DOCKER_GC_INSTALLED", d.installed),
    )


def quiet_sigpipe() -> None:
    """SIGPIPE at its default: `docker-gc status | head` ends quietly (status and a dry run only read)."""
    _restore_default_signals()


def build_docker_gc(env: Mapping[str, str] | None = None) -> DockerGc:
    """docker-gc's use cases over the real docker, state files, settings file, host and terminal."""
    values = os.environ if env is None else env
    runner = SubprocessCommandRunner()
    paths = paths_from(values)
    return DockerGc(
        settings_from(values),
        docker=DockerCli(runner),
        state=GcStateFiles(paths),
        host=GcHost(runner),
        console=TerminalConsole(),
        clock=SystemClockAdapter(),
        config=GcSettingsFile(paths),
        paths=paths,
        protect_env=values.get("DOCKER_GC_PROTECT"),
    )

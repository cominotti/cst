# SPDX-License-Identifier: MPL-2.0
"""The composition root of docker-volumes-nocow: its paths (the volumes folder from
/etc/docker/daemon.json) and the use cases built over the real adapters.

btrfs/docker_volumes_nocow/cli.py parses argv, asks this module for the service and runs one command.
"""

from btrfs.docker_volumes_nocow.adapters import (
    DockerDaemons,
    LocalVolumeTree,
    NocowOutput,
    NocowStateFiles,
    docker_root,
)
from btrfs.docker_volumes_nocow.application import DockerVolumesNocow
from btrfs.docker_volumes_nocow.domain import NocowPaths
from cst_core.adapters import SystemClockAdapter
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.ports import CommandRunnerPort


def nocow_paths() -> NocowPaths:
    """The machine's paths; the volumes folder is data-root/volumes of docker.service."""
    return NocowPaths(volumes=docker_root())


def build_nocow(paths: NocowPaths | None = None, *, runner: CommandRunnerPort | None = None) -> DockerVolumesNocow:
    """The use cases over the real files, ioctls and commands (runner and paths: a test's)."""
    where = nocow_paths() if paths is None else paths
    commands = SubprocessCommandRunner() if runner is None else runner
    return DockerVolumesNocow(
        paths=where,
        tree=LocalVolumeTree(where, commands),
        state=NocowStateFiles(where),
        daemon=DockerDaemons(where, commands),
        out=NocowOutput(where, commands),
        clock=SystemClockAdapter(),
    )

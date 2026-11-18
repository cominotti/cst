# SPDX-License-Identifier: MPL-2.0
"""docker-volumes-nocow's command line: status (the default), mark, migrate, unmark."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from btrfs.docker_volumes_nocow import wiring
from btrfs.docker_volumes_nocow.application import DockerVolumesNocow
from btrfs.docker_volumes_nocow.domain import NocowCommand

USAGE = "usage: docker-volumes-nocow status|mark|migrate|unmark"


def nocow_cli(argv: Sequence[str] | None = None, *, service: DockerVolumesNocow | None = None) -> int:
    """Run docker-volumes-nocow with argv (default: this process's); its exit status."""
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else NocowCommand.STATUS
    for known in NocowCommand:
        if cmd == known:
            return (wiring.build_nocow() if service is None else service).execute(known)
    print(USAGE, file=sys.stderr)
    return 2


def main() -> int:
    """docker-volumes-nocow with this program's arguments."""
    return nocow_cli()

# SPDX-License-Identifier: MPL-2.0
"""The manifest programs whose installed copy may be the only record of a setting, and the step
that keeps it before an install replaces that copy (cst_core.application.install). A module of its
own, not part of the manifest (cst_core/domain/install.py): an older manifest put back over this
checkout (the upgrade tests' legacy trees) leaves it as it is."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class SettingsStep:
    """A program of the manifest whose installed copy may be the only record of a setting, while its
    settings file is missing (an older docker-gc had its protected registries built in). Before an
    install replaces that copy, or a remove deletes it, and only while settings_file is missing, the
    checkout's copy of the program runs args with env_program naming the installed copy and env_file
    the settings file (both under the staging root), and writes the file from it. An exit status other
    than 0 stops the install or the remove before it changes anything."""

    settings_file: str
    args: tuple[str, ...]
    env_program: str
    env_file: str


# The manifest sources with a SettingsStep (by src).
SETTINGS_STEPS: Final[Mapping[str, SettingsStep]] = {
    "btrfs/docker-gc": SettingsStep(
        "/etc/cst/docker-gc.conf", ("settings", "--write"), "DOCKER_GC_INSTALLED", "DOCKER_GC_CONF"
    ),
}

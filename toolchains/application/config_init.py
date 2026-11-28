# SPDX-License-Identifier: MPL-2.0
"""Use case for `cst toolchains config init [--plan] [--force]`."""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING, assert_never

from cst_core.domain.errors import Refused
from toolchains.domain.common import CONF_REL, parse_conf, tilde
from toolchains.domain.config import CARGO_CFG_REL
from toolchains.domain.config_init import (
    UserConfKind,
    active_override_keys,
    detect_cargo_overrides,
    render_starter_conf,
)
from toolchains.domain.prune import checked_settings, known_keys, parse_defaults

if TYPE_CHECKING:
    from toolchains.ports import HomeFilesPort, RunOutputPort


@dataclasses.dataclass(frozen=True, slots=True)
class ToolConfigInit:
    """Create or refresh `~/.config/cst/toolchains.conf` with documented defaults and active overrides."""

    home: str
    prog: str
    files: HomeFilesPort
    out: RunOutputPort

    def run(self, *, plan: bool = False, force: bool = False) -> int:
        """Dry-run (`plan=True`) or write `~/.config/cst/toolchains.conf`."""
        defaults_path, defaults_text = self.files.defaults_conf()
        defaults = parse_defaults(defaults_path, defaults_text)
        assert defaults_text is not None

        conf_path = os.path.join(self.home, CONF_REL)
        shown = tilde(conf_path, self.home)
        kind, existing = self.files.user_conf_state()
        match kind:
            case UserConfKind.SYMLINK:
                raise Refused(f"{shown} is a symbolic link: left as it is")
            case UserConfKind.OTHER:
                raise Refused(f"{shown} is not a regular file: left as it is")
            case UserConfKind.UNREADABLE:
                raise Refused(f"cannot read {shown}: {existing}: left as it is")
            case UserConfKind.REGULAR:
                if not force:
                    msg = f"{shown} already exists (pass --force to refresh while keeping current settings)"
                    if plan:
                        self.out.say(f"kept:      {msg}")
                        return 0
                    raise Refused(msg)
                checked_settings(self.home, defaults, existing)
                active = parse_conf(existing, known_keys())
            case UserConfKind.MISSING:
                cargo_raw = self.files.read_settings(os.path.join(self.home, CARGO_CFG_REL))
                active = detect_cargo_overrides(self.home, cargo_raw, self.files.read_installed_wrapper())
            case _:
                assert_never(kind)

        rendered = render_starter_conf(defaults_text, active, defaults_path=defaults_path)
        if kind == UserConfKind.REGULAR and existing == rendered:
            self.out.say(f"unchanged: {shown}")
            return 0

        keys = active_override_keys(defaults, active)
        detail = (
            f"{len(keys)} active override(s): {', '.join(keys)}"
            if keys
            else "all settings commented out at CST defaults"
        )
        if plan:
            self.out.say(f"would write: {shown} ({detail})")
            suffix = " --force" if force else ""
            self.out.say(f"Apply with: {self.prog} config init{suffix}")
            return 0

        self.files.write_user_conf(rendered)
        self.out.say(f"written:   {shown} ({detail})")
        self.out.say(f"Edit {shown} and uncomment any setting to override, then run: {self.prog} config apply")
        return 0

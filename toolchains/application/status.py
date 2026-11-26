# SPDX-License-Identifier: MPL-2.0
"""`status`: the targets (their policy, last prune and own lines), the timer, the
planned targets, the rust toolchains report, the toolchain settings, the sccache servers
and the files of auto on. It only reads.
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING

from toolchains.domain.auto import label
from toolchains.domain.common import ConfigError, tilde
from toolchains.domain.config import ConfigView
from toolchains.domain.prune import PLANNED, SPECS, saved_run_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from toolchains.application.auto import AutoTimer
    from toolchains.application.config import ToolConfig
    from toolchains.application.prune import Target
    from toolchains.domain.prune import Settings, TargetSpec
    from toolchains.ports import HomeFilesPort, InstalledToolsPort, RunOutputPort, RunStatePort


@dataclasses.dataclass(frozen=True)
class ToolchainsStatus:
    """status for HOME (PROG: how hints name the program): SETTINGS() the checked settings
    (ConfigError: they are shown as bad), TARGET(settings or None, id) each target."""

    home: str
    prog: str
    out: RunOutputPort
    files: HomeFilesPort
    state: RunStatePort
    tools: InstalledToolsPort
    timer: AutoTimer
    config: ToolConfig
    settings: Callable[[], Settings]
    target: Callable[[Settings | None, str], Target]

    def show(self, *, brief: bool) -> None:
        """Every section, or with BRIEF only the timer and each target's last prune."""
        say = self.out.say
        say(f"auto: {self.timer.timer_text()}")
        settings: Settings | None
        try:
            settings = self.settings()
        except ConfigError as e:
            settings = None
            say(f"BAD SETTINGS: {e} (prune and the timer do nothing until it is fixed)")
        for tid, spec in SPECS.items():
            last = saved_run_text(tid, self.state.read(tid))
            if brief:
                say(f"{tid}: last prune: {last}")
            else:
                self._target_section(tid, spec, settings, last)
        if not brief:
            self._other_sections(settings)

    def _target_section(self, tid: str, spec: TargetSpec, settings: Settings | None, last: str) -> None:
        """The section of target TID (LAST: its last prune; SETTINGS None: bad)."""
        say = self.out.say
        say(f"== {tid}: {spec.what}")
        t = self.target(settings, tid)
        say(f"  where:    {self.out.where(t.location())}")
        if settings:
            state = "enabled" if settings.enabled[tid] else "disabled (prune runs it only when named)"
            say(f"  policy:   {state}; {settings.policy_text(tid)} ({settings.source_of(spec)})")
        else:
            say("  policy:   BAD SETTINGS (see above)")
        say(f"  last prune: {last}")
        for line in t.status_lines():
            say(f"  {line}")

    def _other_sections(self, settings: Settings | None) -> None:
        """The sections after the targets: the planned targets, the rust toolchains,
        the toolchain settings, the sccache servers and the files of auto on."""
        home, say = self.home, self.out.say
        say("== not implemented yet (nothing pruned there by CST):")
        for tid, (where, how) in PLANNED.items():
            shown = ", ".join(self.out.where(os.path.join(home, p.strip()[2:])) for p in where.split(","))
            say(f"  {tid:<14} {shown} ({how})")
        say("== rust toolchains (a report: CST never removes one)")
        for line in self.tools.rust_toolchains():
            say(f"  {line}")
        hakari = self.tools.hakari_advisory()
        if hakari:
            say("== workspace feature unification (advisory: CST never edits your repos)")
            for line in hakari:
                say(f"  {line}")
        say(f"== toolchain settings ({self.prog} config)")
        for st, shown, note in self.config.lines(ConfigView.STATUS, settings):
            say(f"  {st:<8} {shown:<24} {note}")
        for line in self.tools.sccache():
            say(f"  {line}")
        st = self.files.auto_states()
        udir = self.files.unit_dir()
        say("== files of auto on: " + ", ".join(f"{label(k, udir, home)} {v}" for k, v in st.items()))
        for path, ours in self.files.stale_files():
            say(
                f"  stale: {tilde(path, home)} (of the earlier `cst btrfs cargo-gc`"
                + ("; auto on or auto off removes it)" if ours else "; not CST's: left alone)")
            )
        self.timer.old_conf_note()

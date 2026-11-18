# SPDX-License-Identifier: MPL-2.0
"""`auto on|off`: the user timer and the copies it runs, and the removal of what the
earlier `cst btrfs cargo-gc` left. Both change the machine in steps: the timers are
stopped, checked, before any file is removed, so a run the user manager refused can be
run again to finish the job.
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING

from cst_core.domain.errors import Refused
from cst_core.domain.files import OwnedState
from cst_core.domain.units import masked_refusal
from toolchains.domain.auto import AUTO_UNITS, label
from toolchains.domain.common import (
    CONF_REL,
    LIB_REL,
    OFF_REL,
    OLD_CONF_REL,
    OLD_TIMER,
    SERVICE,
    TIMER,
    UNIT_DIR_REL,
    WANTS_REL,
    tilde,
)
from toolchains.domain.prune import SPECS

if TYPE_CHECKING:
    from collections.abc import Callable

    from toolchains.application.prune import Target
    from toolchains.domain.prune import Settings
    from toolchains.ports import HomeFilesPort, RunOutputPort, RunStatePort, UserUnitsPort


@dataclasses.dataclass(frozen=True)
class AutoTimer:
    """auto on and auto off for HOME (PROG: how hints name the program): the settings
    (SETTINGS(): the checked settings, ConfigError otherwise) and the targets
    (TARGET(settings, id)) say whether the timer would do anything."""

    home: str
    prog: str
    files: HomeFilesPort
    units: UserUnitsPort
    state: RunStatePort
    out: RunOutputPort
    settings: Callable[[], Settings]
    target: Callable[[Settings, str], Target]

    def remove_stale(self) -> bool:
        """Remove what the earlier `cst btrfs cargo-gc` left. True when all went
        well; False when the daemon-reload after the removal failed (reported).
        Refused (nothing removed) when its timer could not be stopped."""
        home = self.home
        found = self.files.stale_files()
        if not found:
            return True
        timer = os.path.join(home, UNIT_DIR_REL, OLD_TIMER)
        if (timer, True) in found:
            self.units.disable_timer(OLD_TIMER)
        reload = False
        for path, ours in found:
            if not ours:
                self.out.say(f"left alone: {tilde(path, home)} (not written by CST)", 4, err=True)
                continue
            self.files.remove(path)
            self.out.say(f"removed:   {tilde(path, home)} (left by the earlier `cst btrfs cargo-gc`)")
            reload = reload or path.startswith(os.path.join(home, UNIT_DIR_REL) + "/")
        return not (reload and self.units.reload_failed())

    def on(self, *, unless_off: bool = False) -> int:
        rc = 0 if self.remove_stale() else 1
        if unless_off and self.files.turned_off():
            self.out.say(
                f"left off: you turned the automatic pruning off ({self.prog} auto off left ~/{OFF_REL}); "
                f"'{self.prog} auto on' turns it on again"
            )
            return rc
        settings = self.settings()
        enabled = [tid for tid in SPECS if settings.enabled[tid]]
        if not enabled:
            raise Refused(f"no target is enabled in the settings ({settings.source}): the timer would do nothing")
        # Checked as the timer will run: programs are looked for in the PATH of
        # the user manager (the service's PATH), not in this command's (run by
        # snapshots setup through runuser, it is root's). If no target would run,
        # no timer.
        mpath: str | None = None
        asked = False
        refused: list[str] = []
        for tid in enabled:
            t = self.target(settings, tid)
            if t.SPEC.uses_path:
                if not asked:
                    mpath, asked = self.units.manager_path(), True
                t.path = mpath
            try:
                t.check()
            except Refused as e:
                refused.append(f"{tid}: {e}")
        if len(refused) == len(enabled):
            raise Refused("; ".join(refused))
        where = (
            "in the PATH of your user manager, which the timer uses"
            if mpath is not None
            else "in the PATH of this command; the timer uses your user manager's PATH (systemctl --user "
            "show-environment), which could not be read"
        )
        for r in refused:
            self.out.say(
                f"note: {r} (looked for {where}): each timer run tries it and reports it as refused (an error) "
                f"until it is fixed; or set its _ENABLED=no in ~/{CONF_REL}",
                4,
                err=True,
            )
        st = self.files.auto_states()
        masked = [os.path.join(self.home, UNIT_DIR_REL, k) for k in AUTO_UNITS if st[k] == OwnedState.MASKED]
        if why := masked_refusal(masked, user=True):
            raise Refused(why + "; nothing was written")
        conflicts = [k for k, v in st.items() if v == OwnedState.CONFLICT]
        if conflicts:
            raise Refused("not written by CST, left alone: " + ", ".join(label(k) for k in conflicts))
        changed = False
        for rel, data in self.files.auto_copies().items():
            self.out.say(f"{'written:  ' if self.files.put_auto_copy(rel, data) else 'unchanged:'} ~/{rel}")
        for name, render in AUTO_UNITS.items():
            put = self.files.put_auto_unit(name, render())
            self.out.say(f"{'written:  ' if put else 'unchanged:'} {label(name)}")
            changed = changed or put
        if changed:
            self.units.reload()
        self.units.enable_now(TIMER)
        self.out.say(f"enabled:   {TIMER} (hourly; logs: journalctl --user -u {SERVICE})")
        self.files.auto_done()
        if self.files.turned_off():
            self.files.remove(os.path.join(self.home, OFF_REL))
            self.out.say(f"removed:   ~/{OFF_REL} (snapshots setup keeps it enabled again)")
        self.old_conf_note()
        return rc

    def stop_orphan_timer(self) -> None:
        """The timer file is gone, but the user manager may still run the
        timer: what `auto off` left before it checked its `disable` (it removed
        the files anyway). Stopped when the manager does not say it is stopped;
        Refused when the manager gives no answer."""
        if self.units.stop_if_running(TIMER):
            self.out.say(f"stopped:   {TIMER} (it was still running although its file was gone)")

    def off(self) -> int:
        home = self.home
        st = self.files.auto_states()
        # Both timers are stopped, checked, before anything is removed (see
        # UserUnitsPort); remove_stale stops the earlier one first.
        if st[TIMER] in (OwnedState.OK, OwnedState.CHANGE):
            self.units.disable_timer(TIMER)
        elif st[TIMER] == OwnedState.MISSING:
            self.stop_orphan_timer()
        rc = 0 if self.remove_stale() else 1
        removed = False
        for name in AUTO_UNITS:
            if st[name] in (OwnedState.OK, OwnedState.CHANGE):
                self.files.remove(os.path.join(home, UNIT_DIR_REL, name))
                self.out.say(f"removed:   {label(name)}")
                removed = True
            elif st[name] in (OwnedState.CONFLICT, OwnedState.MASKED):
                why = "masked with systemctl --user mask" if st[name] == OwnedState.MASKED else "not written by CST"
                self.out.say(f"left alone: {label(name)} ({why})", 4, err=True)
        # `disable` removes the link that enables the timer. One left pointing
        # at nothing (also left by the earlier unchecked `auto off`) goes too.
        wants = os.path.join(home, WANTS_REL)
        if self.files.dangling_link(wants):
            self.files.remove(wants)
            self.out.say(f"removed:   ~/{WANTS_REL} (it pointed at the removed timer file)")
            removed = True
        if removed and self.units.reload_failed():
            rc = 1
        for rel in self.files.auto_copies():
            if st[rel] in (OwnedState.OK, OwnedState.CHANGE):
                self.files.remove(os.path.join(home, rel))
                self.out.say(f"removed:   ~/{rel}")
            elif st[rel] == OwnedState.CONFLICT:
                self.out.say(f"left alone: ~/{rel} (not written by CST)", 4, err=True)
        self.files.forget_auto_files()
        self.files.remove_if_empty(os.path.join(home, LIB_REL))  # bluetooth keeps its watcher there
        for tid in SPECS:
            self.state.forget(tid)
        if not self.files.turned_off():
            self.files.mark_off()
            self.out.say(f"written:   ~/{OFF_REL} (snapshots setup leaves the pruning off)")
        self.out.say(f"toolchain caches are no longer pruned automatically (settings in ~/{CONF_REL} kept, if any)")
        return rc

    def timer_text(self) -> str:
        """The timer's line of status."""
        if self.files.timer_missing():
            if self.units.active_state(TIMER) == "active":
                return (
                    f"timer {TIMER}: still running although its file is gone (left by an earlier auto off); "
                    f"{self.prog} auto off stops it"
                )
            if self.files.turned_off():
                return (
                    f"timer {TIMER}: turned off by you ({self.prog} auto off; "
                    f"snapshots setup leaves it off; {self.prog} auto on turns it on)"
                )
            return f"timer {TIMER}: not installed ({self.prog} auto on)"
        facts = self.units.timer_facts(TIMER)
        txt = f"timer {TIMER}: {facts.enabled or 'unknown'}, {facts.active or 'unknown'}"
        if facts.active == "active" and facts.next_elapse not in ("", "n/a", "0"):
            txt += f", next {facts.next_elapse}"
        return txt

    def old_conf_note(self) -> None:
        if self.files.old_conf_present():
            self.out.say(
                f"note: ~/{OLD_CONF_REL} is no longer read: put its settings into ~/{CONF_REL} "
                "with the RUST_BUILD_ prefix (e.g. RUST_BUILD_MAX_IDLE=48h), then remove it",
                4,
                err=True,
            )

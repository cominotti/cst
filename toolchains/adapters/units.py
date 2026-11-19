# SPDX-License-Identifier: MPL-2.0
"""The user manager (systemctl --user): checked calls before files are
removed, daemon-reload, the timer's enable and disable, what it says of a timer,
the manager's PATH (UserUnits, the UserUnitsPort; a test replaces its systemctl,
or names a stub program instead of systemctl).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cst_core.ports import CommandResult
    from toolchains.ports import RunOutputPort

from cst_core.adapters import SystemctlAdapter
from cst_core.domain.errors import Refused
from cst_core.domain.values import MINUTE
from toolchains.domain.auto import (
    TIMER_RUNNING_STATES,
    UNIT_STOPPED_STATES,
    TimerFacts,
    UserManagerVerb,
    own_session_env,
)
from toolchains.domain.common import NAME
from toolchains.adapters.program import RUNNER


def systemctl_env() -> dict[str, str]:
    # Run by root through runuser or sudo: none, or root's (/run/user/0): this user's, as the domain says.
    uid, xdg = os.getuid(), os.environ.get("XDG_RUNTIME_DIR", "")
    owner = os.stat(xdg).st_uid if xdg and os.path.isdir(xdg) else None
    return own_session_env(os.environ, uid, owner, own_exists=os.path.isdir(f"/run/user/{uid}"))


def user_systemctl(program: str | None) -> SystemctlAdapter:
    """cst_core's systemctl adapter for the user manager: systemctl --user, on the host from a
    toolbox, this user's session (systemctl_env, read at each call), one minute at most, stdin
    inherited. PROGRAM: a test's stub (CST_TOOLCHAINS_SYSTEMCTL, which wiring reads), never on the
    host; None for the real systemctl. No default: every caller says which, so that no test
    reaches the real user manager by leaving it out."""
    return SystemctlAdapter(
        RUNNER, timeout=MINUTE, user=True, program=program, on_host=True, base_env=systemctl_env(), stdin="inherit"
    )


def _result(manager: SystemctlAdapter, args: Sequence[str]) -> CommandResult:
    """The adapter's call for one systemctl command line (ARGS without systemctl --user)."""
    match args:
        case (UserManagerVerb.IS_ACTIVE, unit):
            res = manager.is_active(unit)
        case (UserManagerVerb.IS_ENABLED, unit):
            res = manager.is_enabled(unit)
        case (UserManagerVerb.SHOW, "-p", prop, "--value", unit):
            res = manager.show_value(unit, prop)
        case (UserManagerVerb.SHOW_ENVIRONMENT,):
            res = manager.show_environment()
        case (UserManagerVerb.START, unit):
            res = manager.start(unit)
        case (UserManagerVerb.STOP, unit):
            res = manager.stop(unit)
        case (UserManagerVerb.ENABLE, "--now", unit):
            res = manager.enable(unit, now=True)
        case (UserManagerVerb.DISABLE, "--now", unit):
            res = manager.disable(unit, now=True)
        case (UserManagerVerb.CLEAN, flag, unit) if flag.startswith("--what="):
            res = manager.clean(unit, what=flag.removeprefix("--what="))
        case (UserManagerVerb.DAEMON_RELOAD,):
            res = manager.daemon_reload_result()
        case _:
            raise ValueError(f"systemctl --user {' '.join(args)}: not a call cst-toolchains makes")
    return res


def systemctl(*args: str, program: str | None, check: bool = False, ok: Sequence[int] = (0,)) -> str:
    """Output (stripped) of systemctl --user (PROGRAM: a stub run instead); '' when it could not run.
    check: Refused when it could not run or exited with a code not in ok."""
    r = _result(user_systemctl(program), args)
    if r.cause is not None:
        if check:
            raise Refused(f"systemctl --user {' '.join(args)}: {r.cause}") from None
        return ""
    if check and r.returncode not in ok:
        raise Refused(f"systemctl --user {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


class UserUnits:
    """UserUnitsPort: the user manager, as auto on, auto off and status use it (PROGRAM: a
    test's stub run instead of systemctl). Every call goes through systemctl(), which a
    test's subclass may replace."""

    def __init__(self, out: RunOutputPort, program: str | None) -> None:
        self.out = out
        self.program = program

    def systemctl(self, *args: str, check: bool = False, ok: Sequence[int] = (0,)) -> str:
        """See systemctl()."""
        return systemctl(*args, check=check, ok=ok, program=self.program)

    def active_state(self, name: str) -> str:
        return self.systemctl(UserManagerVerb.IS_ACTIVE, name)

    def timer_facts(self, name: str) -> TimerFacts:
        enabled = self.systemctl(UserManagerVerb.IS_ENABLED, name)
        active = self.systemctl(UserManagerVerb.IS_ACTIVE, name)
        if active not in TIMER_RUNNING_STATES:
            return TimerFacts(enabled, active)
        next_elapse = self.systemctl(UserManagerVerb.SHOW, "-p", "NextElapseUSecRealtime", "--value", name)
        return TimerFacts(enabled, active, next_elapse)

    def stop_if_running(self, name: str) -> bool:
        # is-active prints the state; it exits 3 when the unit is not active,
        # and 4 when the manager does not know it (nothing loaded it).
        state = self.before_removal(name, UserManagerVerb.IS_ACTIVE, ok=(0, 3, 4))
        if state in UNIT_STOPPED_STATES:
            return False
        self.before_removal(name, UserManagerVerb.STOP)
        return True

    def reload(self) -> None:
        self.systemctl(UserManagerVerb.DAEMON_RELOAD, check=True)

    def enable_now(self, name: str) -> None:
        self.systemctl(UserManagerVerb.ENABLE, "--now", name, check=True)

    def before_removal(self, name: str, *args: str, ok: Sequence[int] = (0,)) -> str:
        """systemctl --user ARGS NAME for a timer, checked, before auto on/off
        removes any file. When the user manager refuses or cannot be reached,
        nothing has been removed yet: Refused, and a second run (once the
        manager answers) can finish the job. Removing the files anyway would
        leave a timer that keeps running with no file left to show it."""
        try:
            return self.systemctl(*args, name, check=True, ok=ok)
        except Refused as e:
            raise Refused(
                f"{e}; nothing was removed and {name} may still be running: run this again once your user "
                "manager answers (`systemctl --user status` shows whether it does)"
            ) from None

    def reload_failed(self) -> bool:
        """daemon-reload after unit files were removed: True (and an error line)
        when it failed. The removal is not undone: the timer is already stopped,
        and the manager forgets the files at its next reload or restart."""
        try:
            self.reload()
        except Refused as e:
            self.out.say(
                f"{NAME}: {e}; the files are removed anyway: run `systemctl --user daemon-reload` once your user "
                "manager answers, so that it forgets them",
                3,
                err=True,
            )
            return True
        return False

    def disable_timer(self, name: str) -> None:
        """`disable --now` a timer, checked (see before_removal), then forget its
        last run: systemd keeps the stamp of a Persistent= timer
        (~/.local/share/systemd/timers/stamp-NAME) after the timer is gone.
        `clean` needs the timer loaded and stopped, so this runs before its file
        is removed; a failed clean leaves that file, which auto on and auto off
        then remove themselves (AutoTimer.forget_stamp)."""
        self.before_removal(name, UserManagerVerb.DISABLE, "--now")
        self.systemctl(UserManagerVerb.CLEAN, "--what=state", name)

    def manager_path(self) -> str | None:
        """PATH of the user manager (what the service runs with), or None when
        it cannot be read."""
        for line in self.systemctl(UserManagerVerb.SHOW_ENVIRONMENT).splitlines():
            if line.startswith("PATH="):
                val = line[len("PATH=") :]
                # systemd quotes a value with special characters ($'...'): not parsed here.
                return None if val[:1] in ("$", "'", '"') else val
        return None

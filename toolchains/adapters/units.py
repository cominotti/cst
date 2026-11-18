# SPDX-License-Identifier: MPL-2.0
"""The user manager (systemctl --user): checked calls before files are
removed, daemon-reload, the timer's disable, the manager's PATH.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

from cst_core.adapters.runner import on_host_argv
from cst_core.domain.errors import Refused
from toolchains.domain import NAME, own_session_env
from toolchains.adapters.program import RUNNER, in_container, say


def systemctl_cmd(*args: str) -> list[str]:
    # Tests point this at a stub: no test may reach the real user manager.
    stub = os.environ.get("CST_TOOLCHAINS_SYSTEMCTL")
    if stub:
        return [stub, "--user", *args]
    cmd = ["systemctl", "--user", *args]
    return on_host_argv(cmd, {}) if in_container() else cmd


def systemctl_env() -> dict[str, str]:
    # Run by root through runuser or sudo: none, or root's (/run/user/0): this user's, as the domain says.
    uid, xdg = os.getuid(), os.environ.get("XDG_RUNTIME_DIR", "")
    owner = os.stat(xdg).st_uid if xdg and os.path.isdir(xdg) else None
    return own_session_env(os.environ, uid, owner, own_exists=os.path.isdir(f"/run/user/{uid}"))


def systemctl(*args: str, check: bool = False, ok: Sequence[int] = (0,)) -> str:
    """Output (stripped) of systemctl --user; '' when it could not run.
    check: Refused when it could not run or exited with a code not in ok."""
    r = RUNNER.run(systemctl_cmd(*args), base_env=systemctl_env(), timeout=60, stdin="inherit")
    if r.cause is not None:
        if check:
            raise Refused(f"systemctl --user {' '.join(args)}: {r.cause}") from None
        return ""
    if check and r.returncode not in ok:
        raise Refused(f"systemctl --user {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout.strip()


def before_removal(name: str, *args: str, ok: Sequence[int] = (0,)) -> str:
    """systemctl --user ARGS NAME for a timer, checked, before auto on/off
    removes any file. When the user manager refuses or cannot be reached,
    nothing has been removed yet: Refused, and a second run (once the
    manager answers) can finish the job. Removing the files anyway would
    leave a timer that keeps running with no file left to show it."""
    try:
        return systemctl(*args, name, check=True, ok=ok)
    except Refused as e:
        raise Refused(
            f"{e}; nothing was removed and {name} may still be running: run this again once your user "
            "manager answers (`systemctl --user status` shows whether it does)"
        ) from None


def reload_failed() -> bool:
    """daemon-reload after unit files were removed: True (and an error line)
    when it failed. The removal is not undone: the timer is already stopped,
    and the manager forgets the files at its next reload or restart."""
    try:
        systemctl("daemon-reload", check=True)
    except Refused as e:
        say(
            f"{NAME}: {e}; the files are removed anyway: run `systemctl --user daemon-reload` once your user "
            "manager answers, so that it forgets them",
            3,
            err=True,
        )
        return True
    return False


def disable_timer(name: str) -> None:
    """`disable --now` a timer, checked (see before_removal), then forget its
    last run: systemd keeps the stamp of a Persistent= timer
    (~/.local/share/systemd/timers/stamp-NAME) after the timer is gone.
    `clean` needs the timer loaded and stopped, so this runs before its file
    is removed; a failed clean only leaves that empty file behind."""
    before_removal(name, "disable", "--now")
    systemctl("clean", "--what=state", name)


def manager_path() -> str | None:
    """PATH of the user manager (what the service runs with), or None when
    it cannot be read."""
    for line in systemctl("show-environment").splitlines():
        if line.startswith("PATH="):
            val = line[len("PATH=") :]
            # systemd quotes a value with special characters ($'...'): not parsed here.
            return None if val[:1] in ("$", "'", '"') else val
    return None

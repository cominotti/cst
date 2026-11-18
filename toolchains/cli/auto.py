# SPDX-License-Identifier: MPL-2.0
"""`auto on|off`: the user timer and the copies it runs, and the removal of
what the earlier `cst btrfs cargo-gc` left.
"""

from __future__ import annotations

import os

from collections.abc import Callable

from cst_core.adapters.files import read_bytes, write_atomic
from cst_core.adapters.runtime import install_payload
from cst_core.domain.errors import Refused
from cst_core.domain.files import UnitFileState
from cst_core.domain.units import masked_refusal
from toolchains.domain.auto import label, render_service, render_timer
from toolchains.domain.common import (
    CONF_REL,
    ConfigError,
    DEFAULTS_REL,
    LIB_REL,
    MARKER,
    OFF_REL,
    OLD_DATA,
    OLD_FILES,
    OLD_MARKER,
    OLD_TIMER,
    OLD_UNITS,
    PROG_REL,
    SERVICE,
    TIMER,
    UNIT_DIR_REL,
    WANTS_REL,
    tilde,
)
from toolchains.adapters.home_files import (
    AUTO_COMPONENT,
    file_state,
    forget_auto_files,
    old_conf_note,
    put_auto_copy,
    put_auto_unit,
    record_done,
    state_path,
    timer_unit_state,
    turned_off,
)
from toolchains.adapters.program import PROG, Program, say
from toolchains.adapters.units import before_removal, disable_timer, manager_path, reload_failed, systemctl
from toolchains.cli.prune import TARGETS, defaults_path, load_settings


UNITS: dict[str, Callable[[], str]] = {SERVICE: render_service, TIMER: render_timer}


def copies() -> dict[str, bytes]:
    """{installed path (relative to home): bytes}: this program as `install`
    writes it (a zipapp) and its defaults, as the timer runs them."""
    defaults = read_bytes(defaults_path())
    if defaults is None:
        raise ConfigError(f"the defaults file {defaults_path()} is missing")
    return {PROG_REL: install_payload("toolchains/cst-toolchains", Program.entry_file), DEFAULTS_REL: defaults}


def unit_states(home: str) -> dict[str, UnitFileState]:
    out: dict[str, UnitFileState] = {rel: file_state(os.path.join(home, rel), text) for rel, text in copies().items()}
    for name, render in UNITS.items():
        out[name] = timer_unit_state(os.path.join(home, UNIT_DIR_REL, name), render())
    return out


def stale_files(home: str) -> list[tuple[str, bool]]:
    """[(path, ours)] of what `cst btrfs cargo-gc enable|disable` left
    (the first version of this cleanup)."""
    out: list[tuple[str, bool]] = []
    for rel in [f"{UNIT_DIR_REL}/{u}" for u in OLD_UNITS] + list(OLD_FILES):
        path = os.path.join(home, rel)
        st = file_state(path, None, OLD_MARKER)
        if st != "missing":
            out.append((path, st == "change"))
    for rel in OLD_DATA:
        path = os.path.join(home, rel)
        if os.path.lexists(path):
            out.append((path, not os.path.islink(path)))
    return out


def remove_stale(home: str) -> bool:
    """Remove what the earlier `cst btrfs cargo-gc` left. True when all went
    well; False when the daemon-reload after the removal failed (reported).
    Refused (nothing removed) when its timer could not be stopped."""
    found = stale_files(home)
    if not found:
        return True
    timer = os.path.join(home, UNIT_DIR_REL, OLD_TIMER)
    if (timer, True) in found:
        disable_timer(OLD_TIMER)
    reload = False
    for path, ours in found:
        if not ours:
            say(f"left alone: {tilde(path, home)} (not written by CST)", 4, err=True)
            continue
        os.unlink(path)
        say(f"removed:   {tilde(path, home)} (left by the earlier `cst btrfs cargo-gc`)")
        reload = reload or path.startswith(os.path.join(home, UNIT_DIR_REL) + "/")
    return not (reload and reload_failed())


def cmd_auto_on(home: str, now: float, unless_off: bool = False) -> int:
    rc = 0 if remove_stale(home) else 1
    if unless_off and turned_off(home):
        say(
            f"left off: you turned the automatic pruning off ({PROG} auto off left ~/{OFF_REL}); "
            f"'{PROG} auto on' turns it on again"
        )
        return rc
    settings = load_settings(home)
    enabled = [cls for tid, cls in TARGETS.items() if settings.enabled[tid]]
    if not enabled:
        raise Refused(f"no target is enabled in the settings ({settings.source}): the timer would do nothing")
    # Checked as the timer will run: programs are looked for in the PATH of
    # the user manager (the service's PATH), not in this command's (run by
    # snapshots setup through runuser, it is root's). If no target would run,
    # no timer.
    mpath: str | None = None
    asked = False
    refused: list[str] = []
    for cls in enabled:
        t = cls.make(home, now, settings)
        if cls.SPEC.uses_path:
            if not asked:
                mpath, asked = manager_path(), True
            t.path = mpath
        try:
            t.check()
        except Refused as e:
            refused.append(f"{cls.SPEC.id}: {e}")
    if len(refused) == len(enabled):
        raise Refused("; ".join(refused))
    where = (
        "in the PATH of your user manager, which the timer uses"
        if mpath is not None
        else "in the PATH of this command; the timer uses your user manager's PATH (systemctl --user "
        "show-environment), which could not be read"
    )
    for r in refused:
        say(
            f"note: {r} (looked for {where}): each timer run tries it and reports it as refused (an error) "
            f"until it is fixed; or set its _ENABLED=no in ~/{CONF_REL}",
            4,
            err=True,
        )
    st = unit_states(home)
    if why := masked_refusal([os.path.join(home, UNIT_DIR_REL, k) for k in UNITS if st[k] == "masked"], user=True):
        raise Refused(why + "; nothing was written")
    conflicts = [k for k, v in st.items() if v == "conflict"]
    if conflicts:
        raise Refused("not written by CST, left alone: " + ", ".join(label(k) for k in conflicts))
    changed = False
    for rel, text in copies().items():
        say(f"{'written:  ' if put_auto_copy(home, rel, text) else 'unchanged:'} ~/{rel}")
    for name, render in UNITS.items():
        put = put_auto_unit(home, name, render())
        say(f"{'written:  ' if put else 'unchanged:'} {label(name)}")
        changed = changed or put
    if changed:
        systemctl("daemon-reload", check=True)
    systemctl("enable", "--now", TIMER, check=True)
    say(f"enabled:   {TIMER} (hourly; logs: journalctl --user -u {SERVICE})")
    record_done(home, AUTO_COMPONENT)
    if turned_off(home):
        os.unlink(os.path.join(home, OFF_REL))
        say(f"removed:   ~/{OFF_REL} (snapshots setup keeps it enabled again)")
    old_conf_note(home)
    return rc


def stop_orphan_timer() -> None:
    """The timer file is gone, but the user manager may still run the
    timer: what `auto off` left before it checked its `disable` (it removed
    the files anyway). Stopped when the manager does not say it is stopped;
    Refused when the manager gives no answer."""
    # is-active prints the state; it exits 3 when the unit is not active,
    # and 4 when the manager does not know it (nothing loaded it).
    if before_removal(TIMER, "is-active", ok=(0, 3, 4)) in ("inactive", "failed"):
        return
    before_removal(TIMER, "stop")
    say(f"stopped:   {TIMER} (it was still running although its file was gone)")


def cmd_auto_off(home: str, now: float) -> int:
    st = unit_states(home)
    # Both timers are stopped, checked, before anything is removed (see
    # before_removal); remove_stale stops the earlier one first.
    if st[TIMER] in ("ok", "change"):
        disable_timer(TIMER)
    elif st[TIMER] == "missing":
        stop_orphan_timer()
    rc = 0 if remove_stale(home) else 1
    removed = False
    for name in UNITS:
        if st[name] in ("ok", "change"):
            os.unlink(os.path.join(home, UNIT_DIR_REL, name))
            say(f"removed:   {label(name)}")
            removed = True
        elif st[name] in ("conflict", "masked"):
            why = "masked with systemctl --user mask" if st[name] == "masked" else "not written by CST"
            say(f"left alone: {label(name)} ({why})", 4, err=True)
    # `disable` removes the link that enables the timer. One left pointing
    # at nothing (also left by the earlier unchecked `auto off`) goes too.
    wants = os.path.join(home, WANTS_REL)
    if os.path.islink(wants) and not os.path.exists(wants):
        os.unlink(wants)
        say(f"removed:   ~/{WANTS_REL} (it pointed at the removed timer file)")
        removed = True
    if removed and reload_failed():
        rc = 1
    for rel in copies():
        if st[rel] in ("ok", "change"):
            os.unlink(os.path.join(home, rel))
            say(f"removed:   ~/{rel}")
        elif st[rel] == "conflict":
            say(f"left alone: ~/{rel} (not written by CST)", 4, err=True)
    forget_auto_files(home)
    try:
        os.rmdir(os.path.join(home, LIB_REL))  # only when empty (bluetooth keeps its watcher there)
    except OSError:
        pass
    for tid in TARGETS:
        try:
            os.unlink(state_path(home, tid))
        except OSError:
            pass
    if not turned_off(home):
        write_atomic(
            os.path.join(home, OFF_REL),
            f"{MARKER}: written by `cst toolchains auto off`.\n"
            "# While this file exists, `cst btrfs snapshots setup` does not turn the hourly\n"
            "# pruning of toolchain caches back on. `cst toolchains auto on` removes it.\n",
            mode=0o644,
        )
        say(f"written:   ~/{OFF_REL} (snapshots setup leaves the pruning off)")
    say(f"toolchain caches are no longer pruned automatically (settings in ~/{CONF_REL} kept, if any)")
    return rc


def timer_text(home: str) -> str:
    if file_state(os.path.join(home, UNIT_DIR_REL, TIMER), render_timer()) == "missing":
        if systemctl("is-active", TIMER) == "active":
            return (
                f"timer {TIMER}: still running although its file is gone (left by an earlier auto off); "
                f"{PROG} auto off stops it"
            )
        if turned_off(home):
            return (
                f"timer {TIMER}: turned off by you ({PROG} auto off; "
                f"snapshots setup leaves it off; {PROG} auto on turns it on)"
            )
        return f"timer {TIMER}: not installed ({PROG} auto on)"
    en = systemctl("is-enabled", TIMER)
    ac = systemctl("is-active", TIMER)
    txt = f"timer {TIMER}: {en or 'unknown'}, {ac or 'unknown'}"
    if ac == "active":
        nxt = systemctl("show", "-p", "NextElapseUSecRealtime", "--value", TIMER)
        if nxt and nxt not in ("n/a", "0"):
            txt += f", next {nxt}"
    return txt

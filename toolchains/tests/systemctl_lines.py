# SPDX-License-Identifier: MPL-2.0
"""A test helper: one systemctl --user command line, as cst-toolchains makes them, run through the
adapter the program builds (toolchains.adapters.units.user_systemctl) and read as UserUnits reads
its answers (said). The tests of the stub program, the session environment, the host reach and
decoding name a call by its command line with it."""

from collections.abc import Sequence

from cst_core.adapters import SystemctlAdapter
from cst_core.ports import CommandResult, UnitName
from toolchains.adapters.units import said, user_systemctl
from toolchains.domain.auto import UserManagerVerb


def _result(manager: SystemctlAdapter, args: Sequence[str]) -> CommandResult:
    """The adapter's call for one systemctl command line (ARGS without systemctl --user)."""
    match args:
        case (UserManagerVerb.IS_ACTIVE, unit):
            res = manager.is_active(UnitName(unit))
        case (UserManagerVerb.IS_ENABLED, unit):
            res = manager.is_enabled(UnitName(unit))
        case (UserManagerVerb.SHOW, "-p", prop, "--value", unit):
            res = manager.show_value(UnitName(unit), prop)
        case (UserManagerVerb.SHOW_ENVIRONMENT,):
            res = manager.show_environment()
        case (UserManagerVerb.START, unit):
            res = manager.start(UnitName(unit))
        case (UserManagerVerb.STOP, unit):
            res = manager.stop(UnitName(unit))
        case (UserManagerVerb.ENABLE, "--now", unit):
            res = manager.enable(UnitName(unit), now=True)
        case (UserManagerVerb.DISABLE, "--now", unit):
            res = manager.disable(UnitName(unit), now=True)
        case (UserManagerVerb.CLEAN, flag, unit) if flag.startswith("--what="):
            res = manager.clean(UnitName(unit), what=flag.removeprefix("--what="))
        case (UserManagerVerb.DAEMON_RELOAD,):
            res = manager.daemon_reload_result()
        case _:
            raise ValueError(f"systemctl --user {' '.join(args)}: not a call cst-toolchains makes")
    return res


def systemctl(*args: str, program: str | None, check: bool = False, ok: Sequence[int] = (0,)) -> str:
    """Output (stripped) of systemctl --user ARGS (PROGRAM: a stub run instead; None: the real one);
    '' when it could not run; check: Refused when it could not run or exited with a code not in ok."""
    return said(_result(user_systemctl(program), args), args, check=check, ok=ok)

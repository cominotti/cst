# SPDX-License-Identifier: MPL-2.0
"""The composition root of `cst bluetooth`: the environment, the re-runs and the adapters.

bluetooth/setup/cli.py parses argv and asks this module for what it needs
from the process: the environment (what bluetooth-setup.sh hands over, the
user's folders, BT_TIMEOUT and the test overrides CST_BT_CONTAINER and
CST_BT_ROOT), the step down from root to SUDO_USER (runuser), the run again
of the program for le-audio's apply, and the use cases over the real adapters.
"""

from collections.abc import Mapping, Sequence
import os
import sys
from typing import NoReturn

from bluetooth.adapters import LocalBluetoothHostAdapter
from bluetooth.setup.adapters import (
    LocalSetupFiles,
    SetupTerminal,
    account,
    effective_uid,
    guarded,
    print_exit,
)
from bluetooth.setup.application.change import Change, ReRun
from bluetooth.setup.application.survey import SetupMachine
from bluetooth.setup.domain import Config, Exit, Options, Paths
from cst_core.adapters import SystemClockAdapter
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.adapters.runtime import SYSTEM_PYTHON, checkout_root, lc_ctype_handover, undo_python_start
from cst_core.adapters.files import read_text
from cst_core.domain.dispatch import script_help, step_down_argv


def start() -> Mapping[str, str]:
    """The process as bluetooth-setup.sh left it: SIGPIPE at its default, the caller's LC_CTYPE
    back, what the shim handed over taken out of the environment (the commands it runs do not
    get it), and output that writes any file name it reads. Returns the environment."""
    undo_python_start()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="surrogateescape")
    return os.environ


def prog(env: Mapping[str, str]) -> str:
    """The name in the help and the hints: CST_PROG (cst sets it), else the name the script was run as."""
    argv0 = os.environ.pop("CST_ARG0", None)
    return env.get("CST_PROG") or argv0 or "bluetooth-setup.sh"


def checkout(entry: str) -> str:
    """The CST folder the program runs from."""
    root = checkout_root(entry)
    if root is None:
        raise SystemExit(f"{entry}: not in a CST checkout")
    return root


def usage(entry: str, prog_name: str) -> str:
    """The help: bluetooth-setup.sh's header comment, then the usage line."""
    return script_help(read_text(os.path.join(checkout(entry), "bluetooth-setup.sh"), ""), prog_name, words="[options]")


def step_down(env: Mapping[str, str], entry: str, opts: Options, prog_name: str) -> Exit | None:
    """As root (through sudo, also `cst status`): run again as SUDO_USER, or the error; None as a user."""
    if effective_uid() != 0:
        return None
    user = env.get("SUDO_USER", "")
    if not user or user == "root":
        return Exit(
            1,
            err=f"{prog_name}: ERROR: run it from your user account (needs SUDO_USER): the WirePlumber parts belong to your session; it calls sudo by itself for main.conf\n",
        )
    found = account(user)
    if found is None:
        return Exit(1, err=f"id: '{user}': no such user\n")
    uid, home = found
    handed = [f"CST_PROG={env['CST_PROG']}"] if env.get("CST_PROG") else []
    self_script = os.path.join(checkout(entry), "bluetooth-setup.sh")
    argv = step_down_argv(
        user,
        uid,
        home,
        unset=["XDG_CONFIG_HOME", "CST_BT_ROOT", "CST_BT_CONTAINER"],
        then=[*handed, self_script, *opts.args],
    )
    replace(argv)


def replace(argv: Sequence[str]) -> NoReturn:
    """This process becomes argv (exec), as the shell's exec did."""
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(argv[0], list(argv))


def in_toolbox(env: Mapping[str, str]) -> bool:
    """Inside a toolbox: CST_BT_CONTAINER (1/0, the tests'), else what cst_core detects."""
    forced = env.get("CST_BT_CONTAINER", "")
    if forced in ("0", "1"):
        return forced == "1"
    return HostExecutionContext().is_container()


def paths(env: Mapping[str, str], entry: str, *, container: bool) -> Paths:
    """The user's folders and the host's root (CST_BT_ROOT, else /run/host in a toolbox)."""
    home = env.get("HOME", "")
    root = env.get("CST_BT_ROOT") or ("/run/host" if container else "")
    return Paths(
        home=home,
        config=env.get("XDG_CONFIG_HOME") or f"{home}/.config",
        etc=f"{root}/etc",
        usr_share=f"{root}/usr/share",
        checkout=checkout(entry),
    )


def bt_host() -> LocalBluetoothHostAdapter:
    """bt-helper's reads, on this machine."""
    return LocalBluetoothHostAdapter()


def build(opts: Options, cfg: Config, where: Paths, *, container: bool, bt_timeout: int) -> Change:
    """The use cases over this machine."""
    probe = HostExecutionContext(exists=lambda _path: container)
    return Change(
        opts,
        cfg,
        where,
        SetupMachine(
            runner=SubprocessCommandRunner(probe),
            clock=SystemClockAdapter(),
            console=SetupTerminal(),
            files=LocalSetupFiles(),
            host=LocalBluetoothHostAdapter(),
            container=container,
            bt_timeout=bt_timeout,
            lc_ctype=lc_ctype_handover().split("=", 1)[1],
            python=SYSTEM_PYTHON,
        ),
    )


def emit(done: Exit) -> int:
    """Print an Exit (the help, a usage error, a refusal) and return its status (adapters.print_exit)."""
    return print_exit(done.out, done.err, done.status)


def run_guarded(run: Change) -> int | ReRun:
    """run's command, its scratch file removed however the run ends, as the shell's EXIT trap
    removed it (adapters.guarded: Ctrl-C, SIGHUP, SIGTERM and a closed stdout end it killed by
    that signal)."""
    return guarded(run.run_command, run.cleanup)

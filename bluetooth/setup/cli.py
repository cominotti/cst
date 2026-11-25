# SPDX-License-Identifier: MPL-2.0
"""The command line of `cst bluetooth` (bluetooth-setup.sh): its arguments, the run's
settings, one use case, the exit status."""

from collections.abc import Sequence
import sys

from bluetooth.setup import wiring
from bluetooth.setup.application.change import ReRun
from bluetooth.setup.application.inputs import read_bluetooth_settings
from bluetooth.setup.domain import Exit, bt_timeout, changes_machine, parse_command_line, resolve


def main(entry: str, argv: Sequence[str]) -> int:
    """Run one command of `cst bluetooth` (entry: the program's file); its exit status."""
    env = wiring.take_handover()
    prog = wiring.prog(env)
    opts = parse_command_line(argv, prog, wiring.usage(entry, prog))
    if isinstance(opts, Exit):
        return wiring.emit(opts)
    stepped = wiring.step_down(env, entry, opts, prog)
    if stepped is not None:
        return wiring.emit(stepped)
    container = wiring.in_toolbox(env)
    if changes_machine(opts) and container:
        return wiring.emit(
            Exit(1, err=f"'{prog} {' '.join(opts.args)}' must run on the host, not inside a toolbox/container.\n")
        )
    timeout = bt_timeout(env.get("BT_TIMEOUT"))
    if isinstance(timeout, Exit):
        return wiring.emit(timeout)
    where = wiring.paths(env, entry, container=container)
    found = read_bluetooth_settings(wiring.bt_host(), where)
    if isinstance(found, Exit):
        return wiring.emit(found)
    settings, user_keys, warnings = found
    sys.stderr.write(warnings)
    cfg = resolve(opts, settings, user_keys, where, prog)
    if isinstance(cfg, Exit):
        return wiring.emit(cfg)
    run = wiring.build(opts, cfg, where, container=container, bt_timeout=timeout)
    result = wiring.run_guarded(run)
    if isinstance(result, ReRun):
        wiring.replace([where.self_script, *result.args])
    return result

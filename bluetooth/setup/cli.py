# SPDX-License-Identifier: MPL-2.0
"""The command line of `cst bluetooth` (bluetooth-setup.sh): its arguments, the run's
settings, one use case, the exit status."""

from collections.abc import Sequence
import sys

from bluetooth.setup import wiring
from bluetooth.setup.application.change import Change, ReRun
from bluetooth.setup.application.survey import StepFailed, read_settings
from cst_core.domain.errors import Refused
from bluetooth.setup.domain import Exit, Options, bt_timeout, changes_machine, parse_command_line, resolve


def emit(done: Exit) -> int:
    """Print an Exit and return its status."""
    sys.stdout.write(done.out)
    sys.stderr.write(done.err)
    return done.status


def main(entry: str, argv: Sequence[str]) -> int:
    """Run one command of `cst bluetooth` (entry: the program's file); its exit status."""
    env = wiring.start()
    prog = wiring.prog(env)
    opts = parse_command_line(argv, prog, wiring.usage(entry, prog))
    if isinstance(opts, Exit):
        return emit(opts)
    stepped = wiring.step_down(env, entry, opts, prog)
    if stepped is not None:
        return emit(stepped)
    container = wiring.in_toolbox(env)
    if changes_machine(opts) and container:
        return emit(
            Exit(1, err=f"'{prog} {' '.join(opts.args)}' must run on the host, not inside a toolbox/container.\n")
        )
    timeout = bt_timeout(env.get("BT_TIMEOUT"))
    if isinstance(timeout, Exit):
        return emit(timeout)
    where = wiring.paths(env, entry, container=container)
    settings, user_keys, warnings = read_settings(wiring.bt_host(), where)
    sys.stderr.write(warnings)
    cfg = resolve(opts, settings, user_keys, where, prog)
    if isinstance(cfg, Exit):
        return emit(cfg)
    run = wiring.build(opts, cfg, where, container=container, bt_timeout=timeout)
    result = execute(run, opts, prog)
    if isinstance(result, ReRun):
        wiring.replace([where.self_script, *result.args])
    return result


def execute(run: Change, opts: Options, prog: str) -> int | ReRun:
    """One command over the machine; its exit status (a stop prints why), or the run again of le-audio."""
    # apply and revert name the backup and the undo when a command stops them (the ERR trap of old).
    changing = opts.cmd == "apply" or (opts.cmd == "revert" and not opts.check)
    stopped = f"Backup of main.conf: {{}}. Undo: {prog} revert"
    result: int | ReRun = 1
    try:
        if opts.cmd == "plan":
            result = run.plan()
        elif opts.cmd == "status":
            result = run.status()
        elif opts.cmd == "apply":
            result = run.apply()
        elif opts.cmd == "revert":
            result = run.revert()
        else:
            result = run.le_audio()
    except Refused as e:
        sys.stderr.write(f"{prog}: ERROR: {e}\n")
        result = 1
    except (StepFailed, OSError) as e:
        status = e.status if isinstance(e, StepFailed) else 1
        if isinstance(e, OSError):
            sys.stderr.write(f"{e}\n")
        if changing:
            sys.stderr.write(
                f"{prog}: stopped: a command failed (exit {status}). {stopped.format(run.backup or 'none taken')}\n"
            )
        result = status
    except KeyboardInterrupt:
        run.cleanup()
        wiring.interrupted()
    finally:
        run.cleanup()
    return result

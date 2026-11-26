# SPDX-License-Identifier: MPL-2.0
"""`cst cpu experiment`: list, turn on and restore runtime-only memory knobs."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import os
from typing import Final

from cst_core.adapters.files import read_text, write_atomic
from cpu.setup.domain import Halt, SetupPaths, current_choice, experiment_line, first_arg

_UTF8: Final = "utf-8"


@dataclass(frozen=True, slots=True)
class ExperimentEnv:
    """Host callbacks and paths shared across `cst cpu experiment` actions."""

    paths: SetupPaths
    prog: str
    say: Callable[[str], None]
    warn: Callable[[str], None]
    echo_to: Callable[[str, str], bool]
    make_run_dir: Callable[[str], None]


def run_experiment(env: ExperimentEnv, args: Sequence[str]) -> int:
    """experiment list|on NAME|off NAME: runtime-only memory knobs."""
    action = first_arg(args, "list")
    name = args[1] if len(args) > 1 else ""
    if action == "list":
        return _experiment_list(env)
    if action not in ("on", "off"):
        raise Halt(2, "experiment: list|on NAME|off NAME")
    if not name:
        raise Halt(1, "cpu-setup.sh: EXPERIMENTS: bad array subscript")
    spec = env.paths.experiments.get(name)
    if spec is None:
        raise Halt(2, f"experiment: unknown '{name}' (see: {env.prog} experiment list)")
    file, value = spec
    saved = env.paths.saved(name)
    if action == "on":
        return _experiment_on(env, name, (file, value), saved)
    return _experiment_off(env, name, file, saved)


def _experiment_list(env: ExperimentEnv) -> int:
    """experiment list: each knob, its value now, and whether it is on."""
    for exp, (file, value) in env.paths.experiments.items():
        try:
            with open(file, encoding=_UTF8, errors="replace") as fh:
                now = fh.read().replace("\n", "")
        except OSError as e:
            env.warn(f"cpu-setup.sh: {file}: {e.strerror}")
            now = ""
        env.say(experiment_line(exp, file, value, now, on=os.path.exists(env.paths.saved(exp))))
    return 0


def _experiment_on(env: ExperimentEnv, name: str, spec: tuple[str, str], saved: str) -> int:
    """experiment on NAME: the value it replaces saved (the first time), then VALUE into FILE."""
    file, value = spec
    env.make_run_dir(env.paths.run)
    if not os.path.exists(saved):
        _save_choice(file, saved)
    if not env.echo_to(file, value):
        raise Halt(1)
    env.say(f"{name}: on ({file} = {value}; runtime only, reverted by '{env.prog} experiment off {name}' or a reboot)")
    return 0


def _experiment_off(env: ExperimentEnv, name: str, file: str, saved: str) -> int:
    """experiment off NAME: the saved value back into FILE."""
    if not os.path.exists(saved):
        env.say(f"{name}: not on")
        return 0
    old = read_text(saved) or ""
    try:
        with open(file, "w", encoding=_UTF8) as fh:
            fh.write(old)
    except OSError as e:
        env.warn(f"cpu-setup.sh: {file}: {e.strerror}")
        raise Halt(1, f"{name}: could not restore {old.rstrip('\n')} into {file}") from None
    os.remove(saved)
    env.say(f"{name}: off ({file} restored)")
    return 0


def _save_choice(file: str, saved: str) -> None:
    """The value an experiment replaces, for 'off' (sysfs THP shows "[current]" among the choices).
    Read first: a value that cannot be read saves nothing (off then has nothing to put back)."""
    try:
        with open(file, encoding=_UTF8, errors="replace") as fh:
            value = current_choice(fh.read())
    except OSError as e:
        raise Halt(2, f"sed: can't read {file}: {e.strerror}") from None
    write_atomic(saved, value, mode="umask", durable=False, relabel=False)

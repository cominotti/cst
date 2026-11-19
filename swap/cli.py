#!/usr/bin/python3 -I
# SPDX-License-Identifier: MPL-2.0
"""Command-line entrypoint for `cst swap` (`swap-setup.sh`)."""

import os.path as _cst_path
import sys as _cst_sys

# CST bootstrap (the same in every entry point; tests/test_entrypoints.py checks
# it): from a checkout, cst_core and the module packages are in the folder above
# this file, and Python writes no bytecode there (no __pycache__ in the checkout,
# even for a program run by its shebang as root); inside an installed zipapp,
# sys.path already holds the archive.
_cst_root = _cst_path.dirname(_cst_path.dirname(_cst_path.realpath(__file__)))
if not _cst_path.isfile(_cst_path.dirname(__file__)) and _cst_root not in _cst_sys.path:
    _cst_sys.dont_write_bytecode = True
    _cst_sys.path.insert(0, _cst_root)

from collections.abc import Callable, Mapping, Sequence
import sys
from typing import NoReturn, TextIO

from cst_core.ports import CommandRunnerPort, ExecutionContextPort, FileSystemPort
from swap import wiring
from swap.domain import SwapCommand, parse_swap_command
from swap.presenter import run_command

_HELP_TEXT = """\
swap-setup.sh  (the `swap` module of CST: run it as `cst swap ...`)

Swap and memory of this workstation (93 GiB RAM, a 48 GiB Windows VM):
zswap, a compressed cache in RAM, in front of an encrypted swap partition
on NVMe (swap0), zram off, swap-friendly VM sysctls, and a memory floor for
the VM. Replaces the standalone apply-swap-config.sh; what that script
wrote is recognised and adopted. Desired files live in ./swap/; the
rationale is in swap/README.md.

Commands:
  plan [--root]          what apply would change (dry run). Works as your
                         user, also in a toolbox; only /etc/crypttab needs
                         root to read (--root: through sudo, on the host)
  apply                  (root; partition in /etc/cst/swap.conf) idempotent:
      * swap0: a marked line in /etc/crypttab (plain dm-crypt, new random
        key every boot) and one in /etc/fstab; activated if not active
      * zswap at boot: /etc/tmpfiles.d/zswap.conf (zstd, pool up to 20 %
        of RAM, shrinker on), also set live
      * zram off: vendor zram-generator.conf masked; zram0 swapped off now
        only when free RAM can take its contents
      * /etc/sysctl.d/99-swap-tuning.conf (swappiness 100, page-cluster 0,
        watermarks), applied live
      * machine.slice MemoryLow=48G (drop-in 10-swap-memory-low.conf, next to
        cpu's 10-cpu-setup.conf; set-property still wins; old 50- name removed)
      * verification at the end
  status [--root]        read-only: config files, live settings, zswap pool
                         and traffic, swap devices, per-cgroup swap, memory
                         pressure. Works as your user, also in a toolbox;
                         the zswap debug counters need root (--root)
  help

apply runs on the host only. Nothing here changes memory.zswap.writeback."""


def format_usage(prog: str) -> str:
    """The help text and usage line for prog, laid out as swap-setup.sh always printed them."""
    return f"\n{_HELP_TEXT}\n\nUsage: {prog} <command> [args]\n"


def execute_cli(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    runner: CommandRunnerPort | None = None,
    fs: FileSystemPort | None = None,
    ctx: ExecutionContextPort | None = None,
    elevate: Callable[[list[str]], NoReturn] | None = None,
) -> int:
    """Parse arguments, enforce host/root preconditions, and dispatch swap use cases."""
    args = list(sys.argv[1:] if argv is None else argv)
    env_map = wiring.swap_env() if env is None else env
    stdout = sys.stdout if out is None else out
    stderr = sys.stderr if err is None else err
    prog = env_map.get("CST_PROG") or "cst swap"

    cmd = args[0] if args and args[0] else "help"  # bash's ${1:-help}: an empty word is help too
    rest = args[1:] if args else []
    orig_args = [cmd, *rest]

    match parse_swap_command(cmd):
        case SwapCommand.HELP:
            stdout.write(format_usage(prog))
            return 0
        case (SwapCommand.PLAN | SwapCommand.STATUS) as command:
            first = rest[0] if rest else ""  # only the first word counts, as in swap-setup.sh
            want_root = first == "--root"
            if first and not want_root:
                print(f"{cmd}: unknown argument '{first}' (only --root)", file=stderr)
                return 2
        case SwapCommand.APPLY as command:
            if rest:
                print("apply: no arguments", file=stderr)
                return 2
            want_root = True
        case None:
            print(f"Unknown command: {cmd}", file=stderr)
            stderr.write(format_usage(prog))
            return 2

    exec_ctx = wiring.execution_context() if ctx is None else ctx
    if want_root:
        if exec_ctx.is_container():
            print(f"'{prog} {' '.join(orig_args)}' must run on the host, not inside a toolbox/container.", file=stderr)
            return 1
        if not exec_ctx.is_root():
            reexec = wiring.sudo_reexec if elevate is None else elevate
            reexec(wiring.sudo_rerun_argv(prog, orig_args))

    service = wiring.build_service(prog, exec_ctx, runner, fs, fallback_root=_cst_root, env=env_map)
    return run_command(command, service, hint=f"{prog} {cmd} --root, on the host", out=stdout, err=stderr)


def main() -> int:
    """CLI entrypoint returning process exit status."""
    wiring.start_swap()
    return execute_cli()


if __name__ == "__main__":
    sys.exit(main())

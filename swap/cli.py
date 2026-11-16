#!/usr/bin/python3 -I
"""Command-line entrypoint for `cst swap` (`swap-setup.sh`)."""

from collections.abc import Callable, Mapping, Sequence
import os
import sys
from typing import NoReturn, TextIO

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from cst_core.adapters import HostExecutionContext, LocalFileSystemAdapter, SubprocessCommandRunner, SystemctlAdapter
from cst_core.ports import CommandRunnerPort, ExecutionContextPort, FileSystemPort
from swap.adapters import LinuxSwapSystemAdapter
from swap.application import MissingDesiredFileError, SwapApplicationService, SwapEnvironment
from swap.presenter import render_apply, render_plan, render_status

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
  apply                  (root) idempotent:
      * swap0: a marked line in /etc/crypttab (plain dm-crypt, new random
        key every boot) and one in /etc/fstab; activated if not active
      * zswap at boot: /etc/tmpfiles.d/zswap.conf (zstd, pool up to 20 %
        of RAM, shrinker on), also set live
      * zram off: vendor zram-generator.conf masked; zram0 swapped off now
        only when free RAM can take its contents
      * /etc/sysctl.d/99-swap-tuning.conf (swappiness 100, page-cluster 0,
        watermarks), applied live
      * machine.slice MemoryLow=48G (drop-in 50-memory-low.conf, next to
        cpu-setup.sh's 10-cpu-setup.conf; different settings, no clash)
      * verification at the end
  status [--root]        read-only: config files, live settings, zswap pool
                         and traffic, swap devices, per-cgroup swap, memory
                         pressure. Works as your user, also in a toolbox;
                         the zswap debug counters need root (--root)
  help

apply runs on the host only. Nothing here changes memory.zswap.writeback."""


def format_usage(prog: str) -> str:
    """Format the help text and usage line for prog."""
    return f"{_HELP_TEXT}\nUsage: {prog} <command> [args]\n"


def _sudo_reexec(argv: list[str]) -> NoReturn:
    os.execvp("sudo", argv)


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
    env_map = os.environ if env is None else env
    stdout = sys.stdout if out is None else out
    stderr = sys.stderr if err is None else err
    prog = env_map.get("CST_PROG") or "cst swap"

    cmd = args[0] if args else "help"
    rest = args[1:] if args else []
    orig_args = [cmd, *rest]
    want_root = False

    if cmd in ("help", "-h", "--help"):
        stdout.write(format_usage(prog))
        return 0
    if cmd in ("plan", "status"):
        if not rest:
            pass
        elif rest == ["--root"]:
            want_root = True
        else:
            print(f"{cmd}: unknown argument '{rest[0]}' (only --root)", file=stderr)
            return 2
    elif cmd == "apply":
        if rest:
            print("apply: no arguments", file=stderr)
            return 2
        want_root = True
    else:
        print(f"Unknown command: {cmd}", file=stderr)
        stderr.write(format_usage(prog))
        return 2

    exec_ctx = HostExecutionContext() if ctx is None else ctx
    if want_root:
        if exec_ctx.is_container():
            print(f"'{prog} {' '.join(orig_args)}' must run on the host, not inside a toolbox/container.", file=stderr)
            return 1
        if not exec_ctx.is_root():
            reexec = _sudo_reexec if elevate is None else elevate
            reexec(
                [
                    "sudo",
                    "--",
                    "env",
                    f"CST_PROG={prog}",
                    sys.executable,
                    "-I",
                    "-B",
                    os.path.abspath(__file__),
                    *orig_args,
                ]
            )

    cmd_runner = SubprocessCommandRunner() if runner is None else runner
    file_sys = LocalFileSystemAdapter() if fs is None else fs
    systemd = SystemctlAdapter(cmd_runner)
    swap_system = LinuxSwapSystemAdapter(cmd_runner, file_sys)
    swap_env = SwapEnvironment(
        src_dir=os.path.dirname(os.path.abspath(__file__)),
        etc_dir=exec_ctx.etc_dir(),
        cpu_run_dir=exec_ctx.host_run_dir("cpu-setup"),
        prog=prog,
    )
    service = SwapApplicationService(swap_env, file_sys, systemd, swap_system)
    hint = f"{prog} {cmd} --root, on the host"

    try:
        if cmd == "plan":
            return render_plan(service.plan_report(), out=stdout)
        if cmd == "status":
            return render_status(
                service.status_report(hint=hint, include_config=True),
                out=stdout,
            )
        return render_apply(service.apply_changes(), out=stdout, err=stderr)
    except MissingDesiredFileError as exc:
        print(f"{prog}: ERROR: {exc}", file=stderr)
        return 1


def main() -> int:
    """CLI entrypoint returning process exit status."""
    return execute_cli()


if __name__ == "__main__":
    sys.exit(main())

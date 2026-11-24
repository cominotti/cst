# SPDX-License-Identifier: MPL-2.0
"""Command-line frontend for `cst btrfs compress-force`."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import sys
from typing import TextIO

from btrfs.compress import presenter, wiring
from btrfs.compress.domain.args import ArgsError, CompressCommand, parse_compress_args

_HELP_TEXT = """\
Root btrfs zstd compression mode across /etc/fstab, rpm-ostree kargs, live mount, and btrfs:top-mount.

Commands:
  status                    read-only: show live, fstab, kargs, and top-mount compression + coherence
                            (works as your user and inside a toolbox; default when no command is given)
  plan [--level N] [--revert]
                            dry run of the changes `apply` (or `revert`) would perform; changes nothing
                            (works as your user and inside a toolbox)
  apply [--level N] [--yes]
                            (root, host only) switch all active layers to compress-force=zstd:N
                            (default level: existing zstd level or 1); asks confirmation unless --yes
  revert [--level N] [--yes]
                            (root, host only) switch all active layers back to compress=zstd:N;
                            asks confirmation unless --yes
  help                      show this help

Never runs automatic `btrfs filesystem defragment` on existing files."""


@dataclass(frozen=True, slots=True)
class CompressCliStreams:
    """Output/environment bundle for testability of `compress_cli`."""

    env: Mapping[str, str] | None = None
    out: TextIO | None = None
    err: TextIO | None = None


def render_compress_usage(prog: str) -> str:
    """Help text and usage line for `prog`."""
    return f"{_HELP_TEXT}\n\nUsage: {prog} <status|plan|apply|revert|help> [options]\n"


def _read_env_and_prog(io_cfg: CompressCliStreams) -> tuple[Mapping[str, str], str]:
    env_map = wiring.read_compress_env() if io_cfg.env is None else io_cfg.env
    prog = env_map.get("CST_PROG") or "cst btrfs compress-force"
    return env_map, prog


def compress_cli(
    argv: Sequence[str] | None = None,
    *,
    entry_file: str = "",
    streams: CompressCliStreams | None = None,
    overrides: wiring.CompressWiringOverrides | None = None,
) -> int:
    """Parse arguments, enforce container/root rules, and run the chosen use case."""
    args = list(sys.argv[1:] if argv is None else argv)
    io_cfg = CompressCliStreams() if streams is None else streams
    env_map, prog = _read_env_and_prog(io_cfg)
    stdout = sys.stdout if io_cfg.out is None else io_cfg.out
    stderr = sys.stderr if io_cfg.err is None else io_cfg.err

    parsed = parse_compress_args(args)
    if isinstance(parsed, ArgsError):
        print(f"{prog}: {parsed.message}", file=stderr)
        stderr.write("\n" + render_compress_usage(prog))
        return parsed.code
    if parsed.command == CompressCommand.HELP:
        stdout.write(render_compress_usage(prog))
        return 0

    ov = wiring.CompressWiringOverrides() if overrides is None else overrides
    refusal = wiring.ensure_host_root(parsed.command, prog, args, entry_file, ov)
    if refusal is not None:
        print(refusal, file=stderr)
        return 1

    service = wiring.assemble_compress_service(entry_file, env_map, ov)
    match parsed.command:
        case CompressCommand.STATUS:
            return presenter.render_compress_status(service.gather_status(), stdout)
        case CompressCommand.PLAN:
            return presenter.render_compress_plan(service.build_change_plan(parsed), prog, stdout, stderr)
        case _:
            return presenter.render_compress_change(service.execute_change(parsed), stdout, stderr)


def main(argv: Sequence[str] | None = None, entry_file: str = "") -> int:
    """Entry point delegating to `compress_cli`."""
    wiring.start_compress_process()
    return compress_cli(argv, entry_file=entry_file)

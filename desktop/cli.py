# SPDX-License-Identifier: MPL-2.0
"""Thin CLI entrypoint for `cst desktop` (`desktop/cst-desktop`)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TextIO

from desktop.domain import DESKTOP_PROG, DesktopCommand, parse_desktop_args
from desktop.presenter import render_desktop_output
from desktop.wiring import DesktopPorts, desktop_prog, prepare_cli_streams, run_desktop_command


def format_desktop_usage(doc: str, prog: str) -> str:
    """Format usage text replacing `cst desktop` with invoked `prog` when set."""
    if doc.strip():
        return doc.replace(DESKTOP_PROG, prog)
    return f"Usage: {prog} <status|gnome localsearch|psd|help> [options]\nRun `{prog} help` for details.\n"


def main(
    argv: Sequence[str],
    *,
    doc: str = "",
    entry_file: str = "",
    ports: DesktopPorts | None = None,
    streams: tuple[TextIO, TextIO] | None = None,
) -> int:
    """Parse arguments, execute `cst desktop` use cases, and render output."""
    out_fh, err_fh = prepare_cli_streams(streams)
    prog = desktop_prog()
    parsed = parse_desktop_args(argv)
    if parsed.error is not None:
        err_fh.write(f"{prog}: {parsed.error}\n\n{format_desktop_usage(doc, prog)}")
        return parsed.code
    if parsed.command == DesktopCommand.HELP:
        out_fh.write(format_desktop_usage(doc, prog))
        return 0
    rc, lines = run_desktop_command(parsed, entry_file=entry_file, ports=ports)
    return render_desktop_output(lines, rc, out_fh, err_fh)

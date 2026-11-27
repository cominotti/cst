# SPDX-License-Identifier: MPL-2.0
"""Composition root for `cst desktop` (`wiring.py`): builds adapters from environment and runs use cases."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import TextIO

from cst_core.adapters.runtime import undo_python_start
from cst_core.adapters.terminal import stderr, stdout
from cst_core.domain.values import ExitAndLines
from desktop.adapters.localsearch import LinuxLocalsearchHost
from desktop.adapters.psd import LinuxPsdHost
from desktop.application.localsearch import LocalsearchService
from desktop.application.psd import PsdRequest, PsdService
from desktop.domain import DESKTOP_PROG, DesktopCommand, DesktopParsedArgs
from desktop.ports import LocalsearchHostPort, PsdHostPort


@dataclass(frozen=True, slots=True)
class DesktopPorts:
    """Optional test fakes injected at the composition root."""

    localsearch: LocalsearchHostPort | None = None
    psd: PsdHostPort | None = None


def desktop_prog() -> str:
    """Resolve `CST_PROG` header/hint name (`cst desktop` when invoked directly or through `cst`)."""
    return os.environ.get("CST_PROG") or DESKTOP_PROG


def prepare_cli_streams(streams: tuple[TextIO, TextIO] | None) -> tuple[TextIO, TextIO]:
    """Clear env propagation on startup and return `(out, err)` streams."""
    undo_python_start()
    return (stdout(), stderr()) if streams is None else streams


def run_desktop_command(
    parsed: DesktopParsedArgs,
    *,
    entry_file: str = "",
    ports: DesktopPorts | None = None,
) -> ExitAndLines:
    """Instantiate services and execute `status`, `gnome localsearch`, or `psd`."""
    prog = desktop_prog()
    injected = DesktopPorts() if ports is None else ports
    ls_host = injected.localsearch if injected.localsearch is not None else LinuxLocalsearchHost()
    psd_host = injected.psd if injected.psd is not None else LinuxPsdHost(entry_file)

    ls_svc = LocalsearchService(ls_host, prog=prog)
    psd_svc = PsdService(psd_host, prog=prog)

    if parsed.command == DesktopCommand.STATUS:
        lines = [*ls_svc.status_lines(), "", *psd_svc.status_lines(user=parsed.user)]
        return 0, lines
    if parsed.command == DesktopCommand.GNOME:
        return ls_svc.run(parsed.ls_action, mode=parsed.mode)
    req = PsdRequest(
        action=parsed.psd_action,
        user=parsed.user,
        profiles=parsed.profiles,
        size_text=parsed.size_text,
        discard=parsed.discard,
        skip_busy=parsed.skip_busy,
    )
    return psd_svc.run(req)

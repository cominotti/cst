# SPDX-License-Identifier: MPL-2.0
"""The commands of cst-toolchains (`cst toolchains ...`): main() parses the
command line, asks toolchains/wiring.py for the session and runs one command
group (prune, auto, config, status, tmpfs), under the run lock where it changes user files.
The program toolchains/cst-toolchains only hands its arguments to main().
"""

from __future__ import annotations

import sys
from typing import assert_never

from collections.abc import Sequence

from cst_core.domain.errors import Refused, Usage
from cst_core.ports import WriteFailed
from toolchains import wiring
from toolchains.cli import auto, config, prune, status
from toolchains.domain.common import NAME, USAGE, ConfigError, ToolchainsAction, ToolchainsCommand, parse_args


def main(argv: Sequence[str], doc: str, entry_file: str, *, parts: wiring.Parts | None = None) -> int:
    """`cst toolchains ...` (argv without the program name). doc is the
    program's help; entry_file the running cst-toolchains; parts what a test
    gives instead of the real adapters."""
    try:
        a = parse_args(argv)
    except Usage as e:
        print(f"{NAME}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    wiring.set_output_mode(a["cmd"], a["action"], plan=a["plan"])
    if a["cmd"] == ToolchainsCommand.HELP:
        print(doc)
        return 0
    if a["cmd"] == ToolchainsCommand.TMPFS:
        return wiring.run_tmpfs(a, entry_file, parts)
    if wiring.run_by_root():
        print(f"{NAME}: run as your user, not as root", file=sys.stderr)
        return 1
    if (rc := wiring.reexec_on_host(argv, entry_file)) is not None:
        return rc
    s = wiring.session(a["home"], entry_file, parts)
    try:
        if a["cmd"] == ToolchainsCommand.STATUS:
            status.cmd_status(s, a["brief"])
            return 0
        assert a["action"] is not None
        match a["cmd"]:
            case ToolchainsCommand.CONFIG:
                return config.cmd_config(s, a)
            case ToolchainsCommand.PRUNE if a["action"] == ToolchainsAction.PLAN:
                return prune.cmd_prune(s, ToolchainsAction.PLAN, a["targets"])
            case ToolchainsCommand.PRUNE:
                return s.lock.run(lambda: prune.cmd_prune(s, ToolchainsAction.APPLY, a["targets"]))
            case ToolchainsCommand.AUTO if a["action"] == ToolchainsAction.ON:
                return s.lock.run(lambda: auto.cmd_auto_on(s, unless_off=a["unless_off"]), busy_refused=True)
            case ToolchainsCommand.AUTO:
                return s.lock.run(lambda: auto.cmd_auto_off(s), busy_refused=True)
            case ToolchainsCommand.HELP | ToolchainsCommand.TMPFS:  # handled above
                return 0
            case _:
                assert_never(a["cmd"])
    except Usage as e:
        print(f"{NAME}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    except (Refused, ConfigError, WriteFailed) as e:
        s.out.say(f"{NAME}: {e}", 3, err=True)
        return 1

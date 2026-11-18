# SPDX-License-Identifier: MPL-2.0
"""The commands of cst-toolchains (`cst toolchains ...`): main() parses the
command line, asks toolchains/wiring.py for the session and runs one command
group (prune, auto, config, status), under the run lock where it changes files.
The program toolchains/cst-toolchains only hands its arguments to main().
"""

from __future__ import annotations

import sys

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
    wiring.set_output_mode(a["cmd"], a["action"])
    if a["cmd"] == ToolchainsCommand.HELP:
        print(doc)
        return 0
    if wiring.run_by_root():
        print(f"{NAME}: run as your user, not as root", file=sys.stderr)
        return 1
    rc = wiring.reexec_on_host(argv, entry_file)
    if rc is not None:
        return rc
    s = wiring.session(a["home"], entry_file, parts)
    cmd, action = a["cmd"], a["action"]
    try:
        if cmd == ToolchainsCommand.STATUS:
            status.cmd_status(s, a["brief"])
            return 0
        assert action is not None
        if cmd == ToolchainsCommand.CONFIG:
            return config.cmd_config(s, action, lean=a["lean"], sccache=a["sccache"])
        if cmd == ToolchainsCommand.PRUNE and action == ToolchainsAction.PLAN:
            return prune.cmd_prune(s, ToolchainsAction.PLAN, a["targets"])
        if cmd == ToolchainsCommand.PRUNE:
            return s.lock.run(lambda: prune.cmd_prune(s, ToolchainsAction.APPLY, a["targets"]))
        if action == ToolchainsAction.ON:
            return s.lock.run(lambda: auto.cmd_auto_on(s, unless_off=a["unless_off"]), busy_refused=True)
        return s.lock.run(lambda: auto.cmd_auto_off(s), busy_refused=True)
    except Usage as e:
        print(f"{NAME}: {e}\n{USAGE}", file=sys.stderr)
        return 2
    except (Refused, ConfigError, WriteFailed) as e:
        s.out.say(f"{NAME}: {e}", 3, err=True)
        return 1

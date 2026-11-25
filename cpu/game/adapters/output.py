# SPDX-License-Identifier: MPL-2.0
"""The game profile's output: the journal's line format on stdout (or the stream given), errors on stderr
(cpu.game.ports.GameOutputPort)."""

from typing import TextIO

from cst_core.adapters.journal import log as journal_log
from cst_core.adapters.terminal import stderr, stdout
from cst_core.ports import CommandResult
from cpu.game.domain import TAG


class GameStreamOutput:
    """The journal's line format on our stream (stdout, or the stream given), errors on stderr."""

    def __init__(self, out: TextIO | None = None) -> None:
        self._out = out

    def _stream(self) -> TextIO:
        return stdout() if self._out is None else self._out

    def log(self, prio: int, msg: str) -> None:
        """A journal line (<prio>game-profile: msg)."""
        journal_log(prio, msg, tag=TAG, stream=self._stream())

    def say(self, text: str) -> None:
        """One line."""
        print(text, file=self._stream(), flush=True)

    def flush(self) -> None:
        """What was said is out."""
        self._stream().flush()

    def relay(self, res: CommandResult) -> None:
        """What a command printed, on our streams (as if it had written there itself)."""
        if res.stdout:
            self._stream().write(res.stdout)
            self._stream().flush()
        if res.stderr:
            stderr().write(res.stderr)
            stderr().flush()

    def error(self, text: str) -> None:
        """One line on stderr."""
        print(text, file=stderr(), flush=True)

    def stderr_text(self, text: str) -> None:
        """text on stderr as it is."""
        stderr().write(text)
        stderr().flush()

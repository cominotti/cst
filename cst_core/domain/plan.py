# SPDX-License-Identifier: MPL-2.0
"""Pure plan reporting primitives."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PlanItem:
    """Single row in a CST plan or status report."""

    state_word: str
    target: str
    note: str = ""

    def format_line(self) -> str:
        """Render the aligned report row without trailing whitespace."""
        return f"  {self.state_word:<9} {self.target:<38} {self.note}".rstrip()

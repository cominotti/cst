"""Pure outcome and plan reporting primitives."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Ok[T]:
    """Successful outcome carrying a typed value."""

    value: T


@dataclass(frozen=True, slots=True)
class Err[E]:
    """Failed outcome carrying a typed domain or application error."""

    error: E


type Outcome[T, E] = Ok[T] | Err[E]


@dataclass(frozen=True, slots=True)
class PlanItem:
    """Single row in a CST plan or status report."""

    state_word: str
    target: str
    note: str = ""

    def format_line(self) -> str:
        """Render the aligned report row without trailing whitespace."""
        return f"  {self.state_word:<9} {self.target:<38} {self.note}".rstrip()

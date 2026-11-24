# SPDX-License-Identifier: MPL-2.0
"""The per-axis collector of `cst perf`'s rules and the verdict and rate helpers they share."""

from collections.abc import Iterable
from typing import Final

from cst_core.domain.values import HOUR
from perf.domain.models import (
    MIN_UPTIME_FOR_RATE_SECONDS,
    PerfAxis,
    PerfAxisReport,
    PerfFinding,
    PerfMetric,
    PerfVerdict,
)

VERDICT_RANK: Final[dict[PerfVerdict, int]] = {
    PerfVerdict.SKIP: 0,
    PerfVerdict.OPTIMIZED: 1,
    PerfVerdict.ATTENTION: 2,
    PerfVerdict.BOTTLENECK: 3,
}


class AxisCollector:
    """Collect ordered metrics and findings for a single axis cleanly."""

    __slots__ = ("axis", "findings", "metrics", "seen")

    def __init__(self, axis: PerfAxis) -> None:
        self.axis = axis
        self.metrics: list[PerfMetric] = []
        self.findings: list[PerfFinding] = []
        self.seen = False

    def metric(self, label: str, value: str) -> None:
        self.seen = True
        self.metrics.append(PerfMetric(label, value))

    def warn(self, finding_id: str, summary: str, detail: str, recommendation: str = "") -> None:
        self.findings.append(PerfFinding(finding_id, self.axis, PerfVerdict.ATTENTION, summary, detail, recommendation))

    def warn_fix(self, finding_id: str, summary: str, detail: str, guidance: tuple[str, str]) -> None:
        recommendation, fix_command = guidance
        self.findings.append(
            PerfFinding(finding_id, self.axis, PerfVerdict.ATTENTION, summary, detail, recommendation, fix_command)
        )

    def add(self, finding: PerfFinding) -> None:
        self.findings.append(finding)

    def report(self) -> PerfAxisReport:
        if not self.seen:
            verdict = PerfVerdict.SKIP
        elif not self.findings:
            verdict = PerfVerdict.OPTIMIZED
        else:
            verdict = worst_perf_verdict(f.verdict for f in self.findings)
        return PerfAxisReport(
            axis=self.axis,
            verdict=verdict,
            metrics=tuple(self.metrics),
            findings=tuple(self.findings),
        )


def worst_perf_verdict(verdicts: Iterable[PerfVerdict]) -> PerfVerdict:
    """Return the most severe verdict among `verdicts`, or `SKIP` if empty."""
    best = PerfVerdict.SKIP
    for v in verdicts:
        if VERDICT_RANK[v] > VERDICT_RANK[best]:
            best = v
    return best


def hourly_rate(count: int, uptime_seconds: float | None) -> float | None:
    if uptime_seconds is None or uptime_seconds < MIN_UPTIME_FOR_RATE_SECONDS:
        return None
    return count / (uptime_seconds / float(HOUR))


def rate_str(count: int, uptime_seconds: float | None) -> str:
    rate = hourly_rate(count, uptime_seconds)
    return f"{count} ({rate:.1f}/h)" if rate is not None else str(count)

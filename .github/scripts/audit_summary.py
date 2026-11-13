#!/usr/bin/env python3
"""Append a GitHub Actions job summary for `invowk audit --format json` output."""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: audit_summary.py <report.json> <exit_code>", file=sys.stderr)
        return 2

    report_path = Path(sys.argv[1])
    exit_code = int(sys.argv[2])
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return 0

    lines = ["### `invowk audit` (`cst.invowkmod`)"]
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        lines.append(f"- Exit code: `{exit_code}` (could not parse JSON report: `{exc}`)")
        with open(summary_file, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return 0

    summary = data.get("summary") or {}
    findings = data.get("findings") or []
    modules_scanned = summary.get("modules_scanned", "?")
    scripts_scanned = summary.get("scripts_scanned", "?")
    duration_ms = summary.get("duration_ms", "?")
    status_word = "PASS" if exit_code == 0 else f"FAIL (rc={exit_code})"
    lines.append(f"- Status: **{status_word}**")
    lines.append(
        f"- Modules: `{modules_scanned}` | Scripts: `{scripts_scanned}` "
        f"| Findings: `{len(findings)}` | Duration: `{duration_ms}ms`"
    )

    if findings:
        lines.append("")
        lines.append("| Severity | Rule | File | Message |")
        lines.append("|---|---|---|---|")
        for item in findings[:25]:
            sev = str(item.get("severity", "-")).replace("|", "\\|")
            rule = str(item.get("rule_id") or item.get("id") or "-").replace("|", "\\|")
            path = str(item.get("file") or item.get("path") or "-").replace("|", "\\|")
            msg = str(item.get("message") or item.get("title") or "-").replace("\n", " ").replace("|", "\\|")
            lines.append(f"| `{sev}` | `{rule}` | `{path}` | {msg} |")

    with open(summary_file, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

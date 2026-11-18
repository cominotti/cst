# SPDX-License-Identifier: MPL-2.0
"""What prune knows of every target, no I/O: the registry (TargetSpec,
SPECS), the checked settings (Settings), the saved summary of a run and its
status line, the planned targets and the run's time budget.
"""

from __future__ import annotations


# Planned, not implemented yet: (where, how).
PLANNED = {
    "rust:registry": (
        "~/.cargo/registry, ~/.cargo/git",
        "cargo already deletes downloads unused for months by itself; maybe cargo clean gc limits",
    ),
    "go:build": ("~/.cache/go-build", "Go already trims entries unused for 5 days by itself"),
    "go:mod": ("~/go/pkg/mod", "by age"),
    "node:npm": ("~/.npm/_cacache", "npm cache verify / by age"),
    "jvm:maven": ("~/.m2/repository", "old -SNAPSHOT builds and superseded versions only, never by age"),
}
# The time one `prune apply` may take in all. The service is stopped by
# systemd after 30 minutes (TimeoutStartSec): the run stops its own work
# before that. Targets with a fixed upper bound (MAX_SECONDS) run first, so a
# long rust:build run never leaves python:uv without time; rust:build starts
# no deletion with less than RM_MARGIN left and stops an rm at the end of the
# budget (what is left is a leftover that the next run deletes).
RUN_BUDGET = 25 * 60
RM_MARGIN = 2 * 60

# SPDX-License-Identifier: MPL-2.0
"""Property and model-based tests (Hypothesis, the "mbt" group of pyproject.toml).

Their modules are named mbt_*.py, not test_*.py, so the unit suite's
discovery (tests/run_unit.py, the standard library only) never imports them:
`invowk cmd test mbt` runs them through uv. This file imports nothing.
"""

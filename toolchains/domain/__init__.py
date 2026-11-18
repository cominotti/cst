# SPDX-License-Identifier: MPL-2.0
"""The rules of cst-toolchains, no I/O, one module per area: the names, formats, settings
syntax and command line every command uses (common), rust:build (build), python:uv (uv), what
prune knows of every target (prune), the units of `auto on` (auto) and the settings `config`
writes (config). The host is in toolchains/adapters/, the commands in toolchains/cli/.
"""

# SPDX-License-Identifier: MPL-2.0
"""Pure starter configuration rendering and active-override detection for `cst toolchains config init`."""

from __future__ import annotations

import contextlib
from enum import StrEnum
import os
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

from cst_core.domain.text import file_lines, strip_ascii_blanks
from toolchains.domain.common import ConfigError, parse_bool, parse_conf, parse_sccache_size
from toolchains.domain.config import (
    CARGO_CFG_REL,
    SCCACHE_SIZE_LINE,
    SCCACHE_WORKSPACE_LINE,
    incremental_state,
    lean_state,
    sccache_state,
    wrapper_path,
)
from toolchains.domain.prune import known_keys, parse_defaults


class UserConfKind(StrEnum):
    """On-disk state of `~/.config/cst/toolchains.conf` before `config init`."""

    MISSING = "missing"
    REGULAR = "regular"
    SYMLINK = "symlink"
    OTHER = "other"
    UNREADABLE = "unreadable"


STARTER_CONF_HEADER: Final[tuple[str, ...]] = (
    "# User settings for `cst toolchains` (~/.config/cst/toolchains.conf).",
    "# Uncomment and edit any KEY=value line below; defaults live in toolchains/toolchains.conf.",
    "# Durations: s, m, h, d or w. Sizes: K, M, G or T (G = GiB; unit required, only 0/off without).",
    "# An unknown key or bad value stops every run, so a typo never deletes anything.",
    "# Apply cargo/uv settings after editing with: cst toolchains config apply",
)


def detect_cargo_overrides(
    home: str,
    cargo_text: str | None,
    installed_wrapper_text: str | None,
) -> dict[str, str]:
    """Infer active CST toolchain overrides from `~/.cargo/config.toml` and installed `rustc-wrapper`."""
    if cargo_text is None or cargo_text == "":
        return {}
    out: dict[str, str] = {}
    cargo_path = os.path.join(home, CARGO_CFG_REL)

    lean = lean_state(cargo_path, cargo_text)
    if not lean["error"] and not lean["add"] and not lean["conflicts"]:
        out["RUST_LEAN_DEBUGINFO"] = "yes"

    inc = incremental_state(cargo_path, cargo_text)
    if not inc["error"] and inc["ours"]:
        out["RUST_INCREMENTAL"] = "off"

    sc = sccache_state(cargo_path, cargo_text, wrapper_path(home))
    if not sc["error"] and sc["ours"]:
        out["RUST_SCCACHE"] = "yes"
        if installed_wrapper_text is not None and installed_wrapper_text != "":
            _extract_wrapper_overrides(installed_wrapper_text, out)
    return out


def _extract_wrapper_overrides(wrapper_text: str, out: dict[str, str]) -> None:
    """Read `cache_size=` and `workspace=` placeholders out of CST's installed `rustc-wrapper`."""
    size_m = SCCACHE_SIZE_LINE.search(wrapper_text)
    if size_m:
        raw_size = size_m.group(0).split("=", 1)[1]
        with contextlib.suppress(ConfigError):
            out["RUST_SCCACHE_CACHE_SIZE"] = parse_sccache_size(raw_size)
    ws_m = SCCACHE_WORKSPACE_LINE.search(wrapper_text)
    if ws_m:
        raw_ws = ws_m.group(0).split("=", 1)[1]
        with contextlib.suppress(ConfigError):
            out["RUST_SCCACHE_WORKSPACE"] = "yes" if parse_bool(raw_ws) else "no"


def active_override_keys(defaults: Mapping[str, str], active: Mapping[str, str]) -> list[str]:
    """Sorted keys in ACTIVE that differ from CST's compiled DEFAULTS."""
    return sorted(
        k for k, v in active.items() if k in defaults and strip_ascii_blanks(v) != strip_ascii_blanks(defaults[k])
    )


def _body_start_index(lines: list[str]) -> int:
    """Skip the repo SPDX / 'Do not edit them here' preamble without touching section comments."""
    first_blank = next((i for i, ln in enumerate(lines) if ln == ""), None)
    if first_blank is not None and not any(
        ln.lstrip() and not ln.lstrip().startswith("#") for ln in lines[:first_blank]
    ):
        return first_blank + 1
    idx = 0
    while idx < len(lines) and lines[idx].startswith(("# SPDX-License-Identifier:", "# managed-by:")):
        idx += 1
    return idx


def render_starter_conf(
    defaults_text: str,
    active: Mapping[str, str] | None = None,
    *,
    defaults_path: str = "toolchains.conf",
) -> str:
    """Render `~/.config/cst/toolchains.conf` from `defaults_text`, keeping defaults commented
    and active non-default overrides uncommented."""
    defaults = parse_defaults(defaults_path, defaults_text)
    overrides = {} if active is None else {k: strip_ascii_blanks(v) for k, v in active.items()}
    lines = [ln.rstrip("\r") for ln in file_lines(defaults_text)]
    body_start = _body_start_index(lines)

    out: list[str] = [*STARTER_CONF_HEADER, ""]
    for line in lines[body_start:]:
        stripped = line.lstrip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key, _, raw_default = line.partition("=")
            key_name = key.strip()
            if key_name in defaults:
                active_val = overrides.get(key_name)
                if active_val is not None and active_val != strip_ascii_blanks(defaults[key_name]):
                    out.append(f"# default: {key_name}={raw_default}")
                    out.append(f"{key_name}={active_val}")
                else:
                    out.append(f"# {line}")
                continue
        out.append(line)

    rendered = "\n".join(out) + "\n"
    # Guarantee every generated starter file parses cleanly through the production conf parser.
    parse_conf(rendered, known_keys())
    return rendered

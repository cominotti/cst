# SPDX-License-Identifier: MPL-2.0
"""Linux host adapter for `cst desktop gnome localsearch`."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import os
from pathlib import Path
import shutil
from typing import Final

from cst_core.adapters import SystemctlAdapter, SystemctlHost
from cst_core.adapters.files import FileOwner, LocalFileSystemAdapter, write_atomic
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.domain.errors import Refused
from cst_core.ports import CommandRunnerPort, OutputMode, UnitName
from desktop.domain import LOCALSEARCH_STATE_REL
from desktop.domain.localsearch import (
    LOCALSEARCH_UNITS,
    TRACKER_SCHEMA,
    LocalsearchSnapshot,
    parse_gvariant_bool,
    parse_gvariant_str_list,
    parse_localsearch_state_json,
    parse_systemd_user_show_map,
)

SEAM_LOCALSEARCH_HOME: Final = "CST_DESKTOP_LOCALSEARCH_HOME"
_GSETTINGS_TIMEOUT_S: Final = 15
_SYSTEMD_USER_PROPS: Final = ("LoadState", "ActiveState", "UnitFileState")


class LinuxLocalsearchHost:
    """`LocalsearchHostPort` over `gsettings`, `systemctl --user`, and `~/.config/cst/`."""

    def __init__(
        self,
        *,
        home: str | None = None,
        runner: CommandRunnerPort | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self._home = home or os.environ.get(SEAM_LOCALSEARCH_HOME) or str(Path.home())
        self._runner = SubprocessCommandRunner() if runner is None else runner
        self._which = which
        self._fs = LocalFileSystemAdapter()
        self._systemd = SystemctlAdapter(self._runner, user=True, on_host=SystemctlHost.LOCAL)

    def _state_path(self) -> str:
        return os.path.join(self._home, LOCALSEARCH_STATE_REL)

    def _schema_exists(self) -> bool:
        res = self._runner.run(
            ["gsettings", "list-schemas"],
            timeout=_GSETTINGS_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        if res.returncode != 0:
            return False
        return TRACKER_SCHEMA in res.stdout.splitlines()

    def _gsettings_get(self, key: str) -> str | None:
        res = self._runner.run(
            ["gsettings", "get", TRACKER_SCHEMA, key],
            timeout=_GSETTINGS_TIMEOUT_S,
            locale_c=True,
            output=OutputMode.CAPTURE,
        )
        return res.stdout.strip() if res.returncode == 0 else None

    def read_snapshot(self) -> LocalsearchSnapshot:
        """Read gsettings keys, user unit status, and CST's pre-change backup record."""
        gsettings_ok = self._which("gsettings") is not None
        schema_ok = self._schema_exists() if gsettings_ok else False
        raw_rec = self._gsettings_get("index-recursive-directories") if schema_ok else None
        raw_sgl = self._gsettings_get("index-single-directories") if schema_ok else None
        raw_mon = self._gsettings_get("enable-monitors") if schema_ok else None
        rec_dirs = parse_gvariant_str_list(raw_rec) if raw_rec is not None else None
        sgl_dirs = parse_gvariant_str_list(raw_sgl) if raw_sgl is not None else None
        monitors = parse_gvariant_bool(raw_mon) if raw_mon is not None else None

        unit_rows = [(u, self._systemd.show(UnitName(u), _SYSTEMD_USER_PROPS)) for u in LOCALSEARCH_UNITS]
        systemd_ok = any(bool(props) for _, props in unit_rows)
        units = parse_systemd_user_show_map(unit_rows)

        saved_state = None
        state_err = ""
        raw_state = self._fs.read_text(self._state_path(), None, regular_only=True)
        if raw_state is not None:
            try:
                saved_state = parse_localsearch_state_json(raw_state)
            except ValueError as exc:
                state_err = str(exc)

        return LocalsearchSnapshot(
            gsettings_ok, schema_ok, rec_dirs, sgl_dirs, monitors, units, saved_state, state_err, systemd_ok
        )

    def save_state(self, text: str) -> None:
        """Persist `desktop-gnome-localsearch.json` atomically inside `~/.config/cst`."""
        write_atomic(self._state_path(), text, mode=0o644, durable=False, owner=FileOwner.CALLER, relabel=False)

    def remove_state(self) -> None:
        """Remove CST's localsearch backup file when reverting."""
        self._fs.remove_file(self._state_path())

    def _run_checked(self, argv: Sequence[str], what: str) -> None:
        res = self._runner.run(argv, timeout=_GSETTINGS_TIMEOUT_S, locale_c=True, output=OutputMode.CAPTURE)
        if res.returncode != 0:
            raise Refused(res.failure(what))

    def set_key(self, key: str, value: str) -> None:
        """Apply one `gsettings set` update, raising `Refused` on failure."""
        self._run_checked(["gsettings", "set", TRACKER_SCHEMA, key, value], f"gsettings set {key}")

    def reset_key(self, key: str) -> None:
        """Reset one `gsettings reset` key to schema default."""
        self._run_checked(["gsettings", "reset", TRACKER_SCHEMA, key], f"gsettings reset {key}")

    def mask_units(self, units: Sequence[str]) -> None:
        """Stop and mask localsearch user systemd units."""
        self._run_checked(["systemctl", "--user", "mask", "--now", *units], "systemctl --user mask")

    def unmask_units(self, units: Sequence[str]) -> None:
        """Unmask localsearch user systemd units."""
        self._run_checked(["systemctl", "--user", "unmask", *units], "systemctl --user unmask")

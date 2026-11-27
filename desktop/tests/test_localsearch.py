# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst desktop gnome localsearch` domain, use cases, CLI, and adapter."""

from __future__ import annotations

from collections.abc import Sequence
import io
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
from typing import override
import unittest

from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.domain.units import ActiveState, EnablementState
from cst_core.ports import CommandFailed, WriteFailed
from desktop.adapters.localsearch import LinuxLocalsearchHost
from desktop.application.localsearch import LocalsearchService
from desktop.cli import main as cli_main
from desktop.domain import (
    LOCALSEARCH_STATE_REL,
    DesktopCommand,
    LocalsearchAction,
    LocalsearchMode,
    parse_desktop_args,
)
from desktop.domain.localsearch import (
    KEY_MONITORS,
    KEY_RECURSIVE,
    KEY_SINGLE,
    LOCALSEARCH_UNITS,
    RESTRICT_RECURSIVE_DIRS,
    LocalsearchHealth,
    LocalsearchSnapshot,
    LocalsearchUnitProbe,
    classify_localsearch_health,
    format_gvariant_str_list,
    parse_gvariant_bool,
    parse_gvariant_str_list,
)
from desktop.wiring import DesktopPorts


class _FakeLocalsearchHost:
    def __init__(self, snap: LocalsearchSnapshot) -> None:
        self.snap = snap
        self.saved: list[str] = []
        self.removed_state = False
        self.set_calls: list[tuple[str, str]] = []
        self.reset_calls: list[str] = []
        self.masked: list[tuple[str, ...]] = []
        self.unmasked: list[tuple[str, ...]] = []

    def read_snapshot(self) -> LocalsearchSnapshot:
        return self.snap

    def save_state(self, text: str) -> None:
        self.saved.append(text)
        data = json.loads(text)
        self.snap = LocalsearchSnapshot(
            gsettings_available=self.snap.gsettings_available,
            schema_installed=self.snap.schema_installed,
            recursive_dirs=self.snap.recursive_dirs,
            single_dirs=self.snap.single_dirs,
            enable_monitors=self.snap.enable_monitors,
            units=self.snap.units,
            saved_state=data,
        )

    def remove_state(self) -> None:
        self.removed_state = True
        self.snap = LocalsearchSnapshot(
            gsettings_available=self.snap.gsettings_available,
            schema_installed=self.snap.schema_installed,
            recursive_dirs=self.snap.recursive_dirs,
            single_dirs=self.snap.single_dirs,
            enable_monitors=self.snap.enable_monitors,
            units=self.snap.units,
            saved_state=None,
        )

    def set_key(self, key: str, value: str) -> None:
        self.set_calls.append((key, value))
        rec = self.snap.recursive_dirs
        sgl = self.snap.single_dirs
        mon = self.snap.enable_monitors
        if key == KEY_RECURSIVE:
            rec = parse_gvariant_str_list(value)
        elif key == KEY_SINGLE:
            sgl = parse_gvariant_str_list(value)
        elif key == KEY_MONITORS:
            mon = parse_gvariant_bool(value)
        self.snap = LocalsearchSnapshot(
            gsettings_available=self.snap.gsettings_available,
            schema_installed=self.snap.schema_installed,
            recursive_dirs=rec,
            single_dirs=sgl,
            enable_monitors=mon,
            units=self.snap.units,
            saved_state=self.snap.saved_state,
        )

    def reset_key(self, key: str) -> None:
        self.reset_calls.append(key)
        self.snap = LocalsearchSnapshot(
            gsettings_available=self.snap.gsettings_available,
            schema_installed=self.snap.schema_installed,
            recursive_dirs=("&HOME",) if key == KEY_RECURSIVE else self.snap.recursive_dirs,
            single_dirs=self.snap.single_dirs,
            enable_monitors=True if key == KEY_MONITORS else self.snap.enable_monitors,
            units=self.snap.units,
            saved_state=self.snap.saved_state,
        )

    def mask_units(self, units: Sequence[str]) -> None:
        seq = tuple(units)
        self.masked.append(seq)
        new_units = tuple(
            LocalsearchUnitProbe(
                u.unit,
                u.present,
                ActiveState.INACTIVE if u.unit in seq else u.active_state,
                EnablementState.MASKED if u.unit in seq else u.unit_file_state,
            )
            for u in self.snap.units
        )
        self.snap = LocalsearchSnapshot(
            gsettings_available=self.snap.gsettings_available,
            schema_installed=self.snap.schema_installed,
            recursive_dirs=self.snap.recursive_dirs,
            single_dirs=self.snap.single_dirs,
            enable_monitors=self.snap.enable_monitors,
            units=new_units,
            saved_state=self.snap.saved_state,
        )

    def unmask_units(self, units: Sequence[str]) -> None:
        seq = tuple(units)
        self.unmasked.append(seq)
        new_units = tuple(
            LocalsearchUnitProbe(
                u.unit,
                u.present,
                u.active_state,
                EnablementState.ENABLED if u.unit in seq else u.unit_file_state,
            )
            for u in self.snap.units
        )
        self.snap = LocalsearchSnapshot(
            gsettings_available=self.snap.gsettings_available,
            schema_installed=self.snap.schema_installed,
            recursive_dirs=self.snap.recursive_dirs,
            single_dirs=self.snap.single_dirs,
            enable_monitors=self.snap.enable_monitors,
            units=new_units,
            saved_state=self.snap.saved_state,
        )


def _default_units(*, masked: bool = False) -> tuple[LocalsearchUnitProbe, ...]:
    ufs = EnablementState.MASKED if masked else EnablementState.ENABLED
    return tuple(
        LocalsearchUnitProbe(u, present=True, active_state=ActiveState.INACTIVE, unit_file_state=ufs)
        for u in LOCALSEARCH_UNITS
    )


class DesktopCliAndDomainTest(unittest.TestCase):
    """Test argument parsing and GVariant helpers for `desktop`."""

    def test_parse_desktop_args_matrix(self) -> None:
        p1 = parse_desktop_args(["status"])
        self.assertEqual(p1.command, DesktopCommand.STATUS)
        self.assertIsNone(p1.error)

        p2 = parse_desktop_args(["gnome", "localsearch", "apply", "--mode", "disable"])
        self.assertEqual(p2.command, DesktopCommand.GNOME)
        self.assertEqual(p2.ls_action, LocalsearchAction.APPLY)
        self.assertEqual(p2.mode, LocalsearchMode.DISABLE)

        p_bad = parse_desktop_args(["gnome", "localsearch", "apply", "--mode=unknown"])
        self.assertIsNotNone(p_bad.error)
        self.assertEqual(p_bad.code, 2)

        p_bad2 = parse_desktop_args(["gnome", "localsearch", "apply", "--mode"])
        self.assertIsNotNone(p_bad2.error)
        self.assertEqual(p_bad2.code, 2)

    def test_gvariant_list_and_bool_parsing(self) -> None:
        self.assertEqual(parse_gvariant_str_list("@as []"), ())
        self.assertEqual(parse_gvariant_str_list("['&DOCUMENTS', '&MUSIC']"), ("&DOCUMENTS", "&MUSIC"))
        self.assertEqual(
            parse_gvariant_str_list(r"""['a,b', "c'd", 'e\\f', 'g\'h']"""),
            ("a,b", "c'd", "e\\f", "g'h"),
        )
        self.assertIsNone(parse_gvariant_str_list("not a list"))
        self.assertIsNone(parse_gvariant_str_list("['a', ]"))
        self.assertIsNone(parse_gvariant_str_list("['unterminated"))
        self.assertEqual(format_gvariant_str_list(()), "@as []")
        self.assertEqual(format_gvariant_str_list(("&DOCUMENTS", "&PICTURES")), "['&DOCUMENTS', '&PICTURES']")
        special = ("a, b", "it's", "back\\slash")
        self.assertEqual(parse_gvariant_str_list(format_gvariant_str_list(special)), special)
        self.assertTrue(parse_gvariant_bool("true"))
        self.assertFalse(parse_gvariant_bool("false"))
        self.assertIsNone(parse_gvariant_bool("maybe"))

    def test_health_none_gsettings_unreachable_systemd_and_revert_message(self) -> None:
        snap_unreadable = LocalsearchSnapshot(
            gsettings_available=True,
            schema_installed=True,
            recursive_dirs=None,
            single_dirs=None,
            enable_monitors=None,
            units=_default_units(masked=False),
            saved_state=None,
        )
        health, _ = classify_localsearch_health(snap_unreadable)
        self.assertEqual(health, LocalsearchHealth.UNAVAILABLE)

        # Unreachable user systemd refuses --mode disable cleanly and flags saved disable state as DRIFT.
        snap_no_bus = LocalsearchSnapshot(
            gsettings_available=True,
            schema_installed=True,
            recursive_dirs=("$HOME",),
            single_dirs=("&DOWNLOAD",),
            enable_monitors=True,
            units=_default_units(masked=False),
            saved_state=None,
            systemd_reachable=False,
        )
        host_bus = _FakeLocalsearchHost(snap_no_bus)
        rc_dis, lines_dis = LocalsearchService(host_bus, prog="cst desktop").run(
            LocalsearchAction.APPLY, mode=LocalsearchMode.DISABLE
        )
        self.assertEqual(rc_dis, 1)
        self.assertIn("cannot reach user systemd manager", lines_dis[0])

        # Reverting when no saved state file exists still prints a revert confirmation, and WriteFailed/CommandFailed are caught.
        snap_no_state = LocalsearchSnapshot(
            gsettings_available=True,
            schema_installed=True,
            recursive_dirs=("&DOCUMENTS",),
            single_dirs=("&DOWNLOAD",),
            enable_monitors=True,
            units=_default_units(masked=False),
            saved_state=None,
        )
        rc_rev, lines_rev = LocalsearchService(_FakeLocalsearchHost(snap_no_state), prog="cst desktop").run(
            LocalsearchAction.REVERT
        )
        self.assertEqual(rc_rev, 0)
        self.assertIn("Reverted GNOME localsearch-3", lines_rev[-1])

        class _WriteFailHost(_FakeLocalsearchHost):
            @override
            def save_state(self, text: str) -> None:
                raise WriteFailed("state.json", "permission denied")

        class _CmdFailHost(_FakeLocalsearchHost):
            @override
            def set_key(self, key: str, value: str) -> None:
                raise CommandFailed(["gsettings"], 1, "", "boom")

        for bad_host in (_WriteFailHost(snap_no_state), _CmdFailHost(snap_no_state)):
            rc_err, lines_err = LocalsearchService(bad_host, prog="cst desktop").run(
                LocalsearchAction.APPLY, mode=LocalsearchMode.RESTRICT
            )
            self.assertEqual(rc_err, 1)
            self.assertTrue(lines_err[0].startswith("ERROR: "))


class LocalsearchLifecycleTest(unittest.TestCase):
    """Test plan, restrict, disable, idempotency, revert, and failure refusals."""

    def test_plan_apply_upgrade_and_revert(self) -> None:
        initial = LocalsearchSnapshot(
            gsettings_available=True,
            schema_installed=True,
            recursive_dirs=("&DOCUMENTS", "&DOWNLOAD", "$HOME/Custom"),
            single_dirs=("$HOME",),
            enable_monitors=True,
            units=_default_units(masked=False),
            saved_state=None,
        )
        host = _FakeLocalsearchHost(initial)
        svc = LocalsearchService(host, prog="cst desktop")

        # 1. Dry-run plan mutates nothing
        rc_plan, lines_plan = svc.run(LocalsearchAction.PLAN, mode=LocalsearchMode.RESTRICT)
        self.assertEqual(rc_plan, 0)
        self.assertIn("Dry run, nothing changed", "\n".join(lines_plan))
        self.assertEqual(host.set_calls, [])
        self.assertEqual(host.saved, [])

        # 2. Apply default restrict mode backs up original values
        rc_app, _ = svc.run(LocalsearchAction.APPLY, mode=LocalsearchMode.RESTRICT)
        self.assertEqual(rc_app, 0)
        self.assertEqual(len(host.saved), 1)
        backup = json.loads(host.saved[0])
        self.assertEqual(backup["mode"], "restrict")
        self.assertEqual(backup["recursive_dirs"], ["&DOCUMENTS", "&DOWNLOAD", "$HOME/Custom"])
        self.assertEqual(backup["single_dirs"], ["$HOME"])
        self.assertEqual(classify_localsearch_health(host.snap)[0], LocalsearchHealth.RESTRICTED_OK)

        # 3. Second restrict apply is a no-op
        host.set_calls.clear()
        rc_app2, lines_app2 = svc.run(LocalsearchAction.APPLY, mode=LocalsearchMode.RESTRICT)
        self.assertEqual(rc_app2, 0)
        self.assertIn("nothing to do", "\n".join(lines_app2))
        self.assertEqual(host.set_calls, [])

        # 4. Upgrade restrict -> disable keeps pre-CST backup keys!
        rc_dis, _ = svc.run(LocalsearchAction.APPLY, mode=LocalsearchMode.DISABLE)
        self.assertEqual(rc_dis, 0)
        backup2 = json.loads(host.saved[-1])
        self.assertEqual(backup2["mode"], "disable")
        self.assertEqual(backup2["recursive_dirs"], ["&DOCUMENTS", "&DOWNLOAD", "$HOME/Custom"])
        self.assertEqual(backup2["single_dirs"], ["$HOME"])
        self.assertEqual(host.masked, [LOCALSEARCH_UNITS])
        self.assertEqual(classify_localsearch_health(host.snap)[0], LocalsearchHealth.DISABLED_OK)

        # 5. Revert restores exact pre-CST keys and removes state
        host.set_calls.clear()
        rc_rev, _ = svc.run(LocalsearchAction.REVERT)
        self.assertEqual(rc_rev, 0)
        self.assertTrue(host.removed_state)
        self.assertIn(LOCALSEARCH_UNITS, host.unmasked)
        self.assertEqual(host.snap.recursive_dirs, ("&DOCUMENTS", "&DOWNLOAD", "$HOME/Custom"))
        self.assertEqual(host.snap.single_dirs, ("$HOME",))
        self.assertEqual(host.snap.enable_monitors, True)

    def test_refuses_without_schema_or_with_corrupt_state(self) -> None:
        bad_schema = LocalsearchSnapshot(
            gsettings_available=True,
            schema_installed=False,
            recursive_dirs=None,
            single_dirs=None,
            enable_monitors=None,
            units=_default_units(),
            saved_state=None,
        )
        host = _FakeLocalsearchHost(bad_schema)
        rc, lines = LocalsearchService(host, prog="cst desktop").run(LocalsearchAction.APPLY)
        self.assertEqual(rc, 1)
        self.assertIn("not installed", lines[0])

        bad_state = LocalsearchSnapshot(
            gsettings_available=True,
            schema_installed=True,
            recursive_dirs=RESTRICT_RECURSIVE_DIRS,
            single_dirs=("&DOWNLOAD",),
            enable_monitors=True,
            units=_default_units(),
            saved_state=None,
            state_error="not JSON",
        )
        rc2, lines2 = LocalsearchService(_FakeLocalsearchHost(bad_state), prog="cst desktop").run(
            LocalsearchAction.APPLY
        )
        self.assertEqual(rc2, 1)
        self.assertIn("cannot read localsearch backup state", lines2[0])

    def test_adapter_against_fake_gsettings_and_systemctl(self) -> None:
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            home_dir = os.path.join(tmp, "home")
            db_dir = os.path.join(tmp, "db")
            os.makedirs(bin_dir)
            os.makedirs(home_dir)
            os.makedirs(db_dir)

            gs_path = os.path.join(bin_dir, "gsettings")
            sc_path = os.path.join(bin_dir, "systemctl")
            with open(gs_path, "w", encoding="utf-8") as fh:
                fh.write(
                    f"""#!/usr/bin/env bash
set -eu
DB={db_dir!r}
case "${{1:-}}" in
  list-schemas) echo "org.freedesktop.Tracker3.Miner.Files" ;;
  get)
    f="$DB/${{3}}"
    if [[ -f "$f" ]]; then cat "$f"
    elif [[ "$3" == "enable-monitors" ]]; then echo "true"
    elif [[ "$3" == "index-recursive-directories" ]]; then echo "['$HOME']"
    else echo "['&DOWNLOAD']"; fi ;;
  set) printf '%s\\n' "$4" > "$DB/${{3}}" ;;
  reset) rm -f "$DB/${{3}}" ;;
  *) exit 2 ;;
esac
"""
                )
            with open(sc_path, "w", encoding="utf-8") as fh:
                fh.write(
                    f"""#!/usr/bin/env bash
set -eu
DB={db_dir!r}
shift # --user
cmd="${{1:-}}"
case "$cmd" in
  show)
    unit="${{@: -1}}"
    if [[ -f "$DB/${{unit}}.masked" ]]; then
      printf 'LoadState=masked\\nActiveState=inactive\\nUnitFileState=masked\\n'
    else
      printf 'LoadState=loaded\\nActiveState=inactive\\nUnitFileState=enabled\\n'
    fi ;;
  mask)
    shift; [[ "${{1:-}}" == "--now" ]] && shift
    for u in "$@"; do touch "$DB/${{u}}.masked"; done ;;
  unmask)
    shift
    for u in "$@"; do rm -f "$DB/${{u}}.masked"; done ;;
  *) exit 0 ;;
esac
"""
                )
            os.chmod(gs_path, stat.S_IRWXU)
            os.chmod(sc_path, stat.S_IRWXU)

            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ.get('PATH', '')}")
            runner = SubprocessCommandRunner(base_env=env)
            ls_host = LinuxLocalsearchHost(
                home=home_dir, runner=runner, which=lambda name: gs_path if name == "gsettings" else None
            )

            out = io.StringIO()
            err = io.StringIO()
            ports = DesktopPorts(localsearch=ls_host)
            self.assertEqual(
                cli_main(["gnome", "localsearch", "apply"], ports=ports, streams=(out, err)), 0, err.getvalue()
            )
            state_file = os.path.join(home_dir, LOCALSEARCH_STATE_REL)
            self.assertTrue(os.path.isfile(state_file))
            self.assertEqual(
                parse_gvariant_str_list(Path(db_dir, KEY_RECURSIVE).read_text(encoding="utf-8")),
                RESTRICT_RECURSIVE_DIRS,
            )

            self.assertEqual(
                cli_main(["gnome", "localsearch", "apply", "--mode", "disable"], ports=ports, streams=(out, err)), 0
            )
            self.assertTrue(os.path.isfile(os.path.join(db_dir, f"{LOCALSEARCH_UNITS[0]}.masked")))

            self.assertEqual(cli_main(["gnome", "localsearch", "revert"], ports=ports, streams=(out, err)), 0)
            self.assertFalse(os.path.exists(state_file))
            self.assertFalse(os.path.exists(os.path.join(db_dir, f"{LOCALSEARCH_UNITS[0]}.masked")))

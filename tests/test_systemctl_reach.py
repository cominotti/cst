# SPDX-License-Identifier: MPL-2.0
"""Where each caller's systemctl calls run from a toolbox: on the host (flatpak-spawn --host) or where
the caller runs. Each caller is built by its own wiring, with a container probe that says "container"
(or "host"), and driven through its own functions; subprocess.run is replaced, so nothing runs."""

from collections.abc import Callable, Generator, Mapping, Sequence
import contextlib
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from bluetooth.setup import wiring as bt_wiring
from bluetooth.setup.domain import Config, Options, Paths, parse_command_line, resolve
from cst_core.adapters.runner import HostExecutionContext, SubprocessCommandRunner
from cst_core.domain.files import UnitFileState
from cst_core.domain.units import UNIT_PATH_PROP
from cst_core.ports import CommandResult, OutputMode, UnitName
from swap import wiring as swap_wiring
from toolchains.adapters import units as tc_units
from toolchains.tests.systemctl_lines import systemctl as systemctl_line
from vm import application as vm_application, wiring as vm_wiring
from vm.domain import TimerStep

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STUB = "/stub/systemctl"
# A caller's own words for a fault (SubprocessCommandRunner's words).
type FaultWords = Callable[[Sequence[str], CommandResult, float | None], CommandResult]


def probe(*, container: bool) -> HostExecutionContext:
    """A container probe that says container (or host), whatever this machine is."""
    return HostExecutionContext(exists=lambda _path: container)


@contextlib.contextmanager
def spawned() -> Generator[list[list[str]]]:
    """The command lines that reach subprocess.run (none runs: each exits 0 with no output)."""
    seen: list[list[str]] = []

    def run(cmd: Sequence[str], *_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        seen.append(list(cmd))
        return subprocess.CompletedProcess(list(cmd), 0, b"", b"")

    with mock.patch("cst_core.adapters.runner.subprocess.run", side_effect=run):
        yield seen


def reach(seen: Sequence[Sequence[str]]) -> list[tuple[str, bool]]:
    """(systemctl's verb, on the host) of each command line."""
    out: list[tuple[str, bool]] = []
    for cmd in seen:
        host = list(cmd[:2]) == ["flatpak-spawn", "--host"]
        argv = [a for a in cmd[2:] if not a.startswith("--env=")] if host else list(cmd)
        out.append((next(a for a in argv[1:] if not a.startswith("--")), host))
    return out


class _TimerFiles:
    """vm's TimerFilesPort with no file, and these timers on."""

    def __init__(self, on: list[str]) -> None:
        self.on = on

    def file_states(self, unit: str) -> dict[str, UnitFileState]:
        del unit
        return {}

    def install(self, states: Mapping[str, UnitFileState], report: Callable[[TimerStep], None]) -> None:
        del states, report

    def mark_complete(self) -> None:
        pass

    def remove(self, path: str) -> None:
        del path

    def forget(self, removed: Sequence[str]) -> None:
        del removed

    def timers_on(self) -> list[str]:
        return self.on


class SystemctlReachTest(unittest.TestCase):
    """Each caller's systemctl reach, through its own wiring and functions."""

    def test_swap_shows_on_the_host_and_changes_where_it_runs(self) -> None:
        tmp = tempfile.mkdtemp(prefix="reach-")
        self.addCleanup(shutil.rmtree, tmp)
        for container in (True, False):
            with self.subTest(container=container), spawned() as seen:
                runner = SubprocessCommandRunner(probe(container=container))
                with mock.patch.object(swap_wiring, "SwapApplicationService") as service:
                    swap_wiring.build_service("cst swap", probe(container=container), runner, None, fallback_root=tmp)
                systemd = service.call_args.args[2]
                systemd.show(UnitName("machine.slice"), ("MemoryLow", "ActiveState"))
                systemd.daemon_reload()
                systemd.start(UnitName("systemd-cryptsetup@swap0.service"))
                systemd.stop(UnitName("dev-zram0.swap"))
                self.assertEqual(
                    reach(seen), [("show", container), ("daemon-reload", False), ("start", False), ("stop", False)]
                )

    def test_bluetooth_shows_on_the_host(self) -> None:
        where = Paths(home="/h", config="/h/.config", etc="/e", usr_share="/u", checkout=REPO)
        opts = parse_command_line(["status"], "cst-t", "help\n")
        assert isinstance(opts, Options)
        cfg = resolve(opts, {"DEVICE": "AA:BB:CC:DD:EE:FF"}, [], where, "cst-t")
        assert isinstance(cfg, Config)
        for container in (True, False):
            with self.subTest(container=container), spawned() as seen:
                run = bt_wiring.build(opts, cfg, where, container=container, bt_timeout=5)
                run.unit_show("bluetooth.service", user=False)
                run.unit_show("wireplumber.service", user=True)
                run.m.user_systemd_host.show_value(UnitName("wireplumber"), "MainPID", output=OutputMode.STDOUT)
                run.m.user_systemd_host.show_value(None, UNIT_PATH_PROP)  # the watcher unit's folder
                run.m.user_systemd_host.is_enabled(UnitName("cst-bt-ldac48.service"), output=OutputMode.STDOUT)
                run.m.user_systemd_host.is_active(UnitName("cst-bt-ldac48.service"), output=OutputMode.STDOUT)
                run.m.user_systemd.show_environment()
                run.m.user_systemd.daemon_reload_result(output=OutputMode.INHERIT)
                run.m.user_systemd.enable(UnitName("cst-bt-ldac48.service"), output=OutputMode.INHERIT)
                run.m.user_systemd.start(UnitName("cst-bt-ldac48.service"), output=OutputMode.INHERIT)
                run.m.user_systemd.restart(UnitName("wireplumber"), output=OutputMode.INHERIT)
                run.m.user_systemd.is_active(UnitName("cst-bt-ldac48.service"), output=OutputMode.STDOUT)
                run.m.user_systemd.disable(UnitName("cst-bt-ldac48.service"), now=True, output=OutputMode.STDOUT)
                self.assertEqual(
                    reach(seen),
                    [
                        ("show", container),
                        ("show", container),
                        ("show", container),
                        ("show", container),
                        ("is-enabled", container),
                        ("is-active", container),
                        ("show-environment", False),
                        ("daemon-reload", False),
                        ("enable", False),
                        ("start", False),
                        ("restart", False),
                        ("is-active", False),
                        ("disable", False),
                    ],
                )
                self.assertTrue(all("--user" in cmd for cmd in seen[1:]), seen[1:])

    def test_vm_reads_and_changes_the_systemd_it_runs_under(self) -> None:
        def runner(*, words: FaultWords | None = None) -> SubprocessCommandRunner:
            return SubprocessCommandRunner(probe(container=True), words=words)

        with mock.patch.object(vm_wiring, "SubprocessCommandRunner", side_effect=runner), spawned() as seen:
            systemd = vm_wiring.systemd()
            timers = _TimerFiles([])
            vm_application.timer_on(timers, systemd, "win", lambda: None, lambda _step: None)
            # The status of a timer that is on reads two properties.
            vm_application.timer_status(_TimerFiles(["cst-vm-backup@win.timer"]), systemd, "win")
            vm_application.timer_off(timers, systemd, "win", lambda _step: None)
        self.assertEqual(
            reach(seen),
            [
                ("daemon-reload", False),
                ("enable", False),
                ("show", False),
                ("show", False),
                ("disable", False),
                ("clean", False),
                ("daemon-reload", False),
            ],
        )

    def test_toolchains_reaches_the_host_unless_a_stub_runs(self) -> None:
        verbs = (
            ("is-active", "t.timer"),
            ("is-enabled", "t.timer"),
            ("show", "-p", "NextElapseUSecRealtime", "--value", "t.timer"),
            ("show-environment",),
            ("start", "t.service"),
            ("stop", "t.service"),
            ("enable", "--now", "t.timer"),
            ("disable", "--now", "t.timer"),
            ("clean", "--what=state", "t.timer"),
            ("daemon-reload",),
        )
        for container in (True, False):
            for program in (None, STUB):
                with (
                    self.subTest(container=container, program=program),
                    mock.patch.object(tc_units, "RUNNER", SubprocessCommandRunner(probe(container=container))),
                    spawned() as seen,
                ):
                    for args in verbs:
                        systemctl_line(*args, program=program)
                    host = container and program is None
                    self.assertEqual(reach(seen), [(args[0], host) for args in verbs])


if __name__ == "__main__":
    unittest.main()

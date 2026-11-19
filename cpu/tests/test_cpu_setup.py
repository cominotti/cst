# SPDX-License-Identifier: MPL-2.0
"""What a caller of cpu-setup.sh (`cst cpu`) sees, whatever the script is written in: the help and
its forms, an unknown command, a closed stdout pipe, no bytecode in the checkout. The script runs
from a scratch copy of the files it needs, as a subprocess.

Run from the repository root:
    invowk cmd test module cpu.tests.test_cpu_setup
"""

from collections.abc import Callable, Mapping, Sequence
import io
import os
import re
import shutil
import subprocess
import tempfile
from typing import Literal, override
import unittest
import unittest.mock

from cpu import domain as cpu_domain
from cpu.setup import cli as setup_cli, wiring as setup_wiring
from cpu.setup.adapters import CpuSetup, target_user
from cpu.setup.domain import (
    DROPIN_NAME,
    OLD_DROPIN_NAME,
    QUIET_ENV,
    SetupPaths,
    conf_lines,
    gamemode_ini,
    helper_dropin,
    toolbox_dropin,
)
from cpu.setup.wiring import Handed
from cst_core.adapters.runner import SubprocessCommandRunner
from cst_core.adapters.runtime import SYSTEM_PYTHON
from cst_core.ports import CommandResult, CommandRunnerPort, OutputMode
from tests.repo_files import REPO

# What cpu-setup.sh reads or runs for these commands (a module that is not there fails the copy).
NEEDED = ("cpu-setup.sh", "cst-install.bash", "cst-link.bash", "cst", "cpu", "cst_core")


def help_text(script: str, prog: str) -> str:
    """The help a script prints from its header: lines 2 to the first empty one, the license line
    left out, "# " taken off, then the usage line (the test's own reading, not the program's)."""
    with open(script, encoding="utf-8") as fh:
        lines = fh.read().split("\n")
    out: list[str] = []
    for number, line in enumerate(lines, start=1):
        if number < 2:
            continue
        if "SPDX-License-Identifier:" not in line:
            out.append(re.sub(r"^# ?", "", line))
        if number > 2 and line == "":
            break
    return "\n".join(out) + f"\nUsage: {prog} <command> [args]\n"


class CpuSetupCommandLineTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-cpu-setup-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.root = os.path.join(self.tmp, "cst")
        os.mkdir(self.root)
        for name in NEEDED:
            src = os.path.join(REPO, name)
            if os.path.isdir(src):
                shutil.copytree(
                    src, os.path.join(self.root, name), ignore=shutil.ignore_patterns("__pycache__", "tests")
                )
            else:
                shutil.copy2(src, os.path.join(self.root, name))
        self.script = os.path.join(self.root, "cpu-setup.sh")

    def run_setup(self, *args: str, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", self.script, *args],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", **(env or {})},
            cwd=self.tmp,
        )

    def test_every_form_of_help_prints_the_header_and_the_usage_line(self) -> None:
        for args in ((), ("help",), ("-h",), ("--help",), ("",), ("help", "extra")):
            with self.subTest(args=args):
                r = self.run_setup(*args)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, help_text(self.script, self.script), ""))
        r = self.run_setup("help", env={"CST_PROG": "cst cpu"})
        self.assertEqual(r.stdout, help_text(self.script, "cst cpu"))
        self.assertIn("Usage: cst cpu <command> [args]\n", r.stdout)
        self.assertIn("quiet [--for SECONDS] [--wait MAX] [--json]", r.stdout)

    def test_an_unknown_command_exits_2_with_the_help_on_stderr(self) -> None:
        r = self.run_setup("not-a-command", env={"CST_PROG": "cst cpu"})
        self.assertEqual(
            (r.returncode, r.stdout, r.stderr),
            (2, "", "Unknown command: not-a-command\n" + help_text(self.script, "cst cpu")),
        )

    def test_help_into_a_closed_pipe_stops_quietly(self) -> None:
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            p = subprocess.run(
                ["bash", "-c", '"$@"; echo "status=$?" >&2', "_", "bash", self.script, "help"],
                stdout=write_end,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                env={"PATH": "/usr/bin:/bin"},
                check=False,
                timeout=60,
            )
        finally:
            os.close(write_end)
        # What the calling shell sees: 141 (128 + SIGPIPE), and nothing else on stderr.
        self.assertEqual(p.stderr, "status=141\n")

    def test_no_bytecode_is_written_into_the_checkout(self) -> None:
        for args in (("help",), ("bogus",)):
            self.run_setup(*args)
        found = [
            os.path.join(d, n)
            for d, dirs, files in os.walk(self.root)
            for n in dirs + files
            if "__pycache__" in n or n.endswith(".pyc")
        ]
        self.assertEqual(found, [])


# ---- in-process: the routes, with a fake host --------------------------------------------------------

SHIM = os.path.join(REPO, "cpu-setup.sh")


class FakeHost:
    """ModuleRoutePort: host or toolbox, the euid, the shim's text; records what is said and exec'd."""

    def __init__(self, *, container: bool = False, euid: int = 1000) -> None:
        self.container = container
        self.uid = euid
        self.said: list[tuple[str, str]] = []
        self.execs: list[list[str]] = []

    def environ(self) -> Mapping[str, str]:
        return dict[str, str]()

    def euid(self) -> int:
        return self.uid

    def is_container(self) -> bool:
        return self.container

    def user_passwd(self, user: str) -> tuple[int, str]:
        raise KeyError(user)

    def exec_on_host(self, argv: Sequence[str], *, settings: Mapping[str, str]) -> int | None:
        raise AssertionError(f"cpu-setup.sh runs flatpak-spawn itself, not {argv} {settings}")

    def read_text(self, path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def say(self, stream: Literal["out", "err"], text: str) -> None:
        self.said.append((stream, text))

    def exec_replace(self, argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
        self.execs.append(list(argv))
        return 0


def handed(
    *, cst_prog: str = "", sudo_user: str = "", quiet: Mapping[str, str] | None = None, script: str = SHIM
) -> Handed:
    return Handed(script, "./cpu-setup.sh", cst_prog, sudo_user, dict(quiet or {}))


class RouteTest(unittest.TestCase):
    """Where each command goes: help, cpu-watch (here or on the host), the toolbox refusal, sudo, root."""

    @override
    def setUp(self) -> None:
        self.ran: list[tuple[str, list[str]]] = []

    def root_command(self, cmd: str, rest: Sequence[str]) -> int:
        self.ran.append((cmd, list(rest)))
        return 7

    def main(self, *argv: str, host: FakeHost, cst_prog: str = "") -> int:
        return setup_cli.main(list(argv), handed=handed(cst_prog=cst_prog), host=host, root_command=self.root_command)

    def test_watch_report_and_quiet_run_the_checkouts_cpu_watch_on_the_host(self) -> None:
        host = FakeHost()
        watch = os.path.join(REPO, "cpu", "cpu-watch")
        self.assertEqual(self.main("quiet", "--for", "5", host=host), 0)
        self.assertEqual(host.execs, [["python3", "-I", "-B", watch, "quiet", "--for", "5"]])
        for cmd in ("watch", "report"):
            host.execs.clear()
            self.main(cmd, "", host=host)
            self.assertEqual(host.execs, [["python3", "-I", "-B", watch, cmd, ""]])
        self.assertEqual(self.ran, [])

    def test_from_a_toolbox_cpu_watch_runs_on_the_host_with_the_limits_that_are_set(self) -> None:
        host = FakeHost(container=True)
        watch = os.path.join(REPO, "cpu", "cpu-watch")
        h = handed(quiet={"QUIET_CPU_PCT": "20", "QUIET_WRITE_MBPS": "8", "QUIET_IO_PSI": ""})
        setup_cli.main(["quiet", "--wait", "2h", "--json"], handed=h, host=host)
        self.assertEqual(
            host.execs,
            [
                [
                    "flatpak-spawn",
                    "--host",
                    "--env=QUIET_CPU_PCT=20",
                    "--env=QUIET_WRITE_MBPS=8",
                    "--env=QUIET_IO_PSI=",
                    "python3",
                    "-I",
                    "-B",
                    watch,
                    "quiet",
                    "--wait",
                    "2h",
                    "--json",
                ]
            ],
        )
        host.execs.clear()
        setup_cli.main(["report"], handed=handed(), host=host)
        self.assertEqual(host.execs, [["flatpak-spawn", "--host", "python3", "-I", "-B", watch, "report"]])

    def test_the_limits_handed_over_are_every_one_cpu_watch_reads(self) -> None:
        # A limit added to cpu-watch but not here would be dropped inside a toolbox.
        self.assertEqual(sorted(QUIET_ENV), sorted(lim.env for lim in cpu_domain.QUIET_LIMITS))
        env = {v: str(i) for i, v in enumerate(QUIET_ENV)} | {"QUIET_OTHER": "x", "CST_SELF": SHIM, "CST_ARG0": "a"}
        with (
            unittest.mock.patch.dict(os.environ, env, clear=True),
            unittest.mock.patch("cpu.setup.wiring.undo_python_start"),
            unittest.mock.patch("cpu.setup.wiring.default_ctrl_c"),
        ):
            got = setup_wiring.start()
            left = dict(os.environ)
        self.assertEqual(dict(got.quiet), {v: str(i) for i, v in enumerate(QUIET_ENV)})
        self.assertEqual((got.script, got.arg0), (SHIM, "a"))
        self.assertNotIn("CST_SELF", left)  # what the shim hands over reaches no command
        self.assertNotIn("CST_ARG0", left)

    def test_root_commands_refuse_in_a_toolbox(self) -> None:
        for cmd in ("install", "status", "uninstall", "game", "vm-pin", "docker-slice", "experiment"):
            with self.subTest(cmd=cmd):
                host = FakeHost(container=True, euid=0)
                self.assertEqual(self.main(cmd, "x", host=host), 1)
                self.assertEqual(
                    host.said, [("err", f"'{cmd}' must run on the host, not inside a toolbox/container.\n")]
                )
                self.assertEqual((host.execs, self.ran), ([], []))

    def test_as_a_user_root_commands_run_the_script_again_through_sudo(self) -> None:
        host = FakeHost()
        self.main("install", host=host)
        self.main("game", "on", "", host=host, cst_prog="cst cpu")
        self.assertEqual(
            host.execs,
            [["sudo", "--", SHIM, "install"], ["sudo", "--", "env", "CST_PROG=cst cpu", SHIM, "game", "on", ""]],
        )
        self.assertEqual(self.ran, [])

    def test_as_root_on_the_host_the_root_command_runs(self) -> None:
        host = FakeHost(euid=0)
        self.assertEqual(self.main("vm-pin", "plan", "vm1", host=host), 7)
        self.assertEqual((self.ran, host.execs, host.said), ([("vm-pin", ["plan", "vm1"])], [], []))

    def test_help_and_unknown_command_through_the_host(self) -> None:
        host = FakeHost(container=True, euid=0)
        self.assertEqual(self.main("", host=host, cst_prog="cst cpu"), 0)
        self.assertEqual(host.said, [("out", help_text(SHIM, "cst cpu"))])
        host.said.clear()
        self.assertEqual(self.main("nope", host=host), 2)
        self.assertEqual(host.said, [("err", "Unknown command: nope\n" + help_text(SHIM, "./cpu-setup.sh"))])

    def test_the_root_commands_are_built_over_the_real_adapters(self) -> None:
        # What wiring builds for a root command: a usage error answers before any I/O.
        err = io.StringIO()
        with unittest.mock.patch("sys.stderr", err):
            run = setup_wiring.root_command(handed(sudo_user="alice"), "cst cpu")
            self.assertEqual(run("experiment", ["bogus"]), 2)
            self.assertEqual(run("game", ["bogus"]), 2)
        self.assertEqual(err.getvalue(), "experiment: list|on NAME|off NAME\ngame: on|off|state\n")

    def test_without_the_shim_it_says_how_it_is_run(self) -> None:
        err = io.StringIO()
        with unittest.mock.patch("sys.stderr", err):
            self.assertEqual(setup_cli.main(["help"], handed=handed(script=""), host=FakeHost()), 2)
        self.assertIn("run by cpu-setup.sh", err.getvalue())


class ShimExecTest(unittest.TestCase):
    """The shim execs Python, which execs cpu-watch (or flatpak-spawn): one process all the way, with
    the caller's environment (nothing the shim hands over, the caller's LC_CTYPE)."""

    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-cpu-shim-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        for name in ("python3", "flatpak-spawn"):
            p = os.path.join(self.bin, name)
            with open(p, "w") as fh:
                fh.write('#!/bin/bash\necho "PID $$"\nfor a in "$@"; do echo "ARG $a"; done\nenv | sed "s/^/ENV /"\n')
            os.chmod(p, 0o755)

    def seen(self, env: Mapping[str, str]) -> tuple[subprocess.CompletedProcess[str], dict[str, str], list[str]]:
        proc = subprocess.Popen(
            ["bash", SHIM, "quiet", "--for", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            env={"PATH": f"{self.bin}:/usr/bin:/bin", **env},
        )
        out, err = proc.communicate(timeout=60)
        done = subprocess.CompletedProcess(proc.args, proc.returncode, out, err)
        self.assertEqual(done.returncode, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], f"PID {proc.pid}", "every step replaced the one before (exec)")
        got = dict(line[4:].partition("=")[::2] for line in lines if line.startswith("ENV "))
        args = [line[4:] for line in lines if line.startswith("ARG ")]
        return done, got, args

    def test_one_process_and_the_callers_environment(self) -> None:
        _, env, args = self.seen({"CST_PROG": "cst cpu"})
        self.assertEqual(args[-6:], ["-I", "-B", os.path.join(REPO, "cpu", "cpu-watch"), "quiet", "--for", "1"])
        self.assertEqual(env.get("CST_PROG"), "cst cpu")
        self.assertEqual([k for k in env if k.startswith("CST_") and k != "CST_PROG"], [])
        self.assertNotIn("LC_CTYPE", env)  # Python's C.UTF-8 under a C locale does not leak
        _, env, _ = self.seen({"LANG": "C"})
        self.assertNotIn("LC_CTYPE", env)
        _, env, _ = self.seen({"LC_CTYPE": "C"})
        self.assertEqual(env.get("LC_CTYPE"), "C")


# ---- in-process: the root commands, with a fake runner -------------------------------------------------


class FakeRunner:
    """CommandRunnerPort: records each command line; answers from `answers` (the first prefix that
    matches the joined command line), else exit 0 with no output. on_call runs before each answer."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.inputs: dict[str, str | None] = {}
        self.envs: list[Mapping[str, str] | None] = []
        self.answers: list[tuple[str, CommandResult]] = []
        self.on_call: Callable[[list[str]], None] = lambda _argv: None

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        **_kw: object,
    ) -> CommandResult:
        self.calls.append(list(argv))
        self.envs.append(env)
        if "text" in argv:
            self.inputs[argv[list(argv).index("text") + 1]] = input_text
        self.on_call(list(argv))
        line = " ".join(argv)
        for prefix, res in self.answers:
            if line.startswith(prefix) or prefix in line:
                return res
        return CommandResult(0, "", "")

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


class CpuSetupCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-cpu-root-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        t = self.tmp
        self.home = os.path.join(t, "home", "tester")
        os.makedirs(os.path.join(self.home, ".config"))
        self.paths = SetupPaths(
            conf=f"{t}/etc/cpu-setup/cpu-setup.conf",
            unit_dir=f"{t}/etc/systemd/system",
            daemon_json=f"{t}/etc/docker/daemon.json",
            hwp_boost=f"{t}/sys/hwp_dynamic_boost",
            hwp_tmpfiles=f"{t}/etc/tmpfiles.d/cpu-setup-hwp-boost.conf",
            e_cores=f"{t}/sys/cpu_atom/cpus",
            unit_runtime=f"{t}/run/systemd/system",
            run=f"{t}/run/cpu-setup",
            cgroot=f"{t}/sys/fs/cgroup",
            sched_ext=f"{t}/sys/sched_ext",
            preempt=f"{t}/sys/preempt",
            etc_group=f"{t}/etc/group",
            lib_group=f"{t}/usr/lib/group",
            etc_gshadow=f"{t}/etc/gshadow",
            experiments={
                "minfree-256m": (f"{t}/proc/min_free_kbytes", "262144"),
                "thp-defer": (f"{t}/sys/defrag", "defer"),
            },
        )
        self.write(self.paths.e_cores, "8-23\n")
        self.write(self.paths.hwp_boost, "0\n")
        self.write(self.paths.etc_group, "wheel:x:10:tester\n")
        self.write(self.paths.lib_group, "gamemode:x:973:\n")
        self.write(self.paths.etc_gshadow, "wheel:::\n")
        self.write(f"{t}/proc/min_free_kbytes", "67584\n")
        self.write(f"{t}/sys/defrag", "always defer [madvise] never\n")
        self.runner = FakeRunner()
        self.passed: list[list[str]] = []
        self.groups = {"gamemode"}

    def write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def read(self, path: str) -> str:
        with open(path) as fh:
            return fh.read()

    def setup_for(
        self, user: str = "tester", prog: str = "cst cpu", runner: CommandRunnerPort | None = None
    ) -> tuple[CpuSetup, io.StringIO, io.StringIO]:
        out, err = io.StringIO(), io.StringIO()

        def passthrough(argv: Sequence[str]) -> int:
            self.passed.append(list(argv))
            return 3

        s = CpuSetup(
            self.paths,
            self.runner if runner is None else runner,
            root=REPO,
            prog=prog,
            user=user,
            passthrough=passthrough,
            home_of=lambda u: self.home if u == "tester" else "",
            has_group=lambda g: g in self.groups,
            me=lambda: "root",
            out=out,
            err=err,
        )
        return s, out, err

    def command(self, cmd: str, *args: str, user: str = "tester") -> tuple[int, str, str]:
        s, out, err = self.setup_for(user)
        rc = s.command(cmd, list(args))
        return rc, out.getvalue(), err.getvalue()


INSTALLER = [SYSTEM_PYTHON, "-I", "-B", os.path.join(REPO, "cst_core", "installer.py")]


def remove_text_files(argv: list[str]) -> None:
    """For FakeRunner.on_call: what the installer's remove-text does to the files it names."""
    if "remove-text" not in argv:
        return
    words = argv[argv.index("remove-text") + 1 :]
    for i, w in enumerate(words):
        if not w.startswith("--") and (i == 0 or words[i - 1] != "--home") and os.path.lexists(w):
            os.remove(w)


class InstallTest(CpuSetupCase):
    def test_install_writes_in_order_and_reloads_after_the_old_names_are_gone(self) -> None:
        old = [
            os.path.join(self.paths.unit_dir, "machine.slice.d", OLD_DROPIN_NAME),
            toolbox_dropin(self.home, OLD_DROPIN_NAME),
            helper_dropin(self.home, OLD_DROPIN_NAME),
        ]
        for f in old:
            self.write(f, "")
        at_reload: list[bool] = []
        self.runner.on_call = lambda argv: (
            at_reload.append(any(os.path.exists(f) for f in old)) if argv == ["systemctl", "daemon-reload"] else None
        )
        self.runner.answers = [("id -nG tester", CommandResult(0, "tester wheel\n", ""))]
        rc, out, err = self.command("install")
        self.assertEqual((rc, err), (0, ""), out)
        self.assertEqual(at_reload, [False], "the managers reload without the old names")
        calls = self.runner.joined()
        inst = " ".join(INSTALLER)
        runuser = f"runuser -u tester -- {inst}"
        expected = [
            f"{inst} text {self.paths.conf} 0644",
            f"{inst} install --set USER=tester --component cpu:core",
            f"{inst} text {self.paths.hwp_tmpfiles} 0644 --component cpu:core",
            f"{runuser} text {toolbox_dropin(self.home)} 0644 --component cpu:toolbox",
            f"{runuser} text {helper_dropin(self.home)} 0644 --component cpu:toolbox",
            f"{runuser} install --home {self.home} --component cpu:gamemode",
            "id -nG tester",
            "usermod -aG gamemode tester",
            "systemctl daemon-reload",
            "systemctl --user -M tester@ daemon-reload",
            "systemctl enable --now cpu-game-watchdog.timer",
            f"{inst} mark --component cpu:core done",
            f"{runuser} mark --home {self.home} --component cpu:toolbox done",
            f"{runuser} mark --home {self.home} --component cpu:gamemode done",
        ]
        got = [c for c in calls if not c.startswith(("python3 -I -c", "bash "))]
        self.assertEqual(got, expected)
        # The programs parse, and the cst link comes from cst-link.bash itself.
        self.assertTrue([c for c in calls if c.startswith("python3 -I -c import ast")])
        self.assertIn(
            f'bash -c set -Eeuo pipefail; source "$1"; link_cst_command "$2" cpu-setup.sh {REPO}/cst-link.bash tester',
            calls,
        )
        # The settings, the HWP boost now, the group from /usr/lib/group, the old names gone.
        self.assertEqual(self.read(self.paths.hwp_boost), "1\n")
        self.assertIn(f"set:       {self.paths.hwp_boost} = 1\n", out)
        self.assertEqual(self.read(self.paths.etc_group), "wheel:x:10:tester\ngamemode:x:973:\n")
        self.assertEqual(self.read(self.paths.etc_gshadow), "wheel:::\ngamemode:!::\n")
        for f in old:
            self.assertIn(f"removed '{f}'\n", out)
        self.assertIn("added tester to the 'gamemode' group (effective at the next login)\n", out)
        self.assertTrue(
            out.endswith("  - measure: cst cpu watch -l baseline --game dota2 --seconds 300   (while a build runs)\n")
        )
        self.assertEqual(os.stat(os.path.dirname(self.paths.conf)).st_mode & 0o777, 0o755)

    def test_what_install_hands_the_installer(self) -> None:
        self.command("install")
        texts = self.runner.inputs
        self.assertEqual(texts[self.paths.conf], conf_lines("tester"))
        self.assertIn("TARGET_USER='tester'\n", conf_lines("tester"))
        self.assertTrue(str(texts[toolbox_dropin(self.home)]).endswith("[Slice]\nCPUWeight=30\nAllowedCPUs=8-23\n"))
        self.assertTrue(str(texts[helper_dropin(self.home)]).endswith("[Service]\nAllowedCPUs=8-23\n"))
        # Every installer call hands over the caller's LC_CTYPE, as cst-install.bash did.
        for argv, env in zip(self.runner.calls, self.runner.envs, strict=True):
            if SYSTEM_PYTHON in argv:
                self.assertEqual(list(env or {}), ["CST_LC_CTYPE"], argv)

    def test_no_e_cores_no_hwp_and_an_own_gamemode_ini(self) -> None:
        os.remove(self.paths.e_cores)
        os.remove(self.paths.hwp_boost)
        self.write(gamemode_ini(self.home), "[general]\n")
        self.write(helper_dropin(self.home), "x")
        self.write(self.paths.conf, "TARGET_USER='tester'\n")
        self.runner.answers = [("id -nG tester", CommandResult(0, "tester gamemode\n", ""))]
        rc, out, err = self.command("install")
        self.assertEqual(rc, 0, err)
        self.assertIn("NOTE: no E-cores found (/sys/devices/cpu_atom/cpus): toolbox CPU restriction skipped", err)
        self.assertIn(
            f"NOTE: {self.paths.hwp_boost} not found (no intel_pstate in HWP mode): HWP dynamic boost skipped", err
        )
        self.assertIn(
            f"NOTE: {gamemode_ini(self.home)} is your own: add [custom] start/end from {REPO}/cpu/gamemode.ini by hand",
            err,
        )
        calls = self.runner.joined()
        runuser = f"runuser -u tester -- {' '.join(INSTALLER)}"
        # The helper's drop-in, which only E-cores need, goes with its install record line.
        self.assertIn(f"{runuser} remove-text --home {self.home} {helper_dropin(self.home)}", calls)
        self.assertNotIn("removed '", out)
        self.assertFalse(
            [c for c in calls if "cpu:gamemode" in c or "usermod" in c or f"text {self.paths.conf}" in c], calls
        )
        # The toolboxes' drop-in is marked whatever happened to gamemode.ini.
        self.assertEqual(calls[-1], f"{runuser} mark --home {self.home} --component cpu:toolbox done")

    def test_install_refuses_without_a_user_to_install_for(self) -> None:
        for user in ("", "root"):
            with self.subTest(user=user):
                self.runner.calls.clear()
                self.assertEqual(
                    self.command("install", user=user),
                    (1, "", "install: run through sudo from your user account (needs SUDO_USER)\n"),
                )
                self.assertEqual(self.runner.calls, [])

    def test_a_failing_step_ends_install_with_its_exit_status(self) -> None:
        self.runner.answers = [
            ("systemctl enable --now", CommandResult(4, "", "")),
            ("id -nG", CommandResult(0, "gamemode", "")),
        ]
        rc, out, _ = self.command("install")
        self.assertEqual(rc, 4)
        self.assertFalse([c for c in self.runner.joined() if " mark " in c])
        self.assertNotIn("Done.", out)
        self.runner.answers = [("usermod", CommandResult(-15, "", ""))]
        self.assertEqual(self.command("install")[0], 143)  # killed by SIGTERM: 128+15, as bash
        self.runner.answers = [("text " + self.paths.hwp_tmpfiles, CommandResult(1, "", ""))]
        self.assertEqual(self.command("install")[0], 1)

    def test_a_group_missing_from_usr_lib_group_stops_install(self) -> None:
        # NSS has the group, but neither /etc/group nor /usr/lib/group has its line: usermod cannot add
        # the user, and install says why.
        self.write(self.paths.lib_group, "other:x:1:\n")
        rc, _, err = self.command("install")
        self.assertEqual(rc, 1)
        self.assertEqual(
            err.splitlines()[-1],
            "cpu-setup.sh: the 'gamemode' group comes from NSS, not /etc/group or /usr/lib/group: "
            "add its line to /etc/group by hand, then run install again",
        )
        self.assertFalse([c for c in self.runner.joined() if c.startswith("usermod")])
        self.groups.clear()
        rc, _, err = self.command("install")
        self.assertEqual(rc, 0)
        self.assertIn("no 'gamemode' group on this system (is the gamemode package installed?)", err)

    def test_a_user_without_a_home_stops_at_the_gamemode_step(self) -> None:
        rc, _, err = self.command("install", user="ghost")
        self.assertEqual(rc, 1)
        self.assertTrue(err.endswith("cst_install_as: no home folder for user 'ghost'\n"), err)


class SwapDropinsTest(CpuSetupCase):
    def test_install_and_uninstall_leave_swaps_drop_ins_alone(self) -> None:
        # machine.slice.d also holds the swap module's MemoryLow drop-in, under its name and under the
        # old one swap's apply moves away: cpu's cleanup of its own old name never touches them.
        d = os.path.join(self.paths.unit_dir, "machine.slice.d")
        swap = [os.path.join(d, "10-swap-memory-low.conf"), os.path.join(d, "50-memory-low.conf")]
        for f in [*swap, os.path.join(d, OLD_DROPIN_NAME)]:
            self.write(f, "[Slice]\nMemoryLow=8G\n")
        self.runner.answers = [("id -nG tester", CommandResult(0, "tester wheel\n", ""))]
        for cmd in ("install", "uninstall", "uninstall"):
            rc, out, err = self.command(cmd)
            self.assertEqual((rc, err), (0, ""), out)
            for f in swap:
                self.assertEqual(self.read(f), "[Slice]\nMemoryLow=8G\n", f"{cmd}: {f}")
                self.assertNotIn(f, out)
        self.assertFalse(os.path.exists(os.path.join(d, OLD_DROPIN_NAME)))


class UninstallTest(CpuSetupCase):
    def test_uninstall_removes_what_install_added_and_keeps_the_rest(self) -> None:
        rt = self.paths.unit_runtime
        left = [f"{rt}/user.slice.d/zz-cst-game.conf", f"{rt}/user@1000.service.d/zz-cst-game.conf"]
        keep = f"{rt}/user-1000.slice.d/10-other.conf"
        old = [
            os.path.join(self.paths.unit_dir, "machine.slice.d", OLD_DROPIN_NAME),
            toolbox_dropin(self.home, OLD_DROPIN_NAME),
        ]
        for f in [*left, *old, keep, self.paths.hwp_tmpfiles, toolbox_dropin(self.home), helper_dropin(self.home)]:
            self.write(f, "")
        self.write(gamemode_ini(self.home), "; Managed by cpu-setup.sh (CST).\n")
        self.runner.on_call = remove_text_files
        rc, out, err = self.command("uninstall")
        self.assertEqual((rc, err), (0, ""))
        for f in left:
            self.assertFalse(os.path.exists(os.path.dirname(f)), f)
            self.assertIn(f"removed '{f}'\n", out)
        self.assertTrue(os.path.exists(keep))
        for f in old:
            self.assertIn(f"removed '{f}'\n", out)
        self.assertFalse(os.path.exists(os.path.join(self.paths.unit_dir, "machine.slice.d")))  # emptied: removed
        self.assertFalse(os.path.exists(self.paths.hwp_tmpfiles))
        self.assertEqual(self.read(self.paths.hwp_boost), "0\n")
        self.assertFalse(os.path.exists(os.path.dirname(toolbox_dropin(self.home))))
        inst = " ".join(INSTALLER)
        dropins = f"{toolbox_dropin(self.home)} {helper_dropin(self.home)}"
        self.assertEqual(
            self.runner.joined(),
            [
                # The watchdog first: a tick between the two would switch the profile back on, just
                # before its unit and its off program are deleted.
                "systemctl disable --now cpu-game-watchdog.timer",
                "systemctl stop cpu-game.service",
                f"{inst} remove --component cpu:core",
                # Through the installer, with their install record lines (the hwp file also when gone).
                f"{inst} remove-text {self.paths.hwp_tmpfiles}",
                f"runuser -u tester -- {inst} remove-text --home {self.home} {dropins}",
                f"runuser -u tester -- {inst} remove --home {self.home} --component cpu:gamemode",
                "systemctl --user -M tester@ daemon-reload",
                "systemctl daemon-reload",
            ],
        )
        self.assertTrue(out.endswith("the previous one is in /etc/cpu-setup/*.xml.before-vm-pin).\n"), out)
        self.assertIn("('cst cpu docker-slice uninstall')", out)
        # Again: nothing left to remove, the same reloads; without a user, only the system side.
        self.write(self.paths.hwp_boost, "1\n")
        _, out, _ = self.command("uninstall")
        self.assertNotIn("removed", out)
        self.assertEqual(self.read(self.paths.hwp_boost), "1\n", "no hwp file: the boost is left as it is")
        self.assertIn(f"{inst} remove-text {self.paths.hwp_tmpfiles}", self.runner.joined())
        self.runner.calls.clear()
        self.command("uninstall", user="")
        self.assertNotIn("systemctl --user -M @ daemon-reload", self.runner.joined())

    def test_an_installer_that_fails_on_the_user_drop_ins_does_not_stop_uninstall(self) -> None:
        # The user's install record was written by a newer CST (or their drop-in folder is not theirs):
        # the installer fails. The drop-ins go as before the record, and both managers are reloaded.
        dropins = [toolbox_dropin(self.home), helper_dropin(self.home)]
        for f in dropins:
            self.write(f, "")
        self.runner.answers = [("remove-text --home", CommandResult(1, "", ""))]
        rc, out, err = self.command("uninstall")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            err, "WARNING: the install record of tester was not changed (see above); removing the drop-ins without it\n"
        )
        for f in dropins:
            self.assertFalse(os.path.exists(f), f)
            self.assertIn(f"removed '{f}'\n", out)
        self.assertEqual(
            self.runner.joined()[-2:], ["systemctl --user -M tester@ daemon-reload", "systemctl daemon-reload"]
        )

    def test_the_game_drop_ins_go_in_the_order_of_the_locale_s_collation(self) -> None:
        # bash's glob lists them by the locale's collation (en_US puts user@1000.service.d before
        # user-1000.slice.d); a key that reverses the code-point order stands in for one.
        rt = self.paths.unit_runtime
        units = ["user-1000.slice", "user.slice", "user@1000.service"]
        for u in units:
            self.write(f"{rt}/{u}.d/zz-cst-game.conf", "")
        with unittest.mock.patch("locale.strxfrm", lambda s: "".join(chr(0x10FFFF - ord(c)) for c in s)):
            _, out, _ = self.command("uninstall")
        removed = [line for line in out.split("\n") if line.startswith("removed '") and "zz-cst-game" in line]
        self.assertEqual(removed, [f"removed '{rt}/{u}.d/zz-cst-game.conf'" for u in sorted(units, reverse=True)])

    def test_the_root_commands_sort_names_as_bash_did(self) -> None:
        h = Handed(script=f"{REPO}/cpu-setup.sh", arg0="cpu-setup.sh", cst_prog="", sudo_user="tester", quiet={})
        with (
            unittest.mock.patch("cpu.setup.wiring.collate_as_bash") as collate,
            unittest.mock.patch("cpu.setup.wiring.target_user", return_value="tester"),
        ):
            setup_wiring.root_command(h, "cst cpu")
        collate.assert_called_once_with()


class DockerSliceTest(CpuSetupCase):
    def test_install_status_uninstall(self) -> None:
        dj = self.paths.daemon_json
        rc, out, _ = self.command("docker-slice", "install")
        self.assertEqual(rc, 0)
        self.assertEqual(self.read(dj), '{\n  "cgroup-parent": "builds.slice"\n}\n')
        self.assertIn(f"written:   {dj} (cgroup-parent: builds.slice)\n", out)
        self.assertTrue(out.endswith("sudo systemctl restart docker\n"))
        inst = " ".join(INSTALLER)
        self.assertEqual(
            self.runner.joined()[:4],
            [
                f"{inst} install --set ALLOWED=AllowedCPUs=8-23 --component cpu:docker-slice",
                "systemctl daemon-reload",
                "systemctl start builds.slice",
                f"{inst} mark --component cpu:docker-slice done",
            ],
        )
        _, out, _ = self.command("docker-slice", "install")
        self.assertIn(f"unchanged: {dj}\n", out)
        os.makedirs(os.path.join(self.paths.builds_cgroup, "docker-a.scope"))
        self.write(os.path.join(self.paths.builds_cgroup, "other"), "")
        self.runner.answers = [("systemctl is-active builds.slice", CommandResult(0, "active\n", ""))]
        self.assertEqual(
            self.command("docker-slice"),
            (0, "builds.slice: active; daemon.json cgroup-parent: builds.slice; containers in it: 1\n", ""),
        )
        self.write(dj, '{"cgroup-parent": "builds.slice", "a": [1, 2]}')
        _, out, _ = self.command("docker-slice", "uninstall")
        self.assertEqual(self.read(dj), '{\n  "a": [\n    1,\n    2\n  ]\n}')  # no newline, as the old writer
        self.assertTrue(out.startswith(f"removed cgroup-parent from {dj}\n"))
        self.write(dj, '{"cgroup-parent": "builds.slice"}')
        self.command("docker-slice", "uninstall")
        self.assertFalse(os.path.exists(dj))

    def test_another_parent_is_left_alone_and_bad_files_stop_it(self) -> None:
        dj = self.paths.daemon_json
        self.write(dj, '{"cgroup-parent": "other.slice"}')
        rc, out, err = self.command("docker-slice", "install")
        self.assertEqual((rc, err), (1, f"{dj} already sets cgroup-parent='other.slice': left alone\n"))
        self.assertNotIn("Restart Docker", out)
        self.write(dj, "{bad")
        self.assertEqual(self.command("docker-slice", "install")[0], 1)
        self.assertEqual(self.command("docker-slice", "uninstall")[0], 1)
        self.assertIn("daemon.json cgroup-parent: -;", self.command("docker-slice", "status")[1])
        self.write(dj, '{"cgroup-parent": 5}')
        self.assertIn("daemon.json cgroup-parent: 5;", self.command("docker-slice", "status")[1])
        for text in ("[1]", '"x"', "null"):
            with self.subTest(text=text):
                self.write(dj, text)
                self.assertEqual(self.command("docker-slice", "install")[::2], (1, f"{dj}: not a JSON object\n"))
                self.assertEqual(self.command("docker-slice", "uninstall")[::2], (1, f"{dj}: not a JSON object\n"))
                self.assertIn("daemon.json cgroup-parent: -;", self.command("docker-slice", "status")[1])
                self.assertEqual(self.read(dj), text)
        self.assertEqual(self.command("docker-slice", "bogus"), (2, "", "docker-slice: install|status|uninstall\n"))
        os.remove(self.paths.e_cores)
        self.runner.calls.clear()
        os.remove(dj)
        self.command("docker-slice", "install")
        self.assertIn("--set ALLOWED= --component cpu:docker-slice", self.runner.joined()[0])


class StagedInstaller:
    """The real installer, staged under a folder of the test (--root after its command)."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        self.real = SubprocessCommandRunner()

    def run(self, argv: Sequence[str], **kw: object) -> CommandResult:
        if argv[:1] == [SYSTEM_PYTHON]:
            return self.real.run([*argv[:5], "--root", self.stage, *argv[5:]], output=OutputMode.CAPTURE)
        return CommandResult(0, "", "")


class DockerSliceTemplateTest(CpuSetupCase):
    """`docker-slice install` writes cpu:docker-slice (builds.slice, a template) through the real
    installer: the file, plus AllowedCPUs= only when there are E-cores."""

    def install(self, ecores: str | None) -> str:
        stage = os.path.join(self.tmp, "stage")
        shutil.rmtree(stage, ignore_errors=True)
        if ecores is None:
            os.remove(self.paths.e_cores)
        else:
            self.write(self.paths.e_cores, ecores + "\n")
        s, _, err = self.setup_for(runner=StagedInstaller(stage))
        self.assertEqual(s.command("docker-slice", ["install"]), 0, err.getvalue())
        with open(os.path.join(stage, "etc/systemd/system/builds.slice")) as f:
            return f.read()

    def test_with_and_without_e_cores(self) -> None:
        with open(os.path.join(REPO, "cpu", "units", "builds.slice.in")) as f:
            plain = f.read().replace("@ALLOWED@\n", "")
        self.assertEqual(self.install("16-23"), plain + "AllowedCPUs=16-23\n")
        self.assertEqual(self.install(None), plain)
        self.assertNotIn("@", plain.split("[Slice]")[1])


class ExperimentTest(CpuSetupCase):
    def test_list_on_off(self) -> None:
        t = self.tmp
        rc, out, _ = self.command("experiment")
        self.assertEqual(rc, 0)
        self.assertEqual(
            out,
            f"  minfree-256m   {t}/proc/min_free_kbytes -> 262144   now: 67584\n"
            f"  thp-defer      {t}/sys/defrag -> defer    now: always defer [madvise] never\n",
        )
        rc, out, _ = self.command("experiment", "on", "thp-defer")
        self.assertEqual(
            (rc, out),
            (
                0,
                f"thp-defer: on ({t}/sys/defrag = defer; runtime only, reverted by 'cst cpu experiment off thp-defer' or a reboot)\n",
            ),
        )
        self.assertEqual(self.read(self.paths.saved("thp-defer")), "madvise\n")
        self.assertEqual(self.read(f"{t}/sys/defrag"), "defer\n")
        self.assertIn("(ON)", self.command("experiment", "list")[1])
        self.command("experiment", "on", "thp-defer")  # a second on keeps the first saved value
        self.assertEqual(self.read(self.paths.saved("thp-defer")), "madvise\n")
        self.assertEqual(
            self.command("experiment", "off", "thp-defer"), (0, f"thp-defer: off ({t}/sys/defrag restored)\n", "")
        )
        self.assertEqual(self.read(f"{t}/sys/defrag"), "madvise\n")
        self.assertEqual(self.command("experiment", "off", "thp-defer"), (0, "thp-defer: not on\n", ""))

    def test_a_value_that_cannot_be_read_saves_nothing(self) -> None:
        # The knob's file cannot be read (a folder: open fails for any user, root too): on stops before
        # it saves anything, so off has nothing to put back and writes nothing into the knob.
        knob = f"{self.tmp}/sys/defrag"
        os.remove(knob)
        os.makedirs(knob)
        rc, out, err = self.command("experiment", "on", "thp-defer")
        self.assertEqual((rc, out, err), (2, "", f"sed: can't read {knob}: Is a directory\n"))
        self.assertFalse(os.path.exists(self.paths.saved("thp-defer")))
        self.assertEqual(self.command("experiment", "off", "thp-defer"), (0, "thp-defer: not on\n", ""))
        self.assertEqual(os.listdir(knob), [])

    def test_usage_and_unknown_names(self) -> None:
        self.assertEqual(self.command("experiment", "bogus"), (2, "", "experiment: list|on NAME|off NAME\n"))
        self.assertEqual(
            self.command("experiment", "on", "nope"),
            (2, "", "experiment: unknown 'nope' (see: cst cpu experiment list)\n"),
        )
        self.assertEqual(self.command("experiment", "on")[0], 1)


class GameAndVmPinTest(CpuSetupCase):
    def test_game_by_hand(self) -> None:
        self.runner.answers = [("cpu-game-profile state", CommandResult(5, "", ""))]
        self.assertEqual(self.command("game", "on")[0], 5)
        self.assertTrue(os.path.exists(os.path.join(self.paths.run, "manual")))
        self.assertEqual(self.runner.joined(), ["systemctl start cpu-game.service", "cpu-game-profile state"])
        self.runner.calls.clear()
        self.command("game", "off")
        self.assertTrue(os.path.exists(os.path.join(self.paths.run, "suppress")))
        self.assertEqual(self.runner.joined(), ["systemctl stop cpu-game.service", "cpu-game-profile state"])
        self.runner.calls.clear()
        for args in ((), ("state",), ("",)):
            self.command("game", *args)
        self.assertEqual(self.runner.joined(), [f"python3 -I -B {REPO}/cpu/game-profile state"] * 3)
        self.assertEqual(self.command("game", "bogus"), (2, "", "game: on|off|state\n"))
        self.runner.answers = [("systemctl start", CommandResult(1, "", ""))]
        self.runner.calls.clear()
        self.assertEqual(self.command("game", "on")[0], 1)
        self.assertEqual(self.runner.joined(), ["systemctl start cpu-game.service"])

    def test_vm_pin_runs_in_the_foreground(self) -> None:
        self.assertEqual(self.command("vm-pin", "apply", "vm1")[0], 3)
        self.assertEqual(self.passed, [["python3", "-I", "-B", f"{REPO}/cpu/vm-pin", "apply", "vm1"]])


class StatusTest(CpuSetupCase):
    def test_the_whole_picture(self) -> None:
        self.write(self.paths.sched_ext, "enabled\n")
        self.write(self.paths.preempt, "none voluntary (full) lazy\n")
        self.write(self.paths.hwp_tmpfiles, "")
        self.write(gamemode_ini(self.home), "; Managed by cpu-setup.sh (CST).\n")
        ucg = f"{self.paths.cgroot}/user.slice/user-1000.slice/user@1000.service"
        self.write(f"{ucg}/cgroup.subtree_control", "cpuset cpu\n")
        self.write(f"{ucg}/user.slice/cpuset.cpus.effective", "8-23\n")
        vm = "".join(f"vm {i}\n" for i in range(1, 7))
        self.runner.answers = [
            ("game-profile state", CommandResult(0, "game profile: off\n  x\n", "")),
            ("is-active cpu-game-watchdog.timer", CommandResult(0, "active\n", "")),
            ("id -nG tester", CommandResult(0, "tester gamemode\n", "")),
            ("id -u tester", CommandResult(0, "1000\n", "")),
            ("is-active gamemoded.service", CommandResult(0, "active\n", "")),
            ("show -P CPUWeight machine.slice", CommandResult(0, "50\n", "")),
            ("show -P CPUWeight user.slice", CommandResult(0, "30\n", "")),
            ("show -P AllowedCPUs user.slice", CommandResult(0, "8-23\n", "")),
            ("vm-pin status", CommandResult(0, vm, "")),
            ("is-active builds.slice", CommandResult(3, "inactive\n", "")),
            ("cpu-watch report", CommandResult(0, "".join(f"r{i}\n" for i in range(7)), "")),
        ]
        rc, out, err = self.command("status")
        self.assertEqual((rc, err), (0, ""))
        lines = out.split("\n")
        self.assertTrue(
            lines[0].startswith("kernel ") and lines[0].endswith(", scheduler EEVDF + sched_ext!, preempt full")
        )
        self.assertEqual(
            "\n".join(lines[1:]),
            "\n== game profile\n  game profile: off\n    x\n  watchdog timer: active\n"
            "\n== GameMode\n  group: member; ini: managed; gamemoded: active\n"
            f"  HWP dynamic boost: 0 (kept at boot by {self.paths.hwp_tmpfiles})\n"
            "\n== baseline weights\n  machine.slice 50, toolboxes 30, app.slice , background.slice \n"
            "\n== E-cores for toolboxes and host commands\n"
            "  cpuset delegated: yes; toolboxes AllowedCPUs=8-23; flatpak-spawn --host AllowedCPUs=\n"
            "  effective (cgroup): toolboxes [8-23]\n"
            "\n== VM\n  vm 1\n  vm 2\n  vm 3\n  vm 4\n  vm 6\n"
            "\n== Docker\n  builds.slice: inactive; daemon.json cgroup-parent: -; containers in it: 0\n"
            "\n== memory experiments\n"
            f"  minfree-256m   {self.tmp}/proc/min_free_kbytes -> 262144   now: 67584\n"
            f"  thp-defer      {self.tmp}/sys/defrag -> defer    now: always defer [madvise] never\n"
            "\n== last measurements\n  r2\n  r3\n  r4\n  r5\n  r6\n",
        )
        self.assertIn(
            f"runuser -u tester -- env HOME={self.home} python3 -I -B {REPO}/cpu/cpu-watch report", self.runner.joined()
        )

    def test_without_a_target_user_the_user_parts_are_left_out_and_it_exits_1(self) -> None:
        rc, out, _ = self.command("status", user="")
        self.assertEqual(rc, 1)
        self.assertIn("\n== GameMode\n  HWP dynamic boost: 0 (not managed)\n", out)
        self.assertIn("\n== E-cores for toolboxes and host commands\n\n== VM\n", out)
        self.assertTrue(out.endswith("\n== last measurements\n"))
        self.assertFalse([c for c in self.runner.joined() if c.startswith("runuser")])

    def test_target_user(self) -> None:
        conf = self.paths.conf
        self.assertEqual(target_user("alice", conf), "alice")
        self.assertEqual(target_user("root", conf), "")  # no settings file
        for text, want in (
            ("TARGET_USER='bob'\n", "bob"),
            ("TARGET_USER=bob\n", "bob"),
            ("X=1\nTARGET_USER='a'\nTARGET_USER=b\n", "a\nb"),
        ):
            with self.subTest(text=text):
                self.write(conf, text)
                self.assertEqual(target_user("", conf), want)
                self.assertEqual(target_user("root", conf), want)

    def test_no_e_cores_reads_as_none(self) -> None:
        s, _, _ = self.setup_for()
        self.assertEqual(s.e_cores(), "8-23")
        os.remove(self.paths.e_cores)
        self.assertEqual(s.e_cores(), "")


class BaselineDropinTest(unittest.TestCase):
    def test_machine_slice_dropin_has_cpu_and_io_weight(self) -> None:
        with open(os.path.join(REPO, "cpu", "units", "machine.slice.d-10-cpu-setup.conf")) as f:
            lines = [line.strip() for line in f if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(lines, ["[Slice]", "CPUWeight=50", "IOWeight=50"])

    def test_baseline_dropins_sort_before_the_runtime_overrides(self) -> None:
        # systemd reads drop-ins in file-name order (byte order) and the last one read wins: the
        # baseline must come first, so that the 50-<Prop> files of `systemctl set-property --runtime`
        # win after a reload.
        self.assertEqual(DROPIN_NAME, "10-cpu-setup.conf")
        self.assertEqual(os.path.basename(SetupPaths().machine_dropin), DROPIN_NAME)
        self.assertTrue(os.path.exists(os.path.join(REPO, "cpu", "units", f"machine.slice.d-{DROPIN_NAME}")))
        overrides = [
            "50-CPUWeight.conf",
            "50-IOWeight.conf",
            "50-AllowedCPUs.conf",
            "50-MemoryLow.conf",
            "50-MemoryZSwapWriteback.conf",
        ]
        self.assertEqual(min([*overrides, DROPIN_NAME], key=os.fsencode), DROPIN_NAME)
        # The old name sorted after them (upper case before lower case): the bug.
        self.assertEqual(max([*overrides, OLD_DROPIN_NAME], key=os.fsencode), OLD_DROPIN_NAME)
        # The user drop-ins get the same name.
        self.assertEqual(toolbox_dropin("/home/u"), "/home/u/.config/systemd/user/user.slice.d/10-cpu-setup.conf")
        self.assertEqual(
            helper_dropin("/home/u"), "/home/u/.config/systemd/user/flatpak-session-helper.service.d/10-cpu-setup.conf"
        )


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: MPL-2.0
"""How vm-setup.sh and toolchains-setup.sh hand a command over to their program.

Each runs as a scratch copy (the script, cst_core, a fake program), with
fakes on PATH for python3 (the program's interpreter), sudo, runuser and
flatpak-spawn. A fake writes what it got (its arguments, the environment it
started with, its process ids, its ignored signals) and stops. The tests
pin what a caller sees: the help, the exit codes and messages, the command
each route execs (the program, sudo, runuser, flatpak-spawn) with its
environment, and that the program replaces the script (signals, Ctrl-C,
exit status, a closed pipe, the caller's LC_CTYPE).

The root routes need EUID 0: they run in a user namespace (unshare -r),
where the process is root without any privilege on the machine. Where
unprivileged user namespaces are off (CI's container), those tests skip;
tier 1's test_73 runs the same routes as real root.
"""

from collections.abc import Mapping, Sequence
import os
import pwd
import re
import shutil
import signal
import subprocess
import tempfile
from typing import ClassVar, Final, override
import unittest

REPO: Final = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS: Final = {"vm": "vm/cst-vm", "toolchains": "toolchains/cst-toolchains"}
# The real programs the scripts and the fakes use (PATH holds only these and the fakes).
REAL: Final = ("bash", "readlink", "dirname", "sed", "id", "getent", "cut", "sleep", "yes", "head", "env", "cat")
CANNOT_REACH: Final = "(cannot reach the host from this container: showing what is visible here)\n"

# A fake command: one line per fact, then what FAKE_MODE asks.
FAKE: Final = r"""#!@BASH@
printf 'FAKE %s\n' "${0##*/}"
for a in "$@"; do printf 'ARG %s\n' "$a"; done
printf 'PID %s %s\n' "$$" "$PPID"
# bash ignores Ctrl-\ itself: a program it starts (cat) shows what the fake inherited.
status="$(exec cat /proc/self/status)"
while read -r k v _; do [[ $k == SigIgn: ]] && printf 'SIGIGN %s\n' "$v"; done <<<"$status"
while IFS= read -r -d '' e; do printf 'ENV %s\n' "$e"; done </proc/$$/environ
printf 'END\n'
case "${FAKE_MODE:-}" in
    kill) kill -TERM $$ ;;
    trap) trap 'kill $! 2>/dev/null; echo cleanup done; exit 130' INT; echo started; sleep 10 >/dev/null 2>&1 & wait $! ;;
    pipe) yes | head -n 1; echo "pipe=${PIPESTATUS[*]}" ;;
esac
exit "${FAKE_RC:-0}"
"""


class Seen:
    """What one fake recorded."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.argv: list[str] = []
        self.pid = 0
        self.ppid = 0
        self.sigign = 0
        self.env: dict[str, str] = {}


def records(stdout: str) -> list[Seen]:
    """The fakes' records in stdout, in order."""
    seen: list[Seen] = []
    for line in stdout.splitlines():
        word, _, rest = line.partition(" ")
        if word == "FAKE":
            seen.append(Seen(rest))
        elif seen and word == "ARG":
            seen[-1].argv.append(rest)
        elif seen and word == "PID":
            pid, ppid = rest.split()
            seen[-1].pid, seen[-1].ppid = int(pid), int(ppid)
        elif seen and word == "SIGIGN":
            seen[-1].sigign = int(rest, 16)
        elif seen and word == "ENV":
            key, _, value = rest.partition("=")
            seen[-1].env[key] = value
    return seen


def help_text(script: str, prog: str) -> str:
    """The help a script prints from its header: lines 2 to the first empty one, the
    license line left out, "# " taken off, then the usage line."""
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


_USERNS: list[bool] = []


def userns_root() -> bool:
    """unshare -r works here: a user namespace where this user is root (no privilege outside)."""
    if not _USERNS:
        unshare = shutil.which("unshare", path="/usr/bin:/bin")
        ok = False
        if unshare is not None:
            probe = subprocess.run(
                [unshare, "-r", "true"], capture_output=True, stdin=subprocess.DEVNULL, check=False, timeout=30
            )
            ok = probe.returncode == 0
        _USERNS.append(ok)
    return _USERNS[0]


class _Copy(unittest.TestCase):
    """A scratch copy of one module's setup script with a fake program and fake commands."""

    module = ""

    @override
    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix=f"cst-route-{self.module}-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.script = os.path.join(self.tmp, f"{self.module}-setup.sh")
        shutil.copy2(os.path.join(REPO, f"{self.module}-setup.sh"), self.script)
        shutil.copytree(
            os.path.join(REPO, "cst_core"),
            os.path.join(self.tmp, "cst_core"),
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        self.tool = os.path.join(self.tmp, TOOLS[self.module])
        os.makedirs(os.path.dirname(self.tool))
        with open(self.tool, "w", encoding="utf-8") as fh:
            fh.write("raise SystemExit('the fake python3 runs instead')\n")
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        for name in REAL:
            found = shutil.which(name, path="/usr/bin:/bin")
            assert found is not None, name
            os.symlink(found, os.path.join(self.bin, name))
        self.fake("python3")

    def fake(self, name: str) -> None:
        """Put a fake name on PATH (replacing a real one)."""
        path = os.path.join(self.bin, name)
        if os.path.lexists(path):
            os.unlink(path)
        bash = shutil.which("bash", path="/usr/bin:/bin")
        assert bash is not None
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(FAKE.replace("@BASH@", bash))
        os.chmod(path, 0o755)

    def env(self, **extra: str) -> dict[str, str]:
        return {"PATH": self.bin, **extra}

    def require_userns_root(self) -> None:
        """Skip the whole test (one skip under its own id, before any subTest) without unshare -r."""
        if not userns_root():
            self.skipTest("needs an unprivileged user namespace (unshare -r); tier 1 test_73 runs it as root")

    def run_script(
        self, *args: str, env: Mapping[str, str], root: bool = False, argv0: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd = ["bash", argv0 or self.script, *args]
        if root:
            assert userns_root(), "call require_userns_root() first"
            cmd = ["/usr/bin/unshare", "-r", *cmd]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=dict(env),
            cwd=self.tmp,
            stdin=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )

    def started_env(self, caller: Mapping[str, str], **added: str) -> dict[str, str]:
        """The environment a program gets: the caller's, what bash adds for a script (exec takes
        its SHLVL back down: 0 with none set), and added."""
        return {**caller, "PWD": self.tmp, "SHLVL": "0", **added}

    def assert_ran(self, seen: Seen, name: str, argv: Sequence[str], env: Mapping[str, str]) -> None:
        self.assertEqual(seen.name, name)
        self.assertEqual(seen.argv, list(argv))
        got = {k: v for k, v in seen.env.items() if k != "_"}  # bash's "last command", not a setting
        self.assertEqual(got, dict(env))

    def only(self, stdout: str, stderr: str = "") -> Seen:
        seen = records(stdout)
        self.assertEqual(len(seen), 1, stdout + stderr)
        return seen[0]


class VmRoutingTest(_Copy):
    module = "vm"

    def test_help_and_an_empty_command(self) -> None:
        for args in ((), ("",), ("help",), ("-h",), ("--help",), ("help", "x")):
            with self.subTest(args=args):
                proc = self.run_script(*args, env=self.env(CST_PROG="cst vm"))
                self.assertEqual((proc.returncode, proc.stderr), (0, ""))
                self.assertEqual(proc.stdout, help_text(self.script, "cst vm"))
                self.assertIn("  backup [VM] [--full]   (root) the VM must be shut off.", proc.stdout)
                self.assertNotIn("SPDX", proc.stdout)
        # Without cst: the name the script was started with.
        proc = self.run_script("help", env=self.env(), argv0="./vm-setup.sh")
        self.assertTrue(proc.stdout.endswith("\nUsage: ./vm-setup.sh <command> [args]\n"), proc.stdout[-80:])
        proc = self.run_script("help", env=self.env(CST_PROG=""))
        self.assertTrue(proc.stdout.endswith(f"\nUsage: {self.script} <command> [args]\n"))

    def test_usage_errors_exit_2_before_anything_else(self) -> None:
        tune = "Usage: cst vm tune plan|apply [VM] [--guest-has-virtio]\n"
        nocow = "Usage: cst vm nocow plan [--root] | nocow apply\n"
        timer = "Usage: cst vm backup-timer on|off|status [VM]\n"
        for container in ("1", "0"):
            for args, err in (
                (("tune",), tune),
                (("tune", ""), tune),
                (("tune", "x", "plan"), tune),
                (("nocow",), nocow),
                (("nocow", "bogus"), nocow),
                (("backup-timer",), timer),
                (("backup-timer", "go"), timer),
                (("nope", "status"), "Unknown command: nope\n" + help_text(self.script, "cst vm")),
            ):
                with self.subTest(args=args, container=container):
                    proc = self.run_script(*args, env=self.env(CST_PROG="cst vm", CST_VM_CONTAINER=container))
                    self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (2, "", err))

    def test_read_commands_on_the_host_run_the_program(self) -> None:
        for args in (
            ("list",),
            ("status", "w", "--x"),
            ("tune", "plan", "w", "--guest-has-virtio"),
            ("nocow", "plan"),
            ("backup-timer", "status", "w"),
        ):
            for caller in ({"CST_PROG": "cst vm"}, {}):
                with self.subTest(args=args, caller=caller):
                    env = self.env(CST_VM_CONTAINER="0", **caller)
                    proc = self.run_script(*args, env=env)
                    self.assertEqual((proc.returncode, proc.stderr), (0, ""))
                    prog = caller.get("CST_PROG", self.script)
                    self.assert_ran(
                        self.only(proc.stdout),
                        "python3",
                        ["-I", "-B", self.tool, *args],
                        self.started_env(env, CST_PROG=prog),
                    )

    def test_read_commands_in_a_toolbox_go_through_flatpak_spawn(self) -> None:
        self.fake("flatpak-spawn")
        for caller in ({"CST_PROG": "cst vm"}, {}, {"CST_PROG": ""}):
            with self.subTest(caller=caller):
                env = self.env(CST_VM_CONTAINER="1", **caller)
                proc = self.run_script("status", "w", env=env)
                self.assertEqual((proc.returncode, proc.stderr), (0, ""))
                prog = caller.get("CST_PROG") or self.script
                argv = ["--host", f"--env=CST_PROG={prog}", "python3", "-I", "-B", self.tool, "status", "w"]
                self.assert_ran(self.only(proc.stdout), "flatpak-spawn", argv, self.started_env(env))

    def test_without_flatpak_spawn_a_container_answers_from_here(self) -> None:
        env = self.env(CST_VM_CONTAINER="1", CST_PROG="cst vm")
        proc = self.run_script("list", env=env)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))
        self.assertTrue(proc.stdout.startswith(CANNOT_REACH + "FAKE python3\n"), proc.stdout[:200])
        self.assert_ran(self.only(proc.stdout), "python3", ["-I", "-B", self.tool, "list"], self.started_env(env))

    def test_changing_commands_refuse_in_a_toolbox(self) -> None:
        self.fake("sudo")
        self.fake("flatpak-spawn")
        for args in (
            ("backup",),
            ("backup", "w", "--nas"),
            ("backup-timer", "on", "w"),
            ("backup-timer", "off"),
            ("tune", "apply", "w"),
            ("nocow", "apply"),
            ("nocow", "plan", "--root"),
            ("nocow", "plan", "x", "--root"),
        ):
            with self.subTest(args=args):
                proc = self.run_script(*args, env=self.env(CST_PROG="cst vm", CST_VM_CONTAINER="1"))
                words = " ".join(args)
                err = (
                    f"'cst vm {words}' must run on the host, not inside a toolbox/container.\n"
                    f"In a host terminal: cst vm {words}\n"
                )
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (1, "", err))

    def test_root_commands_run_the_script_again_through_sudo(self) -> None:
        self.fake("sudo")
        for args in (
            ("backup", "w", "--nas"),
            ("backup-timer", "on", "w"),
            ("tune", "apply"),
            ("nocow", "plan", "--root"),
        ):
            for caller, handed in (
                ({"CST_PROG": "cst vm"}, ["env", "CST_PROG=cst vm"]),
                ({}, []),
                ({"CST_PROG": ""}, []),
            ):
                with self.subTest(args=args, caller=caller):
                    env = self.env(CST_VM_CONTAINER="0", **caller)
                    proc = self.run_script(*args, env=env)
                    self.assertEqual((proc.returncode, proc.stderr), (0, ""))
                    self.assert_ran(
                        self.only(proc.stdout), "sudo", ["--", *handed, self.script, *args], self.started_env(env)
                    )
        proc = self.run_script("backup", env=self.env(CST_VM_CONTAINER="0"))
        self.assertEqual(self.only(proc.stdout).argv, ["--", self.script, "backup"])

    def test_nocow_apply_runs_as_the_caller(self) -> None:
        self.fake("sudo")
        env = self.env(CST_VM_CONTAINER="0", CST_PROG="cst vm")
        proc = self.run_script("nocow", "apply", "x", env=env)
        self.assert_ran(
            self.only(proc.stdout),
            "python3",
            ["-I", "-B", self.tool, "nocow", "apply", "x"],
            self.started_env(env, CST_PROG="cst vm"),
        )

    def test_as_root_the_program_runs_directly(self) -> None:
        self.require_userns_root()
        self.fake("sudo")
        for caller, prog in (({"CST_PROG": "cst vm"}, "cst vm"), ({}, self.script)):
            with self.subTest(caller=caller):
                env = self.env(CST_VM_CONTAINER="0", **caller)
                proc = self.run_script("backup", "w", "--full", env=env, root=True)
                self.assertEqual((proc.returncode, proc.stderr), (0, ""))
                self.assert_ran(
                    self.only(proc.stdout),
                    "python3",
                    ["-I", "-B", self.tool, "backup", "w", "--full"],
                    self.started_env(env, CST_PROG=prog),
                )
        proc = self.run_script("backup", env=self.env(CST_VM_CONTAINER="1"), root=True)
        self.assertEqual((proc.returncode, proc.stdout), (1, ""), "a toolbox refuses root too")


class ToolchainsRoutingTest(_Copy):
    module = "toolchains"

    def test_help_and_unknown_commands(self) -> None:
        for args in ((), ("",), ("help",), ("-h",), ("--help",)):
            with self.subTest(args=args):
                proc = self.run_script(*args, env=self.env(CST_PROG="cst toolchains"))
                self.assertEqual(
                    (proc.returncode, proc.stdout, proc.stderr), (0, help_text(self.script, "cst toolchains"), "")
                )
                self.assertIn("  prune plan [TARGET...] dry run", proc.stdout)
        proc = self.run_script("nope", env=self.env(CST_PROG="cst toolchains"))
        self.assertEqual(
            (proc.returncode, proc.stdout, proc.stderr),
            (2, "", "Unknown command: nope\n" + help_text(self.script, "cst toolchains")),
        )

    def test_as_a_user_the_program_runs_with_its_arguments(self) -> None:
        for args in (("status", "--brief"), ("prune", "bogus"), ("auto", "on"), ("config", "apply", "--home", "/x")):
            for caller in ({"CST_PROG": "cst toolchains"}, {}):
                with self.subTest(args=args, caller=caller):
                    env = self.env(**caller)
                    proc = self.run_script(*args, env=env)
                    self.assertEqual((proc.returncode, proc.stderr), (0, ""))
                    prog = caller.get("CST_PROG", self.script)
                    self.assert_ran(
                        self.only(proc.stdout),
                        "python3",
                        ["-I", "-B", self.tool, *args],
                        self.started_env(env, CST_PROG=prog),
                    )

    def _runuser_argv(self, user: str, handed: Sequence[str], args: Sequence[str]) -> list[str]:
        entry = pwd.getpwnam(user)
        return [
            "-u",
            user,
            "--",
            "env",
            "-u",
            "XDG_CONFIG_HOME",
            "-u",
            "SUDO_USER",
            f"HOME={entry.pw_dir}",
            f"USER={user}",
            f"LOGNAME={user}",
            f"XDG_RUNTIME_DIR=/run/user/{entry.pw_uid}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{entry.pw_uid}/bus",
            *handed,
            self.script,
            *args,
        ]

    def test_as_root_it_runs_again_as_the_sudo_user(self) -> None:
        self.require_userns_root()
        self.fake("runuser")
        me = pwd.getpwuid(os.getuid()).pw_name
        for caller, handed in (
            ({"CST_PROG": "cst toolchains"}, ["CST_PROG=cst toolchains"]),
            ({}, []),
            ({"CST_PROG": ""}, []),
        ):
            with self.subTest(caller=caller):
                env = self.env(SUDO_USER=me, XDG_CONFIG_HOME="/root/.config", **caller)
                proc = self.run_script("status", "--brief", env=env, root=True)
                self.assertEqual((proc.returncode, proc.stderr), (0, ""))
                self.assert_ran(
                    self.only(proc.stdout),
                    "runuser",
                    self._runuser_argv(me, handed, ["status", "--brief"]),
                    self.started_env(env),
                )
        proc = self.run_script("", env=self.env(SUDO_USER=me), root=True)
        self.assertEqual((proc.returncode, proc.stdout), (0, help_text(self.script, self.script)))
        proc = self.run_script("nope", env=self.env(SUDO_USER=me), root=True)
        self.assertEqual((proc.returncode, proc.stdout), (2, ""))

    def test_as_root_without_a_sudo_user_it_refuses(self) -> None:
        self.require_userns_root()
        self.fake("runuser")
        err = (
            "cst toolchains: ERROR: run it from your user account (needs SUDO_USER): "
            "toolchain caches and settings belong to your user\n"
        )
        for caller in ({}, {"SUDO_USER": ""}, {"SUDO_USER": "root"}):
            with self.subTest(caller=caller):
                proc = self.run_script("status", env=self.env(CST_PROG="cst toolchains", **caller), root=True)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (1, "", err))
        proc = self.run_script("status", env=self.env(SUDO_USER="cst-no-such-user"), root=True)
        self.assertEqual((proc.returncode, proc.stdout), (1, ""))
        self.assertNotIn("Traceback", proc.stderr)
        self.assertTrue(proc.stderr, "it says why")


class _ExecChecks(_Copy):
    """What exec gives: the program is the process the caller started (signals, status, pipes, locale)."""

    read_args: tuple[str, ...] = ()
    caller_extra: ClassVar[Mapping[str, str]] = dict[str, str]()

    def _popen(self, extra: Mapping[str, str], prefix: Sequence[str] = ()) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [*prefix, "bash", self.script, *self.read_args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            cwd=self.tmp,
            env=self.env(**self.caller_extra, **extra),
            start_new_session=True,
        )

    def _seen(self, extra: Mapping[str, str], prefix: Sequence[str] = ()) -> Seen:
        proc = self._popen(extra, prefix)
        out, err = proc.communicate(timeout=60)
        return self.only(out, err)


class _ExecTests(_ExecChecks):
    def test_the_program_replaces_the_script(self) -> None:
        proc = self._popen({})
        out, err = proc.communicate(timeout=60)
        seen = self.only(out, err)
        self.assertEqual((seen.pid, seen.ppid), (proc.pid, os.getpid()))
        for rc in ("0", "7", "130"):
            proc = self._popen({"FAKE_RC": rc})
            proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, int(rc))

    def test_a_program_killed_by_a_signal(self) -> None:
        proc = self._popen({"FAKE_MODE": "kill"})
        proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, -signal.SIGTERM)

    def test_ctrl_c_reaches_the_program_and_its_trap(self) -> None:
        proc = self._popen({"FAKE_MODE": "trap"})
        assert proc.stdout is not None
        while proc.stdout.readline() not in ("started\n", ""):
            pass
        os.killpg(proc.pid, signal.SIGINT)
        out, err = proc.communicate(timeout=60)
        self.assertIn("cleanup done", out)
        self.assertNotIn("Traceback", err)
        self.assertEqual(proc.returncode, 130)

    def test_signals_at_their_default_and_an_ignored_one_stays_ignored(self) -> None:
        mask = sum(1 << (s - 1) for s in (signal.SIGINT, signal.SIGQUIT, signal.SIGPIPE, signal.SIGXFSZ))
        seen = self._seen({}, ["/usr/bin/env", "--default-signal=INT,QUIT,PIPE,XFSZ"])
        self.assertEqual(seen.sigign & mask, 0, f"SigIgn {seen.sigign:x}")
        proc = self._popen({"FAKE_MODE": "pipe"}, ["/usr/bin/env", "--default-signal=PIPE"])
        out, _ = proc.communicate(timeout=60)
        self.assertIn("pipe=141 0", out)
        seen = self._seen({}, ["/usr/bin/env", "--ignore-signal=INT"])
        self.assertTrue(seen.sigign & (1 << (signal.SIGINT - 1)), "an ignored Ctrl-C stays ignored (nohup-style)")

    def test_the_program_gets_the_caller_s_lc_ctype(self) -> None:
        for caller, value in (({}, None), ({"LANG": "C"}, None), ({"LC_CTYPE": "C"}, "C"), ({"LC_CTYPE": ""}, "")):
            with self.subTest(caller=caller):
                seen = self._seen(caller)
                self.assertEqual(seen.env.get("LC_CTYPE"), value)
                handed = [k for k in seen.env if k.startswith("CST_") and k not in ("CST_PROG", "CST_VM_CONTAINER")]
                self.assertEqual(handed, [], "nothing a wrapper hands over reaches the program")

    def test_help_into_a_closed_pipe(self) -> None:
        read_end, write_end = os.pipe()
        os.close(read_end)
        try:
            proc = subprocess.run(
                ["bash", "-c", '"$@"; echo "status=$?" >&2', "_", "bash", self.script, "help"],
                stdout=write_end,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                cwd=self.tmp,
                env=self.env(),
                check=False,
                timeout=60,
            )
        finally:
            os.close(write_end)
        # What the calling shell sees: 141 (128 + SIGPIPE), and nothing else on stderr.
        self.assertEqual(proc.stderr, "status=141\n")


class VmExecTest(_ExecTests):
    module = "vm"
    read_args = ("status", "w")
    caller_extra: ClassVar[Mapping[str, str]] = {"CST_VM_CONTAINER": "0"}


class ToolchainsExecTest(_ExecTests):
    module = "toolchains"
    read_args = ("status",)


del _ExecTests, _ExecChecks, _Copy  # bases only: unittest runs their subclasses


if __name__ == "__main__":
    unittest.main()

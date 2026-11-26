# SPDX-License-Identifier: MPL-2.0
"""`cst cpu`'s root commands on the machine: install, status, uninstall, game, vm-pin, docker-slice, experiment.

CpuSetup carries what every step needs (the paths, the command runner, the checkout, the name to show, the
target user) across the steps of a command. Each step runs what cpu-setup.sh ran, the way it ran it: the
installer (cst_core/installer.py, through runuser for the user's files), systemctl, the checkout's programs.
A command that cpu-setup.sh ran under set -e ends the command when it fails, with its exit status (Halt).
"""

from collections.abc import Callable, Mapping, Sequence
import contextlib
import fnmatch
import glob
import grp
import io
import locale
import os
import pwd
from typing import TextIO

from cst_core.adapters.files import ensure_dir, read_text, write_atomic
from cst_core.adapters.install import JsonInstallRecord
from cst_core.adapters.runtime import SYSTEM_PYTHON, lc_ctype_handover
from cst_core.adapters.terminal import stderr, stdout
from cst_core.domain.errors import shell_exit_status
from cst_core.domain.install import INSTALL_MANIFEST, ArtifactKind, select_entries
from cst_core.domain.record import ComponentState
from cst_core.domain.shapes import parse_json
from cst_core.domain.units import UNIT_PATH_PROP, show_manager_value_argv, user_manager_head, user_unit_dir
from cst_core.domain.values import ROOT_ACCOUNT
from cst_core.ports import CommandFault, CommandResult, CommandRunnerPort, InstallRecordPort, OutputMode
from perf.adapters import LinuxPerfProbe, PerfPaths
from perf.domain.evaluate import evaluate_perf_snapshot
from perf.domain.models import PerfAxis
from perf.presenter import render_perf_findings_block
from cpu.game.adapters.host import holds, read_stripped
from cpu.game.domain import APP_SLICE, BUILDS_SLICE, GAME_DROPIN, RUN_FOLDER_MODE, USER_SLICE, cpuset_in
from cpu.power.adapters import read_cpu_topology
from cpu.setup.adapters.experiments import ExperimentEnv, run_experiment
from cpu.setup.adapters.user_dropins import OlderDropins, classify_older_dropins
from cpu.setup.domain import (
    CORE_COMPONENT,
    CPU_POWER_SERVICE,
    DOCKER_SLICE_COMPONENT,
    GAMEMODE_COMPONENT,
    GAMEMODE_GROUP,
    MANAGED_MARK,
    TOOLBOX_COMPONENT,
    WATCHDOG_TIMER,
    DROPIN_NAME,
    OLD_DROPIN_NAME,
    CpuRootCommand,
    DaemonJson,
    DockerSliceAction,
    Halt,
    SchedExtState,
    SetupPaths,
    cgroup_parent_shown,
    docker_slice_status,
    first_arg,
    first_conf_lines,
    first_four_and_last,
    gamemode_ini,
    helper_dropin,
    helper_dropin_text,
    hwp_tmpfiles_text,
    indented,
    install_done_lines,
    is_daemon_json,
    json_text,
    last_lines,
    ntsync_modules_load_text,
    preempt_modes,
    target_user_in,
    toolbox_dropin,
    toolbox_dropin_text,
    uninstall_kept_lines,
    with_builds_parent,
)


def passwd_home(user: str) -> str:
    """The user's home folder (getent passwd), "" for an unknown user."""
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return ""


def group_exists(name: str) -> bool:
    """The group is known (getent group)."""
    try:
        grp.getgrnam(name)
    except KeyError:
        return False
    return True


def my_name() -> str:
    """This process's user name (id -un)."""
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return ""


def target_user(sudo_user: str, conf: str) -> str:
    """Whose session the commands are for: SUDO_USER, else (root, or none) the user install recorded."""
    if sudo_user and sudo_user != ROOT_ACCOUNT:
        return sudo_user
    return target_user_in(read_text(conf) or "")


def _make_run_dir(path: str, mode: int = RUN_FOLDER_MODE) -> None:
    """install -d -m 0755 PATH (the mode also for a folder already there)."""
    ensure_dir(path, mode)
    os.chmod(path, mode)


def _touch(path: str) -> None:
    """touch PATH: created when it is not there, its times set to now."""
    with open(path, "a", encoding="utf-8"):
        # Opening it is the whole point: append mode creates the file and leaves its bytes alone.
        pass
    os.utime(path)


class CpuSetup:
    """The root commands of `cst cpu`, over one runner, one set of paths and one checkout."""

    def __init__(
        self,
        paths: SetupPaths,
        runner: CommandRunnerPort,
        *,
        root: str,
        prog: str,
        user: str,
        passthrough: Callable[[Sequence[str]], int],
        home_of: Callable[[str], str] = passwd_home,
        has_group: Callable[[str], bool] = group_exists,
        me: Callable[[], str] = my_name,
        records: InstallRecordPort | None = None,
        out: TextIO | None = None,
        err: TextIO | None = None,
    ) -> None:
        self.paths = paths
        self._runner = runner
        self.root = root
        self.src = os.path.join(root, "cpu")
        self.prog = prog
        self.user = user
        self._passthrough = passthrough
        self._home_of = home_of
        self._has_group = has_group
        self._me = me
        self._records = JsonInstallRecord() if records is None else records
        self._unit_paths: dict[str, str | None] = {}
        self._out = out
        self._err = err

    # ---- output and commands --------------------------------------------------------------
    def _o(self) -> TextIO:
        return stdout() if self._out is None else self._out

    def _e(self) -> TextIO:
        return stderr() if self._err is None else self._err

    def say(self, line: str = "") -> None:
        """A line on stdout."""
        print(line, file=self._o(), flush=True)

    def warn(self, line: str) -> None:
        """A line on stderr."""
        print(line, file=self._e(), flush=True)

    def write(self, text: str) -> None:
        """Text on stdout as it is."""
        self._o().write(text)
        self._o().flush()

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        quiet: bool = False,
        capture: bool = False,
        merge: bool = False,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """A command as cpu-setup.sh ran it: its output on ours (quiet: its stderr dropped, as 2>/dev/null;
        capture: stdout kept for the caller, stderr on ours; merge: both kept, as 2>&1). check: set -e,
        a failure ends the command with the command's exit status."""
        self._o().flush()
        self._e().flush()
        output: OutputMode = OutputMode.INHERIT
        if merge:
            output = OutputMode.MERGE
        elif quiet:
            output = OutputMode.STDOUT
        elif capture:
            output = OutputMode.CAPTURE
        res = self._runner.run(argv, output=output, input_text=input_text, env=env)
        if quiet and not capture:
            self.write(res.stdout)
        if capture and not quiet and not merge and res.stderr:
            self._e().write(res.stderr)
            self._e().flush()
        if res.fault == CommandFault.MISSING:
            self.warn(f"{argv[0]}: command not found")
        elif res.fault is not None:
            self.warn(f"{argv[0]}: {res.stderr}")
        if check and res.returncode != 0:
            raise Halt(shell_exit_status(res.returncode))
        return res

    def usys(self, *args: str, check: bool = False, quiet: bool = True, capture: bool = False) -> CommandResult:
        """systemctl for the target user's manager (user_manager_head)."""
        return self.run([*user_manager_head(self.user), *args], check=check, quiet=quiet, capture=capture)

    # ---- the installer (what cst-install.bash runs) ----------------------------------------------
    def installer(self, *args: str, as_user: str = "", input_text: str | None = None) -> None:
        """cst_core/installer.py ARGS on the system Python, as as_user when it is another user (runuser)."""
        argv = [SYSTEM_PYTHON, "-I", "-B", os.path.join(self.root, "cst_core", "installer.py"), *args]
        if as_user and as_user != self._me():
            argv = ["runuser", "-u", as_user, "--", *argv]
        handover = lc_ctype_handover().partition("=")
        self.run(argv, input_text=input_text, env={handover[0]: handover[2]})

    def install_text(self, dest: str, text: str, owner: str = ROOT_ACCOUNT, component: str = "") -> None:
        """cst_install_text DEST 0644 [OWNER [COMPONENT]]: with a component, listed in the install record."""
        listed = ["--component", component] if component else []
        self.installer("text", dest, "0644", *listed, as_user="" if owner == ROOT_ACCOUNT else owner, input_text=text)

    def remove_user_text(self, *dests: str) -> None:
        """cst_remove_text_as USER DEST...: generated files of the target user removed with their record
        lines, as that user ('removed:' for each that was there); without a home there is none."""
        home = self._home_of(self.user)
        if home:
            self.installer("remove-text", "--home", home, *dests, as_user=self.user)

    def forget_user_text(self, *dests: str) -> None:
        """Forget record lines under the target user's home without deleting the files."""
        home = self._home_of(self.user)
        if home and dests:
            self.installer("forget-text", "--home", home, *dests, as_user=self.user)

    def required_home(self, user: str, who: str) -> str:
        """The user's home for a step in it (who: the helper that says so), or Halt(1)."""
        home = self._home_of(user)
        if not home:
            raise Halt(1, f"{who}: no home folder for user '{user}'")
        return home

    # ---- small steps ------------------------------------------------------------------------------
    def rm_v(self, *paths: str) -> None:
        """rm -fv PATH...: 'removed' for each that was there."""
        for p in paths:
            if os.path.lexists(p):
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(p)
                self.say(f"removed '{p}'")

    def rmdir(self, *paths: str) -> None:
        """rmdir PATH... 2>/dev/null || true."""
        for p in paths:
            with contextlib.suppress(OSError):
                os.rmdir(p)

    def _user_unit_path(self) -> str | None:
        """The target user manager's UnitPath, asked once per run; None when it cannot be read."""
        if not self.user:
            return None
        if self.user not in self._unit_paths:
            self._o().flush()
            self._e().flush()
            res = self._runner.run(
                show_manager_value_argv(user_manager_head(self.user), UNIT_PATH_PROP),
                check=False,
                timeout=20,
                locale_c=True,
                output=OutputMode.CAPTURE,
            )
            self._unit_paths[self.user] = res.stdout if res.returncode == 0 else None
        return self._unit_paths[self.user]

    def _user_unit_dir(self) -> str:
        """Where the target user manager reads its own units from (user_unit_dir)."""
        return user_unit_dir(self._user_unit_path(), self._home_of(self.user))

    def toolbox_dropin(self, name: str = DROPIN_NAME) -> str:
        return toolbox_dropin(self._user_unit_dir(), name)

    def helper_dropin(self, name: str = DROPIN_NAME) -> str:
        return helper_dropin(self._user_unit_dir(), name)

    def _older_dropins(self, tb_path: str, helper_path: str) -> OlderDropins:
        """Recorded older copies of the cpu:toolbox drop-ins split into remove vs forget-only."""
        home = self._home_of(self.user)
        if not home:
            return OlderDropins((), ())
        pairs = (
            (tb_path, f"user.slice.d/{DROPIN_NAME}"),
            (helper_path, f"flatpak-session-helper.service.d/{DROPIN_NAME}"),
        )
        return classify_older_dropins(self._records, home, self._user_unit_path(), pairs)

    def _clean_older_dropins(self, tb_path: str, helper_path: str) -> None:
        """Remove CST's older copies elsewhere than current; forget aliased or foreign ones."""
        older = self._older_dropins(tb_path, helper_path)
        self.forget_user_text(*older.forget)
        if older.remove:
            self.remove_user_text(*older.remove)
            self.rmdir(*(dict.fromkeys(os.path.dirname(p) for p in older.remove)))

    def remove_old_dropins(self) -> None:
        """The baseline drop-ins under their old name, from before the rename to 10-: they would still
        sort after the game profile's overrides."""
        old = [os.path.join(os.path.dirname(self.paths.machine_dropin), OLD_DROPIN_NAME)]
        if self.user:
            old += [self.toolbox_dropin(OLD_DROPIN_NAME), self.helper_dropin(OLD_DROPIN_NAME)]
        for f in old:
            if os.path.exists(f):
                self.rm_v(f)

    def remove_game_dropins(self) -> None:
        """The game profile's MemoryLow drop-ins, whatever unit they are for, in the order bash's glob lists
        them (the locale's collation: collate_as_bash)."""
        found = glob.glob(os.path.join(glob.escape(self.paths.unit_runtime), "*.d", GAME_DROPIN))
        for f in sorted(found, key=locale.strxfrm):
            if not os.path.exists(f):
                continue
            self.rm_v(f)
            self.rmdir(os.path.dirname(f))

    def in_gamemode_group(self) -> bool:
        """The target user is in the 'gamemode' group (id -nG)."""
        res = self.run(["id", "-nG", self.user], check=False, capture=True)
        return GAMEMODE_GROUP in res.stdout.split()

    def user_cg(self) -> str:
        """The user manager's cgroup folder (the uid asked twice, as the bash script's path did)."""
        uids = [self.run(["id", "-u", self.user], check=False, capture=True).stdout.strip() for _ in range(2)]
        return f"{self.paths.cgroot}/user.slice/user-{uids[0]}.slice/user@{uids[1]}.service"

    def cpuset_delegated(self) -> bool:
        """cpuset delegated to the user manager (active in subtree_control or offered in cgroup.controllers)."""
        ucg = self.user_cg()
        return any(
            cpuset_in(read_text(os.path.join(ucg, name)) or "")
            for name in ("cgroup.subtree_control", "cgroup.controllers")
        )

    def check_syntax(self, path: str) -> bool:
        """A program parses (python's ast, or bash -n)."""
        first = (read_text(path) or "").split("\n", 1)[0]
        if "python" in first:
            check = "import ast, sys; ast.parse(open(sys.argv[1]).read())"
            return self.run(["python3", "-I", "-c", check, path], check=False).returncode == 0
        return self.run(["bash", "-n", path], check=False).returncode == 0

    # ---- install -----------------------------------------------------------------------------------
    def install(self) -> int:
        """The game profile, baseline weights (VM, toolboxes, host commands), GameMode, cpu-watch, the cst link."""
        p, user = self.paths, self.user
        if not user or user == ROOT_ACCOUNT:
            raise Halt(1, "install: run through sudo from your user account (needs SUDO_USER)")
        srcs = [
            e.src
            for kind in (ArtifactKind.PYTHON_ENTRY, ArtifactKind.SHELL)
            for e in select_entries(INSTALL_MANIFEST, component=CORE_COMPONENT, kind=kind)
        ]
        for f in srcs:
            if not self.check_syntax(os.path.join(self.root, f)):
                raise Halt(1, f"{self.root}/{f}: syntax error")
        _make_run_dir(os.path.dirname(p.conf))
        if not os.path.exists(p.conf):
            self.install_text(p.conf, first_conf_lines(user, read_cpu_topology(p.sys_dir, p.p_cores, p.e_cores)))
        self.installer("install", "--set", f"USER={user}", "--component", CORE_COMPONENT)
        if os.access(p.hwp_boost, os.W_OK):
            self.install_text(p.hwp_tmpfiles, hwp_tmpfiles_text(p.hwp_boost), component=CORE_COMPONENT)
            if self._echo_to(p.hwp_boost, "1"):
                self.say(f"set:       {p.hwp_boost} = 1")
        else:
            self.warn(f"NOTE: {p.hwp_boost} not found (no intel_pstate in HWP mode): HWP dynamic boost skipped")
        self._install_ntsync()
        tb_path, helper_path = self.toolbox_dropin(), self.helper_dropin()
        self._clean_older_dropins(tb_path, helper_path)
        self.install_text(tb_path, toolbox_dropin_text(), user, TOOLBOX_COMPONENT)
        self.install_text(helper_path, helper_dropin_text(), user, TOOLBOX_COMPONENT)
        self.remove_old_dropins()  # the daemon-reloads below then read without them
        link = os.path.join(self.root, "cst-link.bash")
        self.run(["bash", "-c", 'set -Eeuo pipefail; source "$1"; link_cst_command "$2"', "cpu-setup.sh", link, user])
        gamemode = self._install_gamemode()
        self._gamemode_group()
        self.run(["systemctl", "daemon-reload"])
        self.usys("daemon-reload")
        self.run(["udevadm", "control", "--reload-rules"], check=False, quiet=True)
        self.run(["systemctl", "enable", "--now", WATCHDOG_TIMER])
        self.run(["systemctl", "enable", CPU_POWER_SERVICE])
        self.run(["systemctl", "restart", CPU_POWER_SERVICE])
        self.installer("mark", "--component", CORE_COMPONENT, ComponentState.DONE)
        home = self.required_home(user, "cst-install.bash")
        self.installer("mark", "--home", home, "--component", TOOLBOX_COMPONENT, ComponentState.DONE, as_user=user)
        if gamemode:
            self.installer("mark", "--home", home, "--component", GAMEMODE_COMPONENT, ComponentState.DONE, as_user=user)
        for line in install_done_lines(self.prog):
            self.say(line)
        return 0

    def _ntsync_active(self) -> bool:
        p = self.paths
        ntsync_nodes = (
            p.ntsync_dev,
            os.path.join(p.sys_dir, "class", "misc", "ntsync"),
            os.path.join(p.sys_dir, "module", "ntsync"),
        )
        return any(os.path.exists(node) for node in ntsync_nodes)

    def _install_ntsync(self) -> None:
        p = self.paths
        info = self.run(["modinfo", "ntsync"], check=False, quiet=True, capture=True)
        if info.returncode == 0 and "(builtin)" not in info.stdout:
            self.install_text(p.ntsync_modules_load, ntsync_modules_load_text(), component=CORE_COMPONENT)
            if not self._ntsync_active():
                if self.run(["modprobe", "ntsync"], check=False, quiet=True).returncode == 0:
                    self.say("loaded:    ntsync kernel module")
                else:
                    self.warn("NOTE: modprobe ntsync failed: autoload file written for next boot")
        elif self._ntsync_active():
            self.installer("remove-text", p.ntsync_modules_load)
        else:
            self.warn("NOTE: ntsync kernel module not available on this kernel: autoload skipped")

    def _echo_to(self, path: str, value: str) -> bool:
        """echo VALUE >PATH; bash's message on stderr when it fails."""
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(value + "\n")
        except OSError as e:
            self.warn(f"cpu-setup.sh: {path}: {e.strerror}")
            return False
        return True

    def _install_gamemode(self) -> bool:
        """Our gamemode.ini unless the user has one of their own; True when it was installed."""
        ini = gamemode_ini(self._home_of(self.user))
        if os.path.exists(ini) and not holds(ini, MANAGED_MARK):
            self.warn(f"NOTE: {ini} is your own: add [custom] start/end from {self.src}/gamemode.ini by hand")
            return False
        home = self.required_home(self.user, "cst_install_as")
        self.installer("install", "--home", home, "--component", GAMEMODE_COMPONENT, as_user=self.user)
        return True

    def _gamemode_group(self) -> None:
        """The user in the 'gamemode' group. Silverblue: system groups live in /usr/lib/group, and
        usermod needs them in /etc/group."""
        p = self.paths
        if not self._has_group(GAMEMODE_GROUP):
            self.warn("no 'gamemode' group on this system (is the gamemode package installed?)")
            return
        entry = f"{GAMEMODE_GROUP}:"
        if not _line_starts(p.etc_group, entry):
            found = [line for line in (read_text(p.lib_group) or "").split("\n") if line.startswith(entry)]
            if not found:
                raise Halt(
                    1,
                    "cpu-setup.sh: the 'gamemode' group comes from NSS, not /etc/group or /usr/lib/group: "
                    "add its line to /etc/group by hand, then run install again",
                )
            _append(p.etc_group, "\n".join(found) + "\n")
            if not _line_starts(p.etc_gshadow, entry):
                _append(p.etc_gshadow, f"{entry}!::\n")
        if not self.in_gamemode_group():
            self.run(["usermod", "-aG", GAMEMODE_GROUP, self.user])
            self.say(f"added {self.user} to the 'gamemode' group (effective at the next login)")

    # ---- uninstall ---------------------------------------------------------------------------------
    def uninstall(self) -> int:
        """What install added, except the settings, the group, the cst link, builds.slice and the VM."""
        p = self.paths
        # The watchdog first: a tick between the two would start the profile again, just before its
        # unit and its off program are deleted.
        self.run(["systemctl", "disable", "--now", WATCHDOG_TIMER], check=False, quiet=True)
        self.run(["systemctl", "stop", "cpu-game.service"], check=False, quiet=True)
        self.run(["systemctl", "disable", "--now", CPU_POWER_SERVICE], check=False, quiet=True)
        self.run(self._checkout_program("cpu-power", "revert"), check=False, quiet=True)
        self.run(self._checkout_program("cpu-irq", "revert"), check=False, quiet=True)  # cpu:irq, before cpu:core
        # Without the watchdog nothing would remove one left behind (the reload below then drops its MemoryLow).
        self.remove_game_dropins()
        self.installer("remove", "--component", CORE_COMPONENT)
        self.run(["udevadm", "control", "--reload-rules"], check=False, quiet=True)
        self.remove_old_dropins()
        had_hwp = os.path.isfile(p.hwp_tmpfiles)
        # Also when it is gone already: its install record line goes too.
        self.installer("remove-text", p.hwp_tmpfiles)
        if had_hwp and os.access(p.hwp_boost, os.W_OK) and self._echo_to(p.hwp_boost, "0"):
            self.say(f"set:       {p.hwp_boost} = 0")
        had_ntsync = os.path.isfile(p.ntsync_modules_load)
        self.installer("remove-text", p.ntsync_modules_load)
        if had_ntsync:
            self.run(["modprobe", "-r", "ntsync"], check=False, quiet=True)
        self.rmdir(os.path.dirname(p.machine_dropin), os.path.dirname(p.delegate_dropin))
        if self.user:
            dropins = (self.toolbox_dropin(), self.helper_dropin())
            older = self._older_dropins(*dropins)
            all_dropins = (*dropins, *older.remove)
            try:
                self.forget_user_text(*older.forget)
                self.remove_user_text(*all_dropins)
            except Halt:
                # The installer failed (a record of a newer CST, a folder the user cannot write): the record
                # stays as it is, the drop-ins go as before the record, and uninstall goes on to the reloads.
                self.warn(
                    f"WARNING: the install record of {self.user} was not changed (see above); "
                    "removing the drop-ins without it"
                )
                self.rm_v(*all_dropins)
            self.rmdir(*(dict.fromkeys(os.path.dirname(p) for p in all_dropins)))
            if holds(gamemode_ini(self._home_of(self.user)), MANAGED_MARK):
                home = self.required_home(self.user, "cst-install.bash")
                self.installer("remove", "--home", home, "--component", GAMEMODE_COMPONENT, as_user=self.user)
            self.usys("daemon-reload")
        self.run(["systemctl", "daemon-reload"])
        for line in uninstall_kept_lines(self.prog, p.conf):
            self.say(line)
        return 0

    # ---- docker-slice ------------------------------------------------------------------------------
    def docker_slice(self, args: Sequence[str]) -> int:
        """docker-slice install|status|uninstall: Docker containers born in builds.slice."""
        match first_arg(args, DockerSliceAction.STATUS):
            case DockerSliceAction.INSTALL:
                self.installer("install", "--component", DOCKER_SLICE_COMPONENT)
                self.run(["systemctl", "daemon-reload"])
                self.run(["systemctl", "start", BUILDS_SLICE])
                self.installer("mark", "--component", DOCKER_SLICE_COMPONENT, ComponentState.DONE)
                self._set_builds_parent()
                self.say("Restart Docker when convenient (stops running containers): sudo systemctl restart docker")
                return 0
            case DockerSliceAction.UNINSTALL:
                self._drop_builds_parent()
                self.installer("remove", "--component", DOCKER_SLICE_COMPONENT)
                self.run(["systemctl", "daemon-reload"])
                self.say("Restart Docker when convenient for new containers to leave builds.slice.")
                return 0
            case DockerSliceAction.STATUS:
                self.say(self.docker_slice_status())
                return 0
            case _:
                raise Halt(2, "docker-slice: install|status|uninstall")

    def _daemon_json(self, *, missing_ok: bool) -> DaemonJson:
        """daemon.json's object ({} when there is none and missing_ok); Halt(1) when it cannot be read."""
        path = self.paths.daemon_json
        try:
            with open(path, encoding="utf-8") as fh:
                current = parse_json(fh.read())
        except FileNotFoundError:
            if missing_ok:
                return {}
            raise Halt(1, f"{path}: not found") from None
        except (OSError, ValueError) as e:
            raise Halt(1, f"{path}: {e}") from None
        if not is_daemon_json(current):
            raise Halt(1, f"{path}: not a JSON object")
        return current

    def _set_builds_parent(self) -> None:
        path = self.paths.daemon_json
        edit = with_builds_parent(path, self._daemon_json(missing_ok=True))
        if edit.error:
            raise Halt(1, edit.message)
        if edit.content is not None:
            write_atomic(path, json_text(edit.content, newline=True), mode="umask", durable=False, relabel=False)
        self.say(edit.message)

    def _drop_builds_parent(self) -> None:
        path = self.paths.daemon_json
        if not os.path.exists(path):
            return
        current = self._daemon_json(missing_ok=False)
        if current.get("cgroup-parent") != BUILDS_SLICE:
            return
        del current["cgroup-parent"]
        if current:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(json_text(current, newline=False))
        else:
            os.remove(path)
        self.say(f"removed cgroup-parent from {path}")

    def docker_slice_status(self) -> str:
        """builds.slice's state, daemon.json's cgroup-parent and the containers in builds.slice."""
        active = self.run(["systemctl", "is-active", BUILDS_SLICE], check=False, quiet=True, capture=True)
        try:
            with open(self.paths.daemon_json, encoding="utf-8") as fh:
                parent = cgroup_parent_shown(parse_json(fh.read()))
        except OSError, ValueError, TypeError:
            parent = "-"
        try:
            names = os.listdir(self.paths.builds_cgroup)
        except OSError:
            names = []
        containers = len([n for n in names if fnmatch.fnmatchcase(n, "docker-*.scope")])
        return docker_slice_status(active.stdout.rstrip("\n"), parent, containers)

    # ---- experiments --------------------------------------------------------------------------------
    def experiment(self, args: Sequence[str]) -> int:
        """experiment list|on NAME|off NAME: runtime-only memory knobs."""
        env = ExperimentEnv(self.paths, self.prog, self.say, self.warn, self._echo_to, _make_run_dir)
        return run_experiment(env, args)

    # ---- game, vm-pin -----------------------------------------------------------------------------
    def game(self, args: Sequence[str]) -> int:
        """game on|off|state: the game profile by hand."""
        action = first_arg(args, "state")
        if action in ("on", "off"):
            _make_run_dir(self.paths.run)
            # on: stays on until 'game off' (the watchdog leaves it alone); off: until this game ends.
            _touch(os.path.join(self.paths.run, "manual" if action == "on" else "suppress"))
            self.run(["systemctl", "start" if action == "on" else "stop", "cpu-game.service"])
            return self.run(["cpu-game-profile", "state"], check=False).returncode
        if action == "state":
            return self.run(self._checkout_program("game-profile", "state"), check=False).returncode
        raise Halt(2, "game: on|off|state")

    def _checkout_program(self, name: str, *args: str) -> list[str]:
        return ["python3", "-I", "-B", os.path.join(self.src, name), *args]

    def vm_pin(self, args: Sequence[str]) -> int:
        """vm-pin plan|apply|status: the checkout's cpu-vm-pin, in the foreground (apply asks)."""
        return self._passthrough(self._checkout_program("vm-pin", *args))

    # ---- status --------------------------------------------------------------------------------------
    def section(self, title: str) -> None:
        self.say()
        self.say(f"== {title}")

    def _render_perf_diagnosis(self) -> None:
        p = self.paths
        probes = (
            os.path.join(p.proc_dir, "interrupts"),
            os.path.join(p.sys_dir, "devices", "system", "cpu", "cpu0"),
            os.path.join(p.sys_dir, "class", "drm"),
        )
        if not any(os.path.exists(node) for node in probes):
            return
        hr = "/" if p.sys_dir == "/sys" else os.path.dirname(p.sys_dir)
        paths = PerfPaths(
            p.proc_dir,
            p.sys_dir,
            p.cgroot,
            os.path.join(hr, "etc"),
            os.path.join(hr, "run"),
            os.path.join(hr, "dev"),
            os.path.join(p.sys_dir, "module", "zswap", "parameters"),
            p.conf,
            p.preempt,
            p.sched_ext,
        )
        axes = (PerfAxis.CPU, PerfAxis.GPU)
        rep = evaluate_perf_snapshot(LinuxPerfProbe(paths).collect_snapshot(axes), axes)
        buf = io.StringIO()
        render_perf_findings_block([f for ax in rep.axes for f in ax.findings], buf)
        self.section("diagnosis")
        self.write(buf.getvalue())

    def _usys_prop(self, prop: str, unit: str) -> CommandResult:
        return self.usys("show", "-P", prop, unit, capture=True)

    def status(self) -> int:
        """The whole picture; nothing in it stops the rest."""
        p, user = self.paths, self.user
        ext = " + sched_ext!" if read_stripped(p.sched_ext) == SchedExtState.ENABLED else ""
        self.say(
            f"kernel {os.uname().release}, scheduler EEVDF{ext}, preempt {preempt_modes(read_text(p.preempt) or '')}"
        )
        self.section("game profile")
        self.write(
            indented(self.run(self._checkout_program("game-profile", "state"), check=False, capture=True).stdout)
        )
        timer = self.run(["systemctl", "is-active", WATCHDOG_TIMER], check=False, quiet=True, capture=True)
        self.say(f"  watchdog timer: {timer.stdout.rstrip('\n')}")
        self.section("GameMode")
        if user:
            member = "member" if self.in_gamemode_group() else "NOT a member"
            ini = "managed" if holds(gamemode_ini(self._home_of(user)), MANAGED_MARK) else "own/absent"
            gamemoded = self.usys("is-active", "gamemoded.service", capture=True).stdout.rstrip("\n")
            self.say(f"  group: {member}; ini: {ini}; gamemoded: {gamemoded}")
        boost = read_stripped(p.hwp_boost)
        kept = f"kept at boot by {p.hwp_tmpfiles}" if os.path.isfile(p.hwp_tmpfiles) else "not managed"
        self.say(f"  HWP dynamic boost: {'n/a' if boost is None else boost} ({kept})")
        nt_state = "active" if self._ntsync_active() else "inactive"
        nt_kept = f"autoloaded by {p.ntsync_modules_load}" if os.path.isfile(p.ntsync_modules_load) else "not managed"
        self.say(f"  ntsync: {nt_state} ({nt_kept})")
        self.section("baseline weights")
        machine = self.run(["systemctl", "show", "-P", "CPUWeight", "machine.slice"], check=False, capture=True)
        w = [
            self._usys_prop("CPUWeight", u).stdout.rstrip("\n")
            for u in (USER_SLICE, "flatpak-session-helper.service", APP_SLICE, "background.slice")
        ]
        ios = [
            self._usys_prop("IOWeight", u).stdout.rstrip("\n") for u in (USER_SLICE, "flatpak-session-helper.service")
        ]
        self.say(
            f"  machine.slice {machine.stdout.rstrip('\n')}, toolboxes {w[0]} (io {ios[0]}), "
            f"flatpak-session-helper {w[1]} (io {ios[1]}), app.slice {w[2]}, background.slice {w[3]}"
        )
        self.section("cpuset for toolboxes and host commands")
        if user:
            self._cpuset_status()
        self.section("VM")
        vm = self.run(self._checkout_program("vm-pin", "status"), check=False, merge=True)
        self.write(indented(first_four_and_last(vm.stdout)))
        self.section("Docker")
        self.write(indented(self.docker_slice_status() + "\n"))
        self.section("memory experiments")
        self.experiment(["list"])
        self.section("GPU runtime PM and CPU C-states")
        self.write(indented(self.run(self._checkout_program("cpu-power", "status"), check=False, capture=True).stdout))
        self.section("irqbalance")
        self.write(indented(self.run(self._checkout_program("cpu-irq", "status"), check=False, capture=True).stdout))
        self._render_perf_diagnosis()
        self.section("last measurements")
        if not user:
            return 1
        report = [
            "runuser",
            "-u",
            user,
            "--",
            "env",
            f"HOME={self._home_of(user)}",
            *self._checkout_program("cpu-watch", "report"),
        ]
        self.write(indented(last_lines(self.run(report, check=False, quiet=True, capture=True).stdout, 5)))
        return 0

    def _cpuset_status(self) -> None:
        delegated = "yes" if self.cpuset_delegated() else "no (next login)"
        toolboxes = self.usys("show", "-P", "AllowedCPUs", USER_SLICE, capture=True).stdout.rstrip("\n")
        helper = self.usys("show", "-P", "AllowedCPUs", "flatpak-session-helper.service", capture=True).stdout.rstrip(
            "\n"
        )
        self.say(
            f"  cpuset delegated: {delegated}; toolboxes AllowedCPUs={toolboxes}; flatpak-spawn --host AllowedCPUs={helper}"
        )
        ucg = self.user_cg()
        effective = (
            read_stripped(os.path.join(ucg, USER_SLICE, "cpuset.cpus.effective"))
            or read_stripped(os.path.join(ucg, "cpuset.cpus.effective"))
            or ""
        )
        self.say(f"  effective (cgroup): toolboxes [{effective}]")

    # ---- dispatch -------------------------------------------------------------------------------------
    def command(self, cmd: str, args: Sequence[str]) -> int:
        """Run one root command; its exit status (a Stop's message on stderr)."""
        try:
            match cmd:
                case CpuRootCommand.INSTALL:
                    return self.install()
                case CpuRootCommand.UNINSTALL:
                    return self.uninstall()
                case CpuRootCommand.STATUS:
                    return self.status()
                case CpuRootCommand.GAME:
                    return self.game(args)
                case CpuRootCommand.VM_PIN:
                    return self.vm_pin(args)
                case CpuRootCommand.DOCKER_SLICE:
                    return self.docker_slice(args)
                case CpuRootCommand.POWER | CpuRootCommand.IRQ:
                    prog = "cpu-power" if cmd == CpuRootCommand.POWER else "cpu-irq"
                    return self.run(self._checkout_program(prog, *args), check=False).returncode
                case _:
                    return self.experiment(args)
        except Halt as stop:
            if stop.message:
                self.warn(stop.message)
            return stop.code


def _line_starts(path: str, prefix: str) -> bool:
    """grep -q '^PREFIX' PATH (False when it cannot be read)."""
    return any(line.startswith(prefix) for line in (read_text(path) or "").split("\n"))


def _append(path: str, text: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)

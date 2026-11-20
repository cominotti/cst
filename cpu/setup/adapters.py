# SPDX-License-Identifier: MPL-2.0
"""`cst cpu`'s root commands on the machine: install, status, uninstall, game, vm-pin, docker-slice, experiment.

CpuSetup carries what every step needs (the paths, the command runner, the
checkout, the name to show, the target user) across the steps of a command.
Each step runs what cpu-setup.sh ran, the way it ran it: the installer
(cst_core/installer.py, through runuser for the user's files), systemctl,
the checkout's programs. A command that cpu-setup.sh ran under set -e ends
the command when it fails, with its exit status (Halt).
"""

from collections.abc import Callable, Mapping, Sequence
import contextlib
import fnmatch
import glob
import grp
import locale
import os
import pwd
from typing import TextIO

from cst_core.adapters.files import ensure_dir, read_text, write_atomic
from cst_core.adapters.runtime import SYSTEM_PYTHON, lc_ctype_handover
from cst_core.adapters.terminal import stderr, stdout
from cst_core.domain.errors import shell_exit_status
from cst_core.domain.install import INSTALL_MANIFEST, ArtifactKind, select_entries
from cst_core.domain.shapes import parse_json
from cst_core.domain.values import ROOT_ACCOUNT
from cst_core.ports import CommandFault, CommandResult, CommandRunnerPort, OutputMode
from cpu.game.adapters.host import holds, read_stripped
from cpu.game.domain import APP_SLICE, BUILDS_SLICE, GAME_DROPIN, USER_SLICE, cpuset_in
from cpu.setup.domain import (
    CORE_COMPONENT,
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
    SetupPaths,
    conf_lines,
    cgroup_parent_shown,
    current_choice,
    docker_slice_status,
    experiment_line,
    first_arg,
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


def _make_run_dir(path: str) -> None:
    """install -d -m 0755 PATH (the mode also for a folder already there)."""
    ensure_dir(path, 0o755)
    os.chmod(path, 0o755)


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
        """systemctl for the target user's manager."""
        return self.run(
            ["systemctl", "--user", "-M", f"{self.user}@", *args], check=check, quiet=quiet, capture=capture
        )

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

    def required_home(self, user: str, who: str) -> str:
        """The user's home for a step in it (who: the helper that says so), or Halt(1)."""
        home = self._home_of(user)
        if not home:
            raise Halt(1, f"{who}: no home folder for user '{user}'")
        return home

    # ---- small steps ------------------------------------------------------------------------------
    def e_cores(self) -> str:
        """The E-cores (/sys/devices/cpu_atom/cpus), "" on a machine without."""
        return read_stripped(self.paths.e_cores) or ""

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

    def toolbox_dropin(self, name: str = DROPIN_NAME) -> str:
        return toolbox_dropin(self._home_of(self.user), name)

    def helper_dropin(self, name: str = DROPIN_NAME) -> str:
        return helper_dropin(self._home_of(self.user), name)

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
        """cpuset in use by the user manager (next login after install)."""
        return cpuset_in(read_text(os.path.join(self.user_cg(), "cgroup.subtree_control")) or "")

    def check_syntax(self, path: str) -> bool:
        """A program parses (python's ast, or bash -n)."""
        first = (read_text(path) or "").split("\n", 1)[0]
        if "python" in first:
            check = "import ast, sys; ast.parse(open(sys.argv[1]).read())"
            return self.run(["python3", "-I", "-c", check, path], check=False).returncode == 0
        return self.run(["bash", "-n", path], check=False).returncode == 0

    # ---- install -----------------------------------------------------------------------------------
    def install(self) -> int:
        """The game profile, baseline weights, the toolboxes on the E-cores, GameMode, cpu-watch, the cst link."""
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
            self.install_text(p.conf, conf_lines(user))
        self.installer("install", "--set", f"USER={user}", "--component", CORE_COMPONENT)
        if os.access(p.hwp_boost, os.W_OK):
            self.install_text(p.hwp_tmpfiles, hwp_tmpfiles_text(p.hwp_boost), component=CORE_COMPONENT)
            if self._echo_to(p.hwp_boost, "1"):
                self.say(f"set:       {p.hwp_boost} = 1")
        else:
            self.warn(f"NOTE: {p.hwp_boost} not found (no intel_pstate in HWP mode): HWP dynamic boost skipped")
        ecores = self.e_cores()
        if ecores:
            self.install_text(self.toolbox_dropin(), toolbox_dropin_text(ecores), user, TOOLBOX_COMPONENT)
            self.install_text(self.helper_dropin(), helper_dropin_text(ecores), user, TOOLBOX_COMPONENT)
        else:
            self.warn("NOTE: no E-cores found (/sys/devices/cpu_atom/cpus): toolbox CPU restriction skipped")
            self.install_text(self.toolbox_dropin(), toolbox_dropin_text(""), user, TOOLBOX_COMPONENT)
            self.remove_user_text(self.helper_dropin())
        self.remove_old_dropins()  # the daemon-reloads below then read without them
        link = os.path.join(self.root, "cst-link.bash")
        self.run(["bash", "-c", 'set -Eeuo pipefail; source "$1"; link_cst_command "$2"', "cpu-setup.sh", link, user])
        gamemode = self._install_gamemode()
        self._gamemode_group()
        self.run(["systemctl", "daemon-reload"])
        self.usys("daemon-reload")
        self.run(["systemctl", "enable", "--now", WATCHDOG_TIMER])
        self.installer("mark", "--component", CORE_COMPONENT, "done")
        home = self.required_home(user, "cst-install.bash")
        self.installer("mark", "--home", home, "--component", TOOLBOX_COMPONENT, "done", as_user=user)
        if gamemode:
            self.installer("mark", "--home", home, "--component", GAMEMODE_COMPONENT, "done", as_user=user)
        for line in install_done_lines(self.prog):
            self.say(line)
        return 0

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
        # Without the watchdog nothing would remove one left behind (the reload below then drops its MemoryLow).
        self.remove_game_dropins()
        self.installer("remove", "--component", CORE_COMPONENT)
        self.remove_old_dropins()
        had_hwp = os.path.isfile(p.hwp_tmpfiles)
        # Also when it is gone already: its install record line goes too.
        self.installer("remove-text", p.hwp_tmpfiles)
        if had_hwp and os.access(p.hwp_boost, os.W_OK) and self._echo_to(p.hwp_boost, "0"):
            self.say(f"set:       {p.hwp_boost} = 0")
        self.rmdir(os.path.dirname(p.machine_dropin), os.path.dirname(p.delegate_dropin))
        if self.user:
            dropins = (self.toolbox_dropin(), self.helper_dropin())
            try:
                self.remove_user_text(*dropins)
            except Halt:
                # The installer failed (a record of a newer CST, a folder the user cannot write): the record
                # stays as it is, the drop-ins go as before the record, and uninstall goes on to the reloads.
                self.warn(
                    f"WARNING: the install record of {self.user} was not changed (see above); "
                    "removing the drop-ins without it"
                )
                self.rm_v(*dropins)
            self.rmdir(os.path.dirname(self.toolbox_dropin()), os.path.dirname(self.helper_dropin()))
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
                ecores = self.e_cores()
                allowed = f"AllowedCPUs={ecores}" if ecores else ""
                self.installer("install", "--set", f"ALLOWED={allowed}", "--component", DOCKER_SLICE_COMPONENT)
                self.run(["systemctl", "daemon-reload"])
                self.run(["systemctl", "start", BUILDS_SLICE])
                self.installer("mark", "--component", DOCKER_SLICE_COMPONENT, "done")
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
        action = first_arg(args, "list")
        name = args[1] if len(args) > 1 else ""
        if action == "list":
            return self._experiment_list()
        if action not in ("on", "off"):
            raise Halt(2, "experiment: list|on NAME|off NAME")
        if not name:
            raise Halt(1, "cpu-setup.sh: EXPERIMENTS: bad array subscript")
        spec = self.paths.experiments.get(name)
        if spec is None:
            raise Halt(2, f"experiment: unknown '{name}' (see: {self.prog} experiment list)")
        file, value = spec
        saved = self.paths.saved(name)
        if action == "on":
            return self._experiment_on(name, file, value, saved)
        return self._experiment_off(name, file, saved)

    def _experiment_list(self) -> int:
        """experiment list: each knob, its value now, and whether it is on."""
        for exp, (file, value) in self.paths.experiments.items():
            try:
                with open(file, encoding="utf-8", errors="replace") as fh:
                    now = fh.read().replace("\n", "")
            except OSError as e:
                self.warn(f"cpu-setup.sh: {file}: {e.strerror}")
                now = ""
            self.say(experiment_line(exp, file, value, now, on=os.path.exists(self.paths.saved(exp))))
        return 0

    def _experiment_on(self, name: str, file: str, value: str, saved: str) -> int:
        """experiment on NAME: the value it replaces saved (the first time), then VALUE into FILE."""
        _make_run_dir(self.paths.run)
        if not os.path.exists(saved):
            self._save_choice(file, saved)
        if not self._echo_to(file, value):
            raise Halt(1)
        self.say(
            f"{name}: on ({file} = {value}; runtime only, reverted by '{self.prog} experiment off {name}' or a reboot)"
        )
        return 0

    def _experiment_off(self, name: str, file: str, saved: str) -> int:
        """experiment off NAME: the saved value back into FILE."""
        if not os.path.exists(saved):
            self.say(f"{name}: not on")
            return 0
        old = read_text(saved) or ""
        try:
            with open(file, "w", encoding="utf-8") as fh:
                fh.write(old)
        except OSError as e:
            self.warn(f"cpu-setup.sh: {file}: {e.strerror}")
            raise Halt(1, f"{name}: could not restore {old.rstrip('\n')} into {file}") from None
        os.remove(saved)
        self.say(f"{name}: off ({file} restored)")
        return 0

    def _save_choice(self, file: str, saved: str) -> None:
        """The value an experiment replaces, for 'off' (sysfs THP shows "[current]" among the choices).
        Read first: a value that cannot be read saves nothing (off then has nothing to put back)."""
        try:
            with open(file, encoding="utf-8", errors="replace") as fh:
                value = current_choice(fh.read())
        except OSError as e:
            raise Halt(2, f"sed: can't read {file}: {e.strerror}") from None
        write_atomic(saved, value, mode="umask", durable=False, relabel=False)

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

    def status(self) -> int:
        """The whole picture; nothing in it stops the rest."""
        p, user = self.paths, self.user
        ext = " + sched_ext!" if read_stripped(p.sched_ext) == "enabled" else ""
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
        self.section("baseline weights")
        machine = self.run(["systemctl", "show", "-P", "CPUWeight", "machine.slice"], check=False, capture=True)
        weights = [
            self.usys("show", "-P", "CPUWeight", u, capture=True).stdout.rstrip("\n")
            for u in (USER_SLICE, APP_SLICE, "background.slice")
        ]
        self.say(
            f"  machine.slice {machine.stdout.rstrip('\n')}, toolboxes {weights[0]}, app.slice {weights[1]}, background.slice {weights[2]}"
        )
        self.section("E-cores for toolboxes and host commands")
        if user:
            self._ecores_status()
        self.section("VM")
        vm = self.run(self._checkout_program("vm-pin", "status"), check=False, merge=True)
        self.write(indented(first_four_and_last(vm.stdout)))
        self.section("Docker")
        self.write(indented(self.docker_slice_status() + "\n"))
        self.section("memory experiments")
        self.experiment(["list"])
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

    def _ecores_status(self) -> None:
        delegated = "yes" if self.cpuset_delegated() else "no (next login)"
        toolboxes = self.usys("show", "-P", "AllowedCPUs", USER_SLICE, capture=True).stdout.rstrip("\n")
        helper = self.usys("show", "-P", "AllowedCPUs", "flatpak-session-helper.service", capture=True).stdout.rstrip(
            "\n"
        )
        self.say(
            f"  cpuset delegated: {delegated}; toolboxes AllowedCPUs={toolboxes}; flatpak-spawn --host AllowedCPUs={helper}"
        )
        effective = read_stripped(os.path.join(self.user_cg(), USER_SLICE, "cpuset.cpus.effective")) or ""
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

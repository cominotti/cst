# SPDX-License-Identifier: MPL-2.0
"""The fake machine of the `cst bluetooth` (bluetooth-setup.sh) tests in test_setup.py.

Machine builds a scratch HOME, a scratch host root (CST_BT_ROOT, with its
/etc/bluetooth/main.conf) and a folder of stub commands on PATH: systemctl,
sudo, busctl, bluetoothctl, journalctl, pactl, pw-dump, flatpak-spawn,
selinuxenabled, sleep, python3 (which answers only bt-helper's wp-roles
and passes every other call to /usr/bin/python3) and runuser (only logged). The stubs keep their state
in files and log every call, so a test sees what the program ran, in order.
Nothing reaches the real machine: sudo runs its command as the caller (with
the root-only options of cp and install left out) and only inside the
scratch folders, and the unit tests never gain privileges
(tests/run_unit.py).
"""

import ast
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import difflib
from enum import StrEnum
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import FrameType
from typing import NoReturn, TextIO
import unittest
from unittest import mock

from bluetooth.setup import cli as bt_cli, wiring as bt_wiring
from cst_core import installer as cst_installer
from cst_core.adapters.runner import c_locale, on_host_argv
from cst_core.ports import (
    CommandFailed,
    CommandResult,
    ContainerProbePort,
    DecodeErrors,
    OutputMode,
    StdinMode,
)

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SETUP = os.path.join(REPO, "bluetooth-setup.sh")
MAC = "0A:1B:2C:3D:4E:5F"
DEV = "0A_1B_2C_3D_4E_5F"
ISO = "6fbaf188-05e0-496a-9885-d6ddfdb4e03e"
type Answer = tuple[int, str, str]  # a command's exit, stdout and stderr
STUBS = (
    "flatpak-spawn",
    "sudo",
    "systemctl",
    "busctl",
    "bluetoothctl",
    "journalctl",
    "pactl",
    "pw-dump",
    "selinuxenabled",
    "sleep",
    "python3",
    "runuser",
)

FAKE = r"""#!/bin/bash
# The fake machine of bluetooth/tests/test_setup.py: one script for every
# command it stands in for (named by $0). Its state is files in $FAKE_STATE;
# every call but sleep's (Python's own sleep logs none) is logged there (log: the command line; env-log: the command and its
# first four arguments, a tab, then the CST_, LC_ and PYTHON variables it got, to catch a leak).
S="${FAKE_STATE:?}"
me="${0##*/}"
log() { printf '%s\n' "$*" >>"$S/log"; }
if [[ "$me" != sleep && ( "$me" != python3 || "$*" == *wp-roles* ) ]]; then
    log "$me $*"
    { printf '%s\t' "$me ${1:-} ${2:-} ${3:-} ${4:-}" | tr '\n' ' '; env | grep -E '^(CST_|LC_|LANG|PYTHON)' | LC_ALL=C sort | tr '\n' ' '; echo; } >>"$S/env-log"
fi
unit() { # unit SCOPE NAME: the file prefix of a unit's state
    local n="${2%.service}"
    echo "$S/units/$1-$n"
}
active_state() { # active_state PREFIX: the unit's ActiveState (PREFIX.state, else active or inactive)
    if [[ -e "$1.state" ]]; then cat "$1.state"; elif [[ -e "$1.active" ]]; then echo active; else echo inactive; fi
}
new_id() { # new_id PREFIX: a new invocation ID for the unit
    local n
    n=$(( $(cat "$S/counter" 2>/dev/null || echo 0) + 1 ))
    echo "$n" >"$S/counter"
    echo "inv-${1##*/}-$n" >"$1.inv"
}
case "$me" in
    flatpak-spawn)
        [[ "$1" == --host ]] && shift
        while [[ "$1" == --env=* ]]; do export "${1#--env=}"; shift; done  # flatpak-spawn --env=K=V
        if [[ "$1" == /usr/bin/python3 && "$*" == *socket* ]]; then
            [[ -e "$S/iso-socket" ]]
            exit
        fi
        exec "$@"
        ;;
    sudo)
        # sudo-keeps-no-lc-all: a sudoers whose env_reset keeps no LC_ALL (env_keep and env_check
        # without it), so the caller's LANG decides cp's language unless env sets LC_ALL.
        [[ -e "$S/sudo-keeps-no-lc-all" ]] && unset LC_ALL
        if [[ "$1" == env ]]; then  # sudo env K=V... CMD: the variables for CMD, then CMD as below
            shift
            while [[ "$1" == *=* ]]; do export "$1"; shift; done
        fi
        args=()
        skip=0
        for a in "$@"; do
            if (( skip )); then skip=0; continue; fi
            case "$1:$a" in
                install:-o|install:-g) skip=1; continue ;;
                install:-Z|cp:--preserve=context) continue ;;
            esac
            args+=("$a")
        done
        if [[ -e "$S/fail-sudo-$1" ]]; then
            how="$(cat "$S/fail-sudo-$1")"
            [[ "$how" == TERM ]] && kill -TERM $$  # killed by a signal
            [[ "$how" == PARENT-TERM ]] && { kill -TERM "$PPID"; exit 0; }  # the program gets SIGTERM
            [[ "$how" == PARENT-HUP ]] && { kill -HUP "$PPID"; exec "${args[@]}"; }  # SIGHUP, the step done
            exit "$how"
        fi
        if [[ "$1" == cp && " ${args[*]} " == *" --update=none-fail "* ]]; then
            # GNU cp 9.5's --update=none-fail, whatever cp this machine has: a destination that is
            # there (a link too) is left alone, exit 1. race-backup: another run creates the
            # destination just before (once), as cp's O_EXCL open then finds it. In a pt_BR locale
            # (coreutils' catalogue) cp says both refusals in Portuguese.
            pt=0
            [[ "${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}" == pt_BR* ]] && pt=1
            kept=()
            for a in "${args[@]}"; do [[ "$a" == --update=none-fail ]] || kept+=("$a"); done
            args=("${kept[@]}")
            dst="${args[-1]}"
            if [[ -e "$S/race-backup" ]]; then
                rm -f "$S/race-backup"
                echo "another run's backup" >"$dst"
                if (( pt )); then
                    echo "cp: não foi possível criar o arquivo comum '$dst': Arquivo existe" >&2
                else
                    echo "cp: cannot create regular file '$dst': File exists" >&2
                fi
                exit 1
            fi
            if [[ -e "$S/partial-backup" ]]; then  # a full disk: the start of the copy is left behind
                head -c 8 "${args[-2]}" >"$dst"
                echo "cp: error writing '$dst': No space left on device" >&2
                exit 1
            fi
            if [[ -e "$dst" || -L "$dst" ]]; then
                if (( pt )); then echo "cp: não substituindo '$dst'" >&2; else echo "cp: not replacing '$dst'" >&2; fi
                exit 1
            fi
        fi
        exec "${args[@]}"
        ;;
    systemctl)
        scope=system
        if [[ "$1" == --user ]]; then scope=user; shift; fi
        mkdir -p "$S/units"
        case "$1" in
            is-active)
                if [[ "$2" == --quiet ]]; then
                    st="$(active_state "$(unit "$scope" "$3")")"
                    [[ "$st" == active || "$st" == reloading || "$st" == refreshing ]]
                    exit
                fi
                if [[ -e "$(unit "$scope" "$2").active" ]]; then echo active; exit 0; fi
                echo inactive
                exit 3
                ;;
            is-enabled)
                if [[ -e "$(unit "$scope" "$2").enabled" ]]; then echo enabled; exit 0; fi
                echo disabled
                exit 1
                ;;
            show)
                [[ -e "$S/hang-show" ]] && exec /usr/bin/sleep 30  # systemd does not answer
                # term-show-when names a path: a look while it exists sends SIGTERM to the program.
                if [[ -e "$S/term-show-when" && -e "$(cat "$S/term-show-when")" ]]; then kill -TERM "$PPID"; exit 1; fi
                if [[ "$*" == *UnitPath* ]]; then  # the manager's own: unit-path's, else systemd's default
                    [[ -e "$S/no-unit-path" ]] && exit 1
                    if [[ -e "$S/unit-path" ]]; then cat "$S/unit-path"; else echo "$HOME/.config/systemd/user.control $HOME/.config/systemd/user /etc/systemd/user /usr/lib/systemd/user"; fi
                    exit 0
                fi
                u="$(unit "$scope" "${!#}")"
                case "$*" in
                    *ActiveState*)   # systemd prints ActiveState= before InvocationID=
                        echo "ActiveState=$(active_state "$u")"
                        echo "InvocationID=$(cat "$u.inv" 2>/dev/null)"
                        exit 0
                        ;;
                    *InvocationID*) cat "$u.inv" 2>/dev/null; exit 0 ;;
                    *MainPID*) cat "$u.pid" 2>/dev/null || echo 0; exit 0 ;;
                esac
                exit 1
                ;;
            show-environment) [[ ! -e "$S/no-session" ]] || exit 1; [[ ! -e "$S/user-env" ]] || cat "$S/user-env"; exit 0 ;;
            daemon-reload) exit 0 ;;
            restart|start)
                u="$(unit "$scope" "$2")"
                [[ -e "$u.fail" ]] && { echo "Job for $2 failed." >&2; exit 1; }
                if [[ "$1" == restart || ! -e "$u.active" ]]; then new_id "$u"; fi
                touch "$u.active"
                exit 0
                ;;
            enable) touch "$(unit "$scope" "$2").enabled"; echo "Created symlink for $2." >&2; exit 0 ;;
            disable)
                u="$(unit "$scope" "${!#}")"
                rm -f "$u.enabled" "$u.active"
                echo "Removed $u." >&2
                exit 0
                ;;
        esac
        exit 1
        ;;
    busctl)
        case "$*" in
            *GetManagedObjects*)  # fail-objects: its answer, then a failure
                cat "$S/objects.json" 2>/dev/null || exit
                [[ ! -e "$S/fail-objects" ]]
                exit
                ;;
            "--system list --no-legend --unique") cat "$S/bus-names" 2>/dev/null; exit 0 ;;
            get-property*) cat "$S/prop-$4-$5" 2>/dev/null; exit ;;
        esac
        exit 1
        ;;
    bluetoothctl)
        via=direct
        [[ "$(tr '\0' ' ' <"/proc/$PPID/cmdline" 2>/dev/null)" == timeout* ]] && via="$(tr '\0' ' ' <"/proc/$PPID/cmdline")"
        log "  stdin=$(readlink /proc/self/fd/0) via=$via"
        case "$1" in
            info) cat "$S/bt-info" 2>/dev/null || { echo "Device $2 not available"; exit 1; } ;;
            bearer)
                [[ -e "$S/bearer-stuck" ]] && { echo "Failed to set bearer"; exit 1; }
                grep -v PreferredBearer "$S/bt-info" >"$S/bt-info.new" 2>/dev/null
                printf '\tPreferredBearer: %s\n' "$3" >>"$S/bt-info.new"
                mv "$S/bt-info.new" "$S/bt-info"
                echo "Changing PreferredBearer to $3 succeeded"
                ;;
            connect) touch "$S/a2dp-connected" ;;
        esac
        exit 0
        ;;
    journalctl)
        case "$*" in
            *_SYSTEMD_INVOCATION_ID=*) cat "$S/bt-journal" 2>/dev/null; exit 0 ;;
            *--user*) cat "$S/lw-journal" 2>/dev/null; exit 0 ;;
        esac
        exit 1
        ;;
    pactl)
        case "$*" in
            "-f json list cards")
                # After bluetoothctl connect: pactl-cards-connected, from the look after a2dp-delay looks.
                if [[ -e "$S/a2dp-connected" && -e "$S/pactl-cards-connected" ]]; then
                    n="$(cat "$S/a2dp-delay" 2>/dev/null || echo 0)"
                    if (( n > 0 )); then echo "$(( n - 1 ))" >"$S/a2dp-delay"; else cat "$S/pactl-cards-connected"; exit; fi
                fi
                cat "$S/pactl-cards" 2>/dev/null
                exit
                ;;
            "-f json list short sources") cat "$S/pactl-sources" 2>/dev/null || echo "[]"; exit 0 ;;
            "-f json list source-outputs") cat "$S/pactl-outputs" 2>/dev/null || echo "[]"; exit 0 ;;
            set-card-profile*) [[ ! -e "$S/no-profile-switch" ]]; exit ;;
        esac
        exit 1
        ;;
    pw-dump) cat "$S/pw-dump.json" 2>/dev/null; exit ;;
    selinuxenabled) [[ -e "$S/selinux" ]]; exit ;;
    sleep) exit 0 ;;
    runuser) exit 0 ;; # the run as SUDO_USER: its command line is in the log
    python3)
        if [[ "$*" == *wp-roles* ]]; then
            log "  XDG_CONFIG_HOME=${XDG_CONFIG_HOME:-}"
            [[ -e "$S/wp-roles" ]] || exit 5
            cat "$S/wp-roles"
            exit 0
        fi
        exec /usr/bin/python3 "$@"
        ;;
esac
echo "fake: no such command: $me" >&2
exit 127
"""

# busctl -j GetManagedObjects with one adapter.
OBJECTS = {
    "type": "a{oa{sa{sv}}}",
    "data": [{"/org/bluez": {}, "/org/bluez/hci0": {"org.bluez.Adapter1": {}, "org.bluez.Media1": {}}}],
}


class SubprocessMode(StrEnum):
    """How Machine.run_subprocess spawns bluetooth-setup.sh for process-boundary tests."""

    PLAIN = "plain"
    CLOSED_STDOUT = "closed-stdout"
    IGNORE_HUP = "ignore-hup"
    ROOT = "root"


@dataclass(frozen=True)
class Run:
    """One run of the program: its exit status, output and the calls the stubs logged."""

    returncode: int
    stdout: str
    stderr: str
    calls: list[str]

    @property
    def text(self) -> str:
        """stdout and stderr, for assertion messages."""
        return self.stdout + self.stderr


class Machine:
    """A scratch machine for one test (removed at its end)."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.tmp = tempfile.mkdtemp(prefix="bt-setup-test-")
        test.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        self.root = os.path.join(self.tmp, "root")
        self.state = os.path.join(self.tmp, "state")
        self.bin = os.path.join(self.tmp, "bin")
        for d in (self.home, self.state, self.bin, os.path.join(self.root, "etc", "bluetooth")):
            os.makedirs(d)
        fake = os.path.join(self.bin, "fake")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE)
        os.chmod(fake, 0o755)
        for name in STUBS:
            os.symlink("fake", os.path.join(self.bin, name))
        self.main_conf = os.path.join(self.root, "etc", "bluetooth", "main.conf")
        self.wp_dir = os.path.join(self.home, ".config", "wireplumber", "wireplumber.conf.d")
        self.wp_conf = os.path.join(self.wp_dir, f"50-bluez-{DEV}.conf")
        self.le_conf = os.path.join(self.wp_dir, "90-cst-le-audio-off.conf")
        self.settings = os.path.join(self.home, ".config", "cst", "bluetooth.conf")
        self.pending = os.path.join(self.home, ".local", "state", "cst", "bluetooth-restart-pending.json")
        self.lw_prog = os.path.join(self.home, ".local", "lib", "cst", "bt-ldac-watch")
        self.lw_unit = os.path.join(self.home, ".config", "systemd", "user", "cst-bt-ldac48.service")
        self.record = os.path.join(self.home, ".local", "state", "cst", "installed.json")

    # ---- the machine's state -------------------------------------------------------------
    def put(self, path: str, text: str, mode: int | None = None) -> str:
        """Write text at path (an absolute path, or one relative to the state folder)."""
        full = os.path.join(self.state, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        if mode is not None:
            os.chmod(full, mode)
        return full

    def read(self, path: str) -> str:
        """The text at path (as put)."""
        with open(os.path.join(self.state, path), encoding="utf-8") as f:
            return f.read()

    def flag(self, name: str, *, on: bool = True) -> None:
        """A state flag of the stubs (a file in the state folder) set or cleared."""
        path = os.path.join(self.state, name)
        if on:
            self.put(name, "")
        elif os.path.exists(path):
            os.unlink(path)

    def unit(
        self, scope: str, name: str, *, active: bool = True, inv: str | None = None, pid: int | None = None
    ) -> None:
        """A unit of the fake systemd (scope system or user): running with invocation ID inv."""
        base = os.path.join("units", f"{scope}-{name}")
        self.flag(base + ".active", on=active)
        if inv is not None:
            self.put(base + ".inv", inv + "\n")
        if pid is not None:
            self.put(base + ".pid", f"{pid}\n")

    def unit_active_state(self, scope: str, name: str, state: str) -> None:
        """The ActiveState word the fake systemctl gives for a unit (without it: active or inactive)."""
        self.put(os.path.join("units", f"{scope}-{name}.state"), state + "\n")

    def unit_inv(self, scope: str, name: str) -> str:
        """The invocation ID the fake systemd has for a unit now."""
        return self.read(os.path.join("units", f"{scope}-{name}.inv")).strip()

    def has(self, name: str) -> bool:
        """A state file of the stubs exists."""
        return os.path.exists(os.path.join(self.state, name))

    def services(self) -> None:
        """bluetoothd and the session's WirePlumber running (with their bus name), the adapter powered."""
        self.unit("system", "bluetooth", inv="bt-0")
        self.unit("user", "wireplumber", inv="wp-0", pid=4242)
        self.put("bus-names", ":1.7 4242 wireplumber danilo :1.7 user@1000.service - -\n")
        # The adapter answers right after a restart (apply waits up to 15 s for it).
        self.put("prop-org.bluez.Adapter1-Powered", "b true\n")

    def adapter(self, *, iso: bool = True, bap: bool = True, listed: bool = True) -> None:
        """One Bluetooth adapter in org.bluez: its ISO feature on and BAP's PACs (as wanted).
        listed=False: only its properties answer (GetManagedObjects lists no adapter), so a run
        on the host never reaches the kernel ISO socket check, which opens a real Bluetooth
        socket with /usr/bin/python3 (no stub can stand in for it there; run() refuses)."""
        if listed:
            self.put("objects.json", json.dumps(OBJECTS))
        self.put("prop-org.bluez.Adapter1-ExperimentalFeatures", f'as 1 "{ISO}"\n' if iso else "as 0\n")
        self.put(
            "prop-org.bluez.Media1-SupportedUUIDs",
            'as 2 "00002bc9-0000-1000-8000-00805f9b34fb" "00002bcb-0000-1000-8000-00805f9b34fb"\n' if bap else "as 0\n",
        )
        self.put("prop-org.bluez.Adapter1-Powered", "b true\n")

    def user_settings(self, text: str) -> None:
        """~/.config/cst/bluetooth.conf."""
        self.put(self.settings, text)

    # ---- running it ----------------------------------------------------------------------
    def env(self, *, container: bool, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """The environment of a run: the stubs first on PATH, the scratch HOME and host root."""
        env: dict[str, str] = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": self.home,
            "FAKE_STATE": self.state,
            "CST_BT_CONTAINER": "1" if container else "0",
            "CST_BT_ROOT": self.root,
        }
        env.update(extra or {})
        return env

    def run(
        self,
        *args: str,
        container: bool = False,
        env: Mapping[str, str] | None = None,
        stdin_text: str | None = None,
    ) -> Run:
        """bluetooth-setup.sh ARGS on this machine in-process via cli.main and SetupModel."""
        if not container and self.has("objects.json"):
            raise AssertionError("an adapter listed on the host: the checks would open a real Bluetooth socket")
        return self._run_in_process(args, container=container, env=env, stdin_text=stdin_text)

    def run_subprocess(
        self,
        *args: str,
        container: bool = False,
        env: Mapping[str, str] | None = None,
        mode: SubprocessMode = SubprocessMode.PLAIN,
    ) -> Run:
        """bluetooth-setup.sh ARGS as a real subprocess (for shim, signal, closed-stdout, and root tests)."""
        if not container and self.has("objects.json"):
            raise AssertionError("an adapter listed on the host: the checks would open a real Bluetooth socket")
        log = os.path.join(self.state, "log")
        if os.path.exists(log):
            os.unlink(log)
        read_end, write_end = os.pipe()
        os.close(read_end)
        closed_stdout = mode == SubprocessMode.CLOSED_STDOUT
        start = ["bash", "-c", 'trap "" HUP; exec bash "$0" "$@"'] if mode == SubprocessMode.IGNORE_HUP else ["bash"]
        r = subprocess.run(
            [*(["/usr/bin/unshare", "-r"] if mode == SubprocessMode.ROOT else []), *start, SETUP, *args],
            check=False,
            stdout=write_end if closed_stdout else subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env(container=container, extra=env),
            stdin=subprocess.DEVNULL,
            timeout=120,
        )
        os.close(write_end)
        calls: list[str] = []
        if os.path.exists(log):
            with open(log, encoding="utf-8") as f:
                calls = f.read().splitlines()
        return Run(r.returncode, r.stdout or "", r.stderr, calls)

    def _run_in_process(
        self,
        args: Sequence[str],
        *,
        container: bool,
        env: Mapping[str, str] | None,
        stdin_text: str | None,
    ) -> Run:
        run_env = self.env(container=container, extra=env)
        run_env["CST_ARG0"] = SETUP
        lc_ctype = run_env.get("LC_CTYPE")
        run_env["CST_LC_CTYPE"] = "" if lc_ctype is None else f"={lc_ctype}"
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        in_buf = io.StringIO(stdin_text or "")
        model = SetupModel(self, lang=run_env.get("LANG", ""), stdout=out_buf, stderr=err_buf)
        saved_signals = _save_signals()
        entry = os.path.join(REPO, "bluetooth", "bt-setup")
        current_args = list(args)
        rc = 1
        try:
            with (
                mock.patch.dict(os.environ, run_env, clear=True),
                mock.patch.object(sys, "stdin", in_buf),
                mock.patch.object(sys, "stdout", out_buf),
                mock.patch.object(sys, "stderr", err_buf),
                mock.patch.object(
                    bt_wiring,
                    "SubprocessCommandRunner",
                    side_effect=lambda ctx: _InProcessCommandRunner(model, ctx),
                ),
                mock.patch.object(bt_wiring, "SystemClockAdapter", _FastClock),
                mock.patch.object(bt_wiring, "replace", side_effect=_raise_rerun),
            ):
                while True:
                    try:
                        rc = bt_cli.main(entry, current_args)
                        break
                    except _ReRunExec as exc:
                        os.environ["CST_ARG0"] = exc.argv[0]
                        cur_lc = os.environ.get("LC_CTYPE")
                        os.environ["CST_LC_CTYPE"] = "" if cur_lc is None else f"={cur_lc}"
                        current_args = list(exc.argv[1:])
        finally:
            _restore_signals(saved_signals)
            stdout_text = out_buf.getvalue()
            stderr_text = err_buf.getvalue()
            in_buf.close()
            out_buf.close()
            err_buf.close()
        return Run(rc, stdout_text, stderr_text, model.log_lines)

    def env_log(self) -> str:
        """What the stubs saw of the CST_, LC_, LANG and PYTHON variables, one line per call."""
        path = os.path.join(self.state, "env-log")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as f:
            return f.read()


# ---- an in-process twin of FAKE ----------------------------------------------------------

BLUETOOTH_SERVICE = "bluetooth.service"
WIREPLUMBER_SERVICE = "wireplumber.service"
_DIFF_STAMP = "1970-01-01 00:00:00.000000000 +0000"
_PATCHED_SIGNALS = (signal.SIGHUP, signal.SIGINT, signal.SIGPIPE, signal.SIGXFSZ, signal.SIGTERM)
type _Disposition = Callable[[int, FrameType | None], object] | int | signal.Handlers | None


def _save_signals() -> list[tuple[int, _Disposition]]:
    return [(sig, signal.getsignal(sig)) for sig in _PATCHED_SIGNALS]


def _restore_signals(saved: Sequence[tuple[int, _Disposition]]) -> None:
    for sig, disp in saved:
        if disp is not None and signal.getsignal(sig) != disp:
            signal.signal(sig, disp)


class _ReRunExec(BaseException):
    """Raised by the mocked wiring.replace when cli.main re-execs itself."""

    def __init__(self, argv: Sequence[str]) -> None:
        super().__init__(argv)
        self.argv = tuple(argv)


def _raise_rerun(argv: Sequence[str]) -> NoReturn:
    raise _ReRunExec(argv)


class _FastClock:
    """ClockPort for in-process Machine.run: real timestamps, zero-cost sleeps."""

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, _seconds: float) -> None:
        return None


class _InProcessCommandRunner:
    """CommandRunnerPort for Machine.run: wraps host commands and merges env, then runs SetupModel."""

    def __init__(self, model: SetupModel, ctx: ContainerProbePort) -> None:
        self._model = model
        self._ctx = ctx

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
        env: Mapping[str, str] | None = None,
        unset: Sequence[str] = (),
        base_env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        locale_c: bool = False,
        new_session: bool = False,
        on_host: bool = False,
        stdin: StdinMode = "devnull",
        output: OutputMode = OutputMode.CAPTURE,
        decode_errors: DecodeErrors = "replace",
        nice: int | None = None,
        pass_fds: Sequence[int] = (),
        on_line: Callable[[str], None] | None = None,
    ) -> CommandResult:
        del timeout, cwd, new_session, decode_errors, nice, pass_fds, on_line
        if input_text is not None and stdin != "devnull":
            raise ValueError("input_text is the command's stdin: stdin must stay 'devnull'")
        hosted = on_host and self._ctx.is_container()
        if hosted and unset:
            raise ValueError("unset cannot reach a command run on the host from a container")
        cmd = on_host_argv(argv, {**(c_locale() if locale_c else {}), **(env or {})}) if hosted else list(argv)
        child_env = dict(os.environ if base_env is None else base_env)
        if locale_c:
            child_env["LC_ALL"] = "C"
        if env is not None:
            child_env.update(env)
        for name in unset:
            child_env.pop(name, None)
        res = self._model.run(
            cmd,
            locale_c=locale_c,
            child_env=child_env,
            stdin=stdin,
            input_text=input_text,
            output=output,
        )
        if check and res.returncode != 0:
            raise CommandFailed(cmd, res.returncode, res.stdout, res.stderr)
        return res


def _diff_texts(old_label: str, old_lines: Sequence[str], new_label: str, new_lines: Sequence[str]) -> Answer:
    hunks = "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            old_label,
            new_label,
            fromfiledate=_DIFF_STAMP,
            tofiledate=_DIFF_STAMP,
        )
    )
    return (1 if hunks else 0), hunks, ""


def _unified_diff(old: str, new: str) -> Answer:
    """diff -u OLD NEW as the real diff answers it: 0 for the same text, 1 and the hunks otherwise."""
    with open(old, encoding="utf-8") as f:
        a = f.read().splitlines(keepends=True)
    with open(new, encoding="utf-8") as f:
        b = f.read().splitlines(keepends=True)
    return _diff_texts(old, a, new, b)


def _text_or_none(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            return f.read()
    except OSError:
        return None


class SetupModel:
    """FAKE in process, for the commands apply and revert run (a CommandRunnerPort over the same
    state files of a Machine), with what each service read at its last (re)start: bluetoothd
    main.conf, WirePlumber the *.conf files of the user's wireplumber.conf.d. The fault tests run many
    applies; FAKE costs a fork of bash per command. A command it does not know raises
    AssertionError, so a new one shows up; test_setup_units' parity test keeps it in step with FAKE.

    restarts lists each restart that worked: (unit, whether it was needed then: the unit read other
    files than are on disk, or, for WirePlumber, bluetoothd restarted after it)."""

    def __init__(
        self,
        m: Machine,
        *,
        lang: str = "",
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        """lang: the LANG the commands get (FAKE reads it from its environment)."""
        self.m = m
        self.lang = lang
        self.locale_c = False  # the call being answered asked for the C locale (runner's locale_c)
        self.calls: list[list[str]] = []
        self.log_lines: list[str] = []
        self.restarts: list[tuple[str, bool]] = []
        self.loaded: dict[str, object] = {
            BLUETOOTH_SERVICE: self.disk(BLUETOOTH_SERVICE),
            WIREPLUMBER_SERVICE: self.disk(WIREPLUMBER_SERVICE),
        }
        self.started = {BLUETOOTH_SERVICE: 0, WIREPLUMBER_SERVICE: 0}  # the order of the last starts
        self.clock = 0
        self._stdout = stdout
        self._stderr = stderr
        self._cmd_env: dict[str, str] = {}
        self._stdin_mode: StdinMode = "devnull"
        self._input_text: str | None = None
        self._via = "direct"

    # ---- what the services read -----------------------------------------------------------
    def disk(self, unit: str) -> object:
        """What the unit would read if it started now."""
        if unit == BLUETOOTH_SERVICE:
            return _text_or_none(self.m.main_conf)
        names = (
            sorted(n for n in os.listdir(self.m.wp_dir) if n.endswith(".conf")) if os.path.isdir(self.m.wp_dir) else []
        )
        return tuple((n, _text_or_none(os.path.join(self.m.wp_dir, n))) for n in names)

    def in_step(self, unit: str) -> bool:
        """The unit runs with what it would read now (WirePlumber: and started after bluetoothd)."""
        if self.loaded[unit] != self.disk(unit):
            return False
        return unit != WIREPLUMBER_SERVICE or self.started[WIREPLUMBER_SERVICE] >= self.started[BLUETOOTH_SERVICE]

    def inv(self, unit: str) -> str:
        scope = "system" if unit == BLUETOOTH_SERVICE else "user"
        return (_text_or_none(self.prefix(scope, unit) + ".inv") or "").strip()

    # ---- the state files, as FAKE keeps them --------------------------------------------------
    def state(self, name: str) -> str:
        return os.path.join(self.m.state, name)

    def has(self, name: str) -> bool:
        return os.path.exists(self.state(name))

    def cat(self, name: str) -> tuple[int, str]:
        text = _text_or_none(self.state(name))
        return (1, "") if text is None else (0, text)

    def prefix(self, scope: str, name: str) -> str:
        return self.state(os.path.join("units", f"{scope}-{name.removesuffix('.service')}"))

    def active_state(self, prefix: str) -> str:
        text = _text_or_none(prefix + ".state")
        if text is not None:
            return text.strip()
        return "active" if os.path.exists(prefix + ".active") else "inactive"

    def new_id(self, prefix: str) -> None:
        n = int((_text_or_none(self.state("counter")) or "0").strip()) + 1
        with open(self.state("counter"), "w", encoding="utf-8") as f:
            f.write(f"{n}\n")
        with open(prefix + ".inv", "w", encoding="utf-8") as f:
            f.write(f"inv-{os.path.basename(prefix)}-{n}\n")

    def log(self, line: str) -> None:
        self.log_lines.append(line)

    # ---- the commands -----------------------------------------------------------------------
    def run(self, argv: Sequence[str], **_kw: object) -> CommandResult:
        """CommandRunnerPort.run: each command FAKE answers that apply and revert run."""
        args = list(argv)
        self.calls.append(args)
        self.locale_c = _kw.get("locale_c") is True
        raw_env = _kw.get("child_env")
        self._cmd_env = dict(raw_env) if isinstance(raw_env, Mapping) else ({"LANG": self.lang} if self.lang else {})
        raw_stdin = _kw.get("stdin", "devnull")
        self._stdin_mode = "inherit" if raw_stdin == "inherit" else "devnull"
        raw_input = _kw.get("input_text")
        self._input_text = raw_input if isinstance(raw_input, str) else None
        self._via = "direct"
        rc, out, err = self.answer(args)
        output = _kw.get("output", OutputMode.CAPTURE)
        mode = output if isinstance(output, OutputMode) else OutputMode.CAPTURE
        return self._format_result(args, rc, out, err, mode)

    def _format_result(self, args: list[str], rc: int, out: str, err: str, mode: OutputMode) -> CommandResult:
        if mode == OutputMode.MERGE:
            return CommandResult(rc, out + err, "", argv=tuple(args))
        if mode == OutputMode.STDOUT:
            return CommandResult(rc, out, "", argv=tuple(args))
        if mode == OutputMode.STDOUT_SHOW_STDERR:
            if self._stderr is not None and err:
                self._stderr.write(err)
            return CommandResult(rc, out, "", argv=tuple(args))
        if mode == OutputMode.INHERIT:
            if self._stdout is not None and out:
                self._stdout.write(out)
            if self._stderr is not None and err:
                self._stderr.write(err)
            return CommandResult(rc, "", "", argv=tuple(args))
        return CommandResult(rc, out, err, argv=tuple(args))

    def answer(self, args: list[str]) -> Answer:
        handler = self.handlers().get(args[0] if args else "")
        found = None if handler is None else handler(args)
        if found is None:
            raise AssertionError(f"SetupModel: no such command: {args}")
        return found

    def handlers(self) -> dict[str, Callable[[list[str]], Answer | None]]:
        """Each command by its first word; a handler answers None for a form FAKE does not know."""
        return {
            "flatpak-spawn": lambda a: self.flatpak_spawn(a[1:]),
            "timeout": lambda a: self.timeout_cmd(a[1:]),
            "env": lambda a: self.env_cmd(a[1:]),
            "mktemp": lambda _a: self.mktemp(),
            "sudo": lambda a: self.logged_sudo(a[1:]),
            "systemctl": lambda a: self.logged_systemctl(a[1:]),
            "busctl": lambda a: self.logged_busctl(a[1:]),
            "bluetoothctl": lambda a: self.bluetoothctl(a[1:]),
            "journalctl": self.logged_journalctl,
            "pactl": lambda a: self.pactl(a[1:]),
            "pw-dump": self.pw_dump,
            "selinuxenabled": self.selinuxenabled,
            "diff": lambda a: _unified_diff(a[-2], a[-1]),
            "bash": lambda a: self._bash_diff(a[1:]),
            "python3": lambda a: self.python3(a[1:]),
            "/usr/bin/python3": lambda a: self.system_python(a[1:]),
        }

    def flatpak_spawn(self, args: list[str]) -> Answer | None:
        self.log(f"flatpak-spawn {' '.join(args)}")
        rest = list(args)
        if rest[:1] == ["--host"]:
            rest.pop(0)
        while rest and rest[0].startswith("--env="):
            key, _, value = rest.pop(0).removeprefix("--env=").partition("=")
            self._cmd_env[key] = value
        if rest[:1] == ["/usr/bin/python3"] and "socket" in " ".join(rest):
            return (0 if self.has("iso-socket") else 1), "", ""
        return self.answer(rest) if rest else None

    def timeout_cmd(self, args: list[str]) -> Answer | None:
        if len(args) < 2:
            return None
        rest = args[1:]
        self._via = f"timeout {args[0]} {' '.join(rest)} "
        return self.answer(rest)

    def env_cmd(self, args: list[str]) -> Answer | None:
        rest = list(args)
        while rest and "=" in rest[0]:
            key, _, value = rest.pop(0).partition("=")
            self._cmd_env[key] = value
        return self.answer(rest) if rest else None

    def mktemp(self) -> Answer:
        tmpdir = self._cmd_env.get("TMPDIR") or self.m.tmp
        if not os.path.isdir(tmpdir):
            return (
                1,
                "",
                f"mktemp: failed to create file via template '{tmpdir}/tmp.XXXXXXXXXX': No such file or directory\n",
            )
        fd, path = tempfile.mkstemp(prefix="tmp.", dir=tmpdir)
        os.close(fd)
        return 0, path + "\n", ""

    def _bash_diff(self, args: list[str]) -> Answer | None:
        if len(args) >= 4 and args[0] == "-c" and args[1] == 'diff -u "$1" <(cat)':
            old_path = args[3]
            with open(old_path, encoding="utf-8") as f:
                a = f.read().splitlines(keepends=True)
            b = (self._input_text or "").splitlines(keepends=True)
            return _diff_texts(old_path, a, "/dev/fd/63", b)
        return None

    def python3(self, args: list[str]) -> Answer | None:
        if "wp-roles" in args:
            self.log(f"python3 {' '.join(args)}")
            self.log(f"  XDG_CONFIG_HOME={self._cmd_env.get('XDG_CONFIG_HOME', '')}")
            return self.wp_roles()
        if len(args) >= 4 and args[:2] == ["-I", "-c"] and "ast.parse" in args[2]:
            try:
                with open(args[3], encoding="utf-8") as f:
                    ast.parse(f.read(), filename=args[3])
            except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                return 1, "", f"{exc}\n"
            return 0, "", ""
        return None

    def system_python(self, args: list[str]) -> Answer | None:
        if "socket" in " ".join(args):
            return (0 if self.has("iso-socket") else 1), "", ""
        if len(args) >= 3 and args[:2] == ["-I", "-B"] and args[2].endswith("cst_core/installer.py"):
            return self._run_installer(args[3:])
        return None

    def _run_installer(self, args: list[str]) -> Answer:
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        raw_in = (self._input_text or "").encode("utf-8")
        in_buf = io.TextIOWrapper(io.BytesIO(raw_in), encoding="utf-8")
        saved_signals = _save_signals()
        try:
            with (
                mock.patch.dict(os.environ, self._cmd_env, clear=True),
                mock.patch.object(sys, "stdin", in_buf),
                mock.patch.object(sys, "stdout", out_buf),
                mock.patch.object(sys, "stderr", err_buf),
            ):
                try:
                    rc = cst_installer.main(args)
                except SystemExit as exc:
                    rc = exc.code if isinstance(exc.code, int) else 1
        finally:
            _restore_signals(saved_signals)
            stdout_text = out_buf.getvalue()
            stderr_text = err_buf.getvalue()
            in_buf.close()
            out_buf.close()
            err_buf.close()
        return rc, stdout_text, stderr_text

    def logged_journalctl(self, args: list[str]) -> Answer | None:
        self.log(" ".join(args))
        return self.journalctl(args)

    def journalctl(self, args: list[str]) -> Answer | None:
        if any(a.startswith("_SYSTEMD_INVOCATION_ID=") for a in args):
            return 0, self.cat("bt-journal")[1], ""
        if "--user" in args:
            return 0, self.cat("lw-journal")[1], ""
        return None

    def pw_dump(self, args: list[str]) -> Answer:
        self.log(" ".join(args))
        return (*self.cat("pw-dump.json"), "")

    def selinuxenabled(self, args: list[str]) -> Answer:
        self.log(" ".join(args))
        return (0 if self.has("selinux") else 1), "", ""

    def wp_roles(self) -> Answer:
        rc, text = self.cat("wp-roles")
        return (0, text, "") if rc == 0 else (5, "", "")

    def sudo_env(self, args: list[str]) -> tuple[str, list[str]]:
        """(LC_ALL as the command sudo runs gets it: env's, else the runner's when sudoers keeps it; that
        command, without env and its KEY=VALUE words)."""
        lc_all = "" if self.has("sudo-keeps-no-lc-all") or not self.locale_c else "C"
        if args[:1] == ["env"]:
            args = args[1:]
            while args and "=" in args[0]:
                key, value = args.pop(0).split("=", 1)
                lc_all = value if key == "LC_ALL" else lc_all
        return lc_all, args

    def logged_sudo(self, args: list[str]) -> Answer:
        self.log(f"sudo {' '.join(args)}")
        return self.sudo(args)

    def sudo(self, args: list[str]) -> Answer:
        lc_all, args = self.sudo_env(args)
        how = _text_or_none(self.state(f"fail-sudo-{args[0]}"))
        if how is not None:
            if not how.strip().isdigit():
                raise AssertionError(f"SetupModel: fail-sudo-{args[0]} {how.strip()} is FAKE's alone")
            return int(how), "", ""
        lang = self._cmd_env.get("LANG", self.lang)
        if args[0] == "cp" and "--update=none-fail" in args:
            return self.cp_none_fail(
                [a for a in args[1:] if a != "--update=none-fail"], pt=(lc_all or lang).startswith("pt_BR")
            )
        kept: list[str] = []
        skip = False
        for a in args[1:]:
            if skip:
                skip = False
                continue
            if args[0] == "install" and a in ("-o", "-g"):
                skip = True
                continue
            if (args[0], a) in (("install", "-Z"), ("cp", "--preserve=context")):
                continue
            kept.append(a)
        match args[0], kept:
            case "cp", ["-p", src, dst]:
                shutil.copy2(src, dst)
            case "install", ["-m", mode, src, dst]:
                shutil.copyfile(src, dst)
                os.chmod(dst, int(mode, 8))
            case "mv", ["-f", src, dst]:
                os.replace(src, dst)
            case "systemctl", ["restart", "bluetooth"]:
                return self.logged_systemctl(["restart", "bluetooth"])
            case _:
                raise AssertionError(f"SetupModel: no such sudo command: {args}")
        return 0, "", ""

    def cp_none_fail(self, args: list[str], *, pt: bool) -> Answer:
        """sudo cp -p [--preserve=context] --update=none-fail SRC DST, as FAKE answers it (pt: in a
        pt_BR locale, its two refusals in Portuguese)."""
        dst = args[-1]
        if self.has("race-backup"):
            os.unlink(self.state("race-backup"))
            with open(dst, "w", encoding="utf-8") as f:
                f.write("another run's backup\n")
            if pt:
                return 1, "", f"cp: não foi possível criar o arquivo comum '{dst}': Arquivo existe\n"
            return 1, "", f"cp: cannot create regular file '{dst}': File exists\n"
        if self.has("partial-backup"):
            with open(args[-2], encoding="utf-8") as src, open(dst, "w", encoding="utf-8") as f:
                f.write(src.read(8))
            return 1, "", f"cp: error writing '{dst}': No space left on device\n"
        if os.path.lexists(dst):
            return 1, "", (f"cp: não substituindo '{dst}'\n" if pt else f"cp: not replacing '{dst}'\n")
        match [a for a in args if a != "--preserve=context"]:
            case ["-p", src, _]:
                shutil.copy2(src, dst)
            case _:
                raise AssertionError(f"SetupModel: no such sudo cp: {args}")
        return 0, "", ""

    def logged_systemctl(self, args: list[str]) -> Answer:
        self.log(f"systemctl {' '.join(args)}")
        return self.systemctl(args)

    def systemctl(self, args: list[str]) -> Answer:
        scope = "system"
        if args[:1] == ["--user"]:
            scope, args = "user", args[1:]
        os.makedirs(self.state("units"), exist_ok=True)
        match args[0]:
            case "is-active":
                result = self.is_active(scope, args)
            case "is-enabled":
                enabled = os.path.exists(self.prefix(scope, args[1]) + ".enabled")
                result = (0, "enabled\n", "") if enabled else (1, "disabled\n", "")
            case "show":
                result = self.show(scope, args)
            case "show-environment":
                result = (1, "", "") if self.has("no-session") else (0, self.cat("user-env")[1], "")
            case "daemon-reload":
                result = (0, "", "")
            case "restart" | "start":
                result = self.start_unit(scope, args)
            case "enable":
                with open(self.prefix(scope, args[1]) + ".enabled", "w", encoding="utf-8"):
                    pass
                result = (0, "", f"Created symlink for {args[1]}.\n")
            case "disable":
                u = self.prefix(scope, args[-1])
                for suffix in (".enabled", ".active"):
                    path = u + suffix
                    if os.path.exists(path):
                        os.unlink(path)
                result = (0, "", f"Removed {u}.\n")
            case _:
                raise AssertionError(f"SetupModel: no such systemctl command: {args}")
        return result

    def is_active(self, scope: str, args: list[str]) -> Answer:
        if args[1] == "--quiet":
            running = self.active_state(self.prefix(scope, args[2])) in ("active", "reloading", "refreshing")
            return (0 if running else 3), "", ""
        active = os.path.exists(self.prefix(scope, args[1]) + ".active")
        return (0, "active\n", "") if active else (3, "inactive\n", "")

    def show(self, scope: str, args: list[str]) -> Answer:
        if self.has("term-show-when"):
            raise AssertionError("SetupModel: term-show-when is FAKE's alone")
        joined = " ".join(args)
        if "UnitPath" in joined:  # the manager's own, as FAKE answers it
            if self.has("no-unit-path"):
                return 1, "", ""
            home = os.path.join(self.m.home, ".config", "systemd")
            default = f"{home}/user.control {home}/user /etc/systemd/user /usr/lib/systemd/user\n"
            return 0, _text_or_none(self.state("unit-path")) or default, ""
        u = self.prefix(scope, args[-1])
        if "ActiveState" in joined:
            inv = (_text_or_none(u + ".inv") or "").strip()
            return 0, f"ActiveState={self.active_state(u)}\nInvocationID={inv}\n", ""
        if "InvocationID" in joined:
            return 0, _text_or_none(u + ".inv") or "", ""
        if "MainPID" in joined:
            return 0, _text_or_none(u + ".pid") or "0\n", ""
        return 1, "", ""

    def start_unit(self, scope: str, args: list[str]) -> Answer:
        u = self.prefix(scope, args[1])
        if os.path.exists(u + ".fail"):
            return 1, "", f"Job for {args[1]} failed.\n"
        if args[0] == "restart" or not os.path.exists(u + ".active"):
            self.new_id(u)
        with open(u + ".active", "w", encoding="utf-8"):
            pass
        self.started_now(f"{args[1].removesuffix('.service')}.service")
        return 0, "", ""

    def started_now(self, unit: str) -> None:
        if unit not in self.loaded:
            return
        self.restarts.append((unit, not self.in_step(unit)))
        self.clock += 1
        self.loaded[unit] = self.disk(unit)
        self.started[unit] = self.clock

    def logged_busctl(self, args: list[str]) -> Answer:
        self.log(f"busctl {' '.join(args)}")
        return self.busctl(args)

    def busctl(self, args: list[str]) -> Answer:
        joined = " ".join(args)
        if "GetManagedObjects" in joined:
            rc, text = self.cat("objects.json")
            return (1 if rc == 0 and self.has("fail-objects") else rc), text, ""
        if joined == "--system list --no-legend --unique":
            return 0, self.cat("bus-names")[1], ""
        if args[:1] == ["get-property"]:
            return (*self.cat(f"prop-{args[3]}-{args[4]}"), "")
        raise AssertionError(f"SetupModel: no such busctl command: {args}")

    def bluetoothctl(self, args: list[str]) -> Answer:
        self.log(f"bluetoothctl {' '.join(args)}")
        stdin_name = "/dev/null" if self._stdin_mode == "devnull" and self._input_text is None else "pipe"
        self.log(f"  stdin={stdin_name} via={self._via}")
        match args[:1]:
            case ["info"]:
                rc, text = self.cat("bt-info")
                return (0, text, "") if rc == 0 else (1, f"Device {args[1]} not available\n", "")
            case ["bearer"]:
                if self.has("bearer-stuck"):
                    return 1, "Failed to set bearer\n", ""
                _, existing = self.cat("bt-info")
                kept = [ln for ln in existing.splitlines(keepends=True) if "PreferredBearer" not in ln]
                kept.append(f"\tPreferredBearer: {args[2]}\n")
                self.m.put("bt-info", "".join(kept))
                return 0, f"Changing PreferredBearer to {args[2]} succeeded\n", ""
            case ["connect"]:
                self.m.flag("a2dp-connected")
                return 0, "", ""
            case _:
                raise AssertionError(f"SetupModel: no such bluetoothctl command: {args}")

    def pactl(self, args: list[str]) -> Answer:
        self.log(f"pactl {' '.join(args)}")
        match args:
            case ["-f", "json", "list", "cards"]:
                if self.has("a2dp-connected") and self.has("pactl-cards-connected"):
                    delay = int((_text_or_none(self.state("a2dp-delay")) or "0").strip())
                    if delay > 0:
                        self.m.put("a2dp-delay", f"{delay - 1}\n")
                    else:
                        return 0, self.cat("pactl-cards-connected")[1], ""
                return (*self.cat("pactl-cards"), "")
            case ["-f", "json", "list", "short", "sources"]:
                rc, text = self.cat("pactl-sources")
                return 0, (text if rc == 0 else "[]\n"), ""
            case ["-f", "json", "list", "source-outputs"]:
                rc, text = self.cat("pactl-outputs")
                return 0, (text if rc == 0 else "[]\n"), ""
            case ["set-card-profile", *_]:
                return (1 if self.has("no-profile-switch") else 0), "", ""
            case _:
                raise AssertionError(f"SetupModel: no such pactl command: {args}")

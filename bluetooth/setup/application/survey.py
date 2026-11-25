# SPDX-License-Identifier: MPL-2.0
"""What `cst bluetooth plan` and `status` read and print, and the checks apply ends with.

Survey holds one run's view of the machine (bluetooth-setup.sh's compute_plan,
le_plan and lw_plan), read again where a change makes it stale, and the
counters of the checks. It reads through the ports only; the commands it
runs, their arguments and where their output goes are bluetooth-setup.sh's.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
import re
from typing import Literal

from cst_core.domain.shapes import parse_json
from bluetooth.domain import (
    BLUETOOTH_UNIT,
    ISO_UUID,
    LDAC_UNIT,
    MARKER,
    WIREPLUMBER_UNIT,
    WholeFileState,
    adapter_path,
    card_state_of,
    current_values,
    edit_general,
    lw_failure,
    render_le_off,
    render_unit,
    render_wp,
    satisfied,
    set_keys,
    settings_report,
    unit_state,
    wp_state,
)
from bluetooth.ports import BluetoothHostPort
from bluetooth.setup.domain import (
    BT_RESTART_MAIN_CONF,
    BT_RESTART_OWED,
    BapState,
    BearerState,
    BluetoothCommand,
    Config,
    Exit,
    LDAC_WATCHER_LABEL,
    LE_AUDIO_LABEL,
    LeState,
    Options,
    Paths,
    OLD_WP_MARKER,
    OWED_NOTE,
    REFUSED_CONFLICT_NOTE,
    WpState,
    bap_state,
    bearer_matches,
    is_running,
    item,
    no_headset_note,
    read_settings_output,
    rule_device,
    senders_of,
    home_tilde,
    wp_label,
    wp_note,
    watcher_prog_state,
    wp_word,
)
from bluetooth.setup.ports import SetupConsolePort, SetupFilesPort
from cst_core.domain.errors import Refused, shell_exit_status
from cst_core.domain.files import OwnedState
from cst_core.domain.units import ActiveState, EnablementState
from cst_core.ports import ClockPort, CommandResult, CommandRunnerPort, OutputMode, SystemdPort, UnitName


def read_bluetooth_settings(
    host: BluetoothHostPort, paths: Paths
) -> tuple[dict[str, str], tuple[str, ...], str] | Exit:
    """(the merged settings, the keys the user's file sets, what to print on stderr). As
    bluetooth-setup.sh's two bt-helper calls (settings, then settings-keys), each names a file
    it cannot read: the first leaves the settings empty, the second (the user's file) ends the
    run with exit 1."""
    files: list[tuple[str, str]] = []
    user: str | None = None
    try:
        for path in (paths.default_settings, paths.user_settings):
            text = host.read_or_none(path)
            if text is not None:
                files.append((path, text))
        user = next((t for p, t in files if p == paths.user_settings), "")
        shown, said = settings_report(files)
    except (OSError, ValueError) as e:
        shown, said = "", f"bt-helper: {e}\n"
    if user is None:  # not read yet: settings-keys read it again
        try:
            user = host.read_or_none(paths.user_settings) or ""
        except (OSError, ValueError) as e:
            return Exit(1, err=f"{said}bt-helper: {e}\n")
    return read_settings_output(shown), tuple(set_keys(user)), said


def is_cst_rule(files: SetupFilesPort, path: str) -> bool:
    """The LDAC rule at path carries the marker of CST or of apply-le-audio.sh (its writers)."""
    return files.contains(path, MARKER, quiet=True) or files.contains(path, OLD_WP_MARKER, quiet=True)


def rule_headsets(files: SetupFilesPort, wp_dir: str) -> list[str]:
    """The headsets of the LDAC rules CST (or apply-le-audio.sh) wrote in wp_dir: its
    50-bluez-<ID>.conf files with one of their markers. Another program's file of that name, a .bak
    copy and a name that is not an address do not count."""
    try:
        names = files.list_dir(wp_dir)
    except OSError:
        return []
    found: list[str] = []
    for name in names:
        device, path = rule_device(name), f"{wp_dir}/{name}"
        if device is None or not files.is_file(path):
            continue
        if is_cst_rule(files, path):
            found.append(device)
    return found


class StepFailed(Exception):
    """A command that must succeed failed (set -e): the run stops with its exit status."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


@dataclass(frozen=True, slots=True)
class SetupMachine:
    """The ports of one run."""

    runner: CommandRunnerPort
    systemd: SystemdPort  # its show only: ActiveState and InvocationID (unit_show)
    user_systemd_host: SystemdPort  # user manager queries on the host (survey)
    user_systemd: SystemdPort  # user manager changes/checks locally (change)
    clock: ClockPort
    console: SetupConsolePort
    files: SetupFilesPort
    host: BluetoothHostPort
    container: bool
    bt_timeout: int
    lc_ctype: str  # what CST_LC_CTYPE hands the installer ("" or "=VALUE")
    c_locale: tuple[str, ...]  # env's words for the C locale (cst_core's c_locale): a command sudo runs
    python: str  # the system Python the installer runs on


class Survey:
    """One run of plan, status or apply: what is on the machine and what apply would change."""

    def __init__(self, opts: Options, cfg: Config, paths: Paths, m: SetupMachine) -> None:
        self.o = opts
        self.p = paths
        self.m = m
        # The headset: --device, DEVICE in the settings, else (the user's file has no DEVICE line) the
        # one LDAC rule CST wrote. CST set a default headset until 2026-11, so a machine set up with it
        # has that rule and no DEVICE line: its headset is used, and apply (or revert) saves it.
        self.device_save = False
        self.device_rules: tuple[str, ...] = ()
        if not cfg.device and "DEVICE" not in cfg.user_keys:
            rules = rule_headsets(m.files, paths.wp_dir)
            if len(rules) == 1:
                cfg = replace(
                    cfg,
                    device=rules[0],
                    device_from=f"from CST's LDAC rule; apply saves it as DEVICE in {self.tilde(paths.user_settings)}",
                )
                self.device_save = True
            elif len(rules) > 1:
                self.device_rules = tuple(rules)
        self.c = cfg
        self.headset = bool(cfg.device)  # none: its parts are skipped and say how to set one
        self.wp_conf = paths.wp_conf(cfg.device_id) if self.headset else ""
        self.le_on_hint = f"{cfg.prog} le-audio on"
        self.le_off_hint = f"{cfg.prog} le-audio off"
        self.tmp = ""  # main.conf as apply would write it (a mktemp file)
        self.backup = ""
        self.fails = 0
        self.warns = 0
        self.mc_changed = False
        self.wp_state: WpState | Literal[""] = ""  # "": not read yet
        self.legacy = False
        self.le_state: LeState | Literal[""] = ""
        self.le_change = False
        self.bearer_state = BearerState.NONE
        self.lw_want = False
        self.lw_unit_st: OwnedState | Literal[""] = ""
        self.lw_prog_st: OwnedState | Literal[""] = ""
        self.lw_enabled = ""
        self.lw_active = ""
        self.lw_change = False
        self.lw_fail = ("", "", "")
        self.owed = ""
        self.bt_owed = False
        self.wp_owed = False
        self.bt_restart = False
        self.wp_restart = False
        self.owing: set[str] = set()  # the units whose restart this run's writes need so far (Change.owe)
        self.owed_warned = False  # the run said it could not record the restarts still owed
        # (user, unit) -> (ActiveState, InvocationID), see unit_show: a system unit and a user unit
        # of the same name are two units.
        self.unit_states: dict[tuple[bool, str], tuple[str, str]] = {}

    # ---- running commands ------------------------------------------------------------------
    def out(self, text: str) -> None:
        self.m.console.out(text)

    def err(self, text: str) -> None:
        self.m.console.err(text)

    def run(
        self,
        argv: Sequence[str],
        *,
        host: bool = False,
        output: OutputMode = OutputMode.CAPTURE,
        input_text: str | None = None,
    ) -> CommandResult:
        """argv (host: on the host from a toolbox, as hostcmd); stdin is /dev/null unless input_text."""
        return self.m.runner.run(argv, on_host=host and self.m.container, output=output, input_text=input_text)

    def quiet(self, argv: Sequence[str], *, host: bool = False) -> str:
        """The stdout of argv, stderr dropped, whatever its exit status ($(CMD 2>/dev/null || true))."""
        return self.run(argv, host=host, output=OutputMode.STDOUT).stdout.rstrip("\n")

    def ok(self, argv: Sequence[str], *, host: bool = False) -> bool:
        """argv succeeded (its output dropped)."""
        return self.run(argv, host=host).returncode == 0

    def must(self, argv: Sequence[str], *, host: bool = False, env: dict[str, str] | None = None) -> None:
        """argv with our stdout and stderr; a failure stops the run (set -e)."""
        r = self.m.runner.run(argv, on_host=host and self.m.container, output=OutputMode.INHERIT, env=env)
        if r.returncode != 0:
            raise StepFailed(shell_exit_status(r.returncode))

    def installer(
        self, *args: str, output: OutputMode = OutputMode.INHERIT, input_text: str | None = None
    ) -> CommandResult:
        """cst_core/installer.py as this user (cst-install.bash's cst_installer)."""
        return self.m.runner.run(
            [self.m.python, "-I", "-B", self.p.installer, *args],
            env={"CST_LC_CTYPE": self.m.lc_ctype},
            output=output,
            input_text=input_text,
        )

    def need_tmp(self) -> str:
        """The scratch file for main.conf (mktemp), made once."""
        if not self.tmp:
            r = self.run(["mktemp"])
            if r.returncode != 0:
                self.err(r.stderr)
                raise StepFailed(shell_exit_status(r.returncode))
            self.tmp = r.stdout.rstrip("\n")
        return self.tmp

    def cleanup(self) -> None:
        if self.tmp:
            self.m.files.remove_file(self.tmp)

    def die(self, message: str) -> Refused:
        """bluetooth-setup.sh's die: "PROG: ERROR: MESSAGE" on stderr, exit status 1."""
        return Refused(message)

    # ---- output ----------------------------------------------------------------------------
    def item(self, word: str, label: str, note: str = "") -> None:
        self.out(item(word, label, note))

    def section(self, title: str) -> None:
        self.out(f"\n== {title}\n")

    def check_ok(self, label: str, note: str = "") -> None:
        self.item("ok", label, note)

    def check_bad(self, label: str, note: str = "") -> None:
        self.item("BAD", label, note)
        self.fails += 1

    def check_warn(self, label: str, note: str = "") -> None:
        self.item("warn", label, note)
        self.warns += 1

    def tilde(self, path: str) -> str:
        return home_tilde(path, self.p.home)

    def restart_hint(self) -> str:
        return "sudo systemctl restart bluetooth && systemctl --user restart wireplumber"

    def user_sets(self, key: str) -> bool:
        return key in self.c.user_keys

    def no_headset(self) -> str:
        return no_headset_note(self.device_rules, self.tilde(self.p.user_settings))

    def wp_title(self) -> str:
        """The rule as the WirePlumber section of apply and revert names it."""
        return self.wp_conf or "no LDAC rule (no headset)"

    # ---- the restarts still owed -----------------------------------------------------------
    def unit_show(self, unit: str, *, user: bool, fresh: bool = False) -> tuple[str, str]:
        """(ActiveState, InvocationID) of the unit from one `systemctl show` (cst_core's: C locale, 20 s
        at most, on the host from a toolbox; '' for what it cannot tell), read once per run and kept
        until CST restarts the unit (forget_unit); fresh: read it again (and keep that)."""
        key = (user, unit)
        if fresh or key not in self.unit_states:
            shown = self.m.systemd.show(UnitName(unit), ("ActiveState", "InvocationID"), user=user)
            self.unit_states[key] = (shown.get("ActiveState", ""), shown.get("InvocationID", ""))
        return self.unit_states[key]

    def forget_unit(self, *units: str) -> None:
        """The units were restarted: the next unit_show of each reads it again (in either scope)."""
        for unit in units:
            for user in (False, True):
                self.unit_states.pop((user, unit), None)

    def unit_invocation(self, unit: str, *, user: bool) -> str:
        """The unit's InvocationID while it runs (as `systemctl is-active` sees it), else ''."""
        active, inv = self.unit_show(unit, user=user)
        return inv if is_running(active) else ""

    def restart_state(self, mode: str, *units: str) -> str | None:
        """The units whose restart is still owed, one per line (save also records them); None on failure."""
        ids = (
            self.unit_invocation(BLUETOOTH_UNIT, user=False),
            self.unit_invocation(WIREPLUMBER_UNIT, user=True),
        )
        try:
            return self.m.host.restart_pending(self.p.pending, ids, units, save=mode == "save")
        except (OSError, ValueError) as e:
            self.err(f"bt-helper: {e}\n")
            return None

    # ---- facts -----------------------------------------------------------------------------
    def le_off_active(self) -> bool:
        """The LE Audio drop-in of CST is in place."""
        return self.m.files.is_file(self.p.le_conf) and self.m.files.contains(self.p.le_conf, MARKER, quiet=False)

    def wait_for(self, seconds: int, probe: Callable[[], bool]) -> bool:
        """probe() true within seconds, asked every half second."""
        for _ in range(seconds * 2):
            if probe():
                return True
            self.m.clock.sleep(0.5)
        return False

    def adapter(self) -> tuple[str, bool]:
        """(the first object with org.bluez.Adapter1 or '', whether busctl and the parse both succeeded)."""
        r = self.run(
            [
                "busctl",
                "--no-pager",
                "-j",
                "call",
                "org.bluez",
                "/",
                "org.freedesktop.DBus.ObjectManager",
                "GetManagedObjects",
            ],
            host=True,
            output=OutputMode.STDOUT,
        )
        try:
            path = adapter_path(parse_json(r.stdout))
        except ValueError, TypeError, AttributeError, KeyError, IndexError:
            path = None
        return path or "", r.returncode == 0 and path is not None

    def bap_endpoint_state(self, *, fresh: bool = False) -> BapState:
        """registered | unregistered | no-record | unknown (bluetooth-setup.sh's bap_endpoint_state).
        bluetoothd's invocation, running or not (a stopped unit keeps its last one), as unit_show
        has it; fresh: read again (the wait after a restart)."""
        inv = self.unit_show(BLUETOOTH_UNIT, user=False, fresh=fresh)[1]
        u = self.m.user_systemd_host
        wp_pid = u.show_value(UnitName("wireplumber"), "MainPID", output=OutputMode.STDOUT).stdout.rstrip("\n")
        if not inv or not re.fullmatch(r"[1-9][0-9]*", wp_pid):
            return BapState.UNKNOWN
        senders = senders_of(self.quiet(["busctl", "--system", "list", "--no-legend", "--unique"], host=True), wp_pid)
        if not senders:
            return BapState.UNKNOWN
        journal = self.run(
            ["journalctl", f"_SYSTEMD_INVOCATION_ID={inv}", "--no-pager", "-o", "cat"],
            host=True,
            output=OutputMode.STDOUT,
        ).stdout
        return bap_state(journal, senders)

    def card_state(self) -> tuple[str, str, str] | None:
        """(PROFILE, A2DP_AVAILABLE 0/1, MIC_IN_USE 0/1) of the headset in PipeWire, or None."""
        if not self.m.files.which("pactl"):
            return None
        parts: list[str] = []
        for what in (["list", "cards"], ["list", "short", "sources"], ["list", "source-outputs"]):
            r = self.run(["pactl", "-f", "json", *what], output=OutputMode.STDOUT)
            if r.returncode != 0:
                return None
            parts.append(r.stdout.rstrip("\n"))
        try:
            st = card_state_of(self.c.device_id, parse_json(f"[{parts[0]},{parts[1]},{parts[2]}]"))
        except ValueError, TypeError, AttributeError, KeyError, IndexError:
            return None
        if st is None:
            return None
        return st[0], str(int(st[1])), str(int(st[2]))

    def bt_info(self) -> str:
        """bluetoothctl info DEVICE, stopped after BT_TIMEOUT seconds (0: no timeout), stdin /dev/null."""
        cmd = ["bluetoothctl", "info", self.c.device]
        if self.m.bt_timeout > 0:
            cmd = ["timeout", str(self.m.bt_timeout), *cmd]
        return self.quiet(cmd, host=True)

    def file_state(self, path: str, rendered: str, *, unit: bool = False) -> WholeFileState:
        """bt-helper's state of a file apply writes: a link judged by what it points to; what is
        there but not a readable plain file is a conflict. A file it cannot decode stops the
        run (STATE="$(file_state ...)" under set -e)."""
        f = self.m.files
        if f.exists(path) and not (f.is_file(path) and f.is_readable(path)):
            return OwnedState.CONFLICT
        try:
            text = self.m.host.read_or_none(path)
        except (OSError, ValueError) as e:
            self.err(f"bt-helper: {e}\n")
            raise StepFailed(1) from e
        return unit_state(text, rendered) if unit else wp_state(text, rendered)

    # ---- what apply would do ---------------------------------------------------------------
    def compute_plan(self) -> None:
        """main.conf, the rule, the drop-in, the bearer, the watcher and the restarts they need."""
        f, mc = self.m.files, self.p.main_conf
        if not f.is_file(mc):
            raise self.die(f"{mc} does not exist (is the bluez package installed?)")
        if f.is_symlink(mc):
            raise self.die(f"{mc} is a symlink: refusing to edit it")
        tmp = self.need_tmp()
        try:
            new, changed = edit_general(self.m.host.read(mc))
        except (OSError, ValueError) as e:
            self.err(f"bt-helper: {e}\n")
            raise self.die(f"could not process {mc} (rc=1)") from e
        f.write_user_file(tmp, new, 0o600)
        self.mc_changed = changed
        # No headset: no rule.
        self.wp_state = (
            WpState(self.file_state(self.wp_conf, render_wp(self.c.device, self.c.quality)))
            if self.headset
            else WpState.NONE
        )
        # The rule's oldest name is renamed to the headset's rule; with no headset it is left alone.
        self.legacy = self.headset and f.is_file(self.p.legacy_wp_conf) and self.p.legacy_wp_conf != self.wp_conf
        self.le_plan()
        self.bearer_state = self._bearer_state()
        self.lw_plan()
        self.read_owed()
        self.bt_restart = self.mc_changed or self.o.force or self.bt_owed
        # A bluetoothd restart takes WirePlumber's endpoints with it; a comment-only renewal restarts nothing.
        self.wp_restart = (
            self.bt_restart
            or self.wp_owed
            or self.legacy
            or self.le_change
            or self.wp_state in (WpState.MISSING, WpState.CHANGE)
        )

    def read_owed(self) -> None:
        """The restarts the record owes while the services run with the IDs it names: owed (the units,
        one per line), bt_owed and wp_owed."""
        text = self.restart_state("show") or ""
        owed = text.split("\n")
        self.bt_owed = BLUETOOTH_UNIT in owed
        self.wp_owed = WIREPLUMBER_UNIT in owed
        self.owed = text.rstrip("\n")

    def _bearer_state(self) -> BearerState:
        """What apply does with the headset's preferred bearer (BEARER)."""
        if not self.c.bearer:
            return BearerState.NONE
        if not self.headset:
            return BearerState.NOHEADSET
        if not self.m.files.which("bluetoothctl") and not self.m.container:
            return BearerState.UNKNOWN
        info = self.bt_info()
        if "Paired: yes" not in info:
            return BearerState.UNPAIRED
        return BearerState.OK if bearer_matches(info, self.c.bearer) else BearerState.SET

    def le_plan(self) -> None:
        """The LE Audio drop-in: its state, and whether apply writes or removes it."""
        f, le = self.m.files, self.p.le_conf
        self.le_change = False
        if self.c.le_audio == "off":
            self.le_state = LeState(self.file_state(le, render_le_off()))
            self.le_change = self.le_state in (LeState.MISSING, LeState.CHANGE)
        elif not f.exists(le):
            self.le_state = LeState.ABSENT
        elif f.contains(le, MARKER, quiet=True):
            self.le_state = LeState.REMOVE
            self.le_change = True
        else:
            self.le_state = LeState.CONFLICT

    def lw_plan(self) -> None:
        """The LDAC rate watcher: its files, whether it is enabled and active, whether apply changes it."""
        f = self.m.files
        self.lw_want = bool(self.c.ldac_rate) and self.headset  # the watcher is for one headset
        self.lw_unit_st = self.file_state(
            self.p.lw_unit_file, render_unit(self.c.device, self.c.ldac_rate or "48000"), unit=True
        )
        self.lw_prog_st = self._lw_prog_state() if f.exists(self.p.lw_prog) else OwnedState.MISSING
        u = self.m.user_systemd_host
        self.lw_enabled = u.is_enabled(UnitName(LDAC_UNIT), output=OutputMode.STDOUT).stdout.rstrip("\n")
        self.lw_active = u.is_active(UnitName(LDAC_UNIT), output=OutputMode.STDOUT).stdout.rstrip("\n")
        if self.lw_want:
            want = (OwnedState.OK, OwnedState.OK, "enabled", ActiveState.ACTIVE)
            done = (self.lw_unit_st, self.lw_prog_st, self.lw_enabled, self.lw_active) == want
            self.lw_change = not done
        else:
            self.lw_change = self.lw_ours_present()

    def _lw_prog_state(self) -> OwnedState:
        """The watcher's program by the installer's plan (watcher_prog_state; a failed plan's error on stderr)."""
        r = self.installer(
            "plan", "--home", self.p.home, "bluetooth/bt-ldac-watch", output=OutputMode.STDOUT_SHOW_STDERR
        )
        said, prog = (r.stdout if r.returncode == 0 else ""), self.p.lw_prog
        return watcher_prog_state(said, prog, lambda: self.m.files.contains(prog, MARKER, quiet=False))

    def lw_off_why(self) -> str:
        """Why apply keeps the watcher off: LDAC_RATE is empty, or no headset is set for it."""
        rate = self.c.ldac_rate
        return f"no headset is set (LDAC_RATE={rate} needs one)" if rate else "LDAC_RATE is empty"

    def lw_ours_present(self) -> bool:
        prog_ours = (OwnedState.OK, OwnedState.ADOPT, OwnedState.CHANGE)
        return self.lw_unit_st in (OwnedState.OK, OwnedState.CHANGE) or self.lw_prog_st in prog_ours

    def lw_unit_label(self) -> str:
        return f"systemd/user/{LDAC_UNIT}"

    def lw_prog_label(self) -> str:
        return self.tilde(self.p.lw_prog)

    # ---- the files part of plan and status ---------------------------------------------------
    def files_report(self) -> None:
        self.section("files")
        try:
            values = current_values(self.m.host.read(self.p.main_conf))
        except (OSError, ValueError) as e:
            self.err(f"bt-helper: {e}\n")
            values = {}
        for key, val in values.items():
            shown = "(not set)" if val is None else val
            label = f"main.conf [General] {key}"
            if val is not None and satisfied(key, val):
                self.item("ok", label, shown)
            elif key == "KernelExperimental":
                self.item("update", label, f"{shown} -> {ISO_UUID} (ISO socket)")
            else:
                self.item("update", label, f"{shown} -> true")
        if self.device_save:
            self.item(
                "set",
                self.tilde(self.p.user_settings),
                f"DEVICE={self.c.device}: the headset of CST's LDAC rule (CST no longer ships a default headset)",
            )
        if self.headset:
            self.item(
                wp_word(self.wp_state), wp_label(self.wp_conf), wp_note(self.wp_state, self.c.quality, self.c.device)
            )
        else:
            self.item("skip", "the headset's LDAC rule", self.no_headset())
        if self.legacy:
            self.item("remove", wp_label(self.p.legacy_wp_conf), "older name of the same rule (apply-le-audio.sh)")
        self.le_report()
        self.le_others_report()
        b, dev = self.c.bearer, self.c.device
        notes = {
            BearerState.NONE: ("ok", "left alone (no BEARER set)"),
            BearerState.OK: ("ok", f"already {b}"),
            BearerState.SET: ("set", f"-> {b} (only for connections the PC starts)"),
            BearerState.UNPAIRED: ("skip", f"{dev} is not paired: apply leaves it"),
            BearerState.NOHEADSET: ("skip", f"BEARER={b} is for a headset, and none is set: apply leaves it"),
            BearerState.UNKNOWN: ("?", "bluetoothctl not found"),
        }
        word, note = notes[self.bearer_state]
        self.item(word, "preferred bearer", note)
        if b == "le" and self.c.le_audio == "off":
            msg = "BEARER=le while LE Audio is off: connections the PC starts get no audio profile; use bredr"
            self.item("warn", "preferred bearer", msg)
        self.lw_report()

    def le_report(self) -> None:
        label, frm = wp_label(self.p.le_conf), self.c.le_from
        rows = {
            ("off", LeState.OK): ("ok", label, f"LE Audio off ({frm}): PipeWire offers only A2DP and HFP"),
            ("off", LeState.ADOPT): (
                "adopt",
                label,
                "LE Audio off; older comments: apply only renews them, no restart",
            ),
            ("off", LeState.CHANGE): (
                "update",
                label,
                "differs (apply rewrites it: LE Audio off; restarts WirePlumber)",
            ),
            ("off", LeState.MISSING): ("write", label, f"LE Audio off ({frm}): PipeWire will offer only A2DP and HFP"),
            ("off", LeState.CONFLICT): ("CONFLICT", label, REFUSED_CONFLICT_NOTE),
            ("on", LeState.ABSENT): ("ok", LE_AUDIO_LABEL, f"on ({frm}): PipeWire offers the bap-* profiles too"),
            ("on", LeState.REMOVE): (
                "remove",
                label,
                f"LE Audio on ({frm}): apply removes it and restarts WirePlumber",
            ),
            ("on", LeState.CONFLICT): ("CONFLICT", label, "not written by CST: left alone (it may keep LE Audio off)"),
        }
        row = rows.get((self.c.le_audio, self.le_state))
        if row is not None:
            self.item(*row)

    def le_others_report(self) -> None:
        if self.c.le_audio != "off":
            return
        try:
            others = self.m.host.other_fragments(self.p.le_conf.rsplit("/", 1)[-1], self.p.fragment_dirs)
        except (OSError, ValueError) as e:
            self.err(f"bt-helper: {e}\n")
            others = []
        for where, path in others:
            if not path:
                continue
            if where == "before":
                msg = "also sets monitor.bluez.properties: while LE Audio is off the CST file replaces that section, so its settings there do not apply"
                self.item("warn", self.tilde(path), msg)
            else:
                msg = "sets monitor.bluez.properties after the CST file: it can bring the LE Audio roles back (status checks them)"
                self.item("warn", self.tilde(path), msg)

    def lw_report(self) -> None:
        svc = f"{LDAC_UNIT} (your session)"
        rate = self.c.ldac_rate
        if not self.lw_want:
            self._lw_off_report(rate)
            return
        if self.user_sets("LDAC_RATE"):
            why = f"on because {self.tilde(self.p.user_settings)} sets LDAC_RATE={rate} (CST default: off)"
            self.item("note", LDAC_WATCHER_LABEL, why)
            hint = f"CST keeps it off until it is checked again on PipeWire 1.6.11; to turn it off: remove LDAC_RATE from that file, then {self.c.prog} apply"
            self.item("note", "", hint)
        if self.o.cmd == BluetoothCommand.PLAN and self.lw_last_failure():
            self.item("warn", LDAC_WATCHER_LABEL, self.lw_failure_text())
        unit_rows = {
            OwnedState.OK: ("ok", f"up to date (LDAC to {rate} Hz)"),
            OwnedState.CHANGE: ("update", "differs (apply rewrites it)"),
            OwnedState.MISSING: ("write", f"the LDAC rate watcher (LDAC_RATE={rate})"),
            OwnedState.CONFLICT: ("CONFLICT", REFUSED_CONFLICT_NOTE),
        }
        prog_rows = {
            OwnedState.OK: ("ok", "same as bt-ldac-watch in CST"),
            OwnedState.ADOPT: ("adopt", "same as bt-ldac-watch in CST; apply adds it to the install record"),
            OwnedState.CHANGE: ("update", "older copy (apply installs the one in CST)"),
            OwnedState.MISSING: ("write", "copy of bluetooth/bt-ldac-watch"),
            OwnedState.CONFLICT: ("CONFLICT", REFUSED_CONFLICT_NOTE),
        }
        for rows, state, label in (
            (unit_rows, self.lw_unit_st, self.lw_unit_label()),
            (prog_rows, self.lw_prog_st, self.lw_prog_label()),
        ):
            if (row := rows.get(state)) is not None:
                self.item(row[0], label, row[1])
        if self.lw_enabled != EnablementState.ENABLED or self.lw_active != ActiveState.ACTIVE:
            why = (
                f"now {self.lw_enabled or 'not installed'}, {self.lw_active or 'inactive'}: apply enables and starts it"
            )
            self.item("enable", svc, why)
        elif self.lw_unit_st == OwnedState.OK and self.lw_prog_st in (OwnedState.OK, OwnedState.ADOPT):
            self.item("ok", svc, "enabled, active")
        else:
            self.item("restart", svc, "apply restarts it with the new files")

    def _lw_off_report(self, rate: str) -> None:
        """The watcher's rows when apply keeps it off."""
        why = self.lw_off_why()
        if self.lw_ours_present():
            self.item("remove", LDAC_WATCHER_LABEL, f"{why}: apply stops and removes {LDAC_UNIT} and its program")
        elif rate:
            self.item("skip", LDAC_WATCHER_LABEL, f"off: {why}")
        else:
            self.item("ok", LDAC_WATCHER_LABEL, "off (LDAC_RATE empty; 48000 turns it on)")
        if self.lw_unit_st == OwnedState.CONFLICT:
            self.item("CONFLICT", self.lw_unit_label(), "not written by CST: left alone")
        if self.lw_prog_st == OwnedState.CONFLICT:
            self.item("CONFLICT", self.lw_prog_label(), "not written by CST: left alone")

    def lw_last_failure(self) -> bool:
        """The watcher's last outcome in its log was a failure (sets lw_fail)."""
        log = self.run(
            ["journalctl", "--user", "-u", LDAC_UNIT, "-o", "short-iso", "--no-pager", "-n", "2000"],
            host=True,
            output=OutputMode.STDOUT,
        ).stdout
        failure = lw_failure(log)
        if failure is None:
            return False
        when, reason, detail = (x.replace("|", "/") for x in failure)  # bt-helper's TIME|REASON|DETAIL
        self.lw_fail = (when, reason, detail)
        return True

    def lw_failure_text(self) -> str:
        when, reason, detail = self.lw_fail
        return f"its last attempt failed at {when}: {reason}" + (f" ({detail})" if detail else "")

    def header(self) -> None:
        c = self.c
        self.out(
            f"Headset {c.device or 'none'} ({c.device_from}), LDAC quality {c.quality}, LE Audio {c.le_audio} ({c.le_from}); host /etc in {self.p.etc}\n"
        )

    def diff(self, old: str, new_text: str | None = None, new_path: str = "") -> None:
        """diff -u OLD NEW | sed 's/^/    /' (NEW: a file, or the text as bash's <(...) gave it)."""
        if new_text is None:
            r = self.run(["diff", "-u", old, new_path])
        else:
            # The new text as a pipe on /dev/fd/N, so the diff names it as bluetooth-setup.sh's did.
            r = self.run(["bash", "-c", 'diff -u "$1" <(cat)', "bt-setup-diff", old], input_text=new_text)
        if r.stderr:
            self.err(r.stderr)
        if r.stdout:
            *lines, last = r.stdout.split("\n")
            self.out("".join(f"    {line}\n" for line in lines) + (f"    {last}" if last else ""))

    def _restarts_report(self) -> None:
        """The restarts section of plan: what apply restarts, and why."""
        self.section("restarts")
        if self.bt_restart:
            # The reason that holds: a restart only the record owes is named as owed, not as a main.conf
            # change (main.conf is already as wanted after --no-restart, or untouched after --force).
            self.item("restart", BLUETOOTH_UNIT, BT_RESTART_MAIN_CONF if self.mc_changed else BT_RESTART_OWED)
            if self.mc_changed and self.bt_owed:
                self.item("", "", OWED_NOTE)
        else:
            self.item("ok", BLUETOOTH_UNIT, "no restart needed")
        if self.wp_restart:
            self.item("restart", "wireplumber (your session)", "audio stops for a moment; A2DP is restored afterwards")
            if self.le_change:
                self.item("", "", "(the LE Audio change takes effect only with this restart)")
            if self.wp_owed:
                self.item("", "", OWED_NOTE)
        else:
            self.item("ok", "wireplumber (your session)", "no restart needed")

    def plan(self) -> int:
        """plan: what apply would change. 0: nothing to do; 10: apply would write or restart something."""
        self.compute_plan()
        self.out(
            "Dry run: nothing is changed. ok = nothing to do; adopt = apply only renews comments or the install record, restarts nothing;\n"
            "write/update/remove/set/restart = apply would change that; CONFLICT = apply stops; ? = could not be read;\n"
            "warn/note = for your information.\n"
        )
        self.header()
        self.files_report()
        self._restarts_report()
        if self.mc_changed:
            self.section("main.conf, as apply would write it")
            self.diff(self.p.main_conf, new_path=self.tmp)
        if self.wp_state == WpState.CHANGE:
            self.section(f"{wp_label(self.wp_conf)}, as apply would write it")
            self.diff(self.wp_conf, render_wp(self.c.device, self.c.quality))
        if self.c.le_audio == "off" and self.le_state == LeState.CHANGE:
            self.section(f"{wp_label(self.p.le_conf)}, as apply would write it")
            self.diff(self.p.le_conf, render_le_off())
        if self.lw_want and self.lw_unit_st == OwnedState.CHANGE:
            self.section(f"{self.lw_unit_label()}, as apply would write it")
            self.diff(self.p.lw_unit_file, render_unit(self.c.device, self.c.ldac_rate))
        self.out("\n")
        if (
            self.mc_changed
            or self.bt_restart
            or self.wp_restart
            or self.legacy
            or self.lw_change
            or self.le_change
            or self.device_save
            or self.wp_state not in (WpState.OK, WpState.NONE)
            or self.bearer_state == BearerState.SET
            or self.le_state == LeState.ADOPT
        ):
            self.out(f"Apply with: {self.c.prog} apply\n")
            return 10
        self.out("Nothing to do.\n")
        return 0

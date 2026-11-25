# SPDX-License-Identifier: MPL-2.0
"""`cst bluetooth apply`, `revert` and `le-audio`: the changes, in bluetooth-setup.sh's order.

The restarts a run owes are recorded before the write that needs them
(Change.owe), so a run stopped after a write leaves the restart owed; the
restarts that a run does not make (--no-restart, the question answered no,
a restart that failed, a stop) stay owed in that record, which the next run
reads (Survey.restart_state): apply and revert resume them.
"""

from collections.abc import Callable
from dataclasses import dataclass
import os.path
import re
import time

from bluetooth.domain import (
    BLUETOOTH_UNIT,
    ISO_UUID,
    LDAC_UNIT,
    MARKER,
    WIREPLUMBER_UNIT,
    render_le_off,
    render_unit,
    render_wp,
    revert_general,
    settings_set,
)
from bluetooth.setup.application import watcher_unit
from bluetooth.setup.application.checks import Checks
from bluetooth.setup.application.inputs import is_cst_rule
from bluetooth.setup.application.survey import StepFailed
from bluetooth.setup.domain import (
    A2DP_SINK_UUID,
    BACKUP_NAME_TRIES,
    BapState,
    BearerState,
    BluetoothCommand,
    LeState,
    OlderCopy,
    WpState,
    backup_name,
    backup_name_taken,
    backup_order,
    bearer_matches,
    changes_machine,
    matches_a_line,
)
from cst_core.domain.errors import Refused, shell_exit_status
from cst_core.domain.files import OwnedState
from cst_core.domain.install import Component, InstallReportWord
from cst_core.domain.units import ActiveState, EnablementState
from cst_core.ports import CommandResult, OutputMode, UnitName

SPACE = r"[ \t\n\r\f\v]"


@dataclass(frozen=True, slots=True)
class ReRun:
    """The program runs again (exec) with these arguments: le-audio's apply."""

    args: tuple[str, ...]


class Change(Checks):
    """One run of apply, revert or le-audio."""

    def run_command(self) -> int | ReRun:
        """The command; a stop says why on stderr: its exit status, or the run again of le-audio."""
        prog = self.c.prog
        # apply and revert name the backup and the undo when a command stops them (the ERR trap of old).
        changing = self.o.cmd == BluetoothCommand.APPLY or (self.o.cmd == BluetoothCommand.REVERT and not self.o.check)
        commands: dict[BluetoothCommand, Callable[[], int | ReRun]] = {
            BluetoothCommand.PLAN: self.plan,
            BluetoothCommand.STATUS: self.status,
            BluetoothCommand.APPLY: self.apply,
            BluetoothCommand.REVERT: self.revert,
            BluetoothCommand.LE_AUDIO: self.le_audio,
        }
        try:
            self.need_tmp()  # every command made it first (TMP="$(mktemp)"), before the ERR trap
        except StepFailed as e:
            return e.status
        try:
            return commands[self.o.cmd]()
        except BrokenPipeError:
            if not changes_machine(self.o):
                raise  # a command that only reads ends by SIGPIPE (wiring.run_guarded)
            self.m.console.out_closed()
            self.err(f"{prog}: stopped: its output was closed.{self.undo_hint() if changing else ''}\n")
            return 1
        except Refused as e:
            self.err(f"{prog}: ERROR: {e}\n")
            return 1
        except (StepFailed, OSError) as e:
            status = e.status if isinstance(e, StepFailed) else 1
            if isinstance(e, OSError):
                self.err(f"{e}\n")
            if changing:
                self.err(f"{prog}: stopped: a command failed (exit {status}).{self.undo_hint()}\n")
            return status

    def undo_hint(self) -> str:
        return f" Backup of main.conf: {self.backup or 'none taken'}. Undo: {self.c.prog} revert"

    def _must_result(self, r: CommandResult) -> None:
        if r.returncode != 0:
            raise StepFailed(shell_exit_status(r.returncode))

    def preconditions(self) -> None:
        for name in ("bluetoothctl", "busctl", "systemctl", "python3", "journalctl", "timeout", "sudo"):
            if not self.m.files.which(name):
                raise self.die(f"command not found: {name}")
        if self.m.user_systemd.show_environment().returncode != 0:
            raise self.die("no systemd --user session (XDG_RUNTIME_DIR/D-Bus): run it in your desktop or login session")

    def checked(self, args: list[str], input_text: str | None = None) -> str:
        """The installer's stdout; its stderr shown; a failure stops the run (out="$(cst_installer ...)" under set -e)."""
        r = self.installer(*args, output=OutputMode.CAPTURE, input_text=input_text)
        if r.stderr:
            self.err(r.stderr)
        if r.returncode != 0:
            raise StepFailed(shell_exit_status(r.returncode))
        return r.stdout

    # ---- main.conf -------------------------------------------------------------------------
    def install_main_conf(self, src: str) -> None:
        """A backup of a name of its own, then the new main.conf atomically (root, through sudo)."""
        mc = self.p.main_conf
        ctx: list[str] = []
        z: list[str] = []
        if (
            self.m.files.which("selinuxenabled")
            and self.run(["selinuxenabled"], output=OutputMode.INHERIT).returncode == 0
        ):
            ctx, z = ["--preserve=context"], ["-Z"]
        bak = self.take_backup(ctx)
        self.backup = bak
        self.out(f"backup:    {bak}\n")
        self.must(["sudo", "install", "-m", "0644", "-o", "root", "-g", "root", *z, src, f"{mc}.new"])
        # bluetoothd reads main.conf; its restart takes WirePlumber's endpoints.
        self.owe(BLUETOOTH_UNIT, WIREPLUMBER_UNIT)
        self.must(["sudo", "mv", "-f", f"{mc}.new", mc])
        self.out(f"written:   {mc}\n")

    def take_backup(self, ctx: list[str]) -> str:
        """main.conf copied (cp -p) to main.conf.bak-YYYYmmdd-HHMMSS, or to the first of -1, -2, ...
        after it that no backup has: a backup is never replaced. cp --update=none-fail creates the
        name it is given only if it does not exist (O_CREAT|O_EXCL; GNU cp 9.5 or later), so a name
        another run takes between the look and the copy is not replaced either: cp fails, the name is
        there (cp says "File exists" or "not replacing"), and the next one is tried. Any other failure
        stops the run, also one that left a part of the copy behind (a full disk). cp runs as
        `sudo env LC_ALL=C cp ...`: it speaks the C locale whatever sudoers keeps of the caller's."""
        mc = self.p.main_conf
        base = f"{mc}.bak-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.m.clock.now()))}"
        taken = self.backup_names()
        for n in range(BACKUP_NAME_TRIES):
            bak = backup_name(base, n)
            if bak in taken:
                continue
            # cp's words are read, so cp gets the C locale from env: sudo passes LC_ALL on only when
            # sudoers keeps it (Fedora's does; one whose env_keep and env_check drop it gave cp the
            # caller's LANG, and a translated "File exists" stopped the run). locale_c is for sudo's own.
            r = self.m.runner.run(
                ["sudo", "env", *self.m.c_locale, "cp", "-p", *ctx, "--update=none-fail", mc, bak], locale_c=True
            )
            if r.returncode == 0:
                return bak
            taken = self.backup_names()
            if bak not in taken or not backup_name_taken(r.stderr):
                self.out(r.stdout)
                self.err(r.stderr)
                raise StepFailed(shell_exit_status(r.returncode))
        raise self.die(f"no free name for a backup of {mc} ({base} and {BACKUP_NAME_TRIES - 1} more are taken)")

    def backup_names(self) -> set[str]:
        """The names main.conf's backups have now."""
        return {name for _, name in self.m.files.backups(self.p.main_conf)}

    def write_wp(self) -> None:
        """The rule, as the user, atomically; one with other settings is kept as .bak first."""
        f = self.m.files
        f.make_dirs(self.p.wp_dir)
        if self.wp_state == WpState.CHANGE:
            f.copy(self.wp_conf, self.wp_conf + ".bak")
            self.out(f"backup:    {self.wp_conf}.bak\n")
        f.write_user_file(self.wp_conf, render_wp(self.c.device, self.c.quality), 0o644)

    # ---- restarts --------------------------------------------------------------------------
    def restart_bluetooth(self) -> bool:
        self.out("restart:   bluetooth.service (Bluetooth connections drop for a few seconds)\n")
        restarted = self.run(["sudo", "systemctl", "restart", "bluetooth"], output=OutputMode.INHERIT).returncode == 0
        self.forget_unit(BLUETOOTH_UNIT)  # a new invocation, also after most failures
        if not restarted:
            self.err(f"{self.c.prog}: ERROR: bluetooth.service was not restarted. To finish: {self.restart_hint()}\n")
            return False
        path, found = self.adapter()
        fallback = (path + "\n" if path else "") + "/org/bluez/hci0"  # $(adapter_path || echo ...)
        target = path if found else fallback
        powered = ["busctl", "get-property", "org.bluez", target, "org.bluez.Adapter1", "Powered"]
        if not self.wait_for(15, lambda: self.ok(powered)):
            self.err("WARNING: the adapter did not answer within 15 s after the restart\n")
        return True

    def restart_wireplumber(self) -> bool:
        self.out("restart:   wireplumber (your session; audio stops for a moment)\n")
        restarted = self.m.user_systemd.restart(UnitName("wireplumber"), output=OutputMode.INHERIT).returncode == 0
        self.forget_unit(WIREPLUMBER_UNIT)
        if not restarted:
            self.err(
                f"{self.c.prog}: ERROR: wireplumber was not restarted. To finish: systemctl --user restart wireplumber\n"
            )
            return False
        self.m.clock.sleep(1)  # let the old WirePlumber unregister before checking
        # Each look reads bluetoothd's invocation again: it may restart by itself during the wait.
        if not self.le_off_active() and not self.wait_for(
            15, lambda: self.bap_endpoint_state(fresh=True) == BapState.REGISTERED
        ):
            self.err("WARNING: WirePlumber did not register the BAP endpoint within 15 s\n")
        self.restore_a2dp()
        return True

    def restore_a2dp(self) -> None:
        """After a WirePlumber restart only HFP comes back by itself: reconnect the Audio Sink and
        switch back to A2DP, unless the headset's microphone is in use (no headset: nothing to do)."""
        if not self.headset:
            return
        info = self.bt_info()
        if not matches_a_line(info, rf"^{SPACE}*BREDR\.Connected: yes"):
            return
        if not self.le_off_active() and matches_a_line(info, rf"^{SPACE}*LE\.Connected: yes"):
            return
        if not self.wait_for(10, lambda: self.card_state() is not None):
            self.err("WARNING: the headset did not show up in PipeWire after the restart\n")
            return
        prof, a2dp, mic = self.card_state() or ("", "", "")
        if prof.startswith("a2dp-sink"):
            return
        if mic == "1":
            self.err(
                f"WARNING: the headset is in {prof} with its microphone in use; A2DP not restored (switch the profile after the call)\n"
            )
            return
        if a2dp != "1":
            prof = self._reconnect_a2dp()
            if prof is None:
                return
        if not prof.startswith("a2dp-sink"):
            r = self.run(
                ["pactl", "set-card-profile", f"bluez_card.{self.c.device_id}", "a2dp-sink"], output=OutputMode.STDOUT
            )
            self.out(r.stdout)
            if r.returncode != 0:
                self.err("WARNING: could not switch the profile to a2dp-sink\n")
                return
        self.out("restored:  the headset is back on A2DP\n")

    def _reconnect_a2dp(self) -> str | None:
        """Connect the Audio Sink again and wait 10 s for it: the card's profile then, or None (warned)."""
        self.out("reconnect: the headset's A2DP profile (Audio Sink)\n")
        self.run(["timeout", "20", "bluetoothctl", "connect", self.c.device, A2DP_SINK_UUID])
        for _ in range(20):
            prof, a2dp, _mic = self.card_state() or ("", "", "")
            if a2dp == "1":
                return prof
            self.m.clock.sleep(0.5)
        self.err("WARNING: A2DP did not come back; switch the headset off and on\n")
        return None

    def confirm_bt_restart(self) -> bool:
        if self.o.yes:
            return True
        answer = self.m.console.ask(
            "Restart Bluetooth now? Bluetooth keyboards, mice and headsets will disconnect. [y/N] "
        )
        if answer is None:
            self.out(f"No terminal to ask on: use --yes to restart. To finish later: {self.restart_hint()}\n")
            return False
        if re.fullmatch(r"[yY]", answer):
            return True
        self.out(f"Restart cancelled. To finish later: {self.restart_hint()}\n")
        return False

    def save_owed(self, *units: str) -> bool:
        """The record saved, owing the units' restarts; False when it cannot be written (warned once a
        run). The invocation IDs are read again first: a service that restarted by itself since they
        were read may have read the old files."""
        self.forget_unit(BLUETOOTH_UNIT, WIREPLUMBER_UNIT)
        if self.restart_state("save", *units) is not None:
            return True
        if not self.owed_warned:
            self.err(f"WARNING: could not record the restarts still owed in {self.p.pending}\n")
            self.owed_warned = True
        return False

    def owe(self, *units: str) -> None:
        """The restarts a write needs, recorded right before it: a run stopped after the write (and
        before its restarts) leaves them owed, and the next run makes them. Each save records every
        restart this run's writes needed so far, under the invocation IDs read right before it: a unit
        whose earlier save failed is in the next one, and a service that restarted by itself since an
        earlier write is owed again under its new ID. A save that fails is warned of, and the run goes
        on; only then can a stop after the write lose the restart. A service that restarts by itself
        between the save and the write reads the old files, and the record no longer owes it: the
        window is that of one write."""
        self.owing.update(units)
        self.save_owed(*sorted(self.owing))

    def do_restarts(self, *, bt: bool, wp: bool) -> int:
        """0: done (or nothing to do); 1: cancelled at the question; 2: a restart failed."""
        if not (bt or wp):
            return 0
        self.save_owed(WIREPLUMBER_UNIT, *([BLUETOOTH_UNIT] if bt else []))
        if self.o.no_restart:
            hint = self.restart_hint() if bt else "systemctl --user restart wireplumber"
            self.out(f"Not restarted (--no-restart). To finish: {hint}\n")
            return 0
        if bt:
            if not self.confirm_bt_restart():
                return 1
            if not self.restart_bluetooth():
                return 2
        if not self.restart_wireplumber():
            return 2
        self.restart_state("save")  # both run with new invocations: nothing is owed any more
        return 0

    # ---- apply -----------------------------------------------------------------------------
    def apply(self) -> int:
        self.preconditions()
        self.compute_plan()
        self.refuse_conflicts()
        self.save_headset()
        self.apply_files()
        if not self.run_restarts():
            return 0
        self.apply_watcher()
        self.apply_bearer()
        # The states compute_plan read are from before the changes: read them again for the checks.
        self.le_plan()
        self.lw_plan()
        self.verify()
        self.out("\n")
        if self.fails:
            raise self.die("verification found problems (see above).")
        self.done()
        return 0

    def save_headset(self) -> None:
        """The headset of CST's LDAC rule saved as DEVICE in the user's file (an older CST set it by
        default, so the rule was its only record): apply, and revert before it removes the rule."""
        if not self.device_save:
            return
        self.out("== settings: the headset of CST's LDAC rule\n")
        if self.o.check:
            self.out(f"would save DEVICE={self.c.device} in {self.tilde(self.p.user_settings)}\n")
        else:
            self.save_setting("DEVICE", self.c.device)

    def refuse_conflicts(self) -> None:
        """apply stops at a file of CST's name that CST did not write."""
        p, c = self.p, self.c
        if self.wp_state == WpState.CONFLICT:
            raise self.die(
                f"{self.wp_conf} was not written by CST or apply-le-audio.sh: move it away, then run apply again."
            )
        if c.le_audio == "off" and self.le_state == LeState.CONFLICT:
            raise self.die(f"{p.le_conf} was not written by CST: move it away, then run apply again.")
        if self.lw_want:
            if self.lw_unit_st == OwnedState.CONFLICT:
                raise self.die(f"{self.lw_unit_file} was not written by CST: move it away, then run apply again.")
            if self.lw_prog_st == OwnedState.CONFLICT:
                raise self.die(f"{p.lw_prog} was not written by CST: move it away, then run apply again.")

    def run_restarts(self) -> bool:
        """apply's restarts; False when the question was answered no (the watcher is set up anyway)."""
        c = self.c
        self.out("== restarts\n")
        if self.bt_restart or self.wp_restart:
            rc = self.do_restarts(bt=self.bt_restart, wp=self.wp_restart)
            if rc == 1:
                # Cancelled: the files are written; the watcher does not need the restarts.
                self.apply_watcher()
                pending = "the restarts above" + (
                    ", the preferred bearer" if self.bearer_state == BearerState.SET else ""
                )
                self.out(f"\nNot done yet: {pending} and the checks.\nAfter restarting, run: {c.prog} apply\n")
                return False
            if rc != 0:
                raise self.die(
                    f"a restart failed (see above). Backup of main.conf: {self.backup or 'none taken'}. Undo: {c.prog} revert"
                )
        else:
            self.out("none needed\n")
        return True

    def apply_files(self) -> None:
        """main.conf, the rule and the drop-in."""
        p, c = self.p, self.c
        self.out(f"== BlueZ: {p.main_conf}\n")
        if self.mc_changed:
            self.install_main_conf(self.tmp)
            self.out(f"           Experimental = true, KernelExperimental = {ISO_UUID}\n")
        else:
            self.out(f"unchanged: {p.main_conf} (Experimental and KernelExperimental already set in [General])\n")
        self.out(f"== WirePlumber: {self.wp_title()}\n")
        if self.legacy:
            self.owe(WIREPLUMBER_UNIT)
            self.m.files.remove_file(p.legacy_wp_conf)
            self.out(f"removed:   {p.legacy_wp_conf} (older name of the same rule)\n")
        match self.wp_state:
            case WpState.OK:
                self.out(f"unchanged: {self.wp_conf}\n")
            case WpState.NONE:
                self.out(f"skipped:   {self.no_headset()}\n")
            case WpState.ADOPT:
                self.write_wp()
                self.out(f"adopted:   {self.wp_conf} (comments renewed, settings unchanged: no restart)\n")
            case _:
                self.owe(WIREPLUMBER_UNIT)
                self.write_wp()
                self.out(f"written:   {self.wp_conf} (LDAC {c.quality})\n")
        self.apply_le()

    def apply_bearer(self) -> None:
        """The preferred bearer, when apply sets one."""
        c = self.c
        match self.bearer_state:
            case BearerState.SET:
                self.out("== preferred bearer\n")
                cmd = ["bluetoothctl", "bearer", c.device, c.bearer]
                if self.m.bt_timeout > 0:
                    cmd = ["timeout", str(self.m.bt_timeout), *cmd]
                said = self.run(cmd, output=OutputMode.MERGE).stdout.rstrip("\n")
                if bearer_matches(self.bt_info(), c.bearer):
                    self.out(
                        f"set:       PreferredBearer = {c.bearer} (for connections the PC starts; disconnect and reconnect)\n"
                    )
                else:
                    self.err(f"WARNING: could not set the bearer: {said or 'no output'}\n")
            case BearerState.UNPAIRED:
                self.err(f"WARNING: {c.device} is not paired; bearer not changed\n")
            case BearerState.NOHEADSET:
                self.err(f"WARNING: BEARER={c.bearer} is for a headset, and none is set; bearer not changed\n")
            case _:
                pass

    def done(self) -> None:
        """apply's last lines, after checks that all passed."""
        c = self.c
        if self.warns:
            self.out(f"Done, with {self.warns} warning(s). Details: {c.prog} status\n")
        elif not self.headset:
            self.out(f"Done. The headset's parts were skipped: {self.no_headset()}\n")
            if c.le_audio == "off":
                self.out(f"Turn LE Audio on: {self.le_on_hint}\n")
        elif c.le_audio == "off":
            self.out(f"Done. LE Audio is off in PipeWire: the headset plays over a2dp-sink, with LDAC ({c.quality}).\n")
            self.out(f"Turn LE Audio on: {self.le_on_hint}\n")
        else:
            self.out(
                f"Done. With the headset on LE: bap-* profiles (LC3). On BR/EDR: a2dp-sink, with LDAC ({c.quality}).\n"
            )
            self.out(f"Choose the transport: {c.prog} apply --bearer le|bredr   (for connections the PC starts)\n")

    def apply_le(self) -> None:
        """The LE Audio drop-in written (LE_AUDIO=off) or removed (on), as the user, atomically."""
        le, f = self.p.le_conf, self.m.files
        self.out(f"== LE Audio: {le}\n")
        if self.c.le_audio == "off":
            if self.le_state == LeState.OK:
                self.out("unchanged: LE Audio off\n")
                return
            f.make_dirs(self.p.wp_dir)
            if self.le_change:  # not a renewal of comments (adopt): WirePlumber reads no comment
                self.owe(WIREPLUMBER_UNIT)
            f.write_user_file(le, render_le_off(), 0o644)
            if self.le_state == LeState.ADOPT:
                self.out(f"adopted:   {le} (comments renewed, no restart)\n")
            else:
                self.out(f"written:   {le} (LE Audio off: PipeWire offers only A2DP and HFP)\n")
            return
        match self.le_state:
            case LeState.REMOVE:
                self.owe(WIREPLUMBER_UNIT)
                f.remove_file(le)
                self.out(f"removed:   {le} (LE Audio on)\n")
            case LeState.ABSENT:
                self.out("unchanged: LE Audio on (no drop-in)\n")
            case LeState.CONFLICT:
                self.err(f"WARNING: {le} was not written by CST: left alone (it may keep LE Audio off)\n")
            case _:
                pass

    def apply_watcher(self) -> None:
        self.out(f"== LDAC rate watcher: {LDAC_UNIT}\n")
        if self.lw_want:
            self.lw_install()
        else:
            self.lw_remove()

    def lw_install(self) -> None:
        """The watcher's program and unit (through the installer, as the user), enabled and started.
        The installer runs also for a current copy: it writes nothing then (`unchanged:`), but adds
        a copy its record does not list yet (installed before the record existed). What it wrote
        (its `written:` lines), not what plan saw, decides the daemon-reload and the restart: a file
        that changed after plan (a git pull meanwhile) is still taken up."""
        p, unit = self.p, LDAC_UNIT
        self.lw_remove_stale()
        if self.lw_prog_st not in (OwnedState.OK, OwnedState.ADOPT):
            self.must(["python3", "-I", "-c", "import ast, sys; ast.parse(open(sys.argv[1]).read())", p.lw_src])
        r = self.installer("install", "--home", p.home, "bluetooth/bt-ldac-watch", output=OutputMode.CAPTURE)
        self.out(r.stdout)  # its lines, then its error, as it printed them
        if r.stderr:
            self.err(r.stderr)
        if r.returncode != 0:
            raise StepFailed(shell_exit_status(r.returncode))
        said = self.checked(
            ["text", "--home", p.home, self.lw_unit_file, "0644", "--component", Component.BLUETOOTH_LDAC],
            input_text=render_unit(self.c.device, self.c.ldac_rate),
        )
        self.out(f"{said.rstrip(chr(10))} (LDAC to {self.c.ldac_rate} Hz)\n")
        unit_changed = said.startswith(InstallReportWord.WRITTEN)
        if unit_changed:
            self._must_result(self.m.user_systemd.daemon_reload_result(output=OutputMode.INHERIT))
        changed = unit_changed or any(line.startswith(InstallReportWord.WRITTEN) for line in r.stdout.splitlines())
        if self.lw_enabled != EnablementState.ENABLED:
            self._must_result(self.m.user_systemd.enable(UnitName(unit), output=OutputMode.INHERIT))
            self.out(f"enabled:   {unit} (starts with your session)\n")
        if self.lw_active != ActiveState.ACTIVE:
            self._must_result(self.m.user_systemd.start(UnitName(unit), output=OutputMode.INHERIT))
            self.out(f"started:   {unit} (logs: journalctl --user -u {unit})\n")
        elif changed:
            self._must_result(self.m.user_systemd.restart(UnitName(unit), output=OutputMode.INHERIT))
            self.out(f"restarted: {unit} (new files)\n")
        else:
            self.out(f"unchanged: {unit} enabled and active\n")
        self.lw_active = self.m.user_systemd.is_active(UnitName(unit), output=OutputMode.STDOUT).stdout.rstrip("\n")
        r = self.installer("mark", "--home", p.home, "--component", Component.BLUETOOTH_LDAC, "done")
        if r.returncode != 0:
            raise StepFailed(shell_exit_status(r.returncode))

    def lw_remove_stale(self) -> None:
        """An older CST's copies of the watcher's unit (lw_stale: recorded where the user manager does not
        read units) removed through the installer, which forgets them in the record too; a file there
        that CST did not write is left alone and only forgotten, with a warning, and so is another path to
        the current unit file (only its record line goes). The manager never
        loaded them: no disable, no reload. With --check: only said."""
        current = self.lw_unit_file
        for path in self.lw_stale:
            kind = watcher_unit.copy_kind(self.m.files, path, current)
            if kind == OlderCopy.SAME:
                # The live unit file by another path: removing it would remove the unit.
                if self.o.check:
                    self.out(f"would forget {path} (the same file as {current})\n")
                else:
                    self.checked(["forget-text", "--home", self.p.home, path])
                    self.out(f"forgotten: {path} (the same file as {current})\n")
            elif self.o.check:
                self.out(f"would remove {path} (an older CST's copy)\n")
            elif kind == OlderCopy.FOREIGN:
                self.checked(["forget-text", "--home", self.p.home, path])
                self.err(f"WARNING: {path} was not written by CST: left alone (forgotten from the install record)\n")
            else:
                said = self.checked(["remove-text", "--home", self.p.home, path])
                self.out(f"{said.rstrip(chr(10))} (an older CST's copy)\n" if said else f"forgotten: {path}\n")

    def lw_remove(self) -> None:
        """The watcher stopped, disabled and removed, if CST installed it (with --check: only said)."""
        p, unit = self.p, LDAC_UNIT
        self.lw_remove_stale()
        if not self.lw_ours_present():
            self.out("unchanged: not installed\n")
            if self.lw_unit_st == OwnedState.CONFLICT:
                self.err(f"WARNING: {self.lw_unit_file} was not written by CST: left alone\n")
            if self.lw_prog_st == OwnedState.CONFLICT:
                self.err(f"WARNING: {p.lw_prog} was not written by CST: left alone\n")
            return
        if self.o.check:
            self.out(f"would stop, disable and remove {unit} and {p.lw_prog}\n")
            return
        if self.lw_unit_st in (OwnedState.OK, OwnedState.CHANGE):
            self.out(self.m.user_systemd.disable(UnitName(unit), now=True, output=OutputMode.STDOUT).stdout)
            self.checked(["remove-text", "--home", p.home, self.lw_unit_file])
            self.out(f"removed:   {self.lw_unit_file} (stopped and disabled)\n")
            self._must_result(self.m.user_systemd.daemon_reload_result(output=OutputMode.INHERIT))
        if self.lw_prog_st in (OwnedState.OK, OwnedState.ADOPT, OwnedState.CHANGE):
            self.checked(["remove", "--home", p.home, "bluetooth/bt-ldac-watch"])
            self.m.files.remove_empty_dir(os.path.dirname(p.lw_prog))
            self.out(f"removed:   {p.lw_prog}\n")
        self.lw_active = ""

    # ---- revert ----------------------------------------------------------------------------
    def oldest_backup(self) -> str | None:
        """The oldest main.conf backup by time, then name and -N number (ours or the old script's):
        the state before any of this."""
        found = sorted(backup_order(mtime, name) for mtime, name in self.m.files.backups(self.p.main_conf))
        return found[0][3] if found else None

    def revert(self) -> int:
        p, f, check = self.p, self.m.files, self.o.check
        if not check:
            self.preconditions()
        mc = p.main_conf
        if not (f.is_file(mc) and not f.is_symlink(mc)):
            raise self.die(f"{mc} is missing or a symlink")
        self.read_owed()  # before any change: what an earlier run (apply or revert) left owed
        changed_mc = self.revert_main_conf()
        self.save_headset()  # the rule goes next: it may be the only place that names the headset
        wp, le = self.revert_files()
        self.out(f"== LDAC rate watcher: {LDAC_UNIT}\n")
        self.lw_plan()
        lw = self.lw_ours_present() or bool(self.lw_stale)
        self.lw_remove()
        bt = changed_mc or self.bt_owed
        restart = bt or wp or le or self.wp_owed  # a bluetoothd restart takes WirePlumber's endpoints
        if check:
            if bt:
                self.out("would restart bluetooth.service (after asking) and wireplumber\n")
            elif restart:
                self.out("would restart wireplumber\n")
            self.owed_report()
            return 10 if restart or lw else 0
        self.out("== restarts\n")
        if not restart:
            self.out("none needed\n")
            return 0
        self.owed_report()
        if self.do_restarts(bt=bt, wp=True) == 2:
            raise self.die(f"a restart failed (see above). Backup of main.conf: {self.backup or 'none taken'}")
        return 0

    def owed_report(self) -> None:
        """revert's line for each restart the record owes (plan's words)."""
        for unit, owed in ((BLUETOOTH_UNIT, self.bt_owed), (WIREPLUMBER_UNIT, self.wp_owed)):
            if owed:
                self.out(f"owed:      {unit} (still owed: an earlier run did not restart it)\n")

    def revert_main_conf(self) -> bool:
        """The two keys back to the oldest backup's; whether main.conf changes."""
        f, check, mc = self.m.files, self.o.check, self.p.main_conf
        changed_mc = False
        self.out(f"== BlueZ: {mc}\n")
        backup = self.oldest_backup()
        if backup is not None:
            tmp = self.need_tmp()
            try:
                new, changed_mc = revert_general(self.m.host.read(mc), self.m.host.read(backup))
            except (OSError, ValueError) as e:
                self.err(f"bt-helper: {e}\n")
                raise self.die(f"could not process {mc} (rc=1)") from e
            f.write_user_file(tmp, new, 0o600)
            if not changed_mc:
                self.out(f"unchanged: Experimental and KernelExperimental are already as in {backup}\n")
            elif check:
                self.out(f"would set the two keys as in {backup}:\n")
                self.diff(mc, new_path=tmp)
            else:
                self.install_main_conf(tmp)
                self.out(f"           the two keys as in {backup}\n")
        else:
            self.err(f"WARNING: no backup {mc}.bak-* found; main.conf not changed\n")
        return changed_mc

    def revert_files(self) -> tuple[bool, bool]:
        """The rule and the drop-in removed when they are CST's; whether each was."""
        p, f, check = self.p, self.m.files, self.o.check
        wp = le = False
        self.out(f"== WirePlumber: {self.wp_title()}\n")
        if not self.headset:
            self.out(f"unchanged: {self.no_headset()}\n")
        elif not f.exists(self.wp_conf):
            self.out("unchanged: the rule does not exist\n")
        elif not is_cst_rule(f, self.wp_conf):
            self.err(f"WARNING: {self.wp_conf} was not written by CST or apply-le-audio.sh: left alone\n")
        else:
            wp = True
            if check:
                self.out(f"would remove {self.wp_conf}\n")
            else:
                self.owe(WIREPLUMBER_UNIT)
                f.remove_file(self.wp_conf)
                self.out(f"removed:   {self.wp_conf}\n")
        self.out(f"== LE Audio: {p.le_conf}\n")
        if not f.exists(p.le_conf):
            self.out("unchanged: the drop-in does not exist (LE Audio on)\n")
        elif not f.contains(p.le_conf, MARKER, quiet=True):
            self.err(f"WARNING: {p.le_conf} was not written by CST: left alone\n")
        else:
            le = True
            if check:
                self.out(f"would remove {p.le_conf} (LE Audio on again)\n")
            else:
                self.owe(WIREPLUMBER_UNIT)
                f.remove_file(p.le_conf)
                self.out(
                    f"removed:   {p.le_conf} (LE Audio on again; LE_AUDIO=off in the settings writes it again at the next apply)\n"
                )
        return wp, le

    # ---- the user's settings ----------------------------------------------------------------
    def save_setting(self, key: str, value: str) -> None:
        """KEY=VALUE in the user's settings file: through a link, atomically, the other lines kept
        (the file and its folder made when missing)."""
        f = self.m.files
        label = self.tilde(self.p.user_settings)
        f.make_dirs(os.path.dirname(self.p.user_settings))
        target = f.realpath(self.p.user_settings)
        try:
            new, changed = settings_set(self.m.host.read_or_none(target), key, value)
        except (OSError, ValueError) as e:
            self.err(f"bt-helper: {e}\n")
            raise self.die(f"could not update {label} (rc=1)") from e
        if changed:
            f.write_through(target, new)
            self.out(f"written:   {label}: {key}={value}\n")
        else:
            self.out(f"unchanged: {label} already has {key}={value}\n")

    # ---- le-audio --------------------------------------------------------------------------
    def le_audio(self) -> int | ReRun:
        """le-audio on|off: LE_AUDIO in the user's settings (through a link, atomically, the other
        lines kept), then apply runs (ReRun). Without on/off: the setting and the CST file's state."""
        c = self.c
        if not self.o.le_set:
            self.le_plan()
            self.out(f"LE Audio in PipeWire: {c.le_audio} ({c.le_from})\n")
            self.le_report()
            self.out(
                f"Turn it on: {self.le_on_hint}\n" if c.le_audio == "off" else f"Turn it off: {self.le_off_hint}\n"
            )
            return 0
        self.save_setting("LE_AUDIO", self.o.le_set)
        args = ("apply", *(["--yes"] if self.o.yes else []), *(["--no-restart"] if self.o.no_restart else []))
        self.out(f"Now: {c.prog} {' '.join(args)}\n")
        return ReRun(args)

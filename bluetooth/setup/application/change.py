# SPDX-License-Identifier: MPL-2.0
"""`cst bluetooth apply`, `revert` and `le-audio`: the changes, in bluetooth-setup.sh's order.

The restarts that a run does not make (--no-restart, the question answered
no, a restart that failed) stay owed in a record that the next run reads
(Survey.restart_state): apply resumes them.
"""

from dataclasses import dataclass
import os.path
import re
import time

from bluetooth.domain import (
    ISO_UUID,
    LDAC_UNIT,
    MARKER,
    render_le_off,
    render_unit,
    render_wp,
    revert_general,
    settings_set,
)
from bluetooth.setup.application.checks import Checks
from bluetooth.setup.application.survey import StepFailed, is_cst_rule
from bluetooth.setup.domain import A2DP_SINK_UUID, bearer_matches, changes_machine, matches_a_line
from cst_core.domain.errors import Refused, shell_exit_status

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
        changing = self.o.cmd == "apply" or (self.o.cmd == "revert" and not self.o.check)
        commands = {"plan": self.plan, "status": self.status, "apply": self.apply, "revert": self.revert}
        try:
            self.need_tmp()  # every command made it first (TMP="$(mktemp)"), before the ERR trap
        except StepFailed as e:
            return e.status
        try:
            return commands.get(self.o.cmd, self.le_audio)()
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

    def preconditions(self) -> None:
        for name in ("bluetoothctl", "busctl", "systemctl", "python3", "journalctl", "timeout", "sudo"):
            if not self.m.files.which(name):
                raise self.die(f"command not found: {name}")
        if not self.ok(["systemctl", "--user", "show-environment"]):
            raise self.die("no systemd --user session (XDG_RUNTIME_DIR/D-Bus): run it in your desktop or login session")

    def checked(self, args: list[str], input_text: str | None = None) -> str:
        """The installer's stdout; its stderr shown; a failure stops the run (out="$(cst_installer ...)" under set -e)."""
        r = self.installer(*args, output="capture", input_text=input_text)
        if r.stderr:
            self.err(r.stderr)
        if r.returncode != 0:
            raise StepFailed(shell_exit_status(r.returncode))
        return r.stdout

    # ---- main.conf -------------------------------------------------------------------------
    def install_main_conf(self, src: str) -> None:
        """A timestamped backup, then the new main.conf atomically (root, through sudo)."""
        mc = self.p.main_conf
        bak = f"{mc}.bak-{time.strftime('%Y%m%d-%H%M%S', time.localtime(self.m.clock.now()))}"
        z: list[str] = []
        if self.m.files.which("selinuxenabled") and self.run(["selinuxenabled"], output="inherit").returncode == 0:
            self.must(["sudo", "cp", "-p", "--preserve=context", mc, bak])
            z = ["-Z"]
        else:
            self.must(["sudo", "cp", "-p", mc, bak])
        self.backup = bak
        self.out(f"backup:    {bak}\n")
        self.must(["sudo", "install", "-m", "0644", "-o", "root", "-g", "root", *z, src, f"{mc}.new"])
        self.must(["sudo", "mv", "-f", f"{mc}.new", mc])
        self.out(f"written:   {mc}\n")

    def write_wp(self) -> None:
        """The rule, as the user, atomically; one with other settings is kept as .bak first."""
        f = self.m.files
        f.make_dirs(self.p.wp_dir)
        if self.wp_state == "change":
            f.copy(self.wp_conf, self.wp_conf + ".bak")
            self.out(f"backup:    {self.wp_conf}.bak\n")
        f.write_user_file(self.wp_conf, render_wp(self.c.device, self.c.quality), 0o644)

    # ---- restarts --------------------------------------------------------------------------
    def restart_bluetooth(self) -> bool:
        self.out("restart:   bluetooth.service (Bluetooth connections drop for a few seconds)\n")
        if self.run(["sudo", "systemctl", "restart", "bluetooth"], output="inherit").returncode != 0:
            self.err(f"{self.c.prog}: ERROR: bluetooth.service was not restarted. To finish: {self.restart_hint()}\n")
            return False
        path, found = self.adapter()
        target = path if found else (path + "\n" if path else "") + "/org/bluez/hci0"  # $(adapter_path || echo ...)
        powered = ["busctl", "get-property", "org.bluez", target, "org.bluez.Adapter1", "Powered"]
        if not self.wait_for(15, lambda: self.ok(powered)):
            self.err("WARNING: the adapter did not answer within 15 s after the restart\n")
        return True

    def restart_wireplumber(self) -> bool:
        self.out("restart:   wireplumber (your session; audio stops for a moment)\n")
        if self.run(["systemctl", "--user", "restart", "wireplumber"], output="inherit").returncode != 0:
            self.err(
                f"{self.c.prog}: ERROR: wireplumber was not restarted. To finish: systemctl --user restart wireplumber\n"
            )
            return False
        self.m.clock.sleep(1)  # let the old WirePlumber unregister before checking
        if not self.le_off_active() and not self.wait_for(15, lambda: self.bap_endpoint_state() == "registered"):
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
            self.out("reconnect: the headset's A2DP profile (Audio Sink)\n")
            self.run(["timeout", "20", "bluetoothctl", "connect", self.c.device, A2DP_SINK_UUID])
            for _ in range(20):
                prof, a2dp, mic = self.card_state() or ("", "", "")
                if a2dp == "1":
                    break
                self.m.clock.sleep(0.5)
            if a2dp != "1":
                self.err("WARNING: A2DP did not come back; switch the headset off and on\n")
                return
        if not prof.startswith("a2dp-sink"):
            r = self.run(["pactl", "set-card-profile", f"bluez_card.{self.c.device_id}", "a2dp-sink"], output="stdout")
            self.out(r.stdout)
            if r.returncode != 0:
                self.err("WARNING: could not switch the profile to a2dp-sink\n")
                return
        self.out("restored:  the headset is back on A2DP\n")

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

    def do_restarts(self, *, bt: bool, wp: bool) -> int:
        """0: done (or nothing to do); 1: cancelled at the question; 2: a restart failed."""
        if not (bt or wp):
            return 0
        owed = ["wireplumber.service", *(["bluetooth.service"] if bt else [])]
        if self.restart_state("save", *owed) is None:
            self.err(f"WARNING: could not record the restarts still owed in {self.p.pending}\n")
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
        if self.wp_state == "conflict":
            raise self.die(
                f"{self.wp_conf} was not written by CST or apply-le-audio.sh: move it away, then run apply again."
            )
        if c.le_audio == "off" and self.le_state == "conflict":
            raise self.die(f"{p.le_conf} was not written by CST: move it away, then run apply again.")
        if self.lw_want:
            if self.lw_unit_st == "conflict":
                raise self.die(f"{p.lw_unit_file} was not written by CST: move it away, then run apply again.")
            if self.lw_prog_st == "conflict":
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
                pending = "the restarts above" + (", the preferred bearer" if self.bearer_state == "set" else "")
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
            self.m.files.remove_file(p.legacy_wp_conf)
            self.out(f"removed:   {p.legacy_wp_conf} (older name of the same rule)\n")
        if self.wp_state == "ok":
            self.out(f"unchanged: {self.wp_conf}\n")
        elif self.wp_state == "none":
            self.out(f"skipped:   {self.no_headset()}\n")
        elif self.wp_state == "adopt":
            self.write_wp()
            self.out(f"adopted:   {self.wp_conf} (comments renewed, settings unchanged: no restart)\n")
        else:
            self.write_wp()
            self.out(f"written:   {self.wp_conf} (LDAC {c.quality})\n")
        self.apply_le()

    def apply_bearer(self) -> None:
        """The preferred bearer, when apply sets one."""
        c = self.c
        if self.bearer_state == "set":
            self.out("== preferred bearer\n")
            cmd = ["bluetoothctl", "bearer", c.device, c.bearer]
            if self.m.bt_timeout > 0:
                cmd = ["timeout", str(self.m.bt_timeout), *cmd]
            said = self.run(cmd, output="merge").stdout.rstrip("\n")
            if bearer_matches(self.bt_info(), c.bearer):
                self.out(
                    f"set:       PreferredBearer = {c.bearer} (for connections the PC starts; disconnect and reconnect)\n"
                )
            else:
                self.err(f"WARNING: could not set the bearer: {said or 'no output'}\n")
        elif self.bearer_state == "unpaired":
            self.err(f"WARNING: {c.device} is not paired; bearer not changed\n")
        elif self.bearer_state == "noheadset":
            self.err(f"WARNING: BEARER={c.bearer} is for a headset, and none is set; bearer not changed\n")

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
            if self.le_state == "ok":
                self.out("unchanged: LE Audio off\n")
                return
            f.make_dirs(self.p.wp_dir)
            f.write_user_file(le, render_le_off(), 0o644)
            if self.le_state == "adopt":
                self.out(f"adopted:   {le} (comments renewed, no restart)\n")
            else:
                self.out(f"written:   {le} (LE Audio off: PipeWire offers only A2DP and HFP)\n")
        elif self.le_state == "remove":
            f.remove_file(le)
            self.out(f"removed:   {le} (LE Audio on)\n")
        elif self.le_state == "absent":
            self.out("unchanged: LE Audio on (no drop-in)\n")
        elif self.le_state == "conflict":
            self.err(f"WARNING: {le} was not written by CST: left alone (it may keep LE Audio off)\n")

    def apply_watcher(self) -> None:
        self.out(f"== LDAC rate watcher: {LDAC_UNIT}\n")
        if self.lw_want:
            self.lw_install()
        else:
            self.lw_remove()

    def lw_install(self) -> None:
        """The watcher's program and unit (through the installer, as the user), enabled and started."""
        p, unit, changed = self.p, LDAC_UNIT, False
        if self.lw_prog_st != "ok":
            self.must(["python3", "-I", "-c", "import ast, sys; ast.parse(open(sys.argv[1]).read())", p.lw_src])
            r = self.installer("install", "--home", p.home, "bluetooth/bt-ldac-watch")
            if r.returncode != 0:
                raise StepFailed(shell_exit_status(r.returncode))
            changed = True
        if self.lw_unit_st != "ok":
            said = self.checked(
                ["text", "--home", p.home, p.lw_unit_file, "0644", "--component", "bluetooth:ldac"],
                input_text=render_unit(self.c.device, self.c.ldac_rate),
            )
            self.out(f"{said.rstrip(chr(10))} (LDAC to {self.c.ldac_rate} Hz)\n")
            changed = True
            self.must(["systemctl", "--user", "daemon-reload"])
        if self.lw_enabled != "enabled":
            self.must(["systemctl", "--user", "enable", unit])
            self.out(f"enabled:   {unit} (starts with your session)\n")
        if self.lw_active != "active":
            self.must(["systemctl", "--user", "start", unit])
            self.out(f"started:   {unit} (logs: journalctl --user -u {unit})\n")
        elif changed:
            self.must(["systemctl", "--user", "restart", unit])
            self.out(f"restarted: {unit} (new files)\n")
        else:
            self.out(f"unchanged: {unit} enabled and active\n")
        self.lw_active = self.quiet(["systemctl", "--user", "is-active", unit])
        r = self.installer("mark", "--home", p.home, "--component", "bluetooth:ldac", "done")
        if r.returncode != 0:
            raise StepFailed(shell_exit_status(r.returncode))

    def lw_remove(self) -> None:
        """The watcher stopped, disabled and removed, if CST installed it (with --check: only said)."""
        p, unit = self.p, LDAC_UNIT
        if not self.lw_ours_present():
            self.out("unchanged: not installed\n")
            if self.lw_unit_st == "conflict":
                self.err(f"WARNING: {p.lw_unit_file} was not written by CST: left alone\n")
            if self.lw_prog_st == "conflict":
                self.err(f"WARNING: {p.lw_prog} was not written by CST: left alone\n")
            return
        if self.o.check:
            self.out(f"would stop, disable and remove {unit} and {p.lw_prog}\n")
            return
        if self.lw_unit_st in ("ok", "change"):
            self.out(self.run(["systemctl", "--user", "disable", "--now", unit], output="stdout").stdout)
            self.checked(["remove-text", "--home", p.home, p.lw_unit_file])
            self.out(f"removed:   {p.lw_unit_file} (stopped and disabled)\n")
            self.must(["systemctl", "--user", "daemon-reload"])
        if self.lw_prog_st in ("ok", "change"):
            self.checked(["remove", "--home", p.home, "bluetooth/bt-ldac-watch"])
            self.m.files.remove_empty_dir(os.path.dirname(p.lw_prog))
            self.out(f"removed:   {p.lw_prog}\n")
        self.lw_active = ""

    # ---- revert ----------------------------------------------------------------------------
    def oldest_backup(self) -> str | None:
        """The oldest main.conf backup by time (ours or the old script's): the state before any of this."""
        found = sorted((int(mtime), name) for mtime, name in self.m.files.backups(self.p.main_conf))
        return found[0][1] if found else None

    def revert(self) -> int:
        p, f, check = self.p, self.m.files, self.o.check
        if not check:
            self.preconditions()
        mc = p.main_conf
        if not (f.is_file(mc) and not f.is_symlink(mc)):
            raise self.die(f"{mc} is missing or a symlink")
        changed_mc = self.revert_main_conf()
        self.save_headset()  # the rule goes next: it may be the only place that names the headset
        wp, le = self.revert_files()
        self.out(f"== LDAC rate watcher: {LDAC_UNIT}\n")
        self.lw_plan()
        lw = self.lw_ours_present()
        self.lw_remove()
        if check:
            if changed_mc:
                self.out("would restart bluetooth.service (after asking) and wireplumber\n")
            if not changed_mc and (wp or le):
                self.out("would restart wireplumber\n")
            return 10 if changed_mc or wp or lw or le else 0
        self.out("== restarts\n")
        if not (changed_mc or wp or le):
            self.out("none needed\n")
            return 0
        if self.do_restarts(bt=changed_mc, wp=True) == 2:
            raise self.die(f"a restart failed (see above). Backup of main.conf: {self.backup or 'none taken'}")
        return 0

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

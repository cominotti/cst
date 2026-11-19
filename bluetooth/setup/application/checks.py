# SPDX-License-Identifier: MPL-2.0
"""The live checks of `cst bluetooth status` (and the end of apply): the services, LE Audio,
the adapter, PipeWire's BAP endpoint, the bearer and the headset in PipeWire."""

from bluetooth.domain import ISO_UUID, LDAC_UNIT, RolesState, parse_roles_state, pw_state
from bluetooth.setup.application.survey import Survey
from bluetooth.setup.domain import BapState, LeState, WpState, preferred_bearer
from cst_core.domain.shapes import parse_json
from cst_core.ports import OutputMode


class Checks(Survey):
    """Survey and the checks; their counters are in Survey (fails, warns)."""

    # ---- the live checks (status, and the end of apply) --------------------------------------
    def wp_roles(self) -> str | None:
        """STATE|VALUE of bluez5.roles as WirePlumber resolves its config (bt-helper wp-roles, on the host)."""
        r = self.run(
            ["env", f"XDG_CONFIG_HOME={self.p.config}", "python3", "-I", "-B", self.p.helper, "wp-roles"],
            host=True,
            output=OutputMode.STDOUT,
        )
        return r.stdout.rstrip("\n") if r.returncode == 0 else None

    def le_roles_check(self) -> None:
        label = "WirePlumber bluez5.roles"
        r = self.wp_roles()
        if r is None:
            self.check_warn(label, "could not be read (libwireplumber-0.5 not found on the host)")
            return
        st, _, val = r.split("\n", 1)[0].partition("|")
        match self.c.le_audio, parse_roles_state(st):
            case "off", RolesState.CLASSIC:
                self.check_ok(label, f"{val} (no LE Audio roles)")
            case "off", RolesState.LE:
                self.check_bad(
                    label,
                    f"{val}: LE Audio roles in it although LE Audio is off; another WirePlumber file adds them (see the files above)",
                )
            case "off", RolesState.DEFAULT:
                if self.le_off_active():
                    self.check_bad(
                        label,
                        "not set: WirePlumber does not use the CST file, so PipeWire's defaults (with LE Audio) apply",
                    )
                else:
                    self.item(
                        "note",
                        label,
                        f"not set yet (PipeWire's defaults, with LE Audio): {self.c.prog} apply writes the CST file",
                    )
            case "on", RolesState.CLASSIC:
                self.check_warn(
                    label,
                    f"{val}: no LE Audio roles although LE Audio is on (a WirePlumber file other than CST's sets them)",
                )
            case _, None:
                self.check_warn(label, f"bt-helper wp-roles answered {st!r}, not a roles state")
            case _:
                self.check_ok(label, f"{val or 'not set: the PipeWire defaults'} (LE Audio roles included)")

    def verify(self) -> None:
        """The live checks: services, LE Audio, the adapter, the BAP endpoint, the bearer, the headset."""
        self.services_check()
        self.le_audio_check()
        self.adapter_check()
        self.bap_check()
        if not self.headset:
            self.section("headset")
            self.item("skip", "headset", self.no_headset())
            return
        self.bearer_check(self.bt_info())
        self.headset_check()

    def services_check(self) -> None:
        prog, unit, rate = self.c.prog, LDAC_UNIT, self.c.ldac_rate
        self.section("services")
        if self.ok(["systemctl", "is-active", "--quiet", "bluetooth"], host=True):
            self.check_ok("bluetooth.service", "active")
        else:
            self.check_bad("bluetooth.service", "not active")
        if self.ok(["systemctl", "--user", "is-active", "--quiet", "wireplumber"], host=True):
            self.check_ok("wireplumber (your session)", "active")
        else:
            self.check_bad("wireplumber (your session)", "not active")
        if self.lw_want:
            if self.lw_active == "active":
                self.check_ok(unit, f"active: LDAC back to {rate} Hz when the headset sets up another rate")
            else:
                self.check_warn(
                    unit,
                    f"{self.lw_active or 'not running'} (LDAC_RATE={rate}): {prog} apply; logs: journalctl --user -u {unit}",
                )
        elif self.lw_active == "active":
            self.check_warn(unit, f"running although {self.lw_off_why()}: {prog} apply stops and removes it")
        if self.lw_want or self.lw_ours_present():
            self.lw_failure_check()

    def le_audio_check(self) -> None:
        prog, frm = self.c.prog, self.c.le_from
        self.section("LE Audio")
        if self.c.le_audio == "off":
            if self.le_state in (LeState.OK, LeState.ADOPT):
                self.check_ok("LE Audio", f"off ({frm}; PipeWire offers only classic profiles)")
            else:
                self.check_warn(
                    "LE Audio", f"off in the settings ({frm}), but its drop-in is not in place yet: {prog} apply"
                )
            self.item("", "", f"turn on with: {self.le_on_hint}")
        else:
            self.check_ok("LE Audio", f"on ({frm})")
            self.item("", "", f"turn off with: {self.le_off_hint}")
        self.le_roles_check()

    def adapter_check(self) -> None:
        adapter, _ = self.adapter()
        if not adapter:
            self.check_bad("adapter", "no Bluetooth adapter in org.bluez")
        else:
            feats = self.quiet(
                ["busctl", "get-property", "org.bluez", adapter, "org.bluez.Adapter1", "ExperimentalFeatures"],
                host=True,
            )
            if ISO_UUID in feats.lower():
                self.check_ok("adapter ISO feature", f"on for {adapter}")
            else:
                self.check_bad(
                    "adapter ISO feature",
                    f"{ISO_UUID} not on for {adapter} (KernelExperimental not in effect? restart bluetooth)",
                )
            # AF_BLUETOOTH=31, SOCK_SEQPACKET=5, BTPROTO_ISO=8: global, until reboot (hence the adapter check).
            probe = ["/usr/bin/python3", "-I", "-c", "import socket; socket.socket(31, 5, 8).close()"]
            if self.run(probe, host=True, output=OutputMode.STDOUT).returncode == 0:
                self.check_ok("kernel ISO socket", "opens")
            else:
                self.check_bad("kernel ISO socket", "not available")
            uuids = self.quiet(
                ["busctl", "get-property", "org.bluez", adapter, "org.bluez.Media1", "SupportedUUIDs"], host=True
            ).lower()
            if "00002bc9" in uuids and "00002bcb" in uuids:
                self.check_ok("BAP in BlueZ", "Sink and Source PAC in Media1")
            else:
                self.check_bad("BAP in BlueZ", "not in Media1 (Experimental not in effect? restart bluetooth)")

    def bap_check(self) -> None:
        prog = self.c.prog
        bap = self.bap_endpoint_state()
        label = "PipeWire BAP LC3 endpoint"
        unknown = "not confirmed (no bluetoothd or WirePlumber found)"
        if self.c.le_audio == "off":
            match bap:
                case BapState.REGISTERED:
                    if self.le_off_active():
                        self.check_warn(
                            label,
                            "still registered: LE Audio off takes effect after systemctl --user restart wireplumber",
                        )
                    else:
                        self.check_warn(label, f"still registered: LE Audio off takes effect after {prog} apply")
                case BapState.UNREGISTERED | BapState.NO_RECORD:
                    self.check_ok(label, "not registered (as wanted with LE Audio off)")
                case _:
                    self.check_warn(label, unknown)
            return
        match bap:
            case BapState.REGISTERED:
                self.check_ok(label, "registered by your WirePlumber with the running bluetoothd")
            case BapState.NO_RECORD:
                self.check_warn(label, "not confirmed: the journal no longer has your WirePlumber's registration")
            case BapState.UNREGISTERED:
                self.check_warn(label, "your WirePlumber removed it (journalctl -u bluetooth | grep BAPSink)")
            case _:
                self.check_warn(label, unknown)

    def headset_check(self) -> None:
        unit, rate = LDAC_UNIT, self.c.ldac_rate
        self.section(f"headset {self.c.device} in PipeWire")
        dump = self.quiet(["timeout", "10", "pw-dump"], host=True)
        if not dump:
            self.check_warn("pw-dump", "no answer: PipeWire stuck? (systemctl --user restart pipewire wireplumber)")
            return
        try:
            st = pw_state(self.c.device_id, parse_json(dump + "\n"))
        except ValueError:
            self.check_warn("pw-dump", "could not read its output (rc=3)")
            return
        except TypeError, AttributeError, KeyError, IndexError:
            self.check_warn("pw-dump", "could not read its output (rc=1)")
            return
        if st is None:
            self.check_warn("headset", "not in PipeWire now (connect it to check LDAC/BAP)")
            return
        prof, rule, codec, quality, node_rate = (x or d for x, d in zip(st, ("?", "?", "-", "-", "-"), strict=False))
        self.check_ok(
            "headset",
            f"profile {prof}, rule ldac.quality={rule}, node codec {codec}, quality {quality}, rate {node_rate} Hz",
        )
        q = self.c.quality
        if self.c.le_audio == "off" and prof.startswith("bap"):
            self.check_warn(
                "headset profile",
                f"{prof} although LE Audio is off: restart WirePlumber (systemctl --user restart wireplumber)",
            )
        if rule != q:
            self.check_warn("LDAC rule", f"not loaded on the device (want {q}; reconnect the headset)")
        if codec == "ldac" and quality != q:
            self.check_warn("LDAC quality", f"LDAC is on at {quality} (want {q}; reconnect the headset)")
        if codec == "ldac" and node_rate.isascii() and node_rate.isdigit() and int(node_rate) > 48000:
            if self.lw_want and self.lw_active == "active":
                self.check_warn(
                    "LDAC sample rate",
                    f"{node_rate} Hz: the watcher switches it to {rate} within seconds, unless it gave up (journalctl --user -u {unit})",
                )
            elif self.lw_want:
                self.check_warn(
                    "LDAC sample rate",
                    f"{node_rate} Hz, set up by the headset; the watcher (LDAC_RATE={rate}) is not running",
                )
            else:
                self.check_warn(
                    "LDAC sample rate",
                    f"{node_rate} Hz, set up by the headset: fewer bits per sample at the same bitrate; LDAC_RATE=48000 turns on the watcher (see bluetooth/README.md)",
                )

    def bearer_check(self, info: str) -> None:
        if self.c.le_audio != "off" or preferred_bearer(info) != "le":
            return
        self.check_warn(
            "preferred bearer",
            f"{self.c.device} prefers LE while LE Audio is off: {self.c.prog} apply --bearer bredr (or BEARER=bredr in the settings)",
        )

    def lw_failure_check(self) -> None:
        if not self.lw_last_failure():
            return
        self.check_warn("LDAC rate watcher", self.lw_failure_text())
        if self.user_sets("LDAC_RATE"):
            self.item(
                "",
                "",
                f"to turn it off: remove LDAC_RATE from {self.tilde(self.p.user_settings)}, then {self.c.prog} apply",
            )
        else:
            self.item("", "", f"to turn it off: make LDAC_RATE empty in the settings, then {self.c.prog} apply")

    def status(self) -> int:
        """status: the files, then the live checks. 1: a check failed."""
        self.compute_plan()
        self.header()
        self.files_report()
        self.verify()
        self.out("\n")
        if self.owed:
            self.out(
                f"A restart an earlier run did not make is still owed ({self.owed.replace(chr(10), ' ')}): {self.c.prog} apply makes it.\n"
            )
        if self.fails:
            self.out(f"{self.fails} check(s) failed, {self.warns} warning(s).\n")
            return 1
        self.out(f"All checks passed{f', {self.warns} warning(s)' if self.warns else ''}.\n")
        if (
            self.mc_changed
            or self.legacy
            or self.lw_change
            or self.le_change
            or self.device_save
            or self.wp_state not in (WpState.OK, WpState.ADOPT, WpState.NONE)
            or self.le_state == LeState.CONFLICT
        ):
            self.out(f"Some files or services differ from what apply sets up: {self.c.prog} plan\n")
        elif self.wp_state == WpState.ADOPT or self.le_state == LeState.ADOPT:
            self.out(f"The WirePlumber rule has older comments: {self.c.prog} apply renews them (no restart).\n")
        return 0

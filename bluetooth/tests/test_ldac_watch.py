"""Unit tests for bluetooth/bt-ldac-watch (the LDAC sample rate watcher):
parsing of pw-dump, pactl events and pactl cards, the act/skip decision, the
one-attempt-per-connection state machine and its cooldown, the bounce with a
fake system; and for its user unit (bt-helper) and the install/remove steps
of bluetooth-setup.sh with systemctl as a stub. Nothing real is switched.

Run from the repository root:
    python3 -I -B -m unittest discover -s bluetooth/tests -t . -v
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

sys.dont_write_bytecode = True

from bluetooth.tests.test_bluetooth import HELPER, MAC, SETUP, WATCH, load, shell_functions  # noqa: E402

DEV = "38_D5_18_CD_AA_4C"


def pw_dump(rate=96000, codec="ldac", profile="a2dp-sink", card=500, out=600, mic_state="suspended",
            suspended=False):
    """A shortened pw-dump of the headset as found on sophia: the card, the
    A2DP output node and WirePlumber's headset source (named with ':')."""
    out_params = {"EnumFormat": [{"mediaType": "audio", "format": {"default": "F32LE"}, "rate": rate,
                                  "channels": 2}],
                  "Format": [] if suspended else [{"mediaType": "audio", "format": "F32LE", "rate": rate,
                                                    "channels": 2}],
                  "Props": [{"volume": 1.0}, {"quality": -1}]}
    return [
        {"id": 36, "type": "PipeWire:Interface:Device", "info": {
            "props": {"device.name": f"bluez_card.{DEV}", "object.serial": str(card),
                      "bluez5.a2dp.ldac.quality": "auto"},
            "params": {"Profile": [{"index": 1, "name": profile}]}}},
        {"id": 129, "type": "PipeWire:Interface:Node", "info": {
            "state": "running",
            "props": {"node.name": f"bluez_output.{DEV}.1", "object.serial": str(out),
                      "api.bluez5.codec": codec, "api.bluez5.profile": "a2dp-sink"},
            "params": out_params}},
        {"id": 48, "type": "PipeWire:Interface:Node", "info": {
            "state": mic_state,
            "props": {"node.name": f"bluez_input.{MAC}", "object.serial": "601"},
            "params": {"Format": []}}},
        {"id": 106, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "bluez_midi.server"}}},
        {"id": 1, "type": "PipeWire:Interface:Core", "info": {}},
    ]


def cards(active="a2dp-sink", sbc=True, lang_names=False):
    """pactl -f json list cards, the headset's card (profile names as PipeWire's)."""
    profs = {
        "off": {"description": "Off", "available": True},
        "a2dp-sink-sbc_xq": {"description": "High Fidelity Playback (A2DP Sink, codec SBC-XQ)", "available": True},
        "a2dp-sink-aac": {"description": "High Fidelity Playback (A2DP Sink, codec AAC)", "available": True},
        "a2dp-sink": {"description": "High Fidelity Playback (A2DP Sink, codec LDAC)", "available": True},
        "headset-head-unit": {"description": "Headset Head Unit (HSP/HFP, codec MSBC)", "available": True},
    }
    if sbc:
        profs["a2dp-sink-sbc"] = {"description": "High Fidelity Playback (A2DP Sink, codec SBC)", "available": True}
    if lang_names:      # descriptions in another language: only the names help
        for p in profs.values():
            p["description"] = "Reprodução"
    return [{"name": "alsa_card.x", "profiles": {}}, {"name": f"bluez_card.{DEV}", "index": 500,
                                                       "active_profile": active, "profiles": profs}]


class ParseTest(unittest.TestCase):
    def setUp(self):
        self.w = load(WATCH, "bt_ldac_watch")
        self.h = load()

    def test_snapshot(self):
        s = self.w.snapshot(DEV, pw_dump())
        self.assertEqual(s, {"card": 500, "profile": "a2dp-sink", "out": 600, "codec": "ldac", "rate": 96000,
                             "inputs": [601], "mic": False})
        self.assertTrue(self.w.snapshot(DEV, pw_dump(mic_state="running"))["mic"])
        self.assertIsNone(self.w.snapshot("00_00", pw_dump())["card"])

    def test_suspended_node_rate_from_enumformat(self):
        self.assertEqual(self.w.snapshot(DEV, pw_dump(rate=48000, suspended=True))["rate"], 48000)
        objs = pw_dump()
        objs[1]["info"]["params"] = {}
        self.assertIsNone(self.w.snapshot(DEV, objs)["rate"])

    def test_helper_reads_the_same_rate(self):
        for objs in (pw_dump(), pw_dump(rate=48000, suspended=True)):
            self.assertEqual(self.h.pw_state(DEV, objs)[4], str(self.w.snapshot(DEV, objs)["rate"]))

    def test_snapshot_cli(self):
        r = subprocess.run([sys.executable, "-I", WATCH, "--device", MAC, "--snapshot"],
                           input=json.dumps(pw_dump()), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("act: LDAC at 96000 Hz (want 48000)", r.stdout)
        r = subprocess.run([sys.executable, "-I", WATCH, "--device", "nope", "--snapshot"],
                           input="[]", capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)

    def test_decide(self):
        d = lambda **kw: self.w.decide(self.w.snapshot(DEV, pw_dump(**kw)), 48000)[0]  # noqa: E731
        self.assertEqual(d(), "act")
        self.assertEqual(d(rate=48000), "none")
        self.assertEqual(d(rate=44100), "act")
        self.assertEqual(d(codec="sbc", rate=44100), "none")
        self.assertEqual(d(mic_state="running"), "skip")
        self.assertEqual(d(profile="headset-head-unit"), "skip")
        self.assertEqual(self.w.decide(self.w.snapshot("00_00", pw_dump()), 48000)[0], "none")
        self.assertEqual(self.w.decide(None, 48000)[0], "none")

    def test_relevant_events(self):
        rel = self.w.relevant
        known = {500, 600, 601}
        self.assertTrue(rel("Event 'new' on card #900\n", known))
        self.assertTrue(rel("Event 'remove' on sink #12\n", known))
        self.assertTrue(rel("Event 'change' on sink #600\n", known))
        self.assertTrue(rel("Event 'change' on source #601\n", known))
        self.assertFalse(rel("Event 'change' on sink #7\n", known))
        self.assertFalse(rel("Event 'new' on sink-input #600\n", known))
        self.assertFalse(rel("Event 'change' on client #500\n", known))
        self.assertFalse(rel("Event 'new' on source #5\n", known))
        self.assertFalse(rel("garbage\n", known))

    def test_profiles(self):
        p = self.w.profiles
        self.assertEqual(p(DEV, cards()), ("a2dp-sink", "a2dp-sink", "a2dp-sink-sbc"))
        self.assertEqual(p(DEV, cards(lang_names=True)), ("a2dp-sink", "a2dp-sink", "a2dp-sink-sbc"))
        # No SBC: another A2DP codec does the same job.
        self.assertEqual(p(DEV, cards(sbc=False))[2], "a2dp-sink-aac")
        self.assertEqual(p(DEV, cards(sbc=False, lang_names=True))[2], None)
        c = cards()
        c[1]["profiles"]["a2dp-sink-sbc"]["available"] = False
        self.assertEqual(p(DEV, c)[2], "a2dp-sink-aac")
        self.assertEqual(p("00_00", cards()), (None, None, None))


class TrackerTest(unittest.TestCase):
    def setUp(self):
        self.w = load(WATCH, "bt_ldac_watch")
        self.t = self.w.Tracker(48000, settle=5, cooldown=600)

    def snap(self, **kw):
        return self.w.snapshot(DEV, pw_dump(**kw))

    def test_settle_then_act_once(self):
        s = self.snap()
        self.assertEqual(self.t.observe(s, 0)[0], "wait")
        what, left = self.t.observe(s, 3)
        self.assertEqual((what, left), ("wait", 2))
        self.assertEqual(self.t.observe(s, 5)[0], "act")
        self.t.attempted(5)
        self.t.own_node(self.snap(out=700, rate=48000))
        self.t.result(True)
        self.assertEqual(self.t.observe(self.snap(out=700, rate=48000), 10)[0], "none")
        # The headset sets 96 kHz again on the same connection: no second try.
        self.assertEqual(self.t.observe(self.snap(out=700), 20)[0], "done")

    def test_good_rate_in_between_restarts_the_settle_time(self):
        self.t.observe(self.snap(), 0)
        self.t.observe(self.snap(rate=48000), 3)
        self.assertEqual(self.t.observe(self.snap(), 6), ("wait", 5))

    def test_new_connection_allows_another_attempt_after_the_cooldown(self):
        self.t.observe(self.snap(), 0)
        self.assertEqual(self.t.observe(self.snap(), 5)[0], "act")
        self.t.attempted(5)
        self.t.result(True)
        # Disconnected, then connected again (a new card object) at 96 kHz.
        self.assertEqual(self.t.observe(None, 100)[0], "none")
        what, left = self.t.observe(self.snap(card=501, out=610), 200)
        self.assertEqual(what, "cooldown")
        self.assertAlmostEqual(left, 405)
        self.assertEqual(self.t.observe(self.snap(card=501, out=610), 605)[0], "wait")
        self.assertEqual(self.t.observe(self.snap(card=501, out=610), 610)[0], "act")

    def test_failed_attempt_gives_up_until_reconnect(self):
        self.t.observe(self.snap(), 0)
        self.t.observe(self.snap(), 5)
        self.t.attempted(5)
        self.t.own_node(self.snap(out=700))
        self.t.result(False)
        self.assertEqual(self.t.observe(self.snap(out=700), 1000)[0], "done")
        # Even a replaced node (the user switched profiles) does not retry.
        self.assertEqual(self.t.observe(self.snap(out=800), 2000)[0], "done")
        self.t.observe(self.snap(card=501, out=900), 3000)
        self.assertEqual(self.t.observe(self.snap(card=501, out=900), 3005)[0], "act")

    def test_node_replaced_by_someone_else_resets_the_attempt(self):
        self.t.observe(self.snap(), 0)
        self.t.observe(self.snap(), 5)
        self.t.attempted(5)
        self.t.own_node(self.snap(out=700, rate=48000))
        self.t.result(True)
        self.t.observe(self.snap(out=700, rate=48000), 10)
        # HFP for a call, then back: new nodes, the headset at 96 kHz again.
        self.t.observe(self.snap(out=750, codec="msbc", rate=16000, profile="headset-head-unit"), 100)
        self.assertEqual(self.t.observe(self.snap(out=760), 700)[0], "wait")
        self.assertEqual(self.t.observe(self.snap(out=760), 705)[0], "act")

    def test_cooldown_on_the_same_connection_after_reset(self):
        self.t.observe(self.snap(), 0)
        self.t.observe(self.snap(), 5)
        self.t.attempted(5)
        self.t.own_node(self.snap(out=700, rate=48000))
        self.t.result(True)
        self.t.observe(self.snap(out=705, codec="msbc", rate=16000, profile="headset-head-unit"), 200)
        self.assertEqual(self.t.observe(self.snap(out=710), 400)[0], "cooldown")

    def test_fix_that_does_not_hold_gives_up(self):
        # The fix worked, then the headset set 96 kHz up again by itself a few
        # seconds later (a new node): no bounce every cooldown.
        self.t.observe(self.snap(), 0)
        self.t.observe(self.snap(), 5)
        self.t.attempted(5)
        self.t.own_node(self.snap(out=700, rate=48000))
        self.t.result(True)
        self.assertEqual(self.t.observe(self.snap(out=700, rate=48000), 8)[0], "none")
        what, why = self.t.observe(self.snap(out=710), 12)
        self.assertEqual(what, "done")
        self.assertIn("by itself", why)
        for now in (700, 1400, 5000):
            self.assertEqual(self.t.observe(self.snap(out=710), now)[0], "done")
        # Also when the node went away in between (a look in that moment).
        self.assertEqual(self.t.observe(self.snap(out=720), 6000)[0], "done")
        # A new connection starts afresh.
        self.t.observe(self.snap(card=501, out=900), 7000)
        self.assertEqual(self.t.observe(self.snap(card=501, out=900), 7005)[0], "act")

    def test_relapse_through_a_moment_without_a_node(self):
        self.t.observe(self.snap(), 0)
        self.t.observe(self.snap(), 5)
        self.t.attempted(5)
        self.t.own_node(self.snap(out=700, rate=48000))
        self.t.result(True)
        objs = [o for o in pw_dump() if "bluez_output" not in str(o)]
        self.assertEqual(self.t.observe(self.w.snapshot(DEV, objs), 10)[0], "none")
        self.assertEqual(self.t.observe(self.snap(out=710), 12)[0], "done")
        self.assertEqual(self.t.observe(self.snap(out=710), 1000)[0], "done")

    def test_at_most_two_attempts_per_connection(self):
        t = self.t
        now = 0
        for n in range(2):
            out = 700 + 100 * n
            t.observe(self.snap(out=out), now)
            self.assertEqual(t.observe(self.snap(out=out), now + 5)[0], "act", n)
            t.attempted(now + 5)
            t.own_node(self.snap(out=out + 1, rate=48000))
            t.result(True)
            # A call (HFP) much later, then back at 96 kHz on a new node.
            t.observe(self.snap(out=out + 2, codec="msbc", rate=16000, profile="headset-head-unit"), now + 1000)
            now += 2000
        what, why = t.observe(self.snap(out=999), now)
        self.assertEqual(what, "done")
        self.assertIn("2 attempts", why)

    def test_skip_while_the_microphone_is_in_use(self):
        self.t.observe(self.snap(mic_state="running"), 0)
        self.assertEqual(self.t.observe(self.snap(mic_state="running"), 10)[0], "skip")
        self.assertEqual(self.t.observe(self.snap(), 11)[0], "wait")
        self.assertEqual(self.t.observe(self.snap(), 16)[0], "act")


class FakeLive:
    """The system as the watcher sees it: switching to the bounce profile and
    back to LDAC makes the PC configure the stream at BACK_RATE."""

    def __init__(self, mod, back_rate=48000, fail=()):
        self.mod = mod
        self.dev = DEV
        self.active = "a2dp-sink"
        self.rate = 96000
        self.out = 600
        self.back_rate = back_rate
        self.fail = set(fail)
        self.calls = []

    def snap(self):
        codec = "ldac" if self.active == "a2dp-sink" else "sbc"
        return self.mod.snapshot(DEV, pw_dump(rate=self.rate, codec=codec, profile=self.active, out=self.out))

    def cards(self):
        return cards(active=self.active)

    def set_profile(self, prof):
        self.calls.append(prof)
        if prof in self.fail:
            return False
        self.active = prof
        self.out += 1
        self.rate = self.back_rate if prof == "a2dp-sink" else 44100
        return True


class BounceTest(unittest.TestCase):
    def setUp(self):
        self.w = load(WATCH, "bt_ldac_watch")
        # No waiting: sleeps do nothing, the wait limits are zero.
        clock = types.SimpleNamespace(t=0.0)
        self.w.time = types.SimpleNamespace(monotonic=lambda: clock.t,
                                            sleep=lambda s: setattr(clock, "t", clock.t + s))
        self.w.SBC_HOLD = 0
        self.lines = []
        self.w.log = lambda msg, level=6: self.lines.append((level, msg))

    def watcher(self, live, dry=False):
        w = self.w.Watcher(DEV, 48000, dry_run=dry)
        w.live = live
        w.tracker = self.w.Tracker(48000, settle=0, cooldown=600)
        return w

    def test_bounce_fixes_the_rate(self):
        live = FakeLive(self.w)
        w = self.watcher(live)
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink"])
        self.assertIn((6, "fixed: LDAC now at 48000 Hz"), self.lines)
        self.assertIn((6, "act: LDAC at 96000 Hz (want 48000): switching a2dp-sink -> a2dp-sink-sbc -> "
                          "a2dp-sink (about a second of silence)"), self.lines)
        # The new node is ours: nothing more to do, and no second attempt.
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink"])

    def test_headset_insists_on_96k(self):
        live = FakeLive(self.w, back_rate=96000)
        w = self.watcher(live)
        w.look()
        self.assertTrue(any(lv == 3 and "came back at 96000 Hz" in m for lv, m in self.lines))
        for _ in range(3):
            w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink"])

    def test_switch_back_fails(self):
        live = FakeLive(self.w, fail={"a2dp-sink"})
        w = self.watcher(live)
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink", "a2dp-sink"])
        self.assertTrue(any(lv == 3 and "could not switch back" in m for lv, m in self.lines))

    def test_dry_run_switches_nothing(self):
        live = FakeLive(self.w)
        w = self.watcher(live, dry=True)
        w.look()
        w.look()
        self.assertEqual(live.calls, [])
        self.assertTrue(any("would switch a2dp-sink -> a2dp-sink-sbc -> a2dp-sink" in m for _, m in self.lines))

    def test_profile_changed_during_the_bounce_is_left_alone(self):
        # WirePlumber switches to HFP (an app opened the microphone) while
        # the card is on SBC: the bounce does not force LDAC back.
        live = FakeLive(self.w)
        real = live.set_profile

        def set_profile(prof):
            ok = real(prof)
            if prof == "a2dp-sink-sbc":
                live.active = "headset-head-unit"
            return ok
        live.set_profile = set_profile
        w = self.watcher(live)
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc"])
        self.assertTrue(any(lv == 3 and "went to headset-head-unit" in m for lv, m in self.lines))
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc"])

    def test_microphone_in_use_during_the_bounce(self):
        live = FakeLive(self.w)
        snap = live.snap

        def snap_mic():
            s = snap()
            if live.active == "a2dp-sink-sbc":
                s["mic"] = True
            return s
        live.snap = snap_mic
        w = self.watcher(live)
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc"])
        self.assertTrue(any(lv == 3 and "microphone came into use" in m and "by hand" in m
                            for lv, m in self.lines))

    def test_alt_never_active_still_switches_back(self):
        live = FakeLive(self.w)
        real = live.set_profile

        def set_profile(prof):
            if prof == "a2dp-sink":
                return real(prof)
            live.calls.append(prof)
            return True             # pactl says yes, the card stays on LDAC
        live.set_profile = set_profile
        w = self.watcher(live)
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink"])
        self.assertIn((6, "fixed: LDAC now at 48000 Hz"), self.lines)

    def test_stop_signal_waits_for_the_bounce(self):
        live = FakeLive(self.w)
        real = live.set_profile

        def set_profile(prof):
            if prof == "a2dp-sink-sbc":
                self.w.Stop.handler(15, None)     # SIGTERM while switching
            return real(prof)
        live.set_profile = set_profile
        w = self.watcher(live)
        self.addCleanup(setattr, self.w.Stop, "asked", False)
        with self.assertRaises(SystemExit):
            w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink"])
        self.assertEqual(live.active, "a2dp-sink")
        self.assertTrue(any("stop asked during the profile switch" in m for _, m in self.lines))
        # Outside a bounce, a stop is immediate.
        self.w.Stop.asked = False
        with self.assertRaises(SystemExit):
            self.w.Stop.handler(15, None)

    def test_no_pw_dump_is_not_a_disconnection(self):
        live = FakeLive(self.w, back_rate=96000)
        w = self.watcher(live)
        w.look()
        n = len(live.calls)
        live.snap_saved = live.snap
        live.snap = lambda: None
        w.look()
        live.snap = live.snap_saved
        w.look()
        self.assertEqual(len(live.calls), n)


class ListenTest(unittest.TestCase):
    """listen(): a look at the start, one more after a relevant burst (also
    when it is not the first line of a read), back on end of file."""

    def test_events_from_a_pipe(self):
        w = load(WATCH, "bt_ldac_watch")
        w.DEBOUNCE = 0
        watcher = w.Watcher(DEV, 48000)
        looks = []
        watcher.look = lambda: looks.append(1)
        watcher.known = {600}
        r, wfd = os.pipe()
        os.write(wfd, b"Event 'change' on client #1\nEvent 'change' on sink #600\nEvent 'new' on sink-input #9\n")
        os.close(wfd)
        with os.fdopen(r, "rb", buffering=0) as f:
            watcher.listen(types.SimpleNamespace(stdout=f))
        # The start, then one for the burst (its relevant line is the second).
        self.assertEqual(len(looks), 2)
        looks.clear()
        r, wfd = os.pipe()
        os.write(wfd, b"Event 'change' on client #1\nEvent 'new' on sink-input #9\n")
        os.close(wfd)
        with os.fdopen(r, "rb", buffering=0) as f:
            watcher.listen(types.SimpleNamespace(stdout=f))
        self.assertEqual(len(looks), 1)     # nothing relevant: only the start

    def test_relevant_burst_triggers_a_look(self):
        w = load(WATCH, "bt_ldac_watch")
        w.DEBOUNCE = 0
        watcher = w.Watcher(DEV, 48000)
        looks = []
        r, wfd = os.pipe()

        def look():
            looks.append(1)
            if len(looks) == 1:
                os.write(wfd, b"Event 'change' on client #1\nEvent 'change' on sink #600\n")
            else:
                os.close(wfd)
        watcher.look = look
        watcher.known = {600}
        with os.fdopen(r, "rb", buffering=0) as f:
            watcher.listen(types.SimpleNamespace(stdout=f))
        self.assertEqual(len(looks), 2)


class UnitTest(unittest.TestCase):
    def setUp(self):
        self.h = load()

    def test_render_and_state(self):
        u = self.h.render_unit(MAC.lower(), "48000")
        self.assertIn(f"ExecStart=/usr/bin/python3 -I %h/.local/lib/cst/bt-ldac-watch --device {MAC} --rate 48000", u)
        self.assertIn("WantedBy=default.target", u)
        self.assertIn("KillMode=mixed", u)
        self.assertTrue(u.startswith(self.h.MARKER))
        self.assertEqual(self.h.unit_state(None, u), "missing")
        self.assertEqual(self.h.unit_state(u, u), "ok")
        self.assertEqual(self.h.unit_state(u.replace("48000", "44100"), u), "change")
        self.assertEqual(self.h.unit_state("[Service]\nExecStart=/bin/true\n", u), "conflict")

    def test_watcher_carries_the_marker(self):
        with open(WATCH) as f:
            self.assertIn(self.h.MARKER, f.read())


class InstallTest(unittest.TestCase):
    """lw_plan, lw_install and lw_remove of bluetooth-setup.sh in a scratch
    HOME, with systemctl as a stub that keeps the state in files."""

    STUBS = """
set -Eeuo pipefail
PROG=test; CHECK=0
SRC="$REPO/bluetooth"
LW_UNIT="cst-bt-ldac48.service"
LW_SRC="${SRC}/bt-ldac-watch"
LW_PROG="${HOME}/.local/lib/cst/bt-ldac-watch"
LW_UNIT_FILE="${HOME}/.config/systemd/user/${LW_UNIT}"
WP_MARKER="# managed-by: bluetooth-setup.sh (CST)"
DEVICE_MAC=38:D5:18:CD:AA:4C
helper() { python3 -I "$REPO/bluetooth/bt-helper" "$@"; }
hostcmd() { "$@"; }
item() { echo "item $1 $2"; }
cmd="${CMD:-apply}"
USER_SETTINGS="${HOME}/.config/cst/bluetooth.conf"
user_sets() { [[ " ${USER_KEYS:-} " == *" $1 "* ]]; }
journalctl() { [[ -n "${JOURNAL:-}" ]] && cat "$JOURNAL"; return 0; }
systemctl() {
    echo "systemctl $*" >>"$LOG"
    case "$2" in
        is-enabled) [[ -e "$HOME/enabled" ]] && { echo enabled; return 0; }; echo disabled; return 1 ;;
        is-active) [[ -e "$HOME/active" ]] && { echo active; return 0; }; echo inactive; return 3 ;;
        enable) touch "$HOME/enabled" ;;
        start|restart) touch "$HOME/active" ;;
        disable) rm -f "$HOME/enabled" "$HOME/active" ;;
    esac
}
"""

    def run_lw(self, body, rate="48000", home=None, extra_env=None):
        home = home or tempfile.mkdtemp(prefix="bt-test-")
        if not hasattr(self, "_homes"):
            self._homes = set()
        if home not in self._homes:
            self._homes.add(home)
            self.addCleanup(shutil.rmtree, home)
        log = os.path.join(home, "log")
        if os.path.exists(log):
            os.remove(log)
        script = (self.STUBS + f'LDAC_RATE="{rate}"\n' +
                  shell_functions("lw_plan", "lw_ours_present", "lw_unit_label", "lw_prog_label",
                                  "lw_report", "lw_install", "lw_remove", "lw_last_failure") + body)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "LOG": log,
               "REPO": os.path.dirname(os.path.dirname(WATCH)), **(extra_env or {})}
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
        calls = []
        if os.path.exists(log):
            with open(log) as f:
                calls = [c for c in f.read().splitlines() if "is-" not in c]
        return r, calls, home

    def test_install_then_nothing_then_remove(self):
        r, calls, home = self.run_lw('lw_plan; echo "change=$LW_CHANGE"; lw_install\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("change=1", r.stdout)
        self.assertEqual(calls, ["systemctl --user daemon-reload", "systemctl --user enable cst-bt-ldac48.service",
                                 "systemctl --user start cst-bt-ldac48.service"])
        prog = os.path.join(home, ".local/lib/cst/bt-ldac-watch")
        self.assertTrue(os.access(prog, os.X_OK))
        with open(os.path.join(home, ".config/systemd/user/cst-bt-ldac48.service")) as f:
            self.assertIn("--rate 48000", f.read())

        r, calls, _ = self.run_lw('lw_plan; echo "change=$LW_CHANGE"; lw_report; lw_install\n', home=home)
        self.assertIn("change=0", r.stdout)
        self.assertIn("item ok cst-bt-ldac48.service (your session)", r.stdout)
        self.assertIn("unchanged: cst-bt-ldac48.service enabled and active", r.stdout)
        self.assertEqual(calls, [])

        # A newer program in CST: copied and the service restarted.
        with open(prog, "a") as f:
            f.write("# old\n")
        r, calls, _ = self.run_lw("lw_plan; lw_report; lw_install\n", home=home)
        self.assertIn("item update ~/.local/lib/cst/bt-ldac-watch", r.stdout)
        self.assertEqual(calls, ["systemctl --user restart cst-bt-ldac48.service"])

        # LDAC_RATE emptied: stopped, disabled, removed.
        r, calls, _ = self.run_lw('lw_plan; echo "change=$LW_CHANGE"; lw_report; lw_remove\n', rate="", home=home)
        self.assertIn("change=1", r.stdout)
        self.assertIn("item remove LDAC rate watcher", r.stdout)
        self.assertEqual(calls, ["systemctl --user disable --now cst-bt-ldac48.service",
                                 "systemctl --user daemon-reload"])
        self.assertFalse(os.path.exists(prog))
        self.assertFalse(os.path.exists(os.path.dirname(prog)))
        self.assertFalse(os.path.exists(os.path.join(home, ".config/systemd/user/cst-bt-ldac48.service")))
        r, calls, _ = self.run_lw('lw_plan; echo "change=$LW_CHANGE"; lw_report\n', rate="", home=home)
        self.assertIn("change=0", r.stdout)
        self.assertIn("item ok LDAC rate watcher", r.stdout)

    def test_plan_names_the_user_file_and_the_last_failure(self):
        from bluetooth.tests.test_bluetooth import JOURNAL
        home = tempfile.mkdtemp(prefix="bt-test-")
        jf = os.path.join(home, "journal")
        with open(jf, "w") as f:
            f.write(JOURNAL)
        r, calls, _ = self.run_lw("lw_plan; lw_report\n", home=home,
                                  extra_env={"CMD": "plan", "USER_KEYS": "LDAC_RATE", "JOURNAL": jf})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("item note LDAC rate watcher", r.stdout)
        self.assertIn("item warn LDAC rate watcher", r.stdout)
        # The default (LDAC_RATE not in the user's file) and no failure in the log: no notes.
        r, _, _ = self.run_lw("lw_plan; lw_report\n", home=home, extra_env={"CMD": "plan"})
        self.assertNotIn("item note", r.stdout)
        self.assertNotIn("item warn", r.stdout)

    def test_foreign_unit_is_left_alone(self):
        home = tempfile.mkdtemp(prefix="bt-test-")
        os.makedirs(os.path.join(home, ".config/systemd/user"))
        with open(os.path.join(home, ".config/systemd/user/cst-bt-ldac48.service"), "w") as f:
            f.write("[Service]\nExecStart=/bin/true\n")
        r, calls, _ = self.run_lw('lw_plan; echo "st=$LW_UNIT_ST change=$LW_CHANGE"; CHECK=1 lw_remove\n',
                                  rate="", home=home)
        self.assertIn("st=conflict change=0", r.stdout)
        self.assertIn("left alone", r.stderr)
        self.assertEqual(calls, [])

    def test_check_mode_changes_nothing(self):
        r, calls, home = self.run_lw("lw_plan; lw_install\n")
        r, calls, _ = self.run_lw("CHECK=1; lw_plan; lw_remove\n", rate="", home=home)
        self.assertIn("would stop, disable and remove", r.stdout)
        self.assertEqual(calls, [])
        self.assertTrue(os.path.exists(os.path.join(home, ".local/lib/cst/bt-ldac-watch")))


class ScriptTest(unittest.TestCase):
    def test_setup_mentions_the_watcher_everywhere(self):
        with open(SETUP) as f:
            sh = f.read()
        for name in ("lw_install", "lw_remove", "lw_report"):
            self.assertRegex(sh, re.compile(rf"^\s+{name}$", re.M), name)

    def run_apply(self, restart_rc, bearer="set"):
        """apply() with every step a stub; DO_RESTARTS_RC is what the
        restart question gave (1 = cancelled, or no terminal)."""
        stubs = f"""
set -Eeuo pipefail
PROG=cst-test; WP_STATE=ok; LW_WANT=1; MC_CHANGED=0; LEGACY=0; BT_RESTART=1; WP_RESTART=1
BEARER_STATE={bearer}; FAILS=0; WARNS=0; LW_UNIT=cst-bt-ldac48.service; MAIN_CONF=m; WP_CONF=w
LDAC_QUALITY=auto; LW_UNIT_ST=ok; LW_PROG_ST=ok; DEVICE_MAC=AA:BB:CC:DD:EE:FF; BEARER=le
LE_AUDIO=off; LE_STATE=ok; LE_CONF=l; LE_ON_HINT=hint
step() {{ echo "STEP $*" >>"$LOG"; }}
preconditions() {{ :; }}; compute_plan() {{ :; }}; die() {{ echo "DIE $*"; exit 1; }}
do_restarts() {{ step do_restarts; return {restart_rc}; }}
lw_install() {{ step lw_install; }}; lw_remove() {{ step lw_remove; }}; apply_le() {{ step apply_le; }}
bluetoothctl() {{ step bluetoothctl "$@"; }}; bt_info() {{ :; }}
verify() {{ step verify; }}
le_plan() {{ :; }}; lw_plan() {{ :; }}
"""
        script = stubs + shell_functions("apply_watcher", "apply") + "apply\n"
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        log = os.path.join(tmp, "log")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                           env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LOG": log})
        r.steps = []
        if os.path.exists(log):
            with open(log) as f:
                r.steps = [x.split()[1] for x in f.read().splitlines()]
        return r

    def test_cancelled_restart_still_sets_up_the_watcher(self):
        r = self.run_apply(1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.steps, ["apply_le", "do_restarts", "lw_install"])
        self.assertIn("Not done yet: the restarts above, the preferred bearer and the checks.", r.stdout)
        self.assertIn("After restarting, run: cst-test apply", r.stdout)
        r = self.run_apply(1, bearer="none")
        self.assertIn("Not done yet: the restarts above and the checks.", r.stdout)

    def test_apply_order_after_the_restarts(self):
        r = self.run_apply(0)
        self.assertEqual(r.steps, ["apply_le", "do_restarts", "lw_install", "bluetoothctl", "verify"])


if __name__ == "__main__":
    unittest.main()

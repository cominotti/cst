# SPDX-License-Identifier: MPL-2.0
"""Unit tests for bluetooth/bt-ldac-watch (the LDAC sample rate watcher):
parsing of pw-dump, pactl events and pactl cards, the act/skip decision, the
one-attempt-per-connection state machine and its cooldown, the bounce with a
fake system; and for its user unit (bt-helper) and the install/remove steps
of bluetooth-setup.sh with systemctl as a stub. Nothing real is switched.

Run from the repository root:
    python3 -I -B -m unittest discover -s bluetooth/tests -t . -v
"""

from collections.abc import Callable
from dataclasses import dataclass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from typing import Any
import unittest
from unittest import mock

sys.dont_write_bytecode = True

from bluetooth import ldac_watch
from bluetooth.domain import watch as bt_watch
from bluetooth.tests.test_bluetooth import MAC, WATCH, HelperModule, load_helper, shell_functions

DEV = "0A_1B_2C_3D_4E_5F"


@dataclass(frozen=True)
class ApplyRun:
    """Result captured from invoking `apply()` in the shell stub harness."""

    returncode: int
    stdout: str
    stderr: str
    steps: list[str]


def pw_dump(
    rate: int = 96000,
    codec: str = "ldac",
    profile: str = "a2dp-sink",
    card: int = 500,
    out: int = 600,
    mic_state: str = "suspended",
    suspended: bool = False,
) -> list[dict[str, Any]]:
    """A shortened pw-dump of the headset as found on sophia: the card, the
    A2DP output node and WirePlumber's headset source (named with ':')."""
    out_params: dict[str, Any] = {
        "EnumFormat": [{"mediaType": "audio", "format": {"default": "F32LE"}, "rate": rate, "channels": 2}],
        "Format": [] if suspended else [{"mediaType": "audio", "format": "F32LE", "rate": rate, "channels": 2}],
        "Props": [{"volume": 1.0}, {"quality": -1}],
    }
    return [
        {
            "id": 36,
            "type": "PipeWire:Interface:Device",
            "info": {
                "props": {
                    "device.name": f"bluez_card.{DEV}",
                    "object.serial": str(card),
                    "bluez5.a2dp.ldac.quality": "auto",
                },
                "params": {"Profile": [{"index": 1, "name": profile}]},
            },
        },
        {
            "id": 129,
            "type": "PipeWire:Interface:Node",
            "info": {
                "state": "running",
                "props": {
                    "node.name": f"bluez_output.{DEV}.1",
                    "object.serial": str(out),
                    "api.bluez5.codec": codec,
                    "api.bluez5.profile": "a2dp-sink",
                },
                "params": out_params,
            },
        },
        {
            "id": 48,
            "type": "PipeWire:Interface:Node",
            "info": {
                "state": mic_state,
                "props": {"node.name": f"bluez_input.{MAC}", "object.serial": "601"},
                "params": {"Format": []},
            },
        },
        {"id": 106, "type": "PipeWire:Interface:Node", "info": {"props": {"node.name": "bluez_midi.server"}}},
        {"id": 1, "type": "PipeWire:Interface:Core", "info": {}},
    ]


def cards(active: str = "a2dp-sink", sbc: bool = True, lang_names: bool = False) -> list[dict[str, Any]]:
    """pactl -f json list cards, the headset's card (profile names as PipeWire's)."""
    profs: dict[str, dict[str, Any]] = {
        "off": {"description": "Off", "available": True},
        "a2dp-sink-sbc_xq": {"description": "High Fidelity Playback (A2DP Sink, codec SBC-XQ)", "available": True},
        "a2dp-sink-aac": {"description": "High Fidelity Playback (A2DP Sink, codec AAC)", "available": True},
        "a2dp-sink": {"description": "High Fidelity Playback (A2DP Sink, codec LDAC)", "available": True},
        "headset-head-unit": {"description": "Headset Head Unit (HSP/HFP, codec MSBC)", "available": True},
    }
    if sbc:
        profs["a2dp-sink-sbc"] = {"description": "High Fidelity Playback (A2DP Sink, codec SBC)", "available": True}
    if lang_names:  # descriptions in another language: only the names help
        for p in profs.values():
            p["description"] = "Reprodução"
    return [
        {"name": "alsa_card.x", "profiles": {}},
        {"name": f"bluez_card.{DEV}", "index": 500, "active_profile": active, "profiles": profs},
    ]


class ParseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h: HelperModule = load_helper()

    def test_snapshot(self) -> None:
        s = bt_watch.snapshot(DEV, pw_dump())
        self.assertEqual(
            s,
            {
                "card": 500,
                "profile": "a2dp-sink",
                "out": 600,
                "codec": "ldac",
                "rate": 96000,
                "inputs": [601],
                "mic": False,
            },
        )
        self.assertTrue(bt_watch.snapshot(DEV, pw_dump(mic_state="running"))["mic"])
        self.assertIsNone(bt_watch.snapshot("00_00", pw_dump())["card"])

    def test_suspended_node_rate_from_enumformat(self) -> None:
        self.assertEqual(bt_watch.snapshot(DEV, pw_dump(rate=48000, suspended=True))["rate"], 48000)
        objs = pw_dump()
        objs[1]["info"]["params"] = {}
        self.assertIsNone(bt_watch.snapshot(DEV, objs)["rate"])

    def test_helper_reads_the_same_rate(self) -> None:
        for objs in (pw_dump(), pw_dump(rate=48000, suspended=True)):
            pw = self.h.pw_state(DEV, objs)
            assert pw is not None
            self.assertEqual(pw[4], str(bt_watch.snapshot(DEV, objs)["rate"]))

    def test_snapshot_cli(self) -> None:
        r = subprocess.run(
            [sys.executable, "-I", WATCH, "--device", MAC, "--snapshot"],
            check=False,
            input=json.dumps(pw_dump()),
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("act: LDAC at 96000 Hz (want 48000)", r.stdout)
        r = subprocess.run(
            [sys.executable, "-I", WATCH, "--device", "nope", "--snapshot"],
            check=False,
            input="[]",
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 2)

    def test_decide(self) -> None:
        def d(
            rate: int = 96000,
            codec: str = "ldac",
            profile: str = "a2dp-sink",
            mic_state: str = "suspended",
        ) -> str:
            return bt_watch.decide(
                bt_watch.snapshot(DEV, pw_dump(rate=rate, codec=codec, profile=profile, mic_state=mic_state)),
                48000,
            )[0]

        self.assertEqual(d(), "act")
        self.assertEqual(d(rate=48000), "none")
        self.assertEqual(d(rate=44100), "act")
        self.assertEqual(d(codec="sbc", rate=44100), "none")
        self.assertEqual(d(mic_state="running"), "skip")
        self.assertEqual(d(profile="headset-head-unit"), "skip")
        self.assertEqual(bt_watch.decide(bt_watch.snapshot("00_00", pw_dump()), 48000)[0], "none")
        self.assertEqual(bt_watch.decide(None, 48000)[0], "none")
        # The reason names the codec in use.
        sbc = bt_watch.snapshot(DEV, pw_dump(rate=44100, codec="sbc"))
        self.assertEqual(bt_watch.decide(sbc, 48000), ("none", "codec sbc"))

    def test_relevant_events(self) -> None:
        rel = bt_watch.relevant
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

    def test_profiles(self) -> None:
        p = bt_watch.profiles
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
    def setUp(self) -> None:
        self.t = bt_watch.Tracker(48000, settle=5, cooldown=600)

    def snap(
        self,
        rate: int = 96000,
        codec: str = "ldac",
        profile: str = "a2dp-sink",
        card: int = 500,
        out: int = 600,
        mic_state: str = "suspended",
        suspended: bool = False,
    ) -> dict[str, Any]:
        return dict(
            bt_watch.snapshot(
                DEV,
                pw_dump(
                    rate=rate,
                    codec=codec,
                    profile=profile,
                    card=card,
                    out=out,
                    mic_state=mic_state,
                    suspended=suspended,
                ),
            )
        )

    def test_settle_then_act_once(self) -> None:
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

    def test_good_rate_in_between_restarts_the_settle_time(self) -> None:
        self.t.observe(self.snap(), 0)
        self.t.observe(self.snap(rate=48000), 3)
        self.assertEqual(self.t.observe(self.snap(), 6), ("wait", 5))

    def test_new_connection_allows_another_attempt_after_the_cooldown(self) -> None:
        self.t.observe(self.snap(), 0)
        self.assertEqual(self.t.observe(self.snap(), 5)[0], "act")
        self.t.attempted(5)
        self.t.result(True)
        # Disconnected, then connected again (a new card object) at 96 kHz.
        self.assertEqual(self.t.observe(None, 100)[0], "none")
        what, left = self.t.observe(self.snap(card=501, out=610), 200)
        self.assertEqual(what, "cooldown")
        assert isinstance(left, (int, float))
        self.assertAlmostEqual(left, 405)
        self.assertEqual(self.t.observe(self.snap(card=501, out=610), 605)[0], "wait")
        self.assertEqual(self.t.observe(self.snap(card=501, out=610), 610)[0], "act")

    def test_failed_attempt_gives_up_until_reconnect(self) -> None:
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

    def test_node_replaced_by_someone_else_resets_the_attempt(self) -> None:
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

    def test_cooldown_on_the_same_connection_after_reset(self) -> None:
        self.t.observe(self.snap(), 0)
        self.t.observe(self.snap(), 5)
        self.t.attempted(5)
        self.t.own_node(self.snap(out=700, rate=48000))
        self.t.result(True)
        self.t.observe(self.snap(out=705, codec="msbc", rate=16000, profile="headset-head-unit"), 200)
        self.assertEqual(self.t.observe(self.snap(out=710), 400)[0], "cooldown")

    def test_fix_that_does_not_hold_gives_up(self) -> None:
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
        assert isinstance(why, str)
        self.assertIn("by itself", why)
        for now in (700, 1400, 5000):
            self.assertEqual(self.t.observe(self.snap(out=710), now)[0], "done")
        # Also when the node went away in between (a look in that moment).
        self.assertEqual(self.t.observe(self.snap(out=720), 6000)[0], "done")
        # A new connection starts afresh.
        self.t.observe(self.snap(card=501, out=900), 7000)
        self.assertEqual(self.t.observe(self.snap(card=501, out=900), 7005)[0], "act")

    def test_relapse_through_a_moment_without_a_node(self) -> None:
        self.t.observe(self.snap(), 0)
        self.t.observe(self.snap(), 5)
        self.t.attempted(5)
        self.t.own_node(self.snap(out=700, rate=48000))
        self.t.result(True)
        objs = [o for o in pw_dump() if "bluez_output" not in str(o)]
        self.assertEqual(self.t.observe(bt_watch.snapshot(DEV, objs), 10)[0], "none")
        self.assertEqual(self.t.observe(self.snap(out=710), 12)[0], "done")
        self.assertEqual(self.t.observe(self.snap(out=710), 1000)[0], "done")

    def test_at_most_two_attempts_per_connection(self) -> None:
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
        assert isinstance(why, str)
        self.assertIn("2 attempts", why)

    def test_skip_while_the_microphone_is_in_use(self) -> None:
        self.t.observe(self.snap(mic_state="running"), 0)
        self.assertEqual(self.t.observe(self.snap(mic_state="running"), 10)[0], "skip")
        self.assertEqual(self.t.observe(self.snap(), 11)[0], "wait")
        self.assertEqual(self.t.observe(self.snap(), 16)[0], "act")


class FakeLive:
    """The system as the watcher sees it: switching to the bounce profile and
    back to LDAC makes the PC configure the stream at BACK_RATE."""

    def __init__(self, back_rate: int = 48000, fail: tuple[str, ...] | set[str] = ()) -> None:
        self.dev = DEV
        self.active = "a2dp-sink"
        self.rate = 96000
        self.out = 600
        self.back_rate = back_rate
        self.fail = set(fail)
        self.calls: list[str] = []
        self.snap_saved: Callable[[], dict[str, Any] | None] | None = None
        self._snap_fn: Callable[[], dict[str, Any] | None] | None = None
        self._set_profile_fn: Callable[[str], bool] | None = None

    def snap(self) -> dict[str, Any] | None:
        if self._snap_fn is not None:
            return self._snap_fn()
        codec = "ldac" if self.active == "a2dp-sink" else "sbc"
        return bt_watch.snapshot(DEV, pw_dump(rate=self.rate, codec=codec, profile=self.active, out=self.out))

    def cards(self) -> list[dict[str, Any]]:
        return cards(active=self.active)

    def set_profile(self, prof: str) -> bool:
        if self._set_profile_fn is not None:
            return self._set_profile_fn(prof)
        return self._real_set_profile(prof)

    def _real_set_profile(self, prof: str) -> bool:
        self.calls.append(prof)
        if prof in self.fail:
            return False
        self.active = prof
        self.out += 1
        self.rate = self.back_rate if prof == "a2dp-sink" else 44100
        return True


class BounceTest(unittest.TestCase):
    def setUp(self) -> None:
        # No waiting: sleeps do nothing, the wait limits are zero.
        clock = types.SimpleNamespace(t=0.0)
        fake_time = types.SimpleNamespace(monotonic=lambda: clock.t, sleep=lambda s: setattr(clock, "t", clock.t + s))
        self.lines: list[tuple[int, str]] = []
        for name, value in (
            ("time", fake_time),
            ("SBC_HOLD", 0),
            ("log", lambda level, msg: self.lines.append((level, msg))),
        ):
            patcher = mock.patch.object(ldac_watch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def watcher(self, live: FakeLive, dry: bool = False) -> ldac_watch.Watcher:
        w = ldac_watch.Watcher(DEV, 48000, dry_run=dry)
        w.live = live
        w.tracker = bt_watch.Tracker(48000, settle=0, cooldown=600)
        return w

    def test_bounce_fixes_the_rate(self) -> None:
        live = FakeLive()
        w = self.watcher(live)
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink"])
        self.assertIn((6, "fixed: LDAC now at 48000 Hz"), self.lines)
        self.assertIn(
            (
                6,
                "act: LDAC at 96000 Hz (want 48000): switching a2dp-sink -> a2dp-sink-sbc -> "
                "a2dp-sink (about a second of silence)",
            ),
            self.lines,
        )
        # The new node is ours: nothing more to do, and no second attempt.
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink"])

    def test_headset_insists_on_96k(self) -> None:
        live = FakeLive(back_rate=96000)
        w = self.watcher(live)
        w.look()
        self.assertTrue(any(lv == 3 and "came back at 96000 Hz" in m for lv, m in self.lines))
        for _ in range(3):
            w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink"])

    def test_switch_back_fails(self) -> None:
        live = FakeLive(fail={"a2dp-sink"})
        w = self.watcher(live)
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink", "a2dp-sink"])
        self.assertTrue(any(lv == 3 and "could not switch back" in m for lv, m in self.lines))

    def test_dry_run_switches_nothing(self) -> None:
        live = FakeLive()
        w = self.watcher(live, dry=True)
        w.look()
        w.look()
        self.assertEqual(live.calls, [])
        self.assertTrue(any("would switch a2dp-sink -> a2dp-sink-sbc -> a2dp-sink" in m for _, m in self.lines))

    def test_profile_changed_during_the_bounce_is_left_alone(self) -> None:
        # WirePlumber switches to HFP (an app opened the microphone) while
        # the card is on SBC: the bounce does not force LDAC back.
        live = FakeLive()

        def set_profile(prof: str) -> bool:
            ok = live._real_set_profile(prof)
            if prof == "a2dp-sink-sbc":
                live.active = "headset-head-unit"
            return ok

        live._set_profile_fn = set_profile
        w = self.watcher(live)
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc"])
        self.assertTrue(any(lv == 3 and "went to headset-head-unit" in m for lv, m in self.lines))
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc"])

    def test_microphone_in_use_during_the_bounce(self) -> None:
        live = FakeLive()

        def snap_mic() -> dict[str, Any]:
            live._snap_fn = None
            s = live.snap()
            live._snap_fn = snap_mic
            assert s is not None
            if live.active == "a2dp-sink-sbc":
                s["mic"] = True
            return s

        live._snap_fn = snap_mic
        w = self.watcher(live)
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc"])
        self.assertTrue(any(lv == 3 and "microphone came into use" in m and "by hand" in m for lv, m in self.lines))

    def test_alt_never_active_still_switches_back(self) -> None:
        live = FakeLive()

        def set_profile(prof: str) -> bool:
            if prof == "a2dp-sink":
                return live._real_set_profile(prof)
            live.calls.append(prof)
            return True  # pactl says yes, the card stays on LDAC

        live._set_profile_fn = set_profile
        w = self.watcher(live)
        w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink"])
        self.assertIn((6, "fixed: LDAC now at 48000 Hz"), self.lines)

    def test_stop_signal_waits_for_the_bounce(self) -> None:
        live = FakeLive()

        def set_profile(prof: str) -> bool:
            if prof == "a2dp-sink-sbc":
                ldac_watch.Stop.handler(15, None)  # SIGTERM while switching
            return live._real_set_profile(prof)

        live._set_profile_fn = set_profile
        w = self.watcher(live)
        self.addCleanup(setattr, ldac_watch.Stop, "asked", False)
        with self.assertRaises(SystemExit):
            w.look()
        self.assertEqual(live.calls, ["a2dp-sink-sbc", "a2dp-sink"])
        self.assertEqual(live.active, "a2dp-sink")
        self.assertTrue(any("stop asked during the profile switch" in m for _, m in self.lines))
        # Outside a bounce, a stop is immediate.
        ldac_watch.Stop.asked = False
        with self.assertRaises(SystemExit):
            ldac_watch.Stop.handler(15, None)

    def test_no_pw_dump_is_not_a_disconnection(self) -> None:
        live = FakeLive(back_rate=96000)
        w = self.watcher(live)
        w.look()
        n = len(live.calls)
        live._snap_fn = lambda: None
        w.look()
        live._snap_fn = None
        w.look()
        self.assertEqual(len(live.calls), n)


class ListenTest(unittest.TestCase):
    """listen(): a look at the start, one more after a relevant burst (also
    when it is not the first line of a read), back on end of file."""

    def test_events_from_a_pipe(self) -> None:
        self.enterContext(mock.patch.object(ldac_watch, "DEBOUNCE", 0))
        watcher = ldac_watch.Watcher(DEV, 48000)
        looks: list[int] = []
        watcher.known = {600}
        with mock.patch.object(watcher, "look", side_effect=lambda: looks.append(1)):
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
            self.assertEqual(len(looks), 1)  # nothing relevant: only the start

    def test_relevant_burst_triggers_a_look(self) -> None:
        self.enterContext(mock.patch.object(ldac_watch, "DEBOUNCE", 0))
        watcher = ldac_watch.Watcher(DEV, 48000)
        looks: list[int] = []
        r, wfd = os.pipe()

        def look() -> None:
            looks.append(1)
            if len(looks) == 1:
                os.write(wfd, b"Event 'change' on client #1\nEvent 'change' on sink #600\n")
            else:
                os.close(wfd)

        watcher.known = {600}
        with (
            mock.patch.object(watcher, "look", side_effect=look),
            os.fdopen(r, "rb", buffering=0) as f,
        ):
            watcher.listen(types.SimpleNamespace(stdout=f))
        self.assertEqual(len(looks), 2)


class UnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.h = load_helper()

    def test_render_and_state(self) -> None:
        u = self.h.render_unit(MAC.lower(), "48000")
        self.assertIn(f"ExecStart=/usr/bin/python3 -I %h/.local/lib/cst/bt-ldac-watch --device {MAC} --rate 48000", u)
        self.assertIn("WantedBy=default.target", u)
        self.assertIn("KillMode=mixed", u)
        self.assertTrue(u.startswith(self.h.MARKER))
        self.assertEqual(self.h.unit_state(None, u), "missing")
        self.assertEqual(self.h.unit_state(u, u), "ok")
        self.assertEqual(self.h.unit_state(u.replace("48000", "44100"), u), "change")
        self.assertEqual(self.h.unit_state("[Service]\nExecStart=/bin/true\n", u), "conflict")

    def test_watcher_carries_the_marker(self) -> None:
        with open(WATCH) as f:
            self.assertIn(self.h.MARKER, f.read())


class InstallTest(unittest.TestCase):
    """lw_plan, lw_install and lw_remove of bluetooth-setup.sh in a scratch
    HOME, with systemctl as a stub that keeps the state in files."""

    STUBS = """
set -Eeuo pipefail
PROG=test; CHECK=0
SRC="$REPO/bluetooth"
source "$REPO/cst-install.bash"  # as bluetooth-setup.sh does: lw_install and lw_plan use the installer
LW_UNIT="cst-bt-ldac48.service"
LW_SRC="${SRC}/bt-ldac-watch"
LW_PROG="${HOME}/.local/lib/cst/bt-ldac-watch"
LW_UNIT_FILE="${HOME}/.config/systemd/user/${LW_UNIT}"
WP_MARKER="# managed-by: bluetooth-setup.sh (CST)"
DEVICE_MAC=0A:1B:2C:3D:4E:5F
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

    def run_lw(
        self,
        body: str,
        rate: str = "48000",
        home: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
        home = home or tempfile.mkdtemp(prefix="bt-test-")
        if not hasattr(self, "_homes"):
            self._homes: set[str] = set()
        if home not in self._homes:
            self._homes.add(home)
            self.addCleanup(shutil.rmtree, home)
        log = os.path.join(home, "log")
        if os.path.exists(log):
            os.remove(log)
        script = (
            self.STUBS
            + f'LDAC_RATE="{rate}"\n'
            + shell_functions(
                "file_state",
                "lw_plan",
                "lw_ours_present",
                "lw_unit_label",
                "lw_prog_label",
                "lw_report",
                "lw_install",
                "lw_remove",
                "lw_last_failure",
            )
            + body
        )
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": home,
            "LOG": log,
            "REPO": os.path.dirname(os.path.dirname(WATCH)),
            **(extra_env or {}),
        }
        r = subprocess.run(
            ["bash", "-c", script], check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL
        )
        calls: list[str] = []
        if os.path.exists(log):
            with open(log) as f:
                calls = [c for c in f.read().splitlines() if "is-" not in c]
        return r, calls, home

    def test_install_then_nothing_then_remove(self) -> None:
        r, calls, home = self.run_lw('lw_plan; echo "change=$LW_CHANGE"; lw_install\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("change=1", r.stdout)
        self.assertEqual(
            calls,
            [
                "systemctl --user daemon-reload",
                "systemctl --user enable cst-bt-ldac48.service",
                "systemctl --user start cst-bt-ldac48.service",
            ],
        )
        prog = os.path.join(home, ".local/lib/cst/bt-ldac-watch")
        unit = os.path.join(home, ".config/systemd/user/cst-bt-ldac48.service")
        self.assertTrue(os.access(prog, os.X_OK))
        with open(unit) as f:
            self.assertIn("--rate 48000", f.read())
        # Both are in the user's install record (in this HOME), the component marked done.
        record = os.path.join(home, ".local/state/cst/installed.json")
        with open(record) as f:
            rec = json.load(f)
        self.assertEqual(sorted(rec["files"]), sorted([prog, unit]))
        self.assertEqual(rec["files"][unit]["src"], None)
        self.assertEqual(rec["files"][prog]["src"], "bluetooth/bt-ldac-watch")
        self.assertEqual(rec["components"], {"bluetooth:ldac": "done"})

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
        self.assertEqual(
            calls, ["systemctl --user disable --now cst-bt-ldac48.service", "systemctl --user daemon-reload"]
        )
        self.assertFalse(os.path.exists(prog))
        self.assertFalse(os.path.exists(os.path.dirname(prog)))
        self.assertFalse(os.path.exists(unit))
        with open(record) as f:
            self.assertEqual(json.load(f)["files"], {}, "the removed files are still in the record")
        r, calls, _ = self.run_lw('lw_plan; echo "change=$LW_CHANGE"; lw_report\n', rate="", home=home)
        self.assertIn("change=0", r.stdout)
        self.assertIn("item ok LDAC rate watcher", r.stdout)

    def test_plan_names_the_user_file_and_the_last_failure(self) -> None:
        from bluetooth.tests.test_bluetooth import JOURNAL

        home = tempfile.mkdtemp(prefix="bt-test-")
        jf = os.path.join(home, "journal")
        with open(jf, "w") as f:
            f.write(JOURNAL)
        r, calls, _ = self.run_lw(
            "lw_plan; lw_report\n", home=home, extra_env={"CMD": "plan", "USER_KEYS": "LDAC_RATE", "JOURNAL": jf}
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("item note LDAC rate watcher", r.stdout)
        self.assertIn("item warn LDAC rate watcher", r.stdout)
        # The default (LDAC_RATE not in the user's file) and no failure in the log: no notes.
        r, _, _ = self.run_lw("lw_plan; lw_report\n", home=home, extra_env={"CMD": "plan"})
        self.assertNotIn("item note", r.stdout)
        self.assertNotIn("item warn", r.stdout)

    def test_the_program_state_comes_from_the_installer_check_and_its_link_rule(self) -> None:
        # status/plan ask the installer (`installer check`), not `cmp <(installer payload)`: no Python
        # message on stderr for an older copy, and a link in the home is judged by what it points to
        # (cst_core.domain.install.link_read_through) instead of being a conflict.
        r, _, home = self.run_lw("lw_plan; lw_install\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        prog = os.path.join(home, ".local/lib/cst/bt-ldac-watch")
        kept = os.path.join(home, "dotfiles-bt-ldac-watch")
        os.rename(prog, kept)
        os.symlink(kept, prog)
        r, _, _ = self.run_lw('lw_plan; echo "st=$LW_PROG_ST"\n', home=home, extra_env={"CMD": "plan"})
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertIn("st=ok", r.stdout)
        with open(kept, "a") as f:
            f.write("# old\n")
        r, _, _ = self.run_lw('lw_plan; echo "st=$LW_PROG_ST"\n', home=home, extra_env={"CMD": "plan"})
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertIn("st=change", r.stdout)
        r, _, _ = self.run_lw("lw_plan; lw_install\n", home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(os.path.islink(prog))
        with open(kept, "rb") as f:
            self.assertTrue(f.read().endswith(b"# old\n"), "the link was written through")

    def test_a_linked_unit_is_judged_by_what_it_points_to(self) -> None:
        # The installer's rule for a link in the home (cst_core.domain.install.link_read_through):
        # the same text is up to date and stays a link; ours with other text is rewritten, the
        # link replaced and its target untouched. Before, any link was a conflict.
        r, _, home = self.run_lw("lw_plan; lw_install\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        unit = os.path.join(home, ".config/systemd/user/cst-bt-ldac48.service")
        kept = os.path.join(home, "dotfiles-cst-bt-ldac48.service")
        os.rename(unit, kept)
        os.symlink(kept, unit)
        r, calls, _ = self.run_lw('lw_plan; echo "st=$LW_UNIT_ST change=$LW_CHANGE"; lw_install\n', home=home)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertIn("st=ok change=0", r.stdout)
        self.assertEqual(calls, [])
        self.assertTrue(os.path.islink(unit))
        with open(kept, "a") as f:
            f.write("# changed by hand\n")
        r, calls, _ = self.run_lw('lw_plan; echo "st=$LW_UNIT_ST"; lw_install\n', home=home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("st=change", r.stdout)
        self.assertIn(f"written:   {unit}", r.stdout)
        self.assertIn("systemctl --user daemon-reload", calls)
        self.assertFalse(os.path.islink(unit))
        with open(kept) as f:
            self.assertTrue(f.read().endswith("# changed by hand\n"), "the link was written through")
        r, _, _ = self.run_lw('lw_plan; echo "st=$LW_UNIT_ST"\n', home=home, extra_env={"CMD": "plan"})
        self.assertIn("st=ok", r.stdout)

    def test_a_masked_or_foreign_linked_unit_stays_a_conflict(self) -> None:
        # Judged by what it points to: /dev/null (a masked unit), a unit not ours or a folder is a conflict.
        home = tempfile.mkdtemp(prefix="bt-test-")
        units = os.path.join(home, ".config/systemd/user")
        os.makedirs(units)
        foreign = os.path.join(home, "foreign.service")
        with open(foreign, "w") as f:
            f.write("[Service]\nExecStart=/bin/true\n")
        unit = os.path.join(units, "cst-bt-ldac48.service")
        folder = os.path.join(home, "a-folder")
        os.mkdir(folder)
        for target in (foreign, os.devnull, folder):
            os.symlink(target, unit)
            r, calls, _ = self.run_lw('lw_plan; echo "st=$LW_UNIT_ST"\n', home=home, extra_env={"CMD": "plan"})
            self.assertEqual(r.stderr, "", target)
            self.assertIn("st=conflict", r.stdout, target)
            self.assertEqual(calls, [])
            os.unlink(unit)

    def test_foreign_unit_is_left_alone(self) -> None:
        home = tempfile.mkdtemp(prefix="bt-test-")
        os.makedirs(os.path.join(home, ".config/systemd/user"))
        with open(os.path.join(home, ".config/systemd/user/cst-bt-ldac48.service"), "w") as f:
            f.write("[Service]\nExecStart=/bin/true\n")
        r, calls, _ = self.run_lw(
            'lw_plan; echo "st=$LW_UNIT_ST change=$LW_CHANGE"; CHECK=1 lw_remove\n', rate="", home=home
        )
        self.assertIn("st=conflict change=0", r.stdout)
        self.assertIn("left alone", r.stderr)
        self.assertEqual(calls, [])

    def test_check_mode_changes_nothing(self) -> None:
        r, calls, home = self.run_lw("lw_plan; lw_install\n")
        r, calls, _ = self.run_lw("CHECK=1; lw_plan; lw_remove\n", rate="", home=home)
        self.assertIn("would stop, disable and remove", r.stdout)
        self.assertEqual(calls, [])
        self.assertTrue(os.path.exists(os.path.join(home, ".local/lib/cst/bt-ldac-watch")))


class ScriptTest(unittest.TestCase):
    def run_steps(self, body: str, *functions: str) -> list[str]:
        """Run bluetooth-setup.sh's `functions` with the watcher's steps (and what they call) as stubs."""
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        log = os.path.join(tmp, "log")
        main_conf = os.path.join(tmp, "main.conf")
        with open(main_conf, "w") as f:
            f.write("[General]\n")
        stubs = f"""
set -Eeuo pipefail
step() {{ echo "STEP $*" >>"$LOG"; }}
lw_install() {{ step lw_install; }}; lw_remove() {{ step lw_remove; }}; lw_report() {{ step lw_report; }}
lw_plan() {{ :; }}; lw_ours_present() {{ return 1; }}; die() {{ echo "DIE $*"; exit 1; }}
item() {{ :; }}; section() {{ :; }}; helper() {{ :; }}; wp_word() {{ :; }}; wp_label() {{ :; }}; wp_note() {{ :; }}
le_report() {{ :; }}; le_others_report() {{ :; }}; oldest_backup() {{ return 1; }}
LW_UNIT=cst-bt-ldac48.service; MAIN_CONF={main_conf}; WP_CONF={tmp}/none-wp; LE_CONF={tmp}/none-le
WP_STATE=ok; LEGACY=0; BEARER_STATE=none; BEARER=le; LE_AUDIO=on; DEVICE_MAC=AA:BB:CC:DD:EE:FF
WP_MARKER=m; OLD_WP_MARKER=o; CHECK=1; TMP={tmp}/tmp
"""
        proc = subprocess.run(
            ["bash", "-c", stubs + shell_functions(*functions) + body],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LOG": log},
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        if not os.path.exists(log):
            return []
        with open(log) as f:
            return [x.split()[1] for x in f.read().splitlines()]

    def test_every_command_reaches_the_watcher(self) -> None:
        # apply installs the watcher when LDAC_RATE is set and removes it when not;
        # plan and status report it; revert removes it.
        self.assertEqual(self.run_steps("LW_WANT=1; apply_watcher\n", "apply_watcher"), ["lw_install"])
        self.assertEqual(self.run_steps("LW_WANT=0; apply_watcher\n", "apply_watcher"), ["lw_remove"])
        self.assertEqual(self.run_steps("files_report\n", "files_report"), ["lw_report"])
        self.assertEqual(self.run_steps("revert || true\n", "revert"), ["lw_remove"])

    def run_apply(self, restart_rc: int, bearer: str = "set") -> ApplyRun:
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
BT_TIMEOUT=0  # call the bluetoothctl stub below directly, not through timeout
bluetoothctl() {{ step bluetoothctl "$@"; }}; bt_info() {{ :; }}
verify() {{ step verify; }}
le_plan() {{ :; }}; lw_plan() {{ :; }}
"""
        script = stubs + shell_functions("apply_watcher", "apply") + "apply\n"
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        log = os.path.join(tmp, "log")
        proc = subprocess.run(
            ["bash", "-c", script],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LOG": log},
            stdin=subprocess.DEVNULL,
        )
        steps: list[str] = []
        if os.path.exists(log):
            with open(log) as f:
                steps = [x.split()[1] for x in f.read().splitlines()]
        return ApplyRun(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr, steps=steps)

    def test_cancelled_restart_still_sets_up_the_watcher(self) -> None:
        r = self.run_apply(1)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.steps, ["apply_le", "do_restarts", "lw_install"])
        self.assertIn("Not done yet: the restarts above, the preferred bearer and the checks.", r.stdout)
        self.assertIn("After restarting, run: cst-test apply", r.stdout)
        r = self.run_apply(1, bearer="none")
        self.assertIn("Not done yet: the restarts above and the checks.", r.stdout)

    def test_apply_order_after_the_restarts(self) -> None:
        r = self.run_apply(0)
        self.assertEqual(r.steps, ["apply_le", "do_restarts", "lw_install", "bluetoothctl", "verify"])


if __name__ == "__main__":
    unittest.main()

# SPDX-License-Identifier: MPL-2.0
"""Unit tests for bluetooth/bt-helper (main.conf editing, the WirePlumber
rule, settings, parsing of busctl/pactl/pw-dump output, the record of
restarts still owed). bluetooth-setup.sh as a whole is in test_setup.py. No
root, no Bluetooth, no service is touched.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s bluetooth/tests -t . -v
"""

from collections.abc import Iterable, Mapping, Sequence
import io
import os
import shutil
import subprocess
import sys
import tempfile
import types
from typing import Any, Protocol, cast, override
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
BT = os.path.dirname(HERE)
REPO = os.path.dirname(BT)
HELPER = os.path.join(BT, "bt-helper")
WATCH = os.path.join(BT, "bt-ldac-watch")
SETUP = os.path.join(REPO, "bluetooth-setup.sh")
DEFAULTS = os.path.join(BT, "bluetooth.conf")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from bluetooth import adapters as bt_adapters, cli as bt_app, domain as bt_domain
from bluetooth.adapters import LocalBluetoothHostAdapter
from bluetooth.domain import RESTART_UNITS, classify_other_fragments, parse_pending, render_pending, still_pending
from tests.script_loader import load_script

ISO = "6fbaf188-05e0-496a-9885-d6ddfdb4e03e"
MAC = "0A:1B:2C:3D:4E:5F"

# The [General] part of BlueZ's shipped main.conf, shortened (the lines and
# comments around the two keys as Fedora ships them).
VENDOR = """\
[General]

# Default adapter name
#Name = BlueZ

# Enables D-Bus experimental interfaces
# Possible values: true or false
#Experimental = false

# Enables kernel experimental features, alternatively a list of UUIDs
# can be given.
# Possible values: true,false,<UUID List>
# 6fbaf188-05e0-496a-9885-d6ddfdb4e03e (BlueZ Experimental ISO socket)
# Defaults to false.
#KernelExperimental = false

[BR]
#PageScanType =

[LE]
#Experimental = false
"""

# What apply-le-audio.sh wrote (verbatim, as found on the host).
OLD_WP = """\
# Fone 0A:1B:2C:3D:4E:5F: qualidade LDAC "auto" quando conectado por A2DP.
# "auto" = taxa adaptativa (990/660/330 kbps); "hq" fixo travava o Beam 4.
# LE Audio (BAP/LC3) vem do BlueZ (Experimental/KernelExperimental em
# /etc/bluetooth/main.conf); os perfis bap-* aparecem quando o fone conecta via LE.
# Gerado por apply-le-audio.sh — edite o script, não este arquivo.
monitor.bluez.rules = [
  {
    matches = [ { device.name = "bluez_card.0A_1B_2C_3D_4E_5F" } ]
    actions = {
      update-props = {
        bluez5.a2dp.ldac.quality = "auto"
      }
    }
  }
]
"""


class HelperModule(Protocol):
    """Typed interface for functions and constants exposed by `bluetooth/bt-helper`."""

    ISO_UUID: str
    MARKER: str
    OLD_MARKERS: tuple[str, ...]
    CLASSIC_ROLES: tuple[str, ...]
    LE_CONF_NAME: str
    LDAC_UNIT: str
    LDAC_PROG: str

    def edit_general(self, text: str) -> tuple[str, bool]: ...
    def revert_general(self, text: str, backup: str) -> tuple[str, bool]: ...
    def current_values(self, text: str) -> dict[str, str | None]: ...
    def render_wp(self, mac: str, quality: str) -> str: ...
    def wp_state(self, existing: str | None, rendered: str) -> str: ...
    def effective(self, text: str) -> list[str]: ...
    def device_id(self, mac: str) -> str: ...
    def render_unit(self, mac: str, rate: str | int) -> str: ...
    def unit_state(self, existing: str | None, rendered: str) -> str: ...
    def merged_settings(self, texts: Iterable[str]) -> dict[str, str]: ...
    def parse_settings(self, text: str) -> dict[str, str]: ...
    def set_keys(self, text: str) -> list[str]: ...
    def adapter_path(self, objects: Mapping[str, Any]) -> str | None: ...
    def card_state(
        self,
        dev: str,
        cards: Sequence[Mapping[str, Any]],
        srcs: Sequence[Mapping[str, Any]],
        outs: Sequence[Mapping[str, Any]],
    ) -> tuple[str, bool, bool] | None: ...
    def pw_state(
        self, dev: str, objs: Sequence[Mapping[str, Any]]
    ) -> tuple[str | None, str | None, str | None, str | None, str | None] | None: ...
    def render_le_off(self) -> str: ...
    def defines_section(self, text: str, section: str = ...) -> bool: ...
    def roles_state(self, value: str | None) -> str: ...
    def other_fragments(self, ours: str, dirs: Sequence[str]) -> list[tuple[str, str]]: ...
    def settings_set(self, text: str | None, key: str, value: str) -> tuple[str, bool]: ...
    def lw_failure(self, text: str) -> tuple[str, str, str] | None: ...


def load(path: str = HELPER, name: str = "bt_helper") -> types.ModuleType:
    """Load an extensionless helper script as a Python module."""
    return load_script(path, name)


def load_helper() -> HelperModule:
    """The rules bt-helper runs: bluetooth.domain, with other_fragments from bluetooth.adapters.

    bt-helper itself only wires argv (tests/test_entrypoints.py); the code it
    runs is imported from where it is defined.
    """
    rules = {k: v for k, v in vars(bt_domain).items() if not k.startswith("__")}
    return cast(HelperModule, types.SimpleNamespace(**rules, other_fragments=bt_adapters.other_fragments))


def general(text: str) -> list[str]:
    """The [General] lines of TEXT."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("[") and out:
            break
        out.append(line)
    return out


class MainConfTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.m = load_helper()

    def test_vendor_examples_replaced_in_place(self) -> None:
        new, changed = self.m.edit_general(VENDOR)
        self.assertTrue(changed)
        old_lines, new_lines = VENDOR.splitlines(), new.splitlines()
        self.assertEqual(len(old_lines), len(new_lines))  # nothing added or lost
        diff = [(a, b) for a, b in zip(old_lines, new_lines, strict=True) if a != b]
        self.assertEqual(
            diff,
            [
                ("#Experimental = false", "Experimental = true"),
                ("#KernelExperimental = false", f"KernelExperimental = {ISO}"),
            ],
        )
        self.assertIn("#Experimental = false", new.split("[LE]")[1])  # other sections untouched

    def test_idempotent(self) -> None:
        once, _ = self.m.edit_general(VENDOR)
        twice, changed = self.m.edit_general(once)
        self.assertFalse(changed)
        self.assertEqual(once, twice)

    def test_old_true_becomes_the_iso_uuid(self) -> None:
        text = VENDOR.replace("#Experimental = false", "Experimental = true").replace(
            "#KernelExperimental = false", "KernelExperimental = true"
        )
        new, changed = self.m.edit_general(text)
        self.assertTrue(changed)
        self.assertIn(f"KernelExperimental = {ISO}", new)
        self.assertNotIn("KernelExperimental = true", new)
        self.assertEqual(self.m.current_values(new), {"Experimental": "true", "KernelExperimental": ISO})

    def test_uuid_list_with_iso_is_kept(self) -> None:
        other = "d4992530-b9ec-469f-ab01-6c481c47da1c"
        text = VENDOR.replace("#Experimental = false", "Experimental = true").replace(
            "#KernelExperimental = false", f"KernelExperimental = {other},{ISO.upper()}"
        )
        new, changed = self.m.edit_general(text)
        self.assertFalse(changed)
        self.assertEqual(new, text)

    def test_last_active_line_wins_and_earlier_ones_are_commented(self) -> None:
        text = "[General]\nExperimental = false\nName = x\nExperimental = false\n[BR]\n"
        new, changed = self.m.edit_general(text)
        self.assertTrue(changed)
        g = general(new)
        self.assertEqual(g.count("Experimental = true"), 1)
        self.assertIn("#Experimental = false", g)
        self.assertEqual(
            g, ["[General]", f"KernelExperimental = {ISO}", "#Experimental = false", "Name = x", "Experimental = true"]
        )
        self.assertIn("Name = x", g)

    def test_missing_key_goes_under_general_and_missing_section_on_top(self) -> None:
        new, changed = self.m.edit_general("[General]\nName = x\n\n[BR]\n")
        self.assertTrue(changed)
        self.assertEqual(general(new)[:3], ["[General]", f"KernelExperimental = {ISO}", "Experimental = true"])
        new, changed = self.m.edit_general("# top comment\n[LE]\nFoo = 1\n")
        self.assertTrue(changed)
        self.assertTrue(
            new.startswith(f"[General]\nExperimental = true\nKernelExperimental = {ISO}\n\n# top comment\n")
        )
        self.assertFalse(self.m.edit_general(new)[1])

    def test_only_general_blocks_count_and_the_right_line_changes(self) -> None:
        # A key in another section is not ours, even on the line right before [General].
        text = "[Policy]\nExperimental = false\n[General]\nName = x\n"
        new, changed = self.m.edit_general(text)
        self.assertTrue(changed)
        self.assertTrue(new.startswith("[Policy]\nExperimental = false\n[General]\n"), new)
        # [General] on line 1: a key line above it (outside any section) is not ours, and the
        # missing keys go right under the header.
        new, _ = self.m.edit_general("Experimental = false\n[General]\nName = x\n")
        self.assertEqual(
            new, f"Experimental = false\n[General]\nKernelExperimental = {ISO}\nExperimental = true\nName = x\n"
        )
        # The last commented example is the one replaced; a missing key goes under the first [General].
        text = "[General]\n#Experimental = a\n#Experimental = b\n[General]\nName = y\n"
        new, _ = self.m.edit_general(text)
        self.assertEqual(
            new,
            f"[General]\nKernelExperimental = {ISO}\n#Experimental = a\nExperimental = true\n[General]\nName = y\n",
        )
        # An empty file gets the section and ends in a newline; a file without one keeps it that way.
        self.assertEqual(self.m.edit_general("")[0], f"[General]\nExperimental = true\nKernelExperimental = {ISO}\n\n")
        self.assertFalse(self.m.edit_general("[LE]")[0].endswith("\n"))

    def test_crlf_kept(self) -> None:
        new, changed = self.m.edit_general(VENDOR.replace("\n", "\r\n"))
        self.assertTrue(changed)
        self.assertEqual(new.count("\r\n"), VENDOR.count("\n"))

    def test_revert_to_vendor_backup(self) -> None:
        applied, _ = self.m.edit_general(VENDOR)
        # A hand edit made later (in another section) survives the revert.
        applied = applied.replace("#PageScanType =", "PageScanType = 1")
        reverted, changed = self.m.revert_general(applied, VENDOR)
        self.assertTrue(changed)
        self.assertEqual(reverted, VENDOR.replace("#PageScanType =", "PageScanType = 1"))
        self.assertFalse(self.m.revert_general(reverted, VENDOR)[1])

    def test_revert_to_backup_with_old_true(self) -> None:
        backup = VENDOR.replace("#KernelExperimental = false", "KernelExperimental = true")
        applied, _ = self.m.edit_general(backup)
        reverted, _ = self.m.revert_general(applied, backup)
        self.assertEqual(reverted, backup)

    def test_revert_without_key_in_backup_comments_it_out(self) -> None:
        current = f"[General]\nExperimental = true\nKernelExperimental = {ISO}\n"
        reverted, changed = self.m.revert_general(current, "[General]\nName = x\n")
        self.assertTrue(changed)
        self.assertEqual(self.m.current_values(reverted), {"Experimental": None, "KernelExperimental": None})
        # A key the backup had on, now absent, comes back.
        reverted, _ = self.m.revert_general("[General]\nName = y\n", "[General]\nExperimental = true\n")
        self.assertEqual(self.m.current_values(reverted)["Experimental"], "true")

    def test_repeated_general_group_counts_as_one(self) -> None:
        # GLib merges a second [General] into the first: its later line wins.
        text = (
            "[General]\nExperimental = true\nKernelExperimental = "
            + ISO
            + "\n[Policy]\nAutoEnable = true\n[General]\nExperimental = false\n"
        )
        self.assertEqual(self.m.current_values(text), {"Experimental": "false", "KernelExperimental": ISO})
        new, changed = self.m.edit_general(text)
        self.assertTrue(changed)
        self.assertEqual(self.m.current_values(new), {"Experimental": "true", "KernelExperimental": ISO})
        self.assertEqual(
            new.splitlines(),
            [
                "[General]",
                "#Experimental = true",
                f"KernelExperimental = {ISO}",
                "[Policy]",
                "AutoEnable = true",
                "[General]",
                "Experimental = true",
            ],
        )
        self.assertFalse(self.m.edit_general(new)[1])
        # Revert reads the backup's blocks the same way.
        reverted, changed = self.m.revert_general(new, text)
        self.assertTrue(changed)
        self.assertEqual(self.m.current_values(reverted)["Experimental"], "false")
        # A key only in a later [General] block is found too.
        self.assertEqual(self.m.current_values("[BR]\n[General]\nExperimental = true\n")["Experimental"], "true")

    def test_no_final_newline(self) -> None:
        applied, _ = self.m.edit_general(VENDOR)
        bare = applied.rstrip("\n")
        self.assertEqual(self.m.revert_general(bare, bare), (bare, False))
        new, changed = self.m.edit_general(VENDOR.rstrip("\n"))
        self.assertTrue(changed)
        self.assertFalse(new.endswith("\n"))  # the file's ending is kept
        reverted, changed = self.m.revert_general(new, VENDOR)
        self.assertTrue(changed)
        self.assertEqual(reverted, VENDOR.rstrip("\n"))
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        p = os.path.join(tmp, "main.conf")
        with open(p, "w") as f:
            f.write(bare)
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "general-revert", p, p], check=False, capture_output=True, text=True
        )
        self.assertEqual((r.returncode, r.stdout), (0, bare))

    def test_cli_exit_codes(self) -> None:
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        p = os.path.join(tmp, "main.conf")
        with open(p, "w") as f:
            f.write(VENDOR)
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "general-apply", p], check=False, capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 10)
        with open(p, "w") as f:
            f.write(r.stdout)
        r2 = subprocess.run(
            [sys.executable, "-I", HELPER, "general-apply", p], check=False, capture_output=True, text=True
        )
        self.assertEqual((r2.returncode, r2.stdout), (0, r.stdout))
        show = subprocess.run(
            [sys.executable, "-I", HELPER, "general-show", p], check=False, capture_output=True, text=True
        )
        self.assertEqual(show.stdout, f"Experimental\tok\ttrue\nKernelExperimental\tok\t{ISO}\n")


class WirePlumberTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.m = load_helper()

    def test_old_script_file_is_adopted(self) -> None:
        new = self.m.render_wp(MAC, "auto")
        self.assertEqual(self.m.wp_state(OLD_WP, new), "adopt")
        self.assertEqual(self.m.wp_state(new, new), "ok")
        self.assertTrue(new.startswith(self.m.MARKER))
        self.assertEqual(self.m.effective(new), self.m.effective(OLD_WP))

    def test_change_conflict_missing(self) -> None:
        new = self.m.render_wp(MAC, "auto")
        self.assertEqual(self.m.wp_state(OLD_WP.replace('"auto"', '"hq"'), new), "change")
        self.assertEqual(self.m.wp_state(self.m.render_wp(MAC, "hq"), new), "change")
        self.assertEqual(self.m.wp_state(OLD_WP.split("monitor", 1)[1], new), "conflict")  # no marker
        self.assertEqual(self.m.wp_state(None, new), "missing")

    def test_render(self) -> None:
        text = self.m.render_wp(MAC.lower(), "hq")
        self.assertIn('device.name = "bluez_card.0A_1B_2C_3D_4E_5F"', text)
        self.assertIn('bluez5.a2dp.ldac.quality = "hq"', text)
        self.assertEqual(text.count("{"), text.count("}"))
        self.assertEqual(text.count("["), text.count("]"))
        self.assertEqual(self.m.device_id("aa:bb:cc:dd:ee:ff"), "AA_BB_CC_DD_EE_FF")


class SettingsTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.m = load_helper()

    def test_defaults_file(self) -> None:
        with open(DEFAULTS) as f:
            s = self.m.merged_settings([f.read()])
        # No headset: CST ships none (an address in the repository would be one person's).
        self.assertEqual(s, {"DEVICE": "", "LDAC_QUALITY": "auto", "BEARER": "", "LDAC_RATE": "", "LE_AUDIO": "off"})

    def test_user_file_wins_and_parsing(self) -> None:
        user = "# mine\nDEVICE = 'aa:bb:cc:dd:ee:ff'   # other headset\nBEARER=\"le\"\nFOO=1\nnot a line\nLDAC_RATE=48000\n"
        with open(DEFAULTS) as f:
            s = self.m.merged_settings([f.read(), user])
        self.assertEqual(
            s,
            {
                "DEVICE": "aa:bb:cc:dd:ee:ff",
                "LDAC_QUALITY": "auto",
                "BEARER": "le",
                "LDAC_RATE": "48000",
                "LE_AUDIO": "off",
            },
        )
        # A line appended by hand: the later line wins.
        s = self.m.merged_settings([user + "LE_AUDIO=off\n", "LE_AUDIO=on\n"])
        self.assertEqual(s["LE_AUDIO"], "on")
        self.assertEqual(self.m.merged_settings(["LE_AUDIO=off\nLE_AUDIO=on\n"])["LE_AUDIO"], "on")

    def test_one_reader_for_export_lines_and_problem_lines(self) -> None:
        # Bluetooth's own reader saw "export LE_AUDIO" as an unknown key, and settings_set
        # then appended a second LE_AUDIO line instead of replacing the one that counts.
        self.assertEqual(self.m.parse_settings("export LE_AUDIO=on\n"), {"LE_AUDIO": "on"})
        self.assertEqual(self.m.settings_set("export LE_AUDIO=on\n", "LE_AUDIO", "off"), ("LE_AUDIO=off\n", True))
        self.assertEqual(self.m.set_keys("export LDAC_RATE=48000\n"), ["LDAC_RATE"])
        # A value bash would expand is a problem, never a literal: `bt-helper settings` warns and ignores it.
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        p = os.path.join(tmp, "user.conf")
        with open(p, "w") as f:
            f.write("LE_AUDIO=on\nLDAC_RATE=$RATE\n")
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "settings", DEFAULTS, p],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("LE_AUDIO=on\n", r.stdout)
        self.assertIn("LDAC_RATE=\n", r.stdout)
        self.assertEqual(
            r.stderr,
            f"warning: {p} line 2: LDAC_RATE=$RATE: $, ` or \\ outside single quotes: "
            "bash would expand it (use single quotes) (the line is ignored)\n",
        )

    def test_settings_keys(self) -> None:
        self.assertEqual(
            self.m.set_keys("# c\nLDAC_RATE=48000\nFOO=1\nLE_AUDIO=on\nLDAC_RATE=\n"), ["LDAC_RATE", "LE_AUDIO"]
        )
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        p = os.path.join(tmp, "user.conf")
        with open(p, "w") as f:
            f.write("LDAC_RATE=48000\n")
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "settings-keys", p], check=False, capture_output=True, text=True
        )
        self.assertEqual((r.returncode, r.stdout), (0, "LDAC_RATE\n"))
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "settings-keys", p + ".missing"], check=False, capture_output=True, text=True
        )
        self.assertEqual((r.returncode, r.stdout), (0, ""))


class LiveParsingTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.m = load_helper()

    def test_adapter_path(self) -> None:
        objs = {
            "type": "a{oa{sa{sv}}}",
            "data": [
                {
                    "/org/bluez": {"org.bluez.AgentManager1": {}},
                    "/org/bluez/hci1": {"org.bluez.Adapter1": {}},
                    "/org/bluez/hci0": {"org.bluez.Adapter1": {}, "org.bluez.Media1": {}},
                    "/org/bluez/hci0/dev_X": {"org.bluez.Device1": {}},
                }
            ],
        }
        self.assertEqual(self.m.adapter_path(objs), "/org/bluez/hci0")
        self.assertIsNone(self.m.adapter_path({"data": [{}]}))

    def test_card_state(self) -> None:
        dev = "0A_1B_2C_3D_4E_5F"
        cards = [
            {"name": "alsa_card.x"},
            {
                "name": f"bluez_card.{dev}",
                "active_profile": "headset-head-unit",
                "profiles": {"a2dp-sink": {"available": True}},
            },
        ]
        srcs = [{"index": 7, "name": f"bluez_input.{dev}.0"}, {"index": 3, "name": "alsa_input.x"}]
        self.assertEqual(self.m.card_state(dev, cards, srcs, [{"source": 3}]), ("headset-head-unit", True, False))
        card = self.m.card_state(dev, cards, srcs, [{"source": 7}])
        assert card is not None
        self.assertIs(card[2], True)
        self.assertIsNone(self.m.card_state("00_00", cards, srcs, []))

    def test_pw_state(self) -> None:
        dev = "0A_1B_2C_3D_4E_5F"
        objs = [
            {
                "type": "PipeWire:Interface:Device",
                "info": {
                    "props": {"device.name": f"bluez_card.{dev}", "bluez5.a2dp.ldac.quality": "auto"},
                    "params": {"Profile": [{"name": "a2dp-sink"}]},
                },
            },
            {
                "type": "PipeWire:Interface:Node",
                "info": {
                    "props": {"node.name": f"bluez_output.{dev}.1", "api.bluez5.codec": "ldac"},
                    "params": {"Props": [{"volume": 1.0}, {"quality": -1}]},
                },
            },
            {"type": "PipeWire:Interface:Node", "info": {"props": {"node.name": f"bluez_input.{dev}"}}},
        ]
        self.assertEqual(self.m.pw_state(dev, objs), ("a2dp-sink", "auto", "ldac", "auto", None))
        objs[1]["info"]["params"]["Format"] = [{"rate": 96000}]
        pw1 = self.m.pw_state(dev, objs)
        assert pw1 is not None
        self.assertEqual(pw1[4], "96000")
        objs[1]["info"]["params"]["Props"][1]["quality"] = 0
        pw2 = self.m.pw_state(dev, objs)
        assert pw2 is not None
        self.assertEqual(pw2[3], "hq")
        self.assertIsNone(self.m.pw_state("00_00", objs))
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "pw-parse", "00_00"], check=False, input="[]", capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 4)
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "pw-parse", dev],
            check=False,
            input="not json",
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 3)


class LeAudioRenderTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.m = load_helper()

    def test_render(self) -> None:
        text = self.m.render_le_off()
        self.assertTrue(text.startswith(self.m.MARKER))
        eff = self.m.effective(text)
        # override. on the SECTION, a plain key inside: WirePlumber 0.5.19
        # keeps `override.bluez5.roles` literally (ignored by the bluez5
        # monitor) when no earlier file defines the section.
        self.assertEqual(
            eff,
            ["override.monitor.bluez.properties = {", "  bluez5.roles = [ a2dp_sink a2dp_source hfp_hf hfp_ag ]", "}"],
        )
        self.assertNotIn("override.bluez5", text)
        self.assertNotIn("bap", " ".join(eff))
        self.assertEqual(text.count("{"), text.count("}"))
        self.assertEqual(text.count("["), text.count("]"))
        self.assertIn("LE_AUDIO=on", text)  # how to turn it on, in the file itself
        self.assertIn("cst bluetooth le-audio on", text)
        self.assertTrue(self.m.defines_section(text))

    def test_classic_roles_are_pipewire_defaults_without_le(self) -> None:
        # PipeWire 1.6.11 (man pipewire-props): the default bluez5.roles.
        default = [
            "a2dp_sink",
            "a2dp_source",
            "bap_sink",
            "bap_source",
            "bap_bcast_sink",
            "bap_bcast_source",
            "hfp_hf",
            "hfp_ag",
        ]
        self.assertEqual(list(self.m.CLASSIC_ROLES), [r for r in default if not r.startswith("bap_")])

    def test_state(self) -> None:
        text = self.m.render_le_off()
        self.assertEqual(self.m.wp_state(None, text), "missing")
        self.assertEqual(self.m.wp_state(text, text), "ok")
        self.assertEqual(self.m.wp_state(text.replace("# Why", "# Reason"), text), "adopt")
        self.assertEqual(self.m.wp_state(text.replace("hfp_ag ]", "hfp_ag bap_sink ]"), text), "change")
        self.assertEqual(self.m.wp_state(text.split("\n", 1)[1].replace("hfp_ag ]", "]"), text), "conflict")
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        p = os.path.join(tmp, self.m.LE_CONF_NAME)
        r = subprocess.run([sys.executable, "-I", HELPER, "le-state", p], check=False, capture_output=True, text=True)
        self.assertEqual(r.stdout, "missing\n")
        r = subprocess.run([sys.executable, "-I", HELPER, "le-render"], check=False, capture_output=True, text=True)
        with open(p, "w") as f:
            f.write(r.stdout)
        r = subprocess.run([sys.executable, "-I", HELPER, "le-state", p], check=False, capture_output=True, text=True)
        self.assertEqual(r.stdout, "ok\n")


class LeAudioEffectTest(unittest.TestCase):
    """What WirePlumber makes of the files: roles, other fragments, the
    settings line that `le-audio on|off` writes."""

    @override
    def setUp(self) -> None:
        self.m = load_helper()
        self.tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_roles_state(self) -> None:
        self.assertEqual(self.m.roles_state(None), "default")
        self.assertEqual(self.m.roles_state("[ a2dp_sink a2dp_source hfp_hf hfp_ag ]"), "classic")
        self.assertEqual(self.m.roles_state("[a2dp_sink, a2dp_source, hfp_hf, hfp_ag, bap_sink]"), "le")

    def test_wp_roles_without_libwireplumber(self) -> None:
        env = dict(os.environ, CST_BT_LIBWP=os.path.join(self.tmp, "no-such-lib.so"))
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "wp-roles"], check=False, capture_output=True, text=True, env=env
        )
        self.assertEqual((r.returncode, r.stdout), (5, ""))

    def test_defines_section(self) -> None:
        for text in (
            "monitor.bluez.properties = {\n}\n",
            "override.monitor.bluez.properties = { }\n",
            '"monitor.bluez.properties": {\n',
            "  monitor.bluez.properties {\n",
        ):
            self.assertTrue(self.m.defines_section(text), text)
        for text in (
            "# monitor.bluez.properties = {\n",
            "monitor.bluez.rules = [\n",
            "monitor.bluez.properties.x = {\n",
        ):
            self.assertFalse(self.m.defines_section(text), text)

    def test_other_fragments(self) -> None:
        user, etc = os.path.join(self.tmp, "user"), os.path.join(self.tmp, "etc")
        for d in (user, etc):
            os.makedirs(d)
        files = {
            (user, "50-bluez-X.conf"): "monitor.bluez.rules = [ ]\n",
            (user, "51-props.conf"): "monitor.bluez.properties = { bluez5.enable-msbc = true }\n",
            (user, "60-hidden.conf"): "context.properties = { }\n",
            (etc, "60-hidden.conf"): "monitor.bluez.properties = { }\n",  # hidden by the user's file
            (etc, "95-late.conf"): "monitor.bluez.properties = { bluez5.roles = [ bap_sink ] }\n",
            (user, "52-off.conf.bak"): "monitor.bluez.properties = { }\n",  # not *.conf: not read
            (user, self.m.LE_CONF_NAME): self.m.render_le_off(),
        }
        for (d, n), t in files.items():
            with open(os.path.join(d, n), "w") as f:
                f.write(t)
        self.assertEqual(
            self.m.other_fragments(self.m.LE_CONF_NAME, [user, etc, os.path.join(self.tmp, "none")]),
            [("before", os.path.join(user, "51-props.conf")), ("after", os.path.join(etc, "95-late.conf"))],
        )
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "wp-others", self.m.LE_CONF_NAME, user, etc],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.stdout.splitlines(), [f"before|{user}/51-props.conf", f"after|{etc}/95-late.conf"])

    def test_settings_set(self) -> None:
        f = self.m.settings_set
        new, changed = f(None, "LE_AUDIO", "on")
        self.assertTrue(changed)
        self.assertEqual(self.m.parse_settings(new), {"LE_AUDIO": "on"})
        # No final newline: the new line is not glued to the last one.
        self.assertEqual(f("LDAC_RATE=48000", "LE_AUDIO", "on"), ("LDAC_RATE=48000\nLE_AUDIO=on\n", True))
        # The line that counts is replaced in place; earlier ones commented out.
        text = "LE_AUDIO=on\n# c\nLE_AUDIO = on  # mine\nBEARER=bredr\n"
        new, changed = f(text, "LE_AUDIO", "off")
        self.assertEqual(new, "# LE_AUDIO=on\n# c\nLE_AUDIO=off\nBEARER=bredr\n")
        self.assertEqual(f(new, "LE_AUDIO", "off"), (new, False))
        self.assertEqual(f("LE_AUDIO = off  # mine\n", "LE_AUDIO", "off"), ("LE_AUDIO = off  # mine\n", False))
        self.assertEqual(f("A=1\r\n", "LE_AUDIO", "on"), ("A=1\r\nLE_AUDIO=on\r\n", True))
        # A broken line of the key (a problem line, which sets nothing) is replaced, not kept beside
        # the new one; a commented-out one is left alone.
        self.assertEqual(f("LE_AUDIO=$X\n", "LE_AUDIO", "on"), ("LE_AUDIO=on\n", True))
        self.assertEqual(
            f("# LE_AUDIO=$X\nexport LE_AUDIO = 'on' x\n", "LE_AUDIO", "off"), ("# LE_AUDIO=$X\nLE_AUDIO=off\n", True)
        )
        p = os.path.join(self.tmp, "s.conf")
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "settings-set", p, "LE_AUDIO", "on"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(r.returncode, 10)
        self.assertIn("LE_AUDIO=on\n", r.stdout)
        for key, val in (("FOO", "on"), ("LE_AUDIO", "on\nDEVICE=x"), ("LE_AUDIO", "a b")):
            r = subprocess.run(
                [sys.executable, "-I", HELPER, "settings-set", p, key, val], check=False, capture_output=True, text=True
            )
            self.assertEqual(r.returncode, 2, (key, val))


def _libwireplumber() -> bool:
    try:
        import ctypes

        ctypes.CDLL("libwireplumber-0.5.so.0")
        return True
    except OSError:
        return False


@unittest.skipUnless(_libwireplumber(), "libwireplumber-0.5 not installed here (inside a toolbox: run on the host)")
class WirePlumberResolveTest(unittest.TestCase):
    """The CST file as WirePlumber itself merges it (read-only: the config
    is only parsed, nothing is started)."""

    def roles(self, files: Mapping[str, str]) -> str:
        xdg = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, xdg)
        d = os.path.join(xdg, "wireplumber", "wireplumber.conf.d")
        os.makedirs(d)
        for n, t in files.items():
            with open(os.path.join(d, n), "w") as f:
                f.write(t)
        env = dict(os.environ, XDG_CONFIG_HOME=xdg)
        env.pop("WIREPLUMBER_CONFIG_DIR", None)
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "wp-roles"], check=False, capture_output=True, text=True, env=env
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_cst_file_alone(self) -> None:
        m = load_helper()
        self.assertEqual(
            self.roles({m.LE_CONF_NAME: m.render_le_off()}), "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]"
        )

    def test_cst_file_replaces_an_earlier_role_list(self) -> None:
        m = load_helper()
        self.assertEqual(
            self.roles(
                {
                    "80-u.conf": "monitor.bluez.properties = { bluez5.roles = [ bap_sink ] }\n",
                    m.LE_CONF_NAME: m.render_le_off(),
                }
            ),
            "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]",
        )

    def test_the_old_key_override_did_nothing(self) -> None:
        # What CST wrote before: the key kept its literal name, roles unset.
        old = "monitor.bluez.properties = {\n  override.bluez5.roles = [ a2dp_sink ]\n}\n"
        self.assertEqual(self.roles({"90-cst-le-audio-off.conf": old}), "default|")


# The watcher's log of 2026-11-11 on sophia (journalctl --user -o short-iso).
JOURNAL = """\
2026-11-06T10:46:25-03:00 sophia python3[4054]: act: LDAC at 96000 Hz (want 48000): switching a2dp-sink -> a2dp-sink-sbc -> a2dp-sink (about a second of silence)
2026-11-06T10:46:30-03:00 sophia python3[4054]: fixed: LDAC now at 48000 Hz
-- Boot d6773d56d73348d29ed4a68a244ac75b --
2026-11-07T15:04:53-03:00 sophia systemd[3108]: Started cst-bt-ldac48.service - CST: LDAC of headset 0A:1B:2C:3D:4E:5F at 48000 Hz.
2026-11-07T15:04:53-03:00 sophia python3[3215]: watching bluez_card.0A_1B_2C_3D_4E_5F: LDAC wanted at 48000 Hz
2026-11-11T14:11:48-03:00 sophia python3[3215]: act: LDAC at 96000 Hz (want 48000): switching a2dp-sink -> a2dp-sink-sbc -> a2dp-sink (about a second of silence)
2026-11-11T14:11:56-03:00 sophia python3[3215]: a2dp-sink-sbc did not become active within 6 s
2026-11-11T14:11:58-03:00 sophia python3[3215]: failed: LDAC came back at 96000 Hz again; no further attempt until the headset connects again
2026-11-11T14:11:59-03:00 sophia python3[3215]: cooldown: 590 s
2026-11-11T14:14:41-03:00 sophia python3[3215]: stopped
2026-11-11T14:14:41-03:00 sophia systemd[3108]: Stopped cst-bt-ldac48.service - CST: LDAC of headset 0A:1B:2C:3D:4E:5F at 48000 Hz.
"""


class WatcherFailureTest(unittest.TestCase):
    @override
    def setUp(self) -> None:
        self.m = load_helper()

    def test_last_outcome_failed(self) -> None:
        self.assertEqual(
            self.m.lw_failure(JOURNAL),
            (
                "2026-11-11T14:11:58-03:00",
                "LDAC came back at 96000 Hz again; no further attempt until the headset connects again",
                "a2dp-sink-sbc did not become active within 6 s",
            ),
        )

    def test_fixed_afterwards_or_never_failed(self) -> None:
        later = "2026-11-12T09:00:05-03:00 sophia python3[999]: fixed: LDAC now at 48000 Hz\n"
        self.assertIsNone(self.m.lw_failure(JOURNAL + later))
        self.assertIsNone(self.m.lw_failure(JOURNAL.split("-- Boot", maxsplit=1)[0]))
        self.assertIsNone(self.m.lw_failure(""))

    def test_detail_only_from_the_same_attempt(self) -> None:
        text = (
            "T1 h python3[1]: some error of an older attempt\n"
            "T2 h python3[1]: act: LDAC at 96000 Hz\n"
            "T3 h python3[1]: failed: profile switch failed\n"
        )
        self.assertEqual(self.m.lw_failure(text), ("T3", "profile switch failed", ""))

    def test_cli(self) -> None:
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "lw-failure"], check=False, input=JOURNAL, capture_output=True, text=True
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip().split("|")[0], "2026-11-11T14:11:58-03:00")
        r = subprocess.run(
            [sys.executable, "-I", HELPER, "lw-failure"], check=False, input="", capture_output=True, text=True
        )
        self.assertEqual((r.returncode, r.stdout), (1, ""))


class PendingRecordTest(unittest.TestCase):
    """The record of restarts still owed (bluetooth.domain), and bt-helper's restart-pending usage."""

    def test_the_record_is_judged_strictly(self) -> None:
        self.assertEqual(parse_pending(None), {})
        self.assertEqual(parse_pending("not json"), {})
        self.assertEqual(parse_pending("[1]"), {})
        self.assertEqual(parse_pending('{"other.service": "x", "bluetooth.service": "", "wireplumber.service": 3}'), {})
        recorded = {"bluetooth.service": "b1", "wireplumber.service": "w1"}
        self.assertEqual(
            still_pending(recorded, {"bluetooth.service": "b1", "wireplumber.service": "w2"}, ()),
            {"bluetooth.service": "b1"},
        )
        self.assertEqual(
            still_pending({}, {"bluetooth.service": "", "wireplumber.service": "w"}, RESTART_UNITS),
            {"wireplumber.service": "w"},
        )
        self.assertEqual(render_pending({}), "")
        self.assertEqual(parse_pending(render_pending(recorded)), recorded)
        # bt-helper: a mode other than show or save, or too few arguments, is a usage error.
        for args in (["restart-pending", "keep", "p", "a", "b"], ["restart-pending", "show", "p", "a"]):
            r = subprocess.run(
                [sys.executable, "-I", HELPER, *args],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
            )
            self.assertEqual(r.returncode, 2, args)


class _FakeBluetoothHost:
    def __init__(
        self,
        *,
        files: Mapping[str, str] | None = None,
        fragments: list[tuple[str, str]] | None = None,
        roles: tuple[bool, str | None] = (True, None),
        stdin_text: str = "",
    ) -> None:
        self.files = dict(files or {})
        self.fragments = list(fragments or [])
        self.roles = roles
        self.stdin_text = stdin_text

    def read(self, path: str) -> str:
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def read_or_none(self, path: str) -> str | None:
        return self.files.get(path)

    def other_fragments(self, ours: str, dirs: Sequence[str]) -> list[tuple[str, str]]:
        return list(self.fragments)

    def wp_effective_roles(self) -> tuple[bool, str | None]:
        return self.roles

    def read_stdin(self) -> str:
        return self.stdin_text

    def restart_pending(self, path: str, ids: tuple[str, str], skipped: Sequence[str], *, save: bool) -> str:
        owed = still_pending(parse_pending(self.files.get(path)), dict(zip(RESTART_UNITS, ids, strict=True)), skipped)
        if save:
            self.files[path] = render_pending(owed)
        return "".join(f"{u}\n" for u in sorted(owed))


class HexagonalArchitectureTest(unittest.TestCase):
    """Tests directly exercising bluetooth.domain, bluetooth.adapters, and bluetooth.application."""

    def test_classify_other_fragments_handles_priority_shadowing_and_ordering(self) -> None:
        dir_files = [
            (
                "/user/wp.conf.d",
                [
                    ("50-rules.conf", "monitor.bluez.rules = []\n"),
                    ("51-props.conf", "monitor.bluez.properties = { bluez5.enable-msbc = true }\n"),
                    ("60-shadowed.conf", "context.properties = {}\n"),
                    ("80-notes.txt", "monitor.bluez.properties = {}\n"),
                    ("90-cst-le-audio-off.conf", "override.monitor.bluez.properties = {}\n"),
                ],
            ),
            (
                "/etc/wp.conf.d",
                [
                    ("60-shadowed.conf", "monitor.bluez.properties = {}\n"),
                    ("95-late.conf", "override.monitor.bluez.properties = { bluez5.roles = [ bap_sink ] }\n"),
                ],
            ),
        ]
        self.assertEqual(
            classify_other_fragments("90-cst-le-audio-off.conf", dir_files),
            [
                ("before", "/user/wp.conf.d/51-props.conf"),
                ("after", "/etc/wp.conf.d/95-late.conf"),
            ],
        )

    def test_local_adapter_and_application_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="bt-hex-") as tmp:
            main_conf = os.path.join(tmp, "main.conf")
            with open(main_conf, "w", encoding="utf-8") as fh:
                fh.write(VENDOR)
            host = LocalBluetoothHostAdapter()
            out, err = io.StringIO(), io.StringIO()
            rc = bt_app.main(["general-apply", main_conf], host, stdout=out, stderr=err)
            self.assertEqual(rc, 10)
            self.assertEqual(err.getvalue(), "")
            with open(main_conf, "w", encoding="utf-8") as fh:
                fh.write(out.getvalue())

            show_out = io.StringIO()
            rc_show = bt_app.main(["general-show", main_conf], host, stdout=show_out, stderr=err)
            self.assertEqual(rc_show, 0)
            self.assertIn(f"KernelExperimental\tok\t{ISO}", show_out.getvalue())

    def test_application_main_with_fake_port(self) -> None:
        fake = _FakeBluetoothHost(roles=(False, "missing libwireplumber"), stdin_text=JOURNAL)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(bt_app.main(["wp-roles"], fake, stdout=out, stderr=err), 5)
        self.assertIn("missing libwireplumber", err.getvalue())

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(bt_app.main(["lw-failure"], fake, stdout=out, stderr=err), 0)
        self.assertTrue(out.getvalue().startswith("2026-11-11T14:11:58-03:00|"))

        out, err = io.StringIO(), io.StringIO()
        # No device-id command, as on main (only the watcher uses domain.device_id): a usage error.
        self.assertEqual(bt_app.main(["device-id", MAC.lower()], fake, stdout=out, stderr=err), 2)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("usage", err.getvalue())

        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(bt_app.main(["unknown"], fake, stdout=out, stderr=err), 2)
        self.assertIn("usage", err.getvalue())


class WatcherRunTest(unittest.TestCase):
    """run(): the watcher's (exit status, stdout) of pw-dump and pactl, also when they did not run."""

    def test_answers(self) -> None:
        self.assertEqual(bt_adapters.run([sys.executable, "-c", "print('hi'); raise SystemExit(4)"]), (4, "hi\n"))
        self.assertEqual(bt_adapters.run(["/nonexistent/cst-pactl"]), (127, ""))
        self.assertEqual(bt_adapters.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3), (124, ""))


class UnreadableInputTest(unittest.TestCase):
    """bt-helper on a file it cannot read or decode: one line on stderr, exit 1 (no traceback)."""

    def helper(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", HELPER, *args], check=False, input=stdin, capture_output=True, text=True
        )

    def test_a_file_it_cannot_read_is_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "main.conf")
            with open(p, "w", encoding="utf-8") as f:
                f.write("[General]\n")
            os.chmod(p, 0)
            for args in (["general-show", p], ["general-apply", p], ["settings", DEFAULTS, p], ["settings-keys", p]):
                r = self.helper(*args)
                self.assertEqual((r.returncode, r.stdout), (1, ""), args)
                self.assertEqual(r.stderr, f"bt-helper: [Errno 13] Permission denied: '{p}'\n", args)

    def test_a_file_that_is_not_utf8_is_one_line(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "50-bluez.conf")
            with open(p, "wb") as f:
                f.write(b"# \xff\n")
            for args in (
                ["wp-state", p, "AA:BB:CC:DD:EE:FF", "auto"],
                ["le-state", p],
                ["settings-set", p, "LE_AUDIO", "on"],
            ):
                r = self.helper(*args)
                self.assertEqual((r.returncode, r.stdout), (1, ""), args)
                self.assertEqual(
                    r.stderr,
                    "bt-helper: 'utf-8' codec can't decode byte 0xff in position 2: invalid start byte\n",
                    args,
                )


class NotUtf8OutputTest(unittest.TestCase):
    """run(): bytes of pw-dump's or pactl's output that are not UTF-8 become U+FFFD (they raised UnicodeDecodeError)."""

    def test_replaced(self) -> None:
        self.assertEqual(bt_adapters.run(["printf", "\\377abc"]), (0, "\ufffdabc"))


if __name__ == "__main__":
    unittest.main()

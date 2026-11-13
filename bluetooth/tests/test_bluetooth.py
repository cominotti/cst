"""Unit tests for bluetooth/bt-helper (main.conf editing, the WirePlumber
rule, settings, parsing of busctl/pactl/pw-dump output) and for the option
checks of bluetooth-setup.sh. No root, no Bluetooth, no service is touched.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s bluetooth/tests -t . -v
"""

import importlib.machinery
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
BT = os.path.dirname(HERE)
REPO = os.path.dirname(BT)
HELPER = os.path.join(BT, "bt-helper")
WATCH = os.path.join(BT, "bt-ldac-watch")
SETUP = os.path.join(REPO, "bluetooth-setup.sh")
DEFAULTS = os.path.join(BT, "bluetooth.conf")

ISO = "6fbaf188-05e0-496a-9885-d6ddfdb4e03e"
MAC = "38:D5:18:CD:AA:4C"

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
# Fone 38:D5:18:CD:AA:4C: qualidade LDAC "auto" quando conectado por A2DP.
# "auto" = taxa adaptativa (990/660/330 kbps); "hq" fixo travava o Beam 4.
# LE Audio (BAP/LC3) vem do BlueZ (Experimental/KernelExperimental em
# /etc/bluetooth/main.conf); os perfis bap-* aparecem quando o fone conecta via LE.
# Gerado por apply-le-audio.sh — edite o script, não este arquivo.
monitor.bluez.rules = [
  {
    matches = [ { device.name = "bluez_card.38_D5_18_CD_AA_4C" } ]
    actions = {
      update-props = {
        bluez5.a2dp.ldac.quality = "auto"
      }
    }
  }
]
"""


def load(path=HELPER, name="bt_helper"):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def general(text):
    """The [General] lines of TEXT."""
    out = []
    for line in text.splitlines():
        if line.startswith("[") and out:
            break
        out.append(line)
    return out


class MainConfTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_vendor_examples_replaced_in_place(self):
        new, changed = self.m.edit_general(VENDOR)
        self.assertTrue(changed)
        old_lines, new_lines = VENDOR.splitlines(), new.splitlines()
        self.assertEqual(len(old_lines), len(new_lines))            # nothing added or lost
        diff = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
        self.assertEqual(diff, [("#Experimental = false", "Experimental = true"),
                                ("#KernelExperimental = false", f"KernelExperimental = {ISO}")])
        self.assertIn("#Experimental = false", new.split("[LE]")[1])  # other sections untouched

    def test_idempotent(self):
        once, _ = self.m.edit_general(VENDOR)
        twice, changed = self.m.edit_general(once)
        self.assertFalse(changed)
        self.assertEqual(once, twice)

    def test_old_true_becomes_the_iso_uuid(self):
        text = VENDOR.replace("#Experimental = false", "Experimental = true").replace(
            "#KernelExperimental = false", "KernelExperimental = true")
        new, changed = self.m.edit_general(text)
        self.assertTrue(changed)
        self.assertIn(f"KernelExperimental = {ISO}", new)
        self.assertNotIn("KernelExperimental = true", new)
        self.assertEqual(self.m.current_values(new),
                         {"Experimental": "true", "KernelExperimental": ISO})

    def test_uuid_list_with_iso_is_kept(self):
        other = "d4992530-b9ec-469f-ab01-6c481c47da1c"
        text = VENDOR.replace("#Experimental = false", "Experimental = true").replace(
            "#KernelExperimental = false", f"KernelExperimental = {other},{ISO.upper()}")
        new, changed = self.m.edit_general(text)
        self.assertFalse(changed)
        self.assertEqual(new, text)

    def test_last_active_line_wins_and_earlier_ones_are_commented(self):
        text = "[General]\nExperimental = false\nName = x\nExperimental = false\n[BR]\n"
        new, changed = self.m.edit_general(text)
        self.assertTrue(changed)
        g = general(new)
        self.assertEqual(g.count("Experimental = true"), 1)
        self.assertIn("#Experimental = false", g)
        self.assertEqual(g, ["[General]", f"KernelExperimental = {ISO}", "#Experimental = false", "Name = x", "Experimental = true"])
        self.assertIn("Name = x", g)

    def test_missing_key_goes_under_general_and_missing_section_on_top(self):
        new, changed = self.m.edit_general("[General]\nName = x\n\n[BR]\n")
        self.assertTrue(changed)
        self.assertEqual(general(new)[:3], ["[General]", f"KernelExperimental = {ISO}", "Experimental = true"])
        new, changed = self.m.edit_general("# top comment\n[LE]\nFoo = 1\n")
        self.assertTrue(changed)
        self.assertTrue(new.startswith(f"[General]\nExperimental = true\nKernelExperimental = {ISO}\n\n# top comment\n"))
        self.assertFalse(self.m.edit_general(new)[1])

    def test_crlf_kept(self):
        new, changed = self.m.edit_general(VENDOR.replace("\n", "\r\n"))
        self.assertTrue(changed)
        self.assertEqual(new.count("\r\n"), VENDOR.count("\n"))

    def test_revert_to_vendor_backup(self):
        applied, _ = self.m.edit_general(VENDOR)
        # A hand edit made later (in another section) survives the revert.
        applied = applied.replace("#PageScanType =", "PageScanType = 1")
        reverted, changed = self.m.revert_general(applied, VENDOR)
        self.assertTrue(changed)
        self.assertEqual(reverted, VENDOR.replace("#PageScanType =", "PageScanType = 1"))
        self.assertFalse(self.m.revert_general(reverted, VENDOR)[1])

    def test_revert_to_backup_with_old_true(self):
        backup = VENDOR.replace("#KernelExperimental = false", "KernelExperimental = true")
        applied, _ = self.m.edit_general(backup)
        reverted, _ = self.m.revert_general(applied, backup)
        self.assertEqual(reverted, backup)

    def test_revert_without_key_in_backup_comments_it_out(self):
        current = f"[General]\nExperimental = true\nKernelExperimental = {ISO}\n"
        reverted, changed = self.m.revert_general(current, "[General]\nName = x\n")
        self.assertTrue(changed)
        self.assertEqual(self.m.current_values(reverted), {"Experimental": None, "KernelExperimental": None})
        # A key the backup had on, now absent, comes back.
        reverted, _ = self.m.revert_general("[General]\nName = y\n", "[General]\nExperimental = true\n")
        self.assertEqual(self.m.current_values(reverted)["Experimental"], "true")

    def test_repeated_general_group_counts_as_one(self):
        # GLib merges a second [General] into the first: its later line wins.
        text = ("[General]\nExperimental = true\nKernelExperimental = " + ISO +
                "\n[Policy]\nAutoEnable = true\n[General]\nExperimental = false\n")
        self.assertEqual(self.m.current_values(text), {"Experimental": "false", "KernelExperimental": ISO})
        new, changed = self.m.edit_general(text)
        self.assertTrue(changed)
        self.assertEqual(self.m.current_values(new), {"Experimental": "true", "KernelExperimental": ISO})
        self.assertEqual(new.splitlines(), ["[General]", "#Experimental = true", f"KernelExperimental = {ISO}",
                                            "[Policy]", "AutoEnable = true", "[General]", "Experimental = true"])
        self.assertFalse(self.m.edit_general(new)[1])
        # Revert reads the backup's blocks the same way.
        reverted, changed = self.m.revert_general(new, text)
        self.assertTrue(changed)
        self.assertEqual(self.m.current_values(reverted)["Experimental"], "false")
        # A key only in a later [General] block is found too.
        self.assertEqual(self.m.current_values("[BR]\n[General]\nExperimental = true\n")["Experimental"], "true")

    def test_no_final_newline(self):
        applied, _ = self.m.edit_general(VENDOR)
        bare = applied.rstrip("\n")
        self.assertEqual(self.m.revert_general(bare, bare), (bare, False))
        new, changed = self.m.edit_general(VENDOR.rstrip("\n"))
        self.assertTrue(changed)
        self.assertFalse(new.endswith("\n"))                       # the file's ending is kept
        reverted, changed = self.m.revert_general(new, VENDOR)
        self.assertTrue(changed)
        self.assertEqual(reverted, VENDOR.rstrip("\n"))
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        p = os.path.join(tmp, "main.conf")
        with open(p, "w") as f:
            f.write(bare)
        r = subprocess.run([sys.executable, "-I", HELPER, "general-revert", p, p], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, bare))

    def test_cli_exit_codes(self):
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        p = os.path.join(tmp, "main.conf")
        with open(p, "w") as f:
            f.write(VENDOR)
        r = subprocess.run([sys.executable, "-I", HELPER, "general-apply", p], capture_output=True, text=True)
        self.assertEqual(r.returncode, 10)
        with open(p, "w") as f:
            f.write(r.stdout)
        r2 = subprocess.run([sys.executable, "-I", HELPER, "general-apply", p], capture_output=True, text=True)
        self.assertEqual((r2.returncode, r2.stdout), (0, r.stdout))
        show = subprocess.run([sys.executable, "-I", HELPER, "general-show", p], capture_output=True, text=True)
        self.assertEqual(show.stdout, f"Experimental\tok\ttrue\nKernelExperimental\tok\t{ISO}\n")


class WirePlumberTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_old_script_file_is_adopted(self):
        new = self.m.render_wp(MAC, "auto")
        self.assertEqual(self.m.wp_state(OLD_WP, new), "adopt")
        self.assertEqual(self.m.wp_state(new, new), "ok")
        self.assertTrue(new.startswith(self.m.MARKER))
        self.assertEqual(self.m.effective(new), self.m.effective(OLD_WP))

    def test_change_conflict_missing(self):
        new = self.m.render_wp(MAC, "auto")
        self.assertEqual(self.m.wp_state(OLD_WP.replace('"auto"', '"hq"'), new), "change")
        self.assertEqual(self.m.wp_state(self.m.render_wp(MAC, "hq"), new), "change")
        self.assertEqual(self.m.wp_state(OLD_WP.split("monitor", 1)[1], new), "conflict")  # no marker
        self.assertEqual(self.m.wp_state(None, new), "missing")

    def test_render(self):
        text = self.m.render_wp(MAC.lower(), "hq")
        self.assertIn('device.name = "bluez_card.38_D5_18_CD_AA_4C"', text)
        self.assertIn('bluez5.a2dp.ldac.quality = "hq"', text)
        self.assertEqual(text.count("{"), text.count("}"))
        self.assertEqual(text.count("["), text.count("]"))
        self.assertEqual(self.m.device_id("aa:bb:cc:dd:ee:ff"), "AA_BB_CC_DD_EE_FF")


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_defaults_file(self):
        with open(DEFAULTS) as f:
            s = self.m.merged_settings([f.read()])
        self.assertEqual(s, {"DEVICE": MAC, "LDAC_QUALITY": "auto", "BEARER": "", "LDAC_RATE": "",
                             "LE_AUDIO": "off"})

    def test_user_file_wins_and_parsing(self):
        user = "# mine\nDEVICE = 'aa:bb:cc:dd:ee:ff'   # other headset\nBEARER=\"le\"\nFOO=1\nnot a line\nLDAC_RATE=48000\n"
        with open(DEFAULTS) as f:
            s = self.m.merged_settings([f.read(), user])
        self.assertEqual(s, {"DEVICE": "aa:bb:cc:dd:ee:ff", "LDAC_QUALITY": "auto", "BEARER": "le",
                             "LDAC_RATE": "48000", "LE_AUDIO": "off"})
        # A line appended by hand: the later line wins.
        s = self.m.merged_settings([user + "LE_AUDIO=off\n", "LE_AUDIO=on\n"])
        self.assertEqual(s["LE_AUDIO"], "on")
        self.assertEqual(self.m.merged_settings(["LE_AUDIO=off\nLE_AUDIO=on\n"])["LE_AUDIO"], "on")

    def test_settings_keys(self):
        self.assertEqual(self.m.set_keys("# c\nLDAC_RATE=48000\nFOO=1\nLE_AUDIO=on\nLDAC_RATE=\n"),
                         ["LDAC_RATE", "LE_AUDIO"])
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        p = os.path.join(tmp, "user.conf")
        with open(p, "w") as f:
            f.write("LDAC_RATE=48000\n")
        r = subprocess.run([sys.executable, "-I", HELPER, "settings-keys", p], capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, "LDAC_RATE\n"))
        r = subprocess.run([sys.executable, "-I", HELPER, "settings-keys", p + ".missing"],
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))

    def test_shell_and_helper_agree(self):
        with open(SETUP) as f:
            sh = f.read()
        self.assertIn(f'ISO_UUID="{self.m.ISO_UUID}"', sh)
        self.assertIn(f'WP_MARKER="{self.m.MARKER}"', sh)
        self.assertIn(f'OLD_WP_MARKER="{self.m.OLD_MARKERS[0]}"', sh)
        self.assertEqual(self.m.ISO_UUID, ISO)
        self.assertIn(f'LW_UNIT="{self.m.LDAC_UNIT}"', sh)
        # The unit runs the copy the script installs.
        self.assertTrue(self.m.LDAC_PROG.startswith("%h/"))
        self.assertIn('LW_PROG="${HOME}/' + self.m.LDAC_PROG[3:] + '"', sh)
        self.assertIn('LE_CONF="${WP_CONF_DIR}/' + self.m.LE_CONF_NAME + '"', sh)


class LiveParsingTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_adapter_path(self):
        objs = {"type": "a{oa{sa{sv}}}", "data": [{
            "/org/bluez": {"org.bluez.AgentManager1": {}},
            "/org/bluez/hci1": {"org.bluez.Adapter1": {}},
            "/org/bluez/hci0": {"org.bluez.Adapter1": {}, "org.bluez.Media1": {}},
            "/org/bluez/hci0/dev_X": {"org.bluez.Device1": {}}}]}
        self.assertEqual(self.m.adapter_path(objs), "/org/bluez/hci0")
        self.assertIsNone(self.m.adapter_path({"data": [{}]}))

    def test_card_state(self):
        dev = "38_D5_18_CD_AA_4C"
        cards = [{"name": "alsa_card.x"}, {"name": f"bluez_card.{dev}", "active_profile": "headset-head-unit",
                                          "profiles": {"a2dp-sink": {"available": True}}}]
        srcs = [{"index": 7, "name": f"bluez_input.{dev}.0"}, {"index": 3, "name": "alsa_input.x"}]
        self.assertEqual(self.m.card_state(dev, cards, srcs, [{"source": 3}]), ("headset-head-unit", True, False))
        self.assertEqual(self.m.card_state(dev, cards, srcs, [{"source": 7}])[2], True)
        self.assertIsNone(self.m.card_state("00_00", cards, srcs, []))

    def test_pw_state(self):
        dev = "38_D5_18_CD_AA_4C"
        objs = [
            {"type": "PipeWire:Interface:Device", "info": {
                "props": {"device.name": f"bluez_card.{dev}", "bluez5.a2dp.ldac.quality": "auto"},
                "params": {"Profile": [{"name": "a2dp-sink"}]}}},
            {"type": "PipeWire:Interface:Node", "info": {
                "props": {"node.name": f"bluez_output.{dev}.1", "api.bluez5.codec": "ldac"},
                "params": {"Props": [{"volume": 1.0}, {"quality": -1}]}}},
            {"type": "PipeWire:Interface:Node", "info": {"props": {"node.name": f"bluez_input.{dev}"}}},
        ]
        self.assertEqual(self.m.pw_state(dev, objs), ("a2dp-sink", "auto", "ldac", "auto", None))
        objs[1]["info"]["params"]["Format"] = [{"rate": 96000}]
        self.assertEqual(self.m.pw_state(dev, objs)[4], "96000")
        objs[1]["info"]["params"]["Props"][1]["quality"] = 0
        self.assertEqual(self.m.pw_state(dev, objs)[3], "hq")
        self.assertIsNone(self.m.pw_state("00_00", objs))
        r = subprocess.run([sys.executable, "-I", HELPER, "pw-parse", "00_00"], input="[]",
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 4)
        r = subprocess.run([sys.executable, "-I", HELPER, "pw-parse", dev], input="not json",
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)


class LeAudioRenderTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_render(self):
        text = self.m.render_le_off()
        self.assertTrue(text.startswith(self.m.MARKER))
        eff = self.m.effective(text)
        # override. on the SECTION, a plain key inside: WirePlumber 0.5.19
        # keeps `override.bluez5.roles` literally (ignored by the bluez5
        # monitor) when no earlier file defines the section.
        self.assertEqual(eff, ["override.monitor.bluez.properties = {",
                               "  bluez5.roles = [ a2dp_sink a2dp_source hfp_hf hfp_ag ]",
                               "}"])
        self.assertNotIn("override.bluez5", text)
        self.assertNotIn("bap", " ".join(eff))
        self.assertEqual(text.count("{"), text.count("}"))
        self.assertEqual(text.count("["), text.count("]"))
        self.assertIn("LE_AUDIO=on", text)            # how to turn it on, in the file itself
        self.assertIn("cst bluetooth le-audio on", text)
        self.assertTrue(self.m.defines_section(text))

    def test_classic_roles_are_pipewire_defaults_without_le(self):
        # PipeWire 1.6.11 (man pipewire-props): the default bluez5.roles.
        default = "a2dp_sink a2dp_source bap_sink bap_source bap_bcast_sink bap_bcast_source hfp_hf hfp_ag".split()
        self.assertEqual(list(self.m.CLASSIC_ROLES), [r for r in default if not r.startswith("bap_")])

    def test_state(self):
        text = self.m.render_le_off()
        self.assertEqual(self.m.wp_state(None, text), "missing")
        self.assertEqual(self.m.wp_state(text, text), "ok")
        self.assertEqual(self.m.wp_state(text.replace("# Why", "# Reason"), text), "adopt")
        self.assertEqual(self.m.wp_state(text.replace("hfp_ag ]", "hfp_ag bap_sink ]"), text), "change")
        self.assertEqual(self.m.wp_state(text.split("\n", 1)[1].replace("hfp_ag ]", "]"), text), "conflict")
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        p = os.path.join(tmp, self.m.LE_CONF_NAME)
        r = subprocess.run([sys.executable, "-I", HELPER, "le-state", p], capture_output=True, text=True)
        self.assertEqual(r.stdout, "missing\n")
        r = subprocess.run([sys.executable, "-I", HELPER, "le-render"], capture_output=True, text=True)
        with open(p, "w") as f:
            f.write(r.stdout)
        r = subprocess.run([sys.executable, "-I", HELPER, "le-state", p], capture_output=True, text=True)
        self.assertEqual(r.stdout, "ok\n")


class LeAudioEffectTest(unittest.TestCase):
    """What WirePlumber makes of the files: roles, other fragments, the
    settings line that `le-audio on|off` writes."""

    def setUp(self):
        self.m = load()
        self.tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_roles_state(self):
        self.assertEqual(self.m.roles_state(None), "default")
        self.assertEqual(self.m.roles_state("[ a2dp_sink a2dp_source hfp_hf hfp_ag ]"), "classic")
        self.assertEqual(self.m.roles_state("[a2dp_sink, a2dp_source, hfp_hf, hfp_ag, bap_sink]"), "le")

    def test_wp_roles_without_libwireplumber(self):
        env = dict(os.environ, CST_BT_LIBWP=os.path.join(self.tmp, "no-such-lib.so"))
        r = subprocess.run([sys.executable, "-I", HELPER, "wp-roles"], capture_output=True, text=True, env=env)
        self.assertEqual((r.returncode, r.stdout), (5, ""))

    def test_defines_section(self):
        for text in ("monitor.bluez.properties = {\n}\n", "override.monitor.bluez.properties = { }\n",
                     '"monitor.bluez.properties": {\n', "  monitor.bluez.properties {\n"):
            self.assertTrue(self.m.defines_section(text), text)
        for text in ("# monitor.bluez.properties = {\n", "monitor.bluez.rules = [\n",
                     "monitor.bluez.properties.x = {\n"):
            self.assertFalse(self.m.defines_section(text), text)

    def test_other_fragments(self):
        user, etc = os.path.join(self.tmp, "user"), os.path.join(self.tmp, "etc")
        for d in (user, etc):
            os.makedirs(d)
        files = {
            (user, "50-bluez-X.conf"): "monitor.bluez.rules = [ ]\n",
            (user, "51-props.conf"): "monitor.bluez.properties = { bluez5.enable-msbc = true }\n",
            (user, "60-hidden.conf"): "context.properties = { }\n",
            (etc, "60-hidden.conf"): "monitor.bluez.properties = { }\n",     # hidden by the user's file
            (etc, "95-late.conf"): "monitor.bluez.properties = { bluez5.roles = [ bap_sink ] }\n",
            (user, "52-off.conf.bak"): "monitor.bluez.properties = { }\n",   # not *.conf: not read
            (user, self.m.LE_CONF_NAME): self.m.render_le_off(),
        }
        for (d, n), t in files.items():
            with open(os.path.join(d, n), "w") as f:
                f.write(t)
        self.assertEqual(self.m.other_fragments(self.m.LE_CONF_NAME, [user, etc, os.path.join(self.tmp, "none")]),
                         [("before", os.path.join(user, "51-props.conf")), ("after", os.path.join(etc, "95-late.conf"))])
        r = subprocess.run([sys.executable, "-I", HELPER, "wp-others", self.m.LE_CONF_NAME, user, etc],
                           capture_output=True, text=True)
        self.assertEqual(r.stdout.splitlines(), [f"before|{user}/51-props.conf", f"after|{etc}/95-late.conf"])

    def test_settings_set(self):
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
        p = os.path.join(self.tmp, "s.conf")
        r = subprocess.run([sys.executable, "-I", HELPER, "settings-set", p, "LE_AUDIO", "on"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 10)
        self.assertIn("LE_AUDIO=on\n", r.stdout)
        for key, val in (("FOO", "on"), ("LE_AUDIO", "on\nDEVICE=x"), ("LE_AUDIO", "a b")):
            r = subprocess.run([sys.executable, "-I", HELPER, "settings-set", p, key, val],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, (key, val))


def _libwireplumber():
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

    def roles(self, files):
        xdg = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, xdg)
        d = os.path.join(xdg, "wireplumber", "wireplumber.conf.d")
        os.makedirs(d)
        for n, t in files.items():
            with open(os.path.join(d, n), "w") as f:
                f.write(t)
        env = dict(os.environ, XDG_CONFIG_HOME=xdg)
        env.pop("WIREPLUMBER_CONFIG_DIR", None)
        r = subprocess.run([sys.executable, "-I", HELPER, "wp-roles"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def test_cst_file_alone(self):
        m = load()
        self.assertEqual(self.roles({m.LE_CONF_NAME: m.render_le_off()}),
                         "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]")

    def test_cst_file_replaces_an_earlier_role_list(self):
        m = load()
        self.assertEqual(self.roles({"80-u.conf": "monitor.bluez.properties = { bluez5.roles = [ bap_sink ] }\n",
                                     m.LE_CONF_NAME: m.render_le_off()}),
                         "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]")

    def test_the_old_key_override_did_nothing(self):
        # What CST wrote before: the key kept its literal name, roles unset.
        old = "monitor.bluez.properties = {\n  override.bluez5.roles = [ a2dp_sink ]\n}\n"
        self.assertEqual(self.roles({"90-cst-le-audio-off.conf": old}), "default|")


# The watcher's log of 2026-11-11 on sophia (journalctl --user -o short-iso).
JOURNAL = """\
2026-11-06T10:46:25-03:00 sophia python3[4054]: act: LDAC at 96000 Hz (want 48000): switching a2dp-sink -> a2dp-sink-sbc -> a2dp-sink (about a second of silence)
2026-11-06T10:46:30-03:00 sophia python3[4054]: fixed: LDAC now at 48000 Hz
-- Boot d6773d56d73348d29ed4a68a244ac75b --
2026-11-07T15:04:53-03:00 sophia systemd[3108]: Started cst-bt-ldac48.service - CST: LDAC of headset 38:D5:18:CD:AA:4C at 48000 Hz.
2026-11-07T15:04:53-03:00 sophia python3[3215]: watching bluez_card.38_D5_18_CD_AA_4C: LDAC wanted at 48000 Hz
2026-11-11T14:11:48-03:00 sophia python3[3215]: act: LDAC at 96000 Hz (want 48000): switching a2dp-sink -> a2dp-sink-sbc -> a2dp-sink (about a second of silence)
2026-11-11T14:11:56-03:00 sophia python3[3215]: a2dp-sink-sbc did not become active within 6 s
2026-11-11T14:11:58-03:00 sophia python3[3215]: failed: LDAC came back at 96000 Hz again; no further attempt until the headset connects again
2026-11-11T14:11:59-03:00 sophia python3[3215]: cooldown: 590 s
2026-11-11T14:14:41-03:00 sophia python3[3215]: stopped
2026-11-11T14:14:41-03:00 sophia systemd[3108]: Stopped cst-bt-ldac48.service - CST: LDAC of headset 38:D5:18:CD:AA:4C at 48000 Hz.
"""


class WatcherFailureTest(unittest.TestCase):
    def setUp(self):
        self.m = load()

    def test_last_outcome_failed(self):
        self.assertEqual(self.m.lw_failure(JOURNAL), (
            "2026-11-11T14:11:58-03:00",
            "LDAC came back at 96000 Hz again; no further attempt until the headset connects again",
            "a2dp-sink-sbc did not become active within 6 s"))

    def test_fixed_afterwards_or_never_failed(self):
        later = "2026-11-12T09:00:05-03:00 sophia python3[999]: fixed: LDAC now at 48000 Hz\n"
        self.assertIsNone(self.m.lw_failure(JOURNAL + later))
        self.assertIsNone(self.m.lw_failure(JOURNAL.split("-- Boot")[0]))
        self.assertIsNone(self.m.lw_failure(""))

    def test_detail_only_from_the_same_attempt(self):
        text = ("T1 h python3[1]: some error of an older attempt\n"
                "T2 h python3[1]: act: LDAC at 96000 Hz\n"
                "T3 h python3[1]: failed: profile switch failed\n")
        self.assertEqual(self.m.lw_failure(text), ("T3", "profile switch failed", ""))

    def test_cli(self):
        r = subprocess.run([sys.executable, "-I", HELPER, "lw-failure"], input=JOURNAL,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip().split("|")[0], "2026-11-11T14:11:58-03:00")
        r = subprocess.run([sys.executable, "-I", HELPER, "lw-failure"], input="",
                           capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (1, ""))


def shell_functions(*names):
    """The text of the named top-level functions of bluetooth-setup.sh."""
    with open(SETUP) as f:
        src = f.read()
    out = []
    for n in names:
        m = re.search(rf"^{n}\(\) {{\n.*?^}}\n", src, re.M | re.S)
        assert m, n
        out.append(m.group(0))
    return "".join(out)


class RestartTest(unittest.TestCase):
    """do_restarts runs in an `||` list (no set -e there): failures must be
    returned by hand. sudo and systemctl are stubs; nothing is restarted."""

    STUBS = """
set -Eeuo pipefail
PROG=test; NO_RESTART=0; YES=1
log() { echo "$*" >>"$LOG"; }
sudo() { log "sudo $*"; return "$SUDO_RC"; }
systemctl() { log "systemctl $*"; return "$WP_RC"; }
wait_for() { [[ "$2" == bap_endpoint_registered ]] && log "wait bap"; return 0; }
adapter_path() { echo /org/bluez/hci0; }
restore_a2dp() { log restore_a2dp; }
restart_hint() { echo hint; }
le_off_active() { [[ "${LE_OFF:-0}" == 1 ]]; }
"""

    def run_restarts(self, sudo_rc, wp_rc, bt=1, wp=1, le_off=0, no_restart=0):
        tmp = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, tmp)
        log = os.path.join(tmp, "log")
        script = (self.STUBS + shell_functions("restart_bluetooth", "restart_wireplumber",
                                               "confirm_bt_restart", "do_restarts") +
                  f'NO_RESTART={no_restart}; rc=0; do_restarts {bt} {wp} || rc=$?; echo "rc=$rc"\n')
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LOG": log,
               "SUDO_RC": str(sudo_rc), "WP_RC": str(wp_rc), "LE_OFF": str(le_off)}
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
        calls = []
        if os.path.exists(log):
            with open(log) as f:
                calls = f.read().splitlines()
        return r, calls

    def test_ok(self):
        r, calls = self.run_restarts(0, 0)
        self.assertIn("rc=0", r.stdout)
        self.assertEqual(calls, ["sudo systemctl restart bluetooth", "systemctl --user restart wireplumber", "wait bap",
                                 "restore_a2dp"])

    def test_no_bap_wait_with_le_audio_off(self):
        r, calls = self.run_restarts(0, 0, bt=0, le_off=1)
        self.assertIn("rc=0", r.stdout)
        self.assertEqual(calls, ["systemctl --user restart wireplumber", "restore_a2dp"])

    def test_no_restart_hint_names_only_what_is_needed(self):
        r, calls = self.run_restarts(0, 0, bt=0, no_restart=1)
        self.assertEqual(calls, [])
        self.assertIn("To finish: systemctl --user restart wireplumber", r.stdout)
        self.assertNotIn("hint", r.stdout)
        r, _ = self.run_restarts(0, 0, bt=1, no_restart=1)
        self.assertIn("To finish: hint", r.stdout)

    def test_failed_bluetooth_restart_stops_before_wireplumber(self):
        r, calls = self.run_restarts(1, 0)
        self.assertIn("rc=2", r.stdout)
        self.assertEqual(calls, ["sudo systemctl restart bluetooth"])
        self.assertIn("bluetooth.service was not restarted", r.stderr)

    def test_failed_wireplumber_restart(self):
        r, calls = self.run_restarts(0, 1, bt=0)
        self.assertIn("rc=2", r.stdout)
        self.assertEqual(calls, ["systemctl --user restart wireplumber"])
        self.assertIn("wireplumber was not restarted", r.stderr)


class LeAudioShellTest(unittest.TestCase):
    """le_plan, le_report, apply_le, plan, bearer_check and revert of
    bluetooth-setup.sh in a scratch HOME. Every system call is a stub; the
    scratch main.conf is a temporary file."""

    STUBS = r"""
set -Eeuo pipefail
PROG=cst-test; CHECK=0; FORCE=0; cmd="${CMD:-plan}"
SRC="$REPO/bluetooth"
WP_MARKER="# managed-by: bluetooth-setup.sh (CST)"
OLD_WP_MARKER="Gerado por apply-le-audio.sh"
DEVICE_MAC=38:D5:18:CD:AA:4C; DEVICE_ID=38_D5_18_CD_AA_4C; LDAC_QUALITY=auto; ETC=/etc
USER_SETTINGS="$HOME/.config/cst/bluetooth.conf"
WP_CONF_DIR="$HOME/.config/wireplumber/wireplumber.conf.d"
WP_CONF="$WP_CONF_DIR/50-bluez-38_D5_18_CD_AA_4C.conf"
LE_CONF="$WP_CONF_DIR/90-cst-le-audio-off.conf"
LE_ON_HINT="cst-test le-audio on"; LE_OFF_HINT="cst-test le-audio off"
WP_FRAGMENT_DIRS=("$WP_CONF_DIR" "$HOME/etc-wp.d")
check_ok() { item ok "$1" "${2:-}"; }
check_bad() { item BAD "$1" "${2:-}"; FAILS=$((FAILS + 1)); }
le_from="the CST default"; BEARER="${BEARER:-}"; device_from=settings
MAIN_CONF="$HOME/main.conf"; TMP="$HOME/tmp"; LW_UNIT=cst-bt-ldac48.service
FAILS=0; WARNS=0
helper() { python3 -I "$REPO/bluetooth/bt-helper" "$@"; }
item() { echo "item $1 | $2 | ${3:-}"; }
section() { echo "== $1"; }
check_warn() { item warn "$1" "${2:-}"; WARNS=$((WARNS + 1)); }
wp_label() { echo "wireplumber.conf.d/$(basename "$1")"; }
user_sets() { return 1; }
lw_report() { :; }
compute_plan() {
    MC_CHANGED=0; WP_STATE=ok; LEGACY=0; BEARER_STATE=none; LW_CHANGE=0; LW_WANT=0
    le_plan
    BT_RESTART=0; WP_RESTART=0; (( LE_CHANGE )) && WP_RESTART=1
    return 0
}
# revert's other parts
preconditions() { :; }; oldest_backup() { return 1; }
lw_plan() { :; }; lw_ours_present() { return 1; }; lw_remove() { echo "lw_remove"; }
do_restarts() { echo "do_restarts $1 $2"; }
die() { echo "DIE $*"; exit 1; }
wp_roles() { [[ -n "${WP_ROLES:-}" ]] || return 5; echo "$WP_ROLES"; }
YES="${YES:-0}"; NO_RESTART="${NO_RESTART:-0}"; le_set="${LE_SET:-}"; SELF="$HOME/self"
"""

    def sh(self, body, le="off", home=None, **env):
        home = home or self.home
        script = (self.STUBS + f'LE_AUDIO="{le}"\n' +
                  shell_functions("le_plan", "le_report", "le_others_report", "le_roles_check", "le_audio_cmd",
                                  "files_report", "apply_le", "header", "plan",
                                  "bearer_check", "revert", "le_off_active") + body)
        e = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home, "REPO": REPO, **env}
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, self.home)
        with open(os.path.join(self.home, "main.conf"), "w") as f:
            f.write(f"[General]\nExperimental = true\nKernelExperimental = {ISO}\n")
        self.le_conf = os.path.join(self.home, ".config/wireplumber/wireplumber.conf.d/90-cst-le-audio-off.conf")

    def state(self, le="off"):
        r = self.sh('le_plan; echo "st=$LE_STATE change=$LE_CHANGE"\n', le=le)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()[-1]

    def test_state_read_again_after_writing(self):
        # apply's closing verify must not report the state from before the
        # changes ("drop-in is not in place yet" right after writing it).
        r = self.sh('compute_plan; echo "before=$LE_STATE"; apply_le; le_plan; echo "after=$LE_STATE"\n')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("before=missing", r.stdout)
        self.assertIn("after=ok", r.stdout)

    def test_apply_reads_state_again_before_verify(self):
        body = shell_functions("apply")
        tail = body[body.rindex("\n    verify\n") - 40:]
        self.assertRegex(tail, r"le_plan\n\s*lw_plan\n\s*verify\n")

    def test_off_on_off(self):
        self.assertEqual(self.state(), "st=missing change=1")
        r = self.sh("le_plan; apply_le\n")
        self.assertIn("written:", r.stdout)
        with open(self.le_conf) as f:
            text = f.read()
        self.assertIn("override.monitor.bluez.properties = {\n  bluez5.roles = [ a2dp_sink a2dp_source hfp_hf hfp_ag ]\n}",
                      text)
        self.assertEqual(self.state(), "st=ok change=0")
        r = self.sh("le_plan; le_off_active && echo active\n")
        self.assertIn("active", r.stdout)
        # Turned on: removed (and WirePlumber restarted, through LE_CHANGE).
        self.assertEqual(self.state("on"), "st=remove change=1")
        r = self.sh("le_plan; apply_le\n", le="on")
        self.assertIn("removed:", r.stdout)
        self.assertFalse(os.path.exists(self.le_conf))
        self.assertEqual(self.state("on"), "st=absent change=0")
        self.assertEqual(self.state(), "st=missing change=1")

    def test_changed_and_foreign_files(self):
        self.sh("le_plan; apply_le\n")
        with open(self.le_conf, "a") as f:
            f.write("monitor.bluez.properties = { bluez5.hw-volume = [ ] }\n")
        self.assertEqual(self.state(), "st=change change=1")
        with open(self.le_conf, "w") as f:
            f.write("override.monitor.bluez.properties = { bluez5.roles = [ a2dp_sink ] }\n")
        self.assertEqual(self.state(), "st=conflict change=0")
        self.assertEqual(self.state("on"), "st=conflict change=0")
        r = self.sh("le_plan; apply_le\n", le="on")
        self.assertIn("left alone", r.stderr)
        self.assertTrue(os.path.exists(self.le_conf))

    def test_plan_output(self):
        r = self.sh("plan || echo rc=$?\n")
        self.assertIn("LE Audio off (the CST default)", r.stdout)
        self.assertIn("item write | wireplumber.conf.d/90-cst-le-audio-off.conf", r.stdout)
        self.assertIn("item restart | wireplumber (your session)", r.stdout)
        self.assertIn("rc=10", r.stdout)
        self.sh("le_plan; apply_le\n")
        r = self.sh("plan || echo rc=$?\n")
        self.assertIn("item ok | wireplumber.conf.d/90-cst-le-audio-off.conf", r.stdout)
        self.assertIn("Nothing to do.", r.stdout)
        r = self.sh("plan || echo rc=$?\n", le="on")
        self.assertIn("item remove | wireplumber.conf.d/90-cst-le-audio-off.conf", r.stdout)
        self.assertIn("rc=10", r.stdout)
        # A preferred bearer of le makes no sense with LE Audio off.
        r = self.sh("plan || :\n", BEARER="le")
        self.assertIn("item warn | preferred bearer | BEARER=le while LE Audio is off", r.stdout)

    def test_plan_shows_the_diff_of_a_changed_file(self):
        self.sh("le_plan; apply_le\n")
        with open(self.le_conf) as f:
            text = f.read()
        with open(self.le_conf, "w") as f:
            f.write(text.replace("hfp_ag ]", "hfp_ag bap_sink ]"))
        r = self.sh("plan || :\n")
        self.assertIn("as apply would write it", r.stdout)
        self.assertIn("-  bluez5.roles = [ a2dp_sink a2dp_source hfp_hf hfp_ag bap_sink ]", r.stdout)

    def test_revert_removes_the_drop_in(self):
        self.sh("le_plan; apply_le\n")
        r = self.sh("CHECK=1; revert || echo rc=$?\n")
        self.assertIn("would remove", r.stdout)
        self.assertIn("would restart wireplumber", r.stdout)
        self.assertIn("rc=10", r.stdout)
        self.assertTrue(os.path.exists(self.le_conf))
        r = self.sh("revert\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("removed:", r.stdout)
        self.assertIn("do_restarts 0 1", r.stdout)
        self.assertFalse(os.path.exists(self.le_conf))
        r = self.sh("revert\n")
        self.assertIn("none needed", r.stdout)

    def test_revert_leaves_a_foreign_file(self):
        os.makedirs(os.path.dirname(self.le_conf))
        with open(self.le_conf, "w") as f:
            f.write("monitor.bluez.properties = { }\n")
        r = self.sh("revert\n")
        self.assertIn("left alone", r.stderr)
        self.assertTrue(os.path.exists(self.le_conf))

    def test_bearer_warning(self):
        info_le = "\tPaired: yes\n\tUUID: Telephony Bearer (0000184b-...)\n\tPreferredBearer: le\n"
        info_bredr = info_le.replace("PreferredBearer: le", "PreferredBearer: bredr")
        body = 'bearer_check "$INFO"; echo "warns=$WARNS"\n'
        r = self.sh(body, INFO=info_le)
        self.assertIn("item warn | preferred bearer", r.stdout)
        self.assertIn("--bearer bredr", r.stdout)
        self.assertIn("warns=1", r.stdout)
        self.assertIn("warns=0", self.sh(body, INFO=info_bredr).stdout)
        self.assertIn("warns=0", self.sh(body, le="on", INFO=info_le).stdout)
        self.assertIn("warns=0", self.sh(body, INFO="").stdout)

    def test_effective_roles_check(self):
        body = 'le_plan; le_roles_check; echo "fails=$FAILS warns=$WARNS"\n'
        classic = "classic|[ a2dp_sink a2dp_source hfp_hf hfp_ag ]"
        r = self.sh(body, WP_ROLES=classic)
        self.assertIn("item ok | WirePlumber bluez5.roles | [ a2dp_sink a2dp_source hfp_hf hfp_ag ] (no LE Audio roles)",
                      r.stdout)
        self.assertIn("fails=0 warns=0", r.stdout)
        r = self.sh(body, WP_ROLES="le|[a2dp_sink, bap_sink]")
        self.assertIn("item BAD | WirePlumber bluez5.roles", r.stdout)
        self.assertIn("fails=1", r.stdout)
        # Not set while the CST file is missing: only a note (le_report already says apply).
        r = self.sh(body, WP_ROLES="default|")
        self.assertIn("item note | WirePlumber bluez5.roles", r.stdout)
        self.assertIn("fails=0 warns=0", r.stdout)
        # Not set although the CST file is in place: WirePlumber does not use it (today's bug).
        self.sh("le_plan; apply_le\n")
        r = self.sh(body, WP_ROLES="default|")
        self.assertIn("item BAD | WirePlumber bluez5.roles | not set: WirePlumber does not use the CST file", r.stdout)
        r = self.sh(body, le="on", WP_ROLES=classic)
        self.assertIn("item warn | WirePlumber bluez5.roles", r.stdout)
        r = self.sh(body, le="on", WP_ROLES="default|")
        self.assertIn("item ok | WirePlumber bluez5.roles | not set: the PipeWire defaults (LE Audio roles included)",
                      r.stdout)
        r = self.sh(body)                       # libwireplumber not found
        self.assertIn("item warn | WirePlumber bluez5.roles | could not be read", r.stdout)
        self.assertIn("fails=0 warns=1", r.stdout)

    def test_other_fragments_are_named(self):
        d = os.path.dirname(self.le_conf)
        os.makedirs(d)
        os.makedirs(os.path.join(self.home, "etc-wp.d"))
        with open(os.path.join(d, "51-mine.conf"), "w") as f:
            f.write("monitor.bluez.properties = {\n  bluez5.enable-msbc = true\n}\n")
        with open(os.path.join(self.home, "etc-wp.d", "99-late.conf"), "w") as f:
            f.write("monitor.bluez.properties = { bluez5.roles = [ bap_sink ] }\n")
        r = self.sh("le_plan; le_others_report\n")
        self.assertIn("item warn | ~/.config/wireplumber/wireplumber.conf.d/51-mine.conf | also sets", r.stdout)
        self.assertIn("item warn | ~/etc-wp.d/99-late.conf | sets monitor.bluez.properties after", r.stdout)
        self.assertEqual(self.sh("le_plan; le_others_report\n", le="on").stdout, "")

    def le_audio(self, value, **env):
        with open(os.path.join(self.home, "self"), "w") as f:
            f.write('#!/bin/sh\necho "SELF $*"\n')
        os.chmod(os.path.join(self.home, "self"), 0o755)
        r = self.sh("le_plan; le_audio_cmd\n", LE_SET=value, **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def test_le_audio_command_without_a_settings_folder(self):
        # Fresh user: no ~/.config/cst yet (the old `echo ... >>` hint failed there).
        settings = os.path.join(self.home, ".config/cst/bluetooth.conf")
        r = self.le_audio("on")
        self.assertIn("written:   ~/.config/cst/bluetooth.conf: LE_AUDIO=on", r.stdout)
        self.assertIn("SELF apply", r.stdout)
        with open(settings) as f:
            self.assertEqual(load().parse_settings(f.read()), {"LE_AUDIO": "on"})
        r = self.le_audio("on", YES="1", NO_RESTART="1")
        self.assertIn("unchanged:", r.stdout)
        self.assertIn("SELF apply --yes --no-restart", r.stdout)

    def test_le_audio_command_keeps_the_other_lines(self):
        # The case that broke the appended hint: no newline at the end.
        os.makedirs(os.path.join(self.home, ".config/cst"))
        real = os.path.join(self.home, "dotfiles.conf")
        with open(real, "w") as f:
            f.write("# mine\nLDAC_RATE=48000")
        os.chmod(real, 0o600)
        settings = os.path.join(self.home, ".config/cst/bluetooth.conf")
        os.symlink(real, settings)
        self.le_audio("on")
        self.assertTrue(os.path.islink(settings))      # written through the symlink
        with open(real) as f:
            self.assertEqual(f.read(), "# mine\nLDAC_RATE=48000\nLE_AUDIO=on\n")
        self.assertEqual(os.stat(real).st_mode & 0o777, 0o600)
        self.le_audio("off")
        with open(real) as f:
            self.assertEqual(f.read(), "# mine\nLDAC_RATE=48000\nLE_AUDIO=off\n")

    def test_le_audio_command_shows_the_setting(self):
        r = self.sh("le_audio_cmd\n")
        self.assertIn("LE Audio in PipeWire: off (the CST default)", r.stdout)
        self.assertIn("Turn it on: cst-test le-audio on", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config/cst")))
        r = self.sh("le_audio_cmd\n", le="on")
        self.assertIn("Turn it off: cst-test le-audio off", r.stdout)

    def test_install_main_conf_respects_selinux_state(self):
        orig = os.path.join(self.home, "main.conf")
        new = os.path.join(self.home, "main.conf.newsrc")
        with open(orig, "w") as f:
            f.write("[General]\n")
        with open(new, "w") as f:
            f.write("[General]\nExperimental = true\n")
        log = os.path.join(self.home, "calls.log")
        script = (
            f'MAIN_CONF={shlex.quote(orig)}\n'
            'sudo() { "$@"; }\n'
            f'cp() {{ printf "cp %s\\n" "$*" >>{shlex.quote(log)}; '
            'local args=(); for a in "$@"; do [[ "$a" == --preserve=context ]] || args+=("$a"); done; '
            'command cp "${args[@]}"; }\n'
            f'install() {{ printf "install %s\\n" "$*" >>{shlex.quote(log)}; '
            'local args=("$@"); local dst="${args[-1]}"; local src="${args[-2]}"; '
            'command cp "$src" "$dst"; }\n'
            'mv() { command mv -f "$2" "$3"; }\n'
            'selinuxenabled() { return 1; }\n'
            f'install_main_conf {shlex.quote(new)}\n'
            'selinuxenabled() { return 0; }\n'
            f'install_main_conf {shlex.quote(new)}\n'
        )
        r = self.sh(shell_functions("install_main_conf") + script)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(log) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 4)
        self.assertNotIn("--preserve=context", lines[0])
        self.assertNotIn(" -Z ", lines[1])
        self.assertIn("--preserve=context", lines[2])
        self.assertIn(" -Z ", lines[3])

    def test_bt_info_uses_timeout_and_null_stdin(self):
        with open(SETUP) as f:
            src = f.read()
        bt_info_fn = re.search(r"^bt_info\(\) .*$", src, re.M).group(0)
        script = (
            bt_info_fn + "\n"
            'hostcmd() { "$@"; }\n'
            'timeout() { printf "stdin_target=%s args=%s\\n" "$(readlink -f /dev/fd/0)" "$*"; }\n'
            'bt_info\n'
        )
        r = self.sh(script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"stdin_target=/dev/null args=5 bluetoothctl info {MAC}", r.stdout)

    def test_apply_bearer_uses_bluetoothctl_and_null_stdin(self):
        with open(SETUP) as f:
            src = f.read()
        apply_fn = re.search(r"^apply\(\) \{\n.*?^\}\n", src, re.M | re.S).group(0)
        stub_plan = (
            'compute_plan() { MC_CHANGED=0; WP_STATE=ok; LE_STATE=ok; LE_CHANGE=0; LEGACY=0; '
            'BT_RESTART=0; WP_RESTART=0; LW_CHANGE=0; LW_WANT=0; BEARER_STATE=set; BEARER=bredr; }\n'
            'lw_plan() { :; }; apply_watcher() { :; }; do_restarts() { return 0; }; bt_info() { printf "\\tPreferredBearer: bredr\\n"; }\n'
            'verify() { :; }\n'
        )
        # 1. With bluetoothctl stubbed as a shell function (as in test_ldac_watch), it runs directly
        fn_log = os.path.join(self.home, "fn.log")
        script_fn = (
            stub_plan
            + f'bluetoothctl() {{ printf "fn stdin=%s args=%s\\n" "$(readlink -f /dev/fd/0)" "$*" >>{shlex.quote(fn_log)}; }}\n'
            + apply_fn + "\napply\n"
        )
        r1 = self.sh(script_fn)
        self.assertEqual(r1.returncode, 0, r1.stderr)
        with open(fn_log) as f:
            self.assertIn(f"fn stdin=/dev/null args=bearer {MAC} bredr", f.read())
        # 2. Without a shell function, bluetoothctl runs under timeout 5 with stdin=/dev/null
        fake_bin = os.path.join(self.home, "bin")
        os.makedirs(fake_bin, exist_ok=True)
        bin_log = os.path.join(self.home, "bin.log")
        fake_bt = os.path.join(fake_bin, "bluetoothctl")
        with open(fake_bt, "w") as f:
            f.write("#!/usr/bin/env bash\nexit 0\n")
        os.chmod(fake_bt, 0o755)
        fake_to = os.path.join(fake_bin, "timeout")
        with open(fake_to, "w") as f:
            f.write(f'#!/usr/bin/env bash\nprintf "bin_to stdin=%s args=%s\\n" "$(readlink -f /dev/fd/0)" "$*" >>{shlex.quote(bin_log)}\n')
        os.chmod(fake_to, 0o755)
        script_bin = (
            f'PATH={shlex.quote(fake_bin)}:"$PATH"\n'
            + stub_plan
            + apply_fn + "\napply\n"
        )
        r2 = self.sh(script_bin)
        self.assertEqual(r2.returncode, 0, r2.stderr)
        with open(bin_log) as f:
            self.assertIn(f"bin_to stdin=/dev/null args=5 bluetoothctl bearer {MAC} bredr", f.read())



class OptionsTest(unittest.TestCase):
    """bluetooth-setup.sh stops on bad options before it looks at the system."""

    def run_setup(self, *args, home=None):
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home or tempfile.gettempdir()}
        return subprocess.run(["bash", SETUP, *args], capture_output=True, text=True, env=env)

    def test_bad_options(self):
        for args in (("plan", "--ldac-quality", "best"), ("plan", "--bearer=usb"), ("plan", "--bogus"),
                     ("plan", "--device", "nope"), ("status", "--force"), ("apply", "--check"),
                     ("plan", "--device"), ("plan", "--le-audio", "maybe"), ("apply", "--le-audio"),
                     ("revert", "--le-audio", "on"), ("plan", "--le-audio=yes"), ("le-audio", "maybe"),
                     ("le-audio", "on", "--le-audio", "off"), ("le-audio", "on", "--device", MAC),
                     ("le-audio", "on", "--force")):
            r = self.run_setup(*args)
            self.assertEqual(r.returncode, 2, (args, r.stderr))
        self.assertEqual(self.run_setup("nonsense").returncode, 2)

    def test_bad_device_in_user_settings_is_named(self):
        home = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, home)
        os.makedirs(os.path.join(home, ".config", "cst"))
        with open(os.path.join(home, ".config", "cst", "bluetooth.conf"), "w") as f:
            f.write("DEVICE=headset\n")
        r = self.run_setup("plan", home=home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("got 'headset'", r.stderr)
        self.assertIn(os.path.join(home, ".config", "cst", "bluetooth.conf"), r.stderr)

    def test_bad_ldac_rate_in_user_settings(self):
        home = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, home)
        os.makedirs(os.path.join(home, ".config", "cst"))
        with open(os.path.join(home, ".config", "cst", "bluetooth.conf"), "w") as f:
            f.write("LDAC_RATE=96000\n")
        r = self.run_setup("plan", home=home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("LDAC_RATE must be empty", r.stderr)

    def test_bad_le_audio_in_user_settings(self):
        home = tempfile.mkdtemp(prefix="bt-test-")
        self.addCleanup(shutil.rmtree, home)
        os.makedirs(os.path.join(home, ".config", "cst"))
        with open(os.path.join(home, ".config", "cst", "bluetooth.conf"), "w") as f:
            f.write("LE_AUDIO=true\n")
        r = self.run_setup("plan", home=home)
        self.assertEqual(r.returncode, 2)
        self.assertIn("LE Audio must be on or off (got 'true'", r.stderr)

    def test_help_lists_the_commands(self):
        r = self.run_setup("help")
        self.assertEqual(r.returncode, 0)
        for c in ("plan", "apply", "status", "revert", "le-audio"):
            self.assertRegex(r.stdout, re.compile(rf"^\s+{c}\b", re.M))


if __name__ == "__main__":
    unittest.main()

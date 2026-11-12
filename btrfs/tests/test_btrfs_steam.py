"""Unit tests for `btrfs-bulk steam plan|apply` (no root, no btrfs, no Steam).

The Steam library is a scratch tree under a temporary HOME, built from
fixtures that are read-only copies of a real Flatpak Steam library's
libraryfolders.vdf, libraryfolder.vdf and app manifests (no credentials in
them; the owner's SteamID replaced by a placeholder). "Steam running", the
busy check, flatpak and the reflink copy are fakes (the reflink fake uses
cp --reflink=auto: /tmp may be tmpfs). The rest is the real code.

Run from the repository root:
    python3 -I -B btrfs/tests/test_btrfs_steam.py -v
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import unittest

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures", "steam")
REAL_ROOT = "/var/home/danilo/.var/app/com.valvesoftware.Steam/.local/share/Steam"


def _load_base():
    loader = importlib.machinery.SourceFileLoader("btrfs_bulk_test_base", os.path.join(HERE, "test_btrfs_bulk.py"))
    spec = importlib.util.spec_from_loader("btrfs_bulk_test_base", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


tb = _load_base()
Crash = tb.Crash
TOOL = tb.TOOL


def fixture(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


class SteamTest(tb.BulkBase):
    def setUp(self):
        super().setUp()
        m = self.m
        self.real_steam_running = m.steam_running
        self.steam_procs = []
        self.no_reflink = False
        m.steam_running = lambda ctx: list(self.steam_procs)

        def copy_reflink(src, dst):
            if self.no_reflink:
                raise RuntimeError("cp: failed to clone: Operation not supported")
            # btrfs: no clone between a NOCOW (+C) file and a copy-on-write one.
            if self.has_fake_nocow(src):
                raise RuntimeError(f"cp: failed to clone '{dst}' from '{src}': Invalid argument")
            r = subprocess.run(["cp", "-a", "--reflink=auto", "--no-target-directory", "--", src, dst],
                               capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(r.stderr)
        m.copy_reflink = copy_reflink
        self.flatpaks["com.valvesoftware.Steam"] = set()
        self.root = self.H(m.STEAM_ROOT)
        self.T = self.B("steam")
        self.make_library()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.apply(self.ctx), ([], []))     # ~/Bulk/steam and the Flatpak permission

    # ---- scratch library ----
    def key(self, p):
        st = os.lstat(p)
        return st.st_dev, st.st_ino

    def has_fake_nocow(self, top):
        if not os.path.isdir(top) or os.path.islink(top):
            return os.path.isfile(top) and self.key(top) in self.nocow
        return any(self.key(os.path.join(r, n)) in self.nocow for r, _, f in os.walk(top) for n in f)

    def make_nocow(self, *rels):
        """+C on these parts of the main library and everything in them (as
        on the host, where the whole Steam folder was made NOCOW)."""
        for rel in rels:
            p = self.S(rel)
            self.nocow.add(self.key(p))
            for r, d, f in os.walk(p):
                for n in d + f:
                    self.nocow.add(self.key(os.path.join(r, n)))

    def S(self, rel):
        return os.path.join(self.root, "steamapps", rel)

    def D(self, rel):
        return os.path.join(self.T, "steamapps", rel)

    def put(self, path, data, mode=0o644):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        os.chmod(path, mode)

    def lf_text(self):
        return fixture("libraryfolders.vdf").replace(REAL_ROOT.encode(), self.root.encode())

    def put_lf(self, data, which=(0, 1)):
        for i in which:
            self.put(self.m.lf_files(self.root)[i], data, 0o755)

    def make_library(self):
        r = self.root
        self.put_lf(self.lf_text())
        self.put(os.path.join(r, "libraryfolder.vdf"), fixture("libraryfolder.vdf"), 0o755)
        self.put(self.S("appmanifest_570.acf"), fixture("appmanifest_570.acf"), 0o755)
        self.put(self.S("appmanifest_1628350.acf"), fixture("appmanifest_1628350.acf"), 0o755)
        g = self.S("common/dota 2 beta")
        self.put(os.path.join(g, "game/bin/linuxsteamrt64/dota2"), "elf", 0o755)
        self.put(os.path.join(g, "game/dota/pak01_dir.vpk"), "vpk")
        os.makedirs(os.path.join(g, "game/empty"))
        os.symlink("bin", os.path.join(g, "game/link"))
        self.put(self.S("common/SteamLinuxRuntime_sniper/run"), "run", 0o755)
        os.symlink(os.path.join(r, "legacycompat/Steam.dll"), self.S("common/Steam.dll"))
        self.put(self.S("workshop/appworkshop_570.acf"), fixture("appworkshop_570.acf"), 0o755)
        self.put(self.S("workshop/content/570/128726494/item.vpk"), "item")
        for d in ("compatdata/570", "downloading", "temp", "workshop/downloads", "workshop/temp"):
            os.makedirs(self.S(d))
        self.put(self.S("shadercache/570/fozpipelinesv6/cache.foz"), "cache")

    # ---- running ----
    def run_out(self, fn, *a):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            r = fn(*a)
        self.out = out.getvalue()
        return r

    def sapply(self, *ids, copy=False):
        return self.run_out(self.m.steam_apply, self.ctx, list(ids), copy)

    def splan(self, *ids, copy=False):
        def plan():
            sv = self.m.steam_survey(self.ctx, list(ids), copy)
            self.m.steam_print(self.ctx, sv)
            return sv
        return self.run_out(plan)

    def lf(self, i=0):
        with open(self.m.lf_files(self.root)[i], "rb") as f:
            return self.m.vdf_parse(f.read())["libraryfolders"]

    def assertMoved(self):
        self.assertTrue(os.path.isfile(self.D("common/dota 2 beta/game/dota/pak01_dir.vpk")))
        self.assertEqual(os.readlink(self.D("common/dota 2 beta/game/link")), "bin")
        self.assertTrue(os.path.isdir(self.D("common/dota 2 beta/game/empty")))
        self.assertTrue(os.path.isfile(self.D("appmanifest_570.acf")))
        self.assertTrue(os.path.isfile(self.D("workshop/content/570/128726494/item.vpk")))
        self.assertTrue(os.path.isfile(self.D("workshop/appworkshop_570.acf")))
        for p in ("common/dota 2 beta", "appmanifest_570.acf", "workshop/content/570", "workshop/appworkshop_570.acf"):
            self.assertFalse(os.path.lexists(self.S(p)), p)
        files = self.m.lf_files(self.root)
        with open(files[0], "rb") as a, open(files[1], "rb") as b:
            self.assertEqual(a.read(), b.read())
        lf = self.lf()
        self.assertEqual(lf["1"]["path"], self.T)
        self.assertEqual(lf["1"]["apps"], {"570": "77185439508"})
        self.assertEqual(lf["0"]["apps"], {"1628350": "810335462"})
        self.assertNoLeftovers()

    def assertInMain(self):
        self.assertTrue(os.path.isfile(self.S("common/dota 2 beta/game/dota/pak01_dir.vpk")))
        self.assertTrue(os.path.isfile(self.S("appmanifest_570.acf")))
        self.assertFalse(os.path.lexists(self.D("appmanifest_570.acf")))
        self.assertFalse(os.path.lexists(self.D("common/dota 2 beta")))
        self.assertIn("570", self.lf()["0"]["apps"])

    def assertNoLeftovers(self):
        self.assertEqual([n for r, d, f in os.walk(self.home) for n in d + f if ".bulk-" in n or ".cst-tmp" in n], [])
        self.assertEqual(self.journals(), [])

    def assertUnchanged(self, before, rc, want_rc, text=None):
        self.assertEqual(rc, want_rc, self.out)
        self.assertEqual(self.tree(), before)
        if text:
            self.assertIn(text, self.out)

    # ---- VDF parser ----
    def test_fixtures_round_trip(self):
        for n in sorted(os.listdir(FIX)):
            data = fixture(n)
            self.assertEqual(self.m.vdf_dump(self.m.vdf_load(data)), data, n)

    def test_parser_refuses_anything_unexpected(self):
        bad = {
            b'"a"\n{\n\tk\t\t"v"\n}\n': "unquoted",
            b'"a"\n{\n\t// c\n}\n': "comment",
            b'#include "x.vdf"\n"a"\n{\n}\n': "#include",
            b'#base "x.vdf"\n"a"\n{\n}\n': "#include",
            b'"a"\n{\n\t"k"\t\t"v" [$WIN32]\n}\n': "[$condition]",
            b'"a"\n{\n\t"k"\t\t"v\\n"\n}\n': "escape",
            b'"a"\n{\n\t"k"\t\t"1"\n\t"k"\t\t"2"\n}\n': "duplicate",
            b'"a"\n{\n\t"k"\t\t"v"\n': "unbalanced",
            b'"a"\n{\n}\n}\n': "unbalanced",
            b'"a"\n{\n}\n"b"\n{\n}\n': "trailing",
            b'"a"\n{\n\t"k"\t\t"v\0"\n}\n': "NUL",
            b'"a"\n{\n\t"k"\t\t"\xff"\n}\n': "UTF-8",
            b'"a"\n{\n\t"k"\t\t"v\n}\n': "unterminated",
            b'"a"\t\t"v"\n': "no '{ }'",
            b'': "empty",
        }
        for data, why in bad.items():
            with self.assertRaises(self.m.Unexpected, msg=data) as cm:
                self.m.vdf_parse(data)
            self.assertIn(why, str(cm.exception), data)

    def test_parser_accepts_the_two_escapes_and_round_trips_them(self):
        data = b'"a"\n{\n\t"k"\t\t"x\\\\y\\"z"\n\t"e"\n\t{\n\t}\n}\n'
        t = self.m.vdf_load(data)
        self.assertEqual(t, {"a": {"k": 'x\\y"z', "e": {}}})

    def test_round_trip_guard_refuses_other_layouts(self):
        with self.assertRaises(self.m.Unexpected) as cm:
            self.m.vdf_load(b'"a" { "k" "v" }\n')
        self.assertIn("format not as expected", str(cm.exception))

    # ---- schema ----
    def test_schema_refusals(self):
        m = self.m
        base = m.vdf_load(self.lf_text())
        m.lf_validate(base)
        cases = []
        t = m.copy.deepcopy(base)
        t["libraryfolders"]["0"]["mounted"] = "1"
        cases.append((t, "unknown key 'mounted'"))
        t = m.copy.deepcopy(base)
        t["libraryfolders"]["2"] = t["libraryfolders"]["0"]
        cases.append((t, "not 0, 1, 2"))
        t = m.copy.deepcopy(base)
        t["libraryfolders"]["0"]["apps"]["dota"] = "1"
        cases.append((t, "not decimal"))
        t = m.copy.deepcopy(base)
        t["libraryfolders"]["0"]["label"] = {}
        cases.append((t, "a block, not a value"))
        t = m.lf_with_library(base, self.root + "/inside", "1")
        cases.append((t, "inside the other"))
        t = m.copy.deepcopy(base)
        t["other"] = {}
        cases.append((t, "exactly one"))
        for t, why in cases:
            with self.assertRaises(m.Unexpected) as cm:
                m.lf_validate(t)
            self.assertIn(why, str(cm.exception))

    def test_library_and_placement_are_idempotent_and_keep_the_rest(self):
        m = self.m
        base = m.vdf_load(self.lf_text())
        one = m.lf_with_library(base, self.T, "42")
        self.assertEqual(m.lf_with_library(one, self.T, "99"), one)
        self.assertEqual(one["libraryfolders"]["0"], base["libraryfolders"]["0"])
        self.assertEqual(list(one["libraryfolders"]["1"]),
                         ["path", "label", "contentid", "totalsize", "update_clean_bytes_tally",
                          "time_last_update_verified", "apps"])
        placed = m.lf_place_app(one, "570", self.T, "77185439508")
        self.assertEqual(m.lf_place_app(placed, "570", self.T, "77185439508"), placed)
        self.assertEqual(placed["libraryfolders"]["1"]["apps"], {"570": "77185439508"})
        e0 = dict(placed["libraryfolders"]["0"])
        b0 = dict(base["libraryfolders"]["0"])
        self.assertEqual(e0.pop("apps"), {"1628350": "810335462"})
        b0.pop("apps")
        self.assertEqual(e0, b0)
        # Back to the main library: the original bytes except the new entry.
        back = m.lf_place_app(placed, "570", self.root, "77185439508")
        self.assertEqual(back["libraryfolders"]["0"]["apps"], {"1628350": "810335462", "570": "77185439508"})
        m.lf_validate(placed)

    # ---- ~/Bulk/steam listed under another spelling ----
    def listed_by_hand(self):
        """~/Bulk/steam added in Steam by hand, under a path through a
        symlink (like /home/u/... where /home -> /var/home)."""
        m = self.m
        alias = os.path.join(self.tmp, "alias")
        os.symlink(self.home, alias)
        path = os.path.join(alias, "Bulk", "steam")
        self.put_lf(m.vdf_dump(m.lf_with_library(m.vdf_load(self.lf_text()), path, "111")))
        os.makedirs(self.D("common"))
        m.u_write_marker(os.path.join(self.T, "libraryfolder.vdf"), "111")
        return path

    def test_library_listed_through_a_symlink_is_the_same_library(self):
        m = self.m
        path = self.listed_by_hand()
        self.assertEqual(m.lf_index(m.vdf_load(self.lf_text()), self.root), "0")
        sv = self.splan()
        self.assertEqual(sv["idx"], "1")
        self.assertNotIn("add as library", self.out)
        self.assertEqual(self.sapply(), 0, self.out)
        lf = self.lf()
        self.assertEqual(sorted(lf), ["0", "1"])
        self.assertEqual(lf["1"]["path"], path)          # kept as the user wrote it
        self.assertEqual(lf["1"]["apps"], {"570": "77185439508"})
        self.assertEqual(lf["0"]["apps"], {"1628350": "810335462"})
        self.assertTrue(os.path.isfile(self.D("common/dota 2 beta/game/dota/pak01_dir.vpk")))
        self.assertNoLeftovers()
        before = self.tree()
        self.assertEqual(self.sapply(), 0, self.out)
        self.assertEqual(self.tree(), before)
        self.splan()
        self.assertIn("ok (library 1)", self.out)

    def test_a_pair_that_fails_the_checks_is_never_written(self):
        m = self.m
        self.listed_by_hand()
        files = m.lf_files(self.root)

        def raw():
            out = []
            for f in files:
                with open(f, "rb") as fh:
                    out.append(fh.read())
            return out
        before = raw()
        S = m.steam_read_pair(self.ctx)
        dup = m.copy.deepcopy(S["tree"])
        dup["libraryfolders"]["2"] = dict(dup["libraryfolders"]["1"], path=self.T)
        with self.assertRaisesRegex(m.Unexpected, "refused to write.*same folder"):
            m.write_vdf_pair(self.ctx, S, dup)
        self.assertEqual(raw(), before)
        # The old lookup (by spelling only) would add a second entry: the
        # check before writing stops it, with nothing written.
        self.patch("lf_index", lambda real: lambda tree, path: next(
            (i for i, e in tree["libraryfolders"].items() if os.path.normpath(e["path"]) == os.path.normpath(path)),
            None))
        self.assertEqual(self.sapply(), 1, self.out)
        self.assertIn("refused to write", self.out)
        self.assertEqual(raw(), before)
        self.assertTrue(os.path.isfile(self.S("common/dota 2 beta/game/dota/pak01_dir.vpk")))
        self.assertEqual(self.journals(), [])

    # ---- selection and plan ----
    def test_plan_default_moves_the_game_skips_the_tool_and_changes_nothing(self):
        before = self.tree()
        sv = self.splan()
        self.assertEqual(self.tree(), before)
        states = {a["id"]: a["state"] for a in sv["apps"]}
        self.assertEqual(states, {"570": "move", "1628350": "skip"})
        self.assertIn("570 Dota 2", self.out)
        self.assertIn("move it with --app 1628350", self.out)
        self.assertIn("add as library 1 (contentid new", self.out)
        self.assertIn("shader cache of 570", self.out)
        self.assertEqual(self.journals(), [])

    def test_explicit_app_moves_a_tool(self):
        sv = self.splan("1628350")
        self.assertEqual([(a["id"], a["state"]) for a in sv["apps"]], [("1628350", "move")])
        self.assertEqual(self.sapply("1628350"), 0, self.out)
        self.assertTrue(os.path.isfile(self.D("common/SteamLinuxRuntime_sniper/run")))
        self.assertEqual(self.lf()["1"]["apps"], {"1628350": "810335462"})
        self.assertTrue(os.path.isfile(self.S("appmanifest_570.acf")))

    def test_only_tools_selects_nothing(self):
        shutil.rmtree(self.S("common/dota 2 beta"))
        os.remove(self.S("appmanifest_570.acf"))
        self.put_lf(self.lf_text().replace(b'\t\t\t"570"\t\t"77185439508"\n', b""))
        before = self.tree()
        self.splan()
        self.assertIn("nothing to move by default; name one with --app ID", self.out)
        self.assertEqual(self.sapply(), 0, self.out)
        self.assertEqual(self.tree(), before)
        self.assertFalse(os.path.lexists(os.path.join(self.T, "libraryfolder.vdf")))

    def test_bad_arguments_are_usage_errors(self):
        for args in ([], ["move"], ["apply", "--app"], ["apply", "--app", "dota"], ["plan", "--app", "-1"],
                     ["plan", "--bogus"], ["apply", "--app", "12345678901"]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.m.steam_cmd(args), 2, args)

    # ---- happy path ----
    def test_apply_moves_the_game_and_is_idempotent(self):
        lf_mode = os.stat(self.m.lf_files(self.root)[0]).st_mode
        self.assertEqual(self.sapply(), 0, self.out)
        self.assertMoved()
        self.assertIn("Make default", self.out)
        with open(os.path.join(self.T, "libraryfolder.vdf"), "rb") as f:
            marker = self.m.vdf_load(f.read())["libraryfolder"]
        self.assertEqual(marker["label"], "")
        self.assertEqual(marker["contentid"], self.lf()["1"]["contentid"])
        self.assertTrue(os.path.isdir(self.D("common")))
        # Stays in the main library.
        self.assertTrue(os.path.isfile(self.S("shadercache/570/fozpipelinesv6/cache.foz")))
        self.assertTrue(os.path.isdir(self.S("compatdata/570")))
        self.assertTrue(os.path.islink(self.S("common/Steam.dll")))
        self.assertTrue(os.path.isfile(self.S("common/SteamLinuxRuntime_sniper/run")))
        # Modes kept; one backup of each file, with the old content.
        for f in self.m.lf_files(self.root):
            self.assertEqual(os.stat(f).st_mode, lf_mode)
            baks = [n for n in os.listdir(os.path.dirname(f)) if n.startswith("libraryfolders.vdf.cst-bak-")]
            self.assertEqual(len(baks), 1)
            with open(os.path.join(os.path.dirname(f), baks[0]), "rb") as b:
                self.assertEqual(b.read(), self.lf_text())
            self.assertEqual(os.stat(os.path.join(os.path.dirname(f), baks[0])).st_mode, lf_mode)
        self.assertEqual(os.stat(self.D("appmanifest_570.acf")).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(self.D("common/dota 2 beta/game/bin/linuxsteamrt64/dota2")).st_mode & 0o777, 0o755)
        # Again: nothing to do, nothing written, no new backup.
        before = self.tree()
        self.assertEqual(self.sapply(), 0, self.out)
        self.assertEqual(self.tree(), before)
        sv = self.splan()
        self.assertEqual({a["id"]: a["state"] for a in sv["apps"]}, {"570": "ok", "1628350": "skip"})
        self.assertIn("ok (library 1)", self.out)

    def test_backups_are_pruned_to_five(self):
        d = os.path.dirname(self.m.lf_files(self.root)[0])
        for i in range(7):
            self.put(os.path.join(d, f"libraryfolders.vdf.cst-bak-2020010{i}T000000"), "old")
        self.assertEqual(self.sapply(), 0, self.out)
        baks = sorted(n for n in os.listdir(d) if ".cst-bak-" in n)
        self.assertEqual(len(baks), 5)
        self.assertNotIn("libraryfolders.vdf.cst-bak-20200101T000000", baks)

    # ---- guards: nothing changes ----
    def test_g1_steam_running(self):
        self.steam_procs = ["process 42 (steamwebhelper)"]
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 5, "Steam is running: nothing done")
        self.splan()
        self.assertIn("YES (process 42 (steamwebhelper))", self.out)

    def test_g2_flatpak_permission_missing(self):
        self.flatpaks["com.valvesoftware.Steam"] = set()
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "run 'cst btrfs bulk apply' first")

    def test_g2_steam_flatpak_not_installed(self):
        del self.flatpaks["com.valvesoftware.Steam"]
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "not installed")

    def test_g3_unexpected_vdf(self):
        self.put_lf(self.lf_text().replace(b'"label"', b'label'))
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "unquoted text")

    def test_g3_copies_differ(self):
        self.put_lf(self.lf_text().replace(b"1147491265", b"1"), which=(1,))
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "differ")

    def test_g4_marker_does_not_match(self):
        self.assertEqual(self.sapply("1628350"), 0, self.out)
        self.put(os.path.join(self.T, "libraryfolder.vdf"), b'"libraryfolder"\n{\n\t"contentid"\t\t"7"\n\t"label"\t\t""\n}\n')
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "contentid")

    def test_g4_steamapps_is_a_symlink(self):
        os.makedirs(os.path.join(self.tmp, "elsewhere"))
        os.symlink(os.path.join(self.tmp, "elsewhere"), os.path.join(self.T, "steamapps"))
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "is a symlink or a file")

    def test_g5_mid_update(self):
        self.put(self.S("appmanifest_570.acf"), fixture("appmanifest_570.acf").replace(
            b'"StateFlags"\t\t"4"', b'"StateFlags"\t\t"1026"'), 0o755)
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "mid-update (StateFlags 1026")
        self.put(self.S("appmanifest_570.acf"), fixture("appmanifest_570.acf"), 0o755)
        self.put(self.S("downloading/570/chunk"), "c")
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "steamapps/downloading/570 is not empty")

    def test_g6_not_in_the_apps_map(self):
        self.put_lf(self.lf_text().replace(b'\t\t\t"570"\t\t"77185439508"\n', b""))
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "lists it under no library")

    def test_g6_manifest_in_both_libraries(self):
        self.put(self.D("appmanifest_570.acf"), fixture("appmanifest_570.acf"))
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "a manifest in both libraries")

    def test_g7_target_exists_is_never_merged(self):
        os.makedirs(self.D("common/dota 2 beta"))
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "exists already: remove it or move it back")

    def test_g7_leftover_new_copy(self):
        os.makedirs(self.D("workshop/content/570.bulk-new"))
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "570.bulk-new exists already")

    def test_g8_proton_prefix_with_data_even_with_explicit_app(self):
        self.put(self.S("compatdata/570/pfx/user.reg"), "x")
        before = self.tree()
        self.assertUnchanged(before, self.sapply("570"), 1, "Move install folder")

    def test_g9_game_folder_is_a_symlink(self):
        g = self.S("common/dota 2 beta")
        os.rename(g, g + "-real")
        os.symlink(g + "-real", g)
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "is a link, not a dir")

    def test_g9_entries_not_owned(self):
        before = self.tree()
        real = os.getuid
        os.getuid = lambda: 0
        try:
            rc = self.sapply()
        finally:
            os.getuid = real
        self.assertUnchanged(before, rc, 1)

    def test_g9_contains_a_subvolume(self):
        st = os.lstat(self.S("common/dota 2 beta/game/dota"))
        self.subvols.add((st.st_dev, st.st_ino))
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "contains another subvolume or a mount")

    def test_g10_no_reflink(self):
        self.no_reflink = True
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "is ~/Bulk on another filesystem?")

    def test_g11_low_space(self):
        self.m.free_bytes = lambda path: 1
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "not enough free space")

    def test_g12_busy(self):
        self.busy.add(self.S("common/dota 2 beta/game"))
        rc = self.sapply()
        self.assertEqual(rc, 5, self.out)
        self.assertIn("in use (open files)", self.out)
        self.assertInMain()
        self.assertNoLeftovers()

    # ---- NOCOW (+C) sources: a real copy, only with --copy ----
    NOCOW_PARTS = ("common/dota 2 beta", "appmanifest_570.acf", "workshop/content/570", "workshop/appworkshop_570.acf")

    def assertCow(self):
        for r, d, f in os.walk(os.path.join(self.T, "steamapps")):
            for n in d + f:
                self.assertNotIn(self.key(os.path.join(r, n)), self.nocow, os.path.join(r, n))

    def test_nocow_source_plan_says_copy(self):
        self.make_nocow(*self.NOCOW_PARTS)
        before = self.tree()
        sv = self.splan()
        self.assertEqual(self.tree(), before)
        a = sv["apps"][0]
        self.assertEqual((a["state"], a["reflink_bytes"]), ("move", 0))
        self.assertEqual(a["copy_bytes"], a["bytes"])
        self.assertEqual({p["mode"] for p in a["parts"]}, {"copy"})
        self.assertEqual(a["nocow_files"], 5)        # dota2, pak01_dir.vpk, item.vpk, two .acf
        self.assertRegex(self.out, r"copy 0 MiB byte by byte: 5 files in it have \+C \(NOCOW\), for example "
                         r"~/\S.*;\n.*btrfs cannot reflink those, so the folders holding them are rewritten "
                         r"into COW ~/Bulk/steam\n.*\(gaining checksums and compression\)")
        self.assertIn("add --copy to allow it", self.out)
        self.assertIn("real copies", self.out)
        self.assertNotIn("(reflink, no data written)", self.out)
        self.splan(copy=True)
        self.assertNotIn("add --copy", self.out)

    def test_mixed_parts_plan(self):
        self.make_nocow("common/dota 2 beta")
        sv = self.splan()
        modes = {p["name"]: p["mode"] for p in sv["apps"][0]["parts"]}
        self.assertEqual(modes, {"game": "copy", "workshop": "reflink", "workshop-acf": "reflink",
                                 "manifest": "reflink"})
        self.assertRegex(self.out, r"move \S+ MiB \(reflink, no data written\) and\n.*copy \S+ MiB byte by byte: 2 files in it have \+C")

    def test_nocow_without_copy_is_refused(self):
        self.make_nocow(*self.NOCOW_PARTS)
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "run again with --copy to allow it")
        self.assertEqual(self.journals(), [])

    def test_nocow_without_copy_others_still_move(self):
        self.make_nocow(*self.NOCOW_PARTS)
        self.assertEqual(self.sapply("570", "1628350"), 1, self.out)
        self.assertIn("570 Dota 2: 0 MiB must be copied byte by byte because 5 files in it have +C (NOCOW), "
                      "for example ~/", self.out)
        self.assertTrue(os.path.isfile(self.D("common/SteamLinuxRuntime_sniper/run")))
        self.assertInMain()
        self.assertNoLeftovers()

    def test_nocow_with_copy_copies_and_second_apply_does_nothing(self):
        self.make_nocow(*self.NOCOW_PARTS)
        self.m.PROGRESS_EVERY = 0           # a progress line per file compared
        self.assertEqual(self.sapply(copy=True), 0, self.out)
        self.assertMoved()
        self.assertCow()
        self.assertIn("start:     570 Dota 2: real copy", self.out)
        self.assertRegex(self.out, r"done: +570 Dota 2: in ~/Bulk/steam now \(\d+m\d\ds\)")
        self.assertIn("comparing game (dota 2 beta)", self.out)
        with open(self.D("common/dota 2 beta/game/dota/pak01_dir.vpk")) as f:
            self.assertEqual(f.read(), "vpk")
        before = self.tree()
        self.assertEqual(self.sapply(copy=True), 0, self.out)
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.sapply(), 0, self.out)       # without --copy too: nothing left to copy
        self.assertEqual(self.tree(), before)

    def test_mixed_parts_apply(self):
        self.make_nocow("common/dota 2 beta")
        self.assertEqual(self.sapply(copy=True), 0, self.out)
        self.assertMoved()
        self.assertCow()

    def corrupt_copy(self, rel, data):
        def make(real):
            def copy(src, dst, label, total):
                real(src, dst, label, total)
                p = os.path.join(dst, rel)
                if os.path.exists(p):
                    st = os.lstat(p)
                    with open(p, "r+b") as f:
                        f.write(data)
                    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
            return copy
        self.patch("copy_bytes", make)

    def test_copy_with_other_content_is_dropped(self):
        self.make_nocow(*self.NOCOW_PARTS)
        self.corrupt_copy("game/dota/pak01_dir.vpk", b"VPK")      # same size and time stamp, other bytes
        self.assertEqual(self.sapply(copy=True), 1, self.out)
        self.assertIn("the content of game/dota/pak01_dir.vpk differs", self.out)
        self.assertInMain()
        with open(self.S("common/dota 2 beta/game/dota/pak01_dir.vpk")) as f:
            self.assertEqual(f.read(), "vpk")
        self.assertNoLeftovers()

    def test_copy_with_another_tree_is_dropped(self):
        self.make_nocow(*self.NOCOW_PARTS)
        self.corrupt_copy("game/dota/pak01_dir.vpk", b"vpk-longer")
        self.assertEqual(self.sapply(copy=True), 1, self.out)
        self.assertIn("copy check failed", self.out)
        self.assertInMain()
        self.assertNoLeftovers()

    def test_copy_that_got_nocow_is_dropped(self):
        self.make_nocow(*self.NOCOW_PARTS)

        def make(real):
            def copy(src, dst, label, total):
                real(src, dst, label, total)
                self.nocow.add(self.key(dst))
            return copy
        self.patch("copy_bytes", make)
        self.assertEqual(self.sapply(copy=True), 1, self.out)
        self.assertIn("NOCOW (+C) entries", self.out)
        self.assertInMain()
        self.assertNoLeftovers()

    def test_kill_during_a_real_copy_is_undone(self):
        self.make_nocow(*self.NOCOW_PARTS)

        def make(real):
            def copy(src, dst, label, total):
                os.makedirs(os.path.join(dst, "game"))            # half done
                shutil.copy(os.path.join(src, "game/dota/pak01_dir.vpk"), os.path.join(dst, "game/x"))
                raise Crash()
            return copy
        real = self.patch("copy_bytes", make)
        # A kill gives no chance to clean up: the half copy stays.
        real_drop = self.patch("u_steam_drop_news", lambda real: lambda j: True)
        with self.assertRaises(Crash), contextlib.redirect_stdout(io.StringIO()):
            self.m.steam_apply(self.ctx, [], True)
        self.m.copy_bytes, self.m.u_steam_drop_news = real, real_drop
        self.assertTrue(os.path.isdir(self.D("common/dota 2 beta.bulk-new")))
        self.assertEqual(self.m.jread("steam-570")["phase"], "prepared")
        self.assertEqual(self.run_out(self.m.recover, self.ctx), [])
        self.assertIn("undid an interrupted move", self.out)
        self.assertInMain()
        self.assertNoLeftovers()
        self.assertEqual(self.sapply(copy=True), 0, self.out)
        self.assertMoved()

    def test_real_copy_needs_its_full_size_free(self):
        self.make_nocow(*self.NOCOW_PARTS)
        self.m.free_bytes = lambda path: self.m.MARGIN     # > STEAM_MARGIN, < copy + MARGIN
        sv = self.splan(copy=True)
        self.assertIn("NEEDS A LOOK: not enough for all", self.out)
        self.assertEqual(sv["space"], "")
        before = self.tree()
        rc = self.sapply(copy=True)
        self.assertEqual(rc, 1, self.out)
        self.assertIn("not enough free space in ~/Bulk for a real copy", self.out)
        self.assertInMain()
        self.assertEqual(self.journals(), [])
        self.assertTrue(before)

    def test_one_empty_nocow_file_is_named_not_the_whole_size(self):
        g = self.S("common/dota 2 beta")
        self.put(os.path.join(g, "game/dota/pak02_dir.vpk"), "x" * 5000)
        self.put(os.path.join(g, "game/empty/new.cfg"), "")
        self.make_nocow("common/dota 2 beta/game/empty/new.cfg")
        sv = self.splan()
        a = sv["apps"][0]
        self.assertEqual(a["nocow_files"], 1)
        self.assertGreater(a["copy_bytes"], 5000)         # the whole game folder, not the 0 bytes of +C data
        self.assertIn("byte by byte: 1 file in it has +C (NOCOW), for example "
                      "~/" + os.path.relpath(os.path.join(g, "game/empty/new.cfg"), self.home), self.out)
        self.assertEqual(self.sapply(), 1, self.out)
        self.assertIn("because 1 file in it has +C (NOCOW)", self.out)
        self.assertIn("recreating them without +C lets the game move by reflink", self.out)
        self.assertInMain()

    def test_snapshot_guard_is_read_from_btrfs_snapshot(self):
        m = self.m
        self.assertEqual(m.snapshot_guard(), {"pause": 250 * m.BIG, "resume": 270 * m.BIG, "emergency": 150 * m.BIG})
        fake = os.path.join(self.tmp, "btrfs-snapshot")
        with open(fake, "w") as f:
            f.write("X=1\nSKIP_AT_OR_BELOW_GIB=40\nRESUME_ABOVE_GIB=45\nEMERGENCY_AT_OR_BELOW_GIB=30\n")
        self.patch("SNAPSHOT", lambda real: fake)
        self.assertEqual(m.snapshot_guard(), {"pause": 40 * m.BIG, "resume": 45 * m.BIG, "emergency": 30 * m.BIG})
        self.assertEqual(m.steam_copy_floor(), 30 * m.BIG + m.MARGIN)
        # snapshots.conf overrides the defaults in btrfs-snapshot, as there.
        with open(os.path.join(m.ETC, "snapshots.conf"), "w") as f:
            f.write("HOME_DIR=''\nEMERGENCY_AT_OR_BELOW_GIB='60'\nSKIP_AT_OR_BELOW_GIB=90  # more\n")
        self.assertEqual(m.snapshot_guard(), {"pause": 90 * m.BIG, "resume": 45 * m.BIG, "emergency": 60 * m.BIG})
        os.remove(os.path.join(m.ETC, "snapshots.conf"))
        m.SNAPSHOT = os.path.join(self.tmp, "absent")
        self.assertEqual(m.snapshot_guard()["emergency"], 150 * m.BIG)

    def test_real_copy_keeps_the_snapshot_emergency_limit_free(self):
        self.make_nocow(*self.NOCOW_PARTS)
        sv = self.splan(copy=True)
        c = sv["copy_bytes"]
        # Enough for the copy plus MARGIN (the old check), not for the floor.
        self.m.free_bytes = lambda path: c + self.m.MARGIN + 10 * self.m.BIG
        self.splan(copy=True)
        self.assertIn("170.0 GiB stays free: the snapshot emergency purge at 150.0 GiB plus 20.0 GiB", self.out)
        self.assertIn("NEEDS A LOOK: not enough for all", self.out)
        self.assertNotIn("NOTE:", self.out)
        self.assertEqual(self.sapply(copy=True), 1, self.out)
        self.assertIn("must stay free so the /home snapshots are not purged", self.out)
        self.assertInMain()
        self.assertEqual(self.journals(), [])

    def test_real_copy_that_pauses_snapshots_is_pointed_out(self):
        self.make_nocow(*self.NOCOW_PARTS)
        self.m.free_bytes = lambda path: 200 * self.m.BIG       # above the floor, below the pause limit
        self.splan(copy=True)
        self.assertIn("NOTE: about 200.0 GiB free after all copies: /home snapshots pause at or below 250.0 GiB", self.out)
        self.assertEqual(self.sapply(copy=True), 0, self.out)
        self.assertMoved()
        self.m.free_bytes = lambda path: 10 ** 15
        self.make_nocow()
        self.splan("1628350", copy=True)
        self.assertNotIn("NOTE:", self.out)

    def test_failed_sync_keeps_the_original(self):
        self.make_nocow(*self.NOCOW_PARTS)
        real_run = subprocess.run

        def run(cmd, *a, **kw):
            if list(cmd[:2]) == ["sync", "-f"]:
                return subprocess.CompletedProcess(cmd, 1, "", "sync: error syncing 'x': Input/output error")
            return real_run(cmd, *a, **kw)
        subprocess.run = run
        self.addCleanup(setattr, subprocess, "run", real_run)
        self.assertEqual(self.sapply(copy=True), 1, self.out)
        self.assertIn("could not be written to the disk (sync: sync: error syncing 'x': Input/output error)", self.out)
        self.assertInMain()
        self.assertNoLeftovers()

    def test_failed_fsync_or_read_back_keeps_the_original(self):
        self.make_nocow(*self.NOCOW_PARTS)

        def make(real):
            def same(a, b, on_chunk=None):
                raise OSError(5, "Input/output error")
            return same
        self.patch("same_file", make)
        self.assertEqual(self.sapply(copy=True), 1, self.out)
        self.assertIn("could not be compared with its copy: [Errno 5] Input/output error", self.out)
        self.assertInMain()
        self.assertNoLeftovers()

    def test_progress_inside_one_large_file(self):
        m = self.m
        src, new = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
        for d in (src, new):
            self.put(os.path.join(d, "big.pak"), b"z" * 100)
        for name, val in (("CHUNK", 10), ("PROGRESS_BYTES", 30), ("PROGRESS_EVERY", 10 ** 6)):
            self.patch(name, lambda real, v=val: v)
        self.assertEqual(self.run_out(m.u_verify_content, src, new, "game", 100), "")
        self.assertEqual(self.out.count("comparing game:"), 3, self.out)     # at 30, 60 and 90 bytes

    def test_target_with_nocow_is_refused(self):
        self.nocow.add(self.key(self.T))
        before = self.tree()
        self.assertUnchanged(before, self.sapply(copy=True), 1, "must stay copy-on-write")

    def test_cli_copy_flag(self):
        self.make_nocow(*self.NOCOW_PARTS)
        self.patch("user_ctx", lambda real: lambda args: self.ctx)     # never the real home
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.m.steam_cmd(["plan", "--app", "570"]), 0)
        self.assertIn("steam apply --app 570 --copy (Steam closed)", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.m.steam_cmd(["plan", "--copy", "x"]), 2)
        self.assertEqual(self.run_out(self.m.steam_cmd, ["apply", "--copy"]), 0)
        self.assertMoved()

    def test_kept_steam_refuses(self):
        self.m.edit_keep(["steam"], set())
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "bulk unkeep steam")

    # ---- interruptions ----
    def patch(self, name, fn):
        real = getattr(self.m, name)
        setattr(self.m, name, fn(real))
        self.addCleanup(setattr, self.m, name, real)
        return real

    def crash_apply(self, *ids):
        with self.assertRaises(Crash), contextlib.redirect_stdout(io.StringIO()):
            self.m.steam_apply(self.ctx, list(ids))

    def crash_os_in(self, fn_name, os_name, after, nth=1):
        """os.<os_name> raises Crash after `after` calls, inside the nth call
        of the module's fn_name."""
        calls = {"fn": 0, "os": 0}
        real_os = getattr(os, os_name)

        def wrapped_os(*a, **k):
            if calls["os"] >= after:
                raise Crash()
            calls["os"] += 1
            return real_os(*a, **k)

        def make(real_fn):
            def patched(*a):
                calls["fn"] += 1
                if calls["fn"] != nth:
                    return real_fn(*a)
                setattr(os, os_name, wrapped_os)
                try:
                    return real_fn(*a)
                finally:
                    setattr(os, os_name, real_os)
            return patched
        real = self.patch(fn_name, make)
        self.crash_apply()
        setattr(self.m, fn_name, real)

    def recovered_then_moved(self):
        self.assertEqual(self.run_out(self.m.recover, self.ctx), [], self.out)
        self.assertEqual(self.journals(), [])
        self.assertEqual(self.sapply(), 0, self.out)
        self.assertMoved()

    def test_steam_started_during_the_copy_is_undone(self):
        def make(real):
            def copy(src, dst):
                real(src, dst)
                self.steam_procs = ["process 7 (steam)"]
            return copy
        self.patch("copy_reflink", make)
        self.assertEqual(self.sapply(), 5, self.out)
        self.assertIn("Steam started", self.out)
        self.assertInMain()
        self.assertNoLeftovers()

    def test_kill_during_the_copy_deletes_the_copies(self):
        n = {"calls": 0}

        def make(real):
            def clock(near):
                n["calls"] += 1
                if n["calls"] == 2:        # the copy is done, stamp2 not taken
                    raise Crash()
                return real(near)
            return clock
        real = self.patch("u_clock", make)
        self.crash_apply()
        self.m.u_clock = real
        self.assertTrue(os.path.isdir(self.D("common/dota 2 beta.bulk-new")))
        self.assertEqual(self.journals(), ["journal-steam-570.json"])
        self.assertEqual(self.run_out(self.m.recover, self.ctx), [])
        self.assertInMain()
        self.assertNoLeftovers()
        self.recovered_then_moved()

    def test_kill_after_the_game_folder_moved_is_rolled_back(self):
        for after in (1, 2, 7):     # src->old only; the game switched; all but the manifest
            with self.subTest(after=after):
                self.crash_os_in("u_steam_switch", "rename", after)
                self.assertFalse(os.path.lexists(self.D("appmanifest_570.acf")))
                self.run_out(self.m.recover, self.ctx)
                self.assertIn("undid an interrupted move", self.out)
                self.assertInMain()
                self.assertNoLeftovers()
        self.recovered_then_moved()

    def test_kill_after_all_renames_before_the_vdf_is_rolled_forward(self):
        self.crash_os_in("write_vdf_pair", "rename", 0, nth=2)   # 1st: the library entry
        self.assertTrue(os.path.isfile(self.D("appmanifest_570.acf")))
        self.assertIn("570", self.lf()["0"]["apps"])
        self.run_out(self.m.recover, self.ctx)
        self.assertIn("finished an interrupted move", self.out)
        self.assertMoved()

    def test_kill_in_the_switching_phase_with_all_renames_done(self):
        def make(real):
            def late(j):
                raise Crash()
            return late
        real = self.patch("u_steam_late", make)
        self.crash_apply()
        self.m.u_steam_late = real
        self.assertEqual(self.m.jread("steam-570")["phase"], "switching")
        self.run_out(self.m.recover, self.ctx)
        self.assertMoved()

    def test_kill_between_the_two_vdf_writes(self):
        self.crash_os_in("u_write_vdf_pair", "rename", 1, nth=2)
        files = self.m.lf_files(self.root)
        with open(files[0], "rb") as a, open(files[1], "rb") as b:
            self.assertNotEqual(a.read(), b.read())
        self.run_out(self.m.recover, self.ctx)
        self.assertMoved()

    def test_kill_between_the_two_vdf_writes_of_the_library_entry(self):
        self.crash_os_in("u_write_vdf_pair", "rename", 1, nth=1)
        self.assertEqual(self.journals(), [])
        sv = self.splan()
        self.assertIn("differ only by the ~/Bulk/steam entry", self.out)
        self.assertEqual(sv["apps"][0]["state"], "move")
        self.assertEqual(self.sapply(), 0, self.out)
        self.assertMoved()

    def test_kill_while_deleting_the_old_copies_is_finished(self):
        self.crash_os_in("u_steam_finish", "unlink", 1)
        self.assertEqual(self.m.jread("steam-570")["phase"], "finishing")
        self.run_out(self.m.recover, self.ctx)
        self.assertMoved()

    def write_late(self, also_new=False):
        def make(real):
            def late(j):
                p = j["parts"][0]
                with open(os.path.join(p["old"], "game/dota/pak01_dir.vpk"), "w") as f:
                    f.write("late")
                if also_new:
                    with open(os.path.join(p["dst"], "game/dota/new.cfg"), "w") as f:
                        f.write("new")
                return real(j)
            return late
        self.patch("u_steam_late", make)

    def test_late_write_into_the_old_copy_is_undone(self):
        self.write_late()
        self.assertEqual(self.sapply(), 1, self.out)
        self.assertIn("written to during the move: undone", self.out)
        self.assertInMain()
        with open(self.S("common/dota 2 beta/game/dota/pak01_dir.vpk")) as f:
            self.assertEqual(f.read(), "late")
        self.assertNoLeftovers()

    def test_writes_on_both_sides_keep_both(self):
        self.write_late(also_new=True)
        self.assertEqual(self.sapply(), 1, self.out)
        self.assertIn("written to on both sides", self.out)
        self.assertEqual(self.journals(), ["journal-steam-570.json"])
        self.assertTrue(os.path.isfile(self.D("common/dota 2 beta/game/dota/new.cfg")))
        old = [n for n in os.listdir(self.S("common")) if ".bulk-old-" in n]
        self.assertEqual(len(old), 1)
        _, text = self.report()
        self.assertIn("steam-570: NEEDS A LOOK", text)
        self.assertEqual(self.sapply(), 1)        # stays until resolved
        shutil.rmtree(os.path.join(self.S("common"), old[0]))
        for n in os.listdir(self.S("")):
            if ".bulk-old-" in n:
                os.remove(self.S(n))
        for n in os.listdir(self.S("workshop")):
            p = self.S("workshop/" + n)
            if ".bulk-old-" in n:
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        shutil.rmtree(self.S("workshop/content/570.bulk-old-" + old[0].rsplit(".bulk-old-", 1)[1]))
        self.run_out(self.m.recover, self.ctx)
        self.assertIn("the move is complete", self.out)
        self.assertMoved()

    def test_source_created_again_is_not_deleted(self):
        self.crash_os_in("u_steam_switch", "rename", 1)
        os.makedirs(self.S("common/dota 2 beta/new"))
        stuck = self.run_out(self.m.recover, self.ctx)
        self.assertEqual(stuck, ["steam-570"])
        self.assertIn("was created again", self.out)
        old = [n for n in os.listdir(self.S("common")) if ".bulk-old-" in n]
        self.assertEqual(len(old), 1)
        self.assertEqual(self.sapply(), 1)
        shutil.rmtree(self.S("common/dota 2 beta"))
        self.recovered_then_moved()

    def test_vdf_changed_meanwhile_stops_the_other_apps(self):
        def make(real):
            def switch(j):
                r = real(j)
                os.utime(self.m.lf_files(self.root)[0], ns=(1, 1))
                return r
            return switch
        real = self.patch("u_steam_switch", make)
        self.assertEqual(self.sapply("570", "1628350"), 1, self.out)
        self.assertIn("changed meanwhile", self.out)
        self.assertTrue(os.path.isfile(self.S("appmanifest_1628350.acf")))   # not touched
        self.assertEqual(self.m.jread("steam-570")["phase"], "switched")
        self.m.u_steam_switch = real
        self.run_out(self.m.recover, self.ctx)
        self.assertMoved()

    # ---- integration ----
    def test_boot_recovers_a_steam_journal_without_the_flag(self):
        self.crash_os_in("write_vdf_pair", "rename", 0, nth=2)
        self.assertFalse(os.path.exists(self.m.FLAG))
        _, text = self.report()
        self.assertIn("steam-570: an interrupted Steam game move is recorded", text)
        self.assertEqual(self.m.journals(), ["steam-570"])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.boot(self.ctx), 0)
        self.assertMoved()

    def test_bulk_status_points_to_bulk_steam(self):
        self.m.BIG = 1
        _, text = self.report()
        self.assertIn("'cst btrfs bulk steam plan'", text)

    def test_steam_running_detection(self):
        m = self.m
        proc, run = os.path.join(self.tmp, "proc"), os.path.join(self.tmp, "run-user")
        m.PROC, m.RUN_USER = proc, run
        self.put(os.path.join(proc, "10", "comm"), "bash\n")
        self.put(os.path.join(proc, "11", "comm"), "reaper\n")      # the REAPER audio editor
        self.assertEqual(self.real_steam_running(self.ctx), [])
        self.put(os.path.join(proc, "12", "comm"), "steamwebhelper\n")
        self.put(os.path.join(run, "1000", ".flatpak", "99", "info"), "[Application]\nname=com.valvesoftware.Steam\n")
        self.put(os.path.join(run, "1000", ".flatpak", "99", "pid"), "10\n")
        self.put(os.path.join(self.home, ".steam", "steam.pid"), "12\n")
        why = self.real_steam_running(self.ctx)
        self.assertEqual(why, sorted(["process 12 (steamwebhelper)", "Flatpak instance 99 (pid 10)",
                                      "~/.steam/steam.pid names the live process 12"]))

    def test_cli_plan_as_user_changes_nothing(self):
        env = dict(os.environ, BTRFS_BULK_HOME=self.home, BTRFS_BULK_ETC=self.m.ETC,
                   BTRFS_BULK_STATE=self.m.STATE, BTRFS_BULK_LOCK=self.m.LOCK)
        before = self.tree()
        r = subprocess.run([sys.executable, "-I", "-B", TOOL, "steam", "plan"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Dry run, nothing changed", r.stdout)
        self.assertEqual(self.tree(), before)
        r = subprocess.run([sys.executable, "-I", "-B", TOOL, "steam", "apply", "--app", "x"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


@unittest.skipUnless(os.environ.get("BTRFS_BULK_REAL_SCRATCH"),
                     "set BTRFS_BULK_REAL_SCRATCH to an empty folder on btrfs (real +C and reflinks)")
class RealBtrfsSteamTest(SteamTest):
    """The NOCOW case on a real btrfs, as the user: real +C flags (chattr's
    ioctl) and real reflinks; Steam, the busy check and flatpak are still
    fakes. BTRFS_BULK_REAL_SCRATCH=~/Workspace/.e2e-x (must exist, on btrfs;
    /tmp is often tmpfs) python3 -I -B btrfs/tests/test_btrfs_steam.py RealBtrfsSteamTest"""

    def setUp(self):
        import tempfile
        scratch = os.environ["BTRFS_BULK_REAL_SCRATCH"]
        old, tempfile.tempdir = tempfile.tempdir, scratch
        try:
            super().setUp()
        finally:
            tempfile.tempdir = old
        m, fresh = self.m, tb.load()
        self.assertEqual(m._fs_magic(self.tmp), m.BTRFS_MAGIC, "the scratch folder is not on btrfs")
        for name in ("is_nocow", "set_nocow", "copy_reflink"):
            setattr(m, name, getattr(fresh, name))

    def put_nocow(self, path, data):
        """A file with +C from its first byte (+C only takes on empty files)."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()
        self.m.set_nocow(path)
        with open(path, "ab") as f:
            f.write(data)

    def test_real_nocow_source(self):
        m = self.m
        for rel in self.NOCOW_PARTS:
            p = self.S(rel)
            files = {}
            for r, _, f in os.walk(p) if os.path.isdir(p) else [(os.path.dirname(p), [], [os.path.basename(p)])]:
                for n in f:
                    q = os.path.join(r, n)
                    if not os.path.islink(q):
                        with open(q, "rb") as fh:
                            files[q] = (fh.read(), os.stat(q).st_mode)
            for q, (data, mode) in files.items():
                os.remove(q)
                self.put_nocow(q, data if q.endswith(".acf") else data * 50000)
                os.chmod(q, mode)
        self.assertTrue(m.is_nocow(self.S("common/dota 2 beta/game/dota/pak01_dir.vpk")))
        self.assertFalse(m.is_nocow(self.T))
        # The host's failure: btrfs refuses the clone (EINVAL).
        with self.assertRaisesRegex(RuntimeError, "Invalid argument"):
            m.copy_reflink(self.S("appmanifest_570.acf"), os.path.join(self.T, "probe"))
        sv = self.splan()
        self.assertEqual(sv["apps"][0]["reflink_bytes"], 0)
        before = self.tree()
        self.assertUnchanged(before, self.sapply(), 1, "--copy")
        self.assertIn("must be copied byte by byte because 5 files in it have +C (NOCOW)", self.out)
        self.assertEqual(self.sapply(copy=True), 0, self.out)
        for r, d, f in os.walk(os.path.join(self.T, "steamapps")):
            for n in d + f:
                q = os.path.join(r, n)
                if not os.path.islink(q):
                    self.assertFalse(m.is_nocow(q), q)
        with open(self.D("common/dota 2 beta/game/dota/pak01_dir.vpk"), "rb") as f:
            self.assertEqual(f.read(), b"vpk" * 50000)
        self.assertNoLeftovers()
        before = self.tree()
        self.assertEqual(self.sapply(copy=True), 0, self.out)
        self.assertEqual(self.tree(), before)
        self.assertIn("Everything asked for is in ~/Bulk/steam already.", self.out)
        self.assertNotIn("start:", self.out)


# Only its own test: the inherited ones rely on the fake +C flags.
for _name in [n for n in dir(SteamTest) if n.startswith("test_")]:
    setattr(RealBtrfsSteamTest, _name, None)


class BulkSteamFrontendTest(unittest.TestCase):
    """`cst btrfs bulk steam` in setup-bulk.bash (sourced the way the frontend
    does; the root-only steps are stubs that only record their call)."""

    def calls(self, *args):
        stubs = ('exec 3>&1; snap_home() { echo snap_home >&3; }; snap_install_runtime() { echo install_runtime >&3; }; '
                 'install_text() { cat >/dev/null; echo "install_text $1" >&3; }; '
                 'systemctl() { echo "systemctl $*" >&3; }; bulk_run() { echo "bulk_run $*"; return 5; }; ')
        r = subprocess.run(["bash", "-c", 'SRC="$1"; shift; . "${SRC}/setup-snapshots.bash"; target_user=u; '
                            + stubs + 'bulk_cmd "$@"; echo "rc $?"', "_", os.path.dirname(TOOL), *args],
                           capture_output=True, text=True, check=True)
        return r.stdout.splitlines()

    def test_steam_apply_installs_the_boot_runtime_first(self):
        self.assertEqual(self.calls("steam", "apply", "--app", "570"), [
            "snap_home", "install_runtime", "install_text /etc/btrfs-setup/bulk.conf",
            "systemctl enable btrfs-bulk-boot.service", "bulk_run steam apply --app 570", "rc 5"])

    def test_steam_plan_installs_nothing(self):
        self.assertEqual(self.calls("steam", "plan"), ["snap_home", "bulk_run steam plan", "rc 5"])


if __name__ == "__main__":
    unittest.main()

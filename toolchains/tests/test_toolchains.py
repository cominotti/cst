"""Unit tests for the toolchains module: the command line and target
selection of toolchains/cst-toolchains, its settings file, `config` (the
cargo and uv settings that `cst btrfs snapshots setup` used to write itself,
and still writes through this module), `auto on|off` (the unit files, and
the removal of what the earlier `cst btrfs cargo-gc` left), `status`, and
the shell frontend toolchains-setup.sh. Everything runs in a scratch home;
systemctl is a stub that only records its arguments.

Run from the repository root (-B: no __pycache__ in the repository):
    python3 -I -B -m unittest discover -s toolchains/tests -t . -v
"""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

from toolchains.tests.test_prune_rust_build import NOW, TOOL, load  # noqa: E402

TOOLCHAINS = os.path.dirname(TOOL)
REPO = os.path.dirname(TOOLCHAINS)
SETUP = os.path.join(REPO, "toolchains-setup.sh")
BTRFS = os.path.join(REPO, "btrfs")
DEFAULTS = os.path.join(TOOLCHAINS, "toolchains.conf")
OLD_MARKER = "# managed-by: btrfs-setup.sh (CST)"

# What `snapshots setup` wrote before the toolchains module existed (the
# printf lines of snap_user_configs, verbatim): the module writes the same.
OLD_CARGO = ('\n# Added by btrfs-setup.sh (CST): intermediate build artifacts live in\n'
             '# ~/.cache (a subvolume excluded from /home snapshots); final ones stay in target/.\n'
             '[build]\nbuild-dir = "%s/.cache/cargo-build/{workspace-path-hash}"\n')
OLD_UV = ('# Added by btrfs-setup.sh (CST): ~/.cache is its own subvolume and\n'
          '# hardlinks cannot cross subvolumes; reflink clones can.\nlink-mode = "clone"\n')

FAKE_SYSTEMCTL = """#!/bin/sh
echo "$*" >>"$SYSTEMCTL_LOG"
"""


class Scratch(unittest.TestCase):
    """A scratch home, a stub systemctl, and the program loaded as a module."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-toolchains-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.log = os.path.join(self.tmp, "systemctl.log")
        self.stub = os.path.join(self.tmp, "systemctl")
        with open(self.stub, "w") as f:
            f.write(FAKE_SYSTEMCTL)
        os.chmod(self.stub, 0o755)
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home,
            "CST_TOOLCHAINS_HERE": "1",
            "CST_TOOLCHAINS_NOW": str(NOW),
            "CST_TOOLCHAINS_SYSTEMCTL": self.stub,
            "SYSTEMCTL_LOG": self.log,
        }
        saved = {k: os.environ.get(k) for k in self.env if k not in ("PATH", "HOME")}
        os.environ.update({k: v for k, v in self.env.items() if k not in ("PATH", "HOME")})

        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        self.m = load()
        self.m.JOURNAL = False
        self.m.cargo_procs = lambda uid=None: []

    def calls(self):
        try:
            with open(self.log) as f:
                return [c for c in f.read().splitlines()
                        if " is-" not in c and " show " not in c and not c.endswith(" show-environment")]
        except FileNotFoundError:
            return []

    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.m.main([*args, "--home", self.home])
        return rc, out.getvalue() + err.getvalue()

    def write(self, rel, text):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def read(self, rel):
        with open(os.path.join(self.home, rel)) as f:
            return f.read()

    def csts_cargo_config(self):
        self.write(".cargo/config.toml", OLD_CARGO % self.home)


class CliTest(Scratch):
    def test_parse(self):
        p = self.m.parse_args
        self.assertEqual(p([])["cmd"], "help")
        self.assertEqual(p(["--help"])["cmd"], "help")
        a = p(["prune", "plan", "rust", "rust:build", "--home", "/h"])
        self.assertEqual((a["cmd"], a["action"], a["targets"], a["home"]), ("prune", "plan", ["rust", "rust:build"], "/h"))
        self.assertEqual(p(["--home=/x", "status", "--brief"])["home"], "/x")
        self.assertTrue(p(["status", "--brief"])["brief"])
        self.assertTrue(p(["auto", "on", "--unless-off"])["unless_off"])
        self.assertEqual(p(["config", "apply"])["action"], "apply")
        for bad in (["prune"], ["prune", "delete"], ["prune", "apply", "--force"], ["auto"], ["auto", "maybe"],
                    ["auto", "off", "--unless-off"], ["config"], ["config", "plan", "x"], ["status", "-v"],
                    ["cargo-gc", "plan"], ["status", "--home"], ["status", "--home="], ["prune", "apply", "--home="],
                    ["auto", "on", "--home", ""]):
            with self.assertRaises(self.m.Usage, msg=bad):
                p(bad)

    def test_empty_home_never_means_the_real_home(self):
        # `--home="$H"` with $H empty: exit 2 before anything is read or changed.
        for args in (["prune", "apply", "--home="], ["auto", "off", "--home="], ["config", "apply", "--home", ""]):
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = self.m.main(args)
            self.assertEqual(rc, 2, args)
            self.assertIn("--home needs a folder", err.getvalue())
        self.assertEqual(self.calls(), [])

    def test_main_exit_codes(self):
        self.assertEqual(self.run_main("prune", "delete")[0], 2)
        rc, out = self.run_main("help")
        self.assertEqual(rc, 0)
        self.assertIn("rust:build", out)
        rc, out = self.run_main("prune", "plan", "go:mod")
        self.assertEqual(rc, 2)
        self.assertIn("not implemented yet", out)


class SelectTest(Scratch):
    def sel(self, *names):
        return [(c.ID, named) for c, named in self.m.select_targets(list(names), self.m.Settings(self.home))]

    def test_selection(self):
        self.assertEqual(self.sel(), [("rust:build", False), ("python:uv", False)])
        self.assertEqual(self.sel("rust"), [("rust:build", True)])
        self.assertEqual(self.sel("python"), [("python:uv", True)])
        self.assertEqual(self.sel("rust:build", "rust"), [("rust:build", True)])
        for name, msg in (("go:mod", "not implemented yet"), ("go", "planned: go:build, go:mod"),
                          ("node", "planned: node:npm"), ("rust:registry", "not implemented yet"),
                          ("foo", "unknown target"), ("rust:foo", "unknown target")):
            with self.assertRaisesRegex(self.m.Usage, msg, msg=name):
                self.sel(name)

    def test_disabled_target(self):
        self.write(".config/cst/toolchains.conf", "RUST_BUILD_ENABLED=no\nPYTHON_UV_ENABLED=no\n")
        self.assertEqual(self.sel(), [])
        self.assertEqual(self.sel("rust:build"), [("rust:build", True)])
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("no target is enabled", out)
        # Named on the command line it runs, and says why.
        self.csts_cargo_config()
        rc, out = self.run_main("prune", "plan", "rust")
        self.assertEqual(rc, 0, out)
        self.assertIn("runs only because it was named", out)


class DefaultsTest(Scratch):
    def test_defaults_file_is_complete_and_marked(self):
        with open(DEFAULTS) as f:
            text = f.read()
        self.assertIn(self.m.MARKER, text)
        raw = self.m.parse_conf(text, self.m.known_keys())
        self.assertEqual(set(raw), self.m.known_keys())
        self.assertEqual(raw, {"RUST_BUILD_ENABLED": "yes", "RUST_BUILD_MAX_IDLE": "48h",
                               "RUST_BUILD_MAX_TOTAL": "150G", "RUST_BUILD_MIN_KEEP": "2h",
                               "RUST_BUILD_ORPHANS": "yes", "RUST_BUILD_INCREMENTAL_IDLE": "6h",
                               "PYTHON_UV_ENABLED": "yes", "PYTHON_UV_INTERVAL": "1d",
                               "RUST_LEAN_DEBUGINFO": "no"})

    def test_every_target_is_documented(self):
        with open(os.path.join(TOOLCHAINS, "README.md")) as f:
            readme = f.read()
        for tid in list(self.m.TARGETS) + list(self.m.PLANNED):
            self.assertIn(f"`{tid}`", readme, tid)

    def test_missing_defaults_stop_the_run(self):
        self.m.defaults_path = lambda: os.path.join(self.tmp, "nope.conf")
        with self.assertRaisesRegex(self.m.ConfigError, "missing"):
            self.m.Settings(self.home)


class ConfigTest(Scratch):
    def test_writes_the_old_lines_once(self):
        rc, out = self.run_main("config", "plan")
        self.assertEqual(rc, 0, out)
        self.assertIn("write", out)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cargo")))      # plan writes nothing
        rc, out = self.run_main("config", "apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(".cargo/config.toml"), OLD_CARGO % self.home)
        self.assertEqual(self.read(".config/uv/uv.toml"), OLD_UV)
        self.assertEqual(out.count("written:"), 2)
        rc, out = self.run_main("config", "apply")
        self.assertIn(f"kept:      {self.home}/.cargo/config.toml (build-dir already set)", out)
        self.assertIn(f"kept:      {self.home}/.config/uv/uv.toml (link-mode already set)", out)
        self.assertEqual(self.read(".cargo/config.toml"), OLD_CARGO % self.home)
        # The line written is the one prune accepts.
        self.assertEqual(self.m.resolve_root(self.home), None)
        rc, out = self.run_main("status")
        self.assertIn("build-dir = CST's line", out)

    def test_existing_settings_are_never_changed(self):
        cargo = '[net]\ngit-fetch-with-cli = true\n[build]\njobs = 8\n'
        uv = 'native-tls = true\n[pip]\nindex-url = "x"\n'
        self.write(".cargo/config.toml", cargo)
        self.write(".config/uv/uv.toml", uv)
        rc, out = self.run_main("config", "apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("already configures [build]: add there by hand", out)
        self.assertIn(f'build-dir = "{self.home}/.cache/cargo-build/{{workspace-path-hash}}"', out)
        self.assertIn('has tables: add link-mode = "clone" at its top by hand', out)
        self.assertEqual(self.read(".cargo/config.toml"), cargo)
        self.assertEqual(self.read(".config/uv/uv.toml"), uv)
        # build.build-dir dotted, and the user's own build-dir: kept.
        for text in ('build.build-dir = "/x"\n', '[build]\n  build-dir = "/elsewhere"\n'):
            self.write(".cargo/config.toml", text)
            rc, out = self.run_main("config", "apply")
            self.assertIn("build-dir already set", out)
            self.assertEqual(self.read(".cargo/config.toml"), text)
        rc, out = self.run_main("config", "plan")
        self.assertIn("your own value", out)

    def test_appends_after_a_missing_newline(self):
        self.write(".cargo/config.toml", '[net]\noffline = false')
        self.write(".config/uv/uv.toml", 'native-tls = true')
        rc, out = self.run_main("config", "apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(".cargo/config.toml"), '[net]\noffline = false\n' + OLD_CARGO % self.home)
        self.assertEqual(self.read(".config/uv/uv.toml"), 'native-tls = true\n' + OLD_UV)


    @unittest.skipIf(os.getuid() == 0, "root writes into read-only folders")
    def test_write_error_is_reported_and_the_other_setting_still_written(self):
        # ~/.cargo not writable (root-owned after a `sudo cargo`, say): no
        # traceback, exit 1, and uv.toml is still written.
        cargo_dir = os.path.join(self.home, ".cargo")
        os.makedirs(cargo_dir)
        os.chmod(cargo_dir, 0o555)
        self.addCleanup(os.chmod, cargo_dir, 0o755)
        rc, out = self.run_main("config", "apply")
        self.assertEqual(rc, 1, out)
        self.assertIn(f"cannot write {cargo_dir}/config.toml: Permission denied: left as it is", out)
        self.assertNotIn("Traceback", out)
        self.assertFalse(os.path.exists(os.path.join(cargo_dir, "config.toml")))
        self.assertEqual(self.read(".config/uv/uv.toml"), OLD_UV)
        self.assertIn(f"written:   {self.home}/.config/uv/uv.toml", out)


class SnapshotsSetupTest(Scratch):
    """snap_user_configs of btrfs/setup-snapshots.bash, sourced like the
    frontend does, with runuser and Chrome's step as stubs: the cargo and uv
    lines are still written (by the toolchains module), and the timer is
    turned on only when the user did not turn it off."""

    def run_setup(self, prog="cst btrfs"):
        script = ('set -Eeuo pipefail; SRC="$1"; H="$2"; PROG="$3"; . "${SRC}/setup-snapshots.bash"; target_user=u\n'
                  'runuser() { shift 3; "$@"; }\n'       # runuser -u USER -- CMD...: CMD as is
                  'snap_home() { echo "$H"; }\n'
                  'snap_chrome() { echo "chrome $1"; }\n'
                  'snap_user_configs "$H"\n')
        return subprocess.run(["bash", "-c", script, "_", BTRFS, self.home, prog],
                              capture_output=True, text=True, env=self.env)

    def test_setup_writes_the_same_lines_and_turns_the_timer_on(self):
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(".cargo/config.toml"), OLD_CARGO % self.home)
        self.assertEqual(self.read(".config/uv/uv.toml"), OLD_UV)
        self.assertIn(f"written:   {self.home}/.cargo/config.toml", r.stdout)
        self.assertIn("enable --now cst-toolchains-prune.timer", "\n".join(self.calls()))
        self.assertTrue(os.path.isfile(os.path.join(self.home, ".config/systemd/user/cst-toolchains-prune.timer")))
        self.assertIn("chrome apply", r.stdout)
        # Again: nothing new.
        r = self.run_setup()
        self.assertIn("build-dir already set", r.stdout)
        self.assertEqual(self.read(".cargo/config.toml"), OLD_CARGO % self.home)

    def test_setup_leaves_the_timer_off_after_auto_off(self):
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        open(self.log, "w").close()
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("left off", r.stdout)
        self.assertIn("cst toolchains auto on", r.stdout)       # the hint names the module
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config/systemd/user/cst-toolchains-prune.timer")))

    def test_preview_has_no_apply_hint(self):
        # setup applies the settings itself after the confirmation: its
        # preview must not tell the user to run `config apply` by hand.
        rc, out = self.run_main("config", "plan")
        self.assertIn("Apply with:", out)                 # the plain command still has it
        script = ('set -Eeuo pipefail; SRC="$1"; H="$2"; PROG="$3"; . "${SRC}/setup-snapshots.bash"; target_user=u\n'
                  'runuser() { shift 3; "$@"; }\n'
                  'snap_home() { echo "$H"; }\n'
                  'snap_toolchains_config_preview\n')
        r = subprocess.run(["bash", "-c", script, "_", BTRFS, self.home, "cst btrfs"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("    write", r.stdout)
        self.assertIn(".cargo/config.toml", r.stdout)
        self.assertNotIn("Apply with", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cargo")))     # preview writes nothing
        # Nothing to write (no hint either): the pipeline still succeeds.
        self.csts_cargo_config()
        self.write(".config/uv/uv.toml", OLD_UV)
        r = subprocess.run(["bash", "-c", script, "_", BTRFS, self.home, "cst btrfs"],
                           capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("    ok", r.stdout)

    def test_hint_name_without_cst(self):
        r = subprocess.run(["bash", "-c", 'SRC="$1"; PROG="$2"; . "${SRC}/setup-snapshots.bash"; snap_toolchains_prog',
                            "_", BTRFS, "./btrfs-setup.sh"], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), os.path.join(REPO, "toolchains-setup.sh"))


class AutoTest(Scratch):
    def unit(self, name):
        return os.path.join(self.home, ".config/systemd/user", name)

    def test_unit_files_and_copies(self):
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        with open(self.unit("cst-toolchains-prune.timer")) as f:
            timer = f.read()
        with open(self.unit("cst-toolchains-prune.service")) as f:
            service = f.read()
        for line in ("OnCalendar=hourly", "RandomizedDelaySec=10min", "Persistent=true", "WantedBy=timers.target"):
            self.assertIn(line, timer)
        for line in ("Type=oneshot", "Nice=19", "IOSchedulingClass=idle", "NoNewPrivileges=yes",
                     "ExecStart=/usr/bin/python3 -I %h/.local/lib/cst/cst-toolchains prune apply",
                     "ConditionPathExists=%h/.local/lib/cst/toolchains.conf"):
            self.assertIn(line, service)
        with open(DEFAULTS) as f:
            self.assertEqual(self.read(".local/lib/cst/toolchains.conf"), f.read())
        self.assertIn(self.m.MARKER, self.read(".local/lib/cst/cst-toolchains"))
        self.assertEqual(self.calls(), ["--user daemon-reload", "--user enable --now cst-toolchains-prune.timer"])
        # The installed copy runs with its own defaults file (as the timer does).
        r = subprocess.run([sys.executable, "-I", os.path.join(self.home, ".local/lib/cst/cst-toolchains"),
                            "prune", "plan", "--home", self.home], capture_output=True, text=True, env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("MAX_IDLE 48h", r.stdout)
        # auto off: everything gone; the folder too when it is empty, but not
        # what the bluetooth module keeps there.
        self.write(".local/lib/cst/bt-ldac-watch", "x")
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        for rel in (".local/lib/cst/cst-toolchains", ".local/lib/cst/toolchains.conf",
                    ".config/systemd/user/cst-toolchains-prune.timer", ".config/systemd/user/cst-toolchains-prune.service"):
            self.assertFalse(os.path.exists(os.path.join(self.home, rel)), rel)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".local/lib/cst/bt-ldac-watch")))
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config/cst/toolchains-auto.off")))

    def test_refuses_without_a_target_to_run(self):
        # No cargo config: rust:build refuses; no uv on PATH: python:uv refuses.
        with mock.patch.dict(os.environ, {"PATH": self.tmp}):
            rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 1)
        self.assertIn("build-dir is not set", out)
        self.assertIn("uv not found on PATH", out)
        self.write(".config/cst/toolchains.conf", "RUST_BUILD_ENABLED=no\nPYTHON_UV_ENABLED=no\n")
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 1)
        self.assertIn("no target is enabled", out)
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(self.unit("cst-toolchains-prune.timer")))

    def stale(self, ours=True):
        mark = OLD_MARKER if ours else "# my own"
        paths = [self.write(".config/systemd/user/cst-cargo-build-gc.timer", f"{mark}: x\n[Timer]\n"),
                 self.write(".config/systemd/user/cst-cargo-build-gc.service", f"{mark}: x\n[Service]\n"),
                 self.write(".local/lib/cst/cargo-build-gc", f"#!/usr/bin/python3\nMARKER = \"{mark}\"\n"),
                 self.write(".config/cst/cargo-gc.disabled", f"{mark}: written by cargo-gc disable\n")]
        if ours:
            paths.append(self.write(".local/state/cst/cargo-build-gc.json", "{}"))
        return paths

    def test_stale_cargo_gc_is_removed_by_auto_on(self):
        self.csts_cargo_config()
        paths = self.stale()
        self.write(".config/cst/cargo-gc.conf", "MAX_IDLE=3d\n")
        rc, out = self.run_main("status")
        self.assertIn("stale: ~/.config/systemd/user/cst-cargo-build-gc.timer", out)
        self.assertIn("cargo-gc.conf is no longer read", out)
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        for p in paths:
            self.assertFalse(os.path.exists(p), p)
        self.assertEqual(self.calls()[:2], ["--user disable --now cst-cargo-build-gc.timer", "--user daemon-reload"])
        self.assertIn("left by the earlier `cst btrfs cargo-gc`", out)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config/cst/cargo-gc.conf")))   # the user's

    def test_stale_cargo_gc_is_removed_by_auto_off(self):
        paths = self.stale()
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        for p in paths:
            self.assertFalse(os.path.exists(p), p)
        self.assertIn("--user disable --now cst-cargo-build-gc.timer", self.calls())
        # Also with --unless-off after auto off (what snapshots setup runs).
        paths = self.stale()
        rc, out = self.run_main("auto", "on", "--unless-off")
        self.assertIn("left off", out)
        for p in paths:
            self.assertFalse(os.path.exists(p), p)

    def test_foreign_files_with_the_old_names_are_left_alone(self):
        paths = self.stale(ours=False)
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        for p in paths:
            self.assertTrue(os.path.exists(p), p)
        self.assertIn("not written by CST", out)
        self.assertNotIn("--user disable --now cst-cargo-build-gc.timer", self.calls())


class StatusTest(Scratch):
    def test_status_sections(self):
        self.csts_cargo_config()
        os.makedirs(os.path.join(self.home, ".cache/cargo-build/aa/0123456789abcd/debug"))
        # go's module cache moved into ~/Bulk by `cst btrfs bulk`: shown where it really is.
        real = os.path.join(self.home, "Bulk/toolchains/go/pkg/mod")
        os.makedirs(real)
        os.makedirs(os.path.join(self.home, "go/pkg"))
        os.symlink(real, os.path.join(self.home, "go/pkg/mod"))
        rc, out = self.run_main("status")
        self.assertEqual(rc, 0, out)
        for text in ("auto: timer cst-toolchains-prune.timer: not installed", "== rust:build:",
                     "where:    ~/.cache/cargo-build", "policy:   enabled; MAX_IDLE 48h", "last prune: none recorded",
                     "in 1 folder(s)", "go:mod", "~/go/pkg/mod -> ~/Bulk/toolchains/go/pkg/mod",
                     "== toolchain settings", "~/.config/uv/uv.toml"):
            self.assertIn(text, out)
        rc, out = self.run_main("status", "--brief")
        self.assertEqual(out.splitlines()[1], "rust:build: last prune: none recorded")

    def test_bad_settings_are_shown(self):
        self.write(".config/cst/toolchains.conf", "RUST_BUILD_MAX_TOTAL=150\n")
        rc, out = self.run_main("status")
        self.assertEqual(rc, 0, out)
        self.assertIn("BAD SETTINGS", out)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 1)
        self.assertIn("not a size", out)


class FrontendTest(Scratch):
    def sh(self, *args, script=SETUP):
        env = dict(self.env, CST_PROG="cst toolchains")
        return subprocess.run(["bash", script, *args], capture_output=True, text=True, env=env)

    def test_help_and_usage(self):
        r = self.sh("help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("auto on [--unless-off]", r.stdout)
        self.assertIn("Usage: cst toolchains", r.stdout)
        r = self.sh("bogus")
        self.assertEqual(r.returncode, 2)
        r = self.sh("prune", "bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("prune: plan or apply", r.stderr)

    def test_runs_the_program_on_the_scratch_home(self):
        r = self.sh("config", "apply", "--home", self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(".config/uv/uv.toml"), OLD_UV)

    def test_cst_knows_the_module(self):
        r = subprocess.run([os.path.join(REPO, "cst"), "help"], capture_output=True, text=True, env=self.env)
        self.assertIn("toolchains", r.stdout)
        r = subprocess.run([os.path.join(REPO, "cst"), "toolchains", "help"], capture_output=True, text=True,
                           env=self.env)
        self.assertEqual(r.returncode, 0)
        self.assertIn("toolchains-setup.sh", r.stdout)

    def test_btrfs_has_no_cargo_gc_any_more(self):
        r = self.sh("cargo-gc", "plan", script=os.path.join(REPO, "btrfs-setup.sh"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("Unknown command: cargo-gc", r.stderr)


if __name__ == "__main__":
    unittest.main()

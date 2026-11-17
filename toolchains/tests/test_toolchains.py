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

from __future__ import annotations

import contextlib
import fcntl
import io
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

from cst_core.adapters.zipapp import build_python_zipapp  # noqa: E402
from cst_core.domain.errors import Refused, Usage  # noqa: E402
from toolchains import adapters as tc_adapters, cli as tc_cli, domain as tc_domain  # noqa: E402
from toolchains.tests.test_prune_rust_build import HELP, NOW, TOOL, _ToolchainsModule, load  # noqa: E402

TOOLCHAINS = os.path.dirname(TOOL)
REPO = os.path.dirname(TOOLCHAINS)
SETUP = os.path.join(REPO, "toolchains-setup.sh")
BTRFS = os.path.join(REPO, "btrfs")
DEFAULTS = os.path.join(TOOLCHAINS, "toolchains.conf")
OLD_MARKER = "# managed-by: btrfs-setup.sh (CST)"

# What `snapshots setup` wrote before the toolchains module existed (the
# printf lines of snap_user_configs, verbatim): the module writes the same.
OLD_CARGO = (
    "\n# Added by btrfs-setup.sh (CST): intermediate build artifacts live in\n"
    "# ~/.cache (a subvolume excluded from /home snapshots); final ones stay in target/.\n"
    '[build]\nbuild-dir = "%s/.cache/cargo-build/{workspace-path-hash}"\n'
)
OLD_UV = (
    "# Added by btrfs-setup.sh (CST): ~/.cache is its own subvolume and\n"
    '# hardlinks cannot cross subvolumes; reflink clones can.\nlink-mode = "clone"\n'
)

FAKE_LOG = """#!/bin/sh
echo "$*" >>"$SYSTEMCTL_LOG"
"""
# is-active answers like systemctl: "inactive" and exit 4 for a unit the
# manager does not know (the default); SYSTEMCTL_ACTIVE=active: a timer
# that still runs (exit 0); another state: exit 3.
FAKE_ANSWERS = """case " $* " in *" is-active "*)
    case "${SYSTEMCTL_ACTIVE:-unknown}" in
        active) echo active; exit 0;;
        unknown) echo inactive; exit 4;;
        *) echo "$SYSTEMCTL_ACTIVE"; exit 3;;
    esac;;
esac
"""
FAKE_SYSTEMCTL = FAKE_LOG + FAKE_ANSWERS


class Scratch(unittest.TestCase):
    """A scratch home, a stub systemctl, and the program loaded as a module."""

    def setUp(self) -> None:
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-toolchains-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.log = os.path.join(self.tmp, "systemctl.log")
        self.stub = os.path.join(self.tmp, "systemctl")
        with open(self.stub, "w") as f:
            f.write(FAKE_SYSTEMCTL)
        os.chmod(self.stub, 0o755)
        self.env: dict[str, str] = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": self.home,
            "CST_TOOLCHAINS_HERE": "1",
            "CST_TOOLCHAINS_NOW": str(NOW),
            "CST_TOOLCHAINS_SYSTEMCTL": self.stub,
            "SYSTEMCTL_LOG": self.log,
            # status must never reach a real sccache server (their sockets live here).
            "XDG_RUNTIME_DIR": os.path.join(self.tmp, "no-runtime-dir"),
        }
        saved = {k: os.environ.get(k) for k in self.env if k not in ("PATH", "HOME")}
        os.environ.update({k: v for k, v in self.env.items() if k not in ("PATH", "HOME")})

        def restore() -> None:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(restore)
        self.m: _ToolchainsModule = load()
        self.addCleanup(self.m.restore)
        self.m.JOURNAL = False
        self.m.cargo_procs = lambda uid=None: []

    # The user manager refusing a call (or not answering: the same exit 1
    # from systemctl). The stub records every call, and fails the ones that
    # contain `word` with systemctl's own error text.
    def failing_stub(self, word: str) -> str:
        stub = os.path.join(self.tmp, "systemctl-failing")
        with open(stub, "w") as f:
            f.write(
                FAKE_LOG
                + f'case " $* " in *" {word} "*) echo "Failed to connect to bus: No medium found" >&2; exit 1;; esac\n'
                + FAKE_ANSWERS
            )
        os.chmod(stub, 0o755)
        return stub

    def failing(self, word: str) -> contextlib.AbstractContextManager[None]:
        return mock.patch.dict(os.environ, {"CST_TOOLCHAINS_SYSTEMCTL": self.failing_stub(word)})

    def calls(self) -> list[str]:
        try:
            with open(self.log) as f:
                return [
                    c
                    for c in f.read().splitlines()
                    if " is-" not in c and " show " not in c and not c.endswith(" show-environment")
                ]
        except FileNotFoundError:
            return []

    def run_main(self, *args: str) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.m.main([*args, "--home", self.home], HELP, TOOL)
        return rc, out.getvalue() + err.getvalue()

    def write(self, rel: str, text: str) -> str:
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def read(self, rel: str) -> str:
        with open(os.path.join(self.home, rel)) as f:
            return f.read()

    def csts_cargo_config(self) -> None:
        self.write(".cargo/config.toml", OLD_CARGO % self.home)


class CliTest(Scratch):
    def test_parse(self) -> None:
        p = self.m.parse_args
        self.assertEqual(p([])["cmd"], "help")
        self.assertEqual(p(["--help"])["cmd"], "help")
        a = p(["prune", "plan", "rust", "rust:build", "--home", "/h"])
        self.assertEqual(
            (a["cmd"], a["action"], a["targets"], a["home"]), ("prune", "plan", ["rust", "rust:build"], "/h")
        )
        self.assertEqual(p(["--home=/x", "status", "--brief"])["home"], "/x")
        self.assertTrue(p(["status", "--brief"])["brief"])
        self.assertTrue(p(["auto", "on", "--unless-off"])["unless_off"])
        self.assertEqual(p(["config", "apply"])["action"], "apply")
        for bad in (
            ["prune"],
            ["prune", "delete"],
            ["prune", "apply", "--force"],
            ["auto"],
            ["auto", "maybe"],
            ["auto", "off", "--unless-off"],
            ["config"],
            ["config", "plan", "x"],
            ["status", "-v"],
            ["cargo-gc", "plan"],
            ["status", "--home"],
            ["status", "--home="],
            ["prune", "apply", "--home="],
            ["auto", "on", "--home", ""],
        ):
            with self.assertRaises(self.m.Usage, msg=bad):
                p(bad)

    def test_empty_home_never_means_the_real_home(self) -> None:
        # `--home="$H"` with $H empty: exit 2 before anything is read or changed.
        for args in (["prune", "apply", "--home="], ["auto", "off", "--home="], ["config", "apply", "--home", ""]):
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = self.m.main(args, HELP, TOOL)
            self.assertEqual(rc, 2, args)
            self.assertIn("--home needs a folder", err.getvalue())
        self.assertEqual(self.calls(), [])

    def test_main_exit_codes(self) -> None:
        self.assertEqual(self.run_main("prune", "delete")[0], 2)
        rc, out = self.run_main("help")
        self.assertEqual(rc, 0)
        self.assertIn("rust:build", out)
        rc, out = self.run_main("prune", "plan", "go:mod")
        self.assertEqual(rc, 2)
        self.assertIn("not implemented yet", out)


class SelectTest(Scratch):
    def sel(self, *names: str) -> list[tuple[str, bool]]:
        return [(c.ID, named) for c, named in self.m.select_targets(list(names), self.m.Settings(self.home))]

    def test_selection(self) -> None:
        self.assertEqual(self.sel(), [("rust:build", False), ("python:uv", False)])
        self.assertEqual(self.sel("rust"), [("rust:build", True)])
        self.assertEqual(self.sel("python"), [("python:uv", True)])
        self.assertEqual(self.sel("rust:build", "rust"), [("rust:build", True)])
        for name, msg in (
            ("go:mod", "not implemented yet"),
            ("go", "planned: go:build, go:mod"),
            ("node", "planned: node:npm"),
            ("rust:registry", "not implemented yet"),
            ("foo", "unknown target"),
            ("rust:foo", "unknown target"),
        ):
            with self.assertRaisesRegex(self.m.Usage, msg, msg=name):
                self.sel(name)

    def test_disabled_target(self) -> None:
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
    def test_defaults_file_is_complete_and_marked(self) -> None:
        with open(DEFAULTS) as f:
            text = f.read()
        self.assertIn(self.m.MARKER, text)
        raw = self.m.parse_conf(text, self.m.known_keys())
        self.assertEqual(set(raw), self.m.known_keys())
        self.assertEqual(
            raw,
            {
                "RUST_BUILD_ENABLED": "yes",
                "RUST_BUILD_MAX_IDLE": "48h",
                "RUST_BUILD_MAX_TOTAL": "150G",
                "RUST_BUILD_MIN_KEEP": "2h",
                "RUST_BUILD_ORPHANS": "yes",
                "RUST_BUILD_INCREMENTAL_IDLE": "6h",
                "PYTHON_UV_ENABLED": "yes",
                "PYTHON_UV_INTERVAL": "1d",
                "RUST_LEAN_DEBUGINFO": "no",
                "RUST_SCCACHE": "no",
                "RUST_SCCACHE_CACHE_SIZE": "25G",
                "RUST_SCCACHE_WORKSPACE": "yes",
            },
        )

    def test_every_target_is_documented(self) -> None:
        with open(os.path.join(TOOLCHAINS, "README.md")) as f:
            readme = f.read()
        for tid in list(self.m.TARGETS) + list(self.m.PLANNED):
            self.assertIn(f"`{tid}`", readme, tid)

    def test_missing_defaults_stop_the_run(self) -> None:
        self.m.defaults_path = lambda: os.path.join(self.tmp, "nope.conf")
        with self.assertRaisesRegex(self.m.ConfigError, "missing"):
            self.m.Settings(self.home)


class ConfigTest(Scratch):
    def test_writes_the_old_lines_once(self) -> None:
        rc, out = self.run_main("config", "plan")
        self.assertEqual(rc, 0, out)
        self.assertIn("write", out)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cargo")))  # plan writes nothing
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

    def test_existing_settings_are_never_changed(self) -> None:
        cargo = "[net]\ngit-fetch-with-cli = true\n[build]\njobs = 8\n"
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

    def test_appends_after_a_missing_newline(self) -> None:
        self.write(".cargo/config.toml", "[net]\noffline = false")
        self.write(".config/uv/uv.toml", "native-tls = true")
        rc, out = self.run_main("config", "apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read(".cargo/config.toml"), "[net]\noffline = false\n" + OLD_CARGO % self.home)
        self.assertEqual(self.read(".config/uv/uv.toml"), "native-tls = true\n" + OLD_UV)

    @unittest.skipIf(os.getuid() == 0, "root writes into read-only folders")
    def test_write_error_is_reported_and_the_other_setting_still_written(self) -> None:
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

    def run_setup(self, prog: str = "cst btrfs", env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        script = (
            'set -Eeuo pipefail; SRC="$1"; H="$2"; PROG="$3"; . "${SRC}/setup-snapshots.bash"; target_user=u\n'
            'runuser() { shift 3; "$@"; }\n'  # runuser -u USER -- CMD...: CMD as is
            'snap_home() { echo "$H"; }\n'
            'snap_chrome() { echo "chrome $1"; }\n'
            'snap_user_configs "$H"\n'
        )
        return subprocess.run(
            ["bash", "-c", script, "_", BTRFS, self.home, prog],
            check=False,
            capture_output=True,
            text=True,
            env=env or self.env,
            stdin=subprocess.DEVNULL,
        )

    def test_setup_writes_the_same_lines_and_turns_the_timer_on(self) -> None:
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

    def test_setup_leaves_the_timer_off_after_auto_off(self) -> None:
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        open(self.log, "w").close()
        r = self.run_setup()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("left off", r.stdout)
        self.assertIn("cst toolchains auto on", r.stdout)  # the hint names the module
        self.assertEqual(self.calls(), [])
        self.assertFalse(os.path.exists(os.path.join(self.home, ".config/systemd/user/cst-toolchains-prune.timer")))

    def test_setup_note_when_auto_on_reports_a_problem(self) -> None:
        # After `auto off`, a stale file of the earlier cargo-gc is removed
        # but the daemon-reload fails: `auto on --unless-off` exits 1 on its
        # "left off" path. The NOTE must not claim the timer "was not turned
        # on" nor tell the user to turn it on against their own choice.
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        self.write(".config/systemd/user/cst-cargo-build-gc.service", f"{OLD_MARKER}: x\n[Service]\n")
        r = self.run_setup(env=dict(self.env, CST_TOOLCHAINS_SYSTEMCTL=self.failing_stub("daemon-reload")))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("left off", r.stdout)
        self.assertIn("NOTE: cst toolchains auto on --unless-off reported a problem (see above)", r.stderr)
        self.assertNotIn("was not turned on", r.stderr)

    def test_preview_has_no_apply_hint(self) -> None:
        # setup applies the settings itself after the confirmation: its
        # preview must not tell the user to run `config apply` by hand.
        rc, out = self.run_main("config", "plan")
        self.assertIn("Apply with:", out)  # the plain command still has it
        script = (
            'set -Eeuo pipefail; SRC="$1"; H="$2"; PROG="$3"; . "${SRC}/setup-snapshots.bash"; target_user=u\n'
            'runuser() { shift 3; "$@"; }\n'
            'snap_home() { echo "$H"; }\n'
            "snap_toolchains_config_preview\n"
        )
        r = subprocess.run(
            ["bash", "-c", script, "_", BTRFS, self.home, "cst btrfs"],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("    write", r.stdout)
        self.assertIn(".cargo/config.toml", r.stdout)
        self.assertNotIn("Apply with", r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".cargo")))  # preview writes nothing
        # Nothing to write (no hint either): the pipeline still succeeds.
        self.csts_cargo_config()
        self.write(".config/uv/uv.toml", OLD_UV)
        r = subprocess.run(
            ["bash", "-c", script, "_", BTRFS, self.home, "cst btrfs"],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("    ok", r.stdout)

    def test_hint_name_without_cst(self) -> None:
        r = subprocess.run(
            [
                "bash",
                "-c",
                'SRC="$1"; PROG="$2"; . "${SRC}/setup-snapshots.bash"; snap_toolchains_prog',
                "_",
                BTRFS,
                "./btrfs-setup.sh",
            ],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        self.assertEqual(r.stdout.strip(), os.path.join(REPO, "toolchains-setup.sh"))


class AutoTest(Scratch):
    def unit(self, name: str) -> str:
        return os.path.join(self.home, ".config/systemd/user", name)

    def test_unit_files_and_copies(self) -> None:
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        with open(self.unit("cst-toolchains-prune.timer")) as f:
            timer = f.read()
        with open(self.unit("cst-toolchains-prune.service")) as f:
            service = f.read()
        for line in ("OnCalendar=hourly", "RandomizedDelaySec=10min", "Persistent=true", "WantedBy=timers.target"):
            self.assertIn(line, timer)
        for line in (
            "Type=oneshot",
            "Nice=19",
            "IOSchedulingClass=idle",
            "NoNewPrivileges=yes",
            "ExecStart=/usr/bin/python3 -I %h/.local/lib/cst/cst-toolchains prune apply",
            "ConditionPathExists=%h/.local/lib/cst/toolchains.conf",
        ):
            self.assertIn(line, service)
        with open(DEFAULTS) as f:
            self.assertEqual(self.read(".local/lib/cst/toolchains.conf"), f.read())
        with open(os.path.join(self.home, ".local/lib/cst/cst-toolchains"), "rb") as f:
            self.assertIn(self.m.MARKER.encode(), f.read())
        self.assertEqual(self.calls(), ["--user daemon-reload", "--user enable --now cst-toolchains-prune.timer"])
        # The installed copy runs with its own defaults file (as the timer does).
        r = subprocess.run(
            [
                sys.executable,
                "-I",
                os.path.join(self.home, ".local/lib/cst/cst-toolchains"),
                "prune",
                "plan",
                "--home",
                self.home,
            ],
            check=False,
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("MAX_IDLE 48h", r.stdout)
        # auto off: everything gone; the folder too when it is empty, but not
        # what the bluetooth module keeps there.
        self.write(".local/lib/cst/bt-ldac-watch", "x")
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        for rel in (
            ".local/lib/cst/cst-toolchains",
            ".local/lib/cst/toolchains.conf",
            ".config/systemd/user/cst-toolchains-prune.timer",
            ".config/systemd/user/cst-toolchains-prune.service",
        ):
            self.assertFalse(os.path.exists(os.path.join(self.home, rel)), rel)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".local/lib/cst/bt-ldac-watch")))
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config/cst/toolchains-auto.off")))

    def test_refuses_without_a_target_to_run(self) -> None:
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

    def stale(self, ours: bool = True) -> list[str]:
        mark = OLD_MARKER if ours else "# my own"
        paths = [
            self.write(".config/systemd/user/cst-cargo-build-gc.timer", f"{mark}: x\n[Timer]\n"),
            self.write(".config/systemd/user/cst-cargo-build-gc.service", f"{mark}: x\n[Service]\n"),
            self.write(".local/lib/cst/cargo-build-gc", f'#!/usr/bin/python3\nMARKER = "{mark}"\n'),
            self.write(".config/cst/cargo-gc.disabled", f"{mark}: written by cargo-gc disable\n"),
        ]
        if ours:
            paths.append(self.write(".local/state/cst/cargo-build-gc.json", "{}"))
        return paths

    def test_stale_cargo_gc_is_removed_by_auto_on(self) -> None:
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
        self.assertTrue(os.path.exists(os.path.join(self.home, ".config/cst/cargo-gc.conf")))  # the user's

    def test_stale_cargo_gc_is_removed_by_auto_off(self) -> None:
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

    def test_foreign_files_with_the_old_names_are_left_alone(self) -> None:
        paths = self.stale(ours=False)
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        for p in paths:
            self.assertTrue(os.path.exists(p), p)
        self.assertIn("not written by CST", out)
        self.assertNotIn("--user disable --now cst-cargo-build-gc.timer", self.calls())

    INSTALLED = (
        ".config/systemd/user/cst-toolchains-prune.timer",
        ".config/systemd/user/cst-toolchains-prune.service",
        ".local/lib/cst/cst-toolchains",
        ".local/lib/cst/toolchains.conf",
    )

    def exists(self, rel: str) -> bool:
        return os.path.exists(os.path.join(self.home, rel))

    def test_auto_off_removes_nothing_when_the_timer_cannot_be_stopped(self) -> None:
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        with self.failing("disable"):
            rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 1, out)
        self.assertIn("systemctl --user disable --now cst-toolchains-prune.timer failed", out)
        self.assertIn("Failed to connect to bus", out)
        self.assertIn("nothing was removed", out)
        for rel in self.INSTALLED:
            self.assertTrue(self.exists(rel), rel)
        self.assertFalse(self.exists(".config/cst/toolchains-auto.off"))
        self.assertEqual(self.calls()[-1], "--user disable --now cst-toolchains-prune.timer")  # no reload after it
        # Once the manager answers, a second run finishes the job.
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        for rel in self.INSTALLED:
            self.assertFalse(self.exists(rel), rel)
        self.assertTrue(self.exists(".config/cst/toolchains-auto.off"))

    def test_auto_off_reports_a_failed_daemon_reload_and_still_cleans_up(self) -> None:
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        with self.failing("daemon-reload"):
            rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 1, out)
        self.assertIn("systemctl --user daemon-reload failed", out)
        self.assertIn("run `systemctl --user daemon-reload` once your user manager answers", out)
        # The timer was stopped first; the cleanup is not undone and goes on.
        self.assertEqual(self.calls()[-2:], ["--user disable --now cst-toolchains-prune.timer", "--user daemon-reload"])
        for rel in self.INSTALLED:
            self.assertFalse(self.exists(rel), rel)
        self.assertTrue(self.exists(".config/cst/toolchains-auto.off"))
        self.assertIn("no longer pruned automatically", out)

    def test_stale_timer_that_cannot_be_stopped_refuses_auto_on_and_off(self) -> None:
        self.csts_cargo_config()
        paths = self.stale()
        for args in (("auto", "on"), ("auto", "off")):
            with self.failing("disable"):
                rc, out = self.run_main(*args)
            self.assertEqual(rc, 1, (args, out))
            self.assertIn("systemctl --user disable --now cst-cargo-build-gc.timer failed", out)
            self.assertIn("nothing was removed", out)
            for p in paths:
                self.assertTrue(os.path.exists(p), (args, p))
            self.assertEqual(self.calls()[-1], "--user disable --now cst-cargo-build-gc.timer")
        self.assertFalse(self.exists(".config/systemd/user/cst-toolchains-prune.timer"))  # auto on stopped early
        self.assertFalse(self.exists(".config/cst/toolchains-auto.off"))  # so did auto off

    def test_stale_files_with_a_failed_daemon_reload_give_a_nonzero_exit(self) -> None:
        # What snapshots setup runs after `auto off`: the stale files go, the
        # failed reload is reported, and the exit code says so (snapshots
        # setup prints a NOTE for it).
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        paths = self.stale()
        with self.failing("daemon-reload"):
            rc, out = self.run_main("auto", "on", "--unless-off")
        self.assertEqual(rc, 1, out)
        self.assertIn("left off", out)
        self.assertIn("systemctl --user daemon-reload failed", out)
        for p in paths:
            self.assertFalse(os.path.exists(p), p)

    def test_auto_off_stops_both_timers_before_it_removes_stale_files(self) -> None:
        # Only the service of the earlier cargo-gc is left (not its timer),
        # and the user manager cannot be reached: no file may go before the
        # manager has said that the timer is stopped.
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        stale = self.write(".config/systemd/user/cst-cargo-build-gc.service", f"{OLD_MARKER}: x\n[Service]\n")
        with self.failing("--user"):
            rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 1, out)
        self.assertIn("nothing was removed", out)
        self.assertNotIn("removed:", out)
        self.assertTrue(os.path.exists(stale))
        for rel in self.INSTALLED:
            self.assertTrue(self.exists(rel), rel)
        # Also with no timer of ours left to disable: asking whether it
        # still runs has to work first.
        for rel in self.INSTALLED:
            os.unlink(os.path.join(self.home, rel))
        with self.failing("--user"):
            rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 1, out)
        self.assertIn("systemctl --user is-active cst-toolchains-prune.timer failed", out)
        self.assertIn("nothing was removed", out)
        self.assertTrue(os.path.exists(stale))
        self.assertFalse(self.exists(".config/cst/toolchains-auto.off"))

    def orphan(self) -> str:
        """What `auto off` left before it checked its `disable`: the timer
        still running (SYSTEMCTL_ACTIVE), its files gone, and the link that
        enabled it pointing at nothing."""
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        for rel in self.INSTALLED:
            os.unlink(os.path.join(self.home, rel))
        link = os.path.join(self.home, ".config/systemd/user/timers.target.wants/cst-toolchains-prune.timer")
        os.makedirs(os.path.dirname(link))
        os.symlink(os.path.join(self.home, ".config/systemd/user/cst-toolchains-prune.timer"), link)
        open(self.log, "w").close()
        return link

    def test_auto_off_stops_a_timer_left_running_without_its_file(self) -> None:
        link = self.orphan()
        with mock.patch.dict(os.environ, {"SYSTEMCTL_ACTIVE": "active"}):
            rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        self.assertIn("stopped:   cst-toolchains-prune.timer", out)
        self.assertEqual(self.calls(), ["--user stop cst-toolchains-prune.timer", "--user daemon-reload"])
        self.assertFalse(os.path.lexists(link))
        self.assertIn("removed:   ~/.config/systemd/user/timers.target.wants/cst-toolchains-prune.timer", out)
        self.assertTrue(self.exists(".config/cst/toolchains-auto.off"))
        # Run again: nothing more to do.
        open(self.log, "w").close()
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.calls(), [])
        self.assertNotIn("stopped:", out)

    def test_status_shows_a_timer_left_running_without_its_file(self) -> None:
        self.orphan()
        with mock.patch.dict(os.environ, {"SYSTEMCTL_ACTIVE": "active"}):
            rc, out = self.run_main("status")
        self.assertEqual(rc, 0, out)
        self.assertIn("still running although its file is gone", out)
        self.assertIn("cst toolchains auto off stops it", out)
        # Not running: "not installed", as before.
        rc, out = self.run_main("status")
        self.assertIn("cst-toolchains-prune.timer: not installed", out)

    def test_auto_off_removes_the_dangling_link_of_a_timer_that_no_longer_runs(self) -> None:
        # The same leftover after a reboot: the timer is not running (is-active
        # exits 3), only the link is left. No stop; the link goes.
        link = self.orphan()
        with mock.patch.dict(os.environ, {"SYSTEMCTL_ACTIVE": "inactive"}):
            rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("stopped:", out)
        self.assertEqual(self.calls(), ["--user daemon-reload"])
        self.assertFalse(os.path.lexists(link))

    def test_auto_off_leaves_everything_when_an_orphan_timer_cannot_be_stopped(self) -> None:
        link = self.orphan()
        with self.failing("stop"), mock.patch.dict(os.environ, {"SYSTEMCTL_ACTIVE": "active"}):
            rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 1, out)
        self.assertIn("systemctl --user stop cst-toolchains-prune.timer failed", out)
        self.assertIn("nothing was removed", out)
        self.assertTrue(os.path.islink(link))
        self.assertFalse(self.exists(".config/cst/toolchains-auto.off"))

    def test_auto_off_keeps_a_link_to_a_timer_file_that_is_there(self) -> None:
        # A timer of the same name that is not CST's: its file and its link stay.
        self.write(".config/systemd/user/cst-toolchains-prune.timer", "[Timer]\nOnCalendar=daily\n")
        link = os.path.join(self.home, ".config/systemd/user/timers.target.wants/cst-toolchains-prune.timer")
        os.makedirs(os.path.dirname(link))
        os.symlink("../cst-toolchains-prune.timer", link)
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        self.assertIn("not written by CST", out)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(self.calls(), [])

    def run_split(self, *args: str) -> tuple[int, str, str]:
        """run_main, with stdout and stderr apart: warnings and errors go to stderr."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = self.m.main([*args, "--home", self.home], HELP, TOOL)
        return rc, out.getvalue(), err.getvalue()

    def raw_calls(self) -> list[str]:
        """Every systemctl call, the questions (is-active, ...) too."""
        try:
            with open(self.log) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def test_auto_off_leaves_a_foreign_unit_file_and_program_copy_alone(self) -> None:
        # A service file and a program copy with CST's names that CST did not
        # write: auto off keeps them and says so on stderr; what is CST's goes.
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        service = self.write(".config/systemd/user/cst-toolchains-prune.service", "[Service]\nExecStart=/bin/true\n")
        prog = self.write(".local/lib/cst/cst-toolchains", "#!/bin/sh\necho mine\n")
        open(self.log, "w").close()
        rc, out, err = self.run_split("auto", "off")
        self.assertEqual(rc, 0, out + err)
        with open(service) as f:
            self.assertEqual(f.read(), "[Service]\nExecStart=/bin/true\n")
        with open(prog) as f:
            self.assertEqual(f.read(), "#!/bin/sh\necho mine\n")
        self.assertFalse(self.exists(".config/systemd/user/cst-toolchains-prune.timer"))
        self.assertFalse(self.exists(".local/lib/cst/toolchains.conf"))
        left = (
            "left alone: ~/.config/systemd/user/cst-toolchains-prune.service (not written by CST)",
            "left alone: ~/.local/lib/cst/cst-toolchains (not written by CST)",
        )
        for line in left:
            self.assertIn(line, err)
            self.assertNotIn(line, out)
        self.assertIn("removed:   ~/.config/systemd/user/cst-toolchains-prune.timer", out)
        self.assertIn("removed:   ~/.local/lib/cst/toolchains.conf", out)
        self.assertEqual(self.calls(), ["--user disable --now cst-toolchains-prune.timer", "--user daemon-reload"])
        # Run again: the foreign files are reported again, and only they (the
        # files that are gone are not "left alone"); nothing else is called.
        open(self.log, "w").close()
        rc, out, err = self.run_split("auto", "off")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([x for x in err.splitlines() if x.startswith("left alone:")], list(left))
        self.assertNotIn("left alone", out)
        self.assertEqual(self.calls(), [])
        self.assertTrue(os.path.exists(service))
        self.assertTrue(os.path.exists(prog))

    def test_auto_on_keeps_a_link_to_the_same_program_and_replaces_one_to_an_older_copy(self) -> None:
        # The installer's one link rule for a user destination (cst_core.domain.install.link_read_through):
        # a link is judged by what it points to; a write replaces the link, never its target.
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        prog = os.path.join(self.home, ".local/lib/cst/cst-toolchains")
        kept = os.path.join(self.home, "dotfiles-cst-toolchains")
        os.rename(prog, kept)
        os.symlink(kept, prog)
        self.assertEqual(tc_cli.unit_states(self.home)[".local/lib/cst/cst-toolchains"], "ok")
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        self.assertIn("unchanged: ~/.local/lib/cst/cst-toolchains", out)
        self.assertTrue(os.path.islink(prog))
        old = '#!/usr/bin/python3 -I\n"""cst-toolchains: old copy"""\nMARKER = "# managed-by: toolchains-setup.sh (CST)"\n'
        with open(kept, "w") as f:
            f.write(old)
        self.assertEqual(tc_cli.unit_states(self.home)[".local/lib/cst/cst-toolchains"], "change")
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        self.assertIn("written:   ~/.local/lib/cst/cst-toolchains", out)
        self.assertFalse(os.path.islink(prog))
        with open(kept) as f:
            self.assertEqual(f.read(), old, "the link was written through")

    def test_auto_on_upgrades_a_plain_script_copy_to_the_zipapp(self) -> None:
        # Older CST versions installed cst-toolchains as a plain script (it
        # holds CST's marker): auto on replaces it with the zipapp, and a
        # second run changes nothing. A foreign program is refused.
        self.csts_cargo_config()
        old = '#!/usr/bin/python3 -I\n"""cst-toolchains: old copy"""\nMARKER = "# managed-by: toolchains-setup.sh (CST)"\n'
        prog = self.write(".local/lib/cst/cst-toolchains", old)
        status = io.StringIO()
        with contextlib.redirect_stdout(status):
            tc_cli.cmd_status(self.home, NOW, False)
        self.assertEqual(tc_cli.unit_states(self.home)[".local/lib/cst/cst-toolchains"], "change")
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        self.assertIn("written:   ~/.local/lib/cst/cst-toolchains", out)
        with open(prog, "rb") as f:
            self.assertEqual(f.read(), build_python_zipapp(REPO, "toolchains/cst-toolchains"))
        self.assertEqual(os.stat(prog).st_mode & 0o777, 0o755)
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        self.assertIn("unchanged: ~/.local/lib/cst/cst-toolchains", out)
        self.write(".local/lib/cst/cst-toolchains", "#!/bin/sh\necho mine\n")
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 1, out)
        self.assertIn("not written by CST, left alone: ~/.local/lib/cst/cst-toolchains", out)
        with open(prog) as f:
            self.assertEqual(f.read(), "#!/bin/sh\necho mine\n")

    def test_auto_off_never_stops_a_foreign_timer_as_an_orphan(self) -> None:
        # A running timer whose file is there but not CST's is not the
        # orphan that auto off stops: the manager is not even asked about it.
        self.write(".config/systemd/user/cst-toolchains-prune.timer", "[Timer]\nOnCalendar=daily\n")
        with mock.patch.dict(os.environ, {"SYSTEMCTL_ACTIVE": "active"}):
            rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.raw_calls(), [])
        self.assertNotIn("stopped:", out)
        self.assertTrue(self.exists(".config/systemd/user/cst-toolchains-prune.timer"))

    def test_auto_off_exits_1_when_the_reload_after_the_stale_files_fails(self) -> None:
        # Only the earlier cargo-gc's files are there: the one daemon-reload
        # is remove_stale's, and its failure alone gives exit 1 (on stderr).
        paths = self.stale()
        with self.failing("daemon-reload"):
            rc, out, err = self.run_split("auto", "off")
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(self.calls(), ["--user disable --now cst-cargo-build-gc.timer", "--user daemon-reload"])
        self.assertIn("systemctl --user daemon-reload failed", err)
        self.assertIn("run `systemctl --user daemon-reload` once your user manager answers", err)
        self.assertNotIn("daemon-reload", out)
        for p in paths:
            self.assertFalse(os.path.exists(p), p)
        self.assertTrue(self.exists(".config/cst/toolchains-auto.off"))
        self.assertIn("no longer pruned automatically", out)

    def test_stale_files_after_a_foreign_one_are_still_removed(self) -> None:
        # The earlier timer is not CST's (it comes first): it stays, is not
        # stopped, and every other stale file still goes.
        paths = self.stale()
        timer = self.write(".config/systemd/user/cst-cargo-build-gc.timer", "# my own\n[Timer]\n")
        rc, out, err = self.run_split("auto", "off")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(os.path.exists(timer))
        for p in paths:
            if p != timer:
                self.assertFalse(os.path.exists(p), p)
        line = "left alone: ~/.config/systemd/user/cst-cargo-build-gc.timer (not written by CST)"
        self.assertIn(line, err)
        self.assertNotIn(line, out)
        self.assertIn("removed:   ~/.config/systemd/user/cst-cargo-build-gc.service", out)
        self.assertEqual(self.calls(), ["--user daemon-reload"])  # the service file went; no disable

    def test_stale_files_reload_only_when_a_unit_file_was_removed(self) -> None:
        # The earlier cargo-gc's files without its unit files: removed, and
        # no daemon-reload (no unit changed), by auto off and by auto on.
        for args in (("auto", "off"), ("auto", "on", "--unless-off")):
            paths = self.stale()
            for unit in paths[:2]:
                os.unlink(unit)
            paths = paths[2:]
            open(self.log, "w").close()
            rc, out = self.run_main(*args)
            self.assertEqual(rc, 0, (args, out))
            for p in paths:
                self.assertFalse(os.path.exists(p), (args, p))
            self.assertEqual(self.calls(), [], args)

    def test_auto_off_removes_the_state_file_of_every_target(self) -> None:
        states = [self.m.state_path(self.home, tid) for tid in self.m.TARGETS]
        self.assertEqual(len(states), 2)
        for path in states:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("{}")
        other = self.write(".local/state/cst/other.json", "{}")
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        for path in states:
            self.assertFalse(os.path.exists(path), path)
        self.assertTrue(os.path.exists(other))

    def test_auto_off_writes_the_off_file_with_mode_0644(self) -> None:
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        rc, out = self.run_main("auto", "off")
        self.assertEqual(rc, 0, out)
        mode = os.stat(os.path.join(self.home, ".config/cst/toolchains-auto.off")).st_mode
        self.assertEqual(stat.S_IMODE(mode), 0o644)

    def hold_lock(self) -> None:
        path = os.path.join(self.home, ".local/state/cst/toolchains.lock")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_auto_on_and_off_fail_while_another_run_holds_the_lock(self) -> None:
        self.csts_cargo_config()
        rc, out = self.run_main("auto", "on")
        self.assertEqual(rc, 0, out)
        open(self.log, "w").close()
        self.hold_lock()
        for args in (("auto", "off"), ("auto", "on"), ("auto", "on", "--unless-off")):
            rc, out = self.run_main(*args)
            self.assertEqual(rc, 1, (args, out))
            self.assertIn("another run is in progress", out)
            self.assertIn("run this again once it has finished", out)
        self.assertEqual(self.calls(), [])
        for rel in self.INSTALLED:
            self.assertTrue(self.exists(rel), rel)
        self.assertFalse(self.exists(".config/cst/toolchains-auto.off"))
        # A prune that finds another run going still exits 0 (the timer's run is not a failure).
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("another run is in progress: nothing done", out)


class StatusTest(Scratch):
    def test_status_sections(self) -> None:
        self.csts_cargo_config()
        os.makedirs(os.path.join(self.home, ".cache/cargo-build/aa/0123456789abcd/debug"))
        # go's module cache moved into ~/Bulk by `cst btrfs bulk`: shown where it really is.
        real = os.path.join(self.home, "Bulk/toolchains/go/pkg/mod")
        os.makedirs(real)
        os.makedirs(os.path.join(self.home, "go/pkg"))
        os.symlink(real, os.path.join(self.home, "go/pkg/mod"))
        rc, out = self.run_main("status")
        self.assertEqual(rc, 0, out)
        for text in (
            "auto: timer cst-toolchains-prune.timer: not installed",
            "== rust:build:",
            "where:    ~/.cache/cargo-build",
            "policy:   enabled; MAX_IDLE 48h",
            "last prune: none recorded",
            "in 1 folder(s)",
            "go:mod",
            "~/go/pkg/mod -> ~/Bulk/toolchains/go/pkg/mod",
            "== toolchain settings",
            "~/.config/uv/uv.toml",
        ):
            self.assertIn(text, out)
        rc, out = self.run_main("status", "--brief")
        self.assertEqual(out.splitlines()[1], "rust:build: last prune: none recorded")

    def test_a_state_file_that_is_not_an_object_is_none_recorded(self) -> None:
        # Intended change from main, where status --brief stopped with an AttributeError traceback
        # (exit 1) on such a file: CST always writes an object, so anything else is no record.
        self.write(".local/state/cst/toolchains-rust-build.json", "[1, 2]")
        self.write(".local/state/cst/toolchains-python-uv.json", '"x"')
        rc, out = self.run_main("status", "--brief")
        self.assertEqual(rc, 0, out)
        self.assertIn("rust:build: last prune: none recorded", out)
        self.assertIn("python:uv: last prune: none recorded", out)

    def test_bad_settings_are_shown(self) -> None:
        self.write(".config/cst/toolchains.conf", "RUST_BUILD_MAX_TOTAL=150\n")
        rc, out = self.run_main("status")
        self.assertEqual(rc, 0, out)
        self.assertIn("BAD SETTINGS", out)
        rc, out = self.run_main("prune", "apply")
        self.assertEqual(rc, 1)
        self.assertIn("not a size", out)


class FrontendTest(Scratch):
    def sh(self, *args: str, script: str = SETUP) -> subprocess.CompletedProcess[str]:
        env = dict(self.env, CST_PROG="cst toolchains")
        return subprocess.run(
            ["bash", script, *args], check=False, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL
        )

    def test_help_and_usage(self) -> None:
        r = self.sh("help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("auto on [--unless-off]", r.stdout)
        self.assertIn("Usage: cst toolchains", r.stdout)
        r = self.sh("bogus")
        self.assertEqual(r.returncode, 2)
        r = self.sh("prune", "bogus")
        self.assertEqual(r.returncode, 2)
        self.assertIn("prune: plan or apply", r.stderr)

    def test_runs_the_program_on_the_scratch_home(self) -> None:
        r = self.sh("config", "apply", "--home", self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.read(".config/uv/uv.toml"), OLD_UV)

    def test_cst_knows_the_module(self) -> None:
        r = subprocess.run(
            [os.path.join(REPO, "cst"), "help"], check=False, capture_output=True, text=True, env=self.env
        )
        self.assertIn("toolchains", r.stdout)
        r = subprocess.run(
            [os.path.join(REPO, "cst"), "toolchains", "help"], check=False, capture_output=True, text=True, env=self.env
        )
        self.assertEqual(r.returncode, 0)
        self.assertIn("toolchains-setup.sh", r.stdout)

    def test_btrfs_has_no_cargo_gc_any_more(self) -> None:
        r = self.sh("cargo-gc", "plan", script=os.path.join(REPO, "btrfs-setup.sh"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("Unknown command: cargo-gc", r.stderr)


class PackageLayersTest(Scratch):
    """The package's layers used directly: the rules in toolchains.domain,
    the user manager through toolchains.adapters, the commands through
    toolchains.cli (what the program's main() runs)."""

    def test_domain_parses_and_decides(self) -> None:
        a = tc_domain.parse_args(["status", "--brief"])
        self.assertEqual((a["cmd"], a["brief"]), ("status", True))
        with self.assertRaises(Usage):
            tc_domain.parse_args(["prune", "bogus"])
        old = tc_domain.Folder("/tmp/build", "aa", "bbbbbbbbbbbbbb")
        old.size, old.activity = 100, NOW - 1e6
        new = tc_domain.Folder("/tmp/build", "cc", "dddddddddddddd")
        new.size, new.activity = 100, NOW - 60
        pol = {"max_idle": 3600 * 48, "max_total": 150 * (1 << 30), "min_keep": 7200}
        self.assertEqual(
            [(f.key, action) for f, action, _ in tc_domain.decide([new, old], pol, NOW, {})],
            [("aa/bbbbbbbbbbbbbb", "delete"), ("cc/dddddddddddddd", "keep")],
        )

    def test_cli_commands_run_on_the_scratch_home(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc_cli.cmd_config(self.home, "apply"), 0)
        self.assertEqual(self.read(".cargo/config.toml"), OLD_CARGO % self.home)
        self.assertEqual(self.read(".config/uv/uv.toml"), OLD_UV)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tc_cli.cmd_status(self.home, NOW, True), 0)
            self.assertEqual(tc_cli.cmd_prune(self.home, NOW, "plan", []), 0)
            self.assertEqual(tc_cli.cmd_config(self.home, "plan"), 0)
            self.assertEqual(tc_cli.cmd_auto_on(self.home, NOW), 0)
            self.assertEqual(tc_cli.cmd_auto_off(self.home, NOW), 0)

    def test_systemctl_adapter_uses_the_configured_stub(self) -> None:
        with mock.patch.dict(os.environ, {"SYSTEMCTL_ACTIVE": "active"}):
            self.assertEqual(tc_adapters.systemctl("is-active", "cst-toolchains-prune.timer"), "active")
        self.assertIsNone(tc_adapters.manager_path())
        with self.failing("is-active"), self.assertRaises(Refused):
            tc_adapters.systemctl("is-active", "cst-toolchains-prune.timer", check=True)


class ParseSizeTest(unittest.TestCase):
    """MAX_TOTAL and friends: a number with its unit, or 0/off for no cap."""

    def test_units_multiply(self) -> None:
        self.assertEqual(tc_domain.parse_size("150G"), 150 << 30)
        self.assertEqual(tc_domain.parse_size(" 1.5k "), 1536)
        self.assertEqual(tc_domain.parse_size("2TiB"), 2 << 40)
        self.assertEqual(tc_domain.parse_size("3mb"), 3 << 20)

    def test_no_cap_is_zero(self) -> None:
        for text in ("0", "0.0", "off", "none", "no", "OFF"):
            self.assertEqual(tc_domain.parse_size(text), 0, text)

    def test_a_bare_number_is_refused(self) -> None:
        for text in ("150", "1x", "", "G"):
            with self.subTest(text=text), self.assertRaises(tc_domain.ConfigError):
                tc_domain.parse_size(text)


class ClosedPipeTest(Scratch):
    def test_status_and_plans_stop_quietly_on_a_closed_pipe(self) -> None:
        # pr-a preexisting #12: `cst toolchains status | head -1` ended in "Exception ignored ...
        # BrokenPipeError" (exit 120). The read-only commands now end on SIGPIPE, silently.
        # Only what needs no fake of this process (the subprocess sees none): help and the brief status.
        env = {**self.env, "PATH": "/usr/bin:/bin"}
        for args in (("help",), ("status", "--brief")):
            with self.subTest(args=args):
                r, w = os.pipe()
                os.close(r)
                try:
                    p = subprocess.run(
                        ["/usr/bin/python3", "-I", TOOL, *args, "--home", self.home],
                        check=False,
                        env=env,
                        stdout=w,
                        stderr=subprocess.PIPE,
                        stdin=subprocess.DEVNULL,
                        timeout=60,
                    )
                finally:
                    os.close(w)
                self.assertEqual((p.returncode, p.stderr), (-signal.SIGPIPE, b""))

    def test_only_the_commands_that_read_get_sigpipe_back(self) -> None:
        restored: list[tuple[str, str | None]] = []
        for cmd, action in (("help", None), ("status", None), ("prune", "plan"), ("config", "plan")):
            with mock.patch.object(
                tc_adapters, "restore_default_signals", lambda c=cmd, a=action: restored.append((c, a))
            ):
                tc_adapters.output_mode(cmd, action)
        self.assertEqual(restored, [("help", None), ("status", None), ("prune", "plan"), ("config", "plan")])
        with mock.patch.object(tc_adapters, "restore_default_signals", side_effect=AssertionError("restored")):
            for cmd, action in (("prune", "apply"), ("config", "apply"), ("auto", "on"), ("auto", "off")):
                tc_adapters.output_mode(cmd, action)  # a lock to release or files to write: Python's handling stays


if __name__ == "__main__":
    unittest.main()

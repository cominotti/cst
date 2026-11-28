# SPDX-License-Identifier: MPL-2.0
"""Unit tests for `cst toolchains config init [--plan] [--force]`."""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import override
from unittest import mock

sys.dont_write_bytecode = True

from cst_core.adapters.zipapp import build_python_zipapp
from cst_core.domain.errors import Usage
from toolchains import cli as tc_cli, wiring as tc_wiring
from toolchains.domain import common as tcd_common, config_init as tcd_config_init, prune as tcd_prune

TOOLCHAINS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(TOOLCHAINS)
TOOL = os.path.join(TOOLCHAINS, "cst-toolchains")
SETUP = os.path.join(REPO, "toolchains-setup.sh")
DEFAULTS = os.path.join(TOOLCHAINS, "toolchains.conf")


def _read_defaults() -> str:
    with open(DEFAULTS, encoding="utf-8") as fh:
        return fh.read()


class ConfigInitArgvTest(unittest.TestCase):
    """CLI parsing of `cst toolchains config init`."""

    def test_valid_flags(self) -> None:
        self.assertEqual(
            tcd_common.parse_args(["config", "init"]),
            {
                "cmd": tcd_common.ToolchainsCommand.CONFIG,
                "action": tcd_common.ToolchainsAction.INIT,
                "targets": [],
                "brief": False,
                "unless_off": False,
                "home": None,
                "lean": None,
                "sccache": None,
                "plan": False,
                "force": False,
                "user": None,
                "size": None,
            },
        )
        parsed = tcd_common.parse_args(["config", "init", "--plan", "--force", "--home", "/tmp/h"])
        self.assertTrue(parsed["plan"])
        self.assertTrue(parsed["force"])
        self.assertEqual(parsed["home"], "/tmp/h")

    def test_rejects_bad_flags_and_duplicates(self) -> None:
        for bad in (
            ["config", "init", "--plan", "--plan"],
            ["config", "init", "--force", "--force"],
            ["config", "init", "--sccache"],
            ["config", "init", "--lean-debuginfo"],
            ["config", "init", "--bogus"],
            ["config", "bogus"],
        ):
            with self.subTest(bad=bad), self.assertRaises(Usage):
                tcd_common.parse_args(bad)


class ConfigInitDomainTest(unittest.TestCase):
    """Pure domain rendering and active cargo override detection."""

    def test_render_fresh_defaults_has_every_key_commented_and_parses_empty(self) -> None:
        defaults_text = _read_defaults()
        rendered = tcd_config_init.render_starter_conf(defaults_text)
        self.assertNotIn("SPDX-License-Identifier", rendered)
        self.assertNotIn("Do not edit them here", rendered)
        self.assertIn("User settings for `cst toolchains`", rendered)
        self.assertEqual(tcd_common.parse_conf(rendered, tcd_prune.known_keys()), {})
        for key in sorted(tcd_prune.known_keys()):
            self.assertIn(f"# {key}=", rendered)

    def test_render_with_overrides_is_idempotent_and_keeps_default_comments(self) -> None:
        defaults_text = _read_defaults()
        overrides = {
            "RUST_INCREMENTAL": "off",
            "RUST_SCCACHE": "yes",
            "RUST_SCCACHE_CACHE_SIZE": "25G",  # matches default: stays commented
            "RUST_BUILD_MAX_IDLE": "7d",
        }
        rendered = tcd_config_init.render_starter_conf(defaults_text, overrides)
        self.assertIn("# default: RUST_INCREMENTAL=auto\nRUST_INCREMENTAL=off\n", rendered)
        self.assertIn("# default: RUST_SCCACHE=no\nRUST_SCCACHE=yes\n", rendered)
        self.assertIn("# default: RUST_BUILD_MAX_IDLE=48h\nRUST_BUILD_MAX_IDLE=7d\n", rendered)
        self.assertIn("# RUST_SCCACHE_CACHE_SIZE=25G\n", rendered)
        self.assertNotIn("\nRUST_SCCACHE_CACHE_SIZE=25G\n", rendered)

        reparsed = tcd_common.parse_conf(rendered, tcd_prune.known_keys())
        self.assertEqual(
            reparsed,
            {
                "RUST_INCREMENTAL": "off",
                "RUST_SCCACHE": "yes",
                "RUST_BUILD_MAX_IDLE": "7d",
            },
        )
        self.assertEqual(tcd_config_init.render_starter_conf(defaults_text, reparsed), rendered)

    def test_render_normalizes_blanks_and_strips_spdx_without_blank_line(self) -> None:
        defaults_text = _read_defaults()
        # Quoted values with ASCII blanks equal to default must not emit overrides or drift on 2nd pass.
        rendered = tcd_config_init.render_starter_conf(
            defaults_text,
            {"RUST_BUILD_MAX_IDLE": " 48h ", "RUST_BUILD_MAX_TOTAL": " 200G "},
        )
        self.assertIn("# RUST_BUILD_MAX_IDLE=48h\n", rendered)
        self.assertNotIn("\nRUST_BUILD_MAX_IDLE=", rendered)
        self.assertIn("\nRUST_BUILD_MAX_TOTAL=200G\n", rendered)
        reparsed = tcd_common.parse_conf(rendered, tcd_prune.known_keys())
        self.assertEqual(tcd_config_init.render_starter_conf(defaults_text, reparsed), rendered)

        # Even if a synthetic defaults_text omits a blank line after SPDX, SPDX does not leak into output.
        compact_defaults = "# SPDX-License-Identifier: MPL-2.0\n# managed-by: cst\n" + "\n".join(
            ln for ln in defaults_text.splitlines() if ln and not ln.lstrip().startswith("#")
        )
        compact_rendered = tcd_config_init.render_starter_conf(compact_defaults)
        self.assertNotIn("SPDX-License-Identifier", compact_rendered)
        self.assertNotIn("managed-by:", compact_rendered)

    def test_detect_cargo_overrides_reads_only_cst_settings(self) -> None:
        home = "/home/tester"
        wrapper_path = f"{home}/.local/libexec/cst/rustc-wrapper"
        cargo_toml = f"""
[build]
build-dir = "{home}/.cache/cargo-build/{{workspace-path-hash}}"
# Added by toolchains-setup.sh (CST) for RUST_SCCACHE=yes: rustc goes through sccache (~/.cache/sccache).
rustc-wrapper = "{wrapper_path}"
# Added by toolchains-setup.sh (CST) for RUST_INCREMENTAL=off: disable incremental compilation.
incremental = false

[profile.dev]
debug = "line-tables-only"
split-debuginfo = "unpacked"

[profile.dev.package."*"]
debug = false
"""
        wrapper_text = (
            "#!/bin/sh\ncache_size=40G # cst: RUST_SCCACHE_CACHE_SIZE\nworkspace=yes # cst: RUST_SCCACHE_WORKSPACE\n"
        )
        self.assertEqual(
            tcd_config_init.detect_cargo_overrides(home, cargo_toml, wrapper_text),
            {
                "RUST_LEAN_DEBUGINFO": "yes",
                "RUST_INCREMENTAL": "off",
                "RUST_SCCACHE": "yes",
                "RUST_SCCACHE_CACHE_SIZE": "40G",
                "RUST_SCCACHE_WORKSPACE": "yes",
            },
        )

    def test_detect_cargo_overrides_ignores_foreign_and_corrupt(self) -> None:
        home = "/home/tester"
        wrapper_path = f"{home}/.local/libexec/cst/rustc-wrapper"
        foreign_toml = """
[build]
rustc-wrapper = "/usr/bin/sccache"
incremental = false

[profile.dev]
debug = true
"""
        self.assertEqual(tcd_config_init.detect_cargo_overrides(home, foreign_toml, None), {})
        partial_lean = """
[profile.dev]
debug = "line-tables-only"
"""
        self.assertEqual(tcd_config_init.detect_cargo_overrides(home, partial_lean, None), {})
        lean_zero = """
[profile.dev]
debug = "line-tables-only"
split-debuginfo = "unpacked"

[profile.dev.package."*"]
debug = 0
"""
        self.assertEqual(tcd_config_init.detect_cargo_overrides(home, lean_zero, None), {"RUST_LEAN_DEBUGINFO": "yes"})
        bad_wrapper = (
            "cache_size=invalid # cst: RUST_SCCACHE_CACHE_SIZE\nworkspace=maybe # cst: RUST_SCCACHE_WORKSPACE\n"
        )
        self.assertEqual(
            tcd_config_init.detect_cargo_overrides(home, f'[build]\nrustc-wrapper = "{wrapper_path}"\n', bad_wrapper),
            {"RUST_SCCACHE": "yes"},
        )
        self.assertEqual(tcd_config_init.detect_cargo_overrides(home, "[broken", None), {})
        self.assertEqual(tcd_config_init.detect_cargo_overrides(home, None, None), {})


class ConfigInitCliTest(unittest.TestCase):
    """Scratch-home end-to-end tests for `cst toolchains config init`."""

    @override
    def setUp(self) -> None:
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            self.addCleanup(signal.signal, sig, signal.getsignal(sig))
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="cst-config-init-test-"))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.conf_path = os.path.join(self.home, ".config", "cst", "toolchains.conf")
        self.env = {
            "PATH": "/usr/bin:/bin",
            "HOME": self.home,
            "LANG": "C.UTF-8",
            "CST_TOOLCHAINS_HERE": "1",
        }

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        parts = tc_wiring.Parts(journal=False)
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch.dict(os.environ, self.env, clear=True),
        ):
            rc = tc_cli.main([*args, "--home", self.home], "help", TOOL, parts=parts)
        return rc, out.getvalue(), err.getvalue()

    def test_fresh_plan_apply_refuse_and_force_idempotent(self) -> None:
        rc, out, err = self.run_cli("config", "init", "--plan")
        self.assertEqual(rc, 0, err)
        self.assertIn("would write: ~/.config/cst/toolchains.conf (all settings commented out at CST defaults)", out)
        self.assertFalse(os.path.exists(self.conf_path))

        rc, out, err = self.run_cli("config", "init")
        self.assertEqual(rc, 0, err)
        self.assertIn("written:   ~/.config/cst/toolchains.conf (all settings commented out at CST defaults)", out)
        self.assertTrue(os.path.isfile(self.conf_path))
        self.assertEqual(stat.S_IMODE(os.stat(self.conf_path).st_mode), 0o644)

        rc, _, _ = self.run_cli("status")
        self.assertEqual(rc, 0)
        rc, _, _ = self.run_cli("config", "plan")
        self.assertEqual(rc, 0)

        # Second init without --force refuses without overwriting; --plan reports kept:.
        with open(self.conf_path, encoding="utf-8") as fh:
            first_bytes = fh.read()
        rc, out, err = self.run_cli("config", "init")
        self.assertEqual(rc, 1)
        self.assertIn("already exists (pass --force", err)

        rc, out, err = self.run_cli("config", "init", "--plan")
        self.assertEqual(rc, 0, err)
        self.assertIn("kept:      ~/.config/cst/toolchains.conf already exists", out)

        # --force on matching file is a no-op (unchanged:).
        rc, out, err = self.run_cli("config", "init", "--force")
        self.assertEqual(rc, 0, err)
        self.assertIn("unchanged: ~/.config/cst/toolchains.conf", out)
        with open(self.conf_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), first_bytes)

    def test_seeds_active_cargo_and_wrapper_overrides_when_conf_missing(self) -> None:
        # Apply lean debug info, sccache wrapper (no binary needed for config files), and incremental=false.
        cargo_dir = os.path.join(self.home, ".cargo")
        os.makedirs(cargo_dir)
        os.makedirs(os.path.dirname(self.conf_path))
        with open(self.conf_path, "w", encoding="utf-8") as fh:
            fh.write(
                "RUST_SCCACHE=yes\n"
                "RUST_LEAN_DEBUGINFO=yes\n"
                "RUST_INCREMENTAL=off\n"
                "RUST_SCCACHE_CACHE_SIZE=40G\n"
                "RUST_SCCACHE_WORKSPACE=yes\n"
            )
        rc, _, err = self.run_cli("config", "apply")
        self.assertEqual(rc, 0, err)
        os.unlink(self.conf_path)

        rc, out, err = self.run_cli("config", "init", "--plan")
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "5 active override(s): RUST_INCREMENTAL, RUST_LEAN_DEBUGINFO, "
            "RUST_SCCACHE, RUST_SCCACHE_CACHE_SIZE, RUST_SCCACHE_WORKSPACE",
            out,
        )
        self.assertFalse(os.path.exists(self.conf_path))

        rc, out, err = self.run_cli("config", "init")
        self.assertEqual(rc, 0, err)
        with open(self.conf_path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("\nRUST_INCREMENTAL=off\n", text)
        self.assertIn("\nRUST_LEAN_DEBUGINFO=yes\n", text)
        self.assertIn("\nRUST_SCCACHE=yes\n", text)
        self.assertIn("\nRUST_SCCACHE_CACHE_SIZE=40G\n", text)
        self.assertIn("\nRUST_SCCACHE_WORKSPACE=yes\n", text)

        # Re-applying config sees everything as kept/ok with zero drift.
        rc, out, err = self.run_cli("config", "apply")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("written:", out)

    def test_force_preserves_hand_edited_minimal_conf_and_refuses_bad_conf(self) -> None:
        os.makedirs(os.path.dirname(self.conf_path))
        with open(self.conf_path, "w", encoding="utf-8") as fh:
            fh.write("RUST_BUILD_MAX_IDLE=7d\nRUST_SCCACHE=yes\n")

        rc, out, err = self.run_cli("config", "init", "--plan", "--force")
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "would write: ~/.config/cst/toolchains.conf (2 active override(s): RUST_BUILD_MAX_IDLE, RUST_SCCACHE)", out
        )
        with open(self.conf_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "RUST_BUILD_MAX_IDLE=7d\nRUST_SCCACHE=yes\n")

        rc, out, err = self.run_cli("config", "init", "--force")
        self.assertEqual(rc, 0, err)
        with open(self.conf_path, encoding="utf-8") as fh:
            expanded = fh.read()
        self.assertIn("\nRUST_BUILD_MAX_IDLE=7d\n", expanded)
        self.assertIn("\nRUST_SCCACHE=yes\n", expanded)
        self.assertIn("# RUST_BUILD_MAX_TOTAL=150G\n", expanded)

        rc, out, err = self.run_cli("config", "init", "--force")
        self.assertEqual(rc, 0, err)
        self.assertIn("unchanged: ~/.config/cst/toolchains.conf", out)

        # Corrupt existing conf is refused even with --force and left intact.
        bad = "UNKNOWN_OPTION=1\n"
        with open(self.conf_path, "w", encoding="utf-8") as fh:
            fh.write(bad)
        for flags in (["config", "init", "--force"], ["config", "init", "--plan", "--force"]):
            rc, _, err = self.run_cli(*flags)
            self.assertEqual(rc, 1)
            self.assertIn("unknown setting 'UNKNOWN_OPTION'", err)
        with open(self.conf_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), bad)

    def test_refuses_symlink_directory_fifo_and_unreadable_conf(self) -> None:
        os.makedirs(os.path.dirname(self.conf_path))
        real_target = os.path.join(self.tmp, "real-target.conf")
        with open(real_target, "w", encoding="utf-8") as fh:
            fh.write("RUST_SCCACHE=yes\n")
        for target in ("/tmp/nonexistent-target", real_target):
            os.symlink(target, self.conf_path)
            for flags in (["config", "init"], ["config", "init", "--force"], ["config", "init", "--plan", "--force"]):
                rc, _, err = self.run_cli(*flags)
                self.assertEqual(rc, 1)
                self.assertIn("symbolic link", err)
            self.assertTrue(os.path.islink(self.conf_path))
            os.unlink(self.conf_path)

        os.makedirs(self.conf_path)
        rc, _, err = self.run_cli("config", "init", "--force")
        self.assertEqual(rc, 1)
        self.assertIn("not a regular file", err)
        os.rmdir(self.conf_path)

        os.mkfifo(self.conf_path)
        rc, _, err = self.run_cli("config", "init", "--force")
        self.assertEqual(rc, 1)
        self.assertIn("not a regular file", err)
        os.unlink(self.conf_path)

        with open(self.conf_path, "wb") as fh:
            fh.write(b"\xff\xfeinvalid")
        rc, _, err = self.run_cli("config", "init", "--force")
        self.assertEqual(rc, 1)
        self.assertIn("not valid UTF-8", err)
        os.unlink(self.conf_path)

        if os.geteuid() != 0:
            with open(self.conf_path, "w", encoding="utf-8") as fh:
                fh.write("RUST_SCCACHE=yes\n")
            os.chmod(self.conf_path, 0o000)
            self.addCleanup(lambda p=self.conf_path: os.path.exists(p) and os.chmod(p, 0o600))
            rc, _, err = self.run_cli("config", "init", "--force")
            self.assertEqual(rc, 1)
            self.assertIn("cannot read", err)
            os.chmod(self.conf_path, 0o600)
            os.unlink(self.conf_path)

    def test_fifo_cargo_or_wrapper_and_write_failure_and_missing_defaults(self) -> None:
        # FIFO at ~/.cargo/config.toml and ~/.local/libexec/cst/rustc-wrapper must not block.
        cargo_dir = os.path.join(self.home, ".cargo")
        wrapper_dir = os.path.join(self.home, ".local", "libexec", "cst")
        os.makedirs(cargo_dir)
        os.makedirs(wrapper_dir)
        os.mkfifo(os.path.join(cargo_dir, "config.toml"))
        os.mkfifo(os.path.join(wrapper_dir, "rustc-wrapper"))
        rc, out, err = self.run_cli("config", "init", "--plan")
        self.assertEqual(rc, 0, err)
        self.assertIn("all settings commented out at CST defaults", out)

        # Parent ~/.config/cst as a regular file blocks lstat with ENOTDIR.
        cfg_parent = os.path.dirname(self.conf_path)
        os.makedirs(os.path.dirname(cfg_parent))
        with open(cfg_parent, "w", encoding="utf-8") as fh:
            fh.write("blocker\n")
        rc, _, err = self.run_cli("config", "init")
        self.assertEqual(rc, 1)
        self.assertIn("Not a directory", err)
        os.unlink(cfg_parent)

        # Read-only ~/.config/cst dir reaches write_user_conf and triggers WriteFailed.
        if os.geteuid() != 0:
            os.makedirs(cfg_parent)
            os.chmod(cfg_parent, 0o555)
            self.addCleanup(lambda p=cfg_parent: os.path.isdir(p) and os.chmod(p, 0o755))
            rc, _, err = self.run_cli("config", "init")
            self.assertEqual(rc, 1)
            self.assertIn("cannot write", err)
            os.chmod(cfg_parent, 0o755)
            os.rmdir(cfg_parent)

        # Missing or corrupt defaults toolchains.conf beside the installed zipapp fails clearly.
        inst = os.path.join(self.tmp, "inst-bad")
        os.makedirs(inst)
        app = os.path.join(inst, "cst-toolchains")
        with open(app, "wb") as fh:
            fh.write(build_python_zipapp(REPO, "toolchains/cst-toolchains"))
        os.chmod(app, 0o755)

        r_miss = subprocess.run(
            [sys.executable, "-I", "-B", app, "config", "init", "--home", self.home],
            cwd="/",
            env=self.env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(r_miss.returncode, 1)
        self.assertIn("is missing", r_miss.stderr)

        with open(os.path.join(inst, "toolchains.conf"), "w", encoding="utf-8") as fh:
            fh.write("UNKNOWN=1\n")
        r_bad = subprocess.run(
            [sys.executable, "-I", "-B", app, "config", "init", "--home", self.home],
            cwd="/",
            env=self.env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(r_bad.returncode, 1)
        self.assertIn("unknown setting", r_bad.stderr)

    def test_shell_shim_and_installed_zipapp(self) -> None:
        # 1. Through toolchains-setup.sh shell shim (--home isolates away from real passwd home)
        r = subprocess.run(
            ["bash", SETUP, "config", "init", "--plan", "--home", self.home],
            env=self.env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("would write: ~/.config/cst/toolchains.conf", r.stdout)

        # 2. Through built zipapp + copied toolchains.conf outside checkout
        inst = os.path.join(self.tmp, "inst")
        os.makedirs(inst)
        app = os.path.join(inst, "cst-toolchains")
        with open(app, "wb") as fh:
            fh.write(build_python_zipapp(REPO, "toolchains/cst-toolchains"))
        os.chmod(app, 0o755)
        shutil.copy(DEFAULTS, os.path.join(inst, "toolchains.conf"))

        r2 = subprocess.run(
            [sys.executable, "-I", "-B", app, "config", "init", "--home", self.home],
            cwd="/",
            env=self.env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertIn("written:   ~/.config/cst/toolchains.conf", r2.stdout)
        self.assertTrue(os.path.isfile(self.conf_path))

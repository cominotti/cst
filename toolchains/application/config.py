# SPDX-License-Identifier: MPL-2.0
"""`config plan|apply`: cargo's build-dir, uv's link-mode, the opt-in lean debug info
and the opt-in sccache wrapper. apply changes the user's settings files in steps that
keep them valid at every moment: a line is appended only when the file parses to the
same settings plus that line, and the wrapper is in place before the line naming it.
"""

from __future__ import annotations

import dataclasses
import json
import os
from typing import TYPE_CHECKING

from cst_core.domain.files import OwnedState
from cst_core.ports import WriteFailed
from toolchains.domain.build import HASH_VAR, ROOT_REL
from toolchains.domain.common import CONF_REL, ConfigError, NAME, RustIncremental, ToolchainsAction, tilde
from toolchains.domain.config import (
    CANNOT_READ,
    CARGO_CFG_REL,
    CARGO_HAS_BUILD,
    CARGO_HAS_BUILD_DIR,
    ConfigLine,
    ConfigMark,
    ConfigTool,
    ConfigView,
    IncrementalState,
    LEAN_REBUILD,
    LeanState,
    SCCACHE_REBUILD,
    SccacheInstallMethod,
    SccacheState,
    ToolSettingState,
    UV_CFG_REL,
    UV_HAS_LINK_MODE,
    UV_HAS_TABLE,
    UV_TEXT,
    WRAPPER_NAME,
    WRAPPER_REL,
    WrapperValues,
    cargo_text,
    incremental_state,
    lean_state,
    sccache_state,
    wrapper_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from toolchains.domain.prune import Settings
    from toolchains.ports import HomeFilesPort, RunOutputPort, SccacheInstallerPort


def _flag_wanted(settings: Settings | None, flag: bool | None, option: str, name: str) -> tuple[bool, str]:
    """(wanted, why) of a yes/no setting: the --NAME / --no-NAME option when given, else OPTION."""
    if flag is not None:
        return flag, (f"--{name}" if flag else f"--no-{name}")
    if settings is None:
        return False, "bad settings"
    val = bool(settings.options[option])
    return val, f"{option}=" + ("yes" if val else "no")


def lean_wanted(settings: Settings | None, lean: bool | None) -> tuple[bool, str]:
    return _flag_wanted(settings, lean, "RUST_LEAN_DEBUGINFO", "lean-debuginfo")


def sccache_wanted(settings: Settings | None, flag: bool | None) -> tuple[bool, str]:
    return _flag_wanted(settings, flag, "RUST_SCCACHE", "sccache")


def incremental_wanted(settings: Settings | None) -> tuple[bool, str]:
    """(disable_incremental, why) from RUST_INCREMENTAL=auto|off."""
    if settings is None:
        return False, "bad settings"
    off = settings.options["RUST_INCREMENTAL"] == RustIncremental.OFF
    return off, "RUST_INCREMENTAL=off" if off else "RUST_INCREMENTAL=auto"


def wrapper_values(settings: Settings | None) -> WrapperValues:
    """No settings (a bad settings file; only --sccache/--no-sccache counts then): the defaults."""
    if not settings:
        return WrapperValues()
    return WrapperValues(
        str(settings.options["RUST_SCCACHE_CACHE_SIZE"]), bool(settings.options["RUST_SCCACHE_WORKSPACE"])
    )


def _foreign_wrapper_text(st: SccacheState) -> str:
    """What the sccache lines say of a rustc-wrapper in cargo's config (ST) that is not CST's."""
    return f"rustc-wrapper = {json.dumps(st['value'])} is yours: left as it is"


@dataclasses.dataclass(frozen=True)
class ToolConfig:
    """config plan and apply in HOME (PROG: how hints name the program); SETTINGS(): the
    checked settings (ConfigError otherwise), whose opt-ins the flags override."""

    home: str
    prog: str
    files: HomeFilesPort
    out: RunOutputPort
    settings: Callable[[], Settings]
    sccache_installer: SccacheInstallerPort

    def items(self) -> list[tuple[ConfigTool, str, ToolSettingState, str]]:
        """[(name, path, state, text to append)]; state: set | by-hand | write |
        unreadable."""
        home = self.home
        out: list[tuple[ConfigTool, str, ToolSettingState, str]] = []
        # (name, file, "our setting is there", "the table is there, set by hand", what apply appends)
        for name, rel, ours, by_hand, append in (
            (ConfigTool.CARGO, CARGO_CFG_REL, CARGO_HAS_BUILD_DIR, CARGO_HAS_BUILD, cargo_text(home)),
            (ConfigTool.UV, UV_CFG_REL, UV_HAS_LINK_MODE, UV_HAS_TABLE, UV_TEXT),
        ):
            path = os.path.join(home, rel)
            text = self.files.read_settings(path)
            if text is None:
                st = ToolSettingState.UNREADABLE
            elif ours.search(text):
                st = ToolSettingState.SET
            elif by_hand.search(text):
                st = ToolSettingState.BY_HAND
            else:
                st = ToolSettingState.WRITE
            out.append((name, path, st, append))
        return out

    def lean_state(self) -> LeanState:
        """See toolchains.domain.config.lean_state (cargo's config in the home)."""
        path = os.path.join(self.home, CARGO_CFG_REL)
        return lean_state(path, self.files.read_settings(path))

    def lean_lines(self, verb: ConfigView, settings: Settings | None, lean: bool | None) -> list[ConfigLine]:
        home = self.home
        st = self.lean_state()
        shown = tilde(st["path"], home)
        want, why = lean_wanted(settings, lean)
        if st["error"]:
            return [(ConfigMark.UNKNOWN, shown, f"lean debug info: the file {st['error']}")]
        if not want:
            if not st["add"] and not st["conflicts"]:
                return [
                    (
                        ConfigMark.ON,
                        shown,
                        f"lean debug info is set ({why} does not remove it: delete the lines by hand)",
                    )
                ]
            return [
                (
                    ConfigMark.OFF,
                    shown,
                    "lean debug info: off (opt-in; RUST_LEAN_DEBUGINFO=yes in ~/"
                    + CONF_REL
                    + f", or config apply --lean-debuginfo); {LEAN_REBUILD}",
                )
            ]
        out: list[ConfigLine] = [(ConfigMark.KEEP, shown, c) for c in st["conflicts"]]
        if not st["add"]:
            if not st["conflicts"]:
                out.append((ConfigMark.OK, shown, f"lean debug info set ({why})"))
            return out
        what = "; ".join(item for _, item in st["add"])
        if st["rewrite"]:
            out.append(
                (
                    ConfigMark.WRITE,
                    shown,
                    f"lean debug info ({why}): {what}; {LEAN_REBUILD}"
                    + ("" if verb == ConfigView.PLAN else f" (missing: {self.prog} config apply)"),
                )
            )
        else:
            out.append(
                (
                    ConfigMark.BY_HAND,
                    shown,
                    f"lean debug info ({why}): the file already has these tables: add yourself: {what}",
                )
            )
        return out

    def lean_apply(self, settings: Settings | None, lean: bool | None) -> int:
        want, why = lean_wanted(settings, lean)
        if not want:
            return 0
        st = self.lean_state()
        path = st["path"]
        if st["error"]:
            self.out.say(f"{NAME}: lean debug info: {path} {st['error']}: left as it is", 3, err=True)
            return 1
        for c in st["conflicts"]:
            self.out.say(f"NOTE: lean debug info: {path}: {c}", 5, err=True)
        if not st["add"]:
            if not st["conflicts"]:
                self.out.say(f"kept:      {path} (lean debug info already set)")
            return 0
        if not st["rewrite"]:
            self.out.say(f"NOTE: lean debug info: {path} already has these tables: add by hand:", 5, err=True)
            for _, item in st["add"]:
                self.out.say(f"      {item}", 5, err=True)
            return 0
        if self._rewrite_failed(path, st["rewrite"]):
            return 1
        self.out.say(
            f"written:   {path} (lean debug info, {why}; the next dev build of each workspace compiles everything once)"
        )
        return 0

    def wrapper_lines(self, verb: ConfigView, text: str | None, values: WrapperValues) -> list[ConfigLine]:
        home = self.home
        shown = tilde(wrapper_path(home), home)
        if text is None:
            return [
                (
                    ConfigMark.UNKNOWN,
                    shown,
                    (
                        f"sccache wrapper: its source ({WRAPPER_NAME} next to this program) is missing: "
                        f"run {self.prog} config apply from CST"
                    ),
                )
            ]
        st = self.files.wrapper_state(text)
        if st == OwnedState.OK:
            return [(ConfigMark.OK, shown, f"sccache wrapper up to date ({values})")]
        if st == OwnedState.CONFLICT:
            return [(ConfigMark.KEEP, shown, "not CST's file: left alone, so CST writes no rustc-wrapper line")]
        return [
            (
                ConfigMark.WRITE,
                shown,
                f"sccache wrapper ({'missing' if st == OwnedState.MISSING else 'outdated'}; {values})"
                + ("" if verb == ConfigView.PLAN else f" (fix: {self.prog} config apply)"),
            )
        ]

    def sccache_state(self) -> SccacheState:
        """See toolchains.domain.config.sccache_state (cargo's config in the home)."""
        path = os.path.join(self.home, CARGO_CFG_REL)
        return sccache_state(path, self.files.read_settings(path), wrapper_path(self.home))

    def sccache_lines(self, verb: ConfigView, settings: Settings | None, flag: bool | None) -> list[ConfigLine]:
        home = self.home
        st = self.sccache_state()
        shown = tilde(st["path"], home)
        want, why = sccache_wanted(settings, flag)
        values = wrapper_values(settings)
        off = f"sccache: off (opt-in; RUST_SCCACHE=yes in ~/{CONF_REL}, or config apply --sccache); {SCCACHE_REBUILD}"
        if st["error"]:
            return [(ConfigMark.UNKNOWN, shown, f"sccache: the file {st['error']}")]
        if flag is False:
            return self._sccache_off_lines(st, shown, why)
        foreign = st["value"] is not None and not st["ours"]
        yours = _foreign_wrapper_text(st)
        text = self.files.wrapper_text(values)
        if not want:
            if st["ours"]:
                out = [
                    (
                        ConfigMark.ON,
                        shown,
                        (
                            f"sccache is on (CST's rustc-wrapper; {why} does not remove it: "
                            f"{self.prog} config apply --no-sccache does)"
                        ),
                    )
                ]
                out += [ln for ln in self.wrapper_lines(verb, text, values) if ln[0] != ConfigMark.OK]
                return out + self._sccache_binary_lines(verb, want=False)
            return [(ConfigMark.OFF, shown, off + (f"; {yours}" if foreign else ""))]
        if foreign:
            return [(ConfigMark.KEEP, shown, f"sccache ({why}): {yours} (CST never overwrites it)")]
        out = self.wrapper_lines(verb, text, values)
        if st["ours"]:
            out.append((ConfigMark.OK, shown, f"sccache: rustc-wrapper set ({why})"))
        elif st["add"]:
            out.append(
                (
                    ConfigMark.WRITE,
                    shown,
                    f'sccache ({why}): [build] rustc-wrapper = "~/{WRAPPER_REL}"; {SCCACHE_REBUILD}'
                    + ("" if verb == ConfigView.PLAN else f" (missing: {self.prog} config apply)"),
                )
            )
        else:
            out.append(
                (
                    ConfigMark.BY_HAND,
                    shown,
                    (
                        f"sccache ({why}): CST cannot add the key safely here: put "
                        f'rustc-wrapper = "{wrapper_path(home)}" into [build] yourself'
                    ),
                )
            )
        return out + self._sccache_binary_lines(verb, want=True)

    def _sccache_binary_lines(self, verb: ConfigView, *, want: bool) -> list[ConfigLine]:
        """Status or plan lines when sccache binary itself is absent while rustc-wrapper applies."""
        if self.sccache_installer.find_sccache() is not None:
            return []
        target = "~/.cargo/bin/sccache"
        if not want:
            return [
                (
                    ConfigMark.UNKNOWN,
                    target,
                    "sccache binary missing (rustc-wrapper runs rustc directly until sccache is installed)",
                )
            ]
        method = self.sccache_installer.install_method()
        fix = "" if verb == ConfigView.PLAN else f" (fix: {self.prog} config apply)"
        if method == SccacheInstallMethod.BINSTALL:
            note = (
                "sccache binary missing: apply tries cargo-binstall --no-confirm sccache "
                "(fallback: cargo install sccache --locked; fail-open)" + fix
            )
            return [(ConfigMark.WRITE, target, note)]
        if method == SccacheInstallMethod.CARGO:
            note = "sccache binary missing: apply tries cargo install sccache --locked (fail-open)" + fix
            return [(ConfigMark.WRITE, target, note)]
        return [
            (
                ConfigMark.UNKNOWN,
                target,
                "sccache binary missing and cargo not found (wrapper runs rustc directly until sccache is installed)",
            )
        ]

    def _ensure_sccache_binary(self) -> None:
        """Install sccache into ~/.cargo/bin if missing (fail-open)."""
        if self.sccache_installer.find_sccache() is not None:
            return
        ok, detail = self.sccache_installer.install()
        if ok:
            self.out.say(f"installed: sccache ({detail})")
        else:
            self.out.say(
                f"NOTE: sccache: could not install sccache ({detail}); "
                "rustc-wrapper will run rustc directly until sccache is installed",
                5,
                err=True,
            )

    def _sccache_off_lines(self, st: SccacheState, shown: str, why: str) -> list[ConfigLine]:
        """The lines of --no-sccache (WHY: what turns it off) for cargo's config SHOWN in state ST."""
        foreign = st["value"] is not None and not st["ours"]
        if foreign:
            return [(ConfigMark.KEEP, shown, f"sccache ({why}): {_foreign_wrapper_text(st)}")]
        if not st["ours"]:
            return [(ConfigMark.OFF, shown, "sccache: off")]
        if st["remove"]:
            return [
                (
                    ConfigMark.REMOVE,
                    shown,
                    (
                        f"sccache: CST's rustc-wrapper line ({why}); {SCCACHE_REBUILD}; "
                        f"~/{WRAPPER_REL} stays (unused, harmless)"
                    ),
                )
            ]
        return [
            (
                ConfigMark.BY_HAND,
                shown,
                f"sccache ({why}): CST cannot remove its rustc-wrapper line safely: delete it yourself",
            )
        ]

    def install_wrapper(self, values: WrapperValues) -> int:
        """0 when the wrapper is in place (written or unchanged), else 1."""
        home = self.home
        text = self.files.wrapper_text(values)
        path = wrapper_path(home)
        if text is None:
            self.out.say(
                f"{NAME}: sccache: {WRAPPER_NAME} is missing next to this program: run {self.prog} config apply from CST",
                3,
                err=True,
            )
            return 1
        if self.files.wrapper_state(text) == OwnedState.CONFLICT:
            self.out.say(f"{NAME}: sccache: {path} is not CST's file: left alone", 3, err=True)
            return 1
        try:
            written = self.files.put_wrapper(text, values.record())
        except WriteFailed as e:
            self.out.say(f"{NAME}: {e}", 3, err=True)
            return 1
        self.out.say(
            f"written:   {path} (sccache wrapper, {values})" if written else f"kept:      {path} (sccache wrapper)"
        )
        return 0

    def _rewrite_failed(self, path: str, text: str) -> bool:
        """Rewrite the settings file PATH with TEXT; True, after its message, when that failed. The
        message says whether the new text is in place (only the sync of its folder failed) or the file
        is as it was."""
        done = self.files.rewrite(path, text)
        if not done.error:
            return False
        if done.replaced:
            self.out.say(
                f"{NAME}: {path}: written, but its folder could not be synced ({done.error}): "
                "the change may not survive a power loss",
                3,
                err=True,
            )
        else:
            self.out.say(f"{NAME}: cannot write {path}: {done.error}: left as it is", 3, err=True)
        return True

    def sccache_apply(self, settings: Settings | None, flag: bool | None) -> int:
        home = self.home
        want, why = sccache_wanted(settings, flag)
        st = self.sccache_state()
        path = st["path"]
        if st["error"]:
            if want or flag is False or st["ours"]:
                self.out.say(f"{NAME}: sccache: {path} {st['error']}: left as it is", 3, err=True)
                return 1
            return 0
        if flag is False:
            if not st["ours"]:
                return 0
            if not st["remove"]:
                self.out.say(
                    f"NOTE: sccache: {path}: CST cannot remove its rustc-wrapper line safely: delete it yourself",
                    5,
                    err=True,
                )
                return 0
            if self._rewrite_failed(path, st["remove"]):
                return 1
            self.out.say(f"removed:   {path} (CST's rustc-wrapper line, {why}; ~/{WRAPPER_REL} stays, unused)")
            return 0
        if not want:
            # The line is there (set earlier): keep its wrapper current, so cargo
            # never names a missing program.
            return self.install_wrapper(wrapper_values(settings)) if st["ours"] else 0
        if st["value"] is not None and not st["ours"]:
            self.out.say(
                f"NOTE: sccache: {path}: rustc-wrapper = {json.dumps(st['value'])} is yours: left as it is "
                f"(sccache not set up)",
                5,
                err=True,
            )
            return 0
        # The wrapper first: the line must never name a missing program.
        if self.install_wrapper(wrapper_values(settings)):
            return 1
        if st["ours"]:
            self.out.say(f"kept:      {path} (rustc-wrapper already set)")
            self._ensure_sccache_binary()
            return 0
        if not st["add"]:
            self.out.say(
                f"NOTE: sccache: {path}: CST cannot add the key safely: put this into [build] by hand:", 5, err=True
            )
            self.out.say(f"      rustc-wrapper = {json.dumps(wrapper_path(home))}", 5, err=True)
            self._ensure_sccache_binary()
            return 0
        if self._rewrite_failed(path, st["add"]):
            return 1
        self.out.say(f"written:   {path} (rustc-wrapper, {why}: rustc goes through sccache where it is on PATH)")
        self._ensure_sccache_binary()
        return 0

    def incremental_state(self) -> IncrementalState:
        """See toolchains.domain.config.incremental_state (cargo's config in the home)."""
        path = os.path.join(self.home, CARGO_CFG_REL)
        return incremental_state(path, self.files.read_settings(path))

    def incremental_lines(self, verb: ConfigView, settings: Settings | None) -> list[ConfigLine]:
        """Plan or status line for RUST_INCREMENTAL=auto|off."""
        st = self.incremental_state()
        shown = tilde(st["path"], self.home)
        want, why = incremental_wanted(settings)
        if st["error"]:
            return [(ConfigMark.UNKNOWN, shown, f"incremental builds: the file {st['error']}")]
        if not want:
            return self._incremental_auto_lines(st, shown, why)
        if st["value"] is False:
            return [(ConfigMark.OK, shown, f"incremental builds: off ({why})")]
        if st["value"] is not None:
            return [
                (
                    ConfigMark.KEEP,
                    shown,
                    f"incremental builds ({why}): [build] incremental = {json.dumps(st['value'])} is yours: left as it is",
                )
            ]
        if st["add"]:
            fix = "" if verb == ConfigView.PLAN else f" (missing: {self.prog} config apply)"
            return [(ConfigMark.WRITE, shown, f"incremental builds ({why}): [build] incremental = false{fix}")]
        return [
            (
                ConfigMark.BY_HAND,
                shown,
                f"incremental builds ({why}): CST cannot add the key safely here: put incremental = false into [build] yourself",
            )
        ]

    @staticmethod
    def _incremental_auto_lines(st: IncrementalState, shown: str, why: str) -> list[ConfigLine]:
        if st["ours"]:
            if st["remove"]:
                return [
                    (ConfigMark.REMOVE, shown, f"incremental builds: CST's [build] incremental = false line ({why})")
                ]
            return [
                (
                    ConfigMark.BY_HAND,
                    shown,
                    f"incremental builds ({why}): CST cannot remove its incremental = false line safely: delete it yourself",
                )
            ]
        if st["value"] is not None:
            return [
                (
                    ConfigMark.KEEP,
                    shown,
                    f"incremental builds ({why}): [build] incremental = {json.dumps(st['value'])} is yours: left as it is",
                )
            ]
        return [
            (
                ConfigMark.OFF,
                shown,
                f"incremental builds: cargo default ({why}; set RUST_INCREMENTAL=off to disable globally, or CARGO_INCREMENTAL=0 in agent/CI shells)",
            )
        ]

    def incremental_apply(self, settings: Settings | None) -> int:
        want, why = incremental_wanted(settings)
        st = self.incremental_state()
        path = st["path"]
        if st["error"]:
            if want or st["ours"]:
                self.out.say(f"{NAME}: incremental builds: {path} {st['error']}: left as it is", 3, err=True)
                return 1
            return 0
        if not want:
            return self._incremental_remove(st, path, why)
        return self._incremental_write(st, path, why)

    def _incremental_remove(self, st: IncrementalState, path: str, why: str) -> int:
        if not st["ours"]:
            return 0
        if not st["remove"]:
            self.out.say(
                f"NOTE: incremental builds: {path}: CST cannot remove its incremental = false line safely: delete it yourself",
                5,
                err=True,
            )
            return 0
        if self._rewrite_failed(path, st["remove"]):
            return 1
        self.out.say(f"removed:   {path} (CST's [build] incremental = false line, {why})")
        return 0

    def _incremental_write(self, st: IncrementalState, path: str, why: str) -> int:
        if st["value"] is False:
            self.out.say(f"kept:      {path} (incremental = false already set)")
            return 0
        if st["value"] is not None:
            self.out.say(
                f"NOTE: incremental builds: {path}: incremental = {json.dumps(st['value'])} is yours: left as it is",
                5,
                err=True,
            )
            return 0
        if not st["add"]:
            self.out.say(
                f"NOTE: incremental builds: {path}: CST cannot add the key safely: put incremental = false into [build] by hand",
                5,
                err=True,
            )
            return 0
        if self._rewrite_failed(path, st["add"]):
            return 1
        self.out.say(f"written:   {path} (incremental = false, {why})")
        return 0

    def lines(
        self,
        verb: ConfigView,
        settings: Settings | None = None,
        lean: bool | None = None,
        sccache: bool | None = None,
    ) -> list[ConfigLine]:
        """The plan or status lines of the settings (lean, sccache: the explicit
        flags)."""
        home = self.home
        out: list[ConfigLine] = []
        for name, path, st, _ in self.items():
            shown = tilde(path, home)
            out.append(
                self._cargo_line(st, shown, verb) if name == ConfigTool.CARGO else self._uv_line(st, shown, verb)
            )
        out += self.lean_lines(verb, settings, lean)
        out += self.incremental_lines(verb, settings)
        out += self.sccache_lines(verb, settings, sccache)
        return out

    def _cargo_line(self, st: ToolSettingState, shown: str, verb: ConfigView) -> ConfigLine:
        """The line of cargo's build-dir in its config SHOWN, in state ST."""
        match st:
            case ToolSettingState.SET:
                if self.files.build_dir_is_csts():
                    return (ConfigMark.OK, shown, "build-dir = CST's line (rust:build prunes that folder)")
                return (
                    ConfigMark.KEEP,
                    shown,
                    "build-dir set to your own value: left as it is (rust:build then prunes nothing)",
                )
            case ToolSettingState.BY_HAND:
                return (
                    ConfigMark.BY_HAND,
                    shown,
                    f'has a [build] table: add build-dir = "{self.home}/{ROOT_REL}/{HASH_VAR}" there yourself',
                )
            case ToolSettingState.WRITE:
                return (
                    ConfigMark.WRITE,
                    shown,
                    f'[build] build-dir = "~/{ROOT_REL}/{HASH_VAR}"'
                    + ("" if verb == ConfigView.PLAN else f" (missing: {self.prog} config apply)"),
                )
            case _:
                return (ConfigMark.UNKNOWN, shown, CANNOT_READ)

    def _uv_line(self, st: ToolSettingState, shown: str, verb: ConfigView) -> ConfigLine:
        """The line of uv's link-mode in its config SHOWN, in state ST."""
        match st:
            case ToolSettingState.SET:
                return (ConfigMark.OK, shown, "link-mode already set")
            case ToolSettingState.BY_HAND:
                return (ConfigMark.BY_HAND, shown, 'has tables: add link-mode = "clone" at its top yourself')
            case ToolSettingState.WRITE:
                return (
                    ConfigMark.WRITE,
                    shown,
                    'link-mode = "clone" (hardlinks cannot cross subvolumes)'
                    + ("" if verb == ConfigView.PLAN else f" (missing: {self.prog} config apply)"),
                )
            case _:
                return (ConfigMark.UNKNOWN, shown, CANNOT_READ)

    def run(self, action: ToolchainsAction, lean: bool | None = None, sccache: bool | None = None) -> int:
        home = self.home
        settings: Settings | None
        try:
            settings = self.settings()
        except ConfigError as e:
            settings = None
            off = [
                w
                for w, flag in (("lean debug info", lean), ("incremental", None), ("sccache", sccache))
                if flag is None
            ]
            if off:
                self.out.say(f"note: BAD SETTINGS ({e}): {', '.join(off)} treated as off", 4, err=True)
        if action == ToolchainsAction.PLAN:
            lines = self.lines(ConfigView.PLAN, settings, lean, sccache)
            for st, shown, note in lines:
                self.out.say(f"  {st:<8} {shown:<24} {note}")
            if any(st in (ConfigMark.WRITE, ConfigMark.REMOVE) for st, _, _ in lines):
                flags = {True: " --lean-debuginfo", False: " --no-lean-debuginfo"}.get(lean, "")
                flags += {True: " --sccache", False: " --no-sccache"}.get(sccache, "")
                self.out.say(f"Apply with: {self.prog} config apply" + flags)
            return 0
        rc = 0
        for name, path, st, text in self.items():
            match name, st:
                case _, ToolSettingState.UNREADABLE:
                    self.out.say(f"{NAME}: cannot read {path}: left as it is", 3, err=True)
                    rc = 1
                case ConfigTool.CARGO, ToolSettingState.SET:
                    self.out.say(f"kept:      {path} (build-dir already set)")
                case ConfigTool.CARGO, ToolSettingState.BY_HAND:
                    self.out.say(f"NOTE: {path} already configures [build]: add there by hand:", 5, err=True)
                    self.out.say(f'      build-dir = "{home}/{ROOT_REL}/{HASH_VAR}"', 5, err=True)
                case ConfigTool.UV, ToolSettingState.SET:
                    self.out.say(f"kept:      {path} (link-mode already set)")
                case ConfigTool.UV, ToolSettingState.BY_HAND:
                    self.out.say(f'NOTE: {path} has tables: add link-mode = "clone" at its top by hand', 5, err=True)
                case _:
                    err = self.files.append(path, text)
                    if err:
                        # For example a ~/.cargo owned by root after a `sudo cargo`:
                        # report it and go on with the other setting.
                        self.out.say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
                        rc = 1
                        continue
                    self.out.say(f"written:   {path}")
        rc = max(rc, self.lean_apply(settings, lean))
        rc = max(rc, self.incremental_apply(settings))
        return max(rc, self.sccache_apply(settings, sccache))

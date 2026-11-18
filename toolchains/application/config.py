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

from cst_core.ports import WriteFailed
from toolchains.domain.build import HASH_VAR, ROOT_REL
from toolchains.domain.common import CONF_REL, ConfigError, NAME, tilde
from toolchains.domain.config import (
    BY_HAND,
    CANNOT_READ,
    CARGO_CFG_REL,
    CARGO_HAS_BUILD,
    CARGO_HAS_BUILD_DIR,
    LEAN_REBUILD,
    LeanState,
    SCCACHE_REBUILD,
    SccacheState,
    UV_CFG_REL,
    UV_HAS_LINK_MODE,
    UV_HAS_TABLE,
    UV_TEXT,
    WRAPPER_NAME,
    WRAPPER_REL,
    WrapperValues,
    cargo_text,
    lean_state,
    sccache_state,
    wrapper_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from toolchains.domain.prune import Settings
    from toolchains.ports import HomeFilesPort, RunOutputPort


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


def wrapper_values(settings: Settings | None) -> WrapperValues:
    """No settings (a bad settings file; only --sccache/--no-sccache counts then): the defaults."""
    if not settings:
        return WrapperValues()
    return WrapperValues(
        str(settings.options["RUST_SCCACHE_CACHE_SIZE"]), bool(settings.options["RUST_SCCACHE_WORKSPACE"])
    )


@dataclasses.dataclass(frozen=True)
class ToolConfig:
    """config plan and apply in HOME (PROG: how hints name the program); SETTINGS(): the
    checked settings (ConfigError otherwise), whose opt-ins the flags override."""

    home: str
    prog: str
    files: HomeFilesPort
    out: RunOutputPort
    settings: Callable[[], Settings]

    def items(self) -> list[tuple[str, str, str, str]]:
        """[(name, path, state, text to append)]; state: set | by-hand | write |
        unreadable."""
        home = self.home
        out: list[tuple[str, str, str, str]] = []
        # (name, file, "our setting is there", "the table is there, set by hand", what apply appends)
        for name, rel, ours, by_hand, append in (
            ("cargo", CARGO_CFG_REL, CARGO_HAS_BUILD_DIR, CARGO_HAS_BUILD, cargo_text(home)),
            ("uv", UV_CFG_REL, UV_HAS_LINK_MODE, UV_HAS_TABLE, UV_TEXT),
        ):
            path = os.path.join(home, rel)
            text = self.files.read_settings(path)
            if text is None:
                st = "unreadable"
            elif ours.search(text):
                st = "set"
            elif by_hand.search(text):
                st = "by-hand"
            else:
                st = "write"
            out.append((name, path, st, append))
        return out

    def lean_state(self) -> LeanState:
        """See toolchains.domain.config.lean_state (cargo's config in the home)."""
        path = os.path.join(self.home, CARGO_CFG_REL)
        return lean_state(path, self.files.read_settings(path))

    def lean_lines(self, verb: str, settings: Settings | None, lean: bool | None) -> list[tuple[str, str, str]]:
        home = self.home
        st = self.lean_state()
        shown = tilde(st["path"], home)
        want, why = lean_wanted(settings, lean)
        if st["error"]:
            return [("?", shown, f"lean debug info: the file {st['error']}")]
        if not want:
            if not st["add"] and not st["conflicts"]:
                return [("on", shown, f"lean debug info is set ({why} does not remove it: delete the lines by hand)")]
            return [
                (
                    "off",
                    shown,
                    "lean debug info: off (opt-in; RUST_LEAN_DEBUGINFO=yes in ~/"
                    + CONF_REL
                    + f", or config apply --lean-debuginfo); {LEAN_REBUILD}",
                )
            ]
        out: list[tuple[str, str, str]] = [("keep", shown, c) for c in st["conflicts"]]
        if not st["add"]:
            if not st["conflicts"]:
                out.append(("ok", shown, f"lean debug info set ({why})"))
            return out
        what = "; ".join(b.replace("\n", " ").strip() for _, b in st["add"])
        if st["append"]:
            out.append(
                (
                    "write",
                    shown,
                    f"lean debug info ({why}): {what}; {LEAN_REBUILD}"
                    + ("" if verb == "plan" else f" (missing: {self.prog} config apply)"),
                )
            )
        else:
            out.append(
                (BY_HAND, shown, f"lean debug info ({why}): the file already has these tables: add yourself: {what}")
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
        if not st["append"]:
            self.out.say(f"NOTE: lean debug info: {path} already has these tables: add by hand:", 5, err=True)
            for _, block in st["add"]:
                for line in block.splitlines():
                    self.out.say(f"      {line}", 5, err=True)
            return 0
        err = self.files.append(path, st["append"])
        if err:
            self.out.say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
            return 1
        self.out.say(
            f"written:   {path} (lean debug info, {why}; the next dev build of each workspace compiles everything once)"
        )
        return 0

    def wrapper_lines(self, verb: str, text: str | None, values: WrapperValues) -> list[tuple[str, str, str]]:
        home = self.home
        shown = tilde(wrapper_path(home), home)
        if text is None:
            return [
                (
                    "?",
                    shown,
                    f"sccache wrapper: its source ({WRAPPER_NAME} next to this program) is missing: "
                    f"run {self.prog} config apply from CST",
                )
            ]
        st = self.files.wrapper_state(text)
        if st == "ok":
            return [("ok", shown, f"sccache wrapper up to date ({values})")]
        if st == "conflict":
            return [("keep", shown, "not CST's file: left alone, so CST writes no rustc-wrapper line")]
        return [
            (
                "write",
                shown,
                f"sccache wrapper ({'missing' if st == 'missing' else 'outdated'}; {values})"
                + ("" if verb == "plan" else f" (fix: {self.prog} config apply)"),
            )
        ]

    def sccache_state(self) -> SccacheState:
        """See toolchains.domain.config.sccache_state (cargo's config in the home)."""
        path = os.path.join(self.home, CARGO_CFG_REL)
        return sccache_state(path, self.files.read_settings(path), wrapper_path(self.home))

    def sccache_lines(self, verb: str, settings: Settings | None, flag: bool | None) -> list[tuple[str, str, str]]:
        home = self.home
        st = self.sccache_state()
        shown = tilde(st["path"], home)
        want, why = sccache_wanted(settings, flag)
        values = wrapper_values(settings)
        off = f"sccache: off (opt-in; RUST_SCCACHE=yes in ~/{CONF_REL}, or config apply --sccache); {SCCACHE_REBUILD}"
        if st["error"]:
            return [("?", shown, f"sccache: the file {st['error']}")]
        foreign = st["value"] is not None and not st["ours"]
        yours = f"rustc-wrapper = {json.dumps(st['value'])} is yours: left as it is"
        if flag is False:
            if st["ours"]:
                if st["remove"]:
                    return [
                        (
                            "remove",
                            shown,
                            f"sccache: CST's rustc-wrapper line ({why}); {SCCACHE_REBUILD}; "
                            f"~/{WRAPPER_REL} stays (unused, harmless)",
                        )
                    ]
                return [
                    (
                        BY_HAND,
                        shown,
                        f"sccache ({why}): CST cannot remove its rustc-wrapper line safely: delete it yourself",
                    )
                ]
            return [("keep", shown, f"sccache ({why}): {yours}")] if foreign else [("off", shown, "sccache: off")]
        text = self.files.wrapper_text(values)
        if not want:
            if st["ours"]:
                out = [
                    (
                        "on",
                        shown,
                        f"sccache is on (CST's rustc-wrapper; {why} does not remove it: "
                        f"{self.prog} config apply --no-sccache does)",
                    )
                ]
                return out + [ln for ln in self.wrapper_lines(verb, text, values) if ln[0] != "ok"]
            return [("off", shown, off + (f"; {yours}" if foreign else ""))]
        if foreign:
            return [("keep", shown, f"sccache ({why}): {yours} (CST never overwrites it)")]
        out = self.wrapper_lines(verb, text, values)
        if st["ours"]:
            out.append(("ok", shown, f"sccache: rustc-wrapper set ({why})"))
        elif st["add"]:
            out.append(
                (
                    "write",
                    shown,
                    f'sccache ({why}): [build] rustc-wrapper = "~/{WRAPPER_REL}"; {SCCACHE_REBUILD}'
                    + ("" if verb == "plan" else f" (missing: {self.prog} config apply)"),
                )
            )
        else:
            out.append(
                (
                    BY_HAND,
                    shown,
                    f"sccache ({why}): CST cannot add the key safely here: put "
                    f'rustc-wrapper = "{wrapper_path(home)}" into [build] yourself',
                )
            )
        return out

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
        if self.files.wrapper_state(text) == "conflict":
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
            err = self.files.rewrite(path, st["remove"])
            if err:
                self.out.say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
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
            return 0
        if not st["add"]:
            self.out.say(
                f"NOTE: sccache: {path}: CST cannot add the key safely: put this into [build] by hand:", 5, err=True
            )
            self.out.say(f"      rustc-wrapper = {json.dumps(wrapper_path(home))}", 5, err=True)
            return 0
        err = self.files.rewrite(path, st["add"])
        if err:
            self.out.say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
            return 1
        self.out.say(f"written:   {path} (rustc-wrapper, {why}: rustc goes through sccache where it is on PATH)")
        return 0

    def lines(
        self,
        verb: str,
        settings: Settings | None = None,
        lean: bool | None = None,
        sccache: bool | None = None,
    ) -> list[tuple[str, str, str]]:
        """The plan or status lines of the settings (lean, sccache: the explicit
        flags)."""
        home = self.home
        out: list[tuple[str, str, str]] = []
        for name, path, st, _ in self.items():
            shown = tilde(path, home)
            if name == "cargo":
                if st == "set":
                    if self.files.build_dir_is_csts():
                        out.append(("ok", shown, "build-dir = CST's line (rust:build prunes that folder)"))
                    else:
                        out.append(
                            (
                                "keep",
                                shown,
                                "build-dir set to your own value: left as it is (rust:build then prunes nothing)",
                            )
                        )
                elif st == "by-hand":
                    out.append(
                        (
                            BY_HAND,
                            shown,
                            f'has a [build] table: add build-dir = "{home}/{ROOT_REL}/{HASH_VAR}" there yourself',
                        )
                    )
                elif st == "write":
                    out.append(
                        (
                            "write",
                            shown,
                            f'[build] build-dir = "~/{ROOT_REL}/{HASH_VAR}"'
                            + ("" if verb == "plan" else f" (missing: {self.prog} config apply)"),
                        )
                    )
                else:
                    out.append(("?", shown, CANNOT_READ))
            else:
                if st == "set":
                    out.append(("ok", shown, "link-mode already set"))
                elif st == "by-hand":
                    out.append((BY_HAND, shown, 'has tables: add link-mode = "clone" at its top yourself'))
                elif st == "write":
                    out.append(
                        (
                            "write",
                            shown,
                            'link-mode = "clone" (hardlinks cannot cross subvolumes)'
                            + ("" if verb == "plan" else f" (missing: {self.prog} config apply)"),
                        )
                    )
                else:
                    out.append(("?", shown, CANNOT_READ))
        out += self.lean_lines(verb, settings, lean)
        out += self.sccache_lines(verb, settings, sccache)
        return out

    def run(self, action: str, lean: bool | None = None, sccache: bool | None = None) -> int:
        home = self.home
        settings: Settings | None
        try:
            settings = self.settings()
        except ConfigError as e:
            settings = None
            off = [w for w, flag in (("lean debug info", lean), ("sccache", sccache)) if flag is None]
            if off:
                self.out.say(f"note: BAD SETTINGS ({e}): {' and '.join(off)} treated as off", 4, err=True)
        if action == "plan":
            lines = self.lines("plan", settings, lean, sccache)
            for st, shown, note in lines:
                self.out.say(f"  {st:<8} {shown:<24} {note}")
            if any(st in ("write", "remove") for st, _, _ in lines):
                flags = {True: " --lean-debuginfo", False: " --no-lean-debuginfo"}.get(lean, "")
                flags += {True: " --sccache", False: " --no-sccache"}.get(sccache, "")
                self.out.say(f"Apply with: {self.prog} config apply" + flags)
            return 0
        rc = 0
        for name, path, st, text in self.items():
            if st == "unreadable":
                self.out.say(f"{NAME}: cannot read {path}: left as it is", 3, err=True)
                rc = 1
            elif name == "cargo" and st == "set":
                self.out.say(f"kept:      {path} (build-dir already set)")
            elif name == "cargo" and st == "by-hand":
                self.out.say(f"NOTE: {path} already configures [build]: add there by hand:", 5, err=True)
                self.out.say(f'      build-dir = "{home}/{ROOT_REL}/{HASH_VAR}"', 5, err=True)
            elif name == "uv" and st == "set":
                self.out.say(f"kept:      {path} (link-mode already set)")
            elif name == "uv" and st == "by-hand":
                self.out.say(f'NOTE: {path} has tables: add link-mode = "clone" at its top by hand', 5, err=True)
            else:
                err = self.files.append(path, text)
                if err:
                    # For example a ~/.cargo owned by root after a `sudo cargo`:
                    # report it and go on with the other setting.
                    self.out.say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
                    rc = 1
                    continue
                self.out.say(f"written:   {path}")
        rc = max(rc, self.lean_apply(settings, lean))
        return max(rc, self.sccache_apply(settings, sccache))

# SPDX-License-Identifier: MPL-2.0
"""`config plan|apply`: cargo's build-dir, uv's link-mode, the opt-in lean
debug info and the opt-in sccache wrapper.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tomllib

from cst_core.adapters.files import read_text
from cst_core.ports import WriteFailed
from toolchains.domain.build import HASH_VAR, ROOT_REL, expected_template
from toolchains.domain.common import CONF_REL, ConfigError, NAME, parses_as, plain, tilde
from toolchains.domain.config import (
    ANY_HEADER,
    BUILD_HEADER,
    CARGO_CFG_REL,
    CARGO_HAS_BUILD,
    CARGO_HAS_BUILD_DIR,
    KEY_LINE,
    LEAN_HEAD,
    LEAN_KEYS,
    LEAN_REBUILD,
    LeanState,
    SCCACHE_HEAD,
    SCCACHE_REBUILD,
    SCCACHE_SIZE_LINE,
    SCCACHE_WORKSPACE_LINE,
    SccacheState,
    UV_CFG_REL,
    UV_HAS_LINK_MODE,
    UV_HAS_TABLE,
    UV_TEXT,
    WRAPPER_NAME,
    WRAPPER_REL,
    WS,
    WrapperValues,
    cargo_text,
    dig,
    same_debug,
)
from toolchains.adapters.home_files import append_text, file_state, put_wrapper, read_cfg, wrapper_path, write_cfg
from toolchains.adapters.program import PROG, Program, say
from toolchains.cli.prune import Settings


def config_items(home: str) -> list[tuple[str, str, str, str]]:
    """[(name, path, state, text to append)]; state: set | by-hand | write |
    unreadable."""
    out: list[tuple[str, str, str, str]] = []
    # (name, file, "our setting is there", "the table is there, set by hand", what apply appends)
    for name, rel, ours, by_hand, append in (
        ("cargo", CARGO_CFG_REL, CARGO_HAS_BUILD_DIR, CARGO_HAS_BUILD, cargo_text(home)),
        ("uv", UV_CFG_REL, UV_HAS_LINK_MODE, UV_HAS_TABLE, UV_TEXT),
    ):
        path = os.path.join(home, rel)
        text = read_cfg(path)
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


def cargo_build_dir_is_csts(home: str) -> bool:
    try:
        with open(os.path.join(home, CARGO_CFG_REL), "rb") as f:
            build = tomllib.load(f).get("build")
    except OSError, tomllib.TOMLDecodeError:
        return False
    return isinstance(build, dict) and build.get("build-dir") == expected_template(home)


def lean_state(home: str) -> LeanState:
    """{path, add: [(label, block)], have: [label], conflicts: [text],
    append: text or '' (when appending is safe), error: ''}."""
    path = os.path.join(home, CARGO_CFG_REL)
    st: LeanState = {"path": path, "add": [], "have": [], "conflicts": [], "append": "", "error": ""}
    text = read_cfg(path)
    if text is None:
        st["error"] = "cannot be read"
        return st
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        st["error"] = f"is not valid TOML ({e})"
        return st
    for label_, tpath, want, block in LEAN_KEYS:
        table = dig(data, tpath)
        if isinstance(table, dict) and "debug" in table:
            if same_debug(table["debug"], want):
                st["have"].append(label_)
            else:
                st["conflicts"].append(f"[{label_}] debug = {json.dumps(table['debug'])} is yours: left as it is")
        else:
            st["add"].append((label_, block))
    if st["add"]:
        extra = LEAN_HEAD + "\n".join(b for _, b in st["add"])
        new = text + ("" if not text or text.endswith("\n") else "\n") + extra
        # Appended tables must not clash with the file's (TOML forbids a
        # table twice): parse the result and compare.
        try:
            nd = tomllib.loads(new)
        except tomllib.TOMLDecodeError:
            nd = None
        expect = json.loads(json.dumps(data))
        for label_, tpath, want, _ in LEAN_KEYS:
            if any(label_ == a for a, _ in st["add"]):
                d = expect
                for k in tpath:
                    d = d.setdefault(k, {})
                d["debug"] = want
        if nd is not None and json.loads(json.dumps(nd)) == expect:
            st["append"] = extra
    return st


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


def lean_lines(home: str, verb: str, settings: Settings | None, lean: bool | None) -> list[tuple[str, str, str]]:
    st = lean_state(home)
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
                + ("" if verb == "plan" else f" (missing: {PROG} config apply)"),
            )
        )
    else:
        out.append(
            ("BY HAND", shown, f"lean debug info ({why}): the file already has these tables: add yourself: {what}")
        )
    return out


def lean_apply(home: str, settings: Settings | None, lean: bool | None) -> int:
    want, why = lean_wanted(settings, lean)
    if not want:
        return 0
    st = lean_state(home)
    path = st["path"]
    if st["error"]:
        say(f"{NAME}: lean debug info: {path} {st['error']}: left as it is", 3, err=True)
        return 1
    for c in st["conflicts"]:
        say(f"NOTE: lean debug info: {path}: {c}", 5, err=True)
    if not st["add"]:
        if not st["conflicts"]:
            say(f"kept:      {path} (lean debug info already set)")
        return 0
    if not st["append"]:
        say(f"NOTE: lean debug info: {path} already has these tables: add by hand:", 5, err=True)
        for _, block in st["add"]:
            for line in block.splitlines():
                say(f"      {line}", 5, err=True)
        return 0
    err = append_text(path, st["append"])
    if err:
        say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
        return 1
    say(f"written:   {path} (lean debug info, {why}; the next dev build of each workspace compiles everything once)")
    return 0


def wrapper_text(values: WrapperValues) -> str | None:
    """The wrapper as installed (the settings written in), or None when its
    source is not next to this program (the timer's copy has none)."""
    text = read_text(os.path.join(Program.folder(), WRAPPER_NAME))
    if text is None:
        return None
    text, n = SCCACHE_SIZE_LINE.subn(f"cache_size={values.size}", text, count=1)
    text, m = SCCACHE_WORKSPACE_LINE.subn(f"workspace={'yes' if values.workspace else 'no'}", text, count=1)
    return text if n == m == 1 else None


def wrapper_state(home: str, text: str) -> str:
    """ok | change | missing | conflict (not CST's; a symlink counts by what it points to)."""
    path = wrapper_path(home)
    st = file_state(path, text)
    if st == "ok":
        try:
            if stat.S_IMODE(os.stat(path).st_mode) != 0o755:
                st = "change"
        except OSError:
            st = "change"
    return st


def wrapper_values(settings: Settings | None) -> WrapperValues:
    """No settings (a bad settings file; only --sccache/--no-sccache counts then): the defaults."""
    if not settings:
        return WrapperValues()
    return WrapperValues(
        str(settings.options["RUST_SCCACHE_CACHE_SIZE"]), bool(settings.options["RUST_SCCACHE_WORKSPACE"])
    )


def sccache_state(home: str) -> SccacheState:
    """{path, error, value (build.rustc-wrapper now, or None), ours, add,
    remove}: add / remove are the whole new file text, '' when CST cannot
    make that change safely (each candidate is parsed again and must equal
    the file with only that one key added or removed)."""
    path = os.path.join(home, CARGO_CFG_REL)
    st: SccacheState = {"path": path, "error": "", "value": None, "ours": False, "add": "", "remove": ""}
    text = read_cfg(path)
    if text is None:
        st["error"] = "cannot be read"
        return st
    try:
        data = plain(tomllib.loads(text))
    except tomllib.TOMLDecodeError as e:
        st["error"] = f"is not valid TOML ({e})"
        return st
    want = wrapper_path(home)
    build = data.get("build")
    if build is not None and not isinstance(build, dict):
        return st  # build = 1: no add, no remove
    value = (build or {}).get("rustc-wrapper")
    st["value"], st["ours"] = value, value == want
    keyline = f"rustc-wrapper = {json.dumps(want)}"
    if value is None:
        expect = plain(data)
        expect.setdefault("build", {})["rustc-wrapper"] = want
        headers = list(BUILD_HEADER.finditer(text))
        cands: list[str] = []
        if len(headers) == 1:
            h = headers[0]
            nxt = ANY_HEADER.search(text, h.end())
            section_end = nxt.start() if nxt else len(text)
            keys = list(KEY_LINE.finditer(text, h.end(), section_end))
            # After the table's last key (the build-dir line CST writes),
            # else right after the header.
            spots = ([text.find("\n", keys[-1].start())] if keys else []) + [h.end()]
            for pos in spots:
                if pos < 0:
                    pos = len(text)
                rest = text[pos:]
                cands.append(text[:pos] + "\n" + SCCACHE_HEAD + "\n" + keyline + (rest if rest else "\n"))
        elif not headers:
            sep = "" if not text or text.endswith("\n") else "\n"
            cands.append(text + sep + ("\n" if text else "") + SCCACHE_HEAD + "\n[build]\n" + keyline + "\n")
        st["add"] = next((c for c in cands if parses_as(c, expect)), "")
    elif st["ours"]:
        lines = text.splitlines(keepends=True)
        pat = re.compile(rf"{WS}*rustc-wrapper{WS}*={WS}*{re.escape(json.dumps(want))}{WS}*(?:#.*)?\n?\Z")
        idx = [i for i, ln in enumerate(lines) if pat.match(ln)]
        if len(idx) == 1:
            i = idx[0]
            drop = {i}
            if i > 0 and lines[i - 1].startswith(SCCACHE_HEAD[:48]):
                drop.add(i - 1)
            # CST's own appended block (a [build] header whose own section
            # holds only this key; [build.x] subtables may follow): all of it.
            after: list[str] = []
            for ln in lines[i + 1 :]:
                if ANY_HEADER.match(ln):
                    break
                after.append(ln)
            if (
                i > 1
                and BUILD_HEADER.match(lines[i - 1].rstrip("\r\n"))
                and lines[i - 2].startswith(SCCACHE_HEAD[:48])
                and not any(KEY_LINE.match(ln) for ln in after)
            ):
                drop |= {i - 1, i - 2}
                if i > 2 and not lines[i - 3].strip():
                    drop.add(i - 3)
            new = "".join(ln for n, ln in enumerate(lines) if n not in drop)
            expect = plain(data)
            del expect["build"]["rustc-wrapper"]
            alt = {k: v for k, v in expect.items() if not (k == "build" and v == {})}
            if parses_as(new, expect) or parses_as(new, alt):
                st["remove"] = new
    return st


def sccache_wanted(settings: Settings | None, flag: bool | None) -> tuple[bool, str]:
    return _flag_wanted(settings, flag, "RUST_SCCACHE", "sccache")


def wrapper_lines(home: str, verb: str, text: str | None, values: WrapperValues) -> list[tuple[str, str, str]]:
    shown = tilde(wrapper_path(home), home)
    if text is None:
        return [
            (
                "?",
                shown,
                f"sccache wrapper: its source ({WRAPPER_NAME} next to this program) is missing: "
                f"run {PROG} config apply from CST",
            )
        ]
    st = wrapper_state(home, text)
    if st == "ok":
        return [("ok", shown, f"sccache wrapper up to date ({values})")]
    if st == "conflict":
        return [("keep", shown, "not CST's file: left alone, so CST writes no rustc-wrapper line")]
    return [
        (
            "write",
            shown,
            f"sccache wrapper ({'missing' if st == 'missing' else 'outdated'}; {values})"
            + ("" if verb == "plan" else f" (fix: {PROG} config apply)"),
        )
    ]


def sccache_lines(home: str, verb: str, settings: Settings | None, flag: bool | None) -> list[tuple[str, str, str]]:
    st = sccache_state(home)
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
                    "BY HAND",
                    shown,
                    f"sccache ({why}): CST cannot remove its rustc-wrapper line safely: delete it yourself",
                )
            ]
        return [("keep", shown, f"sccache ({why}): {yours}")] if foreign else [("off", shown, "sccache: off")]
    text = wrapper_text(values)
    if not want:
        if st["ours"]:
            out = [
                (
                    "on",
                    shown,
                    f"sccache is on (CST's rustc-wrapper; {why} does not remove it: "
                    f"{PROG} config apply --no-sccache does)",
                )
            ]
            return out + [ln for ln in wrapper_lines(home, verb, text, values) if ln[0] != "ok"]
        return [("off", shown, off + (f"; {yours}" if foreign else ""))]
    if foreign:
        return [("keep", shown, f"sccache ({why}): {yours} (CST never overwrites it)")]
    out = wrapper_lines(home, verb, text, values)
    if st["ours"]:
        out.append(("ok", shown, f"sccache: rustc-wrapper set ({why})"))
    elif st["add"]:
        out.append(
            (
                "write",
                shown,
                f'sccache ({why}): [build] rustc-wrapper = "~/{WRAPPER_REL}"; {SCCACHE_REBUILD}'
                + ("" if verb == "plan" else f" (missing: {PROG} config apply)"),
            )
        )
    else:
        out.append(
            (
                "BY HAND",
                shown,
                f"sccache ({why}): CST cannot add the key safely here: put "
                f'rustc-wrapper = "{wrapper_path(home)}" into [build] yourself',
            )
        )
    return out


def install_wrapper(home: str, values: WrapperValues) -> int:
    """0 when the wrapper is in place (written or unchanged), else 1."""
    text = wrapper_text(values)
    path = wrapper_path(home)
    if text is None:
        say(
            f"{NAME}: sccache: {WRAPPER_NAME} is missing next to this program: run {PROG} config apply from CST",
            3,
            err=True,
        )
        return 1
    if wrapper_state(home, text) == "conflict":
        say(f"{NAME}: sccache: {path} is not CST's file: left alone", 3, err=True)
        return 1
    try:
        written = put_wrapper(home, path, text, values.record())
    except WriteFailed as e:
        say(f"{NAME}: {e}", 3, err=True)
        return 1
    say(f"written:   {path} (sccache wrapper, {values})" if written else f"kept:      {path} (sccache wrapper)")
    return 0


def sccache_apply(home: str, settings: Settings | None, flag: bool | None) -> int:
    want, why = sccache_wanted(settings, flag)
    st = sccache_state(home)
    path = st["path"]
    if st["error"]:
        if want or flag is False or st["ours"]:
            say(f"{NAME}: sccache: {path} {st['error']}: left as it is", 3, err=True)
            return 1
        return 0
    if flag is False:
        if not st["ours"]:
            return 0
        if not st["remove"]:
            say(
                f"NOTE: sccache: {path}: CST cannot remove its rustc-wrapper line safely: delete it yourself",
                5,
                err=True,
            )
            return 0
        err = write_cfg(path, st["remove"])
        if err:
            say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
            return 1
        say(f"removed:   {path} (CST's rustc-wrapper line, {why}; ~/{WRAPPER_REL} stays, unused)")
        return 0
    if not want:
        # The line is there (set earlier): keep its wrapper current, so cargo
        # never names a missing program.
        return install_wrapper(home, wrapper_values(settings)) if st["ours"] else 0
    if st["value"] is not None and not st["ours"]:
        say(
            f"NOTE: sccache: {path}: rustc-wrapper = {json.dumps(st['value'])} is yours: left as it is "
            f"(sccache not set up)",
            5,
            err=True,
        )
        return 0
    # The wrapper first: the line must never name a missing program.
    if install_wrapper(home, wrapper_values(settings)):
        return 1
    if st["ours"]:
        say(f"kept:      {path} (rustc-wrapper already set)")
        return 0
    if not st["add"]:
        say(f"NOTE: sccache: {path}: CST cannot add the key safely: put this into [build] by hand:", 5, err=True)
        say(f"      rustc-wrapper = {json.dumps(wrapper_path(home))}", 5, err=True)
        return 0
    err = write_cfg(path, st["add"])
    if err:
        say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
        return 1
    say(f"written:   {path} (rustc-wrapper, {why}: rustc goes through sccache where it is on PATH)")
    return 0


def config_lines(
    home: str,
    verb: str,
    settings: Settings | None = None,
    lean: bool | None = None,
    sccache: bool | None = None,
) -> list[tuple[str, str, str]]:
    """The plan or status lines of the settings (lean, sccache: the explicit
    flags)."""
    out: list[tuple[str, str, str]] = []
    for name, path, st, _ in config_items(home):
        shown = tilde(path, home)
        if name == "cargo":
            if st == "set":
                if cargo_build_dir_is_csts(home):
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
                        "BY HAND",
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
                        + ("" if verb == "plan" else f" (missing: {PROG} config apply)"),
                    )
                )
            else:
                out.append(("?", shown, "cannot be read"))
        else:
            if st == "set":
                out.append(("ok", shown, "link-mode already set"))
            elif st == "by-hand":
                out.append(("BY HAND", shown, 'has tables: add link-mode = "clone" at its top yourself'))
            elif st == "write":
                out.append(
                    (
                        "write",
                        shown,
                        'link-mode = "clone" (hardlinks cannot cross subvolumes)'
                        + ("" if verb == "plan" else f" (missing: {PROG} config apply)"),
                    )
                )
            else:
                out.append(("?", shown, "cannot be read"))
    out += lean_lines(home, verb, settings, lean)
    out += sccache_lines(home, verb, settings, sccache)
    return out


def cmd_config(home: str, action: str, lean: bool | None = None, sccache: bool | None = None) -> int:
    settings: Settings | None
    try:
        settings = Settings(home)
    except ConfigError as e:
        settings = None
        off = [w for w, flag in (("lean debug info", lean), ("sccache", sccache)) if flag is None]
        if off:
            say(f"note: BAD SETTINGS ({e}): {' and '.join(off)} treated as off", 4, err=True)
    if action == "plan":
        lines = config_lines(home, "plan", settings, lean, sccache)
        for st, shown, note in lines:
            say(f"  {st:<8} {shown:<24} {note}")
        if any(st in ("write", "remove") for st, _, _ in lines):
            flags = {True: " --lean-debuginfo", False: " --no-lean-debuginfo"}.get(lean, "")
            flags += {True: " --sccache", False: " --no-sccache"}.get(sccache, "")
            say(f"Apply with: {PROG} config apply" + flags)
        return 0
    rc = 0
    for name, path, st, text in config_items(home):
        if st == "unreadable":
            say(f"{NAME}: cannot read {path}: left as it is", 3, err=True)
            rc = 1
        elif name == "cargo" and st == "set":
            say(f"kept:      {path} (build-dir already set)")
        elif name == "cargo" and st == "by-hand":
            say(f"NOTE: {path} already configures [build]: add there by hand:", 5, err=True)
            say(f'      build-dir = "{home}/{ROOT_REL}/{HASH_VAR}"', 5, err=True)
        elif name == "uv" and st == "set":
            say(f"kept:      {path} (link-mode already set)")
        elif name == "uv" and st == "by-hand":
            say(f'NOTE: {path} has tables: add link-mode = "clone" at its top by hand', 5, err=True)
        else:
            err = append_text(path, text)
            if err:
                # For example a ~/.cargo owned by root after a `sudo cargo`:
                # report it and go on with the other setting.
                say(f"{NAME}: cannot write {path}: {err}: left as it is", 3, err=True)
                rc = 1
                continue
            say(f"written:   {path}")
    rc = max(rc, lean_apply(home, settings, lean))
    return max(rc, sccache_apply(home, settings, sccache))

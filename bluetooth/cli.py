"""The `bt-helper` command line: argv to the bluetooth.domain rules, printed.

A driving adapter: it reads files and the system through BluetoothHostPort
(bluetooth.adapters in production, fakes in tests) and writes the answers
to the given streams, in the formats bluetooth-setup.sh parses.
"""

from collections.abc import Sequence
import json
import sys
from typing import Final, TextIO

from bluetooth.domain import (
    SETTING_KEYS,
    SETTINGS_VALUE_RE,
    adapter_path,
    card_state,
    current_values,
    device_id,
    edit_general,
    lw_failure,
    merged_settings,
    pw_state,
    render_le_off,
    render_unit,
    render_wp,
    revert_general,
    roles_state,
    satisfied,
    set_keys,
    settings_set,
    unit_state,
    wp_state,
)
from bluetooth.ports import BluetoothHostPort

_ARITY: Final[dict[str, int]] = {
    "general-apply": 1,
    "general-show": 1,
    "general-revert": 2,
    "wp-render": 2,
    "wp-state": 3,
    "ldac-unit": 2,
    "unit-state": 3,
    "adapter-path": 0,
    "card-state": 1,
    "pw-parse": 1,
    "le-render": 0,
    "le-state": 1,
    "settings-keys": 1,
    "lw-failure": 0,
    "wp-roles": 0,
    "settings-set": 3,
    "device-id": 1,
}


def main(
    argv: Sequence[str],
    host: BluetoothHostPort,
    *,
    doc: str = "",
    argv0: str = "bt-helper",
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Dispatch one `bt-helper` command against `host` and return its exit status."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr

    if not argv:
        if doc:
            out.write(doc)
        return 2

    cmd, args = argv[0], argv[1:]
    if cmd == "wp-others":
        if not args:
            print(f"bt-helper: usage: see {argv0} (no arguments)", file=err)
            return 2
        for where, path in host.other_fragments(args[0], args[1:]):
            print(f"{where}|{path}", file=out)
        return 0
    if cmd == "settings":
        texts = [t for t in (host.read_or_none(a) for a in args) if t is not None]
        for k, v in merged_settings(texts).items():
            print(f"{k}={v}", file=out)
        return 0
    if cmd not in _ARITY or len(args) != _ARITY[cmd]:
        print(f"bt-helper: usage: see {argv0} (no arguments)", file=err)
        return 2
    if cmd == "general-apply":
        new, changed = edit_general(host.read(args[0]))
        out.write(new)
        return 10 if changed else 0
    if cmd == "general-show":
        for key, val in current_values(host.read(args[0])).items():
            ok = val is not None and satisfied(key, val)
            print(f"{key}\t{'ok' if ok else 'update'}\t{'(not set)' if val is None else val}", file=out)
        return 0
    if cmd == "general-revert":
        new, changed = revert_general(host.read(args[0]), host.read(args[1]))
        out.write(new)
        return 10 if changed else 0
    if cmd == "wp-render":
        out.write(render_wp(args[0], args[1]))
        return 0
    if cmd == "wp-state":
        print(wp_state(host.read_or_none(args[0]), render_wp(args[1], args[2])), file=out)
        return 0
    if cmd == "le-render":
        out.write(render_le_off())
        return 0
    if cmd == "le-state":
        print(wp_state(host.read_or_none(args[0]), render_le_off()), file=out)
        return 0
    if cmd == "wp-roles":
        ok, value = host.wp_effective_roles()
        if not ok:
            print(f"bt-helper: {value}", file=err)
            return 5
        print(f"{roles_state(value)}|{'' if value is None else value}", file=out)
        return 0
    if cmd == "settings-set":
        if args[1] not in SETTING_KEYS or not SETTINGS_VALUE_RE.fullmatch(args[2]):
            print(f"bt-helper: settings-set: unknown key or bad value: {args[1]}={args[2]}", file=err)
            return 2
        new, changed = settings_set(host.read_or_none(args[0]), args[1], args[2])
        out.write(new)
        return 10 if changed else 0
    if cmd == "settings-keys":
        text = host.read_or_none(args[0])
        for k in set_keys(text or ""):
            print(k, file=out)
        return 0
    if cmd == "lw-failure":
        failure = lw_failure(host.read_stdin())
        if failure is None:
            return 1
        print("|".join(x.replace("|", "/") for x in failure), file=out)
        return 0
    if cmd == "ldac-unit":
        out.write(render_unit(args[0], args[1]))
        return 0
    if cmd == "unit-state":
        print(unit_state(host.read_or_none(args[0]), render_unit(args[1], args[2])), file=out)
        return 0
    if cmd == "device-id":
        print(device_id(args[0]), file=out)
        return 0

    try:
        data = json.loads(host.read_stdin())
    except ValueError:
        return 3 if cmd == "pw-parse" else 1
    if cmd == "adapter-path":
        path = adapter_path(data)
        if path is None:
            return 1
        print(path, file=out)
        return 0
    if cmd == "card-state":
        card_st = card_state(args[0], *data)
        if card_st is None:
            return 1
        print(f"{card_st[0]}|{int(card_st[1])}|{int(card_st[2])}", file=out)
        return 0
    pw_st = pw_state(args[0], data)  # pw-parse
    if pw_st is None:
        return 4
    print("|".join(x or d for x, d in zip(pw_st, ("?", "?", "-", "-", "-"), strict=False)), file=out)
    return 0

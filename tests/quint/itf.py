# SPDX-License-Identifier: MPL-2.0
"""ITF traces (the JSON quint writes with --out-itf) as Python values, for the replay tests of CST's Quint
specs (btrfs/tests/test_docker_volumes_nocow_replay.py, vm/tests/test_vm_nas_replay.py).

A test helper: it imports cst_core.domain.shapes (Python 3.13 or later), so tests/quint/cst-quint, which
runs on the CI runner's Python 3.12, never imports it."""

from __future__ import annotations

from cst_core.domain.shapes import json_object

type Value = bool | int | str | list[Value] | dict[str, Value]


def itf(value: object) -> Value:
    """An ITF value as Python: {"#bigint": "N"} an int, {"#set": [...]} and {"#tup": [...]} a list, {"#map": [[k, v],
    ...]} a dict, records and lists as they are."""
    if isinstance(value, bool | str):
        return value
    if isinstance(value, list):
        return [itf(v) for v in value]
    if not json_object(value):
        raise AssertionError(f"not an ITF value: {value!r}")
    if "#bigint" in value:
        return int(str(value["#bigint"]))
    if "#set" in value or "#tup" in value:  # a set (its items in the trace's order) or a tuple: a list
        members = value.get("#set", value.get("#tup"))
        if not isinstance(members, list):
            raise AssertionError(f"not an ITF set or tuple: {value!r}")
        return [itf(v) for v in members]
    if "#map" in value:
        pairs = value["#map"]
        if not isinstance(pairs, list):
            raise AssertionError(f"not an ITF map: {value!r}")
        out: dict[str, Value] = {}
        for pair in pairs:
            if not (isinstance(pair, list) and len(pair) == 2):
                raise AssertionError(f"not an ITF map entry: {pair!r}")
            out[str(itf(pair[0]))] = itf(pair[1])
        return out
    return {k: itf(v) for k, v in value.items() if not k.startswith("#")}


def record(value: Value) -> dict[str, Value]:
    if not isinstance(value, dict):
        raise AssertionError(f"not a record: {value!r}")
    return value


def text(value: Value) -> str:
    if not isinstance(value, str):
        raise AssertionError(f"not a string: {value!r}")
    return value


def number(value: Value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssertionError(f"not an int: {value!r}")
    return value


def truth(value: Value) -> bool:
    if not isinstance(value, bool):
        raise AssertionError(f"not a bool: {value!r}")
    return value


def items(value: Value) -> list[Value]:
    if not isinstance(value, list):
        raise AssertionError(f"not a list: {value!r}")
    return value

# SPDX-License-Identifier: MPL-2.0
"""JSON and TOML from outside CST, parsed once and checked by shape.

parse_json and parse_toml are the only callers of the json and tomllib parsers in
production (PRIMITIVE_OWNERS in tests/test_architecture.py). They return object, not
Any: ty then asks every reader to check the shape (json_object, has_shape or a TypeIs
guard of its own) before the value reaches a TypedDict or a typed parameter.
"""

from collections.abc import Mapping
import json
import tomllib
from typing import TypeIs

# A shape's keys -> the types each may hold (has_shape).
type KeyTypes = Mapping[str, tuple[type, ...]]


def parse_json(text: str | bytes) -> object:
    """json.loads(text): the same value and the same exceptions (JSONDecodeError, a
    UnicodeDecodeError for bytes that are not UTF-8, RecursionError for deep nesting).
    A file reader passes f.read(): json.load(f) is exactly json.loads(f.read())."""
    return json.loads(text)


def parse_toml(data: str | bytes) -> object:
    """tomllib.loads(data): the same value and the same TOMLDecodeError. Bytes are
    decoded as tomllib.load decodes a binary file (strict UTF-8: a bad byte is a
    UnicodeDecodeError, not a TOMLDecodeError), so a reader passes f.read()."""
    return tomllib.loads(data.decode() if isinstance(data, bytes) else data)


def json_object(value: object) -> TypeIs[dict[str, object]]:
    """value is a JSON object: a dict whose keys are all strings."""
    return isinstance(value, dict) and all(isinstance(k, str) for k in value)


def has_shape(obj: object, required: KeyTypes, optional: KeyTypes) -> bool:
    """obj is a JSON object with every required key and each known key of its type (a bool
    is no int here, as JSON tells them apart). Keys it does not name are not checked."""
    if not json_object(obj):
        return False
    for key, kinds in (*required.items(), *optional.items()):
        if key not in obj:
            if key in required:
                return False
            continue
        value = obj[key]
        if not isinstance(value, kinds) or (isinstance(value, bool) and bool not in kinds):
            return False
    return True

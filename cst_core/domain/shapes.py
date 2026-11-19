# SPDX-License-Identifier: MPL-2.0
"""JSON and TOML from outside CST, parsed once and checked by shape.

parse_json and parse_toml are the only callers of the json and tomllib parsers in
production (PRIMITIVE_OWNERS in tests/test_architecture.py). They return object, not
Any: ty then asks every reader to check the shape (json_object, has_shape or a TypeIs
guard of its own) before the value reaches a TypedDict or a typed parameter.
"""

from collections.abc import Callable, Mapping
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


class ShapeError(TypeError):
    """A parsed JSON value is not of the shape a reader needs. Raised where Python's own
    TypeError, AttributeError, KeyError or IndexError came before (a .get on a list, a
    subscript of a number): a TypeError, so a caller that catches TypeError still does."""


def object_of(value: object) -> dict[str, object]:
    """value as a JSON object, for its .get and its keys; ShapeError for anything else."""
    if json_object(value):
        return value
    raise ShapeError(f"not a JSON object: {type(value).__name__}")


def object_or_empty(value: object) -> dict[str, object]:
    """`value or {}` as a JSON object: {} for a falsy value (null, false, 0, "", []);
    ShapeError for any other value that is not an object."""
    return object_of(value) if value else {}


def items_of(value: object) -> list[object]:
    """What `for x in value` gives for a JSON value: a list's items, a string's characters, an
    object's keys; ShapeError for a number, a bool or null."""
    if isinstance(value, list | str | dict):
        return list(value)
    raise ShapeError(f"cannot iterate over {type(value).__name__}")


def first_item(value: object) -> object:
    """value[0] of a JSON value: a list's first item, a string's first character; ShapeError
    for an empty one, an object (its keys are strings), a number, a bool or null."""
    if isinstance(value, list | str) and len(value) > 0:
        return value[0]
    raise ShapeError(f"no first item in {type(value).__name__}")


def has_item(container: object, item: str) -> bool:
    """`item in container` for a JSON value: an object's key, a list's item, a substring of a
    string; ShapeError for a number, a bool or null."""
    if isinstance(container, list | str | dict):
        return item in container
    raise ShapeError(f"cannot look into {type(container).__name__}")


def list_of[T](value: object, item: Callable[[object], TypeIs[T]]) -> TypeIs[list[T]]:
    """value is a list whose every item passes the guard ITEM (all or nothing; a reader that skips
    the items of another shape filters with the guard instead). For a value just parsed: a list
    narrowed this way must not be given to code that adds items of another type."""
    return isinstance(value, list) and all(item(v) for v in value)


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

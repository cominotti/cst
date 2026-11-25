---
description: A closed vocabulary (states, kinds, subcommands, modes, results) is a StrEnum matched with match; a number with a unit or meaning uses cst_core's names; a record read by key is a TypedDict; outside text is parsed once at the border; tests/test_magic_values.py counts what is left
---
<!-- SPDX-License-Identifier: MPL-2.0 -->
<!-- global rule: loaded for every request -->

# Name the value

ty catches `OwnedState.OKK` and a `match` that forgets a case; it does not
catch `state == "okk"`. So a word or a number that means something is
declared once and used by name.

## Which form

- **StrEnum** (`enum.StrEnum`, in the package's domain module): a closed set
  of words the code compares or matches: states of plan and status, kinds,
  subcommands, modes, results. A member is the `str` of its value, so
  f-strings, `str()`, `json.dumps`, dict keys, argv words and `sorted()` give
  the plain word, and a test that passes a plain string still compares equal.
  `repr()` and `!r` do not: print `.value` or `str(x)` where the output
  showed the word.
- **A Literal of members** (`type WholeFileState = Literal[OwnedState.OK, ...]`)
  for the subset one function returns; not a `Literal` of strings. A
  `Literal[...]` of strings stays only for a fixed outside format, listed in
  `MAGIC_EXEMPT` with the reason.
- **Final**: one value with a meaning (`MAX_TRIES: Final = 3`), not a set.
- **Units**: `MINUTE`, `HOUR`, `DAY`, `WEEK`, `MIB`, `GIB`
  (`cst_core.domain.values`) and `ExitStatus` (`cst_core.domain.errors`:
  `TIMED_OUT` 124, `CANNOT_RUN` 126, `NOT_FOUND` 127, `SIGNALLED` 128,
  `KILLED` 137), never 3600, 86400 or 127 written out.
- **TypedDict** (next to its reader or writer, `NotRequired` for an optional
  key) for a JSON or dict format read by key; not `dict[str, Any | object |
  Json]` in a signature. The reader still returns what `json.loads` gave: a
  reader that validates returns it through a `TypeIs[TheDict]` guard, never a
  `cast`, never a rebuilt dict. The guard is built on `cst_core.domain.shapes`
  (`json_object`, `has_shape`, `list_of`) over the `object` that `parse_json`
  or `parse_toml` gave, and checks every key whose type is narrower than
  `object` or `Json` and every Required key. Two gates keep this border:
  `json.load(s)` and `tomllib.load(s)` are called nowhere else in production
  code (`PRIMITIVE_OWNERS` in `tests/test_architecture.py`), so every reader
  gets the `object` that `parse_json` and `parse_toml` return, which ty does
  not let reach a narrower type unchecked; and ty's `unsound-return` and
  `unsound-assignment` refuse a value typed `Any` from elsewhere (a regex
  group, `sys.stdout`, `struct.unpack`) where a narrower type is declared
  ([docs/dev-tools.md](../../docs/dev-tools.md#what-each-gate-checks)).

## Rules that keep the behaviour

- Text from outside (systemd, files, argv, docker, JSON) is converted **once,
  at the border**, by a parse function that keeps the fall-through of the
  code before it. `MyEnum(text)` alone raises `ValueError` where an `else`
  used to run.
- An if/elif chain with 3 or more string arms on one subject becomes `match`
  on the enum. Its `else` becomes `case _:` with the same body; a chain
  with no `else` gets `case _: pass`. `case _: assert_never(x)` only where
  the subject is always made inside the program as the enum.
- Compare with `==` or `match`, never `is`: tests and old state files give
  plain strings.
- A word two vocabularies share (`"ok"`, `"missing"`, `"root"`) means both
  are enums: compare each subject with a member of its own enum.
- A literal another program reads from the source text (doctor's regex for
  `NAME: Final = N`, a default read with `ast.literal_eval`) stays literal,
  in `MAGIC_EXEMPT` with the reason.
- A format two programs share (a state file's tags, the install record's
  pending and done) is defined once, in one enum both import.
- Give each enum a specific name (`MoveResult`, `WpState`): the same-name
  gate (`tests/test_duplication.py`) fails on one name defined in two files.

## The gates

- `tests/test_magic_values.py` counts, per package of production code (tests
  keep the raw words), string comparisons with an enum's word (leak), if/elif
  or match chains of 3 string arms, unit numbers, loose dicts in signatures,
  `Literal`s of 3 strings and `int()` of text (int-of-text: an `int()` whose
  argument is not a number by its form, or that has a base; text goes
  through `parse_whole_number` or `parse_integer`, a float through
  `math.trunc`; cst_core's parsers and bash arithmetic's readers are
  exempt), under `MAGIC_CEILINGS` (never above), one entry
  per package and kind of hit. A package and kind with no entry may have no
  hit at all, so a new hit there fails. `MAGIC_EXEMPT` names a scope that
  keeps one (`path:qualname`), with its reason; a stale entry fails. No
  ignore comments.
- ruff's `PLR2004` (a number compared with a literal) is counted in
  `RUFF_BUDGETS` (`tests/ruff_budget.py`), production code only.
- A test passes the member where ty asks for it and keeps the raw string in
  its assertion: the assertion pins the value.

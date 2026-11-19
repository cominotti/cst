# SPDX-License-Identifier: MPL-2.0
"""as_user: run one step of btrfs-bulk in a child process that dropped to the user, and read
back its result (JSON over a pipe), checked by a guard per step.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import os
import pwd
import sys
from typing import TypeIs

from btrfs.bulk.domain import FlatpakAccess, Unexpected
from cst_core.domain.shapes import KeyTypes, has_shape, json_object, parse_json
from cst_core.domain.values import Json

type Account = pwd.struct_passwd  # whose uid, gids and HOME the child takes


def is_bool(v: object) -> TypeIs[bool]:
    return isinstance(v, bool)


def is_int(v: object) -> TypeIs[int]:
    return isinstance(v, int) and not isinstance(v, bool)


def is_str(v: object) -> TypeIs[str]:
    return isinstance(v, str)


def is_none(v: object) -> TypeIs[None]:
    return v is None


def is_list(v: object) -> TypeIs[list[Json]]:
    return isinstance(v, list)


def is_flatpak_access(v: object) -> TypeIs[FlatpakAccess]:
    """A FlatpakAccess as it crosses back as JSON: the word of one of its members."""
    return isinstance(v, str) and v in set(FlatpakAccess)


def is_user_read(v: object) -> TypeIs[dict[str, Json] | None]:
    """u_read's answer: None, or the file's base64 bytes and its whole-number mtime, mode and uid."""
    return v is None or has_shape(v, _USER_READ, {})


def as_user[**P, R: Json](
    pw: Account | None,
    home: str,
    check: Callable[[object], TypeIs[R]],
    fn: Callable[P, R],
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """fn(*args, **kwargs) in a child process with the user's uid, gids and HOME
    (pw None: already the user, run here). Returns its result, which crosses
    back as JSON (R: a Json value) and must pass CHECK (a result of another
    shape is no result of fn: RuntimeError); an exception there is raised here
    again."""
    if pw is None:
        return fn(*args, **kwargs)
    sys.stdout.flush()
    sys.stderr.flush()
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(r)
            os.setgroups(os.getgrouplist(pw.pw_name, pw.pw_gid))
            os.setresgid(pw.pw_gid, pw.pw_gid, pw.pw_gid)
            os.setresuid(pw.pw_uid, pw.pw_uid, pw.pw_uid)
            os.umask(0o022)
            os.chdir(home)
            for k in [k for k in os.environ if k.startswith("XDG_")]:
                del os.environ[k]
            os.environ.update(HOME=pw.pw_dir, USER=pw.pw_name, LOGNAME=pw.pw_name)
            out = {"ok": fn(*args, **kwargs)}
        except Unexpected as e:
            out = {"unexpected": str(e)}
        except BaseException as e:  # noqa: BLE001 - reported to the parent
            out = {"error": f"{type(e).__name__}: {e}"}
            code = 1
        try:
            os.write(w, json.dumps(out).encode())
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
    os.close(w)
    data = b""
    while True:
        chunk = os.read(r, 65536)
        if not chunk:
            break
        data += chunk
    os.close(r)
    os.waitpid(pid, 0)
    return child_result(data, check)


def child_result[R: Json](data: bytes, check: Callable[[object], TypeIs[R]]) -> R:
    """The result as_user's child wrote (JSON: {"ok": result}, {"unexpected": why} or
    {"error": why}); Unexpected or RuntimeError for the other two, and RuntimeError for a
    result CHECK refuses or an answer that is none of these (the child died)."""
    try:
        out = parse_json(data or b"{}")
    except ValueError, RecursionError:
        out = {}
    if not json_object(out):
        out = {}
    if "unexpected" in out:
        raise Unexpected(str(out["unexpected"]))
    if "ok" not in out:
        raise RuntimeError(str(out.get("error", "child process died")))
    result = out["ok"]
    if not check(result):
        raise RuntimeError(f"the child process gave a result of another shape ({type(result).__name__})")
    return result


_USER_READ: KeyTypes = {"b64": (str,), "mtime": (int,), "mode": (int,), "uid": (int,)}

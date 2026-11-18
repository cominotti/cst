# SPDX-License-Identifier: MPL-2.0
"""The boundaries the prune use case (toolchains/application/) reaches the machine
through: cargo's build folders and who uses them, uv, the saved run summaries, and the
lines a run says. cst_core's ClockPort gives the monotonic time; the adapters in
toolchains/adapters/ implement these.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cst_core.domain.values import Json
    from cst_core.ports import CommandResult
    from toolchains.domain.build import CargoCache, Folder, Workspaces
    from toolchains.domain.prune import RunRecord

# (folders, leftovers of an interrupted run, other entries with why they are left alone)
type Scan = tuple[list[Folder], list[Folder], list[tuple[str, str]]]
# {folder key: [(pid, comm)]}: the processes that use a folder
type Users = dict[str, list[tuple[str, str]]]


class BuildTreePort(Protocol):
    """cargo's build folders under the root build-dir names, and what uses them:
    the user's processes, the cargo processes (asked with `cargo metadata`) and
    the workspaces that still claim a folder. A deletion is one call: it takes
    cargo's locks, checks the users and the rule again, renames away and removes."""

    def root(self) -> str | None:
        """The root from build-dir in the user's cargo config; None when it does not exist
        yet; Refused when the setting is not CST's or the folder is odd."""
        ...

    def scan(self, root: str, now: float) -> Scan: ...

    def device(self, root: str) -> int:
        """The root's filesystem: a folder is measured against it for the whole run."""
        ...

    def measure(self, folder: Folder, dev: int, now: float) -> Folder:
        """FOLDER measured (size, activity, incremental folders), not leaving DEV."""
        ...

    def exists(self, path: str) -> bool:
        """Something is at PATH (a link counts): a folder that was not renamed away."""
        ...

    def remove(self, path: str, timeout: float | None) -> tuple[bool, str]:
        """(ok, message) of removing a tree (a leftover), stopped after TIMEOUT seconds."""
        ...

    def delete(
        self,
        root: str,
        folder: Folder,
        now: float,
        cache: CargoCache,
        recheck: Callable[[], str] | None,
        deadline: float | None,
    ) -> tuple[str, str]:
        """Delete FOLDER while no process uses it and RECHECK() says '' (deleted, busy,
        changed, stop or failed, with the detail)."""
        ...

    def trim(
        self,
        root: str,
        folder: Folder,
        now: float,
        cache: CargoCache,
        recheck: Callable[[], str] | None,
        deadline: float | None,
    ) -> tuple[str, str]:
        """Delete only FOLDER's incremental folders, with the same checks as delete
        (trimmed, busy, changed, stop or failed, with the detail)."""
        ...

    def open_by(self, root: str) -> tuple[Users, int]:
        """The user's processes with a file, folder or mapping under ROOT, per folder key,
        and how many Rust tools of the user could not be inspected."""
        ...

    def cargo_runs(self, root: str, cache: CargoCache) -> tuple[Users, list[tuple[str, str, str | None, str]]]:
        """The folders running cargo processes build in, and the cargo processes whose
        folder is unknown (pid, comm, cwd, why); CACHE keeps cargo's answers for one run."""
        ...

    def workspaces(self, root: str) -> Workspaces:
        """Which folders of ROOT a workspace under ~/Workspace still claims."""
        ...


class UvPort(Protocol):
    """uv and its cache: found on a PATH, asked (read only), and run to prune."""

    def find(self, path: str | None) -> str | None:
        """The uv program on PATH (None: this process's PATH), or None."""
        ...

    def facts(self, uv: str) -> tuple[str, bool]:
        """(version text, whether `uv cache prune` has --dry-run), from uv itself."""
        ...

    def preview(self, uv: str, cache: str) -> tuple[list[str], bool]:
        """(lines, ok) of `uv cache prune --dry-run`."""
        ...

    def prune(self, uv: str, cache: str, timeout: float) -> CommandResult:
        """`uv cache prune` on CACHE at idle priority, stopped after TIMEOUT seconds."""
        ...

    def cache_kind(self, path: str) -> str:
        """What is at PATH (followed through links): 'missing', 'folder' or 'other'."""
        ...

    def is_dir(self, path: str) -> bool: ...

    def lock_busy(self, cache: str) -> bool:
        """Another uv holds the cache's lock now."""
        ...

    def size(self, path: str) -> int:
        """The size of a tree as du shows it."""
        ...


class RunStatePort(Protocol):
    """The summaries of the last run of each target (~/.local/state/cst)."""

    def read(self, tid: str) -> Mapping[str, Json] | None: ...

    def write(self, tid: str, record: RunRecord) -> None:
        """Saved; a failure is said, not raised (the run itself went through)."""
        ...


class RunOutputPort(Protocol):
    """The lines a command says (with the journal's priority under systemd)."""

    def say(self, msg: str, prio: int = 6, *, err: bool = False) -> None: ...

    def where(self, path: str) -> str:
        """PATH as the lines show it: with ~, and where it really is when a link on the way
        leads elsewhere."""
        ...

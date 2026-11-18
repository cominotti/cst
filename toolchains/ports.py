# SPDX-License-Identifier: MPL-2.0
"""The boundaries the use cases (toolchains/application/) reach the machine
through: cargo's build folders and who uses them, uv, the saved run summaries, the
lines a run says, the user manager, the files CST keeps in the home and the
reports of what is installed besides the caches. cst_core's ClockPort gives the
monotonic time; the adapters in toolchains/adapters/ implement these.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from cst_core.ports import CommandResult
    from cst_core.domain.files import UnitFileState, WholeFileState
    from toolchains.domain.auto import TimerFacts
    from toolchains.domain.build import CargoCache, Folder, Workspaces
    from toolchains.domain.config import WrapperValues
    from toolchains.domain.prune import RunRecord, SavedRun
    from toolchains.domain.uv import UvCacheKind

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

    def cache_kind(self, path: str) -> UvCacheKind:
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

    def read(self, tid: str) -> SavedRun | None: ...

    def write(self, tid: str, record: RunRecord) -> None:
        """Saved; a failure is said, not raised (the run itself went through)."""
        ...

    def forget(self, tid: str) -> None:
        """The summary is removed (auto off); one that cannot be removed is left."""
        ...


class RunOutputPort(Protocol):
    """The lines a command says (with the journal's priority under systemd)."""

    def say(self, msg: str, prio: int = 6, *, err: bool = False) -> None: ...

    def where(self, path: str) -> str:
        """PATH as the lines show it: with ~, and where it really is when a link on the way
        leads elsewhere."""
        ...


class UserUnitsPort(Protocol):
    """The user manager (systemctl --user), as auto on, auto off and status use it. The
    calls made before files are removed are checked: Refused when the manager refuses or
    gives no answer, so nothing is removed yet and a second run can finish the job."""

    def active_state(self, name: str) -> str:
        """What is-active says of NAME ('' when the manager could not be asked)."""
        ...

    def timer_facts(self, name: str) -> TimerFacts:
        """What the manager says of the timer NAME (its next run only while it is active)."""
        ...

    def stop_if_running(self, name: str) -> bool:
        """Stop NAME unless the manager says it is inactive or failed, both checked;
        True when it was stopped."""
        ...

    def disable_timer(self, name: str) -> None:
        """`disable --now` the timer NAME, checked, then forget its last run."""
        ...

    def reload(self) -> None:
        """daemon-reload, checked (Refused)."""
        ...

    def reload_failed(self) -> bool:
        """daemon-reload after unit files were removed: True (and an error line) when it failed."""
        ...

    def enable_now(self, name: str) -> None:
        """`enable --now` NAME, checked (Refused)."""
        ...

    def manager_path(self) -> str | None:
        """PATH of the user manager (what the timer's service runs with), or None when it
        cannot be read."""
        ...


class HomeFilesPort(Protocol):
    """The files CST keeps in the user's home: what auto on installs (the program's copy,
    its defaults and the two user units, listed in the user's install record), the file
    auto off leaves, what the earlier `cst btrfs cargo-gc` left, and for config the tools'
    settings files (cargo's config, uv.toml) and the sccache wrapper. PATH arguments are
    absolute paths in the home."""

    def auto_copies(self) -> dict[str, bytes]:
        """{path relative to the home: bytes} of the copies the timer runs; ConfigError
        when the defaults next to this program are missing."""
        ...

    def auto_states(self) -> dict[str, UnitFileState]:
        """The state of each copy (by its relative path) and unit (by its name)."""
        ...

    def timer_missing(self) -> bool:
        """No timer file of auto on is there (a link to nothing counts as none)."""
        ...

    def stale_files(self) -> list[tuple[str, bool]]:
        """[(path, ours)] of what the earlier `cst btrfs cargo-gc` left."""
        ...

    def put_auto_copy(self, rel: str, data: bytes) -> bool:
        """Written through the installer (and listed) unless current; True when written."""
        ...

    def put_auto_unit(self, name: str, text: str) -> bool:
        """Written through the installer (and listed) unless current; True when written."""
        ...

    def auto_done(self) -> None:
        """The user's install record says auto on finished."""
        ...

    def forget_auto_files(self) -> None:
        """The copies and units leave the user's install record (auto off removed them)."""
        ...

    def turned_off(self) -> bool:
        """The file auto off leaves is there."""
        ...

    def mark_off(self) -> None:
        """Write the file auto off leaves."""
        ...

    def remove(self, path: str) -> None:
        """Remove the file (or link) PATH; OSError when that fails."""
        ...

    def dangling_link(self, path: str) -> bool:
        """PATH is a symbolic link to nothing."""
        ...

    def remove_if_empty(self, path: str) -> None:
        """Remove the folder PATH when it is empty; otherwise leave it."""
        ...

    def old_conf_present(self) -> bool:
        """The settings file of the earlier `cst btrfs cargo-gc` is there (no longer read)."""
        ...

    def read_settings(self, path: str) -> str | None:
        """The text of a tool's settings file (bytes that are not UTF-8 replaced); '' when it
        is missing; None when it cannot be read."""
        ...

    def append(self, path: str, text: str) -> str:
        """Append TEXT to the settings file PATH (made if missing; a newline first when it
        does not end in one). '' or the error."""
        ...

    def rewrite(self, path: str, text: str) -> str:
        """Replace the settings file's text (atomic, same mode; a link is followed). '' or
        the error."""
        ...

    def build_dir_is_csts(self) -> bool:
        """build-dir in the user's cargo config is the line CST writes."""
        ...

    def wrapper_text(self, values: WrapperValues) -> str | None:
        """The sccache wrapper as installed with VALUES written in, or None when its
        source is not next to this program (the timer's copy has none)."""
        ...

    def wrapper_state(self, text: str) -> WholeFileState:
        """ok | change | missing | conflict of the installed wrapper against TEXT."""
        ...

    def put_wrapper(self, text: str, values: Mapping[str, str]) -> bool:
        """The wrapper TEXT, at its place in the home, through the installer, listed with VALUES,
        its component marked done; True when written (WriteFailed when it cannot be written)."""
        ...


class InstalledToolsPort(Protocol):
    """What status reports of the tools installed besides the caches (read only)."""

    def rust_toolchains(self) -> list[str]:
        """The installed rustup toolchains, their size, and which files under ~/Workspace pin
        or name them."""
        ...

    def sccache(self) -> list[str]:
        """The sccache cache's size and, per running server, its numbers and where it runs."""
        ...

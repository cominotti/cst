# SPDX-License-Identifier: MPL-2.0
"""docker-gc's ports: the docker engine, its own state files and run lock, the host, the terminal."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from btrfs.docker_gc.domain import Container, DockerEvent, EventsEnd, GcState, Image, RunSummary, Volume
from cst_core.ports import CommandResult


class EventSink(Protocol):
    """What the recorder does with `docker events` (DockerPort.follow_events calls it).

    resolve runs on the reading thread with no lock held (it may ask docker);
    use, tick and finish run under the stream's lock, so they never overlap.
    """

    def started(self) -> None:
        """The stream runs and a stop request is caught."""
        ...

    def resolve(self, event: DockerEvent) -> str:
        """The image ID an event used ("" for none)."""
        ...

    def use(self, image_id: str) -> None:
        """image_id was used now."""
        ...

    def tick(self) -> None:
        """Called every few seconds (the flush interval), whatever arrives."""
        ...

    def finish(self) -> None:
        """The stream ended: the last save."""
        ...


class DockerPort(Protocol):
    """The docker commands docker-gc runs. A docker that does not answer in time, or
    a listing or inspect that fails, raises RuntimeError; one that cannot start raises OSError."""

    def is_up(self) -> bool:
        """The daemon answers. False only when it is not running or docker is not installed; any
        other failure of `docker info` (permission denied, SELinux) raises RuntimeError."""
        ...

    def containers(self) -> list[Container]:
        """Every container, inspected (one that vanished meanwhile is left out)."""
        ...

    def volumes(self) -> list[Volume]: ...

    def images(self) -> list[Image]: ...

    def image_id(self, ref: str, *, timeout: int) -> str:
        """The ID a reference names now ("" when docker does not know it)."""
        ...

    def image_tags(self, image_id: str) -> list[str] | None:
        """The tags of an image (["?"] when its inspect is unreadable); None when it is gone."""
        ...

    def remove_container(self, container_id: str) -> CommandResult: ...

    def remove_volume(self, name: str) -> CommandResult: ...

    def remove_image(self, ref: str) -> CommandResult: ...

    def prune_build_cache(self, *, unused_hours: int) -> CommandResult: ...

    def disk_usage(self) -> Mapping[str, str]:
        """`docker system df`: size per type ("Images", ...)."""
        ...

    def follow_events(self, sink: EventSink, *, every: float) -> EventsEnd:
        """Follow container and image events until the stream ends or a stop request arrives."""
        ...


class GcStatePort(Protocol):
    """docker-gc's files: the recorded image use, the last run's summary, the run lock."""

    def state_exists(self) -> bool: ...

    def load_state(self) -> GcState:
        """The recorded state; a fresh one (since now) when there is none or it is unreadable."""
        ...

    def save_state(self, state: GcState) -> None: ...

    def load_summary(self) -> RunSummary | None: ...

    def save_summary(self, summary: RunSummary) -> None: ...

    def take_run_lock(self) -> bool:
        """Hold the run lock until the process ends; False when another run holds it."""
        ...


class GcSettingsPort(Protocol):
    """The settings file (GcPaths.settings_file) and the installed docker-gc (GcPaths.installed)."""

    def read_settings(self) -> str | None:
        """The settings file's text; None when it is missing. One it cannot read raises UnknownProtection."""
        ...

    def installed_sources(self) -> tuple[str, ...] | None:
        """The Python sources of the installed docker-gc that hold its built-in protected registries
        (none when it cannot read them); None when no docker-gc is installed."""
        ...

    def write_settings(self, text: str) -> None:
        """Write the settings file (root, 0644), atomically."""
        ...


class GcHostPort(Protocol):
    """The machine around docker: its uptime and the recorder's unit."""

    def uptime(self) -> float: ...

    def recorder_active(self) -> bool:
        """docker-gc-recorder.service is active (a systemctl that does not answer: False)."""
        ...


class ConsolePort(Protocol):
    """The terminal and the journal: lines out, journal lines with a priority, questions."""

    def say(self, line: str) -> None: ...

    def error(self, line: str) -> None: ...

    def log(self, prio: int, msg: str) -> None:
        """One journal line, "<prio>docker-gc: msg"."""
        ...

    def interactive(self) -> bool:
        """stdin is a terminal (someone can answer)."""
        ...

    def ask(self, question: str) -> bool:
        """A yes/no question; no answer (end of input) is no."""
        ...

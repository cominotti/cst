# SPDX-License-Identifier: MPL-2.0
"""Messages from a Type=notify service to systemd (sd_notify(3)): READY=1 once it has started, WATCHDOG=1 as its keep-alive.

systemd gives such a service the address of its notification socket in the
environment variable NOTIFY_SOCKET: a path, or an abstract socket name written
with a leading @. Each message is one datagram on an AF_UNIX socket. Without
the variable (the program started by hand, by a test, or by a unit of another
Type=) every send does nothing. A send that fails does not raise: the service
goes on, and one journal line (a warning) says so, until a send works again.
The socket does not block: when systemd's queue is full (PID 1 busy or stuck),
the send fails at once instead of stopping the service until PID 1 reads again.
With WatchdogSec= in the unit, systemd restarts the service when the
WATCHDOG=1 messages stop for that long.
"""

from collections.abc import Callable, Mapping
import socket
from typing import Protocol, Self

from cst_core.adapters.journal import log as journal_log


class _Datagrams(Protocol):
    """What the notifier needs of its socket (a fake in the tests)."""

    def sendto(self, data: bytes, address: str, /) -> int: ...

    def close(self) -> None: ...


def _datagram_socket() -> _Datagrams:
    return socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC | socket.SOCK_NONBLOCK)


def _address(value: str | None) -> str | None:
    """The socket address of a NOTIFY_SOCKET value: a path, or an abstract name (@name); None for anything else."""
    if not value:
        return None
    if value.startswith("@") and len(value) > 1:
        return "\0" + value[1:]
    return value if value.startswith("/") else None


class ServiceNotifier:
    """Sends sd_notify messages to the address it was given (None: sends nothing)."""

    def __init__(self, address: str | None, *, open_socket: Callable[[], _Datagrams] = _datagram_socket) -> None:
        self._address = address
        self._open_socket = open_socket
        self._sock: _Datagrams | None = None
        self._warned = False

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> Self:
        """The notifier of the socket NOTIFY_SOCKET names in environ (one that sends nothing without it)."""
        return cls(_address(environ.get("NOTIFY_SOCKET")))

    def send(self, state: str) -> bool:
        """Send state (such as "READY=1"); True when it was sent, False when there is no socket or the send failed."""
        if self._address is None:
            return False
        try:
            if self._sock is None:
                self._sock = self._open_socket()
            self._sock.sendto(state.encode("utf-8"), self._address)
        except OSError as e:
            # A full queue (EAGAIN) leaves the socket as it is; any other failure opens a new one next time.
            if self._sock is not None and not isinstance(e, BlockingIOError):
                self._sock.close()
                self._sock = None
            if not self._warned:
                self._warned = True
                journal_log(4, f"cannot notify systemd ({state.split('=', 1)[0]}): {e}")
            return False
        self._warned = False
        return True

    def ready(self) -> bool:
        """READY=1: the service has started (a Type=notify unit becomes active only now)."""
        return self.send("READY=1")

    def watchdog(self) -> bool:
        """WATCHDOG=1: the service is still working (resets systemd's WatchdogSec= timer)."""
        return self.send("WATCHDOG=1")

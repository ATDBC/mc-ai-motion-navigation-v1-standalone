"""One authenticated local process, bounded framing, and no replay after uncertain I/O."""
from __future__ import annotations

from dataclasses import dataclass
import math
import socket
import struct
import time
from typing import Callable

import psutil


MAX_INBOUND_FRAME_BYTES = 1_048_576


@dataclass(frozen=True, slots=True)
class ClientProcessIdentity:
    pid: int
    create_time: float

    def __post_init__(self) -> None:
        if (type(self.pid) is not int or self.pid <= 0
                or type(self.create_time) not in (int, float)
                or not math.isfinite(self.create_time) or self.create_time <= 0):
            raise ValueError("invalid registered client process identity")


class DeploymentTransport:
    """Controller-thread API. Parent supervision owns the client process and hard OS-call timeout."""

    def __init__(self, *, port: int = 0, clock_ns: Callable[[], int] = time.perf_counter_ns):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("invalid loopback port")
        self._clock_ns = clock_ns
        self._closed = False
        self._blocking_io_ns_total = 0
        self._peer: socket.socket | None = None
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            self._listener.bind(("127.0.0.1", port))
            self._listener.listen(1)
            self.port = self._listener.getsockname()[1]
        except BaseException:
            self._listener.close()
            self._closed = True
            raise

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def blocking_io_ns_total(self) -> int:
        """Elapsed time inside blocking socket calls, excluding local encoding."""
        return self._blocking_io_ns_total

    def _remaining(self, deadline_ns: int) -> float:
        if type(deadline_ns) is not int or deadline_ns <= 0:
            raise ValueError("deadline must be a positive exact integer")
        remaining = (deadline_ns - self._clock_ns()) / 1_000_000_000
        if remaining <= 0:
            raise TimeoutError("deployment I/O deadline exceeded")
        # A reset can legitimately wait for vanilla terrain-screen readiness for
        # over 30 seconds. The caller's absolute deadline bounds the whole frame;
        # individual socket operations must not silently shorten that deadline.
        return remaining

    def _open(self) -> None:
        if self._closed:
            raise RuntimeError("deployment transport closed; recreate it")

    def _seal(self, error: BaseException) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            error.add_note(f"transport cleanup failed: {type(cleanup).__name__}")

    def accept(self, identity: ClientProcessIdentity, deadline_ns: int) -> dict[str, object]:
        self._open()
        if type(identity) is not ClientProcessIdentity or self._peer is not None:
            raise ValueError("accept requires one registered client process")
        try:
            self._listener.settimeout(self._remaining(deadline_ns))
            io_started = self._clock_ns()
            try:
                self._peer, remote = self._listener.accept()
            finally:
                self._blocking_io_ns_total += max(
                    0, self._clock_ns() - io_started,
                )
            local = self._peer.getsockname()
            self._listener.close()
            if remote[0] != "127.0.0.1" or local != ("127.0.0.1", self.port):
                raise PermissionError("not the configured loopback connection")
            # Never infer ownership from a self-reported PID or a loopback address.
            process = psutil.Process(identity.pid)
            actual_create_time = process.create_time()
            if actual_create_time != identity.create_time:
                raise PermissionError("registered client identity changed")
            matches = [item for item in process.net_connections(kind="tcp4")
                       if tuple(item.laddr) == remote and tuple(item.raddr) == local
                       and item.status == psutil.CONN_ESTABLISHED]
            if (len(matches) != 1 or not process.is_running()
                    or psutil.Process(identity.pid).create_time() != actual_create_time):
                raise PermissionError("socket does not belong to registered client")
            self._remaining(deadline_ns)
            self._peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return {"pid": identity.pid, "create_time": actual_create_time,
                    "controller_address": local, "client_address": remote}
        except (psutil.AccessDenied, psutil.NoSuchProcess) as error:
            denied = PermissionError("registered client ownership is unavailable")
            self._seal(denied)
            raise denied from error
        except BaseException as error:
            self._seal(error)
            raise

    def send(self, payload: bytes, deadline_ns: int) -> None:
        self._open()
        if type(payload) is not bytes or not 0 < len(payload) <= 16384:
            raise ValueError("invalid outbound deployment frame")
        if self._peer is None:
            raise RuntimeError("deployment client is not authenticated")
        try:
            self._peer.settimeout(self._remaining(deadline_ns))
            io_started = self._clock_ns()
            try:
                self._peer.sendall(struct.pack(">I", len(payload)) + payload)
            finally:
                self._blocking_io_ns_total += max(
                    0, self._clock_ns() - io_started,
                )
            self._remaining(deadline_ns)
        except BaseException as error:
            self._seal(error)
            raise

    def _receive_exact(self, count: int, deadline_ns: int) -> bytes:
        result = bytearray()
        while len(result) < count:
            self._peer.settimeout(self._remaining(deadline_ns))
            io_started = self._clock_ns()
            try:
                chunk = self._peer.recv(count - len(result))
            finally:
                self._blocking_io_ns_total += max(
                    0, self._clock_ns() - io_started,
                )
            if not chunk:
                raise EOFError("deployment client closed a frame")
            result.extend(chunk)
        self._remaining(deadline_ns)
        return bytes(result)

    def receive(self, deadline_ns: int) -> bytes:
        self._open()
        if self._peer is None:
            raise RuntimeError("deployment client is not authenticated")
        try:
            size, = struct.unpack(">I", self._receive_exact(4, deadline_ns))
            if not 0 < size <= MAX_INBOUND_FRAME_BYTES:
                raise ValueError("invalid inbound deployment frame length")
            return self._receive_exact(size, deadline_ns)
        except BaseException as error:
            self._seal(error)
            raise

    def close(self) -> None:
        self._closed = True
        errors = []
        for handle in (self._peer, self._listener):
            if handle is not None:
                try:
                    handle.close()
                except OSError as error:
                    errors.append(error)
        if errors:
            raise ExceptionGroup("deployment transport cleanup failed", errors)

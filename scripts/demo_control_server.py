"""Authenticated single-owner local command channel. No Minecraft execution on the I/O thread."""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import socket
import threading
import time

import psutil

from mc2p.backends.deployment_transport import ClientProcessIdentity
from scripts.demo_control_protocol import DemoCommandV1, MAX_FRAME_BYTES, decode_command, decode_object


class DemoControlServer:
    def __init__(self, session_id: str, token: str, owner_pid: int | None = None,
                 owner_create_time: float | None = None):
        if type(session_id) is not str or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session_id) is None:
            raise ValueError('invalid control session identity')
        if type(token) is not str or re.fullmatch(r'[0-9a-f]{64}', token) is None:
            raise ValueError('invalid control credential')
        self.session_id, self._token = session_id, token
        if (owner_pid is None) != (owner_create_time is None):
            raise ValueError('owner identity must be complete or unbound')
        self._identity = None if owner_pid is None else ClientProcessIdentity(owner_pid, owner_create_time)
        self._commands: queue.Queue[DemoCommandV1] = queue.Queue(32)
        self._outgoing: queue.Queue[bytes] = queue.Queue(64)
        self._pending: set[int] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._owner_bound = threading.Event()
        if self._identity is not None: self._owner_bound.set()
        self._thread = self._listener = self._peer = None
        self.authenticated = False
        self.failure: str | None = None
        self.last_sequence = 0
        self._owner_deadline_ns = 0
        self.port = None

    def bind_owner(self, pid: int, create_time: float) -> None:
        """Supervisor-only one-time binding after launch, before connection authentication."""
        identity = ClientProcessIdentity(pid, create_time)
        with self._lock:
            if self._identity is not None or self._stop.is_set() or self.authenticated:
                raise ValueError('control owner cannot be rebound')
            if psutil.Process(pid).create_time() != identity.create_time:
                raise PermissionError('control owner identity changed')
            self._identity = identity
            self._owner_bound.set()

    @property
    def owner_deadline_ns(self) -> int:
        with self._lock:
            return 0 if self._stop.is_set() else self._owner_deadline_ns

    def start(self) -> int:
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                raise RuntimeError('control server cannot restart')
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                listener.bind(('127.0.0.1', 0))
                listener.listen(1)
                listener.settimeout(.2)
                self.port = listener.getsockname()[1]
                self._listener = listener
                self._thread = threading.Thread(target=self._run, name='mc2p-demo-control', daemon=True)
                self._thread.start()
                return self.port
            except BaseException:
                listener.close()
                raise

    def _verify_owner(self, remote, local) -> None:
        identity = self._identity
        if identity is None: raise PermissionError('control owner not bound')
        process = psutil.Process(identity.pid)
        if process.create_time() != identity.create_time or not process.is_running():
            raise PermissionError('control owner identity changed')
        client_matches = [c for c in process.net_connections(kind='tcp4')
                          if tuple(c.laddr) == remote and tuple(c.raddr) == local and c.status == psutil.CONN_ESTABLISHED]
        server_matches = [c for c in psutil.Process(os.getpid()).net_connections(kind='tcp4')
                          if tuple(c.laddr) == local and tuple(c.raddr) == remote and c.status == psutil.CONN_ESTABLISHED]
        if (remote[0] != '127.0.0.1' or local != ('127.0.0.1', self.port)
                or len(client_matches) != 1 or len(server_matches) != 1
                or psutil.Process(identity.pid).create_time() != identity.create_time):
            raise PermissionError('control socket ownership cannot be proven')

    def _enqueue_reply(self, sequence: int, phase: str, status: dict) -> None:
        payload = json.dumps({'schema_version': 'mc2p.demo-reply.v1', 'session_id': self.session_id,
                              'sequence': sequence, 'phase': phase, 'status': status},
                             allow_nan=False, separators=(',', ':')).encode()+b'\n'
        if len(payload) > MAX_FRAME_BYTES: raise ValueError('control reply too large')
        self._outgoing.put_nowait(payload)

    def reply(self, sequence: int, phase: str, status: dict) -> None:
        with self._lock:
            if (not self.authenticated or self._stop.is_set() or type(sequence) is not int
                    or sequence not in self._pending):
                raise ValueError('reply has no active pending command')
            if phase not in {'accepted', 'applied', 'rejected', 'failed'} or type(status) is not dict:
                raise ValueError('invalid applied command reply')
            if phase == 'accepted': return  # Already emitted atomically with enqueue; never duplicate it.
            try:
                self._enqueue_reply(sequence, phase, status)
            except queue.Full:
                self._seal('control_reply_queue_full')
                raise RuntimeError('control reply queue full') from None
            self._pending.remove(sequence)

    def poll(self) -> DemoCommandV1 | None:
        with self._lock:
            if not self.authenticated or self._stop.is_set(): return None
            try: return self._commands.get_nowait()
            except queue.Empty: return None

    def _seal(self, reason: str | None = None) -> None:
        with self._lock:
            if reason and self.failure is None: self.failure = reason
            self._stop.set()
            self.authenticated = False
            self._owner_deadline_ns = 0
            self._token = ''
            self._pending.clear()
            for pending in (self._commands, self._outgoing):
                while True:
                    try: pending.get_nowait()
                    except queue.Empty: break
            for handle in (self._peer, self._listener):
                if handle is not None:
                    try: handle.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    handle.close()

    def _run(self) -> None:
        buffer = bytearray()
        try:
            startup_deadline = time.perf_counter_ns()+150_000_000_000
            while not self._stop.is_set():
                if time.perf_counter_ns() >= startup_deadline: raise TimeoutError('control startup expired')
                try:
                    peer, remote = self._listener.accept()
                    break
                except socket.timeout:
                    if self._identity is not None and psutil.Process(self._identity.pid).create_time() != self._identity.create_time:
                        raise PermissionError('control owner identity changed')
            else: return
            self._peer = peer
            self._listener.close()
            handshake_deadline = time.perf_counter_ns()+3_000_000_000
            while not self._owner_bound.is_set():
                if self._stop.is_set(): return
                remaining = (handshake_deadline-time.perf_counter_ns())/1_000_000_000
                if remaining <= 0: raise TimeoutError('control owner binding expired')
                self._owner_bound.wait(min(.1, remaining))
            self._verify_owner(remote, peer.getsockname())
            peer.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer.settimeout(.1)
            while not self._stop.is_set():
                deadline = self.owner_deadline_ns if self.authenticated else handshake_deadline
                if time.perf_counter_ns() >= deadline:
                    raise TimeoutError('control authorization expired')
                # Only this thread performs I/O. GUI/Runtime reply callbacks merely enqueue.
                for _ in range(64):
                    if self._stop.is_set(): return
                    try: output = self._outgoing.get_nowait()
                    except queue.Empty: break
                    remaining = (deadline-time.perf_counter_ns())/1_000_000_000
                    if remaining <= 0: raise TimeoutError('control reply authorization expired')
                    peer.settimeout(min(1.0, remaining))
                    peer.sendall(output)
                peer.settimeout(.1)
                try: chunk = peer.recv(min(4096, MAX_FRAME_BYTES+1-len(buffer)))
                except socket.timeout: continue
                if not chunk: raise EOFError('control connection closed')
                buffer.extend(chunk)
                while b'\n' in buffer:
                    line, _, remainder = buffer.partition(b'\n')
                    buffer = bytearray(remainder)
                    if len(line)+1 > MAX_FRAME_BYTES: raise ValueError('control frame too large')
                    if time.perf_counter_ns() >= deadline: raise TimeoutError('late control frame')
                    if not self.authenticated:
                        handshake = decode_object(bytes(line))
                        if (set(handshake) != {'schema_version', 'session_id', 'token'}
                                or handshake['schema_version'] != 'mc2p.demo-handshake.v1'
                                or handshake['session_id'] != self.session_id
                                or type(handshake['token']) is not str
                                or re.fullmatch(r'[0-9a-f]{64}', handshake['token']) is None
                                or not hmac.compare_digest(handshake['token'], self._token)):
                            raise PermissionError('control authentication rejected')
                        with self._lock:
                            self.authenticated = True
                            self._token = ''
                            self._owner_deadline_ns = time.perf_counter_ns()+3_000_000_000
                        self._outgoing.put_nowait(json.dumps({'schema_version': 'mc2p.demo-ready.v1',
                                                             'session_id': self.session_id}).encode()+b'\n')
                        deadline = self.owner_deadline_ns
                        continue
                    command = decode_command(bytes(line))
                    with self._lock:
                        if command.sequence <= self.last_sequence:
                            raise ValueError('old or duplicate control sequence')
                        if command.kind == 'heartbeat':
                            self._owner_deadline_ns = time.perf_counter_ns()+3_000_000_000
                            deadline = self._owner_deadline_ns
                        else:
                            if len(self._pending) >= 32: raise queue.Full()
                            self._commands.put_nowait(command)
                            self._pending.add(command.sequence)
                            self._enqueue_reply(command.sequence, 'accepted', {})
                        self.last_sequence = command.sequence
                if len(buffer) >= MAX_FRAME_BYTES:
                    raise ValueError('control frame exceeds bounded buffer')
        except (OSError, ValueError, TypeError, EOFError, queue.Full, psutil.Error) as error:
            # Never expose handshake contents/credentials in logs or user status.
            if not self._stop.is_set(): self._seal('control_' + type(error).__name__)
        finally:
            self._seal()

    def close(self) -> None:
        self._seal()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
            if self._thread.is_alive(): raise TimeoutError('control thread did not stop')

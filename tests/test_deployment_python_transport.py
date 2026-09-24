from contextlib import contextmanager
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import psutil


ROOT = Path(__file__).resolve().parents[1]


def deadline(seconds=2):
    return time.perf_counter_ns() + round(seconds * 1_000_000_000)


class DeploymentPythonTransportTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "mc2p/backends/deployment_transport.py").is_file(),
                        "Python deployment transport is missing")
        from mc2p.backends.deployment_transport import DeploymentTransport, ClientProcessIdentity
        self.Transport = DeploymentTransport
        self.Identity = ClientProcessIdentity
        self.identity = ClientProcessIdentity(os.getpid(), psutil.Process().create_time())

    @contextmanager
    def connected(self):
        transport = self.Transport()
        peer = socket.create_connection(("127.0.0.1", transport.port), timeout=2)
        try:
            proof = transport.accept(self.identity, deadline())
            self.assertEqual(proof["pid"], os.getpid())
            self.assertEqual(proof["create_time"], self.identity.create_time)
            yield transport, peer
        finally:
            peer.close()
            transport.close()

    def test_real_framed_io_and_idempotent_close_release_port(self):
        with self.connected() as (transport, peer):
            port = transport.port
            before = transport.blocking_io_ns_total
            transport.send(b"abc", deadline())
            self.assertEqual(peer.recv(7), b"\0\0\0\3abc")
            peer.sendall(b"\0\0\0\2ok")
            self.assertEqual(transport.receive(deadline()), b"ok")
            self.assertGreaterEqual(transport.blocking_io_ns_total, before)
            transport.close()
            transport.close()
            self.assertTrue(transport.closed)
        with socket.socket() as rebound:
            rebound.bind(("127.0.0.1", port))

    def test_actual_child_pid_owns_the_accepted_socket(self):
        code = ("import socket,sys; s=socket.create_connection(('127.0.0.1',int(sys.argv[1]))); "
                "s.settimeout(3); s.sendall(b'\\0\\0\\0\\2ok'); "
                "print(s.recv(32).hex()); s.close()")
        transport = self.Transport()
        child = subprocess.Popen([sys.executable, "-c", code, str(transport.port)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            identity = self.Identity(child.pid, psutil.Process(child.pid).create_time())
            proof = transport.accept(identity, deadline())
            self.assertEqual(proof["pid"], child.pid)
            self.assertNotEqual(proof["pid"], os.getpid())
            self.assertEqual(transport.receive(deadline()), b"ok")
            transport.send(b"A", deadline())
            out, err = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 0, err)
            self.assertEqual(out.strip(), "0000000141")
        finally:
            transport.close()
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=3)

    def test_wrong_process_identity_closes_before_disclosing_any_bytes(self):
        for wrong in (self.Identity(os.getpid(), self.identity.create_time - 1),
                      self.Identity(os.getpid(), self.identity.create_time + .005),
                      self.Identity(os.getppid(), psutil.Process(os.getppid()).create_time())):
            with self.subTest(wrong=wrong):
                transport = self.Transport()
                with socket.create_connection(("127.0.0.1", transport.port), timeout=2) as peer:
                    try:
                        with self.assertRaises(PermissionError):
                            transport.accept(wrong, deadline())
                        self.assertTrue(transport.closed)
                        self.assertEqual(peer.recv(1), b"")
                    finally:
                        transport.close()

    def test_partial_reads_use_one_total_deadline(self):
        with self.connected() as (transport, peer):
            stop = threading.Event()
            errors = []
            def trickle():
                try:
                    peer.sendall(struct.pack(">I", 1000))
                    while not stop.wait(0.025):
                        peer.sendall(b"A")
                except OSError:
                    pass
                except BaseException as error:
                    errors.append(error)
            writer = threading.Thread(target=trickle)
            writer.start()
            try:
                with self.assertRaises(TimeoutError):
                    transport.receive(deadline(0.15))
                self.assertTrue(transport.closed)
                with self.assertRaises(RuntimeError):
                    transport.receive(deadline())
            finally:
                stop.set()
                writer.join(2)
            self.assertFalse(writer.is_alive())
            self.assertEqual(errors, [])

    def test_initial_sample_can_arrive_after_30_seconds_within_requested_deadline(self):
        # Hidden vanilla startup can spend 30 seconds in DownloadingTerrainScreen.
        # A per-recv cap must not silently shorten the caller's absolute reset deadline.
        with self.connected() as (transport, peer):
            errors = []
            def send_ready():
                try:
                    peer.sendall(b"\0\0\0\2ok")
                except BaseException as error:
                    errors.append(error)
            timer = threading.Timer(30.25, send_ready)
            timer.start()
            try:
                try:
                    result = transport.receive(deadline(35))
                except TimeoutError:
                    self.fail("initial observation was cut off before the requested deadline")
                self.assertEqual(result, b"ok")
                self.assertFalse(transport.closed)
            finally:
                timer.cancel()
                timer.join(2)
            self.assertFalse(timer.is_alive())
            self.assertEqual(errors, [])

    def test_rejects_oversize_zero_length_and_partial_eof(self):
        for payload in (b"\0\0\0\0", struct.pack(">I", 1_048_577), b"\xff" * 4,
                        b"\0\0", b"\0\0\0\4A"):
            with self.subTest(payload=payload), self.connected() as (transport, peer):
                peer.sendall(payload)
                peer.shutdown(socket.SHUT_WR)
                with self.assertRaises((ValueError, EOFError)):
                    transport.receive(deadline())
                self.assertTrue(transport.closed)

    def test_expired_accept_and_send_are_sealed_without_io(self):
        transport = self.Transport()
        try:
            with self.assertRaises(TimeoutError):
                transport.accept(self.identity, time.perf_counter_ns() - 1)
            self.assertTrue(transport.closed)
        finally:
            transport.close()
        with self.connected() as (transport, peer):
            with self.assertRaises(TimeoutError):
                transport.send(b"secret", time.perf_counter_ns() - 1)
            self.assertTrue(transport.closed)
            self.assertEqual(peer.recv(1), b"")

    def test_invalid_frame_and_configuration_never_coerce(self):
        for port in (-1, 65536, True, "8129"):
            with self.assertRaises(ValueError):
                self.Transport(port=port)
        with self.connected() as (transport, peer):
            for value in (b"", b"x" * 16385, "string", bytearray(b"a")):
                with self.subTest(value_type=type(value)), self.assertRaises(ValueError):
                    transport.send(value, deadline())
            self.assertFalse(transport.closed)
            transport.send(b"a", deadline())
            self.assertEqual(peer.recv(5), b"\0\0\0\1a")

    def test_unavailable_os_ownership_fails_closed_before_credentials(self):
        transport = self.Transport()
        with socket.create_connection(("127.0.0.1", transport.port), timeout=2) as peer:
            try:
                with patch.object(psutil.Process, "net_connections", side_effect=psutil.AccessDenied()):
                    with self.assertRaises(PermissionError):
                        transport.accept(self.identity, deadline())
                self.assertTrue(transport.closed)
                self.assertEqual(peer.recv(1), b"")
            finally:
                transport.close()

    def test_actual_backpressure_write_timeout_seals_connection(self):
        with self.connected() as (transport, peer):
            peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            writer = transport._peer
            writer.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            writer.setblocking(False)
            total = 0
            limit = deadline()
            while total < 16 * 1024 * 1024 and time.perf_counter_ns() < limit:
                try:
                    total += writer.send(b"x" * 65536)
                except BlockingIOError:
                    break
            else:
                self.fail("fixture could not establish socket backpressure")
            with self.assertRaises(TimeoutError):
                transport.send(b"A" * 16384, deadline(.05))
            self.assertTrue(transport.closed)

"""Literal protocol fixtures, never claimed as Minecraft integration evidence."""
from contextlib import contextmanager
import json
import os
import socket
import struct
import threading
import time

import psutil
from tests.observation_v2_fixtures import valid_payload_value
from tests.test_action_receipt import receipt_value
from mc2p.contracts.observation_request_v3 import OBSERVATION_V2


TOKEN = "0123456789abcdef" * 4


def sample_value(sequence=0, *, tick=100, status=None):
    payload = valid_payload_value(generation_id=sequence, sample_world_tick=tick)
    payload["client_sample"].update(started_at_monotonic_ns=9000 + sequence * 100,
                                    completed_at_monotonic_ns=9010 + sequence * 100)
    receipt = receipt_value(generation_id=sequence, episode_id="ep" if sequence else None,
                            request_sequence_id=sequence - 1 if sequence else None, world_tick=tick)
    receipt.update(status=status or ("executed" if sequence else "idle"),
                   reason="neutral" if sequence else "none", on_client_thread=bool(sequence),
                   execution_thread="Render thread" if sequence else "none",
                   input_samples=sequence, leased_input_samples=sequence)
    diagnostics = dict(schema_version="mc2p.deployment_diagnostics.v1", client_tick=100 + 3 * sequence,
                       remote_address="127.0.0.1:25599", has_integrated_server=False,
                       window_recorded=True, window_visible_at_creation=False, window_visible=False,
                       world_render_attempts=sequence, world_render_completions=0,
                       gui_render_attempts=0, gui_render_completions=0,
                       framebuffer_capture_attempts=0, image_encode_attempts=0)
    return dict(schema_version="mc2p.deployment_sample.v1", episode_id="ep",
                observation=payload, receipt=receipt, diagnostics=diagnostics)


def encoded(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode()


def recv_exact(peer, count):
    result = b""
    while len(result) < count:
        part = peer.recv(count - len(result))
        if not part:
            raise EOFError()
        result += part
    return result


@contextmanager
def backend_peer(samples, *, delay=None, clock_ns=time.perf_counter_ns,
                 observation_schema_version=OBSERVATION_V2, **backend_options):
    """Loopback fixture; legacy V2 must now be selected explicitly here."""
    from mc2p.backends.deployment_transport import ClientProcessIdentity, DeploymentTransport
    from mc2p.backends.fabric_behavior import FabricBehaviorBackendV1
    transport = DeploymentTransport(clock_ns=clock_ns)
    peer = socket.create_connection(("127.0.0.1", transport.port), timeout=3)
    try:
        backend = FabricBehaviorBackendV1(transport=transport,
            client_identity=ClientProcessIdentity(os.getpid(), psutil.Process().create_time()),
            token=TOKEN, server_port=25599, clock_ns=clock_ns,
            observation_schema_version=observation_schema_version, **backend_options)
    except BaseException:
        peer.close()
        transport.close()
        raise
    requests, errors = [], []
    def serve():
        try:
            for index, sample in enumerate(samples):
                size, = struct.unpack(">I", recv_exact(peer, 4))
                if not 0 < size <= 16384:
                    raise AssertionError("unbounded formal request")
                requests.append(json.loads(recv_exact(peer, size)))
                if delay is not None:
                    delay(index)
                payload = sample if type(sample) is bytes else encoded(sample)
                peer.sendall(struct.pack(">I", len(payload)) + payload)
            # Any unexpected replay remains visible to the test after close.
            while True:
                size, = struct.unpack(">I", recv_exact(peer, 4))
                requests.append(json.loads(recv_exact(peer, size)))
        except (EOFError, ConnectionError):
            pass
        except BaseException as error:
            errors.append(error)
        finally:
            peer.close()
    worker = threading.Thread(target=serve)
    worker.start()
    try:
        yield backend, transport, requests
    finally:
        backend.close()
        worker.join(4)
        if worker.is_alive():
            peer.close()
            raise AssertionError("fixture protocol thread survived close")
        if errors:
            raise ExceptionGroup("fixture peer failed", errors)

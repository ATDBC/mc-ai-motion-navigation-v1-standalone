"""Formal Player Runtime backend for one independent, parent-supervised Fabric client."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
import re
import time
from typing import Callable
import uuid

from mc2p.backends.client_behavior_payload import encode_behavior_action
from mc2p.backends.client_observation_payload import (
    ClientObservationPayloadError, decode_client_observation_payload, snapshot_v2_from_payload,
)
from mc2p.backends.deployment_transport import ClientProcessIdentity, DeploymentTransport
from mc2p.backends.client_observation_payload_v3 import (
    decode_client_observation_value_v3,
    require_formal_surface_perception,
    snapshot_v3_from_payload,
)
from mc2p.contracts.observation_request_v3 import (
    OBSERVATION_V2, OBSERVATION_V3, ObservationRequestV3, validate_observation_schema, resolve_observation_request,
)
from mc2p.contracts.action_v1 import ActionSnapshotV1
from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.report import FailureCodeV0, FailureV0
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.runtime.backend_v1 import BackendStepResultV1


_DIAGNOSTIC_COUNTERS = {"client_tick", "world_render_attempts", "world_render_completions",
    "gui_render_attempts", "gui_render_completions", "framebuffer_capture_attempts", "image_encode_attempts"}
_DIAGNOSTIC_FLAGS = {"has_integrated_server", "window_recorded", "window_visible_at_creation", "window_visible"}


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _strict_sample(payload: bytes, *, schema="mc2p.deployment_sample.v1") -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ContractViolation("duplicate deployment JSON key")
            result[key] = value
        return result
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"), object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ContractViolation("invalid deployment JSON") from error
    if (type(value) is not dict or set(value) != {"schema_version", "episode_id", "observation", "receipt", "diagnostics"}
            or value["schema_version"] != schema):
        raise ContractViolation("invalid deployment sample schema/fields")
    return value


class FabricBehaviorBackendV1:
    """Never launches/resets a server world. Parent owns exact client-process termination."""

    action_schema_version = "mc2p.action-snapshot.v1"

    def __init__(self, *, transport: DeploymentTransport, client_identity: ClientProcessIdentity,
                 token: str, server_port: int, clock_ns: Callable[[], int] = time.perf_counter_ns,
                 observation_schema_version: str = OBSERVATION_V3):
        self._observation_schema_version = validate_observation_schema(observation_schema_version)
        if (type(transport) is not DeploymentTransport or type(client_identity) is not ClientProcessIdentity
                or type(token) is not str or re.fullmatch("[0-9a-f]{64}", token) is None
                or type(server_port) is not int or not 1 <= server_port <= 65535):
            raise ContractViolation("invalid independent client backend configuration")
        self._transport, self._identity, self._token = transport, client_identity, token
        self._server_port, self._clock_ns = server_port, clock_ns
        self._controller_clock_id = "controller-" + uuid.uuid4().hex
        self._closed = self._used = False
        self._episode = None
        self._sequence = 0
        self._last_request = -1
        self._observation = None
        self._diagnostics = self._receipt = self._peer_proof = None

    @property
    def observation_schema_version(self) -> str:
        return self._observation_schema_version

    @property
    def last_diagnostics(self) -> dict | None:
        return deepcopy(self._diagnostics)

    @property
    def last_behavior_receipt(self) -> dict | None:
        return deepcopy(self._receipt)

    @property
    def peer_identity_proof(self) -> dict | None:
        return deepcopy(self._peer_proof)

    @property
    def blocking_io_ns_total(self) -> int:
        """Cumulative socket-blocking time for precise controller attribution."""
        return self._transport.blocking_io_ns_total

    def _open(self) -> None:
        if self._closed or self._transport.closed:
            raise ContractViolation("Fabric backend is closed; recreate the client and backend")

    def _seal(self, error: BaseException) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            error.add_note(f"Fabric cleanup failed: {type(cleanup).__name__}")

    def _reset_failure(self, request, code, message) -> ResetResultV0:
        cleanup_failures = []
        try:
            self.close()
        except Exception as cleanup:
            # Keep the primary classification; never echo credentials or remote data.
            cleanup_failures.append(type(cleanup).__name__)
        return ResetResultV0(request.request_id, request.episode_id, False,
            failure=FailureV0(code, message, False, "fabric_backend",
                json.dumps({"cleanup_failures": cleanup_failures})))

    def reset(self, request: ResetRequestV0) -> ResetResultV0:
        self._open()
        if type(request) is not ResetRequestV0:
            raise ContractViolation("Fabric reset requires ResetRequestV0")
        if self._used:
            return self._reset_failure(request, FailureCodeV0.CONFIGURATION,
                "remote session cannot reset a live world; recreate the client and backend")
        if (request.scenario_id != "remote-session" or request.seed != 0
                or json.loads(request.backend_options_json) != {}
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", request.episode_id) is None):
            return self._reset_failure(request, FailureCodeV0.CONFIGURATION,
                "only remote-session seed=0 without world options is supported")
        self._used = True
        started = self._clock_ns()
        try:
            self._peer_proof = self._transport.accept(self._identity, request.deadline_monotonic_ns)
            session_value = dict(schema_version="mc2p.deployment_session.v1",
                                 episode_id=request.episode_id, token=self._token)
            if self.observation_schema_version == OBSERVATION_V3:
                session_value.update(schema_version="mc2p.deployment_session.v2",
                                     observation_schema_version=self.observation_schema_version)
            session = _json_bytes(session_value)
            self._token = None
            self._transport.send(session, request.deadline_monotonic_ns)
            observation, _ = self._read_sample(request.episode_id, 0, None, started, request.deadline_monotonic_ns,
                resolve_observation_request(self.observation_schema_version, None))
            self._episode, self._observation = request.episode_id, observation
            return ResetResultV0(request.request_id, request.episode_id, True, observation=observation)
        except TimeoutError:
            return self._reset_failure(request, FailureCodeV0.DEADLINE_EXCEEDED, "Fabric session reset deadline exceeded")
        except (ContractViolation, ValueError, RecursionError):
            return self._reset_failure(request, FailureCodeV0.OBSERVATION_INVARIANT, "Fabric reset sample was rejected")
        except (OSError, EOFError):
            return self._reset_failure(request, FailureCodeV0.BACKEND_IO, "Fabric session connection failed")
        except BaseException as error:
            self._seal(error)
            raise

    def _read_sample(self, episode, sequence, request_sequence, started, deadline_ns, observation_request=None):
        v3 = self.observation_schema_version == OBSERVATION_V3
        frame = self._transport.receive(deadline_ns)
        envelope = _strict_sample(frame,
                                 schema="mc2p.deployment_sample.v2" if v3 else "mc2p.deployment_sample.v1")
        if envelope["episode_id"] != episode:
            raise ContractViolation("deployment episode mismatch")
        # The transport bounds the complete envelope to 1 MiB, which is
        # stricter than separately serializing and bounding its observation
        # member. Avoid a second encoding of every V3 observation here.
        payload = None if v3 else _json_bytes(envelope["observation"])
        try:
            decoded = (decode_client_observation_value_v3(envelope["observation"])
                       if v3 else decode_client_observation_payload(payload, expected_generation_id=sequence))
        except ClientObservationPayloadError as error:
            raise ContractViolation("deployment observation violated the declared contract") from error
        if decoded.generation_id != sequence:
            raise ContractViolation("deployment observation generation mismatch")
        if v3:
            require_formal_surface_perception(decoded)
        if v3 and decoded.field_profile != observation_request.field_profile:
            raise ContractViolation("observation_profile_mismatch")
        if v3:
            requested_entity = observation_request.entity_track_id
            tracked_entity = decoded.tracked_entity
            if requested_entity is None:
                if tracked_entity.value is not None or tracked_entity.reason_code != "not_requested":
                    raise ContractViolation("observation_entity_query_mismatch")
            elif tracked_entity.value is not None:
                if tracked_entity.value.track_id != requested_entity:
                    raise ContractViolation("observation_entity_query_mismatch")
            elif tracked_entity.reason_code == "not_requested":
                raise ContractViolation("observation_entity_query_mismatch")
        receipt = envelope["receipt"]
        immutable_receipt = behavior_receipt_from_mapping(receipt)
        self._validate_diagnostics(envelope["diagnostics"])
        if (receipt["generation_id"] != sequence or receipt["world_tick"] != decoded.sample_world_tick
                or receipt["request_sequence_id"] != request_sequence
                or receipt["episode_id"] != (episode if sequence else None)
                or receipt["action_keyboard_callbacks"] != 0 or receipt["action_mouse_callbacks"] != 0
                or receipt["handled_screen_render_completions"] != 0):
            raise ContractViolation("deployment receipt does not match observation/request")
        if sequence == 0:
            if receipt["status"] != "idle" or receipt["input_samples"] != 0:
                raise ContractViolation("deployment reset has stale action state")
        elif receipt["on_client_thread"] is not True or receipt["status"] == "idle":
            raise ContractViolation("deployment action lacks client-thread receipt")
        if self._observation is not None:
            previous = self._observation.client_sample
            if (decoded.client_sample.clock_id != previous.clock_id
                    or decoded.client_sample.started_at_monotonic_ns < previous.completed_at_monotonic_ns):
                raise ContractViolation("deployment client clock changed or regressed")
        received = self._clock_ns()
        if received >= deadline_ns:
            raise TimeoutError("deployment sample validation exceeded deadline")
        projector = snapshot_v3_from_payload if v3 else snapshot_v2_from_payload
        observation = projector(decoded, episode_id=episode, request_sequence_id=request_sequence,
            request_started_at_monotonic_ns=started, received_at_monotonic_ns=received,
            controller_clock_id=self._controller_clock_id, source_backend="fabric")
        if self._clock_ns() >= deadline_ns:
            raise TimeoutError("deployment snapshot construction exceeded deadline")
        self._diagnostics, self._receipt = envelope["diagnostics"], receipt
        return observation, immutable_receipt

    def _validate_diagnostics(self, value):
        if (type(value) is not dict or set(value) != _DIAGNOSTIC_COUNTERS | _DIAGNOSTIC_FLAGS | {"schema_version", "remote_address"}
                or value["schema_version"] != "mc2p.deployment_diagnostics.v1"
                or value["remote_address"] != f"127.0.0.1:{self._server_port}"):
            raise ContractViolation("invalid deployment diagnostics")
        for key in _DIAGNOSTIC_COUNTERS:
            require_nonnegative_int(value[key], key)
        for key in _DIAGNOSTIC_FLAGS:
            if type(value[key]) is not bool:
                raise ContractViolation("non-boolean deployment flag")
        if (value["window_recorded"] is not True or value["has_integrated_server"]
                or value["window_visible_at_creation"] or value["window_visible"]
                or any(value[name] != 0 for name in ("framebuffer_capture_attempts", "image_encode_attempts",
                                                    "world_render_completions", "gui_render_completions"))):
            raise ContractViolation("deployment zero-image/independent-client guard failed")
        if self._diagnostics is not None:
            if (value["client_tick"] <= self._diagnostics["client_tick"]
                    or any(value[name] < self._diagnostics[name] for name in _DIAGNOSTIC_COUNTERS)):
                raise ContractViolation("deployment diagnostic counters regressed")

    def step(self, action: ActionSnapshotV1, deadline_monotonic_ns: int, *,
             observation_request: ObservationRequestV3 | None = None) -> BackendStepResultV1:
        self._open()
        request = resolve_observation_request(self.observation_schema_version, observation_request)
        if (type(action) is not ActionSnapshotV1 or self._observation is None
                or action.episode_id != self._episode or action.observation_sequence_id != self._sequence
                or action.request_sequence_id <= self._last_request):
            raise ContractViolation("formal Fabric request is invalid, stale or duplicated")
        require_nonnegative_int(deadline_monotonic_ns, "step deadline")
        limit = min(deadline_monotonic_ns, action.deadline_monotonic_ns)
        started = self._clock_ns()
        self._receipt = None
        try:
            payload = encode_behavior_action(replace(action, deadline_monotonic_ns=limit), now_ns=started,
                observation_received_at_ns=self._observation.received_at_monotonic_ns)
            if request is not None:
                payload = _json_bytes(dict(schema_version="mc2p.client_step.v3", action=json.loads(payload),
                                           observation_request=asdict(request)))
                if len(payload) > 16384:
                    raise ContractViolation("deployment step exceeds size bound")
            self._transport.send(payload, limit)
            observation, receipt = self._read_sample(self._episode, self._sequence + 1,
                action.request_sequence_id, started, limit, request)
            self._sequence += 1
            self._last_request = action.request_sequence_id
            self._observation = observation
            return BackendStepResultV1(observation, 0.0, False, False, receipt)
        except BaseException as error:
            self._seal(error)
            raise

    def close(self) -> None:
        self._closed = True
        self._token = None
        self._receipt = self._diagnostics = None
        self._transport.close()

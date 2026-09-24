"""Formal Action V1 adapter; V0 key steps remain only in the explicit legacy backend."""
from __future__ import annotations

from dataclasses import replace
from typing import Any

from mc2p.backends.craftground import (
    CraftGroundBackendV0, observation_from_craftground, remaining_timeout_seconds,
)
from mc2p.backends.client_behavior_payload import behavior_action_message, decode_behavior_receipt
from mc2p.backends.client_observation_payload import extract_length_delimited_field
from mc2p.backends.craftground_runtime import load_sandbox_manifest
from mc2p.backends.craftground_transport_epoch import bind_transport_epoch, decode_transport_epoch
from mc2p.contracts.action_v1 import ActionSnapshotV1
from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.report import FailureCodeV0
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.runtime.backend_v1 import BackendStepResultV1
from mc2p.contracts.observation_request_v3 import OBSERVATION_V3, ObservationRequestV3, resolve_observation_request


class CraftGroundBehaviorBackendV1(CraftGroundBackendV0):
    """Reuse lifecycle and V2 collection only, never the legacy action converter."""

    action_schema_version = "mc2p.action-snapshot.v1"

    last_behavior_receipt: dict | None = None
    _requires_recreation: bool = False
    _observation_received_at_ns: int | None = None
    _world_identity: tuple[str, int] | None = None

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._transport_epoch: str | None = None
        self._last_client_sample = None
        self._uses_transport_epoch = self._runtime_env_path is not None and bool(
            {"install nonblocking reference scheduler v1", "install nonblocking lockstep scheduler v1"}
            .intersection(load_sandbox_manifest(self._runtime_env_path).patch_operations))

    def _reset_environment(self, request: ResetRequestV0):
        if not self._uses_transport_epoch:
            return super()._reset_environment(request)
        from craftground.proto.action_space_pb2 import ActionSpaceMessageV2

        previous = self._transport_epoch
        self._requires_recreation = True
        if previous is None:
            observation, info = super()._reset_environment(request)
        else:
            if self._env.queued_commands:
                raise ContractViolation("formal reset cannot carry queued commands")
            message = bind_transport_epoch(ActionSpaceMessageV2(commands=["fastreset "]), previous)
            self._env.ipc.send_action(message, [])
            raw = self._env.ipc.read_observation()
            observation = self._env.convert_observation_v2(raw)
            info = observation
        current = decode_transport_epoch(observation["full"].SerializeToString())
        if previous is None:
            if current.rsplit("/", 1)[1] != "1":
                raise ContractViolation("initial reset has a stale transport epoch")
        else:
            client, counter = previous.rsplit("/", 1)
            if current != f"{client}/{int(counter) + 1}":
                raise ContractViolation("reset did not advance the same client's transport epoch exactly once")
        self._transport_epoch = current
        return observation, info

    def _close_environment(self, env: Any) -> None:
        if not self._uses_transport_epoch:
            return super()._close_environment(env)
        from craftground.proto.action_space_pb2 import ActionSpaceMessageV2

        sock = getattr(env.ipc, "sock", None)
        process = getattr(env, "process", None)
        can_send = not self._requires_recreation and self._transport_epoch is not None
        self._requires_recreation = True
        failures: list[Exception] = []
        try:
            if sock is not None and can_send:
                sock.settimeout(1.0)
                env.ipc.send_action(bind_transport_epoch(
                    ActionSpaceMessageV2(commands=["exit"]), self._transport_epoch), [])
                # Keep the peer alive until its client-thread stop closes the transport. Sending
                # exit then immediately closing would race the worker's fail-on-EOF boundary.
                if sock.recv(1) != b"":
                    raise ContractViolation("unexpected observation during formal close")
                # EOF only means the worker closed its socket. Vanilla disconnect/save and the
                # Gradle launcher can still be running; give that owned process a bounded grace.
                if process is not None:
                    exit_code = process.wait(timeout=10.0)
                    if exit_code != 0:
                        raise RuntimeError(f"normal game exit returned {exit_code}")
        except Exception as error:
            failures.append(error)
        finally:
            env.ipc.sock = None  # Upstream destroy must never send another, unbound exit.
            if sock is not None:
                try:
                    sock.close()
                except Exception as error:
                    failures.append(error)
            try:
                # Upstream terminate sends a signal before checking liveness. Do not hand it an
                # exited Popen/PID; the outer backend retained the handle for final verification.
                if process is not None and process.poll() is not None:
                    env.process = None
                super()._close_environment(env)
            except Exception as error:
                failures.append(error)
        if failures:
            raise ExceptionGroup("formal client cleanup failed: " + "; ".join(map(str, failures)), failures)

    @property
    def supported_scenarios(self) -> frozenset[str]:
        return super().supported_scenarios | {"flat-bonus-chest", "flat-furnace-chest"}

    def _configure_initial_environment(self, initial: Any, scenario_id: str) -> None:
        super()._configure_initial_environment(initial, scenario_id)
        initial.bonus_chest = scenario_id in {"flat-bonus-chest", "flat-furnace-chest"}
        # Normal new-world superflat preset, solely an isolated functional fixture.
        # Dense furnace terrain is NOT a representative performance/timing fixture.
        initial.world_type_args = ("minecraft:bedrock,2*minecraft:dirt,minecraft:furnace;minecraft:plains"
                                   if scenario_id == "flat-furnace-chest" else "")

    def reset(self, request: ResetRequestV0) -> ResetResultV0:
        self.last_behavior_receipt = None
        if self._requires_recreation:
            return self._reset_failure(request, FailureCodeV0.BACKEND_START,
                "formal IPC may be desynchronized; close and recreate the backend before reset",
                retryable=False)
        identity = (request.scenario_id, request.seed)
        if self._env is not None and self._world_identity is not None and (
                identity != self._world_identity or request.scenario_id in {"flat-bonus-chest", "flat-furnace-chest"}):
            return self._reset_failure(request, FailureCodeV0.CONFIGURATION,
                "close and recreate for a new seed/scenario or a fresh bonus chest; fast reset preserves the world",
                retryable=False)
        result = super().reset(request)
        if result.succeeded and result.observation is not None:
            self._world_identity = identity
            self._observation_received_at_ns = result.observation.received_at_monotonic_ns
            self._last_client_sample = result.observation.client_sample
            self._requires_recreation = False
        if not result.succeeded and self._env is not None:
            self._requires_recreation = True
        return result

    @property
    def environment_guard_commands(self) -> tuple[str, ...]:
        # A declared survival fixture exercises the ordinary personal InventoryScreen.
        return tuple("gamemode survival" if command == "gamemode creative" else command
                     for command in super().environment_guard_commands)

    def _validate_reset_snapshot(self, snapshot) -> None:
        if self.observation_schema_version == OBSERVATION_V3 and self._last_client_sample is not None:
            previous, current = self._last_client_sample, snapshot.client_sample
            if current.clock_id != previous.clock_id or current.started_at_monotonic_ns < previous.completed_at_monotonic_ns:
                raise ContractViolation("CraftGround reset client clock changed or regressed")

    def step(self, action: ActionSnapshotV1, deadline_monotonic_ns: int, *,
             observation_request: ObservationRequestV3 | None = None) -> BackendStepResultV1:
        request = resolve_observation_request(self.observation_schema_version, observation_request)
        if type(action) is not ActionSnapshotV1:
            raise ContractViolation("formal backend requires ActionSnapshotV1; key fallback is disabled")
        if self._requires_recreation:
            raise ContractViolation("formal IPC may be desynchronized; close and recreate the backend")
        if self._closed or self._env is None or self._episode_id is None:
            raise ContractViolation("formal backend must be reset and open")
        if action.episode_id != self._episode_id or action.observation_sequence_id != self._sequence_id:
            raise ContractViolation("formal request is based on a stale episode or observation")
        if self._env.queued_commands:
            raise ContractViolation("formal action channel cannot carry queued commands")
        deadline = min(deadline_monotonic_ns, action.deadline_monotonic_ns)
        started = self._clock_ns()
        self._set_socket_timeout(remaining_timeout_seconds(deadline, started))
        message = behavior_action_message(replace(action, deadline_monotonic_ns=deadline), now_ns=started,
                                           observation_received_at_ns=self._observation_received_at_ns,
                                           observation_request=request)
        if self._uses_transport_epoch:
            message = bind_transport_epoch(message, self._transport_epoch)
        # From this point even a partial send may have produced a side effect. Only a
        # fully validated response reopens the stream; never blindly resend after failure.
        self._requires_recreation = True
        self.last_behavior_receipt = None
        self._env.ipc.send_action(message, [])
        raw = self._env.ipc.read_observation()
        if self._uses_transport_epoch and decode_transport_epoch(raw.SerializeToString()) != self._transport_epoch:
            raise ContractViolation("step response has a stale transport epoch")
        observation = self._env.convert_observation_v2(raw)
        received = self._clock_ns()
        if received >= deadline:
            raise TimeoutError("formal client behavior exceeded deadline")
        next_sequence = self._sequence_id + 1
        snapshot = observation_from_craftground(
            observation, episode_id=self._episode_id, sequence_id=next_sequence,
            request_sequence_id=action.request_sequence_id, request_started_at_monotonic_ns=started,
            received_at_monotonic_ns=received,
            controller_clock_id=self._controller_clock_id,
            observation_schema_version=self.observation_schema_version,
        )
        if request is not None and snapshot.field_profile != request.field_profile:
            raise ContractViolation("observation_profile_mismatch")
        if request is not None and self._last_client_sample is not None:
            previous, current = self._last_client_sample, snapshot.client_sample
            if current.clock_id != previous.clock_id or current.started_at_monotonic_ns < previous.completed_at_monotonic_ns:
                raise ContractViolation("CraftGround client clock changed or regressed")
        payload = extract_length_delimited_field(raw.SerializeToString(), field_number=50002)
        receipt = decode_behavior_receipt(payload)
        if (not isinstance(receipt, dict)
                or receipt.get("schema_version") not in {
                    "mc2p.client_action_receipt.v2", "mc2p.client_action_receipt.v3"}
                or receipt.get("execution_path") != "client_behavior_v1"
                or receipt.get("episode_id") != self._episode_id
                or receipt.get("request_sequence_id") != action.request_sequence_id
                or receipt.get("generation_id") != next_sequence
                or receipt.get("world_tick") != snapshot.world_time_ticks.value
                or receipt.get("on_client_thread") is not True
                or receipt["action_keyboard_callbacks"] != 0
                or receipt["action_mouse_callbacks"] != 0):
            raise ContractViolation("formal client receipt does not match request/observation")
        immutable_receipt = behavior_receipt_from_mapping(receipt)
        if request is not None and self._clock_ns() >= deadline:
            raise TimeoutError("formal client sample validation exceeded deadline")
        self.last_behavior_receipt = receipt
        self._sequence_id = next_sequence
        self._observation_received_at_ns = received
        self._last_client_sample = snapshot.client_sample
        self._requires_recreation = False
        # Upstream CraftGround step has the same fixed reward/termination outputs.
        return BackendStepResultV1(snapshot, 0.0, False, False, immutable_receipt)

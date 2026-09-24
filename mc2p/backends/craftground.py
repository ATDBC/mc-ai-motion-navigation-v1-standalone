"""CraftGround 2.7.4 / Minecraft 1.21 PlayerBackendV0 adapter."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import socket
import time
import uuid
from types import MethodType
from typing import Any, Callable, Mapping

from mc2p.backends.craftground_runtime import (
    CraftGroundClockModeV0,
    CraftGroundObservationModeV0,
    RuntimePreparationError,
    validate_sandbox_for_mode,
)
from mc2p.backends.client_observation_payload import (
    ClientObservationPayloadError,
    snapshot_v2_from_craftground,
)
from mc2p.contracts.action import ActionSnapshotV0
from mc2p.contracts.common import ContractViolation, FieldValueV0
from mc2p.contracts.observation_v2 import ObservationSnapshotV2
from mc2p.contracts.observation_v3 import ObservationSnapshotV3
from mc2p.contracts.observation_request_v3 import OBSERVATION_V2, OBSERVATION_V3, validate_observation_schema
from mc2p.backends.client_observation_payload_v3 import decode_client_observation_payload_v3, snapshot_v3_from_payload
from mc2p.backends.client_observation_payload import extract_length_delimited_field
from mc2p.contracts.report import FailureCodeV0, FailureV0
from mc2p.contracts.reset import ResetRequestV0, ResetResultV0
from mc2p.runtime.backend import BackendStepResultV0
from mc2p.diagnostics.debug_frame import DebugFrameSinkV0, DebugFrameV0


_PRIVILEGED_FIELDS = frozenset(
    {"height_info", "surrounding_blocks", "surrounding_entities"}
)
_DEBUG_FRAME_KEYS = (("pov", "primary"), ("pov_2", "secondary"))


@dataclass(frozen=True, slots=True)
class CraftGroundObservationDiagnosticsV0:
    observation_mode: CraftGroundObservationModeV0
    observation_count: int
    debug_frame_count: int
    debug_sink_failure_count: int
    last_raw_keys: tuple[str, ...]


class _ObservationMonitor:
    def __init__(self, mode: CraftGroundObservationModeV0) -> None:
        self._mode = mode
        self._observation_count = 0
        self._debug_frame_count = 0
        self._debug_sink_failure_count = 0
        self._last_raw_keys: tuple[str, ...] = ()

    @property
    def next_sequence_id(self) -> int:
        return self._observation_count

    def record(
        self,
        raw_keys: tuple[str, ...],
        *,
        debug_frames: int = 0,
        sink_failures: int = 0,
    ) -> None:
        self._observation_count += 1
        self._debug_frame_count += debug_frames
        self._debug_sink_failure_count += sink_failures
        self._last_raw_keys = raw_keys

    def snapshot(self) -> CraftGroundObservationDiagnosticsV0:
        return CraftGroundObservationDiagnosticsV0(
            observation_mode=self._mode,
            observation_count=self._observation_count,
            debug_frame_count=self._debug_frame_count,
            debug_sink_failure_count=self._debug_sink_failure_count,
            last_raw_keys=self._last_raw_keys,
        )


def _debug_frame_from_value(
    value: Any,
    *,
    sequence_id: int,
    eye: str,
) -> DebugFrameV0:
    try:
        shape = tuple(value.shape)
        dtype = str(value.dtype)
        payload = value.tobytes(order="C")
    except (AttributeError, TypeError, ValueError) as error:
        raise ContractViolation("CraftGround debug POV is invalid") from error
    if len(shape) != 3:
        raise ContractViolation("CraftGround debug POV shape must be HWC")
    height, width, channels = shape
    return DebugFrameV0(
        sequence_id=sequence_id,
        eye=eye,
        width=int(width),
        height=int(height),
        channels=int(channels),
        dtype=dtype,
        payload=payload,
    )


def configure_craftground_observation_mode(
    env: Any,
    mode: CraftGroundObservationModeV0,
    debug_frame_sink: DebugFrameSinkV0 | None,
) -> _ObservationMonitor:
    """Install a conversion hook on one environment instance only."""

    if not isinstance(mode, CraftGroundObservationModeV0):
        raise ContractViolation("CraftGround observation mode is invalid")
    if mode is CraftGroundObservationModeV0.STRUCTURED_ONLY:
        if debug_frame_sink is not None:
            raise ContractViolation(
                "debug frame sink is only valid in pov_debug observation mode"
            )
    elif debug_frame_sink is None or not callable(
        getattr(debug_frame_sink, "write", None)
    ):
        raise ContractViolation("pov_debug requires an explicit debug frame sink")

    monitor = _ObservationMonitor(mode)
    if mode is CraftGroundObservationModeV0.STRUCTURED_ONLY:
        def _convert_structured(bound_env: Any, raw: Any) -> dict[str, Any]:
            bound_env.queued_commands = []
            raw.yaw = ((raw.yaw + 180) % 360) - 180
            monitor.record(("full",))
            return {"full": raw}

        env.convert_observation_v2 = MethodType(_convert_structured, env)
        return monitor

    upstream_converter = getattr(env, "convert_observation_v2", None)
    if not callable(upstream_converter):
        raise ContractViolation("CraftGround environment converter is unavailable")

    def _convert_debug(bound_env: Any, raw: Any) -> dict[str, Any]:
        del bound_env
        converted = upstream_converter(raw)
        if not isinstance(converted, Mapping) or "full" not in converted:
            raise ContractViolation("CraftGround debug conversion missing full")
        raw_keys = tuple(sorted(str(key) for key in converted))
        frames = 0
        failures = 0
        for key, eye in _DEBUG_FRAME_KEYS:
            if key not in converted:
                continue
            frame = _debug_frame_from_value(
                converted[key],
                sequence_id=monitor.next_sequence_id,
                eye=eye,
            )
            try:
                debug_frame_sink.write(frame)
            except Exception:
                failures += 1
            else:
                frames += 1
        monitor.record(raw_keys, debug_frames=frames, sink_failures=failures)
        return {"full": converted["full"]}

    env.convert_observation_v2 = MethodType(_convert_debug, env)
    return monitor


def remaining_timeout_seconds(deadline_ns: int, now_ns: int) -> float:
    if deadline_ns <= now_ns:
        raise TimeoutError("backend deadline expired before IO")
    remaining = (deadline_ns - now_ns) / 1_000_000_000
    return max(0.001, min(30.0, remaining))


def action_to_craftground_v2(
    action: ActionSnapshotV0,
) -> dict[str, bool | float]:
    if not isinstance(action, ActionSnapshotV0):
        raise ContractViolation("CraftGround action must be ActionSnapshotV0")
    mapped: dict[str, bool | float] = {
        "attack": action.interaction.attack,
        "back": action.locomotion.back,
        "forward": action.locomotion.forward,
        "jump": action.locomotion.jump,
        "left": action.locomotion.left,
        "right": action.locomotion.right,
        "sneak": action.locomotion.sneak,
        "sprint": action.locomotion.sprint,
        "use": action.interaction.use,
        "drop": action.gui.drop,
        "inventory": action.gui.inventory,
        "camera_pitch": float(action.camera.pitch_delta),
        "camera_yaw": float(action.camera.yaw_delta),
    }
    mapped.update(
        {
            f"hotbar.{slot}": action.hotbar.selected_slot == slot
            for slot in range(1, 10)
        }
    )
    return mapped


def _attribute_group(
    full: Any,
    names: tuple[str, ...],
    factory: Callable[..., Any],
    label: str,
) -> FieldValueV0[Any]:
    try:
        values = [getattr(full, name) for name in names]
    except AttributeError:
        return FieldValueV0.missing(f"CraftGround did not expose {label}")
    return FieldValueV0.valid(factory(*values))


def _scalar_field(
    full: Any,
    name: str,
    factory: Callable[[Any], Any],
) -> FieldValueV0[Any]:
    try:
        value = getattr(full, name)
    except AttributeError:
        return FieldValueV0.missing(f"CraftGround did not expose {name}")
    return FieldValueV0.valid(factory(value))


def observation_from_craftground(
    observation: Mapping[str, Any],
    *,
    episode_id: str,
    sequence_id: int,
    request_sequence_id: int | None,
    request_started_at_monotonic_ns: int,
    received_at_monotonic_ns: int,
    controller_clock_id: str,
    observation_schema_version: str = OBSERVATION_V2,
) -> ObservationSnapshotV2 | ObservationSnapshotV3:
    validate_observation_schema(observation_schema_version)
    if "full" not in observation:
        raise ContractViolation("CraftGround observation missing full")
    full = observation["full"]
    try:
        present_fields = tuple(
            sorted(descriptor.name for descriptor, _ in full.ListFields())
        )
    except AttributeError as error:
        raise ContractViolation("CraftGround full observation is invalid") from error
    privileged = tuple(
        sorted(set(present_fields).intersection(_PRIVILEGED_FIELDS))
    )
    try:
        serialized = full.SerializeToString()
        world_time_ticks = int(full.world_time)
        if observation_schema_version == OBSERVATION_V3:
            decoded = decode_client_observation_payload_v3(extract_length_delimited_field(serialized, field_number=50000))
            if decoded.generation_id != sequence_id or decoded.sample_world_tick != world_time_ticks:
                raise ContractViolation("CraftGround V3 generation/world tick mismatch")
            return snapshot_v3_from_payload(decoded, episode_id=episode_id, request_sequence_id=request_sequence_id,
                request_started_at_monotonic_ns=request_started_at_monotonic_ns,
                received_at_monotonic_ns=received_at_monotonic_ns, controller_clock_id=controller_clock_id,
                source_backend="craftground", privileged_fields_present=privileged)
        return snapshot_v2_from_craftground(
            serialized,
            episode_id=episode_id,
            sequence_id=sequence_id,
            request_sequence_id=request_sequence_id,
            request_started_at_monotonic_ns=request_started_at_monotonic_ns,
            received_at_monotonic_ns=received_at_monotonic_ns,
            controller_clock_id=controller_clock_id,
            world_time_ticks=world_time_ticks,
            privileged_fields_present=privileged,
        )
    except (AttributeError, TypeError, ValueError, ClientObservationPayloadError) as error:
        raise ContractViolation(f"CraftGround declared observation is invalid: {error}") from error


@dataclass(frozen=True, slots=True)
class CraftGroundCleanupStatusV0:
    actual_port: int
    process_stopped: bool
    port_released: bool


class CraftGroundBackendV0:
    def __init__(
        self,
        *,
        port: int = 8125,
        runtime_env_path: Path | None = None,
        clock_mode: CraftGroundClockModeV0 = CraftGroundClockModeV0.ACCELERATED,
        observation_mode: CraftGroundObservationModeV0 = (
            CraftGroundObservationModeV0.STRUCTURED_ONLY
        ),
        debug_frame_sink: DebugFrameSinkV0 | None = None,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        observation_schema_version: str = OBSERVATION_V2,
    ) -> None:
        self._observation_schema_version = validate_observation_schema(observation_schema_version)
        if observation_schema_version == OBSERVATION_V3 and getattr(self, "action_schema_version", None) != "mc2p.action-snapshot.v1":
            raise ContractViolation("V3 observation requires formal Action V1 backend")
        if observation_schema_version == OBSERVATION_V3 and observation_mode is not CraftGroundObservationModeV0.STRUCTURED_ONLY:
            raise ContractViolation("V3 requires structured_only runtime")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ContractViolation("CraftGround port is invalid")
        if not isinstance(clock_mode, CraftGroundClockModeV0):
            raise ContractViolation("CraftGround clock mode is invalid")
        if not isinstance(observation_mode, CraftGroundObservationModeV0):
            raise ContractViolation("CraftGround observation mode is invalid")
        if observation_mode is CraftGroundObservationModeV0.POV_DEBUG:
            if debug_frame_sink is None or not callable(
                getattr(debug_frame_sink, "write", None)
            ):
                raise ContractViolation(
                    "pov_debug requires an explicit debug frame sink"
                )
        elif debug_frame_sink is not None:
            raise ContractViolation(
                "debug frame sink is only valid in pov_debug observation mode"
            )
        resolved_runtime: Path | None = None
        if runtime_env_path is not None:
            candidate = Path(runtime_env_path)
            if not candidate.is_absolute():
                raise ContractViolation("CraftGround runtime_env_path must be absolute")
            resolved_runtime = candidate.resolve()
            try:
                validate_sandbox_for_mode(
                    resolved_runtime,
                    clock_mode,
                    observation_mode,
                    observation_schema_version=observation_schema_version,
                )
            except RuntimePreparationError as error:
                raise ContractViolation(str(error)) from error
        elif observation_mode is CraftGroundObservationModeV0.STRUCTURED_ONLY:
            raise ContractViolation(
                "structured_only observation mode requires an explicit sandbox"
            )
        elif clock_mode in {
                CraftGroundClockModeV0.REFERENCE_20_TPS,
                CraftGroundClockModeV0.LOCKSTEP_ACCELERATED,
            }:
            raise ContractViolation(
                f"{clock_mode.value} clock mode requires an explicit sandbox"
            )
        self._requested_port = port
        self._runtime_env_path = resolved_runtime
        self._clock_mode = clock_mode
        self._observation_mode = observation_mode
        self._debug_frame_sink = debug_frame_sink
        self._clock_ns = clock_ns
        self._controller_clock_id = "controller:" + str(uuid.uuid4())
        self._env: Any = None
        self._episode_id: str | None = None
        self._sequence_id = 0
        self._closed = False
        self._cleanup_status: CraftGroundCleanupStatusV0 | None = None
        self._observation_monitor: _ObservationMonitor | None = None

    @property
    def observation_schema_version(self) -> str:
        return self._observation_schema_version

    @property
    def cleanup_status(self) -> CraftGroundCleanupStatusV0 | None:
        return self._cleanup_status

    @property
    def observation_diagnostics(self) -> CraftGroundObservationDiagnosticsV0:
        if self._observation_monitor is not None:
            return self._observation_monitor.snapshot()
        return CraftGroundObservationDiagnosticsV0(
            observation_mode=self._observation_mode,
            observation_count=0,
            debug_frame_count=0,
            debug_sink_failure_count=0,
            last_raw_keys=(),
        )

    @property
    def environment_guard_commands(self) -> tuple[str, ...]:
        commands = ["difficulty peaceful", "gamemode creative"]
        if self._clock_mode is CraftGroundClockModeV0.REFERENCE_20_TPS:
            commands.append("tick rate 20")
        return tuple(commands)

    @property
    def actual_port(self) -> int:
        if self._env is None:
            return self._requested_port
        return int(self._env.ipc.port)

    def reset(self, request: ResetRequestV0) -> ResetResultV0:
        if self._closed:
            raise ContractViolation("CraftGround backend is closed")
        if request.scenario_id not in self.supported_scenarios:
            return self._reset_failure(
                request,
                FailureCodeV0.CONFIGURATION,
                f"unsupported scenario: {request.scenario_id}",
                retryable=False,
            )
        try:
            remaining_timeout_seconds(
                request.deadline_monotonic_ns,
                self._clock_ns(),
            )
            if self._env is None:
                self._env = self._create_environment(request.seed, scenario_id=request.scenario_id)
            else:
                self._set_socket_timeout(
                    remaining_timeout_seconds(
                        request.deadline_monotonic_ns,
                        self._clock_ns(),
                    )
                )
            started = self._clock_ns()
            observation, _ = self._reset_environment(request)
            received = self._clock_ns()
            if received >= request.deadline_monotonic_ns:
                raise TimeoutError("CraftGround reset exceeded deadline")
            snapshot = observation_from_craftground(
                observation,
                episode_id=request.episode_id,
                sequence_id=0,
                request_sequence_id=None,
                request_started_at_monotonic_ns=started,
                received_at_monotonic_ns=received,
                controller_clock_id=self._controller_clock_id,
                observation_schema_version=self.observation_schema_version,
            )
            if type(snapshot) is ObservationSnapshotV3 and snapshot.field_profile != "navigation_v1":
                raise ContractViolation("reset requires navigation observation")
            self._validate_reset_snapshot(snapshot)
            if type(snapshot) is ObservationSnapshotV3 and self._clock_ns() >= request.deadline_monotonic_ns:
                raise TimeoutError("CraftGround reset validation exceeded deadline")
            self._episode_id = request.episode_id
            self._sequence_id = 0
            return ResetResultV0(
                request_id=request.request_id,
                actual_episode_id=request.episode_id,
                succeeded=True,
                observation=snapshot,
            )
        except Exception as error:
            if isinstance(error, TimeoutError):
                code = FailureCodeV0.DEADLINE_EXCEEDED
            elif isinstance(error, ContractViolation):
                code = FailureCodeV0.OBSERVATION_INVARIANT
            else:
                code = FailureCodeV0.BACKEND_START
            return self._reset_failure(
                request,
                code,
                str(error),
                retryable=True,
                error=error,
            )

    def _validate_reset_snapshot(self, snapshot: ObservationSnapshotV2 | ObservationSnapshotV3) -> None:
        """Formal adapters may enforce session clocks before reset state is committed."""

    def step(
        self,
        action: ActionSnapshotV0,
        deadline_monotonic_ns: int,
    ) -> BackendStepResultV0:
        if self._closed:
            raise ContractViolation("CraftGround backend is closed")
        if self._env is None or self._episode_id is None:
            raise ContractViolation("CraftGround backend must be reset before step")
        timeout = remaining_timeout_seconds(
            deadline_monotonic_ns,
            self._clock_ns(),
        )
        self._set_socket_timeout(timeout)
        started = self._clock_ns()
        observation, reward, terminated, truncated, _ = self._env.step(
            action_to_craftground_v2(action)
        )
        received = self._clock_ns()
        if received > deadline_monotonic_ns:
            raise TimeoutError("CraftGround step exceeded deadline")
        self._sequence_id += 1
        snapshot = observation_from_craftground(
            observation,
            episode_id=self._episode_id,
            sequence_id=self._sequence_id,
            request_sequence_id=action.action_sequence_id,
            request_started_at_monotonic_ns=started,
            received_at_monotonic_ns=received,
            controller_clock_id=self._controller_clock_id,
        )
        return BackendStepResultV0(
            observation=snapshot,
            reward=float(reward),
            terminated=bool(terminated),
            truncated=bool(truncated),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        env = self._env
        self._env = None
        if env is None:
            self._cleanup_status = CraftGroundCleanupStatusV0(
                actual_port=self._requested_port,
                process_stopped=True,
                port_released=True,
            )
            return
        process = getattr(env, "process", None)
        port = int(env.ipc.port)
        close_error: Exception | None = None
        try:
            self._close_environment(env)
        except Exception as error:
            close_error = error
        process_alive, port_released = self._wait_for_cleanup(process, port)
        self._cleanup_status = CraftGroundCleanupStatusV0(
            actual_port=port,
            process_stopped=not process_alive,
            port_released=port_released,
        )
        failures: list[str] = []
        if close_error is not None:
            failures.append(f"env.close failed: {close_error}")
        if process_alive:
            failures.append("captured CraftGround process is still alive")
        if not port_released:
            failures.append(f"CraftGround port {port} is still accepting connections")
        if failures:
            raise RuntimeError("; ".join(failures)) from close_error

    @property
    def supported_scenarios(self) -> frozenset[str]:
        return frozenset({"flat-safe"})

    def _reset_environment(self, request: ResetRequestV0):
        return self._env.reset(seed=request.seed)

    def _close_environment(self, env: Any) -> None:
        env.close()

    def _configure_initial_environment(self, initial: Any, scenario_id: str) -> None:
        """Scenario configuration only, before the new isolated world is generated."""

    def _create_environment(self, seed: int, *, scenario_id: str = "flat-safe"):
        import craftground
        from craftground.environment.action_space import ActionSpaceVersion
        from craftground.initial_environment_config import (
            Difficulty,
            GameMode,
            WorldType,
        )

        initial = craftground.InitialEnvironmentConfig(
            image_width=64,
            image_height=64,
            gamemode=GameMode.CREATIVE,
            difficulty=Difficulty.PEACEFUL,
            world_type=WorldType.SUPERFLAT,
            seed=str(seed),
            generate_structures=False,
            initial_extra_commands=self.environment_guard_commands,
            render_distance=2,
            simulation_distance=5,
        )
        self._configure_initial_environment(initial, scenario_id)
        arguments: dict[str, Any] = {
            "initial_env_config": initial,
            "mc_version": "1.21",
            "port": self._requested_port,
            "use_shared_memory": False,
            "action_space_version": ActionSpaceVersion.V2_MINERL_HUMAN,
            "cleanup_world": True,
            "verbose_gradle": False,
            "verbose_python": False,
            "verbose_jvm": False,
        }
        if self._runtime_env_path is not None:
            arguments["env_path"] = str(self._runtime_env_path)
        env = craftground.make(**arguments)
        actual_port = int(env.ipc.port)
        if actual_port != self._requested_port:
            close_error: Exception | None = None
            try:
                env.close()
            except Exception as error:
                close_error = error
            suffix = "" if close_error is None else f"; close failed: {close_error}"
            raise RuntimeError(
                f"CraftGround requested port {self._requested_port} but selected "
                f"{actual_port}{suffix}"
            )
        self._observation_monitor = configure_craftground_observation_mode(
            env,
            self._observation_mode,
            self._debug_frame_sink,
        )
        return env

    def _set_socket_timeout(self, timeout: float) -> None:
        sock = getattr(self._env.ipc, "sock", None)
        if sock is None:
            raise ConnectionError("CraftGround socket is unavailable")
        sock.settimeout(timeout)

    @staticmethod
    def _port_accepts_connections(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(0.1)
            return client.connect_ex(("127.0.0.1", port)) == 0

    @classmethod
    def _wait_for_cleanup(
        cls,
        process: Any,
        port: int,
        timeout_seconds: float = 5.0,
    ) -> tuple[bool, bool]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            process_alive = process is not None and process.poll() is None
            port_released = not cls._port_accepts_connections(port)
            if not process_alive and port_released:
                return False, True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return process_alive, port_released
            time.sleep(min(0.1, remaining))

    @staticmethod
    def _reset_failure(
        request: ResetRequestV0,
        code: FailureCodeV0,
        message: str,
        *,
        retryable: bool,
        error: Exception | None = None,
    ) -> ResetResultV0:
        detail = {}
        if error is not None:
            detail["exception_type"] = type(error).__name__
        return ResetResultV0(
            request_id=request.request_id,
            actual_episode_id=request.episode_id,
            succeeded=False,
            failure=FailureV0(
                code=code,
                message=message or code.value,
                retryable=retryable,
                source="craftground",
                detail_json=json.dumps(detail, sort_keys=True),
            ),
        )

"""Run the supervised real-Minecraft Player Runtime V0 vertical smoke."""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import re
import socket
import sys
import time
import traceback
from typing import Any, Callable, Mapping, Sequence
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from mc2p.backends.craftground import CraftGroundBackendV0
from mc2p.backends.client_observation_payload import decode_client_observation_payload
from mc2p.backends.craftground_runtime import (
    CraftGroundClockModeV0,
    CraftGroundObservationModeV0,
    prepare_runtime_sandbox,
    resolve_mc121_runtime_path,
)
from mc2p.contracts.action import (
    ActionIntentV0,
    ActionPriorityV0,
    CameraActionV0,
    LocomotionActionV0,
)
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import FieldStatusV0, FieldValueV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ObservationSnapshotV2
from mc2p.contracts.report import (
    ExecutionStatusV0,
    FailureCodeV0,
    FailureV0,
)
from mc2p.contracts.reset import ResetRequestV0
from mc2p.contracts.task import (
    ComparisonOperatorV0,
    SuccessCriterionV0,
    TaskIntentV0,
)
from mc2p.runtime.player_runtime import PlayerRuntimeV0
from mc2p.runtime.trace import JsonlTraceWriterV0, trace_projection
from scripts.control_probe_core import (
    strict_json_dumps,
    supervise_process,
    write_json_atomic,
)


DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "player-runtime-v0"
DEFAULT_SEED = 20260903
DEFAULT_PORT = 8125
EXPECTED_TRACE_RECORDS = 23
_RUN_ID_COMPONENT = re.compile(r"[^a-z0-9-]+")
_FORMAL_OBSERVATION_V2_KEYS = frozenset(
    item.name for item in fields(ObservationSnapshotV2)
)
_IMAGE_KEY_TOKENS = ("pov", "rgb", "image", "frame", "pixel", "texture", "screenshot")


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    run_id: str
    run_dir: Path
    seed: int = DEFAULT_SEED
    port: int = DEFAULT_PORT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    sandbox_path: Path | None = None


def make_run_id(
    *,
    now: datetime | None = None,
    unique: str | None = None,
) -> str:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    timestamp = instant.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    component = (unique or uuid.uuid4().hex[:8]).casefold()
    component = _RUN_ID_COMPONENT.sub("-", component).strip("-")
    if not component:
        raise ValueError("run id unique component is empty after sanitization")
    return f"{timestamp}-{component}"


def allocate_run_directory(root: Path) -> Path:
    artifact_root = Path(root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        candidate = artifact_root / make_run_id()
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not allocate a unique smoke artifact directory")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the Player Runtime V0 vertical slice in real Minecraft.",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=DEFAULT_ARTIFACT_ROOT,
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--sandbox-path", type=Path, help=argparse.SUPPRESS)
    return parser


def _validate_config(config: SmokeConfig) -> None:
    if not config.run_id or Path(config.run_id).name != config.run_id:
        raise ValueError("run_id must be one safe path component")
    if type(config.seed) is not int:
        raise ValueError("seed must be an integer")
    if type(config.port) is not int or not 1 <= config.port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    if config.sandbox_path is not None and not config.sandbox_path.is_absolute():
        raise ValueError("sandbox_path must be absolute")


def _versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "craftground": importlib.metadata.version("craftground"),
        "minecraft": "1.21",
        "action_space": "V2_MINERL_HUMAN",
        "runtime_contract": "v0",
    }


def _worker_command(config: SmokeConfig) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--run-id",
        config.run_id,
        "--run-dir",
        str(config.run_dir.resolve()),
        "--seed",
        str(config.seed),
        "--port",
        str(config.port),
        "--timeout-seconds",
        str(config.timeout_seconds),
    ]
    if config.sandbox_path is not None:
        command.extend(("--sandbox-path", str(config.sandbox_path)))
    return command


def _failure(
    code: FailureCodeV0,
    message: str,
    *,
    source: str,
    retryable: bool,
    error: BaseException | None = None,
) -> dict[str, Any]:
    detail: dict[str, str] = {}
    if error is not None:
        detail["exception_type"] = type(error).__name__
    value = FailureV0(
        code=code,
        message=message or code.value,
        retryable=retryable,
        source=source,
        detail_json=json.dumps(detail, sort_keys=True),
    )
    projected = trace_projection(value)
    assert isinstance(projected, dict)
    return projected


def _read_trace(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"trace line {line_number} is not an object")
            records.append(value)
    return records


def _payload(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("trace record payload is not an object")
    return payload


def _observation(record: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = _payload(record)
    if record.get("record_type") == "reset":
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("reset trace has no result")
        observation = result.get("observation")
    else:
        backend_result = payload.get("backend_result")
        if not isinstance(backend_result, Mapping):
            raise ValueError("step trace has no backend result")
        observation = backend_result.get("observation")
    if not isinstance(observation, Mapping):
        raise ValueError("trace record has no observation")
    return observation


def _valid_field(observation: Mapping[str, Any], name: str) -> Any:
    field = observation.get(name)
    if not isinstance(field, Mapping) or field.get("status") != "valid":
        raise ValueError(f"observation field {name} is not valid")
    return field.get("value")


def _yaw(record: Mapping[str, Any]) -> float:
    return float(_valid_field(_observation(record), "yaw_degrees"))


def _position(record: Mapping[str, Any]) -> tuple[float, float, float]:
    value = _valid_field(_observation(record), "position")
    if not isinstance(value, Mapping):
        raise ValueError("position value is not an object")
    return float(value["x"]), float(value["y"]), float(value["z"])


def _angle_delta(previous: float, current: float) -> float:
    return ((current - previous + 180.0) % 360.0) - 180.0


def _step_action(record: Mapping[str, Any]) -> Mapping[str, Any]:
    decision = _payload(record).get("decision")
    if not isinstance(decision, Mapping):
        raise ValueError("step trace has no arbitration decision")
    action = decision.get("action")
    if not isinstance(action, Mapping):
        raise ValueError("step trace has no action")
    return action


def _is_neutral_action(action: Mapping[str, Any]) -> bool:
    locomotion = action.get("locomotion")
    camera = action.get("camera")
    interaction = action.get("interaction")
    hotbar = action.get("hotbar")
    gui = action.get("gui")
    if not all(
        isinstance(value, Mapping)
        for value in (locomotion, camera, interaction, hotbar, gui)
    ):
        return False
    assert isinstance(locomotion, Mapping)
    assert isinstance(camera, Mapping)
    assert isinstance(interaction, Mapping)
    assert isinstance(hotbar, Mapping)
    assert isinstance(gui, Mapping)
    return (
        all(
            locomotion.get(key) is False
            for key in (
                "forward",
                "back",
                "left",
                "right",
                "jump",
                "sneak",
                "sprint",
            )
        )
        and float(camera.get("pitch_delta", 1.0)) == 0.0
        and float(camera.get("yaw_delta", 1.0)) == 0.0
        and interaction.get("attack") is False
        and interaction.get("use") is False
        and hotbar.get("selected_slot") is None
        and gui.get("drop") is False
        and gui.get("inventory") is False
    )


def _check(
    name: str,
    passed: bool,
    observed: object,
    requirement: str,
) -> dict[str, object]:
    return {
        "name": name,
        "passed": bool(passed),
        "observed": observed,
        "requirement": requirement,
    }


def _formal_observation_violations(
    value: object,
    *,
    path: str,
) -> list[str]:
    violations: list[str] = []
    if isinstance(value, Mapping):
        keys = {str(key) for key in value}
        if {"byte_length", "sha256"}.issubset(keys):
            violations.append(f"{path}: binary trace projection")
        for key, child in value.items():
            normalized = re.sub(r"[^a-z0-9]+", "_", str(key).casefold())
            if any(token in normalized for token in _IMAGE_KEY_TOKENS):
                violations.append(f"{path}.{key}: image-bearing key")
            violations.extend(
                _formal_observation_violations(child, path=f"{path}.{key}")
            )
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            violations.extend(
                _formal_observation_violations(child, path=f"{path}[{index}]")
            )
    return violations


def _exact_mapping(value: object, expected: set[str], path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} is not an object")
    actual = {str(key) for key in value}
    if actual != expected:
        raise ValueError(
            f"{path} fields differ: missing={sorted(expected - actual)}, "
            f"extra={sorted(actual - expected)}"
        )
    return value


def _parse_vec3(value: object, path: str) -> Vec3V0:
    vector = _exact_mapping(value, {"x", "y", "z"}, path)
    return Vec3V0(x=vector["x"], y=vector["y"], z=vector["z"])


def _parse_field(
    observation: Mapping[str, Any],
    name: str,
    parse_valid: Callable[[object], Any],
) -> FieldValueV0[Any]:
    field = _exact_mapping(
        observation[name],
        {"status", "value", "detail"},
        f"observation.{name}",
    )
    status = FieldStatusV0(field["status"])
    raw_value = field["value"]
    value = parse_valid(raw_value) if status is FieldStatusV0.VALID else raw_value
    return FieldValueV0(status=status, value=value, detail=field["detail"])


def _trace_pairs_as_objects(value: object, keys: tuple[str, str], name: str) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array of pairs")
    result = []
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"{name} entry must be an exact JSON pair")
        result.append(dict(zip(keys, pair)))
    return result


def _parse_observation_v2(observation: Mapping[str, Any]) -> ObservationSnapshotV2:
    actual_keys = frozenset(str(key) for key in observation)
    if actual_keys != _FORMAL_OBSERVATION_V2_KEYS:
        missing = sorted(_FORMAL_OBSERVATION_V2_KEYS - actual_keys)
        extra = sorted(actual_keys - _FORMAL_OBSERVATION_V2_KEYS)
        raise ValueError(f"top-level fields differ: missing={missing}, extra={extra}")
    if observation.get("schema_version") != "mc2p.observation.v2":
        raise ValueError(f"schema={observation.get('schema_version')!r}")
    privileged = observation["privileged_fields_present"]
    if not isinstance(privileged, list):
        raise ValueError("privileged_fields_present is not a JSON array")
    world_time = _parse_field(observation, "world_time_ticks", lambda value: value)
    if world_time.status is not FieldStatusV0.VALID or type(world_time.value) is not int:
        raise ValueError("world_time_ticks must be a valid integer")
    payload = {
        "schema_version": "mc2p.client_observation.v2",
        "generation_id": observation["sequence_id"],
        "sample_world_tick": world_time.value,
        "client_sample": observation["client_sample"],
        "self_state": observation["self_state"],
        "inventory": observation["inventory"],
        "gui": observation["gui"],
        "perception": observation["perception"],
    }
    # Runtime dataclass tuples serialize as pairs, unlike the JVM wire's named objects.
    # Convert only these three structural representations, then reuse strict wire validation.
    # Never mutate the artifact, discard extra fields, or loosen the wire protocol itself.
    payload = deepcopy(payload)
    gui = payload["gui"]
    if gui["status"] == "valid":
        gui["value"]["properties"] = _trace_pairs_as_objects(
            gui["value"]["properties"], ("property_id", "value"), "gui.properties")
    perception = payload["perception"]
    if perception["status"] == "valid":
        for ray in perception["value"]["block_rays"]:
            ray["state_properties"] = _trace_pairs_as_objects(
                ray["state_properties"], ("name", "value"), "block.state_properties")
        for entity in perception["value"]["visible_entities"]:
            entity["equipment"] = _trace_pairs_as_objects(
                entity["equipment"], ("slot", "item"), "entity.equipment")
    decoded = decode_client_observation_payload(
        json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"),
        expected_generation_id=observation["sequence_id"],
        expected_world_tick=world_time.value,
    )
    return ObservationSnapshotV2(
        episode_id=observation["episode_id"],
        sequence_id=observation["sequence_id"],
        request_sequence_id=observation["request_sequence_id"],
        request_started_at_monotonic_ns=observation["request_started_at_monotonic_ns"],
        received_at_monotonic_ns=observation["received_at_monotonic_ns"],
        controller_clock_id=observation["controller_clock_id"],
        client_sample=decoded.client_sample,
        server_state_age_ns=_parse_field(observation, "server_state_age_ns", lambda value: value),
        world_time_ticks=world_time,
        position=_parse_field(
            observation,
            "position",
            lambda value: _parse_vec3(value, "observation.position.value"),
        ),
        yaw_degrees=_parse_field(
            observation,
            "yaw_degrees",
            lambda value: value,
        ),
        pitch_degrees=_parse_field(
            observation,
            "pitch_degrees",
            lambda value: value,
        ),
        is_on_ground=_parse_field(
            observation,
            "is_on_ground",
            lambda value: value,
        ),
        is_dead=_parse_field(observation, "is_dead", lambda value: value),
        health_points=_parse_field(
            observation,
            "health_points",
            lambda value: value,
        ),
        food_points=_parse_field(
            observation,
            "food_points",
            lambda value: value,
        ),
        self_state=decoded.self_state,
        inventory=decoded.inventory,
        gui=decoded.gui,
        perception=decoded.perception,
        source_backend=observation["source_backend"],
        privileged_fields_present=tuple(privileged),
    )


def _validate_formal_observations(
    observations: Sequence[Mapping[str, Any]],
) -> list[str]:
    violations: list[str] = []
    for index, observation in enumerate(observations):
        try:
            _parse_observation_v2(observation)
        except (KeyError, TypeError, ValueError) as error:
            violations.append(f"observation[{index}]: {error}")
        violations.extend(
            _formal_observation_violations(
                observation,
                path=f"observation[{index}]",
            )
        )
    return violations


def _evaluate_trace(
    records: Sequence[Mapping[str, Any]],
    cancelled_camera_ids: Sequence[str],
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    metrics: dict[str, Any] = {"trace_record_count": len(records)}
    checks: list[dict[str, object]] = []
    record_types = [record.get("record_type") for record in records]
    expected_types = ["reset", *("step" for _ in range(22))]
    complete = len(records) == EXPECTED_TRACE_RECORDS and record_types == expected_types
    checks.append(
        _check(
            "trace_shape",
            complete,
            {"count": len(records), "record_types": record_types},
            "one reset followed by exactly 22 step records",
        )
    )
    if not complete:
        return metrics, checks

    observations = [_observation(record) for record in records]
    formal_violations = _validate_formal_observations(observations)
    checks.append(
        _check(
            "formal_observation_v2_no_images",
            not formal_violations,
            formal_violations,
            "every observation has the exact V2 surface and no image-bearing or binary payload",
        )
    )
    if formal_violations:
        return metrics, checks
    observation_sequences = [
        int(observation["sequence_id"]) for observation in observations
    ]
    step_actions = [_step_action(record) for record in records[1:]]
    action_sequences = [int(action["action_sequence_id"]) for action in step_actions]
    request_sequences = [
        observation["request_sequence_id"] for observation in observations[1:]
    ]
    sequences_continuous = (
        observation_sequences == list(range(23))
        and action_sequences == list(range(22))
        and request_sequences == list(range(22))
    )
    checks.append(
        _check(
            "continuous_sequences",
            sequences_continuous,
            {
                "observation": observation_sequences,
                "action": action_sequences,
                "request": request_sequences,
            },
            "observation 0..22 and matching action/request 0..21",
        )
    )

    selected = []
    for record in records[1:]:
        decision = _payload(record)["decision"]
        selected.append(decision["selected_intents"])
    expected_split = (
        all(item == [] for item in selected[0:3])
        and all(
            item
            == [
                ["locomotion", "intent-locomotion"],
                ["camera", "intent-camera"],
            ]
            for item in selected[3:9]
        )
        and all(
            item == [["locomotion", "intent-locomotion"]]
            for item in selected[9:21]
        )
        and selected[21] == []
        and list(cancelled_camera_ids) == ["intent-camera"]
    )
    checks.append(
        _check(
            "group_arbiter_and_source_cancel",
            expected_split,
            {
                "cancelled_camera_ids": list(cancelled_camera_ids),
                "selected_intents": selected,
            },
            "camera and locomotion merge, camera source cancels independently",
        )
    )

    reports = [_payload(record).get("report") for record in records[1:]]
    final_report = reports[-1]
    cancelled_final = (
        isinstance(final_report, Mapping)
        and final_report.get("status") == ExecutionStatusV0.CANCELLED.value
        and isinstance(final_report.get("failure"), Mapping)
        and final_report["failure"].get("code") == FailureCodeV0.CANCELLED.value
    )
    checks.append(
        _check(
            "cancelled_final_report",
            cancelled_final,
            final_report,
            "final boundary emits a structured cancelled report",
        )
    )
    final_neutral = _is_neutral_action(step_actions[-1])
    checks.append(
        _check(
            "final_action_neutral",
            final_neutral,
            step_actions[-1],
            "cancellation boundary writes one complete neutral snapshot",
        )
    )

    backend_results = [_payload(record)["backend_result"] for record in records[1:]]
    not_terminated = all(
        not result["terminated"] and not result["truncated"]
        for result in backend_results
    )
    alive = all(not bool(_valid_field(observation, "is_dead")) for observation in observations)
    checks.append(
        _check(
            "episode_continues",
            not_terminated and alive,
            {"not_terminated": not_terminated, "alive": alive},
            "no termination, truncation, or player death",
        )
    )

    yaw_deltas = [
        _angle_delta(_yaw(records[index - 1]), _yaw(records[index]))
        for index in range(4, 11)
    ]
    yaw_combined = sum(yaw_deltas[:6])
    yaw_tail = yaw_deltas[6]
    yaw_total = yaw_combined + yaw_tail
    metrics.update(
        {
            "yaw_combined_response_degrees": yaw_combined,
            "yaw_first_post_cancel_tail_degrees": yaw_tail,
            "yaw_total_boundary_response_degrees": yaw_total,
        }
    )
    checks.append(
        _check(
            "yaw_boundary_response",
            abs(yaw_total) > 30.0,
            {
                "combined": yaw_combined,
                "first_post_cancel_tail": yaw_tail,
                "total": yaw_total,
            },
            "combined camera response plus one boundary observation exceeds 30 degrees",
        )
    )

    start_position = _position(records[3])
    end_position = _position(records[21])
    displacement = math.hypot(
        end_position[0] - start_position[0],
        end_position[2] - start_position[2],
    )
    metrics["forward_net_displacement_blocks"] = displacement
    checks.append(
        _check(
            "forward_movement",
            displacement > 0.5,
            displacement,
            "18 forward actions move the player more than 0.5 block",
        )
    )
    return metrics, checks


def _write_failure_text(
    run_dir: Path,
    failures: Sequence[Mapping[str, Any]],
    tracebacks: Sequence[str],
) -> None:
    if not failures and not tracebacks:
        return
    sections = [strict_json_dumps(failure) for failure in failures]
    sections.extend(item.strip() for item in tracebacks if item.strip())
    (run_dir / "failure.txt").write_text(
        "\n\n".join(sections) + "\n",
        encoding="utf-8",
    )


def _make_task(deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0(
        task_id="runtime-v0-smoke",
        task_type="move-and-turn",
        parameters_json='{"forward_steps":18,"yaw_steps":6}',
        success_criteria=(
            SuccessCriterionV0(
                metric="horizontal_displacement",
                operator=ComparisonOperatorV0.GREATER_THAN,
                target_value=0.5,
                unit="blocks",
            ),
        ),
        priority=200,
        deadline_monotonic_ns=deadline_ns,
        interruptible=True,
        max_risk=0.0,
    )


def _run_runtime_sequence(
    runtime: PlayerRuntimeV0,
    config: SmokeConfig,
    deadline_ns: int,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    reset = runtime.reset(
        ResetRequestV0(
            request_id=f"reset-{config.run_id}",
            episode_id=f"episode-{config.seed}",
            scenario_id="flat-safe",
            seed=config.seed,
            deadline_monotonic_ns=deadline_ns,
        )
    )
    if not reset.succeeded:
        assert reset.failure is not None
        projected = trace_projection(reset.failure)
        assert isinstance(projected, dict)
        return projected, ()

    task = _make_task(deadline_ns)
    profile = BehaviorProfileV0()
    for _ in range(3):
        result = runtime.step(task, profile, deadline_ns)
        if result.report.status is not ExecutionStatusV0.RUNNING:
            assert result.report.failure is not None
            projected = trace_projection(result.report.failure)
            assert isinstance(projected, dict)
            return projected, ()

    submitted = time.perf_counter_ns()
    runtime.submit_intent(
        ActionIntentV0(
            intent_id="intent-locomotion",
            source_id="locomotion-controller",
            priority=ActionPriorityV0.TASK,
            submitted_at_monotonic_ns=submitted,
            expires_at_monotonic_ns=deadline_ns,
            locomotion=LocomotionActionV0(forward=True),
        )
    )
    runtime.submit_intent(
        ActionIntentV0(
            intent_id="intent-camera",
            source_id="camera-controller",
            priority=ActionPriorityV0.TASK,
            submitted_at_monotonic_ns=submitted,
            expires_at_monotonic_ns=deadline_ns,
            camera=CameraActionV0(yaw_delta=15.0),
        )
    )
    for _ in range(6):
        result = runtime.step(task, profile, deadline_ns)
        if result.report.status is not ExecutionStatusV0.RUNNING:
            assert result.report.failure is not None
            projected = trace_projection(result.report.failure)
            assert isinstance(projected, dict)
            return projected, ()

    cancelled_camera_ids = runtime.cancel_source("camera-controller")
    for _ in range(2):
        result = runtime.step(task, profile, deadline_ns)
        if result.report.status is not ExecutionStatusV0.RUNNING:
            assert result.report.failure is not None
            projected = trace_projection(result.report.failure)
            assert isinstance(projected, dict)
            return projected, cancelled_camera_ids
    for _ in range(10):
        result = runtime.step(task, profile, deadline_ns)
        if result.report.status is not ExecutionStatusV0.RUNNING:
            assert result.report.failure is not None
            projected = trace_projection(result.report.failure)
            assert isinstance(projected, dict)
            return projected, cancelled_camera_ids

    runtime.cancel("operator-interrupt-smoke")
    final = runtime.step(task, profile, deadline_ns)
    if final.report.status is not ExecutionStatusV0.CANCELLED:
        if final.report.failure is not None:
            projected = trace_projection(final.report.failure)
            assert isinstance(projected, dict)
            return projected, cancelled_camera_ids
        return (
            _failure(
                FailureCodeV0.CONTRACT,
                f"expected cancelled final report, got {final.report.status.value}",
                source="smoke",
                retryable=False,
            ),
            cancelled_camera_ids,
        )
    return None, cancelled_camera_ids


def run_worker(config: SmokeConfig) -> int:
    _validate_config(config)
    config.run_dir.mkdir(parents=True, exist_ok=True)
    trace_path = config.run_dir / "trace.jsonl"
    if trace_path.exists():
        raise FileExistsError(f"trace already exists: {trace_path}")

    runtime: PlayerRuntimeV0 | None = None
    backend: CraftGroundBackendV0 | None = None
    primary_failure: dict[str, Any] | None = None
    failure_tracebacks: list[str] = []
    cancelled_camera_ids: tuple[str, ...] = ()
    started = time.perf_counter_ns()
    worker_budget = max(1.0, config.timeout_seconds - 15.0)
    deadline_ns = started + int(worker_budget * 1_000_000_000)

    try:
        trace_writer = JsonlTraceWriterV0(trace_path)
        if config.sandbox_path is None:
            raise ValueError("structured worker requires an explicit sandbox")
        backend = CraftGroundBackendV0(
            port=config.port,
            runtime_env_path=config.sandbox_path,
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
        )
        runtime = PlayerRuntimeV0(backend, trace_writer)
        primary_failure, cancelled_camera_ids = _run_runtime_sequence(
            runtime,
            config,
            deadline_ns,
        )
    except KeyboardInterrupt as error:
        primary_failure = _failure(
            FailureCodeV0.CANCELLED,
            "worker interrupted",
            source="smoke",
            retryable=True,
            error=error,
        )
        failure_tracebacks.append(traceback.format_exc())
    except Exception as error:
        primary_failure = _failure(
            FailureCodeV0.UNEXPECTED,
            str(error),
            source="smoke",
            retryable=False,
            error=error,
        )
        failure_tracebacks.append(traceback.format_exc())
    finally:
        if runtime is not None:
            runtime.close()
        elif backend is not None:
            try:
                backend.close()
            except Exception as error:
                if primary_failure is None:
                    primary_failure = _failure(
                        FailureCodeV0.CLEANUP,
                        str(error),
                        source="smoke",
                        retryable=True,
                        error=error,
                    )
                failure_tracebacks.append(traceback.format_exc())

    cleanup_failures = (
        []
        if runtime is None
        else [trace_projection(item) for item in runtime.cleanup_failures]
    )
    cleanup_status = None if backend is None else backend.cleanup_status
    cleanup = (
        {
            "actual_port": config.port,
            "process_stopped": False,
            "port_released": not _port_accepts_connections(config.port),
        }
        if cleanup_status is None
        else trace_projection(cleanup_status)
    )
    assert isinstance(cleanup, dict)

    records: list[dict[str, Any]] = []
    checks: list[dict[str, object]] = []
    metrics: dict[str, Any] = {"trace_record_count": 0}
    try:
        records = _read_trace(trace_path)
        metrics, checks = _evaluate_trace(records, cancelled_camera_ids)
    except Exception as error:
        if primary_failure is None:
            primary_failure = _failure(
                FailureCodeV0.OBSERVATION_INVARIANT,
                str(error),
                source="smoke",
                retryable=False,
                error=error,
            )
        failure_tracebacks.append(traceback.format_exc())

    process_stopped = cleanup.get("process_stopped") is True
    port_released = cleanup.get("port_released") is True
    checks.extend(
        [
            _check(
                "process_stopped",
                process_stopped,
                cleanup,
                "captured CraftGround process stopped after close",
            ),
            _check(
                "port_released",
                port_released,
                cleanup,
                "actual CraftGround control port released after close",
            ),
        ]
    )
    failed_checks = [check["name"] for check in checks if not check["passed"]]
    if primary_failure is None and failed_checks:
        primary_failure = _failure(
            FailureCodeV0.CONTRACT,
            f"smoke checks failed: {failed_checks}",
            source="smoke",
            retryable=False,
        )

    elapsed_seconds = (time.perf_counter_ns() - started) / 1_000_000_000
    status = (
        "passed"
        if primary_failure is None and not cleanup_failures and not failed_checks
        else "failed"
    )
    report = {
        "schema_version": "mc2p.player-runtime-smoke.v0",
        "run_id": config.run_id,
        "status": status,
        "versions": _versions(),
        "config": {
            "seed": config.seed,
            "requested_port": config.port,
            "timeout_seconds": config.timeout_seconds,
            "expected_trace_records": EXPECTED_TRACE_RECORDS,
        },
        "elapsed_seconds": elapsed_seconds,
        "metrics": metrics,
        "checks": checks,
        "primary_failure": primary_failure,
        "cleanup_failures": cleanup_failures,
        "cleanup": cleanup,
        "trace_file": trace_path.name,
    }
    write_json_atomic(config.run_dir / "result.json", report)
    failures = ([] if primary_failure is None else [primary_failure]) + cleanup_failures
    _write_failure_text(config.run_dir, failures, failure_tracebacks)
    print(strict_json_dumps(report))
    return 0 if status == "passed" else 2


def _port_accepts_connections(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.1)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _read_result(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("smoke result must be a JSON object")
    return value


def run_parent(config: SmokeConfig) -> int:
    _validate_config(config)
    print(f"PLAYER_RUNTIME_V0_RUN_DIR={config.run_dir.resolve()}")
    try:
        prepared = prepare_runtime_sandbox(
            source_root=resolve_mc121_runtime_path(),
            sandbox_parent=config.run_dir / "sandboxes",
            sandbox_id="accelerated-structured",
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
        )
        worker_config = replace(config, sandbox_path=prepared.path)
    except Exception as error:
        write_json_atomic(
            config.run_dir / "result.json",
            {
                "schema_version": "mc2p.player-runtime-smoke.v0",
                "run_id": config.run_id,
                "status": "failed",
                "primary_failure": _failure(
                    FailureCodeV0.CONFIGURATION,
                    str(error),
                    source="sandbox",
                    retryable=False,
                    error=error,
                ),
                "cleanup_failures": [],
            },
        )
        return 1
    supervision = supervise_process(
        _worker_command(worker_config),
        timeout_seconds=config.timeout_seconds,
        grace_seconds=10.0,
    )
    supervision_value = trace_projection(supervision)
    write_json_atomic(config.run_dir / "supervision.json", supervision_value)
    if supervision.timed_out or supervision.cancelled:
        failure = _failure(
            (
                FailureCodeV0.DEADLINE_EXCEEDED
                if supervision.timed_out
                else FailureCodeV0.CANCELLED
            ),
            (
                f"worker exceeded {config.timeout_seconds} seconds"
                if supervision.timed_out
                else "operator cancelled smoke supervisor"
            ),
            source="supervisor",
            retryable=True,
        )
        result_path = config.run_dir / "result.json"
        if not result_path.exists():
            write_json_atomic(
                result_path,
                {
                    "schema_version": "mc2p.player-runtime-smoke.v0",
                    "run_id": config.run_id,
                    "status": "failed",
                    "primary_failure": failure,
                    "cleanup_failures": [],
                    "cleanup": {
                        "actual_port": config.port,
                        "process_stopped": supervision.forced_termination,
                        "port_released": not _port_accepts_connections(config.port),
                    },
                },
            )
        return 1 if supervision.timed_out else 130

    try:
        report = _read_result(config.run_dir / "result.json")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"PLAYER_RUNTIME_V0_RESULT_ERROR={type(error).__name__}: {error}")
        return 1
    if supervision.exit_code == 0 and report.get("status") == "passed":
        print("PLAYER_RUNTIME_V0_SMOKE_OK")
        return 0
    return supervision.exit_code if supervision.exit_code in {1, 2, 130} else 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.worker:
        if not arguments.run_id or arguments.run_dir is None:
            raise ValueError("worker requires --run-id and --run-dir")
        return run_worker(
            SmokeConfig(
                run_id=arguments.run_id,
                run_dir=arguments.run_dir,
                seed=arguments.seed,
                port=arguments.port,
                timeout_seconds=arguments.timeout_seconds,
                sandbox_path=(
                    arguments.sandbox_path.resolve()
                    if arguments.sandbox_path is not None
                    else None
                ),
            )
        )

    run_dir = allocate_run_directory(arguments.artifacts_dir)
    return run_parent(
        SmokeConfig(
            run_id=run_dir.name,
            run_dir=run_dir,
            seed=arguments.seed,
            port=arguments.port,
            timeout_seconds=arguments.timeout_seconds,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

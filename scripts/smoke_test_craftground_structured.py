"""Supervise a real Minecraft structured-only, zero-image vertical smoke."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import socket
import statistics
import sys
import time
import traceback
from typing import Any, Mapping, Sequence
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))

from mc2p.backends.craftground import CraftGroundBackendV0
from mc2p.backends.craftground_runtime import (
    MC121_RUNTIME_0_1_0_FINGERPRINTS,
    CraftGroundClockModeV0,
    CraftGroundObservationModeV0,
    capture_runtime_source_fingerprints,
    prepare_runtime_sandbox,
    resolve_mc121_runtime_path,
)
from mc2p.contracts.action import ActionSnapshotV0, LocomotionActionV0
from mc2p.contracts.common import FieldStatusV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ObservationSnapshotV2
from mc2p.contracts.reset import ResetRequestV0
from mc2p.runtime.trace import trace_projection
from scripts.control_probe_core import (
    append_jsonl,
    strict_json_dumps,
    write_json_atomic,
)
from scripts.probe_craftground_timing_parallel import run_bounded_process


DEFAULT_ARTIFACT_ROOT = PROJECT_ROOT / "artifacts" / "structured-only"
DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_SEED = 20260905
DEFAULT_PORT = 8126
EXPECTED_OBSERVATIONS = 9
DIAGNOSTIC_FILE_NAME = "mc2p-structured-observation.jsonl"
FORBIDDEN_FORMAL_KEYS = frozenset(
    {"pov", "pov_2", "rgb", "rgb_2", "image", "image2", "payload"}
)


@dataclass(frozen=True, slots=True)
class StructuredSmokeConfigV0:
    run_id: str
    run_dir: Path
    sandbox_path: Path
    seed: int = DEFAULT_SEED
    port: int = DEFAULT_PORT
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def _check(name: str, passed: bool, actual: object, expected: object) -> dict[str, object]:
    return {
        "name": name,
        "passed": bool(passed),
        "actual": actual,
        "expected": expected,
    }


def evaluate_structured_evidence(
    evidence: Mapping[str, object],
) -> list[dict[str, object]]:
    """Evaluate the zero-image and cleanup proof independently of Minecraft."""

    formal_keys = evidence.get("formal_observation_keys")
    observations = evidence.get("jvm_observations")
    image_bytes = evidence.get("image_bytes")
    captures = evidence.get("framebuffer_capture_calls")
    encodes = evidence.get("image_encode_calls")
    creation_visibility = evidence.get("window_visible_at_creation")
    visibility = evidence.get("window_visible")
    world_attempts = evidence.get("render_world_attempts")
    world_completions = evidence.get("render_world_completions")
    world_attempts_valid = (
        isinstance(world_attempts, list)
        and len(world_attempts) == EXPECTED_OBSERVATIONS
        and all(type(value) is int and value >= 0 for value in world_attempts)
        and all(left < right for left, right in zip(world_attempts, world_attempts[1:]))
    )
    zero_world_completions = (
        isinstance(world_completions, list)
        and len(world_completions) == EXPECTED_OBSERVATIONS
        and all(type(value) is int and value == 0 for value in world_completions)
    )
    return [
        _check("formal_keys", formal_keys == ["full"], formal_keys, ["full"]),
        _check(
            "jvm_observation_count",
            observations == EXPECTED_OBSERVATIONS,
            observations,
            EXPECTED_OBSERVATIONS,
        ),
        _check(
            "zero_image_bytes",
            isinstance(image_bytes, list)
            and len(image_bytes) == EXPECTED_OBSERVATIONS
            and all(value == 0 for value in image_bytes),
            image_bytes,
            [0] * EXPECTED_OBSERVATIONS,
        ),
        _check(
            "zero_framebuffer_captures",
            isinstance(captures, list)
            and len(captures) == EXPECTED_OBSERVATIONS
            and all(value == 0 for value in captures),
            captures,
            [0] * EXPECTED_OBSERVATIONS,
        ),
        _check(
            "zero_image_encodes",
            isinstance(encodes, list)
            and len(encodes) == EXPECTED_OBSERVATIONS
            and all(value == 0 for value in encodes),
            encodes,
            [0] * EXPECTED_OBSERVATIONS,
        ),
        _check(
            "window_created_invisible",
            isinstance(creation_visibility, list)
            and len(creation_visibility) == EXPECTED_OBSERVATIONS
            and all(value is False for value in creation_visibility),
            creation_visibility,
            [False] * EXPECTED_OBSERVATIONS,
        ),
        _check(
            "window_invisible",
            isinstance(visibility, list)
            and len(visibility) == EXPECTED_OBSERVATIONS
            and all(value is False for value in visibility),
            visibility,
            [False] * EXPECTED_OBSERVATIONS,
        ),
        _check(
            "world_render_skipped",
            world_attempts_valid,
            world_attempts,
            "renderWorld HEAD attempts strictly increase at every post-reset step",
        ),
        _check(
            "zero_render_world_completions",
            zero_world_completions,
            world_completions,
            [0] * EXPECTED_OBSERVATIONS,
        ),
        _check(
            "process_stopped",
            evidence.get("process_stopped") is True,
            evidence.get("process_stopped"),
            True,
        ),
        _check(
            "port_released",
            evidence.get("port_released") is True,
            evidence.get("port_released"),
            True,
        ),
    ]


def _validate_config(config: StructuredSmokeConfigV0) -> None:
    if not config.run_id or Path(config.run_id).name != config.run_id:
        raise ValueError("run_id must be one safe path component")
    if not config.run_dir.is_absolute() or not config.sandbox_path.is_absolute():
        raise ValueError("run and sandbox paths must be absolute")
    if type(config.seed) is not int:
        raise ValueError("seed must be an integer")
    if type(config.port) is not int or not 1 <= config.port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")


def _make_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def _allocate_run_directory(root: Path) -> Path:
    artifact_root = Path(root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    for _ in range(16):
        candidate = artifact_root / _make_run_id()
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError("could not allocate structured smoke directory")


def _worker_command(config: StructuredSmokeConfigV0) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--run-id",
        config.run_id,
        "--run-dir",
        str(config.run_dir),
        "--sandbox-path",
        str(config.sandbox_path),
        "--seed",
        str(config.seed),
        "--port",
        str(config.port),
        "--timeout-seconds",
        str(config.timeout_seconds),
    ]


def _position(observation: ObservationSnapshotV2) -> Vec3V0:
    field = observation.position
    if field.status is not FieldStatusV0.VALID or not isinstance(field.value, Vec3V0):
        raise RuntimeError("structured observation position is unavailable")
    return field.value


def _is_dead(observation: ObservationSnapshotV2) -> bool:
    field = observation.is_dead
    if field.status is not FieldStatusV0.VALID or field.value is None:
        raise RuntimeError("structured observation death state is unavailable")
    return bool(field.value)


def _horizontal_distance(left: Vec3V0, right: Vec3V0) -> float:
    return math.hypot(right.x - left.x, right.z - left.z)


def build_action_schedule() -> tuple[ActionSnapshotV0, ...]:
    forward = tuple(
        replace(
            ActionSnapshotV0.neutral(sequence),
            locomotion=LocomotionActionV0(forward=True),
        )
        for sequence in range(4)
    )
    neutral = tuple(ActionSnapshotV0.neutral(sequence) for sequence in range(4, 8))
    return forward + neutral


def _read_diagnostic_tail(path: Path, offset: int) -> list[dict[str, object]]:
    if not path.is_file():
        raise RuntimeError(f"structured diagnostics are missing: {path}")
    with path.open("rb") as stream:
        stream.seek(offset)
        payload = stream.read().decode("utf-8")
    records: list[dict[str, object]] = []
    expected_keys = {
        "schema_version",
        "session_id",
        "observation_sequence",
        "image_bytes",
        "image_1_bytes",
        "image_2_bytes",
        "framebuffer_capture_calls",
        "image_encode_calls",
        "window_visible_at_creation",
        "window_visible",
        "render_world_attempts",
        "render_world_completions",
    }
    for line in payload.splitlines():
        if not line:
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise RuntimeError("structured diagnostic record keys are invalid")
        if value["schema_version"] != "mc2p.structured-observation-diagnostics.v2":
            raise RuntimeError("structured diagnostic schema is invalid")
        if not all(
            type(value[field]) is int and value[field] >= 0
            for field in ("render_world_attempts", "render_world_completions")
        ):
            raise RuntimeError("structured diagnostic render counters are invalid")
        records.append(value)
    if records:
        sessions = {record["session_id"] for record in records}
        sequences = [record["observation_sequence"] for record in records]
        if len(sessions) != 1 or sequences != list(range(1, len(records) + 1)):
            raise RuntimeError("structured diagnostic sequence is not continuous")
    return records


def _formal_keys(value: object) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        for key, child in value.items():
            found.add(str(key))
            found.update(_formal_keys(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.update(_formal_keys(child))
    return found


def _behavior_checks(
    observations: Sequence[ObservationSnapshotV2],
    terminated: Sequence[bool],
    truncated: Sequence[bool],
    source_unchanged: bool,
) -> list[dict[str, object]]:
    sequences = [item.sequence_id for item in observations]
    requests = [item.request_sequence_id for item in observations]
    positions = [_position(item) for item in observations]
    displacements = [
        _horizontal_distance(left, right)
        for left, right in zip(positions, positions[1:])
    ]
    forward = displacements[:4]
    release = displacements[4:]
    forward_net = _horizontal_distance(positions[0], positions[4])
    release_tail = statistics.median(release[-2:]) if len(release) >= 2 else math.inf
    forward_median = statistics.median(forward) if forward else 0.0
    formal_projection = [trace_projection(item) for item in observations]
    forbidden = sorted(_formal_keys(formal_projection).intersection(FORBIDDEN_FORMAL_KEYS))
    return [
        _check("formal_observation_v2", all(item.schema_version == "mc2p.observation.v2" for item in observations), [item.schema_version for item in observations], "all v2"),
        _check("inventory_36_slots", all(item.inventory.value is not None and len(item.inventory.value.main) == 36 for item in observations), [None if item.inventory.value is None else len(item.inventory.value.main) for item in observations], "all 36"),
        _check("fixed_1431_block_rays", all(item.perception.value is not None and item.perception.value.sensor_profile_revision == 3 and len(item.perception.value.block_rays) == 1431 for item in observations), [None if item.perception.value is None else len(item.perception.value.block_rays) for item in observations], "all profile3/1431"),
        _check("bounded_visible_entities", all(item.perception.value is not None and len(item.perception.value.visible_entities) <= 64 for item in observations), [None if item.perception.value is None else len(item.perception.value.visible_entities) for item in observations], "all <= 64"),
        _check("continuous_observation_sequence", sequences == list(range(9)), sequences, list(range(9))),
        _check("continuous_request_sequence", requests == [None] + list(range(8)), requests, [None] + list(range(8))),
        _check("formal_payload_has_no_images", not forbidden, forbidden, []),
        _check("forward_moved", forward_net > 0.1, forward_net, "> 0.1 block"),
        _check("neutral_release_stable", release_tail < forward_median, {"release_tail": release_tail, "forward_median": forward_median}, "release tail < forward median"),
        _check("not_terminated", not any(terminated), list(terminated), "all false"),
        _check("not_truncated", not any(truncated), list(truncated), "all false"),
        _check("alive", not any(_is_dead(item) for item in observations), [_is_dead(item) for item in observations], "all false"),
        _check("source_fingerprints_unchanged", source_unchanged, source_unchanged, True),
    ]


def run_worker(config: StructuredSmokeConfigV0) -> int:
    _validate_config(config)
    diagnostics_path = config.sandbox_path / "run" / DIAGNOSTIC_FILE_NAME
    diagnostics_offset = diagnostics_path.stat().st_size if diagnostics_path.is_file() else 0
    source_root = resolve_mc121_runtime_path()
    source_before = capture_runtime_source_fingerprints(
        source_root, MC121_RUNTIME_0_1_0_FINGERPRINTS
    )
    observations: list[ObservationSnapshotV2] = []
    terminated_values: list[bool] = []
    truncated_values: list[bool] = []
    backend: CraftGroundBackendV0 | None = None
    primary_failure: dict[str, str] | None = None
    cleanup_failures: list[str] = []
    jvm_records: list[dict[str, object]] = []
    started = time.perf_counter_ns()
    deadline_ns = started + round(max(1.0, config.timeout_seconds - 15.0) * 1e9)
    try:
        backend = CraftGroundBackendV0(
            port=config.port,
            runtime_env_path=config.sandbox_path,
            clock_mode=CraftGroundClockModeV0.ACCELERATED,
            observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
        )
        reset = backend.reset(
            ResetRequestV0(
                request_id="structured-reset",
                episode_id=f"seed-{config.seed}",
                scenario_id="flat-safe",
                seed=config.seed,
                deadline_monotonic_ns=deadline_ns,
            )
        )
        if not reset.succeeded or reset.observation is None:
            message = reset.failure.message if reset.failure is not None else "missing reset"
            raise RuntimeError(f"structured reset failed: {message}")
        observations.append(reset.observation)
        append_jsonl(config.run_dir / "observations.jsonl", trace_projection(reset.observation))
        for action in build_action_schedule():
            result = backend.step(action, deadline_ns)
            observations.append(result.observation)
            terminated_values.append(result.terminated)
            truncated_values.append(result.truncated)
            append_jsonl(
                config.run_dir / "observations.jsonl",
                trace_projection(result.observation),
            )
    except BaseException as error:
        primary_failure = {
            "type": type(error).__name__,
            "message": str(error) or type(error).__name__,
        }
        (config.run_dir / "failure.txt").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
    finally:
        if backend is not None:
            try:
                backend.close()
            except Exception as error:
                cleanup_failures.append(f"{type(error).__name__}: {error}")

    cleanup = backend.cleanup_status if backend is not None else None
    try:
        jvm_records = _read_diagnostic_tail(diagnostics_path, diagnostics_offset)
    except Exception as error:
        if primary_failure is None:
            primary_failure = {
                "type": type(error).__name__,
                "message": str(error) or type(error).__name__,
            }
    source_after = capture_runtime_source_fingerprints(
        source_root, MC121_RUNTIME_0_1_0_FINGERPRINTS
    )
    observation_diagnostics = (
        backend.observation_diagnostics if backend is not None else None
    )
    evidence: dict[str, object] = {
        "formal_observation_keys": (
            list(observation_diagnostics.last_raw_keys)
            if observation_diagnostics is not None
            else []
        ),
        "jvm_observations": len(jvm_records),
        "image_bytes": [record["image_bytes"] for record in jvm_records],
        "framebuffer_capture_calls": [
            record["framebuffer_capture_calls"] for record in jvm_records
        ],
        "image_encode_calls": [record["image_encode_calls"] for record in jvm_records],
        "window_visible_at_creation": [
            record["window_visible_at_creation"] for record in jvm_records
        ],
        "window_visible": [record["window_visible"] for record in jvm_records],
        "render_world_attempts": [
            record["render_world_attempts"] for record in jvm_records
        ],
        "render_world_completions": [
            record["render_world_completions"] for record in jvm_records
        ],
        "process_stopped": cleanup is not None and cleanup.process_stopped,
        "port_released": cleanup is not None and cleanup.port_released,
    }
    checks = evaluate_structured_evidence(evidence)
    if len(observations) == EXPECTED_OBSERVATIONS:
        checks.extend(
            _behavior_checks(
                observations,
                terminated_values,
                truncated_values,
                source_before == source_after == dict(MC121_RUNTIME_0_1_0_FINGERPRINTS),
            )
        )
    else:
        checks.append(
            _check(
                "formal_observation_count",
                False,
                len(observations),
                EXPECTED_OBSERVATIONS,
            )
        )
    failed = [str(check["name"]) for check in checks if not check["passed"]]
    if primary_failure is None and failed:
        primary_failure = {
            "type": "StructuredEvidenceFailure",
            "message": f"failed checks: {failed}",
        }
    status = "passed" if primary_failure is None and not cleanup_failures else "failed"
    result = {
        "schema_version": "mc2p.structured-only-smoke.v0",
        "run_id": config.run_id,
        "status": status,
        "evidence": evidence,
        "checks": checks,
        "observation_diagnostics": (
            asdict(observation_diagnostics)
            if observation_diagnostics is not None
            else None
        ),
        "primary_failure": primary_failure,
        "cleanup_failures": cleanup_failures,
        "elapsed_seconds": (time.perf_counter_ns() - started) / 1e9,
    }
    write_json_atomic(config.run_dir / "result.json", result)
    print(strict_json_dumps(result))
    return 0 if status == "passed" else 2


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
        client.settimeout(0.1)
        return client.connect_ex(("127.0.0.1", port)) != 0


def run_parent(config: StructuredSmokeConfigV0) -> int:
    _validate_config(config)
    print(f"CRAFTGROUND_STRUCTURED_ONLY_RUN_DIR={config.run_dir}")
    supervision = run_bounded_process(
        _worker_command(config),
        cwd=PROJECT_ROOT,
        environment=os.environ,
        log_path=config.run_dir / "worker.log",
        timeout_seconds=config.timeout_seconds,
    )
    write_json_atomic(config.run_dir / "supervision.json", trace_projection(supervision))
    if (
        supervision.primary_failure is not None
        or supervision.cleanup_failures
        or not supervision.process_stopped
    ):
        result_path = config.run_dir / "result.json"
        if not result_path.exists():
            write_json_atomic(
                result_path,
                {
                    "schema_version": "mc2p.structured-only-smoke.v0",
                    "run_id": config.run_id,
                    "status": "failed",
                    "primary_failure": {
                        "type": "SupervisionError",
                        "message": (
                            supervision.primary_failure
                            or "; ".join(supervision.cleanup_failures)
                            or "structured-only worker tree did not stop"
                        ),
                    },
                    "cleanup_failures": list(supervision.cleanup_failures),
                    "evidence": {
                        "process_stopped": supervision.process_stopped,
                        "port_released": _port_is_free(config.port),
                    },
                    "checks": [],
                },
            )
        return 1
    try:
        result = json.loads((config.run_dir / "result.json").read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"CRAFTGROUND_STRUCTURED_ONLY_RESULT_ERROR={type(error).__name__}: {error}")
        return 1
    if supervision.return_code == 0 and result.get("status") == "passed":
        print("CRAFTGROUND_STRUCTURED_ONLY_OK")
        return 0
    return supervision.return_code if supervision.return_code in {1, 2, 130} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify real Minecraft structured-only zero-image operation."
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--artifacts-dir", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--run-id", help=argparse.SUPPRESS)
    parser.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--sandbox-path", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.worker:
        if not arguments.run_id or arguments.run_dir is None or arguments.sandbox_path is None:
            raise ValueError("worker requires run id, run directory, and sandbox path")
        return run_worker(
            StructuredSmokeConfigV0(
                run_id=arguments.run_id,
                run_dir=arguments.run_dir.resolve(),
                sandbox_path=arguments.sandbox_path.resolve(),
                seed=arguments.seed,
                port=arguments.port,
                timeout_seconds=arguments.timeout_seconds,
            )
        )

    run_dir = _allocate_run_directory(arguments.artifacts_dir)
    prepared = prepare_runtime_sandbox(
        source_root=resolve_mc121_runtime_path(),
        sandbox_parent=run_dir / "sandboxes",
        sandbox_id="accelerated-structured",
        clock_mode=CraftGroundClockModeV0.ACCELERATED,
        observation_mode=CraftGroundObservationModeV0.STRUCTURED_ONLY,
    )
    return run_parent(
        StructuredSmokeConfigV0(
            run_id=run_dir.name,
            run_dir=run_dir,
            sandbox_path=prepared.path,
            seed=arguments.seed,
            port=arguments.port,
            timeout_seconds=arguments.timeout_seconds,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

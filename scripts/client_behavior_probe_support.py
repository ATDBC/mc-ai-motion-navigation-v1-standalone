"""Use a fingerprint-validated sandbox's declared clock; never silently change modes."""
from pathlib import Path
import math

from mc2p.backends.craftground_behavior import CraftGroundBehaviorBackendV1
from mc2p.backends.craftground_runtime import CraftGroundClockModeV0, CraftGroundObservationModeV0, load_sandbox_manifest
from mc2p.contracts.observation_request_v3 import OBSERVATION_V3
from scripts.timing_parallel_probe_core import LockstepTraceEventV0


def backend_for_sandbox(sandbox: Path, port: int) -> CraftGroundBehaviorBackendV1:
    manifest = load_sandbox_manifest(sandbox)
    if manifest.observation_mode is not CraftGroundObservationModeV0.STRUCTURED_ONLY:
        raise ValueError("formal behavior probe requires structured_only")
    if manifest.observation_schema_version != OBSERVATION_V3:
        raise ValueError("formal behavior probe requires an Observation V3 sandbox manifest")
    # Backend initialization/reset also validates mode, current recipe and source fingerprints.
    return CraftGroundBehaviorBackendV1(port=port, runtime_env_path=sandbox,
        clock_mode=manifest.clock_mode, observation_mode=manifest.observation_mode,
        observation_schema_version=OBSERVATION_V3)


def sandbox_provenance(sandbox: Path) -> dict:
    manifest = load_sandbox_manifest(sandbox)
    if manifest.observation_schema_version != OBSERVATION_V3:
        raise ValueError("formal behavior probe requires an Observation V3 sandbox manifest")
    nonblocking = (manifest.clock_mode is CraftGroundClockModeV0.REFERENCE_20_TPS
        and "install nonblocking reference scheduler v1" in manifest.patch_operations)
    lockstep = (manifest.clock_mode is CraftGroundClockModeV0.LOCKSTEP_ACCELERATED
        and "install nonblocking lockstep scheduler v1" in manifest.patch_operations)
    return {"clock_mode": manifest.clock_mode.value, "observation_mode": manifest.observation_mode.value,
            "observation_schema_version": OBSERVATION_V3, "knowledge_model": "block_state_v1",
            "default_field_profile": "navigation_v1",
            "sandbox": str(sandbox.resolve()), "patch_recipe_fingerprint": manifest.patch_recipe_fingerprint,
            "scheduling_mode": "reference_nonblocking_v1" if nonblocking else (
                "lockstep_nonblocking_v1" if lockstep else "legacy_step_v0")}


def evaluate_input_samples(counts: list[int], *, action_count: int) -> dict:
    return {"name": "one_actual_player_input_sample_per_action",
            "passed": len(counts) == action_count and all(type(v) is int for v in counts)
            and counts == list(range(1, action_count + 1)), "actual": counts}


def compare_control_steps(reference: list[dict], lockstep: list[dict], *, action_count: int) -> dict:
    """Compare local-player per-action controls, NOT entity/server/full-game timing equivalence."""
    mismatches = []
    max_error = 0.0
    if len(reference) != len(lockstep) or len(reference) != action_count:
        mismatches.append("record_count")
    for i, (ref, acc) in enumerate(zip(reference, lockstep)):
        if ref["label"] != acc["label"] or any(ref["receipt"][key] != acc["receipt"][key]
                for key in ("status", "reason", "input_samples")):
            mismatches.append(f"{i}:phase_or_outcome")
        pairs = [(ref["after"]["position"][axis] - ref["before"]["position"][axis],
                  acc["after"]["position"][axis] - acc["before"]["position"][axis]) for axis in ("x", "y", "z")]
        pairs += [(ref["after"][angle], acc["after"][angle]) for angle in ("yaw", "pitch")]
        for a, b in pairs:
            if not math.isfinite(a) or not math.isfinite(b):
                mismatches.append(f"{i}:nonfinite")
                continue
            error = abs(a - b)
            max_error = max(max_error, error)
            if error > 1e-6:
                mismatches.append(f"{i}:control_delta")
    return {"name": "paired_local_player_controls", "passed": not mismatches,
            "action_count": action_count, "maximum_absolute_error": max_error, "mismatches": mismatches}


def evaluate_behavior_lockstep(world_ticks: list[int], events: list[LockstepTraceEventV0], *, action_count: int) -> list[dict]:
    expected = [("server_arm_complete" if i == 0 else "server_tick_complete", i)
                for i in range(action_count + 1)]
    order = [pair for event, generation in expected for pair in ((event, generation), ("client_observation", generation))]
    servers = [e for e in events if e.event in ("server_arm_complete", "server_tick_complete")]
    clients = [e for e in events if e.event == "client_observation"]

    def increments(values):
        return len(values) == action_count + 1 and all(type(v) is int and v >= 0 for v in values) and all(
            b - a == 1 for a, b in zip(values, values[1:]))

    checks = {
        "lockstep_one_session": len({e.session_id for e in events}) == 1,
        "lockstep_causal_order": [(e.event, e.generation) for e in events] == order,
        "lockstep_observation_ticks": increments(world_ticks),
        "lockstep_server_ticks": increments([e.server_world_time for e in servers]),
        "lockstep_client_mapping": len(clients) == action_count + 1
            and [e.client_world_time for e in clients] == world_ticks,
        "lockstep_completion_mapping": len(clients) == len(servers) == action_count + 1
            and [e.server_world_time for e in clients] == [e.server_world_time for e in servers],
    }
    return [{"name": key, "passed": value} for key, value in checks.items()]

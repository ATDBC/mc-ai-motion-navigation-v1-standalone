"""D122-4 shared component/Fabric trial and controlled transport timing probe."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import importlib
import json
import math
from functools import lru_cache
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from mc2p.backends.client_behavior_payload import ACTION_SCHEMA_V2
from mc2p.backends.fabric_behavior import FabricBehaviorBackendV1
from mc2p.contracts.action_v1 import ActionIntentV1, ActionSnapshotV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from scripts.control_probe_core import write_json_atomic

_D1223_PRODUCTION_HASHES = (
    ROOT / "evidence/motion_navigation/d122-client-timing-v1/d122-3/"
    "production-semantic-hashes.json"
)
_D124_PRODUCTION_HASHES = (
    ROOT / "evidence/motion_navigation/d124-proof-dependency-projection-v1/"
    "production-semantic-hashes.json"
)
_D124_D122_SMOKE_HISTORY = (
    ROOT / "evidence/motion_navigation/d124-proof-dependency-projection-v1/"
    "d122/smoke-history.json"
)
_D1224_EVIDENCE = ROOT / "evidence/motion_navigation/d122-client-timing-v1/d122-4"
_SMOKE_SOURCE_RUNS = {
    5: "20261020T113015069455Z-9339b944",
    6: "20261020T113255672053Z-4500b836",
    7: "20261020T113835759757Z-d5255eb7",
    8: "20261020T115158638012Z-bc79106d",
}
_SMOKE_FAILURE_TYPES = {5: "ValueError", 6: "ModuleNotFoundError", 7: "RuntimeError", 8: "RuntimeError"}


POLICIES = ("timely_l1", "late_l2", "never_delivered")
_DIRECTIONS = ("south", "east", "north", "west")
NEVER_FABRIC_RUNTIME_FAILURE = {
    "code": "deadline_exceeded",
    "message": "D122 injected successor transport loss",
    "retryable": True,
    "source": "runtime",
}
NEVER_COMPONENT_RUNTIME_FAILURE = {
    "code": "contract",
    "message": "successor_transport_not_delivered",
    "retryable": True,
    "source": "client_behavior",
}


_FABRIC_RUNNER_DEPENDENCIES = {
    "ActionRoute": ("mc2p.motion_nav.action_route", "ActionRoute"),
    "JumpGapSegment": ("mc2p.motion_nav.action_route", "JumpGapSegment"),
    "AsyncWorkIdentity": ("mc2p.motion_nav.async_work", "AsyncWorkIdentity"),
    "AsyncWorkKind": ("mc2p.motion_nav.async_work", "AsyncWorkKind"),
    "GoalPlanningPolicy": ("mc2p.motion_nav.goal_planning_policy", "GoalPlanningPolicy"),
    "JumpGapEdge": ("mc2p.motion_nav.jump_gap", "JumpGapEdge"),
    "MotionCandidateAdmitter": ("mc2p.motion_nav.motion_candidate", "MotionCandidateAdmitter"),
    "MotionCandidateContext": ("mc2p.motion_nav.motion_candidate", "MotionCandidateContext"),
    "MotionCandidateStatus": ("mc2p.motion_nav.motion_candidate", "MotionCandidateStatus"),
    "VerifiedMotionCandidate": ("mc2p.motion_nav.motion_candidate", "VerifiedMotionCandidate"),
    "VerifiedMotionExecutor": ("mc2p.motion_nav.motion_candidate", "VerifiedMotionExecutor"),
    "TaskDamageBudget": ("mc2p.motion_nav.motion_risk", "TaskDamageBudget"),
    "LandingRegion": ("mc2p.motion_nav.motion_solver", "LandingRegion"),
    "SolveStatus": ("mc2p.motion_nav.motion_solver", "SolveStatus"),
    "VerifiedMotionResult": ("mc2p.motion_nav.motion_solver", "VerifiedMotionResult"),
    "replay_verified_commands": ("mc2p.motion_nav.motion_solver", "_replay_verified_commands"),
    "bind_fixed_successor": ("mc2p.motion_nav.motion_solver", "bind_fixed_successor"),
    "GapMotionSolveResult": ("mc2p.motion_nav.motion_worker", "GapMotionSolveResult"),
    "NavigationSession": ("mc2p.motion_nav.navigation_session", "NavigationSession"),
    "NavigationSessionProfiles": ("mc2p.motion_nav.navigation_session", "NavigationSessionProfiles"),
    "CandidateExecutionWindow": ("mc2p.motion_nav.online_motion", "CandidateExecutionWindow"),
    "PhysicsWorldBounds": ("mc2p.motion_nav.physics_adapter", "PhysicsWorldBounds"),
    "PhysicsWorldView": ("mc2p.motion_nav.physics_adapter", "PhysicsWorldView"),
    "JAVA_1_21_RULESET": ("mc2p.motion_nav.physics_types", "JAVA_1_21_RULESET"),
    "ActiveRoute": ("mc2p.motion_nav.route_admission", "ActiveRoute"),
    "ExecutableCorridor": ("mc2p.motion_nav.route_admission", "ExecutableCorridor"),
    "SupportSurface": ("mc2p.motion_nav.support_surfaces", "SupportSurface"),
    "SurfaceNodeId": ("mc2p.motion_nav.support_surfaces", "SurfaceNodeId"),
    "HorizontalRegion": ("mc2p.motion_nav.support_surfaces", "HorizontalRegion"),
    "seal_snapshot_version": ("mc2p.motion_nav.world_snapshot", "seal_snapshot_version"),
    "RuntimeNavigationDriver": ("mc2p.skills.navigation_session_driver", "RuntimeNavigationDriver"),
    "RuntimeNavigationDriverState": ("mc2p.skills.navigation_session_driver", "RuntimeNavigationDriverState"),
    "PreparedMotionJob": ("mc2p.motion_nav.motion_worker", "PreparedMotionJob"),
    "PreparedSubmitStatus": ("mc2p.motion_nav.prepared_motion", "PreparedSubmitStatus"),
    "execute_job": ("mc2p.motion_nav.motion_worker", "_execute_job"),
    "NavigationObservationAdapter": ("mc2p.motion_nav.runtime_adapter", "NavigationObservationAdapter"),
    "install_manual_route": ("scripts.d121_handoff_execution_probe", "_install_manual_route"),
    "QueuedWorker": ("tests.motion_nav.test_d102_receiver", "QueuedWorker"),
    "fixed_case": ("tests.motion_nav.test_d121_successor_binding", "fixed_case"),
    "CONFIG": ("tests.sim.runner", "CONFIG"),
    "InlinePlannerWorker": ("tests.sim.runner", "InlinePlannerWorker"),
    "goal": ("tests.sim.runner", "_goal"),
    "d118_runtime": ("scripts", "d118_momentum_calibration_runtime"),
    "d120_calibration": ("scripts", "d120_momentum_entry_calibration"),
}


def resolve_fabric_runner_dependencies() -> dict[str, object]:
    """Resolve every deferred runner dependency without starting Java or Minecraft."""
    resolved: dict[str, object] = {}
    for name, (module_name, attribute) in _FABRIC_RUNNER_DEPENDENCIES.items():
        module = importlib.import_module(module_name)
        resolved[name] = getattr(module, attribute)
    return resolved


@lru_cache(maxsize=4)
def _canonical_fabric_dependency_manifest(direction_index: int) -> dict:
    """Derive the exact parent/successor/fallback cells from the frozen proof fixture."""
    from mc2p.motion_nav.motion_solver import SolveStatus, bind_fixed_successor
    from tests.motion_nav.test_d121_successor_binding import fixed_case

    request, fixture, template, successor_spec, snapshot = fixed_case(direction_index)
    bound = bind_fixed_successor(
        template, request, successor_spec, snapshot, damage_remaining_points=0.,
    )
    if bound.status is not SolveStatus.SOLVED or bound.proof.successor_binding is None:
        raise RuntimeError(f"D122 canonical dependency binding failed: {bound}")
    binding = bound.proof.successor_binding
    parent = set(template.world_dependencies)
    fallback = {position for tail in template.fallback_tails
                for position in tail.dependencies}
    successor = {position for position, _ in binding.dependency_versions}
    parent.update(position for position, _ in binding.parent_dependency_versions)
    required = parent | fallback | successor
    facts = dict(snapshot.facts)
    unknown = tuple(sorted(position for position in required
                           if position not in facts or facts[position].knowledge.value == "unknown"))
    return {
        "origin": fixture.context.origin,
        "feet_y": fixture.context.feet_y,
        "trial": fixture.trial,
        "required": tuple(sorted(required)),
        "facts": facts,
        "unknown": unknown,
        "categories": {
            "parent": tuple(sorted(parent)),
            "successor": tuple(sorted(successor)),
            "fallback": tuple(sorted(fallback)),
        },
    }


def fabric_dependency_manifest(direction_index: int, *, origin: tuple[int, int],
                               feet_y: int) -> dict:
    """Translate all frozen proof dependencies to one live fixture location."""
    canonical = _canonical_fabric_dependency_manifest(direction_index)
    dx = origin[0] - canonical["origin"][0]
    dy = feet_y - canonical["feet_y"]
    dz = origin[1] - canonical["origin"][1]
    translate = lambda position: (position[0] + dx, position[1] + dy, position[2] + dz)
    facts = {
        translate(position): {
            "knowledge": canonical["facts"][position].knowledge.value,
            "material": (None if canonical["facts"][position].block is None else
                         canonical["facts"][position].block.material_key),
        }
        for position in canonical["required"] if position in canonical["facts"]
    }
    from scripts import d118_momentum_calibration_runtime as frozen
    legacy = set(frozen._fixture_fact_positions(
        canonical["trial"], origin, feet_y,
    ))
    return {
        "facts": facts,
        "categories": {
            name: tuple(sorted(translate(position) for position in positions))
            for name, positions in canonical["categories"].items()
        },
        "unknown_canonical_facts": tuple(translate(position) for position in canonical["unknown"]),
        "legacy_request_missing_count": len(set(facts) - legacy),
    }


def fabric_observation_positions(trial, *, direction_index: int,
                                 origin: tuple[int, int], feet_y: int) -> tuple[tuple[int, int, int], ...]:
    """Preserve the D118 centerline checks and add every proof dependency."""
    from scripts import d118_momentum_calibration_runtime as frozen
    legacy = set(frozen._fixture_fact_positions(trial, origin, feet_y))
    required = set(fabric_dependency_manifest(
        direction_index, origin=origin, feet_y=feet_y,
    )["facts"])
    return tuple(sorted(legacy | required))


def inspect_fabric_dependency_knowledge(world, manifest: Mapping) -> dict:
    """Report UNKNOWN and mismatches without treating either as AIR."""
    known, unknown, mismatched = [], [], []
    for position, expected in manifest["facts"].items():
        fact = world.cell(position)
        if fact.knowledge.value == "unknown":
            unknown.append(position)
            continue
        material = None if fact.block is None else fact.block.material_key
        if (fact.knowledge.value != expected["knowledge"]
                or material != expected["material"]):
            mismatched.append({
                "position": position,
                "expected": expected,
                "actual": {"knowledge": fact.knowledge.value, "material": material},
            })
            continue
        known.append(position)
    return {
        "required_count": len(manifest["facts"]),
        "known_count": len(known),
        "unknown": tuple(sorted(unknown)),
        "mismatched": tuple(mismatched),
        "categories": {name: len(positions) for name, positions in manifest["categories"].items()},
    }


@dataclass(frozen=True)
class D122FabricTrial:
    direction_index: int
    repetition: int
    policy: str

    def __post_init__(self) -> None:
        if self.direction_index not in range(4):
            raise ValueError("D122 direction is invalid")
        if type(self.repetition) is not int or self.repetition < 0:
            raise ValueError("D122 repetition is invalid")
        if self.policy not in POLICIES:
            raise ValueError("D122 transport policy is invalid")

    @property
    def trial_id(self) -> str:
        return f"d122-{_DIRECTIONS[self.direction_index]}-{self.repetition}-{self.policy}"


def build_trial_plan(*, full_matrix: bool) -> tuple[D122FabricTrial, ...]:
    directions = range(4) if full_matrix else range(1)
    repetitions = range(3) if full_matrix else range(1)
    return tuple(D122FabricTrial(direction, repetition, policy)
                 for direction in directions
                 for repetition in repetitions
                 for policy in POLICIES)


def _safe_body(value: Mapping) -> bool:
    if type(value) is not dict:
        return False
    velocity = value.get("velocity")
    return (value.get("on_ground") is True
            and type(velocity) in (list, tuple) and len(velocity) == 3
            and math.hypot(float(velocity[0]), float(velocity[2])) <= .01)


def _failure_mapping(failure) -> dict | None:
    if failure is None:
        return None
    return {
        "code": failure.code.value,
        "message": failure.message,
        "retryable": failure.retryable,
        "source": failure.source,
    }


def _parent_terminal_body(physics_rows: Iterable[Mapping],
                          parent_sequence: int | None) -> dict | None:
    """Read the fallback terminal body from the exact parent command evidence."""
    if type(parent_sequence) is not int:
        return None
    owned = [row for row in physics_rows
             if row.get("request_sequence_id") == parent_sequence
             and row.get("input_state") == "default_active"
             and type(row.get("post_state")) is dict]
    if not owned:
        return None
    state = owned[-1]["post_state"]
    position = state.get("position")
    velocity = state.get("velocity")
    if type(position) is not dict or type(velocity) is not dict:
        return None
    return {
        "position": [position.get(axis) for axis in ("x", "y", "z")],
        "velocity": [velocity.get(axis) for axis in ("x", "y", "z")],
        "on_ground": state.get("on_ground"),
        "evidence": "parent_physics_post_state",
        "movement_tick_id": owned[-1].get("movement_tick_id"),
        "request_sequence_id": parent_sequence,
    }


def validate_trial_result(row: Mapping) -> tuple[str, ...]:
    """One backend-neutral assertion set for component and real Fabric rows."""
    failures: list[str] = []
    policy = row.get("policy")
    backend = row.get("backend")
    if backend == "calculator" and row.get("raw_case_passed") is not True:
        failures.append("raw_component_case_failed")
    if row.get("action_schema") != ACTION_SCHEMA_V2:
        failures.append("action_schema_not_v2")
    if row.get("reset_succeeded") is not True:
        failures.append("reset_not_succeeded")
    if row.get("start_grounded_still") is not True:
        failures.append("start_not_grounded_still")
    if row.get("parent_default_installed_before_l") is not True:
        failures.append("parent_default_not_installed_before_l")
    if row.get("fallback_armed_tick") != row.get("landing_tick"):
        failures.append("fallback_not_armed_at_l")
    parent_sequence = row.get("parent_request_sequence_id")
    successor_sequence = row.get("successor_request_sequence_id")
    armed_event = row.get("fallback_armed_event")
    if (type(parent_sequence) is not int or type(armed_event) is not dict
            or armed_event.get("request_sequence_id") != parent_sequence):
        failures.append("fallback_armed_not_owned_by_parent")
    window = row.get("successor_window")
    expected = row.get("landing_tick")
    expected = None if type(expected) is not int else expected + 1
    if (type(window) is not dict or window.get("earliest_tick") != expected
            or window.get("latest_tick") != expected):
        failures.append("successor_window_not_l_plus_1")
    successor = row.get("successor_jump_sample_ticks")
    defaults = row.get("default_sample_ticks")
    duration = row.get("default_duration_ticks")
    if type(duration) is not int or duration <= 0:
        failures.append("default_duration_not_positive_int")
    if type(successor) is not list or type(defaults) is not list:
        failures.append("input_samples_missing")
    elif policy == "timely_l1":
        if row.get("successor_reached_client") is not True:
            failures.append("timely_successor_not_at_client")
        if successor != [expected] or defaults:
            failures.append("timely_first_effect_differs")
        if row.get("driver_state") != "success":
            failures.append("timely_driver_not_success")
        if row.get("goal_satisfied") is not True:
            failures.append("timely_goal_not_satisfied")
        if row.get("transport_outcome") != "successor_applied":
            failures.append("timely_transport_outcome_differs")
    elif policy == "late_l2":
        if row.get("successor_reached_client") is not True:
            failures.append("late_successor_not_at_client")
        if successor or len(defaults) != duration:
            failures.append("late_did_not_run_default")
        if row.get("window_missed_tick") != expected + 1:
            failures.append("late_not_rejected_at_l_plus_2")
        missed_event = row.get("window_missed_event")
        if (type(successor_sequence) is not int or type(missed_event) is not dict
                or missed_event.get("request_sequence_id") != successor_sequence):
            failures.append("window_missed_not_owned_by_successor")
        if row.get("transport_outcome") != "window_missed":
            failures.append("late_transport_outcome_differs")
        if row.get("driver_state") != "failed":
            failures.append("late_driver_not_failed")
        if row.get("goal_satisfied") is not False:
            failures.append("late_goal_unexpectedly_satisfied")
    elif policy == "never_delivered":
        if successor or len(defaults) != duration:
            failures.append("never_did_not_run_default")
        if row.get("successor_reached_client") is not False:
            failures.append("never_reached_client")
        attempted = row.get("attempted_successor")
        if type(attempted) is not dict:
            failures.append("never_attempted_successor_missing")
        else:
            if attempted.get("request_sequence_id") != successor_sequence:
                failures.append("never_attempted_successor_identity_differs")
            if attempted.get("first_effect_window") != window:
                failures.append("never_attempted_successor_window_differs")
            if attempted.get("injection_reason") != "successor_transport_not_delivered":
                failures.append("never_transport_reason_differs")
        if row.get("transport_outcome") != "successor_transport_loss":
            failures.append("never_transport_outcome_differs")
        if row.get("driver_state") != "failed":
            failures.append("never_driver_not_failed")
        if row.get("goal_satisfied") is not False:
            failures.append("never_goal_unexpectedly_satisfied")
    else:
        failures.append("unknown_policy")
    if not _safe_body(row.get("final_body", {})):
        failures.append("fallback_terminal_not_safe")
    error = row.get("error")
    if error is not None:
        failures.append("unexpected_trial_error")
    runtime_failure = row.get("runtime_failure")
    expected_runtime_failure = None
    if policy == "never_delivered":
        expected_runtime_failure = (NEVER_FABRIC_RUNTIME_FAILURE
                                    if backend == "fabric"
                                    else NEVER_COMPONENT_RUNTIME_FAILURE)
        expected_runtime_state = "failed" if backend == "fabric" else "ready"
    else:
        expected_runtime_state = "ready"
    if row.get("runtime_state") != expected_runtime_state:
        failures.append("unexpected_runtime_state")
    if runtime_failure != expected_runtime_failure:
        failures.append("unexpected_runtime_failure")
    health = row.get("health_evidence")
    if type(health) is not dict or type(health.get("decreased")) is not bool:
        failures.append("health_evidence_missing")
    elif health["decreased"]:
        failures.append("health_decreased")
    if row.get("source_released") is not True:
        failures.append("source_not_released")
    if row.get("prepared_frame_clear") is not True:
        failures.append("prepared_frame_not_clear")
    if row.get("driver_terminal") is not True:
        failures.append("driver_not_terminal")
    return tuple(failures)


def assemble_trial_result(trial: D122FabricTrial, *, backend: str,
                          reset_succeeded: bool, start_grounded_still: bool,
                          payload: Mapping) -> dict:
    """Shared outer boundary used after component or Fabric setup/run."""
    row = {
        "schema_version": "mc2p.d122-shared-trial.v1",
        "backend": backend,
        "trial_id": trial.trial_id,
        "direction_index": trial.direction_index,
        "repetition": trial.repetition,
        "policy": trial.policy,
        "reset_succeeded": reset_succeeded,
        "start_grounded_still": start_grounded_still,
        **dict(payload),
    }
    failures = validate_trial_result(row)
    return {**row, "passed": not failures, "failures": list(failures)}


def component_trial(trial: D122FabricTrial) -> dict:
    # The calculator harness depends on tests.sim/native (and NumPy). Keep it
    # outside the import surface of the dependency-minimal standalone package.
    from scripts import d122_formal_timing_probe as component
    raw = component.run_case(trial.policy, direction_index=trial.direction_index)
    parent = raw.get("parent_action") or {}
    default = parent.get("safe_default") or {}
    events = raw.get("timing_events", ())
    successor = raw.get("successor_action") or {}
    parent_sequence = parent.get("request_sequence_id")
    successor_sequence = successor.get("request_sequence_id")
    armed_events = [event for event in events
                    if event.get("status") == "fallback_armed"
                    and event.get("request_sequence_id") == parent_sequence]
    missed_events = [event for event in events
                     if event.get("status") == "window_missed"
                     and event.get("request_sequence_id") == successor_sequence]
    transport_events = raw.get("transport_events", ())
    attempted = next((event.get("attempted_request") for event in transport_events
                      if event.get("kind") == "successor_lost_before_client"), None)
    successor_ticks = [sample["movement_tick_id"]
                       for sample in raw.get("successor_samples", ())
                       if sample.get("jump")]
    defaults = [sample["movement_tick_id"]
                for sample in raw.get("default_samples", ())]
    landing = raw.get("L")
    frames = raw.get("frames", ())
    payload = {
        "action_schema": ACTION_SCHEMA_V2,
        "landing_tick": landing,
        "parent_default_installed_before_l": bool(
            parent and len(armed_events) == 1
            and armed_events[0].get("movement_tick_id") == landing
        ),
        "parent_request_sequence_id": parent_sequence,
        "successor_request_sequence_id": successor_sequence,
        "fallback_armed_event": armed_events[0] if len(armed_events) == 1 else None,
        "fallback_armed_tick": (armed_events[0].get("movement_tick_id")
                                if len(armed_events) == 1 else None),
        "default_duration_ticks": default.get("valid_for_ticks"),
        "successor_window": successor.get("first_effect_window"),
        "successor_reached_client": trial.policy != "never_delivered",
        "successor_jump_sample_ticks": successor_ticks,
        "window_missed_event": missed_events[0] if len(missed_events) == 1 else None,
        "window_missed_tick": (missed_events[0].get("movement_tick_id")
                               if len(missed_events) == 1 else None),
        "default_sample_ticks": defaults,
        "health_evidence": {
            "source": "calculator_final_state",
            "decreased": raw.get("final_body", {}).get("damage") != 0.0,
        },
        "final_body": raw.get("final_body"),
        "source_released": raw.get("source_released"),
        "prepared_frame_clear": raw.get("prepared_frame") is False,
        "driver_terminal": raw.get("driver_state") in {
            "success", "failed", "stopped", "cancelled",
        },
        "runtime_state": raw.get("runtime_state"),
        "runtime_failure": raw.get("runtime_failure"),
        "driver_state": raw.get("driver_state"),
        "goal_satisfied": raw.get("goal_satisfied"),
        "error": raw.get("error"),
        "attempted_successor": attempted,
        "transport_outcome": (
            "successor_applied" if trial.policy == "timely_l1"
            else "window_missed" if trial.policy == "late_l2"
            else "successor_transport_loss"
        ),
        "transport_events": list(transport_events),
        "raw_case_passed": raw.get("passed"),
        "synchronous_timings": raw.get("synchronous_timings", ()),
        "frame_count": len(frames),
    }
    return assemble_trial_result(
        trial, backend="calculator", reset_succeeded=True,
        start_grounded_still=True, payload=payload,
    )


def component_preflight() -> dict:
    trials = build_trial_plan(full_matrix=False)
    rows = [component_trial(trial) for trial in trials]
    return {
        "schema_version": "mc2p.d122-component-outer-preflight.v1",
        "result": "PASS" if all(row["passed"] for row in rows) else "FAILED_STOPPED",
        "trials": rows,
    }


def _live_physics_rows(directory: Path) -> tuple[dict, ...]:
    rows: list[dict] = []
    if not directory.exists():
        return ()
    for path in sorted(directory.glob("segment-*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except (ValueError, TypeError):
                continue
            if type(value) is dict:
                rows.append(value)
    return tuple(rows)


class D122FabricBehaviorBackend(FabricBehaviorBackendV1):
    """Formal Fabric backend with a bounded successor-only transport fault."""

    def __init__(self, *args, physics_directory: Path, **kwargs):
        super().__init__(*args, requested_action_schema=ACTION_SCHEMA_V2, **kwargs)
        self._d122_physics_directory = Path(physics_directory)
        self._d122_policy = "timely_l1"
        self._d122_parent_action: ActionSnapshotV1 | None = None
        self.d122_transport_events: list[dict] = []
        self.d122_steps: list[dict] = []

    def select_d122_policy(self, policy: str) -> None:
        if policy not in POLICIES:
            raise ValueError("D122 transport policy is invalid")
        self._d122_policy = policy
        self._d122_parent_action = None
        self.d122_transport_events.append({"kind": "policy_selected", "policy": policy})

    @staticmethod
    def _successor(action: ActionSnapshotV1) -> bool:
        window = action.first_effect_window
        return window is not None and action.movement.jump

    def _wait_for(self, predicate: Callable[[tuple[dict, ...]], bool], deadline_ns: int) -> tuple[dict, ...]:
        while time.perf_counter_ns() < deadline_ns:
            rows = _live_physics_rows(self._d122_physics_directory)
            if predicate(rows):
                return rows
            time.sleep(.001)
        raise TimeoutError("D122 controlled transport wait exceeded its action deadline")

    def step(self, action, deadline_monotonic_ns, *, observation_request=None):
        if action.safe_default is not None:
            self._d122_parent_action = action
        successor = self._successor(action)
        if successor and self._d122_policy == "late_l2":
            target = action.first_effect_window.latest_tick
            self._wait_for(lambda rows: any(row.get("movement_tick_id") >= target for row in rows),
                           deadline_monotonic_ns)
            self.d122_transport_events.append({
                "kind": "successor_released_after_l_plus_1",
                "sequence": action.request_sequence_id,
                "observed_tick": target,
            })
        elif successor and self._d122_policy == "never_delivered":
            parent = self._d122_parent_action
            if parent is None or parent.safe_default is None:
                raise RuntimeError("D122 never fault has no armed parent default")
            duration = parent.safe_default.valid_for_ticks
            parent_sequence = parent.request_sequence_id
            rows = self._wait_for(
                lambda values: sum(
                    row.get("request_sequence_id") == parent_sequence
                    and row.get("input_state") == "default_active"
                    for row in values
                ) >= duration,
                deadline_monotonic_ns,
            )
            ticks = [row["movement_tick_id"] for row in rows
                     if row.get("request_sequence_id") == parent_sequence
                     and row.get("input_state") == "default_active"]
            self.d122_transport_events.append({
                "kind": "successor_lost_before_client",
                "sequence": action.request_sequence_id,
                "default_ticks_observed": ticks[-duration:],
                "attempted_request": {
                    "request_sequence_id": action.request_sequence_id,
                    "first_effect_window": asdict(action.first_effect_window),
                    "injection_reason": "successor_transport_not_delivered",
                },
            })
            raise TimeoutError("D122 injected successor transport loss")
        started = time.perf_counter_ns()
        result = super().step(action, deadline_monotonic_ns,
                              observation_request=observation_request)
        self.d122_steps.append({
            "request_sequence_id": action.request_sequence_id,
            "first_effect_window": (None if action.first_effect_window is None
                                    else asdict(action.first_effect_window)),
            "safe_default": (None if action.safe_default is None
                             else asdict(action.safe_default)),
            "elapsed_ns": time.perf_counter_ns() - started,
            "receipt": asdict(result.receipt),
        })
        return result


def _fixture_action_step(runtime, episode: str, source: str, deadline_ns: int,
                         request: ObservationRequestV3):
    runtime.cancel_source(source)
    now = time.perf_counter_ns()
    runtime.submit_intent(ActionIntentV1(
        f"{source}-{runtime.observation.sequence_id}", source, episode,
        runtime.observation.sequence_id, ActionPriorityV0.TASK, now,
        min(deadline_ns, now + 750_000_000), movement=MovementV1(),
        valid_for_ticks=1,
    ))
    task = TaskIntentV0(
        source, "d122_fixture", "{}",
        (SuccessCriterionV0("fixture_step", ComparisonOperatorV0.EQUAL, 1, "count"),),
        200, deadline_ns, True, 0.0,
    )
    result = runtime.step(task, BehaviorProfileV0(), min(deadline_ns, now + 5_000_000_000),
                          observation_request=request)
    if result.observation is None or result.backend_result is None:
        raise RuntimeError(f"D122 fixture step failed: {result.report}")
    return result


class _FabricExecutionCase:
    """One explicit D121 route anchored to the observed Fabric start state."""

    def __init__(self, runtime, backend, episode: str, *, direction_index: int,
                 origin: tuple[int, int], feet_y: int, deadline_ns: int):
        from dataclasses import replace
        from mc2p.motion_nav.action_route import ActionRoute, JumpGapSegment
        from mc2p.motion_nav.async_work import AsyncWorkIdentity, AsyncWorkKind
        from mc2p.motion_nav.goal_planning_policy import GoalPlanningPolicy
        from mc2p.motion_nav.jump_gap import JumpGapEdge
        from mc2p.motion_nav.motion_candidate import (
            MotionCandidateAdmitter, MotionCandidateContext, MotionCandidateStatus,
            VerifiedMotionCandidate, VerifiedMotionExecutor,
        )
        from mc2p.motion_nav.motion_risk import TaskDamageBudget
        from mc2p.motion_nav.motion_solver import (
            LandingRegion, SolveStatus, VerifiedMotionResult,
            _replay_verified_commands, bind_fixed_successor,
        )
        from mc2p.motion_nav.motion_worker import GapMotionSolveResult
        from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
        from mc2p.motion_nav.online_motion import CandidateExecutionWindow
        from mc2p.motion_nav.physics_adapter import PhysicsWorldBounds, PhysicsWorldView
        from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
        from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
        from mc2p.motion_nav.support_surfaces import SupportSurface, SurfaceNodeId, HorizontalRegion
        from mc2p.motion_nav.world_snapshot import seal_snapshot_version
        from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
        from scripts.d121_handoff_execution_probe import _install_manual_route
        from tests.motion_nav.test_d102_receiver import QueuedWorker
        from tests.motion_nav.test_d121_successor_binding import fixed_case
        from tests.sim.runner import CONFIG, InlinePlannerWorker, _goal
        from scripts import d120_momentum_entry_calibration as calibration

        self.runtime, self.backend = runtime, backend
        self.worker = QueuedWorker()
        task_id = f"d122-fabric-{direction_index}-{runtime.observation.sequence_id}"
        self.session = NavigationSession(
            task_id, NavigationSessionProfiles.load(CONFIG),
            planner_worker=InlinePlannerWorker(), motion_worker=self.worker,
            clock_ns=time.perf_counter_ns,
        )
        self.session._motion_result_inbox.attach_worker(self.worker)
        self.driver = RuntimeNavigationDriver(
            runtime, self.session, clock_ns=time.perf_counter_ns,
            step_window_ns=1_000_000_000,
        )
        goal_x, goal_z = calibration.frozen._local_to_world(
            origin, direction_index, .5, 6.5,
        )
        self.driver.start(
            "fixed-goal", 1, _goal((goal_x, feet_y, goal_z)),
            time.perf_counter_ns(),
            planning_policy=GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND,
        )
        frame = self.session._frame
        self.anchor = self.session.execution_anchor(
            runtime.observation, runtime.input_ledger,
        )
        self.world = PhysicsWorldView(frame.world, JAVA_1_21_RULESET)
        base_request, _base_fixture, template, base_spec, _ = fixed_case(direction_index)
        first = tuple(calibration.frozen._local_to_world(
            origin, direction_index, x, z,
        ) for x in (.3, .7) for z in (1., 2.))
        target = tuple(calibration.frozen._local_to_world(
            origin, direction_index, x, z,
        ) for x in (.3, .7) for z in (6.3, 11.7))
        first_region = LandingRegion(
            min(x for x, _ in first), max(x for x, _ in first),
            min(z for _, z in first), max(z for _, z in first), float(feet_y),
        )
        target_region = LandingRegion(
            min(x for x, _ in target), max(x for x, _ in target),
            min(z for _, z in target), max(z for _, z in target), float(feet_y),
        )
        start_tick = self.anchor.movement_tick_id + 1
        window = CandidateExecutionWindow(start_tick, start_tick)
        # _local_to_world returns X/Z. Keep the support height explicit.
        landing_x, landing_z = calibration.frozen._local_to_world(
            origin, direction_index, .5, 2.05023666079027,
        )
        continuation = replace(base_request.continuation, entry_window=replace(
            base_request.continuation.entry_window,
            reference_point=(landing_x, float(feet_y), landing_z),
        ))
        spec = replace(base_spec, landing=target_region)
        request = replace(
            base_request, landing=first_region, execution_window=window,
            continuation=continuation, fixed_successor_spec=spec,
        )
        primary, failure = _replay_verified_commands(
            self.anchor.physics_state, template.commands, self.world, request,
            start_tick=start_tick,
        )
        if failure is not None:
            raise RuntimeError(f"D122 Fabric first proof replay failed: {failure}")
        proof = replace(
            template, entry_state=self.anchor.physics_state,
            tick_inputs=primary.tick_inputs, trajectory=primary.trajectory,
            step_events=primary.step_events, exit_state=primary.exit_state,
            landing=first_region,
            release_safe_command_indices=primary.release_safe_command_indices,
            world_dependencies=primary.world_dependencies,
            resource_incomplete_reasons=primary.resource_incomplete_reasons,
            execution_window=window, continuation=continuation,
            fallback_tails=primary.fallback_tails,
            successor_binding=None,
        )
        self.source = frame.world._owner
        snapshot = seal_snapshot_version(
            self.source.borrow_snapshot_version(
                PhysicsWorldBounds(origin[0] - 14, origin[0] + 14,
                                   feet_y - 4, feet_y + 6,
                                   origin[1] - 14, origin[1] + 14),
                ruleset=JAVA_1_21_RULESET,
            ), JAVA_1_21_RULESET,
        )
        self.bound = bind_fixed_successor(
            proof, request, spec, snapshot, damage_remaining_points=0.,
        )
        if self.bound.status is not SolveStatus.SOLVED:
            raise RuntimeError(f"D122 Fabric successor binding failed: {self.bound}")
        self.proof = self.bound.proof
        surfaces = []
        for local_z in (-6, 1, 6):
            x, z = calibration.frozen._local_to_world(
                origin, direction_index, .5, local_z + .5,
            )
            cx, cz = math.floor(x), math.floor(z)
            surfaces.append(SupportSurface(
                SurfaceNodeId(cx, cz, feet_y, 0), (x, float(feet_y), z),
                HorizontalRegion(cx, cz, cx + 1, cz + 1), 1.,
                ("minecraft:grass_block",), (),
            ))
        actions = tuple(JumpGapSegment(
            JumpGapEdge(a.node_id, b.node_id, "d122-fabric-explicit-proof", .9, ()),
            a, b, (),
        ) for a, b in zip(surfaces, surfaces[1:]))
        self.route = ActiveRoute(
            "fixed-route", 1, self.session._request.request_id, "fixed-goal", 1,
            frame.session.value, None, 12., 0., (),
            ExecutableCorridor(tuple(s.node_id for s in surfaces), (), 12., surfaces[-1].node_id),
            ActionRoute("fixed-route", actions),
            planning_generation=self.session._request.sequence,
        )
        _install_manual_route(
            self.session, self.route, frame, self.anchor, runtime.input_ledger,
        )
        self.coordinator = self.session._coordinator
        self.controller = VerifiedMotionExecutor()
        self.context = MotionCandidateContext(
            self.route.source_request_id, self.route.planning_generation,
            self.route.goal_id, 1, self.route.route_id, 1, 0, start_tick,
            TaskDamageBudget(), self.proof.resource_incomplete_reasons,
        )
        arguments = asdict(self.context)
        arguments.pop("accepted_resource_incomplete_reasons")
        arguments["damage_budget"] = self.context.damage_budget
        arguments.update(intended_start_tick=start_tick, changed_cells=())
        admitted = MotionCandidateAdmitter().admit_committed(
            VerifiedMotionCandidate(self.proof, self.context), self.anchor,
            executor=self.controller, **arguments,
        )
        if admitted.status is not MotionCandidateStatus.ACCEPTED:
            raise RuntimeError(f"D122 Fabric parent admission failed: {admitted}")
        self.session._executor.install_verified_motion(admitted.candidate)
        self.identity = AsyncWorkIdentity(
            self.session.current_computation_scope,
            self.coordinator._owner_instance_id, AsyncWorkKind.MOTION_SOLVE,
            "fabric-current", 1,
        )
        self.request = request
        submitted = self.coordinator.prepare_fixed_successor(
            GapMotionSolveResult("fabric-current", 1, self.bound, 0, self.identity),
            self.request, self.context, self.anchor, self.world,
            ledger_identity=runtime.input_ledger.ledger_identity,
            expected_work_identity=self.identity,
        )
        from mc2p.motion_nav.prepared_motion import PreparedSubmitStatus
        from mc2p.motion_nav.motion_worker import PreparedMotionJob, _execute_job
        if submitted.status is not PreparedSubmitStatus.ACCEPTED:
            raise RuntimeError(f"D122 Fabric prepared submit failed: {submitted}")
        if not self.worker.submitted.wait(10):
            raise RuntimeError("D122 Fabric prepared worker did not accept the job")
        if not self.session._motion_result_inbox.wait_for_receiver(timeout=10):
            raise RuntimeError("D122 Fabric prepared receiver did not seal the job")
        job = next(job for job in self.worker.jobs if type(job) is PreparedMotionJob)
        self.worker.put(_execute_job(job))
        if not self.session._motion_result_inbox.wait_for_receiver(timeout=10):
            raise RuntimeError("D122 Fabric prepared result was not received")

    def tick(self):
        return self.driver.tick(
            BehaviorProfileV0(), time.perf_counter_ns() + 1_000_000_000,
        )

    def close(self) -> None:
        self.session.close()


def run_fabric_runtime(runtime, backend: D122FabricBehaviorBackend, episode: str,
                       directory: Path, deadline_ns: int, fixture_writer,
                       player_teleporter, *, direction_index: int,
                       repetition: int) -> tuple[dict, list[dict], list[dict]]:
    """Run timely/late/never with shared assembly; never is last and ends Runtime."""
    dependencies = resolve_fabric_runner_dependencies()
    NavigationObservationAdapter = dependencies["NavigationObservationAdapter"]
    RuntimeNavigationDriverState = dependencies["RuntimeNavigationDriverState"]
    frozen = dependencies["d118_runtime"]
    calibration = dependencies["d120_calibration"]

    if backend.negotiated_action_schema != ACTION_SCHEMA_V2:
        raise RuntimeError("D122 Fabric session did not negotiate action v2")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    fixture = calibration.build_fixture(direction_index)
    frame = NavigationObservationAdapter().ingest(runtime.observation)
    request = ObservationRequestV3("navigation_v1", ())
    setup = 0
    source = "d122-fixture-control"
    while type(frame.body.movement_tick_id) is not int or frame.body.movement_tick_id < 0:
        result = _fixture_action_step(runtime, episode, source, deadline_ns, request)
        frame = NavigationObservationAdapter().ingest(result.observation)
        setup += 1
        if setup > 5:
            raise RuntimeError("D122 setup has no movement tick")
    feet_y = frozen._bounded_fixture_feet_y(frame.body.position[1])
    origin = (math.floor(frame.body.position[0]) + 24,
              math.floor(frame.body.position[2]) + 24)
    trials = []
    for policy in POLICIES:
        trial = D122FabricTrial(direction_index, repetition, policy)
        for row in frozen.trial_fixture_rows(fixture.trial, origin=origin, feet_y=feet_y):
            fixture_writer(
                tuple(tuple(position) for position in row["positions"]), row["material"],
                trial_id=trial.trial_id, phase=row["phase"], origin=origin,
                feet_y=feet_y,
            )
        local_x, _, local_z = fixture.trial.start_local
        start_x, start_z = frozen._local_to_world(
            origin, direction_index, local_x, local_z,
        )
        yaw = frozen._DIRECTIONS[direction_index][2]
        request = ObservationRequestV3(
            "navigation_v1",
            fabric_observation_positions(
                fixture.trial, direction_index=direction_index,
                origin=origin, feet_y=feet_y,
            ),
        )
        starting = frame.body
        player_teleporter(start_x, float(feet_y), start_z, yaw, 0.)

        def advance_teleport():
            nonlocal frame
            result = _fixture_action_step(runtime, episode, source, deadline_ns, request)
            frame = NavigationObservationAdapter().ingest(result.observation)
            return frame.body

        frozen._wait_for_b10_teleport_settlement(
            advance_teleport,
            starting_movement_tick=starting.movement_tick_id,
            starting_position=starting.position,
            starting_yaw_degrees=math.degrees(starting.yaw_radians),
            starting_on_ground=starting.is_on_ground,
            target_position=(start_x, float(feet_y), start_z),
            yaw_degrees=yaw, position_tolerance=.03,
        )
        speed = math.hypot(frame.body.velocity_blocks_per_second[0],
                           frame.body.velocity_blocks_per_second[2])
        start_ok = frame.body.is_on_ground and speed <= .03
        if not start_ok:
            raise RuntimeError("D122 trial start is not grounded and still")
        frozen._assert_observed_fixture(frame, fixture.trial, origin, feet_y)
        dependency_manifest = fabric_dependency_manifest(
            direction_index, origin=origin, feet_y=feet_y,
        )
        dependency_precheck = inspect_fabric_dependency_knowledge(
            frame.world, dependency_manifest,
        )
        if dependency_precheck["unknown"] or dependency_precheck["mismatched"]:
            raise RuntimeError(
                f"D122 proof dependency precheck failed: {dependency_precheck}"
            )
        runtime.cancel_source(source)
        backend.select_d122_policy(policy)
        case = _FabricExecutionCase(
            runtime, backend, episode, direction_index=direction_index,
            origin=origin, feet_y=feet_y, deadline_ns=deadline_ns,
        )
        start_health = frame.body.health_points
        start_step_index = len(backend.d122_steps)
        start_transport_index = len(backend.d122_transport_events)
        error = None
        runtime_failure = None
        frame_rows = []
        try:
            for _ in range(90):
                try:
                    result = case.tick()
                except Exception as exception:
                    error = {"type": type(exception).__name__, "message": str(exception)}
                    case.driver.release("d122-fabric-transport-ended")
                    break
                decision = result.decision
                if result.report.failure is not None:
                    runtime_failure = _failure_mapping(result.report.failure)
                frame_rows.append({
                    "request_sequence_id": decision.action.request_sequence_id,
                    "selected_intents": list(decision.selected_intents),
                    "driver_state": case.driver.state.value,
                    "owner": (None if case.session._supervisor.route is None
                              else case.session._supervisor.route.owner_id),
                })
                if result.report.failure is not None and runtime.state.value != "ready":
                    case.driver.release("d122-fabric-runtime-failure")
                    # PlayerRuntimeV1 converted the backend exception into the
                    # terminal typed result above. Do not call a sealed Runtime
                    # again while collecting post-failure sidecar evidence.
                    break
                if case.driver.state is RuntimeNavigationDriverState.SUCCESS:
                    case.driver.release("d122-fabric-complete")
                if case.driver.source is None:
                    break
            if case.driver.source is not None:
                case.driver.release("d122-fabric-frame-limit")
            steps = backend.d122_steps[start_step_index:]
            parent = next((row for row in steps if row["safe_default"] is not None), None)
            successor = next((row for row in steps if row["first_effect_window"] is not None), None)
            timing = [event for row in steps for event in row["receipt"].get("timing_events", ())]
            parent_seq = None if parent is None else parent["request_sequence_id"]
            transport_events = backend.d122_transport_events[start_transport_index:]
            attempted = next((event.get("attempted_request") for event in transport_events
                              if event.get("kind") == "successor_lost_before_client"), None)
            successor_seq = (successor["request_sequence_id"] if successor is not None
                             else None if attempted is None
                             else attempted.get("request_sequence_id"))
            successor_window = (successor["first_effect_window"] if successor is not None
                                else None if attempted is None
                                else attempted.get("first_effect_window"))
            armed_events = [event for event in timing
                            if event.get("status") == "fallback_armed"
                            and event.get("request_sequence_id") == parent_seq]
            missed_events = [event for event in timing
                             if event.get("status") == "window_missed"
                             and event.get("request_sequence_id") == successor_seq]
            physics = _live_physics_rows(backend._d122_physics_directory)
            defaults = [row["movement_tick_id"] for row in physics
                        if row.get("request_sequence_id") == parent_seq
                        and row.get("input_state") == "default_active"]
            jumps = [row["movement_tick_id"] for row in physics
                     if row.get("request_sequence_id") == successor_seq
                     and row.get("actual_input", {}).get("jump") is True]
            landing = None if parent is None else parent["safe_default"]["activation_tick"] - 1
            current = (runtime.observation.self_state.value
                       if runtime.observation is not None else None)
            if policy == "never_delivered":
                # Runtime intentionally fails on transport loss and no longer
                # admits observations. The old Runtime observation predates
                # the fallback, so only the exact parent sidecar proves body
                # state. Health remains unproved until a later formal read.
                final_body = _parent_terminal_body(physics, parent_seq)
                health_evidence = None
            else:
                velocity = None if current is None else list(current.velocity_blocks_per_second)
                final_body = {
                    "position": None if current is None else list(current.position),
                    "velocity": velocity,
                    "on_ground": None if current is None else current.is_on_ground,
                    "evidence": "runtime_observation",
                }
                health = None if current is None else current.health_points
                health_evidence = ({
                    "source": "runtime_observation",
                    "decreased": health is None or start_health is None or health < start_health,
                } if current is not None else None)
            payload = {
                "action_schema": backend.negotiated_action_schema,
                "landing_tick": landing,
                "parent_default_installed_before_l": bool(
                    parent and len(armed_events) == 1
                    and armed_events[0].get("movement_tick_id") == landing
                ),
                "parent_request_sequence_id": parent_seq,
                "successor_request_sequence_id": successor_seq,
                "fallback_armed_event": (armed_events[0]
                                         if len(armed_events) == 1 else None),
                "fallback_armed_tick": (armed_events[0].get("movement_tick_id")
                                        if len(armed_events) == 1 else None),
                "default_duration_ticks": (None if parent is None else
                                           parent["safe_default"]["valid_for_ticks"]),
                "successor_window": successor_window,
                "successor_reached_client": (policy != "never_delivered" and successor is not None),
                "successor_jump_sample_ticks": jumps,
                "window_missed_event": (missed_events[0]
                                        if len(missed_events) == 1 else None),
                "window_missed_tick": (missed_events[0].get("movement_tick_id")
                                       if len(missed_events) == 1 else None),
                "default_sample_ticks": defaults,
                "health_evidence": health_evidence,
                "final_body": final_body,
                "source_released": case.driver.source is None,
                "prepared_frame_clear": not case.driver.has_prepared_frame,
                "driver_terminal": case.driver.state in {
                    RuntimeNavigationDriverState.SUCCESS,
                    RuntimeNavigationDriverState.FAILED,
                    RuntimeNavigationDriverState.STOPPED,
                    RuntimeNavigationDriverState.CANCELLED,
                },
                "runtime_state": runtime.state.value,
                "runtime_failure": runtime_failure,
                "driver_state": case.driver.state.value,
                "goal_satisfied": case.driver.state is RuntimeNavigationDriverState.SUCCESS,
                "error": error,
                "attempted_successor": attempted,
                "transport_outcome": (
                    "successor_applied" if policy == "timely_l1"
                    else "window_missed" if policy == "late_l2"
                    else "successor_transport_loss"
                ),
                "dependency_precheck": dependency_precheck,
                "frames": frame_rows,
                "backend_steps": steps,
                "transport_events": transport_events,
            }
            row = assemble_trial_result(
                trial, backend="fabric", reset_succeeded=True,
                start_grounded_still=start_ok, payload=payload,
            )
            failures = tuple(row["failures"])
            trials.append(row)
            write_json_atomic(directory / f"{trial.trial_id}.json", row)
            if failures:
                raise RuntimeError(f"D122 trial failed: {trial.trial_id}: {failures}")
        finally:
            case.close()
        if policy != "never_delivered":
            frame = NavigationObservationAdapter().ingest(runtime.observation)
    runtime.cancel_source(source)
    summary = {
        "schema_version": "mc2p.d122-fabric-runtime.v1",
        "status": "passed" if len(trials) == 3 and all(row["passed"] for row in trials) else "failed",
        "direction_index": direction_index,
        "repetition": repetition,
        "trial_count": len(trials),
        "trials": trials,
    }
    write_json_atomic(directory / "d122-runtime.json", summary)
    checks = [{"name": "d122_shared_trial_" + row["policy"], "passed": row["passed"]}
              for row in trials]
    return summary, trials, checks


def fabric_probe_command(*, server_port: int, ipc_port: int,
                         full_matrix: bool = False) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "scripts/probe_fabric_deployment_observation.py"),
        "--seed", "21001",
        "--server-port", str(server_port),
        "--ipc-port", str(ipc_port),
        "--d122-client-timing-probe",
        "--time-diagnostics",
        "--physics-tick-diagnostics",
        "--ftick-diagnostics",
        "--timeout-seconds", "1200" if full_matrix else "600",
    ]
    if full_matrix:
        command.append("--d122-full-matrix")
    return command


def _run_directory(stdout: str) -> Path | None:
    prefix = "FABRIC_DEPLOYMENT_RUN_DIR="
    values = [Path(line[len(prefix):].strip()) for line in stdout.splitlines()
              if line.startswith(prefix)]
    return values[0] if len(values) == 1 else None


def preflight_reference(path: Path) -> dict:
    payload = json.loads(path.read_text("utf-8"))
    if (payload.get("schema") != "mc2p.d122-fabric-preflight.v2"
            or payload.get("status") != "PASS"):
        raise ValueError("D122 Fabric preflight is not a signed PASS")
    frozen = _production_semantic_binding()
    if payload.get("production_semantics") != frozen:
        raise ValueError("D122 Fabric preflight does not bind the current production chain")
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "status": payload["status"],
        "production_semantics": frozen,
    }


def _production_semantic_binding() -> dict:
    historical = json.loads(_D1223_PRODUCTION_HASHES.read_text("utf-8"))
    record = json.loads(_D124_PRODUCTION_HASHES.read_text("utf-8"))
    historical_files = historical.get("files")
    current_files = record.get("files")
    files = ({**historical_files, **current_files}
             if isinstance(historical_files, dict) and isinstance(current_files, dict)
             else None)
    if (historical.get("schema") != "mc2p.public-production-semantic-hashes.v1"
            or historical.get("status") != "current"
            or historical.get("normalization") != "CRLF_TO_LF"
            or type(historical_files) is not dict or len(historical_files) != 27
            or record.get("schema") != "mc2p.public-production-semantic-hashes.v1"
            or record.get("status") != "current"
            or record.get("normalization") != "CRLF_TO_LF"
            or type(current_files) is not dict
            or record.get("previous_d122_current_record_sha256")
            != hashlib.sha256(_D1223_PRODUCTION_HASHES.read_bytes()).hexdigest()
            or type(files) is not dict):
        raise ValueError("D124 production semantic record does not continue D122-3")
    actual = {}
    for name, expected in sorted(files.items()):
        path = ROOT / name
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if digest != expected:
            raise ValueError(f"D124 production semantic drift: {name}")
        actual[name] = digest
    return {
        "record": _D124_PRODUCTION_HASHES.relative_to(ROOT).as_posix(),
        "record_sha256": hashlib.sha256(_D124_PRODUCTION_HASHES.read_bytes()).hexdigest(),
        "previous_record": _D1223_PRODUCTION_HASHES.relative_to(ROOT).as_posix(),
        "previous_record_sha256": hashlib.sha256(_D1223_PRODUCTION_HASHES.read_bytes()).hexdigest(),
        "normalization": "CRLF_TO_LF",
        "file_count": len(actual),
        "files": actual,
    }


def d122_smoke_history_rows(*, evidence_root: Path = _D1224_EVIDENCE,
                            artifacts_root: Path = ROOT / "artifacts/fabric-deployment") -> list[dict]:
    """Read the fixed v1-v8 history without trusting a regenerated hash table."""
    source_results_available = all(
        (artifacts_root / run / "result.json").is_file()
        for run in _SMOKE_SOURCE_RUNS.values()
    )
    if not source_results_available:
        compact = json.loads(_D124_D122_SMOKE_HISTORY.read_text("utf-8"))
        if compact.get("schema") != "mc2p.d122-smoke-history.v1":
            raise ValueError("D122 compact smoke history schema differs")
        rows = compact.get("rows")
        if type(rows) is not list:
            raise ValueError("D122 compact smoke history rows are unavailable")
        return [dict(row) for row in rows]
    rows = []
    for version in range(1, 9):
        summary_path = evidence_root / f"fabric-smoke-v{version}" / "summary.json"
        summary = json.loads(summary_path.read_text("utf-8"))
        source = summary.get("source_run")
        source_name = None if source is None else Path(source).name
        source_result = None
        trial_count = 0
        if source_name is not None:
            source_result = json.loads((artifacts_root / source_name / "result.json").read_text("utf-8"))
            trial_dir = artifacts_root / source_name / "client-0/d122-client-timing"
            trial_count = len(tuple(trial_dir.glob("d122-*.json"))) if trial_dir.exists() else 0
        rows.append({
            "version": version,
            "status": summary.get("status"),
            "return_code": summary.get("return_code"),
            "source_run": source_name,
            "source_status": None if source_result is None else source_result.get("status"),
            "failure_type": (None if source_result is None else
                             (source_result.get("primary_failure") or {}).get("type")),
            "trial_count": trial_count,
        })
    return rows


def validate_d122_smoke_history_rows(rows: Iterable[Mapping]) -> tuple[str, ...]:
    failures = []
    indexed = {row.get("version"): row for row in rows}
    if set(indexed) != set(range(1, 9)):
        return ("v1_v8_history_versions_differ",)
    for version in range(1, 9):
        row = indexed[version]
        if row.get("status") != "failed" or row.get("return_code") != 1:
            failures.append(f"v{version}_launch_boundary_differs")
        expected_source = _SMOKE_SOURCE_RUNS.get(version)
        if row.get("source_run") != expected_source:
            failures.append(f"v{version}_source_run_differs")
        if version <= 4:
            if row.get("source_status") is not None or row.get("failure_type") is not None:
                failures.append(f"v{version}_unexpected_source_result")
        else:
            if (row.get("source_status") != "failed"
                    or row.get("failure_type") != _SMOKE_FAILURE_TYPES[version]):
                failures.append(f"v{version}_source_failure_differs")
        if row.get("trial_count") != 0:
            failures.append(f"v{version}_trial_count_differs")
    return tuple(failures)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component-preflight", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--server-port", type=int, default=25597)
    parser.add_argument("--ipc-port", type=int, default=8140)
    parser.add_argument("--full-matrix", action="store_true")
    parser.add_argument("--preflight-manifest", type=Path)
    args = parser.parse_args(argv)
    if args.component_preflight is not None:
        if args.component_preflight.exists():
            raise FileExistsError("D122 component evidence is append-only")
        result = component_preflight()
        args.component_preflight.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(args.component_preflight, result)
        print(json.dumps({"result": result["result"], "trials": len(result["trials"])}))
        return 0 if result["result"] == "PASS" else 2
    if args.output is None:
        parser.error("--output is required for Fabric launch or sealing")
    if args.output.exists():
        raise FileExistsError("D122 Fabric evidence is append-only")
    command = fabric_probe_command(server_port=args.server_port, ipc_port=args.ipc_port,
                                   full_matrix=args.full_matrix)
    run_dir = args.run_dir
    stdout = stderr = ""
    return_code = 0
    if run_dir is None:
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace")
        stdout, stderr, return_code = completed.stdout, completed.stderr, completed.returncode
        run_dir = _run_directory(stdout)
    args.output.mkdir(parents=True, exist_ok=False)
    summary = {
        "schema_version": "mc2p.d122-fabric-launch.v1",
        "status": "captured" if return_code == 0 and run_dir is not None else "failed",
        "full_matrix": args.full_matrix,
        "command": command,
        "return_code": return_code,
        "source_run": None if run_dir is None else str(run_dir.resolve()),
    }
    if args.preflight_manifest is not None:
        summary["preflight"] = preflight_reference(args.preflight_manifest)
    write_json_atomic(args.output / "summary.json", summary)
    (args.output / "probe-stdout.txt").write_text(stdout, encoding="utf-8", newline="\n")
    (args.output / "probe-stderr.txt").write_text(stderr, encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["status"] == "captured" else 2


if __name__ == "__main__":
    raise SystemExit(main())

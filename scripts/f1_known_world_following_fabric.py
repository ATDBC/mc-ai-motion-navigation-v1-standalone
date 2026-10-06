"""F1-D two-player Fabric plan and reusable formal-runtime components."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import time

import psutil

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1,
    OrderedIntentV1,
    ordered_intent_id,
)
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.task import (
    ComparisonOperatorV0,
    SuccessCriterionV0,
    TaskIntentV0,
)
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
)
from mc2p.motion_nav.async_work import AsyncWorkKind
from mc2p.motion_nav.goal_observation import ObservedGoalStatus
from mc2p.runtime.async_trace import BoundedAsyncTraceWriter
from mc2p.runtime.segmented_trace import SegmentedJsonlWriter, SegmentedTraceWriter
from mc2p.skills.known_world_follow_driver import (
    KnownWorldFollowDriver,
    KnownWorldFollowState,
    KnownWorldFollowStatus,
)
from mc2p.skills.local_perception import project_follow_view
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from scripts.control_probe_core import write_json_atomic
from scripts.fabric_deployment_launch import ROOT, inspect_launch, verify_assets
from scripts.follow_fixture_world import PLAYERS, offline_uuid
from scripts.follow_runtime_host import FollowRuntimeHost
from scripts.probe_fabric_deployment_observation import port_free
from scripts.streaming_time_evidence import export_segmented_runtime_time_evidence
from tests.sim.product_metrics import (
    RevisionResponseMode,
    revision_responses,
)


SCHEMA_VERSION = "mc2p.f1-known-world-following-fabric.v1"
DEFAULT_PORTS = (25607, 8241, 8242)
TARGET_MOVEMENT_PATTERN = (1, 1, 0, 0, 1, 1, 0, 0, 1, 0, 0)
TARGET_SPEED_BLOCKS_PER_SECOND = 2.0
TARGET_SPEED_TOLERANCE_BLOCKS_PER_SECOND = .2
FOLLOWER_START = (.5, -60.0, .5)
TARGET_START = (.5, -60.0, 6.5)
TRACE_CAPACITY = 2048
STABLE_WARMUP_TICKS = 40


@dataclass(frozen=True, slots=True)
class FabricFollowScenario:
    identifier: str
    direction: str
    move_ticks: int
    pause_ticks: int = 0
    resume_ticks: int = 0
    final_hold_ticks: int = 120
    cancel_tick: int | None = None
    delay_first_follower_input: bool = False

    @property
    def motion_end_tick(self) -> int:
        return self.move_ticks + self.pause_ticks + self.resume_ticks

    @property
    def total_ticks(self) -> int:
        cancel = 0 if self.cancel_tick is None else self.cancel_tick
        return max(self.motion_end_tick + self.final_hold_ticks, cancel + 20)

    def as_plan(self) -> dict:
        return {
            "id": self.identifier,
            "direction": self.direction,
            "move_ticks": self.move_ticks,
            "pause_ticks": self.pause_ticks,
            "pause_seconds": self.pause_ticks * .05,
            "resume_ticks": self.resume_ticks,
            "final_hold_ticks": self.final_hold_ticks,
            "cancel_tick": self.cancel_tick,
            "delay_first_follower_input": self.delay_first_follower_input,
        }

    def stable_tick(self, completed_tick: int) -> bool:
        if type(completed_tick) is not int:
            raise TypeError("completed target tick must be an integer")
        if STABLE_WARMUP_TICKS <= completed_tick <= self.move_ticks:
            return True
        resumed_at = self.move_ticks + self.pause_ticks
        return (
            self.resume_ticks > STABLE_WARMUP_TICKS
            and resumed_at + STABLE_WARMUP_TICKS <= completed_tick
            <= resumed_at + self.resume_ticks
        )


SCENARIOS = (
    FabricFollowScenario("straight_2_0", "forward", 66),
    FabricFollowScenario("lateral_2_0", "left", 66),
    FabricFollowScenario(
        "move_stop_800_resume_2_0", "forward", 33,
        pause_ticks=800, resume_ticks=66,
    ),
    FabricFollowScenario(
        "normal_cancel_2_0", "forward", 66,
        final_hold_ticks=0, cancel_tick=44,
    ),
    FabricFollowScenario(
        "first_input_late_one_tick_2_0", "forward", 66,
        delay_first_follower_input=True,
    ),
)
SCENARIO_BY_ID = {item.identifier: item for item in SCENARIOS}


def frozen_plan() -> dict:
    """Return the reviewable declaration used before any game process starts."""
    return {
        "schema_version": SCHEMA_VERSION,
        "stage": "F1-D",
        "sample_claim": "representative_only",
        "formal_chain": (
            "PlayerRuntimeV1->RuntimeNavigationDriver->"
            "KnownWorldFollowDriver"
        ),
        "roles": {"robot": PLAYERS[0], "target_player": PLAYERS[1]},
        "offline_uuids": {name: offline_uuid(name) for name in PLAYERS},
        "seed": 21001,
        "fixture_scenario": "static",
        "terrain": "known_open_ordinary_grass",
        "starts": {
            "robot": list(FOLLOWER_START),
            "target_player": list(TARGET_START),
        },
        "initial_horizontal_distance_blocks": 6.0,
        "target_speed_blocks_per_second": TARGET_SPEED_BLOCKS_PER_SECOND,
        "target_speed_tolerance_blocks_per_second": (
            TARGET_SPEED_TOLERANCE_BLOCKS_PER_SECOND
        ),
        "target_input_pattern": list(TARGET_MOVEMENT_PATTERN),
        "target_input_semantics": (
            "normal one-tick player movement through the target Runtime arbiter"
        ),
        "permits_teleport_or_world_write": False,
        "scenarios": [item.as_plan() for item in SCENARIOS],
        "thresholds": {
            "stable_mean_excess_lag_blocks_max": .75,
            "stable_p95_excess_lag_blocks_max": 1.5,
            "planning_submissions_per_revision_max": 1.25,
            "revision_response_p95_ticks_max": 5,
            "safety_violations_max": 0,
            "hold_distance_blocks": 2.5,
        },
        "normal_cancel_capability_gates": (
            "cancel,safety,source_release,bounded_terminal"
        ),
    }


def _in_motion_phase(scenario: FabricFollowScenario, tick: int) -> bool:
    if not 0 <= tick < scenario.total_ticks:
        return False
    if tick < scenario.move_ticks:
        return True
    resumed_at = scenario.move_ticks + scenario.pause_ticks
    return resumed_at <= tick < resumed_at + scenario.resume_ticks


def target_movement(
    scenario: FabricFollowScenario, tick: int,
) -> MovementV1:
    """One declared target-player input; scene truth never enters the robot."""
    if type(scenario) is not FabricFollowScenario or type(tick) is not int:
        raise TypeError("target movement requires a frozen scenario and tick")
    if not _in_motion_phase(scenario, tick):
        return MovementV1()
    phase_tick = (
        tick if tick < scenario.move_ticks
        else tick - scenario.move_ticks - scenario.pause_ticks
    )
    if not TARGET_MOVEMENT_PATTERN[phase_tick % len(TARGET_MOVEMENT_PATTERN)]:
        return MovementV1()
    if scenario.direction == "forward":
        return MovementV1(forward=1)
    if scenario.direction == "left":
        return MovementV1(strafe=1)
    raise ValueError("undeclared target direction")


class F1SegmentedDiagnosticTrace:
    """Segmented formal trace plus separately associated client diagnostics."""

    def __init__(self, directory: Path, backend) -> None:
        self.backend = backend
        self.trace = BoundedAsyncTraceWriter(
            SegmentedTraceWriter(directory / "trace"),
            capacity=TRACE_CAPACITY,
        )
        try:
            self.diagnostics = SegmentedJsonlWriter(directory / "diagnostics")
        except BaseException:
            self.trace.close()
            raise

    def write(self, record_type: str, payload: object) -> None:
        self.trace.write(record_type, payload)
        observation = None
        if record_type == "reset" and payload["result"].succeeded:
            observation = payload["result"].observation
        elif record_type in {"step", "close_release"}:
            observation = payload["backend_result"].observation
        if observation is not None:
            self.diagnostics.write({
                "episode_id": observation.episode_id,
                "observation_sequence_id": observation.sequence_id,
                "diagnostics": self.backend.last_diagnostics,
            })

    def close(self) -> None:
        primary = None
        try:
            self.trace.close()
        except BaseException as error:
            primary = error
        try:
            self.diagnostics.close()
        except BaseException as error:
            if primary is None:
                primary = error
            else:
                primary.add_note("diagnostic close failed: " + repr(error))
        if primary is not None:
            raise primary


class F1FabricHost(FollowRuntimeHost):
    """The existing isolated two-client host with current segmented evidence."""

    def _client_environment(self, token: str, port: int) -> dict:
        result = super()._client_environment(token, port)
        result["MC2P_TIME_SEGMENTED"] = "1"
        result["MC2P_MOVEMENT_DIAGNOSTICS"] = "1"
        return result

    def _create_trace(self, directory: Path, backend):
        return F1SegmentedDiagnosticTrace(directory, backend)

    def _export_time_evidence(self, directory: Path) -> dict:
        return export_segmented_runtime_time_evidence(
            directory, directory / "time-evidence",
        )


class TargetPlayerController:
    """Own exactly one ordered input source in the target player's Runtime."""

    def __init__(self, runtime) -> None:
        self.runtime = runtime
        self.source = runtime.register_ordered_source("f1-target-player")
        self.sequence = 0
        self.closed = False

    def proposal(
        self, movement: MovementV1, deadline_ns: int,
    ) -> ControlFrameProposalV1:
        if self.closed:
            raise RuntimeError("target controller is closed")
        if type(movement) is not MovementV1:
            raise TypeError("target controller requires MovementV1")
        request = ObservationRequestV3("navigation_v1")
        if movement == MovementV1():
            self.runtime.cancel_source(self.source.source_id)
            return ControlFrameProposalV1(observation_request=request)
        self.sequence += 1
        now_ns = time.perf_counter_ns()
        identity = ordered_intent_id(self.source, self.sequence)
        intent = ActionIntentV1(
            identity, self.source.source_id,
            self.runtime.observation.episode_id,
            self.runtime.observation.sequence_id,
            ActionPriorityV0.TASK,
            now_ns, min(deadline_ns, now_ns + 250_000_000),
            movement=movement, valid_for_ticks=1,
        )
        return ControlFrameProposalV1(
            (OrderedIntentV1(self.source, self.sequence, intent),),
            request,
        )

    def close(self) -> None:
        if self.closed:
            return
        self.runtime.cancel_source(self.source.source_id)
        self.runtime.unregister_ordered_source(self.source)
        self.closed = True


def _task(task_id: str, task_type: str, deadline_ns: int) -> TaskIntentV0:
    return TaskIntentV0(
        task_id, task_type, "{}",
        (SuccessCriterionV0(
            "bounded_runtime_step", ComparisonOperatorV0.EQUAL,
            1, "boolean",
        ),),
        100, deadline_ns, True, 0,
    )


def _normal_runtime_step(runtime, task_id: str, deadline_ns: int, *,
                         proposal: ControlFrameProposalV1):
    return runtime.control_frame(
        _task(task_id, "f1_fabric_target", deadline_ns),
        BehaviorProfileV0(), deadline_ns, proposals=(proposal,),
    )


def _paired_step(executor, leader_call, follower_call, deadline_ns: int):
    pending = executor.submit(leader_call)
    primary = result = None
    try:
        result = follower_call()
    except BaseException as error:
        primary = error
    try:
        pending.result(timeout=max(
            .001, (deadline_ns - time.perf_counter_ns()) / 1e9,
        ))
    except BaseException as error:
        if primary is None:
            primary = error
        else:
            primary.add_note("target player step failed: " + repr(error))
    if primary is not None:
        raise primary
    return result, pending.result()


def _position(runtime) -> tuple[float, float, float]:
    value = runtime.observation.position.value
    if value is None:
        raise RuntimeError("runtime position is unavailable")
    return value.x, value.y, value.z


def _bind_visible_player(host: F1FabricHost, controller: TargetPlayerController,
                         executor: ThreadPoolExecutor) -> str:
    request = ObservationRequestV3("navigation_v1")
    for index in range(20):
        deadline = min(host.deadline_ns, time.perf_counter_ns() + 2_000_000_000)
        leader_proposal = controller.proposal(MovementV1(), deadline)
        follower_proposal = ControlFrameProposalV1(
            observation_request=request,
        )
        _paired_step(
            executor,
            lambda: _normal_runtime_step(
                host.leader, "f1-target-bind", deadline,
                proposal=leader_proposal,
            ),
            lambda: _normal_runtime_step(
                host.follower, "f1-robot-bind", deadline,
                proposal=follower_proposal,
            ),
            deadline,
        )
        observation = host.follower.observation
        view = project_follow_view(
            observation, time.perf_counter_ns(),
            observation.controller_clock_id,
        )
        players = tuple(
            entity for entity in view.entities
            if entity.entity_type == "minecraft:player"
        )
        if view.available and len(players) == 1:
            return players[0].track_id
    raise TimeoutError("F1-D could not bind one visible target player")


def smoke_evidence_gates(host) -> tuple[list[dict], list[dict]]:
    """Turn every host close/evidence result into a blocking smoke gate."""
    checks = []
    failures = []
    for item in host.checks:
        check = {"name": item["name"], "passed": item["passed"] is True}
        checks.append(check)
        if not check["passed"]:
            failures.append({
                "kind": "host_check_failed",
                "name": check["name"],
            })
    checks.append({
        "name": "host_checks_present",
        "passed": bool(host.checks),
    })
    if not host.checks:
        failures.append({
            "kind": "host_checks_missing",
            "name": "host_checks_present",
        })
    checks.append({
        "name": "host_cleanup_complete",
        "passed": not host.cleanup_failures,
    })
    failures.extend({
        "kind": "cleanup_failure",
        "detail": item,
    } for item in host.cleanup_failures)
    return checks, failures


def _nearest_rank(values: list[float] | list[int], fraction: float):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _rounded(value):
    return None if value is None else round(float(value), 6)


def _numeric_summary(values: list[float]) -> dict:
    return {
        "sample_count": len(values),
        "mean_blocks": _rounded(sum(values) / len(values) if values else None),
        "p95_blocks": _rounded(_nearest_rank(values, .95)),
        "max_blocks": _rounded(max(values) if values else None),
        "final_blocks": _rounded(values[-1] if values else None),
    }


def summarize_scenario(
    scenario: FabricFollowScenario,
    data: dict,
    *,
    host_checks: list[dict],
    cleanup_failures: list[dict],
) -> dict:
    """Apply the frozen F1-C rulers to actual two-client observations."""
    if type(scenario) is not FabricFollowScenario:
        raise TypeError("scenario summary requires a frozen F1-D scenario")
    samples = data["samples"]
    initial_raw = float(data["initial_raw_distance_blocks"])
    overall_raw = [initial_raw]
    stable_raw = []
    stable_speeds = []
    for row in samples:
        robot = row["robot_position"]
        target = row["target_position"]
        distance = math.hypot(
            robot[0] - target[0], robot[2] - target[2],
        )
        overall_raw.append(distance)
        if row["stable"]:
            stable_raw.append(distance)
            speed = row["target_speed_blocks_per_second"]
            if speed is not None:
                stable_speeds.append(float(speed))
    overall_excess = [max(0.0, item - 2.5) for item in overall_raw]
    stable_excess = [max(0.0, item - 2.5) for item in stable_raw]

    responses = revision_responses(
        data["response_frames"],
        start_tick=data["response_start_tick"],
        start_position=tuple(data["response_start_position"]),
        mode=RevisionResponseMode.MOVEMENT_OR_MATCHING_SATISFACTION,
    )
    response_details = [
        {
            **item,
            "end": (
                "effective" if item["end"] in {"movement", "satisfied"}
                else item["end"]
            ),
            "evidence": item["end"],
        }
        for item in responses
    ]
    effective_responses = [
        item["response_ticks"] for item in response_details
        if item["end"] == "effective"
    ]
    response_p95 = _nearest_rank(effective_responses, .95)
    unanswered = sum(
        item["end"] == "unanswered" for item in response_details
    )
    accepted = data["accepted_revisions"]
    planning_ratio = (
        data["planning_submissions"] / accepted if accepted else None
    )
    speed_mean = (
        sum(stable_speeds) / len(stable_speeds)
        if stable_speeds else None
    )
    terminal = bool(
        data["cancel_requested"]
        and data["terminal_session_state"] == "cancelled"
        and data["terminal_driver_state"] == "cancelled"
        and data["source_released"]
        and data["cancel_tail_ticks"] <= 20
    )
    host_evidence = bool(host_checks) and all(
        item.get("passed") is True for item in host_checks
    ) and not cleanup_failures
    satisfied_idle_stopping_ticks = []
    for row in samples:
        session = row.get("session") or {}
        diagnostics = row.get("diagnostics") or {}
        handoff = diagnostics.get("handoff") or {}
        if (
            session.get("state") == "stopping"
            and session.get("observed_goal_status") == "satisfied"
            and session.get("reach_policy") == "keep_active_on_reach"
            and not row.get("cancellation_requested", False)
            and session.get("route_id") is None
            and diagnostics.get("pending_route_id") is None
            and handoff.get("disposition") == "quiescent"
            and not diagnostics.get("controller_ids")
            and not diagnostics.get("active_waits")
            and not diagnostics.get("body_control_activities")
        ):
            satisfied_idle_stopping_ticks.append(row["tick"])
    safety = not data["safety_violations"]
    capability_gates_apply = scenario.cancel_tick is None
    final_raw = overall_raw[-1]
    stable_gate = (
        None if not capability_gates_apply else bool(
            stable_excess
            and sum(stable_excess) / len(stable_excess) <= .75
            and _nearest_rank(stable_excess, .95) <= 1.5
        )
    )
    late_evidence = data["late_input_evidence"]
    late_gate = None
    if scenario.delay_first_follower_input:
        late_gate = bool(
            late_evidence is not None
            and late_evidence.get("injection_requested") is True
            and late_evidence.get("input_application_status") == "applied"
            and late_evidence.get("valid_for_ticks") == 1
            and late_evidence.get("normal_application_offset_ticks") == 1
            and late_evidence.get("actual_application_offset_ticks") == 2
            and late_evidence.get("requested_last_tick")
                == late_evidence.get("requested_first_tick")
            and late_evidence.get("latest_allowed_first_tick")
                == late_evidence.get("requested_first_tick", -2) + 1
            and late_evidence.get("actual_application_ticks")
                == [late_evidence.get("requested_first_tick", -2) + 1]
        )
    gates = {
        "target_speed": (
            None if not capability_gates_apply else bool(
                speed_mean is not None
                and abs(speed_mean - TARGET_SPEED_BLOCKS_PER_SECOND)
                <= TARGET_SPEED_TOLERANCE_BLOCKS_PER_SECOND
            )
        ),
        "stable_lag": stable_gate,
        "final_within_hold": (
            None if not capability_gates_apply else final_raw <= 2.5
        ),
        "planning_ratio": (
            None if not capability_gates_apply else bool(
                planning_ratio is not None and planning_ratio <= 1.25
            )
        ),
        "revision_response": (
            None if not capability_gates_apply else bool(
                unanswered == 0 and response_p95 is not None
                and response_p95 <= 5
            )
        ),
        "late_input": late_gate,
        "safety": safety,
        "satisfied_idle_lifecycle": not satisfied_idle_stopping_ticks,
        "terminal": terminal,
        "host_evidence": host_evidence,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "fabric_scenario",
        "scenario": scenario.as_plan(),
        "sample_claim": "representative_only",
        "actual_target_trajectory": {
            "initial_position": (
                None if not samples else samples[0]["target_position"]
            ),
            "final_position": (
                None if not samples else samples[-1]["target_position"]
            ),
            "stable_speed_sample_count": len(stable_speeds),
            "stable_speed_mean_blocks_per_second": _rounded(speed_mean),
            "stable_speed_p95_blocks_per_second": _rounded(
                _nearest_rank(stable_speeds, .95)
            ),
        },
        "initial_raw_distance_blocks": _rounded(initial_raw),
        "overall_raw_distance": _numeric_summary(overall_raw),
        "stable_raw_distance": _numeric_summary(stable_raw),
        "overall_excess_lag": _numeric_summary(overall_excess),
        "stable_excess_lag": _numeric_summary(stable_excess),
        "final_raw_distance_blocks": _rounded(final_raw),
        "revision_response_details": response_details,
        "revision_response_p95_ticks": response_p95,
        "accepted_revisions": accepted,
        "rejected_revisions": data["rejected_revisions"],
        "planning_submissions": data["planning_submissions"],
        "planning_submissions_per_accepted_revision": _rounded(
            planning_ratio
        ),
        "target_actual_input_ticks": data["target_actual_input_ticks"],
        "robot_actual_input_ticks": data["robot_actual_input_ticks"],
        "task_recoveries": data["task_recoveries"],
        "safety_violations": data["safety_violations"],
        "satisfied_idle_stopping_ticks": satisfied_idle_stopping_ticks,
        "terminal_session_state": data["terminal_session_state"],
        "terminal_driver_state": data["terminal_driver_state"],
        "source_released": data["source_released"],
        "cancel_tail_ticks": data["cancel_tail_ticks"],
        "late_input_evidence": late_evidence,
        "update_status_counts": data["update_status_counts"],
        "host_checks": host_checks,
        "cleanup_failures": cleanup_failures,
        "gates": gates,
        "passed": all(value for value in gates.values()
                      if value is not None),
    }


def _movement_tick(runtime) -> int:
    own = runtime.observation.self_state.value
    if own is None or own.movement_tick_id is None:
        raise RuntimeError("runtime movement tick is unavailable")
    return own.movement_tick_id


def _actual_applications(result) -> list[dict]:
    if result.backend_result is None:
        return []
    return [
        asdict(item)
        for item in result.backend_result.receipt.input_applications
    ]


def _nonneutral_applications(applications: list[dict]) -> int:
    return sum(
        item["forward"] != 0 or item["strafe"] != 0
        for item in applications
    )


def _planning_submission_keys(session: NavigationSession) -> set[str]:
    result = set()
    for owner in session.async_work_diagnostics:
        for event in owner.events:
            if (event.operation == "begin"
                    and event.identity.work_kind is AsyncWorkKind.PLANNING):
                result.add(event.identity.key)
    return result


def planning_identity_violations(state: str, diagnostics) -> tuple[str, ...]:
    """Mirror the formal monitor's I15, I16 and I17 state guards."""
    violations = []
    if (state == "planning"
            and (not diagnostics.planning_work_owned
                 or not diagnostics.planning_work_identity_valid)):
        violations.append("I15:planning_work_identity_invalid")
    if (state == "planning"
            and not diagnostics.planning_permit_identity_valid):
        violations.append("I16:planning_permit_identity_invalid")
    if (state == "needs_information"
            and not diagnostics.planning_information_identity_valid):
        violations.append("I17:planning_information_identity_invalid")
    return tuple(violations)


def _driver_step(driver: RuntimeNavigationDriver, deadline_ns: int,
                 delay_state: dict) -> tuple[object, dict | None]:
    proposals = driver.prepare_proposals(deadline_ns)
    nonneutral = any(
        envelope.intent.movement is not None
        and envelope.intent.movement != MovementV1()
        for proposal in proposals for envelope in proposal.intents
    )
    before_tick = _movement_tick(driver.runtime)
    inject = bool(
        delay_state["enabled"] and not delay_state["attempted"] and nonneutral
    )
    if inject:
        delay_state["attempted"] = True
        time.sleep(.055)
    result = driver.runtime.control_frame(
        _task("f1-robot", "f1_fabric_follow", deadline_ns),
        BehaviorProfileV0(), deadline_ns, proposals=proposals,
    )
    driver.adopt_result(result)
    evidence = None
    if inject:
        diagnostics = driver.last_frame_diagnostics
        sequence = (
            None if diagnostics is None
            else diagnostics.action_request_sequence
        )
        record = (
            None if sequence is None
            else driver.runtime.input_ledger.record(sequence)
        )
        evidence = {
            "injection_requested": True,
            "sleep_milliseconds": 55,
            "action_request_sequence": sequence,
            "movement_tick_before_dispatch": before_tick,
            "requested_first_tick": (
                None if record is None else record.requested_first_tick
            ),
            "requested_last_tick": (
                None if record is None else record.requested_last_tick
            ),
            "latest_allowed_first_tick": (
                None if record is None else record.latest_allowed_first_tick
            ),
            "input_application_status": (
                None if record is None else record.status.value
            ),
            "valid_for_ticks": (
                None if record is None else record.action.valid_for_ticks
            ),
            "normal_application_offset_ticks": (
                None if record is None
                else record.requested_first_tick - before_tick
            ),
            "actual_application_ticks": (
                [] if record is None else list(record.applied_ticks)
            ),
            "actual_application_offset_ticks": (
                None if record is None or not record.applied_ticks
                else min(record.applied_ticks) - before_tick
            ),
        }
        delay_state["evidence"] = evidence
    return result, evidence


def _new_case_host(run_dir: Path, ports: tuple[int, int, int]) -> F1FabricHost:
    run_dir = Path(run_dir).absolute()
    evidence_root = (ROOT / "artifacts/f1-known-world-following").absolute()
    if run_dir.parent != evidence_root:
        raise ValueError("F1-D run must be one new batch under its artifact root")
    evidence_root.mkdir(parents=True, exist_ok=True)
    if type(ports) is not tuple or len(ports) != 3:
        raise ValueError("F1-D run requires three declared ports")
    return F1FabricHost(
        run_dir, seed=21001, scenario="static",
        server_port=ports[0], follower_port=ports[1], leader_port=ports[2],
        deadline_ns=time.perf_counter_ns() + 300_000_000_000,
    )


def _run_formal_case(run_dir: Path, scenario: FabricFollowScenario, *,
                     tick_limit: int, evidence_name: str,
                     ports: tuple[int, int, int]) -> tuple[dict, F1FabricHost]:
    """One shared two-client chain for smoke and every frozen scenario."""
    if type(scenario) is not FabricFollowScenario:
        raise TypeError("F1-D run requires one frozen scenario")
    host = _new_case_host(run_dir, ports)
    session = follow = driver = controller = samples = executor = None
    rows = []
    response_frames = []
    revisions = []
    status_counts = {}
    planning_keys = set()
    safety = set()
    rejected_revisions = 0
    target_applied = follower_applied = recoveries = cancel_tail = 0
    cancellation_requested = False
    delay_state = {
        "enabled": scenario.delay_first_follower_input,
        "attempted": False,
        "evidence": None,
    }
    cancelled = None
    response_start_tick = None
    response_start_position = None
    initial_raw = None
    initial_health = {}
    final_session_state = final_driver_state = "unavailable"
    source_released = False
    with host:
        controller = TargetPlayerController(host.leader)
        samples = SegmentedJsonlWriter(Path(run_dir).absolute() / evidence_name)
        executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="f1-target-player",
        )
        try:
            target_track_id = _bind_visible_player(host, controller, executor)
            deadline = min(
                host.deadline_ns, time.perf_counter_ns() + 2_000_000_000,
            )
            target_proposal = controller.proposal(MovementV1(), deadline)
            tracked_request = ObservationRequestV3(
                "navigation_v1", entity_track_id=target_track_id,
            )
            _paired_step(
                executor,
                lambda: _normal_runtime_step(
                    host.leader, "f1-target-track", deadline,
                    proposal=target_proposal,
                ),
                lambda: _normal_runtime_step(
                    host.follower, "f1-robot-track", deadline,
                    proposal=ControlFrameProposalV1(
                        observation_request=tracked_request,
                    ),
                ),
                deadline,
            )
            tracked = host.follower.observation.tracked_entity.value
            if (tracked is None or tracked.track_id != target_track_id
                    or tracked.entity_type != "minecraft:player"
                    or not tracked.is_loaded or tracked.is_dead):
                raise RuntimeError("F1-D target life fact is unavailable")

            profiles = NavigationSessionProfiles.load(
                ROOT / "config/motion-navigation",
            )
            identity = scenario.identifier
            session = NavigationSession(
                "f1-d/" + identity, profiles,
                observation_adapter=(
                    host.follower.navigation_observation_adapter
                ),
            )
            driver = RuntimeNavigationDriver(
                host.follower, session,
                observation_request=tracked_request,
            )
            follow = KnownWorldFollowDriver(
                driver, task_id="f1-d-task/" + identity,
                goal_id="f1-d-goal/" + identity,
                target_track_id=target_track_id,
            )
            started = follow.start(time.perf_counter_ns())
            latest_follow_result = started
            if started.status is not KnownWorldFollowStatus.STARTED:
                raise RuntimeError("F1-D formal follow did not start")
            goal_position = started.submitted_target_position
            robot_before = _position(host.follower)
            target_before = _position(host.leader)
            target_tick_before = _movement_tick(host.leader)
            response_start_tick = _movement_tick(host.follower)
            response_start_position = robot_before
            initial_raw = math.hypot(
                robot_before[0] - target_before[0],
                robot_before[2] - target_before[2],
            )
            initial_health = {
                "robot": host.follower.observation.health_points.value,
                "target": host.leader.observation.health_points.value,
            }
            last_sequence = host.follower.observation.sequence_id

            for tick in range(tick_limit):
                revision_requests = []
                if (scenario.cancel_tick is not None
                        and tick == scenario.cancel_tick):
                    cancelled = follow.cancel()
                    latest_follow_result = cancelled
                    cancellation_requested = True
                    status_counts[cancelled.status.value] = (
                        status_counts.get(cancelled.status.value, 0) + 1
                    )
                elif (follow.state in {
                        KnownWorldFollowState.FOLLOWING,
                        KnownWorldFollowState.NEEDS_TARGET_OBSERVATION,
                      }
                      and host.follower.observation.sequence_id
                      != last_sequence):
                    update = follow.update(time.perf_counter_ns())
                    latest_follow_result = update
                    last_sequence = host.follower.observation.sequence_id
                    status_counts[update.status.value] = (
                        status_counts.get(update.status.value, 0) + 1
                    )
                    if update.status is KnownWorldFollowStatus.REVISED:
                        revisions.append(update.revision)
                        goal_position = update.submitted_target_position
                        revision_requests.append({
                            "revision": update.revision,
                            "movement_tick": _movement_tick(host.follower),
                        })
                    elif update.status in {
                        KnownWorldFollowStatus.NAVIGATION_ENDED,
                        KnownWorldFollowStatus.TARGET_INVALID,
                        KnownWorldFollowStatus.TARGET_DEAD,
                        KnownWorldFollowStatus.SESSION_CHANGED,
                        KnownWorldFollowStatus.OBSERVATION_REWRITTEN,
                    }:
                        rejected_revisions += 1
                        safety.add("follow_ended_during_measurement")

                if driver.source is None:
                    if not cancellation_requested:
                        safety.add("navigation_source_released_early")
                    break
                deadline = min(
                    host.deadline_ns,
                    time.perf_counter_ns() + 2_000_000_000,
                )
                target_proposal = controller.proposal(
                    target_movement(scenario, tick), deadline,
                )
                follower_result, target_result = _paired_step(
                    executor,
                    lambda: _normal_runtime_step(
                        host.leader, "f1-target-scenario", deadline,
                        proposal=target_proposal,
                    ),
                    lambda: _driver_step(driver, deadline, delay_state)[0],
                    deadline,
                )
                target_apps = _actual_applications(target_result)
                follower_apps = _actual_applications(follower_result)
                target_applied += _nonneutral_applications(target_apps)
                follower_applied += _nonneutral_applications(follower_apps)
                if (follower_result.report.failure is not None
                        or target_result.report.failure is not None):
                    safety.add("runtime_failure")

                robot_position = _position(host.follower)
                target_position = _position(host.leader)
                target_tick = _movement_tick(host.leader)
                target_tick_delta = target_tick - target_tick_before
                target_speed = (
                    None if target_tick_delta <= 0 else
                    math.hypot(
                        target_position[0] - target_before[0],
                        target_position[2] - target_before[2],
                    ) * 20.0 / target_tick_delta
                )
                target_before = target_position
                target_tick_before = target_tick
                diagnostics = session.diagnostics
                planning_keys.update(_planning_submission_keys(session))
                recoveries = max(
                    recoveries, diagnostics.recovery_total_starts,
                )
                if diagnostics.illegal_transition_count:
                    safety.add("illegal_navigation_transition")
                safety.update(planning_identity_violations(
                    session.report.state.value, diagnostics,
                ))
                for role, runtime in (
                    ("robot", host.follower), ("target", host.leader),
                ):
                    observation = runtime.observation
                    if observation.is_dead.value is True:
                        safety.add(role + "_dead")
                    health = observation.health_points.value
                    if (health is not None and initial_health[role] is not None
                            and health < initial_health[role]):
                        safety.add(role + "_health_lost")
                    if any(event.target_is_self
                           for event in observation.damage_events):
                        safety.add(role + "_damage_event")

                applied = (
                    follower_apps[-1] if follower_apps else {
                        "forward": 0.0, "strafe": 0.0,
                    }
                )
                frame = {
                    "movement_tick": _movement_tick(host.follower),
                    "position": list(robot_position),
                    "driver_state": driver.state,
                    "source_bound": driver.source is not None,
                    "goal_satisfied": (
                        session.report.observed_goal_status
                        is ObservedGoalStatus.SATISFIED
                    ),
                    "goal_revision": session.report.goal_revision,
                    "goal_revision_requests": revision_requests,
                    "goal_position": (
                        None if goal_position is None else [
                            goal_position.x, goal_position.y, goal_position.z,
                        ]
                    ),
                    "applied_movement": {
                        "forward": applied["forward"],
                        "strafe": applied["strafe"],
                    },
                }
                response_frames.append(frame)
                row = {
                    "tick": tick,
                    "robot_movement_tick": frame["movement_tick"],
                    "target_movement_tick": target_tick,
                    "robot_position": list(robot_position),
                    "target_position": list(target_position),
                    "horizontal_distance": math.hypot(
                        robot_position[0] - target_position[0],
                        robot_position[2] - target_position[2],
                    ),
                    "stable": scenario.stable_tick(tick + 1),
                    "target_speed_blocks_per_second": target_speed,
                    "follow_revision": follow.revision,
                    "follow_update_status": (
                        None if not revision_requests else "revised"
                    ),
                    "cancellation_requested": cancellation_requested,
                    "submitted_target_position": (
                        None
                        if latest_follow_result.submitted_target_position is None
                        else [
                            latest_follow_result.submitted_target_position.x,
                            latest_follow_result.submitted_target_position.y,
                            latest_follow_result.submitted_target_position.z,
                        ]
                    ),
                    "planning_submissions": sorted(planning_keys),
                    "target_actual_inputs": target_apps,
                    "robot_actual_inputs": follower_apps,
                    "session": asdict(session.report),
                    "diagnostics": asdict(diagnostics),
                }
                rows.append(row)
                samples.write(row)

                if follow.state is KnownWorldFollowState.STOPPING:
                    pending = follow.update(time.perf_counter_ns())
                    status_counts[pending.status.value] = (
                        status_counts.get(pending.status.value, 0) + 1
                    )
                if cancellation_requested and driver.source is None:
                    break
                if driver.state in {"failed", "success"}:
                    safety.add("unexpected_navigation_terminal")
                    break

            if driver.source is not None and not cancellation_requested:
                cancelled = follow.cancel()
                cancellation_requested = True
                status_counts[cancelled.status.value] = (
                    status_counts.get(cancelled.status.value, 0) + 1
                )
            for cancel_tail in range(1, 21):
                if driver.source is None:
                    cancel_tail -= 1
                    break
                deadline = min(
                    host.deadline_ns,
                    time.perf_counter_ns() + 2_000_000_000,
                )
                target_proposal = controller.proposal(MovementV1(), deadline)
                _paired_step(
                    executor,
                    lambda: _normal_runtime_step(
                        host.leader, "f1-target-stop", deadline,
                        proposal=target_proposal,
                    ),
                    lambda: _driver_step(
                        driver, deadline,
                        {"enabled": False, "attempted": True,
                         "evidence": None},
                    )[0],
                    deadline,
                )
                if follow.state is KnownWorldFollowState.STOPPING:
                    cancelled = follow.update(time.perf_counter_ns())
            source_released = driver.source is None
            if not source_released:
                safety.add("cancel_source_not_released")
            final_session_state = session.report.state.value
            final_driver_state = driver.state
        finally:
            if samples is not None:
                samples.close()
            if controller is not None:
                controller.close()
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            if session is not None:
                session.close()

    data = {
        "samples": rows,
        "response_frames": response_frames,
        "response_start_tick": response_start_tick,
        "response_start_position": response_start_position,
        "initial_raw_distance_blocks": initial_raw,
        "accepted_revisions": 0 if follow is None else follow.revision,
        "rejected_revisions": rejected_revisions,
        "planning_submissions": len(planning_keys),
        "target_actual_input_ticks": target_applied,
        "robot_actual_input_ticks": follower_applied,
        "task_recoveries": recoveries,
        "safety_violations": sorted(safety),
        "terminal_session_state": final_session_state,
        "terminal_driver_state": final_driver_state,
        "source_released": source_released,
        "cancel_requested": cancellation_requested,
        "cancel_status": (
            None if cancelled is None else cancelled.status.value
        ),
        "cancel_tail_ticks": cancel_tail,
        "late_input_evidence": delay_state["evidence"],
        "update_status_counts": dict(sorted(status_counts.items())),
        "accepted_revisions_after_start": revisions,
    }
    return data, host


def run_smoke(run_dir: Path, *, ports: tuple[int, int, int] = DEFAULT_PORTS) -> dict:
    """Start the smallest real two-client chain; never run from preflight."""
    scenario = SCENARIO_BY_ID["straight_2_0"]
    data, host = _run_formal_case(
        run_dir, scenario, tick_limit=22,
        evidence_name="smoke-samples", ports=ports,
    )
    checks = [
        {"name": "both_normal_clients_ready", "passed": len(host.clients) == 2},
        {"name": "target_actual_input_applied",
         "passed": data["target_actual_input_ticks"] > 0},
        {"name": "robot_actual_input_applied",
         "passed": data["robot_actual_input_ticks"] > 0},
        {"name": "target_revision_accepted",
         "passed": bool(data["accepted_revisions_after_start"])},
        {"name": "cancel_released_source",
         "passed": data["source_released"]},
    ]
    host_checks, failure_reasons = smoke_evidence_gates(host)
    checks.extend(host_checks)
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "fabric_smoke",
        "passed": all(item["passed"] for item in checks),
        "scenario": scenario.as_plan(),
        "ports": list(ports),
        "target_actual_input_ticks": data["target_actual_input_ticks"],
        "robot_actual_input_ticks": data["robot_actual_input_ticks"],
        "accepted_revisions_after_start": data[
            "accepted_revisions_after_start"
        ],
        "cancel_status": data["cancel_status"],
        "source_released": data["source_released"],
        "sample_count": len(data["samples"]),
        "checks": checks,
        "failure_reasons": failure_reasons,
    }
    write_json_atomic(Path(run_dir).absolute() / "smoke-result.json", report)
    return report


def run_scenario(run_dir: Path, scenario: FabricFollowScenario, *,
                 ports: tuple[int, int, int] = DEFAULT_PORTS) -> dict:
    data, host = _run_formal_case(
        run_dir, scenario, tick_limit=scenario.total_ticks,
        evidence_name="scenario-samples", ports=ports,
    )
    report = summarize_scenario(
        scenario, data, host_checks=host.checks,
        cleanup_failures=host.cleanup_failures,
    )
    write_json_atomic(
        Path(run_dir).absolute() / "scenario-result.json", report,
    )
    return report


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preflight(output: Path, *, check_assets: bool = True) -> dict:
    """Validate the frozen declaration without constructing a game host."""
    output = Path(output).absolute()
    if output.exists():
        raise FileExistsError(output)
    plan = frozen_plan()
    stable_sample_counts = {
        scenario.identifier: sum(
            scenario.stable_tick(completed_tick)
            for completed_tick in range(1, scenario.total_ticks + 1)
        )
        for scenario in SCENARIOS
        if scenario.cancel_tick is None
    }
    checks = [
        {"name": "two_distinct_real_player_identities", "passed": (
            len(set(plan["offline_uuids"].values())) == 2
        )},
        {"name": "normal_target_inputs_only", "passed": (
            not plan["permits_teleport_or_world_write"]
            and all(item in (0, 1) for item in TARGET_MOVEMENT_PATTERN)
        )},
        {"name": "forty_second_stop_is_frozen", "passed": (
            SCENARIO_BY_ID["move_stop_800_resume_2_0"].pause_ticks == 800
        )},
        {"name": "quality_scenarios_have_stable_samples", "passed": (
            all(count > 0 for count in stable_sample_counts.values())
        ), "sample_counts": stable_sample_counts},
        {"name": "new_ports_are_distinct", "passed": (
            len(set(DEFAULT_PORTS)) == 3
        )},
    ]
    environment = {
        "asset_check_requested": check_assets,
        "ports": list(DEFAULT_PORTS),
        "available_memory_bytes": psutil.virtual_memory().available,
    }
    if check_assets:
        launch_path = (
            ROOT / "deployment/fabric-observation-probe/build/launch/"
            "client-launch.json"
        )
        launch = json.loads(launch_path.read_text("utf-8"))
        environment.update({
            "launch": inspect_launch(launch),
            "assets": verify_assets(),
            "ports_free": [port_free(port) for port in DEFAULT_PORTS],
        })
        checks.extend((
            {"name": "all_declared_ports_free",
             "passed": all(environment["ports_free"])},
            {"name": "seven_gibibytes_available",
             "passed": environment["available_memory_bytes"] >= 7 * 1024**3},
        ))
    source_paths = (
        Path(__file__),
        ROOT / "scripts/follow_runtime_host.py",
        ROOT / "mc2p/skills/known_world_follow_driver.py",
        ROOT / "mc2p/skills/navigation_session_driver.py",
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "preflight",
        "passed": all(item["passed"] for item in checks),
        "processes_started": 0,
        "formal_chain": plan["formal_chain"],
        "trace_format": "segmented_jsonl",
        "plan": plan,
        "environment": environment,
        "checks": checks,
        "source_sha256": {
            path.relative_to(ROOT).as_posix(): _hash(path)
            for path in source_paths
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output, report)
    return report


def parse_arguments(argv=None):
    import argparse

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    preflight_parser = commands.add_parser("preflight")
    preflight_parser.add_argument("--output", type=Path, required=True)
    preflight_parser.add_argument("--skip-assets", action="store_true")
    smoke_parser = commands.add_parser("smoke")
    smoke_parser.add_argument("--output", type=Path, required=True)
    scenario_parser = commands.add_parser("scenario")
    scenario_parser.add_argument("--output", type=Path, required=True)
    scenario_parser.add_argument(
        "--id", dest="scenario_id", choices=tuple(SCENARIO_BY_ID),
        required=True,
    )
    return parser.parse_args(argv)


def main() -> int:
    arguments = parse_arguments()
    if arguments.command == "preflight":
        report = preflight(
            arguments.output, check_assets=not arguments.skip_assets,
        )
    elif arguments.command == "smoke":
        report = run_smoke(arguments.output)
    else:
        report = run_scenario(
            arguments.output, SCENARIO_BY_ID[arguments.scenario_id],
        )
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

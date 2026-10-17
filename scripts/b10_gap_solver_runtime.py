"""Controlled Fabric acceptance for the B10-B one-cell-gap command solver."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import gc
import json
import math
from pathlib import Path
import sys
import threading
import time
from typing import Callable, Mapping

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_receipt import ClientBehaviorReceiptV3
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.contracts.observation_request_v3 import (
    MAX_AIR_QUERY_POSITIONS,
    ObservationRequestV3,
)
from mc2p.contracts.task import ComparisonOperatorV0, SuccessCriterionV0, TaskIntentV0
from mc2p.motion_nav.air_motion import load_air_motion_profiles
from mc2p.motion_nav.async_work import AsyncAdmissionDisposition
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.motion_solver import (
    SOLVER_ID, GapSolveRequest, LandingRegion, SolveResult, SolveStatus,
    load_gap_solver_policy, solve_one_cell_gap,
)
from mc2p.motion_nav.motion_coordination import _gap_physics_snapshot
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.motion_worker import MotionSolverWorker
from mc2p.motion_nav.motion_candidate import (
    MotionCandidateAdmitter, MotionCandidateContext, MotionCandidateStatus,
    VerifiedMotionCandidate, VerifiedMotionExecutor,
    VerifiedMotionExecutorState,
)
from mc2p.motion_nav.online_motion import (
    CandidateExecutionWindow, InputApplicationLedger, MotionTickPhase,
    ProjectionStatus, StateAnchor, project_movement_command,
)
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, StateBuildStatus,
)
from mc2p.motion_nav.navigation_session import (
    NavigationSession, NavigationSessionProfiles, NavigationSessionState,
)
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.movement_transition import (
    GoalState,
    GoalSupport,
    MovementMode,
)
from mc2p.motion_nav.step_transition import load_step_profile
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import Aabb, CellKnowledge
from mc2p.runtime.player_runtime_v1 import RuntimeStateV1, RuntimeStepResultV1
from mc2p.skills.navigation_session_driver import (
    NavigationFrameDiagnostics,
    RuntimeNavigationDriver,
    RuntimeNavigationDriverState,
)
from scripts.control_probe_core import (
    strict_json_dumps,
    write_json_atomic,
    write_jsonl_atomic,
)


_SOURCE = "b10-gap-solver"


@dataclass(frozen=True, slots=True)
class _AirRequestPlan:
    """Complete fixture scope; each transport frame sends one bounded slice."""

    field_profile: str
    air_positions: tuple[tuple[int, int, int], ...]
    entity_track_id: str | None = None


_AirRequestSource = ObservationRequestV3 | _AirRequestPlan
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/motion-navigation"
_DIRECTIONS = ((0, 1, 0.0), (1, 0, -90.0), (0, -1, 180.0), (-1, 0, 90.0))
_STATE_ASSUMPTIONS = dict(
    jumping_cooldown_ticks=0,
    movement_speed_attribute=.1,
    step_height_blocks=.6,
    gravity_attribute=.08,
    jump_strength_attribute=.42,
)


@dataclass(frozen=True, slots=True)
class _B10CoordinatorScenario:
    """One preregistered formal-worker delivery scenario."""

    name: str
    kind: str
    direction_index: int
    repetition: int
    worker_delay_ticks: int = 0
    never_return: bool = False
    fabric_supported: bool = True
    unsupported_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.name) is not str or not self.name:
            raise ValueError("B10 coordinator scenario name is invalid")
        if self.kind not in {
            "baseline", "first_input_late1", "worker_delay",
            "worker_never_return",
        }:
            raise ValueError("B10 coordinator scenario kind is invalid")
        if type(self.direction_index) is not int or not 0 <= self.direction_index < 4:
            raise ValueError("B10 coordinator direction is invalid")
        if type(self.repetition) is not int or self.repetition < 0:
            raise ValueError("B10 coordinator repetition is invalid")
        if (type(self.worker_delay_ticks) is not int
                or self.worker_delay_ticks < 0):
            raise ValueError("B10 worker delay must be nonnegative")
        if type(self.never_return) is not bool:
            raise TypeError("B10 never-return flag must be bool")
        if type(self.fabric_supported) is not bool:
            raise TypeError("B10 Fabric support flag must be bool")
        if self.fabric_supported != (self.unsupported_reason is None):
            raise ValueError("B10 unsupported scenario needs one reason")


def _b10_coordinator_scenarios() -> tuple[_B10CoordinatorScenario, ...]:
    """Return the frozen 56-case matrix without pretending Fabric can delay input."""
    scenarios: list[_B10CoordinatorScenario] = []
    for direction in range(4):
        for repetition in range(5):
            scenarios.append(_B10CoordinatorScenario(
                f"baseline-{direction}-{repetition}",
                "baseline", direction, repetition,
            ))
    for direction in range(4):
        for repetition in range(2):
            scenarios.append(_B10CoordinatorScenario(
                f"first-input-late1-{direction}-{repetition}",
                "first_input_late1", direction, repetition,
                fabric_supported=False,
                unsupported_reason="fabric_input_late1_injection_unavailable",
            ))
    for delay in (2, 5, 10):
        for direction in range(4):
            for repetition in range(2):
                scenarios.append(_B10CoordinatorScenario(
                    f"worker-delay-{delay}-{direction}-{repetition}",
                    "worker_delay", direction, repetition,
                    worker_delay_ticks=delay,
                ))
    for direction in range(4):
        scenarios.append(_B10CoordinatorScenario(
            f"worker-never-return-{direction}",
            "worker_never_return", direction, 0,
            never_return=True,
        ))
    if len(scenarios) != 56 or len({item.name for item in scenarios}) != 56:
        raise RuntimeError("B10 coordinator matrix is not the frozen 56 cases")
    return tuple(scenarios)


_D113_TARGETED_PROBE = "coordinator-worker-delay-5-0-0"


def _selected_b10_coordinator_scenarios(targeted_probe: str | None):
    if targeted_probe is None:
        return _b10_coordinator_scenarios()
    if type(targeted_probe) is not str or targeted_probe != _D113_TARGETED_PROBE:
        raise ValueError("only the explicit D113 5-tick targeted probe is supported")
    return tuple(scenario for scenario in _b10_coordinator_scenarios()
                 if f"coordinator-{scenario.name}" == targeted_probe)


def _targeted_b10_summary(trials: list[dict], *, runtime_state: str):
    """One-case diagnostic evidence can never sign the frozen formal matrix."""
    one_case = len(trials) == 1 and trials[0].get("name") == _D113_TARGETED_PROBE
    trial = trials[0] if one_case else {}
    deliveries = trial.get("worker_delivery", ())
    injection = trial.get("worker_injection", {})
    submissions = injection.get("submissions", ())
    # Do not trust a cached delay value or a receiver/take endpoint as visibility.
    identity_chain = one_case and bool(submissions) and not injection.get("stale_result_count", 0)
    identities = [row.get("identity") for row in submissions]
    identity_chain = bool(identity_chain
        and all(type(identity) is str and identity for identity in identities)
        and len(set(identities)) == len(identities)
        and set(identities) == {row.get("identity") for row in deliveries})
    for submission in submissions:
        matches = [row for row in deliveries if row.get("identity") == submission.get("identity")]
        identity_chain = identity_chain and len(matches) == 1
        if len(matches) != 1:
            continue
        delivery = matches[0]
        submit = submission.get("worker_submit_tick")
        visible = submission.get("first_control_visible_tick")
        identity_chain = identity_chain and (
            type(submit) is int and type(visible) is int and visible - submit == 5
            and submission.get("injection_status") == "matched"
            and submission.get("target_visible_tick") == visible
            and submission.get("requested_delivery_ticks") == 5
            and submission.get("actual_delivery_ticks") == 5
            and delivery.get("worker_submit_tick") == submit
            and delivery.get("first_control_visible_tick") == visible)
    chain = trial.get("delivery_chain") or {}
    first = next((sample for sample in trial.get("samples", ())
                  if sample.get("verified_command_index") == 0), {})
    ticks = [chain.get(key) for key in (
        "worker_submit_tick", "receiver_observed_result_tick", "registry_publish_tick",
        "first_control_visible_tick", "first_control_consume_tick", "result_take_tick")]
    identity_chain = bool(identity_chain and trial.get("complete_delivery_chain")
        and chain.get("identity") == trial.get("motion_identity") == first.get("motion_identity")
        and any(row.get("identity") == chain.get("identity") for row in submissions)
        and all(type(tick) is int for tick in ticks)
        and ticks == sorted(ticks)
        and type(first.get("grant_movement_tick")) is int
        and type(first.get("expected_movement_tick")) is int
        and ticks[-1] <= first["grant_movement_tick"] < first["expected_movement_tick"]
        and all(sample.get("actual_minus_latest_ticks") is not None
                and sample["actual_minus_latest_ticks"] <= 0
                for sample in trial.get("samples", ())
                if sample.get("verified_command_index") is not None))
    safe_complete = bool(
        one_case
        and _b10_coordinator_completed(trial)
        and runtime_state == "ready"
    )
    summary = dict(schema_version="mc2p.d113-targeted-probe.v1", mode="targeted_probe",
                   partial=False, matrix_complete=False, formal_acceptance_eligible=False,
                   targeted_probe=_D113_TARGETED_PROBE, coordinator_preregistered_count=1,
                   coordinator_executed_count=len(trials), coordinator_trials=trials,
                   runtime_state=runtime_state, actual_5_tick_identity_chain=identity_chain)
    checks = [dict(name="d113_targeted_one_case_only", passed=one_case),
              dict(name="d113_targeted_actual_5_tick_identity_chain", passed=identity_chain),
              dict(name="d113_targeted_safe_complete_and_runtime_usable", passed=safe_complete)]
    return summary, checks


@dataclass(slots=True)
class _DelayedWorkerSubmission:
    generation: int
    identity: str
    submit_tick: int
    worker_observe_tick: int | None = None
    wrapper_publish_tick: int | None = None
    discarded_as_stale: bool = False
    job_anchor_movement_tick: int | None = None
    operation: str | None = None
    requested_delivery_ticks: int = 0
    target_visible_tick: int | None = None
    first_control_visible_tick: int | None = None
    injection_status: str = "incomplete"

    def evidence(self) -> dict:
        return {
            "identity": self.identity,
            "submit_tick": self.submit_tick,
            "worker_observe_tick": self.worker_observe_tick,
            "wrapper_publish_tick": self.wrapper_publish_tick,
            "discarded_as_stale": self.discarded_as_stale,
            "job_anchor_movement_tick": self.job_anchor_movement_tick,
            "operation": self.operation,
            "worker_submit_tick": self.submit_tick,
            "requested_delivery_ticks": self.requested_delivery_ticks,
            "target_visible_tick": self.target_visible_tick,
            "first_control_visible_tick": self.first_control_visible_tick,
            "actual_delivery_ticks": (None if self.first_control_visible_tick is None
                                      else self.first_control_visible_tick - self.submit_tick),
            "injection_status": self.injection_status,
        }


def _motion_worker_item_key(value) -> tuple[str, object] | None:
    identity = getattr(value, "work_identity", None)
    if identity is not None:
        return "identity", identity
    request = getattr(value, "request", None)
    token = None if request is None else getattr(request, "job_token", None)
    if token is None:
        token = getattr(value, "job_token", None)
    return None if token is None else ("job_token", token)


class _MovementTickDelayedMotionWorker:
    """Harness-only first-control-read gate; receiver publication stays natural.

    Only never-return withholds results on the worker port, so unfinished work
    continues to occupy real inbox slots. The real worker remains outer-owned.
    """

    def __init__(self, worker) -> None:
        self._worker = worker
        self._lock = threading.Lock()
        self._generation = 0
        self._scenario: _B10CoordinatorScenario | None = None
        self._movement_tick: int | None = None
        self._submissions: dict[
            tuple[str, object], _DelayedWorkerSubmission
        ] = {}
        self._trial_submissions: list[_DelayedWorkerSubmission] = []
        self._held: list[
            tuple[object, _DelayedWorkerSubmission, int | None]
        ] = []
        self._stale_result_count = 0
        self._gate_inbox = None
        self._gate_originals = {}

    def install_read_gate(self, inbox) -> None:
        from mc2p.motion_nav.async_work import AsyncWorkIdentity
        from mc2p.contracts.common import require_nonnegative_int
        from mc2p.motion_nav.motion_worker import MotionResultInbox
        if type(inbox) is not MotionResultInbox:
            raise TypeError("B10 gate requires the existing typed inbox instance")
        if self._gate_inbox is not None:
            raise RuntimeError("B10 read gate already installed")
        self._gate_inbox = inbox
        self._gate_originals = {name: getattr(inbox, name) for name in ("peek", "take", "query_prepared")}

        def read(name, identity, **kwargs):
            if type(identity) is not AsyncWorkIdentity:
                # Invoke the public validator before the harness can hide work.
                return self._gate_originals[name](identity, **kwargs)
            if name == "query_prepared":
                require_nonnegative_int(kwargs["movement_tick"], "prepared query movement tick")
            with self._lock:
                record = self._submissions.get(("identity", identity))
                tick = self._movement_tick
                if (self._scenario is None or record is None
                        or record.generation != self._generation):
                    # A registered request can be polled before its worker
                    # accepts it. Missing submission alone is not a stale result.
                    if record is not None:
                        self._stale_result_count += 1
                        record.discarded_as_stale = True
                        record.injection_status = "old_generation"
                    return invisible(name)
                target = record.target_visible_tick
                if self._scenario.never_return or tick is None or (target is not None and tick < target):
                    return invisible(name)
            value = self._gate_originals[name](identity, **kwargs)
            ready = (value is not None if name == "peek" else bool(value) if name == "take"
                     else value.status is PreparedQueryStatus.READY_MATCH)
            with self._lock:
                if record.first_control_visible_tick is None:
                    if ready:
                        record.first_control_visible_tick = tick
                        if record.injection_status == "incomplete":
                            record.injection_status = ("matched" if target is None or tick == target
                                                       else "control_tick_skipped")
                    elif target is not None and tick >= target and record.injection_status == "incomplete":
                        record.injection_status = ("worker_not_ready" if tick == target
                                                   else "control_tick_skipped")
            return value

        def invisible(name):
            return None if name == "peek" else () if name == "take" else inbox._NOT_READY

        from mc2p.motion_nav.motion_worker import PreparedQueryStatus
        inbox.peek = lambda identity: read("peek", identity)
        inbox.take = lambda identity: read("take", identity)
        inbox.query_prepared = lambda identity, *, movement_tick: read(
            "query_prepared", identity, movement_tick=movement_tick)

    def _remove_read_gate(self) -> None:
        if self._gate_inbox is not None:
            for name, native in self._gate_originals.items():
                setattr(self._gate_inbox, name, native)
        self._gate_inbox = None
        self._gate_originals = {}

    def begin_trial(
            self, scenario: _B10CoordinatorScenario, *, movement_tick: int,
    ) -> None:
        if type(scenario) is not _B10CoordinatorScenario:
            raise TypeError("B10 delayed worker needs a typed scenario")
        if type(movement_tick) is not int or movement_tick < 0:
            raise ValueError("B10 delayed worker movement tick is invalid")
        with self._lock:
            if self._scenario is not None:
                raise RuntimeError("B10 delayed worker trial is already active")
            self._generation += 1
            self._submissions = {key: record for key, record in self._submissions.items()
                                 if record.generation >= self._generation - 1}
            self._scenario = scenario
            self._movement_tick = movement_tick
            self._trial_submissions = []
            self._held = []
            self._stale_result_count = 0

    def publish_movement_tick(self, movement_tick: int) -> None:
        if type(movement_tick) is not int or movement_tick < 0:
            raise ValueError("B10 delayed worker movement tick is invalid")
        with self._lock:
            if self._scenario is None:
                return
            if (self._movement_tick is not None
                    and movement_tick < self._movement_tick):
                raise ValueError("B10 movement tick cannot go backwards")
            self._movement_tick = movement_tick

    def submit(self, job) -> bool:
        key = _motion_worker_item_key(job)
        with self._lock:
            if self._scenario is None or self._movement_tick is None:
                raise RuntimeError("B10 delayed worker has no active trial")
            if key is None:
                raise TypeError("B10 delayed worker cannot identify submitted work")
            record = _DelayedWorkerSubmission(
                self._generation, repr(getattr(key[1], "key", key[1])), self._movement_tick,
                job_anchor_movement_tick=getattr(getattr(job, "anchor", None), "movement_tick_id", None),
                operation=getattr(getattr(job, "operation", None), "value", None),
                requested_delivery_ticks=self._scenario.worker_delay_ticks,
                target_visible_tick=(None if self._scenario.never_return or self._scenario.kind == "baseline"
                                     else self._movement_tick + self._scenario.worker_delay_ticks),
            )
            self._submissions[key] = record
            prepared_identity = getattr(getattr(job, "request", None), "async_identity", None)
            if prepared_identity is not None:
                self._submissions[("identity", prepared_identity)] = record
                record.identity = repr(prepared_identity.key)
            self._trial_submissions.append(record)
        submitted = self._worker.submit(job)
        if not submitted:
            with self._lock:
                self._submissions.pop(key, None)
                if prepared_identity is not None:
                    self._submissions.pop(("identity", prepared_identity), None)
                self._trial_submissions.remove(record)
        return submitted

    def poll_available(self) -> tuple[object, ...]:
        incoming = self._worker.poll_available()
        with self._lock:
            tick = self._movement_tick
            scenario = self._scenario
            generation = self._generation
            for result in incoming:
                key = _motion_worker_item_key(result)
                record = None if key is None else self._submissions.get(key)
                if (record is None or scenario is None
                        or record.generation != generation):
                    if record is not None:
                        record.discarded_as_stale = True
                    self._stale_result_count += 1
                    continue
                record.worker_observe_tick = tick
                due_tick = None if scenario.never_return else record.submit_tick
                self._held.append((result, record, due_tick))
            released: list[object] = []
            retained = []
            for result, record, due_tick in self._held:
                if (scenario is not None and not scenario.never_return
                        and tick is not None and due_tick is not None
                        and tick >= due_tick):
                    record.wrapper_publish_tick = tick
                    released.append(result)
                else:
                    retained.append((result, record, due_tick))
            self._held = retained
            return tuple(released)

    def end_trial(self) -> dict:
        self._remove_read_gate()
        with self._lock:
            if self._scenario is None:
                raise RuntimeError("B10 delayed worker has no active trial")
            scenario = self._scenario
            held = sum(
                record.generation == self._generation
                for _, record, _ in self._held
            )
            evidence = {
                "scenario": scenario.name,
                "scenario_kind": scenario.kind,
                "worker_delay_ticks": scenario.worker_delay_ticks,
                "never_return": scenario.never_return,
                "submissions": [
                    record.evidence() for record in self._trial_submissions
                ],
                "held_result_count": held,
                "stale_result_count": self._stale_result_count,
            }
            self._scenario = None
            self._movement_tick = None
            self._trial_submissions = []
            self._held = []
            return evidence

    def close(self) -> None:
        # The outer B10 lifecycle owns and closes the real process worker.
        self._remove_read_gate()
        with self._lock:
            self._scenario = None
            self._movement_tick = None
            self._trial_submissions = []
            self._held = []

    def is_alive(self) -> bool:
        return self._worker.is_alive()


@dataclass(frozen=True, slots=True)
class _B10FormalDriverFrame:
    """One paired prepare/control/adopt frame from the shared B10 loop."""

    movement_tick_before: int
    movement_tick_after: int
    decision_observation_sequence_id: int
    observation_age_ns: int
    proposal_elapsed_ns: int
    control_wall_ns: int
    backend_elapsed_ns: int
    backend_blocking_io_ns: int
    proposal: object
    result: RuntimeStepResultV1
    diagnostics: NavigationFrameDiagnostics | None


@dataclass(frozen=True, slots=True)
class _B10FormalDriverScenarioResult:
    """Bounded outcome of one component or Fabric coordinator scenario."""

    terminal_state: str
    terminal_reason: str
    bounded_terminal: bool
    ordered_source_id: str | None
    source_released: bool
    manual_intent_selected: bool
    old_session_intent_selected: bool
    prepared_frame_count: int
    adopted_frame_count: int
    discarded_frame_count: int
    waiting_frame_count: int
    observed_action_routes: tuple[tuple[str, ...], ...]
    observed_route_endpoints: tuple[tuple[float, float, float], ...]
    frames: tuple[_B10FormalDriverFrame, ...]
    worker_injection: dict


def _b10_body_pose_is_settled(
    body,
    *,
    target_position: tuple[float, float, float],
    yaw_degrees: float,
    position_tolerance: float,
) -> bool:
    yaw_error = abs(
        (math.degrees(body.yaw_radians) - yaw_degrees + 180.0) % 360.0
        - 180.0
    )
    horizontal_speed = math.hypot(
        body.velocity_blocks_per_second[0],
        body.velocity_blocks_per_second[2],
    )
    return bool(
        math.dist(body.position, target_position) <= position_tolerance
        and body.is_on_ground
        and horizontal_speed <= .03
        and yaw_error <= 1.0
    )


def _wait_for_b10_teleport_settlement(
    advance_frame: Callable[[], object],
    *,
    starting_movement_tick: int,
    starting_position: tuple[float, float, float],
    starting_yaw_degrees: float,
    starting_on_ground: bool,
    target_position: tuple[float, float, float],
    yaw_degrees: float,
    position_tolerance: float,
    max_frames: int = 16,
    minimum_ticks: int = 3,
    stable_frames: int = 2,
):
    """Wait past the server command boundary and require repeated stability."""
    if (type(max_frames) is not int or type(minimum_ticks) is not int
            or type(stable_frames) is not int or max_frames < 1
            or minimum_ticks < 1 or stable_frames < 1
            or minimum_ticks > max_frames or stable_frames > max_frames):
        raise ValueError("B10 teleport settlement bounds are invalid")
    consecutive = 0
    last_body = None
    previous_movement_tick = starting_movement_tick
    observed_application = False
    starting_yaw_error = abs(
        (starting_yaw_degrees - yaw_degrees + 180.0) % 360.0 - 180.0
    )
    same_target_pose = bool(
        math.dist(starting_position, target_position) <= position_tolerance
        and starting_yaw_error <= 1.0
    )
    if type(starting_movement_tick) is not int or starting_movement_tick < 0:
        raise ValueError("B10 teleport starting movement tick is invalid")
    for _ in range(max_frames):
        last_body = advance_frame()
        if _b10_body_pose_is_settled(
            last_body,
            target_position=target_position,
            yaw_degrees=yaw_degrees,
            position_tolerance=position_tolerance,
        ):
            consecutive += 1
        else:
            consecutive = 0
        movement_tick = last_body.movement_tick_id
        if (type(movement_tick) is not int
                or movement_tick <= previous_movement_tick):
            raise RuntimeError("B10 teleport movement tick did not advance monotonically")
        previous_movement_tick = movement_tick
        target_yaw_error = abs(
            (math.degrees(last_body.yaw_radians) - yaw_degrees
             + 180.0) % 360.0 - 180.0
        )
        # A same-pose teleport cannot be distinguished from the old stable
        # observation by position or yaw. Fabric consistently exposes its
        # arrival as a one-frame on_ground=false transition. For a different
        # target pose, entering the target tolerance is the causal evidence.
        application_seen_now = (
            starting_on_ground and not last_body.is_on_ground
            if same_target_pose
            else math.dist(last_body.position, target_position)
                <= position_tolerance and target_yaw_error <= 1.0
        )
        observed_application = bool(
            observed_application or application_seen_now
        )
        if not observed_application:
            consecutive = 0
        if (observed_application
                and movement_tick - starting_movement_tick >= minimum_ticks
                and consecutive >= stable_frames):
            return last_body
    if last_body is None:
        raise RuntimeError("B10 teleport settlement did not advance")
    yaw_error = abs(
        (math.degrees(last_body.yaw_radians) - yaw_degrees + 180.0) % 360.0
        - 180.0
    )
    horizontal_speed = math.hypot(
        last_body.velocity_blocks_per_second[0],
        last_body.velocity_blocks_per_second[2],
    )
    raise RuntimeError(
        "B10 teleport application was not observed or did not settle: "
        f"target={target_position}, actual={last_body.position}, "
        f"on_ground={last_body.is_on_ground}, speed={horizontal_speed}, "
        f"yaw_error={yaw_error}"
    )


def _b10_frame_has_no_movement_intent(proposals) -> bool:
    """Use the proposed body ownership, not route submit_input, as waiting."""
    return not any(
        ordered.intent.movement is not None
        for control in proposals
        for ordered in control.intents
    )


def _runtime_movement_tick(runtime) -> int:
    observation = runtime.observation
    own = None if observation is None else observation.self_state.value
    tick = None if own is None else own.movement_tick_id
    if type(tick) is not int or tick < 0:
        raise RuntimeError("B10 formal driver requires a movement tick")
    return tick


def _b10_stale_intent_seen(
    seen_intents: set[str],
    current_intents: set[str],
    selected_intents: set[str],
    candidate_intents: set[str],
) -> bool:
    """Return whether arbitration still mentions an older session intent."""
    return bool(
        (seen_intents - current_intents)
        .intersection(selected_intents | candidate_intents)
    )


def _run_b10_formal_driver_scenario(
    *,
    runtime,
    session: NavigationSession,
    delayed_motion_worker: _MovementTickDelayedMotionWorker,
    scenario: _B10CoordinatorScenario,
    goal_state: GoalState,
    goal_id: str,
    goal_revision: int,
    task: TaskIntentV0,
    behavior: BehaviorProfileV0,
    expected_start_position: tuple[float, float, float],
    expected_start_yaw_degrees: float,
    manual_source=None,
    clock_ns: Callable[[], int] = time.perf_counter_ns,
    max_control_frames: int = 64,
    after_frame: Callable[[_B10FormalDriverFrame], None] | None = None,
) -> _B10FormalDriverScenarioResult:
    """Run one B10 scenario through the same formal driver on every frame.

    The game backend can be Fabric or CalculatorBackend.  Only the backend and
    clock differ; worker delay, navigation preparation, Runtime arbitration,
    result adoption and source release remain on this one path.
    """
    if type(session) is not NavigationSession:
        raise TypeError("B10 formal driver requires NavigationSession")
    if type(delayed_motion_worker) is not _MovementTickDelayedMotionWorker:
        raise TypeError("B10 formal driver requires the delayed worker gate")
    if type(scenario) is not _B10CoordinatorScenario:
        raise TypeError("B10 formal driver requires a typed scenario")
    if type(goal_state) is not GoalState:
        raise TypeError("B10 formal driver requires a GoalState")
    if type(max_control_frames) is not int or max_control_frames < 1:
        raise ValueError("B10 formal driver frame limit must be positive")
    manual_intents: set[str] = set()
    manual_intent_prefix = None
    if manual_source is not None:
        manual_intent_prefix = f"{manual_source.source_id}/"
        manual_intents.update(runtime.cancel_source(manual_source.source_id))

    driver = RuntimeNavigationDriver(
        runtime,
        session,
        clock_ns=clock_ns,
    )
    frames: list[_B10FormalDriverFrame] = []
    seen_session_intents: set[str] = set()
    manual_intent_selected = False
    old_session_intent_selected = False
    worker_injection: dict = {}
    primary_error: BaseException | None = None
    prepared_frame_count = 0
    adopted_frame_count = 0
    discarded_frame_count = 0
    waiting_frame_count = 0
    observed_action_routes: list[tuple[str, ...]] = []
    observed_route_endpoints: list[tuple[float, float, float]] = []
    registered_source = None

    movement_tick = _runtime_movement_tick(runtime)
    delayed_motion_worker.begin_trial(scenario, movement_tick=movement_tick)
    delayed_motion_worker.install_read_gate(session._motion_result_inbox)
    try:
        start_body = _runtime_navigation_frame(runtime).body
        if not _b10_body_pose_is_settled(
            start_body,
            target_position=expected_start_position,
            yaw_degrees=expected_start_yaw_degrees,
            position_tolerance=.03,
        ):
            raise RuntimeError(
                "B10 formal driver start is not settled at the fixture origin"
            )
        driver.start(
            goal_id,
            goal_revision,
            goal_state,
            clock_ns(),
        )
        ordered_source_id = (
            None if driver.source is None else driver.source.source_id
        )
        registered_source = driver.source
        for _ in range(max_control_frames):
            tick_before = _runtime_movement_tick(runtime)
            delayed_motion_worker.publish_movement_tick(tick_before)
            session._motion_result_inbox.publish_diagnostic_movement_tick(
                tick_before,
            )
            decision_observation_sequence_id = runtime.observation.sequence_id
            observation_age_ns = max(
                0,
                time.perf_counter_ns()
                - runtime.observation.received_at_monotonic_ns,
            )
            owner_deadline = min(
                task.deadline_monotonic_ns,
                clock_ns() + 5_000_000_000,
            )
            proposal_started_ns = time.perf_counter_ns()
            proposals = driver.prepare_proposals(owner_deadline)
            prepared_frame_count += 1
            proposal_elapsed_ns = time.perf_counter_ns() - proposal_started_ns
            proposal = driver.prepared_proposal
            if proposal is None:
                driver.discard_prepared()
                discarded_frame_count += 1
                raise RuntimeError("B10 formal driver omitted its prepared proposal")
            if _b10_frame_has_no_movement_intent(proposals):
                waiting_frame_count += 1
            current_intents = {
                ordered.intent.intent_id
                for control in proposals
                for ordered in control.intents
            }
            prepared_streams = driver.prepared_streams
            control_started_ns = time.perf_counter_ns()
            backend_started_ns = runtime.backend_elapsed_ns_total
            blocking_started_ns = runtime.backend_blocking_io_ns_total
            try:
                result = runtime.control_frame(
                    task,
                    behavior,
                    owner_deadline,
                    proposals=proposals,
                    prepared_streams=prepared_streams,
                )
            except BaseException:
                driver.discard_prepared()
                discarded_frame_count += 1
                raise
            control_wall_ns = time.perf_counter_ns() - control_started_ns
            backend_elapsed_ns = (
                runtime.backend_elapsed_ns_total - backend_started_ns
            )
            backend_blocking_io_ns = (
                runtime.backend_blocking_io_ns_total - blocking_started_ns
            )
            driver.adopt_result(result)
            adopted_frame_count += 1
            active_route = session.active_route
            if active_route is not None:
                route_kinds = tuple(
                    type(action).__name__
                    for action in active_route.action_route.actions
                )
                if route_kinds not in observed_action_routes:
                    observed_action_routes.append(route_kinds)
                last_action = active_route.action_route.actions[-1]
                fixed_route = getattr(last_action, "fixed_route", None)
                if fixed_route is not None:
                    point = fixed_route.points[-1]
                    endpoint = (point.x, point.y, point.z)
                    if endpoint not in observed_route_endpoints:
                        observed_route_endpoints.append(endpoint)
            tick_after = _runtime_movement_tick(runtime)
            diagnostics = driver.last_frame_diagnostics
            selected = set(
                () if result.decision is None
                else (intent_id for _, intent_id in result.decision.selected_intents)
            )
            candidates = set(
                () if result.decision is None
                else result.decision.candidate_intent_ids
            )
            manual_intent_selected = bool(
                manual_intent_selected
                or manual_intents.intersection(selected | candidates)
                or (
                    manual_intent_prefix is not None
                    and any(
                        intent_id.startswith(manual_intent_prefix)
                        for intent_id in selected | candidates
                    )
                )
            )
            old_session_intent_selected = bool(
                old_session_intent_selected
                or _b10_stale_intent_seen(
                    seen_session_intents,
                    current_intents,
                    selected,
                    candidates,
                )
            )
            seen_session_intents.update(current_intents)
            frame = _B10FormalDriverFrame(
                tick_before,
                tick_after,
                decision_observation_sequence_id,
                observation_age_ns,
                proposal_elapsed_ns,
                control_wall_ns,
                backend_elapsed_ns,
                backend_blocking_io_ns,
                proposal,
                result,
                diagnostics,
            )
            frames.append(frame)
            if after_frame is not None:
                after_frame(frame)

            if runtime.state is not RuntimeStateV1.READY:
                break
            if driver.state in {
                RuntimeNavigationDriverState.SUCCESS,
                RuntimeNavigationDriverState.FAILED,
                RuntimeNavigationDriverState.CANCELLED,
                RuntimeNavigationDriverState.STOPPED,
                RuntimeNavigationDriverState.INTERACTION_REQUIRED,
            }:
                if driver.release("b10_formal_scenario_terminal"):
                    break
        else:
            if driver.source is not None:
                released = driver.release("b10_formal_scenario_frame_limit")
                if not released or driver.source is not None:
                    raise RuntimeError(
                        "B10 formal driver frame limit left body responsibility "
                        "unresolved"
                    )
            raise RuntimeError("B10 formal driver reached its frame limit")
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error: BaseException | None = None
        if driver.has_prepared_frame:
            try:
                driver.discard_prepared()
                discarded_frame_count += 1
            except BaseException as error:
                cleanup_error = error
        if primary_error is not None and driver.source is not None:
            try:
                driver.release("b10_formal_scenario_exception")
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        try:
            worker_injection = delayed_motion_worker.end_trial()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        try:
            session.close()
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error

    terminal_state = session.report.state.value
    terminal_reason = session.report.reason
    source_released = bool(
        driver.source is None
        and (
            registered_source is None
            or not runtime.has_ordered_source(registered_source)
        )
    )
    bounded_terminal = bool(
        source_released
        and runtime.state is RuntimeStateV1.READY
        and driver.state in {
            RuntimeNavigationDriverState.SUCCESS,
            RuntimeNavigationDriverState.FAILED,
            RuntimeNavigationDriverState.CANCELLED,
            RuntimeNavigationDriverState.STOPPED,
            RuntimeNavigationDriverState.INTERACTION_REQUIRED,
        }
    )
    return _B10FormalDriverScenarioResult(
        terminal_state,
        terminal_reason,
        bounded_terminal,
        ordered_source_id,
        source_released,
        manual_intent_selected,
        old_session_intent_selected,
        prepared_frame_count,
        adopted_frame_count,
        discarded_frame_count,
        waiting_frame_count,
        tuple(observed_action_routes),
        tuple(observed_route_endpoints),
        tuple(frames),
        worker_injection,
    )


class _B10EvidenceOverflow(RuntimeError):
    """The bounded B10 evidence buffer could not retain every row."""


def _worker_injection_valid(trial: dict) -> bool:
    injection = trial.get("worker_injection", {})
    if injection.get("stale_result_count", 0):
        return False
    if trial["scenario_kind"] != "worker_delay":
        return True
    submissions = injection.get("submissions", ())
    return bool(submissions) and all(
        row["injection_status"] == "matched"
        and row["first_control_visible_tick"] == row["target_visible_tick"]
        and row["actual_delivery_ticks"] == row["requested_delivery_ticks"]
        for row in submissions)


def _b10_coordinator_source_and_safety_valid(trial: dict) -> bool:
    recorded = trial.get("recorded_control_frames")
    adopted = trial.get("adopted_frame_count")
    return bool(
        _worker_injection_valid(trial)
        and type(recorded) is int
        and recorded > 0
        and recorded == adopted
        and trial.get("bounded_terminal") is True
        and trial.get("landed") is True
        and trial.get("damage_points") == 0.0
        and trial.get("source_released") is True
        and trial.get("old_manual_intent_selected") is False
        and trial.get("old_session_intent_selected") is False
        and trial.get("left_start_support") is False
    )


def _b10_coordinator_completed(trial: dict) -> bool:
    return bool(
        _b10_coordinator_source_and_safety_valid(trial)
        and trial.get("terminal_state")
            == NavigationSessionState.COMPLETE.value
        and trial.get("horizontal_error_blocks", float("inf")) <= .2
        and trial.get("level_error_blocks", float("inf")) <= .03
        and trial.get("horizontal_collisions") == 0
    )


def _b10_coordinator_typed_safe_terminal(trial: dict) -> bool:
    reason = trial.get("terminal_reason")
    unresolved = (
        "deadline",
        "unavailable",
        "unresolved",
    )
    return bool(
        _b10_coordinator_source_and_safety_valid(trial)
        and trial.get("terminal_state") in {
            NavigationSessionState.CANCELLED.value,
            NavigationSessionState.FAILED.value,
            NavigationSessionState.REQUIRES_INTERACTION.value,
        }
        and type(reason) is str
        and reason
        and "harness_wall_timeout" not in reason
        and not any(marker in reason.lower() for marker in unresolved)
    )


def _b10_coordinator_never_return_safe(trial: dict) -> bool:
    reason = trial.get("terminal_reason")
    injection = trial.get("worker_injection", {})
    return bool(
        _b10_coordinator_typed_safe_terminal(trial)
        and trial.get("scenario_kind") == "worker_never_return"
        and "motion_solver_request_expired" in reason
        and trial.get("waiting_polls", 0) > 0
        and trial.get("verified_command_count") == 0
        and trial.get("jump_pulses") == 0
        and not trial.get("left_start_support")
        and injection.get("held_result_count") == 2
    )


def _b10_delivery_chain_fields(samples, receiver_delivery) -> dict:
    first_verified = next((
        sample for sample in samples
        if sample["verified_command_index"] == 0
    ), None)
    actual_application_tick = (
        None if first_verified is None
        or not first_verified["actual_movement_ticks"]
        else min(first_verified["actual_movement_ticks"])
    )
    motion_identity = (
        None if first_verified is None
        else first_verified["motion_identity"]
    )
    delivery_chain = next((
        item for item in receiver_delivery
        if item["identity"] == motion_identity
    ), None)
    complete_delivery_chain = (
        first_verified is not None
        and delivery_chain is not None
        and all(type(value) is int for value in (
            delivery_chain["worker_submit_tick"],
            delivery_chain["receiver_observed_result_tick"],
            delivery_chain["registry_publish_tick"],
            delivery_chain["first_control_consume_tick"],
            delivery_chain["worker_elapsed_ns"],
            delivery_chain["worker_queue_after_completion_ns"],
            first_verified["grant_movement_tick"],
            first_verified["expected_movement_tick"],
            first_verified["latest_movement_tick"],
            actual_application_tick,
        ))
        and delivery_chain["worker_submit_tick"]
            <= delivery_chain["receiver_observed_result_tick"]
            <= delivery_chain["registry_publish_tick"]
            <= delivery_chain["first_control_consume_tick"]
            <= first_verified["grant_movement_tick"]
            < actual_application_tick
    )
    return {
        "motion_identity": motion_identity,
        "delivery_chain": delivery_chain,
        "complete_delivery_chain": complete_delivery_chain,
        "first_verified_application_tick": actual_application_tick,
    }


def _b10_formal_trial_fields(
    formal_result: _B10FormalDriverScenarioResult,
    *,
    samples,
    recorded_control_frames: int,
    start_position: tuple[float, float, float],
) -> dict:
    """Build the strict lifecycle fields shared by Fabric and component trials."""
    verified_command_count = sum(
        sample["verified_command_index"] is not None
        for sample in samples
    )
    left_start_support = _b10_uncommanded_body_departed(
        formal_result.frames,
        verified_command_count=verified_command_count,
        start_position=start_position,
    )
    return {
        "terminal_state": formal_result.terminal_state,
        "terminal_reason": formal_result.terminal_reason,
        "ordered_source_id": formal_result.ordered_source_id,
        "source_release_status": (
            "explicitly_released"
            if formal_result.source_released else "not_released"
        ),
        "source_released": formal_result.source_released,
        "bounded_terminal": formal_result.bounded_terminal,
        "old_manual_intent_selected": formal_result.manual_intent_selected,
        "old_session_intent_selected": (
            formal_result.old_session_intent_selected
        ),
        "recorded_control_frames": recorded_control_frames,
        "adopted_frame_count": formal_result.adopted_frame_count,
        "verified_command_count": verified_command_count,
        "jump_pulses": sum(
            int(sample["movement"]["jump"])
            for sample in samples
        ),
        "left_start_support": left_start_support,
        "worker_injection": formal_result.worker_injection,
    }


def _build_b10_coordinator_trial(
    *,
    trial_name: str,
    scenario: _B10CoordinatorScenario,
    formal_result: _B10FormalDriverScenarioResult,
    samples,
    receiver_delivery,
    recorded_control_frames: int,
    start_position: tuple[float, float, float],
    target_position: tuple[float, float, float],
    final_position: tuple[float, float, float],
    landed: bool,
    damage_points: float,
) -> dict:
    """Assemble the exact coordinator record used by components and Fabric."""
    delivery_fields = _b10_delivery_chain_fields(samples, receiver_delivery)
    formal_fields = _b10_formal_trial_fields(
        formal_result,
        samples=samples,
        recorded_control_frames=recorded_control_frames,
        start_position=start_position,
    )
    return dict(
        name=trial_name,
        scenario=scenario.name,
        scenario_kind=scenario.kind,
        direction_index=scenario.direction_index,
        repetition=scenario.repetition,
        fabric_supported=scenario.fabric_supported,
        unsupported_reason=scenario.unsupported_reason,
        worker_delay_ticks=scenario.worker_delay_ticks,
        worker_never_return=scenario.never_return,
        waiting_polls=formal_result.waiting_frame_count,
        entry_alignment_frames=sum(
            sample["reason_code"] == "aligning_verified_motion_heading"
            for sample in samples
        ),
        command_count=len(samples),
        final_position=list(final_position),
        horizontal_error_blocks=math.hypot(
            final_position[0] - target_position[0],
            final_position[2] - target_position[2],
        ),
        level_error_blocks=abs(final_position[1] - target_position[1]),
        landed=landed,
        damage_points=damage_points,
        worker_delivery=receiver_delivery,
        **delivery_fields,
        horizontal_collisions=sum(
            int(sample["horizontal_collision"])
            for sample in samples
        ),
        samples=samples,
        **formal_fields,
    )


def _summarize_b10_coordinator_trials(
    coordinator_trials,
    *,
    preregistered_count: int,
) -> dict:
    """Return the coordinator-only counts consumed by the outer acceptance."""
    executed = [
        trial for trial in coordinator_trials
        if trial.get("fabric_supported") is True
    ]
    unsupported = [
        trial for trial in coordinator_trials
        if trial.get("fabric_supported") is False
    ]
    return dict(
        coordinator_preregistered_count=preregistered_count,
        coordinator_executed_count=len(executed),
        coordinator_unsupported_count=len(unsupported),
        coordinator_validation_count=len(executed),
        coordinator_validation_success_count=sum(
            trial["waiting_polls"] > 0
            and _b10_coordinator_completed(trial)
            for trial in executed
        ),
        coordinator_baseline_success_count=sum(
            _b10_coordinator_completed(trial)
            for trial in executed
            if trial["scenario_kind"] == "baseline"
        ),
        coordinator_delay_2_5_success_count=sum(
            _b10_coordinator_completed(trial)
            for trial in executed
            if trial["scenario_kind"] == "worker_delay"
            and trial["worker_delay_ticks"] in {2, 5}
        ),
        coordinator_delay_10_safe_count=sum(
            _b10_coordinator_completed(trial)
            or _b10_coordinator_typed_safe_terminal(trial)
            for trial in executed
            if trial["scenario_kind"] == "worker_delay"
            and trial["worker_delay_ticks"] == 10
        ),
        coordinator_never_return_safe_count=sum(
            _b10_coordinator_never_return_safe(trial)
            for trial in executed
            if trial["scenario_kind"] == "worker_never_return"
        ),
    )


def _b10_uncommanded_body_departed(
    frames,
    *,
    verified_command_count: int,
    start_position: tuple[float, float, float],
) -> bool:
    """Reject real uncommanded displacement, not a one-frame ground flag blip."""
    if verified_command_count != 0:
        return False
    for frame in frames:
        body = frame.result.observation.self_state.value
        position = body.position
        if position.y < start_position[1] - .01:
            return True
        if not body.is_on_ground and (
            abs(position.x - start_position[0]) > .01
            or abs(position.y - start_position[1]) > .01
            or abs(position.z - start_position[2]) > .01
        ):
            return True
    return False


def _b10_coordinator_delivery_gate(trial: dict) -> bool:
    if not _worker_injection_valid(trial):
        return False
    verified = [
        sample for sample in trial["samples"]
        if sample["verified_command_index"] is not None
    ]
    if not verified:
        return True
    first = next((
        sample for sample in verified
        if sample["verified_command_index"] == 0
    ), None)
    chain = trial["delivery_chain"]
    return bool(
        first is not None
        and trial["complete_delivery_chain"]
        and chain["first_control_consume_tick"]
            <= first["expected_movement_tick"] - 1
        and first["grant_movement_tick"]
            <= first["expected_movement_tick"] - 1
        and all(
            sample["actual_minus_expected_ticks"] is not None
            and sample["actual_minus_expected_ticks"] >= 0
            and sample["actual_minus_latest_ticks"] is not None
            and sample["actual_minus_latest_ticks"] <= 0
            for sample in verified
        )
    )


@dataclass(frozen=True, slots=True)
class _B10StreamSpec:
    max_rows: int
    max_bytes: int
    max_record_bytes: int

    def __post_init__(self) -> None:
        if type(self.max_rows) is not int or self.max_rows <= 0:
            raise ValueError("B10 stream max_rows must be positive")
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError("B10 stream max_bytes must be positive")
        if type(self.max_record_bytes) is not int or self.max_record_bytes <= 0:
            raise ValueError("B10 stream max_record_bytes must be positive")
        if self.max_record_bytes > self.max_bytes:
            raise ValueError("B10 stream record bound exceeds its byte bound")


# These limits exceed the largest formal B10 batch recorded before A5-R2 by at
# least 2x while bounding the total retained JSON.  The coordinator bounds
# cover the preregistered 56-case D112 matrix, including typed failures.
_B10_STREAM_SPECS: Mapping[str, _B10StreamSpec] = {
    "diagnostics.jsonl": _B10StreamSpec(8_192, 16 << 20, 8 << 10),
    "b10-gap-solver-trials.jsonl": _B10StreamSpec(256, 4 << 20, 64 << 10),
    "b10-coordinator-control-frames.jsonl": _B10StreamSpec(
        2_048, 8 << 20, 16 << 10,
    ),
    "b10-coordinator-trials.jsonl": _B10StreamSpec(64, 4 << 20, 64 << 10),
    "r4-moving-gap-negative-trials.jsonl": _B10StreamSpec(
        64, 1 << 20, 16 << 10,
    ),
    "b10-receiver-diagnostics.jsonl": _B10StreamSpec(
        16_384, 16 << 20, 4 << 10,
    ),
}


class _B10EvidenceBuffer:
    """Retain every B10 JSONL stream off the control path and flush once."""

    def __init__(
            self,
            directory: Path,
            *,
            stream_specs: Mapping[str, _B10StreamSpec] = _B10_STREAM_SPECS,
    ) -> None:
        if not isinstance(directory, Path):
            raise TypeError("B10 evidence directory must be a Path")
        if type(stream_specs) is not dict:
            stream_specs = dict(stream_specs)
        if not stream_specs:
            raise ValueError("B10 evidence streams cannot be empty")
        self._directory = directory
        self._specs = dict(stream_specs)
        self._records: dict[str, list[str]] = {
            name: [] for name in self._specs
        }
        self._bytes = dict.fromkeys(self._specs, 0)
        self._overflow = dict.fromkeys(self._specs, 0)
        self._receiver_seen: set[str] = set()
        self._duplicates = 0
        self._flushed = False
        self._flush_failures: list[str] = []

    @property
    def duplicate_count(self) -> int:
        return self._duplicates

    @property
    def overflow_count(self) -> int:
        return sum(self._overflow.values())

    @property
    def flush_failure_count(self) -> int:
        return len(self._flush_failures)

    def rows(self, stream: str) -> tuple[dict, ...]:
        self._require_stream(stream)
        return tuple(json.loads(row) for row in self._records[stream])

    def _require_stream(self, stream: str) -> _B10StreamSpec:
        if type(stream) is not str or stream not in self._specs:
            raise ValueError(f"unknown B10 evidence stream: {stream!r}")
        return self._specs[stream]

    def append(self, stream: str, row: object) -> bool:
        spec = self._require_stream(stream)
        encoded = strict_json_dumps(row)
        size = len(encoded.encode("utf-8")) + 1
        if (size > spec.max_record_bytes
                or len(self._records[stream]) >= spec.max_rows
                or self._bytes[stream] + size > spec.max_bytes):
            self._overflow[stream] += 1
            return False
        # The immutable JSON breaks caller references without retaining a
        # second, GC-tracked object tree during the control trials.
        self._records[stream].append(encoded)
        self._bytes[stream] += size
        return True

    def collect_receiver(self, inbox, trial: str, *, episode_id: str | None = None) -> None:
        if type(trial) is not str or not trial or len(trial) > 128:
            raise ValueError("receiver diagnostic trial id is invalid")
        for row in inbox.drain_receiver_diagnostics():
            if type(row) is not dict or len(row) > 16:
                raise ValueError("receiver diagnostic row is not a small record")
            fields: dict[str, str | int | bool | None] = {"trial": trial}
            if episode_id is not None:
                fields["episode_id"] = episode_id
            for key, value in row.items():
                if type(key) is not str or type(value) not in (str, int, bool, type(None)):
                    raise ValueError("receiver diagnostic row contains a non-scalar field")
                if type(value) is str and len(value) > 512:
                    raise ValueError("receiver diagnostic string is too large")
                fields[key] = value
            frozen = strict_json_dumps(fields)
            if frozen in self._receiver_seen:
                self._duplicates += 1
                continue
            if self.append("b10-receiver-diagnostics.jsonl", fields):
                self._receiver_seen.add(frozen)

    def flush(self) -> None:
        if self._flushed:
            if self.overflow_count:
                raise _B10EvidenceOverflow(
                    f"B10 evidence buffer overflowed by {self.overflow_count} rows"
                )
            return
        for stream, spec in self._specs.items():
            rows = list(self.rows(stream))
            if self._overflow[stream]:
                rows.append({
                    "schema_version": "mc2p.b10-evidence-buffer-overflow.v1",
                    "event": "buffer_overflow",
                    "stream": stream,
                    "max_rows": spec.max_rows,
                    "max_bytes": spec.max_bytes,
                    "overflow_count": self._overflow[stream],
                })
            if rows:
                write_jsonl_atomic(self._directory / stream, rows)
        self._flushed = True
        if self.overflow_count:
            raise _B10EvidenceOverflow(
                f"B10 evidence buffer overflowed by {self.overflow_count} rows"
            )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _traceback) -> bool:
        try:
            self.flush()
        except BaseException as flush_error:
            self._flush_failures.append(
                f"{type(flush_error).__name__}: {flush_error}"
            )
            if exc_type is None:
                raise
            if exc is not None and hasattr(exc, "add_note"):
                exc.add_note(
                    "B10 evidence flush also failed: "
                    f"{type(flush_error).__name__}: {flush_error}"
                )
        return False


# Historical tests and imports used this name.  The active implementation is
# the unified buffer above; no receiver-only production buffer remains.
_ReceiverDiagnosticOverflow = _B10EvidenceOverflow


def _bounded_fixture_feet_y(observed_y: float) -> int:
    """Keep the isolated B10 pit far enough above the Java world floor."""
    if not math.isfinite(observed_y):
        raise RuntimeError("B10 fixture received a nonfinite player height")
    return max(0, math.floor(observed_y + 1.0e-6))


def _inspection_support(position: tuple[int, int, int]) -> tuple[int, int, int]:
    """Return a lateral support from which a hidden pit cell is fully visible."""
    return position[0] + 2, position[1] - 1, position[2]


def _inspection_supports(
        position: tuple[int, int, int],
        *,
        excluded_columns: frozenset[tuple[int, int]] = frozenset(),
) -> tuple[tuple[int, int, int], ...]:
    x, y, z = position
    candidates = (
        _inspection_support(position),
        (x - 2, y - 1, z),
        (x, y - 1, z + 2),
        (x, y - 1, z - 2),
    )
    available = tuple(
        support for support in candidates
        if (support[0], support[2]) not in excluded_columns
    )
    if available:
        return available
    fallback = (
        (x + 3, y - 1, z),
        (x - 3, y - 1, z),
        (x, y - 1, z + 3),
        (x, y - 1, z - 3),
    )
    return tuple(
        support for support in fallback
        if (support[0], support[2]) not in excluded_columns
    )


def _information_retry_allowed(
        missing_cells: tuple[tuple[int, int, int], ...],
        seen_cells: set[tuple[int, int, int]], *,
        completed_rounds: int, max_rounds: int) -> bool:
    """Keep acquiring newly revealed facts within the rollout's finite horizon."""
    return (
        completed_rounds < max_rounds
        and any(position not in seen_cells for position in missing_cells)
    )


def _percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


class _GCPauseTimeline:
    """Bounded process-wide events; pair and attribute only after all trials."""

    def __init__(self, *, max_events: int = 65_536) -> None:
        if type(max_events) is not int or max_events <= 0:
            raise ValueError("GC timeline event limit must be positive")
        self.events: list[tuple[int, str, int]] = []
        self.max_events = max_events
        self.dropped_events = 0
        self.measurement_error_count = 0
        self.callback = self._record

    def _record(self, phase, info) -> None:
        # Never let optional instrumentation replace a solver exception. No
        # dictionaries, pairing, logging or attribution on the callback path.
        try:
            generation = info["generation"]
            if phase not in ("start", "stop") or type(generation) is not int or generation not in (0, 1, 2):
                raise ValueError("invalid GC callback event")
            if len(self.events) >= self.max_events:
                self.dropped_events += 1
                return
            self.events.append((generation, phase, time.perf_counter_ns()))
        except BaseException:
            self.measurement_error_count += 1

    def __enter__(self):
        gc.callbacks.append(self.callback)
        return self

    def close(self) -> None:
        # Identity avoids invoking equality on third-party callbacks.
        for index in range(len(gc.callbacks) - 1, -1, -1):
            if gc.callbacks[index] is self.callback:
                del gc.callbacks[index]

    def __exit__(self, *_args) -> bool:
        self.close()
        return False


def _attribute_solve_gc_pauses(trials: list[dict], timeline: _GCPauseTimeline) -> dict:
    """Intersect complete GC intervals with the frozen raw solve intervals.

    Final derived fields live in the summary's trials. Already accepted JSONL
    rows keep raw same-clock start/end values and their original byte budgets.
    """
    pending: dict[int, int] = {}
    intervals = []
    unpaired = 0
    for generation, phase, stamp in timeline.events:
        if phase == "start":
            unpaired += int(generation in pending)
            pending[generation] = stamp
        else:
            started = pending.pop(generation, None)
            if started is None or stamp < started:
                unpaired += 1
            else:
                intervals.append(dict(generation=generation, start_ns=started, end_ns=stamp))
    unpaired += len(pending)
    complete = not (unpaired or timeline.dropped_events or timeline.measurement_error_count)
    corrected = []
    for trial in trials:
        started, ended = trial["solve_started_ns"], trial["solve_ended_ns"]
        counts = dict.fromkeys(("gen0", "gen1", "gen2"), 0)
        by_generation = dict(counts)
        clips = []
        for interval in intervals:
            low = max(started, interval["start_ns"])
            high = min(ended, interval["end_ns"])
            if high > low:
                name = f"gen{interval['generation']}"
                counts[name] += 1
                by_generation[name] += high - low
                clips.append((low, high))
        # A union also remains conservative for injected overlapping intervals.
        pause = 0
        previous_end = started
        for low, high in sorted(clips):
            pause += max(0, high - max(low, previous_end))
            previous_end = max(previous_end, high)
        value = trial["solve_time_ns"] - pause if complete else None
        trial.update(solve_gc_pause_overlap_counts=counts,
                     solve_gc_pause_overlap_ns_by_generation=by_generation,
                     solve_gc_pause_overlap_ns=pause,
                     solve_gc_pause_complete=complete,
                     solve_wall_minus_gc_pause_ns=value)
        if value is not None:
            corrected.append(value)
    return dict(
        gc_pause_timeline=dict(
            clock="perf_counter_ns", max_events=timeline.max_events,
            event_count=len(timeline.events), dropped_events=timeline.dropped_events,
            measurement_error_count=timeline.measurement_error_count,
            unpaired_events=unpaired, complete=complete,
            events=[dict(generation=g, phase=p, monotonic_ns=t)
                    for g, p, t in timeline.events],
            intervals=intervals,
        ),
        solve_wall_minus_gc_pause_ns=dict(
            diagnostic_only=True, samples=len(corrected),
            p95=_percentile(corrected, .95) if corrected else None,
            p99=_percentile(corrected, .99) if corrected else None,
            maximum=max(corrected) if corrected else None,
        ),
    )


def _measure_gap_solve(anchor, world, request) -> tuple[SolveResult, dict]:
    """Add optional diagnostics without changing the solve or its wall interval.

    GC counters are process-wide completed collections, so overlap indicates a
    collection during this interval, not that this solve caused or paid for it.
    """
    errors = []

    def sample(stage, reader):
        try:
            return reader()
        except Exception as error:
            errors.append(dict(stage=stage, type=type(error).__name__,
                               message=str(error)))
            return None

    def gc_counts():
        counts = tuple(row["collections"] for row in gc.get_stats())
        if len(counts) != 3 or any(type(value) is not int or value < 0
                                   for value in counts):
            raise ValueError("GC statistics must contain three collection counts")
        return counts

    def delta(before, after):
        value = after - before
        if type(value) is not int or value < 0:
            raise ValueError("solve diagnostic counter moved backwards or is invalid")
        return value

    gc_before = sample("gc_before", gc_counts)
    cpu_before = sample("thread_cpu_before", time.thread_time_ns)
    started = time.perf_counter_ns()
    # Do not catch solver errors: diagnostic failures must never replace them.
    solved = solve_one_cell_gap(anchor, world, request)
    ended = time.perf_counter_ns()
    solve_ns = ended - started
    cpu_after = sample("thread_cpu_after", time.thread_time_ns)
    gc_after = sample("gc_after", gc_counts)
    cpu_ns = None
    if cpu_before is not None and cpu_after is not None:
        cpu_ns = sample("thread_cpu_delta", lambda: delta(cpu_before, cpu_after))
    gc_delta = dict.fromkeys(("gen0", "gen1", "gen2"))
    if gc_before is not None and gc_after is not None:
        for index, name in enumerate(gc_delta):
            gc_delta[name] = sample(
                f"{name}_delta", lambda: delta(gc_before[index], gc_after[index]),
            )
    return solved, dict(
        solve_time_ns=solve_ns,
        solve_started_ns=started,
        solve_ended_ns=ended,
        solve_thread_cpu_ns=cpu_ns,
        solve_gc_collections_delta=gc_delta,
        # False with null deltas/errors means unknown, not proven GC absence.
        solve_gc_overlap=any(value is not None and value > 0
                             for value in gc_delta.values()),
        solve_measurement_errors=errors,
    )


def _solve_measurement_summary(trials: list[dict]) -> dict:
    cpu = [trial["solve_thread_cpu_ns"] for trial in trials
           if trial["solve_thread_cpu_ns"] is not None]
    return dict(
        solve_thread_cpu_ns=dict(
            count=len(cpu), min=min(cpu) if cpu else None,
            max=max(cpu) if cpu else None,
            p95=_percentile(cpu, .95) if cpu else None,
            p99=_percentile(cpu, .99) if cpu else None,
        ),
        solve_gc_overlap_count=sum(trial["solve_gc_overlap"] for trial in trials),
        solve_measurement_error_count=sum(
            bool(trial["solve_measurement_errors"]) for trial in trials
        ),
    )


def _partial_b10_summary(evidence_buffer, gc_timeline, *, targeted_probe=None) -> dict:
    """Report retained evidence on failure, without manufacturing later gates."""
    trials = list(evidence_buffer.rows("b10-gap-solver-trials.jsonl"))
    coordinator = list(evidence_buffer.rows("b10-coordinator-trials.jsonl"))
    times = [trial["solve_time_ns"] for trial in trials]
    return dict(
        schema_version="mc2p.b10-partial-summary.v1", partial=True, matrix_complete=False,
        mode="targeted_probe" if targeted_probe is not None else "formal_matrix",
        formal_acceptance_eligible=targeted_probe is None,
        targeted_probe=targeted_probe,
        synchronous_probe_timing=dict(scope="synchronous_probe_only", disposition="report_only",
            planned_count=0 if targeted_probe is not None else 142,
            completed_count=len(trials), warning_threshold_ns=30_000_000,
            warning_count=sum(value >= 30_000_000 for value in times)),
        coordinator_matrix=dict(planned_count=len(_selected_b10_coordinator_scenarios(targeted_probe)),
                                recorded_count=len(coordinator),
                                completed_count=sum(row.get("terminal_state") == "complete" for row in coordinator)),
        solve_time_ns=dict(samples=len(times), p95=_percentile(times, .95) if times else None,
            p99=_percentile(times, .99) if times else None, maximum=max(times) if times else None),
        **_solve_measurement_summary(trials), **_attribute_solve_gc_pauses(trials, gc_timeline),
        trials=trials, coordinator_trials=coordinator,
    )


def _receiver_diagnostic_summary(rows: list[dict]) -> dict:
    cycles = [row for row in rows if row.get("event") == "receiver_cycle"]
    durations = [row["duration_ns"] for row in cycles
                 if type(row.get("duration_ns")) is int]
    submitted = {row.get("identity"): row for row in rows
                 if row.get("event") == "worker_submit"
                 and type(row.get("identity")) is str}
    observed = {row.get("identity"): row for row in rows
                if row.get("event") == "receiver_observed_result"
                and type(row.get("identity")) is str}
    published = {row.get("identity"): row for row in rows
                 if row.get("event") == "registry_publish"
                 and type(row.get("identity")) is str}
    consumed = {row.get("identity"): row for row in rows
                if row.get("event") == "first_control_consume"
                and type(row.get("identity")) is str}
    visible = {row.get("identity"): row for row in rows
               if row.get("event") == "first_control_visible" and type(row.get("identity")) is str}
    taken = {row.get("identity"): row for row in rows
             if row.get("event") == "result_take" and type(row.get("identity")) is str}
    delivery = []
    invisible_next_decision = 0
    unavailable = 0
    for identity in dict.fromkeys((*submitted, *observed, *published)):
        publish = published.get(identity, {})
        submit = submitted.get(identity)
        observed_row = observed.get(identity)
        consume = consumed.get(identity)
        observed_tick = (
            None if observed_row is None else observed_row.get("movement_tick")
        )
        publish_tick = publish.get("movement_tick")
        consume_tick = None if consume is None else consume.get("movement_tick")
        visible_tick = visible.get(identity, {}).get("movement_tick")
        take_tick = taken.get(identity, {}).get("movement_tick")
        submit_tick = None if submit is None else submit.get("worker_submit_tick", submit.get("movement_tick"))
        if not all(type(value) is int for value in
                   (observed_tick, publish_tick, consume_tick)):
            unavailable += 1
        elif consume_tick > publish_tick + 1:
            invisible_next_decision += 1
        delivery.append({
            "identity": identity,
            "worker_submit_tick": submit_tick,
            "job_anchor_movement_tick": None if submit is None else submit.get("job_anchor_movement_tick"),
            "receiver_observed_result_tick": observed_tick,
            "registry_publish_tick": publish_tick,
            "first_control_consume_tick": consume_tick,
            "first_control_visible_tick": visible_tick,
            "result_take_tick": take_tick,
            "actual_delivery_ticks": (visible_tick - submit_tick
                if type(visible_tick) is int and type(submit_tick) is int else None),
            "receiver_observed_to_registry_publish_ticks": (
                None if type(observed_tick) is not int or type(publish_tick) is not int
                else publish_tick - observed_tick
            ),
            "registry_publish_to_first_control_consume_ticks": (
                None if type(publish_tick) is not int or type(consume_tick) is not int
                else consume_tick - publish_tick
            ),
            "worker_submit_to_observed_ticks": (
                None if submit is None or type(submit.get("movement_tick")) is not int
                or type(observed_tick) is not int
                else observed_tick - submit["movement_tick"]
            ),
            "worker_elapsed_ns": (
                None if observed_row is None
                else observed_row.get("worker_elapsed_ns")
            ),
            "worker_queue_after_completion_ns": (
                None if observed_row is None
                or type(observed_row.get("monotonic_ns")) is not int
                or type(observed_row.get("worker_completed_monotonic_ns")) is not int
                or observed_row["worker_completed_monotonic_ns"] <= 0
                else max(0, observed_row["monotonic_ns"]
                         - observed_row["worker_completed_monotonic_ns"])
            ),
        })
    completion_available = sum(
        type(item["worker_queue_after_completion_ns"]) is int
        for item in delivery
    )
    return {
        "schema_version": "mc2p.b10-receiver-diagnostic-summary.v4",
        "worker_completion_to_receiver_observed": {
            "status": "available" if completion_available else "unavailable",
            "available_count": completion_available,
            "unavailable_count": len(delivery) - completion_available,
        },
        "cycle_count": len(cycles),
        "cycle_duration_ns": {
            "samples": len(durations),
            "p95": None if not durations else _percentile(durations, .95),
            "p99": None if not durations else _percentile(durations, .99),
            "maximum": None if not durations else max(durations),
        },
        "delivery_count": len(delivery),
        "delivery_tick_unavailable_count": unavailable,
        "published_but_next_decision_invisible_count": invisible_next_decision,
        "deliveries": delivery,
    }


def _application_matches(application, movement: MovementV1) -> bool:
    return (
        application.state in {"leased", "neutral"}
        and math.isclose(application.forward, float(movement.forward), abs_tol=1e-9)
        and math.isclose(application.strafe, float(movement.strafe), abs_tol=1e-9)
        and application.jump is movement.jump
        and application.sneak is movement.sneak
        and application.sprint is movement.sprint
    )


def _runtime_navigation_frame(runtime):
    """Read the immutable navigation projection already owned by Runtime."""
    frame = runtime.navigation_observation_adapter.latest_frame
    observation = runtime.observation
    if frame is None or observation is None:
        raise RuntimeError("B10 requires a Runtime-owned navigation frame")
    if frame.body.sequence_id != observation.sequence_id:
        raise RuntimeError("B10 Runtime navigation frame is stale")
    return frame


def _pending_air_request(frame, request: _AirRequestSource | None):
    """Avoid retransmitting air already retained by the Runtime world model.

    A cell remembered as a block is queried again because the fixture may have
    removed it.  UNKNOWN cells still need a positive air answer.  Known AIR is
    already sufficient for the current route and does not need to be serialized
    in every 20 Hz control frame.
    """
    if request is None or not request.air_positions:
        return request
    pending = tuple(
        position for position in request.air_positions
        if frame.world.cell(position).knowledge is not CellKnowledge.AIR
    )[:MAX_AIR_QUERY_POSITIONS]
    if pending == request.air_positions:
        return request
    return ObservationRequestV3(
        request.field_profile,
        pending,
        request.entity_track_id,
    )


def _targeted_information_request(frame, positions):
    """Request only the unknown cells that currently block one calculation."""
    pending = tuple(sorted({
        position for position in positions
        if frame.world.cell(position).knowledge is CellKnowledge.UNKNOWN
    }))[:MAX_AIR_QUERY_POSITIONS]
    return ObservationRequestV3("navigation_v1", pending)


def _observation_vantage(
        position: tuple[int, int, int],
        supports: tuple[tuple[int, int, int], ...],
) -> tuple[int, int, int]:
    """Choose a grounded fixture position far enough to see one whole cell."""
    below_support = bool(supports) and position[1] < max(
        support[1] for support in supports
    )
    minimum_distance = .9 if below_support else 1.5
    candidates = tuple(
        support for support in supports
        if minimum_distance <= math.hypot(
            support[0] + .5 - (position[0] + .5),
            support[2] + .5 - (position[2] + .5),
        ) <= 14.0
    )
    if not candidates:
        raise RuntimeError(f"B10 has no safe observation vantage for {position}")
    ranked = lambda support: (
        math.hypot(
            support[0] + .5 - (position[0] + .5),
            support[2] + .5 - (position[2] + .5),
        ),
        support,
    )
    return (min if below_support else max)(
        candidates,
        key=ranked,
    )


def _runtime_input_ledger(runtime) -> InputApplicationLedger:
    """Use the ledger owned by Runtime's sole input output path."""
    ledger = runtime.input_ledger
    if type(ledger) is not InputApplicationLedger:
        raise RuntimeError("B10 requires the Runtime-owned input ledger")
    return ledger


def _verified_submission_window(decision) -> tuple[int, int, int] | None:
    """Return a complete proof window, while allowing neutral safety output."""
    values = (
        decision.verified_command_index,
        decision.expected_movement_tick,
        decision.latest_movement_tick,
    )
    if values == (None, None, None):
        return None
    if any(value is None for value in values):
        raise RuntimeError("B10 session returned incomplete verified command identity")
    return values


def _manual_executor_submission_window(
    decision,
) -> tuple[int, int, int] | None:
    """Separate proof commands from neutral coasting in the direct probe.

    The solver proves the complete trajectory, but only its active input prefix
    needs a command identity and an application window.  Once that prefix has
    been observed, neutral free fall is carried by ordinary neutral input until
    a later observation confirms the proved landing state.
    """
    if not decision.submittable_as_verified_command:
        if (decision.reason == "coast_to_verified_landing"
                and decision.movement == MovementV1()):
            return None
        raise RuntimeError(
            "B10 manual executor returned unverified active movement"
        )
    values = (
        decision.command_index,
        decision.expected_movement_tick,
        decision.latest_movement_tick,
    )
    if any(value is None for value in values):
        raise RuntimeError(
            "B10 manual executor returned an incomplete submission window"
        )
    return values


def _input_window_diagnostics(
    applications,
    verified_window: tuple[int, int, int] | None,
) -> dict:
    ticks = [item.movement_tick_id for item in applications]
    states = [item.state for item in applications]
    command_index = expected_tick = latest_tick = None
    if verified_window is not None:
        command_index, expected_tick, latest_tick = verified_window
    actual_tick = ticks[0] if ticks else None
    return {
        "verified_command_index": command_index,
        "expected_movement_tick": expected_tick,
        "latest_movement_tick": latest_tick,
        "actual_movement_ticks": ticks,
        "application_states": states,
        "actual_minus_expected_ticks": (
            None if actual_tick is None or expected_tick is None
            else actual_tick - expected_tick
        ),
        "actual_minus_latest_ticks": (
            None if actual_tick is None or latest_tick is None
            else actual_tick - latest_tick
        ),
        "remaining_to_latest_ticks": (
            None if actual_tick is None or latest_tick is None
            else latest_tick - actual_tick
        ),
        "remaining_to_latest_milliseconds": (
            None if actual_tick is None or latest_tick is None
            else (latest_tick - actual_tick) * 50.0
        ),
    }


@dataclass(frozen=True, slots=True)
class _B10CapturedFormalFrame:
    navigation_frame: NavigationFrame
    latest_application: object
    sample: dict | None
    evidence: dict


def _capture_b10_formal_driver_frame(
    *,
    driver_frame: _B10FormalDriverFrame,
    runtime,
    session: NavigationSession,
    trial_name: str,
    episode: str,
    scenario_kind: str,
    evidence_buffer: _B10EvidenceBuffer,
) -> _B10CapturedFormalFrame:
    """Capture the v3 row used by both Fabric and component evidence."""
    proposal = driver_frame.proposal
    decision = proposal.route_decision
    result = driver_frame.result
    if (result.observation is None or result.backend_result is None
            or result.decision is None):
        raise RuntimeError(f"B10 formal driver step failed: {result.report}")
    receipt = result.backend_result.receipt
    if type(receipt) is not ClientBehaviorReceiptV3:
        raise RuntimeError("B10 formal driver requires V3 input evidence")
    latest_application = receipt.last_input_sample
    if latest_application is None:
        raise RuntimeError("B10 formal driver lost its movement tick")
    navigation_frame = _runtime_navigation_frame(runtime)
    verified_window = None if decision is None else _verified_submission_window(decision)
    action = result.decision.action
    owned_applications = tuple(
        application
        for application in receipt.input_applications
        if application.request_sequence_id == action.request_sequence_id
    )
    admission = (
        None if session._coordinator is None
        else session._coordinator.last_admission
    )
    motion_identity = (
        repr(admission.identity.key)
        if admission is not None
        and admission.disposition is AsyncAdmissionDisposition.APPLIED
        else None
    )
    reason_code = proposal.report.reason if decision is None else decision.reason_code
    submit_input = bool(decision is not None and decision.submit_input)
    evidence = {
        "schema_version": "mc2p.b10-coordinator-control-frame.v3",
        "trial": trial_name,
        "episode_id": episode,
        "scenario_kind": scenario_kind,
        "control_sequence_id": action.request_sequence_id,
        "decision_observation_sequence_id": (
            driver_frame.decision_observation_sequence_id
        ),
        "result_observation_sequence_id": navigation_frame.body.sequence_id,
        "observation_sequence_id": navigation_frame.body.sequence_id,
        "session_state": proposal.report.state.value,
        "decision_state": None if decision is None else decision.state.value,
        "reason_code": reason_code,
        "submit_input": submit_input,
        "grant_movement_tick": driver_frame.movement_tick_before,
        "motion_identity": motion_identity,
        "position": list(navigation_frame.body.position),
        "on_ground": navigation_frame.body.is_on_ground,
        "horizontal_collision": navigation_frame.body.horizontal_collision,
        "observation_age_ns": driver_frame.observation_age_ns,
        "proposal_elapsed_ns": driver_frame.proposal_elapsed_ns,
        "control_wall_ns": driver_frame.control_wall_ns,
        "backend_elapsed_ns": driver_frame.backend_elapsed_ns,
        "backend_blocking_io_ns": driver_frame.backend_blocking_io_ns,
        "runtime_outside_backend_ns": max(
            0,
            driver_frame.control_wall_ns - driver_frame.backend_elapsed_ns,
        ),
        "driver_proposed_intents": (
            () if driver_frame.diagnostics is None
            else driver_frame.diagnostics.proposed_intents
        ),
        "driver_selected_intents": (
            () if driver_frame.diagnostics is None
            else driver_frame.diagnostics.selected_intents
        ),
        "driver_candidate_intents": (
            () if driver_frame.diagnostics is None
            else driver_frame.diagnostics.candidate_intents
        ),
        "driver_action_deadline_ns": (
            None if driver_frame.diagnostics is None
            else driver_frame.diagnostics.action_deadline_ns
        ),
    }
    sample = None
    if submit_input:
        if not owned_applications:
            raise RuntimeError("B10 formal driver input had no application sample")
        sample = {
            "command_index": decision.verified_command_index,
            "decision_state": decision.state.value,
            "reason_code": decision.reason_code,
            "submit_input": True,
            "grant_movement_tick": driver_frame.movement_tick_before,
            "motion_identity": motion_identity,
            "control_sequence_id": action.request_sequence_id,
            "movement": asdict(decision.movement),
            "position": list(navigation_frame.body.position),
            "on_ground": navigation_frame.body.is_on_ground,
            "horizontal_collision": navigation_frame.body.horizontal_collision,
            "observation_age_ns": driver_frame.observation_age_ns,
            "proposal_elapsed_ns": driver_frame.proposal_elapsed_ns,
            "route_control_time_ns": decision.control_time_ns,
            "control_wall_ns": driver_frame.control_wall_ns,
            "backend_elapsed_ns": driver_frame.backend_elapsed_ns,
            "backend_blocking_io_ns": driver_frame.backend_blocking_io_ns,
            "runtime_outside_backend_ns": max(
                0,
                driver_frame.control_wall_ns - driver_frame.backend_elapsed_ns,
            ),
            **_input_window_diagnostics(owned_applications, verified_window),
        }
        evidence.update(sample)
    if not evidence_buffer.append(
        "b10-coordinator-control-frames.jsonl",
        evidence,
    ):
        raise _B10EvidenceOverflow("B10 formal driver frame evidence overflow")
    return _B10CapturedFormalFrame(
        navigation_frame,
        latest_application,
        sample,
        evidence,
    )


def run_b10_gap_solver_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[tuple[int, int, int], ...], str], None],
    player_teleporter: Callable[[float, float, float, float, float], None],
    *, pipeline_diagnostic: Callable[[], dict] | None = None,
    lifecycle_report: dict | None = None,
    targeted_probe: str | None = None,
) -> tuple[dict, list[dict], list[dict]]:
    """Keep workers alive and preserve the first execution failure.

    Cleanup is reported separately because raising from a ``finally`` block
    would replace the Runtime/driver failure that caused cleanup to start.
    """
    _selected_b10_coordinator_scenarios(targeted_probe)  # Validate before acquiring resources.
    report = {} if lifecycle_report is None else lifecycle_report
    report.clear()
    report.update(
        schema_version="mc2p.b10-probe-lifecycle.v1",
        ordered_source_release={
            "status": "not_released",
            "source_id": None,
        },
        cleanup_failures=[],
    )
    evidence_buffer = _B10EvidenceBuffer(directory)
    gc_timeline = _GCPauseTimeline()
    planner_owner = motion_owner = None
    manual_source = None
    result = None
    completed = False
    primary: BaseException | None = None
    primary_traceback = None

    def cleanup_failure(stage: str, error: BaseException) -> None:
        report["cleanup_failures"].append({
            "stage": stage,
            "type": type(error).__name__,
            "message": str(error),
        })

    try:
        gc_timeline.__enter__()
        planner_owner = PlannerWorker()
        planner_worker = planner_owner.__enter__()
        motion_owner = MotionSolverWorker(max_pending=4)
        motion_worker = motion_owner.__enter__()
        manual_source = runtime.register_ordered_source(_SOURCE)
        report["ordered_source_release"]["source_id"] = getattr(
            manual_source, "source_id", None,
        )
        result = _run_b10_gap_solver_runtime(
            runtime,
            backend,
            episode,
            directory,
            deadline_ns,
            fixture_writer,
            player_teleporter,
            planner_worker=planner_worker,
            motion_worker=motion_worker,
            manual_source=manual_source,
            evidence_buffer=evidence_buffer,
            gc_timeline=gc_timeline,
            pipeline_diagnostic=pipeline_diagnostic,
            targeted_probe=targeted_probe,
        )
        completed = True
    except BaseException as error:
        primary = error
        primary_traceback = error.__traceback__
    finally:
        gc_timeline.close()
        if not completed:
            try:
                partial = _partial_b10_summary(evidence_buffer, gc_timeline, targeted_probe=targeted_probe)
                partial["primary_error_type"] = None if primary is None else type(primary).__name__
                (directory / "b10-gap-solver-partial-summary.json").write_text(
                    strict_json_dumps(partial) + "\n", encoding="utf-8")
                report["partial_summary"] = "b10-gap-solver-partial-summary.json"
            except BaseException as error:
                cleanup_failure("partial_summary", error)
        if manual_source is not None:
            if getattr(getattr(runtime, "state", None), "value", None) == "ready":
                try:
                    runtime.unregister_ordered_source(manual_source)
                except BaseException as error:
                    cleanup_failure("ordered_source_unregister", error)
                    report["ordered_source_release"]["status"] = (
                        "pending_runtime_close"
                    )
                else:
                    report["ordered_source_release"]["status"] = (
                        "explicitly_released"
                    )
            else:
                report["ordered_source_release"]["status"] = (
                    "pending_runtime_close"
                )
        for stage, owner in (
            ("motion_worker_close", motion_owner),
            ("planner_worker_close", planner_owner),
        ):
            if owner is None:
                continue
            try:
                owner.__exit__(
                    None if primary is None else type(primary),
                    primary,
                    primary_traceback,
                )
            except BaseException as error:
                cleanup_failure(stage, error)
        try:
            evidence_buffer.flush()
        except BaseException as error:
            cleanup_failure("evidence_flush", error)

    if primary is not None:
        raise primary.with_traceback(primary_traceback)
    if not completed:
        raise RuntimeError("B10 probe ended without a result or primary failure")
    return result


def finalize_b10_source_release(report: dict, *, runtime_state: str | None) -> dict:
    """Resolve deferred release from the close boundary's immutable facts."""
    release = report.get("ordered_source_release")
    if type(release) is not dict:
        raise ValueError("B10 lifecycle omitted ordered source release")
    if release.get("status") == "explicitly_released":
        return report
    release["status"] = (
        "cleared_on_runtime_close"
        if release.get("source_id") is not None and runtime_state == "closed"
        else "not_released"
    )
    return report


def _run_b10_gap_solver_runtime(
    runtime,
    backend,
    episode: str,
    directory: Path,
    deadline_ns: int,
    fixture_writer: Callable[[tuple[tuple[int, int, int], ...], str], None],
    player_teleporter: Callable[[float, float, float, float, float], None],
    *,
    planner_worker: PlannerWorker,
    motion_worker: MotionSolverWorker,
    manual_source,
    evidence_buffer: _B10EvidenceBuffer,
    gc_timeline: _GCPauseTimeline,
    pipeline_diagnostic: Callable[[], dict] | None = None,
    targeted_probe: str | None = None,
) -> tuple[dict, list[dict], list[dict]]:
    """Solve and execute frozen trials, including the default coordinator."""
    task = TaskIntentV0(
        "b10-gap-solver", "solve_same_height_one_cell_gap", "{}",
        (SuccessCriterionV0("validation_trials", ComparisonOperatorV0.EQUAL, 150, "count"),),
        5000, deadline_ns, True, 0.0,
    )
    behavior = BehaviorProfileV0()
    frame = _runtime_navigation_frame(runtime)
    rows: list[str] = []
    counter = 0
    latest_application = None
    def diagnostic() -> None:
        row = dict(
            episode_id=episode,
            observation_sequence_id=runtime.observation.sequence_id,
            diagnostics=backend.last_diagnostics,
        )
        if pipeline_diagnostic is not None:
            row["observation_pipeline"] = pipeline_diagnostic()
        rows.append(strict_json_dumps(row))
        evidence_buffer.append("diagnostics.jsonl", row)

    def step(
            movement=MovementV1(), *, look=None, request=None,
            input_execution_window: CandidateExecutionWindow | None = None):
        nonlocal counter, frame, latest_application
        runtime.cancel_source(manual_source.source_id)
        counter += 1
        now = time.perf_counter_ns()
        intent = ActionIntentV1(
            ordered_intent_id(manual_source, counter),
            manual_source.source_id,
            episode,
            runtime.observation.sequence_id,
            ActionPriorityV0.TASK,
            now,
            min(deadline_ns, now + 750_000_000),
            movement=movement,
            look=look,
            valid_for_ticks=1,
        )
        runtime.submit_ordered_intent(
            OrderedIntentV1(manual_source, counter, intent)
        )
        active_request = _pending_air_request(frame, request)
        result = runtime.step(
            task, behavior, min(deadline_ns, now + 5_000_000_000),
            observation_request=active_request,
            input_execution_window=input_execution_window,
        )
        if result.observation is None or result.backend_result is None:
            raise RuntimeError(f"B10 Fabric step failed: {result.report}")
        if result.decision is None or result.decision.action.movement != movement:
            raise RuntimeError("B10 command bypassed the formal arbiter")
        receipt = result.backend_result.receipt
        if type(receipt) is not ClientBehaviorReceiptV3:
            raise RuntimeError("B10 controlled execution requires V3 input evidence")
        owned = tuple(
            item for item in receipt.input_applications
            if item.request_sequence_id == result.decision.action.request_sequence_id
        )
        if not owned or not any(_application_matches(item, movement) for item in owned):
            raise RuntimeError("B10 command was not observed at the player movement input")
        latest_application = receipt.last_input_sample
        frame = _runtime_navigation_frame(runtime)
        diagnostic()
        return result

    diagnostic()
    feet_y = _bounded_fixture_feet_y(frame.body.position[1])
    origin_x = math.floor(frame.body.position[0])
    origin_z = math.floor(frame.body.position[2])
    start_support = (origin_x, feet_y - 1, origin_z)
    targets = tuple(
        (origin_x + dx * 2, feet_y - 1, origin_z + dz * 2)
        for dx, dz, _ in _DIRECTIONS
    )
    turn_directions = tuple((dz, -dx) for dx, dz, _ in _DIRECTIONS)
    turn_supports = tuple(
        (target[0] + turn[0], target[1], target[2] + turn[1])
        for target, turn in zip(targets, turn_directions)
    )
    volume = tuple(
        (x, y, z)
        for x in range(origin_x - 3, origin_x + 4)
        for y in range(feet_y - 2, feet_y + 4)
        for z in range(origin_z - 3, origin_z + 4)
    )
    supports = (start_support, *targets, *turn_supports)
    support_set = set(supports)
    fixture_writer(
        tuple(position for position in volume if position not in support_set),
        "minecraft:air",
    )
    fixture_writer(supports, "minecraft:grass_block")
    request = _AirRequestPlan(
        "navigation_v1",
        tuple(position for position in volume if position not in support_set),
    )

    def teleport(
            yaw: float, *, position: tuple[float, float, float] | None = None,
            observation_request: _AirRequestSource | None = None,
            position_tolerance: float = .03) -> None:
        nonlocal frame
        if (not math.isfinite(position_tolerance)
                or not .0 < position_tolerance <= .45):
            raise RuntimeError("B10 teleport tolerance is invalid")
        target_position = position or (
            origin_x + .5, float(feet_y), origin_z + .5,
        )
        active_request = observation_request or request
        starting_body = frame.body
        starting_movement_tick = starting_body.movement_tick_id
        player_teleporter(*target_position, yaw, 0.0)

        def advance_teleport_frame():
            step(request=active_request)
            return frame.body

        _wait_for_b10_teleport_settlement(
            advance_teleport_frame,
            starting_movement_tick=starting_movement_tick,
            starting_position=starting_body.position,
            starting_yaw_degrees=math.degrees(starting_body.yaw_radians),
            starting_on_ground=starting_body.is_on_ground,
            target_position=target_position,
            yaw_degrees=yaw,
            position_tolerance=position_tolerance,
        )

    def look_at_cell(
            position: tuple[int, int, int], *,
            observation_request: _AirRequestSource | None = None) -> None:
        active_request = observation_request or request
        for _ in range(8):
            dx = position[0] + .5 - frame.body.position[0]
            dy = position[1] + .5 - (frame.body.position[1] + 1.62)
            dz = position[2] + .5 - frame.body.position[2]
            target_yaw = math.degrees(math.atan2(-dx, dz))
            target_pitch = -math.degrees(
                math.atan2(dy, max(1.0e-6, math.hypot(dx, dz)))
            )
            step(
                look=LookV1(
                    (target_yaw - math.degrees(frame.body.yaw_radians) + 180) % 360 - 180,
                    target_pitch - math.degrees(frame.body.pitch_radians),
                ),
                request=active_request,
            )
            if frame.world.cell(position).knowledge is CellKnowledge.BLOCK:
                return
        raise RuntimeError(f"B10 support was not formally observed: {position}")

    def observe_missing_cells(
            positions: tuple[tuple[int, int, int], ...]) -> None:
        """Acquire only the facts that blocked the previous solver attempt.

        The test fixture may move the player between safe known supports while
        preparing a trial.  This is observation setup, not part of the actor's
        executed motion.  A cell remains unknown unless profile 4 can actually
        expose its full visual-air volume or its block surface.
        """
        nonlocal frame
        for position in positions:
            if frame.world.cell(position).knowledge is not CellKnowledge.UNKNOWN:
                continue
            if position[1] <= feet_y - 1:
                vantages = _inspection_supports(
                    position,
                    excluded_columns=frozenset(
                        (support[0], support[2]) for support in supports
                    ),
                )
            else:
                vantages = (_observation_vantage(position, supports),)
            tried = []
            for vantage in vantages:
                if frame.world.cell(position).knowledge is not CellKnowledge.UNKNOWN:
                    break
                tried.append(vantage)
                request_for_cell = _targeted_information_request(frame, (position,))
                if position[1] <= feet_y - 1:
                    fixture_writer((
                        (vantage[0], position[1], vantage[2]),
                        (vantage[0], position[1] + 1, vantage[2]),
                    ), "minecraft:air")
                    fixture_writer((vantage,), "minecraft:grass_block")
                    # The RCON commands and the player teleport are separate
                    # server events.  Let one observed tick confirm the new
                    # platform before putting the body on it; otherwise the
                    # old block can push the player away during teleport.
                    step(request=request_for_cell)
                target_position = (
                    vantage[0] + .5,
                    float(vantage[1] + 1),
                    vantage[2] + .5,
                )
                dx = position[0] + .5 - target_position[0]
                dz = position[2] + .5 - target_position[2]
                yaw = math.degrees(math.atan2(-dx, dz))
                teleport(
                    yaw,
                    position=target_position,
                    observation_request=request_for_cell,
                    # A one-block support can leave the vanilla server's safe
                    # teleport correction a few centimetres off centre.  This
                    # remains a stable observation stance; it is not an actor
                    # motion or a landing acceptance tolerance.
                    position_tolerance=.45,
                )
                for _ in range(12):
                    if (frame.world.cell(position).knowledge
                            is not CellKnowledge.UNKNOWN):
                        break
                    dx = position[0] + .5 - frame.body.position[0]
                    dy = position[1] + .5 - (frame.body.position[1] + 1.62)
                    dz = position[2] + .5 - frame.body.position[2]
                    target_yaw = math.degrees(math.atan2(-dx, dz))
                    target_pitch = -math.degrees(
                        math.atan2(dy, max(1.0e-6, math.hypot(dx, dz)))
                    )
                    step(
                        look=LookV1(
                            (target_yaw
                             - math.degrees(frame.body.yaw_radians) + 180)
                            % 360 - 180,
                            target_pitch - math.degrees(frame.body.pitch_radians),
                        ),
                        request=request_for_cell,
                    )
            if frame.world.cell(position).knowledge is CellKnowledge.UNKNOWN:
                raise RuntimeError(
                    "B10 profile-4 observation could not resolve required cell: "
                    f"{position} from supports {tuple(tried)}"
                )

    teleport(0.0)
    for position in supports:
        look_at_cell(position)

    trials: list[dict] = []
    solve_times: list[int] = []
    physics_snapshot_times: list[int] = []

    def run_trial(
            direction_index: int, repetition: int, group: str, *,
            moving_exit: bool = False, prepared_entry: bool = False,
            observation_request: _AirRequestSource | None = None,
            speed_band: tuple[float, float] | None = None,
            reprepare_entry: Callable[[], None] | None = None) -> None:
        nonlocal frame
        dx, dz, yaw = _DIRECTIONS[direction_index]
        active_request = observation_request or request
        if not prepared_entry:
            teleport(yaw, observation_request=active_request)
        target = targets[direction_index]
        information_rounds = 0
        information_cells_seen: set[tuple[int, int, int]] = set()
        information_attempts: list[tuple[tuple[int, int, int], ...]] = []
        while True:
            if latest_application is None:
                raise RuntimeError("B10 has no movement-tick evidence for its anchor")
            built = build_physics_state(
                frame, JAVA_1_21_RULESET, _STATE_ASSUMPTIONS,
            )
            if built.status is not StateBuildStatus.READY or built.state is None:
                raise RuntimeError(f"B10 entry state is incomplete: {built}")
            movement_tick = latest_application.movement_tick_id
            entry_state = replace(built.state, movement_tick_id=movement_tick)
            entry_speed = math.hypot(
                frame.body.velocity_blocks_per_second[0],
                frame.body.velocity_blocks_per_second[2],
            )
            anchor = StateAnchor(
                frame.session, frame.body.sequence_id, movement_tick,
                MotionTickPhase.AFTER_MOVEMENT,
                latest_application.request_sequence_id,
                (movement_tick, movement_tick),
                JAVA_1_21_RULESET.ruleset_id,
                JAVA_1_21_RULESET.state_schema,
                "mc2p.input-projection.v1", entry_state,
            )
            solve_request = GapSolveRequest(
                (dx, dz),
                LandingRegion(
                    target[0] + .3, target[0] + .7,
                    target[2] + .3, target[2] + .7,
                    float(target[1] + 1),
                ),
                CandidateExecutionWindow(movement_tick + 1, movement_tick + 2),
                max_candidates=12, max_ticks=20,
                exit_direction=(turn_directions[direction_index]
                                if moving_exit else None),
                exit_motion_ticks=1 if moving_exit else 0,
            )
            live_physics_world = PhysicsWorldView(
                frame.world, JAVA_1_21_RULESET,
            )
            snapshot_started = time.perf_counter_ns()
            solve_world = _gap_physics_snapshot(
                live_physics_world, anchor, solve_request,
            )
            snapshot_ns = time.perf_counter_ns() - snapshot_started
            solved, solve_measurement = _measure_gap_solve(
                anchor,
                solve_world,
                solve_request,
            )
            solve_ns = solve_measurement["solve_time_ns"]
            if (solved.status is not SolveStatus.NEEDS_WORLD
                    or not solved.missing_cells):
                break
            if not _information_retry_allowed(
                    solved.missing_cells,
                    information_cells_seen,
                    completed_rounds=information_rounds,
                    max_rounds=solve_request.max_ticks):
                break
            information_attempts.append(solved.missing_cells)
            information_cells_seen.update(solved.missing_cells)
            observe_missing_cells(solved.missing_cells)
            information_rounds += 1
            if reprepare_entry is not None:
                reprepare_entry()
            else:
                teleport(yaw, observation_request=active_request)
        solve_times.append(solve_ns)
        physics_snapshot_times.append(snapshot_ns)
        if solved.status is not SolveStatus.SOLVED or solved.proof is None:
            raise RuntimeError(
                "B10 solver returned "
                f"{solved.status.value}: {solved.reasons}; "
                f"missing_cells={solved.missing_cells}; "
                f"information_attempts={information_attempts}"
            )
        if frame.body.sequence_id != anchor.observation_sequence_id:
            raise RuntimeError("B10 world or body changed before candidate execution")
        damage_budget = TaskDamageBudget()
        reusable = VerifiedMotionCandidate(
            solved.proof,
            MotionCandidateContext(
                f"{episode}-{group}-{direction_index}-{repetition}",
                repetition, "b10-gap-goal", direction_index,
                f"b10-gap-route-{group}-{direction_index}", 1, 0, 1,
                damage_budget,
                ("server_hunger_clock_not_in_physics_state",),
            ),
        )
        admission = MotionCandidateAdmitter().admit(
            reusable, anchor,
            planning_request_id=reusable.context.planning_request_id,
            planning_generation=reusable.context.planning_generation,
            goal_id=reusable.context.goal_id,
            goal_revision=reusable.context.goal_revision,
            route_id=reusable.context.route_id,
            route_revision=reusable.context.route_revision,
            action_index=0, candidate_revision=1,
            damage_budget=damage_budget,
            intended_start_tick=movement_tick + 1,
            changed_cells=(),
        )
        if (admission.status is not MotionCandidateStatus.ACCEPTED
                or admission.candidate is None):
            raise RuntimeError(f"B10 candidate rejected: {admission.reason}")
        executor = VerifiedMotionExecutor()
        executor.start(admission.candidate)
        ledger = _runtime_input_ledger(runtime)
        current_anchor = anchor
        samples = []
        for _ in range(len(solved.proof.commands) + 2):
            decision = executor.decide(current_anchor, ledger)
            if decision.state is VerifiedMotionExecutorState.COMPLETE:
                break
            if (decision.state is not VerifiedMotionExecutorState.RUNNING
                    or decision.movement is None):
                raise RuntimeError(f"B10 verified executor stopped: {decision}")
            submission_window = _manual_executor_submission_window(decision)
            look = None
            if decision.movement_yaw_radians is not None:
                yaw_delta = math.degrees(math.atan2(
                    math.sin(decision.movement_yaw_radians
                             - frame.body.yaw_radians),
                    math.cos(decision.movement_yaw_radians
                             - frame.body.yaw_radians),
                ))
                if abs(yaw_delta) > 1.0e-6:
                    look = LookV1(yaw_delta, 0.0)
            execution_window = (
                None if submission_window is None
                else CandidateExecutionWindow(
                    submission_window[1], submission_window[2],
                )
            )
            result = step(
                decision.movement, look=look, request=active_request,
                input_execution_window=execution_window,
            )
            action = result.decision.action
            receipt = result.backend_result.receipt
            owned_applications = tuple(
                application
                for application in receipt.input_applications
                if application.request_sequence_id == action.request_sequence_id
            )
            if not owned_applications:
                raise RuntimeError(
                    "B10 verified input had no formal application sample"
                )
            if submission_window is not None:
                _, expected_tick, latest_tick = submission_window
                executor.register_submission(
                    decision.command_index,
                    control_sequence=action.request_sequence_id,
                    requested_movement_tick=expected_tick,
                    requested_latest_movement_tick=latest_tick,
                )
            if latest_application is None:
                raise RuntimeError("B10 execution lost the movement-tick receipt")
            built_after = build_physics_state(
                frame, JAVA_1_21_RULESET, _STATE_ASSUMPTIONS,
            )
            if (built_after.status is not StateBuildStatus.READY
                    or built_after.state is None):
                raise RuntimeError("B10 could not anchor the observed exit state")
            observed_tick = latest_application.movement_tick_id
            current_anchor = StateAnchor(
                frame.session, frame.body.sequence_id, observed_tick,
                MotionTickPhase.AFTER_MOVEMENT,
                latest_application.request_sequence_id,
                (observed_tick, observed_tick),
                JAVA_1_21_RULESET.ruleset_id,
                JAVA_1_21_RULESET.state_schema,
                "mc2p.input-projection.v1",
                replace(built_after.state, movement_tick_id=observed_tick),
            )
            samples.append(dict(
                command_index=decision.command_index,
                command=asdict(decision.movement),
                required_movement_yaw_radians=(
                    decision.movement_yaw_radians
                ),
                look=asdict(look) if look is not None else None,
                position=list(frame.body.position),
                velocity=list(frame.body.velocity_blocks_per_second),
                on_ground=frame.body.is_on_ground,
                horizontal_collision=frame.body.horizontal_collision,
                receipt_status=result.backend_result.receipt.status,
                verified_submission=submission_window is not None,
            ))
        else:
            raise RuntimeError("B10 verified executor exceeded its proof horizon")
        if executor.state is not VerifiedMotionExecutorState.COMPLETE:
            raise RuntimeError(f"B10 verified executor ended in {executor.state.value}")
        target_position = (target[0] + .5, float(target[1] + 1), target[2] + .5)
        horizontal_error = math.hypot(
            frame.body.position[0] - target_position[0],
            frame.body.position[2] - target_position[2],
        )
        trial = dict(
            name=f"{group}-{direction_index}-{repetition}",
            episode_id=episode,
            group=group, direction_index=direction_index, repetition=repetition,
            moving_exit=moving_exit,
            requested_speed_band=list(speed_band) if speed_band is not None else None,
            entry_speed_blocks_per_second=entry_speed,
            entry_position=list(anchor.physics_state.position),
            start_observation_sequence_id=anchor.observation_sequence_id,
            anchor_movement_tick_id=anchor.movement_tick_id,
            information_rounds=information_rounds,
            information_attempts=[list(attempt) for attempt in information_attempts],
            **solve_measurement,
            candidates_evaluated=solved.candidates_evaluated,
            command_count=len(solved.proof.commands),
            tick_input_count=len(solved.proof.tick_inputs),
            trajectory_state_count=len(solved.proof.trajectory),
            world_dependency_count=len(solved.proof.world_dependencies),
            target_position=list(target_position),
            final_position=list(frame.body.position),
            horizontal_error_blocks=horizontal_error,
            level_error_blocks=abs(frame.body.position[1] - target_position[1]),
            jump_pulses=sum(int(row["command"]["jump"]) for row in samples),
            horizontal_collisions=sum(int(row["horizontal_collision"]) for row in samples),
            landed=frame.body.is_on_ground,
            samples=samples,
        )
        trials.append(trial)
        evidence_buffer.append("b10-gap-solver-trials.jsonl", trial)

    if targeted_probe is None:
        run_trial(0, 0, "preflight")
        for direction_index in range(4):
            for repetition in range(10):
                run_trial(direction_index, repetition, "validation")
        run_trial(0, 0, "turn_preflight", moving_exit=True)
        for direction_index in range(4):
            for repetition in range(10):
                run_trial(
                    direction_index, repetition, "turn_validation",
                    moving_exit=True,
                )

    environment = load_frozen_environment(CONFIG / "environment-v1.json")
    catalog = BlockMotionCatalog.load(
        CONFIG / "block-motion-traits-v1.json",
        CONFIG / "vanilla-block-registry-1_21.json",
    )
    ground_profile = load_ground_motion_profile(
        CONFIG / "ordinary-ground-b07-v1.json",
        environment=environment, catalog=catalog,
    )
    jump_profile = load_jump_up_profile(
        CONFIG / "jump-up-b06-v1.json",
        environment=environment, catalog=catalog,
    )
    step_profile = load_step_profile(
        CONFIG / "step-b07-v1.json", environment=environment,
    )
    air_profiles = load_air_motion_profiles(
        CONFIG / "air-motions-b09-v1.json",
        environment=environment, catalog=catalog,
    )
    session_profiles = NavigationSessionProfiles(
        ground_profile, jump_profile, step_profile, air=air_profiles,
        gap_solver=load_gap_solver_policy(
            CONFIG / "air-motions-b09-v1.json",
        ),
    )
    coordinator_trials: list[dict] = []
    def collect_receiver_diagnostics(session: NavigationSession, trial: str) -> None:
        evidence_buffer.collect_receiver(session._motion_result_inbox, trial, episode_id=episode)

    def anchor_now() -> StateAnchor:
        if latest_application is None:
            raise RuntimeError("B10 coordinator has no movement-tick evidence")
        built = build_physics_state(frame, JAVA_1_21_RULESET, _STATE_ASSUMPTIONS)
        if built.status is not StateBuildStatus.READY or built.state is None:
            raise RuntimeError(f"B10 coordinator state is incomplete: {built}")
        tick = latest_application.movement_tick_id
        return StateAnchor(
            frame.session, frame.body.sequence_id, tick,
            MotionTickPhase.AFTER_MOVEMENT,
            latest_application.request_sequence_id, (tick, tick),
            JAVA_1_21_RULESET.ruleset_id,
            JAVA_1_21_RULESET.state_schema,
            "mc2p.input-projection.v1",
            replace(built.state, movement_tick_id=tick),
        )

    delayed_motion_worker = _MovementTickDelayedMotionWorker(motion_worker)
    coordinator_scenarios = _selected_b10_coordinator_scenarios(targeted_probe)
    for scenario_index, scenario in enumerate(coordinator_scenarios):
        direction_index = scenario.direction_index
        repetition = scenario.repetition
        trial_name = f"coordinator-{scenario.name}"
        if not scenario.fabric_supported:
            trial = dict(
                name=trial_name,
                scenario=scenario.name,
                scenario_kind=scenario.kind,
                direction_index=direction_index,
                repetition=repetition,
                fabric_supported=False,
                unsupported_reason=scenario.unsupported_reason,
                terminal_state="not_run",
                terminal_reason=scenario.unsupported_reason,
                damage_points=0.0,
                ordered_source_id=None,
                source_release_status="not_registered",
                source_released=True,
                worker_delivery=None,
                samples=[],
            )
            coordinator_trials.append(trial)
            evidence_buffer.append("b10-coordinator-trials.jsonl", trial)
            continue
        _, _, yaw = _DIRECTIONS[direction_index]
        teleport(yaw)
        start_health = float(frame.body.health_points)
        start_position = frame.body.position
        target = targets[direction_index]
        start_query = query_support_surfaces(
            frame.world, origin_x, origin_z, feet_y, feet_y,
        )
        target_query = query_support_surfaces(
            frame.world, target[0], target[2], feet_y, feet_y,
        )
        if not start_query.surfaces or not target_query.surfaces:
            raise RuntimeError("B10 session could not resolve a known support surface")
        route_id = f"b10-session-{scenario_index}-{direction_index}-{repetition}"
        session = NavigationSession(
            route_id,
            session_profiles,
            planner_worker=planner_worker,
            owns_planner_worker=False,
            motion_worker=delayed_motion_worker,
            observation_adapter=runtime.navigation_observation_adapter,
        )
        samples: list[dict] = []

        def capture_formal_frame(
            driver_frame: _B10FormalDriverFrame,
        ) -> None:
            nonlocal frame, latest_application
            collect_receiver_diagnostics(session, trial_name)
            captured = _capture_b10_formal_driver_frame(
                driver_frame=driver_frame,
                runtime=runtime,
                session=session,
                trial_name=trial_name,
                episode=episode,
                scenario_kind=scenario.kind,
                evidence_buffer=evidence_buffer,
            )
            frame = captured.navigation_frame
            latest_application = captured.latest_application
            diagnostic()
            if captured.sample is not None:
                samples.append(captured.sample)

        target_position = (
            target[0] + .5,
            float(target[1] + 1),
            target[2] + .5,
        )
        goal_state = GoalState(
            Aabb(
                target_position[0] - .2,
                target_position[1] - .08,
                target_position[2] - .2,
                target_position[0] + .2,
                target_position[1] + .08,
                target_position[2] + .2,
            ),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        try:
            formal_result = _run_b10_formal_driver_scenario(
                runtime=runtime,
                session=session,
                delayed_motion_worker=delayed_motion_worker,
                scenario=scenario,
                goal_state=goal_state,
                goal_id="b10-gap-goal",
                goal_revision=scenario_index + 1,
                task=task,
                behavior=behavior,
                expected_start_position=start_position,
                expected_start_yaw_degrees=yaw,
                manual_source=manual_source,
                clock_ns=time.perf_counter_ns,
                max_control_frames=96,
                after_frame=capture_formal_frame,
            )
        except BaseException as error:
            evidence_buffer.append(
                "b10-coordinator-trials.jsonl",
                {
                    "name": trial_name,
                    "scenario": scenario.name,
                    "scenario_kind": scenario.kind,
                    "fabric_supported": True,
                    "partial": True,
                    "terminal_state": "incomplete",
                    "primary_error_type": type(error).__name__,
                    "source_release_status": (
                        "shared_cleanup_not_signed_after_error"
                    ),
                    "source_released": False,
                    "recorded_control_frames": sum(
                        row.get("trial") == trial_name
                        for row in evidence_buffer.rows(
                            "b10-coordinator-control-frames.jsonl"
                        )
                    ),
                },
            )
            raise
        collect_receiver_diagnostics(session, trial_name)
        receiver_rows = [
            row for row in evidence_buffer.rows(
                "b10-receiver-diagnostics.jsonl"
            )
            if row.get("trial") == trial_name
        ]
        receiver_delivery = _receiver_diagnostic_summary(
            receiver_rows,
        )["deliveries"]
        recorded_control_frames = sum(
            row.get("trial") == trial_name
            for row in evidence_buffer.rows(
                "b10-coordinator-control-frames.jsonl"
            )
        )
        trial = _build_b10_coordinator_trial(
            trial_name=trial_name,
            scenario=scenario,
            formal_result=formal_result,
            samples=samples,
            receiver_delivery=receiver_delivery,
            recorded_control_frames=recorded_control_frames,
            start_position=start_position,
            target_position=target_position,
            final_position=frame.body.position,
            landed=frame.body.is_on_ground,
            damage_points=max(
                0.0, start_health - float(frame.body.health_points),
            ),
        )
        coordinator_trials.append(trial)
        evidence_buffer.append("b10-coordinator-trials.jsonl", trial)

    if targeted_probe is not None:
        summary, checks = _targeted_b10_summary(coordinator_trials, runtime_state=runtime.state.value)
        write_json_atomic(directory / "b10-gap-solver.json", summary)
        return summary, [json.loads(row) for row in rows], checks

    moving_negative_trials: list[dict] = []
    speed_bands = ((.5, 1.0), (1.0, 2.0), (2.0, 3.0))
    speed_targets = (.75, 1.5, 2.5)

    def configure_moving_lane(direction_index: int) -> _AirRequestPlan:
        dx, dz, _ = _DIRECTIONS[direction_index]
        lane_supports = tuple(dict.fromkeys((
            *( (origin_x - dx * distance, feet_y - 1,
                 origin_z - dz * distance) for distance in range(4) ),
            targets[direction_index],
        )))
        # The negative landing case must remain fully observed while all bounded
        # templates run to their horizon.  Keep a narrow corridor, but extend it
        # beyond the landing support so a rejected candidate is not mislabeled as
        # missing world knowledge after it passes the deliberately narrow target.
        lane_volume = tuple(
            (
                origin_x + dx * along - dz * lateral,
                y,
                origin_z + dz * along + dx * lateral,
            )
            for along in range(-5, 9)
            for lateral in range(-2, 3)
            for y in range(feet_y - 2, feet_y + 4)
        )
        known_gap_and_landing_depth = tuple(
            (origin_x + dx * distance, y, origin_z + dz * distance)
            for distance in (0, 1, 2)
            for y in range(feet_y - 24, feet_y - 2)
        )
        support_set = set(lane_supports)
        air = tuple(dict.fromkeys(
            position
            for position in (*lane_volume, *known_gap_and_landing_depth)
            if position not in support_set
        ))
        fixture_writer(air, "minecraft:air")
        fixture_writer(lane_supports, "minecraft:grass_block")
        return _AirRequestPlan("navigation_v1", air)

    def prepare_moving_entry(
            direction_index: int, speed_band: tuple[float, float],
            target_speed: float,
            observation_request: _AirRequestSource, *,
            look_position: tuple[int, int, int] | None = None) -> None:
        nonlocal frame
        dx, dz, yaw = _DIRECTIONS[direction_index]
        start_position = (
            origin_x + .5 - dx * 3.0,
            float(feet_y),
            origin_z + .5 - dz * 3.0,
        )
        teleport(
            yaw, position=start_position,
            observation_request=observation_request,
        )
        look_at_cell(
            look_position or targets[direction_index],
            observation_request=observation_request,
        )
        target_along = -.05
        controls = (
            MovementV1(),
            MovementV1(forward=1),
            MovementV1(forward=1, sprint=True),
            MovementV1(forward=-1),
        )
        for _ in range(80):
            along = (
                (frame.body.position[0] - (origin_x + .5)) * dx
                + (frame.body.position[2] - (origin_z + .5)) * dz
            )
            forward_speed = (
                frame.body.velocity_blocks_per_second[0] * dx
                + frame.body.velocity_blocks_per_second[2] * dz
            )
            if (-.18 <= along <= .05
                    and speed_band[0] - 1.0e-6 <= forward_speed
                        <= speed_band[1] + 1.0e-6
                    and frame.body.is_on_ground):
                return
            if along > .07:
                raise RuntimeError(
                    "R4 approach passed its verified entry before reaching "
                    f"speed band {speed_band}: along={along}, speed={forward_speed}"
                )
            built = build_physics_state(
                frame, JAVA_1_21_RULESET, _STATE_ASSUMPTIONS,
            )
            if built.status is not StateBuildStatus.READY or built.state is None:
                raise RuntimeError(f"R4 approach state is incomplete: {built}")
            physics_world = PhysicsWorldView(frame.world, JAVA_1_21_RULESET)
            ranked = []
            position_weight = 5.0 if along > -.5 else 1.5
            for movement in controls:
                projected = project_movement_command(
                    built.state, movement,
                    movement_yaw_radians=built.state.yaw_radians,
                )
                if (projected.status is not ProjectionStatus.READY
                        or projected.tick_input is None):
                    continue
                calculated = physics_step(
                    built.state, projected.tick_input,
                    physics_world, JAVA_1_21_RULESET,
                )
                if (calculated.status is not CalculationStatus.OK
                        or calculated.next_state is None):
                    continue
                next_state = calculated.next_state
                next_along = (
                    (next_state.position[0] - (origin_x + .5)) * dx
                    + (next_state.position[2] - (origin_z + .5)) * dz
                )
                next_speed = 20.0 * (
                    next_state.velocity_blocks_per_tick[0] * dx
                    + next_state.velocity_blocks_per_tick[2] * dz
                )
                score = (
                    abs(next_along - target_along) * position_weight
                    + abs(next_speed - target_speed) * .8
                )
                if next_along > .06:
                    score += 100.0 + (next_along - .06) * 100.0
                if next_speed < -.01:
                    score += 20.0
                ranked.append((score, movement))
            if not ranked:
                raise RuntimeError("R4 approach has no calculable ground input")
            ranked.sort(key=lambda item: item[0])
            step(ranked[0][1], request=observation_request)
        raise RuntimeError(
            f"R4 approach did not reach speed band {speed_band} within 80 ticks"
        )

    for direction_index in range(4):
        moving_request = configure_moving_lane(direction_index)
        dx, dz, _ = _DIRECTIONS[direction_index]
        target = targets[direction_index]
        for band_index, (speed_band, target_speed) in enumerate(zip(
                speed_bands, speed_targets)):
            for repetition in range(5):
                if repetition == 0:
                    low_ceiling = (target[0], feet_y + 1, target[2])
                    fixture_writer((low_ceiling,), "minecraft:stone")
                    blocked_request = _AirRequestPlan(
                        "navigation_v1",
                        tuple(
                            position for position in moving_request.air_positions
                            if position != low_ceiling
                        ),
                    )
                    prepare_moving_entry(
                        direction_index,
                        speed_band,
                        target_speed,
                        blocked_request,
                        look_position=low_ceiling,
                    )
                    landing = LandingRegion(
                        target[0] + .3, target[0] + .7,
                        target[2] + .3, target[2] + .7,
                        float(target[1] + 1),
                    )

                    def solve_negative_pair():
                        current_anchor = anchor_now()
                        window = CandidateExecutionWindow(
                            current_anchor.movement_tick_id + 1,
                            current_anchor.movement_tick_id + 2,
                        )
                        current_world = PhysicsWorldView(
                            frame.world, JAVA_1_21_RULESET,
                        )
                        forward_request = GapSolveRequest(
                            (dx, dz), landing, window,
                        )
                        reverse_request = GapSolveRequest(
                            (-dx, -dz), landing, window,
                        )
                        return (
                            current_anchor,
                            solve_one_cell_gap(
                                current_anchor, current_world,
                                reverse_request,
                            ),
                            solve_one_cell_gap(
                                current_anchor, current_world,
                                forward_request,
                            ),
                        )

                    negative_anchor, reverse, insufficient = solve_negative_pair()
                    negative_information: set[tuple[int, int, int]] = set()
                    negative_information_rounds = 0
                    for _ in range(20):
                        if (insufficient.status is not SolveStatus.NEEDS_WORLD
                                or not insufficient.missing_cells):
                            break
                        if not _information_retry_allowed(
                                insufficient.missing_cells,
                                negative_information,
                                completed_rounds=negative_information_rounds,
                                max_rounds=20):
                            break
                        negative_information.update(insufficient.missing_cells)
                        negative_information_rounds += 1
                        observe_missing_cells(insufficient.missing_cells)
                        prepare_moving_entry(
                            direction_index,
                            speed_band,
                            target_speed,
                            blocked_request,
                            look_position=low_ceiling,
                        )
                        negative_anchor, reverse, insufficient = (
                            solve_negative_pair()
                        )
                    negative = dict(
                        direction_index=direction_index,
                        speed_band_index=band_index,
                        entry_speed_blocks_per_second=20.0 * math.hypot(
                            negative_anchor.physics_state.velocity_blocks_per_tick[0],
                            negative_anchor.physics_state.velocity_blocks_per_tick[2],
                        ),
                        entry_direction_status=reverse.status.value,
                        entry_direction_reasons=list(reverse.reasons),
                        insufficient_landing_status=insufficient.status.value,
                        insufficient_landing_reasons=list(insufficient.reasons),
                        insufficient_landing_missing_cells=(
                            [list(position) for position in insufficient.missing_cells]
                        ),
                    )
                    moving_negative_trials.append(negative)
                    evidence_buffer.append(
                        "r4-moving-gap-negative-trials.jsonl",
                        negative,
                    )
                    fixture_writer((low_ceiling,), "minecraft:air")
                    prepare_moving_entry(
                        direction_index, speed_band, target_speed, moving_request,
                    )
                    if frame.world.cell(low_ceiling).knowledge is not CellKnowledge.AIR:
                        raise RuntimeError(
                            "R4 removed landing obstruction was not reobserved as air"
                        )
                else:
                    prepare_moving_entry(
                        direction_index, speed_band, target_speed, moving_request,
                    )
                run_trial(
                    direction_index, repetition,
                    f"r4_validation_{band_index}",
                    prepared_entry=True,
                    observation_request=moving_request,
                    speed_band=speed_band,
                    reprepare_entry=lambda direction_index=direction_index,
                    speed_band=speed_band, target_speed=target_speed,
                    moving_request=moving_request: prepare_moving_entry(
                        direction_index,
                        speed_band,
                        target_speed,
                        moving_request,
                    ),
                )

    # All control trials have finished. A delayed stop callback can now pair
    # with a start from an earlier solve before any sample is attributed.
    gc_timeline.close()
    gc_attribution = _attribute_solve_gc_pauses(trials, gc_timeline)
    validation = [trial for trial in trials if trial["group"] == "validation"]
    turn_validation = [
        trial for trial in trials if trial["group"] == "turn_validation"
    ]
    moving_validation = [
        trial for trial in trials if trial["group"].startswith("r4_validation_")
    ]
    coordinator_executed = [
        trial for trial in coordinator_trials if trial["fabric_supported"]
    ]
    coordinator_unsupported = [
        trial for trial in coordinator_trials if not trial["fabric_supported"]
    ]

    summary = dict(
        schema_version="mc2p.b10-gap-solver.v2",
        partial=False, matrix_complete=True,
        solver_id=SOLVER_ID,
        preflight_count=1,
        turn_preflight_count=1,
        validation_count=len(validation),
        validation_success_count=sum(
            trial["landed"]
            and trial["horizontal_error_blocks"] <= .2
            and trial["level_error_blocks"] <= .03
            and trial["horizontal_collisions"] == 0
            and trial["jump_pulses"] == 1
            for trial in validation
        ),
        turn_validation_count=len(turn_validation),
        turn_validation_success_count=sum(
            trial["landed"]
            and trial["horizontal_error_blocks"] <= .2
            and trial["level_error_blocks"] <= .03
            and trial["horizontal_collisions"] == 0
            and trial["jump_pulses"] == 1
            for trial in turn_validation
        ),
        moving_validation_count=len(moving_validation),
        moving_validation_success_count=sum(
            trial["landed"]
            and trial["horizontal_error_blocks"] <= .2
            and trial["level_error_blocks"] <= .03
            and trial["horizontal_collisions"] == 0
            and trial["jump_pulses"] == 1
            and trial["requested_speed_band"][0] - 1.0e-6
                <= trial["entry_speed_blocks_per_second"]
                <= trial["requested_speed_band"][1] + 1.0e-6
            for trial in moving_validation
        ),
        moving_negative_case_count=len(moving_negative_trials) * 2,
        moving_negative_trials=moving_negative_trials,
        **_summarize_b10_coordinator_trials(
            coordinator_trials,
            preregistered_count=len(coordinator_trials),
        ),
        solve_time_ns=dict(
            samples=len(solve_times),
            p95=_percentile(solve_times, .95),
            p99=_percentile(solve_times, .99),
            maximum=max(solve_times),
        ),
        synchronous_probe_timing=dict(
            scope="synchronous_probe_only",
            disposition="report_only",
            planned_count=142,
            completed_count=len(trials),
            warning_threshold_ns=30_000_000,
            warning_count=sum(value >= 30_000_000 for value in solve_times),
        ),
        **_solve_measurement_summary(trials),
        **gc_attribution,
        physics_snapshot_time_ns=dict(
            samples=len(physics_snapshot_times),
            p95=_percentile(physics_snapshot_times, .95),
            p99=_percentile(physics_snapshot_times, .99),
            maximum=max(physics_snapshot_times),
        ),
        receiver_diagnostics=_receiver_diagnostic_summary(
            list(evidence_buffer.rows("b10-receiver-diagnostics.jsonl")),
        ),
        trials=trials,
        coordinator_trials=coordinator_trials,
    )
    write_json_atomic(directory / "b10-gap-solver.json", summary)
    checks = [
        dict(name="b10b_preflight_separate_from_validation",
             passed=(len(trials) - len(moving_validation) == 82
                     and summary["preflight_count"] == 1
                     and summary["turn_preflight_count"] == 1)),
        dict(name="b10b_four_directions_ten_fabric_runs_each", passed=(
            len(validation) == 40
            and {index: sum(trial["direction_index"] == index for trial in validation)
                 for index in range(4)} == {0: 10, 1: 10, 2: 10, 3: 10}
        )),
        dict(name="b10c_turn_exit_four_directions_ten_runs_each", passed=(
            len(turn_validation) == 40
            and summary["turn_validation_success_count"] == 40
            and {index: sum(trial["direction_index"] == index
                            for trial in turn_validation)
                 for index in range(4)} == {0: 10, 1: 10, 2: 10, 3: 10}
        )),
        dict(name="b10b_all_validation_runs_land_in_target", passed=(
            summary["validation_success_count"] == 40
        )),
        dict(name="b10c_preregistered_coordinator_matrix_is_honest", passed=(
            summary["coordinator_preregistered_count"] == 56
            and summary["coordinator_executed_count"] == 48
            and summary["coordinator_unsupported_count"] == 8
            and all(
                trial["unsupported_reason"]
                    == "fabric_input_late1_injection_unavailable"
                for trial in coordinator_unsupported
            )
        )),
        dict(name="b10c_worker_delivery_matrix_is_safe_and_bounded", passed=(
            summary["coordinator_baseline_success_count"] == 20
            and summary["coordinator_delay_2_5_success_count"] == 16
            and summary["coordinator_delay_10_safe_count"] == 8
            and summary["coordinator_never_return_safe_count"] == 4
            and all(trial["damage_points"] == 0.0
                    for trial in coordinator_executed)
            and all(
                trial["entry_alignment_frames"] <= 5
                for trial in coordinator_executed
            )
            and all(_b10_coordinator_delivery_gate(trial)
                    for trial in coordinator_executed)
        )),
        dict(name="r4_three_speed_bands_four_directions_five_runs_each",
             passed=(
                 len(moving_validation) == 60
                 and summary["moving_validation_success_count"] == 60
                 and {
                     (direction, band): sum(
                         trial["direction_index"] == direction
                         and trial["group"] == f"r4_validation_{band}"
                         for trial in moving_validation
                     )
                     for direction in range(4) for band in range(3)
                 } == {
                     (direction, band): 5
                     for direction in range(4) for band in range(3)
                 }
             )),
        dict(name="r4_two_typed_negative_cases_per_speed_direction_category",
             passed=(
                 summary["moving_negative_case_count"] == 24
                 and all(
                     trial["entry_direction_status"]
                         == SolveStatus.NEEDS_STATE.value
                     and "entry_velocity_direction"
                         in trial["entry_direction_reasons"]
                     and trial["insufficient_landing_status"]
                         == SolveStatus.NO_SOLUTION_WITHIN_SEARCH.value
                     for trial in moving_negative_trials
                 )
             )),
        dict(name="b10b_proof_records_stay_correlated", passed=all(
            trial["command_count"] == trial["tick_input_count"]
            and trial["trajectory_state_count"] == trial["command_count"] + 1
            and trial["world_dependency_count"] > 0
            for trial in trials
        )),
        dict(name="b10b_solver_is_bounded", passed=(
            all(trial["candidates_evaluated"] <= 12 for trial in trials)
            and all(trial["command_count"] <= 20 for trial in trials)
        )),
        dict(name="b10b_gc_pause_timeline_complete",
             passed=summary["gc_pause_timeline"]["complete"]),
    ]
    return summary, [json.loads(row) for row in rows], checks

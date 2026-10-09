"""Formal-path scenario runner for navigation coordination.

Formal chain: PlayerRuntimeV1 -> RuntimeNavigationDriver -> NavigationSession ->
(inline) planner, (inline) motion solver, admission, executors; the game is
CalculatorBackend.  Two test substitutions are made, both synchronous versions of
existing worker interfaces:
  * InlinePlannerWorker, which runs planner_worker._execute_job (the same
    function, with the same error wrapping, as the real planner process);
  * InlineMotionWorker, which runs motion_worker._execute_job on poll.  The
    coordinator accepts the same typed worker protocol.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from copy import deepcopy
from pathlib import Path
import math
import random
from typing import Callable

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav import motion_worker, planner_worker
from mc2p.motion_nav.motion_risk import TaskDamageBudget, TaskRiskLedger
from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.world_model import Aabb
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver, RuntimeNavigationDriverState
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport
from mc2p.motion_nav.movement_transition import MovementMode
from tests.test_player_runtime import _RecordingTrace
from mc2p.motion_nav.runtime_adapter import TEST_ORACLE

from tests.sim.backend import CalculatorBackend, Perturbations, Scene
from tests.sim.monitor import InvariantMonitor, TickEvidence
from tests.sim.async_monitor import AsyncCoverageRequirement, ObservedAsyncActivity, VerificationAssessment
from tests.sim.planning_clock import deterministic_planning_clock
from mc2p.motion_nav.async_work import AsyncWorkKind

CONFIG = Path("config/motion-navigation")
TERMINAL_DRIVER = {RuntimeNavigationDriverState.SUCCESS, RuntimeNavigationDriverState.FAILED,
                   RuntimeNavigationDriverState.CANCELLED, RuntimeNavigationDriverState.STOPPED,
                   RuntimeNavigationDriverState.INTERACTION_REQUIRED}


def _goal(position, risk_policy_id="no_expected_damage") -> GoalState:
    """Same goal region as scripts/continuous_height_runtime.py (formal Fabric probe)."""
    x, y, z = position
    return GoalState(Aabb(x - .20, y - .08, z - .20, x + .20, y + .08, z + .20),
                     GoalSupport.SOLID, frozenset({MovementMode.WALK}), frozenset({"standing"}), .6,
                     risk_policy_id=risk_policy_id)


class InlinePlannerWorker:
    """Synchronous stand-in for PlannerWorker using the worker's own job function."""

    def __init__(self):
        self._job = None
        self._queued_jobs = []
        self.activity = []

    def is_alive(self) -> bool:
        return True

    def submit_surface_snapshot(self, snapshot, ground_profile, step_profile, request,
                                jump_profile=None, *, air_profiles=(), ground_mode_profile=None) -> bool:
        job = planner_worker._PlanningJob(None, snapshot, ground_profile, ground_mode_profile,
                                          step_profile, jump_profile, air_profiles, request)
        if self._job is None:
            self._job = job
        else:
            if self._queued_jobs:
                return planner_worker.PlanningSubmissionStatus.BUSY
            self._queued_jobs.append(job)
        self.activity.append(ObservedAsyncActivity(request.work_identity, "submit"))
        return True

    def poll_latest(self):
        job = self._job
        self._job = self._queued_jobs.pop(0) if self._queued_jobs else None
        if job is not None:
            self.activity.append(ObservedAsyncActivity(job.request.work_identity, "poll"))
        if job is None:
            result = None
        else:
            with deterministic_planning_clock():
                result = planner_worker._execute_job(job)
        return result

    def poll_available(self):
        result = self.poll_latest()
        return () if result is None else (result,)

    def close(self) -> None:
        self._job = None
        self._queued_jobs.clear()


class InlineMotionWorker:
    """Synchronous stand-in with MotionSolverWorker's interface."""

    pid = None

    def __init__(self, *_, **__):
        self._pending = []
        self.activity = []

    def is_alive(self) -> bool:
        return True

    def submit(self, job) -> bool:
        self._pending.append(job)
        self.activity.append(ObservedAsyncActivity(job.work_identity, "submit"))
        return True

    def poll_available(self):
        done = tuple(motion_worker._execute_job(job) for job in self._pending)
        self.activity.extend(ObservedAsyncActivity(job.work_identity, "poll") for job in self._pending)
        self._pending = []
        return done

    def close(self) -> None:
        self._pending = []


@dataclass
class Event:
    """Run `action(context)` once, on the first tick where `when(context)` holds."""

    name: str
    when: Callable[["Context"], bool]
    action: Callable[["Context"], None]
    fired_at: int | None = None
    kind: str | None = None
    goal_revision: int | None = None


@dataclass
class Scenario:
    name: str
    scene: Scene
    start: tuple[float, float, float]
    goal: tuple[float, float, float]
    yaw_degrees: float = 0.0
    damage_points: float = 0.0
    perturbations: Perturbations = field(default_factory=Perturbations)
    events: list[Event] = field(default_factory=list)
    max_ticks: int = 400
    expect: str = "success"          # what a correct navigation stack should do
    goal_yaw_degrees: float | None = None
    start_velocity_blocks_per_tick: tuple[float, float, float] | None = None
    landing_support_cells: tuple[tuple[int, int, int], ...] = ()
    async_coverage: AsyncCoverageRequirement = field(default_factory=AsyncCoverageRequirement)
    initial_unknown_cells: frozenset[tuple[int, int, int]] = frozenset()


@dataclass
class Context:
    tick: int
    backend: CalculatorBackend
    session: NavigationSession
    driver: RuntimeNavigationDriver
    clock: list[int]
    damage_points: float
    risk_policy_id: str
    goal_state: GoalState
    goal_position: tuple[float, float, float]
    planner_worker: InlinePlannerWorker | None = None

    @property
    def diagnostics(self):
        return self.session.diagnostics

    @property
    def planning_activity(self) -> tuple[ObservedAsyncActivity, ...]:
        return (() if self.planner_worker is None else tuple(self.planner_worker.activity))


def seed_memory(runtime: PlayerRuntimeV1, scene: Scene, *, exclude=frozenset()) -> None:
    """Pre-load the Runtime-owned world with earlier, non-visual knowledge of the scene.

    Stands in for a map explored earlier: blocks and air are known, but no cell
    carries near lower-part visual evidence.
    """
    runtime.navigation_observation_adapter.seed_test_oracle_memory(
        TEST_ORACLE, {p: scene.geometry(b) for p, b in scene.solids.items() if p not in exclude},
        tuple(p for p in scene.air_cells() if p not in exclude),
    )


@dataclass
class Result:
    scenario: str
    expect: str
    outcome: str
    reason: str
    ticks: int
    final_position: tuple[float, float, float]
    damage: float
    violations: list[tuple[int, str, str]]
    events: list[str]
    coverage_gaps: tuple[str, ...]
    trace: list[dict]
    event_dispatches: tuple[tuple[str, int], ...] = ()
    dispatched_perturbations: tuple[tuple[str, int], ...] = ()
    applied_perturbations: tuple[tuple[str, int], ...] = ()
    verification: VerificationAssessment | None = None

    @property
    def verification_complete(self) -> bool:
        return self.verification is not None and self.verification.complete

    @property
    def verdict(self) -> str:
        ok = (self.outcome == self.expect) and not self.violations and self.verification_complete
        return "PASS" if ok else "FAIL"

    @property
    def outcome_class(self) -> str:
        if self.violations:
            return "unexpected_result"
        if self.outcome == "success":
            return "task_success"
        final = self.trace[-1] if self.trace else None
        safe_release = (
            final is not None
            and not final["source_bound"]
            and final["on_ground"]
            and final["support_fraction"] is not None
            and final["support_fraction"] > 0.0
        )
        if safe_release and self.outcome in {
                "failed", "cancelled", "closed", "requires_interaction"}:
            return "bounded_safe_failure"
        return "unexpected_result"

    @property
    def recovery_failures(self) -> int:
        return max(
            (int(row["recovery_total_starts"]) for row in self.trace),
            default=0,
        )


def run(scenario: Scenario, *, after_terminal_ticks: int = 20,
        trace_sink: Callable[[dict], None] | None = None,
        event_ticks: dict[str, int] | None = None,
        control_step: Callable[[Context], tuple[str, ...] | None] | None = None,
        risk_ledger: TaskRiskLedger | None = None,
        reach_policy: GoalReachPolicy = GoalReachPolicy.COMPLETE_ON_REACH,
        backend_factory=CalculatorBackend,
        planner_factory=InlinePlannerWorker,
        motion_factory=InlineMotionWorker) -> Result:
    events = [replace(event, fired_at=None) for event in scenario.events]
    if event_ticks is not None:
        if set(event_ticks) != {event.name for event in events}:
            raise ValueError("frozen event names do not match scenario")
        events = [replace(event, when=(lambda context, predicate=event.when,
                                       at=event_ticks[event.name]:
                                       context.tick >= at and predicate(context)))
                  for event in events]
    clock = [100_000_000]
    command_activities = {}
    backend = backend_factory(clock, Scene(dict(scenario.scene.solids), scenario.scene.volume).with_floor(),
                                scenario.start, scenario.yaw_degrees,
                                perturbations=deepcopy(scenario.perturbations))
    if scenario.start_velocity_blocks_per_tick is not None:
        backend.state = replace(
            backend.state,
            velocity_blocks_per_tick=scenario.start_velocity_blocks_per_tick,
        )
    runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
    reset = runtime.reset(ResetRequestV0("reset-sim", backend.episode, "test", 1, 10_000_000_000))
    if not reset.succeeded:
        raise RuntimeError("reset failed")
    seed_memory(runtime, backend.scene, exclude=scenario.initial_unknown_cells)
    profiles = NavigationSessionProfiles.load(CONFIG)
    planner = planner_factory()
    motion = motion_factory()
    session = NavigationSession("sim", profiles, planner_worker=planner,
                                motion_worker=motion,
                                risk_ledger=risk_ledger,
                                clock_ns=lambda: clock[0])
    driver = RuntimeNavigationDriver(runtime, session, clock_ns=lambda: clock[0])
    backend.set_external_perturbation_guard(lambda: (
        driver.source is not None
        and driver.state not in TERMINAL_DRIVER
        and session.diagnostics.state.value not in {
            "complete", "failed", "cancelled", "closed",
        }
    ))
    # This harness contains the navigation body owners, but not C1's external
    # motion detector and recovery driver.  Inject shoves only while one of the
    # owners under test is active; otherwise the harness would manufacture an
    # unowned disturbance that the omitted outer layer is responsible for.
    backend.set_external_impulse_guard(
        lambda: session.has_owned_body_control
    )
    policy = "sim-budget" if scenario.damage_points else "no_expected_damage"
    goal = _goal(scenario.goal, policy)
    if scenario.goal_yaw_degrees is not None:
        goal = replace(
            goal,
            required_yaw_radians=math.radians(scenario.goal_yaw_degrees),
            maximum_yaw_error_radians=math.radians(2.0),
        )
    driver.start("goal", 1, goal, clock[0],
                 damage_budget=TaskDamageBudget(policy, scenario.damage_points),
                 reach_policy=reach_policy)
    monitor = InvariantMonitor()
    context = Context(0, backend, session, driver, clock,
                      scenario.damage_points, policy, goal, scenario.goal, planner)
    trace: list[dict] = []
    released_ticks = 0
    information_activity = {}
    perturbations_stopped = False
    tick = 0
    try:
        for tick in range(1, scenario.max_ticks + 1):
            context.tick = tick
            planner_activity_start = len(planner.activity)
            external_movement_intents: tuple[str, ...] = ()
            movement_tick_before = backend.movement_tick
            submitted_count_before = len(backend.actions)
            for event in events:
                if (event.fired_at is None
                        and driver.source is not None
                        and driver.state not in TERMINAL_DRIVER
                        and context.diagnostics.state.value not in {
                            "complete", "failed", "cancelled", "closed",
                        }
                        and event.when(context)):
                    event.fired_at = tick
                    event.action(context)
            if driver.source is not None and driver.state in {
                    RuntimeNavigationDriverState.SUCCESS, RuntimeNavigationDriverState.FAILED, RuntimeNavigationDriverState.CANCELLED}:
                driver.release("simulation_terminal_release")
            if driver.source is None or driver.state in TERMINAL_DRIVER:
                if not perturbations_stopped:
                    backend.stop_external_perturbations()
                    perturbations_stopped = True
                # The client still samples any accepted lease after source release.
                backend.free_tick()
                released_ticks += 1
            else:
                if control_step is None:
                    driver.tick(BehaviorProfileV0(), clock[0] + 500_000_000)
                else:
                    external_movement_intents = control_step(context) or ()
                    if (type(external_movement_intents) is not tuple
                            or not all(type(item) is str
                                       for item in external_movement_intents)):
                        raise TypeError("control step must return movement intent ids")
            if backend.movement_tick == movement_tick_before:
                backend.free_tick()
            if backend.movement_tick != movement_tick_before + 1:
                raise RuntimeError(
                    "one scenario loop advanced multiple physical ticks; "
                    "the event and every application sample must be recorded"
                )
            diagnostics = session.diagnostics
            report = session.report
            information = session.planning_information_update
            if information is not None:
                information_activity[information.information_identity] = ObservedAsyncActivity(
                    information.information_identity, "notification")
            frame_diagnostics = (driver.last_frame_diagnostics
                                 if len(backend.actions) > submitted_count_before else None)
            applied_request = (backend.applied_commands[-1][1]
                               if backend.applied_commands
                               and backend.applied_commands[-1][0] == backend.movement_tick
                               else None)
            applied_movement = backend.applied[-1]
            input_record = (
                None if frame_diagnostics is None or frame_diagnostics.action_request_sequence is None
                else runtime.input_ledger.record(frame_diagnostics.action_request_sequence)
            )
            # Associate application with the actual winning submitted command,
            # even when its former owner handed over in the meantime.
            if frame_diagnostics is not None and frame_diagnostics.action_request_sequence is not None:
                command_activities[frame_diagnostics.action_request_sequence] = frame_diagnostics.movement_activity
                while len(command_activities) > 128:
                    command_activities.pop(next(iter(command_activities)))
            applied_activity = command_activities.get(applied_request)
            evidence = TickEvidence(
                backend.movement_tick, clock[0], backend.state.position, backend.state.on_ground,
                diagnostics.controller_ids,
                diagnostics.source_bound and driver.source is not None,
                diagnostics.state.value, diagnostics.reason,
                backend.damage_taken, scenario.damage_points,
                diagnostics.damage_spent, diagnostics.request_generation,
                diagnostics.goal_revision, diagnostics.information_wait_frames,
                applied_request, applied_movement,
                tuple(backend.submitted_commands),
                external_movement_intents + (() if frame_diagnostics is None else
                    frame_diagnostics.proposed_movement_intents),
                () if frame_diagnostics is None else frame_diagnostics.selected_intents,
                None if frame_diagnostics is None else frame_diagnostics.action_movement,
                context.goal_position,
                context.goal_state, backend.state.velocity_blocks_per_tick,
                backend.state.pose, backend.state.yaw_radians,
                context.goal_state.risk_policy_id, diagnostics.action_index,
                retry_attempt_id=diagnostics.retry_last_failure_attempt_id,
                observation_sequence=backend.sequence,
                route_id=diagnostics.route_id,
                submitted_request=(None if frame_diagnostics is None else
                                   frame_diagnostics.action_request_sequence),
                handoff=diagnostics.handoff,
                risk_actions=diagnostics.risk_actions,
                retry_round_failures=diagnostics.retry_round_failures,
                retry_total_failures=diagnostics.retry_total_failures,
                retry_cause_counts=tuple(
                    (cause.value, count)
                    for cause, count in diagnostics.retry_cause_counts
                ),
                retry_progress_version=diagnostics.retry_progress_version,
                retry_progress_evidence=diagnostics.retry_progress_evidence,
                retry_approved_round=diagnostics.retry_approved_round,
                retry_approved_total=diagnostics.retry_approved_total,
                retry_approved_cause_counts=tuple(
                    (cause.value, count)
                    for cause, count in diagnostics.retry_approved_cause_counts
                ),
                support_fraction=diagnostics.support_fraction,
                illegal_transition_count=diagnostics.illegal_transition_count,
                body_control_progress=diagnostics.body_control_progress,
                active_waits=diagnostics.active_waits,
                planning_work_owned=diagnostics.planning_work_owned,
                planning_work_identity_valid=
                    diagnostics.planning_work_identity_valid,
                planning_permit_identity_valid=
                    diagnostics.planning_permit_identity_valid,
                planning_information_identity_valid=
                    diagnostics.planning_information_identity_valid,
                async_work_diagnostics=session.async_work_diagnostics,
                applied_body_activity=applied_activity,
                active_motion_mailboxes=session.active_motion_mailboxes,
                recovery_budget_kind=diagnostics.recovery_budget_kind,
                recovery_maximum_starts=diagnostics.recovery_maximum_starts,
                recovery_window_starts=diagnostics.recovery_window_starts,
                recovery_total_starts=diagnostics.recovery_total_starts,
                recovery_limit_status=diagnostics.recovery_limit_status,
                recovery_limit_window_starts=(
                    diagnostics.recovery_limit_window_starts
                ),
                reach_policy=report.reach_policy,
                observed_goal_status=report.observed_goal_status,
                body_control_activities=diagnostics.body_control_activities,
                recovery_wait_status=diagnostics.recovery_wait_status,
                session_state=report.state,
            )
            monitor.check(evidence)
            row = {
                # External R28 ruler: actual worker submissions, not retry totals.
                "planning_submissions": tuple(
                    item.identity.key for item in planner.activity[planner_activity_start:]
                    if item.operation == "submit"
                ),
                "goal_revision_requests": tuple(
                    {"revision": event.goal_revision,
                     "movement_tick": movement_tick_before}
                    for event in events if event.fired_at == tick and event.goal_revision is not None
                ),
                "goal_position": context.goal_position,
                "goal_satisfied": all(
                    lower <= value <= upper for lower, value, upper in zip(
                        context.goal_state.region.as_tuple()[:3],
                        backend.state.position,
                        context.goal_state.region.as_tuple()[3:],
                    )
                ),
                "input_window": None if input_record is None else {
                    "requested_first_tick": input_record.requested_first_tick,
                    "latest_allowed_first_tick": input_record.latest_allowed_first_tick,
                    "requested_last_tick": input_record.requested_last_tick,
                },
                "async_events": tuple(asdict(event) for event in monitor.async_monitor.last_events),
                "async_coverage": dict(monitor.async_monitor.coverage),
                "loop_tick": tick, "tick": backend.movement_tick,
                "clock_ns": clock[0], "observation_sequence": backend.sequence,
                "movement_tick": backend.movement_tick,
                "submitted_request": None if not backend.actions else backend.actions[-1].request_sequence_id,
                "applied_request": applied_request,
                "sample_state": backend.sample_states[-1],
                "command_event": backend.command_events[-1],
                "proposed_intents": () if frame_diagnostics is None else frame_diagnostics.proposed_intents,
                "proposed_movement_intents": external_movement_intents + (
                    () if frame_diagnostics is None else
                    frame_diagnostics.proposed_movement_intents),
                "selected_intents": () if frame_diagnostics is None else frame_diagnostics.selected_intents,
                "candidate_intents": () if frame_diagnostics is None else frame_diagnostics.candidate_intents,
                "suppressed_intents": () if frame_diagnostics is None else frame_diagnostics.suppressed_intents,
                "action_request_sequence": None if frame_diagnostics is None else frame_diagnostics.action_request_sequence,
                "action_deadline_ns": None if frame_diagnostics is None else frame_diagnostics.action_deadline_ns,
                "applied_movement": {"forward": applied_movement.forward,
                                     "strafe": applied_movement.strafe,
                                     "jump": applied_movement.jump,
                                     "sneak": applied_movement.sneak,
                                     "sprint": applied_movement.sprint},
                "sampled_input": {
                    "forward": backend.sampled_inputs[-1].forward,
                    "strafe": backend.sampled_inputs[-1].strafe,
                    "jump": backend.sampled_inputs[-1].jump,
                    "sneak": backend.sampled_inputs[-1].sneak,
                    "sprint": backend.sampled_inputs[-1].sprint,
                },
                "position": backend.state.position,
                "velocity": backend.state.velocity_blocks_per_tick,
                "yaw_radians": backend.state.yaw_radians,
                "pitch_radians": backend.state.pitch_radians,
                "pose": backend.state.pose,
                "sneaking": backend.state.sneaking,
                "on_ground": backend.state.on_ground,
                "controller_ids": diagnostics.controller_ids,
                "body_control_activities": tuple(asdict(activity) for activity in diagnostics.body_control_activities),
                "submitted_body_activity": (None if frame_diagnostics is None or frame_diagnostics.movement_activity is None
                                             else asdict(frame_diagnostics.movement_activity)),
                "applied_body_activity": None if applied_activity is None else asdict(applied_activity),
                "controller_phase": diagnostics.controller_phase,
                "action_kind": diagnostics.action_kind,
                "action_index": diagnostics.action_index,
                "source_bound": diagnostics.source_bound and driver.source is not None,
                "request_generation": diagnostics.request_generation,
                "goal_revision": diagnostics.goal_revision,
                "route_id": diagnostics.route_id,
                "route_validation": (
                    None if diagnostics.route_validation is None else
                    asdict(diagnostics.route_validation)
                ),
                "local_direct_admission": (
                    None if diagnostics.local_direct_admission is None else
                    asdict(diagnostics.local_direct_admission)
                ),
                "damage": backend.damage_taken,
                "damage_spent": diagnostics.damage_spent,
                "risk_available_points": diagnostics.risk_available_points,
                "risk_policy_revision": diagnostics.risk_policy_revision,
                "risk_submission_capacity_exhausted":
                    diagnostics.risk_submission_capacity_exhausted,
                "support_fraction": diagnostics.support_fraction,
                "transition_count": diagnostics.transition_count,
                "illegal_transition_count": diagnostics.illegal_transition_count,
                "active_waits": diagnostics.active_waits,
                "planning_work_owned": diagnostics.planning_work_owned,
                "planning_work_identity_valid":
                    diagnostics.planning_work_identity_valid,
                "planning_permit_identity_valid":
                    diagnostics.planning_permit_identity_valid,
                "planning_information_identity_valid":
                    diagnostics.planning_information_identity_valid,
                "body_control_progress": (
                    None if diagnostics.body_control_progress is None else {
                        "owner_id": diagnostics.body_control_progress.owner_id,
                        "phase_label": diagnostics.body_control_progress.phase_label,
                        "progress_revision":
                            diagnostics.body_control_progress.progress_revision,
                        "action_index":
                            diagnostics.body_control_progress.action_index,
                        "last_confirmed_application_tick":
                            diagnostics.body_control_progress
                                .last_confirmed_application_tick,
                        "stall_limit_ticks":
                            diagnostics.body_control_progress.stall_limit_ticks,
                    }
                ),
                "retry_round_failures": diagnostics.retry_round_failures,
                "retry_total_failures": diagnostics.retry_total_failures,
                "retry_approved_round": diagnostics.retry_approved_round,
                "retry_approved_total": diagnostics.retry_approved_total,
                "retry_approved_cause_counts": tuple(
                    (cause.value, count)
                    for cause, count in diagnostics.retry_approved_cause_counts
                ),
                "retry_last_failure_attempt_id":
                    diagnostics.retry_last_failure_attempt_id,
                "retry_cause_counts": tuple(
                    (cause.value, count)
                    for cause, count in diagnostics.retry_cause_counts
                ),
                "retry_progress_version": diagnostics.retry_progress_version,
                "recovery_budget_kind": (
                    None if diagnostics.recovery_budget_kind is None else
                    diagnostics.recovery_budget_kind.value
                ),
                "recovery_maximum_starts": diagnostics.recovery_maximum_starts,
                "recovery_window_starts": diagnostics.recovery_window_starts,
                "recovery_total_starts": diagnostics.recovery_total_starts,
                "recovery_limit_status": (
                    None if diagnostics.recovery_limit_status is None else
                    diagnostics.recovery_limit_status.value
                ),
                "recovery_limit_window_starts": (
                    diagnostics.recovery_limit_window_starts
                ),
                "recovery_wait_status": (
                    None if diagnostics.recovery_wait_status is None else
                    diagnostics.recovery_wait_status.value
                ),
                "recovery_wait_capacity_exhausted":
                    diagnostics.recovery_wait_capacity_exhausted,
                "retry_progress_evidence": (
                    None if diagnostics.retry_progress_evidence is None else {
                        "kind": diagnostics.retry_progress_evidence.kind.value,
                        "observation_sequence":
                            diagnostics.retry_progress_evidence.observation_sequence,
                        "support": diagnostics.retry_progress_evidence.support,
                        "action_id": diagnostics.retry_progress_evidence.action_id,
                        "fact_id": diagnostics.retry_progress_evidence.fact_id,
                    }
                ),
                "risk_actions": tuple({
                    "action_id": action.action_id,
                    "state": action.state.value,
                    "expected_damage": action.expected_damage_points,
                    "authorized_limit": action.authorized_limit_points,
                    "available_at_reservation":
                        action.available_at_reservation_points,
                    "policy_revision": action.policy_revision,
                    "submitted_sequences": action.submitted_sequences,
                    "commit_kind": (None if action.commit_evidence is None else
                                    action.commit_evidence.kind.value),
                    "observed_damage": action.observed_damage_points,
                    "observed_damage_lower_bound":
                        action.observed_damage_lower_bound_points,
                    "health_evidence_complete":
                        action.health_evidence_complete,
                } for action in diagnostics.risk_actions),
                "session_state": diagnostics.state.value,
                "session_reason": diagnostics.reason,
                "driver_state": driver.state.value,
                "driver_reason": driver.reason,
                "runtime_failure": driver.last_runtime_failure,
                "body_handoff": (
                    None if diagnostics.handoff is None else {
                        "owner": diagnostics.handoff.owner_id,
                        "disposition": diagnostics.handoff.disposition.value,
                        "reason": diagnostics.handoff.reason,
                        "observation_sequence":
                            diagnostics.handoff.observation_sequence_id,
                        "movement_tick": diagnostics.handoff.movement_tick_id,
                        "movement": {
                            "forward": diagnostics.handoff.movement.forward,
                            "strafe": diagnostics.handoff.movement.strafe,
                            "jump": diagnostics.handoff.movement.jump,
                            "sneak": diagnostics.handoff.movement.sneak,
                            "sprint": diagnostics.handoff.movement.sprint,
                        },
                        "successor": diagnostics.handoff.successor_id,
                        "control_sequence": diagnostics.handoff.control_sequence,
                    }
                ),
            }
            trace.append(row)
            if trace_sink is not None:
                trace_sink(row)
            stable = backend.state.on_ground and all(
                abs(backend.state.velocity_blocks_per_tick[i]) < .002 for i in (0, 2))
            if released_ticks >= after_terminal_ticks and stable:
                break
        report = session.report
        driver_state = driver.state.value
    finally:
        session.close()
        runtime.close()
    outcome = {"complete": "success"}.get(report.state.value, report.state.value)
    if driver_state == "failed" and outcome != "failed":
        outcome = f"driver_failed/{outcome}"
    requirement = scenario.async_coverage
    if outcome == "success" and not requirement.no_async_work:
        requirement = replace(requirement, applied_kinds=tuple(dict.fromkeys(
            (*requirement.applied_kinds, AsyncWorkKind.PLANNING))))
    verification = monitor.finalize(requirement, (*planner.activity, *motion.activity,
                                                 *information_activity.values()))
    return Result(
        scenario.name, scenario.expect, outcome, str(report.reason), tick,
        tuple(round(v, 2) for v in backend.state.position), backend.damage_taken,
        monitor.violations,
        [f"{e.name}@{e.fired_at}" for e in events if e.fired_at is not None],
        tuple(sorted(monitor.coverage_gaps)), trace,
        tuple(
            (event.kind, event.fired_at)
            for event in events
            if event.kind is not None and event.fired_at is not None
        ),
        tuple(backend.dispatched_perturbations),
        tuple(backend.applied_perturbations),
        verification,
    )


# ----------------------------------------------------------------- scenes
STONE = "minecraft:stone"


def lane(columns: list[list], *, width: int = 1, extra=None) -> Scene:
    """columns[z] lists the solid y levels (int = stone, (y, SLAB_ID) = bottom slab)."""
    solids = {}
    for z, column in enumerate(columns):
        for x in range(-(width // 2), width - width // 2):
            for item in column:
                y, block = item if isinstance(item, tuple) else (item, STONE)
                solids[(x, y, z)] = block
    solids.update(extra or {})
    xs = [p[0] for p in solids]
    zs = [p[2] for p in solids]
    return Scene(solids, ((min(xs) - 3, max(xs) + 3), (52, 72), (min(zs) - 3, max(zs) + 3)))


def late_ticks(probability: float, seed: int, horizon: int = 600) -> frozenset[int]:
    rng = random.Random(seed)
    return frozenset(t for t in range(2, horizon) if rng.random() < probability)

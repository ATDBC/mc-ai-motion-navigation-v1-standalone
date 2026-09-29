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

from dataclasses import dataclass, field, replace
from pathlib import Path
import math
import random
from typing import Callable

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav import motion_worker, planner_worker
from mc2p.motion_nav.motion_risk import TaskDamageBudget, TaskRiskLedger
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.world_model import Aabb
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport
from mc2p.motion_nav.movement_transition import MovementMode
from tests.test_player_runtime import _RecordingTrace
from mc2p.motion_nav.runtime_adapter import TEST_ORACLE

from tests.sim.backend import CalculatorBackend, Perturbations, Scene
from tests.sim.monitor import InvariantMonitor, TickEvidence

CONFIG = Path("config/motion-navigation")
TERMINAL_DRIVER = {"success", "failed", "cancelled", "stopped", "interaction_required"}


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

    def is_alive(self) -> bool:
        return True

    def submit_surface_snapshot(self, snapshot, ground_profile, step_profile, request,
                                jump_profile=None, *, air_profiles=(), ground_mode_profile=None) -> bool:
        self._job = planner_worker._PlanningJob(None, snapshot, ground_profile, ground_mode_profile,
                                                step_profile, jump_profile, air_profiles, request)
        return True

    def poll_latest(self):
        job, self._job = self._job, None
        return None if job is None else planner_worker._execute_job(job)

    def close(self) -> None:
        self._job = None


class InlineMotionWorker:
    """Synchronous stand-in with MotionSolverWorker's interface."""

    pid = None

    def __init__(self, *_, **__):
        self._pending = []

    def is_alive(self) -> bool:
        return True

    def submit(self, job) -> bool:
        self._pending.append(job)
        return True

    def poll_available(self):
        done = tuple(motion_worker._execute_job(job) for job in self._pending)
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

    @property
    def diagnostics(self):
        return self.session.diagnostics


def seed_memory(runtime: PlayerRuntimeV1, scene: Scene) -> None:
    """Pre-load the Runtime-owned world with earlier, non-visual knowledge of the scene.

    Stands in for a map explored earlier: blocks and air are known, but no cell
    carries near lower-part visual evidence.
    """
    runtime.navigation_observation_adapter.seed_test_oracle_memory(
        TEST_ORACLE, {p: scene.geometry(b) for p, b in scene.solids.items()},
        scene.air_cells(),
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

    @property
    def verdict(self) -> str:
        ok = (self.outcome == self.expect) and not self.violations
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
            (int(row["retry_total_failures"]) for row in self.trace),
            default=0,
        )


def run(scenario: Scenario, *, after_terminal_ticks: int = 20,
        trace_sink: Callable[[dict], None] | None = None,
        event_ticks: dict[str, int] | None = None,
        control_step: Callable[[Context], tuple[str, ...] | None] | None = None,
        risk_ledger: TaskRiskLedger | None = None) -> Result:
    events = [replace(event, fired_at=None) for event in scenario.events]
    if event_ticks is not None:
        if set(event_ticks) != {event.name for event in events}:
            raise ValueError("frozen event names do not match scenario")
        events = [replace(event, when=(lambda context, predicate=event.when,
                                       at=event_ticks[event.name]:
                                       context.tick >= at and predicate(context)))
                  for event in events]
    clock = [100_000_000]
    backend = CalculatorBackend(clock, Scene(dict(scenario.scene.solids), scenario.scene.volume).with_floor(),
                                scenario.start, scenario.yaw_degrees,
                                perturbations=scenario.perturbations)
    if scenario.start_velocity_blocks_per_tick is not None:
        backend.state = replace(
            backend.state,
            velocity_blocks_per_tick=scenario.start_velocity_blocks_per_tick,
        )
    runtime = PlayerRuntimeV1(backend, _RecordingTrace(), lambda: clock[0])
    reset = runtime.reset(ResetRequestV0("reset-sim", backend.episode, "test", 1, 10_000_000_000))
    if not reset.succeeded:
        raise RuntimeError("reset failed")
    seed_memory(runtime, backend.scene)
    profiles = NavigationSessionProfiles.load(CONFIG)
    session = NavigationSession("sim", profiles, planner_worker=InlinePlannerWorker(),
                                motion_worker=InlineMotionWorker(),
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
        lambda: bool(session.diagnostics.controller_ids)
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
                 damage_budget=TaskDamageBudget(policy, scenario.damage_points))
    monitor = InvariantMonitor()
    context = Context(0, backend, session, driver, clock,
                      scenario.damage_points, policy, goal, scenario.goal)
    trace: list[dict] = []
    released_ticks = 0
    perturbations_stopped = False
    tick = 0
    try:
        for tick in range(1, scenario.max_ticks + 1):
            context.tick = tick
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
                    "success", "failed", "cancelled"}:
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
            frame_diagnostics = (driver.last_frame_diagnostics
                                 if len(backend.actions) > submitted_count_before else None)
            applied_request = (backend.applied_commands[-1][1]
                               if backend.applied_commands
                               and backend.applied_commands[-1][0] == backend.movement_tick
                               else None)
            applied_movement = backend.applied[-1]
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
            )
            monitor.check(evidence)
            row = {
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
                "controller_phase": diagnostics.controller_phase,
                "action_kind": diagnostics.action_kind,
                "action_index": diagnostics.action_index,
                "source_bound": diagnostics.source_bound and driver.source is not None,
                "request_generation": diagnostics.request_generation,
                "goal_revision": diagnostics.goal_revision,
                "route_id": diagnostics.route_id,
                "damage": backend.damage_taken,
                "damage_spent": diagnostics.damage_spent,
                "risk_available_points": diagnostics.risk_available_points,
                "risk_policy_revision": diagnostics.risk_policy_revision,
                "risk_submission_capacity_exhausted":
                    diagnostics.risk_submission_capacity_exhausted,
                "support_fraction": diagnostics.support_fraction,
                "transition_count": diagnostics.transition_count,
                "illegal_transition_count": diagnostics.illegal_transition_count,
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
                "driver_state": driver.state,
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
        driver_state = driver.state
    finally:
        session.close()
        runtime.close()
    outcome = {"complete": "success"}.get(report.state.value, report.state.value)
    if driver_state == "failed" and outcome != "failed":
        outcome = f"driver_failed/{outcome}"
    return Result(scenario.name, scenario.expect, outcome, str(report.reason), tick,
                  tuple(round(v, 2) for v in backend.state.position), backend.damage_taken,
                  monitor.violations,
                  [f"{e.name}@{e.fired_at}" for e in events if e.fired_at is not None],
                  tuple(sorted(monitor.coverage_gaps)), trace)


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

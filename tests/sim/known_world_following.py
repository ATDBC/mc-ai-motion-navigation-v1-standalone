"""F1-C formal known-world following scenarios and frozen metrics."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from functools import lru_cache
import math
from typing import Iterable

from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import ControlFrameProposalV1
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_request_v3 import ObservationRequestV3
from mc2p.contracts.observation_v2 import ObservationGroupV2
from mc2p.contracts.observation_v3 import TrackedEntityStateV3
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionProfiles
from mc2p.motion_nav.goal_observation import ObservedGoalStatus
from mc2p.motion_nav.world_model import Aabb
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.known_world_follow_driver import (
    FOLLOW_HOLD_DISTANCE_BLOCKS,
    KnownWorldFollowDriver,
    KnownWorldFollowState,
    KnownWorldFollowStatus,
)
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from tests.sim.backend import CalculatorBackend, Perturbations
from tests.sim.monitor import InvariantMonitor, TickEvidence
from tests.sim.product_metrics import RevisionResponseMode, revision_responses
from tests.sim.runner import (
    CONFIG, InlineMotionWorker, InlinePlannerWorker, lane, seed_memory,
)
from tests.test_player_runtime import _RecordingTrace, _task


SCHEMA_VERSION = "mc2p.f1-known-world-following.v2"
TARGET_TRACK_ID = "f1-visible-player"
TICK_SECONDS = .05
STABLE_WARMUP_TICKS = 40
MAX_STABLE_MEAN_LAG_BLOCKS = .75
MAX_STABLE_P95_LAG_BLOCKS = 1.5
MAX_PLANNING_SUBMISSIONS_PER_REVISION = 1.25
MAX_REVISION_RESPONSE_P95_TICKS = 5.0
SAFETY_VIOLATION_LIMIT = 0
INITIAL_TARGET_DISTANCE_BLOCKS = 4.0
_START = (.5, 64.0, .5)
_TARGET_START = (.5, 64.0, _START[2] + INITIAL_TARGET_DISTANCE_BLOCKS)


@dataclass(frozen=True, slots=True)
class FollowScenario:
    name: str
    speed_blocks_per_second: float
    path: str = "straight"
    move_ticks: int = 120
    pause_ticks: int = 0
    resume_ticks: int = 0
    final_hold_ticks: int = 160
    update_calls_per_tick: int = 1
    missing_observation_ticks: tuple[int, ...] = ()
    delay_first_input: bool = False
    cancel_tick: int | None = None
    in_scope_speed: bool = True
    initial_unknown_cells: tuple[tuple[int, int, int], ...] = ()

    @property
    def motion_end_tick(self) -> int:
        return self.move_ticks + self.pause_ticks + self.resume_ticks

    @property
    def scheduled_cancel_tick(self) -> int:
        return self.cancel_tick or self.motion_end_tick + self.final_hold_ticks

    @property
    def maximum_ticks(self) -> int:
        return self.scheduled_cancel_tick + 40

    def moving_tick_count(self, tick: int) -> int:
        first = min(max(tick, 0), self.move_ticks)
        if self.resume_ticks == 0:
            return first
        resumed = min(
            max(tick - self.move_ticks - self.pause_ticks, 0),
            self.resume_ticks,
        )
        return first + resumed

    def target_position(self, tick: int) -> tuple[float, float, float]:
        moving = self.moving_tick_count(tick)
        forward_speed = self.speed_blocks_per_second
        x = _TARGET_START[0]
        if self.path == "lateral":
            amplitude, period_ticks = .5, 120.0
            lateral_peak_speed = amplitude * 2.0 * math.pi / (
                period_ticks * TICK_SECONDS
            )
            forward_speed = math.sqrt(
                self.speed_blocks_per_second ** 2 - lateral_peak_speed ** 2
            )
            phase_tick = min(tick, self.move_ticks)
            x += amplitude * math.sin(2.0 * math.pi * phase_tick / period_ticks)
        z = _TARGET_START[2] + moving * forward_speed * TICK_SECONDS
        return x, _TARGET_START[1], z

    def target_velocity(self, tick: int) -> tuple[float, float, float]:
        if tick <= 0:
            return 0.0, 0.0, 0.0
        before, after = self.target_position(tick - 1), self.target_position(tick)
        return tuple(after[index] - before[index] for index in range(3))

    def stable_tick(self, tick: int) -> bool:
        if STABLE_WARMUP_TICKS <= tick <= self.move_ticks:
            return True
        resumed_at = self.move_ticks + self.pause_ticks
        return (self.resume_ticks > STABLE_WARMUP_TICKS
                and resumed_at + STABLE_WARMUP_TICKS <= tick
                    <= resumed_at + self.resume_ticks)


SCENARIOS = (
    FollowScenario("straight_2_0", 2.0),
    FollowScenario("straight_3_3", 3.3),
    FollowScenario("straight_4_0", 4.0),
    FollowScenario("lateral_3_3", 3.3, path="lateral"),
    FollowScenario(
        "move_stop_800_resume", 3.3,
        move_ticks=80, pause_ticks=800, resume_ticks=80,
    ),
    FollowScenario("high_frequency_throttle", .8),
    FollowScenario("normal_cancel", 3.3, final_hold_ticks=0, cancel_tick=100),
    FollowScenario(
        "one_missing_observation", 3.3,
        missing_observation_ticks=(60,),
    ),
    FollowScenario("first_input_late_one_tick", 3.3, delay_first_input=True),
    FollowScenario(
        "straight_5_0_out_of_scope", 5.0,
        final_hold_ticks=220, in_scope_speed=False,
    ),
)
SCENARIO_BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}
REPRESENTATIVE_SCENARIOS = (
    "straight_2_0", "move_stop_800_resume", "first_input_late_one_tick",
)


class FollowingBackend(CalculatorBackend):
    """Calculator world plus one legal visible and registered player fact."""

    def __init__(self, clock, scene, start, yaw_degrees=0.0, *,
                 scenario: FollowScenario):
        super().__init__(clock, scene, start, yaw_degrees,
                         perturbations=Perturbations())
        self.follow_scenario = scenario
        self.target_tick = 0
        self.target_motion_enabled = False
        self.requested_track_id: str | None = None
        self.first_delayed_input_tick: int | None = None

    def enable_target_motion(self) -> None:
        self.target_motion_enabled = True

    def advance(self, movement: MovementV1, look_yaw: float = 0.0,
                look_pitch: float = 0.0) -> None:
        super().advance(movement, look_yaw, look_pitch)
        if self.target_motion_enabled:
            self.target_tick += 1

    def _sample_command(self, action: ActionSnapshotV1 | None):
        if (action is not None and self.target_motion_enabled
                and self.follow_scenario.delay_first_input
                and self.first_delayed_input_tick is None
                and action.movement != MovementV1()):
            delayed_tick = self.movement_tick + 1
            self.perturbations.late_ticks = frozenset({delayed_tick})
            self.first_delayed_input_tick = delayed_tick
        return super()._sample_command(action)

    def step(self, action, deadline, *, observation_request=None):
        self.requested_track_id = (
            None if observation_request is None
            else observation_request.entity_track_id
        )
        return super().step(
            action, deadline, observation_request=observation_request,
        )

    def observation(self, *, request_sequence_id=None, air_positions=()):
        snapshot = super().observation(
            request_sequence_id=request_sequence_id,
            air_positions=air_positions,
        )
        perception = snapshot.perception.value
        own = snapshot.self_state.value
        assert perception is not None and own is not None
        target = self.follow_scenario.target_position(self.target_tick)
        target_velocity = self.follow_scenario.target_velocity(self.target_tick)
        observer_velocity = self.state.velocity_blocks_per_tick
        relative_velocity = tuple(
            target_velocity[index] - observer_velocity[index]
            for index in range(3)
        )
        own_position = (own.position.x, own.position.y, own.position.z)
        relative = Vec3V0(*(target[index] - own_position[index]
                            for index in range(3)))
        missing = self.target_tick in self.follow_scenario.missing_observation_ticks
        template = perception.visible_entities[0]
        visible = () if missing else (replace(
            template,
            track_id=TARGET_TRACK_ID,
            entity_type="minecraft:player",
            display_name="F1 target",
            relative_position=relative,
            relative_velocity=Vec3V0(*relative_velocity),
            bounding_box_size=Vec3V0(.6, 1.8, .6),
        ),)
        snapshot = replace(
            snapshot,
            perception=replace(
                snapshot.perception,
                value=replace(perception, visible_entities=visible),
            ),
        )
        if not missing and self.requested_track_id == TARGET_TRACK_ID:
            tracked = TrackedEntityStateV3(
                TARGET_TRACK_ID, "minecraft:player", relative,
                Vec3V0(*relative_velocity), 0.0, 0.0, Vec3V0(.6, 1.8, .6),
                "standing", True, True, False, 20.0, 20.0,
            )
            snapshot = replace(
                snapshot,
                tracked_entity=ObservationGroupV2.valid(
                    snapshot.world_time_ticks.value,
                    "client_registered_entity",
                    tracked,
                ),
            )
        return snapshot


def _follow_goal(position: tuple[float, float, float]) -> GoalState:
    half = FOLLOW_HOLD_DISTANCE_BLOCKS / math.sqrt(2.0)
    x, y, z = position
    return GoalState(
        Aabb(x - half, y - .1, z - half, x + half, y + .1, z + half),
        GoalSupport.SOLID, frozenset({MovementMode.WALK}),
        frozenset({"standing"}), .6,
    )


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * percentile
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return float(ordered[low])
    fraction = rank - low
    return ordered[low] * (1.0 - fraction) + ordered[high] * fraction


def _nearest_rank(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 6)


def _distance_summary(values: list[float]) -> dict:
    return {
        "sample_count": len(values),
        "mean_blocks": _rounded(sum(values) / len(values) if values else None),
        "p95_blocks": _rounded(_percentile(values, .95)),
        "max_blocks": _rounded(max(values) if values else None),
        "final_blocks": _rounded(values[-1] if values else None),
    }


def _horizontal_distance(first, second) -> float:
    return math.hypot(first[0] - second[0], first[2] - second[2])


def _tick_evidence(
    *, tick: int, clock: list[int], backend: FollowingBackend,
    session: NavigationSession, driver: RuntimeNavigationDriver,
    goal_state: GoalState, goal_position: tuple[float, float, float],
    frame_diagnostics, command_activities: dict,
) -> TickEvidence:
    diagnostics = session.diagnostics
    report = session.report
    applied_request = (
        backend.applied_commands[-1][1]
        if backend.applied_commands[-1][0] == backend.movement_tick else None
    )
    if (frame_diagnostics is not None
            and frame_diagnostics.action_request_sequence is not None):
        command_activities[frame_diagnostics.action_request_sequence] = (
            frame_diagnostics.movement_activity
        )
    applied_activity = command_activities.get(applied_request)
    return TickEvidence(
        tick=backend.movement_tick,
        now_ns=clock[0],
        position=backend.state.position,
        on_ground=backend.state.on_ground,
        controller_ids=diagnostics.controller_ids,
        source_bound=diagnostics.source_bound and driver.source is not None,
        state=diagnostics.state.value,
        reason=diagnostics.reason,
        damage=backend.damage_taken,
        damage_limit=0.0,
        committed_damage=diagnostics.damage_spent,
        request_generation=diagnostics.request_generation,
        goal_revision=diagnostics.goal_revision,
        wait_frames=diagnostics.information_wait_frames,
        applied_request=applied_request,
        applied_movement=backend.applied[-1],
        issued_commands=tuple(backend.submitted_commands),
        proposed_movement_intents=(
            () if frame_diagnostics is None
            else frame_diagnostics.proposed_movement_intents
        ),
        selected_intents=(
            () if frame_diagnostics is None else frame_diagnostics.selected_intents
        ),
        action_movement=(
            None if frame_diagnostics is None else frame_diagnostics.action_movement
        ),
        goal_position=goal_position,
        goal_state=goal_state,
        velocity=backend.state.velocity_blocks_per_tick,
        pose=backend.state.pose,
        yaw_radians=backend.state.yaw_radians,
        risk_policy_id=goal_state.risk_policy_id,
        action_index=diagnostics.action_index,
        retry_attempt_id=diagnostics.retry_last_failure_attempt_id,
        observation_sequence=backend.sequence,
        route_id=diagnostics.route_id,
        submitted_request=(
            None if frame_diagnostics is None
            else frame_diagnostics.action_request_sequence
        ),
        handoff=diagnostics.handoff,
        risk_actions=diagnostics.risk_actions,
        retry_round_failures=diagnostics.retry_round_failures,
        retry_total_failures=diagnostics.retry_total_failures,
        retry_cause_counts=tuple(
            (cause.value, count) for cause, count in diagnostics.retry_cause_counts
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
        planning_work_identity_valid=diagnostics.planning_work_identity_valid,
        planning_permit_identity_valid=diagnostics.planning_permit_identity_valid,
        planning_information_identity_valid=(
            diagnostics.planning_information_identity_valid
        ),
        async_work_diagnostics=session.async_work_diagnostics,
        active_motion_mailboxes=session.active_motion_mailboxes,
        applied_body_activity=applied_activity,
        recovery_budget_kind=diagnostics.recovery_budget_kind,
        recovery_maximum_starts=diagnostics.recovery_maximum_starts,
        recovery_window_starts=diagnostics.recovery_window_starts,
        recovery_total_starts=diagnostics.recovery_total_starts,
        recovery_limit_status=diagnostics.recovery_limit_status,
        recovery_limit_window_starts=diagnostics.recovery_limit_window_starts,
        reach_policy=report.reach_policy,
        observed_goal_status=report.observed_goal_status,
        body_control_activities=diagnostics.body_control_activities,
        recovery_wait_status=diagnostics.recovery_wait_status,
        session_state=report.state,
    )


@lru_cache(maxsize=None)
def run_scenario(scenario: FollowScenario) -> dict:
    return _run_scenario(scenario, _RecordingTrace())


def run_scenario_with_trace(
    scenario: FollowScenario,
    trace_writer,
    *,
    cancel_when=None,
    after_tick=None,
    control_path_started=None,
    control_path_finished=None,
) -> dict:
    if cancel_when is not None and not callable(cancel_when):
        raise TypeError("long-session cancel predicate must be callable")
    if after_tick is not None and not callable(after_tick):
        raise TypeError("long-session tick callback must be callable")
    if ((control_path_started is None) != (control_path_finished is None)
            or (control_path_started is not None
                and not callable(control_path_started))
            or (control_path_finished is not None
                and not callable(control_path_finished))):
        raise TypeError("long-session control-path callbacks must be callable")
    return _run_scenario(
        scenario, trace_writer,
        cancel_when=cancel_when, after_tick=after_tick,
        control_path_started=control_path_started,
        control_path_finished=control_path_finished,
    )


def _run_scenario(
    scenario: FollowScenario,
    trace_writer,
    *,
    cancel_when=None,
    after_tick=None,
    control_path_started=None,
    control_path_finished=None,
) -> dict:
    if type(scenario) is not FollowScenario:
        raise TypeError("F1-C requires a frozen FollowScenario")
    clock = [100_000_000]
    scene = lane([[63]] * 72, width=25)
    backend = FollowingBackend(clock, scene.with_floor(), _START,
                               scenario=scenario)
    runtime = PlayerRuntimeV1(backend, trace_writer, lambda: clock[0])
    planner = InlinePlannerWorker()
    motion = InlineMotionWorker()
    session = None
    try:
        reset = runtime.reset(ResetRequestV0(
            "f1-follow-reset", backend.episode, "test", 1, 20_000_000_000,
        ))
        if not reset.succeeded:
            raise RuntimeError("F1 following reset failed")
        if scenario.initial_unknown_cells:
            seed_memory(
                runtime,
                backend.scene,
                exclude=frozenset(scenario.initial_unknown_cells),
            )
        else:
            seed_memory(runtime, backend.scene)
        bootstrap_deadline = clock[0] + 500_000_000
        runtime.control_frame(
            _task(bootstrap_deadline), BehaviorProfileV0(), bootstrap_deadline,
            proposals=(ControlFrameProposalV1(
                observation_request=ObservationRequestV3(
                    "navigation_v1", entity_track_id=TARGET_TRACK_ID,
                ),
            ),),
        )
        profiles = NavigationSessionProfiles.load(CONFIG)
        session = NavigationSession(
            f"f1/{scenario.name}", profiles,
            planner_worker=planner, motion_worker=motion,
            clock_ns=lambda: clock[0],
        )
        driver = RuntimeNavigationDriver(
            runtime, session, clock_ns=lambda: clock[0],
            observation_request=ObservationRequestV3(
                "navigation_v1", entity_track_id=TARGET_TRACK_ID,
            ),
        )
        backend.set_external_perturbation_guard(lambda: driver.source is not None)
        backend.set_external_impulse_guard(lambda: session.has_owned_body_control)
        follow = KnownWorldFollowDriver(
            driver,
            task_id=f"f1-task/{scenario.name}",
            goal_id=f"f1-goal/{scenario.name}",
            target_track_id=TARGET_TRACK_ID,
        )
        started = follow.start(clock[0])
        if started.status is not KnownWorldFollowStatus.STARTED:
            raise RuntimeError(f"F1 following did not start: {started.status}")
        backend.enable_target_motion()

        monitor = InvariantMonitor()
        command_activities: dict = {}
        goal_position = _TARGET_START
        goal_state = _follow_goal(goal_position)
        accepted_revisions = 1
        rejected_revisions = 0
        throttled_revisions = 0
        unchanged_updates = 0
        duplicate_updates = 0
        fresh_observation_updates = 0
        observation_gaps = 0
        update_status_counts: dict[str, int] = {}
        response_frames: list[dict] = []
        measurement_start_tick = backend.movement_tick
        measurement_start_position = backend.state.position
        last_follow_observation_sequence = runtime.observation.sequence_id
        overall_excess_lags = [max(
            0.0, INITIAL_TARGET_DISTANCE_BLOCKS - FOLLOW_HOLD_DISTANCE_BLOCKS,
        )]
        stable_excess_lags: list[float] = []
        overall_raw_distances = [INITIAL_TARGET_DISTANCE_BLOCKS]
        stable_raw_distances: list[float] = []
        last_owner = ()
        controller_switches = 0
        zero_intervals: list[list[int]] = []
        zero_start: int | None = None
        recoveries = 0
        cancellation_requested = False
        observed_ticks = 0
        state_history: list[dict] = []
        last_state_reason = None
        revision_information_waits: list[dict] = []
        revision_information_movement_gap_ticks: list[int] = []
        revision_pre_control_states: list[dict] = []

        for tick in range(1, scenario.maximum_ticks + 1):
            observed_ticks = tick
            actions_before = len(backend.actions)
            position_before = backend.state.position
            request_movement_tick = backend.movement_tick
            revision_requests = []
            primary_follow_result = None
            pending_follow_result = None
            fresh_follow_observation = False
            benchmark_cancel = (
                cancel_when(tick) if cancel_when is not None else False
            )
            if control_path_started is not None:
                control_path_started(tick)

            should_cancel = (
                tick == scenario.scheduled_cancel_tick
                or benchmark_cancel
            )
            if should_cancel and not cancellation_requested:
                primary_follow_result = follow.cancel()
                cancellation_requested = True
            elif follow.state in {
                KnownWorldFollowState.FOLLOWING,
                KnownWorldFollowState.NEEDS_TARGET_OBSERVATION,
            } and runtime.observation.sequence_id != last_follow_observation_sequence:
                if scenario.update_calls_per_tick != 1:
                    raise AssertionError(
                        "F1-C only updates once for each fresh Runtime observation"
                    )
                last_follow_observation_sequence = runtime.observation.sequence_id
                fresh_follow_observation = True
                primary_follow_result = follow.update(clock[0])

            if follow.state is KnownWorldFollowState.STOPPING:
                pending_follow_result = follow.update(clock[0])

            revised_this_tick = (
                primary_follow_result is not None
                and primary_follow_result.status
                    is KnownWorldFollowStatus.REVISED
            )
            if revised_this_tick:
                revision_pre_control_states.append({
                    "tick": tick,
                    "revision": primary_follow_result.revision,
                    "state": session.report.state.value,
                    "terminal": session.report.terminal,
                    "missing_cell_count": len(session.report.missing_cells),
                    "incumbent_route_id": (
                        session.diagnostics.incumbent_route_id
                    ),
                })

            deadline_ns = None
            if driver.source is not None:
                deadline_ns = clock[0] + 500_000_000
                driver.tick(BehaviorProfileV0(), deadline_ns)
            else:
                backend.free_tick()
            if control_path_finished is not None:
                control_path_finished(tick, deadline_ns, clock[0])

            if fresh_follow_observation:
                fresh_observation_updates += 1
            if primary_follow_result is not None:
                status = primary_follow_result.status
                update_status_counts[status.value] = (
                    update_status_counts.get(status.value, 0) + 1
                )
                if status is KnownWorldFollowStatus.REVISED:
                    accepted_revisions += 1
                    assert (primary_follow_result.submitted_target_position
                            is not None)
                    submitted = primary_follow_result.submitted_target_position
                    goal_position = (submitted.x, submitted.y, submitted.z)
                    goal_state = _follow_goal(goal_position)
                    revision_requests.append({
                        "revision": primary_follow_result.revision,
                        "movement_tick": request_movement_tick,
                    })
                elif status is KnownWorldFollowStatus.THROTTLED:
                    throttled_revisions += 1
                elif status is KnownWorldFollowStatus.UNCHANGED:
                    unchanged_updates += 1
                elif status is KnownWorldFollowStatus.DUPLICATE_OBSERVATION:
                    duplicate_updates += 1
                elif status in {
                    KnownWorldFollowStatus.NEEDS_TARGET_OBSERVATION,
                    KnownWorldFollowStatus.OBSERVATION_UNAVAILABLE,
                }:
                    observation_gaps += 1
                elif status in {
                    KnownWorldFollowStatus.NAVIGATION_ENDED,
                    KnownWorldFollowStatus.TARGET_INVALID,
                    KnownWorldFollowStatus.TARGET_DEAD,
                    KnownWorldFollowStatus.SESSION_CHANGED,
                    KnownWorldFollowStatus.OBSERVATION_REWRITTEN,
                }:
                    rejected_revisions += 1
            if pending_follow_result is not None:
                status = pending_follow_result.status
                update_status_counts[status.value] = (
                    update_status_counts.get(status.value, 0) + 1
                )

            frame_diagnostics = (
                driver.last_frame_diagnostics
                if len(backend.actions) > actions_before else None
            )
            monitor.check(_tick_evidence(
                tick=tick, clock=clock, backend=backend,
                session=session, driver=driver,
                goal_state=goal_state, goal_position=goal_position,
                frame_diagnostics=frame_diagnostics,
                command_activities=command_activities,
            ))
            diagnostics = session.diagnostics
            information_wait_with_incumbent = (
                bool(session.report.missing_cells)
                and session.body_route_snapshot is not None
            )
            if revised_this_tick and information_wait_with_incumbent:
                revision_information_waits.append({
                    "tick": tick,
                    "revision": primary_follow_result.revision,
                    "missing_cell_count": len(session.report.missing_cells),
                })
            state_reason = (session.report.state.value, session.report.reason)
            if state_reason != last_state_reason:
                state_history.append({
                    "tick": tick,
                    "state": state_reason[0],
                    "reason": state_reason[1],
                    "goal_revision": session.report.goal_revision,
                })
                last_state_reason = state_reason
            recoveries = max(recoveries, diagnostics.recovery_total_starts)
            owner = tuple(
                (item, diagnostics.route_id if item == "route_executor" else None)
                for item in diagnostics.controller_ids
            )
            if owner and last_owner and owner != last_owner:
                controller_switches += 1
            if owner:
                last_owner = owner

            position_after = backend.state.position
            moved = _horizontal_distance(position_after, position_before)
            target_after = scenario.target_position(backend.target_tick)
            applied_movement = backend.applied[-1]
            if (information_wait_with_incumbent
                    and applied_movement == MovementV1()):
                revision_information_movement_gap_ticks.append(tick)
            response_frames.append({
                "movement_tick": backend.movement_tick,
                "position": position_after,
                "driver_state": driver.state,
                "source_bound": driver.source is not None,
                "goal_satisfied": (
                    session.report.observed_goal_status
                    is ObservedGoalStatus.SATISFIED
                ),
                "goal_revision": session.report.goal_revision,
                "goal_revision_requests": revision_requests,
                "goal_position": goal_position,
                "applied_movement": asdict(applied_movement),
            })

            if moved <= .005 and driver.source is not None:
                if zero_start is None:
                    zero_start = tick
            elif zero_start is not None:
                if tick - zero_start >= 3:
                    zero_intervals.append([zero_start, tick - 1])
                zero_start = None

            raw_distance = _horizontal_distance(position_after, target_after)
            lag = max(0.0, raw_distance - FOLLOW_HOLD_DISTANCE_BLOCKS)
            overall_raw_distances.append(raw_distance)
            overall_excess_lags.append(lag)
            if scenario.stable_tick(backend.target_tick):
                stable_raw_distances.append(raw_distance)
                stable_excess_lags.append(lag)
            if after_tick is not None:
                after_tick(tick)
            if cancellation_requested and driver.source is None:
                break

        if zero_start is not None and observed_ticks - zero_start + 1 >= 3:
            zero_intervals.append([zero_start, observed_ticks])

        product_responses = revision_responses(
            response_frames,
            start_tick=measurement_start_tick,
            start_position=measurement_start_position,
            mode=(RevisionResponseMode
                  .MOVEMENT_OR_MATCHING_SATISFACTION),
        )
        response_details = [
            {**item, "evidence": item["end"], "end": (
                "effective" if item["end"] in {"movement", "satisfied"}
                else item["end"]
            )}
            for item in product_responses
        ]
        response_outcomes = {
            end: sum(item["end"] == end for item in response_details)
            for end in ("effective", "superseded", "unanswered")
        }

        planning_submissions = sum(
            item.operation == "submit" for item in planner.activity
        )
        planning_ratio = planning_submissions / accepted_revisions
        response_values = [
            item["response_ticks"] for item in response_details
            if item["end"] == "effective"
        ]
        unanswered = response_outcomes["unanswered"]
        response_p95 = _nearest_rank(response_values, .95)
        violations = [list(item) for item in monitor.violations]
        overall_raw = _distance_summary(overall_raw_distances)
        stable_raw = _distance_summary(stable_raw_distances)
        overall_excess = _distance_summary(overall_excess_lags)
        stable_excess = _distance_summary(stable_excess_lags)
        final_raw = overall_raw_distances[-1]
        final_lag = overall_excess_lags[-1]
        capability_gates_apply = scenario.cancel_tick is None
        stable_gate = (
            None if (not capability_gates_apply
                     or not scenario.in_scope_speed) else bool(
                stable_excess["mean_blocks"] is not None
                and stable_excess["p95_blocks"] is not None
                and stable_excess["mean_blocks"] <= MAX_STABLE_MEAN_LAG_BLOCKS
                and stable_excess["p95_blocks"] <= MAX_STABLE_P95_LAG_BLOCKS
            )
        )
        final_hold_required = scenario.cancel_tick is None
        gates = {
            "stable_lag": stable_gate,
            "final_within_hold": (
                None if not final_hold_required
                else final_raw <= FOLLOW_HOLD_DISTANCE_BLOCKS + 1.0e-9
            ),
            "planning_ratio": (
                None if not capability_gates_apply else
                planning_ratio <= MAX_PLANNING_SUBMISSIONS_PER_REVISION
            ),
            "safety": len(violations) == SAFETY_VIOLATION_LIMIT,
            "revision_response": (
                None if not capability_gates_apply else
                unanswered == 0 and response_p95 is not None
                and response_p95 <= MAX_REVISION_RESPONSE_P95_TICKS
            ),
            "terminal": (
                session.report.state.value == "cancelled"
                and driver.source is None
            ),
        }
        applicable = [value for value in gates.values() if value is not None]
        return {
            "schema_version": SCHEMA_VERSION,
            "scenario": scenario.name,
            "formal_chain": (
                "PlayerRuntimeV1->RuntimeNavigationDriver->"
                "KnownWorldFollowDriver"
            ),
            "configuration": asdict(scenario),
            "target_speed_semantics": (
                "maximum horizontal speed with sinusoidal lateral drift"
                if scenario.path == "lateral" else
                "constant straight-line speed during moving phases"
            ),
            "excess_lag_definition": (
                "max(0,horizontal_target_distance-2.5_hold_distance)"
            ),
            "distance_sampling_windows": {
                "overall": {
                    "definition": (
                        "initial pre-control distance through the final "
                        "observed movement tick"
                    ),
                    "includes_initial_distance": True,
                    "sample_count": len(overall_raw_distances),
                },
                "stable": {
                    "definition": (
                        "moving target phases after a 40-tick warmup; "
                        "pause and final hold samples excluded"
                    ),
                    "includes_initial_distance": False,
                    "sample_count": len(stable_raw_distances),
                },
            },
            "initial_raw_distance_blocks": INITIAL_TARGET_DISTANCE_BLOCKS,
            "overall_raw_distance": overall_raw,
            "stable_raw_distance": stable_raw,
            "overall_excess_lag": overall_excess,
            "stable_excess_lag": stable_excess,
            "final_raw_distance_blocks": _rounded(final_raw),
            "final_excess_lag_blocks": _rounded(final_lag),
            "revision_response_details": response_details,
            "revision_response_outcomes": response_outcomes,
            "revision_response_p95_ticks": response_p95,
            "unanswered_revisions": unanswered,
            "accepted_revisions": accepted_revisions,
            "rejected_revisions": rejected_revisions,
            "throttled_revisions": throttled_revisions,
            "unchanged_updates": unchanged_updates,
            "duplicate_updates": duplicate_updates,
            "fresh_observation_updates": fresh_observation_updates,
            "observation_gap_updates": observation_gaps,
            "update_status_counts": dict(sorted(update_status_counts.items())),
            "planning_submissions": planning_submissions,
            "planning_submissions_per_accepted_revision": _rounded(planning_ratio),
            "task_recoveries": recoveries,
            "controller_switches": controller_switches,
            "zero_displacement_intervals": zero_intervals,
            "zero_displacement_ticks": sum(
                end - begin + 1 for begin, end in zero_intervals
            ),
            "i4_violations": sum(item[1] == "I4" for item in violations),
            "safety_violations": violations,
            "terminal_session_state": session.report.state.value,
            "terminal_session_reason": session.report.reason,
            "terminal_driver_state": driver.state,
            "terminal_driver_reason": driver.reason,
            "source_released": driver.source is None,
            "first_delayed_input_tick": backend.first_delayed_input_tick,
            "applied_perturbations": [list(item)
                                      for item in backend.applied_perturbations],
            "observed_ticks": observed_ticks,
            "state_history": state_history,
            "revision_information_waits": revision_information_waits,
            "revision_information_movement_gap_ticks": (
                revision_information_movement_gap_ticks
            ),
            "revision_pre_control_states": revision_pre_control_states,
            "gates": gates,
            "passed": all(applicable),
        }
    finally:
        if session is not None:
            session.close()
        runtime.close()


def run_manifest(names: Iterable[str] | None = None) -> dict:
    selected = tuple(SCENARIO_BY_NAME[name] for name in names) if names else SCENARIOS
    results = [run_scenario(scenario) for scenario in selected]
    failures = [result["scenario"] for result in results if not result["passed"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "thresholds": {
            "stable_mean_lag_blocks_max": MAX_STABLE_MEAN_LAG_BLOCKS,
            "stable_p95_lag_blocks_max": MAX_STABLE_P95_LAG_BLOCKS,
            "planning_submissions_per_revision_max":
                MAX_PLANNING_SUBMISSIONS_PER_REVISION,
            "revision_response_p95_ticks_max":
                MAX_REVISION_RESPONSE_P95_TICKS,
            "safety_violations_max": SAFETY_VIOLATION_LIMIT,
            "hold_distance_blocks": FOLLOW_HOLD_DISTANCE_BLOCKS,
        },
        "scenario_order": [scenario.name for scenario in selected],
        "results": results,
        "failed_scenarios": failures,
        "f1_c_complete": not failures,
    }

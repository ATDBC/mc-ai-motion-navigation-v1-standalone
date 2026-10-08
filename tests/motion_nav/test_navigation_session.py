from __future__ import annotations

from dataclasses import replace
from mc2p.motion_nav.async_work import ComputationInvalidationCause
import math
from pathlib import Path
import time
import unittest
from unittest.mock import Mock

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.action_v1 import ActionSnapshotV1, LookV1, MovementV1
from mc2p.contracts.intent_source import IntentSourceV1
from mc2p.contracts.observation_v3 import AirQueryResultV3
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SnapshotBuildStatus,
    SurfacePlanningRequest,
    SurfacePlanningStatus, SurfaceSearchNeed, surface_search_need,
    plan_known_surface_snapshot,
)
from mc2p.motion_nav.movement_transition import (
    GoalState,
    GoalSupport,
    MovementMode,
)
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger, MotionTickPhase, StateAnchor,
)
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from mc2p.motion_nav.goal_observation import (
    ObservedGoalStatus, evaluate_observed_goal,
)
from mc2p.motion_nav.motion_residual import (
    MotionResidualResult, MotionResidualStatus,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget, TaskRiskLedger
from mc2p.motion_nav.motion_risk import RiskCommitEvidence, RiskCommitKind
from mc2p.motion_nav.retry_ledger import RetryLedger, WaitPolicy
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.navigation_session import (
    NavigationSession as _ProductionNavigationSession,
    NavigationSessionProfiles,
    NavigationSessionState,
    information_look_for_missing_cells,
    information_probe_movement,
)
from mc2p.motion_nav.motion_worker import (
    MotionWorkerCancelStatus, MotionWorkerHealth, MotionWorkerReadiness,
    _execute_job,
)
from mc2p.motion_nav.navigation_lifecycle import NavigationTransitionAction
from mc2p.motion_nav.landing_edge_probe import (
    LandingEdgeProbe, LandingEdgeProbeState,
)
from mc2p.motion_nav.action_route_executor import (
    ActionRouteDecision, ActionRouteExecutor, ActionRouteState,
)
from mc2p.motion_nav.action_route import ActionRoute, ControlledDropSegment
from mc2p.motion_nav.controlled_drop import ControlledDropEdge
from mc2p.motion_nav.route_admission import (
    ActiveRoute, AdmissionReason, ExecutableCorridor,
)
from mc2p.motion_nav.route_body_controller import RouteControl
from mc2p.motion_nav.support_surfaces import (
    HorizontalRegion, SupportSurface, SurfaceNodeId, query_support_surfaces,
)
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, VisualAirEvidence,
    WorldKnowledge, WorldSessionId,
)
from tests.motion_nav.test_b07_step_route import (
    frame,
    step_profile,
)
from tests.motion_nav.test_b07_support_surfaces import surface_world
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_b09_air_transitions import air_profile
from tests.motion_nav.test_b10_gap_solver import fixture as gap_fixture
from tests.motion_nav.test_b10_online_motion import state as physics_state
from tests.observation_v3_fixtures import valid_snapshot_v3


class _InlineMotionWorker:
    """Explicit standalone-test port; production Session never creates it."""

    def __init__(self):
        self.pending = []

    def submit(self, job):
        self.pending.append(job)
        return True

    def poll_available(self):
        values = tuple(_execute_job(job) for job in self.pending)
        self.pending.clear()
        return values

    def cancel(self, identity, _status=None):
        return MotionWorkerCancelStatus.ACCEPTED

    def close(self):
        self.pending.clear()

    def is_alive(self):
        return True

    @property
    def health(self):
        return MotionWorkerHealth(
            MotionWorkerReadiness.READY, None, 0, None, None,
        )


def NavigationSession(*args, **kwargs):
    """Construct a standalone Session with an explicitly supplied test port."""
    kwargs.setdefault("motion_worker", _InlineMotionWorker())
    return _ProductionNavigationSession(*args, **kwargs)


class _InlinePlanner:
    def __init__(self, *, hold_first: bool = False) -> None:
        self.jobs = []
        self.hold_first = hold_first
        self.closed = False
        self.polls = 0

    def submit_surface_snapshot(
        self,
        snapshot,
        ground_profile,
        step,
        request,
        jump,
        *,
        air_profiles=(),
        ground_mode_profile=None,
    ) -> bool:
        self.jobs.append((
            snapshot, ground_profile, step, request, jump,
            air_profiles, ground_mode_profile,
        ))
        return True

    def poll_latest(self):
        self.polls += 1
        if not self.jobs or (self.hold_first and len(self.jobs) == 1):
            return None
        (snapshot, ground, step, request, jump,
         air_profiles, ground_mode) = self.jobs.pop(0)
        return plan_known_surface_snapshot(
            snapshot, ground, step, request, jump,
            air_profiles=air_profiles,
            ground_mode_profile=ground_mode,
        )

    def poll_available(self):
        candidate = self.poll_latest()
        return () if candidate is None else (candidate,)

    def is_alive(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class _DelayedCancelExecutor(ActionRouteExecutor):
    """Models an airborne owner that needs one more frame to land."""

    def __init__(self) -> None:
        self.cancel_requested = False
        self.action_index = 0
        self.decisions = 0
        self.state = ActionRouteState.RUNNING

    def cancel(self) -> None:
        self.cancel_requested = True

    def decide(self, frame, **_):
        self.decisions += 1
        if self.decisions == 1:
            self.state = ActionRouteState.CANCELLING
            return ActionRouteDecision(
                ActionRouteState.CANCELLING, MovementV1(forward=1), None,
                1, 0, "landing_after_cancel", (), 0,
            )
        self.state = ActionRouteState.CANCELLED
        return ActionRouteDecision(
            ActionRouteState.CANCELLED, MovementV1(), None,
            1, 0, "cancelled_after_landing", (), 0,
        )


class _RepeatingRecoveryExecutor(ActionRouteExecutor):
    def __init__(self, state: ActionRouteState, reason: str) -> None:
        self.state = state
        self.reason = reason
        self.route = None
        self.action_index = 0

    def cancel(self) -> None:
        pass

    def requires_safe_handoff(self, _frame) -> bool:
        return False

    def decide(self, _frame, **_):
        return ActionRouteDecision(
            self.state, MovementV1(), None,
            1, 0, self.reason, (), 0,
        )


class _ObservationRouteExecutor(ActionRouteExecutor):
    """Minimal active route owner for observation-request contract tests."""

    def __init__(self, route: ActionRoute) -> None:
        self.route = route
        self.action_index = 0
        self.state = ActionRouteState.RUNNING


class _WaitingRouteExecutor(_RepeatingRecoveryExecutor):
    def __init__(self) -> None:
        super().__init__(ActionRouteState.RUNNING, "awaiting_verified_motion")

    def decide(self, _frame, **_):
        return ActionRouteDecision(
            ActionRouteState.RUNNING, MovementV1(), None,
            1, 0, self.reason, (), 0, submit_input=False,
        )


def _replace_route_executor(session: NavigationSession,
                            executor: ActionRouteExecutor) -> None:
    control = session._supervisor.incumbent_route
    assert control is not None
    executor.route = control.route.action_route
    session._supervisor._route = replace(
        control, executor=executor, coordinator=None,
    )


def _ground_anchor(current: NavigationFrame) -> StateAnchor:
    tick = current.body.sequence_id
    state = replace(
        physics_state(movement_tick_id=tick),
        session=current.session,
        position=current.body.position,
        velocity_blocks_per_tick=(
            current.body.velocity_blocks_per_second[0] / 20.0,
            -.0784000015258789 if current.body.is_on_ground else
            current.body.velocity_blocks_per_second[1] / 20.0,
            current.body.velocity_blocks_per_second[2] / 20.0,
        ),
        # A grounded 1.21 sample still carries gravity's small downward
        # velocity. Zero would make the calculator falsely infer airborne.
        vertical_collision=True,
        yaw_radians=current.body.yaw_radians,
        pitch_radians=current.body.pitch_radians,
        pose=current.body.pose,
        on_ground=current.body.is_on_ground,
        sneaking=current.body.is_sneaking,
    )
    return StateAnchor(
        current.session, tick, tick, MotionTickPhase.AFTER_MOVEMENT,
        None, None, JAVA_1_21_RULESET.ruleset_id,
        JAVA_1_21_RULESET.state_schema,
        "mc2p.input-projection.v1", state,
    )


def _goal(position: tuple[float, float, float]) -> GoalState:
    x, y, z = position
    return GoalState(
        Aabb(x - .05, y - .05, z - .05, x + .05, y + .05, z + .05),
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _nodes(world, columns: tuple[int, ...]):
    result = []
    for x in columns:
        queried = query_support_surfaces(world.view(), x, 0, 1, 1)
        assert queried.surfaces
        result.append(queried.surfaces[0])
    return tuple(result)


def _gap_session(*, session_id: str = "gap-session"):
    anchor, _, _, _ = gap_fixture()
    knowledge = WorldKnowledge(anchor.session)
    known = ObservationStamp(anchor.session, 1, 1, "test", 1)
    knowledge.confirm_air(known, tuple(
        (x, y, z)
        for x in range(-2, 3)
        for y in range(60, 71)
        for z in range(-2, 5)
    ))
    knowledge.observe_blocks(known, {
        (0, 63, 0): BlockGeometry.full_cube("minecraft:grass_block"),
        (0, 63, 2): BlockGeometry.full_cube("minecraft:grass_block"),
    })
    world = knowledge.view()
    goal = query_support_surfaces(world, 0, 2, 64, 64).surfaces[0]
    state = anchor.physics_state
    x, y, z = state.position
    body = BodyState(
        state.session, anchor.observation_sequence_id,
        ObservationStamp(
            state.session, anchor.observation_sequence_id,
            anchor.movement_tick_id, "test", 1_000_000_000,
        ),
        state.position,
        tuple(value * 20.0 for value in state.velocity_blocks_per_tick),
        state.yaw_radians, state.pitch_radians, state.pose,
        Aabb(
            x - state.body_width / 2, y, z - state.body_width / 2,
            x + state.body_width / 2, y + state.body_height,
            z + state.body_width / 2,
        ),
        state.on_ground, state.horizontal_collision, state.vertical_collision,
        is_sprinting=state.sprinting, is_sneaking=state.sneaking,
        food_points=state.food_points,
        saturation_points=state.saturation_points,
    )
    current = NavigationFrame(state.session, body, world, "fabric")
    profiles = NavigationSessionProfiles(
        replace(
            ordinary_profile(),
            support_materials=frozenset({"minecraft:grass_block"}),
        ),
        jump_profile(), step_profile(),
        air=(air_profile(MovementMode.JUMP_GAP),),
    )
    session = NavigationSession(
        session_id, profiles, planner_worker=_InlinePlanner(),
        clock_ns=lambda: 1_000_000_000,
    )
    session.bind_source(_source())
    session.start_goal("gap-goal", 1, _goal(goal.position), current)
    return session, current, anchor


def _drive_until_verified_command(session, current, anchor):
    ledger = InputApplicationLedger(max_records=64)
    deadline = time.perf_counter() + 5.0
    saw_motion_wait = False
    while time.perf_counter() < deadline:
        proposal = session.propose(
            current, anchor, 2_000_000_000, input_ledger=ledger,
        )
        decision = proposal.route_decision
        if decision is not None and decision.reason_code == "awaiting_verified_motion":
            saw_motion_wait = True
        if (decision is not None and decision.submit_input
                and decision.verified_command_index is not None):
            return proposal, ledger, saw_motion_wait, current, anchor
        if decision is not None and decision.submit_input:
            from tests.motion_nav.preparation_fixture import apply_tick
            from mc2p.motion_nav.physics_adapter import PhysicsWorldView
            anchor = apply_tick(anchor, PhysicsWorldView(current.world, JAVA_1_21_RULESET), ledger,
                                decision.movement, 100 + anchor.movement_tick_id)
            state = anchor.physics_state
            current = replace(current, body=replace(
                current.body, sequence_id=anchor.observation_sequence_id,
                stamp=ObservationStamp(state.session, anchor.observation_sequence_id,
                    anchor.movement_tick_id, "test", 1_000_000_000),
                position=state.position, body_box=state.body_box,
                velocity_blocks_per_second=tuple(value * 20 for value in state.velocity_blocks_per_tick),
                is_on_ground=state.on_ground, horizontal_collision=state.horizontal_collision,
                vertical_collision=state.vertical_collision))
        time.sleep(.01)
    raise AssertionError("verified jump command was not submitted")


def _source() -> IntentSourceV1:
    return IntentSourceV1(
        "0" * 32, "episode", 0, 1,
        "ordered/" + "0" * 32 + "/0/1",
    )


def _known_world(blocks):
    session = WorldSessionId("navigation-session-world")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "test-clock", 1)
    xs = tuple(x for x, _, _ in blocks)
    zs = tuple(z for _, _, z in blocks)
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(min(xs) - 2, max(xs) + 3)
        for y in range(-3, 7)
        for z in range(min(zs) - 2, max(zs) + 3)
    ))
    world.observe_blocks(stamp, blocks)
    return world


def _known_corridor_with_one_irrelevant_unknown():
    """A complete straight corridor with one unknown cell outside the route."""
    session = WorldSessionId("navigation-session-partial-world")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "test-clock", 1)
    unknown = (-2, -1, -1)
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(-2, 3)
        for y in range(-1, 3)
        for z in range(-1, 2)
        if (x, y, z) != unknown
    ))
    world.observe_blocks(stamp, {
        (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
        for x in range(-1, 2)
    })
    return world, unknown


def _known_endpoints_with_unknown_gap():
    session = WorldSessionId("navigation-session-unknown-gap")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "test-clock", 1)
    unknown = (0, 0, 0)
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(-2, 3)
        for y in range(-1, 3)
        for z in range(-1, 2)
        if (x, y, z) != unknown
    ))
    world.observe_blocks(stamp, {
        (-1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
    })
    return world, unknown


def _known_endpoints_with_external_unknown_and_unknown_gap():
    session = WorldSessionId("navigation-session-targeted-unknown-gap")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "test-clock", 1)
    external = (-2, -1, -1)
    gap = (0, 0, 0)
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(-2, 3)
        for y in range(-1, 3)
        for z in range(-1, 2)
        if (x, y, z) not in {external, gap}
    ))
    world.observe_blocks(stamp, {
        (-1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
    })
    return world, external, gap


class NavigationSessionTests(unittest.TestCase):
    def profiles(self) -> NavigationSessionProfiles:
        return NavigationSessionProfiles(
            ordinary_profile(), jump_profile(), step_profile(),
        )

    def test_same_support_planner_has_typed_no_search_result(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        node = _nodes(world, (0,))[0]
        request = SurfacePlanningRequest(
            1, "same-support-request", "same-support-goal", 1,
            world.session.value, node.node_id, node.node_id,
            goal_state=_goal(node.position),
        )
        bounds = KnownMapBounds(0, 0, 1, 1, 0, 0, True)
        progress = KnownMapSnapshotBuilder(world.view(), bounds).advance(
            world.view(), 1000,
        )
        self.assertIs(progress.status, SnapshotBuildStatus.COMPLETE)
        self.assertIs(surface_search_need(request),
                      SurfaceSearchNeed.SAME_SUPPORT_LOCAL_GOAL)
        result = plan_known_surface_snapshot(
            progress.snapshot, ordinary_profile(), step_profile(), request,
        )
        self.assertIs(result.status,
                      SurfacePlanningStatus.NO_GRAPH_SEARCH_NEEDED)
        self.assertEqual(result.segments, ())
        self.assertIsNone(result.total_cost_seconds)

    def test_same_support_goal_waits_for_previous_input_receipt(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        node = _nodes(world, (0,))[0]
        initial = frame(world, 0, node.position)
        session = NavigationSession(
            "same-support-pending", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal("goal", 1, _goal(node.position), initial)
        ledger = InputApplicationLedger()
        ledger.submit(world.session, ActionSnapshotV1(
            "episode", 1, 0, 2_000_000_000,
            movement=MovementV1(forward=1), valid_for_ticks=1,
        ), requested_first_tick=1)
        pending = session.propose(
            initial, None, 2_000_000_000, input_ledger=ledger,
        )
        self.assertIs(pending.report.state, NavigationSessionState.EXECUTING)
        self.assertEqual(pending.report.reason, "waiting_for_previous_input")
        self.assertIsNone(session.active_route)
        ledger.expire(1, at_tick=2)
        complete = session.propose(
            frame(world, 1, node.position), None, 2_000_000_000,
            input_ledger=ledger,
        )
        self.assertIs(complete.report.state, NavigationSessionState.COMPLETE)

    def test_completed_session_rejects_goal_revision_without_mutation(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        node = _nodes(world, (0,))[0]
        initial = frame(world, 0, node.position)
        session = NavigationSession(
            "complete-rejects-revision", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal("goal", 1, _goal(node.position), initial)
        completed = session.propose(initial, None, 2_000_000_000)
        self.assertIs(completed.report.state, NavigationSessionState.COMPLETE)
        before = session.report

        accepted = session.update_goal("goal", 2, _goal(node.position))

        self.assertFalse(accepted)
        self.assertEqual(session.report, before)

    def test_replan_after_reaching_goal_support_skips_graph_search(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, goal = _nodes(world, (0, 1))
        initial = frame(world, 0, start.position)
        planner = _InlinePlanner()
        session = NavigationSession(
            "replan-on-goal", self.profiles(), planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "far-request", "goal", 1, world.session.value,
            start.node_id, goal.node_id, goal_state=_goal(goal.position),
        ), initial)
        arrived = frame(world, 1, goal.position)
        session.observe(arrived, ())
        session._reissue_request_from_current(arrived, "arrived_and_replanned",
            computation_invalidation=ComputationInvalidationCause.NEW_STATE_ANCHOR)
        result = session.propose(arrived, None, 2_000_000_000)
        self.assertIs(result.report.state, NavigationSessionState.COMPLETE)
        self.assertFalse(planner.jobs)

    def test_cancelled_planner_result_cannot_restore_old_route(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, goal = _nodes(world, (0, 1))
        initial = frame(world, 0, start.position)
        planner = _InlinePlanner(hold_first=True)
        session = NavigationSession(
            "cancel-old-worker", self.profiles(), planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "old-request", "goal", 1, world.session.value,
            start.node_id, goal.node_id, goal_state=_goal(goal.position),
        ), initial)
        session.propose(initial, None, 2_000_000_000)
        self.assertTrue(planner.jobs)
        session.cancel("cancel_before_result")
        planner.hold_first = False
        cancelled = session.propose(
            frame(world, 1, start.position), None, 2_000_000_000,
        )
        self.assertIs(cancelled.report.state, NavigationSessionState.CANCELLED)
        self.assertIsNone(session.active_route)

    def test_unknown_support_cannot_complete_even_with_any_goal_support(self):
        session_id = WorldSessionId("unknown-direct-support")
        world = WorldKnowledge(session_id)
        stamp = ObservationStamp(session_id, 1, 1, "test-clock", 1)
        world.confirm_air(stamp, tuple(
            (x, y, z) for x in range(-1, 2)
            for y in range(-1, 4) for z in range(-1, 2)
            if (x, y, z) != (0, 0, 0)
        ))
        initial = frame(world, 0, (.5, 1.0, .5))
        goal = replace(_goal((.5, 1.0, .5)), support=GoalSupport.ANY)
        observed = evaluate_observed_goal(
            initial, goal, "no_expected_damage",
        )
        self.assertIs(observed.status, ObservedGoalStatus.NEEDS_INFORMATION)
        navigation = NavigationSession(
            "unknown-goal-support", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        navigation.start_goal("goal", 1, goal, initial)
        self.assertIs(navigation.report.state,
                      NavigationSessionState.NEEDS_INFORMATION)

    def test_goal_waits_when_current_velocity_will_leave_region_next_tick(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        goal = GoalState(
            Aabb(.3, .95, .3, .7, 1.05, .7),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        moving = frame(
            world, 1, (.5, 1.0, .697), velocity=(0.0, 0.0, .078),
            yaw=0.0,
        )

        observed = evaluate_observed_goal(
            moving, goal, "no_expected_damage",
        )

        self.assertIs(observed.status, ObservedGoalStatus.NOT_SATISFIED)


    def test_goal_start_binds_the_callers_task_damage_budget(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, -4, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        current = frame(world, 0, (.5, 1.0, .5))
        session = NavigationSession(
            "damage-budget-session", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        budget = TaskDamageBudget("allow_one_point", 1.0)

        session.start_goal(
            "drop-goal", 1,
            replace(_goal((1.5, -3.0, .5)),
                    risk_policy_id="allow_one_point"), current,
            damage_budget=budget,
        )

        self.assertEqual(session._request.damage_budget, budget)
        self.assertEqual(session._request.goal_state.risk_policy_id,
                         "allow_one_point")

    def test_replanning_keeps_damage_already_spent_by_the_same_task(self):
        session = NavigationSession(
            "cumulative-damage-session", self.profiles(),
            planner_worker=_InlinePlanner(),
        )
        session._task_damage_budget = TaskDamageBudget(
            "task-total-four", 4.0,
        )
        session._risk_ledger = TaskRiskLedger(
            "cumulative-damage-task", session._task_damage_budget,
        )
        session._risk_ledger.reserve("earlier-drop", 2, policy_revision=0)
        session._risk_ledger.commit("earlier-drop", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=1,
        ))
        session._clear_active_execution()
        session._record_completed_movement_damage(1.0)

        self.assertEqual(
            session._remaining_damage_budget(),
            TaskDamageBudget("task-total-four", 2.0),
        )

    def test_admission_rechecks_damage_spent_after_request_was_issued(self):
        world_session = WorldSessionId("stale-damage-budget-world")
        world = WorldKnowledge(world_session)
        stamp = ObservationStamp(world_session, 1, 1, "test-clock", 1)
        stone = BlockGeometry.full_cube("minecraft:stone")
        blocks = {(0, 63, 0): stone, (1, 58, 0): stone}
        landing_cell = (1, 59, 0)
        world.confirm_air(
            stamp,
            tuple(
                (x, y, z)
                for x in range(-3, 5)
                for y in range(55, 71)
                for z in range(-3, 4)
                if (x, y, z) not in blocks
            ),
            visual_evidence={
                landing_cell: VisualAirEvidence(stamp, 2.0, True),
            },
        )
        world.observe_blocks(stamp, blocks)
        session = NavigationSession(
            "stale-damage-budget",
            NavigationSessionProfiles.load(Path("config/motion-navigation")),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        body = (.5, 64.0, .5)
        session.start_goal(
            "task", 1,
            replace(_goal((1.5, 59.0, .5)), risk_policy_id="two"),
            frame(world, 1, body),
            damage_budget=TaskDamageBudget("two", 2.0),
        )
        session._risk_ledger.reserve("earlier-drop", 2, policy_revision=0)
        session._risk_ledger.commit("earlier-drop", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=1,
        ))
        session._clear_active_execution()

        proposal = session.propose(
            frame(world, 2, body), None, 2_000_000_000,
        )

        self.assertIsNone(session.active_route)
        self.assertIsNot(proposal.report.state, NavigationSessionState.EXECUTING)
        self.assertEqual(
            session._request.damage_budget.maximum_expected_damage_points,
            0.0,
        )

    def test_started_session_cannot_replace_its_world_owner(self):
        from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter

        session, _, _ = _gap_session(session_id="owner-is-frozen")

        with self.assertRaisesRegex(ContractViolation, "world owner"):
            session.attach_observation_adapter(NavigationObservationAdapter())

    def test_motion_residual_world_query_precedes_bulk_planning_queries(self):
        session = NavigationSession(
            "residual-query-priority", self.profiles(),
            planner_worker=_InlinePlanner(),
        )
        session.ingest(valid_snapshot_v3(sequence=1))
        session._snapshot_missing = tuple(
            (-200 + index, 0, 0) for index in range(200)
        )
        urgent = (100, 1, 100)
        session._residual_missing = (urgent,)

        request = session.observation_request(max_positions=2)

        self.assertIn(urgent, request.air_positions)
        self.assertEqual(len(request.air_positions), 2)

    def test_owned_edge_probe_rechecks_its_landing_cell_each_frame(self):
        session = NavigationSession(
            "edge-probe-query-priority", self.profiles(),
            planner_worker=_InlinePlanner(),
        )
        session.ingest(valid_snapshot_v3(sequence=1))
        landing = (0, -4, 1)
        session._snapshot_missing = (landing,)
        session._edge_probe = LandingEdgeProbe(
            "edge-probe-query-goal", 1, landing, 1,
        )

        first = session.observation_request()
        second = session.observation_request()

        self.assertEqual(first.air_positions, (landing,))
        self.assertEqual(second.air_positions, (landing,))

    def test_grounded_drop_rechecks_landing_dependencies_each_frame(self):
        session = NavigationSession(
            "drop-dependency-query-priority", self.profiles(),
            planner_worker=_InlinePlanner(),
        )
        frame = session.ingest(valid_snapshot_v3(sequence=1))
        start_id = SurfaceNodeId(0, 0, 1, 0)
        end_id = SurfaceNodeId(0, 1, 0, 0)
        start = SupportSurface(
            start_id, (.5, 1.0, .5), HorizontalRegion(0, 0, 1, 1),
            1.0, ("minecraft:grass_block",), (),
        )
        end = SupportSurface(
            end_id, (.5, 0.0, 1.5), HorizontalRegion(0, 1, 1, 2),
            1.0, ("minecraft:grass_block",), (),
        )
        landing_support = (0, -1, 1)
        action = ControlledDropSegment(
            ControlledDropEdge(start_id, end_id, "direct-fall", 1.0, ()),
            start, end, (landing_support,),
        )
        action_route = ActionRoute("drop-observation-route", (action,))
        active_route = ActiveRoute(
            "drop-observation-route", 1, "request", "goal", 1,
            frame.session.value, None, 1.0, 0.0, (),
            ExecutableCorridor(
                (start_id, end_id), (landing_support,), 1.0, end_id,
            ),
            action_route,
        )
        executor = _ObservationRouteExecutor(action_route)
        self.assertTrue(session._supervisor.offer_route(
            RouteControl(active_route, executor), frame,
        ))

        first = session.observation_request()
        second = session.observation_request()

        self.assertIn(landing_support, first.air_positions)
        self.assertIn(landing_support, second.air_positions)

    def test_duplicate_residual_check_keeps_pending_world_query(self):
        session = NavigationSession(
            "residual-query-duplicate", self.profiles(),
            planner_worker=_InlinePlanner(),
        )
        snapshot = valid_snapshot_v3(sequence=1)
        urgent = (100, 1, 100)
        session._motion_residual = Mock()
        session._motion_residual.observe.side_effect = (
            MotionResidualResult(
                MotionResidualStatus.NEEDS_WORLD, 1, 2,
                missing_cells=(urgent,),
            ),
            None,
        )
        ledger = InputApplicationLedger()

        session.motion_residual(snapshot, ledger)
        session.motion_residual(snapshot, ledger)

        self.assertIn(urgent, session.observation_request().air_positions)

    def test_loaded_walk_profile_is_the_ground_mode_profile_used_by_planning(self):
        profiles = NavigationSessionProfiles.load(
            Path(__file__).resolve().parents[2] / "config" / "motion-navigation",
        )

        self.assertIsNotNone(profiles.ground_modes)
        self.assertIs(
            profiles.ground,
            profiles.ground_modes.require(MovementMode.WALK).motion,
        )

    def test_same_sequence_in_a_new_world_session_is_not_reused_from_cache(self):
        session = NavigationSession(
            "world-change-cache", self.profiles(), planner_worker=_InlinePlanner(),
        )
        first = session.ingest(replace(
            valid_snapshot_v3(sequence=1), episode_id="episode-one",
        ))
        second = session.ingest(replace(
            valid_snapshot_v3(sequence=1), episode_id="episode-two",
        ))

        self.assertNotEqual(first.session, second.session)
        self.assertIs(session.report.state, NavigationSessionState.FAILED)
        self.assertEqual(session.report.reason, "world_session_changed")

    def test_start_plans_admits_and_returns_one_ordered_navigation_intent(self):
        world = _known_world({
            (-1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, _, goal = _nodes(world, (-1, 0, 1))
        initial = frame(world, 0, start.position)
        request = SurfacePlanningRequest(
            1, "request-1", "goal-1", 1, world.session.value,
            start.node_id, goal.node_id, goal_state=_goal(goal.position),
        )
        planner = _InlinePlanner()
        session = NavigationSession(
            "session-1", self.profiles(), planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())

        session.start(request, initial)
        proposal = session.propose(
            initial, None, 2_000_000_000,
        )

        self.assertIs(proposal.report.state, NavigationSessionState.EXECUTING)
        self.assertEqual(proposal.report.goal_id, "goal-1")
        self.assertIsNotNone(proposal.control_frame)
        self.assertEqual(len(proposal.control_frame.intents), 1)
        self.assertEqual(
            proposal.control_frame.intents[0].intent.movement.forward, 1,
        )
        self.assertEqual(
            proposal.control_frame.intents[0].intent.movement_observed_yaw_limit_degrees,
            5.0,
        )
        self.assertEqual(len(proposal.control_frame.task_events), 2)
        session_event, decision_event = proposal.control_frame.task_events
        self.assertEqual(session_event.record_type, "navigation_session_decision")
        self.assertTrue(session_event.payload["active_route"])
        self.assertTrue(session_event.payload["route_decision_present"])
        self.assertEqual(decision_event.record_type, "navigation_route_decision")
        self.assertEqual(
            decision_event.payload["reason_code"],
            proposal.route_decision.reason_code,
        )
        self.assertTrue(decision_event.payload["submit_input"])
        self.assertEqual(proposal.report.route_id, session.active_route.route_id)

        polls_after_admission = planner.polls
        session.propose(
            frame(world, 1, start.position), None, 2_000_000_000,
        )
        self.assertEqual(
            planner.polls, polls_after_admission,
            "an admitted route does not poll a planner with no pending request",
        )

    def test_walk_recomputes_all_four_ground_axes_from_the_observed_yaw(self):
        world = _known_world({
            (-1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, _, goal = _nodes(world, (-1, 0, 1))
        expected_by_yaw = (
            (0.0, MovementV1(strafe=1)),
            (math.pi / 2, MovementV1(forward=-1)),
            (math.pi, MovementV1(strafe=-1)),
            (math.pi * 1.5, MovementV1(forward=1)),
        )

        for index, (yaw, expected) in enumerate(expected_by_yaw):
            with self.subTest(yaw=yaw):
                initial = frame(world, index, start.position, yaw=yaw)
                request = SurfacePlanningRequest(
                    index + 10, f"axis-request-{index}",
                    f"axis-goal-{index}", 1, world.session.value,
                    start.node_id, goal.node_id,
                    goal_state=_goal(goal.position),
                )
                session = NavigationSession(
                    f"axis-session-{index}", self.profiles(),
                    planner_worker=_InlinePlanner(),
                    clock_ns=lambda: 1_000_000_000,
                )
                session.bind_source(_source())
                try:
                    session.start(request, initial)
                    proposal = session.propose(initial, None, 2_000_000_000)
                    intent = proposal.control_frame.intents[0].intent

                    self.assertEqual(intent.movement, expected)
                    self.assertEqual(intent.valid_for_ticks, 1)
                    self.assertEqual(
                        intent.movement_observed_yaw_limit_degrees, 5.0,
                    )
                finally:
                    session.close()

    def test_walk_recomputes_movement_from_the_conditioned_final_yaw(self):
        world = _known_world({
            (-1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, _, goal = _nodes(world, (-1, 0, 1))
        initial = frame(world, 0, start.position, yaw=0.0)
        request = SurfacePlanningRequest(
            25, "conditioned-yaw-request", "conditioned-yaw-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        )
        session = NavigationSession(
            "conditioned-yaw-session", self.profiles(),
            planner_worker=_InlinePlanner(),
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        try:
            session.start(request, initial)
            proposal = session.propose(
                initial,
                None,
                2_000_000_000,
                conditioned_yaw_delta_degrees=90.0,
                conditioned_look_intent_id="combat-look",
            )
            intent = proposal.control_frame.intents[0].intent

            self.assertEqual(intent.movement, MovementV1(forward=-1))
            self.assertEqual(
                intent.movement_conditioned_look_intent_id,
                "combat-look",
            )
            self.assertIsNone(intent.movement_observed_yaw_limit_degrees)
            self.assertEqual(intent.valid_for_ticks, 1)
        finally:
            session.close()

    def test_known_route_is_not_blocked_by_irrelevant_unknown_scope_cell(self):
        world, irrelevant_unknown = _known_corridor_with_one_irrelevant_unknown()
        start, _, goal = _nodes(world, (-1, 0, 1))
        initial = frame(world, 0, start.position)
        request = SurfacePlanningRequest(
            1, "partial-request", "partial-goal", 1, world.session.value,
            start.node_id, goal.node_id, goal_state=_goal(goal.position),
        )
        session = NavigationSession(
            "partial-session", self.profiles(), planner_worker=_InlinePlanner(),
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())

        session.start(request, initial)
        proposal = session.propose(initial, None, 2_000_000_000)

        self.assertIs(proposal.report.state, NavigationSessionState.EXECUTING)
        self.assertNotIn(irrelevant_unknown, proposal.report.missing_cells)
        self.assertIsNotNone(proposal.control_frame)
        self.assertEqual(
            proposal.control_frame.intents[0].intent.movement.forward, 1,
        )

    def test_request_start_frame_changes_are_not_replayed_as_late_route_changes(self):
        world = _known_world({
            (-1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, _, goal = _nodes(world, (-1, 0, 1))
        initial = replace(
            frame(world, 7, start.position),
            changed_cells=((0, 0, 0),),
        )
        session = NavigationSession(
            "same-frame-session", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "same-frame-request", "same-frame-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)

        proposal = session.propose(initial, None, 2_000_000_000)

        self.assertIs(proposal.report.state, NavigationSessionState.EXECUTING)
        self.assertEqual(proposal.report.reason, "tracking_fixed_route")

    def test_no_known_route_requests_only_the_missing_information(self):
        world, unknown_gap = _known_endpoints_with_unknown_gap()
        start, goal = _nodes(world, (-1, 1))
        initial = frame(world, 0, start.position)
        session = NavigationSession(
            "unknown-gap-session", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "unknown-gap-request", "unknown-gap-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)

        proposal = session.propose(initial, None, 2_000_000_000)

        self.assertIs(
            proposal.report.state, NavigationSessionState.NEEDS_INFORMATION,
        )
        self.assertEqual(
            proposal.report.reason, "no_known_route_requires_information",
        )
        self.assertIn(unknown_gap, proposal.report.missing_cells)
        self.assertIsNone(session.active_route)

    def test_no_known_route_uses_planner_blockers_not_snapshot_order(self):
        world, external, gap = (
            _known_endpoints_with_external_unknown_and_unknown_gap()
        )
        start, goal = _nodes(world, (-1, 1))
        initial = frame(world, 0, start.position)
        session = NavigationSession(
            "targeted-unknown-gap-session", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "targeted-unknown-gap-request", "targeted-unknown-gap-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)

        proposal = session.propose(initial, None, 2_000_000_000)

        self.assertIs(
            proposal.report.state, NavigationSessionState.NEEDS_INFORMATION,
        )
        self.assertEqual(proposal.report.missing_cells, (gap,))
        self.assertNotIn(external, proposal.report.missing_cells)

    def test_missing_cell_outside_view_adds_low_priority_information_look(self):
        world, unknown_gap = _known_endpoints_with_unknown_gap()
        start, goal = _nodes(world, (-1, 1))
        initial = frame(world, 0, start.position)
        initial = replace(
            initial,
            body=replace(initial.body, yaw_radians=math.radians(90.0)),
            air_query_results=(AirQueryResultV3(unknown_gap, "outside_view"),),
        )
        session = NavigationSession(
            "unknown-gap-look-session", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "unknown-gap-look-request", "unknown-gap-look-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)

        proposal = session.propose(initial, None, 2_000_000_000)

        self.assertIs(
            proposal.report.state, NavigationSessionState.NEEDS_INFORMATION,
        )
        self.assertIn(unknown_gap, proposal.report.missing_cells)
        self.assertIsNotNone(proposal.control_frame)
        looks = tuple(
            ordered.intent
            for ordered in proposal.control_frame.intents
            if ordered.intent.look is not None
        )
        self.assertEqual(len(looks), 1)
        self.assertIs(looks[0].priority, ActionPriorityV0.BEHAVIOR)
        self.assertIsNone(looks[0].movement)
        self.assertLessEqual(abs(looks[0].look.yaw_delta_degrees), 36.0)

    def test_missing_cell_already_in_view_does_not_turn_in_place(self):
        world, unknown_gap = _known_endpoints_with_unknown_gap()
        start, goal = _nodes(world, (-1, 1))
        initial = frame(world, 0, start.position)
        initial = replace(
            initial,
            body=replace(
                initial.body,
                yaw_radians=math.radians(-90.0),
                pitch_radians=math.radians(77.0),
            ),
            air_query_results=(AirQueryResultV3(unknown_gap, "occluded"),),
        )
        session = NavigationSession(
            "unknown-gap-occluded-session", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "unknown-gap-occluded-request", "unknown-gap-occluded-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)

        proposal = session.propose(initial, None, 2_000_000_000)

        self.assertIsNotNone(proposal.control_frame)
        self.assertFalse(any(
            ordered.intent.look is not None
            for ordered in proposal.control_frame.intents
        ))

    def test_visible_landing_cell_without_lower_evidence_looks_below_center(self):
        world, missing = _known_endpoints_with_unknown_gap()
        start, _ = _nodes(world, (-1, 1))
        current = frame(world, 0, start.position)
        eye_y = current.body.body_box.max_y - .18
        horizontal = math.hypot(
            missing[0] + .5 - current.body.position[0],
            missing[2] + .5 - current.body.position[2],
        )
        center_pitch = -math.degrees(math.atan2(
            missing[1] + .5 - eye_y, max(horizontal, 1.0e-9),
        ))
        current = replace(
            current,
            body=replace(
                current.body, pitch_radians=math.radians(center_pitch),
            ),
        )

        centered = information_look_for_missing_cells(
            current, (missing,), {missing: "visible_air"},
        )
        lower = information_look_for_missing_cells(
            current, (missing,), {missing: "visible_air"},
            lower_region_positions=frozenset({missing}),
        )

        self.assertIsNone(centered)
        self.assertIsNotNone(lower)
        self.assertGreater(lower.pitch_delta_degrees, 1.0)

    def test_lower_landing_evidence_uses_bounded_sneak_edge_probe(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (0, -3, 1): BlockGeometry.full_cube("minecraft:stone"),
        })
        current = frame(world, 0, (.5, 1.0, .5), yaw=0.0)
        landing_body_cell = (0, -2, 1)

        movement = information_probe_movement(
            current, frozenset({landing_body_cell}),
        )

        self.assertEqual(
            movement,
            MovementV1(forward=1, strafe=1, sneak=True),
        )
        # The eye must move around one corner of the support.  Looking straight
        # over the edge leaves the platform between the eye and the lower part
        # of the landing cell.
        edge = frame(world, 1, (1.15, 1.0, .85), yaw=0.0)
        probe = LandingEdgeProbe(
            "lower-landing-probe", 1, landing_body_cell, 0,
        )
        probe.vantage_position = (1.15, .85)
        self.assertEqual(
            probe.movement(edge),
            MovementV1(sneak=True),
        )
        self.assertEqual(
            information_probe_movement(
                replace(current, body=replace(current.body, is_on_ground=False)),
                frozenset({landing_body_cell}),
            ),
            MovementV1(),
        )

    def test_admission_reason_text_does_not_start_edge_probe(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (0, -5, 1): BlockGeometry.full_cube("minecraft:stone"),
        })
        landing_body_cell = (0, -4, 1)
        current = replace(
            frame(world, 0, (.5, 1.0, .5), yaw=0.0),
            air_query_results=(
                AirQueryResultV3(landing_body_cell, "occluded"),
            ),
        )
        session = NavigationSession(
            "occluded-drop-edge-probe", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session._transition(
            NavigationTransitionAction.WAIT_FOR_INFORMATION,
            "landing_visual_evidence_missing",
        )
        session._reason = AdmissionReason.LANDING_VISUAL_EVIDENCE_MISSING
        session._snapshot_missing = (landing_body_cell,)

        self.assertIsNone(session._information_look(current))
        self.assertEqual(
            information_probe_movement(
                current, frozenset(session._information.lower_required),
            ),
            MovementV1(),
        )

        session._reason = "no_known_route_requires_information"
        session._information.lower_required.clear()
        session._information_look(current)
        self.assertEqual(session._information.lower_required, set())

    def test_edge_probe_hold_keeps_sneak_while_verified_motion_is_pending(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        current = frame(world, 0, (.5, 1.0, .5), yaw=0.0)
        session = NavigationSession(
            "edge-hold-session", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session._frame = current
        session._edge_probe = LandingEdgeProbe(
            "edge-hold-goal", 1, (0, -2, 1), 0,
        )
        waiting = ActionRouteDecision(
            ActionRouteState.RUNNING, MovementV1(), None,
            1, 0, "awaiting_verified_motion", (), 0,
            submit_input=False,
        )

        route_control = Mock()
        route_control.executor.route = None
        route_control.route.route_id = "waiting-route"
        proposal = session._proposal(
            MovementV1(sneak=True), None, 1, 2_000_000_000,
            route_decision=waiting,
            route_control=route_control,
        )

        self.assertEqual(
            proposal.control_frame.intents[0].intent.movement,
            MovementV1(sneak=True),
        )
        self.assertIsNone(
            proposal.route_owner_id,
            "edge safety movement must not be registered as a route command",
        )

    def test_new_air_fact_does_not_restart_before_edge_probe_owns_evidence(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        landing = (0, -2, 1)
        initial = frame(world, 0, (.5, 1.0, .5), yaw=0.0)
        session = NavigationSession(
            "edge-probe-premature-replan", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session._frame = initial
        session._transition(
            NavigationTransitionAction.WAIT_FOR_INFORMATION,
            "landing_visual_evidence_missing",
        )
        session._reason = AdmissionReason.LANDING_VISUAL_EVIDENCE_MISSING
        session._snapshot_missing = (landing,)
        session._request = Mock(reach_policy=session._goal_requests.reach_policy)
        session._edge_probe = LandingEdgeProbe(
            "edge-probe-goal", 1, landing, 0,
        )
        stamp = ObservationStamp(world.session, 1, 1, "test-clock", 1)
        world.confirm_air(
            stamp, (landing,),
            {landing: VisualAirEvidence(stamp, 2.0, True)},
        )
        observed = replace(
            frame(world, 1, (.9, 1.0, .8), yaw=0.0),
            air_query_results=(
                AirQueryResultV3(
                    landing, "visible_air", 2.0,
                    lower_region_visible=True,
                ),
            ),
        )
        session._reissue_request_from_current = Mock()

        session.observe(observed, (landing,))

        session._reissue_request_from_current.assert_not_called()
        self.assertIs(session._state, NavigationSessionState.NEEDS_INFORMATION)
        self.assertIs(
            session._edge_probe.state, LandingEdgeProbeState.APPROACHING,
        )

    def test_edge_probe_handoff_waits_until_sneak_has_released(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        crouching = replace(
            frame(world, 1, (.5, 1.0, .5), yaw=0.0),
            body=replace(
                frame(world, 1, (.5, 1.0, .5), yaw=0.0).body,
                pose="crouching", is_sneaking=True,
            ),
        )
        probe = LandingEdgeProbe(
            "edge-handoff-goal", 1, (0, -2, 1), 0,
        )
        probe.entry_position = (.5, .5)
        probe.state = LandingEdgeProbeState.HOLDING_EDGE
        probe.begin_handoff("route_admitted")

        self.assertFalse(probe.handoff_ready(crouching))
        standing = replace(
            crouching,
            body=replace(
                crouching.body, pose="standing", is_sneaking=False,
            ),
        )
        self.assertTrue(probe.handoff_ready(standing))

    def test_edge_probe_moves_from_view_corner_to_stable_drop_entry(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        landing = (0, -2, 1)
        stamp = ObservationStamp(world.session, 3, 3, "test-clock", 3)
        world.confirm_air(
            stamp, (landing,),
            {landing: VisualAirEvidence(stamp, 3.0, True)},
        )
        edge = replace(
            frame(world, 3, (1.15, 1.0, .85), yaw=0.0),
            body=replace(
                frame(world, 3, (1.15, 1.0, .85), yaw=0.0).body,
                yaw_radians=math.radians(40.0),
                pitch_radians=math.radians(74.0),
                pose="crouching", is_sneaking=True,
            ),
        )
        probe = LandingEdgeProbe("returning-probe", 1, landing, 0)
        start = frame(world, 0, (.5, 1.0, .5), yaw=0.0)
        probe.movement(start)
        probe.vantage_position = (1.15, .85)
        probe.edge_sequence_id = 2
        probe.state = LandingEdgeProbeState.HOLDING_EDGE

        probe.begin_entry_alignment(edge)
        self.assertTrue(probe.positioning_entry)
        positioning = probe.movement(edge)
        self.assertTrue(positioning.sneak)
        self.assertNotEqual(positioning, MovementV1(sneak=True))

        entry = replace(
            edge,
            body=replace(
                edge.body,
                position=(.5, 1.0, .85),
                velocity_blocks_per_second=(0.0, 0.0, 0.0),
            ),
        )
        self.assertEqual(probe.movement(entry), MovementV1())
        self.assertTrue(probe.releasing)
        standing = replace(
            entry,
            body=replace(entry.body, pose="standing", is_sneaking=False),
        )
        self.assertFalse(probe.finish_release(standing))
        self.assertEqual(
            probe.release_look(standing),
            LookV1(yaw_delta_degrees=-36.0, pitch_delta_degrees=-36.0),
        )
        restored = replace(
            standing,
            body=replace(
                standing.body, yaw_radians=0.0, pitch_radians=0.0,
            ),
        )
        self.assertTrue(probe.finish_release(restored))
        self.assertTrue(probe.ready)
        self.assertTrue(probe.allows_evidence(restored, landing))
        later = frame(world, 20, (.5, 1.0, .85), yaw=0.0)
        self.assertTrue(probe.allows_evidence(later, landing))

    def test_goal_revision_releases_edge_probe_before_new_walk(self):
        world_session = WorldSessionId("edge-probe-goal-revision")
        world = WorldKnowledge(world_session)
        stamp = ObservationStamp(world_session, 1, 1, "test-clock", 1)
        stone = BlockGeometry.full_cube("minecraft:stone")
        blocks = {
            **{(0, 63, z): stone for z in range(-4, 1)},
            (1, 59, 0): stone,
        }
        world.confirm_air(stamp, tuple(
            (x, y, z)
            for x in range(-3, 5)
            for y in range(55, 71)
            for z in range(-7, 4)
            if (x, y, z) not in blocks
        ))
        world.observe_blocks(stamp, blocks)
        session = NavigationSession(
            "edge-probe-goal-revision-session",
            NavigationSessionProfiles.load(Path("config/motion-navigation")),
            planner_worker=_InlinePlanner(),
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        body = (.5, 64.0, .5)
        landing_cell = (1, 60, 0)
        session.start_goal(
            "task", 1,
            replace(_goal((1.5, 60.0, .5)), risk_policy_id="one"),
            frame(world, 0, body),
            damage_budget=TaskDamageBudget("one", 1.0),
        )
        for sequence in range(1, 4):
            current = replace(
                frame(world, sequence, body),
                air_query_results=(AirQueryResultV3(landing_cell, "occluded"),),
            )
            proposal = session.propose(current, None, 2_000_000_000)
            movement = proposal.control_frame.intents[0].intent.movement
            self.assertEqual(movement.forward, 1)

        session.update_goal(
            "task", 2,
            replace(_goal((.5, 64.0, -3.5)), risk_policy_id="one"),
        )
        for sequence in range(4, 10):
            proposal = session.propose(
                frame(world, sequence, body), None, 2_000_000_000,
            )
            self.assertIs(proposal.report.state,
                          NavigationSessionState.CANCELLING)
            self.assertIsNone(proposal.route_decision)
            self.assertTrue(
                proposal.control_frame.intents[0].intent.movement.sneak,
                "missing Runtime input evidence cannot release the old probe",
            )

    def test_input_lost_returns_a_terminal_result_without_replanning(self):
        world = _known_world({
            (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
            for x in range(4)
        })
        start, goal = _nodes(world, (0, 3))
        session = NavigationSession(
            "input-lost-terminal", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "task", 1, _goal(goal.position), frame(world, 0, start.position),
        )
        session.propose(frame(world, 1, start.position), None, 2_000_000_000)
        self.assertIsNotNone(session._executor)
        request_sequence = session._request.sequence
        _replace_route_executor(session, _RepeatingRecoveryExecutor(
            ActionRouteState.INPUT_LOST, "input_application_unconfirmed",
        ))

        current = frame(world, 2, start.position)
        proposal = session.propose(
            current, _ground_anchor(current), 2_000_000_000,
            input_ledger=InputApplicationLedger(),
        )

        self.assertIs(proposal.report.state, NavigationSessionState.FAILED)
        self.assertEqual(proposal.report.reason, "input_application_unconfirmed")
        self.assertEqual(session._request.sequence, request_sequence)

    def test_input_lost_after_cancel_and_safe_release_keeps_cancel_outcome(self):
        world = _known_world({
            (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
            for x in range(4)
        })
        start, goal = _nodes(world, (0, 3))
        session = NavigationSession(
            "cancel-input-lost-terminal", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "task", 1, _goal(goal.position), frame(world, 0, start.position),
        )
        session.propose(frame(world, 1, start.position), None, 2_000_000_000)
        self.assertIsNotNone(session._executor)
        session.cancel("user_cancelled")
        _replace_route_executor(session, _RepeatingRecoveryExecutor(
            ActionRouteState.INPUT_LOST, "input_application_unconfirmed",
        ))

        current = frame(world, 2, start.position)
        proposal = session.propose(
            current, _ground_anchor(current), 2_000_000_000,
            input_ledger=InputApplicationLedger(),
        )

        self.assertIs(proposal.report.state, NavigationSessionState.CANCELLED)
        self.assertEqual(proposal.report.reason, "user_cancelled")

    def test_old_route_input_lost_during_goal_revision_is_not_replaced(self):
        world = _known_world({
            (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
            for x in range(4)
        })
        start, goal = _nodes(world, (0, 3))
        session = NavigationSession(
            "revision-input-lost", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "task", 1, _goal(goal.position), frame(world, 0, start.position),
        )
        session.propose(frame(world, 1, start.position), None, 2_000_000_000)
        old_request = session.active_route.source_request_id
        session.update_goal("task", 2, _goal(start.position))
        self.assertNotEqual(session._request.request_id, old_request)
        _replace_route_executor(session, _RepeatingRecoveryExecutor(
            ActionRouteState.INPUT_LOST, "old_air_input_unconfirmed",
        ))
        current = frame(world, 2, start.position)
        proposal = session.propose(
            current, _ground_anchor(current), 2_000_000_000,
            input_ledger=InputApplicationLedger(),
        )
        self.assertIs(proposal.report.state, NavigationSessionState.FAILED)
        self.assertEqual(proposal.report.reason, "old_air_input_unconfirmed")

    def test_finite_task_recovery_is_bounded_to_twelve_cycles(self):
        world = _known_world({
            (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
            for x in range(4)
        })
        start, goal = _nodes(world, (0, 3))
        session = NavigationSession(
            "bounded-same-replan", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start_goal(
            "task", 1, _goal(goal.position), frame(world, 0, start.position),
        )
        terminal = None
        ledger = InputApplicationLedger()
        for sequence in range(1, 40):
            if session._executor is not None:
                _replace_route_executor(session, _RepeatingRecoveryExecutor(
                    ActionRouteState.NEEDS_REPLAN,
                    "ground_traversal_stalled",
                ))
            current = frame(world, sequence, start.position)
            terminal = session.propose(
                current, _ground_anchor(current), 2_000_000_000,
                input_ledger=ledger,
            )
            if terminal.report.terminal:
                break

        self.assertIsNotNone(terminal)
        self.assertIs(terminal.report.state, NavigationSessionState.FAILED,
                      session._supervisor.last_handoff)
        self.assertEqual(terminal.report.reason, "task_recovery_budget_exhausted")
        self.assertEqual(session._retry_ledger.total_recovery_starts, 12)

    def test_structurally_occluded_information_wait_is_bounded(self):
        world, unknown_gap = _known_endpoints_with_unknown_gap()
        start, goal = _nodes(world, (-1, 1))
        initial = frame(world, 0, start.position)
        initial = replace(
            initial,
            air_query_results=(AirQueryResultV3(unknown_gap, "occluded"),),
        )
        session = NavigationSession(
            "bounded-occluded-information", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "bounded-occluded-request", "bounded-occluded-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)
        session.propose(initial, None, 2_000_000_000)

        for sequence in range(1, 41):
            current = replace(
                initial,
                body=replace(initial.body, sequence_id=sequence),
            )
            session._information_look(current)

        self.assertIs(session.report.state, NavigationSessionState.FAILED)
        self.assertEqual(
            session.report.reason,
            "information_occluded_requires_observation_position",
        )

    def test_out_of_range_information_wait_is_bounded_without_turning(self):
        world, unknown_gap = _known_endpoints_with_unknown_gap()
        start, goal = _nodes(world, (-1, 1))
        initial = replace(
            frame(world, 0, start.position),
            air_query_results=(AirQueryResultV3(unknown_gap, "out_of_range"),),
        )
        session = NavigationSession(
            "bounded-out-of-range-information", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "bounded-range-request", "bounded-range-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)
        session.propose(initial, None, 2_000_000_000)

        for sequence in range(1, 41):
            current = replace(
                initial,
                body=replace(initial.body, sequence_id=sequence),
                air_query_results=(),
            )
            self.assertIsNone(session._information_look(current))

        self.assertIs(session.report.state, NavigationSessionState.FAILED)
        self.assertEqual(session.report.reason, "information_out_of_range")

    def test_information_wait_bounds_missing_query_responses(self):
        world, missing = _known_endpoints_with_unknown_gap()
        start, goal = _nodes(world, (-1, 1))
        initial = frame(world, 0, start.position)
        session = NavigationSession(
            "missing-query-response-wait", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "missing-query-request", "missing-query-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)
        session.propose(initial, None, 2_000_000_000)
        self.assertIn(missing, session.report.missing_cells)
        for sequence in range(1, 41):
            current = replace(initial, body=replace(
                initial.body, sequence_id=sequence,
            ), air_query_results=())
            session._information_look(current)
        self.assertIs(session.report.state, NavigationSessionState.FAILED)
        self.assertEqual(session.report.reason,
                         "information_unavailable_timeout")

    def test_repeated_partial_query_cannot_reset_another_missing_cell_wait(self):
        world, first = _known_endpoints_with_unknown_gap()
        start, goal = _nodes(world, (-1, 1))
        second = (first[0] + 1, first[1], first[2])
        initial = frame(world, 0, start.position)
        session = NavigationSession(
            "partial-query-response-wait", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "partial-query-request", "partial-query-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)
        session.propose(initial, None, 2_000_000_000)
        # This test exercises the generic multi-cell wait owner directly. The
        # planner-owned information selection has its own paging/progress
        # tests in test_planning_coordinator.
        session._planning_coordinator = None
        session._snapshot_missing = (first, second)
        session._reissue_request_from_current = Mock()
        session._information_look(initial)
        for sequence in range(1, 42):
            current = replace(initial, body=replace(
                initial.body, sequence_id=sequence,
            ), air_query_results=(AirQueryResultV3(
                first, "visible_air", 1.0, True,
            ),))
            session.observe(current, ())
            session._information_look(current)
        self.assertIs(session.report.state, NavigationSessionState.FAILED)
        self.assertEqual(session.report.reason,
                         "information_unavailable_timeout")

    def test_conditioned_task_look_suppresses_information_look(self):
        world, _ = _known_endpoints_with_unknown_gap()
        start, goal = _nodes(world, (-1, 1))
        initial = frame(world, 0, start.position)
        initial = replace(
            initial,
            body=replace(initial.body, yaw_radians=math.radians(90.0)),
        )
        session = NavigationSession(
            "unknown-gap-task-look-session", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "unknown-gap-task-look-request", "unknown-gap-task-look-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)

        proposal = session.propose(
            initial, None, 2_000_000_000,
            conditioned_yaw_delta_degrees=15.0,
            conditioned_look_intent_id="combat-look/1",
        )

        self.assertIsNotNone(proposal.control_frame)
        self.assertFalse(any(
            ordered.intent.look is not None
            for ordered in proposal.control_frame.intents
        ))

    def test_step_route_uses_the_same_session_instead_of_a_script_only_path(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:smooth_stone_slab", "boxes",
                (Aabb(0, 0, 0, 1, .5, 1),),
            ),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start = query_support_surfaces(world.view(), 0, 0, .5, .5).surfaces[0]
        goal = query_support_surfaces(world.view(), 1, 0, 1, 1).surfaces[0]
        initial = frame(world, 0, start.position)
        request = SurfacePlanningRequest(
            1, "step-request", "step-goal", 1, world.session.value,
            start.node_id, goal.node_id, goal_state=_goal(goal.position),
        )
        session = NavigationSession(
            "step-session", self.profiles(), planner_worker=_InlinePlanner(),
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())

        session.start(request, initial)
        proposal = session.propose(initial, None, 2_000_000_000)

        self.assertIs(proposal.report.state, NavigationSessionState.EXECUTING)
        step_intent = proposal.control_frame.intents[0].intent
        self.assertTrue(step_intent.movement.forward)
        self.assertIsNone(step_intent.movement_observed_yaw_limit_degrees)
        from mc2p.motion_nav.action_route import StepSegment
        self.assertIs(type(session.active_route.action_route.actions[0]), StepSegment)

    def test_goal_revision_rejects_a_late_old_result_before_it_can_own_input(self):
        class OldThenHeldPlanner(_InlinePlanner):
            def poll_available(self):
                if self.jobs and self.jobs[0][3].goal_revision > 1:
                    return ()
                return super().poll_available()
        world = _known_world({
            (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-1, 2)
        })
        start, old_goal, new_goal = _nodes(world, (-1, 0, 1))
        initial = frame(world, 0, start.position)
        planner = OldThenHeldPlanner(hold_first=True)
        session = NavigationSession(
            "revision-session", self.profiles(), planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "old-request", "combat-goal", 1, world.session.value,
            start.node_id, old_goal.node_id,
            goal_state=_goal(old_goal.position),
        ), initial)
        waiting = session.propose(initial, None, 2_000_000_000)
        self.assertIs(waiting.report.state, NavigationSessionState.PLANNING)
        self.assertIsNotNone(waiting.control_frame)
        self.assertEqual(
            [event.record_type for event in waiting.control_frame.task_events],
            ["navigation_session_decision"],
        )
        waiting_event = waiting.control_frame.task_events[0]
        self.assertEqual(waiting_event.payload["state"], "planning")
        self.assertEqual(waiting_event.payload["reason_code"], "planning_submitted")
        self.assertFalse(waiting_event.payload["active_route"])
        self.assertFalse(waiting_event.payload["route_decision_present"])
        self.assertFalse(waiting_event.payload["submit_input"])

        session.update_goal("combat-goal", 2, _goal(new_goal.position))
        # Let the old result arrive first. It must be discarded by request and
        # goal revision before an executor can be started.
        planner.hold_first = False
        stale = session.propose(initial, None, 2_000_000_000)

        self.assertIsNone(session.active_route)
        self.assertNotEqual(stale.report.route_id, "old-request")
        self.assertIn(stale.report.state, {
            NavigationSessionState.SNAPSHOTTING,
            NavigationSessionState.PLANNING,
        })
        self.assertEqual(stale.report.goal_revision, 2)

    def test_goal_revision_keeps_safe_active_route_until_replacement_is_ready(self):
        world = _known_world({
            (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-1, 9)
        })
        start, old_goal, new_goal = _nodes(world, (0, 6, 8))
        planner = _InlinePlanner()
        session = NavigationSession(
            "live-revision-session", self.profiles(), planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        initial = frame(world, 0, start.position)
        session.start(SurfacePlanningRequest(
            1, "request-1", "goal", 1, world.session.value,
            start.node_id, old_goal.node_id, goal_state=_goal(old_goal.position),
        ), initial)
        moving = session.propose(initial, None, 2_000_000_000)
        self.assertNotEqual(
            moving.control_frame.intents[0].intent.movement, MovementV1(),
        )
        old_route_id = session.active_route.route_id

        planner.hold_first = True
        session.update_goal("goal", 2, _goal(new_goal.position))
        while_replanning = session.propose(
            frame(world, 1, start.position), None, 2_000_000_000,
        )

        self.assertEqual(session.active_route.route_id, old_route_id)
        self.assertEqual(while_replanning.report.goal_revision, 2)
        self.assertNotEqual(
            while_replanning.control_frame.intents[0].intent.movement,
            MovementV1(),
        )

    def test_quiescent_terminal_predecessor_waits_for_pending_route_command(self):
        world = _known_world({
            (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-1, 9)
        })
        start, old_goal, new_goal = _nodes(world, (0, 6, 8))
        session = NavigationSession(
            "terminal-predecessor-handoff", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        initial = frame(world, 0, start.position)
        session.start(SurfacePlanningRequest(
            1, "request-1", "goal", 1, world.session.value,
            start.node_id, old_goal.node_id, goal_state=_goal(old_goal.position),
        ), initial)
        session.propose(initial, None, 2_000_000_000)
        incumbent = session._supervisor.incumbent_route
        self.assertIsNotNone(incumbent)
        terminal_executor = _RepeatingRecoveryExecutor(
            ActionRouteState.UNSUPPORTED,
            "ordinary_ground_state_lost",
        )
        terminal_executor.route = incumbent.route.action_route
        session._supervisor._route = replace(
            incumbent,
            executor=terminal_executor,
            coordinator=None,
        )
        successor = replace(
            incumbent.route,
            route_id="pending-successor",
            source_request_id="request-2",
            goal_revision=2,
            # This synthetic waiting executor does not produce the admitted
            # Walk controller's typed progress evidence.
            validation_plan=None,
        )
        current = frame(world, 1, start.position)
        anchor = _ground_anchor(current)
        ledger = InputApplicationLedger()
        waiting_executor = _WaitingRouteExecutor()
        waiting_executor.route = successor.action_route
        self.assertTrue(session._supervisor.offer_route(
            RouteControl(successor, waiting_executor),
            current, ledger, anchor,
        ))
        session._request = replace(
            session._request,
            request_id="request-2",
            sequence=2,
            goal=new_goal.node_id,
            goal_revision=2,
            goal_state=_goal(new_goal.position),
        )

        proposal = session.propose(
            current, anchor, 2_000_000_000, input_ledger=ledger,
        )

        self.assertIs(proposal.report.state, NavigationSessionState.STOPPING)
        self.assertEqual(
            proposal.report.reason, "successor_route_waiting_for_motion",
        )
        self.assertIsNone(proposal.route_decision)
        self.assertIsNotNone(session.diagnostics.pending_route_id)
        self.assertIsNotNone(session.diagnostics.incumbent_route_id)

    def test_pending_goal_accepts_a_new_revision_before_support_is_known(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start = _nodes(world, (0,))[0]
        initial = frame(world, 0, start.position)
        session = NavigationSession(
            "pending-session", self.profiles(), planner_worker=_InlinePlanner(),
            clock_ns=lambda: 1_000_000_000,
        )

        session.start_goal("moving-goal", 1, _goal((5.5, 1.0, .5)), initial)
        session.update_goal("moving-goal", 2, _goal((6.5, 1.0, .5)))

        self.assertIs(session.report.state, NavigationSessionState.NEEDS_INFORMATION)
        self.assertEqual(session.report.goal_id, "moving-goal")
        self.assertEqual(session.report.goal_revision, 2)

    def test_missing_current_support_requests_information_instead_of_raising(self):
        world_session = WorldSessionId("navigation-session-missing-body-support")
        world = WorldKnowledge(world_session)
        stamp = ObservationStamp(world_session, 1, 1, "test-clock", 1)
        missing_support = (0, 0, 0)
        world.confirm_air(stamp, tuple(
            (x, y, z)
            for x in range(-1, 3)
            for y in range(-2, 4)
            for z in range(-1, 2)
            if (x, y, z) != missing_support
        ))
        world.observe_blocks(stamp, {
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        initial = frame(world, 0, (.5, 1.0, .5))
        session = NavigationSession(
            "missing-support-session", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )

        session.start_goal("known-goal", 1, _goal((1.5, 1.0, .5)), initial)

        self.assertIs(
            session.report.state, NavigationSessionState.NEEDS_INFORMATION,
        )
        self.assertEqual(
            session.report.reason, "current_surface_requires_information",
        )
        self.assertIn(missing_support, session.report.missing_cells)

    def test_cancel_returns_a_neutral_intent_and_closes_owned_workers(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, goal = _nodes(world, (0, 1))
        initial = frame(world, 0, start.position)
        planner = _InlinePlanner()
        session = NavigationSession(
            "cancel-session", self.profiles(), planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "cancel-request", "cancel-goal", 1, world.session.value,
            start.node_id, goal.node_id, goal_state=_goal(goal.position),
        ), initial)
        session.propose(initial, None, 2_000_000_000)

        session.cancel("task_cancelled")
        current = frame(world, 1, start.position)
        cancelled = session.propose(
            current, _ground_anchor(current), 2_000_000_000,
            input_ledger=InputApplicationLedger(),
        )
        session.close()

        self.assertIs(cancelled.report.state, NavigationSessionState.CANCELLED,
                      session._supervisor.last_handoff)
        self.assertEqual(
            cancelled.control_frame.intents[0].intent.movement.forward, 0,
        )
        self.assertTrue(planner.closed)

    def test_cancel_keeps_executor_until_its_landing_responsibility_finishes(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, goal = _nodes(world, (0, 1))
        session = NavigationSession(
            "cancel-owner-session", self.profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        initial = frame(world, 0, start.position)
        session.start(SurfacePlanningRequest(
            1, "cancel-owner-request", "cancel-owner-goal", 1,
            world.session.value, start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)
        session.propose(initial, None, 2_000_000_000)
        owner = _DelayedCancelExecutor()
        _replace_route_executor(session, owner)

        session.cancel("task_cancelled")
        landing = session.propose(
            frame(world, 1, start.position), None, 2_000_000_000,
        )
        current = frame(world, 2, start.position)
        finished = session.propose(
            current, _ground_anchor(current), 2_000_000_000,
            input_ledger=InputApplicationLedger(),
        )

        self.assertTrue(owner.cancel_requested)
        self.assertEqual(owner.decisions, 2)
        self.assertIs(landing.report.state, NavigationSessionState.CANCELLING)
        self.assertEqual(
            landing.control_frame.intents[0].intent.movement,
            MovementV1(forward=1),
        )
        self.assertIs(finished.report.state, NavigationSessionState.CANCELLED)

    def test_session_can_borrow_a_long_lived_planner_worker(self):
        planner = _InlinePlanner()
        session = NavigationSession(
            "borrowed-planner-session",
            self.profiles(),
            planner_worker=planner,
            owns_planner_worker=False,
            clock_ns=lambda: 1_000_000_000,
        )

        session.close()

        self.assertFalse(planner.closed)

    def test_terminal_session_transfers_workers_to_fresh_successor(self):
        planner = _InlinePlanner()
        session = NavigationSession(
            "terminal-session",
            self.profiles(),
            planner_worker=planner,
            clock_ns=lambda: 1_000_000_000,
        )
        session._transition(
            NavigationTransitionAction.MARK_FAILED,
            "test_terminal",
        )

        successor = session.spawn_successor("terminal-session-successor", task_id="new-task")

        self.assertTrue(session._closed)
        self.assertFalse(planner.closed)
        self.assertIs(successor._planner, planner)
        self.assertIs(successor.report.state, NavigationSessionState.READY)
        successor.close()
        self.assertTrue(planner.closed)

    def test_terminal_session_retires_owned_waits_before_fresh_successor(self):
        planner = _InlinePlanner()
        ledger = RetryLedger("goal")
        session = NavigationSession(
            "wait-owner-first",
            self.profiles(),
            planner_worker=planner,
            retry_ledger=ledger,
            clock_ns=lambda: 1_000_000_000,
        )
        policy = WaitPolicy(40, 2_000_000_000)
        ledger.begin_wait(
            "information",
            "navigation-session/wait-owner-first/information",
            policy,
            1,
            1_000_000_000,
        )
        ledger.begin_wait(
            "recovery",
            "navigation-session/wait-owner-first/recovery",
            policy,
            1,
            1_000_000_000,
        )

        session._transition(
            NavigationTransitionAction.MARK_FAILED,
            "information_out_of_range",
        )

        self.assertEqual(ledger.active_waits(), ())
        successor = session.spawn_successor("wait-owner-second", task_id="new-task")
        self.assertIsNone(successor._retry_ledger)
        self.assertIsNone(successor._risk_ledger)
        successor.close()

    def test_alive_planner_that_never_returns_has_a_caller_side_deadline(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, goal = _nodes(world, (0, 1))
        initial = frame(world, 0, start.position)
        clock = [1_000_000_000]
        planner = _InlinePlanner(hold_first=True)
        session = NavigationSession(
            "planner-timeout",
            self.profiles(),
            planner_worker=planner,
            clock_ns=lambda: clock[0],
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1,
            "planner-timeout-request",
            "goal",
            1,
            world.session.value,
            start.node_id,
            goal.node_id,
            maximum_planning_seconds=.05,
            goal_state=_goal(goal.position),
        ), initial)
        waiting = session.propose(initial, None, 2_000_000_000)
        self.assertIs(waiting.report.state, NavigationSessionState.PLANNING)
        self.assertEqual(waiting.report.reason, "planning_submitted")

        clock[0] += 100_000_000
        expired = session.propose(
            frame(world, 1, start.position),
            None,
            2_000_000_000,
        )

        self.assertIs(expired.report.state, NavigationSessionState.PLANNING)
        self.assertEqual(expired.report.reason, "planning_timeout_retry_started")
        self.assertEqual(
            session._planning_coordinator.local_attempt_failures, 1,
        )
        self.assertEqual(session._retry_ledger.total_recovery_starts, 0)

        planner.hold_first = False
        late = session.propose(
            frame(world, 2, start.position),
            None,
            2_000_000_000,
        )

        self.assertIs(late.report.state, NavigationSessionState.PLANNING)
        self.assertIsNone(session.active_route)

    def test_result_arriving_after_caller_deadline_is_not_admitted(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, goal = _nodes(world, (0, 1))
        initial = frame(world, 0, start.position)
        clock = [1_000_000_000]
        planner = _InlinePlanner(hold_first=True)
        session = NavigationSession(
            "late-planner-result", self.profiles(),
            planner_worker=planner, clock_ns=lambda: clock[0],
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "late-result-request", "goal", 1, world.session.value,
            start.node_id, goal.node_id,
            maximum_planning_seconds=.05,
            goal_state=_goal(goal.position),
        ), initial)
        session.propose(initial, None, 2_000_000_000)

        clock[0] += 200_000_000
        planner.hold_first = False
        expired = session.propose(
            frame(world, 1, start.position), None, 2_000_000_000,
        )

        self.assertIs(expired.report.state, NavigationSessionState.PLANNING)
        self.assertEqual(expired.report.reason, "planning_timeout_retry_started")
        self.assertIsNone(session.active_route)

    def test_dependency_rejection_immediately_owns_a_replanning_job(self):
        world = _known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        start, goal = _nodes(world, (0, 1))
        initial = frame(world, 0, start.position)
        planner = _InlinePlanner(hold_first=True)
        session = NavigationSession(
            "dependency-replan", self.profiles(),
            planner_worker=planner, clock_ns=lambda: 1_000_000_000,
        )
        session.bind_source(_source())
        session.start(SurfacePlanningRequest(
            1, "dependency-request", "goal", 1, world.session.value,
            start.node_id, goal.node_id,
            goal_state=_goal(goal.position),
        ), initial)
        session.propose(initial, None, 2_000_000_000)

        world.observe_blocks(ObservationStamp(
            world.session, 2, 2, "test-clock", 100_000_000,
        ), {(1, 0, 0): BlockGeometry.full_cube("minecraft:dirt")})
        changed = frame(world, 1, start.position)
        session.observe(changed, ((1, 0, 0),))
        planner.hold_first = False
        rejected = session.propose(changed, None, 2_000_000_000)

        self.assertIs(rejected.report.state, NavigationSessionState.PLANNING)
        self.assertTrue(session.diagnostics.planning_work_owned)
        self.assertEqual(session._retry_ledger.total_recovery_starts, 0)
        self.assertEqual(session._planning_coordinator.local_attempt_failures, 1)

    def test_gap_route_is_solved_by_the_session_coordinator(self):
        session, current, anchor = _gap_session()
        proposal, _, saw_motion_wait, current, anchor = _drive_until_verified_command(
            session, current, anchor,
        )

        from mc2p.motion_nav.action_route import JumpGapSegment
        self.assertIs(
            type(session.active_route.action_route.actions[0]), JumpGapSegment,
        )
        self.assertIsNotNone(proposal.route_decision)
        self.assertTrue(saw_motion_wait)
        self.assertTrue(proposal.route_decision.submit_input)
        self.assertTrue(proposal.control_frame.intents[0].intent.movement.jump)
        self.assertEqual(
            proposal.control_frame.intents[0].intent.movement_observed_yaw_limit_degrees,
            None,
        )
        session.close()

    def test_verified_motion_without_current_anchor_keeps_landing_owner(self):
        session, current, anchor = _gap_session(
            session_id="gap-missing-anchor-session",
        )
        try:
            _, ledger, _, current, anchor = _drive_until_verified_command(
                session, current, anchor,
            )
            executor = session._executor

            proposal = session.propose(
                current, None, 2_000_000_000, input_ledger=ledger,
            )

            self.assertIs(session._executor, executor)
            self.assertIs(
                proposal.report.state, NavigationSessionState.EXECUTING,
            )
            self.assertEqual(
                proposal.route_decision.reason_code,
                "verified_motion_anchor_unavailable_retain_landing",
            )
            self.assertTrue(proposal.route_decision.submit_input)
            self.assertEqual(
                proposal.control_frame.intents[0].intent.movement,
                MovementV1(),
            )
        finally:
            session.close()

    def test_airborne_dependency_change_keeps_executor_until_safe_terminal(self):
        session, current, anchor = _gap_session(
            session_id="gap-dependency-session",
        )
        try:
            _, _, _, current, anchor = _drive_until_verified_command(session, current, anchor)
            executor = session._executor
            dependency = session.active_route.action_route.dependencies[0]
            gap_position = (.5, 64.0, 1.5)
            airborne = replace(
                current.body,
                sequence_id=current.body.sequence_id + 1,
                position=gap_position,
                body_box=Aabb(.2, 64.0, 1.2, .8, 65.8, 1.8),
                is_on_ground=False,
            )
            changed = NavigationFrame(
                current.session, airborne, current.world, "fabric", (dependency,),
            )
            airborne_anchor = replace(
                anchor,
                observation_sequence_id=airborne.sequence_id,
                physics_state=replace(
                    anchor.physics_state,
                    position=gap_position,
                    on_ground=False,
                ),
            )

            session.observe(changed, changed.changed_cells)
            proposal = session.propose(
                changed, airborne_anchor, 2_000_000_000,
                input_ledger=InputApplicationLedger(max_records=64),
            )

            self.assertIs(session._executor, executor)
            self.assertIsNotNone(session.active_route)
            self.assertIs(
                proposal.report.state, NavigationSessionState.EXECUTING,
            )
            self.assertIsNotNone(proposal.route_decision)
        finally:
            session.close()

    def test_ground_handoff_waits_for_inflight_verified_command(self):
        session, current, anchor = _gap_session(
            session_id="gap-ground-handoff-session",
        )
        try:
            submitted, ledger, _, current, anchor = _drive_until_verified_command(
                session, current, anchor,
            )
            session.register_verified_submission(
                submitted, control_sequence=41,
            )
            old_executor = session._executor
            goal = query_support_surfaces(
                current.world, 0, 2, 64, 64,
            ).surfaces[0]
            session.update_goal("gap-goal", 2, _goal(goal.position))

            proposal = session.propose(
                current, anchor, 2_000_000_000, input_ledger=ledger,
            )

            self.assertIs(session._executor, old_executor)
            self.assertEqual(
                proposal.report.reason,
                "executing_safe_prefix_during_replan",
            )
            self.assertIsNotNone(proposal.route_decision)
            self.assertEqual(
                proposal.route_decision.reason_code, "awaiting_application",
            )
            self.assertIsNotNone(proposal.control_frame)
            self.assertEqual(proposal.control_frame.intents, ())
            self.assertEqual(len(proposal.control_frame.task_events), 2)
            decision_event = next(
                event for event in proposal.control_frame.task_events
                if event.record_type == "navigation_route_decision"
            )
            self.assertEqual(
                decision_event.payload["reason_code"], "awaiting_application",
            )
            self.assertFalse(decision_event.payload["submit_input"])
        finally:
            session.close()

    def test_body_support_prefers_feet_height_over_nearer_lower_slab(self):
        session, current, _ = _gap_session(
            session_id="body-support-height-session",
        )
        try:
            stamp = ObservationStamp(current.session, 1, 1, "test", 1)
            knowledge = WorldKnowledge(current.session)
            knowledge.confirm_air(stamp, tuple(
                (x, y, z)
                for x in range(-2, 4)
                for y in range(60, 71)
                for z in range(-2, 3)
            ))
            knowledge.observe_blocks(stamp, {
                (0, 63, 0): BlockGeometry.full_cube("minecraft:grass_block"),
                (1, 63, 0): BlockGeometry(
                    "minecraft:smooth_stone_slab",
                    "boxes",
                    (Aabb(0, 0, 0, 1, .5, 1),),
                ),
            })
            body_x = 1.2
            body = replace(
                current.body,
                position=(body_x, 64.0, .5),
                body_box=Aabb(body_x - .3, 64.0, .2, body_x + .3, 65.8, .8),
                is_on_ground=True,
            )
            node, missing = _ProductionNavigationSession._surface_for_body(replace(
                current,
                body=body,
                world=knowledge.view(),
            ))

            self.assertEqual(missing, ())
            self.assertIsNotNone(node)
            self.assertEqual((node.column_x, node.vertical_band), (0, 64))
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()

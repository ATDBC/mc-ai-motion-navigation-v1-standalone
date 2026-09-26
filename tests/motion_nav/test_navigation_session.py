from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import time
import unittest
from unittest.mock import Mock

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.intent_source import IntentSourceV1
from mc2p.motion_nav.known_map_planner import (
    SurfacePlanningRequest,
    plan_known_surface_snapshot,
)
from mc2p.motion_nav.movement_transition import (
    GoalState,
    GoalSupport,
    MovementMode,
)
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.motion_residual import (
    MotionResidualResult, MotionResidualStatus,
)
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.navigation_session import (
    NavigationSession,
    NavigationSessionProfiles,
    NavigationSessionState,
)
from mc2p.motion_nav.action_route_executor import (
    ActionRouteDecision, ActionRouteState,
)
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
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
from tests.observation_v3_fixtures import valid_snapshot_v3


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

    def is_alive(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class _DelayedCancelExecutor:
    """Models an airborne owner that needs one more frame to land."""

    def __init__(self) -> None:
        self.cancel_requested = False
        self.decisions = 0

    def cancel(self) -> None:
        self.cancel_requested = True

    def decide(self, frame, **_):
        self.decisions += 1
        if self.decisions == 1:
            return ActionRouteDecision(
                ActionRouteState.CANCELLING, MovementV1(forward=1), None,
                1, 0, "landing_after_cancel", (), 0,
            )
        return ActionRouteDecision(
            ActionRouteState.CANCELLED, MovementV1(), None,
            1, 0, "cancelled_after_landing", (), 0,
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
        if decision is not None and decision.submit_input:
            return proposal, ledger, saw_motion_wait
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


class NavigationSessionTests(unittest.TestCase):
    def profiles(self) -> NavigationSessionProfiles:
        return NavigationSessionProfiles(
            ordinary_profile(), jump_profile(), step_profile(),
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
        world = _known_world({
            (x, 0, 0): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-1, 2)
        })
        start, old_goal, new_goal = _nodes(world, (-1, 0, 1))
        initial = frame(world, 0, start.position)
        planner = _InlinePlanner(hold_first=True)
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
        cancelled = session.propose(
            frame(world, 1, start.position), None, 2_000_000_000,
        )
        session.close()

        self.assertIs(cancelled.report.state, NavigationSessionState.CANCELLED)
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
        session._executor = owner

        session.cancel("task_cancelled")
        landing = session.propose(
            frame(world, 1, start.position), None, 2_000_000_000,
        )
        finished = session.propose(
            frame(world, 2, start.position), None, 2_000_000_000,
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

    def test_gap_route_is_solved_by_the_session_coordinator(self):
        session, current, anchor = _gap_session()
        proposal, _, saw_motion_wait = _drive_until_verified_command(
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
            _, ledger, _ = _drive_until_verified_command(
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
            _drive_until_verified_command(session, current, anchor)
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
            submitted, ledger, _ = _drive_until_verified_command(
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
            node, missing = NavigationSession._surface_for_body(replace(
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

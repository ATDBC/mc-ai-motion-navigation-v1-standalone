from __future__ import annotations

import math
import unittest
from dataclasses import replace
from pathlib import Path

from mc2p.motion_nav.action_route import ActionRoute, WalkSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
from mc2p.motion_nav.ground_motion import PlanarBodyState, load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds,
    PlanningRequest,
    PlanningStatus,
    WalkEdge,
    WalkGraph,
    WalkNode,
    astar_plan,
    build_walk_graph,
)
from mc2p.motion_nav.movement_transition import (
    CancellationMode,
    GoalState,
    GoalSupport,
    MovementMode,
    MovementStateClass,
    MovementTransition,
    ResourceChange,
    ResourceState,
    compose_movement_transitions,
)
from mc2p.motion_nav.world_model import Aabb, WorldSessionId
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from tests.motion_nav.test_fixed_route_walk import FlatFixture


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/motion-navigation"


def node(node_id):
    return WalkNode(node_id, (node_id[0] + 0.5, float(node_id[1]), node_id[2] + 0.5), ())


def transition(name: str, seconds: float, stamina_delta: float = 0.0):
    state = MovementStateClass(
        mode=MovementMode.WALK,
        pose="standing",
        minimum_speed_blocks_per_second=0.0,
        maximum_speed_blocks_per_second=5.0,
    )
    return MovementTransition(
        transition_id=name,
        environment_id="fabric-1_21-motion-v1",
        mode=MovementMode.WALK,
        entry=state,
        exits=(state,),
        duration_seconds=seconds,
        dependencies=(),
        resource_change=ResourceChange((("stamina", stamina_delta),)),
        cancellation=CancellationMode.GROUND_STOP,
        input_loss=CancellationMode.GROUND_STOP,
    )


class B06MovementContractTests(unittest.TestCase):
    def test_goal_checks_region_support_mode_pose_speed_and_resources(self):
        goal = GoalState(
            region=Aabb(4.0, 1.0, 4.0, 5.0, 2.0, 5.0),
            support=GoalSupport.SOLID,
            allowed_modes=frozenset({MovementMode.WALK}),
            allowed_poses=frozenset({"standing"}),
            maximum_terminal_speed_blocks_per_second=0.1,
            minimum_resources=ResourceState((("stamina", 2.0),)),
        )
        self.assertTrue(goal.accepts(
            position=(4.5, 1.0, 4.5),
            support=GoalSupport.SOLID,
            mode=MovementMode.WALK,
            pose="standing",
            speed_blocks_per_second=0.05,
            resources=ResourceState((("stamina", 3.0),)),
        ))
        self.assertFalse(goal.accepts(
            position=(4.5, 1.0, 4.5),
            support=GoalSupport.SOLID,
            mode=MovementMode.WALK,
            pose="standing",
            speed_blocks_per_second=0.2,
            resources=ResourceState((("stamina", 3.0),)),
        ))
        self.assertFalse(goal.accepts(
            position=(4.5, 1.0, 4.5),
            support=GoalSupport.SOLID,
            mode=MovementMode.WALK,
            pose="standing",
            speed_blocks_per_second=0.05,
            resources=ResourceState((("stamina", 1.0),)),
        ))

    def test_goal_heading_and_risk_policy_are_part_of_completion(self):
        goal = GoalState(
            region=Aabb(0.0, 1.0, 0.0, 1.0, 2.0, 1.0),
            support=GoalSupport.SOLID,
            allowed_modes=frozenset({MovementMode.WALK}),
            allowed_poses=frozenset({"standing"}),
            maximum_terminal_speed_blocks_per_second=0.1,
            required_yaw_radians=0.0,
            maximum_yaw_error_radians=0.1,
            risk_policy_id="no_expected_damage",
        )
        common = dict(
            position=(0.5, 1.0, 0.5), support=GoalSupport.SOLID,
            mode=MovementMode.WALK, pose="standing",
            speed_blocks_per_second=0.0, resources=ResourceState(),
        )

        self.assertTrue(goal.accepts(
            **common, yaw_radians=0.05,
            applied_risk_policy_id="no_expected_damage",
        ))
        self.assertFalse(goal.accepts(
            **common, yaw_radians=0.2,
            applied_risk_policy_id="no_expected_damage",
        ))
        self.assertFalse(goal.accepts(
            **common, yaw_radians=0.05,
            applied_risk_policy_id="allow_expected_damage",
        ))
        request = PlanningRequest(
            1, "heading-goal", "goal", 1, "world",
            (0, 1, 0), (0, 1, 0), goal_state=goal,
        )
        self.assertIs(request.goal_state, goal)

    def test_resource_search_keeps_slower_nondominated_prefix(self):
        start, fast, slow, merge, goal = (
            (0, 1, 0), (1, 1, 0), (0, 1, 1), (1, 1, 1), (2, 1, 1),
        )
        nodes = tuple(node(value) for value in sorted((start, fast, slow, merge, goal)))
        edges = (
            WalkEdge(start, fast, 0.5, (), transition("fast-a", 0.5, -8.0)),
            WalkEdge(fast, merge, 0.5, (), transition("fast-b", 0.5, 0.0)),
            WalkEdge(start, slow, 1.5, (), transition("slow-a", 1.5, -1.0)),
            WalkEdge(slow, merge, 1.5, (), transition("slow-b", 1.5, 0.0)),
            WalkEdge(merge, goal, 1.0, (), transition("finish", 1.0, -2.0)),
        )
        graph = WalkGraph(
            "resource-world", 1, KnownMapBounds(0, 2, 1, 1, 0, 1, True),
            nodes, tuple(sorted(edges, key=lambda edge: (edge.start, edge.end))), False,
        )
        request = PlanningRequest(
            1, "resource-route", "goal", 1, "resource-world", start, goal,
            initial_resources=ResourceState((("stamina", 9.0),)),
            minimum_resources=ResourceState((("stamina", 0.0),)),
        )

        result = astar_plan(graph, request)

        self.assertIs(result.status, PlanningStatus.COMPLETE)
        self.assertEqual(tuple(item.node_id for item in result.path), (start, slow, merge, goal))
        self.assertEqual(result.final_resources, ResourceState((("stamina", 6.0),)))
        self.assertAlmostEqual(result.total_cost_seconds, 4.0)

    def test_resource_recovery_cannot_exceed_the_declared_initial_capacity(self):
        start, goal = (0, 1, 0), (1, 1, 0)
        graph = WalkGraph(
            "resource-world", 1, KnownMapBounds(0, 1, 1, 1, 0, 0, True),
            (node(start), node(goal)),
            (WalkEdge(start, goal, 1.0, (), transition("restore", 1.0, 2.0)),),
            False,
        )
        request = PlanningRequest(
            1, "resource-cap", "goal", 1, "resource-world", start, goal,
            initial_resources=ResourceState((("stamina", 5.0),)),
            minimum_resources=ResourceState((("stamina", 0.0),)),
        )

        result = astar_plan(graph, request)

        self.assertIs(result.status, PlanningStatus.COMPLETE)
        self.assertEqual(result.final_resources, ResourceState((("stamina", 5.0),)))

    def test_resource_search_rejects_an_implicit_negative_balance(self):
        start, goal = (0, 1, 0), (1, 1, 0)
        graph = WalkGraph(
            "resource-world", 1, KnownMapBounds(0, 1, 1, 1, 0, 0, True),
            (node(start), node(goal)),
            (WalkEdge(start, goal, 1.0, (), transition("overdraw", 1.0, -2.0)),),
            False,
        )
        request = PlanningRequest(
            1, "resource-overdraw", "goal", 1, "resource-world", start, goal,
            initial_resources=ResourceState((("stamina", 1.0),)),
        )

        result = astar_plan(graph, request)

        self.assertIs(result.status, PlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE)
        self.assertIsNone(result.final_resources)
        with self.assertRaisesRegex(Exception, "nonnegative"):
            ResourceState((("stamina", -1.0),))

    def test_resource_changing_walk_edges_keep_their_order_during_admission(self):
        fixture = FlatFixture("minecraft:stone")
        start, middle, goal = (0, 1, 0), (0, 1, 1), (0, 1, 2)
        graph = WalkGraph(
            fixture.session.value,
            fixture.world.view().geometry_revision,
            KnownMapBounds(0, 0, 1, 1, 0, 2, True),
            tuple(node(value) for value in (start, middle, goal)),
            (
                WalkEdge(start, middle, 1.0, (), transition("recover", 1.0, 10.0)),
                WalkEdge(middle, goal, 1.0, (), transition("consume", 1.0, -5.0)),
            ),
            False,
        )
        request = PlanningRequest(
            1, "ordered-resources", "goal", 1, fixture.session.value, start, goal,
            initial_resources=ResourceState((("stamina", 10.0),)),
            minimum_resources=ResourceState((("stamina", 0.0),)),
        )
        candidate = astar_plan(graph, request)
        self.assertEqual(candidate.final_resources, ResourceState((("stamina", 5.0),)))

        admitted = RouteAdmitter().admit(
            candidate,
            fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0)),
            expected_request_id=request.request_id,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        changes = tuple(
            action.transition.resource_change
            for action in admitted.route.action_route.actions
        )
        self.assertEqual(changes, (
            ResourceChange((("stamina", 10.0),)),
            ResourceChange((("stamina", -5.0),)),
        ))
        with self.assertRaisesRegex(Exception, "resource-changing"):
            compose_movement_transitions(
                "invalid-resource-composition", tuple(edge.transition for edge in graph.edges),
            )

    def test_action_segment_carries_the_planner_transition(self):
        planned = transition("walk-segment", 1.0)
        segment = WalkSegment(
            FixedRoute("walk", (RoutePoint(0.5, 1.0, 0.5), RoutePoint(1.5, 1.0, 0.5))),
            ((0, 1, 0), (1, 1, 0)),
            (),
            planned,
        )
        self.assertIs(segment.transition, planned)

    def test_b06_planning_and_admission_preserve_the_shared_transition(self):
        environment = load_frozen_environment(CONFIG / "environment-v1.json")
        catalog = BlockMotionCatalog.load(
            CONFIG / "block-motion-traits-v1.json",
            CONFIG / "vanilla-block-registry-1_21.json",
        )
        profile = load_ground_motion_profile(
            CONFIG / "ordinary-ground-b06-v1.json",
            environment=environment,
            catalog=catalog,
        )
        fixture = FlatFixture("minecraft:stone")
        graph = build_walk_graph(
            fixture.world.view(), KnownMapBounds(-1, 1, 1, 1, 0, 3, True), profile,
        )
        request = PlanningRequest(
            2, "b06-transition", "goal", 1, fixture.session.value,
            (0, 1, 0), (0, 1, 2),
            goal_state=GoalState(
                region=Aabb(0.25, 1.0, 2.25, 0.75, 1.5, 2.75),
                support=GoalSupport.SOLID,
                allowed_modes=frozenset({MovementMode.WALK}),
                allowed_poses=frozenset({"standing"}),
                maximum_terminal_speed_blocks_per_second=0.1,
            ),
        )
        candidate = astar_plan(graph, request)
        self.assertIs(candidate.goal_state, request.goal_state)
        self.assertTrue(candidate.segments)
        self.assertTrue(all(edge.transition is not None for edge in candidate.segments))
        self.assertTrue(all(
            edge.transition.trajectory_profile_id == profile.profile_id
            and edge.transition.risk_tags == frozenset()
            for edge in candidate.segments
        ))

        frame = fixture.frame(0, PlanarBodyState(0.5, 0.5, 0, 0, 0))
        admitted = RouteAdmitter().admit(
            candidate, frame,
            expected_request_id=request.request_id,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        self.assertIs(admitted.route.goal_state, request.goal_state)
        self.assertIs(admitted.route.action_route.goal_state, request.goal_state)
        self.assertEqual(admitted.route.action_route.final_resources,
                         candidate.final_resources)
        self.assertTrue(all(
            action.transition is not None
            for action in admitted.route.action_route.actions
        ))

    def test_action_route_completes_only_after_observed_goal_state(self):
        environment = load_frozen_environment(CONFIG / "environment-v1.json")
        catalog = BlockMotionCatalog.load(
            CONFIG / "block-motion-traits-v1.json",
            CONFIG / "vanilla-block-registry-1_21.json",
        )
        ground = load_ground_motion_profile(
            CONFIG / "ordinary-ground-b06-v1.json",
            environment=environment,
            catalog=catalog,
        )
        jump = load_jump_up_profile(
            CONFIG / "jump-up-b06-v1.json",
            environment=environment,
            catalog=catalog,
        )
        fixture = FlatFixture("minecraft:stone")
        goal = GoalState(
            region=Aabb(.49, 1.0, .49, .51, 2.0, .51),
            support=GoalSupport.SOLID,
            allowed_modes=frozenset({MovementMode.WALK}),
            allowed_poses=frozenset({"standing"}),
            maximum_terminal_speed_blocks_per_second=.001,
            required_yaw_radians=math.pi / 2,
            maximum_yaw_error_radians=.01,
        )
        segment = WalkSegment(
            FixedRoute("goal-check", (RoutePoint(.5, 1.0, .5),)),
            ((0, 1, 0),), (),
        )
        route = ActionRoute("goal-check", (segment,), goal, ResourceState())
        executor = ActionRouteExecutor(ground, jump)
        frame = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        executor.start(route, frame)

        turning = executor.decide(frame)
        self.assertIs(turning.state, ActionRouteState.RUNNING)
        self.assertIsNotNone(turning.look)
        self.assertNotEqual(turning.reason_code, "action_route_complete")

        aligned = fixture.frame(1, PlanarBodyState(.5, .5, 0, 0, math.pi / 2))
        complete = executor.decide(aligned)
        self.assertIs(complete.state, ActionRouteState.COMPLETE)
        self.assertEqual(complete.reason_code, "goal_state_satisfied")

    def test_action_route_does_not_complete_outside_declared_goal_region(self):
        environment = load_frozen_environment(CONFIG / "environment-v1.json")
        catalog = BlockMotionCatalog.load(
            CONFIG / "block-motion-traits-v1.json",
            CONFIG / "vanilla-block-registry-1_21.json",
        )
        ground = load_ground_motion_profile(
            CONFIG / "ordinary-ground-b06-v1.json",
            environment=environment,
            catalog=catalog,
        )
        jump = load_jump_up_profile(
            CONFIG / "jump-up-b06-v1.json",
            environment=environment,
            catalog=catalog,
        )
        fixture = FlatFixture("minecraft:stone")
        route = ActionRoute(
            "wrong-goal",
            (WalkSegment(
                FixedRoute("wrong-goal-walk", (RoutePoint(.5, 1.0, .5),)),
                ((0, 1, 0),), (),
            ),),
            GoalState(
                region=Aabb(1.49, 1.0, 1.49, 1.51, 2.0, 1.51),
                support=GoalSupport.SOLID,
                allowed_modes=frozenset({MovementMode.WALK}),
                allowed_poses=frozenset({"standing"}),
                maximum_terminal_speed_blocks_per_second=.1,
            ),
            ResourceState(),
        )
        executor = ActionRouteExecutor(ground, jump)
        frame = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        executor.start(route, frame)
        decision = executor.decide(frame)
        self.assertIs(decision.state, ActionRouteState.FAILED)
        self.assertEqual(decision.reason_code, "goal_state_not_satisfied")

    def test_grounded_fabric_gravity_velocity_does_not_prevent_goal_completion(self):
        environment = load_frozen_environment(CONFIG / "environment-v1.json")
        catalog = BlockMotionCatalog.load(
            CONFIG / "block-motion-traits-v1.json",
            CONFIG / "vanilla-block-registry-1_21.json",
        )
        ground = load_ground_motion_profile(
            CONFIG / "ordinary-ground-b06-v1.json",
            environment=environment,
            catalog=catalog,
        )
        jump = load_jump_up_profile(
            CONFIG / "jump-up-b06-v1.json",
            environment=environment,
            catalog=catalog,
        )
        fixture = FlatFixture("minecraft:stone")
        route = ActionRoute(
            "fabric-grounded-velocity",
            (WalkSegment(
                FixedRoute("fabric-grounded-velocity-walk", (RoutePoint(.5, 1.0, .5),)),
                ((0, 1, 0),), (),
            ),),
            GoalState(
                region=Aabb(.49, 1.0, .49, .51, 2.0, .51),
                support=GoalSupport.SOLID,
                allowed_modes=frozenset({MovementMode.WALK}),
                allowed_poses=frozenset({"standing"}),
                maximum_terminal_speed_blocks_per_second=.1,
            ),
            ResourceState(),
        )
        executor = ActionRouteExecutor(ground, jump)
        frame = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        frame = replace(frame, body=replace(
            frame.body,
            velocity_blocks_per_second=(0.0, -1.568000030517578, 0.0),
        ))
        executor.start(route, frame)

        decision = executor.decide(frame)

        self.assertIs(decision.state, ActionRouteState.COMPLETE)
        self.assertEqual(decision.reason_code, "goal_state_satisfied")

    def test_goal_alignment_obeys_cancel_input_loss_session_and_terminal_state(self):
        environment = load_frozen_environment(CONFIG / "environment-v1.json")
        catalog = BlockMotionCatalog.load(
            CONFIG / "block-motion-traits-v1.json",
            CONFIG / "vanilla-block-registry-1_21.json",
        )
        ground = load_ground_motion_profile(
            CONFIG / "ordinary-ground-b06-v1.json",
            environment=environment,
            catalog=catalog,
        )
        jump = load_jump_up_profile(
            CONFIG / "jump-up-b06-v1.json",
            environment=environment,
            catalog=catalog,
        )
        fixture = FlatFixture("minecraft:stone")
        goal = GoalState(
            region=Aabb(.49, 1.0, .49, .51, 2.0, .51),
            support=GoalSupport.SOLID,
            allowed_modes=frozenset({MovementMode.WALK}),
            allowed_poses=frozenset({"standing"}),
            maximum_terminal_speed_blocks_per_second=.1,
            required_yaw_radians=math.pi / 2,
            maximum_yaw_error_radians=.01,
        )
        route = ActionRoute(
            "goal-lifecycle",
            (WalkSegment(
                FixedRoute("goal-lifecycle-walk", (RoutePoint(.5, 1.0, .5),)),
                ((0, 1, 0),), (),
            ),),
            goal,
            ResourceState(),
        )
        initial = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))

        cancelled = ActionRouteExecutor(ground, jump)
        cancelled.start(route, initial)
        self.assertEqual(cancelled.decide(initial).reason_code, "aligning_goal_heading")
        cancelled.cancel()
        cancel_result = cancelled.decide(
            fixture.frame(1, PlanarBodyState(.5, .5, 0, 0, math.pi / 2)),
        )
        self.assertIs(cancel_result.state, ActionRouteState.CANCELLED)
        self.assertIs(
            cancelled.decide(
                fixture.frame(2, PlanarBodyState(.5, .5, 0, 0, math.pi / 2)),
            ).state,
            ActionRouteState.CANCELLED,
        )

        input_lost = ActionRouteExecutor(ground, jump)
        input_lost.start(route, initial)
        self.assertEqual(input_lost.decide(initial).reason_code, "aligning_goal_heading")
        self.assertIs(
            input_lost.decide(fixture.frame(1, PlanarBodyState(.5, .5, 0, 0, 0)),
                              input_confirmed=False).state,
            ActionRouteState.INPUT_LOST,
        )

        changed_world = ActionRouteExecutor(ground, jump)
        changed_world.start(route, initial)
        self.assertEqual(changed_world.decide(initial).reason_code,
                         "aligning_goal_heading")
        other = replace(
            fixture.frame(1, PlanarBodyState(.5, .5, 0, 0, 0)),
            session=WorldSessionId("replacement-world"),
        )
        changed = changed_world.decide(other)
        self.assertIs(changed.state, ActionRouteState.FAILED)
        self.assertEqual(changed.reason_code, "world_session_changed")


if __name__ == "__main__":
    unittest.main()

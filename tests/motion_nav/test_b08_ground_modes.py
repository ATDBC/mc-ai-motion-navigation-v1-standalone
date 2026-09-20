from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import math
import unittest

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_modes import (
    GroundModeProfiles, ModeReadiness, evaluate_ground_mode,
    load_ground_mode_profiles, movement_for_ground_mode, observed_ground_mode,
)
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, PlanningRequest, PlanningStatus,
    WalkEdge, WalkGraph, WalkNode, astar_plan,
)
from mc2p.motion_nav.fixed_route import (
    FixedRoute, FixedRouteController, FixedRouteState, RoutePoint,
)
from mc2p.motion_nav.action_route import ActionRoute, WalkSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.movement_transition import (
    CancellationMode, GoalState, GoalSupport, MovementStateClass,
    MovementTransition, ResourceChange, ResourceState,
)
from mc2p.motion_nav.world_model import Aabb
from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp
from mc2p.motion_nav.movement_transition import MovementMode
from tests.motion_nav.test_fixed_route_walk import FlatFixture
from mc2p.motion_nav.ground_motion import (
    GroundControl, PlanarBodyState, control_world_direction,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/motion-navigation"


class B08GroundModeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        environment = load_frozen_environment(CONFIG / "environment-v1.json")
        catalog = BlockMotionCatalog.load(
            CONFIG / "block-motion-traits-v1.json",
            CONFIG / "vanilla-block-registry-1_21.json",
        )
        cls.profiles = load_ground_mode_profiles(
            CONFIG / "ground-modes-b08-v1.json",
            environment=environment,
            catalog=catalog,
        )

    def frame(self, **changes):
        frame = FlatFixture().frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        return replace(frame, body=replace(frame.body, **changes))

    def test_repository_profiles_are_complete_and_independently_identified(self):
        self.assertIsInstance(self.profiles, GroundModeProfiles)
        self.assertEqual(set(self.profiles.modes), {
            MovementMode.WALK, MovementMode.SPRINT,
            MovementMode.CROUCH, MovementMode.CRAWL,
        })
        identifiers = {profile.motion.profile_id for profile in self.profiles.modes.values()}
        self.assertEqual(len(identifiers), 4)

    def test_requested_input_is_not_actual_mode_evidence(self):
        sprint = self.profiles.require(MovementMode.SPRINT)
        standing = self.frame(is_sprinting=False, food_points=20)
        self.assertIs(evaluate_ground_mode(sprint, standing.body), ModeReadiness.PENDING)
        requested = movement_for_ground_mode(sprint, MovementV1(forward=1))
        self.assertTrue(requested.sprint)
        self.assertFalse(standing.body.is_sprinting)
        confirmed = replace(standing.body, is_sprinting=True)
        self.assertIs(evaluate_ground_mode(sprint, confirmed), ModeReadiness.READY)

    def test_sprint_resource_and_crouch_pose_are_distinct_conditions(self):
        sprint = self.profiles.require(MovementMode.SPRINT)
        crouch = self.profiles.require(MovementMode.CROUCH)
        self.assertIs(
            evaluate_ground_mode(sprint, self.frame(food_points=6).body),
            ModeReadiness.RESOURCE_UNAVAILABLE,
        )
        crouching = self.frame(pose="crouching", is_sneaking=True)
        self.assertIs(evaluate_ground_mode(crouch, crouching.body), ModeReadiness.READY)
        self.assertTrue(movement_for_ground_mode(crouch, MovementV1()).sneak)

    def test_crawl_requires_observed_legal_preexisting_state(self):
        crawl = self.profiles.require(MovementMode.CRAWL)
        standing = self.frame()
        self.assertIs(evaluate_ground_mode(crawl, standing.body), ModeReadiness.INVALID_ENTRY)
        submerged = self.frame(
            pose="swimming", is_swimming=True, is_submerged_in_water=True,
        )
        self.assertIs(evaluate_ground_mode(crawl, submerged.body), ModeReadiness.INVALID_ENTRY)
        crawling = self.frame(
            pose="swimming", is_swimming=False, is_submerged_in_water=False,
        )
        self.assertIs(evaluate_ground_mode(crawl, crawling.body), ModeReadiness.READY)
        self.assertIs(observed_ground_mode(crawling.body), MovementMode.CRAWL)

    def test_walk_waits_for_observed_release_of_other_modes(self):
        walk = self.profiles.require(MovementMode.WALK)
        self.assertIs(
            evaluate_ground_mode(walk, self.frame(is_sprinting=True).body),
            ModeReadiness.PENDING,
        )
        self.assertIs(
            evaluate_ground_mode(walk, self.frame(pose="crouching", is_sneaking=True).body),
            ModeReadiness.PENDING,
        )

    def route(self):
        return FixedRoute("b08-route", (
            RoutePoint(.5, 1.0, .5), RoutePoint(.5, 1.0, 3.5),
        ))

    def test_fixed_route_requests_sprint_until_actual_state_confirms(self):
        sprint = self.profiles.require(MovementMode.SPRINT)
        fixture = FlatFixture()
        initial = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        controller = FixedRouteController(sprint.motion, mode_profile=sprint)
        controller.start(self.route(), initial)

        request = controller.decide(initial)
        self.assertIs(request.state, FixedRouteState.RUNNING)
        self.assertTrue(request.movement.sprint)
        self.assertGreater(request.movement.forward, 0)
        self.assertEqual(request.reason, "ground_mode_confirmation_pending")

        confirmed = replace(
            fixture.frame(1, PlanarBodyState(.5, .55, 0, .5, 0)).body,
            is_sprinting=True,
        )
        moving = controller.decide(replace(initial, body=confirmed))
        self.assertIs(moving.state, FixedRouteState.RUNNING)
        self.assertTrue(moving.movement.sprint)
        self.assertEqual(moving.reason, "tracking_fixed_route")

    def test_known_full_floor_does_not_require_hidden_lower_owner_cells(self):
        """A complete floor is sufficient evidence even when its substrate is unseen."""
        sprint = self.profiles.require(MovementMode.SPRINT)
        fixture = FlatFixture()
        fixture.world.invalidate(
            ObservationStamp(fixture.session, 1, 1, "test-clock", 1),
            tuple((x, -1, z) for x in range(-16, 17) for z in range(-16, 17)),
        )
        initial = fixture.frame(2, PlanarBodyState(.5, .5, 0, 0, 0))
        controller = FixedRouteController(sprint.motion, mode_profile=sprint)
        controller.start(self.route(), initial)

        decision = controller.decide(initial)

        self.assertIs(decision.state, FixedRouteState.RUNNING)
        self.assertEqual(decision.reason, "ground_mode_confirmation_pending")
        self.assertGreater(decision.movement.forward, 0)
        self.assertEqual(decision.missing_cells, ())

    def test_mode_confirmation_is_bounded_and_resource_loss_is_explicit(self):
        sprint = self.profiles.require(MovementMode.SPRINT)
        fixture = FlatFixture()
        initial = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        controller = FixedRouteController(sprint.motion, mode_profile=sprint)
        controller.start(self.route(), initial)
        result = None
        for sequence in range(sprint.confirmation_ticks + 1):
            frame = fixture.frame(sequence, PlanarBodyState(.5, .5, 0, 0, 0))
            result = controller.decide(frame)
        self.assertIs(result.state, FixedRouteState.UNSUPPORTED)
        self.assertEqual(result.reason, "ground_mode_confirmation_timeout")

        controller = FixedRouteController(sprint.motion, mode_profile=sprint)
        hungry = replace(initial, body=replace(initial.body, food_points=6))
        controller.start(self.route(), hungry)
        refused = controller.decide(hungry)
        self.assertIs(refused.state, FixedRouteState.UNSUPPORTED)
        self.assertEqual(refused.reason, "ground_mode_resource_unavailable")

    def test_cancel_and_input_loss_release_each_mode_safely(self):
        fixture = FlatFixture()
        for mode in (MovementMode.SPRINT, MovementMode.CROUCH, MovementMode.CRAWL):
            with self.subTest(mode=mode.value):
                profile = self.profiles.require(mode)
                initial = fixture.frame(1, PlanarBodyState(.5, .5, 0, .5, 0))
                changes = {}
                if mode is MovementMode.SPRINT:
                    changes = {"is_sprinting": True}
                elif mode is MovementMode.CROUCH:
                    changes = {"pose": "crouching", "is_sneaking": True}
                else:
                    changes = {"pose": "swimming", "is_swimming": False}
                body = replace(initial.body, **changes)
                if mode in {MovementMode.CROUCH, MovementMode.CRAWL}:
                    height = 1.5 if mode is MovementMode.CROUCH else .6
                    body = replace(body, body_box=replace(
                        body.body_box, max_y=body.body_box.min_y + height,
                    ))
                active = replace(initial, body=body)

                cancelling = FixedRouteController(profile.motion, mode_profile=profile)
                cancelling.start(self.route(), active)
                cancelling.cancel()
                braking = cancelling.decide(active)
                self.assertIs(braking.state, FixedRouteState.CANCELLING)
                if mode is MovementMode.CROUCH:
                    self.assertTrue(braking.movement.sneak)

                lost = FixedRouteController(profile.motion, mode_profile=profile)
                lost.start(self.route(), active)
                decision = lost.decide(active, input_confirmed=False)
                self.assertIs(decision.state, FixedRouteState.INPUT_LOST)
                self.assertEqual(decision.movement, MovementV1())

    def test_cancellation_keeps_braking_when_sprint_flag_drops(self):
        sprint = self.profiles.require(MovementMode.SPRINT)
        fixture = FlatFixture()
        initial = fixture.frame(1, PlanarBodyState(.5, .5, 0, .5, 0))
        active = replace(initial, body=replace(initial.body, is_sprinting=True))
        controller = FixedRouteController(sprint.motion, mode_profile=sprint)
        controller.start(self.route(), active)
        controller.cancel()
        self.assertIs(controller.decide(active).state, FixedRouteState.CANCELLING)

        slowing = fixture.frame(2, PlanarBodyState(.5, .55, 0, .2, 0))
        decision = controller.decide(slowing)

        self.assertIs(decision.state, FixedRouteState.CANCELLING)
        self.assertEqual(decision.reason, "cancel_braking")

    def test_camera_yaw_does_not_change_world_route_direction_for_any_mode(self):
        fixture = FlatFixture()
        for mode in (MovementMode.WALK, MovementMode.SPRINT,
                     MovementMode.CROUCH, MovementMode.CRAWL):
            with self.subTest(mode=mode.value):
                profile = self.profiles.require(mode)
                initial = fixture.frame(
                    1, PlanarBodyState(.5, .5, 0, 0, math.pi / 2),
                )
                changes = {}
                if mode is MovementMode.SPRINT:
                    changes = {"is_sprinting": True}
                elif mode is MovementMode.CROUCH:
                    changes = {"pose": "crouching", "is_sneaking": True}
                elif mode is MovementMode.CRAWL:
                    changes = {"pose": "swimming", "is_swimming": False}
                body = replace(initial.body, **changes)
                if mode in {MovementMode.CROUCH, MovementMode.CRAWL}:
                    height = 1.5 if mode is MovementMode.CROUCH else .6
                    body = replace(body, body_box=replace(
                        body.body_box, max_y=body.body_box.min_y + height,
                    ))
                frame = replace(initial, body=body)
                controller = FixedRouteController(profile.motion, mode_profile=profile)
                controller.start(self.route(), frame)
                decision = controller.decide(frame)
                world_x, world_z = control_world_direction(GroundControl(
                    decision.movement.forward, -decision.movement.strafe,
                    frame.body.yaw_radians,
                ))
                self.assertGreater(world_z, .7)
                self.assertAlmostEqual(world_x, 0.0, delta=.15)

    def test_sprint_resource_cost_cannot_discard_a_viable_walk_prefix(self):
        start, sprint_node, walk_node, merge, goal = (
            (0, 1, 0), (1, 1, 0), (0, 1, 1), (1, 1, 1), (2, 1, 1),
        )

        def node(value):
            return WalkNode(value, (value[0] + .5, float(value[1]), value[2] + .5), ())

        def edge_transition(name, mode, duration, food):
            pose = "standing"
            state = MovementStateClass(mode, pose, 0.0, 6.0)
            return MovementTransition(
                name, "fabric-1_21-motion-v1", mode, state, (state,), duration, (),
                ResourceChange((("food", food),)),
                CancellationMode.GROUND_STOP, CancellationMode.GROUND_STOP,
            )

        graph = WalkGraph(
            "b08-resource", 1, KnownMapBounds(0, 2, 1, 1, 0, 1, True),
            tuple(node(value) for value in sorted((start, sprint_node, walk_node, merge, goal))),
            tuple(sorted((
                WalkEdge(start, sprint_node, .5, (), edge_transition(
                    "sprint-fast-a", MovementMode.SPRINT, .5, -8)),
                WalkEdge(sprint_node, merge, .5, (), edge_transition(
                    "sprint-fast-b", MovementMode.SPRINT, .5, 0)),
                WalkEdge(start, walk_node, 1.5, (), edge_transition(
                    "walk-safe-a", MovementMode.WALK, 1.5, -1)),
                WalkEdge(walk_node, merge, 1.5, (), edge_transition(
                    "walk-safe-b", MovementMode.WALK, 1.5, 0)),
                WalkEdge(merge, goal, 1.0, (), edge_transition(
                    "sprint-required-finish", MovementMode.SPRINT, 1.0, -2)),
            ), key=lambda edge: (edge.start, edge.end))),
            False,
        )
        request = PlanningRequest(
            1, "b08-resource", "goal", 1, graph.world_session,
            start, goal, initial_resources=ResourceState((("food", 9.0),)),
            minimum_resources=ResourceState((("food", 0.0),)),
        )

        result = astar_plan(graph, request)

        self.assertIs(result.status, PlanningStatus.COMPLETE)
        self.assertEqual(tuple(item.node_id for item in result.path),
                         (start, walk_node, merge, goal))

    def test_crouch_waits_neutral_then_tracks_with_sneak_held(self):
        crouch = self.profiles.require(MovementMode.CROUCH)
        fixture = FlatFixture()
        initial = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        controller = FixedRouteController(crouch.motion, mode_profile=crouch)
        controller.start(self.route(), initial)
        entering = controller.decide(initial)
        self.assertEqual((entering.movement.forward, entering.movement.strafe), (0, 0))
        self.assertTrue(entering.movement.sneak)

        body = replace(
            fixture.frame(1, PlanarBodyState(.5, .5, 0, 0, 0)).body,
            pose="crouching", is_sneaking=True,
        )
        body = replace(body, body_box=replace(body.body_box, max_y=body.body_box.min_y + 1.5))
        moving = controller.decide(replace(initial, body=body))
        self.assertEqual(moving.reason, "tracking_fixed_route")
        self.assertTrue(moving.movement.sneak)

    def test_crawl_controller_rejects_entry_but_accepts_observed_crawl(self):
        crawl = self.profiles.require(MovementMode.CRAWL)
        fixture = FlatFixture()
        initial = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        refused = FixedRouteController(crawl.motion, mode_profile=crawl)
        refused.start(self.route(), initial)
        self.assertEqual(refused.decide(initial).reason, "ground_mode_invalid_entry")

        body = replace(initial.body, pose="swimming", is_swimming=True)
        body = replace(body, body_box=replace(body.body_box, max_y=body.body_box.min_y + .6))
        crawling = replace(initial, body=body)
        accepted = FixedRouteController(crawl.motion, mode_profile=crawl)
        accepted.start(self.route(), crawling)
        self.assertEqual(accepted.decide(crawling).reason, "tracking_fixed_route")

    def transition(self, mode: MovementMode) -> MovementTransition:
        profile = self.profiles.require(mode)
        state = MovementStateClass(
            mode, next(iter(profile.poses)), 0.0,
            profile.motion.maximum_speed_blocks_per_second,
        )
        return MovementTransition(
            f"b08-{mode.value}", profile.motion.environment_id, mode,
            state, (state,), 1.0, (), ResourceChange(),
            CancellationMode.GROUND_STOP, CancellationMode.GROUND_STOP,
            trajectory_profile_id=profile.motion.profile_id,
        )

    def executor(self) -> ActionRouteExecutor:
        environment = load_frozen_environment(CONFIG / "environment-v1.json")
        catalog = BlockMotionCatalog.load(
            CONFIG / "block-motion-traits-v1.json",
            CONFIG / "vanilla-block-registry-1_21.json",
        )
        jump = load_jump_up_profile(
            CONFIG / "jump-up-b06-v1.json", environment=environment, catalog=catalog,
        )
        return ActionRouteExecutor(
            self.profiles.require(MovementMode.WALK).motion,
            jump,
            ground_modes=self.profiles,
        )

    def test_action_route_selects_the_segment_mode_profile(self):
        fixture = FlatFixture()
        initial = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        route = ActionRoute("sprint-route", (
            WalkSegment(self.route(), ((0, 1, 0), (0, 1, 3)), (),
                        self.transition(MovementMode.SPRINT)),
        ))
        executor = self.executor()
        executor.start(route, initial)
        decision = executor.decide(initial)
        self.assertIs(decision.state, ActionRouteState.RUNNING)
        self.assertTrue(decision.movement.sprint)
        self.assertEqual(decision.reason_code, "ground_mode_confirmation_pending")

    def test_explicit_walk_segment_releases_an_observed_crouch(self):
        fixture = FlatFixture()
        initial = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        body = replace(initial.body, pose="crouching", is_sneaking=True)
        body = replace(body, body_box=replace(body.body_box, max_y=body.body_box.min_y + 1.5))
        crouching = replace(initial, body=body)
        route = ActionRoute("walk-after-crouch", (
            WalkSegment(self.route(), ((0, 1, 0), (0, 1, 3)), (),
                        self.transition(MovementMode.WALK)),
        ))
        executor = self.executor()
        executor.start(route, crouching)

        decision = executor.decide(crouching)

        self.assertIs(decision.state, ActionRouteState.RUNNING)
        self.assertEqual(decision.reason_code, "ground_mode_confirmation_pending")
        self.assertFalse(decision.movement.sneak)

    def test_goal_uses_observed_crouch_mode_instead_of_assuming_walk(self):
        fixture = FlatFixture()
        initial = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        body = replace(initial.body, pose="crouching", is_sneaking=True)
        body = replace(body, body_box=replace(body.body_box, max_y=body.body_box.min_y + 1.5))
        crouching = replace(initial, body=body)
        point = FixedRoute("crouch-goal", (RoutePoint(.5, 1.0, .5),))
        goal = GoalState(
            Aabb(.4, .9, .4, .6, 1.2, .6), GoalSupport.SOLID,
            frozenset({MovementMode.CROUCH}), frozenset({"crouching"}), .1,
        )
        route = ActionRoute("crouch-goal", (
            WalkSegment(point, ((0, 1, 0),), (), self.transition(MovementMode.CROUCH)),
        ), goal, ResourceState())
        executor = self.executor()
        executor.start(route, crouching)
        decision = executor.decide(crouching)
        self.assertIs(decision.state, ActionRouteState.COMPLETE)
        self.assertEqual(decision.reason_code, "goal_state_satisfied")

    def test_crawl_exit_checks_standing_clearance_before_releasing_mode(self):
        walk = self.profiles.require(MovementMode.WALK)
        fixture = FlatFixture()
        fixture.world.observe_blocks(
            ObservationStamp(fixture.session, 1, 1, "test-clock", 1),
            {(0, 2, 0): BlockGeometry.full_cube("minecraft:stone")},
        )
        initial = fixture.frame(0, PlanarBodyState(.5, .5, 0, 0, 0))
        body = replace(initial.body, pose="swimming", is_swimming=True)
        body = replace(body, body_box=replace(body.body_box, max_y=body.body_box.min_y + .6))
        crawling = replace(initial, body=body, world=fixture.world.view())
        controller = FixedRouteController(walk.motion, mode_profile=walk)
        controller.start(self.route(), crawling)
        decision = controller.decide(crawling)
        self.assertIs(decision.state, FixedRouteState.BLOCKED)
        self.assertEqual(decision.reason, "ground_mode_exit_clearance_blocked")


if __name__ == "__main__":
    unittest.main()

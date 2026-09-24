from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import mc2p.motion_nav as motion_nav
from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.air_motion import (
    AirMotionController, AirMotionProfile, AirMotionState,
    load_air_motion_profiles, query_air_motion,
)
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.action_route import (
    ControlledDropSegment, JumpGapSegment, WalkSegment,
)
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.controlled_drop import ControlledDropEdge
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.jump_gap import JumpGapEdge
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SnapshotBuildStatus,
    SurfaceControlledDropEdge, SurfaceJumpGapEdge, SurfaceWalkEdge,
    SurfacePlanningRequest, SurfacePlanningStatus, astar_surface_plan,
    build_surface_graph, dijkstra_surface_reference,
    plan_known_surface_snapshot,
)
from mc2p.motion_nav.movement_transition import (
    GoalState, GoalSupport, MovementMode, ResourceState,
)
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.support_surfaces import SurfaceNodeId, query_support_surfaces
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)
from tests.motion_nav.test_b07_step_transition import profile as step_profile
from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
from tests.motion_nav.test_jump_up import jump_profile


def air_profile(mode: MovementMode) -> AirMotionProfile:
    if mode is MovementMode.JUMP_GAP:
        return AirMotionProfile(
            "test-jump-gap", "fabric-1_21-motion-v1", mode,
            horizontal_cells=2, target_height_delta_blocks=0.0,
            jump_input=True, sprint_input=True,
            minimum_entry_speed_blocks_per_second=0.0,
            maximum_entry_speed_blocks_per_second=.1,
            entry_center_tolerance_blocks=.15,
            maximum_forward_offset_blocks=.12,
            maximum_backward_offset_blocks=.12,
            maximum_lateral_offset_blocks=.08,
            maximum_yaw_error_degrees=2.0,
            horizontal_safety_margin_blocks=0.01,
            reference_positions=(
                (0.0, 0.0, 0.0), (0.0, .42, .35),
                (0.0, .75, .8), (0.0, .85, 1.25),
                (0.0, .55, 1.7), (0.0, 0.0, 2.0),
            ),
            departure_release_progress_blocks=0.0,
            forward_release_progress_blocks=.4,
            maximum_departure_wait_ticks=4, maximum_airborne_ticks=16,
            landing_horizontal_radius_blocks=.28,
            landing_level_tolerance_blocks=.1,
            maximum_exit_speed_blocks_per_second=.15,
            maximum_settle_ticks=10, recovery_forward_blocks=.1,
            maximum_fall_distance_blocks=0.0, cost_seconds=.9,
            support_materials=frozenset({"minecraft:grass_block"}),
            minimum_food_points=7,
        )
    if mode is MovementMode.CONTROLLED_DROP:
        return AirMotionProfile(
            "test-controlled-drop", "fabric-1_21-motion-v1", mode,
            horizontal_cells=1, target_height_delta_blocks=-1.0,
            jump_input=False, sprint_input=False,
            minimum_entry_speed_blocks_per_second=0.0,
            maximum_entry_speed_blocks_per_second=.1,
            entry_center_tolerance_blocks=.12,
            maximum_forward_offset_blocks=.08,
            maximum_backward_offset_blocks=.01,
            maximum_lateral_offset_blocks=.08,
            maximum_yaw_error_degrees=2.0,
            horizontal_safety_margin_blocks=0.01,
            reference_positions=(
                (0.0, 0.0, 0.0), (0.0, 0.0, .35),
                (0.0, 0.0, .75), (0.0, 0.0, .82),
                (0.0, -.08, .82), (0.0, -.35, .92),
                (0.0, -.72, 1.0),
                (0.0, -1.0, 1.0),
            ),
            departure_release_progress_blocks=.7,
            forward_release_progress_blocks=.72,
            maximum_departure_wait_ticks=8, maximum_airborne_ticks=12,
            landing_horizontal_radius_blocks=.34,
            landing_level_tolerance_blocks=.1,
            maximum_exit_speed_blocks_per_second=.15,
            maximum_settle_ticks=10, recovery_forward_blocks=.1,
            maximum_fall_distance_blocks=1.0, cost_seconds=.7,
            support_materials=frozenset({"minecraft:grass_block"}),
        )
    raise AssertionError(mode)


def known_world(blocks: dict[tuple[int, int, int], BlockGeometry]) -> WorldKnowledge:
    session = WorldSessionId("b09-air")
    world = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "test-clock", 50_000_000)
    world.confirm_air(stamp, tuple(
        (x, y, z)
        for x in range(-2, 4)
        for y in range(-3, 6)
        for z in range(-2, 5)
        if (x, y, z) not in blocks
    ))
    world.observe_blocks(stamp, blocks)
    return world


def surface(world: WorldKnowledge, x: int, z: int, y: float):
    result = query_support_surfaces(world.view(), x, z, y - .1, y + .1)
    assert result.status is QueryStatus.FEASIBLE
    return min(result.surfaces, key=lambda item: abs(item.position[1] - y))


def frame(world: WorldKnowledge, sequence: int, position: tuple[float, float, float],
          velocity: tuple[float, float, float], *, on_ground: bool,
          yaw_radians: float = 0.0, food_points: int = 20,
          game_mode: str = "survival") -> NavigationFrame:
    stamp = ObservationStamp(
        world.session, sequence, sequence, "test-clock", sequence * 50_000_000,
    )
    x, y, z = position
    body = BodyState(
        world.session, sequence, stamp, position, velocity, yaw_radians, 0.0,
        "standing", Aabb(x - .3, y, z - .3, x + .3, y + 1.8, z + .3),
        on_ground, False, False, food_points=food_points, game_mode=game_mode,
    )
    return NavigationFrame(world.session, body, world.view(), "fabric")


class B09AirTransitionTests(unittest.TestCase):
    def test_prepare_releases_input_when_the_previous_input_was_not_confirmed(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 2, 1.0)
        controller = AirMotionController(air_profile(MovementMode.JUMP_GAP))
        initial = frame(world, 1, start.position, (0, 0, 0), on_ground=True)
        controller.start(start, end, initial)

        decision = controller.decide(initial, input_confirmed=False)

        self.assertIs(decision.state, AirMotionState.INPUT_LOST)
        self.assertEqual(decision.movement, MovementV1())
        self.assertEqual(decision.reason_code, "input_lost_before_departure")

    def test_sprint_gap_requires_seven_food_points_before_departure(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 2, 1.0)
        profile = air_profile(MovementMode.JUMP_GAP)

        low = AirMotionController(profile)
        low_frame = frame(
            world, 1, start.position, (0, 0, 0), on_ground=True,
            food_points=6,
        )
        low.start(start, end, low_frame)
        rejected = low.decide(low_frame)

        self.assertIs(rejected.state, AirMotionState.UNSUPPORTED)
        self.assertEqual(rejected.movement, MovementV1())
        self.assertEqual(rejected.reason_code, "sprint_resource_unavailable")

        enough = AirMotionController(profile)
        enough_frame = frame(
            world, 2, start.position, (0, 0, 0), on_ground=True,
            food_points=7,
        )
        enough.start(start, end, enough_frame)
        accepted = enough.decide(enough_frame)

        self.assertIs(accepted.state, AirMotionState.REQUEST_DEPARTURE)
        self.assertTrue(accepted.movement.sprint)

    def test_public_motion_navigation_entry_exports_b09_contracts(self) -> None:
        expected = {
            "AirMotionController", "AirMotionDecision", "AirMotionProfile",
            "AirMotionQuery", "AirMotionState", "ControlledDropEdge",
            "JumpGapEdge", "SurfaceControlledDropEdge", "SurfaceJumpGapEdge",
            "load_air_motion_profiles", "query_air_motion",
            "query_controlled_drop", "query_jump_gap",
        }

        self.assertTrue(expected.issubset(set(motion_nav.__all__)))
        for name in expected:
            self.assertTrue(hasattr(motion_nav, name), name)

    def test_repository_profiles_are_bound_to_environment_and_ordinary_materials(self):
        root = Path(__file__).resolve().parents[2]
        environment = load_frozen_environment(
            root / "config/motion-navigation/environment-v1.json"
        )
        catalog = BlockMotionCatalog.load(
            root / "config/motion-navigation/block-motion-traits-v1.json",
            root / "config/motion-navigation/vanilla-block-registry-1_21.json",
        )
        profiles = load_air_motion_profiles(
            root / "config/motion-navigation/air-motions-b09-v1.json",
            environment=environment, catalog=catalog,
        )

        self.assertEqual(
            {profile.mode for profile in profiles},
            {MovementMode.JUMP_GAP, MovementMode.CONTROLLED_DROP},
        )
        self.assertTrue(all(
            profile.environment_id == environment.environment_id
            and profile.support_materials == catalog.materials_for_ground_model(
                "ordinary-ground-v1"
            )
            for profile in profiles
        ))

    def test_query_accepts_only_the_profile_relation_and_known_safe_trajectory(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 2, 1.0)
        profile = air_profile(MovementMode.JUMP_GAP)

        result = query_air_motion(world.view(), start, end, profile)

        self.assertIs(result.status, QueryStatus.FEASIBLE)
        self.assertTrue(result.dependencies)
        wrong = surface(world, 0, 0, 1.0)
        self.assertIs(
            query_air_motion(world.view(), start, wrong, profile).status,
            QueryStatus.UNSUPPORTED,
        )

    def test_query_reports_blocked_and_unknown_without_treating_gap_as_floor(self):
        blocks = {
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        }
        world = known_world(blocks)
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 2, 1.0)
        profile = air_profile(MovementMode.JUMP_GAP)
        stamp = ObservationStamp(world.session, 2, 2, "test-clock", 100_000_000)
        world.observe_blocks(stamp, {
            (0, 2, 1): BlockGeometry.full_cube("minecraft:stone"),
        })
        self.assertIs(query_air_motion(
            world.view(), start, end, profile,
        ).status, QueryStatus.BLOCKED)

        world = known_world(blocks)
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 2, 1.0)
        world.invalidate(stamp, ((0, 2, 1),))
        missing = query_air_motion(world.view(), start, end, profile)
        self.assertIs(missing.status, QueryStatus.NEEDS_INFORMATION)
        self.assertIn((0, 2, 1), missing.missing_cells)

    def test_controlled_drop_is_distinct_and_never_requests_jump(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, -1, 1): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 1, 0.0)
        profile = air_profile(MovementMode.CONTROLLED_DROP)
        self.assertIs(
            query_air_motion(world.view(), start, end, profile).status,
            QueryStatus.FEASIBLE,
        )
        controller = AirMotionController(profile)
        controller.start(start, end, frame(world, 1, start.position, (0, 0, 0), on_ground=True))
        request = controller.decide(
            frame(world, 2, start.position, (0, 0, 0), on_ground=True)
        )
        self.assertIs(request.state, AirMotionState.REQUEST_DEPARTURE)
        self.assertFalse(request.movement.jump)

    def test_controlled_drop_releases_forward_before_leaving_support(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, -1, 1): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 1, 0.0)
        controller = AirMotionController(air_profile(MovementMode.CONTROLLED_DROP))
        controller.start(
            start, end, frame(world, 1, start.position, (0, 0, 0), on_ground=True),
        )
        controller.decide(
            frame(world, 2, start.position, (0, 0, 0), on_ground=True),
        )

        release = controller.decide(
            frame(world, 3, (0.5, 1.0, 1.22), (0, 0, 2.0), on_ground=True),
        )

        self.assertIs(release.state, AirMotionState.REQUEST_DEPARTURE)
        self.assertEqual(release.movement, MovementV1())
        self.assertEqual(release.reason_code, "coasting_to_drop_edge")

    def test_controlled_drop_entry_distinguishes_forward_and_backward_offset(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, -1, 1): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 1, 0.0)
        profile = air_profile(MovementMode.CONTROLLED_DROP)

        accepted = AirMotionController(profile)
        accepted.start(
            start, end,
            frame(world, 1, (0.5, 1.0, .57), (0, 0, 0), on_ground=True),
        )
        self.assertIs(
            accepted.decide(
                frame(world, 2, (0.5, 1.0, .57), (0, 0, 0), on_ground=True)
            ).state,
            AirMotionState.REQUEST_DEPARTURE,
        )

        rejected = AirMotionController(profile)
        rejected.start(
            start, end,
            frame(world, 3, (0.5, 1.0, .48), (0, 0, 0), on_ground=True),
        )
        decision = rejected.decide(
            frame(world, 4, (0.5, 1.0, .48), (0, 0, 0), on_ground=True)
        )
        self.assertIs(decision.state, AirMotionState.UNSUPPORTED)
        self.assertEqual(decision.reason_code, "entry_position_out_of_range")

    def test_controller_advances_only_from_observed_departure_and_landing(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 2, 1.0)
        profile = air_profile(MovementMode.JUMP_GAP)
        controller = AirMotionController(profile)
        controller.start(start, end, frame(world, 1, start.position, (0, 0, 0), on_ground=True))

        request = controller.decide(frame(world, 2, start.position, (0, 0, 0), on_ground=True))
        self.assertIs(request.state, AirMotionState.REQUEST_DEPARTURE)
        self.assertTrue(request.movement.jump)
        still_grounded = controller.decide(
            frame(world, 3, (0.5, 1.0, .62), (0, 0, 2.0), on_ground=True)
        )
        self.assertIs(still_grounded.state, AirMotionState.REQUEST_DEPARTURE)
        airborne = controller.decide(
            frame(world, 4, (0.5, 1.4, 1.0), (0, 5.0, 3.0), on_ground=False)
        )
        self.assertIs(airborne.state, AirMotionState.AIRBORNE)
        landed = controller.decide(
            frame(world, 5, end.position, (0, 0, 0), on_ground=True)
        )
        self.assertIs(landed.state, AirMotionState.COMPLETE)

    def test_airborne_cancel_keeps_the_calibrated_safe_tracking_input(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 2, 1.0)
        controller = AirMotionController(air_profile(MovementMode.JUMP_GAP))
        controller.start(
            start, end, frame(world, 1, start.position, (0, 0, 0), on_ground=True),
        )
        controller.decide(
            frame(world, 2, start.position, (0, 0, 0), on_ground=True),
        )
        controller.cancel()

        cancelling = controller.decide(
            frame(world, 3, (0.5, 1.42, .8274), (0, 5.0, 3.5), on_ground=False),
        )

        self.assertIs(cancelling.state, AirMotionState.CANCELLING)
        self.assertEqual(cancelling.movement.forward, 1)
        landed = controller.decide(
            frame(world, 4, end.position, (0, 0, 0), on_ground=True),
        )
        self.assertIs(landed.state, AirMotionState.CANCELLED)

    def test_airborne_progress_is_measured_from_the_observed_entry_position(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 2, 1.0)
        profile = air_profile(MovementMode.JUMP_GAP)
        controller = AirMotionController(profile)
        entry = (start.position[0], start.position[1], start.position[2] + .08)
        controller.start(
            start, end, frame(world, 1, entry, (0, 0, 0), on_ground=True),
        )
        controller.decide(frame(world, 2, entry, (0, 0, 0), on_ground=True))

        airborne = controller.decide(frame(
            world, 3, (entry[0], entry[1] + .42, entry[2] + .3274),
            (0, 5.0, 3.5), on_ground=False,
        ))

        self.assertIs(airborne.state, AirMotionState.AIRBORNE)
        self.assertEqual(airborne.movement.forward, 1)

    def test_airborne_input_loss_keeps_safe_tracking_then_reports_input_lost(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        start, end = surface(world, 0, 0, 1.0), surface(world, 0, 2, 1.0)
        controller = AirMotionController(air_profile(MovementMode.JUMP_GAP))
        controller.start(
            start, end, frame(world, 1, start.position, (0, 0, 0), on_ground=True),
        )
        controller.decide(
            frame(world, 2, start.position, (0, 0, 0), on_ground=True),
        )

        recovering = controller.decide(
            frame(world, 3, (0.5, 1.42, .8274), (0, 5.0, 3.5), on_ground=False),
            input_confirmed=False,
        )

        self.assertIs(recovering.state, AirMotionState.CANCELLING)
        self.assertEqual(recovering.movement.forward, 1)
        landed = controller.decide(
            frame(world, 4, end.position, (0, 0, 0), on_ground=True),
        )
        self.assertIs(landed.state, AirMotionState.INPUT_LOST)

    def test_surface_planning_preserves_goal_and_rejects_a_hungry_sprint_gap(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 0, 0, 1, 0, 2, True),
            ground_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        gap = next(edge for edge in graph.edges if type(edge) is SurfaceJumpGapEdge)
        end = next(node for node in graph.nodes if node.node_id == gap.end)
        goal = GoalState(
            Aabb(
                end.position[0] - .2, end.position[1] - .1, end.position[2] - .2,
                end.position[0] + .2, end.position[1] + .1, end.position[2] + .2,
            ),
            GoalSupport.SOLID, frozenset({MovementMode.WALK}),
            frozenset({"standing"}), .15,
            minimum_resources=ResourceState((("food_points", 7.0),)),
        )
        hungry = SurfacePlanningRequest(
            1, "hungry-gap", "goal", 1, graph.world_session,
            gap.start, gap.end, initial_resources=ResourceState((("food_points", 6.0),)),
        )
        rejected = astar_surface_plan(graph, hungry)
        self.assertIsNot(rejected.status, SurfacePlanningStatus.COMPLETE)

        request = SurfacePlanningRequest(
            2, "fed-gap", "goal", 1, graph.world_session,
            gap.start, gap.end,
            initial_resources=ResourceState((("food_points", 7.0),)),
            minimum_resources=ResourceState((("food_points", 7.0),)),
            goal_state=goal,
        )
        candidate = astar_surface_plan(graph, request)

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertEqual(candidate.final_resources,
                         ResourceState((("food_points", 7.0),)))
        self.assertIs(candidate.goal_state, goal)
        self.assertEqual(
            candidate.segments[0].transition.minimum_entry_resources,
            ResourceState((("food_points", 7.0),)),
        )
        hungry_admission = RouteAdmitter().admit_surface(
            candidate,
            frame(
                world, 3, candidate.path[0].position, (0, 0, 0),
                on_ground=True, food_points=6,
            ),
            expected_request_id=request.request_id,
            goal_id=request.goal_id, goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIs(hungry_admission.status, AdmissionStatus.REJECTED)
        self.assertEqual(hungry_admission.reason,
                         "route_resources_below_minimum")
        initial = frame(
            world, 4, candidate.path[0].position, (0, 0, 0),
            on_ground=True, food_points=7,
        )
        admitted = RouteAdmitter().admit_surface(
            candidate, initial, expected_request_id=request.request_id,
            goal_id=request.goal_id, goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        self.assertIs(admitted.route.goal_state, goal)
        self.assertIs(admitted.route.action_route.goal_state, goal)
        self.assertEqual(admitted.route.action_route.final_resources,
                         candidate.final_resources)

    def test_surface_graph_adds_typed_gap_and_drop_edges_and_astar_matches_dijkstra(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
            (1, -1, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 2, True),
            ground_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),
                          air_profile(MovementMode.CONTROLLED_DROP)),
        )
        gap = next(edge for edge in graph.edges if type(edge) is SurfaceJumpGapEdge)
        drop = next(edge for edge in graph.edges if type(edge) is SurfaceControlledDropEdge)
        self.assertIs(type(gap.air_edge), JumpGapEdge)
        self.assertIs(type(drop.air_edge), ControlledDropEdge)

        request = SurfacePlanningRequest(
            1, "b09-route", "goal", 1, graph.world_session,
            gap.start, drop.end,
            initial_resources=ResourceState((("food_points", 20.0),)),
        )
        candidate = astar_surface_plan(graph, request)
        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertAlmostEqual(
            candidate.total_cost_seconds,
            dijkstra_surface_reference(graph, request.start, request.goal),
        )
        self.assertEqual(
            tuple(type(edge) for edge in candidate.segments),
            (SurfaceJumpGapEdge, SurfaceControlledDropEdge),
        )

        start_frame = frame(
            world, 1, graph.nodes[[node.node_id for node in graph.nodes].index(
                request.start
            )].position, (0, 0, 0), on_ground=True,
        )
        admitted = RouteAdmitter().admit_surface(
            candidate, start_frame, expected_request_id=request.request_id,
            goal_id=request.goal_id, goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        assert admitted.route is not None
        self.assertEqual(
            tuple(type(action) for action in admitted.route.action_route.actions),
            (JumpGapSegment, ControlledDropSegment),
        )

        executor = ActionRouteExecutor(
            ground_profile(), jump_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),
                          air_profile(MovementMode.CONTROLLED_DROP)),
        )
        executor.start(
            admitted.route.action_route, start_frame,
            require_verified_gap_motion=False,
        )
        first = executor.decide(start_frame)
        self.assertTrue(first.movement.jump)
        executor.decide(frame(
            world, 2, (0.5, 1.4, 1.1), (0, 4.0, 3.0), on_ground=False,
        ))
        handoff = executor.decide(frame(
            world, 3, (0.5, 1.0, 2.5), (0, 0, 0), on_ground=True,
            yaw_radians=-math.pi / 2,
        ))
        self.assertEqual(handoff.action_index, 1)
        self.assertFalse(handoff.movement.jump)
        executor.decide(frame(
            world, 4, (1.0, .6, 2.5), (2.0, -4.0, 0), on_ground=False,
            yaw_radians=-math.pi / 2,
        ))
        completed = executor.decide(frame(
            world, 5, (1.5, 0.0, 2.5), (0, 0, 0), on_ground=True,
            yaw_radians=-math.pi / 2,
        ))
        self.assertIs(completed.state, ActionRouteState.COMPLETE)

    def test_formal_snapshot_planner_considers_step_and_drop_for_same_surfaces(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (1, -1, 0): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        bounds = KnownMapBounds(0, 1, 0, 1, 0, 0, True)
        progress = KnownMapSnapshotBuilder(world.view(), bounds).advance(
            world.view(), 10_000,
        )
        self.assertIs(progress.status, SnapshotBuildStatus.COMPLETE)
        graph = build_surface_graph(
            world.view(), bounds, ground_profile(),
            replace(
                step_profile(), maximum_down_height_blocks=1.0,
                cost_seconds=1.0,
            ),
            air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
        )
        start = next(node.node_id for node in graph.nodes
                     if node.node_id.column_x == 0)
        goal = next(node.node_id for node in graph.nodes
                    if node.node_id.column_x == 1)
        request = SurfacePlanningRequest(
            9, "parallel-step-drop", "goal", 1, world.session.value,
            start, goal,
        )

        candidate = plan_known_surface_snapshot(
            progress.snapshot, ground_profile(),
            replace(
                step_profile(), maximum_down_height_blocks=1.0,
                cost_seconds=1.0,
            ),
            request,
            air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
        )

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertIs(type(candidate.segments[0]), SurfaceControlledDropEdge)
        self.assertAlmostEqual(candidate.total_cost_seconds, .7)

    def test_flat_middle_support_rejects_gap_before_expensive_sweep(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 1): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        with patch(
            "mc2p.motion_nav.known_map_planner.query_jump_gap",
            side_effect=AssertionError("flat support must reject JumpGap early"),
        ):
            graph = build_surface_graph(
                world.view(), KnownMapBounds(0, 0, 1, 1, 0, 2, True),
                ground_profile(), step_profile(),
                air_profiles=(air_profile(MovementMode.JUMP_GAP),),
            )

        self.assertFalse(any(type(edge) is SurfaceJumpGapEdge
                             for edge in graph.edges))

    def test_formal_air_planning_reports_missing_top_clearance(self):
        world = known_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        bounds = KnownMapBounds(0, 0, 1, 1, 0, 2, True)
        progress = KnownMapSnapshotBuilder(world.view(), bounds).advance(
            world.view(), 10_000,
        )
        request = SurfacePlanningRequest(
            10, "missing-air-clearance", "goal", 1, world.session.value,
            SurfaceNodeId(0, 0, 1, 0), SurfaceNodeId(0, 2, 1, 0),
        )

        candidate = plan_known_surface_snapshot(
            progress.snapshot, ground_profile(), step_profile(), request,
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )

        self.assertIs(candidate.status, SurfacePlanningStatus.UNSUPPORTED)
        self.assertEqual(candidate.reasons,
                         ("insufficient_top_clearance",))

    def test_walk_gap_walk_route_is_built_and_can_be_planned_in_background(self):
        world = known_world({
            (0, 0, -1): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 2): BlockGeometry.full_cube("minecraft:grass_block"),
            (0, 0, 3): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        bounds = KnownMapBounds(0, 0, 1, 1, -1, 3, True,
                                extra_top_clearance_cells=2)
        graph = build_surface_graph(
            world.view(), bounds, ground_profile(), step_profile(),
            air_profiles=(air_profile(MovementMode.JUMP_GAP),),
        )
        start = next(node.node_id for node in graph.nodes
                     if node.node_id.column_z == -1)
        goal = next(node.node_id for node in graph.nodes
                    if node.node_id.column_z == 3)
        request = SurfacePlanningRequest(
            2, "b09-walk-gap-walk", "goal", 1, graph.world_session,
            start, goal,
            initial_resources=ResourceState((("food_points", 20.0),)),
        )
        candidate = astar_surface_plan(graph, request)
        self.assertEqual(
            tuple(type(edge) for edge in candidate.segments),
            (SurfaceWalkEdge, SurfaceJumpGapEdge, SurfaceWalkEdge),
        )
        admitted = RouteAdmitter().admit_surface(
            candidate,
            frame(world, 1, next(node.position for node in graph.nodes
                                 if node.node_id == start),
                  (0, 0, 0), on_ground=True),
            expected_request_id=request.request_id,
            goal_id=request.goal_id, goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        assert admitted.route is not None
        self.assertEqual(
            tuple(type(action) for action in admitted.route.action_route.actions),
            (WalkSegment, JumpGapSegment, WalkSegment),
        )

        builder = KnownMapSnapshotBuilder(world.view(), bounds)
        progress = builder.advance(world.view(), 10_000)
        self.assertIs(progress.status, SnapshotBuildStatus.COMPLETE)
        worker = PlannerWorker()
        try:
            worker.submit_surface_snapshot(
                progress.snapshot, ground_profile(), step_profile(), request,
                air_profiles=(air_profile(MovementMode.JUMP_GAP),),
            )
            result = None
            deadline = time.perf_counter() + 3
            while result is None and time.perf_counter() < deadline:
                result = worker.poll_latest()
                time.sleep(.01)
            self.assertIsNotNone(result)
            self.assertIs(result.status, SurfacePlanningStatus.COMPLETE)
            self.assertEqual(
                tuple(type(edge) for edge in result.segments),
                (SurfaceWalkEdge, SurfaceJumpGapEdge, SurfaceWalkEdge),
            )
        finally:
            worker.close()


if __name__ == "__main__":
    unittest.main()

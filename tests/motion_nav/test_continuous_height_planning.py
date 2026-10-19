from dataclasses import replace
import math
import unittest
from unittest.mock import patch

from mc2p.motion_nav import known_map_planner as planner_module

from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds,
    KnownMapSnapshotBuilder,
    SnapshotBuildStatus,
    SurfacePlanningRequest,
    SurfacePlanningStatus,
    SurfaceWalkEdge,
    SurfaceControlledDropEdge,
    build_surface_graph,
    plan_known_surface_snapshot,
    seconds_to_planning_ticks,
    astar_surface_plan,
)
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.ground_traversal import (
    GroundTraversalProofCache,
    verify_ground_traversal,
)
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.action_route import WalkSegment
from mc2p.motion_nav.ground_motion import PlanarBodyState
from mc2p.motion_nav.movement_transition import MovementMode
from tests.motion_nav.test_b07_surface_planning import (
    mixed_height_world,
    ordinary_profile,
)
from tests.motion_nav.test_b07_step_transition import profile as step_profile
from tests.motion_nav.test_b07_support_surfaces import surface_world
from tests.motion_nav.test_ground_traversal import traversal_fixture
from tests.motion_nav.test_b10_gap_solver import fixture as gap_fixture
from tests.motion_nav.test_fixed_route_walk import FlatFixture
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge,
)


class ContinuousHeightPlanningTests(unittest.TestCase):
    def test_seconds_are_rounded_up_to_positive_planning_ticks(self):
        self.assertEqual(seconds_to_planning_ticks(0.0001), 1)
        self.assertEqual(seconds_to_planning_ticks(0.05), 1)
        self.assertEqual(seconds_to_planning_ticks(0.05001), 2)

    def test_adjacent_half_block_height_change_has_walk_candidate_and_step_fallback(self):
        world = mixed_height_world()

        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )
        forward = tuple(
            edge for edge in graph.edges
            if edge.start.column_x == 0 and edge.end.column_x == 1
        )

        walks = tuple(edge for edge in forward if type(edge) is SurfaceWalkEdge)
        self.assertEqual(len(walks), 1)
        self.assertTrue(walks[0].requires_ground_traversal_proof)
        self.assertTrue(any(type(edge).__name__ == "StepEdge" for edge in forward))

    def test_surface_route_reports_integer_tick_cost_as_search_truth(self):
        world = mixed_height_world()
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )
        request = SurfacePlanningRequest(
            1, "continuous-height", "continuous-height-goal", 1,
            world.session.value,
            SurfaceNodeId(0, 0, 0, 0),
            SurfaceNodeId(1, 0, 1, 0),
        )

        candidate = astar_surface_plan(graph, request)

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertIs(type(candidate.total_cost_ticks), int)
        self.assertEqual(
            candidate.total_cost_seconds,
            candidate.total_cost_ticks * ordinary_profile().tick_seconds,
        )

    def test_snapshot_planner_attaches_formal_proof_before_using_low_height_walk(self):
        world = surface_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (1, 0, 0): BlockGeometry(
                "minecraft:smooth_stone_slab", "boxes",
                (Aabb(0, 0, 0, 1, .5, 1),),
            ),
            (2, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        world.confirm_air(
            ObservationStamp(
                world.session, 2, 2, "test-clock", 50_000_000,
            ),
            tuple((2, y, z) for y in range(-2, 6) for z in range(-1, 2)
                  if (2, y, z) != (2, 0, 0)),
        )
        bounds = KnownMapBounds(0, 2, 0, 1, 0, 0, True)
        built = KnownMapSnapshotBuilder(world.view(), bounds).advance(
            world.view(), 10_000,
        )
        self.assertIs(built.status, SnapshotBuildStatus.COMPLETE)
        anchor, _, _, _ = gap_fixture()
        entry = replace(
            anchor.physics_state,
            session=world.session,
            movement_tick_id=0,
            position=(0.5, 1.0, 0.5),
            velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
            yaw_radians=-1.5707963267948966,
        )
        request = SurfacePlanningRequest(
            2, "proved-height", "proved-height-goal", 1,
            world.session.value,
            SurfaceNodeId(0, 0, 1, 0),
            SurfaceNodeId(2, 0, 1, 0),
            entry_physics_state=entry,
        )

        candidate = plan_known_surface_snapshot(
            built.snapshot, ordinary_profile(),
            replace(step_profile(), cost_seconds=.8), request,
        )

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertTrue(
            candidate.ground_traversal_plans,
            msg=(candidate.total_cost_ticks,
                 tuple(type(edge).__name__ for edge in candidate.segments)),
        )
        self.assertTrue(any(type(edge) is SurfaceWalkEdge
                            for edge in candidate.segments))
        self.assertEqual(
            candidate.total_cost_ticks,
            candidate.ground_traversal_plans[0].estimated_ticks,
        )
        frame = FlatFixture().frame(
            0, PlanarBodyState(0.5, 0.5, 0.0, 0.0, 0.0),
        )
        action_route = RouteAdmitter._surface_action_route(
            candidate, frame, 0.0, (), "proved-height-route",
        )
        self.assertIsNotNone(action_route)
        self.assertIs(type(action_route.actions[0]), WalkSegment)
        self.assertIsNotNone(action_route.actions[0].traversal_plan)

        stamp = ObservationStamp(
            world.session, 3, 3, "test-clock", 150_000_000,
        )
        position = (0.55, 1.0, 0.5)
        body = BodyState(
            world.session, 3, stamp, position, (0.0, 0.0, 0.0),
            -1.5707963267948966, 0.0, "standing",
            Aabb(.25, 1.0, .2, .85, 2.8, .8),
            True, False, False,
        )
        admitted = RouteAdmitter().admit_surface(
            candidate,
            NavigationFrame(world.session, body, world.view(), "fixture"),
            expected_request_id=request.request_id,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)

    def test_ground_proof_cache_is_bounded_and_revision_keyed_by_caller(self):
        state, route, world = traversal_fixture()
        result = verify_ground_traversal(
            state, route, world, ordinary_profile(), maximum_ticks=80,
        )
        cache = GroundTraversalProofCache(2)
        cache.put(("revision", 1), result)
        cache.put(("revision", 2), result)
        self.assertIs(cache.get(("revision", 1)), result)
        cache.put(("revision", 3), result)

        self.assertIsNone(cache.get(("revision", 2)))
        self.assertIs(cache.get(("revision", 1)), result)
        self.assertIs(cache.get(("revision", 3)), result)

    def test_mixed_rise_and_drop_route_keeps_its_ground_proof(self):
        from tests.motion_nav.test_b09_air_transitions import air_profile, frame

        session = surface_world({}).session
        world = WorldKnowledge(session)
        observed = ObservationStamp(
            session, 1, 1, "test-clock", 50_000_000,
        )
        blocks = {
            (0, 0, z): BlockGeometry.full_cube("minecraft:stone")
            for z in range(6)
        }
        blocks[(0, 1, 2)] = BlockGeometry(
            "minecraft:smooth_stone_slab", "boxes",
            (Aabb(0, 0, 0, 1, .5, 1),),
        )
        blocks[(0, 1, 3)] = BlockGeometry.full_cube("minecraft:stone")
        world.confirm_air(observed, tuple(
            (x, y, z)
            for x in range(-2, 3)
            for y in range(-2, 6)
            for z in range(-1, 7)
            if (x, y, z) not in blocks
        ))
        world.observe_blocks(observed, blocks)
        bounds = KnownMapBounds(
            -1, 1, 0, 3, -1, 6, True, extra_top_clearance_cells=2,
        )
        built = KnownMapSnapshotBuilder(world.view(), bounds).advance(
            world.view(), 100_000,
        )
        anchor, _, _, _ = gap_fixture()
        entry = replace(
            anchor.physics_state,
            session=session,
            position=(.5, 1.0, .5),
            movement_tick_id=0,
            yaw_radians=0.0,
            velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
        )
        request = SurfacePlanningRequest(
            1, "mixed-height", "mixed-height-goal", 1, session.value,
            SurfaceNodeId(0, 0, 1, 0),
            SurfaceNodeId(0, 5, 1, 0),
            entry_physics_state=entry,
        )
        drop_profile = replace(
            air_profile(MovementMode.CONTROLLED_DROP),
            support_materials=frozenset({
                "minecraft:stone", "minecraft:smooth_stone_slab",
            }),
        )
        candidate = plan_known_surface_snapshot(
            built.snapshot, ordinary_profile(),
            replace(step_profile(), cost_seconds=.8), request,
            air_profiles=(drop_profile,),
        )

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertTrue(candidate.ground_traversal_plans)
        self.assertTrue(any(
            type(edge) is SurfaceControlledDropEdge
            for edge in candidate.segments
        ))
        admitted = RouteAdmitter().admit_surface(
            candidate,
            frame(world, 2, (.5, 1.0, .5), (0.0, 0.0, 0.0), on_ground=True),
            expected_request_id=request.request_id,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
            changed_cells=(),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)

    def test_consecutive_one_block_descents_share_one_ground_traversal_proof(self):
        from mc2p.contracts.action_v1 import MovementV1
        from mc2p.motion_nav.online_motion import project_movement_command
        from mc2p.motion_nav.physics_1_21 import step
        from mc2p.motion_nav.physics_adapter import PhysicsWorldView
        from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
        from tests.motion_nav.test_b09_air_transitions import air_profile
        from tests.motion_nav.test_continuous_descent import world_and_anchor

        anchor, physics_world = world_and_anchor(stair_count=4)
        world = physics_world._world
        bounds = KnownMapBounds(0, 0, 59, 64, 0, 5, True)
        snapshot = KnownMapSnapshotBuilder(world, bounds).advance(
            world, 100_000,
        ).snapshot
        request = SurfacePlanningRequest(
            1, "continuous-four-step-descent", "descent-goal", 1,
            world.session.value,
            SurfaceNodeId(0, 0, 64, 0),
            SurfaceNodeId(0, 4, 60, 0),
            entry_physics_state=anchor.physics_state,
        )

        ground = replace(
            ordinary_profile(),
            support_materials=frozenset({"minecraft:grass_block"}),
        )
        candidate = plan_known_surface_snapshot(
            snapshot, ground, step_profile(), request,
            air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
        )

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertEqual(
            tuple(type(edge) for edge in candidate.segments),
            (SurfaceWalkEdge,) * 4,
        )
        self.assertEqual(len(candidate.ground_traversal_plans), 1)
        proof = candidate.ground_traversal_plans[0]
        self.assertEqual(len(proof.surface_node_path), 5)

        # The comparison uses the same 1.21 calculator and world.  It holds
        # forward until the final support is reached, then releases to the
        # same <=0.1 block/s terminal condition as the formal proof.
        state = anchor.physics_state
        reference_ticks = None
        braking = False
        for tick in range(1, 100):
            at_goal = (
                state.position[2] >= 4.15
                and abs(state.position[1] - 60.0) <= .10
                and state.on_ground
            )
            braking = braking or at_goal
            speed = (
                state.velocity_blocks_per_tick[0] ** 2
                + state.velocity_blocks_per_tick[2] ** 2
            ) ** .5 * 20.0
            if braking and speed <= .10:
                reference_ticks = tick - 1
                break
            projected = project_movement_command(
                state,
                MovementV1() if braking else MovementV1(forward=1),
                movement_yaw_radians=0.0,
            )
            calculated = step(
                state, projected.tick_input,
                PhysicsWorldView(world, JAVA_1_21_RULESET),
                JAVA_1_21_RULESET,
            )
            self.assertIsNotNone(calculated.next_state)
            state = calculated.next_state
        self.assertIsNotNone(reference_ticks)
        self.assertLessEqual(
            proof.estimated_ticks,
            int(reference_ticks * 1.3),
            (proof.estimated_ticks, reference_ticks),
        )

        without_anchor = plan_known_surface_snapshot(
            snapshot,
            ground,
            step_profile(),
            replace(request, request_id="descent-without-anchor",
                    entry_physics_state=None),
            air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
        )
        self.assertIs(without_anchor.status, SurfacePlanningStatus.COMPLETE)
        self.assertTrue(all(
            type(edge) is SurfaceControlledDropEdge
            for edge in without_anchor.segments
        ))
        self.assertFalse(without_anchor.ground_traversal_plans)

    def test_isolated_one_block_descent_stays_a_controlled_drop(self):
        from tests.motion_nav.test_b09_air_transitions import air_profile
        from tests.motion_nav.test_continuous_descent import world_and_anchor

        anchor, physics_world = world_and_anchor(direct_height=1)
        world = physics_world._world
        snapshot = KnownMapSnapshotBuilder(
            world, KnownMapBounds(0, 0, 63, 64, 0, 1, True),
        ).advance(world, 100_000).snapshot
        request = SurfacePlanningRequest(
            1, "isolated-one-block-drop", "drop-goal", 1,
            world.session.value,
            SurfaceNodeId(0, 0, 64, 0),
            SurfaceNodeId(0, 1, 63, 0),
            entry_physics_state=anchor.physics_state,
        )

        ground = replace(
            ordinary_profile(),
            support_materials=frozenset({"minecraft:grass_block"}),
        )
        candidate = plan_known_surface_snapshot(
            snapshot, ground, step_profile(), request,
            air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
        )

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertEqual(len(candidate.segments), 1)
        self.assertIs(type(candidate.segments[0]), SurfaceControlledDropEdge)
        self.assertFalse(candidate.ground_traversal_plans)

    def test_ground_after_air_uses_step_fallback_without_repeated_research(self):
        from tests.motion_nav.test_b09_air_transitions import air_profile, frame

        session = surface_world({}).session
        world = WorldKnowledge(session)
        observed = ObservationStamp(
            session, 1, 1, "test-clock", 50_000_000,
        )
        grass = BlockGeometry.full_cube("minecraft:grass_block")
        slab = BlockGeometry(
            "minecraft:smooth_stone_slab", "boxes",
            (Aabb(0, 0, 0, 1, .5, 1),),
        )
        columns: list[list[int | tuple[int, BlockGeometry]]] = [
            [63, 64], [63, 64], [63], [63],
        ]
        full_height = 63
        for row in range(8):
            if row % 2 == 0:
                columns.append(
                    list(range(63, full_height + 1))
                    + [(full_height + 1, slab)]
                )
            else:
                full_height += 1
                columns.append(list(range(63, full_height + 1)))
        columns.append(list(columns[-1]))
        blocks = {}
        air = set()
        for z in range(-2, len(columns) + 2):
            column = columns[min(max(z, 0), len(columns) - 1)]
            solids = {
                item if type(item) is int else item[0]:
                grass if type(item) is int else item[1]
                for item in column
            }
            for x in range(-11, 12):
                blocks[(x, 62, z)] = grass
                for y in range(63, 75):
                    if y in solids:
                        blocks[(x, y, z)] = solids[y]
                    else:
                        air.add((x, y, z))
        world.observe_blocks(observed, blocks)
        world.confirm_air(observed, tuple(sorted(air)))
        view = world.view()

        def top(column):
            value = column[-1]
            if type(value) is int:
                return float(value + 1)
            return float(value[0]) + value[1].boxes[0].max_y

        start_position = (.5, top(columns[0]), .5)
        goal_position = (.5, top(columns[-1]), len(columns) - .5)

        def node(position):
            found = query_support_surfaces(
                view, math.floor(position[0]), math.floor(position[2]),
                position[1] - .1, position[1] + .1,
            )
            return min(
                found.surfaces,
                key=lambda value: abs(value.position[1] - position[1]),
            ).node_id

        snapshot = KnownMapSnapshotBuilder(
            view,
            KnownMapBounds(
                -10, 10, 63, 71, -1, len(columns), True,
                extra_top_clearance_cells=2,
            ),
        ).advance(view, 1_000_000).snapshot
        anchor, _, _, _ = gap_fixture()
        entry = replace(
            anchor.physics_state,
            session=session,
            position=start_position,
            movement_tick_id=0,
            yaw_radians=0.0,
            velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
        )
        request = SurfacePlanningRequest(
            1, "post-air-step-fallback", "post-air-goal", 1,
            session.value, node(start_position), node(goal_position),
            entry_physics_state=entry,
        )
        calls = 0
        original_plain = planner_module._plain_search
        original_resource = planner_module._resource_aware_search

        def counted_plain(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_plain(*args, **kwargs)

        def counted_resource(*args, **kwargs):
            nonlocal calls
            calls += 1
            return original_resource(*args, **kwargs)

        with (
            patch.object(planner_module, "_plain_search", counted_plain),
            patch.object(
                planner_module, "_resource_aware_search", counted_resource,
            ),
        ):
            candidate = plan_known_surface_snapshot(
                snapshot,
                replace(
                    ordinary_profile(),
                    support_materials=frozenset({
                        "minecraft:grass_block",
                        "minecraft:smooth_stone_slab",
                    }),
                ),
                step_profile(),
                request,
                air_profiles=(air_profile(MovementMode.CONTROLLED_DROP),),
            )

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertLessEqual(calls, 3)
        first_air = next(
            index for index, edge in enumerate(candidate.segments)
            if type(edge) is SurfaceControlledDropEdge
        )
        self.assertFalse(any(
            type(edge) is SurfaceWalkEdge
            and edge.requires_ground_traversal_proof
            for edge in candidate.segments[first_air + 1:]
        ))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from dataclasses import replace
import time
from unittest.mock import patch

from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SnapshotBuildStatus,
    SurfacePlanningRequest, SurfacePlanningStatus,
    SurfaceGraph, SurfaceJumpUpEdge, SurfaceNode, _SurfaceExpander,
    astar_surface_plan, build_surface_graph,
    dijkstra_surface_reference, plan_known_surface_snapshot,
)
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.step_transition import StepEdge
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.support_surfaces import (
    HorizontalRegion, SupportSurface, SurfaceNodeId,
)
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)
from tests.motion_nav.test_b07_step_transition import profile as step_profile
from tests.motion_nav.test_b07_support_surfaces import surface_world
from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
from tests.motion_nav.test_jump_up import jump_profile


def mixed_height_world():
    return surface_world({
        (0, 0, 0): BlockGeometry(
            "minecraft:smooth_stone_slab", "boxes",
            (Aabb(0, 0, 0, 1, .5, 1),),
        ),
        (1, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
    })


def ordinary_profile():
    return replace(
        ground_profile(),
        support_materials=frozenset({
            "minecraft:smooth_stone_slab", "minecraft:stone",
        }),
    )


def flat_surface_world(size: int) -> WorldKnowledge:
    session = WorldSessionId("b07-flat-surface")
    world = WorldKnowledge(session)
    observed = ObservationStamp(session, 1, 1, "test-clock", 50_000_000)
    world.confirm_air(observed, tuple(
        (x, y, z)
        for x in range(-1, size + 1)
        for y in range(-1, 4)
        for z in range(-1, size + 1)
    ))
    world.observe_blocks(observed, {
        (x, 0, z): BlockGeometry.full_cube("minecraft:stone")
        for x in range(size)
        for z in range(size)
    })
    return world


class B07SurfacePlanningTests(unittest.TestCase):
    def test_parallel_motion_edges_keep_distinct_planner_states(self):
        world = mixed_height_world()
        base = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )
        forward = next(
            edge for edge in base.edges
            if edge.start.column_x == 0 and edge.end.column_x == 1
        )
        fast_transition = replace(
            forward.transition,
            trajectory_profile_id="parallel-fast-profile",
            duration_seconds=forward.cost_seconds / 2,
        )
        fast = replace(
            forward,
            profile_id="parallel-fast-profile",
            cost_seconds=forward.cost_seconds / 2,
            transition=fast_transition,
        )
        graph = SurfaceGraph(
            base.world_session, base.geometry_revision, base.bounds, base.nodes,
            tuple(sorted((forward, fast), key=lambda edge: (
                edge.start, edge.end, edge.transition.trajectory_profile_id,
            ))),
            False,
        )
        request = SurfacePlanningRequest(
            5, "parallel-surface", "parallel-goal", 1,
            world.session.value, forward.start, forward.end,
        )

        candidate = astar_surface_plan(graph, request)

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertEqual(
            candidate.segments[0].transition.trajectory_profile_id,
            "parallel-fast-profile",
        )
        self.assertEqual(
            tuple(state.node_id for state in candidate.planner_states),
            (forward.start, forward.end),
        )
        self.assertIsNotNone(candidate.planner_states[-1].movement_mode)
        self.assertTrue(all(
            state.heading is None for state in candidate.planner_states
        ))

    def test_surface_snapshot_search_expands_lazily_without_materializing_graph(self):
        size = 20
        world = flat_surface_world(size)
        bounds = KnownMapBounds(0, size - 1, 1, 1, 0, size - 1, True)
        progress = KnownMapSnapshotBuilder(world.view(), bounds).advance(
            world.view(), 1_000_000,
        )
        self.assertIs(progress.status, SnapshotBuildStatus.COMPLETE)
        request = SurfacePlanningRequest(
            4, "lazy-surface", "lazy-goal", 1, world.session.value,
            SurfaceNodeId(0, 0, 1, 0),
            SurfaceNodeId(size - 1, size - 1, 1, 0),
        )

        with patch(
            "mc2p.motion_nav.known_map_planner.build_surface_graph",
            side_effect=AssertionError("snapshot planning must stay lazy"),
        ):
            candidate = plan_known_surface_snapshot(
                progress.snapshot, ordinary_profile(), step_profile(), request,
            )

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertLessEqual(candidate.expanded_nodes, 2 * size)

    def test_non_diagonal_flat_search_does_not_expand_the_whole_rectangle(self):
        size = 100
        world = flat_surface_world(size)
        bounds = KnownMapBounds(0, size - 1, 1, 1, 0, size - 1, True)
        progress = KnownMapSnapshotBuilder(world.view(), bounds).advance(
            world.view(), 1_000_000,
        )
        request = SurfacePlanningRequest(
            5, "off-axis-surface", "off-axis-goal", 1,
            world.session.value, SurfaceNodeId(0, 0, 1, 0),
            SurfaceNodeId(99, 33, 1, 0), maximum_planning_seconds=5.0,
        )

        candidate = plan_known_surface_snapshot(
            progress.snapshot, ordinary_profile(), step_profile(), request,
        )

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertLessEqual(candidate.expanded_nodes, 150)

    def test_adjacent_columns_with_same_representative_keep_positive_walk_cost(self):
        world = flat_surface_world(2)
        expander = _SurfaceExpander(
            world.view(), KnownMapBounds(0, 1, 1, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )
        region = HorizontalRegion(0, 0, 1, 1)
        first = SurfaceNode(SupportSurface(
            SurfaceNodeId(0, 0, 1, 0), (1.0, 1.0, .5), region,
            1.0, ("minecraft:stone",), (),
        ))
        second = SurfaceNode(SupportSurface(
            SurfaceNodeId(1, 0, 1, 0), (1.0, 1.0, .5),
            HorizontalRegion(1, 0, 2, 1), 1.0,
            ("minecraft:stone",), (),
        ))
        with patch(
            "mc2p.motion_nav.known_map_planner._surface_walk_query",
            return_value=(QueryStatus.FEASIBLE, ()),
        ):
            edge = expander._build_edge(first, second)

        self.assertGreater(edge.cost_seconds, 0.0)

    def test_b07_ground_profile_adds_only_declared_shape_materials(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        environment = load_frozen_environment(
            root / "config/motion-navigation/environment-v1.json"
        )
        catalog = BlockMotionCatalog.load(
            root / "config/motion-navigation/block-motion-traits-v1.json",
            root / "config/motion-navigation/vanilla-block-registry-1_21.json",
        )
        motion = load_ground_motion_profile(
            root / "config/motion-navigation/ordinary-ground-b07-v1.json",
            environment=environment, catalog=catalog,
        )

        self.assertTrue({
            "minecraft:smooth_stone_slab", "minecraft:oak_stairs",
            "minecraft:white_carpet", "minecraft:snow", "minecraft:dirt_path",
        }.issubset(motion.support_materials))

    def test_two_treads_inside_one_stair_cell_are_connected(self):
        world = surface_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:oak_stairs", "boxes", (
                    Aabb(0, 0, 0, 1, .5, 1),
                    Aabb(0, .5, .5, 1, 1, 1),
                ),
            ),
        })
        motion = replace(
            ground_profile(),
            support_materials=frozenset({"minecraft:oak_stairs"}),
        )

        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 0, 0, 1, 0, 0, True),
            motion, step_profile(),
        )

        self.assertEqual(tuple(node.position[1] for node in graph.nodes), (.5, 1.0))
        self.assertEqual(
            {edge.direction for edge in graph.edges if type(edge) is StepEdge},
            {"up", "down"},
        )

    def test_graph_keeps_fractional_surfaces_and_typed_step_edges(self):
        world = mixed_height_world()
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )

        heights = {node.surface.node_id: node.surface.position[1]
                   for node in graph.nodes}
        self.assertIn(.5, heights.values())
        self.assertIn(1.0, heights.values())
        step_edges = [edge for edge in graph.edges if type(edge) is StepEdge]
        self.assertEqual({edge.direction for edge in step_edges}, {"up", "down"})
        self.assertTrue(all(edge.dependencies for edge in step_edges))

    def test_surface_astar_matches_independent_dijkstra(self):
        world = mixed_height_world()
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )
        start = next(node.node_id for node in graph.nodes
                     if node.surface.position[0] == .5)
        goal = next(node.node_id for node in graph.nodes
                    if node.surface.position[0] == 1.5)
        request = SurfacePlanningRequest(
            1, "surface-request", "surface-goal", 1,
            world.session.value, start, goal,
        )

        candidate = astar_surface_plan(graph, request)
        reference = dijkstra_surface_reference(graph, start, goal)

        self.assertIs(candidate.status, SurfacePlanningStatus.COMPLETE)
        self.assertAlmostEqual(candidate.total_cost_seconds, reference)
        self.assertEqual(candidate.path[0].node_id, start)
        self.assertEqual(candidate.path[-1].node_id, goal)

    def test_missing_column_prevents_a_complete_scope_claim(self):
        world = mixed_height_world()
        world.invalidate(world.view().cell((0, 0, 0)).stamp, ((1, 1, 0),))

        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )

        self.assertFalse(graph.complete_scope)

    def test_surface_search_runs_in_the_existing_background_worker(self):
        world = mixed_height_world()
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 0, 1, 0, 0, True),
            ordinary_profile(), step_profile(),
        )
        request = SurfacePlanningRequest(
            2, "surface-worker", "surface-goal", 1, world.session.value,
            graph.nodes[0].node_id, graph.nodes[-1].node_id,
        )
        worker = PlannerWorker()
        try:
            worker.submit_surface(graph, request)
            result = None
            deadline = time.perf_counter() + 3
            while result is None and time.perf_counter() < deadline:
                result = worker.poll_latest()
                time.sleep(.01)
            self.assertIsNotNone(result)
            self.assertIs(result.status, SurfacePlanningStatus.COMPLETE)
        finally:
            worker.close()

    def test_surface_snapshot_graph_construction_also_runs_in_worker(self):
        world = mixed_height_world()
        bounds = KnownMapBounds(0, 1, 0, 1, 0, 0, True)
        graph = build_surface_graph(
            world.view(), bounds, ordinary_profile(), step_profile(),
        )
        builder = KnownMapSnapshotBuilder(world.view(), bounds)
        progress = builder.advance(world.view(), 10_000)
        self.assertIs(progress.status, SnapshotBuildStatus.COMPLETE)
        request = SurfacePlanningRequest(
            3, "surface-snapshot-worker", "surface-goal", 1,
            world.session.value, graph.nodes[0].node_id, graph.nodes[-1].node_id,
        )
        worker = PlannerWorker(debug_delay_seconds=.1)
        try:
            started = time.perf_counter()
            worker.submit_surface_snapshot(
                progress.snapshot, ordinary_profile(), step_profile(), request,
            )
            self.assertLess(time.perf_counter() - started, .05)
            result = None
            deadline = time.perf_counter() + 3
            while result is None and time.perf_counter() < deadline:
                result = worker.poll_latest()
                time.sleep(.01)
            self.assertIsNotNone(result)
            self.assertIs(result.status, SurfacePlanningStatus.COMPLETE)
        finally:
            worker.close()

    def test_surface_graph_hands_one_block_rise_to_existing_jump_up(self):
        world = surface_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (1, 1, 0): BlockGeometry.full_cube("minecraft:grass_block"),
        })
        graph = build_surface_graph(
            world.view(), KnownMapBounds(0, 1, 1, 2, 0, 0, True),
            ground_profile(), step_profile(), jump_profile(),
        )

        jumps = [edge for edge in graph.edges if type(edge) is SurfaceJumpUpEdge]
        self.assertEqual(len(jumps), 1)
        self.assertEqual(jumps[0].jump_edge.end[1] - jumps[0].jump_edge.start[1], 1)


if __name__ == "__main__":
    unittest.main()

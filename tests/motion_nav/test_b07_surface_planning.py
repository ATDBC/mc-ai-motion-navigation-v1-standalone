from __future__ import annotations

import unittest
from dataclasses import replace
import time

from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SnapshotBuildStatus,
    SurfacePlanningRequest, SurfacePlanningStatus,
    SurfaceJumpUpEdge, astar_surface_plan, build_surface_graph,
    dijkstra_surface_reference,
)
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.step_transition import StepEdge
from mc2p.motion_nav.planner_worker import PlannerWorker
from mc2p.motion_nav.world_model import Aabb, BlockGeometry
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


class B07SurfacePlanningTests(unittest.TestCase):
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

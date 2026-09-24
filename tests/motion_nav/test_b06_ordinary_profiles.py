from __future__ import annotations

from pathlib import Path
import unittest

from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, FixedRouteState, RoutePoint
from mc2p.motion_nav.ground_motion import PlanarBodyState, load_ground_motion_profile
from mc2p.motion_nav.jump_up import (
    JumpUpController, JumpUpState, load_jump_up_profile, query_jump_up,
)
from mc2p.motion_nav.known_map_planner import KnownMapBounds, build_walk_graph
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, ObservationStamp
from tests.motion_nav.test_fixed_route_walk import FlatFixture


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/motion-navigation"


class B06OrdinaryProfilesTests(unittest.TestCase):
    def test_profile_metadata_records_the_fabric_replacement_check(self):
        import json

        expected = sorted(self.catalog.materials_for_ground_model("ordinary-ground-v1"))
        for name in ("ordinary-ground-b06-v1.json", "jump-up-b06-v1.json"):
            with self.subTest(name=name):
                document = json.loads((CONFIG / name).read_text("utf-8"))
                validation = document["validation"]
                self.assertEqual(
                    validation["material_equivalence_status"],
                    "passed_b06_fabric_replacement_check",
                )
                self.assertEqual(validation["material_equivalence_materials"], expected)
                self.assertTrue(validation["material_equivalence_run"].startswith(
                    "artifacts/fabric-deployment/"
                ))

    @classmethod
    def setUpClass(cls):
        cls.environment = load_frozen_environment(CONFIG / "environment-v1.json")
        cls.catalog = BlockMotionCatalog.load(
            CONFIG / "block-motion-traits-v1.json",
            CONFIG / "vanilla-block-registry-1_21.json",
        )

    def test_new_profiles_resolve_materials_from_one_ground_model(self):
        ground = load_ground_motion_profile(
            CONFIG / "ordinary-ground-b06-v1.json",
            environment=self.environment,
            catalog=self.catalog,
        )
        jump = load_jump_up_profile(
            CONFIG / "jump-up-b06-v1.json",
            environment=self.environment,
            catalog=self.catalog,
        )
        expected = frozenset({
            "minecraft:dirt", "minecraft:glass", "minecraft:grass_block",
            "minecraft:oak_planks", "minecraft:stone",
        })
        self.assertEqual(ground.support_materials, expected)
        self.assertEqual(jump.support_materials, expected)
        self.assertEqual(ground.ground_model_id, "ordinary-ground-v1")
        self.assertEqual(jump.ground_model_id, "ordinary-ground-v1")
        self.assertEqual(ground.environment_id, self.environment.environment_id)
        self.assertEqual(jump.environment_id, self.environment.environment_id)

    def test_each_ordinary_representative_builds_walk_and_jump_edges(self):
        ground = load_ground_motion_profile(
            CONFIG / "ordinary-ground-b06-v1.json",
            environment=self.environment,
            catalog=self.catalog,
        )
        jump = load_jump_up_profile(
            CONFIG / "jump-up-b06-v1.json",
            environment=self.environment,
            catalog=self.catalog,
        )
        for material in sorted(ground.support_materials):
            with self.subTest(material=material):
                fixture = FlatFixture(material)
                graph = build_walk_graph(
                    fixture.world.view(), KnownMapBounds(-1, 1, 1, 1, 0, 2, True), ground,
                )
                self.assertIn((0, 1, 1), {node.node_id for node in graph.nodes})

                stamp = ObservationStamp(fixture.session, 2, 2, "test-clock", 100_000_000)
                fixture.world.observe_blocks(stamp, {
                    (0, 1, 1): BlockGeometry.full_cube(material),
                })
                fixture.world.confirm_air(stamp, tuple(
                    (0, y, z) for y in (1, 2, 3, 4) for z in (0, 1)
                    if (0, y, z) != (0, 1, 1)
                ))
                self.assertIs(
                    query_jump_up(fixture.world.view(), (0, 1, 0), (0, 2, 1), jump).status,
                    QueryStatus.FEASIBLE,
                )

    def test_special_surface_remains_outside_the_ordinary_profiles(self):
        ground = load_ground_motion_profile(
            CONFIG / "ordinary-ground-b06-v1.json",
            environment=self.environment,
            catalog=self.catalog,
        )
        fixture = FlatFixture("minecraft:ice")
        graph = build_walk_graph(
            fixture.world.view(), KnownMapBounds(-1, 1, 1, 1, 0, 2, True), ground,
        )
        self.assertTrue(graph.has_unsupported)
        self.assertNotIn((0, 1, 1), {node.node_id for node in graph.nodes})

    def test_special_empty_block_is_rejected_by_planning_and_direct_execution(self):
        ground = load_ground_motion_profile(
            CONFIG / "ordinary-ground-b06-v1.json",
            environment=self.environment,
            catalog=self.catalog,
        )
        fixture = FlatFixture("minecraft:stone")
        stamp = ObservationStamp(fixture.session, 2, 2, "test-clock", 100_000_000)
        fixture.world.observe_blocks(stamp, {
            (0, 1, 1): BlockGeometry.empty("minecraft:cobweb"),
        })
        graph = build_walk_graph(
            fixture.world.view(), KnownMapBounds(0, 0, 1, 1, 0, 2, True), ground,
        )
        self.assertTrue(graph.has_unsupported)
        self.assertNotIn((0, 1, 1), {node.node_id for node in graph.nodes})

        controller = FixedRouteController(ground)
        frame = fixture.frame(2, PlanarBodyState(.5, 1.5, 0, 0, 0))
        controller.start(FixedRoute(
            "cobweb-route", (RoutePoint(.5, 1, .5), RoutePoint(.5, 1, 2.5)),
        ), frame)
        self.assertIs(controller.decide(frame).state, FixedRouteState.UNSUPPORTED)

    def test_ordinary_material_with_abnormal_shape_is_rejected_as_support(self):
        ground = load_ground_motion_profile(
            CONFIG / "ordinary-ground-b06-v1.json",
            environment=self.environment,
            catalog=self.catalog,
        )
        fixture = FlatFixture("minecraft:stone")
        stamp = ObservationStamp(fixture.session, 2, 2, "test-clock", 100_000_000)
        fixture.world.observe_blocks(stamp, {
            (0, 0, 1): BlockGeometry(
                "minecraft:stone", "boxes", (Aabb(0, .5, 0, 1, 1, 1),),
            ),
        })
        graph = build_walk_graph(
            fixture.world.view(), KnownMapBounds(0, 0, 1, 1, 0, 2, True), ground,
        )
        self.assertTrue(graph.has_unsupported)
        self.assertNotIn((0, 1, 1), {node.node_id for node in graph.nodes})

    def test_jump_up_rejects_deferred_volume_block(self):
        jump = load_jump_up_profile(
            CONFIG / "jump-up-b06-v1.json",
            environment=self.environment,
            catalog=self.catalog,
        )
        fixture = FlatFixture("minecraft:stone")
        stamp = ObservationStamp(fixture.session, 2, 2, "test-clock", 100_000_000)
        fixture.world.observe_blocks(stamp, {
            (0, 1, 1): BlockGeometry.full_cube("minecraft:stone"),
            (0, 2, 0): BlockGeometry.empty("minecraft:cobweb"),
        })
        fixture.world.confirm_air(stamp, tuple(
            (0, y, z) for y in (1, 2, 3, 4) for z in (0, 1)
            if (0, y, z) not in {(0, 1, 1), (0, 2, 0)}
        ))
        query = query_jump_up(fixture.world.view(), (0, 1, 0), (0, 2, 1), jump)
        self.assertIs(query.status, QueryStatus.UNSUPPORTED)

        controller = JumpUpController(jump)
        frame = fixture.frame(2, PlanarBodyState(.5, .5, 0, 0, 0))
        controller.start((0, 1, 0), (0, 2, 1), frame)
        self.assertIs(controller.decide(frame).state, JumpUpState.UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()

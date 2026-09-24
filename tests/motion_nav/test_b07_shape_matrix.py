from __future__ import annotations

from pathlib import Path
import unittest

from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog, MotionEffect, TraitStatus
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.geometry import QueryStatus, sweep
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.step_transition import load_step_profile, query_step
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import Aabb, BlockGeometry
from tests.motion_nav.test_b07_support_surfaces import surface_world


ROOT = Path(__file__).resolve().parents[2]


class B07ShapeMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = load_frozen_environment(
            ROOT / "config/motion-navigation/environment-v1.json"
        )
        cls.catalog = BlockMotionCatalog.load(
            ROOT / "config/motion-navigation/block-motion-traits-v1.json",
            ROOT / "config/motion-navigation/vanilla-block-registry-1_21.json",
        )
        cls.ground = load_ground_motion_profile(
            ROOT / "config/motion-navigation/ordinary-ground-b07-v1.json",
            environment=cls.environment, catalog=cls.catalog,
        )
        cls.step = load_step_profile(
            ROOT / "config/motion-navigation/step-b07-v1.json",
            environment=cls.environment,
        )

    def test_declared_partial_surfaces_keep_real_heights(self):
        cases = (
            ("minecraft:smooth_stone_slab", (Aabb(0, 0, 0, 1, .5, 1),), .5),
            ("minecraft:smooth_stone_slab", (Aabb(0, .5, 0, 1, 1, 1),), 1.0),
            ("minecraft:white_carpet", (Aabb(0, 0, 0, 1, 1 / 16, 1),), 1 / 16),
            ("minecraft:snow", (Aabb(0, 0, 0, 1, 1 / 8, 1),), 1 / 8),
            ("minecraft:snow", (Aabb(0, 0, 0, 1, 3 / 8, 1),), 3 / 8),
            ("minecraft:snow", (Aabb(0, 0, 0, 1, 1 / 2, 1),), 1 / 2),
            ("minecraft:snow", (Aabb(0, 0, 0, 1, 5 / 8, 1),), 5 / 8),
            ("minecraft:dirt_path", (Aabb(0, 0, 0, 1, 15 / 16, 1),), 15 / 16),
        )
        for material, boxes, expected in cases:
            with self.subTest(material=material, height=expected):
                geometry = BlockGeometry(material, "boxes", boxes)
                classification = self.catalog.classify(geometry)
                result = query_support_surfaces(
                    surface_world({(0, 0, 0): geometry}).view(),
                    0, 0, 0.0, 1.0,
                )
                self.assertIs(classification.status, TraitStatus.SUPPORTED)
                self.assertIn(expected, tuple(item.position[1] for item in result.surfaces))

    def test_straight_and_corner_stairs_keep_both_treads(self):
        shapes = (
            (
                Aabb(0, 0, 0, 1, .5, 1),
                Aabb(0, .5, .5, 1, 1, 1),
            ),
            (
                Aabb(0, 0, 0, 1, .5, 1),
                Aabb(.5, .5, .5, 1, 1, 1),
            ),
        )
        for boxes in shapes:
            with self.subTest(boxes=len(boxes)):
                result = query_support_surfaces(surface_world({
                    (0, 0, 0): BlockGeometry("minecraft:oak_stairs", "boxes", boxes),
                }).view(), 0, 0, 0.0, 1.0)
                self.assertEqual(tuple(item.position[1] for item in result.surfaces), (.5, 1.0))

    def test_profile_accepts_half_block_and_rejects_taller_snow_step(self):
        half_world = surface_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (1, 1, 0): BlockGeometry("minecraft:snow", "boxes", (Aabb(0, 0, 0, 1, .5, 1),)),
        })
        low = query_support_surfaces(half_world.view(), 0, 0, 1, 2).surfaces[0]
        high = query_support_surfaces(half_world.view(), 1, 0, 1, 2).surfaces[0]
        self.assertIs(query_step(half_world.view(), low, high, self.step).status,
                      QueryStatus.FEASIBLE)

        tall_world = surface_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (1, 0, 0): BlockGeometry.full_cube("minecraft:grass_block"),
            (1, 1, 0): BlockGeometry("minecraft:snow", "boxes", (Aabb(0, 0, 0, 1, .625, 1),)),
        })
        low = query_support_surfaces(tall_world.view(), 0, 0, 1, 2).surfaces[0]
        high = query_support_surfaces(tall_world.view(), 1, 0, 1, 2).surfaces[0]
        result = query_step(tall_world.view(), low, high, self.step)
        self.assertIs(result.status, QueryStatus.UNSUPPORTED)
        self.assertEqual(result.reason_code, "step_height_outside_profile")

    def test_fence_wall_bars_and_trapdoor_use_collision_not_names(self):
        obstacles = (
            BlockGeometry("minecraft:oak_fence", "boxes", (Aabb(.375, 0, 0, .625, 1.5, 1),)),
            BlockGeometry("minecraft:cobblestone_wall", "boxes", (Aabb(.25, 0, 0, .75, 1.5, 1),)),
            BlockGeometry("minecraft:iron_bars", "boxes", (Aabb(.45, 0, 0, .55, 1, 1),)),
            BlockGeometry("minecraft:oak_trapdoor", "boxes", (Aabb(.45, 0, 0, .55, 1, 1),)),
        )
        body = Aabb(-.3, 0, .2, .3, 1.8, .8)
        for obstacle in obstacles:
            with self.subTest(material=obstacle.material_key):
                world = surface_world({(0, 0, 0): obstacle})
                self.assertIs(sweep(body, (1.0, 0.0, 0.0), world.view()).status,
                              QueryStatus.BLOCKED)

        open_side = surface_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:oak_trapdoor", "boxes",
                (Aabb(0, 0, 0, .1875, 1, 1),),
            ),
        })
        offset_body = Aabb(.3, 0, -.3, .9, 1.8, .3)
        self.assertIs(sweep(offset_body, (0.0, 0.0, 1.0), open_side.view()).status,
                      QueryStatus.FEASIBLE)

    def test_farmland_remains_deferred_for_world_change_risk(self):
        result = self.catalog.classify(BlockGeometry(
            "minecraft:farmland", "boxes", (Aabb(0, 0, 0, 1, 15 / 16, 1),),
        ))
        self.assertIs(result.status, TraitStatus.DEFERRED)
        self.assertIn(MotionEffect.WORLD_CHANGE_RISK, result.effects)


if __name__ == "__main__":
    unittest.main()

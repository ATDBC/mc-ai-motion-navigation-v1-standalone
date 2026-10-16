from __future__ import annotations

from pathlib import Path
import unittest

from mc2p.motion_nav.block_motion_traits import (
    BlockMotionCatalog,
    MotionEffect,
    TraitStatus,
)
from mc2p.motion_nav.world_model import Aabb, BlockGeometry


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = ROOT / "config/motion-navigation/vanilla-block-registry-1_21.json"
TRAITS = ROOT / "config/motion-navigation/block-motion-traits-v1.json"


class B06BlockMotionTraitsTests(unittest.TestCase):
    def setUp(self):
        self.catalog = BlockMotionCatalog.load(TRAITS, REGISTRY)

    def test_registry_snapshot_covers_the_frozen_vanilla_blockstate_set(self):
        self.assertEqual(self.catalog.minecraft_version, "1.21")
        self.assertEqual(len(self.catalog.registered_materials), 1062)
        self.assertIn("minecraft:air", self.catalog.registered_materials)
        self.assertIn("minecraft:stone", self.catalog.registered_materials)
        self.assertIn("minecraft:powder_snow", self.catalog.registered_materials)

    def test_confirmed_ordinary_full_cubes_share_one_motion_model(self):
        results = tuple(self.catalog.classify(BlockGeometry.full_cube(material)) for material in (
            "minecraft:grass_block",
            "minecraft:stone",
            "minecraft:dirt",
            "minecraft:oak_planks",
            "minecraft:glass",
        ))
        self.assertTrue(all(result.status is TraitStatus.SUPPORTED for result in results))
        self.assertEqual({result.ground_model_id for result in results}, {"ordinary-ground-v1"})
        self.assertTrue(all(result.effects == frozenset() for result in results))

    def test_geometry_and_special_effects_prevent_unsafe_ordinary_fallback(self):
        same_id_different_shape = self.catalog.classify(BlockGeometry(
            "minecraft:stone", "boxes", (Aabb(0, 0, 0, 1, 0.5, 1),),
        ))
        self.assertIs(same_id_different_shape.status, TraitStatus.UNSUPPORTED)
        self.assertEqual(same_id_different_shape.reason_code, "ordinary_geometry_mismatch")

        ice = self.catalog.classify(BlockGeometry.full_cube("minecraft:ice"))
        self.assertIs(ice.status, TraitStatus.DEFERRED)
        self.assertIn(MotionEffect.SLIPPERY, ice.effects)
        self.assertIsNone(ice.ground_model_id)

        cobweb = self.catalog.classify(BlockGeometry.empty("minecraft:cobweb"))
        self.assertIs(cobweb.status, TraitStatus.DEFERRED)
        self.assertIn(MotionEffect.VOLUME_SLOWING, cobweb.effects)

        water = self.catalog.classify(BlockGeometry.empty("minecraft:water", fluid=True))
        self.assertIs(water.status, TraitStatus.DEFERRED)
        self.assertIn(MotionEffect.FLUID, water.effects)

    def test_unregistered_behavior_and_unknown_id_are_not_treated_as_ordinary(self):
        known_but_unclassified = self.catalog.classify(
            BlockGeometry.full_cube("minecraft:diamond_block")
        )
        self.assertIs(known_but_unclassified.status, TraitStatus.UNSUPPORTED)
        self.assertEqual(known_but_unclassified.reason_code, "motion_behavior_unclassified")

        unknown = self.catalog.classify(BlockGeometry.full_cube("example:future_block"))
        self.assertIs(unknown.status, TraitStatus.UNSUPPORTED)
        self.assertEqual(unknown.reason_code, "material_not_in_frozen_registry")

    def test_b07_whitelisted_partial_shapes_reuse_ordinary_ground_dynamics(self):
        representatives = (
            ("minecraft:smooth_stone_slab", (Aabb(0, 0, 0, 1, .5, 1),)),
            ("minecraft:oak_stairs", (
                Aabb(0, 0, 0, 1, .5, 1),
                Aabb(0, .5, .5, 1, 1, 1),
            )),
            ("minecraft:white_carpet", (Aabb(0, 0, 0, 1, 1 / 16, 1),)),
            ("minecraft:snow", (Aabb(0, 0, 0, 1, 3 / 8, 1),)),
            ("minecraft:dirt_path", (Aabb(0, 0, 0, 1, 15 / 16, 1),)),
        )

        for material, boxes in representatives:
            with self.subTest(material=material):
                result = self.catalog.classify(BlockGeometry(material, "boxes", boxes))
                self.assertIs(result.status, TraitStatus.SUPPORTED)
                self.assertEqual(result.ground_model_id, "ordinary-ground-v1")
                self.assertEqual(result.reason_code, "ordinary_shape_supported")

        double_slab = self.catalog.classify(
            BlockGeometry.full_cube("minecraft:smooth_stone_slab")
        )
        self.assertIs(double_slab.status, TraitStatus.SUPPORTED)
        self.assertEqual(double_slab.reason_code, "ordinary_shape_full_cube_supported")


if __name__ == "__main__":
    unittest.main()

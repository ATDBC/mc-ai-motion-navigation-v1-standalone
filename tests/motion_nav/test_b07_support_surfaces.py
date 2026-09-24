from __future__ import annotations

import unittest

from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.support_surfaces import SurfaceNodeId, query_support_surfaces
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)


def surface_world(blocks, *, known_y=range(-2, 6)):
    session = WorldSessionId("b07-surfaces")
    world = WorldKnowledge(session)
    observed = ObservationStamp(session, 1, 1, "test-clock", 50_000_000)
    world.confirm_air(observed, tuple(
        (x, y, z)
        for x in range(-1, 2)
        for y in known_y
        for z in range(-1, 2)
    ))
    world.observe_blocks(observed, blocks)
    return world


class B07SupportSurfaceTests(unittest.TestCase):
    def test_bottom_and_top_slabs_keep_their_actual_surface_height(self):
        bottom = surface_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:smooth_stone_slab", "boxes",
                (Aabb(0, 0, 0, 1, .5, 1),),
            ),
        })
        top = surface_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:smooth_stone_slab", "boxes",
                (Aabb(0, .5, 0, 1, 1, 1),),
            ),
        })

        lower = query_support_surfaces(bottom.view(), 0, 0, 0.0, 2.0)
        upper = query_support_surfaces(top.view(), 0, 0, 0.0, 2.0)

        self.assertIs(lower.status, QueryStatus.FEASIBLE)
        self.assertEqual(tuple(surface.position[1] for surface in lower.surfaces), (.5,))
        self.assertIs(upper.status, QueryStatus.FEASIBLE)
        self.assertEqual(tuple(surface.position[1] for surface in upper.surfaces), (1.0,))

    def test_same_column_surfaces_have_distinct_stable_ids(self):
        world = surface_world({
            (0, 0, 0): BlockGeometry.full_cube("minecraft:stone"),
            (0, 4, 0): BlockGeometry.full_cube("minecraft:stone"),
        }, known_y=range(-2, 8))

        first = query_support_surfaces(world.view(), 0, 0, 1.0, 5.0)
        second = query_support_surfaces(world.view(), 0, 0, 1.0, 5.0)

        self.assertEqual(first.surfaces, second.surfaces)
        self.assertEqual(tuple(surface.position[1] for surface in first.surfaces), (1.0, 5.0))
        self.assertEqual(
            tuple(surface.node_id for surface in first.surfaces),
            (SurfaceNodeId(0, 0, 1, 0), SurfaceNodeId(0, 0, 5, 0)),
        )

    def test_stair_upper_tread_is_a_standable_surface_but_fence_top_is_not(self):
        stair = surface_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:oak_stairs", "boxes",
                (
                    Aabb(0, 0, 0, 1, .5, 1),
                    Aabb(0, .5, .5, 1, 1, 1),
                ),
            ),
        })
        fence = surface_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:oak_fence", "boxes",
                (Aabb(.375, 0, .375, .625, 1.5, .625),),
            ),
        })

        stair_result = query_support_surfaces(stair.view(), 0, 0, 0.0, 2.0)
        fence_result = query_support_surfaces(fence.view(), 0, 0, 0.0, 2.0)

        self.assertIs(stair_result.status, QueryStatus.FEASIBLE)
        self.assertEqual(
            tuple(surface.position[1] for surface in stair_result.surfaces),
            (.5, 1.0),
        )
        self.assertLess(stair_result.surfaces[0].position[2], .5)
        self.assertIs(fence_result.status, QueryStatus.BLOCKED)
        self.assertEqual(fence_result.surfaces, ())

    def test_adjacent_boxes_at_same_height_form_one_surface_component(self):
        world = surface_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:oak_stairs", "boxes",
                (
                    Aabb(0, 0, 0, .5, 1, 1),
                    Aabb(.5, 0, 0, 1, 1, 1),
                ),
            ),
        })

        result = query_support_surfaces(world.view(), 0, 0, 0.0, 2.0)

        self.assertIs(result.status, QueryStatus.FEASIBLE)
        self.assertEqual(len(result.surfaces), 1)
        self.assertEqual(result.surfaces[0].position, (.5, 1.0, .5))

    def test_carpet_and_snow_layers_preserve_fractional_height(self):
        carpet = surface_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:white_carpet", "boxes",
                (Aabb(0, 0, 0, 1, 1 / 16, 1),),
            ),
        })
        snow = surface_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:snow", "boxes",
                (Aabb(0, 0, 0, 1, 6 / 16, 1),),
            ),
        })

        carpet_result = query_support_surfaces(carpet.view(), 0, 0, 0.0, 1.0)
        snow_result = query_support_surfaces(snow.view(), 0, 0, 0.0, 1.0)

        self.assertEqual(carpet_result.surfaces[0].position[1], 1 / 16)
        self.assertEqual(snow_result.surfaces[0].position[1], 6 / 16)

    def test_unknown_column_fact_is_not_silently_ignored(self):
        session = WorldSessionId("b07-surface-unknown")
        world = WorldKnowledge(session)

        result = query_support_surfaces(world.view(), 0, 0, 0.0, 2.0)

        self.assertIs(result.status, QueryStatus.NEEDS_INFORMATION)
        self.assertTrue(result.missing_cells)
        self.assertEqual(result.surfaces, ())

    def test_surface_dependencies_include_shape_owner_and_clearance(self):
        world = surface_world({
            (0, 0, 0): BlockGeometry(
                "minecraft:dirt_path", "boxes",
                (Aabb(0, 0, 0, 1, 15 / 16, 1),),
            ),
        })

        result = query_support_surfaces(world.view(), 0, 0, 0.0, 2.0)

        self.assertIs(result.status, QueryStatus.FEASIBLE)
        self.assertIn((0, 0, 0), result.dependencies)
        self.assertIn((0, 0, 0), result.surfaces[0].dependencies)


if __name__ == "__main__":
    unittest.main()

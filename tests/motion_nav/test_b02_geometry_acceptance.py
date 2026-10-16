import json
import math
from pathlib import Path
import random
import unittest

from mc2p.motion_nav.geometry import QueryStatus, query_support, required_cells_for_sweep, sweep
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)


ROOT = Path(__file__).resolve().parents[2]


def known_world(body, delta, blocks=(), air=()):
    session = WorldSessionId("b02-geometry")
    stamp = ObservationStamp(session, 1, 1, "test-clock", 1)
    world = WorldKnowledge(session)
    cells = set(required_cells_for_sweep(body, delta))
    cells.update(position for position, _ in blocks)
    cells.update(air)
    world.confirm_air(stamp, tuple(sorted(cells)))
    if blocks:
        world.observe_blocks(stamp, dict(blocks))
    return world.view()


def intersects(a, b):
    return (a.max_x > b.min_x and a.min_x < b.max_x
            and a.max_y > b.min_y and a.min_y < b.max_y
            and a.max_z > b.min_z and a.min_z < b.max_z)


class B02GeometryAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        doc = json.loads((ROOT / "config/motion-navigation/reference-scenes-v1.json").read_text("utf-8"))
        cls.scenes = {scene["id"]: scene for scene in doc["scenes"]}

    def test_s00_open_ground_has_clear_body_volume_and_support(self):
        self.assertEqual(self.scenes["S00"]["identity"]["case"], "empty")
        body = Aabb(1.2, -60, 1.2, 1.8, -58.2, 1.8)
        floor = [((x, -61, z), BlockGeometry.full_cube("minecraft:stone"))
                 for x in range(1, 2) for z in range(1, 5)]
        world = known_world(body, (0, 0, 3), floor)
        self.assertIs(sweep(body, (0, 0, 3), world).status, QueryStatus.FEASIBLE)
        self.assertGreater(query_support(body, world).support_fraction, 0.99)

    def test_s02_corner_wall_blocks_the_continuous_sweep(self):
        self.assertEqual(self.scenes["S02"]["identity"]["case"], "corner_prelook")
        body = Aabb(0.2, 0, 0.2, 0.8, 1.8, 0.8)
        wall = ((0, 0, 2), BlockGeometry.full_cube("minecraft:stone"))
        result = sweep(body, (0, 0, 3), known_world(body, (0, 0, 3), (wall,)))
        self.assertIs(result.status, QueryStatus.BLOCKED)
        self.assertGreater(result.first_collision_fraction, 0)
        self.assertLess(result.first_collision_fraction, 1)

    def test_s04_floating_wall_blocks_headroom_even_with_visible_floor(self):
        self.assertEqual(self.scenes["S04"]["identity"]["floating_wall_y"], -59)
        body = Aabb(1.2, -60, 1.2, 1.8, -58.2, 1.8)
        blocks = (
            ((1, -61, 1), BlockGeometry.full_cube("minecraft:stone")),
            ((1, -59, 2), BlockGeometry.full_cube("minecraft:stone")),
        )
        world = known_world(body, (0, 0, 2), blocks)
        self.assertGreater(query_support(body, world).support_fraction, 0.99)
        self.assertIs(sweep(body, (0, 0, 2), world).status, QueryStatus.BLOCKED)

    def test_s05_pit_is_known_loss_of_support_not_unknown(self):
        self.assertEqual(self.scenes["S05"]["identity"]["case"], "pit")
        start = Aabb(7.2, -60, 2.2, 7.8, -58.2, 2.8)
        endpoint = start.moved(0, 0, 1)
        floor = (((7, -61, 2), BlockGeometry.full_cube("minecraft:stone")),)
        world = known_world(start, (0, 0, 1), floor, air=((7, -61, 3),))
        self.assertIs(query_support(start, world).status, QueryStatus.FEASIBLE)
        first = query_support(endpoint, world)
        world_owner = world._owner
        self.assertIsNotNone(world_owner)
        world_owner.confirm_air(
            ObservationStamp(world.session, 2, 2, "test-clock", 2),
            first.missing_cells,
        )
        support = query_support(endpoint, world_owner.view())
        self.assertIs(support.status, QueryStatus.BLOCKED)
        self.assertEqual(support.missing_cells, ())

    def test_ten_thousand_random_full_cube_sweeps_match_brute_reference(self):
        rng = random.Random(20260918)
        for case in range(10_000):
            min_x, min_y, min_z = (rng.uniform(-1.8, 1.8) for _ in range(3))
            body = Aabb(min_x, min_y, min_z,
                        min_x + rng.uniform(.2, .9), min_y + rng.uniform(.5, 1.9),
                        min_z + rng.uniform(.2, .9))
            delta = tuple(rng.uniform(-2, 2) for _ in range(3))
            cells = required_cells_for_sweep(body, delta)
            obstacle_position = rng.choice(cells)
            obstacle = Aabb(*obstacle_position,
                            obstacle_position[0] + 1, obstacle_position[1] + 1,
                            obstacle_position[2] + 1)
            block = (obstacle_position, BlockGeometry.full_cube("minecraft:stone"))
            actual = sweep(body, delta, known_world(body, delta, (block,)))
            # The independent reference samples much more finely than one
            # control tick. Full cubes and the declared body-size range make
            # every real overlap wider than this 0.001 trajectory fraction.
            brute = any(intersects(body.moved(*(axis * step / 1000 for axis in delta)), obstacle)
                        for step in range(1001))
            with self.subTest(case=case):
                self.assertEqual(actual.status is QueryStatus.BLOCKED, brute)


if __name__ == "__main__":
    unittest.main()

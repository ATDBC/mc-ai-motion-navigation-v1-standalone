import unittest
from tests.sim.backend import Scene
from tests.sim.runner import Scenario, run
from tests.sim.continuous_height_matrix import matrix_scenario


class CornerProgressTests(unittest.TestCase):
    def test_rotated_wall_detour_completes_with_equal_entry(self):
        solids = {(x, 63, z): "minecraft:stone" for x in range(-3, 4) for z in range(15)}
        solids.update({(x, y, 5): "minecraft:stone" for x in (-1, 0, 1) for y in (64, 65, 66)})
        for offset in (0., .3):
            base = Scenario("corner-progress", Scene(solids, ((-5, 5), (60, 68), (-2, 16))),
                            (.5 + offset, 64., .5 - offset), (2.5, 64., 10.5))
            for direction in ("south", "east", "north", "west"):
                with self.subTest(offset=offset, direction=direction):
                    result = run(matrix_scenario(base, direction=direction, speed_blocks_per_second=3.,
                                                 seed=35, late_probability=0.))
                    self.assertEqual(result.outcome, "success", result.reason)
                    self.assertFalse(result.violations)

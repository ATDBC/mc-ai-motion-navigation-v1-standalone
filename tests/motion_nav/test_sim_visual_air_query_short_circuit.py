"""Equivalent, bounded witness search in the shared simulation sensor."""
import math
import random
from types import SimpleNamespace
import unittest

from tests.sim.backend import CalculatorBackend, Scene, MAX_DISTANCE


def _old_air_query(backend, cell):
    """Frozen eager reference, before the performance-only fixture repair."""
    if cell in backend.scene.solids:
        return None
    eye = backend._eye()
    nearest = [max(cell[i]-eye[i], eye[i]-cell[i]-1, 0.) for i in range(3)]
    distance = math.sqrt(sum(v*v for v in nearest))
    if distance > MAX_DISTANCE:
        return dict(position=list(cell), status='out_of_range',
                    observer_distance_blocks=None, lower_region_visible=None)
    grid = (.1, .3, .5, .7, .9)
    points = [(cell[0]+a, cell[1]+h, cell[2]+b)
              for a in grid for b in grid for h in (.05, .2, .5, .95)]
    in_view = [p for p in points if backend._in_view(eye, p)]
    if not in_view:
        return dict(position=list(cell), status='outside_view',
                    observer_distance_blocks=None, lower_region_visible=None)
    visible = [p for p in in_view if backend._visible(eye, p, cell)]
    if not visible:
        return dict(position=list(cell), status='occluded',
                    observer_distance_blocks=None, lower_region_visible=None)
    return dict(position=list(cell), status='visible_air',
                observer_distance_blocks=distance,
                lower_region_visible=any(p[1]-cell[1] <= .25 for p in visible))


def _backend(solids=None, position=(.5, 0., .5), yaw=0., pitch=0., pose='standing'):
    # Exercise the real sensor geometry without creating unrelated physics state.
    backend = object.__new__(CalculatorBackend)
    backend.scene = Scene(solids or {}, ((-22, 22), (-3, 6), (-22, 22)))
    backend.state = SimpleNamespace(position=position, yaw_radians=math.radians(yaw),
                                    pitch_radians=math.radians(pitch), pose=pose)
    return backend


class _VisibilityProbe(CalculatorBackend):
    def __init__(self, predicate, in_view=True):
        self.scene = SimpleNamespace(solids={})
        self.predicate = predicate
        self.in_view = in_view
        self.visible_calls = []

    def _eye(self):
        return (.5, 1.62, -.5)

    def _in_view(self, eye, point):
        return self.in_view

    def _visible(self, eye, point, target):
        self.visible_calls.append(point)
        return self.predicate(point)


class SimVisualAirShortCircuitTests(unittest.TestCase):
    def _assert_lower_witness_stops(self, implementation):
        lower = _VisibilityProbe(lambda p: p[1] <= .25)
        self.assertTrue(implementation(lower, (0, 0, 0))['lower_region_visible'])
        self.assertEqual(len(lower.visible_calls), 1)

    def _assert_upper_then_lower_stops(self, implementation):
        upper_then_lower = _VisibilityProbe(
            lambda p: p == (.1, .5, .1) or p == (.1, .05, .3))
        self.assertTrue(implementation(upper_then_lower, (0, 0, 0))['lower_region_visible'])
        self.assertEqual(upper_then_lower.visible_calls,
                         [(.1, .05, .1), (.1, .2, .1), (.1, .5, .1), (.1, .05, .3)])

    def _assert_upper_only_checks_every_lower_candidate(self, implementation):
        upper_only = _VisibilityProbe(lambda p: p[1] > .25)
        self.assertFalse(implementation(upper_only, (0, 0, 0))['lower_region_visible'])
        self.assertEqual(sum(p[1] <= .25 for p in upper_only.visible_calls), 50)
        self.assertEqual(sum(p[1] > .25 for p in upper_only.visible_calls), 1)

    def test_stops_after_first_lower_witness(self):
        self._assert_lower_witness_stops(CalculatorBackend.air_query)

    def test_upper_witness_skips_more_upper_rays_until_later_lower_witness(self):
        self._assert_upper_then_lower_stops(CalculatorBackend.air_query)

    def test_only_upper_visible_still_checks_every_lower_candidate(self):
        self._assert_upper_only_checks_every_lower_candidate(CalculatorBackend.air_query)

    def test_occlusion_requires_all_candidates_and_outside_view_requires_no_rays(self):
        occluded = _VisibilityProbe(lambda p: False)
        self.assertEqual(occluded.air_query((0, 0, 0))['status'], 'occluded')
        self.assertEqual(len(occluded.visible_calls), 100)
        outside = _VisibilityProbe(lambda p: True, in_view=False)
        self.assertEqual(outside.air_query((0, 0, 0))['status'], 'outside_view')
        self.assertEqual(outside.visible_calls, [])

    def _assert_classifications(self, implementation):
        for name, predicate, in_view in (
                ('occluded', lambda p: False, True),
                ('upper_only', lambda p: p[1] > .25, True),
                ('lower_only', lambda p: p[1] <= .25, True),
                ('outside_view', lambda p: True, False)):
            with self.subTest(classification=name):
                probe = _VisibilityProbe(predicate, in_view)
                self.assertEqual(implementation(probe, (0, 0, 0)),
                                 _old_air_query(_VisibilityProbe(predicate, in_view), (0, 0, 0)))

    def test_all_fields_for_invisible_upper_lower_and_occluded_samples(self):
        self._assert_classifications(CalculatorBackend.air_query)

    def test_fixed_random_worlds_match_eager_reference_per_cell(self):
        rng = random.Random('f2r-shared-air-equivalence-v1')
        materials = ('minecraft:stone', 'minecraft:stone_slab', 'minecraft:dirt_path',
                     'minecraft:white_carpet', 'minecraft:snow[layers=4]')
        for scene_id in range(6):
            solids = {(x, y, z): rng.choice(materials)
                      for x in range(-3, 5) for y in range(4) for z in range(1, 9)
                      if rng.random() < .15}
            for view in range(3):
                backend = _backend(solids, (rng.uniform(-1., 1.), 0., rng.uniform(-1., 1.)),
                                   rng.uniform(-180., 180.), rng.uniform(-65., 65.),
                                   'crouching' if view == 1 else 'standing')
                cells = [(rng.randrange(-4, 6), rng.randrange(-1, 5), rng.randrange(0, 10))
                         for _ in range(24)]
                for cell in cells:
                    with self.subTest(scene=scene_id, view=view, cell=cell):
                        self.assertEqual(backend.air_query(cell), _old_air_query(backend, cell))

    def test_geometry_fov_and_distance_boundaries_match_reference(self):
        # A low wall permits the upper half, while hiding every lower witness.
        wall = {(x, 1, 1): 'minecraft:stone_slab' for x in range(-2, 3)}
        upper = _backend(wall)
        self.assertEqual(upper.air_query((0, 1, 3)), _old_air_query(upper, (0, 1, 3)))
        self.assertFalse(upper.air_query((0, 1, 3))['lower_region_visible'])
        full_wall = {(x, y, 1): 'minecraft:stone' for x in range(-2, 3) for y in range(4)}
        occluded = _backend(full_wall)
        self.assertEqual(occluded.air_query((0, 1, 3))['status'], 'occluded')
        self.assertEqual(occluded.air_query((0, 1, 3)), _old_air_query(occluded, (0, 1, 3)))
        for yaw in (-120., -60.-1.e-8, -60., -60.+1.e-8, 0., 60.-1.e-8, 60., 60.+1.e-8, 120.):
            for pitch in (-60.-1.e-8, -60., -60.+1.e-8, 0., 60.-1.e-8, 60., 60.+1.e-8):
                backend = _backend(yaw=yaw, pitch=pitch)
                with self.subTest(yaw=yaw, pitch=pitch):
                    self.assertEqual(backend.air_query((0, 1, 3)), _old_air_query(backend, (0, 1, 3)))
        for offset in (-1.e-8, 0., 1.e-8):
            backend = _backend(position=(offset, 0., .5), yaw=-90.)
            with self.subTest(distance_offset=offset):
                result = backend.air_query((16, 1, 0))
                self.assertEqual(result, _old_air_query(backend, (16, 1, 0)))
                self.assertEqual(result['status'], 'out_of_range' if offset < 0. else 'visible_air')
        solid = _backend({(0, 1, 3): 'minecraft:stone'})
        self.assertIsNone(solid.air_query((0, 1, 3)))

    def test_removed_short_circuit_and_flipped_classification_are_detected(self):
        with self.assertRaises(AssertionError):
            self._assert_lower_witness_stops(_old_air_query)
        with self.assertRaises(AssertionError):
            self._assert_upper_then_lower_stops(_old_air_query)
        with self.assertRaises(AssertionError):
            self._assert_upper_only_checks_every_lower_candidate(_old_air_query)

        def flipped(backend, cell):
            result = CalculatorBackend.air_query(backend, cell)
            if result is not None and result['status'] == 'visible_air':
                result['lower_region_visible'] = not result['lower_region_visible']
            return result

        # Avoid swallowing an assertion inside a subTest context.
        probe = _VisibilityProbe(lambda p: p[1] <= .25)
        with self.assertRaises(AssertionError):
            self.assertEqual(flipped(probe, (0, 0, 0)),
                             _old_air_query(_VisibilityProbe(lambda p: p[1] <= .25), (0, 0, 0)))


if __name__ == '__main__':
    unittest.main()

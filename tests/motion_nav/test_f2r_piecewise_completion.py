"""L-shaped target regression and exact subrectangle safety properties."""
import math
import unittest

from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.support_surfaces import query_support_surfaces, standable_region_in_goal
from mc2p.motion_nav.world_model import Aabb, BlockGeometry, ObservationStamp
from tests.sim.backend import CalculatorBackend
from tests.sim.f2r_cases import TARGETS, DIRECTIONS, layout, goal_for


class PiecewiseCompletionTests(unittest.TestCase):
    def assert_known_safe_region(self, world, selected):
        from mc2p.motion_nav.geometry import unknown_shape_owner_is_fully_covered
        from mc2p.motion_nav.world_model import CellKnowledge
        self.assertIs(selected.status, QueryStatus.FEASIBLE)
        region = selected.completion_region
        bounds = region.bounds
        y = region.support_height
        envelope = Aabb(bounds.min_x-.3, y, bounds.min_z-.3,
                        bounds.max_x+.3, y+1.8, bounds.max_z+.3)
        unknown = tuple(p for p in selected.dependencies if world.cell(p).knowledge is CellKnowledge.UNKNOWN
                        and not unknown_shape_owner_is_fully_covered(world, p))
        self.assertTrue(unknown)
        self.assertTrue(all(p not in region.dependencies for p in unknown))
        for p in unknown:
            worst = Aabb(p[0], p[1], p[2], p[0]+1., p[1]+2., p[2]+1.)
            overlap = all(min(a, b) > max(c, d)+1.e-9 for a, b, c, d in zip(
                envelope.as_tuple()[3:], worst.as_tuple()[3:], envelope.as_tuple()[:3], worst.as_tuple()[:3]))
            self.assertFalse(overlap, p)
        for x in (bounds.min_x, bounds.max_x):
            for z in (bounds.min_z, bounds.max_z):
                body = Aabb(x-.3, y, z-.3, x+.3, y+1.8, z+.3)
                self.assertIs(sweep(body, (0., 0., 0.), world).status, QueryStatus.FEASIBLE)
                support = query_support(body, world)
                self.assertIs(support.status, QueryStatus.FEASIBLE)
                self.assertGreaterEqual(support.support_fraction, .5)

    def test_unknown_corner_clipping_keeps_completely_known_safe_subset(self):
        from tests.sim.continuous_height_matrix import _rotate_cell
        for family in ('outer_corner', 'diagonal_pillar'):
            for direction in DIRECTIONS:
                for target in TARGETS:
                    with self.subTest(family=family, direction=direction, target=target):
                        scene, start, position = layout(family, direction)
                        backend = CalculatorBackend([0], scene, start, 0.)
                        world = backend.truth
                        surface = query_support_surfaces(world.view(), math.floor(position[0]),
                            math.floor(position[2]), 64., 64.).surfaces[0]
                        cells = tuple(_rotate_cell((2, y, 7), direction) for y in (64, 65, 66))
                        world.invalidate(ObservationStamp(backend.state.session, 2, 2, 'f2r-unknown', 100_000_000), cells)
                        result = standable_region_in_goal(world.view(), surface, goal_for(position, target).region,
                                                         connection_from=start)
                        self.assert_known_safe_region(world.view(), result)
                        # Outside selection facts can change without making
                        # the already-proved execution rectangle unsafe.
                        for sequence, blocks in ((3, {p: BlockGeometry.full_cube('minecraft:stone') for p in cells}),
                                                  (4, {p: BlockGeometry.empty('minecraft:air') for p in cells})):
                            world.observe_blocks(ObservationStamp(backend.state.session, sequence, sequence,
                                'f2r-outside-change', sequence*50_000_000), blocks)
                            region = result.completion_region
                            for x in (region.bounds.min_x, region.bounds.max_x):
                                for z in (region.bounds.min_z, region.bounds.max_z):
                                    body = Aabb(x-.3, 64., z-.3, x+.3, 65.8, z+.3)
                                    self.assertIs(sweep(body, (0., 0., 0.), world.view()).status, QueryStatus.FEASIBLE)
                                    self.assertIs(query_support(body, world.view()).status, QueryStatus.FEASIBLE)

    def test_old_single_column_strip_keeps_safe_known_subset(self):
        from tests.motion_nav.test_d060_terminal_node_exact_proof import _world
        from tests.motion_nav.test_d062_direct_walk_validation import _goal
        world = _world(62, 16)
        surface = query_support_surfaces(world.view(), 0, 9, -60., -60.).surfaces[0]
        result = standable_region_in_goal(world.view(), surface, _goal(9.8).region, connection_from=(.5, -60., 9.1))
        self.assert_known_safe_region(world.view(), result)

    def test_unknown_inside_execution_area_still_requires_information(self):
        scene, start, position = layout('diagonal_pillar')
        backend = CalculatorBackend([0], scene, start, 0.)
        world = backend.truth
        surface = query_support_surfaces(world.view(), 1, 6, 64., 64.).surfaces[0]
        cell = (1, 64, 6)
        world.invalidate(ObservationStamp(backend.state.session, 2, 2, 'f2r-execution-unknown', 100_000_000), (cell,))
        result = standable_region_in_goal(world.view(), surface, goal_for(position, 'product').region)
        self.assertIs(result.status, QueryStatus.NEEDS_INFORMATION)
        self.assertIn(cell, result.missing_cells)
        self.assertIsNone(result.completion_region)

    def test_unknown_owner_covered_by_known_full_cube_keeps_existing_exception(self):
        scene, start, position = layout('diagonal_pillar')
        backend = CalculatorBackend([0], scene, start, 0.)
        world = backend.truth
        surface = query_support_surfaces(world.view(), 1, 6, 64., 64.).surfaces[0]
        world.invalidate(ObservationStamp(backend.state.session, 2, 2, 'f2r-covered', 100_000_000), ((2, 64, 7),))
        selected = standable_region_in_goal(world.view(), surface, goal_for(position, 'product').region)
        self.assertIs(selected.status, QueryStatus.FEASIBLE)

    def test_v8_preserves_v7_inputs_and_fabric_matrix_is_frozen(self):
        import json
        from pathlib import Path
        from scripts.f2_ground_route_runtime import frozen_plan, fixture
        from scripts.f2_ground_route_evidence import digest
        from tests.sim.f2r_cases import materialized_manifest
        manifest_root = Path(__file__).resolve().parents[1]/'sim/manifests'
        v7 = json.loads((manifest_root/'navigation-product-r28-v7.json').read_text('utf-8'))
        v8 = materialized_manifest()
        self.assertEqual(v8['product_reference'], v7)
        self.assertEqual(len(v8['tasks']), 104)
        self.assertEqual(len(v8['clutter_scan']), 1800)
        index_path = manifest_root.parents[2]/'evidence/motion_navigation/F2R-piecewise-completion-v1/frozen-input-index.jsonl'
        old_inputs = {r['id']: r['input_sha256'] for r in map(json.loads, index_path.read_text('utf-8').splitlines())}
        generated = {c['id']: digest(c) for c in v8['tasks']+v8['clutter_scan']}
        self.assertEqual(old_inputs, generated)
        for layer, expected in v8['expected_inputs']['by_layer'].items():
            selected = [c for c in v8['tasks']+v8['clutter_scan'] if c['family']+'/'+c['target'] == layer]
            self.assertEqual(len(selected), expected['cases'])
            self.assertEqual(digest(selected), expected['input_sha256'])
        self.assertEqual(len(frozen_plan()), 93)
        matrix = frozen_plan(f2r=True)
        self.assertEqual(len(matrix), 16)
        self.assertEqual(len({r['id'] for r in matrix}), 16)
        self.assertEqual(sum(r['condition'] == 'late_first' for r in matrix), 8)
        for row in matrix:
            solids, start, target = fixture(row)
            self.assertEqual(row['scene_sha256'], digest(sorted((list(p), m) for p, m in solids.items())))
            self.assertEqual(row['start_position'], list(start))
            self.assertEqual(row['goal_bounds'], list(goal_for(target, 'product').region.as_tuple()))

    def test_equal_area_selects_fixed_coordinate_order(self):
        from tests.sim.backend import Scene
        scene = Scene({(x, 63, z): 'minecraft:stone' for x in range(-2, 4) for z in range(-2, 4)},
                      ((-3, 5), (60, 68), (-3, 5)))
        backend = CalculatorBackend([0], scene, (.5, 64., .5), 0.)
        world = backend.truth
        surface = query_support_surfaces(world.view(), 0, 0, 64., 64.).surfaces[0]
        # Binary-exact coordinates make the two largest pieces exactly equal.
        stamp = ObservationStamp(backend.state.session, 2, 2, 'f2r-tie', 100_000_000)
        world.observe_blocks(stamp, {(0, 64, 0): BlockGeometry('minecraft:stone', 'boxes',
            (Aabb(.425, 0., .425, .575, 1., .575),))})
        goal = Aabb(0., 63.99, 0., 1., 64.01, 1.)
        selected = standable_region_in_goal(world.view(), surface, goal, body_width=.6)
        self.assertIs(selected.status, QueryStatus.FEASIBLE)
        self.assertEqual(selected.completion_region.bounds.as_tuple(), (0., 63.99, 0., .125, 64.01, 1.))

    def test_changed_piece_invalidates_typed_region_replay(self):
        from mc2p.motion_nav.route_validation import (WalkValidationRecipe, WalkValidationQueryKind,
            StandableRegionQueryArgs, GroundCapabilityIdentity, replay_walk_validation_recipe)
        from tests.motion_nav.test_f2_ground_completion_region import PROFILES
        scene, start, position = layout('diagonal_pillar')
        backend = CalculatorBackend([0], scene, start, 0.)
        world = backend.truth
        surface = query_support_surfaces(world.view(), 1, 6, 64., 64.).surfaces[0]
        goal = goal_for(position, 'product')
        selected = standable_region_in_goal(world.view(), surface, goal.region, connection_from=start)
        args = StandableRegionQueryArgs(surface, goal.region, start, selected.completion_region)
        recipe = WalkValidationRecipe('f2r-region', WalkValidationQueryKind.STANDABLE_REGION,
            None, None, PROFILES.ground, GroundCapabilityIdentity.from_profile(PROFILES.ground),
            selected.completion_region.dependencies, args)
        self.assertIs(replay_walk_validation_recipe(recipe, world.view())[0], QueryStatus.FEASIBLE)
        # Removing the boundary owner expands the exact piece; old proof fails.
        world.confirm_air(ObservationStamp(backend.state.session, 2, 2, 'f2r-change', 100_000_000),
                          ((2, 64, 7), (2, 65, 7), (2, 66, 7)))
        self.assertIs(replay_walk_validation_recipe(recipe, world.view())[0], QueryStatus.BLOCKED)

    def test_outer_corner_pillar_and_two_pillars_keep_exact_standable_rectangle(self):
        for family in ('outer_corner', 'diagonal_pillar', 'two_pillars'):
            for target in TARGETS:
                for direction in DIRECTIONS:
                    with self.subTest(family=family, target=target, direction=direction):
                        scene, start, position = layout(family, direction)
                        world = CalculatorBackend([0], scene, start, 0.).world._world
                        surface = query_support_surfaces(world, math.floor(position[0]), math.floor(position[2]), 64., 64.).surfaces[0]
                        goal = goal_for(position, target)
                        selected = standable_region_in_goal(world, surface, goal.region, connection_from=start)
                        self.assertIs(selected.status, QueryStatus.FEASIBLE)
                        region = selected.completion_region
                        bounds = region.bounds
                        self.assertTrue(all(a <= b for a, b in zip(goal.region.as_tuple()[:3], bounds.as_tuple()[:3])))
                        self.assertTrue(all(a <= b for a, b in zip(bounds.as_tuple()[3:], goal.region.as_tuple()[3:])))
                        self.assertEqual(selected, standable_region_in_goal(world, surface, goal.region, connection_from=start))
                        for x, z in [((bounds.min_x+bounds.max_x)/2, (bounds.min_z+bounds.max_z)/2),
                                     *((x, z) for x in (bounds.min_x, bounds.max_x) for z in (bounds.min_z, bounds.max_z))]:
                            body = Aabb(x-.3, 64., z-.3, x+.3, 65.8, z+.3)
                            self.assertIs(sweep(body, (0., 0., 0.), world).status, QueryStatus.FEASIBLE)
                            support = query_support(body, world)
                            self.assertIs(support.status, QueryStatus.FEASIBLE)
                            self.assertGreaterEqual(support.support_fraction, .5)


if __name__ == '__main__':
    unittest.main()

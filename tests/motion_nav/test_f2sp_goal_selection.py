"""F2-SP complete-scan oracle and selection-equivalence gates."""
from __future__ import annotations

import math
import random
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.navigation_session import NavigationSession
import mc2p.motion_nav.navigation_session as navigation_session_module
from mc2p.motion_nav.support_surfaces import (
    SurfaceNodeId,
    query_support_surfaces,
    standable_region_in_goal,
)
from mc2p.motion_nav.world_model import (
    Aabb,
    BlockGeometry,
    ObservationStamp,
    WorldKnowledge,
    WorldQueryCache,
    WorldSessionId,
)


def _known_world(*, name: str = "f2sp-known", unknown: frozenset[tuple[int, int, int]] = frozenset()):
    session = WorldSessionId(name)
    knowledge = WorldKnowledge(session)
    stamp = ObservationStamp(session, 1, 1, "f2sp-test", 50_000_000)
    positions = tuple(
        (x, y, z)
        for x in range(-6, 7)
        for y in range(58, 69)
        for z in range(-6, 7)
    )
    knowledge.confirm_air(stamp, tuple(position for position in positions if position not in unknown))
    floor = {
        (x, 63, z): BlockGeometry.full_cube("minecraft:stone")
        for x in range(-5, 6)
        for z in range(-5, 6)
    }
    floor[(2, 63, 0)] = BlockGeometry(
        "minecraft:smooth_stone_slab", "boxes",
        (Aabb(0., 0., 0., 1., .5, 1.),),
    )
    floor[(-2, 63, 0)] = BlockGeometry(
        "minecraft:dirt_path", "boxes",
        (Aabb(0., 0., 0., 1., 15 / 16, 1.),),
    )
    floor[(0, 64, 2)] = BlockGeometry.full_cube("minecraft:stone")
    knowledge.observe_blocks(stamp, {position: geometry for position, geometry in floor.items() if position not in unknown})
    return knowledge


def _goal(bounds: Aabb) -> GoalState:
    return GoalState(
        bounds,
        GoalSupport.SOLID,
        frozenset({MovementMode.WALK}),
        frozenset({"standing"}),
        .6,
    )


def _eager_goal_surface(world, goal: GoalState):
    """Independent complete scan; it never uses the production early stop."""
    candidates = []
    discovery_missing = set()
    max_x = math.floor(math.nextafter(goal.region.max_x, -math.inf))
    max_z = math.floor(math.nextafter(goal.region.max_z, -math.inf))
    for x in range(math.floor(goal.region.min_x), max_x + 1):
        for z in range(math.floor(goal.region.min_z), max_z + 1):
            result = query_support_surfaces(
                world, x, z, goal.region.min_y, goal.region.max_y,
                collect_complete_missing=True,
            )
            discovery_missing.update(result.missing_cells)
            candidates.extend(result.surfaces)
    if discovery_missing:
        return None, tuple(sorted(discovery_missing))
    center = (
        (goal.region.min_x + goal.region.max_x) / 2,
        (goal.region.min_y + goal.region.max_y) / 2,
        (goal.region.min_z + goal.region.max_z) / 2,
    )
    cache = WorldQueryCache(world)
    selection_missing = set()
    for surface in sorted(
        candidates,
        key=lambda item: (math.dist(item.position, center), item.node_id),
    ):
        result = standable_region_in_goal(
            world, surface, goal.region, query_cache=cache,
        )
        if result.status is QueryStatus.FEASIBLE:
            return surface.node_id, ()
        selection_missing.update(result.missing_cells)
    return None, tuple(sorted(selection_missing))


class F2SPCompleteScanOracleTests(unittest.TestCase):
    def test_known_world_random_goals_match_independent_eager_oracle(self):
        world = _known_world().view()
        rng = random.Random(20261008)
        cases = [
            Aabb(1.7, 63.4, -.3, 2.8, 64.1, .8),       # lower slab
            Aabb(-2.3, 63.7, -.4, -1.2, 64.1, .7),     # dirt path
            Aabb(-.4, 64.7, 1.7, .8, 65.1, 2.8),       # raised surface
        ]
        for _ in range(96):
            cx = rng.uniform(-3.8, 3.8)
            cz = rng.uniform(-3.8, 3.8)
            rx = rng.uniform(.35, 1.35)
            rz = rng.uniform(.35, 1.35)
            cases.append(Aabb(cx-rx, 63.85, cz-rz, cx+rx, 64.15, cz+rz))
        for index, bounds in enumerate(cases):
            with self.subTest(index=index, bounds=bounds):
                goal = _goal(bounds)
                expected = _eager_goal_surface(world, goal)
                actual = NavigationSession._surface_for_goal(
                    SimpleNamespace(world=world), goal,
                )
                self.assertEqual(actual, expected)

    def test_four_direction_translation_keeps_relative_selected_node(self):
        for dx, dz in ((3, 0), (-3, 0), (0, 3), (0, -3)):
            knowledge = _known_world(name=f"f2sp-dir-{dx}-{dz}")
            world = knowledge.view()
            goal = _goal(Aabb(dx-.8, 63.85, dz-.8, dx+.8, 64.15, dz+.8))
            actual = NavigationSession._surface_for_goal(SimpleNamespace(world=world), goal)
            expected = _eager_goal_surface(world, goal)
            self.assertEqual(actual, expected)
            self.assertIsNotNone(actual[0])

    def test_eager_oracle_exposes_surface_node_id_tie_mutation(self):
        world = _known_world(name="f2sp-tie").view()
        goal = _goal(Aabb(-1., 63.85, .1, 1., 64.15, .9))
        expected, missing = _eager_goal_surface(world, goal)
        self.assertEqual(missing, ())
        self.assertEqual(expected, SurfaceNodeId(-1, 0, 64, 0))
        # A distance-only implementation may keep the later equal-distance node.
        mutant = SurfaceNodeId(0, 0, 64, 0)
        self.assertNotEqual(mutant, expected)

    def test_relevant_unknown_is_not_silently_granted(self):
        # Leave the support owner closest to the target centre unknown while a
        # farther complete column remains. The baseline must wait for that fact.
        knowledge = _known_world(
            name="f2sp-relevant-unknown",
            unknown=frozenset({(0, 63, 0)}),
        )
        world = knowledge.view()
        goal = _goal(Aabb(-.2, 63.85, -.2, 1.8, 64.15, 1.2))
        node, missing = NavigationSession._surface_for_goal(SimpleNamespace(world=world), goal)
        self.assertIsNone(node)
        self.assertTrue(missing)
        self.assertIn((0, 63, 0), missing)

    def test_irrelevant_far_unknown_does_not_block_a_proved_nearer_surface(self):
        knowledge = _known_world(
            name="f2sp-irrelevant-unknown",
            unknown=frozenset({(3, 63, 0)}),
        )
        world = knowledge.view()
        goal = _goal(Aabb(.1, 63.85, .2, 4., 64.15, .8))
        node, missing = NavigationSession._surface_for_goal(
            SimpleNamespace(world=world), goal,
        )
        self.assertEqual(node, SurfaceNodeId(1, 0, 64, 0))
        self.assertEqual(missing, ())

    def test_no_feasible_surface_scans_every_goal_column(self):
        world = _known_world(name="f2sp-all-blocked").view()
        goal = _goal(Aabb(0., 63.85, .1, 3., 64.15, .9))
        queried = []

        def blocked(_world, x, z, *_args, **_kwargs):
            queried.append((x, z))
            from mc2p.motion_nav.support_surfaces import SupportSurfaceResult
            return SupportSurfaceResult(QueryStatus.BLOCKED, (), ())

        with patch.object(
            navigation_session_module, "query_support_surfaces",
            side_effect=blocked,
        ):
            result = NavigationSession._surface_for_goal(
                SimpleNamespace(world=world), goal,
            )
        self.assertEqual(result, (None, ()))
        self.assertEqual(queried, [(1, 0), (0, 0), (2, 0)])

    def test_equal_lower_bound_column_is_not_skipped(self):
        world = _known_world(name="f2sp-equal-bound").view()
        goal = _goal(Aabb(-1., 63.85, .1, 1., 64.15, .9))
        calls = []
        original = navigation_session_module.query_support_surfaces

        def observed(query_world, x, z, *args, **kwargs):
            calls.append((x, z))
            return original(query_world, x, z, *args, **kwargs)

        with patch.object(
            navigation_session_module, "query_support_surfaces",
            side_effect=observed,
        ):
            node, missing = NavigationSession._surface_for_goal(
                SimpleNamespace(world=world), goal,
            )
        self.assertEqual(node, SurfaceNodeId(-1, 0, 64, 0))
        self.assertEqual(missing, ())
        self.assertEqual(calls, [(-1, 0), (0, 0)])

    def test_proved_far_columns_are_not_queried_in_known_world(self):
        world = _known_world(name="f2sp-known-pruning").view()
        goal = _goal(Aabb(.1, 63.85, .1, 5.9, 64.15, .9))
        calls = []
        original = navigation_session_module.query_support_surfaces

        def observed(query_world, x, z, *args, **kwargs):
            calls.append((x, z))
            return original(query_world, x, z, *args, **kwargs)

        with patch.object(
            navigation_session_module, "query_support_surfaces",
            side_effect=observed,
        ):
            node, missing = NavigationSession._surface_for_goal(
                SimpleNamespace(world=world), goal,
            )
        self.assertEqual(node, SurfaceNodeId(3, 0, 64, 0))
        self.assertEqual(missing, ())
        self.assertEqual(calls, [(2, 0), (3, 0)])

    def test_selection_uses_one_cache_for_discovery_and_exact_query(self):
        world = _known_world(name="f2sp-shared-selection-cache").view()
        goal = _goal(Aabb(-1.2, 63.85, -.8, 1.8, 64.15, 1.2))
        caches = []
        original_discovery = navigation_session_module.query_support_surfaces
        original_exact = navigation_session_module.standable_region_in_goal

        def discovery(*args, **kwargs):
            caches.append(kwargs.get("query_cache"))
            return original_discovery(*args, **kwargs)

        def exact(*args, **kwargs):
            caches.append(kwargs.get("query_cache"))
            return original_exact(*args, **kwargs)

        with (
            patch.object(navigation_session_module, "query_support_surfaces",
                         side_effect=discovery),
            patch.object(navigation_session_module, "standable_region_in_goal",
                         side_effect=exact),
        ):
            NavigationSession._surface_for_goal(SimpleNamespace(world=world), goal)
        self.assertTrue(caches)
        self.assertIsInstance(caches[0], WorldQueryCache)
        self.assertEqual({id(cache) for cache in caches}, {id(caches[0])})

    def test_selection_has_no_wall_clock_or_candidate_budget_semantics(self):
        source = inspect.getsource(NavigationSession._surface_for_goal)
        self.assertNotIn("perf_counter", source)
        self.assertNotIn("clock_ns", source)
        self.assertNotIn("candidate_limit", source)
        self.assertNotIn("time_budget", source)


class F2SPWorldQueryCacheTests(unittest.TestCase):
    def test_support_surface_cache_keeps_result_and_reuses_all_fact_reads(self):
        world = _known_world(name="f2sp-cache").view()
        uncached = query_support_surfaces(world, 0, 0, 64., 64.)
        cache = WorldQueryCache(world)
        original = type(world)._cell_at_valid_position
        calls = 0

        def counted(view, position):
            nonlocal calls
            calls += 1
            return original(view, position)

        with patch.object(type(world), "_cell_at_valid_position", counted):
            first = query_support_surfaces(
                world, 0, 0, 64., 64., query_cache=cache,
            )
            first_call_count = calls
            second = query_support_surfaces(
                world, 0, 0, 64., 64., query_cache=cache,
            )

        self.assertEqual(first, uncached)
        self.assertEqual(second, uncached)
        self.assertGreater(first_call_count, 0)
        self.assertEqual(calls, first_call_count)
        self.assertTrue(cache.touched_cells)

    def test_support_surface_cache_rejects_another_world_view(self):
        first = _known_world(name="f2sp-cache-first").view()
        second = _known_world(name="f2sp-cache-second").view()
        cache = WorldQueryCache(first)
        with self.assertRaises(ContractViolation):
            query_support_surfaces(
                second, 0, 0, 64., 64., query_cache=cache,
            )

    def test_support_surface_cache_rejects_an_untyped_cache(self):
        world = _known_world(name="f2sp-cache-untyped").view()
        with self.assertRaises(ContractViolation):
            query_support_surfaces(
                world, 0, 0, 64., 64., query_cache=object(),
            )


if __name__ == "__main__":
    unittest.main()

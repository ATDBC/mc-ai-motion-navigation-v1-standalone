"""F2-REC R1 freezes exact completion geometry before production changes."""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import unittest

from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.route_admission import RouteAdmitter
from mc2p.motion_nav.route_validation import (
    GroundCapabilityIdentity,
    StandableRegionQueryArgs,
    WalkValidationQueryKind,
    WalkValidationRecipe,
    replay_walk_validation_recipe,
)
import mc2p.motion_nav.support_surfaces as support_surfaces
from mc2p.motion_nav.support_surfaces import (
    query_support_surfaces,
    standable_point_in_region,
    standable_region_in_goal,
)
from mc2p.motion_nav.world_model import Aabb, ObservationStamp, WorldQueryCache
from tests.motion_nav.test_f2_ground_completion_region import PROFILES
from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.f2r_cases import goal_for, materialized_manifest as v8_manifest, scenario_for
from tests.sim.f2s_cases import materialized_manifest, scene_for


ROOT = Path(__file__).resolve().parents[2]
STONE = "minecraft:stone"


def _case(family: str, target: str = "melee") -> dict:
    return next(
        row for row in materialized_manifest()["support_region_cases"]
        if row["family"] == family
        and row["target"] == target
        and row["direction"] == "south"
        and row["condition"] == "normal"
    )


def _surface_and_goal(family: str):
    case = _case(family)
    backend = CalculatorBackend(
        [0], scene_for(case), tuple(case["start"]), case["yaw_degrees"],
    )
    goal = goal_for(tuple(case["goal"]), case["target"])
    surface = next(
        surface
        for x in range(int(goal.region.min_x) - 1, int(goal.region.max_x) + 2)
        for z in range(int(goal.region.min_z) - 1, int(goal.region.max_z) + 2)
        for surface in query_support_surfaces(
            backend.world._world, x, z,
            goal.region.min_y - 1.0, goal.region.max_y + 1.0,
        ).surfaces
        if standable_point_in_region(
            backend.world._world, surface, goal.region,
        ).status is QueryStatus.FEASIBLE
    )
    return backend, surface, goal


class F2RecoveryR1RegionGeometryTests(unittest.TestCase):
    def test_allowed_change_ids_are_frozen_before_r1(self):
        evidence = json.loads((
            ROOT / "evidence/motion_navigation/F2REC-recovery-v1/r1/"
            "allowed-changes.json"
        ).read_text("utf-8"))
        self.assertEqual(evidence["counts"], {
            "initial_f2s_c01_red": 64,
            "allowed_red_to_success": 152,
            "frozen_old_success": 64,
        })
        self.assertTrue(all(
            row.startswith((
                "f2s/platform_outer_corner/",
                "f2s/bridge_head/",
                "f2s/column_top/",
            ))
            for row in evidence["initial_f2s_c01_ids"]
        ))

    def test_platform_bridge_and_column_keep_positive_safe_regions(self):
        for family in ("platform_outer_corner", "bridge_head", "column_top"):
            with self.subTest(family=family):
                backend, surface, goal = _surface_and_goal(family)
                selected = standable_region_in_goal(
                    backend.world._world,
                    surface,
                    goal.region,
                    connection_from=backend.state.position,
                )
                self.assertIs(selected.status, QueryStatus.FEASIBLE)
                completion = selected.completion_region
                self.assertIsNotNone(completion)
                bounds = completion.bounds
                self.assertGreater(bounds.max_x - bounds.min_x, 0.0)
                self.assertGreater(bounds.max_z - bounds.min_z, 0.0)
                goal_values, bound_values = goal.region.as_tuple(), bounds.as_tuple()
                self.assertTrue(all(
                    goal_values[index] <= bound_values[index] + 1.0e-9
                    for index in range(3)
                ))
                self.assertTrue(all(
                    bound_values[index] <= goal_values[index] + 1.0e-9
                    for index in range(3, 6)
                ))
                for x in (bounds.min_x, bounds.max_x):
                    for z in (bounds.min_z, bounds.max_z):
                        body = Aabb(x - .3, completion.support_height, z - .3,
                                    x + .3, completion.support_height + 1.8, z + .3)
                        self.assertIs(
                            sweep(body, (0.0, 0.0, 0.0), backend.world._world).status,
                            QueryStatus.FEASIBLE,
                        )
                        self.assertGreaterEqual(
                            query_support(body, backend.world._world).support_fraction
                            + 1.0e-9,
                            .5,
                        )

    def test_grid_selection_is_independent_of_old_piece_cut_order(self):
        case = _case("cross_piece")
        backend = CalculatorBackend(
            [0], scene_for(case), tuple(case["start"]), case["yaw_degrees"],
        )
        surface = query_support_surfaces(
            backend.world._world, 0, 0, 64.0, 64.0,
        ).surfaces[0]
        selected = standable_region_in_goal(
            backend.world._world,
            surface,
            Aabb(.6, 63.92, .2, 1.0, 64.08, 1.0),
        )
        self.assertIs(selected.status, QueryStatus.FEASIBLE)
        bounds = selected.completion_region.bounds
        self.assertEqual(
            (bounds.min_x, bounds.min_z, bounds.max_x, bounds.max_z),
            (.6, .2, 1.0, .7),
        )

    def test_single_grid_threshold_cut_remains_typed_unsupported(self):
        backend = CalculatorBackend(
            [0], Scene({(0, 63, 0): STONE}, ((-2, 3), (60, 68), (-2, 3))),
            (.5, 64.0, .5), 0.0,
        )
        surface = query_support_surfaces(
            backend.world._world, 0, 0, 64.0, 64.0,
        ).surfaces[0]
        selected = standable_region_in_goal(
            backend.world._world,
            surface,
            Aabb(.8, 63.99, .8, 1.0, 64.01, 1.0),
        )
        self.assertIs(selected.status, QueryStatus.UNSUPPORTED)

    def test_replay_validates_bound_region_without_reselecting_outside_area(self):
        self.assertTrue(hasattr(support_surfaces, "validate_standable_region"))
        from tests.sim.f2r_cases import layout

        scene, start, position = layout("diagonal_pillar")
        backend = CalculatorBackend([0], scene, start, 0.0)
        surface = query_support_surfaces(
            backend.truth.view(), 1, 6, 64.0, 64.0,
        ).surfaces[0]
        goal = goal_for(position, "product")
        selected = standable_region_in_goal(
            backend.truth.view(), surface, goal.region, connection_from=start,
        )
        args = StandableRegionQueryArgs(
            surface, goal.region, start, selected.completion_region,
        )
        recipe = WalkValidationRecipe(
            "f2rec-r1-bound-region",
            WalkValidationQueryKind.STANDABLE_REGION,
            None,
            None,
            PROFILES.ground,
            GroundCapabilityIdentity.from_profile(PROFILES.ground),
            selected.completion_region.dependencies,
            args,
        )
        self.assertIs(
            replay_walk_validation_recipe(recipe, backend.truth.view())[0],
            QueryStatus.FEASIBLE,
        )
        backend.truth.confirm_air(
            ObservationStamp(
                backend.state.session, 2, 2, "f2rec-r1-outside-improved",
                100_000_000,
            ),
            ((2, 64, 7), (2, 65, 7), (2, 66, 7)),
        )
        self.assertIs(
            replay_walk_validation_recipe(recipe, backend.truth.view())[0],
            QueryStatus.FEASIBLE,
        )

    def test_cache_is_optional_and_does_not_change_region_semantics(self):
        backend, surface, goal = _surface_and_goal("platform_outer_corner")
        uncached = standable_region_in_goal(
            backend.world._world, surface, goal.region,
            connection_from=backend.state.position,
        )
        cached = standable_region_in_goal(
            backend.world._world, surface, goal.region,
            connection_from=backend.state.position,
            query_cache=WorldQueryCache(backend.world._world),
        )
        self.assertEqual(cached, uncached)

    def test_terminal_region_keeps_safe_planner_surface_endpoint(self):
        case = next(
            row for row in v8_manifest()["clutter_scan"]
            if row["id"] == "f2r/clutter/0.1/0/21/melee"
        )
        scenario = scenario_for(case)
        backend = CalculatorBackend(
            [0], scenario.scene, scenario.start, scenario.yaw_degrees,
        )
        goal = replace(
            goal_for(tuple(case["goal"]), case["target"]),
            region=Aabb(*case["goal_box"]),
        )
        surface = query_support_surfaces(
            backend.world._world, 3, 4, 63.9, 64.1,
        ).surfaces[0]
        selected = RouteAdmitter._goal_completion(
            backend.world._world,
            surface,
            goal,
            PROFILES.ground,
            (2.5, 64.0, 4.5),
        )
        self.assertIs(selected.status, QueryStatus.FEASIBLE)
        self.assertTrue(selected.completion_region.contains(surface.position))
        self.assertEqual(selected.position, surface.position)


if __name__ == "__main__":
    unittest.main()

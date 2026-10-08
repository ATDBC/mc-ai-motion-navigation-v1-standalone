"""F2-S Task 0 freezes RED behavior before production changes."""
from __future__ import annotations

import inspect
import io
import json
import random
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from mc2p.motion_nav.geometry import QueryStatus, query_support, sweep
import mc2p.motion_nav.navigation_session as navigation_session_module
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.route_validation import (
    GroundCapabilityIdentity,
    StandableRegionQueryArgs,
    WalkValidationQueryKind,
    WalkValidationRecipe,
    replay_walk_validation_recipe,
)
from mc2p.motion_nav.route_admission import RouteAdmitter
from mc2p.motion_nav.support_surfaces import (
    HorizontalRegion,
    StandableRegionDiagnostics,
    StandableRegionResult,
    SupportSurface,
    SupportSurfaceResult,
    SurfaceNodeId,
    _largest_valid_subrectangle,
    query_support_surfaces,
    query_standable_connection,
    standable_point_in_region,
    standable_region_in_goal,
)
from mc2p.motion_nav.ground_route_execution import GroundCompletionRegion
from mc2p.motion_nav.world_model import Aabb, ObservationStamp, WorldQueryCache
from scripts.f2s_support_region_evidence import (
    benchmark,
    classify,
    independent_reference_label,
    main as evidence_main,
    run_v9_case,
    summarize_v9_rows,
    v9_new_gate,
)
from tests.motion_nav.test_f2_ground_completion_region import PROFILES
from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.f2r_cases import materialized_manifest as v8_manifest
from tests.sim.f2s_cases import (
    FAMILIES,
    MANIFEST,
    input_digest,
    materialized_manifest,
    validate_taxonomy,
)


STONE = "minecraft:stone"


def _platform_world():
    solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
    scene = Scene(solids, ((-6, 7), (60, 68), (-3, 13)))
    backend = CalculatorBackend([0], scene, (.5, 64., .5), 0.)
    return scene, backend.world._world


def _cross_piece_world():
    solids = {(x, 63, z): STONE for x in range(-2, 3) for z in range(-2, 3)}
    solids.update({(1, y, 1): STONE for y in (64, 65, 66)})
    scene = Scene(solids, ((-4, 5), (60, 68), (-4, 5)))
    backend = CalculatorBackend([0], scene, (.5, 64., .5), 0.)
    return backend, backend.world._world


class F2SFreezeTests(unittest.TestCase):
    def test_benchmark_cli_returns_failure_when_a_frozen_timing_gate_fails(self):
        with redirect_stdout(io.StringIO()):
            with patch(
                "scripts.f2s_support_region_evidence.benchmark",
                return_value={"passed": False},
            ):
                self.assertEqual(evidence_main(["benchmark"]), 1)
            with patch(
                "scripts.f2s_support_region_evidence.classify",
                return_value={"schema_version": "classification"},
            ):
                self.assertEqual(evidence_main(["classify"]), 0)

    def test_green_region_benchmark_uses_real_diagnostics_and_frozen_gates(self):
        report = benchmark()
        self.assertTrue(report["passed"])
        self.assertEqual(report["hash_basis"], "windows_worktree_bytes")
        self.assertEqual(set(report["cases"]), {
            "first_feasible", "last_feasible", "all_unavailable",
        })
        expected_queries = {
            "first_feasible": 1,
            "last_feasible": 12,
            "all_unavailable": 11,
        }
        for name, case in report["cases"].items():
            self.assertEqual(case["exact_query_count"], {
                "minimum": expected_queries[name],
                "maximum": expected_queries[name],
            })
            self.assertTrue(case["observed_unified_grids"])
            self.assertTrue(all(case["gates"].values()))

    def test_v9_summary_counts_standable_point_product_gaps_per_layer(self):
        report = summarize_v9_rows((
            {"id": "a", "layer": "support_edge", "outcome": "success",
             "reason": "completed", "standable_point_exists": True},
            {"id": "b", "layer": "support_edge", "outcome": "failed",
             "reason": "fixed_route_stalled", "standable_point_exists": True},
            {"id": "c", "layer": "height_edge", "outcome": "failed",
             "reason": "planning_no_known_route",
             "standable_point_exists": False},
        ))
        self.assertEqual(
            report["layers"]["support_edge"]
            ["standable_point_exists_but_task_failed"],
            1,
        )
        self.assertEqual(report["layers"]["support_edge"]["completed"], 1)
        self.assertEqual(report["layers"]["height_edge"]["completed"], 0)

    def test_v9_formal_runner_records_original_goal_and_safety_fields(self):
        case = next(
            row for row in materialized_manifest()["support_region_cases"]
            if row["id"] == "f2s/platform_outer_corner/product/south/normal"
        )
        row = run_v9_case(case)
        self.assertEqual(row["id"], case["id"])
        self.assertEqual(row["layer"], "support_edge")
        self.assertEqual(row["outcome"], "success")
        self.assertTrue(row["standable_point_exists"])
        self.assertFalse(row["violations"])
        self.assertTrue(row["source_released"])
        self.assertEqual(row["damage"], 0.0)

    def test_v9_completion_gap_is_reported_but_only_safety_fails_gate(self):
        bounded_gap = {
            "outcome": "failed",
            "violations": [],
            "source_released": True,
            "damage": 0.0,
            "terminal_sneaking": False,
        }
        self.assertTrue(v9_new_gate((bounded_gap,)))
        self.assertFalse(v9_new_gate(({**bounded_gap, "damage": 1.0},)))

    def test_terminal_region_reuses_safe_planner_surface_endpoint(self):
        from dataclasses import replace
        from tests.sim.f2r_cases import goal_for, scenario_for

        case = next(
            row for row in v8_manifest()["clutter_scan"]
            if row["id"] == "f2r/clutter/0.1/0/21/melee"
        )
        scenario = scenario_for(case)
        backend = CalculatorBackend(
            [0], scenario.scene, scenario.start, scenario.yaw_degrees
        )
        world = backend.world._world
        goal = replace(
            goal_for(tuple(case["goal"]), case["target"]),
            region=Aabb(*case["goal_box"]),
        )
        surface = query_support_surfaces(world, 3, 4, 63.9, 64.1).surfaces[0]
        incoming = (2.5, 64.0, 4.5)
        selected = RouteAdmitter._goal_completion(
            world, surface, goal, PROFILES.ground, incoming
        )
        self.assertIs(selected.status, QueryStatus.FEASIBLE)
        self.assertTrue(selected.completion_region.contains(surface.position))
        self.assertEqual(selected.position, surface.position)
        self.assertIs(
            query_standable_connection(
                world, surface, selected.position, incoming
            ).status,
            QueryStatus.FEASIBLE,
        )

    def test_terminal_tail_keeps_the_planner_surface_before_small_goal(self):
        from dataclasses import replace
        import tests.sim.runner as runner
        from tests.sim.f2r_cases import goal_for, scenario_for

        case = next(
            row for row in v8_manifest()["clutter_scan"]
            if row["id"] == "f2r/clutter/0.1/0/21/product"
        )
        scenario = scenario_for(case)
        goal = replace(
            goal_for(tuple(case["goal"]), case["target"]),
            region=Aabb(*case["goal_box"]),
        )
        with patch.object(runner, "_goal", lambda *_args, **_kwargs: goal):
            result = runner.run(scenario)
        self.assertFalse(result.violations)
        self.assertEqual(result.outcome, "success", result.reason)

    def test_largest_valid_subrectangle_uses_weighted_area_and_stable_tie_break(self):
        xs = (0., .2, 1.)
        zs = (0., .5, 1.)
        self.assertEqual(
            _largest_valid_subrectangle(xs, zs, ((True, True), (False, True))),
            (0., .5, 1., 1.),
        )
        self.assertEqual(
            _largest_valid_subrectangle((0., 1., 2.), (0., 1.), ((True,), (True,))),
            (0., 0., 2., 1.),
        )

    def test_largest_valid_subrectangle_matches_bounded_bruteforce(self):
        randomizer = random.Random(7601)
        for _ in range(200):
            nx, nz = randomizer.randint(1, 5), randomizer.randint(1, 5)
            xs = tuple(float(value) for value in range(nx + 1))
            zs = tuple(float(value) for value in range(nz + 1))
            valid = tuple(tuple(randomizer.random() < .65 for _ in range(nz))
                          for _ in range(nx))
            expected = []
            for x0 in range(nx):
                for x1 in range(x0 + 1, nx + 1):
                    for z0 in range(nz):
                        for z1 in range(z0 + 1, nz + 1):
                            if all(valid[x][z]
                                   for x in range(x0, x1)
                                   for z in range(z0, z1)):
                                rectangle = (xs[x0], zs[z0], xs[x1], zs[z1])
                                area = (rectangle[2] - rectangle[0]) * (rectangle[3] - rectangle[1])
                                expected.append((-area, *rectangle))
            actual = _largest_valid_subrectangle(xs, zs, valid)
            self.assertEqual(actual, None if not expected else tuple(min(expected)[1:]))

    def test_v9_preserves_v8_and_freezes_unique_inputs(self):
        validate_taxonomy()
        v8 = v8_manifest()
        v9 = materialized_manifest()
        self.assertEqual(v9["v8_tasks"], v8["tasks"])
        self.assertEqual(v9["v8_clutter_scan"], v8["clutter_scan"])
        self.assertEqual(len(v9["support_region_cases"]), 216)
        self.assertEqual(len({row["id"] for row in v9["support_region_cases"]}), 216)
        self.assertEqual(
            input_digest(v9["support_region_cases"]),
            v9["expected_inputs"]["support_region_input_sha256"],
        )
        self.assertEqual(tuple(v9["support_region_inputs"]["families"]), FAMILIES)
        self.assertTrue(all(
            "standable_point_exists_but_task_failed" in row["expected_output_fields"]
            for row in v9["support_region_cases"]
        ))

    def test_v8_failure_ids_and_independent_reference_labels_are_frozen(self):
        report = classify()
        self.assertEqual(report["source_rows"], 1904)
        self.assertEqual(report["no_route_count"], 41)
        self.assertEqual(report["reference_label_counts"], {
            "REFERENCE_REACHABLE": 39,
            "UNRESOLVED": 2,
        })
        self.assertEqual(
            len(report["execution_failures"]["fixed_route_has_no_forward_control"]), 9
        )
        self.assertEqual(len(report["execution_failures"]["fixed_route_stalled"]), 5)
        frozen = json.loads(MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(frozen["frozen_no_route_labels"], report["reference_labels"])
        self.assertEqual(frozen["frozen_execution_failures"], report["execution_failures"])
        source = inspect.getsource(independent_reference_label)
        self.assertNotIn("known_map_planner", source)
        self.assertNotIn("astar", source.lower())
        case = next(row for row in v8_manifest()["clutter_scan"]
                    if row["id"] == report["reference_labels"][0]["id"])
        exhausted = independent_reference_label(case, maximum_states=4096, maximum_expansions=0)
        self.assertEqual(exhausted["label"], "UNRESOLVED")
        self.assertEqual(exhausted["reason"], "budget_exhausted")

    def test_red_platform_corner_keeps_a_positive_supported_region(self):
        _, world = _platform_world()
        goal = Aabb(4.2, 63.9, 10.2, 5.2, 64.1, 11.2)
        surface = query_support_surfaces(world, 4, 10, 64., 64.).surfaces[0]
        self.assertIs(standable_point_in_region(world, surface, goal).status, QueryStatus.FEASIBLE)
        selected = standable_region_in_goal(world, surface, goal, connection_from=(.5, 64., .5))
        self.assertIs(selected.status, QueryStatus.FEASIBLE)
        self.assertIsNotNone(selected.completion_region)
        bounds = selected.completion_region.bounds
        self.assertGreater((bounds.max_x - bounds.min_x) * (bounds.max_z - bounds.min_z), 0.)

    def test_red_cross_piece_returns_largest_safe_rectangle(self):
        _, world = _cross_piece_world()
        surface = query_support_surfaces(world, 0, 0, 64., 64.).surfaces[0]
        goal = Aabb(.6, 63.92, .2, 1., 64.08, 1.)
        selected = standable_region_in_goal(world, surface, goal)
        self.assertIs(selected.status, QueryStatus.FEASIBLE)
        bounds = selected.completion_region.bounds
        self.assertEqual(
            (bounds.min_x, bounds.min_z, bounds.max_x, bounds.max_z),
            (.6, .2, 1., .7),
        )
        for i in range(21):
            for j in range(21):
                x = .6 + .4 * i / 20
                z = .2 + .5 * j / 20
                body = Aabb(x - .3, 64., z - .3, x + .3, 65.8, z + .3)
                self.assertIs(sweep(body, (0., 0., 0.), world).status, QueryStatus.FEASIBLE)
                self.assertGreaterEqual(query_support(body, world).support_fraction, .5)

    def test_region_query_reports_unified_grid_size_without_changing_result(self):
        _, world = _cross_piece_world()
        surface = query_support_surfaces(world, 0, 0, 64., 64.).surfaces[0]
        goal = Aabb(.6, 63.92, .2, 1., 64.08, 1.)
        diagnostics = StandableRegionDiagnostics()
        selected = standable_region_in_goal(world, surface, goal, diagnostics=diagnostics)
        self.assertIs(selected.status, QueryStatus.FEASIBLE)
        self.assertGreaterEqual(diagnostics.x_boundaries, 2)
        self.assertGreaterEqual(diagnostics.z_boundaries, 2)
        self.assertEqual(
            diagnostics.cell_count,
            (diagnostics.x_boundaries - 1) * (diagnostics.z_boundaries - 1),
        )
        self.assertGreater(diagnostics.valid_cell_count, 0)

    def test_random_unified_regions_are_dense_safe_and_deterministic(self):
        randomizer = random.Random(7602)
        for case in range(120):
            solids = {(x, 63, z): STONE
                      for x in range(-2, 3) for z in range(-2, 3)
                      if (x, z) == (0, 0) or randomizer.random() > .18}
            for x in range(-1, 2):
                for z in range(-1, 2):
                    if (x, z) != (0, 0) and randomizer.random() < .22:
                        solids[(x, 64, z)] = STONE

            scene = Scene(solids, ((-4, 5), (60, 68), (-4, 5)))
            backend = CalculatorBackend([0], scene, (.5, 64., .5), 0.)
            world = backend.world._world
            surface = SupportSurface(
                SurfaceNodeId(0, 0, 64, 0),
                (.5, 64., .5),
                HorizontalRegion(0., 0., 1., 1.),
                1.,
                (STONE,),
                ((0, 63, 0),),
            )
            goal = Aabb(.05, 63.92, .05, .95, 64.08, .95)
            selected = standable_region_in_goal(world, surface, goal)
            self.assertEqual(selected, standable_region_in_goal(world, surface, goal))
            if selected.status is not QueryStatus.FEASIBLE:
                continue
            bounds = selected.completion_region.bounds
            with self.subTest(case=case, bounds=bounds):
                self.assertGreater(bounds.max_x - bounds.min_x, 0.)
                self.assertGreater(bounds.max_z - bounds.min_z, 0.)
                for i in range(13):
                    for j in range(13):
                        x = bounds.min_x + (bounds.max_x-bounds.min_x)*i/12
                        z = bounds.min_z + (bounds.max_z-bounds.min_z)*j/12
                        body = Aabb(x-.3, 64., z-.3, x+.3, 65.8, z+.3)
                        self.assertIs(sweep(body, (0., 0., 0.), world).status,
                                      QueryStatus.FEASIBLE)
                        support = query_support(body, world)
                        self.assertIs(support.status, QueryStatus.FEASIBLE)
                        self.assertGreaterEqual(support.support_fraction+1.e-9, .5)

    def test_red_replay_keeps_bound_region_when_outside_area_improves(self):
        from tests.sim.f2r_cases import goal_for, layout
        scene, start, position = layout("diagonal_pillar")
        backend = CalculatorBackend([0], scene, start, 0.)
        world = backend.truth
        surface = query_support_surfaces(world.view(), 1, 6, 64., 64.).surfaces[0]
        goal = goal_for(position, "product")
        selected = standable_region_in_goal(world.view(), surface, goal.region, connection_from=start)
        args = StandableRegionQueryArgs(surface, goal.region, start, selected.completion_region)
        recipe = WalkValidationRecipe(
            "f2s-bound-region",
            WalkValidationQueryKind.STANDABLE_REGION,
            None,
            None,
            PROFILES.ground,
            GroundCapabilityIdentity.from_profile(PROFILES.ground),
            selected.completion_region.dependencies,
            args,
        )
        self.assertIs(replay_walk_validation_recipe(recipe, world.view())[0], QueryStatus.FEASIBLE)
        world.confirm_air(
            ObservationStamp(backend.state.session, 2, 2, "f2s-outside-improved", 100_000_000),
            ((2, 64, 7), (2, 65, 7), (2, 66, 7)),
        )
        bounds = selected.completion_region.bounds
        for x in (bounds.min_x, bounds.max_x):
            for z in (bounds.min_z, bounds.max_z):
                body = Aabb(x - .3, 64., z - .3, x + .3, 65.8, z + .3)
                self.assertIs(sweep(body, (0., 0., 0.), world.view()).status, QueryStatus.FEASIBLE)
                self.assertGreaterEqual(query_support(body, world.view()).support_fraction, .5)
        self.assertIs(replay_walk_validation_recipe(recipe, world.view())[0], QueryStatus.FEASIBLE)

    def test_formal_callers_do_not_use_point_selection_or_reselect_on_replay(self):
        import mc2p.motion_nav.known_map_planner as planner_module
        import mc2p.motion_nav.route_validation as validation_module

        self.assertNotIn(
            "standable_point_in_region",
            inspect.getsource(NavigationSession._surface_for_goal),
        )
        planner_source = inspect.getsource(planner_module)
        self.assertNotIn("standable_point_in_region", planner_source)
        replay_source = inspect.getsource(validation_module.replay_walk_validation_recipe)
        self.assertNotIn("standable_region_in_goal", replay_source)

    def _session_selection(self, statuses):
        _, world = _platform_world()
        x_by_index = (.5, .25, .75)
        surfaces_by_index = tuple(
            SupportSurface(
                SurfaceNodeId(0, 0, 64, index),
                (x_by_index[index], 64., .5),
                HorizontalRegion(0., 0., 1., 1.),
                1.,
                (STONE,),
                ((0, 63, 0),),
            )
            for index in range(len(statuses))
        )
        # The provider deliberately returns 2,0,1.  Surface 1 and 2 are both
        # .25 blocks from the goal centre, so SurfaceNodeId must break the tie.
        surfaces = tuple(surfaces_by_index[index] for index in (2, 0, 1))
        missing_cell = (9, 64, 9)
        calls = []

        def exact_query(query_world, surface, goal_region, **kwargs):
            calls.append((surface.node_id, kwargs.get("query_cache")))
            status = statuses[surface.node_id.surface_index]
            if status is QueryStatus.FEASIBLE:
                region = GroundCompletionRegion(
                    Aabb(.2, 63.95, .2, .8, 64.05, .8),
                    (.5, 64., .5),
                    64.,
                    (surface.node_id.column_x, surface.node_id.column_z,
                     surface.node_id.vertical_band, surface.node_id.surface_index),
                    ((0, 63, 0),),
                )
                return StandableRegionResult(status, region, region.dependencies)
            if status is QueryStatus.NEEDS_INFORMATION:
                return StandableRegionResult(status, missing_cells=(missing_cell,))
            return StandableRegionResult(status)

        support_result = SupportSurfaceResult(QueryStatus.FEASIBLE, surfaces, (), ())
        goal = GoalState(
            Aabb(0., 63.9, 0., 1., 64.1, 1.),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        with (
            patch.object(navigation_session_module, "query_support_surfaces",
                         return_value=support_result),
            patch.object(navigation_session_module, "standable_region_in_goal",
                         side_effect=exact_query, create=True),
        ):
            result = NavigationSession._surface_for_goal(SimpleNamespace(world=world), goal)
        return result, calls, missing_cell

    def test_red_session_first_feasible_stops_after_one_exact_query(self):
        (node, missing), calls, _ = self._session_selection((
            QueryStatus.FEASIBLE, QueryStatus.BLOCKED, QueryStatus.BLOCKED,
        ))
        self.assertEqual(node, SurfaceNodeId(0, 0, 64, 0))
        self.assertEqual(missing, ())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], SurfaceNodeId(0, 0, 64, 0))
        self.assertIsInstance(calls[0][1], WorldQueryCache)

    def test_relevant_missing_before_feasible_blocks_and_reuses_cache(self):
        (node, missing), calls, missing_cell = self._session_selection((
            QueryStatus.BLOCKED, QueryStatus.NEEDS_INFORMATION, QueryStatus.FEASIBLE,
        ))
        self.assertIsNone(node)
        self.assertEqual(missing, (missing_cell,))
        self.assertEqual(len(calls), 3)
        self.assertEqual(tuple(node for node, _ in calls), tuple(
            SurfaceNodeId(0, 0, 64, index) for index in range(3)
        ))
        self.assertEqual(len({id(cache) for _, cache in calls}), 1)
        self.assertIsInstance(calls[0][1], WorldQueryCache)

    def test_red_session_all_unavailable_queries_all_and_reports_missing(self):
        (node, missing), calls, missing_cell = self._session_selection((
            QueryStatus.BLOCKED, QueryStatus.NEEDS_INFORMATION, QueryStatus.UNSUPPORTED,
        ))
        self.assertIsNone(node)
        self.assertEqual(missing, (missing_cell,))
        self.assertEqual(len(calls), 3)
        self.assertEqual(tuple(node for node, _ in calls), tuple(
            SurfaceNodeId(0, 0, 64, index) for index in range(3)
        ))
        self.assertEqual(len({id(cache) for _, cache in calls}), 1)
        self.assertIsInstance(calls[0][1], WorldQueryCache)

    def test_red_formal_session_reaches_platform_outer_corner(self):
        import tests.sim.runner as runner
        scene, _ = _platform_world()
        goal_position = (4.7, 64., 10.7)
        from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
        goal = GoalState(
            Aabb(4.2, 63.9, 10.2, 5.2, 64.1, 11.2),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        )
        with patch.object(runner, "_goal", lambda *_args, **_kwargs: goal):
            result = runner.run(runner.Scenario(
                "f2s/platform_outer_corner/melee/south/normal",
                scene,
                (.5, 64., .5),
                goal_position,
                max_ticks=400,
            ))
        self.assertFalse(result.violations)
        self.assertEqual(
            result.outcome,
            "success",
            f"current formal failure: {result.reason} at {result.final_position}",
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.run_continuous_height_matrix import run_cases
from tests.sim.backend import Scene
from tests.sim.continuous_height_matrix import (
    height_action_matrix_cases,
    height_action_scenarios,
    matrix_scenario,
    small_height_matrix_cases,
    small_height_scenarios,
)
from tests.sim.continuous_height_planning_matrix import (
    load_planning_manifest,
    run_planning_case,
)
from tests.sim.runner import run


MANIFEST = Path(
    "tests/sim/manifests/continuous-height-full-matrix.json"
)


class ContinuousHeightFullMatrixTests(unittest.TestCase):
    def test_planning_matrix_freezes_one_thousand_mixed_surface_worlds(self) -> None:
        document = load_planning_manifest()

        self.assertEqual(
            (document["seed_start"], document["seed_end"]),
            (282001, 283000),
        )
        self.assertEqual(document["grid_size"], 8)
        self.assertEqual(len(document["materials"]), 4)

    def test_planning_matrix_sample_matches_dijkstra_reference(self) -> None:
        results = tuple(run_planning_case(seed) for seed in range(282001, 282013))

        self.assertTrue(all(result.matches_reference for result in results))
        self.assertTrue(any(result.reference_reachable for result in results))

    def test_manifest_freezes_shapes_speeds_directions_and_seeds(self) -> None:
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))

        self.assertEqual(
            document["schema_version"],
            "mc2p.continuous-height-full-matrix.v1",
        )
        self.assertEqual(
            (document["seed_start"], document["seed_end"]),
            (281001, 281100),
        )
        self.assertEqual(document["directions"], ["east", "south", "west", "north"])
        self.assertEqual(
            [band["id"] for band in document["entry_speed_bands"]],
            ["low", "medium", "high"],
        )
        self.assertEqual(len(document["small_height_families"]), 11)
        self.assertEqual(len(document["height_action_families"]), 9)
        self.assertEqual(
            len(set(document["small_height_families"])),
            len(document["small_height_families"]),
        )
        self.assertEqual(
            len(set(document["height_action_families"])),
            len(document["height_action_families"]),
        )
        self.assertEqual(document["component_repetitions"], 100)
        self.assertEqual(document["fabric_repetitions_per_direction"], 5)

    def test_calculator_scene_uses_real_authorized_shape_heights(self) -> None:
        scene = Scene({}, ((-1, 1), (60, 70), (-1, 1)))
        cases = {
            "minecraft:dirt_path": (0.9375,),
            "minecraft:white_carpet": (0.0625,),
            "minecraft:snow[layers=2]": (0.125,),
            "minecraft:snow[layers=4]": (0.375,),
            "minecraft:snow[layers=5]": (0.5,),
            "minecraft:smooth_stone_slab[type=bottom]": (0.5,),
            "minecraft:oak_stairs[facing=south,half=bottom,shape=straight]": (0.5, 1.0),
        }

        for block_id, expected_tops in cases.items():
            with self.subTest(block_id=block_id):
                geometry = scene.geometry(block_id)
                self.assertEqual(geometry.material_key, block_id.split("[", 1)[0])
                self.assertEqual(
                    tuple(sorted({box.max_y for box in geometry.boxes})),
                    expected_tops,
                )

    def test_each_frozen_small_height_family_reaches_formal_path(self) -> None:
        results = tuple(run(scenario) for scenario in small_height_scenarios())

        self.assertEqual(len(results), 11)
        self.assertEqual(
            tuple(result.scenario for result in results),
            tuple(json.loads(MANIFEST.read_text(encoding="utf-8"))[
                "small_height_families"
            ]),
        )
        self.assertFalse(
            tuple(
                (result.scenario, result.outcome, result.reason, result.violations)
                for result in results
                if result.outcome != "success" or result.violations
            ),
        )

    def test_each_frozen_height_action_family_reaches_formal_path(self) -> None:
        results = tuple(run(scenario) for scenario in height_action_scenarios())

        self.assertEqual(len(results), 9)
        self.assertEqual(
            tuple(result.scenario for result in results),
            tuple(json.loads(MANIFEST.read_text(encoding="utf-8"))[
                "height_action_families"
            ]),
        )
        self.assertFalse(tuple(
            (result.scenario, result.outcome, result.reason, result.violations)
            for result in results
            if result.outcome != "success" or result.violations
        ))

    def test_matrix_scenario_rotates_world_stairs_and_entry_velocity(self) -> None:
        base = next(
            scenario for scenario in small_height_scenarios()
            if scenario.name == "stairs_up"
        )

        east = matrix_scenario(
            base, direction="east", speed_blocks_per_second=2.5,
            seed=281001, late_probability=0.2,
        )
        north = matrix_scenario(
            base, direction="north", speed_blocks_per_second=.75,
            seed=281002, late_probability=0.0,
        )

        self.assertEqual(east.yaw_degrees, -90.0)
        self.assertEqual(east.start_velocity_blocks_per_tick, (.125, -0.0784, 0.0))
        self.assertTrue(any(
            "facing=east" in block_id
            for block_id in east.scene.solids.values()
        ))
        self.assertGreater(east.goal[0], east.start[0])
        self.assertTrue(east.perturbations.late_ticks)
        self.assertEqual(north.yaw_degrees, 180.0)
        self.assertEqual(north.start_velocity_blocks_per_tick, (0.0, -0.0784, -.0375))
        self.assertLess(north.goal[2], north.start[2])
        self.assertFalse(north.perturbations.late_ticks)

    def test_frozen_component_cases_cover_each_family_direction_and_speed(self) -> None:
        normal = small_height_matrix_cases("normal")
        late = small_height_matrix_cases("late")

        self.assertEqual(len(normal), 1_100)
        self.assertEqual(len(late), 1_100)
        for family in json.loads(MANIFEST.read_text(encoding="utf-8"))[
                "small_height_families"]:
            family_cases = tuple(case for case in late if case.family == family)
            self.assertEqual(len(family_cases), 100)
            self.assertEqual(
                {case.direction for case in family_cases},
                {"east", "south", "west", "north"},
            )
            self.assertEqual(
                {case.speed_band for case in family_cases},
                {"low", "medium", "high"},
            )
            self.assertTrue(all(case.scenario.perturbations.late_ticks
                                for case in family_cases))
        self.assertTrue(all(
            not case.scenario.perturbations.late_ticks for case in normal
        ))

    def test_matrix_runner_writes_a_frozen_summary_and_refuses_overwrite(self) -> None:
        case = small_height_matrix_cases("normal")[0]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "matrix"
            run_cases((case,), output)

            summary = json.loads(
                (output / "summary.json").read_text(encoding="utf-8")
            )
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["case_count"], 1)
            self.assertEqual(summary["outcomes"], {"task_success": 1})
            self.assertEqual(
                len((output / "results.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()),
                1,
            )
            self.assertFalse(tuple((output / "failures").iterdir()))
            with self.assertRaises(FileExistsError):
                run_cases((case,), output)

    def test_frozen_height_action_cases_cover_each_family_direction_and_speed(self) -> None:
        document = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cases = height_action_matrix_cases("normal")

        self.assertEqual(len(cases), 900)
        self.assertEqual(
            {case.family for case in cases},
            set(document["height_action_families"]),
        )
        self.assertEqual({case.direction for case in cases}, set(document["directions"]))
        self.assertEqual({case.speed_band for case in cases}, {"low", "medium", "high"})

    def test_verified_stair_descent_is_not_replaced_by_step_fallback(self) -> None:
        case = next(
            case for case in small_height_matrix_cases("normal")
            if case.family == "stairs_down" and case.seed == 281022
        )

        result = run(case.scenario)

        self.assertEqual((result.outcome, result.reason),
                         ("success", "goal_state_satisfied"))
        self.assertEqual(
            {row["action_kind"] for row in result.trace
             if row["action_kind"] is not None},
            {"WalkSegment"},
        )

    def test_replanned_overhanging_start_keeps_continuous_ground_proof(self) -> None:
        case = next(
            case for case in small_height_matrix_cases("late")
            if case.family == "snow_layers_2" and case.seed == 281060
        )

        result = run(case.scenario)

        self.assertEqual((result.outcome, result.reason),
                         ("success", "goal_state_satisfied"))
        self.assertEqual(
            {row["action_kind"] for row in result.trace
             if row["action_kind"] is not None},
            {"WalkSegment"},
        )

    def test_late_jump_landing_on_destination_support_continues_route(self) -> None:
        case = next(
            case for case in height_action_matrix_cases("late")
            if case.family == "jump_up_1" and case.seed == 281010
        )

        result = run(case.scenario)

        self.assertEqual(
            (result.outcome, result.reason),
            ("success", "goal_state_satisfied"),
        )
        self.assertFalse(result.violations)

    def test_no_input_frame_revokes_stale_navigation_movement(self) -> None:
        case = next(
            case for case in height_action_matrix_cases("late")
            if case.family == "step_down_1" and case.seed == 281067
        )

        result = run(case.scenario)

        self.assertEqual(
            (result.outcome, result.reason),
            ("success", "goal_state_satisfied"),
        )
        self.assertFalse(result.violations)

    def test_late_jump_keeps_body_owner_until_application_window_closes(self) -> None:
        case = next(
            case for case in height_action_matrix_cases("late")
            if case.family == "jump_up_1" and case.seed == 281046
        )

        result = run(case.scenario)

        self.assertEqual(
            (result.outcome, result.reason),
            ("success", "goal_state_satisfied"),
        )
        self.assertFalse(result.violations)


if __name__ == "__main__":
    unittest.main()

"""F1-C frozen formal-chain following manifest and gates."""
from __future__ import annotations

from dataclasses import replace
import json
import math
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.fixed_route import FixedRouteController, FixedRouteState

from tests.sim.known_world_following import (
    REPRESENTATIVE_SCENARIOS, SCENARIOS, SCENARIO_BY_NAME, TARGET_TRACK_ID,
    FollowScenario, FollowingBackend,
    run_manifest, run_scenario, run_scenario_with_trace, _RecordingTrace,
)
from tests.sim.product_metrics import revision_responses
from tests.sim.runner import lane


class F1KnownWorldFollowingTests(unittest.TestCase):
    def test_00_representative_scenarios_fail_fast_with_real_safety_monitor(self):
        for name in REPRESENTATIVE_SCENARIOS:
            with self.subTest(name=name):
                result = run_scenario(SCENARIO_BY_NAME[name])
                self.assertEqual(
                    result["formal_chain"],
                    "PlayerRuntimeV1->RuntimeNavigationDriver->KnownWorldFollowDriver",
                )
                self.assertEqual(result["i4_violations"], 0)
                self.assertEqual(result["safety_violations"], [])
                self.assertTrue(result["source_released"])

    def test_01_response_samples_follow_product_metric_semantics(self):
        result = run_scenario(SCENARIO_BY_NAME["straight_2_0"])

        self.assertIn("revision_response_outcomes", result)
        details = result["revision_response_details"]
        self.assertNotIn(1, [item["revision"] for item in details])
        self.assertEqual(
            sum(result["revision_response_outcomes"].values()),
            result["accepted_revisions"] - 1,
        )
        self.assertLessEqual(
            {item["end"] for item in details},
            {"effective", "superseded", "unanswered"},
        )
        self.assertLessEqual(
            {item["evidence"] for item in details},
            {"movement", "satisfied", "superseded", "unanswered"},
        )
        self.assertIn("movement", {item["evidence"] for item in details})
        self.assertIn("satisfied", {item["evidence"] for item in details})
        self.assertTrue(all(
            item["evidence"] in {"movement", "satisfied"}
            for item in details if item["end"] == "effective"
        ))
        self.assertTrue(result["gates"]["revision_response"])
        effective = sorted(
            item["response_ticks"] for item in details
            if item["end"] == "effective"
        )
        expected_p95 = effective[math.ceil(.95 * len(effective)) - 1]
        self.assertEqual(result["revision_response_p95_ticks"], expected_p95)
        self.assertIsInstance(result["revision_response_p95_ticks"], int)

    def test_04_normal_cancel_reports_distance_without_capability_gates(self):
        result = run_scenario(SCENARIO_BY_NAME["normal_cancel"])

        self.assertGreater(result["stable_raw_distance"]["sample_count"], 0)
        self.assertIsNotNone(result["stable_raw_distance"]["mean_blocks"])
        self.assertIsNotNone(result["stable_excess_lag"]["p95_blocks"])
        self.assertIsNone(result["gates"]["stable_lag"])
        self.assertIsNone(result["gates"]["final_within_hold"])
        self.assertIsNone(result["gates"]["planning_ratio"])
        self.assertIsNone(result["gates"]["revision_response"])
        self.assertTrue(result["gates"]["safety"])
        self.assertTrue(result["gates"]["terminal"])
        self.assertTrue(result["passed"])

    def test_02_entity_velocity_is_relative_blocks_per_tick(self):
        clock = [100_000_000]
        scenario = FollowScenario("relative-velocity-probe", 3.3)
        backend = FollowingBackend(
            clock, lane([[63]] * 8, width=5).with_floor(),
            (.5, 64.0, .5), scenario=scenario,
        )
        backend.target_tick = 10
        backend.requested_track_id = TARGET_TRACK_ID
        backend.state = replace(
            backend.state,
            velocity_blocks_per_tick=(.04, 0.0, .07),
        )

        observation = backend.observation()
        visible = observation.perception.value.visible_entities[0]
        tracked = observation.tracked_entity.value
        self.assertIsNotNone(tracked)
        expected = (-.04, 0.0, .095)
        self.assertEqual(
            tuple(round(value, 12) for value in (
                visible.relative_velocity.x,
                visible.relative_velocity.y,
                visible.relative_velocity.z,
            )),
            expected,
        )
        self.assertEqual(visible.relative_velocity, tracked.relative_velocity)

    def test_03_response_tracker_supersedes_before_using_frozen_target(self):
        def row(tick, x, revision, requests, target):
            return {
                "movement_tick": tick,
                "position": (x, 64.0, 0.0),
                "driver_state": "running",
                "source_bound": True,
                "goal_satisfied": False,
                "goal_revision": revision,
                "goal_revision_requests": requests,
                "goal_position": target,
                "applied_movement": {"forward": 1, "strafe": 0},
            }

        responses = revision_responses([
            row(2, 0.0, 2, [{"revision": 2, "movement_tick": 1}],
                [10.0, 64.0, 0.0]),
            row(3, 1.0, 3, [{"revision": 3, "movement_tick": 2}],
                [-10.0, 64.0, 0.0]),
            row(4, 0.0, 3, [], [-10.0, 64.0, 0.0]),
        ], start_tick=1, start_position=(0.0, 64.0, 0.0))

        self.assertEqual(responses, [
            {"revision": 2, "response_ticks": None, "end": "superseded"},
            {"revision": 3, "response_ticks": 2, "end": "movement"},
        ])

    def test_d077_three_revisions_skip_only_irrelevant_unknown_without_stopping(self):
        unknown = tuple(
            (x, y, z)
            for x in range(-2, 3)
            for y in (64, 65)
            for z in (8, 12, 16)
        )
        current_decisions, samples = [], []
        original = FixedRouteController.decide

        def record(controller, frame, **kwargs):
            decision = original(controller, frame, **kwargs)
            current_decisions.append((controller._route, frame.body.position, decision))
            return decision

        def sample(row):
            samples.append((row, tuple(current_decisions)))
            current_decisions.clear()

        with patch.object(FixedRouteController, 'decide', new=record):
            result = run_scenario_with_trace(FollowScenario(
                "d064_three_revision_information",
                2.0,
                move_ticks=120,
                final_hold_ticks=20,
                initial_unknown_cells=unknown,
            ), _RecordingTrace(), trajectory_sink=sample)

        waits = result["revision_information_waits"]
        self.assertEqual(waits, [])
        self.assertNotIn(
            "stopping",
            {item["state"] for item in result["state_history"]},
        )
        self.assertEqual(result['revision_information_movement_gap_ticks'], [])
        self.assertGreaterEqual(result["accepted_revisions"], 3)
        self.assertEqual(result["task_recoveries"], 0)
        self.assertEqual(result["planning_submissions"], 0)

    def test_d064_resume_after_800_tick_hold_waits_for_new_facts(self):
        unknown = tuple(
            (x, y, z)
            for x in range(-3, 4)
            for y in (64, 65)
            # Region stopping happens before the old exact guide. Row 9 is
            # required by the first resumed direct target after the hold;
            # row 11 is reached only after a new incumbent already exists.
            for z in (9,)
        )
        scenario = FollowScenario(
            "d064_move_stop_800_resume_regression",
            2.0,
            move_ticks=33,
            pause_ticks=800,
            resume_ticks=33,
            final_hold_ticks=20,
            initial_unknown_cells=unknown,
        )
        result = run_scenario(scenario)

        pause_end = scenario.move_ticks + scenario.pause_ticks
        pause_states = {
            item["state"]
            for item in result["state_history"]
            if scenario.move_ticks < item["tick"] <= pause_end
        }
        self.assertFalse({"failed", "stopping"} & pause_states)
        self.assertEqual(result["task_recoveries"], 0)
        self.assertGreaterEqual(max(
            max(0, min(end, pause_end) - max(begin, scenario.move_ticks+1) + 1)
            for begin, end in result['zero_displacement_intervals']), 700)

        resume_waits = [
            item for item in result["revision_pre_control_states"]
            if item["tick"] > pause_end
            and item["missing_cell_count"] > 0
            and item["incumbent_route_id"] is None
        ]
        self.assertTrue(resume_waits)
        self.assertTrue(all(
            item["missing_cell_count"] > 0 for item in resume_waits
        ))
        self.assertTrue(all(
            item["missing_cell_count"] <= 128 for item in resume_waits
        ))
        self.assertTrue(all(
            item["state"] == "needs_information" and not item["terminal"]
            for item in resume_waits
        ))
        self.assertTrue(any(
            item['tick'] > resume_waits[0]['tick']
            and item['revision'] > resume_waits[0]['revision']
            and item['missing_cell_count'] == 0
            and item['incumbent_route_id'] is not None
            for item in result['revision_pre_control_states']))
        self.assertTrue(all(
            not item["terminal"] and item["state"] != "stopping"
            for item in result["revision_pre_control_states"]
            if item["tick"] > pause_end
            and item["missing_cell_count"] > 0
        ))
        self.assertEqual(
            result["revision_response_outcomes"]["unanswered"],
            0,
        )
        self.assertEqual(result["terminal_session_state"], "cancelled")
        self.assertTrue(result["source_released"])
        self.assertEqual(result["safety_violations"], [])

    def test_10_full_manifest_records_every_frozen_metric_and_honest_gate(self):
        report = run_manifest()

        self.assertEqual(
            report["scenario_order"],
            [scenario.name for scenario in SCENARIOS],
        )
        self.assertEqual(
            report["f1_c_complete"],
            not report["failed_scenarios"],
        )
        self.assertEqual(report["thresholds"], {
            "stable_mean_lag_blocks_max": .75,
            "stable_p95_lag_blocks_max": 1.5,
            "planning_submissions_per_revision_max": 1.25,
            "revision_response_p95_ticks_max": 5.0,
            "safety_violations_max": 0,
            "hold_distance_blocks": 2.5,
        })
        required = {
            "overall_raw_distance", "stable_raw_distance",
            "overall_excess_lag", "stable_excess_lag",
            "distance_sampling_windows", "final_raw_distance_blocks",
            "final_excess_lag_blocks", "revision_response_details",
            "revision_response_outcomes",
            "revision_response_p95_ticks", "accepted_revisions",
            "rejected_revisions", "throttled_revisions",
            "planning_submissions", "planning_submissions_per_accepted_revision",
            "task_recoveries", "controller_switches",
            "zero_displacement_intervals", "zero_displacement_ticks",
            "i4_violations", "safety_violations", "terminal_session_state",
            "gates", "passed",
        }
        by_name = {item["scenario"]: item for item in report["results"]}
        for result in report["results"]:
            self.assertTrue(required <= result.keys(), result["scenario"])
            overall_window = result["distance_sampling_windows"]["overall"]
            stable_window = result["distance_sampling_windows"]["stable"]
            self.assertTrue(overall_window["includes_initial_distance"])
            self.assertFalse(stable_window["includes_initial_distance"])
            for suffix in ("raw_distance", "excess_lag"):
                overall = result[f"overall_{suffix}"]
                stable = result[f"stable_{suffix}"]
                self.assertEqual(
                    overall["sample_count"], overall_window["sample_count"],
                )
                self.assertEqual(
                    stable["sample_count"], stable_window["sample_count"],
                )
                self.assertEqual(overall["final_blocks"],
                                 (result["final_raw_distance_blocks"]
                                  if suffix == "raw_distance" else
                                  result["final_excess_lag_blocks"]))
            self.assertEqual(
                result["passed"],
                all(value for value in result["gates"].values()
                    if value is not None),
            )

        stopped = by_name["move_stop_800_resume"]
        self.assertEqual(stopped["configuration"]["pause_ticks"], 800)
        self.assertGreaterEqual(
            max(end - begin + 1
                for begin, end in stopped["zero_displacement_intervals"]),
            700,
        )
        self.assertEqual(stopped["i4_violations"], 0)
        high_frequency = by_name["high_frequency_throttle"]
        ordinary = by_name["straight_3_3"]
        self.assertEqual(high_frequency["configuration"]["update_calls_per_tick"], 1)
        self.assertNotEqual(
            high_frequency["configuration"]["speed_blocks_per_second"],
            ordinary["configuration"]["speed_blocks_per_second"],
        )
        self.assertGreater(high_frequency["fresh_observation_updates"], 0)
        self.assertGreater(high_frequency["throttled_revisions"], 0)
        self.assertEqual(high_frequency["duplicate_updates"], 0)
        self.assertNotEqual(
            (high_frequency["accepted_revisions"],
             high_frequency["throttled_revisions"]),
            (ordinary["accepted_revisions"], ordinary["throttled_revisions"]),
        )
        self.assertGreaterEqual(
            by_name["one_missing_observation"]["observation_gap_updates"], 1,
        )
        delayed = by_name["first_input_late_one_tick"]
        self.assertIsNotNone(delayed["first_delayed_input_tick"])
        self.assertIn(
            ["late_input", delayed["first_delayed_input_tick"]],
            delayed["applied_perturbations"],
        )
        self.assertEqual(
            by_name["normal_cancel"]["terminal_session_state"], "cancelled",
        )
        self.assertIsNone(by_name["normal_cancel"]["gates"]["stable_lag"])
        self.assertIsNone(by_name["normal_cancel"]["gates"]["planning_ratio"])
        self.assertIsNone(
            by_name["normal_cancel"]["gates"]["revision_response"],
        )
        outside = by_name["straight_5_0_out_of_scope"]
        self.assertIsNone(outside["gates"]["stable_lag"])
        self.assertTrue(outside["gates"]["final_within_hold"])

        # The durable report must remain strict JSON even when gates fail.
        first = json.dumps(report, sort_keys=True, allow_nan=False)
        second = json.dumps(report, sort_keys=True, allow_nan=False)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

"""D129 post-submit INVALID_HARNESS_RUN must be recomputed from raw evidence."""
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import d129_invalid_harness as validator


RUN10 = Path(
    "evidence/motion_navigation/d123-proof-and-transport-v1/"
    "d128/smoke-run-10"
)


class D129InvalidHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence = validator.load_invalid_harness_evidence(RUN10)

    def test_run10_raw_manifest_and_seven_way_conjunction_pass(self):
        result = validator.evaluate_invalid_harness_evidence(self.evidence)
        self.assertEqual(result["status"], "PASS", result)
        self.assertTrue(result["eligible_for_invalid_harness"])
        self.assertEqual(result["manifest"]["verified_file_count"], 50)
        self.assertEqual(set(result["conjunction"]), {
            "fixture_or_tool_causally_broke_registered_precondition",
            "raw_request_receipt_ftick_physics_identity_is_unique",
            "tested_non_neutral_input_application_count_is_zero",
            "safe_default_application_count_is_zero",
            "body_did_not_enter_tested_behavior",
            "health_did_not_decrease",
            "no_independent_transport_protocol_or_product_safety_failure",
        })
        self.assertTrue(all(result["conjunction"].values()))
        self.assertTrue(all(not value for value in result["disqualifiers"].values()))

    def test_each_conjunct_and_every_disqualifier_is_fail_closed(self):
        mutations = []

        causal = deepcopy(self.evidence)
        causal["dispatch"]["pre_dispatch_elapsed_ns"] = 0
        mutations.append((causal,
            "fixture_or_tool_causally_broke_registered_precondition"))

        identity = deepcopy(self.evidence)
        identity["identity_records"]["dispatches"].append(deepcopy(
            identity["identity_records"]["dispatches"][0]
        ))
        mutations.append((identity,
            "raw_request_receipt_ftick_physics_identity_is_unique"))

        applied = deepcopy(self.evidence)
        application = applied["receipt"]["input_applications"][-1]
        application.update({
            "episode_id": applied["action"]["episode_id"],
            "request_sequence_id": applied["action"]["request_sequence_id"],
            "forward": 1.0, "strafe": 0.0, "jump": False,
            "sneak": False, "sprint": True, "state": "leased",
        })
        mutations.append((applied,
            "tested_non_neutral_input_application_count_is_zero"))

        defaulted = deepcopy(self.evidence)
        next(row for row in defaulted["physics"]
             if row["movement_tick_id"] >= 8)["input_state"] = "default_active"
        mutations.append((defaulted,
            "safe_default_application_count_is_zero"))

        moved = deepcopy(self.evidence)
        moved["physics"][-1]["post_state"]["position"]["z"] += 0.25
        mutations.append((moved, "body_did_not_enter_tested_behavior"))

        terminal_moved = deepcopy(self.evidence)
        terminal_moved["terminal_observation"]["position"][2] += 0.25
        mutations.append((terminal_moved,
                          "body_did_not_enter_tested_behavior"))

        damaged = deepcopy(self.evidence)
        damaged["terminal_observation"]["health_points"] -= 1.0
        mutations.append((damaged, "health_did_not_decrease"))

        transport = deepcopy(self.evidence)
        transport["trial"]["frames"][0]["transport_fact"] = {
            "kind": "delivery_unknown",
        }
        mutations.append((transport,
            "no_independent_transport_protocol_or_product_safety_failure"))

        for changed, failed_conjunct in mutations:
            with self.subTest(failed_conjunct=failed_conjunct):
                result = validator.evaluate_invalid_harness_evidence(changed)
                self.assertFalse(result["eligible_for_invalid_harness"])
                self.assertFalse(result["conjunction"][failed_conjunct])
                self.assertTrue(any(result["disqualifiers"].values()))

        for field, value in (
            ("runtime_failure", {"code": "contract"}),
            ("runtime_state", "failed"),
            ("driver_state", "failed"),
            ("error", {"type": "RuntimeError"}),
        ):
            changed = deepcopy(self.evidence)
            changed["trial"][field] = value
            with self.subTest(formal_failure=field):
                result = validator.evaluate_invalid_harness_evidence(changed)
                self.assertFalse(result["eligible_for_invalid_harness"])
                self.assertTrue(result["disqualifiers"]["formal_driver_or_runtime_failure"])

        neutral_application = deepcopy(self.evidence)
        neutral_row = neutral_application["receipt"]["input_applications"][-1]
        neutral_row.update({
            "episode_id": neutral_application["action"]["episode_id"],
            "request_sequence_id": (
                neutral_application["action"]["request_sequence_id"]
            ),
            "state": "neutral", "forward": 0.0, "strafe": 0.0,
            "jump": False, "sneak": False, "sprint": False,
        })
        result = validator.evaluate_invalid_harness_evidence(
            neutral_application,
        )
        self.assertTrue(result["conjunction"][
            "tested_non_neutral_input_application_count_is_zero"
        ])
        self.assertFalse(result["eligible_for_invalid_harness"])
        self.assertTrue(result["disqualifiers"][
            "any_tested_input_application"
        ])

        for field, malformed in (
            ("input_applications", "malformed"),
            ("timing_events", {"not": "a-list"}),
        ):
            changed = deepcopy(self.evidence)
            changed["receipt"][field] = malformed
            with self.subTest(malformed_receipt=field):
                result = validator.evaluate_invalid_harness_evidence(changed)
                self.assertFalse(result["eligible_for_invalid_harness"])
                self.assertTrue(result["disqualifiers"]["identity_not_unique"])

        dropped = deepcopy(self.evidence)
        dropped["receipt"]["dropped_input_samples"] = 1
        result = validator.evaluate_invalid_harness_evidence(dropped)
        self.assertFalse(result["eligible_for_invalid_harness"])
        self.assertTrue(result["disqualifiers"]["transport_fact"])

    def test_manifest_hash_mismatch_stops_before_evidence_parsing(self):
        original = validator._sha256

        def changed(path):
            value = original(path)
            return "0" * 64 if path.name == "summary.json" else value

        with patch.object(validator, "_sha256", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "manifest hash"):
                validator.load_invalid_harness_evidence(RUN10)

    def test_interval_physics_and_observations_are_complete(self):
        missing_tick = deepcopy(self.evidence)
        del missing_tick["physics"][50]

        duplicate_tick = deepcopy(self.evidence)
        duplicate_tick["physics"].insert(
            51, deepcopy(duplicate_tick["physics"][50]),
        )

        health_recovered = deepcopy(self.evidence)
        health_recovered["interval_observations"] = [
            deepcopy(health_recovered["start_observation"]),
            {
                **deepcopy(health_recovered["start_observation"]),
                "sequence_id": 8,
                "movement_tick_id": 35,
                "health_points": 19.0,
            },
            deepcopy(health_recovered["terminal_observation"]),
        ]

        damaged_then_clear = deepcopy(self.evidence)
        damaged_then_clear["interval_observations"] = [
            deepcopy(damaged_then_clear["start_observation"]),
            {
                **deepcopy(damaged_then_clear["start_observation"]),
                "sequence_id": 8,
                "movement_tick_id": 35,
                "damage_events": [{"kind": "test-damage"}],
            },
            deepcopy(damaged_then_clear["terminal_observation"]),
        ]

        for name, changed, disqualifier in (
            ("missing_tick", missing_tick, "identity_not_unique"),
            ("duplicate_tick", duplicate_tick, "identity_not_unique"),
            ("health_recovered", health_recovered, "health_damage"),
            ("damage_then_clear", damaged_then_clear, "health_damage"),
        ):
            with self.subTest(name=name):
                result = validator.evaluate_invalid_harness_evidence(changed)
                self.assertFalse(result["eligible_for_invalid_harness"])
                self.assertTrue(result["disqualifiers"][disqualifier])


if __name__ == "__main__":
    unittest.main()

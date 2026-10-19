import unittest
import re

from scripts.c1_moving_melee_runtime import (
    C1B_NEGATIVE_INJECTIONS,
    _latency_summary,
    c1b_trial_plan,
    evaluate_c1b_positive,
    summarize_c1b_trials,
    validate_seed_receipt,
)


class C1MovingMeleeRuntimeTests(unittest.TestCase):
    def test_plan_freezes_twenty_unique_positives_and_ten_negatives(self):
        rows = c1b_trial_plan(21001)
        positives = [row for row in rows if row["classification"] == "positive"]
        negatives = [row for row in rows if row["classification"] == "negative"]
        self.assertEqual(len(positives), 20)
        self.assertEqual(len({(row["direction"], row["ai_seed"])
                              for row in positives}), 20)
        self.assertEqual({row["direction"] for row in positives},
                         {"north", "east", "south", "west"})
        self.assertEqual({row["injection"] for row in negatives},
                         set(C1B_NEGATIVE_INJECTIONS))
        self.assertEqual(len(negatives), 10)
        self.assertTrue(all(re.fullmatch(r"[a-z0-9-]{1,80}", row["trial_id"])
                            for row in rows))

    def test_positive_requires_every_frozen_completion_fact(self):
        trial = next(row for row in c1b_trial_plan(21001)
                     if row["classification"] == "positive"
                     and row["requires_engagement_position"])
        evidence = {
            "fixture_valid": True,
            "target_displacement_before_first_attack": 1.1,
            "confirmed_hits": 2,
            "reapproaches": 1,
            "engagement_position_uses": 1,
            "explicit_death": True,
            "attacks_after_death": 0,
            "player_health_delta": 0.0,
            "external_speed_change": 0.0,
            "terminal_state": "complete",
        }
        self.assertEqual(evaluate_c1b_positive(trial, evidence), ())
        for field, bad in (
            ("target_displacement_before_first_attack", .99),
            ("confirmed_hits", 1), ("reapproaches", 0),
            ("engagement_position_uses", 0), ("explicit_death", False),
            ("attacks_after_death", 1), ("player_health_delta", -.5),
            ("external_speed_change", .01), ("terminal_state", "failed"),
        ):
            changed = dict(evidence, **{field: bad})
            self.assertTrue(evaluate_c1b_positive(trial, changed), field)

    def test_fixture_invalid_is_separate_but_reachable_timeout_stays_in_denominator(self):
        rows = [
            {"classification": "positive", "fixture_valid": True, "passed": True},
            {"classification": "positive", "fixture_valid": True, "passed": False,
             "terminal_state": "timeout"},
            {"classification": "positive", "fixture_valid": False, "passed": False},
            {"classification": "negative", "fixture_valid": True, "passed": True},
        ]
        summary = summarize_c1b_trials(rows)
        self.assertEqual((summary["positive_passed"], summary["positive_total"]), (1, 2))
        self.assertEqual(summary["fixture_invalid"], 1)
        self.assertEqual((summary["negative_passed"], summary["negative_total"]), (1, 1))

    def test_seed_receipt_must_match_and_precede_first_ai_tick(self):
        trial = next(row for row in c1b_trial_plan(21001)
                     if row["classification"] == "positive")
        receipt = {
            "event": "spawned", "scenario_id": trial["trial_id"],
            "seed": trial["ai_seed"], "spawn_tick": 7, "seed_applied_tick": 7,
            "seed_applied_before_first_ai_tick": True,
        }
        self.assertTrue(validate_seed_receipt(trial, receipt))
        self.assertFalse(validate_seed_receipt(trial, dict(
            receipt, seed_applied_before_first_ai_tick=False)))
        self.assertFalse(validate_seed_receipt(trial, dict(receipt, seed=1)))

    def test_control_latency_uses_nearest_rank_over_all_frames(self):
        summary = _latency_summary(range(1, 101))
        self.assertEqual(summary, {
            "count": 100, "p50": 50.0, "p95": 95.0,
            "p99": 99.0, "max": 100.0,
        })


if __name__ == "__main__":
    unittest.main()

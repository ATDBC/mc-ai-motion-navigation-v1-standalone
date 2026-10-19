from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from scripts.b12_attack_evidence_runtime import (
    B12A_FABRIC_NEGATIVE_CASES,
    B12A_NEGATIVE_CASES,
    B12A_RUNTIME_INJECTION_CASES,
    b12a_trial_plan,
    evaluate_b12a_trial,
    summarize_b12a_trials,
    write_b12a_manifest,
)
from scripts.b12a_fabric_runtime import evaluate_damage_source_diagnostics


class B12AttackEvidenceRuntimeTests(unittest.TestCase):
    def test_damage_source_diagnostics_separate_self_attack_from_environment(self):
        diagnostics = {
            "player_attack": {
                "outcome": "source_confirmed_hit",
                "evidence_grade": "source_confirmed",
                "event_sequence_id": 4,
                "damage_type": "minecraft:player_attack",
            },
            "environment_damage": {
                "target_matched": True,
                "event_sequence_id": 5,
                "damage_type": "minecraft:on_fire",
                "source_entity_present": False,
                "source_is_self": False,
                "direct_entity_present": False,
                "direct_source_is_self": False,
            },
        }

        self.assertTrue(all(
            check["passed"]
            for check in evaluate_damage_source_diagnostics(diagnostics)
        ))
        diagnostics["environment_damage"]["source_is_self"] = True
        self.assertFalse(all(
            check["passed"]
            for check in evaluate_damage_source_diagnostics(diagnostics)
        ))

    def test_plan_freezes_positive_and_boundary_groups_with_unique_seeds(self):
        trials = b12a_trial_plan(21001)
        positives = [row for row in trials if row["classification"] == "positive"]
        negatives = [row for row in trials if row["classification"] == "negative"]

        self.assertEqual(len(positives), 40)
        self.assertEqual(
            {(row["scenario"], row["repeat"]) for row in positives},
            {(scenario, repeat)
             for scenario in ("fixed_visible", "moving_visible")
             for repeat in range(1, 21)},
        )
        for scenario in ("fixed_visible", "moving_visible"):
            group = [row for row in positives if row["scenario"] == scenario]
            self.assertEqual(
                {direction: sum(row["direction"] == direction for row in group)
                 for direction in ("north", "east", "south", "west")},
                {"north": 5, "east": 5, "south": 5, "west": 5},
            )
        moving = [row for row in positives if row["scenario"] == "moving_visible"]
        self.assertEqual({row["ai_seed"] for row in moving},
                         {31001, 31002, 31003, 31004, 31005})
        self.assertEqual(len(negatives), 2 * len(B12A_NEGATIVE_CASES))
        self.assertEqual(
            {row["injection"] for row in negatives},
            set(B12A_NEGATIVE_CASES),
        )
        self.assertEqual(len({row["trial_id"] for row in trials}), len(trials))
        self.assertEqual(len({row["scenario_seed"] for row in trials}), len(trials))
        self.assertEqual(
            set(B12A_FABRIC_NEGATIVE_CASES)
            | set(B12A_RUNTIME_INJECTION_CASES),
            set(B12A_NEGATIVE_CASES),
        )
        self.assertFalse(
            set(B12A_FABRIC_NEGATIVE_CASES)
            & set(B12A_RUNTIME_INJECTION_CASES)
        )
        self.assertTrue(all(
            row["evidence_source"] == "fabric"
            for row in positives
        ))
        self.assertTrue(all(
            row["evidence_source"] == (
                "fabric" if row["injection"] in B12A_FABRIC_NEGATIVE_CASES
                else "deterministic_runtime"
            )
            for row in negatives
        ))
        session_change = next(row for row in negatives
                              if row["injection"] == "world_session_change")
        self.assertEqual(session_change["expected_attempt_outcome"], "cancelled")
        self.assertEqual(session_change["expected_terminal_state"], "cancelled")

    def test_positive_requires_online_replay_agreement_and_correlated_hit(self):
        trial = next(row for row in b12a_trial_plan(21001)
                     if row["scenario"] == "fixed_visible")
        evidence = {
            "fixture_valid": True,
            "runtime_state": "ready",
            "terminal_state": "complete",
            "online_outcomes": ["command_correlated_hit"],
            "replayed_outcomes": ["command_correlated_hit"],
            "engagement_grants": 1,
            "task_outcome": None,
        }
        self.assertEqual(evaluate_b12a_trial(trial, evidence), ())

        source_confirmed = dict(
            evidence,
            online_outcomes=["source_confirmed_hit"],
            replayed_outcomes=["source_confirmed_hit"],
        )
        self.assertEqual(evaluate_b12a_trial(trial, source_confirmed), ())

        health_only = dict(
            evidence,
            online_outcomes=["confirmation_timeout"],
            replayed_outcomes=["confirmation_timeout"],
        )
        self.assertIn(
            "missing_confirmed_hit",
            evaluate_b12a_trial(trial, health_only),
        )
        mismatch = dict(evidence, replayed_outcomes=["observation_interrupted"])
        self.assertIn("online_replay_mismatch", evaluate_b12a_trial(trial, mismatch))

    def test_negative_requires_declared_outcome_and_never_false_grants_engagement(self):
        plan = b12a_trial_plan(21001)
        trial = next(row for row in plan if row.get("injection") == "health_decline_only")
        evidence = {
            "fixture_valid": True,
            "runtime_state": "ready",
            "terminal_state": "complete",
            "online_outcomes": ["confirmation_timeout"],
            "replayed_outcomes": ["confirmation_timeout"],
            "unattributed_damage_facts": 1,
            "engagement_grants": 0,
            "task_outcome": None,
        }
        self.assertEqual(evaluate_b12a_trial(trial, evidence), ())
        self.assertIn(
            "unexpected_engagement_grant",
            evaluate_b12a_trial(trial, dict(evidence, engagement_grants=1)),
        )

        exhausted = next(row for row in plan
                         if row.get("injection") == "input_budget_exhausted")
        exhausted_evidence = dict(
            evidence,
            online_outcomes=["input_failed", "input_failed"],
            replayed_outcomes=["input_failed", "input_failed"],
            unattributed_damage_facts=0,
            terminal_state="needs_task_decision",
            task_outcome="input_retry_exhausted",
        )
        self.assertEqual(evaluate_b12a_trial(exhausted, exhausted_evidence), ())

    def test_summary_keeps_reachable_timeout_in_denominator(self):
        rows = [
            {"classification": "positive", "fixture_valid": True,
             "passed": True, "terminal_state": "complete"},
            {"classification": "positive", "fixture_valid": True,
             "passed": False, "terminal_state": "timeout"},
            {"classification": "positive", "fixture_valid": False,
             "passed": False, "terminal_state": "fixture_invalid"},
            {"classification": "negative", "fixture_valid": True,
             "passed": True, "injection": "gate_rejected"},
        ]
        summary = summarize_b12a_trials(rows)
        self.assertEqual((summary["positive_passed"], summary["positive_total"]), (1, 2))
        self.assertEqual((summary["negative_passed"], summary["negative_total"]), (1, 1))
        self.assertEqual(summary["fixture_invalid"], 1)
        self.assertEqual(summary["positive_timeouts"], 1)

    def test_manifest_records_frozen_plan_and_hashes(self):
        with TemporaryDirectory(prefix="mc2p-b12a-") as directory:
            path = write_b12a_manifest(
                Path(directory), world_seed=21001,
                code_hashes={"b.py": "b", "a.py": "a"},
            )
            payload = json.loads(path.read_text("utf-8"))
        self.assertEqual(payload["schema_version"], "mc2p.b12a-manifest.v1")
        self.assertEqual(payload["world_seed"], 21001)
        self.assertEqual(list(payload["code_hashes"]), ["a.py", "b.py"])
        self.assertEqual(payload["trials"], list(b12a_trial_plan(21001)))


if __name__ == "__main__":
    unittest.main()

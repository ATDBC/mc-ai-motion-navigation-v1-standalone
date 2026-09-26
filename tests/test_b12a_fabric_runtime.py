"""B12-A Fabric-only additions keep a frozen, minimal scene list."""
import unittest
from unittest.mock import patch

from mc2p.skills.attack_evidence import (
    AttackAttemptKeyV1, AttackAttemptOutcome, AttackAttemptPhase,
    AttackAttemptReportV1, AttackEvidenceGrade,
)
from scripts.b12a_fabric_runtime import (
    b12a_fabric_trial_plan, evaluate_b12a_fabric_records,
    unattributed_death_fixture_commands,
)


class B12AFabricRuntimeTests(unittest.TestCase):
    def test_plan_only_contains_game_dependent_cases_not_already_in_c1_gate_run(self):
        rows = b12a_fabric_trial_plan(21001)

        self.assertEqual(len(rows), 8)
        self.assertEqual(
            {row["injection"] for row in rows},
            {
                "confirmation_timeout", "health_decline_only",
                "unattributed_death", "target_revision_after_submit",
            },
        )
        self.assertEqual(
            {row["repeat"] for row in rows},
            {1, 2},
        )
        self.assertEqual(len({row["trial_id"] for row in rows}), 8)
        self.assertEqual(len({row["scenario_seed"] for row in rows}), 8)
        self.assertTrue(all(row["evidence_source"] == "fabric" for row in rows))

    def test_unattributed_death_fixture_does_not_use_damage_command(self):
        commands = unattributed_death_fixture_commands()

        self.assertEqual(len(commands), 1)
        self.assertIn("Health:0.0f", commands[0])
        self.assertNotIn("kill ", commands[0])

    def test_evaluator_requires_online_and_replayed_outcomes_to_match(self):
        trial = dict(b12a_fabric_trial_plan(21001)[0])
        key = AttackAttemptKeyV1(
            "episode-1", "task-1", "goal-1", 1, "entity-1", 1,
        )
        row = {
            **trial,
            "attempt_key": {
                "episode_id": key.episode_id,
                "task_id": key.task_id,
                "goal_id": key.goal_id,
                "target_revision": key.target_revision,
                "track_id": key.track_id,
                "attempt_sequence": key.attempt_sequence,
            },
            "attempt_outcome": trial["expected_attempt_outcome"],
            "runtime_state": "ready",
            "injection_applied": True,
        }
        replayed = AttackAttemptReportV1(
            key, AttackAttemptPhase.TERMINAL,
            AttackAttemptOutcome.CONFIRMATION_TIMEOUT,
            AttackEvidenceGrade.NONE,
            intent_id="intent-1", action_request_sequence_id=2,
            attack_observation_sequence_id=3,
            confirmation_deadline_ns=4, receipt_status="pending_confirmation",
        )
        with patch(
            "scripts.b12a_fabric_runtime.replay_attack_attempt",
            return_value=replayed,
        ):
            rows, checks = evaluate_b12a_fabric_records((row,), ())

        self.assertTrue(rows[0]["passed"])
        self.assertTrue(checks[0]["passed"])


if __name__ == "__main__":
    unittest.main()

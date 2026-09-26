"""Shared one-strike core has no navigation and preserves causal attack evidence."""
import unittest

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, AttackEntityV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.reset import ResetRequestV0
from mc2p.contracts.intent_source import (
    ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id,
)
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.attack_evidence import (
    AttackAttemptOutcome, AttackAttemptPhase, AttackEvidenceGrade,
)
from mc2p.skills.fixed_melee import CombatTargetV1
from mc2p.skills.melee_strike_driver import MeleeStrikeDriver, MeleeStrikeOutcome
from tests.test_fixed_melee_driver import MeleeBackend, TRACK
from tests.test_player_runtime import _RecordingTrace


class MeleeStrikeDriverTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100_000_000]
        self.backend = MeleeBackend(self.clock)
        self.trace = _RecordingTrace()
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset", "episode-1", "test", 1, 2_000_000_000)
        ).succeeded)
        self.profile = BehaviorProfileV0()
        self.target = CombatTargetV1(
            "combat-task", "combat-goal", 1, "episode-1", TRACK,
        )

    def tearDown(self):
        self.runtime.close()

    def driver(self):
        return MeleeStrikeDriver(self.runtime, clock_ns=lambda: self.clock[0])

    def test_far_target_requests_approach_without_creating_navigation(self):
        self.runtime.close()
        self.backend = MeleeBackend(self.clock, distance=5.0)
        self.runtime = PlayerRuntimeV1(self.backend, self.trace, lambda: self.clock[0])
        self.assertTrue(self.runtime.reset(
            ResetRequestV0("reset-far", "episode-1", "test", 1, 2_000_000_000)
        ).succeeded)
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        self.assertEqual(driver.report.state, "needs_approach")
        self.assertFalse(driver.report.terminal)
        self.assertFalse(hasattr(driver, "approach_driver"))
        self.assertEqual(self.backend.actions, [])

    def test_explicit_death_completes_without_submitting_attack(self):
        self.backend.dead = True
        self.backend.health = 0.0
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        for _ in range(3):
            if driver.report.terminal:
                break
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("complete", "target_dead"))
        self.assertFalse(any(type(action.operation) is AttackEntityV1
                             for action in self.backend.actions))

    def test_submitted_attack_cancel_keeps_actual_hit_result_without_resend(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        while not driver.report.attack_submitted:
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        driver.cancel(self.profile, "test_cancel")
        attacks = [action for action in self.backend.actions
                   if type(action.operation) is AttackEntityV1]
        self.assertEqual(len(attacks), 1)
        self.assertEqual(driver.report.state, "cancelled")
        self.assertTrue(driver.report.hit_observed)

    def test_target_revision_after_submit_closes_old_attempt_without_reusing_hit(self):
        self.backend.confirm_hit = False
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        while not driver.report.attack_submitted:
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        driver.replace_target(CombatTargetV1(
            self.target.task_id, self.target.goal_id, 2,
            self.target.episode_id, self.target.track_id,
        ), self.clock[0])

        self.assertTrue(driver.report.terminal)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("failed", "target_revised_after_submit"))
        self.assertIs(
            driver.attempt_report.outcome,
            AttackAttemptOutcome.TARGET_REVISED,
        )
        self.assertFalse(driver.report.hit_observed)

    def test_external_recovery_cannot_erase_a_confirmed_hit(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        while not driver.report.attack_submitted:
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        driver.interrupt_for_external_motion(self.profile, "damage_knockback")
        driver.observe_after_external_motion(self.runtime.observation)
        self.assertEqual((driver.report.state, driver.report.reason),
                         ("complete", "hit_confirmed"))

        self.backend.hurt = 0
        self.backend.sequence += 1
        self.clock[0] += 50_000_000
        driver.observe_after_external_motion(self.backend.observation())

        self.assertEqual((driver.report.state, driver.report.reason),
                         ("complete", "hit_confirmed"))

    def test_confirmation_timeout_returns_typed_unconfirmed_outcome(self):
        self.backend.confirm_hit = False
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        for _ in range(40):
            if driver.report.terminal:
                break
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.assertEqual(driver.report.state, "unconfirmed")
        self.assertEqual(driver.report.outcome, MeleeStrikeOutcome.UNCONFIRMED)
        self.assertEqual(driver.report.attack_submissions, 1)

    def test_operation_rejection_is_retryable_without_failing_runtime(self):
        self.backend.operation_reject_reason = "entity_target_mismatch"
        driver = self.driver()
        driver.start(self.target, self.clock[0])

        for _ in range(12):
            if driver.report.terminal:
                break
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        self.assertEqual(driver.report.state, "operation_rejected")
        self.assertEqual(
            driver.report.outcome,
            MeleeStrikeOutcome.RETRYABLE_OPERATION_REJECTION,
        )
        self.assertEqual(driver.report.attack_submissions, 1)
        self.assertEqual(self.runtime.state.value, "ready")

    def test_confirmed_hit_carries_command_bound_attempt_evidence(self):
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        for _ in range(12):
            if driver.report.terminal:
                break
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        attempt = driver.attempt_report
        self.assertIs(attempt.phase, AttackAttemptPhase.TERMINAL)
        self.assertIs(attempt.outcome, AttackAttemptOutcome.COMMAND_CORRELATED_HIT)
        self.assertIs(attempt.evidence_grade, AttackEvidenceGrade.COMMAND_CORRELATED)
        self.assertIsNotNone(attempt.intent_id)
        self.assertIsNotNone(attempt.action_request_sequence_id)
        self.assertIsNotNone(attempt.attack_observation_sequence_id)
        self.assertEqual(attempt.receipt_status, "pending_confirmation")

    def test_health_decline_alone_stays_unattributed_until_timeout(self):
        self.backend.confirm_hit = False
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        while not driver.report.attack_submitted:
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        self.backend.health = 18.0
        driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.assertFalse(driver.report.terminal)
        self.assertTrue(driver.attempt_report.target_damaged_unattributed)
        self.assertIs(
            driver.attempt_report.evidence_grade,
            AttackEvidenceGrade.TARGET_STATE_ONLY,
        )

        for _ in range(30):
            if driver.report.terminal:
                break
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.assertIs(
            driver.attempt_report.outcome,
            AttackAttemptOutcome.CONFIRMATION_TIMEOUT,
        )
        self.assertFalse(driver.report.hit_observed)

    def test_unattributed_death_completes_task_without_confirming_hit(self):
        self.backend.confirm_hit = False
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        while not driver.report.attack_submitted:
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)
        self.backend.dead = True
        self.backend.health = 0.0
        for _ in range(3):
            if driver.report.terminal:
                break
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        self.assertEqual(driver.report.outcome, MeleeStrikeOutcome.TARGET_DEAD)
        self.assertIs(
            driver.attempt_report.outcome,
            AttackAttemptOutcome.TARGET_DEAD_UNATTRIBUTED,
        )
        self.assertIs(
            driver.attempt_report.evidence_grade,
            AttackEvidenceGrade.TARGET_STATE_ONLY,
        )
        self.assertFalse(driver.report.hit_observed)

    def test_client_input_failure_is_not_a_gate_rejection(self):
        self.backend.reject_reason = "backend_rejected"
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        for _ in range(12):
            if driver.report.terminal:
                break
            driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        self.assertEqual(driver.report.outcome, MeleeStrikeOutcome.FAILED)
        self.assertIs(
            driver.attempt_report.outcome,
            AttackAttemptOutcome.INPUT_FAILED,
        )
        self.assertEqual(driver.attempt_report.receipt_status, "rejected")

    def test_attack_losing_arbitration_is_deferred_without_submission(self):
        self.backend.confirm_hit = False
        driver = self.driver()
        driver.start(self.target, self.clock[0])
        driver.tick(self.profile, self.clock[0] + 2_000_000_000)

        source = self.runtime.register_ordered_source("competing-operation")
        observation = self.runtime.observation
        intent = ActionIntentV1(
            ordered_intent_id(source, 1), source.source_id, source.episode_id,
            observation.sequence_id, ActionPriorityV0.PLAYER,
            self.clock[0], self.clock[0] + 250_000_000,
            operation=AttackEntityV1("entity-zombie-other"),
        )
        proposal = ControlFrameProposalV1((OrderedIntentV1(source, 1, intent),))
        driver.tick(
            self.profile, self.clock[0] + 2_000_000_000,
            additional_proposals=(proposal,),
        )

        self.assertIs(
            driver.attempt_report.outcome,
            AttackAttemptOutcome.DEFERRED_BY_ARBITRATION,
        )
        self.assertEqual(driver.report.attack_submissions, 0)


if __name__ == "__main__":
    unittest.main()

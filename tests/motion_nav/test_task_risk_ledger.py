"""Task risk authority from admission through delayed command settlement."""
from __future__ import annotations

import unittest

from mc2p.contracts.action_receipt import ClientInputApplicationV1
from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.motion_risk import (
    RiskCommitEvidence, RiskCommitKind, RiskReleaseEvidence,
    RiskReservationStatus, RiskSubmissionStatus,
    TaskDamageBudget, TaskRiskLedger,
)
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.world_model import WorldSessionId


class TaskRiskLedgerTests(unittest.TestCase):
    def test_two_late_candidates_cannot_share_one_two_point_budget(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("two", 2))
        first = ledger.reserve("drop-a", 2, policy_revision=0)
        self.assertIs(first.status, RiskReservationStatus.RESERVED)
        self.assertIs(ledger.reserve("drop-b", 2, policy_revision=0).status,
                      RiskReservationStatus.INSUFFICIENT)
        ledger.mark_submitted("drop-a", 7)
        self.assertFalse(ledger.release_unstarted(
            "drop-a", RiskReleaseEvidence.ARBITRATION_LOST,
        ))
        self.assertEqual(ledger.available_points, 0)
        ledger.commit("drop-a", RiskCommitEvidence(
            RiskCommitKind.APPLIED_COMMAND, control_sequence=7,
            movement_tick_id=10,
        ))
        self.assertEqual(ledger.committed_points, 2)
        self.assertEqual(ledger.available_points, 0)
        self.assertIs(ledger.reserve("drop-b", 2, policy_revision=0).status,
                      RiskReservationStatus.INSUFFICIENT)

    def test_arbitration_loser_releases_unsubmitted_hold(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("two", 2))
        ledger.reserve("drop-a", 2, policy_revision=0)
        self.assertTrue(ledger.release_unstarted(
            "drop-a", RiskReleaseEvidence.ARBITRATION_LOST,
        ))
        self.assertEqual(ledger.available_points, 2)
        self.assertIs(ledger.reserve("drop-b", 2, policy_revision=0).status,
                      RiskReservationStatus.RESERVED)
        self.assertIs(ledger.reserve("drop-a", 2, policy_revision=0).status,
                      RiskReservationStatus.RELEASED_REQUIRES_NEW_ACTION)

    def test_budget_reduction_keeps_old_commit_and_rejects_new_risk(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("two", 2))
        ledger.reserve("drop-a", 2, policy_revision=0)
        ledger.mark_submitted("drop-a", 5)
        ledger.commit("drop-a", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=10,
        ))
        ledger.update_policy(TaskDamageBudget("zero", 0), revision=1)
        self.assertEqual(ledger.available_points, 0)
        self.assertIs(ledger.reserve("drop-b", 1, policy_revision=1).status,
                      RiskReservationStatus.INSUFFICIENT)
        self.assertIs(ledger.reserve("drop-c", 1, policy_revision=0).status,
                      RiskReservationStatus.STALE_POLICY)
        self.assertFalse(ledger.settle("drop-a", observed_damage_points=2))
        self.assertEqual(ledger.authorized_commit("drop-a"), (0, 2))

    def test_duplicate_receipt_and_observed_damage_do_not_double_commit(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("two", 2))
        ledger.reserve("drop-a", 2, policy_revision=0)
        evidence = RiskCommitEvidence(
            RiskCommitKind.APPLIED_COMMAND, control_sequence=9,
            movement_tick_id=13,
        )
        ledger.mark_submitted("drop-a", 9)
        self.assertTrue(ledger.commit("drop-a", evidence))
        self.assertFalse(ledger.commit("drop-a", evidence))
        self.assertTrue(ledger.settle("drop-a", observed_damage_points=3))
        self.assertEqual(ledger.committed_points, 2)

    def test_fixed_capacity_refuses_new_reservation_without_forgetting_hold(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("many", 100))
        for index in range(64):
            self.assertIs(ledger.reserve(
                f"drop-{index}", 1, policy_revision=0,
            ).status, RiskReservationStatus.RESERVED)
        self.assertIs(ledger.reserve("overflow", 1, policy_revision=0).status,
                      RiskReservationStatus.CAPACITY_EXHAUSTED)
        self.assertEqual(ledger.available_points, 36)

    def test_unsubmitted_old_policy_reservation_cannot_start_after_cut(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("two", 2))
        ledger.reserve("drop-a", 2, policy_revision=0)
        ledger.update_policy(TaskDamageBudget("zero", 0), revision=1)
        self.assertIs(ledger.reserve("drop-a", 2, policy_revision=1).status,
                      RiskReservationStatus.STALE_POLICY)
        with self.assertRaises(ContractViolation):
            ledger.mark_submitted("drop-a", 1)
        self.assertIs(ledger.reserve("drop-b", 2, policy_revision=1).status,
                      RiskReservationStatus.INSUFFICIENT)

    def test_overrun_blocks_prepared_but_unsubmitted_action(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("four", 4))
        ledger.reserve("drop-a", 2, policy_revision=0)
        ledger.reserve("drop-b", 2, policy_revision=0)
        ledger.commit("drop-a", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=4,
        ))
        self.assertTrue(ledger.settle("drop-a", observed_damage_points=3))
        self.assertTrue(ledger.risk_overrun)
        self.assertIs(ledger.reserve("drop-b", 2, policy_revision=0).status,
                      RiskReservationStatus.RISK_OVERRUN)
        with self.assertRaises(ContractViolation):
            ledger.mark_submitted("drop-b", 5)
        self.assertIs(ledger.reserve("drop-c", 1, policy_revision=0).status,
                      RiskReservationStatus.RISK_OVERRUN)

    def test_superseded_without_sample_does_not_prove_nonapplication(self):
        applications = InputApplicationLedger()
        world = WorldSessionId("world")
        first = ActionSnapshotV1(
            "episode", 1, 0, 1_000_000_000,
            movement=MovementV1(forward=1), valid_for_ticks=1,
        )
        second = ActionSnapshotV1(
            "episode", 2, 0, 1_000_000_000,
            movement=MovementV1(), valid_for_ticks=1,
        )
        applications.submit(world, first, requested_first_tick=4)
        applications.submit(world, second, requested_first_tick=6)
        applications.observe_sample(ClientInputApplicationV1(
            "mc2p.input-application.v1", 6, "episode", 2, 0,
            "neutral", 0.0, 0.0, False, False, False,
        ))
        ledger = TaskRiskLedger("task", TaskDamageBudget("two", 2))
        ledger.reserve("drop", 2, policy_revision=0)
        ledger.mark_submitted("drop", 1)
        self.assertFalse(ledger.release_unstarted(
            "drop", RiskReleaseEvidence.CONFIRMED_NOT_APPLIED,
            input_ledger=applications,
        ))
        self.assertEqual(ledger.available_points, 0)

    def test_neutral_selected_input_can_commit_risk_with_existing_velocity(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("two", 2))
        ledger.reserve("drop", 2, policy_revision=0)
        # A neutral sample does not brake an already moving player instantly.
        ledger.mark_submitted("drop", 12)
        self.assertTrue(ledger.commit("drop", RiskCommitEvidence(
            RiskCommitKind.APPLIED_COMMAND, control_sequence=12,
            movement_tick_id=16,
        )))
        self.assertEqual(ledger.available_points, 0)

    def test_submission_capacity_keeps_risk_held_without_runtime_exception(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("two", 2))
        ledger.reserve("drop", 2, policy_revision=0)
        for sequence in range(64):
            self.assertIs(ledger.mark_submitted("drop", sequence),
                          RiskSubmissionStatus.SUBMITTED)
        self.assertIs(ledger.mark_submitted("drop", 64),
                      RiskSubmissionStatus.CAPACITY_EXHAUSTED)
        self.assertEqual(len(ledger.action("drop").submitted_sequences), 64)
        self.assertEqual(ledger.available_points, 0)
        self.assertFalse(ledger.release_unstarted(
            "drop", RiskReleaseEvidence.ARBITRATION_LOST,
        ))

    def test_healing_between_losses_does_not_hide_overrun(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("four", 4))
        ledger.reserve("drop", 2, policy_revision=0)
        ledger.observe_health("drop", observation_sequence=1,
                              health_points=17, on_ground=True)
        ledger.commit("drop", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=2,
        ))
        for sequence, health, ground in (
                (2, 18, False), (3, 15, False), (4, 15, True)):
            ledger.observe_health("drop", observation_sequence=sequence,
                                  health_points=health, on_ground=ground)
        self.assertEqual(ledger.action("drop").observed_damage_points, 3)
        self.assertTrue(ledger.risk_overrun)
        self.assertIs(ledger.reserve("next", 1, policy_revision=0).status,
                      RiskReservationStatus.RISK_OVERRUN)

    def test_missing_sample_does_not_hide_confirmed_loss_lower_bound(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("four", 4))
        ledger.reserve("drop", 2, policy_revision=0)
        ledger.observe_health("drop", observation_sequence=1,
                              health_points=20, on_ground=True)
        ledger.commit("drop", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=2,
        ))
        for sequence, health, ground in (
                (2, None, False), (3, 20, False),
                (4, 17, False), (5, 17, True)):
            ledger.observe_health("drop", observation_sequence=sequence,
                                  health_points=health, on_ground=ground)
        record = ledger.action("drop")
        self.assertIsNone(record.observed_damage_points)
        self.assertFalse(record.health_evidence_complete)
        self.assertEqual(record.observed_damage_lower_bound_points, 3)
        self.assertTrue(ledger.risk_overrun)
        self.assertIs(ledger.reserve("next", 1, policy_revision=0).status,
                      RiskReservationStatus.RISK_OVERRUN)

    def test_net_drop_across_missing_health_sample_is_still_lower_bound(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("four", 4))
        ledger.reserve("drop", 2, policy_revision=0)
        ledger.observe_health("drop", observation_sequence=1,
                              health_points=20, on_ground=True)
        ledger.commit("drop", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=2,
        ))
        ledger.observe_health("drop", observation_sequence=2,
                              health_points=None, on_ground=False)
        ledger.observe_health("drop", observation_sequence=3,
                              health_points=17, on_ground=True)
        action = ledger.action("drop")
        self.assertEqual(action.observed_damage_lower_bound_points, 3)
        self.assertIsNone(action.observed_damage_points)
        self.assertFalse(action.health_evidence_complete)
        self.assertTrue(ledger.risk_overrun)

    def test_missing_health_first_drop_does_not_charge_second_drop_to_it(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("three", 3))
        ledger.reserve("first", 0, policy_revision=0)
        ledger.observe_health("first", observation_sequence=1,
                              health_points=20, on_ground=True)
        ledger.commit("first", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=2,
        ))
        ledger.observe_health("first", observation_sequence=2,
                              health_points=None, on_ground=False)
        ledger.observe_health("first", observation_sequence=3,
                              health_points=20, on_ground=True)
        self.assertFalse(ledger.action("first").health_evidence_complete)
        ledger.reserve("second", 3, policy_revision=0)
        ledger.observe_health("second", observation_sequence=3,
                              health_points=20, on_ground=True)
        ledger.commit("second", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE, observation_sequence=4,
        ))
        ledger.observe_health("first", observation_sequence=4,
                              health_points=17, on_ground=False)
        ledger.observe_health("second", observation_sequence=4,
                              health_points=17, on_ground=False)
        ledger.observe_health("first", observation_sequence=5,
                              health_points=17, on_ground=True)
        ledger.observe_health("second", observation_sequence=5,
                              health_points=17, on_ground=True)
        self.assertEqual(
            ledger.action("first").observed_damage_lower_bound_points, 0,
        )
        self.assertIsNone(ledger.action("first").observed_damage_points)
        self.assertEqual(ledger.action("second").observed_damage_points, 3)
        self.assertFalse(ledger.risk_overrun)

    def test_grounded_cancel_closes_committed_health_window(self):
        ledger = TaskRiskLedger("task", TaskDamageBudget("one", 1))
        ledger.reserve("cancelled", 0, policy_revision=0)
        ledger.observe_health("cancelled", observation_sequence=1,
                              health_points=20, on_ground=True)
        ledger.mark_submitted("cancelled", 1)
        ledger.commit("cancelled", RiskCommitEvidence(
            RiskCommitKind.APPLIED_COMMAND, control_sequence=1,
            movement_tick_id=1,
        ))
        self.assertTrue(ledger.close_health_window(
            "cancelled", on_ground=True,
        ))
        ledger.observe_health("cancelled", observation_sequence=2,
                              health_points=17, on_ground=True)
        self.assertEqual(ledger.action("cancelled").observed_damage_points, 0)
        self.assertFalse(ledger.risk_overrun)


if __name__ == "__main__":
    unittest.main()

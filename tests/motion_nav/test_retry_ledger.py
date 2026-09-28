"""Task retry counts and evidence-backed progress boundaries."""
from __future__ import annotations

import unittest

from mc2p.motion_nav.retry_ledger import (
    ProgressEvidence, ProgressKind, RetryCause, RetryLedger,
    RetryLedgerCapacityExceeded, RetryVerdict,
    WaitPolicy, WaitVerdict,
)


class RetryLedgerTests(unittest.TestCase):
    def test_repeated_failure_id_and_alternating_causes_do_not_create_retries(self):
        ledger = RetryLedger("task")
        causes = tuple(RetryCause)
        for index in range(6):
            decision = ledger.record_failure(f"attempt-{index}", causes[index % 5])
            self.assertIs(decision.verdict, RetryVerdict.RETRY)
            self.assertTrue(decision.first_seen)
            repeated = ledger.record_failure(f"attempt-{index}", causes[index % 5])
            self.assertIs(repeated.verdict, RetryVerdict.RETRY)
            self.assertFalse(repeated.first_seen)
        self.assertIs(ledger.record_failure(
            "attempt-6", RetryCause.DEPENDENCY).verdict,
            RetryVerdict.ROUND_EXHAUSTED)
        self.assertEqual(ledger.total_failures, 7)

    def test_same_cause_third_failure_is_exhausted(self):
        ledger = RetryLedger("task")
        self.assertIs(ledger.record_failure("a", RetryCause.EXECUTION).verdict,
                      RetryVerdict.RETRY)
        self.assertIs(ledger.record_failure("b", RetryCause.EXECUTION).verdict,
                      RetryVerdict.RETRY)
        self.assertIs(ledger.record_failure("c", RetryCause.EXECUTION).verdict,
                      RetryVerdict.CAUSE_EXHAUSTED)
        self.assertIs(ledger.record_failure("c", RetryCause.EXECUTION).verdict,
                      RetryVerdict.CAUSE_EXHAUSTED)
        self.assertIs(ledger.record_failure("d", RetryCause.DEPENDENCY).verdict,
                      RetryVerdict.CAUSE_EXHAUSTED)
        self.assertEqual(ledger.total_failures, 3)

    def test_backtracking_and_route_revision_cannot_reset_round(self):
        ledger = RetryLedger("task", initial_support=(0, 64, 0))
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, 2, support=(0, 64, 0),
            valid_corridor=((0, 64, 0), (1, 64, 0)),
        )))
        self.assertTrue(ledger.record_progress(ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, 3, support=(1, 64, 0),
            valid_corridor=((0, 64, 0), (1, 64, 0)),
        )))
        ledger.record_failure("a", RetryCause.EXECUTION)
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, 4, support=(0, 64, 0),
            valid_corridor=((0, 64, 0), (1, 64, 0)),
        )))
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, 5, support=(1, 64, 0),
            valid_corridor=((0, 64, 0), (1, 64, 0)),
        )))
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, 6, support=(9, 64, 0),
            valid_corridor=((0, 64, 0), (1, 64, 0)),
        )))
        self.assertEqual(ledger.round_failures, 1)

    def test_multiple_new_facts_in_one_observation_clear_only_once(self):
        ledger = RetryLedger("task")
        ledger.set_blockers(("a", "b"))
        self.assertTrue(ledger.record_progress(ProgressEvidence(
            ProgressKind.BLOCKING_FACT, 3, fact_id="a",
        )))
        ledger.record_failure("failure", RetryCause.INFORMATION)
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.BLOCKING_FACT, 3, fact_id="b",
        )))
        self.assertEqual(ledger.round_failures, 1)
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.BLOCKING_FACT, 4, fact_id="b",
        )))

    def test_new_fact_must_answer_current_blocker_and_task_total_never_resets(self):
        ledger = RetryLedger("task")
        for index in range(6):
            blocker = f"landing-lower-face-{index}"
            ledger.set_blockers((blocker,))
            self.assertFalse(ledger.record_progress(ProgressEvidence(
                ProgressKind.BLOCKING_FACT, index * 4 + 1,
                fact_id=f"unrelated-{index}",
            )))
            self.assertTrue(ledger.record_progress(ProgressEvidence(
                ProgressKind.BLOCKING_FACT, index * 4 + 2,
                fact_id=blocker,
            )))
            for offset in range(2):
                self.assertIs(ledger.record_failure(
                    f"{index}-{offset}", RetryCause.EXECUTION).verdict,
                    RetryVerdict.RETRY,
                )
        self.assertEqual(ledger.total_failures, 12)
        self.assertIs(ledger.record_failure("5-1", RetryCause.EXECUTION).verdict,
                      RetryVerdict.RETRY)
        self.assertIs(ledger.record_failure("extra", RetryCause.DEPENDENCY).verdict,
                      RetryVerdict.TASK_EXHAUSTED)
        self.assertEqual(ledger.total_failures, 13)
        stale = ledger.record_failure("0-0", RetryCause.EXECUTION)
        self.assertIs(stale.verdict, RetryVerdict.RETRY)
        self.assertFalse(stale.first_seen)

    def test_progress_capacity_refuses_new_reset_without_evicting_history(self):
        ledger = RetryLedger("task")
        for index in range(4096):
            self.assertTrue(ledger.record_progress(ProgressEvidence(
                ProgressKind.ACTION_COMPLETED, index,
                action_id=f"action-{index}",
            )))
        ledger.record_failure("attempt", RetryCause.EXECUTION)
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.ACTION_COMPLETED, 4096,
            action_id="action-over-capacity",
        )))
        self.assertTrue(ledger.progress_capacity_exhausted)
        self.assertEqual(ledger.round_failures, 1)
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.ACTION_COMPLETED, 4097,
            action_id="action-0",
        )))

    def test_wait_capacity_keeps_existing_deadlines(self):
        ledger = RetryLedger("task")
        policy = WaitPolicy(40, 2_000_000_000)
        for index in range(8):
            ledger.begin_wait(f"wait-{index}", policy, 0, 0)
        with self.assertRaises(RetryLedgerCapacityExceeded):
            ledger.begin_wait("overflow", policy, 20, 1_000_000_000)
        self.assertIs(ledger.check_wait("wait-0", 40, 1_000_000_000),
                      WaitVerdict.EXHAUSTED_TICKS)

    def test_blocker_capacity_rejects_oversize_without_replacing_existing(self):
        ledger = RetryLedger("task")
        ledger.set_blockers(("current-cell",))
        with self.assertRaises(RetryLedgerCapacityExceeded):
            ledger.set_blockers(tuple(f"cell-{index}" for index in range(129)))
        self.assertTrue(ledger.record_progress(ProgressEvidence(
            ProgressKind.BLOCKING_FACT, 1, fact_id="current-cell",
        )))

    def test_wait_uses_original_movement_tick_and_wall_clock(self):
        ledger = RetryLedger("task")
        policy = WaitPolicy(maximum_movement_ticks=40,
                            maximum_elapsed_ns=2_000_000_000)
        ledger.begin_wait("information", policy, 10, 1_000_000_000)
        ledger.begin_wait("information", policy, 30, 2_000_000_000)
        self.assertIs(ledger.check_wait("information", 49, 2_900_000_000),
                      WaitVerdict.WAITING)
        self.assertIs(ledger.check_wait("information", 50, 2_900_000_000),
                      WaitVerdict.EXHAUSTED_TICKS)
        self.assertIs(ledger.check_wait("information", 49, 3_000_000_000),
                      WaitVerdict.EXHAUSTED_CLOCK)


if __name__ == "__main__":
    unittest.main()

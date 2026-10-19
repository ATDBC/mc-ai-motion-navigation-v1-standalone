"""Task retry counts and evidence-backed progress boundaries."""
from __future__ import annotations

import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.retry_ledger import (
    LocalAttemptChain, LocalAttemptVerdict,
    ProgressEvidence, ProgressKind, RecoveryBudgetPolicy,
    RecoveryFinishEvidence, RecoveryFinishKind, RecoveryIdentity,
    RecoveryLimitStatus,
    RetryCause, RetryLedger, RetryLedgerCapacityExceeded,
    TaskDemandState,
    WaitPolicy, WaitVerdict,
)


class FakeTaskClock:
    def __init__(self) -> None:
        self.now_ns = 0

    def __call__(self) -> int:
        return self.now_ns

    def advance_seconds(self, seconds: float) -> None:
        self.now_ns += int(seconds * 1_000_000_000)


class RetryLedgerTests(unittest.TestCase):
    def test_local_attempt_chain_is_bounded_idempotent_and_reason_agnostic(self):
        chain = LocalAttemptChain(maximum_failures=3)

        first = chain.record("candidate-1")
        repeated = chain.record("candidate-1")
        second = chain.record("worker-2")
        exhausted = chain.record("entry-3")

        self.assertIs(first.verdict, LocalAttemptVerdict.RETRY)
        self.assertTrue(first.first_seen)
        self.assertEqual(repeated, type(first)(LocalAttemptVerdict.RETRY, False))
        self.assertIs(second.verdict, LocalAttemptVerdict.RETRY)
        self.assertIs(exhausted.verdict, LocalAttemptVerdict.EXHAUSTED)
        self.assertEqual(chain.failure_count, 3)
        self.assertEqual(
            chain.record("fourth"),
            type(first)(LocalAttemptVerdict.EXHAUSTED, False),
        )

        chain.reset()
        self.assertIs(chain.record("new-action").verdict,
                      LocalAttemptVerdict.RETRY)

    def test_recovery_policy_cannot_exceed_frozen_production_maximum(self):
        with self.assertRaisesRegex(ContractViolation, "frozen task maximum"):
            RecoveryBudgetPolicy.finite(maximum_recoveries=13)
        with self.assertRaisesRegex(ContractViolation, "frozen task maximum"):
            RecoveryBudgetPolicy.persistent(maximum_recoveries=13)

    def test_finite_recovery_budget_allows_twelve_and_rejects_thirteenth(self):
        clock = FakeTaskClock()
        ledger = RetryLedger("task", policy=RecoveryBudgetPolicy.finite(), clock_ns=clock)
        for index in range(12):
            identity = RecoveryIdentity(index, f"recovery-{index}")
            started = ledger.begin_recovery(identity, RetryCause.EXECUTION)
            self.assertIs(started.status, RecoveryLimitStatus.ALLOWED)
            self.assertTrue(started.first_seen)
            ledger.finish_recovery(
                identity,
                RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
            )
        exhausted = ledger.begin_recovery(
            RecoveryIdentity(12, "recovery-12"), RetryCause.PLANNING,
        )
        self.assertIs(exhausted.status, RecoveryLimitStatus.FINITE_TOTAL_EXHAUSTED)
        self.assertTrue(exhausted.first_seen)

    def test_persistent_recovery_window_is_open_on_the_left(self):
        clock = FakeTaskClock()
        policy = RecoveryBudgetPolicy.persistent(
            maximum_recoveries=12, recovery_window_ns=60_000_000_000,
            maximum_recovery_ns=10_000_000_000,
            maximum_no_progress_ns=30_000_000_000,
        )
        ledger = RetryLedger("task", policy=policy, clock_ns=clock)
        for index in range(12):
            identity = RecoveryIdentity(index, f"recovery-{index}")
            started = ledger.begin_recovery(identity, RetryCause.EXECUTION)
            self.assertIs(started.status, RecoveryLimitStatus.ALLOWED)
            ledger.finish_recovery(
                identity,
                RecoveryFinishEvidence(RecoveryFinishKind.BODY_HANDOFF),
            )
            if index == 0:
                clock.advance_seconds(1)
        self.assertIs(
            ledger.begin_recovery(
                RecoveryIdentity(12, "recovery-12"), RetryCause.EXECUTION,
            ).status,
            RecoveryLimitStatus.PERSISTENT_RATE_EXHAUSTED,
        )

        clock = FakeTaskClock()
        ledger = RetryLedger("task-2", policy=policy, clock_ns=clock)
        first_identity = RecoveryIdentity(0, "first")
        first = ledger.begin_recovery(first_identity, RetryCause.EXECUTION)
        self.assertIs(first.status, RecoveryLimitStatus.ALLOWED)
        ledger.finish_recovery(
            first_identity, RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
        )
        for index in range(11):
            clock.advance_seconds(1)
            self.assertIs(
                ledger.begin_recovery(
                    RecoveryIdentity(index + 1, f"later-{index}"), RetryCause.EXECUTION,
                ).status,
                RecoveryLimitStatus.ALLOWED,
            )
            ledger.finish_recovery(
                RecoveryIdentity(index + 1, f"later-{index}"),
                RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
            )
        clock.now_ns = 60_000_000_000
        self.assertIs(
            ledger.begin_recovery(
                RecoveryIdentity(12, "boundary"), RetryCause.EXECUTION,
            ).status,
            RecoveryLimitStatus.ALLOWED,
        )

    def test_duplicate_recovery_identity_and_diagnostics_charge_once(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "task", policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=1, recovery_window_ns=60_000_000_000,
                maximum_recovery_ns=10_000_000_000,
                maximum_no_progress_ns=30_000_000_000,
            ), clock_ns=clock,
        )
        identity = RecoveryIdentity(4, "same")
        first = ledger.begin_recovery(identity, RetryCause.PLANNING)
        repeated = ledger.begin_recovery(
            RecoveryIdentity(4, "different-diagnostic-label"), RetryCause.EXECUTION,
        )
        ledger.record_recovery_cause(identity, RetryCause.DEPENDENCY)
        ledger.record_recovery_cause(identity, RetryCause.PLANNING)
        self.assertTrue(first.first_seen)
        self.assertFalse(repeated.first_seen)
        self.assertIs(repeated.status, RecoveryLimitStatus.ALLOWED)
        self.assertEqual(ledger.recovery_starts_in_window, 1)
        self.assertEqual(ledger.total_recovery_starts, 1)
        self.assertEqual(dict(ledger.recovery_cause_counts)[RetryCause.PLANNING], 2)
        self.assertEqual(dict(ledger.recovery_cause_counts)[RetryCause.EXECUTION], 0)
        self.assertEqual(dict(ledger.recovery_cause_counts)[RetryCause.DEPENDENCY], 1)

    def test_retired_recovery_identity_never_charges_again_after_window(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "task", policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=2, recovery_window_ns=10,
                maximum_recovery_ns=100,
                maximum_no_progress_ns=100,
            ), clock_ns=clock,
        )
        identity = RecoveryIdentity(0, "original")
        self.assertTrue(ledger.begin_recovery(identity, RetryCause.EXECUTION).first_seen)
        ledger.finish_recovery(
            identity, RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
        )
        clock.now_ns = 11
        self.assertEqual(ledger.recovery_starts_in_window, 0)
        replay = ledger.begin_recovery(
            RecoveryIdentity(0, "late-replay"), RetryCause.PLANNING,
        )
        self.assertFalse(replay.first_seen)
        self.assertIs(replay.status, RecoveryLimitStatus.ALLOWED)
        self.assertEqual(ledger.total_recovery_starts, 1)
        next_identity = RecoveryIdentity(2, "next-with-gap")
        self.assertIs(
            ledger.begin_recovery(next_identity, RetryCause.EXECUTION).status,
            RecoveryLimitStatus.ALLOWED,
        )
        ledger.finish_recovery(
            next_identity, RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
        )
        self.assertEqual(ledger.total_recovery_starts, 2)
        skipped = ledger.begin_recovery(
            RecoveryIdentity(1, "late-gap"), RetryCause.PLANNING,
        )
        self.assertFalse(skipped.first_seen)
        self.assertIs(skipped.status, RecoveryLimitStatus.ALLOWED)
        self.assertEqual(ledger.total_recovery_starts, 2)

    def test_terminal_limit_does_not_remember_unbounded_new_identities(self):
        ledger = RetryLedger(
            "task", policy=RecoveryBudgetPolicy.finite(maximum_recoveries=1),
        )
        first = RecoveryIdentity(0, "first")
        ledger.begin_recovery(first, RetryCause.EXECUTION)
        ledger.finish_recovery(
            first, RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
        )
        self.assertIs(
            ledger.begin_recovery(
                RecoveryIdentity(1, "exhausted"), RetryCause.EXECUTION,
            ).status,
            RecoveryLimitStatus.FINITE_TOTAL_EXHAUSTED,
        )
        remembered = ledger.remembered_recovery_result_count
        for sequence in range(2, 10_002):
            result = ledger.begin_recovery(
                RecoveryIdentity(sequence, f"late-{sequence}"), RetryCause.PLANNING,
            )
            self.assertIs(result.status, RecoveryLimitStatus.FINITE_TOTAL_EXHAUSTED)
            self.assertFalse(result.first_seen)
        self.assertEqual(ledger.remembered_recovery_result_count, remembered)

    def test_terminal_cleanup_stops_new_actions_but_keeps_recovery_deadline(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "task", policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12, recovery_window_ns=100,
                maximum_recovery_ns=2,
                maximum_no_progress_ns=3,
            ), clock_ns=clock,
        )
        active = RecoveryIdentity(0, "landing-tail")
        ledger.begin_recovery(active, RetryCause.EXECUTION)
        ledger.begin_wait("landing", "motion-owner", WaitPolicy(40, 100), 0, 0)
        self.assertIs(
            ledger.observe_task_activity_status(TaskDemandState.TERMINAL_CLEANUP),
            RecoveryLimitStatus.ALLOWED,
        )
        blocked = ledger.begin_recovery(
            RecoveryIdentity(1, "new-target-action"), RetryCause.PLANNING,
        )
        self.assertIs(blocked.status, RecoveryLimitStatus.TARGET_ACTIONS_STOPPED)
        self.assertFalse(blocked.first_seen)
        self.assertEqual(ledger.total_recovery_starts, 1)
        self.assertEqual(len(ledger.active_waits("motion-owner")), 1)
        clock.now_ns = 2
        self.assertIs(
            ledger.observe_task_activity_status(TaskDemandState.TERMINAL_CLEANUP),
            RecoveryLimitStatus.SINGLE_RECOVERY_EXHAUSTED,
        )
        with self.assertRaisesRegex(ContractViolation, "immutable"):
            ledger.observe_task_activity_status(TaskDemandState.UNMET)

    def test_same_task_continuation_preserves_unmet_elapsed_across_cleanup(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "persistent-continuation",
            policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12,
                recovery_window_ns=60_000_000_000,
                maximum_recovery_ns=10_000_000_000,
                maximum_no_progress_ns=30_000_000_000,
            ),
            clock_ns=clock,
        )
        clock.advance_seconds(29)
        self.assertIs(
            ledger.observe_task_activity_status(TaskDemandState.TERMINAL_CLEANUP),
            RecoveryLimitStatus.ALLOWED,
        )
        self.assertIsNone(ledger.last_true_progress_ns)
        clock.advance_seconds(60)

        self.assertTrue(ledger.can_resume_same_task_continuation())
        self.assertTrue(ledger.resume_same_task_continuation())
        self.assertEqual(
            clock.now_ns - ledger.last_true_progress_ns,
            29_000_000_000,
        )
        clock.advance_seconds(1)
        self.assertIs(
            ledger.current_limit_status(),
            RecoveryLimitStatus.NO_PROGRESS_EXHAUSTED,
        )

    def test_same_task_continuation_rechecks_active_recovery_deadline(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "persistent-active-recovery",
            policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12,
                recovery_window_ns=60_000_000_000,
                maximum_recovery_ns=10_000_000_000,
                maximum_no_progress_ns=30_000_000_000,
            ),
            clock_ns=clock,
        )
        active = RecoveryIdentity(0, "active-before-cleanup")
        ledger.begin_recovery(active, RetryCause.EXECUTION)
        ledger.observe_task_activity_status(TaskDemandState.TERMINAL_CLEANUP)
        clock.advance_seconds(11)

        self.assertFalse(ledger.can_resume_same_task_continuation())
        self.assertIs(
            ledger.recovery_limit_status,
            RecoveryLimitStatus.SINGLE_RECOVERY_EXHAUSTED,
        )
        self.assertFalse(ledger.resume_same_task_continuation())

    def test_progress_does_not_finish_or_refund_recovery_budget(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "task", policy=RecoveryBudgetPolicy.finite(maximum_recoveries=1),
            clock_ns=clock,
        )
        active_identity = RecoveryIdentity(0, "active")
        ledger.begin_recovery(active_identity, RetryCause.EXECUTION)
        self.assertTrue(ledger.observe_task_activity(
            TaskDemandState.UNMET,
            ProgressEvidence(ProgressKind.ACTION_COMPLETED, 1, action_id="step-a"),
        ))
        self.assertEqual(ledger.active_recovery_id, active_identity)
        ledger.finish_recovery(
            active_identity, RecoveryFinishEvidence(RecoveryFinishKind.BODY_HANDOFF),
        )
        self.assertIs(
            ledger.begin_recovery(
                RecoveryIdentity(1, "next"), RetryCause.EXECUTION,
            ).status,
            RecoveryLimitStatus.FINITE_TOTAL_EXHAUSTED,
        )

        clock = FakeTaskClock()
        persistent = RetryLedger(
            "persistent", policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=1, recovery_window_ns=60_000_000_000,
                maximum_recovery_ns=10_000_000_000,
                maximum_no_progress_ns=30_000_000_000,
            ), clock_ns=clock,
        )
        first_identity = RecoveryIdentity(0, "first")
        persistent.begin_recovery(first_identity, RetryCause.EXECUTION)
        persistent.finish_recovery(
            first_identity, RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
        )
        self.assertTrue(persistent.observe_task_activity(
            TaskDemandState.UNMET,
            ProgressEvidence(ProgressKind.ACTION_COMPLETED, 1,
                             action_id="new-progress"),
        ))
        self.assertIs(
            persistent.begin_recovery(
                RecoveryIdentity(1, "second"), RetryCause.EXECUTION,
            ).status,
            RecoveryLimitStatus.PERSISTENT_RATE_EXHAUSTED,
        )

    def test_finish_requires_typed_evidence_and_matching_identity(self):
        ledger = RetryLedger("task", policy=RecoveryBudgetPolicy.finite())
        active_identity = RecoveryIdentity(0, "active")
        ledger.begin_recovery(active_identity, RetryCause.EXECUTION)
        with self.assertRaisesRegex(ContractViolation, "finish evidence"):
            ledger.finish_recovery(active_identity, "safe")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ContractViolation, "identity"):
            ledger.finish_recovery(
                RecoveryIdentity(1, "other"),
                RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
            )
        self.assertEqual(ledger.active_recovery_id, active_identity)

    def test_finish_after_deadline_preserves_typed_exhaustion(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "late-safe-release",
            policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12,
                recovery_window_ns=60_000_000_000,
                maximum_recovery_ns=10_000_000_000,
                maximum_no_progress_ns=30_000_000_000,
            ),
            clock_ns=clock,
        )
        identity = RecoveryIdentity(0, "first-quiescent-at-eleven-seconds")
        ledger.begin_recovery(identity, RetryCause.EXECUTION)
        clock.advance_seconds(11)

        ledger.finish_recovery(
            identity,
            RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
        )

        self.assertIsNone(ledger.active_recovery_id)
        self.assertIs(
            ledger.recovery_limit_status,
            RecoveryLimitStatus.SINGLE_RECOVERY_EXHAUSTED,
        )

    def test_rate_exhaustion_keeps_decision_window_count_after_window_slides(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "rate-window-evidence",
            policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=1,
                recovery_window_ns=60_000_000_000,
                maximum_recovery_ns=10_000_000_000,
                maximum_no_progress_ns=30_000_000_000,
            ),
            clock_ns=clock,
        )
        first = RecoveryIdentity(0, "first")
        ledger.begin_recovery(first, RetryCause.EXECUTION)
        ledger.finish_recovery(
            first, RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
        )
        self.assertIs(
            ledger.begin_recovery(
                RecoveryIdentity(1, "exhausted"), RetryCause.EXECUTION,
            ).status,
            RecoveryLimitStatus.PERSISTENT_RATE_EXHAUSTED,
        )
        clock.advance_seconds(61)

        self.assertEqual(ledger.recovery_starts_in_window, 0)
        self.assertEqual(ledger.recovery_starts_at_limit_decision, 1)

    def test_persistent_deadlines_and_finite_policy_scope(self):
        clock = FakeTaskClock()
        short = RecoveryBudgetPolicy.persistent(
            maximum_recoveries=12, recovery_window_ns=100_000_000_000,
            maximum_recovery_ns=2_000_000_000,
            maximum_no_progress_ns=3_000_000_000,
        )
        recovery = RetryLedger("recovery", policy=short, clock_ns=clock)
        recovery.begin_recovery(RecoveryIdentity(0, "active"), RetryCause.EXECUTION)
        clock.advance_seconds(2)
        self.assertIs(
            recovery.observe_task_activity_status(TaskDemandState.UNMET),
            RecoveryLimitStatus.SINGLE_RECOVERY_EXHAUSTED,
        )

        clock = FakeTaskClock()
        no_progress = RetryLedger("no-progress", policy=short, clock_ns=clock)
        clock.advance_seconds(3)
        self.assertIs(
            no_progress.observe_task_activity_status(TaskDemandState.UNMET),
            RecoveryLimitStatus.NO_PROGRESS_EXHAUSTED,
        )

        clock = FakeTaskClock()
        finite = RetryLedger("finite", policy=RecoveryBudgetPolicy.finite(), clock_ns=clock)
        finite.begin_recovery(RecoveryIdentity(0, "active"), RetryCause.EXECUTION)
        clock.advance_seconds(1_000)
        self.assertIs(
            finite.observe_task_activity_status(TaskDemandState.UNMET),
            RecoveryLimitStatus.ALLOWED,
        )

    def test_same_frame_progress_precedes_no_progress_deadline(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "task", policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12, recovery_window_ns=60_000_000_000,
                maximum_recovery_ns=10_000_000_000,
                maximum_no_progress_ns=3_000_000_000,
            ), clock_ns=clock,
        )
        clock.advance_seconds(3)
        status = ledger.observe_task_activity_status(
            TaskDemandState.UNMET,
            ProgressEvidence(ProgressKind.ACTION_COMPLETED, 1, action_id="step"),
        )
        self.assertIs(status, RecoveryLimitStatus.ALLOWED)

    def test_stable_satisfied_pauses_and_new_unmet_interval_reuses_support(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "task", policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12, recovery_window_ns=60_000_000_000,
                maximum_recovery_ns=10_000_000_000,
                maximum_no_progress_ns=3_000_000_000,
            ), clock_ns=clock,
        )
        support = ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, 1, support=(1, 64, 0),
            valid_corridor=((0, 64, 0), (1, 64, 0)),
        )
        self.assertTrue(ledger.observe_task_activity(TaskDemandState.UNMET, support))
        self.assertIs(
            ledger.observe_task_activity_status(TaskDemandState.STABLE_SATISFIED),
            RecoveryLimitStatus.ALLOWED,
        )
        clock.advance_seconds(100)
        self.assertIs(
            ledger.observe_task_activity_status(TaskDemandState.STABLE_SATISFIED),
            RecoveryLimitStatus.ALLOWED,
        )
        self.assertIs(
            ledger.observe_task_activity_status(TaskDemandState.UNMET),
            RecoveryLimitStatus.ALLOWED,
        )
        reused = ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, 2, support=(1, 64, 0),
            valid_corridor=((0, 64, 0), (1, 64, 0)),
        )
        self.assertTrue(ledger.observe_task_activity(TaskDemandState.UNMET, reused))

    def test_persistent_progress_is_bounded_but_accepts_more_than_4096_supports(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "task", policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12, recovery_window_ns=60_000_000_000,
                maximum_recovery_ns=10_000_000_000,
                maximum_no_progress_ns=30_000_000_000,
            ), clock_ns=clock,
        )
        for index in range(5000):
            evidence = ProgressEvidence(
                ProgressKind.ROUTE_FRONTIER, index,
                support=(index, 64, 0), valid_corridor=((index, 64, 0),),
            )
            self.assertTrue(ledger.observe_task_activity(TaskDemandState.UNMET, evidence))
        self.assertLessEqual(ledger.recent_progress_identity_count, 8192)
        self.assertFalse(ledger.progress_capacity_exhausted)

        for index in range(5000, 8192):
            evidence = ProgressEvidence(
                ProgressKind.ROUTE_FRONTIER, index,
                support=(index, 64, 0), valid_corridor=((index, 64, 0),),
            )
            self.assertTrue(ledger.observe_task_activity(TaskDemandState.UNMET, evidence))
        with self.assertRaises(RetryLedgerCapacityExceeded):
            ledger.observe_task_activity(
                TaskDemandState.UNMET,
                ProgressEvidence(
                    ProgressKind.ROUTE_FRONTIER, 8192,
                    support=(8192, 64, 0), valid_corridor=((8192, 64, 0),),
                ),
            )

    def test_two_point_oscillation_cannot_refresh_no_progress(self):
        clock = FakeTaskClock()
        ledger = RetryLedger(
            "task", initial_support=(0, 64, 0),
            policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12, recovery_window_ns=60_000_000_000,
                maximum_recovery_ns=10_000_000_000,
                maximum_no_progress_ns=3_000_000_000,
            ), clock_ns=clock,
        )
        corridor = ((0, 64, 0), (1, 64, 0))
        self.assertTrue(ledger.observe_task_activity(
            TaskDemandState.UNMET,
            ProgressEvidence(ProgressKind.ROUTE_FRONTIER, 1,
                             support=(1, 64, 0), valid_corridor=corridor),
        ))
        for sequence, support in ((2, (0, 64, 0)), (3, (1, 64, 0))):
            clock.advance_seconds(1)
            self.assertFalse(ledger.observe_task_activity(
                TaskDemandState.UNMET,
                ProgressEvidence(ProgressKind.ROUTE_FRONTIER, sequence,
                                 support=support, valid_corridor=corridor),
            ))
        clock.advance_seconds(1)
        self.assertIs(
            ledger.observe_task_activity_status(TaskDemandState.UNMET),
            RecoveryLimitStatus.NO_PROGRESS_EXHAUSTED,
        )

    def test_owner_checked_wait_end_rejects_another_owner(self):
        ledger = RetryLedger("task")
        policy = WaitPolicy(40, 2_000_000_000)
        ledger.begin_wait("probe", "owner-a", policy, 0, 0)
        with self.assertRaisesRegex(ContractViolation, "owner"):
            ledger.end_wait_owned("probe", "owner-b")
        self.assertTrue(ledger.end_wait_owned("probe", "owner-a"))
        self.assertFalse(ledger.end_wait_owned("probe", "owner-a"))

    def test_task_clock_rollback_is_a_contract_error(self):
        clock = FakeTaskClock()
        ledger = RetryLedger("task", policy=RecoveryBudgetPolicy.persistent(), clock_ns=clock)
        clock.advance_seconds(1)
        ledger.observe_task_activity_status(TaskDemandState.UNMET)
        clock.now_ns = 0
        with self.assertRaisesRegex(ContractViolation, "backwards"):
            ledger.observe_task_activity_status(TaskDemandState.UNMET)

    def test_waits_can_be_closed_by_the_action_that_owns_them(self):
        ledger = RetryLedger("task")
        policy = WaitPolicy(40, 2_000_000_000)
        ledger.begin_wait("probe-a-acquisition", "probe-a", policy, 0, 0)
        ledger.begin_wait("probe-a-recovery", "probe-a", policy, 0, 0)
        ledger.begin_wait("route-recovery", "route-a", policy, 0, 0)

        self.assertEqual(
            ledger.end_owner_waits("probe-a"),
            ("probe-a-acquisition", "probe-a-recovery"),
        )
        self.assertEqual(
            tuple(token.wait_id for token in ledger.active_waits()),
            ("route-recovery",),
        )

    def test_existing_wait_cannot_be_adopted_by_another_owner(self):
        ledger = RetryLedger("task")
        policy = WaitPolicy(40, 2_000_000_000)
        ledger.begin_wait("shared-id", "probe-a", policy, 0, 0)
        with self.assertRaisesRegex(Exception, "owner"):
            ledger.begin_wait("shared-id", "probe-b", policy, 1, 1)

    def test_backtracking_and_route_revision_cannot_create_progress(self):
        ledger = RetryLedger("task", initial_support=(0, 64, 0))
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, 2, support=(0, 64, 0),
            valid_corridor=((0, 64, 0), (1, 64, 0)),
        )))
        self.assertTrue(ledger.record_progress(ProgressEvidence(
            ProgressKind.ROUTE_FRONTIER, 3, support=(1, 64, 0),
            valid_corridor=((0, 64, 0), (1, 64, 0)),
        )))
        progress_version = ledger.progress_version
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
        self.assertEqual(ledger.progress_version, progress_version)

    def test_multiple_new_facts_in_one_observation_clear_only_once(self):
        ledger = RetryLedger("task")
        ledger.set_blockers(("a", "b"))
        self.assertTrue(ledger.record_progress(ProgressEvidence(
            ProgressKind.BLOCKING_FACT, 3, fact_id="a",
        )))
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.BLOCKING_FACT, 3, fact_id="b",
        )))
        self.assertEqual(ledger.progress_version, 1)
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.BLOCKING_FACT, 4, fact_id="b",
        )))

    def test_new_fact_must_answer_current_blocker(self):
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
        self.assertEqual(ledger.progress_version, 6)

    def test_progress_capacity_refuses_new_reset_without_evicting_history(self):
        ledger = RetryLedger("task")
        for index in range(4096):
            self.assertTrue(ledger.record_progress(ProgressEvidence(
                ProgressKind.ACTION_COMPLETED, index,
                action_id=f"action-{index}",
            )))
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.ACTION_COMPLETED, 4096,
            action_id="action-over-capacity",
        )))
        self.assertTrue(ledger.progress_capacity_exhausted)
        self.assertFalse(ledger.record_progress(ProgressEvidence(
            ProgressKind.ACTION_COMPLETED, 4097,
            action_id="action-0",
        )))

    def test_wait_capacity_keeps_existing_deadlines(self):
        ledger = RetryLedger("task")
        policy = WaitPolicy(40, 2_000_000_000)
        for index in range(8):
            ledger.begin_wait(f"wait-{index}", "test-owner", policy, 0, 0)
        with self.assertRaises(RetryLedgerCapacityExceeded):
            ledger.begin_wait(
                "overflow", "test-owner", policy, 20, 1_000_000_000,
            )
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
        ledger.begin_wait(
            "information", "test-owner", policy, 10, 1_000_000_000,
        )
        ledger.begin_wait(
            "information", "test-owner", policy, 30, 2_000_000_000,
        )
        self.assertIs(ledger.check_wait("information", 49, 2_900_000_000),
                      WaitVerdict.WAITING)
        self.assertIs(ledger.check_wait("information", 50, 2_900_000_000),
                      WaitVerdict.EXHAUSTED_TICKS)
        self.assertIs(ledger.check_wait("information", 49, 3_000_000_000),
                      WaitVerdict.EXHAUSTED_CLOCK)


if __name__ == "__main__":
    unittest.main()

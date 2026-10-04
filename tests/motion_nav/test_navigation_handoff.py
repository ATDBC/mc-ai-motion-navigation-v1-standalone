from __future__ import annotations

import unittest
from dataclasses import replace

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.body_control import (
    HandoffDisposition, HandoffEvidence, StopCause,
)
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from mc2p.motion_nav.geometry import Aabb
from mc2p.motion_nav.movement_transition import (
    GoalState, GoalSupport, MovementMode,
)
from mc2p.motion_nav.navigation_handoff import (
    HandoffDestination, NavigationHandoffCoordinator,
    RecoveryRequestStatus,
)
from mc2p.motion_nav.navigation_owners import PendingGoalRevision
from mc2p.motion_nav.world_model import WorldSessionId
from mc2p.motion_nav.retry_ledger import (
    ProgressEvidence, ProgressKind, RecoveryBudgetPolicy,
    RecoveryIdentity, RecoveryLimitStatus, RetryLedger, TaskDemandState,
)


class _Clock:
    def __init__(self) -> None:
        self.now_ns = 0

    def __call__(self) -> int:
        return self.now_ns


def _goal(revision: int) -> PendingGoalRevision:
    x = revision + .5
    return PendingGoalRevision(
        "goal", revision,
        GoalState(
            Aabb(x - .05, 63.95, .45, x + .05, 64.05, .55),
            GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),
            frozenset({"standing"}),
            .6,
        ),
        TaskDamageBudget(),
    )


def _quiescent() -> HandoffEvidence:
    return HandoffEvidence(
        "route/old", WorldSessionId("world"),
        HandoffDisposition.QUIESCENT, 10, 20, MovementV1(),
        "supported_released_input_tail_verified",
    )


class NavigationHandoffTests(unittest.TestCase):
    def _permit(
        self, coordinator, budget, observation_sequence=1, *,
        demand=TaskDemandState.UNMET, progress=(),
    ):
        return coordinator.observe_task_activity(
            budget=budget,
            observation_sequence=observation_sequence,
            demand_state=demand,
            progress=progress,
        )

    def test_recovery_is_charged_once_and_waits_for_current_release(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        coordinator = NavigationHandoffCoordinator()
        budget = RetryLedger("goal")
        permit = self._permit(coordinator, budget)
        arguments = dict(request_id="route/deviation", destination=HandoffDestination.REPLAN,
                         reason="needs_replan", budget=budget,
                         activity_permit=permit,
                         recovery_identity=RecoveryIdentity(1, "route/deviation"))
        self.assertTrue(coordinator.request_recovery(**arguments))
        self.assertFalse(coordinator.request_recovery(**arguments))
        self.assertFalse(coordinator.request_recovery(**dict(arguments, request_id="route/another-label")))
        self.assertEqual(budget.total_recovery_starts, 1)
        self.assertIsNotNone(budget.active_recovery_id)
        handoff = replace(_quiescent(), world_session=frame.session,
                          observation_sequence_id=frame.body.sequence_id)
        facts = dict(goal_ready=True, start_ready=True, missing_cells=(),
                     unavailable_reason="current_surface_unavailable")
        self.assertIsNone(coordinator.advance(frame, handoff=replace(handoff,
            observation_sequence_id=frame.body.sequence_id + 1), **facts))
        resolution = coordinator.advance(frame, handoff=handoff, budget=budget, **facts)
        self.assertIs(resolution.destination, HandoffDestination.REPLAN)
        self.assertEqual(resolution.request_id, "route/deviation")
        self.assertIsNone(budget.active_recovery_id)
        self.assertIsNone(coordinator.advance(frame, handoff=handoff, **facts))

    def test_recovery_without_current_activity_permit_fails_closed(self):
        coordinator = NavigationHandoffCoordinator()
        budget = RetryLedger("goal")
        result = coordinator.request_recovery(
            request_id="route/deviation",
            destination=HandoffDestination.REPLAN,
            reason="needs_replan",
            budget=budget,
        )
        self.assertIs(result.status, RecoveryRequestStatus.ACTIVITY_REQUIRED)
        self.assertFalse(result)
        self.assertEqual(budget.total_recovery_starts, 0)
        self.assertIsNone(coordinator.stop_request)

    def test_same_frame_progress_is_absorbed_before_recovery_permission(self):
        clock = _Clock()
        budget = RetryLedger(
            "goal",
            policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12,
                recovery_window_ns=60,
                maximum_recovery_ns=10,
                maximum_no_progress_ns=3,
            ),
            clock_ns=clock,
        )
        coordinator = NavigationHandoffCoordinator()
        clock.now_ns = 3
        permit = self._permit(
            coordinator, budget,
            progress=(ProgressEvidence(
                ProgressKind.ACTION_COMPLETED, 1, action_id="step",
            ),),
        )
        self.assertIs(permit.limit_status, RecoveryLimitStatus.ALLOWED)
        result = coordinator.request_recovery(
            request_id="route/deviation",
            destination=HandoffDestination.REPLAN,
            reason="needs_replan",
            budget=budget,
            activity_permit=permit,
            recovery_identity=RecoveryIdentity(1, "route/deviation"),
        )
        self.assertIs(result.status, RecoveryRequestStatus.STAGED)
        self.assertEqual(budget.total_recovery_starts, 1)

    def test_expired_no_progress_rejects_before_charging_and_stages_typed_failure(self):
        clock = _Clock()
        budget = RetryLedger(
            "goal",
            policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12,
                recovery_window_ns=60,
                maximum_recovery_ns=10,
                maximum_no_progress_ns=3,
            ),
            clock_ns=clock,
        )
        coordinator = NavigationHandoffCoordinator()
        clock.now_ns = 3
        permit = self._permit(coordinator, budget)
        self.assertIs(permit.limit_status, RecoveryLimitStatus.NO_PROGRESS_EXHAUSTED)
        result = coordinator.request_recovery(
            request_id="route/deviation",
            destination=HandoffDestination.REPLAN,
            reason="needs_replan",
            budget=budget,
            activity_permit=permit,
            recovery_identity=RecoveryIdentity(1, "route/deviation"),
        )
        self.assertIs(result.status, RecoveryRequestStatus.LIMIT_EXHAUSTED)
        self.assertEqual(budget.total_recovery_starts, 0)
        self.assertIs(coordinator.stop_request.destination, HandoffDestination.FAIL)
        self.assertIs(
            coordinator.stop_request.recovery_limit_status,
            RecoveryLimitStatus.NO_PROGRESS_EXHAUSTED,
        )

    def test_duplicate_request_and_repeated_frame_charge_once(self):
        coordinator = NavigationHandoffCoordinator()
        budget = RetryLedger("goal")
        permit = self._permit(coordinator, budget, 7)
        first = coordinator.request_recovery(
            request_id="route/deviation", destination=HandoffDestination.REPLAN,
            reason="needs_replan", budget=budget, activity_permit=permit,
            recovery_identity=RecoveryIdentity(7, "route/deviation"),
        )
        repeated_permit = self._permit(coordinator, budget, 7)
        repeated = coordinator.request_recovery(
            request_id="route/deviation", destination=HandoffDestination.REPLAN,
            reason="needs_replan", budget=budget,
            activity_permit=repeated_permit,
            recovery_identity=RecoveryIdentity(7, "route/deviation"),
        )
        self.assertIs(first.status, RecoveryRequestStatus.STAGED)
        self.assertIs(repeated.status, RecoveryRequestStatus.DUPLICATE)
        self.assertEqual(first.recovery_identity, repeated.recovery_identity)
        self.assertEqual(budget.total_recovery_starts, 1)

    def test_recovery_finishes_only_with_verified_handoff_evidence(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        coordinator = NavigationHandoffCoordinator()
        budget = RetryLedger("goal")
        permit = self._permit(coordinator, budget, frame.body.sequence_id)
        result = coordinator.request_recovery(
            request_id="route/deviation", destination=HandoffDestination.REPLAN,
            reason="needs_replan", budget=budget, activity_permit=permit,
            recovery_identity=RecoveryIdentity(
                frame.body.sequence_id, "route/deviation",
            ),
        )
        self.assertTrue(result)
        budget.observe_task_activity(
            TaskDemandState.UNMET,
            ProgressEvidence(ProgressKind.ACTION_COMPLETED, 2, action_id="progress"),
        )
        self.assertEqual(budget.active_recovery_id, result.recovery_identity)
        retained = replace(
            _quiescent(), world_session=frame.session,
            observation_sequence_id=frame.body.sequence_id,
            disposition=HandoffDisposition.RETAIN,
        )
        self.assertIsNone(coordinator.advance(
            frame, handoff=retained, budget=budget,
            goal_ready=True, start_ready=True, missing_cells=(),
            unavailable_reason="unavailable",
        ))
        self.assertEqual(budget.active_recovery_id, result.recovery_identity)
        handoff = replace(
            _quiescent(), world_session=frame.session,
            observation_sequence_id=frame.body.sequence_id,
        )
        self.assertIsNotNone(coordinator.advance(
            frame, handoff=handoff, budget=budget,
            goal_ready=True, start_ready=True, missing_cells=(),
            unavailable_reason="unavailable",
        ))
        self.assertIsNone(budget.active_recovery_id)

    def test_persistent_thirteenth_recovery_stages_typed_failure(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, base_frame, _, _, _ = gap_owner(DeferredMotionWorker())
        clock = _Clock()
        budget = RetryLedger(
            "goal", policy=RecoveryBudgetPolicy.persistent(), clock_ns=clock,
        )
        coordinator = NavigationHandoffCoordinator()
        for index in range(12):
            current = replace(
                base_frame,
                body=replace(base_frame.body, sequence_id=index + 1),
            )
            permit = self._permit(coordinator, budget, index + 1)
            result = coordinator.request_recovery(
                request_id=f"route/recovery-{index}",
                destination=HandoffDestination.REPLAN,
                reason="needs_replan", budget=budget,
                activity_permit=permit,
                recovery_identity=RecoveryIdentity(
                    index + 1, f"route/recovery-{index}",
                ),
            )
            self.assertIs(result.status, RecoveryRequestStatus.STAGED)
            handoff = replace(
                _quiescent(), world_session=current.session,
                observation_sequence_id=current.body.sequence_id,
            )
            self.assertIsNotNone(coordinator.advance(
                current, handoff=handoff, budget=budget,
                goal_ready=True, start_ready=True, missing_cells=(),
                unavailable_reason="unavailable",
            ))
            clock.now_ns += 1_000_000_000

        permit = self._permit(coordinator, budget, 13)
        exhausted = coordinator.request_recovery(
            request_id="route/recovery-12",
            destination=HandoffDestination.REPLAN,
            reason="needs_replan", budget=budget,
            activity_permit=permit,
            recovery_identity=RecoveryIdentity(13, "route/recovery-12"),
        )
        self.assertIs(exhausted.status, RecoveryRequestStatus.LIMIT_EXHAUSTED)
        self.assertIs(
            exhausted.limit_status,
            RecoveryLimitStatus.PERSISTENT_RATE_EXHAUSTED,
        )
        self.assertEqual(budget.total_recovery_starts, 12)
        self.assertIs(coordinator.stop_request.destination, HandoffDestination.FAIL)

    def test_terminal_cleanup_keeps_active_recovery_deadline_and_requires_release(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        clock = _Clock()
        budget = RetryLedger(
            "goal", policy=RecoveryBudgetPolicy.persistent(), clock_ns=clock,
        )
        coordinator = NavigationHandoffCoordinator()
        permit = self._permit(coordinator, budget, frame.body.sequence_id)
        started = coordinator.request_recovery(
            request_id="route/recovery",
            destination=HandoffDestination.REPLAN,
            reason="needs_replan", budget=budget,
            activity_permit=permit,
            recovery_identity=RecoveryIdentity(
                frame.body.sequence_id, "route/recovery",
            ),
        )
        self.assertTrue(started)
        coordinator.request_recovery(
            request_id="task/cancel", destination=HandoffDestination.CANCEL,
            reason="cancelled", budget=budget,
        )
        clock.now_ns = 10_000_000_000
        cleanup = self._permit(
            coordinator, budget, frame.body.sequence_id + 1,
            demand=TaskDemandState.TERMINAL_CLEANUP,
        )
        self.assertIs(
            cleanup.limit_status,
            RecoveryLimitStatus.SINGLE_RECOVERY_EXHAUSTED,
        )
        self.assertEqual(budget.active_recovery_id, started.recovery_identity)
        with self.assertRaisesRegex(ContractViolation, "handoff evidence"):
            coordinator.finish_ending(budget=budget)
        handoff = replace(
            _quiescent(), world_session=frame.session,
            observation_sequence_id=frame.body.sequence_id,
        )
        resolution = coordinator.finish_ending(
            budget=budget, handoff=handoff, frame=frame,
        )
        self.assertIs(resolution.destination, HandoffDestination.CANCEL)
        self.assertIsNone(budget.active_recovery_id)

    def test_first_quiescent_at_eleven_seconds_releases_body_and_keeps_limit(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        clock = _Clock()
        budget = RetryLedger(
            "goal", policy=RecoveryBudgetPolicy.persistent(), clock_ns=clock,
        )
        coordinator = NavigationHandoffCoordinator()
        permit = self._permit(coordinator, budget, frame.body.sequence_id)
        started = coordinator.request_recovery(
            request_id="route/late-release",
            destination=HandoffDestination.REPLAN,
            reason="needs_replan",
            budget=budget,
            activity_permit=permit,
            recovery_identity=RecoveryIdentity(
                frame.body.sequence_id, "route/late-release",
            ),
        )
        self.assertTrue(started)
        clock.now_ns = 11_000_000_000

        next_observation = frame.body.sequence_id + 1
        coordinator.begin_task_activity(
            budget=budget,
            observation_sequence=next_observation,
            demand_state=TaskDemandState.UNMET,
        )
        current = replace(
            frame, body=replace(frame.body, sequence_id=next_observation),
        )
        handoff = replace(
            _quiescent(), world_session=current.session,
            observation_sequence_id=next_observation,
        )
        resolution = coordinator.advance(
            current, handoff=handoff, budget=budget,
            goal_ready=True, start_ready=True, missing_cells=(),
            unavailable_reason="unavailable",
        )

        self.assertIs(resolution.destination, HandoffDestination.REPLAN)
        self.assertIsNone(budget.active_recovery_id)
        permit = coordinator.finalize_task_activity(
            budget=budget, observation_sequence=next_observation,
        )
        self.assertIs(
            permit.limit_status,
            RecoveryLimitStatus.SINGLE_RECOVERY_EXHAUSTED,
        )
        self.assertIs(
            coordinator.stop_request.destination,
            HandoffDestination.FAIL,
        )

    def test_stable_satisfied_activity_has_no_no_progress_timeout(self):
        clock = _Clock()
        budget = RetryLedger(
            "goal", policy=RecoveryBudgetPolicy.persistent(), clock_ns=clock,
        )
        coordinator = NavigationHandoffCoordinator()
        first = self._permit(
            coordinator, budget, 1,
            demand=TaskDemandState.STABLE_SATISFIED,
        )
        clock.now_ns = 600_000_000_000
        later = self._permit(
            coordinator, budget, 2,
            demand=TaskDemandState.STABLE_SATISFIED,
        )
        self.assertIs(first.limit_status, RecoveryLimitStatus.ALLOWED)
        self.assertIs(later.limit_status, RecoveryLimitStatus.ALLOWED)
        self.assertIsNone(coordinator.stop_request)

    def test_frame_start_does_not_exhaust_before_same_frame_progress(self):
        clock = _Clock()
        budget = RetryLedger(
            "goal", policy=RecoveryBudgetPolicy.persistent(
                maximum_recoveries=12, recovery_window_ns=60,
                maximum_recovery_ns=10, maximum_no_progress_ns=3,
            ), clock_ns=clock,
        )
        coordinator = NavigationHandoffCoordinator()
        clock.now_ns = 3
        coordinator.begin_task_activity(
            budget=budget, observation_sequence=4,
            demand_state=TaskDemandState.UNMET,
        )
        self.assertIs(budget.recovery_limit_status, RecoveryLimitStatus.ALLOWED)
        coordinator.record_task_progress(
            budget=budget, observation_sequence=4,
            demand_state=TaskDemandState.UNMET,
            progress=(ProgressEvidence(
                ProgressKind.ACTION_COMPLETED, 4, action_id="boundary-step",
            ),),
        )
        permit = coordinator.finalize_task_activity(
            budget=budget, observation_sequence=4,
        )
        self.assertIs(permit.limit_status, RecoveryLimitStatus.ALLOWED)

    def test_permit_expires_with_task_clock_and_is_one_use(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        clock = _Clock()
        policy = RecoveryBudgetPolicy.persistent(
            maximum_recoveries=12, recovery_window_ns=60,
            maximum_recovery_ns=10, maximum_no_progress_ns=3,
        )
        expired_budget = RetryLedger("expired", policy=policy, clock_ns=clock)
        expired = NavigationHandoffCoordinator()
        permit = self._permit(expired, expired_budget, 1)
        clock.now_ns = 4
        denied = expired.request_recovery(
            request_id="route/late", destination=HandoffDestination.REPLAN,
            reason="late", budget=expired_budget, activity_permit=permit,
            recovery_identity=RecoveryIdentity(1, "route/late"),
        )
        self.assertIs(denied.status, RecoveryRequestStatus.LIMIT_EXHAUSTED)
        self.assertEqual(expired_budget.total_recovery_starts, 0)

        clock = _Clock()
        budget = RetryLedger("once", policy=policy, clock_ns=clock)
        coordinator = NavigationHandoffCoordinator()
        permit = self._permit(
            coordinator, budget, frame.body.sequence_id,
        )
        first = coordinator.request_recovery(
            request_id="route/first", destination=HandoffDestination.REPLAN,
            reason="first", budget=budget, activity_permit=permit,
            recovery_identity=RecoveryIdentity(
                frame.body.sequence_id, "route/first",
            ),
        )
        handoff = replace(
            _quiescent(), world_session=frame.session,
            observation_sequence_id=frame.body.sequence_id,
        )
        coordinator.advance(
            frame, handoff=handoff, budget=budget,
            goal_ready=True, start_ready=True, missing_cells=(),
            unavailable_reason="unavailable",
        )
        second = coordinator.request_recovery(
            request_id="route/second", destination=HandoffDestination.REPLAN,
            reason="second", budget=budget, activity_permit=permit,
            recovery_identity=RecoveryIdentity(
                frame.body.sequence_id, "route/second",
            ),
        )
        self.assertTrue(first)
        self.assertIs(second.status, RecoveryRequestStatus.ACTIVITY_REQUIRED)
        self.assertEqual(budget.total_recovery_starts, 1)
        self.assertFalse(hasattr(coordinator, "_recovery_by_request"))

    def test_unseen_stale_observation_cannot_purchase_recovery(self):
        clock = _Clock()
        budget = RetryLedger(
            "stale", policy=RecoveryBudgetPolicy.persistent(), clock_ns=clock,
        )
        coordinator = NavigationHandoffCoordinator()
        permit = self._permit(coordinator, budget, 5)

        stale = coordinator.request_recovery(
            request_id="route/late-unseen",
            destination=HandoffDestination.REPLAN,
            reason="late_unseen",
            budget=budget,
            activity_permit=permit,
            recovery_identity=RecoveryIdentity(4, "route/late-unseen"),
        )

        self.assertIs(stale.status, RecoveryRequestStatus.STALE)
        self.assertEqual(budget.total_recovery_starts, 0)
        self.assertIsNone(budget.active_recovery_id)
        self.assertIsNone(coordinator.stop_request)

    def test_recovery_waits_for_missing_support_and_keeps_unavailable_support_as_failure(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        handoff = replace(_quiescent(), world_session=frame.session,
                          observation_sequence_id=frame.body.sequence_id)
        for missing, expected in (((), HandoffDestination.FAIL),
                                  (((0, 63, 0),), HandoffDestination.WAIT_FOR_INFORMATION)):
            with self.subTest(destination=expected):
                coordinator = NavigationHandoffCoordinator()
                budget = RetryLedger("goal")
                permit = self._permit(coordinator, budget)
                coordinator.request_recovery(request_id="route/deviation", destination=HandoffDestination.REPLAN,
                                             reason="needs_replan", budget=budget,
                                             activity_permit=permit,
                                             recovery_identity=RecoveryIdentity(
                                                 1, "route/deviation",
                                             ))
                result = coordinator.advance(frame, handoff=handoff, goal_ready=True, start_ready=False,
                                             missing_cells=missing, unavailable_reason="current_surface_unavailable",
                                             budget=budget)
                self.assertIs(result.destination, expected)
                self.assertEqual(result.missing_cells, missing)

    def test_cancel_does_not_spend_retry_and_cannot_be_revised_into_recovery(self):
        coordinator = NavigationHandoffCoordinator()
        budget = RetryLedger("goal")
        coordinator.request_recovery(request_id="task/cancel", destination=HandoffDestination.CANCEL,
                                     reason="cancelled", budget=budget)
        self.assertEqual(budget.total_recovery_starts, 0)
        with self.assertRaisesRegex(ContractViolation, "ending"):
            coordinator.stage_goal(_goal(2), StopCause.GOAL_REVISED, "revision-2")

    def test_newer_goal_revision_atomically_replaces_uncommitted_revision(self):
        coordinator = NavigationHandoffCoordinator()
        coordinator.stage_goal(
            _goal(2), StopCause.GOAL_REVISED, "revision-2",
        )
        coordinator.stage_goal(
            _goal(3), StopCause.GOAL_REVISED, "revision-3",
        )

        self.assertEqual(coordinator.pending_goal, _goal(3))
        self.assertEqual(coordinator.stop_request.reason, "revision-3")
        with self.assertRaisesRegex(Exception, "newer"):
            coordinator.stage_goal(
                _goal(2), StopCause.GOAL_REVISED, "stale-revision",
            )

    def test_handoff_result_is_chosen_once_from_current_facts(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        handoff = replace(_quiescent(), world_session=frame.session,
                          observation_sequence_id=frame.body.sequence_id)
        cases = (
            (True, True, (), HandoffDestination.REPLAN),
            (False, True, ((1, 64, 1),), HandoffDestination.WAIT_FOR_INFORMATION),
            (False, True, (), HandoffDestination.FAIL),
        )
        for goal_ready, start_ready, missing, expected in cases:
            with self.subTest(expected=expected):
                coordinator = NavigationHandoffCoordinator()
                coordinator.stage_goal(
                    _goal(2), StopCause.GOAL_REVISED, "goal-revised",
                )
                resolution = coordinator.resolve_goal(
                    frame, handoff, goal_ready=goal_ready,
                    start_ready=start_ready, missing_cells=missing,
                    unavailable_reason="goal_surface_unavailable",
                )
                self.assertIs(resolution.destination, expected)
                self.assertEqual(resolution.pending_goal, _goal(2))
                self.assertIsNone(coordinator.pending_goal)

    def test_goal_handoff_requires_current_quiescent_evidence(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        coordinator = NavigationHandoffCoordinator()
        coordinator.stage_goal(
            _goal(2), StopCause.GOAL_REVISED, "goal-revised",
        )
        transferable = HandoffEvidence(
            "route/old", WorldSessionId("world"),
            HandoffDisposition.TRANSFERABLE, 10, 20,
            MovementV1(forward=1), "successor_selected",
            "route/new", 7, 2, 0,
        )

        self.assertIsNone(coordinator.resolve_goal(
            frame, transferable, goal_ready=True, start_ready=True,
            missing_cells=(), unavailable_reason="unavailable",
        ))
        self.assertEqual(coordinator.pending_goal, _goal(2))


if __name__ == "__main__":
    unittest.main()

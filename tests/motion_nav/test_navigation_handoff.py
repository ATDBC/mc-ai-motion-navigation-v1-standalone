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
)
from mc2p.motion_nav.navigation_owners import PendingGoalRevision
from mc2p.motion_nav.world_model import WorldSessionId
from mc2p.motion_nav.retry_ledger import RetryLedger


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
    def test_recovery_is_charged_once_and_waits_for_current_release(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        coordinator = NavigationHandoffCoordinator()
        budget = RetryLedger("goal")
        arguments = dict(request_id="route/deviation", destination=HandoffDestination.REPLAN,
                         reason="needs_replan", budget=budget)
        self.assertTrue(coordinator.request_recovery(**arguments))
        self.assertFalse(coordinator.request_recovery(**arguments))
        self.assertFalse(coordinator.request_recovery(**dict(arguments, request_id="route/another-label")))
        self.assertEqual(budget.total_failures, 1)
        handoff = replace(_quiescent(), world_session=frame.session,
                          observation_sequence_id=frame.body.sequence_id)
        facts = dict(goal_ready=True, start_ready=True, missing_cells=(),
                     unavailable_reason="current_surface_unavailable")
        self.assertIsNone(coordinator.advance(frame, handoff=replace(handoff,
            observation_sequence_id=frame.body.sequence_id + 1), **facts))
        resolution = coordinator.advance(frame, handoff=handoff, **facts)
        self.assertIs(resolution.destination, HandoffDestination.REPLAN)
        self.assertEqual(resolution.request_id, "route/deviation")
        self.assertIsNone(coordinator.advance(frame, handoff=handoff, **facts))

    def test_recovery_waits_for_missing_support_and_keeps_unavailable_support_as_failure(self):
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        handoff = replace(_quiescent(), world_session=frame.session,
                          observation_sequence_id=frame.body.sequence_id)
        for missing, expected in (((), HandoffDestination.FAIL),
                                  (((0, 63, 0),), HandoffDestination.WAIT_FOR_INFORMATION)):
            with self.subTest(destination=expected):
                coordinator = NavigationHandoffCoordinator()
                coordinator.request_recovery(request_id="route/deviation", destination=HandoffDestination.REPLAN,
                                             reason="needs_replan", budget=RetryLedger("goal"))
                result = coordinator.advance(frame, handoff=handoff, goal_ready=True, start_ready=False,
                                             missing_cells=missing, unavailable_reason="current_surface_unavailable")
                self.assertIs(result.destination, expected)
                self.assertEqual(result.missing_cells, missing)

    def test_cancel_does_not_spend_retry_and_cannot_be_revised_into_recovery(self):
        coordinator = NavigationHandoffCoordinator()
        budget = RetryLedger("goal")
        coordinator.request_recovery(request_id="task/cancel", destination=HandoffDestination.CANCEL,
                                     reason="cancelled", budget=budget)
        self.assertEqual(budget.total_failures, 0)
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
                    _quiescent(), goal_ready=goal_ready,
                    start_ready=start_ready, missing_cells=missing,
                    unavailable_reason="goal_surface_unavailable",
                )
                self.assertIs(resolution.destination, expected)
                self.assertEqual(resolution.pending_goal, _goal(2))
                self.assertIsNone(coordinator.pending_goal)

    def test_goal_handoff_requires_current_quiescent_evidence(self):
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

        with self.assertRaisesRegex(Exception, "quiescent"):
            coordinator.resolve_goal(
                transferable, goal_ready=True, start_ready=True,
                missing_cells=(), unavailable_reason="unavailable",
            )
        self.assertEqual(coordinator.pending_goal, _goal(2))


if __name__ == "__main__":
    unittest.main()

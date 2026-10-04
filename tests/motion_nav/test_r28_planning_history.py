from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.motion_nav.async_work import AsyncAdmissionDisposition
from mc2p.motion_nav.known_map_planner import SurfacePlanningStatus
from mc2p.motion_nav.planning_coordinator import (
    PlanningHistoryCapacityExceeded,
    PlanningUpdateKind,
)
from tests.motion_nav.test_b07_step_route import frame
from tests.motion_nav.test_planning_coordinator import _permit
from tests.motion_nav import test_r28_dual_planning as dual_fixtures


class PlanningHistoryTests(unittest.TestCase):
    def setUp(self):
        self.helper = dual_fixtures.DualPlanningTests()
        self.world, self.planner, self.owner, _ = self.helper.start()
        self.clock = [1_000_000_000]
        self.owner._clock = lambda: self.clock[0]
        self.completed_history = []

    def begin_revision(self, revision, **changes):
        tick = revision * 2
        self.clock[0] = 1_000_000_000 + tick * 50_000_000
        request = replace(self.owner.request, sequence=revision,
            request_id=f'history-request-{revision}', goal_revision=revision,
            work_identity=None, **changes)
        self.owner.revise_request(request)
        self.owner.begin(request, frame(self.world, tick, (-.5, 1., .5)),
            permit=replace(_permit(), permit_id=f'history-permit-{revision}',
                goal_revision=revision), state_anchor=None,
            remaining_damage_budget=request.damage_budget)
        self.helper.advance(self.owner, self.world, tick)
        return tick

    def deliver_current(self, tick):
        self.planner.deliveries = [self.planner.results[-1]]
        update = self.helper.advance(self.owner, self.world, tick + 1)
        self.assertIs(update.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertEqual(update.route.work_identity, self.planner.requests[-1].work_identity)
        identity = update.route.work_identity
        self.completed_history.append((self.owner._retired[identity],
            self.owner._known_work_windows[identity]))
        return update

    def complete_revisions(self, first=2, last=161):
        for revision in range(first, last + 1):
            self.deliver_current(self.begin_revision(revision))

    def assert_history_bounded(self):
        self.assertLessEqual(len(self.owner._retired), 64)
        self.assertLessEqual(len(self.owner._known_work_windows), 64)
        self.assertLessEqual(len(self.owner._event_history), 128)
        self.assertLessEqual(len(self.owner.async_diagnostics.events), 384)
        self.assertLessEqual(len(self.owner.admission_records), 64)
        self.assertEqual(self.owner._retry_ledger.total_recovery_starts, 0)

    def test_160_revisions_bound_history_and_classify_evicted_duplicate(self):
        self.helper.advance(self.owner, self.world, 0)
        first_result = self.planner.results[0]
        self.deliver_current(0)
        self.complete_revisions()
        self.assert_history_bounded()
        self.assertNotIn(first_result.work_identity, self.owner._retired)
        self.assertNotIn(first_result.work_identity, self.owner._known_work_windows)
        tick = self.begin_revision(162)
        self.planner.deliveries = [first_result, self.planner.results[-1]]
        update = self.helper.advance(self.owner, self.world, tick + 1)
        self.assertIs(update.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertEqual(update.route.work_identity, self.planner.requests[-1].work_identity)
        late = next(record for record in self.owner.admission_records
            if record.identity == first_result.work_identity)
        self.assertIs(late.disposition, AsyncAdmissionDisposition.DISCARDED_LATE)
        self.assertFalse(late.identity_matched)
        self.assert_history_bounded()

    def test_160_revisions_preserve_old_pending_receipt_and_its_original_window(self):
        self.helper.advance(self.owner, self.world, 0)
        old_result = self.planner.results[0]
        old_identity = old_result.work_identity
        old_window = self.owner.work_window
        self.complete_revisions()
        self.assert_history_bounded()
        self.assertIn(old_identity, self.owner._retired)
        self.assertEqual(self.owner._known_work_windows[old_identity], old_window)
        self.assertEqual(self.owner.async_diagnostics.pending_planning_receipts,
            (old_identity,))
        tick = self.begin_revision(162)
        self.planner.deliveries = [old_result, self.planner.results[-1]]
        update = self.helper.advance(self.owner, self.world, tick + 1)
        self.assertIs(update.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertEqual(update.route.work_identity, self.planner.requests[-1].work_identity)
        late = next(record for record in self.owner.admission_records
            if record.identity == old_identity)
        self.assertIs(late.disposition, AsyncAdmissionDisposition.DISCARDED_LATE)
        self.assertEqual(late.deadline_monotonic_ns, old_window.deadline_monotonic_ns)
        self.assertFalse(self.owner.async_diagnostics.pending_planning_receipts)
        self.assert_history_bounded()

    def test_failure_origin_remains_valid_after_160_revisions_and_older_result(self):
        from tests.motion_nav.test_navigation_session import _goal
        self.helper.advance(self.owner, self.world, 0)
        self.deliver_current(0)
        self.complete_revisions()
        tick = self.begin_revision(162)
        older_result = self.planner.results[-1]
        self.helper.revise(self.owner, 163,
            goal_state=_goal((-1.5, 1., 1.5)))
        self.helper.advance(self.owner, self.world, tick + 1)
        failed_result = self.planner.results[-1]
        self.planner.deliveries = [replace(failed_result,
            status=SurfacePlanningStatus.INTERNAL_ERROR, path=(), segments=())]
        update = self.helper.advance(self.owner, self.world, tick + 2)
        self.assertIs(update.kind, PlanningUpdateKind.RUNNING)
        self.assertIn(failed_result.work_identity, self.owner._retired)
        self.assertIn(failed_result.work_identity, self.owner._known_work_windows)
        # A delayed prune can merge older retirement records after the failure.
        # Replay real completed records so both failure and live work must stay pinned.
        for summary, window in self.completed_history:
            self.owner._remember_history(self.owner._retired, summary.identity, summary)
            self.owner._remember_work_window(summary.identity, window)
        self.assertIn(failed_result.work_identity, self.owner._retired)
        self.assertIn(failed_result.work_identity, self.owner._known_work_windows)
        self.assertIn(older_result.work_identity, self.owner._known_work_windows)
        self.planner.deliveries = [older_result]
        update = self.helper.advance(self.owner, self.world, tick + 3)
        self.assertIs(update.kind, PlanningUpdateKind.FAILED)
        self.assertEqual(update.attempt_id, failed_result.work_identity.key)
        self.assertIsNone(self.owner._selected.lifecycle.identity)
        self.assertNotEqual(self.owner._selected.attempt_id, update.attempt_id)
        self.assertTrue(self.owner.failure_matches_retired_work(update.failure))
        for summary, window in self.completed_history:
            self.owner._remember_history(self.owner._retired, summary.identity, summary)
            self.owner._remember_work_window(summary.identity, window)
        self.assertTrue(self.owner.failure_matches_retired_work(update.failure))
        self.assertIn(failed_result.work_identity, self.owner._known_work_windows)
        self.assert_history_bounded()

    def test_full_protected_history_fails_with_type_without_dropping_receipt(self):
        self.helper.advance(self.owner, self.world, 0)
        old_identity = self.planner.results[0].work_identity
        old_window = self.owner.work_window
        with patch('mc2p.motion_nav.planning_coordinator._PLANNING_HISTORY_LIMIT', 1):
            with self.assertRaises(PlanningHistoryCapacityExceeded):
                self.begin_revision(2)
        self.assertEqual(self.owner._known_work_windows, {old_identity: old_window})
        self.assertEqual(set(self.owner._retired), {old_identity})
        self.assertEqual(self.owner.async_diagnostics.pending_planning_receipts,
            (old_identity,))
        self.assertEqual(len(self.planner.requests), 1)
        self.assertEqual(self.owner._attempt_sequence, 1)
        self.planner.deliveries = [self.planner.results[0]]
        self.owner._poll_results()
        self.assertIs(self.owner.admission_records[-1].disposition,
            AsyncAdmissionDisposition.DISCARDED_LATE)
        self.assert_history_bounded()


if __name__ == '__main__':
    unittest.main()

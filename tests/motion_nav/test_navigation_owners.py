from __future__ import annotations

import unittest

from mc2p.motion_nav.navigation_owners import (
    GoalRequestLedger,
    InformationAcquisitionState,
    PlanningPipelineState,
)


class NavigationOwnerTests(unittest.TestCase):
    def test_goal_request_ledger_replaces_one_authoritative_request(self):
        owner = GoalRequestLedger()
        owner.request = "request-1"
        owner.pending_goal = ("goal", 2)
        owner.request = "request-2"

        self.assertEqual(owner.request, "request-2")
        self.assertEqual(owner.pending_goal, ("goal", 2))

    def test_planning_pipeline_clear_drops_only_planning_state(self):
        owner = PlanningPipelineState()
        owner.builder = "builder"
        owner.snapshot = "snapshot"
        owner.snapshot_request_id = "request"
        owner.changed_cells.add((1, 2, 3))

        owner.clear()

        self.assertIsNone(owner.builder)
        self.assertIsNone(owner.snapshot)
        self.assertIsNone(owner.snapshot_request_id)
        self.assertEqual(owner.changed_cells, set())

    def test_information_owner_clears_one_acquisition_without_planning_state(self):
        owner = InformationAcquisitionState()
        owner.missing_cells = ((1, 2, 3),)
        owner.statuses[(1, 2, 3)] = "outside_view"
        owner.lower_required.add((1, 2, 3))
        owner.wait_frames = 8

        owner.clear_request()

        self.assertEqual(owner.missing_cells, ())
        self.assertEqual(owner.statuses, {})
        self.assertEqual(owner.lower_required, set())
        self.assertEqual(owner.wait_frames, 0)


if __name__ == "__main__":
    unittest.main()

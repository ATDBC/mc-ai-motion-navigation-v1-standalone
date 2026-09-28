from __future__ import annotations

from dataclasses import replace
import unittest

from mc2p.motion_nav.action_preconditions import (
    ActionPreconditionStatus,
    check_action_precondition,
)
from mc2p.motion_nav.action_route import ActionRoute, ControlledDropSegment
from mc2p.motion_nav.controlled_drop import ControlledDropEdge
from mc2p.motion_nav.route_admission import ActiveRoute, ExecutableCorridor
from mc2p.motion_nav.support_surfaces import (
    HorizontalRegion, SupportSurface, SurfaceNodeId,
)
from mc2p.motion_nav.world_model import ObservationStamp, VisualAirEvidence
from tests.motion_nav.test_continuous_descent import SESSION, world_and_anchor
from tests.motion_nav.test_b10_motion_candidate import (
    VerifiedMotionRouteIntegrationTests,
)


def _drop_route() -> ActiveRoute:
    start_id = SurfaceNodeId(0, 0, 64, 0)
    end_id = SurfaceNodeId(0, 1, 58, 0)
    start = SupportSurface(
        start_id, (.5, 64.0, .5), HorizontalRegion(0, 0, 1, 1),
        1.0, ("minecraft:grass_block",), (),
    )
    end = SupportSurface(
        end_id, (.5, 58.0, 1.5), HorizontalRegion(0, 1, 1, 2),
        1.0, ("minecraft:grass_block",), (),
    )
    action = ControlledDropSegment(
        ControlledDropEdge(start_id, end_id, "direct-fall", 1.0, ()),
        start, end, (),
    )
    action_route = ActionRoute("precondition-route", (action,))
    return ActiveRoute(
        "precondition-route", 3, "request", "goal", 2, SESSION.value,
        None, 1.0, 0.0, (),
        ExecutableCorridor((start_id, end_id), (), 1.0, end_id),
        action_route, planning_generation=4,
    )


class ActionPreconditionTests(unittest.TestCase):
    def test_far_drop_requests_bound_acquisition_at_action_boundary(self):
        anchor, physics_world = world_and_anchor(direct_height=6, speed=0.0)
        frame = VerifiedMotionRouteIntegrationTests.frame(
            physics_world._world, anchor.physics_state, 1,
        )

        result = check_action_precondition(
            _drop_route(), 0, frame, task_id="navigation-task",
        )

        self.assertIs(
            result.status, ActionPreconditionStatus.NEEDS_ACQUISITION,
        )
        self.assertEqual(result.missing_cells, ((0, 58, 1),))
        self.assertIsNotNone(result.acquisition)
        self.assertEqual(result.acquisition.route_id, "precondition-route")
        self.assertEqual(result.acquisition.route_revision, 3)
        self.assertEqual(result.acquisition.action_index, 0)
        self.assertEqual(result.acquisition.goal_revision, 2)

    def test_fresh_nearby_lower_evidence_makes_drop_ready(self):
        anchor, physics_world = world_and_anchor(direct_height=6, speed=0.0)
        owner = physics_world._world._owner
        self.assertIsNotNone(owner)
        stamp = ObservationStamp(SESSION, 2, 2, "test", 100_000_000)
        owner.confirm_air(
            stamp, ((0, 58, 1),),
            {(0, 58, 1): VisualAirEvidence(stamp, 4.0, True)},
        )
        frame = VerifiedMotionRouteIntegrationTests.frame(
            owner.view(), replace(anchor.physics_state, movement_tick_id=2), 2,
        )

        result = check_action_precondition(
            _drop_route(), 0, frame, task_id="navigation-task",
        )

        self.assertIs(result.status, ActionPreconditionStatus.READY)
        self.assertIsNone(result.acquisition)


if __name__ == "__main__":
    unittest.main()

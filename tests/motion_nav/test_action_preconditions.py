from __future__ import annotations

from dataclasses import replace
import unittest

from mc2p.motion_nav.action_preconditions import (
    AcquisitionGrant,
    ActionPreconditionReason,
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
        support = owner.view().cell((0, 57, 1)).block
        self.assertIsNotNone(support)
        owner.observe_blocks(stamp, {(0, 57, 1): support})
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

    def test_acquisition_grant_survives_later_air_view_without_lower_region(self):
        anchor, physics_world = world_and_anchor(direct_height=6, speed=0.0)
        owner = physics_world._world._owner
        self.assertIsNotNone(owner)
        route = _drop_route()
        lower_stamp = ObservationStamp(
            SESSION, 2, 2, "edge-probe", 100_000_000,
        )
        owner.confirm_air(
            lower_stamp, ((0, 58, 1),),
            {(0, 58, 1): VisualAirEvidence(lower_stamp, 4.0, True)},
        )
        grant = AcquisitionGrant(
            "landing-proof", route.route_id, route.route_revision, 0,
            (0, 58, 1), lower_stamp.sequence_id, (),
        )

        ordinary_stamp = ObservationStamp(
            SESSION, 3, 3, "ordinary-view", 150_000_000,
        )
        support = owner.view().cell((0, 57, 1)).block
        self.assertIsNotNone(support)
        owner.observe_blocks(ordinary_stamp, {(0, 57, 1): support})
        owner.confirm_air(
            ordinary_stamp, ((0, 58, 1),),
            {(0, 58, 1): VisualAirEvidence(
                ordinary_stamp, 4.0, False,
            )},
        )
        frame = VerifiedMotionRouteIntegrationTests.frame(
            owner.view(), replace(anchor.physics_state, movement_tick_id=3), 3,
        )

        result = check_action_precondition(
            route, 0, frame, task_id="navigation-task",
            acquisition_grant=grant,
        )

        self.assertIs(result.status, ActionPreconditionStatus.READY)
        self.assertIs(result.reason, ActionPreconditionReason.READY)

    def test_old_or_wrong_acquisition_grant_cannot_make_drop_ready(self):
        anchor, physics_world = world_and_anchor(direct_height=6, speed=0.0)
        owner = physics_world._world._owner
        self.assertIsNotNone(owner)
        route = _drop_route()
        current_stamp = ObservationStamp(
            SESSION, 100, 100, "ordinary-view", 5_000_000_000,
        )
        support = owner.view().cell((0, 57, 1)).block
        self.assertIsNotNone(support)
        owner.observe_blocks(current_stamp, {(0, 57, 1): support})
        owner.confirm_air(
            current_stamp, ((0, 58, 1),),
            {(0, 58, 1): VisualAirEvidence(
                current_stamp, 4.0, False,
            )},
        )
        frame = VerifiedMotionRouteIntegrationTests.frame(
            owner.view(),
            replace(anchor.physics_state, movement_tick_id=100),
            100,
        )
        grants = (
            AcquisitionGrant(
                "old-route", "another-route", route.route_revision, 0,
                (0, 58, 1), 100, (),
            ),
            AcquisitionGrant(
                "old-revision", route.route_id, route.route_revision + 1, 0,
                (0, 58, 1), 100, (),
            ),
            AcquisitionGrant(
                "old-action", route.route_id, route.route_revision, 1,
                (0, 58, 1), 100, (),
            ),
            AcquisitionGrant(
                "expired-evidence", route.route_id, route.route_revision, 0,
                (0, 58, 1), 1, (),
            ),
        )

        for grant in grants:
            with self.subTest(grant=grant.acquisition_id):
                result = check_action_precondition(
                    route, 0, frame, task_id="navigation-task",
                    acquisition_grant=grant,
                )

                self.assertIs(
                    result.status,
                    ActionPreconditionStatus.NEEDS_ACQUISITION,
                )
                self.assertIsNotNone(result.acquisition)

    def test_fresh_air_where_landing_support_was_rejects_drop(self):
        anchor, physics_world = world_and_anchor(direct_height=6, speed=0.0)
        owner = physics_world._world._owner
        self.assertIsNotNone(owner)
        stamp = ObservationStamp(SESSION, 2, 2, "test", 100_000_000)
        owner.confirm_air(
            stamp,
            ((0, 57, 1), (0, 58, 1)),
            {(0, 58, 1): VisualAirEvidence(stamp, 4.0, True)},
        )
        frame = VerifiedMotionRouteIntegrationTests.frame(
            owner.view(), replace(anchor.physics_state, movement_tick_id=2), 2,
        )

        result = check_action_precondition(
            _drop_route(), 0, frame, task_id="navigation-task",
        )

        self.assertIs(result.status, ActionPreconditionStatus.REJECTED)
        self.assertIs(
            result.reason, ActionPreconditionReason.LANDING_SUPPORT_MISSING,
        )

    def test_stale_landing_support_requires_refresh_before_drop(self):
        anchor, physics_world = world_and_anchor(direct_height=6, speed=0.0)
        owner = physics_world._world._owner
        self.assertIsNotNone(owner)
        stamp = ObservationStamp(SESSION, 10, 10, "test", 500_000_000)
        owner.confirm_air(
            stamp, ((0, 58, 1),),
            {(0, 58, 1): VisualAirEvidence(stamp, 4.0, True)},
        )
        frame = VerifiedMotionRouteIntegrationTests.frame(
            owner.view(), replace(anchor.physics_state, movement_tick_id=10), 10,
        )

        result = check_action_precondition(
            _drop_route(), 0, frame, task_id="navigation-task",
        )

        self.assertIs(
            result.status, ActionPreconditionStatus.NEEDS_INFORMATION,
        )
        self.assertIs(
            result.reason,
            ActionPreconditionReason.LANDING_SUPPORT_INFORMATION_REQUIRED,
        )
        self.assertIn((0, 57, 1), result.missing_cells)


if __name__ == "__main__":
    unittest.main()

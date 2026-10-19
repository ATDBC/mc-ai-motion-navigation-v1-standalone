"""Probe completion chooses a destination without granting body release."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.body_control import HandoffDisposition, HandoffEvidence, StopCause
from mc2p.motion_nav.landing_edge_probe import LandingEdgeProbe, LandingEdgeProbeState
from mc2p.motion_nav.navigation_handoff import HandoffDestination, NavigationHandoffCoordinator
from mc2p.motion_nav.navigation_owners import InformationAcquisitionState
from mc2p.motion_nav.probe_body_controller import ProbeBodyController
from mc2p.motion_nav.retry_ledger import RecoveryIdentity, RetryLedger, TaskDemandState
from mc2p.motion_nav.world_model import WorldSessionId
from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner


class ProbeHandoffResolutionTests(unittest.TestCase):
    def setUp(self):
        owner, self.frame, self.anchor, _, self.ledger = gap_owner(DeferredMotionWorker())
        self.anchor = replace(self.anchor, observation_sequence_id=self.frame.body.sequence_id)
        self.route = owner.route
        self.coordinator = NavigationHandoffCoordinator()
        self.probe = LandingEdgeProbe("goal", 1, (0, 62, 2), 0,
            world_session=self.frame.session.value)
        self.probe.request_stop(StopCause.ROUTE_REPLACED)
        self.controller = ProbeBodyController(self.probe)
        self.handoff = HandoffEvidence(self.probe.owner_id, self.frame.session,
            HandoffDisposition.QUIESCENT, self.frame.body.sequence_id, None,
            MovementV1(), "verified_probe_retreat")

    def resolve(self, **changes):
        arguments = dict(handoff=self.handoff, route=None, action_index=None,
                         current_request_id="request", pending_route_id=None)
        arguments.update(changes)
        return self.coordinator.resolve_probe(self.frame,
            outcome=self.controller.outcome(self.frame), **arguments)

    def test_accepted_ending_stops_suspended_route_before_publishing(self):
        self.coordinator.request_recovery(request_id="end", destination=HandoffDestination.CANCEL,
            reason="cancelled", budget=RetryLedger("goal"))
        result = self.resolve(route=self.route, action_index=0)
        self.assertEqual(result.action.value, "stop_suspended_route")
        self.assertEqual(result.reason, "cancelled")
        self.assertTrue(self.coordinator.accepted_ending)
        self.assertEqual(self.resolve().action.value, "finish_ending")

    def test_acquisition_timeout_remains_failure_with_suspended_route(self):
        self.probe.request_stop(StopCause.ACQUISITION_TIMED_OUT)
        result = self.resolve(route=self.route, action_index=0)
        self.assertEqual(result.action.value, "acquisition_failed")
        self.assertEqual(result.reason, "edge_probe_acquisition_timeout")

    def test_registered_retry_keeps_its_identity_and_charge(self):
        budget = RetryLedger("goal")
        permit = self.coordinator.observe_task_activity(
            budget=budget,
            observation_sequence=self.frame.body.sequence_id,
            demand_state=TaskDemandState.UNMET,
        )
        self.coordinator.request_recovery(request_id="route/input-lost",
            destination=HandoffDestination.REPLAN, reason="input_lost", budget=budget,
            activity_permit=permit,
            recovery_identity=RecoveryIdentity(
                self.frame.body.sequence_id, "route/input-lost",
            ))
        result = self.resolve(route=self.route, action_index=0)
        self.assertEqual(result.action.value, "resolve_recovery")
        self.assertEqual(self.coordinator.stop_request.request_id, "route/input-lost")
        self.assertEqual(budget.total_recovery_starts, 1)

    def test_replaced_request_waits_for_incumbent_release(self):
        result = self.resolve(route=self.route, action_index=0, current_request_id="new-request")
        self.assertEqual(result.action.value, "wait_for_route_release")

    def test_pending_route_waits_for_runtime_selected_input(self):
        result = self.resolve(route=self.route, action_index=0, pending_route_id="candidate")
        self.assertEqual(result.action.value, "wait_for_route_input")

    def test_otherwise_replans_from_current_body(self):
        self.assertEqual(self.resolve().action.value, "replan")

    def test_missing_stale_foreign_or_other_owner_evidence_cannot_choose_exit(self):
        variants = (None, replace(self.handoff, observation_sequence_id=0),
            replace(self.handoff, world_session=WorldSessionId("foreign")),
            replace(self.handoff, owner_id="other-probe"),
            replace(self.handoff, disposition=HandoffDisposition.RETAIN))
        for handoff in variants:
            with self.subTest(handoff=handoff):
                self.assertIsNone(self.resolve(handoff=handoff))

    def test_ready_completion_keeps_body_and_information_owner_records_grant(self):
        self.probe.state = LandingEdgeProbeState.READY
        self.probe.route_id = self.route.route_id
        self.probe.route_revision = self.route.route_revision
        self.probe.action_index = 0
        self.probe.acquisition_id = "landing-acquisition"
        self.probe.evidence_sequence_id = self.frame.body.sequence_id
        outcome = self.controller.outcome(self.frame)
        information = InformationAcquisitionState()
        information.complete_probe(outcome)
        result = self.resolve(route=self.route, action_index=0, handoff=None)
        self.assertEqual(result.action.value, "continue_route")
        self.assertEqual(information.completed_grant.acquisition_id, "landing-acquisition")
        self.assertTrue(self.probe.owned)
        self.assertIsNone(self.controller.outcome(replace(self.frame,
            session=WorldSessionId("foreign"))))

    def test_foreign_or_stale_anchor_does_not_release_probe(self):
        # Isolate the controller's evidence gate from the physics proof.
        with patch.object(LandingEdgeProbe, "stop_ready", return_value=True):
            for anchor in (None, replace(self.anchor, session=WorldSessionId("foreign")),
                           replace(self.anchor, observation_sequence_id=0)):
                with self.subTest(anchor=anchor):
                    decision = self.controller.decide(self.frame, self.ledger, anchor)
                    self.assertIs(decision.handoff.disposition, HandoffDisposition.RETAIN)
                    self.assertTrue(self.probe.owned)
            decision = self.controller.decide(self.frame, self.ledger, self.anchor)
            self.assertIs(decision.handoff.disposition, HandoffDisposition.QUIESCENT)
            self.assertTrue(self.probe.owned, "only the supervisor can retire the owner")

    def test_ready_fact_cannot_override_accepted_ending(self):
        self.probe.state = LandingEdgeProbeState.READY
        self.probe.route_id = self.route.route_id
        self.probe.route_revision = self.route.route_revision
        self.probe.action_index = 0
        self.coordinator.request_recovery(request_id="end", destination=HandoffDestination.FAIL,
            reason="formal_failure", budget=RetryLedger("goal"))
        self.assertIsNone(self.resolve(route=self.route, action_index=0, handoff=None))
        self.assertTrue(self.coordinator.accepted_ending)


if __name__ == "__main__":
    unittest.main()

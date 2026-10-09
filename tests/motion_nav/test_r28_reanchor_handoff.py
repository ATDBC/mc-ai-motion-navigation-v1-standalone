"""The formal driver leaves re-anchoring with its one typed handoff owner."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.body_control import HandoffDisposition, HandoffEvidence, StopCause
from mc2p.motion_nav.navigation_handoff import HandoffDestination, NavigationHandoffCoordinator
from mc2p.motion_nav.planning_coordinator import PlanningAttemptPermitKind
from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionState
from mc2p.motion_nav.action_route_executor import ActionRouteState
from mc2p.motion_nav.landing_edge_probe import LandingEdgeProbe, LandingEdgeProbeState
from mc2p.motion_nav.online_motion import InputApplicationLedger
from mc2p.motion_nav.navigation_lifecycle import NavigationTransitionAction
from mc2p.motion_nav.planning_coordinator import PlanningCoordinator
from mc2p.motion_nav.world_model import BlockGeometry
from tests.motion_nav import test_navigation_session as session_fixtures
from tests.motion_nav.test_b07_step_route import frame as navigation_frame
from mc2p.motion_nav.retry_ledger import RecoveryIdentity, RetryLedger, TaskDemandState
from mc2p.motion_nav.world_model import WorldSessionId
from tests.motion_nav.test_navigation_supervised_interruptions import scenario
from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker, gap_owner
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS


class ReanchorHandoffTests(unittest.TestCase):
    def test_reanchor_has_progress_permission_and_never_charges_retry(self):
        coordinator, budget = NavigationHandoffCoordinator(), RetryLedger("goal")
        self.assertTrue(coordinator.stage_reanchor(
            request_id="request/entry", cause=StopCause.ROUTE_REPLACED, reason="entry_changed"))
        self.assertFalse(coordinator.stage_reanchor(
            request_id="request/entry", cause=StopCause.ROUTE_REPLACED, reason="entry_changed"))
        self.assertEqual(budget.total_recovery_starts, 0)
        self.assertIs(coordinator.stop_request.planning_permit_kind, PlanningAttemptPermitKind.PROGRESS)

    def test_reanchor_rejects_missing_stable_request_identity(self):
        coordinator = NavigationHandoffCoordinator()
        with self.assertRaises(ContractViolation):
            coordinator.stage_reanchor(request_id=None, cause=StopCause.ROUTE_REPLACED,
                                       reason="entry_changed")
        self.assertIsNone(coordinator.stop_request)

    def test_missing_current_support_selects_information_without_starting_retry(self):
        _, frame, _, _, _ = gap_owner(DeferredMotionWorker())
        coordinator = NavigationHandoffCoordinator()
        coordinator.stage_reanchor(request_id="request/entry", cause=StopCause.ROUTE_REPLACED,
                                   reason="entry_changed")
        handoff = HandoffEvidence("route/old", frame.session, HandoffDisposition.QUIESCENT,
                                  frame.body.sequence_id, 20, MovementV1(), "supported")
        resolution = coordinator.advance(frame, handoff=handoff, goal_ready=True, start_ready=False,
            missing_cells=((0, 63, 0),), unavailable_reason="current_surface_unavailable")
        self.assertIs(resolution.destination, HandoffDestination.WAIT_FOR_INFORMATION)
        self.assertEqual(resolution.missing_cells, ((0, 63, 0),))
        self.assertIs(resolution.planning_permit_kind, PlanningAttemptPermitKind.PROGRESS)
        self.assertIsNone(coordinator.stop_request)

    def test_true_retry_keeps_its_permission_when_latest_goal_is_staged(self):
        from tests.motion_nav.test_navigation_handoff import _goal as pending_goal
        coordinator, budget = NavigationHandoffCoordinator(), RetryLedger("goal")
        permit = coordinator.observe_task_activity(
            budget=budget, observation_sequence=1,
            demand_state=TaskDemandState.UNMET,
        )
        coordinator.request_recovery(request_id="route/deviation", destination=HandoffDestination.REPLAN,
                                     reason="needs_replan", budget=budget,
                                     activity_permit=permit,
                                     recovery_identity=RecoveryIdentity(
                                         1, "route/deviation",
                                     ))
        coordinator.stage_goal(pending_goal(2), StopCause.GOAL_REVISED, "revision-2")
        coordinator.stage_goal(pending_goal(3), StopCause.GOAL_REVISED, "revision-3")
        self.assertEqual(coordinator.stop_request.request_id, "route/deviation")
        self.assertIs(coordinator.stop_request.planning_permit_kind, PlanningAttemptPermitKind.RETRY)
        self.assertEqual(budget.total_recovery_starts, 1)

    def test_clearing_a_waiting_goal_does_not_cancel_an_active_reanchor(self):
        coordinator = NavigationHandoffCoordinator()
        coordinator.stage_reanchor(request_id="request/entry", cause=StopCause.ROUTE_REPLACED,
                                   reason="entry_changed")
        request = coordinator.stop_request
        coordinator.clear_waiting_goal()
        self.assertIs(coordinator.stop_request, request)

    def test_repeated_airborne_revision_keeps_latest_goal_and_rejects_invalid_release(self):
        checked, request_ids = [], []
        def revise(context, revision, target):
            before = context.diagnostics.recovery_total_starts
            context.goal_position = target
            context.goal_state = _goal(target, context.risk_policy_id)
            self.assertTrue(context.driver.replace_goal("goal", revision, context.goal_state, context.clock[0]))
            coordinator = context.session._handoff
            request = coordinator.stop_request
            self.assertIsNotNone(request)
            self.assertIs(request.planning_permit_kind, PlanningAttemptPermitKind.TASK_UPDATE)
            self.assertEqual(context.diagnostics.recovery_total_starts, before)
            request_ids.append(request.request_id)
            frame = context.session._frame
            evidence = HandoffEvidence("route/old", frame.session, HandoffDisposition.QUIESCENT,
                                       frame.body.sequence_id, context.backend.movement_tick,
                                       MovementV1(), "release")
            for invalid in (replace(evidence, observation_sequence_id=frame.body.sequence_id - 1),
                            replace(evidence, world_session=WorldSessionId("other-world")),
                            replace(evidence, disposition=HandoffDisposition.RETAIN)):
                self.assertIsNone(coordinator.advance(frame, handoff=invalid, goal_ready=True,
                    start_ready=True, missing_cells=(), unavailable_reason="unavailable"))
                self.assertIs(coordinator.stop_request, request)
            checked.append(context.backend.movement_tick)
        events = [Event("airborne-revision-2", lambda c: not c.backend.state.on_ground,
                        lambda c: revise(c, 2, (.5, 59., 3.5))),
                  Event("airborne-revision-3", lambda c: bool(checked) and not c.backend.state.on_ground,
                        lambda c: revise(c, 3, (.5, 59., 5.5)))]
        result = run(replace(scenario("direct_drop_5_budget_2"), events=events,
                             max_ticks=180, perturbations=Perturbations()))
        self.assertEqual(len(checked), 2)
        self.assertEqual(request_ids[0], request_ids[1])
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertFalse(result.violations, result.violations)
        self.assertTrue(all(row["controller_ids"] for row in result.trace if not row["on_ground"]))
        self.assertTrue(any(row["planning_submissions"] and row["goal_revision"] == 3
                            for row in result.trace))
        self.assertFalse(result.trace[-1]["source_bound"])

    def test_revision_during_probe_stop_is_owned_without_recovery_charge(self):
        revised = []
        def revise(context, revision, target):
            before = context.diagnostics.recovery_total_starts
            context.goal_state = _goal(target, context.risk_policy_id)
            context.goal_position = target
            self.assertTrue(context.driver.replace_goal("goal", revision, context.goal_state, context.clock[0]))
            request = context.session._handoff.stop_request
            self.assertIsNotNone(request)
            self.assertIs(request.planning_permit_kind, PlanningAttemptPermitKind.TASK_UPDATE)
            self.assertEqual(context.diagnostics.recovery_total_starts, before)
            revised.append(context.tick)
        events = [Event("probe-revision-2", lambda c: c.session._edge_probe is not None
                        and c.session._edge_probe.active and c.tick >= 12,
                        lambda c: revise(c, 2, (.5, 64., .5))),
                  Event("probe-stopping-revision-3", lambda c: bool(revised)
                        and c.session._edge_probe is not None
                        and c.session._edge_probe.state.value == "stopping",
                        lambda c: revise(c, 3, (.5, 64., 1.5)))]
        result = run(replace(scenario("direct_drop_2"), events=events, max_ticks=220,
                             perturbations=Perturbations()))
        self.assertEqual(len(revised), 2)
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertFalse(result.violations, result.violations)
        self.assertEqual(result.recovery_failures, 0)
        self.assertFalse(result.trace[-1]["source_bound"])

    def test_normal_revision_and_continuous_transfer_do_not_charge_recovery(self):
        base = next(s for s in SCENARIOS if s.name == "flat_walk")
        def revise(context):
            context.goal_position = (.5, 64., 6.5)
            context.goal_state = _goal(context.goal_position, context.risk_policy_id)
            self.assertTrue(context.driver.replace_goal("goal", 2, context.goal_state, context.clock[0]))
        result = run(replace(base, perturbations=Perturbations(),
            events=[Event("normal-revision", lambda c: c.tick >= 14, revise)]))
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertEqual(result.recovery_failures, 0)
        self.assertFalse(result.violations, result.violations)
        self.assertTrue(any(row["body_handoff"] is not None
                            and row["body_handoff"]["disposition"] == "transferable"
                            for row in result.trace))

    def test_missing_support_readiness_on_formal_release_selects_typed_information_wait(self):
        original = NavigationSession._surface_for_body
        original_advance = NavigationHandoffCoordinator.advance
        delayed, waiting = [], []
        contexts = []
        def missing_once(session, frame):
            if delayed and contexts[-1].tick == delayed[0]:
                return None, ((0, 58, 3),)
            handoff = session._supervisor.last_handoff
            if (session._handoff.pending_goal is not None and frame.body.is_on_ground
                    and not session.has_owned_body_control and not delayed
                    and handoff is not None and handoff.disposition is HandoffDisposition.QUIESCENT
                    and handoff.observation_sequence_id == frame.body.sequence_id):
                # Inject one unavailable domain response; release permission still
                # comes from the unmodified supervisor and actual input ledger.
                context = contexts[-1]
                delayed.append(context.tick)
                context.backend.perturbations.world_edits[context.backend.movement_tick + 1] = {
                    (0, 58, 3): "minecraft:grass_block",
                }
                return None, ((0, 58, 3),)
            return original(frame)
        def advance(owner, frame, **facts):
            result = original_advance(owner, frame, **facts)
            if result is not None and result.destination is HandoffDestination.WAIT_FOR_INFORMATION:
                waiting.append(contexts[-1].tick)
                self.assertIn((0, 58, 3), result.missing_cells)
                self.assertFalse(contexts[-1].session.diagnostics.planning_work_owned)
            return result
        def revise(context):
            context.goal_position = (.5, 59., 5.5)
            context.goal_state = _goal(context.goal_position, context.risk_policy_id)
            self.assertTrue(context.driver.replace_goal("goal", 2, context.goal_state, context.clock[0]))
        def step(context):
            contexts[:] = [context]
            context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
        case = replace(scenario("direct_drop_5_budget_2"), perturbations=Perturbations(),
            events=[Event("airborne-revision", lambda c: not c.backend.state.on_ground, revise)])
        with patch.object(NavigationSession, "_surface_for_body", missing_once), \
                patch.object(NavigationHandoffCoordinator, "advance", advance):
            result = run(case, control_step=step)
        self.assertTrue(delayed)
        self.assertTrue(waiting, (delayed, result.outcome, result.reason,
                                 [(row["tick"], row["session_state"]) for row in result.trace
                                  if delayed and delayed[0] <= row["loop_tick"] <= delayed[0] + 3]))
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertFalse(result.violations, result.violations)
        self.assertTrue(any(row["planning_submissions"] and row["loop_tick"] >= delayed[0]
                            for row in result.trace))


class ReanchorConsumptionTests(unittest.TestCase):
    def owner(self):
        class CountingPlanner(session_fixtures._InlinePlanner):
            def __init__(self):
                super().__init__()
                self.submissions = 0
            def submit_surface_snapshot(self, *args, **kwargs):
                self.submissions += 1
                return super().submit_surface_snapshot(*args, **kwargs)
        world = session_fixtures._known_world({
            (x, 0, 0): BlockGeometry.full_cube("minecraft:stone") for x in range(4)
        })
        planner = CountingPlanner()
        session = NavigationSession("reanchor-consumption", session_fixtures.NavigationSessionTests().profiles(),
                                    planner_worker=planner, clock_ns=lambda: 1_000_000_000)
        session.bind_source(session_fixtures._source())
        session.start_goal("task", 1, session_fixtures._goal((3.5, 1., .5)),
                           navigation_frame(world, 0, (.5, 1., .5)))
        session.propose(navigation_frame(world, 1, (.5, 1., .5)), None, 2_000_000_000)
        self.assertIsNotNone(session._executor)
        return session, world, planner

    def decide(self, session, world, state, reason):
        session_fixtures._replace_route_executor(
            session, session_fixtures._RepeatingRecoveryExecutor(state, reason))
        current = navigation_frame(world, 2, (.5, 1., .5))
        return session.propose(current, session_fixtures._ground_anchor(current), 2_000_000_000,
                               input_ledger=InputApplicationLedger())

    def test_free_reanchor_does_not_replace_old_input_lost_with_new_work(self):
        session, world, planner = self.owner()
        sequence, submissions = session._request.sequence, planner.submissions
        self.assertTrue(session._wait_for_active_terminal((), "waiting_for_old_body"))
        proposal = self.decide(session, world, ActionRouteState.INPUT_LOST, "old_input_unconfirmed")
        self.assertIs(proposal.report.state, NavigationSessionState.FAILED)
        self.assertEqual(proposal.report.reason, "old_input_unconfirmed")
        self.assertEqual(session._request.sequence, sequence)
        self.assertEqual(planner.submissions, submissions)
        self.assertEqual(session.diagnostics.recovery_total_starts, 0)

    def test_free_reanchor_does_not_preempt_probe_action_failure(self):
        for state in (ActionRouteState.FAILED, ActionRouteState.BLOCKED, ActionRouteState.UNSUPPORTED):
            with self.subTest(state=state):
                session, world, planner = self.owner()
                sequence, submissions = session._request.sequence, planner.submissions
                self.assertTrue(session._wait_for_active_terminal((), "waiting_for_old_body"))
                probe = LandingEdgeProbe("task", 1, (0, -1, 1), 0)
                probe.state = LandingEdgeProbeState.READY
                session._edge_probe = probe
                proposal = self.decide(session, world, state, "old_motion_failed")
                self.assertIs(proposal.report.state, NavigationSessionState.STOPPING)
                self.assertTrue(session._handoff.ending)
                self.assertIs(session._handoff.stop_request.destination, HandoffDestination.FAIL)
                self.assertEqual(session._handoff.stop_request.reason, "old_motion_failed")
                self.assertIs(probe.stop_cause, StopCause.MOTION_UNSOLVABLE)
                self.assertTrue(probe.owned)
                self.assertEqual(session._request.sequence, sequence)
                self.assertEqual(planner.submissions, submissions)

    def test_accepted_cancel_and_close_still_win_over_input_lost(self):
        for ending, expected in (("cancel", NavigationSessionState.CANCELLED),
                                 ("close", NavigationSessionState.CLOSED)):
            with self.subTest(ending=ending):
                session, world, planner = self.owner()
                sequence, submissions = session._request.sequence, planner.submissions
                session._wait_for_active_terminal((), "waiting_for_old_body")
                if ending == "cancel":
                    session.cancel("accepted_cancel")
                else:
                    session.close()
                proposal = self.decide(session, world, ActionRouteState.INPUT_LOST, "old_input_unconfirmed")
                self.assertIs(proposal.report.state, expected)
                self.assertEqual(session._request.sequence, sequence)
                self.assertEqual(planner.submissions, submissions)

    def test_paid_recovery_keeps_its_existing_priority(self):
        session, world, _ = self.owner()
        sequence = session._request.sequence
        permit = session._handoff.activity_permit
        self.assertIsNotNone(permit)
        session._handoff.request_recovery(request_id="route/deviation", destination=HandoffDestination.REPLAN,
                                         reason="old_recovery", budget=session._retry_ledger,
                                         activity_permit=permit,
                                         recovery_identity=RecoveryIdentity(
                                             permit.observation_sequence,
                                             "route/deviation",
                                         ))
        proposal = self.decide(session, world, ActionRouteState.INPUT_LOST, "old_input_unconfirmed")
        self.assertIs(proposal.report.state, NavigationSessionState.PLANNING)
        self.assertGreater(session._request.sequence, sequence)
        self.assertEqual(session.diagnostics.recovery_total_starts, 1)

    def test_formal_pending_goal_consumes_original_permission_and_event_identity(self):
        for kind in (PlanningAttemptPermitKind.RETRY, PlanningAttemptPermitKind.TASK_UPDATE,
                     PlanningAttemptPermitKind.PROGRESS):
            with self.subTest(kind=kind):
                requests, revisions, permits = [], [], []
                event_id = "route/" + kind.value
                expected_failures = int(kind is PlanningAttemptPermitKind.RETRY)
                def begin_recovery(context):
                    session = context.session
                    if kind is PlanningAttemptPermitKind.RETRY:
                        permit = session._handoff.activity_permit
                        self.assertIsNotNone(permit)
                        accepted = session._handoff.request_recovery(
                            request_id=event_id, destination=HandoffDestination.REPLAN,
                            reason="requested_recovery", budget=session._retry_ledger,
                            activity_permit=permit,
                            recovery_identity=RecoveryIdentity(
                                permit.observation_sequence, event_id,
                            ))
                    else:
                        accepted = session._handoff.stage_reanchor(request_id=event_id,
                            cause=StopCause.ROUTE_REPLACED, reason="requested_reanchor", planning_permit_kind=kind)
                    self.assertTrue(accepted)
                    session._supervisor.request_route_stop(StopCause.ROUTE_REPLACED)
                    session._transition(NavigationTransitionAction.BEGIN_STOPPING, "requested_body_exit")
                    requests.append(context.tick)
                def revise_twice(context):
                    for revision, position in ((2, (.5, 64., 6.5)), (3, (.5, 64., 8.5))):
                        goal = _goal(position, context.risk_policy_id)
                        self.assertTrue(context.driver.replace_goal("goal", revision, goal, context.clock[0]))
                        context.goal_state, context.goal_position = goal, position
                    self.assertEqual(context.session._handoff.stop_request.request_id, event_id)
                    self.assertIs(context.session._handoff.stop_request.planning_permit_kind, kind)
                    revisions.append(context.tick)
                original = PlanningCoordinator.begin
                def record_begin(owner, request, current, **options):
                    original(owner, request, current, **options)
                    if request.goal_revision == 3:
                        permits.append((owner._active_permit, owner.request.work_identity))
                base = next(s for s in SCENARIOS if s.name == "flat_walk")
                events = [Event("begin-recovery", lambda c: c.tick >= 10 and c.session.active_route is not None,
                                begin_recovery),
                          Event("revise-stopping", lambda c: bool(requests)
                                and c.session.report.state is NavigationSessionState.STOPPING,
                                revise_twice)]
                with patch.object(PlanningCoordinator, "begin", record_begin):
                    result = run(replace(base, events=events, perturbations=Perturbations(), max_ticks=220))
                self.assertTrue(revisions)
                self.assertTrue(permits)
                self.assertTrue(all(permit.kind is kind and permit.source_event_id == event_id
                                    for permit, _ in permits))
                self.assertTrue(all(identity is not None for _, identity in permits))
                self.assertEqual(result.recovery_failures, expected_failures)
                self.assertEqual(result.outcome, "success", result.reason)
                self.assertFalse(result.violations, result.violations)
                self.assertEqual(result.trace[-1]["goal_revision"], 3)
                self.assertTrue(any(row["planning_submissions"] and row["goal_revision"] == 3
                                    for row in result.trace))


if __name__ == "__main__":
    unittest.main()

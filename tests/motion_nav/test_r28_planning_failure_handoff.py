"""Replacement failures through Runtime, workers and verified body release."""
from dataclasses import replace
from mc2p.motion_nav.async_work import ComputationInvalidationCause
import unittest
from unittest.mock import patch

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.motion_nav.known_map_planner import (
    PlanningBlocker, PlanningBlockerKind, PlanningFrontierKind,
    PlanningInformationNeed, SurfacePlanningStatus,
)
from mc2p.motion_nav.planning_coordinator import InformationOutcome, PlanningAttemptPermitKind, PlanningFailure
from mc2p.motion_nav.navigation_handoff import HandoffDestination, NavigationHandoffCoordinator
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.retry_ledger import RecoveryIdentity, RetryLedger, TaskDemandState
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.body_control import StopCause
from tests.motion_nav.test_action_continuity_formal import _gap_case
from tests.sim.runner import Event, InlinePlannerWorker, _goal, run
from tests.sim.scenarios import SCENARIOS


class _FailReplacementWorker(InlinePlannerWorker):
    def __init__(self, *, information=False, grounded=False, before_delivery=None):
        super().__init__()
        self.context = None
        self.failure_at = None
        self.failure_observation = None
        self.information = information
        self.grounded = grounded
        self.deliver_after_observation = 0
        self.before_delivery = before_delivery

    def poll_latest(self):
        if self._job is not None and self._job.request.goal_revision == 2:
            if self.context is None:
                return None
            if self.context.session._frame.body.sequence_id < self.deliver_after_observation:
                return None
            if not self.grounded and self.context.backend.state.on_ground:
                return None
            if self.before_delivery is not None:
                self.before_delivery(self.context)
            result = super().poll_latest()
            self.failure_at = self.context.tick
            self.failure_observation = self.context.session._frame.body.sequence_id
            if self.information:
                blocker = PlanningBlocker((20, 69, 9), PlanningBlockerKind.CLEARANCE,
                                          "test-unavailable", PlanningFrontierKind.SEARCH_EDGE,
                                          "test-frontier")
                need = PlanningInformationNeed(result.world_session, "test-snapshot",
                                               result.request_id, result.goal_id,
                                               result.goal_revision, 1, (blocker,), False)
                return replace(result, status=SurfacePlanningStatus.NO_KNOWN_ROUTE,
                               information_need=need)
            return replace(result, status=SurfacePlanningStatus.NO_ROUTE_WITHIN_COMPLETE_SCOPE,
                           information_need=None)
        return super().poll_latest()


class PlanningFailureHandoffFormalTests(unittest.TestCase):
    def run_failure(self, followup=None, *, information=False, before_delivery=None,
                    before_information_failure=None):
        worker = _FailReplacementWorker(information=information, before_delivery=before_delivery)
        seen = []

        def revise(context):
            worker.context = context
            target = (.55, 64., 7.5)
            context.goal_position = target
            context.goal_state = _goal(target, context.risk_policy_id)
            self.assertTrue(context.driver.replace_goal("goal", 2, context.goal_state,
                                                       context.clock[0]))

        def inspect(context):
            worker.context = context
            context.driver.tick(BehaviorProfileV0(),
                                context.clock[0] + 500_000_000)
            if worker.failure_at == context.tick:
                if information:
                    planning = context.session._planning_coordinator
                    update = planning.current_information_update
                    if before_information_failure is not None:
                        before_information_failure(context, update)
                    failure = planning.reconcile_information(
                        update, context.session._frame, edge_probe=None,
                        outcomes=tuple((blocker.blocker_key, InformationOutcome.TIMED_OUT)
                                       for blocker in update.information_need.blockers), current_scope=planning._request_ledger.current_computation_scope)
                    context.session._fail_planning_or_preserve_incumbent(failure.failure)
                stop = context.session._handoff.stop_request
                self.assertIsNotNone(stop, "replacement failure must be owned by handoff")
                self.assertEqual(stop.destination.value, "fail")
                self.assertEqual(stop.planning_failure.request_id,
                                 context.session._request.request_id)
                self.assertFalse(context.session._planning_coordinator.has_owned_work)
                seen.append((context.tick, context.session._planning_coordinator._terminal_update))
                if followup is not None:
                    followup(context, seen[-1][1])

        case = replace(_gap_case(), events=[Event("replacement", lambda c: c.tick >= 8, revise)],
                       max_ticks=160, expect="failed")
        result = run(case, planner_factory=lambda: worker, control_step=inspect)
        self.assertEqual(len(seen), 1, "failure must occur while incumbent is airborne")
        self.assertFalse(result.violations, result.violations)
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertEqual(result.recovery_failures, 0)
        airborne = [row for row in result.trace if row["tick"] >= seen[0][0]
                    and not row["on_ground"]]
        self.assertTrue(airborne)
        self.assertTrue(all(row["source_bound"] for row in airborne))
        self.assertTrue(all(row["session_state"] not in {"failed", "cancelled", "closed"}
                            for row in airborne))
        return result

    def test_airborne_no_route_waits_for_verified_release(self):
        result = self.run_failure()
        self.assertEqual((result.outcome, result.reason),
                         ("failed", "no_route_within_complete_scope"))

    def test_unpublished_failure_can_be_superseded_by_newer_goal(self):
        def revise(context, update):
            target = (.55, 64., 8.5)
            context.goal_position = target
            context.goal_state = _goal(target, context.risk_policy_id)
            self.assertTrue(context.driver.replace_goal("goal", 3, context.goal_state,
                                                       context.clock[0]))
        result = self.run_failure(revise)
        self.assertEqual(result.outcome, "success", result.reason)

    def test_cancel_supersedes_unpublished_failure_and_rejects_revision(self):
        def cancel(context, update):
            context.session.cancel("cancel-after-planning-failure")
            self.assertFalse(context.driver.replace_goal("goal", 3, _goal((.55, 64., 8.5)),
                                                        context.clock[0]))
        result = self.run_failure(cancel)
        self.assertEqual((result.outcome, result.reason),
                         ("cancelled", "cancel-after-planning-failure"))

    def test_repeated_failure_notification_is_idempotent(self):
        def repeat(context, update):
            stop = context.session._handoff.stop_request
            context.session._fail_planning_or_preserve_incumbent(update.failure)
            self.assertIs(context.session._handoff.stop_request, stop)
        self.assertEqual(self.run_failure(repeat).outcome, "failed")

    def test_superseded_failure_cannot_stop_newer_goal(self):
        def revise_then_old_failure(context, update):
            target = (.55, 64., 8.5)
            context.goal_position = target
            context.goal_state = _goal(target)
            self.assertTrue(context.driver.replace_goal("goal", 3, context.goal_state,
                                                       context.clock[0]))
            current = context.session._handoff.stop_request
            context.session._fail_planning_or_preserve_incumbent(update.failure)
            self.assertIs(context.session._handoff.stop_request, current)
        result = self.run_failure(revise_then_old_failure)
        self.assertEqual(result.outcome, "success", result.reason)

    def test_close_and_formal_failure_remain_immutable(self):
        for destination in (HandoffDestination.CLOSE, HandoffDestination.FAIL):
            with self.subTest(destination=destination):
                def end(context, update):
                    context.session._request_ending(destination, StopCause.CANCELLED,
                                                    "formal-end-after-failure")
                    current = context.session._handoff.stop_request
                    context.session._fail_planning_or_preserve_incumbent(update.failure)
                    self.assertIs(context.session._handoff.stop_request, current)
                    self.assertFalse(context.driver.replace_goal("goal", 3,
                                     _goal((.55, 64., 8.5)), context.clock[0]))
                result = self.run_failure(end)
                self.assertEqual(result.trace[-1]["session_state"],
                                 "closed" if destination is HandoffDestination.CLOSE else "failed")
                self.assertEqual(result.reason, "closed" if destination is HandoffDestination.CLOSE
                                 else "formal-end-after-failure")

    def test_registered_retry_identity_survives_failure_then_new_goal(self):
        from tests.motion_nav.test_navigation_handoff import _goal as pending_goal
        from tests.motion_nav.test_planning_coordinator import (
            PlanningCoordinatorTests, _permit, _world, _request,
        )
        from tests.motion_nav.test_b07_step_route import frame
        from mc2p.motion_nav.support_surfaces import query_support_surfaces
        world = _world(unknown=frozenset({(0, 0, 0)}))
        request = _request(world)
        planning = PlanningCoordinatorTests().coordinator()
        current = frame(world, 0, query_support_surfaces(
            world.view(), -1, 0, 1, 1).surfaces[0].position)
        planning.begin(request, current, permit=_permit(), state_anchor=None,
                       remaining_damage_budget=request.damage_budget)
        selected = planning.advance(current, state_anchor=None, edge_probe=None,
                                    remaining_damage_budget=request.damage_budget, current_scope=planning._request_ledger.current_computation_scope)
        failed = planning.reconcile_information(selected, current, outcomes=tuple(
            (blocker.blocker_key, InformationOutcome.TIMED_OUT)
            for blocker in selected.information_need.blockers), current_scope=planning._request_ledger.current_computation_scope)
        handoff, budget = NavigationHandoffCoordinator(), RetryLedger("goal")
        activity = handoff.observe_task_activity(
            budget=budget, observation_sequence=current.body.sequence_id,
            demand_state=TaskDemandState.UNMET,
        )
        handoff.request_recovery(request_id="route/deviation", destination=HandoffDestination.REPLAN,
                                 reason="needs-replan", budget=budget,
                                 activity_permit=activity,
                                 recovery_identity=RecoveryIdentity(
                                     current.body.sequence_id, "route/deviation",
                                 ))
        self.assertTrue(handoff.stage_planning_failure(failed.failure, request, planning=planning))
        handoff.stage_goal(pending_goal(2), StopCause.GOAL_REVISED, "new-goal")
        self.assertEqual(handoff.stop_request.request_id, "route/deviation")
        self.assertIs(handoff.stop_request.planning_permit_kind, PlanningAttemptPermitKind.RETRY)
        self.assertEqual(budget.total_recovery_starts, 1)

    def test_task_a_missing_reanchor_support_still_waits_for_current_body(self):
        seen = []

        def control(context):
            context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
            if seen or context.backend.state.position[1] <= 65.01:
                return
            session = context.session
            session.observe(session._adapter.latest_frame, ())
            session._reissue_request_from_current(session._frame, "test-restart-in-air",
                computation_invalidation=ComputationInvalidationCause.NEW_STATE_ANCHOR)
            stop = session._handoff.stop_request
            self.assertIsNotNone(stop)
            self.assertIs(stop.destination, HandoffDestination.REPLAN)
            self.assertIsNone(stop.planning_failure)
            self.assertIs(stop.planning_permit_kind, PlanningAttemptPermitKind.PROGRESS)
            seen.append(context.tick)

        result = run(replace(_gap_case(), max_ticks=160),
                     control_step=control)
        self.assertEqual(len(seen), 1)
        self.assertEqual(result.outcome, "success", result.reason)
        self.assertEqual(result.recovery_failures, 0)
        self.assertFalse(result.violations, result.violations)
        self.assertTrue(result.trace[-1]["on_ground"])

    def test_airborne_information_failure_keeps_incumbent(self):
        result = self.run_failure(information=True)
        self.assertEqual((result.outcome, result.reason),
                         ("failed", "information_frontier_unavailable"))

    def test_foreign_attempt_is_rejected_while_current_work_remains_active(self):
        seen = []

        def inject(context):
            session = context.session
            planning = session._planning_coordinator
            current, identity = planning.request, planning.work_identity
            previous = session._handoff.stop_request
            forged = PlanningFailure("foreign-attempt-failure", current.world_session,
                                     "foreign-owner/old-attempt", current.request_id,
                                     current.goal_revision)
            session._fail_planning_or_preserve_incumbent(forged)
            self.assertIs(session._handoff.stop_request, previous)
            self.assertTrue(planning.has_owned_work)
            self.assertIs(planning.work_identity, identity)
            seen.append(identity)

        result = self.run_failure(before_delivery=inject)
        self.assertEqual(len(seen), 1)
        self.assertEqual(result.reason, "no_route_within_complete_scope")

    def test_old_planning_attempt_is_rejected_after_information_transition(self):
        planning_attempts, information_attempts = [], []

        def remember(context):
            planning_attempts.append(context.session._planning_coordinator.work_identity.key)

        def inject(context, selected):
            session = context.session
            planning = session._planning_coordinator
            current, identity = planning.request, planning.work_identity
            information_attempts.append(identity.key)
            self.assertNotEqual(planning_attempts[-1], identity.key)
            previous = session._handoff.stop_request
            old = PlanningFailure("old-planning-attempt-failure", current.world_session,
                                  planning_attempts[-1], current.request_id, current.goal_revision)
            session._fail_planning_or_preserve_incumbent(old)
            self.assertIs(session._handoff.stop_request, previous)
            self.assertTrue(planning.has_owned_work)
            self.assertIs(planning.work_identity, identity)

        def repeat_valid_information_failure(context, update):
            self.assertEqual(update.failure.attempt_id, information_attempts[-1])
            previous = context.session._handoff.stop_request
            context.session._fail_planning_or_preserve_incumbent(update.failure)
            self.assertIs(context.session._handoff.stop_request, previous)

        result = self.run_failure(repeat_valid_information_failure, information=True,
                                  before_delivery=remember, before_information_failure=inject)
        self.assertEqual(result.reason, "information_frontier_unavailable")

    def test_retired_failure_rejects_wrong_attempt_and_accepts_diagnostic_reason(self):
        injected = []
        route_failure = NavigationSession._fail_planning_or_preserve_incumbent

        def intercept(session, failure):
            if not injected:
                planning = session._planning_coordinator
                self.assertFalse(planning.has_owned_work)
                previous = session._handoff.stop_request
                foreign = replace(failure, attempt_id="foreign-retired-attempt",
                                  reason="foreign-failure")
                route_failure(session, foreign)
                self.assertIs(session._handoff.stop_request, previous)
                injected.append(failure)
                failure = replace(failure, reason="current-failure-diagnostic")
            route_failure(session, failure)

        with patch.object(NavigationSession, "_fail_planning_or_preserve_incumbent", intercept):
            result = self.run_failure()
        self.assertEqual(len(injected), 1)
        self.assertEqual(result.reason, "current-failure-diagnostic")

    def test_failure_and_old_route_terminal_in_same_observation(self):
        worker = _FailReplacementWorker(grounded=True)
        worker.deliver_after_observation = 5
        publications, terminals = [], []
        finish = NavigationHandoffCoordinator.finish_ending
        step = ActionRouteExecutor.decide

        def record_finish(owner, **kwargs):
            publications.append(owner.stop_request.planning_failure)
            return finish(owner, **kwargs)

        def record_step(executor, frame, *args, **kwargs):
            decision = step(executor, frame, *args, **kwargs)
            if decision.state.value in {"complete", "cancelled"}:
                terminals.append(frame.body.sequence_id)
            return decision

        def revise(context):
            worker.context = context
            target = (.5, 64., 7.5)
            context.goal_state = _goal(target)
            context.goal_position = target
            self.assertTrue(context.driver.replace_goal("goal", 2, context.goal_state,
                                                       context.clock[0]))
            context.session._supervisor.request_route_stop(StopCause.GOAL_REVISED)

        def control(context):
            worker.context = context
            context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)

        case = replace(next(s for s in SCENARIOS if s.name == "flat_walk"),
                       events=[Event("replace-before-first-input", lambda c: c.tick >= 2,
                                     revise)], max_ticks=120, expect="failed")
        with patch.object(NavigationHandoffCoordinator, "finish_ending", record_finish), \
                patch.object(ActionRouteExecutor, "decide", record_step):
            result = run(case, planner_factory=lambda: worker, control_step=control)
        self.assertEqual((result.outcome, result.reason),
                         ("failed", "no_route_within_complete_scope"))
        self.assertFalse(result.violations, result.violations)
        self.assertEqual(len(publications), 1)
        self.assertIsNotNone(publications[0])
        self.assertTrue(terminals)
        self.assertIn(worker.failure_observation, terminals)
        self.assertEqual(result.recovery_failures, 0)


if __name__ == "__main__":
    unittest.main()

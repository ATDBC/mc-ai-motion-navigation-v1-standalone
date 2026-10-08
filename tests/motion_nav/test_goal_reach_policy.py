"""Public persistent goals use the same Runtime body and task owners."""
from dataclasses import replace
import inspect
import math
import unittest

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.goal_observation import ObservedGoalStatus
from mc2p.motion_nav.known_map_planner import PlanningRequest
from mc2p.motion_nav.navigation_owners import GoalRequestLedger
from mc2p.motion_nav.navigation_lifecycle import (
    NavigationSessionState, NavigationTransitionAction,
)
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.retry_ledger import (
    ProgressEvidence, ProgressKind, RecoveryBudgetKind, RecoveryBudgetPolicy,
    RecoveryLimitStatus, RetryLedger, TaskDemandState,
)
from tests.motion_nav.test_action_continuity_formal import _gap_case
from tests.sim.backend import Perturbations
from tests.sim.product_metrics import strict_trace
from tests.sim.runner import Event, Scenario, _goal, run
from tests.sim.scenarios import SCENARIOS, drop_ledge


def policy(name):
    from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
    return getattr(GoalReachPolicy, name)


class GoalReachPolicyFormalTests(unittest.TestCase):
    def flat(self):
        return replace(next(s for s in SCENARIOS if s.name == "flat_walk"),
                       perturbations=Perturbations(), max_ticks=240)

    def hold_and_cancel(self, case, *, next_goal=None, step=None):
        held = []
        revised = []
        owners = []

        def satisfied(context):
            report = context.session.report
            return (not report.terminal and report.observed_goal_status is ObservedGoalStatus.SATISFIED
                    and not context.session.has_owned_body_control)

        def hold(context):
            if satisfied(context):
                report, diagnostics = context.session.report, context.diagnostics
                held.append((context.tick, report.goal_revision))
                self.assertFalse(report.terminal)
                self.assertIs(report.reach_policy, policy("KEEP_ACTIVE_ON_REACH"))
                self.assertFalse(diagnostics.controller_ids)
                self.assertFalse(diagnostics.body_control_activities)
                self.assertFalse(diagnostics.active_waits)
                self.assertFalse(diagnostics.planning_work_owned)
                request = context.session.observation_request()
                self.assertFalse(request.air_positions)
                self.assertFalse(context.session._information.missing_cells)
                self.assertIsNone(context.session.planning_information_update)
                self.assertTrue(context.backend.state.on_ground)
                self.assertLessEqual(math.hypot(*context.backend.state.velocity_blocks_per_tick[::2]) * 20, .1)
            if step is None:
                context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
            else:
                step(context)

        def enough(context, revision):
            return satisfied(context) and sum(rev == revision for _, rev in held) >= 8

        def revise(context):
            before = (context.session._retry_ledger, context.session._risk_ledger,
                      context.diagnostics.recovery_total_starts,
                      context.diagnostics.damage_spent, context.diagnostics.risk_available_points)
            context.goal_position = next_goal
            context.goal_state = _goal(next_goal, context.risk_policy_id)
            self.assertTrue(context.driver.replace_goal("goal", 2, context.goal_state, context.clock[0]))
            self.assertIs(context.session._retry_ledger, before[0])
            self.assertIs(context.session._risk_ledger, before[1])
            self.assertEqual(context.diagnostics.recovery_total_starts, before[2])
            self.assertEqual(context.diagnostics.damage_spent, before[3])
            self.assertEqual(context.diagnostics.risk_available_points, before[4])
            owners.append(before)
            revised.append(context.tick)

        events = ([] if next_goal is None else [Event("revise-satisfied-goal", lambda c: enough(c, 1), revise)])
        final_revision = 1 if next_goal is None else 2
        events.append(Event("cancel-satisfied-goal", lambda c: enough(c, final_revision),
                            lambda c: c.session.cancel("persistent-goal-cancelled")))
        result = run(replace(case, events=events, expect="cancelled"),
                     reach_policy=policy("KEEP_ACTIVE_ON_REACH"), control_step=hold)
        self.assertEqual(result.outcome, "cancelled", result.reason)
        self.assertFalse(result.violations, result.violations)
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertTrue(any(event.startswith("cancel-satisfied-goal@") for event in result.events))
        for tick, _ in held:
            row = next(row for row in result.trace if row["loop_tick"] == tick)
            self.assertFalse(row["planning_submissions"])
            self.assertFalse(row["proposed_movement_intents"])
            self.assertFalse(any(row["sampled_input"].values()))
        if next_goal is not None:
            self.assertEqual(len(revised), 1)
            self.assertGreaterEqual(sum(rev == 2 for _, rev in held), 8)
        return result, owners

    def test_default_reach_completion_preserves_formal_trace(self):
        implicit = run(self.flat())
        explicit = run(self.flat(), reach_policy=policy("COMPLETE_ON_REACH"))
        self.assertEqual(implicit.outcome, "success", implicit.reason)
        self.assertEqual(explicit.outcome, "success", explicit.reason)
        self.assertEqual(strict_trace(implicit.trace), strict_trace(explicit.trace))

    def test_persistent_goal_releases_body_and_waits_then_cancels(self):
        self.hold_and_cancel(self.flat())

    def test_already_satisfied_task_never_builds_an_empty_route(self):
        case = self.flat()
        result, _ = self.hold_and_cancel(replace(case, goal=case.start))
        self.assertTrue(all(row["route_id"] is None for row in result.trace))
        self.assertTrue(all(not row["planning_submissions"] for row in result.trace))

    def test_revised_satisfied_goal_keeps_task_and_balances(self):
        self.hold_and_cancel(self.flat(), next_goal=(.5, 64., 4.5))

    def satisfied_cross_node_revision(self, *, hold_submitted_result=False):
        from mc2p.motion_nav.movement_transition import (
            GoalState, GoalSupport, MovementMode,
        )
        from mc2p.motion_nav.online_motion import InputApplicationLedger
        from mc2p.motion_nav.world_model import Aabb, BlockGeometry
        from tests.motion_nav import test_navigation_session as fixtures

        world = fixtures._known_world({
            (0, 0, z): BlockGeometry.full_cube("minecraft:stone")
            for z in range(12)
        })
        current = fixtures.frame(world, 1, (.5, 1., 5.159))
        half = 2.5 / math.sqrt(2.0)

        def hold_goal(z):
            return GoalState(
                Aabb(.5 - half, .9, z - half,
                     .5 + half, 1.1, z + half),
                GoalSupport.SOLID,
                frozenset({MovementMode.WALK}),
                frozenset({"standing"}),
                .6,
            )

        clock = [100_000_000]
        planner = fixtures._InlinePlanner(hold_first=hold_submitted_result)
        session = NavigationSession(
            "satisfied-cross-node",
            fixtures.NavigationSessionTests().profiles(),
            planner_worker=planner, motion_worker=fixtures._InlineMotionWorker(),
            clock_ns=lambda: clock[0],
        )
        session.bind_source(fixtures._source())
        session.start_goal(
            "goal", 24, hold_goal(6.0), current,
            task_id="persistent-task",
            reach_policy=policy("KEEP_ACTIVE_ON_REACH"),
        )
        self.assertIs(
            session.report.observed_goal_status,
            ObservedGoalStatus.SATISFIED,
        )
        self.assertFalse(session.diagnostics.planning_work_owned)
        self.assertTrue(session.update_goal("goal", 25, hold_goal(6.5)))
        self.assertIs(
            session.report.observed_goal_status,
            ObservedGoalStatus.SATISFIED,
        )
        coordinator = session._planning_coordinator
        self.assertIsNotNone(coordinator)
        self.assertTrue(coordinator.has_owned_work)
        self.assertFalse(planner.jobs)
        return (
            session, coordinator, planner, current, hold_goal, clock,
            InputApplicationLedger(), fixtures._ground_anchor(current),
        )

    def test_satisfied_cross_node_revision_retires_planning_and_can_leave_hold(self):
        (session, coordinator, planner, current, hold_goal, clock,
         ledger, anchor) = self.satisfied_cross_node_revision()
        identity = coordinator.work_identity
        before = session.diagnostics
        try:
            self.assertTrue(
                session._retain_satisfied_goal(current, ledger, anchor)
            )

            self.assertFalse(coordinator.has_owned_work)
            self.assertFalse(session.diagnostics.planning_work_owned)
            self.assertEqual(session.report.state.value, "executing")
            self.assertEqual(session.report.reason, "goal_state_satisfied")
            self.assertEqual(coordinator.local_attempt_failures, 0)
            self.assertEqual(
                session.diagnostics.recovery_total_starts,
                before.recovery_total_starts,
            )
            self.assertEqual(
                session.diagnostics.retry_total_failures,
                before.retry_total_failures,
            )
            self.assertIn(
                (identity, "finish", "goal_state_satisfied"),
                tuple(
                    (event.identity, event.operation, event.cause)
                    for event in coordinator.async_diagnostics.events
                ),
            )

            self.assertTrue(session.update_goal("goal", 26, hold_goal(9.5)))
            self.assertIs(
                session.report.observed_goal_status,
                ObservedGoalStatus.NOT_SATISFIED,
            )
            self.assertTrue(coordinator.has_owned_work)
            session.propose(
                current, anchor, clock[0] + 500_000_000,
                input_ledger=ledger,
            )
            self.assertGreater(planner.polls, 0)
            self.assertFalse(session.report.terminal)
        finally:
            session.close()

    def test_quiescent_satisfied_hold_leaves_stopping_for_active_idle(self):
        (session, _coordinator, _planner, current, _hold_goal, _clock,
         ledger, anchor) = self.satisfied_cross_node_revision()
        try:
            session._transition(
                NavigationTransitionAction.BEGIN_STOPPING,
                "route_release_waiting_for_evidence",
            )
            self.assertIs(session.report.state, NavigationSessionState.STOPPING)

            self.assertTrue(
                session._retain_satisfied_goal(current, ledger, anchor)
            )

            self.assertIs(session.report.state, NavigationSessionState.EXECUTING)
            self.assertEqual(session.report.reason, "goal_state_satisfied")
            self.assertFalse(session.has_owned_body_control)
            self.assertFalse(session.diagnostics.active_waits)
        finally:
            session.close()

    def test_satisfied_goal_retains_submitted_receipt_identity_and_history(self):
        (session, coordinator, planner, current, _hold_goal, _clock,
         ledger, anchor) = self.satisfied_cross_node_revision(
             hold_submitted_result=True,
         )
        identity = coordinator.work_identity
        try:
            session._advance_planning(current, anchor, ledger)
            self.assertEqual(
                coordinator.async_diagnostics.pending_planning_receipts,
                (identity,),
            )
            self.assertTrue(coordinator.has_owned_work)

            self.assertTrue(
                session._retain_satisfied_goal(current, ledger, anchor)
            )

            diagnostics = coordinator.async_diagnostics
            self.assertFalse(coordinator.has_owned_work)
            self.assertEqual(diagnostics.planning_work, ())
            self.assertEqual(
                diagnostics.pending_planning_receipts,
                (identity,),
            )
            self.assertIn(
                (identity, "finish", "goal_state_satisfied"),
                tuple(
                    (event.identity, event.operation, event.cause)
                    for event in diagnostics.events
                ),
            )
            self.assertEqual(coordinator.local_attempt_failures, 0)
            self.assertEqual(session.diagnostics.recovery_total_starts, 0)
            self.assertEqual(session.diagnostics.retry_total_failures, 0)
            self.assertFalse(session.report.terminal)
        finally:
            session.close()

    def test_real_five_block_drop_keeps_spent_risk_on_revision(self):
        case = Scenario("persistent-five-block-drop", drop_ledge(5), (.5, 64., .5),
                        (.5, 59., 4.5), damage_points=2., max_ticks=240)
        result, owners = self.hold_and_cancel(case, next_goal=(.5, 59., 3.5))
        self.assertEqual(owners[0][3:], (2., 0.))
        self.assertEqual(result.damage, 2.)

    def test_airborne_and_delayed_input_keep_original_body_owner(self):
        late_at = []
        def delay_first_verified(context):
            driver = context.driver
            deadline = context.clock[0] + 500_000_000
            proposals = driver.prepare_proposals(deadline)
            windows = [envelope.intent.movement_tick_window
                       for proposal in proposals for envelope in proposal.intents
                       if envelope.intent.movement_tick_window is not None]
            if windows and not late_at:
                late_at.append(windows[0].earliest_tick)
                context.backend.perturbations.late_ticks = frozenset(late_at)
            result = driver.runtime.control_frame(driver._task(deadline), BehaviorProfileV0(),
                                                  deadline, proposals=proposals)
            driver.adopt_result(result)
        result, _ = self.hold_and_cancel(replace(_gap_case(),
            perturbations=Perturbations(), max_ticks=220), step=delay_first_verified)
        self.assertTrue(late_at)
        self.assertIn(("late_input", late_at[0]), result.applied_perturbations)
        airborne = [row for row in result.trace if not row["on_ground"]]
        self.assertTrue(airborne)
        self.assertTrue(all(row["controller_ids"] for row in airborne))
        self.assertTrue(all(row["body_control_activities"] for row in airborne))

    def test_satisfied_goal_with_inflight_stop_keeps_body_until_confirmation(self):
        from mc2p.motion_nav.online_motion import InputApplicationStatus
        late_at, pending = [], []
        def delay_stop(context):
            driver, session = context.driver, context.session
            deadline = context.clock[0] + 500_000_000
            satisfied = session.report.observed_goal_status is ObservedGoalStatus.SATISFIED
            proposals = driver.prepare_proposals(deadline)
            movement = [envelope.intent.movement for proposal in proposals for envelope in proposal.intents
                        if envelope.intent.movement is not None]
            if satisfied and movement and not late_at:
                late_at.append(context.backend.movement_tick + 1)
                context.backend.perturbations.late_ticks = frozenset(late_at)
            result = driver.runtime.control_frame(driver._task(deadline), BehaviorProfileV0(),
                                                  deadline, proposals=proposals)
            driver.adopt_result(result)
            if late_at and context.backend.movement_tick == late_at[0]:
                records = [record for record in driver.runtime.input_ledger.snapshot()
                           if record.status in {InputApplicationStatus.IN_FLIGHT,
                                                InputApplicationStatus.PARTIALLY_APPLIED}]
                self.assertTrue(records)
                self.assertTrue(session.has_owned_body_control)
                self.assertFalse(session.report.terminal)
                pending.append(context.tick)
        result, _ = self.hold_and_cancel(self.flat(), step=delay_stop)
        self.assertTrue(pending)
        self.assertIn(("late_input", late_at[0]), result.applied_perturbations)

    def test_task_reach_policy_cannot_change_on_revision(self):
        keep = policy("KEEP_ACTIVE_ON_REACH")
        request = PlanningRequest(1, "r1", "goal", 1, "world", (0, 1, 0), (0, 1, 1),
                                  reach_policy=keep)
        owner = GoalRequestLedger()
        owner.accept(request)
        with self.assertRaises(ContractViolation):
            owner.advance("session", reach_policy=policy("COMPLETE_ON_REACH"))
        self.assertIs(owner.request, request)
        self.assertIs(owner.reach_policy, keep)
        self.assertNotIn("reach_policy", inspect.signature(NavigationSession.update_goal).parameters)

    def test_task_start_selects_recovery_policy_once(self):
        complete = self.flat()
        complete_context = None

        def capture_complete(context):
            nonlocal complete_context
            complete_context = context
            context.session.cancel("captured")

        run(replace(complete, events=[Event("capture", lambda c: c.tick >= 1,
                                           capture_complete)], expect="cancelled"))
        self.assertIs(
            complete_context.session._retry_ledger.policy.kind,
            RecoveryBudgetKind.FINITE,
        )

        keep_context = None

        def capture_keep(context):
            nonlocal keep_context
            keep_context = context
            context.session.cancel("captured")

        run(replace(complete, events=[Event("capture", lambda c: c.tick >= 1,
                                           capture_keep)], expect="cancelled"),
            reach_policy=policy("KEEP_ACTIVE_ON_REACH"))
        self.assertIs(
            keep_context.session._retry_ledger.policy.kind,
            RecoveryBudgetKind.PERSISTENT,
        )

    def test_injected_retry_ledger_must_match_task_reach_policy(self):
        from tests.motion_nav import test_navigation_session as fixtures

        current = fixtures.frame(
            fixtures._known_world({
                (x, 0, 0): fixtures.BlockGeometry.full_cube("minecraft:stone")
                for x in range(3)
            }),
            0,
            (.5, 1., .5),
        )
        goal = fixtures._goal((2.5, 1., .5))
        cases = (
            (RetryLedger("task", policy=RecoveryBudgetPolicy.finite()),
             policy("KEEP_ACTIVE_ON_REACH")),
            (RetryLedger("task", policy=RecoveryBudgetPolicy.persistent()),
             policy("COMPLETE_ON_REACH")),
        )
        for ledger, reach_policy in cases:
            with self.subTest(policy=reach_policy):
                session = NavigationSession(
                    "policy-mismatch",
                    fixtures.NavigationSessionTests().profiles(),
                    planner_worker=fixtures._InlinePlanner(), motion_worker=fixtures._InlineMotionWorker(),
                    retry_ledger=ledger,
                )
                session.bind_source(fixtures._source())
                with self.assertRaisesRegex(ContractViolation, "recovery policy"):
                    session.start_goal(
                        "goal", 1, goal, current,
                        task_id="task", reach_policy=reach_policy,
                    )

    def test_reading_diagnostics_cannot_change_boundary_progress_result(self):
        from tests.motion_nav import test_navigation_session as fixtures

        def boundary_result(*, read_diagnostics: bool):
            clock = [0]
            ledger = RetryLedger(
                "diagnostic-boundary",
                policy=RecoveryBudgetPolicy.persistent(),
                clock_ns=lambda: clock[0],
            )
            session = NavigationSession(
                "diagnostic-boundary",
                fixtures.NavigationSessionTests().profiles(),
                planner_worker=fixtures._InlinePlanner(), motion_worker=fixtures._InlineMotionWorker(),
                retry_ledger=ledger,
                clock_ns=lambda: clock[0],
            )
            clock[0] = 30_000_000_000
            if read_diagnostics:
                self.assertIs(
                    session.diagnostics.recovery_limit_status,
                    RecoveryLimitStatus.ALLOWED,
                )
            accepted = ledger.record_task_activity(
                TaskDemandState.UNMET,
                ProgressEvidence(
                    ProgressKind.ACTION_COMPLETED,
                    1,
                    action_id="boundary-progress",
                ),
            )
            status = ledger.finalize_task_activity()
            return accepted, status, session.report.state, session.report.reason

        without_read = boundary_result(read_diagnostics=False)
        with_read = boundary_result(read_diagnostics=True)

        self.assertEqual(with_read, without_read)
        self.assertEqual(
            with_read[:2],
            (True, RecoveryLimitStatus.ALLOWED),
        )

    def test_formal_persistent_goal_stops_target_movement_at_no_progress_limit(self):
        jumped = []

        def advance_task_clock(context):
            if (not jumped
                    and context.diagnostics.route_id is not None
                    and "route_executor" in context.diagnostics.controller_ids):
                context.clock[0] += 31_000_000_000
                jumped.append(context.tick)
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )

        case = replace(
            self.flat(), max_ticks=80,
            expect="failed",
        )
        result = run(
            case, reach_policy=policy("KEEP_ACTIVE_ON_REACH"),
            control_step=advance_task_clock,
        )
        self.assertTrue(jumped)
        self.assertEqual(result.outcome, "failed", result.reason)
        self.assertEqual(result.reason, "task_no_progress_deadline_exhausted")
        limit_row = next(
            row for row in result.trace if row["loop_tick"] == jumped[0]
        )
        self.assertEqual(limit_row["session_state"], "stopping")
        self.assertTrue(limit_row["controller_ids"])
        exhausted_rows = [
            row for row in result.trace
            if row["loop_tick"] >= jumped[0]
        ]
        self.assertTrue(exhausted_rows)
        self.assertTrue(all(
            row["controller_ids"] or not row["source_bound"]
            for row in exhausted_rows
        ))
        self.assertTrue(all(
            not row["sampled_input"]["forward"]
            for row in exhausted_rows[1:]
        ))


if __name__ == "__main__":
    unittest.main()

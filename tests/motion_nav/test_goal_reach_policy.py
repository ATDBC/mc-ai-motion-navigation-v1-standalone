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
from mc2p.motion_nav.navigation_session import NavigationSession
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
                      context.diagnostics.retry_total_failures,
                      context.diagnostics.damage_spent, context.diagnostics.risk_available_points)
            context.goal_position = next_goal
            context.goal_state = _goal(next_goal, context.risk_policy_id)
            self.assertTrue(context.driver.replace_goal("goal", 2, context.goal_state, context.clock[0]))
            self.assertIs(context.session._retry_ledger, before[0])
            self.assertIs(context.session._risk_ledger, before[1])
            self.assertEqual(context.diagnostics.retry_total_failures, before[2])
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


if __name__ == "__main__":
    unittest.main()

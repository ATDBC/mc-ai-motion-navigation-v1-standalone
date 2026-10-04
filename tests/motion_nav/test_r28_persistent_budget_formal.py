"""R28-4 persistent recovery budgets on the formal Runtime navigation path."""
from __future__ import annotations

from dataclasses import replace
import unittest

from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.motion_nav.goal_observation import ObservedGoalStatus
from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
from mc2p.motion_nav.navigation_handoff import StopCause
from mc2p.motion_nav.navigation_session import PendingPlanningRecovery
from mc2p.motion_nav.retry_ledger import (
    RecoveryFinishEvidence, RecoveryFinishKind, RecoveryIdentity,
    RetryCause,
)
from tests.sim.backend import Perturbations
from tests.sim.runner import InlinePlannerWorker, _goal, run
from tests.sim.scenarios import SCENARIOS


class _OneTimeoutPerRecoveryPlanner(InlinePlannerWorker):
    """Lose one result after each injected deviation, then deliver its retry."""

    def __init__(self) -> None:
        super().__init__()
        self._armed = False
        self._armed_submissions = 0
        self.completed_timeout_cycles = 0

    def arm_one_timeout(self) -> None:
        if self._armed:
            raise AssertionError("planner timeout cycle is already active")
        self._armed = True
        self._armed_submissions = 0

    def submit_surface_snapshot(self, *args, **kwargs) -> bool:
        submitted = super().submit_surface_snapshot(*args, **kwargs)
        if self._armed:
            self._armed_submissions += 1
            if self._armed_submissions >= 2:
                self._armed = False
                self.completed_timeout_cycles += 1
        return submitted

    def poll_latest(self):
        if self._armed and self._armed_submissions == 1:
            return None
        return super().poll_latest()


class PersistentBudgetFormalTests(unittest.TestCase):
    def flat(self, *, expect: str, max_ticks: int):
        return replace(
            next(item for item in SCENARIOS if item.name == "flat_walk"),
            perturbations=Perturbations(),
            expect=expect,
            max_ticks=max_ticks,
        )

    def _inject_execution_deviation(self, context) -> None:
        session = context.session
        self.assertIsNotNone(session.active_route)
        self.assertTrue(session.has_owned_body_control)
        self.assertIsNone(session._handoff.stop_request)
        session._pending_planning_recovery = PendingPlanningRecovery(
            "active_route_dependency_changed",
            RetryCause.DEPENDENCY,
        )
        session._supervisor.route.request_stop(StopCause.DEPENDENCY_CHANGED)

    def _run_recovery_journey(
        self, *, count: int, interval_ns: int,
        expect: str, max_ticks: int,
        planner: _OneTimeoutPerRecoveryPlanner | None = None,
    ):
        recoveries: list[tuple[int, int]] = []
        pending = False
        revision = 1

        def step(context):
            nonlocal pending, revision
            session = context.session
            stable = (
                session.report.observed_goal_status
                    is ObservedGoalStatus.SATISFIED
                and not session.has_owned_body_control
            )
            if stable and len(recoveries) < count and not pending:
                context.clock[0] += interval_ns
                revision += 1
                position = (
                    (.5, 64.0, .5)
                    if revision % 2 == 0
                    else (.5, 64.0, 8.5)
                )
                goal = _goal(position, context.risk_policy_id)
                self.assertTrue(context.driver.replace_goal(
                    "goal", revision, goal, context.clock[0],
                ))
                context.goal_state = goal
                context.goal_position = position
                pending = True
            elif (
                pending
                and session.active_route is not None
                and session.has_owned_body_control
                and session._handoff.stop_request is None
            ):
                if planner is not None:
                    planner.arm_one_timeout()
                self._inject_execution_deviation(context)
                recoveries.append((context.tick, context.clock[0]))
                pending = False
            elif stable and len(recoveries) >= count and expect == "cancelled":
                session.cancel("formal_persistent_budget_complete")
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )

        result = run(
            self.flat(expect=expect, max_ticks=max_ticks),
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            control_step=step,
            planner_factory=(
                InlinePlannerWorker
                if planner is None else lambda: planner
            ),
        )
        return result, recoveries

    def test_thirty_virtual_minutes_allow_twenty_low_rate_recoveries(self):
        result, recoveries = self._run_recovery_journey(
            count=20,
            interval_ns=90_000_000_000,
            expect="cancelled",
            max_ticks=1200,
        )

        self.assertEqual(result.outcome, "cancelled", result.reason)
        self.assertEqual(result.violations, [])
        self.assertEqual(len(recoveries), 20)
        self.assertGreaterEqual(recoveries[-1][1] - recoveries[0][1],
                                19 * 90_000_000_000)
        self.assertEqual(result.trace[-1]["recovery_total_starts"], 20)
        self.assertEqual(max(
            row["recovery_window_starts"] for row in result.trace
        ), 1)
        self.assertEqual(result.trace[-1]["active_waits"], ())
        self.assertFalse(result.trace[-1]["source_bound"])

    def test_thirteenth_recovery_with_planning_timeouts_is_typed_and_bounded(self):
        planner = _OneTimeoutPerRecoveryPlanner()
        result, recoveries = self._run_recovery_journey(
            count=13,
            interval_ns=0,
            expect="failed",
            max_ticks=1000,
            planner=planner,
        )

        self.assertEqual(result.outcome, "failed", result.reason)
        self.assertEqual(result.reason, "task_recovery_rate_exhausted")
        self.assertEqual(result.violations, [])
        self.assertEqual(len(recoveries), 13)
        self.assertEqual(max(
            row["recovery_total_starts"] for row in result.trace
        ), 12)
        self.assertEqual(
            result.trace[-1]["recovery_limit_status"],
            "persistent_rate_exhausted",
        )
        self.assertEqual(planner.completed_timeout_cycles, 12)
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertEqual(result.trace[-1]["active_waits"], ())

    def test_one_recovery_over_ten_seconds_has_typed_bounded_cleanup(self):
        started: list[int] = []
        advanced: list[int] = []

        def step(context):
            session = context.session
            if (not started and session.active_route is not None
                    and session.has_owned_body_control):
                self._inject_execution_deviation(context)
                started.append(context.tick)
            elif (started and not advanced
                  and session._retry_ledger.active_recovery_id is not None):
                context.clock[0] += 11_000_000_000
                advanced.append(context.tick)
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )

        result = run(
            self.flat(expect="failed", max_ticks=150),
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            control_step=step,
        )

        self.assertTrue(started)
        self.assertTrue(advanced)
        self.assertEqual(
            (result.outcome, result.reason),
            ("failed", "single_recovery_deadline_exhausted"),
        )
        self.assertEqual(result.violations, [])
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertEqual(result.trace[-1]["active_waits"], ())

    def test_airborne_no_progress_limit_keeps_strict_owner_until_landing(self):
        advanced: list[int] = []

        def step(context):
            if (not advanced and not context.backend.state.on_ground
                    and context.session.has_owned_body_control):
                context.clock[0] += 31_000_000_000
                advanced.append(context.tick)
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )

        base = next(
            item for item in SCENARIOS if item.name == "direct_drop_2"
        )
        result = run(
            replace(base, expect="failed", max_ticks=180),
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            control_step=step,
        )

        self.assertTrue(advanced)
        self.assertEqual(
            (result.outcome, result.reason),
            ("failed", "task_no_progress_deadline_exhausted"),
        )
        self.assertEqual(result.violations, [])
        airborne_cleanup = [
            row for row in result.trace
            if row["loop_tick"] >= advanced[0] and not row["on_ground"]
        ]
        self.assertTrue(airborne_cleanup)
        self.assertTrue(all(
            "route_executor" in row["controller_ids"]
            for row in airborne_cleanup
        ))
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])

    def test_airborne_rate_exhaustion_keeps_strict_owner_until_landing(self):
        charged: list[int] = []
        exhausted: list[int] = []

        def step(context):
            session = context.session
            ledger = session._retry_ledger
            if not charged:
                for sequence in range(12):
                    identity = RecoveryIdentity(
                        sequence, f"precharged-{sequence}",
                    )
                    self.assertTrue(ledger.begin_recovery(
                        identity, RetryCause.EXECUTION,
                    ).first_seen)
                    ledger.finish_recovery(
                        identity,
                        RecoveryFinishEvidence(
                            RecoveryFinishKind.SAFE_RELEASE,
                        ),
                    )
                charged.append(context.tick)
            elif (not exhausted and not context.backend.state.on_ground
                  and session.has_owned_body_control):
                self._inject_execution_deviation(context)
                exhausted.append(context.tick)
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )

        base = next(
            item for item in SCENARIOS if item.name == "direct_drop_2"
        )
        result = run(
            replace(base, expect="failed", max_ticks=180),
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            control_step=step,
        )

        self.assertTrue(charged)
        self.assertTrue(exhausted)
        self.assertEqual(
            (result.outcome, result.reason),
            ("failed", "task_recovery_rate_exhausted"),
        )
        self.assertEqual(result.violations, [])
        cleanup = [
            row for row in result.trace
            if row["loop_tick"] >= exhausted[0]
        ]
        self.assertTrue(any(not row["on_ground"] for row in cleanup))
        self.assertTrue(all(
            "route_executor" in row["controller_ids"]
            for row in cleanup if not row["on_ground"]
        ))
        self.assertFalse(any(
            row["planning_submissions"] for row in cleanup
        ))
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertEqual(result.trace[-1]["active_waits"], ())

    def test_long_satisfied_hold_starts_a_fresh_unmet_interval(self):
        held: list[int] = []
        revised: list[int] = []
        stable_frames = 0

        def step(context):
            nonlocal stable_frames
            session = context.session
            stable = (
                session.report.observed_goal_status
                    is ObservedGoalStatus.SATISFIED
                and not session.has_owned_body_control
            )
            stable_frames = stable_frames + 1 if stable else 0
            if stable and stable_frames >= 3 and not held:
                context.clock[0] += 600_000_000_000
                held.append(context.tick)
                position = (.5, 64.0, 8.5)
                goal = _goal(position, context.risk_policy_id)
                self.assertTrue(context.driver.replace_goal(
                    "goal", 2, goal, context.clock[0],
                ))
                context.goal_state = goal
                context.goal_position = position
                revised.append(context.tick)
            elif stable and revised:
                session.cancel("long_hold_complete")
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )

        case = replace(
            self.flat(expect="cancelled", max_ticks=180),
            goal=(.5, 64.0, .5),
        )
        result = run(
            case,
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            control_step=step,
        )

        self.assertEqual(result.outcome, "cancelled", result.reason)
        self.assertEqual(result.violations, [])
        self.assertEqual(len(held), 1)
        self.assertEqual(len(revised), 1)
        self.assertTrue(any(
            row["goal_revision"] == 2 and row["route_id"] is not None
            for row in result.trace
        ))
        self.assertEqual(result.trace[-1]["active_waits"], ())

    def test_revisions_every_three_five_eight_ticks_cannot_fake_progress(self):
        revision = 1

        def step(context):
            nonlocal revision
            if (context.tick % 3 == 0 or context.tick % 5 == 0
                    or context.tick % 8 == 0):
                next_revision = revision + 1
                position = (
                    (.5, 64.0, .5)
                    if next_revision % 2 == 0
                    else (.5, 64.0, 8.5)
                )
                goal = _goal(position, context.risk_policy_id)
                if context.driver.replace_goal(
                        "goal", next_revision, goal, context.clock[0]):
                    revision = next_revision
                    context.goal_state = goal
                    context.goal_position = position
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )

        result = run(
            self.flat(expect="failed", max_ticks=800),
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            control_step=step,
        )

        self.assertGreater(revision, 100)
        self.assertEqual(
            (result.outcome, result.reason),
            ("failed", "task_no_progress_deadline_exhausted"),
        )
        self.assertEqual(result.violations, [])
        self.assertEqual(max(
            row["recovery_total_starts"] for row in result.trace
        ), 0)
        self.assertFalse(result.trace[-1]["source_bound"])

    def test_cancel_during_active_recovery_preserves_cancel_and_cleans_owners(self):
        started: list[int] = []
        cancelled: list[int] = []

        def step(context):
            session = context.session
            if (not started and session.active_route is not None
                    and session.has_owned_body_control):
                self._inject_execution_deviation(context)
                started.append(context.tick)
            elif (started and not cancelled
                  and session._retry_ledger.active_recovery_id is not None):
                session.cancel("cancel_during_recovery")
                cancelled.append(context.tick)
            context.driver.tick(
                BehaviorProfileV0(), context.clock[0] + 500_000_000,
            )

        result = run(
            self.flat(expect="cancelled", max_ticks=150),
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            control_step=step,
        )

        self.assertTrue(started)
        self.assertTrue(cancelled)
        self.assertEqual(
            (result.outcome, result.reason),
            ("cancelled", "cancel_during_recovery"),
        )
        self.assertEqual(result.violations, [])
        self.assertEqual(result.trace[-1]["recovery_total_starts"], 1)
        self.assertTrue(result.trace[-1]["on_ground"])
        self.assertFalse(result.trace[-1]["source_bound"])
        self.assertEqual(result.trace[-1]["active_waits"], ())


if __name__ == "__main__":
    unittest.main()

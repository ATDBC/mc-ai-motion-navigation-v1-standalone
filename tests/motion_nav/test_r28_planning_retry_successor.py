from __future__ import annotations

import unittest
from dataclasses import replace

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.async_work import AsyncComputationScope
from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
from mc2p.motion_nav.goal_planning_policy import GoalPlanningPolicy
from mc2p.motion_nav.motion_risk import (
    RiskCommitEvidence,
    RiskCommitKind,
    TaskDamageBudget,
    TaskRiskLedger,
)
from mc2p.motion_nav.navigation_lifecycle import (
    NavigationTransitionAction,
)
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.planning_coordinator import (
    PlanningAttemptPermit,
    PlanningAttemptPermitKind,
    PlanningCapabilities,
    PlanningCoordinator,
    PlanningRetryTrigger,
    PlanningUpdateKind,
)
from mc2p.motion_nav.retry_ledger import (
    RecoveryBudgetPolicy,
    RecoveryFinishEvidence,
    RecoveryFinishKind,
    RecoveryIdentity,
    RecoveryLimitStatus,
    RetryCause,
    RetryLedger,
)
from tests.motion_nav.test_navigation_session import _InlinePlanner, _goal
from tests.motion_nav.test_planning_coordinator import _permit, _request, _world
from tests.motion_nav.test_b07_step_route import frame, step_profile
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_jump_up import jump_profile
from mc2p.motion_nav.support_surfaces import SurfaceNodeId, query_support_surfaces
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.route_validation import GroundCapabilityIdentity
from mc2p.motion_nav.world_model import BlockGeometry, ObservationStamp
from tests.motion_nav.test_d060_terminal_node_exact_proof import (
    _world as direct_world,
)
from tests.motion_nav.test_d064_ground_direct_handoff import (
    _goal as direct_goal,
    _request as direct_request,
)


class PlanningRetryChainContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.coordinator = PlanningCoordinator(
            "coordinator-task",
            PlanningCapabilities(
                ordinary_profile(), step_profile(), jump_profile(), (), None,
            ),
            planner_worker=_InlinePlanner(),
            route_admitter=RouteAdmitter(),
            retry_ledger=RetryLedger("coordinator-task"),
            clock_ns=lambda: 1_000_000_000,
            snapshot_cells_per_step=10_000,
        )
        self.world = _world()
        self.request = _request(self.world)
        self.current = frame(
            self.world,
            0,
            query_support_surfaces(
                self.world.view(), -1, 0, 1, 1,
            ).surfaces[0].position,
        )
        self.coordinator.begin(
            self.request,
            self.current,
            permit=_permit(),
            state_anchor=None,
            remaining_damage_budget=self.request.damage_budget,
        )

    def test_alternating_causes_exhaust_the_third_local_planning_failure(self):
        updates = []
        for index, cause in enumerate((
            RetryCause.PLANNING,
            RetryCause.DEPENDENCY,
            RetryCause.EXECUTION,
        ), 1):
            updates.append(self.coordinator.retry_from_current(
                self.current,
                trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
                cause=cause,
                failure_id=f"alternating-{index}",
                remaining_damage_budget=self.request.damage_budget,
            ))

        self.assertEqual(
            tuple(update.kind for update in updates),
            (PlanningUpdateKind.RUNNING,
             PlanningUpdateKind.RUNNING,
             PlanningUpdateKind.FAILED),
        )
        self.assertEqual(updates[-1].failure.reason, "planning_retry_exhausted")
        self.assertEqual(self.coordinator._retry_ledger.total_recovery_starts, 0)

    def test_goal_and_attempt_revisions_do_not_reset_local_planning_chain(self):
        first = self.coordinator.retry_from_current(
            self.current,
            trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
            cause=RetryCause.DEPENDENCY,
            failure_id="revision-failure-1",
            remaining_damage_budget=self.request.damage_budget,
        )
        revised = replace(
            first.request,
            sequence=first.request.sequence + 1,
            request_id="coordinator-request-revised",
            goal_revision=first.request.goal_revision + 1,
        )
        self.coordinator.begin(
            revised,
            self.current,
            permit=PlanningAttemptPermit(
                "coordinator-revision-permit",
                "coordinator-task",
                revised.goal_revision,
                "goal-revised",
                PlanningAttemptPermitKind.TASK_UPDATE,
            ),
            state_anchor=None,
            remaining_damage_budget=revised.damage_budget,
        )
        second = self.coordinator.retry_from_current(
            self.current,
            trigger=PlanningRetryTrigger.WORK_FAILURE,
            cause=RetryCause.PLANNING,
            failure_id="revision-failure-2",
            remaining_damage_budget=revised.damage_budget,
        )
        third = self.coordinator.retry_from_current(
            self.current,
            trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
            cause=RetryCause.EXECUTION,
            failure_id="revision-failure-3",
            remaining_damage_budget=revised.damage_budget,
        )

        self.assertIs(second.kind, PlanningUpdateKind.RUNNING)
        self.assertIs(third.kind, PlanningUpdateKind.FAILED)
        self.assertEqual(self.coordinator.local_attempt_failures, 3)

    def test_route_delivery_does_not_reset_until_session_confirms_admission(self):
        for index in range(2):
            update = self.coordinator.retry_from_current(
                self.current,
                trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
                cause=RetryCause.DEPENDENCY,
                failure_id=f"candidate-invalid-{index}",
                remaining_damage_budget=self.request.damage_budget,
            )
            self.assertIs(update.kind, PlanningUpdateKind.RUNNING)

        delivered = self.coordinator.advance(
            self.current,
            state_anchor=None,
            edge_probe=None,
            remaining_damage_budget=self.request.damage_budget,
            current_scope=self.coordinator._request_ledger.current_computation_scope,
        )
        self.assertIs(delivered.kind, PlanningUpdateKind.ROUTE_READY)
        self.assertEqual(self.coordinator.local_attempt_failures, 2)

        exhausted = self.coordinator.retry_from_current(
            self.current,
            trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
            cause=RetryCause.EXECUTION,
            failure_id="candidate-invalid-after-delivery",
            remaining_damage_budget=self.request.damage_budget,
        )
        self.assertIs(exhausted.kind, PlanningUpdateKind.FAILED)
        self.assertEqual(exhausted.failure.reason, "planning_retry_exhausted")

    def test_confirmed_route_admission_resets_local_planning_chain(self):
        self.coordinator.retry_from_current(
            self.current,
            trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
            cause=RetryCause.DEPENDENCY,
            failure_id="candidate-invalid-before-admission",
            remaining_damage_budget=self.request.damage_budget,
        )
        delivered = self.coordinator.advance(
            self.current,
            state_anchor=None,
            edge_probe=None,
            remaining_damage_budget=self.request.damage_budget,
            current_scope=self.coordinator._request_ledger.current_computation_scope,
        )

        self.coordinator.confirm_route_admitted(delivered.route)

        self.assertEqual(self.coordinator.local_attempt_failures, 0)

    def test_confirmed_nonplanner_route_resets_chain_before_background_resumes(self):
        world = direct_world(281, 16)
        profile = ordinary_profile()
        current = frame(world, 0, (.5, -60.0, 1.5))
        request = direct_request(world, goal_z=12)
        coordinator = PlanningCoordinator(
            "coordinator-task",
            PlanningCapabilities(
                profile, step_profile(), jump_profile(), (), None,
            ),
            planner_worker=_InlinePlanner(hold_first=True),
            route_admitter=RouteAdmitter(),
            retry_ledger=RetryLedger("coordinator-task"),
            clock_ns=lambda: 1_000_000_000,
            snapshot_cells_per_step=10_000,
        )
        coordinator.begin(
            request,
            current,
            permit=PlanningAttemptPermit(
                "coordinator-direct-sequence-permit",
                "coordinator-task",
                request.goal_revision,
                "background-before-direct",
                PlanningAttemptPermitKind.TASK_UPDATE,
            ),
            state_anchor=None,
            remaining_damage_budget=request.damage_budget,
        )
        for index in range(2):
            retried = coordinator.retry_from_current(
                current,
                trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
                cause=RetryCause.DEPENDENCY,
                failure_id=f"before-direct-{index}",
                remaining_damage_budget=request.damage_budget,
            )
            self.assertIs(retried.kind, PlanningUpdateKind.RUNNING)
        self.assertEqual(coordinator.local_attempt_failures, 2)

        previous = coordinator.request
        direct = replace(
            previous,
            sequence=previous.sequence + 1,
            request_id="coordinator-direct-revision",
            goal_revision=previous.goal_revision + 1,
            goal=SurfaceNodeId(0, 7, -60, 0),
            goal_state=direct_goal(7.5),
            work_identity=None,
        )
        coordinator.revise_request(direct)
        coordinator.cancel_work("ground_direct_precedes_background_planning")
        admitted = RouteAdmitter().admit_ground_direct(
            direct,
            current,
            ground_profile=profile,
            capability_identity=GroundCapabilityIdentity.from_profile(profile),
        )
        self.assertIs(admitted.status, AdmissionStatus.ACCEPTED)
        self.assertIsNone(admitted.route.work_identity)

        scope = coordinator._request_ledger.current_computation_scope
        with self.assertRaises(ContractViolation):
            coordinator.confirm_nonplanner_route_admitted(
                admitted.route,
                current_scope=AsyncComputationScope(
                    scope.world_session_id,
                    scope.task_id,
                    scope.generation + 1,
                ),
            )
        coordinator.confirm_nonplanner_route_admitted(
            admitted.route,
            current_scope=scope,
        )
        self.assertEqual(coordinator.local_attempt_failures, 0)

        background = replace(
            direct,
            sequence=direct.sequence + 1,
            request_id="coordinator-background-revision",
            goal_revision=direct.goal_revision + 1,
            goal=SurfaceNodeId(0, 12, -60, 0),
            goal_state=direct_goal(12.5),
        )
        coordinator.revise_request(background)
        coordinator.begin(
            background,
            current,
            permit=PlanningAttemptPermit(
                "coordinator-background-permit",
                "coordinator-task",
                background.goal_revision,
                "background-after-direct",
                PlanningAttemptPermitKind.TASK_UPDATE,
            ),
            state_anchor=None,
            remaining_damage_budget=background.damage_budget,
        )
        next_failure = coordinator.retry_from_current(
            current,
            trigger=PlanningRetryTrigger.LOCAL_RESULT_INVALID,
            cause=RetryCause.PLANNING,
            failure_id="background-after-direct-failure",
            remaining_damage_budget=background.damage_budget,
        )

        self.assertIs(next_failure.kind, PlanningUpdateKind.RUNNING)
        self.assertEqual(coordinator.local_attempt_failures, 1)

    def test_recovery_reanchor_requires_a_handoff_permit(self):
        with self.assertRaisesRegex(ContractViolation, "handoff permit"):
            self.coordinator.retry_from_current(
                self.current,
                trigger=PlanningRetryTrigger.RECOVERY_REANCHOR,
                cause=RetryCause.DEPENDENCY,
                failure_id="unpaid-recovery-reanchor",
                remaining_damage_budget=self.request.damage_budget,
            )


class SuccessorAndContinuationContractTests(unittest.TestCase):
    def _session_profiles(self):
        from tests.motion_nav.test_navigation_session import NavigationSessionTests
        return NavigationSessionTests().profiles()

    def _terminal_session(self):
        from unittest.mock import Mock
        from mc2p.motion_nav.motion_worker import MotionWorkerPort
        planner = _InlinePlanner()
        motion = Mock(spec=MotionWorkerPort)
        session = NavigationSession(
            "old-session", self._session_profiles(),
            planner_worker=planner, retry_ledger=RetryLedger("old-task"),
            risk_ledger=TaskRiskLedger("old-task", TaskDamageBudget()),
            motion_worker=motion,
            clock_ns=lambda: 1_000_000_000,
        )
        session._owns_motion_worker = True
        session._transition(NavigationTransitionAction.MARK_FAILED, "old-ended")
        self.addCleanup(session.close)
        return session, planner

    def test_successor_requires_explicit_new_task_before_worker_transfer(self):
        session, planner = self._terminal_session()
        motion = session._motion_worker
        with self.assertRaises(TypeError):
            session.spawn_successor("new-session")
        with self.assertRaises(TypeError):
            session.spawn_successor("new-session", "new-task")
        for task_id in (None, "", " ", "old-task"):
            with self.subTest(task_id=task_id):
                with self.assertRaises(ContractViolation):
                    session.spawn_successor("new-session", task_id=task_id)
                self.assertFalse(session._closed)
                self.assertTrue(session._owns_planner_worker)
                self.assertTrue(session._owns_motion_worker)
                self.assertIs(session._planner, planner)
                self.assertFalse(planner.closed)
                motion.close.assert_not_called()
        with self.assertRaises(ContractViolation):
            session.spawn_successor("", task_id="new-task")
        self.assertFalse(session._closed)
        self.assertTrue(session._owns_planner_worker)
        successor = session.spawn_successor("new-session", task_id="new-task")
        self.addCleanup(successor.close)
        self.assertTrue(session._closed)
        self.assertTrue(successor._owns_planner_worker)
        self.assertIs(successor._planner, planner)
        self.assertIs(successor._motion_worker, motion)
        self.assertTrue(successor._owns_motion_worker)
        self.assertFalse(session._owns_motion_worker)
        motion.close.assert_not_called()
        successor.close()
        self.assertTrue(planner.closed)
        motion.close.assert_called_once_with()

    def test_mismatched_successor_start_preserves_fresh_state_and_allows_retry(self):
        for entry in ("start", "start_goal"):
            with self.subTest(entry=entry):
                session, planner = self._terminal_session()
                successor = session.spawn_successor("new-session", task_id="new-task")
                self.addCleanup(successor.close)
                world = _world()
                current = frame(world, 0, (-.5, 1., .5))
                request = replace(_request(world), goal_id="wrong-task",
                                  reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
                                  goal_state=_goal((.5, 1., .5)))
                before_report = successor.report
                before_lifecycle = successor._lifecycle.state
                if entry == "start":
                    def start(task_id):
                        successor.start(replace(request, goal_id=task_id), current)
                else:
                    def start(task_id):
                        successor.start_goal("same-goal", 1, request.goal_state, current,
                            task_id=task_id, reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH)
                for invalid_id in ("wrong-task", "old-task", ""):
                    with self.subTest(invalid_id=invalid_id):
                        with self.assertRaises(ContractViolation):
                            start(invalid_id)
                if entry == "start_goal":
                    with self.assertRaises(ContractViolation):
                        start(None)
                self.assertEqual(successor.report, before_report)
                self.assertIs(successor._lifecycle.state, before_lifecycle)
                self.assertIsNone(successor._retry_ledger)
                self.assertIsNone(successor._risk_ledger)
                self.assertIsNone(successor._request)
                self.assertIsNone(successor._pending_goal)
                self.assertIs(successor._goal_requests.reach_policy,
                              GoalReachPolicy.COMPLETE_ON_REACH)
                self.assertEqual(planner.jobs, [])
                self.assertIs(successor._planner, planner)
                self.assertTrue(successor._owns_planner_worker)
                self.assertTrue(successor._owns_motion_worker)
                successor._motion_worker.close.assert_not_called()
                self.assertTrue(session._closed)
                self.assertFalse(planner.closed)
                start("new-task")
                self.assertEqual(successor._retry_ledger.task_id, "new-task")
                self.assertEqual(successor._risk_ledger.task_id, "new-task")
                self.assertIs(successor._goal_requests.reach_policy,
                              GoalReachPolicy.KEEP_ACTIVE_ON_REACH)

    def test_true_successor_reuses_workers_but_starts_without_task_ledgers(self):
        planner = _InlinePlanner()
        old_retry = RetryLedger("old-task")
        old_risk = TaskRiskLedger("old-task", TaskDamageBudget("old-risk", 2))
        session = NavigationSession(
            "old-session", self._session_profiles(),
            planner_worker=planner, retry_ledger=old_retry,
            risk_ledger=old_risk, clock_ns=lambda: 1_000_000_000,
        )
        session._transition(NavigationTransitionAction.MARK_FAILED, "old-ended")

        successor = session.spawn_successor("new-session", task_id="new-task")

        self.assertIs(successor._planner, planner)
        self.assertIsNone(successor._retry_ledger)
        self.assertIsNone(successor._risk_ledger)
        self.assertIsNot(successor._goal_requests, session._goal_requests)
        world = _world()
        request = replace(
            _request(world),
            request_id="new-task-request",
            goal_id="new-task",
        )
        current = frame(
            world,
            0,
            query_support_surfaces(
                world.view(), -1, 0, 1, 1,
            ).surfaces[0].position,
        )
        successor.start(request, current)
        self.assertEqual(successor._retry_ledger.task_id, "new-task")
        self.assertEqual(successor._risk_ledger.task_id, "new-task")
        self.assertIsNot(successor._retry_ledger, old_retry)
        self.assertIsNot(successor._risk_ledger, old_risk)
        successor.close()

    def test_same_task_rebuild_keeps_ledgers_policy_and_spent_damage(self):
        retry = RetryLedger(
            "combat-task", policy=RecoveryBudgetPolicy.persistent(),
            clock_ns=lambda: 1_000_000_000,
        )
        recovery = RecoveryIdentity(1, "combat-recovery")
        retry.begin_recovery(recovery, RetryCause.EXECUTION)
        retry.finish_recovery(
            recovery,
            RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
        )
        risk = TaskRiskLedger(
            "combat-task", TaskDamageBudget("combat-risk", 4),
        )
        risk.reserve("drop", 2, policy_revision=0)
        risk.commit("drop", RiskCommitEvidence(
            RiskCommitKind.OBSERVED_DEPARTURE,
            observation_sequence=1,
        ))
        risk.settle("drop", observed_damage_points=2)
        session = NavigationSession(
            "combat-session", self._session_profiles(),
            planner_worker=_InlinePlanner(), retry_ledger=retry,
            risk_ledger=risk, clock_ns=lambda: 1_000_000_000,
        )
        session._goal_requests.select_reach_policy(
            GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
        )
        session._goal_requests.select_planning_policy(
            GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND,
        )
        session._task_damage_budget = risk.budget
        session._movement_damage_spent_points = 2.0
        session._transition(NavigationTransitionAction.MARK_FAILED, "approach-ended")
        evidence = session.same_task_continuation_evidence()
        self.assertIsNotNone(evidence)

        continuation = session.rebuild_same_task(
            "combat-session-2", evidence,
        )

        self.assertIs(continuation._retry_ledger, retry)
        self.assertIs(continuation._risk_ledger, risk)
        self.assertIs(
            continuation._goal_requests.reach_policy,
            GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
        )
        self.assertIs(
            continuation._goal_requests.planning_policy,
            GoalPlanningPolicy.PROVED_LOCAL_DIRECT_THEN_BACKGROUND,
        )
        self.assertEqual(continuation._movement_damage_spent_points, 2.0)
        self.assertEqual(continuation._retry_ledger.total_recovery_starts, 1)
        self.assertEqual(continuation._risk_ledger.committed_points, 2.0)
        world = _world()
        request = replace(
            _request(world),
            request_id="combat-continuation-request",
            goal_id="combat-task",
            reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH,
            damage_budget=risk.budget,
        )
        current = frame(
            world,
            0,
            query_support_surfaces(
                world.view(), -1, 0, 1, 1,
            ).surfaces[0].position,
        )
        continuation.start(request, current)
        self.assertIs(continuation._retry_ledger, retry)
        self.assertIs(continuation._risk_ledger, risk)
        self.assertEqual(continuation._movement_damage_spent_points, 2.0)
        continuation.close()

    def test_exhausted_task_ledger_cannot_issue_continuation_evidence(self):
        retry = RetryLedger(
            "combat-task",
            policy=RecoveryBudgetPolicy.finite(maximum_recoveries=1),
            clock_ns=lambda: 1_000_000_000,
        )
        first = RecoveryIdentity(1, "first")
        retry.begin_recovery(first, RetryCause.EXECUTION)
        retry.finish_recovery(
            first,
            RecoveryFinishEvidence(RecoveryFinishKind.SAFE_RELEASE),
        )
        retry.begin_recovery(RecoveryIdentity(2, "exhaust"), RetryCause.PLANNING)
        session = NavigationSession(
            "exhausted-session", self._session_profiles(),
            planner_worker=_InlinePlanner(), retry_ledger=retry,
            risk_ledger=TaskRiskLedger("combat-task", TaskDamageBudget()),
            clock_ns=lambda: 1_000_000_000,
        )
        session._transition(NavigationTransitionAction.MARK_FAILED, "ended")

        self.assertIsNone(session.same_task_continuation_evidence())
        with self.assertRaises(ContractViolation):
            session.rebuild_same_task("forbidden", object())

    def test_continuation_evidence_rechecks_no_progress_clock(self):
        now = [0]
        retry = RetryLedger(
            "combat-task",
            policy=RecoveryBudgetPolicy.persistent(
                maximum_no_progress_ns=30_000_000_000,
            ),
            clock_ns=lambda: now[0],
        )
        session = NavigationSession(
            "expired-session", self._session_profiles(),
            planner_worker=_InlinePlanner(), retry_ledger=retry,
            risk_ledger=TaskRiskLedger("combat-task", TaskDamageBudget()),
            clock_ns=lambda: now[0],
        )
        session._transition(NavigationTransitionAction.MARK_FAILED, "ended")
        now[0] = 31_000_000_000

        self.assertIsNone(session.same_task_continuation_evidence())
        self.assertIs(
            retry.recovery_limit_status,
            RecoveryLimitStatus.NO_PROGRESS_EXHAUSTED,
        )

    def test_continuation_consumption_rechecks_after_evidence_was_issued(self):
        now = [0]
        retry = RetryLedger(
            "combat-task",
            policy=RecoveryBudgetPolicy.persistent(
                maximum_no_progress_ns=30_000_000_000,
            ),
            clock_ns=lambda: now[0],
        )
        session = NavigationSession(
            "expiring-session", self._session_profiles(),
            planner_worker=_InlinePlanner(), retry_ledger=retry,
            risk_ledger=TaskRiskLedger("combat-task", TaskDamageBudget()),
            clock_ns=lambda: now[0],
        )
        session._transition(NavigationTransitionAction.MARK_FAILED, "ended")
        now[0] = 29_000_000_000
        evidence = session.same_task_continuation_evidence()
        self.assertIsNotNone(evidence)
        now[0] = 31_000_000_000

        with self.assertRaisesRegex(ContractViolation, "absent, stale, or foreign"):
            session.rebuild_same_task("too-late", evidence)

    def test_active_route_reality_deviation_buys_one_task_recovery(self):
        world = _world()
        request = _request(world)
        current = frame(
            world,
            0,
            query_support_surfaces(
                world.view(), -1, 0, 1, 1,
            ).surfaces[0].position,
        )
        session = NavigationSession(
            "deviation-session", self._session_profiles(),
            planner_worker=_InlinePlanner(), clock_ns=lambda: 1_000_000_000,
        )
        session.start(request, current)
        session.propose(current, None, 2_000_000_000)
        self.assertIsNotNone(session.active_route)
        dependency = session.active_route.action_route.dependencies[0]
        world.observe_blocks(ObservationStamp(
            world.session, 2, 2, "test-clock", 2,
        ), {dependency: BlockGeometry.full_cube("minecraft:dirt")})
        changed = replace(
            frame(world, 1, current.body.position),
            changed_cells=(dependency,),
        )

        session.propose(changed, None, 2_000_000_000)

        self.assertEqual(session._retry_ledger.total_recovery_starts, 1)
        session.close()


if __name__ == "__main__":
    unittest.main()

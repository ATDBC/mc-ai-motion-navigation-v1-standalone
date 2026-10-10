"""D094 same-support recovery and input-loss cause on the formal chain."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from mc2p.motion_nav.body_control import StopCause
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.known_map_planner import SurfacePlanningRequest
from mc2p.motion_nav.movement_transition import GoalState, GoalSupport, MovementMode
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.route_validation import GroundCapabilityIdentity, replay_walk_validation_recipe
from mc2p.motion_nav.support_surfaces import query_support_surfaces, SurfaceNodeId
from mc2p.motion_nav.world_model import Aabb, BlockGeometry
from scripts.action_entry_late_hardening import run_case
from tests.motion_nav.test_b07_support_surfaces import surface_world
from tests.motion_nav.test_b07_step_transition import frame
from tests.motion_nav.test_b07_surface_planning import ordinary_profile


class PostInputLossLandingRecoveryTests(unittest.TestCase):
    def local(self, start=-.18, *, grounded=True, material="minecraft:stone", goal_x=.5):
        world=surface_world({(0,0,0): BlockGeometry.full_cube(material)})
        profile=ordinary_profile()
        goal=GoalState(Aabb(goal_x-.1,.99,.4,goal_x+.1,1.01,.6),GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),frozenset({"standing"}),.6)
        request=SurfacePlanningRequest(1,"d094-local","d094-goal",0,world.session.value,
            SurfaceNodeId(0,0,1,0),SurfaceNodeId(0,0,1,0),goal_state=goal)
        result=RouteAdmitter().admit_local_direct(request,frame(world,1,(start,1.,.5),on_ground=grounded),
            ground_profile=profile,capability_identity=GroundCapabilityIdentity.from_profile(profile))
        return world,result

    def test_grounded_twenty_percent_support_walks_inward_and_replays(self):
        world,result=self.local()
        self.assertIs(result.status,AdmissionStatus.ACCEPTED)
        route=result.route
        status,_=replay_walk_validation_recipe(route.validation_plan.recipes[0],world.view())
        self.assertIs(status,QueryStatus.FEASIBLE)
        self.assertEqual(route.fixed_route.points[0].x,-.18)

    def test_below_execution_minimum_and_airborne_start_reject(self):
        for start,grounded in ((-.22,True),(-.18,False)):
            with self.subTest(start=start,grounded=grounded):
                _,result=self.local(start,grounded=grounded)
                self.assertIsNot(result.status,AdmissionStatus.ACCEPTED)

    def test_unsupported_material_is_not_granted_low_support_entry(self):
        _,result=self.local(material="minecraft:cactus")
        self.assertIsNot(result.status,AdmissionStatus.ACCEPTED)

    def test_unknown_start_neighbor_requires_information_before_movement(self):
        from mc2p.motion_nav.world_model import ObservationStamp
        world,result=self.local()
        world.invalidate(ObservationStamp(world.session,2,2,"test-clock",100_000_000),((-1,1,0),))
        profile=ordinary_profile()
        goal=GoalState(Aabb(.4,.99,.4,.6,1.01,.6),GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),frozenset({"standing"}),.6)
        request=SurfacePlanningRequest(2,"d094-unknown","d094-goal",0,world.session.value,
            SurfaceNodeId(0,0,1,0),SurfaceNodeId(0,0,1,0),goal_state=goal)
        result=RouteAdmitter().admit_local_direct(request,frame(world,2,(-.18,1.,.5)),
            ground_profile=profile,capability_identity=GroundCapabilityIdentity.from_profile(profile))
        self.assertIsNot(result.status,AdmissionStatus.ACCEPTED)
        self.assertIsNone(result.route)
        self.assertTrue(result.missing_cells)

    def test_local_admission_reads_execution_minimum_from_ground_config(self):
        from types import SimpleNamespace
        with patch("mc2p.motion_nav.route_admission.FixedRouteConfig",
                   return_value=SimpleNamespace(minimum_support_fraction=.25)):
            _,result=self.local()
        self.assertIsNot(result.status,AdmissionStatus.ACCEPTED)

    def test_landing_input_loss_replans_on_the_formal_path(self):
        result=run_case("column_landing_turn",4)
        self.assertEqual(result["outcome"],"success",result["reason"])
        self.assertFalse(result["violations"])
        self.assertEqual(result["damage"],0)
        # The recovery must contain a second request, followed by grounded Walk.
        self.assertGreater(max(row["recovery_total_starts"] for row in result["trace"]),0)

    def test_support_cannot_decrease_before_margin_or_dip_after_it(self):
        from mc2p.motion_nav.geometry import query_support
        for dip_after in (False,True):
            def support(box,world,**kwargs):
                result=query_support(box,world,**kwargs)
                x=(box.min_x+box.max_x)/2
                if dip_after:
                    fraction=.6 if x<.2 else .45 if x<.35 else 1.
                else:
                    fraction=.2 if x<-.1 else .18 if x<0. else 1.
                return replace(result,support_fraction=fraction)
            with self.subTest(dip_after=dip_after), patch(
                    "mc2p.motion_nav.support_surfaces.query_support",side_effect=support):
                _,result=self.local()
            self.assertIsNot(result.status,AdmissionStatus.ACCEPTED)

    def test_default_connection_stays_strict_and_outward_is_rejected(self):
        from mc2p.motion_nav.support_surfaces import query_standable_connection
        world,_=self.local()
        surface=query_support_surfaces(world.view(),0,0,1.,1.).surfaces[0]
        self.assertIs(query_standable_connection(world.view(),surface,(.5,1.,.5),(-.18,1.,.5)).status,
                      QueryStatus.BLOCKED)
        self.assertIs(query_standable_connection(world.view(),surface,(-.18,1.,.5),(.5,1.,.5),
                      observed_start_minimum_support=.15).status,QueryStatus.BLOCKED)

    def test_different_support_surface_cannot_use_local_direct(self):
        from mc2p.motion_nav.route_admission import AdmissionReason
        world=surface_world({(0,0,0):BlockGeometry.full_cube("minecraft:stone"),
                             (1,0,0):BlockGeometry.full_cube("minecraft:stone")})
        profile=ordinary_profile()
        goal=GoalState(Aabb(1.4,.99,.4,1.6,1.01,.6),GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),frozenset({"standing"}),.6)
        request=SurfacePlanningRequest(1,"different-face","goal",0,world.session.value,
            SurfaceNodeId(0,0,1,0),SurfaceNodeId(1,0,1,0),goal_state=goal)
        result=RouteAdmitter().admit_local_direct(request,frame(world,1,(.5,1.,.5)),
            ground_profile=profile,capability_identity=GroundCapabilityIdentity.from_profile(profile))
        self.assertIs(result.reason,AdmissionReason.CANDIDATE_BASIS_MISMATCH)
        self.assertIsNone(result.route)

    def test_recovery_owner_keeps_input_loss_after_release_and_budget_exhaustion(self):
        from mc2p.motion_nav.navigation_handoff import NavigationHandoffCoordinator,HandoffDestination
        from mc2p.motion_nav.retry_ledger import RetryLedger,RecoveryBudgetPolicy,RecoveryIdentity,TaskDemandState
        from tests.motion_nav.test_navigation_handoff import _quiescent
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker,gap_owner
        _,current,_,_,_=gap_owner(DeferredMotionWorker())
        owner=NavigationHandoffCoordinator()
        budget=RetryLedger("d094-budget",policy=RecoveryBudgetPolicy.finite(maximum_recoveries=1))
        permit=owner.observe_task_activity(budget=budget,observation_sequence=1,demand_state=TaskDemandState.UNMET)
        self.assertTrue(owner.request_recovery(request_id="lost",destination=HandoffDestination.REPLAN,
            reason="any-report-text",budget=budget,cause=StopCause.INPUT_LOST,
            activity_permit=permit,recovery_identity=RecoveryIdentity(1,"lost")))
        handoff=replace(_quiescent(),world_session=current.session,observation_sequence_id=current.body.sequence_id)
        owner.advance(current,handoff=handoff,goal_ready=True,start_ready=True,missing_cells=(),
            unavailable_reason="unavailable",budget=budget)
        self.assertIsNone(owner.stop_request)
        self.assertIs(owner.recovery_cause,StopCause.INPUT_LOST)
        permit=owner.observe_task_activity(budget=budget,observation_sequence=2,demand_state=TaskDemandState.UNMET)
        result=owner.request_recovery(request_id="lost-again",destination=HandoffDestination.REPLAN,
            reason="different-text",budget=budget,cause=StopCause.INPUT_LOST,
            activity_permit=permit,recovery_identity=RecoveryIdentity(2,"lost-again"))
        self.assertFalse(result)
        self.assertTrue(owner.ending)
        self.assertIs(owner.stop_request.cause,StopCause.INPUT_LOST)
        owner.finish_ending(budget=budget,handoff=handoff,frame=current)
        self.assertIs(owner.recovery_cause,StopCause.INPUT_LOST)

    def test_session_reports_input_loss_during_replan_even_when_last_decision_changes(self):
        from mc2p.contracts.behavior import BehaviorProfileV0
        from tests.sim.runner import run
        from scripts.action_entry_late_hardening import scenario_for
        reports=[]
        def step(context):
            context.driver.tick(BehaviorProfileV0(),context.clock[0]+500_000_000)
            report=context.session.report
            if report.failure_cause is StopCause.INPUT_LOST:
                reports.append(report)
            return ()
        result=run(scenario_for("column_landing_turn",4),control_step=step)
        self.assertEqual(result.outcome,"success")
        self.assertTrue(reports)
        self.assertTrue(any(r.state.value in {"planning","stopping","executing"} for r in reports))

    def test_recovery_cause_does_not_pollute_a_later_unrelated_failure(self):
        from mc2p.motion_nav.navigation_handoff import NavigationHandoffCoordinator,HandoffDestination
        from mc2p.motion_nav.retry_ledger import RetryLedger,RecoveryIdentity,TaskDemandState
        from tests.motion_nav.test_navigation_handoff import _quiescent
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker,gap_owner
        _,current,_,_,_=gap_owner(DeferredMotionWorker())
        owner=NavigationHandoffCoordinator();budget=RetryLedger("later-failure")
        permit=owner.observe_task_activity(budget=budget,observation_sequence=1,demand_state=TaskDemandState.UNMET)
        owner.request_recovery(request_id="lost",destination=HandoffDestination.REPLAN,
            reason="arbitrary-text",budget=budget,cause=StopCause.INPUT_LOST,
            activity_permit=permit,recovery_identity=RecoveryIdentity(1,"lost"))
        handoff=replace(_quiescent(),world_session=current.session,observation_sequence_id=current.body.sequence_id)
        owner.advance(current,handoff=handoff,goal_ready=True,start_ready=True,missing_cells=(),
            unavailable_reason="unavailable",budget=budget)
        owner.complete_replanning()
        self.assertIsNone(owner.recovery_cause)
        owner.request_recovery(request_id="unrelated",destination=HandoffDestination.FAIL,
            reason="new-failure",budget=budget,cause=StopCause.CANCELLED)
        self.assertIs(owner.stop_request.cause,StopCause.CANCELLED)
        self.assertIs(owner.recovery_cause,StopCause.CANCELLED)
        owner.clear_goal()
        self.assertIsNone(owner.recovery_cause)
        self.assertIsNone(NavigationHandoffCoordinator().recovery_cause)

    def test_airborne_final_mismatch_keeps_failed_input_loss_fallback(self):
        from types import SimpleNamespace
        from mc2p.motion_nav.action_route_executor import ActionRouteExecutor,ActionRouteState
        from mc2p.motion_nav.goal_observation import ObservedGoal,ObservedGoalStatus
        from tests.motion_nav.test_jump_up import jump_profile
        world=surface_world({(0,0,0):BlockGeometry.full_cube("minecraft:stone")})
        executor=ActionRouteExecutor(ordinary_profile(),jump_profile())
        executor.route=SimpleNamespace(goal_state=SimpleNamespace())
        executor._final_action_failure_cause=StopCause.INPUT_LOST
        with patch("mc2p.motion_nav.action_route_executor.evaluate_observed_goal",
                   return_value=ObservedGoal(ObservedGoalStatus.NOT_SATISFIED)):
            decision=executor._finish_goal(frame(world,1,(.5,1.,.5),on_ground=False),0)
        self.assertIs(decision.state,ActionRouteState.FAILED)
        self.assertIs(decision.failure_cause,StopCause.INPUT_LOST)

    def test_planning_failure_keeps_initiating_cause_in_session_report(self):
        from mc2p.contracts.behavior import BehaviorProfileV0
        from mc2p.motion_nav.route_admission import AdmissionResult,AdmissionReason
        from tests.sim.runner import run
        from scripts.action_entry_late_hardening import scenario_for
        reports=[]
        def step(context):
            context.driver.tick(BehaviorProfileV0(),context.clock[0]+500_000_000)
            reports.append(context.session.report)
            return ()
        with patch.object(RouteAdmitter,"admit_local_direct",return_value=AdmissionResult(
                AdmissionStatus.REJECTED,AdmissionReason.CURRENT_BODY_CANNOT_CONNECT)):
            result=run(scenario_for("column_landing_turn",4),control_step=step)
        self.assertEqual(result.outcome,"failed")
        self.assertEqual(result.reason,"same_support_local_path_unavailable")
        self.assertIs(reports[-1].failure_cause,StopCause.INPUT_LOST)
        self.assertTrue(reports[-1].terminal)
        self.assertFalse(result.violations)

    def test_cancel_during_input_loss_replan_reports_cancellation(self):
        from mc2p.contracts.behavior import BehaviorProfileV0
        from tests.sim.runner import run
        from scripts.action_entry_late_hardening import scenario_for
        reports=[];cancelled=False
        def step(context):
            nonlocal cancelled
            context.driver.tick(BehaviorProfileV0(),context.clock[0]+500_000_000)
            if not cancelled and context.session.report.failure_cause is StopCause.INPUT_LOST:
                context.session.cancel("cancel_after_input_loss")
                cancelled=True
            reports.append(context.session.report)
            return ()
        result=run(scenario_for("column_landing_turn",4),control_step=step)
        self.assertTrue(cancelled)
        self.assertEqual(result.outcome,"cancelled")
        self.assertIs(reports[-1].failure_cause,StopCause.CANCELLED)
        self.assertTrue(reports[-1].terminal)
        self.assertFalse(result.violations)

    def test_duplicate_recovery_does_not_replace_the_owned_cause(self):
        from mc2p.motion_nav.navigation_handoff import NavigationHandoffCoordinator,HandoffDestination,RecoveryRequestStatus
        from mc2p.motion_nav.retry_ledger import RetryLedger,RecoveryIdentity,TaskDemandState,RecoveryStartRegistration,RecoveryLimitStatus
        from tests.motion_nav.test_navigation_handoff import _quiescent
        from tests.motion_nav.test_r27_async_admission import DeferredMotionWorker,gap_owner
        _,current,_,_,_=gap_owner(DeferredMotionWorker())
        owner=NavigationHandoffCoordinator();budget=RetryLedger("duplicate")
        permit=owner.observe_task_activity(budget=budget,observation_sequence=1,demand_state=TaskDemandState.UNMET)
        owner.request_recovery(request_id="lost",destination=HandoffDestination.REPLAN,
            reason="initial",budget=budget,cause=StopCause.INPUT_LOST,
            activity_permit=permit,recovery_identity=RecoveryIdentity(1,"lost"))
        handoff=replace(_quiescent(),world_session=current.session,observation_sequence_id=current.body.sequence_id)
        owner.advance(current,handoff=handoff,goal_ready=True,start_ready=True,missing_cells=(),
            unavailable_reason="unavailable",budget=budget)
        permit=owner.observe_task_activity(budget=budget,observation_sequence=2,demand_state=TaskDemandState.UNMET)
        with patch.object(budget,"begin_recovery",return_value=RecoveryStartRegistration(
                RecoveryLimitStatus.ALLOWED,False)):
            duplicate=owner.request_recovery(request_id="completed-identity",destination=HandoffDestination.REPLAN,
                reason="duplicate-report",budget=budget,cause=StopCause.CANCELLED,
                activity_permit=permit,recovery_identity=RecoveryIdentity(2,"completed-identity"))
        self.assertIs(duplicate.status,RecoveryRequestStatus.DUPLICATE)
        self.assertIs(owner.recovery_cause,StopCause.INPUT_LOST)
        self.assertIsNone(owner.stop_request)

    def test_internal_failure_during_input_loss_replan_has_its_own_cause(self):
        from mc2p.contracts.behavior import BehaviorProfileV0
        from tests.sim.runner import run
        from scripts.action_entry_late_hardening import scenario_for
        reports=[];injected=False
        def step(context):
            nonlocal injected
            context.driver.tick(BehaviorProfileV0(),context.clock[0]+500_000_000)
            if not injected and context.session.report.failure_cause is StopCause.INPUT_LOST:
                context.session.handle_internal_contract_failure("tp0_internal_fault")
                injected=True
            reports.append(context.session.report)
            return ()
        result=run(scenario_for("column_landing_turn",4),control_step=step)
        self.assertTrue(injected)
        self.assertEqual(result.outcome,"failed")
        self.assertEqual(result.reason,"tp0_internal_fault")
        self.assertIs(reports[-1].failure_cause,StopCause.CANCELLED)
        self.assertFalse(result.violations)

    def test_failed_goal_revision_does_not_report_goal_revised_as_failure(self):
        from mc2p.contracts.behavior import BehaviorProfileV0
        from tests.sim.runner import run
        from scripts.action_entry_late_hardening import scenario_for
        reports=[];accepted=False
        inaccessible=GoalState(Aabb(3.4,63.99,9.4,3.6,64.01,9.6),GoalSupport.SOLID,
            frozenset({MovementMode.WALK}),frozenset({"standing"}),.6)
        def step(context):
            nonlocal accepted
            context.driver.tick(BehaviorProfileV0(),context.clock[0]+500_000_000)
            report=context.session.report
            if not accepted and report.state.value=="executing":
                accepted=context.driver.replace_goal(report.goal_id,report.goal_revision+1,inaccessible,context.clock[0])
            reports.append(context.session.report)
            return ()
        result=run(scenario_for("column_landing_turn",None),control_step=step)
        self.assertTrue(accepted)
        self.assertEqual(result.outcome,"failed")
        self.assertEqual(result.reason,"goal_surface_unavailable")
        self.assertIsNone(reports[-1].failure_cause)
        self.assertFalse(result.violations)

    def test_invalidated_air_proof_is_not_reenabled_by_later_cancel(self):
        from mc2p.motion_nav.action_route import ActionRoute,JumpGapSegment
        from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
        from mc2p.motion_nav.jump_gap import JumpGapEdge
        from mc2p.motion_nav.motion_candidate import MotionCandidateAdmitter,MotionCandidateStatus
        from mc2p.motion_nav.motion_risk import TaskDamageBudget
        from mc2p.motion_nav.online_motion import InputApplicationLedger
        from mc2p.motion_nav.support_surfaces import SupportSurface,HorizontalRegion
        from tests.motion_nav.test_b10_gap_solver import fixture
        from tests.motion_nav.test_b10_motion_candidate import (
            solved_candidate, VerifiedMotionRouteIntegrationTests, VerifiedMotionExecutorTests,
        )
        from mc2p.motion_nav.action_route_executor import ActionRouteState
        from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
        from tests.motion_nav.test_jump_up import jump_profile
        from tests.motion_nav.test_b09_air_transitions import air_profile
        anchor,reusable=solved_candidate()
        reusable=replace(reusable,context=replace(reusable.context,action_index=0))
        admitted=MotionCandidateAdmitter().admit(reusable,anchor,planning_request_id="request-1",
            planning_generation=4,goal_id="goal-1",goal_revision=2,route_id="route-1",
            route_revision=3,action_index=0,candidate_revision=5,damage_budget=TaskDamageBudget(),
            intended_start_tick=11,changed_cells=())
        self.assertIs(admitted.status,MotionCandidateStatus.ACCEPTED)
        start_id=SurfaceNodeId(0,0,64,0);end_id=SurfaceNodeId(0,2,64,0)
        surfaces=[SupportSurface(node,(.5,64.,z+.5),HorizontalRegion(0,z,1,z+1),
            1.,("minecraft:grass_block",),()) for node,z in ((start_id,0),(end_id,2))]
        route=ActionRoute("route-1",(JumpGapSegment(
            JumpGapEdge(start_id,end_id,"test-jump-gap",.9,()),*surfaces,()),))
        _,physics_world,_,_=fixture();world=physics_world._world
        executor=ActionRouteExecutor(ground_profile(),jump_profile(),air_profiles=(air_profile(MovementMode.JUMP_GAP),))
        frame_for=VerifiedMotionRouteIntegrationTests.frame
        executor.start(route,frame_for(world,anchor.physics_state,anchor.observation_sequence_id),
            verified_motion=(admitted.candidate,))
        ledger = InputApplicationLedger()
        first = executor.decide(frame_for(world,anchor.physics_state,anchor.observation_sequence_id),
            state_anchor=anchor,input_ledger=ledger)
        self.assertIs(first.state, ActionRouteState.RUNNING)
        self.assertEqual(first.verified_command_index, 0)
        self.assertTrue(first.movement.jump)
        executor.register_verified_submission(0, control_sequence=100, requested_movement_tick=11)
        VerifiedMotionExecutorTests.applied(ledger,anchor,100,11,first.movement)
        airborne=replace(anchor,movement_tick_id=11,observation_sequence_id=anchor.observation_sequence_id+1,
            physics_state=admitted.candidate.proof.trajectory[1])
        self.assertFalse(airborne.physics_state.on_ground)
        changed=(admitted.candidate.proof.world_dependencies[0],)
        current=replace(frame_for(world,airborne.physics_state,airborne.observation_sequence_id),changed_cells=changed)
        discarded=executor.decide(current,state_anchor=airborne,input_ledger=ledger)
        self.assertIs(discarded.state, ActionRouteState.CANCELLING)
        self.assertIsNone(discarded.verified_command_index)
        from mc2p.contracts.action_v1 import MovementV1
        self.assertEqual(discarded.movement, MovementV1())
        executor.request_stop(StopCause.DEPENDENCY_CHANGED)
        executor.request_stop(StopCause.CANCELLED)
        later=replace(airborne,movement_tick_id=12,observation_sequence_id=airborne.observation_sequence_id+1,
            physics_state=replace(airborne.physics_state,movement_tick_id=12))
        stopped=executor.decide(frame_for(world,later.physics_state,later.observation_sequence_id),
            state_anchor=later,input_ledger=ledger)
        self.assertIsNone(stopped.verified_command_index)
        self.assertFalse(stopped.movement.jump)
        self.assertEqual(stopped.movement,discarded.movement)

    def test_session_dependency_change_and_activity_exhaustion_do_not_refine_ending(self):
        from tests.motion_nav.test_navigation_session import (
            NavigationSessionTests, _InlinePlanner, _known_world, _nodes, _source, _goal, _ground_anchor,
        )
        from mc2p.motion_nav.navigation_session import NavigationSession, NavigationSessionState
        from mc2p.motion_nav.navigation_handoff import HandoffDestination
        from mc2p.motion_nav.retry_ledger import RecoveryBudgetPolicy
        from mc2p.motion_nav.online_motion import InputApplicationLedger
        from mc2p.motion_nav.world_model import ObservationStamp
        from mc2p.motion_nav.route_validation import ActiveRouteValidationDisposition
        world = _known_world({(x,0,0):BlockGeometry.full_cube("minecraft:stone") for x in range(4)})
        start, goal = _nodes(world, (0,3))
        now = [1_000_000_000]
        policy = RecoveryBudgetPolicy.persistent(maximum_no_progress_ns=3_000_000_000)
        with patch.object(NavigationSession, "_recovery_budget_policy", return_value=policy):
            session = NavigationSession("tp0-same-frame-limit", NavigationSessionTests().profiles(),
                planner_worker=_InlinePlanner(), clock_ns=lambda: now[0])
            try:
                session.bind_source(_source())
                session.start_goal("tp0-task",1,_goal(goal.position),frame(world,0,start.position))
                initial = frame(world,1,start.position)
                proposal = session.propose(initial,_ground_anchor(initial),now[0]+500_000_000,
                    input_ledger=InputApplicationLedger())
                self.assertIsNotNone(session.active_route)
                changed_cell = (2,1,0)
                self.assertIn(changed_cell,session.active_route.action_route.dependencies)
                now[0] += 3_000_000_000
                world.observe_blocks(ObservationStamp(world.session,2,2,"test-clock",now[0]),
                    {changed_cell:BlockGeometry.full_cube("minecraft:stone")})
                current = replace(frame(world,2,start.position),changed_cells=(changed_cell,))
                with patch.object(session._handoff,"request_recovery",wraps=session._handoff.request_recovery) as requests, \
                     patch.object(session._supervisor,"request_route_stop",wraps=session._supervisor.request_route_stop) as stops:
                    proposal = session.propose(current,_ground_anchor(current),now[0]+500_000_000,
                        input_ledger=InputApplicationLedger())
                self.assertIs(session.diagnostics.route_validation.incumbent.disposition,ActiveRouteValidationDisposition.STOP)
                self.assertIs(proposal.report.state,NavigationSessionState.FAILED)
                self.assertEqual(proposal.report.reason,"task_no_progress_deadline_exhausted")
                self.assertIsNone(proposal.report.failure_cause)
                self.assertIs(stops.call_args_list[-1].args[0], StopCause.MOTION_UNSOLVABLE)
                self.assertTrue(any(call.kwargs.get("destination") is HandoffDestination.FAIL
                    for call in requests.call_args_list))
                self.assertFalse(any(call.kwargs.get("destination") is HandoffDestination.REPLAN
                    for call in requests.call_args_list))
            finally:
                session.close()

    def test_normal_goal_mismatch_still_fails_without_replan(self):
        from mc2p.motion_nav.goal_observation import ObservedGoal,ObservedGoalStatus
        with patch("mc2p.motion_nav.action_route_executor.evaluate_observed_goal",
                   return_value=ObservedGoal(ObservedGoalStatus.NOT_SATISFIED)):
            result=run_case("column_landing_turn",None)
        self.assertEqual(result["reason"],"goal_state_not_satisfied")

if __name__=="__main__": unittest.main()

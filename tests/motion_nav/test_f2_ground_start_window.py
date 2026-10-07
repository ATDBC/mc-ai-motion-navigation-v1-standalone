"""A delayed ordinary Walk may reuse a proved stationary entry, not a new lease."""
from dataclasses import asdict,replace
import math
import unittest
from unittest.mock import patch

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1,LookV1,MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import ControlFrameProposalV1,OrderedIntentV1,ordered_intent_id
from mc2p.contracts.reset import ResetRequestV0
from mc2p.motion_nav.action_route import WalkSegment
from mc2p.motion_nav.action_route_executor import _ordinary_walk_start_window
from mc2p.motion_nav.fixed_route import FixedRoute,FixedRouteController,RoutePoint
from mc2p.motion_nav.navigation_session import NavigationSession,NavigationSessionProfiles
from mc2p.motion_nav.support_surfaces import SurfaceNodeId
from mc2p.motion_nav.online_motion import InputApplicationLedger,InputApplicationStatus
from tests.motion_nav.test_b10_online_motion import SESSION,action,sample
from mc2p.motion_nav.world_model import WorldQueryCache
from scripts.f2_ground_route_evidence import ROOT,_frame
from tests.sim.backend import CalculatorBackend
from tests.sim.product_cases import Scene
from tests.sim.runner import InlinePlannerWorker,InlineMotionWorker,seed_memory,_goal
from tests.test_player_runtime import _RecordingTrace,_task
from mc2p.runtime.player_runtime_v1 import PlayerRuntimeV1
from mc2p.skills.navigation_session_driver import RuntimeNavigationDriver
from mc2p.motion_nav import action_route_executor as executor_module


class F2GroundStartWindowTests(unittest.TestCase):
    def setUp(self):
        scene=Scene({(x,63,z):"minecraft:stone" for x in range(-3,4) for z in range(-2,12)},
                    ((-4,4),(60,68),(-3,13)))
        self.backend=CalculatorBackend([0],scene,(.73,64.,.67))
        self.backend.state=replace(self.backend.state,
            velocity_blocks_per_tick=(0.,-.0784000015258789,0.))
        self.profile=NavigationSessionProfiles.load(ROOT/"config/motion-navigation").ground
        self.frame=_frame(self.backend.state,self.backend.world._world,1)
        self.action=WalkSegment(FixedRoute("f2-multi-point-start",(
            RoutePoint(.73,64.,.67),RoutePoint(.5,64.,1.5),
            RoutePoint(.5,64.,4.5),RoutePoint(.73,64.,8.33))),
            (SurfaceNodeId(0,0,64,0),SurfaceNodeId(0,8,64,0)),())
        controller=FixedRouteController(self.profile)
        controller.start(self.action.fixed_route,self.frame)
        self.decision=controller.decide(self.frame,physics_state=self.backend.state)

    def window(self,**changes):
        values=dict(physics_state=self.backend.state,profile=self.profile,
                    query_cache=WorldQueryCache(self.frame.world),first_command=True)
        values.update(changes)
        return _ordinary_walk_start_window(self.action,self.decision,self.frame,**values)

    def test_stationary_multi_point_candidate_gets_one_late_start(self):
        self.assertEqual(self.window(),(1,2))
        self.assertEqual(self.decision.full_candidates,0)

    def test_on_time_late_one_and_late_two_use_the_same_single_tick_lease(self):
        first,latest=self.window()
        for offset,expected in ((0,InputApplicationStatus.APPLIED),
                (1,InputApplicationStatus.APPLIED),(2,InputApplicationStatus.APPLIED_OUTSIDE_WINDOW)):
            with self.subTest(offset=offset):
                ledger=InputApplicationLedger()
                submitted=ledger.submit(SESSION,action(1),requested_first_tick=first,
                    latest_allowed_first_tick=latest)
                self.assertEqual(submitted.requested_last_tick,first)
                observed=ledger.observe_sample(sample(1,first+offset))
                self.assertIs(observed.status,expected)

    def test_only_first_command_and_matching_stationary_state_get_slack(self):
        for change in ({"first_command":False},{"physics_state":None},
                {"physics_state":replace(self.backend.state,position=(.8,64.,.67))},
                {"physics_state":replace(self.backend.state,velocity_blocks_per_tick=(.01,-.0784,0.))}):
            with self.subTest(change=change):self.assertIsNone(self.window(**change))

    def test_neutral_prefix_that_moves_or_changes_collision_gets_no_slack(self):
        self.frame=replace(self.frame,body=replace(self.frame.body,vertical_collision=False))
        state=replace(self.backend.state,vertical_collision=False)
        self.assertIsNone(self.window(physics_state=state))

    def test_rejected_wall_or_unproved_edge_candidate_gets_no_slack(self):
        for kind in ("wall","edge"):
            with self.subTest(kind=kind):
                solids={(x,63,z):"minecraft:stone" for x in range(-3,4)
                    for z in range(-2,1 if kind=="edge" else 6)}
                if kind=="wall":
                    solids.update({(x,y,1):"minecraft:stone" for x in range(-3,4) for y in (64,65,66)})
                backend=CalculatorBackend([0],Scene(solids,((-4,4),(60,68),(-3,7))),(.73,64.,.7))
                state=replace(backend.state,velocity_blocks_per_tick=(0.,-.0784000015258789,0.))
                self.frame=_frame(state,backend.world._world,1)
                self.action=WalkSegment(FixedRoute("f2-negative-start",(
                    RoutePoint(.73,64.,.7),RoutePoint(.73,64.,3.5))),
                    (SurfaceNodeId(0,0,64,0),SurfaceNodeId(0,3,64,0)),())
                controller=FixedRouteController(self.profile)
                controller.start(self.action.fixed_route,self.frame)
                self.decision=controller.decide(self.frame,physics_state=state)
                window=self.window(physics_state=state)
                if kind=="wall":
                    # A stationary release can still precede the controller's
                    # existing safe backward escape. It cannot authorize the
                    # rejected forward command through the frontal wall.
                    self.assertLessEqual(self.decision.movement.forward,0)
                else:
                    self.assertIsNone(window,str(self.decision))


class F2GroundStartFormalChainTests(unittest.TestCase):
    """The owner generates a window before Runtime chooses and applies input."""

    def assert_formal_start(self, *, late=0, look_wins=True):
        clock=[100_000_000]
        scene=Scene({(x,63,z):"minecraft:stone" for x in range(-3,4) for z in range(-2,14)},
                    ((-4,4),(60,68),(-3,15)))
        backend=CalculatorBackend(clock,scene,(.73,64.,.67))
        # Settle through the existing physics entry, never invent a state anchor.
        backend.advance(MovementV1())
        runtime=PlayerRuntimeV1(backend,_RecordingTrace(),lambda:clock[0])
        session=None
        try:
            self.assertTrue(runtime.reset(ResetRequestV0(
                "f2-start-reset",backend.episode,"test",1,10_000_000_000)).succeeded)
            seed_memory(runtime,scene)
            session=NavigationSession("f2-start-chain",NavigationSessionProfiles.load(
                ROOT/"config/motion-navigation"),planner_worker=InlinePlannerWorker(),
                motion_worker=InlineMotionWorker(),clock_ns=lambda:clock[0])
            driver=RuntimeNavigationDriver(runtime,session,clock_ns=lambda:clock[0])
            # Longer than direct admission: the real planner must supply a
            # multi-point ordinary route, which exposed the original defect.
            driver.start("f2-start-goal",1,_goal((.73,64.,10.33)),clock[0])
            deadline=clock[0]+500_000_000
            source=runtime.register_ordered_source("f2-planned-look")
            look=OrderedIntentV1(source,1,ActionIntentV1(
                ordered_intent_id(source,1),source.source_id,source.episode_id,
                runtime.observation.sequence_id,ActionPriorityV0.TASK,clock[0],deadline,
                look=LookV1(15.,0.)))
            # Wrap the real calculator only to inspect its input. All output
            # and admission decisions still come from production code.
            with patch.object(executor_module,"verified_ground_route_candidate",
                    wraps=executor_module.verified_ground_route_candidate) as prefix, \
                    patch.object(executor_module,"_ordinary_walk_start_window",
                    wraps=executor_module._ordinary_walk_start_window) as start:
                proposals=driver.prepare_proposals(deadline,conditioned_look=look)
            generated=[e.intent for p in proposals for e in p.intents
                if e.intent.movement is not None and e.intent.movement != MovementV1()]
            self.assertEqual(len(generated),1)
            movement=generated[0]
            self.assertEqual(movement.movement_conditioned_look_intent_id,look.intent.intent_id)
            window=movement.movement_tick_window
            self.assertIsNotNone(window,"Session/Executor did not generate the first-start window")
            self.assertEqual(window.latest_tick,window.earliest_tick+1)
            self.assertEqual(movement.valid_for_ticks,1)
            self.assertEqual(prefix.call_count,1)
            self.assertEqual(start.call_count,1)
            self.assertGreater(len(start.call_args.args[0].fixed_route.points),2)
            proved_frame,proved_state,command=prefix.call_args.args
            self.assertEqual(command,MovementV1())
            self.assertAlmostEqual(proved_state.yaw_radians,math.radians(15.))
            self.assertEqual(proved_frame.body.yaw_radians,proved_state.yaw_radians)
            self.assertEqual(prefix.call_args.kwargs["control_ticks"],1)
            self.assertEqual(prefix.call_args.kwargs["tail_ticks"],0)
            extras=(ControlFrameProposalV1(intents=(look,)),)
            if not look_wins:
                other=runtime.register_ordered_source("f2-player-look")
                steal=OrderedIntentV1(other,1,ActionIntentV1(
                    ordered_intent_id(other,1),other.source_id,other.episode_id,
                    runtime.observation.sequence_id,ActionPriorityV0.PLAYER,clock[0],deadline,
                    look=LookV1(-20.,0.)))
                extras+=(ControlFrameProposalV1(intents=(steal,)),)
            for _ in range(late):
                backend.free_tick()
            result=runtime.control_frame(_task(deadline),BehaviorProfileV0(),deadline,
                proposals=proposals+extras)
            driver.adopt_result(result)
            self.assertIsNone(result.report.failure)
            record=runtime.input_ledger.record(result.decision.action.request_sequence_id)
            self.assertIsNotNone(record)
            self.assertEqual(record.action.valid_for_ticks,1)
            if look_wins:
                self.assertEqual(result.decision.movement_tick_window,window)
                self.assertEqual(result.decision.action.movement,movement.movement)
                self.assertEqual(record.requested_first_tick,window.earliest_tick)
                self.assertEqual(record.latest_allowed_first_tick,window.latest_tick)
                self.assertEqual(record.applied_ticks,(window.earliest_tick+late,))
                self.assertIs(record.status,InputApplicationStatus.APPLIED_OUTSIDE_WINDOW
                    if late==2 else InputApplicationStatus.APPLIED)
            else:
                self.assertEqual(result.decision.action.movement,MovementV1())
                self.assertIsNone(result.decision.movement_tick_window)
                self.assertEqual(record.latest_allowed_first_tick,record.requested_first_tick)
                self.assertIs(record.status,InputApplicationStatus.APPLIED)
                self.assertTrue(any(s[1]=="conditioned_look_not_selected"
                    for s in result.decision.suppressed_intents))
            return {"late_ticks":late,"planned_look_won":look_wins,
                "generated_intent":asdict(movement),
                "formal_prefix_yaw_radians":proved_state.yaw_radians,
                "adopted_window":None if result.decision.movement_tick_window is None
                    else asdict(result.decision.movement_tick_window),
                "actual_action":asdict(result.decision.action),
                "actual_record":asdict(record)}
        finally:
            if session is not None:
                session.close()
            runtime.close()

    def test_planned_look_wins_and_first_move_applies_on_time(self):
        self.assert_formal_start()

    def test_planned_look_wins_and_first_move_applies_one_tick_late(self):
        self.assert_formal_start(late=1)

    def test_first_move_two_ticks_late_is_outside_generated_window(self):
        self.assert_formal_start(late=2)

    def test_losing_planned_look_sends_neutral_and_adopts_no_start_window(self):
        self.assert_formal_start(look_wins=False)


if __name__=="__main__":unittest.main()

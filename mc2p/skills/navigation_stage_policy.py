"""Stage navigation revision 3, retaining the measured J3 control body."""
import math
from dataclasses import replace

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.skills.navigation_joint_policy import JointPlanner, ObservationNeed, _yaw
from mc2p.skills.navigation_look import wrap
from mc2p.skills.navigation_stage_goals import StageGoalSelector, known_segment
from mc2p.skills.navigation_stage_precision import native_safe_gaze


from mc2p.skills.navigation_stage_routes import StageRoutes


class StageJointPlanner(JointPlanner):
    stage_mode=True

    def __init__(self,*args,**kwargs):
        if kwargs.get('force_route_id') is not None:
            raise ContractViolation('stage revision does not reuse old forced-route cost trials')
        super().__init__(*args,**kwargs)
        self.stage=StageRoutes(static_history=self.static_history)
        self._routes=self.stage
        self._stage_binding=None
        self._stage_checkpoint=None
        self._last_feedback_ns=None
        self._stage_releases=[]

    def clear(self):
        super().clear()
        self.stage.clear()
        self._stage_binding=None
        self._stage_checkpoint=None
        self._last_feedback_ns=None
        self._stage_releases=[]

    def decide(self,snapshot,view,goal,now_ns,*,belief,memory_generation):
        binding=(memory_generation,snapshot.scope_id,
            None if snapshot.latest is None else snapshot.latest.stamp.scope,goal.goal_id,goal.position)
        if binding!=self._stage_binding:
            self.clear();self._stage_binding=binding
        self.stage.work=dict(mode='idle',cell_hits=0,cell_misses=0)
        self._stage_releases=[]
        if snapshot.latest is not None:self.stage.selector.bind(snapshot,goal)
        self._stage_checkpoint=self.stage.checkpoint()
        return super().decide(snapshot,view,goal,now_ns,belief=belief,memory_generation=memory_generation)

    def begin_frame(self):
        super().begin_frame()
        self._stage_diagnostic()

    def _stage_diagnostic(self):
        self._diagnostic['joint_revision']=3
        if getattr(self,'navigation_strategy',None) is not None:
            self._diagnostic['navigation_strategy']=self.navigation_strategy
        details=self.stage.selector.diagnostic if (self._diagnostic.get('planned') and not self.stage.scanning
            and not self._diagnostic.get('budget_exhausted') and self.stage.work['mode'] in ('built','reused','fallback')) else {}
        self._diagnostic['stage_goal']=dict(details,
            scanning=self.stage.scanning,scan_degrees=self.stage.scan_degrees)
        self._diagnostic['stage_work']=dict(self.stage.work)

    def _finish(self,*args,**kwargs):
        decision=super()._finish(*args,**kwargs)
        released=(self.selected is not None and any(self.selected.proposal is p for p in self._stage_releases))
        self._stage_releases=[]
        if self._diagnostic['budget_exhausted'] and self._stage_checkpoint is not None:
            self.stage.restore(self._stage_checkpoint)
        self._stage_diagnostic()
        if self._cache is not None:self._cache['planning_summary']=self.diagnostic
        if decision.reason=='no_admissible_candidate':
            # The geometric corridor can remain clear while correcting toward
            # an old control line would hit a wall. Replan/rebase after rejection
            # instead of retrying the same cached reference indefinitely.
            self.stage.plan=None;self.stage.tail=()
            self._route_origin=self._route_endpoint=None
        elif released:
            # Keep the checked route, but the rejected moving command must not
            # make its old tracking line survive through a stationary turn.
            self._route_origin=self._route_endpoint=None
        return decision

    def _connector(self,snapshot,view,now_ns,waypoint,belief):
        if (not self.stage.scanning and self.stage.now_ns==now_ns and self.stage.plan is not None
                and self.stage.plan.waypoint==waypoint and self.stage.plan.connector is not None):
            return self.stage.plan.connector
        return known_segment(snapshot,view,now_ns,view.base.own.position,waypoint,belief,static_history=self.static_history)

    def _gazes_and_need(self,snapshot,view,waypoint,travel_yaw,goal):
        if self.stage.scanning:
            return self._gaze_candidates(snapshot,view,waypoint,()),None
        if waypoint!=view.base.own.position:
            return [('front',travel_yaw,45.,None)],None
        return super()._gazes_and_need(snapshot,view,waypoint,travel_yaw,goal)

    def _guard_candidate(self,snapshot,view,now_ns,proposal,memory_generation,belief):
        proposal,report=super()._guard_candidate(snapshot,view,now_ns,proposal,memory_generation,belief)
        if (report.reason is not None and proposal.movement!=MovementV1()
                and (proposal.look.yaw_delta_degrees or proposal.look.pitch_delta_degrees)):
            axes='yaw' if proposal.look.yaw_delta_degrees else 'pitch'
            if proposal.look.yaw_delta_degrees and proposal.look.pitch_delta_degrees:axes='yaw_pitch'
            if self.capabilities.allows(MovementV1(),axes):
                released=replace(proposal,movement=MovementV1(),endpoint=proposal.origin)
                self._stage_releases.append(released)
                return super()._guard_candidate(snapshot,view,now_ns,released,memory_generation,belief)
        return proposal,report

    def _make_needs(self,snapshot,view,goal,now_ns,routes,summary,belief):
        if self.stage.scanning or self.stage.plan is None:return ()
        return tuple(ObservationNeed('terrain/'+'/'.join(map(str,block)),block,
            self.stage.selector.need_reasons.get(block,'missing_support'),0,
            min(goal.deadline_ns,now_ns+1_000_000_000),
            now_ns if snapshot.terrain_index.get(block) is None else snapshot.terrain_index[block].last_seen.request_start_ns)
            for block in self.stage.plan.information[:8])

    def _gaze_candidates(self,snapshot,view,waypoint,gazes):
        own=view.base.own
        if self.stage.scanning:
            return [('front',own.yaw+min(3.,360.-self.stage.scan_degrees),own.pitch,None)]
        if waypoint==own.position:
            if self.stage.idle_since is not None and self.stage.now_ns-self.stage.idle_since>=2_000_000_000:
                return [('hold',own.yaw,own.pitch,None)]
            needs=[g for g in gazes if g[0]=='need']
            return needs or [('front',own.yaw+3.,own.pitch,None)]
        # Keep the continuous route as the reference. A useful turn exists
        # independently of whether some unrelated remembered block expires.
        front=next(g for g in gazes if g[0]=='front')
        return [front]

    def _proposal(self,snapshot,view,waypoint,yaw,pitch,generation):
        proposal=super()._proposal(snapshot,view,waypoint,yaw,pitch,generation)
        if proposal is not None:
            safe=native_safe_gaze(view.base.own,proposal.look)
            if safe is not None:
                proposal=super()._proposal(snapshot,view,waypoint,*safe,generation)
        if (proposal is not None and waypoint!=view.base.own.position
                and proposal.movement==MovementV1()
                and proposal.look.yaw_delta_degrees==proposal.look.pitch_delta_degrees==0):
            return None
        return proposal

    def feedback(self,selected,snapshot,view,now_ns):
        previous=self.selected
        if previous is not None or selected:
            self._previous_movement=previous.proposal.movement if selected and previous is not None else MovementV1()
        # Turning, rejected commands and budget holds are not failed route
        # traversal. Keep their elapsed time out of the no-progress clock.
        if (self._last_feedback_ns is not None and self.stage.selector._progress_at is not None
                and (not selected or previous is None or previous.proposal.movement==MovementV1())):
            self.stage.selector._progress_at+=max(0,now_ns-self._last_feedback_ns)
        self._last_feedback_ns=now_ns
        stamp=None if snapshot.latest is None else snapshot.latest.stamp
        if (self.stage.scanning and selected and previous is not None and view.base.own is not None
                and previous.proposal.look.yaw_delta_degrees>0 and stamp!=self.stage._feedback_stamp):
            delta=wrap(view.base.own.yaw-previous.proposal.yaw)
            if 0<delta<=6.:
                self.stage.scan_degrees=min(360.,self.stage.scan_degrees+delta)
                if self.stage.scan_degrees>=360.-1e-6:self.stage.selector.clear()
            self.stage._feedback_stamp=stamp
        feedback=super().feedback(selected,snapshot,view,now_ns)
        # A release clears the tracking reference before feedback, so it must
        # report the measured old-line offset without penalizing that route as
        # though its rejected moving proposal had been executed.
        route=self._diagnostic.get('original_route')
        if (previous is not None and previous.proposal.movement==MovementV1()
                and feedback['lateral_error_blocks'] is None and route is not None and view.base.own is not None):
            origin,end=route;position=view.base.own.position
            dx,dz=end.x-origin.x,end.z-origin.z;length=math.hypot(dx,dz)
            feedback['lateral_error_blocks']=(position.x-origin.x if length==0 else
                ((position.x-origin.x)*dz-(position.z-origin.z)*dx)/length)
            self._last_feedback=dict(feedback)
        return feedback


class StaticTerrainStagePlanner(StageJointPlanner):
    """Named strategy entry; reuse the verified stage implementation and body."""
    from mc2p.skills.navigation_strategy import GOAL_DIRECTED_EXPLORATION as navigation_strategy
    static_history=True

    def __init__(self,**kwargs):
        from mc2p.skills.navigation_terrain_review import TerrainReviewQueue
        self.terrain_review=TerrainReviewQueue()
        super().__init__('E',**kwargs)
        from mc2p.skills.navigation_strategy import strategy_config
        self.group=self.navigation_strategy
        self.config=strategy_config(self.navigation_strategy)

    def clear(self):
        super().clear()
        self.terrain_review.clear()
        self._review_belief=None
        self._review_checkpoint=None
        self._review_evaluated=False
        self._review_path=()

    def decide(self,*args,**kwargs):
        self._review_belief=None;self._review_checkpoint=None
        self._review_evaluated=False;self._review_path=()
        return super().decide(*args,**kwargs)

    def _stage_diagnostic(self):
        super()._stage_diagnostic()
        self._diagnostic['joint_revision']=4
        self._diagnostic['terrain_review']=dict(evaluated=getattr(self,'_review_evaluated',False),
            path=getattr(self,'_review_path',()),state_sha256=self.terrain_review.state_digest())

    def _make_needs(self,snapshot,view,goal,now_ns,routes,summary,belief):
        information=super()._make_needs(snapshot,view,goal,now_ns,routes,summary,belief)
        if self.stage.scanning or self.stage.plan is None:return information
        import copy
        self._review_belief=belief
        self._review_evaluated=True;self._review_path=self.stage.plan.path
        self._review_checkpoint=copy.deepcopy(self.terrain_review.__dict__)
        speed,_=self._timing('normal_speed_blocks_per_second',6.)
        speed=max(speed,20*math.hypot(view.base.own.velocity.x,view.base.own.velocity.z))
        delivery,_=self._timing('sample_delivery_ns',1_000_000_000)
        rate,_=self._timing('look_rate_degrees_per_second',60.)
        review=self.terrain_review.update(snapshot,view,now_ns,self.stage.plan.path,belief=belief,
            goal_deadline_ns=goal.deadline_ns,speed_blocks_per_second=speed,
            delivery_ns=int(delivery),look_rate_degrees_per_second=rate)
        return (review+information)[:8]

    def _finish(self,*args,**kwargs):
        decision=super()._finish(*args,**kwargs)
        if self._diagnostic.get('budget_exhausted') and getattr(self,'_review_checkpoint',None) is not None:
            self.terrain_review.__dict__.update(self._review_checkpoint)
        elif not self._diagnostic.get('budget_exhausted') and getattr(self,'_review_belief',None) is not None:
            self.terrain_review.commit_holds(self._review_belief,self.stage.now_ns)
        self._review_checkpoint=None
        self._review_belief=None
        self._diagnostic['terrain_review']['state_sha256']=self.terrain_review.state_digest()
        if self._cache is not None:self._cache['planning_summary']=self.diagnostic
        return decision

    def _gazes_and_need(self,snapshot,view,waypoint,travel_yaw,goal):
        if self.terrain_review.last_needs and not self.stage.scanning:
            return JointPlanner._gazes_and_need(self,snapshot,view,waypoint,travel_yaw,goal)
        return super()._gazes_and_need(snapshot,view,waypoint,travel_yaw,goal)

    def _gaze_candidates(self,snapshot,view,waypoint,gazes):
        if self.terrain_review.last_needs and not self.stage.scanning:
            useful=[g for g in gazes if g[0] in ('need','reposition')]
            front=[g for g in gazes if g[0]=='front']
            return (useful+front)[:3] or super()._gaze_candidates(snapshot,view,waypoint,gazes)
        return super()._gaze_candidates(snapshot,view,waypoint,gazes)

    def _guard_candidate(self,snapshot,view,now_ns,proposal,memory_generation,belief):
        if self.terrain_review.due(now_ns) and proposal.movement!=MovementV1():
            proposal=replace(proposal,movement=MovementV1(),endpoint=proposal.origin)
            self._stage_releases.append(proposal)
        return super()._guard_candidate(snapshot,view,now_ns,proposal,memory_generation,belief)

    def revalidate(self,decision,snapshot,view,now_ns,*,belief,memory_generation):
        checked=super().revalidate(decision,snapshot,view,now_ns,belief=belief,memory_generation=memory_generation)
        if checked.movement!=MovementV1() and self.terrain_review.due(now_ns):
            from mc2p.skills.navigation_controller import NavigationDecision
            self._diagnostic['submit_rejection']='terrain_review_due'
            self.selected=None
            return NavigationDecision(state='blocked',reason='joint_recheck_terrain_review_due')
        return checked


from mc2p.skills.navigation_execution_policy import NavigationExecutionMixin


class GoalDirectedExplorationPlanner(NavigationExecutionMixin, StaticTerrainStagePlanner):
    """Named navigation with coordinated execution and bounded local recovery."""

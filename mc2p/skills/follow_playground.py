"""Flat-world controller using only restricted lawful observations, never a world handle.

Observed block checks are a conservative short movement guard, not a proof that
unknown volumes are air. Block collisions stop control. Jump checks extend the
same guard to the full expected normal jump corridor and landing footprint.
"""
from dataclasses import dataclass, replace
import math
from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.follow_gaits import AutoGaitConfig, AutoGaitSelector, FixedDistanceGate, fixed_movement
from mc2p.skills.follow_playground_types import PlaygroundView, TrackedTarget
from mc2p.skills.follow_tracking import TargetTracker
from mc2p.skills.follow_types import MOVEMENT_FRESHNESS_NS
from mc2p.skills.local_navigation import LocalBlockMemory, _support
from mc2p.skills.motion_guard import inspect_playground_motion
from mc2p.skills.navigation_controller import NavigationController
from mc2p.skills.navigation_motion import NavigationBlockMap
from mc2p.skills.target_belief import TargetBelief
from mc2p.skills.target_search import TargetSearch, SearchReport
from mc2p.skills.active_perception import ActivePerceptionCoordinator
from mc2p.skills.perception_needs import PerceptionConfig, PerceptionNeed
from mc2p.skills.perception_candidates import make_needs
from mc2p.skills.navigation_evidence import NavigationEvidence


def wrap(angle: float) -> float:
    return (angle+180)%360-180


@dataclass(frozen=True, slots=True)
class LocalRetreat:
    goal: Vec3V0
    origin: Vec3V0
    started_at_ns: int
    observation_sequence_id: int


@dataclass(frozen=True, slots=True)
class PlaygroundDecision:
    movement: MovementV1
    look: LookV1
    state: str
    reason: str
    target: TrackedTarget
    distance: float | None
    requested_mode: str
    selected_gait: str | None = None
    movement_requires_look: bool = True
    closing_speed: float = 0
    own_speed: float = 0
    speed_estimate_missing: bool = True
    navigation_mode: str = 'legacy_v1'
    local_retreat: LocalRetreat | None = None
    cognition_mode: str = 'legacy_tracking'
    search_report: SearchReport | None = None
    target_observation_age_ns: int | None = None
    target_history_size: int = 0
    observation_check_count: int = 0


def guard_playground_motion(memory: LocalBlockMemory, view: PlaygroundView, now_ns: int,
                            floor: int | None, yaw: float, *, jump: bool = False,
                            allowed_player_contact: str | None = None) -> str | None:
    """None permits the declared bounded checks; any reason requires neutral."""
    return inspect_playground_motion(memory, view, now_ns, floor, yaw, jump=jump,
                                     allowed_player_contact=allowed_player_contact).reason


class PlaygroundFollower:
    def __init__(self, task_id: str, initial_view: PlaygroundView, mode: str = 'auto', *,
                 perception_variant='m6_baseline', perception_config=None) -> None:
        require_identifier(task_id,'playground task id')
        self.task_id = task_id
        self.tracker = TargetTracker()
        self._target = self.tracker.bind_unique(initial_view,initial_view.base.received_at_ns)
        self.memory = LocalBlockMemory()
        if perception_variant not in {'m6_baseline','smooth_only','active_perception_v1'}:
            raise ContractViolation('unknown_perception_variant')
        self.perception_variant=perception_variant
        if perception_config is not None and type(perception_config) is not PerceptionConfig:
            raise ContractViolation('follower requires PerceptionConfig')
        self.perception=None if perception_variant=='m6_baseline' else ActivePerceptionCoordinator(
            perception_variant,perception_config or PerceptionConfig())
        self.navigator = NavigationController() if self.perception is None else NavigationController(perception=self.perception)
        self.search = TargetSearch() if self.perception is None else TargetSearch(perception=self.perception)
        self.belief = None
        self._belief_view = None
        self._search_report = None
        self._retreat = None
        self._navigation_mode = 'legacy_v1'
        self.gate = FixedDistanceGate()
        self.auto_config = AutoGaitConfig()
        self._auto = AutoGaitSelector(self.auto_config)
        self._speed_sample = None
        self._speeds = (0.,0.,True)
        self._mode = 'auto'
        self.set_mode(mode)
        self._cancelled = None
        self._last_sequence = -1
        self._last_decision_ns = None
        self._floor = None
        self._high_since = None
        self._ground_scans = 0
        self._search_origin = None
        self._search_index = 0
        self.last_decision = None
        self.last_execution_confirmed = None

    @property
    def mode(self) -> str: return self._mode

    def set_mode(self, mode: str) -> None:
        if mode != 'auto': fixed_movement(mode)
        if mode != self._mode:
            self.clear_navigation()
            self._speed_sample = None
            self._speeds = (0.,0.,True)
            self._auto = AutoGaitSelector(self.auto_config)
        self._mode = mode

    def set_distance(self, distance_blocks: float) -> None:
        if self.mode!='auto': raise ContractViolation('distance_requires_auto')
        config = replace(self.auto_config,target_distance=distance_blocks)
        self.auto_config,self._auto = config,AutoGaitSelector(config)

    def _estimate_speeds(self, view: PlaygroundView, distance: float) -> None:
        base = view.base
        sample = (distance,base.own.position,base.received_at_ns)
        old = self._speed_sample
        self._speeds = (0.,0.,True)
        if old is not None and 0 < base.received_at_ns-old[2] <= MOVEMENT_FRESHNESS_NS:
            seconds = (base.received_at_ns-old[2])/1_000_000_000
            self._speeds = ((old[0]-distance)/seconds,
                math.hypot(sample[1].x-old[1].x,sample[1].z-old[1].z)/seconds,False)
        self._speed_sample = sample

    def cancel(self, reason: str) -> None:
        if not isinstance(reason,str) or not reason: raise ContractViolation('missing cancel reason')
        self._cancelled = reason
        self._speed_sample,self._speeds = None,(0.,0.,True)
        self.memory.clear()
        self.clear_navigation()

    def clear_navigation(self, *, clear_perception=True) -> None:
        self.navigator.clear()
        self._retreat = None
        self.search.interrupt()
        if clear_perception and self.perception is not None:
            self.perception.clear_control('follow_control_released')

    def bind_navigation(self, snapshot, now_ns) -> None:
        if self.belief is not None:
            raise ContractViolation('cognition already bound')
        self.belief=TargetBelief(self._target.track_id,snapshot,now_ns)
        self._belief_view=self.belief.view(now_ns)

    def _local_retreat(self, navigation, view, now_ns):
        goal,origin,started = self._retreat.goal,self._retreat.origin,self._retreat.started_at_ns
        own=view.base.own
        if (now_ns-started>=2_000_000_000
                or math.hypot(own.position.x-origin.x,own.position.z-origin.z)>1.5):
            self.clear_navigation()
            return self._result('waiting_target','local_retreat_expired')
        result=self.navigator.decide(navigation,view,now_ns,goal,MovementV1(forward=1),self._floor)
        if result.reason=='local_waypoint_reached':
            self.clear_navigation()
        return self._result(result.state,'local_retreat_segment' if result.movement.forward else result.reason,
                            movement=result.movement,look=result.look,gait='normal')

    def _result(self, state: str, reason: str, *, distance: float | None = None,
                movement: MovementV1 = MovementV1(), look: LookV1 = LookV1(),
                gait: str | None = None) -> PlaygroundDecision:
        self.last_decision = PlaygroundDecision(movement,look,state,reason,self._target,distance,self.mode,gait,
            True,*self._speeds,self._navigation_mode,self._retreat,
            'legacy_tracking' if self.belief is None else 'm4_search_v1',self._search_report,
            None if self._belief_view is None else self._belief_view.last_seen_age_ns,
            0 if self._belief_view is None else len(self._belief_view.samples),
            0 if self._belief_view is None else len(self._belief_view.checks))
        return self.last_decision

    @staticmethod
    def _look(view: PlaygroundView, yaw: float, pitch: float) -> LookV1:
        own = view.base.own
        dy,dp = wrap(yaw-own.yaw),pitch-own.pitch
        return LookV1(0 if abs(dy)<=8 else max(-15,min(15,dy)),
                      0 if abs(dp)<=6 else max(-10,min(10,dp)))

    def _task_look(self,view,navigation,now_ns,yaw,pitch,*,preaim=False):
        if self.perception is None:
            return self._look(view,yaw,pitch)
        if self.perception_variant=='smooth_only':
            return self.perception.command_legacy(view,yaw,pitch,now_ns,owner='follow_track')
        needs=make_needs(navigation,view,None,None,target_track_id=self._target.track_id,
            aim=None,now_ns=now_ns,deadline_ns=now_ns+250_000_000,config=self.perception.config)
        if preaim and self._target.position is not None:
            point=self.navigator.preview_heading(navigation,view,now_ns,self._target.position,self._floor,
                target_track_id=self._target.track_id,stop_distance=2.)
            if point is not None:
                # A flat-route preview aims ahead, not down at its ground point.
                # This is only a look suggestion; no future movement is granted.
                point=Vec3V0(point.x,view.base.own.position.y+1.62,point.z)
                need=PerceptionNeed('follow-preaim','follow_track',navigation.scope_id,
                    navigation.latest.stamp,'track',80,now_ns+250_000_000,point,'filtered_check',
                    track_id=self._target.track_id)
                needs=(need,)+needs
        return self.perception.decide(navigation,view,needs,None,None,now_ns,
                                     now_ns+250_000_000).look

    def cognition_status(self,now_ns):
        """Historical ages and persistent budget, without mutating belief/search."""
        require_nonnegative_int(now_ns,'cognition status time')
        belief=self._belief_view
        last=None if belief is None else belief.last_seen
        return dict(mode='legacy_tracking' if self.belief is None else 'm4_search_v1',
            phase=None if self.last_decision is None else self.last_decision.state,
            reason=None if self.last_decision is None else self.last_decision.reason,
            target_observation_age_ns=None if last is None else max(0,now_ns-last.stamp.request_start_ns),
            target_history_size=0 if belief is None else len(belief.samples),
            observation_check_count=0 if belief is None else len(belief.checks),search=self.search.status(now_ns))

    def decide(self, view: PlaygroundView, now_ns: int, *, navigation=None) -> PlaygroundDecision:
        require_nonnegative_int(now_ns,'decision time')
        if self.perception is not None and navigation is None:
            raise ContractViolation('new_perception_requires_formal_navigation')
        self._navigation_mode='legacy_v1' if navigation is None else 'm2_local_v1'
        if self._cancelled: return self._result('cancelled',self._cancelled)
        self._search_report=None
        if self.belief is None:
            self._target = target = self.tracker.observe(view,now_ns)
        else:
            if navigation is None: raise ContractViolation('formal cognition requires navigation memory')
            purpose=('route_check' if self.navigator.gate.pending is not None and self.last_execution_confirmed
                     else 'search' if self.search.started is not None else 'track')
            self._belief_view=self.belief.update(navigation,now_ns,purpose=purpose)
            entity=self._belief_view.visible_entity
            last=self._belief_view.last_seen
            self._target=target=TrackedTarget('visible' if entity else 'remembered' if last else 'lost',
                self.belief.track_id,None if entity is None else entity.position,None if entity is None else entity.size,
                None if last is None else last.stamp.received_at_ns)
            if entity is not None: self.search.clear()
        # Observation validity is independent of whether a new control is due.
        if target.status!='visible': self._speed_sample,self._speeds = None,(0.,0.,True)
        base,own = view.base,view.base.own
        if target.status=='invalidated':
            self.cancel(target.reason)
            return self._result('cancelled',target.reason)
        if not base.available or own is None or not 0 <= now_ns-base.received_at_ns <= MOVEMENT_FRESHNESS_NS:
            self.clear_navigation()
            self._speed_sample,self._speeds = None,(0.,0.,True)
            return self._result('waiting_target','stale_or_missing_observation')
        if own.dead or base.gui_open:
            self.cancel('player_dead' if own.dead else 'gui_open')
            return self._result('cancelled',self._cancelled)
        if own.unsupported_motion or view.status_effect_ids:
            self.clear_navigation()
            self._speed_sample,self._speeds = None,(0.,0.,True)
            return self._result('blocked','unsupported_motion_or_effects')
        if base.sequence_id==self._last_sequence:
            return self._result('waiting_observation','awaiting_new_observation')
        if self._last_decision_ns is not None and now_ns-self._last_decision_ns<50_000_000:
            return self._result('waiting_observation','control_interval')
        self._last_sequence,self._last_decision_ns = base.sequence_id,now_ns
        # No silent runtime fallback: formal Driver always supplies M2. Omitting
        # navigation is the retained direct-call V1 baseline used in old tests.
        if navigation is None:
            self.memory.update(base)
            memory=self.memory
        else:
            memory=NavigationBlockMap(navigation)
        memory.prune(own.position,now_ns)
        if own.on_ground and abs(own.position.y-round(own.position.y))<=.001:
            floor = round(own.position.y)-1
            if _support(memory,(math.floor(own.position.x),math.floor(own.position.z)),floor,now_ns,MOVEMENT_FRESHNESS_NS+1):
                self._floor = floor
        if navigation is not None and self._retreat is not None:
            return self._local_retreat(navigation,view,now_ns)
        if target.status!='visible':
            self.navigator.clear()
            self._speed_sample,self._speeds = None,(0.,0.,True)
            if self.belief is not None:
                gait='normal' if self.mode=='auto' else self.mode
                result=self.search.decide(navigation,view,self._belief_view,now_ns,fixed_movement(gait),self._floor)
                if self.search.completed_check is not None:
                    self.belief.record_check(self.search.completed_check)
                    self._belief_view=self.belief.view(now_ns)
                self._search_report=result.report
                if result.state=='waiting_target': self._target=replace(self._target,status='waiting_target',reason=result.reason)
                return self._result(result.state,result.reason,movement=result.movement,look=result.look,gait=gait)
            if target.status=='waiting_target': return self._result('waiting_target',target.reason)
            if self.tracker.consume_search_turn(view,now_ns):
                if self._search_origin is None:
                    self._search_origin = (math.degrees(math.atan2(-(target.position.x-own.position.x),target.position.z-own.position.z))
                                           if target.position is not None else own.yaw)
                if navigation is not None and self._search_index==0 and abs(wrap(self._search_origin-own.yaw))>8:
                    # Reach the last-seen scan origin first. Advancing its offset
                    # while still turning can make the requested direction run
                    # away and reverse the turn before ever inspecting the origin.
                    return self._result('searching','target_not_visible',look=self._look(view,self._search_origin,20))
                self._search_index += 1
                yaw = self._search_origin+15*self._search_index
                return self._result('searching','target_not_visible',look=self._look(view,yaw,20))
            return self._result('searching','awaiting_search_observation')
        self._search_origin,self._search_index = None,0
        goal = target.position
        distance = math.hypot(goal.x-own.position.x,goal.z-own.position.z)
        self._estimate_speeds(view,distance)
        yaw = math.degrees(math.atan2(-(goal.x-own.position.x),goal.z-own.position.z))
        # Bias toward lower half while keeping useful ground in the legal sensor cone.
        pitch = math.degrees(math.atan2(own.position.y+1.62-goal.y-target.size.y*.35,max(distance,.01)))
        look = self._look(view,yaw,max(12,min(35,pitch)))
        if self._floor is not None and goal.y-self._floor-1>1.5:
            if self._high_since is None: self._high_since = now_ns
            return self._result('blocked' if now_ns-self._high_since>=1_000_000_000 else 'waiting_target',
                                'target_above_reachable_floor',distance=distance,
                                look=self._task_look(view,navigation,now_ns,yaw,max(12,min(35,pitch))))
        self._high_since = None
        gait = self.mode
        if self.mode=='auto':
            safe_jump = guard_playground_motion(memory,view,now_ns,self._floor,
                own.yaw+look.yaw_delta_degrees,jump=True) is None
            gait = self._auto.choose(distance,self._speeds[0],self._speeds[1],wrap(yaw-own.yaw),safe_jump,now_ns)
            state = ('retreat' if distance<self.auto_config.target_distance-self.auto_config.restart_band
                     else 'hold' if gait=='hold' else 'approach')
        else: state = self.gate.update(distance)
        if state=='hold':
            self.clear_navigation(clear_perception=False)
            self._ground_scans = 0
            return self._result('holding_distance','distance_hysteresis',distance=distance,
                look=self._task_look(view,navigation,now_ns,yaw,max(12,min(35,pitch)),preaim=True),gait='hold')
        if distance<.01:
            return self._result('blocked','unsafe_overlap',distance=distance,
                look=self._task_look(view,navigation,now_ns,yaw,max(12,min(35,pitch))))
        if navigation is not None:
            if state=='retreat':
                if not own.on_ground or self._floor is None:
                    return self._result('waiting_observation','awaiting_grounded_retreat',distance=distance,
                        look=self._task_look(view,navigation,now_ns,yaw,max(12,min(35,pitch))))
                dx,dz=(own.position.x-goal.x)/distance,(own.position.z-goal.z)/distance
                point=Vec3V0(own.position.x+dx,own.position.y,own.position.z+dz)
                self._retreat=LocalRetreat(point,own.position,now_ns,base.sequence_id)
                self.navigator.clear()
                return self._local_retreat(navigation,view,now_ns)
            point=Vec3V0(goal.x,own.position.y if self._floor is None else self._floor+1,goal.z)
            options={} if self.perception is None else {'target_track_id':target.track_id}
            result=self.navigator.decide(navigation,view,now_ns,point,fixed_movement(gait),self._floor,
                allowed_player_contact=target.track_id if self.mode!='auto' else None,stop_distance=2,**options)
            return self._result(result.state,result.reason,distance=distance,movement=result.movement,
                                look=result.look,gait=gait)
        if abs(wrap(yaw-own.yaw))>20:
            return self._result('following','turn_to_target',distance=distance,look=look)
        movement = MovementV1(forward=-1) if state=='retreat' else fixed_movement(gait)
        guard = guard_playground_motion(self.memory,view,now_ns,self._floor,
            own.yaw+look.yaw_delta_degrees+(180 if state=='retreat' else 0),
            jump=movement.jump or not own.on_ground,
            allowed_player_contact=target.track_id if self.mode!='auto' and state=='approach' else None)
        if guard:
            if self._ground_scans<16:
                scan_pitch = (35,45,55,30)[self._ground_scans//4%4]
                self._ground_scans += 1
                return self._result('searching',guard,distance=distance,look=self._look(view,yaw,scan_pitch))
            return self._result('blocked',guard,distance=distance,look=look)
        self._ground_scans = 0
        return self._result('following','bounded_retreat' if state=='retreat' else 'bounded_step',
                            distance=distance,movement=movement,look=look,gait='normal' if state=='retreat' else gait)

    def feedback(self, executed: bool, view: PlaygroundView, now_ns: int, *,
                 evidence: NavigationEvidence | None=None) -> None:
        if type(executed) is not bool: raise ContractViolation('execution feedback must be boolean')
        require_nonnegative_int(now_ns,'feedback time')
        # Preemption cannot count as movement or clear the target/search budget.
        self.last_execution_confirmed = executed
        if self.perception is not None:
            if type(evidence) is not NavigationEvidence:
                raise ContractViolation('new_perception_feedback_requires_formal_evidence')
            self.perception.feedback(executed,evidence,now_ns)
        self.navigator.feedback(executed,view,now_ns)
        self.search.feedback(executed,view,now_ns)

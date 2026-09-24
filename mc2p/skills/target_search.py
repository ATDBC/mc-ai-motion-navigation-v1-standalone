"""Finite single-target search over observed support, never a hidden world map."""
from dataclasses import dataclass
import math

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.local_navigation import _support, _obstacle, plan_local_route
from mc2p.skills.navigation_controller import NavigationController
from mc2p.skills.navigation_evidence import EvidenceStamp
from mc2p.skills.navigation_motion import NavigationBlockMap
from mc2p.skills.target_attention import TargetAttention


@dataclass(frozen=True, slots=True)
class SearchCandidate:
    point: Vec3V0
    stamp: EvidenceStamp
    kind: str


@dataclass(frozen=True, slots=True)
class SearchReport:
    started_at_ns: int
    phase: str
    elapsed_ns: int
    travelled_blocks: float
    candidate_count: int
    look_requests: int
    completed_checks: int
    goal: SearchCandidate | None
    last_seen_sequence_id: int | None
    last_seen_age_ns: int | None
    schema_version: str = 'mc2p.target-search-report.v1'


@dataclass(frozen=True, slots=True)
class SearchDecision:
    movement: MovementV1
    look: LookV1
    state: str
    reason: str
    report: SearchReport | None


def _direction(belief):
    trend=next((h.direction for h in belief.hypotheses if h.kind=='motion_direction'),None)
    if trend is not None: return trend
    last=belief.last_seen
    if last is None: return (0.,1.)
    dx=last.entity.position.x-last.observer_pose.position.x
    dz=last.entity.position.z-last.observer_pose.position.z
    length=math.hypot(dx,dz)
    return (0.,1.) if length<.01 else (dx/length,dz/length)


def search_candidates(snapshot,view,belief,now_ns,visited):
    """At most sixteen ranked, historically reachable support cells. Not permits."""
    last,own=belief.last_seen,view.base.own
    if snapshot.invalid_reason or last is None or own is None or not own.on_ground:
        return ()
    floor=round(own.position.y)-1
    if abs(own.position.y-floor-1)>.001 or abs(last.entity.position.y-own.position.y)>1.5:
        return ()
    memory=NavigationBlockMap(snapshot); memory.prune(own.position,now_ns)
    direction=_direction(belief)
    candidates=[]
    for record in snapshot.terrain:
        x,y,z=record.block.position
        if y!=floor: continue
        point=Vec3V0(x+.5,floor+1,z+.5)
        distance=math.hypot(point.x-own.position.x,point.z-own.position.z)
        last_distance=math.hypot(point.x-last.entity.position.x,point.z-last.entity.position.z)
        if not .75<=distance<=4.5 or last_distance>8: continue
        if any(math.hypot(point.x-p.x,point.z-p.z)<.8 for p in visited): continue
        if not _support(memory,(x,z),floor,now_ns,60_000_000_000): continue
        if _obstacle(memory,view.base,point.x,point.z,floor): continue
        frontier=any(not _support(memory,(x+dx,z+dz),floor,now_ns,60_000_000_000)
                     for dx,dz in ((1,0),(-1,0),(0,1),(0,-1)))
        forward=(point.x-last.entity.position.x)*direction[0]+(point.z-last.entity.position.z)*direction[1]
        score=last_distance+.15*distance-.25*forward-.1*frontier
        candidates.append((score,x,z,SearchCandidate(point,record.last_seen,
                                                   'observed_frontier' if frontier else 'last_seen_route')))
    result=[]
    for _,_,_,candidate in sorted(candidates)[:16]:
        route=plan_local_route(memory,view.base,candidate.point,now_ns,support_freshness_ns=60_000_000_000)
        if route.cells: result.append(candidate)
    return tuple(result)


class TargetSearch:
    def __init__(self, *, perception=None):
        self.perception=perception
        self.attention=TargetAttention(perception=perception,perception_owner='search_attention')
        self.navigator=NavigationController() if perception is None else NavigationController(
            perception=perception,perception_owner='search_nav')
        self.clear()

    def clear(self):
        self.attention.clear(); self.navigator.clear()
        self.started=None
        self.phase='orient_last_seen'
        self.goal=None
        self.travelled=0.
        self.candidates=0
        self.look_requests=0
        self.checks=0
        self.visited=[]
        self._last_position=None
        self._last_sequence=-1
        self._last_now=0
        self._scope=None
        self._reason=None
        self._no_route_checks=0
        self.completed_check=None

    def interrupt(self):
        """Release local control, preserve the current loss's terminal/budget state."""
        self.attention.clear(); self.navigator.clear()
        self.goal=None
        self.phase='orient_last_seen'

    def feedback(self,selected,view,now_ns):
        self.attention.feedback(selected,view,now_ns)
        self.navigator.feedback(selected,view,now_ns)

    def status(self,now_ns):
        """Read-only delivery projection; querying never advances or renews search."""
        require_nonnegative_int(now_ns,'search status time')
        if self.started is None: return None
        return dict(phase=self.phase,reason=self._reason,started_at_ns=self.started,
            elapsed_ns=max(0,now_ns-self.started),travelled_blocks=self.travelled,
            candidate_count=self.candidates,look_requests=self.look_requests,completed_checks=self.checks,
            remaining_ns=max(0,30_000_000_000-(now_ns-self.started)),
            remaining_blocks=max(0,24-self.travelled),remaining_candidates=max(0,16-self.candidates),
            remaining_look_requests=max(0,96-self.look_requests),remaining_checks=max(0,24-self.checks))

    def _result(self,belief,now_ns,reason,*,movement=MovementV1(),look=LookV1(),state='searching'):
        last=belief.last_seen
        report=SearchReport(self.started,self.phase,max(0,now_ns-self.started),self.travelled,
            self.candidates,self.look_requests,self.checks,self.goal,
            None if last is None else last.stamp.sequence_id,belief.last_seen_age_ns)
        return SearchDecision(movement,look,state,reason,report)

    def _look(self,view,belief,point,now_ns,purpose,*,snapshot=None):
        self.look_requests+=1
        look=self.attention.request(view,point,now_ns,purpose=purpose,snapshot=snapshot,
            deadline_ns=self.started+30_000_000_000)
        return self._result(belief,now_ns,purpose,look=look)

    def decide(self,snapshot,view,belief,now_ns,movement,floor):
        require_nonnegative_int(now_ns,'search time')
        if now_ns<self._last_now: raise ContractViolation('search_time_regression')
        self._last_now=now_ns
        self.completed_check=None
        scope=(snapshot.scope_id,view.base.episode_id,view.base.controller_clock_id,view.base.client_clock_id,belief.track_id)
        if self._scope is not None and (scope!=self._scope or snapshot.invalid_reason or belief.status=='invalidated'):
            self._reason='search_scope_invalidated'
        latest,base=snapshot.latest,view.base
        visible_matches=(belief.status=='visible' and latest is not None and latest.available and base.available
            and latest.stamp.sequence_id==base.sequence_id and latest.stamp.scope==scope[1:4]
            and latest.stamp.request_start_ns==base.request_start_ns and latest.stamp.received_at_ns==base.received_at_ns
            and latest.stamp.recent(now_ns,500_000_000) and belief.last_seen is not None
            and belief.last_seen.stamp==latest.stamp and belief.visible_entity in latest.entities
            and belief.visible_entity in base.entities and belief.visible_entity.track_id==belief.track_id)
        if visible_matches and self._reason!='search_scope_invalidated':
            self.clear()
            return SearchDecision(MovementV1(),LookV1(),'following','target_reobserved',None)
        if self.started is None:
            self.started=now_ns
            self._scope=(snapshot.scope_id,view.base.episode_id,view.base.controller_clock_id,view.base.client_clock_id,belief.track_id)
        if scope!=self._scope or snapshot.invalid_reason or belief.status=='invalidated':
            self._reason='search_scope_invalidated'
        base,own=view.base,view.base.own
        if own is not None and base.available and base.sequence_id>self._last_sequence:
            if self._last_position is not None:
                self.travelled+=math.hypot(own.position.x-self._last_position.x,own.position.z-self._last_position.z)
            self._last_position=own.position
        if self._reason is None:
            if now_ns-self.started>=30_000_000_000: self._reason='search_time_budget_exhausted'
            elif self.travelled>=24: self._reason='search_distance_budget_exhausted'
            elif self.look_requests>=96 or self.checks>=24: self._reason='search_observation_budget_exhausted'
        if self._reason:
            self.attention.clear(); self.navigator.clear()
            self.phase='waiting'
            return self._result(belief,now_ns,self._reason,state='waiting_target')
        if belief.status=='visible':
            self.attention.clear(); self.navigator.clear()
            return self._result(belief,now_ns,'search_belief_observation_mismatch')
        if (not base.available or own is None or snapshot.latest is None or not snapshot.latest.available
                or not 0<=now_ns-base.request_start_ns<=500_000_000):
            self.attention.clear(); self.navigator.clear()
            return self._result(belief,now_ns,'search_observation_unavailable')
        if base.sequence_id<=self._last_sequence:
            return self._result(belief,now_ns,'awaiting_search_observation')
        self._last_sequence=base.sequence_id
        if own.dead or base.gui_open or own.unsupported_motion or view.status_effect_ids:
            self._reason='search_body_unavailable'
            return self._result(belief,now_ns,self._reason,state='blocked')
        last=belief.last_seen
        if last is None or floor is not None and abs(last.entity.position.y-floor-1)>1.5:
            self._reason='search_no_supported_last_seen'
            return self._result(belief,now_ns,self._reason,state='waiting_target')
        check=self.attention.consume(snapshot,view,now_ns)
        if check is not None:
            self.checks+=1; self.completed_check=check
            if self.phase=='inspect_waypoint':
                self.visited.append(self.goal.point); self.goal=None
            self.phase='choose_waypoint'
        if self.attention.point is not None:
            return self._look(view,belief,self.attention.point,now_ns,self.attention.purpose,snapshot=snapshot)
        if self.phase=='orient_last_seen':
            return self._look(view,belief,last.entity.position,now_ns,
                              'reacquire' if belief.status=='reacquiring' else 'search',snapshot=snapshot)
        if self.phase=='choose_waypoint':
            if self.candidates>=16:
                self._reason='search_candidate_budget_exhausted'
                return self._result(belief,now_ns,self._reason,state='waiting_target')
            candidates=search_candidates(snapshot,view,belief,now_ns,self.visited)
            if not candidates:
                if self._no_route_checks>=3:
                    self._reason='no_observed_search_route'
                    return self._result(belief,now_ns,self._reason,state='waiting_target')
                self._no_route_checks+=1
                # Inspect a different direction from the same place; this is
                # only a look point, never an unobserved movement destination.
                dx,dz=_direction(belief)
                angle=math.atan2(dz,dx)+(0,math.pi/4,-math.pi/4)[self._no_route_checks-1]
                point=Vec3V0(own.position.x+3*math.cos(angle),own.position.y,own.position.z+3*math.sin(angle))
                return self._look(view,belief,point,now_ns,'search',snapshot=snapshot)
            self.goal=candidates[0]; self.candidates+=1
            self.phase='move_to_observe'; self.navigator.clear()
        if self.phase=='move_to_observe':
            distance=math.hypot(own.position.x-self.goal.point.x,own.position.z-self.goal.point.z)
            if distance<=.4:
                self.phase='inspect_waypoint'; self.navigator.clear()
            else:
                decision=self.navigator.decide(snapshot,view,now_ns,self.goal.point,movement,floor)
                if decision.state=='blocked':
                    self.visited.append(self.goal.point); self.goal=None
                    self.phase='choose_waypoint'; self.navigator.clear()
                elif decision.movement==MovementV1():
                    self.look_requests+=1
                return self._result(belief,now_ns,'search_'+decision.reason,
                                    movement=decision.movement,look=decision.look)
        if self.phase=='inspect_waypoint':
            dx,dz=_direction(belief)
            point=Vec3V0(own.position.x+3*dx,own.position.y,own.position.z+3*dz)
            return self._look(view,belief,point,now_ns,'search',snapshot=snapshot)
        return self._result(belief,now_ns,'awaiting_search_observation')

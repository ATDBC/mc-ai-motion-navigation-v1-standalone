"""Bounded local candidate -> observe -> check -> move loop. No target prediction."""
from dataclasses import dataclass, replace
import math

from mc2p.contracts.action_v1 import LookV1, MovementV1
from mc2p.contracts.common import ContractViolation, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.local_navigation import plan_local_route
from mc2p.skills.navigation_look import ObservationGate, wrap
from mc2p.skills.navigation_motion import (
    NavigationBlockMap, check_navigation_motion, report_navigation_motion,
)
from mc2p.skills.perception_candidates import make_needs
from mc2p.skills.perception_needs import MotionProposal, PerceptionNeed
from mc2p.skills.route_steering import choose_route_waypoint, route_observation_frontier
from mc2p.skills.route_continuity import choose_continuous_waypoint


# One original-speed control step. This is a prediction, not the 250ms
# dispatch timeout or permission to renew the original evidence.
ESTIMATED_CONTROL_INTERVAL_NS = 50_000_000


@dataclass(frozen=True, slots=True)
class NavigationDecision:
    movement: MovementV1 = MovementV1()
    look: LookV1 = LookV1()
    state: str = 'waiting_observation'
    reason: str = 'awaiting_new_observation'


class NavigationController:
    def __init__(self, *, perception=None, perception_owner='follow_nav'):
        if perception_owner not in {'follow_nav','search_nav'}:
            raise ContractViolation('invalid navigation perception owner')
        if perception is not None and getattr(perception,'variant',None) not in {
                'smooth_only','active_perception_v1'}:
            raise ContractViolation('invalid navigation perception coordinator')
        self.perception=perception
        self.perception_owner=perception_owner
        self.gate=ObservationGate()
        self._reset_local()

    def _reset_local(self):
        self.gate.clear()
        self._started_ns=None
        self._checks=0
        self._last_sequence=-1
        self._scope=None
        self._force_check=False
        self._movement_sample=None
        self._active_look_request=None
        self._continuity_anchor=None
        self._continuity_target=None

    def clear(self):
        self._reset_local()
        if self.perception is not None:
            self.perception.clear_control('navigation_cleared',owner=self.perception_owner)

    @staticmethod
    def preview_heading(snapshot, view, now_ns, goal, floor, *, target_track_id, stop_distance):
        """No state update or action: the hold owner may request this direction."""
        return choose_continuous_waypoint(snapshot,view,goal,now_ns,floor,
            target_track_id=target_track_id,stop_distance=stop_distance,preserve_heading=False)

    def feedback(self, selected, view, now_ns):
        if self.perception is None or self.perception.variant=='smooth_only':
            self.gate.feedback(selected,view,now_ns)
        elif self.perception.variant=='active_perception_v1':
            request=self._active_look_request
            self._active_look_request=None
            base,own=view.base,view.base.own
            if (selected and request is not None and base.available and own is not None
                    and (base.episode_id,base.controller_clock_id,base.client_clock_id)==request[0]
                    and base.sequence_id>request[1]
                    and base.client_sample_start_ns>=request[2]
                    and 0<=now_ns-base.request_start_ns<=500_000_000
                    and base.received_at_ns>=request[5] and now_ns>=base.received_at_ns
                    and abs(wrap(own.yaw-request[3]))<=8
                    and abs(own.pitch-request[4])<=6):
                self._checks+=1
        previous=self._movement_sample
        self._movement_sample=None
        base,own=view.base,view.base.own
        if (selected and previous is not None and base.available and own is not None
                and (base.episode_id,base.controller_clock_id,base.client_clock_id)==previous[0]
                and base.sequence_id>previous[1] and base.client_sample_start_ns>=previous[2]
                and 0<=now_ns-base.request_start_ns<=500_000_000
                and base.received_at_ns>=previous[4]
                and math.hypot(own.position.x-previous[3].x,own.position.z-previous[3].z)>.01):
            self._started_ns=None; self._checks=0
        if not selected:
            self._force_check=True
            self._continuity_anchor=None

    def _observe(self, view, yaw, now_ns, reason):
        if self._started_ns is None:
            self._started_ns=now_ns
        if now_ns-self._started_ns>=3_000_000_000 or self._checks>=16:
            return NavigationDecision(state='blocked',reason='observation_budget_exhausted')
        # Keep a partial turn's requested pitch until its post-sample arrives.
        pitch=self.gate.pending.pitch if self.gate.pending else (35,45,55,30)[self._checks%4]
        look=self.gate.request(view,yaw,pitch,now_ns)
        if self.perception is not None and self.perception.variant=='smooth_only':
            look=self.perception.command_legacy(view,yaw,pitch,now_ns,
                                                owner=self.perception_owner)
        return NavigationDecision(look=look,state='searching',reason=reason)

    def _clear_perception(self, reason):
        self._active_look_request=None
        self._continuity_anchor=None
        if self.perception is not None:
            self.perception.clear_control(reason,owner=self.perception_owner)

    def _remember_active_look(self, decision, view, now_ns):
        status=self.perception.status()
        requested_yaw=status.get('requested_yaw_degrees')
        requested_pitch=status.get('requested_pitch_degrees')
        if (decision.pending_need_ids and type(requested_yaw) in (int,float)
                and type(requested_pitch) in (int,float)
                and math.isfinite(requested_yaw) and math.isfinite(requested_pitch)):
            base=view.base
            self._active_look_request=(
                (base.episode_id,base.controller_clock_id,base.client_clock_id),
                base.sequence_id,base.client_sample_end_ns,wrap(requested_yaw),
                max(-90,min(90,requested_pitch)),now_ns,
            )

    def _observe_route_frontier(self, snapshot, view, now_ns, frontier, target_track_id):
        block,point=frontier
        deadline=self._started_ns+3_000_000_000
        need=PerceptionNeed('route-frontier/'+'/'.join(str(v) for v in block),
            'follow_nav',snapshot.scope_id,snapshot.latest.stamp,'route_check',95,deadline,
            point,'observed_block',block=block)
        tracking=make_needs(snapshot,view,None,None,target_track_id=target_track_id,aim=None,
            now_ns=now_ns,deadline_ns=deadline,config=self.perception.config)
        result=self.perception.decide(snapshot,view,(need,)+tracking,None,None,now_ns,deadline)
        self._remember_active_look(result,view,now_ns)
        # Even a confirmed frontier is not a route or a movement permit.
        # A later decide must re-run the original BFS and full actual-yaw guard.
        reason=('route_frontier_observation' if result.reason=='selected_task_fragment' else result.reason)
        state='searching' if result.look!=LookV1() else 'waiting_observation'
        return NavigationDecision(look=result.look,state=state,reason=reason)

    def _active_decide(self, snapshot, view, now_ns, waypoint, movement, floor, yaw,
                       allowed_player_contact, target_track_id):
        own=view.base.own
        assert own is not None and snapshot.latest is not None
        if self._started_ns is None:
            self._started_ns=now_ns
        deadline_ns=self._started_ns+3_000_000_000
        long_horizon=movement.jump or not own.on_ground
        speed=math.hypot(own.velocity.x,own.velocity.z)
        horizon=max(4.5,14*speed) if long_horizon else .45+2*speed
        proposal=MotionProposal(snapshot.scope_id,snapshot.latest.stamp,waypoint,floor,yaw,
            movement,horizon,ESTIMATED_CONTROL_INTERVAL_NS,allowed_player_contact)
        report=report_navigation_motion(snapshot,view,now_ns,floor,own.yaw,
            jump=long_horizon,allowed_player_contact=allowed_player_contact)
        needs=make_needs(snapshot,view,proposal,report,target_track_id=target_track_id,
            aim=None,now_ns=now_ns,deadline_ns=deadline_ns,config=self.perception.config)
        if self.perception_owner=='search_nav':
            needs=tuple(replace(need,owner='search_nav') if need.owner=='follow_nav' else need
                        for need in needs)
        forced_id=None
        if floor is None or self._force_check:
            forced_id='navigation-floor-check' if floor is None else 'navigation-forced-check'
            if floor is None:
                point=Vec3V0(own.position.x,own.position.y-1.,own.position.z)
            else:
                radians=math.radians(yaw)
                point=Vec3V0(own.position.x-math.sin(radians),own.position.y+1.62,
                            own.position.z+math.cos(radians))
            forced=PerceptionNeed(forced_id,self.perception_owner,snapshot.scope_id,
                snapshot.latest.stamp,'route_check',100,deadline_ns,point,'filtered_check')
            needs=(forced,)+tuple(need for need in needs if need.need_id!=forced_id)
            needs=needs[:self.perception.config.max_needs]
        decision=self.perception.decide(snapshot,view,needs,proposal,report,now_ns,deadline_ns)
        if forced_id is not None and forced_id in decision.completed_need_ids:
            self._force_check=False
        selected_movement=decision.movement
        if (self._force_check or floor is None or abs(wrap(yaw-own.yaw))>8
                or decision.look.yaw_delta_degrees!=0):
            selected_movement=MovementV1()
        self._remember_active_look(decision,view,now_ns)
        if selected_movement!=MovementV1():
            base=view.base
            self._movement_sample=((base.episode_id,base.controller_clock_id,base.client_clock_id),
                base.sequence_id,base.client_sample_end_ns,own.position,now_ns)
            return NavigationDecision(selected_movement,decision.look,'following',decision.reason)
        state='searching' if decision.look!=LookV1() else 'waiting_observation'
        return NavigationDecision(MovementV1(),decision.look,state,decision.reason)

    def decide(self, snapshot, view, now_ns, goal, movement, floor, *, allowed_player_contact=None,
               stop_distance=0, target_track_id=None):
        require_nonnegative_int(now_ns,'navigation decision time')
        if movement.forward!=1 or movement.strafe!=0:
            self.gate.clear(); self._force_check=True
            self._clear_perception('unsupported_navigation_movement')
            return NavigationDecision(state='blocked',reason='unsupported_navigation_movement')
        base,own=view.base,view.base.own
        if (snapshot.invalid_reason or snapshot.latest is None or not snapshot.latest.available
                or not base.available or own is None or not 0<=now_ns-base.request_start_ns<=500_000_000):
            self.gate.clear(); self._force_check=True
            self._clear_perception('navigation_observation_unavailable')
            return NavigationDecision(reason='navigation_observation_unavailable')
        scope=(snapshot.scope_id,base.episode_id,base.controller_clock_id,base.client_clock_id)
        if self._scope is not None and self._scope!=scope:
            self.clear()
            return NavigationDecision(state='blocked',reason='navigation_scope_changed')
        self._scope=scope
        if base.sequence_id<=self._last_sequence:
            return NavigationDecision()
        self._last_sequence=base.sequence_id
        if self._started_ns is not None and (now_ns-self._started_ns>=3_000_000_000 or self._checks>=16):
            return NavigationDecision(state='blocked',reason='observation_budget_exhausted')
        if floor is None or abs(goal.y-floor-1)>.1:
            yaw=math.degrees(math.atan2(-(goal.x-own.position.x),goal.z-own.position.z))
            if self.perception is not None and self.perception.variant=='active_perception_v1':
                underfoot=Vec3V0(own.position.x,own.position.y-1.,own.position.z)
                return self._active_decide(snapshot,view,now_ns,underfoot,movement,None,yaw,
                                           allowed_player_contact,target_track_id)
            return self._observe(view,yaw,now_ns,'unknown_reachable_floor')
        distance=math.hypot(goal.x-own.position.x,goal.z-own.position.z)
        if distance<=.2:
            self.gate.clear(); self._started_ns=None; self._checks=0
            self._clear_perception('local_waypoint_reached')
            return NavigationDecision(state='holding_distance',reason='local_waypoint_reached')
        active_follow=(self.perception is not None and self.perception.variant=='active_perception_v1'
                       and self.perception_owner=='follow_nav' and target_track_id is not None)
        waypoint=None
        if active_follow:
            targets=tuple(entity for entity in base.entities if entity.track_id==target_track_id
                          and entity.entity_type=='minecraft:player')
            if len(targets)!=1 or base.entities_truncated:
                self._clear_perception('navigation_target_unavailable')
                return NavigationDecision(reason='navigation_target_unavailable')
            if self._continuity_target!=target_track_id:
                self._continuity_anchor=None
            self._continuity_target=target_track_id
            waypoint=choose_continuous_waypoint(snapshot,view,goal,now_ns,floor,
                target_track_id=target_track_id,stop_distance=stop_distance,anchor=self._continuity_anchor)
            self._continuity_anchor=waypoint
        if waypoint is None:
            route=plan_local_route(NavigationBlockMap(snapshot),base,goal,now_ns,
                                   support_freshness_ns=60_000_000_000,stop_distance=stop_distance)
            waypoint=Vec3V0(route.cells[1][0]+.5,floor+1,route.cells[1][1]+.5) if len(route.cells)>1 else goal
            if active_follow:
                waypoint=choose_route_waypoint(view,route,waypoint,target_track_id=target_track_id,
                    margin_degrees=self.perception.config.target_yaw_margin_degrees,
                    horizontal_fov_degrees=snapshot.latest.coverage.horizontal_fov_degrees)
                if waypoint is None:
                    if self._started_ns is None:
                        self._started_ns=now_ns
                    frontier=route_observation_frontier(snapshot,view,target_track_id=target_track_id,
                        floor=floor,stop_distance=stop_distance,now_ns=now_ns)
                    if frontier is not None:
                        return self._observe_route_frontier(snapshot,view,now_ns,frontier,target_track_id)
                    self._clear_perception('route_view_conflict')
                    return NavigationDecision(state='blocked',reason='route_view_conflict')
        yaw=math.degrees(math.atan2(-(waypoint.x-own.position.x),waypoint.z-own.position.z))
        if self.perception is not None and self.perception.variant=='active_perception_v1':
            if own.horizontal_collision:
                if self._started_ns is None:
                    self._started_ns=now_ns
                self._force_check=True
                self._clear_perception('current_collision')
                return NavigationDecision(state='searching',reason='current_collision')
            return self._active_decide(snapshot,view,now_ns,waypoint,movement,floor,yaw,
                                       allowed_player_contact,target_track_id)
        if self.gate.pending is not None:
            if not self.gate.confirmed(view,now_ns):
                return self._observe(view,yaw,now_ns,'awaiting_selected_look_observation')
            self._checks+=1
            self.gate.clear(); self._force_check=False
        if own.horizontal_collision:
            self._force_check=True
            return self._observe(view,yaw,now_ns,'current_collision')
        if self._force_check or abs(wrap(yaw-own.yaw))>8:
            return self._observe(view,yaw,now_ns,'observe_route_direction')
        guard=check_navigation_motion(snapshot,view,now_ns,floor,own.yaw,
            jump=movement.jump or not own.on_ground,allowed_player_contact=allowed_player_contact)
        if guard:
            return self._observe(view,yaw,now_ns,guard)
        if self._started_ns is None: self._started_ns=now_ns
        self._movement_sample=((base.episode_id,base.controller_clock_id,base.client_clock_id),
            base.sequence_id,base.client_sample_end_ns,own.position,now_ns)
        # Preserve the actual checked direction; do not rotate and then pretend
        # the previous block sample already covers the rotated footprint.
        return NavigationDecision(movement=movement,state='following',reason='observed_bounded_step')

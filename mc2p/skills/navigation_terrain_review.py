"""Static terrain admission and route-related observation, without world queries."""
from mc2p.skills.navigation_belief import support_admission
import math
from dataclasses import replace

from mc2p.skills.block_geometry import is_supported_floor
from mc2p.skills.navigation_belief import HISTORICAL_SUPPORT_NS


def static_support_reason(record,now_ns,*,historical,contradicted,terrain,belief,block,view):
    marker=belief.get(block)
    if record is not None and (marker is None or marker.history!=record):
        return 'control_unavailable'
    # A retained ordinary support is a static hypothesis, not a new observation.
    # Unknown/unsupported/fluid/contradicted/future records still fail here.
    reason=support_admission(record,now_ns,historical=historical,
        high_consequence=False,contradicted=contradicted or bool(marker and marker.contradicted))
    if reason is None and marker.review_after_ns is not None:return 'uncertain_history'
    return reason


class TerrainReviewQueue:
    """Latched review of at most eight supports on the next four route blocks.

    The extra .2 observation margin discovers nearby support boundaries; it
    never replaces, shrinks or authorizes the actual continuous motion guard.
    """
    def __init__(self,*,unified_motion=False):
        self.unified_motion=unified_motion
        self.clear()

    def clear(self):
        self.pending={};self.verified={};self.binding=None
        self.last_needs=();self.blocked_since=None
        self.hold_blocks=()
        self.due_since={}
        self.episodes={}
        self.capacity_exhausted=False
        self.prefix_exhausted=False

    def commit_holds(self,belief,now_ns):
        if self.hold_blocks:belief.require_review(self.hold_blocks,now_ns)
        self.hold_blocks=()

    def state_digest(self):
        import hashlib,json
        from mc2p.runtime.trace import trace_projection
        state=dict(binding=self.binding,pending=tuple(sorted(self.pending.items())),
            verified=tuple(sorted(self.verified.items())),due_since=tuple(sorted(self.due_since.items())),
            holds=self.hold_blocks)
        if self.unified_motion:
            state.update(episodes=tuple(sorted(self.episodes.items())),
                capacity_exhausted=self.capacity_exhausted,prefix_exhausted=self.prefix_exhausted)
        return hashlib.sha256(json.dumps(trace_projection(state),sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def due(self,now_ns):
        return any(n.required_by_ns<=now_ns for n in self.last_needs)

    def due_for(self,proposal,now_ns):
        from mc2p.skills.navigation_motion_envelope import proposal_depends_on
        if not self.unified_motion:return self.due(now_ns)
        needs={n.block for n in self.last_needs if n.required_by_ns<=now_ns}
        # Retained due facts apply when a new candidate again touches the block,
        # even if the current route cancelled its active gaze request.
        needs.update(b for b,n in self.episodes.items() if n.required_by_ns<=now_ns)
        return proposal_depends_on(needs,proposal)

    def preserve_facts_from(self,other):
        """Keep latest review evidence across a discarded planning transaction.

        Call old_queue.preserve_facts_from(trial_queue) before restoring the old
        policy. This queue owns observation needs and waits, not selected moves.
        A later update recomputes active route requests from these retained facts.
        """
        if not self.unified_motion or not other.unified_motion:return
        if self.binding not in (None,other.binding):return
        for name in ('binding','pending','verified','due_since','episodes','last_needs',
                     'blocked_since','hold_blocks','capacity_exhausted','prefix_exhausted'):
            value=getattr(other,name)
            setattr(self,name,value.copy() if isinstance(value,dict) else value)

    @staticmethod
    def _route_cells(own,path):
        points=(own,)+tuple(p for p in path if p!=own)
        cells={};travel=0.
        for a,b in zip(points,points[1:]):
            length=math.hypot(b.x-a.x,b.z-a.z)
            if length<1e-9:continue
            used=min(length,4.-travel)
            for i in range(max(1,math.ceil(used/.25))+1):
                along=min(used,i*.25);t=along/length
                x,z=a.x+(b.x-a.x)*t,a.z+(b.z-a.z)*t
                cells.setdefault((math.floor(x),round(own.y)-1,math.floor(z)),(travel+along,x,z))
            travel+=used
            if travel>=4.:break
        return cells

    @staticmethod
    def _edge_context(terrain,belief,block,x,z):
        # Only nearby support boundaries matter. A known full wall occupies
        # its column; its hidden floor is not a walking/review dependency.
        y=block[1];edge=[]
        for bx in range(math.floor(x-.6),math.floor(x+.6)+1):
            for bz in range(math.floor(z-.6),math.floor(z+.6)+1):
                if (bx,y,bz)==block:continue
                wall=terrain.get((bx,y+1,bz));wm=belief.get((bx,y+1,bz))
                if (wall is not None and wall.block.collision.kind=='full_cube'
                    and wm is not None and wm.history==wall and not wm.contradicted):continue
                record=terrain.get((bx,y,bz));marker=belief.get((bx,y,bz))
                if (record is None or marker is None or marker.history!=record or marker.contradicted
                    or not is_supported_floor(record.block)):
                    edge.append((bx,y,bz))
        return tuple(edge)

    def update(self,snapshot,view,now_ns,path,*,belief,goal_deadline_ns,
               speed_blocks_per_second,delivery_ns,look_rate_degrees_per_second,
               control_interval_ns=50_000_000,release_ns=250_000_000):
        if self.unified_motion:
            return self._update_unified(snapshot,view,now_ns,path,belief=belief,
                goal_deadline_ns=goal_deadline_ns,speed_blocks_per_second=speed_blocks_per_second,
                delivery_ns=delivery_ns,look_rate_degrees_per_second=look_rate_degrees_per_second,
                control_interval_ns=control_interval_ns,release_ns=release_ns)
        from mc2p.skills.navigation_joint_policy import ObservationNeed
        own=view.base.own
        binding=(snapshot.scope_id,snapshot.latest.stamp.scope)
        if self.binding!=binding:self.clear();self.binding=binding
        terrain=snapshot.terrain_index;cells=self._route_cells(own.position,path)
        self.hold_blocks=()
        self.pending={b:v for b,v in self.pending.items() if b in cells}
        self.verified={b:v for b,v in self.verified.items() if b in cells}
        speed=max(.1,float(speed_blocks_per_second))
        drift=math.hypot(own.velocity.x,own.velocity.z)
        stopping=.45+4*drift  # Existing travel plus neutral drift extent.
        for block,(distance,x,z) in cells.items():
            record=terrain.get(block);marker=belief.get(block)
            if (record is None or marker is None or marker.history!=record or marker.contradicted
                or not is_supported_floor(record.block)):
                self.pending.pop(block,None);self.verified.pop(block,None);continue
            edge=self._edge_context(terrain,belief,block,x,z)
            lower_hint=any((lower:=terrain.get((block[0],y,block[2]))) is not None
                and lower.last_seen.request_start_ns>record.last_seen.request_start_ns
                and is_supported_floor(lower.block) for y in (block[1]-1,block[1]-2))
            entities=tuple((e.track_id,e.position,e.size) for e in view.base.entities
                if abs(e.position.x-x)<1.+e.size.x/2 and abs(e.position.z-z)<1.+e.size.z/2
                and e.position.y<own.position.y+1.8 and e.position.y+e.size.y>own.position.y)
            context=(record.block,edge,entities,lower_hint)
            existing=self.pending.get(block)
            if existing is not None and record.last_seen.request_start_ns>existing.evidence_after_ns:
                self.pending.pop(block);self.verified[block]=context
                existing=None
            yaw=math.degrees(math.atan2(-(block[0]+.5-own.position.x),block[2]+.5-own.position.z))
            horizontal=math.hypot(block[0]+.5-own.position.x,block[2]+.5-own.position.z)
            pitch=math.degrees(math.atan2(1.62,max(.001,horizontal)))
            angle=abs((yaw-own.yaw+180)%360-180)+abs(pitch-own.pitch)
            looking=int(angle/max(1.,look_rate_degrees_per_second)*1e9)+delivery_ns
            expires=record.last_seen.request_start_ns+HISTORICAL_SUPPORT_NS
            retention_due=expires<=now_ns+int((distance/speed)*1e9)+looking
            risky=bool(edge or entities or lower_hint)
            if not retention_due and (not risky or self.verified.get(block)==context):continue
            # A support sampled with this live body already supplies the check.
            if not retention_due and record.last_seen.request_start_ns>=view.base.request_start_ns:
                self.verified[block]=context;continue
            deadline=min(goal_deadline_ns,now_ns+int(max(0.,distance-stopping)/speed*1e9))
            if retention_due:deadline=min(deadline,expires-delivery_ns)
            # required_by marks the last safe time to keep moving. Begin gaze
            # as soon as the turn+delivery cannot fit before that boundary.
            priority=0 if deadline-now_ns<=looking else 1
            if existing is not None:
                self.pending[block]=replace(existing,required_by_ns=min(existing.required_by_ns,deadline),priority=priority)
            elif len(self.pending)<8:
                self.pending[block]=ObservationNeed('terrain-review/'+'/'.join(map(str,block)),block,
                    'uncertain_history',priority,max(0,deadline),record.last_seen.request_start_ns)
        self.last_needs=tuple(sorted(self.pending.values(),key=lambda n:(n.priority,n.required_by_ns,n.block)))[:8]
        due={n.block:n for n in self.last_needs if n.required_by_ns<=now_ns}
        self.due_since={b:v for b,v in self.due_since.items()
            if b in due and v[0]==due[b].evidence_after_ns}
        for block,need in due.items():self.due_since.setdefault(block,(need.evidence_after_ns,now_ns))
        self.blocked_since=min((v[1] for v in self.due_since.values()),default=None)
        self.hold_blocks=tuple(sorted(b for b,(_,since) in self.due_since.items() if now_ns-since>=3_000_000_000))
        return self.last_needs

    @staticmethod
    def _motion_route_cells(own,path,distance):
        from mc2p.skills.normal_navigation_guard import COMBINED_BODY_MARGIN, _polygon_open_rect
        points=(own,)+tuple(p for p in path if p!=own)
        cells={};travel=0.;margin=COMBINED_BODY_MARGIN
        for a,b in zip(points,points[1:]):
            length=math.hypot(b.x-a.x,b.z-a.z)
            if length<1e-9:continue
            used=min(length,distance-travel)
            previous=(a.x,a.z)
            for i in range(1,max(1,math.ceil(used/.25))+1):
                along=min(used,i*.25);t=along/length
                current=(a.x+(b.x-a.x)*t,a.z+(b.z-a.z)*t)
                for x in range(math.floor(min(previous[0],current[0])-margin),math.floor(max(previous[0],current[0])+margin)+1):
                    for z in range(math.floor(min(previous[1],current[1])-margin),math.floor(max(previous[1],current[1])+margin)+1):
                        if _polygon_open_rect((previous,current),x-margin,z-margin,x+1+margin,z+1+margin):
                            cells.setdefault((x,round(own.y)-1,z),(travel+max(0.,along-.25),*current))
                previous=current
            travel+=used
            if travel>=distance:break
        return cells

    def _update_unified(self,snapshot,view,now_ns,path,*,belief,goal_deadline_ns,
            speed_blocks_per_second,delivery_ns,look_rate_degrees_per_second,
            control_interval_ns,release_ns):
        from mc2p.skills.navigation_joint_policy import ObservationNeed
        from mc2p.skills.navigation_motion_envelope import stopping_requirement
        own=view.base.own
        binding=(snapshot.scope_id,snapshot.latest.stamp.scope)
        if self.binding!=binding:self.clear();self.binding=binding
        requirement=stopping_requirement(own.velocity,speed_blocks_per_second=max(.1,float(speed_blocks_per_second)),
            delivery_ns=delivery_ns,control_interval_ns=control_interval_ns,release_ns=release_ns)
        self.prefix_exhausted=not requirement.covered
        terrain=snapshot.terrain_index
        cells=self._motion_route_cells(own.position,path,requirement.prefix_distance)
        speed=max(.1,float(speed_blocks_per_second))
        # Resolve only strictly newer evidence for that support. Route names and
        # unrelated observations cannot reset the original review episode.
        resolved=set()
        for block,need in tuple(self.episodes.items()):
            record=terrain.get(block);marker=belief.get(block)
            if (record is not None and marker is not None and marker.history==record
                and not marker.contradicted and marker.review_after_ns is None
                and need.evidence_after_ns<record.last_seen.request_start_ns<=now_ns):
                self.episodes.pop(block);self.due_since.pop(block,None)
                self.verified.pop(block,None);resolved.add(block)
        self.pending={}
        for block,(distance,x,z) in cells.items():
            record=terrain.get(block);marker=belief.get(block)
            if (record is None or marker is None or marker.history!=record or marker.contradicted
                or record.last_seen.request_start_ns>now_ns or not is_supported_floor(record.block)):
                continue
            seen=record.last_seen.request_start_ns
            edge=self._edge_context(terrain,belief,block,x,z)
            lower_seen=max((lower.last_seen.request_start_ns for y in (block[1]-1,block[1]-2)
                if (lower:=terrain.get((block[0],y,block[2]))) is not None
                and (lower_marker:=belief.get((block[0],y,block[2]))) is not None
                and lower_marker.history==lower and not lower_marker.contradicted
                and seen<lower.last_seen.request_start_ns<=now_ns and is_supported_floor(lower.block)),default=seen)
            entities=tuple(e for e in view.base.entities if abs(e.position.x-x)<1.+e.size.x/2
                and abs(e.position.z-z)<1.+e.size.z/2 and e.position.y<own.position.y+1.8
                and e.position.y+e.size.y>own.position.y)
            yaw=math.degrees(math.atan2(-(block[0]+.5-own.position.x),block[2]+.5-own.position.z))
            horizontal=math.hypot(block[0]+.5-own.position.x,block[2]+.5-own.position.z)
            pitch=math.degrees(math.atan2(1.62,max(.001,horizontal)))
            angle=math.hypot((yaw-own.yaw+180)%360-180,pitch-own.pitch)
            turn_ns=int(angle/max(1.,look_rate_degrees_per_second)*1e9)
            looking=turn_ns+delivery_ns
            expires=seen+HISTORICAL_SUPPORT_NS
            retention_due=expires<=now_ns+int(distance/speed*1e9)+looking
            existing=self.episodes.get(block)
            risky=bool(edge or entities or lower_seen>seen)
            context=(edge,tuple((e.track_id,e.position,e.size) for e in entities),lower_seen)
            fresh=(seen>=view.base.request_start_ns or block in resolved
                or self.verified.get(block)==(seen,context))
            if fresh and lower_seen==seen:self.verified[block]=(seen,context)
            if existing is None and not retention_due and (not risky or (fresh and lower_seen==seen)):
                continue
            local_requirement=stopping_requirement(own.velocity,speed_blocks_per_second=speed,
                delivery_ns=delivery_ns,control_interval_ns=control_interval_ns,release_ns=release_ns,
                look_ns=turn_ns)
            self.prefix_exhausted=self.prefix_exhausted or not local_requirement.covered
            deadline=min(goal_deadline_ns,now_ns+int(max(0.,distance-local_requirement.distance)/speed*1e9))
            if retention_due:deadline=min(deadline,expires-delivery_ns-control_interval_ns-release_ns)
            priority=0 if deadline-now_ns<=looking else 1
            if existing is not None:
                need=replace(existing,required_by_ns=min(existing.required_by_ns,max(0,deadline)),priority=priority,
                    evidence_after_ns=max(existing.evidence_after_ns,lower_seen))
            else:
                need=ObservationNeed('terrain-review/'+'/'.join(map(str,block)),block,'uncertain_history',
                    priority,max(0,deadline),lower_seen)
                if len(self.episodes)>=256:
                    # Keep all previous restrictions; require the new support
                    # to be sampled again instead of evicting a retry deadline.
                    self.capacity_exhausted=True
                    belief.require_review((block,),now_ns)
                    continue
            self.episodes[block]=need;self.pending[block]=need
        for block,need in self.episodes.items():
            if need.required_by_ns<=now_ns:
                previous=self.due_since.get(block)
                self.due_since[block]=(need.evidence_after_ns,now_ns if previous is None else previous[1])
        self.verified={b:t for b,t in self.verified.items() if b in cells}
        self.last_needs=tuple(sorted(self.pending.values(),key=lambda n:(n.priority,n.required_by_ns,n.block)))[:8]
        active=set(self.pending)
        self.blocked_since=min((since for b,(_,since) in self.due_since.items() if b in active),default=None)
        self.hold_blocks=tuple(sorted(b for b,(_,since) in self.due_since.items() if now_ns-since>=3_000_000_000))
        return self.last_needs

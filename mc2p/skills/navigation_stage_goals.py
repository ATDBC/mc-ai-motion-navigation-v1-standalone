"""Task-directed stage goals over observed, same-height terrain only."""
from collections import deque
from dataclasses import dataclass
import math
import time
import copy
import heapq

from mc2p.contracts.common import ContractViolation, require_finite, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v3 import AabbV3
from mc2p.skills.block_geometry import overlaps
from mc2p.skills.normal_navigation_guard import _inspect_sweep, BODY_MARGIN, COMBINED_BODY_MARGIN
from mc2p.skills.point_goal_policy import PointGoalPolicy
from mc2p.skills.terrain_spatial_index import box_tiles
from mc2p.skills.navigation_stage_geometry import StageGeometry,StageSupport


class FrameQueries:
    """Memoize immutable record/tile reads inside one synchronous plan only."""
    def __init__(self,source):
        self.source=source
        self.records={}
        self.collisions={}

    def get(self,key):
        if key not in self.records:self.records[key]=self.source.get(key)
        return self.records[key]

    def collision_candidates(self,bounds):
        key=box_tiles(bounds)
        if key not in self.collisions:self.collisions[key]=tuple(self.source.collision_candidates(bounds))
        return self.collisions[key]

    def positions_in_grid(self,minimum,maximum):
        return self.source.positions_in_grid(minimum,maximum)


class _StageOcclusionCache:
    """Bounded trusted-wall geometry, independent of stage trial/actor state."""
    def __init__(self):self.clear()

    def clear(self):
        self.entries={};self.facts={};self.binding=None;self.center=None
        self.hits=self.misses=0

    @staticmethod
    def _signature(fact):
        if fact is None:return None
        return fact[0],fact[1],bool(0<=fact[2]<=1 and fact[3] and fact[4] and not fact[5])

    def prepare(self,geometry_cache):
        if self.binding!=geometry_cache.binding:
            self.clear();self.binding=geometry_cache.binding
        self.center=geometry_cache.center
        floor=self.center[2];facts=geometry_cache.facts
        changed=(p for p in self.facts.keys()|facts.keys()
            if self._signature(self.facts.get(p))!=self._signature(facts.get(p)))
        changed_tiles=set();overflow=False
        for position in changed:
            for fact in (self.facts.get(position),facts.get(position)):
                if fact is None:continue
                collision,fluid=fact[:2]
                # Unsupported shapes never supply trusted occlusion. Changes
                # to/from known boxes are covered by the other side of the diff.
                if collision.kind=='unsupported':continue
                boxes=(AabbV3(0,0,0,1,1,1),) if collision.kind=='full_cube' else collision.boxes
                if fluid is not None:boxes=boxes+(AabbV3(0,0,0,1,1,1),)
                for box in boxes:
                    if box.max_y+position[1]<=floor+1 or box.min_y+position[1]>=floor+2.8:continue
                    tiles=box_tiles(AabbV3(box.min_x+position[0],box.min_y+position[1],box.min_z+position[2],
                        box.max_x+position[0],box.max_y+position[1],box.max_z+position[2]))
                    if tiles is None:overflow=True
                    else:changed_tiles.update(tiles)
        cx,cz,_=self.center
        self.entries={key:value for key,value in self.entries.items()
            if not overflow and key[2]==floor and abs(key[0]-cx)<=9 and abs(key[1]-cz)<=9
            and not value[1].intersection(changed_tiles)}
        self.facts=facts
        self.hits=self.misses=0

    def value(self,cell,floor,body,compute):
        cx,cz,cy=self.center
        if floor!=cy or abs(cell[0]-cx)>9 or abs(cell[1]-cz)>9:
            self.misses+=1
            return compute()
        key=(*cell,floor)
        if key in self.entries:
            self.hits+=1
            return self.entries[key][0]
        self.misses+=1
        value=compute()
        self.entries[key]=(value,frozenset(box_tiles(body)))
        return value


@dataclass(frozen=True)
class StagePlan:
    target: Vec3V0 | None
    waypoint: Vec3V0 | None
    path: tuple
    information: tuple
    reason: str
    summary: dict
    connector: object = None
    entry_cost_revision: int | None = None
    entry_waypoint: Vec3V0 | None = None


def known_segment(snapshot, view, now_ns, start, end, belief, *, control_margin=False,static_history=False):
    """Finite geometric path check, never authorization for an input or drift."""
    return _inspect_sweep(snapshot,view,now_ns,((start.x,start.z),(end.x,end.z)),
        round(start.y)-1,max(1,math.ceil(math.hypot(end.x-start.x,end.z-start.z)/.1)),
        margin=COMBINED_BODY_MARGIN if control_margin else BODY_MARGIN,historical=True,belief=belief,
        static_history=static_history)


class StageGoalSelector:
    def __init__(self,*,static_history=False,entry_cost=None):
        self.static_history=static_history
        self.connection_filter=None
        self.edge_filter=None
        # The optional callback returns nonnegative block-equivalent overhead
        # for this actual initial connector, never action permission.
        self.entry_cost=entry_cost
        self.entry_cost_revision=0
        self.geometry=StageGeometry(static_history=static_history)
        self._occlusion_cache=_StageOcclusionCache()
        self.clear()

    def clear(self):
        self.target=None
        self.attempts={}
        self._binding=None
        self._best_remaining=math.inf
        self._progress_at=None
        self._information=()
        self._arrived_at=None
        self._attempt_information={}
        self.need_reasons={}
        self.diagnostic={}
        self.geometry.support=None
        self.geometry.cache.clear()
        self._occlusion_cache.clear()

    def checkpoint(self):
        return {key:copy.copy(value) for key,value in self.__dict__.items()
            if key not in ('geometry','_occlusion_cache')}

    def restore(self,state):
        for key,value in state.items():setattr(self,key,value)

    def bind(self,snapshot,goal):
        binding=(snapshot.scope_id,snapshot.latest.stamp.scope,goal.goal_id,goal.position)
        if binding!=self._binding:
            self.clear();self._binding=binding

    def prepare(self,snapshot,view,now_ns,belief):
        terrain=FrameQueries(snapshot.terrain_index);markers=FrameQueries(belief)
        self.geometry.prepare(snapshot,view,now_ns,terrain,markers)
        if self.entry_cost is not None:self._occlusion_cache.prepare(self.geometry.cache)
        return terrain,markers

    def _trusted_occlusion(self,cell,floor,terrain,belief,now_ns):
        body=self.geometry._cell_body(cell,floor)
        def compute():
            for record,boxes,_ in terrain.collision_candidates(body):
                if boxes is None or not any(overlaps(body,b) for b in boxes):continue
                marker=belief.get(record.block.position)
                if (marker is not None and marker.history==record and not marker.contradicted
                        and 0<=now_ns-record.last_seen.request_start_ns<=60_000_000_000):
                    return True
            return False
        if self.entry_cost is None:return compute()
        return self._occlusion_cache.value(cell,floor,body,compute)

    def reject_current(self):
        if self.target is not None:
            key=(math.floor(self.target.x),math.floor(self.target.z))
            self.attempts[key]=min(8,self.attempts.get(key,0)+1)
            self._attempt_information[key]=self._information
            if len(self.attempts)>64:
                removed=next(iter(self.attempts));self.attempts.pop(removed)
                self._attempt_information.pop(removed,None)
        self.target=None
        self._progress_at=None
        self._best_remaining=math.inf
        self._arrived_at=None

    @staticmethod
    def neighbors(cell):
        x,z=cell
        return ((x+1,z),(x,z+1),(x-1,z),(x,z-1))

    def _entry_search(self,snapshot,view,now_ns,belief,start,legal,point,edge_clear,over):
        """At most five real entries and 256 settled cells, within caller budget.

        Only the first connection has measured execution overhead. Later edges
        retain geometric distance; no future body heading or action is invented.
        A zero-length seed must not bypass the cost of actually entering a route.
        """
        require_nonnegative_int(self.entry_cost_revision,'entry cost revision')
        parents={};distances={};weighted={};costs={};entries={};reports={};queue=[]
        own=view.base.own.position
        for cell in (start,)+self.neighbors(start):
            if over():break
            if cell not in legal:continue
            endpoint=point(cell)
            distance=math.hypot(endpoint.x-own.x,endpoint.z-own.z)
            if distance<.25:continue
            if self.connection_filter is not None and not self.connection_filter(own,endpoint):continue
            if cell!=start and self.edge_filter is not None and not self.edge_filter(start,cell):continue
            report=known_segment(snapshot,view,now_ns,own,endpoint,belief,
                control_margin=True,static_history=self.static_history)
            if report.reason is not None:continue
            cost=self.entry_cost(own,endpoint)
            require_finite(cost,'entry execution cost')
            if cost<0:raise ContractViolation('entry execution cost must be nonnegative')
            cost=float(cost)
            parents[cell]=None;distances[cell]=distance;weighted[cell]=distance+cost
            costs[cell]=cost;entries[cell]=cell;reports[cell]=report
            heapq.heappush(queue,(weighted[cell],cell))
        settled=set()
        while queue and len(settled)<256 and not over():
            weight,cell=heapq.heappop(queue)
            if cell in settled or weight!=weighted[cell]:continue
            settled.add(cell)
            for neighbor in self.neighbors(cell):
                if neighbor not in legal or neighbor in settled or not edge_clear(cell,neighbor):continue
                candidate=weight+1.
                if candidate>=weighted.get(neighbor,math.inf):continue
                weighted[neighbor]=candidate;distances[neighbor]=distances[cell]+1.
                parents[neighbor]=cell;costs[neighbor]=costs[cell];entries[neighbor]=entries[cell]
                heapq.heappush(queue,(candidate,neighbor))
        # A discovered-but-unsettled label is not a final minimum-cost route.
        return ({c:parents[c] for c in settled},{c:distances[c] for c in settled},
                {c:costs[c] for c in settled},{c:entries[c] for c in settled},reports,len(settled))

    def plan(self,snapshot,view,goal,now_ns,belief,started_ns,*,prepared=None,budget_ns=None):
        own=view.base.own
        binding=(snapshot.scope_id,snapshot.latest.stamp.scope,goal.goal_id,goal.position)
        if binding!=self._binding:
            self.bind(snapshot,goal);prepared=None
        floor=round(own.position.y)-1
        start=(math.floor(own.position.x),math.floor(own.position.z))
        dest=(math.floor(goal.position.x),math.floor(goal.position.z))
        geometry=self.geometry;geometry._current_view=view
        original_belief=belief
        terrain,belief=self.prepare(snapshot,view,now_ns,belief) if prepared is None else prepared
        config=geometry.config
        deadline=started_ns+(config.planning_budget_ns if budget_ns is None else budget_ns)
        def over():return time.perf_counter_ns()>=deadline
        points={}
        def point(cell):
            if cell not in points:points[cell]=Vec3V0(cell[0]+.5,own.position.y,cell[1]+.5)
            return points[cell]
        nearby=sorted({(p[0],p[2]) for p in terrain.positions_in_grid(
            (start[0]-8,floor,start[1]-8),(start[0]+8,floor,start[1]+8))
            if math.hypot(p[0]+.5-own.position.x,p[2]+.5-own.position.z)<=8},
            key=lambda c:(math.hypot(c[0]+.5-own.position.x,c[1]+.5-own.position.z),c))
        legal=set();rejected=[];checked=0
        for cell in nearby:
            if over():break
            checked+=1
            reason=geometry._cell_reason(cell,floor,now_ns,terrain,belief)
            if reason is None:legal.add(cell)
            else:rejected.append((cell,reason))
        parents={start:None} if start in legal else {}
        distances={start:0.} if parents else {}
        queue=deque(parents)
        expansions=0
        edges={}
        def edge_clear(cell,neighbor):
            if self.edge_filter is not None and not self.edge_filter(cell,neighbor):return False
            key=tuple(sorted((cell,neighbor)))
            if key not in edges:
                if self.entry_cost is not None:
                    cached=geometry.cache.cached_edge(cell,neighbor,own.position.y)
                    if cached is not None:
                        edges[key]=cached
                        return cached
                a,b=point(cell),point(neighbor)
                body=AabbV3(min(a.x,b.x)-BODY_MARGIN,own.position.y,min(a.z,b.z)-BODY_MARGIN,
                    max(a.x,b.x)+BODY_MARGIN,own.position.y+1.8,max(a.z,b.z)+BODY_MARGIN)
                edges[key]=geometry.cache.edge(cell,neighbor,own.position.y,body,lambda:
                    not any(any(overlaps(body,box) for box in (potential if boxes is None else boxes))
                        for _,boxes,potential in terrain.collision_candidates(body)))
            return edges[key]
        execution_costs={};entry_roots={};entry_reports={}
        if self.entry_cost is not None:
            parents,distances,execution_costs,entry_roots,entry_reports,expansions=self._entry_search(
                snapshot,view,now_ns,original_belief,start,legal,point,edge_clear,over)
        else:
            while queue and expansions<256 and not over():
                cell=queue.popleft();expansions+=1
                for neighbor in self.neighbors(cell):
                    if neighbor in legal and neighbor not in parents and edge_clear(cell,neighbor):
                        parents[neighbor]=cell;distances[neighbor]=distances[cell]+1
                        queue.append(neighbor)
        summary=dict(expansions=expansions,cells_checked=checked,candidates=(),
            cell_rejections=tuple(rejected[:64]),summary_truncated=len(rejected)>64,
            budget_exhausted=over())
        if over():return StagePlan(None,None,(),(),'budget',summary)
        reasons=dict(rejected)
        refreshable={'missing_support','uncertain_history','missing_field','contradiction'}
        self.need_reasons={}
        def need_reason(cell):
            if cell in legal:return None
            if cell not in reasons:
                reasons[cell]=geometry._cell_reason(cell,floor,now_ns,terrain,belief)
            reason=reasons[cell]
            if reason in refreshable:
                self.need_reasons[(cell[0],floor,cell[1])]=reason
                return reason
            return None
        for key,unknown in tuple(self._attempt_information.items()):
            if any(terrain.get(block) is not None and need_reason((block[0],block[2])) is None for block in unknown):
                self.attempts.pop(key,None);self._attempt_information.pop(key,None)
        complete=dest in distances and abs(goal.position.y-own.position.y)<.01
        if (not complete and self.target is not None
                and math.hypot(own.position.x-self.target.x,own.position.z-self.target.z)<.35):
            missing=tuple(b for b in self._information if need_reason((b[0],b[2])) is not None)
            if self._arrived_at is None:self._arrived_at=now_ns
            if missing and now_ns-self._arrived_at<2_000_000_000:
                self.diagnostic=dict(reason='observe_stage',target=self.target,path=(own.position,),
                    scores=(),attempts=len(self.attempts))
                return StagePlan(self.target,own.position,(own.position,),missing,'observe_stage',summary)
            self.reject_current()

        # A finite goal-directed corridor predicts relevant unknown floor, not
        # its state. A known wall stops the forecast, even if terrain behind it
        # happens to exist elsewhere in the client's retained memory.
        wall_cache={}
        def trusted_occlusion(c):
            if c in wall_cache:return wall_cache[c]
            blocked=self._trusted_occlusion(c,floor,terrain,belief,now_ns)
            wall_cache[c]=blocked
            return blocked
        def information(cell):
            p=point(cell);length=math.hypot(goal.position.x-p.x,goal.position.z-p.z)
            missing=[];blocked=False;seen=set()
            for index in range(1,min(16,math.ceil(length*2))+1):
                t=min(1.,index*.5/max(.01,length))
                c=(math.floor(p.x+t*(goal.position.x-p.x)),math.floor(p.z+t*(goal.position.z-p.z)))
                if c in seen:continue
                seen.add(c)
                if trusted_occlusion(c):
                    blocked=True;break
                if need_reason(c) is not None:missing.append((c[0],floor,c[1]))
            return tuple(missing[:8]),blocked

        rows=[]
        attempts_at_ranking=dict(self.attempts)
        for cell,distance in (() if complete else distances.items()):
            if over():break
            p=point(cell)
            if math.hypot(p.x-own.position.x,p.z-own.position.z)<.35 and cell!=dest:continue
            if cell in self.attempts:continue
            frontier=any(need_reason(n) is not None or (n not in legal and reasons.get(n) is None)
                         for n in self.neighbors(cell))
            if cell!=dest and not frontier and p!=self.target:continue
            missing,blocked=information(cell)
            remaining=math.hypot(p.x-goal.position.x,p.z-goal.position.z)
            gain=min(4,len(missing))
            score=distance+remaining+6*blocked-1.5*gain+8*self.attempts.get(cell,0)
            score+=execution_costs.get(cell,0.)
            rows.append((score,cell,missing,distance,remaining,blocked))
        if over():
            summary['budget_exhausted']=True
            return StagePlan(None,None,(),(),'budget',summary)
        if complete:
            goal_tail=(math.hypot(goal.position.x-point(dest).x,goal.position.z-point(dest).z)
                if self.entry_cost is not None else 0.)
            physical_distance=distances[dest]+goal_tail
            selected=(physical_distance+execution_costs.get(dest,0.),dest,(),physical_distance,0.,False)
            reason='known_goal'
        elif rows:
            selected=min(rows,key=lambda row:(row[0],row[1]));reason='information_stage'
            old=next((r for r in rows if point(r[1])==self.target),None)
            if old is not None and old[0]-selected[0]<1.:selected=old;reason='retain_stage'
        else:
            self.diagnostic=dict(reason='observe_here',target=None,path=(),scores=(),attempts=len(self.attempts))
            missing=information(start)[0];summary['budget_exhausted']=over()
            return StagePlan(None,own.position,(),missing, 'observe_here',summary)
        target=goal.position if complete else point(selected[1])
        cells=[];cell=selected[1]
        while cell is not None:
            cells.append(cell);cell=parents[cell]
        path=tuple(point(c) for c in reversed(cells))
        if self.entry_cost is not None:
            path=(own.position,)+path
            if complete and path[-1]!=goal.position:path=path+(goal.position,)
        elif complete:path=path[:-1]+(goal.position,)
        # The path's first cell center is behind us while leaving that cell.
        # Measure the continuous distance to the next path point instead.
        tail=path[1:] if len(path)>1 else path
        remaining=math.hypot(own.position.x-tail[0].x,own.position.z-tail[0].z)+sum(
            math.hypot(b.x-a.x,b.z-a.z) for a,b in zip(tail,tail[1:]))
        if target!=self.target:
            self._best_remaining=remaining;self._progress_at=now_ns
            self._arrived_at=None
        elif remaining<self._best_remaining-.25:
            self._best_remaining=remaining;self._progress_at=now_ns
        elif (self.entry_cost is None and self._progress_at is not None
                and now_ns-self._progress_at>3_000_000_000):
            self.reject_current()
            self.diagnostic=dict(reason='stage_no_progress',target=None,path=(),scores=(),attempts=len(self.attempts))
            return StagePlan(None,own.position,(),(),'stage_no_progress',summary)
        if reason!='stage_no_progress':self.target=target
        self._information=selected[2]
        waypoint=None;connector=None
        # Rejoin the farthest safe point on a short prefix, including corners
        # only when the complete connecting corridor is known and clear.
        connection_points=(point(entry_roots[selected[1]]),) if self.entry_cost is not None else reversed(path[:7])
        for p in connection_points:
            if over():break
            distance=math.hypot(p.x-own.position.x,p.z-own.position.z)
            if distance<.25 or distance>4:continue
            if self.connection_filter is not None and not self.connection_filter(own.position,p):continue
            report=(entry_reports[entry_roots[selected[1]]] if self.entry_cost is not None else
                known_segment(snapshot,view,now_ns,own.position,p,original_belief,control_margin=True,
                    static_history=self.static_history))
            if report.reason is None:
                waypoint=p;connector=report;break
        if waypoint is None:
            waypoint=own.position
            if self.connection_filter is not None:reason='connection_unavailable'
        summary['budget_exhausted']=over()
        overhead=execution_costs.get(selected[1],0.)
        # JointPlanner already scores the current control/gaze intervals. The
        # stage-only execution estimate must not be charged there a second time.
        summary['candidates']=(((math.floor(waypoint.x),math.floor(waypoint.z)),max(0.,selected[0]-overhead),0.,0.),)
        self.diagnostic=dict(reason=reason,target=target,path=path,scores=tuple(
            dict(cell=r[1],score=r[0],path_cost=r[3],goal_distance=r[4],information=len(r[2]),
                 known_occlusion=r[5],attempts=attempts_at_ranking.get(r[1],0))
            for r in sorted(rows)[:16]),attempts=len(self.attempts))
        if self.entry_cost is not None:
            self.diagnostic.update(execution_cost_blocks=overhead,chosen_entry=waypoint,
                entry_cost_revision=self.entry_cost_revision,score=selected[0],base_score=selected[0]-overhead,
                occlusion_cache_hits=self._occlusion_cache.hits,occlusion_cache_misses=self._occlusion_cache.misses)
            self.diagnostic['scores']=tuple(dict(row,
                execution_cost_blocks=execution_costs.get(row['cell'],0.),
                base_score=row['score']-execution_costs.get(row['cell'],0.))
                for row in self.diagnostic['scores'])
        return StagePlan(target,waypoint,path,selected[2],reason,summary,connector,
            self.entry_cost_revision if self.entry_cost is not None else None,
            waypoint if self.entry_cost is not None else None)

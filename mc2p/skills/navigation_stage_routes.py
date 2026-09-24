"""Retain stage routes; build fresh movement proposals against each new sample."""
from dataclasses import replace
import math
import time

from mc2p.contracts.observation_v3 import AabbV3
from mc2p.skills.block_geometry import overlaps
from mc2p.skills.normal_navigation_guard import BODY_MARGIN
from mc2p.skills.navigation_stage_goals import StageGoalSelector,known_segment


def distance(a,b):return math.hypot(a.x-b.x,a.z-b.z)


class StageRoutes:
    def __init__(self,*,static_history=False):
        self.static_history=static_history
        self.execution_coordinator=None
        self.selector=StageGoalSelector(static_history=static_history)
        self.clear()

    def clear(self):
        self.selector.clear()
        self.plan=None
        self.scan_degrees=0.
        self.scan_required=None
        self.scanning=False
        self._feedback_stamp=None
        self.idle_since=None
        self.now_ns=None
        self.last_full_ns=None
        self.tail=()
        self.work=dict(mode='idle',cell_hits=0,cell_misses=0)

    def checkpoint(self):
        return ({k:v for k,v in self.__dict__.items() if k not in ('selector','work')},self.selector.checkpoint())

    def restore(self,checkpoint):
        state,selector=checkpoint
        self.__dict__.update(state);self.selector.restore(selector)
        self.work=dict(self.work,mode='discarded')

    def _reuse(self,snapshot,view,goal,now_ns,belief,prepared,started,*,fallback=False):
        own=view.base.own.position;plan=self.plan
        if plan is None or plan.target is None or not self.tail or distance(own,plan.target)<.35:return None
        if (getattr(plan,'entry_cost_revision',None) is not None
                and plan.entry_cost_revision!=self.selector.entry_cost_revision):return None
        if abs(own.y-plan.target.y)>.01:return None
        # Information stages get a bounded refresh even without a geometry
        # change. A known complete route only needs validity/progress checks.
        if not fallback and plan.reason!='known_goal' and (self.last_full_ns is None or now_ns-self.last_full_ns>=250_000_000):return None
        terrain,markers=prepared;geometry=self.selector.geometry
        floor=round(own.y)-1
        # Recheck cached admission against current facts. Cardinal edges use
        # their exact swept rectangle; the body-to-waypoint connector below
        # remains a finite continuous check with the unchanged support rule.
        for p in self.tail:
            if geometry._cell_reason((math.floor(p.x),math.floor(p.z)),floor,now_ns,terrain,markers) is not None:return None
        for a,b in zip(self.tail,self.tail[1:]):
            if self.selector.edge_filter is not None and not self.selector.edge_filter(
                    (math.floor(a.x),math.floor(a.z)),(math.floor(b.x),math.floor(b.z))):return None
            if a.x!=b.x and a.z!=b.z:
                if known_segment(snapshot,view,now_ns,a,b,belief,static_history=self.static_history).reason is not None:return None
            else:
                body=AabbV3(min(a.x,b.x)-BODY_MARGIN,own.y,min(a.z,b.z)-BODY_MARGIN,
                    max(a.x,b.x)+BODY_MARGIN,own.y+1.8,max(a.z,b.z)+BODY_MARGIN)
                if any(any(overlaps(body,box) for box in (potential if boxes is None else boxes))
                    for _,boxes,potential in terrain.collision_candidates(body)):return None
        entry=getattr(plan,'entry_waypoint',None)
        indices=(self.tail.index(entry),) if entry in self.tail and distance(own,entry)>=.25 else reversed(range(min(7,len(self.tail))))
        for index in indices:
            if time.perf_counter_ns()-started>=10_000_000:return None
            point=self.tail[index]
            if not .25<=distance(own,point)<=4:continue
            if self.selector.connection_filter is not None and not self.selector.connection_filter(own,point):continue
            report=known_segment(snapshot,view,now_ns,own,point,belief,control_margin=True,static_history=self.static_history)
            if report.reason is not None:continue
            tail=self.tail[index:]
            remaining=distance(own,point)+sum(distance(a,b) for a,b in zip(tail,tail[1:]))
            if remaining<self.selector._best_remaining-.25:
                self.selector._best_remaining=remaining;self.selector._progress_at=now_ns
            elif self.selector._progress_at is not None and now_ns-self.selector._progress_at>3_000_000_000:
                return None
            self.tail=tail
            summary=dict(expansions=0,cells_checked=0,candidates=(((math.floor(point.x),math.floor(point.z)),
                max(0.,remaining+distance(plan.target,goal.position)),0.,0.),),cell_rejections=(),
                summary_truncated=False,budget_exhausted=False)
            path=(own,)+tail
            reason='known_goal' if plan.reason=='known_goal' else 'retain_stage'
            previous_cost=self.selector.diagnostic.get('execution_cost_blocks',0.)
            self.selector.diagnostic=dict(reason=reason,target=plan.target,path=path,scores=(),attempts=len(self.selector.attempts))
            if self.selector.entry_cost is not None:
                cost=previous_cost if entry==point and distance(own,entry)>=.25 else 0.
                base=summary['candidates'][0][1]
                self.selector.diagnostic.update(entry_cost_revision=self.selector.entry_cost_revision,
                    execution_cost_blocks=cost,chosen_entry=point,base_score=base,score=base+cost,
                    occlusion_cache_hits=0,occlusion_cache_misses=0)
            return replace(plan,waypoint=point,path=path,reason=reason,summary=summary,connector=report)
        return None

    def _route(self,snapshot,view,now_ns,goal,belief,started):
        self.now_ns=now_ns
        if self.scan_required and self.scan_degrees<360.-1e-6:
            self.scanning=True;self.work=dict(mode='scan',cell_hits=0,cell_misses=0)
            return (view.base.own.position,),dict(expansions=0,cells_checked=0,candidates=(),
                cell_rejections=(),summary_truncated=False,budget_exhausted=False)
        prepared=self.selector.prepare(snapshot,view,now_ns,belief)
        if self.execution_coordinator is not None:
            override=self.execution_coordinator.route_override(snapshot,view,now_ns,goal,belief,started,prepared)
            if override is not None:return override
        reused=self._reuse(snapshot,view,goal,now_ns,belief,prepared,started)
        if reused is not None:
            self.plan=reused;mode='reused'
        else:
            checkpoint=self.selector.checkpoint()
            # Leave two milliseconds for the short connector/proposal guard.
            # A smaller slice can starve the first complete route entirely.
            trial=self.selector.plan(snapshot,view,goal,now_ns,belief,started,prepared=prepared,budget_ns=8_000_000)
            if trial.summary['budget_exhausted']:
                self.selector.restore(checkpoint)
                fallback=self._reuse(snapshot,view,goal,now_ns,belief,prepared,started,fallback=True)
            else:fallback=None
            self.plan=fallback if fallback is not None else trial
            mode='fallback' if fallback is not None else 'built'
            if not trial.summary['budget_exhausted']:
                self.last_full_ns=now_ns;self.tail=self.plan.path
            if self.scan_required is None and not self.plan.summary['budget_exhausted']:
                self.scan_required=self.plan.reason!='known_goal'
            if fallback is None and not trial.summary['budget_exhausted'] and self.plan.connector is not None:
                index=self.tail.index(self.plan.waypoint)
                self.tail=self.tail[index:]
                self.plan=replace(self.plan,path=(view.base.own.position,)+self.tail)
                self.selector.diagnostic=dict(self.selector.diagnostic,path=self.plan.path)
        geometry=self.selector.geometry
        self.work=dict(mode=mode,cell_hits=geometry.cache.hits,cell_misses=geometry.cache.misses)
        self.scanning=bool(self.scan_required and self.scan_degrees<360.-1e-6)
        if self.scanning:
            self.work['mode']='scan'
            return (view.base.own.position,),dict(expansions=0,cells_checked=0,candidates=(),
                cell_rejections=(),summary_truncated=False,budget_exhausted=False)
        if self.plan.waypoint==view.base.own.position:
            if self.idle_since is None:self.idle_since=now_ns
        else:self.idle_since=None
        return (self.plan.waypoint,) if self.plan.waypoint is not None else (),self.plan.summary

"""Evaluator-only normal-player routes. Never import into actor/skills."""
from collections import Counter
import math


def search_goal(kind, elapsed_ns, home):
    x,z=home
    if kind=='enter': return x,z+13
    if kind in {'corner','search_wait'}: return x+4,z+13
    if kind=='escape': return x,z-400
    raise ValueError('undeclared search leader phase')


class SearchLeaderEvidence:
    """Verify actual self positions at declared route endpoints, not phase labels."""
    def __init__(self,plan,started_at_ns):
        self.plan=plan; self.started=started_at_ns; self.home=None
        self.counts=Counter()

    def observe(self,obs):
        own=obs['self_state']['value']
        if own is None: return
        position=own['position']
        if self.home is None:
            # Frozen native fixture starts the leader six blocks ahead of actor.
            self.home=(position['x'],position['z']-6)
        elapsed=obs['received_at_monotonic_ns']-self.started
        for phase in self.plan['phases']:
            kind=phase['kind']
            if kind not in {'enter','corner','search_wait'}: continue
            end=phase['start_ns']+phase['duration_ns']
            span=3_000_000_000 if kind=='search_wait' else 500_000_000
            if end-span<=elapsed<end:
                x,z=search_goal(kind,elapsed,self.home)
                self.counts[kind+':samples']+=1
                self.counts[kind+':at_goal']+=(math.hypot(position['x']-x,position['z']-z)<=.6
                                              and abs(position['y']+60)<=.01)

    def checks(self):
        return {'leader_actual_'+kind: self.counts[kind+':samples']>=minimum
                and self.counts[kind+':at_goal']/max(1,self.counts[kind+':samples'])>=.9
                for kind,minimum in (('enter',4),('corner',4),('search_wait',27))}

    def metrics(self): return dict(self.counts)

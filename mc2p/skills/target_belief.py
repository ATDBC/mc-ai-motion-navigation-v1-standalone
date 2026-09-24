"""Single authorized target's historical evidence and conditional search hints.

No hidden position predictor: centers below are ORIGINAL observations, radii
are heuristic search scales, and other_unknown is never removed. Units are
blocks and monotonic nanoseconds within the stamped clock domains.
"""
from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.follow_types import FollowEntity
from mc2p.skills.navigation_evidence import EvidenceStamp, ObservationPose
from mc2p.skills.navigation_memory import MemorySnapshot

RETENTION_NS = 60_000_000_000
FRESHNESS_NS = 500_000_000
PURPOSES = frozenset({'track','route_check','reacquire','search','contradiction'})


@dataclass(frozen=True, slots=True)
class TargetSample:
    entity: FollowEntity
    stamp: EvidenceStamp
    observer_pose: ObservationPose


@dataclass(frozen=True, slots=True)
class TargetHypothesis:
    kind: str
    center: Vec3V0 | None
    radius_blocks: float | None
    direction: tuple[float,float] | None
    evidence_level: str


@dataclass(frozen=True, slots=True)
class TargetBeliefView:
    track_id: str
    status: str
    visible_entity: FollowEntity | None
    last_seen: TargetSample | None
    samples: tuple[TargetSample,...]
    hypotheses: tuple[TargetHypothesis,...]
    last_seen_age_ns: int | None
    loss_started_ns: int | None
    checks: tuple
    assumptions: tuple[str,...] = ('ordinary_motion_only','server_state_age_unknown',
                                 'teleports_effects_unmodelled','search_scale_not_position_bound')


class TargetBelief:
    def __init__(self, track_id: str, snapshot: MemorySnapshot, now_ns: int):
        require_identifier(track_id,'belief target reference')
        require_nonnegative_int(now_ns,'belief time')
        latest=snapshot.latest
        if (type(snapshot) is not MemorySnapshot or snapshot.invalid_reason or latest is None
                or not latest.available or not latest.stamp.recent(now_ns,FRESHNESS_NS)
                or latest.coverage.entities_truncated):
            raise ContractViolation('belief binding requires fresh complete legal view')
        players=[e for e in latest.entities if e.entity_type=='minecraft:player']
        if len(players)!=1 or players[0].track_id!=track_id:
            raise ContractViolation('belief binding requires unique authorized visible player')
        self.track_id=track_id
        self._scope=(snapshot.scope_id,latest.stamp.scope,latest.stamp.source_backend)
        self._samples=[]
        self._checks=[]
        self._latest=None
        self._last_stamp=None
        self._last_now=now_ns
        self._invalid=False
        self._purpose='track'
        self._loss_started=None
        self.update(snapshot,now_ns)

    @property
    def observation_scope(self):
        """Immutable identity boundary for read-only delivery projections."""
        return None if self._invalid else (*self._scope[1],self._scope[2])

    def _invalidate(self, reason):
        self._invalid=True
        self._samples.clear(); self._checks.clear(); self._latest=None
        raise ContractViolation(reason)

    def _time(self,now_ns):
        require_nonnegative_int(now_ns,'belief time')
        if now_ns<self._last_now: self._invalidate('belief_time_regression')
        self._last_now=now_ns

    def update(self,snapshot: MemorySnapshot,now_ns: int,*,purpose='track') -> TargetBeliefView:
        self._time(now_ns)
        if self._invalid: raise ContractViolation('belief_requires_explicit_rebind')
        if purpose not in PURPOSES: raise ContractViolation('unknown_observation_purpose')
        latest=snapshot.latest
        if snapshot.invalid_reason or snapshot.scope_id!=self._scope[0]:
            self._invalidate('belief_scope_changed')
        if latest is not None:
            stamp=latest.stamp
            if (snapshot.scope_id,stamp.scope,stamp.source_backend)!=self._scope:
                self._invalidate('belief_scope_changed')
            if stamp.received_at_ns>now_ns: self._invalidate('belief_future_sample')
            old=self._last_stamp
            if old is not None:
                if stamp.sequence_id<old.sequence_id: self._invalidate('belief_sequence_regression')
                if stamp.sequence_id==old.sequence_id:
                    if stamp!=old or (self._latest is not None and latest!=self._latest):
                        self._invalidate('belief_sequence_rewritten')
                    self._purpose=purpose
                    return self.view(now_ns)
                if (stamp.request_start_ns<old.request_start_ns or stamp.received_at_ns<old.received_at_ns
                        or stamp.client_sample.started_at_monotonic_ns<old.client_sample.completed_at_monotonic_ns):
                    self._invalidate('belief_sample_time_regression')
            matches=[e for e in latest.entities if e.track_id==self.track_id] if latest.available else []
            if len(matches)>1 or any(e.entity_type!='minecraft:player' for e in matches):
                self._invalidate('belief_target_reference_invalid')
            if matches:
                self._samples.append(TargetSample(matches[0],stamp,latest.pose))
                self._samples=self._samples[-8:]
            self._latest,self._last_stamp=latest,stamp
        else:
            self._latest=None
        self._purpose=purpose
        return self.view(now_ns)

    def record_check(self,check):
        # A check is a confirmed collection, not a claim that a room is empty.
        from mc2p.skills.target_attention import TargetCheck
        if type(check) is not TargetCheck:
            raise ContractViolation('belief requires confirmed TargetCheck')
        if self._invalid or (check.scope_id,check.stamp.scope,check.stamp.source_backend)!=self._scope:
            raise ContractViolation('check_scope_mismatch')
        if check.stamp.received_at_ns>self._last_now:
            raise ContractViolation('check_requires_observed_sample')
        if self._checks and check.stamp.sequence_id<=self._checks[-1].stamp.sequence_id:
            return
        self._checks.append(check)
        self._checks=self._checks[-16:]

    def view(self,now_ns: int) -> TargetBeliefView:
        self._time(now_ns)
        self._samples=[s for s in self._samples if now_ns-s.stamp.request_start_ns<RETENTION_NS]
        self._checks=[c for c in self._checks if now_ns-c.stamp.request_start_ns<RETENTION_NS]
        if self._latest and now_ns-self._latest.stamp.request_start_ns>=RETENTION_NS:
            self._latest=None
        last=self._samples[-1] if self._samples else None
        visible=None
        if self._latest and self._latest.available and self._latest.stamp.recent(now_ns,FRESHNESS_NS):
            visible=next((e for e in self._latest.entities if e.track_id==self.track_id),None)
        age=None if last is None else now_ns-last.stamp.request_start_ns
        hints=[]
        if last is not None:
            radius=min(8.,.6+6*age/1_000_000_000)
            level='high' if age<=500_000_000 else 'medium' if age<=2_000_000_000 else 'low'
            hints.append(TargetHypothesis('last_seen',last.entity.position,radius,None,level))
            if len(self._samples)>=2:
                before=self._samples[-2]
                dt=(last.stamp.client_sample.completed_at_monotonic_ns-
                    before.stamp.client_sample.completed_at_monotonic_ns)/1_000_000_000
                dx=last.entity.position.x-before.entity.position.x
                dz=last.entity.position.z-before.entity.position.z
                distance=math.hypot(dx,dz)
                if .05<=dt<=1 and .02<distance<=6*dt and abs(last.entity.position.y-before.entity.position.y)<=1.5:
                    hints.append(TargetHypothesis('motion_direction',last.entity.position,radius,
                                                  (dx/distance,dz/distance),level))
        hints.append(TargetHypothesis('other_unknown',None,None,None,'unknown'))
        status=('invalidated' if self._invalid else 'visible' if visible else
                'reacquiring' if last and self._purpose=='route_check' else 'remembered' if last else 'lost')
        # Querying changes neither evidence nor its timestamp. It may notice
        # freshness expiry for the first time and latch that loss event once.
        if visible: self._loss_started=None
        elif not self._invalid and self._loss_started is None: self._loss_started=now_ns
        return TargetBeliefView(self.track_id,status,visible,last,tuple(self._samples),tuple(hints),
                               age,self._loss_started,tuple(self._checks))

"""Task-directed neutral looks with exact, bounded post-observation coverage.

This module never issues movement and never infers empty rooms from an absent
entity. Runtime selection/receipt confirmation is supplied by the sole driver.
"""
from dataclasses import dataclass, replace
import math

from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.navigation_evidence import EvidenceStamp, ObservationPose, ObservationCoverage
from mc2p.contracts.observation_v3 import ObservedBlockV3
from mc2p.skills.navigation_look import ObservationGate
from mc2p.skills.target_belief import PURPOSES
from mc2p.skills.perception_needs import PerceptionNeed


@dataclass(frozen=True, slots=True)
class TargetCheck:
    scope_id: str
    stamp: EvidenceStamp
    pose: ObservationPose
    coverage: ObservationCoverage
    blocks: tuple[ObservedBlockV3,...]
    point: Vec3V0
    purpose: str
    visible_track_ids: tuple[str,...]
    outcome: str


class TargetAttention:
    def __init__(self, *, perception=None, perception_owner='search_attention'):
        self.perception=perception
        self.perception_owner=perception_owner
        self.gate=ObservationGate()
        self.clear()

    def clear(self):
        self.gate.clear()
        if self.perception is not None:
            self.perception.clear_control('attention_cleared',owner=self.perception_owner)
        self.point=None
        self.purpose=None
        self._active_need=None

    def request(self,view,point,now_ns,*,purpose,pitch=None,snapshot=None,deadline_ns=None):
        if type(point) is not Vec3V0 or purpose not in PURPOSES:
            raise ContractViolation('invalid task observation request')
        own=view.base.own
        if own is None: raise ContractViolation('observation request needs self pose')
        distance=math.hypot(point.x-own.position.x,point.z-own.position.z)
        yaw=own.yaw if distance<.01 else math.degrees(math.atan2(-(point.x-own.position.x),point.z-own.position.z))
        if pitch is None:
            pitch=max(12,min(35,math.degrees(math.atan2(own.position.y+1.0-point.y,max(distance,.01)))))
        if self.perception is None:
            result=self.gate.request(view,yaw,pitch,now_ns)
        elif self.perception.variant=='smooth_only':
            # Keep the old request/confirmation goal; only the executor changes.
            self.gate.request(view,yaw,pitch,now_ns)
            result=self.perception.command_legacy(view,yaw,pitch,now_ns,owner=self.perception_owner)
        else:
            if snapshot is None or snapshot.latest is None:
                raise ContractViolation('active_attention_requires_navigation_evidence')
            deadline=min(now_ns+250_000_000,deadline_ns or now_ns+250_000_000)
            self._active_need=PerceptionNeed('attention-check',self.perception_owner,snapshot.scope_id,
                snapshot.latest.stamp,'search' if purpose=='reacquire' else purpose,70,deadline,
                point,'filtered_check')
            result=self.perception.decide(snapshot,view,(self._active_need,),None,None,now_ns,deadline).look
        self.point,self.purpose=point,purpose
        return result

    def feedback(self,selected,view,now_ns):
        self.gate.feedback(selected,view,now_ns)

    def consume(self,snapshot,view,now_ns):
        active=self.perception is not None and self.perception.variant=='active_perception_v1'
        if not active and not self.gate.confirmed(view,now_ns): return None
        latest,base=snapshot.latest,view.base
        if snapshot.invalid_reason or latest is None or not latest.available or latest.coverage is None:
            return None
        stamp,pose=latest.stamp,latest.pose
        if (stamp.scope!=(base.episode_id,base.controller_clock_id,base.client_clock_id)
                or stamp.sequence_id!=base.sequence_id or pose.position!=base.own.position
                or pose.yaw!=base.own.yaw or pose.pitch!=base.own.pitch
                or stamp.request_start_ns!=base.request_start_ns or stamp.received_at_ns!=base.received_at_ns
                or stamp.client_sample.started_at_monotonic_ns!=base.client_sample_start_ns
                or stamp.client_sample.completed_at_monotonic_ns!=base.client_sample_end_ns):
            return None
        if (latest.entities!=base.entities or latest.coverage.entities_truncated!=base.entities_truncated
                or pose.pose!=view.pose or pose.on_ground!=base.own.on_ground
                or pose.horizontal_collision!=base.own.horizontal_collision
                or latest.blocks!=base.observed_blocks):
            return None
        if active:
            if self._active_need is None:
                return None
            if now_ns>=self._active_need.deadline_ns or stamp.received_at_ns>=self._active_need.deadline_ns:
                self.perception.clear_control('attention_expired',owner=self.perception_owner)
                self._active_need=None
                return None
            current=replace(self._active_need,based_on=stamp)
            if not self.perception.consume_observation_need(current,latest,now_ns):
                return None
        ids=tuple(e.track_id for e in latest.entities)
        outcome=('incomplete_entity_sample' if latest.coverage.entities_truncated else
                 'entities_detected_in_filtered_sample' if ids else 'not_detected_in_filtered_sample')
        check=TargetCheck(snapshot.scope_id,stamp,pose,latest.coverage,latest.blocks,self.point,self.purpose,ids,outcome)
        self.clear()
        return check

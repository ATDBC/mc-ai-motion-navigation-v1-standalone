"""Bounded work-face goals and causal aim, never operations or world knowledge.

The active task routes its camera tick here until release. Arbitration and
receipt verification remain with its Runtime owner; feedback is not success.
"""
from dataclasses import dataclass,replace
import math

from mc2p.contracts.action_v1 import LookV1,MovementV1
from mc2p.contracts.common import ContractViolation,require_identifier,require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.skills.active_perception import ActivePerceptionCoordinator
from mc2p.skills.block_geometry import world_boxes
from mc2p.skills.navigation_views import PlaygroundView
from mc2p.skills.navigation_evidence import EvidenceStamp,NavigationEvidence
from mc2p.skills.navigation_memory import MemorySnapshot
from mc2p.skills.perception_needs import (
    AimConstraint,PerceptionNeed,PerceptionDecision,BLOCK_FACES,OPERATION_KINDS,_block,_stamp,
)
from mc2p.skills.targeting import block_target_matches


def _neutral(reason):
    return PerceptionDecision(MovementV1(),LookV1(),reason,None,(),(),None)


def _face_points(block,face):
    if block.fluid_id is not None or block.collision.kind=='unsupported':return ()
    result=[]
    for box in world_boxes(block):
        x,y,z=(box.min_x+box.max_x)/2,(box.min_y+box.max_y)/2,(box.min_z+box.max_z)/2
        point={'up':(x,box.max_y,z),'down':(x,box.min_y,z),
            'north':(x,y,box.min_z),'south':(x,y,box.max_z),
            'west':(box.min_x,y,z),'east':(box.max_x,y,z)}[face]
        result.append(Vec3V0(*point))
    return tuple(result)


def _distance(pose,point):
    own=pose.position
    return math.sqrt((point.x-own.x)**2+(point.y-own.y-1.62)**2+(point.z-own.z)**2)


@dataclass(frozen=True,slots=True)
class ObservedWorkFace:
    scope_id: str
    based_on: EvidenceStamp
    block: tuple[int,int,int]
    face: str
    aim_point: Vec3V0

    def __post_init__(self):
        require_identifier(self.scope_id,'work-face scope')
        _stamp(self.based_on,'work-face evidence')
        _block(self.block,'work-face block')
        if (self.block is None or type(self.face) is not str or self.face not in BLOCK_FACES
                or type(self.aim_point) is not Vec3V0):
            raise ContractViolation('invalid observed work face')


def select_work_face(evidence: NavigationEvidence,scope_id: str,*,
                     allowed_block_ids: tuple[str,...],face: str) -> ObservedWorkFace | None:
    """Pick current known geometry for the standing probe, not an observed face.

    Excluding the body's floor keeps the existing stationary mining scenario
    from choosing to undermine itself; this is not a new global safety rule.
    4.5 blocks is a conservative survival candidate bound, not reach permission.
    """
    if type(evidence) is not NavigationEvidence:raise ContractViolation('work selection requires evidence')
    require_identifier(scope_id,'work selection scope')
    if (type(allowed_block_ids) is not tuple or not 0<len(allowed_block_ids)<=64
            or type(face) is not str or face not in BLOCK_FACES):
        raise ContractViolation('invalid bounded work selection')
    for name in allowed_block_ids:require_identifier(name,'work block id')
    if (not evidence.available or evidence.pose is None or evidence.coverage is None
            or evidence.coverage.source_kind!='client_perception_filtered'):return None
    own=evidence.pose.position
    candidates=[]
    for block in evidence.blocks:
        if block.block_id not in allowed_block_ids:continue
        points=_face_points(block,face)
        if not points:continue
        if any(box.max_y<=own.y+.05 and box.min_x<own.x+.3 and box.max_x>own.x-.3
               and box.min_z<own.z+.3 and box.max_z>own.z-.3 for box in world_boxes(block)):
            continue
        current=block_target_matches(evidence.field_profile,evidence.targeting,
            block_position=block.position,face=face)
        for point in points:
            distance=_distance(evidence.pose,point)
            if 0<distance<=4.5:
                candidates.append((not current,distance,block.position,(point.x,point.y,point.z),point))
    if not candidates:return None
    _,_,block,_,point=min(candidates)
    return ObservedWorkFace(scope_id,evidence.stamp,block,face,point)


class InteractionAim:
    def __init__(self,coordinator: ActivePerceptionCoordinator):
        if type(coordinator) is not ActivePerceptionCoordinator or coordinator.variant!='active_perception_v1':
            raise ContractViolation('work aim requires the shared active coordinator')
        self.coordinator=coordinator
        self.target=None
        self._need=None
        self._operation=None

    def release(self,reason: str):
        require_identifier(reason,'work release reason')
        active=self.target is not None
        self.target=self._need=self._operation=None
        # The task releases work before handing the tick to another consumer.
        # Completion may already have cleared the need owner but left the last
        # gaze command; clear that too before an operation can be submitted.
        self.coordinator.clear_control(reason,owner=None if active else 'work_face')

    def begin(self,target: ObservedWorkFace,*,operation_kind: str,deadline_ns: int):
        if type(target) is not ObservedWorkFace:raise ContractViolation('work target must be typed')
        require_nonnegative_int(deadline_ns,'work deadline')
        if type(operation_kind) is not str or operation_kind not in OPERATION_KINDS:
            raise ContractViolation('invalid work operation kind')
        need=PerceptionNeed('interaction-work-face','work_face',target.scope_id,target.based_on,
            'aim',100,deadline_ns,target.aim_point,'center_block_face',block=target.block,face=target.face)
        self.release('work_replaced')
        self.target,self._need,self._operation=target,need,operation_kind

    def _stop(self,reason):
        self.release(reason)
        return _neutral(reason)

    def decide(self,snapshot: MemorySnapshot,view: PlaygroundView,now_ns: int) -> PerceptionDecision:
        require_nonnegative_int(now_ns,'work decision time')
        if type(snapshot) is not MemorySnapshot or type(view) is not PlaygroundView:
            raise ContractViolation('work aim requires exact snapshot and view')
        if self.target is None:return _neutral('interaction_aim_inactive')
        target,need,latest=self.target,self._need,snapshot.latest
        if now_ns>=need.deadline_ns:return self._stop('interaction_aim_expired')
        if (snapshot.invalid_reason or latest is None or not latest.available or latest.pose is None
                or snapshot.scope_id!=target.scope_id or latest.stamp.scope!=target.based_on.scope
                or latest.stamp.source_backend!=target.based_on.source_backend
                or latest.stamp.sequence_id<target.based_on.sequence_id):
            return self._stop('interaction_aim_scope_or_evidence_changed')
        if (not view.base.available or view.base.own is None or view.base.gui_open or view.base.own.dead
                or view.base.own.unsupported_motion or now_ns<latest.stamp.received_at_ns
                or not 0<=now_ns-latest.stamp.request_start_ns<=500_000_000):
            return self._stop('interaction_aim_body_or_time_invalid')
        matches=tuple(b for b in latest.blocks if b.position==target.block)
        if (len(matches)!=1 or target.aim_point not in _face_points(matches[0],target.face)
                or _distance(latest.pose,target.aim_point)>4.5):
            return self._stop('interaction_aim_current_geometry_missing')
        current=replace(need,based_on=latest.stamp)
        return self.coordinator.decide(snapshot,view,(current,),None,None,now_ns,
            min(need.deadline_ns,now_ns+250_000_000),aim=AimConstraint(current,self._operation,True))

    def feedback(self,selected: bool,evidence: NavigationEvidence,now_ns: int):
        self.coordinator.feedback(selected,evidence,now_ns)
        if not selected:self.release('interaction_aim_preempted')


class ActionEvidenceInvalidations:
    """Up to 64 old operation dependencies; a read never renews or forgets them."""
    def __init__(self):self.clear()

    def clear(self):
        self._records={}
        self._scope=None

    @staticmethod
    def _scope_of(stamp):return stamp.scope,stamp.source_backend

    def mark(self,blocks: tuple[tuple[int,int,int],...],stamp: EvidenceStamp):
        _stamp(stamp,'operation evidence')
        if type(blocks) is not tuple or not blocks or any(type(b) is not tuple for b in blocks):
            raise ContractViolation('operation dependencies must be a nonempty tuple')
        for block in blocks:_block(block,'operation dependency')
        if len(set(blocks))!=len(blocks) or len(set(self._records)|set(blocks))>64:
            raise ContractViolation('operation dependencies duplicate or exceed capacity')
        scope=self._scope_of(stamp)
        if self._scope is not None and scope!=self._scope:
            raise ContractViolation('operation scope change requires clear')
        for block in blocks:
            old=self._records.get(block)
            if old is not None and stamp!=old and not self.allows(block,stamp):
                raise ContractViolation('operation evidence regressed')
        self._scope=scope
        self._records.update((block,stamp) for block in blocks)

    def allows(self,block: tuple[int,int,int],stamp: EvidenceStamp) -> bool:
        _block(block,'operation dependency query')
        if block is None:raise ContractViolation('operation dependency query requires block')
        _stamp(stamp,'operation evidence query')
        if self._scope is not None and self._scope_of(stamp)!=self._scope:return False
        old=self._records.get(block)
        return old is None or (stamp.sequence_id>old.sequence_id and stamp.received_at_ns>=old.received_at_ns
            and stamp.request_start_ns>=old.request_start_ns
            and stamp.client_sample.started_at_monotonic_ns>=old.client_sample.completed_at_monotonic_ns)

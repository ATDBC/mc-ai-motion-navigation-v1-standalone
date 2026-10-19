"""Read-only block-history projection; candidate routes never authorize movement."""
import math
from types import MappingProxyType

from mc2p.contracts.common import require_nonnegative_int
from mc2p.skills.local_navigation import BlockRecord, RADIUS
from mc2p.skills.navigation_memory import MemorySnapshot
from mc2p.skills.navigation_views import PlaygroundView
from mc2p.skills.motion_guard import GuardGap, GuardReport, _inspect_motion
from mc2p.skills.perception_needs import MotionEvidenceReport


class NavigationBlockMap:
    def __init__(self, snapshot: MemorySnapshot):
        self._records = {item.block.position: BlockRecord(item.block,
            item.last_seen.sequence_id, item.last_seen.request_start_ns)
            for item in snapshot.terrain} if snapshot.invalid_reason is None else {}

    @property
    def records(self):
        return MappingProxyType(self._records)

    def prune(self, position, now_ns):
        require_nonnegative_int(now_ns, 'navigation prune time')
        self._records = {block: record for block,record in self._records.items()
            if math.hypot(block[0]+.5-position.x,block[2]+.5-position.z)<=RADIUS
            and abs(block[1]-position.y)<=RADIUS}


def report_navigation_motion(snapshot: MemorySnapshot, view: PlaygroundView, now_ns: int,
                             floor: int | None, yaw: float, *, jump: bool = False,
                             allowed_player_contact: str | None = None) -> MotionEvidenceReport:
    latest,base=snapshot.latest,view.base

    def denied(reason):
        return MotionEvidenceReport(snapshot.scope_id, None if latest is None else latest.stamp,
            GuardReport(reason, (GuardGap(None, reason, None),), 0, False), yaw, floor,
            jump, allowed_player_contact, (), None, False)

    if (snapshot.invalid_reason or latest is None or not latest.available
            or not base.available or base.own is None):
        return denied('navigation_observation_unavailable')
    stamp=latest.stamp
    if (stamp.scope != (base.episode_id,base.controller_clock_id,base.client_clock_id)
            or stamp.sequence_id!=base.sequence_id or latest.pose.position!=base.own.position
            or latest.pose.yaw!=base.own.yaw or latest.pose.pitch!=base.own.pitch
            or stamp.client_sample.started_at_monotonic_ns!=base.client_sample_start_ns
            or stamp.client_sample.completed_at_monotonic_ns!=base.client_sample_end_ns
            or stamp.request_start_ns!=base.request_start_ns or stamp.received_at_ns!=base.received_at_ns):
        return denied('navigation_observation_mismatch')
    if not stamp.recent(now_ns,500_000_000):
        return denied('stale_navigation_request')
    inspected = _inspect_motion(NavigationBlockMap(snapshot),view,now_ns,floor,yaw,
                               jump=jump,allowed_player_contact=allowed_player_contact)
    terrain = {item.block.position: item for item in snapshot.terrain}
    evidence = tuple((block, terrain[block].last_seen) for block in inspected.supports)
    expiry = stamp.request_start_ns+500_000_000
    if inspected.earliest_support_expiry_ns is not None:
        expiry = min(expiry, inspected.earliest_support_expiry_ns)
    return MotionEvidenceReport(snapshot.scope_id, stamp, inspected.guard, yaw, floor, jump,
        allowed_player_contact, evidence, expiry, inspected.guard.summary_truncated)


def check_navigation_motion(snapshot, view, now_ns, floor, yaw, *, jump=False,
                            allowed_player_contact=None):
    return report_navigation_motion(snapshot, view, now_ns, floor, yaw, jump=jump,
                                    allowed_player_contact=allowed_player_contact).reason

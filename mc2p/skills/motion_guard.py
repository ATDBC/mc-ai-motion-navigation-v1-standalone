"""Single bounded movement check; summaries never authorize unobserved space."""
from dataclasses import dataclass
import math

from mc2p.contracts.common import ContractViolation, require_identifier, require_nonnegative_int
from mc2p.skills.follow_types import MOVEMENT_FRESHNESS_NS
from mc2p.skills.follow_playground_types import PlaygroundView
from mc2p.skills.local_navigation import BODY_MARGIN, _support, _intersects, collision_obstructs
from mc2p.skills.local_navigation import prepare_obstacles as _sweep_obstacles
from mc2p.contracts.observation_v3 import AabbV3

MAX_SUMMARY = 64


@dataclass(frozen=True, slots=True)
class GuardGap:
    block: tuple[int, int, int] | None
    reason: str
    last_seen_ns: int | None

    def __post_init__(self):
        if self.block is not None and (type(self.block) is not tuple or len(self.block) != 3
                                      or any(type(v) is not int for v in self.block)):
            raise ContractViolation('gap block must be an integer triple')
        require_identifier(self.reason, 'gap reason')
        if self.last_seen_ns is not None:
            require_nonnegative_int(self.last_seen_ns, 'gap observation time')


@dataclass(frozen=True, slots=True)
class GuardReport:
    reason: str | None
    gaps: tuple[GuardGap, ...]
    inspected_steps: int
    summary_truncated: bool

    def __post_init__(self):
        if self.reason is not None:
            require_identifier(self.reason, 'guard reason')
        if (type(self.gaps) is not tuple or len(self.gaps) > MAX_SUMMARY
                or any(type(gap) is not GuardGap for gap in self.gaps)):
            raise ContractViolation('guard gaps must be a bounded immutable tuple')
        require_nonnegative_int(self.inspected_steps, 'guard inspected steps')
        if type(self.summary_truncated) is not bool:
            raise ContractViolation('guard truncation must be boolean')


@dataclass(frozen=True, slots=True)
class _Inspection:
    guard: GuardReport
    supports: tuple[tuple[int, int, int], ...]
    earliest_support_expiry_ns: int | None


def _inspect_motion(memory, view, now_ns, floor, yaw, *, jump=False, allowed_player_contact=None):
    base, own = view.base, view.base.own

    def early(reason):
        return _Inspection(GuardReport(reason, (GuardGap(None, reason, None),), 0, False), (), None)

    # Preserve the pre-extraction failure order, geometry and freshness boundaries.
    if not base.available or own is None or floor is None:
        return early('unknown_reachable_floor')
    if not 0 <= now_ns-base.received_at_ns <= MOVEMENT_FRESHNESS_NS:
        return early('stale_motion_state')
    if own.dead or own.unsupported_motion:
        return early('unsupported_motion_state')
    if own.horizontal_collision:
        return early('current_collision')
    speed = math.hypot(own.velocity.x, own.velocity.z)
    height = own.position.y-floor-1
    if speed > .65 or abs(own.velocity.y) > .6 or not -.01 <= height <= 1.5:
        return early('unsupported_velocity_or_height')
    memory.prune(own.position, now_ns)
    dx, dz = -math.sin(math.radians(yaw)), math.cos(math.radians(yaw))
    horizon = max(4.5, 14*speed) if jump else .45+2*speed
    ceiling = floor+1+(3.1 if jump else max(1.8, height+1.8))
    steps = math.ceil(horizon/.1)
    reason = None
    gaps, supports = {}, {}
    truncated = False
    expiry = None
    # Each immutable block has identical geometry for every swept body in this
    # inspection. Discard this projection at return; keep timestamps untouched.
    obstacles = _sweep_obstacles(memory,floor+1,ceiling)

    def reject(failure, block=None, top=None, detail=None):
        nonlocal reason, truncated
        if reason is None:
            reason = failure
        key = (block, detail or failure)
        if key not in gaps:
            if len(gaps)+len(supports) < MAX_SUMMARY:
                gaps[key] = GuardGap(block, detail or failure, top)
            else:
                truncated = True

    for i in range(steps+1):
        f = i/steps
        x, z = own.position.x+dx*horizon*f, own.position.z+dz*horizon*f
        drift_x, drift_z = 2*own.velocity.x*f, 2*own.velocity.z*f
        for bx in range(math.floor(x-BODY_MARGIN+min(0, drift_x)), math.floor(x+BODY_MARGIN+max(0, drift_x))+1):
            for bz in range(math.floor(z-BODY_MARGIN+min(0, drift_z)), math.floor(z+BODY_MARGIN+max(0, drift_z))+1):
                block = (bx, floor, bz)
                record = memory.records.get(block)
                if not _support(memory, (bx, bz), floor, now_ns, MOVEMENT_FRESHNESS_NS+1):
                    top = None if record is None else record.last_seen_ns
                    detail = ('support_not_observed' if record is None else
                              'support_time_invalid' if now_ns < top else
                              'support_expired' if now_ns-top > MOVEMENT_FRESHNESS_NS else
                              'unsupported_support_semantics')
                    reject('unknown_landing_support' if jump else 'unknown_footprint_support', block, top, detail)
                else:
                    # Aggregate across ALL checked support, including omitted summary items.
                    end = record.last_seen_ns+MOVEMENT_FRESHNESS_NS
                    expiry = end if expiry is None else min(expiry, end)
                    if block not in supports:
                        if len(gaps)+len(supports) < MAX_SUMMARY:
                            supports[block] = None
                        else:
                            truncated = True
        for ox in (0, drift_x):
            for oz in (0, drift_z):
                left, right = x+ox-BODY_MARGIN, x+ox+BODY_MARGIN
                bottom, top = z+oz-BODY_MARGIN, z+oz+BODY_MARGIN
                body = AabbV3(left,floor+1,bottom,right,ceiling,top)
                for block, record, boxes in obstacles:
                    if collision_obstructs(boxes,body):
                        reject('observed_body_or_head_obstacle', block, record.last_seen_ns)
                for entity in base.entities:
                    if entity.track_id == allowed_player_contact and entity.entity_type == 'minecraft:player':
                        continue
                    if (_intersects(floor+1, ceiling, entity.position.y, entity.position.y+entity.size.y)
                            and _intersects(left, right, entity.position.x-entity.size.x/2, entity.position.x+entity.size.x/2)
                            and _intersects(bottom, top, entity.position.z-entity.size.z/2, entity.position.z+entity.size.z/2)):
                        reject('observed_entity_contact')
    return _Inspection(GuardReport(reason, tuple(gaps.values()), steps+1, truncated), tuple(supports), expiry)


def inspect_playground_motion(memory, view: PlaygroundView, now_ns: int, floor: int | None,
                              yaw: float, *, jump: bool = False,
                              allowed_player_contact: str | None = None) -> GuardReport:
    return _inspect_motion(memory, view, now_ns, floor, yaw, jump=jump,
                           allowed_player_contact=allowed_player_contact).guard

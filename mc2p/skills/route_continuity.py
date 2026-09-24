"""Revalidated short geometric goals, not cached routes or movement permits."""
import math

from mc2p.contracts.common import ContractViolation, require_finite, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v3 import AabbV3
from mc2p.skills.block_geometry import overlaps
from mc2p.skills.local_navigation import BODY_MARGIN, _support, collision_obstructs, prepare_obstacles
from mc2p.skills.navigation_motion import NavigationBlockMap
from mc2p.skills.navigation_memory import MemorySnapshot
from mc2p.skills.follow_playground_types import PlaygroundView


def choose_continuous_waypoint(snapshot, view, goal: Vec3V0, now_ns: int, floor: int, *,
                               target_track_id: str, stop_distance: float,
                               anchor: Vec3V0 | None = None,
                               preserve_heading: bool = True) -> Vec3V0 | None:
    require_nonnegative_int(now_ns, 'continuous route time')
    require_finite(stop_distance, 'continuous route stop distance')
    if type(preserve_heading) is not bool:
        raise ContractViolation('continuous route heading preference must be bool')
    if (type(snapshot) is not MemorySnapshot or type(view) is not PlaygroundView
            or type(goal) is not Vec3V0 or anchor is not None and type(anchor) is not Vec3V0):
        raise ContractViolation('continuous route requires exact evidence and points')
    base, latest = view.base, snapshot.latest
    own = base.own
    targets = tuple(item for item in base.entities if item.track_id == target_track_id)
    target = targets[0] if len(targets)==1 else None
    if (snapshot.invalid_reason or latest is None or not latest.available or not base.available
            or own is None or target is None or base.gui_open or own.dead or own.horizontal_collision
            or own.unsupported_motion or base.entities_truncated or target.entity_type!='minecraft:player'
            or type(floor) is not int or stop_distance < 0
            or now_ns < base.received_at_ns or not 0 <= now_ns-base.request_start_ns <= 500_000_000
            or latest.stamp.scope != (base.episode_id, base.controller_clock_id, base.client_clock_id)
            or latest.stamp.sequence_id != base.sequence_id or latest.pose is None
            or latest.stamp.request_start_ns != base.request_start_ns
            or latest.stamp.received_at_ns != base.received_at_ns
            or latest.stamp.client_sample.started_at_monotonic_ns != base.client_sample_start_ns
            or latest.stamp.client_sample.completed_at_monotonic_ns != base.client_sample_end_ns
            or latest.pose.position != own.position or latest.pose.yaw != own.yaw
            or latest.pose.pitch != own.pitch or latest.pose.on_ground != own.on_ground
            or latest.pose.horizontal_collision != own.horizontal_collision or latest.pose.pose != view.pose
            or latest.entities != base.entities or latest.blocks != base.observed_blocks
            or latest.coverage is None or latest.coverage.entities_truncated != base.entities_truncated
            or goal.x != target.position.x or goal.z != target.position.z
            or abs(goal.y-floor-1) > .1):
        return None
    position = own.position
    dx, dz = goal.x-position.x, goal.z-position.z
    distance = math.hypot(dx, dz)
    maximum = min(3., distance-stop_distance)
    if maximum < .5:
        return None
    memory = NavigationBlockMap(snapshot)
    memory.prune(position, now_ns)
    feet = floor+1.
    obstacles = prepare_obstacles(memory, feet, feet+1.8)

    def allowed(point):
        length = math.hypot(point.x-position.x, point.z-position.z)
        remaining = math.hypot(point.x-goal.x, point.z-goal.z)
        if (not .4999999 <= length <= 3.0000001 or abs(point.y-feet) > .1
                or remaining >= distance-.1 or remaining < stop_distance-1e-7):
            return False
        # Conservative enclosing rectangle: every part of the swept body lies
        # inside it. No sampling gaps and no inferred unobserved floor cells.
        body = AabbV3(min(position.x,point.x)-BODY_MARGIN,feet,
            min(position.z,point.z)-BODY_MARGIN,max(position.x,point.x)+BODY_MARGIN,
            feet+1.8,max(position.z,point.z)+BODY_MARGIN)
        for x in range(math.floor(body.min_x), math.floor(body.max_x)+1):
            for z in range(math.floor(body.min_z), math.floor(body.max_z)+1):
                if not _support(memory,(x,z),floor,now_ns,60_000_000_000):
                    return False
        if any(collision_obstructs(boxes,body) for _,_,boxes in obstacles):
            return False
        for entity in base.entities:
            p,s = entity.position,entity.size
            if overlaps(body,AabbV3(p.x-s.x/2,p.y,p.z-s.z/2,p.x+s.x/2,p.y+s.y,p.z+s.z/2)):
                return False
        return True

    if (anchor is not None and math.hypot(anchor.x-position.x,anchor.z-position.z) > .5000001
            and allowed(anchor)):
        return anchor
    bearing = math.degrees(math.atan2(-dx,dz))
    directions = []
    if preserve_heading and abs((own.yaw-bearing+180)%360-180) <= 8.:
        directions.append((-math.sin(math.radians(own.yaw)),math.cos(math.radians(own.yaw))))
    directions.append((dx/distance,dz/distance))
    for length in dict.fromkeys(min(maximum,value) for value in (3.,2.,1.,.5)):
        for ux,uz in directions:
            point = Vec3V0(position.x+ux*length,feet,position.z+uz*length)
            if allowed(point):
                return point
    return None

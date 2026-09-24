"""Finite steering points inside the existing next route cell, not a new path.

Geometry predicts a useful direction only. The caller must turn with neutral
input, observe again and apply the unchanged full actual-heading motion guard.
"""
import math

from mc2p.contracts.common import ContractViolation, require_finite, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0


def _wrap(angle):
    return (angle+180.)%360.-180.


def _ray_point(own, cell, yaw, height):
    """Midpoint of a forward ray's finite intersection with the open cell."""
    direction=(-math.sin(math.radians(yaw)),math.cos(math.radians(yaw)))
    enter,leave=0.,float('inf')
    for origin,component,low in zip((own.x,own.z),direction,cell):
        low,high=low+.001,low+.999
        if abs(component)<1e-12:
            if not low<=origin<=high:
                return None
            continue
        before,after=sorted(((low-origin)/component,(high-origin)/component))
        enter,leave=max(enter,before),min(leave,after)
    if leave<=enter or not math.isfinite(leave) or leave<=0:
        return None
    distance=(enter+leave)/2
    return Vec3V0(own.x+direction[0]*distance,height,own.z+direction[1]*distance)


def choose_route_waypoint(view, route, original, *, target_track_id, margin_degrees,
                          horizontal_fov_degrees=90.):
    """Keep the route; return a point, or None for a geometric conflict.

    Missing target/route leaves the original consumer semantics untouched.
    At most eleven points are considered (two rays and nine in-cell points).
    No memory, world handle, terrain edits, or alternate-cell search is used.
    """
    require_finite(margin_degrees,'steering target margin')
    require_finite(horizontal_fov_degrees,'steering horizontal FOV')
    if horizontal_fov_degrees not in (90., 120.) or not 0<=margin_degrees<horizontal_fov_degrees/2:
        raise ContractViolation('steering margin outside declared horizontal FOV')
    own=view.base.own
    target=next((e for e in view.base.entities if e.track_id==target_track_id),None)
    if not view.base.available or own is None or target is None or len(route.cells)<2:
        return original
    current=(math.floor(own.position.x),math.floor(own.position.z))
    cell=route.cells[1]
    if (route.reason!='candidate_route' or route.cells[0]!=current
            or abs(cell[0]-current[0])+abs(cell[1]-current[1])!=1):
        return original
    dx,dz=target.position.x-own.position.x,target.position.z-own.position.z
    if math.hypot(dx,dz)<.01:
        return original
    bearing=math.degrees(math.atan2(-dx,dz))
    # The caller supplies its observation's profile (legacy default 90). This inner
    # region is a preference for proposals, not a proof of future visibility.
    half=horizontal_fov_degrees/2-margin_degrees
    retained=_ray_point(own.position,cell,own.yaw,original.y)
    if retained is not None and abs(_wrap(own.yaw-bearing))<=half:
        return retained
    toward=_ray_point(own.position,cell,bearing,original.y)
    points=([point for point in (retained,toward) if point is not None]+
        [Vec3V0(cell[0]+x,original.y,cell[1]+z) for x in (.15,.5,.85) for z in (.15,.5,.85)])
    ranked=[]
    route_yaw=math.degrees(math.atan2(-(original.x-own.position.x),original.z-own.position.z))
    for point in points:
        yaw=math.degrees(math.atan2(-(point.x-own.position.x),point.z-own.position.z))
        error=abs(_wrap(yaw-bearing))
        if error<=horizontal_fov_degrees/2-1.:
            # Preserve route progress whenever it already meets the target's
            # inner margin. Aiming straight at a moving entity can otherwise
            # cut across cells and force a new stop at every cell transition.
            ranked.append((error>half,abs(_wrap(yaw-route_yaw)),abs(_wrap(yaw-own.yaw)),
                           error,point.x,point.z,point))
    return min(ranked,key=lambda item:item[:-1])[-1] if ranked else None


def route_observation_frontier(snapshot, view, *, target_track_id, floor, stop_distance, now_ns):
    """First missing support on a bounded viewing ray, NEVER a traversable path.

    Used only after the existing route's next cell has no tracking-compatible
    steering point. New evidence may let the unchanged BFS choose another route.
    The stopping condition uses the same cell-center disk as that BFS.
    """
    from mc2p.skills.local_navigation import RADIUS, _obstacle, _support
    from mc2p.skills.block_geometry import is_supported_floor
    from mc2p.skills.navigation_motion import NavigationBlockMap
    require_nonnegative_int(now_ns, 'route observation time')
    require_finite(stop_distance, 'route observation stop distance')
    if stop_distance < 0:
        raise ContractViolation('route observation stop distance must be nonnegative')
    own=view.base.own
    target=next((item for item in view.base.entities if item.track_id==target_track_id),None)
    if (not view.base.available or own is None or target is None or type(floor) is not int
            or snapshot.invalid_reason or snapshot.latest is None or snapshot.latest.coverage is None):
        return None
    dx,dz=target.position.x-own.position.x,target.position.z-own.position.z
    distance=math.hypot(dx,dz)
    if distance<=.01:
        return None
    direction=(dx/distance,dz/distance)
    cell=[math.floor(own.position.x),math.floor(own.position.z)]
    signs=[1 if value>0 else -1 if value<0 else 0 for value in direction]
    delta=[abs(1/value) if value else math.inf for value in direction]
    crossing=[((cell[index]+(1 if signs[index]>0 else 0)-origin)/direction[index]
               if signs[index] else math.inf)
              for index,origin in enumerate((own.position.x,own.position.z))]
    surfaces=NavigationBlockMap(snapshot)
    coverage=snapshot.latest.coverage
    target_yaw=math.degrees(math.atan2(-dx,dz))
    target_pitch=math.degrees(math.atan2(own.position.y+1.62-target.position.y-target.size.y/2,distance))
    for _ in range(2*math.ceil(RADIUS)+2):
        cx,cz=cell
        if math.hypot(cx+.5-own.position.x,cz+.5-own.position.z)>RADIUS:
            return None
        if _obstacle(surfaces,view.base,cx+.5,cz+.5,floor):
            return None
        block=(cx,floor,cz)
        record=surfaces.records.get(block)
        if record is not None and not is_supported_floor(record.block):
            return None
        if not _support(surfaces,(cx,cz),floor,now_ns,60_000_000_000):
            point=Vec3V0(cx+.5,floor+1.,cz+.5)
            px,pz=point.x-own.position.x,point.z-own.position.z
            yaw=math.degrees(math.atan2(-px,pz))
            pitch=math.degrees(math.atan2(own.position.y+1.62-point.y,math.hypot(px,pz)))
            if (abs(_wrap(yaw-target_yaw))<=coverage.horizontal_fov_degrees/2-1.
                    and abs(pitch-target_pitch)<=coverage.vertical_fov_degrees/2-1.):
                return block,point
            return None
        if math.hypot(cx+.5-target.position.x,cz+.5-target.position.z)<=stop_distance:
            return None
        next_crossing=min(crossing)
        if next_crossing>min(distance,RADIUS):
            return None
        # Advance both coordinates at exact corner crossings; this is a ray
        # for choosing an observation, not a diagonal movement instruction.
        for index in range(2):
            if crossing[index]==next_crossing:
                cell[index]+=signs[index]
                crossing[index]+=delta[index]
    return None

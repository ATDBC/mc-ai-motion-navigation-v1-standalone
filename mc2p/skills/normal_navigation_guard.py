"""Belief-aware standing-body guard for the frozen forward/stop baseline."""
from __future__ import annotations

import math

from mc2p.contracts.common import ContractViolation, require_finite, require_nonnegative_int
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v3 import AabbV3
from mc2p.skills.block_geometry import is_supported_floor, overlaps
from mc2p.skills.follow_playground_types import PlaygroundView
from mc2p.skills.local_navigation import BODY_MARGIN, prepare_block_collision
from mc2p.skills.motion_guard import GuardGap, GuardReport, MAX_SUMMARY
from mc2p.skills.navigation_belief import BeliefStore, HISTORICAL_SUPPORT_NS, support_admission
from mc2p.skills.navigation_memory import MemorySnapshot, TerrainHistory
from mc2p.skills.navigation_recovery import classify_rejection
from mc2p.skills.normal_direction_control import world_direction
from mc2p.skills.normal_navigation_types import ControlProposal


STANDING_HEIGHT = 1.8
SAMPLE_STEP = .1
COMBINED_BODY_MARGIN = BODY_MARGIN + .10


def _early(reason: str) -> GuardReport:
    return GuardReport(reason, (GuardGap(None, reason, None),), 0, False)


def _terrain(snapshot: MemorySnapshot) -> dict[tuple[int, int, int], TerrainHistory]:
    return snapshot.terrain_index


def _bound(snapshot: MemorySnapshot, view: PlaygroundView, now_ns: int) -> str | None:
    latest, base, own = snapshot.latest, view.base, view.base.own
    if snapshot.invalid_reason is not None or latest is None or not latest.available:
        return "control_unavailable"
    if not base.available or own is None:
        return "control_unavailable"
    if base.entities_truncated:
        return "control_unavailable"
    stamp = latest.stamp
    if (
        stamp.scope != (base.episode_id, base.controller_clock_id, base.client_clock_id)
        or stamp.sequence_id != base.sequence_id
        or latest.pose is None
        or latest.pose.position != own.position
        or latest.pose.yaw != own.yaw
        or latest.pose.pitch != own.pitch
        or stamp.client_sample.started_at_monotonic_ns != base.client_sample_start_ns
        or stamp.client_sample.completed_at_monotonic_ns != base.client_sample_end_ns
        or stamp.request_start_ns != base.request_start_ns
        or stamp.received_at_ns != base.received_at_ns
    ):
        return "control_unavailable"
    if not 0 <= now_ns - stamp.request_start_ns <= 500_000_000:
        return "control_unavailable"
    if (
        own.dead or own.unsupported_motion or not own.on_ground
        or view.pose != "standing" or view.game_mode != "survival"
        or bool(view.status_effect_ids) or base.gui_open
    ):
        return "unsupported_motion"
    if own.horizontal_collision:
        return "known_obstacle"
    speed = math.hypot(own.velocity.x, own.velocity.z)
    if speed > .65 or abs(own.velocity.y) > .6:
        return "unsupported_motion"
    return None


def _neighborhood_complete(
    terrain: dict[tuple[int, int, int], TerrainHistory],
    belief: BeliefStore,
    block: tuple[int, int, int],
    now_ns: int,
    view: PlaygroundView,
) -> bool:
    if view.base.entities_truncated:
        return False
    x, y, z = block
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            grid = (x + dx, y, z + dz)
            record = terrain.get(grid)
            marker = belief.get(grid)
            if record is None or marker is None or marker.history != record or marker.contradicted:
                return False
            if support_admission(record, now_ns, historical=True,
                                 high_consequence=False, contradicted=False) is not None:
                return False
    # A one-cell context must not hide a retained obstruction, contradiction,
    # or current entity.  Complete floor records alone are insufficient.
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            for by in (y, y+1, y+2):
                position = (x+dx, by, z+dz)
                record = terrain.get(position)
                if record is None:
                    continue
                marker = belief.get(position)
                if marker is None or marker.history != record or marker.contradicted:
                    return False
                if by > y and (record.block.fluid_id is not None
                               or record.block.collision.kind != "empty"):
                    return False
                if by == y and record.block.collision.kind != "empty" \
                        and not is_supported_floor(record.block):
                    return False
    for entity in view.base.entities:
        if (abs(entity.position.x-(x+.5)) <= 1.5+entity.size.x/2
                and abs(entity.position.z-(z+.5)) <= 1.5+entity.size.z/2):
            return False
    return True


def _support_reason(record, now_ns, *, historical, contradicted, terrain, belief, block, view):
    """Inspect neighboring history only when it can change support admission."""
    reason = support_admission(record, now_ns, historical=historical,
                               high_consequence=True, contradicted=contradicted)
    if reason != "uncertain_history" or not historical:
        return reason
    # Only the age limit depends on context. Fresh records and decisive
    # rejections have already returned; history beyond retention cannot improve.
    if now_ns - record.last_seen.request_start_ns > HISTORICAL_SUPPORT_NS:
        return reason
    if not _neighborhood_complete(terrain, belief, block, now_ns, view):
        return reason
    return support_admission(record, now_ns, historical=historical,
                             high_consequence=False, contradicted=contradicted)


def _convex_hull(points: tuple[tuple[float, float], ...]) -> tuple[tuple[float, float], ...]:
    ordered = sorted(set(points))
    if len(ordered) <= 1:
        return tuple(ordered)

    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])

    lower = []
    for point in ordered:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 1e-12:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(ordered):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 1e-12:
            upper.pop()
        upper.append(point)
    return tuple(lower[:-1] + upper[:-1])


def _segment_open_rect(start, end, low_x, low_z, high_x, high_z) -> bool:
    low_t, high_t = 0., 1.
    for origin, target, low, high in (
        (start[0], end[0], low_x, high_x),
        (start[1], end[1], low_z, high_z),
    ):
        delta = target-origin
        if abs(delta) < 1e-12:
            if not low < origin < high:
                return False
        else:
            first, last = sorted(((low-origin)/delta, (high-origin)/delta))
            low_t, high_t = max(low_t, first), min(high_t, last)
            if low_t >= high_t:
                return False
    return True


def _polygon_open_rect(poly, low_x, low_z, high_x, high_z) -> bool:
    if len(poly) == 1:
        return low_x < poly[0][0] < high_x and low_z < poly[0][1] < high_z
    if len(poly) == 2:
        return _segment_open_rect(poly[0], poly[1], low_x, low_z, high_x, high_z)
    rectangle = ((low_x, low_z), (low_x, high_z),
                 (high_x, low_z), (high_x, high_z))
    axes = [(1., 0.), (0., 1.)]
    for index, point in enumerate(poly):
        other = poly[(index+1) % len(poly)]
        edge = (other[0]-point[0], other[1]-point[1])
        axes.append((-edge[1], edge[0]))
    for axis in axes:
        p = tuple(point[0]*axis[0]+point[1]*axis[1] for point in poly)
        r = tuple(point[0]*axis[0]+point[1]*axis[1] for point in rectangle)
        if max(p) <= min(r)+1e-9 or max(r) <= min(p)+1e-9:
            return False
    return True


def _inspect_sweep(
    snapshot: MemorySnapshot,
    view: PlaygroundView,
    now_ns: int,
    sweep: tuple[tuple[float, float], ...],
    floor: int,
    steps: int,
    *,
    margin: float,
    historical: bool,
    belief: BeliefStore,
    static_history: bool = False,
) -> GuardReport:
    """Apply the shared support, collision, and summary policy to one hull."""
    base, own = view.base, view.base.own
    assert own is not None
    terrain = _terrain(snapshot)
    causes: list[str] = []
    summaries: dict[tuple[tuple[int, int, int] | None, str], GuardGap] = {}
    truncated = False

    def reject(reason: str, block=None, seen=None) -> None:
        nonlocal truncated
        causes.append(reason)
        key = (block, reason)
        if key in summaries:
            return
        if len(summaries) < MAX_SUMMARY:
            summaries[key] = GuardGap(block, reason, seen)
        else:
            truncated = True

    min_x = min(point[0] for point in sweep)-margin
    max_x = max(point[0] for point in sweep)+margin
    min_z = min(point[1] for point in sweep)-margin
    max_z = max(point[1] for point in sweep)+margin
    for bx in range(math.floor(min_x), math.floor(max_x)+1):
        for bz in range(math.floor(min_z), math.floor(max_z)+1):
            if not _polygon_open_rect(sweep, bx-margin, bz-margin,
                                      bx+1+margin, bz+1+margin):
                continue
            block = (bx, floor, bz)
            record = terrain.get(block)
            marker = belief.get(block)
            if marker is not None and marker.history != record:
                reject("control_unavailable", block,
                       None if record is None else record.last_seen.request_start_ns)
                continue
            contradicted = marker.contradicted if marker is not None else False
            support_check=_support_reason
            if static_history:
                from mc2p.skills.navigation_terrain_review import static_support_reason
                support_check=static_support_reason
            reason = support_check(record, now_ns, historical=historical,
                contradicted=contradicted, terrain=terrain, belief=belief, block=block, view=view)
            if reason is not None:
                reject(reason, block,
                       None if record is None else record.last_seen.request_start_ns)

    bounds = AabbV3(min_x,own.position.y,min_z,max_x,own.position.y+STANDING_HEIGHT,max_z)
    for record, boxes, potential_boxes in terrain.collision_candidates(bounds):
        for box in potential_boxes:
            if box.max_y <= own.position.y+1e-9 or box.min_y >= own.position.y+STANDING_HEIGHT-1e-9:
                continue
            if _polygon_open_rect(sweep, box.min_x-margin, box.min_z-margin,
                                  box.max_x+margin, box.max_z+margin):
                marker = belief.get(record.block.position)
                if marker is None or marker.history != record:
                    obstacle_reason = "control_unavailable"
                elif boxes is None:
                    obstacle_reason = "missing_field"
                elif marker.contradicted:
                    obstacle_reason = "contradiction"
                else:
                    obstacle_reason = "known_obstacle"
                reject(obstacle_reason, record.block.position,
                       record.last_seen.request_start_ns)
    for entity in base.entities:
        if (entity.position.y+entity.size.y <= own.position.y+1e-9
                or entity.position.y >= own.position.y+STANDING_HEIGHT-1e-9):
            continue
        if _polygon_open_rect(sweep,
            entity.position.x-entity.size.x/2-margin,
            entity.position.z-entity.size.z/2-margin,
            entity.position.x+entity.size.x/2+margin,
            entity.position.z+entity.size.z/2+margin):
            reject("known_obstacle")

    reason = classify_rejection(tuple(causes)) if causes else None
    return GuardReport(reason, tuple(summaries.values()), steps + 1, truncated)


def check_normal_segment(
    snapshot: MemorySnapshot,
    view: PlaygroundView,
    now_ns: int,
    end: Vec3V0,
    *,
    historical: bool,
    belief: BeliefStore,
    static_history: bool = False,
) -> GuardReport:
    """Check the full same-height straight sweep and release-drift envelope.

    The bounded gap tuple is diagnostic only.  Rejection classification is
    computed from every inspected support, block and entity before truncation.
    """
    if type(snapshot) is not MemorySnapshot or type(view) is not PlaygroundView:
        raise ContractViolation("normal segment requires formal memory and playground view")
    if type(end) is not Vec3V0 or type(belief) is not BeliefStore:
        raise ContractViolation("normal segment requires Vec3V0 and BeliefStore")
    require_nonnegative_int(now_ns, "normal segment time")
    if type(historical) is not bool or type(static_history) is not bool:
        raise ContractViolation("historical admission must be boolean")
    for value, name in ((end.x, "normal segment end x"),
                        (end.y, "normal segment end y"),
                        (end.z, "normal segment end z")):
        require_finite(value, name)
    bound = _bound(snapshot, view, now_ns)
    if bound is not None:
        return _early(bound)
    if (belief.scope_id != snapshot.scope_id or snapshot.latest is None
            or belief.evidence_scope != snapshot.latest.stamp.scope):
        return _early("control_unavailable")

    base, own = view.base, view.base.own
    assert own is not None
    floor_float = own.position.y - 1
    floor = round(floor_float)
    if abs(floor_float - floor) > .01 or abs(end.y - own.position.y) > .1:
        return _early("unsupported_motion")
    dx, dz = end.x - own.position.x, end.z - own.position.z
    distance = math.hypot(dx, dz)
    if distance > 8.0:
        raise ContractViolation("normal segment exceeds frozen planning radius")
    speed = math.hypot(own.velocity.x, own.velocity.z)
    if distance < .01:
        if speed > 1e-6:
            direction_x, direction_z = own.velocity.x/speed, own.velocity.z/speed
        else:
            radians = math.radians(own.yaw)
            direction_x, direction_z = -math.sin(radians), math.cos(radians)
    else:
        direction_x, direction_z = dx / distance, dz / distance
    horizon = max(distance, .45 + 2 * speed)
    steps = max(1, math.ceil(horizon / SAMPLE_STEP))
    endpoints = tuple(
        (own.position.x + direction_x*horizon + drift_x,
         own.position.z + direction_z*horizon + drift_z)
        for drift_x in (0., 2*own.velocity.x)
        for drift_z in (0., 2*own.velocity.z)
    )
    sweep = _convex_hull(((own.position.x, own.position.z),) + endpoints)
    return _inspect_sweep(
        snapshot,
        view,
        now_ns,
        sweep,
        floor,
        steps,
        margin=BODY_MARGIN,
        historical=historical,
        belief=belief,
        static_history=static_history,
    )


def check_control_proposal(
    snapshot: MemorySnapshot,
    view: PlaygroundView,
    now_ns: int,
    proposal: ControlProposal,
    *,
    historical: bool,
    memory_generation: int,
    belief: BeliefStore,
    static_history: bool = False,
) -> GuardReport:
    """Authorize one evidence-bound combined normal control prefix."""
    if type(snapshot) is not MemorySnapshot or type(view) is not PlaygroundView:
        raise ContractViolation("combined guard requires formal memory and playground view")
    if type(proposal) is not ControlProposal or type(belief) is not BeliefStore:
        raise ContractViolation("combined guard requires ControlProposal and BeliefStore")
    require_nonnegative_int(now_ns, "combined guard time")
    require_nonnegative_int(memory_generation, "combined guard memory generation")
    if type(historical) is not bool or type(static_history) is not bool:
        raise ContractViolation("historical admission must be boolean")

    bound = _bound(snapshot, view, now_ns)
    if bound is not None:
        return _early(bound)
    latest = snapshot.latest
    own = view.base.own
    assert latest is not None and own is not None
    if (
        proposal.scope_id != snapshot.scope_id
        or proposal.memory_generation != memory_generation
        or proposal.based_on != latest.stamp
        or proposal.origin != own.position
        or proposal.velocity != own.velocity
        or proposal.yaw != own.yaw
        or proposal.pitch != own.pitch
        or belief.scope_id != snapshot.scope_id
        or belief.evidence_scope != latest.stamp.scope
    ):
        return _early("control_unavailable")
    movement = proposal.movement
    if movement.jump or movement.sneak or movement.sprint:
        return _early("unsupported_motion")

    floor_float = own.position.y - 1
    floor = round(floor_float)
    if abs(floor_float - floor) > .01 or abs(proposal.endpoint.y-own.position.y) > .1:
        return _early("unsupported_motion")
    delta_x = proposal.endpoint.x-own.position.x
    delta_z = proposal.endpoint.z-own.position.z
    distance = math.hypot(delta_x, delta_z)
    speed = math.hypot(own.velocity.x, own.velocity.z)
    if distance > .45 + 2*speed + 1e-9:
        return _early("control_unavailable")

    effective_yaw = proposal.yaw + proposal.look.yaw_delta_degrees
    direction_x, direction_z = world_direction(
        effective_yaw, movement.forward, movement.strafe
    )
    if direction_x == 0. and direction_z == 0.:
        if distance > 1e-9:
            return _early("control_unavailable")
    elif (
        distance <= 1e-9
        or delta_x*direction_x + delta_z*direction_z <= 0.
        or abs(delta_x*direction_z-delta_z*direction_x) > 1e-7
    ):
        return _early("control_unavailable")

    origin = (own.position.x, own.position.z)
    travel_distance = 0. if direction_x == direction_z == 0. else .45 + 2*speed
    endpoint = (
        origin[0] + direction_x*travel_distance,
        origin[1] + direction_z*travel_distance,
    )
    drift_x, drift_z = 2*own.velocity.x, 2*own.velocity.z
    sweep = _convex_hull((
        origin,
        endpoint,
        (origin[0]+drift_x, origin[1]+drift_z),
        (endpoint[0]+drift_x, endpoint[1]+drift_z),
    ))
    steps = max(1, math.ceil(
        (travel_distance + math.hypot(drift_x, drift_z)) / SAMPLE_STEP
    ))
    return _inspect_sweep(
        snapshot,
        view,
        now_ns,
        sweep,
        floor,
        steps,
        margin=COMBINED_BODY_MARGIN,
        historical=historical,
        belief=belief,
        static_history=static_history,
    )

"""Bounded background screening of an ordinary Walk terminal approach.

The trials are calculation data. They do not enter observations, memory, the
input ledger or movement permissions; online control remains closed loop.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
import math
import time

from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint, FixedRouteController, FixedRouteConfig, FixedRouteState, terminal_route_config
from mc2p.motion_nav.geometry import QueryStatus
from mc2p.motion_nav.online_motion import project_movement_command, ProjectionStatus
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.support_surfaces import query_standable_connection, StandablePointResult
from mc2p.motion_nav.world_model import Aabb, ObservationStamp, WorldQueryCache
from mc2p.motion_nav.segment_entry import SegmentEntryWindow, physics_fits_segment_entry
from mc2p.motion_nav.movement_transition import MovementMode
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, CalculationStatus

# One controller/geometry call is not preemptible. The measured final-call
# overrun was 2.6 ms at P99; keep its exit and result assembly inside the shared
# deadline rather than granting every candidate its own extra time.
_FINISH_RESERVE_NS = 5_000_000


class TerminalApproachStatus(StrEnum):
    FEASIBLE = 'feasible'
    BLOCKED = 'blocked'
    NEEDS_INFORMATION = 'needs_information'
    UNSUPPORTED = 'unsupported'
    BUDGET_EXHAUSTED = 'budget_exhausted'


class TerminalApproachReason(StrEnum):
    SUPPORTED = 'terminal_approach_supported'
    STOP_RANGE_TOO_SMALL = 'terminal_approach_stop_range_too_small'
    GEOMETRY_BLOCKED = 'terminal_approach_geometry_blocked'
    NEEDS_WORLD = 'terminal_approach_needs_information'
    ENTRY_UNSUPPORTED = 'terminal_approach_entry_unsupported'
    CONTROL_UNSUPPORTED = 'terminal_approach_control_unsupported'
    BUDGET_EXHAUSTED = 'terminal_approach_budget_exhausted'


@dataclass(frozen=True, slots=True)
class TerminalApproachResult:
    status: TerminalApproachStatus
    reason: TerminalApproachReason
    position: tuple | None = None
    stop_region: Aabb | None = None
    entry_window: SegmentEntryWindow | None = None
    dependencies: tuple = ()
    missing_cells: tuple = ()
    profile_id: str | None = None
    connection: FixedRoute | None = None
    elapsed_ns: int = 0
    rollout_ticks: int = 0
    candidate_count: int = 0
    # These finite reference trials screen the current policy; they are not a
    # universal reachability proof for every continuous state in an envelope.
    reference_speeds: tuple = ()
    ruleset_id: str = JAVA_1_21_RULESET.ruleset_id
    conventional_target: StandablePointResult | None = None


def terminal_points(surface, goal, config=FixedRouteConfig()):
    region = goal.region
    x,z = ((region.min_x+region.max_x)/2,(region.min_z+region.max_z)/2)
    low_x, high_x = max(region.min_x,surface.region.min_x), min(region.max_x,surface.region.max_x)
    low_z, high_z = max(region.min_z,surface.region.min_z), min(region.max_z,surface.region.max_z)
    if low_x >= high_x or low_z >= high_z:
        return ()
    center = ((low_x+high_x)/2,(low_z+high_z)/2)
    primary = (min(max(x,low_x),high_x),min(max(z,low_z),high_z))
    dx,dz = (high_x-low_x)/4,(high_z-low_z)/4
    values = (primary,center,(center[0]-dx,center[1]),(center[0]+dx,center[1]),
              (center[0],center[1]-dz),(center[0],center[1]+dz))
    reserve = config.motion_prediction_margin_blocks + config.stopped_speed_blocks_per_second*.05
    return tuple((px,surface.position[1],pz) for px,pz in dict.fromkeys(values)
                 if min(px-region.min_x,region.max_x-px,pz-region.min_z,region.max_z-pz) > reserve)


def _calculation_frame(world, state, tick):
    stamp = ObservationStamp(world.session,tick,tick,'terminal-calculation',tick*50_000_000)
    body = BodyState(world.session,tick,stamp,state.position,
                     tuple(v*20 for v in state.velocity_blocks_per_tick),state.yaw_radians,
                     state.pitch_radians,state.pose,state.body_box,state.on_ground,
                     state.horizontal_collision,state.vertical_collision,
                     food_points=state.food_points, saturation_points=state.saturation_points,
                     movement_tick_id=tick)
    return NavigationFrame(world.session,body,world,'calculation-only')


def query_terminal_approach(world, surface, goal, connection, entry, profile,
                            config=FixedRouteConfig(), *, deadline_ns, template, trial_route=None,
                            mode_profile=None):
    started = time.perf_counter_ns()
    work_deadline_ns = deadline_ns - _FINISH_RESERVE_NS
    dependencies, missing = set(surface.dependencies), set()
    query_cache = WorldQueryCache(world)
    ticks = 0
    def result(status, reason, position=None, stop_region=None, speeds=()):
        dependencies.update(query_cache.touched_cells)
        if time.perf_counter_ns() > deadline_ns and status is not TerminalApproachStatus.BUDGET_EXHAUSTED:
            status,reason,position,stop_region = TerminalApproachStatus.BUDGET_EXHAUSTED,TerminalApproachReason.BUDGET_EXHAUSTED,None,None
        return TerminalApproachResult(status,reason,position,stop_region,entry,
            tuple(sorted(dependencies)),tuple(sorted(missing)),profile.profile_id,
            connection if position is not None else None,time.perf_counter_ns()-started,ticks,
            reference_speeds=speeds)
    if time.perf_counter_ns() >= work_deadline_ns:
        return result(TerminalApproachStatus.BUDGET_EXHAUSTED,TerminalApproachReason.BUDGET_EXHAUSTED)
    a,b = connection.points[0],connection.points[-1]
    dx,dz = b.x-a.x,b.z-a.z
    length = math.hypot(dx,dz)
    if length > 2.0+1.e-8 or any(abs(p.y-b.y)>1.e-8 for p in connection.points):
        return result(TerminalApproachStatus.UNSUPPORTED,TerminalApproachReason.ENTRY_UNSUPPORTED)
    geometry = query_standable_connection(world,surface,(b.x,b.y,b.z),(a.x,a.y,a.z))
    dependencies.update(geometry.dependencies); missing.update(geometry.missing_cells)
    if geometry.status is not QueryStatus.FEASIBLE:
        return result(TerminalApproachStatus.NEEDS_INFORMATION if missing else
                      TerminalApproachStatus.UNSUPPORTED if geometry.status is QueryStatus.UNSUPPORTED else TerminalApproachStatus.BLOCKED,
                      TerminalApproachReason.NEEDS_WORLD if missing else TerminalApproachReason.GEOMETRY_BLOCKED)
    if template is None or template.pose != 'standing' or template.status_effects or template.is_using_item:
        return result(TerminalApproachStatus.UNSUPPORTED,TerminalApproachReason.ENTRY_UNSUPPORTED)
    if entry is not None and (entry.profile_id != profile.profile_id
            or not physics_fits_segment_entry(entry, template, MovementMode.WALK)):
        return result(TerminalApproachStatus.UNSUPPORTED,TerminalApproachReason.ENTRY_UNSUPPORTED)
    configured = terminal_route_config(config,profile,goal,b)
    radius = configured.endpoint_tolerance_blocks
    if radius <= config.motion_prediction_margin_blocks:
        return result(TerminalApproachStatus.UNSUPPORTED,TerminalApproachReason.STOP_RANGE_TOO_SMALL)
    stop = Aabb(b.x-radius,max(goal.region.min_y,b.y-.01),b.z-radius,
                b.x+radius,min(goal.region.max_y,b.y+.01),b.z+radius)
    if trial_route is None and math.dist(template.position,(a.x,a.y,a.z)) > 1.e-8:
        return result(TerminalApproachStatus.UNSUPPORTED,TerminalApproachReason.ENTRY_UNSUPPORTED)
    speed = math.hypot(template.velocity_blocks_per_tick[0],template.velocity_blocks_per_tick[2])*20
    direction = ((template.velocity_blocks_per_tick[0]*20/speed,template.velocity_blocks_per_tick[2]*20/speed)
                 if speed>.1 else (dx/length,dz/length) if length>1.e-8 else (0.,1.))
    speeds = (speed,)
    if entry is None:
        entry = SegmentEntryWindow(template.position,direction,0.,0.,0.,
            template.position[1],template.position[1],
            min(speeds),max(speeds),0.,frozenset({'standing'}),frozenset({MovementMode.WALK}),
            template.yaw_radians,0.,profile.profile_id)
    for speed in speeds:
        state = template
        controller = FixedRouteController(profile,configured,mode_profile=mode_profile)
        controller.start(trial_route or connection,_calculation_frame(world,state,0))
        for tick in range(1,65):
            if time.perf_counter_ns() >= work_deadline_ns:
                return result(TerminalApproachStatus.BUDGET_EXHAUSTED,TerminalApproachReason.BUDGET_EXHAUSTED)
            frame = _calculation_frame(world,state,tick)
            choice = controller.decide(frame, query_cache=query_cache)
            ticks += 1
            if choice.state is FixedRouteState.SUCCEEDED:
                break
            if choice.state not in {FixedRouteState.RUNNING,FixedRouteState.BRAKING}:
                missing.update(choice.missing_cells)
                return result(TerminalApproachStatus.NEEDS_INFORMATION if missing else TerminalApproachStatus.UNSUPPORTED,
                              TerminalApproachReason.NEEDS_WORLD if missing else TerminalApproachReason.CONTROL_UNSUPPORTED)
            projected = project_movement_command(state,choice.movement,movement_yaw_radians=state.yaw_radians)
            if projected.status is not ProjectionStatus.READY or projected.tick_input is None:
                return result(TerminalApproachStatus.UNSUPPORTED,TerminalApproachReason.ENTRY_UNSUPPORTED)
            calculated = step(state,projected.tick_input,PhysicsWorldView(world,JAVA_1_21_RULESET),JAVA_1_21_RULESET)
            dependencies.update(calculated.dependencies)
            missing.update(calculated.missing_cells)
            if calculated.status is not CalculationStatus.OK or calculated.next_state is None:
                return result(TerminalApproachStatus.NEEDS_INFORMATION if missing else TerminalApproachStatus.UNSUPPORTED,
                              TerminalApproachReason.NEEDS_WORLD if missing else TerminalApproachReason.CONTROL_UNSUPPORTED)
            state = calculated.next_state
        else:
            return result(TerminalApproachStatus.BUDGET_EXHAUSTED,TerminalApproachReason.BUDGET_EXHAUSTED)
    return result(TerminalApproachStatus.FEASIBLE,TerminalApproachReason.SUPPORTED,(b.x,b.y,b.z),stop,speeds)


def select_terminal_approach(world, surface, goal, preceding, profile, template, *, deadline_ns, approach_points=None,
                             mode_profile=None):
    """One target shares one deadline and at most six internal positions."""
    started = time.perf_counter_ns()
    if started >= deadline_ns:
        return TerminalApproachResult(TerminalApproachStatus.BUDGET_EXHAUSTED,
                                      TerminalApproachReason.BUDGET_EXHAUSTED)
    points = terminal_points(surface,goal)
    if not points:
        return TerminalApproachResult(TerminalApproachStatus.UNSUPPORTED,
                                      TerminalApproachReason.STOP_RANGE_TOO_SMALL)
    dependencies, missing, ticks = set(), set(), 0
    last = None
    for count, point in enumerate(points,1):
        conventional = query_standable_connection(world,surface,point,surface.position)
        if conventional.status is not QueryStatus.FEASIBLE:
            conventional = None
        for source in dict.fromkeys((preceding,surface.position)):
            connection = FixedRoute('terminal-screen', (RoutePoint(*source),RoutePoint(*point)))
            trial_route = None
            if approach_points is not None:
                prefix = approach_points[:-1] if source == preceding else approach_points
                trial_route = FixedRoute('terminal-actual-entry', tuple(RoutePoint(*p) for p in (*prefix,point)))
            last = query_terminal_approach(world,surface,goal,connection,None,profile,
                                          deadline_ns=deadline_ns,template=template,
                                          trial_route=trial_route,mode_profile=mode_profile)
            dependencies.update(last.dependencies); missing.update(last.missing_cells)
            ticks += last.rollout_ticks
            if last.status in {TerminalApproachStatus.FEASIBLE,TerminalApproachStatus.BUDGET_EXHAUSTED}:
                return replace(last,dependencies=tuple(sorted(dependencies)),
                    missing_cells=tuple(sorted(missing)), elapsed_ns=time.perf_counter_ns()-started,
                    rollout_ticks=ticks,candidate_count=count,
                    conventional_target=conventional if last.status is TerminalApproachStatus.BUDGET_EXHAUSTED else None)
    return replace(last, status=TerminalApproachStatus.NEEDS_INFORMATION if missing else last.status,
                   reason=TerminalApproachReason.NEEDS_WORLD if missing else last.reason,
                   dependencies=tuple(sorted(dependencies)),missing_cells=tuple(sorted(missing)),
                   elapsed_ns=time.perf_counter_ns()-started,rollout_ticks=ticks,candidate_count=len(points))

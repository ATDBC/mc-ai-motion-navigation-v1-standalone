"""Height changes in the current ground chain: stop-and-go versus plain walking.

Each scenario is a 7-wide straight lane (so no detour helps), fully known.
"current": the repository planner (build_surface_graph + astar_surface_plan),
RouteAdmitter and ActionRouteExecutor, closed-loop with physics_1_21.step.
"hold-forward": the same calculator driven by a trivial policy -- hold W toward
the goal, press jump only in front of a full one-block rise, release to brake at
the end.  Vanilla step height (0.6) and falling do the rest.  It is a lower
bound on what continuous execution could achieve, not a proposed controller.

Run from the repository root of a 4b9e73e checkout:
    PYTHONPATH=. python -B <this script>

Round-16 copy: known air widened to y=79 so taller lanes fit; the helpers
(build_world, initial_state, top, run_hold_forward) are what the round-16
scripts import.  main() still drives the old graph API, not the 59c45bb proof
and background-coordination chain, so its output is not a round-16 result.
"""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.air_motion import load_air_motion_profiles
from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.environment_identity import load_frozen_environment
from mc2p.motion_nav.ground_motion import load_ground_motion_profile
from mc2p.motion_nav.jump_up import load_jump_up_profile
from mc2p.motion_nav.known_map_planner import (
    KnownMapBounds, KnownMapSnapshotBuilder, SnapshotBuildStatus, SurfacePlanningRequest,
    SurfacePlanningStatus,
)
from mc2p.motion_nav.physics_1_21 import step as physics_step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import CalculationStatus, JAVA_1_21_RULESET, TickInput
from mc2p.motion_nav.planning_reference import astar_surface_plan, build_surface_graph
from mc2p.motion_nav.route_admission import AdmissionStatus, RouteAdmitter
from mc2p.motion_nav.runtime_adapter import BodyState, NavigationFrame
from mc2p.motion_nav.step_transition import load_step_profile
from mc2p.motion_nav.support_surfaces import query_support_surfaces
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge, WorldSessionId,
)

CONFIG = Path("config/motion-navigation")
SESSION = WorldSessionId("height-transitions")
GRASS = BlockGeometry.full_cube("minecraft:grass_block")
SLAB = BlockGeometry("minecraft:smooth_stone_slab", "boxes", (Aabb(0, 0, 0, 1, .5, 1),))
PATH = BlockGeometry("minecraft:dirt_path", "boxes", (Aabb(0, 0, 0, 1, .9375, 1),))

# Column profiles along z: list of (block_y, geometry) stacked on a y=62 base.
SCENARIOS = {
    "two half-slab steps up (B07 shape)":   ([63], [63], [63, (64, SLAB)], [63, 64], [63, 64], [63, 64]),
    "two half-slab steps down":             ([63, 64], [63, 64], [63, 64], [63, (64, SLAB)], [63], [63]),
    "grass / dirt-path lane (1/16 steps)":  tuple([63] if z % 2 == 0 else [(63, PATH)] for z in range(9)),
    "one full block up":                    ([63], [63], [63], [63, 64], [63, 64], [63, 64]),
    "one full block down":                  ([63, 64], [63, 64], [63, 64], [63], [63], [63]),
}


def build_world(columns):
    world = WorldKnowledge(SESSION)
    stamp = ObservationStamp(SESSION, 0, 0, "sim", 0)
    blocks, air = {}, set()
    for z in range(-3, len(columns) + 3):
        column = columns[min(max(z, 0), len(columns) - 1)]
        solid = {}
        for item in column:
            y, geometry = item if isinstance(item, tuple) else (item, GRASS)
            solid[y] = geometry
        for x in range(-4, 5):
            blocks[(x, 62, z)] = GRASS
            for y in range(63, 80):
                if y in solid:
                    blocks[(x, y, z)] = solid[y]
                else:
                    air.add((x, y, z))
    world.observe_blocks(stamp, blocks)
    world.confirm_air(stamp, tuple(sorted(air)))
    return world


def top(columns, z):
    column = columns[z]
    best = 0.0
    for item in column:
        y, geometry = item if isinstance(item, tuple) else (item, GRASS)
        best = max(best, y + max(box.max_y for box in geometry.boxes) if geometry.boxes else y + 1.0)
    return best


def frame_from(state, world, sequence):
    stamp = ObservationStamp(SESSION, sequence, sequence, "sim", sequence * 50_000_000)
    x, y, z = state.position
    body = BodyState(SESSION, sequence, stamp, state.position,
                     tuple(v * 20.0 for v in state.velocity_blocks_per_tick), state.yaw_radians,
                     state.pitch_radians, state.pose, Aabb(x - .3, y, z - .3, x + .3, y + 1.8, z + .3),
                     state.on_ground, state.horizontal_collision, state.vertical_collision,
                     state.sprinting, state.sneaking)
    return NavigationFrame(SESSION, body, world.view(), "fabric")


def initial_state(world, position):
    seed = BodyState(SESSION, 1, ObservationStamp(SESSION, 1, 1, "sim", 0), position,
                     (0.0, -1.568, 0.0), 0.0, 0.0, "standing",
                     Aabb(position[0] - .3, position[1], position[2] - .3,
                          position[0] + .3, position[1] + 1.8, position[2] + .3), True, False, True)
    frame = NavigationFrame(SESSION, seed, world.view(), "fabric")
    return frame, build_physics_state(frame, JAVA_1_21_RULESET, dict(
        jumping_cooldown_ticks=0, movement_speed_attribute=0.1, step_height_blocks=0.6,
        gravity_attribute=0.08, jump_strength_attribute=0.42)).require_state()


def stats(speeds):
    moving = [s for s in speeds]
    # interior stops: after first exceeding 1.5 b/s, count drops below 0.3 b/s before the final brake
    stops, above = 0, False
    for s in moving[:-6]:
        if s > 1.5:
            above = True
        elif above and s < 0.3:
            stops += 1
            above = False
    return stops


def run_current(columns, profiles, delay=0):
    ground, step_profile, jump, air = profiles
    world = build_world(columns)
    start = (.5, top(columns, 0), .5)
    goal = (.5, top(columns, len(columns) - 1), len(columns) - .5)
    frame, state = initial_state(world, start)

    def surface_at(position):
        result = query_support_surfaces(frame.world, math.floor(position[0]), math.floor(position[2]),
                                        position[1] - .1, position[1] + .1)
        return min(result.surfaces, key=lambda s: abs(s.position[1] - position[1]))

    bounds = KnownMapBounds(-3, 3, 62, 67, -1, len(columns), True)
    snapshot = KnownMapSnapshotBuilder(frame.world, bounds).advance(frame.world, 100_000)
    assert snapshot.status is SnapshotBuildStatus.COMPLETE
    graph = build_surface_graph(snapshot.snapshot.world, snapshot.snapshot.bounds, ground,
                                step_profile, jump, air_profiles=air)
    request = SurfacePlanningRequest(1, "r", "g", 1, SESSION.value, surface_at(start).node_id,
                                     surface_at(goal).node_id)
    candidate = astar_surface_plan(graph, request)
    if candidate.status is not SurfacePlanningStatus.COMPLETE:
        return dict(result=candidate.status.value)
    admission = RouteAdmitter().admit_surface(candidate, frame, expected_request_id="r",
                                              goal_id="g", goal_revision=1, changed_cells=())
    if admission.status is not AdmissionStatus.ACCEPTED:
        return dict(result="admission:" + admission.reason)
    route = admission.route.action_route
    kinds = [type(a).__name__.replace("Segment", "") for a in route.actions]
    executor = ActionRouteExecutor(ground, jump, step_profile, air_profiles=air)
    executor.start(route, frame)
    physics_world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)
    pending = [MovementV1()] * delay
    speeds = []
    for tick in range(400):
        decision = executor.decide(frame, input_confirmed=True)
        if decision.state is ActionRouteState.COMPLETE:
            return dict(result="complete", ticks=tick, actions=kinds, stops=stats(speeds),
                        planned_cost_s=round(candidate.total_cost_seconds, 2))
        if decision.state is not ActionRouteState.RUNNING:
            p = frame.body.position
            return dict(result=f"{decision.state.value}/{decision.reason_code}", ticks=tick, actions=kinds,
                        action_index=decision.action_index,
                        body=(round(p[0], 3), round(p[1], 3), round(p[2], 3)))
        yaw = state.yaw_radians + (math.radians(decision.look.yaw_delta_degrees) if decision.look else 0.0)
        state = replace(state, yaw_radians=yaw)
        pending.append(decision.movement)
        m = pending.pop(0)
        result = physics_step(state, TickInput(float(m.forward), float(m.strafe), m.jump, m.sneak, m.sprint, yaw),
                              physics_world, JAVA_1_21_RULESET)
        assert result.status is CalculationStatus.OK, result
        state = replace(result.next_state, movement_tick_id=0)
        frame = frame_from(state, world, tick + 2)
        speeds.append(math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]) * 20)
    return dict(result="timeout", actions=kinds)


def run_hold_forward(columns):
    world = build_world(columns)
    start = (.5, top(columns, 0), .5)
    goal_z = len(columns) - .5
    _, state = initial_state(world, start)
    physics_world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)
    speeds = []
    for tick in range(400):
        x, y, z = state.position
        v = state.velocity_blocks_per_tick[2]
        remaining = goal_z - z
        braking = v * .546 / (1 - .546)          # distance covered after releasing W on ground
        if abs(remaining) < .15 and abs(v) * 20 < .5 and state.on_ground:
            return dict(result="complete", ticks=tick, stops=stats(speeds))
        if remaining > braking + .05:
            forward = 1.0
        elif remaining < -.1 and v * 20 < 1.0:
            forward = -1.0                          # walked past the goal: come back
        else:
            forward = 0.0
        ahead = math.floor(z + .3 + .45)          # column the front of the body is about to enter
        here = math.floor(z)
        jump = False
        if 0 <= ahead < len(columns) and state.on_ground:
            jump = top(columns, ahead) - top(columns, min(max(here, 0), len(columns) - 1)) > .6
        result = physics_step(state, TickInput(forward, 0.0, jump, False, False, 0.0),
                              physics_world, JAVA_1_21_RULESET)
        assert result.status is CalculationStatus.OK, result
        state = replace(result.next_state, movement_tick_id=0)
        speeds.append(math.hypot(state.velocity_blocks_per_tick[0], state.velocity_blocks_per_tick[2]) * 20)
    return dict(result="timeout")


def main():
    environment = load_frozen_environment(CONFIG / "environment-v1.json")
    catalog = BlockMotionCatalog.load(CONFIG / "block-motion-traits-v1.json",
                                      CONFIG / "vanilla-block-registry-1_21.json")
    profiles = (
        load_ground_motion_profile(CONFIG / "ordinary-ground-b07-v1.json", environment=environment, catalog=catalog),
        load_step_profile(CONFIG / "step-b07-v1.json", environment=environment),
        load_jump_up_profile(CONFIG / "jump-up-b06-v1.json", environment=environment, catalog=catalog),
        load_air_motion_profiles(CONFIG / "air-motions-b09-v1.json", environment=environment, catalog=catalog),
    )
    for name, columns in SCENARIOS.items():
        current = run_current(columns, profiles)
        delayed = run_current(columns, profiles, delay=1)
        baseline = run_hold_forward(columns)
        print(f"{name} ({len(columns) - 1} blocks)")
        print(f"  current   : {current}")
        print(f"  +1 tick   : {delayed.get('result')} {delayed.get('ticks', '')}")
        print(f"  hold W    : {baseline}")


if __name__ == "__main__":
    main()

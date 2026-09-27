"""Round-15 review: B07 Walk-Step-Step-Walk continuity in a closed loop.

Uses the repository's own planner, admission, ActionRouteExecutor and the 1.21
movement calculator (physics_1_21.step) as the "game".  The world is fully
known, so perception cannot be the cause of anything this reproduces.

Run from the repository root of a 4b9e73e checkout:
    PYTHONPATH=. python -B <this script>            # sweep: delay 0/1/2, heading, start offset, dropped leases
    PYTHONPATH=. python -B <this script> jitter     # 1-tick delay, 20% of leases one tick late, 300 seeds x 3 headings
    PYTHONPATH=. python -B <this script> 1 0.0 0.0 0  # one verbose run: delay yaw start_dz drop_every
"""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
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

ROOT = Path(".").resolve()
SESSION = WorldSessionId("b07-closed-loop")
X, FEET, Z = 0, 64, 0                 # same layout as scripts/step_transition_runtime.py


def world() -> WorldKnowledge:
    knowledge = WorldKnowledge(SESSION)
    stamp = ObservationStamp(SESSION, 0, 0, "sim", 0)
    grass = BlockGeometry.full_cube("minecraft:grass_block")
    slab = BlockGeometry("minecraft:smooth_stone_slab", "boxes", (Aabb(0, 0, 0, 1, .5, 1),))
    volume = {(x, y, z) for x in range(X - 1, X + 2) for z in range(Z - 1, Z + 4)
              for y in range(FEET - 3, FEET + 4)}
    blocks = {(X, FEET - 1, Z - 1): grass, (X, FEET - 1, Z): grass, (X, FEET, Z + 1): slab,
              (X, FEET, Z + 2): grass, (X, FEET, Z + 3): grass}
    air = set(volume) - set(blocks)
    for x in range(X - 8, X + 9):                # untouched flat terrain around the fixture
        for z in range(Z - 8, Z + 12):
            if (x, FEET - 1, z) not in volume:
                blocks[(x, FEET - 1, z)] = grass
            for y in range(FEET, FEET + 4):
                if (x, y, z) not in volume:
                    air.add((x, y, z))
            for y in range(FEET - 3, FEET - 1):
                if (x, y, z) not in volume:
                    blocks[(x, y, z)] = grass
    knowledge.observe_blocks(stamp, blocks)
    knowledge.confirm_air(stamp, tuple(sorted(air)))
    return knowledge


def frame_from(state, knowledge, sequence) -> NavigationFrame:
    stamp = ObservationStamp(SESSION, sequence, sequence, "sim", sequence * 50_000_000)
    x, y, z = state.position
    box = Aabb(x - .3, y, z - .3, x + .3, y + state.body_height, z + .3)
    body = BodyState(
        SESSION, sequence, stamp, state.position,
        tuple(v * 20.0 for v in state.velocity_blocks_per_tick), state.yaw_radians,
        state.pitch_radians, state.pose, box, state.on_ground, state.horizontal_collision,
        state.vertical_collision, state.sprinting, state.sneaking,
    )
    return NavigationFrame(SESSION, body, knowledge.view(), "fabric")


def main(delay: int = 0, yaw_offset_deg: float = 0.0, start_dz: float = 0.0,
         drop_every: int = 0, verbose: bool = True, jitter_seed: int | None = None,
         late_probability: float = 0.0) -> tuple[str, int, str]:
    environment = load_frozen_environment(ROOT / "config/motion-navigation/environment-v1.json")
    catalog = BlockMotionCatalog.load(
        ROOT / "config/motion-navigation/block-motion-traits-v1.json",
        ROOT / "config/motion-navigation/vanilla-block-registry-1_21.json")
    step_profile = load_step_profile(ROOT / "config/motion-navigation/step-b07-v1.json",
                                     environment=environment)
    ground = load_ground_motion_profile(ROOT / "config/motion-navigation/ordinary-ground-b07-v1.json",
                                        environment=environment, catalog=catalog)
    jump = load_jump_up_profile(ROOT / "config/motion-navigation/jump-up-b06-v1.json",
                                environment=environment, catalog=catalog)
    knowledge = world()
    route_start = (X + .5, float(FEET), Z - .5)
    start_position = (X + .5, float(FEET), Z - .5 + start_dz)

    def surface_at(frame, position):
        result = query_support_surfaces(frame.world, math.floor(position[0]), math.floor(position[2]),
                                        position[1] - .1, position[1] + .1)
        return min(result.surfaces, key=lambda s: abs(s.position[1] - position[1]))

    # Build the initial physics state from a standing frame.
    seed = BodyState(SESSION, 1, ObservationStamp(SESSION, 1, 1, "sim", 0), start_position,
                     (0.0, -1.568, 0.0), math.radians(yaw_offset_deg), 0.0, "standing",
                     Aabb(start_position[0] - .3, FEET, start_position[2] - .3,
                          start_position[0] + .3, FEET + 1.8, start_position[2] + .3),
                     True, False, True)
    frame = NavigationFrame(SESSION, seed, knowledge.view(), "fabric")
    state = build_physics_state(frame, JAVA_1_21_RULESET, dict(
        jumping_cooldown_ticks=0, movement_speed_attribute=0.1, step_height_blocks=0.6,
        gravity_attribute=0.08, jump_strength_attribute=0.42)).require_state()

    bounds = KnownMapBounds(X, X, FEET, FEET + 1, Z - 1, Z + 3, True)
    snapshot = KnownMapSnapshotBuilder(frame.world, bounds).advance(frame.world, 10_000)
    assert snapshot.status is SnapshotBuildStatus.COMPLETE
    graph = build_surface_graph(snapshot.snapshot.world, snapshot.snapshot.bounds, ground, step_profile)
    request = SurfacePlanningRequest(1, "sim-req", "sim-goal", 1, SESSION.value,
                                     surface_at(frame, route_start).node_id,
                                     surface_at(frame, (X + .5, float(FEET + 1), Z + 3.5)).node_id)
    candidate = astar_surface_plan(graph, request)
    assert candidate.status is SurfacePlanningStatus.COMPLETE, candidate.status
    admission = RouteAdmitter().admit_surface(candidate, frame, expected_request_id="sim-req",
                                              goal_id="sim-goal", goal_revision=1, changed_cells=())
    assert admission.status is AdmissionStatus.ACCEPTED, admission.reason
    route = admission.route.action_route
    if verbose: print("actions:", [type(a).__name__ for a in route.actions])
    for index, action in enumerate(route.actions):
        points = getattr(getattr(action, "fixed_route", None), "points", None)
        if points:
            if verbose: print(f"  walk {index}: " + " -> ".join(f"({p.x:.2f},{p.y:.2f},{p.z:.2f})" for p in points))

    executor = ActionRouteExecutor(ground, jump, step_profile)
    executor.start(route, frame)
    physics_world = PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET)
    sequence = 1
    last = None
    pending = [MovementV1()] * delay
    import random
    rng = random.Random(jitter_seed)
    arrivals: dict[int, list] = {}
    if verbose: print(f"--- input delay {delay} tick(s), yaw {yaw_offset_deg:+.1f}, start dz {start_dz:+.2f}, drop every {drop_every}")
    for tick in range(160):
        decision = executor.decide(frame, input_confirmed=True)
        row = (decision.action_index, decision.state.value, decision.reason_code)
        if row != last and verbose:
            p = frame.body.position
            v = frame.body.velocity_blocks_per_second
            print(f"tick {tick:3d} action {row[0]} {row[1]:<12} {row[2]:<34} "
                  f"pos=({p[0]:.3f},{p[1]:.3f},{p[2]:.3f}) vz={v[2]:+.2f} ground={frame.body.is_on_ground}")
        last = row
        if decision.state is not ActionRouteState.RUNNING:
            return decision.state.value, decision.action_index, decision.reason_code
        yaw = state.yaw_radians + (math.radians(decision.look.yaw_delta_degrees) if decision.look else 0.0)
        state = replace(state, yaw_radians=yaw)
        if jitter_seed is None:
            pending.append(decision.movement)
            m = pending.pop(0)
        else:
            # Each 1-tick lease arrives after `delay` ticks, or one tick later with
            # probability late_probability.  A tick with no arrival applies neutral
            # input; when two arrive together the newer one wins.
            late = rng.random() < late_probability
            arrivals.setdefault(tick + delay + (1 if late else 0), []).append(decision.movement)
            arrived = arrivals.pop(tick, [])
            m = arrived[-1] if arrived else MovementV1()
        if drop_every and tick % drop_every == drop_every - 1:
            m = MovementV1()          # a missed 1-tick lease: the client applies neutral input
        result = physics_step(state, TickInput(float(m.forward), float(m.strafe), m.jump, m.sneak,
                                               m.sprint, yaw), physics_world, JAVA_1_21_RULESET)
        assert result.status is CalculationStatus.OK, (result.status, result.unsupported_reasons,
                                                       result.missing_cells)
        state = replace(result.next_state, movement_tick_id=0)
        sequence += 1
        frame = frame_from(state, knowledge, sequence)
        if not state.on_ground and verbose:
            print(f"         tick {tick:3d}: body left the ground at "
                  f"({state.position[0]:.3f},{state.position[1]:.3f},{state.position[2]:.3f})"
                  f" vy={state.velocity_blocks_per_tick[1]:+.3f}, events={result.events}")


    return "timeout", -1, ""


if __name__ == "__main__":
    import collections
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "jitter":
        outcomes = collections.Counter()
        examples = {}
        for seed in range(300):
            for yaw in (-2.0, 0.0, 2.0):
                result = main(1, yaw, 0.0, 0, verbose=False, jitter_seed=seed, late_probability=0.2)
                outcomes[result] += 1
                examples.setdefault(result, (seed, yaw))
        for key, count in sorted(outcomes.items(), key=lambda item: -item[1]):
            print(f"{key[0]:<12} action {key[1]:>2} {key[2]:<34} x{count:<4} e.g. seed/yaw {examples[key]}")
        raise SystemExit
    if len(sys.argv) > 1:
        args = sys.argv[1:]
        seed = int(args[4]) if len(args) > 4 else None
        print(main(int(args[0]), float(args[1]), float(args[2]), int(args[3]), True, seed, 0.2))
        raise SystemExit
    if "--jitter" in sys.argv:
        pass
    outcomes = collections.Counter()
    examples = {}
    for delay in (0, 1, 2):
        for yaw in (-4.0, -2.0, 0.0, 2.0, 4.0):
            for dz in (-0.1, 0.0, 0.1):
                for drop in (0, 3, 5, 7):
                    result = main(delay, yaw, dz, drop, verbose=False)
                    key = (delay,) + result
                    outcomes[key] += 1
                    examples.setdefault(key, (delay, yaw, dz, drop))
    for key, count in sorted(outcomes.items()):
        print(f"delay {key[0]}: {key[1]:<12} action {key[2]:>2} {key[3]:<34} x{count:<3} e.g. args {examples[key]}")

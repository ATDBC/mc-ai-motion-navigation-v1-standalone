"""Round-16 review: how robust is the verified varying-height Walk?

Uses the upstream fixture (tests.motion_nav.test_ground_traversal.traversal_fixture:
flat grass, a bottom slab, then a full block -> two half-block rises), the real
verify_ground_traversal proof and FixedRouteController, closed-loop with
physics_1_21.step.  Only the conditions around the controller change:

  * input latency: every command late by 1 tick; or each command late by one
    extra tick with 20% probability (100 frozen seeds).  A late command leaves
    the previous keys held for that tick, as in the upstream late-release test;
  * held yaw offset: the body looks theta away from the route direction for the
    whole run (what the combat look does during pursuit; the Walk transition
    does not claim route look ownership);
  * entry speed differing from the state the proof was computed from.

Run from the repository root of a 59c45bb checkout:
    PYTHONPATH=. python -B <this script>
"""
from dataclasses import replace
import math
import random

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav.fixed_route import FixedRouteController, FixedRouteState
from mc2p.motion_nav.ground_traversal import verify_ground_traversal
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from tests.motion_nav.test_continuous_height_execution import frame_from_state
from tests.motion_nav.test_fixed_route_walk import profile
from tests.motion_nav.test_ground_traversal import traversal_fixture

TERMINAL_FAIL = {FixedRouteState.FAILED, FixedRouteState.UNSUPPORTED,
                 FixedRouteState.BLOCKED, FixedRouteState.INPUT_LOST}


def run(*, downhill=False, delay=0, late_probability=0.0, seed=0, yaw_offset_deg=0.0,
        proof_speed=0.0, actual_speed=0.0):
    state, route, world = traversal_fixture()
    yaw = 0.0
    if downhill:
        route = replace(route, route_id="down", points=tuple(reversed(route.points)))
        state = replace(state, position=(0.5, 2.0, 2.5))
        yaw = math.pi
    heading = yaw + math.radians(yaw_offset_deg)
    # Minecraft yaw 0 faces +z; velocity along the route direction
    direction = -1.0 if downhill else 1.0
    proof_state = replace(state, yaw_radians=heading,
                          velocity_blocks_per_tick=(0.0, -0.0784000015258789, direction * proof_speed / 20))
    proof = verify_ground_traversal(proof_state, route, world, profile(), maximum_ticks=120)
    if proof.plan is None:
        return f"no proof: {proof.status.value} {proof.reasons}"
    state = replace(proof_state, velocity_blocks_per_tick=(0.0, -0.0784000015258789, direction * actual_speed / 20))
    controller = FixedRouteController(profile())
    controller.start(route, frame_from_state(state, world, 0), traversal_plan=proof.plan)
    rng = random.Random(seed)
    arrivals = {}
    pending = [MovementV1()] * delay
    held = MovementV1()
    for tick in range(1, 120):
        decision = controller.decide(frame_from_state(state, world, tick))
        if decision.state is FixedRouteState.SUCCEEDED:
            return f"complete in {tick} ticks"
        if decision.state in TERMINAL_FAIL:
            return f"{decision.state.value}/{decision.reason} at tick {tick}"
        if late_probability:
            late = rng.random() < late_probability
            arrivals.setdefault(tick + (1 if late else 0), []).append(decision.movement)
            arrived = arrivals.pop(tick, [])
            # a late command leaves the previous keys held for one more tick
            applied = arrived[-1] if arrived else held
            held = applied
        else:
            pending.append(decision.movement)
            applied = pending.pop(0)
        projected = project_movement_command(state, applied)      # moves relative to the held yaw
        state = step(state, projected.tick_input, world, JAVA_1_21_RULESET).next_state
        state = replace(state, yaw_radians=heading)                  # combat look keeps the yaw
    return "timeout"


def proof_terminal_margin(downhill):
    state, route, world = traversal_fixture()
    if downhill:
        route = replace(route, route_id="down", points=tuple(reversed(route.points)))
        state = replace(state, position=(0.5, 2.0, 2.5), yaw_radians=math.pi)
    plan = verify_ground_traversal(state, route, world, profile(), maximum_ticks=120).plan
    goal, end = route.points[-1], plan.trajectory[-1].position
    distance = math.hypot(end[0] - goal.x, end[2] - goal.z)
    return f"proof ends {distance:.3f} from the goal (completion radius 0.35, margin {0.35 - distance:.3f})"


def main():
    for downhill in (False, True):
        label = "down two half-blocks" if downhill else "up two half-blocks"
        print(label)
        print("  nominal proof                 :", proof_terminal_margin(downhill))
        print("  nominal                       :", run(downhill=downhill))
        print("  every command 1 tick late     :", run(downhill=downhill, delay=1))
        results = [run(downhill=downhill, late_probability=0.2, seed=s) for s in range(100)]
        ok = sum(r.startswith("complete") for r in results)
        failures = sorted({r.split(" at ")[0] for r in results if not r.startswith("complete")})
        print(f"  20% commands 1 tick late      : {ok}/100 complete; failures: {failures}")
        for theta in (10, 20, 30, 45, 60, 90):
            print(f"  yaw held {theta:>2} deg off route    :", run(downhill=downhill, yaw_offset_deg=theta))
        for proof_speed, actual_speed in ((0.0, 1.0), (0.0, 2.0), (0.0, 3.0), (2.0, 0.0)):
            print(f"  proof from {proof_speed:.0f} b/s, start at {actual_speed:.0f} b/s:",
                  run(downhill=downhill, proof_speed=proof_speed, actual_speed=actual_speed))


if __name__ == "__main__":
    main()

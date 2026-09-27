"""Round-17 review: per-frame cost of the closed-loop traversal controller.

Times FixedRouteController.decide() on the upstream traversal_fixture (two
half-block rises / descents), closed-loop with physics_1_21.step, and counts the
1.21 calculator steps each decision runs.  Absolute times depend on the
machine; the step count does not.

Run from the repository root of a 5fa2f33 checkout:
    PYTHONPATH=. python -B <this script>
"""
from dataclasses import replace
import math
import statistics
import time

from mc2p.contracts.action_v1 import MovementV1
from mc2p.motion_nav import fixed_route as fixed_route_module
from mc2p.motion_nav.fixed_route import FixedRouteController, FixedRouteState
from mc2p.motion_nav.ground_traversal import verify_ground_traversal
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from tests.motion_nav.test_continuous_height_execution import frame_from_state
from tests.motion_nav.test_fixed_route_walk import profile
from tests.motion_nav.test_ground_traversal import traversal_fixture

calls = {"steps": 0}
original_step = fixed_route_module.physics_step


def counting_step(*args, **kwargs):
    calls["steps"] += 1
    return original_step(*args, **kwargs)


fixed_route_module.physics_step = counting_step


def run(kind, yaw_offset_deg=0.0, repeats=20):
    samples, steps_per_frame = [], []
    for _ in range(repeats):
        state, route, world = traversal_fixture()
        if kind == "down":
            route = replace(route, route_id="down", points=tuple(reversed(route.points)))
            state = replace(state, position=(0.5, 2.0, 2.5), yaw_radians=math.pi)
        heading = state.yaw_radians + math.radians(yaw_offset_deg)
        state = replace(state, yaw_radians=heading)
        plan = verify_ground_traversal(state, route, world, profile(), maximum_ticks=120).plan
        controller = FixedRouteController(profile())
        controller.start(route, frame_from_state(state, world, 0), traversal_plan=plan)
        for tick in range(1, 80):
            current = frame_from_state(state, world, tick)
            calls["steps"] = 0
            started = time.perf_counter()
            decision = controller.decide(current)
            samples.append((time.perf_counter() - started) * 1000.0)
            steps_per_frame.append(calls["steps"])
            if decision.state not in {FixedRouteState.RUNNING, FixedRouteState.BRAKING,
                                      FixedRouteState.CANCELLING}:
                break
            projected = project_movement_command(state, decision.movement)
            state = step(state, projected.tick_input, world, JAVA_1_21_RULESET).next_state
            state = replace(state, yaw_radians=heading)
    samples.sort()
    pick = lambda q: samples[min(len(samples) - 1, int(q * len(samples)))]  # noqa: E731
    return (f"{kind:<5} yaw+{yaw_offset_deg:>2.0f}: frames {len(samples):>4}  P50 {pick(.5):6.2f} ms  "
            f"P95 {pick(.95):6.2f} ms  max {samples[-1]:6.2f} ms  "
            f"calculator steps/frame median {statistics.median(steps_per_frame):.0f} max {max(steps_per_frame)}")


for kind in ("up", "down"):
    for offset in (0.0, 30.0):
        print(run(kind, offset))

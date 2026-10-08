"""Per-frame FixedRouteController.decide cost on the same routes, old vs new.

    python <this file>          (from a 1ee76b6 or 2bad9bf checkout root)

Closed loop with the 1.21 calculator backend: decide -> project -> physics step.
Routes: open straight lane, offset straight line, and a wall-tangent line.
Reports decide() wall time per frame (perf_counter_ns), outcome and frames.
Single process; compare the two commits on one machine, not absolute numbers.
"""
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

from tests.sim.backend import CalculatorBackend, Scene
from tests.sim.runner import lane
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, FixedRouteState, RoutePoint
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, CalculationStatus
from scripts.f2_ground_route_evidence import ROOT, _frame

PROFILES = NavigationSessionProfiles.load(ROOT / 'config/motion-navigation')
STONE = "minecraft:stone"


def scene_with_wall():
    base = lane([[63]] * 12, width=3)
    return replace(base, solids={**base.solids, **{(2, y, z): STONE for y in (64, 65) for z in range(2, 10)}})


ROUTES = {
    "open straight": (lane([[63]] * 12, width=3), (.5, 64., .5), (.5, 64., 9.5)),
    "offset straight": (lane([[63]] * 12, width=3), (.2, 64., .5), (.8, 64., 9.5)),
    "wall tangent": (scene_with_wall(), (1.5, 64., .5), (1.7, 64., 9.5)),
}

for name, (scene, start, goal) in ROUTES.items():
    samples, outcomes = [], []
    for repeat in range(5):
        backend = CalculatorBackend([0], scene, start, 0.)
        route = FixedRoute(f'hotpath-{repeat}', (RoutePoint(*start), RoutePoint(*goal)))
        controller = FixedRouteController(PROFILES.ground)
        state = backend.state
        controller.start(route, _frame(state, backend.world._world, 0))
        decision = None
        for tick in range(1, 200):
            frame = _frame(state, backend.world._world, tick)
            t0 = time.perf_counter_ns()
            decision = controller.decide(frame, physics_state=state)
            samples.append((time.perf_counter_ns() - t0) / 1e6)
            if decision.state not in {FixedRouteState.RUNNING, FixedRouteState.BRAKING}:
                break
            result = step(state, project_movement_command(state, decision.movement).tick_input,
                          backend.world, JAVA_1_21_RULESET)
            if result.status is not CalculationStatus.OK:
                break
            state = result.next_state
        outcomes.append((decision.state.value, tick))
    samples.sort()
    n = len(samples)
    print(f"{name:16s}: outcomes {sorted(set(outcomes))}; decide ms p50 {samples[n//2]:.3f} "
          f"p95 {samples[int(n*.95)]:.3f} max {samples[-1]:.3f} (n={n})")

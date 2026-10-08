"""Does the per-frame verifier budget (started + 30 ms, perf_counter) change behaviour?

    python <this file>          (from a 2bad9bf checkout root)

Runs the same closed-loop offset route twice per mode with identical inputs:
  normal  - real clock;
  slow    - the verifier's clock advances 31 ms per call (as on an overloaded
            or slower machine), so its deadline is reached on the first check.
Prints the decision sequence hash, finishing tick and reasons seen.
"""
import hashlib
import sys
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path.cwd()))

from tests.sim.backend import CalculatorBackend
from tests.sim.runner import lane
from mc2p.motion_nav import ground_candidate_verifier as gcv
from mc2p.motion_nav.fixed_route import FixedRoute, FixedRouteController, FixedRouteState, RoutePoint
from mc2p.motion_nav.navigation_session import NavigationSessionProfiles
from mc2p.motion_nav.online_motion import project_movement_command
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET
from scripts.f2_ground_route_evidence import ROOT, _frame

PROFILES = NavigationSessionProfiles.load(ROOT / 'config/motion-navigation')


def run():
    backend = CalculatorBackend([0], lane([[63]] * 12, width=3), (.2, 64., .5), 0.)
    route = FixedRoute('budget', (RoutePoint(.2, 64., .5), RoutePoint(.8, 64., 9.5)))
    controller = FixedRouteController(PROFILES.ground)
    state = backend.state
    controller.start(route, _frame(state, backend.world._world, 0))
    trail, reasons = [], set()
    for tick in range(1, 200):
        decision = controller.decide(_frame(state, backend.world._world, tick), physics_state=state)
        trail.append((decision.state.value, decision.movement.forward, decision.movement.strafe))
        reasons.add(decision.reason)
        if decision.state not in {FixedRouteState.RUNNING, FixedRouteState.BRAKING}:
            break
        state = step(state, project_movement_command(state, decision.movement).tick_input,
                     backend.world, JAVA_1_21_RULESET).next_state
    return hashlib.sha256(repr(trail).encode()).hexdigest()[:12], tick, decision.state.value, sorted(reasons)


class SlowClock:
    def __init__(self): self.now = time.perf_counter_ns()
    def __call__(self):
        self.now += 31_000_000
        return self.now


for mode in ("normal", "normal", "slow", "slow"):
    if mode == "slow":
        defaults = dict(gcv.GroundCandidateVerifier.__init__.__kwdefaults__)
        with patch.dict(gcv.GroundCandidateVerifier.__init__.__kwdefaults__, {"clock_ns": SlowClock()}):
            result = run()
    else:
        result = run()
    print(mode, result)

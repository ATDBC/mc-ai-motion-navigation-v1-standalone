# Run from the repository root of an eb4653c checkout: PYTHONPATH=. python -B <script>
"""Diagnosis only: cancellation during the 2-block drop followed by one late tick (reads private fields)."""
import sys
from dataclasses import replace
from mc2p.motion_nav.navigation_session import NavigationSession
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, run
from tests.sim.scenarios import SCENARIOS

at = int(sys.argv[1]) if len(sys.argv) > 1 else 38
original = NavigationSession.propose
seen = []


def propose(self, *args, **kwargs):
    result = original(self, *args, **kwargs)
    executor = self._executor
    controller = None if executor is None else getattr(executor, "_controller", None)
    key = (self._state.value, self._reason,
           None if executor is None else executor.state.value,
           None if controller is None else getattr(getattr(controller, "state", None), "value", None),
           result.route_decision.reason_code if result.route_decision is not None else None,
           None if self._frame is None else self._frame.body.is_on_ground)
    if not seen or seen[-1][1] != key:
        seen.append((None if self._frame is None else self._frame.body.sequence_id, key))
    return result


NavigationSession.propose = propose
base = next(s for s in SCENARIOS if s.name == "direct_drop_2")
r = run(replace(base, events=[Event("cancel", lambda c: c.tick >= at, lambda c: c.driver.release("harness_cancel"))],
                perturbations=Perturbations(late_ticks=frozenset({at + 1})), max_ticks=300))
print(r.outcome, r.reason, r.ticks, r.final_position, [(t, c) for t, c, _ in r.violations])
for item in seen[-8:]:
    print(item)

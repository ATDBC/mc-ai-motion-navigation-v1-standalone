# Run from the repository root of an eb4653c checkout: PYTHONPATH=. python -B <script>
"""Diagnosis only: inner executor/controller state after revision + one late tick (reads private fields)."""
import sys
from dataclasses import replace
import tests.sim.runner as runner
from mc2p.motion_nav.navigation_session import NavigationSession

at = int(sys.argv[1]) if len(sys.argv) > 1 else 36
original = NavigationSession.propose
seen = []


def propose(self, *args, **kwargs):
    result = original(self, *args, **kwargs)
    executor = self._executor
    controller = None if executor is None else getattr(executor, "_controller", None)
    key = (None if executor is None else executor.state.value,
           None if controller is None else type(controller).__name__,
           None if controller is None else getattr(getattr(controller, "state", None), "value", None),
           None if self._frame is None else self._frame.body.is_on_ground,
           result.route_decision.reason_code if result.route_decision is not None else None)
    if not seen or seen[-1][1] != key:
        seen.append((None if self._frame is None else self._frame.body.sequence_id, key))
    return result


NavigationSession.propose = propose
sys.argv = ["revise_then_late.py", str(at)]
import runpy
runpy.run_path(__file__.replace("revise_then_late_inner.py", "revise_then_late.py"), run_name="__main__")
print("-- executor timeline (observation sequence, (route state, controller, controller state, on_ground, decision reason)) --")
for item in seen:
    print(item)

"""The same straight courses through the current formal navigation chain (7348b64), for comparison.

Runtime -> RuntimeNavigationDriver -> NavigationSession -> planner / admission / executors, with the
calculator backend.  Goal = the centre of the prototype's goal box, ordinary +/-0.20 point goal.

    PYTHONPATH=. python -B current_stack.py
"""
import kinodynamic_search as ks
from tests.sim.backend import Scene
from tests.sim.runner import Scenario, run

GOALS = {
    "flat 20": (0.5, 64.0, 20.5), "gap 1": (0.5, 64.0, 14.5), "gap 2": (0.5, 64.0, 15.5),
    "gap 3": (0.5, 64.0, 16.5), "gap 4": (0.5, 64.0, 17.5), "gap 3 + 4 floor + gap 3": (0.5, 64.0, 22.5),
    "step up 1 at speed": (0.5, 65.0, 16.5), "turn then gap 3": (17.5, 64.0, 6.5),
}
for course in ks.courses():
    result = run(Scenario(course.name, Scene(dict(course.solids), course.bounds), course.start,
                          GOALS[course.name], max_ticks=400))
    terminal = next((row["tick"] for row in result.trace
                     if row.get("session_state") in {"complete", "failed", "cancelled"}), None)
    start_tick = result.trace[0]["tick"] if result.trace else 0
    print(f"{course.name:<26} {result.outcome:<8} {result.reason:<36} "
          f"ticks to task end={None if terminal is None else terminal - start_tick}  damage={result.damage}")

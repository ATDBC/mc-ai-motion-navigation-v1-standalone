"""Does a test leave a shared simulator scenario mutated?  (1f0fefe)

    PYTHONPATH=. python -B scenario_leak_check.py

Runs RouteHandoffTests.test_ready_probe_waits_for_selected_route_and_keeps_late_input_owner in
this process and prints the module-level SCENARIOS["direct_drop_2"].perturbations before and after.
That test's control step assigns context.backend.perturbations.late_ticks; dataclasses.replace()
is shallow, so the Perturbations object of the shared scenario is the one being changed.
"""
import unittest

from tests.sim.scenarios import SCENARIOS

shared = next(s for s in SCENARIOS if s.name == "direct_drop_2")
print("before:", id(shared.perturbations), "late_ticks =", sorted(shared.perturbations.late_ticks))
suite = unittest.defaultTestLoader.loadTestsFromName(
    "tests.motion_nav.test_navigation_route_handoff.RouteHandoffTests."
    "test_ready_probe_waits_for_selected_route_and_keeps_late_input_owner")
outcome = unittest.TextTestRunner(verbosity=0).run(suite)
print("after: ", id(shared.perturbations), "late_ticks =", sorted(shared.perturbations.late_ticks))
print("leaked" if shared.perturbations.late_ticks else "no leak")

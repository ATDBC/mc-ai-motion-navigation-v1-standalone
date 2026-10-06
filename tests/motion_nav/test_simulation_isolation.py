"""One simulated task cannot rewrite the next task's disturbances."""
from copy import deepcopy
from dataclasses import replace
import unittest

from tests.sim.backend import Perturbations
from tests.sim.runner import Event, run
from tests.sim.scenario_isolation import shared_scenario_hash
from tests.sim.scenarios import SCENARIOS
from tests.motion_nav import test_navigation_route_handoff as handoff_checks
from tests.motion_nav import test_navigation_supervised_interruptions as interruption_checks


class SimulationIsolationTests(unittest.TestCase):
    def test_late_input_and_nested_edits_do_not_change_shared_scenarios(self):
        base = next(s for s in SCENARIOS if s.name == "flat_walk")
        before = shared_scenario_hash()
        originals = deepcopy(base.perturbations)
        def mutate(context):
            context.backend.perturbations.late_ticks |= frozenset({context.backend.movement_tick + 1})
            context.backend.perturbations.impulses[999] = (0., 0., .01)
            context.backend.perturbations.world_edits[999] = {(0, 63, 4): "minecraft:stone"}
        first = run(replace(base, events=[Event("mutate-local-disturbances", lambda c: c.tick >= 10, mutate)]))
        self.assertTrue(first.events)
        self.assertEqual(shared_scenario_hash(), before)
        self.assertEqual(base.perturbations, originals)
        second = run(base)
        pristine = run(replace(base, perturbations=Perturbations()))
        self.assertEqual(second.applied_perturbations, pristine.applied_perturbations)
        self.assertEqual([(r["position"], r["sampled_input"]) for r in second.trace],
                         [(r["position"], r["sampled_input"]) for r in pristine.trace])

    def test_landing_safety_is_the_same_before_and_after_late_input_task(self):
        safety = interruption_checks.SupervisedInterruptionTests(
            "test_removed_landing_support_is_rechecked_four_ticks_before_departure",
        )
        mutator = handoff_checks.RouteHandoffTests(
            "test_ready_probe_waits_for_selected_route_and_keeps_late_input_owner",
        )
        for order in ((mutator, safety), (safety, mutator)):
            with self.subTest(order=[test.id() for test in order]):
                before = shared_scenario_hash()
                result = unittest.TestResult()
                for test in order:
                    # TestCase instances hold cleanup state; create a fresh one per order.
                    type(test)(test._testMethodName).run(result)
                self.assertEqual(result.failures, [])
                self.assertEqual(result.errors, [])
                self.assertEqual(before, shared_scenario_hash())

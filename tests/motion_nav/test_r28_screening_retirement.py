"""Formal planning must not simulate a live controller to choose a terminal."""
import unittest
from unittest.mock import patch

from tests.sim.runner import InlinePlannerWorker, Scenario, lane, run
from tests.sim.product_cases import player_layout


class GeometryOnlyPlanner(InlinePlannerWorker):
    def poll_latest(self):
        with patch('mc2p.motion_nav.fixed_route.FixedRouteController.decide',
                   side_effect=AssertionError('planner ran online tracking')):
            return super().poll_latest()


class ScreeningRetirementTests(unittest.TestCase):
    def test_formal_planning_does_not_run_the_tracking_controller(self):
        result = run(Scenario('no-terminal-rollout', lane([[63]] * 8, width=3),
                              (.5, 64., .5), (.63, 64., 6.4)),
                     planner_factory=GeometryOnlyPlanner)
        self.assertEqual(result.outcome, 'success', result.reason)
        self.assertFalse(result.violations)

    def test_wall_control_failure_is_not_a_screening_conclusion(self):
        scene, start, goal = player_layout('player_wall_head')
        result = run(Scenario('honest-wall-result', scene, start, goal))
        self.assertFalse(result.violations)
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(result.reason, 'fixed_route_has_no_forward_control')


if __name__ == '__main__':
    unittest.main()

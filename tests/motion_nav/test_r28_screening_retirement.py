"""Formal planning must not simulate a live controller to choose a terminal."""
import unittest
from unittest.mock import patch

from tests.sim.runner import InlinePlannerWorker, Scenario, lane, run
from tests.sim.product_cases import player_layout
from scripts.f2_ground_route_evidence import terminal_controller_evidence


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

    def test_wall_outcome_comes_from_live_control_without_screening(self):
        for family in ('player_wall_head', 'player_wall_parallel', 'player_corner',
                       'player_corridor_end', 'player_ledge_1', 'player_ledge_2'):
            with self.subTest(family=family), terminal_controller_evidence() as actual:
                scene, start, goal = player_layout(family)
                result = run(Scenario('honest-result-'+family, scene, start, goal),
                             planner_factory=GeometryOnlyPlanner)
                self.assertFalse(result.violations)
                self.assertEqual(result.outcome, 'success', result.reason)
                self.assertTrue(result.verification_complete)
                self.assertTrue(actual['contracts'])
                self.assertTrue(actual['formal_goal_checks'])


if __name__ == '__main__':
    unittest.main()

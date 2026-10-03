"""Formal product cases retain body responsibility while recovering an entry."""
import json
from pathlib import Path
import unittest
import math

from dataclasses import replace
from mc2p.motion_nav.action_route import ActionRoute, WalkSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
from mc2p.motion_nav.safe_ground_control import verified_ground_rollout
from mc2p.motion_nav.geometry import query_support
from mc2p.motion_nav.world_model import BlockGeometry
from tests.motion_nav.test_b07_step_route import frame
from tests.motion_nav.test_b07_surface_planning import ordinary_profile
from tests.motion_nav.test_jump_up import jump_profile
from tests.motion_nav.test_navigation_session import _ground_anchor, _known_world

from tests.sim.product_cases import product_scenario
from tests.sim.runner import run


class MotionBaselineRecoveryTests(unittest.TestCase):
    def scenario(self, seed):
        manifest = json.loads(Path(
            'tests/sim/manifests/navigation-product-r28-v4.json'
        ).read_text(encoding='utf-8'))
        group = next(x for x in manifest['groups'] if x['id'] == 'drop-late')
        return product_scenario(manifest, group, seed)[0]

    def test_changed_heading_during_background_revalidation_can_realign(self):
        for seed in (15, 69, 119, 163):
            with self.subTest(seed=seed):
                result = run(self.scenario(seed))
                self.assertEqual(result.outcome, 'success', result.reason)
                self.assertFalse(result.violations)
                self.assertFalse(result.trace[-1]['source_bound'])

    def test_grounded_input_loss_retreats_from_tiny_support_before_release(self):
        result = run(self.scenario(56))
        self.assertIn(result.outcome, {'success', 'failed'})
        self.assertFalse(result.violations)
        self.assertFalse(result.trace[-1]['source_bound'])
        self.assertTrue(result.trace[-1]['on_ground'])
        self.assertGreaterEqual(result.trace[-1]['support_fraction'], .01)
        self.assertEqual(result.trace[-1]['damage'], 0)

    def terminal_fixture(self, terminal):
        world = _known_world({(0, 0, 0): BlockGeometry.full_cube('minecraft:stone')})
        initial = frame(world, 0, (.5, 1., .5))
        executor = ActionRouteExecutor(ordinary_profile(), jump_profile())
        executor.start(ActionRoute('edge-terminal', (WalkSegment(
            FixedRoute('edge-walk', (RoutePoint(.5, 1., .5), RoutePoint(.5, 1., .6))),
            ((0, 1, 0),), ((0, 0, 0),)),)), initial)
        executor.state = terminal
        current = frame(world, 1, (.5, 1., 1.297))
        return executor, current, _ground_anchor(current)

    def test_each_terminal_result_retains_only_calculator_proved_retreat(self):
        for terminal in (ActionRouteState.COMPLETE, ActionRouteState.CANCELLED,
                         ActionRouteState.FAILED, ActionRouteState.UNSUPPORTED,
                         ActionRouteState.INPUT_LOST):
            with self.subTest(terminal=terminal):
                executor, current, anchor = self.terminal_fixture(terminal)
                decision = executor.decide(current, state_anchor=anchor)
                self.assertEqual(decision.state, terminal)
                self.assertTrue(decision.submit_input)
                self.assertTrue(decision.movement.sneak)
                stopped = verified_ground_rollout(current, anchor.physics_state,
                    decision.movement, control_ticks=1, tail_ticks=8, minimum_support=.0001)
                self.assertIsNotNone(stopped)
                self.assertLess(stopped.position[2], current.body.position[2])

    def test_missing_current_anchor_or_airborne_body_cannot_authorize_retreat(self):
        executor, current, anchor = self.terminal_fixture(ActionRouteState.INPUT_LOST)
        for body, state in ((current, None),
                            (current, replace(anchor, observation_sequence_id=0)),
                            (replace(current, body=replace(current.body, is_on_ground=False)), anchor)):
            with self.subTest(body=body.body.is_on_ground, anchor=state is not None):
                decision = executor.decide(body, state_anchor=state)
                self.assertFalse(decision.submit_input)

    def test_terminal_retreat_uses_the_conditioned_winning_yaw(self):
        for yaw in (0., math.pi / 2, math.pi):
            with self.subTest(yaw=yaw):
                executor, current, anchor = self.terminal_fixture(ActionRouteState.INPUT_LOST)
                decision = executor.decide(current, state_anchor=anchor, movement_yaw_radians=yaw)
                actual = verified_ground_rollout(current,
                    replace(anchor.physics_state, yaw_radians=yaw), decision.movement,
                    control_ticks=1, tail_ticks=8, minimum_support=.0001)
                self.assertIsNotNone(actual)
                before = query_support(current.body.body_box, current.world)
                after = query_support(actual.body_box, current.world)
                self.assertGreater(after.support_fraction, before.support_fraction + .001)


if __name__ == '__main__':
    unittest.main()

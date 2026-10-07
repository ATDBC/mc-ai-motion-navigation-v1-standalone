"""An unrelated controller and result plug into the fixed action interface."""
from dataclasses import dataclass, replace
from types import SimpleNamespace
import unittest
import ast
from pathlib import Path
from unittest.mock import patch

from mc2p.contracts.action_v1 import MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.actions.registry import ACTION_REGISTRY, ActionRegistry
from mc2p.motion_nav.action_route import ActionRoute, ControlledDropSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor, ActionRouteState
from tests.motion_nav.test_action_preconditions import _drop_route
from tests.motion_nav.test_continuous_descent import world_and_anchor
from tests.motion_nav import test_b10_motion_candidate as motion_candidate
from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
from tests.motion_nav.test_jump_up import jump_profile


@dataclass(frozen=True)
class NewAction:
    dependencies: tuple = ()
    entry_window: object = None


@dataclass(frozen=True)
class NewResult:
    finished: bool
    cancelled: bool
    confirmed: bool


class NewController:
    def __init__(self): self.cancelled = False; self.finished = False
    def cancel(self): self.cancelled = True
    def decide(self, frame, *, input_confirmed):
        return NewResult(self.finished, self.cancelled, input_confirmed)


class ControllerAdapterTests(unittest.TestCase):
    def test_executor_does_not_inspect_air_controller_type(self):
        tree = ast.parse(Path('mc2p/motion_nav/action_route_executor.py').read_text())
        comparisons = [n for n in ast.walk(tree) if isinstance(n, ast.Compare)]
        self.assertFalse(any(isinstance(n, ast.Name) and n.id == 'AirMotionController'
                             for comparison in comparisons for n in ast.walk(comparison)))
    def adapter(self):
        from mc2p.motion_nav.actions.contracts import ActionControllerAdapter, ActionControllerResult
        def interpret(result):
            state = (ActionRouteState.COMPLETE if result.finished else
                     ActionRouteState.CANCELLED if result.cancelled else
                     ActionRouteState.INPUT_LOST if not result.confirmed else None)
            return ActionControllerResult(state, MovementV1(forward=1), 1, 'new-result')
        def stop(controller, frame):
            return ActionControllerResult(ActionRouteState.CANCELLED, MovementV1(), 1, 'new-stop')
        return ActionControllerAdapter(lambda *args: NewController(), lambda *args: (.05, .1), interpret, stop)

    def test_same_frame_protection_requires_an_adapter_operation(self):
        spec = ACTION_REGISTRY.for_type(ControlledDropSegment)
        with self.assertRaises(ContractViolation):
            ActionRegistry((replace(spec, controller_adapter=replace(self.adapter(), stop_protection=None)),))

    def test_invalid_adapter_object_reports_a_contract_violation(self):
        spec = ACTION_REGISTRY.for_type(ControlledDropSegment)
        with self.assertRaises(ContractViolation):
            ActionRegistry((replace(spec, controller_adapter=object()),))

    def test_all_three_adapter_functions_are_required(self):
        spec = ACTION_REGISTRY.for_type(ControlledDropSegment)
        adapter = self.adapter()
        for member in ('create', 'entry_limits', 'interpret'):
            with self.subTest(member=member), self.assertRaises(ContractViolation):
                ActionRegistry((replace(spec, controller_adapter=replace(adapter, **{member: None})),))

    def test_unrelated_controller_executes_cancels_and_reports_input_loss(self):
        spec = ACTION_REGISTRY.for_type(ControlledDropSegment)
        registry = ActionRegistry(ACTION_REGISTRY.specs + (replace(spec,
            segment_type=NewAction, controller_adapter=self.adapter(),
            requires_verified_motion=False, needs_background_solving=False,
            solve_kind=None, solve_geometry=None, tracks_damage=False,
            damage_committed=lambda *args, **kwargs: False),))
        anchor, world = world_and_anchor(direct_height=6, speed=0.0)
        frame = motion_candidate.VerifiedMotionRouteIntegrationTests.frame(world._world, anchor.physics_state, 1)
        with patch('mc2p.motion_nav.actions.registry.ACTION_REGISTRY', registry):
            route = ActionRoute('new-controller-route', (NewAction(),))
            for cancelled, confirmed, expected in ((False, True, ActionRouteState.RUNNING),
                (True, True, ActionRouteState.CANCELLED), (False, False, ActionRouteState.INPUT_LOST)):
                executor = ActionRouteExecutor(ground_profile(), jump_profile())
                executor.start(route, frame, require_verified_gap_motion=False)
                self.assertIs(type(executor._controller), NewController)
                if cancelled: executor.cancel()
                decision = executor.decide(frame, input_confirmed=confirmed)
                self.assertIs(decision.state, expected)
                self.assertEqual(decision.reason_code, 'new-result')

            executor = ActionRouteExecutor(ground_profile(), jump_profile())
            executor.start(route, frame, require_verified_gap_motion=False)
            previous = executor.decide(frame)
            from mc2p.motion_nav.body_control import StopCause
            executor.request_stop(StopCause.CANCELLED)
            stopped = executor.stop_protection(previous, frame, state_anchor=anchor)
            self.assertIs(stopped.state, ActionRouteState.CANCELLED)
            self.assertEqual(stopped.reason_code, 'new-stop')

            complete = ActionRouteExecutor(ground_profile(), jump_profile())
            complete.start(route, frame, require_verified_gap_motion=False)
            complete._controller.finished = True
            self.assertIs(complete.decide(frame).state, ActionRouteState.COMPLETE)

            from mc2p.motion_nav.action_route import WalkSegment
            from mc2p.motion_nav.fixed_route import FixedRoute, RoutePoint
            prefix = WalkSegment(FixedRoute('prefix', (RoutePoint(.5, 64, .5),
                                                      RoutePoint(.5, 64, 1.5))), ((0, 64, 0), (0, 64, 1)), ())
            approach = ActionRouteExecutor(ground_profile(), jump_profile())
            approach.start(ActionRoute('new-approach', (prefix, NewAction())), frame,
                           require_verified_gap_motion=False)
            self.assertEqual(approach._controller.config.endpoint_tolerance_blocks, .05)
            self.assertEqual(approach._controller.config.stopped_speed_blocks_per_second, .1)
            from mc2p.motion_nav.fixed_route import FixedRouteDecision, FixedRouteState
            first_done = FixedRouteDecision(FixedRouteState.SUCCEEDED, MovementV1(),
                                            1.0, 0.0, (), 0, 1, 'prefix-done')
            with patch.object(approach._controller, 'decide', return_value=first_done):
                next_decision = approach.decide(frame)
            self.assertIs(type(approach._controller), NewController)
            self.assertEqual(next_decision.action_index, 1)
            self.assertEqual(next_decision.reason_code, 'new-result')
            self.assertEqual(next_decision.movement, MovementV1(forward=1))

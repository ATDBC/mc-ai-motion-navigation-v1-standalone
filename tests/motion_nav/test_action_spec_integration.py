"""Action declarations are consumed at the existing owner boundaries."""
from dataclasses import dataclass, fields, replace
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from mc2p.contracts.common import ContractViolation

from mc2p.motion_nav.actions.registry import ACTION_REGISTRY, ActionRegistry
from mc2p.motion_nav.action_preconditions import (
    ActionPreconditionResult, ActionPreconditionStatus, ActionPreconditionReason,
    check_action_precondition,
)
from mc2p.motion_nav.action_route import ControlledDropSegment
from mc2p.motion_nav.action_route_executor import ActionRouteExecutor
from mc2p.motion_nav.actions.contracts import BodyCommitment
from mc2p.motion_nav.motion_solver import MotionSolveKind
from mc2p.motion_nav import motion_coordination
from mc2p.motion_nav.navigation_session import NavigationSession
from tests.motion_nav.test_action_preconditions import _drop_route
from tests.motion_nav.test_continuous_descent import world_and_anchor
from tests.motion_nav import test_b10_motion_candidate as motion_candidate


@dataclass(frozen=True, slots=True)
class RegisteredDescentProbe(ControlledDropSegment):
    """Test-only identity using existing descent geometry and safety rules."""


class ActionSpecOwnerBoundaryTests(TestCase):
    def registry_with(self, **changes):
        return ActionRegistry(tuple(
            replace(spec, **changes) if spec.segment_type is ControlledDropSegment else spec
            for spec in ACTION_REGISTRY.specs))

    def test_route_risk_uses_the_registered_declaration(self):
        with patch('mc2p.motion_nav.actions.registry.ACTION_REGISTRY',
                   self.registry_with(expected_damage_points=lambda action: 7.0)):
            self.assertEqual(NavigationSession._route_expected_damage_points(_drop_route()), 7.0)

    def test_executor_books_registered_damage_once_at_commitment(self):
        action = _drop_route().action_route.actions[0]
        owner = SimpleNamespace(action_index=0, _completed_movement_damage_points=0.0,
                                _committed_damage_actions=set())
        frame = SimpleNamespace(body=SimpleNamespace(is_on_ground=True))
        with patch('mc2p.motion_nav.actions.registry.ACTION_REGISTRY',
                   self.registry_with(expected_damage_points=lambda action: 7.0)):
            ActionRouteExecutor._commit_action_damage_if_started(owner, action, frame)
            self.assertEqual(owner._completed_movement_damage_points, 0.0)
            ActionRouteExecutor._commit_action_damage_if_started(owner, action, frame, force=True)
            ActionRouteExecutor._commit_action_damage_if_started(owner, action, frame, force=True)
            self.assertEqual(owner._completed_movement_damage_points, 7.0)

    def test_body_handoff_reads_the_explicit_commitment(self):
        from mc2p.motion_nav.action_route_executor import ActionRouteState
        anchor, world = world_and_anchor(direct_height=6, speed=0.0)
        frame = motion_candidate.VerifiedMotionRouteIntegrationTests.frame(world._world, anchor.physics_state, 1)
        owner = SimpleNamespace(route=_drop_route().action_route, _controller=object(),
                                state=ActionRouteState.RUNNING, action_index=0)
        with patch('mc2p.motion_nav.actions.registry.ACTION_REGISTRY',
                   self.registry_with(body_commitment=BodyCommitment.GROUND)):
            self.assertFalse(ActionRouteExecutor.requires_safe_handoff(owner, frame))

    def test_precondition_facade_uses_the_registered_declaration(self):
        anchor, world = world_and_anchor(direct_height=6, speed=0.0)
        frame = motion_candidate.VerifiedMotionRouteIntegrationTests.frame(world._world, anchor.physics_state, 1)
        result = ActionPreconditionResult(ActionPreconditionStatus.REJECTED,
                                         ActionPreconditionReason.LANDING_SUPPORT_MISSING)
        with patch('mc2p.motion_nav.actions.registry.ACTION_REGISTRY',
                   self.registry_with(precondition=lambda *args, **kwargs: result)):
            self.assertIs(check_action_precondition(_drop_route(), 0, frame, task_id='test-task'), result)

    def test_same_frame_protection_uses_the_registered_stop_declaration(self):
        spec = ACTION_REGISTRY.for_type(ControlledDropSegment)
        executor = SimpleNamespace(route=_drop_route().action_route,
                                   active_verified_entry_state=lambda: None)
        advance = SimpleNamespace(route_advance=SimpleNamespace(
            control=SimpleNamespace(executor=executor), decision=SimpleNamespace(action_index=0)))
        with patch('mc2p.motion_nav.actions.registry.ACTION_REGISTRY',
                   self.registry_with(stop_hold=replace(spec.stop_hold, same_frame_protection=False))):
            self.assertFalse(NavigationSession._needs_same_frame_stop_protection(advance))



    def test_boundary_selection_keeps_started_and_upcoming_checks_separate(self):
        from mc2p.motion_nav.action_preconditions import select_current_boundary, select_upcoming_boundary
        from mc2p.motion_nav.action_route import ActionRoute
        route = _drop_route()
        anchor, world = world_and_anchor(direct_height=6, speed=0.0)
        frame = motion_candidate.VerifiedMotionRouteIntegrationTests.frame(world._world, anchor.physics_state, 1)
        self.assertIsNone(select_current_boundary(route, 0, None, frame, started=True,
                                                  through_grounded_departure=False))
        choice = select_current_boundary(route, 0, None, frame, started=True,
                                         through_grounded_departure=True)
        self.assertEqual(choice.action_index, 0)
        with self.assertRaises((AttributeError, TypeError)):
            choice.action_index = 1
        route = replace(route, action_route=ActionRoute(route.route_id, route.action_route.actions * 2))
        self.assertEqual(select_upcoming_boundary(route, 0, frame, probe_binding=None, grant=None).action_index, 1)
        airborne = replace(frame, body=replace(frame.body, is_on_ground=False))
        self.assertIsNone(select_upcoming_boundary(route, 0, airborne, probe_binding=None, grant=None))
        binding = (route.route_id, route.route_revision, 1)
        self.assertEqual(select_upcoming_boundary(route, 0, airborne, probe_binding=binding, grant=None).action_index, 1)
        self.assertIsNone(select_upcoming_boundary(route, 0, airborne, probe_binding=('old', 0, 1), grant=None))

    def test_solving_reads_kind_and_geometry_from_the_registered_spec(self):
        action = _drop_route().action_route.actions[0]
        geometry = ACTION_REGISTRY.require(action).solve_geometry(action, None)
        with patch('mc2p.motion_nav.actions.registry.ACTION_REGISTRY', self.registry_with(
                solve_kind=MotionSolveKind.JUMP_GAP,
                solve_geometry=lambda action, anchor: replace(geometry, direction=(-1, 0)))):
            self.assertIs(motion_coordination._air_action_kind(action), MotionSolveKind.JUMP_GAP)
            self.assertEqual(motion_coordination._air_action_direction(action), (-1, 0))

    def test_new_identity_enters_route_solver_and_executor_via_only_registration(self):
        from mc2p.motion_nav.action_route import ActionRoute
        from mc2p.motion_nav.air_motion import AirMotionController
        from mc2p.motion_nav.movement_transition import MovementMode
        from tests.motion_nav.test_b09_air_transitions import air_profile
        from tests.motion_nav.test_fixed_route_walk import profile as ground_profile
        from tests.motion_nav.test_jump_up import jump_profile
        base = _drop_route()
        original = base.action_route.actions[0]
        profile = air_profile(MovementMode.CONTROLLED_DROP)
        action = RegisteredDescentProbe(**{field.name: getattr(original, field.name)
                                          for field in fields(original)})
        action = replace(action, edge=replace(action.edge, profile_id=profile.profile_id))
        registry = ActionRegistry(ACTION_REGISTRY.specs + (
            replace(ACTION_REGISTRY.require(original), segment_type=RegisteredDescentProbe),))
        anchor, world = world_and_anchor(direct_height=6, speed=0.0)
        frame = motion_candidate.VerifiedMotionRouteIntegrationTests.frame(world._world, anchor.physics_state, 1)
        with self.assertRaises(ContractViolation):
            ActionRoute(base.route_id, (action,))
        with patch('mc2p.motion_nav.actions.registry.ACTION_REGISTRY', registry):
            route = replace(base, action_route=ActionRoute(base.route_id, (action,)))
            self.assertEqual(NavigationSession._route_expected_damage_points(route), 3.0)
            self.assertIs(motion_coordination._air_action_kind(action), MotionSolveKind.CONTROLLED_DROP)
            self.assertEqual(motion_coordination._air_action_direction(action), (0, 1))
            self.assertIs(check_action_precondition(route, 0, frame, task_id='probe').status,
                          ActionPreconditionStatus.NEEDS_ACQUISITION)
            executor = ActionRouteExecutor(ground_profile(), jump_profile(), air_profiles=(profile,))
            executor.start(route.action_route, frame, require_verified_gap_motion=False)
            self.assertIs(type(executor._controller), AirMotionController)
            self.assertTrue(executor.requires_safe_handoff(frame))
            waiting = ActionRouteExecutor(ground_profile(), jump_profile(), air_profiles=(profile,))
            waiting.start(route.action_route, frame, require_verified_gap_motion=False,
                          require_verified_motion_actions=frozenset({0}))
            self.assertIsNone(waiting._controller)



    def test_destination_recovery_uses_the_registered_completion_query(self):
        anchor, world = world_and_anchor(direct_height=6, speed=0.0)
        frame = motion_candidate.VerifiedMotionRouteIntegrationTests.frame(world._world, anchor.physics_state, 1)
        action = _drop_route().action_route.actions[0]
        with patch('mc2p.motion_nav.actions.registry.ACTION_REGISTRY',
                   self.registry_with(completed=lambda action, frame: True)):
            self.assertTrue(ActionRouteExecutor._landed_on_current_action_destination(action, frame))

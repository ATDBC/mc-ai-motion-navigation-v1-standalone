"""Action rules are explicit, immutable, and reject unregistered action types."""
from dataclasses import FrozenInstanceError, replace
import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.action_route import (
    ControlledDropSegment, JumpGapSegment, JumpUpSegment, StepSegment, WalkSegment,
)
from mc2p.motion_nav.actions.contracts import BodyCommitment, StopHold
from mc2p.motion_nav.actions.registry import ACTION_REGISTRY, ActionRegistry


class ActionSpecContractTests(unittest.TestCase):
    def test_all_five_existing_actions_are_registered_with_explicit_safety(self):
        self.assertEqual(ACTION_REGISTRY.registered_types, frozenset({
            WalkSegment, JumpUpSegment, StepSegment, JumpGapSegment, ControlledDropSegment,
        }))
        for spec in ACTION_REGISTRY.specs:
            self.assertIsInstance(spec.body_commitment, BodyCommitment)
            self.assertIs(type(spec.requires_verified_motion), bool)
            self.assertIs(type(spec.needs_background_solving), bool)
            self.assertIs(type(spec.stop_hold), StopHold)
            self.assertTrue(callable(spec.expected_damage_points))

    def test_duplicate_or_unknown_action_is_rejected(self):
        with self.assertRaises(ContractViolation):
            ActionRegistry(ACTION_REGISTRY.specs + (ACTION_REGISTRY.specs[0],))
        with self.assertRaises(ContractViolation):
            ACTION_REGISTRY.require(object())

    def test_missing_safety_declarations_are_rejected_at_registration(self):
        spec = ACTION_REGISTRY.for_type(ControlledDropSegment)
        for member in ('body_commitment', 'requires_verified_motion',
                       'expected_damage_points', 'stop_hold', 'needs_background_solving',
                       'tracks_damage', 'damage_committed'):
            with self.subTest(member=member):
                with self.assertRaises(ContractViolation):
                    ActionRegistry((replace(spec, **{member: None}),))

    def test_background_solving_must_have_a_kind_and_geometry(self):
        spec = ACTION_REGISTRY.for_type(ControlledDropSegment)
        for changes in ({'solve_kind': None}, {'solve_geometry': None}):
            with self.subTest(changes=changes):
                with self.assertRaises(ContractViolation):
                    ActionRegistry((replace(spec, **changes),))

    def test_air_controller_creation_cannot_be_left_undeclared(self):
        spec = ACTION_REGISTRY.for_type(ControlledDropSegment)
        with self.assertRaises(ContractViolation):
            ActionRegistry((replace(spec, controller_adapter=replace(spec.controller_adapter, create=None)),))

    def test_spec_and_stop_declarations_cannot_store_execution_state(self):
        spec = ACTION_REGISTRY.for_type(ControlledDropSegment)
        with self.assertRaises(FrozenInstanceError):
            spec.body_commitment = BodyCommitment.GROUND
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            spec.owner_id = 'one-run-owner'
        with self.assertRaises(FrozenInstanceError):
            spec.stop_hold.same_frame_protection = False
        self.assertFalse(hasattr(spec, '__dict__'))




    def test_collected_geometry_and_safety_facts_match_the_frozen_corpus(self):
        import json
        from dataclasses import asdict
        from pathlib import Path
        from types import SimpleNamespace
        from mc2p.motion_nav.actions.existing import AIR_CONTROLLER_ADAPTER
        from mc2p.motion_nav.actions.registry import action_spec
        from mc2p.contracts.action_v1 import MovementV1
        from tests.motion_nav.action_spec_fixtures import geometry_action
        corpus = json.loads(Path('evidence/motion_navigation/redesign-m1/controlled-drop-corpus.json').read_text())
        self.assertEqual(len(corpus), 9)
        anchor = SimpleNamespace(physics_state=SimpleNamespace(body_width=.6))
        for record in corpus:
            action = geometry_action(record['action'])
            expected, spec = record['expected'], action_spec(action)
            self.assertEqual(spec.expected_damage_points(action), expected['damage_points'])
            self.assertIs(spec.body_commitment, BodyCommitment.TRANSITION)
            self.assertIs(spec.controller_adapter, AIR_CONTROLLER_ADAPTER)
            self.assertTrue(spec.requires_verified_motion)
            self.assertTrue(spec.stop_hold.same_frame_protection)
            self.assertEqual(spec.stop_hold.information_movement, MovementV1(sneak=True))
            self.assertEqual(spec.stop_hold.dependency_movement, MovementV1(sneak=True))
            self.assertEqual(spec.solve_kind.value, expected['solve_kind'])
            self.assertEqual(spec.entry_observation(action, None).needs_acquisition_before_solve,
                             expected['requires_acquisition'])
            geometry = spec.solve_geometry(action, anchor)
            self.assertEqual(geometry.direction, tuple(expected['direction']))
            self.assertEqual(asdict(geometry.landing), expected['landing'])
            for case in expected['completion_cases']:
                frame = SimpleNamespace(body=SimpleNamespace(
                    position=case['position'], is_on_ground=case['grounded']))
                self.assertEqual(spec.completed(action, frame), case['completed'])


if __name__ == '__main__':
    unittest.main()

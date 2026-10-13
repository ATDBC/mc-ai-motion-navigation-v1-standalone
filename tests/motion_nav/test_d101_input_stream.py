"""D101 isolated stream timing and key-node comparison contracts."""
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch

from experiments.motion_navigation.trajectory_proto.d101_input_stream import (
    FirstApplicationReceipt, InputStreamTracker, StreamGeneration, StreamStartBranch,
)
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.physics_types import TickInput
from tests.motion_nav.test_trajectory_proto_contracts import physics_state


def generation():
    walk = TickInput(1., 0., False, False, False, 0.)
    jump = replace(walk, jump=True)
    return StreamGeneration(
        generation_id="stream-1", owner_id="owner-1", request_id="request-1",
        action_index=2, requested_start_tick=11, commands=(walk, jump, walk),
        dependency_identity=(("world-1", 3), ("route-1", 4)),
        first_control_sequence=100,
    )


def receipt(stream, *, tick=11, sequence=100, command=None):
    return FirstApplicationReceipt(sequence, tick,
                                   stream.commands[0] if command is None else command)


class InputStreamTests(unittest.TestCase):
    def test_first_receipt_selects_only_the_on_time_or_late_one_start_branch(self):
        stream = generation()
        for offset, branch in ((0, StreamStartBranch.ON_TIME), (1, StreamStartBranch.LATE_ONE)):
            with self.subTest(offset=offset):
                tracker = InputStreamTracker.start(stream)
                self.assertEqual(tracker.record_first_application(receipt(stream, tick=11 + offset)), branch)
                self.assertEqual(tuple(tracker.expected_effect_tick(index) for index in range(3)),
                                 (11 + offset, 12 + offset, 13 + offset))

    def test_second_command_cannot_choose_a_new_late_one_branch(self):
        stream = generation()
        tracker = InputStreamTracker.start(stream)
        tracker.record_first_application(receipt(stream))
        with self.assertRaises(ContractViolation):
            tracker.record_first_application(receipt(stream, tick=13, sequence=101,
                                                     command=stream.commands[1]))
        self.assertEqual(tracker.expected_effect_tick(1), 12)

    def test_first_receipt_late_two_ticks_is_rejected(self):
        stream = generation()
        tracker = InputStreamTracker.start(stream)
        with self.assertRaises(ContractViolation):
            tracker.record_first_application(receipt(stream, tick=13))
        with self.assertRaises(ContractViolation):
            tracker.expected_effect_tick(0)

    def test_duplicate_or_out_of_order_first_receipt_is_rejected(self):
        stream = generation()
        for sequence in (99, 101):
            with self.subTest(sequence=sequence), self.assertRaises(ContractViolation):
                InputStreamTracker.start(stream).record_first_application(receipt(stream, sequence=sequence))
        tracker = InputStreamTracker.start(stream)
        tracker.record_first_application(receipt(stream))
        with self.assertRaises(ContractViolation):
            tracker.record_first_application(receipt(stream))

    def test_wrong_command_or_old_stream_receipt_is_rejected(self):
        stream = generation()
        with self.assertRaises(ContractViolation):
            InputStreamTracker.start(stream).record_first_application(
                receipt(stream, command=stream.commands[1]))
        new_stream = replace(stream, generation_id="stream-2", first_control_sequence=200)
        with self.assertRaises(ContractViolation):
            InputStreamTracker.start(new_stream).record_first_application(receipt(stream))

    def test_generation_and_receipt_are_immutable_and_invalid_ticks_are_rejected(self):
        stream = generation()
        for value, attribute in ((stream, "generation_id"), (receipt(stream), "effect_tick")):
            with self.assertRaises(FrozenInstanceError):
                setattr(value, attribute, "changed")
        with self.assertRaises(ContractViolation):
            replace(stream, commands=list(stream.commands))
        tracker = InputStreamTracker.start(stream)
        tracker.record_first_application(receipt(stream))
        for index in (-1, 3, True):
            with self.subTest(index=index), self.assertRaises(ContractViolation):
                tracker.expected_effect_tick(index)


def envelope(stream=None, *, branch=StreamStartBranch.ON_TIME):
    from experiments.motion_navigation.trajectory_proto.d101_input_stream import KeyNodeEnvelope
    stream = generation() if stream is None else stream
    return KeyNodeEnvelope(
        generation=stream, start_branch=branch, command_index=0,
        reference_state=replace(physics_state(), movement_tick_id=11 + int(branch is StreamStartBranch.LATE_ONE)),
        position_min=(.4, 1., .4), position_max=(.6, 1., .6),
        velocity_min=(-.01, 0., -.01), velocity_max=(.01, 0., .01),
        yaw_range=(-.02, .02), pitch_range=(-.02, .02),
        commitment_command_index=1,
    )


class KeyNodeTests(unittest.TestCase):
    def ready_tracker(self, stream=None, *, late=False):
        stream = generation() if stream is None else stream
        tracker = InputStreamTracker.start(stream)
        tracker.record_first_application(receipt(stream, tick=11 + int(late)))
        return tracker

    def compare(self, tracker, observed, node, **overrides):
        return tracker.compare_key_node(
            observed, node, generation_id=overrides.get("generation_id", tracker.generation.generation_id),
            dependency_identity=overrides.get("dependency_identity", tracker.generation.dependency_identity))

    def test_matching_key_node_is_compare_only_and_calls_no_full_proof(self):
        from experiments.motion_navigation.trajectory_proto.d101_input_stream import KeyNodeCheckKind
        node = envelope()
        for observed in (node.reference_state,
                         replace(node.reference_state, position=node.position_min,
                                 velocity_blocks_per_tick=node.velocity_max, yaw_radians=.02)):
            with self.subTest(observed=observed), patch(
                    "experiments.motion_navigation.trajectory_proto.commitment.scan_commitment") as counter:
                result = self.compare(self.ready_tracker(), observed, node)
                self.assertEqual(result.kind, KeyNodeCheckKind.COMPARE_ONLY)
                self.assertEqual(result.full_proof_calls, 0)
                self.assertEqual(counter.call_count, 0)
                self.assertTrue({"generation", "dependency_identity", "start_branch", "position",
                                 "velocity_blocks_per_tick", "pose", "on_ground", "commitment_phase"}
                                <= set(result.compared_fields))

    def test_body_outside_envelope_requires_reproof_without_external_force_label(self):
        from experiments.motion_navigation.trajectory_proto.d101_input_stream import KeyNodeCheckKind
        node = envelope()
        for changes in ({"position": (.600001, 1., .5)},
                        {"velocity_blocks_per_tick": (.010001, 0., 0.)},
                        {"pose": "crouching"}, {"on_ground": False},
                        {"yaw_radians": .020001}, {"gravity_attribute": .09},
                        {"fall_distance_blocks": 1.}):
            with self.subTest(changes=changes):
                result = self.compare(self.ready_tracker(), replace(node.reference_state, **changes), node)
                self.assertEqual(result.kind, KeyNodeCheckKind.REPROOF_REQUIRED)
                self.assertEqual(result.full_proof_calls, 0)

    def test_external_force_label_inside_envelope_does_not_invalidate_the_proof(self):
        from experiments.motion_navigation.trajectory_proto.d101_input_stream import KeyNodeCheckKind
        node = envelope()
        # Attribution stays diagnostic: neither label changes the comparison input.
        for external_force_label in (None, "entity_push", "explosion"):
            with self.subTest(external_force_label=external_force_label):
                result = self.compare(self.ready_tracker(),
                                      replace(node.reference_state, position=(.59, 1., .5)), node)
                self.assertEqual(result.kind, KeyNodeCheckKind.COMPARE_ONLY)
                self.assertEqual(result.full_proof_calls, 0)

    def test_dependency_or_generation_change_requires_reproof(self):
        from experiments.motion_navigation.trajectory_proto.d101_input_stream import KeyNodeCheckKind
        node = envelope()
        for overrides in ({"dependency_identity": (("world-1", 4), ("route-1", 4))},):
            with self.subTest(overrides=overrides):
                result = self.compare(self.ready_tracker(), node.reference_state, node, **overrides)
                self.assertEqual(result.kind, KeyNodeCheckKind.REPROOF_REQUIRED)
        for field, value in (("generation_id", "stream-old"),
                             ("owner_id", "other-owner"), ("request_id", "other-request"),
                             ("action_index", 3), ("commands", generation().commands[::-1][1:])):
            with self.subTest(field=field):
                other = replace(generation(), **{field: value})
                result = self.compare(self.ready_tracker(), node.reference_state,
                                      replace(node, generation=other))
                self.assertEqual(result.kind, KeyNodeCheckKind.REPROOF_REQUIRED)

    def test_unselected_branch_is_rejected_and_wrong_branch_or_tick_requires_reproof(self):
        from experiments.motion_navigation.trajectory_proto.d101_input_stream import KeyNodeCheckKind
        node = envelope()
        result = self.compare(InputStreamTracker.start(generation()), node.reference_state, node)
        self.assertEqual(result.kind, KeyNodeCheckKind.REJECTED)
        result = self.compare(self.ready_tracker(), node.reference_state, node, generation_id="stream-old")
        self.assertEqual(result.kind, KeyNodeCheckKind.REJECTED)
        result = self.compare(self.ready_tracker(late=True), node.reference_state, node)
        self.assertEqual(result.kind, KeyNodeCheckKind.REPROOF_REQUIRED)
        late_node = envelope(branch=StreamStartBranch.LATE_ONE)
        result = self.compare(self.ready_tracker(late=True), late_node.reference_state, late_node)
        self.assertEqual(result.kind, KeyNodeCheckKind.COMPARE_ONLY)
        result = self.compare(self.ready_tracker(), replace(node.reference_state, movement_tick_id=12), node)
        self.assertEqual(result.kind, KeyNodeCheckKind.REPROOF_REQUIRED)

    def test_envelope_is_frozen_and_cannot_claim_another_commitment_phase(self):
        node = envelope()
        with self.assertRaises(FrozenInstanceError):
            node.position_max = (1., 1., 1.)
        self.assertEqual(node.commitment_phase, "pre_commit")
        self.assertEqual(replace(node, commitment_command_index=0).commitment_phase, "commit_boundary")
        later = replace(node, command_index=2,
                        reference_state=replace(node.reference_state, movement_tick_id=13))
        self.assertEqual(later.commitment_phase, "post_commit")
        for changes in ({"commitment_command_index": 3},
                        {"position_min": (.7, 1., .4)},
                        {"yaw_range": (float("nan"), 0.)}):
            with self.subTest(changes=changes), self.assertRaises(ContractViolation):
                replace(node, **changes)


if __name__ == "__main__":
    unittest.main()

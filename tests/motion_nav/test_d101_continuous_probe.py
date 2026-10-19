"""D101 single jump uses actual receipts and existing proof, not a new search."""
import unittest
from unittest.mock import patch

from experiments.motion_navigation.trajectory_proto.d101_continuous_probe import (
    SingleJumpProbeSpec, run_single_jump_probe,
)
from experiments.motion_navigation.trajectory_proto.d101_input_stream import StreamStartBranch


class SingleJumpProbeTests(unittest.TestCase):
    def test_single_jump_on_time_and_late_one_use_the_same_inputs_and_both_land(self):
        results = [run_single_jump_probe(SingleJumpProbeSpec(branch, 1))[0]
                   for branch in StreamStartBranch]
        self.assertEqual(results[0].input_hash, results[1].input_hash)
        for result in results:
            self.assertEqual(result.terminal_status, "COMPLETED")
            self.assertTrue(result.final_state.on_ground)
            self.assertEqual(result.safety_events, ())
            self.assertGreater(result.physics_steps, len(result.inputs))

    def test_first_receipt_prunes_the_other_branch_before_commitment(self):
        for branch in StreamStartBranch:
            result = run_single_jump_probe(SingleJumpProbeSpec(branch, 1))[0]
            first = next(row for row in result.decisions if row.first_receipt is not None)
            commit = next(row for row in result.decisions if "commitment" in row.key_nodes)
            self.assertEqual(first.surviving_branches, (branch,))
            self.assertLess(first.decision_index, commit.decision_index)
            self.assertTrue(all(row.surviving_branches == (branch,)
                                for row in result.decisions[first.decision_index:]))

    def test_matching_commitment_and_landing_nodes_do_not_repeat_full_proof(self):
        from experiments.motion_navigation.trajectory_proto import d101_continuous_probe as probe
        original = probe.scan_commitment
        with patch.object(probe, "scan_commitment", wraps=original) as counter:
            result = run_single_jump_probe(SingleJumpProbeSpec(StreamStartBranch.ON_TIME, 1))[0]
        self.assertEqual(counter.call_count, 1)
        self.assertEqual(result.full_proof_calls, 1)
        self.assertEqual(result.key_node_full_proof_calls, 0)
        self.assertTrue({"commitment", "landing"} <=
                        {node for row in result.decisions for node in row.key_nodes})
        self.assertTrue(all(check.kind.value == "COMPARE_ONLY"
                            for row in result.decisions for check in row.key_checks))

    def test_middle_command_late_one_is_a_bounded_deviation_not_a_new_start_branch(self):
        result = run_single_jump_probe(SingleJumpProbeSpec(
            StreamStartBranch.ON_TIME, 1, injection="middle_late_one"))[0]
        self.assertEqual(result.terminal_status, "BOUNDED_DEVIATION")
        self.assertEqual(result.reason, "middle_command_late_one")
        self.assertTrue(result.final_state.on_ground)
        self.assertEqual(result.safety_events, ())
        self.assertEqual(result.surviving_branches, (StreamStartBranch.ON_TIME,))
        self.assertTrue(all(row.advance_source in ("candidate", "proved_stop_tail")
                            for row in result.decisions if row.physics_steps))

    def test_generations_allocate_disjoint_control_sequences(self):
        results = run_single_jump_probe(SingleJumpProbeSpec(StreamStartBranch.ON_TIME, 2))
        sequence_sets = [set(row.control_sequence for row in result.decisions
                             if row.control_sequence is not None) for result in results]
        self.assertTrue(sequence_sets[0].isdisjoint(sequence_sets[1]))
        self.assertNotEqual(results[0].generation.generation_id, results[1].generation.generation_id)


if __name__ == "__main__":
    unittest.main()

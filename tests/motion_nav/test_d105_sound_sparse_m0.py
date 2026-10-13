"""D105 A3 keeps the D101 workload but uses only saved sound-sparse proof data."""
import importlib.util
import unittest
from unittest.mock import patch

from experiments.motion_navigation.trajectory_proto import d101_continuous_probe as legacy_probe
from experiments.motion_navigation.trajectory_proto.d101_continuous_probe import SingleJumpProbeSpec
from experiments.motion_navigation.trajectory_proto.d101_input_stream import StreamStartBranch
from experiments.motion_navigation.trajectory_proto.sound_sparse_proof import SoundSparseProof


D105_PROBE_EXISTS = importlib.util.find_spec(
    "experiments.motion_navigation.trajectory_proto.d105_continuous_probe") is not None
D105_MEASUREMENT_EXISTS = importlib.util.find_spec(
    "experiments.motion_navigation.trajectory_proto.d105_measurement") is not None


class D105M0AvailabilityTests(unittest.TestCase):
    def test_d105_has_an_independent_probe_and_measurement_chain(self):
        self.assertTrue(D105_PROBE_EXISTS)
        self.assertTrue(D105_MEASUREMENT_EXISTS)


@unittest.skipUnless(D105_PROBE_EXISTS, "D105 A3 probe has not been implemented")
class D105SoundSparseProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from experiments.motion_navigation.trajectory_proto import d105_continuous_probe

        cls.probe = d105_continuous_probe

    def test_d105_probe_uses_sound_sparse_proof_and_resolves_saved_key_tails(self):
        observed = []
        original = self.probe.scan_sound_sparse_commitment

        def inspect(*args, **kwargs):
            result = original(*args, **kwargs)
            self.assertIs(type(result.proof), SoundSparseProof)
            for branch in result.proof.branches:
                self.assertTrue(branch.safety_tails)
                for compact in branch.safety_tails:
                    full = self.probe.saved_executable_tail(branch, compact.boundary)
                    self.assertEqual(full.inputs, compact.inputs)
                    self.assertEqual(full.states[0], compact.start_state)
                    self.assertEqual(full.states[-1], compact.end_state)
            observed.append(result.proof)
            return result

        with patch.object(self.probe, "scan_sound_sparse_commitment", side_effect=inspect):
            result = self.probe.run_sound_single_jump_probe(SingleJumpProbeSpec(
                StreamStartBranch.ON_TIME, 1))[0]

        self.assertEqual(result.terminal_status, "COMPLETED")
        self.assertEqual(len(observed), 1)

    def test_middle_late_one_uses_saved_non_key_tail_without_reproof(self):
        observed = []
        original = self.probe.scan_sound_sparse_commitment

        def inspect(*args, **kwargs):
            result = original(*args, **kwargs)
            for branch in result.proof.branches:
                self.assertIsNone(branch.safety_tail(1))
                saved = self.probe.saved_executable_tail(branch, 1)
                self.assertEqual(saved.boundary, 1)
            observed.append(result.proof)
            return result

        with patch.object(self.probe, "scan_sound_sparse_commitment", side_effect=inspect):
            result = self.probe.run_sound_single_jump_probe(SingleJumpProbeSpec(
                StreamStartBranch.ON_TIME, 1, injection="middle_late_one"))[0]

        self.assertEqual(result.terminal_status, "BOUNDED_DEVIATION")
        self.assertEqual(result.full_proof_calls, 1)
        self.assertTrue(observed)

    def test_existing_d101_probe_still_uses_legacy_scanner(self):
        original = legacy_probe.scan_commitment
        with patch.object(legacy_probe, "scan_commitment", wraps=original) as legacy_scan:
            result = legacy_probe.run_single_jump_probe(SingleJumpProbeSpec(
                StreamStartBranch.ON_TIME, 1))[0]
        self.assertEqual(result.terminal_status, "COMPLETED")
        self.assertEqual(legacy_scan.call_count, 1)


if __name__ == "__main__":
    unittest.main()

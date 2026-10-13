"""M0 gates consume complete Windows decisions, including initial proof work."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.motion_navigation.trajectory_proto.d101_continuous_probe import (
    SingleJumpProbeSpec, run_single_jump_probe,
)
from experiments.motion_navigation.trajectory_proto.d101_input_stream import StreamStartBranch
from experiments.motion_navigation.trajectory_proto.d101_measurement import (
    recompute_m0, run_m0, summarize_decision_times, summarize_m0,
)


class M0MeasurementTests(unittest.TestCase):
    def test_m0_summary_has_both_start_branches_and_no_safety_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            summary = run_m0(rounds=1, output=Path(folder) / "m0")
            self.assertEqual(summary.positive_groups["on_time"], {"runs": 1, "completed": 1})
            self.assertEqual(summary.positive_groups["late_one"], {"runs": 1, "completed": 1})
            self.assertEqual(summary.safety_violations, 0)
            self.assertEqual(summary.wrong_branch_survivals, 0)
            self.assertEqual(summary.key_node_full_proof_calls, 0)
            self.assertEqual(summary.compare_only_count, 4)
            self.assertGreater(summary.full_proof_calls, 0)
            self.assertGreater(summary.physics_steps, summary.decision_count)
            self.assertEqual(summary, recompute_m0(Path(folder) / "m0"))

    def test_m0_performance_uses_complete_decision_time_and_nearest_rank(self):
        result = run_single_jump_probe(SingleJumpProbeSpec(StreamStartBranch.ON_TIME, 1))[0]
        prototype = result.decisions[0]
        rows = tuple(replace(prototype, duration_ns=i * 1000000) for i in range(1, 101))
        times = summarize_decision_times(rows)
        self.assertEqual((times.p50_ms, times.p95_ms, times.p99_ms, times.max_ms), (50., 95., 99., 100.))
        measured = summarize_decision_times(result.decisions)
        self.assertEqual(measured.samples, len(result.decisions))
        self.assertEqual(measured.max_ms, max(result.decision_durations_ns) / 1000000.)

    def test_protocol_physics_and_comparison_are_inside_the_decision_clock(self):
        from experiments.motion_navigation.trajectory_proto import d101_continuous_probe as probe
        probe.frozen_inputs()
        clock = [0]
        original_step = probe.CountedPhysics.step
        original_compare = probe.InputStreamTracker.compare_key_node
        original_receipt = probe.InputStreamTracker.record_first_application

        def step(*args, **kwargs):
            clock[0] += 17
            return original_step(*args, **kwargs)

        def compare(*args, **kwargs):
            clock[0] += 31
            return original_compare(*args, **kwargs)

        def receipt(*args, **kwargs):
            clock[0] += 43
            return original_receipt(*args, **kwargs)

        with patch.object(probe.time, "perf_counter_ns", side_effect=lambda: clock[0]), \
                patch.object(probe.CountedPhysics, "step", step), \
                patch.object(probe.InputStreamTracker, "compare_key_node", compare), \
                patch.object(probe.InputStreamTracker, "record_first_application", receipt):
            result = probe.run_single_jump_probe(SingleJumpProbeSpec(StreamStartBranch.ON_TIME, 1))[0]
        self.assertEqual(result.decisions[0].duration_ns, result.decisions[0].physics_steps * 17 + 43)
        for row in result.decisions:
            if row.key_checks:
                self.assertEqual(row.duration_ns, row.physics_steps * 17 + 31)

    def test_m0_rejects_reference_only_e13_e14_as_gate_inputs(self):
        for reference in ("E13", "E14"):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                summarize_m0((), rounds=1, source_kind=reference)

    def test_non_windows_samples_cannot_pass_the_formal_gate(self):
        with patch("experiments.motion_navigation.trajectory_proto.d101_measurement.platform.system",
                   return_value="Linux"), self.assertRaises(ValueError):
            run_m0(rounds=1, output=Path("must-not-be-written"))

    def test_existing_evidence_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "existing.json").write_text("original", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                run_m0(rounds=1, output=output)
            self.assertEqual((output / "existing.json").read_text(encoding="utf-8"), "original")


if __name__ == "__main__":
    unittest.main()

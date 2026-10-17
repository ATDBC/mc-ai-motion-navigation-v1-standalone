from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import b10_gap_solver_runtime as probe


TARGET = "coordinator-worker-delay-5-0-0"


def completed_trial():
    chain = dict(identity="job", worker_submit_tick=10, receiver_observed_result_tick=11,
                 registry_publish_tick=11, first_control_visible_tick=15,
                 first_control_consume_tick=15, result_take_tick=15)
    injection = dict(identity="job", worker_submit_tick=10, first_control_visible_tick=15,
                     target_visible_tick=15, requested_delivery_ticks=5,
                     actual_delivery_ticks=5, injection_status="matched")
    return dict(name=TARGET, scenario_kind="worker_delay", terminal_state="complete",
                damage_points=0., source_released=True, landed=True,
                old_manual_intent_selected=False, old_session_intent_selected=False,
                left_start_support=False, bounded_terminal=True,
                recorded_control_frames=1, adopted_frame_count=1,
                horizontal_error_blocks=.01, level_error_blocks=.01,
                horizontal_collisions=0, complete_delivery_chain=True,
                motion_identity="job", delivery_chain=chain, worker_delivery=[chain],
                worker_injection=dict(stale_result_count=0, submissions=[injection]),
                samples=[dict(verified_command_index=0, motion_identity="job",
                              grant_movement_tick=15, expected_movement_tick=16,
                              actual_minus_latest_ticks=0)])


class D113TargetedProbeTests(unittest.TestCase):
    def test_cli_rejects_target_without_b10_and_non_exact_targets_before_launch(self):
        script = Path(probe.ROOT) / "scripts/probe_fabric_deployment_observation.py"
        for arguments in (("--b10-targeted-probe", TARGET),
                          ("--b10-gap-solver-probe", "--b10-targeted-probe", "*")):
            result = subprocess.run([sys.executable, str(script), *arguments],
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("unrecognized arguments", result.stderr)
            self.assertNotIn("FABRIC_DEPLOYMENT_RUN_DIR", result.stdout)

    def test_target_propagates_and_partial_summary_cannot_claim_56_cases(self):
        runtime = MagicMock()
        runtime.state.value = "ready"
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(probe, "PlannerWorker"), \
                patch.object(probe, "MotionSolverWorker"), \
                patch.object(probe, "_run_b10_gap_solver_runtime", side_effect=TimeoutError("stop")) as run:
            with self.assertRaises(TimeoutError):
                probe.run_b10_gap_solver_runtime(runtime, None, "e", Path(directory), 1,
                    lambda *_: None, lambda *_: None, targeted_probe=TARGET)
            self.assertEqual(run.call_args.kwargs["targeted_probe"], TARGET)
            partial = json.loads((Path(directory) / "b10-gap-solver-partial-summary.json").read_text())
            self.assertEqual(partial["coordinator_matrix"]["planned_count"], 1)
            self.assertEqual(partial["synchronous_probe_timing"]["planned_count"], 0)
            self.assertFalse(partial["formal_acceptance_eligible"])

    def test_selector_keeps_frozen_default_and_only_one_explicit_target(self):
        frozen = probe._b10_coordinator_scenarios()
        self.assertEqual(len(frozen), 56)
        self.assertEqual(probe._selected_b10_coordinator_scenarios(None), frozen)
        selected = probe._selected_b10_coordinator_scenarios(TARGET)
        self.assertEqual([s.name for s in selected], ["worker-delay-5-0-0"])
        self.assertEqual(probe._b10_coordinator_scenarios(), frozen)
        for bad in ("", "*", "coordinator-worker-delay-5", "coordinator-worker-delay-5-0-1", True):
            with self.subTest(bad=bad), self.assertRaises((TypeError, ValueError)):
                probe._selected_b10_coordinator_scenarios(bad)

    def test_summary_reports_one_case_without_signing_formal_matrix(self):
        summary, checks = probe._targeted_b10_summary([completed_trial()], runtime_state="ready")
        self.assertEqual(summary["mode"], "targeted_probe")
        self.assertEqual(summary["coordinator_preregistered_count"], 1)
        self.assertFalse(summary["matrix_complete"])
        self.assertFalse(summary["formal_acceptance_eligible"])
        self.assertTrue(all(c["passed"] for c in checks))
        self.assertFalse(any("matrix_is" in c["name"] for c in checks))

    def test_target_requires_recomputed_visible_delay_and_matching_identity(self):
        mutations = [
            lambda t: t["worker_injection"]["submissions"][0].update(first_control_visible_tick=16),
            lambda t: t["worker_injection"]["submissions"][0].update(injection_status="worker_not_ready"),
            lambda t: t["worker_injection"]["submissions"][0].update(worker_submit_tick=9),
            lambda t: (t["delivery_chain"].update(worker_submit_tick=9),
                       t["worker_injection"]["submissions"][0].update(worker_submit_tick=9)),
            lambda t: t["delivery_chain"].update(first_control_visible_tick=14),
            lambda t: t.update(motion_identity="other"),
            lambda t: t["samples"][0].update(motion_identity="other"),
            lambda t: t["worker_delivery"].append(deepcopy(t["delivery_chain"])),
            lambda t: t["worker_injection"]["submissions"].append(
                deepcopy(t["worker_injection"]["submissions"][0])),
            lambda t: t["delivery_chain"].update(result_take_tick=None),
            lambda t: t.update(damage_points=1),
            lambda t: t.update(source_released=False),
            lambda t: t.update(old_manual_intent_selected=True),
            lambda t: t.update(old_session_intent_selected=True),
            lambda t: t.update(terminal_state="failed"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                trial = completed_trial()
                mutation(trial)
                _, checks = probe._targeted_b10_summary([trial], runtime_state="ready")
                self.assertFalse(all(c["passed"] for c in checks))
        for trials, runtime in (([], "ready"), ([completed_trial()] * 2, "ready"),
                                ([completed_trial()], "failed")):
            _, checks = probe._targeted_b10_summary(trials, runtime_state=runtime)
            self.assertFalse(all(c["passed"] for c in checks))


if __name__ == "__main__":
    unittest.main()

import json
from pathlib import Path
import tempfile
import unittest


class D112DeliveryBaselineTests(unittest.TestCase):
    def test_artifact_summary_separates_control_cost_and_strict_window_misses(self):
        from scripts.analyze_d112_delivery_baseline import analyze_client_directory

        with tempfile.TemporaryDirectory() as temporary:
            client = Path(temporary)
            self._write_jsonl(client / "trace.jsonl", ({
                "record_type": "step",
                "payload": {
                    "decision_observation_received_ns": 1_000_000,
                    "dispatched_monotonic_ns": 3_000_000,
                },
            }, {
                "record_type": "step",
                "payload": {
                    "decision_observation_received_ns": 10_000_000,
                    "dispatched_monotonic_ns": 14_000_000,
                },
            }))
            self._write_jsonl(client / "b10-receiver-diagnostics.jsonl", ({
                "event": "worker_submit", "trial": "trial-a",
                "identity": "candidate-a", "movement_tick": 98,
            }, {
                "event": "receiver_observed_result", "trial": "trial-a",
                "identity": "candidate-a", "movement_tick": 99,
                "worker_elapsed_ns": 12_000_000,
                "monotonic_ns": 30_000_000,
                "worker_completed_monotonic_ns": 29_000_000,
            }, {
                "event": "registry_publish", "trial": "trial-a",
                "identity": "candidate-a", "movement_tick": 99,
            }, {
                "event": "first_control_consume", "trial": "trial-a",
                "identity": "candidate-a", "movement_tick": 100,
            }))
            self._write_jsonl(client / "b10-coordinator-control-frames.jsonl", ({
                "trial": "trial-a", "proposal_elapsed_ns": 2_000_000,
                "runtime_outside_backend_ns": 500_000,
                "verified_command_index": 0, "command_index": 0,
                "grant_movement_tick": 100,
                "motion_identity": "candidate-a",
                "control_sequence_id": 77,
                "expected_movement_tick": 101, "latest_movement_tick": 102,
                "actual_movement_ticks": [102],
            }, {
                "trial": "trial-a", "proposal_elapsed_ns": 1_000_000,
                "runtime_outside_backend_ns": 250_000,
                "verified_command_index": 1, "command_index": 1,
                "expected_movement_tick": 102, "latest_movement_tick": 102,
                "actual_movement_ticks": [102],
            }))

            report = analyze_client_directory(client)

        self.assertEqual(report["trace_control_prepare_ms"]["samples"], 2)
        self.assertEqual(report["trace_control_prepare_ms"]["maximum"], 4.0)
        self.assertEqual(
            report["coordinator_proposal_plus_runtime_outside_ms"]["values"],
            [2.5, 1.25],
        )
        delivery = report["formal_candidate_delivery"]
        self.assertEqual(delivery["trial_count"], 1)
        self.assertEqual(delivery["consume_to_earliest_tick_lead"]["values"], [1])
        self.assertEqual(delivery["first_input_to_latest_tick_margin"]["values"], [0])
        self.assertEqual(delivery["first_input_window_miss_count"], 0)
        self.assertEqual(delivery["all_verified_window_miss_count"], 0)
        self.assertEqual(delivery["exact_window_application_count"], 1)
        self.assertEqual(delivery["complete_identity_chain_count"], 1)
        self.assertEqual(delivery["trials"][0]["identity"], "candidate-a")
        self.assertEqual(delivery["trials"][0]["worker_submit_tick"], 98)
        self.assertEqual(delivery["trials"][0]["worker_complete_to_receiver_ns"], 1_000_000)
        self.assertEqual(delivery["trials"][0]["grant_movement_tick"], 100)
        self.assertEqual(delivery["trials"][0]["control_sequence_id"], 77)

    def test_final_command_identity_cannot_join_an_older_trial_consume(self):
        from scripts.analyze_d112_delivery_baseline import summarize_formal_delivery

        receiver = ({
            "event": "worker_submit", "trial": "trial", "identity": "old",
            "movement_tick": 5,
        }, {
            "event": "first_control_consume", "trial": "trial", "identity": "old",
            "movement_tick": 10,
        }, {
            "event": "worker_submit", "trial": "trial", "identity": "new",
            "movement_tick": 18,
        }, {
            "event": "receiver_observed_result", "trial": "trial", "identity": "new",
            "movement_tick": 19, "worker_elapsed_ns": 1,
            "worker_completed_monotonic_ns": 100, "monotonic_ns": 101,
        }, {
            "event": "registry_publish", "trial": "trial", "identity": "new",
            "movement_tick": 19,
        }, {
            "event": "first_control_consume", "trial": "trial", "identity": "new",
            "movement_tick": 20,
        })
        frames = ({
            "trial": "trial", "verified_command_index": 0,
            "motion_identity": "new", "control_sequence_id": 4,
            "grant_movement_tick": 20,
            "expected_movement_tick": 21, "latest_movement_tick": 22,
            "actual_movement_ticks": [21],
        },)

        report = summarize_formal_delivery(receiver, frames)

        self.assertEqual(report["consume_to_earliest_tick_lead"]["values"], [1])
        self.assertEqual(report["trials"][0]["identity"], "new")
        self.assertTrue(report["trials"][0]["complete_identity_chain"])

    def test_window_miss_is_strictly_later_than_latest(self):
        from scripts.analyze_d112_delivery_baseline import summarize_formal_delivery

        receiver = ({
            "event": "first_control_consume", "trial": "late",
            "identity": "candidate", "movement_tick": 20,
        },)
        frames = ({
            "trial": "late", "verified_command_index": 0, "command_index": 0,
            "expected_movement_tick": 21, "latest_movement_tick": 22,
            "actual_movement_ticks": [23],
        }, {
            "trial": "late", "verified_command_index": 1, "command_index": 1,
            "expected_movement_tick": 22, "latest_movement_tick": 22,
            "actual_movement_ticks": [22],
        })

        report = summarize_formal_delivery(receiver, frames)

        self.assertEqual(report["first_input_window_miss_count"], 1)
        self.assertEqual(report["all_verified_window_miss_count"], 1)
        self.assertEqual(report["exact_window_application_count"], 1)

    def test_fixed_calibration_is_report_only_and_has_stable_work_signature(self):
        from scripts.analyze_d112_delivery_baseline import run_calibration

        report = run_calibration(warmups=1, samples=2, rounds=2)

        self.assertEqual(report["purpose"], "reference_calibration_only")
        self.assertFalse(report["device_admission"])
        self.assertEqual(len(report["rounds"]), 2)
        signatures = {row["input_signature_sha256"] for row in report["rounds"]}
        workloads = {json.dumps(row["workload"], sort_keys=True)
                     for row in report["rounds"]}
        self.assertEqual(len(signatures), 1)
        self.assertEqual(len(workloads), 1)
        for row in report["rounds"]:
            self.assertEqual(row["wall_ms"]["samples"], 2)
            self.assertEqual(row["cpu_ms"]["samples"], 2)
            self.assertGreater(row["wall_ms"]["minimum"], 0)
            self.assertGreater(row["cpu_ms"]["minimum"], 0)

    @staticmethod
    def _write_jsonl(path: Path, rows) -> None:
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()

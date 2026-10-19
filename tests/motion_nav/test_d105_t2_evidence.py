"""Evidence helpers for the D105 T2 Windows timeline."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


class T2EvidenceTests(unittest.TestCase):
    def test_lf_hash_is_independent_of_worktree_newlines(self) -> None:
        from scripts.measure_sound_motion_t2_d105 import sha256_lf_bytes

        self.assertEqual(sha256_lf_bytes(b"first\r\nsecond\r\n"),
                         sha256_lf_bytes(b"first\nsecond\n"))

    def test_historical_index_recomputes_evidence_without_rebinding_sources(self) -> None:
        from scripts.measure_sound_motion_t2_d105 import verify_evidence_index

        index = ROOT / "evidence/motion_navigation/sound-motion-proof-d105-v1/t2/index.json"
        strict = verify_evidence_index(index)
        result = verify_evidence_index(index, require_current_sources=False)

        self.assertFalse(strict["passed"])
        self.assertIn(
            "T2 source hash mismatch: mc2p/motion_nav/motion_worker.py",
            strict["errors"],
        )
        self.assertTrue(result["passed"], result["errors"])
        self.assertGreaterEqual(result["evidence_file_count"], 15)
        self.assertEqual(result["verified_evidence_file_count"],
                         result["evidence_file_count"])

    def test_actual_receiver_timing_recomputes_without_new_measurement(self) -> None:
        from scripts import measure_sound_motion_t2_d105 as meter

        path = (ROOT / "evidence/motion_navigation/sound-motion-proof-d105-v1/t2"
                / "receiver-timing/index.json")
        with patch.object(meter, "measure_receiver_sample",
                          side_effect=AssertionError("recompute cannot time receiver")):
            result = meter.recompute_receiver_timing(path)

        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(result["sample_count"], 10)
        self.assertEqual(result["summary"], result["recorded_summary"])

    def test_actual_generation_commands_resolve_repository_python(self) -> None:
        from scripts.measure_sound_motion_t2_d105 import validate_generation_commands

        root_index = (ROOT / "evidence/motion_navigation/sound-motion-proof-d105-v1/t2"
                      / "index.json")
        timing_index = root_index.parent / "receiver-timing/index.json"
        result = validate_generation_commands(root_index)
        timing = json.loads(timing_index.read_text("utf-8"))

        self.assertTrue(result["passed"], result["errors"])
        self.assertEqual(timing["generation_command_argv"][0], ".\\.venv\\python.exe")
        if (ROOT / "EXPORT-METADATA.json").is_file():
            self.assertTrue(Path(sys.executable).is_file())
        else:
            self.assertTrue((ROOT / ".venv/python.exe").is_file())
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            (target / "receiver-timing").mkdir()
            changed = json.loads(root_index.read_text("utf-8"))
            changed["generation_commands"]["receiver_timing"] = (
                ".\\.venv\\python.exe -c other_work")
            (target / "index.json").write_text(json.dumps(changed), "utf-8")
            (target / "receiver-timing/index.json").write_bytes(
                timing_index.read_bytes())

            mutated = validate_generation_commands(target / "index.json")

        self.assertFalse(mutated["passed"])
        self.assertTrue(any("receiver timing command" in error for error in mutated["errors"]),
                        mutated["errors"])

    def test_standalone_export_uses_current_interpreter_for_recorded_repository_python(self) -> None:
        from scripts import measure_sound_motion_t2_d105 as meter

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "EXPORT-METADATA.json").write_text("{}\n", "utf-8")
            with patch.object(meter, "ROOT", root):
                self.assertTrue(meter._interpreter_is_available(meter.REPOSITORY_PYTHON))
                self.assertFalse(meter._interpreter_is_available(r".\.venv\other.exe"))

    def test_timeline_is_segmented_without_using_clock_as_a_gate(self) -> None:
        from scripts.measure_sound_motion_t2_d105 import extract_timeline

        raw = {
            "transport_events": [
                {"operation": "submit", "timestamp_ns": 10, "job_token": "job"},
                {"operation": "receive", "timestamp_ns": 20, "job_token": "job"},
            ],
            "native_events": [
                {"operation": "publish", "timestamp_ns": 30, "proof_token": "proof"},
                {"operation": "first_consume", "timestamp_ns": 40,
                 "proof_token": "proof"},
            ],
        }

        timeline = extract_timeline(raw)

        self.assertEqual(
            timeline["segments_ns"],
            {
                "submit_to_worker_result_received": 10,
                "worker_result_received_to_receiver_publish": 10,
                "receiver_publish_to_first_consume": 10,
                "submit_to_first_consume": 30,
            },
        )
        self.assertEqual(timeline["identity"], {"job_token": "job",
                                                   "proof_token": "proof"})
        self.assertTrue(timeline["complete"])
        self.assertEqual(timeline["gate_inputs"], [])

    def test_timeline_rejects_duplicate_or_out_of_order_events(self) -> None:
        from scripts.measure_sound_motion_t2_d105 import extract_timeline

        duplicate = {
            "transport_events": [
                {"operation": "submit", "timestamp_ns": 10, "job_token": "job"},
                {"operation": "receive", "timestamp_ns": 20, "job_token": "job"},
                {"operation": "receive", "timestamp_ns": 21, "job_token": "job"},
            ],
            "native_events": [
                {"operation": "publish", "timestamp_ns": 30, "proof_token": "proof"},
                {"operation": "first_consume", "timestamp_ns": 40,
                 "proof_token": "proof"},
            ],
        }
        out_of_order = {
            "transport_events": [
                {"operation": "submit", "timestamp_ns": 10, "job_token": "job"},
                {"operation": "receive", "timestamp_ns": 31, "job_token": "job"},
            ],
            "native_events": [
                {"operation": "publish", "timestamp_ns": 30, "proof_token": "proof"},
                {"operation": "first_consume", "timestamp_ns": 40,
                 "proof_token": "proof"},
            ],
        }

        self.assertFalse(extract_timeline(duplicate)["complete"])
        self.assertFalse(extract_timeline(out_of_order)["complete"])


if __name__ == "__main__":
    unittest.main()

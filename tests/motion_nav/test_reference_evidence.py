import hashlib
import json
from pathlib import Path
import tempfile
import unittest


def canonical_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


class ReferenceEvidenceTests(unittest.TestCase):
    def api(self):
        try:
            from mc2p.motion_nav.evidence import reference_evidence
        except ModuleNotFoundError:
            self.fail("motion navigation evidence normalizer is not implemented")
        return reference_evidence

    def make_evidence(self, root):
        evidence = root / "evidence"
        trajectory = evidence / "trajectory"
        trajectory.mkdir(parents=True)
        records = [
            {
                "schema_version": "mc2p.normal-navigation-record.v1",
                "record_type": "execution",
                "controller_ns": 100,
                "request_sequence_id": 7,
                "requested_action": {"forward": 1},
                "selected_action": {"forward": 1},
                "confirmed_execution": {"status": "executed", "request_sequence_id": 7},
            },
            {
                "schema_version": "mc2p.normal-navigation-record.v1",
                "record_type": "sample",
                "sample": {
                    "episode_id": "episode",
                    "sequence_id": 9,
                    "request_sequence_id": 7,
                    "position": [1.5, -60.0, 2.5],
                    "horizontal_speed_blocks_per_tick": 0.2,
                    "on_ground": True,
                    "controller_clock_id": "controller-a",
                    "request_started_at_ns": 110,
                    "received_at_ns": 150,
                    "client_sample": {
                        "clock_id": "jvm-b",
                        "started_at_ns": 900,
                        "completed_at_ns": 920,
                    },
                },
            },
        ]
        segment = b"".join(canonical_bytes(record) for record in records)
        segment_path = trajectory / "segment-00000000.jsonl"
        segment_path.write_bytes(segment)
        entry = {
            "schema_version": "mc2p.segment.v1",
            "index": 0,
            "filename": segment_path.name,
            "record_count": 2,
            "byte_count": len(segment),
            "first_record_ordinal": 0,
            "last_record_ordinal": 1,
            "sha256": hashlib.sha256(segment).hexdigest(),
        }
        manifest = canonical_bytes(entry)
        (trajectory / "manifest.jsonl").write_bytes(manifest)
        (trajectory / "complete.json").write_text(
            json.dumps(
                {
                    "schema_version": "mc2p.segment-complete.v1",
                    "segment_count": 1,
                    "record_count": 2,
                    "byte_count": len(segment),
                    "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        (evidence / "run-manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": "mc2p.normal-navigation-run.v2",
                    "source_archive": {"tree_sha256": "f" * 64, "file_count": 2},
                    "case_plan": {"case": "fixture_case"},
                }
            ),
            encoding="utf-8",
        )
        (evidence / "terminal.json").write_text(
            json.dumps({"driver_state": "success", "driver_reason": "point_goal_reached"}),
            encoding="utf-8",
        )
        return evidence

    def test_normalizes_inputs_body_and_separate_clock_durations(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = self.make_evidence(root)
            with self.assertRaisesRegex(ValueError, "outside raw evidence"):
                api.normalize_legacy_evidence(
                    evidence,
                    evidence / "normalized",
                    version_id="v",
                    scene_id="S00",
                )
            output = root / "normalized"
            summary = api.normalize_legacy_evidence(
                evidence,
                output,
                version_id="hierarchical-current",
                scene_id="S04",
            )
            self.assertEqual(summary["counts"], {"inputs": 1, "body": 1, "timings": 1})
            self.assertEqual(set(p.name for p in output.iterdir()), {"inputs.jsonl", "body.jsonl", "timings.jsonl", "run.json"})
            timing = json.loads((output / "timings.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(timing["controller_observation_delivery_ns"], 40)
            self.assertEqual(timing["client_sampling_ns"], 20)
            self.assertEqual(timing["controller_clock_id"], "controller-a")
            self.assertEqual(timing["client_clock_id"], "jvm-b")
            self.assertNotIn("cross_clock_latency_ns", timing)
            run = json.loads((output / "run.json").read_text(encoding="utf-8"))
            self.assertEqual(run["version_id"], "hierarchical-current")
            self.assertEqual(run["scene_id"], "S04")
            self.assertEqual(run["source_case_id"], "fixture_case")
            self.assertEqual(run["source_archive"]["tree_sha256"], "f" * 64)

    def test_rejects_existing_destination_and_corrupt_source(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = self.make_evidence(root)
            output = root / "normalized"
            output.mkdir()
            with self.assertRaises(FileExistsError):
                api.normalize_legacy_evidence(evidence, output, version_id="v", scene_id="S00")
            output.rmdir()
            segment = evidence / "trajectory" / "segment-00000000.jsonl"
            segment.write_bytes(segment.read_bytes() + b"{}\n")
            with self.assertRaises(ValueError):
                api.normalize_legacy_evidence(evidence, output, version_id="v", scene_id="S00")
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

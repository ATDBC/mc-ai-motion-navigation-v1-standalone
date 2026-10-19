"""Historical direct-copy failure is immutable; current messages have new gates."""
from pathlib import Path
import hashlib
import json
import unittest

from scripts.export_motion_navigation_standalone import load_manifest


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence/motion_navigation/d121-momentum-handoff-v1/s2/stop.json"


class D121S2StopTests(unittest.TestCase):
    def test_historical_stop_bytes_are_preserved(self):
        raw = EVIDENCE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),
            "fb45e9c21e1bad9c54ea0a4f1aeedd680b5781f3bbe7cfad6ee1b10265bdf667")
        self.assertGreater(json.loads(raw)["direct_copy_pickle_bytes"], 2097152)

    def test_current_formal_messages_meet_the_half_limit(self):
        summary = json.loads((ROOT / "evidence/motion_navigation/d121-momentum-handoff-v1/"
                              "task-3-preparation/repair/summary.json").read_text("utf-8"))
        self.assertEqual(summary["hard_limit_bytes"], 2097152)
        self.assertEqual(len(summary["directions"]), 4)
        for row in summary["directions"]:
            self.assertEqual(set(row["sizes_bytes"]), {
                "current_result_canonical", "current_result_pickle5", "prepared_job_canonical",
                "prepared_job_pickle5", "prepared_job_queue_sender", "bundle_canonical",
                "wire_protocol5_payload"})
            for name, size in row["sizes_bytes"].items():
                with self.subTest(direction=row["direction_index"], field=name):
                    self.assertLessEqual(size, 1048576)

    def test_historical_stop_and_current_preparation_are_required_exports(self):
        required = {path.as_posix() for path in load_manifest().required_paths}
        self.assertIn("evidence/motion_navigation/d121-momentum-handoff-v1/s2/stop.json", required)
        self.assertIn("tests/motion_nav/test_d121_successor_binding.py", required)


if __name__ == "__main__":
    unittest.main()

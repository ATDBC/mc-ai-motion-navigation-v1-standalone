"""D121-S2 stopped-message evidence; production joint implementation is absent."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from mc2p.motion_nav import prepared_motion
from mc2p.motion_nav.prepared_motion_proof import MAXIMUM_MESSAGE_BYTES
from scripts.d121_joint_worker_stop_probe import collect_stop_facts
from scripts.export_motion_navigation_standalone import load_manifest


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = (ROOT / "evidence/motion_navigation/d121-momentum-handoff-v1"
            / "s2/stop.json")


class D121S2StopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.actual = collect_stop_facts()

    def test_test_side_direct_copy_pickle_exceeds_existing_message_limit(self):
        self.assertLess(self.actual["direct_copy_canonical_bytes"], MAXIMUM_MESSAGE_BYTES)
        self.assertGreater(self.actual["direct_copy_canonical_limit_ratio"], .90)
        self.assertGreater(self.actual["direct_copy_pickle_bytes"], MAXIMUM_MESSAGE_BYTES)
        self.assertGreater(self.actual["direct_copy_pickle_limit_ratio"], 1.)
        self.assertGreater(
            self.actual["parts"]["joint_fact_canonical_bytes"],
            self.actual["parts"]["successor_proof_canonical_bytes"],
        )

    def test_injected_budget_error_maps_to_typed_failure_without_ready(self):
        worker = self.actual["injected_budget_failure_mapping"]
        self.assertEqual(worker["wire_schema"],
                         "mc2p.prepared-motion-compute-failure.v1")
        self.assertEqual(worker["compute_status"], "budget_exhausted")
        self.assertEqual(worker["admission_status"], "stale")
        self.assertFalse(worker["ready"])
        self.assertEqual(worker["ready_handle_count"], 0)
        self.assertIsNone(self.actual["background_ready_margin_ticks"])
        self.assertFalse(self.actual["method"]["production_joint_message_executed"])
        self.assertIn("mapping", self.actual["measurement_boundary"]["worker_status"])

    def test_temporary_joint_production_types_remain_withdrawn(self):
        self.assertFalse(hasattr(prepared_motion, "PreparedIncumbentMotion"))
        self.assertFalse(hasattr(prepared_motion, "PreparedJointMotionProof"))
        self.assertEqual(
            self.actual["history"],
            {
                "s0": "complete",
                "s1": "complete",
                "s2": "stopped",
                "s3": "not_run",
                "s4": "not_run",
                "temporary_production_implementation": "withdrawn",
            },
        )

    def test_checked_in_stop_evidence_matches_reproduction(self):
        recorded = json.loads(EVIDENCE.read_text(encoding="utf-8"))
        volatile = {"direct_copy_pickle_bytes", "direct_copy_pickle_limit_ratio"}
        self.assertEqual(
            {key: value for key, value in recorded.items() if key not in volatile},
            {key: value for key, value in self.actual.items() if key not in volatile},
        )
        self.assertGreater(recorded["direct_copy_pickle_bytes"], MAXIMUM_MESSAGE_BYTES)
        self.assertGreater(self.actual["direct_copy_pickle_bytes"], MAXIMUM_MESSAGE_BYTES)
        self.assertEqual(EVIDENCE.read_bytes()[-1:], b"\n")
        self.assertNotIn(b"\r", EVIDENCE.read_bytes())

    def test_stop_script_test_and_evidence_are_required_exports(self):
        manifest = load_manifest()
        required = {path.as_posix() for path in manifest.required_paths}
        for relative in (
            "scripts/d121_joint_worker_stop_probe.py",
            "tests/motion_nav/test_d121_joint_worker_stop.py",
            "evidence/motion_navigation/d121-momentum-handoff-v1/s2/stop.json",
            "evidence/motion_navigation/d121-momentum-handoff-v1/"
            "published-production-semantic-hashes.json",
        ):
            self.assertIn(relative, required)


if __name__ == "__main__":
    unittest.main()

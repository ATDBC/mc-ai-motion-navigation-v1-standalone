"""S0-R collectors preserve separate behavior and path evidence."""
import tempfile
import unittest
from pathlib import Path
from scripts.navigation_s0r_evidence import case_jobs, collect
from scripts.navigation_structure_paths import path_probe


class S0REvidenceTests(unittest.TestCase):
    def test_frozen_denominators_and_unique_case_names(self):
        for group, expected in (("product", 2000), ("coordination", 1448),
                                ("faults", 16), ("follow", 10), ("world_changes", 19)):
            jobs = list(case_jobs(group))
            self.assertEqual(len(jobs), expected)
            self.assertEqual(len({job[0] for job in jobs}), expected)

    def test_evidence_refuses_overwrite_and_incomplete_manifest_references(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(FileExistsError):
                collect("faults", Path(root), workers=1, representative=True)

        import hashlib
        import json
        from scripts.navigation_structure_baseline import verify_manifest
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "index.json"
            evidence.write_bytes(b"{}")
            digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
            manifest = {"sets": {name: {"index": "index.json", "index_sha256": digest,
                                       "repeat_index": "index.json", "repeat_index_sha256": digest}
                                 for name in ("product", "coordination", "faults", "follow", "world_changes")},
                        "path_matrix_sha256": digest, "deletion_inventory_sha256": digest,
                        "compact_summary_files": {"summary.json": digest}}
            for name in ("path-matrix.json", "deletion-inventory.json", "summary.json"):
                (root / name).write_bytes(b"{}")
            (root / "baseline-manifest.json").write_text(json.dumps(manifest))
            self.assertTrue(verify_manifest(root)["verified"])
            manifest["compact_summary_files"] = {"missing-summary.json": digest}
            (root / "baseline-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(FileNotFoundError):
                verify_manifest(root)
            manifest["compact_summary_files"] = {"summary.json": "0" * 64}
            (root / "baseline-manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):
                verify_manifest(root)

    def test_path_probe_records_real_replay_results_and_restores_bindings(self):
        from mc2p.motion_nav import route_admission
        from tests.sim.s0r_world_changes import run_world_change
        original = route_admission.replay_walk_validation_recipe
        with path_probe() as counts:
            run_world_change(follow=False, edit_tick=10, material="grass")
            run_world_change(follow=False, edit_tick=10, material="obstacle")
        self.assertIs(route_admission.replay_walk_validation_recipe, original)
        self.assertGreater(counts["route_validation.replay_walk_validation_recipe:feasible"], 0)
        self.assertGreater(counts["route_validation.replay_walk_validation_recipe:blocked"], 0)

    def test_path_collection_never_calls_behavior_signer(self):
        from unittest.mock import patch
        from scripts.navigation_structure_paths import _count_job
        job = next(job for job in case_jobs("world_changes") if job[0] == "world/point/10/grass")
        with patch("scripts.navigation_s0r_evidence.structure_signature",
                   side_effect=AssertionError("path run attempted to sign behavior")):
            group, identifier, counts = _count_job(("world_changes", job))
        self.assertGreater(counts["route_validation.replay_walk_validation_recipe"], 0)

    def test_behavior_signatures_repeat_without_path_probe(self):
        from scripts.navigation_s0r_evidence import execute_case
        job = next(job for job in case_jobs("faults") if job[1] == "io")
        self.assertEqual(execute_case(job)["signature"], execute_case(job)["signature"])

    def test_inventory_has_review_fields_and_unexecuted_mutations(self):
        from scripts.navigation_structure_inventory import SPECS, build_inventory
        matrix = {"candidates": {spec[0]: {group: 0 for group in
                    ("product", "coordination", "faults", "follow", "world_changes")} for spec in SPECS}}
        value = build_inventory(matrix)
        self.assertEqual({item["clue"] for item in value["candidates"]}.intersection(
            {"K1", "K2", "K3", "K4", "K5", "K6"}), {"K1", "K2", "K3", "K4", "K5", "K6"})
        for item in value["candidates"]:
            self.assertTrue({"category", "duplicate_of", "legacy_of", "kept_owner", "entering_sets",
                             "detecting_check", "expected_delta", "risk", "closure"}.issubset(item))
            self.assertEqual(item["detecting_check"]["status"], "planned_not_executed")
            self.assertNotIn(item["closure"], ("deleted", "merged"))

    def test_owned_verification_changes_each_affect_the_signature(self):
        from copy import deepcopy
        from unittest.mock import patch
        from scripts.navigation_s0r_evidence import execute_case
        raw = {"passed": True, "task_outcome": "failed", "reason": "bounded_fixture",
               "violations": [], "trace": [],
               "verification": {"status": "verified", "coverage": {
                    "planning": {"begin": 1, "apply": 1, "finish": 1}}, "gaps": []}}
        job = ("interrupt/verification-probe", "interrupt", {"fixture": "verification"})
        with patch("scripts.navigation_migration_evidence._execute", return_value=raw):
            reference = execute_case(job)["signature"]
        for field in ("status", "coverage", "gaps"):
            changed = deepcopy(raw)
            if field == "status":
                changed["verification"][field] = "unverified"
            elif field == "coverage":
                changed["verification"][field]["planning"]["finish"] = 0
            else:
                changed["verification"][field] = ["missing finish evidence"]
            with self.subTest(field=field), patch(
                    "scripts.navigation_migration_evidence._execute", return_value=changed):
                self.assertNotEqual(reference, execute_case(job)["signature"])

    def test_signature_schema_mismatch_cannot_compare_as_equal(self):
        from scripts.navigation_structure_baseline import compare_indexes
        basis = {"kind": "coordination", "normalizer_version": "r28-structure-trajectory-v1", "cases": []}
        changed = {**basis, "signature_schema_version": "mc2p.s0r-owned-verification.v2"}
        with self.assertRaises(ValueError):
            compare_indexes(basis, changed)

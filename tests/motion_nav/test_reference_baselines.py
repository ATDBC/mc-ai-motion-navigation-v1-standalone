import hashlib
import json
from pathlib import Path
import tempfile
import unittest


class ReferenceBaselineTests(unittest.TestCase):
    def api(self):
        try:
            from mc2p.motion_nav.evidence import reference_baselines
        except ModuleNotFoundError:
            self.fail("motion navigation reference catalog is not implemented")
        return reference_baselines

    def test_repository_catalog_keeps_three_distinct_references_and_four_scenes(self):
        api = self.api()
        catalog = api.load_reference_catalog(
            Path("config/motion-navigation/reference-versions-v1.json"),
            Path("config/motion-navigation/reference-scenes-v1.json"),
        )
        self.assertEqual(
            set(catalog.versions),
            {"pre-floating-7ab0b35f", "overlap-air-legacy", "hierarchical-current"},
        )
        self.assertEqual(set(catalog.scenes), {"S00", "S02", "S04", "S05"})
        self.assertEqual(
            catalog.versions["pre-floating-7ab0b35f"].role,
            "motion_performance_reference",
        )
        self.assertEqual(
            catalog.versions["overlap-air-legacy"].role,
            "knowledge_semantics_reference",
        )
        self.assertEqual(
            catalog.versions["hierarchical-current"].role,
            "planning_lifecycle_reference",
        )
        self.assertEqual(len({v.full_source_tree_sha256 for v in catalog.versions.values()}), 3)
        self.assertTrue(all(v.promoted_as_new_base is False for v in catalog.versions.values()))
        self.assertEqual(api.verify_scene_sources(catalog, Path(".")), 1)
        trees = api.verify_version_snapshot_trees(
            catalog, Path("config/motion-navigation/reference-SHA256SUMS.txt")
        )
        self.assertEqual(trees, {
            "pre-floating-7ab0b35f": 88,
            "overlap-air-legacy": 55,
            "hierarchical-current": 18,
        })

    def test_catalog_rejects_duplicate_ids_and_unsafe_paths(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            versions = {
                "schema_version": "mc2p.motion-navigation-reference-versions.v1",
                "published_repository": {"commit": "a" * 40},
                "versions": [
                    self.version("same", "first"),
                    self.version("same", "../escape"),
                ],
            }
            scenes = {
                "schema_version": "mc2p.motion-navigation-reference-scenes.v1",
                "scenes": [self.scene("S00")],
            }
            vp = root / "versions.json"
            sp = root / "scenes.json"
            vp.write_text(json.dumps(versions), encoding="utf-8")
            sp.write_text(json.dumps(scenes), encoding="utf-8")
            with self.assertRaises(ValueError):
                api.load_reference_catalog(vp, sp)

    def test_snapshot_verification_detects_hash_mismatch_and_unexpected_files(self):
        api = self.api()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = root / "version" / "code.py"
            payload.parent.mkdir()
            payload.write_text("ok\n", encoding="utf-8")
            digest = hashlib.sha256(payload.read_bytes()).hexdigest()
            sums = root / "expected.txt"
            sums.write_text(f"{digest}  version/code.py\n", encoding="utf-8")
            report = api.verify_snapshot(root, sums)
            self.assertEqual(report.file_count, 1)
            self.assertTrue(report.ok)

            external = root.parent / f"{root.name}-external-sums.txt"
            external.write_bytes(sums.read_bytes())
            copied = root / "SHA256SUMS.txt"
            copied.write_bytes(sums.read_bytes())
            sums.unlink()
            self.assertTrue(api.verify_snapshot(root, external).ok)
            external.unlink()
            copied.unlink()

            sums.write_text(f"{digest}  version/code.py\n", encoding="utf-8")

            payload.write_text("changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                api.verify_snapshot(root, sums)

            payload.write_text("ok\n", encoding="utf-8")
            (root / "extra.txt").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unexpected file"):
                api.verify_snapshot(root, sums)

    @staticmethod
    def version(version_id, snapshot_path):
        return {
            "id": version_id,
            "role": "motion_performance_reference",
            "snapshot_path": snapshot_path,
            "report_path": "reports/report.md",
            "full_source_tree_sha256": "1" * 64,
            "promoted_as_new_base": False,
            "existing_evidence": [],
        }

    @staticmethod
    def scene(scene_id):
        return {
            "id": scene_id,
            "name": "scene",
            "purpose": "test",
            "identity": {"kind": "generated", "case": "empty", "seed": 21001},
        }


if __name__ == "__main__":
    unittest.main()

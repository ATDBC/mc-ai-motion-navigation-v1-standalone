import hashlib
from pathlib import Path
import tempfile
import unittest
import json


class IncrementalMotionTableTests(unittest.TestCase):
    def test_frozen_table_has_declared_origin_and_loads_all_rows(self):
        from experiments.motion_navigation.trajectory_proto.motion_table import (
            FROZEN_TABLE_PATH,
            FROZEN_TABLE_SHA256,
            load_frozen_table,
        )

        raw = FROZEN_TABLE_PATH.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), FROZEN_TABLE_SHA256)
        table = load_frozen_table()
        self.assertEqual(len(table.rows), 147)
        self.assertEqual(table.source_commit, "f5e43e74")
        self.assertEqual(table.params("gap1", "rest-C")[0].ground_gait, "S")

    def test_changed_bytes_or_expected_identity_are_rejected(self):
        from experiments.motion_navigation.trajectory_proto.motion_table import (
            FrozenTableError,
            FROZEN_TABLE_PATH,
            load_table,
        )

        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "table.json"
            changed.write_bytes(FROZEN_TABLE_PATH.read_bytes() + b"\n")
            with self.assertRaises(FrozenTableError):
                load_table(changed)
        with self.assertRaises(FrozenTableError):
            load_table(FROZEN_TABLE_PATH, expected_identity={"ruleset_id": "wrong"})

    def test_module_has_no_review_or_tmp_runtime_dependency(self):
        from experiments.motion_navigation.trajectory_proto import motion_table

        source = Path(motion_table.__file__).read_text(encoding="utf-8")
        self.assertNotIn("reviews", source)
        self.assertNotIn(".tmp", source)

    def test_import_manifest_matches_every_project_owned_frozen_file(self):
        from experiments.motion_navigation.trajectory_proto.motion_table import FROZEN_TABLE_PATH

        root = FROZEN_TABLE_PATH.parent
        manifest = json.loads((root / "d098-p1-import-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["source_commit"], "f5e43e74")
        for row in manifest["files"]:
            raw = (root / row["path"]).read_bytes()
            self.assertEqual(len(raw), row["bytes"], row["path"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), row["sha256"], row["path"])


if __name__ == "__main__":
    unittest.main()

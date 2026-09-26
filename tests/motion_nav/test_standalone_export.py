from __future__ import annotations

from pathlib import Path
import shutil
import unittest
import uuid

from scripts.export_motion_navigation_standalone import (
    ExportViolation,
    export_tree,
    load_manifest,
    verify_tree,
)


ROOT = Path(__file__).resolve().parents[2]


class StandaloneExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = ROOT / ".tmp" / f"standalone-export-test-{uuid.uuid4().hex}"
        cls.manifest = load_manifest()
        export_tree(cls.root, clean=True, manifest=cls.manifest)

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.root.exists():
            shutil.rmtree(cls.root)

    def test_real_export_has_a_closed_hash_manifest(self) -> None:
        report = verify_tree(self.root, manifest=self.manifest)

        self.assertGreater(report.file_count, 100)
        self.assertEqual(report.missing_files, ())
        self.assertEqual(report.extra_files, ())
        self.assertEqual(report.changed_files, ())
        self.assertTrue((self.root / "README.md").is_file())
        self.assertTrue((self.root / "requirements.txt").is_file())
        attributes = set(
            (self.root / ".gitattributes").read_text("utf-8").splitlines()
        )
        self.assertIn("* -text whitespace=cr-at-eol,-blank-at-eof", attributes)
        self.assertIn(
            "deployment/surface-depth-diagnostic/native/vendor/** -whitespace",
            attributes,
        )

    def test_reviewed_missing_dependencies_are_real_export_files(self) -> None:
        required = (
            "tests/test_navigation_motion.py",
            "tests/test_visible_equipment_projection.py",
            "scripts/b12b_partial_combat_runtime.py",
            "tests/test_b12b_partial_combat_runtime.py",
            "tests/test_b12b_runtime_injection_acceptance.py",
        )

        self.assertEqual(
            tuple(path for path in required if not (self.root / path).is_file()),
            (),
        )
        requirements = (self.root / "requirements.txt").read_text("utf-8")
        self.assertIn("psutil", requirements)

    def test_verifier_rejects_changed_and_extra_files(self) -> None:
        target = self.root / "README.md"
        original = target.read_bytes()
        extra = self.root / "unexpected.txt"
        try:
            target.write_bytes(original + b"\nchanged\n")
            extra.write_text("unexpected", encoding="utf-8")
            with self.assertRaises(ExportViolation) as raised:
                verify_tree(self.root, manifest=self.manifest)
            self.assertIn("README.md", str(raised.exception))
            self.assertIn("unexpected.txt", str(raised.exception))
        finally:
            target.write_bytes(original)
            extra.unlink(missing_ok=True)

    def test_verifier_ignores_generated_python_cache(self) -> None:
        cache = self.root / "mc2p" / "__pycache__"
        compiled = cache / "generated.cpython-311.pyc"
        cache.mkdir(exist_ok=True)
        compiled.write_bytes(b"generated")
        try:
            report = verify_tree(self.root, manifest=self.manifest)
            self.assertEqual(report.extra_files, ())
        finally:
            compiled.unlink(missing_ok=True)
            try:
                cache.rmdir()
            except OSError:
                pass

    def test_clean_guard_rejects_workspace_and_paths_outside_tmp(self) -> None:
        with self.assertRaises(ExportViolation):
            export_tree(ROOT, clean=True, manifest=self.manifest)
        with self.assertRaises(ExportViolation):
            export_tree(ROOT.parent / "standalone-export", clean=True,
                        manifest=self.manifest)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import unittest
import uuid

from scripts.export_motion_navigation_standalone import (
    ExportViolation,
    export_tree,
    load_manifest,
    verify_tree,
    _allowed_source,
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
        self.assertIn("numpy", requirements)

    def test_m0_m1_snapshot_contains_reproduction_tools_and_compact_evidence(self):
        required = (
            'mc2p/motion_nav/actions/contracts.py',
            'mc2p/motion_nav/actions/registry.py',
            'mc2p/motion_nav/actions/controlled_drop.py',
            'scripts/navigation_design_metrics.py',
            'scripts/navigation_dead_path_probe.py',
            'scripts/navigation_historical_seed_probe.py',
            'tests/motion_nav/test_action_specs.py',
            'tests/motion_nav/test_action_spec_integration.py',
            'tests/motion_nav/action_spec_fixtures.py',
            'docs/motion_navigation/architecture/action-spec-v1.md',
            'docs/motion_navigation/decisions/0072-end-post-f1-cleanup-and-validate-action-spec.md',
            'docs/motion_navigation/stages/motion-navigation-middle-layer-M0-M1-plan.md',
            'docs/motion_navigation/acceptance/motion-navigation-middle-layer-M0-M1.md',
            'evidence/motion_navigation/redesign-m0/baseline-manifest.json',
            'evidence/motion_navigation/redesign-m1/final-manifest.json',
        )
        self.assertEqual(tuple(p for p in required if not (self.root/p).is_file()), ())
        self.assertFalse((self.root/'docs/superpowers').exists())
        for name in ('README.md', 'AGENTS.md'):
            text = (self.root/name).read_text('utf-8')
            self.assertIn('M1', text)
            self.assertIn('结构止损', text)
            self.assertIn('M2', text)
        self.assertFalse(any('.tmp' in p.relative_to(self.root).parts
                             for p in self.root.rglob('*') if p.is_file()))
        for directory in ('redesign-m0', 'redesign-m1'):
            self.assertTrue(all(p.stat().st_size < 1_000_000 for p in
                (self.root/'evidence/motion_navigation'/directory).rglob('*') if p.is_file()))

    def test_workspace_tmp_cannot_be_selected_as_export_source(self):
        self.assertFalse(_allowed_source(self.root/'README.md'))

    def test_f2_export_has_current_entrypoints_and_preserves_final_evidence(self):
        required = (
            "scripts/f2rec_r0_baseline.py",
            "scripts/f2rec_r0_v9.py",
            "scripts/f2_ground_route_evidence.py",
            "scripts/f2_ground_route_quality.py",
            "scripts/f2_ground_route_runtime.py",
            "scripts/f2r_piecewise_evidence.py",
            "tests/sim/manifests/navigation-product-r28-v8.json",
            "tests/motion_nav/test_f2r_piecewise_completion.py",
            "evidence/motion_navigation/action-spec-hardening-v1/metrics-baseline-corrected.json",
            "evidence/motion_navigation/F2R-piecewise-completion-v1/red/geometry/summary.json",
            "evidence/motion_navigation/F2-ground-route-v1/baseline/measurement-v2/error-copies.json",
            "evidence/motion_navigation/F2-ground-route-v1/final/windows/forward-final.json",
            "evidence/motion_navigation/F2-ground-route-v1/final/windows/reverse-final.json",
            "evidence/motion_navigation/F2-ground-route-v1/final/fabric/accepted-batches.json",
            "evidence/motion_navigation/F2-ground-route-v1/final/fabric/trials.jsonl",
            "evidence/motion_navigation/F2-ground-route-v1/final/fabric/external-force-earlier-bounded-result.json",
            "evidence/motion_navigation/F2-ground-route-v1/final/fabric/failures/corner-replay-red.json",
            "evidence/motion_navigation/F2REC-recovery-v1/r0/f2r-production-semantic-hashes.json",
        )
        self.assertEqual(tuple(p for p in required if not (self.root/p).is_file()), ())
        self.assertFalse((self.root/'docs/superpowers/plans/2026-10-07-non-center-ground-route-execution.md').exists())
        for name in ("README.md", "AGENTS.md"):
            text = (self.root/name).read_text("utf-8")
            self.assertIn("F2", text)
            self.assertIn("冻结范围验收通过", text)
            self.assertIn("1998/2000", text)
            self.assertIn("93/93", text)
            self.assertIn("F2-non-center-ground-route-execution.md", text)
        source = ROOT / "evidence/motion_navigation/F2-ground-route-v1/final"
        exported = self.root / "evidence/motion_navigation/F2-ground-route-v1/final"
        for path in source.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts:
                self.assertEqual((exported/path.relative_to(source)).read_bytes(), path.read_bytes())

    def test_f2_export_matches_frozen_production_fingerprints(self):
        source = self.root / "evidence/motion_navigation/F2R-piecewise-completion-v1/source-consistency.json"
        report = json.loads(source.read_text("utf-8"))
        canonical = json.loads((
            ROOT / "evidence/motion_navigation/F2REC-recovery-v1/r0/"
            "f2r-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        r1 = json.loads((
            ROOT / "evidence/motion_navigation/F2REC-recovery-v1/r1/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        r2 = json.loads((
            ROOT / "evidence/motion_navigation/F2REC-recovery-v1/r2-revalidation/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        r3 = json.loads((
            ROOT / "evidence/motion_navigation/F2REC-recovery-v1/r3/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        for name, expected in report["full_production_files"].items():
            raw = (self.root/name).read_bytes()
            lf = raw.replace(b"\r\n", b"\n")
            raw_hash = hashlib.sha256(raw).hexdigest()
            if raw_hash != expected:
                self.assertEqual(
                    hashlib.sha256(lf).hexdigest(),
                    r3.get(name, r2.get(name, r1.get(name, canonical[name]))),
                    name,
                )

    def test_export_has_no_local_artifacts_credentials_or_worlds(self):
        forbidden_parts = {".tmp", "artifacts", ".venv", ".gradle", "world", "worlds", "saves"}
        forbidden_names = {".env", "credentials.json", "secrets.json", "launcher_accounts.json", "level.dat"}
        for path in self.root.rglob("*"):
            if path.is_file():
                relative = path.relative_to(self.root)
                self.assertFalse(forbidden_parts.intersection(relative.parts), relative)
                self.assertNotIn(path.name, forbidden_names, relative)

    def test_exported_first_party_modules_all_import(self) -> None:
        script = """
import importlib
from pathlib import Path

root = Path.cwd()
modules = []
for prefix in ("mc2p", "scripts", "tools"):
    for path in sorted((root / prefix).rglob("*.py")):
        relative = path.relative_to(root).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        if parts:
            modules.append(".".join(parts))
for module in modules:
    importlib.import_module(module)
print(f"IMPORTED_FIRST_PARTY_MODULES={len(modules)}")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(
            result.returncode, 0, result.stdout + result.stderr,
        )
        self.assertIn("IMPORTED_FIRST_PARTY_MODULES=", result.stdout)

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

    def test_nested_evidence_hash_manifest_is_protected_by_snapshot_hashes(self):
        target = self.root/'evidence/motion_navigation/representative-v1/SHA256SUMS.txt'
        original = target.read_bytes()
        try:
            target.write_bytes(original+b'\ntampered inner manifest\n')
            with self.assertRaises(ExportViolation) as raised:
                verify_tree(self.root, manifest=self.manifest)
            self.assertIn('representative-v1/SHA256SUMS.txt', str(raised.exception))
        finally:
            target.write_bytes(original)

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

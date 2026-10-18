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

    def test_d113_read_only_baseline_is_a_required_export_file(self):
        relative = "evidence/motion_navigation/d113-control-visible-v1/d112-read-only-baseline.json"
        self.assertIn("evidence/motion_navigation/d113-control-visible-v1/**/*", self.manifest.include_globs)
        self.assertIn(relative, {path.as_posix() for path in self.manifest.required_paths})
        self.assertEqual((self.root / relative).read_bytes(), (ROOT / relative).read_bytes())

    def test_d114_restores_failed_trajectory_reports_as_required_export_files(self):
        relatives = (
            ".superpowers/sdd/2026-10-10-continuous-short-trajectory-prototype/"
            "task-5a-r1-report.md",
            ".superpowers/sdd/2026-10-10-continuous-short-trajectory-prototype/"
            "task-5a-r2-plan.md",
            ".superpowers/sdd/2026-10-10-continuous-short-trajectory-prototype/"
            "task-5a-r2-report.md",
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        for relative in relatives:
            with self.subTest(relative=relative):
                self.assertIn(relative, required)
                self.assertEqual(
                    (self.root / relative).read_bytes(),
                    (ROOT / relative).read_bytes(),
                )

        originals = {relative: (self.root / relative).read_bytes() for relative in relatives}
        try:
            for relative in relatives:
                (self.root / relative).unlink()
            with self.assertRaises(ExportViolation) as raised:
                verify_tree(self.root, manifest=self.manifest)
            for relative in relatives:
                self.assertIn(relative, str(raised.exception))
        finally:
            for relative, contents in originals.items():
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(contents)

    def test_d113_historical_semantics_are_exported_without_claiming_current_bytes(self):
        relative = "evidence/motion_navigation/d113-control-visible-v1/published-production-semantic-hashes.json"
        self.assertIn(relative, {path.as_posix() for path in self.manifest.required_paths})
        report = json.loads((self.root / relative).read_text("utf-8"))
        self.assertEqual(report["normalization"], "CRLF_TO_LF")
        self.assertTrue({
            "mc2p/motion_nav/motion_coordination.py",
            "mc2p/motion_nav/motion_solver.py",
            "mc2p/motion_nav/motion_worker.py",
            "scripts/analyze_d113_delivery_baseline.py",
            "scripts/b10_gap_solver_runtime.py",
            "deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/DeploymentObservationProbe.java",
            "deployment/fabric-observation-probe/src/main/java/com/mc2p/deployment/ProbeFailureDiagnostics.java",
        } <= report["files"].keys())
        for expected in report["files"].values():
            self.assertRegex(expected, r"^[0-9a-f]{64}$")
        if not (ROOT / ".git").exists():
            return
        commit = report["source_commit"]
        if subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                          cwd=ROOT, capture_output=True).returncode != 0:
            return
        for name, expected in report["files"].items():
            raw = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT)
            self.assertEqual(hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
                             expected, (commit, name))

    def test_d114_historical_semantics_remain_bound_to_the_published_commit(self):
        relative = "evidence/motion_navigation/d114-formal-driver-v1/published-production-semantic-hashes.json"
        self.assertIn(relative, {path.as_posix() for path in self.manifest.required_paths})
        report = json.loads((self.root / relative).read_text("utf-8"))
        self.assertEqual(report["schema"], "mc2p.public-production-semantic-hashes.v1")
        self.assertEqual(report["normalization"], "CRLF_TO_LF")
        self.assertEqual(report["base_commit"], "d077629fdd238d495d67ce312d8be2f91122e18a")
        self.assertEqual(report["source_commit"], "2ca3bd4d95b265e987e49df28605cf552cdf7b91")
        self.assertEqual(set(report["files"]), {
            "mc2p/skills/navigation_session_driver.py",
            "scripts/analyze_d113_delivery_baseline.py",
            "scripts/analyze_d114_formal_driver_run.py",
            "scripts/b10_gap_solver_runtime.py",
        })
        if not (ROOT / ".git").exists():
            return
        commit = report["source_commit"]
        if subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                          cwd=ROOT, capture_output=True).returncode != 0:
            return
        for name, expected in report["files"].items():
            raw = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT)
            self.assertEqual(hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
                             expected, (commit, name))

    def test_d115_historical_semantics_remain_bound_to_the_published_commit(self):
        relative = "evidence/motion_navigation/d115-two-jump-v1/published-production-semantic-hashes.json"
        self.assertIn(relative, {path.as_posix() for path in self.manifest.required_paths})
        report = json.loads((self.root / relative).read_text("utf-8"))
        self.assertEqual(report["schema"], "mc2p.public-production-semantic-hashes.v1")
        self.assertEqual(report["normalization"], "CRLF_TO_LF")
        self.assertEqual(set(report["files"]), {
            "mc2p/motion_nav/motion_coordination.py",
            "scripts/b10_gap_solver_runtime.py",
        })
        self.assertEqual(report["source_commit"], "3efc2fc3")
        if not (ROOT / ".git").exists():
            return
        commit = report["source_commit"]
        if subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                          cwd=ROOT, capture_output=True).returncode != 0:
            return
        for name, expected in report["files"].items():
            raw = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT)
            self.assertEqual(
                hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
                expected,
                (commit, name),
            )

    def test_d116_historical_semantics_remain_bound_to_the_published_commit(self):
        relative = "evidence/motion_navigation/d116-handoff-timeline-v1/published-production-semantic-hashes.json"
        self.assertIn(relative, {path.as_posix() for path in self.manifest.required_paths})
        report = json.loads((self.root / relative).read_text("utf-8"))
        self.assertEqual(report["schema"], "mc2p.public-production-semantic-hashes.v1")
        self.assertEqual(report["normalization"], "CRLF_TO_LF")
        self.assertEqual(set(report["files"]), {
            "mc2p/contracts/motion_activation.py",
            "mc2p/motion_nav/action_route_executor.py",
        })
        self.assertEqual(
            report["source_commit"],
            "30f42cf641a8081283b917a91d14a21f738f38c6",
        )
        if not (ROOT / ".git").exists():
            return
        commit = report["source_commit"]
        if subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                          cwd=ROOT, capture_output=True).returncode != 0:
            return
        for name, expected in report["files"].items():
            raw = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT)
            self.assertEqual(
                hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
                expected,
                (commit, name),
            )

    def test_d117_current_semantics_cover_prepared_handoff_bytes(self):
        relative = (
            "evidence/motion_navigation/d117-prepared-handoff-v1/"
            "published-production-semantic-hashes.json"
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        self.assertIn(relative, required)
        for path in (
            "docs/motion_navigation/decisions/0117-close-prepared-successor-handoff.md",
            "docs/motion_navigation/stages/d117-prepared-successor-handoff-closure-plan.md",
            "docs/motion_navigation/acceptance/d117-prepared-successor-handoff-closure.md",
            "scripts/d116_handoff_timeline.py",
            "tests/motion_nav/test_d117_prepared_liveness_monitor.py",
        ):
            self.assertIn(path, required)
            self.assertEqual(
                (self.root / path).read_bytes(),
                (ROOT / path).read_bytes(),
            )
        report = json.loads((self.root / relative).read_text("utf-8"))
        self.assertEqual(report["schema"], "mc2p.public-production-semantic-hashes.v1")
        self.assertEqual(report["normalization"], "CRLF_TO_LF")
        self.assertEqual(report["status"], "current")
        self.assertEqual(
            report["source_commit"],
            "410a7bc9f0485920e830b78e1b0f7f758c425863",
        )
        self.assertEqual(set(report["files"]), {
            "mc2p/motion_nav/action_route_executor.py",
            "mc2p/motion_nav/execution_supervisor.py",
            "mc2p/motion_nav/motion_candidate.py",
            "mc2p/motion_nav/motion_coordination.py",
            "mc2p/motion_nav/motion_worker.py",
            "mc2p/motion_nav/prepared_motion.py",
            "mc2p/motion_nav/route_admission.py",
            "mc2p/motion_nav/route_body_controller.py",
            "mc2p/motion_nav/world_model.py",
        })
        for name, expected in report["files"].items():
            raw = (self.root / name).read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(raw).hexdigest(), expected, name)

    def test_d110_historical_semantics_are_exported_without_claiming_current_bytes(self):
        relative = "evidence/motion_navigation/d110-evidence-lifecycle-v1/published-production-semantic-hashes.json"
        report = json.loads((self.root / relative).read_text("utf-8"))
        self.assertEqual(report["source_commit"], "67f4f1d82fa3946472437f6f747e005591fd2bca")
        self.assertEqual(report["public_commit"], "f66358654398a18d01d224cc4f5902ef48a31b84")
        self.assertEqual(report["normalization"], "CRLF_TO_LF")
        self.assertTrue({"scripts/b10_gap_solver_runtime.py",
                         "scripts/client_time_evidence.py",
                         "scripts/fabric_visibility_scenario.py",
                         "scripts/run_motion_nav_shards.py",
                         "scripts/probe_fabric_deployment_observation.py"}
                        <= report["files"].keys())
        for expected in report["files"].values():
            self.assertRegex(expected, r"^[0-9a-f]{64}$")
        # A source-only snapshot or public clone need not contain private
        # historical objects. Check each available commit, never D111's bytes.
        if not (ROOT / ".git").exists():
            return
        commits = [commit for commit in (report["source_commit"], report["public_commit"])
                   if subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                                     cwd=ROOT, capture_output=True).returncode == 0]
        for name, expected in report["files"].items():
            for commit in commits:
                raw = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT)
                self.assertEqual(hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest(),
                                 expected, (commit, name))

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
        handoff = json.loads((
            ROOT / "evidence/motion_navigation/action-entry-handoff-v1/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        late_handoff = json.loads((
            ROOT / "evidence/motion_navigation/action-entry-late-hardening-v1/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        post_loss = json.loads((
            ROOT / "evidence/motion_navigation/post-input-loss-landing-v1/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        tp0 = json.loads((
            self.root / "evidence/motion_navigation/trajectory-proto-tp0-v1/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        published = json.loads((
            self.root / "evidence/motion_navigation/prepared-motion-proof-d102-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d105 = json.loads((
            self.root / "evidence/motion_navigation/sound-motion-proof-d105-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d107 = json.loads((
            self.root / "evidence/motion_navigation/d107-regression-recovery-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d108 = json.loads((
            self.root / "evidence/motion_navigation/d108-snapshot-hardening-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d109 = json.loads((
            self.root / "evidence/motion_navigation/d109-b10-lifecycle-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d112 = json.loads((
            self.root / "evidence/motion_navigation/d112-worker-delivery-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d113 = json.loads((
            self.root / "evidence/motion_navigation/d113-control-visible-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d114 = json.loads((
            self.root / "evidence/motion_navigation/d114-formal-driver-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d115 = json.loads((
            self.root / "evidence/motion_navigation/d115-two-jump-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d116 = json.loads((
            self.root / "evidence/motion_navigation/d116-handoff-timeline-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d117 = json.loads((
            self.root / "evidence/motion_navigation/d117-prepared-handoff-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        current_semantics = {**canonical, **r1, **r2, **r3, **handoff,
                             **late_handoff, **post_loss, **tp0, **published,
                             **d105, **d107, **d108, **d109, **d112, **d113,
                             **d114, **d115, **d116, **d117}
        tp0_root = self.root / "evidence/motion_navigation/trajectory-proto-tp0-v1"
        frozen = json.loads((tp0_root / "frozen-control-manifest.json").read_text("utf-8"))
        audit = json.loads((tp0_root / "fabric/audit-summary.json").read_text("utf-8"))
        direct = json.loads((tp0_root / "checks-forward-direct-summary.json").read_text("utf-8"))
        self.assertEqual(frozen["frozen_control_commit"], audit["frozen_control_commit"])
        self.assertEqual(audit["cases"], 4)
        self.assertTrue(audit["passed"])
        self.assertFalse(direct["generated_by_run_motion_navigation_checks"])
        self.assertIsNone(direct["shared_scenario_hash_after"])
        for name, expected in report["full_production_files"].items():
            raw = (self.root/name).read_bytes()
            lf = raw.replace(b"\r\n", b"\n")
            raw_hash = hashlib.sha256(raw).hexdigest()
            if raw_hash != expected:
                self.assertEqual(
                    hashlib.sha256(lf).hexdigest(),
                    current_semantics[name],
                    name,
                )

    def test_export_has_no_local_artifacts_credentials_or_worlds(self):
        self.assertFalse(
            (self.root / "tests/motion_nav/test_d102_evidence_contract.py").exists(),
            "local-raw D102 evidence tests must not enter the source-only snapshot",
        )
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

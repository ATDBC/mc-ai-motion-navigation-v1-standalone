from __future__ import annotations

from pathlib import Path, PurePosixPath
import hashlib
from contextlib import redirect_stdout
from io import StringIO
import json
import shutil
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

from scripts.export_motion_navigation_standalone import (
    ExportViolation,
    ExportManifest,
    export_tree,
    load_manifest,
    main,
    verify_tree,
    _allowed_source,
)


ROOT = Path(__file__).resolve().parents[2]


class ByteReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = ROOT / ".tmp" / f"byte-reconciliation-{uuid.uuid4().hex}"
        self.source = self.sandbox / "source"
        self.source.mkdir(parents=True)
        self.base = self.source / ".tmp" / "base"
        self.target = self.source / ".tmp" / "candidate"
        self.files = {
            "main.py": b"VALUE = 1\r\n",
            "same.txt": b"alpha\r\nbeta\r\n",
            "changed.txt": b"old\r\n",
            "binary.bin": b"\xff\r\n",
            "nul.bin": b"\0\r\n",
            "bom.txt": b"\xef\xbb\xbftext\r\n",
            "spaces.txt": b"word \r\n",
            "deleted.txt": b"removed\r\n",
            "requirements.txt": b"numpy==2.4.6\npsutil>=5.9\n",
        }
        for name, raw in self.files.items():
            (self.source / name).write_bytes(raw)
        manifest_path = self.source / "manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")
        self.manifest = ExportManifest(
            manifest_path, ((PurePosixPath("main.py"), PurePosixPath("entry.py")),),
            tuple(name for name in self.files if name != "main.py"),
            ("main.py",), (PurePosixPath("requirements.txt"),),
        )
        self.patches = [
            patch("scripts.export_motion_navigation_standalone.ROOT", self.source),
            patch("scripts.export_motion_navigation_standalone._source_commit", return_value="1" * 40),
            patch("scripts.export_motion_navigation_standalone._source_dirty", return_value=False),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(self.cleanup)
        export_tree(self.base, clean=True, manifest=self.manifest)

    def cleanup(self):
        for item in reversed(self.patches):
            item.stop()
        shutil.rmtree(self.sandbox)

    def candidate_manifest(self):
        from dataclasses import replace
        (self.source / "deleted.txt").unlink(missing_ok=True)
        (self.source / "added.txt").write_bytes(b"new\n")
        return replace(
            self.manifest,
            include_globs=tuple(name for name in self.manifest.include_globs if name != "deleted.txt") + ("added.txt",),
            required_paths=self.manifest.required_paths + (PurePosixPath("added.txt"),),
        )

    def test_export_reuses_only_utf8_eol_equivalent_bytes_and_reports_changes(self):
        (self.source / "same.txt").write_bytes(b"alpha\nbeta\n")
        (self.source / "changed.txt").write_bytes(b"new\n")
        (self.source / "binary.bin").write_bytes(b"\xff\n")
        (self.source / "nul.bin").write_bytes(b"\0\n")
        (self.source / "bom.txt").write_bytes(b"text\n")
        (self.source / "spaces.txt").write_bytes(b"word\n")
        reports = []
        result = export_tree(
            self.target, clean=True, manifest=self.candidate_manifest(),
            byte_base_root=self.base, byte_report_sink=reports.append,
        )
        self.assertEqual(result, self.target)
        self.assertEqual((self.target / "same.txt").read_bytes(), self.files["same.txt"])
        for name in ("changed.txt", "binary.bin", "nul.bin", "bom.txt", "spaces.txt"):
            self.assertEqual((self.target / name).read_bytes(), (self.source / name).read_bytes())
        self.assertFalse((self.target / "deleted.txt").exists())
        report, = reports
        self.assertIn("same.txt", report.reused)
        self.assertTrue({"changed.txt", "binary.bin", "nul.bin", "bom.txt", "spaces.txt", "EXPORT-METADATA.json", "SHA256SUMS.txt"} <= set(report.current))
        self.assertEqual(report.added, ("added.txt",))
        self.assertEqual(report.deleted, ("deleted.txt",))
        verify_tree(self.target, manifest=self.candidate_manifest())

    def test_no_report_sink_still_reconciles_and_metadata_stays_current(self):
        (self.source / "same.txt").write_bytes(b"alpha\nbeta\n")
        with patch("scripts.export_motion_navigation_standalone._source_commit", return_value="2" * 40):
            export_tree(self.target, clean=True, manifest=self.manifest, byte_base_root=self.base)
        self.assertEqual((self.target / "same.txt").read_bytes(), self.files["same.txt"])
        self.assertEqual(json.loads((self.target / "EXPORT-METADATA.json").read_text())["source_commit"], "2" * 40)
        self.assertNotEqual((self.target / "SHA256SUMS.txt").read_bytes(), (self.base / "SHA256SUMS.txt").read_bytes())
        verify_tree(self.target, manifest=self.manifest)

    def test_cli_reports_reconciliation_without_adding_report_files(self):
        (self.source / "same.txt").write_bytes(b"alpha\nbeta\n")
        output = StringIO()
        with patch("scripts.export_motion_navigation_standalone.load_manifest", return_value=self.manifest):
            with redirect_stdout(output):
                status = main(["export", "--root", str(self.target), "--byte-base-root", str(self.base)])
        self.assertEqual(status, 0)
        lines = output.getvalue().splitlines()
        report = json.loads(lines[1])["byte_reconciliation"]
        self.assertIn("same.txt", report["reused"])
        self.assertFalse((self.target / "byte-reconciliation.json").exists())
        verify_tree(self.target, manifest=self.manifest)

    def test_corrupt_base_is_rejected_before_cleaning_existing_target(self):
        (self.base / "same.txt").write_bytes(b"tampered")
        self.target.mkdir()
        marker = self.target / "keep.txt"
        marker.write_bytes(b"keep")
        with self.assertRaises(ExportViolation):
            export_tree(self.target, clean=True, manifest=self.manifest, byte_base_root=self.base)
        self.assertEqual(marker.read_bytes(), b"keep")


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

    def test_d123_1_stop_evidence_and_its_compact_index_targets_are_required(self):
        base = "evidence/motion_navigation/d123-proof-and-transport-v1/d123-1/"
        for name in (base + "fixed-eye-run-2/stop.json", base + "index.json"):
            self.assertIn(PurePosixPath(name), self.manifest.required_paths)
            self.assertTrue((self.root / name).is_file(), name)
        index = json.loads((self.root / (base + "index.json")).read_text("utf-8"))
        for name, expected in index["evidence_sha256"].items():
            self.assertIn(PurePosixPath(name), self.manifest.required_paths)
            self.assertEqual(hashlib.sha256((self.root / name).read_bytes()).hexdigest(), expected)
        stop = json.loads((self.root / (base + "fixed-eye-run-2/stop.json")).read_text("utf-8"))
        self.assertEqual(stop["historical_actor"]["trace_sha256"],
                         "a893782ad9121847e7fa6ff5cb489cd90f5fecf0dd78a9c30238c08d2cdb3bf4")
        self.assertFalse((self.root / "artifacts").exists())

    def test_manifest_rejects_omitting_each_d123_1_stop_evidence_selection(self):
        from dataclasses import replace
        from scripts.export_motion_navigation_standalone import collect_export_files
        base = "evidence/motion_navigation/d123-proof-and-transport-v1/d123-1/"
        for name in (base + "fixed-eye-run-2/stop.json", base + "index.json"):
            with self.subTest(name=name):
                changed = replace(self.manifest, include_globs=tuple(
                    selected for selected in self.manifest.include_globs if selected != name))
                with self.assertRaises(ExportViolation) as raised:
                    collect_export_files(changed)
                self.assertIn(name, str(raised.exception))

    def test_required_d123_1_evidence_cannot_be_hidden_by_rewriting_snapshot_hash_list(self):
        base = "evidence/motion_navigation/d123-proof-and-transport-v1/d123-1/"
        hash_file = self.root / "SHA256SUMS.txt"
        original_hashes = hash_file.read_bytes()
        for name in (base + "fixed-eye-run-2/stop.json", base + "index.json"):
            with self.subTest(name=name):
                target = self.root / name
                self.assertTrue(target.is_file(), name)
                original = target.read_bytes()
                try:
                    target.unlink()
                    hash_file.write_bytes(b"\n".join(line for line in original_hashes.splitlines()
                        if not line.endswith(name.encode("utf-8"))) + b"\n")
                    with self.assertRaises(ExportViolation) as raised:
                        verify_tree(self.root, manifest=self.manifest)
                    self.assertIn(name, str(raised.exception))
                finally:
                    target.write_bytes(original)
                    hash_file.write_bytes(original_hashes)

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

    def test_d117_historical_semantics_remain_bound_to_the_published_commit(self):
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

    def test_d120_compact_evidence_preserves_limits_history_and_source_identity(self):
        base = "evidence/motion_navigation/d120-momentum-entry-v1/"
        required = {path.as_posix() for path in self.manifest.required_paths}
        for name in ("index.json","README.md","windows-regression/summary.json","offline/summary.json","offline/manifest.json",
                     "south/summary.json","south/analysis.json","full/summary.json","full/analysis.json",
                     "history/jdk-preflight/summary.json","history/south-health/summary.json",
                     "history/full-yaw/summary.json"):
            self.assertIn(base+name,required)
        index = json.loads((self.root/base/"index.json").read_text("utf-8"))
        self.assertEqual(index["sound_envelope"],"unverified")
        self.assertFalse(index["production_capability_authorized"])
        self.assertEqual(index["d118_historical_status"],"FAILED/STOPPED")
        regression = json.loads((self.root/base/"windows-regression/summary.json").read_text("utf-8"))
        self.assertEqual(regression["status"],"passed")
        for name,run in regression["runs"].items():
            self.assertEqual((run["module_count"],run["test_count"]),
                (197,2534) if name.startswith("source-") else (196,2510))
            self.assertTrue(all(run[k]==0 for k in ("failures","errors","timeouts","skipped","cleanup_failures")))
            self.assertEqual(run["state"],"complete")
            self.assertGreater(run["bytes"],0)
            self.assertEqual(len(run["sha256"]),64)
        self.assertEqual(set(regression["runs"]),{"source-forward","source-reverse","public-forward","public-reverse"})
        self.assertEqual(regression["java"],{"source":{"tests_run":3,"passed":3},"public":{"tests_run":3,"passed":3}})
        self.assertEqual(regression["verified_baseline_file_count"],2634)
        for name,digest in index["files"].items():
            self.assertEqual(hashlib.sha256((self.root/base/name).read_bytes()).hexdigest(),digest,name)
        offline = json.loads((self.root/base/"offline/summary.json").read_text("utf-8"))
        self.assertEqual((offline["sample_count"],offline["suffix_count"],offline["main_physics_steps"]),(972,2916,47628))
        for mode,trials,commands in (("south",3,123),("full",36,1476)):
            summary = json.loads((self.root/base/mode/"summary.json").read_text("utf-8"))
            analysis = json.loads((self.root/base/mode/"analysis.json").read_text("utf-8"))
            self.assertEqual(summary["acceptance_status"],"passed")
            self.assertEqual(len(summary["raw_files"]),14)
            self.assertEqual((len(analysis["entries"]),analysis["joined_command_count"]),(trials,commands))
            self.assertEqual(analysis["status"],"passed")
            self.assertEqual(analysis["health_evidence"]["health_decrease_count"],0)
            self.assertEqual(analysis["post_release_tail_status"],"passed")
            self.assertTrue(all(e["within_registered_bounds"] and e["timing_passed"]
                and all(s["passed"] for s in e["raw_suffix_replay"].values()) for e in analysis["entries"]))
        for name in ("jdk-preflight","south-health","full-yaw"):
            summary = json.loads((self.root/base/"history"/name/"summary.json").read_text("utf-8"))
            self.assertEqual(summary["status"],"failed")
            original = index["original_files"]["history/"+name+"/summary.json"]
            self.assertEqual(hashlib.sha256((self.root/base/"history"/name/"summary.json").read_bytes()).hexdigest(),original["sha256"])
        current_harness_path = self.root / (
            "evidence/motion_navigation/d122-client-timing-v1/d122-4/"
            "harness-semantic-hashes.json"
        )
        current_harness = json.loads(current_harness_path.read_text("utf-8"))
        d124_current = json.loads((
            self.root / "evidence/motion_navigation/d124-proof-dependency-projection-v1/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d124_harness_record = json.loads((
            self.root / "evidence/motion_navigation/d124-proof-dependency-projection-v1/"
            "harness-semantic-hashes.json"
        ).read_text("utf-8"))
        self.assertEqual(
            d124_harness_record["previous_d122_harness_record_sha256"],
            hashlib.sha256(current_harness_path.read_bytes()).hexdigest(),
        )
        d124_harness = d124_harness_record["files"]
        d126_root = (
            self.root / "evidence/motion_navigation/d126-known-map-motion-v1/"
            "run-1"
        )
        d126_summary_path = d126_root / "summary.json"
        d126_current = json.loads(
            d126_summary_path.read_text("utf-8")
        )["current_source_hashes"]
        d126_manifest = json.loads(
            (d126_root / "manifest.json").read_text("utf-8")
        )
        from scripts.d123_fabric_transport import current_harness_semantic_hashes
        d123_current = current_harness_semantic_hashes()
        self.assertEqual(
            d126_manifest["files"]["summary.json"],
            hashlib.sha256(d126_summary_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            current_harness["previous_d120_index_sha256"],
            hashlib.sha256((self.root / base / "index.json").read_bytes()).hexdigest(),
        )
        for name,digest in index["implementation_semantic_hashes"].items():
            expected = d123_current.get(
                name, d124_current.get(name, current_harness["files"].get(name, digest))
            )
            self.assertEqual(hashlib.sha256((self.root/name).read_bytes().replace(b"\r\n",b"\n")).hexdigest(),expected,name)
        for name, digest in current_harness["files"].items():
            expected = d123_current.get(
                name, d126_current.get(name, d124_harness.get(name, digest))
            )
            self.assertTrue((self.root / name).is_file(), name)
            self.assertEqual(len(expected), 64, name)
        self.assertEqual(
            d126_current["scripts/d122_fabric_timing.py"],
            "650879798abd0f30b96fdc5d334cd3c9b161a3efea2f5d27acbb572c2e76090b",
        )
        self.assertIn("scripts/d120_momentum_calibration.py",required)
        self.assertIn("scripts/d120_momentum_entry_calibration.py",required)
        self.assertIn("tests/motion_nav/test_d120_momentum_entry_calibration.py",required)
        self.assertIn("scripts/d123_fabric_transport.py", required)
        self.assertIn("tests/motion_nav/test_d123_fabric_transport.py", required)

    def test_d121_s0_diagnostics_keep_their_explicit_non_authority_boundary(self):
        base = "evidence/motion_navigation/d121-momentum-handoff-v1/s0/"
        required = {path.as_posix() for path in self.manifest.required_paths}
        for name in (
            "scripts/d121_momentum_handoff_probe.py",
            "tests/motion_nav/test_d121_momentum_handoff.py",
            base + "diagnostic.json",
            base + "history/pre-receipt-gate/final.json",
            base + "final.json",
        ):
            self.assertIn(name, required)
        final = json.loads((self.root / base / "final.json").read_text("utf-8"))
        self.assertEqual(final["status"], "diagnostic_only")
        self.assertEqual(final["sound_envelope"], "unverified")
        self.assertEqual(final["diagnostic_checks_status"], "passed")
        self.assertTrue(all(row["receipt_identity_match"]
                            and row["receipt_movement_match"]
                            and row["final_position_velocity_match"]
                            for row in final["runtime"]))
        self.assertTrue(all(fact["status"] == "unimplemented"
                            for fact in final["gaps"]))

    def test_d121_historical_s1_and_current_preparation_semantics_are_separate(self):
        relative = (
            "evidence/motion_navigation/d121-momentum-handoff-v1/"
            "published-production-semantic-hashes.json"
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        self.assertIn(relative, required)
        report = json.loads((self.root / relative).read_text("utf-8"))
        self.assertEqual(report["schema"], "mc2p.public-production-semantic-hashes.v1")
        self.assertEqual(report["normalization"], "CRLF_TO_LF")
        self.assertEqual(report["status"], "current")
        self.assertEqual(
            report["source_commit"],
            "1ef025d193d3c9ac61b0d5dd1a27d27774c9f9d5",
        )
        self.assertEqual(set(report["files"]), {
            "mc2p/motion_nav/motion_chain.py",
            "mc2p/motion_nav/motion_solver.py",
            "mc2p/motion_nav/prepared_motion.py",
            "mc2p/motion_nav/prepared_motion_proof.py",
        })
        current_path = (self.root / "evidence/motion_navigation/d121-momentum-handoff-v1/"
                        "release-repair/final/production-semantic-hashes.json")
        self.assertIn(current_path.relative_to(self.root).as_posix(), required)
        current = json.loads(current_path.read_text("utf-8"))
        self.assertEqual(current["normalization"], "CRLF_TO_LF")
        self.assertEqual(current["historical_s1_record_sha256"],
                         hashlib.sha256((self.root / relative).read_bytes()).hexdigest())
        task3_path = self.root / "evidence/motion_navigation/d121-momentum-handoff-v1/task-3-preparation/repair/production-semantic-hashes.json"
        self.assertEqual(current["previous_task3_record_sha256"],
                         hashlib.sha256(task3_path.read_bytes()).hexdigest())
        task4_path = self.root / "evidence/motion_navigation/d121-momentum-handoff-v1/task-4-execution/production-semantic-hashes.json"
        self.assertEqual(current["previous_task4_record_sha256"],
                         hashlib.sha256(task4_path.read_bytes()).hexdigest())
        self.assertTrue(set(report["files"]).issubset(current["files"]))

    def test_d122_historical_semantics_continue_d121_without_rewriting_history(self):
        relative = (
            "evidence/motion_navigation/d122-client-timing-v1/d122-3/"
            "production-semantic-hashes.json"
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        self.assertIn(relative, required)
        report = json.loads((self.root / relative).read_text("utf-8"))
        self.assertEqual(report["schema"], "mc2p.public-production-semantic-hashes.v1")
        self.assertEqual(report["normalization"], "CRLF_TO_LF")
        self.assertEqual(report["status"], "current")
        previous = self.root / (
            "evidence/motion_navigation/d121-momentum-handoff-v1/"
            "release-repair/final/production-semantic-hashes.json"
        )
        self.assertEqual(
            report["previous_d121_release_repair_record_sha256"],
            hashlib.sha256(previous.read_bytes()).hexdigest(),
        )
        d124_path = self.root / (
            "evidence/motion_navigation/d124-proof-dependency-projection-v1/"
            "production-semantic-hashes.json"
        )
        d124 = json.loads(d124_path.read_text("utf-8"))
        self.assertEqual(
            d124["previous_d122_current_record_sha256"],
            hashlib.sha256((self.root / relative).read_bytes()).hexdigest(),
        )
        self.assertTrue(all(
            (self.root / name).is_file() and len(expected) == 64
            for name, expected in report["files"].items()
        ))

        from scripts.d122_fabric_timing import _D127_CURRENT_HASHES
        for name, expected in _D127_CURRENT_HASHES.items():
            raw = (self.root / name).read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(hashlib.sha256(raw).hexdigest(), expected, name)

        d127_root = self.root / (
            "evidence/motion_navigation/d123-proof-and-transport-v1/"
            "d123-2/run-1"
        )
        d127_manifest = json.loads(
            (d127_root / "manifest.json").read_text("utf-8")
        )
        self.assertEqual(
            d127_manifest["files"]["summary.json"],
            hashlib.sha256((d127_root / "summary.json").read_bytes()).hexdigest(),
        )

    def test_d122_component_result_and_review_red_are_both_exported(self):
        evidence_root = Path(
            "evidence/motion_navigation/d122-client-timing-v1/d122-3"
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        for name in (
            "formal-matrix.json",
            "formal-matrix-final.json",
            "never-lifecycle-red.txt",
            "d122-directed-final.txt",
            "d122-directed-final2.txt",
            "final-gates.json",
        ):
            self.assertIn((evidence_root / name).as_posix(), required)

        historical = json.loads((self.root / evidence_root / "formal-matrix.json").read_text("utf-8"))
        final = json.loads((self.root / evidence_root / "formal-matrix-final.json").read_text("utf-8"))
        self.assertEqual(len(historical["runs"]), 12)
        self.assertEqual(historical["result"], "PASS")
        self.assertTrue(any(not run["source_released"] for run in historical["runs"]))
        self.assertEqual(len(final["runs"]), 20)
        self.assertEqual(final["result"], "PASS")
        self.assertTrue(all(run["passed"] for run in final["runs"]))
        self.assertTrue(all(run["source_released"] for run in final["runs"]))
        gates = json.loads((self.root / evidence_root / "final-gates.json").read_text("utf-8"))
        self.assertEqual(gates["status"], "PASS")
        self.assertFalse(gates["fabric_run"])
        self.assertEqual(gates["source"]["forward"]["tests"], 2680)
        self.assertEqual(gates["standalone"]["forward"]["tests"], 2656)

    def test_d124_exports_compact_dependency_projection_evidence(self):
        evidence_root = Path(
            "evidence/motion_navigation/d124-proof-dependency-projection-v1"
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        for relative in (
            "README.md",
            "manifest.json",
            "targeted/run-1/manifest.json",
            "targeted/run-2/manifest.json",
            "targeted/run-2/dependencies.json",
            "targeted/run-2/minecraft-1_21-rules.json",
            "targeted/run-2/source-boundaries.json",
            "targeted/run-2/source-hashes.json",
            "targeted/run-2/summary.json",
            "targeted/run-3/manifest.json",
            "targeted/run-3/dependencies.json",
            "targeted/run-3/minecraft-1_21-rules.json",
            "targeted/run-3/source-boundaries.json",
            "targeted/run-3/source-hashes.json",
            "targeted/run-3/summary.json",
            "targeted/run-4/manifest.json",
            "targeted/run-4/dependencies.json",
            "targeted/run-4/minecraft-1_21-rules.json",
            "targeted/run-4/source-boundaries.json",
            "targeted/run-4/source-hashes.json",
            "targeted/run-4/summary.json",
            "targeted/run-5/manifest.json",
            "targeted/run-5/dependencies.json",
            "targeted/run-5/minecraft-1_21-rules.json",
            "targeted/run-5/source-boundaries.json",
            "targeted/run-5/source-hashes.json",
            "targeted/run-5/action-transport.json",
            "targeted/run-5/summary.json",
            "targeted/run-6/manifest.json",
            "targeted/run-6/dependencies.json",
            "targeted/run-6/minecraft-1_21-rules.json",
            "targeted/run-6/source-boundaries.json",
            "targeted/run-6/source-hashes.json",
            "targeted/run-6/action-transport.json",
            "targeted/run-6/summary.json",
            "targeted/run-7/manifest.json",
            "targeted/run-7/environment.json",
            "targeted/run-7/failure.json",
            "targeted/run-8/manifest.json",
            "targeted/run-8/dependencies.json",
            "targeted/run-8/minecraft-1_21-rules.json",
            "targeted/run-8/source-boundaries.json",
            "targeted/run-8/source-hashes.json",
            "targeted/run-8/action-transport.json",
            "targeted/run-8/summary.json",
            "production-semantic-hashes.json",
            "harness-semantic-hashes.json",
            "incremental-motion-proof-current.json",
            "full-source-diagnostics/red.txt",
            "full-source-diagnostics/green.txt",
            "full-source-diagnostics/summary.json",
            "d120/compact.json",
            "d122/compact.json",
            "d122/v1-compat.json",
            "d122/smoke-history.json",
            "performance/message-sizes.json",
        ):
            self.assertIn((evidence_root / relative).as_posix(), required)

        manifest = json.loads((self.root / evidence_root / "manifest.json").read_text("utf-8"))
        for relative, expected in manifest["files"].items():
            actual = hashlib.sha256((self.root / evidence_root / relative).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, relative)

        d120 = json.loads((self.root / evidence_root / "d120/compact.json").read_text("utf-8"))
        self.assertTrue(d120["summary"]["all_exactly_equal"])
        self.assertEqual(d120["summary"]["entrances"], 972)
        self.assertEqual(d120["summary"]["suffixes"], 2916)
        self.assertEqual(d120["summary"]["old_success_regressions"], 0)
        self.assertTrue(all(row["equal"] for row in d120["directions"]))

        d122 = json.loads((self.root / evidence_root / "d122/compact.json").read_text("utf-8"))
        self.assertEqual(d122["result"], "PASS")
        self.assertEqual(len(d122["runs"]), 20)
        self.assertTrue(all(row["passed"] for row in d122["runs"]))
        self.assertTrue(d122["performance"]["passed"])

        for path in (self.root / evidence_root).rglob("*.json"):
            with self.subTest(path=path.relative_to(self.root)):
                json.loads(path.read_text("utf-8"))

    def test_d125_exports_probe_tests_and_hash_bound_stop_evidence(self):
        evidence_root = Path(
            "evidence/motion_navigation/d125-three-required-fact-observation-v1"
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        expected = (
            Path("scripts/d125_three_fact_observation.py"),
            Path("scripts/surface_depth_probe/build.py"),
            Path("scripts/surface_depth_probe/native.py"),
            Path("tests/motion_nav/test_d125_three_fact_observation.py"),
            Path("deployment/surface-depth-diagnostic/native/CMakeLists.txt"),
            Path("deployment/surface-depth-diagnostic/native/cache.cpp"),
            Path("deployment/surface-depth-diagnostic/native/geometry.hpp"),
            Path("deployment/surface-depth-diagnostic/native/jni.cpp"),
            Path(
                "deployment/surface-depth-diagnostic/native/vendor/clipper2/"
                "src/clipper.engine.cpp"
            ),
            evidence_root / "README.md",
            evidence_root / "d125-0/run-1/manifest.json",
            evidence_root / "d125-0/run-1/stop.json",
            evidence_root / "d125-0/run-2/manifest.json",
            evidence_root / "d125-0/run-2/stop.json",
        )
        for relative in expected:
            with self.subTest(relative=relative):
                self.assertIn(relative.as_posix(), required)
                self.assertTrue((self.root / relative).is_file())

        for run in ("run-1", "run-2"):
            root = self.root / evidence_root / "d125-0" / run
            manifest = json.loads((root / "manifest.json").read_text("utf-8"))
            self.assertEqual(
                hashlib.sha256((root / "stop.json").read_bytes()).hexdigest(),
                manifest["files"]["stop.json"],
            )
            stop = json.loads((root / "stop.json").read_text("utf-8"))
            self.assertEqual(stop["status"], "PARTIAL_STOPPED")
            self.assertFalse(stop["next_steps_authorized"])

        final = json.loads((
            self.root / evidence_root / "d125-0/run-2/stop.json"
        ).read_text("utf-8"))
        script = self.root / "scripts/d125_three_fact_observation.py"
        self.assertEqual(
            hashlib.sha256(script.read_bytes()).hexdigest(),
            final["inputs"]["scripts/d125_three_fact_observation.py"],
        )

    def test_d126_exports_known_map_gate_and_compact_evidence(self):
        evidence_root = Path(
            "evidence/motion_navigation/d126-known-map-motion-v1/run-1"
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        expected = (
            Path("scripts/d126_known_map_motion.py"),
            Path("tests/motion_nav/test_d126_known_map_motion.py"),
            Path("tests/observation_v3_fixtures.py"),
            evidence_root / "README.md",
            evidence_root / "manifest.json",
            evidence_root / "summary.json",
        )
        for relative in expected:
            with self.subTest(relative=relative):
                self.assertIn(relative.as_posix(), required)
                self.assertTrue((self.root / relative).is_file())

        root = self.root / evidence_root
        manifest = json.loads((root / "manifest.json").read_text("utf-8"))
        for relative, expected_hash in manifest["files"].items():
            self.assertEqual(
                hashlib.sha256((root / relative).read_bytes()).hexdigest(),
                expected_hash,
            )
        summary = json.loads((root / "summary.json").read_text("utf-8"))
        self.assertEqual(summary["status"], "PASS")
        self.assertFalse(summary["fabric_run"])
        self.assertEqual(summary["production_files_modified"], 0)
        self.assertEqual(
            [(row["required_count"], row["known_count"],
              row["unknown_count"], row["mismatch_count"])
             for row in summary["directions"]],
            [(58, 58, 0, 0)] * 4,
        )
        self.assertTrue(all(row["same_runtime_owner"]
                            for row in summary["directions"]))
        self.assertEqual(summary["boundaries"]["production_oracle_hits"], [])

    def test_d123_2_exports_transport_contract_and_compact_evidence(self):
        evidence_root = Path(
            "evidence/motion_navigation/d123-proof-and-transport-v1/"
            "d123-2/run-1"
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        expected = (
            Path("mc2p/runtime/transport_lifecycle.py"),
            Path("scripts/d123_transport_preflight.py"),
            Path("scripts/d127_transport_lifecycle_evidence.py"),
            Path("tests/motion_nav/test_d127_transport_lifecycle.py"),
            Path("tests/test_fabric_behavior.py"),
            Path("tests/java/ClientNotAppliedTimingTest.java"),
            Path("mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorExecutor.java"),
            Path("mc2p/backends/runtime_overlays/mc121_actions/ClientBehaviorInput.java"),
            Path("mc2p/backends/runtime_overlays/mc121_actions/ClientTimingSchedule.java"),
            evidence_root / "README.md",
            evidence_root / "manifest.json",
            evidence_root / "summary.json",
        )
        for relative in expected:
            with self.subTest(relative=relative):
                self.assertIn(relative.as_posix(), required)
                self.assertTrue((self.root / relative).is_file())

        root = self.root / evidence_root
        manifest = json.loads((root / "manifest.json").read_text("utf-8"))
        for relative, expected_hash in manifest["files"].items():
            self.assertEqual(
                hashlib.sha256((root / relative).read_bytes()).hexdigest(),
                expected_hash,
            )
        summary = json.loads((root / "summary.json").read_text("utf-8"))
        self.assertEqual(summary["status"], "PASS")
        self.assertFalse(summary["fabric_run"])
        self.assertEqual(summary["preflight"]["passed"], 3)
        self.assertEqual(summary["preflight"]["total"], 3)
        self.assertEqual(summary["contracts"], [
            "local_not_dispatched",
            "remote_rejected_before_sampling",
            "delivery_unknown",
            "channel_lost",
        ])
        self.assertFalse(summary["unresolved"]["source_released"])
        self.assertTrue(
            summary["unresolved"]["owner_snapshot_before_backend_close"]
        )

    def test_d122_fabric_stop_history_and_observation_boundary_are_exported(self):
        evidence_root = Path(
            "evidence/motion_navigation/d122-client-timing-v1/d122-4"
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        expected = {
            "expected-files.json",
            "component-preflight.json",
            "component-preflight-review-fixes.json",
            "real-gradle-home.json",
            "dependency-precheck.json",
            "harness-semantic-hashes.json",
            "v8-preflight.json",
            "environment-stop-history.json",
            "fabric-smoke-v4/environment-stop.json",
            "fabric-smoke-v8/observation-stop.json",
            "final-self-check.json",
        }
        expected.update(
            f"fabric-smoke-v{version}/summary.json" for version in range(1, 9)
        )
        for name in expected:
            self.assertIn((evidence_root / name).as_posix(), required)

        current = json.loads((
            self.root / evidence_root / "component-preflight-review-fixes.json"
        ).read_text("utf-8"))
        self.assertEqual(current["result"], "PASS")
        self.assertEqual(
            {row["policy"] for row in current["trials"]},
            {"timely_l1", "late_l2", "never_delivered"},
        )
        self.assertTrue(all(
            row["passed"] and row["raw_case_passed"]
            and row["failures"] == [] and row["error"] is None
            for row in current["trials"]
        ))
        never = next(
            row for row in current["trials"]
            if row["policy"] == "never_delivered"
        )
        self.assertEqual(never["transport_outcome"], "successor_transport_loss")
        self.assertTrue(never["driver_terminal"])
        self.assertTrue(never["source_released"])
        self.assertEqual(
            never["runtime_failure"],
            {
                "code": "contract",
                "message": "successor_transport_not_delivered",
                "retryable": True,
                "source": "client_behavior",
            },
        )
        historical = json.loads((
            self.root / evidence_root / "component-preflight.json"
        ).read_text("utf-8"))
        self.assertEqual(historical["result"], "PASS")
        self.assertTrue(all(row["passed"] for row in historical["trials"]))

        stopped = json.loads(
            (self.root / evidence_root / "fabric-smoke-v8/observation-stop.json")
            .read_text("utf-8")
        )
        self.assertEqual(stopped["required_count"], 67)
        self.assertEqual(stopped["known_count"], 55)
        self.assertEqual(stopped["unknown_count"], 12)
        self.assertEqual(
            stopped["air_query_status_counts"],
            {"occluded": 10, "outside_view": 2},
        )
        self.assertEqual(stopped["fabric_protocol_trials_run"], 0)
        self.assertFalse(stopped["full_matrix_started"])
        self.assertIn("UNKNOWN remained UNKNOWN", stopped["conclusion"])

    def test_d119_current_semantics_cover_zero_height_step_bytes(self):
        relative = (
            "evidence/motion_navigation/d119-zero-height-step-v1/"
            "published-production-semantic-hashes.json"
        )
        required = {path.as_posix() for path in self.manifest.required_paths}
        self.assertIn(relative, required)
        report = json.loads((self.root / relative).read_text("utf-8"))
        self.assertEqual(report["schema"], "mc2p.public-production-semantic-hashes.v1")
        self.assertEqual(report["normalization"], "CRLF_TO_LF")
        self.assertEqual(report["status"], "current")
        self.assertEqual(
            report["source_commit"],
            "05a97922bdf5a089f8e6373c2c78db4cdae3f6dc",
        )
        self.assertEqual(set(report["files"]), {
            "mc2p/motion_nav/physics_1_21.py",
        })
        self.assertEqual(
            report["files"]["mc2p/motion_nav/physics_1_21.py"],
            "995641c686c89bb571c2bcd02a4c002ae5d07d715f3acd9d898f1eb138356688",
        )
        d124 = json.loads((
            self.root / "evidence/motion_navigation/d124-proof-dependency-projection-v1/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))
        current = (self.root / "mc2p/motion_nav/physics_1_21.py").read_bytes().replace(b"\r\n", b"\n")
        self.assertEqual(hashlib.sha256(current).hexdigest(), d124["files"]["mc2p/motion_nav/physics_1_21.py"])

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
        d119 = json.loads((
            self.root / "evidence/motion_navigation/d119-zero-height-step-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d121 = json.loads((
            self.root / "evidence/motion_navigation/d121-momentum-handoff-v1/"
            "published-production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d121_task3 = json.loads((
            self.root / "evidence/motion_navigation/d121-momentum-handoff-v1/"
            "task-3-preparation/repair/production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d121_task4 = json.loads((
            self.root / "evidence/motion_navigation/d121-momentum-handoff-v1/"
            "task-4-execution/production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d121_task4_repair = json.loads((
            self.root / "evidence/motion_navigation/d121-momentum-handoff-v1/"
            "release-repair/final/production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d122 = json.loads((
            self.root / "evidence/motion_navigation/d122-client-timing-v1/d122-3/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        d124 = json.loads((
            self.root / "evidence/motion_navigation/d124-proof-dependency-projection-v1/"
            "production-semantic-hashes.json"
        ).read_text("utf-8"))["files"]
        from scripts.d122_fabric_timing import _D127_CURRENT_HASHES
        current_semantics = {**canonical, **r1, **r2, **r3, **handoff,
                             **late_handoff, **post_loss, **tp0, **published,
                             **d105, **d107, **d108, **d109, **d112, **d113,
                             **d114, **d115, **d116, **d117, **d119, **d121, **d121_task3, **d121_task4,
                             **d121_task4_repair, **d122, **d124,
                             **_D127_CURRENT_HASHES}
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

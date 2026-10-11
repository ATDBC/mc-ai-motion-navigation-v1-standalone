"""Windows-only trial runner contracts; no trial code is a runtime dependency."""
import json
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import measure_d098_trial_windows as wrapper


class WindowsTrialWrapperTests(unittest.TestCase):
    def test_formal_commands_are_frozen(self):
        self.assertEqual(wrapper.TRIAL_ARGV, {
            "T2": ("perf_record", "--no-setd"),
            "T3": ("perf_record", "--passes", "1"),
        })

    def test_windows_load_uses_provider_and_initializes_before_read(self):
        events = []
        def getter():
            events.append("read")
            return (.25, .5, .75)
        with patch.object(wrapper.platform, "system", return_value="Windows"):
            load = wrapper.initialize_load(getter, lambda _: events.append("wait"))
        self.assertEqual(load, (.25, .5, .75))
        self.assertEqual(events, ["read", "wait", "read"])

    def test_unavailable_load_is_null(self):
        def unavailable():
            raise OSError("no counters")
        self.assertIsNone(wrapper.initialize_load(unavailable, lambda _: None))

    def test_runpy_preserves_script_bytes_and_sets_exact_argv(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp)
            script = trial / "m2" / "run_m2.py"
            script.parent.mkdir()
            script.write_text("import sys\n", encoding="utf-8")
            original = script.read_bytes()
            observed = []
            def run(path, run_name):
                observed.append((Path(path), run_name, sys.argv[:], sys.path[:]))
                for name in wrapper.RECORD_OUTPUTS:
                    (script.parent / name).write_text("{}", encoding="utf-8")
                raise SystemExit(0)
            for label, argv in wrapper.TRIAL_ARGV.items():
                with patch.object(wrapper.runpy, "run_path", side_effect=run):
                    wrapper.invoke_trial(trial, label, lambda: (.1, .2, .3))
                self.assertEqual(observed[-1][:3], (script, "__main__", [str(script), *argv]))
            self.assertEqual(script.read_bytes(), original)
            self.assertEqual(observed[0][3][:3], [str(wrapper.ROOT), str(trial), str(trial / "m2")])

    def test_changed_script_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp)
            script = trial / "m2" / "run_m2.py"
            script.parent.mkdir()
            script.write_bytes(b"original")
            with patch.object(wrapper.runpy, "run_path", side_effect=lambda *a, **k: script.write_bytes(b"changed")):
                with self.assertRaisesRegex(RuntimeError, "bytes changed"):
                    wrapper.invoke_trial(trial, "T2", lambda: (.1, .2, .3))

    def test_exception_records_failure_without_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "result.json"
            with self.assertRaisesRegex(RuntimeError, "trial failed"):
                with wrapper.execution_record(output, {"loadavg": None}):
                    raise RuntimeError("trial failed")
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result["execution"], "failed")
            self.assertNotIn("passed", result)

    def test_sample_signature_ignores_only_wall_time(self):
        columns = ["pass", "config", "cand", "kind", "steps", "ms"]
        a = {"columns": columns, "rows": [[0, "strict", "cand", "tick", 60, 2.0]]}
        b = {"columns": columns, "rows": [[0, "strict", "cand", "tick", 60, 8.0]]}
        self.assertEqual(wrapper.sample_signature(a), wrapper.sample_signature(b))
        b["rows"][0][4] = 59
        self.assertNotEqual(wrapper.sample_signature(a), wrapper.sample_signature(b))

    def test_zero_exit_without_new_outputs_rejects_old_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp)
            m2 = trial / "m2"
            m2.mkdir()
            script = m2 / "run_m2.py"
            script.write_text("raise SystemExit(0)\n", encoding="utf-8")
            for name in ("performance_record.json", "performance_record_samples.json"):
                (m2 / name).write_text('{"old": true}', encoding="utf-8")
            record = trial / "execution.json"
            with self.assertRaisesRegex(RuntimeError, "fresh outputs"):
                with wrapper.execution_record(record, {}):
                    wrapper.invoke_trial(trial, "T2", lambda: (.1, .2, .3))
            self.assertEqual(json.loads(record.read_text())["execution"], "failed")
            self.assertFalse((m2 / "performance_record.json").exists())
            self.assertFalse((m2 / "performance_record_samples.json").exists())

    def test_candidate_coverage_is_computed_and_rejects_incomplete_config(self):
        columns = ["pass", "config", "cand", "kind", "steps", "ms"]
        samples = {"columns": columns, "rows": [
            [0, config, cand, "full_scan" if config == "full_scan" else "plan", 60, 2.0]
            for config in ("strict", "strict_runahead", "full_scan") for cand in ("a", "b")]}
        population = {"candidates": 2, "passes": 1}
        self.assertTrue(wrapper.candidate_coverage(samples, population)["complete"])
        partial = deepcopy(samples)
        partial["rows"] = [r for r in partial["rows"] if not (r[1] == "strict_runahead" and r[2] == "b")]
        self.assertFalse(wrapper.candidate_coverage(partial, population)["complete"])
        partial = deepcopy(samples)
        partial["rows"].append(partial["rows"][-1])
        self.assertFalse(wrapper.candidate_coverage(partial, population)["complete"])

    def test_added_non_time_field_and_changed_columns_affect_signature(self):
        a = {"columns": ["pass", "config", "cand", "kind", "steps", "ms"],
             "rows": [[0, "strict", "cand", "tick", 60, 2.0]]}
        b = deepcopy(a)
        b["columns"].append("permit_count")
        b["rows"][0].append(1)
        self.assertNotEqual(wrapper.sample_signature(a), wrapper.sample_signature(b))
        c = deepcopy(b)
        c["rows"][0][-1] = 2
        self.assertNotEqual(wrapper.sample_signature(b), wrapper.sample_signature(c))
        c = deepcopy(b)
        c["columns"][-1] = "recovery_count"
        self.assertNotEqual(wrapper.sample_signature(b), wrapper.sample_signature(c))

    def test_sample_signature_requires_declared_wall_column_and_row_shape(self):
        with self.assertRaises(ValueError):
            wrapper.sample_signature({"columns": ["config"], "rows": [["strict"]]})
        with self.assertRaises(ValueError):
            wrapper.sample_signature({"columns": ["pass", "config", "cand", "kind", "steps", "ms"],
                                      "rows": [[0, "strict"]]})

    def test_integrity_rejects_matching_incomplete_records_and_column_mismatch(self):
        samples = {"columns": ["pass", "config", "cand", "kind", "steps", "ms"], "rows": [
            [0, "full_scan", "a", "full_scan", 1, 2.0],
            [0, "full_scan", "b", "full_scan", 1, 2.0],
            [0, "strict", "a", "plan", 1, 2.0],
            [0, "strict", "b", "plan", 1, 2.0],
            [0, "strict_runahead", "a", "plan", 1, 2.0]]}
        summary = {"population": {"candidates": 2, "passes": 1},
                   "configs": {"strict": {"tick": {"steps": {"samples": 2}}}},
                   "code_sha256": {"run_m2.py": "frozen"},
                   "step_total_check": {"all_equal": True}, "SMOKE_TEST_NOT_A_RECORD": False,
                   "gate_view": {"strict_runahead": {
                       "tick_le_10ms": True, "commit_le_50ms": True, "tick_samples_ge_1000": True}}}
        with tempfile.TemporaryDirectory() as tmp:
            trial = Path(tmp)
            (trial / "m2").mkdir()
            (trial / "timing").mkdir()
            output = trial / "output"
            output.mkdir()
            for path, payload in (
                (trial / "m2" / "performance_record.json", summary),
                (trial / "m2" / "performance_record_samples.json", samples),
                (trial / "timing" / wrapper.REFERENCE_FILES["T2"][0], summary),
                (trial / "timing" / wrapper.REFERENCE_FILES["T2"][1], samples)):
                path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "integrity failed"):
                wrapper.collect_result(trial, "T2", output)
            result = json.loads((output / "T2-result.json").read_text())
            self.assertFalse(result["reproduction_integrity_passed"])
            self.assertFalse(result["integrity_checks"]["all_candidates_verified_in_original_harness"])
            self.assertTrue(result["integrity_checks"]["every_sample_non_time_fields_match_reference"])
            samples["rows"].append([0, "strict_runahead", "b", "plan", 1, 2.0])
            (trial / "timing" / wrapper.REFERENCE_FILES["T2"][1]).write_text(json.dumps(samples))
            samples["columns"].append("permit_count")
            for row in samples["rows"]:
                row.append(1)
            (trial / "m2" / "performance_record_samples.json").write_text(json.dumps(samples))
            with self.assertRaisesRegex(RuntimeError, "integrity failed"):
                wrapper.collect_result(trial, "T2", output)
            result = json.loads((output / "T2-result.json").read_text())
            self.assertTrue(result["candidate_coverage"]["complete"])
            self.assertFalse(result["integrity_checks"]["sample_columns_match_reference"])
            self.assertFalse(result["reproduction_integrity_passed"])

    def test_frozen_duplicate_candidate_names_require_matching_occurrences(self):
        samples = {"columns": ["pass", "config", "cand", "kind", "steps", "ms"], "rows": [
            [0, config, "same-name", "full_scan" if config == "full_scan" else "plan", 1, 2.0]
            for config in ("strict", "strict_runahead", "full_scan") for _ in range(2)]}
        population = {"candidates": 2, "passes": 1}
        result = wrapper.candidate_coverage(samples, population)
        self.assertTrue(result["complete"])
        self.assertEqual(result["candidate_ids_per_pass"], 1)
        self.assertEqual(result["repeated_candidate_id_occurrences_per_pass"], 1)
        samples["rows"].pop(0)
        result = wrapper.candidate_coverage(samples, population)
        self.assertFalse(result["complete"])
        self.assertFalse(result["candidate_occurrence_counts_match"])

    def test_gzip_is_deterministic_and_preserves_bytes(self):
        import gzip
        raw = b'{"rows": []}'
        self.assertEqual(wrapper.deterministic_gzip(raw), wrapper.deterministic_gzip(raw))
        self.assertEqual(gzip.decompress(wrapper.deterministic_gzip(raw)), raw)

    def _saved_evidence(self, directory, audited):
        output = Path(directory)
        raw_summary, raw_samples = b'{"summary": true}', b'{"rows": []}'
        compressed = wrapper.deterministic_gzip(raw_samples)
        base = {"reproduction_integrity_passed": True,
                "summary_sha256": wrapper.sha256(raw_summary),
                "samples_gzip_sha256": wrapper.sha256(compressed),
                "samples_uncompressed_sha256": wrapper.sha256(raw_samples)}
        signed = deepcopy(base)
        if audited:
            signed["audit_provenance"] = {
                "previous_result_sha256": "first-generation-input",
                "original_execution_wrapper_sha256": "original-wrapper",
                "original_execution_started_utc": "original-date"}
        for name, raw in (("T2-summary.json", raw_summary), ("T2-samples.json.gz", compressed),
                          ("T2-result.json", json.dumps(signed).encode()),
                          ("T2-execution.json", b'{"wrapper_sha256":"original-wrapper","started_utc":"original-date"}'),
                          ("manifest.json", b'{"signed": true}')):
            (output / name).write_bytes(raw)
        return output, base

    def test_two_daily_rechecks_preserve_all_evidence_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, base = self._saved_evidence(tmp, audited=True)
            before = {p.name: p.read_bytes() for p in output.iterdir()}
            with patch.object(wrapper, "analyze_record", return_value=base):
                for _ in range(2):
                    result = wrapper.recheck_evidence(output, "T2", output)
                    self.assertEqual(result["audit_provenance"]["previous_result_sha256"], "first-generation-input")
                    self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)

    def test_initial_audit_rejects_overwriting_existing_audit(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, base = self._saved_evidence(tmp, audited=False)
            with patch.object(wrapper, "analyze_record", return_value=base):
                result = wrapper.audit_evidence(output, "T2", output)
                signed = {p.name: p.read_bytes() for p in output.iterdir()}
                with self.assertRaisesRegex(RuntimeError, "already exists"):
                    wrapper.audit_evidence(output, "T2", output)
                self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, signed)
                self.assertEqual(result["audit_provenance"]["original_execution_wrapper_sha256"], "original-wrapper")


if __name__ == "__main__":
    unittest.main()

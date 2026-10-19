"""The comparison must expose regressions and reject incomplete evidence."""
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.r28_baseline_alignment import compare, compare_migration, raw_record
from tests.sim.product_metrics import extract_metrics, strict_trace


def write_case(root, *, outcome="success", forward=1, seed=0, seed_count=1):
    root.mkdir()
    rows = []
    for tick in range(2, 6):
        terminal = tick >= 4
        rows.append({"movement_tick": tick, "position": [.5, 64., .5 + .1 * (tick - 2)],
                     "velocity": [0., 0., .1 if not terminal else 0.], "on_ground": True, "pose": "standing",
                     "driver_state": outcome if terminal else "executing", "source_bound": tick < 5,
                     "controller_ids": ["route_executor"] if tick < 5 else [], "route_id": "route",
                     "planning_submissions": ["request"] if tick == 2 else [],
                     "goal_revision": 1, "goal_revision_requests": [], "goal_position": [.5, 64., .8],
                     "goal_satisfied": terminal and outcome == "success", "risk_actions": [],
                     "input_window": {"requested_first_tick": tick},
                     "applied_movement": {"forward": forward if not terminal else 0, "strafe": 0}})
    metrics = extract_metrics(rows, start_tick=1, start_position=(.5, 64., .5), outcome=outcome)
    name = f"point-normal-{seed:06d}"
    record = {"id": name, "group": "point-normal", "family": "point", "seed": seed,
              "parameters": {"case": "flat"}, "strict": True, "metrics": metrics,
              "reason": "test_terminal", "exception": None, "trace_file": f"{name}.json.gz"}
    raw = {"record": record, "trace": rows, "strict_trace": strict_trace(rows)}
    payload = gzip.compress(json.dumps(raw).encode(), mtime=0)
    (root / record["trace_file"]).write_bytes(payload)
    record = dict(record, trace_sha256=hashlib.sha256(payload).hexdigest())
    (root / "runs.jsonl").write_text(json.dumps(record) + "\n", "utf-8")
    metadata = {"manifest_sha256": "fixed", "extractor_version": metrics["schema_version"],
                "environment": {"test": True}, "start_clock_ns": 100000000, "tick_seconds": .05,
                "groups": ["point-normal"], "seed_count": seed_count,
                "manifest": {"seed_start": seed, "ground_tick_tolerance": 2}, "harness": {"files": {}}}
    (root / "metadata.json").write_text(json.dumps(metadata), "utf-8")
    return record


class BaselineAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old, self.new = Path(self.temp.name) / "old", Path(self.temp.name) / "new"

    def test_success_becoming_failure_stays_in_denominator(self):
        write_case(self.old)
        write_case(self.new, outcome="failed")
        result = compare(self.old, self.new)
        self.assertEqual(result["pairs"], 1)
        self.assertEqual(result["lost_successes"], ["point-normal-000000"])

    def test_actual_input_changes_are_visible_even_with_same_outcome(self):
        write_case(self.old)
        write_case(self.new, forward=-1)
        result = compare(self.old, self.new)
        self.assertEqual(result["lost_successes"], [])
        self.assertTrue(result["differences"][0]["physical_trace_changed"])
        self.assertTrue(result["differences"][0]["strict_trace_changed"])

    def test_changed_trace_bytes_are_rejected(self):
        row = write_case(self.old)
        (self.old / row["trace_file"]).write_bytes(b"replacement")
        with self.assertRaisesRegex(ValueError, "checksum"):
            raw_record(self.old, row)

    def test_incomplete_declared_run_cannot_be_reported_as_complete(self):
        write_case(self.old, seed_count=2)
        write_case(self.new)
        with self.assertRaisesRegex(ValueError, "denominator"):
            compare(self.old, self.new)

    def test_different_task_parameters_are_rejected(self):
        write_case(self.old)
        row = write_case(self.new)
        row["parameters"] = {"case": "wall_detour"}
        (self.new / "runs.jsonl").write_text(json.dumps(row) + "\n", "utf-8")
        with self.assertRaisesRegex(ValueError, "inputs differ"):
            compare(self.old, self.new)

    def test_unreviewed_simulator_changes_are_rejected(self):
        write_case(self.old)
        write_case(self.new)
        path = self.new / "metadata.json"
        metadata = json.loads(path.read_text("utf-8"))
        metadata["harness"]["files"] = {"tests/sim/backend.py": "changed"}
        path.write_text(json.dumps(metadata), "utf-8")
        with self.assertRaisesRegex(ValueError, "unreviewed simulator"):
            compare(self.old, self.new)

    def _migration_record(self, root, *, passed=True, input_value="flat"):
        root.mkdir()
        row = {"id": "async/example-1", "kind": "async", "input": input_value,
               "passed": passed, "exception": None,
               "signature": {"outcome": "success" if passed else "failed", "trace": []}}
        data = gzip.compress(json.dumps(row).encode(), mtime=0)
        (root / "case-0001.json.gz").write_bytes(data)
        summary = {"harness": {"files": {}}, "cases": [{"id": row["id"], "file": "case-0001.json.gz",
            "sha256": hashlib.sha256(data).hexdigest()}]}
        (root / "summary.json").write_text(json.dumps(summary), "utf-8")

    def test_coordination_failure_is_reported_separately_from_products(self):
        self._migration_record(self.old)
        self._migration_record(self.new, passed=False)
        result = compare_migration(self.old, self.new)
        self.assertEqual(result["failed"], ["async/example-1"])
        self.assertFalse(result["product_denominator"])

    def test_changed_coordinator_injection_is_not_a_paired_comparison(self):
        self._migration_record(self.old)
        self._migration_record(self.new, input_value="stairs")
        with self.assertRaisesRegex(ValueError, "inputs differ"):
            compare_migration(self.old, self.new)

    def test_migration_transport_preserves_original_hashes_and_bytes(self):
        self._migration_record(self.old)
        self._migration_record(self.new)
        path = self.old / 'case-0001.json.gz'
        compressed = path.read_bytes()
        raw = gzip.decompress(compressed)
        (self.old / 'case-0001.json').write_bytes(raw)
        path.unlink()
        transport = {'traces': {'case-0001.json.gz': {
            'archive_path': 'case-0001.json',
            'source_compressed_sha256': hashlib.sha256(compressed).hexdigest(),
            'decompressed_sha256': hashlib.sha256(raw).hexdigest()}}}
        (self.old / 'trace-transport.json').write_text(json.dumps(transport), 'utf-8')
        result = compare_migration(self.old, self.new)
        self.assertEqual(result['difference_count'], 0)
        self.assertEqual(result['pairs'], 1)
        (self.old / 'case-0001.json').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            compare_migration(self.old, self.new)

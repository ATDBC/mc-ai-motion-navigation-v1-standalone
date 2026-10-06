"""The D058 benchmark writes auditable evidence and refuses replacement."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class D058RouteRevalidationBenchmarkTests(unittest.TestCase):
    def test_cli_writes_schema_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="d058-benchmark-test-") as root:
            output = Path(root) / "evidence"
            command = [
                sys.executable,
                "-m",
                "scripts.benchmark_d058_route_revalidation",
                "--output",
                str(output),
                "--warmup",
                "1",
                "--samples",
                "2",
                "--f1-scenario",
                "straight_2_0",
                "--prepare-discard",
                "1",
                "--minimum-prepare-samples",
                "1",
                "--gc-diagnostic",
            ]
            completed = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True,
                timeout=120,
            )
            self.assertIn(completed.returncode, {0, 1}, completed.stderr)

            payload = json.loads(
                (output / "performance.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                payload["schema_version"],
                "mc2p.d058-route-revalidation-performance.v1",
            )
            self.assertEqual(
                set(payload["route_revalidation_groups"]),
                {
                    "nonempty_no_intersection",
                    "surface_edge_one_replay",
                    "standable_connection_one_replay",
                    "complex_shape_one_replay",
                    "single_incumbent_two_replays",
                    "incumbent_pending_four_replays",
                },
            )
            self.assertTrue(all(
                len(item["raw_samples_ns"]) == 2
                for item in payload["route_revalidation_groups"].values()
            ))
            self.assertGreaterEqual(
                payload["complete_prepare"]["retained_samples"], 1,
            )
            prepare = payload["complete_prepare"]
            ranges = prepare["scenario_sample_ranges"]
            self.assertEqual(len(ranges), 1)
            self.assertEqual(ranges[0]["scenario"], "straight_2_0")
            self.assertEqual(ranges[0]["scenario_ordinal"], 0)
            self.assertEqual(ranges[0]["captured_start_ordinal"], 0)
            self.assertEqual(
                ranges[0]["captured_end_ordinal"],
                prepare["captured_samples"] - 1,
            )
            self.assertEqual(
                ranges[0]["captured_samples"], prepare["captured_samples"],
            )
            self.assertEqual(ranges[0]["retained_start_ordinal"], 0)
            self.assertEqual(
                ranges[0]["retained_end_ordinal"],
                prepare["retained_samples"] - 1,
            )
            self.assertEqual(
                ranges[0]["retained_samples"], prepare["retained_samples"],
            )
            self.assertGreaterEqual(ranges[0]["gc_collected_after"], 0)
            gc_diagnostic = prepare["gc_diagnostic"]
            self.assertEqual(
                gc_diagnostic["event_count"],
                len(gc_diagnostic["events"]),
            )
            self.assertFalse(gc_diagnostic["overflowed"])
            self.assertEqual(
                gc_diagnostic["sample_ordinal_basis"],
                "zero_based_captured_prepare_sample",
            )
            self.assertEqual(
                gc_diagnostic["clock"], "time.perf_counter_ns",
            )
            for event in gc_diagnostic["events"]:
                self.assertLess(
                    event["sample_ordinal"], prepare["captured_samples"],
                )
                self.assertIn(event["generation"], {0, 1, 2})
                self.assertLessEqual(event["start_ns"], event["end_ns"])
                self.assertEqual(
                    event["duration_ns"],
                    event["end_ns"] - event["start_ns"],
                )
                self.assertGreaterEqual(event["collected"], 0)
            self.assertEqual(
                completed.returncode,
                0 if payload["gates"]["all_passed"] else 1,
            )
            self.assertIn(
                "mc2p/motion_nav/route_body_controller.py",
                {item["path"] for item in payload["source"]["files"]},
            )
            self.assertTrue((output / "COMMAND.txt").is_file())
            sums = {
                name: digest
                for digest, name in (
                    line.split("  ", 1)
                    for line in (output / "SHA256SUMS")
                        .read_text(encoding="utf-8").splitlines()
                )
            }
            self.assertEqual(
                sums,
                {
                    name: hashlib.sha256((output / name).read_bytes())
                        .hexdigest()
                    for name in ("COMMAND.txt", "performance.json")
                },
            )

            refused = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(refused.returncode, 2)
            self.assertIn("refusing to overwrite", refused.stderr)

            compatibility_output = Path(root) / "compatibility-evidence"
            compatibility_command = command[:-1]
            compatibility_command[
                compatibility_command.index(str(output))
            ] = str(compatibility_output)
            compatible = subprocess.run(
                compatibility_command,
                cwd=ROOT, capture_output=True, text=True, timeout=120,
            )
            self.assertIn(compatible.returncode, {0, 1}, compatible.stderr)
            compatible_payload = json.loads(
                (compatibility_output / "performance.json")
                    .read_text(encoding="utf-8")
            )
            self.assertNotIn(
                "gc_diagnostic", compatible_payload["complete_prepare"],
            )
            self.assertEqual(
                set(compatible_payload["configuration"]),
                {
                    "route_group_warmup_iterations",
                    "route_group_measured_iterations",
                    "prepare_discarded_prefix_samples",
                    "prepare_minimum_retained_samples",
                    "f1_scenarios",
                },
            )
            self.assertEqual(
                compatible_payload["complete_prepare"]["scenario_order"],
                prepare["scenario_order"],
            )
            self.assertEqual(
                compatible_payload["complete_prepare"]["scenario_results"],
                prepare["scenario_results"],
            )
            self.assertEqual(
                compatible_payload["complete_prepare"]["captured_samples"],
                prepare["captured_samples"],
            )
            self.assertEqual(
                compatible.returncode,
                0 if compatible_payload["gates"]["all_passed"] else 1,
            )


if __name__ == "__main__":
    unittest.main()

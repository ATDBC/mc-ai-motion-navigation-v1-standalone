import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


class F2RecoveryR0BaselineTests(unittest.TestCase):
    def test_v9_inputs_and_labels_are_frozen_without_control_expectations(self):
        from tests.sim.f2s_cases import (
            input_digest,
            materialized_manifest,
        )

        manifest = materialized_manifest()
        cases = manifest["support_region_cases"]
        expected = manifest["expected_inputs"]
        self.assertEqual(len(cases), 216)
        self.assertEqual(
            input_digest(tuple(cases)),
            expected["support_region_input_sha256"],
        )
        labels = manifest["frozen_no_route_labels"]
        self.assertEqual(len(labels), 41)
        self.assertEqual(len({row["id"] for row in labels}), 41)
        self.assertTrue(all(row["label"] in {
            "REFERENCE_REACHABLE", "PROVEN_UNREACHABLE", "UNRESOLVED",
        } for row in labels))
        failures = manifest["frozen_execution_failures"]
        self.assertEqual(
            len(failures["fixed_route_has_no_forward_control"]), 9,
        )
        self.assertEqual(len(failures["fixed_route_stalled"]), 5)
        self.assertNotIn("expected_outcome", manifest["support_region_inputs"])

    def test_worker_lifecycle_probe_covers_all_four_frozen_boundaries(self):
        from scripts.f2rec_r0_baseline import worker_lifecycle_probe

        probe = worker_lifecycle_probe()
        self.assertEqual(probe["probe_kind"], "dynamic_formal_path")
        self.assertEqual(set(probe["cases"]), {
            "ready_after_death",
            "cancel_backpressure",
            "pre_publish_invalidation",
            "runtime_close",
        })
        for result in probe["cases"].values():
            self.assertIn(result["status"], {"PASS", "RED"})
            self.assertIn(result["risk"], {
                "closed", "capacity_debt", "lifecycle_debt",
                "hard_lifecycle_risk",
            })
            self.assertTrue(result["evidence"])
            self.assertIsInstance(result["observed"], dict)
            self.assertTrue(result["observed"])
        self.assertEqual(
            probe["cases"]["ready_after_death"]["status"], "PASS",
        )
        self.assertEqual(
            probe["cases"]["pre_publish_invalidation"]["status"], "PASS",
        )
        self.assertEqual(
            probe["cases"]["cancel_backpressure"]["status"], "PASS",
        )
        self.assertEqual(
            probe["cases"]["runtime_close"]["status"], "PASS",
        )
        self.assertEqual(
            probe["cases"]["runtime_close"]["risk"], "lifecycle_debt",
        )
        self.assertFalse(probe["current_hard_risk"])
        self.assertTrue(
            probe["cases"]["ready_after_death"]["observed"]
            ["airborne_owner_retained"],
        )
        self.assertFalse(
            probe["cases"]["pre_publish_invalidation"]["observed"]
            ["retired_result_executable"],
        )
        self.assertTrue(
            probe["cases"]["cancel_backpressure"]["observed"]
            ["bounded_terminal_reached"],
        )
        self.assertTrue(
            probe["cases"]["runtime_close"]["observed"]
            ["neutral_release_sent"],
        )

    def test_v9_formal_probe_records_f2r_support_edge_red(self):
        # R0 is immutable historical evidence.  Later recovery stages are
        # expected to turn this RED green, so this gate reads the frozen R0
        # result instead of rerunning current production code.
        evidence = ROOT / (
            "evidence/motion_navigation/F2REC-recovery-v1/r0/v9-runs.jsonl"
        )
        result = next(
            record
            for record in (
                json.loads(line)
                for line in evidence.read_text("utf-8").splitlines()
            )
            if record["id"]
                == "f2s/platform_outer_corner/product/south/normal"
        )
        self.assertEqual(
            result["id"],
            "f2s/platform_outer_corner/product/south/normal",
        )
        self.assertTrue(result["standable_point_exists"])
        self.assertNotEqual(result["outcome"], "success")
        self.assertEqual(result["violations"], [])
        self.assertTrue(result["source_released"])

    def test_hotpath_probe_uses_three_fixed_routes_and_reports_medians(self):
        from scripts.f2rec_r0_baseline import collect_ground_hotpaths

        result = collect_ground_hotpaths(repeats=1)
        self.assertEqual(tuple(result["routes"]), (
            "straight", "turn", "wall_contact",
        ))
        for route in result["routes"].values():
            self.assertEqual(route["runs"], 1)
            self.assertGreater(route["frame_samples"], 0)
            self.assertGreaterEqual(route["median_control_ms"], 0.0)
            self.assertEqual(route["safety_events"], 0)

    def test_tool_provenance_hashes_the_runtime_tools_and_records_checkout(self):
        from scripts.f2rec_r0_baseline import collect_tool_provenance

        # R0 belongs to its recorded dirty tool set, not the checkout used to
        # review it.  Pin the historical record, including every tool digest.
        frozen_bytes = (ROOT / (
            "evidence/motion_navigation/F2REC-recovery-v1/r0/"
            "tool-provenance.json"
        )).read_bytes()
        self.assertEqual(
            hashlib.sha256(frozen_bytes).hexdigest(),
            "57da769896dd269e0a862c12bed413b956a3570217d4ce2e10680834ec3f4644",
        )
        frozen = json.loads(frozen_bytes)
        self.assertEqual(frozen["branch"], "codex/f2r-recovery")
        self.assertEqual(
            frozen["head_commit"],
            "a4e3bcf28d5c7aaf6dda195aec18a884b3b1fe14",
        )

        # A public main checkout must be recorded as main.  Exercise that path
        # even when this test is run from the source recovery branch.
        git_values = {
            ("branch", "--show-current"): "main",
            ("rev-parse", "HEAD"): "f" * 40,
            ("diff", "--name-only"): "",
            ("ls-files", "--others", "--exclude-standard"): "",
            ("worktree", "list", "--porcelain"): "worktree public-checkout",
        }
        with patch("scripts.f2rec_r0_baseline._git",
                   side_effect=lambda *args: git_values[args]):
            provenance = collect_tool_provenance()
        self.assertEqual(provenance["formal_platform"], "windows")
        self.assertEqual(provenance["branch"], "main")
        self.assertEqual(provenance["head_commit"], "f" * 40)
        self.assertEqual(provenance["checkout_kind"], "shared_primary_checkout")
        tools = provenance["runtime_tools"]
        self.assertEqual(set(tools), set(frozen["runtime_tools"]))
        self.assertIn("scripts/f2rec_r0_baseline.py", tools)
        for relative, digest in tools.items():
            self.assertEqual(
                digest, hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()

"""D125 keeps actor observation honest before adding a body acquisition."""
from __future__ import annotations

import tempfile
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from scripts import d125_three_fact_observation as probe


def _ensure_native_test_library() -> bool:
    """Build the Windows-only test dependency when a source-only export lacks it."""
    if os.name != "nt":
        return False
    from scripts.surface_depth_probe import build as native_build

    if native_build.NATIVE_LIBRARY.is_file():
        return False
    environment = {}
    bundled_java = (
        Path(sys.executable).resolve().parent / "Library/lib/jvm"
    )
    if ("JAVA_HOME" not in os.environ
            and (bundled_java / "bin/java.exe").is_file()
            and (bundled_java / "bin/javac.exe").is_file()):
        environment["JAVA_HOME"] = str(bundled_java)
    with patch.dict(os.environ, environment):
        native_build.build_native()
    return True


class D125ObservationFeasibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ensure_native_test_library()

    def test_windows_fixture_builds_native_library_only_when_missing(self):
        from scripts.surface_depth_probe import build as native_build

        with (patch.object(os, "name", "nt"),
              patch.object(native_build, "NATIVE_LIBRARY") as library,
              patch.object(native_build, "build_native") as build_native):
            library.is_file.return_value = True
            self.assertFalse(_ensure_native_test_library())
            build_native.assert_not_called()

            library.is_file.return_value = False
            self.assertTrue(_ensure_native_test_library())
            build_native.assert_called_once_with()

        with (patch.object(os, "name", "posix"),
              patch.object(native_build, "build_native") as build_native):
            self.assertFalse(_ensure_native_test_library())
            build_native.assert_not_called()

    def test_four_directions_classify_three_formal_lower_facts(self):
        for direction in range(4):
            with self.subTest(direction=direction):
                rows = probe.required_fact_roles(direction)
                self.assertEqual(len(rows), 3)
                self.assertEqual(sum(row["parent_required"] for row in rows), 2)
                self.assertEqual(sum(row["successor_required"] for row in rows), 2)
                self.assertEqual(sum(
                    row["parent_required"] and row["successor_required"]
                    for row in rows), 1)

    def test_safe_precommit_stance_sees_two_but_not_joint_parent_fact(self):
        for direction in range(4):
            with self.subTest(direction=direction):
                result = probe.visibility_evidence(direction)
                blocked = tuple(result["blocked_parent_cell"])
                self.assertEqual(
                    set(map(tuple, result["staging_witness"]["visible_cells"])),
                    set(map(tuple, result["required_cells"])) - {blocked},
                )
                self.assertEqual(result["safe_scan"]["blocked_visible_count"], 0)
                self.assertTrue(result["continuous_blocker"]["proven"])
                self.assertIn(blocked, set(map(tuple, result["middle_witness"]["visible_cells"])))
                self.assertEqual(
                    set(map(tuple, result["middle_witness"]["visible_cells"])),
                    set(map(tuple, result["required_cells"])),
                )

    def test_removing_middle_platform_makes_blocked_fact_visible(self):
        for direction in range(4):
            with self.subTest(direction=direction):
                result = probe.visibility_evidence(direction)
                self.assertTrue(result["middle_platform_removed_mutation"]["blocked_visible"])

    def test_continuous_blocker_certificate_keeps_every_boundary_inside(self):
        for direction in range(4):
            with self.subTest(direction=direction):
                certificate = probe.visibility_evidence(direction)[
                    "continuous_blocker"
                ]["inequality_certificate"]
                front = certificate["x_at_middle_front"]
                back = certificate["x_at_middle_back"]
                top = certificate["x_at_start_top_crossing"]
                self.assertGreater(front[0], -1.0)
                self.assertLess(front[1], 2.0)
                self.assertGreater(back[0], -1.0)
                self.assertLess(back[1], 2.0)
                self.assertLess(certificate["maximum_y_at_middle_back"], 64.0)
                self.assertGreater(top[0], -1.0)
                self.assertLess(top[1], 2.0)
                self.assertGreater(
                    certificate["minimum_z_at_start_top_crossing"], -12.0,
                )

    def test_partial_actor_facts_do_not_relax_parent_proof(self):
        for direction in range(4):
            with self.subTest(direction=direction):
                result = probe.formal_blocked_fact_gate(direction)
                self.assertEqual(result["offline_dependency_gate"], "NEEDS_WORLD")
                self.assertEqual(result["parent_status"], "needs_world")
                self.assertEqual(result["gate_unknown"], (result["blocked_parent_cell"],))
                self.assertFalse(result["unknown_to_air_inference_used"])

    def test_deleting_joint_parent_dependency_breaks_role_gate(self):
        original = probe.dependency.replay

        def delete_parent(case, changes):
            result = original(case, changes)
            if tuple(changes) == ((0, 62, 2),):
                result = dict(result)
                result["parent_status"] = "solved"
            return result

        probe.required_fact_roles.cache_clear()
        try:
            with patch.object(probe.dependency, "replay", side_effect=delete_parent):
                with self.assertRaisesRegex(ValueError, "proof roles differ"):
                    probe.required_fact_roles(0)
        finally:
            probe.required_fact_roles.cache_clear()

    def test_landing_edge_probe_vantage_is_after_the_gap(self):
        for direction in range(4):
            with self.subTest(direction=direction):
                result = probe.landing_probe_applicability(direction)
                self.assertFalse(result["applicable_from_start_support"])
                self.assertTrue(result["vantage_on_middle_platform"])
                self.assertFalse(result["can_authorize_precommit"])

    def test_run_writes_typed_stop_without_product_activity(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "d125"
            self.assertEqual(probe.run(output), 2)
            result = probe.read_json(output / "stop.json")
            self.assertEqual(result["status"], "PARTIAL_STOPPED")
            self.assertEqual(
                result["stop_trigger"]["code"],
                "PRECOMMIT_FACT_NOT_OBSERVABLE_FROM_SAFE_REACHABLE_VIEWPOINT",
            )
            self.assertEqual(result["safety"]["movement_trials_started"], 0)
            self.assertFalse(result["safety"]["fabric_started"])
            self.assertEqual(result["safety"]["production_files_modified"], 0)
            self.assertFalse(result["next_steps_authorized"])

    def test_published_stop_is_hash_bound_and_keeps_scope_closed(self):
        evidence = (
            probe.ROOT / "evidence/motion_navigation/"
            "d125-three-required-fact-observation-v1/d125-0/run-2"
        )
        manifest = json.loads((evidence / "manifest.json").read_text("utf-8"))
        self.assertEqual(
            hashlib.sha256((evidence / "stop.json").read_bytes()).hexdigest(),
            manifest["files"]["stop.json"],
        )
        result = json.loads((evidence / "stop.json").read_text("utf-8"))
        self.assertEqual(result["status"], "PARTIAL_STOPPED")
        self.assertEqual(len(result["directions"]), 4)
        self.assertTrue(all(
            row["visibility"]["safe_scan"]["blocked_visible_count"] == 0
            and row["visibility"]["continuous_blocker"]["proven"]
            and row["formal_gate"]["offline_dependency_gate"] == "NEEDS_WORLD"
            for row in result["directions"]
        ))
        self.assertEqual(result["safety"]["movement_trials_started"], 0)
        self.assertFalse(result["safety"]["fabric_started"])


if __name__ == "__main__":
    unittest.main()

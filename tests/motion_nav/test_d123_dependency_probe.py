"""D123-0 discrimination and frozen evidence checks; no Fabric."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import d123_dependency_probe as probe
from scripts import d123_dependency_analysis as analysis
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_types import TickInput
from mc2p.motion_nav.world_model import CellKnowledge, UNKNOWN_CELL
from dataclasses import replace


class D123DependencyProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = probe.fixed_case()

    def test_unknown_under_air_rejects_parent_and_successor(self):
        for position in ((0, 62, 0), (0, 62, 2), (0, 62, 5)):
            row = probe.replay(self.case, {position: UNKNOWN_CELL})
            self.assertIn("needs_world", (row["parent_status"], row["successor_binding_status"]), row)
            self.assertEqual(row["offline_dependency_gate"], "NEEDS_WORLD")
            self.assertIn(position, row["gate_unknown"])

    def test_full_cube_shadow_removes_equivalent_broad_candidate_from_current_gate(self):
        base = probe.replay(self.case, {})
        position = (0, 62, -6)
        for mode in ("air", "cube", "unknown"):
            row = probe.replay(self.case, {position: probe.fact_for(self.case[4], position, mode)})
            for key in ("main_signature", "exit_signature", "risk_signature", "fallback_behavior_signature", "damage"):
                self.assertEqual(base[key], row[key], (mode, key))
            self.assertNotIn(position, row["dependencies"])
            self.assertEqual(row["offline_dependency_gate"], "known")

    def test_hidden_material_does_not_act_through_full_cube_shadow(self):
        position = (0, 62, -6)
        ordinary = probe.replay(self.case, {
            position: probe.fact_for(self.case[4], position, "tall_ordinary")})
        boundary = probe.replay(self.case, {
            position: probe.fact_for(self.case[4], position, "tall_material_boundary")})
        self.assertEqual(ordinary["parent_status"], "solved")
        self.assertEqual(boundary["parent_status"], "solved")
        self.assertEqual(ordinary["main_signature"], boundary["main_signature"])
        self.assertNotIn(position, boundary["dependencies"])

    def test_body_inside_lower_space_restores_unknown_requirement(self):
        position, above = (0, 62, -6), (0, 63, -6)
        state = replace(self.case[2].entry_state, position=(.5, 62.9, -5.5),
                        on_ground=False, velocity_blocks_per_tick=(0., -.0784000015258789, 0.))
        world = probe.world_for(self.case[4], {
            position: UNKNOWN_CELL, above: probe.fact_for(self.case[4], above, "air")})
        result = step(state, TickInput(0., 0., False, False, False, state.yaw_radians),
                      world, probe.JAVA_1_21_RULESET)
        self.assertEqual(result.status.value, "needs_world")
        self.assertIn(position, result.missing_cells)

    def test_exact_d120_data_absent_never_becomes_small_success(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "missing"
            with self.assertRaisesRegex(FileNotFoundError, "exact D120 raw coverage unavailable"):
                probe.d120_comparison(source, Path(directory) / "output")

    def test_overlays_leave_frozen_source_unchanged(self):
        before = self.case[4].canonical_bytes
        position = (0, 62, 0)
        probe.replay(self.case, {position: UNKNOWN_CELL})
        self.assertEqual(before, self.case[4].canonical_bytes)
        self.assertEqual(dict(self.case[4].facts)[position].knowledge, CellKnowledge.AIR)


class D123EvidenceTests(unittest.TestCase):
    def test_final_classification_checks_successor_and_independent_material_reads(self):
        directory = probe.ROOT / "evidence/motion_navigation/d123-proof-and-transport-v1/d123-0/run-1"
        result = analysis.classify(directory)
        self.assertEqual(result["whole_chain_required_gap_cells"], 12)
        self.assertEqual(result["protected_candidates_without_removal_permission"], 36)
        self.assertEqual(result["material_counterexamples_in_parent_or_successor"], 36)
        self.assertEqual(result["removed_dependencies"], 0)
        for row in result["cells"]:
            self.assertFalse(row["removal_authorized"])
        late_only = next(row for row in result["cells"]
                         if row["direction"] == 0 and tuple(row["position"]) == (0, 62, 5))
        self.assertEqual(late_only["unknown_parent_status"], "solved")
        self.assertEqual(late_only["unknown_successor_status"], "needs_world")
        self.assertEqual(late_only["collision"], "required_observe")

    def test_current_evidence_hashes_and_972_exact_comparison(self):
        directory = probe.ROOT / "evidence/motion_navigation/d123-proof-and-transport-v1/d123-0/run-1"
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        for relative, expected in manifest["files"].items():
            self.assertEqual(hashlib.sha256((directory / relative).read_bytes()).hexdigest(), expected, relative)
        summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["d120"]["entrances"], 972)
        self.assertEqual(summary["d120"]["suffixes"], 2916)
        self.assertTrue(summary["d120"]["all_exactly_equal"])
        self.assertEqual(summary["d120"]["old_success_regressions"], 0)
        self.assertEqual(summary["status"], "STOPPED_INSUFFICIENT_EVIDENCE")
        self.assertEqual(summary["single_rows"], 144)
        self.assertGreater(summary["material_boundary_counterexamples"], 0)
        paths = json.loads((directory / "read-paths.json").read_text(encoding="utf-8"))
        for direction in range(4):
            self.assertEqual(len({tuple(r["position"]) for r in paths if r["direction"] == direction}), 12)
        for direction in range(4):
            original = json.loads((directory / f"d120-direction-{direction}.json").read_text(encoding="utf-8"))
            self.assertTrue(original["equal"])
            self.assertTrue(all(row["equal"] for row in original["sample_signatures"]))


if __name__ == "__main__":
    unittest.main()

"""Formal D122 dependency gate must reject known-but-mismatched fixture facts."""
from __future__ import annotations
import json
import unittest

from scripts import d123_dependency_gate_repair as repair
from scripts import d123_dependency_probe as probe


class D123DependencyGateRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = probe.fixed_case()
        cls.protected = (0, 62, 0)

    def test_protected_air_matches_expected_fixture_and_passes(self):
        outcome = repair.formal_gate(self.case, {
            self.protected: probe.fact_for(self.case[4], self.protected, "air")})
        self.assertEqual(outcome["status"], repair.DependencyGateStatus.PASS)
        self.assertTrue(outcome["accepted"])
        self.assertEqual((outcome["known_count"], outcome["unknown_count"], outcome["mismatch_count"]), (58, 0, 0))

    def test_protected_cube_is_known_but_mismatched_and_rejected(self):
        outcome = repair.formal_gate(self.case, {
            self.protected: probe.fact_for(self.case[4], self.protected, "cube")})
        self.assertEqual(outcome["status"], repair.DependencyGateStatus.FACT_MISMATCH)
        self.assertFalse(outcome["accepted"])
        self.assertEqual((outcome["known_count"], outcome["unknown_count"], outcome["mismatch_count"]), (57, 0, 1))
        self.assertEqual(outcome["mismatched"][0]["position"], self.protected)

    def test_protected_unknown_is_not_mismatch_or_air_and_rejected(self):
        outcome = repair.formal_gate(self.case, {self.protected: probe.UNKNOWN_CELL})
        self.assertEqual(outcome["status"], repair.DependencyGateStatus.NEEDS_WORLD)
        self.assertFalse(outcome["accepted"])
        self.assertEqual((outcome["known_count"], outcome["unknown_count"], outcome["mismatch_count"]), (57, 1, 0))
        self.assertEqual(outcome["unknown"], (self.protected,))

    def test_adjacent_pair_cube_retains_both_mismatches(self):
        pair = ((0, 62, 0), (0, 62, 2))
        outcome = repair.formal_gate(self.case, {
            position: probe.fact_for(self.case[4], position, "cube") for position in pair})
        self.assertEqual(outcome["status"], repair.DependencyGateStatus.FACT_MISMATCH)
        self.assertEqual({r["position"] for r in outcome["mismatched"]}, set(pair))

    def test_unknown_and_mismatch_have_distinct_typed_combined_status(self):
        outcome = repair.formal_gate(self.case, {
            (0, 62, 0): probe.UNKNOWN_CELL,
            (0, 62, 2): probe.fact_for(self.case[4], (0, 62, 2), "cube")})
        self.assertEqual(outcome["status"], repair.DependencyGateStatus.NEEDS_WORLD_AND_FACT_MISMATCH)
        self.assertEqual((outcome["known_count"], outcome["unknown_count"], outcome["mismatch_count"]), (56, 1, 1))

    def test_d124_removed_lower_fact_is_not_a_current_gate_dependency(self):
        removed = (0, 62, -6)
        outcome = repair.formal_gate(self.case, {
            removed: probe.fact_for(self.case[4], removed, "cube")})
        self.assertEqual(outcome["status"], repair.DependencyGateStatus.PASS)
        self.assertNotIn(removed, {tuple(row["position"]) for row in outcome["mismatched"]})

    def test_repair_covers_every_existing_raw_row_without_new_experiments(self):
        root = probe.ROOT / "evidence/motion_navigation/d123-proof-and-transport-v1/d123-0"
        raw = json.loads((root / "run-1/mutations.json").read_text(encoding="utf-8"))
        repaired = json.loads((root / "repair-1/formal-dependency-gates.json").read_text(encoding="utf-8"))
        self.assertEqual(len(repaired["rows"]), len(raw))
        self.assertEqual(len(raw), 396)
        for index, row in enumerate(repaired["rows"]):
            self.assertEqual(row["source_row_index"], index)
            self.assertEqual(row["kind"], raw[index]["kind"])
            gate = row["formal_dependency_gate"]
            self.assertEqual(gate["known_count"] + gate["unknown_count"] + gate["mismatch_count"], 67)
            self.assertEqual(gate["accepted"], gate["unknown_count"] == gate["mismatch_count"] == 0)
        cubes = [r for r in repaired["rows"] if r["kind"] == "single" and r["mode"] == "cube"]
        self.assertEqual(len(cubes), 48)
        self.assertTrue(all(r["historical_knowledge_only_gate"] == "known" and
                            r["formal_dependency_gate"]["status"] == "FACT_MISMATCH" for r in cubes))


if __name__ == "__main__":
    unittest.main()

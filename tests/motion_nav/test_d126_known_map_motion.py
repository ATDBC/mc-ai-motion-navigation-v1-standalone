"""D126 keeps known-map motion tests separate from visual sensing."""
from __future__ import annotations

from dataclasses import replace
import inspect
from types import SimpleNamespace
import unittest

from mc2p.contracts.observation_v3 import (
    AirQueryResultV3,
    CollisionShapeV3,
    ObservedBlockV3,
)
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter, TEST_ORACLE
from mc2p.motion_nav.world_model import BlockGeometry, CellKnowledge
from mc2p.motion_nav.world_snapshot import DependencyStatus
from scripts import d122_fabric_timing as probe
from scripts import d118_momentum_calibration_runtime as frozen
from scripts.d120_momentum_entry_calibration import build_fixture
from tests.motion_nav.test_d059_consumers_and_identity import _snapshot
from tests.observation_v3_fixtures import valid_snapshot_v3


class D126KnownMapMotionTests(unittest.TestCase):
    def _runtime(self):
        adapter = NavigationObservationAdapter()
        adapter.ingest(_snapshot(1))
        return SimpleNamespace(navigation_observation_adapter=adapter)

    def test_four_directions_seed_runtime_owner_with_all_current_dependencies(self):
        for direction in range(4):
            with self.subTest(direction=direction):
                runtime = self._runtime()
                fixture = build_fixture(direction)
                known = probe.known_fixture_map(
                    fixture.trial, origin=(30, 40), feet_y=64,
                )
                frame, report = probe.seed_runtime_known_fixture_map(runtime, known)
                manifest = probe.fabric_dependency_manifest(
                    direction, origin=(30, 40), feet_y=64,
                )
                gate = probe.inspect_fabric_dependency_knowledge(frame.world, manifest)
                self.assertEqual(
                    (gate["required_count"], gate["known_count"], gate["unknown"], gate["mismatched"]),
                    (58, 58, (), ()),
                )
                self.assertIs(
                    frame.world._owner,
                    runtime.navigation_observation_adapter.snapshot_world_port,
                )
                self.assertEqual(report["source"], "test_oracle_fixture_map")
                self.assertEqual(report["rows_sha256"], known["rows_sha256"])

    def test_rows_are_applied_in_server_order_and_support_overrides_clear(self):
        fixture = build_fixture(0)
        known = probe.known_fixture_map(
            fixture.trial, origin=(10, 20), feet_y=64,
        )
        rows = frozen.trial_fixture_rows(
            fixture.trial, origin=(10, 20), feet_y=64,
        )
        support = tuple(tuple(position) for row in rows if row["phase"] == "support"
                        for position in row["positions"])
        self.assertTrue(set(support).issubset(known["blocks"]))
        self.assertFalse(set(support).intersection(known["air"]))
        self.assertEqual(known["server_rows"], probe.canonical_fixture_rows(rows))

        invalid = ({
            "phase": "unknown",
            "material": "minecraft:air",
            "positions": ((0, 64, 0),),
        },)
        with self.assertRaises(ValueError):
            probe.canonical_fixture_rows(invalid)

    def test_missing_joint_fact_and_wrong_material_still_fail_existing_gate(self):
        fixture = build_fixture(0)
        known = probe.known_fixture_map(fixture.trial, origin=(0, 0), feet_y=64)
        manifest = probe.fabric_dependency_manifest(0, origin=(0, 0), feet_y=64)
        joint = next(position for position in manifest["facts"]
                     if position[1] == 62 and position[2] == 2)

        missing_runtime = self._runtime()
        missing = probe.without_known_fixture_position(known, joint)
        missing_frame, _ = probe.seed_runtime_known_fixture_map(missing_runtime, missing)
        missing_gate = probe.inspect_fabric_dependency_knowledge(missing_frame.world, manifest)
        self.assertEqual(missing_gate["unknown"], (joint,))

        wrong_runtime = self._runtime()
        wrong = probe.with_known_fixture_block(
            known, joint, BlockGeometry.full_cube("minecraft:stone"),
        )
        wrong_frame, _ = probe.seed_runtime_known_fixture_map(wrong_runtime, wrong)
        wrong_gate = probe.inspect_fabric_dependency_knowledge(wrong_frame.world, manifest)
        self.assertEqual(tuple(row["position"] for row in wrong_gate["mismatched"]),
                         (joint,))

    def test_seeding_a_temporary_adapter_does_not_prepare_runtime_owner(self):
        runtime = self._runtime()
        fixture = build_fixture(0)
        known = probe.known_fixture_map(fixture.trial, origin=(0, 0), feet_y=64)
        temporary = NavigationObservationAdapter()
        temporary.ingest(_snapshot(1))
        temporary.seed_test_oracle_memory(TEST_ORACLE, known["blocks"], known["air"])
        manifest = probe.fabric_dependency_manifest(0, origin=(0, 0), feet_y=64)
        current = runtime.navigation_observation_adapter.latest_frame
        self.assertIsNotNone(current)
        gate = probe.inspect_fabric_dependency_knowledge(current.world, manifest)
        self.assertGreater(len(gate["unknown"]), 0)

    def test_later_occluded_result_does_not_erase_known_map_fact(self):
        runtime = self._runtime()
        fixture = build_fixture(0)
        known = probe.known_fixture_map(fixture.trial, origin=(0, 0), feet_y=64)
        frame, _ = probe.seed_runtime_known_fixture_map(runtime, known)
        position = next(iter(known["air"]))
        self.assertIs(frame.world.cell(position).knowledge, CellKnowledge.AIR)
        newer = _snapshot(2)
        perception = newer.perception.value
        newer = replace(
            newer,
            perception=replace(
                newer.perception,
                value=replace(
                    perception,
                    air_query_results=(AirQueryResultV3(position, "occluded"),),
                ),
            ),
        )
        after = runtime.navigation_observation_adapter.ingest(newer)
        self.assertIs(after.world.cell(position).knowledge, CellKnowledge.AIR)
        versions = after.world._owner.dependency_versions((position,))

        changed = valid_snapshot_v3(
            sequence=3,
            blocks=(ObservedBlockV3(
                position,
                "minecraft:stone",
                CollisionShapeV3("full_cube"),
                None,
                ("surface_depth",),
            ),),
        )
        runtime.navigation_observation_adapter.ingest(changed)
        result = after.world._owner.dependency_changes(versions)

        self.assertIs(result.status, DependencyStatus.CHANGED)
        self.assertEqual(result.changed_positions, (position,))

    def test_oracle_boundary_and_runner_use_the_runtime_owner(self):
        runtime = self._runtime()
        fixture = build_fixture(0)
        known = probe.known_fixture_map(fixture.trial, origin=(0, 0), feet_y=64)
        with self.assertRaises(ContractViolation):
            runtime.navigation_observation_adapter.seed_test_oracle_memory(
                object(), known["blocks"], known["air"],
            )
        source = inspect.getsource(probe.run_fabric_runtime)
        self.assertNotIn("NavigationObservationAdapter()", source)
        self.assertIn("seed_runtime_known_fixture_map", source)
        self.assertNotIn("fabric_observation_positions(", source)

    def test_oracle_seed_requires_an_existing_frame_and_disjoint_facts(self):
        fixture = build_fixture(0)
        known = probe.known_fixture_map(fixture.trial, origin=(0, 0), feet_y=64)
        fresh = NavigationObservationAdapter()
        with self.assertRaises(ContractViolation):
            fresh.seed_test_oracle_memory(
                TEST_ORACLE, known["blocks"], known["air"],
            )

        runtime = self._runtime()
        position = next(iter(known["blocks"]))
        with self.assertRaises(ContractViolation):
            runtime.navigation_observation_adapter.seed_test_oracle_memory(
                TEST_ORACLE, known["blocks"], (*known["air"], position),
            )

    def test_production_runtime_and_skill_modules_never_use_test_oracle(self):
        for root in (probe.ROOT / "mc2p/runtime", probe.ROOT / "mc2p/skills"):
            for path in root.rglob("*.py"):
                source = path.read_text("utf-8")
                self.assertNotIn("seed_test_oracle_memory", source, path)
                self.assertNotIn("TEST_ORACLE", source, path)


if __name__ == "__main__":
    unittest.main()

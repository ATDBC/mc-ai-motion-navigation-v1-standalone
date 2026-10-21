"""D123-1: preflight and actor calibration may never enter movement trials."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from scripts import d123_actor_calibration as probe
from scripts import d122_fabric_timing as d122
from scripts import d123_dependency_probe as dependency


class D123ActorCalibrationTests(unittest.TestCase):
    def test_product_modes_cannot_launch_while_transport_is_red(self):
        launch = Mock()
        for mode in ("smoke", "matrix", "anything"):
            with self.subTest(mode=mode):
                self.assertEqual(probe.dispatch(mode, {"status": "PASS"}, launch), 2)
        launch.assert_not_called()

    def test_failed_offline_gate_cannot_launch_calibration(self):
        launch = Mock()
        self.assertEqual(probe.dispatch("calibration-only", {"status": "FAIL"}, launch), 2)
        launch.assert_not_called()

    def test_calibration_launch_receives_no_trial_capability(self):
        launch = Mock(return_value=0)
        self.assertEqual(probe.dispatch("calibration-only", {"status": "PASS"}, launch), 0)
        launch.assert_called_once_with()

    def test_d123_historical_fixture_retains_67_and_all_12_lower_cells(self):
        historical = json.loads((
            probe.ROOT / "evidence/motion_navigation/d123-proof-and-transport-v1/"
            "d123-0/required-facts.json"
        ).read_text("utf-8"))
        self.assertEqual(len(historical), 4)
        for row in historical:
            self.assertEqual(len(row["facts"]), 67)
            self.assertEqual(sum(position[1] == 62 for position, _ in row["facts"]), 12)

    def test_all_four_directions_use_d124_current_58_and_three_lower_cells(self):
        for direction in range(4):
            manifest = d122.fabric_dependency_manifest(direction, origin=(0, 0), feet_y=64)
            self.assertEqual(len(manifest["facts"]), 58)
            self.assertEqual(sum(p[1] == 62 for p in manifest["facts"]), 3)
            request = probe.calibration_request(direction, origin=(0, 0), feet_y=64)
            self.assertTrue(set(manifest["facts"]).issubset(request.air_positions))

    def test_actor_unknown_and_known_mismatch_are_typed_and_keep_positions(self):
        case = dependency.fixed_case()
        manifest = d122.fabric_dependency_manifest(0, origin=(0, 0), feet_y=64)
        protected = (0, 62, 0)
        class World:
            def cell(self, position):
                return dependency.UNKNOWN_CELL if position == protected else dict(case[4].facts)[position]
        result = probe.inspect_actor_gate(World(), manifest)
        self.assertEqual(result["status"], "NEEDS_WORLD")
        self.assertEqual(result["unknown_count"], 1)
        self.assertEqual(result["unknown"], (protected,))

    def test_static_gate_rejects_airborne_moving_or_unhealthy_actor(self):
        body = SimpleNamespace(is_on_ground=True, velocity_blocks_per_second=(0., 0., 0.), health_points=20.)
        self.assertEqual(probe.static_gate(body, 20.), ())
        for name, value, expected in (("is_on_ground", False, "not_grounded"),
                                      ("velocity_blocks_per_second", (.2, 0., 0.), "not_still"),
                                      ("health_points", 19., "health_changed")):
            changed = deepcopy(body)
            setattr(changed, name, value)
            self.assertIn(expected, probe.static_gate(changed, 20.))

    def test_calibration_bound_expires_on_either_clock(self):
        self.assertFalse(probe.within_bounds(100, 100, 0, 0))
        self.assertFalse(probe.within_bounds(0, probe.MAX_CALIBRATION_WALL_NS, 0, 0))
        self.assertFalse(probe.within_bounds(probe.MAX_CALIBRATION_TICKS, 0, 0, 0))
        self.assertTrue(probe.within_bounds(1, 1, 0, 0))

    def test_offline_semantic_proof_or_export_failure_happens_before_launch(self):
        for gate in ("environment", "semantic_hash", "proof_fact", "standalone_file"):
            with self.subTest(gate=gate):
                events = []
                def preflight():
                    events.append(gate)
                    return {"status": "FAIL", "failed_gate": gate}
                launch = Mock(side_effect=lambda: events.append("minecraft"))
                self.assertEqual(probe.preflight_then_dispatch("calibration-only", preflight, launch), 2)
                self.assertEqual(events, [gate])
                launch.assert_not_called()

    def test_existing_formal_adapter_can_accumulate_two_observations(self):
        from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
        from mc2p.contracts.observation_v3 import ObservedBlockV3, CollisionShapeV3
        from tests.observation_v3_fixtures import valid_snapshot_v3
        adapter = NavigationObservationAdapter()
        air = ObservedBlockV3((1, 64, 0), "minecraft:air", CollisionShapeV3("empty"), None, ("air_query",))
        block = ObservedBlockV3((2, 64, 0), "minecraft:stone", CollisionShapeV3("full_cube"), None, ("surface_depth",))
        adapter.ingest(valid_snapshot_v3(blocks=(air,), sequence=1))
        final = adapter.ingest(valid_snapshot_v3(blocks=(block,), sequence=2))
        self.assertEqual(final.world.cell((1, 64, 0)).knowledge.value, "air")
        self.assertEqual(final.world.cell((2, 64, 0)).knowledge.value, "block")

    def test_continuous_support_plane_argument_requires_opaque_support_and_eye_above(self):
        eye, cell, rect = (.5, 65.62, -5.17), (0, 62, -6), (-1, -12, 2, 0)
        self.assertTrue(probe.support_plane_blocks_cell(eye, cell, rect, 64))
        self.assertFalse(probe.support_plane_blocks_cell(eye, cell, (2, -12, 3, 0), 64))
        self.assertFalse(probe.support_plane_blocks_cell((.5, 62.5, -5.17), cell, rect, 64))

    def test_four_rotations_have_a_full_cube_blocker_for_current_dependencies(self):
        for direction in range(4):
            geometry = probe.fixed_eye_geometry(direction)
            self.assertEqual(len(geometry["required"]), 58)
            self.assertEqual(len(geometry["lower"]), 3)
            self.assertTrue(geometry["continuous_blocked"])


if __name__ == "__main__":
    unittest.main()

"""Detached vanilla collector tests: no game, network, renderer or actor world edits."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mc2p.backends.client_observation_payload_v3 import decode_client_observation_payload_v3
from tests.observation_v3_fixtures import surface_payload_value, encoded

ROOT = Path(__file__).resolve().parents[1]


class ClientBlockObservationV3Tests(unittest.TestCase):
    def test_entity_visibility_reuses_surface_geometry_and_rule_table(self):
        collector = (ROOT / (
            "mc2p/backends/runtime_overlays/mc121_observation/"
            "ClientObservationCollector.java"
        )).read_text("utf-8")
        self.assertIn("surfaceVisibleBoxes", collector)
        self.assertIn("SurfaceVisibilityRules.classify", collector)
        self.assertIn('"surface_bbox_exact_16"', collector)
        self.assertIn('"surface_rules_five_point_16_32"', collector)
        self.assertNotIn("new RaycastContext", collector)

    def test_air_query_never_reads_hidden_world_state_directly(self):
        source = (ROOT / (
            "mc2p/backends/runtime_overlays/mc121_observation/"
            "ClientBlockObservationV3.java"
        )).read_text("utf-8")
        self.assertNotIn("discoverRequestedAir", source)
        self.assertNotIn("getBlockState(position).isAir", source)
        self.assertIn("SurfaceFrame surface=surfaceProvider.sample", source)

    def test_surface_sensor_refuses_unloaded_air_candidates(self):
        source = (ROOT / (
            "mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/"
            "SurfaceSensor.java"
        )).read_text("utf-8")
        self.assertIn("client.world.isChunkLoaded(position)", source)
        self.assertIn("position.getY() >= client.world.getBottomY()", source)
        self.assertIn("position.getY() < client.world.getTopY()", source)

    def test_surface_air_failures_are_reported_with_typed_statuses(self):
        observation = (ROOT / (
            "mc2p/backends/runtime_overlays/mc121_observation/"
            "ClientBlockObservationV3.java"
        )).read_text("utf-8")
        sensor = (ROOT / (
            "mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/"
            "SurfaceSensor.java"
        )).read_text("utf-8")
        self.assertIn("enum AirStatus", observation)
        self.assertIn('addProperty("status",result.status().wireName)', observation)
        self.assertIn("AirStatus.OUTSIDE_VIEW", sensor)
        self.assertIn("AirStatus.OUT_OF_RANGE", sensor)
        self.assertIn("AirStatus.OCCLUDED", sensor)
        self.assertIn("AirStatus.UNAVAILABLE", sensor)

    def test_surface_provider_must_classify_every_requested_air_cell_once(self):
        source = (ROOT / (
            "mc2p/backends/runtime_overlays/mc121_observation/"
            "ClientBlockObservationV3.java"
        )).read_text("utf-8")
        self.assertIn("classifiedAir.addAll(surface.visualAir())", source)
        self.assertIn("classifiedAir.equals(requestedAir)", source)

    def test_native_discovery_dedup_shape_targeting_and_python_parity(self):
        from tests.test_visible_equipment_projection import VisibleEquipmentProjectionTests
        source = ROOT / "mc2p/backends/runtime_overlays/mc121_observation/ClientBlockObservationV3.java"
        self.assertTrue(source.is_file(), "V3 shared collector has not been implemented")
        with TemporaryDirectory(prefix="mc2p-block-v3-") as directory:
            output = Path(directory) / "native.json"
            VisibleEquipmentProjectionTests()._run_java_harness(
                "ClientBlockObservationV3Test", "CLIENT_BLOCK_OBSERVATION_V3_OK", args=(str(output),))
            results = json.loads(output.read_text("utf-8"))
            missing = decode_client_observation_payload_v3(encoded(results["missing_frame"]))
            self.assertEqual(missing.generation_id, 2)
            self.assertIsNone(missing.self_state.value)
            self.assertEqual(missing.targeting.reason_code, "not_requested")
            value = surface_payload_value()
            value["perception"]["value"]["blocks"] = results["blocks"]
            decoded = decode_client_observation_payload_v3(encoded(value))
            self.assertEqual(len(decoded.perception.value.blocks), 1)
            self.assertEqual(decoded.perception.value.blocks[0].sources, ("body_contact", "surface_depth"))
            value = surface_payload_value()
            value["perception"]["value"]["blocks"] = results["air_blocks"]
            decoded = decode_client_observation_payload_v3(encoded(value))
            self.assertEqual(decoded.perception.value.blocks[0].block_id, "minecraft:air")
            self.assertEqual(decoded.perception.value.blocks[0].sources, ("air_query",))
            value = surface_payload_value()
            value["perception"]["value"]["blocks"] = results["body_air_blocks"]
            decoded = decode_client_observation_payload_v3(encoded(value))
            self.assertEqual(
                [(block.position, block.block_id, block.sources) for block in decoded.perception.value.blocks],
                [
                    ((0, 64, 0), "minecraft:air", ("body_contact",)),
                    ((0, 65, 0), "minecraft:air", ("body_contact",)),
                ],
            )
            value = surface_payload_value("interaction_v1")
            value["perception"]["value"]["blocks"] = results["target_blocks"]
            value["targeting"] = results["targeting"]
            self.assertEqual(decode_client_observation_payload_v3(encoded(value)).targeting.value.face, "north")

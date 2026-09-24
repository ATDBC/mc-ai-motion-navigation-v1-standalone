"""Strict byte/value decoding, profile echo and cross-group coherence."""
import importlib
import unittest
from dataclasses import replace

from mc2p.backends.client_observation_payload import ClientObservationPayloadError, decode_client_observation_payload
from mc2p.contracts.common import ContractViolation, FieldStatusV0
from tests.observation_v2_fixtures import valid_payload_bytes as v2_bytes
from tests.observation_v3_fixtures import (
    block_value, encoded, tracked_entity_value, valid_payload_value,
)


class ClientObservationPayloadV3Tests(unittest.TestCase):
    def setUp(self):
        try:
            self.api = importlib.import_module("mc2p.backends.client_observation_payload_v3")
        except ModuleNotFoundError as error:
            if error.name != "mc2p.backends.client_observation_payload_v3":
                raise
            self.fail("V3 decoder implementation is missing")

    def decode(self, value):
        return self.api.decode_client_observation_payload_v3(encoded(value))

    def project(self, decoded, **changes):
        args = dict(episode_id="v3-test", request_sequence_id=0,
                    request_started_at_monotonic_ns=100, received_at_monotonic_ns=200,
                    controller_clock_id="controller-test", source_backend="fixture")
        args.update(changes)
        return self.api.snapshot_v3_from_payload(decoded, **args)

    def test_navigation_decodes_only_minimal_block_data(self):
        value = valid_payload_value()
        value["perception"]["value"]["blocks"] = [block_value()]
        decoded = self.decode(value)
        self.assertEqual(decoded.perception.value.blocks[0].collision.kind, "full_cube")
        self.assertEqual(decoded.targeting.reason_code, "not_requested")
        self.assertEqual(decoded.field_profile, "navigation_v1")
        self.assertEqual(self.api.decode_client_observation_value_v3(value), decoded)

    def test_self_hurt_and_movement_tick_use_the_shared_strict_contract(self):
        value = valid_payload_value()
        value["self_state"]["value"].update(
            hurt_animation_ticks=9,
            movement_tick_id=41,
        )
        own = self.decode(value).self_state.value
        self.assertEqual((own.hurt_animation_ticks, own.movement_tick_id), (9, 41))

    def test_absent_blocks_are_not_synthesized(self):
        self.assertEqual(self.decode(valid_payload_value()).perception.value.blocks, ())

    def test_tracked_living_entity_is_strictly_decoded(self):
        value = valid_payload_value("interaction_v1")
        value["tracked_entity"] = dict(
            status="valid", sample_world_tick=100,
            source_kind="client_registered_entity", reason_code=None,
            value=tracked_entity_value(),
        )
        decoded = self.decode(value)
        self.assertEqual(decoded.tracked_entity.value.track_id, "entity-world-7")
        self.assertEqual(decoded.tracked_entity.value.health_points, 20.0)
        self.assertFalse(decoded.tracked_entity.value.is_dead)
        self.assertEqual(self.project(decoded).tracked_entity, decoded.tracked_entity)

    def test_tracked_entity_rejects_missing_death_and_invalid_health(self):
        for mutate in (
            lambda item: item.pop("is_dead"),
            lambda item: item.update(health_points=-1.0),
            lambda item: item.update(health_points=21.0),
            lambda item: item.update(is_loaded=False),
        ):
            value = valid_payload_value("interaction_v1")
            entity = tracked_entity_value()
            mutate(entity)
            value["tracked_entity"] = dict(
                status="valid", sample_world_tick=100,
                source_kind="client_registered_entity", reason_code=None, value=entity,
            )
            with self.subTest(entity=entity), self.assertRaises(ClientObservationPayloadError):
                self.decode(value)

    def test_extra_fields_and_ray_shells_are_rejected(self):
        for key in ("block_light", "sky_light", "outline", "state_properties", "face", "ray_id", "pov"):
            value = valid_payload_value()
            block = block_value()
            block[key] = None
            value["perception"]["value"]["blocks"] = [block]
            with self.subTest(key=key), self.assertRaises(ClientObservationPayloadError):
                self.decode(value)
        value = valid_payload_value()
        value["perception"]["value"]["block_rays"] = []
        with self.assertRaises(ClientObservationPayloadError):
            self.decode(value)

    def test_duplicates_and_unordered_blocks_are_rejected(self):
        for blocks in ([block_value(), block_value()], [block_value((1,63,0)), block_value((0,63,0))]):
            value = valid_payload_value()
            value["perception"]["value"]["blocks"] = blocks
            with self.subTest(blocks=blocks), self.assertRaises(ClientObservationPayloadError):
                self.decode(value)

    def test_geometry_is_preserved_not_clipped(self):
        value = valid_payload_value()
        block = block_value()
        block["collision"] = dict(kind="boxes", boxes=[[-.25, 0, 0, 1.25, 1.5, 1]], reason=None)
        value["perception"]["value"]["blocks"] = [block]
        box = self.decode(value).perception.value.blocks[0].collision.boxes[0]
        self.assertEqual((box.min_x, box.max_x, box.max_y), (-.25, 1.25, 1.5))

    def test_malformed_wire_geometry_is_rejected(self):
        for boxes in ([[0,0,0,1,1]], [[0,0,0,1,1,1,2]], [[0,0,0,0,1,1]], [[False,0,0,1,1,1]]):
            value = valid_payload_value()
            block = block_value()
            block["collision"] = dict(kind="boxes", boxes=boxes, reason=None)
            value["perception"]["value"]["blocks"] = [block]
            with self.subTest(boxes=boxes), self.assertRaises(ClientObservationPayloadError):
                self.decode(value)

    def test_missing_not_requested_unsupported_and_miss_are_distinct(self):
        nav = self.decode(valid_payload_value())
        interaction = self.decode(valid_payload_value("interaction_v1"))
        self.assertIsNone(nav.targeting.value)
        self.assertEqual(interaction.targeting.value.hit_kind, "miss")
        value = valid_payload_value("interaction_v1")
        value["targeting"].update(status="unsupported", reason_code="target_query_unavailable", value=None)
        self.assertIs(self.decode(value).targeting.status, FieldStatusV0.UNSUPPORTED)

    def test_profile_cannot_keep_unrequested_targeting(self):
        value = valid_payload_value("interaction_v1")
        value["field_profile"] = "navigation_v1"
        with self.assertRaises(ClientObservationPayloadError):
            self.decode(value)
        value = valid_payload_value()
        value["field_profile"] = "interaction_v1"
        with self.assertRaises(ClientObservationPayloadError):
            self.decode(value)

    def test_interaction_accepts_only_current_query_availability(self):
        for status in ("stale", "invalid"):
            value = valid_payload_value("interaction_v1")
            value["targeting"].update(status=status, reason_code="old_target", value=None)
            with self.subTest(status=status), self.assertRaises(ClientObservationPayloadError):
                self.decode(value)
    def test_target_block_must_have_corresponding_authorized_block(self):
        value = valid_payload_value("interaction_v1")
        value["targeting"]["value"] = dict(hit_kind="block", block_position=[0,63,0], entity_ref=None,
            face="up", hit_position=dict(x=.5,y=64,z=.5), distance_blocks=1.62)
        with self.assertRaises(ClientObservationPayloadError):
            self.decode(value)
        value["perception"]["value"]["blocks"] = [block_value(sources=("current_target", "first_hit_ray"))]
        self.assertEqual(self.decode(value).targeting.value.block_position, (0,63,0))

    def test_target_entity_cannot_reference_a_hidden_entity(self):
        value = valid_payload_value("interaction_v1")
        value["targeting"]["value"] = dict(hit_kind="entity", block_position=None, entity_ref="hidden",
            face=None, hit_position=dict(x=1,y=64,z=1), distance_blocks=2.)
        with self.assertRaises(ClientObservationPayloadError):
            self.decode(value)

    def test_navigation_cannot_claim_current_target_source(self):
        value = valid_payload_value()
        value["perception"]["value"]["blocks"] = [block_value(sources=("current_target",))]
        with self.assertRaises(ClientObservationPayloadError):
            self.decode(value)

    def test_sensor_profile_cannot_silently_change(self):
        for key, bad in (("ray_columns", 15), ("ray_rows", 9.), ("sensor_profile_revision", 2),
                         ("horizontal_fov_degrees", 90), ("body_expansion_blocks", .1),
                         ("knowledge_model", "surface_v2")):
            value = valid_payload_value()
            value["perception"]["value"][key] = bad
            with self.subTest(key=key), self.assertRaises(ClientObservationPayloadError):
                self.decode(value)

    def test_source_cardinality_limits_are_checked(self):
        for source, count in (("body_contact", 513), ("first_hit_ray", 1432)):
            value = valid_payload_value()
            value["perception"]["value"]["blocks"] = [block_value((i,63,0), sources=(source,)) for i in range(count)]
            with self.subTest(source=source), self.assertRaises(ClientObservationPayloadError):
                self.decode(value)

    def test_invalid_bytes_json_and_size_are_rejected(self):
        raw = encoded(valid_payload_value())
        cases = [raw.decode(), b"\xff", b"{}", b"[]", raw + b"x", b" " * 1_048_577,
                 raw.replace(b'"generation_id":0', b'"generation_id":0,"generation_id":0'),
                 raw.replace(b'"generation_id":0', b'"generation_id":NaN'),
                 raw.replace(b'"generation_id":0', b'"generation_id":true'),
                 raw.replace(b'"max_block_distance":16.0', b'"max_block_distance":1e999')]
        for index, case in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(ClientObservationPayloadError):
                self.api.decode_client_observation_payload_v3(case)

    def test_schema_versions_do_not_cross_decode(self):
        with self.assertRaises(ClientObservationPayloadError):
            self.api.decode_client_observation_payload_v3(v2_bytes())
        with self.assertRaises(ClientObservationPayloadError):
            decode_client_observation_payload(encoded(valid_payload_value()))

    def test_tick_clock_and_availability_consistency(self):
        for key in ("self_state", "inventory", "gui", "perception", "targeting", "tracked_entity"):
            value = valid_payload_value()
            value[key]["sample_world_tick"] = 99
            with self.subTest(key=key), self.assertRaises(ClientObservationPayloadError):
                self.decode(value)
        for change in (dict(clock_id=""), dict(started_at_monotonic_ns=True), dict(completed_at_monotonic_ns=1)):
            value = valid_payload_value()
            value["client_sample"].update(change)
            with self.subTest(change=change), self.assertRaises(ClientObservationPayloadError):
                self.decode(value)

    def test_input_dict_mutation_cannot_change_decoded_knowledge(self):
        value = valid_payload_value()
        value["perception"]["value"]["blocks"] = [block_value()]
        decoded = self.api.decode_client_observation_value_v3(value)
        value["perception"]["value"]["blocks"][0]["position"][0] = 123
        self.assertEqual(decoded.perception.value.blocks[0].position, (0,63,0))

    def test_projection_preserves_unavailable_self_fields(self):
        value = valid_payload_value()
        value["self_state"].update(status="missing", reason_code="no_player", value=None)
        obs = self.project(self.decode(value))
        self.assertIs(obs.position.status, FieldStatusV0.MISSING)
        self.assertIsNone(obs.position.value)
        self.assertEqual(obs.position.detail, "no_player")
        self.assertIsNone(obs.is_on_ground.value)

    def test_projection_rejects_v2_payload_and_same_clock(self):
        with self.assertRaises(ContractViolation):
            self.project(decode_client_observation_payload(v2_bytes()))
        with self.assertRaises(ContractViolation):
            self.project(self.decode(valid_payload_value()), controller_clock_id="jvm-test")

    def test_unknown_collision_is_distinct_from_empty_and_fluid_is_kept(self):
        for kind, reason in (("empty", None), ("unsupported", "shape_unavailable")):
            value = valid_payload_value()
            block = block_value()
            block.update(collision=dict(kind=kind, boxes=[], reason=reason), fluid_id="minecraft:water")
            value["perception"]["value"]["blocks"] = [block]
            decoded = self.decode(value).perception.value.blocks[0]
            self.assertEqual((decoded.collision.kind, decoded.fluid_id), (kind, "minecraft:water"))

    def test_entity_target_uses_current_filtered_reference_only(self):
        value = valid_payload_value("interaction_v1")
        value["perception"]["value"]["visible_entities"] = [dict(track_id="visible-1",
            entity_type="minecraft:player", display_name=None, relative_position=dict(x=1.,y=0.,z=0.),
            relative_velocity=dict(x=0.,y=0.,z=0.), relative_yaw_degrees=0., pitch_degrees=0.,
            bounding_box_size=dict(x=.6,y=1.8,z=.6), pose="standing", is_on_ground=True, equipment=[])]
        value["targeting"]["value"] = dict(hit_kind="entity", block_position=None, entity_ref="visible-1",
            face=None, hit_position=dict(x=1.5,y=65,z=.5), distance_blocks=1.)
        self.assertEqual(self.decode(value).targeting.value.entity_ref, "visible-1")
        value["perception"]["value"]["visible_entities"][0]["server_entity_id"] = 77
        with self.assertRaises(ClientObservationPayloadError):
            self.decode(value)

    def test_entity_limits_and_truncation_are_checked(self):
        value = valid_payload_value()
        value["perception"]["value"]["entities_truncated"] = True
        with self.assertRaises(ClientObservationPayloadError):
            self.decode(value)
        value = valid_payload_value()
        value["perception"]["value"]["visible_entities"] = [{}] * 65
        with self.assertRaises(ClientObservationPayloadError):
            self.decode(value)

    def test_exact_size_boundary_is_accepted_not_truncated(self):
        raw = encoded(valid_payload_value())
        padded = raw + b" " * (1_048_576 - len(raw))
        self.assertEqual(self.api.decode_client_observation_payload_v3(padded).generation_id, 0)
        with self.assertRaises(ClientObservationPayloadError):
            self.api.decode_client_observation_payload_v3(padded + b" ")

    def test_direct_payload_constructor_preserves_group_invariants(self):
        payload = self.decode(valid_payload_value())
        for change in (dict(generation_id=-1), dict(sample_world_tick=True), dict(field_profile="all"),
                       dict(client_sample={}), dict(targeting=replace(payload.targeting, reason_code="old_cache"))):
            with self.subTest(change=change), self.assertRaises(ContractViolation):
                replace(payload, **change)

    def test_nonvalid_groups_cannot_smuggle_values_or_wrong_sources(self):
        for name in ("self_state", "inventory", "gui", "perception", "targeting"):
            value = valid_payload_value()
            value[name].update(status="missing", reason_code="missing", value={})
            with self.subTest(name=name), self.assertRaises(ClientObservationPayloadError):
                self.decode(value)
        value = valid_payload_value()
        value["targeting"]["source_kind"] = "client_player"
        with self.assertRaises(ClientObservationPayloadError):
            self.decode(value)

    def test_nested_duplicate_key_and_deep_json_fail_closed(self):
        raw = encoded(valid_payload_value())
        duplicate = raw.replace(b'"ray_rows":9', b'"ray_rows":9,"ray_rows":9')
        for case in (duplicate, b"[" * 2000 + b"0" + b"]" * 2000):
            with self.subTest(size=len(case)), self.assertRaises(ClientObservationPayloadError):
                self.api.decode_client_observation_payload_v3(case)


if __name__ == "__main__":
    unittest.main()

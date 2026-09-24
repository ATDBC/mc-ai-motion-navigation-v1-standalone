from __future__ import annotations

import unittest

from mc2p.backends.client_observation_payload import (
    ClientObservationPayloadError,
    decode_client_observation_payload,
    extract_length_delimited_field,
    snapshot_v2_from_craftground,
)
from mc2p.contracts.observation_v2 import ObservationSnapshotV2
from tests.observation_v2_fixtures import (
    valid_payload_bytes, valid_payload_value, visible_entity_value,
)
import json
from dataclasses import replace
from mc2p.contracts.common import ContractViolation


def _varint(value: int) -> bytes:
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            result.append(byte | 0x80)
        else:
            result.append(byte)
            return bytes(result)


def _field(number: int, wire_type: int, payload: bytes) -> bytes:
    return _varint((number << 3) | wire_type) + payload


class ProtobufFieldScannerTests(unittest.TestCase):
    def test_extracts_one_field_50000_while_skipping_known_wire_types(self) -> None:
        payload = b'{"schema_version":"mc2p.client_observation.v2"}'
        message = b"".join(
            (
                _field(1, 0, _varint(7)),
                _field(2, 1, b"12345678"),
                _field(3, 2, _varint(3) + b"abc"),
                _field(4, 5, b"1234"),
                _field(50000, 2, _varint(len(payload)) + payload),
            )
        )

        self.assertEqual(extract_length_delimited_field(message), payload)

    def test_rejects_missing_duplicate_wrong_type_and_truncated_fields(self) -> None:
        payload = b"v2"
        valid = _field(50000, 2, _varint(len(payload)) + payload)
        cases = {
            "missing": _field(1, 0, _varint(1)),
            "duplicate": valid + valid,
            "wire type": _field(50000, 0, _varint(1)),
            "truncated": _field(50000, 2, _varint(3) + b"x"),
        }
        for label, message in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(ClientObservationPayloadError):
                    extract_length_delimited_field(message)

    def test_rejects_payload_over_limit(self) -> None:
        payload = b"abcd"
        message = _field(50000, 2, _varint(len(payload)) + payload)

        with self.assertRaisesRegex(ClientObservationPayloadError, "limit"):
            extract_length_delimited_field(message, max_bytes=3)

    def test_rejects_overlong_tag_varint(self) -> None:
        with self.assertRaisesRegex(ClientObservationPayloadError, "varint"):
            extract_length_delimited_field(b"\x80" * 10 + b"\x00")


class ClientObservationJsonTests(unittest.TestCase):
    def test_visible_entity_hurt_animation_is_strict_and_legacy_missing_is_readable(self) -> None:
        current = valid_payload_value()
        current["perception"]["value"]["visible_entities"] = [
            visible_entity_value(hurt_animation_ticks=7)
        ]
        decoded = decode_client_observation_payload(json.dumps(current).encode("utf-8"))
        self.assertEqual(decoded.perception.value.visible_entities[0].hurt_animation_ticks, 7)

        legacy = valid_payload_value()
        legacy_entity = visible_entity_value()
        del legacy_entity["hurt_animation_ticks"]
        legacy["perception"]["value"]["visible_entities"] = [legacy_entity]
        decoded = decode_client_observation_payload(json.dumps(legacy).encode("utf-8"))
        self.assertIsNone(decoded.perception.value.visible_entities[0].hurt_animation_ticks)

        for invalid in (None, True, 21):
            with self.subTest(invalid=invalid):
                value = valid_payload_value()
                value["perception"]["value"]["visible_entities"] = [
                    visible_entity_value(hurt_animation_ticks=invalid)
                ]
                with self.assertRaises(ClientObservationPayloadError):
                    decode_client_observation_payload(json.dumps(value).encode("utf-8"))

    def test_backend_neutral_projection_preserves_wire_values_and_clock_domains(self) -> None:
        import mc2p.backends.client_observation_payload as module
        self.assertTrue(hasattr(module, "snapshot_v2_from_payload"), "backend-neutral projection is missing")
        payload = valid_payload_bytes(generation_id=3, sample_world_tick=321)
        decoded = decode_client_observation_payload(payload)
        arguments = dict(episode_id="episode-1", request_sequence_id=2,
                         request_started_at_monotonic_ns=10, received_at_monotonic_ns=20,
                         controller_clock_id="controller-test")
        snapshot = module.snapshot_v2_from_payload(decoded, source_backend="fabric", **arguments)
        self.assertEqual(snapshot.sequence_id, 3)
        self.assertEqual(snapshot.world_time_ticks.value, 321)
        self.assertEqual(snapshot.position.value.x, 1.0)
        self.assertEqual(snapshot.source_backend, "fabric")
        self.assertEqual(snapshot.controller_clock_id, "controller-test")
        self.assertNotEqual(snapshot.client_sample.clock_id, snapshot.controller_clock_id)
        self.assertIsNone(snapshot.server_state_age_ns.value)
        message = _field(50000, 2, _varint(len(payload)) + payload)
        old = snapshot_v2_from_craftground(message, sequence_id=3, world_time_ticks=321, **arguments)
        self.assertEqual(old, replace(snapshot, source_backend="craftground"))

    def test_builds_live_v2_snapshot_from_wire_message(self) -> None:
        payload = valid_payload_bytes(generation_id=3, sample_world_tick=321)
        message = _field(1, 0, _varint(321)) + _field(
            50000,
            2,
            _varint(len(payload)) + payload,
        )

        snapshot = snapshot_v2_from_craftground(
            message,
            episode_id="episode-1",
            sequence_id=3,
            request_sequence_id=2,
            request_started_at_monotonic_ns=10,
            received_at_monotonic_ns=20,
            controller_clock_id="controller-test",
            world_time_ticks=321,
            privileged_fields_present=("surrounding_blocks",),
        )

        self.assertIsInstance(snapshot, ObservationSnapshotV2)
        self.assertEqual(snapshot.position.value.x, 1.0)
        self.assertEqual(snapshot.is_dead.value, False)
        self.assertEqual(snapshot.privileged_fields_present, ("surrounding_blocks",))

    def test_craftground_wrapper_still_rejects_coerced_expected_sequence_or_tick(self) -> None:
        from mc2p.contracts.common import ContractViolation
        payload = valid_payload_bytes(generation_id=1, sample_world_tick=1)
        message = _field(50000, 2, _varint(len(payload)) + payload)
        args = dict(episode_id="episode-1", sequence_id=1, world_time_ticks=1,
                    request_sequence_id=0, request_started_at_monotonic_ns=10,
                    received_at_monotonic_ns=20, controller_clock_id="controller-test")
        for name in ("sequence_id", "world_time_ticks"):
            for value in (True, 1.0, None):
                with self.subTest(name=name, value=value):
                    with self.assertRaises((ContractViolation, ClientObservationPayloadError)):
                        snapshot_v2_from_craftground(message, **(args | {name: value}))

    def test_decodes_exact_valid_payload(self) -> None:
        decoded = decode_client_observation_payload(
            valid_payload_bytes(generation_id=4, sample_world_tick=321),
            expected_generation_id=4,
            expected_world_tick=321,
        )

        self.assertEqual(decoded.schema_version, "mc2p.client_observation.v2")
        self.assertEqual(decoded.generation_id, 4)
        self.assertEqual(decoded.sample_world_tick, 321)
        self.assertEqual(decoded.self_state.value.position.x, 1.0)
        self.assertFalse(decoded.self_state.value.is_dead)
        self.assertEqual(decoded.self_state.value.hurt_animation_ticks, 0)
        self.assertEqual(decoded.self_state.value.movement_tick_id, 1)
        self.assertEqual(len(decoded.inventory.value.main), 36)
        self.assertFalse(decoded.gui.value.open)
        self.assertEqual(len(decoded.perception.value.block_rays), 135)

    def test_rejects_duplicate_and_unknown_json_keys(self) -> None:
        duplicate = b'{"schema_version":"mc2p.client_observation.v2","schema_version":"wrong"}'
        unknown_value = valid_payload_value()
        unknown_value["image"] = "forbidden"
        unknown = json.dumps(unknown_value).encode("utf-8")

        with self.assertRaisesRegex(ClientObservationPayloadError, "duplicate"):
            decode_client_observation_payload(duplicate)
        with self.assertRaisesRegex(ClientObservationPayloadError, "keys"):
            decode_client_observation_payload(unknown)

    def test_rejects_generation_tick_utf8_and_nonfinite_values(self) -> None:
        with self.assertRaisesRegex(ClientObservationPayloadError, "generation"):
            decode_client_observation_payload(
                valid_payload_bytes(generation_id=2),
                expected_generation_id=3,
            )
        with self.assertRaisesRegex(ClientObservationPayloadError, "world tick"):
            decode_client_observation_payload(
                valid_payload_bytes(sample_world_tick=100),
                expected_world_tick=101,
            )
        with self.assertRaisesRegex(ClientObservationPayloadError, "UTF-8"):
            decode_client_observation_payload(b"\xff")
        nonfinite = valid_payload_bytes().replace(b'"yaw_degrees":90.0', b'"yaw_degrees":NaN')
        with self.assertRaisesRegex(ClientObservationPayloadError, "JSON"):
            decode_client_observation_payload(nonfinite)

    def test_rejects_boolean_where_numeric_value_is_required(self) -> None:
        value = valid_payload_value()
        value["self_state"]["value"]["health_points"] = True

        with self.assertRaisesRegex(ClientObservationPayloadError, "number"):
            decode_client_observation_payload(json.dumps(value).encode("utf-8"))

    def test_legacy_self_payload_does_not_invent_no_damage(self) -> None:
        value = valid_payload_value()
        own = value["self_state"]["value"]
        own.pop("hurt_animation_ticks")
        own.pop("movement_tick_id")

        decoded = decode_client_observation_payload(json.dumps(value).encode("utf-8"))

        self.assertIsNone(decoded.self_state.value.hurt_animation_ticks)
        self.assertIsNone(decoded.self_state.value.movement_tick_id)

    def test_self_hurt_and_movement_tick_must_appear_together(self) -> None:
        for missing in ("hurt_animation_ticks", "movement_tick_id"):
            with self.subTest(missing=missing):
                value = valid_payload_value()
                value["self_state"]["value"].pop(missing)
                with self.assertRaisesRegex(ClientObservationPayloadError, "together"):
                    decode_client_observation_payload(json.dumps(value).encode("utf-8"))

    def test_rejects_invalid_self_hurt_and_movement_tick(self) -> None:
        cases = (
            ("hurt_animation_ticks", 21),
            ("hurt_animation_ticks", True),
            ("movement_tick_id", -1),
            ("movement_tick_id", 1.5),
        )
        for name, invalid in cases:
            with self.subTest(name=name, invalid=invalid):
                value = valid_payload_value()
                value["self_state"]["value"][name] = invalid
                with self.assertRaises((ClientObservationPayloadError, ContractViolation)):
                    decode_client_observation_payload(json.dumps(value).encode("utf-8"))


if __name__ == "__main__":
    unittest.main()

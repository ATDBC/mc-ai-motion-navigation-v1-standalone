from dataclasses import asdict, fields, replace
import json
import re
import unittest
from unittest.mock import patch

import mc2p.contracts.common as contract_common
import mc2p.contracts.observation_request_v3 as request_contract
from mc2p.contracts.common import ContractViolation
from mc2p.contracts.observation_request_v3 import (
    MAX_AIR_QUERY_POSITIONS,
    MAX_OBSERVATION_REQUEST_BYTES,
    ObservationRequestV3,
    merge_observation_requests,
    observation_request_wire_upper_bound,
    resolve_observation_request,
)


class D103ObservationHotPathTests(unittest.TestCase):
    @staticmethod
    def largest_request() -> ObservationRequestV3:
        minimum = -(2 ** 31)
        return ObservationRequestV3(
            "interaction_v1",
            tuple((minimum + index, minimum, minimum) for index in range(128)),
            "x" * 128,
        )

    def test_public_construction_does_not_encode_for_capacity(self) -> None:
        with patch("json.dumps", wraps=json.dumps) as encode:
            request = self.largest_request()
        self.assertEqual(len(request.air_positions), 128)
        self.assertEqual(encode.call_count, 0)

    def test_unchanged_merge_reuses_a_validated_value(self) -> None:
        complete = ObservationRequestV3(
            "interaction_v1", ((1, 64, 0), (2, 64, 0)), "entity-1",
        )
        same = ObservationRequestV3(
            "interaction_v1", ((2, 64, 0), (1, 64, 0)), "entity-1",
        )
        subset = ObservationRequestV3("navigation_v1", ((1, 64, 0),))
        for requests in ((complete, same), (subset, complete), (complete, subset)):
            with self.subTest(requests=requests):
                result = merge_observation_requests(requests)
                self.assertEqual(result, complete)
                self.assertTrue(any(result is request for request in requests))
        self.assertIs(resolve_observation_request(
            "mc2p.client_observation.v3", None,
        ), resolve_observation_request("mc2p.client_observation.v3", None))

    def test_wire_upper_bound_is_bound_to_public_field_rules(self) -> None:
        request = self.largest_request()
        self.assertEqual(
            tuple(value.name for value in fields(request)),
            ("field_profile", "air_positions", "entity_track_id", "schema_version"),
        )
        self.assertEqual(request_contract._FIELD_PROFILES,
                         ("navigation_v1", "interaction_v1"))
        self.assertEqual(request_contract._MAX_ENTITY_TRACK_ID_LENGTH, 128)
        self.assertEqual(
            (request_contract._MIN_GRID_AXIS, request_contract._MAX_GRID_AXIS),
            (-(2 ** 31), 2 ** 31 - 1),
        )
        self.assertEqual(MAX_AIR_QUERY_POSITIONS, 128)
        self.assertEqual(MAX_OBSERVATION_REQUEST_BYTES, 16384)
        identifier_rule = contract_common._IDENTIFIER_PATTERN
        self.assertIs(type(identifier_rule), re.Pattern)
        self.assertEqual(identifier_rule.pattern, r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$")
        self.assertEqual(identifier_rule.flags, re.UNICODE)
        wire = json.dumps(asdict(request), separators=(",", ":")).encode("utf-8")
        self.assertEqual(len(wire), 5112)
        self.assertEqual(observation_request_wire_upper_bound(), 5112)

    def test_invalid_identifier_characters_cannot_break_the_bound(self) -> None:
        invalid = ("entity\\", 'entity"', "实体", "entityK")
        invalid += tuple("entity" + chr(value) for value in range(32))
        for entity_id in invalid:
            with self.subTest(entity_id=repr(entity_id)), \
                    self.assertRaises(ContractViolation):
                ObservationRequestV3(entity_track_id=entity_id)

    def test_bound_guard_detects_identifier_rule_relaxation(self) -> None:
        largest = self.largest_request()
        original_pattern = contract_common._IDENTIFIER_PATTERN
        mutations = (
            (re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/\\-]*$'), "\\"),
            (re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/"-]*$'), '"'),
            (re.compile(original_pattern.pattern, re.IGNORECASE), "K"),
        )
        for pattern, character in mutations:
            with self.subTest(character=repr(character)), \
                    patch.object(contract_common, "_IDENTIFIER_PATTERN", pattern):
                request = replace(largest, entity_track_id="A" + character * 127)
                wire = json.dumps(asdict(request), separators=(",", ":")).encode("utf-8")
                self.assertGreater(len(wire), observation_request_wire_upper_bound())


if __name__ == "__main__":
    unittest.main()

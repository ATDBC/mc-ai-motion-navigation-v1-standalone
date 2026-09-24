"""V3 value boundaries: no surface gating, malformed geometry or implicit knowledge."""
import importlib
import unittest
from dataclasses import FrozenInstanceError, replace

from mc2p.contracts.common import ContractViolation, FieldValueV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import ObservationSnapshotV2, ObservationGroupV2, VisibleEntityV2
from tests.observation_v3_fixtures import valid_snapshot_v3


class ObservationV3ContractTests(unittest.TestCase):
    def setUp(self):
        try:
            self.api = importlib.import_module("mc2p.contracts.observation_v3")
            self.request = importlib.import_module("mc2p.contracts.observation_request_v3")
        except ModuleNotFoundError as error:
            if error.name not in {"mc2p.contracts.observation_v3", "mc2p.contracts.observation_request_v3"}:
                raise
            self.fail("V3 contract implementation is missing")

    def block(self, **changes):
        return replace(self.api.ObservedBlockV3((0, 63, 0), "minecraft:stone",
            self.api.CollisionShapeV3("full_cube"), None, ("first_hit_ray",)), **changes)

    def test_only_two_explicit_profiles_are_accepted(self):
        for profile in ("navigation_v1", "interaction_v1"):
            self.assertEqual(self.request.ObservationRequestV3(profile).field_profile, profile)
        for invalid in (None, True, [], "all", "navigation", ""):
            with self.subTest(invalid=invalid), self.assertRaises(ContractViolation):
                self.request.ObservationRequestV3(invalid)

    def test_aabb_retains_outside_unit_cell_geometry(self):
        box = self.api.AabbV3(-.25, 0, .25, 1.25, 1.5, .75)
        self.assertEqual((box.min_x, box.max_y), (-.25, 1.5))

    def test_invalid_aabb_is_not_accepted_as_empty(self):
        for values in ((0, 0, 0, 0, 1, 1), (2, 0, 0, 1, 1, 1),
                       (True, 0, 0, 1, 1, 1), (float("nan"), 0, 0, 1, 1, 1),
                       (0, 0, 0, float("inf"), 1, 1)):
            with self.subTest(values=values), self.assertRaises(ContractViolation):
                self.api.AabbV3(*values)

    def test_shape_forms_cannot_contradict_their_geometry(self):
        box = self.api.AabbV3(0, 0, 0, 1, .5, 1)
        for args in (("boxes", ()), ("full_cube", (box,)), ("empty", (), "unknown"),
                     ("unsupported", (), None), ("unsupported", (box,), "not_supported"),
                     ("boxes", (box, box)), ("boxes", [box]), ("solid", ())):
            with self.subTest(args=args), self.assertRaises(ContractViolation):
                self.api.CollisionShapeV3(*args)
        self.assertEqual(self.api.CollisionShapeV3("unsupported", reason="not_supported").kind, "unsupported")

    def test_block_requires_unique_lawful_sources_and_integer_grid(self):
        for position in ((0., 63, 0), (False, 63, 0), (0, 63), [0, 63, 0]):
            with self.subTest(position=position), self.assertRaises(ContractViolation):
                self.block(position=position)
        for sources in ((), ("cached_world",), ("first_hit_ray", "first_hit_ray"),
                        ("first_hit_ray", "body_contact"), ["first_hit_ray"]):
            with self.subTest(sources=sources), self.assertRaises(ContractViolation):
                self.block(sources=sources)

    def test_state_is_immutable_and_contains_no_face_permission(self):
        block = self.block(sources=("body_contact", "first_hit_ray"))
        with self.assertRaises(FrozenInstanceError):
            block.fluid_id = "minecraft:water"
        self.assertEqual(set(block.__dataclass_fields__),
                         {"position", "block_id", "collision", "fluid_id", "sources"})

    def test_snapshot_is_not_a_v2_subclass_or_image_wrapper(self):
        obs = valid_snapshot_v3(blocks=(self.block(),))
        self.assertNotIsInstance(obs, ObservationSnapshotV2)
        self.assertEqual(obs.schema_version, "mc2p.observation.v3")
        self.assertEqual(obs.perception.value.blocks[0].position, (0, 63, 0))
        self.assertNotIn("block_rays", obs.perception.value.__dataclass_fields__)
        self.assertNotIn("pov", obs.__dataclass_fields__)
        self.assertEqual(obs.tracked_entity.reason_code, "not_requested")

    def test_tracked_entity_contract_keeps_health_death_and_identity_separate(self):
        entity = self.api.TrackedEntityStateV3(
            "entity-world-7", "minecraft:zombie",
            Vec3V0(2, 0, 1), Vec3V0(.1, 0, 0), 15, 0,
            Vec3V0(.6, 1.95, .6), "standing", True, True, False, 20, 20,
        )
        self.assertEqual((entity.health_points, entity.is_dead), (20, False))
        for change in (
            dict(track_id=""), dict(is_loaded=False), dict(health_points=-1),
            dict(health_points=21), dict(max_health_points=0), dict(is_dead=1),
        ):
            with self.subTest(change=change), self.assertRaises(ContractViolation):
                replace(entity, **change)

    def test_snapshot_rejects_identity_time_and_projection_mismatch(self):
        obs = valid_snapshot_v3()
        changes = (dict(episode_id=""), dict(sequence_id=True), dict(request_sequence_id=-1),
            dict(controller_clock_id="jvm-test"), dict(received_at_monotonic_ns=1),
            dict(world_time_ticks=FieldValueV0.valid(True)), dict(position=FieldValueV0.valid(Vec3V0(4, 4, 4))),
            dict(server_state_age_ns=FieldValueV0.valid(0)), dict(privileged_fields_present=("z", "a")),
            dict(gui=replace(obs.gui, sample_world_tick=99)))
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ContractViolation):
                replace(obs, **change)

    def test_direct_snapshot_rejects_wrong_group_types_and_sources(self):
        obs = valid_snapshot_v3()
        for change in (dict(gui=replace(obs.gui, value=obs.inventory.value)),
                       dict(perception=replace(obs.perception, source_kind="client_inventory")),
                       dict(targeting=replace(obs.targeting, source_kind="client_player"))):
            with self.subTest(change=change), self.assertRaises(ContractViolation):
                replace(obs, **change)

    def test_missing_self_does_not_manufacture_valid_position(self):
        obs = valid_snapshot_v3()
        with self.assertRaises(ContractViolation):
            replace(obs, self_state=ObservationGroupV2.missing(101, "client_player", "no_player"))

    def test_target_kind_has_exact_parameters(self):
        target = self.api.TargetingStateV3("block", (0, 63, 0), None, "up", Vec3V0(.5, 64, .5), 1.62)
        for changes in (dict(entity_ref="entity-1"), dict(face=None), dict(hit_position=None),
                        dict(distance_blocks=-1), dict(hit_kind="miss"), dict(face="diagonal")):
            with self.subTest(changes=changes), self.assertRaises(ContractViolation):
                replace(target, **changes)

    def test_entity_sorting_handles_finite_values_and_rejects_bad_order(self):
        near = VisibleEntityV2("near", "minecraft:cow", None, Vec3V0(1,0,0), Vec3V0(0,0,0),
                               0, 0, Vec3V0(.9,1.4,.9), "standing", True, ())
        far = replace(near, track_id="far", relative_position=Vec3V0(1e308,0,0))
        perception = valid_snapshot_v3().perception.value
        try:
            result = replace(perception, visible_entities=(near, far))
        except OverflowError:
            self.fail("finite entity coordinates must not cause a raw arithmetic exception")
        self.assertEqual(tuple(e.track_id for e in result.visible_entities), ("near", "far"))
        for entities in ((far, near), (near, near)):
            with self.subTest(refs=tuple(e.track_id for e in entities)), self.assertRaises(ContractViolation):
                replace(perception, visible_entities=entities)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from dataclasses import asdict, fields, replace
import json
import unittest

from mc2p.contracts.common import ContractViolation, FieldStatusV0, FieldValueV0
from mc2p.contracts.observation import Vec3V0
from mc2p.contracts.observation_v2 import (
    ClientSampleTimingV2,
    BlockRayV2,
    BodyContactV2,
    GuiSlotV2,
    GuiStateV2,
    InventoryStateV2,
    ItemStackV2,
    ObservationGroupV2,
    ObservationSnapshotV2,
    PerceptionStateV2,
    SelfStateV2,
    StatusEffectV2,
    VisibleEntityV2,
)


def _empty_item() -> ItemStackV2:
    return ItemStackV2(
        empty=True,
        item_id=None,
        count=0,
        damage=0,
        max_damage=0,
        damageable=False,
        custom_name=None,
    )


def _self_state() -> SelfStateV2:
    return SelfStateV2(
        position=Vec3V0(1.0, 2.0, 3.0),
        velocity=Vec3V0(0.0, 0.0, 0.0),
        yaw_degrees=90.0,
        pitch_degrees=0.0,
        head_yaw_degrees=90.0,
        body_yaw_degrees=90.0,
        is_dead=False,
        is_on_ground=True,
        horizontal_collision=False,
        vertical_collision=True,
        pose="standing",
        is_sprinting=False,
        is_sneaking=False,
        is_swimming=False,
        is_submerged_in_water=False,
        is_climbing=False,
        is_fall_flying=False,
        is_burning=False,
        fall_distance_blocks=0.0,
        air_ticks=300,
        max_air_ticks=300,
        health_points=20.0,
        max_health_points=20.0,
        absorption_points=0.0,
        armor_points=0,
        food_points=20,
        saturation_points=5.0,
        experience_level=0,
        experience_progress=0.0,
        total_experience=0,
        attack_cooldown=1.0,
        hurt_animation_ticks=0,
        movement_tick_id=1,
        active_hand=None,
        is_using_item=False,
        item_use_ticks_remaining=0,
        status_effects=(),
        game_mode="creative",
        is_flying=False,
        allow_flying=True,
    )


def _inventory() -> InventoryStateV2:
    empty = _empty_item()
    return InventoryStateV2(
        main=tuple(empty for _ in range(36)),
        selected_hotbar_slot=0,
        head=empty,
        chest=empty,
        legs=empty,
        feet=empty,
        offhand=empty,
        main_hand=empty,
    )


def _rays() -> tuple[BlockRayV2, ...]:
    result: list[BlockRayV2] = []
    for row in range(9):
        for column in range(15):
            result.append(
                BlockRayV2(
                    ray_id=row * 15 + column,
                    row=row,
                    column=column,
                    yaw_offset_degrees=-45.0 + column * (90.0 / 14.0),
                    pitch_offset_degrees=-30.0 + row * (60.0 / 8.0),
                    hit_kind="miss",
                    distance_blocks=16.0,
                )
            )
    return tuple(result)


def _perception() -> PerceptionStateV2:
    return PerceptionStateV2(
        horizontal_fov_degrees=90.0,
        vertical_fov_degrees=60.0,
        ray_columns=15,
        ray_rows=9,
        max_block_distance=16.0,
        body_expansion_blocks=0.05,
        block_epsilon_blocks=0.001,
        entity_max_distance=32.0,
        entity_occlusion_epsilon_blocks=0.05,
        block_rays=_rays(),
        body_contacts=(),
        visible_entities=(),
        entities_truncated=False,
        truncated_entity_count=0,
    )


def make_snapshot(**overrides: object) -> ObservationSnapshotV2:
    self_state = _self_state()
    values: dict[str, object] = {
        "episode_id": "episode-1",
        "sequence_id": 0,
        "request_sequence_id": None,
        "request_started_at_monotonic_ns": 10,
        "received_at_monotonic_ns": 20,
        "controller_clock_id": "controller-test",
        "client_sample": ClientSampleTimingV2("jvm-test", 9000, 9010),
        "world_time_ticks": FieldValueV0.valid(100),
        "position": FieldValueV0.valid(self_state.position),
        "yaw_degrees": FieldValueV0.valid(self_state.yaw_degrees),
        "pitch_degrees": FieldValueV0.valid(self_state.pitch_degrees),
        "is_on_ground": FieldValueV0.valid(self_state.is_on_ground),
        "is_dead": FieldValueV0.valid(False),
        "health_points": FieldValueV0.valid(self_state.health_points),
        "food_points": FieldValueV0.valid(float(self_state.food_points)),
        "self_state": ObservationGroupV2.valid(100, "client_player", self_state),
        "inventory": ObservationGroupV2.valid(
            100, "client_inventory", _inventory()
        ),
        "gui": ObservationGroupV2.valid(
            100,
            "client_screen_handler",
            GuiStateV2.closed(),
        ),
        "perception": ObservationGroupV2.valid(
            100,
            "client_perception_filtered",
            _perception(),
        ),
        "source_backend": "craftground",
    }
    values.update(overrides)
    return ObservationSnapshotV2(**values)  # type: ignore[arg-type]


class ObservationV2ContractTests(unittest.TestCase):
    def test_visible_entity_hurt_animation_is_bounded(self) -> None:
        entity = VisibleEntityV2(
            "entity-1", "minecraft:zombie", None,
            Vec3V0(0, 0, 2), Vec3V0(0, 0, 0), 0.0, 0.0,
            Vec3V0(.6, 1.95, .6), "standing", True, (),
            hurt_animation_ticks=10,
        )
        self.assertEqual(entity.hurt_animation_ticks, 10)
        self.assertIsNone(replace(entity, hurt_animation_ticks=None).hurt_animation_ticks)
        for invalid in (-1, 21, True, 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ContractViolation):
                replace(entity, hurt_animation_ticks=invalid)

    def test_valid_snapshot_is_v2_and_has_no_image_field(self) -> None:
        snapshot = make_snapshot()

        self.assertEqual(snapshot.schema_version, "mc2p.observation.v2")
        self.assertEqual(len(snapshot.inventory.value.main), 36)
        self.assertEqual(len(snapshot.perception.value.block_rays), 135)
        names = {item.name.casefold() for item in fields(ObservationSnapshotV2)}
        self.assertTrue(names.isdisjoint({"pov", "rgb", "image", "pixels"}))
        projection = json.dumps(asdict(snapshot), sort_keys=True)
        self.assertNotIn('"pov"', projection.casefold())
        self.assertNotIn('"rgb"', projection.casefold())

    def test_inventory_requires_exactly_36_main_slots(self) -> None:
        valid = _inventory()

        with self.assertRaisesRegex(ContractViolation, "36"):
            replace(valid, main=valid.main[:-1])

    def test_group_tick_must_match_snapshot_world_tick(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "sample world tick"):
            make_snapshot(
                inventory=ObservationGroupV2.valid(
                    99,
                    "client_inventory",
                    _inventory(),
                )
            )

    def test_top_level_self_projection_cannot_disagree(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "position"):
            make_snapshot(position=FieldValueV0.valid(Vec3V0(9.0, 2.0, 3.0)))

        dead = replace(_self_state(), is_dead=True)
        with self.assertRaisesRegex(ContractViolation, "is_dead"):
            make_snapshot(
                self_state=ObservationGroupV2.valid(
                    100,
                    "client_player",
                    dead,
                )
            )

    def test_block_ray_grid_coordinates_and_angles_are_exact(self) -> None:
        rays = list(_rays())
        rays[1] = replace(rays[1], column=0)
        with self.assertRaisesRegex(ContractViolation, "grid"):
            replace(_perception(), block_rays=tuple(rays))

        rays = list(_rays())
        rays[1] = replace(rays[1], yaw_offset_degrees=0.0)
        with self.assertRaisesRegex(ContractViolation, "angle"):
            replace(_perception(), block_rays=tuple(rays))

    def test_non_valid_group_cannot_carry_data(self) -> None:
        with self.assertRaisesRegex(ContractViolation, "must not carry"):
            ObservationGroupV2(
                status=FieldStatusV0.MISSING,
                sample_world_tick=100,
                source_kind="client_inventory",
                reason_code="player_missing",
                value=_inventory(),
            )

    def test_closed_gui_cannot_retain_slots(self) -> None:
        closed = GuiStateV2.closed()

        with self.assertRaisesRegex(ContractViolation, "closed"):
            replace(closed, sync_id=1)

    def test_status_gui_contact_and_entity_values_reject_hidden_identifiers(self) -> None:
        effect = StatusEffectV2(
            effect_id="minecraft:speed",
            amplifier=1,
            duration_ticks=40,
            ambient=False,
            show_particles=True,
            show_icon=True,
        )
        slot = GuiSlotV2(
            slot_id=0,
            x=8,
            y=18,
            source_kind="container",
            source_index=0,
            item=_empty_item(),
            enabled=True,
            can_take=True,
        )
        contact = BodyContactV2(
            relative_block_position=Vec3V0(0.0, -1.0, 0.0),
            block_id="minecraft:stone",
            fluid_id=None,
            collision_shape="solid",
        )
        entity = VisibleEntityV2(
            track_id="track-1",
            entity_type="minecraft:zombie",
            display_name="Zombie",
            relative_position=Vec3V0(0.0, 0.0, 4.0),
            relative_velocity=Vec3V0(0.0, 0.0, 0.0),
            relative_yaw_degrees=0.0,
            pitch_degrees=0.0,
            bounding_box_size=Vec3V0(0.6, 1.95, 0.6),
            pose="standing",
            is_on_ground=True,
            equipment=(),
        )

        self.assertEqual(effect.effect_id, "minecraft:speed")
        self.assertEqual(slot.source_kind, "container")
        self.assertEqual(contact.block_id, "minecraft:stone")
        self.assertEqual(entity.track_id, "track-1")
        self.assertFalse(hasattr(entity, "uuid"))
        self.assertFalse(hasattr(entity, "health"))


if __name__ == "__main__":
    unittest.main()

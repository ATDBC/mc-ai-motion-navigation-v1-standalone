import math
import unittest
from dataclasses import replace

from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_types import (
    CalculationStatus, JAVA_1_21_RULESET, ResourceStatus, TickInput,
)
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockGeometry, WorldKnowledge
from tests.observation_v3_fixtures import valid_snapshot_v3


class B09RPhysicsStepTests(unittest.TestCase):
    def setUp(self):
        frame = NavigationObservationAdapter().ingest(valid_snapshot_v3(sequence=7))
        self.frame_stamp = frame.body.stamp
        assumptions = dict(
            jumping_cooldown_ticks=0, movement_speed_attribute=0.1,
            step_height_blocks=0.6, gravity_attribute=0.08,
            jump_strength_attribute=0.42,
        )
        self.state = build_physics_state(frame, JAVA_1_21_RULESET, assumptions).require_state()
        world = WorldKnowledge(frame.session)
        positions = tuple(
            (x, y, z) for x in range(-3, 4) for y in range(62, 68) for z in range(-3, 4)
        )
        world.confirm_air(frame.body.stamp, positions)
        world.observe_blocks(frame.body.stamp, {
            (x, 63, z): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-3, 4) for z in range(-3, 4)
        })
        self.world = PhysicsWorldView(world.view(), JAVA_1_21_RULESET)
        self.state = replace(
            self.state, position=(0.5, 64.0, 0.5),
            velocity_blocks_per_tick=(0.0, -0.0784000015258789, 0.0),
            on_ground=True, vertical_collision=True, game_mode="survival",
            flying=False, allow_flying=False,
        )

    def test_ground_forward_tick_uses_vanilla_input_scaling_friction_and_gravity_order(self):
        result = step(self.state, TickInput(1, 0, False, False, False, 0), self.world, JAVA_1_21_RULESET)
        self.assertIs(result.status, CalculationStatus.OK)
        after = result.next_state
        self.assertAlmostEqual(after.position[0], 0.5, places=8)
        self.assertAlmostEqual(after.position[1], 64.0, places=8)
        self.assertAlmostEqual(after.position[2], 0.598000009, places=7)
        self.assertAlmostEqual(after.velocity_blocks_per_tick[0], 0.0, places=8)
        self.assertAlmostEqual(after.velocity_blocks_per_tick[1], -0.0784000015, places=8)
        self.assertAlmostEqual(after.velocity_blocks_per_tick[2], 0.098000009 * 0.54600006, places=7)
        self.assertTrue(after.on_ground)
        self.assertIn("vertical_collision", result.events)

    def test_jump_sets_vertical_velocity_then_moves_and_applies_gravity(self):
        result = step(self.state, TickInput(1, 0, True, False, False, 0), self.world, JAVA_1_21_RULESET)
        self.assertIs(result.status, CalculationStatus.OK)
        after = result.next_state
        self.assertAlmostEqual(after.position[1], 64.42, places=8)
        self.assertAlmostEqual(after.velocity_blocks_per_tick[1], 0.3332, places=7)
        self.assertFalse(after.on_ground)
        self.assertEqual(after.jumping_cooldown_ticks, 10)
        self.assertIn("takeoff", result.events)

    def test_airborne_release_uses_air_drag_and_does_not_need_a_goal_or_action_name(self):
        initial = replace(
            self.state, position=(0.5, 65.0, 0.5),
            velocity_blocks_per_tick=(0.1, 0.2, -0.1),
            on_ground=False, vertical_collision=False,
        )
        result = step(initial, TickInput(0, 0, False, False, False, math.pi / 3), self.world, JAVA_1_21_RULESET)
        self.assertIs(result.status, CalculationStatus.OK)
        after = result.next_state
        self.assertEqual(after.position, (0.6, 65.2, 0.4))
        self.assertAlmostEqual(after.velocity_blocks_per_tick[0], 0.091, places=8)
        self.assertAlmostEqual(after.velocity_blocks_per_tick[1], 0.1176, places=8)
        self.assertAlmostEqual(after.velocity_blocks_per_tick[2], -0.091, places=8)

    def test_airborne_forward_uses_player_flying_speed_not_ground_attribute_fraction(self):
        initial = replace(
            self.state, position=(.5, 65., .5),
            velocity_blocks_per_tick=(0., .2, .1), on_ground=False,
            vertical_collision=False,
        )
        result = step(initial, TickInput(1, 0, False, False, False, 0),
                      self.world, JAVA_1_21_RULESET)
        self.assertIs(result.status, CalculationStatus.OK)
        self.assertAlmostEqual(result.next_state.position[2], .6196000018, places=7)

    def test_unknown_collision_owner_stops_without_a_partial_next_state(self):
        unknown = PhysicsWorldView(
            WorldKnowledge(self.state.session).view(), JAVA_1_21_RULESET)
        result = step(self.state, TickInput(1, 0, False, False, False, 0), unknown, JAVA_1_21_RULESET)
        self.assertIs(result.status, CalculationStatus.NEEDS_WORLD)
        self.assertIsNone(result.next_state)
        self.assertTrue(result.missing_cells)

    def test_side_collision_clips_only_the_blocked_axis(self):
        world = self._world_with_extra({
            (0, 64, 1): BlockGeometry.full_cube("minecraft:stone"),
        })
        moving = replace(self.state, velocity_blocks_per_tick=(0.05, -0.0784000015, 0.35))
        result = step(moving, TickInput(0, 0, False, False, False, 0), world, JAVA_1_21_RULESET)
        self.assertIs(result.status, CalculationStatus.OK)
        self.assertAlmostEqual(result.next_state.position[0], 0.55, places=8)
        self.assertAlmostEqual(result.next_state.position[2], 0.7, places=8)
        self.assertEqual(result.next_state.velocity_blocks_per_tick[2], 0.0)
        self.assertTrue(result.next_state.horizontal_collision)

    def test_ceiling_collision_and_negative_coordinate_landing_are_normal_results(self):
        ceiling = self._world_with_extra({
            (0, 66, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        result = step(self.state, TickInput(0, 0, True, False, False, 0), ceiling, JAVA_1_21_RULESET)
        self.assertIs(result.status, CalculationStatus.OK)
        self.assertAlmostEqual(result.next_state.position[1], 64.2, places=8)
        self.assertTrue(result.next_state.vertical_collision)
        self.assertFalse(result.next_state.on_ground)

        landing_world = self._world_at_origin(-2, 63, -2)
        falling = replace(
            self.state, position=(-1.5, 64.25, -1.5),
            velocity_blocks_per_tick=(0, -0.4, 0), on_ground=False,
            vertical_collision=False,
        )
        landed = step(falling, TickInput(0, 0, False, False, False, 0),
                      landing_world, JAVA_1_21_RULESET)
        self.assertIs(landed.status, CalculationStatus.OK)
        self.assertEqual(landed.next_state.position, (-1.5, 64.0, -1.5))
        self.assertTrue(landed.next_state.on_ground)
        self.assertIn("landed", landed.events)

    def test_half_block_step_uses_real_height_and_keeps_ground_contact(self):
        slab = BlockGeometry(
            "minecraft:smooth_stone_slab", "boxes",
            boxes=(self._local_box(0, 0, 0, 1, 0.5, 1),),
        )
        world = self._world_with_extra({(0, 64, 1): slab})
        moving = replace(self.state, velocity_blocks_per_tick=(0, -0.0784000015, 0.35))
        result = step(moving, TickInput(0, 0, False, False, False, 0), world, JAVA_1_21_RULESET)
        self.assertIs(result.status, CalculationStatus.OK)
        self.assertAlmostEqual(result.next_state.position[1], 64.5, places=8)
        self.assertGreater(result.next_state.position[2], 0.7)
        self.assertTrue(result.next_state.on_ground)
        self.assertIn("step_up", result.events)

    def test_supported_carpet_snow_and_stair_shapes_use_their_real_tread_height(self):
        shapes = (
            ("minecraft:white_carpet", (self._local_box(0, 0, 0, 1, 1 / 16, 1),), 1 / 16),
            ("minecraft:snow", (self._local_box(0, 0, 0, 1, .5, 1),), .5),
            ("minecraft:oak_stairs", (
                self._local_box(0, 0, 0, 1, .5, 1),
                self._local_box(0, .5, .5, 1, 1, 1),
            ), .5),
        )
        for material, boxes, expected_height in shapes:
            with self.subTest(material=material):
                shape = BlockGeometry(material, "boxes", boxes=boxes)
                world = self._world_with_extra({(0, 64, 1): shape})
                moving = replace(
                    self.state,
                    velocity_blocks_per_tick=(0, -0.0784000015, .35),
                )
                result = step(
                    moving, TickInput(0, 0, False, False, False, 0),
                    world, JAVA_1_21_RULESET,
                )
                self.assertIs(result.status, CalculationStatus.OK)
                self.assertAlmostEqual(
                    result.next_state.position[1], 64 + expected_height, places=8)
                self.assertIn("step_up", result.events)

    def test_sprint_intent_only_becomes_actual_sprint_when_eligible(self):
        hungry = replace(self.state, food_points=6)
        refused = step(hungry, TickInput(1, 0, False, False, True, 0),
                       self.world, JAVA_1_21_RULESET)
        self.assertIs(refused.status, CalculationStatus.OK)
        self.assertFalse(refused.next_state.sprinting)
        self.assertIn("sprint_intent_refused", refused.events)

        allowed = step(self.state, TickInput(1, 0, False, False, True, 0),
                       self.world, JAVA_1_21_RULESET)
        self.assertIs(allowed.status, CalculationStatus.OK)
        self.assertTrue(allowed.next_state.sprinting)
        self.assertGreater(allowed.next_state.position[2], refused.next_state.position[2])
        self.assertIn("sprint_started", allowed.events)

    def test_sprint_stops_when_forward_input_or_eligibility_is_lost(self):
        sprinting = replace(self.state, sprinting=True)
        released = step(sprinting, TickInput(0, 0, False, False, True, 0),
                        self.world, JAVA_1_21_RULESET)
        self.assertIs(released.status, CalculationStatus.OK)
        self.assertFalse(released.next_state.sprinting)
        self.assertIn("sprint_stopped", released.events)

        using_item = replace(sprinting, is_using_item=True)
        unsupported = step(using_item, TickInput(1, 0, False, False, True, 0),
                           self.world, JAVA_1_21_RULESET)
        self.assertIs(unsupported.status, CalculationStatus.UNSUPPORTED)
        self.assertIn("using_item_input_mutation_not_modeled", unsupported.unsupported_reasons)

    def test_sprint_stops_when_intent_is_released_while_forward_is_held(self):
        sprinting = replace(self.state, sprinting=True)

        released = step(
            sprinting, TickInput(1, 0, False, False, False, 0),
            self.world, JAVA_1_21_RULESET,
        )

        self.assertIs(released.status, CalculationStatus.OK)
        self.assertFalse(released.next_state.sprinting)
        self.assertIn("sprint_stopped", released.events)

    def test_crouch_clips_motion_before_the_body_leaves_ledge_support(self):
        world = WorldKnowledge(self.state.session)
        known = tuple(
            (x, y, z)
            for x in range(-2, 3)
            for y in range(62, 67)
            for z in range(-2, 4)
        )
        world.confirm_air(self.frame_stamp, known)
        world.observe_blocks(self.frame_stamp, {
            (0, 63, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        crouching = replace(
            self.state, position=(.5, 64., 1.29), pose="crouching",
            body_height=1.5, sneaking=True,
            velocity_blocks_per_tick=(0., 0., 0.),
        )

        result = step(
            crouching, TickInput(1, 0, False, True, False, 0),
            PhysicsWorldView(world.view(), JAVA_1_21_RULESET),
            JAVA_1_21_RULESET,
        )

        self.assertIs(result.status, CalculationStatus.OK)
        self.assertLessEqual(result.next_state.position[2], 1.300000001)
        self.assertIn("sneak_edge_clipped", result.events)

    def test_landing_tick_can_step_from_clipped_vertical_position(self):
        slab = BlockGeometry(
            "minecraft:smooth_stone_slab", "boxes",
            boxes=(self._local_box(0, 0, 0, 1, .5, 1),),
        )
        world = self._world_with_extra({(0, 64, 1): slab})
        falling = replace(
            self.state, position=(.5, 64.8, .5),
            velocity_blocks_per_tick=(0., -.9, .35),
            on_ground=False, vertical_collision=False,
        )

        result = step(
            falling, TickInput(0, 0, False, False, False, 0),
            world, JAVA_1_21_RULESET,
        )

        self.assertIs(result.status, CalculationStatus.OK)
        self.assertAlmostEqual(result.next_state.position[1], 64.5, places=8)
        self.assertAlmostEqual(result.next_state.position[2], .85, places=8)
        self.assertIn("step_up", result.events)

    def test_takeoff_into_full_block_does_not_reuse_jump_height_as_step_height(self):
        world = self._world_with_extra({
            (0, 64, 1): BlockGeometry.full_cube("minecraft:stone"),
        })
        moving = replace(
            self.state, velocity_blocks_per_tick=(0., 0., .35),
        )

        result = step(
            moving, TickInput(0, 0, True, False, False, 0),
            world, JAVA_1_21_RULESET,
        )

        self.assertIs(result.status, CalculationStatus.OK)
        self.assertAlmostEqual(result.next_state.position[1], 64.42, places=8)
        self.assertAlmostEqual(result.next_state.position[2], .7, places=8)
        self.assertNotIn("step_up", result.events)

    def test_sneaking_takeoff_is_not_ledge_clipped(self):
        world = self._single_edge_world()
        crouching = replace(
            self.state, position=(.5, 64., 1.29), pose="crouching",
            body_height=1.5, sneaking=True,
            velocity_blocks_per_tick=(0., 0., 0.),
        )

        result = step(
            crouching, TickInput(1, 0, True, True, False, 0),
            world, JAVA_1_21_RULESET,
        )

        self.assertIs(result.status, CalculationStatus.OK)
        self.assertGreater(result.next_state.position[2], 1.3)
        self.assertNotIn("sneak_edge_clipped", result.events)

    def test_near_ground_sneaking_fall_still_clips_at_the_ledge(self):
        world = self._single_edge_world()
        falling = replace(
            self.state, position=(.5, 64.1, 1.29), pose="crouching",
            body_height=1.5, sneaking=True, on_ground=False,
            vertical_collision=False, fall_distance_blocks=.1,
            velocity_blocks_per_tick=(0., -.1, .1),
        )

        result = step(
            falling, TickInput(0, 0, False, True, False, 0),
            world, JAVA_1_21_RULESET,
        )

        self.assertIs(result.status, CalculationStatus.OK)
        self.assertLessEqual(result.next_state.position[2], 1.300000001)
        self.assertIn("sneak_edge_clipped", result.events)

    def test_near_ground_entry_subtracts_accumulated_fall_distance(self):
        world = self._single_edge_world()
        falling = replace(
            self.state, position=(.5, 64.5, 1.29), pose="crouching",
            body_height=1.5, sneaking=True, on_ground=False,
            vertical_collision=False, fall_distance_blocks=.3,
            velocity_blocks_per_tick=(0., -.2, .06),
        )

        result = step(
            falling, TickInput(.3, 0, False, True, False, 0),
            world, JAVA_1_21_RULESET,
        )

        self.assertIs(result.status, CalculationStatus.OK)
        self.assertAlmostEqual(result.next_state.position[2], 1.35588, places=7)
        self.assertNotIn("sneak_edge_clipped", result.events)

    def test_floating_wall_is_not_mistaken_for_ledge_support(self):
        world = self._single_edge_world({
            (0, 64, 2): BlockGeometry.full_cube("minecraft:stone"),
        })
        crouching = replace(
            self.state, position=(.5, 64., 1.29), pose="crouching",
            body_height=1.5, sneaking=True,
            velocity_blocks_per_tick=(0., 0., .5),
        )

        result = step(
            crouching, TickInput(0, 0, False, True, False, 0),
            world, JAVA_1_21_RULESET,
        )

        self.assertIs(result.status, CalculationStatus.OK)
        self.assertLessEqual(result.next_state.position[2], 1.300000001)
        self.assertIn("sneak_edge_clipped", result.events)

    def test_first_contact_with_unmodeled_surface_material_is_unsupported(self):
        world = self._world_at_origin(0, 63, 0, {
            (0, 63, 0): BlockGeometry.full_cube("minecraft:slime_block"),
        })
        falling = replace(
            self.state, position=(.5, 64.25, .5),
            velocity_blocks_per_tick=(0., -.4, 0.),
            on_ground=False, vertical_collision=False,
        )

        result = step(
            falling, TickInput(0, 0, False, False, False, 0),
            world, JAVA_1_21_RULESET,
        )

        self.assertIs(result.status, CalculationStatus.UNSUPPORTED)
        self.assertIn("contact_material_motion_rule_not_supported",
                      result.unsupported_reasons)

    def test_plain_motion_does_not_require_unused_step_clearance(self):
        world = WorldKnowledge(self.state.session)
        required = tuple(
            (x, y, z)
            for x in range(-1, 2)
            for y in range(62, 66)
            for z in range(-1, 3)
        )
        world.confirm_air(self.frame_stamp, required)
        world.observe_blocks(self.frame_stamp, {
            (x, 63, z): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-1, 2) for z in range(-1, 3)
        })

        result = step(
            self.state, TickInput(1, 0, False, False, False, 0),
            PhysicsWorldView(world.view(), JAVA_1_21_RULESET),
            JAVA_1_21_RULESET,
        )

        self.assertIs(result.status, CalculationStatus.OK)

    def test_crouch_and_legal_crawl_keep_the_sampled_input_without_double_slowing(self):
        crouching = replace(
            self.state, pose="crouching", body_height=1.5, sneaking=True,
        )
        crouch = step(crouching, TickInput(.3, 0, False, True, False, 0),
                      self.world, JAVA_1_21_RULESET)
        self.assertIs(crouch.status, CalculationStatus.OK)
        self.assertAlmostEqual(crouch.next_state.position[2], .5294000027, places=7)
        self.assertEqual(crouch.next_state.pose, "crouching")

        crawling = replace(
            self.state, pose="swimming", body_height=.6, sneaking=False,
            swimming=False, submerged_in_water=False,
        )
        low_ceiling = self._world_with_extra({
            (0, 65, 0): BlockGeometry.full_cube("minecraft:stone"),
        })
        crawl = step(crawling, TickInput(.3, 0, False, False, False, 0),
                     low_ceiling, JAVA_1_21_RULESET)
        self.assertIs(crawl.status, CalculationStatus.OK)
        self.assertEqual(crawl.next_state.pose, "swimming")
        self.assertEqual(crawl.next_state.body_height, .6)

    def test_crawl_exits_only_when_standing_body_is_clear(self):
        crawling = replace(
            self.state, pose="swimming", body_height=.6, sneaking=False,
            swimming=False, submerged_in_water=False,
        )
        result = step(crawling, TickInput(0, 0, False, False, False, 0),
                      self.world, JAVA_1_21_RULESET)
        self.assertIs(result.status, CalculationStatus.OK)
        self.assertEqual(result.next_state.pose, "standing")
        self.assertEqual(result.next_state.body_height, 1.8)
        self.assertIn("crawl_exited", result.events)

    def test_crouch_pose_follows_the_previous_sampled_sneak_state(self):
        pressed = step(
            self.state, TickInput(.3, 0, False, True, False, 0),
            self.world, JAVA_1_21_RULESET,
        )
        self.assertEqual(pressed.next_state.pose, "standing")
        self.assertTrue(pressed.next_state.sneaking)
        entered = step(
            pressed.next_state, TickInput(.3, 0, False, True, False, 0),
            self.world, JAVA_1_21_RULESET,
        )
        self.assertEqual(entered.next_state.pose, "crouching")
        self.assertIn("crouch_entered", entered.events)

        released = step(
            entered.next_state, TickInput(.3, 0, False, False, False, 0),
            self.world, JAVA_1_21_RULESET,
        )
        self.assertEqual(released.next_state.pose, "crouching")
        exited = step(
            released.next_state, TickInput(.3, 0, False, False, False, 0),
            self.world, JAVA_1_21_RULESET,
        )
        self.assertEqual(exited.next_state.pose, "standing")
        self.assertIn("crouch_exited", exited.events)

    def test_resource_update_reports_exact_jump_cost_and_incomplete_server_state(self):
        jumped = step(self.state, TickInput(0, 0, True, False, False, 0),
                      self.world, JAVA_1_21_RULESET)
        self.assertIs(jumped.resource_update.status, ResourceStatus.CONDITIONAL)
        self.assertAlmostEqual(jumped.resource_update.exhaustion_delta, .05)
        self.assertIn("server_hunger_clock_not_in_physics_state",
                      jumped.resource_update.incomplete_reasons)

    @staticmethod
    def _local_box(*values):
        from mc2p.motion_nav.world_model import Aabb
        return Aabb(*values)

    def _world_with_extra(self, extra):
        return self._world_at_origin(0, 63, 0, extra)

    def _single_edge_world(self, extra=None):
        world = WorldKnowledge(self.state.session)
        positions = tuple(
            (x, y, z)
            for x in range(-2, 3)
            for y in range(61, 68)
            for z in range(-2, 5)
        )
        world.confirm_air(self.frame_stamp, positions)
        blocks = {(0, 63, 0): BlockGeometry.full_cube("minecraft:stone")}
        blocks.update(extra or {})
        world.observe_blocks(self.frame_stamp, blocks)
        return PhysicsWorldView(world.view(), JAVA_1_21_RULESET)

    def _world_at_origin(self, center_x, floor_y, center_z, extra=None):
        world = WorldKnowledge(self.state.session)
        positions = tuple(
            (x, y, z)
            for x in range(center_x - 3, center_x + 4)
            for y in range(floor_y - 2, floor_y + 6)
            for z in range(center_z - 3, center_z + 4)
        )
        world.confirm_air(self.frame_stamp, positions)
        blocks = {
            (x, floor_y, z): BlockGeometry.full_cube("minecraft:stone")
            for x in range(center_x - 3, center_x + 4)
            for z in range(center_z - 3, center_z + 4)
        }
        blocks.update(extra or {})
        world.observe_blocks(self.frame_stamp, blocks)
        return PhysicsWorldView(world.view(), JAVA_1_21_RULESET)


if __name__ == "__main__":
    unittest.main()

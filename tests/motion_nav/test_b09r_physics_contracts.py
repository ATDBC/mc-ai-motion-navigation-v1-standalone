import unittest
from dataclasses import replace

from mc2p.motion_nav.physics_adapter import (
    PhysicsWorldView, build_physics_state,
)
from mc2p.motion_nav.physics_types import (
    JAVA_1_21_RULESET, PhysicsState, StateBuildStatus, TickInput,
)
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import (
    ObservationStamp, WorldKnowledge, WorldSessionId, WorldView,
)
from tests.observation_v3_fixtures import valid_snapshot_v3


class B09RPhysicsContractsTests(unittest.TestCase):
    def test_public_motion_navigation_entry_exports_b09r_contracts(self):
        import mc2p.motion_nav as public

        for name in (
            "JAVA_1_21_RULESET", "PhysicsState", "TickInput", "StepResult",
            "PhysicsWorldView", "build_physics_state", "physics_step", "physics_rollout",
        ):
            self.assertTrue(hasattr(public, name), name)

    def setUp(self):
        self.frame = NavigationObservationAdapter().ingest(valid_snapshot_v3(sequence=4))
        self.assumptions = {
            "jumping_cooldown_ticks": 0,
            "movement_speed_attribute": 0.1,
            "step_height_blocks": 0.6,
            "gravity_attribute": 0.08,
            "jump_strength_attribute": 0.42,
        }

    def test_missing_internal_state_never_becomes_a_complete_physics_state(self):
        result = build_physics_state(self.frame, JAVA_1_21_RULESET, {})
        self.assertIs(result.status, StateBuildStatus.NEEDS_STATE)
        self.assertEqual(result.missing_fields, tuple(sorted(self.assumptions)))
        self.assertIsNone(result.state)

    def test_observed_and_explicit_state_builds_and_round_trips_without_defaults(self):
        result = build_physics_state(
            self.frame, JAVA_1_21_RULESET, self.assumptions)
        self.assertIs(result.status, StateBuildStatus.READY)
        state = result.require_state()
        self.assertEqual(state.velocity_blocks_per_tick, (0.0, 0.0, 0.0))
        self.assertEqual(state.jumping_cooldown_ticks, 0)
        self.assertEqual(state.movement_speed_attribute, 0.1)
        self.assertEqual(state.step_height_blocks, 0.6)
        self.assertEqual(PhysicsState.from_mapping(state.to_mapping()), state)
        self.assertNotEqual(replace(state, jumping_cooldown_ticks=1), state)
        self.assertEqual(
            dict(result.provenance)["jumping_cooldown_ticks"], "explicit_assumption")

    def test_assumption_cannot_override_an_observed_fact(self):
        result = build_physics_state(
            self.frame, JAVA_1_21_RULESET,
            {**self.assumptions, "game_mode": "survival"},
        )
        self.assertIs(result.status, StateBuildStatus.INVALID_INPUT)
        self.assertIn("game_mode", result.conflicts)

    def test_state_build_rejects_mixed_world_sessions(self):
        other = WorldSessionId("other-session")
        mixed_body = replace(self.frame.body, session=other)
        mixed_frame = replace(self.frame, body=mixed_body)

        result = build_physics_state(
            mixed_frame, JAVA_1_21_RULESET, self.assumptions)

        self.assertIs(result.status, StateBuildStatus.INVALID_INPUT)
        self.assertIn("body_session_mismatch", result.conflicts)

        mixed_stamp = replace(self.frame.body.stamp, session=other)
        mixed_body = replace(self.frame.body, stamp=mixed_stamp)
        mixed_frame = replace(self.frame, body=mixed_body)
        result = build_physics_state(
            mixed_frame, JAVA_1_21_RULESET, self.assumptions)
        self.assertIs(result.status, StateBuildStatus.INVALID_INPUT)
        self.assertIn("body_stamp_session_mismatch", result.conflicts)

    def test_tick_input_is_the_complete_post_sample_input_and_round_trips(self):
        value = TickInput(
            forward=0.3, strafe=-0.3, jump=True, sneak=True, sprint=False,
            movement_yaw_radians=1.25,
        )
        self.assertEqual(TickInput.from_mapping(value.to_mapping()), value)
        with self.assertRaises(ValueError):
            TickInput(1.1, 0, False, False, False, 0)

    def test_physics_world_keeps_unknown_distinct_and_rejects_another_session(self):
        physics_world = PhysicsWorldView(self.frame.world, JAVA_1_21_RULESET)
        query = physics_world.shapes(((10, 64, 10),))
        self.assertEqual(query.missing_cells, ((10, 64, 10),))
        self.assertEqual(query.boxes, ())
        other = WorldView.detached(WorldSessionId("other"), 0, 0, {})
        with self.assertRaises(ValueError):
            PhysicsWorldView(other, JAVA_1_21_RULESET, expected_session=self.frame.session)

    def test_physics_world_reuses_an_identical_shape_query(self):
        known = WorldKnowledge(self.frame.session)
        known.confirm_air(self.frame.body.stamp, ((0, 64, 0), (1, 64, 0)))
        world = PhysicsWorldView(known.view(), JAVA_1_21_RULESET)

        first = world.shapes(((1, 64, 0), (0, 64, 0)))
        second = world.shapes(((0, 64, 0), (1, 64, 0), (0, 64, 0)))

        self.assertIs(second, first)

    def test_cached_shape_query_still_rejects_an_expired_live_view(self):
        known = WorldKnowledge(self.frame.session)
        position = (0, 64, 0)
        known.confirm_air(self.frame.body.stamp, (position,))
        world = PhysicsWorldView(known.view(), JAVA_1_21_RULESET)
        world.shapes((position,))
        newer = ObservationStamp(
            self.frame.session,
            self.frame.body.stamp.sequence_id + 1,
            self.frame.body.stamp.world_tick + 1,
            self.frame.body.stamp.controller_clock_id,
            self.frame.body.stamp.received_monotonic_ns + 50_000_000,
        )
        known.confirm_air(newer, (position,))

        with self.assertRaises(ValueError):
            world.shapes((position,))


if __name__ == "__main__":
    unittest.main()

import unittest

from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state
from mc2p.motion_nav.physics_rollout import (
    ExternalEvent, RolloutOptions, RolloutOutputMode, RolloutStopReason, rollout,
)
from mc2p.motion_nav.physics_types import (
    JAVA_1_21_RULESET, PhysicsState, ResourceStatus, TickInput,
)
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import BlockGeometry, WorldKnowledge
from tests.observation_v3_fixtures import valid_snapshot_v3


class B09RPhysicsRolloutTests(unittest.TestCase):
    def setUp(self):
        frame = NavigationObservationAdapter().ingest(valid_snapshot_v3(sequence=12))
        self.initial = build_physics_state(frame, JAVA_1_21_RULESET, {
            "jumping_cooldown_ticks": 0,
            "movement_speed_attribute": .1,
            "step_height_blocks": .6,
            "gravity_attribute": .08,
            "jump_strength_attribute": .42,
        }).require_state()
        from dataclasses import replace
        self.initial = replace(
            self.initial, position=(.5, 64., .5),
            velocity_blocks_per_tick=(0., -.0784000015258789, 0.),
            on_ground=True, vertical_collision=True,
        )
        known = WorldKnowledge(frame.session)
        positions = tuple(
            (x, y, z) for x in range(-8, 9) for y in range(61, 69)
            for z in range(-8, 9)
        )
        known.confirm_air(frame.body.stamp, positions)
        known.observe_blocks(frame.body.stamp, {
            (x, 63, z): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-8, 9) for z in range(-8, 9)
        })
        self.world = PhysicsWorldView(known.view(), JAVA_1_21_RULESET)
        self.inputs = tuple(
            TickInput(1, 0, False, False, False, 0) for _ in range(8)
        )

    def test_continuous_and_saved_restored_rollout_are_identical(self):
        continuous = rollout(self.initial, self.inputs, self.world,
                             JAVA_1_21_RULESET)
        first = rollout(self.initial, self.inputs[:3], self.world,
                        JAVA_1_21_RULESET)
        restored = PhysicsState.from_mapping(first.final_state.to_mapping())
        second = rollout(restored, self.inputs[3:], self.world,
                         JAVA_1_21_RULESET)

        self.assertEqual(continuous.stop_reason, RolloutStopReason.COMPLETE)
        self.assertEqual(second.stop_reason, RolloutStopReason.COMPLETE)
        self.assertEqual(continuous.final_state, second.final_state)

    def test_branching_does_not_mutate_the_saved_state_or_other_branch(self):
        saved = PhysicsState.from_mapping(self.initial.to_mapping())
        left = rollout(saved, (TickInput(0, 1, False, False, False, 0),) * 4,
                       self.world, JAVA_1_21_RULESET)
        right = rollout(saved, (TickInput(0, -1, False, False, False, 0),) * 4,
                        self.world, JAVA_1_21_RULESET)
        repeated_left = rollout(saved, (TickInput(0, 1, False, False, False, 0),) * 4,
                                self.world, JAVA_1_21_RULESET)

        self.assertEqual(saved, self.initial)
        self.assertEqual(left.final_state, repeated_left.final_state)
        self.assertNotEqual(left.final_state.position, right.final_state.position)

    def test_summary_and_full_outputs_have_the_same_result(self):
        full = rollout(self.initial, self.inputs, self.world, JAVA_1_21_RULESET,
                       RolloutOptions(output_mode=RolloutOutputMode.FULL))
        summary = rollout(self.initial, self.inputs, self.world, JAVA_1_21_RULESET,
                          RolloutOptions(output_mode=RolloutOutputMode.SUMMARY))
        self.assertEqual(full.final_state, summary.final_state)
        self.assertEqual(full.ticks_completed, summary.ticks_completed)
        self.assertEqual(len(full.states), len(self.inputs))
        self.assertEqual(summary.states, ())

    def test_rollout_preserves_events_steps_and_resource_completeness(self):
        inputs = (
            TickInput(1, 0, True, False, True, 0),
            TickInput(1, 0, False, False, True, 0),
        )
        full = rollout(
            self.initial, inputs, self.world, JAVA_1_21_RULESET,
            RolloutOptions(output_mode=RolloutOutputMode.FULL),
        )
        summary = rollout(
            self.initial, inputs, self.world, JAVA_1_21_RULESET,
            RolloutOptions(output_mode=RolloutOutputMode.SUMMARY),
        )

        self.assertEqual(len(full.steps), 2)
        self.assertEqual(summary.steps, ())
        self.assertIn((0, "takeoff"), full.events)
        self.assertEqual(summary.events, full.events)
        self.assertIs(full.resource_update.status, ResourceStatus.CONDITIONAL)
        self.assertEqual(summary.resource_update, full.resource_update)
        self.assertAlmostEqual(full.resource_update.exhaustion_delta, .2)
        self.assertIn("server_hunger_clock_not_in_physics_state",
                      full.resource_update.incomplete_reasons)

    def test_budget_cancel_and_external_event_stop_only_at_tick_boundaries(self):
        limited = rollout(
            self.initial, self.inputs, self.world, JAVA_1_21_RULESET,
            RolloutOptions(max_ticks=3),
        )
        self.assertEqual(limited.stop_reason, RolloutStopReason.MAX_TICKS)
        self.assertEqual(limited.ticks_completed, 3)
        self.assertEqual(limited.final_state.movement_tick_id,
                         self.initial.movement_tick_id + 3)

        external = rollout(
            self.initial, self.inputs, self.world, JAVA_1_21_RULESET,
            RolloutOptions(external_events=(ExternalEvent(2, "server_correction"),)),
        )
        self.assertEqual(external.stop_reason, RolloutStopReason.EXTERNAL_EVENT)
        self.assertEqual(external.ticks_completed, 2)
        self.assertEqual(external.external_event.kind, "server_correction")

        cancelled = rollout(
            self.initial, self.inputs, self.world, JAVA_1_21_RULESET,
            RolloutOptions(cancel_check=lambda completed: completed >= 1),
        )
        self.assertEqual(cancelled.stop_reason, RolloutStopReason.CANCELLED)
        self.assertEqual(cancelled.ticks_completed, 1)

    def test_missing_world_returns_only_the_complete_prefix(self):
        unknown = PhysicsWorldView(
            WorldKnowledge(self.initial.session).view(), JAVA_1_21_RULESET)
        result = rollout(self.initial, self.inputs, unknown, JAVA_1_21_RULESET)
        self.assertEqual(result.stop_reason, RolloutStopReason.STEP_INCOMPLETE)
        self.assertEqual(result.ticks_completed, 0)
        self.assertEqual(result.final_state, self.initial)
        self.assertIsNotNone(result.incomplete_step)


if __name__ == "__main__":
    unittest.main()

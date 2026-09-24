import math
import unittest
from dataclasses import replace
from unittest.mock import patch

from mc2p.contracts.action_receipt import ClientInputApplicationV1
from mc2p.contracts.action_v1 import ActionSnapshotV1, LookV1, MovementV1
from mc2p.contracts.common import FieldValueV0
from mc2p.contracts.observation import Vec3V0
from mc2p.motion_nav.motion_residual import (
    MotionResidualResult,
    MotionResidualStatus,
    MotionResidualTracker,
    calculate_motion_residual,
)
from mc2p.motion_nav.online_motion import (
    InputApplicationLedger,
    MotionTickPhase,
    StateAnchor,
)
from mc2p.motion_nav.physics_1_21 import step
from mc2p.motion_nav.physics_adapter import PhysicsWorldView
from mc2p.motion_nav.physics_types import (
    CalculationStatus,
    JAVA_1_21_RULESET,
    PhysicsState,
    StateBuildResult,
    StateBuildStatus,
    TickInput,
)
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.world_model import (
    BlockGeometry,
    ObservationStamp,
    WorldKnowledge,
    WorldSessionId,
)
from tests.observation_v3_fixtures import valid_snapshot_v3


SESSION = WorldSessionId("fabric:residual-test:clock")
STAMP = ObservationStamp(SESSION, 1, 1, "clock", 1)


def state(*, tick=20, yaw=0.0):
    return PhysicsState(
        JAVA_1_21_RULESET.ruleset_id, JAVA_1_21_RULESET.state_schema,
        SESSION, tick, (0.5, 64.0, 0.5), (0.0, -0.0784000015258789, 0.0),
        yaw, 0.0, "standing", 0.6, 1.8, True, False, True,
        False, False, 0, 0.0, 0.1, 0.6, 0.08, 0.42, 20, 5.0,
        "survival", (), False, False, False, False, False, False,
    )


def anchor(physics_state):
    return StateAnchor(
        SESSION, 1, physics_state.movement_tick_id,
        MotionTickPhase.AFTER_MOVEMENT, None, None,
        JAVA_1_21_RULESET.ruleset_id,
        JAVA_1_21_RULESET.state_schema,
        "mc2p.input-projection.v1", physics_state,
    )


def application(sequence, tick, movement):
    return ClientInputApplicationV1(
        "mc2p.input-application.v1", tick, "episode", sequence, 100 + tick,
        "leased", float(movement.forward), float(movement.strafe),
        movement.jump, movement.sneak, movement.sprint,
    )


def observation(*, seq, tick):
    base = valid_snapshot_v3(sequence=seq)
    own = replace(
        base.self_state.value,
        movement_tick_id=tick,
        hurt_animation_ticks=0,
        health_points=20.0,
        velocity=Vec3V0(0.0, 0.0, 0.0),
        is_on_ground=True,
    )
    return replace(
        base,
        episode_id="episode-a",
        self_state=replace(base.self_state, value=own),
        health_points=FieldValueV0.valid(20.0),
        is_on_ground=FieldValueV0.valid(True),
    )


class MotionResidualTests(unittest.TestCase):
    def setUp(self):
        knowledge = WorldKnowledge(SESSION)
        known = tuple(
            (x, y, z)
            for x in range(-2, 3)
            for y in range(62, 68)
            for z in range(-2, 4)
        )
        knowledge.confirm_air(STAMP, known)
        knowledge.observe_blocks(STAMP, {
            (x, 63, z): BlockGeometry.full_cube("minecraft:stone")
            for x in range(-2, 3) for z in range(-2, 4)
        })
        self.world = PhysicsWorldView(knowledge.view(), JAVA_1_21_RULESET)

    def _ledger(self, movement, *, look=LookV1(), tick=21):
        action = ActionSnapshotV1(
            "episode", 7, 1, 1_000_000,
            movement=movement, look=look,
        )
        ledger = InputApplicationLedger(max_records=4)
        ledger.submit(SESSION, action, requested_first_tick=tick)
        ledger.observe_sample(application(7, tick, movement))
        return ledger

    def test_exact_walk_prediction_has_no_external_motion_residual(self):
        initial = state()
        movement = MovementV1(forward=1)
        expected = step(
            initial, TickInput(1, 0, False, False, False, 0.0),
            self.world, JAVA_1_21_RULESET,
        )
        self.assertIs(expected.status, CalculationStatus.OK)

        result = calculate_motion_residual(
            anchor(initial), expected.next_state,
            self._ledger(movement), self.world,
        )

        self.assertIs(result.status, MotionResidualStatus.MATCHED)
        self.assertEqual(result.predicted_state, expected.next_state)
        self.assertAlmostEqual(result.position_error_blocks, 0.0)
        self.assertAlmostEqual(result.velocity_error_blocks_per_tick, 0.0)

    def test_unexplained_displacement_is_a_deviation(self):
        initial = state()
        movement = MovementV1(forward=1)
        expected = step(
            initial, TickInput(1, 0, False, False, False, 0.0),
            self.world, JAVA_1_21_RULESET,
        ).next_state
        observed = replace(
            expected,
            position=(expected.position[0] + 0.4, *expected.position[1:]),
            velocity_blocks_per_tick=(
                expected.velocity_blocks_per_tick[0] + 0.25,
                *expected.velocity_blocks_per_tick[1:],
            ),
        )

        result = calculate_motion_residual(
            anchor(initial), observed, self._ledger(movement), self.world,
        )

        self.assertIs(result.status, MotionResidualStatus.DEVIATION)
        self.assertGreater(result.position_error_blocks, 0.39)
        self.assertGreater(result.velocity_error_blocks_per_tick, 0.24)

    def test_missing_applied_tick_is_incomplete_evidence_not_neutral_input(self):
        result = calculate_motion_residual(
            anchor(state()), replace(state(), movement_tick_id=21),
            InputApplicationLedger(max_records=4), self.world,
        )

        self.assertIs(result.status, MotionResidualStatus.NEEDS_INPUT)
        self.assertEqual(result.missing_ticks, (21,))
        self.assertIsNone(result.predicted_state)

    def test_turn_and_jump_use_actual_one_shot_look_before_movement(self):
        initial = state(yaw=0.0)
        movement = MovementV1(forward=1, jump=True, sprint=True)
        yaw = math.pi / 2
        expected = step(
            initial, TickInput(1, 0, True, False, True, yaw),
            self.world, JAVA_1_21_RULESET,
        ).next_state

        result = calculate_motion_residual(
            anchor(initial), expected,
            self._ledger(movement, look=LookV1(yaw_delta_degrees=90)),
            self.world,
        )

        self.assertIs(result.status, MotionResidualStatus.MATCHED)
        self.assertAlmostEqual(result.predicted_state.yaw_radians, yaw)

    def test_tracker_keeps_original_anchor_until_missing_world_is_observed(self):
        adapter = NavigationObservationAdapter()
        snapshots = tuple(
            observation(seq=seq, tick=tick)
            for seq, tick in ((1, 20), (2, 21), (3, 22))
        )
        frames = tuple(adapter.ingest(item) for item in snapshots)
        states = tuple(
            replace(state(tick=tick), session=frame.session)
            for tick, frame in zip((20, 21, 22), frames)
        )
        missing = MotionResidualResult(
            MotionResidualStatus.NEEDS_WORLD, 20, 21,
            dependencies=((0, 62, 0),), missing_cells=((0, 62, 0),),
        )
        matched = MotionResidualResult(
            MotionResidualStatus.MATCHED, 20, 22, states[-1], 0.0, 0.0,
        )
        tracker = MotionResidualTracker()
        ledger = InputApplicationLedger(max_records=4)

        with patch(
            "mc2p.motion_nav.motion_residual.build_physics_state",
            side_effect=tuple(
                StateBuildResult(StateBuildStatus.READY, item) for item in states
            ),
        ), patch(
            "mc2p.motion_nav.motion_residual.calculate_motion_residual",
            side_effect=(missing, matched),
        ) as calculate:
            self.assertIsNone(tracker.observe(snapshots[0], frames[0], ledger))
            self.assertIs(
                tracker.observe(snapshots[1], frames[1], ledger).status,
                MotionResidualStatus.NEEDS_WORLD,
            )
            self.assertEqual(tracker.anchor.movement_tick_id, 20)
            self.assertIs(
                tracker.observe(snapshots[2], frames[2], ledger).status,
                MotionResidualStatus.MATCHED,
            )

        self.assertEqual(calculate.call_args_list[1].args[0].movement_tick_id, 20)
        self.assertEqual(tracker.anchor.movement_tick_id, 22)


if __name__ == "__main__":
    unittest.main()

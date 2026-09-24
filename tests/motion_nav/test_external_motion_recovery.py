from dataclasses import replace
import unittest

from mc2p.contracts.common import ContractViolation, FieldValueV0
from mc2p.contracts.observation import Vec3V0
from mc2p.motion_nav.external_motion import ExternalMotionEventV1, ExternalMotionSource
from mc2p.motion_nav.external_motion_recovery import (
    ExternalMotionRecoveryConfig,
    ExternalMotionRecoveryController,
    RecoveryDirective,
)
from tests.observation_v3_fixtures import valid_snapshot_v3


def event(*, generation=1, tick=30, episode="episode-a"):
    return ExternalMotionEventV1(
        event_id=f"damage/{episode}/{tick}/{generation}",
        generation=generation,
        episode_id=episode,
        observation_sequence_id=tick,
        movement_tick_id=tick,
        source=ExternalMotionSource.DAMAGE_KNOCKBACK,
        health_delta_points=-2.0,
        previous_position=Vec3V0(0.0, 64.0, 0.0),
        position=Vec3V0(0.1, 64.1, 0.0),
        previous_velocity=Vec3V0(0.0, 0.0, 0.0),
        velocity=Vec3V0(0.1, 0.2, 0.0),
        was_on_ground=True,
        is_on_ground=False,
    )


def snapshot(*, tick, ground, speed=0.0, seq=None, episode="episode-a"):
    sequence = tick if seq is None else seq
    base = valid_snapshot_v3(sequence=sequence)
    own = replace(
        base.self_state.value,
        movement_tick_id=tick,
        hurt_animation_ticks=0,
        is_on_ground=ground,
        velocity=Vec3V0(speed, 0.0, 0.0),
    )
    return replace(
        base,
        episode_id=episode,
        self_state=replace(base.self_state, value=own),
        is_on_ground=FieldValueV0.valid(ground),
    )


class ExternalMotionRecoveryControllerTests(unittest.TestCase):
    def test_airborne_ground_brake_and_two_stable_ticks(self):
        control = ExternalMotionRecoveryController()
        control.start(event(generation=1, tick=30))
        self.assertEqual(
            control.decide(snapshot(tick=31, ground=False)).directive,
            RecoveryDirective.NEUTRAL_AIR,
        )
        self.assertEqual(
            control.decide(snapshot(tick=32, ground=True, speed=.08)).directive,
            RecoveryDirective.START_GROUND_HOLD,
        )
        self.assertFalse(
            control.decide(snapshot(tick=33, ground=True, speed=.02)).complete
        )
        finished = control.decide(snapshot(tick=34, ground=True, speed=.02))
        self.assertTrue(finished.complete)
        self.assertEqual(finished.directive, RecoveryDirective.COMPLETE)

    def test_duplicate_tick_does_not_count_as_second_stable_tick(self):
        control = ExternalMotionRecoveryController()
        control.start(event())
        control.decide(snapshot(tick=31, ground=True, speed=.01, seq=31))
        repeated = control.decide(snapshot(tick=31, ground=True, speed=.01, seq=32))
        self.assertFalse(repeated.complete)
        self.assertEqual(repeated.stable_ticks, 1)

    def test_unstable_or_airborne_tick_resets_stability(self):
        control = ExternalMotionRecoveryController()
        control.start(event())
        control.decide(snapshot(tick=31, ground=True, speed=.01))
        self.assertEqual(
            control.decide(snapshot(tick=32, ground=True, speed=.08)).stable_ticks, 0
        )
        control.decide(snapshot(tick=33, ground=True, speed=.01))
        airborne = control.decide(snapshot(tick=34, ground=False, speed=.01))
        self.assertEqual(airborne.stable_ticks, 0)
        self.assertEqual(airborne.directive, RecoveryDirective.NEUTRAL_AIR)

    def test_repeat_event_updates_one_recovery_without_renewing_deadline(self):
        control = ExternalMotionRecoveryController()
        control.start(event(generation=1, tick=30))
        self.assertFalse(control.observe_event(event(generation=1, tick=30)))
        self.assertTrue(control.observe_event(event(generation=2, tick=35)))
        decision = control.decide(snapshot(tick=71, ground=False))
        self.assertEqual(decision.directive, RecoveryDirective.EXHAUSTED)
        self.assertEqual(decision.elapsed_ticks, 41)

    def test_fifth_event_exhausts_the_task_budget(self):
        control = ExternalMotionRecoveryController()
        control.start(event(generation=1, tick=30))
        for generation in range(2, 6):
            control.observe_event(event(generation=generation, tick=29 + generation))
        decision = control.decide(snapshot(tick=35, ground=False))
        self.assertEqual(decision.directive, RecoveryDirective.EXHAUSTED)
        self.assertEqual(decision.reason, "event_budget_exhausted")

    def test_session_and_order_must_remain_consistent(self):
        control = ExternalMotionRecoveryController()
        control.start(event())
        with self.assertRaisesRegex(ContractViolation, "session"):
            control.decide(snapshot(tick=31, ground=False, episode="episode-b"))

        control = ExternalMotionRecoveryController()
        control.start(event())
        control.decide(snapshot(tick=32, ground=False, seq=32))
        with self.assertRaisesRegex(ContractViolation, "sequence"):
            control.decide(snapshot(tick=33, ground=False, seq=31))

    def test_config_and_completed_controller_are_bounded(self):
        with self.assertRaises(ContractViolation):
            ExternalMotionRecoveryConfig(maximum_recovery_ticks=0)
        control = ExternalMotionRecoveryController()
        with self.assertRaisesRegex(ContractViolation, "active"):
            control.decide(snapshot(tick=31, ground=False))
        control.start(event())
        control.decide(snapshot(tick=31, ground=True, speed=.01))
        control.decide(snapshot(tick=32, ground=True, speed=.01))
        with self.assertRaisesRegex(ContractViolation, "complete"):
            control.decide(snapshot(tick=33, ground=True, speed=.01))


if __name__ == "__main__":
    unittest.main()

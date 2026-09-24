from dataclasses import replace
import unittest

from mc2p.contracts.common import ContractViolation, FieldValueV0
from mc2p.contracts.observation import Vec3V0
from mc2p.motion_nav.external_motion import (
    DamageKnockbackDetector,
    ExternalMotionSource,
)
from tests.observation_v3_fixtures import valid_snapshot_v3


def snapshot(*, seq, tick, hurt, health, episode="episode-a",
             velocity=(0.0, 0.0, 0.0), ground=True):
    base = valid_snapshot_v3(sequence=seq)
    own = replace(
        base.self_state.value,
        hurt_animation_ticks=hurt,
        movement_tick_id=tick,
        health_points=health,
        velocity=Vec3V0(*velocity),
        is_on_ground=ground,
    )
    return replace(
        base,
        episode_id=episode,
        self_state=replace(base.self_state, value=own),
        health_points=FieldValueV0.valid(health),
        is_on_ground=FieldValueV0.valid(ground),
    )


class DamageKnockbackDetectorTests(unittest.TestCase):
    def test_zero_to_positive_hurt_with_health_loss_creates_one_event(self):
        detector = DamageKnockbackDetector()
        self.assertIsNone(detector.observe(
            snapshot(seq=1, tick=20, hurt=0, health=20)
        ).event)

        found = detector.observe(snapshot(seq=2, tick=21, hurt=10, health=18))

        self.assertEqual(found.event.source, ExternalMotionSource.DAMAGE_KNOCKBACK)
        self.assertEqual(found.event.health_delta_points, -2)
        self.assertEqual(found.event.generation, 1)
        duplicate = detector.observe(snapshot(seq=2, tick=21, hurt=10, health=18))
        self.assertIsNone(duplicate.event)
        self.assertEqual(duplicate.reason, "duplicate_observation")

    def test_motion_deviation_without_hurt_is_not_knockback(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))

        result = detector.observe(snapshot(
            seq=2, tick=21, hurt=0, health=20, velocity=(1.0, 0.0, 0.0)
        ))

        self.assertIsNone(result.event)
        self.assertEqual(result.reason, "no_damage_transition")

    def test_damage_evidence_requires_both_transition_and_health_loss(self):
        for hurt, health, reason in (
            (10, 20, "no_health_loss"),
            (0, 18, "no_damage_transition"),
        ):
            with self.subTest(hurt=hurt, health=health):
                detector = DamageKnockbackDetector()
                detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
                result = detector.observe(snapshot(
                    seq=2, tick=21, hurt=hurt, health=health
                ))
                self.assertIsNone(result.event)
                self.assertEqual(result.reason, reason)

    def test_health_loss_one_tick_after_hurt_transition_confirms_same_damage(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))

        pending = detector.observe(snapshot(seq=2, tick=21, hurt=9, health=20))
        found = detector.observe(snapshot(
            seq=3, tick=22, hurt=8, health=17,
            velocity=(.1, .2, 0.0), ground=False,
        ))

        self.assertIsNone(pending.event)
        self.assertEqual(pending.reason, "no_health_loss")
        self.assertIsNotNone(found.event)
        self.assertEqual(found.event.health_delta_points, -3)
        self.assertEqual(found.event.observation_sequence_id, 3)
        self.assertEqual(found.event.movement_tick_id, 22)

    def test_missing_motion_evidence_clears_comparison_baseline(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        unavailable = detector.observe(snapshot(
            seq=2, tick=None, hurt=None, health=20
        ))
        self.assertEqual(unavailable.reason, "motion_evidence_unavailable")

        result = detector.observe(snapshot(seq=3, tick=22, hurt=10, health=18))

        self.assertIsNone(result.event)
        self.assertEqual(result.reason, "baseline_established")

    def test_new_session_reanchors_and_resets_generation(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        first = detector.observe(snapshot(seq=2, tick=21, hurt=10, health=18))
        self.assertEqual(first.event.generation, 1)

        changed = detector.observe(snapshot(
            seq=1, tick=1, hurt=0, health=20, episode="episode-b"
        ))

        self.assertIsNone(changed.event)
        self.assertEqual(changed.reason, "session_reanchored")
        second = detector.observe(snapshot(
            seq=2, tick=2, hurt=10, health=18, episode="episode-b"
        ))
        self.assertEqual(second.event.generation, 1)

    def test_sequence_or_movement_tick_cannot_move_backward(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=2, tick=20, hurt=0, health=20))
        with self.assertRaisesRegex(ContractViolation, "sequence"):
            detector.observe(snapshot(seq=1, tick=21, hurt=0, health=20))

        detector.reset()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        with self.assertRaisesRegex(ContractViolation, "movement tick"):
            detector.observe(snapshot(seq=2, tick=19, hurt=0, health=20))

    def test_separate_damage_transitions_increment_generation(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        first = detector.observe(snapshot(seq=2, tick=21, hurt=10, health=18))
        detector.observe(snapshot(seq=3, tick=31, hurt=0, health=18))
        second = detector.observe(snapshot(seq=4, tick=32, hurt=10, health=16))

        self.assertEqual((first.event.generation, second.event.generation), (1, 2))
        self.assertNotEqual(first.event.event_id, second.event.event_id)


if __name__ == "__main__":
    unittest.main()

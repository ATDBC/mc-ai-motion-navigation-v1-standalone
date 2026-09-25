from dataclasses import replace
import unittest

from mc2p.contracts.common import ContractViolation, FieldValueV0
from mc2p.contracts.observation import Vec3V0
from mc2p.motion_nav.external_motion import (
    DamageFactDetector,
    DamageKnockbackDetector,
    ExternalMotionSource,
)
from mc2p.motion_nav.motion_residual import (
    MotionResidualResult,
    MotionResidualStatus,
)
from tests.motion_nav.test_motion_residual import state as physics_state
from tests.observation_v3_fixtures import valid_snapshot_v3


def snapshot(*, seq, tick, hurt, health, absorption=0.0, episode="episode-a",
             velocity=(0.0, 0.0, 0.0), ground=True):
    base = valid_snapshot_v3(sequence=seq)
    own = replace(
        base.self_state.value,
        hurt_animation_ticks=hurt,
        movement_tick_id=tick,
        health_points=health,
        absorption_points=absorption,
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


def residual(*, deviation, first=20, last=21):
    predicted = physics_state(tick=last)
    return MotionResidualResult(
        MotionResidualStatus.DEVIATION if deviation else MotionResidualStatus.MATCHED,
        first, last, predicted,
        0.4 if deviation else 0.0,
        0.2 if deviation else 0.0,
    )


class DamageFactTests(unittest.TestCase):
    def test_health_damage_is_a_fact_even_without_motion_deviation(self):
        detector = DamageFactDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        found = detector.observe(snapshot(seq=2, tick=21, hurt=10, health=18))
        self.assertEqual(found.fact.health_delta_points, -2)
        self.assertEqual(found.fact.absorption_delta_points, 0)
        self.assertEqual(found.fact.total_damage_points, 2)

    def test_absorption_only_damage_is_not_lost(self):
        detector = DamageFactDetector()
        detector.observe(snapshot(
            seq=1, tick=20, hurt=0, health=20, absorption=4,
        ))
        found = detector.observe(snapshot(
            seq=2, tick=21, hurt=10, health=20, absorption=2,
        ))
        self.assertEqual(found.fact.health_delta_points, 0)
        self.assertEqual(found.fact.absorption_delta_points, -2)
        self.assertEqual(found.fact.total_damage_points, 2)

    def test_health_loss_one_tick_after_hurt_transition_confirms_same_damage(self):
        detector = DamageFactDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        pending = detector.observe(snapshot(seq=2, tick=21, hurt=9, health=20))
        found = detector.observe(snapshot(seq=3, tick=22, hurt=8, health=17))
        self.assertIsNone(pending.fact)
        self.assertEqual(pending.reason, "damage_amount_pending")
        self.assertEqual(found.fact.health_delta_points, -3)
        self.assertEqual(found.fact.observation_sequence_id, 3)


class ExternalMotionDetectorTests(unittest.TestCase):
    def test_damage_and_residual_together_create_damage_knockback(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        found = detector.observe(
            snapshot(seq=2, tick=21, hurt=10, health=18),
            residual(deviation=True),
        )
        self.assertEqual(found.event.source, ExternalMotionSource.DAMAGE_KNOCKBACK)
        self.assertEqual(found.event.health_delta_points, -2)
        self.assertEqual(found.event.generation, 1)
        self.assertIsNotNone(found.damage_fact)
        duplicate = detector.observe(
            snapshot(seq=2, tick=21, hurt=10, health=18),
            residual(deviation=True),
        )
        self.assertIsNone(duplicate.event)
        self.assertEqual(duplicate.reason, "duplicate_observation")

    def test_damage_without_residual_does_not_start_motion_recovery(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        pending = detector.observe(
            snapshot(seq=2, tick=21, hurt=10, health=18),
            residual(deviation=False),
        )
        found = detector.observe(
            snapshot(seq=3, tick=22, hurt=9, health=18),
            residual(deviation=False, first=21, last=22),
        )
        self.assertIsNone(pending.event)
        self.assertEqual(pending.reason, "damage_motion_grace")
        self.assertIsNone(found.event)
        self.assertIsNotNone(found.damage_fact)
        self.assertEqual(found.reason, "damage_without_motion_residual")

    def test_damage_and_knockback_one_tick_apart_form_one_damage_event(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=444, tick=447, hurt=0, health=20))
        damage = detector.observe(
            snapshot(seq=445, tick=448, hurt=10, health=17),
            residual(deviation=False, first=447, last=448),
        )
        found = detector.observe(
            snapshot(
                seq=446, tick=449, hurt=9, health=17,
                velocity=(0.35, 0.18, 0.0), ground=False,
            ),
            residual(deviation=True, first=448, last=449),
        )

        self.assertIsNone(damage.event)
        self.assertEqual(damage.reason, "damage_motion_grace")
        self.assertEqual(
            found.event.source, ExternalMotionSource.DAMAGE_KNOCKBACK,
        )
        self.assertEqual(found.reason, "damage_knockback_confirmed")
        self.assertEqual(found.event.observation_sequence_id, 445)
        self.assertEqual(found.event.movement_tick_id, 448)
        self.assertEqual(found.event.health_delta_points, -3)

    def test_motion_deviation_without_damage_is_unattributed_external_motion(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        result = detector.observe(
            snapshot(
                seq=2, tick=21, hurt=0, health=20,
                velocity=(1.0, 0.0, 0.0),
            ),
            residual(deviation=True),
        )
        self.assertEqual(
            result.event.source, ExternalMotionSource.UNATTRIBUTED_EXTERNAL_MOTION,
        )
        self.assertIsNone(result.damage_fact)

    def test_damage_without_motion_evidence_remains_a_fact_only(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        result = detector.observe(snapshot(seq=2, tick=21, hurt=10, health=18))
        self.assertIsNone(result.event)
        self.assertIsNotNone(result.damage_fact)
        self.assertEqual(result.reason, "motion_residual_unavailable")

    def test_damage_with_unsupported_motion_model_requests_conservative_recovery(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        unsupported = MotionResidualResult(
            MotionResidualStatus.UNSUPPORTED, 20, 21,
            reasons=("surface_motion_rule_not_supported",),
        )

        result = detector.observe(
            snapshot(seq=2, tick=21, hurt=10, health=18), unsupported,
        )

        self.assertEqual(
            result.event.source,
            ExternalMotionSource.DAMAGE_WITH_UNVERIFIED_MOTION,
        )
        self.assertEqual(result.reason, "damage_motion_unverified")
        self.assertEqual(result.event.health_delta_points, -2)
        self.assertIsNone(result.event.position_residual_blocks)

    def test_invalid_residual_with_damage_also_invalidates_the_old_motion_proof(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        invalid = MotionResidualResult(
            MotionResidualStatus.INVALID_INPUT, 20, 21,
            reasons=("anchor_or_identity_not_comparable",),
        )

        result = detector.observe(
            snapshot(seq=2, tick=21, hurt=10, health=18), invalid,
        )

        self.assertEqual(
            result.event.source,
            ExternalMotionSource.DAMAGE_WITH_UNVERIFIED_MOTION,
        )

    def test_two_tick_deviation_after_missing_world_is_unverified_motion(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        unavailable = MotionResidualResult(
            MotionResidualStatus.NEEDS_WORLD, 20, 21,
            missing_cells=((0, 62, 0),),
        )
        pending = detector.observe(
            snapshot(seq=2, tick=21, hurt=10, health=18), unavailable,
        )
        found = detector.observe(
            snapshot(seq=3, tick=22, hurt=9, health=18),
            residual(deviation=True, first=20, last=22),
        )

        self.assertIsNone(pending.event)
        self.assertEqual(pending.reason, "motion_residual_unavailable")
        self.assertEqual(
            found.event.source,
            ExternalMotionSource.DAMAGE_WITH_UNVERIFIED_MOTION,
        )
        self.assertEqual(found.reason, "damage_motion_unverified_span")
        self.assertEqual(found.event.observation_sequence_id, 2)

    def test_delayed_damage_fact_does_not_claim_long_residual_as_knockback(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        pending = detector.observe(
            snapshot(seq=2, tick=21, hurt=9, health=20),
            MotionResidualResult(
                MotionResidualStatus.NEEDS_WORLD, 20, 21,
                missing_cells=((0, 62, 0),),
            ),
        )
        found = detector.observe(
            snapshot(seq=3, tick=22, hurt=8, health=17),
            residual(deviation=True, first=20, last=22),
        )

        self.assertIsNone(pending.event)
        self.assertEqual(pending.reason, "damage_amount_pending")
        self.assertEqual(
            found.event.source,
            ExternalMotionSource.DAMAGE_WITH_UNVERIFIED_MOTION,
        )
        self.assertEqual(found.reason, "damage_motion_unverified_span")

    def test_long_deviation_after_missing_world_is_not_labeled_knockback(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=444, tick=447, hurt=0, health=20))
        detector.observe(
            snapshot(seq=445, tick=448, hurt=10, health=17),
            residual(deviation=False, first=447, last=448),
        )
        for index, tick in enumerate(range(449, 455)):
            detector.observe(
                snapshot(seq=446 + index, tick=tick, hurt=9, health=17),
                MotionResidualResult(
                    MotionResidualStatus.NEEDS_WORLD,
                    448,
                    tick,
                    missing_cells=((-1, 64, 0),),
                ),
            )

        found = detector.observe(
            snapshot(
                seq=452,
                tick=455,
                hurt=2,
                health=17,
                velocity=(.3, 0.0, 0.0),
            ),
            residual(deviation=True, first=448, last=455),
        )

        self.assertEqual(
            found.event.source,
            ExternalMotionSource.DAMAGE_WITH_UNVERIFIED_MOTION,
        )
        self.assertEqual(found.reason, "damage_motion_unverified_span")

    def test_damage_residual_timeout_requests_conservative_recovery(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        found = None
        for index in range(12):
            tick = 21 + index
            found = detector.observe(
                snapshot(
                    seq=2 + index, tick=tick,
                    hurt=max(0, 10 - index), health=17, ground=False,
                ),
                MotionResidualResult(
                    MotionResidualStatus.NEEDS_WORLD, 20, tick,
                    missing_cells=((0, 62, -2),),
                ),
            )
            if found.event is not None:
                break

        self.assertIsNotNone(found.event)
        self.assertEqual(
            found.event.source,
            ExternalMotionSource.DAMAGE_WITH_UNVERIFIED_MOTION,
        )
        self.assertEqual(found.reason, "damage_motion_unverified_timeout")
        self.assertEqual(found.event.movement_tick_id, 21)

    def test_later_residual_after_missing_world_keeps_damage_attribution(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        detector.observe(
            snapshot(seq=2, tick=21, hurt=10, health=18),
            MotionResidualResult(
                MotionResidualStatus.NEEDS_WORLD, 20, 21,
                missing_cells=((0, 62, 0),),
            ),
        )
        found = detector.observe(
            snapshot(seq=3, tick=23, hurt=8, health=18),
            residual(deviation=True, first=22, last=23),
        )

        self.assertEqual(
            found.event.source,
            ExternalMotionSource.DAMAGE_WITH_UNVERIFIED_MOTION,
        )
        self.assertEqual(found.reason, "damage_motion_unverified_gap")
        self.assertIsNotNone(found.damage_fact)
        self.assertEqual(found.damage_fact.movement_tick_id, 21)

    def test_two_tick_matched_residual_after_grace_proves_no_knockback(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=444, tick=447, hurt=0, health=20))
        grace = detector.observe(
            snapshot(seq=445, tick=448, hurt=10, health=17),
            residual(deviation=False, first=447, last=448),
        )
        settled = detector.observe(
            snapshot(seq=446, tick=450, hurt=9, health=17),
            residual(deviation=False, first=448, last=450),
        )

        self.assertEqual(grace.reason, "damage_motion_grace")
        self.assertIsNone(settled.event)
        self.assertEqual(settled.reason, "damage_without_motion_residual")

    def test_unconfirmed_damage_residual_cannot_leak_into_later_damage(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        pending = detector.observe(
            snapshot(seq=2, tick=21, hurt=9, health=20),
            residual(deviation=True),
        )
        cleared = detector.observe(
            snapshot(seq=3, tick=22, hurt=0, health=20),
            residual(deviation=False, first=21, last=22),
        )
        found = detector.observe(
            snapshot(seq=4, tick=23, hurt=10, health=18),
            residual(deviation=False, first=22, last=23),
        )
        settled = detector.observe(
            snapshot(seq=5, tick=24, hurt=9, health=18),
            residual(deviation=False, first=23, last=24),
        )

        self.assertEqual(pending.reason, "damage_amount_pending")
        self.assertEqual(cleared.reason, "motion_matches_prediction")
        self.assertIsNone(found.event)
        self.assertEqual(found.reason, "damage_motion_grace")
        self.assertIsNone(settled.event)
        self.assertEqual(settled.reason, "damage_without_motion_residual")

    def test_missing_motion_evidence_clears_comparison_baseline(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        unavailable = detector.observe(snapshot(
            seq=2, tick=None, hurt=None, health=20,
        ))
        self.assertEqual(unavailable.reason, "motion_evidence_unavailable")
        result = detector.observe(
            snapshot(seq=3, tick=22, hurt=10, health=18),
            residual(deviation=True, first=21, last=22),
        )
        self.assertIsNone(result.event)
        self.assertEqual(result.reason, "baseline_established")

    def test_new_session_reanchors_and_resets_generation(self):
        detector = DamageKnockbackDetector()
        detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
        first = detector.observe(
            snapshot(seq=2, tick=21, hurt=10, health=18),
            residual(deviation=True),
        )
        self.assertEqual(first.event.generation, 1)
        changed = detector.observe(snapshot(
            seq=1, tick=1, hurt=0, health=20, episode="episode-b",
        ))
        self.assertIsNone(changed.event)
        self.assertEqual(changed.reason, "session_reanchored")
        second = detector.observe(
            snapshot(seq=2, tick=2, hurt=10, health=18, episode="episode-b"),
            residual(deviation=True, first=1, last=2),
        )
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


if __name__ == "__main__":
    unittest.main()

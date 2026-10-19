"""Pure C1-B phase selection keeps death separate from damage."""
import unittest

from mc2p.skills.engagement_memory import TargetPositionSource
from mc2p.skills.moving_melee import (
    MovingMeleePhase, decide_moving_melee,
)
from mc2p.skills.melee_strike_driver import MeleeStrikeOutcome


class MovingMeleePolicyTests(unittest.TestCase):
    def test_external_motion_recovery_is_a_distinct_nonterminal_phase(self):
        decision = decide_moving_melee(
            MovingMeleePhase.RECOVERING_EXTERNAL_MOTION,
            position_source=TargetPositionSource.VISION,
            within_attack_distance=False,
            target_dead=False,
        )
        self.assertEqual(decision.phase, MovingMeleePhase.PURSUING)
        self.assertFalse(decision.terminal)

    def test_unavailable_target_fails(self):
        decision = decide_moving_melee(
            MovingMeleePhase.ACQUIRE_VISIBLE_TARGET,
            position_source=None, within_attack_distance=False,
            target_dead=False,
        )
        self.assertEqual((decision.phase, decision.reason),
                         (MovingMeleePhase.FAILED, "target_unavailable"))

    def test_visible_close_target_is_strike_ready(self):
        decision = decide_moving_melee(
            MovingMeleePhase.PURSUING,
            position_source=TargetPositionSource.VISION,
            within_attack_distance=True, target_dead=False,
        )
        self.assertEqual(decision.phase, MovingMeleePhase.STRIKE_READY)

    def test_engagement_position_can_guide_pursuit_but_not_attack(self):
        decision = decide_moving_melee(
            MovingMeleePhase.RECOVERING_CADENCE,
            position_source=TargetPositionSource.ENGAGEMENT,
            within_attack_distance=True, target_dead=False,
        )
        self.assertEqual((decision.phase, decision.reason),
                         (MovingMeleePhase.PURSUING, "direct_vision_required"))

    def test_confirmed_hit_does_not_mean_target_dead(self):
        decision = decide_moving_melee(
            MovingMeleePhase.STRIKING,
            position_source=TargetPositionSource.VISION,
            within_attack_distance=True, target_dead=False,
            strike_outcome=MeleeStrikeOutcome.HIT_CONFIRMED,
        )
        self.assertEqual(decision.phase, MovingMeleePhase.RECOVERING_CADENCE)

    def test_explicit_death_is_terminal_and_idempotent(self):
        first = decide_moving_melee(
            MovingMeleePhase.CONFIRMING_DEATH,
            position_source=None, within_attack_distance=False,
            target_dead=True,
        )
        repeated = decide_moving_melee(
            first.phase, position_source=None, within_attack_distance=False,
            target_dead=True,
        )
        self.assertEqual(first.phase, MovingMeleePhase.COMPLETE)
        self.assertEqual(repeated, first)


if __name__ == "__main__":
    unittest.main()

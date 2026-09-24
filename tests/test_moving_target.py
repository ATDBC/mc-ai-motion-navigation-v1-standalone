"""Moving combat stand-off goals change only for explicit invalidation facts."""
from dataclasses import replace
import unittest

from mc2p.contracts.observation import Vec3V0
from mc2p.skills.engagement_memory import TargetPositionFactV1, TargetPositionSource
from mc2p.skills.moving_target import (
    COMBAT_GOAL_RADIUS_BLOCKS,
    decide_moving_goal,
)


def fact(x, z, sequence=1):
    return TargetPositionFactV1(
        "entity-world-7", TargetPositionSource.VISION,
        Vec3V0(x, 0, z), Vec3V0(0, 0, 0), sequence, 100_000_000,
        20, 20, False,
    )


SELF = Vec3V0(.1, 64, .5)


class MovingTargetTests(unittest.TestCase):
    def initial(self):
        return decide_moving_goal(
            fact(0, 4), SELF, "scope", 10_000_000_000,
        )

    def test_subthreshold_motion_keeps_the_same_goal_and_revision(self):
        first = self.initial()
        current = first
        for offset in (.2, .4, .6, .74):
            current = decide_moving_goal(
                fact(offset, 4, current.revision + 1), SELF,
                "scope", 10_000_000_000, previous=current,
            )
            self.assertFalse(current.changed)
            self.assertEqual(current.revision, first.revision)
            self.assertIs(current.goal, first.goal)
            self.assertEqual(current.reason, "within_reuse_bounds")

    def test_combat_goal_region_stays_inside_attack_distance(self):
        decision = self.initial()

        self.assertEqual(decision.goal.radius, COMBAT_GOAL_RADIUS_BLOCKS)
        self.assertLess(2.5 + decision.goal.radius, 3.0)

    def test_subthreshold_motion_replaces_goal_when_old_region_left_attack_range(self):
        first = self.initial()

        moved = decide_moving_goal(
            fact(0, 4.3, 2), SELF, "scope", 10_000_000_000,
            previous=first,
        )

        self.assertTrue(moved.changed)
        self.assertEqual(moved.reason, "old_endpoint_invalid")
        self.assertEqual(moved.revision, first.revision + 1)

    def test_four_explicit_conditions_replace_the_goal_once(self):
        first = self.initial()
        cases = (
            (fact(.76, 4), {}, "target_moved"),
            (fact(1.0, 4), {"movement_threshold_blocks": 10.0}, "support_region_changed"),
            (fact(.1, 4), {"old_endpoint_attack_valid": False}, "old_endpoint_invalid"),
            (fact(.1, 4), {"route_connectable": False}, "route_unconnectable"),
        )
        for update, flags, reason in cases:
            with self.subTest(reason=reason):
                changed = decide_moving_goal(
                    update, SELF, "scope", 10_000_000_000,
                    previous=first, **flags,
                )
                self.assertTrue(changed.changed)
                self.assertEqual(changed.revision, first.revision + 1)
                self.assertEqual(changed.reason, reason)

    def test_no_reason_means_twenty_updates_cannot_replace_the_goal(self):
        decision = self.initial()
        original = decision.goal
        for sequence in range(2, 22):
            decision = decide_moving_goal(
                fact(.1, 4, sequence), SELF,
                "scope", 10_000_000_000, previous=decision,
            )
            self.assertIs(decision.goal, original)
            self.assertFalse(decision.changed)


if __name__ == "__main__":
    unittest.main()

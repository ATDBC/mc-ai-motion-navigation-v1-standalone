import math
import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.motion_risk import (
    TaskDamageBudget,
    conservative_plain_fall_damage_points,
)


class TaskDamageBudgetTests(unittest.TestCase):
    def test_default_budget_allows_only_zero_expected_damage(self):
        budget = TaskDamageBudget()

        self.assertTrue(budget.allows(
            0.0, health_points=None, absorption_points=None,
        ))
        self.assertFalse(budget.allows(
            1.0, health_points=20.0, absorption_points=0.0,
        ))

    def test_positive_budget_needs_vitality_and_cannot_be_lethal(self):
        budget = TaskDamageBudget(
            risk_policy_id="allow-two-points",
            maximum_expected_damage_points=2.0,
        )

        self.assertFalse(budget.allows(
            1.0, health_points=None, absorption_points=None,
        ))
        self.assertTrue(budget.allows(
            2.0, health_points=3.0, absorption_points=0.0,
        ))
        self.assertFalse(budget.allows(
            2.0, health_points=2.0, absorption_points=0.0,
        ))
        self.assertTrue(budget.allows(
            2.0, health_points=1.0, absorption_points=2.0,
        ))

    def test_invalid_budget_and_prediction_are_rejected(self):
        invalid_budgets = (
            {"risk_policy_id": ""},
            {"maximum_expected_damage_points": -1.0},
            {"maximum_expected_damage_points": math.inf},
        )
        for values in invalid_budgets:
            with self.subTest(values=values), self.assertRaises(ContractViolation):
                TaskDamageBudget(**values)

        budget = TaskDamageBudget()
        for damage in (-1.0, math.inf, math.nan):
            with self.subTest(damage=damage), self.assertRaises(ContractViolation):
                budget.allows(
                    damage, health_points=20.0, absorption_points=0.0,
                )


class PlainFallDamageTests(unittest.TestCase):
    def test_uses_conservative_full_block_landing_bound(self):
        cases = (
            (0.0, 0.0),
            (3.0, 0.0),
            (3.01, 1.0),
            (4.0, 1.0),
            (6.0, 3.0),
        )
        for distance, expected in cases:
            with self.subTest(distance=distance):
                self.assertEqual(
                    conservative_plain_fall_damage_points(distance), expected,
                )

        for distance in (-1.0, math.inf, math.nan):
            with self.subTest(distance=distance), self.assertRaises(ContractViolation):
                conservative_plain_fall_damage_points(distance)


if __name__ == "__main__":
    unittest.main()

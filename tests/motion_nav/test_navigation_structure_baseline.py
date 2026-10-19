"""S0 freezes one deterministic, versioned structure-cleanup ruler."""
import math
import unittest

from scripts.navigation_structure_baseline import (
    NORMALIZER_VERSION,
    compare_indexes,
    normalize_structure_value,
    product_behavior_payload,
    structure_signature,
)


class NavigationStructureBaselineTests(unittest.TestCase):
    def test_normalizer_rounds_only_finite_floats_and_preserves_types(self):
        source = {
            "float": 1.1234567896,
            "integer": 1,
            "boolean": True,
            "none": None,
            "list": [2.0000000004, "x"],
            "tuple": (3.0000000006,),
            "infinite": math.inf,
        }

        normalized = normalize_structure_value(source)

        self.assertEqual(NORMALIZER_VERSION, "r28-structure-trajectory-v1")
        self.assertEqual(normalized["float"], 1.12345679)
        self.assertIs(type(normalized["integer"]), int)
        self.assertIs(type(normalized["boolean"]), bool)
        self.assertIs(type(normalized["list"]), list)
        self.assertIs(type(normalized["tuple"]), tuple)
        self.assertEqual(normalized["tuple"], (3.000000001,))
        self.assertEqual(normalized["infinite"], math.inf)

    def test_signature_ignores_sub_ninth_decimal_float_noise(self):
        first = {"trace": [{"position": [1.0000000001, 2.0]}]}
        second = {"trace": [{"position": [1.0000000002, 2.0]}]}

        self.assertEqual(structure_signature(first), structure_signature(second))

    def test_comparison_rejects_changed_or_incomplete_denominator(self):
        baseline = {
            "normalizer_version": NORMALIZER_VERSION,
            "kind": "product",
            "cases": [{"id": "a", "signature": "one"},
                      {"id": "b", "signature": "two"}],
        }
        changed = {
            **baseline,
            "cases": [{"id": "a", "signature": "one"},
                      {"id": "b", "signature": "changed"}],
        }

        self.assertEqual(compare_indexes(baseline, changed), {
            "cases": 2, "differences": ["b"], "equivalent": False,
        })
        with self.assertRaisesRegex(ValueError, "denominator"):
            compare_indexes(baseline, {**baseline, "cases": baseline["cases"][:1]})

    def test_product_payload_keeps_handoff_but_normalizes_random_identity(self):
        def sample(successor):
            trace = [{
                "movement_tick": 4,
                "body_handoff": {"owner": "route/random-owner", "successor": successor,
                                  "disposition": "transferable", "reason": "selected",
                                  "movement": {"forward": 1}},
                "async_coverage": {"begun": 1, "finished": 1, "applied": 1},
                "planning_submissions": ["random-request"],
            }]
            return product_behavior_payload(
                {"id": "case", "parameters": {}, "metrics": {"outcome": "success"},
                 "reason": "done", "motion_jobs": []},
                {"trace": trace, "strict_trace": [{"movement_tick": 4}]},
            )

        first = sample("random-successor-a")
        second = sample("random-successor-b")
        self.assertEqual(structure_signature(first), structure_signature(second))
        self.assertEqual(first["handoffs"][0]["handoff"]["successor"], 1)
        self.assertEqual(first["planning_work"], [{"movement_tick": 4, "submissions": 1}])


if __name__ == "__main__":
    unittest.main()

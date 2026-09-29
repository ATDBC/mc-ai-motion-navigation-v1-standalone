from __future__ import annotations

import json
from pathlib import Path
import unittest

from tests.sim.run_navigation_interrupt_matrix import expand_cases


MANIFEST = Path(
    "tests/sim/manifests/navigation-coordination-interrupt-late.json"
)


class NavigationInterruptMatrixTests(unittest.TestCase):
    def test_frozen_manifest_expands_to_review_twenty_family(self):
        cases = expand_cases(json.loads(MANIFEST.read_text(encoding="utf-8")))

        self.assertEqual(len(cases), 192)
        self.assertEqual(
            {case.scenario for case in cases},
            {"direct_drop_2", "direct_drop_5_budget_2"},
        )
        self.assertEqual({case.interruption for case in cases}, {"revise", "cancel"})
        self.assertEqual({case.interrupt_tick for case in cases}, set(range(30, 46)))
        self.assertEqual({case.late_offset for case in cases}, {None, 1, 2})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import unittest


class NavigationSeedScanTests(unittest.TestCase):
    def test_s5_manifest_freezes_one_hundred_seeds_per_family(self):
        path = Path("tests/sim/manifests/navigation-coordination-s5-seeds.json")
        document = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(document["seed_start"], 280001)
        self.assertEqual(document["seed_end"], 280100)
        self.assertGreaterEqual(len(document["families"]), 2)
        self.assertTrue(all(family["allowed_results"]
                            for family in document["families"]))


if __name__ == "__main__":
    unittest.main()

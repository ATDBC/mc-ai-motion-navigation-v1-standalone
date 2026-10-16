from __future__ import annotations

import unittest
from pathlib import Path

from mc2p.motion_nav.block_motion_traits import BlockMotionCatalog
from mc2p.motion_nav.world_model import (
    BlockGeometry, CellFact, CellKnowledge, ObservationStamp, WorldSessionId,
)
from scripts.b06_ordinary_material_runtime import ORDINARY_MATERIALS, cell_matches_material


ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config/motion-navigation"


class B06FabricMaterialProbeTests(unittest.TestCase):
    def test_probe_covers_every_declared_ordinary_material_once(self):
        catalog = BlockMotionCatalog.load(
            CONFIG / "block-motion-traits-v1.json",
            CONFIG / "vanilla-block-registry-1_21.json",
        )

        self.assertEqual(
            ORDINARY_MATERIALS,
            tuple(sorted(catalog.materials_for_ground_model("ordinary-ground-v1"))),
        )
        self.assertEqual(len(ORDINARY_MATERIALS), len(set(ORDINARY_MATERIALS)))

    def test_material_check_reads_the_world_fact_block_field(self):
        stamp = ObservationStamp(WorldSessionId("session"), 1, 1, "clock", 1)
        fact = CellFact(
            CellKnowledge.BLOCK,
            stamp,
            BlockGeometry.full_cube("minecraft:stone"),
        )

        self.assertTrue(cell_matches_material(fact, "minecraft:stone"))
        self.assertFalse(cell_matches_material(fact, "minecraft:dirt"))


if __name__ == "__main__":
    unittest.main()

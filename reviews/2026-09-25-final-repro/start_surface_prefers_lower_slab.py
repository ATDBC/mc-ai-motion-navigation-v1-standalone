"""The new start-surface lookup can pick a lower slab instead of the block underfoot.

Run from the repository root of a checkout of commit 1ea37d7:

    PYTHONPATH=. python reviews/2026-09-25-final-repro/start_surface_prefers_lower_slab.py

``NavigationSession._surface_for_body`` now collects every support surface
whose region overlaps the body box and returns the one nearest to the body
position in 3D.  The body stands on a full block (top y=64) with its centre
0.15-0.20 block past the edge, as in the B11 work position.  With air next to
the block the lookup correctly returns the block.  With a bottom slab next to
it (top y=63.5), the slab column is 0.5 lower but horizontally closer, so it
wins the 3D distance even though the feet are exactly on y=64.  The support
surface architecture says to pick the surface closest to the body position
and foot height.

The world and body are built from the fixture in
``tests/motion_nav/test_navigation_session.py``.

Observed at 1ea37d7: the air case returns band 64 (the block), the slab case
returns band 63 (the slab).
"""
from __future__ import annotations

from dataclasses import replace

import tests.motion_nav.test_navigation_session as fixture
from mc2p.motion_nav.navigation_session import NavigationSession
from mc2p.motion_nav.world_model import (
    Aabb, BlockGeometry, ObservationStamp, WorldKnowledge,
)


def main() -> None:
    session, current, _ = fixture._gap_session()
    try:
        stamp = ObservationStamp(current.session, 1, 1, "test", 1)
        slab = BlockGeometry(
            "minecraft:smooth_stone_slab", "boxes", (Aabb(0, 0, 0, 1, .5, 1),),
        )
        for label, neighbour in (("air next to the block", None),
                                 ("bottom slab next to the block", slab)):
            knowledge = WorldKnowledge(current.session)
            knowledge.confirm_air(stamp, tuple(
                (x, y, z) for x in range(-2, 4) for y in range(60, 71)
                for z in range(-2, 3)
            ))
            blocks = {(0, 63, 0): BlockGeometry.full_cube("minecraft:grass_block")}
            if neighbour is not None:
                blocks[(1, 63, 0)] = neighbour
            knowledge.observe_blocks(stamp, blocks)
            for body_x in (1.15, 1.20):
                body = replace(
                    current.body, position=(body_x, 64.0, 0.5),
                    body_box=Aabb(body_x - .3, 64.0, .2, body_x + .3, 65.8, .8),
                    is_on_ground=True,
                )
                frame = replace(current, body=body, world=knowledge.view())
                node, _ = NavigationSession._surface_for_body(frame)
                print(f"{label}: feet y=64, body x={body_x:.2f} -> "
                      f"column {node.column_x}, band {node.vertical_band}")
    finally:
        session.close()


if __name__ == "__main__":
    main()

"""D024 grace window when the knockback tick itself needs world cells.

Run from the repository root of a checkout of commit ba4acda:

    PYTHONPATH=. python reviews/2026-09-25-b11-repro/damage_grace_then_missing_world.py

Sequence (ticks as in the public C1-C split, 447 -> 448 -> 449 -> 450):

1. damage at movement tick 448, residual 447->448 MATCHED   -> grace;
2. residual 448->449 NEEDS_WORLD (knockback pushes the body toward cells
   that are not yet known), anchor stays at 448;
3. residual 448->450 DEVIATION once the cells are known.

``_residual_can_attribute_damage`` accepts either ``anchor < damage <= observed``
or exactly ``anchor == damage`` with ``observed == damage + 1``.  Step 3 has
``anchor == damage`` but ``observed == damage + 2``, so the damage fact is
dropped and the same knockback becomes unattributed external motion.

Observed at ba4acda: grace -> motion_residual_unavailable ->
unattributed_external_motion_confirmed.  Before D024 the same input gave
damage_without_motion_residual on step 1, so D024 does not make this worse.
"""
from __future__ import annotations

from mc2p.motion_nav.motion_residual import MotionResidualResult, MotionResidualStatus
from mc2p.motion_nav.external_motion import DamageKnockbackDetector
from tests.motion_nav.test_external_motion import residual, snapshot


def main() -> None:
    detector = DamageKnockbackDetector()
    detector.observe(snapshot(seq=444, tick=447, hurt=0, health=20))
    steps = [
        detector.observe(
            snapshot(seq=445, tick=448, hurt=10, health=17),
            residual(deviation=False, first=447, last=448),
        ),
        detector.observe(
            snapshot(seq=446, tick=449, hurt=9, health=17,
                     velocity=(0.35, 0.18, 0.0), ground=False),
            MotionResidualResult(
                MotionResidualStatus.NEEDS_WORLD, 448, 449,
                missing_cells=((-1, 64, 0),),
            ),
        ),
        detector.observe(
            snapshot(seq=447, tick=450, hurt=8, health=17,
                     velocity=(0.30, 0.10, 0.0), ground=False),
            residual(deviation=True, first=448, last=450),
        ),
    ]
    for index, result in enumerate(steps, start=1):
        source = None if result.event is None else result.event.source.value
        print(f"step {index}: reason={result.reason} event={source}")


if __name__ == "__main__":
    main()

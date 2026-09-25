"""After the D024/D026 change a deviation up to 8 ticks after damage can claim it.

Run from the repository root of a checkout of commit 1ea37d7:

    PYTHONPATH=. python reviews/2026-09-25-final-repro/damage_window_spans_missing_world_chain.py

``_residual_can_attribute_damage`` now accepts any complete residual whose
anchor equals the damage tick.  While the residual keeps asking for world
cells, the anchor stays on the damage tick, so the first complete residual
can span up to the 8-tick replay limit.  A deviation that appears at the end
of that span is merged into ``DAMAGE_KNOCKBACK``.

D024 still says the detector waits exactly one adjacent tick and explains
why a wider window is unsafe (a later piston push could be mistaken for
knockback).  The C1-C architecture text was updated to "one or more ticks";
D024 and the C1-R6 acceptance note were not.

Observed at 1ea37d7: grace at tick 448, six NEEDS_WORLD observations, then a
448->455 deviation -> damage_knockback_confirmed.
"""
from __future__ import annotations

from mc2p.motion_nav.external_motion import DamageKnockbackDetector
from mc2p.motion_nav.motion_residual import MotionResidualResult, MotionResidualStatus
from tests.motion_nav.test_external_motion import residual, snapshot


def main() -> None:
    detector = DamageKnockbackDetector()
    detector.observe(snapshot(seq=444, tick=447, hurt=0, health=20))
    result = detector.observe(
        snapshot(seq=445, tick=448, hurt=10, health=17),
        residual(deviation=False, first=447, last=448),
    )
    print("tick 448:", result.reason)
    for index, tick in enumerate(range(449, 455)):
        result = detector.observe(
            snapshot(seq=446 + index, tick=tick, hurt=9, health=17),
            MotionResidualResult(
                MotionResidualStatus.NEEDS_WORLD, 448, tick,
                missing_cells=((-1, 64, 0),),
            ),
        )
    print("ticks 449-454:", result.reason)
    result = detector.observe(
        snapshot(seq=452, tick=455, hurt=2, health=17,
                 velocity=(0.3, 0.0, 0.0)),
        residual(deviation=True, first=448, last=455),
    )
    source = None if result.event is None else result.event.source.value
    print("tick 455 (448->455 deviation):", result.reason, source)


if __name__ == "__main__":
    main()

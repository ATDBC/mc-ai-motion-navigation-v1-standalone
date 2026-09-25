"""Damage whose motion residual keeps asking for world cells.

Run from the repository root of a checkout of commit d1dad57:

    PYTHONPATH=. python reviews/2026-09-25-c1r-rereview-2-repro/damage_with_missing_world_residual.py

Knockback usually pushes the body backward, into space outside the 120-degree
view.  The residual then reports ``NEEDS_WORLD``.  ``DamageKnockbackDetector``
keeps the damage fact pending for 8 movement ticks and then drops it
(``mc2p/motion_nav/external_motion.py`` around line 389) without emitting
``DAMAGE_WITH_UNVERIFIED_MOTION``.

Observed at d1dad57: 12 airborne ticks after a 3-point hit produce no event.
"""
from __future__ import annotations

from mc2p.motion_nav.external_motion import DamageKnockbackDetector
from mc2p.motion_nav.motion_residual import MotionResidualResult, MotionResidualStatus
from tests.motion_nav.test_external_motion import snapshot


def _needs_world(anchor_tick: int, tick: int) -> MotionResidualResult:
    return MotionResidualResult(
        MotionResidualStatus.NEEDS_WORLD, anchor_tick, tick,
        missing_cells=((0, 62, -2),),
    )


def main() -> None:
    detector = DamageKnockbackDetector()
    detector.observe(snapshot(seq=1, tick=20, hurt=0, health=20))
    events, reasons = [], []
    for index in range(12):
        tick = 21 + index
        result = detector.observe(
            snapshot(seq=2 + index, tick=tick, hurt=max(0, 10 - index),
                     health=17, ground=False),
            _needs_world(20, tick),
        )
        reasons.append(result.reason)
        if result.event is not None:
            events.append((tick, result.event.source.value))
    print("events:", events)
    print("reasons:", sorted(set(reasons)), "x", len(reasons))


if __name__ == "__main__":
    main()

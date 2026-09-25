"""A fully matched residual after the grace tick is now reported as unverified motion.

Run from the repository root of a checkout of commit dd38c6b:

    PYTHONPATH=. python reviews/2026-09-25-d026-repro/damage_grace_then_matched_two_tick_residual.py

D026 item 7 targets a residual chain interrupted by missing world or input
evidence.  The new branch in ``DamageKnockbackDetector.observe`` fires for any
complete residual that is not "adjacent", including one that simply spans two
movement ticks because an observation was skipped.  A MATCHED residual
448->450 proves the body moved exactly as predicted through the knockback
tick, yet it now produces ``DAMAGE_WITH_UNVERIFIED_MOTION`` and conservative
recovery.

Public traces contain such spans: 31 of 1223 non-duplicate residuals in the
C1-C pass batch and 5 of 1823 in the C1-B pass batch cover more than one tick.

Observed at dd38c6b: adjacent MATCHED -> damage_without_motion_residual
(no event); two-tick MATCHED -> damage_motion_unverified_gap with a
damage_with_unverified_motion event.
"""
from __future__ import annotations

from mc2p.motion_nav.external_motion import DamageKnockbackDetector
from tests.motion_nav.test_external_motion import residual, snapshot


def run(label: str, last_tick: int) -> None:
    detector = DamageKnockbackDetector()
    detector.observe(snapshot(seq=444, tick=447, hurt=0, health=20))
    grace = detector.observe(
        snapshot(seq=445, tick=448, hurt=10, health=17),
        residual(deviation=False, first=447, last=448),
    )
    after = detector.observe(
        snapshot(seq=446, tick=last_tick, hurt=9, health=17),
        residual(deviation=False, first=448, last=last_tick),
    )
    source = None if after.event is None else after.event.source.value
    print(f"{label}: {grace.reason} -> {after.reason} event={source}")


if __name__ == "__main__":
    run("MATCHED 448->449 (adjacent)", 449)
    run("MATCHED 448->450 (one observation skipped)", 450)

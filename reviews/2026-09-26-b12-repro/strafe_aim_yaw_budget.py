"""How often the D031 5-degree same-frame yaw limit lets strafing continue.

Run from the repository root of a checkout of commit 2c6401c:

    PYTHONPATH=. python reviews/2026-09-26-b12-repro/strafe_aim_yaw_budget.py

The formal B12-B Fabric batch never executes movement together with a
non-zero combat yaw: every movement+attack frame had yaw delta 0 (the player
was teleported already facing a ``NoAI`` zombie), and all six non-zero-yaw
frames were the large-turn boundary with movement suppressed.  This script
estimates the case the batch does not cover: keeping the target in the
crosshair while strafing around it.

Kinematic model, one frame per movement tick:

* the player circle-strafes at a fixed standoff radius at vanilla walking
  speed (0.2159 block/tick), as a standoff-keeping route would;
* the target stands still (a moving target only adds angular rate);
* combat asks for the full yaw correction each frame (``combat_aim_angles``);
* ``ActionArbiterV1`` decides with the formal ``_within_observed_yaw_limit``
  rule whether the navigation movement survives that look.

This is a geometry estimate, not a Minecraft physics replay (no friction,
acceleration or collision).  It shows where the per-tick bearing change
crosses 5 degrees.

Observed at 2c6401c: at 3.0 and 2.5 blocks every frame keeps the strafe
(required yaw 4.1 and 4.9 degrees, just under the limit); at 2.0 and 1.5
blocks only 50% of frames keep it (6.2 and 8.2 degrees).
"""
from __future__ import annotations

import math

from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, LookV1, MovementV1
from mc2p.contracts.observation import Vec3V0
from mc2p.runtime.arbiter_v1 import _within_observed_yaw_limit
from mc2p.skills.combat_aim import combat_aim_angles

WALK_BLOCKS_PER_TICK = 0.2159


def _intent(sequence: int, **values) -> ActionIntentV1:
    return ActionIntentV1(
        f"intent-{sequence}-{len(values)}", "source", "episode", sequence,
        ActionPriorityV0.TASK, 0, 1_000_000_000, **values,
    )


def run(distance: float, ticks: int = 200) -> tuple[float, float]:
    """Circle-strafe at a fixed standoff radius around a stationary target.

    After each executed strafe step the player stays on the circle, standing
    in for a route that keeps the standoff distance.  A moving target only
    adds its own angular rate, so these numbers are a lower bound.
    """
    angle = 0.0          # player position on the circle, radians
    yaw = 0.0            # facing +z toward the target at start
    moved = 0
    worst_delta = 0.0
    for tick in range(ticks):
        player = (distance * math.sin(angle), -distance * math.cos(angle))
        desired, _ = combat_aim_angles(
            Vec3V0(-player[0], 0.0, -player[1]), Vec3V0(.6, 1.95, .6), 1.62,
        )
        delta = (desired - yaw + 180.0) % 360.0 - 180.0
        worst_delta = max(worst_delta, abs(delta))
        delta = max(-15.0, min(15.0, delta))
        movement = _intent(
            tick, movement=MovementV1(strafe=-1),
            movement_observed_yaw_limit_degrees=5.0,
        )
        look = _intent(tick, look=LookV1(delta, 0.0))
        allowed, _ = _within_observed_yaw_limit(movement, look, tick)
        yaw += delta
        if allowed:
            moved += 1
            angle += WALK_BLOCKS_PER_TICK / distance
    return moved / ticks, worst_delta


def main() -> None:
    for distance in (3.0, 2.5, 2.0, 1.5):
        share, worst = run(distance)
        print(f"standoff {distance:.1f} blocks: strafe kept on {share:.0%} of "
              f"frames (largest requested yaw {worst:.1f} deg)")


if __name__ == "__main__":
    main()

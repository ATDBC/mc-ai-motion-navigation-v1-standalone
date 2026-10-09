"""Edge cases around the H1/H2 entry-handoff repair.

    python <this file> [--late [--p=0.2] [--seeds=8]] [family ...]        (from a checkout root)

Families (formal sim Runtime -> Driver -> Session -> Executor -> Coordinator):
  turn_jumpup   walk +z, turn 90 deg, JumpUp onto a 2-wide step   (frozen family)
  turn_gap      walk +z, turn 90 deg, JumpGap over a 1-wide pit
  turn_drop     walk +z on a 1-high platform, turn 90 deg, step down
  back_jumpup   straight +z JumpUp, start yaw 180 (facing away)
  diag_jumpup   straight +z JumpUp, start yaw 45
  aligned_jumpup straight +z JumpUp, start yaw 0 (already aligned; control)
  aligned_far   as aligned_jumpup, goal 3 cells past the step instead of on
                the landing cell

Per run: outcome, segment kinds, ticks, the lowest horizontal speed in the
8 ticks before the first takeoff (shows whether the body still stops), the
largest single-tick yaw change and the largest yaw overshoot past the
entry yaw after the turn starts.  --late adds random one-tick late inputs
(probability --p=0.2 by default, the rate the project's own closed-loop
and product suites use; seeds --seeds=1..8).
"""
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

import tests.sim.runner as runner
from tests.sim.backend import Scene, Perturbations

STONE = "minecraft:stone"


def base_floor(xs, zs, y=63):
    return {(x, y, z): STONE for x in xs for z in zs}


def turn_jumpup():
    # L corridor: x=0 for z=-1..9, then z=9 for x=0..8; step at x>=3 on z=9.
    s = base_floor([0], range(-1, 10))
    s.update(base_floor(range(0, 9), [9]))
    s.update({(x, 64, 9): STONE for x in range(3, 9)})
    return s, (.5, 64., .5), (5.5, 65., 9.5), 0., -90.


def turn_gap():
    s = base_floor(range(-4, 9), range(-1, 12))
    for z in range(-1, 12):
        s.pop((3, 63, z), None)
    return s, (.5, 64., .5), (5.5, 64., 9.5), 0., -90.


def turn_drop():
    s = base_floor(range(-4, 9), range(-1, 12))
    s.update({(x, 64, z): STONE for x in range(-4, 3) for z in range(-1, 12)})
    return s, (.5, 65., .5), (5.5, 64., 9.5), 0., -90.


def straight(yaw, goal_z=2.5):
    def make():
        s = base_floor(range(-7, 8), range(-7, 8))
        s.update({(x, 64, z): STONE for x in range(-7, 8) for z in (2, 3, 4, 5, 6)})
        return s, (.5, 64., -1.5), (.5, 65., goal_z), yaw, 0.
    return make


FAMILIES = {
    "turn_jumpup": turn_jumpup, "turn_gap": turn_gap, "turn_drop": turn_drop,
    "back_jumpup": straight(180.), "diag_jumpup": straight(45.),
    "aligned_jumpup": straight(0.),
    "aligned_far": straight(0., 5.5),
}


def wrap(a):
    return (a + 180.) % 360. - 180.


def run_one(name, make, late_seed):
    solids, start, goal, yaw, entry_yaw = make()
    pert = Perturbations() if late_seed is None else Perturbations(
        late_ticks=runner.late_ticks(PROB, late_seed))
    rows = []
    kinds = set()

    def step(context):
        from mc2p.contracts.behavior import BehaviorProfileV0
        ex = context.session._executor
        if ex is not None and getattr(ex, "route", None) is not None:
            kinds.update(type(a).__name__ for a in ex.route.actions)
        context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
        st = context.backend.state
        rows.append((math.degrees(st.yaw_radians), st.velocity_blocks_per_tick,
                     st.on_ground, st.position))
        return ()

    scene = Scene(solids, ((-9, 12), (60, 70), (-9, 14)))
    r = runner.run(runner.Scenario(name, scene, start, goal, yaw, max_ticks=400,
                                   perturbations=pert), control_step=step)
    takeoff = next((i for i in range(1, len(rows))
                    if rows[i - 1][2] and not rows[i][2]), None)
    pre = (min(math.hypot(v[0], v[2]) * 20 for _, v, _, _ in rows[max(0, takeoff - 8):takeoff])
           if takeoff else None)
    dy = [abs(wrap(rows[i][0] - rows[i - 1][0])) for i in range(1, len(rows))]
    first_turn = next((i for i, d in enumerate(dy) if d > 0.5), None)
    over = 0.0
    if first_turn is not None:
        init = wrap(entry_yaw - rows[first_turn][0])
        for y, *_ in rows[first_turn + 1:(takeoff or len(rows))]:
            e = wrap(entry_yaw - y)
            if init and e * init < 0:
                over = max(over, abs(e))
    late = "normal" if late_seed is None else f"late s{late_seed}"
    print(f"{name:12s} {late:9s} {r.outcome}/{r.reason} ticks={r.ticks} "
          f"segs={sorted(kinds)} takeoff_tick={takeoff} "
          f"pre_takeoff_min_speed={None if pre is None else round(pre, 3)} b/s "
          f"max_yaw_step={max(dy, default=0):.1f} overshoot={over:.1f} "
          f"final={tuple(round(v, 2) for v in r.final_position)} "
          f"violations={len(r.violations)} damage={r.damage}")


opts = dict(a[2:].split("=", 1) for a in sys.argv[1:] if a.startswith("--") and "=" in a)
PROB = float(opts.get("p", 0.2))
NSEEDS = int(opts.get("seeds", 8))
seeds = [None] + (list(range(1, NSEEDS + 1)) if "--late" in sys.argv else [])
only = [a for a in sys.argv[1:] if not a.startswith("--")]
for name, make in FAMILIES.items():
    if only and name not in only:
        continue
    for seed in seeds:
        run_one(name, make, seed)

"""v9 column-top goals under the project's 20% random one-tick-late model.

    python <this file> [seeds] [start_yaw]   (from a checkout root; default 8, 0)

Same 2x2 raised column as the f2rec-review probes (x 3..4, z 9..10, one
block high, start (0.5, 64, 0.5) facing +z).  Two goals: the landing cell
(3.5, 65, 9.5), where JumpUp is the route's last action, and the frozen
outer-corner goal (4.7, 65, 10.7), which adds a trailing Walk.  Prints
outcome, final position and the route's segment kinds.  start_yaw -90
faces the jump direction from the start, which bypasses the D1 turn and
lets an older checkout run the same late seeds.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

import tests.sim.runner as runner
from tests.sim.backend import Scene, Perturbations

STONE = "minecraft:stone"
solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
solids.update({(x, 64, z): STONE for x in (3, 4) for z in (9, 10)})
scene = Scene(solids, ((-7, 8), (60, 69), (-5, 14)))
n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
yaw = float(sys.argv[2]) if len(sys.argv) > 2 else 0.

for goal in ((3.5, 65., 9.5), (4.7, 65., 10.7)):
    ok = 0
    for seed in [None] + list(range(1, n + 1)):
        pert = Perturbations() if seed is None else Perturbations(
            late_ticks=runner.late_ticks(0.2, seed))
        kinds = []

        def step(context, kinds=kinds):
            from mc2p.contracts.behavior import BehaviorProfileV0
            ex = context.session._executor
            route = getattr(ex, "route", None)
            if route is not None and not kinds:
                kinds.extend(type(a).__name__.replace("Segment", "") for a in route.actions)
            context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
            return ()

        r = runner.run(runner.Scenario("column", scene, (.5, 64., .5), goal, yaw,
                                       max_ticks=300, perturbations=pert),
                       control_step=step)
        ok += seed is not None and r.outcome == "success"
        label = "normal" if seed is None else f"late s{seed}"
        print(f"goal {goal} {label:8s} {r.outcome}/{r.reason} "
              f"final={tuple(round(v, 2) for v in r.final_position)} route={kinds} "
              f"violations={len(r.violations)} damage={r.damage}", flush=True)
    print(f"goal {goal}: late success {ok}/{n}")

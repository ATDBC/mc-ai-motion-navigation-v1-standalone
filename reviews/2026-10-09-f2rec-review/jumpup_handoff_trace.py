"""Trace the Walk -> JumpUp handoff onto the raised 2x2 block.

    python <this file> [goal_x goal_z]      (from a checkout root)

Prints the admitted ActionRoute (segment types, Walk end point, JumpUp edge),
the handoff entry limits, and the last frames before the stall: position,
velocity, applied input, executor state and the FixedRoute decision reason.
"""
import math
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))

import tests.sim.runner as runner
from tests.sim.backend import Scene

STONE = "minecraft:stone"
solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
solids.update({(x, 64, z): STONE for x in (3, 4) for z in (9, 10)})
scene = Scene(solids, ((-7, 8), (60, 69), (-5, 14)))
gx, gz = (float(sys.argv[1]), float(sys.argv[2])) if len(sys.argv) == 3 else (3.5, 9.5)

seen = {"route": None}
frames = []


def step(context):
    from mc2p.contracts.behavior import BehaviorProfileV0
    executor = context.session._executor
    route = getattr(executor, "route", None)
    if route is not None and route is not seen["route"]:
        seen["route"] = route
        print("admitted route:", route.route_id if hasattr(route, "route_id") else "")
        for i, action in enumerate(route.actions):
            kind = type(action).__name__
            if kind == "WalkSegment":
                pts = [(round(p.x, 3), round(p.z, 3)) for p in action.fixed_route.points]
                print(f"  [{i}] Walk points={pts}")
            elif kind == "JumpUpSegment":
                print(f"  [{i}] JumpUp start={action.edge.start} end={action.edge.end}")
            else:
                print(f"  [{i}] {kind}")
        jp = executor.jump_profile
        print(f"  JumpUp entry limits: centre tolerance {jp.entry_center_tolerance_blocks}, "
              f"max entry speed {jp.maximum_entry_speed_blocks_per_second}")
    context.driver.tick(BehaviorProfileV0(), context.clock[0] + 500_000_000)
    controller = getattr(executor, "_controller", None)
    frames.append((context.backend.state.position, context.backend.state.velocity_blocks_per_tick,
                   getattr(executor, "action_index", None), str(getattr(executor, "state", "")),
                   context.session.report.reason, type(controller).__name__,
                   getattr(getattr(controller, "config", None), "endpoint_tolerance_blocks", None)))
    return ()


result = runner.run(runner.Scenario("handoff", scene, (.5, 64., .5), (gx, 65., gz), max_ticks=300),
                    control_step=step)
print(f"outcome {result.outcome}/{result.reason} ticks {result.ticks} final "
      f"{tuple(round(v, 3) for v in result.final_position)}")
for pos, vel, index, state, reason, ctl, tol in frames[-25:]:
    speed = math.hypot(vel[0], vel[2]) * 20
    print(f"  pos=({pos[0]:.3f},{pos[1]:.2f},{pos[2]:.3f}) speed={speed:.3f} b/s action={index} "
          f"exec={state} controller={ctl} endpoint_tol={tol} reason={reason}")

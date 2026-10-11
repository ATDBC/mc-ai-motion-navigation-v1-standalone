"""Facts about the single P3 action jump_gap_1:A3: inputs, goal, proof intervals, branch trajectories."""
import math
from experiments.motion_navigation.trajectory_proto import closed_loop_measurement as M
from experiments.motion_navigation.trajectory_proto.closed_loop import RollingProofOwner, FrozenInputPlan

def lab(c):
    if c.forward == 0 and not c.jump: return "N"
    return ("SJ" if c.sprint else "J") if c.jump else ("S" if c.sprint else "W")

_, _, request, world, inputs, _, _ = M._cases()["jump_gap_1:A3"]
print("inputs:", " ".join(f"{i}:{lab(c)}" for i, c in enumerate(inputs)))
g = request.goal.region
print("goal region x[%.3f,%.3f] y[%.3f,%.3f] z[%.3f,%.3f]" % (g.min_x, g.max_x, g.min_y, g.max_y, g.min_z, g.max_z),
      "min terminal speed", request.minimum_terminal_speed_blocks_per_second, "damage budget", request.task_damage_budget.maximum_expected_damage_points)
print("terrain y=0 along x=0:", "".join("#" if world.cell((0, 0, z)).knowledge.value == "block" else "." for z in range(-3, 10)), "(z=-3..9)")
print("hole depth at z=1..:", [next((y for y in range(-1, -51, -1) if world.cell((0, y, 1)).knowledge.value == "block"), None)])
owner = RollingProofOwner.start(request, world, FrozenInputPlan(inputs), runahead_steps=24)
for b in owner.bundle.trace.branch_states and [owner.bundle.trace]:
    pass
for i, states in enumerate(owner.bundle.trace.branch_states):
    print(f"branch {i} final", tuple(round(v, 3) for v in states[-1].position), "on_ground", states[-1].on_ground)
    print("   z by boundary:", [round(s.position[2], 2) for s in states], )
    print("   ground by boundary:", "".join("G" if s.on_ground else "." for s in states))

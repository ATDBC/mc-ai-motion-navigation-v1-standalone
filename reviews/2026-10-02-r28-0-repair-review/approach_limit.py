"""How close to a ledge or a wall can ordinary walking bring the body?  (run on any checkout)

Sweeps point goals toward a ledge (platform ends at z = 11) and toward a wall face (x = 2) on
flat, fully known ground and prints the outcome and the final body position.

    PYTHONPATH=. python -B approach_limit.py
"""
from tests.sim.backend import Scene
from tests.sim.runner import Scenario, run

STONE = "minecraft:stone"
solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
solids.update({(2, y, z): STONE for y in (64, 65, 66) for z in range(4, 9)})
scene = Scene(solids, ((-6, 6), (60, 68), (-3, 14)))

print("ledge at z = 11.0 (goal box +/-0.20; body half-width 0.30)")
for z in (10.40, 10.50, 10.55, 10.60, 10.65, 10.70, 10.75, 10.85):
    r = run(Scenario("ledge", scene, (0.5, 64.0, 0.5), (0.5, 64.0, z), max_ticks=300))
    print(f"  goal z={z:<6} {r.outcome:<8}{r.reason:<38} final z={r.final_position[2]:.3f}  "
          f"body front to ledge {11.0 - (r.final_position[2] + 0.3):+.3f}")
print("wall face at x = 2.0")
for x in (1.30, 1.40, 1.45, 1.50, 1.55, 1.60, 1.70):
    r = run(Scenario("wall", scene, (0.5, 64.0, 0.5), (x, 64.0, 6.5), max_ticks=300))
    print(f"  goal x={x:<6} {r.outcome:<8}{r.reason:<38} final x={r.final_position[0]:.3f}  "
          f"body face to wall {2.0 - (r.final_position[0] + 0.3):+.3f}")

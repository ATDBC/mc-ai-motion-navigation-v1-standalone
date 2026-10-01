"""Goals that are standable for a player but sit near a ledge or a wall (41c7d9b).

A player can stand with its 0.6-wide box partly over a ledge, or up against a wall, so "come to
me" / follow goals are often there.  For each goal the script records the concrete point that
standable_point_in_region picks, the margin from that point to the +/-0.20 goal box, and the
navigation outcome.

Run from the repository root of a 41c7d9b checkout:  PYTHONPATH=. python -B <this file>
"""
import mc2p.motion_nav.support_surfaces as ss
import mc2p.motion_nav.route_admission as ra
from tests.sim.backend import Scene
from tests.sim.runner import Scenario, run

STONE = "minecraft:stone"
picked = []
original = ss.standable_point_in_region


def recording(world, surface, region, **kwargs):
    result = original(world, surface, region, **kwargs)
    if kwargs.get("connection_from") is not None and result.position is not None:
        x, _, z = result.position
        margin = min(x - region.min_x, region.max_x - x, z - region.min_z, region.max_z - z)
        picked.append((round(x, 3), round(z, 3), round(margin, 3)))
    return result


ra.standable_point_in_region = recording

# Platform: blocks x in [-4, 4], z in [-1, 10]; ledge at z = 11.  Wall: x = 2, z 4..8, y 64..66.
solids = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-1, 11)}
solids.update({(2, y, z): STONE for y in (64, 65, 66) for z in range(4, 9)})
scene = Scene(solids, ((-6, 6), (60, 68), (-3, 14)))

cases = [("ledge", (0.5, 64.0, z)) for z in (10.85, 10.95, 11.05, 11.15)]
cases += [("wall", (x, 64.0, 6.5)) for x in (1.70, 1.75, 1.80, 1.85, 1.90)]
cases += [("open", (0.5, 64.0, 9.5)), ("open", (0.83, 64.0, 9.17))]
print(f"{'kind':<6}{'goal':<22}{'picked point':<20}{'margin':>8}  outcome")
for kind, goal in cases:
    picked.clear()
    result = run(Scenario(f"{kind}", scene, (0.5, 64.0, 0.5), goal, max_ticks=300))
    point = picked[-1] if picked else None
    print(f"{kind:<6}{str(goal):<22}{str(point[:2]) if point else '-':<20}{(point[2] if point else float('nan')):>8}  "
          f"{result.outcome} {result.reason} ticks={result.ticks} final=({result.final_position[0]:.3f}, {result.final_position[2]:.3f})")

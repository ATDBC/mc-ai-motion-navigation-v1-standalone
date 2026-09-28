"""Released risk records accumulate even without any competitor: every look-only alignment frame at a drop
boundary is 'not selected' for movement, releases the reservation, and the next frame opens a new record.
The task ledger never forgets records and refuses new reservations at 64."""
from tests.sim.backend import Scene
from tests.sim.runner import Scenario, run
from tests.sim.scenarios import drop_ledge

STONE = "minecraft:stone"


def summary(r):
    records = r.trace[-1]["risk_actions"]
    states = {}
    for record in records:
        states[record["state"]] = states.get(record["state"], 0) + 1
    return len(records), states


for yaw in (0, 90, 180):
    r = run(Scenario(f"edge_start_yaw{yaw}", drop_ledge(2), (.5, 64.0, 2.5), (.5, 62.0, 4.5),
                     yaw_degrees=yaw, max_ticks=300))
    n, states = summary(r)
    print(f"start at edge facing {yaw:>3} deg -> {r.outcome:<8} {r.reason:<30} risk_records={n} {states}", flush=True)


def terraces(count, run_length=3):
    solids = {}
    top = 63
    z = 0
    for _ in range(count):
        for dz in range(run_length):
            for x in (-1, 0, 1):
                solids[(x, top, z + dz)] = STONE
        z += run_length
        top -= 2
    return Scene(solids, ((-4, 4), (top - 4, 72), (-3, z + 3))), top + 2, z


for count in (4, 8):
    scene, last_top, length = terraces(count)
    r = run(Scenario(f"terraces_{count}", scene, (.5, 64.0, .5), (.5, last_top + 1.0, length - 1.5), max_ticks=1500))
    n, states = summary(r)
    print(f"{count} consecutive 2-block drops -> {r.outcome:<8} {r.reason:<30} ticks={r.ticks} risk_records={n} {states} "
          f"records/drop={n / count:.1f}", flush=True)

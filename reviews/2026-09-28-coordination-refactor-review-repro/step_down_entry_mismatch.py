"""Show which entry field fails for the isolated one-block step down after a two-block approach."""
from mc2p.motion_nav import motion_candidate as mc
from tests.sim.backend import Scene
from tests.sim.runner import Scenario, run

original = mc._state_fits_entry


def fits(actual, expected):
    ok = original(actual, expected)
    if not ok:
        dp = max(abs(a - b) for a, b in zip(actual.position, expected.position))
        dv = max(abs(a - b) for a, b in zip(actual.velocity_blocks_per_tick, expected.velocity_blocks_per_tick))
        print(f"entry mismatch: actual pos={tuple(round(v, 3) for v in actual.position)} "
              f"vel={tuple(round(v, 3) for v in actual.velocity_blocks_per_tick)} on_ground={actual.on_ground} | "
              f"expected pos={tuple(round(v, 3) for v in expected.position)} "
              f"vel={tuple(round(v, 3) for v in expected.velocity_blocks_per_tick)} on_ground={expected.on_ground} | "
              f"max|dpos|={dp:.3f} (tol {mc._ENTRY_POSITION_TOLERANCE}) max|dvel|={dv:.3f} "
              f"(tol {mc._ENTRY_VELOCITY_TOLERANCE_PER_TICK})")
    return ok


mc._state_fits_entry = fits
STONE = "minecraft:stone"
solids = {(0, 63, z): STONE for z in range(2)}
solids[(0, 62, 2)] = STONE
r = run(Scenario("drop1_approach2", Scene(solids, ((-3, 3), (52, 72), (-3, 5))), (.5, 64.0, .5), (.5, 63.0, 2.5),
                 max_ticks=200))
print(r.outcome, r.reason, r.ticks, r.final_position)

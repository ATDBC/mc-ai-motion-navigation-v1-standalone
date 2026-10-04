"""Can a thin follow layer use the R28 contracts as they are?  (b1d43c2, simulator only)

R28-3 completion criterion 12: "a formal follow probe can submit goal revisions only, without
managing generations, cancelling computations or admitting results itself".  This probe is
such a consumer: one KEEP_ACTIVE_ON_REACH task on open, known, flat ground; a target that
walks away along +z at a fixed pace and then stops; the "follow layer" only calls
driver.replace_goal() with the target's next position.  Nothing else.

    PYTHONPATH=. python -B follow_consumer_probe.py

Reported per target pace: task state at the end, how far behind the bot was (blocks, from
the trace), accepted revisions, planning submissions, task recoveries and safety violations.
"""
import math

from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.backend import Scene
from tests.sim.runner import Event, Scenario, _goal, run

STONE = "minecraft:stone"
SOLIDS = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-2, 75)}


def scenario(ticks_per_block: int, *, stop_z: int = 60, lateral: bool = False) -> Scenario:
    events = []
    revision, z, tick = 1, 4, 20
    while z < stop_z:
        z += 1
        revision += 1
        x = .5 + (1.5 * math.sin(z / 4.0) if lateral else 0.)
        position = (x, 64.0, z + .5)

        def change(context, revision=revision, position=position):
            goal = _goal(position, context.risk_policy_id)
            accepted = context.driver.replace_goal(
                "goal", revision, goal, context.clock[0],
                damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points))
            if accepted:
                context.goal_state, context.goal_position = goal, position
                ACCEPTED.append(revision)

        events.append(Event(f"follow_{revision}", lambda ctx, at=tick: ctx.tick >= at, change,
                            kind="goal_revision", goal_revision=revision))
        tick += ticks_per_block
    return Scenario(f"follow-{ticks_per_block}{'-lateral' if lateral else ''}",
                    Scene(dict(SOLIDS), ((-6, 6), (60, 68), (-4, 77))),
                    (.5, 64.0, .5), (.5, 64.0, 4.5), events=events, max_ticks=tick + 200)


ACCEPTED: list[int] = []
print(f"{'case':<20}{'pace b/s':>9}{'end state':>22}{'lag mean':>10}{'lag p95':>9}{'lag max':>9}"
      f"{'final lag':>10}{'accepted':>10}{'plans':>7}{'recoveries':>11}{'violations':>11}")
for ticks_per_block, lateral in ((10, False), (6, False), (5, False), (4, False), (6, True)):
    ACCEPTED.clear()
    sc = scenario(ticks_per_block, lateral=lateral)
    result = run(sc, reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH)
    lags, plans, recoveries, last_goal = [], 0, 0, sc.goal
    goal_by_tick = {}
    for row in result.trace:
        goal = row.get("goal_position") or last_goal
        last_goal = goal
        position = row.get("position")
        if position and goal:
            lags.append(math.dist((position[0], position[2]), (goal[0], goal[2])))
        plans += len(row.get("planning_submissions") or ())
        recoveries = max(recoveries, row.get("retry_approved_total") or 0)
    lags_sorted = sorted(lags)
    p95 = lags_sorted[math.ceil(.95 * len(lags_sorted)) - 1] if lags_sorted else float("nan")
    state = result.trace[-1].get("driver_state") if result.trace else "?"
    print(f"{sc.name:<20}{20 / ticks_per_block:>9.1f}{result.outcome + '/' + str(state):>22}"
          f"{sum(lags) / len(lags):>10.2f}{p95:>9.2f}{max(lags):>9.2f}{lags[-1]:>10.2f}"
          f"{len(ACCEPTED):>6}/{len(sc.events):<3}{plans:>7}{recoveries:>11}{len(result.violations):>11}")
    if result.outcome not in {"success", "timeout"} or result.violations:
        print("    reason:", result.reason, result.violations[:3])

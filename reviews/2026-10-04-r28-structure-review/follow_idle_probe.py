"""Does a satisfied KEEP_ACTIVE task survive a long idle period?  (b1d43c2, simulator only)

R28-4: a persistent task fails after 30 s without progress, but "stable satisfaction pauses the
no-progress clock".  Here the target walks 20 blocks and then stands still for 40 s (800 ticks);
the follow layer keeps the same goal.  Then the target moves again: does the same task follow?

    PYTHONPATH=. python -B follow_idle_probe.py
"""
from mc2p.motion_nav.goal_reach_policy import GoalReachPolicy
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.backend import Scene
from tests.sim.runner import Event, Scenario, _goal, run

STONE = "minecraft:stone"
SOLIDS = {(x, 63, z): STONE for x in range(-4, 5) for z in range(-2, 50)}


def revision_event(tick, revision, position):
    def change(context):
        goal = _goal(position, context.risk_policy_id)
        if context.driver.replace_goal("goal", revision, goal, context.clock[0],
                                       damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points)):
            context.goal_state, context.goal_position = goal, position
    return Event(f"follow_{revision}", lambda ctx: ctx.tick >= tick, change,
                 kind="goal_revision", goal_revision=revision)


events, revision, tick = [], 1, 20
for z in range(5, 25):                      # walk away at 3.3 b/s
    revision += 1
    events.append(revision_event(tick, revision, (.5, 64.0, z + .5)))
    tick += 6
idle_start = tick
tick += 800                                 # stand still for 40 s
for z in range(25, 35):                     # then walk again
    revision += 1
    events.append(revision_event(tick, revision, (.5, 64.0, z + .5)))
    tick += 6
scenario = Scenario("follow-idle", Scene(dict(SOLIDS), ((-6, 6), (60, 68), (-4, 52))),
                    (.5, 64.0, .5), (.5, 64.0, 4.5), events=events, max_ticks=tick + 100)
result = run(scenario, reach_policy=GoalReachPolicy.KEEP_ACTIVE_ON_REACH)
last = result.trace[-1]
print(f"idle from tick {idle_start} to {idle_start + 800}; run ended at tick {result.ticks}")
print(f"outcome {result.outcome}, reason {result.reason}, final position {result.final_position}, goal {last.get('goal_position')}")
states = {}
for row in result.trace:
    states.setdefault((row.get("session_state"), row.get("session_reason")), row.get("tick", row.get("loop_tick")))
print("first tick of each (session state, reason):")
for key, first in states.items():
    print(f"  {first!s:>6}  {key}")
kinds = sorted({(v[0], v[1].split(' ')[0]) for v in result.violations})
print(f"monitor violations: {len(result.violations)}; kinds {kinds}; first {result.violations[:1]}")

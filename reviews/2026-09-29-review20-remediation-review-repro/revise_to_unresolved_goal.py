# Run from the repository root of a 340cac6 checkout: PYTHONPATH=. python -B <script>
"""One goal revision whose new goal surface cannot be resolved yet (outside the known map, or on a column
boundary), issued at different phases of direct_drop_2.  A legal caller action must not raise, and must not
leave the session half-updated."""
from dataclasses import replace
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.runner import Event, _goal, run
from tests.sim.scenarios import SCENARIOS

base = next(s for s in SCENARIOS if s.name == "direct_drop_2")
for goal in ((0.5, 62.0, 30.5), (1.0, 62.0, 4.5)):
    for at in (5, 15, 25, 34, 38, 45, 60):
        def revise(context, goal=goal):
            state_before = context.diagnostics.state.value
            try:
                context.driver.replace_goal("goal", 2, _goal(goal, context.risk_policy_id), context.clock[0],
                                            damage_budget=TaskDamageBudget(context.risk_policy_id,
                                                                           context.damage_points))
                context.goal_position = goal
                revise.outcome = f"accepted in {state_before}"
            except Exception as error:  # noqa: BLE001 - the exception is the finding
                d = context.diagnostics
                revise.outcome = (f"RAISED in {state_before}: {type(error).__name__}: {error}; after: "
                                  f"state={d.state.value} reason={d.reason} goal_rev={d.goal_revision}")
                raise
        revise.outcome = "not fired"
        try:
            r = run(replace(base, events=[Event("revise", lambda c, at=at: c.tick >= at, revise)], max_ticks=300))
            summary = f"{r.outcome}/{r.reason} violations={[v[1] for v in r.violations]}"
        except Exception as error:  # noqa: BLE001
            summary = f"run aborted by {type(error).__name__}"
        print(f"goal={goal} revise@{at:<3} {revise.outcome} -> {summary}", flush=True)

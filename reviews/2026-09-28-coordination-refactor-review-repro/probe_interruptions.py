"""Interruptions while the landing-edge probe owns the body (drop_ledge 2, goal on the lower floor)."""
from dataclasses import replace
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, Scenario, _goal, run
from tests.sim.scenarios import drop_ledge, probe_active

base = Scenario("drop2", drop_ledge(2), (.5, 64.0, .5), (.5, 62.0, 4.5), max_ticks=400)
holding = lambda c: probe_active(c) and c.backend.state.position[2] > 2.75


def release(context):
    context.driver.release("harness_cancel")


def goal_to(position, revision):
    def action(context):
        goal = _goal(position, context.risk_policy_id)
        context.driver.replace_goal("goal", revision, goal, context.clock[0],
                                    damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points))
        context.goal_state = goal
        context.goal_position = position
    return action


def first_tick(predicate):
    return lambda c: predicate(c)


cases = [
    ("cancel_while_holding_edge", dict(events=[Event("cancel", holding, release)], expect="cancelled")),
    ("knockback_toward_edge_while_holding", "impulse"),
    ("landing_removed_after_evidence", "remove_landing"),
    ("goal_flip_flop_during_probe_x4", dict(events=[
        Event("away1", lambda c: probe_active(c), goal_to((.5, 64.0, .5), 2)),
        Event("back1", lambda c: c.tick > 60 and c.goal_position == (.5, 64.0, .5), goal_to((.5, 62.0, 4.5), 3)),
        Event("away2", lambda c: c.tick > 80 and probe_active(c), goal_to((.5, 64.0, .5), 4)),
        Event("back2", lambda c: c.tick > 120 and c.goal_position == (.5, 64.0, .5), goal_to((.5, 62.0, 4.5), 5)),
    ], max_ticks=600)),
]
for name, spec in cases:
    if spec == "impulse":
        # find the tick at which the probe holds the edge, then push toward the drop
        probe = run(base)
        hold = next(row["tick"] for row in probe.trace
                    if "landing_edge_probe" in row["controller_ids"] and row["position"][2] > 2.75)
        scenario = replace(base, name=name, perturbations=Perturbations(impulses={hold + 2: (0.0, 0.0, 0.25)}))
    elif spec == "remove_landing":
        probe = run(base)
        start = next(row["tick"] for row in probe.trace if not row["on_ground"])
        edits = {start - 1: {(x, 61, z): None for x in (-1, 0, 1) for z in (3, 4, 5)}}
        scenario = replace(base, name=name, perturbations=Perturbations(world_edits=edits), expect="failed")
    else:
        scenario = replace(base, name=name, **spec)
    r = run(scenario)
    risk = r.trace[-1]["risk_actions"] if r.trace else ()
    print(f"{name:<38} expect={scenario.expect:<9} -> {r.outcome:<10} {r.reason:<40} ticks={r.ticks:<4} "
          f"pos={r.final_position} damage={r.damage:g} risk_records={len(risk)} "
          f"driver_end={r.trace[-1]['driver_state']}/{r.trace[-1]['driver_reason']} "
          f"violations={[(t, c) for t, c, _ in r.violations]} events={r.events}", flush=True)
